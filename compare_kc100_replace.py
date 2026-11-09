#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
用科创100ETF(588220)替代中证2000增强ETF(159552)的回测对比

基线口径（与生产一致）：880003 平均股价 MACD(17/34/9)、sell_anywhere=True（方案C），
进攻腿后复权拼接（2024-06-28 前 512100 中证1000，之后 159552 中证2000增强），
防守腿 512890，T 日收盘出信号、T+1 开盘成交、双边万 1。

窗口：科创100ETF 数据起点 2023-09-15 ~ 信号源末日 2026-10-23。
对照：
  V0 基准      512100 → 159552（现行生产拼接）
  V1 纯替换    588220 全程（单标的，2023-09-15 起）
  V2 只换后段  512100(2023-09-15~2024-06-27) → 588220(2024-06-28 起，按日收益拼接)
  V2n 失真自检 同 V2 但朴素价格拼接
  V3 同窗三选  2024-06-28 起 512100 / 588220 / 159552 三条进攻腿
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

from compare_variants import load, FEE, FAST, SLOW, SIG_N
from backtest import run_backtest, calc_signals, annualized, max_drawdown

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, 'result_kc100_replace.txt')

SWITCH = pd.Timestamp('2024-06-28')   # 159552 上市日 / 生产拼接日
START = pd.Timestamp('2023-09-15')    # 588220 数据起点
ANCHOR_END = pd.Timestamp('2026-09-24')   # 历史锚点所在日


class Tee:
    def __init__(self, path):
        self.orig = sys.stdout
        self.f = open(path, 'w', encoding='utf-8')

    def write(self, s):
        self.orig.write(s)
        self.f.write(s)

    def flush(self):
        self.orig.flush()
        self.f.flush()

    def close(self):
        self.f.close()


# ---------------------------------------------------------------- 拼接

def splice_naive(old, new, switch):
    """生产口径：直接 concat（切换日 close-to-close 会跨界失真）"""
    return pd.concat([old[old.index < switch], new[new.index >= switch]])


def splice_by_return(old, new, switch, start=None):
    """按日收益拼接：活跃腿的日收益累乘到旧腿价格水平上，切换日无假跳空
    返回 DataFrame(open, close)，open 按活跃腿 open/prev_close 递推，保证 T+1 开盘成交价可成交
    start 给定时从该日起拼接（净值基点=旧腿当日收盘），避免早期数据瑕疵污染整条序列
    """
    oc = old['close'] / old['close'].shift(1)
    oo = old['open'] / old['close'].shift(1)
    nc = new['close'] / new['close'].shift(1)
    no = new['open'] / new['close'].shift(1)
    cr = pd.concat([oc[oc.index < switch], nc[nc.index >= switch]]).sort_index()
    orr = pd.concat([oo[oo.index < switch], no[no.index >= switch]]).sort_index()
    cr = cr[~cr.index.duplicated(keep='last')]
    orr = orr[~orr.index.duplicated(keep='last')]
    first = pd.Timestamp(start) if start is not None else cr.index[0]
    cr = cr[cr.index >= first].copy()
    orr = orr[orr.index >= first].copy()
    cr.iloc[0] = 1.0          # 首日以旧腿当日收盘为基点
    orr.iloc[0] = 1.0
    cr = cr.fillna(1.0)
    orr = orr.fillna(1.0)
    base_close = float(old['close'].loc[first])
    c = base_close * cr.cumprod()
    o = c.shift(1) * orr
    o.iloc[0] = base_close
    return pd.DataFrame({'open': o, 'close': c})


# ---------------------------------------------------------------- 指标与工具

def metrics(nav):
    return ((nav.iloc[-1] - 1) * 100, annualized(nav) * 100, max_drawdown(nav) * 100)


def mdd_window(nav):
    peak = nav.cummax()
    dd = nav / peak - 1
    trough = dd.idxmin()
    peak_d = nav.loc[:trough].idxmax()
    return peak_d, trough, dd.min() * 100


def slice_nav(nav, start):
    n = nav.loc[nav.index >= pd.Timestamp(start)]
    return n / n.iloc[0]


