#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
参数寻优（不影响方案B）：网格搜索 DIF 信号策略的参数组合
搜索空间：
  MACD 参数 fast/slow/sig
  信号类型 turn(DIF拐点) / cross(DIF与DEA金叉死叉)
  买入阈值 buy_th（DIF 低于该值才买）/ 卖出阈值 sell_th（DIF 高于该值才卖）
排名按全区间累计收益，并对前若干名做样本外（后半段）检验
"""
import sys
import io
import numpy as np
import pandas as pd

if sys.platform == 'win32' and not getattr(sys.stdout, '_utf8_wrapped', False):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stdout._utf8_wrapped = True

from backtest import ema, annualized, max_drawdown
from export_json import load

FEE = 0.0001          # 单边万1，换仓扣双边
MIN_TRADES = 5
SPLIT = '2021-08-01'  # 样本内外分界


def run(avg_close, idx, r1000, rdiv, fast, slow, sig_n, mode, buy_th, sell_th):
    dif_full = ema(avg_close, fast) - ema(avg_close, slow)
    dea_full = ema(dif_full, sig_n)
    dif = dif_full.loc[idx]
    dea = dea_full.loc[idx]
    d1 = dif.shift(1)
    d2 = dif.shift(2)
    if mode == 'turn':
        up = (d1 < d2) & (dif > d1)
        down = (d1 > d2) & (dif < d1)
    else:  # cross：金叉/死叉
        e1 = dea.shift(1)
        up = (d1 <= e1) & (dif > dea)
        down = (d1 >= e1) & (dif < dea)
    buy_sig = (up & (dif < buy_th)).fillna(False).values
    sell_sig = (down & (dif > sell_th)).fillna(False).values

    n = len(idx)
    hold = np.zeros(n, dtype=bool)
    state = False
    pending = None
    trades = 0
    for i in range(n):
        if pending is not None:
            if pending and not state:
                state = True
                trades += 1
            elif not pending and state:
                state = False
                trades += 1
            pending = None
        hold[i] = state
        if buy_sig[i]:
            pending = True
        elif sell_sig[i]:
            pending = False

    if trades < MIN_TRADES:
        return None
    sr = np.where(hold, r1000, rdiv)
    # 换仓日扣双边费用：hold 发生跳变的当天
    switch = np.zeros(n)
    switch[1:] = np.abs(hold[1:].astype(int) - hold[:-1].astype(int))
    sr = sr - switch * 2 * FEE
    nav = pd.Series(np.cumprod(1 + sr), index=idx)
    return nav, trades


def main():
    avg = load('avg_880003.csv')
    etf1000 = load('etf_512100_hfq.csv')
    etfdiv = load('etf_510880_hfq.csv')
    idx = etf1000.index.intersection(etfdiv.index).intersection(avg.index)
    avg_close = avg['close']
    r1000 = etf1000.loc[idx, 'close'].pct_change().fillna(0).values
    rdiv = etfdiv.loc[idx, 'close'].pct_change().fillna(0).values

    fasts = [6, 8, 10, 12, 14, 16]
    slows = [21, 26, 30, 35, 40, 45]
    signos = [5, 7, 9, 11]
    modes = ['turn', 'cross']
    buy_ths = [0.0, np.inf]        # 零下才买 / 任意位置
    sell_ths = [-np.inf, 0.0]      # 任意位置卖 / 零上才卖

    results = []
    tested = 0
    for fast in fasts:
        for slow in slows:
            if fast >= slow:
                continue
            for sig_n in signos:
                for mode in modes:
                    for bt in buy_ths:
                        for st in sell_ths:
                            tested += 1
                            out = run(avg_close, idx, r1000, rdiv,
                                      fast, slow, sig_n, mode, bt, st)
                            if out is None:
                                continue
                            nav, trades = out
                            results.append({
                                'fast': fast, 'slow': slow, 'sig': sig_n,
                                'mode': mode,
                                'buy': '零下' if bt == 0 else '任意',
                                'sell': '任意' if st == -np.inf else '零上',
                                'cum': float(nav.iloc[-1] - 1),
                                'ann': annualized(nav),
                                'mdd': max_drawdown(nav),
                                'trades': trades,
                                'nav': nav,
                            })
    print('共测试 %d 个参数组合，%d 个有效（换仓>=%d次）'
          % (tested, len(results), MIN_TRADES))

    results.sort(key=lambda r: r['cum'], reverse=True)

    # 方案B基准：12/26/9 turn 零下买 任意卖
    for r in results:
        if (r['fast'], r['slow'], r['sig'], r['mode'], r['buy'], r['sell']) \
                == (12, 26, 9, 'turn', '零下', '任意'):
            r['is_B'] = True

    split = pd.Timestamp(SPLIT)
    print('\n%-4s %-14s %-6s %-14s %9s %8s %9s %6s | 样本外(%s后) %8s %9s'
          % ('排名', 'MACD', '信号', '买/卖阈值', '累计收益', '年化', '最大回撤', '换仓',
             SPLIT[:4], '年化', '最大回撤'))
    shown_B = False
    for rank, r in enumerate(results[:15], 1):
        nav = r['nav']
        oos = nav.loc[nav.index >= split]
        oos = oos / oos.iloc[0]
        print('%-4d %-14s %-6s %-14s %8.1f%% %7.2f%% %8.1f%% %6d | %16.2f%% %8.1f%%'
              % (rank, '%d/%d/%d' % (r['fast'], r['slow'], r['sig']),
                 '拐点' if r['mode'] == 'turn' else '叉',
                 '%s买/%s卖' % (r['buy'], r['sell']),
                 r['cum'] * 100, r['ann'] * 100, r['mdd'] * 100, r['trades'],
                 annualized(oos) * 100, max_drawdown(oos) * 100))
    # 方案B在全量排名中的位置
    for rank, r in enumerate(results, 1):
        if r.get('is_B'):
            nav = r['nav']
            oos = nav.loc[nav.index >= split]
            oos = oos / oos.iloc[0]
            print('\n方案B(12/26/9 拐点 零下买/任意卖) 全量排名 %d/%d: '
                  '累计 %.1f%% 年化 %.2f%% 回撤 %.1f%% 换仓 %d | 样本外年化 %.2f%% 回撤 %.1f%%'
                  % (rank, len(results), r['cum'] * 100, r['ann'] * 100,
                     r['mdd'] * 100, r['trades'],
                     annualized(oos) * 100, max_drawdown(oos) * 100))
            shown_B = True
    if not shown_B:
        print('\n方案B不在有效结果中')


if __name__ == '__main__':
    main()
