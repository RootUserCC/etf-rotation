#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
防守腿多标的动量择优回测（510880 红利 / 512890 红利低波 / 159201 自由现金流）

成交口径与 compare_variants.dual_defense_backtest 完全一致：
  - 信号 T 日收盘产生（calc_signals, MACD 17/34/9, sell_anywhere=True），T+1 开盘成交
  - 换仓日收益 = 旧仓隔夜段(昨收→今开) × 新仓日内段(今开→今收) × (1-2*fee)
  - 初始持有防守仓（按首日动量选取，不足窗口取列表首只）
  - 每次切入防守仓时，比较各候选近20日涨幅选高者；防守期间不换仓
对比：
  1) 长窗口(2019起)：固定512890 / 固定510880 / 双择优
  2) 短窗口(159201上市起)：固定512890 / 双择优(880/890) / 三择优(880/890/159201)
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

from backtest import calc_signals
from compare_variants import (load, fetch_etf_hfq_auto, metrics, print_table,
                              FEE, FAST, SLOW, SIG_N, MOM_N)


def multi_defense_backtest(avg, atk, defs, start_date, mom_n=MOM_N, label='轮动'):
    """防守腿 N 标的 20日动量择优。defs: [(名称, df), ...]，单只即固定防守。"""
    sig = calc_signals(avg['close'], sell_anywhere=True,
                       fast=FAST, slow=SLOW, sig_n=SIG_N)
    idx = atk.index.intersection(sig.index)
    for _, d in defs:
        idx = idx.intersection(d.index)
    start_date = pd.Timestamp(start_date)
    idx = idx[idx >= start_date]
    sig = sig.loc[idx]

    o_atk, c_atk = atk.loc[idx, 'open'], atk.loc[idx, 'close']
    o_defs = [d.loc[idx, 'open'] for _, d in defs]
    c_defs = [d.loc[idx, 'close'] for _, d in defs]
    moms = [d['close'] / d['close'].shift(mom_n) - 1 for _, d in defs]

    def pick_def(sig_day):
        """信号日收盘后按近 mom_n 日涨幅选防守标的，返回下标"""
        best, best_m = 0, np.nan
        for j, mom in enumerate(moms):
            m = mom.get(sig_day, np.nan)
            if pd.isna(m):
                continue
            if pd.isna(best_m) or m > best_m:
                best, best_m = j, m
        return best

    n = len(idx)
    state_atk = False
    cur = pick_def(idx[0])
    pending = None
    trades = []
    choice_log = []
    hold_atk = np.zeros(n, dtype=bool)
    hold_j = np.zeros(n, dtype=int)

    for i in range(n):
        if pending is not None:
            if pending == 'buy' and not state_atk:
                state_atk = True
                trades.append((idx[i], '买入进攻/卖出防守', float(o_atk.iloc[i]), None))
            elif pending == 'sell' and state_atk:
                state_atk = False
                cur = pick_def(idx[i - 1])
                trades.append((idx[i], '卖出进攻/买入防守',
                               float(o_defs[cur].iloc[i]), defs[cur][0]))
                choice_log.append((idx[i], defs[cur][0],
                                   [float(m.get(idx[i - 1], np.nan)) for m in moms]))
            pending = None
        hold_atk[i] = state_atk
        hold_j[i] = cur
        if sig['buy_sig'].iloc[i]:
            pending = 'buy'
        elif sig['sell_sig'].iloc[i]:
            pending = 'sell'

    switch_at = {d: a for d, a, _, _ in trades}
    wealth = [1.0]
    for i in range(1, n):
        d = idx[i]
        if d not in switch_at:
            c = c_atk if hold_atk[i] else c_defs[hold_j[i]]
            w = wealth[-1] * float(c.iloc[i]) / float(c.iloc[i - 1])
        else:
            a = switch_at[d]
            if a.startswith('买入进攻'):
                j = hold_j[i]
                w = (wealth[-1] * float(o_defs[j].iloc[i]) / float(c_defs[j].iloc[i - 1])
                     * float(c_atk.iloc[i]) / float(o_atk.iloc[i]) * (1 - 2 * FEE))
            else:
                j = hold_j[i]
                w = (wealth[-1] * float(o_atk.iloc[i]) / float(c_atk.iloc[i - 1])
                     * float(c_defs[j].iloc[i]) / float(o_defs[j].iloc[i]) * (1 - 2 * FEE))
        wealth.append(w)
    nav = pd.Series(wealth, index=idx)
    return {'nav': nav, 'trades': trades, 'idx': idx, 'choice_log': choice_log, 'label': label}


