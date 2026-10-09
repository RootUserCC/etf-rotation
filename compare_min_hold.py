#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
最少持仓 N 天约束回测（只约束进攻腿的卖出）——基线为生产口径方案C

基线口径（与 compare_position_size.py / compare_offense_pool.py / export_json.py 一致）：
- 信号：880003 收盘价 MACD-DIF 拐点，fast=17 slow=34 sig_n=9，sell_anywhere=True
- 进攻腿：2024-06-28 前 = 512100 后复权，之后 = 159552 后复权（拼接，缺口前向填充）
- 防守腿两套口径都跑：
    A) 512890 固定（= site/data.json 对账口径，基线 +538.68%/27.30%/-18.06%/135 次）
    B) 512890/159201 近20日动量双择优（基线 +593.20%/28.66%）
- 成交口径：信号 T 日收盘产生，T+1 日开盘成交；换仓日拆隔夜段(昨收→今开)
  + 日内段(今开→今收)，扣双边 2*fee（fee=0.0001）
- 回测窗口 2019-01-18 ~ 2026-09-22，信号用全部历史预热

与已有"冷却期"变体（reduce_loss.py V2：换仓后 K 天内忽略一切新信号，对称地同时
封锁买入和卖出）不同，本文件的"最少持仓 N 天"只管入场之后：
买入进攻腿后，持仓不满 N 个交易日时出现的卖出信号——
- 变体 D（丢弃）：该卖出信号作废，之后等下一个新卖出信号才卖；
- 变体 E（顺延）：在持仓满 N 天后的第一个交易日开盘执行卖出。
  E 的顺延等待期规则（按最新信号状态处理）：
    * 等待期内再次出现卖出信号 → 维持原顺延执行日不变（顺延锚定入场日 entry_i+N，
      不会因为新卖出信号而再往后推）；
    * 等待期内出现买入信号 → 视为"先卖后买"拐点序列的最新状态为买入，顺延取消，
      继续持仓（该买入信号在基线里对应一次再入场，两边在此处重新收敛）；
    * 顺延取消后若再出现卖出信号，重新按当时已持仓天数判定（通常已 ≥N，立即执行）。
持仓天数口径：T 日收盘买入信号、T+1 开盘成交，成交日为持仓第 1 天（收盘计）。

N ∈ {2, 3, 5, 8, 10, 15, 20}，2 变体 × 7 档 N × 2 套防守口径。

输出：基线 vs 全部变体的累计/年化/最大回撤/换仓次数/进攻仓占比、分年度收益、
前后半稳健性（2022-01-01 切分）、反事实分析（被拦截的短持仓卖出逐笔：
"当时卖出"的基线路径 vs 约束后的实际路径收益差）。结果写入 result_min_hold.txt。
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
        sys.stdout.reconfigure(encoding='utf-8')
    sys.stdout._utf8_wrapped = True

from backtest import calc_signals, annualized, max_drawdown

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, 'data')

FEE = 0.0001
FAST, SLOW, SIG_N = 17, 34, 9
DEF_MOM_N = 20                          # 防守双择优动量窗口（生产口径）
START = '2019-01-18'                    # 512890 上市日
SWITCH_ATK = pd.Timestamp('2024-06-28')  # 进攻腿拼接切换日
SPLIT_DATE = pd.Timestamp('2022-01-01')  # 稳健性前后半分界
N_LIST = [2, 3, 5, 8, 10, 15, 20]

_lines = []


def out(s=''):
    print(s)
    _lines.append(s)


def load(name):
    df = pd.read_csv(os.path.join(DATA_DIR, name), parse_dates=['date'])
    df = df.set_index('date').sort_index()
    df.index = df.index.normalize()
    return df


def align(s, idx):
    """对齐到交易日历 idx；上市区间内个别缺口前向填充（当日收益为0），
    上市前/数据截止后保持 NaN（同 compare_position_size.align）"""
    r = s.reindex(idx).ffill()
    r[(idx < s.index[0]) | (idx > s.index[-1])] = np.nan
    return r


