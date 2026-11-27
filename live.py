#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""盘中实时：ETF 快照（新浪，5s）+ 880003 平均股价（东财 800005，60s）+ S1/S2 增强信号预览。

写 site/live.json（<3KB），前端每 5 秒轮询并只更新数字，不重载 data.json、不重算回测。
休市时写 closed=true 并每 60 秒检查一次；本模块由 server.py 以守护线程启动。
"""
import datetime as dt
import json
import os
import time

import pandas as pd

import market_data as md
from backtest import calc_signals, ema

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, 'data')
SITE_DIR = os.path.join(BASE, 'site')
LIVE_FILE = os.path.join(SITE_DIR, 'live.json')
LOOP_SEC = 5
AVG_SEC = 60

S1_WIN = 20
S1_TH = 0.0010
S2_FAST = 12
S2_SLOW = 26
SWITCH_ATK = pd.Timestamp('2024-06-28')


def _load_json(path, default=None):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return default if default is not None else {}


def _write(doc):
    tmp = LIVE_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(doc, f, ensure_ascii=False)
    os.replace(tmp, LIVE_FILE)


def _avg_closes():
    p = os.path.join(DATA_DIR, 'avg_880003.csv')
    if not os.path.exists(p):
        return None
    df = md.read_avg_csv(p)
    s = df['close'].copy()
    s.index = s.index.normalize()
    return s[~s.index.duplicated(keep='last')].sort_index()


def _load_ratio_history():
    """加载用于 S2 风格比值 R-DIF(12/26) 的历史比值序列与不复权→后复权锚定因子"""
    try:
        h1 = md.read_price_csv(os.path.join(DATA_DIR, 'etf_512100_hfq.csv'))
        h2 = md.read_price_csv(os.path.join(DATA_DIR, 'etf_159552_hfq.csv'))
        hd = md.read_price_csv(os.path.join(DATA_DIR, 'etf_512890_hfq.csv'))
        r2 = md.read_price_csv(os.path.join(DATA_DIR, 'etf_159552.csv'))
        rd = md.read_price_csv(os.path.join(DATA_DIR, 'etf_512890.csv'))
    except Exception:  # noqa: BLE001
        return None

    atk_idx = h1.index[h1.index < SWITCH_ATK].union(h2.index[h2.index >= SWITCH_ATK])
    idx = atk_idx.intersection(hd.index)
    if len(idx) < 40:
        return None

    a1 = h1.reindex(idx).ffill()
    a2 = h2.reindex(idx).ffill()
    vals = [1.0]
    for i in range(1, len(idx)):
        d = idx[i]
        if d < SWITCH_ATK:
            r = float(a1['close'].iloc[i]) / float(a1['close'].iloc[i - 1])
        elif d == SWITCH_ATK:
            r = (float(a1['open'].iloc[i]) / float(a1['close'].iloc[i - 1])
                 * float(a2['close'].iloc[i]) / float(a2['open'].iloc[i]))
        else:
            r = float(a2['close'].iloc[i]) / float(a2['close'].iloc[i - 1])
        vals.append(vals[-1] * r)
    atk_nav = pd.Series(vals, index=idx)
    div_c = hd.reindex(idx).ffill()['close']
    ratio = atk_nav / (div_c / div_c.iloc[0])

    # 最新交易日的原始收盘价（用于把盘中实时涨跌幅准确拼接到比值上）
    last_d = idx[-1]
    last_atk_raw = float(r2['close'].loc[:last_d].iloc[-1])
    last_div_raw = float(rd['close'].loc[:last_d].iloc[-1])
    return {
        'ratio': ratio,
        'last_atk_raw': last_atk_raw,
        'last_div_raw': last_div_raw,
        'last_ratio': float(ratio.iloc[-1]),
    }


def _calc_live_s2(ratio_ctx, etfs):
    """结合盘中 159552 与 512890 实时价计算 S2 风格比值 R-DIF(12/26) 与走强状态"""
    if not ratio_ctx:
        return None, False
    px_map = {e['code']: e.get('price') for e in (etfs or []) if e.get('price')}
    p_atk = px_map.get('159552')
    p_div = px_map.get('512890')
    r_series = ratio_ctx['ratio'].copy()
    today = pd.Timestamp.now().normalize()
    if p_atk and p_div and ratio_ctx['last_atk_raw'] > 0 and ratio_ctx['last_div_raw'] > 0:
        atk_chg = p_atk / ratio_ctx['last_atk_raw']
        div_chg = p_div / ratio_ctx['last_div_raw']
        r_series.loc[today] = ratio_ctx['last_ratio'] * (atk_chg / div_chg)
        r_series = r_series.sort_index()
    r_dif = ema(r_series, S2_FAST) - ema(r_series, S2_SLOW)
    cur = float(r_dif.iloc[-1])
    prev = float(r_dif.iloc[-2]) if len(r_dif) > 1 else cur
    return cur, bool(cur > 0 and cur > prev)


def _holding():
    """当前应持：atk=进攻仓 / div=防守仓（来自最近一次导出的持仓明细）"""
    d = _load_json(os.path.join(SITE_DIR, 'data.json'))
    legs = d.get('legs') or []
    if not legs:
        return None
    return 'atk' if legs[-1].get('asset') == '1000' else 'div'


def signal_preview(closes, live_value=None, ratio_ctx=None, etfs=None):
    """在历史收盘序列上追加“当前值”，返回 dif/dea/S1/S2 与增强信号预览。

    口径与 export_json.py 完全一致：
    - 买：880003 DIF(17/34) 零下拐头向上
    - 卖：DIF 拐头向下 + S1(DIF>0 或 20日回落/close > 0.0010) + S2(非 R-DIF(12/26) 零上走强)
    """
    if closes is None or len(closes) < 40 or live_value is None:
        return {'preview': 'none', 'holding': _holding(),
                'note': '休市或无实时平均股价，暂无预览'}
    today = pd.Timestamp.now().normalize()
    s = closes.copy()
    s.loc[today] = live_value
    s = s.sort_index()
    sig = calc_signals(s, sell_anywhere=True, fast=17, slow=34, sig_n=9)
    last = sig.iloc[-1]
    dif_now, dea_now = float(last['dif']), float(last['dea'])

    # DIF(x) = (a17-a34)*x + C，用前一日的 EMA 反解
    prev = s.iloc[:-1]
    e17, e34 = ema(prev, 17), ema(prev, 34)
    a17, a34 = 2.0 / 18, 2.0 / 35
    c = (1 - a17) * float(e17.iloc[-1]) - (1 - a34) * float(e34.iloc[-1])
    dif_prev = float(sig['dif'].iloc[-2]) if len(sig) > 1 else dif_now
    x_turn = (dif_prev - c) / (a17 - a34)
    x_zero = (0.0 - c) / (a17 - a34)
    cur = float(s.iloc[-1])

    # S1 零下回落比值 & S2 风格比值
    dif_max20 = float(sig['dif'].tail(S1_WIN).max())
    rel_drop = (dif_max20 - dif_now) / cur if cur > 0 else 0.0
    s1_pass = (dif_now > 0) or (rel_drop > S1_TH)
    r_dif, r_strong = _calc_live_s2(ratio_ctx, etfs)

    holding = _holding()
    preview, note, blocked_reason = 'none', '', ''
    if dif_now < 0:
        if holding == 'div' and dif_now > dif_prev:
            preview = 'buy'
            note = '若现在收盘：DIF 零下拐头向上 → 明日开盘买入进攻仓（T1/T3买159552；T2+/T3+买60%159552+40%588220）'
        elif holding == 'atk' and dif_now < dif_prev:
            if not s1_pass:
                blocked_reason = f'S1 零下回落 {rel_drop*1000:.2f}‰ ≤ 1.0‰'
                note = f'若现在收盘：DIF 拐头向下，但被 {blocked_reason} 屏蔽 → 维持进攻仓'
            elif r_strong:
                blocked_reason = f'S2 风格比值 R-DIF({r_dif:+.4f}) 零上走强'
                note = f'若现在收盘：DIF 拐头向下，但被 {blocked_reason} 屏蔽 → 维持进攻仓'
            else:
                preview = 'sell'
                note = '若现在收盘：DIF 拐头向下且通过 S1+S2 → T1/T3 明日开盘切防守；T2+/T3+ 可在 14:55 后尾盘切防守'
        elif holding == 'div':
            note = '若现在收盘：无买入信号（需 DIF 由降转升且仍在零下）'
        else:
            note = '若现在收盘：继续持有进攻仓（DIF 未拐头向下）'
    else:
        if holding == 'atk' and dif_now < dif_prev:
            if r_strong:
                blocked_reason = f'S2 风格比值 R-DIF({r_dif:+.4f}) 零上走强'
                note = f'若现在收盘：DIF 高位拐头向下，但被 {blocked_reason} 屏蔽 → 维持进攻仓'
            else:
                preview = 'sell'
                note = '若现在收盘：DIF 高位拐头向下且通过 S2 → T1/T3 明日开盘切防守；T2+/T3+ 可在 14:55 后尾盘切防守'
        elif holding == 'div':
            note = '若现在收盘：无买入信号（DIF 在零上，仅零下买入）'
        else:
            note = '若现在收盘：继续持有进攻仓'

    def pct(x):
        return None if cur == 0 else round((x / cur - 1) * 100, 2)

    return {
        'dif': round(dif_now, 4), 'dea': round(dea_now, 4),
        'macd': round((dif_now - dea_now) * 2, 4),
        'diff': round(dif_now - dif_prev, 4),
        's1_rel_drop': round(rel_drop, 5),
        'r_dif': None if r_dif is None else round(r_dif, 4),
        'r_strong': bool(r_strong),
        'blocked_reason': blocked_reason,
        'holding': holding, 'preview': preview, 'note': note,
        'turn_price': round(x_turn, 2), 'turn_pct': pct(x_turn),
        'zero_price': round(x_zero, 2), 'zero_pct': pct(x_zero),
    }


def _etf_quotes():
    """ETF 实时报价：新浪批量快照（覆盖全部 md.SYMBOLS，含 518880 / 513100）"""
    pairs = [(m, c) for c, m, _ in md.SYMBOLS]
    snap, source = md.sina_snapshot(pairs), 'sina'
    names = {c: n for c, _, n in md.SYMBOLS}
    out = []
    for code, _, _ in md.SYMBOLS:
        q = snap.get(code)
        if not q or not q['prev']:
            continue
        out.append({
            'code': code, 'name': names.get(code, q['name'][:8]),
            'price': round(q['price'], 3),
            'chg_pct': round((q['price'] / q['prev'] - 1) * 100, 2),
        })
    return out, source


def _closed_doc(closes):
    """休市：用已导出的数据展示最后状态"""
    d = _load_json(os.path.join(SITE_DIR, 'data.json'))
    dif = d.get('dif') or []
    dea = d.get('dea') or []
    s1 = d.get('s1_rel_drop') or []
    rd = d.get('r_dif') or []
    rs = d.get('r_strong') or []
    val = float(closes.iloc[-1]) if closes is not None and len(closes) else None
    prev = float(closes.iloc[-2]) if closes is not None and len(closes) > 1 else None
    return {
        'time': dt.datetime.now().strftime('%H:%M:%S'),
        'closed': True,
        'avg': {
            'value': round(val, 2) if val is not None else None,
            'prev': round(prev, 2) if prev is not None else None,
            'chg_pct': round((val / prev - 1) * 100, 2) if val and prev else None,
            'date': str(closes.index[-1].date()) if closes is not None and len(closes) else '',
        },
        'sig': {
            'dif': dif[-1] if dif else None, 'dea': dea[-1] if dea else None,
            's1_rel_drop': s1[-1] if s1 else None,
            'r_dif': rd[-1] if rd else None,
            'r_strong': bool(rs[-1]) if rs else False,
            'holding': _holding(), 'preview': 'none', 'note': '休市',
        },
        'etfs': [],
        'stale': bool(d.get('avg_stale')),
    }


def main():
    os.makedirs(SITE_DIR, exist_ok=True)
    closes = _avg_closes()
    ratio_ctx = _load_ratio_history()
    live_avg = None
    live_ohlc = None
    live_date = ''
    # 热启动：若已有当日实时均价缓存，先承接避免重启瞬间空白
    prev_live = _load_json(LIVE_FILE)
    if prev_live.get('today') == dt.date.today().isoformat():
        pa = prev_live.get('avg') or {}
        if pa.get('value') is not None:
            live_avg = float(pa['value'])
            live_ohlc = pa.get('ohlc')
            live_date = prev_live.get('today', '')
    avg_ts = 0.0
    avg_err = ''
    while True:
        try:
            now = time.time()
            if not md.is_trading_time():
                _write(_closed_doc(_avg_closes()))
                time.sleep(60)
                continue

            avg_interval = 10 if live_avg is None else AVG_SEC
            if now - avg_ts >= avg_interval:
                try:
                    daily, pre = md.em_avg_daily()
                    day = daily.index[-1]
                    row = daily.iloc[-1]
                    live_avg = float(row['close'])
                    live_ohlc = [round(float(row['open']), 2), round(float(row['high']), 2),
                                 round(float(row['low']), 2), round(float(row['close']), 2)]
                    live_date = str(day.date())
                    avg_err = ''
                except Exception as e:  # noqa: BLE001 - 东财限流时保留上一笔
                    avg_err = repr(e)[:80]
                avg_ts = now
                closes = _avg_closes()
                ratio_ctx = _load_ratio_history()

            etfs, quote_src = _etf_quotes()
            doc = {
                'time': dt.datetime.now().strftime('%H:%M:%S'),
                'closed': False,
                'today': live_date,
                'avg': {'value': round(live_avg, 2) if live_avg else None,
                        'ohlc': live_ohlc,
                        'prev': None, 'chg_pct': None, 'date': '',
                        'source': 'eastmoney:800005' if not avg_err else 'cache',
                        'error': avg_err},
                'sig': signal_preview(closes, live_avg, ratio_ctx=ratio_ctx, etfs=etfs) or {},
                'etfs': etfs,
                'quote_source': quote_src,
                'stale': bool(avg_err),
            }
            base = _load_json(os.path.join(SITE_DIR, 'data.json'))
            val = doc['avg']['value']
            if val and closes is not None and len(closes) > 1:
                doc['avg']['date'] = str(closes.index[-1].date())
                doc['avg']['prev'] = round(float(closes.iloc[-1]), 2)
                doc['avg']['chg_pct'] = round((val / float(closes.iloc[-1]) - 1) * 100, 2)
            doc['sig']['holding'] = doc['sig'].get('holding') or _holding()
            if not base:
                doc['stale'] = True
            _write(doc)
        except Exception as e:  # noqa: BLE001 - 实时线程任何异常都不能退出
            try:
                _write({'time': dt.datetime.now().strftime('%H:%M:%S'), 'closed': False,
                        'error': repr(e)[:120], 'etfs': [], 'sig': {}, 'stale': True})
            except Exception:  # noqa: BLE001
                pass
        time.sleep(LOOP_SEC)


if __name__ == '__main__':
    main()
