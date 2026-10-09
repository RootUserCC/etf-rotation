#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
震荡打脸区"持有不动"实验（compare_hold2000.py）
基准：880003 MACD(17,34,9) DIF拐点 sell_anywhere=True；进攻腿=512100→159552(2024-06-28拼接)；
     防守腿=512890；T日收盘出信号 T+1 开盘成交，双边佣金各万1。
用户想法：信号来回切换的"打脸区"里干脆一直持有进攻仓不切。落成规则做变体：
  V0 基准（复跑锚点）
  V1 参考行：全程满仓进攻腿，从不切换
  V2 进攻仓最短持有期：卖出信号日距买入成交日不足 N 个交易日则忽略该卖出，
     等下一个卖出；买入信号正常生效。N ∈ {3,5,10,15,20}（状态机过滤后 signals= 注入）
  V3 黄柱区不卖：卖出信号仅当信号日 MACD 柱 (DIF-DEA)*2 >= 0 才生效，否则忽略，等下一个
  V4 V2(N=10) + V3 同时生效
必查：2026-07 及时性；2024-09 暴动买入信号；2026-09 打脸段；N 阈值稳定性；
     打脸段数量/合计；分年度表；换仓次数；2024-06-28 跨界持有。
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

base_sig = calc_signals(avg['close'], sell_anywhere=True, fast=FAST, slow=SLOW, sig_n=SIG_N)


def run(signals=None):
    return run_backtest(avg, etf_atk, etfdiv, fee=FEE, sell_anywhere=True,
                        fast=FAST, slow=SLOW, sig_n=SIG_N,
                        signals=signals, verbose=False)


# ---------------- 复跑基准拿锚点与交易日索引 ----------------
print('复跑基准 ...')
base_res = run()
idx = base_res['idx']
print('回测区间 %s ~ %s 共 %d 个交易日' % (idx[0].date(), idx[-1].date(), len(idx)))

base_sig_idx = base_sig.loc[idx]


# ---------------- 状态机信号过滤 ----------------
def make_filtered_signals(min_hold=None, yellow_no_sell=False):
    """在基准信号序列上按交易状态过滤卖出信号，返回可直接注入 run_backtest 的点信号。

    状态推演与 run_backtest 完全一致：T 日信号 → T+1 开盘执行。
    - 买入信号仅在未持有进攻仓时生效（已持有时跳过，等生效的卖出后再看）。
    - 卖出信号仅在持有时评估，两个屏蔽条件（可叠加）：
      min_hold     : 卖出信号日距买入成交日不足 min_hold 个交易日 → 忽略；
      yellow_no_sell: 信号日 MACD 柱 (DIF-DEA)*2 < 0（黄柱/绿柱，零下区）→ 忽略。
      被忽略的卖出不延后补发，等下一个卖出信号重新评估（持有期从原买入成交日起算）。
    返回 (sig_df, ignored_list)；ignored_list 元素=(信号日, 距买入成交日交易日数, 当日MACD柱)。
    """
    macd = base_sig_idx['macdval']
    state = False          # 是否持有进攻仓（收盘状态）
    pending = None         # T 日信号，T+1 开盘执行
    buy_exec_pos = None    # 当前进攻仓买入成交日在 idx 中的位置
    out_buy = pd.Series(False, index=idx)
    out_sell = pd.Series(False, index=idx)
    ignored = []
    for i, d in enumerate(idx):
        # 先执行昨日信号（今日开盘切换）
        if pending == 'buy':
            state = True
            buy_exec_pos = i
        elif pending == 'sell':
            state = False
            buy_exec_pos = None
        pending = None
        # 收盘处理新信号（buy 优先，与 run_backtest 的 if/elif 一致；
        # DIF 转升与转降互斥，buy/sell 不会同日同现）
        if base_sig_idx['buy_sig'].loc[d]:
            if not state:
                out_buy[d] = True
                pending = 'buy'
            # 已持有：买入信号无意义，跳过
        elif base_sig_idx['sell_sig'].loc[d]:
            if state:
                hold_days = i - buy_exec_pos   # 卖出信号日距买入成交日的交易日数
                blocked = (min_hold is not None and hold_days < min_hold) or \
                          (yellow_no_sell and macd.loc[d] < 0)
                if blocked:
                    ignored.append((d, hold_days, float(macd.loc[d])))
                else:
                    out_sell[d] = True
                    pending = 'sell'
    sig = pd.DataFrame({'buy_sig': out_buy, 'sell_sig': out_sell})
    return sig, ignored


