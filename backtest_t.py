#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
通达信 VWAP 偏离日内做T指标 · 5分钟近似回测（159552，T+1 合规，不改任何现有文件）

指标规则（1分钟公式近似到5分钟）：
  日内VWAP = 当日累计AMOUNT / 当日累计VOL / 100，每日开盘重置。
  注：practice.py 的 intraday_vwap 是"收盘价等权平均"近似，与本公式口径不同。
      5分钟CSV无 amount 列，此处用 AMOUNT≈close*VOL*100 还原，即
      VWAP = Σ(close*vol) / Σ(vol)（×100 约掉），与通达信公式一致。
  偏离 = (close - VWAP)/VWAP*100 (%)
  偏离上穿 +thr → T卖信号；偏离下穿 -thr → T买信号（CROSS=首次穿越才触发）
  强偏离过滤（可选）：信号确认时 |偏离| 已超 ±0.8% → 不再逆势开新T
    （卖信号要求 dev<+0.8，买信号要求 dev>-0.8；趋势日停手）

现实约束：
  T+1：始终持有 10000 份底仓；T卖信号卖 1/3 底仓（只能卖隔夜可卖部分），
       T买信号用现金买回；当日买入份额当日锁定不可再卖。
  费用：双边各万1，无印花税。
  信号以 5分钟K线收盘确认，成交价用下一根5分钟K线开盘价（防未来函数）；
  信号在当日最后一根K线出现则丢弃（不隔夜执行）。
  收盘纪律：当日未接回的卖出，在当日最后一根K线收盘价强制接回。

输出：
  1) 基线纯持有收益
  2) 主回测（±0.5%，无过滤）：总收益、做T额外贡献(pp)、次数、胜率、单笔均盈亏、最大单日做T亏损
  3) 参数敏感性：±0.3/±0.5/±0.8 × 强偏离过滤开/关
  4) 天花板：t_optimal.optimal_t 逐日理论最优做T收益合计及捕获率
  5) 分月做T超额收益
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

from t_optimal import optimal_t

FEE = 0.0001                # 单边万1
BASE_SHARES = 10000.0       # 底仓（份）
SELL_SHARES = BASE_SHARES / 3.0   # 每次T卖 1/3 底仓
STRONG = 0.8                # 强偏离参考线 (%)
CODE = '159552'
CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   'data', 'etf_%s_5min.csv' % CODE)


def load_and_dev():
    """加载5分钟数据并计算日内VWAP偏离（%%，每日重置，无前视）"""
    m = pd.read_csv(CSV, parse_dates=['dt']).set_index('dt')
    day = m.index.date
    amt = m['close'] * m['vol']                       # AMOUNT 的比例量
    cum_amt = amt.groupby(day).cumsum()
    cum_vol = m['vol'].groupby(day).cumsum()
    vwap = (cum_amt / cum_vol).where(cum_vol > 0, m['close'])
    m = m.assign(vwap=vwap, dev=(m['close'] / vwap - 1.0) * 100.0)
    return m


def make_signals(m, thr, use_filter):
    """返回 sell_sig/buy_sig（在当根收盘确认的布尔 Series，按日重置的 CROSS）"""
    dev = m['dev']
    prev = dev.groupby(m.index.date).shift(1)         # 日内前一根，跨日为NaN
    sell = (dev > thr) & (prev <= thr)
    buy = (dev < -thr) & (prev >= -thr)
    if use_filter:
        sell &= dev < STRONG       # 偏离已超+0.8%：趋势日，不逆势开新卖T
        buy &= dev > -STRONG       # 偏离已破-0.8%：不逆势接刀
    return sell.fillna(False), buy.fillna(False)


