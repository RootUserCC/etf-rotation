#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
多信号源共振/投票实验：880003 平均股价 + 进攻腿 etf_atk + 512890 红利低波
三个序列各自用 calc_signals 计算 DIF 拐点（统一 fast=17, slow=34, sig_n=9,
sell_anywhere=True），合成外部信号经 run_backtest 的 signals= 注入，
执行/费用口径与基准完全一致（T 日收盘信号，T+1 开盘成交，双边万1）。

变体：
  A 多数表决：≥2 个序列当日出买点 → buy_sig；≥2 个出卖点 → sell_sig；
    同日买卖冲突时卖优先（清掉当日 buy_sig）。
  B 进攻腿主导：纯进攻腿信号（对照组，等价于 compare_signal_atk 的 atk(17,34)）。
  C 880003 主信号 + 进攻腿确认：880003 出买点当日，要求进攻腿 DIF 也上行
    （dif.diff()>0）才置 buy_sig；卖出沿用 880003 卖点不变。
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
FAST, SLOW, SIG_N = 17, 34, 9


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
        'n_slap': n_slap,
        'slap_ret': slap_ret * 100,
        'ytd26': period_ret(nav, '2026-01-01') * 100,
        'sep26': period_ret(nav, '2026-09-01') * 100,
        'yearly': yearly_table(nav),
        'cross': cross_switch(res),
    }


