#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
信号源对比实验：基准(880003 平均股价) vs 进攻腿自身(etf_atk 拼接) MACD-DIF 拐点信号

基准: calc_signals(avg['close'], sell_anywhere=True, fast=17, slow=34, sig_n=9)
变体: calc_signals(etf_atk['close'], sell_anywhere=True, fast/slow 五组, sig_n=9)
      + 最优 fast/slow 下 buy_anywhere=True（DIF 拐头即买，不要求零下）
所有变体通过 run_backtest 的 signals= 注入，执行/费用口径与基准完全一致
（T 日收盘信号，T+1 开盘成交，双边万1）。
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

from backtest import run_backtest, calc_signals, annualized, max_drawdown

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
FEE = 0.0001
SWITCH = pd.Timestamp('2024-06-28')

PARAMS = [(12, 26), (17, 34), (8, 21), (10, 22), (24, 52)]


def load(name: str) -> pd.DataFrame:
    df = pd.read_csv(os.path.join(DATA_DIR, name), parse_dates=['date'])
    df = df.set_index('date').sort_index()
    df.index = df.index.normalize()
    return df


def round_trips(res, etf_atk):
    """把 trades 配成 (买入日,卖出日,持仓交易日数,进攻腿收益) 的回合列表"""
    idx = res['idx']
    trips = []
    entry = None
    for d, action, price in res['trades']:
        if action.startswith('买入'):
            entry = (d, price)
        elif action.startswith('卖出') and entry is not None:
            exit_px = float(etf_atk.loc[d, 'open'])
            hold_days = idx.get_loc(d) - idx.get_loc(entry[0])
            ret = exit_px / entry[1] - 1 - 2 * FEE
            trips.append((entry[0], d, hold_days, ret))
            entry = None
    return trips


def face_slaps(trips):
    """≤5 个交易日且亏损的回合：数量与合计收益"""
    slaps = [t for t in trips if t[2] <= 5 and t[3] < 0]
    return len(slaps), sum(t[3] for t in slaps)


def cross_switch(res):
    """2024-06-28 拼接切换日是否跨界持有进攻仓"""
    h = res['hold1000']
    pre = h[h.index < SWITCH]
    post = h[h.index >= SWITCH]
    if len(pre) and len(post):
        return bool(pre.iloc[-1] and post.iloc[0])
    return False


def period_ret(nav, start):
    seg = nav[nav.index >= pd.Timestamp(start)]
    if len(seg) < 2:
        return float('nan')
    base = nav[nav.index < pd.Timestamp(start)]
    prev = base.iloc[-1] if len(base) else nav.iloc[0]
    return float(seg.iloc[-1] / prev - 1)


def yearly_table(nav):
    y = nav.resample('YE').last()
    out = y.pct_change()
    out.iloc[0] = y.iloc[0] / nav.iloc[0] - 1
    return out


def summarize(name, res, etf_atk):
    nav = res['nav_strat']
    trips = round_trips(res, etf_atk)
    n_slap, slap_ret = face_slaps(trips)
    return {
        'name': name,
        'cum': (nav.iloc[-1] - 1) * 100,
        'ann': annualized(nav) * 100,
        'mdd': max_drawdown(nav) * 100,
        'trades': len(res['trades']),
        'trips': trips,
        'n_slap': n_slap,
        'slap_ret': slap_ret * 100,
        'ytd26': period_ret(nav, '2026-01-01') * 100,
        'sep26': period_ret(nav, '2026-09-01') * 100,
        'yearly': yearly_table(nav),
        'cross': cross_switch(res),
        'nav': nav,
    }


def buy_anywhere_signals(close, fast, slow, sig_n=9):
    """DIF 拐头即买（不要求零下）、DIF 转降即卖——基于 calc_signals 的 dif 重算"""
    df = calc_signals(close, sell_anywhere=True, fast=fast, slow=slow, sig_n=sig_n)
    dif = df['dif']
    d1, d2 = dif.shift(1), dif.shift(2)
    out = df[['buy_sig', 'sell_sig']].copy()
    out['buy_sig'] = ((d1 < d2) & (dif > d1)).fillna(False)
    return out


