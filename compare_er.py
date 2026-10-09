#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ER 效率系数（Kaufman Efficiency Ratio）震荡过滤实验
- 基准：880003 平均股价 DIF 拐点信号（fast=17, slow=34, sig_n=9, sell_anywhere=True）
- 变体：信号日 ER(N) < th 时过滤信号（直接丢弃，不补发）
  A 双向过滤 N∈{10,20}, th∈{0.15,0.20,0.25,0.30,0.35}
  B 仅过滤卖出 N=20, th∈{0.20,0.25,0.30}
  C 仅过滤买入 N=20, th∈{0.20,0.25,0.30}
口径见 _opt_baseline_brief.md
"""
import sys
import io
import pandas as pd
import numpy as np

if sys.platform == 'win32' and sys.stdout is not None and not getattr(sys.stdout, '_utf8_wrapped', False):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stdout._utf8_wrapped = True

from backtest import run_backtest, calc_signals, max_drawdown, annualized

SWITCH = pd.Timestamp('2024-06-28')
FEE = 0.0001


def load(path):
    df = pd.read_csv(path, parse_dates=['date']).set_index('date').sort_index()
    df.index = df.index.normalize()
    return df


def er_ratio(close, n):
    num = (close - close.shift(n)).abs()
    den = close.diff().abs().rolling(n).sum()
    return num / den.replace(0, np.nan)


def segments(res):
    """从 hold1000 提取持仓段: [(start,end,days,ret)]，ret 为该段策略收益(近似用 nav 比值)"""
    hold = res['hold1000']
    nav = res['nav_strat']
    segs = []
    in_seg = False
    for i, (d, h) in enumerate(hold.items()):
        if h and not in_seg:
            in_seg = True
            s_i = i
        elif not h and in_seg:
            in_seg = False
            # 段收益：段末 nav / 段开始前一交易日 nav
            base = nav.iloc[s_i - 1] if s_i > 0 else 1.0
            segs.append((hold.index[s_i], d, i - s_i, nav.iloc[i] / base - 1))
    if in_seg:
        i = len(hold) - 1
        base = nav.iloc[s_i - 1] if s_i > 0 else 1.0
        segs.append((hold.index[s_i], hold.index[i], i - s_i + 1, nav.iloc[i] / base - 1))
    return segs


def metrics(res, label):
    nav = res['nav_strat']
    m = {}
    m['label'] = label
    m['total'] = nav.iloc[-1] - 1
    m['ann'] = annualized(nav)
    m['mdd'] = max_drawdown(nav)
    m['trades'] = len(res['trades'])
    # 2026 YTD
    nav26 = nav.loc['2026-01-01':]
    m['ytd26'] = nav26.iloc[-1] / nav.loc[:'2025-12-31'].iloc[-1] - 1
    # 2026-09 当月
    n_sep = nav.loc['2026-09-01':]
    n_aug = nav.loc[:'2026-08-31']
    m['sep26'] = n_sep.iloc[-1] / n_aug.iloc[-1] - 1
    # 分年度
    yearly = nav.resample('YE').last().pct_change()
    yearly.iloc[0] = nav.resample('YE').last().iloc[0] / nav.iloc[0] - 1
    m['yearly'] = {dt.year: v for dt, v in yearly.items()}
    # 打脸段: ≤5交易日且亏损
    segs = segments(res)
    bad = [s for s in segs if s[2] <= 5 and s[3] < 0]
    m['segs'] = segs
    m['bad_n'] = len(bad)
    m['bad_sum'] = sum(s[3] for s in bad)
    m['bad'] = bad
    # 切换日跨界持有检查
    if res['hold1000'].loc[SWITCH:SWITCH].any() if SWITCH in res['hold1000'].index else False:
        m['cross'] = True
    else:
        # SWITCH 当天若持有进攻仓则跨界
        m['cross'] = bool(res['hold1000'].get(SWITCH, False))
    return m


def fmt_pct(x):
    return '%.2f%%' % (x * 100)


def main():
    avg = load('data/avg_880003.csv')
    etf1000 = load('data/etf_512100_hfq.csv')
    etf2000e = load('data/etf_159552_hfq.csv')
    etfdiv = load('data/etf_512890_hfq.csv')
    etf_atk = pd.concat([etf1000[etf1000.index < SWITCH], etf2000e[etf2000e.index >= SWITCH]])

    close = avg['close']
    er10 = er_ratio(close, 10)
    er20 = er_ratio(close, 20)

    # ---- 基准 ----
    base_res = run_backtest(avg, etf_atk, etfdiv, fee=FEE, sell_anywhere=True,
                            fast=17, slow=34, sig_n=9, verbose=False)
    base_sig = calc_signals(close, sell_anywhere=True, fast=17, slow=34, sig_n=9)
    base = metrics(base_res, '基准')

    def run_variant(name, n, th, mode):
        """mode: 'both'/'sell'/'buy'；信号日 ER<th 丢弃，不补发"""
        er = er10 if n == 10 else er20
        sig = base_sig.copy()
        low = (er < th).reindex(sig.index).fillna(False)
        if mode in ('both', 'sell'):
            sig.loc[low, 'sell_sig'] = False
        if mode in ('both', 'buy'):
            sig.loc[low, 'buy_sig'] = False
        res = run_backtest(avg, etf_atk, etfdiv, fee=FEE, sell_anywhere=True,
                           signals=sig, verbose=False)
        m = metrics(res, name)
        m['n'] = n
        m['th'] = th
        m['mode'] = mode
        m['sig'] = sig
        m['res'] = res
        return m

    variants = []
    for n in (10, 20):
        for th in (0.15, 0.20, 0.25, 0.30, 0.35):
            variants.append(run_variant('A-双向 N=%d th=%.2f' % (n, th), n, th, 'both'))
    for th in (0.20, 0.25, 0.30):
        variants.append(run_variant('B-仅卖 N=20 th=%.2f' % th, 20, th, 'sell'))
    for th in (0.20, 0.25, 0.30):
        variants.append(run_variant('C-仅买 N=20 th=%.2f' % th, 20, th, 'buy'))

    # ============ 必查项 ============
    checks = []

    # 1) 2026-09 打脸段
    sep26_start, sep26_end = pd.Timestamp('2026-09-01'), pd.Timestamp('2026-09-30')
    base_sep_bad = [s for s in base['bad']
                    if s[0] >= sep26_start - pd.Timedelta(days=10) and s[1] >= sep26_start and s[0] <= sep26_end]
    checks.append('【2026-09 打脸段】基准当月 %s，打脸段:' % fmt_pct(base['sep26']))
    for s in base_sep_bad:
        checks.append('  %s ~ %s  持仓%d天  段收益%s' % (s[0].date(), s[1].date(), s[2], fmt_pct(s[3])))

    # 2) 2026-07 切防守是否仍及时（找 2026-07 的卖出信号及其 ER 值）
    sell_days = base_sig.index[base_sig['sell_sig'] & (base_sig.index >= '2026-06-15') & (base_sig.index <= '2026-08-15')]
    checks.append('')
    checks.append('【2026-07 切防守检查】基准卖出信号日及 ER 值:')
    for d in sell_days:
        checks.append('  %s  ER10=%.3f  ER20=%.3f' % (d.date(), er10.get(d, np.nan), er20.get(d, np.nan)))
    checks.append('  → 各变体该段是否仍执行卖出:')
    for v in variants:
        kept = []
        for d in sell_days:
            kept.append('%s:%s' % (d.strftime('%m-%d'), '保留' if v['sig'].loc[d, 'sell_sig'] else '被过滤'))
        checks.append('  %-22s %s' % (v['label'], '  '.join(kept)))

    # 3) 震荡区占比 & 打脸段落点
    idx_bt = base_res['idx']
    low20 = (er20 < 0.20).reindex(idx_bt).fillna(False)
    checks.append('')
    checks.append('【震荡区统计】ER(20)<0.20 的交易日占比: %.1f%% (%d/%d)'
                  % (low20.mean() * 100, int(low20.sum()), len(idx_bt)))
    checks.append('基准打脸段共 %d 个，其中信号买入日落在震荡区(ER20<0.20)的:' % base['bad_n'])
    in_low = 0
    for s in base['bad']:
        # 买入信号日 = 持仓段开始前一交易日
        pos = idx_bt.get_loc(s[0])
        sig_day = idx_bt[pos - 1] if pos > 0 else s[0]
        er_v = er20.get(sig_day, np.nan)
        flag = (er_v < 0.20) if not np.isnan(er_v) else False
        in_low += flag
        checks.append('  %s~%s %d天 %s  信号日%s ER20=%.3f %s'
                      % (s[0].strftime('%y-%m-%d'), s[1].strftime('%y-%m-%d'), s[2], fmt_pct(s[3]),
                         sig_day.strftime('%y-%m-%d'), er_v, '震荡区' if flag else ''))
    checks.append('落在震荡区的打脸段: %d/%d (%.0f%%)' % (in_low, base['bad_n'], in_low / max(base['bad_n'], 1) * 100))

    # 4) 2022 熊市 & 2024-09 暴动
    def period_ret(nav, a, b):
        s = nav.loc[a:b]
        return s.iloc[-1] / nav.loc[:a].iloc[:-1].iloc[-1] - 1 if len(nav.loc[:a]) > 1 else s.iloc[-1] / s.iloc[0] - 1

    checks.append('')
    checks.append('【2022 熊市 (2022-01-01~2022-12-31) 与 2024-09 暴动 (2024-09-13~2024-10-08)】')
    checks.append('%-22s %12s %12s' % ('变体', '2022全年', '2024-09暴动'))
    row = '%-22s %12s %12s' % ('基准', fmt_pct(base['yearly'].get(2022, np.nan)),
                               fmt_pct(period_ret(base_res['nav_strat'], '2024-09-13', '2024-10-08')))
    checks.append(row)
    for v in variants:
        checks.append('%-22s %12s %12s' % (v['label'], fmt_pct(v['yearly'].get(2022, np.nan)),
                                           fmt_pct(period_ret(v['res']['nav_strat'], '2024-09-13', '2024-10-08'))))
    # 2024-09 暴动时基准买入信号是否被过滤
    buy_2409 = base_sig.index[base_sig['buy_sig'] & (base_sig.index >= '2024-09-01') & (base_sig.index <= '2024-10-15')]
    checks.append('2024-09~10 暴动区买入信号日: %s' % ', '.join(str(d.date()) for d in buy_2409))
    for d in buy_2409:
        checks.append('  %s ER10=%.3f ER20=%.3f' % (d.date(), er10.get(d, np.nan), er20.get(d, np.nan)))

    # ============ 汇总表 ============
    lines = []
    lines.append('=' * 100)
    lines.append('ER 效率系数震荡过滤实验结果  (生成于 2026-09-24, 回测区间 %s ~ %s)'
                 % (idx_bt[0].date(), idx_bt[-1].date()))
    lines.append('基准: 880003 DIF拐点 (17/34/9, sell_anywhere), 进攻腿 512100→159552(2024-06-28切换), 防守 512890, fee=万1双边')
    lines.append('信号作废口径: 信号日 ER(N)<th 时信号直接丢弃, 不延后补发')
    lines.append('=' * 100)
    hdr = '%-24s %9s %9s %9s %6s %9s %9s %7s %9s %5s' % (
        '变体', '累计', '年化', '最大回撤', '换仓', '2026YTD', '2026-09', '打脸段', '段合计', '跨界')
    lines.append(hdr)
    lines.append('-' * 100)

    def mrow(m):
        return '%-24s %9s %9s %9s %6d %9s %9s %7d %9s %5s' % (
            m['label'], fmt_pct(m['total']), fmt_pct(m['ann']), fmt_pct(m['mdd']),
            m['trades'], fmt_pct(m['ytd26']), fmt_pct(m['sep26']),
            m['bad_n'], fmt_pct(m['bad_sum']), '是' if m['cross'] else '否')

    lines.append(mrow(base))
    for v in variants:
        lines.append(mrow(v))

    lines.append('')
    lines.append('分年度收益表 (策略年化口径=当年收益率):')
    years = sorted(base['yearly'].keys())
    lines.append('%-24s' % '变体' + ''.join('%9d' % y for y in years))
    lines.append('%-24s' % '基准' + ''.join('%9s' % fmt_pct(base['yearly'][y]) for y in years))
    for v in variants:
        lines.append('%-24s' % v['label'] + ''.join('%9s' % fmt_pct(v['yearly'].get(y, np.nan)) for y in years))

    lines.append('')
    lines.append('基准打脸段明细 (≤5交易日且亏损, 共%d段, 合计%s):' % (base['bad_n'], fmt_pct(base['bad_sum'])))
    for s in base['bad']:
        lines.append('  %s ~ %s  %d天  %s' % (s[0].date(), s[1].date(), s[2], fmt_pct(s[3])))

    lines.append('')
    lines.append('=' * 100)
    lines.append('必查项')
    lines.append('=' * 100)
    lines.extend(checks)

    # 2026-09 改善量
    lines.append('')
    lines.append('2026-09 当月改善量 (变体 - 基准 %s):' % fmt_pct(base['sep26']))
    for v in variants:
        lines.append('  %-24s %s (Δ%+.2fpp)' % (v['label'], fmt_pct(v['sep26']), (v['sep26'] - base['sep26']) * 100))

    out = '\n'.join(lines)
    print(out)
    with open('result_er.txt', 'w', encoding='utf-8') as f:
        f.write(out + '\n')
    print('\n已写入 result_er.txt')


if __name__ == '__main__':
    main()