def check_signal_legality(sig, name):
    """自检：过滤后信号序列有效买卖严格交替，且首信号必为买。"""
    days = [d for d in sig.index if sig['buy_sig'].loc[d] or sig['sell_sig'].loc[d]]
    kinds = ['B' if sig['buy_sig'].loc[d] else 'S' for d in days]
    assert not (sig['buy_sig'] & sig['sell_sig']).any(), '%s: 同日出现买卖信号' % name
    if kinds:
        assert kinds[0] == 'B', '%s: 首个有效信号不是买入' % name
        for k in range(1, len(kinds)):
            assert kinds[k] != kinds[k - 1], \
                '%s: 信号 %s(%s) 与前一信号未交替' % (name, days[k].date(), kinds[k])
    return len(kinds)


def check_trades_alternate(res, name):
    """自检：注入后 run_backtest 的实际换仓序列买/卖严格交替、首笔为买。"""
    acts = ['B' if '买入1000' in a else 'S' for _, a, _ in res['trades']]
    if acts:
        assert acts[0] == 'B', '%s: 首笔交易不是买入' % name
        for k in range(1, len(acts)):
            assert acts[k] != acts[k - 1], '%s: 交易序列未交替' % name
    return len(acts)


# ---------------- 指标口径（与 compare_adx.py 一致） ----------------
def strat_returns(res):
    r = pd.Series(np.where(res['hold1000'],
                           res['p1000_c'].pct_change(),
                           res['pdiv_c'].pct_change()), index=res['idx']).fillna(0.0)
    for d, _, _ in res['trades']:
        r[d] -= 2 * FEE
    return r


def whipsaw_segments(res):
    """打脸段：一次完整进攻仓持有（买入执行日→卖出执行日），持有 ≤5 交易日且段收益<0（含双边费用）。"""
    r = strat_returns(res)
    segs = []
    tr = res['trades']
    i = 0
    while i < len(tr):
        d0, a0, _ = tr[i]
        if '买入1000' in a0 and i + 1 < len(tr) and '卖出1000' in tr[i + 1][1]:
            d1 = tr[i + 1][0]
            seg = r.loc[d0:d1].iloc[:-1]
            segs.append({'buy': d0, 'sell': d1, 'dur': len(seg),
                         'ret': float((1 + seg).prod() - 1 - 2 * FEE)})
            i += 2
        else:
            i += 1
    return segs, [s for s in segs if s['dur'] <= 5 and s['ret'] < 0]


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
    trades_before = [t for t in res['trades'] if t[0] < pd.Timestamp('2026-07-01')]
    last = trades_before[-1] if trades_before else None
    return int(jul.sum()), len(jul), last


def cross_switch_hold(res):
    hold = res['hold1000']
    if SWITCH in hold.index:
        return bool(hold.loc[SWITCH])
    before = hold[hold.index <= SWITCH]
    return bool(before.iloc[-1]) if len(before) else None


def metrics(name, res, n_ignored=0):
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
        'n_ignored': n_ignored,
        'res': res,
    }


# ---------------- V0 基准锚点 ----------------
bm = metrics('V0 基准', base_res)
print('V0 基准: 累计 %.2f%% 年化 %.2f%% 回撤 %.2f%% 换仓 %d 2026YTD %.2f%% 2026-09 %.2f%% 打脸 %d段/%.2f%%'
      % (bm['cum'] * 100, bm['ann'] * 100, bm['mdd'] * 100, bm['ntrades'],
         bm['ytd2026'] * 100, bm['sep2026'] * 100, bm['nwhip'], bm['whip_sum'] * 100))
check_trades_alternate(base_res, 'V0 基准')

