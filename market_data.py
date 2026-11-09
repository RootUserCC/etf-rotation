#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""行情取数层：腾讯日线 / 东财平均股价 / 新浪盘中快照，通达信仅作兜底。

- 每个源都带超时、短退避（0.5/1/2s）和主备入口，任一源抽风不会拖垮整轮更新；
- 只负责"取数 + 合并"，不写站点文件（落盘在 fetch_data.py / export_json.py）；
- fetch_data.py（日线）与 live.py（盘中快照）共用同一套接口。
"""
import datetime as dt
import json
import threading
import time

import pandas as pd
import requests

# 进攻/防守池标的（market: 1=上海 0=深圳）
SYMBOLS = [
    ('512100', 1, '中证1000ETF'),
    ('512890', 1, '红利低波ETF'),
    ('159552', 0, '中证2000增强ETF'),
    ('588220', 1, '科创100ETF'),
    ('515880', 1, '通信ETF'),
    ('159915', 0, '创业板ETF'),
    ('510300', 1, '沪深300ETF'),
    ('159201', 0, '自由现金流ETF'),
]
INDEXES = [
    ('000001', 1, '上证指数'),
    ('399006', 0, '创业板指'),
]

# 东财" A股平均股价"，与通达信 880003 同一序列（2026-10-22 收盘实测均为 24.22）
EM_AVG_SECID = '47.800005'
EM_UT = '7eea3edcaed734bea9cbfc24409ed989'

TX_HOSTS = [
    'https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get',
    'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get',
]
SINA_URL = 'https://hq.sinajs.cn/list='
SINA_HEADERS = {'Referer': 'https://finance.sina.com.cn'}

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'

_local = threading.local()


def _session():
    """每线程一个 session：requests.Session 非线程安全；trust_env=False 绕开系统代理"""
    s = getattr(_local, 's', None)
    if s is None:
        s = requests.Session()
        s.trust_env = False
        s.headers.update({'User-Agent': UA})
        _local.s = s
    return s


def _get(url, params=None, headers=None, tries=3, timeout=8, encoding=None):
    """带短退避的 GET，返回响应文本"""
    last = None
    for i in range(tries):
        try:
            r = _session().get(url, params=params, headers=headers, timeout=timeout)
            r.raise_for_status()
            if encoding:
                r.encoding = encoding
            return r.text
        except Exception as e:  # noqa: BLE001 - 网络异常一律重试
            last = e
            if i < tries - 1:
                time.sleep(0.5 * (i + 1))
    raise RuntimeError('%s 请求失败: %r' % (url.split('/')[2], last))


def market_of(code: str) -> int:
    return 1 if code.startswith(('5', '6', '9')) else 0


def symbol_of(code: str, market: int) -> str:
    return ('sh' if market == 1 else 'sz') + code


# ---------------- 腾讯日线 ----------------

def _tx_rows(text: str, sym: str, fq: str):
    data = json.loads(text)['data'][sym]
    key = (fq + 'day') if (fq + 'day') in data else 'day'
    rows = data.get(key) or []
    if not rows:
        return None
    df = pd.DataFrame(
        [[r[0], float(r[1]), float(r[3]), float(r[4]), float(r[2])] for r in rows],
        columns=['date', 'open', 'high', 'low', 'close'])
    df['date'] = pd.to_datetime(df['date'])
    return df.set_index('date').sort_index()


def tx_daily(code: str, market: int, fq: str = 'hfq', n: int = 320) -> pd.DataFrame:
    """腾讯最近 n 根日线；fq='hfq' 后复权 / 'qfq' 前复权 / '' 不复权"""
    sym = symbol_of(code, market)
    param = '%s,day,,,%d,%s' % (sym, n, fq)
    last = None
    for url in TX_HOSTS:
        try:
            df = _tx_rows(_get(url, params={'param': param}), sym, fq)
            if df is not None and len(df):
                return df
        except Exception as e:  # noqa: BLE001 - 换下一个入口
            last = e
    raise RuntimeError('腾讯日线 %s 失败: %r' % (code, last))


def tx_history(code: str, market: int, fq: str = 'hfq', start_year: int = 2015) -> pd.DataFrame:
    """按年区间翻页拉全历史（仅本地 CSV 缺失/缺口过大时用）"""
    sym = symbol_of(code, market)
    year_now = dt.date.today().year
    parts = []
    for year in range(start_year, year_now + 1):
        param = '%s,day,%d-01-01,%d-12-31,400,%s' % (sym, year, year, fq)
        for url in TX_HOSTS:
            try:
                df = _tx_rows(_get(url, params={'param': param}), sym, fq)
                if df is not None:
                    parts.append(df)
                break
            except Exception:  # noqa: BLE001
                continue
    if not parts:
        raise RuntimeError('腾讯历史 %s 无数据' % code)
    out = pd.concat(parts)
    return out[~out.index.duplicated(keep='last')].sort_index()


# ---------------- 东财日线（ETF 备用源） ----------------

def em_daily(code: str, market: int, fq: int = 1) -> pd.DataFrame:
    """东财日线；fq=1 后复权 / 0 不复权"""
    url = 'https://push2his.eastmoney.com/api/qt/stock/kline/get'
    params = {
        'ut': EM_UT,
        'secid': '%d.%s' % (market, code),
        'fields1': 'f1,f2,f3,f4,f5,f6',
        'fields2': 'f51,f52,f53,f54,f55,f56,f57',
        'klt': '101', 'fqt': str(fq), 'beg': '20150101', 'end': '20991231',
    }
    j = json.loads(_get(url, params=params))
    klines = (j.get('data') or {}).get('klines') or []
    if not klines:
        raise RuntimeError('东财日线 %s 无数据' % code)
    rows = [k.split(',') for k in klines]
    df = pd.DataFrame(rows, columns=['date', 'open', 'close', 'high', 'low', 'vol', 'amount'])
    df['date'] = pd.to_datetime(df['date'])
    for c in ['open', 'close', 'high', 'low']:
        df[c] = df[c].astype(float)
    return df.set_index('date')[['open', 'high', 'low', 'close']].sort_index()


# ---------------- 东财平均股价（880003 替代） ----------------

def em_avg_daily():
    """东财 800005 日线；返回 (df, pre_close)，兼供收盘更新与盘中预览。"""
    daily = em_daily('800005', 47, 0)
    pre_close = float(daily['close'].iloc[-2]) if len(daily) > 1 else None
    return daily, pre_close


# ---------------- 通达信 880003（兜底，公共节点经常整批不可用） ----------------

def tdx_avg(total: int = 8000, servers=None):
    """通达信板块指数 880003 平均股价；全失败返回 None"""
    try:
        from pytdx.hq import TdxHq_API
    except ImportError:
        return None
    if servers is None:
        servers = [('117.34.114.14', 7709), ('59.36.5.11', 7709), ('180.153.18.170', 7709)]
    for ip, port in servers:
        api = TdxHq_API()
        try:
            if not api.connect(ip, port, time_out=1.5):
                continue
            rows, start = [], 0
            while len(rows) < total:
                batch = api.get_index_bars(9, 1, '880003', start, 800)
                if not batch:
                    break
                rows = batch + rows
                if len(batch) < 800:
                    break
                start += len(batch)
            api.disconnect()
            if not rows:
                continue
            df = pd.DataFrame(rows)
            df['date'] = pd.to_datetime(df['datetime'])
            df = df.set_index('date')[['open', 'high', 'low', 'close']]
            df = df[~df.index.duplicated(keep='last')].sort_index()
            df.index = df.index.normalize()
            return df
        except Exception:  # noqa: BLE001 - 换下一个节点
            try:
                api.disconnect()
            except Exception:  # noqa: BLE001
                pass
    return None


# ---------------- 新浪盘中快照 ----------------

def sina_snapshot(pairs):
    """批量快照；pairs=[(market, code), ...] → {code: {name, open, prev, price, high, low, vol, date, time}}

    一次请求覆盖全部标的（实测 ~280ms），用于盘中每 5 秒刷新。
    """
    syms = [symbol_of(c, m) for m, c in pairs]
    text = _get(SINA_URL + ','.join(syms), headers=SINA_HEADERS, encoding='gbk', timeout=5)
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith('var hq_str_') or '="' not in line:
            continue
        head, body = line.split('="', 1)
        sym = head.replace('var hq_str_', '')
        f = body.rstrip('";').split(',')
        if len(f) < 32 or not f[0]:
            continue
        code = sym[2:]
        try:
            out[code] = {
                'name': f[0], 'open': float(f[1]), 'prev': float(f[2]),
                'price': float(f[3]), 'high': float(f[4]), 'low': float(f[5]),
                'vol': float(f[8]), 'date': f[30], 'time': f[31],
            }
        except ValueError:
            continue
    return out


# ---------------- CSV 读写与增量合并 ----------------

def read_price_csv(path):
    df = pd.read_csv(path, parse_dates=['date'])
    df = df.set_index('date').sort_index()
    df.index = df.index.normalize()
    return df


def write_price_csv(df, path, columns=None):
    out = df.sort_index()
    if columns:
        for c in columns:
            if c not in out.columns:
                out[c] = float('nan')
        out = out[columns]
    out.to_csv(path)


def read_avg_csv(path):
    df = pd.read_csv(path, parse_dates=['date'])
    df = df.set_index('date').sort_index()
    df.index = df.index.normalize()
    return df


def write_avg_csv(df, path):
    out = df.sort_index()
    out.index = out.index.strftime('%Y-%m-%d 15:00:00')
    out.index.name = 'date'
    out.to_csv(path)


def merge_append(old, new, label='', rescale=True):
    """只把 old 最后日期之后的新行追加进来，历史行不动。

    有重叠时按最后重叠日收盘比值缩放新行：不同数据源的复权基准不同（腾讯后复权
    与通达信自算在除息日口径略有差异），缩放后接缝无跳变。
    rescale=False 用于平均股价这类"值本身就是真值"的序列，只在重叠校验后直接追加。
    """
    if old is None or old.empty:
        return new.sort_index()
    new = new.sort_index()
    common = old.index.intersection(new.index)
    if not len(common):
        raise ValueError('%s 与本地数据无重叠，需要全量回补' % label)
    if rescale:
        d = common[-1]
        ratio = float(old.loc[d, 'close']) / float(new.loc[d, 'close'])
        if abs(ratio - 1) > 1e-9:
            num = new.select_dtypes('number').columns
            new = new.copy()
            new[num] = new[num] * ratio
    add = new[new.index > old.index[-1]]
    if add.empty:
        return old
    out = pd.concat([old, add])
    return out[~out.index.duplicated(keep='last')].sort_index()


def is_trading_day(d: dt.date) -> bool:
    return d.weekday() < 5


def is_trading_time(now: dt.datetime = None) -> bool:
    now = now or dt.datetime.now()
    if not is_trading_day(now.date()):
        return False
    t = now.time()
    return (dt.time(9, 15) <= t <= dt.time(11, 35)) or (dt.time(12, 55) <= t <= dt.time(15, 5))
