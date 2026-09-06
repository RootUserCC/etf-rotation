#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
T+1 合规的"底仓 + 日内高抛低吸"回测（5分钟K线，2024-08 ~ 2026-08）

模型：
  - 始终满仓思路：底仓 = 全部份额，当日买入份额当日锁定不可卖
  - 超买信号 → 目标仓位降至 50%（卖出可卖底仓的一半）
  - 超卖信号 → 目标仓位回到 100%（用现金接回，接回部分当日锁定）
  - 若底仓当日已锁（上午卖、下午买回后又出超买信号），只能卖剩余可卖部分
  - 每次卖/买各扣单边费（万1 / 千一 两档）

规则（信号都基于持仓标的自身的 5 分钟K线，无前视，当根收盘成交）：
  T1 MACD  : 5分MACD死叉→50%，金叉→100%（最接近你实盘的做法）
  T2 RSI   : RSI(14)>70→50%，RSI(14)<40→100%
  T3 VWAP  : 收盘>当日VWAP×1.01→50%，收盘<VWAP→100%

评价口径：相对"全程持有不动"的超额收益（即摊薄增强）、
完整T回合数、回合胜率（接回价<卖出价）、平均每回合收益。
"""
import sys
import io
import os

import numpy as np
import pandas as pd

if sys.platform == 'win32' and not getattr(sys.stdout, '_utf8_wrapped', False):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stdout._utf8_wrapped = True

from t_pattern import rsi, intraday_vwap
from t_ideal import ema
from t_pattern_long import DATA_DIR
from t_pattern_t1 import load_5min

TARGET_LOW = 0.5    # 超卖买回前的目标仓位


def signals_macd(close):
    dif = ema(close, 12) - ema(close, 26)
    dea = ema(dif, 9)
    sell = (dif < dea) & (dif.shift(1) >= dea.shift(1))     # 死叉
    buy = (dif > dea) & (dif.shift(1) <= dea.shift(1))      # 金叉
    return buy.fillna(False), sell.fillna(False)


def signals_rsi(close):
    r = rsi(close, 14)
    return (r < 40).fillna(False), (r > 70).fillna(False)


def signals_vwap(close):
    v = intraday_vwap(close)
    return (close < v).fillna(False), (close > v * 1.01).fillna(False)


RULES = {'T1 5分MACD': signals_macd, 'T2 RSI(70/40)': signals_rsi,
         'T3 VWAP偏离1%': signals_vwap}


def simulate(close: pd.Series, buy_sig, sell_sig, fee_side=0.0001):
    """底仓+日内T模拟。份额记账：sh_old(可卖)/sh_new(锁定) + cash。
    返回 nav 序列、完整T回合列表 [(卖价, 买回价)]。"""
    n = len(close)
    px = close.values
    buy_v = buy_sig.values
    sell_v = sell_sig.values
    days = pd.Series(close.index.date, index=close.index)

    sh_old, sh_new, cash = 1.0 / px[0], 0.0, 0.0   # 初始全仓，净值1
    target = 1.0
    nav = np.empty(n)
    cur_day = None
    trips = []          # (卖价, 买回价)
    pending_sell_px = None

    for i in range(n):
        d = days.iloc[i]
        if d != cur_day:
            sh_old += sh_new
            sh_new = 0.0
            cur_day = d
        price = px[i]
        total_val = (sh_old + sh_new) * price + cash

        if sell_v[i] and target == 1.0:
            target = TARGET_LOW
            sell_val = total_val * (1 - TARGET_LOW)          # 要减出的市值
            sell_sh = sell_val / price
            sell_sh = min(sell_sh, sh_old)                   # 只能卖可卖部分
            if sell_sh > 1e-12:
                sh_old -= sell_sh
                cash += sell_sh * price * (1 - fee_side)
                pending_sell_px = price
        elif buy_v[i] and target < 1.0:
            target = 1.0
            if cash > 1e-9:
                buy_val = cash * (1 - fee_side)
                sh_new += buy_val / price
                if pending_sell_px is not None:
                    trips.append((pending_sell_px, price))
                    pending_sell_px = None
                cash = 0.0
        nav[i] = (sh_old + sh_new) * price + cash

    return pd.Series(nav, index=close.index) / nav[0], trips


def analyze(name, code, market_note=''):
    m = load_5min(code)
    close = m['close']
    hold_ret = close.iloc[-1] / close.iloc[0] - 1
    print('===== %s (%s)  区间 %s ~ %s，持有不动 %+.2f%% ====='
          % (name, code, close.index[0].date(), close.index[-1].date(), hold_ret * 100))
    print('%-16s %8s %8s %6s %6s %8s %8s'
          % ('规则', '净值收益', '超额', '回合', '胜率', '回合均赚', '净@千一超额'))
    for rname, fn in RULES.items():
        buy_sig, sell_sig = fn(close)
        nav, trips = simulate(close, buy_sig, sell_sig, fee_side=0.0001)
        nav2, _ = simulate(close, buy_sig, sell_sig, fee_side=0.0005)
        ret = nav.iloc[-1] - 1
        excess = ret - hold_ret
        excess2 = (nav2.iloc[-1] - 1) - hold_ret
        wins = sum(1 for s, b in trips if b < s)
        n_tr = len(trips)
        win_rate = wins / n_tr * 100 if n_tr else 0.0
        avg = np.mean([(s - b) / s for s, b in trips]) * 100 if n_tr else 0.0
        print('%-16s %+7.2f%% %+7.2f%% %6d %5.0f%% %+7.2f%% %+8.2f%%'
              % (rname, ret * 100, excess * 100, n_tr, win_rate, avg, excess2 * 100))
    print()


def main():
    analyze('2000增强ETF', '159552')
    analyze('红利低波ETF', '512890')


if __name__ == '__main__':
    main()
