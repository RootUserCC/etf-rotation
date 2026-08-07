#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
计算通达信口径的"平均股价"：沪深全部A股收盘价算术平均
- 数据源：pytdx 标准行情（个股日线）
- 口径：每交易日取所有A股收盘价（停牌股沿用最後收盘价 ff）的算术平均
- 退市股在最后交易日后剔除；新股自上市日起纳入
- 结果保存 data/avg_computed.csv，含 close 与样本数 count
带断点续跑：个股数据缓存在 data/stocks_cache.pkl
"""
import sys
import io
import os
import pickle
import time

import pandas as pd

if sys.platform == 'win32' and not getattr(sys.stdout, '_utf8_wrapped', False):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stdout._utf8_wrapped = True

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
CACHE = os.path.join(DATA_DIR, 'stocks_cache.pkl')
os.makedirs(DATA_DIR, exist_ok=True)

HQ_SERVERS = [
    ('180.153.18.170', 7709),    # 上海电信主站Z1
    ('218.6.170.47', 7709),
    ('119.147.212.81', 7709),
    ('101.227.73.20', 7709),
]

START_DATE = '2016-06-01'   # 512100 上市(2016-11) + MACD 预热
SH_PREFIX = ('600', '601', '603', '605', '688')
SZ_PREFIX = ('000', '001', '002', '003', '300', '301')


def connect(api, servers):
    for ip, port in servers:
        try:
            if api.connect(ip, port, time_out=5):
                probe = api.get_security_bars(9, 1, '600519', 0, 1)
                if probe:
                    return (ip, port)
                api.disconnect()
        except Exception:
            continue
    raise RuntimeError('无可用 hq 服务器')


def get_a_share_list(api):
    """获取沪深A股代码列表 [(market, code)]"""
    stocks = []
    for market, prefixes in [(1, SH_PREFIX), (0, SZ_PREFIX)]:
        total = api.get_security_count(market)
        start = 0
        while start < total:
            batch = api.get_security_list(market, start)
            if not batch:
                break
            for item in batch:
                code = item['code']
                if code.startswith(prefixes):
                    stocks.append((market, code))
            start += len(batch)
    return stocks


def fetch_stock_bars(api, market, code, min_date):
    """拉取单只股票全部日线，返回 {date_str: close}"""
    closes = {}
    start = 0
    while True:
        batch = api.get_security_bars(9, market, code, start, 800)
        if not batch:
            break
        for b in batch:
            dt = b['datetime'][:10]
            if dt >= min_date:
                closes[dt] = b['close']
        if len(batch) < 800:
            break
        start += len(batch)
        # 已取到足够早的数据则停止（batch 按时间升序，首条早于 min_date 即可停）
        if batch[0]['datetime'][:10] <= min_date:
            break
    return closes


def main():
    from pytdx.hq import TdxHq_API
    api = TdxHq_API()
    server = connect(api, HQ_SERVERS)
    print('hq 已连接 %s:%d' % server)

    stocks = get_a_share_list(api)
    print('沪深A股数量: %d' % len(stocks))

    # 加载缓存
    cache = {}
    if os.path.exists(CACHE):
        with open(CACHE, 'rb') as f:
            cache = pickle.load(f)
        print('缓存已有 %d 只' % len(cache))

    t0 = time.time()
    done = 0
    for market, code in stocks:
        key = '%d%s' % (market, code)
        if key in cache:
            done += 1
            continue
        try:
            cache[key] = fetch_stock_bars(api, market, code, START_DATE)
        except Exception as e:
            print('  %s 失败: %s，重连...' % (key, repr(e)[:60]))
            try:
                api.disconnect()
            except Exception:
                pass
            server = connect(api, HQ_SERVERS)
            print('  重连 %s:%d' % server)
            try:
                cache[key] = fetch_stock_bars(api, market, code, START_DATE)
            except Exception as e2:
                print('  %s 再失败，跳过: %s' % (key, repr(e2)[:60]))
                cache[key] = {}
        done += 1
        if done % 100 == 0:
            with open(CACHE, 'wb') as f:
                pickle.dump(cache, f)
            el = time.time() - t0
            print('进度 %d/%d  用时 %.0fs' % (done, len(stocks), el))
        time.sleep(0.02)

    api.disconnect()
    with open(CACHE, 'wb') as f:
        pickle.dump(cache, f)
    print('个股数据下载完成，共 %d 只' % len(cache))

    # 合成平均股价
    print('合成平均股价...')
    series = {}
    for key, closes in cache.items():
        if closes:
            series[key] = pd.Series(closes)
            series[key].index = pd.to_datetime(series[key].index)
    mat = pd.DataFrame(series).sort_index()
    # 每只股票仅在 [首bar, 末bar] 区间内 ffill（停牌沿用，退市后剔除）
    mat = mat.apply(lambda col: col.loc[col.first_valid_index():col.last_valid_index()].ffill()
                    .reindex(mat.loc[col.first_valid_index():col.last_valid_index()].index)
                    if col.first_valid_index() is not None else col)
    avg = mat.mean(axis=1, skipna=True)
    cnt = mat.notna().sum(axis=1)
    out = pd.DataFrame({'close': avg, 'count': cnt})
    out.index.name = 'date'
    out.to_csv(os.path.join(DATA_DIR, 'avg_computed.csv'))
    print('平均股价序列: %d 个交易日, %s ~ %s' % (len(out), out.index[0].date(), out.index[-1].date()))
    print(out.tail(5).to_string())


if __name__ == '__main__':
    main()
