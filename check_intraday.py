#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证"日内高点多在10点前、低点在13:30/14:30"的说法：拉分钟线统计高低点出现时刻分布"""
import sys, io, time
import pandas as pd

if sys.platform == 'win32' and sys.stdout is not None:
    sys.stdout.reconfigure(encoding='utf-8')

from pytdx.hq import TdxHq_API

SERVERS = [('180.153.18.170', 7709), ('218.6.170.47', 7709), ('119.147.212.81', 7709)]

def connect(api):
    for ip, port in SERVERS:
        try:
            if api.connect(ip, port, time_out=5):
                probe = api.get_index_bars(8, 1, '000001', 0, 1)
                if probe:
                    print('已连接 %s:%d' % (ip, port))
                    return
                api.disconnect()
        except Exception:
            continue
    raise RuntimeError('无可用服务器')

api = TdxHq_API()
connect(api)

# category=8: 1分钟K线, 尽量多拉历史
rows, start = [], 0
while True:
    batch = api.get_index_bars(8, 1, '000001', start, 800)
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
df['date'] = df['dt'].dt.date
df['hm'] = df['dt'].dt.strftime('%H:%M')
print('1分钟K线 %d 根, %s ~ %s, 共 %d 个交易日' % (len(df), df['dt'].iloc[0], df['dt'].iloc[-1], df['date'].nunique()))

# 每个交易日：日内最高/最低出现的时刻（若多次出现取最早）
def day_extreme_times(g):
    hi = g.loc[g['high'] == g['high'].max(), 'hm'].min()
    lo = g.loc[g['low'] == g['low'].min(), 'hm'].min()
    return pd.Series({'hi_t': hi, 'lo_t': lo})

res = df.groupby('date').apply(day_extreme_times, include_groups=False).reset_index()

def bucket(hm):
    h, m = int(hm[:2]), int(hm[3:])
    t = h * 60 + m
    for lo, hi, name in [
        (570, 600, '09:30-10:00'), (600, 630, '10:00-10:30'), (630, 660, '10:30-11:00'),
        (660, 690, '11:00-11:30'), (780, 810, '13:00-13:30'), (810, 840, '13:30-14:00'),
        (840, 870, '14:00-14:30'), (870, 900, '14:30-15:00')]:
        if lo <= t < hi:
            return name
    return hm

res['hi_b'] = res['hi_t'].map(bucket)
res['lo_b'] = res['lo_t'].map(bucket)
order = ['09:30-10:00', '10:00-10:30', '10:30-11:00', '11:00-11:30',
         '13:00-13:30', '13:30-14:00', '14:00-14:30', '14:30-15:00']

n = len(res)
print('\n日内【最高点】出现时段分布:')
c = res['hi_b'].value_counts()
for b in order:
    print('  %s  %4d 次  %5.1f%%' % (b, c.get(b, 0), c.get(b, 0) / n * 100))
print('\n日内【最低点】出现时段分布:')
c = res['lo_b'].value_counts()
for b in order:
    print('  %s  %4d 次  %5.1f%%' % (b, c.get(b, 0), c.get(b, 0) / n * 100))

res.to_csv('data/intraday_extreme_times.csv', index=False)