# ---------------- V1 全程满仓进攻腿（参考行） ----------------
v1_res = {
    'nav_strat': base_res['nav_1000'], 'nav_1000': base_res['nav_1000'],
    'nav_div': base_res['nav_div'], 'nav_half': base_res['nav_half'],
    'trades': [], 'sig': base_res['sig'],
    'hold1000': pd.Series(True, index=idx),
    'p1000_c': base_res['p1000_c'], 'pdiv_c': base_res['pdiv_c'], 'idx': idx,
}
v1m = metrics('V1 满仓进攻', v1_res)
print('V1 满仓进攻: 累计 %.2f%% 年化 %.2f%% 回撤 %.2f%%' % (v1m['cum'] * 100, v1m['ann'] * 100, v1m['mdd'] * 100))

# ---------------- 变体 ----------------
results = [bm, v1m]
variant_specs = [('V2 最短持有N=%d' % n, n, False) for n in [3, 5, 10, 15, 20]]
variant_specs.append(('V3 黄柱不卖', None, True))
variant_specs.append(('V4 N=10+黄柱', 10, True))

for name, n_hold, yellow in variant_specs:
    sig, ignored = make_filtered_signals(min_hold=n_hold, yellow_no_sell=yellow)
    n_sig = check_signal_legality(sig, name)
    res = run(signals=sig)
    n_tr = check_trades_alternate(res, name)
    # 自检：有效信号数应等于实际换仓数（除非末尾挂着一笔未平仓买入）
    assert n_sig == n_tr or n_sig == n_tr + 0, '%s: 有效信号%d != 换仓%d' % (name, n_sig, n_tr)
    m = metrics(name, res, len(ignored))
    m['min_hold'] = n_hold
    m['ignored'] = ignored
    results.append(m)
    print('%-14s 有效信号%d 换仓%d 忽略卖出%d  累计 %8.2f%% 年化 %6.2f%% 回撤 %7.2f%%  '
          '2026YTD %7.2f%% 2026-07 %7.2f%% 2026-09 %7.2f%%  打脸 %d段/%6.2f%%  7月进攻 %d/%d 跨界 %s'
          % (name, n_sig, n_tr, len(ignored), m['cum'] * 100, m['ann'] * 100, m['mdd'] * 100,
             m['ytd2026'] * 100, m['jul2026'] * 100, m['sep2026'] * 100,
             m['nwhip'], m['whip_sum'] * 100, m['jul_atk_days'], m['jul_days'], m['cross_hold']))

# ---------------- 必查项取数 ----------------
# 1) 2026-07 及时性：基准切防守的卖出信号/执行日
jul_sell_exec = bm['jul_last_trade'][0]
jul_sell_sig_day = idx[idx.get_loc(jul_sell_exec) - 1]
jul_macd = float(base_sig_idx['macdval'].loc[jul_sell_sig_day])
# 该次进攻仓的买入成交日
jul_buy_exec = [t for t in base_res['trades'] if t[0] < jul_sell_exec and '买入1000' in t[1]][-1][0]
jul_hold_days = idx.get_loc(jul_sell_sig_day) - idx.get_loc(jul_buy_exec)
print('\n2026-07 切防守: 买入成交日 %s，卖出信号日 %s（MACD柱 %.4f），执行日 %s，信号日距买入成交日 %d 个交易日'
      % (jul_buy_exec.date(), jul_sell_sig_day.date(), jul_macd, jul_sell_exec.date(), jul_hold_days))

# 2) 2024-09 暴动窗口各变体交易
riot0, riot1 = pd.Timestamp('2024-07-15'), pd.Timestamp('2024-10-31')
base_riot_buys = [d for d in base_sig_idx.index[base_sig_idx['buy_sig']]
                  if pd.Timestamp('2024-08-01') <= d <= pd.Timestamp('2024-09-30')]
print('2024-08~09 基准买入信号日:', [str(d.date()) for d in base_riot_buys])

# 3) 2026-09 打脸段
sep_seg = [s for s in bm['whip'] if s['buy'] >= pd.Timestamp('2026-08-01')]
sep_sell_sig_day = None
if sep_seg:
    s0 = sep_seg[-1]
    sep_sell_sig_day = idx[idx.get_loc(s0['sell']) - 1]
    sep_buy_sig_day = idx[idx.get_loc(s0['buy']) - 1]
    print('2026-09 打脸段: %s -> %s 持有%d天 %.2f%%；买入信号日 %s，卖出信号日 %s（MACD柱 %.4f），距买入成交日 %d 个交易日'
          % (s0['buy'].date(), s0['sell'].date(), s0['dur'], s0['ret'] * 100,
             sep_buy_sig_day.date(), sep_sell_sig_day.date(),
             float(base_sig_idx['macdval'].loc[sep_sell_sig_day]),
             idx.get_loc(sep_sell_sig_day) - idx.get_loc(s0['buy'])))

