#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""黄柱个股策略：滚动样本外验证（排查幸存者偏差）

背景：现行回测（ai_yellow_bar.py / gen_signals.py，2026 年 +130.8%）用的股票池是
"现在"从 site/ai.html 选好的 AI 强势股，回看历史存在幸存者偏差（year_test.py 已自承）。

本脚本用"当时可得信息"做滚动选池对照，生产信号口径与 gen_signals.py 完全一致：
  放宽计数（黄柱起算、hist 不回落即可、翻红不重置）第 3 根收盘买入；
  柱回落（变色）收盘卖出 或 止损 -8%；费用分 不计费 / 单边千1 两档。

对照口径（按买入日归属年度）：
  FIXED  = 现行固定池（site/ai.html），即有幸存者偏差的基准
  ALL    = 扩展候选池全量（data/ai_ytd_screen.csv 的 AI 概念成分股 ∪ 现行池），不做任何筛选
  ROLL30 = 每年初用截至上一年末的数据，从扩展候选池按上一年涨幅选前 30 只（模拟"当时选强势股"）
  池内分组 = 现行池按上一年涨幅分 前1/3 / 中1/3 / 后1/3（无池外历史时的下限估计，一并给出）

局限（如实说明）：扩展候选池本身取自"今天"的东财概念板块成分，已退市/边缘化的
AI 股不在其中，因此 ALL/ROLL30 仍带板块级幸存者偏差，只是比手工精选池弱得多；
池内后 1/3 组提供无筛选口径的下限参照。

