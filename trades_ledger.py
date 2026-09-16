#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""方案C 理想执行台账：2019-01-18 起每一次持仓段明细 + 统计规律

口径：方案C信号（880003 MACD 17/34/9 拐点，零下买/任意卖），进攻仓 512100→159552
于 2024-06-28 拼接，防守仓 512890；T+1 开盘成交，持仓段收益=换仓开盘价对开盘价，
扣双边万1（与 export_json 的 legs 口径一致）；首日视作收盘建仓防守仓。
输出 result_trades.txt：全量台账 + 统计。
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
                             SWITCH_ATK, START, FEE)
from export_json import ATK_NAME_OLD, ATK_NAME_NEW


def main():
    avg = load('avg_880003.csv')
    etf1000 = load('etf_512100_hfq.csv')
    etf2000e = load('etf_159552_hfq.csv')
    etfdiv = load('etf_512890_hfq.csv')
    sig_full = calc_signals(avg['close'], sell_anywhere=True, fast=17, slow=34, sig_n=9)

    atk_o = pd.concat([etf1000['open'][etf1000.index < SWITCH_ATK],
                       etf2000e['open'][etf2000e.index >= SWITCH_ATK]])
    atk_c = pd.concat([etf1000['close'][etf1000.index < SWITCH_ATK],
                       etf2000e['close'][etf2000e.index >= SWITCH_ATK]])
    idx = etfdiv.index.intersection(atk_c.index).intersection(avg.index)
    idx = idx[idx >= START]

    on_a, id_a, cc_a = leg_returns(atk_o, atk_c)
    on_d, id_d, cc_d = leg_returns(etfdiv['open'], etfdiv['close'])
    on1, id1, cc1 = leg_returns(etf1000['open'], etf1000['close'])
    on_a.loc[SWITCH_ATK], id_a.loc[SWITCH_ATK], cc_a.loc[SWITCH_ATK] = \
        on1.loc[SWITCH_ATK], id1.loc[SWITCH_ATK], cc1.loc[SWITCH_ATK]
    legs_ret = {'atk': (on_a.loc[idx].fillna(0), id_a.loc[idx].fillna(0), cc_a.loc[idx].fillna(0)),
                'div': (on_d.loc[idx].fillna(0), id_d.loc[idx].fillna(0), cc_d.loc[idx].fillna(0))}
    nav, st, _ = run_variant(idx, sig_full.loc[idx], legs_ret, etfdiv['close'].loc[idx],
                             np.full(len(idx), np.nan), exec_mode='open', use_cash=False)

    o = {'atk': atk_o.loc[idx], 'div': etfdiv['open'].loc[idx]}
    c = {'atk': atk_c.loc[idx], 'div': etfdiv['close'].loc[idx]}
    sig = sig_full.loc[idx]

    # ---- 持仓段台账：状态变化点即换仓点（T+1开盘价成交）----
    legs = []
    leg_start_i = 0
    leg = 'div'                       # 首日收盘建仓防守仓
    entry_px = float(c['div'].iloc[0])
    for i in range(1, len(idx)):
        if st.iloc[i] != leg:         # 今开换仓：旧仓今开了结，新仓今开建仓
            legs.append(dict(leg=leg, b=idx[leg_start_i], s=idx[i],
                             bpx=entry_px, spx=float(o[leg].iloc[i]), days=i - leg_start_i))
            leg = st.iloc[i]
            leg_start_i = i
            entry_px = float(o[leg].iloc[i])
    legs.append(dict(leg=leg, b=idx[leg_start_i], s=None,
                     bpx=entry_px, spx=float(c[leg].iloc[-1]), days=len(idx) - leg_start_i))

    for L in legs:
        L['ret'] = L['spx'] / L['bpx'] - 1 - 2 * FEE
        L['name'] = ({'atk': ATK_NAME_OLD if L['b'] < SWITCH_ATK else ATK_NAME_NEW,
                      'div': '红利低波512890'})[L['leg']]
        # 建仓前一交易日（信号日）的 DIF
        si = max(idx.searchsorted(L['b']) - 1, 0)
        L['dif'] = float(sig['dif'].iloc[si])

    out = []
    pr = out.append
    pr('方案C 理想执行台账（%s ~ %s）' % (idx[0].date(), idx[-1].date()))
    pr('口径：严格信号、T+1开盘成交、开盘价对开盘价、双边万1，无手动干预')
    pr('=' * 90)
    pr('%-4s %-14s %-12s %-12s %6s %9s %9s %9s' %
       ('#', '标的', '买入日', '卖出日', '持有天', '买价', '卖价', '段收益'))
    cum = 1.0
    for n, L in enumerate(legs, 1):
        cum *= 1 + L['ret']
        pr('%-4d %-14s %-12s %-12s %6d %9.3f %9.3f %+8.1f%%  累计 %+7.1f%%' %
           (n, L['name'], L['b'].date(), L['s'].date() if L['s'] is not None else '持有中',
            L['days'], L['bpx'], L['spx'], L['ret'] * 100, (cum - 1) * 100))

    # ---- 统计 ----
    A = pd.DataFrame([L for L in legs if L['leg'] == 'atk'])
    D = pd.DataFrame([L for L in legs if L['leg'] == 'div'])

    def stat(df, name):
        r = df['ret']
        wins = r[r > 0]
        pr('\n【%s】 %d 段：胜率 %.0f%%，均值 %+.1f%%，中位 %+.1f%%，'
           '平均持有 %.0f 天（中位 %d 天）' %
           (name, len(df), (r > 0).mean() * 100, r.mean() * 100, r.median() * 100,
            df['days'].mean(), df['days'].median()))
        pr('  盈利段 %d 个：均值 %+.1f%%；亏损段 %d 个：均值 %+.1f%%；盈亏比 %.1f' %
           (len(wins), wins.mean() * 100 if len(wins) else 0,
            len(r) - len(wins), r[r <= 0].mean() * 100 if (r <= 0).any() else 0,
            abs(wins.mean() / r[r <= 0].mean()) if (r <= 0).any() and len(wins) else float('nan')))
        pr('  最大单笔盈利 %+.1f%%，最大单笔亏损 %+.1f%%' % (r.max() * 100, r.min() * 100))

    pr('\n' + '=' * 90)
    pr('统计规律')
    stat(A, '进攻仓持仓段')
    stat(D, '防守仓持仓段')

    # 利润集中度：前5大盈利段贡献
    allr = pd.concat([A, D])
    top5 = allr['ret'].nlargest(5).sum()
    tot = allr['ret'].sum()
    pr('\n利润集中度：全部 %d 段算术收益合计 %+.0f%%，其中前 5 大盈利段合计 %+.0f%%（%.0f%%）' %
       (len(allr), tot * 100, top5 * 100, top5 / tot * 100))

    # 短持仓（打脸段）：<=5 天
    for df, name in ((A, '进攻仓'), (D, '防守仓')):
        s = df[df['days'] <= 5]
        if len(s):
            pr('%s 持有≤5天的段：%d 个（占 %.0f%%），平均收益 %+.1f%%，合计 %+.1f%%' %
               (name, len(s), len(s) / len(df) * 100, s['ret'].mean() * 100, s['ret'].sum() * 100))

    # 进攻仓买点 DIF 深度 vs 段收益
    pr('\n进攻仓买入信号日 DIF 深度 vs 该段收益（DIF 越深=市场跌得越惨时抄底）:')
    q = pd.qcut(A['dif'], 3, labels=['DIF最深1/3', '中间1/3', 'DIF最浅1/3'])
    for k, g in A.groupby(q, observed=True):
        pr('  %-10s %2d 段：胜率 %.0f%%，平均段收益 %+.1f%%，平均DIF %.3f' %
           (k, len(g), (g['ret'] > 0).mean() * 100, g['ret'].mean() * 100, g['dif'].mean()))

    # 持有时长分组
    pr('\n进攻仓持仓时长分组:')
    bins = [0, 5, 15, 40, 10 ** 9]
    labels = ['≤5天', '6~15天', '16~40天', '>40天']
    for (lo, hi), lb in zip(zip(bins, bins[1:]), labels):
        g = A[(A['days'] > lo) & (A['days'] <= hi)]
        if len(g):
            pr('  %-8s %2d 段：胜率 %.0f%%，平均 %+.1f%%，合计 %+.0f%%' %
               (lb, len(g), (g['ret'] > 0).mean() * 100, g['ret'].mean() * 100,
                g['ret'].sum() * 100))

    # 分年度换仓次数与收益
    pr('\n分年度：换仓次数 / 当年策略收益')
    y_nav = nav.resample('YE').last()
    y_ret = y_nav.pct_change()
    y_ret.iloc[0] = y_nav.iloc[0] / nav.iloc[0] - 1
    for y in sorted(set(d.year for d in idx)):
        n_sw = sum(1 for L in legs if L['b'].year == y)
        yr = y_ret[y_ret.index.year == y]
        if len(yr):
            pr('  %d: 建仓 %2d 次，策略收益 %+6.1f%%' % (y, n_sw, yr.iloc[0] * 100))

    text = '\n'.join(out)
    print(text)
    with open('result_trades.txt', 'w', encoding='utf-8') as f:
        f.write(text + '\n')
    print('\n已保存 result_trades.txt')


if __name__ == '__main__':
    main()
