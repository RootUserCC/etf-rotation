#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""上帝视角 2026 年逐笔交易明细：进入/离开日 + 后复权价格 + 段收益"""
import sys, io
import pandas as pd

if sys.platform == 'win32' and not getattr(sys.stdout, '_utf8_wrapped', False):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stdout._utf8_wrapped = True

from compare_improve import load, FEE
from optimal_path import optimal_path

COST = 2 * FEE
atk = load('etf_159552_hfq.csv')   # 中证2000增强 后复权
div = load('etf_512890_hfq.csv')   # 红利低波 后复权
idx = atk.index.intersection(div.index)
idx = idx[idx >= pd.Timestamp('2026-01-01')]
r_a = atk['close'].loc[idx].pct_change().fillna(0)
r_d = div['close'].loc[idx].pct_change().fillna(0)
nav_opt, path = optimal_path(r_a, r_d, idx, COST)

p_a = atk['close'].loc[idx]   # 后复权收盘价
p_d = div['close'].loc[idx]

print('上帝视角 2026 逐笔交易（后复权收盘价口径：进入=前一交易日收盘买入，离开=当日收盘卖出，换仓扣万2）')
print('%-4s %-6s %-10s %8s   %-10s %8s   %4s %8s' % ('#', '标的', '进入日', '买入价', '离开日', '卖出价', '天数', '段收益'))
seg_start = 0
n = 0
for i in range(1, len(idx) + 1):
    if i == len(idx) or path.iloc[i] != path.iloc[seg_start]:
        n += 1
        leg = path.iloc[seg_start]
        r = r_a if leg == 0 else r_d
        p = p_a if leg == 0 else p_d
        seg_ret = float((1 + r.iloc[seg_start:i]).prod() - 1)
        if seg_start > 0:
            seg_ret -= COST
        buy_px = p.iloc[seg_start - 1] if seg_start > 0 else p.iloc[0]
        sell_px = p.iloc[i - 1]
        print('%-4d %-6s %-10s %8.3f   %-10s %8.3f   %4d %+7.2f%%' % (
            n, '进攻' if leg == 0 else '防守',
            idx[seg_start].date(), buy_px, idx[i - 1].date(), sell_px,
            i - seg_start, seg_ret * 100))
        seg_start = i
print('\n合计 %d 笔，累计 %+8.1f%%（上帝视角，不可实盘）' % (n, (nav_opt.iloc[-1] - 1) * 100))
