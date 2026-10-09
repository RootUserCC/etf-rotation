#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
全量扫描深圳 A 股，找出 2026-01-01 以来收盘涨停过的股票。
- 数据通道：复用 fetch_data._connect_hq / _bars_to_df（pytdx 通达信）
- 输出：data/limitup_stocks/sz_<code>.csv（命中股票完整日线）、
        data/limitup_events_sz.csv（事件清单 code,date,pct）
"""
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd
from pytdx.hq import TdxHq_API

import fetch_data as fd

EVENT_SINCE = pd.Timestamp('2026-01-01')
CODE_PREFIXES = ('000', '001', '002', '003', '300', '301')
OUT_DIR = os.path.join(fd.DATA_DIR, 'limitup_stocks')
EVENTS_CSV = os.path.join(fd.DATA_DIR, 'limitup_events_sz.csv')


def list_sz_stocks(api):
    stocks = []
    start = 0
    while True:
        batch = api.get_security_list(0, start)
        if not batch:
            break
        stocks.extend(batch)
        if len(batch) < 1000:
            break
        start += 1000
    out = []
    for s in stocks:
        code = str(s.get('code', ''))
        name = str(s.get('name', ''))
        if not code.startswith(CODE_PREFIXES):
            continue
        if 'ST' in name.upper() or '退' in name:
            continue
        out.append(code)
    return out


def probe_chinext_codes():
    """清单接口被服务器截断在 6000 条（300/301 创业板取不到），
    直接按号段 300001-301999 补全；不存在的代码取 K 线返回空，自然跳过。
    注意：创业板补全部分无法拿到名称，未做 ST/退 过滤（退市股无 K 线会自然跳过）。"""
    return ['%06d' % n for n in range(300001, 302000)]


def find_limitup_events(code, df):
    close = df['close']
    high = df['high']
    prev_close = close.shift(1)
    limit = 0.20 if code.startswith('30') else 0.10
    pos = np.arange(len(df))
    mask = (
        (close / prev_close - 1 >= limit - 0.003)
        & (close >= high - 0.001)
        & (pos >= 20)
        & (df.index >= EVENT_SINCE)
    )
    events = []
    for dt, c, pc in zip(df.index[mask], close[mask], prev_close[mask]):
        events.append((code, dt.strftime('%Y-%m-%d'), round(c / pc - 1, 4)))
    return events


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    api = TdxHq_API()
    fd._connect_hq(api)
    scanned = 0
    skipped = 0
    hit_codes = 0
    all_events = []
    try:
        codes = list_sz_stocks(api)
        print('主板清单：%d 只（已过滤非 000/001/002/003 及 ST/退）' % len(codes))
        chinext = probe_chinext_codes()
        codes = codes + chinext
        print('补全创业板号段 300001-301999：%d 个探测位，合计 %d 个' % (len(chinext), len(codes)))
        for i, code in enumerate(codes, 1):
            if i % 200 == 0:
                print('进度 %d/%d：扫描 %d 跳过 %d 命中 %d 事件 %d'
                      % (i, len(codes), scanned, skipped, hit_codes, len(all_events)))
            if i % 500 == 0:
                time.sleep(1)
            try:
                rows = api.get_security_bars(9, 0, code, 0, 320)
            except Exception as e:
                skipped += 1
                print('  %s get_security_bars 异常跳过：%s' % (code, e))
                continue
            if not rows or len(rows) < 100:
                skipped += 1
                continue
            scanned += 1
            df = fd._bars_to_df(rows, ['open', 'high', 'low', 'close'])
            if len(df) < 100:
                skipped += 1
                continue
            events = find_limitup_events(code, df)
            if events:
                hit_codes += 1
                all_events.extend(events)
                df.to_csv(os.path.join(OUT_DIR, 'sz_%s.csv' % code),
                          index_label='date', encoding='utf-8-sig')
    finally:
        api.disconnect()

    ev_df = pd.DataFrame(all_events, columns=['code', 'date', 'pct'])
    ev_df.to_csv(EVENTS_CSV, index=False, encoding='utf-8-sig')
    print('=== 扫描完成 ===')
    print('扫描总数（有效数据）：%d' % scanned)
    print('跳过数：%d' % skipped)
    print('有涨停的股票数：%d' % hit_codes)
    print('事件总数：%d' % len(all_events))


if __name__ == '__main__':
    main()