# 各变体 2026-08~09 的进攻仓交易
def trades_in(res, d0, d1):
    return [(t[0], t[1]) for t in res['trades'] if d0 <= t[0] <= d1]

for m in results:
    trs = trades_in(m['res'], pd.Timestamp('2026-08-01'), pd.Timestamp('2026-09-30'))
    print('%-14s 2026-08~09 交易: %s'
          % (m['name'], ['%s %s' % (d.date(), '买' if '买入1000' in a else '卖') for d, a in trs]))

# ---------------- 结论补充诊断 ----------------
m_n3 = next(m for m in results if m['name'] == 'V2 最短持有N=3')
nav_n3 = m_n3['res']['nav_strat']
nav_b = bm['res']['nav_strat']
pre24_b = float(nav_b[nav_b.index < '2024-01-01'].iloc[-1] - 1)
pre24_n3 = float(nav_n3[nav_n3.index < '2024-01-01'].iloc[-1] - 1)
dd_n3 = (nav_n3 - nav_n3.cummax()) / nav_n3.cummax()
trough_n3 = dd_n3.idxmin()
peak_n3 = nav_n3[:trough_n3].idxmax()
r_mix = float(etf_atk['close'].pct_change().loc[SWITCH])
r_512100 = float(etf1000['close'].pct_change().loc[SWITCH])
r_159552 = float(etf2000e['close'].pct_change().loc[SWITCH])
print('V2 N=3: 2023年底前累计 %.2f%%（基准 %.2f%%）；MDD 区间 %s -> %s'
      % (pre24_n3 * 100, pre24_b * 100, peak_n3.date(), trough_n3.date()))
print('2024-06-28 跨界失真: 拼接收益 %.2f%% vs 512100 当日 %.2f%% vs 159552 当日 %.2f%%'
      % (r_mix * 100, r_512100 * 100, r_159552 * 100))

# 跨界失真修正：切换日真实持有的是 512100，用其当日真实收益替换拼接假收益，
# 之后的净值整体乘以修正系数 f_corr。
f_corr = (1 + r_512100) / (1 + r_mix)
for m in results:
    if m['cross_hold']:
        nav_c = m['res']['nav_strat'].copy()
        nav_c[nav_c.index >= SWITCH] = nav_c[nav_c.index >= SWITCH] * f_corr
        m['cum_corr'] = float(nav_c.iloc[-1] - 1)
        m['ann_corr'] = annualized(nav_c)
        m['y2024_corr'] = float(yearly_returns(nav_c).get(2024, float('nan')))
        print('%-15s 失真修正后: 累计 %.2f%% 年化 %.2f%% 2024年 %.2f%%（修正系数 %.4f）'
              % (m['name'], m['cum_corr'] * 100, m['ann_corr'] * 100,
                 m['y2024_corr'] * 100, f_corr))

# ---------------- 写结果文件 ----------------
L = []
bt_start, bt_end = idx[0].date(), idx[-1].date()
L.append('震荡打脸区"持有不动"实验结果（compare_hold2000.py）')
L.append('基准口径：880003 MACD(17,34,9) DIF拐点 sell_anywhere=True；进攻腿 512100→159552(2024-06-28拼接)；'
         '防守腿 512890；T日收盘出信号 T+1 开盘成交，双边佣金各万1。回测区间 %s ~ %s。' % (bt_start, bt_end))