# ---------------------------------------------------------------------------
# 最少持仓 N 天引擎
# ---------------------------------------------------------------------------
def minhold_backtest(avg, etf_atk, def1, def2, start_date,
                     variant=None, N=None, label=''):
    """
    variant: None=基线 / 'D'=丢弃 / 'E'=顺延；N=最少持仓交易日数。
    def2 为 None 时防守 = 固定 def1。
    返回 dict: nav/trades/n_switch/pos_atk/idx/episodes
      episodes: 每个进攻持仓段 {entry_i, exit_i, exit_via, blocked:[...], cancel_buy_i}
        blocked 元素: {sig_i, held} 被拦截的卖出信号（信号日下标、当时已持仓天数）
        exit_via: 'signal'(正常卖出) / 'defer'(顺延执行) / None(持有至期末)
    """
    sig_full = calc_signals(avg['close'], sell_anywhere=True,
                            fast=FAST, slow=SLOW, sig_n=SIG_N)
    idx = def1.index.intersection(sig_full.index)
    start_date = pd.Timestamp(start_date)
    idx = idx[idx >= start_date]
    sig = sig_full.loc[idx]

    o_atk, c_atk = align(etf_atk['open'], idx), align(etf_atk['close'], idx)
    o_d1, c_d1 = def1.loc[idx, 'open'], def1.loc[idx, 'close']
    has_d2 = def2 is not None and len(def2) > 0
    o_d2 = align(def2['open'], idx) if has_d2 else None
    c_d2 = align(def2['close'], idx) if has_d2 else None

    mom_d1 = def1['close'] / def1['close'].shift(DEF_MOM_N) - 1
    mom_d2 = (def2['close'] / def2['close'].shift(DEF_MOM_N) - 1) if has_d2 else None

    def pick_def(day):
        m1 = mom_d1.get(day, np.nan)
        m2 = mom_d2.get(day, np.nan) if has_d2 else np.nan
        if pd.isna(m2):
            return 'd1'
        if pd.isna(m1):
            return 'd2'
        return 'd1' if m1 >= m2 else 'd2'

    def def_prices(tag):
        return (o_d1, c_d1) if tag == 'd1' else (o_d2, c_d2)

    n = len(idx)
    state_atk = False
    cur_def = pick_def(idx[0])
    pending = None                # ('enter'/'exit', def_old, def_new)
    trades = []
    exec_at = {}                  # 执行日 -> (p_old, p_new, def_old, def_new)
    def_hold = []
    pos_atk = np.zeros(n)
    entry_i = -1                  # 当前进攻段入场执行日下标
    defer_exec_i = None           # E 变体：顺延卖出执行日下标（= entry_i + N）
    episodes = []
    cur_ep = None

    for i in range(n):
        day = idx[i]
        # ---- T+1 开盘执行 ----
        # 1) E 变体顺延卖出：到达 entry_i+N 日开盘执行；防守标的用执行前一交易日
        #    收盘的最新动量选取（不使用前视数据）
        if defer_exec_i is not None and i >= defer_exec_i and state_atk:
            new_def = pick_def(idx[i - 1])
            state_atk = False
            trades.append((day, '卖出进攻(顺延)/买入防守:%s' % new_def))
            exec_at[day] = (1.0, 0.0, cur_def, new_def)
            cur_def = new_def
            defer_exec_i = None
            if cur_ep is not None:
                cur_ep['exit_i'] = i
                cur_ep['exit_via'] = 'defer'
                episodes.append(cur_ep)
                cur_ep = None
        # 2) 常规信号执行
        if pending is not None:
            kind, d_old, d_new = pending
            o_dn, _ = def_prices(d_new)
            exec_ok = (not pd.isna(o_atk.iloc[i])) and (not pd.isna(o_dn.iloc[i]))
            if exec_ok:
                if kind == 'enter' and not state_atk:
                    state_atk = True
                    trades.append((day, '买入进攻/卖出防守'))
                    exec_at[day] = (0.0, 1.0, d_old, d_new)
                    entry_i = i
                    cur_ep = {'entry_i': i, 'exit_i': None, 'exit_via': None,
                              'blocked': [], 'cancel_buy_i': None}
                elif kind == 'exit' and state_atk:
                    state_atk = False
                    trades.append((day, '卖出进攻/买入防守:%s' % d_new))
                    exec_at[day] = (1.0, 0.0, d_old, d_new)
                    cur_def = d_new
                    if cur_ep is not None:
                        cur_ep['exit_i'] = i
                        cur_ep['exit_via'] = 'signal'
                        episodes.append(cur_ep)
                        cur_ep = None
            pending = None
        pos_atk[i] = 1.0 if state_atk else 0.0
        def_hold.append(cur_def)

        # ---- 收盘决策（只用当日及之前数据） ----
        if sig['buy_sig'].iloc[i]:
            if not state_atk:
                pending = ('enter', cur_def, cur_def)
            elif defer_exec_i is not None:
                # E 顺延等待期内出现买入信号：按最新信号状态取消顺延，继续持仓
                defer_exec_i = None
                if cur_ep is not None:
                    cur_ep['cancel_buy_i'] = i
            # 持有进攻仓期间的其余买入信号忽略（与基线一致）
        elif sig['sell_sig'].iloc[i]:
            if state_atk:
                if defer_exec_i is not None:
                    # E 顺延等待期内再次出现卖出信号：维持原顺延日不变
                    cur_ep['blocked'].append({'sig_i': i, 'held': i - entry_i + 1,
                                              'kind': 'defer_wait'})
                else:
                    held = i - entry_i + 1        # 当日收盘已持仓交易日数
                    if variant is None or held >= N:
                        new_def = pick_def(day)
                        pending = ('exit', cur_def, new_def)
                    else:
                        # 持仓不满 N 天，卖出被拦截
                        cur_ep['blocked'].append({'sig_i': i, 'held': held,
                                                  'kind': variant})
                        if variant == 'E':
                            defer_exec_i = entry_i + N   # 满 N 天后第一个交易日开盘卖
                        # variant == 'D'：信号作废，等新卖出信号
            # 防守仓中的卖出信号无效（与基线一致）

    # 期末仍持有进攻仓的段
    if cur_ep is not None:
        episodes.append(cur_ep)

    # ---- 净值（与 compare_position_size sizing_backtest base 口径一致，仓位恒为 0/1） ----
    wealth = [1.0]
    for i in range(1, n):
        d = idx[i]
        ev = exec_at.get(d)
        if ev is None:
            p = pos_atk[i]
            _, c_d = def_prices(def_hold[i])
            w = wealth[-1] * (p * float(c_atk.iloc[i]) / float(c_atk.iloc[i - 1])
                              + (1 - p) * float(c_d.iloc[i]) / float(c_d.iloc[i - 1]))
        else:
            p_old, p_new, d_old, d_new = ev
            o_do, c_do = def_prices(d_old)
            o_dn, c_dn = def_prices(d_new)
            overnight = p_old * float(o_atk.iloc[i]) / float(c_atk.iloc[i - 1]) \
                + (1 - p_old) * float(o_do.iloc[i]) / float(c_do.iloc[i - 1])
            intraday = p_new * float(c_atk.iloc[i]) / float(o_atk.iloc[i]) \
                + (1 - p_new) * float(c_dn.iloc[i]) / float(o_dn.iloc[i])
            cost = abs(p_new - p_old) * 2 * FEE
            w = wealth[-1] * overnight * intraday * (1 - cost)
        wealth.append(w)
    nav = pd.Series(wealth, index=idx)
    return {'nav': nav, 'trades': trades, 'idx': idx, 'label': label,
            'n_switch': len(trades), 'pos_atk': pd.Series(pos_atk, index=idx),
            'episodes': episodes, 'sig': sig,
            'prices': {'o_atk': o_atk, 'c_atk': c_atk,
                       'o_d1': o_d1, 'c_d1': c_d1, 'o_d2': o_d2, 'c_d2': c_d2}}


