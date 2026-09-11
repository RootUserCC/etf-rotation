#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""黄柱「放宽计数」对比回测

现行（严格）：一段连续黄柱（hist<0 且回升）中的第 N 根才买，黄柱中断/翻红即重新计数，
             黄柱段不足 N 根则不出信号。
放宽（混合）：从第 1 根黄柱（绿柱缩头）起，只要 hist 持续不回落（可变红，即翻红不重置），
             第 N 根买入；中途 hist 回落（重新变绿/蓝柱回落）则作废重新等。

输出：黄柱段长度分布（回答"没有 4 根黄柱怎么办"）、严格 vs 放宽同 N 对比、
     放宽版小网格寻优 + 上下半年稳健性。
数据：data/ai_stocks/ 本地缓存。
"""
import os

import numpy as np
import pandas as pd

import ai_yellow_bar as ay


def mixed_run(df):
    """放宽版计数：从黄柱起步的连续 hist 上升段（可翻红），段首必须是黄柱（hist<0）"""
    close = df['close']
    dif = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
    dea = dif.ewm(span=9, adjust=False).mean()
    hist = (2 * (dif - dea)).values
    h1 = np.r_[np.nan, hist[:-1]]
    rising = hist > h1
    yrun = np.zeros(len(hist), dtype=int)
    cnt, start_yellow = 0, False
    for i in range(len(hist)):
        if rising[i]:
            if cnt == 0:
                start_yellow = hist[i] < 0
            cnt += 1
            yrun[i] = cnt if start_yellow else 0
        else:
            cnt, start_yellow = 0, False
    return hist, yrun


def main():
    prep_strict, prep_mixed, hist_map = {}, {}, {}
    for code, name, _ in ay.load_stock_list():
        path = os.path.join(ay.CACHE, '%s.csv' % code)
        if not os.path.exists(path):
            continue
        df = pd.read_csv(path, parse_dates=['date'], index_col='date')
        p = ay.prepare(df)
        hist, yrun = mixed_run(df)
        p2 = dict(p)
        p2['run'] = yrun
        prep_strict[code] = p
        prep_mixed[code] = p2
        hist_map[code] = (p, hist)
    print('股票池 %d 只' % len(prep_strict))

    # ===== 1. 黄柱段长度分布：「没有 4 根黄柱」有多常见 =====
    print('\n===== 2026 年以来黄柱段长度分布（全池） =====')
    from collections import Counter
    lens = Counter()
    for code, (p, hist) in hist_map.items():
        run, idx = p['run'], p['idx']
        i = 60
        while i < len(run):
            if run[i] == 1 and idx[i] >= ay.SIGNAL_START:
                j = i
                while j + 1 < len(run) and run[j + 1] == run[j] + 1:
                    j += 1
                lens[run[j]] += 1
                i = j + 1
            else:
                i += 1
    total = sum(lens.values())
    reach4 = sum(v for k, v in lens.items() if k >= 4)
    for k in sorted(lens):
        print('  黄柱段长度 %d：%d 段' % (k, lens[k]))
    print('  共 %d 段，走到 4 根及以上的仅 %d 段（%.0f%%），%d 段被严格规则直接放弃'
          % (total, reach4, reach4 / total * 100 if total else 0, total - reach4))

    # ===== 2. 严格 vs 放宽，同 N + 首根蓝柱卖出 =====
    print('\n===== 严格黄柱 vs 放宽混合（无过滤 + 首根蓝柱卖出） =====')
    print('%-4s | %-28s | %-28s' % ('N', '严格黄柱（笔/胜率/均盈亏/PF）', '放宽混合（笔/胜率/均盈亏/PF）'))
    for n in [2, 3, 4, 5]:
        line = '%-4d | ' % n
        for prep in (prep_strict, prep_mixed):
            tr = []
            for code, p in prep.items():
                tr += ay.simulate(p, n, ay.BUY_FILTERS['无过滤'], ay.SELL_RULES['首根蓝柱'], ay.SIGNAL_START)
            c, w, m, pl, pf = ay.stats(tr)
            line += ' %3d笔 %5.1f%% %+6.2f%% %5.2f  | ' % (c, w * 100, m * 100, pf)
        print(line)

    # ===== 3. 放宽版小网格：N(2-6) × 5过滤 × 4卖出 =====
    sells = ['首根蓝柱', '蓝柱或20日', '持有20日', '跌破20日线']
    print('\n===== 放宽版网格：N(2-6) × 5种过滤 × %d种卖出 =====' % len(sells))
    results = []
    for n in [2, 3, 4, 5, 6]:
        for fname, filt in ay.BUY_FILTERS.items():
            for sname in sells:
                tr = []
                for code, p in prep_mixed.items():
                    tr += ay.simulate(p, n, filt, ay.SELL_RULES[sname], ay.SIGNAL_START)
                c, w, m, pl, pf = ay.stats(tr)
                results.append((n, fname, sname, c, w, m, pl, pf, tr))
    pool = [r for r in results if r[3] >= 150] or [r for r in results if r[3] >= 50]
    ranked = sorted(pool, key=lambda r: (r[7] != r[7], -(r[7] or 0)))
    print('%-4s %-14s %-12s %6s %8s %9s %8s %8s' % ('N', '买入过滤', '卖出规则', '交易数', '胜率', '平均盈亏', '盈亏比', '收益因子'))
    for r in ranked[:15]:
        print('%-4d %-14s %-12s %6d %7.1f%% %8.2f%% %8.2f %8.2f'
              % (r[0], r[1], r[2], r[3], r[4] * 100, r[5] * 100, r[6], r[7]))

    # ===== 4. 前 5 名上下半年稳健性 =====
    print('\n===== 放宽版前 5 名稳健性对照 =====')
    mid = pd.Timestamp('2026-06-01')
    print('%-4s %-14s %-12s | %18s | %18s' % ('N', '买入过滤', '卖出规则', '上半年', '下半年'))
    for r in ranked[:5]:
        h1 = [t for t in r[8] if t[0] < mid]
        h2 = [t for t in r[8] if t[0] >= mid]
        fmt = lambda tr: ('%d笔 %.0f%% %+.1f%% PF%.2f' % ((lambda s: (s[0], s[1] * 100, s[2] * 100, s[4]))(ay.stats(tr)))) if tr else '无交易'
        print('%-4d %-14s %-12s | %18s | %18s' % (r[0], r[1], r[2], fmt(h1), fmt(h2)))


if __name__ == '__main__':
    main()
