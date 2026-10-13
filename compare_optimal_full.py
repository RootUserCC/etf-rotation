#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""上帝视角全区间（2019-01-18 起）：进攻腿=512100→159552拼接，防守=512890，DP 最优路径"""
import sys, io
import numpy as np
import pandas as pd

if sys.platform == 'win32' and not getattr(sys.stdout, '_utf8_wrapped', False):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stdout._utf8_wrapped = True

from compare_improve import load, SWITCH_ATK, START, FEE, annualized, max_drawdown
from optimal_path import optimal_path

COST = 2 * FEE

atk1000 = load('etf_512100_hfq.csv')
atk2000 = load('etf_159552_hfq.csv')
div = load('etf_512890_hfq.csv')

atk_close = pd.concat([atk1000['close'][atk1000.index < SWITCH_ATK],
                       atk2000['close'][atk2000.index >= SWITCH_ATK]])
idx = atk_close.index.intersection(div.index)
idx = idx[idx >= START]

r_a = atk_close.loc[idx].pct_change().fillna(0)
r_d = div['close'].loc[idx].pct_change().fillna(0)

nav_opt, path = optimal_path(r_a, r_d, idx, COST)
nav_hold_a = (1 + r_a).cumprod()
nav_hold_d = (1 + r_d).cumprod()

years = (len(idx) - 1) / 244
print('上帝视角最优路径 · 全区间（%s ~ %s，%d 个交易日，%.2f 年）'
      % (idx[0].date(), idx[-1].date(), len(idx), years))
print('=' * 70)
for name, nv in [('上帝视角最优路径', nav_opt),
                 ('进攻腿买入持有(512100→159552)', nav_hold_a),
                 ('512890 买入持有', nav_hold_d)]:
    cum = nv.iloc[-1] - 1
    print('%-26s 累计 %+12.1f%%  年化 %+8.1f%%  最大回撤 %6.1f%%'
          % (name, cum * 100, ((1 + cum) ** (1 / years) - 1) * 100, max_drawdown(nv) * 100))
print('最终净值倍数: %.1f 倍（1万 → %.0f 万）' % (nav_opt.iloc[-1], nav_opt.iloc[-1]))
print('换仓次数: %d   持仓占比: 进攻 %.0f%% / 防守 %.0f%%'
      % (int((path.diff() != 0).sum()), (path.values == 0).mean() * 100,
         (path.values == 1).mean() * 100))

# 分年度收益
print('\n分年度收益:')
for y, g in nav_opt.groupby(nav_opt.index.year):
    seg = g / g.iloc[0]
    prev_note = ''
    print('  %d: %+10.1f%%' % (y, (seg.iloc[-1] - 1) * 100))

# 高成本敏感性
nav_exp, path_exp = optimal_path(r_a, r_d, idx, 0.001)
print('\n若每次换仓成本千1: 累计 %+12.1f%%, 换仓 %d 次'
      % ((nav_exp.iloc[-1] - 1) * 100, int((path_exp.diff() != 0).sum())))
