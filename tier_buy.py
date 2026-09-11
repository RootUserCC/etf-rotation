#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""黄柱分批建仓回测：第 2/3/4 根各买 1/3（先到先买，段断了就持有已买部分），
首根蓝柱一次性卖出。与「严格第4根一把买」「放宽第4根」同口径对比。
数据：data/ai_stocks/ 本地缓存。
"""
import os

import pandas as pd

import ai_yellow_bar as ay
from compare_ymix import mixed_run


def simulate_tier(p, tiers, sell, start):
    """分批买入：run 到达 tiers 中下一档时加一份仓；卖出规则同 ay.simulate"""
    idx, closes, run = p['idx'], p['closes'], p['run']
    n = len(closes)
    trades = []
    i = 60
    while i < n:
        if run[i] == tiers[0] and idx[i] >= start:
            entries = [closes[i]]
            j = i + 1
            exit_j = n - 1
            while j < n:
                if len(entries) < len(tiers) and run[j] == tiers[len(entries)]:
                    entries.append(closes[j])
                c = closes[j]
                if sell['blue'] and p['blue'][j]:
                    exit_j = j
                    break
                if j - i >= sell['maxd']:
                    exit_j = j
                    break
                j += 1
            ret = sum(closes[exit_j] / e - 1 for e in entries) / len(entries)
            trades.append((idx[i], idx[exit_j], ret, len(entries)))
            i = exit_j + 1
        else:
            i += 1
    return trades


def main():
    prep_strict, prep_mixed = {}, {}
    for code, name, _ in ay.load_stock_list():
        path = os.path.join(ay.CACHE, '%s.csv' % code)
        if not os.path.exists(path):
            continue
        df = pd.read_csv(path, parse_dates=['date'], index_col='date')
        p = ay.prepare(df)
        p2 = dict(p)
        p2['run'] = mixed_run(df)[1]
        prep_strict[code] = p
        prep_mixed[code] = p2

    blue = ay.SELL_RULES['首根蓝柱']
    print('%-26s %6s %8s %9s %8s %8s %8s' % ('方案', '交易数', '胜率', '平均盈亏', '盈亏比', '收益因子', '平均仓位'))

    def row(tag, trades):
        c, w, m, pl, pf = ay.stats([(b, s, r) for b, s, r, *_ in trades])
        avg_pos = sum(t[3] for t in trades) / len(trades) / 3 if trades else 0
        print('%-26s %6d %7.1f%% %8.2f%% %8.2f %8.2f %7.0f%%'
              % (tag, c, w * 100, m * 100, pl, pf, avg_pos * 100))
        return trades

    row('严格 第4根一把买', [(b, s, r, 3) for prep in (prep_strict,)
        for p in prep.values() for b, s, r in
        ay.simulate(p, 4, ay.BUY_FILTERS['无过滤'], blue, ay.SIGNAL_START)])
    row('严格 第3根一把买', [(b, s, r, 3) for prep in (prep_strict,)
        for p in prep.values() for b, s, r in
        ay.simulate(p, 3, ay.BUY_FILTERS['无过滤'], blue, ay.SIGNAL_START)])
    row('放宽 第4根一把买', [(b, s, r, 3) for prep in (prep_mixed,)
        for p in prep.values() for b, s, r in
        ay.simulate(p, 4, ay.BUY_FILTERS['无过滤'], blue, ay.SIGNAL_START)])

    t234 = row('分批 2/3/4根各1/3', [t for p in prep_strict.values()
               for t in simulate_tier(p, (2, 3, 4), blue, ay.SIGNAL_START)])
    row('分批 3/4根各1/2', [(b, s, r, k * 1.5) for p in prep_strict.values()
        for b, s, r, k in simulate_tier(p, (3, 4), blue, ay.SIGNAL_START)])

    # 分批 2/3/4 稳健性
    mid = pd.Timestamp('2026-06-01')
    for tag, tr in [('上半年', [t for t in t234 if t[0] < mid]), ('下半年', [t for t in t234 if t[0] >= mid])]:
        c, w, m, pl, pf = ay.stats([(b, s, r) for b, s, r, _ in tr])
        print('  分批2/3/4 %s：%d笔 %.1f%% %+.2f%% PF%.2f' % (tag, c, w * 100, m * 100, pf))


if __name__ == '__main__':
    main()
