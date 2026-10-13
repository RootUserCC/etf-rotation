#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""四色柱状态跟随（红黄柱=进攻 / 粉绿柱=防守） vs 方案C基准
规则：MACD(12,26,9) 柱子每日定色——
  粉=零上缩头(m>0 且 m<前 且 前>0)；黄=零下缩小(m<0 且 m>前 且 前<0)；
  其余零上=红，其余零下=绿。红/黄=持有进攻仓，粉/绿=持有防守仓，状态翻转即信号。
变体A=柱子源为进攻腿自身(512100→159552拼接)；变体B=柱子源为880003平均股价。
执行口径: T日收盘定色 → T+1开盘成交，双边万1，经 run_backtest(signals=) 注入。
"""
import sys, io
import numpy as np
import pandas as pd

if sys.platform == 'win32' and not getattr(sys.stdout, '_utf8_wrapped', False):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stdout._utf8_wrapped = True

from backtest import run_backtest, ema, annualized, max_drawdown

SWITCH = pd.Timestamp('2024-06-28')
FEE = 0.0001


def load(p):
    df = pd.read_csv('data/' + p, parse_dates=['date']).set_index('date').sort_index()
    df.index = df.index.normalize()
    return df


def bar4_signals(close: pd.Series) -> pd.DataFrame:
    """四色柱状态 → 点信号（状态翻转日置位）"""
    dif = ema(close, 12) - ema(close, 26)
    dea = ema(dif, 9)
    m = (dif - dea) * 2
    m_prev = m.shift(1)
    pink = (m > 0) & (m < m_prev) & (m_prev > 0)      # 零上缩头
    yellow = (m < 0) & (m > m_prev) & (m_prev < 0)    # 零下缩小
    red = (m >= 0) & ~pink
    # green = (m < 0) & ~yellow
    attack = (red | yellow).fillna(False)             # 红/黄=进攻
    prev = attack.shift(1).fillna(False)
    df = pd.DataFrame({'buy_sig': attack & ~prev, 'sell_sig': ~attack & prev},
                      index=close.index)
    return df, attack


def whipsaw(res):
    """≤5个交易日且亏损的段数与合计收益（用净值段近似，含费用）"""
    nav, trades = res['nav_strat'], res['trades']
    cnt, total = 0, 0.0
    for i in range(0, len(trades) - 1, 2):
        b, s = trades[i][0], trades[i + 1][0]
        days = len(res['idx'][(res['idx'] >= b) & (res['idx'] <= s)])
        ret = nav.loc[s] / nav.loc[b] - 1
        if days <= 5 and ret < 0:
            cnt += 1
            total += ret
    return cnt, total


def metrics(res, label):
    nav = res['nav_strat']
    cum = nav.iloc[-1] - 1
    y26 = nav.loc['2026-01-01':]
    ytd26 = y26.iloc[-1] / nav.loc[:'2025-12-31'].iloc[-1] - 1
    sep = nav.loc['2026-09-01':'2026-09-30']
    sep_ret = sep.iloc[-1] / nav.loc[:'2026-08-31'].iloc[-1] - 1 if len(sep) else float('nan')
    wp_n, wp_s = whipsaw(res)
    cross = bool(res['hold1000'].loc[:SWITCH].iloc[-1]) if (res['idx'] <= SWITCH).any() else '?'
    print('%-26s %8.2f%% %8.2f%% %8.2f%% %6d %9.2f%% %9.2f%%   %2d/%7.2f%%   %s' % (
        label, cum * 100, annualized(nav) * 100, max_drawdown(nav) * 100,
        len(res['trades']), ytd26 * 100, sep_ret * 100, wp_n, wp_s * 100,
        '是!' if cross is True else '否'))
    yearly = nav.resample('YE').last()
    yr = yearly.pct_change()
    yr.iloc[0] = yearly.iloc[0] / nav.iloc[0] - 1
    return {y: r for y, r in zip(yearly.index.year, yr)}


def main():
    avg = load('avg_880003.csv')
    e1000 = load('etf_512100_hfq.csv')
    e2000 = load('etf_159552_hfq.csv')
    ediv = load('etf_512890_hfq.csv')
    etf_atk = pd.concat([e1000[e1000.index < SWITCH], e2000[e2000.index >= SWITCH]])

    # 基准锚点：方案C
    base = run_backtest(avg, etf_atk, ediv, fee=FEE, sell_anywhere=True,
                        fast=17, slow=34, sig_n=9, verbose=False)

    # 变体A：柱子源=进攻腿自身；变体B：柱子源=880003
    sigA, stateA = bar4_signals(etf_atk['close'])
    sigB, stateB = bar4_signals(avg['close'])
    resA = run_backtest(avg, etf_atk, ediv, fee=FEE, signals=sigA, verbose=False)
    resB = run_backtest(avg, etf_atk, ediv, fee=FEE, signals=sigB, verbose=False)

    print('回测区间: %s ~ %s  共 %d 个交易日' % (
        base['idx'][0].date(), base['idx'][-1].date(), len(base['idx'])))
    print('口径: 四色柱(12,26,9)状态跟随 红/黄=进攻 粉/绿=防守; T收盘定色 T+1开盘成交 双边万1')
    print('=' * 108)
    print('%-26s %10s %10s %10s %6s %11s %11s %14s %6s' % (
        '方案', '累计收益', '年化', '最大回撤', '换仓', '2026YTD', '2026-09月', '打脸段', '跨界持有'))
    yb = metrics(base, '基准 方案C(880003 DIF拐点)')
    ya = metrics(resA, 'A 四色柱@进攻腿自身')
    y2 = metrics(resB, 'B 四色柱@880003')

    print('\n分年度收益(%):')
    years = sorted(yb)
    print('%-26s' % '方案' + ''.join('%9d' % y for y in years))
    for label, ys in [('基准 方案C', yb), ('A 四色柱@进攻腿', ya), ('B 四色柱@880003', y2)]:
        print('%-26s' % label + ''.join('%9.2f' % (ys[y] * 100) for y in years))

    for name, st, res in [('A@进攻腿', stateA, resA), ('B@880003', stateB, resB)]:
        print('%s: 进攻状态天数占比 %.1f%%，实际换仓 %d 次'
              % (name, st.loc[res['idx']].mean() * 100, len(res['trades'])))


if __name__ == '__main__':
    main()
