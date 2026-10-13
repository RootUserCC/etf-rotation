#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""四色柱状态跟随 · 信号日当天收盘成交（尾盘口径）· 2026 逐笔 vs 上帝视角同窗对比
规则: 进攻腿自身(512100→159552拼接)MACD(12,26,9)四色柱, 红/黄=进攻, 粉/绿=防守。
执行: T日收盘定色即按T日收盘价成交（双边万2）。
上帝视角: close-to-close DP 最优路径(optimal_path.optimal_path)，同2026窗口。
"""
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
from compare_bar4color import bar4_signals
from optimal_path import optimal_path

atk1000 = load('etf_512100_hfq.csv')
atk2000 = load('etf_159552_hfq.csv')
div = load('etf_512890_hfq.csv')
atk_c = pd.concat([atk1000['close'][atk1000.index < SWITCH_ATK],
                   atk2000['close'][atk2000.index >= SWITCH_ATK]])
div_c = div['close']

idx = atk_c.index.intersection(div_c.index)
idx = idx[idx >= START]
a = atk_c.loc[idx]
d = div_c.loc[idx]
ra = a.pct_change().fillna(0)
rd = d.pct_change().fillna(0)

# 四色柱信号（全历史预热）
_, state_full = bar4_signals(atk_c)
state_full = state_full.reindex(idx).fillna(False)  # True=进攻

# ---- 尾盘成交回测：收盘定色即按收盘价换仓 ----
n = len(idx)
nav = np.ones(n)
st = np.zeros(n, dtype=bool)   # 收盘状态: True=进攻
trades = []                    # (日期, 新状态, 价格)
cur = False
for i in range(n):
    tgt = bool(state_full.iloc[i])
    if i > 0:
        nav[i] = nav[i - 1] * (1 + (ra.iloc[i] if cur else rd.iloc[i]))
    if tgt != cur:
        nav[i] *= (1 - 2 * FEE)
        cur = tgt
        trades.append((idx[i], cur, float((a if cur else d).iloc[i])))
    st[i] = cur
nav = pd.Series(nav, index=idx)

years = (idx[-1] - idx[0]).days / 365.0
nav26 = nav.loc['2026-01-01':]
ytd26 = nav26.iloc[-1] / nav.loc[:'2025-12-31'].iloc[-1] - 1
print('四色柱@进攻腿自身 · 尾盘(收盘)成交  全区间: 累计 %+.1f%%  年化 %+.2f%%  回撤 %.1f%%  换仓 %d'
      % ((nav.iloc[-1] - 1) * 100, (nav.iloc[-1] ** (1 / years) - 1) * 100,
         max_drawdown(nav) * 100, len(trades)))
print('2026YTD: %+.2f%%（T+1开盘口径同规则为 +17.45%%，方案C基准 +32.77%%）' % (ytd26 * 100))

# ---- 上帝视角 close-to-close（2026窗口）----
idx26 = idx[idx >= pd.Timestamp('2026-01-01')]
r_a26 = a.loc[idx26].pct_change().fillna(0)
r_d26 = d.loc[idx26].pct_change().fillna(0)
gnav, gpath = optimal_path(r_a26, r_d26, idx26, 2 * FEE)

# ---- 2026 逐笔 vs 上帝视角 ----
print('\n2026 逐笔 vs 上帝视角（进入/离开=收盘成交；上帝收益=同期最优路径净值变化）:')
print('%-4s %-4s %-10s %8s %-10s %8s %4s %8s   %-10s %8s' % (
    '#', '腿', '进入日', '买入价', '离开日', '卖出价', '天数', '本段', '上帝持仓', '上帝同期'))
tr26 = [t for t in trades if t[0] >= pd.Timestamp('2026-01-01')]
bounds = [t[0] for t in tr26] + [idx26[-1]]
k = 0
for i, (t0, newstate, px) in enumerate(tr26):
    k += 1
    t1 = bounds[i + 1]
    seg = nav.loc[t0:t1]
    seg_ret = seg.iloc[-1] / seg.iloc[0] - 1
    px_out = float((a if newstate else d).loc[t1])
    days = len(seg)
    g_seg = gnav.loc[t0:t1]
    g_ret = g_seg.iloc[-1] / g_seg.iloc[0] - 1 if len(g_seg) > 1 else 0.0
    g_hold = gpath.loc[t0:t1].mode().iloc[0]
    print('%-4d %-4s %-10s %8.3f %-10s %8.3f %4d %+7.2f%%   %-10s %+7.2f%%' % (
        k, '进攻' if newstate else '防守', t0.date(), px, t1.date(), px_out,
        days, seg_ret * 100, '进攻' if g_hold == 0 else '防守', g_ret * 100))
print('\n上帝视角2026(close-to-close): %+.1f%%   本规则(尾盘): %+.1f%%   方案C基准(T+1): +32.8%%'
      % ((gnav.iloc[-1] - 1) * 100, ytd26 * 100))
