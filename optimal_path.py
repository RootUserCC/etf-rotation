#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""上帝视角最优路径：2026 年起每日事后在 159552 / 512890 间二选一的最大收益路径

动态规划全局最优：状态=当日收盘持有哪条腿，换仓扣双边万2，收盘→收盘口径。
输出 result_optimal.txt：最优持仓段明细 + 与系统(方案C)/买入持有的对比 + 规律统计。
用途：给"择时最多值多少钱"一个上界参照——系统收益与最优路径的差，就是信号不完美
所付出的理论成本；不代表任何可实盘实现的策略。
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

from compare_improve import (load, calc_signals, leg_returns, run_variant,
                             SWITCH_ATK, START, FEE, annualized, max_drawdown)

COST = 2 * FEE          # 换仓双边万2
YEAR_START = pd.Timestamp('2026-01-01')


def optimal_path(r_a, r_d, idx, cost):
    """DP：V[t][s]=第t天收盘持有s的最大净值；回溯最优路径"""
    n = len(idx)
    ra, rd = r_a.values, r_d.values
    V = np.ones((n, 2))
    pre = np.zeros((n, 2), dtype=int)      # 前一天持有的腿
    for t in range(1, n):
        for s in range(2):
            r = ra[t] if s == 0 else rd[t]
            stay = V[t - 1][s]
            sw = V[t - 1][1 - s] * (1 - cost)
            if stay >= sw:
                V[t][s] = stay * (1 + r)
                pre[t][s] = s
            else:
                V[t][s] = sw * (1 + r)
                pre[t][s] = 1 - s
    # 回溯
    path = np.zeros(n, dtype=int)
    path[-1] = 0 if V[-1][0] >= V[-1][1] else 1
    for t in range(n - 1, 0, -1):
        path[t - 1] = pre[t][path[t]]
    nav = np.ones(n)
    for t in range(1, n):
        c = cost if path[t] != path[t - 1] else 0.0
        nav[t] = nav[t - 1] * (1 - c) * (1 + (ra[t] if path[t] == 0 else rd[t]))
    return pd.Series(nav, index=idx), pd.Series(path, index=idx)


