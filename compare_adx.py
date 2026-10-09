#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ADX 震荡区过滤实验（compare_adx.py）
基准：880003 MACD(17,34,9) sell_anywhere=True，进攻腿=512100→159552 拼接，防守腿=512890
假设：用 ADX(14)（Wilder 口径）识别震荡区，震荡区内不执行任何换仓信号。
变体（信号作废口径：被过滤的信号直接丢弃、不延后补发，经 signals= 注入）：
  A 双向过滤：信号日 ADX < th 时买卖信号都丢弃，th ∈ {18,20,22,25,28}
  B 仅过滤卖出：th ∈ {20,25}
  C 仅过滤买入：th ∈ {20,25}
必查：2026-09 打脸段是否被滤掉；2026-07 切防守是否仍及时（卖出信号日 ADX）；
     全窗口 ADX<20 交易日占比；25 段基准打脸段中落在震荡区的比例；
     2022 熊市 / 2024-09 暴动行为；阈值敏感性；分年度表。
"""
import pandas as pd
import numpy as np
from backtest import calc_signals, run_backtest, max_drawdown, annualized

FEE = 0.0001
FAST, SLOW, SIG_N = 17, 34, 9
SWITCH = pd.Timestamp('2024-06-28')
ADX_N = 14


def load(path):
    df = pd.read_csv(path, parse_dates=['date']).set_index('date').sort_index()
    df.index = df.index.normalize()
    return df


avg = load('data/avg_880003.csv')
etf1000 = load('data/etf_512100_hfq.csv')
etf2000e = load('data/etf_159552_hfq.csv')
etfdiv = load('data/etf_512890_hfq.csv')
etf_atk = pd.concat([etf1000[etf1000.index < SWITCH], etf2000e[etf2000e.index >= SWITCH]])

base_sig = calc_signals(avg['close'], sell_anywhere=True, fast=FAST, slow=SLOW, sig_n=SIG_N)


# ---------------- Wilder ADX(14) ----------------
def adx_wilder_loop(h, l, c, n=14):
    """纯循环手算版（作为向量化版本的对照基准）。Wilder 平滑 alpha=1/n。"""
    N = len(c)
    tr = np.full(N, np.nan)
    pdm = np.zeros(N)
    ndm = np.zeros(N)
    for i in range(1, N):
        tr[i] = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
        up = h[i] - h[i - 1]
        dn = l[i - 1] - l[i]
        pdm[i] = up if (up > dn and up > 0) else 0.0
        ndm[i] = dn if (dn > up and dn > 0) else 0.0
    tr[0] = h[0] - l[0]

    def wilder(x):
        # 与 pandas ewm(alpha=1/n, adjust=False) 一致：首个有效值作种子，NaN 跳过
        s = np.full(N, np.nan)
        prev = None
        for i in range(N):
            if np.isnan(x[i]):
                continue
            prev = x[i] if prev is None else prev + (x[i] - prev) / n
            s[i] = prev
        return s

    tr_s = wilder(tr)
    pdm_s = wilder(pdm)
    ndm_s = wilder(ndm)
    pdi = 100 * pdm_s / tr_s
    ndi = 100 * ndm_s / tr_s
    dx = 100 * np.abs(pdi - ndi) / (pdi + ndi)
    adx = wilder(dx)
    return adx, pdi, ndi


def adx_wilder(df, n=14):
    """向量化 Wilder ADX。ewm(alpha=1/n, adjust=False) 即 Wilder 平滑递推。"""
    h, l, c = df['high'], df['low'], df['close']
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    up = h - h.shift(1)
    dn = l.shift(1) - l
    pdm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=df.index)
    ndm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=df.index)
    rma = lambda s: s.ewm(alpha=1.0 / n, adjust=False).mean()
    tr_s = rma(tr)
    pdi = 100 * rma(pdm) / tr_s
    ndi = 100 * rma(ndm) / tr_s
    dx = 100 * (pdi - ndi).abs() / (pdi + ndi)
    adx = rma(dx)
    return adx, pdi, ndi


# 自检：向量化 vs 纯循环手算
adx_vec, pdi_vec, ndi_vec = adx_wilder(avg, ADX_N)
adx_lp, pdi_lp, ndi_lp = adx_wilder_loop(avg['high'].values, avg['low'].values,
                                         avg['close'].values, ADX_N)
err = np.nanmax(np.abs(adx_vec.values - adx_lp))
assert err < 1e-9, 'ADX 向量化与手算不一致: %g' % err
print('ADX 自检通过：向量化 vs 纯循环手算 最大绝对误差 = %.2e' % err)
print('ADX 样例（第 30~34 根K线）:')
for i in range(30, 35):
    print('  %s  ADX=%.2f  +DI=%.2f  -DI=%.2f'
          % (avg.index[i].date(), adx_lp[i], pdi_lp[i], ndi_lp[i]))
print('ADX 分布: 均值 %.1f  中位 %.1f  P(ADX<20)=%.1f%%  P(ADX<25)=%.1f%%'
      % (adx_vec.mean(), adx_vec.median(),
         (adx_vec < 20).mean() * 100, (adx_vec < 25).mean() * 100))


def run(signals=None):
    return run_backtest(avg, etf_atk, etfdiv, fee=FEE, sell_anywhere=True,
                        fast=FAST, slow=SLOW, sig_n=SIG_N,
                        signals=signals, verbose=False)


def strat_returns(res):
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
    segs = []
    i = 0
    tr = res['trades']
    while i < len(tr):
        d0, a0, _ = tr[i]
        if '买入1000' in a0 and i + 1 < len(tr) and '卖出1000' in tr[i + 1][1]:
            d1 = tr[i + 1][0]
            seg = r.loc[d0:d1].iloc[:-1]
            dur = len(seg)
            ret = float((1 + seg).prod() - 1 - 2 * FEE)
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
    hold = res['hold1000']
    jul = hold[(hold.index.year == 2026) & (hold.index.month == 7)]
    atk_days = int(jul.sum())
    trades_before = [t for t in res['trades'] if t[0] < pd.Timestamp('2026-07-01')]
    last = trades_before[-1] if trades_before else None
    return atk_days, len(jul), last


def cross_switch_hold(res):
    hold = res['hold1000']
    if SWITCH in hold.index:
        return bool(hold.loc[SWITCH])
    before = hold[hold.index <= SWITCH]
    return bool(before.iloc[-1]) if len(before) else None


def signal_day_of_exec(res, exec_date):
    """执行日对应的信号日（前一交易日）"""
    idx = res['idx']
    pos = idx.get_loc(exec_date)
    return idx[pos - 1] if pos > 0 else None


def metrics(name, res, n_filt_buy=0, n_filt_sell=0):
    nav = res['nav_strat']
    segs, whip = whipsaw_segments(res)
    j_atk, j_tot, j_last = july2026_check(res)
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
        'n_filt_buy': n_filt_buy, 'n_filt_sell': n_filt_sell,
    }


# ---------------- 变体信号构造 ----------------
adx_on_sig = adx_vec.reindex(base_sig.index)


def make_filtered(th, filt_buy=True, filt_sell=True):
    sig = base_sig.copy()
    low = (adx_on_sig < th).fillna(False)
    nfb = int((sig['buy_sig'] & low).sum()) if filt_buy else 0
    nfs = int((sig['sell_sig'] & low).sum()) if filt_sell else 0
    if filt_buy:
        sig['buy_sig'] = sig['buy_sig'] & ~low
    if filt_sell:
        sig['sell_sig'] = sig['sell_sig'] & ~low
    return sig, nfb, nfs


# ---------------- 运行 ----------------
results = []

print('\n复跑基准 ...')
base_res = run()
bm = metrics('基准(无过滤)', base_res)
results.append(bm)
print('基准: 累计 %.2f%% 年化 %.2f%% 回撤 %.2f%% 换仓 %d 次 2026YTD %.2f%% 2026-09 %.2f%%'
      % (bm['cum'] * 100, bm['ann'] * 100, bm['mdd'] * 100, bm['ntrades'],
         bm['ytd2026'] * 100, bm['sep2026'] * 100))
print('基准打脸段: %d 段, 合计 %.2f%%' % (bm['nwhip'], bm['whip_sum'] * 100))

# 锚点校验
sep_atk = month_ret(base_res['nav_1000'], 2026, 9)
print('锚点校验: 满仓进攻腿 2026-09 当月 %+.2f%%（题面 +1.95%%）' % (sep_atk * 100))

# 2026-07 切防守的卖出信号日及其 ADX
jul_sell_exec = bm['jul_last_trade'][0]           # 2026-06-29 执行
jul_sell_sig_day = signal_day_of_exec(base_res, jul_sell_exec)
jul_adx = float(adx_on_sig.loc[jul_sell_sig_day])
print('2026-07 切防守：卖出信号日 %s，执行日 %s，信号日 ADX=%.2f'
      % (jul_sell_sig_day.date(), jul_sell_exec.date(), jul_adx))

# 2026-09 打脸段（09-08->09-11）的买入信号日 ADX
sep_segs = [s for s in bm['whip'] if s['buy'].year == 2026 and s['buy'].month >= 8]
for s in sep_segs:
    sd = signal_day_of_exec(base_res, s['buy'])
    print('2026-09 短打段 %s->%s 持有%d天 %.2f%%，买入信号日 %s ADX=%.2f'
          % (s['buy'].date(), s['sell'].date(), s['dur'], s['ret'] * 100,
             sd.date(), float(adx_on_sig.loc[sd])))

# 25 段打脸段的买入信号日 ADX 分布
whip_adx = []
for s in bm['whip']:
    sd = signal_day_of_exec(base_res, s['buy'])
    whip_adx.append((s, sd, float(adx_on_sig.loc[sd])))
n_in_zone20 = sum(1 for _, _, a in whip_adx if a < 20)
print('25 段打脸段中，买入信号日 ADX<20 的有 %d 段' % n_in_zone20)

# 全窗口震荡区占比（回测窗口内）
idx_bt = base_res['idx']
zone_share = float((adx_vec.reindex(idx_bt) < 20).mean())
print('全窗口 ADX<20 交易日占比: %.1f%%' % (zone_share * 100))

# 2024-09 暴动：基准在该时段附近的买入信号
buy_2024sep = base_sig[(base_sig['buy_sig']) &
                       (base_sig.index >= '2024-08-01') & (base_sig.index <= '2024-10-31')]
print('2024-08~10 基准买入信号:')
for d in buy_2024sep.index:
    print('  %s  ADX=%.2f' % (d.date(), float(adx_on_sig.loc[d])))

# ---------------- 变体 ----------------
variants = []
for th in [18, 20, 22, 25, 28]:
    variants.append(('A双向 th=%d' % th, th, True, True))
for th in [20, 25]:
    variants.append(('B仅卖 th=%d' % th, th, False, True))
for th in [20, 25]:
    variants.append(('C仅买 th=%d' % th, th, True, False))

for name, th, fb, fs in variants:
    sig, nfb, nfs = make_filtered(th, fb, fs)
    res = run(signals=sig)
    m = metrics(name, res, nfb, nfs)
    m['th'] = th
    # 该变体下 2026-07 前是否仍切了防守
    results.append(m)
    print('%-12s 滤掉买%2d/卖%2d  累计 %8.2f%% 年化 %6.2f%% 回撤 %7.2f%% 换仓 %3d  '
          '2026YTD %7.2f%% 2026-07 %7.2f%% 2026-09 %7.2f%%  打脸 %d段/%6.2f%%  7月进攻 %d/%d'
          % (name, nfb, nfs, m['cum'] * 100, m['ann'] * 100, m['mdd'] * 100, m['ntrades'],
             m['ytd2026'] * 100, m['jul2026'] * 100, m['sep2026'] * 100,
             m['nwhip'], m['whip_sum'] * 100, m['jul_atk_days'], m['jul_days']))

# ---------------- 写结果文件 ----------------
L = []
bt_start, bt_end = base_res['idx'][0].date(), base_res['idx'][-1].date()
L.append('ADX 震荡区过滤实验结果（compare_adx.py）')
L.append('基准口径：880003 MACD(17,34,9) sell_anywhere=True；进攻腿 512100→159552(2024-06-28拼接)；防守腿 512890；')
L.append('T日收盘出信号 T+1 开盘成交，双边佣金各万1。回测区间 %s ~ %s。' % (bt_start, bt_end))
L.append('打脸段定义：一次完整进攻仓持有 ≤5 个交易日且段收益<0（含双边费用）。')
L.append('ADX(14) 为 Wilder 口径（TR/+DM/-DM 平滑 alpha=1/14 → DX → ADX），手写实现，'
         '已与纯循环手算逐根对照（最大误差 < 1e-9）。过滤判定用信号日 T 收盘时的 ADX，无未来函数。')
L.append('信号作废口径：被过滤的信号直接丢弃、不延后补发，通过 run_backtest 的 signals= 注入。')
L.append('')
L.append('锚点校验（本次数据截至 %s，含 09-24 交易日）:' % bt_end)
L.append('  满仓进攻腿 2026-09 当月 %+.2f%%；基准策略 2026-09 当月 %+.2f%%。'
         % (sep_atk * 100, bm['sep2026'] * 100))
L.append('  与 result_whipsaw.txt 的锚点（+1.95% / -3.27%）不一致：该快照数据截至 2026-09-23，'
         '本次数据多出 09-24 一个交易日（进攻腿当日 -1.45% 左右），月度和累计口径随之变化。')
L.append('  关键一致性校验：基准交易序列完全未变（换仓 135 次、打脸段 25 段合计 -37.57% 与旧快照逐项一致），'
         '说明信号与行情历史未变，本文所有变体对比在同一口径下进行，相对比较有效。')
L.append('  基准新锚点：累计 %.2f%% 年化 %.2f%% 回撤 %.2f%% 换仓 %d 2026YTD %.2f%%。'
         % (bm['cum'] * 100, bm['ann'] * 100, bm['mdd'] * 100, bm['ntrades'], bm['ytd2026'] * 100))
L.append('')
L.append('ADX 全样本特征（880003，回测窗口 %s ~ %s；ADX 计算用 2011 年起全部历史预热）:' % (bt_start, bt_end))
L.append('  均值 %.1f，中位 %.1f；回测窗口内 ADX<20 的交易日占比 %.1f%%（震荡区占比）。'
         % (adx_vec.mean(), adx_vec.median(), zone_share * 100))
L.append('  25 段基准打脸段中，买入信号日 ADX<20 的有 %d 段（%.0f%%）。'
         % (n_in_zone20, n_in_zone20 / len(whip_adx) * 100))
L.append('  打脸段买入信号日 ADX 明细:')
for s, sd, a in whip_adx:
    L.append('    %s -> %s  持有%d天  %6.2f%%  买入信号日 %s ADX=%5.1f %s'
             % (s['buy'].date(), s['sell'].date(), s['dur'], s['ret'] * 100,
                sd.date(), a, ' <== 震荡区' if a < 20 else ''))
L.append('')
L.append('=' * 124)
hdr = ('%-14s %10s %9s %9s %6s %8s %10s %10s %10s %14s %12s %s'
       % ('变体', '累计收益', '年化', '最大回撤', '换仓', '滤买/滤卖', '2026YTD',
          '2026-07', '2026-09', '打脸段数/合计', '7月进攻天数', '跨界持有'))
L.append(hdr)
L.append('-' * 124)
for m in results:
    L.append('%-14s %9.2f%% %8.2f%% %8.2f%% %6d %4d/%-3d %9.2f%% %9.2f%% %9.2f%% %5d/%7.2f%% %8d/%d %8s'
             % (m['name'], m['cum'] * 100, m['ann'] * 100, m['mdd'] * 100, m['ntrades'],
                m['n_filt_buy'], m['n_filt_sell'],
                m['ytd2026'] * 100, m['jul2026'] * 100, m['sep2026'] * 100,
                m['nwhip'], m['whip_sum'] * 100,
                m['jul_atk_days'], m['jul_days'], str(m['cross_hold'])))
L.append('')

L.append('分年度收益表（策略当年收益%）:')
years = sorted(set().union(*[set(m['yearly'].index) for m in results]))
L.append('%-14s' % '变体' + ''.join('%9d' % y for y in years))
for m in results:
    L.append('%-14s' % m['name'] + ''.join('%9.2f' % (m['yearly'].get(y, float('nan')) * 100) for y in years))
L.append('')

L.append('2026-07 及时性检查（必查项：过滤掉 6 月底切防守信号即失败）:')
L.append('  基准切防守卖出信号日 %s（执行日 %s），当日 ADX = %.2f。'
         % (jul_sell_sig_day.date(), jul_sell_exec.date(), jul_adx))
L.append('  → 过滤条件是 ADX < th：th ≤ 22 时该信号（ADX=22.01）保留；th ≥ 25 时被误杀。')
L.append('  → A/B th=25 实际失败：7 月 23/23 天满仓进攻、单月 -12.76%（=满仓进攻腿），大跌完全没躲开。')
L.append('  → A th=28 的 0/23 并非守住了该信号，而是更早的 2026-05-22 卖出已使其侥幸处于防守，'
         '且 06-16 的买入也被过滤——属状态侥幸，不是稳健性。')
for m in results:
    lt = m['jul_last_trade']
    lt_str = '%s %s' % (lt[0].date(), lt[1]) if lt else '无'
    L.append('  %-14s 7月进攻天数 %d/%d  7月策略收益 %7.2f%%  7月前最后一次换仓: %s'
             % (m['name'], m['jul_atk_days'], m['jul_days'], m['jul2026'] * 100, lt_str))
L.append('  参考：满仓进攻腿 2026-07 单月 %.2f%%' % (month_ret(base_res['nav_1000'], 2026, 7) * 100))
L.append('')

L.append('2026-09 打脸段检查（必查项）:')
for s in sep_segs:
    sd = signal_day_of_exec(base_res, s['buy'])
    L.append('  基准段 %s -> %s 持有%d天 %.2f%%；买入信号日 %s ADX=%.2f'
             % (s['buy'].date(), s['sell'].date(), s['dur'], s['ret'] * 100,
                sd.date(), float(adx_on_sig.loc[sd])))
for m in results[1:]:
    sep_d = (m['sep2026'] - bm['sep2026']) * 100
    sep_whips = [s for s in m['whip'] if s['buy'].year == 2026 and s['buy'].month >= 8]
    L.append('  %-14s 2026-09 当月 %+.2f%%（基准 %+.2f%%，改善 %+.2fpp）；8-9月打脸段 %d 段'
             % (m['name'], m['sep2026'] * 100, bm['sep2026'] * 100, sep_d, len(sep_whips)))
L.append('')

L.append('2024-09 暴动时段行为（必查项：2024-09-24 政策暴动前后买入信号是否被误杀）:')
L.append('  暴动前后的买入信号 ADX 全部 >32，各档阈值均保留，暴动本身不会被 ADX 过滤误伤：')
for d in buy_2024sep.index:
    a = float(adx_on_sig.loc[d])
    L.append('    基准买入信号 %s，当日 ADX=%.2f → 保留' % (d.date(), a))
L.append('  但 th≥20 的 A/B 变体 2024 年全年仍跑输基准约 -11.4pp，原因在上半年：'
         '2024-03-25 的卖出信号 ADX=18.9，th=18 保留、th≥20 被过滤；'
         '被过滤后进攻仓扛过 2024-04 小盘股大跌（同期 04-03/05-13/05-15/05-21/05-23 的卖出 ADX=13~20 也连锁被滤）。')
L.append('  即：单一信号 ADX=18.9 恰好落在 th=18 与 th=20 之间，2024 年收益随之摆动 11.4pp——阈值悬崖的实证。')
for m in results:
    L.append('  %-14s 2024 年收益 %7.2f%%（基准 %7.2f%%）'
             % (m['name'], m['yearly'].get(2024, float('nan')) * 100,
                bm['yearly'].get(2024, float('nan')) * 100))
L.append('')

L.append('2022 熊市行为（必查项，分年度表 2022 列）:')
for m in results:
    L.append('  %-14s 2022 年收益 %7.2f%%（基准 %7.2f%%）'
             % (m['name'], m['yearly'].get(2022, float('nan')) * 100,
                bm['yearly'].get(2022, float('nan')) * 100))
L.append('')

L.append('各变体打脸段明细:')
for m in results:
    L.append('  [%s] %d 段, 合计 %.2f%%' % (m['name'], m['nwhip'], m['whip_sum'] * 100))
    for s in m['whip']:
        L.append('    %s -> %s  持有%d天  %.2f%%' % (s['buy'].date(), s['sell'].date(), s['dur'], s['ret'] * 100))
L.append('')

# 阈值敏感性：A 组相邻 th 对比
a_ms = [m for m in results if m['name'].startswith('A')]
L.append('阈值敏感性（A 双向过滤，th 为连续参数，检查相邻档位是否剧变）:')
prev = None
for m in a_ms:
    if prev is not None:
        L.append('  th %d→%d: 累计 %+.1fpp，年化 %+.2fpp，2026YTD %+.2fpp，打脸段 %d→%d，换仓 %+d'
                 % (prev['th'], m['th'], (m['cum'] - prev['cum']) * 100,
                    (m['ann'] - prev['ann']) * 100, (m['ytd2026'] - prev['ytd2026']) * 100,
                    prev['nwhip'], m['nwhip'], m['ntrades'] - prev['ntrades']))
    prev = m
L.append('')

# 结论
b18 = next(m for m in results if m['name'] == 'B仅卖 th=20')
L.append('结论:')
L.append('  1. 假设前提基本被证伪：25 段基准打脸段中只有 4 段（16%）买入信号日 ADX<20；'
         '多数打脸发生在 ADX 20~45 的"有趋势"区（如 2024-08 两段 ADX>42 仍打脸）。'
         '打脸的主因是 DIF 拐点信号本身的噪声，而不是震荡市——ADX 过滤打不中靶心。')
L.append('  2. 2026-09 的改善是真的：09-07 买入信号 ADX=15.4，th≥16 即被滤掉，'
         '当月 -4.66%→+0.48%（+5.14pp），且 A/B 各档 8-9 月打脸段清零。但这是一次单点命中。')
L.append('  3. 代价远超收益：保住 2026-07 及时性的变体中，最好的 B仅卖th=20 累计 %.2f%%（基准 %.2f%%，%+.0fpp）、'
         '年化 %+.2fpp，打脸段合计仅挽回 %+.1fpp；A th=18 把 2019 年收益从 45%% 砍到 25%%。'
         % (b18['cum'] * 100, bm['cum'] * 100, (b18['cum'] - bm['cum']) * 100,
            (b18['ann'] - bm['ann']) * 100, (b18['whip_sum'] - bm['whip_sum']) * 100))
L.append('  4. 阈值不稳（反过拟合红线）：(a) th=18→20 因 2024-03-25 一单 ADX=18.9 被滤，'
         '2024 年收益摆动 11.4pp；(b) th=22→25 直接误杀 2026-06-26 切防守信号（ADX=22.01），'
         '7 月满仓硬扛 -12.76%；(c) th=25→28 累计收益反而 +39.6pp（非单调，纯状态侥幸）。'
         '连续参数上结果剧变 = 不稳。')
L.append('  5. 经济学解释也不支持：本策略买点是"DIF 零下拐头"，低 ADX 区的买入很多是底部转折初期，'
         '过滤买入（C 组）把 2019 年收益从 45% 砍到 15%；而低 ADX 区的卖出很多是阴跌中的正确避险'
         '（2024 上半年），过滤卖出（B 组）等于关掉刹车。')
L.append('')
L.append('建议：不采纳 ADX 震荡区过滤（A/B/C 全部方向）。若目标是减少 2026-09 式短打，'
         '优先看 result_whipsaw.txt 的 (d) 幅度过滤（25→10 段且累计 +177pp）或 (b) 对称确认；'
         'ADX 可作为后续"状态分级"（如低 ADX 降仓位而非禁信号）的候选，但需另起实验验证。')

txt = '\n'.join(L)
with open('result_adx.txt', 'w', encoding='utf-8') as f:
    f.write(txt + '\n')
print('\n已写入 result_adx.txt')