def lead_lag(base_sig, var_sig, window=20):
    """对每个基准买入信号，找 ±window 个自然日内最近的变体买入信号，返回天数差列表
    （正 = 变体信号滞后于基准，负 = 变体提前）"""
    bidx = base_sig.index[base_sig['buy_sig']]
    vidx = var_sig.index[var_sig['buy_sig']]
    diffs = []
    for d in bidx:
        near = vidx[np.abs(vidx - d) <= pd.Timedelta(days=window)]
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

    # ---- 三个序列各自的 DIF 拐点信号（统一 17/34/9, sell_anywhere=True）----
    sig_880 = calc_signals(avg['close'], sell_anywhere=True, fast=FAST, slow=SLOW, sig_n=SIG_N)
    sig_atk = calc_signals(etf_atk['close'], sell_anywhere=True, fast=FAST, slow=SLOW, sig_n=SIG_N)
    sig_div = calc_signals(etfdiv['close'], sell_anywhere=True, fast=FAST, slow=SLOW, sig_n=SIG_N)

    out('三个信号源独立信号数（买点/卖点）:')
    out('  880003 平均股价: 买 %d / 卖 %d' % (int(sig_880['buy_sig'].sum()), int(sig_880['sell_sig'].sum())))
    out('  进攻腿 etf_atk : 买 %d / 卖 %d' % (int(sig_atk['buy_sig'].sum()), int(sig_atk['sell_sig'].sum())))
    out('  512890 红利低波: 买 %d / 卖 %d' % (int(sig_div['buy_sig'].sum()), int(sig_div['sell_sig'].sum())))
    out('')

    # ---- 基准 ----
    base = run_backtest(avg, etf_atk, etfdiv, fee=FEE, sell_anywhere=True,
                        fast=FAST, slow=SLOW, sig_n=SIG_N, label='基准(880003)', verbose=False)
    sb = summarize('基准 880003', base, etf_atk)
    out('回测区间: %s ~ %s  共 %d 个交易日' % (base['idx'][0].date(), base['idx'][-1].date(), len(base['idx'])))
    out('基准锚点: 累计 %.2f%%  年化 %.2f%%  最大回撤 %.2f%%  换仓 %d 次  打脸段 %d 段/%.2f%%'
        % (sb['cum'], sb['ann'], sb['mdd'], sb['trades'], sb['n_slap'], sb['slap_ret']))
    out('')

    # ---- 变体 A：多数表决（≥2 买票 → buy；≥2 卖票 → sell；同日冲突卖优先）----
    common = sig_880.index.intersection(sig_atk.index).intersection(sig_div.index)
    b_votes = (sig_880.loc[common, 'buy_sig'].astype(int)
               + sig_atk.loc[common, 'buy_sig'].astype(int)
               + sig_div.loc[common, 'buy_sig'].astype(int))
    s_votes = (sig_880.loc[common, 'sell_sig'].astype(int)
               + sig_atk.loc[common, 'sell_sig'].astype(int)
               + sig_div.loc[common, 'sell_sig'].astype(int))
    sig_a = pd.DataFrame(index=common)
    sig_a['buy_sig'] = b_votes >= 2
    sig_a['sell_sig'] = s_votes >= 2
    conflict = sig_a['buy_sig'] & sig_a['sell_sig']
    sig_a.loc[conflict, 'buy_sig'] = False   # 同日买卖冲突：卖优先
    out('变体A 多数表决: 买信号 %d 次, 卖信号 %d 次（其中同日冲突卖优先清买 %d 次）'
        % (int(sig_a['buy_sig'].sum()), int(sig_a['sell_sig'].sum()), int(conflict.sum())))
    res_a = run_backtest(avg, etf_atk, etfdiv, fee=FEE, signals=sig_a,
                         label='A多数表决', verbose=False)

    # ---- 变体 B：进攻腿主导（纯进攻腿信号，对照）----
    sig_b = sig_atk[['buy_sig', 'sell_sig']].copy()
    res_b = run_backtest(avg, etf_atk, etfdiv, fee=FEE, signals=sig_b,
                         label='B进攻腿主导', verbose=False)
    out('变体B 进攻腿主导: 买信号 %d 次, 卖信号 %d 次'
        % (int(sig_b['buy_sig'].sum()), int(sig_b['sell_sig'].sum())))

    # ---- 变体 C：880003 主信号 + 进攻腿 DIF 上行确认 ----
    # reindex 后对缺失日期（进攻腿未上市前）按"不确认"处理；astype(bool) 防止 object dtype 的 ~ 陷阱
    atk_dif_up = (sig_atk['dif'].diff() > 0).reindex(sig_880.index).fillna(False).astype(bool)
    raw_buy = sig_880['buy_sig'].astype(bool)
    sig_c = pd.DataFrame(index=sig_880.index)
    sig_c['buy_sig'] = raw_buy & atk_dif_up
    sig_c['sell_sig'] = sig_880['sell_sig'].astype(bool)
    filtered_mask = raw_buy & ~atk_dif_up
    win = base['idx']   # 回测窗口（2019-01-18 起）
    n_filtered_win = int(filtered_mask.loc[filtered_mask.index.isin(win)].sum())
    n_raw_win = int(raw_buy.loc[raw_buy.index.isin(win)].sum())
    out('变体C 880003+进攻腿确认: 回测窗口内原始买点 %d 个, 被进攻腿DIF未上行过滤 %d 个, 保留 %d 个'
        % (n_raw_win, n_filtered_win, n_raw_win - n_filtered_win))
    out('  （全历史口径: 原始 %d / 过滤 %d / 保留 %d，早期进攻腿未上市一律不确认）'
        % (int(raw_buy.sum()), int(filtered_mask.sum()), int(sig_c['buy_sig'].sum())))
    out('  窗口内被过滤买点日期: ' + ', '.join(str(d.date()) for d in filtered_mask.index[filtered_mask] if d in set(win)))
    res_c = run_backtest(avg, etf_atk, etfdiv, fee=FEE, signals=sig_c,
                         label='C确认', verbose=False)
    out('')

    summaries = [
        sb,
        summarize('A 多数表决(≥2)', res_a, etf_atk),
        summarize('B 进攻腿主导', res_b, etf_atk),
        summarize('C 880003+进攻腿确认', res_c, etf_atk),
    ]

    # ---- 汇总表 ----
    out('=' * 100)
    out('汇总对比（执行口径: T收盘信号/T+1开盘，双边万1）')
    out('%-24s %9s %8s %9s %7s %9s %9s %8s %10s' %
        ('版本', '累计收益%', '年化%', '最大回撤%', '换仓', '2026YTD%', '2026-09%', '打脸段', '打脸合计%'))
    for m in summaries:
        out('%-24s %9.2f %8.2f %9.2f %7d %9.2f %9.2f %8d %10.2f' %
            (m['name'], m['cum'], m['ann'], m['mdd'], m['trades'],
             m['ytd26'], m['sep26'], m['n_slap'], m['slap_ret']))
    out('')
    out('切换日(2024-06-28)跨界持有进攻仓: ' +
        ', '.join('%s=%s' % (m['name'], '是(失真风险)' if m['cross'] else '否') for m in summaries))
    out('')

    # ---- 分年度收益 ----
    out('分年度收益表（%）:')
    years = sb['yearly'].index
    out('%-24s' % '版本' + ''.join('%9d' % y.year for y in years))
    for m in summaries:
        out('%-24s' % m['name'] + ''.join('%9.2f' % (m['yearly'].loc[y] * 100) for y in years))
    out('')

    # ---- 信号提前/滞后与假信号分析（相对基准买点，±20 自然日内最近匹配）----
    out('信号层面分析（相对基准 880003 的买入信号，±20 个自然日内最近匹配）:')
    out('%-24s %7s %7s %16s %9s' % ('版本', '买信号', '卖信号', '提前/滞后', '中位天数'))
    for name, sig in [('A 多数表决(≥2)', sig_a), ('B 进攻腿主导', sig_b), ('C 880003+进攻腿确认', sig_c)]:
        diffs = lead_lag(sig_880, sig)
        if diffs:
            med = float(np.median(diffs))
            ahead = sum(1 for d in diffs if d < 0)
            lag = sum(1 for d in diffs if d > 0)
            desc = '提前%d/滞后%d/同步%d' % (ahead, lag, len(diffs) - ahead - lag)
        else:
            med, desc = float('nan'), '无匹配'
        out('%-24s %7d %7d %16s %9.1f' %
            (name, int(sig['buy_sig'].sum()), int(sig['sell_sig'].sum()), desc, med))
    out('基准 880003: 买信号 %d 次 / 卖信号 %d 次'
        % (int(sig_880['buy_sig'].sum()), int(sig_880['sell_sig'].sum())))
    out('')

    # ---- 换仓明细（变体与基准信号差异日）----
    out('变体C 被过滤的基准买点明细（回测窗口内，这些买点进攻腿DIF当日未上行）:')
    for d in filtered_mask.index[filtered_mask]:
        if d not in set(win):
            continue
        out('  %s  880003 DIF=%.4f(拐升)  atk DIF=%.4f  atk ΔDIF=%.5f'
            % (d.date(), sig_880.loc[d, 'dif'], sig_atk.loc[d, 'dif'], sig_atk['dif'].diff().loc[d]))
    out('')

    conclusion = """\
==================== 结论 ====================
核心问题：多信号源共振/投票能否在"减少假信号"与"拐点滞后"之间取得比单一
880003 信号更好的 trade-off？
回答：不能。三个变体全部跑输基准，不建议采纳。

1) 变体A（多数表决 ≥2/3）：假信号确实大幅减少（打脸段 10 段/-17.55%，
   换仓 62 次，均为各版本中最低），但代价是灾难性的——年化仅 12.25%
   （基准 28.33%），累计 143% vs 基准 580%，最大回撤反而恶化到 -38.05%。
   原因：512890 红利低波波动太小、买点稀少（全历史仅 65 个 vs 880003 的
   142 个），"≥2 票"在多数时候等价于"880003 与进攻腿同日共振"，买点
   变得太稀疏，2024（+1.88% vs 31.05%）和 2025（+20.57% vs 42.18%）
   两段主升几乎全部踏空。假信号少了，但真信号丢得更多——多数表决把
   "信号稀疏化"的代价放得过大。

2) 变体B（进攻腿主导，对照组）：年化 23.14%、打脸段 33 段/-49.94%、
   换仓 144 次，全面差于基准，与 compare_signal_atk 的既往结论一致：
   高波动进攻腿自身信号毛刺多，震荡市里反复挨打。

3) 变体C（880003 主信号 + 进攻腿 DIF 上行确认）：是最不差的变体。
   窗口内 68 个基准买点被过滤 19 个（保留 49 个），无额外滞后
   （同日确认，相对基准买点中位天数差 0，同步 67/80）；打脸段
   18 段/-27.08%（基准 25 段/-38.88%），换仓 97 次（基准 135），
   回撤 -18.56% 略优于基准 -18.83%。但年化 23.90% 仍落后基准
   4.4 个百分点（累计 419% vs 580%）。被过滤的 19 个买点里既有真
   假信号（如 2026-09-07 过滤后 9 月当月 -0.33% vs 基准 -3.27%，
   反而躲亏），也有真底（2019-07、2022-03-17、2024-08 底部的多个
   买点），2025 年少赚 14 个百分点（28.11% vs 42.18%）是主要失分年。
   即：确认机制对假信号的"过滤精度"不够——进攻腿 DIF 当日是否上行，
   与 880003 买点真假的相关性不足，过滤对错各半，净效果为负。

4) 综合：三序列共振的思路方向合理（假信号确实随确认条件加严而减少），
   但三个信号源同质性太高（都是 A 股中小盘/全市场 β），共振条件筛选
   掉的真假信号比例接近，无法提供净增益。基准单一 880003 信号仍是
   最优信号源配置。所有变体在 2024-06-28 拼接切换日均未跨界持有
   进攻仓，无跨标的失真。

最终结论：不采纳任何变体，维持基准（880003 单一信号源 17/34/9）。
若未来仍想降低打脸段损耗，变体C 的框架（确认式过滤、零额外滞后）
可作为载体，但确认条件需要换成与买点真假相关性更高的变量
（如量能、宽度指标），而非进攻腿 DIF 当日方向。"""
    out(conclusion)

    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           'result_signal_vote.txt'), 'w', encoding='utf-8') as fp:
        fp.write('\n'.join(lines) + '\n')

    # 关键数字回显，供写结论用
    for m in summaries:
        print('[%s] cum=%.2f ann=%.2f mdd=%.2f trades=%d ytd26=%.2f sep26=%.2f slap=%d/%.2f cross=%s'
              % (m['name'], m['cum'], m['ann'], m['mdd'], m['trades'],
                 m['ytd26'], m['sep26'], m['n_slap'], m['slap_ret'], m['cross']))


if __name__ == '__main__':
    main()
