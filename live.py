#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""盘中实时：ETF 快照（新浪，5s）+ 880003 平均股价（东财 800005，60s）+ 信号预览。

写 site/live.json（<2KB），前端每 5 秒轮询并只更新数字，不重载 data.json、不重算回测。
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


def _holding():
    """当前应持：atk=进攻仓 / div=防守仓（来自最近一次导出的持仓明细）"""
    d = _load_json(os.path.join(SITE_DIR, 'data.json'))
    legs = d.get('legs') or []
    if not legs:
        return None
    return 'atk' if legs[-1].get('asset') == '1000' else 'div'


def signal_preview(closes, live_value=None):
    """在历史收盘序列上追加“当前值”，返回 dif/dea/柱值与信号预览。

    通达信口径：DIF=EMA(17)-EMA(34)，DEA=EMA(DIF,9)；
    买 = DIF 零下拐头向上，卖 = DIF 拐头向下（任意位置，方案C）。
    另给出翻转所需的 880003 收盘价（DIF 对当日收盘价是线性的，可直接解）。
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
    x_turn = (dif_prev - c) / (a17 - a34)      # DIF 与昨日持平的价格
    x_zero = (0.0 - c) / (a17 - a34)           # DIF 归零的价格
    cur = float(s.iloc[-1])

    holding = _holding()
    preview, note = 'none', ''
    if dif_now < 0:
        if holding == 'div' and dif_now > dif_prev:
            preview = 'buy'
            note = '若现在收盘：DIF 零下拐头向上 → 明日开盘买入进攻仓'
        elif holding == 'atk' and dif_now < dif_prev:
            preview = 'sell'
            note = '若现在收盘：DIF 拐头向下 → 明日开盘卖出进攻仓'
        elif holding == 'div':
            note = '若现在收盘：无买入信号（需 DIF 由降转升且仍在零下）'
        else:
            note = '若现在收盘：继续持有进攻仓（DIF 未拐头向下）'
    else:
        if holding == 'atk' and dif_now < dif_prev:
            preview = 'sell'
            note = '若现在收盘：DIF 拐头向下 → 明日开盘卖出进攻仓'
        elif holding == 'div':
            note = '若现在收盘：无买入信号（DIF 在零上，方案C 只在零下买入）'
        else:
            note = '若现在收盘：继续持有进攻仓'

    def pct(x):
        return None if cur == 0 else round((x / cur - 1) * 100, 2)

    return {
        'dif': round(dif_now, 4), 'dea': round(dea_now, 4),
        'macd': round((dif_now - dea_now) * 2, 4),
        'diff': round(dif_now - dif_prev, 4),
        'holding': holding, 'preview': preview, 'note': note,
        'turn_price': round(x_turn, 2), 'turn_pct': pct(x_turn),
        'zero_price': round(x_zero, 2), 'zero_pct': pct(x_zero),
    }


def _etf_quotes():
    """ETF 实时报价：新浪批量快照"""
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
            'holding': _holding(), 'preview': 'none', 'note': '休市',
        },
        'etfs': [],
        'stale': bool(d.get('avg_stale')),
    }


def main():
    os.makedirs(SITE_DIR, exist_ok=True)
    closes = _avg_closes()
    live_avg = None
    live_ohlc = None      # 当日盘中临时 K 线 [open, high, low, close]
    live_date = ''        # 该临时点对应的交易日
    avg_ts = 0.0
    avg_err = ''
    while True:
        try:
            now = time.time()
            if not md.is_trading_time():
                _write(_closed_doc(_avg_closes()))
                time.sleep(60)
                continue

            if now - avg_ts >= AVG_SEC:
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
                'sig': signal_preview(closes, live_avg) or {},
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
