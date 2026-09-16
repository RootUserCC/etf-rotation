#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
自选股分组「MACD 四色柱第 N 根买入」回测模拟

口径（通达信四色柱，与 site/ai.html 一致）：
  MACD(12,26,9)，柱 = 2*(DIF-DEA)
  黄柱 = 柱 < 0 且缩头（hist > 前一日 hist，绿柱回升）
  红柱 = 柱 > 0 且放大（hist > 前一日 hist）
  蓝柱 = 柱 > 0 且缩头（hist < 前一日 hist）
  第 N 根黄/红柱 = 一段连续同色柱中的第 N 根，当日收盘买入

卖出规则（分别统计）：
  blue   = 首根蓝柱收盘卖出，最 paired 的经典搭配「黄买蓝卖 / 红买蓝卖」
  止盈X% = 收益达到 X% 收盘卖出（可叠加蓝柱，谁先触发算谁）
  H 日   = 固定持有 H 个交易日后收盘卖出

输出：每股票 + 全池汇总（交易次数、胜率、平均盈亏、盈亏比=平均盈利/平均亏损、收益因子），
     黄柱/红柱分别做网格搜索找最优条件，红柱额外给出「第 2 根红柱买入 × 各卖出规则」对比。
行情数据：pytdx 日线（不复权），缓存到 data/ai_stocks/
"""
import os
import re
import sys
import time

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fetch_data as fd

ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(ROOT, 'data', 'ai_stocks')
os.makedirs(CACHE, exist_ok=True)

# 只统计该日期之后出现的买入信号（MACD 仍用全历史预热）
SIGNAL_START = pd.Timestamp('2026-01-01')


def load_stock_list():
    """从 site/ai.html 的 DATA 数组解析股票池（6 位代码；港股 5 位代码、ETF、科创板自动排除）"""
    text = open(os.path.join(ROOT, 'site', 'ai.html'), encoding='utf-8').read()
    stocks = re.findall(r"\['(\d{6})', '([^']+)', '([^']*)'\]", text)
    seen, out = set(), []
    for code, name, note in stocks:
        if code not in seen and 'ETF' not in name and not code.startswith('688'):
            seen.add(code)
            out.append((code, name, note))
    return out


def market_of(code: str) -> int:
    """pytdx 市场：上海(5/6/9 开头)=1，深圳(0/1/2/3 开头)=0"""
    return 1 if code[0] in '569' else 0


def fetch_daily(api, code: str) -> pd.DataFrame:
    """pytdx 日线，带本地缓存（当日已抓过则直接用）"""
    path = os.path.join(CACHE, '%s.csv' % code)
    today = pd.Timestamp.now().date()
    if os.path.exists(path):
        df = pd.read_csv(path, parse_dates=['date'], index_col='date')
        if df.index[-1].date() >= today - pd.Timedelta(days=3):
            return df
    rows = fd._fetch_bars(api, code, 8000, market_of(code))
    if not rows:
        raise RuntimeError('%s 无数据' % code)
    df = fd._bars_to_df(rows, ['open', 'high', 'low', 'close'])
    df.to_csv(path)
    return df


def prepare(df: pd.DataFrame):
    """预计算一次指标，供多次模拟复用"""
    close = df['close']
    dif = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
    dea = dif.ewm(span=9, adjust=False).mean()
    hist = 2 * (dif - dea)
    h1 = hist.shift(1)
    yellow = (hist < 0) & (hist > h1)          # 绿柱缩头 = 黄柱
    red = (hist > 0) & (hist > h1)             # 红柱放大 = 红柱
    return {
        'idx': close.index,
        'closes': close.values,
        'run': yellow.groupby((~yellow).cumsum()).cumsum().values,   # 连续黄柱计数
        'red_run': red.groupby((~red).cumsum()).cumsum().values,     # 连续红柱计数
        'blue': ((hist > 0) & (hist < h1)).fillna(False).values,     # 红柱缩头 = 蓝柱
        'dif': dif.values,
        'ma20': close.rolling(20).mean().values,
        'ma60': close.rolling(60).mean().values,
    }


# 买入过滤：在柱信号上叠加的条件
BUY_FILTERS = {
    '无过滤': lambda p, i: True,
    'DIF>0(强势回踩)': lambda p, i: p['dif'][i] > 0,
    'DIF<0(深跌区)': lambda p, i: p['dif'][i] < 0,
    '站上60日线': lambda p, i: p['closes'][i] > p['ma60'][i],
    '跌破20日线': lambda p, i: p['closes'][i] < p['ma20'][i],
}

# 黄柱卖出规则：key → (止盈, 止损, 最长持有, 用蓝柱, 用MA20)
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

# 红柱卖出规则：纯止盈梯度 + 蓝柱 + 止盈与蓝柱孰先
RED_SELL_RULES = {
    '首根蓝柱': dict(tp=None, sl=None, maxd=120, blue=True, ma20=False),
    '止盈5%': dict(tp=0.05, sl=None, maxd=60, blue=False, ma20=False),
    '止盈8%': dict(tp=0.08, sl=None, maxd=60, blue=False, ma20=False),
    '止盈10%': dict(tp=0.10, sl=None, maxd=60, blue=False, ma20=False),
    '止盈15%': dict(tp=0.15, sl=None, maxd=60, blue=False, ma20=False),
    '止盈20%': dict(tp=0.20, sl=None, maxd=60, blue=False, ma20=False),
    '止盈30%': dict(tp=0.30, sl=None, maxd=60, blue=False, ma20=False),
    '止盈10%或蓝柱': dict(tp=0.10, sl=None, maxd=120, blue=True, ma20=False),
    '止盈20%或蓝柱': dict(tp=0.20, sl=None, maxd=120, blue=True, ma20=False),
    '止盈8%止损8%': dict(tp=0.08, sl=0.08, maxd=60, blue=False, ma20=False),
}


def simulate(p, n_bar: int, buy_filter, sell: dict, start=None, run_key='run'):
    """第 n_bar 根同色柱收盘买入（叠加 buy_filter），按 sell 规则卖出"""
    idx, closes, run = p['idx'], p['closes'], p[run_key]
    n = len(closes)
    trades = []
    i = 60  # MACD/MA60 预热
    while i < n:
        if run[i] == n_bar and (start is None or idx[i] >= start) and buy_filter(p, i):
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
            i = exit_j + 1  # 卖出后才能再开仓
        else:
            i += 1
    return trades


def stats(trades):
    """交易列表 → (次数, 胜率, 平均盈亏, 盈亏比, 收益因子)"""
    if not trades:
        return 0, 0.0, 0.0, float('nan'), float('nan')
    rets = pd.Series([t[2] for t in trades])
    win = rets[rets > 0]
    loss = rets[rets <= 0]
    pl = win.mean() / abs(loss.mean()) if len(win) and len(loss) else float('nan')
    pf = win.sum() / abs(loss.sum()) if len(win) and len(loss) else float('nan')
    return len(rets), (rets > 0).mean(), rets.mean(), pl, pf


def grid_search(prep, run_key, n_list, sell_rules, min_trades=150):
    """N × 买入过滤 × 卖出规则 全网格，返回按收益因子排序的结果列表"""
    results = []
    for n_b in n_list:
        for fname, filt in BUY_FILTERS.items():
            for sname, srule in sell_rules.items():
                all_trades = []
                for code, p in prep.items():
                    all_trades += simulate(p, n_b, filt, srule, SIGNAL_START, run_key)
                c, w, m, pl, pf = stats(all_trades)
                results.append((n_b, fname, sname, c, w, m, pl, pf, all_trades))
    # 交易数 >= min_trades 才参与排名（样本太小没有统计意义），不够则放宽
    pool = [r for r in results if r[3] >= min_trades]
    if len(pool) < 5:
        pool = [r for r in results if r[3] >= 50]
    return sorted(pool, key=lambda r: (r[7] != r[7], -(r[7] or 0)))


def print_ranked(ranked, bar_name, topn=20):
    print('%-4s %-14s %-14s %6s %8s %9s %8s %8s' % ('N', '买入过滤', '卖出规则', '交易数', '胜率', '平均盈亏', '盈亏比', '收益因子'))
    for r in ranked[:topn]:
        print('%-4d %-14s %-14s %6d %7.1f%% %8.2f%% %8.2f %8.2f'
              % (r[0], r[1], r[2], r[3], r[4] * 100, r[5] * 100, r[6], r[7]))


def main():
    stocks = load_stock_list()
    print('股票池 %d 只（来自 site/ai.html，6 位代码）' % len(stocks))

    from pytdx.hq import TdxHq_API
    api = TdxHq_API()
    fd._connect_hq(api)

    data = {}
    for k, (code, name, note) in enumerate(stocks):
        try:
            data[code] = fetch_daily(api, code)
        except Exception as e:
            print('  [跳过] %s %s: %s' % (code, name, repr(e)[:50]))
        if (k + 1) % 10 == 0:
            print('  已获取 %d/%d ...' % (k + 1, len(stocks)))
    api.disconnect()
    print('成功获取 %d 只' % len(data))

    prep = {code: prepare(df) for code, df in data.items()}
    names = {code: name for code, name, note in stocks}

    # ========== 第一部分：黄柱基准（第 2 根黄柱无过滤买入） ==========
    print('\n===== 黄柱基准：第 2 根黄柱无过滤买入（%s 以来，全池汇总） =====' % SIGNAL_START.date())
    print('%-14s %6s %8s %9s %8s %8s' % ('卖出规则', '交易数', '胜率', '平均盈亏', '盈亏比', '收益因子'))
    for sname in ['首根蓝柱', '持有10日', '持有20日', '蓝柱或20日', '止损8%+20日']:
        all_trades = []
        for code, p in prep.items():
            all_trades += simulate(p, 2, BUY_FILTERS['无过滤'], SELL_RULES[sname], SIGNAL_START)
        c, w, m, pl, pf = stats(all_trades)
        print('%-14s %6d %7.1f%% %8.2f%% %8.2f %8.2f' % (sname, c, w * 100, m * 100, pl, pf))

    # ========== 第二部分：红柱专题——第 2 根红柱买入，止盈多少卖 / 蓝柱卖 ==========
    print('\n===== 红柱专题：第 2 根红柱无过滤买入 × 各卖出规则 =====')
    print('%-16s %6s %8s %9s %8s %8s' % ('卖出规则', '交易数', '胜率', '平均盈亏', '盈亏比', '收益因子'))
    tp_scan = []
    for sname, srule in RED_SELL_RULES.items():
        all_trades = []
        for code, p in prep.items():
            all_trades += simulate(p, 2, BUY_FILTERS['无过滤'], srule, SIGNAL_START, 'red_run')
        c, w, m, pl, pf = stats(all_trades)
        tp_scan.append((sname, c, w, m, pl, pf))
        print('%-16s %6d %7.1f%% %8.2f%% %8.2f %8.2f' % (sname, c, w * 100, m * 100, pl, pf))

    # ========== 第三部分：黄柱全网格搜索最优买卖条件 ==========
    print('\n===== 黄柱全网格：N(1-5) × 5种买入过滤 × 8种卖出 = 200 组 =====')
    ranked = grid_search(prep, 'run', [1, 2, 3, 4, 5], SELL_RULES)
    print_ranked(ranked, '黄柱')

    # ========== 第四部分：红柱全网格搜索最优买卖条件 ==========
    print('\n===== 红柱全网格：N(1-4) × 5种买入过滤 × %d种卖出 = %d 组 ====='
          % (len(RED_SELL_RULES), 4 * len(BUY_FILTERS) * len(RED_SELL_RULES)))
    red_ranked = grid_search(prep, 'red_run', [1, 2, 3, 4], RED_SELL_RULES)
    print_ranked(red_ranked, '红柱')

    # ========== 第五部分：黄/红前 5 名上下半年稳健性对照 ==========
    print('\n===== 前 5 名稳健性对照（按买入日分上下半年，防过拟合） =====')
    mid = pd.Timestamp('2026-06-01')
    for tag, rk in [('黄柱', ranked), ('红柱', red_ranked)]:
        print('--- %s ---' % tag)
        print('%-4s %-14s %-14s | %16s | %16s' % ('N', '买入过滤', '卖出规则', '上半年(胜率/均盈亏/PF)', '下半年(胜率/均盈亏/PF)'))
        for r in rk[:5]:
            h1 = [t for t in r[8] if t[0] < mid]
            h2 = [t for t in r[8] if t[0] >= mid]
            c1, w1, m1, _, pf1 = stats(h1)
            c2, w2, m2, _, pf2 = stats(h2)
            fmt = lambda c, w, m, pf: '%d笔 %.0f%% %+.1f%% %.2f' % (c, w * 100, m * 100, pf) if c else '无交易'
            print('%-4d %-14s %-14s | %16s | %16s' % (r[0], r[1], r[2], fmt(c1, w1, m1, pf1), fmt(c2, w2, m2, pf2)))

    # ========== 第六部分：黄柱最优组合逐股票明细 ==========
    best = ranked[0]
    print('\n===== 黄柱最优组合逐股票：第 %d 根黄柱 + %s + %s =====' % (best[0], best[1], best[2]))
    print('%-8s %-6s %6s %8s %9s %8s' % ('代码', '名称', '交易数', '胜率', '平均盈亏', '盈亏比'))
    rows = []
    for code, p in prep.items():
        tr = simulate(p, best[0], BUY_FILTERS[best[1]], SELL_RULES[best[2]], SIGNAL_START)
        c, w, m, pl, pf = stats(tr)
        rows.append((code, names[code], c, w, m, pl))
        print('%-8s %-6s %6d %7.1f%% %8.2f%% %8.2f' % (code, names[code], c, w * 100, m * 100, pl))
    pd.DataFrame(rows, columns=['code', 'name', 'trades', 'win_rate', 'avg_ret', 'pl_ratio']
                 ).to_csv(os.path.join(ROOT, 'data', 'ai_yellow_bar_best_2026.csv'),
                          index=False, encoding='utf-8-sig')
    print('明细已存 data/ai_yellow_bar_best_2026.csv')

    # ========== 第七部分：导出 JSON 供 web 端（site/ai.html）展示 ==========
    def _r2(x):
        return None if x != x else round(float(x), 4)

    def _combo(r):
        return {'n': r[0], 'filter': r[1], 'sell': r[2], 'trades': r[3],
                'win': _r2(r[4]), 'avg': _r2(r[5]), 'pl': _r2(r[6]), 'pf': _r2(r[7])}

    red_best = red_ranked[0]
    out = {
        'updated': str(pd.Timestamp.now().date()),
        'since': str(SIGNAL_START.date()),
        'pool': len(prep),
        'best': _combo(best),
        'top': [_combo(r) for r in ranked[:10]],
        'red': {
            'best': _combo(red_best),
            'top': [_combo(r) for r in red_ranked[:10]],
            'tp_scan': [{'sell': s, 'trades': c, 'win': _r2(w), 'avg': _r2(m),
                         'pl': _r2(pl), 'pf': _r2(pf)} for s, c, w, m, pl, pf in tp_scan],
        },
        'per_stock': [{'code': c, 'name': nm, 'trades': t,
                       'win': _r2(w), 'avg': _r2(m), 'pl': _r2(pl)}
                      for c, nm, t, w, m, pl in rows],
    }
    import json
    jpath = os.path.join(ROOT, 'site', 'ai_backtest.json')
    with open(jpath, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print('已导出 %s' % jpath)


if __name__ == '__main__':
    main()
