#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
导出网站数据：平均股价(880003) + 512100/510880（后复权）+ 方案C信号/换仓点（MACD 17/34/9）
输出 site/data.json
"""
import sys
import io
import os
import json

import pandas as pd

if sys.platform == 'win32' and not getattr(sys.stdout, '_utf8_wrapped', False):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stdout._utf8_wrapped = True

from backtest import run_backtest, calc_signals

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, 'data')
SITE_DIR = os.path.join(BASE, 'site')
os.makedirs(SITE_DIR, exist_ok=True)


def load(name):
    df = pd.read_csv(os.path.join(DATA_DIR, name), parse_dates=['date'])
    df = df.set_index('date').sort_index()
    df.index = df.index.normalize()
    return df


def main():
    avg = load('avg_880003.csv')
    etf1000 = load('etf_512100_hfq.csv')
    etfdiv = load('etf_510880_hfq.csv')

    # 方案C全历史信号与换仓（MACD 17/34/9 慢速拐点；回测窗口=512100上市起，保证净值/持仓区间完整）
    res = run_backtest(avg, etf1000, etfdiv, fee=0.0001, sell_anywhere=True,
                       fast=17, slow=34, sig_n=9, label='C', verbose=False)
    sig_full = calc_signals(avg['close'], sell_anywhere=True,
                            fast=17, slow=34, sig_n=9)

    idx = res['idx']
    dates = [d.strftime('%Y-%m-%d') for d in idx]

    def series(df):
        s = df.loc[idx, 'close']
        return [round(float(v), 3) for v in s.values]

    # 持仓区间（持有1000ETF的连续段）
    hold = res['hold1000'].values
    spans = []
    start_i = None
    for i, h in enumerate(hold):
        if h and start_i is None:
            start_i = i
        elif not h and start_i is not None:
            spans.append([dates[start_i], dates[i - 1]])
            start_i = None
    if start_i is not None:
        spans.append([dates[start_i], dates[-1]])

    # 换仓点：信号于 T-1 日收盘产生（DIF/DEA/MACD柱 取信号日数值），T 日开盘成交
    sig = sig_full.loc[idx]
    pos_of = {d: i for i, d in enumerate(idx)}
    nav = res['nav_strat']
    prev_nav = 1.0
    trades = []
    for d, a, p in res['trades']:
        i = pos_of[d]
        si = max(i - 1, 0)                    # 信号日 = 成交日前一交易日
        nav_i = float(nav.iloc[i])
        trades.append({
            'date': d.strftime('%Y-%m-%d'),
            'sig_date': idx[si].strftime('%Y-%m-%d'),
            'action': 'buy' if a.startswith('买入') else 'sell',
            'price': round(float(p), 3),
            'p1000': round(float(res['p1000_c'].iloc[i]), 3),
            'pdiv': round(float(res['pdiv_c'].iloc[i]), 3),
            'dif': round(float(sig['dif'].iloc[si]), 4),
            'dea': round(float(sig['dea'].iloc[si]), 4),
            'macdval': round(float(sig['macdval'].iloc[si]), 4),
            'leg_ret': round(nav_i / prev_nav - 1, 4),   # 距上次换仓的区间收益（含费用）
            'cum_ret': round(nav_i - 1, 4),              # 策略累计收益
        })
        prev_nav = nav_i

    out = {
        'dates': dates,
        'avg': series(avg),
        'etf1000': series(etf1000),
        'etfdiv': series(etfdiv),
        'dif': [round(float(v), 4) for v in sig['dif'].values],
        'dea': [round(float(v), 4) for v in sig['dea'].values],
        'trades': trades,
        'hold_spans': spans,
        'nav_strat': [round(float(v), 4) for v in res['nav_strat'].values],
        'updated': pd.Timestamp.now().strftime('%Y-%m-%d %H:%M'),
    }
    path = os.path.join(SITE_DIR, 'data.json')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False)
    print('已导出 %s  (%d 个交易日, %d 个换仓点, %d 段持仓区间)'
          % (path, len(dates), len(trades), len(spans)))


if __name__ == '__main__':
    main()