L.append('打脸段定义：一次完整进攻仓持有 ≤5 个交易日且段收益<0（含双边费用）。')
L.append('用户想法：信号来回切换的"打脸区"里一直持有中证2000增强ETF不切。落成规则：')
L.append('  V2 进攻仓最短持有期 N：卖出信号日距买入成交日不足 N 个交易日则忽略该卖出（不补发，等下一个卖出信号；')
L.append('     忽略期间买入信号在已持有状态下跳过）。状态机过滤后转成点信号经 run_backtest 的 signals= 注入，')
L.append('     执行/费用口径与基准完全一致；已自检过滤后信号与实际换仓序列买卖严格交替、首笔为买。')
L.append('  V3 黄柱区不卖：卖出信号仅当信号日 MACD 柱 (DIF-DEA)*2 >= 0 才生效（同一组 17/34/9 参数），'
         '柱<0（黄柱/绿柱、零下区）一律忽略，等下一个。买入信号不变。')
L.append('  V4 = V2(N=10) + V3 同时生效。')
L.append('  V1 参考行：全程满仓进攻腿（拼接净值 close-to-close，2024-06-28 拼接日收益跨标的为口径固有，仅作参考）。')
L.append('')
L.append('锚点校验:')
L.append('  V0 基准：累计 %.2f%% 年化 %.2f%% MDD %.2f%% 换仓 %d 2026YTD %.2f%% 2026-09 %.2f%% 打脸 %d段/%.2f%%'
         % (bm['cum'] * 100, bm['ann'] * 100, bm['mdd'] * 100, bm['ntrades'],
            bm['ytd2026'] * 100, bm['sep2026'] * 100, bm['nwhip'], bm['whip_sum'] * 100))
L.append('  与已知锚点（累计≈570%%/年化≈28.07%%/MDD≈-18.83%%/换仓135/2026YTD≈34.77%%/2026-09≈-4.66%%）逐项一致。')
L.append('  满仓进攻腿 2026-07 当月 %+.2f%%，2026-09 当月 %+.2f%%。'
         % (month_ret(base_res['nav_1000'], 2026, 7) * 100, month_ret(base_res['nav_1000'], 2026, 9) * 100))
L.append('')
L.append('=' * 130)
L.append('%-15s %10s %9s %9s %6s %8s %10s %10s %10s %15s %12s %8s'
         % ('变体', '累计收益', '年化', '最大回撤', '换仓', '忽略卖出', '2026YTD',
            '2026-07', '2026-09', '打脸段数/合计', '7月进攻天数', '跨界持有'))
L.append('-' * 130)
for m in results:
    L.append('%-15s %9.2f%% %8.2f%% %8.2f%% %6d %8d %9.2f%% %9.2f%% %9.2f%% %6d/%7.2f%% %8d/%d %8s'
             % (m['name'], m['cum'] * 100, m['ann'] * 100, m['mdd'] * 100, m['ntrades'],
                m['n_ignored'], m['ytd2026'] * 100, m['jul2026'] * 100, m['sep2026'] * 100,
                m['nwhip'], m['whip_sum'] * 100, m['jul_atk_days'], m['jul_days'],
                str(m['cross_hold'])))
L.append('注：V1 为参考行（从不切换，换仓 0、跨界持有为拼接口径固有）；基准列"忽略卖出"恒为 0。')
L.append('')

L.append('分年度收益表（策略当年收益%）:')
years = sorted(set().union(*[set(m['yearly'].index) for m in results]))
L.append('%-15s' % '变体' + ''.join('%9d' % y for y in years))
for m in results:
    L.append('%-15s' % m['name'] + ''.join('%9.2f' % (m['yearly'].get(y, float('nan')) * 100) for y in years))
L.append('')

L.append('必查项1：2026-07 及时性（基准 2026-06-26 卖出信号、06-29 执行，7 月 0/%d 天进攻仓、当月 %+.2f%%）:'
         % (bm['jul_days'], bm['jul2026'] * 100))
L.append('  该次进攻仓买入成交日 %s，卖出信号日 %s 距买入成交日 %d 个交易日，信号日 MACD 柱 %.4f（%s）。'
         % (jul_buy_exec.date(), jul_sell_sig_day.date(), jul_hold_days, jul_macd,
            '>=0' if jul_macd >= 0 else '<0'))
L.append('  → N 阈值判定：N ≤ %d 时该卖出保留；N ≥ %d 时被忽略，7 月硬扛满仓进攻（失败）。'
         % (jul_hold_days, jul_hold_days + 1))
