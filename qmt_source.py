#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QMT（迅投 xtquant）取数适配器。

客户端需已登录且数据服务可用；常见运行方式是极简模式（XtMiniQmt.exe）。
若调用报 200005「未找到数据服务」，请检查 QMT 登录状态和数据服务。
不可用时 available() 返回 False，调用方自动回退新浪/腾讯，不抛异常、不拖慢刷新。
"""
import threading
import time

import numpy as np

_TTL = 60              # 探测结果缓存 60 秒，避免每 5 秒重连
_lock = threading.Lock()
_ok = False
_checked = 0.0
_last_err = ''


def _xt():
    import xtquant.xtdata as xtdata
    xtdata.enable_hello = False
    return xtdata


def code_of(code: str, market: int) -> str:
    return '%s.%s' % (code, 'SH' if market == 1 else 'SZ')


def probe(force=False):
    """探测数据服务是否可用，返回 (ok, 错误信息)"""
    global _ok, _checked, _last_err
    with _lock:
        now = time.time()
        if not force and now - _checked < _TTL:
            return _ok, _last_err
        _checked = now
        try:
            detail = _xt().get_instrument_detail('512890.SH')
            _ok = bool(detail)
            _last_err = '' if _ok else 'get_instrument_detail 返回空'
        except Exception as e:  # noqa: BLE001 - 服务未开属常态
            _ok = False
            _last_err = str(e)[:160]
        return _ok, _last_err


def available(force=False) -> bool:
    return probe(force)[0]


def snapshot(pairs):
    """实时快照（约 3 秒一笔），兼容新浪快照结构"""
    x = _xt()
    codes = [code_of(c, m) for m, c in pairs]
    ticks = x.get_full_tick(codes) or {}
    now = time.localtime()
    out = {}
    for key, v in ticks.items():
        code = key.split('.')[0]
        price = v.get('lastPrice')
        prev = v.get('lastClose')
        if not price or not prev or (isinstance(price, float) and np.isnan(price)):
            continue
        out[code] = {
            'name': '', 'open': float(v.get('open') or 0), 'prev': float(prev),
            'price': float(price), 'high': float(v.get('high') or 0),
            'low': float(v.get('low') or 0), 'vol': float(v.get('volume') or 0),
            'date': time.strftime('%Y-%m-%d', now), 'time': time.strftime('%H:%M:%S', now),
        }
    return out


def daily(code, market, back=True, count=320):
    """日线：back=True 后复权。返回 date 索引的 open/high/low/close。"""
    import pandas as pd
    x = _xt()
    c = code_of(code, market)
    x.download_history_data(c, '1d', '')
    data = x.get_market_data_ex(
        ['open', 'high', 'low', 'close'], [c], period='1d',
        count=count, dividend_type='back' if back else 'none')
    df = (data or {}).get(c)
    if df is None or not len(df):
        raise RuntimeError('QMT 日线 %s 无数据' % c)
    df = df[['open', 'high', 'low', 'close']].astype(float)
    df.index = pd.to_datetime(df.index.astype(str))
    return df.sort_index()


def self_test():
    """命令行自检：python -c "import qmt_source; qmt_source.self_test()" """
    ok, err = probe(force=True)
    print('数据服务:', '可用' if ok else '不可用 - ' + err)
    if not ok:
        print('提示：客户端需以「极简模式」登录（XtMiniQmt.exe 在运行），普通 GUI 模式没有数据服务。')
        return
    t = snapshot([(1, '512890'), (0, '159552')])
    for k, v in t.items():
        print(k, v['price'], v['prev'], v['time'])
    df = daily('512890', 1, back=True, count=5)
    print(df)
