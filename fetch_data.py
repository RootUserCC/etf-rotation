#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据获取（2026-10 起主通道改为腾讯日线 + 东财平均股价，通达信仅作兜底）。

- ETF 不复权/后复权日线：腾讯 newfqkline（主）/ fqkline（备），每次只取最近 320 根，
  与本地 CSV 增量合并，历史行不动；后复权按最后重叠日比值衔接，接缝无跳变
- 指数日线：腾讯不复权日线
- 平均股价 880003：东财 800005「A股平均股价」（与通达信 880003 同一序列）日线；
  失败回退通达信；两者都失败保留旧数据并在 data/_status.json 标记 stale
- 通达信 _connect_hq / HQ_SERVERS 保留，供 compare_variants.py 等旧脚本复用

输出 CSV 到 data/ 目录；进度写 data/_progress.json，状态写 data/_status.json。
"""
import io
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

import market_data as md

# pythonw（无窗口计划任务）下 sys.stdout 为 None，不能 reconfigure
if sys.platform == 'win32' and sys.stdout is not None and not getattr(sys.stdout, '_utf8_wrapped', False):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stdout._utf8_wrapped = True

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, 'data')
os.makedirs(DATA_DIR, exist_ok=True)

STATUS_FILE = os.path.join(DATA_DIR, '_status.json')
PROGRESS_FILE = os.path.join(DATA_DIR, '_progress.json')

# ---------------- 通达信旧通道（保留给 compare_variants / week_diversify 等脚本） ----------------

HQ_SERVERS = [
    ('117.34.114.13', 7709),
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
    # 公共节点经常整批抽风（能连上但查询返回空），隔几秒再扫一轮可显著降低误判
    for attempt in range(2):
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
        if attempt == 0:
            time.sleep(5)
    raise RuntimeError('无可用 hq 服务器')


def _bars_to_df(rows, columns) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    df['date'] = pd.to_datetime(df['datetime'])
    df = df.set_index('date')[columns]
    df = df[~df.index.duplicated(keep='last')].sort_index()
    df.index = df.index.normalize()
    return df


def fetch_etf_hq(code: str, total: int = 8000, market: int = 1) -> pd.DataFrame:
    """ETF 日线（不复权）。旧签名保留：week_diversify.py 等按 total/market 调用。

    走腾讯日线：total<=320 时一次请求，否则按年翻页取全历史。
    """
    if total <= 320:
        return md.tx_daily(code, market, '')
    return md.tx_history(code, market, '', start_year=2015)


def fetch_etf_hfq(code: str, market: int = 1, total: int = 8000) -> pd.DataFrame:
    """ETF 后复权日线（腾讯 hfqday，与本地历史逐位一致）"""
    if total <= 320:
        return md.tx_daily(code, market, 'hfq')
    return md.tx_history(code, market, 'hfq', start_year=2015)


def fetch_index_hq(code: str, market: int = 1, total: int = 8000) -> pd.DataFrame:
    """指数日线（不复权）"""
    if total <= 320:
        return md.tx_daily(code, market, '')
    return md.tx_history(code, market, '', start_year=2015)


def fetch_avg_price_hq(total: int = 8000):
    """通达信板块指数 880003 平均股价（兜底通道，全失败返回 None）"""
    return md.tdx_avg(total)


# ---------------- 增量更新 ----------------

def _write_json(path, obj):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


def _progress(done, total, current=''):
    _write_json(PROGRESS_FILE, {
        'done': done, 'total': total, 'current': current,
        'time': time.strftime('%Y-%m-%d %H:%M:%S'),
    })


def _update_one(code, market, fq, suffix, errors, kind='etf'):
    """拉最近 320 根并合并进本地 CSV；本地缺失/缺口过大时全量回补"""
    if kind == 'index':
        path = os.path.join(DATA_DIR, 'index_%s.csv' % code)
        cols = None
    else:
        path = os.path.join(DATA_DIR, 'etf_%s%s.csv' % (code, suffix))
        cols = ['open', 'high', 'low', 'close', 'vol', 'amount'] if suffix == '' else None
    label = os.path.basename(path)[:-4]
    try:
        new = md.tx_daily(code, market, fq, n=320)
        if os.path.exists(path):
            old = md.read_price_csv(path)
            try:
                out = md.merge_append(old, new, label=label, rescale=(fq != ''))
            except ValueError:
                print('  [回补] %s 与本地无重叠，拉全历史' % label)
                out = md.merge_append(old, md.tx_history(code, market, fq), label=label,
                                      rescale=(fq != ''))
        else:
            out = md.tx_history(code, market, fq, start_year=2016 if fq else 2015)
        md.write_price_csv(out, path, cols)
        print('  %s: %d 行 %s ~ %s' % (label, len(out), out.index[0].date(), out.index[-1].date()))
    except Exception as e:  # noqa: BLE001 - 单标的失败不影响其余标的
        print('  [警告] %s 更新失败(%s)，保留旧 CSV' % (label, repr(e)[:80]))
        errors.append('%s: %s' % (label, repr(e)[:120]))


def _etf_last_date():
    """本地 ETF 数据的最新交易日（用于判断平均股价是否落后）"""
    last = None
    for code, _, _ in md.SYMBOLS:
        p = os.path.join(DATA_DIR, 'etf_%s_hfq.csv' % code)
        if os.path.exists(p):
            d = md.read_price_csv(p).index[-1]
            last = d if last is None else max(last, d)
    return last


def _update_avg(errors):
    """平均股价：东财 800005 补最近完整交易日；失败回退通达信；都失败保留旧数据"""
    path = os.path.join(DATA_DIR, 'avg_880003.csv')
    old = md.read_avg_csv(path) if os.path.exists(path) else None
    now = pd.Timestamp.now()
    today = now.normalize()
    source = ''

    try:
        daily, pre_close = md.em_avg_daily()
        complete = daily[daily.index < today]
        if now.hour >= 15 and now.minute >= 2 and today in daily.index:
            complete = pd.concat([complete, daily.loc[[today]]])
        base = old if old is not None else complete.iloc[:0]
        out = md.merge_append(base, complete, label='avg_880003', rescale=False)
        source = 'eastmoney:800005'
    except Exception as e:  # noqa: BLE001 - 东财限流很常见，回退通达信
        print('  [警告] 东财平均股价不可用(%s)，尝试通达信 880003...' % repr(e)[:80])
        errors.append('avg_em: %s' % repr(e)[:120])
        tdx = md.tdx_avg()
        if tdx is None or not len(tdx):
            print('  [警告] 通达信也不可用，avg_880003.csv 保留旧数据')
            errors.append('avg_tdx: 无可用节点')
            if old is None:
                raise RuntimeError('平均股价无任何可用数据源')
            last = _etf_last_date()
            stale = last is not None and old.index[-1].normalize() < last.normalize()
            return old, (old.index[-1].date().isoformat(), 'cached', stale)
        base = old if old is not None else tdx.iloc[:0]
        out = md.merge_append(base, tdx, label='avg_880003', rescale=False)
        source = 'tdx:880003'

    if old is not None and out.index[-1] == old.index[-1] and len(out) == len(old):
        print('  平均股价无新增（最后 %s）' % out.index[-1].date())
    else:
        md.write_avg_csv(out, path)
        print('  avg_880003: %d 行 ~ %s（来源 %s）' % (len(out), out.index[-1].date(), source))
    if source == '':
        source = 'cached'

    # 陈旧判断：平均股价是否落后于 ETF 的最新交易日（节假日不会误报）
    etf_last = _etf_last_date()
    stale = etf_last is not None and out.index[-1].normalize() < etf_last.normalize()
    return out, (out.index[-1].date().isoformat(), source, stale)


def main():
    t0 = time.time()
    errors = []
    print('--- 更新 ETF / 指数日线（腾讯通道，增量合并）---')
    jobs = [(code, market, fq, suffix, 'etf')
            for code, market, _ in md.SYMBOLS
            for fq, suffix in (('hfq', '_hfq'), ('', ''))]
    jobs += [(code, market, '', '', 'index') for code, market, _ in md.INDEXES]
    total_jobs = len(jobs)
    _progress(0, total_jobs, 'start')

    done = 0
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(_update_one, code, market, fq, suffix, errors, kind): code
                for code, market, fq, suffix, kind in jobs}
        for fut in as_completed(futs):
            done += 1
            _progress(done, total_jobs, futs[fut])

    print('--- 更新 880003 平均股价 ---')
    _, (avg_date, avg_source, avg_stale) = _update_avg(errors)

    elapsed = round(time.time() - t0, 2)
    _write_json(STATUS_FILE, {
        'time': time.strftime('%Y-%m-%d %H:%M:%S'),
        'avg_date': avg_date,
        'avg_source': avg_source,
        'avg_stale': bool(avg_stale),
        'errors': errors,
        'elapsed': elapsed,
    })
    _progress(total_jobs, total_jobs, 'done')
    print('完成，用时 %.2fs%s' % (elapsed, '（有告警，见 data/_status.json）' if errors else ''))
    if errors:
        for e in errors:
            print('  ! %s' % e)


if __name__ == '__main__':
    main()
