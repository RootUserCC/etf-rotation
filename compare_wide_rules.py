#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""宽基/行业/跨境 ETF 买卖规则搜索（统一口径，含样本内/外拆分）。

规则家族：
  T  880003 择时（DIF 拐点参数网格）→ 固定进攻腿（512100→159552 拼接）/ 防守 512890
  M  截面动量轮动（池 × 动量窗口 × 持仓数 × 调仓频率 × 绝对动量过滤 × 防守标的）
  H  择时 + 动量择优（880003 决定进攻/防守，进攻仓内部按动量轮动）
评估：T 日收盘信号、T+1 开盘成交、双边万1；全区间 2020-01-02~2026-10-23，
      样本内 2020-2023，样本外 2024-2026。
"""
import io
import sys

import numpy as np
import pandas as pd

import research_engine as re

if sys.platform == 'win32' and sys.stdout is not None:
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from backtest import calc_signals

START = '2020-01-02'
END = '2026-10-23'
FEE = re.FEE

META = {m['code']: m for m in re.load_meta()}
BROAD = [c for c, m in META.items() if m['group'] == 'broad']
SECTOR = [c for c, m in META.items() if m['group'] == 'sector']
ALL = BROAD + SECTOR
WIDE = ALL + [c for c, m in META.items() if m['group'] in ('overseas', 'commodity', 'bond')]

O, C = re.panel(sorted(set(WIDE)))
AVG = re.avg_price()
IDX = C.index.intersection(AVG.index)
IDX = IDX[(IDX >= pd.Timestamp(START)) & (IDX <= pd.Timestamp(END))]

ATTACK_OLD, ATTACK_NEW = '512100', '159552'
SWITCH = pd.Timestamp('2024-06-28')


def state_from(dif, up_rule='below', sell_rule='anywhere'):
    dif = dif.dropna()
    d1, d2 = dif.shift(1), dif.shift(2)
    up = (d1 < d2) & (dif > d1)
    down = (d1 > d2) & (dif < d1)
    if up_rule == 'below':
        buy = up & (dif < 0)
    elif up_rule == 'anywhere':
        buy = up
    elif up_rule == 'above':
        buy = up & (dif > 0)
    sell = down if sell_rule == 'anywhere' else (down & (dif > 0))
    state = pd.Series(False, index=dif.index)
    s = False
    for i in range(len(dif)):
        if buy.iloc[i]:
            s = True
        elif sell.iloc[i]:
            s = False
        state.iloc[i] = s
    return state


def attack_weights(frame):
    """进攻权重随拼接日切换：2024-06-28 前 512100，之后 159552"""
    W = pd.DataFrame(0.0, index=IDX, columns=frame.columns)
    for i, d in enumerate(IDX):
        W.loc[d, ATTACK_OLD if d < SWITCH else ATTACK_NEW] = 1.0
    return W


def run_targets(W, start=START, end=END):
    nav, st, to = re.run(O, C, W, fee=FEE, start=start, end=end)
    return nav, st, float(to.sum())



def table(rows, title):
    print('\n' + '=' * 118)
    print(title)
    print('=' * 118)
    print('%-46s %9s %9s %7s %7s %7s %10s' %
          ('规则', '年化', '最大回撤', 'Calmar', 'Sharpe', '换手', '累计'))
    print('-' * 118)
    for name, st, to, nav in rows:
        print('%-46s %8.2f%% %8.2f%% %7.2f %7.2f %7.1f %9.1f%%' %
              (name, st['cagr'] * 100, st['mdd'] * 100, st['calmar'], st['sharpe'],
               to, st['total'] * 100))


def build_switch(signal, attack_pool=None, attack_mom=None, defense='512890'):
    """择时 + （可选）进攻池动量择优；signal 为 T 日收盘状态，T+1 开盘生效"""
    W = pd.DataFrame(0.0, index=IDX, columns=O.columns)
    on = signal.reindex(IDX).fillna(False).astype(bool)

    if attack_pool is None:
        for i in range(1, len(IDX)):
            d = IDX[i]
            if on.iloc[i - 1]:
                W.loc[d, ATTACK_OLD if d < SWITCH else ATTACK_NEW] = 1.0
            else:
                W.loc[d, defense] = 1.0
        return W

    # 动量择优：进攻区间内每 5 个交易日重排一次
    score = C[attack_pool] / C[attack_pool].shift(attack_mom or 20) - 1
    for i in range(1, len(IDX)):
        d = IDX[i]
        if not on.iloc[i - 1]:
            W.loc[d, defense] = 1.0
            continue
        if i % 5 != 0:
            W.iloc[i] = W.iloc[i - 1].values
            continue
        s = score.loc[IDX[i - 1]].dropna()
        s = s[s.index.isin(attack_pool)]
        if not len(s):
            W.loc[d, ATTACK_OLD if d < SWITCH else ATTACK_NEW] = 1.0
        else:
            W.loc[d, s.idxmax()] = 1.0
    return W


def main():
    print('数据：%d 只 ETF，%s ~ %s，共 %d 个交易日' %
          (O.shape[1], IDX[0].date(), IDX[-1].date(), len(IDX)))
    print('口径：T 收盘信号 → T+1 开盘成交，双边万1；样本内 2020-2023 / 样本外 2024-2026')

    close_avg = AVG.reindex(O.index).ffill()
    sig = calc_signals(close_avg, sell_anywhere=True, fast=17, slow=34, sig_n=9)
    base_state = state_from(sig['dif'])
    W_base = build_switch(base_state)
    nav_base, st_base, to_base = run_targets(W_base)

    rows_t, rows_m, rows_h = [], [], []

    # ---------- T：880003 择时参数 ----------
    best_t = None
    for fast, slow in [(12, 26), (17, 34), (20, 38), (24, 44)]:
        s = calc_signals(close_avg, sell_anywhere=True, fast=fast, slow=slow, sig_n=9)
        for up in ('below', 'anywhere'):
            for sell in ('anywhere', 'above'):
                state = state_from(s['dif'], up, sell)
                W = build_switch(state)
                nav, st, to = run_targets(W)
                nm = 'T DIF(%d/%d) 买%s 卖%s' % (fast, slow, up, sell)
                rows_t.append((nm, st, to, nav))
                if best_t is None or st['cagr'] > best_t[1]['cagr']:
                    best_t = (nm, st)

    # ---------- M：截面动量轮动 ----------
    pools = {'宽基': BROAD, '行业': SECTOR, '宽基+行业': ALL, '全池': WIDE}
    best_m = None
    for pname, pool in pools.items():
        for N in (10, 20, 40, 60):
            for k in (1, 2, 3):
                for rebal in (5, 20):
                    for absf in (False, True):
                        for dfn in ('512890', '511260'):
                            W = re.momentum_targets(O, C, IDX, pool, N=N, top_k=k,
                                                    rebal=rebal, abs_filter=absf, defense=dfn)
                            nav, st, to = run_targets(W)
                            nm = 'M %s N%d top%d %d日%s 防守%s' % (
                                pname, N, k, rebal, '绝对' if absf else '', dfn)
                            rows_m.append((nm, st, to, nav))
                            if best_m is None or st['cagr'] > best_m[1]['cagr']:
                                best_m = (nm, st)

    # ---------- H：择时 + 动量择优 ----------
    best_h = None
    for pool_name, pool in (('行业', SECTOR), ('宽基+行业', ALL), ('全池', WIDE)):
        for mom in (10, 20, 60):
            W = build_switch(base_state, attack_pool=pool, attack_mom=mom)
            nav, st, to = run_targets(W)
            nm = 'H 基线择时 + %s 动量%d 择优' % (pool_name, mom)
            rows_h.append((nm, st, to, nav))
            if best_h is None or st['cagr'] > best_h[1]['cagr']:
                best_h = (nm, st)

    # ---------- 输出 ----------
    def top(rows, key='cagr', n=18):
        return sorted(rows, key=lambda r: r[1][key], reverse=True)[:n]

    print('\n基线（方案C 17/34/9）：年化 %.2f%%，回撤 %.2f%%，换手 %.0f'
          % (st_base['cagr'] * 100, st_base['mdd'] * 100, to_base))
    table([(n, st, to, nav) for n, st, to, nav in top(rows_t)], 'T 择时家族 Top18（按全区间年化）')
    table(top(rows_m), 'M 动量轮动 Top18（按全区间年化）')
    table(top(rows_h), 'H 择时+择优（按全区间年化）')

    # 样本外：对全区间前 30 名重算 2024 起表现
    print('\n' + '=' * 118)
    print('样本外检验（2024-01-01 ~ 2026-10-23）：全区间 Top10 规则')
    print('=' * 118)
    print('%-46s %9s %9s %7s' % ('规则', '样本外年化', '样本外回撤', 'Calmar'))
    print('-' * 118)
    for nm, st, to, nav in sorted(rows_m + rows_h + rows_t,
                                  key=lambda r: r[1]['cagr'], reverse=True)[:10]:
        sub = nav[nav.index >= pd.Timestamp('2024-01-01')]
        s2 = re.metrics(sub)
        print('%-46s %8.2f%% %8.2f%% %7.2f' %
              (nm, s2['cagr'] * 100, s2['mdd'] * 100, s2['calmar']))

    # 分年度：基线 + 三类最优
    nav_map = {'基线': nav_base}
    for label, best, rows in (('最优T', best_t, rows_t), ('最优M', best_m, rows_m),
                              ('最优H', best_h, rows_h)):
        if not best:
            continue
        for nm, st, to, nav in rows:
            if nm == best[0]:
                nav_map[label] = nav
                break
    years = sorted(set().union(*[set(re.yearly_returns(n)) for n in nav_map.values()]))
    labels = list(nav_map)
    print('%-6s ' % '年份' + ' '.join('%10s' % l for l in labels))
    for y in years:
        vals = [re.yearly_returns(nav_map[l]).get(y) for l in labels]
        print('%-6d ' % y + ' '.join('%10s' % (('%.1f%%' % (v * 100)) if v is not None else '-')
                                     for v in vals))

    print('\n最优规则：')
    print('  基线      %s  年化 %.2f%%  回撤 %.2f%%' % ('方案C', st_base['cagr'] * 100, st_base['mdd'] * 100))
    if best_t:
        print('  择时最优  %s  年化 %.2f%%  回撤 %.2f%%' % (best_t[0], best_t[1]['cagr'] * 100, best_t[1]['mdd'] * 100))
    if best_m:
        print('  动量最优  %s  年化 %.2f%%  回撤 %.2f%%' % (best_m[0], best_m[1]['cagr'] * 100, best_m[1]['mdd'] * 100))
    if best_h:
        print('  混合最优  %s  年化 %.2f%%  回撤 %.2f%%' % (best_h[0], best_h[1]['cagr'] * 100, best_h[1]['mdd'] * 100))


if __name__ == '__main__':
    main()