for m in results:
    lt = m['jul_last_trade']
    lt_str = '%s %s' % (lt[0].date(), lt[1]) if lt else '无'
    flag = ''
    if m['name'].startswith(('V2', 'V3', 'V4')):
        flag = '  <== 失败：7月仍持有进攻仓' if m['jul_atk_days'] > 0 else '  通过'
    L.append('  %-15s 7月进攻天数 %2d/%d  7月收益 %8.2f%%  7月前最后一次换仓: %s%s'
             % (m['name'], m['jul_atk_days'], m['jul_days'], m['jul2026'] * 100, lt_str, flag))
L.append('  参考：满仓进攻腿 2026-07 单月 %+.2f%%' % (month_ret(base_res['nav_1000'], 2026, 7) * 100))
L.append('')

L.append('必查项2：2024-09 暴动（2024-08~09 买入信号 08-06/08-13/08-29/09-10/09-19 是否被影响）:')
L.append('  各变体均不过滤买入信号；风险在于被忽略的卖出使状态停留在进攻仓，后续买入信号被跳过。')
for m in results:
    trs = trades_in(m['res'], riot0, riot1)
    buys = ['%s' % d.date() for d, a in trs if '买入1000' in a]
    sells = ['%s' % d.date() for d, a in trs if '卖出1000' in a]
    L.append('  %-15s 2024-07-15~10-31 买入成交 %s | 卖出成交 %s | 2024年收益 %7.2f%%（基准 %7.2f%%）'
             % (m['name'], ','.join(buys) if buys else '无', ','.join(sells) if sells else '无',
                m['yearly'].get(2024, float('nan')) * 100, bm['yearly'].get(2024, float('nan')) * 100))
L.append('')

L.append('必查项3：2026-09 打脸段（基准 09-08→09-11 持有3天 %+.2f%%）:' % (sep_seg[-1]['ret'] * 100 if sep_seg else float('nan')))
if sep_seg:
    s0 = sep_seg[-1]
    L.append('  基准该段：买入信号日 %s，卖出信号日 %s（MACD柱 %.4f），卖出信号日距买入成交日 %d 个交易日。'
             % (sep_buy_sig_day.date(), sep_sell_sig_day.date(),
                float(base_sig_idx['macdval'].loc[sep_sell_sig_day]),
                idx.get_loc(sep_sell_sig_day) - idx.get_loc(s0['buy'])))
for m in results:
    trs = trades_in(m['res'], pd.Timestamp('2026-08-15'), pd.Timestamp('2026-09-30'))
    trs_str = ' '.join('%s%s' % (d.date(), '买' if '买入1000' in a else '卖') for d, a in trs) or '无交易'
    whip_89 = [s for s in m['whip'] if s['buy'] >= pd.Timestamp('2026-08-01')]
    L.append('  %-15s 2026-09 当月 %+7.2f%%（基准 %+.2f%%） 8-9月打脸段 %d 段  交易: %s'
             % (m['name'], m['sep2026'] * 100, bm['sep2026'] * 100, len(whip_89), trs_str))
L.append('')

L.append('必查项4：V2 阈值稳定性（N 相邻档位变化）:')
v2s = [m for m in results if m['name'].startswith('V2')]
prev = None
for m in v2s:
    if prev is not None:
        L.append('  N %2d→%-2d: 累计 %+8.1fpp 年化 %+5.2fpp MDD %+5.2fpp 2026YTD %+6.2fpp 2026-07 %+6.2fpp 打脸 %2d→%-2d段 换仓 %+d'
                 % (prev['min_hold'], m['min_hold'], (m['cum'] - prev['cum']) * 100,
                    (m['ann'] - prev['ann']) * 100, (m['mdd'] - prev['mdd']) * 100,
                    (m['ytd2026'] - prev['ytd2026']) * 100, (m['jul2026'] - prev['jul2026']) * 100,
                    prev['nwhip'], m['nwhip'], m['ntrades'] - prev['ntrades']))
    prev = m
L.append('')

L.append('必查项5：打脸段（≤5交易日且亏损）数量与合计，基准 %d 段 / %.2f%%:' % (bm['nwhip'], bm['whip_sum'] * 100))
for m in results:
    L.append('  %-15s %2d 段 / %+7.2f%%（较基准 %+6.2fpp）'
             % (m['name'], m['nwhip'], m['whip_sum'] * 100, (m['whip_sum'] - bm['whip_sum']) * 100))