def simulate(m, sell_sig, buy_sig):
    """底仓+做T 逐根模拟（信号当根收盘确认，下一根开盘价成交）。
    返回: equity(净值序列), trips[(卖时,卖价,买时,买价,是否强平)], daily_t_pnl(按日%%)"""
    idx = m.index
    op = m['open'].values
    cl = m['close'].values
    days = m.index.date
    sell_v = sell_sig.values
    buy_v = buy_sig.values
    n = len(m)

    sh_old, sh_new, cash = BASE_SHARES, 0.0, 0.0
    pending = []            # 未接回的卖出: (时间, 价格, 份额)
    trips = []
    equity = np.empty(n)
    day_pnl = {}            # date -> 当日做T净盈亏(元)
    pend_order = []         # 上一根收盘确认、待本根开盘执行: ('sell'/'buy', 信号日)

    cur_day = None
    for i in range(n):
        d = days[i]
        if d != cur_day:                    # 新交易日：解锁昨日买入
            sh_old += sh_new
            sh_new = 0.0
            cur_day = d
        # 1) 本根开盘：执行上一根收盘确认的信号（信号日须为本日，否则丢弃）
        exe, pend_order = pend_order, []
        for kind, sig_day in exe:
            if sig_day != d:
                continue
            px = op[i]
            if kind == 'sell':
                s = min(SELL_SHARES, sh_old)
                if s > 1e-9:
                    sh_old -= s
                    cash += s * px * (1 - FEE)
                    pending.append((idx[i], px, s))
            else:                           # buy：用全部现金接回
                if cash > 1e-9 and pending:
                    buy_px = px
                    got = cash * (1 - FEE) / buy_px
                    sh_new += got
                    cash = 0.0
                    for (st, sp, s) in pending:
                        pnl = s * (sp * (1 - FEE) - buy_px * (1 + FEE))
                        trips.append((st, sp, idx[i], buy_px, False))
                        day_pnl[d] = day_pnl.get(d, 0.0) + pnl
                    pending = []
        # 2) 当日最后一根：未接回的按收盘价强制接回（收盘纪律）
        last_bar = (i == n - 1) or (days[i + 1] != d)
        if last_bar and cash > 1e-9 and pending:
            buy_px = cl[i]
            got = cash * (1 - FEE) / buy_px
            sh_new += got
            cash = 0.0
            for (st, sp, s) in pending:
                pnl = s * (sp * (1 - FEE) - buy_px * (1 + FEE))
                trips.append((st, sp, idx[i], buy_px, True))
                day_pnl[d] = day_pnl.get(d, 0.0) + pnl
            pending = []
        # 3) 本根收盘：确认信号 → 下一根开盘执行（当日最后一根不排单）
        if not last_bar:
            if sell_v[i]:
                pend_order.append(('sell', d))
            elif buy_v[i]:
                pend_order.append(('buy', d))
        equity[i] = (sh_old + sh_new) * cl[i] + cash

    equity = pd.Series(equity, index=idx) / equity[0]
    # 单日做T盈亏换算成占当日开盘底仓市值的%
    day_open_val = pd.Series(op, index=idx).groupby(days).first() * BASE_SHARES
    daily_pct = pd.Series(day_pnl) / day_open_val * 100.0
    return equity, trips, daily_pct


def trip_stats(trips):
    """(次数, 胜率%%, 单笔均盈亏%%, 强平占比%%)；单笔盈亏=(卖净得-买净付)/卖市值"""
    if not trips:
        return 0, 0.0, 0.0, 0.0
    pnl_pct = [(sp * (1 - FEE) - bp * (1 + FEE)) / sp * 100
               for (_, sp, _, bp, _) in trips]
    wins = sum(1 for x in pnl_pct if x > 0)
    forced = sum(1 for t in trips if t[4])
    return len(trips), wins / len(trips) * 100, float(np.mean(pnl_pct)), forced / len(trips) * 100


