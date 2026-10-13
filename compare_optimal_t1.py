#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""上帝视角 2026 · T+1 开盘成交口径：T日收盘决策(预知未来) → T+1开盘换仓。
换仓日 = 旧腿隔夜段(昨收→今开) + 新腿日内段(今开→今收)，扣双边万2。
对比 close-to-close 口径(optimal_path.py)看执行延迟的理论成本。"""
import sys, io
import numpy as np
import pandas as pd

if sys.platform == 'win32' and not getattr(sys.stdout, '_utf8_wrapped', False):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stdout._utf8_wrapped = True

from compare_improve import load, FEE, max_drawdown

COST = 2 * FEE
atk = load('etf_159552_hfq.csv')
div = load('etf_512890_hfq.csv')
idx = atk.index.intersection(div.index)
idx = idx[idx >= pd.Timestamp('2026-01-01')]

o = {0: atk['open'].loc[idx].values, 1: div['open'].loc[idx].values}
c = {0: atk['close'].loc[idx].values, 1: div['close'].loc[idx].values}
n = len(idx)

# 每日因子: on=隔夜(昨收→今开), id=日内(今开→今收), cc=close-to-close
on = {s: np.ones(n) for s in (0, 1)}
idy = {s: np.ones(n) for s in (0, 1)}
cc = {s: np.ones(n) for s in (0, 1)}
for s in (0, 1):
    on[s][1:] = o[s][1:] / c[s][:-1]
    idy[s] = c[s] / o[s]
    cc[s][1:] = c[s][1:] / c[s][:-1]

# DP: V[t][s] = 第t天收盘持有腿s的最大净值
V = np.ones((n, 2))
pre = np.zeros((n, 2), dtype=int)
for t in range(1, n):
    for s in range(2):
        stay = V[t - 1][s] * cc[s][t]
        sw = V[t - 1][1 - s] * on[1 - s][t] * idy[s][t] * (1 - COST)
        if stay >= sw:
            V[t][s] = stay; pre[t][s] = s
        else:
            V[t][s] = sw; pre[t][s] = 1 - s

path = np.zeros(n, dtype=int)
path[-1] = 0 if V[-1][0] >= V[-1][1] else 1
for t in range(n - 1, 0, -1):
    path[t - 1] = pre[t][path[t]]

nav = np.ones(n)
for t in range(1, n):
    if path[t] == path[t - 1]:
        nav[t] = nav[t - 1] * cc[path[t]][t]
    else:
        nav[t] = nav[t - 1] * on[path[t - 1]][t] * idy[path[t]][t] * (1 - COST)
nav = pd.Series(nav, index=idx)

n_sw = int((np.diff(path) != 0).sum())
years = (n - 1) / 244
cum = nav.iloc[-1] - 1
print('上帝视角 2026 · T+1开盘成交口径（%s ~ %s，%d 个交易日）' % (idx[0].date(), idx[-1].date(), n))
print('=' * 74)
print('累计 %+8.1f%%   最大回撤 %5.1f%%   换仓 %d 次   进攻占比 %.0f%%' % (
    cum * 100, max_drawdown(nav) * 100, n_sw, (path == 0).mean() * 100))
print('对比: close-to-close口径 +357.8%% / 方案C系统实际 +27.3%%')
print('T+1执行延迟的理论成本: %.1f 个百分点' % ((3.578 - cum) * 100))

print('\n逐笔明细（T日决策 → T+1开盘成交，后复权价；离开=换仓日开盘价卖出）:')
print('%-4s %-6s %-10s %8s   %-10s %8s   %4s %8s' % ('#', '标的', '买入日', '买入价(开)', '卖出日', '卖出价(开)', '天数', '段收益'))
seg_start = 0
k = 0
for i in range(1, n + 1):
    if i == n or path[i] != path[seg_start]:
        k += 1
        s = path[seg_start]
        # 段净值因子：进入日吃 idy（开盘买入），中间日 cc，离开日如果是换出则只吃到隔夜 on
        f = 1.0
        for t in range(seg_start, i):
            if t == seg_start and seg_start > 0:
                f *= idy[s][t]              # 进入日: 开盘买入吃日内
            else:
                f *= cc[s][t]
        # 若非最后一段，最后一天(i-1)之后 i 日开盘换出: i 日吃旧腿隔夜
        if i < n:
            f *= on[s][i]
        if seg_start > 0:
            f *= (1 - COST)                 # 进入时的换仓成本
        seg_ret = f - 1
        buy_px = o[s][seg_start]
        sell_px = o[s][i] if i < n else c[s][n - 1]
        print('%-4d %-6s %-10s %8.3f   %-10s %8.3f   %4d %+7.2f%%' % (
            k, '进攻' if s == 0 else '防守', idx[seg_start].date(), buy_px,
            idx[i].date() if i < n else idx[n - 1].date(), sell_px,
            i - seg_start, seg_ret * 100))
        seg_start = i
print('\n合计 %d 笔' % k)
