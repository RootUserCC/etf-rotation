#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
KDJ 组合对比回测：MACD-DIF 拐点（基准） vs 各种 KDJ 组合口径

KDJ(9,3,3) 按通达信口径：SMA(X,N,1) = ewm(alpha=1/N, adjust=False)，
计算基于"平均股价"指数(880003)的 H/L/C，与 MACD 信号同源。

  V0 基准      ：DIF 零下拐头买 / 零上拐头卖（现行口径）
  V1 KDJ确认买 ：买点不变，但要求当日 K>D（KDJ 已金叉）才入场
  V2 KDJ提前卖 ：买点不变；卖点 = 原卖点 OR 高位死叉(CROSS(D,K) 且 K>70)
  V3 纯KDJ     ：买 = CROSS(K,D) 且 K<30（原公式的 C>MA20 过滤在指数上
                 与 K<30 互斥——回测 10 年触发 0 次，故去掉）；
                 卖 = (CROSS(D,K) 且 K>70) OR 跌破MA20
成交口径与 run_backtest 一致：T 日收盘信号，T+1 开盘成交，双边万1。
"""
import sys
import io
import os

import numpy as np
import pandas as pd

if sys.platform == 'win32' and not getattr(sys.stdout, '_utf8_wrapped', False):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stdout._utf8_wrapped = True

from backtest import run_backtest, calc_signals, annualized, max_drawdown

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
FEE = 0.0001


def load(name: str) -> pd.DataFrame:
    df = pd.read_csv(os.path.join(DATA_DIR, name), parse_dates=['date'])
    df = df.set_index('date').sort_index()
    df.index = df.index.normalize()
    return df


def tdx_sma(s: pd.Series, n: int) -> pd.Series:
    """通达信 SMA(X,N,1)"""
    return s.ewm(alpha=1.0 / n, adjust=False).mean()


def calc_kdj(avg: pd.DataFrame, n: int = 9, m1: int = 3, m2: int = 3) -> pd.DataFrame:
    """KDJ(9,3,3)，返回 K/D/J 与 20 日均线"""
    hh = avg['high'].rolling(n).max()
    ll = avg['low'].rolling(n).min()
    rsv = (avg['close'] - ll) / (hh - ll) * 100
    k = tdx_sma(rsv, m1)
    d = tdx_sma(k, m2)
    j = 3 * k - 2 * d
    return pd.DataFrame({'K': k, 'D': d, 'J': j,
                         'ma20': avg['close'].rolling(20).mean()},
                        index=avg.index)


def cross_up(a: pd.Series, b: pd.Series) -> pd.Series:
    return (a > b) & (a.shift(1) <= b.shift(1))


def cross_down(a: pd.Series, b: pd.Series) -> pd.Series:
    return (a < b) & (a.shift(1) >= b.shift(1))


def build_signals(avg: pd.DataFrame) -> dict:
    """构造 4 个版本的 buy_sig/sell_sig"""
    base = calc_signals(avg['close'])          # V0：现行 MACD DIF 拐点
    kdj = calc_kdj(avg)
    c = avg['close']

    sigs = {'V0 基准(MACD DIF拐点)': base[['buy_sig', 'sell_sig']].copy()}

    # V1：买点加 KDJ 金叉确认（K>D）
    v1 = base[['buy_sig', 'sell_sig']].copy()
    v1['buy_sig'] = base['buy_sig'] & (kdj['K'] > kdj['D'])
    sigs['V1 MACD买+KDJ金叉确认'] = v1

    # V2：卖点增加 KDJ 高位死叉
    sell_dead = cross_down(kdj['K'], kdj['D']) & (kdj['K'] > 70)
    v2 = base[['buy_sig', 'sell_sig']].copy()
    v2['sell_sig'] = base['sell_sig'] | sell_dead
    sigs['V2 MACD买+KDJ高位死叉卖'] = v2

    # V3：纯 KDJ（用户公式映射到轮动；C>MA20 与 K<30 在指数上互斥，去掉）
    buy_main = cross_up(kdj['K'], kdj['D']) & (kdj['K'] < 30)
    sell_exit = sell_dead | cross_down(c, kdj['ma20'])
    sigs['V3 纯KDJ(K<30金叉买)'] = pd.DataFrame(
        {'buy_sig': buy_main.fillna(False), 'sell_sig': sell_exit.fillna(False)},
        index=avg.index)
    return sigs


def main():
    avg = load('avg_880003.csv')
    etf1000 = load('etf_512100_hfq.csv')
    etfdiv = load('etf_510880_hfq.csv')

    start = etf1000.index[0] - pd.Timedelta(days=120)
    avg = avg[avg.index >= start]

    rows = []
    for name, sig in build_signals(avg).items():
        r = run_backtest(avg, etf1000, etfdiv, fee=FEE,
                         signals=sig, label=name, verbose=False)
        nav = r['nav_strat']
        rows.append({
            '版本': name,
            '累计收益%': round((nav.iloc[-1] - 1) * 100, 2),
            '年化%': round(annualized(nav) * 100, 2),
            '最大回撤%': round(max_drawdown(nav) * 100, 2),
            '换仓次数': len(r['trades']),
            '1000持仓%': round(r['hold1000'].mean() * 100, 1),
        })

    print('回测区间: %s ~ %s' % (r['idx'][0].date(), r['idx'][-1].date()))
    print(pd.DataFrame(rows).to_string(index=False))


if __name__ == '__main__':
    main()
