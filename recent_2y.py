#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""近2年回测 + 买卖点标注图（方案B：对称信号）"""
import sys
import io
import os

import pandas as pd

if sys.platform == 'win32' and not getattr(sys.stdout, '_utf8_wrapped', False):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stdout._utf8_wrapped = True

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

from backtest import run_backtest, max_drawdown, annualized

plt.rcParams['font.sans-serif'] = ['Microsoft YaHei', 'SimHei']
plt.rcParams['axes.unicode_minus'] = False

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
YEARS = 2


def load(name):
    df = pd.read_csv(os.path.join(DATA_DIR, name), parse_dates=['date'])
    df = df.set_index('date').sort_index()
    df.index = df.index.normalize()
    return df


def main():
    avg = load('avg_880003.csv')
    etf1000 = load('etf_512100_hfq.csv')
    etfdiv = load('etf_510880_hfq.csv')

    start = etf1000.index[-1] - pd.Timedelta(days=int(YEARS * 365.25))

    resB = run_backtest(avg, etf1000, etfdiv, fee=0.0001, sell_anywhere=True,
                        label='轮动策略B(对称)', start_date=start, verbose=False)
    resA = run_backtest(avg, etf1000, etfdiv, fee=0.0001, sell_anywhere=False,
                        label='轮动策略A(原版)', start_date=start, verbose=False)

    idx = resB['idx']
    print('近2年回测: %s ~ %s  共 %d 个交易日' % (idx[0].date(), idx[-1].date(), len(idx)))
    fmt = '%-18s %10s %10s'
    print(fmt % ('方案', '累计收益', '最大回撤'))
    for name, res in [('B 对称信号', resB), ('A 原版信号', resA)]:
        nav = res['nav_strat']
        print(fmt % (name, '%.2f%%' % ((nav.iloc[-1] - 1) * 100),
                     '%.2f%%' % (max_drawdown(nav) * 100)))
    for name, nav in [('1000ETF 持有', resB['nav_1000']),
                      ('红利ETF 持有', resB['nav_div']),
                      ('50/50 静态', resB['nav_half'])]:
        print(fmt % (name, '%.2f%%' % ((nav.iloc[-1] - 1) * 100),
                     '%.2f%%' % (max_drawdown(nav) * 100)))
    print('-' * 50)
    print('方案B 换仓明细（%d 次）:' % len(resB['trades']))
    for d, action, price in resB['trades']:
        print('  %s  %-22s  成交 %.3f' % (d.date(), action, price))

    # ---------- 绘图 ----------
    fig, axes = plt.subplots(3, 1, figsize=(13, 10), sharex=True,
                             gridspec_kw={'height_ratios': [1.2, 1.2, 1]})

    # 图1：512100 价格 + 买卖点
    ax = axes[0]
    p = resB['p1000_c']
    ax.plot(p.index, p.values, color='gray', lw=1.0, label='中证1000ETF(512100 后复权)')
    # 持仓1000区间底色
    hold = resB['hold1000']
    ax.fill_between(hold.index, p.min(), p.max(), where=hold.values,
                    color='red', alpha=0.08, label='持有1000ETF区间')
    for d, action, price in resB['trades']:
        if action.startswith('买入'):
            ax.scatter(d, p.loc[d], marker='^', color='red', s=90, zorder=5)
            ax.annotate('买', (d, p.loc[d]), textcoords='offset points',
                        xytext=(0, 8), ha='center', color='red', fontsize=8)
        else:
            ax.scatter(d, p.loc[d], marker='v', color='green', s=90, zorder=5)
            ax.annotate('卖', (d, p.loc[d]), textcoords='offset points',
                        xytext=(0, -14), ha='center', color='green', fontsize=8)
    ax.set_title('方案B 买卖点（信号：平均股价880003 DIF拐点；▲买入1000ETF ▼切回红利ETF）')
    ax.legend(loc='upper left', fontsize=8)
    ax.grid(alpha=0.3)

    # 图2：净值对比
    ax = axes[1]
    ax.plot(idx, resB['nav_strat'].values, color='red', lw=1.4, label='方案B 对称信号')
    ax.plot(idx, resA['nav_strat'].values, color='orange', lw=1.0, label='方案A 原版信号')
    ax.plot(idx, resB['nav_1000'].values, color='gray', lw=1.0, label='1000ETF 持有')
    ax.plot(idx, resB['nav_div'].values, color='green', lw=1.0, label='红利ETF 持有')
    for d, action, price in resB['trades']:
        nv = resB['nav_strat'].loc[d]
        ax.scatter(d, nv, marker='^' if action.startswith('买入') else 'v',
                   color='red' if action.startswith('买入') else 'green', s=50, zorder=5)
    ax.set_title('近2年净值对比（起点=1）')
    ax.legend(loc='upper left', fontsize=8)
    ax.grid(alpha=0.3)

    # 图3：平均股价 DIF + 信号
    ax = axes[2]
    sig = resB['sig']
    ax.plot(sig.index, sig['dif'].values, color='black', lw=0.9, label='平均股价 DIF')
    ax.plot(sig.index, sig['dea'].values, color='steelblue', lw=0.9, label='DEA')
    ax.axhline(0, color='gray', lw=0.6, ls='--')
    buys = sig[sig['buy_sig']]
    sells = sig[sig['sell_sig']]
    ax.scatter(buys.index, buys['dif'], marker='^', color='red', s=40, label='买入信号(DIF<0拐头)', zorder=5)
    ax.scatter(sells.index, sells['dif'], marker='v', color='green', s=40, label='卖出信号(DIF拐头)', zorder=5)
    ax.set_title('平均股价(880003) MACD-DIF 与信号')
    ax.legend(loc='upper left', fontsize=8)
    ax.grid(alpha=0.3)

    fig.tight_layout()
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'recent_2y_trades.png')
    fig.savefig(out, dpi=130)
    print('\n图已保存: %s' % out)


if __name__ == '__main__':
    main()
