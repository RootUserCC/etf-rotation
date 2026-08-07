#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""主入口：读取 data/ 下 CSV，执行轮动回测"""
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


def load(name: str) -> pd.DataFrame:
    df = pd.read_csv(os.path.join(DATA_DIR, name), parse_dates=['date'])
    df = df.set_index('date').sort_index()
    df.index = df.index.normalize()   # 统一为纯日期，消除 15:00 时间戳差异
    return df


def main():
    avg = load('avg_880003.csv')
    etf1000 = load('etf_512100_hfq.csv')   # 东财后复权（含份额合并/分红调整）
    etfdiv = load('etf_510880_hfq.csv')    # 东财后复权（含分红再投资）

    # 回测起点不早于 512100 上市（2016-09）且预留 MACD 预热期
    start = etf1000.index[0] - pd.Timedelta(days=120)
    avg = avg[avg.index >= start]

    run_backtest(avg, etf1000, etfdiv, fee=0.0001)


if __name__ == '__main__':
    main()
