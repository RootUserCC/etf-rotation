#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
涨停股 + MACD 黄柱「吃主升浪」回测（2026 年以来）

思路：股票出现收盘涨停（主力进攻信号）后，等回踩使 MACD 绿柱缩头（黄柱），
在第 N 根黄柱收盘买入，按多种卖出规则离场，全网格搜索最优组合。

口径与 ai_yellow_bar.py / site/ai.html 完全一致：
  MACD(12,26,9)，柱 = 2*(DIF-DEA)
  黄柱 = 柱 < 0 且 hist > 前一日 hist（绿柱缩头）
  蓝柱 = 柱 > 0 且 hist < 前一日 hist（红柱缩头）

数据：data/limitup_stocks/{sz,sh}_<code>.csv（compare_limitup_fetch_sz/sh.py 生成，
      2026 年以来有过收盘涨停的全 A 股票池，不含 ST/退市/科创板）
事件：data/limitup_events_sz.csv / data/limitup_events_sh.csv

对比基准：
  A. 同一股票池内「无涨停要求」的全部黄柱信号（隔离涨停条件的增量价值）
  B. 涨停次日收盘直接买（不用 MACD 等回踩，看黄柱择时是否优于直接追）
"""
import glob
import os
import sys
import io

import pandas as pd

if sys.platform == 'win32' and sys.stdout is not None:
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

ROOT = os.path.dirname(os.path.abspath(__file__))
STOCK_DIR = os.path.join(ROOT, 'data', 'limitup_stocks')
SIGNAL_START = pd.Timestamp('2026-01-01')
MID = pd.Timestamp('2026-06-01')  # 稳健性对照分界


def load_events():
    """code -> 2026 年涨停日(Timestamp)列表；同时给每只股票标注板块涨停幅度"""
    events = {}
    for f in ['data/limitup_events_sz.csv', 'data/limitup_events_sh.csv']:
        path = os.path.join(ROOT, f)
        if not os.path.exists(path):
            continue
        df = pd.read_csv(path, dtype={'code': str}, parse_dates=['date'])
        for code, g in df.groupby('code'):
            events.setdefault(code, []).extend(g['date'].tolist())
    return {c: sorted(v) for c, v in events.items()}


def limit_of(code: str) -> float:
    return 0.20 if code.startswith('30') else 0.10


def prepare(df: pd.DataFrame, event_dates):
    """预计算指标 + 涨停窗口标记，供多次模拟复用"""
    close = df['close']
    dif = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
    dea = dif.ewm(span=9, adjust=False).mean()
    hist = 2 * (dif - dea)
    h1 = hist.shift(1)
    yellow = (hist < 0) & (hist > h1)
    idx = close.index

    # 涨停事件映射到 bar 位置
    pos = {d: i for i, d in enumerate(idx)}
    ev_pos = sorted(pos[d] for d in event_dates if d in pos)

    n = len(close)
    lu10 = [False] * n   # 前 10 个交易日内有涨停（不含当日）
    lu20 = [False] * n   # 前 20 个交易日内有涨停（不含当日）
    last_lu_close = [float('nan')] * n  # 最近一次涨停的收盘价
    import bisect
    for i in range(n):
        k = bisect.bisect_left(ev_pos, i)  # ev_pos[k] >= i，事件须严格在 i 之前
        if k > 0:
            e = ev_pos[k - 1]
            last_lu_close[i] = close.values[e]
            if i - e <= 10:
                lu10[i] = True
            if i - e <= 20:
                lu20[i] = True

    # 当日自身涨停（买入不可成交，需剔除）
    pct = close.pct_change()
    code_limit = None  # 由调用方按 code 判定后传入? 简化为按 pct 阈值双档
    self_lu = ((pct >= 0.097) & (close >= df['high'] - 0.001)).fillna(False).values

    return {
        'idx': idx,
        'closes': close.values,
        'run': yellow.groupby((~yellow).cumsum()).cumsum().values,
        'blue': ((hist > 0) & (hist < h1)).fillna(False).values,
        'dif': dif.values,
        'ma20': close.rolling(20).mean().values,
        'lu10': lu10,
        'lu20': lu20,
        'last_lu_close': last_lu_close,
        'self_lu': self_lu,
        'ev_pos': ev_pos,
    }


# 买入条件：黄柱第 N 根 × 涨停窗口 × 叠加过滤
def make_filter(name):
    if name == '无过滤':
        return lambda p, i: True
    if name == 'DIF>0':
        return lambda p, i: p['dif'][i] > 0
    if name == '站上MA20':
        return lambda p, i: p['closes'][i] > p['ma20'][i]
    if name == '不破涨停收盘':
        return lambda p, i: p['closes'][i] >= p['last_lu_close'][i]
    raise KeyError(name)


FILTERS = ['无过滤', 'DIF>0', '站上MA20', '不破涨停收盘']

SELL_RULES = {
    '首根蓝柱': dict(tp=None, sl=None, maxd=120, blue=True, ma20=False),
    '持有10日': dict(tp=None, sl=None, maxd=10, blue=False, ma20=False),
    '持有20日': dict(tp=None, sl=None, maxd=20, blue=False, ma20=False),
    '蓝柱或20日': dict(tp=None, sl=None, maxd=20, blue=True, ma20=False),
    '止损8%+20日': dict(tp=None, sl=0.08, maxd=20, blue=False, ma20=False),
    '止盈15%止损8%': dict(tp=0.15, sl=0.08, maxd=60, blue=False, ma20=False),
    '止盈20%止损10%': dict(tp=0.20, sl=0.10, maxd=60, blue=False, ma20=False),
    '跌破20日线': dict(tp=None, sl=None, maxd=60, blue=False, ma20=True),
}


def simulate(p, n_bar, lu_key, filt, sell, start):
    """第 n_bar 根黄柱收盘买入（要求前 W 日有涨停 + 过滤条件），按 sell 规则卖出"""
    idx, closes, run = p['idx'], p['closes'], p['run']
    lu = p[lu_key] if lu_key else None
    n = len(closes)
    trades = []
    i = 60
    while i < n:
        ok = run[i] == n_bar and idx[i] >= start and not p['self_lu'][i]
        if ok and lu is not None and not lu[i]:
            ok = False
        if ok and not filt(p, i):
            ok = False
        if ok:
            entry = closes[i]
            j = i + 1
            exit_j = n - 1
            while j < n:
                c = closes[j]
                if sell['sl'] and c <= entry * (1 - sell['sl']):
                    exit_j = j
                    break
                if sell['tp'] and c >= entry * (1 + sell['tp']):
                    exit_j = j
                    break
                if sell['blue'] and p['blue'][j]:
                    exit_j = j
                    break
                if sell['ma20'] and c < p['ma20'][j]:
                    exit_j = j
                    break
                if j - i >= sell['maxd']:
                    exit_j = j
                    break
                j += 1
            trades.append((idx[i], idx[exit_j], closes[exit_j] / entry - 1))
            i = exit_j + 1
        else:
            i += 1
    return trades


def simulate_chase(p, sell, start):
    """基准 B：涨停次日收盘直接买入（次日自身涨停则跳过，买不进）"""
    idx, closes = p['idx'], p['closes']
    n = len(closes)
    trades = []
    last_entry = -1
    for e in p['ev_pos']:
        i = e + 1
        if i >= n or i < 60 or idx[i] < start or i <= last_entry or p['self_lu'][i]:
            continue
        entry = closes[i]
        j = i + 1
        exit_j = n - 1
        while j < n:
            c = closes[j]
            if sell['sl'] and c <= entry * (1 - sell['sl']):
                exit_j = j
                break
            if sell['tp'] and c >= entry * (1 + sell['tp']):
                exit_j = j
                break
            if sell['blue'] and p['blue'][j]:
                exit_j = j
                break
            if sell['ma20'] and c < p['ma20'][j]:
                exit_j = j
                break
            if j - i >= sell['maxd']:
                exit_j = j
                break
            j += 1
        trades.append((idx[i], idx[exit_j], closes[exit_j] / entry - 1))
        last_entry = exit_j
    return trades


def stats(trades):
    if not trades:
        return 0, 0.0, 0.0, float('nan'), float('nan')
    rets = pd.Series([t[2] for t in trades])
    win = rets[rets > 0]
    loss = rets[rets <= 0]
    pl = win.mean() / abs(loss.mean()) if len(win) and len(loss) else float('nan')
    pf = win.sum() / abs(loss.sum()) if len(win) and len(loss) else float('nan')
    return len(rets), (rets > 0).mean(), rets.mean(), pl, pf


def fmt_row(tag, s):
    c, w, m, pl, pf = s
    return '%-34s %6d %7.1f%% %8.2f%% %8.2f %8.2f' % (tag, c, w * 100, m * 100, pl, pf)


def main():
    events = load_events()
    print('涨停事件股票数 %d，事件总数 %d' % (len(events), sum(len(v) for v in events.values())))

    prep = {}
    for path in glob.glob(os.path.join(STOCK_DIR, '*.csv')):
        code = os.path.basename(path).split('_')[1].split('.')[0]
        if code not in events:
            continue
        df = pd.read_csv(path, parse_dates=['date'], index_col='date')
        if len(df) < 100:
            continue
        prep[code] = prepare(df, events[code])
    print('有效股票 %d 只（数据 >=100 根）' % len(prep))

    # 涨停事件月度分布
    all_ev = [d for v in events.values() for d in v]
    if all_ev:
        ms = pd.Series(all_ev).dt.to_period('M').value_counts().sort_index()
        print('\n涨停事件月度分布: ' + '  '.join('%s:%d' % (k, v) for k, v in ms.items()))

    # ========== 基准 A：同池无涨停要求的黄柱 ==========
    print('\n===== 基准A：同池「无涨停要求」黄柱信号（隔离涨停条件价值） =====')
    print('%-34s %6s %8s %9s %8s %8s' % ('组合', '交易数', '胜率', '平均盈亏', '盈亏比', '收益因子'))
    for n_b in [1, 2]:
        for sname in ['首根蓝柱', '蓝柱或20日', '止盈15%止损8%']:
            tr = []
            for p in prep.values():
                tr += simulate(p, n_b, None, make_filter('无过滤'), SELL_RULES[sname], SIGNAL_START)
            print(fmt_row('第%d根黄柱+%s' % (n_b, sname), stats(tr)))

    # ========== 基准 B：涨停次日直接买 ==========
    print('\n===== 基准B：涨停次日收盘直接买（不等黄柱回踩） =====')
    print('%-34s %6s %8s %9s %8s %8s' % ('卖出规则', '交易数', '胜率', '平均盈亏', '盈亏比', '收益因子'))
    for sname, srule in SELL_RULES.items():
        tr = []
        for p in prep.values():
            tr += simulate_chase(p, srule, SIGNAL_START)
        print(fmt_row(sname, stats(tr)))

    # ========== 全网格：N × 涨停窗口 × 过滤 × 卖出 ==========
    print('\n===== 全网格：N(1-2) × 窗口(10日/20日) × 4过滤 × 8卖出 =====')
    results = []
    for n_b in [1, 2]:
        for lu_key, lu_name in [('lu10', '涨停10日内'), ('lu20', '涨停20日内')]:
            for fname in FILTERS:
                filt = make_filter(fname)
                for sname, srule in SELL_RULES.items():
                    tr = []
                    for p in prep.values():
                        tr += simulate(p, n_b, lu_key, filt, srule, SIGNAL_START)
                    results.append((n_b, lu_name, fname, sname, stats(tr), tr))
    pool = [r for r in results if r[4][0] >= 100]
    if len(pool) < 5:
        pool = [r for r in results if r[4][0] >= 50]
    pool.sort(key=lambda r: (r[4][4] != r[4][4], -(r[4][4] or 0)))
    print('%-34s %6s %8s %9s %8s %8s' % ('组合(N/窗口/过滤/卖出)', '交易数', '胜率', '平均盈亏', '盈亏比', '收益因子'))
    for r in pool[:20]:
        print(fmt_row('N%d %s %s %s' % (r[0], r[1], r[2], r[3]), r[4]))

    # ========== 稳健性：前 5 名上下半年对照 ==========
    print('\n===== 前 5 名稳健性对照（1-5月 vs 6-9月，防过拟合） =====')
    print('%-34s | %18s | %18s' % ('组合', '上半年(笔/胜率/均/PF)', '下半年(笔/胜率/均/PF)'))
    for r in pool[:5]:
        h1 = [t for t in r[5] if t[0] < MID]
        h2 = [t for t in r[5] if t[0] >= MID]
        f = lambda s: '%d笔 %.0f%% %+.1f%% %.2f' % (s[0], s[1] * 100, s[2] * 100, s[4]) if s[0] else '无交易'
        print('%-34s | %18s | %18s' % ('N%d %s %s %s' % (r[0], r[1], r[2], r[3]), f(stats(h1)), f(stats(h2))))

    # ========== 最优组合逐股明细 + 最近信号 ==========
    best = pool[0]
    print('\n===== 最优组合逐股明细：N%d %s %s %s =====' % (best[0], best[1], best[2], best[3]))
    filt = make_filter(best[2])
    srule = SELL_RULES[best[3]]
    rows = []
    for code, p in prep.items():
        tr = simulate(p, best[0], 'lu10' if best[1] == '涨停10日内' else 'lu20', filt, srule, SIGNAL_START)
        s = stats(tr)
        rows.append((code, s[0], s[1], s[2], s[3]))
    rows.sort(key=lambda r: -r[3])
    print('%-8s %6s %8s %9s %8s' % ('代码', '交易数', '胜率', '平均盈亏', '盈亏比'))
    for r in rows[:30]:
        print('%-8s %6d %7.1f%% %8.2f%% %8.2f' % (r[0], r[1], r[2] * 100, r[3] * 100, r[4]))
    pd.DataFrame(rows, columns=['code', 'trades', 'win_rate', 'avg_ret', 'pl_ratio']
                 ).to_csv(os.path.join(ROOT, 'data', 'limitup_yellow_best.csv'),
                          index=False, encoding='utf-8-sig')

    recent = sorted([t for t in best[5] if t[0] >= pd.Timestamp('2026-08-01')], key=lambda t: t[0])
    print('\n最优组合 8 月以来信号 %d 笔（买入日/卖出日/盈亏）:' % len(recent))
    for t in recent[-20:]:
        print('  %s -> %s  %+.1f%%' % (t[0].date(), t[1].date(), t[2] * 100))

    print('\n明细已存 data/limitup_yellow_best.csv')


if __name__ == '__main__':
    main()