# ---------------------------------------------------------------------------
# 反事实分析：被拦截的卖出，"当时卖出"（基线路径）vs 约束后的实际路径
# ---------------------------------------------------------------------------
def shadow_baseline(a, b, tag0, sig, idx, P, pick_def, value_at='open'):
    """基线影子账户：close(a-1) 持有进攻、净值1；open(a) 卖出进攻换防守 tag0（扣双边费），
    之后按原始信号流正常轮动（T 收盘信号 → T+1 开盘执行），b 日估值（open 或 close）。
    返回净值倍数。"""
    o_atk, c_atk = P['o_atk'], P['c_atk']
    o_d1, c_d1, o_d2, c_d2 = P['o_d1'], P['c_d1'], P['o_d2'], P['c_d2']

    def prices_of(tag):
        if tag == 'atk':
            return o_atk, c_atk
        return (o_d1, c_d1) if tag == 'd1' else (o_d2, c_d2)

    w = 1.0
    held = 'atk'
    pending = tag0                # open(a) 执行：卖出进攻 → 防守 tag0
    for i in range(a, b + 1):
        o_h, c_h = prices_of(held)
        if pending is not None:           # open(i) 执行
            w *= float(o_h.iloc[i]) / float(c_h.iloc[i - 1]) * (1 - 2 * FEE)
            held = pending
            pending = None
            o_h, c_h = prices_of(held)
            if i == b and value_at == 'open':
                break
            w *= float(c_h.iloc[i]) / float(o_h.iloc[i])      # 新仓日内段
        else:
            if i == b and value_at == 'open':
                w *= float(o_h.iloc[i]) / float(c_h.iloc[i - 1])
                break
            w *= float(c_h.iloc[i]) / float(c_h.iloc[i - 1])
        if i == b:
            break
        # 收盘扫描新信号（close(a-1) 的卖出信号已被 open(a) 的执行消费，从 close(a) 扫起）
        if sig['buy_sig'].iloc[i] and held != 'atk':
            pending = 'atk'
        elif sig['sell_sig'].iloc[i] and held == 'atk':
            pending = pick_def(idx[i])
    return w


