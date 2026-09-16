#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""红柱策略止损网格：第 2 根红柱买，止盈 TP% + 止损 SL% + 最长 60 日。

第一部分：逐笔统计（胜率/平均盈亏/盈亏比/收益因子），与 ai_yellow_bar.py 口径一致；
第二部分：40w 组合模拟（10 仓×4w，与黄柱第4根策略共用仓位），看最大回撤。
数据：data/ai_stocks/ 缓存（不复权）。
"""
import os

import pandas as pd

import ai_yellow_bar as ay

CAP = 400000
SLOTS = 10


def load_data():
    stocks = ay.load_stock_list()
    data = {}
    for code, name, _ in stocks:
        path = os.path.join(ay.CACHE, '%s.csv' % code)
        if os.path.exists(path):
            data[code] = pd.read_csv(path, parse_dates=['date'], index_col='date')
    return data


def red_trades(p, tp, sl, maxd=60):
    """第 2 根红柱买，止盈 tp / 止损 sl / 满 maxd 日，返回交易列表"""
    idx, closes, run = p['idx'], p['closes'], p['red_run']
    trades = []
    i = 60
    while i < len(closes):
        if run[i] == 2 and idx[i] >= ay.SIGNAL_START:
            entry = closes[i]
            j = i + 1
            exit_j = len(closes) - 1
            while j < len(closes):
                c = closes[j]
                if sl and c <= entry * (1 - sl):
                    exit_j = j
                    break
                if tp and c >= entry * (1 + tp):
                    exit_j = j
                    break
                if j - i >= maxd:
                    exit_j = j
                    break
                j += 1
            trades.append((idx[i], idx[exit_j], closes[exit_j] / entry - 1))
            i = exit_j + 1
        else:
            i += 1
    return trades


def build_events(data, tp, sl):
    """黄柱(第4根/蓝柱) + 红柱(第2根/止盈止损) 的每日事件表"""
    events = {}
    for code, df in data.items():
        p = ay.prepare(df)
        idx, closes = p['idx'], p['closes']
        for d, _, _ in []:
            pass
        # 黄柱线
        i = 60
        while i < len(idx):
            if p['run'][i] == 4 and idx[i] >= ay.SIGNAL_START:
                events.setdefault(idx[i], []).append((code, 'buy_y'))
                j = i + 1
                while j < len(idx):
                    if p['blue'][j] or j - i >= 120:
                        events.setdefault(idx[j], []).append((code, 'sell_y'))
                        break
                    j += 1
                i = (j if j < len(idx) else len(idx) - 1) + 1
            else:
                i += 1
        # 红柱线
        i = 60
        while i < len(idx):
            if p['red_run'][i] == 2 and idx[i] >= ay.SIGNAL_START:
                events.setdefault(idx[i], []).append((code, 'buy_r'))
                entry = closes[i]
                j = i + 1
                while j < len(idx):
                    c = closes[j]
                    if (sl and c <= entry * (1 - sl)) or (tp and c >= entry * (1 + tp)) or j - i >= 60:
                        events.setdefault(idx[j], []).append((code, 'sell_r'))
                        break
                    j += 1
                i = (j if j < len(idx) else len(idx) - 1) + 1
            else:
                i += 1
    return events


def run_port(data, events, slots=SLOTS, cap=CAP):
    cash, pos = cap, {}
    slot_amt = cap / slots
    eq_curve = []
    days = sorted(set(d for df in data.values() for d in df.index if d >= ay.SIGNAL_START))
    for d in days:
        for code, ev in events.get(d, []):
            key = code + '_' + ev[-1]
            if ev.startswith('sell'):
                if key in pos:
                    amt, entry = pos.pop(key)
                    price = data[code]['close'].get(d)
                    if price is not None:
                        cash += amt * price / entry
            else:
                if key in pos or cash < slot_amt:
                    continue
                pos[key] = (slot_amt, data[code]['close'].get(d))
                cash -= slot_amt
        mtm = sum(amt * data[k[:6]]['close'].loc[:d].iloc[-1] / entry
                  for k, (amt, entry) in pos.items() if d in data[k[:6]].index)
        eq_curve.append((d, cash + mtm))
    eq = pd.Series([e[1] for e in eq_curve], index=[e[0] for e in eq_curve])
    dd = (eq / eq.cummax() - 1).min()
    return eq.iloc[-1], dd


def main():
    data = load_data()
    prep = {code: ay.prepare(df) for code, df in data.items()}
    print('股票池 %d 只，信号起始 %s\n' % (len(data), ay.SIGNAL_START.date()))

    # ===== 第一部分：止损网格（逐笔统计） =====
    print('===== 红柱第2根：止盈 x 止损 网格（逐笔） =====')
    print('%-8s %-8s %6s %8s %9s %8s %8s %8s' % ('止盈', '止损', '交易数', '胜率', '平均盈亏', '盈亏比', '收益因子', '最大单笔亏'))
    results = []
    for tp in [0.05, 0.08, 0.10]:
        for sl in [None, 0.03, 0.05, 0.08, 0.10, 0.15]:
            all_trades = []
            for code, p in prep.items():
                all_trades += red_trades(p, tp, sl)
            c, w, m, pl, pf = ay.stats(all_trades)
            worst = min((t[2] for t in all_trades), default=0)
            results.append((tp, sl, c, w, m, pl, pf, worst))
            print('%-8s %-8s %6d %7.1f%% %8.2f%% %8.2f %8.2f %7.1f%%'
                  % ('%d%%' % (tp * 100), ('%d%%' % (sl * 100)) if sl else '无',
                     c, w * 100, m * 100, pl, pf, worst * 100))

    # ===== 第二部分：组合模拟（黄4 + 红2带止损，10仓×4w） =====
    print('\n===== 组合模拟：黄柱第4根 + 红柱第2根（止盈8%%），40w / %d仓 =====' % SLOTS)
    print('%-8s %10s %8s %8s' % ('红柱止损', '期末权益', '收益率', '最大回撤'))
    for sl in [None, 0.03, 0.05, 0.08, 0.10, 0.15]:
        events = build_events(data, 0.08, sl)
        final, dd = run_port(data, events)
        print('%-8s %10.0f %7.1f%% %7.1f%%'
              % (('%d%%' % (sl * 100)) if sl else '无', final, (final / CAP - 1) * 100, dd * 100))


if __name__ == '__main__':
    main()
