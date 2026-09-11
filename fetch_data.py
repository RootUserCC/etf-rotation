#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据获取（2026-09 起全面改为 pytdx 通达信通道，不再使用东财/腾讯网页接口）：
- ETF 不复权日线：get_security_bars 日线原样（OHLC + vol + amount）
- ETF 后复权日线：不复权日线 + get_xdxr_info 除权记录自算
  （category=1 现金分红 / category=11 缩股，与 compare_variants.fetch_etf_hfq_mkt 同一套逻辑）
- 指数日线：get_index_bars
- 平均股价 880003：通达信板块指数，get_index_bars（尽力而为，
  失败时保留旧 CSV、仅告警，不再中断整个更新任务）
- _connect_hq / HQ_SERVERS 保留供 compare_variants.py / t_ideal.py / t_pattern_long.py 复用
输出 CSV 到 data/ 目录
"""
import sys
import io
import os
import time

import pandas as pd

# pythonw（无窗口计划任务）下 sys.stdout 为 None，不能 reconfigure
if sys.platform == 'win32' and sys.stdout is not None and not getattr(sys.stdout, '_utf8_wrapped', False):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stdout._utf8_wrapped = True

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
os.makedirs(DATA_DIR, exist_ok=True)

# 通达信 hq 服务器（2026-09 实测：大部分公共节点 TCP 可连但数据查询返回空，
# 前 8 台为当时实测个股K线+880003 均可用的节点，后面是旧节点留作兜底）
HQ_SERVERS = [
    ('59.36.5.11', 7709),
    ('117.34.114.14', 7709),
    ('117.34.114.15', 7709),
    ('117.34.114.16', 7709),
    ('117.34.114.17', 7709),
    ('117.34.114.18', 7709),
    ('117.34.114.20', 7709),
    ('117.34.114.27', 7709),
    ('115.238.90.165', 7709),
    ('124.71.187.122', 7709),
    ('180.153.18.170', 7709),
    ('115.238.56.198', 7709),
    ('218.75.126.9', 7709),
    ('218.6.170.47', 7709),
    ('123.125.108.14', 7709),
    ('60.191.117.167', 7709),
    ('110.41.147.114', 7709),
    ('119.97.185.59', 7709),
    ('124.70.133.119', 7709),
    ('123.60.84.66', 7709),
]
EXHQ_SERVERS = [
    ('112.74.214.43', 7727),
    ('120.24.0.77', 7727),
    ('47.103.48.45', 7727),
    ('101.133.214.242', 7727),
    ('124.74.236.94', 7721),
]


def _connect_hq(api):
    for ip, port in HQ_SERVERS:
        try:
            if api.connect(ip, port, time_out=5):
                probe = api.get_security_bars(9, 1, '600519', 0, 1)
                if probe:
                    print('hq 已连接 %s:%d' % (ip, port))
                    return
                api.disconnect()
        except Exception:
            continue
    raise RuntimeError('无可用 hq 服务器')


def _fetch_bars(api, code: str, total: int = 8000, market: int = 1, index: bool = False):
    """分页拉日线（category=9），index=True 走板块/指数接口"""
    fn = api.get_index_bars if index else api.get_security_bars
    rows = []
    start = 0
    while len(rows) < total:
        batch = fn(9, market, code, start, 800)
        if not batch:
            break
        rows = batch + rows
        if len(batch) < 800:
            break
        start += len(batch)
        time.sleep(0.3)
    return rows


def _bars_to_df(rows, columns) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    df['date'] = pd.to_datetime(df['datetime'])
    df = df.set_index('date')[columns]
    df = df[~df.index.duplicated(keep='last')].sort_index()
    df.index = df.index.normalize()
    return df


def fetch_etf_hq(code: str, total: int = 8000, market: int = 1) -> pd.DataFrame:
    """ETF 日线（不复权）：pytdx get_security_bars（market=1 上海 / 0 深圳）"""
    from pytdx.hq import TdxHq_API
    api = TdxHq_API()
    _connect_hq(api)
    try:
        rows = _fetch_bars(api, code, total, market)
    finally:
        api.disconnect()
    if not rows:
        raise RuntimeError('ETF %s 无数据' % code)
    return _bars_to_df(rows, ['open', 'high', 'low', 'close', 'vol', 'amount'])


def fetch_etf_hfq(code: str, market: int = 1, total: int = 8000) -> pd.DataFrame:
    """ETF 后复权日线：不复权日线 + xdxr 除权记录自算（market=1 上海 / 0 深圳）"""
    from pytdx.hq import TdxHq_API
    api = TdxHq_API()
    _connect_hq(api)
    try:
        rows = _fetch_bars(api, code, total, market)
        try:
            xdxr = api.get_xdxr_info(market, code) or []
        except Exception:
            xdxr = []
    finally:
        api.disconnect()
    if not rows:
        raise RuntimeError('ETF %s 无数据' % code)

    df = _bars_to_df(rows, ['open', 'high', 'low', 'close'])

    close = df['close']
    ret = close.pct_change().fillna(0.0)
    n_adj = 0
    for rec in xdxr:
        d = pd.Timestamp(rec['year'], rec['month'], rec['day'])
        pos = close.index.searchsorted(d)
        if pos >= len(close):
            continue
        d = close.index[pos]
        if pos == 0:
            continue
        prev = close.index[pos - 1]
        if rec['category'] == 1 and rec.get('fenhong'):
            div = float(rec['fenhong']) / 10.0
            ret.loc[d] = (close.loc[d] + div) / close.loc[prev] - 1
            n_adj += 1
        elif rec['category'] == 11 and rec.get('suogu'):
            s = float(rec['suogu'])
            ret.loc[d] = close.loc[d] * s / close.loc[prev] - 1
            n_adj += 1

    hfq_close = close.iloc[0] * (1 + ret).cumprod()
    factor = hfq_close / close
    out = df.mul(factor, axis=0)
    print('  [TDX复权] %s: 除权记录修正 %d 处' % (code, n_adj))
    return out


def fetch_index_hq(code: str, market: int = 1, total: int = 8000) -> pd.DataFrame:
    """指数日线：pytdx get_index_bars（market=1 上海 / 0 深圳）"""
    from pytdx.hq import TdxHq_API
    api = TdxHq_API()
    _connect_hq(api)
    try:
        rows = _fetch_bars(api, code, total, market, index=True)
    finally:
        api.disconnect()
    if not rows:
        raise RuntimeError('指数 %s 无数据' % code)
    return _bars_to_df(rows, ['open', 'high', 'low', 'close'])


def fetch_avg_price_hq(total: int = 8000):
    """拉取通达信板块指数 880003 平均股价 日线（尽力而为，全失败返回 None）"""
    from pytdx.hq import TdxHq_API
    for ip, port in HQ_SERVERS:
        api = TdxHq_API()
        try:
            if not api.connect(ip, port, time_out=6):
                continue
            rows = []
            start = 0
            while len(rows) < total:
                batch = api.get_index_bars(9, 1, '880003', start, 800)
                if not batch:
                    break
                rows = batch + rows
                if len(batch) < 800:
                    break
                start += len(batch)
                time.sleep(0.3)
            api.disconnect()
            if rows:
                print('hq 已连接 %s:%d' % (ip, port))
                df = pd.DataFrame(rows)
                df['date'] = pd.to_datetime(df['datetime'])
                df = df.set_index('date')[['open', 'high', 'low', 'close']]
                return df[~df.index.duplicated(keep='last')].sort_index()
        except Exception:
            try:
                api.disconnect()
            except Exception:
                pass
            continue
    return None


def _retry(fn, tries=3, delay=3):
    """拉取失败自动重试（通道偶发断连）"""
    last = None
    for i in range(tries):
        try:
            return fn()
        except Exception as e:
            last = e
            print('  第%d次失败(%s)，%ds后重试...' % (i + 1, repr(e)[:60], delay))
            time.sleep(delay)
    raise last


def main():
    print('--- 拉取 512100.SH 中证1000ETF(后复权) ---')
    df1000 = _retry(lambda: fetch_etf_hfq('512100'))
    df1000.to_csv(os.path.join(DATA_DIR, 'etf_512100_hfq.csv'))
    print('  %d 行  %s ~ %s' % (len(df1000), df1000.index[0].date(), df1000.index[-1].date()))

    print('--- 拉取 512100.SH 中证1000ETF(不复权) ---')
    df1000_raw = _retry(lambda: fetch_etf_hq('512100'))
    df1000_raw.to_csv(os.path.join(DATA_DIR, 'etf_512100.csv'))
    print('  %d 行  %s ~ %s' % (len(df1000_raw), df1000_raw.index[0].date(), df1000_raw.index[-1].date()))

    print('--- 拉取 512890.SH 红利低波ETF(后复权) ---')
    dfdiv = _retry(lambda: fetch_etf_hfq('512890'))
    dfdiv.to_csv(os.path.join(DATA_DIR, 'etf_512890_hfq.csv'))
    print('  %d 行  %s ~ %s' % (len(dfdiv), dfdiv.index[0].date(), dfdiv.index[-1].date()))

    print('--- 拉取 512890.SH 红利低波ETF(不复权) ---')
    dfdiv_raw = _retry(lambda: fetch_etf_hq('512890'))
    dfdiv_raw.to_csv(os.path.join(DATA_DIR, 'etf_512890.csv'))
    print('  %d 行  %s ~ %s' % (len(dfdiv_raw), dfdiv_raw.index[0].date(), dfdiv_raw.index[-1].date()))

    # 进攻仓新标的：中证2000增强ETF 159552（深圳 market=0，2024-06-28 上市）
    print('--- 拉取 159552.SZ 中证2000增强ETF(后复权) ---')
    df2000e = _retry(lambda: fetch_etf_hfq('159552', market=0))
    df2000e.to_csv(os.path.join(DATA_DIR, 'etf_159552_hfq.csv'))
    print('  %d 行  %s ~ %s' % (len(df2000e), df2000e.index[0].date(), df2000e.index[-1].date()))

    print('--- 拉取 159552.SZ 中证2000增强ETF(不复权) ---')
    df2000e_raw = _retry(lambda: fetch_etf_hq('159552', market=0))
    df2000e_raw.to_csv(os.path.join(DATA_DIR, 'etf_159552.csv'))
    print('  %d 行  %s ~ %s' % (len(df2000e_raw), df2000e_raw.index[0].date(), df2000e_raw.index[-1].date()))

    # 880003 平均股价：通达信板块指数，尽力而为，失败保留旧数据不致命
    print('--- 拉取 880003 平均股价(尽力而为) ---')
    dfavg = fetch_avg_price_hq()
    if dfavg is not None and len(dfavg):
        dfavg.to_csv(os.path.join(DATA_DIR, 'avg_880003.csv'))
        print('  %d 行  %s ~ %s' % (len(dfavg), dfavg.index[0].date(), dfavg.index[-1].date()))
        print(dfavg.tail(3).to_string())
    else:
        old = pd.read_csv(os.path.join(DATA_DIR, 'avg_880003.csv'), parse_dates=['date'])
        print('  [警告] 通达信通道不可用，avg_880003.csv 保留旧数据（最后日期 %s），本次不更新'
              % old['date'].iloc[-1].date())

    print('--- 拉取 510300.SH 沪深300ETF(后复权) ---')
    df300 = _retry(lambda: fetch_etf_hfq('510300'))
    df300.to_csv(os.path.join(DATA_DIR, 'etf_510300_hfq.csv'))
    print('  %d 行  %s ~ %s' % (len(df300), df300.index[0].date(), df300.index[-1].date()))

    # 防守池择优候选：自由现金流ETF 159201（深圳 market=0，2025-02 上市）
    print('--- 拉取 159201.SZ 自由现金流ETF(后复权) ---')
    df201 = _retry(lambda: fetch_etf_hfq('159201', market=0))
    df201.to_csv(os.path.join(DATA_DIR, 'etf_159201_hfq.csv'))
    print('  %d 行  %s ~ %s' % (len(df201), df201.index[0].date(), df201.index[-1].date()))

    print('--- 拉取 000001.SH 上证指数 ---')
    dfsh = _retry(lambda: fetch_index_hq('000001', market=1))
    dfsh.to_csv(os.path.join(DATA_DIR, 'index_000001.csv'))
    print('  %d 行  %s ~ %s' % (len(dfsh), dfsh.index[0].date(), dfsh.index[-1].date()))

    print('--- 拉取 399006.SZ 创业板指 ---')
    dfcyb = _retry(lambda: fetch_index_hq('399006', market=0))
    dfcyb.to_csv(os.path.join(DATA_DIR, 'index_399006.csv'))
    print('  %d 行  %s ~ %s' % (len(dfcyb), dfcyb.index[0].date(), dfcyb.index[-1].date()))


if __name__ == '__main__':
    main()