def lead_lag(base_sig, atk_sig, window=10):
    """对每个基准买入信号，找 ±window 个交易日内最近的变体买入信号，返回天数差列表
    （正 = 变体信号滞后于基准，负 = 变体提前）"""
    bidx = base_sig.index[base_sig['buy_sig']]
    aidx = atk_sig.index[atk_sig['buy_sig']]
    diffs = []
    for d in bidx:
        near = aidx[np.abs(aidx - d) <= pd.Timedelta(days=window * 2)]
        if len(near):
            diffs.append((near[np.argmin(np.abs(near - d))] - d).days)
    return diffs


def main():
    avg = load('avg_880003.csv')
    etf1000 = load('etf_512100_hfq.csv')
    etf2000e = load('etf_159552_hfq.csv')
    etfdiv = load('etf_512890_hfq.csv')
    etf_atk = pd.concat([etf1000[etf1000.index < SWITCH],
                         etf2000e[etf2000e.index >= SWITCH]])

    lines = []

    def out(s=''):
        print(s)
        lines.append(s)

    # ---- 基准 ----
    base = run_backtest(avg, etf_atk, etfdiv, fee=FEE, sell_anywhere=True,
                        fast=17, slow=34, sig_n=9, label='基准(880003)', verbose=False)
    sb = summarize('基准 880003 (17,34)', base, etf_atk)
    base_sig = calc_signals(avg['close'], sell_anywhere=True, fast=17, slow=34, sig_n=9)

    out('回测区间: %s ~ %s  共 %d 个交易日' % (base['idx'][0].date(), base['idx'][-1].date(), len(base['idx'])))
    out('基准锚点: 累计 %.2f%%  年化 %.2f%%  最大回撤 %.2f%%  换仓 %d 次'
        % (sb['cum'], sb['ann'], sb['mdd'], sb['trades']))
    out('')

    # ---- 变体：进攻腿自身信号，五组参数 ----
    summaries = [sb]
    variants = {}
    for f, s in PARAMS:
        sig = calc_signals(etf_atk['close'], sell_anywhere=True, fast=f, slow=s, sig_n=9)
        r = run_backtest(avg, etf_atk, etfdiv, fee=FEE, signals=sig,
                         label='atk(%d,%d)' % (f, s), verbose=False)
        name = '进攻腿自身 (%d,%d,9)' % (f, s)
        summaries.append(summarize(name, r, etf_atk))
        variants[name] = (sig, r)
        print('跑完 %s: 年化 %.2f%%' % (name, summaries[-1]['ann']))

    # 最优 fast/slow（按年化）+ buy_anywhere
    best = max(summaries[1:], key=lambda x: x['ann'])
    bf, bs = [int(x) for x in best['name'].split('(')[1].split(')')[0].split(',')[:2]]
    sig_ba = buy_anywhere_signals(etf_atk['close'], bf, bs)
    r_ba = run_backtest(avg, etf_atk, etfdiv, fee=FEE, signals=sig_ba,
                        label='atk(%d,%d)+buy_anywhere' % (bf, bs), verbose=False)
    name_ba = '进攻腿 (%d,%d)+拐头即买' % (bf, bs)
    summaries.append(summarize(name_ba, r_ba, etf_atk))
    variants[name_ba] = (sig_ba, r_ba)

    # ---- 汇总表 ----
    out('=' * 100)
    out('汇总对比（执行口径: T收盘信号/T+1开盘，双边万1）')
    out('%-26s %9s %8s %9s %7s %9s %9s %8s %10s' %
        ('版本', '累计收益%', '年化%', '最大回撤%', '换仓', '2026YTD%', '2026-09%', '打脸段', '打脸合计%'))
    for m in summaries:
        out('%-26s %9.2f %8.2f %9.2f %7d %9.2f %9.2f %8d %10.2f' %
            (m['name'], m['cum'], m['ann'], m['mdd'], m['trades'],
             m['ytd26'], m['sep26'], m['n_slap'], m['slap_ret']))
    out('')
    out('切换日(2024-06-28)跨界持有进攻仓: ' +
        ', '.join('%s=%s' % (m['name'], '是(失真风险)' if m['cross'] else '否') for m in summaries))
    out('')

    # ---- 分年度收益 ----
    out('分年度收益表（%）:')
    years = sb['yearly'].index
    header = '%-26s' % '版本' + ''.join('%8d' % y.year for y in years)
    out(header)
    for m in summaries:
        out('%-26s' % m['name'] + ''.join('%8.2f' % (m['yearly'].loc[y] * 100) for y in years))
    out('')

    # ---- 信号提前/滞后与假信号分析 ----
    out('信号层面分析（相对基准 880003 的买入信号，±20 个自然日内最近匹配）:')
    out('%-26s %7s %7s %10s %10s %8s' % ('版本', '买信号', '卖信号', '提前/滞后', '中位天数', '打脸段'))
    for name, (sig, r) in variants.items():
        diffs = lead_lag(base_sig, sig)
        m = [x for x in summaries if x['name'] == name][0]
        if diffs:
            med = float(np.median(diffs))
            ahead = sum(1 for d in diffs if d < 0)
            lag = sum(1 for d in diffs if d > 0)
            desc = '提前%d/滞后%d/同步%d' % (ahead, lag, len(diffs) - ahead - lag)
        else:
            med, desc = float('nan'), '无匹配'
        out('%-26s %7d %7d %10s %10.1f %8d' %
            (name, int(sig['buy_sig'].sum()), int(sig['sell_sig'].sum()), desc, med, m['n_slap']))
    out('基准买信号 %d 次 / 卖信号 %d 次' % (int(base_sig['buy_sig'].sum()), int(base_sig['sell_sig'].sum())))
    out('')

    conclusion = """\
==================== 结论 ====================
核心问题：用进攻腿 ETF 自身价格做信号，是否比 880003 平均股价更好？
回答：不好，且差距明显，不建议采纳。

1) 收益全面落后：五组参数年化 19.16%~23.14%，全部低于基准的 28.33%；
   累计收益最高的 (17,34) 也只有 395%，比基准 580% 少 185 个百分点。
   最优参数恰是 (17,34)（与基准同参数），说明落后的原因在信号源而非参数。
2) 假信号更多、更伤：同参数 (17,34) 下，进攻腿信号的打脸段 33 段/-49.94%，
   基准为 25 段/-38.88%；参数越快假信号越爆炸（(8,21) 56 段/-87%）。
   拐头即买(buy_anywhere) 更差：换仓 309 次、打脸段 84 段/-120%，年化仅 20%。
3) 拐点 timing 并非主因：与基准买点匹配看，中位天数差为 0，快参数略偏提前
   （如 (8,21) 提前31/滞后13），(24,52) 明显滞后（滞后28/提前7）。
   即自身信号"提前"了一点，但提前换来的是抖动市里的反复挨打。
4) 为什么 880003 更好：平均股价指数是全市场几千只股票的等权平均，
   相当于天然降噪的宽基，DIF 拐点更平滑、更少毛刺；进攻腿 ETF
   （中证1000/2000增强）波动大、单日噪声多，DIF 频繁假拐头，
   在震荡/下跌年（典型如 2022：基准 +13.44%，自身信号 -5%~-16%）
   反复"零下抄底接刀"，把基准赚到的防守收益吐了回去。
   换言之：信号源的"先验平滑"比"与持仓同源"更重要——择时看大盘温度（880003），
   持仓弹性交给进攻腿，两者分工优于让高波动标的自己给自己发令。
5) 代价与风险：所有变体最大回撤均不差于基准太多（最好 (24,52) -16.07% 靠更迟钝），
   但换仓次数普遍上升（118~309 vs 135），费用与打脸损耗同步放大。
   所有变体在 2024-06-28 拼接切换日均未跨界持有进攻仓，无跨标的失真。
最终结论：维持 880003 作为信号源，不建议切换到进攻腿自身价格信号。"""
    out(conclusion)

    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           'result_signal_atk.txt'), 'w', encoding='utf-8') as fp:
        fp.write('\n'.join(lines) + '\n')

    # 供结论使用的关键数据回显
    print('\n[best]', best['name'], 'ann=%.2f' % best['ann'])
    print('[buy_anywhere]', name_ba, 'ann=%.2f mdd=%.2f trades=%d slap=%d' %
          (summaries[-1]['ann'], summaries[-1]['mdd'], summaries[-1]['trades'], summaries[-1]['n_slap']))


if __name__ == '__main__':
    main()
