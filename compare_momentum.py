#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
双动量相对强弱轮动 vs DIF 拐点基准
- 每日收盘比较拼接进攻腿 etf_atk 与 512890 的 N 日动量 (close/close.shift(N)-1)，持有动量高者
- 状态翻转日置 buy_sig/sell_sig，经 run_backtest(signals=) 注入，执行/费用口径与基准完全一致
- 变体：N ∈ {10,20,40,60}；最优 N + 绝对动量过滤（两者动量皆<0 强制防守）；最优 N + 缓冲带 b ∈ {1%,2%}
"""
import pandas as pd
import numpy as np
from backtest import run_backtest, max_drawdown, annualized

SWITCH = pd.Timestamp('2024-06-28')
FEE = 0.0001


def load(path):
    df = pd.read_csv(path, parse_dates=['date']).set_index('date').sort_index()
    df.index = df.index.normalize()
    return df


def yearly_returns(nav):
    y = nav.resample('YE').last()
    r = y.pct_change()
    r.iloc[0] = y.iloc[0] / nav.iloc[0] - 1
    r.index = r.index.year
    return r


def period_ret(nav, start, end):
    w = nav[(nav.index >= start) & (nav.index <= end)]
    if len(w) < 2:
        return float('nan')
    return float(w.iloc[-1] / w.iloc[0] - 1)


def face_slaps(res, max_days=5):
    """≤5个交易日且亏损的打脸段：相邻两次换仓之间，持仓段亏损且时长<=max_days"""
    trades = res['trades']
    nav = res['nav_strat']
    idx = res['idx']
    pos = {d: i for i, d in enumerate(idx)}
    cnt, total = 0, 0.0
    legs = []
    for i in range(len(trades) - 1):
        d0, d1 = trades[i][0], trades[i + 1][0]
        days = pos[d1] - pos[d0]
        ret = nav.loc[d1] / nav.loc[d0] - 1
        if days <= max_days and ret < 0:
            cnt += 1
            total += ret
            legs.append((d0.date(), d1.date(), days, ret))
    return cnt, total, legs


def cross_switch(res):
    """2024-06-28 拼接切换日是否持有进攻仓（跨界失真检查）"""
    h = res['hold1000']
    around = h[(h.index >= SWITCH - pd.Timedelta(days=5)) & (h.index <= SWITCH + pd.Timedelta(days=5))]
    return bool(around.loc[:around.index[around.index <= SWITCH][-1]].iloc[-1]) if len(around) else False


def make_momentum_signals(close_atk, close_div, n, abs_filter=False, buffer=0.0):
    mom_a = close_atk / close_atk.shift(n) - 1
    mom_d = close_div / close_div.shift(n) - 1
    common = close_atk.index.intersection(close_div.index)
    mom_a, mom_d = mom_a.loc[common], mom_d.loc[common]
    valid = mom_a.notna() & mom_d.notna()
    state = False  # False=防守腿, True=进攻腿
    states = pd.Series(False, index=common)
    for d in common:
        if valid.loc[d]:
            a, dd = mom_a.loc[d], mom_d.loc[d]
            if abs_filter and a < 0 and dd < 0:
                state = False
            elif buffer > 0:
                if (not state) and (a - dd) > buffer:
                    state = True
                elif state and (dd - a) > buffer:
                    state = False
            else:
                state = a > dd
        states.loc[d] = state
    sig = pd.DataFrame(index=common)
    prev = states.shift(1).fillna(False)
    sig['buy_sig'] = states & ~prev
    sig['sell_sig'] = ~states & prev
    return sig


def run_variant(name, sig, avg, etf_atk, etfdiv):
    res = run_backtest(avg, etf_atk, etfdiv, fee=FEE, signals=sig, verbose=False)
    nav = res['nav_strat']
    cnt, tot, legs = face_slaps(res)
    return {
        'name': name, 'res': res, 'nav': nav,
        'cum': float(nav.iloc[-1] - 1), 'ann': annualized(nav), 'mdd': max_drawdown(nav),
        'ntrades': len(res['trades']),
        'ytd26': period_ret(nav, '2026-01-01', '2026-12-31'),
        'sep26': period_ret(nav, '2026-09-01', '2026-09-30'),
        'yearly': yearly_returns(nav),
        'slap_cnt': cnt, 'slap_ret': tot, 'slap_legs': legs,
        'cross': cross_switch(res),
        'hold_atk_pct': float(res['hold1000'].mean()),
    }


def fmt_row(s):
    return ('%-26s %9.2f%% %8.2f%% %8.2f%% %6d %9.2f%% %8.2f%% %8.2f%%'
            % (s['name'], s['cum'] * 100, s['ann'] * 100, s['mdd'] * 100,
               s['ntrades'], s['ytd26'] * 100, s['sep26'] * 100, s['slap_ret'] * 100))


def main():
    avg = load('data/avg_880003.csv')
    etf1000 = load('data/etf_512100_hfq.csv')
    etf2000e = load('data/etf_159552_hfq.csv')
    etfdiv = load('data/etf_512890_hfq.csv')
    etf_atk = pd.concat([etf1000[etf1000.index < SWITCH], etf2000e[etf2000e.index >= SWITCH]])

    out = []

    # ---- 基准 ----
    base_res = run_backtest(avg, etf_atk, etfdiv, fee=FEE, sell_anywhere=True,
                            fast=17, slow=34, sig_n=9, verbose=False)
    nav = base_res['nav_strat']
    cnt, tot, legs = face_slaps(base_res)
    baseline = {
        'name': '基准 DIF拐点(17/34/9)', 'res': base_res, 'nav': nav,
        'cum': float(nav.iloc[-1] - 1), 'ann': annualized(nav), 'mdd': max_drawdown(nav),
        'ntrades': len(base_res['trades']),
        'ytd26': period_ret(nav, '2026-01-01', '2026-12-31'),
        'sep26': period_ret(nav, '2026-09-01', '2026-09-30'),
        'yearly': yearly_returns(nav),
        'slap_cnt': cnt, 'slap_ret': tot, 'slap_legs': legs,
        'cross': cross_switch(base_res),
        'hold_atk_pct': float(base_res['hold1000'].mean()),
    }
    print('基准: 累计 %.2f%% 年化 %.2f%% MDD %.2f%% 换仓 %d 次'
          % (baseline['cum'] * 100, baseline['ann'] * 100, baseline['mdd'] * 100, baseline['ntrades']))

    results = [baseline]

    # ---- N 基础版 ----
    base_variants = {}
    for n in (10, 20, 40, 60):
        sig = make_momentum_signals(etf_atk['close'], etfdiv['close'], n)
        v = run_variant('动量N=%d' % n, sig, avg, etf_atk, etfdiv)
        base_variants[n] = v
        results.append(v)
        print('N=%d: 累计 %.2f%% 换仓 %d 次' % (n, v['cum'] * 100, v['ntrades']))

    # 最优 N 按年化收益选
    best_n = max(base_variants, key=lambda k: base_variants[k]['ann'])
    print('最优 N（按年化）= %d' % best_n)

    # ---- 绝对动量过滤 ----
    sig_abs = make_momentum_signals(etf_atk['close'], etfdiv['close'], best_n, abs_filter=True)
    v_abs = run_variant('N=%d+绝对动量过滤' % best_n, sig_abs, avg, etf_atk, etfdiv)
    results.append(v_abs)

    # ---- 缓冲带 ----
    for b in (0.01, 0.02):
        sig_b = make_momentum_signals(etf_atk['close'], etfdiv['close'], best_n, buffer=b)
        v_b = run_variant('N=%d+缓冲带%.0f%%' % (best_n, b * 100), sig_b, avg, etf_atk, etfdiv)
        results.append(v_b)

    # ---- 汇总输出 ----
    hdr = ('%-26s %10s %9s %9s %6s %10s %9s %9s'
           % ('策略', '累计收益', '年化收益', '最大回撤', '换仓', '2026YTD', '2026-09', '打脸段合计'))
    lines = []
    lines.append('=' * 110)
    lines.append('双动量相对强弱轮动 vs DIF拐点基准  |  区间 %s ~ %s  |  fee=%.4f 双边  |  T收盘信号 T+1开盘成交'
                 % (base_res['idx'][0].date(), base_res['idx'][-1].date(), FEE))
    lines.append('=' * 110)
    lines.append(hdr)
    for s in results:
        lines.append(fmt_row(s) + '  (打脸段%d次)' % s['slap_cnt'])
    lines.append('-' * 110)
    lines.append('持仓进攻腿时间占比 / 跨界持有进攻仓(2024-06-28):')
    for s in results:
        lines.append('  %-26s 进攻仓 %.1f%%   跨界持有: %s'
                     % (s['name'], s['hold_atk_pct'] * 100, '是(收益有跨标的失真风险)' if s['cross'] else '否'))

    # 分年度表
    lines.append('-' * 110)
    lines.append('分年度收益(%):')
    years = sorted(set().union(*[set(s['yearly'].index) for s in results]))
    header = '年份  ' + ''.join('%-26s' % s['name'] for s in results)
    lines.append(header)
    for y in years:
        row = '%d  ' % y
        for s in results:
            val = s['yearly'].get(y, float('nan'))
            row += '%25.2f%%' % (val * 100) if pd.notna(val) else '%26s' % '-'
        lines.append(row)

    # 费用侵蚀估算：换仓次数 * 2*fee
    lines.append('-' * 110)
    lines.append('费用侵蚀估算(换仓次数 × 双边0.02%，复利近似):')
    for s in results:
        drag = (1 - 2 * FEE) ** s['ntrades'] - 1
        lines.append('  %-26s 换仓 %4d 次，累计费用拖累约 %.2f%%' % (s['name'], s['ntrades'], -drag * 100))

    # 核心结论
    lines.append('=' * 110)
    lines.append('核心结论:')
    bm = base_variants
    win_years = {n: [y for y in years if y in bm[n]['yearly'] and y in baseline['yearly']
                     and bm[n]['yearly'][y] > baseline['yearly'][y]] for n in bm}
    for n in bm:
        v = bm[n]
        wy = win_years[n]
        ly = [y for y in years if y in v['yearly'] and y in baseline['yearly'] and y not in wy]
        lines.append('  N=%d vs 基准: 累计 %+.2fpp, 年化 %+.2fpp, 换仓 %d vs %d 次; 赢的年份 %s, 输的年份 %s'
                     % (n, (v['cum'] - baseline['cum']) * 100, (v['ann'] - baseline['ann']) * 100,
                        v['ntrades'], baseline['ntrades'], wy, ly))
    best = bm[best_n]
    verdict = '优于基准' if best['ann'] > baseline['ann'] else '未跑赢基准'
    lines.append('  纯动量轮动(最优N=%d)整体%s（年化 %.2f%% vs %.2f%%），换仓 %d 次 vs 基准 %d 次。'
                 % (best_n, verdict, best['ann'] * 100, baseline['ann'] * 100,
                    best['ntrades'], baseline['ntrades']))
    lines.append('  绝对动量过滤: 年化 %+.2fpp vs 未过滤; 缓冲带1%%: %+.2fpp, 2%%: %+.2fpp (vs 基础版 N=%d)'
                 % ((v_abs['ann'] - best['ann']) * 100,
                    (results[-2]['ann'] - best['ann']) * 100,
                    (results[-1]['ann'] - best['ann']) * 100, best_n))

    text = '\n'.join(lines)
    print(text)
    with open('result_momentum.txt', 'w', encoding='utf-8') as f:
        f.write(text + '\n')
    print('\n已写入 result_momentum.txt')


if __name__ == '__main__':
    main()
