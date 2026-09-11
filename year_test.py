#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""分年度验证黄柱策略的趋势依赖性：同一股票池、同一规则（第4根黄柱买+首根蓝柱卖），
按买入日年份统计。趋势好的年份应显著优于熊市年份。
注意：当前池子是 2026 年选出的强势板块，历史年份存在幸存者/选择偏差，结果偏乐观。
"""
import os

import pandas as pd

import ai_yellow_bar as ay


def main():
    prep = {}
    for code, name, _ in ay.load_stock_list():
        path = os.path.join(ay.CACHE, '%s.csv' % code)
        if not os.path.exists(path):
            continue
        df = pd.read_csv(path, parse_dates=['date'], index_col='date')
        prep[code] = ay.prepare(df)

    blue = ay.SELL_RULES['首根蓝柱']
    none_f = ay.BUY_FILTERS['无过滤']

    # 每年全池所有交易
    print('%-6s %6s %8s %9s %8s %8s' % ('年份', '交易数', '胜率', '平均盈亏', '盈亏比', '收益因子'))
    for year in range(2019, 2027):
        tr = []
        for p in prep.values():
            for b, s, r in ay.simulate(p, 4, none_f, blue, pd.Timestamp('%d-01-01' % year)):
                if b < pd.Timestamp('%d-01-01' % (year + 1)):
                    tr.append((b, s, r))
        c, w, m, pl, pf = ay.stats(tr)
        print('%-6d %6d %7.1f%% %8.2f%% %8.2f %8.2f' % (year, c, w * 100, m * 100, pl, pf))

    # 沪深300 各年涨跌作趋势参照
    hs = pd.read_csv('data/etf_510300_hfq.csv', parse_dates=['date'], index_col='date')['close']
    print('\n沪深300ETF 各年涨跌：')
    for year in range(2019, 2027):
        s = hs.loc['%d-01-01' % year:'%d-12-31' % year]
        if len(s):
            print('  %d: %+.1f%%' % (year, (s.iloc[-1] / s.iloc[0] - 1) * 100))


if __name__ == '__main__':
    main()
