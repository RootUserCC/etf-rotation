#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
近两周 159552(2000增强) / 512890(红利低波) 来回做T 的理想 vs 现实测算

层级：
  L1 持有不动（基准）
  L2 日频完美换股：每天收盘预知明天哪条腿涨，昨收切换（日线天花板）
  L3 5分钟完美换股：每根5分钟K线都站对腿（理论极限，含日内做T）
  L4 日内完美低买高卖：每天选振幅大的腿，当日最低买最高卖
  L5 5分钟MACD规则换股（无前视）：159552的5分MACD金叉持有159552，
     死叉切512890，5分钟收盘价成交，双边万1
分钟数据：pytdx 5分钟K线（category=0）
"""
import sys
import io
import os
import time

import numpy as np
import pandas as pd

# pythonw（无窗口计划任务）下 sys.stdout 为 None，不能 reconfigure
if sys.platform == 'win32' and sys.stdout is not None and not getattr(sys.stdout, '_utf8_wrapped', False):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stdout._utf8_wrapped = True

import fetch_data as fd

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
FEE = 0.0002          # 双边万1 × 2
DAYS = 10             # 最近10个交易日


def load_daily(name):
    df = pd.read_csv(os.path.join(DATA_DIR, name), parse_dates=['date'])
    df = df.set_index('date').sort_index()
    df.index = df.index.normalize()
    return df


def fetch_5min(code, market, n_bars=DAYS * 48 + 96):
    """拉最近 n_bars 根 5 分钟K线"""
    from pytdx.hq import TdxHq_API
    api = TdxHq_API()
    fd._connect_hq(api)
    rows = []
    start = 0
    while len(rows) < n_bars:
        batch = api.get_security_bars(0, market, code, start, 800)
        if not batch:
            break
        rows = batch + rows
        if len(batch) < 800:
            break
        start += len(batch)
        time.sleep(0.3)
    api.disconnect()
    df = pd.DataFrame(rows)
    df['dt'] = pd.to_datetime(df['datetime'])
    return df.set_index('dt')[['open', 'high', 'low', 'close', 'vol']].sort_index()


def ema(s, n):
    return s.ewm(alpha=2.0 / (n + 1), adjust=False).mean()


def pct(x):
    return '%.2f%%' % (x * 100)


def main():
    a = load_daily('etf_159552_hfq.csv')
    b = load_daily('etf_512890_hfq.csv')
    idx = a.index.intersection(b.index)[-(DAYS + 1):]
    ra = a.loc[idx, 'close'].pct_change()
    rb = b.loc[idx, 'close'].pct_change()

    print('=' * 60)
    print('区间: %s ~ %s（%d 个交易日）' % (idx[1].date(), idx[-1].date(), DAYS))
    print('=' * 60)

    # L1 持有不动
    hold_a = a.loc[idx[-1], 'close'] / a.loc[idx[0], 'close'] - 1
    hold_b = b.loc[idx[-1], 'close'] / b.loc[idx[0], 'close'] - 1
    print('L1 持有不动        : 2000增强 %s / 红利低波 %s' % (pct(hold_a), pct(hold_b)))

    # L2 日频完美换股
    best_daily = pd.concat([ra, rb], axis=1, sort=True).max(axis=1).dropna()
    l2 = (1 + best_daily).prod() - 1
    print('L2 日频完美换股    : %s（每天收盘换股，预知次日涨跌）' % pct(l2))

    # L4 日内完美低买高卖（基于日线 high/low）
    swing = pd.concat([a['high'] / a['low'], b['high'] / b['low']],
                      axis=1, sort=True).loc[idx[1:]].max(axis=1)
    l4 = swing.prod() - 1
    print('L4 日内完美做T     : %s（每天选振幅大的腿，最低买最高卖）' % pct(l4))

    # ---- 5分钟数据 ----
    print('-' * 60)
    print('拉取 5 分钟数据...')
    m_a = fetch_5min('159552', 0)   # 深市
    m_b = fetch_5min('512890', 1)   # 沪市
    common = m_a.index.intersection(m_b.index)
    start_dt = pd.Timestamp(idx[1])
    common = common[common >= start_dt]
    pa = m_a.loc[common, 'close']
    pb = m_b.loc[common, 'close']
    print('5分钟K线: %d 根，%s ~ %s' % (len(common), common[0], common[-1]))

    # L3 5分钟完美换股（理论极限）
    r5a = pa.pct_change()
    r5b = pb.pct_change()
    best5 = pd.concat([r5a, r5b], axis=1, sort=True).max(axis=1).dropna()
    l3 = (1 + best5).prod() - 1
    print('L3 5分钟完美换股   : %s（理论极限，每根K线都站对）' % pct(l3))

    # L5 5分钟MACD规则换股（无前视，信号当根收盘成交）
    dif = ema(pa, 12) - ema(pa, 26)
    dea = ema(dif, 9)
    golden = (dif > dea) & (dif.shift(1) <= dea.shift(1))
    dead = (dif < dea) & (dif.shift(1) >= dea.shift(1))

    n = len(common)
    hold_a_arr = np.zeros(n, dtype=bool)
    state = False          # 起始持有红利低波
    switches = 0
    for i in range(n):
        if golden.iloc[i] and not state:
            state = True
            switches += 1
        elif dead.iloc[i] and state:
            state = False
            switches += 1
        hold_a_arr[i] = state
    hold_s = pd.Series(hold_a_arr, index=common)
    strat_r = np.where(hold_s, r5a, r5b)
    strat_r = pd.Series(strat_r, index=common).fillna(0.0)
    l5_gross = (1 + strat_r).prod() - 1
    l5_net = l5_gross - switches * FEE
    print('L5 5分MACD规则换股 : 毛收益 %s，换股 %d 次，扣费后 %s'
          % (pct(l5_gross), switches, pct(l5_net)))
    print('-' * 60)
    print('规则换股捕获率（扣费后 / 5分钟理论极限）: %.1f%%'
          % (l5_net / l3 * 100 if l3 else float('nan')))


if __name__ == '__main__':
    main()
