#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""强势股低吸策略回测（日线）

思路：只在中期强势的股票上做回调低吸，赚"强势股二次启动"的钱。
  - 强势条件（选股）：多头排列 / 接近新高 / 两者叠加（可叠加大盘过滤）
  - 低吸触发（择时）：回踩MA20 / 自近10日高点回撤 / KDJ-J超卖 / 连跌后首阳
  - 卖出：止损止盈 / 跌破均线 / 持有上限
标的：515050 通信ETF 2026Q2 十大重仓（AI算力链），后复权日线，2016-01 ~ 2026-09
成交：信号日收盘产生 → 次日开盘买入/卖出，同票串行、无重叠持仓
费用：买入万1，卖出万1 + 印花税0.05%
对照：同一只股票、相同持有天数的"任意日开盘入场"平均净收益（剥离个股beta与幸存者偏差）
"""
import sys
import io
import os

import numpy as np
import pandas as pd

if sys.platform == 'win32' and sys.stdout is not None and not getattr(sys.stdout, '_utf8_wrapped', False):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stdout._utf8_wrapped = True

from backtest import max_drawdown, annualized, ema

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')

STOCKS = [
    ('300502', '新易盛'), ('603986', '兆易创新'), ('300308', '中际旭创'),
    ('002475', '立讯精密'), ('002384', '东山精密'), ('601138', '工业富联'),
    ('600183', '生益科技'), ('600487', '亨通光电'), ('002463', '沪电股份'),
    ('300394', '天孚通信'),
]

FEE_BUY = 0.0001
FEE_SELL = 0.0001 + 0.0005          # 万1 + 印花税0.05%
ROUND_TRIP = FEE_BUY + FEE_SELL
MAXH = 60                            # 基准对照的最大持有天数


def load(name):
    df = pd.read_csv(os.path.join(DATA, name), parse_dates=['date']).set_index('date')
    df.index = df.index.normalize()
    return df[~df.index.duplicated(keep='first')]


def prep(df):
    c, h, l = df['close'], df['high'], df['low']
    ind = {}
    ind['ma10'] = c.rolling(10).mean()
    ind['ma20'] = c.rolling(20).mean()
    ind['ma60'] = c.rolling(60).mean()
    ind['hh10'] = h.rolling(10).max()
    ind['hh250'] = h.rolling(250).max()
    ll9, hh9 = l.rolling(9).min(), h.rolling(9).max()
    rsv = (c - ll9) / (hh9 - ll9) * 100
    k = rsv.ewm(com=2, adjust=False).mean()          # 通达信 SMA(RSV,3,1)
    d = k.ewm(com=2, adjust=False).mean()
    ind['j'] = 3 * k - 2 * d
    return ind


def cond_strong(df, ind, kind, mkt=None):
    c = df['close']
    if kind == 'S0':
        s = pd.Series(True, index=df.index)
    elif kind == 'S1':                                # 多头排列
        s = (c > ind['ma60']) & (ind['ma20'] > ind['ma60'])
    elif kind == 'S2':                                # 距250日高点回撤<15%
        s = c >= 0.85 * ind['hh250']
    else:                                             # S3 = 多头排列 + 接近新高
        s = (c > ind['ma60']) & (ind['ma20'] > ind['ma60']) & (c >= 0.85 * ind['hh250'])
    s = s.fillna(False)
    if mkt is not None:
        s = s & mkt
    return s


def cond_dip(df, ind, kind):
    c, o, l = df['close'], df['open'], df['low']
    if kind == 'D0':
        return pd.Series(True, index=df.index)
    if kind == 'D1':                                  # 盘中回踩MA20、收盘站上
        return ((l <= ind['ma20'] * 1.01) & (c > ind['ma20'] * 0.99)).fillna(False)
    if kind == 'D2':                                  # 自近10日高点回撤>=8%
        return (c / ind['hh10'] - 1 <= -0.08).fillna(False)
    if kind == 'D3':                                  # KDJ J<10
        return (ind['j'] < 10).fillna(False)
    return ((c > o) & (c > c.shift(1)) & (c / c.shift(3) - 1 < 0)).fillna(False)   # D4 连跌后首阳


def sim_stock(df, ind, strong, dip, sell):
    """单票串行：空仓时满足 强势&低吸 → 次日开盘买入；持仓触及卖出 → 次日开盘卖出。
    返回 (trades, contrib)，contrib 为逐日贡献收益（1/N 仓位口径，含费用）"""
    n = len(df)
    o, c = df['open'].values, df['close'].values
    ma10 = ind['ma10'].values
    ma20 = ind['ma20'].values
    contrib = np.zeros(n)
    trades = []
    pos, pending_buy, pending_sell = None, False, False
    for i in range(1, n):
        if pos is not None and pending_sell:
            px = o[i]
            trades.append(dict(entry_i=pos['entry_i'], exit_i=i, entry=pos['entry'],
                               exit=px, h=i - pos['entry_i'],
                               ret=px / pos['entry'] - 1 - ROUND_TRIP))
            contrib[i] += px / c[i - 1] - 1 - FEE_SELL          # 昨收 → 今开
            pos, pending_sell = None, False
        bought = False
        if pos is None and pending_buy and np.isfinite(o[i]):
            pos = dict(entry=o[i], entry_i=i)
            contrib[i] += c[i] / o[i] - 1 - FEE_BUY             # 今开 → 今收
            pending_buy, bought = False, True
        if pos is None:
            if bool(strong.iloc[i]) and bool(dip.iloc[i]):
                pending_buy = True
            continue
        if not bought:
            contrib[i] = c[i] / c[i - 1] - 1
        h = i - pos['entry_i']
        r = c[i] / pos['entry'] - 1
        if sell == 'X1':
            out = r <= -0.08 or r >= 0.20 or c[i] < ma20[i] or h >= 40
        elif sell == 'X2':
            out = r <= -0.05 or r >= 0.10 or c[i] < ma10[i] or h >= 20
        elif sell == 'X3':                                      # 只按均线与时间
            out = c[i] < ma20[i] or h >= 20
        else:                                                   # X4 让利润奔跑：只止损/止盈/时间
            out = r <= -0.08 or r >= 0.30 or h >= 60
        if out and h >= 1:
            pending_sell = True
    posmask = np.zeros(n, dtype=bool)
    for t in trades:
        posmask[t['entry_i']:t['exit_i']] = True
    return trades, contrib, posmask


def run(stock_data, strong_kind, dip_kind, sell_kind, mkt=None, start=None):
    allt, cs, pc = [], None, None
    for code, name, df, ind, fwd in stock_data:
        mk = None if mkt is None else mkt.reindex(df.index).fillna(False)
        tr, contrib, posmask = sim_stock(df, ind, cond_strong(df, ind, strong_kind, mk),
                                         cond_dip(df, ind, dip_kind), sell_kind)
        idx = df.index
        for t in tr:
            t['stock'] = name
            t['date'] = idx[t['entry_i']].date()
            h = min(t['h'], MAXH)
            base = fwd.get(h, np.nan)
            t['base'] = base
            t['edge'] = t['ret'] - base if np.isfinite(base) else np.nan
        if start is not None:
            mask = idx >= pd.Timestamp(start)
            tr = [t for t in tr if pd.Timestamp(t['date']) >= pd.Timestamp(start)]
            contrib = np.where(mask, contrib, 0.0)
            posmask = np.where(mask, posmask, False)
        allt += tr
        k = len(stock_data)
        cser = pd.Series(contrib / k, index=idx)
        pser = pd.Series(posmask.astype(float), index=idx)
        cs = cser if cs is None else cs.add(cser, fill_value=0.0)
        pc = pser if pc is None else pc.add(pser, fill_value=0.0)
    cs, pc = cs.fillna(0.0), pc.fillna(0.0)
    nav_dil = (1 + cs).cumprod()                                  # 每只1/k仓，空闲闲置
    nav_norm = (1 + cs * len(stock_data) / pc.clip(lower=1.0)).cumprod()   # 资金平摊给当日持仓，满仓
    return allt, nav_norm, float(pc.mean()) / len(stock_data)


def summarize(tag, trades, nav, exposure):
    n = len(trades)
    if n == 0:
        print('%-32s %5d %s' % (tag, 0, '无交易'))
        return None
    r = np.array([t['ret'] for t in trades])
    e = np.array([t['edge'] for t in trades if np.isfinite(t['edge'])])
    pos, neg = r[r > 0].sum(), -r[r < 0].sum()
    pf = pos / neg if neg > 0 else float('inf')
    hold = np.mean([t['h'] for t in trades])
    print('%-32s %5d %7.1f%% %+7.2f%% %+7.2f%% %+7.2f%% %6.2f %5.1f %5.1f%% %8.1f%% %+7.1f%% %7.1f%%'
          % (tag, n, 100 * (r > 0).mean(), 100 * r.mean(), 100 * np.median(r),
             100 * (e.mean() if len(e) else np.nan), pf, hold, 100 * exposure,
             100 * (nav.iloc[-1] - 1), 100 * annualized(nav), 100 * max_drawdown(nav)))
    return dict(n=n, win=(r > 0).mean(), avg=r.mean(), med=np.median(r), pf=pf,
                edge=e.mean() if len(e) else np.nan, cum=nav.iloc[-1] - 1)


def sig_huang(df, nth, dif_pos=False):
    """MACD 四色柱黄柱信号：黄柱=绿柱缩头（hist<0且hist>前值），买入=一段连续黄柱的第 nth 根
    卖出=首根蓝柱（hist>0且hist<前值）。dif_pos=True 时要求 DIF>0（对应现有'强势回踩'过滤）"""
    c = df['close']
    dif = ema(c, 12) - ema(c, 26)
    dea = ema(dif, 9)
    hist = 2 * (dif - dea)
    yellow = (hist < 0) & (hist > hist.shift(1))
    blue = (hist > 0) & (hist < hist.shift(1))
    run = np.zeros(len(df), dtype=int)
    cur = 0
    for i, y in enumerate(yellow.fillna(False).values):
        cur = cur + 1 if y else 0
        run[i] = cur
    buy = yellow.fillna(False).values & (run == nth)
    if dif_pos:
        buy = buy & (dif > 0).values
    return pd.Series(buy, index=df.index), blue.fillna(False)


def sim_signal(df, buy_sig, sell_sig):
    n = len(df)
    o, c = df['open'].values, df['close'].values
    contrib = np.zeros(n)
    trades = []
    pos, pending_buy, pending_sell = None, False, False
    for i in range(1, n):
        if pos is not None and pending_sell:
            contrib[i] += o[i] / c[i - 1] - 1 - FEE_SELL
            trades.append(dict(entry_i=pos['entry_i'], exit_i=i, entry=pos['entry'],
                               exit=o[i], h=i - pos['entry_i'],
                               ret=o[i] / pos['entry'] - 1 - ROUND_TRIP))
            pos, pending_sell = None, False
        bought = False
        if pos is None and pending_buy:
            pos = dict(entry=o[i], entry_i=i)
            contrib[i] += c[i] / o[i] - 1 - FEE_BUY
            pending_buy, bought = False, True
        if pos is None:
            if bool(buy_sig.iloc[i]):
                pending_buy = True
            continue
        if not bought:
            contrib[i] = c[i] / c[i - 1] - 1
        if bool(sell_sig.iloc[i]):
            pending_sell = True
    posmask = np.zeros(n, dtype=bool)
    for t in trades:
        posmask[t['entry_i']:t['exit_i']] = True
    return trades, contrib, posmask


def run_signal(stock_data, buy_fn, sell_fn, start=None):
    allt, cs, pc = [], None, None
    for code, name, df, ind, fwd in stock_data:
        b, s = buy_fn(df)
        tr, contrib, posmask = sim_signal(df, b, s)
        idx = df.index
        for t in tr:
            t['stock'] = name
            t['date'] = idx[t['entry_i']].date()
            h = min(t['h'], MAXH)
            base = fwd.get(h, np.nan)
            t['base'], t['edge'] = base, t['ret'] - base if np.isfinite(base) else np.nan
        if start is not None:
            mask = idx >= pd.Timestamp(start)
            tr = [t for t in tr if pd.Timestamp(t['date']) >= pd.Timestamp(start)]
            contrib, posmask = np.where(mask, contrib, 0.0), np.where(mask, posmask, False)
        allt += tr
        k = len(stock_data)
        cser, pser = pd.Series(contrib / k, index=idx), pd.Series(posmask.astype(float), index=idx)
        cs = cser if cs is None else cs.add(cser, fill_value=0.0)
        pc = pser if pc is None else pc.add(pser, fill_value=0.0)
    cs, pc = cs.fillna(0.0), pc.fillna(0.0)
    nav = (1 + cs * len(stock_data) / pc.clip(lower=1.0)).cumprod()
    return allt, nav, float(pc.mean()) / len(stock_data)


def main():
    avg = load('avg_880003.csv')
    mkt_ma20 = (avg['close'] > avg['close'].rolling(20).mean())

    stock_data = []
    for code, name in STOCKS:
        df = load('stock_%s_hfq.csv' % code)
        ind = prep(df)
        o, c = df['open'].values, df['close'].values
        n = len(df)
        fwd = {}
        for h in range(1, MAXH + 1):
            arr = np.full(n, np.nan)
            arr[:n - h] = o[h:] / o[:n - h] - 1 - ROUND_TRIP    # 任意日开盘买、h日后开盘卖
            fwd[h] = float(np.nanmean(arr))                     # 同股、同持有天数的任意入场期望
        stock_data.append((code, name, df, ind, fwd))

    START = '2017-01-01'
    print('=' * 132)
    print('强势股低吸 · 日线回测（%s 起）  标的=515050十大重仓10只AI算力链（后复权）' % START)
    print('信号收盘产生→次日开盘成交；买万1、卖万1+印花税0.05%')
    print('"超额"=本笔净收益 − 同股同持有天数的任意入场期望（剥离个股beta）；净值=资金平摊给当日持仓，满仓口径')
    print('=' * 132)
    print('%-32s %5s %8s %8s %8s %8s %6s %6s %7s %9s %8s %8s'
          % ('组合', '笔数', '胜率', '均收益', '中位', '超额', '盈亏比', '持有天', '平均仓位', '累计', '年化', '回撤'))

    def show(label, sk, dk, xk, mkt=None):
        tr, nav, expo = run(stock_data, sk, dk, xk, mkt=mkt, start=START)
        summarize('   ' + label, tr, nav, expo)
        return tr, nav

    print('-' * 132)
    print('[1] 强势过滤（低吸固定 D1 回踩MA20，卖出 X1）')
    show('S0 无强势过滤（只低吸）', 'S0', 'D1', 'X1')
    show('S1 多头排列', 'S1', 'D1', 'X1')
    show('S2 接近250日新高', 'S2', 'D1', 'X1')
    show('S3 多头排列+接近新高', 'S3', 'D1', 'X1')
    show('S3 + 大盘>MA20 过滤', 'S3', 'D1', 'X1', mkt=mkt_ma20)

    print('\n[2] 低吸触发（强势固定 S3，卖出 X1）')
    show('D0 不择时（强势即买）', 'S3', 'D0', 'X1')
    show('D1 回踩MA20', 'S3', 'D1', 'X1')
    show('D2 近10日回撤≥8%', 'S3', 'D2', 'X1')
    show('D3 KDJ-J<10', 'S3', 'D3', 'X1')
    show('D4 连跌3日后首阳', 'S3', 'D4', 'X1')

    print('\n[3] 卖出规则（强势 S3，低吸取 D0/D2/D3 三种）')
    show('D0+X1 止损8/止盈20/破MA20/40日', 'S3', 'D0', 'X1')
    show('D0+X2 止损5/止盈10/破MA10/20日', 'S3', 'D0', 'X2')
    show('D0+X3 破MA20/最长20日', 'S3', 'D0', 'X3')
    show('D0+X4 只止损8/止盈30/最长60日', 'S3', 'D0', 'X4')
    show('D2+X4 回撤8%+让利润奔跑', 'S3', 'D2', 'X4')
    show('D3+X4 KDJ超卖+让利润奔跑', 'S3', 'D3', 'X4')
    show('D1+X4 回踩MA20+让利润奔跑', 'S3', 'D1', 'X4')
    show('D0+X4 + 大盘>MA20 过滤', 'S3', 'D0', 'X4', mkt=mkt_ma20)
    show('D2+X4 + 大盘>MA20 过滤', 'S3', 'D2', 'X4', mkt=mkt_ma20)

    print('\n[4] 对照：现有 MACD 黄柱体系（同窗口、同费用、同成交口径）')
    for nth in (1, 2, 3, 4):
        tr, nav, expo = run_signal(stock_data, lambda d, n=nth: sig_huang(d, n),
                                   lambda d: sig_huang(d, 1)[1], start=START)
        summarize('   第%d根黄柱+首根蓝柱卖' % nth, tr, nav, expo)
    tr, nav, expo = run_signal(stock_data, lambda d: sig_huang(d, 2, dif_pos=True),
                               lambda d: sig_huang(d, 1)[1], start=START)
    summarize('   第2根黄柱+DIF>0(强势回踩)', tr, nav, expo)

    print('\n[5] 基准（同窗口）')
    rets = []
    for code, name, df, ind, fwd in stock_data:
        d = df.loc[START:]
        nav = d['close'] / d['close'].iloc[0]
        print('   %-10s 纯持有 %+9.1f%%  年化 %6.1f%%  回撤 %7.1f%%'
              % (name, 100 * (nav.iloc[-1] - 1), 100 * annualized(nav), 100 * max_drawdown(nav)))
        rets.append(d['close'].pct_change())
    rr = pd.concat(rets, axis=1)
    bn = (1 + rr.mean(axis=1).fillna(0)).cumprod()
    print('   %-10s 纯持有 %+9.1f%%  年化 %6.1f%%  回撤 %7.1f%%'
          % ('等权篮子10只', 100 * (bn.iloc[-1] - 1), 100 * annualized(bn), 100 * max_drawdown(bn)))

    print('\n[6] 明细：S3+D0+X4 全部交易')
    tr, nav, expo = run(stock_data, 'S3', 'D0', 'X4', start=START)
    tr = sorted(tr, key=lambda t: t['date'])
    print('%-12s %-10s %5s %9s %9s %9s' % ('入场日', '股票', '持有', '本笔净收益', '同股基准', '超额'))
    for t in tr:
        print('%-12s %-10s %5d %+8.2f%% %+8.2f%% %+8.2f%%'
              % (t['date'], t['stock'], t['h'], 100 * t['ret'], 100 * t['base'], 100 * t['edge']))
    print('\n按年（S3+D0+X4）：')
    byyear = {}
    for t in tr:
        byyear.setdefault(t['date'].year, []).append(t)
    print('%-6s %5s %8s %9s %9s %9s %8s' % ('年份', '笔数', '胜率', '均收益', '中位', '超额均值', '基准均'))
    for y in sorted(byyear):
        ts = byyear[y]
        r = np.array([t['ret'] for t in ts])
        e = np.array([t['edge'] for t in ts if np.isfinite(t['edge'])])
        b = np.array([t['base'] for t in ts if np.isfinite(t['base'])])
        print('%-6d %5d %7.1f%% %+8.2f%% %+8.2f%% %+8.2f%% %+7.2f%%'
              % (y, len(ts), 100 * (r > 0).mean(), 100 * r.mean(), 100 * np.median(r),
                 100 * (e.mean() if len(e) else np.nan), 100 * (b.mean() if len(b) else np.nan)))


if __name__ == '__main__':
    main()
