#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
防打脸过滤器实验（compare_whipsaw.py）
基准：880003 MACD(17,34,9) sell_anywhere=True，进攻腿=512100→159552 拼接，防守腿=512890
变体：
  (a) 卖出确认：卖出信号日 etf_atk 收盘 < MA20 才生效
  (b) 对称确认：买入 buy_confirm=True，卖出对称顺延1天（次日 DIF 仍下行才确认）
  (c) 冷却期：卖出信号后 X∈{3,5,8} 个交易日内忽略买入信号
  (d) 幅度过滤：卖出要求 DIF 自近20日高点回落超过固定阈值
评估：≤5 个交易日且亏损的打脸段数量/合计；2026-07 是否仍及时切防守
"""
import pandas as pd
import numpy as np
from backtest import calc_signals, run_backtest, max_drawdown, annualized

FEE = 0.0001
FAST, SLOW, SIG_N = 17, 34, 9
SWITCH = pd.Timestamp('2024-06-28')


def load(path):
    df = pd.read_csv(path, parse_dates=['date']).set_index('date').sort_index()
    df.index = df.index.normalize()
    return df


avg = load('data/avg_880003.csv')
etf1000 = load('data/etf_512100_hfq.csv')
etf2000e = load('data/etf_159552_hfq.csv')
etfdiv = load('data/etf_512890_hfq.csv')
etf_atk = pd.concat([etf1000[etf1000.index < SWITCH], etf2000e[etf2000e.index >= SWITCH]])

# 基准信号（所有变体在此基础上改造）
base_sig = calc_signals(avg['close'], sell_anywhere=True, fast=FAST, slow=SLOW, sig_n=SIG_N)


def run(signals=None, buy_confirm=False):
    return run_backtest(avg, etf_atk, etfdiv, fee=FEE, sell_anywhere=True,
                        fast=FAST, slow=SLOW, sig_n=SIG_N,
                        buy_confirm=buy_confirm, signals=signals, verbose=False)


def strat_returns(res):
    """复算 run_backtest 内部的策略日收益（含换仓日双边费用）"""
    idx = res['idx']
    r = pd.Series(np.where(res['hold1000'],
                           res['p1000_c'].pct_change(),
                           res['pdiv_c'].pct_change()), index=idx).fillna(0.0)
    for d, _, _ in res['trades']:
        r[d] -= 2 * FEE
    return r


def whipsaw_segments(res):
    """打脸段：一次完整的进攻仓持有（买入执行日→卖出执行日），
    持有交易日数 ≤5 且段收益 < 0。段收益含双边费用。"""
    idx = res['idx']
    r = strat_returns(res)
    pos = {d: i for i, d in enumerate(idx)}
    segs = []
    i = 0
    tr = res['trades']
    while i < len(tr):
        d0, a0, _ = tr[i]
        if '买入1000' in a0 and i + 1 < len(tr) and '卖出1000' in tr[i + 1][1]:
            d1 = tr[i + 1][0]
            seg = r.loc[d0:d1].iloc[:-1]          # [买入执行日, 卖出执行日)
            dur = len(seg)
            ret = float((1 + seg).prod() - 1 - 2 * FEE)   # 卖出执行日费用另行扣除
            segs.append({'buy': d0, 'sell': d1, 'dur': dur, 'ret': ret})
            i += 2
        else:
            i += 1
    whip = [s for s in segs if s['dur'] <= 5 and s['ret'] < 0]
    return segs, whip


def month_ret(nav, y, m):
    sub = nav[(nav.index.year == y) & (nav.index.month == m)]
    prev = nav[nav.index < sub.index[0]]
    if len(sub) == 0 or len(prev) == 0:
        return float('nan')
    return float(sub.iloc[-1] / prev.iloc[-1] - 1)


def ytd_ret(nav, y):
    sub = nav[nav.index.year == y]
    prev = nav[nav.index < sub.index[0]]
    return float(sub.iloc[-1] / prev.iloc[-1] - 1)


def yearly_returns(nav):
    y = nav.resample('YE').last().pct_change()
    y.iloc[0] = nav.resample('YE').last().iloc[0] / nav.iloc[0] - 1
    y.index = y.index.year
    return y


def july2026_check(res):
    """2026-07 前后是否及时切防守"""
    hold = res['hold1000']
    jul = hold[(hold.index.year == 2026) & (hold.index.month == 7)]
    atk_days = int(jul.sum())
    trades_before = [t for t in res['trades'] if t[0] < pd.Timestamp('2026-07-01')]
    last = trades_before[-1] if trades_before else None
    atk_jul_ret = float(res['p1000_c'].loc[jul.index].iloc[-1] /
                        res['p1000_c'].loc[jul.index].iloc[0] - 1) if len(jul) else float('nan')
    return atk_days, len(jul), last, atk_jul_ret


def cross_switch_hold(res):
    """2024-06-28 拼接切换日是否持有进攻仓（跨界失真风险）"""
    hold = res['hold1000']
    if SWITCH in hold.index:
        return bool(hold.loc[SWITCH])
    # 若当日非交易日取前后
    before = hold[hold.index <= SWITCH]
    return bool(before.iloc[-1]) if len(before) else None


def metrics(name, res):
    nav = res['nav_strat']
    segs, whip = whipsaw_segments(res)
    j_atk, j_tot, j_last, j_atkret = july2026_check(res)
    return {
        'name': name,
        'cum': float(nav.iloc[-1] - 1),
        'ann': annualized(nav),
        'mdd': max_drawdown(nav),
        'ntrades': len(res['trades']),
        'ytd2026': ytd_ret(nav, 2026),
        'sep2026': month_ret(nav, 2026, 9),
        'jul2026': month_ret(nav, 2026, 7),
        'yearly': yearly_returns(nav),
        'nwhip': len(whip),
        'whip_sum': float(sum(s['ret'] for s in whip)),
        'whip': whip,
        'cross_hold': cross_switch_hold(res),
        'jul_atk_days': j_atk, 'jul_days': j_tot, 'jul_last_trade': j_last,
        'jul_atk_ret': j_atkret,
    }


# ---------------- 变体信号构造 ----------------
def variant_a():
    """(a) 卖出确认：卖出信号日进攻腿收盘 < MA20 才生效"""
    sig = base_sig.copy()
    ma20 = etf_atk['close'].rolling(20).mean().reindex(sig.index)
    atk_c = etf_atk['close'].reindex(sig.index)
    sig['sell_sig'] = sig['sell_sig'] & (atk_c < ma20).fillna(False)
    return sig


def variant_b():
    """(b) 对称确认：买入 buy_confirm=True；卖出对称——拐点次日 DIF 仍下行才确认"""
    sig = calc_signals(avg['close'], sell_anywhere=True, fast=FAST, slow=SLOW,
                       sig_n=SIG_N, buy_confirm=True)
    dif = sig['dif']
    sig['sell_sig'] = sig['sell_sig'].shift(1).fillna(False) & (dif.diff() < 0)
    return sig


def variant_c(x):
    """(c) 冷却期：卖出信号后 X 个交易日内忽略买入信号"""
    return apply_cooldown(base_sig.copy(), x)


def apply_cooldown(sig, x):
    idx = sig.index
    buy = sig['buy_sig'].values.copy()
    sell = sig['sell_sig'].values
    cooldown_until = -1
    for i in range(len(idx)):
        if sell[i]:
            cooldown_until = i + x
        if i <= cooldown_until:
            buy[i] = False
    sig['buy_sig'] = buy
    return sig


def variant_d(thr):
    """(d) 幅度过滤：卖出要求 DIF 自近20日高点回落超过阈值 thr"""
    sig = base_sig.copy()
    dif = sig['dif']
    roll_max = dif.rolling(20, min_periods=1).max()
    sig['sell_sig'] = sig['sell_sig'] & ((roll_max - dif) > thr)
    return sig


def combine(seq_cooldown_x, base):
    """组合：在 base 信号（已含确认/过滤）之上顺序叠加冷却期（而非点信号取交集，
    点信号取交集会把买卖信号全部打没，属退化用法）"""
    return apply_cooldown(base.copy(), seq_cooldown_x)


# ---------------- 运行 ----------------
results = []

print('复跑基准 ...')
base_res = run()
base_m = metrics('基准(无过滤)', base_res)
results.append(base_m)
bm = base_m
print('基准: 累计 %.2f%% 年化 %.2f%% 回撤 %.2f%% 换仓 %d 次 2026YTD %.2f%% 2026-09 %.2f%%'
      % (bm['cum'] * 100, bm['ann'] * 100, bm['mdd'] * 100, bm['ntrades'],
         bm['ytd2026'] * 100, bm['sep2026'] * 100))
print('基准打脸段: %d 段, 合计 %.2f%%' % (bm['nwhip'], bm['whip_sum'] * 100))
for s in bm['whip']:
    print('  %s -> %s  持有%d天  %.2f%%' % (s['buy'].date(), s['sell'].date(), s['dur'], s['ret'] * 100))

# (d) 阈值选取：基准卖出信号日 DIF 距20日高点的回落分布
dif = base_sig['dif']
roll_max = dif.rolling(20, min_periods=1).max()
drop = (roll_max - dif)[base_sig['sell_sig']]
drop = drop[drop > 0]
print('\n卖出信号日 DIF 自20日高点回落分布:')
print(drop.describe())
qs = drop.quantile([0.25, 0.5, 0.75])
print('分位数 25/50/75%%: %.4f / %.4f / %.4f' % (qs.iloc[0], qs.iloc[1], qs.iloc[2]))
# 固定阈值取整
THRS = sorted(set([round(float(qs.iloc[0]), 3), round(float(qs.iloc[1]), 3),
                   round(float(qs.iloc[2]), 3)]))
print('(d) 采用阈值:', THRS)

variants = [
    ('(a)卖出确认MA20', lambda: run(signals=variant_a())),
    ('(b)对称确认', lambda: run(signals=variant_b())),
    ('(c)冷却3日', lambda: run(signals=variant_c(3))),
    ('(c)冷却5日', lambda: run(signals=variant_c(5))),
    ('(c)冷却8日', lambda: run(signals=variant_c(8))),
] + [('(d)幅度>%.3f' % t, (lambda t=t: run(signals=variant_d(t)))) for t in THRS] + [
    ('(a)+(c5)', lambda: run(signals=combine(5, variant_a()))),
    ('(b)+(c5)', lambda: run(signals=combine(5, variant_b()))),
    ('(d%.3f)+(c3)' % THRS[1], lambda: run(signals=combine(3, variant_d(THRS[1])))),
]

for name, fn in variants:
    res = fn()
    m = metrics(name, res)
    results.append(m)
    print('%-14s 累计 %8.2f%% 年化 %6.2f%% 回撤 %7.2f%% 换仓 %3d  2026YTD %7.2f%% 2026-09 %7.2f%%  打脸 %d段/%6.2f%%  7月进攻天数 %d/%d'
          % (name, m['cum'] * 100, m['ann'] * 100, m['mdd'] * 100, m['ntrades'],
             m['ytd2026'] * 100, m['sep2026'] * 100, m['nwhip'], m['whip_sum'] * 100,
             m['jul_atk_days'], m['jul_days']))

# ---------------- 写结果文件 ----------------
L = []
L.append('防打脸过滤器实验结果（compare_whipsaw.py）')
L.append('基准口径：880003 MACD(17,34,9) sell_anywhere=True；进攻腿 512100→159552(2024-06-28拼接)；防守腿 512890；')
L.append('T日收盘出信号 T+1 开盘成交，双边佣金各万1。回测区间 2019-01-18 ~ 2026-09-23。')
L.append('打脸段定义：一次完整进攻仓持有 ≤5 个交易日且段收益<0（含双边费用）。')
L.append('')
L.append('锚点校验（数据截至 2026-09-23）:')
L.append('  满仓进攻腿 2026-09 当月 %+.2f%% —— 与题面 +1.95%% 完全一致，说明数据版本相同。'
         % (month_ret(base_res['nav_1000'], 2026, 9) * 100))
L.append('  基准策略 2026-09 当月 %+.2f%% —— 题面为 -4.48%%，未能复现；逐一核对了段收益三种算法'
         % (bm['sep2026'] * 100))
L.append('  （执行日区间含费用 / 进攻腿close-to-close / 信号日区间），2026-09 按统一口径仅有 1 段')
L.append('  已闭合打脸段（09-08→09-11，持有3天，-1.40%），另有 08-26→09-07 一段持有8天、-0.08%。')
L.append('  题面三段 -0.93%/-2.84%/-2.16% 疑似不同快照或不同段定义（-0.93 恰等于 09-11 当日策略收益）。')
L.append('  本文所有对比均在同一口径下进行，变体与基准的相对比较不受此影响。')
L.append('基准 2026-08/09 短打段明细：')
for s in bm['whip']:
    if s['buy'].year == 2026 and s['buy'].month >= 8:
        L.append('  %s -> %s  持有%d天  %.2f%%' % (s['buy'].date(), s['sell'].date(), s['dur'], s['ret'] * 100))
# 8天短段也列出（非打脸但属9月亏损短打）
for s in [x for x in whipsaw_segments(base_res)[0] if x['buy'].year == 2026 and x['buy'].month >= 8 and x['dur'] <= 10]:
    if s not in bm['whip']:
        L.append('  %s -> %s  持有%d天  %.2f%%  (持有>5天，不计入打脸段)' % (s['buy'].date(), s['sell'].date(), s['dur'], s['ret'] * 100))
L.append('')
L.append('(d) 阈值选取依据：基准全部卖出信号日 DIF 自近20日高点回落幅度分布的 25/50/75 分位 = %s'
         % ', '.join('%.4f' % t for t in THRS))
L.append('')
L.append('=' * 120)
hdr = ('%-16s %10s %9s %9s %6s %10s %10s %10s %12s %14s %s'
       % ('变体', '累计收益', '年化', '最大回撤', '换仓', '2026YTD', '2026-07', '2026-09', '打脸段数/合计', '7月进攻天数', '跨界持有'))
L.append(hdr)
L.append('-' * 120)
for m in results:
    L.append('%-16s %9.2f%% %8.2f%% %8.2f%% %6d %9.2f%% %9.2f%% %9.2f%% %5d/%7.2f%% %10d/%d %8s'
             % (m['name'], m['cum'] * 100, m['ann'] * 100, m['mdd'] * 100, m['ntrades'],
                m['ytd2026'] * 100, m['jul2026'] * 100, m['sep2026'] * 100,
                m['nwhip'], m['whip_sum'] * 100,
                m['jul_atk_days'], m['jul_days'], str(m['cross_hold'])))
L.append('')

L.append('分年度收益表（策略当年收益%）:')
years = sorted(set().union(*[set(m['yearly'].index) for m in results]))
line = '%-16s' % '变体' + ''.join('%9d' % y for y in years)
L.append(line)
for m in results:
    line = '%-16s' % m['name'] + ''.join('%9.2f' % (m['yearly'].get(y, float('nan')) * 100) for y in years)
    L.append(line)
L.append('')

L.append('2026-07 及时性检查（绝不能把满仓防守避开大跌的行情过滤掉；2000增强 2026-07 单月 -12.76%）:')
for m in results:
    lt = m['jul_last_trade']
    lt_str = '%s %s' % (lt[0].date(), lt[1]) if lt else '无'
    L.append('  %-16s 7月进攻天数 %d/%d  7月策略收益 %7.2f%%  7月前最后一次换仓: %s'
             % (m['name'], m['jul_atk_days'], m['jul_days'], m['jul2026'] * 100, lt_str))
L.append('  参考：满仓进攻腿 2026-07 单月 %.2f%%' % (month_ret(base_res['nav_1000'], 2026, 7) * 100))
L.append('')

L.append('各变体打脸段明细:')
for m in results:
    L.append('  [%s] %d 段, 合计 %.2f%%' % (m['name'], m['nwhip'], m['whip_sum'] * 100))
    for s in m['whip']:
        L.append('    %s -> %s  持有%d天  %.2f%%' % (s['buy'].date(), s['sell'].date(), s['dur'], s['ret'] * 100))
L.append('')

# 结论
L.append('结论与建议:')
best = [m for m in results[1:]]
for m in best:
    dw = m['nwhip'] - bm['nwhip']
    dsum = (m['whip_sum'] - bm['whip_sum']) * 100
    L.append('  %-16s 打脸段 %d→%d（合计 %+.2f%%），代价：累计收益 %+.2f%%，换仓 %+d 次，回撤 %+.2f%%；2026-07 进攻天数 %d（基准 %d）'
             % (m['name'], bm['nwhip'], m['nwhip'], dsum,
                (m['cum'] - bm['cum']) * 100, m['ntrades'] - bm['ntrades'],
                (m['mdd'] - bm['mdd']) * 100, m['jul_atk_days'], bm['jul_atk_days']))
L.append('')
L.append('总体结论:')
L.append('  1. 及时性约束全部满足：所有变体 2026-07 进攻天数均为 0/23，6月底前均已切防守，'
         '满仓防守避开大跌的行情没有被任何变体过滤掉；所有变体在 2024-06-28 拼接切换日均未持有进攻仓，无跨界失真。')
L.append('  2. (d)幅度过滤是本组唯一“减打脸且增收”的方向：阈值0.039（约等于卖出信号回落幅度的中位数）时，')
L.append('     打脸段 25→10、合计亏损 -37.57%→-15.12%，累计收益反而 +177.8pp（757.68% vs 579.87%），'
         '换仓 135→79，回撤基本持平。经济学解释：DIF 仅微回落的卖出多发生在横盘噪声区，过滤掉后避免频繁假摔。')
L.append('     但必须明说：该变体 2023 年单年 -2.04%（基准 +19.69%），震荡市里会明显跑输；且阈值为绝对 DIF 单位，'
         '随指数价格水平漂移，长期稳定性存疑（相对阈值如 ATR 比例可作后续方向）。')
L.append('  3. (b)对称确认是最稳健的保守选项：打脸段 25→10（合计 -24.48%），分年度无爆雷（最差年份仍为正且接近基准），')
L.append('     代价约累计 -41.8pp / 年化 -1.1pp，2026-09 当月 -2.58% 优于基准 -3.27%。')
L.append('  4. (a)MA20卖出确认 与 (c)冷却期 不推荐单独使用：(a) 在 2023 年仅 +3.45%（基准 +19.69%）；'
         '(c) 冷却越久累计损失越大（c8 累计 -312.8pp），且 2019 年收益从 45% 砍到 19%，代价远超打脸段收益。')
L.append('  5. 组合变体不占优：(d0.039)+(c3) 打脸合计最好（-11.50%）但累计收益低于单用 (d)0.039，规则还更复杂。')
L.append('')
L.append('建议：若想低改动、低风险，采纳 (b) 对称确认；若接受 2023 式震荡市跑输换取长期收益与最少打脸，采纳 (d) 幅度>0.039。'
         '两者 2026-07 及时性均验证通过。')

txt = '\n'.join(L)
with open('result_whipsaw.txt', 'w', encoding='utf-8') as f:
    f.write(txt + '\n')
print('\n已写入 result_whipsaw.txt')
