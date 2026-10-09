# -*- coding: utf-8 -*-
"""全量扫描上海 A 股（600/601/603/605），找 2026-01-01 以来收盘涨停过的股票。
命中股票日线存 data/limitup_stocks/sh_<code>.csv，事件汇总存 data/limitup_events_sh.csv。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fetch_data as fd

import pandas as pd
from pytdx.hq import TdxHq_API

CUTOFF = pd.Timestamp('2026-01-01')
OUT_DIR = os.path.join('data', 'limitup_stocks')
EVENTS_CSV = os.path.join('data', 'limitup_events_sh.csv')
CODE_PREFIXES = ('600', '601', '603', '605')


def list_sh_a_stocks(api):
    stocks = []
    start = 0
    while True:
        rows = api.get_security_list(1, start)
        if not rows:
            if start < 1000:  # 部分服务器 start<1000 返回空，但数据实际从 1000 开始
                start += 1000
                continue
            break
        for r in rows:
            code = r['code']
            name = r['name']
            if not code.startswith(CODE_PREFIXES):
                continue
            if 'ST' in name or '退' in name:
                continue
            stocks.append((code, name))
        if len(rows) < 1000:
            break
        start += 1000
    return stocks


def find_limitup_events(df):
    """收盘封板判定：涨幅>=9.7% 且收盘在最高价，且 bar 位置>=20"""
    close = df['close']
    prev_close = close.shift(1)
    cond = (close / prev_close - 1 >= 0.097) & (close >= df['high'] - 0.001)
    cond &= pd.Series(range(len(df)), index=df.index) >= 20
    return df.index[cond]


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    api = TdxHq_API()
    fd._connect_hq(api)
    events = []
    scanned = skipped = hit = 0
    try:
        stocks = list_sh_a_stocks(api)
        print('上海 A 股（600/601/603/605，剔除 ST/退）共 %d 只' % len(stocks))
        for i, (code, name) in enumerate(stocks):
            scanned += 1
            if scanned % 200 == 0:
                print('进度 %d/%d，命中 %d 只，事件 %d 条' % (scanned, len(stocks), hit, len(events)))
            if scanned % 500 == 0:
                time.sleep(1)
            try:
                rows = api.get_security_bars(9, 1, code, 0, 320)
            except Exception as e:
                print('%s %s 请求异常：%s，跳过' % (code, name, e))
                skipped += 1
                continue
            if not rows or len(rows) < 100:
                skipped += 1
                continue
            df = fd._bars_to_df(rows, ['open', 'high', 'low', 'close'])
            hit_dates = [d for d in find_limitup_events(df) if d >= CUTOFF]
            if not hit_dates:
                continue
            hit += 1
            df.to_csv(os.path.join(OUT_DIR, 'sh_%s.csv' % code), encoding='utf-8-sig')
            prev = df['close'].shift(1)
            for d in hit_dates:
                pct = df.loc[d, 'close'] / prev.loc[d] - 1
                events.append({'code': code, 'date': d.strftime('%Y-%m-%d'), 'pct': round(pct, 4)})
    finally:
        api.disconnect()
    pd.DataFrame(events, columns=['code', 'date', 'pct']).to_csv(EVENTS_CSV, index=False, encoding='utf-8-sig')
    print('--- 汇总 ---')
    print('扫描总数: %d' % scanned)
    print('跳过数: %d' % skipped)
    print('有涨停的股票数: %d' % hit)
    print('事件总数: %d' % len(events))


if __name__ == '__main__':
    main()
