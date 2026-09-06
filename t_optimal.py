#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
单腿做T的后见之明最优解（动态规划，T+1约束，5分钟颗粒度）

问题：全程只玩一只ETF（默认159552），期初全仓底仓（全部可卖），
每根5分钟K线收盘可选：持币/持股/卖出可卖部分/用现金买入。
T+1：当日买入份额锁定，次日才可卖。全有或全无（不拆仓），双边各万1。

状态：S=持股且可卖 / L=持股但当日买入锁定 / C=持币
倒推DP求终值最大化，再正向重建最优交易序列。
"""
import sys
import io
import os

import numpy as np
import pandas as pd

# pythonw（无窗口计划任务）下 sys.stdout 为 None，不能 reconfigure
if sys.platform == 'win32' and sys.stdout is not None and not getattr(sys.stdout, '_utf8_wrapped', False):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stdout._utf8_wrapped = True

from t_pattern_long import DATA_DIR
from t_pattern_t1 import load_5min

FEE = 0.0001          # 单边万1
DAYS = 10


def optimal_t(close: pd.Series, fee=FEE):
    px = close.values
    n = len(px)
    days = pd.Series(close.index.date, index=close.index).values
    # 下一根是否新交易日（L→S 解锁）
    next_newday = np.zeros(n, dtype=bool)
    next_newday[:-1] = days[1:] != days[:-1]

    S, L, C = 0, 1, 2
    f = np.ones((n, 3))          # 每元价值的终值倍数
    choice = np.zeros((n, 3), dtype=np.int8)   # 0=不动 1=卖 2=买

    for i in range(n - 2, -1, -1):
        ratio = px[i + 1] / px[i]
        unlock = next_newday[i]
        fL_next = f[i + 1, S] if unlock else f[i + 1, L]
        # C：不动 / 买入(变L，次日解锁)
        buy = (1 - fee) * ratio * fL_next
        stay_c = f[i + 1, C]
        f[i, C] = max(stay_c, buy)
        choice[i, C] = 2 if buy > stay_c else 0
        # S：不动 / 卖出(变C)
        stay_s = ratio * f[i + 1, S]
        sell = (1 - fee) * f[i + 1, C]
        f[i, S] = max(stay_s, sell)
        choice[i, S] = 1 if sell > stay_s else 0
        # L：不能卖，只能持
        f[i, L] = ratio * fL_next

    # 正向重建
    trades = []
    state = S
    i = 0
    while i < n - 1:
        c = choice[i, state]
        if state == S and c == 1:
            trades.append((close.index[i], '卖出', px[i]))
            state = C
        elif state == C and c == 2:
            trades.append((close.index[i], '买入', px[i]))
            state = L
            # 买入后当根即锁定；往后遇到新交易日解锁
        if state == L and next_newday[i]:
            state = S
        i += 1
    return f[0, S] - 1, trades


def main():
    for code, name in [('159552', '2000增强'), ('512890', '红利低波')]:
        m = load_5min(code)
        close = m['close']
        day_list = sorted(set(close.index.date))[-DAYS:]
        close = close[close.index.date >= day_list[0]]
        hold = close.iloc[-1] / close.iloc[0] - 1
        ret, trades = optimal_t(close)
        print('=' * 60)
        print('%s (%s)  %s ~ %s' % (name, code, day_list[0], day_list[-1]))
        print('持有不动: %+.2f%%   T+1最优做T: %+.2f%%   增强: %+.2fpp'
              % (hold * 100, ret * 100, (ret - hold) * 100))
        print('最优操作序列（共 %d 笔）:' % len(trades))
        cur = None
        for t, act, p in trades:
            d = t.date()
            if d != cur:
                print('  -- %s --' % d)
                cur = d
            print('    %s %s @ %.3f' % (t.strftime('%H:%M'), act, p))
        print()


if __name__ == '__main__':
    main()
