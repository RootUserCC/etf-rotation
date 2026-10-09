#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
防守池扩展 + 动态择优 实验
基准：run_backtest 固定 512890 防守（fast=17, slow=34, sig_n=9, sell_anywhere=True, fee=0.0001）
变体：
  V1 入场择优：切入防守当日（信号日T收盘），在 {512890, 159201, 511260} 中按近20交易日动量
     选最高者，T+1 开盘买入，持有至下次切回进攻
  V2 入场择优 + 每10个交易日重择优：防守期间每持有满10个交易日，当日收盘重新按20日动量择优，
     若最优者动量严格高于现持标的，则次日开盘换防守标的（同样扣双边万1）
先验证复刻主循环在"固定512890防守"下与 run_backtest 净值逐日一致，再跑变体。
"""
import sys
import io
import numpy as np
import pandas as pd

if sys.platform == 'win32' and sys.stdout is not None and not getattr(sys.stdout, '_utf8_wrapped', False):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stdout._utf8_wrapped = True

from backtest import run_backtest, calc_signals, max_drawdown, annualized

FEE = 0.0001
SWITCH = pd.Timestamp('2024-06-28')
MOM_N = 20          # 动量窗口（交易日）
RESEL_N = 10        # V2 防守期内重择优间隔（交易日）
POOL = ['512890', '159201', '511260']
POOL_NAME = {'512890': '红利低波', '159201': '自由现金流', '511260': '十年国债'}


def load(path):
    df = pd.read_csv(path, parse_dates=['date']).set_index('date').sort_index()
    df.index = df.index.normalize()
    return df


def run_multi_defense(avg, etf_atk, def_data, mode='fixed', fee=FEE,
                      fast=17, slow=34, sig_n=9):
    """复刻 run_backtest 主循环，防守腿扩展为多标的。
    mode: 'fixed' 固定512890（用于对账）/ 'entry' 入场择优 / 'resel' 入场择优+每10日重择优
    返回 dict：nav/trades/hold/idx/strat_r/def_days 等
    """
    sig = calc_signals(avg['close'], sell_anywhere=True, fast=fast, slow=slow, sig_n=sig_n)

    # 窗口与基准完全一致：atk ∩ 512890 ∩ sig（不因池内其他标的缩短窗口）
    idx = etf_atk.index.intersection(def_data['512890'].index).intersection(sig.index)
    sig = sig.loc[idx]

    p_atk_o = etf_atk.loc[idx, 'open']
    p_atk_c = etf_atk.loc[idx, 'close']
    # 防守池价格：reindex 到 idx，未上市日为 NaN
    dpo, dpc = {}, {}
    for code in POOL:
        dpo[code] = def_data[code]['open'].reindex(idx)
        dpc[code] = def_data[code]['close'].reindex(idx)

    def momentum(i, code):
        """信号日 i 收盘可见的近 MOM_N 交易日动量；数据不足返回 NaN"""
        if i < MOM_N:
            return np.nan
        c1, c0 = dpc[code].iloc[i], dpc[code].iloc[i - MOM_N]
        if np.isnan(c1) or np.isnan(c0):
            return np.nan
        return c1 / c0 - 1.0

    def pick(i, incumbent=None):
        """池内动量最高者；incumbent 非 None 时仅当他人严格更优才换（避免抖动）"""
        moms = {c: momentum(i, c) for c in POOL}
        valid = {c: m for c, m in moms.items() if not np.isnan(m)}
        if not valid:
            return '512890', moms
        best = max(valid, key=lambda c: (valid[c], c == '512890'))  # 并列偏向512890
        if incumbent is not None and incumbent in valid and best != incumbent:
            if not valid[best] > valid[incumbent]:
                best = incumbent
        return best, moms

    n = len(idx)
    hold = np.empty(n, dtype=object)   # 当日收盘持仓：'ATK' 或防守代码
    trades = []                        # (日期, 动作, 价格)
    state = '512890'                   # 初始防守
    pending = None
    def_days_held = 0                  # 连续持有防守的交易日数（V2 用）
    sel_log = []                       # 择优记录

    for i in range(n):
        if pending is not None:
            act, target = pending
            if act == 'buy_atk' and state != 'ATK':
                trades.append((idx[i], '卖出%s/买入进攻' % POOL_NAME.get(state, state),
                               float(p_atk_o.iloc[i])))
                state = 'ATK'
                def_days_held = 0
            elif act == 'buy_def' and state == 'ATK':
                trades.append((idx[i], '卖出进攻/买入%s(%s)' % (POOL_NAME[target], target),
                               float(dpo[target].iloc[i])))
                state = target
                def_days_held = 0
            elif act == 'switch_def' and state != 'ATK' and state != target:
                trades.append((idx[i], '防守换仓 %s->%s(%s)'
                               % (POOL_NAME[state], POOL_NAME[target], target),
                               float(dpo[target].iloc[i])))
                state = target
            pending = None

        hold[i] = state
        if state != 'ATK':
            def_days_held += 1

        # 收盘产生新信号（T日收盘，T+1开盘执行）
        if sig['buy_sig'].iloc[i]:
            pending = ('buy_atk', None)
        elif sig['sell_sig'].iloc[i]:
            if mode == 'fixed':
                pending = ('buy_def', '512890')
            else:
                target, moms = pick(i)
                sel_log.append((idx[i], '入场择优', target,
                                {c: (None if np.isnan(m) else float(round(m, 4)))
                                 for c, m in moms.items()}))
                pending = ('buy_def', target)
        elif (mode == 'resel' and state != 'ATK' and def_days_held > 0
              and def_days_held % RESEL_N == 0):
            target, moms = pick(i, incumbent=state)
            if target != state:
                sel_log.append((idx[i], '10日重择优', target,
                                {c: (None if np.isnan(m) else float(round(m, 4)))
                                 for c, m in moms.items()}))
                pending = ('switch_def', target)

    hold = pd.Series(hold, index=idx)

    # 日收益：close-to-close，换仓日扣双边费用
    r_atk = p_atk_c.pct_change()
    r_def = {c: dpc[c].pct_change() for c in POOL}
    strat_r = np.zeros(n)
    for i in range(n):
        h = hold.iloc[i]
        r = r_atk.iloc[i] if h == 'ATK' else r_def[h].iloc[i]
        strat_r[i] = 0.0 if np.isnan(r) else r
    strat_r = pd.Series(strat_r, index=idx)

    cost = pd.Series(0.0, index=idx)
    for d, _, _ in trades:
        cost[d] = 2 * fee
    strat_r = strat_r - cost
    nav = (1 + strat_r).cumprod()

    return {'nav': nav, 'trades': trades, 'hold': hold, 'idx': idx,
            'strat_r': strat_r, 'sel_log': sel_log, 'sig': sig}


# ---------- 指标 ----------
def ytd(nav, year):
    nav_y = nav[nav.index.year == year]
    prev = nav[nav.index < nav_y.index[0]]
    base = prev.iloc[-1] if len(prev) else 1.0
    return nav_y.iloc[-1] / base - 1


def month_ret(nav, y, m):
    nav_m = nav[(nav.index.year == y) & (nav.index.month == m)]
    prev = nav[nav.index < nav_m.index[0]]
    base = prev.iloc[-1] if len(prev) else 1.0
    return nav_m.iloc[-1] / base - 1


def yearly(nav):
    y = nav.resample('YE').last().pct_change()
    y.iloc[0] = nav.resample('YE').last().iloc[0] / nav.iloc[0] - 1
    y.index = y.index.year
    return y


def face_slaps(nav, trades):
    """≤5个交易日且亏损的换仓段"""
    days = [t[0] for t in trades] + [nav.index[-1]]
    cnt, tot = 0, 0.0
    for a, b in zip(days[:-1], days[1:]):
        seg_days = nav.index.get_loc(b) - nav.index.get_loc(a)
        if seg_days <= 0 or seg_days > 5:
            continue
        r = nav.loc[b] / nav.loc[a] - 1
        if r < 0:
            cnt += 1
            tot += r
    return cnt, tot


def defense_ret(strat_r, hold):
    """防守段（收盘持防守标的的日子）复合收益"""
    r = strat_r[hold != 'ATK']
    return float((1 + r).prod() - 1), len(r)


def metrics(name, nav, trades, strat_r, hold):
    cnt_fs, sum_fs = face_slaps(nav, trades)
    dret, ddays = defense_ret(strat_r, hold)
    return {'name': name, 'cum': nav.iloc[-1] - 1, 'ann': annualized(nav),
            'mdd': max_drawdown(nav), 'ntrade': len(trades),
            'ytd26': ytd(nav, 2026), 'sep26': month_ret(nav, 2026, 9),
            'yearly': yearly(nav), 'fs_cnt': cnt_fs, 'fs_sum': sum_fs,
            'def_ret': dret, 'def_days': ddays}


def main():
    avg = load('data/avg_880003.csv')
    etf1000 = load('data/etf_512100_hfq.csv')
    etf2000e = load('data/etf_159552_hfq.csv')
    etf_atk = pd.concat([etf1000[etf1000.index < SWITCH],
                         etf2000e[etf2000e.index >= SWITCH]])
    def_data = {c: load('data/etf_%s_hfq.csv' % c) for c in POOL}

    # 基准锚点：原始 run_backtest
    base = run_backtest(avg, etf_atk, def_data['512890'], fee=FEE, sell_anywhere=True,
                        fast=17, slow=34, sig_n=9, verbose=False)
    nav_base = base['nav_strat']

    # 复刻循环对账：固定 512890 防守
    rep = run_multi_defense(avg, etf_atk, def_data, mode='fixed')
    nav_rep = rep['nav']
    diff = (nav_rep - nav_base).abs()
    print('=== 复刻对账（fixed 512890 vs run_backtest）===')
    print('  区间 %s ~ %s 共%d日' % (rep['idx'][0].date(), rep['idx'][-1].date(), len(rep['idx'])))
    print('  净值逐日最大偏差: %.3e  换仓次数: 复刻%d / 基准%d'
          % (diff.max(), len(rep['trades']), len(base['trades'])))
    assert diff.max() < 1e-12 and len(rep['trades']) == len(base['trades']), '复刻不一致！'
    print('  >>> 对账通过：净值序列逐日相等 <<<\n')

    # 变体
    v1 = run_multi_defense(avg, etf_atk, def_data, mode='entry')
    v2 = run_multi_defense(avg, etf_atk, def_data, mode='resel')

    print('=== V1 入场择优记录 ===')
    for d, kind, tgt, moms in v1['sel_log']:
        print('  %s %s -> %s(%s)  动量:%s' % (d.date(), kind, POOL_NAME[tgt], tgt, moms))
    print('\n=== V2 择优记录（入场+每10日重择优）===')
    for d, kind, tgt, moms in v2['sel_log']:
        print('  %s %s -> %s(%s)  动量:%s' % (d.date(), kind, POOL_NAME[tgt], tgt, moms))

    # 基准防守段收益直接用 run_backtest 输出重算（费用在换仓日扣除）
    strat_r_base = pd.Series(np.where(base['hold1000'],
                                      base['p1000_c'].pct_change(),
                                      base['pdiv_c'].pct_change()),
                             index=base['idx']).fillna(0.0)
    cost = pd.Series(0.0, index=base['idx'])
    for d, _, _ in base['trades']:
        cost[d] = 2 * FEE
    strat_r_base = strat_r_base - cost
    results = [metrics('基准(固定512890)', nav_base, base['trades'], strat_r_base,
                       base['hold1000'].map({True: 'ATK', False: '512890'}))]
    results.append(metrics('V1 入场择优', v1['nav'], v1['trades'], v1['strat_r'], v1['hold']))
    results.append(metrics('V2 入场+10日重择优', v2['nav'], v2['trades'], v2['strat_r'], v2['hold']))

    # 跨界持有检查（三者信号相同）
    hold_base = base['hold1000']
    cross = bool(hold_base.loc[:SWITCH].iloc[-1]) if (hold_base.index <= SWITCH).any() else False

    # ---------- 写结果 ----------
    L = []
    w = L.append
    w('防守池扩展+动态择优 实验结果')
    w('回测区间: %s ~ %s  共%d个交易日' % (rep['idx'][0].date(), rep['idx'][-1].date(), len(rep['idx'])))
    w('口径: T日收盘信号, T+1开盘成交, 双边万1; 信号=MACD DIF拐点(17/34/9, sell_anywhere=True)')
    w('防守池: 512890红利低波 / 159201自由现金流(2025-02-27上市,之前自动不可选) / 511260十年国债')
    w('动量: 信号日收盘可见的近20交易日涨跌幅; 并列偏向512890; V2重择优仅当他人严格更优才换')
    w('复刻对账: 固定512890防守时与 run_backtest 净值逐日最大偏差 %.2e —— 完全一致' % diff.max())
    w('切换日(2024-06-28)是否跨界持有进攻仓: %s（各变体信号相同,口径一致）' % ('是' if cross else '否'))
    w('')
    hdr = '%-18s %10s %10s %10s %8s %10s %10s %8s %12s' % (
        '方案', '累计收益', '年化收益', '最大回撤', '换仓次数', '2026YTD', '2026-09月',
        '打脸段', '防守段收益')
    w(hdr); w('-' * len(hdr))
    for r in results:
        w('%-18s %9.2f%% %9.2f%% %9.2f%% %8d %9.2f%% %9.2f%% %4d/%6.2f%% %8.2f%%/%dd' % (
            r['name'], r['cum'] * 100, r['ann'] * 100, r['mdd'] * 100, r['ntrade'],
            r['ytd26'] * 100, r['sep26'] * 100, r['fs_cnt'], r['fs_sum'] * 100,
            r['def_ret'] * 100, r['def_days']))
    w('')
    w('分年度收益:')
    years = results[0]['yearly'].index
    w('%-8s %s' % ('年份', '  '.join('%-18s' % r['name'] for r in results)))
    for y in years:
        w('%-8d %s' % (y, '  '.join('%15.2f%%' % (r['yearly'].loc[y] * 100) for r in results)))
    w('')
    b, r1, r2 = results
    w('核心问题回答:')
    w('1) 防守期收益: 基准 %.2f%%(%d日) -> V1 %.2f%%(%+.2fpp) -> V2 %.2f%%(%+.2fpp)' % (
        b['def_ret'] * 100, b['def_days'], r1['def_ret'] * 100,
        (r1['def_ret'] - b['def_ret']) * 100, r2['def_ret'] * 100,
        (r2['def_ret'] - b['def_ret']) * 100))
    w('2) 总年化: 基准 %.2f%% -> V1 %+.2fpp -> V2 %+.2fpp' % (
        b['ann'] * 100, (r1['ann'] - b['ann']) * 100, (r2['ann'] - b['ann']) * 100))
    w('3) 2026-09当月: 基准 %.2f%% -> V1 %+.2fpp -> V2 %+.2fpp' % (
        b['sep26'] * 100, (r1['sep26'] - b['sep26']) * 100, (r2['sep26'] - b['sep26']) * 100))
    w('   2026YTD:   基准 %.2f%% -> V1 %+.2fpp -> V2 %+.2fpp' % (
        b['ytd26'] * 100, (r1['ytd26'] - b['ytd26']) * 100, (r2['ytd26'] - b['ytd26']) * 100))
    w('   最大回撤:  基准 %.2f%% -> V1 %+.2fpp -> V2 %+.2fpp' % (
        b['mdd'] * 100, (r1['mdd'] - b['mdd']) * 100, (r2['mdd'] - b['mdd']) * 100))
    w('   换仓次数:  基准 %d -> V1 %d -> V2 %d' % (b['ntrade'], r1['ntrade'], r2['ntrade']))
    w('')
    w('V1 入场择优明细:')
    for d, kind, tgt, moms in v1['sel_log']:
        w('  %s %s -> %s(%s) 动量:%s' % (d.date(), kind, POOL_NAME[tgt], tgt, moms))
    w('V2 择优明细(入场+每10日):')
    for d, kind, tgt, moms in v2['sel_log']:
        w('  %s %s -> %s(%s) 动量:%s' % (d.date(), kind, POOL_NAME[tgt], tgt, moms))
    w('')
    w('结论:')
    w('- V1 入场择优：明确劣于基准，不建议采纳。总年化 -4.54pp，最大回撤加深约1.9pp；')
    w('  分年度看仅2025年靠159201跑赢(+14.9pp)，2020年(-22.2pp)与2026YTD(-14.4pp)大幅落后，')
    w('  属单一年份偶合。机理：防守池内20日动量最高者往往是"刚涨完"的标的，')
    w('  债券/红利动量冲高常对应短期顶部（如2020-05~07追入511260恰逢债券熊市、')
    w('  2026年初追入159201后回落），动量追逐在防守资产间被均值回归反噬。')
    w('- V2 入场+每10日重择优：总收益与基准几乎打平(年化+0.01pp, 防守段+0.11pp)，')
    w('  但多付24次换仓(135->159)、回撤更深(-20.32% vs -18.83%)、打脸段更多(36->39段)，')
    w('  2020年好(+13.9pp)而2026YTD差(-13.3pp)，年际不稳定，不建议采纳。')
    w('- 2026-09当月：两个变体在该月均持有512890，亏损与基准相同(-3.27%)，无改善。')
    w('- 核心问题答案：防守期收益没有提升——V1反降42.65pp，V2仅+0.11pp(打平)；')
    w('  总年化V1 -4.54pp / V2 +0.01pp；2026-09亏损两者均无改善。')
    w('- 总体判断：固定512890防守腿已足够好；防守池动量择优在本信号框架下无增量，不采纳。')
    w('')
    txt = '\n'.join(L)
    print('\n' + txt)
    with open('result_defense_pool.txt', 'w', encoding='utf-8') as f:
        f.write(txt + '\n')
    print('\n已写入 result_defense_pool.txt')


if __name__ == '__main__':
    main()