def counterfactual(ep, res, pick_def):
    """单个被拦截段的反事实对比。
    返回 dict 或 None（无拦截）。窗口 = [close(a-1), open/close(b)]：
      a = 首个被拦卖出信号的基线执行日（sig_i+1）
      b = 收敛日：变体实际卖出执行日 / E取消时基线再入场执行日 / 期末
    变体路径：全程持进攻到 b（若 b 为卖出执行日则扣双边退出费）。
    """
    if not ep['blocked']:
        return None
    P = res['prices']
    sig, idx = res['sig'], res['idx']
    n = len(idx)
    first_blk = ep['blocked'][0]
    a = first_blk['sig_i'] + 1
    if a >= n:
        return None
    # 基线在 open(a) 卖出时选的防守标的（信号日收盘动量，生产口径）
    tag0 = pick_def(idx[first_blk['sig_i']])

    o_atk, c_atk = P['o_atk'], P['c_atk']
    if ep['exit_i'] is not None:
        b = ep['exit_i']
        if ep['exit_via'] == 'defer':
            outcome = 'E顺延卖出'
        else:
            outcome = '新信号卖出'   # D 丢弃后等来的新卖出（或 E 取消后的正常卖出）
        exit_fee = True
        value_at = 'open'
        w_variant = float(o_atk.iloc[b]) / float(c_atk.iloc[a - 1]) * (1 - 2 * FEE)
    elif ep['cancel_buy_i'] is not None:
        # E 顺延被买入信号取消：基线在该买入信号 T+1 开盘再入场，两边收敛
        b = ep['cancel_buy_i'] + 1
        if b >= n:
            b = n - 1
            value_at = 'close'
            w_variant = float(c_atk.iloc[b]) / float(c_atk.iloc[a - 1])
            outcome = 'E顺延取消(期末)'
            exit_fee = False
        else:
            value_at = 'open'
            w_variant = float(o_atk.iloc[b]) / float(c_atk.iloc[a - 1])
            outcome = 'E顺延取消(转买)'
            exit_fee = False
    else:
        b = n - 1
        value_at = 'close'
        w_variant = float(c_atk.iloc[b]) / float(c_atk.iloc[a - 1])
        outcome = '持有至期末'
        exit_fee = False

    w_base = shadow_baseline(a, b, tag0, sig, idx, P, pick_def, value_at=value_at)

    # 基线原始段收益（入场 open(entry_i) → open(a) 卖出，扣出入双边费）
    entry_i = ep['entry_i']
    base_seg = float(o_atk.iloc[a]) / float(o_atk.iloc[entry_i]) * (1 - 2 * FEE) ** 2 - 1

    return {'entry_day': idx[entry_i], 'blk_day': idx[first_blk['sig_i']],
            'held_at_blk': first_blk['held'], 'n_blocked': len(ep['blocked']),
            'a_day': idx[a], 'b_day': idx[b], 'win_days': b - a + 1,
            'outcome': outcome, 'w_base': w_base - 1, 'w_variant': w_variant - 1,
            'diff': (w_variant - w_base) * 100, 'base_seg': base_seg * 100}