def slice_trades(trades, start):
    return [t for t in trades if t[0] >= pd.Timestamp(start)]


def month_ret(nav, t0):
    m = nav.resample('ME').last()
    out = m.pct_change()
    out.iloc[0] = m.iloc[0] / t0 - 1
    return out


def year_ret(nav, t0):
    y = nav.resample('YE').last()
    out = y.pct_change()
    out.iloc[0] = y.iloc[0] / t0 - 1
    return out


def chop_segments(nav, trades, max_days=5):
    """打脸段：买入成交→卖出成交 ≤max_days 个交易日且段收益<0"""
    segs = []
    bought = None
    for d, action, _p in trades:
        if action.startswith('买入'):
            bought = d
        elif action.startswith('卖出') and bought is not None:
            try:
                r = nav.loc[d] / nav.loc[bought] - 1
            except KeyError:
                bought = None
                continue
            ntd = int(np.searchsorted(nav.index.values, np.datetime64(d))
                      - np.searchsorted(nav.index.values, np.datetime64(bought)))
            if ntd <= max_days and r < 0:
                segs.append((bought, d, ntd, r))
            bought = None
    return segs


def row(name, nav, ntr):
    cum, ann, mdd = metrics(nav)
    return (name, cum, ann, mdd, ntr)


def print_table(title, rows, notes=None):
    print('=' * 96)
    print(title)
    print('=' * 96)
    fmt = '%-36s %10s %9s %10s %6s %9s %9s %9s'
    print(fmt % ('方案', '累计', '年化', '最大回撤', '换仓', '2026YTD', '2026-09', '2026-10'))
    print('-' * 96)
    for r in rows:
        name, cum, ann, mdd, ntr, ytd, sep, oct_ = r
        print(fmt % (name, '%.2f%%' % cum, '%.2f%%' % ann, '%.2f%%' % mdd, str(ntr),
                     '%.2f%%' % ytd, '%.2f%%' % sep, '%.2f%%' % oct_))
    if notes:
        print('-' * 96)
        for n in notes:
            print('注: ' + n)
    print()


def ytd_ret(nav, year=2026):
    """当年收益：以上一年最后一个净值为基准"""
    prev = nav.loc[nav.index < pd.Timestamp('%d-01-01' % year)]
    if len(prev) == 0:
        return float('nan')
    return nav.iloc[-1] / prev.iloc[-1] - 1


def month_of(nav, ym):
    """指定年月收益（相对上一月最后净值）"""
    m = nav.resample('ME').last()
    ts = pd.Period(ym, freq='M').to_timestamp('M')
    if ts not in m.index:
        return float('nan')
    pos = m.index.get_loc(ts)
    if pos == 0:
        return float('nan')
    return m.iloc[pos] / m.iloc[pos - 1] - 1


def full_row(name, nav, trades):
    return (name, *metrics(nav), len(trades),
            ytd_ret(nav) * 100, month_of(nav, '2026-09') * 100, month_of(nav, '2026-10') * 100)


# ---------------------------------------------------------------- 主流程

