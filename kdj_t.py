#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
日内做T信号对比：KDJ vs MACD变色 vs 参数寻优（159552 五分钟K线，T+1合规）

口径（与 t_intraday.py 一致）：底仓满仓，信号触发卖出一半隔夜底仓，
现金接回；14:30 强制接回；买卖各万1。

规则：
  K0 纯KDJ    ：J从100上回落卖；K<30金叉 或 J上穿0 接回（用户公式映射）
  K1 KDJ+过滤 ：卖出加 偏离VWAP>dev + 上午时段；接回加午后时段
  C0 纯MACD变色：红→蓝卖；绿→黄/蓝→红 接回
  C1 MACD变色+过滤：同上过滤
  GRID        ：KDJ/MACD 各做参数网格，样本内(2024-08~2025-07)选优，
                样本外(2025-08~2026-08)验证
"""
import sys
import io
import itertools

import numpy as np
import pandas as pd

if sys.platform == 'win32' and not getattr(sys.stdout, '_utf8_wrapped', False):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stdout._utf8_wrapped = True

from t_intraday import simulate
from t_pattern_t1 import load_5min
from t_pattern import intraday_vwap
from t_ideal import ema

FEE = 0.0001
IS_END = '2025-07-31'   # 样本内/外分界


def calc_kdj(df, n=9):
    hh = df['high'].rolling(n).max()
    ll = df['low'].rolling(n).min()
    rsv = (df['close'] - ll) / (hh - ll) * 100
    k = rsv.ewm(alpha=1.0 / 3, adjust=False).mean()
    d = k.ewm(alpha=1.0 / 3, adjust=False).mean()
    return k, d, 3 * k - 2 * d


def cross_up(a, b):
    return (a > b) & (a.shift(1) <= b.shift(1))


def cross_down(a, b):
    return (a < b) & (a.shift(1) >= b.shift(1))


def macd_signals(df, fast=12, slow=26, sn=9, dev=None):
    """MACD柱变色做T信号；dev=None 为纯变色，否则加 VWAP+时段过滤"""
    close = df['close']
    dif = ema(close, fast) - ema(close, slow)
    hist = (dif - ema(dif, sn)) * 2
    h1, h2 = hist.shift(1), hist.shift(2)
    blue = (hist >= 0) & (h1 > 0) & (hist < h1)
    yellow = (hist < 0) & (h1 < 0) & (hist > h1)
    red_exp = (hist >= 0) & (h1 >= 0) & (h1 < h2) & (hist > h1)
    sell, buy = blue.fillna(False), (yellow | red_exp).fillna(False)
    if dev is not None:
        vwap = intraday_vwap(close)
        tt = close.index.strftime('%H:%M')
        sell = (blue & (close > vwap * (1 + dev)) & (tt >= '09:45') & (tt <= '11:30')).fillna(False)
        buy = (((yellow | red_exp | (close < vwap)) & (tt >= '13:00')) | (tt == '14:30')).fillna(False)
    return buy, sell


def kdj_signals(df, n=9, jthr=100, dev=None):
    """KDJ做T信号（用户公式映射到5分钟）；dev=None 为纯KDJ，否则加过滤"""
    close = df['close']
    k, d, j = calc_kdj(df, n)
    sell_half = cross_down(j, pd.Series(jthr, index=df.index)) & (j.shift(1) > jthr)
    sell_dead = cross_down(k, d) & (k > 70)
    sell = (sell_half | sell_dead).fillna(False)
    buy = (cross_up(k, d) & (k < 30) | cross_up(j, pd.Series(0.0, index=df.index))).fillna(False)
    if dev is not None:
        vwap = intraday_vwap(close)
        tt = close.index.strftime('%H:%M')
        sell = (sell & (close > vwap * (1 + dev)) & (tt >= '09:45') & (tt <= '11:30')).fillna(False)
        buy = ((buy | (close < vwap)) & (tt >= '13:00') | (tt == '14:30')).fillna(False)
    return buy, sell


def evaluate(df, buy, sell, fee=FEE):
    """返回 (超额pp, 回合数, 胜率%, 回合均%)"""
    hold = df['close'].iloc[-1] / df['close'].iloc[0] - 1
    nav, trips = simulate(df['close'], buy, sell, fee_side=fee)
    ex = (nav.iloc[-1] - 1 - hold) * 100
    wins = sum(1 for s, b in trips if b < s)
    n = len(trips)
    avg = float(pd.Series([(s - b) / s for s, b in trips]).mean() * 100) if n else 0.0
    return ex, n, (wins / n * 100 if n else 0.0), avg


def main():
    m = load_5min('159552')
    close = m['close']
    hold_full = close.iloc[-1] / close.iloc[0] - 1
    print('159552 五分钟: %s ~ %s，持有不动 %+.2f%%'
          % (close.index[0].date(), close.index[-1].date(), hold_full * 100))

    is_mask = close.index <= pd.Timestamp(IS_END + ' 23:59')
    df_is, df_oos = m[is_mask], m[~is_mask]
    for tag, d in [('样本内', df_is), ('样本外', df_oos)]:
        hr = d['close'].iloc[-1] / d['close'].iloc[0] - 1
        print('%s区间 %s ~ %s 持有 %+.2f%%' % (tag, d.index[0].date(), d.index[-1].date(), hr * 100))

    # ---- 固定规则全窗口对比 ----
    print('\n===== 固定规则 · 全窗口（超额 vs 持有不动，pp）=====')
    rules = [
        ('K0 纯KDJ', kdj_signals(m, 9, 100, None)),
        ('K1 KDJ+VWAP0.8%+时段', kdj_signals(m, 9, 100, 0.008)),
        ('C0 纯MACD变色', macd_signals(m, dev=None)),
        ('C1 MACD变色+VWAP0.8%+时段', macd_signals(m, dev=0.008)),
    ]
    for name, (buy, sell) in rules:
        ex, n, wr, avg = evaluate(m, buy, sell)
        ex_is, _, _, _ = evaluate(df_is, buy[is_mask], sell[is_mask])
        ex_oos, _, _, _ = evaluate(df_oos, buy[~is_mask], sell[~is_mask])
        print('%-26s 全窗口 %+7.2fpp（回合%d 胜率%.0f%% 回合均%+.2f%%） | 样本内 %+6.2f 样本外 %+6.2f'
              % (name, ex, n, wr, avg, ex_is, ex_oos))

    # ---- 参数寻优：样本内选优，样本外验证 ----
    print('\n===== 参数寻优（样本内选 Top5，看样本外是否站得住）=====')
    grids = {
        'KDJ': [(n, jt, dv) for n, jt, dv in itertools.product([6, 9, 14], [90, 100, 110], [0.005, 0.008, 0.012])],
        'MACD': [(f, s, dv) for f, s, dv in itertools.product([8, 12, 17], [21, 26, 34], [0.005, 0.008, 0.012])],
    }
    for kind, grid in grids.items():
        scored = []
        for p in grid:
            if kind == 'KDJ':
                buy, sell = kdj_signals(m, p[0], p[1], p[2])
            else:
                buy, sell = macd_signals(m, p[0], p[1], 9, p[2])
            ex_is, n_is, _, _ = evaluate(df_is, buy[is_mask], sell[is_mask])
            if n_is < 5:      # 回合太少无统计意义
                continue
            scored.append((ex_is, p, buy, sell))
        scored.sort(key=lambda x: -x[0])
        print('--- %s ---' % kind)
        for ex_is, p, buy, sell in scored[:5]:
            ex_oos, n_oos, wr_oos, avg_oos = evaluate(df_oos, buy[~is_mask], sell[~is_mask])
            print('  参数%s  样本内 %+7.2fpp  →  样本外 %+7.2fpp（回合%d 胜率%.0f%% 回合均%+.2f%%）'
                  % (p, ex_is, ex_oos, n_oos, wr_oos, avg_oos))


if __name__ == '__main__':
    main()