# ---------------------------------------------------------------------------
# 统计输出
# ---------------------------------------------------------------------------
def metrics_of(nav):
    return ((nav.iloc[-1] - 1) * 100, annualized(nav) * 100, max_drawdown(nav) * 100)


def yearly_returns(nav):
    y = nav.resample('YE').last()
    r = y.pct_change()
    r.iloc[0] = y.iloc[0] / nav.iloc[0] - 1
    r.index = r.index.year
    return r


def half_metrics(nav, lo, hi):
    w = nav[(nav.index >= lo) & (nav.index < hi)]
    if len(w) < 2:
        return None
    w = w / w.iloc[0]
    return annualized(w) * 100, max_drawdown(w) * 100


def run_panel(title, avg, etf_atk, def1, def2):
    out('=' * 100)
    out(title)
    out('=' * 100)

    results = {}
    results['基线'] = minhold_backtest(avg, etf_atk, def1, def2, START, label='基线')
    for v in ('D', 'E'):
        for N in N_LIST:
            nm = '%s%d(%s,N=%d)' % (v, N, '丢弃' if v == 'D' else '顺延', N)
            results[nm] = minhold_backtest(avg, etf_atk, def1, def2, START,
                                           variant=v, N=N, label=nm)

    idx = results['基线']['idx']
    out('回测窗口: %s ~ %s  共 %d 个交易日' % (idx[0].date(), idx[-1].date(), len(idx)))
    out()
    fmt = '%-16s %10s %10s %10s %8s %10s'
    out(fmt % ('方案', '累计收益', '年化收益', '最大回撤', '换仓次数', '进攻仓占比'))
    out('-' * 100)
    for name, res in results.items():
        cum, ann, mdd = metrics_of(res['nav'])
        out(fmt % (name, '%.2f%%' % cum, '%.2f%%' % ann, '%.2f%%' % mdd,
                   res['n_switch'], '%.1f%%' % (res['pos_atk'].mean() * 100)))
    out()

    # ---- 分年度收益（行=方案，列=年份） ----
    yr_table = {nm: yearly_returns(r['nav']) for nm, r in results.items()}
    years = list(yr_table['基线'].index)
    out('分年度收益(%):')
    out('%-16s' % '方案' + ''.join('%9d' % y for y in years))
    for nm in results:
        line = '%-16s' % nm
        for y in years:
            line += '%9.2f' % (yr_table[nm].get(y, np.nan) * 100)
        out(line)
    out()

    # ---- 前后半稳健性（2022-01-01 切分） ----
    halves = [('前半', idx[0], SPLIT_DATE),
              ('后半', SPLIT_DATE, idx[-1] + pd.Timedelta(days=1))]
    out('前后半稳健性（按 %s 切分；净值归一化后算年化/回撤）:' % SPLIT_DATE.date())
    out('%-16s | %9s %9s | %9s %9s | %s' % ('方案', '前半年化', '前半回撤',
                                            '后半年化', '后半回撤', '两半年化均≥基线?'))
    out('-' * 100)
    base_h = [half_metrics(results['基线']['nav'], lo, hi) for _, lo, hi in halves]
    robust = []
    for name, res in results.items():
        hh = [half_metrics(res['nav'], lo, hi) for _, lo, hi in halves]
        ok = all(hh[k][0] >= base_h[k][0] for k in (0, 1))
        if ok and name != '基线':
            robust.append(name)
        mark = '（基线）' if name == '基线' else ('是' if ok else '否')
        out('%-16s | %8.2f%% %8.2f%% | %8.2f%% %8.2f%% | %s'
            % (name, hh[0][0], hh[0][1], hh[1][0], hh[1][1], mark))
    out()
    return results, base_h, robust


def pick_def_factory(def1, def2):
    has_d2 = def2 is not None and len(def2) > 0
    mom_d1 = def1['close'] / def1['close'].shift(DEF_MOM_N) - 1
    mom_d2 = (def2['close'] / def2['close'].shift(DEF_MOM_N) - 1) if has_d2 else None

    def pick_def(day):
        m1 = mom_d1.get(day, np.nan)
        m2 = mom_d2.get(day, np.nan) if has_d2 else np.nan
        if pd.isna(m2):
            return 'd1'
        if pd.isna(m1):
            return 'd2'
        return 'd1' if m1 >= m2 else 'd2'
    return pick_def