def main():
    atk = load('etf_159552_hfq.csv')
    div = load('etf_512890_hfq.csv')
    idx = atk.index.intersection(div.index)
    idx = idx[idx >= YEAR_START]
    r_a = atk['close'].loc[idx].pct_change().fillna(0)
    r_d = div['close'].loc[idx].pct_change().fillna(0)

    nav_opt, path = optimal_path(r_a, r_d, idx, COST)

    # 系统（方案C）同窗对比
    avg = load('avg_880003.csv')
    etf1000 = load('etf_512100_hfq.csv')
    etf2000e = load('etf_159552_hfq.csv')
    sig_full = calc_signals(avg['close'], sell_anywhere=True, fast=17, slow=34, sig_n=9)
    atk_o = pd.concat([etf1000['open'][etf1000.index < SWITCH_ATK],
                       etf2000e['open'][etf2000e.index >= SWITCH_ATK]])
    atk_c = pd.concat([etf1000['close'][etf1000.index < SWITCH_ATK],
                       etf2000e['close'][etf2000e.index >= SWITCH_ATK]])
    idx_full = div.index.intersection(atk_c.index).intersection(avg.index)
    idx_full = idx_full[idx_full >= START]
    on_a, id_a, cc_a = leg_returns(atk_o, atk_c)
    on_d, id_d, cc_d = leg_returns(div['open'], div['close'])
    on1, id1, cc1 = leg_returns(etf1000['open'], etf1000['close'])
    on_a.loc[SWITCH_ATK], id_a.loc[SWITCH_ATK], cc_a.loc[SWITCH_ATK] = \
        on1.loc[SWITCH_ATK], id1.loc[SWITCH_ATK], cc1.loc[SWITCH_ATK]
    legs_ret = {'atk': (on_a.loc[idx_full].fillna(0), id_a.loc[idx_full].fillna(0), cc_a.loc[idx_full].fillna(0)),
                'div': (on_d.loc[idx_full].fillna(0), id_d.loc[idx_full].fillna(0), cc_d.loc[idx_full].fillna(0))}
    nav_sys, st_sys, _ = run_variant(idx_full, sig_full.loc[idx_full], legs_ret,
                                     div['close'].loc[idx_full],
                                     np.full(len(idx_full), np.nan),
                                     exec_mode='open', use_cash=False)
    nav_sys = nav_sys.loc[idx]
    nav_sys = nav_sys / nav_sys.iloc[0]
    st_sys = st_sys.loc[idx]

    nav_hold_a = (1 + r_a).cumprod()
    nav_hold_d = (1 + r_d).cumprod()

    out = []
    pr = out.append
    pr('上帝视角最优路径 vs 系统 vs 持有（%s ~ %s，%d 个交易日）' % (idx[0].date(), idx[-1].date(), len(idx)))
    pr('=' * 78)
    for name, nv in [('上帝视角最优路径', nav_opt), ('方案C系统(实际执行)', nav_sys),
                     ('159552 买入持有', nav_hold_a), ('512890 买入持有', nav_hold_d)]:
        pr('%-22s 累计 %+8.1f%%  最大回撤 %6.1f%%' %
           (name, (nv.iloc[-1] - 1) * 100, max_drawdown(nv) * 100))
    pr('系统收益 / 最优路径 = %.1f%%（信号不完美的理论成本为 %.1f 个百分点）' %
       ((nav_sys.iloc[-1] - 1) / (nav_opt.iloc[-1] - 1) * 100,
        ((nav_opt.iloc[-1] - 1) - (nav_sys.iloc[-1] - 1)) * 100))

    # 最优路径持仓段明细
    pr('\n最优路径持仓段（换仓共 %d 次）:' % int((path.diff() != 0).sum()))
    pr('%-4s %-14s %-12s %-12s %6s %9s %14s' % ('#', '标的', '进入日', '离开日', '持有天', '段收益', '同段系统持有'))
    seg_start = 0
    segs = []
    for i in range(1, len(idx) + 1):
        if i == len(idx) or path.iloc[i] != path.iloc[seg_start]:
            leg = path.iloc[seg_start]
            r = r_a if leg == 0 else r_d
            # 段收益：进入当日即吃新腿收益并扣换仓成本（与 nav 口径一致）
            seg_ret = float((1 + r.iloc[seg_start:i]).prod() - 1)
            if seg_start > 0:
                seg_ret -= COST
            sys_hold = st_sys.iloc[seg_start:i].mode()
            sys_name = {'atk': '进攻仓', 'div': '防守仓'}[sys_hold.iloc[0]] if len(sys_hold) else '-'
            segs.append((leg, idx[seg_start], idx[i - 1], i - seg_start, seg_ret, sys_name))
            seg_start = i
    for n, (leg, b, s, days, ret, sys_name) in enumerate(segs, 1):
        pr('%-4d %-14s %-12s %-12s %6d %+8.1f%% %14s' %
           (n, '159552进攻' if leg == 0 else '512890防守', b.date(), s.date(), days,
            ret * 100, sys_name))

    # 规律统计
    pr('\n规律统计:')
    p = path.values
    pr('最优路径持仓占比: 进攻 %.0f%% / 防守 %.0f%%；系统同期: 进攻 %.0f%% / 防守 %.0f%%' %
       ((p == 0).mean() * 100, (p == 1).mean() * 100,
        (st_sys == 'atk').mean() * 100, (st_sys == 'div').mean() * 100))
    seg_days = [s[3] for s in segs]
    pr('最优段持有时长: 均值 %.1f 天, 中位 %d 天, 最短 %d 天, 最长 %d 天' %
       (np.mean(seg_days), np.median(seg_days), min(seg_days), max(seg_days)))
    diff = (r_a - r_d).abs()
    pr('两腿日收益差 >1%% 的天数: %d（%.0f%%）；这些天贡献了最优路径超额的主体' %
       ((diff > 0.01).sum(), (diff > 0.01).mean() * 100))
    # 系统持有与最优一致的比例
    agree = ((st_sys == 'atk').values == (p == 0)).mean()
    pr('系统持仓与最优路径一致的天数占比: %.0f%%' % (agree * 100))
    # 换仓敏感：成本提到千1（万2的50倍）后最优还剩多少
    nav_exp, path_exp = optimal_path(r_a, r_d, idx, 0.001)
    pr('若每次换仓成本为千1（万2的50倍）: 最优路径累计 %+8.1f%%, 换仓 %d 次' %
       ((nav_exp.iloc[-1] - 1) * 100, int((path_exp.diff() != 0).sum())))

    text = '\n'.join(out)
    print(text)
    with open('result_optimal.txt', 'w', encoding='utf-8') as f:
        f.write(text + '\n')
    print('\n已保存 result_optimal.txt')


if __name__ == '__main__':
    main()