数据：现行池用 data/ai_stocks/ 缓存；扩展池缺失的日线用 pytdx 补抓到
data/ai_stocks_ext/（独立目录，不污染生产缓存）。
输出：result_rolling_oos.txt
用法：python compare_rolling_oos.py [--fetch-only]
"""
import os
import sys
import time

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

import ai_yellow_bar as ay
import fetch_data as fd
from compare_ymix import mixed_run

EXT_CACHE = os.path.join(ROOT, 'data', 'ai_stocks_ext')
os.makedirs(EXT_CACHE, exist_ok=True)

SCREEN_CSV = os.path.join(ROOT, 'data', 'ai_ytd_screen.csv')
OUT_TXT = os.path.join(ROOT, 'result_rolling_oos.txt')

YEARS = list(range(2016, 2027))   # 2026 为年内截至数据末端
FETCH_BARS = 3600                 # 覆盖 2015 年以来 + MACD 预热即可


def load_broad_universe():
    """扩展候选池：ai_ytd_screen.csv 全量 ∪ 现行池代码"""
    codes = {}
    if os.path.exists(SCREEN_CSV):
        df = pd.read_csv(SCREEN_CSV, dtype={'code': str})
        for _, r in df.iterrows():
            c = str(r['code']).zfill(6)
            if len(c) == 6 and not c.startswith(('688', '4', '8', '5', '1')):
                codes[c] = str(r.get('name', ''))
    for code, name, _ in ay.load_stock_list():
        codes.setdefault(code, name)
    return codes


def cache_path(code):
    p = os.path.join(ay.CACHE, '%s.csv' % code)
    if os.path.exists(p):
        return p
    p = os.path.join(EXT_CACHE, '%s.csv' % code)
    return p if os.path.exists(p) else None


def fetch_missing(codes):
    """用 pytdx 把扩展池缺失的日线补抓到 EXT_CACHE（增量，断点续跑）"""
    missing = [c for c in codes if cache_path(c) is None]
    print('扩展池 %d 只，需补抓 %d 只' % (len(codes), len(missing)))
    if not missing:
        return
    from pytdx.hq import TdxHq_API
    api = TdxHq_API()
    fd._connect_hq(api)
    ok = fail = 0
    try:
        for k, code in enumerate(missing):
            try:
                rows = fd._fetch_bars(api, code, FETCH_BARS, ay.market_of(code))
                if not rows:
                    raise RuntimeError('无数据')
                df = fd._bars_to_df(rows, ['open', 'high', 'low', 'close'])
                df.to_csv(os.path.join(EXT_CACHE, '%s.csv' % code))
                ok += 1
            except Exception as e:
                fail += 1
                print('  [失败] %s %s: %s' % (code, codes[code], repr(e)[:60]))
            if (k + 1) % 25 == 0:
                print('  已补抓 %d/%d（成功 %d 失败 %d）' % (k + 1, len(missing), ok, fail))
            time.sleep(0.1)
    finally:
        api.disconnect()
    print('补抓完成：成功 %d，失败 %d' % (ok, fail))


def simulate_prod(df, start, end, fee=0.0):
    """生产口径：放宽计数第 3 根买，柱变色或 -8% 止损卖。按买入日归属 [start, end)。
    fee 为单边费率。返回 [(buy_date, sell_date, ret)]"""
    hist, yrun = mixed_run(df)
    closes = df['close'].values
    idx = df.index
    n = len(closes)
    trades = []
    i = 60  # MACD 预热，与 ay.simulate 一致
    while i < n:
        if yrun[i] == 3 and start <= idx[i] < end:
            entry = closes[i]
            j = i + 1
            while j < n:
                if yrun[j] == 0 or closes[j] <= entry * 0.92:
                    break
                j += 1
            j = min(j, n - 1)  # 数据末端仍持仓按最后收盘平仓（与 ay.simulate 一致）
            ret = (closes[j] * (1 - fee)) / (entry * (1 + fee)) - 1
            trades.append((idx[i], idx[j], ret))
            i = j + 1  # 卖出后才能再开仓
        else:
            i += 1
    return trades


def prev_year_return(df, year):
    """上一年涨幅：year-1 年最后交易日收盘 / year-2 年最后交易日收盘 - 1；数据不足返回 None"""
    c = df['close']
    try:
        e1 = c.loc[:'%d-12-31' % (year - 1)]
        e2 = c.loc[:'%d-12-31' % (year - 2)]
    except Exception:
        return None
    if len(e1) < 120 or len(e2) < 60:      # 上年交易数据太少不参与排名
        return None
    if e1.index[-1] < pd.Timestamp('%d-07-01' % (year - 1)):  # 上年下半年无数据（停牌/未上市）
        return None
    return float(e1.iloc[-1] / e2.iloc[-1] - 1)


def stats_line(trades):
    """(次数, 胜率, 平均盈亏, 收益因子)"""
    if not trades:
        return 0, 0.0, 0.0, float('nan')
    rets = pd.Series([t[2] for t in trades])
    win, loss = rets[rets > 0], rets[rets <= 0]
    pf = win.sum() / abs(loss.sum()) if len(win) and len(loss) else float('nan')
    return len(rets), (rets > 0).mean(), rets.mean(), pf


def fmt(trades):
    c, w, m, pf = stats_line(trades)
    if not c:
        return '   无交易'
    return '%4d笔 %5.1f%% %+6.2f%% PF=%.2f' % (c, w * 100, m * 100, pf)


def main(fetch_only=False):
    universe = load_broad_universe()
    print('扩展候选池 %d 只（ai_ytd_screen.csv ∪ 现行池）' % len(universe))
    fetch_missing(universe)
    if fetch_only:
        return

    pool_codes = [c for c, _, _ in ay.load_stock_list()]
    data = {}
    for code in universe:
        p = cache_path(code)
        if not p:
            continue
        try:
            df = pd.read_csv(p, parse_dates=['date'], index_col='date')
            if len(df) >= 80:
                data[code] = df
        except Exception:
            pass
    print('可用历史数据 %d 只（现行池 %d 只）' % (len(data), sum(c in data for c in pool_codes)))

    lines = []
    out = lines.append
    out('黄柱个股策略：滚动样本外验证（排除幸存者偏差）  生成于 %s' % pd.Timestamp.now())
    out('信号口径=生产口径：放宽计数第3根买、柱变色或-8%止损卖；费用两档：不计费 / 单边千1')
    out('扩展候选池 %d 只（今日东财 AI 概念成分 ∪ 现行池，仍含板块级幸存者偏差）' % len(universe))
    out('')

    schemes = ['FIXED', 'ALL', 'ROLL30', '池内前1/3', '池内中1/3', '池内后1/3']
    # summary[scheme] = {fee: [trades]}
    summary = {s: {0.0: [], 0.001: []} for s in schemes}
    yearly = {}   # (year, scheme) -> trades(fee=0)

    for year in YEARS:
        start = pd.Timestamp('%d-01-01' % year)
        end = pd.Timestamp('%d-01-01' % (year + 1))

        # 上一年涨幅（仅用上一年末之前的数据可得的信息）
        prev_ret = {c: prev_year_return(df, year) for c, df in data.items()}
        ranked = sorted(((c, r) for c, r in prev_ret.items() if r is not None),
                        key=lambda x: -x[1])
        roll30 = [c for c, _ in ranked[:30]]

        pool_with_hist = [c for c in pool_codes if prev_ret.get(c) is not None]
        pool_sorted = sorted(pool_with_hist, key=lambda c: -prev_ret[c])
        t = len(pool_sorted) // 3
        terciles = {'池内前1/3': pool_sorted[:t] or pool_sorted[:1],
                    '池内中1/3': pool_sorted[t:2 * t],
                    '池内后1/3': pool_sorted[2 * t:]}

        scheme_codes = {
            'FIXED': [c for c in pool_codes if c in data],
            'ALL': list(data.keys()),
            'ROLL30': roll30,
            **terciles,
        }

        out('===== %d 年（ROLL30 候选 %d 只有上年数据；池内分组基数 %d 只） ====='
            % (year, len(ranked), len(pool_sorted)))
        for s in schemes:
            tr0, tr1 = [], []
            for c in scheme_codes[s]:
                df = data.get(c)
                if df is None:
                    continue
                tr0 += simulate_prod(df, start, end, 0.0)
                tr1 += simulate_prod(df, start, end, 0.001)
            yearly[(year, s)] = tr0
            summary[s][0.0] += tr0
            summary[s][0.001] += tr1
            out('  %-8s 池%3d只 | 不计费: %s | 单边千1: %s'
                % (s, len(scheme_codes[s]), fmt(tr0), fmt(tr1)))
        out('')

    out('===== 全区间汇总（2016-%d，按买入日归属） =====' % YEARS[-1])
    out('%-8s | %26s | %26s' % ('口径', '不计费', '单边千1'))
    for s in schemes:
        out('%-8s | %26s | %26s' % (s, fmt(summary[s][0.0]), fmt(summary[s][0.001])))
    out('')

    # 幸存者偏差量化：FIXED vs ALL / ROLL30 / 池内后1/3
    out('===== 幸存者偏差量化（不计费口径，全区间） =====')
    base = summary['FIXED'][0.0]
    bc, bw, bm, bpf = stats_line(base)
    out('FIXED（现行池，含偏差基准）：%d笔 胜率 %.1f%% 平均盈亏 %+.2f%% PF=%.2f'
        % (bc, bw * 100, bm * 100, bpf))
    for s in ['ALL', 'ROLL30', '池内前1/3', '池内中1/3', '池内后1/3']:
        c, w, m, pf = stats_line(summary[s][0.0])
        if c:
            out('%-8s：%d笔 胜率 %.1f%% 平均盈亏 %+.2f%% PF=%.2f | 与FIXED差：均盈亏 %+.2fpp，胜率 %+.1fpp'
                % (s, c, w * 100, m * 100, pf, (m - bm) * 100, (w - bw) * 100))
    out('')
    out('逐年口径说明：FIXED=现行固定池；ALL=扩展池全量不筛选；ROLL30=每年初按上一年涨幅从扩展池选前30；')
    out('池内X/3=现行池按上一年涨幅分组（"后1/3"为无筛选下限参照：买上年弱势的池内股）。')

    text = '\n'.join(lines)
    print(text)
    with open(OUT_TXT, 'w', encoding='utf-8') as f:
        f.write(text + '\n')
    print('\n已写入 %s' % OUT_TXT)


if __name__ == '__main__':
    main('--fetch-only' in sys.argv)
