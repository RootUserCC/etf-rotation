#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""盘中实时：ETF 快照（新浪，5s）+ 880003 平均股价（东财 800005，60s）+ S1/S2 增强信号预览。

写 site/live.json（<3KB），前端每 5 秒轮询并只更新数字，不重载 data.json、不重算回测。
休市时写 closed=true 并每 60 秒检查一次；本模块由 server.py 以守护线程启动。
"""
import datetime as dt
import json
import os
import threading
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


def signal_preview(closes, live_value=None, ratio_ctx=None, etfs=None, live_ohlc=None, now=None):
    """在历史收盘序列上追加“当前值”，返回 dif/dea/S1/S2 与增强信号预览（含盘中分时 x_turn 买点监控）。

    口径与 export_json.py 完全一致：
    - 买：880003 DIF(17/34) 零下拐头向上
    - 卖：DIF 拐头向下 + S1(DIF>0 或 20日回落/close > 0.0010) + S2(非 R-DIF(12/26) 零上走强)
    - 盘中分时买点：防守期且昨日 DIF 零下下行时，实时监控 880003 过拐点价 x_turn + 阳线防高开低走 + ≥10:00 节点
    """
    if closes is None or len(closes) < 40 or live_value is None:
        return {'preview': 'none', 'holding': _holding(),
                'note': '休市或无实时平均股价，暂无预览'}
    now_dt = now or dt.datetime.now()
    today = pd.Timestamp(now_dt.date())
    s = closes.copy()
    s.loc[today] = live_value
    s = s.sort_index()
    sig = calc_signals(s, sell_anywhere=True, fast=17, slow=34, sig_n=9)
    last = sig.iloc[-1]
    dif_now, dea_now = float(last['dif']), float(last['dea'])

    # DIF(x) = (a17-a34)*x + C，用严格早于今日的历史收盘 EMA 反解（零前视）
    prev = s.iloc[:-1]
    e17, e34 = ema(prev, 17), ema(prev, 34)
    dif_prev_series = e17 - e34
    a17, a34 = 2.0 / 18, 2.0 / 35
    c = (1 - a17) * float(e17.iloc[-1]) - (1 - a34) * float(e34.iloc[-1])
    dif_prev = float(dif_prev_series.iloc[-1])
    dif_prev2 = float(dif_prev_series.iloc[-2]) if len(dif_prev_series) > 1 else dif_prev
    prev_close = float(prev.iloc[-1])
    x_turn = (dif_prev - c) / (a17 - a34)
    x_turn_buf = x_turn * 1.005
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

    def pct_base(x):
        return None if prev_close == 0 else round((x / prev_close - 1) * 100, 2)

    # 盘中分时买点评估（日线 x_turn 投影 + 阳线防高开低走 + ≥10:00 节点）
    avg_open = float(live_ohlc[0]) if live_ohlc and len(live_ohlc) >= 1 and live_ohlc[0] else None
    avg_yang = bool(avg_open is not None and cur > avg_open)
    not_gap_trap = bool(avg_open is None or avg_open < x_turn)

    etf_map = {e['code']: e for e in (etfs or []) if e and e.get('code')}
    e552 = etf_map.get('159552')
    if e552 and e552.get('price') and e552.get('open'):
        etf_yang = bool(e552['price'] > e552['open'])
    else:
        etf_yang = avg_yang if avg_open is not None else True

    eligible_buy = bool(holding == 'div' and dif_prev < 0 and dif_prev < dif_prev2)
    cross_turn = bool(cur >= x_turn and cur < x_zero)
    cross_buf = bool(cur >= x_turn_buf and cur < x_zero)
    t_now = now_dt.time()
    time_ok = bool(t_now >= dt.time(10, 0))
    if t_now < dt.time(10, 30):
        next_cp = '10:30'
    elif t_now < dt.time(11, 30):
        next_cp = '11:30'
    elif t_now < dt.time(14, 0):
        next_cp = '14:00'
    else:
        next_cp = '14:55'
    in_cp_window = bool(
        (dt.time(10, 25) <= t_now <= dt.time(10, 35))
        or (dt.time(11, 25) <= t_now <= dt.time(13, 5))
        or (dt.time(13, 55) <= t_now <= dt.time(14, 5))
    )

    yang_ok = bool((avg_open is None or avg_yang) and etf_yang)
    if holding == 'atk':
        ib_status = 'holding_atk'
        ib_label = '已持进攻仓'
        ib_hint = f'当前持进攻仓，无需分时买点；DIF 转降临界价 {x_turn:.2f}（距现价 {pct(x_turn):+.2f}%）'
    elif not eligible_buy:
        ib_status = 'standby'
        ib_label = '非零下待买态'
        ib_hint = '昨日 DIF 未处于零下下行状态（或已拐头待执行），无需盘中提前触发'
    elif cross_turn and not yang_ok:
        ib_status = 'blocked_gap'
        ib_label = '阴线/高开低走拦截'
        ib_hint = (f'虽过拐点价 {x_turn:.2f}，但当日走阴（880003 或 159552 低于开盘价），'
                   '按回测纪律拦截以规避 11-24 类高开低走陷阱')
    elif cross_turn and not time_ok:
        ib_status = 'wait_time'
        ib_label = '10:00 前暂观察'
        ib_hint = f'已过拐点价 {x_turn:.2f} 且走阳，跳过开盘前 30 分钟脉冲，待 10:00 后（推荐 10:30/11:30 节点）站稳再买'
    elif cross_turn:
        ib_status = 'triggered'
        ib_strong = bool(cross_buf and not_gap_trap)
        ib_label = '盘中买点强触发' if ib_strong else '盘中买点触发'
        cp_tip = '当前正处推荐检查节点，可冷静限价切入进攻仓' if in_cp_window else f'建议在 {next_cp} 节点（最简 11:30→13:00）限价切入进攻仓'
        gap_warn = '' if not_gap_trap else '（注：今日高开过线，建议先半仓或等 11:30 确认）'
        ib_hint = (f'880003 站上拐点价 {x_turn:.2f} 且当日阳线！{cp_tip}{gap_warn}；'
                   '若 15:00 收盘跌回拐点下方，次日 09:35 认错卖出转现金/T+0 货基防锁仓')
    else:
        ib_status = 'watch'
        ib_label = '防守待机监控'
        ib_hint = (f'防守期监控中：880003 距拐点价 {x_turn:.2f} 还差 {pct(x_turn):+.2f}%'
                   f'（+0.5% 缓冲价 {x_turn_buf:.2f}），推荐盯 10:30 / 11:30 / 14:00 节点')

    intraday_buy = {
        'status': ib_status,
        'label': ib_label,
        'eligible': eligible_buy,
        'x_turn': round(x_turn, 2),
        'x_turn_pct_cur': pct(x_turn),
        'x_turn_pct_base': pct_base(x_turn),
        'x_turn_buf': round(x_turn_buf, 2),
        'x_turn_buf_pct_cur': pct(x_turn_buf),
        'x_zero': round(x_zero, 2),
        'avg_open': round(avg_open, 2) if avg_open is not None else None,
        'cross_turn': cross_turn,
        'cross_buf': cross_buf,
        'avg_yang': avg_yang,
        'etf_yang': etf_yang,
        'yang_ok': yang_ok,
        'not_gap_trap': not_gap_trap,
        'time_ok': time_ok,
        'in_cp_window': in_cp_window,
        'next_cp': next_cp,
        'hint': ib_hint,
    }

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
        'intraday_buy': intraday_buy,
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
            'open': round(q['open'], 3) if q.get('open') else None,
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


class LiveEngine:
    """按需实时行情引擎：仅在网页打开请求 /api/live 时按节流间隔抓取，无访问时零网络请求、零 CPU。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._closes = None
        self._ratio_ctx = None
        self._live_avg = None
        self._live_ohlc = None
        self._live_date = ''
        self._avg_ts = 0.0
        self._avg_err = ''
        self._etfs = []
        self._quote_src = 'sina'
        self._etf_ts = 0.0
        self._closed_ts = 0.0
        self._cached_doc = None
        self._warm_load()

    def _warm_load(self):
        prev_live = _load_json(LIVE_FILE)
        if prev_live.get('today') == dt.date.today().isoformat():
            pa = prev_live.get('avg') or {}
            if pa.get('value') is not None:
                self._live_avg = float(pa['value'])
                self._live_ohlc = pa.get('ohlc')
                self._live_date = prev_live.get('today', '')

    def get_snapshot(self, force: bool = False) -> dict:
        with self._lock:
            now = time.time()
            if not md.is_trading_time():
                if force or self._cached_doc is None or not self._cached_doc.get('closed') or (now - self._closed_ts >= 30):
                    doc = _closed_doc(_avg_closes())
                    self._cached_doc = doc
                    self._closed_ts = now
                    try:
                        os.makedirs(SITE_DIR, exist_ok=True)
                        _write(doc)
                    except Exception:  # noqa: BLE001
                        pass
                return self._cached_doc

            # 交易时段内：仅按节流间隔刷新（ETF 5s，平均股价 60s）
            if not force and self._cached_doc and not self._cached_doc.get('closed') and (now - self._etf_ts < LOOP_SEC):
                return self._cached_doc

            if self._closes is None:
                self._closes = _avg_closes()
            if self._ratio_ctx is None:
                self._ratio_ctx = _load_ratio_history()

            avg_interval = 10 if self._live_avg is None else AVG_SEC
            if force or (now - self._avg_ts >= avg_interval):
                try:
                    daily, _ = md.em_avg_daily()
                    day = daily.index[-1]
                    row = daily.iloc[-1]
                    self._live_avg = float(row['close'])
                    self._live_ohlc = [round(float(row['open']), 2), round(float(row['high']), 2),
                                       round(float(row['low']), 2), round(float(row['close']), 2)]
                    self._live_date = str(day.date())
                    self._avg_err = ''
                except Exception as e:  # noqa: BLE001 - 东财限流时保留上一笔
                    self._avg_err = repr(e)[:80]
                self._avg_ts = now
                self._closes = _avg_closes()
                self._ratio_ctx = _load_ratio_history()

            try:
                self._etfs, self._quote_src = _etf_quotes()
            except Exception:  # noqa: BLE001 - 快照临时异常保留上一次报价
                pass
            self._etf_ts = now

            doc = {
                'time': dt.datetime.now().strftime('%H:%M:%S'),
                'closed': False,
                'today': self._live_date,
                'avg': {'value': round(self._live_avg, 2) if self._live_avg else None,
                        'ohlc': self._live_ohlc,
                        'prev': None, 'chg_pct': None, 'date': '',
                        'source': 'eastmoney:800005' if not self._avg_err else 'cache',
                        'error': self._avg_err},
                'sig': signal_preview(self._closes, self._live_avg, ratio_ctx=self._ratio_ctx,
                                      etfs=self._etfs, live_ohlc=self._live_ohlc) or {},
                'etfs': self._etfs,
                'quote_source': self._quote_src,
                'stale': bool(self._avg_err),
                'mode': 'on_demand',
            }
            base = _load_json(os.path.join(SITE_DIR, 'data.json'))
            val = doc['avg']['value']
            if val and self._closes is not None and len(self._closes) > 1:
                doc['avg']['date'] = str(self._closes.index[-1].date())
                doc['avg']['prev'] = round(float(self._closes.iloc[-1]), 2)
                doc['avg']['chg_pct'] = round((val / float(self._closes.iloc[-1]) - 1) * 100, 2)
            doc['sig']['holding'] = doc['sig'].get('holding') or _holding()
            if not base:
                doc['stale'] = True

            self._cached_doc = doc
            try:
                os.makedirs(SITE_DIR, exist_ok=True)
                _write(doc)
            except Exception:  # noqa: BLE001
                pass
            return doc


_ENGINE = None
_ENGINE_LOCK = threading.Lock()


def get_live_snapshot(force: bool = False) -> dict:
    """供 server.py 的 /api/live 按需调用（网页关闭时不执行任何抓取）。"""
    global _ENGINE
    with _ENGINE_LOCK:
        if _ENGINE is None:
            _ENGINE = LiveEngine()
    try:
        return _ENGINE.get_snapshot(force=force)
    except Exception as e:  # noqa: BLE001
        return {'time': dt.datetime.now().strftime('%H:%M:%S'), 'closed': False,
                'error': repr(e)[:120], 'etfs': [], 'sig': {}, 'stale': True}


def main():
    """独立命令行持续轮询模式（常规本地服务由 server.py /api/live 按需触发，无需常驻运行）。"""
    os.makedirs(SITE_DIR, exist_ok=True)
    while True:
        doc = get_live_snapshot()
        time.sleep(60 if doc.get('closed') else LOOP_SEC)


if __name__ == '__main__':
    main()
