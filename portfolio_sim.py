#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""组合级回测：2026-01-01 起，全池 54 只按黄柱策略实战模拟。

规则（与目前推荐一致）：
  买：第 3 根黄柱买半仓，同一段走到第 4 根再加半仓（收盘成交）
  卖：首根蓝柱收盘清仓；最长持有 120 个交易日强制清
  仓位：总本金 100 万，5 个槽位，每槽 20 万（半仓=10 万），没空槽的信号放弃
  费用：单边按成交额 0.08% 估（佣金+印花税+滑点打包），可关

输出：期末收益、最大回撤、成交明细，对比同期沪深300ETF/上证指数。
数据：data/ai_stocks/ 缓存；基准 data/etf_510300_hfq.csv、data/index_000001.csv
"""
import os

import pandas as pd

import ai_yellow_bar as ay

ROOT = os.path.dirname(os.path.abspath(__file__))
START = pd.Timestamp('2026-01-01')
CAPITAL = 1_000_000.0
SLOTS = 5                 # 每槽满仓 = CAPITAL/SLOTS；第3根先用半个槽
MAXD = 120
FEE = 0.0008              # 单边费用率，0 表示不计费


def load_pool():
    pool = {}
    for code, name, _ in ay.load_stock_list():
        path = os.path.join(ay.CACHE, '%s.csv' % code)
        if not os.path.exists(path):
            continue
        df = pd.read_csv(path, parse_dates=['date'], index_col='date')
        p = ay.prepare(df)
        pool[code] = {'name': name, 'p': p,
                      'close': pd.Series(p['closes'], index=p['idx'])}
    return pool


def run_portfolio(pool, tier34=True, fee=FEE):
    """tier34=True：3根半仓+4根半仓；False：严格第4根一把满仓"""
    days = sorted(set().union(*[set(v['close'].index) for v in pool.values()]))
    days = [d for d in days if d >= START]
    cash = CAPITAL
    pos = {}   # code -> {'entries': [(price, amt)], 'held': 已持有bar数, 'buy_day': 首次买入日}
    trades, skipped = [], 0
    equity_curve = []
    last_close = {}

    for day in days:
        # --- 先卖 ---
        for code in list(pos):
            s = pool[code]['close']
            if day not in s.index:
                continue
            i = s.index.get_loc(day)
            pos[code]['held'] += 1
            p = pool[code]['p']
            if p['blue'][i] or pos[code]['held'] >= MAXD:
                amt = sum(a for _, a in pos[code]['entries'])
                val = sum(a * s.iloc[i] / e for e, a in pos[code]['entries'])
                cash += val * (1 - fee)
                ret = val / amt - 1
                trades.append((code, pool[code]['name'], pos[code]['buy_day'], day,
                               ret, amt, list(pos[code]['entries'])))
                del pos[code]
        # --- 再买 ---
        for code, v in pool.items():
            s = v['close']
            if day not in s.index:
                continue
            i = s.index.get_loc(day)
            run = v['p']['run'][i]
            price = s.iloc[i]
            unit = CAPITAL / SLOTS / 2          # 半仓金额
            if code in pos:
                if run == 4 and len(pos[code]['entries']) == 1:
                    if cash >= unit:
                        cash -= unit * (1 + fee)
                        pos[code]['entries'].append((price, unit))
                continue
            want = (run == 3 and tier34) or (run == (3 if tier34 else 4))
            if want:
                amt = unit if tier34 else unit * 2
                if cash >= amt:
                    cash -= amt * (1 + fee)
                    pos[code] = {'entries': [(price, amt)], 'held': 0, 'buy_day': day}
                else:
                    skipped += 1
        # --- 记净值 ---
        for code, v in pool.items():
            s = v['close']
            if day in s.index:
                last_close[code] = s.loc[day]
        mkt = sum(a * last_close[code] / e for code, po in pos.items() for e, a in po['entries'])
        equity_curve.append((day, cash + mkt))

    eq = pd.Series(dict(equity_curve)).sort_index()
    ret = eq.iloc[-1] / CAPITAL - 1
    dd = (eq / eq.cummax() - 1).min()
    return eq, trades, skipped, ret, dd, pos


def bench(path, col='close'):
    df = pd.read_csv(path, parse_dates=['date'], index_col='date')
    s = df[col].loc[lambda x: x.index >= START]
    return s.iloc[-1] / s.iloc[0] - 1, s.index[0].date(), s.index[-1].date()


def main():
    pool = load_pool()
    print('股票池 %d 只 | 本金 %d 万 | 槽位 %d × %d 万 | 费用单边 %.2f%%\n'
          % (len(pool), CAPITAL / 1e4, SLOTS, CAPITAL / SLOTS / 1e4, FEE * 100))

    variants = [('分批 3根半仓+4根半仓', True), ('严格 第4根一把满仓', False)]
    results = {}
    for tag, t34 in variants:
        for ftag, fee in [('扣费', FEE), ('毛收益', 0.0)]:
            eq, trades, skipped, ret, dd, pos = run_portfolio(pool, t34, fee)
            wins = sum(1 for t in trades if t[4] > 0)
            print('【%s · %s】期末 %+.2f%% | 最大回撤 %.2f%% | 清仓 %d 笔(胜率 %.0f%%) | 持仓中 %d 只 | 因满仓错过 %d 个信号'
                  % (tag, ftag, ret * 100, dd * 100, len(trades),
                     wins / len(trades) * 100 if trades else 0, len(pos), skipped))
            if ftag == '扣费':
                results[tag] = dict(eq=eq, trades=trades, pos=pos)
        print()

    for tag, path in [('沪深300ETF(后复权)', 'data/etf_510300_hfq.csv'),
                      ('上证指数', 'data/index_000001.csv'),
                      ('创业板指', 'data/index_399006.csv')]:
        r, d0, d1 = bench(os.path.join(ROOT, path))
        print('基准 %s：%+.2f%%（%s ~ %s）' % (tag, r * 100, d0, d1))

    export_detail(pool, results)
    plot_equity(results)


def _entries_str(entries):
    return ' + '.join('%.0f万@%.2f' % (a / 1e4, e) for e, a in entries)


def export_detail(pool, results):
    """已平仓明细 + 当前持仓 + 收益曲线 → data/portfolio_*.csv"""
    trade_rows, open_rows = [], []
    for tag, r in results.items():
        for code, name, buy_day, sell_day, ret, amt, entries in r['trades']:
            trade_rows.append({
                '方案': tag, '代码': code, '名称': name,
                '买入日': str(buy_day.date()), '卖出日': str(sell_day.date()),
                '持有天数': (sell_day - buy_day).days, '投入(万)': round(amt / 1e4, 1),
                '收益率%': round(ret * 100, 2), '盈亏(万)': round(amt * ret / 1e4, 2),
                '分批明细': _entries_str(entries)})
        for code, po in r['pos'].items():
            s = pool[code]['close']
            last = s.iloc[-1]
            amt = sum(a for _, a in po['entries'])
            val = sum(a * last / e for e, a in po['entries'])
            open_rows.append({
                '方案': tag, '代码': code, '名称': pool[code]['name'],
                '买入日': str(po['buy_day'].date()), '持有天数': (s.index[-1] - po['buy_day']).days,
                '投入(万)': round(amt / 1e4, 1), '现价': round(float(last), 2),
                '浮动盈亏%': round((val / amt - 1) * 100, 2), '分批明细': _entries_str(po['entries'])})
    pd.DataFrame(trade_rows).to_csv(os.path.join(ROOT, 'data', 'portfolio_trades.csv'),
                                    index=False, encoding='utf-8-sig')
    pd.DataFrame(open_rows).to_csv(os.path.join(ROOT, 'data', 'portfolio_open.csv'),
                                   index=False, encoding='utf-8-sig')
    eq_df = pd.DataFrame({tag: r['eq'] / CAPITAL - 1 for tag, r in results.items()})
    eq_df.to_csv(os.path.join(ROOT, 'data', 'portfolio_equity.csv'), encoding='utf-8-sig',
                 index_label='date')
    print('\n已导出 data/portfolio_trades.csv（清仓 %d 笔）、data/portfolio_open.csv（持仓 %d 只）、data/portfolio_equity.csv'
          % (len(trade_rows), len(open_rows)))

    for tag, r in results.items():
        print('\n===== %s · 当前持仓 =====' % tag)
        for o in [x for x in open_rows if x['方案'] == tag]:
            print('  %s %s | 买 %s | 投入 %.1f 万 | 浮动盈亏 %+.2f%% | %s'
                  % (o['名称'], o['代码'], o['买入日'], o['投入(万)'], o['浮动盈亏%'], o['分批明细']))


def plot_equity(results):
    """收益曲线：两方案 + 沪深300ETF 归一化对比，附回撤子图"""
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    plt.rcParams['font.sans-serif'] = ['Microsoft YaHei', 'SimHei']
    plt.rcParams['axes.unicode_minus'] = False

    hs300 = pd.read_csv(os.path.join(ROOT, 'data', 'etf_510300_hfq.csv'),
                        parse_dates=['date'], index_col='date')['close']
    hs300 = hs300.loc[lambda x: x.index >= START]
    hs300 = hs300 / hs300.iloc[0] - 1

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(11, 7), sharex=True,
                                   gridspec_kw={'height_ratios': [3, 1]})
    for tag, r in results.items():
        eq = r['eq'] / CAPITAL - 1
        ax1.plot(eq.index, eq.values * 100, label='%s (%+.1f%%)' % (tag, eq.iloc[-1] * 100), lw=1.4)
        dd = (r['eq'] / r['eq'].cummax() - 1) * 100
        ax2.plot(dd.index, dd.values, lw=1.0, label=tag)
    ax1.plot(hs300.index, hs300.values * 100, '--', color='gray',
             label='沪深300ETF (%+.1f%%)' % (hs300.iloc[-1] * 100), lw=1.1)
    ax1.axhline(0, color='black', lw=0.6, alpha=0.5)
    ax1.set_ylabel('累计收益率 %')
    first_eq = results[next(iter(results))]['eq']
    ax1.set_title('黄柱策略组合模拟（本金100万 · 5槽位 · 扣费）%s ~ %s'
                  % (START.date(), first_eq.index[-1].date()))
    ax1.legend(fontsize=9)
    ax1.grid(alpha=0.3)
    ax2.set_ylabel('回撤 %')
    ax2.grid(alpha=0.3)
    ax2.legend(fontsize=8)
    fig.tight_layout()
    out = os.path.join(ROOT, 'portfolio_equity.png')
    fig.savefig(out, dpi=120)
    print('\n收益曲线已存 %s' % out)


if __name__ == '__main__':
    main()