def row_of(res, label):
    cum, ann, mdd = metrics(res['nav'])
    return (label, cum, ann, mdd, len(res['trades']))


def buy_hold(df, idx, name):
    nav = df.loc[idx, 'close']
    nav = nav / nav.iloc[0]
    cum, ann, mdd = metrics(nav)
    return (name + ' 买入持有', cum, ann, mdd, '-')


def main():
    avg = load('avg_880003.csv')
    atk = load('etf_512100_hfq.csv')
    d890 = load('etf_512890_hfq.csv')
    d201 = load('etf_159201_hfq.csv')
    print('在线拉取 510880 最新后复权...')
    d880 = fetch_etf_hfq_auto('510880')
    print('510880: %s ~ %s\n' % (d880.index[0].date(), d880.index[-1].date()))

    DEFS3 = [('510880', d880), ('512890', d890), ('159201', d201)]

    # ============ 1) 长窗口：2019-01-18 起（159201 未上市，只放 880/890） ============
    start1 = '2019-01-18'
    defs2 = DEFS3[:2]
    rows = []
    rows.append(row_of(multi_defense_backtest(avg, atk, defs2[1:], start1),
                       '防守=512890 固定(现有)'))
    rows.append(row_of(multi_defense_backtest(avg, atk, defs2[:1], start1),
                       '防守=510880 固定'))
    res2 = multi_defense_backtest(avg, atk, defs2, start1)
    rows.append(row_of(res2, '防守=880/890 20日动量择优'))
    win = res2['idx']
    rows.append(buy_hold(d890, win, '512890'))
    rows.append(buy_hold(d880, win, '510880'))
    rows.append(buy_hold(atk, win, '512100'))
    print_table('1) 长窗口 %s ~ %s（进攻=512100）' % (win[0].date(), win[-1].date()), rows,
                notes=['择优：切入防守时按近20日涨幅选高者，防守期间不换仓；双边万1'])
    if res2['choice_log']:
        print('择优明细（880/890，切入防守时的选择）:')
        for d, name, ms in res2['choice_log']:
            print('  %s  选中 %s   (880近20日 %s, 890近20日 %s)'
                  % (d.date(), name,
                     'NA' if pd.isna(ms[0]) else '%+.2f%%' % (ms[0] * 100),
                     'NA' if pd.isna(ms[1]) else '%+.2f%%' % (ms[1] * 100)))
        print()

    # ============ 2) 短窗口：159201 上市起，三选一 ============
    start2 = d201.index[0]
    rows = []
    rows.append(row_of(multi_defense_backtest(avg, atk, DEFS3[1:2], start2),
                       '防守=512890 固定(现有)'))
    rows.append(row_of(multi_defense_backtest(avg, atk, DEFS3[:2], start2),
                       '防守=880/890 双择优'))
    res3 = multi_defense_backtest(avg, atk, DEFS3, start2)
    rows.append(row_of(res3, '防守=880/890/159201 三择优'))
    win = res3['idx']
    rows.append(buy_hold(d890, win, '512890'))
    rows.append(buy_hold(d880, win, '510880'))
    rows.append(buy_hold(d201, win, '159201'))
    rows.append(buy_hold(atk, win, '512100'))
    span_days = (win[-1] - win[0]).days
    print_table('2) 短窗口 %s ~ %s（159201 上市起，进攻=512100）'
                % (win[0].date(), win[-1].date()), rows,
                notes=['*** 窗口仅约 %d 个月，样本太短仅供参考 ***' % round(span_days / 30.4)])
    if res3['choice_log']:
        print('择优明细（三选一，切入防守时的选择）:')
        for d, name, ms in res3['choice_log']:
            fmt = lambda m: 'NA' if pd.isna(m) else '%+.2f%%' % (m * 100)
            print('  %s  选中 %-6s (880 %s, 890 %s, 159201 %s)'
                  % (d.date(), name, fmt(ms[0]), fmt(ms[1]), fmt(ms[2])))


if __name__ == '__main__':
    main()