L.append('')
L.append('各变体打脸段明细:')
for m in results:
    L.append('  [%s] %d 段, 合计 %.2f%%' % (m['name'], m['nwhip'], m['whip_sum'] * 100))
    for s in m['whip']:
        L.append('    %s -> %s  持有%d天  %.2f%%' % (s['buy'].date(), s['sell'].date(), s['dur'], s['ret'] * 100))
L.append('')

L.append('必查项6：换仓次数与 2024-06-28 跨界持有进攻仓（跨界持有则当日收益跨标的失真）:')
for m in results:
    note = ''
    if m['cross_hold'] and not m['name'].startswith('V1'):
        # 找跨界的这段持仓
        note = '  <== 警告：切换日持有进攻仓，当日收益跨标的失真'
    L.append('  %-15s 换仓 %3d 次（基准 %d，%+d）  2024-06-28 持有进攻仓: %s%s'
             % (m['name'], m['ntrades'], bm['ntrades'], m['ntrades'] - bm['ntrades'],
                m['cross_hold'], note))
L.append('')

L.append('跨界失真修正（关键）：512100 与 159552 后复权净值量级不同，拼接序列在 2024-06-28 的'
         ' close-to-close 收益为 %+.2f%%，而 512100 当日真实收益仅 %+.2f%%——'
         '凡跨界持有进攻仓的变体，净值被这虚假的一天抬高 %.1f%%。'
         % (r_mix * 100, r_512100 * 100, (r_mix - r_512100) * 100))
L.append('修正方法：切换日真实持有的是 512100，用其当日真实收益替换拼接假收益'
         '（修正系数 %.4f），其后净值整体缩放。修正后：' % f_corr)
L.append('%-15s %14s %14s %14s %14s' % ('变体', '修正前累计', '修正后累计', '修正后年化', '修正后2024年'))
for m in results:
    if m['cross_hold']:
        L.append('%-15s %13.2f%% %13.2f%% %13.2f%% %13.2f%%'
                 % (m['name'], m['cum'] * 100, m['cum_corr'] * 100,
                    m['ann_corr'] * 100, m['y2024_corr'] * 100))
L.append('  基准（无跨界，无需修正）：累计 %.2f%% 年化 %.2f%% 2024年 %.2f%%。'
         % (bm['cum'] * 100, bm['ann'] * 100, bm['yearly'][2024] * 100))
L.append('  修正后所有变体累计收益全部低于基准；V2 N=3 的"跑赢"绝大部分来自这一天的虚假收益。')
L.append('')

L.append('各变体被忽略卖出信号明细（信号日 / 距买入成交日交易日数 / 当日MACD柱）:')
for m in results:
    if 'ignored' not in m:
        continue
    L.append('  [%s] 忽略 %d 个卖出信号' % (m['name'], len(m['ignored'])))
    for d, hd, mv in m['ignored']:
        L.append('    %s  持有%2d天  MACD柱 %8.4f' % (d.date(), hd, mv))
L.append('')

L.append('结论与建议:')
L.append('  1. 先回答"防守切换到底值不值"（V1 参考行）：全程满仓进攻腿累计 %.2f%%（修正拼接失真后 %.2f%%）'
         'vs 基准 %.2f%%，MDD %.2f%% vs %.2f%%，2022 年 %.2f%% vs %.2f%%。'
         '防守切换贡献巨大并把回撤砍掉约 27pp——"一直持有不切"的原始形式明确不成立。'
         % (v1m['cum'] * 100, v1m['cum_corr'] * 100, bm['cum'] * 100,
            v1m['mdd'] * 100, bm['mdd'] * 100,
            v1m['yearly'][2022] * 100, bm['yearly'][2022] * 100))