def main():
    m = load_and_dev()
    close = m['close']
    days = sorted(set(m.index.date))
    n_days = len(days)
    years = (m.index[-1] - m.index[0]).days / 365.25
    hold_ret = close.iloc[-1] / close.iloc[0] - 1

    print('=' * 74)
    print('数据: %s 5分钟  %s ~ %s，%d 个交易日（%.2f 年）'
          % (CODE, days[0], days[-1], n_days, years))
    print('1) 基线：纯持有 区间收益 %+.2f%%（年化 %+.2f%%）'
          % (hold_ret * 100, ((1 + hold_ret) ** (1 / years) - 1) * 100))
    print('=' * 74)

    # ---- 跑全部 6 组参数 ----
    results = {}
    for thr in (0.3, 0.5, 0.8):
        for flt in (False, True):
            sell_sig, buy_sig = make_signals(m, thr, flt)
            equity, trips, daily_pct = simulate(m, sell_sig, buy_sig)
            results[(thr, flt)] = (equity, trips, daily_pct)

    # ---- 2) 主回测：±0.5%，无过滤 ----
    equity, trips, daily_pct = results[(0.5, False)]
    total_ret = equity.iloc[-1] - 1
    extra_pp = (total_ret - hold_ret) * 100
    n_tr, wr, avg, forced = trip_stats(trips)
    max_day_loss = daily_pct.min() if len(daily_pct) else 0.0
    print('\n2) 主回测：底仓10000份 + VWAP±0.5%%穿越做T（卖/买各1/3底仓，无过滤）')
    print('   区间总收益(底仓+做T): %+.2f%%（年化 %+.2f%%）'
          % (total_ret * 100, ((1 + total_ret) ** (1 / years) - 1) * 100))
    print('   做T额外贡献: %+.2fpp（年化约 %+.2fpp）' % (extra_pp, extra_pp / years))
    print('   做T次数: %d（其中收盘强平接回 %.0f%%）  胜率: %.1f%%  单笔均盈亏: %+.3f%%'
          % (n_tr, forced, wr, avg))
    print('   最大单日做T亏损: %+.2f%%（占当日底仓市值）' % max_day_loss)

    # ---- 3) 参数敏感性 ----
    print('\n3) 参数敏感性（全窗口，做T额外贡献 pp / 年化 pp）')
    print('%-22s %9s %9s %6s %7s %9s %9s'
          % ('参数', '额外pp', '年化pp', '次数', '胜率', '单笔均%', '最大日亏%'))
    for thr in (0.3, 0.5, 0.8):
        for flt in (False, True):
            eq, tr, dp = results[(thr, flt)]
            ex = (eq.iloc[-1] - 1 - hold_ret) * 100
            nt, w, a, _ = trip_stats(tr)
            mdl = dp.min() if len(dp) else 0.0
            if not np.isfinite(mdl):
                mdl = 0.0
            print('%-22s %+8.2f %+8.2f %6d %6.1f%% %+8.3f %+8.2f'
                  % ('±%.1f%% %s' % (thr, '含强偏离过滤' if flt else '无过滤      '),
                     ex, ex / years, nt, w, a, mdl))

    # ---- 4) 天花板：逐日 optimal_t ----
    print('\n4) 天花板参照（t_optimal.optimal_t 逐日DP，全仓进出后见之明）')
    ceil_extra = 0.0
    for d in days:
        dc = close[m.index.date == d]
        opt_ret, _ = optimal_t(dc)
        hold_d = dc.iloc[-1] / dc.iloc[0] - 1
        ceil_extra += max(opt_ret - hold_d, 0.0)
    # optimal_t 是全仓口径，本策略单次只用1/3底仓，给出两种捕获率
    cap = extra_pp / 100 / ceil_extra * 100 if ceil_extra > 0 else 0.0
    print('   逐日理论最优做T合计(相对当日持有): %+.2fpp（年化 %+.2fpp）'
          % (ceil_extra * 100, ceil_extra * 100 / years))
    print('   本策略捕获率: %.1f%%（注意：天花板按全仓进出计，本策略单次仅1/3底仓，'
          '满仓口径捕获率≈%.0f%%）' % (cap, cap / 3))

    # ---- 5) 分月统计 ----
    print('\n5) 分月做T超额收益（主参数 ±0.5%% 无过滤；策略月收益 - 持有月收益，pp）')
    month = pd.Series(m.index.strftime('%Y-%m'), index=m.index)
    hold_curve = close / close.iloc[0]
    df = pd.DataFrame({'策略': equity, '持有': hold_curve})
    monthly = df.groupby(month).last() / df.groupby(month).first() - 1
    ex_m = (monthly['策略'] - monthly['持有']) * 100
    out = pd.DataFrame({'做T超额pp': ex_m.round(2),
                        '持有月收益%': (monthly['持有'] * 100).round(2)})
    print(out.to_string())
    pos = (ex_m > 0).sum()
    print('正超额月份: %d/%d（%.0f%%），月均 %+.2fpp，最差月 %+.2fpp，最好月 %+.2fpp'
          % (pos, len(ex_m), pos / len(ex_m) * 100, ex_m.mean(), ex_m.min(), ex_m.max()))


if __name__ == '__main__':
    main()
