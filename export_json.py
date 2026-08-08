#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
导出网站数据：平均股价(880003) + 512100/512890（后复权）+ 方案C信号/换仓点（MACD 17/34/9）
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
    etfdiv = load('etf_512890_hfq.csv')

    # 方案C全历史信号与换仓（MACD 17/34/9 慢速拐点；回测窗口=512890上市起(2019-01-18)，保证净值/持仓区间完整）
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
    odiv = etfdiv.loc[idx, 'open']
    o1000 = etf1000.loc[idx, 'open']
    cdiv = etfdiv.loc[idx, 'close']
    c1000 = etf1000.loc[idx, 'close']
    # 真实成交口径账户净值：普通日按持仓收盘→收盘；换仓日拆为
    # 旧仓隔夜段(昨收→今开) + 新仓日内段(今开→今收)，扣双边万1
    switch_at = {pos_of[d]: a for d, a, p in res['trades']}
    wealth = [1.0]
    held_div = True
    for i in range(1, len(idx)):
        a = switch_at.get(i)
        if a is None:
            c = cdiv if held_div else c1000
            w = wealth[-1] * float(c.iloc[i]) / float(c.iloc[i - 1])
        elif a.startswith('买入'):
            w = (wealth[-1] * float(odiv.iloc[i]) / float(cdiv.iloc[i - 1])
                 * float(c1000.iloc[i]) / float(o1000.iloc[i]) * (1 - 2 * 0.0001))
            held_div = False
        else:
            w = (wealth[-1] * float(o1000.iloc[i]) / float(c1000.iloc[i - 1])
                 * float(cdiv.iloc[i]) / float(odiv.iloc[i]) * (1 - 2 * 0.0001))
            held_div = True
        wealth.append(w)
    wealth = pd.Series(wealth, index=idx)
    # 区间收益按实盘口径：上次换仓开盘价建仓 → 本次换仓开盘价了结，扣双边万1
    # 累计收益取换仓成交时刻（当日开盘）的账户价值 = 区间收益逐笔连乘，两行严格对账
    entry = float(etfdiv.loc[idx[0], 'close'])   # 初始持有红利低波，视作回测首日收盘建仓
    wopen = 1.0
    trades = []
    for d, a, p in res['trades']:
        i = pos_of[d]
        si = max(i - 1, 0)                    # 信号日 = 成交日前一交易日
        # 本次操作卖出（了结）的标的：'买入1000ETF/卖出红利低波'→红利低波；反之→1000ETF
        closed = odiv if a.startswith('买入') else o1000
        leg_ret = float(closed.iloc[i]) / entry - 1 - 2 * 0.0001
        wopen *= (1 + leg_ret)
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
            'leg_ret': round(leg_ret, 4),            # 本段持仓收益（开盘价口径，含双边费用）
            'cum_ret': round(wopen - 1, 4),          # 累计收益（换仓时刻，=区间收益连乘）
        })
        entry = float(p)                          # 新仓位以当日开盘价建仓

    # 持仓明细：每行一段完整持仓（买入→卖出），最后一行为当前持有中（浮动盈亏按最新收盘估）
    legs = []
    leg_date = idx[0]
    leg_price = float(etfdiv.loc[idx[0], 'close'])   # 首段视作回测首日收盘建仓红利低波
    leg_asset = 'div'
    prev_i = 0
    wcum = 1.0
    for d, a, p in res['trades']:
        i = pos_of[d]
        closed_o = odiv if leg_asset == 'div' else o1000
        sell_price = float(closed_o.iloc[i])
        ret = sell_price / leg_price - 1 - 2 * 0.0001
        wcum *= (1 + ret)
        legs.append({
            'asset': leg_asset,
            'buy_date': leg_date.strftime('%Y-%m-%d'),
            'buy_price': round(leg_price, 3),
            'sell_date': d.strftime('%Y-%m-%d'),
            'sell_price': round(sell_price, 3),
            'days': i - prev_i,
            'ret': round(ret, 4),
            'cum': round(wcum - 1, 4),
            'open': False,
        })
        leg_asset = '1000' if a.startswith('买入') else 'div'
        leg_date = d
        leg_price = float(p)
        prev_i = i
    last_close = float((cdiv if leg_asset == 'div' else c1000).iloc[-1])
    ret_open = last_close / leg_price - 1 - 2 * 0.0001
    legs.append({
        'asset': leg_asset,
        'buy_date': leg_date.strftime('%Y-%m-%d'),
        'buy_price': round(leg_price, 3),
        'sell_date': None,
        'sell_price': round(last_close, 3),
        'days': len(idx) - 1 - prev_i,
        'ret': round(ret_open, 4),
        'cum': round(wcum * (1 + ret_open) - 1, 4),
        'open': True,
    })

    out = {
        'dates': dates,
        'avg': series(avg),
        'etf1000': series(etf1000),
        'etfdiv': series(etfdiv),
        'dif': [round(float(v), 4) for v in sig['dif'].values],
        'dea': [round(float(v), 4) for v in sig['dea'].values],
        'trades': trades,
        'legs': legs,
        'hold_spans': spans,
        'nav_strat': [round(float(v), 4) for v in wealth.values],   # 真实成交口径净值
        'updated': pd.Timestamp.now().strftime('%Y-%m-%d %H:%M'),
    }
    path = os.path.join(SITE_DIR, 'data.json')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False)
    print('已导出 %s  (%d 个交易日, %d 个换仓点, %d 段持仓区间)'
          % (path, len(dates), len(trades), len(spans)))


if __name__ == '__main__':
    main()