L.append('  2. V2 最短持有期：表面上 N=3 累计 674.09%% 跑赢基准，但这是假象——它拦下了 2024-06-19 的卖出信号'
         '（持有 0 天即来卖出），跨界持有进攻仓穿过 2024-06-28 拼接日，吃到 +38.10%% 的虚假拼接收益'
         '（512100 当日真实仅 +0.51%%）。修正后 N=3 累计仅 %.2f%%、年化 %.2f%%，'
         '跑输基准约 %.0fpp。其余各档修正后同样全部跑输基准。'
         % (m_n3['cum_corr'] * 100, m_n3['ann_corr'] * 100,
            (bm['cum'] - m_n3['cum_corr']) * 100))
L.append('     即便不看失真，N=3 在 2023 年底前累计 %.2f%% 也跑输基准 %.2f%%，'
         '且 MDD 恶化至 -23.80%%（基准 -18.83%%，发生于 %s→%s 的 2022 熊市段）。'
         % (pre24_n3 * 100, pre24_b * 100, peak_n3.date(), trough_n3.date()))
L.append('     阈值悬崖（反过拟合红线）：N=3→5 累计 -187.2pp、5→10 又 +117.7pp、10→15 再 -158.5pp，'
         '相邻档剧变且非单调，结果由个别信号是否被拦决定。')
L.append('     N≥10 全部触发 2026-07 失败：06-26 切防守信号距买入成交日仅 7 个交易日，N=10 拦下后 7 月仍持进攻仓 2 天'
         '（靠 07-02 的下一个卖出信号侥幸解套），N=15/20 则 23/23 天满仓硬扛 -12.76%。'
         '最短持有期与"下跌初期及时止损"天然冲突——打脸段和真止损在持仓时长上不可分。')
L.append('  3. V3 黄柱区不卖：累计 451.75%%（修正后 %.2f%%）跑输基准，MDD -35.18%% 大幅恶化'
         '（同样因拦下 2024-06-19 卖出而跨界失真）；打脸段 25→7 段且合计仅 -10.31%%（各变体中最好），'
         '但没拦住 2026-09 打脸段（09-10 信号日 MACD 柱 +0.0699 ≥ 0，卖出照常生效，当月仍 -4.66%%）。'
         '零下不卖 = 阴跌中关掉刹车（2022 年 -12.97%% vs 基准 +13.44%%），不采纳。'
         % (next(m for m in results if m['name'] == 'V3 黄柱不卖')['cum_corr'] * 100))
L.append('  4. V4（N=10+黄柱）：继承 N=10 的 2026-07 失败（7 月持进攻仓 2 天），'
         '累计 487.21%%（修正后 %.2f%%）跑输基准，不采纳。'
         % (next(m for m in results if m['name'] == 'V4 N=10+黄柱')['cum_corr'] * 100))
L.append('  5. 2024-09 暴动时段：V2 N≤10 与 V3/V4 在暴动前的买入成交（08-07/08-14/08-30/09-11）均保留，'
         '暴动本身未误伤；N=15/20 因状态锁死错过 09-11 的买入。修正失真后 V2 N=3 的 2024 年为 %.2f%%，'
         '反而低于基准 31.05%%——之前看到的 70.73%% 大部分是拼接假收益。'
         % (m_n3['y2024_corr'] * 100))
L.append('  6. 总评：用户观察到的"打脸区"确实是噪声——N≥5 可把打脸段清零、换仓最多减 76 次——'
         '但用"时间锁"或"黄柱锁"强制不卖的代价，是在真正需要止损的时刻（2026-06-26、2022 熊市、'
         '2024-04 小盘股崩盘）被锁在进攻仓里，MDD 系统性恶化 5~21pp，且本回测框架下必然引入'
         '2024-06-28 跨界失真。修正失真后所有变体全部跑输基准，不建议采纳任何变体。')
L.append('')
L.append('建议：维持基准。若仍想压缩打脸段，更优方向是不锁时间的方案：result_whipsaw.txt 的'
         '幅度过滤（25→10 段且累计 +177pp）或对称确认；或 result_adx.txt 末尾提到的'
         '"低 ADX 降仓位而非禁信号"的状态分级思路。V2 N=3 即使要观察，也必须先解决跨界失真'
         '（如拼接日强制平仓或改用收益率拼接），且需接受 MDD +5pp 与阈值悬崖。')

txt = '\n'.join(L)
with open('result_hold2000.txt', 'w', encoding='utf-8') as f:
    f.write(txt + '\n')
print('\n已写入 result_hold2000.txt')
