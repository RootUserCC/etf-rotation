#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第二轮：卖出规则 / 防守标的 / 买入确认，以及全部标的买入持有基准。

统一口径：T 收盘信号、T+1 开盘成交、双边万1；2020-01-02~2026-10-23。
"""
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
CODES = sorted(META)
O, C = re.panel(CODES)
AVG = re.avg_price()
IDX = C.index.intersection(AVG.index)
IDX = IDX[(IDX >= pd.Timestamp(START)) & (IDX <= pd.Timestamp(END))]
ATTACK_OLD, ATTACK_NEW = '512100', '159552'
SWITCH = pd.Timestamp('2024-06-28')
DEFENSE_POOL = ['512890', '511260', '518880', '511010', '510880']


def run(W, start=START, end=END):
    return re.run(O, C, W, fee=FEE, start=start, end=end)


def build_state(sig, close, buy_mode, sell_mode):
    dif, dea = sig['dif'], sig['dea']
    ma20 = close.rolling(20).mean()
    ma60 = close.rolling(60).mean()
    d1, d2 = dif.shift(1), dif.shift(2)
    up = (d1 < d2) & (dif > d1)
    down = (d1 > d2) & (dif < d1)

    if buy_mode == 'below_up':
        buy = up & (dif < 0)
    elif buy_mode == 'below_up_ma20':
        buy = up & (dif < 0) & (close > ma20)
    elif buy_mode == 'up_any':
        buy = up
    else:
        buy = up & (dif < 0)
    buy2 = up & (d1 < d2) & (dif.shift(2) < dif.shift(1)) & (dif < 0)  # 连续两日抬升
    if buy_mode == 'confirm':
        buy = buy2

    if sell_mode == 'down':
        sell = down
    elif sell_mode == 'down_ma20':
        sell = down & (close < ma20)
    elif sell_mode == 'down_ma60':
        sell = down & (close < ma60)
    elif sell_mode == 'dead':
        sell = dif < dea
    elif sell_mode == 'ma20':
        sell = close < ma20
    elif sell_mode == 'ma60':
        sell = close < ma60
    elif sell_mode == 'down2':
        sell = down & (dif.shift(1) < dif.shift(2))
    else:
        sell = down

    state = pd.Series(False, index=close.index)
    s = False
    for i in range(len(close)):
        if s:
            if bool(sell.iloc[i]):
                s = False
        else:
            if bool(buy.iloc[i]):
                s = True
        state.iloc[i] = s
    return state


def weights_switch(state, defense='512890', defense_pool=None, def_mom=20,
                   attack='spliced'):
    W = pd.DataFrame(0.0, index=IDX, columns=O.columns)
    on = state.reindex(IDX).fillna(False).astype(bool)
    score = C[DEFENSE_POOL] / C[DEFENSE_POOL].shift(def_mom) - 1
    held_def = defense
    for i in range(1, len(IDX)):
        d = IDX[i]
        if on.iloc[i - 1]:
            if attack == 'spliced':
                W.loc[d, ATTACK_OLD if d < SWITCH else ATTACK_NEW] = 1.0
            else:
                W.loc[d, attack] = 1.0
            continue
        if defense_pool:
            s = score.loc[IDX[i - 1]].dropna()
            if len(s):
                held_def = s.idxmax()
        W.loc[d, held_def] = 1.0
    return W


def show(rows, title, n=15):
    print('\n' + '=' * 104)
    print(title)
    print('=' * 104)
    print('%-44s %9s %9s %7s %7s %8s' % ('规则', '年化', '回撤', 'Calmar', 'Sharpe', '样本外年化'))
    print('-' * 104)
    for nm, st, nav, to in sorted(rows, key=lambda r: r[1]['cagr'], reverse=True)[:n]:
        sub = nav[nav.index >= pd.Timestamp('2024-01-01')]
        s2 = re.metrics(sub)
        print('%-44s %8.2f%% %8.2f%% %7.2f %7.2f %8.2f%%' %
              (nm, st['cagr'] * 100, st['mdd'] * 100, st['calmar'], st['sharpe'], s2['cagr'] * 100))


def main():
    close_avg = AVG.reindex(O.index).ffill()
    sig = calc_signals(close_avg, sell_anywhere=True, fast=17, slow=34, sig_n=9)
    rows = []

    # ---- 1. 买入/卖出规则组合 ----
    for buy_m in ('below_up', 'below_up_ma20', 'confirm', 'up_any'):
        for sell_m in ('down', 'down_ma20', 'down_ma60', 'dead', 'ma20', 'ma60', 'down2'):
            st_ = build_state(sig, close_avg, buy_m, sell_m)
            W = weights_switch(st_)
            nav, st, to = run(W)
            rows.append(('B %s / S %s' % (buy_m, sell_m), st, nav, to.sum()))

    # ---- 2. 防守择优（红利/国债/黄金/现金）----
    for pool in (None, ['512890', '511260'], ['512890', '511260', '518880'],
                 ['512890', '518880'], DEFENSE_POOL):
        for mom in (10, 20, 60):
            if pool is None and mom != 20:
                continue
            st_ = build_state(sig, close_avg, 'below_up', 'down')
            W = weights_switch(st_, defense_pool=pool, def_mom=mom)
            nav, st, to = run(W)
            nm = 'D 防守择优 %s M%d' % ('固定512890' if pool is None else '+'.join(pool), mom)
            rows.append((nm, st, nav, to.sum()))

    # ---- 3. 进攻腿换标的（择时不变）----
    for atk in ('159552', '512100', '588220', '159915', '510300', '512890'):
        st_ = build_state(sig, close_avg, 'below_up', 'down')
        W = weights_switch(st_, attack=atk)
        nav, st, to = run(W)
        rows.append(('A 进攻固定 %s' % atk, st, nav, to.sum()))

    show(rows, '买入/卖出/防守/进攻标的 搜索结果（全区间年化排序）')

    # ---- 4. 全部标的买入持有基准 ----
    print('\n' + '=' * 104)
    print('买入持有基准（2020-01-02~2026-10-23，后复权，不含费用）')
    print('=' * 104)
    bh = []
    for c in CODES:
        s = C[c].dropna()
        s = s[(s.index >= pd.Timestamp(START)) & (s.index <= pd.Timestamp(END))]
        if len(s) < 200:
            continue
        m = re.metrics(s / s.iloc[0])
        bh.append((c, META[c]['name'], META[c]['group'], m['cagr'], m['mdd']))
    print('%-7s %-16s %-10s %9s %9s' % ('代码', '名称', '类别', '年化', '最大回撤'))
    for c, n, g, cg, md in sorted(bh, key=lambda x: x[3], reverse=True):
        print('%-7s %-16s %-10s %8.1f%% %8.1f%%' % (c, n, g, cg * 100, md * 100))


if __name__ == '__main__':
    main()
