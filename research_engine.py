#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多标的轮动研究引擎（供 compare_wide_*.py 复用）。

口径与生产 run_backtest 一致：
- 信号在 T 日收盘产生，T+1 开盘成交；
- 收益拆为「昨收→今开」隔夜段（用昨日权重）+「今开→今收」日内段（用今日权重）；
- 费用 = fee × 单边换手（一次满仓切换换手=2，即双边万1）。
"""
import json
import os

import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, 'data')
FEE = 0.0001

_cache = {}


def load_price(code):
    if code not in _cache:
        df = pd.read_csv(os.path.join(DATA, 'etf_%s_hfq.csv' % code), parse_dates=['date'])
        df = df.set_index('date').sort_index()
        df.index = df.index.normalize()
        _cache[code] = df
    return _cache[code]


def panel(codes):
    """返回 (open, close) 两个 DataFrame，index=日期，columns=代码。未上市为 NaN"""
    opens, closes = {}, {}
    for c in codes:
        df = load_price(c)
        opens[c] = df['open']
        closes[c] = df['close']
    O = pd.DataFrame(opens).sort_index()
    C = pd.DataFrame(closes).sort_index()
    O = O[~O.index.duplicated(keep='last')]
    C = C[~C.index.duplicated(keep='last')]
    return O, C


def avg_price():
    df = pd.read_csv(os.path.join(DATA, 'avg_880003.csv'), parse_dates=['date'])
    s = df.set_index('date')['close'].sort_index()
    s.index = s.index.normalize()
    return s[~s.index.duplicated(keep='last')]


def metrics(nav, start=None):
    nav = nav.dropna()
    if start is not None:
        nav = nav[nav.index >= pd.Timestamp(start)]
    if len(nav) < 30:
        return {'cagr': np.nan, 'mdd': np.nan, 'calmar': np.nan,
                'sharpe': np.nan, 'total': np.nan, 'days': 0}
    n = len(nav)
    total = nav.iloc[-1] / nav.iloc[0] - 1
    cagr = (1 + total) ** (252.0 / n) - 1
    dd = nav / nav.cummax() - 1
    mdd = float(dd.min())
    ret = nav.pct_change().dropna()
    sharpe = float(ret.mean() / ret.std() * np.sqrt(252)) if ret.std() > 0 else np.nan
    return {'cagr': cagr, 'mdd': mdd, 'calmar': (cagr / abs(mdd)) if mdd else np.nan,
            'sharpe': sharpe, 'total': total, 'days': n}


def run(O, C, W, fee=FEE, start=None, end=None):
    """W: 目标权重（index=日期，表示该日开盘前的目标持仓），返回 (nav, stats, turnover)"""
    idx = C.index
    if start is not None:
        idx = idx[idx >= pd.Timestamp(start)]
    if end is not None:
        idx = idx[idx <= pd.Timestamp(end)]
    O = O.reindex(index=idx, columns=C.columns)
    C = C.reindex(index=idx, columns=C.columns)
    W = W.reindex(index=idx, columns=C.columns).fillna(0.0)
    W = W.div(W.sum(axis=1).replace(0, np.nan), axis=0).fillna(0.0)

    prev_close = C.shift(1)
    overnight = (O / prev_close - 1).replace([np.inf, -np.inf], np.nan).fillna(0.0)
    intraday = (C / O - 1).replace([np.inf, -np.inf], np.nan).fillna(0.0)
    Wprev = W.shift(1).fillna(0.0)

    r = (Wprev * overnight).sum(axis=1) + (W * intraday).sum(axis=1)
    turnover = (W - Wprev).abs().sum(axis=1)
    r = r - fee * turnover
    nav = (1 + r).cumprod()
    return nav, metrics(nav), turnover


def _apply_decisions(W, decisions, idx):
    """decisions: Series(index=决策日, value=权重 dict)，次日开盘生效"""
    rows = pd.DataFrame(0.0, index=idx, columns=W.columns)
    for d, w in decisions.items():
        if d not in rows.index:
            continue
        pos = rows.index.get_loc(d)
        if pos + 1 >= len(rows):
            continue
        for k, v in w.items():
            if k in rows.columns:
                rows.iloc[pos + 1:, rows.columns.get_loc(k)] = v
    return rows


def targets_from_switch(risk_on, attack_w, defense_w, idx):
    """risk_on: Series(bool) 在 T 日收盘决定；T+1 开盘切到进攻/防守权重"""
    W = pd.DataFrame(0.0, index=idx, columns=sorted(set(list(attack_w) + list(defense_w))))
    on = risk_on.reindex(idx).fillna(False).astype(bool)
    for i, d in enumerate(idx):
        if i == 0:
            continue
        src = attack_w if on.iloc[i - 1] else defense_w
        for k, v in src.items():
            W.iloc[i, W.columns.get_loc(k)] = v
    return W


def momentum_targets(O, C, idx, universe, N=20, top_k=1, rebal=5,
                     abs_filter=False, defense=None, min_gap=None):
    """截面动量轮动：每 rebal 个交易日按 N 日涨幅取前 top_k，等权。

    abs_filter=True 时，只有 N 日涨幅 > 0 的标的才入选，不足则持 defense。
    min_gap: 第一名与第二名最小动量差（避免频繁切换）。
    """
    cols = list(dict.fromkeys(universe))
    all_cols = cols + ([defense] if defense and defense not in cols else [])
    W = pd.DataFrame(0.0, index=idx, columns=all_cols)
    score_all = C[cols] / C[cols].shift(N) - 1

    held = []
    for i in range(N, len(idx), rebal):
        d = idx[i]
        s = score_all.loc[d].dropna()
        s = s[s.index.isin(cols)]
        # 当日/次日必须有开盘价，否则不可交易
        if i + 1 < len(idx):
            tradable = O.loc[idx[i + 1]].reindex(s.index).notna()
            s = s[tradable]
        if not len(s):
            continue
        s = s.sort_values(ascending=False)
        if abs_filter:
            s = s[s > 0]
        pick = list(s.index[:top_k]) if len(s) else []
        if min_gap is not None and len(pick) and len(s) > 1:
            if s.iloc[0] - s.iloc[1] < min_gap and set(held) == set(pick[:1]):
                pick = held[:top_k]
        if not pick and defense:
            pick = [defense]
        held = pick
        w = {c: 1.0 / len(pick) for c in pick}
        rows_from = i + 1
        rows_to = min(i + 1 + rebal, len(idx))
        for c, v in w.items():
            if c in W.columns:
                W.iloc[rows_from:rows_to, W.columns.get_loc(c)] = v
    return W


def yearly_returns(nav):
    nav = nav.dropna()
    out = {}
    for y, g in nav.groupby(nav.index.year):
        if len(g) < 20:
            continue
        out[int(y)] = float(g.iloc[-1] / g.iloc[0] - 1)
    return out


def load_meta():
    with open(os.path.join(DATA, 'wide_universe.json'), encoding='utf-8') as f:
        return json.load(f)
