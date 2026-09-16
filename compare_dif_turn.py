#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对比：通达信 DIF 拐点策略(方案C:零下买/任意卖) vs web端黄柱计数策略

口径与 ai_yellow_bar.py 一致：同一股票池、2026-01-01 以来的信号、
信号当日收盘成交、单笔持仓卖出后才能再开仓、数据用 data/ai_stocks/ 缓存。
"""
import os

import pandas as pd

import ai_yellow_bar as ay


def prepare_dif(df: pd.DataFrame, fast: int, slow: int, sig: int):
    """DIF/DEA + 拐点标记"""
    close = df['close']
    dif = close.ewm(span=fast, adjust=False).mean() - close.ewm(span=slow, adjust=False).mean()
    dea = dif.ewm(span=sig, adjust=False).mean()
    d1, d2 = dif.shift(1), dif.shift(2)
    turn_up = ((d1 < d2) & (dif > d1)).fillna(False).values   # DIF 转升
    turn_dn = ((d1 > d2) & (dif < d1)).fillna(False).values   # DIF 转降
    return {'idx': close.index, 'closes': close.values,
            'dif': dif.values, 'turn_up': turn_up, 'turn_dn': turn_dn}


def simulate_dif(p, start):
    """方案C：DIF<0 且 DIF 转升 → 当日收盘买；DIF 转降 → 当日收盘卖"""
    idx, closes = p['idx'], p['closes']
    n = len(closes)
    trades = []
    i = 60  # 预热
    while i < n:
        if p['turn_up'][i] and p['dif'][i] < 0 and idx[i] >= start:
            entry = closes[i]
            j = i + 1
            exit_j = n - 1
            while j < n:
                if p['turn_dn'][j]:
                    exit_j = j
                    break
                j += 1
            trades.append((idx[i], idx[exit_j], closes[exit_j] / entry - 1))
            i = exit_j + 1
        else:
            i += 1
    return trades


def main():
    stocks = ay.load_stock_list()
    data = {}
    for code, name, _ in stocks:
        path = os.path.join(ay.CACHE, '%s.csv' % code)
        if os.path.exists(path):
            data[code] = pd.read_csv(path, parse_dates=['date'], index_col='date')
    print('股票池 %d 只（缓存数据），信号起始 %s\n' % (len(data), ay.SIGNAL_START.date()))

    # ---- DIF 拐点策略：两组参数 ----
    for tag, (f, s, g) in [('DIF拐点(17,34,9) 通达信公式', (17, 34, 9)),
                           ('DIF拐点(12,26,9) 标准参数', (12, 26, 9))]:
        all_trades = []
        for code, df in data.items():
            all_trades += simulate_dif(prepare_dif(df, f, s, g), ay.SIGNAL_START)
        c, w, m, pl, pf = ay.stats(all_trades)
        print('%-26s 交易 %3d 笔  胜率 %5.1f%%  平均盈亏 %+6.2f%%  盈亏比 %5.2f  收益因子 %5.2f'
              % (tag, c, w * 100, m * 100, pl, pf))

    # ---- web 端黄柱策略：最优(第4根+首根蓝柱) 与基准(第2根+首根蓝柱) ----
    prep = {code: ay.prepare(df) for code, df in data.items()}
    for tag, n_bar in [('黄柱第4根+首根蓝柱 web最优', 4), ('黄柱第2根+首根蓝柱 web基准', 2)]:
        all_trades = []
        for code, p in prep.items():
            all_trades += ay.simulate(p, n_bar, ay.BUY_FILTERS['无过滤'],
                                      ay.SELL_RULES['首根蓝柱'], ay.SIGNAL_START)
        c, w, m, pl, pf = ay.stats(all_trades)
        print('%-26s 交易 %3d 笔  胜率 %5.1f%%  平均盈亏 %+6.2f%%  盈亏比 %5.2f  收益因子 %5.2f'
              % (tag, c, w * 100, m * 100, pl, pf))

    # ---- DIF 拐点策略逐股票明细（17,34,9） ----
    print('\n===== DIF拐点(17,34,9) 逐股票明细 =====')
    names = {code: name for code, name, _ in stocks}
    print('%-8s %-6s %6s %8s %9s' % ('代码', '名称', '交易数', '胜率', '平均盈亏'))
    for code, df in data.items():
        tr = simulate_dif(prepare_dif(df, 17, 34, 9), ay.SIGNAL_START)
        c, w, m, pl, pf = ay.stats(tr)
        print('%-8s %-6s %6d %7.1f%% %8.2f%%' % (code, names.get(code, ''), c, w * 100, m * 100))


if __name__ == '__main__':
    main()