def run_counterfactual(title, results, def1, def2):
    """被拦截的短持仓卖出逐笔反事实：基线路径 vs 约束路径。
    注意 D/E 两变体的持仓段划分不同（D 丢弃卖出后很快由新信号卖出并可能再入场，
    形成新的持仓段；E 顺延期间买入信号只取消顺延、不产生新段），两者拦截集合
    不完全相同，因此逐笔表分开列出，不做逐行配对。"""
    pick_def = pick_def_factory(def1, def2)
    out('=' * 100)
    out(title)
    out('=' * 100)
    out('说明: 每个被拦截段一行。"当时卖出"=基线路径(信号次日开盘卖进攻换防守,之后按原信号'
        '正常轮动,含费用)；')
    out('      "约束后"=变体实际路径(继续持进攻至 D 的新卖出/E 的顺延卖出或取消,含费用)。')
    out('      差值>0 表示拦截多赚, <0 表示拦截多亏。基线段收益=该进攻段入场→被拦卖出点的收益。')
    out()
    grand = {}
    for N in N_LIST:
        grand[N] = {}
        for v, vname in (('D', '丢弃'), ('E', '顺延')):
            res = results['%s%d(%s,N=%d)' % (v, N, vname, N)]
            cfs = [counterfactual(ep, res, pick_def) for ep in res['episodes']]
            cfs = [c for c in cfs if c]
            if not cfs:
                out('---- N=%d 变体%s(%s): 无被拦截的卖出信号 ----' % (N, v, vname))
                out()
                grand[N][v] = None
                continue
            out('---- N=%d 变体%s(%s): 被拦截段 %d 个 ----' % (N, v, vname, len(cfs)))
            out('%-10s %-10s %4s %4s | %-16s %10s %10s %8s | %9s'
                % ('入场日', '首个被拦卖出', '已持', '拦截',
                   '结局', '当时卖出', '约束后', '差值', '基线段收益'))
            n_lose, n_win, d_sum = 0, 0, 0.0
            for c in cfs:
                if c['base_seg'] < 0:
                    n_lose += 1
                if c['diff'] > 0:
                    n_win += 1
                d_sum += c['diff']
                out('%-10s %-10s %4d %4d | %-16s %9.2f%% %9.2f%% %+7.2f%% | %+8.2f%%'
                    % (c['entry_day'].date(), c['blk_day'].date(), c['held_at_blk'],
                       c['n_blocked'], c['outcome'],
                       c['w_base'] * 100, c['w_variant'] * 100, c['diff'],
                       c['base_seg']))
            out('  小计: 拦截 %d 段, 基线段本身亏损 %d 个(%.0f%%) | 拦截多赚 %d/%d, 累计差值 %+.2f%%'
                % (len(cfs), n_lose, n_lose / len(cfs) * 100, n_win, len(cfs), d_sum))
            out()
            grand[N][v] = {'n': len(cfs), 'n_lose': n_lose, 'win': n_win, 'sum': d_sum}
    return grand


