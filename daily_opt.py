#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
日线轮动信号对比：MACD vs KDJ vs 参数寻优（512100/510880，最近5年）

口径与 run_backtest 一致：信号日收盘产生，T+1 开盘成交，双边万1。
窗口：2021-08 ~ 2026-08；样本内 2021-08~2024-07 选参数，
      样本外 2024-08~2026-08 验证。指标预热用全部历史。

信号定义：
  MACD：DIF 零下拐头买 / 零上拐头卖（现行口径，fast/slow 寻优，sig=9）
  KDJ ：CROSS(K,D) 且 K<k_buy 买；(CROSS(D,K) 且 K>k_sell) 卖，
        可选叠加跌破MA20卖出（N/k_buy/k_sell/ma20 寻优）
"""
import sys
import io
import itertools

import numpy as np
import pandas as pd

if sys.platform == 'win32' and not getattr(sys.stdout, '_utf8_wrapped', False):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stdout._utf8_wrapped = True

from backtest import run_backtest, calc_signals, annualized, max_drawdown
from compare_kdj import load, calc_kdj, cross_up, cross_down

FEE = 0.0001
W_START = '2021-08-01'     # 5年窗口起点
SPLIT = '2024-08-01'       # 样本内/外分界


def kdj_sig(avg, n=9, k_buy=30, k_sell=70, use_ma20=True):
    kdj = calc_kdj(avg, n=n)
    c = avg['close']
    buy = cross_up(kdj['K'], kdj['D']) & (kdj['K'] < k_buy)
    sell = cross_down(kdj['K'], kdj['D']) & (kdj['K'] > k_sell)
    if use_ma20:
        sell = sell | cross_down(c, kdj['ma20'])
    return pd.DataFrame({'buy_sig': buy.fillna(False),
                         'sell_sig': sell.fillna(False)}, index=avg.index)


def macd_sig(avg, fast=12, slow=26):
    return calc_signals(avg['close'], fast=fast, slow=slow)


def evaluate(avg, e1, e2, sig, start=None, end=None):
    """start/end 限定统计区间；end 存在时同步截断数据（信号预热不受影响，
    因为 sig 是基于完整 avg 预先算好的）"""
    if end is not None:
        avg = avg[avg.index < end]
        e1 = e1[e1.index < end]
        e2 = e2[e2.index < end]
        sig = sig[sig.index < end]
    r = run_backtest(avg, e1, e2, fee=FEE, signals=sig,
                     start_date=start, verbose=False)
    nav = r['nav_strat']
    return annualized(nav) * 100, max_drawdown(nav) * 100, len(r['trades']), nav


def main():
    avg = load('avg_880003.csv')
    e1 = load('etf_512100_hfq.csv')
    e2 = load('etf_510880_hfq.csv')
    start0 = e1.index[0] - pd.Timedelta(days=120)
    avg = avg[avg.index >= start0]

    # 基准：5年窗口持有不动
    r0 = run_backtest(avg, e1, e2, fee=FEE,
                      signals=macd_sig(avg), start_date=W_START, verbose=False)
    print('===== 窗口 %s ~ %s =====' % (W_START, r0['idx'][-1].date()))
    print('持有不动: 1000ETF %+.2f%% / 红利ETF %+.2f%%'
          % ((r0['nav_1000'].iloc[-1] - 1) * 100, (r0['nav_div'].iloc[-1] - 1) * 100))

    print('\n===== 固定规则 =====')
    print('%-36s %8s %8s %6s | IS年化  OOS年化' % ('规则', '年化%', '回撤%', '换仓'))
    fixed = [
        ('MACD 12/26（现行）', macd_sig(avg)),
        ('KDJ(9) K<30买/K>70死叉+破MA20卖', kdj_sig(avg)),
    ]
    for name, sig in fixed:
        ann, dd, nt, _ = evaluate(avg, e1, e2, sig, start=W_START)
        ann_is, _, _, _ = evaluate(avg, e1, e2, sig, start=W_START, end=SPLIT)
        ann_oos, _, _, _ = evaluate(avg, e1, e2, sig, start=SPLIT)
        print('%-36s %+7.2f %+7.2f %6d | %+7.2f %+7.2f' % (name, ann, dd, nt, ann_is, ann_oos))

    grids = {
        'MACD': [(f, s) for f, s in itertools.product([8, 12, 17], [21, 26, 34]) if f < s],
        'KDJ': list(itertools.product([6, 9, 14, 21], [20, 30, 40], [60, 70, 80], [True, False])),
    }
    for kind, grid in grids.items():
        scored = []
        for p in grid:
            sig = (macd_sig(avg, p[0], p[1]) if kind == 'MACD'
                   else kdj_sig(avg, p[0], p[1], p[2], p[3]))
            ann_is, dd_is, nt_is, _ = evaluate(avg, e1, e2, sig, start=W_START, end=SPLIT)
            if nt_is < 3:
                continue
            scored.append((ann_is, dd_is, p, sig))
        scored.sort(key=lambda x: -x[0])
        print('\n===== %s 寻优：样本内 Top5 → 样本外验证 =====' % kind)
        for ann_is, dd_is, p, sig in scored[:5]:
            ann_oos, dd_oos, _, _ = evaluate(avg, e1, e2, sig, start=SPLIT)
            ann_all, dd_all, nt_all, _ = evaluate(avg, e1, e2, sig, start=W_START)
            print('  参数%-20s IS年化 %+6.2f%%(回撤%5.1f) → OOS年化 %+6.2f%%(回撤%5.1f) | 全窗口 %+6.2f%% 换仓%d'
                  % (str(p), ann_is, dd_is, ann_oos, dd_oos, ann_all, nt_all))


if __name__ == '__main__':
    main()
