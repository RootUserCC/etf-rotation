#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对比原版信号（零下不卖）与对称版信号（DIF转降即切红利）"""
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

from backtest import run_backtest

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')


def load(name):
    df = pd.read_csv(os.path.join(DATA_DIR, name), parse_dates=['date'])
    df = df.set_index('date').sort_index()
    df.index = df.index.normalize()
    return df


def main():
    avg = load('avg_880003.csv')
    etf1000 = load('etf_512100_hfq.csv')
    etfdiv = load('etf_510880_hfq.csv')
    start = etf1000.index[0] - pd.Timedelta(days=120)
    avg = avg[avg.index >= start]

    print('\n########## 方案A：原版信号（买入 DIF<0 拐头，卖出 DIF>0 拐头） ##########\n')
    run_backtest(avg, etf1000, etfdiv, fee=0.0001,
                 sell_anywhere=False, label='轮动策略A(原版)')

    print('\n\n########## 方案B：对称信号（买入 DIF<0 拐头，卖出 DIF 任意位置拐头） ##########\n')
    run_backtest(avg, etf1000, etfdiv, fee=0.0001,
                 sell_anywhere=True, label='轮动策略B(对称)')


if __name__ == '__main__':
    main()
