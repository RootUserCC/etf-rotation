#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 ai_yellow_bar.py 回测的最优组合逐笔交易（买入/卖出日期）导出成 txt。

数据源：data/ai_stocks/ 本地缓存日线；最优参数取自 site/ai_backtest.json。
输出：回测交易记录.txt（项目根目录）
"""
import json
import os

import pandas as pd

import ai_yellow_bar as ay

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, '回测交易记录.txt')


def collect_trades(run_key, n_bar, filt, srule):
    """逐股票跑最优组合，返回 {code: [(买日, 卖日, 收益率, 持有天数), ...]}"""
    result = {}
    for code, name, _note in ay.load_stock_list():
        path = os.path.join(ay.CACHE, '%s.csv' % code)
        if not os.path.exists(path):
            continue
        df = pd.read_csv(path, parse_dates=['date'], index_col='date')
        p = ay.prepare(df)
        trades = ay.simulate(p, n_bar, filt, srule, ay.SIGNAL_START, run_key)
        result[code] = (name, [(b.date(), s.date(), r, (s - b).days) for b, s, r in trades])
    return result


def fmt_section(f, title, cond, trades_by_stock):
    total = sum(len(v[1]) for v in trades_by_stock.values())
    rets = [t[2] for v in trades_by_stock.values() for t in v[1]]
    wins = sum(1 for r in rets if r > 0)
    f.write('%s\n' % title)
    f.write('条件：%s | 共 %d 笔，胜率 %.1f%%，平均盈亏 %+.2f%%\n'
            % (cond, total, wins / total * 100 if total else 0,
               sum(rets) / total * 100 if total else 0))
    f.write('-' * 60 + '\n')
    for code, (name, trades) in trades_by_stock.items():
        if not trades:
            continue
        f.write('\n%s %s（%d 笔）\n' % (name, code, len(trades)))
        for k, (b, s, r, days) in enumerate(trades, 1):
            f.write('  %d. 买 %s → 卖 %s  持有%3d天  %+.2f%%\n'
                    % (k, b, s, days, r * 100))
    f.write('\n' + '=' * 60 + '\n\n')


def main():
    bt = json.load(open(os.path.join(ROOT, 'site', 'ai_backtest.json'), encoding='utf-8'))
    names = {c: n for c, n, _ in ay.load_stock_list()}

    yb, rb = bt['best'], bt['red']['best']
    y_trades = collect_trades('run', yb['n'], ay.BUY_FILTERS[yb['filter']], ay.SELL_RULES[yb['sell']])
    r_trades = collect_trades('red_run', rb['n'], ay.BUY_FILTERS[rb['filter']], ay.RED_SELL_RULES[rb['sell']])

    with open(OUT, 'w', encoding='utf-8') as f:
        f.write('回测交易记录（%s ~ %s，股票池 %d 只，未计手续费滑点，仅供研究参考）\n'
                % (bt['since'], bt['updated'], bt['pool']))
        f.write('=' * 60 + '\n\n')
        fmt_section(f, '【黄柱最优】第 %d 根黄柱买入（%s）→ %s卖出'
                    % (yb['n'], yb['filter'], yb['sell']),
                    '黄柱=%s | 卖出=%s' % (yb['filter'], yb['sell']), y_trades)
        fmt_section(f, '【红柱最优】第 %d 根红柱买入（%s）→ %s卖出'
                    % (rb['n'], rb['filter'], rb['sell']),
                    '红柱=%s | 卖出=%s' % (rb['filter'], rb['sell']), r_trades)
    print('已导出 %s' % OUT)


if __name__ == '__main__':
    main()