def main():
    avg = load('avg_880003.csv')
    def1 = load('etf_512890_hfq.csv')
    def2 = load('etf_159201_hfq.csv')
    e1000 = load('etf_512100_hfq.csv')
    e552 = load('etf_159552_hfq.csv')
    etf_atk = pd.concat([e1000[e1000.index < SWITCH_ATK],
                         e552[e552.index >= SWITCH_ATK]])
    out('最少持仓 N 天约束回测（变体D=丢弃被拦卖出 / 变体E=顺延至满N天卖出）')
    out('本地数据: 880003 %s ~ %s | 512890 %s ~ %s | 159201 %s ~ %s | 进攻腿拼接 %s ~ %s'
        % (avg.index[0].date(), avg.index[-1].date(),
           def1.index[0].date(), def1.index[-1].date(),
           def2.index[0].date(), def2.index[-1].date(),
           etf_atk.index[0].date(), etf_atk.index[-1].date()))
    out('与"冷却期"的区别: 冷却期(reduce_loss.py V2)是换仓后 K 天内忽略一切新信号,买卖双向封锁;')
    out('最少持仓 N 天只在买入进攻腿后约束卖出端: 未满 N 天的卖出信号被丢弃(D)或顺延(E),买入端不受影响。')
    out()

    # ---- 基线对账 ----
    res_a = minhold_backtest(avg, etf_atk, def1, None, START, label='A')
    cum, ann, mdd = metrics_of(res_a['nav'])
    out('【口径对账A】防守=512890固定: 累计 %.2f%% 年化 %.2f%% 回撤 %.2f%% 换仓 %d'
        '（对账基准 +538.68%% / 27.30%% / -18.06%% / 135 次 → %s）'
        % (cum, ann, mdd, res_a['n_switch'],
           '复现一致' if (abs(cum - 538.68) < 0.01 and res_a['n_switch'] == 135) else '不一致!'))
    res_b = minhold_backtest(avg, etf_atk, def1, def2, START, label='B')
    cum_b, ann_b, mdd_b = metrics_of(res_b['nav'])
    out('【口径对账B】防守=512890/159201双择优: 累计 %.2f%% 年化 %.2f%% 回撤 %.2f%% 换仓 %d'
        '（基准 +593.20%% / 28.66%%）' % (cum_b, ann_b, mdd_b, res_b['n_switch']))
    out()

    resA, base_hA, robustA = run_panel('面板A · 防守腿 = 512890 固定（对账口径）',
                                       avg, etf_atk, def1, None)
    resB, base_hB, robustB = run_panel('面板B · 防守腿 = 512890/159201 近20日动量双择优（生产口径）',
                                       avg, etf_atk, def1, def2)

    grandA = run_counterfactual('反事实分析 · 面板A（防守=512890固定）', resA, def1, None)
    grandB = run_counterfactual('反事实分析 · 面板B（防守=512890/159201双择优）', resB, def1, def2)

    # ---- 结论 ----
    out('=' * 100)
    out('结论')
    out('=' * 100)
    baseA = metrics_of(resA['基线']['nav'])
    baseB = metrics_of(resB['基线']['nav'])
    out('集成门槛: 前后两半（2022-01-01 切分）年化均 ≥ 基线，且全区间最大回撤恶化 ≤ 2pp。')
    out('%-16s | %9s %9s | %8s %8s | %s'
        % ('方案', 'A年化', 'A回撤Δpp', 'B年化', 'B回撤Δpp', '是否过门槛'))
    passed = []
    for v in ('D', 'E'):
        for N in N_LIST:
            nm = '%s%d(%s,N=%d)' % (v, N, '丢弃' if v == 'D' else '顺延', N)
            mA = metrics_of(resA[nm]['nav'])
            mB = metrics_of(resB[nm]['nav'])
            ok_half = nm in robustA and nm in robustB
            ok_mdd = (mA[2] >= baseA[2] - 2.0) and (mB[2] >= baseB[2] - 2.0)
            ok = ok_half and ok_mdd
            if ok:
                passed.append((nm, mA[1], mB[1]))
            out('%-16s | %8.2f%% %+8.2f | %8.2f%% %+8.2f | %s%s'
                % (nm, mA[1], mA[2] - baseA[2], mB[1], mB[2] - baseB[2],
                   '通过' if ok else '未过',
                   '' if ok else ('（前后半: %s; 回撤: %s）'
                                  % ('过' if ok_half else '不过',
                                     '过' if ok_mdd else '不过'))))
    out()
    if passed:
        best = max(passed, key=lambda x: x[1] + x[2])
        out('过门槛方案: %s' % '、'.join(p[0] for p in passed))
        out('两口径年化之和最高者: %s（A %.2f%% / B %.2f%%）' % (best[0], best[1], best[2]))
    else:
        out('过门槛方案: 无')
    out()
    out('反事实汇总（被拦截段统计）:')
    out('%-14s | %3s | %6s %6s | %8s %9s | %8s %9s'
        % ('口径', 'N', 'D拦截段', 'D亏损段', 'D多赚', 'D累计差', 'E多赚', 'E累计差'))
    for tag, grand in (('A(512890)', grandA), ('B(双择优)', grandB)):
        for N in N_LIST:
            g = grand.get(N) or {}
            gd, ge = g.get('D'), g.get('E')
            if gd:
                out('%-14s | %3d | %6d %6d | %5d/%-2d %+8.2f%% | %5d/%-2d %+8.2f%%'
                    % (tag, N, gd['n'], gd['n_lose'],
                       gd['win'], gd['n'], gd['sum'],
                       ge['win'], ge['n'], ge['sum']))
    out()

    path = os.path.join(BASE, 'result_min_hold.txt')
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(_lines) + '\n')
    out('结果已写入 %s' % path)


if __name__ == '__main__':
    main()