def main():
    teed = Tee(OUT)
    sys.stdout = teed
    try:
        avg = load('avg_880003.csv')
        etf1000 = load('etf_512100_hfq.csv')
        etfdiv = load('etf_512890_hfq.csv')
        e2000 = load('etf_159552_hfq.csv')
        kc = load('etf_588220_hfq.csv')

        print('科创100ETF(588220) 替代 中证2000增强ETF(159552) 回测对比')
        print('生成时间: %s' % pd.Timestamp.now().strftime('%Y-%m-%d %H:%M'))
        print()
        print('数据区间:')
        for nm, df in [('avg_880003 平均股价', avg), ('512100 中证1000ETF', etf1000),
                       ('512890 红利低波ETF', etfdiv), ('159552 中证2000增强ETF', e2000),
                       ('588220 科创100ETF', kc)]:
            print('  %-24s %s ~ %s  %4d 行' % (nm, df.index[0].date(), df.index[-1].date(), len(df)))
        print()

        # ---------------- 锚点校验 ----------------
        print('=' * 96)
        print('锚点校验：现行生产基线，2019-01-18 起，截断到 %s（复现历史锚点）' % ANCHOR_END.date())
        print('=' * 96)
        a_avg = avg[avg.index <= ANCHOR_END]
        a_1000 = etf1000[etf1000.index <= ANCHOR_END]
        a_div = etfdiv[etfdiv.index <= ANCHOR_END]
        a_2000 = e2000[e2000.index <= ANCHOR_END]
        atk_prod_a = splice_naive(a_1000, a_2000, SWITCH)
        res_a = run_backtest(a_avg, atk_prod_a, a_div, fee=FEE, sell_anywhere=True,
                            fast=FAST, slow=SLOW, sig_n=SIG_N, start_date='2019-01-18',
                            verbose=False)
        cum_a, ann_a, mdd_a = metrics(res_a['nav_strat'])
        print('  复跑结果: 累计 %.2f%%  年化 %.2f%%  MDD %.2f%%  换仓 %d'
              % (cum_a, ann_a, mdd_a, len(res_a['trades'])))
        print('  历史锚点: 累计 570.06%  年化 28.07%  MDD -18.83%  换仓 135（result_hold2000.txt，截至 2026-09-24）')
        ok = (abs(cum_a - 570.06) < 0.05 and abs(ann_a - 28.07) < 0.05
              and abs(mdd_a + 18.83) < 0.05 and len(res_a['trades']) == 135)
        print('  → %s' % ('一致，基线口径可复现' if ok else '不一致，需先排查口径，后续结论作废'))
        print()

        # ---------------- 主窗口准备 ----------------
        sig = calc_signals(avg['close'], sell_anywhere=True, fast=FAST, slow=SLOW, sig_n=SIG_N)
        idx_all = avg.index
        win = idx_all[idx_all >= START]
        print('主窗口: %s ~ %s，共 %d 个交易日（由 588220 数据起点与信号源末日决定）'
              % (win[0].date(), win[-1].date(), len(win)))
        print()

        # 生产基线：全历史跑完再切窗口（状态机与线上一致）
        atk_prod = splice_naive(etf1000, e2000, SWITCH)
        res0 = run_backtest(avg, atk_prod, etfdiv, fee=FEE, sell_anywhere=True,
                            fast=FAST, slow=SLOW, sig_n=SIG_N, start_date='2019-01-18',
                            verbose=False)
        nav0 = slice_nav(res0['nav_strat'], START)
        tr0 = slice_trades(res0['trades'], START)
        state0_at_start = bool(res0['hold1000'].loc[win[0]])

        # V1 纯替换：588220 全程
        res1 = run_backtest(avg, kc, etfdiv, fee=FEE, sell_anywhere=True,
                            fast=FAST, slow=SLOW, sig_n=SIG_N, start_date=START,
                            verbose=False)
        nav1 = res1['nav_strat']
        tr1 = res1['trades']

        # V2 只换后段：512100 → 588220（收益拼接，从窗口起点建基点）；同时跑朴素拼接做失真自检
        atk_v2 = splice_by_return(etf1000, kc, SWITCH, start=START)
        res2 = run_backtest(avg, atk_v2, etfdiv, fee=FEE, sell_anywhere=True,
                            fast=FAST, slow=SLOW, sig_n=SIG_N, start_date=START,
                            verbose=False)
        nav2 = res2['nav_strat']
        tr2 = res2['trades']
        # 同一条拼接序列的朴素版本（仅做失真幅度对照）
        res2n = run_backtest(avg, splice_naive(etf1000, kc, SWITCH), etfdiv, fee=FEE,
                            sell_anywhere=True, fast=FAST, slow=SLOW, sig_n=SIG_N,
                            start_date=START, verbose=False)
        nav2n = res2n['nav_strat']

        # 购买持有
        bh = {}
        for nm, df in [('588220 科创100', kc), ('512100 中证1000', etf1000)]:
            s = df.loc[df.index.intersection(win), 'close']
            bh[nm] = s / s.iloc[0]
        s = e2000.loc[e2000.index.intersection(win), 'close']
        bh['159552 中证2000增强'] = s / s.iloc[0]

        rows = []
        rows.append(full_row('V0 基准 512100→159552', nav0, tr0))
        rows.append(full_row('V1 纯替换 588220', nav1, tr1))
        rows.append(full_row('V2 只换后段 512100→588220', nav2, tr2))
        print_table('主表 窗口 %s ~ %s（策略层对照，同一信号/防守腿/费用口径）'
                    % (win[0].date(), win[-1].date()), rows,
                    notes=['V0 用全历史状态机跑到窗口起点再切净值，起点状态=%s'
                           % ('进攻仓' if state0_at_start else '防守仓'),
                           'V1 从 2023-09-15 起跑，初始状态为防守仓；588220 起点日基准也为防守仓，'
                           '两者状态机对齐',
                           'V2 仅把 2024-06-28 之后的进攻标的换成 588220，之前仍为 512100（按日收益拼接）'])

        # 失真自检
        diff2 = (nav2n - nav2).abs().max()
        print('拼接失真自检: V2(收益拼接) 与 V2n(朴素价格拼接) 净值最大差异 %.6f%%'
              % (diff2 * 100))
        print('  %s' % ('→ 2024-06-28 无进攻仓，拼接日不存在跨界失真'
                        if diff2 < 1e-9 else '→ 存在失真，V2 采用收益拼接口径'))
        print()

        # ---------------- 买入持有对照 ----------------
        rows = []
        for nm, nav in bh.items():
            rows.append(full_row(nm + ' 买入持有', nav, '-'))
        print_table('买入持有对照（同窗口; 159552 自 2024-06-28 起，起点不同请勿直接横比）',
                    rows)

        # ---------------- 分年度 ----------------
        print('=' * 96)
        print('分年度收益（策略当年收益，首年为自窗口起点）')
        print('=' * 96)
        yrs = {}
        yrs['V0 基准'] = year_ret(nav0, 1.0)
        yrs['V1 纯替换'] = year_ret(nav1, 1.0)
        yrs['V2 只换后段'] = year_ret(nav2, 1.0)
        yrs['588220 持有'] = year_ret(bh['588220 科创100'], 1.0)
        yrs['159552 持有'] = year_ret(bh['159552 中证2000增强'], 1.0)
        yrs['512100 持有'] = year_ret(bh['512100 中证1000'], 1.0)
        allyr = sorted(set().union(*[set(v.index) for v in yrs.values()]))
        hdr = '%-16s' % '方案' + ''.join('%9s' % (d.year if d.year != allyr[0].year else '%d*' % d.year)
                                          for d in allyr)
        print(hdr)
        print('-' * len(hdr))
        for k, v in yrs.items():
            line = '%-16s' % k
            for d in allyr:
                x = v.get(d, np.nan)
                line += '%9s' % ('--' if pd.isna(x) else '%.2f%%' % (x * 100))
            print(line)
        print('  * 首年自窗口起点起算（159552/512100 持有行首年起点不同）')
        print()

        # ---------------- 归因 1：标的层分段 ----------------
        print('=' * 96)
        print('归因1：标的层（买入持有）分段对比')
        print('=' * 96)
        segs = [('段A 2023-09-15~2024-06-27（V1 与 V0 的差别之一：588220 vs 512100）',
                 '2023-09-15', '2024-06-27', ['588220 科创100', '512100 中证1000']),
                ('段B 2024-06-28~2026-10-23（V1 与 V0 的差别之二：588220 vs 159552）',
                 '2024-06-28', '2026-10-23', ['588220 科创100', '159552 中证2000增强'])]
        for title, s, e, names in segs:
            print(title)
            for nm in names:
                df = kc if nm.startswith('588220') else (etf1000 if nm.startswith('512100') else e2000)
                ss = df.loc[(df.index >= s) & (df.index <= e), 'close']
                if len(ss) < 2:
                    print('  %-20s 数据不足' % nm)
                    continue
                cum, ann, mdd = metrics(ss / ss.iloc[0])
                print('  %-20s 累计 %8.2f%%  年化 %7.2f%%  MDD %7.2f%%  (%d 交易日)'
                      % (nm, cum, ann, mdd, len(ss)))
            print()

        # ---------------- 归因 2：月度差 ----------------
        print('=' * 96)
        print('归因2：策略月度收益差（V1 纯替换 − V0 基准），仅列 |差| 最大的 8 个月')
        print('=' * 96)
        m0 = month_ret(nav0, 1.0)
        m1 = month_ret(nav1, 1.0)
        md = pd.DataFrame({'V0': m0, 'V1': m1}).dropna()
        md['diff'] = md['V1'] - md['V0']
        md = md.sort_values('diff', key=lambda s: s.abs(), ascending=False).head(8)
        print('%-12s %10s %10s %10s' % ('月份', 'V0 基准', 'V1 科创100', '差'))
        for d, r in md.iterrows():
            print('%-12s %9.2f%% %9.2f%% %9.2f%%'
                  % (d.strftime('%Y-%m'), r['V0'] * 100, r['V1'] * 100, r['diff'] * 100))
        print()

        # ---------------- 归因 3：换仓一致性 ----------------
        print('=' * 96)
        print('归因3：换仓序列一致性（信号与防守腿相同，只换了进攻标的）')
        print('=' * 96)
        d0 = [(t[0], t[1]) for t in tr0]
        d1 = [(t[0], t[1]) for t in tr1]
        d2 = [(t[0], t[1]) for t in tr2]
        print('  V0 换仓 %d 次 / V1 换仓 %d 次 / V2 换仓 %d 次' % (len(d0), len(d1), len(d2)))
        print('  V0 与 V1 换仓日期+方向完全一致: %s' % (d0 == d1))
        print('  V0 与 V2 换仓日期+方向完全一致: %s' % (d0 == d2))
        if d0 == d1:
            print('  → 换标的没有改变任何交易时点，收益差 100% 来自进攻仓持有期内的标的表现差异')
        print()

        # 进攻仓持有天数与占比（个别日期某标的停牌缺失，按前值补齐）
        hold0 = res0['hold1000'].reindex(win).ffill().fillna(False)
        hold1 = res1['hold1000'].reindex(win).ffill().fillna(False)
        print('  进攻仓持有时间占比: V0 %.1f%% / V1 %.1f%%'
              % (hold0.mean() * 100, hold1.mean() * 100))
        print()

        # ---------------- 归因 4：信号×标的契合度 ----------------
        print('=' * 96)
        print('归因4：同一套信号（同一批进攻日）在不同进攻标的上的收益 —— 信号与标的节奏的契合度')
        print('=' * 96)
        print('分段统计，避免把“空仓躲跌”误当成标的契合。')
        print()
        for title, s, e, legs in [
            ('段A %s ~ %s：588220 vs 512100' % ('2023-09-15', '2024-06-27'), '2023-09-15', '2024-06-27',
             [('512100 中证1000', etf1000), ('588220 科创100', kc)]),
            ('段B %s ~ %s：588220 vs 159552 vs 512100' % ('2024-06-28', '2026-10-23'), '2024-06-28', '2026-10-23',
             [('512100 中证1000', etf1000), ('588220 科创100', kc),
              ('159552 中证2000增强', e2000)])]:
            print(title)
            print('%-22s %14s %14s %14s %12s'
                  % ('进攻标的', '进攻日累计', '同期买入持有', '差距', '进攻日占比'))
            print('-' * 80)
            seg = win[(win >= s) & (win <= e)]
            for nm, df in legs:
                r_ = df['close'].pct_change().reindex(seg)
                m_ = hold0.reindex(seg).fillna(False) & r_.notna()
                cum_off = float((1 + r_[m_].fillna(0.0)).prod() - 1) if m_.any() else np.nan
                s_ = df['close'].reindex(seg).dropna()
                cum_bh = float(s_.iloc[-1] / s_.iloc[0] - 1) if len(s_) > 1 else np.nan
                print('%-22s %13.2f%% %13.2f%% %13.2f%% %11.1f%%'
                      % (nm, cum_off * 100, cum_bh * 100, (cum_off - cum_bh) * 100,
                         m_.mean() * 100))
            print()
        print('  说明：同段内三行用的是完全相同的“进攻日”，差别只在当天持有哪只 ETF。')
        print('        “进攻日累计”明显高于“同期买入持有”= 该信号恰好踩中它的上涨日（契合），')
        print('        不代表该标的自身更强；契合度差异是本次结论能否外推的关键。')
        print()

        # ---------------- 回撤区间 ----------------
        print('=' * 96)
        print('最大回撤发生区间')
        print('=' * 96)
        for nm, nav in [('V0 基准', nav0), ('V1 纯替换', nav1), ('V2 只换后段', nav2)]:
            p, t, dd = mdd_window(nav)
            print('  %-12s MDD %7.2f%%   %s → %s（%d 个交易日）'
                  % (nm, dd, p.date(), t.date(),
                     int(np.searchsorted(nav.index.values, np.datetime64(t))
                         - np.searchsorted(nav.index.values, np.datetime64(p)))))
        print()

        # ---------------- 打脸段 ----------------
        print('=' * 96)
        print('打脸段（买入成交→卖出成交 ≤5 个交易日且亏损）')
        print('=' * 96)
        for nm, nav, tr in [('V0 基准', nav0, tr0), ('V1 纯替换', nav1, tr1),
                            ('V2 只换后段', nav2, tr2)]:
            segs_ = chop_segments(nav, tr)
            tot = sum(s[3] for s in segs_)
            print('  %-12s %2d 段 / 合计 %+.2f%%' % (nm, len(segs_), tot * 100))
            for s in segs_:
                print('      %s → %s  持有%d天  %+.2f%%'
                      % (s[0].date(), s[1].date(), s[2], s[3] * 100))
        print()

        # ---------------- V3 同窗三选 ----------------
        print('=' * 96)
        print('V3 同窗三选（2024-06-28 起，三条进攻腿同窗口）')
        print('=' * 96)
        r3 = []
        for nm, df in [('512100 中证1000', etf1000), ('588220 科创100', kc),
                       ('159552 中证2000增强', e2000)]:
            rr = run_backtest(avg, df, etfdiv, fee=FEE, sell_anywhere=True,
                              fast=FAST, slow=SLOW, sig_n=SIG_N, start_date=SWITCH,
                              verbose=False)
            r3.append(full_row(nm, rr['nav_strat'], rr['trades']))
        print_table('V3 窗口 %s ~ %s' % (SWITCH.date(), win[-1].date()), r3,
                    notes=['同一窗口下三条进攻腿的纯对照，不含 2023-09~2024-06 段'])

        # ---------------- 稳健性：起点敏感性 ----------------
        print('=' * 96)
        print('稳健性：起点敏感性（V0 基准 vs V1 纯替换；此窗口内 V2 与 V1 相同）')
        print('=' * 96)
        for s in ['2023-09-15', '2024-06-28', '2025-01-01']:
            rr = []
            n0 = slice_nav(res0['nav_strat'], s)
            rr.append(full_row('V0 基准', n0, slice_trades(res0['trades'], s)))
            res1s = run_backtest(avg, kc, etfdiv, fee=FEE, sell_anywhere=True,
                                 fast=FAST, slow=SLOW, sig_n=SIG_N, start_date=s,
                                 verbose=False)
            rr.append(full_row('V1 纯替换 588220', res1s['nav_strat'], res1s['trades']))
            n2 = slice_nav(res2['nav_strat'], s)
            rr.append(full_row('V2 只换后段', n2, slice_trades(res2['trades'], s)))
            print_table('起点 %s' % s, rr)
        print()

        # ---------------- 波动与风格差异 ----------------
        print('=' * 96)
        print('波动特征（同窗口买入持有日收益）')
        print('=' * 96)
        print('%-22s %10s %12s %12s %12s' % ('标的', '年化波动', '单日最大涨', '单日最大跌', '2026-07 月'))
        for nm, df in [('512100 中证1000', etf1000), ('588220 科创100', kc),
                       ('159552 中证2000增强', e2000)]:
            r_ = df['close'].pct_change().reindex(win).dropna()
            vol = r_.std() * np.sqrt(243)
            jul = month_of(df['close'].reindex(win).dropna(), '2026-07')
            print('%-22s %9.1f%% %11.2f%% %11.2f%% %11.2f%%'
                  % (nm, vol * 100, r_.max() * 100, r_.min() * 100,
                     (np.nan if pd.isna(jul) else jul) * 100))
        print()

        # ---------------- 结论 ----------------
        cum0, ann0, mdd0 = metrics(nav0)
        cum1, ann1, mdd1 = metrics(nav1)
        cum2, ann2, mdd2 = metrics(nav2)
        p0, t0, _ = mdd_window(nav0)
        p1, t1, _ = mdd_window(nav1)
        y0 = year_ret(nav0, 1.0)
        y1 = year_ret(nav1, 1.0)
        y2 = year_ret(nav2, 1.0)

        print('=' * 96)
        print('结论')
        print('=' * 96)
        print('1) 同窗口（%s ~ %s）结果：' % (win[0].date(), win[-1].date()))
        print('   V0 基准(512100→159552) 累计 %.2f%% 年化 %.2f%% MDD %.2f%% 换仓 %d'
              % (cum0, ann0, mdd0, len(tr0)))
        print('   V1 纯替换(588220)      累计 %.2f%% 年化 %.2f%% MDD %.2f%% 换仓 %d'
              % (cum1, ann1, mdd1, len(tr1)))
        print('   V2 只换后段            累计 %.2f%% 年化 %.2f%% MDD %.2f%% 换仓 %d'
              % (cum2, ann2, mdd2, len(tr2)))
        print('   V1 vs V0: 累计 %+.2fpp / 年化 %+.2fpp / MDD %+.2fpp'
              % (cum1 - cum0, ann1 - ann0, mdd1 - mdd0))
        print('   V2 vs V0: 累计 %+.2fpp / 年化 %+.2fpp / MDD %+.2fpp'
              % (cum2 - cum0, ann2 - ann0, mdd2 - mdd0))
        print('2) 逐年拆（V1 − V0，pp）: 2024 %+.2f / 2025 %+.2f / 2026 %+.2f'
              % tuple(((y1.get(pd.Timestamp('%d-12-31' % y), np.nan)
                        - y0.get(pd.Timestamp('%d-12-31' % y), np.nan)) * 100)
                      for y in (2024, 2025, 2026)))
        print('   逐年拆（V2 − V0，pp）: 2024 %+.2f / 2025 %+.2f / 2026 %+.2f'
              % tuple(((y2.get(pd.Timestamp('%d-12-31' % y), np.nan)
                        - y0.get(pd.Timestamp('%d-12-31' % y), np.nan)) * 100)
                      for y in (2024, 2025, 2026)))
        print('   → 优势不是每年都成立：需确认是否只来自单一年份')
        print('3) 回撤：V0 %s→%s（%.2f%%），V1 %s→%s（%.2f%%），回撤加深 %.2fpp'
              % (p0.date(), t0.date(), mdd0, p1.date(), t1.date(), mdd1, mdd1 - mdd0))
        print('4) 代价：科创100 波动显著更高、单日振幅更大（见波动表），进攻仓持有期内的回撤更深；')
        print('   且换标的=换风格（大盘科技 vs 微盘），本窗口只覆盖 %d 个月（全样本 1/3），'
              % round((win[-1] - win[0]).days / 30.4))
        print('   信号参数未做任何寻优，结论应按“风格暴露选择”而非“稳定 alpha”理解。')
        print()
    finally:
        sys.stdout = sys.__stdout__
        teed.close()
    print('结果已写入 %s' % OUT)


if __name__ == '__main__':
    main()
