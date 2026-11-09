#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第三轮：跨资产动量、组合信号（趋势确认/回撤止损）、信号载体替换。"""
import io
import sys

import numpy as np
import pandas as pd

import research_engine as re
from backtest import calc_signals

if sys.platform == 'win32' and sys.stdout is not None:
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

START, END, FEE = '2020-01-02', '2026-10-23', re.FEE
META = {m['code']: m for m in re.load_meta()}
O, C = re.panel(sorted(META))
AVG = re.avg_price()
IDX = C.index.intersection(AVG.index)
IDX = IDX[(IDX >= pd.Timestamp(START)) & (IDX <= pd.Timestamp(END))]
ATTACK_OLD, ATTACK_NEW, SWITCH = '512100', '159552', pd.Timestamp('2024-06-28')


def run(W):
    return re.run(O, C, W, fee=FEE, start=START, end=END)


def state_rules(sig, close, trend_confirm=False, trail=None):
    dif, dea = sig['dif'], sig['dea']
    ma60 = close.rolling(60).mean()
    d1, d2 = dif.shift(1), dif.shift(2)
    up = (d1 < d2) & (dif > d1) & (dif < 0)
    down = (d1 > d2) & (dif < d1)
    confirm = (close > ma60) & (dif > 0) & (dif > d1) & (d1 <= d2)
    state = pd.Series(False, index=close.index)
    s = False
    peak = None
    for i in range(len(close)):
        c = float(close.iloc[i]) if not np.isnan(close.iloc[i]) else np.nan
        if s:
            if peak is not None and not np.isnan(c):
                peak = max(peak, c)
            sell = bool(down.iloc[i]) or (not np.isnan(ma60.iloc[i]) and c < ma60.iloc[i])
            if trail and peak and not np.isnan(c) and c / peak - 1 <= -trail:
                sell = True
            if sell:
                s, peak = False, None
        else:
            if bool(up.iloc[i]) or (trend_confirm and bool(confirm.iloc[i])):
                s, peak = True, c
        state.iloc[i] = s
    return state


def W_from_state(state, defense='512890'):
    W = pd.DataFrame(0.0, index=IDX, columns=O.columns)
    on = state.reindex(IDX).fillna(False).astype(bool)
    for i in range(1, len(IDX)):
        d = IDX[i]
        if on.iloc[i - 1]:
            W.loc[d, ATTACK_OLD if d < SWITCH else ATTACK_NEW] = 1.0
        else:
            W.loc[d, defense] = 1.0
    return W


def show(rows, title, n=20):
    print('\n' + '=' * 106)
    print(title)
    print('=' * 106)
    print('%-46s %9s %9s %7s %7s %8s' % ('规则', '年化', '回撤', 'Calmar', 'Sharpe', '样本外'))
    print('-' * 106)
    for nm, st, nav in sorted(rows, key=lambda r: r[1]['cagr'], reverse=True)[:n]:
        sub = nav[nav.index >= pd.Timestamp('2024-01-01')]
        s2 = re.metrics(sub)
        print('%-46s %8.2f%% %8.2f%% %7.2f %7.2f %7.2f%%' %
              (nm, st['cagr'] * 100, st['mdd'] * 100, st['calmar'], st['sharpe'], s2['cagr'] * 100))


def main():
    rows = []
    close_avg = AVG.reindex(O.index).ffill()
    sig = calc_signals(close_avg, sell_anywhere=True, fast=17, slow=34, sig_n=9)

    # 1) 组合信号：趋势确认 / 回撤止损
    for tc in (False, True):
        for trail in (None, 0.05, 0.08, 0.12):
            st_ = state_rules(sig, close_avg, trend_confirm=tc, trail=trail)
            nav, st, to = run(W_from_state(st_))
            rows.append(('R 择时+趋势确认%s 止损%s' % ('开' if tc else '关', trail or '无'), st, nav))

    # 2) 信号载体替换（同样的 DIF 拐点规则，换指数/ETF）
    for carrier in ('159552', '512100', '588220', '159915', '510300', '512480'):
        s = C[carrier].dropna()
        sg = calc_signals(s, sell_anywhere=True, fast=17, slow=34, sig_n=9)
        st_ = state_rules(sg, s)
        nav, st, to = run(W_from_state(st_))
        rows.append(('C 信号载体 %s' % carrier, st, nav))

    show(rows, '组合信号 / 信号载体（全区间年化）')

    # 3) 跨资产动量（月频）：股/海外/黄金/债
    pools = {
        '股+海外+金': ['159552', '512100', '588220', '159915', '510300', '513100', '518880'],
        '股+海外+金+债': ['159552', '512100', '588220', '159915', '510300', '513100', '518880', '511260'],
        '全行业+海外+金': ['512480', '515220', '515880', '512400', '513100', '518880', '159552', '159915'],
    }
    rows2 = []
    for pname, pool in pools.items():
        for N in (20, 60, 120):
            for k in (1, 2):
                for absf in (False, True):
                    for dfn in ('512890', '511260'):
                        W = re.momentum_targets(O, C, IDX, pool, N=N, top_k=k, rebal=20,
                                                abs_filter=absf, defense=dfn)
                        nav, st, to = run(W)
                        rows2.append(('X %s N%d top%d %s 防%s' %
                                      (pname, N, k, '绝对' if absf else '相对', dfn), st, nav))
    show(rows2, '跨资产动量（20 日调仓，全区间年化）', n=20)


if __name__ == '__main__':
    main()
