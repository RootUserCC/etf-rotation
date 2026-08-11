#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据获取：
- 平均股价 880003：通达信扩展行情（exhq），自动探测板块指数市场代码
- ETF 512100.SH / 512890.SH：通达信标准行情（hq），上海市场 market=1
输出 CSV 到 data/ 目录
"""
import sys
import io
import os
import time

import pandas as pd

if sys.platform == 'win32' and not getattr(sys.stdout, '_utf8_wrapped', False):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stdout._utf8_wrapped = True

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
os.makedirs(DATA_DIR, exist_ok=True)

HQ_SERVERS = [
    ('180.153.18.170', 7709),    # 上海电信主站Z1（实测可用）
    ('218.6.170.47', 7709),      # 上证云成都电信一
    ('119.147.212.81', 7709),
    ('101.227.73.20', 7709),
    ('14.215.128.18', 7709),
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


def _fetch_bars(api, code: str, total: int = 8000):
    rows = []
    start = 0
    while len(rows) < total:
        batch = api.get_security_bars(9, 1, code, start, 800)
        if not batch:
            break
        rows = batch + rows
        if len(batch) < 800:
            break
        start += len(batch)
        time.sleep(0.3)
    return rows


def fetch_etf_hq(code: str, total: int = 8000) -> pd.DataFrame:
    """标准行情协议拉取 ETF 日线（market=1 上海，不复权）"""
    from pytdx.hq import TdxHq_API
    api = TdxHq_API()
    _connect_hq(api)
    rows = _fetch_bars(api, code, total)
    api.disconnect()

    df = pd.DataFrame(rows)
    df['date'] = pd.to_datetime(df['datetime'])
    df = df.set_index('date')[['open', 'high', 'low', 'close', 'vol', 'amount']]
    df = df[~df.index.duplicated(keep='last')].sort_index()
    return df


def fetch_etf_hfq_tdx(code: str) -> pd.DataFrame:
    """纯通达信通道计算后复权日线：
    原始K线 + get_xdxr_info 除权除息记录（fenhong 单位为"每10份"，suogu 为缩股比例），
    在除权日修正当日收益（分红按现金再投资、缩股按份额折算），
    复权因子 = 复权收盘/原始收盘，套用到 OHLC。
    """
    from pytdx.hq import TdxHq_API
    api = TdxHq_API()
    _connect_hq(api)
    rows = _fetch_bars(api, code)
    try:
        xdxr = api.get_xdxr_info(1, code) or []
    except Exception:
        xdxr = []
    api.disconnect()

    df = pd.DataFrame(rows)
    df['date'] = pd.to_datetime(df['datetime'])
    df = df.set_index('date')[['open', 'high', 'low', 'close']]
    df = df[~df.index.duplicated(keep='last')].sort_index()

    close = df['close']
    ret = close.pct_change().fillna(0.0)
    n_adj = 0
    for rec in xdxr:
        d = pd.Timestamp(rec['year'], rec['month'], rec['day'])
        pos = close.index.searchsorted(d)
        if pos >= len(close):
            continue
        d = close.index[pos]          # 除权日（若非交易日则顺延）
        if pos == 0:
            continue
        prev = close.index[pos - 1]
        if rec['category'] == 1 and rec.get('fenhong'):
            div = float(rec['fenhong']) / 10.0          # 每10份分红 → 每份
            ret.loc[d] = (close.loc[d] + div) / close.loc[prev] - 1
            n_adj += 1
        elif rec['category'] == 11 and rec.get('suogu'):
            s = float(rec['suogu'])                     # 1份旧 → s份新
            ret.loc[d] = close.loc[d] * s / close.loc[prev] - 1
            n_adj += 1

    hfq_close = close.iloc[0] * (1 + ret).cumprod()
    factor = hfq_close / close
    out = df.mul(factor, axis=0)
    print('  [TDX复权] %s: 除权记录修正 %d 处' % (code, n_adj))
    return out


def fetch_avg_price_hq(total: int = 8000) -> pd.DataFrame:
    """标准行情 get_index_bars 拉取通达信板块指数 880003 平均股价 日线"""
    from pytdx.hq import TdxHq_API
    api = TdxHq_API()
    for ip, port in HQ_SERVERS:
        try:
            if api.connect(ip, port, time_out=5):
                probe = api.get_index_bars(9, 1, '880003', 0, 1)
                if probe:
                    print('hq 已连接 %s:%d' % (ip, port))
                    break
                api.disconnect()
        except Exception:
            continue
    else:
        raise RuntimeError('无可用 hq 服务器')

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

    df = pd.DataFrame(rows)
    df['date'] = pd.to_datetime(df['datetime'])
    df = df.set_index('date')[['open', 'high', 'low', 'close']]
    df = df[~df.index.duplicated(keep='last')].sort_index()
    return df


def fetch_etf_hfq_tx(code: str) -> pd.DataFrame:
    """腾讯后复权日线（后备通道），按年分页"""
    import requests
    sess = requests.Session()
    sess.trust_env = False
    url = 'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get'
    symbol = 'sh%s' % code
    rows = []
    this_year = pd.Timestamp.now().year
    for year in range(2016, this_year + 1):
        param = '%s,day,%d-01-01,%d-12-31,400,hfq' % (symbol, year, year)
        r = sess.get(url, params={'param': param}, timeout=15)
        kl = r.json()['data'][symbol]
        key = 'hfqday' if 'hfqday' in kl else 'day'
        rows.extend(kl.get(key) or [])
        time.sleep(0.3)
    df = pd.DataFrame(rows, columns=['date', 'open', 'close', 'high', 'low', 'vol'][:len(rows[0])])
    df['date'] = pd.to_datetime(df['date'])
    for c in ['open', 'close', 'high', 'low']:
        df[c] = df[c].astype(float)
    df = df.set_index('date')[['open', 'high', 'low', 'close']]
    return df[~df.index.duplicated(keep='last')].sort_index()


def fetch_etf_hfq_em(code: str) -> pd.DataFrame:
    """后复权日线：东财主通道，失败自动切腾讯后备"""
    import requests
    url = 'https://push2his.eastmoney.com/api/qt/stock/kline/get'
    params = {
        'fields1': 'f1,f2,f3,f4,f5,f6',
        'fields2': 'f51,f52,f53,f54,f55,f56,f57,f58',
        'ut': '7eea3edcaed734bea9cbfc24409ed989',
        'klt': '101', 'fqt': '2',   # fqt=2 后复权
        'beg': '20160101', 'end': '20991231',
        'secid': '1.%s' % code,
    }
    ua = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    sess = requests.Session()
    sess.trust_env = False   # 绕过系统代理
    last_err = None
    for _ in range(4):
        try:
            r = sess.get(url, params=params, headers=ua, timeout=15)
            kl = r.json()['data']['klines']
            rows = [k.split(',') for k in kl]
            df = pd.DataFrame(rows, columns=['date', 'open', 'close', 'high', 'low', 'vol', 'amount', 'amplitude'])
            df['date'] = pd.to_datetime(df['date'])
            for c in ['open', 'close', 'high', 'low']:
                df[c] = df[c].astype(float)
            return df.set_index('date')[['open', 'high', 'low', 'close']].sort_index()
        except Exception as e:
            last_err = e
            time.sleep(2)
    print('  东财通道失败(%s)，切换腾讯后备通道...' % repr(last_err)[:60])
    return fetch_etf_hfq_tx(code)


def fetch_etf_hfq(code: str) -> pd.DataFrame:
    """后复权日线：通达信自算（主，经除权缺口验证最准）→ 东财 → 腾讯"""
    try:
        return fetch_etf_hfq_tdx(code)
    except Exception as e:
        print('  TDX复权通道失败(%s)，切东财/腾讯...' % repr(e)[:60])
        return fetch_etf_hfq_em(code)


def main():
    print('--- 拉取 512100.SH 中证1000ETF(后复权) ---')
    df1000 = fetch_etf_hfq('512100')
    df1000.to_csv(os.path.join(DATA_DIR, 'etf_512100_hfq.csv'))
    print('  %d 行  %s ~ %s' % (len(df1000), df1000.index[0].date(), df1000.index[-1].date()))

    print('--- 拉取 512100.SH 中证1000ETF(不复权) ---')
    df1000_raw = fetch_etf_hq('512100')
    df1000_raw.to_csv(os.path.join(DATA_DIR, 'etf_512100.csv'))
    print('  %d 行  %s ~ %s' % (len(df1000_raw), df1000_raw.index[0].date(), df1000_raw.index[-1].date()))

    print('--- 拉取 512890.SH 红利低波ETF(后复权) ---')
    dfdiv = fetch_etf_hfq('512890')
    dfdiv.to_csv(os.path.join(DATA_DIR, 'etf_512890_hfq.csv'))
    print('  %d 行  %s ~ %s' % (len(dfdiv), dfdiv.index[0].date(), dfdiv.index[-1].date()))

    print('--- 拉取 512890.SH 红利低波ETF(不复权) ---')
    dfdiv_raw = fetch_etf_hq('512890')
    dfdiv_raw.to_csv(os.path.join(DATA_DIR, 'etf_512890.csv'))
    print('  %d 行  %s ~ %s' % (len(dfdiv_raw), dfdiv_raw.index[0].date(), dfdiv_raw.index[-1].date()))

    print('--- 拉取 880003 平均股价 ---')
    dfavg = fetch_avg_price_hq()
    dfavg.to_csv(os.path.join(DATA_DIR, 'avg_880003.csv'))
    print('  %d 行  %s ~ %s' % (len(dfavg), dfavg.index[0].date(), dfavg.index[-1].date()))
    print(dfavg.tail(3).to_string())


if __name__ == '__main__':
    main()
