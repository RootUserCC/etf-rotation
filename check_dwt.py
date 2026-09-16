#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""做T叠加回测：底仓持有 + 1/3仓位做T vs 纯持有（1分钟线，近4个月）
反T: 偏离VWAP≥+0.5%卖出, 跌回-0.2%接回 / 涨破+0.5%止损接回 / 尾盘接回
正T: 偏离VWAP≤-0.5%买入, 涨回+0.2%卖出 / 跌破-0.5%止损卖出 / 尾盘卖出
趋势过滤: 10:00已涨超1.5%的日子不做反T"""
import sys
import pandas as pd
import numpy as np
from pytdx.hq import TdxHq_API

if sys.platform == 'win32' and sys.stdout is not None:
    sys.stdout.reconfigure(encoding='utf-8')

api = TdxHq_API()
for ip, port in [('180.153.18.170', 7709), ('218.6.170.47', 7709)]:
    try:
        if api.connect(ip, port, time_out=5) and api.get_security_bars(8, 1, '512100', 0, 1):
            break
        api.disconnect()
    except Exception:
        continue

BACK, STOP, TH, W = 0.002, 0.005, 0.005, 1 / 3

def fetch(code, mkt):
    rows, start = [], 0
    while True:
        b = api.get_security_bars(8, mkt, code, start, 800)
        if not b:
            break
        rows = b + rows
        if len(b) < 800:
            break
        start += len(b)
    df = pd.DataFrame(rows)
    df['dt'] = pd.to_datetime(df['datetime'])
    df['date'] = df['dt'].dt.date
    df['hm'] = df['dt'].dt.strftime('%H:%M')
    df['typ'] = (df['high'] + df['low'] + df['close']) / 3
    df['pv'] = df['typ'] * df['vol']
    g = df.groupby('date')
    df['vwap'] = g['pv'].cumsum() / g['vol'].cumsum()
    df['dev'] = df['close'] / df['vwap'] - 1
    return df

def sim_day(day, prev_close, side, trend_filter):
    """返回当天做T盈亏(占做T仓位比例)"""
    day = day.reset_index(drop=True)
    if len(day) < 60:
        return 0.0
    if trend_filter and side == 'sell':
        p10 = day.loc[day['hm'] <= '10:00', 'close']
        if len(p10) and p10.iloc[-1] > prev_close * 1.015:
            return 0.0
    dev = day['dev'].values
    if side == 'sell':
        t = np.where(dev >= TH)[0]
    else:
        t = np.where(dev <= -TH)[0]
    if len(t) == 0 or t[0] >= len(day) - 10:
        return 0.0
    i = t[0]
    px0 = day['close'].iloc[i]
    after = day['close'].iloc[i + 1:].values
    if side == 'sell':
        win = after <= px0 * (1 - BACK)
        stop = after >= px0 * (1 + STOP)
        sgn = 1
    else:
        win = after >= px0 * (1 + BACK)
        stop = after <= px0 * (1 - STOP)
        sgn = -1
    wi = np.where(win)[0]
    si = np.where(stop)[0]
    w = wi[0] if len(wi) else 10 ** 9
    s = si[0] if len(si) else 10 ** 9
    if w < s:
        px1 = after[w]
    elif s < w:
        px1 = after[s]
    else:
        px1 = after[-1]
    return sgn * (px1 - px0) / px0

for code, mkt, name in [('512100', 1, '中证1000'), ('159552', 0, '中证2000增强'),
                        ('510300', 1, '沪深300'), ('512890', 1, '红利低波')]:
    df = fetch(code, mkt)
    days = sorted(df['date'].unique())
    closes = df.groupby('date')['close'].last()
    hold_ret = closes.pct_change().fillna(0).values
    prev = closes.shift(1)
    res = {k: [] for k in ['sell', 'buy', 'sell_tf']}
    for d in days:
        day = df[df['date'] == d]
        pc = prev.loc[d]
        if pd.isna(pc):
            for k in res:
                res[k].append(0.0)
            continue
        res['sell'].append(sim_day(day, pc, 'sell', False))
        res['buy'].append(sim_day(day, pc, 'buy', False))
        res['sell_tf'].append(sim_day(day, pc, 'sell', True))

    n = len(days)
    hold_total = np.prod(1 + hold_ret) - 1
    print('\n=== %s %s (%d天) 纯持有 %+.1f%% ===' % (code, name, n, hold_total * 100))
    variants = [('反T(高卖)', 'sell', None), ('正T(低买)', 'buy', None),
                ('正T+反T', None, None), ('反T+趋势过滤', 'sell_tf', None)]
    for label, key, _ in variants:
        if label == '正T+反T':
            t = (np.array(res['sell']) + np.array(res['buy'])) * W
        else:
            t = np.array(res[key]) * W
        tot = np.prod(1 + hold_ret + t) - 1
        trig = np.count_nonzero(res[key] if key else np.array(res['sell']) + np.array(res['buy']))
        print('  +%-12s 总收益 %+.1f%% (超额 %+.2f%%)  触发%d天' % (label, tot * 100, (tot - hold_total) * 100, trig))

api.disconnect()
