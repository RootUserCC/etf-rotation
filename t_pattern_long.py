#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
R1~R5 切换规则的长窗口验证（5分钟K线，拉取通达信全部可用历史）

- 数据缓存到 data/etf_159552_5min.csv / etf_512890_5min.csv
- 口径：信号当根5分钟收盘成交；换股成本两档：万1双边(0.02%)/保守千一(0.1%)
- 输出：全窗口汇总、按月拆分、按行情类型拆分（按159552当日涨跌分
  大涨>1%/大跌<-1%/震荡三类）
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
from t_pattern import rsi, intraday_vwap, run_rule
from t_ideal import ema

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
FEES = {'万1双边': 0.0002, '千一保守': 0.001}


def fetch_5min_all(code, market, cache):
    """拉全部可用5分钟历史并缓存；缓存存在则直接用"""
    if os.path.exists(cache):
        df = pd.read_csv(cache, parse_dates=['dt']).set_index('dt')
        return df
    from pytdx.hq import TdxHq_API
    api = TdxHq_API()
    fd._connect_hq(api)
    rows = []
    start = 0
    while True:
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
    df = df.set_index('dt')[['open', 'high', 'low', 'close', 'vol']].sort_index()
    df = df[~df.index.duplicated(keep='last')]
    df.to_csv(cache)
    return df


def build_rules(pa, pb):
    ratio = pa / pb
    dif = ema(ratio, 12) - ema(ratio, 26)
    dea = ema(dif, 9)
    difa = ema(pa, 12) - ema(pa, 26)
    deaa = ema(difa, 9)
    return {
        'R1 比值MACD': (dif > dea),
        'R2 比值>MA20': (ratio > ratio.rolling(20).mean()),
        'R3 比值RSI>50': (rsi(ratio) > 50),
        'R4 比值>日内VWAP': (ratio > intraday_vwap(ratio)),
        'R5 单腿MACD': (difa > deaa),
    }


def perf(rules, r5a, r5b, label):
    """一组规则在指定切片上的表现，返回 dict 列表"""
    rows = []
    base_a = (1 + r5a.fillna(0)).prod() - 1
    base_b = (1 + r5b.fillna(0)).prod() - 1
    rows.append({'分组': label, '规则': '— 持有2000增强', '毛收益': base_a,
                 '换股': 0, '净@万1': base_a, '净@千一': base_a})
    rows.append({'分组': label, '规则': '— 持有红利低波', '毛收益': base_b,
                 '换股': 0, '净@万1': base_b, '净@千一': base_b})
    for name, cond in rules.items():
        hold = cond.fillna(False)
        g, sw, _, _ = run_rule(hold, r5a, r5b)
        rows.append({'分组': label, '规则': name, '毛收益': g, '换股': sw,
                     '净@万1': g - sw * FEES['万1双边'],
                     '净@千一': g - sw * FEES['千一保守']})
    return rows


def show(rows):
    df = pd.DataFrame(rows)
    for c in ['毛收益', '净@万1', '净@千一']:
        df[c] = (df[c] * 100).round(2).astype(str) + '%'
    print(df.to_string(index=False))


def main():
    print('拉取/读取 5 分钟全量历史...')
    m_a = fetch_5min_all('159552', 0, os.path.join(DATA_DIR, 'etf_159552_5min.csv'))
    m_b = fetch_5min_all('512890', 1, os.path.join(DATA_DIR, 'etf_512890_5min.csv'))
    common = m_a.index.intersection(m_b.index)
    pa, pb = m_a.loc[common, 'close'], m_b.loc[common, 'close']
    r5a, r5b = pa.pct_change(), pb.pct_change()
    days = pd.Series(common.date).nunique()
    print('区间: %s ~ %s，共 %d 个交易日 / %d 根K线'
          % (common[0].date(), common[-1].date(), days, len(common)))

    rules = build_rules(pa, pb)

    print('\n========== 全窗口汇总 ==========')
    show(perf(rules, r5a, r5b, '全窗口'))

    # 按月拆分
    print('\n========== 按月拆分（毛收益 | 换股 | 净@万1）==========')
    month = pd.Series(common.strftime('%Y-%m'), index=common)
    for m in sorted(month.unique()):
        mask = (month == m).values
        sub = {k: v[mask] for k, v in rules.items()}
        print('--- %s ---' % m)
        show(perf(sub, r5a[mask], r5b[mask], m))

    # 按行情类型拆分（按159552当日涨跌幅分类）
    print('\n========== 按行情类型拆分 ==========')
    day_key = pd.Series(common.date, index=common)
    day_ret = pa.groupby(day_key).last() / pa.groupby(day_key).first() - 1
    cls = day_ret.map(lambda x: '大涨日(>+1%)' if x > 0.01 else ('大跌日(<-1%)' if x < -0.01 else '震荡日'))
    for c in ['大涨日(>+1%)', '大跌日(<-1%)', '震荡日']:
        days_in = cls[cls == c].index
        mask = day_key.isin(days_in).values
        if not mask.any():
            continue
        sub = {k: v[mask] for k, v in rules.items()}
        print('--- %s，共 %d 天 ---' % (c, len(days_in)))
        show(perf(sub, r5a[mask], r5b[mask], c))


if __name__ == '__main__':
    main()
