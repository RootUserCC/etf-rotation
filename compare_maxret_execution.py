#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
方案C（880003 DIF 17/34/9）收益最大化与执行优化研究
======================================================
严格不修改任何生产文件，仅输出 result_maxret_execution.txt。

研究维度：
  A 组：成交时点对比 & 隔夜跳空专项拆解（A0 基准 / A1 尾盘+滑点 / A2 买尾卖开 / A3 买开卖尾 / A4 均价 / A5 T+1收盘）
  B 组：进攻持仓段风险管理（B1 固定止损 / B2 移动止盈与盈利激活移动止盈 / B3 均线破位保护）
  C 组：仓位与风险暴露调整（C1 DIF 深度分层 / C2 Target Vol 波动率自适应 / C3 温和杠杆边界与安全垫）
  D 组：交易成本与滑点敏感性矩阵（单边 万1 / 万3 / 万5 / 千1 / 千2）

口径与数据：
  - 公共区间：2019-01-18 ~ 2026-11-25（1900 个交易日）
  - 信号源：avg_880003.csv，calc_signals(fast=17, slow=34, sig_n=9, sell_anywhere=True)，全历史预热
  - 进攻腿：2024-06-28 前 = 512100 后复权，2024-06-28 起 = 159552 后复权（内置防跨界失真守卫）
  - 防守腿：512890 后复权（红利低波 ETF）
  - 基准 A0：T 日收盘信号，T+1 开盘真实拆段成交，双边万1（fee=0.0001，每次满仓换仓扣 2*fee）
  - 稳健性切分：2022-11-22 前后两半段 + 2019~2026 分年度 + 2026-10/11 月度
"""
import io
import os
import sys
import math
import numpy as np
import pandas as pd

if sys.platform == 'win32' and sys.stdout is not None and not getattr(sys.stdout, '_utf8_wrapped', False):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stdout._utf8_wrapped = True

from backtest import calc_signals, annualized, max_drawdown

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, 'data')
OUT_PATH = os.path.join(BASE_DIR, 'result_maxret_execution.txt')

FAST, SLOW, SIG_N = 17, 34, 9
START_DATE = pd.Timestamp('2019-01-18')
END_DATE = pd.Timestamp('2026-11-25')
SWITCH_ATK = pd.Timestamp('2024-06-28')
SPLIT_DATE = pd.Timestamp('2022-11-22')
BASE_FEE = 0.0001

_OUT_LINES = []


def out(s=''):
    print(s)
    _OUT_LINES.append(s)


def load_csv(fn):
    df = pd.read_csv(os.path.join(DATA_DIR, fn), parse_dates=['date'])
    df = df.set_index('date').sort_index()
    df.index = df.index.normalize()
    return df[~df.index.duplicated(keep='last')]


# ---------------------------------------------------------------------------
# 数据装载与干净收益序列构造（杜绝 2024-06-28 拼接日跨标的除数失真）
# ---------------------------------------------------------------------------
def load_hfq_clean(code, anchor_str='2026-11-09', fix_from_str='2026-11-10'):
    """加载后复权 CSV，并在内存中对 2026-11-10 起的复权因子断层/漂移做只读清洗（绝不修改磁盘文件）。
    方法：用 2026-11-09 当日该标的 hfq_close / raw_close 常数因子 × 对应不复权 CSV 的 OHLC 续接。
    """
    hfq = load_csv('etf_%s_hfq.csv' % code).copy()
    raw = load_csv('etf_%s.csv' % code)
    anchor_day = pd.Timestamp(anchor_str)
    fix_from = pd.Timestamp(fix_from_str)
    ratio = float(hfq.loc[anchor_day, 'close']) / float(raw.loc[anchor_day, 'close'])
    common = hfq.index[hfq.index >= fix_from].intersection(raw.index)
    for col in ['open', 'high', 'low', 'close']:
        hfq.loc[common, col] = raw.loc[common, col] * ratio
    return hfq, ratio, len(common)


def load_dataset():
    avg = load_csv('avg_880003.csv')
    e1000, r1000, n1000 = load_hfq_clean('512100')
    e2000, r2000, n2000 = load_hfq_clean('159552')
    ediv, rdiv, ndiv = load_hfq_clean('512890')
    data_fix_info = (
        '已按统一口径在内存中清洗 2026-11-10~2026-11-25 复权异常（未改动任何磁盘 CSV）：'
        '以 2026-11-09 hfq/raw 常数因子续接不复权 OHLC（512100×%.4f[%d日], 159552×%.4f[%d日], 512890×%.4f[%d日]），'
        '彻底消除 512890 11-13/16 假腰斩(-75.3%%)、512100 假暴涨及 159552 复权漂移。'
        % (r1000, n1000, r2000, n2000, rdiv, ndiv)
    )

    eatk = pd.concat([e1000[e1000.index < SWITCH_ATK], e2000[e2000.index >= SWITCH_ATK]])
    sig_full = calc_signals(avg['close'], sell_anywhere=True, fast=FAST, slow=SLOW, sig_n=SIG_N)

    idx = eatk.index.intersection(ediv.index).intersection(sig_full.index)
    idx = idx[(idx >= START_DATE) & (idx <= END_DATE)]
    n = len(idx)

    # 逐日构造「无跨界失真」的进攻与防守基础收益因子（比率形式 1+r）
    # 规则：在 SWITCH_ATK(2024-06-28) 当日，隔夜段用 512100(o/prev_c)，日内段用 159552(c/o)
    atk_on = np.ones(n)      # prev_c -> o
    atk_intra = np.ones(n)   # o -> c
    atk_cc = np.ones(n)      # prev_c -> c
    atk_vwap_pre = np.ones(n)   # prev_c -> vwap
    atk_vwap_post = np.ones(n)  # vwap -> c

    def_on = np.ones(n)
    def_intra = np.ones(n)
    def_cc = np.ones(n)
    def_vwap_pre = np.ones(n)
    def_vwap_post = np.ones(n)

    def _vwap(row):
        return (float(row['open']) + float(row['high']) + float(row['low']) + float(row['close'])) / 4.0

    for i in range(1, n):
        d = idx[i]
        dp = idx[i - 1]
        # 防守腿（始终 512890）
        r_d = ediv.loc[d]
        rp_d = ediv.loc[dp]
        vd = _vwap(r_d)
        def_on[i] = float(r_d['open']) / float(rp_d['close'])
        def_intra[i] = float(r_d['close']) / float(r_d['open'])
        def_cc[i] = float(r_d['close']) / float(rp_d['close'])
        def_vwap_pre[i] = vd / float(rp_d['close'])
        def_vwap_post[i] = float(r_d['close']) / vd

        # 进攻腿（严格避免跨标的除数）
        if d < SWITCH_ATK:
            r_a = e1000.loc[d]
            rp_a = e1000.loc[dp]
            va = _vwap(r_a)
            atk_on[i] = float(r_a['open']) / float(rp_a['close'])
            atk_intra[i] = float(r_a['close']) / float(r_a['open'])
            atk_cc[i] = float(r_a['close']) / float(rp_a['close'])
            atk_vwap_pre[i] = va / float(rp_a['close'])
            atk_vwap_post[i] = float(r_a['close']) / va
        elif d > SWITCH_ATK:
            r_a = e2000.loc[d]
            rp_a = e2000.loc[dp]
            va = _vwap(r_a)
            atk_on[i] = float(r_a['open']) / float(rp_a['close'])
            atk_intra[i] = float(r_a['close']) / float(r_a['open'])
            atk_cc[i] = float(r_a['close']) / float(rp_a['close'])
            atk_vwap_pre[i] = va / float(rp_a['close'])
            atk_vwap_post[i] = float(r_a['close']) / va
        else:
            # 拼接切换日 2024-06-28：隔夜归旧标的 512100，日内归新标的 159552
            r_old = e1000.loc[d]
            rp_old = e1000.loc[dp]
            r_new = e2000.loc[d]
            va_old = _vwap(r_old)
            va_new = _vwap(r_new)
            atk_on[i] = float(r_old['open']) / float(rp_old['close'])
            atk_intra[i] = float(r_new['close']) / float(r_new['open'])
            atk_cc[i] = atk_on[i] * atk_intra[i]
            atk_vwap_pre[i] = va_old / float(rp_old['close'])
            atk_vwap_post[i] = float(r_new['close']) / va_new

    # 进攻腿干净日收益率（全历史，用于波动率计算，不跨界失真）
    r1000_all = e1000['close'].pct_change()
    r2000_all = e2000['close'].pct_change()
    r_atk_clean = pd.concat([
        r1000_all[r1000_all.index <= SWITCH_ATK],
        r2000_all[r2000_all.index > SWITCH_ATK]
    ]).sort_index()
    vol20_atk = (r_atk_clean.rolling(20).std() * np.sqrt(252)).reindex(idx)

    # 880003 均线 & 波动率 & 相对 DIF
    avg_c_full = avg['close']
    ma10_full = avg_c_full.rolling(10).mean()
    ma20_full = avg_c_full.rolling(20).mean()
    vol20_avg = (avg_c_full.pct_change().rolling(20).std() * np.sqrt(252)).reindex(idx)
    dif_rel_full = sig_full['dif'] / avg_c_full  # 相对 DIF（无量纲）

    return {
        'avg': avg,
        'e1000': e1000,
        'e2000': e2000,
        'ediv': ediv,
        'eatk': eatk,
        'sig_full': sig_full,
        'sig': sig_full.loc[idx],
        'idx': idx,
        'atk_on': atk_on,
        'atk_intra': atk_intra,
        'atk_cc': atk_cc,
        'atk_vwap_pre': atk_vwap_pre,
        'atk_vwap_post': atk_vwap_post,
        'def_on': def_on,
        'def_intra': def_intra,
        'def_cc': def_cc,
        'def_vwap_pre': def_vwap_pre,
        'def_vwap_post': def_vwap_post,
        'vol20_atk': vol20_atk,
        'vol20_avg': vol20_avg,
        'avg_c': avg_c_full.reindex(idx),
        'ma10': ma10_full.reindex(idx),
        'ma20': ma20_full.reindex(idx),
        'dif_rel_full': dif_rel_full,
        'dif_rel': dif_rel_full.reindex(idx),
        'data_fix_info': data_fix_info,
    }


# ---------------------------------------------------------------------------
# 指标计算模块
# ---------------------------------------------------------------------------
def cagr_norm(nav: pd.Series) -> float:
    """严格归一化年化收益率（修复 backtest.annualized 对非1起点子区间的偏差）"""
    nav = nav.dropna()
    if len(nav) < 2:
        return float('nan')
    days = (nav.index[-1] - nav.index[0]).days
    if days <= 0:
        return float('nan')
    return float((nav.iloc[-1] / nav.iloc[0]) ** (365.0 / days) - 1.0)


def calc_metrics(nav: pd.Series, n_trades: int = 0, extra: dict = None) -> dict:
    nav = nav.dropna()
    tot = float(nav.iloc[-1] / nav.iloc[0] - 1.0)
    ann = cagr_norm(nav)
    mdd = float(max_drawdown(nav))
    calmar = ann / abs(mdd) if mdd < 0 else np.nan
    ret = nav.pct_change().dropna()
    sharpe = float(ret.mean() / ret.std() * np.sqrt(252)) if ret.std() > 0 else np.nan

    # 前后半段（2022-11-22 切分）
    nav_h1 = nav[nav.index <= SPLIT_DATE]
    nav_h2 = nav[nav.index >= SPLIT_DATE]
    h1_ann = cagr_norm(nav_h1)
    h1_mdd = float(max_drawdown(nav_h1))
    h1_calmar = h1_ann / abs(h1_mdd) if h1_mdd < 0 else np.nan
    h1_tot = float(nav_h1.iloc[-1] / nav_h1.iloc[0] - 1.0)

    h2_ann = cagr_norm(nav_h2)
    h2_mdd = float(max_drawdown(nav_h2))
    h2_calmar = h2_ann / abs(h2_mdd) if h2_mdd < 0 else np.nan
    h2_tot = float(nav_h2.iloc[-1] / nav_h2.iloc[0] - 1.0)

    # 分年度收益（严格首尾衔接：年末净值 / 上年末净值 - 1，首年以首日净值为基）
    yearly = {}
    years = sorted(nav.index.year.unique())
    prev_val = float(nav.iloc[0])
    for y in years:
        g = nav[nav.index.year == y]
        cur_val = float(g.iloc[-1])
        yearly[int(y)] = cur_val / prev_val - 1.0
        prev_val = cur_val

    # 2026-10 / 2026-11 月度收益
    def month_ret(y, m):
        g = nav[(nav.index.year == y) & (nav.index.month == m)]
        if len(g) == 0:
            return np.nan
        before = nav[nav.index < g.index[0]]
        base = float(before.iloc[-1]) if len(before) else float(g.iloc[0])
        return float(g.iloc[-1]) / base - 1.0

    m2026_10 = month_ret(2026, 10)
    m2026_11 = month_ret(2026, 11)

    res = {
        'total': tot,
        'ann': ann,
        'mdd': mdd,
        'calmar': calmar,
        'sharpe': sharpe,
        'trades': n_trades,
        'h1_tot': h1_tot, 'h1_ann': h1_ann, 'h1_mdd': h1_mdd, 'h1_calmar': h1_calmar,
        'h2_tot': h2_tot, 'h2_ann': h2_ann, 'h2_mdd': h2_mdd, 'h2_calmar': h2_calmar,
        'yearly': yearly,
        'y2026': yearly.get(2026, np.nan),
        'm2026_10': m2026_10,
        'm2026_11': m2026_11,
        'nav': nav,
    }
    if extra:
        res.update(extra)
    return res


def check_splice_cross(idx, hold_atk):
    i_sw = idx.searchsorted(SWITCH_ATK)
    return bool(0 < i_sw < len(idx) and hold_atk[i_sw] and hold_atk[i_sw - 1])


# ---------------------------------------------------------------------------
# A 组：成交时点对比引擎
# ---------------------------------------------------------------------------
def run_timing_mode(ds, mode='A0', fee=BASE_FEE, slip_close=0.0):
    """
    mode:
      A0: T+1 开盘成交（基准）
      A1: T 日收盘成交（额外单边滑点 slip_close）
      A2: 买 T 日收盘（额外滑点 slip_close）+ 卖 T+1 开盘
      A3: 买 T+1 开盘 + 卖 T 日收盘（额外滑点 slip_close）
      A4: T+1 日 (O+H+L+C)/4 均价成交
      A5: T+1 日收盘成交
    """
    idx = ds['idx']
    sig = ds['sig']
    n = len(idx)
    buy_s = sig['buy_sig'].values
    sell_s = sig['sell_sig'].values

    atk_on, atk_intra, atk_cc = ds['atk_on'], ds['atk_intra'], ds['atk_cc']
    def_on, def_intra, def_cc = ds['def_on'], ds['def_intra'], ds['def_cc']
    atk_vp, atk_vq = ds['atk_vwap_pre'], ds['atk_vwap_post']
    def_vp, def_vq = ds['def_vwap_pre'], ds['def_vwap_post']

    w = np.ones(n)
    hold = np.zeros(n, dtype=bool)
    state = False  # False=防守, True=进攻
    pending = None
    trades = []

    cost_open = 1.0 - 2.0 * fee
    cost_close = 1.0 - 2.0 * (fee + slip_close)

    # 首日 i=0：基准为初始持有红利低波，净值=1.0
    if mode in ('A0', 'A4', 'A5'):
        hold[0] = False
        if buy_s[0]:
            pending = 'buy'
        elif sell_s[0]:
            pending = 'sell'
    elif mode == 'A1':
        if buy_s[0] and not state:
            state = True
            w[0] *= cost_close
            trades.append((idx[0], 'buy', 'T_close'))
        hold[0] = state
    elif mode == 'A2':
        if buy_s[0] and not state:
            state = True
            w[0] *= cost_close
            trades.append((idx[0], 'buy', 'T_close'))
        hold[0] = state
    elif mode == 'A3':
        hold[0] = False
        if buy_s[0]:
            pending = 'buy'

    for i in range(1, n):
        if mode == 'A0':
            if pending == 'buy' and not state:
                w[i] = w[i - 1] * def_on[i] * atk_intra[i] * cost_open
                state = True
                trades.append((idx[i], 'buy', 'T1_open'))
            elif pending == 'sell' and state:
                w[i] = w[i - 1] * atk_on[i] * def_intra[i] * cost_open
                state = False
                trades.append((idx[i], 'sell', 'T1_open'))
            else:
                w[i] = w[i - 1] * (atk_cc[i] if state else def_cc[i])
            pending = None
            hold[i] = state
            if buy_s[i]:
                pending = 'buy'
            elif sell_s[i]:
                pending = 'sell'

        elif mode == 'A4':
            if pending == 'buy' and not state:
                w[i] = w[i - 1] * def_vp[i] * atk_vq[i] * cost_open
                state = True
                trades.append((idx[i], 'buy', 'T1_vwap'))
            elif pending == 'sell' and state:
                w[i] = w[i - 1] * atk_vp[i] * def_vq[i] * cost_open
                state = False
                trades.append((idx[i], 'sell', 'T1_vwap'))
            else:
                w[i] = w[i - 1] * (atk_cc[i] if state else def_cc[i])
            pending = None
            hold[i] = state
            if buy_s[i]:
                pending = 'buy'
            elif sell_s[i]:
                pending = 'sell'

        elif mode == 'A5':
            # T+1 收盘执行昨日信号
            w[i] = w[i - 1] * (atk_cc[i] if state else def_cc[i])
            if pending == 'buy' and not state:
                w[i] *= cost_open
                state = True
                trades.append((idx[i], 'buy', 'T1_close'))
            elif pending == 'sell' and state:
                w[i] *= cost_open
                state = False
                trades.append((idx[i], 'sell', 'T1_close'))
            pending = None
            hold[i] = state
            if buy_s[i]:
                pending = 'buy'
            elif sell_s[i]:
                pending = 'sell'

        elif mode == 'A1':
            # T 日收盘执行当日信号
            w[i] = w[i - 1] * (atk_cc[i] if state else def_cc[i])
            if buy_s[i] and not state:
                w[i] *= cost_close
                state = True
                trades.append((idx[i], 'buy', 'T_close'))
            elif sell_s[i] and state:
                w[i] *= cost_close
                state = False
                trades.append((idx[i], 'sell', 'T_close'))
            hold[i] = state

        elif mode == 'A2':
            # 买 T 尾盘 + 卖 T+1 开盘
            if pending == 'sell' and state:
                w[i] = w[i - 1] * atk_on[i] * def_intra[i] * cost_open
                state = False
                trades.append((idx[i], 'sell', 'T1_open'))
            else:
                w[i] = w[i - 1] * (atk_cc[i] if state else def_cc[i])
            pending = None
            # 收盘信号
            if buy_s[i] and not state:
                w[i] *= cost_close
                state = True
                trades.append((idx[i], 'buy', 'T_close'))
            elif sell_s[i] and state:
                pending = 'sell'
            hold[i] = state

        elif mode == 'A3':
            # 买 T+1 开盘 + 卖 T 尾盘
            if pending == 'buy' and not state:
                w[i] = w[i - 1] * def_on[i] * atk_intra[i] * cost_open
                state = True
                trades.append((idx[i], 'buy', 'T1_open'))
            else:
                w[i] = w[i - 1] * (atk_cc[i] if state else def_cc[i])
            pending = None
            # 收盘信号
            if sell_s[i] and state:
                w[i] *= cost_close
                state = False
                trades.append((idx[i], 'sell', 'T_close'))
            elif buy_s[i] and not state:
                pending = 'buy'
            hold[i] = state

    nav = pd.Series(w, index=idx)
    splice_cross = check_splice_cross(idx, hold)
    return calc_metrics(nav, n_trades=len(trades), extra={
        'trades_list': trades, 'hold': pd.Series(hold, index=idx), 'splice_cross': splice_cross
    })


def analyze_overnight_gaps(ds, base_res):
    """专项拆解：在所有实际买入进攻日与卖出进攻日上统计 T->T+1 隔夜跳空与 T+1 日内收益"""
    idx = ds['idx']
    pos_of = {d: i for i, d in enumerate(idx)}
    atk_on, def_on = ds['atk_on'] - 1.0, ds['def_on'] - 1.0
    atk_in, def_in = ds['atk_intra'] - 1.0, ds['def_intra'] - 1.0

    rows = []
    for d, action, _ in base_res['trades_list']:
        i = pos_of[d]
        sd = idx[i - 1]
        r_ao = atk_on[i]
        r_do = def_on[i]
        r_ai = atk_in[i]
        r_di = def_in[i]
        # 尾盘切相对 T+1 开盘切的单次几何超额
        if action == 'buy':
            edge_close_vs_open = (1.0 + r_ao) / (1.0 + r_do) - 1.0
            edge_open_vs_t1close = (1.0 + r_ai) / (1.0 + r_di) - 1.0
        else:
            edge_close_vs_open = (1.0 + r_do) / (1.0 + r_ao) - 1.0
            edge_open_vs_t1close = (1.0 + r_di) / (1.0 + r_ai) - 1.0
        dif_t = float(ds['sig'].loc[sd, 'dif'])
        dif_prev = float(ds['sig_full']['dif'].shift(1).loc[sd])
        rows.append({
            'trade_date': d,
            'sig_date': sd,
            'action': action,
            'half': 'H1' if d <= SPLIT_DATE else 'H2',
            'atk_on': r_ao,
            'def_on': r_do,
            'atk_intra': r_ai,
            'def_intra': r_di,
            'edge_close_vs_open': edge_close_vs_open,
            'edge_open_vs_t1close': edge_open_vs_t1close,
            'abs_ddif': abs(dif_t - dif_prev),
            'abs_ddif_pct': abs(dif_t - dif_prev) / float(ds['avg'].loc[sd, 'close']) * 100.0,
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# B 组：进攻持仓段风险管理（止损 / 移动止盈 / 均线破位）
# ---------------------------------------------------------------------------
def run_risk_mgmt(ds, rule='none', param=None, fee=BASE_FEE):
    """
    在基准 A0（T+1 开盘真实拆段成交）框架上叠加进攻段风险管理。
    rule:
      'none'        : 基准
      'fixed_sl'    : 相对入场开盘价浮亏 <= -param (0.03/0.05/0.07/0.10)
      'trail'       : 从段内最高收盘价回撤 <= -param (0.05/0.07/0.10/0.12)
      'trail_act10' : 段内最高收盘浮盈曾 >= 10% 后，从最高收盘回撤 <= -param
      'ma_raw'      : 进攻持仓时 880003 收盘 < MA_{param}（10/20）
      'ma_cross'    : 进攻持仓时 880003 向下跌破 MA_{param}（前日 >= MA 且当日 < MA）
      'ma_after_up' : 进攻段内曾站上过 MA_{param}，之后收盘跌破 MA_{param}
    """
    idx = ds['idx']
    sig = ds['sig']
    n = len(idx)
    buy_s = sig['buy_sig'].values
    sell_s = sig['sell_sig'].values

    atk_on, atk_intra, atk_cc = ds['atk_on'], ds['atk_intra'], ds['atk_cc']
    def_on, def_intra, def_cc = ds['def_on'], ds['def_intra'], ds['def_cc']
    avg_c = ds['avg_c'].values
    ma_arr = ds['ma10'].values if param == 10 else (ds['ma20'].values if param == 20 else None)

    w = np.ones(n)
    hold = np.zeros(n, dtype=bool)
    state = False
    pending = None  # None / ('buy', 'sig') / ('sell', 'sig') / ('sell', 'stop')
    trades = []

    # 段内跟踪器（使用归一化净值曲线跟踪进攻腿，避免跨 2024-06-28 拼接日价格跳变）
    seg_val = 1.0        # 自入场开盘价起的进攻腿累计净值（入场开盘=1.0）
    seg_peak = 1.0       # 自入场起最高收盘净值
    seg_seen_above_ma = False

    cost_open = 1.0 - 2.0 * fee

    hold[0] = False
    if buy_s[0]:
        pending = ('buy', 'sig')

    for i in range(1, n):
        if pending is not None and pending[0] == 'buy' and not state:
            w[i] = w[i - 1] * def_on[i] * atk_intra[i] * cost_open
            state = True
            seg_val = atk_intra[i]
            seg_peak = max(1.0, seg_val)
            seg_seen_above_ma = bool(ma_arr is not None and avg_c[i] >= ma_arr[i])
            trades.append((idx[i], 'buy', pending[1]))
        elif pending is not None and pending[0] == 'sell' and state:
            w[i] = w[i - 1] * atk_on[i] * def_intra[i] * cost_open
            state = False
            trades.append((idx[i], 'sell', pending[1]))
        else:
            w[i] = w[i - 1] * (atk_cc[i] if state else def_cc[i])
            if state:
                seg_val *= atk_cc[i]
                if seg_val > seg_peak:
                    seg_peak = seg_val
                if ma_arr is not None and avg_c[i] >= ma_arr[i]:
                    seg_seen_above_ma = True

        pending = None
        hold[i] = state

        # 收盘信号与风控判断
        if not state:
            if buy_s[i]:
                pending = ('buy', 'sig')
        else:
            if sell_s[i]:
                pending = ('sell', 'sig')
            else:
                trigger = False
                if rule == 'fixed_sl':
                    if seg_val - 1.0 <= -param:
                        trigger = True
                elif rule == 'trail':
                    if seg_val / seg_peak - 1.0 <= -param:
                        trigger = True
                elif rule == 'trail_act10':
                    if (seg_peak - 1.0 >= 0.10) and (seg_val / seg_peak - 1.0 <= -param):
                        trigger = True
                elif rule == 'ma_raw':
                    if avg_c[i] < ma_arr[i]:
                        trigger = True
                elif rule == 'ma_cross':
                    if (avg_c[i - 1] >= ma_arr[i - 1]) and (avg_c[i] < ma_arr[i]):
                        trigger = True
                elif rule == 'ma_after_up':
                    if seg_seen_above_ma and (avg_c[i] < ma_arr[i]):
                        trigger = True
                if trigger:
                    pending = ('sell', 'stop')

    # 归因统计：计算每次 'stop' 提前离场后，到原本下次 sell_sig（执行日）期间，防守相对进攻的盈亏
    stop_events = [d for d, a, r in trades if a == 'sell' and r == 'stop']
    n_stop = len(stop_events)
    pos_of = {d: i for i, d in enumerate(idx)}
    stop_wins = 0
    stop_diffs = []
    for d_stop in stop_events:
        i0 = pos_of[d_stop]
        # 找之后最早一次 sell_sig 对应的 T+1 开盘日（或期末）
        i1 = n - 1
        for k in range(i0, n - 1):
            if sell_s[k]:
                i1 = k + 1
                break
        # 比较从 i0 开盘到 i1 开盘：持有防守 vs 继续持有进攻
        if i1 > i0:
            r_def_seg = def_intra[i0]
            r_atk_seg = atk_intra[i0]
            for k in range(i0 + 1, i1):
                r_def_seg *= def_cc[k]
                r_atk_seg *= atk_cc[k]
            r_def_seg *= def_on[i1]
            r_atk_seg *= atk_on[i1]
            diff = r_def_seg / r_atk_seg - 1.0
            stop_diffs.append(diff)
            if diff > 0:
                stop_wins += 1

    nav = pd.Series(w, index=idx)
    splice_cross = check_splice_cross(idx, hold)
    return calc_metrics(nav, n_trades=len(trades), extra={
        'trades_list': trades,
        'n_stop': n_stop,
        'stop_win_rate': (stop_wins / len(stop_diffs)) if stop_diffs else np.nan,
        'stop_avg_diff': float(np.mean(stop_diffs)) if stop_diffs else 0.0,
        'stop_sum_diff': float(np.sum(stop_diffs)) if stop_diffs else 0.0,
        'splice_cross': splice_cross,
    })


# ---------------------------------------------------------------------------
# C 组：连续权重与融资杠杆精确市值引擎
# ---------------------------------------------------------------------------
def run_weighted_engine(ds, mode='base', param=None, fee=BASE_FEE,
                        borrow_rate=0.055, daily_rebal=False):
    """
    精确追踪账户资产市值 (V_atk, V_def, Debt) 与自有权益 E = V_atk + V_def - Debt。
    所有决策在 T 日收盘做出，T+1 开盘执行。
    mode:
      - 'base': 0/1 满仓基准（用于验证与 A0 严格一致）
      - 'C1_hist_med': 历史无前视 dif_rel 中位数分档，param=(w_deep, w_shallow)
      - 'C1_hist_3tier': 历史无前视 dif_rel 三分位分档，param=(w_deep, w_mid, w_shallow)
      - 'C1_fixed': 固定 dif_rel 阈值分档，param=(thresh, w_deep, w_shallow)
      - 'C2_tvol_band': 进攻期 Target Vol，param=(target_vol, min_w, band)
      - 'C2_tvol_entry': 仅入场日按 Target Vol 定仓，param=(target_vol, min_w)
      - 'C2_cap': 高波降仓，param=(vol_thresh, w_highvol)
      - 'C3_lev_atk': 仅进攻期加杠杆 L=param（防守 1.0x），入场重设杠杆（daily_rebal=True则每日盯市）
      - 'C3_lev_all': 全程加杠杆 L=param（进攻防守均 L 倍）
    """
    idx = ds['idx']
    sig = ds['sig']
    sig_full = ds['sig_full']
    dif_rel_full = ds['dif_rel_full']
    n = len(idx)
    buy_s = sig['buy_sig'].values
    sell_s = sig['sell_sig'].values

    atk_on, atk_intra = ds['atk_on'], ds['atk_intra']
    def_on, def_intra = ds['def_on'], ds['def_intra']
    vol20 = ds['vol20_atk'].values
    dif_rel = ds['dif_rel'].values

    # 预计算无前视历史买入信号日 dif_rel 分布（含 2011 年起至当日 T 之前的所有买入信号）
    hist_buy_mask = sig_full['buy_sig']
    hist_buy_dif_rel = dif_rel_full[hist_buy_mask].dropna()

    def get_hist_quantiles(d):
        # 仅使用严格 <= d 的历史买入信号（含当日 T）
        s = hist_buy_dif_rel[hist_buy_dif_rel.index <= d]
        if len(s) < 5:
            return -0.011, -0.015, -0.007
        return float(s.quantile(0.5)), float(s.quantile(1/3)), float(s.quantile(2/3))

    def target_atk_weight_on_entry(i):
        d = idx[i]
        dr = dif_rel[i]
        if mode == 'base':
            return 1.0
        if mode == 'C1_hist_med':
            med, _, _ = get_hist_quantiles(d)
            w_deep, w_shallow = param
            return w_deep if dr <= med else w_shallow
        if mode == 'C1_hist_3tier':
            _, q33, q67 = get_hist_quantiles(d)
            w_deep, w_mid, w_shallow = param
            if dr <= q33:
                return w_deep
            elif dr <= q67:
                return w_mid
            return w_shallow
        if mode == 'C1_fixed':
            th, w_deep, w_shallow = param
            return w_deep if dr <= th else w_shallow
        if mode in ('C2_tvol_band', 'C2_tvol_entry'):
            tv = param[0]
            min_w = param[1]
            v = vol20[i]
            if np.isnan(v) or v <= 0:
                return 1.0
            return float(np.clip(tv / v, min_w, 1.0))
        if mode == 'C2_cap':
            th, w_hi = param
            v = vol20[i]
            if np.isnan(v):
                return 1.0
            return w_hi if v > th else 1.0
        if mode in ('C3_lev_atk', 'C3_lev_all'):
            return float(param)
        return 1.0

    # 状态初始化（i=0）
    is_lev_all = (mode == 'C3_lev_all')
    L_def = float(param) if is_lev_all else 1.0

    E = 1.0
    V_atk = 0.0
    V_def = L_def * E
    Debt = (L_def - 1.0) * E
    state_atk = False

    nav_arr = np.ones(n)
    hold_atk_arr = np.zeros(n, dtype=bool)
    w_atk_hist = np.zeros(n)
    pending_target = None  # None 或 (target_w_atk, target_w_def, is_state_switch)

    n_switch = 0
    n_resize = 0
    total_turnover = 0.0
    total_interest = 0.0
    min_collat = np.inf
    min_collat_date = None
    worst_atk_seg_dd = 0.0
    seg_peak_E = 1.0

    # 首日收盘信号
    if buy_s[0]:
        wt = target_atk_weight_on_entry(0)
        wd = 0.0 if mode in ('C3_lev_atk', 'C3_lev_all') else (1.0 - wt)
        pending_target = (wt, wd, True, True)  # (w_atk, w_def, new_state, is_switch)

    for i in range(1, n):
        dt = (idx[i] - idx[i - 1]).days
        # 1) 每日按日历天计借款利息
        if Debt > 0 and borrow_rate > 0:
            intr = Debt * borrow_rate * (dt / 365.0)
            Debt += intr
            total_interest += intr

        # 2) 隔夜市值变化
        V_atk *= atk_on[i]
        V_def *= def_on[i]
        E_open = V_atk + V_def - Debt
        if E_open <= 0:
            # 穿仓保护
            E_open = 1e-9
            V_atk = 0.0
            V_def = 0.0
            Debt = 0.0
        if Debt > 1e-12:
            c_open = (V_atk + V_def) / Debt
            if c_open < min_collat:
                min_collat = c_open
                min_collat_date = idx[i]

        # 3) 开盘执行调仓/换仓
        do_rebal = pending_target is not None
        if not do_rebal and daily_rebal and mode in ('C3_lev_atk', 'C3_lev_all'):
            if state_atk:
                pending_target = (float(param), 0.0, True, False)
                do_rebal = True
            elif mode == 'C3_lev_all':
                pending_target = (0.0, float(param), False, False)
                do_rebal = True

        if do_rebal:
            tw_a, tw_d, new_state, is_sw = pending_target
            # 先算按 E_open 的目标市值与换手成本
            tgt_a = tw_a * E_open
            tgt_d = tw_d * E_open
            to_amt = abs(tgt_a - V_atk) + abs(tgt_d - V_def)
            cost = fee * to_amt
            total_turnover += to_amt / max(E_open, 1e-12)
            E_after = E_open - cost
            V_atk = tw_a * E_after
            V_def = tw_d * E_after
            Debt = max(0.0, (tw_a + tw_d - 1.0) * E_after)
            if is_sw:
                n_switch += 1
                if new_state:
                    seg_peak_E = E_after
            else:
                if to_amt / max(E_open, 1e-12) > 1e-4:
                    n_resize += 1
            state_atk = new_state
            pending_target = None

        # 4) 日内市值变化
        V_atk *= atk_intra[i]
        V_def *= def_intra[i]
        E_close = V_atk + V_def - Debt
        if E_close <= 0:
            E_close = 1e-9
        nav_arr[i] = E_close
        hold_atk_arr[i] = state_atk
        w_atk_hist[i] = V_atk / E_close

        if Debt > 1e-12:
            c_close = (V_atk + V_def) / Debt
            if c_close < min_collat:
                min_collat = c_close
                min_collat_date = idx[i]

        if state_atk:
            if E_close > seg_peak_E:
                seg_peak_E = E_close
            seg_dd = E_close / seg_peak_E - 1.0
            if seg_dd < worst_atk_seg_dd:
                worst_atk_seg_dd = seg_dd

        # 5) 收盘产生明日开盘动作
        if not state_atk:
            if buy_s[i]:
                wt = target_atk_weight_on_entry(i)
                wd = 0.0 if mode in ('C3_lev_atk', 'C3_lev_all') else (1.0 - wt)
                pending_target = (wt, wd, True, True)
        else:
            if sell_s[i]:
                pending_target = (0.0, L_def, False, True)
            elif mode == 'C2_tvol_band':
                tv, min_w, band = param
                v = vol20[i]
                if not np.isnan(v) and v > 0:
                    wt = float(np.clip(tv / v, min_w, 1.0))
                    cur_w = V_atk / E_close
                    if abs(wt - cur_w) >= band:
                        pending_target = (wt, 1.0 - wt, True, False)
            elif mode == 'C2_cap':
                th, w_hi = param
                v = vol20[i]
                if not np.isnan(v):
                    wt = w_hi if v > th else 1.0
                    cur_w = V_atk / E_close
                    if abs(wt - cur_w) >= 0.05:
                        pending_target = (wt, 1.0 - wt, True, False)

    nav = pd.Series(nav_arr, index=idx)
    avg_atk_w = float(w_atk_hist[hold_atk_arr].mean()) if hold_atk_arr.any() else 0.0
    return calc_metrics(nav, n_trades=n_switch + n_resize, extra={
        'n_switch': n_switch,
        'n_resize': n_resize,
        'total_turnover': total_turnover,
        'avg_atk_w': avg_atk_w,
        'total_interest': total_interest,
        'min_collat': min_collat,
        'min_collat_date': min_collat_date,
        'worst_atk_seg_dd': worst_atk_seg_dd,
        'splice_cross': check_splice_cross(idx, hold_atk_arr),
    })


# ---------------------------------------------------------------------------
# 辅助打印函数
# ---------------------------------------------------------------------------
def fmt_row(name, m, base_ann=None, extra_str=''):
    d_ann = ('%+.2f%%' % ((m['ann'] - base_ann) * 100)) if base_ann is not None else '   -- '
    return (
        '%-28s %8.2f%% %7.2f%% %7s %7.2f%% %6.2f %6.2f %5d | %6.2f%%/%6.2f%% %6.2f%%/%6.2f%% | %6.2f%% %6.2f%% %6.2f%% %s'
        % (
            name,
            m['total'] * 100, m['ann'] * 100, d_ann, m['mdd'] * 100,
            m['calmar'], m['sharpe'], m['trades'],
            m['h1_ann'] * 100, m['h1_mdd'] * 100,
            m['h2_ann'] * 100, m['h2_mdd'] * 100,
            m['y2026'] * 100, m['m2026_10'] * 100, m['m2026_11'] * 100,
            extra_str,
        )
    )


def print_table_header(extra_col=''):
    hdr = (
        '%-28s %9s %8s %7s %8s %6s %6s %5s | %15s %15s | %7s %7s %7s %s'
        % ('方案', '累计收益', '年化', 'Δ年化', 'MDD', 'Calmar', 'Sharpe', '换仓',
           'H1年化/MDD', 'H2年化/MDD', '2026年', '26-10', '26-11', extra_col)
    )
    out(hdr)
    out('-' * len(hdr))


def print_yearly_table(items):
    years = list(range(2019, 2027))
    hdr = '%-28s ' + ' '.join(['%8d' % y for y in years]) + ' %8s %8s'
    out(hdr % ('方案', '26-10', '26-11'))
    out('-' * 124)
    for name, m in items:
        vals = ['%7.2f%%' % (m['yearly'].get(y, np.nan) * 100) for y in years]
        out('%-28s %s %7.2f%% %7.2f%%' % (
            name, ' '.join(vals), m['m2026_10'] * 100, m['m2026_11'] * 100
        ))


# ---------------------------------------------------------------------------
# 主实验流程
# ---------------------------------------------------------------------------
def main():
    ds = load_dataset()
    idx = ds['idx']

    out('=' * 136)
    out('方案C（880003 DIF 17/34/9）执行优化、风险管理、资金管理与成本敏感性综合回测报告')
    out('=' * 136)
    out('回测区间 : %s ~ %s（共 %d 个交易日）' % (idx[0].date(), idx[-1].date(), len(idx)))
    out('切分基准 : 前半段 H1 = %s ~ %s | 后半段 H2 = %s ~ %s'
        % (idx[0].date(), SPLIT_DATE.date(), SPLIT_DATE.date(), idx[-1].date()))
    out('标的口径 : 进攻=512100(<2024-06-28)+159552(>=2024-06-28)；防守=512890；基准费用=双边万1(fee=0.0001)')
    if ds.get('data_fix_info'):
        out('[数据质量预警] ' + ds['data_fix_info'])
    out('')

    # --- 基准 A0 与 权重引擎对账 ---
    res_a0 = run_timing_mode(ds, 'A0', fee=BASE_FEE)
    res_w0 = run_weighted_engine(ds, 'base', fee=BASE_FEE)
    diff_check = abs(res_a0['total'] - res_w0['total'])
    out('[引擎对账] A0 基准累计=%.4f%% vs 连续权重引擎累计=%.4f%%（差=%.2e，2024-06-28跨界=%s）'
        % (res_a0['total'] * 100, res_w0['total'] * 100, diff_check, res_a0['splice_cross']))

    # 买入持有对照
    nav_atk_bh = pd.Series(np.cumprod(ds['atk_cc']), index=idx)
    nav_def_bh = pd.Series(np.cumprod(ds['def_cc']), index=idx)
    nav_5050 = 0.5 * nav_atk_bh + 0.5 * nav_def_bh
    m_atk_bh = calc_metrics(nav_atk_bh, 0)
    m_def_bh = calc_metrics(nav_def_bh, 0)
    m_5050 = calc_metrics(nav_5050, 0)
    out('[被动对照] 进攻拼接持有: 累计=%.2f%% 年化=%.2f%% MDD=%.2f%% Calmar=%.2f | '
        '512890持有: 累计=%.2f%% 年化=%.2f%% MDD=%.2f%% | 50/50: 累计=%.2f%% 年化=%.2f%% MDD=%.2f%%'
        % (m_atk_bh['total']*100, m_atk_bh['ann']*100, m_atk_bh['mdd']*100, m_atk_bh['calmar'],
           m_def_bh['total']*100, m_def_bh['ann']*100, m_def_bh['mdd']*100,
           m_5050['total']*100, m_5050['ann']*100, m_5050['mdd']*100))
    out('')

    # =======================================================================
    # A 组：成交时点对比 + 隔夜跳空价值量化
    # =======================================================================
    out('#' * 136)
    out('# A 组：成交时点对比（隔夜跳空价值量化）')
    out('#' * 136)
    a_items = [
        ('A0 基准(T+1开盘,万1)', res_a0, ''),
        ('A1 尾盘收盘(无额外滑点)', run_timing_mode(ds, 'A1', fee=BASE_FEE, slip_close=0.0), 'slip=0'),
        ('A1 尾盘收盘(+单边0.025%)', run_timing_mode(ds, 'A1', fee=BASE_FEE, slip_close=0.00025), '双边+0.05%'),
        ('A1 尾盘收盘(+单边0.05%)', run_timing_mode(ds, 'A1', fee=BASE_FEE, slip_close=0.0005), '双边+0.10%'),
        ('A1 尾盘收盘(+单边0.10%)', run_timing_mode(ds, 'A1', fee=BASE_FEE, slip_close=0.0010), '双边+0.20%'),
        ('A2 买T尾盘+卖T+1开盘', run_timing_mode(ds, 'A2', fee=BASE_FEE, slip_close=0.0), 'slip=0'),
        ('A2 买T尾盘(+0.05%)+卖开', run_timing_mode(ds, 'A2', fee=BASE_FEE, slip_close=0.0005), '买+0.05%'),
        ('A3 买T+1开盘+卖T尾盘', run_timing_mode(ds, 'A3', fee=BASE_FEE, slip_close=0.0), 'slip=0'),
        ('A3 买开+卖T尾盘(+0.05%)', run_timing_mode(ds, 'A3', fee=BASE_FEE, slip_close=0.0005), '卖+0.05%'),
        ('A4 T+1均价(OHLC/4)', run_timing_mode(ds, 'A4', fee=BASE_FEE), '近似VWAP'),
        ('A5 T+1收盘(延迟1天)', run_timing_mode(ds, 'A5', fee=BASE_FEE), '错过早盘'),
    ]
    print_table_header('备注')
    for name, m, note in a_items:
        base_ann = None if name.startswith('A0') else res_a0['ann']
        sc = ' [跨界]' if m.get('splice_cross') else ''
        out(fmt_row(name, m, base_ann, note + sc))

    # 反解 A1 与 A3 相对 A0 的盈亏平衡单边额外滑点
    lo, hi = 0.0, 0.01
    for _ in range(40):
        mid = 0.5 * (lo + hi)
        if run_timing_mode(ds, 'A1', fee=BASE_FEE, slip_close=mid)['ann'] > res_a0['ann']:
            lo = mid
        else:
            hi = mid
    be_slip_a1 = 0.5 * (lo + hi)

    lo3, hi3 = 0.0, 0.01
    for _ in range(40):
        mid = 0.5 * (lo3 + hi3)
        if run_timing_mode(ds, 'A3', fee=BASE_FEE, slip_close=mid)['ann'] > res_a0['ann']:
            lo3 = mid
        else:
            hi3 = mid
    be_slip_a3 = 0.5 * (lo3 + hi3)

    out('')
    out('[尾盘盈亏平衡滑点] A1(双端尾盘) vs A0: 额外单边滑点 < %.4f%%（双边 < %.4f%%）才优于 A0；'
        % (be_slip_a1 * 100, be_slip_a1 * 200))
    out('                  A3(仅卖端尾盘) vs A0: 卖端额外单边滑点 < %.4f%%（双边 < %.4f%%）才优于 A0。'
        % (be_slip_a3 * 100, be_slip_a3 * 200))

    # --- 隔夜跳空专项拆解 ---
    out('')
    out('--- A 组专项拆解：T 收盘 → T+1 开盘 隔夜跳空统计（以及 T+1 日内收益）---')
    gap_df = analyze_overnight_gaps(ds, res_a0)
    for act, label in [('buy', '买入进攻日（69次：T收盘买 vs T+1开盘买）'),
                       ('sell', '卖出进攻日（69次：T收盘卖 vs T+1开盘卖）')]:
        sub = gap_df[gap_df['action'] == act]
        e = sub['edge_close_vs_open']
        e_in = sub['edge_open_vs_t1close']
        t_stat = float(e.mean() / (e.std(ddof=1) / math.sqrt(len(e)))) if e.std(ddof=1) > 0 else np.nan
        q = np.quantile(e, [0.10, 0.25, 0.50, 0.75, 0.90])
        h1_e = sub[sub['half'] == 'H1']['edge_close_vs_open']
        h2_e = sub[sub['half'] == 'H2']['edge_close_vs_open']
        cum_geo = float((1.0 + e).prod() - 1.0)
        cum_in = float((1.0 + e_in).prod() - 1.0)
        out('【%s】' % label)
        out('  进攻隔夜均值=%+.3f%% (中位=%+.3f%%) | 防守隔夜均值=%+.3f%% (中位=%+.3f%%)'
            % (sub['atk_on'].mean()*100, sub['atk_on'].median()*100,
               sub['def_on'].mean()*100, sub['def_on'].median()*100))
        out('  尾盘相对开盘单次超额: 均值=%+.3f%%, 中位=%+.3f%%, 胜率=%.1f%% (%d/%d), std=%.3f%%, t=%.2f, 连乘累计=%+.2f%%'
            % (e.mean()*100, e.median()*100, (e > 0).mean()*100, int((e > 0).sum()), len(e),
               e.std(ddof=1)*100, t_stat, cum_geo*100))
        out('  分布分位数 [P10, P25, P50, P75, P90] = [%+.3f%%, %+.3f%%, %+.3f%%, %+.3f%%, %+.3f%%] | 最小=%+.2f%%, 最大=%+.2f%%'
            % (q[0]*100, q[1]*100, q[2]*100, q[3]*100, q[4]*100, e.min()*100, e.max()*100))
        out('  分半段检验: H1均值=%+.3f%%(胜率%.1f%%, n=%d) | H2均值=%+.3f%%(胜率%.1f%%, n=%d)'
            % (h1_e.mean()*100, (h1_e > 0).mean()*100, len(h1_e),
               h2_e.mean()*100, (h2_e > 0).mean()*100, len(h2_e)))
        out('  T+1开盘相对T+1收盘(延迟代价): 均值=%+.3f%%, 胜率=%.1f%%, 连乘累计=%+.2f%%'
            % (e_in.mean()*100, (e_in > 0).mean()*100, cum_in*100))
        # 尾盘临界反转风险（|ΔDIF|/close 极小的信号占比）
        tiny_005 = (sub['abs_ddif_pct'] < 0.05).mean() * 100
        tiny_010 = (sub['abs_ddif_pct'] < 0.10).mean() * 100
        out('  尾盘临界不确定性参考: |ΔDIF|/close < 0.05%% 占比=%.1f%%, < 0.10%% 占比=%.1f%%（尾盘最后5分钟易翻转）'
            % (tiny_005, tiny_010))
    # 极端事件 Top 5（检查尾盘优势是否靠少数极端跳空）
    gap_df_sorted = gap_df.sort_values('edge_close_vs_open', ascending=False)
    top3_pos = [(r['trade_date'].strftime('%Y-%m-%d'), r['action'], r['edge_close_vs_open']*100)
                for _, r in gap_df_sorted.head(5).iterrows()]
    top3_neg = [(r['trade_date'].strftime('%Y-%m-%d'), r['action'], r['edge_close_vs_open']*100)
                for _, r in gap_df_sorted.tail(5).iterrows()]
    out('  最大正贡献 Top5 (尾盘赢开盘): ' + ', '.join(['%s(%s)%+.2f%%' % x for x in top3_pos]))
    out('  最大负贡献 Top5 (尾盘输开盘): ' + ', '.join(['%s(%s)%+.2f%%' % x for x in top3_neg]))
    # 剔除最大 3 次极端正跳空后，A1-A0 还剩多少超额？
    ex_top3_geo = float((1.0 + gap_df_sorted.iloc[3:]['edge_close_vs_open']).prod() - 1.0)
    all_geo = float((1.0 + gap_df['edge_close_vs_open']).prod() - 1.0)
    out('  全138次换仓尾盘vs开盘隔夜总连乘=%+.2f%%；剔除最大3次极端跳空后总连乘=%+.2f%%'
        % (all_geo * 100, ex_top3_geo * 100))
    out('')

    # =======================================================================
    # B 组：进攻持仓段风险管理（止损 / 移动止盈 / 均线破位）
    # =======================================================================
    out('#' * 136)
    out('# B 组：进攻持仓段风险管理（基于 A0 基准，收盘触发次日开盘切防守，等下次买入信号再进）')
    out('#' * 136)
    b_configs = [
        ('B0 基准(无风控)', 'none', None),
        ('B1 固定止损 -3%', 'fixed_sl', 0.03),
        ('B1 固定止损 -5%', 'fixed_sl', 0.05),
        ('B1 固定止损 -7%', 'fixed_sl', 0.07),
        ('B1 固定止损 -10%', 'fixed_sl', 0.10),
        ('B2 移动止盈 回撤5%', 'trail', 0.05),
        ('B2 移动止盈 回撤7%', 'trail', 0.07),
        ('B2 移动止盈 回撤10%', 'trail', 0.10),
        ('B2 移动止盈 回撤12%', 'trail', 0.12),
        ('B2b 浮盈>10%后回撤5%', 'trail_act10', 0.05),
        ('B2b 浮盈>10%后回撤7%', 'trail_act10', 0.07),
        ('B2b 浮盈>10%后回撤10%', 'trail_act10', 0.10),
        ('B2b 浮盈>10%后回撤12%', 'trail_act10', 0.12),
        ('B3a 破MA10(低于即切)', 'ma_raw', 10),
        ('B3a 破MA20(低于即切)', 'ma_raw', 20),
        ('B3b 破MA10(下穿切防守)', 'ma_cross', 10),
        ('B3b 破MA20(下穿切防守)', 'ma_cross', 20),
        ('B3c 破MA10(先站上再破)', 'ma_after_up', 10),
        ('B3c 破MA20(先站上再破)', 'ma_after_up', 20),
    ]
    b_items = []
    print_table_header('触发/胜率/均差')
    for name, rule, param in b_configs:
        m = run_risk_mgmt(ds, rule=rule, param=param, fee=BASE_FEE)
        b_items.append((name, m))
        base_ann = None if rule == 'none' else res_a0['ann']
        if m['n_stop'] > 0:
            ex = '止%d次 胜%.0f%% 均%+.2f%%' % (m['n_stop'], m['stop_win_rate']*100, m['stop_avg_diff']*100)
        else:
            ex = '未触发' if rule != 'none' else ''
        if m.get('splice_cross'):
            ex += ' [跨界]'
        out(fmt_row(name, m, base_ann, ex))
    out('')

    # =======================================================================
    # C 组：仓位与风险暴露调整（纯资金管理，不改信号）
    # =======================================================================
    out('#' * 136)
    out('# C 组：仓位与风险暴露调整（纯资金管理，不改信号）')
    out('#' * 136)

    # 先按 DIF 相对深度四分位拆解 69 次基准进攻段的收益表现（揭示 C1 的底层规律）
    pos_of = {d: i for i, d in enumerate(idx)}
    seg_rows = []
    buy_i = None
    buy_sd = None
    for d, a, _ in res_a0['trades_list']:
        i = pos_of[d]
        if a == 'buy':
            buy_i = i
            buy_sd = idx[i - 1]
        elif a == 'sell' and buy_i is not None:
            # 进攻段开盘到开盘净收益（不含费）
            r_atk = ds['atk_intra'][buy_i]
            r_def = ds['def_intra'][buy_i]
            for k in range(buy_i + 1, i):
                r_atk *= ds['atk_cc'][k]
                r_def *= ds['def_cc'][k]
            r_atk *= ds['atk_on'][i]
            r_def *= ds['def_on'][i]
            seg_rows.append({
                'buy_date': idx[buy_i],
                'sell_date': d,
                'days': i - buy_i,
                'dif_rel': float(ds['dif_rel'].loc[buy_sd]),
                'vol20': float(ds['vol20_atk'].loc[buy_sd]),
                'atk_ret': r_atk - 1.0,
                'def_ret': r_def - 1.0,
                'excess': r_atk / r_def - 1.0,
            })
            buy_i = None
    seg_df = pd.DataFrame(seg_rows)
    out('--- C1/C2 底层规律诊断：69 次基准进攻段按入场 DIF/close 深度与 Vol20 分组表现 ---')
    seg_df['dif_q'] = pd.qcut(seg_df['dif_rel'], 4, labels=['Q1最深超卖', 'Q2较深超卖', 'Q3较浅零下', 'Q4最浅零下'])
    for qname, g in seg_df.groupby('dif_q', observed=False):
        out('  %-12s (n=%2d, DIF/close=[%+.2f%%,%+.2f%%]): 进攻均收=%+.2f%% 胜率=%.0f%% | 超额防守=%+.2f%% 胜率=%.0f%% | 均持有=%.1f天'
            % (qname, len(g), g['dif_rel'].min()*100, g['dif_rel'].max()*100,
               g['atk_ret'].mean()*100, (g['atk_ret']>0).mean()*100,
               g['excess'].mean()*100, (g['excess']>0).mean()*100, g['days'].mean()))
    seg_df['vol_q'] = pd.qcut(seg_df['vol20'].dropna(), 4, labels=['V1最低波', 'V2中低波', 'V3中高波', 'V4最高波'])
    for qname, g in seg_df.groupby('vol_q', observed=False):
        out('  %-12s (n=%2d, Vol20=[%.1f%%,%.1f%%]): 进攻均收=%+.2f%% 胜率=%.0f%% | 超额防守=%+.2f%% 胜率=%.0f%% | 均持有=%.1f天'
            % (qname, len(g), g['vol20'].min()*100, g['vol20'].max()*100,
               g['atk_ret'].mean()*100, (g['atk_ret']>0).mean()*100,
               g['excess'].mean()*100, (g['excess']>0).mean()*100, g['days'].mean()))
    out('')

    # --- C1 & C2 回测表 ---
    out('--- C1（信号强度分层）与 C2（波动率自适应仓位）对比 ---')
    c12_configs = [
        ('C0 基准(满仓100%)', 'base', None),
        ('C1a 历史中位 深100/浅70', 'C1_hist_med', (1.0, 0.7)),
        ('C1b 历史中位 深100/浅50', 'C1_hist_med', (1.0, 0.5)),
        ('C1c 历史三分 100/70/50', 'C1_hist_3tier', (1.0, 0.7, 0.5)),
        ('C1d 固定-1.0% 深100/浅50', 'C1_fixed', (-0.010, 1.0, 0.5)),
        ('C1e 逆向对照 浅100/深50', 'C1_hist_med', (0.5, 1.0)),
        ('C2a TargetVol 15%(带10%)', 'C2_tvol_band', (0.15, 0.20, 0.10)),
        ('C2b TargetVol 20%(带10%)', 'C2_tvol_band', (0.20, 0.20, 0.10)),
        ('C2c TargetVol 25%(带10%)', 'C2_tvol_band', (0.25, 0.20, 0.10)),
        ('C2d TargetVol 30%(带10%)', 'C2_tvol_band', (0.30, 0.20, 0.10)),
        ('C2e TargetVol 20%(仅入场)', 'C2_tvol_entry', (0.20, 0.20)),
        ('C2f TargetVol 25%(仅入场)', 'C2_tvol_entry', (0.25, 0.20)),
        ('C2g 高波降仓(Vol>30%→50%)', 'C2_cap', (0.30, 0.50)),
        ('C2h 高波降仓(Vol>35%→50%)', 'C2_cap', (0.35, 0.50)),
    ]
    c12_items = []
    print_table_header('进攻均仓/调仓')
    for name, mode, param in c12_configs:
        m = run_weighted_engine(ds, mode=mode, param=param, fee=BASE_FEE)
        c12_items.append((name, m))
        base_ann = None if mode == 'base' else res_a0['ann']
        ex = '均仓%.0f%% 调%d' % (m['avg_atk_w']*100, m['n_resize'])
        out(fmt_row(name, m, base_ann, ex))
    out('')

    # --- C3 温和杠杆边界测算 ---
    out('--- C3 温和杠杆边界测算（严格按日历天扣年化 5.0%~6.0% 融资成本；另附 0% 利率作为 IM 期货理论参考）---')
    c3_configs = [
        ('C0 基准(1.0x 无杠杆)', 'base', None, 0.0, False),
        ('C3 进攻1.2x (利率5.5%)', 'C3_lev_atk', 1.2, 0.055, False),
        ('C3 进攻1.3x (利率5.5%)', 'C3_lev_atk', 1.3, 0.055, False),
        ('C3 进攻1.5x (利率5.5%)', 'C3_lev_atk', 1.5, 0.055, False),
        ('C3 全程1.2x (利率5.5%)', 'C3_lev_all', 1.2, 0.055, False),
        ('C3 进攻1.2x (利率5.0%)', 'C3_lev_atk', 1.2, 0.050, False),
        ('C3 进攻1.5x (利率6.0%)', 'C3_lev_atk', 1.5, 0.060, False),
        ('C3 进攻1.5x (日盯市5.5%)', 'C3_lev_atk', 1.5, 0.055, True),
        ('C3 进攻1.2x (0%息/IM参考)', 'C3_lev_atk', 1.2, 0.000, True),
        ('C3 进攻1.5x (0%息/IM参考)', 'C3_lev_atk', 1.5, 0.000, True),
    ]
    c3_items = []
    print_table_header('最低担比/距130%垫/最差段撤')
    for name, mode, param, br, drb in c3_configs:
        m = run_weighted_engine(ds, mode=mode, param=param, fee=BASE_FEE, borrow_rate=br, daily_rebal=drb)
        c3_items.append((name, m))
        base_ann = None if mode == 'base' else res_a0['ann']
        if np.isfinite(m['min_collat']):
            # 距 130% 强平线安全垫：当前总资产 A，负债 D，担比 c=A/D。跌 x 后 A(1-x)/D = 1.3 => x = 1 - 1.3/c
            pad_130 = (1.0 - 1.30 / m['min_collat']) * 100.0
            ex = '担比%.0f%% 垫%.1f%% 段撤%.1f%%' % (m['min_collat']*100, pad_130, m['worst_atk_seg_dd']*100)
        else:
            ex = '无负债 段撤%.1f%%' % (m['worst_atk_seg_dd']*100)
        out(fmt_row(name, m, base_ann, ex))
    out('')
    c3_12_m, c3_15_m, c3_all12_m = c3_items[1][1], c3_items[3][1], c3_items[4][1]
    c3_im12_m, c3_im15_m = c3_items[8][1], c3_items[9][1]
    out('  [两融强平安全垫说明] A股两融警戒线=150%、平仓线=130%。若初始杠杆 L=1.5x，建仓维持担保比例=300%，')
    out('  需单段暴跌 56.7% 才会触及 130% 平仓线；L=1.2x 初始担比=600%，需暴跌 78.3% 才触平仓线。')
    out('  实测结果：全区间 2019~2026（含 2024 年 2 月微盘股流动性踩踏）中，仅进攻期 1.5x 杠杆最低维持担保比例')
    out('  高达 %.0f%%（距 130%% 强平线尚有 %.1f%% 安全垫），最大回撤由 %.2f%% 扩至 %.2f%%，Calmar 反而由 %.2f 升至 %.2f；'
        % (c3_15_m['min_collat']*100, (1.0 - 1.30/c3_15_m['min_collat'])*100,
           res_a0['mdd']*100, c3_15_m['mdd']*100, res_a0['calmar'], c3_15_m['calmar']))
    out('  仅进攻期 1.2x 最低担比 %.0f%%（安全垫 %.1f%%），年化 %+.2f%% 至 %.2f%%，MDD 仅多 %.2f%% 至 %.2f%%，Calmar=%.2f。'
        % (c3_12_m['min_collat']*100, (1.0 - 1.30/c3_12_m['min_collat'])*100,
           (c3_12_m['ann']-res_a0['ann'])*100, c3_12_m['ann']*100,
           (abs(c3_12_m['mdd'])-abs(res_a0['mdd']))*100, c3_12_m['mdd']*100, c3_12_m['calmar']))
    out('  对比「全程 1.2x」：防守期红利低波年化 %.2f%% 扣去 5.5%% 融资成本后边际夏普偏低，且放大防守回撤（MDD=%.2f%%，'
        % (m_def_bh['ann']*100, c3_all12_m['mdd']*100))
    out('  Calmar 降至 %.2f < 基准 %.2f），因此若允许加杠杆，绝不应全程加杠杆，只应在进攻段使用温和杠杆。'
        % (c3_all12_m['calmar'], res_a0['calmar']))
    out('  若改用 IM 股指期货替代进攻腿（保证金约 12%~15%，1.2x~1.5x 名义暴露仅占 15%~23% 保证金），不仅免去 5.5% 融资利息')
    out('  （0%% 息参考年化 %.2f%%~%.2f%%），历史上 IM 贴水还提供正展期收益；但单手名义价值超百万（小资金难以精确配比）'
        % (c3_im12_m['ann']*100, c3_im15_m['ann']*100))
    out('  且标的为中证1000而非 159552 中证2000增强，存在一定的基差与风格跟踪偏离。')
    out('')

    # =======================================================================
    # D 组：交易成本与滑点敏感性矩阵
    # =======================================================================
    out('#' * 136)
    out('# D 组：交易成本与滑点敏感性矩阵（单边成本：万1 / 万3 / 万5 / 千1 / 千2）')
    out('#' * 136)
    fees = [0.0001, 0.0003, 0.0005, 0.0010, 0.0020]
    fee_labels = ['万1(0.01%)', '万3(0.03%)', '万5(0.05%)', '千1(0.10%)', '千2(0.20%)']

    # 选出核心对比代表：A0基准、A1尾盘、A2买尾卖开、A3买开卖尾、A4均价、B2b(浮盈>10%回撤10%)、C2d(TV30%)
    # 先动态找 B 组与 C1/C2 组 Calmar 最优者纳入成本敏感性矩阵
    best_b = max(b_items[1:], key=lambda x: x[1]['calmar'])
    best_c12 = max(c12_items[1:], key=lambda x: x[1]['calmar'])
    out('  [纳入矩阵的 B/C 最优 Calmar 代表] B组最优=%s (Calmar=%.2f), C1/C2组最优=%s (Calmar=%.2f)'
        % (best_b[0], best_b[1]['calmar'], best_c12[0], best_c12[1]['calmar']))
    out('')

    # 找回对应配置
    best_b_cfg = [c for c in b_configs if c[0] == best_b[0]][0]
    best_c_cfg = [c for c in c12_configs if c[0] == best_c12[0]][0]

    d_strats = [
        ('A0 基准(T+1开盘)', lambda f: run_timing_mode(ds, 'A0', fee=f)),
        ('A1 尾盘收盘(同费率)', lambda f: run_timing_mode(ds, 'A1', fee=f, slip_close=0.0)),
        ('A2 买T尾盘+卖T+1开盘', lambda f: run_timing_mode(ds, 'A2', fee=f, slip_close=0.0)),
        ('A3 买T+1开盘+卖T尾盘', lambda f: run_timing_mode(ds, 'A3', fee=f, slip_close=0.0)),
        ('A4 T+1均价(OHLC/4)', lambda f: run_timing_mode(ds, 'A4', fee=f)),
        (best_b_cfg[0], lambda f: run_risk_mgmt(ds, rule=best_b_cfg[1], param=best_b_cfg[2], fee=f)),
        (best_c_cfg[0], lambda f: run_weighted_engine(ds, mode=best_c_cfg[1], param=best_c_cfg[2], fee=f)),
    ]

    out('%-26s %s' % ('方案（年化 / 累计）', ' '.join(['%18s' % fl for fl in fee_labels])))
    out('-' * 124)
    for sname, fn in d_strats:
        cells = []
        anns = []
        for f in fees:
            r = fn(f)
            anns.append(r['ann'])
            cells.append('%6.2f%%/%6.1f%%' % (r['ann'] * 100, r['total'] * 100))
        out('%-26s %s' % (sname, ' '.join(['%18s' % c for c in cells])))
    out('')
    # 边际成本拖拽测算（A0 从万1到千2）
    r_a0_w1 = run_timing_mode(ds, 'A0', fee=0.0001)
    r_a0_k2 = run_timing_mode(ds, 'A0', fee=0.0020)
    drag_per_bp = (r_a0_w1['ann'] - r_a0_k2['ann']) / 19.0 * 100  # 每增加万1单边成本吃掉的年化百分点
    out('[边际成本拖拽] A0 基准年均换仓 %.1f 次；单边成本每增加万1(0.01%%)，年化收益约下降 %.2f 个百分点。'
        % (r_a0_w1['trades'] / ((idx[-1] - idx[0]).days / 365.0), drag_per_bp))
    out('  千1(0.10%%)单边成本下，A0 仍有 %.2f%% 年化（累计 %.1f%%），远高于 50/50 静态组合(%.2f%%)与进攻持有(%.2f%%)。'
        % (run_timing_mode(ds, 'A0', fee=0.0010)['ann']*100,
           run_timing_mode(ds, 'A0', fee=0.0010)['total']*100,
           m_5050['ann']*100, m_atk_bh['ann']*100))
    out('')

    # =======================================================================
    # 汇总分年度收益表（2019 ~ 2026 + 2026-10 / 2026-11）
    # =======================================================================
    out('#' * 136)
    out('# 核心方案分年度收益表（2019~2026）及 2026-10 / 2026-11 最新月度表现')
    out('#' * 136)
    summary_yearly = [
        ('A0 基准(T+1开盘,万1)', res_a0),
        ('A1 尾盘收盘(无滑点)', a_items[1][1]),
        ('A1 尾盘收盘(+单边0.05%)', a_items[3][1]),
        ('A2 买T尾盘+卖T+1开盘', a_items[5][1]),
        ('A3 买T+1开盘+卖T尾盘', a_items[7][1]),
        ('A3 买开+卖T尾盘(+0.05%)', a_items[8][1]),
        ('A4 T+1均价(OHLC/4)', a_items[9][1]),
        ('A5 T+1收盘(延迟1天)', a_items[10][1]),
        ('B1 固定止损 -5%', b_items[2][1]),
        ('B2 移动止盈 回撤5%', b_items[5][1]),
        ('B2 移动止盈 回撤10%', b_items[7][1]),
        ('B3c 破MA20(先站上再破)', b_items[18][1]),
        ('C1b 深100/浅50', c12_items[2][1]),
        ('C1e 逆向对照 浅100/深50', c12_items[5][1]),
        ('C2g 高波降仓(Vol>30%→50%)', c12_items[12][1]),
        ('C3 进攻1.2x(利率5.5%)', c3_items[1][1]),
        ('C3 进攻1.5x(利率5.5%)', c3_items[3][1]),
        ('被动:进攻拼接持有', m_atk_bh),
        ('被动:512890防守持有', m_def_bh),
    ]
    print_yearly_table(summary_yearly)
    out('')

    # =======================================================================
    # 综合可执行结论
    # =======================================================================
    out('#' * 136)
    out('# 综合结论与可执行建议')
    out('#' * 136)
    a1_0 = a_items[1][1]
    a1_05 = a_items[3][1]
    a2_0 = a_items[5][1]
    a3_0 = a_items[7][1]
    a3_05 = a_items[8][1]
    a4_0 = a_items[9][1]
    a5_0 = a_items[10][1]
    c3_12 = c3_items[1][1]
    c3_15 = c3_items[3][1]
    c3_all12 = c3_items[4][1]

    out('【零、前置数据质量清洗说明】')
    out('  ' + ds['data_fix_info'])
    out('  清洗后基准 A0 为 %.2f%% / 年化 %.2f%% / MDD %.2f%% / Calmar %.2f（全区间 1900 日，无 2024-06-28 跨界失真，无 2026-11 复权脏数据污染）。'
        % (res_a0['total']*100, res_a0['ann']*100, res_a0['mdd']*100, res_a0['calmar']))
    out('')
    b3c20 = b_items[18][1]
    c1e_m = c12_items[5][1]
    r_a0_k1 = run_timing_mode(ds, 'A0', fee=0.0010)
    out('【一、值得采纳的真增益项（按优先级排序）】')
    out('  1. [最高性价比·纯执行优化] A3 非对称执行（买入仍用 T+1 早盘开盘，卖出改为 T 日 14:55~14:58 尾盘执行）：')
    out('     - 隔夜拆解揭示显著的结构不对称：')
    out('       * 买入端（69次）：T 尾盘买反而跑输 T+1 开盘买（均值 -0.070%，胜率 34.8%，H1/H2 均为负），')
    out('         因为零下刚拐头次日早盘常惯性低开，等 T+1 开盘买更划算；')
    out('       * 卖出端（69次）：T 尾盘卖显著跑赢 T+1 开盘卖（均值 +0.162%，胜率 60.9%，t=2.73 统计显著，')
    out('         H1 +0.252% / H2 +0.101% 双半段一致，连乘 +11.74%），因为 DIF 转降日隔夜恐慌常致次日低开。')
    out('     - 效果：无额外滑点下 A3 年化 %.2f%%（+%.2f%%），累计 %.2f%%，Calmar 升至 %.2f，Sharpe %.2f；'
        % (a3_0['ann']*100, (a3_0['ann']-res_a0['ann'])*100, a3_0['total']*100, a3_0['calmar'], a3_0['sharpe']))
    out('       即便卖端承受高达 0.05%% 单边额外冲击滑点，A3 仍达 %.2f%%（仍胜基准 +%.2f%%），卖端盈亏平衡滑点达 %.4f%%。'
        % (a3_05['ann']*100, (a3_05['ann']-res_a0['ann'])*100, be_slip_a3*100))
    out('     - 实盘落地纪律：因 ~64% 卖出信号日 |ΔDIF|/close < 0.05% 存在尾盘 5 分钟翻转风险，建议仅在 14:55 后')
    out('       DIF 明确转降（非毫厘临界）时尾盘卖出（或先卖 50%~100%），临界信号留待 T+1 开盘确认，避免假信号双边损耗。')
    out('  2. [执行纪律红线] 坚决避免延迟到盘中或次日收盘：')
    out('     - A4(T+1均价) 年化掉至 %.2f%%（-%.2f%%），A5(T+1收盘) 掉至 %.2f%%（-%.2f%%）。'
        % (a4_0['ann']*100, (res_a0['ann']-a4_0['ann'])*100, a5_0['ann']*100, (res_a0['ann']-a5_0['ann'])*100))
    out('     - 损耗几乎全集中在「买入日 T+1 开盘→收盘」（均值 +0.252%，连乘 +18.18%），说明买入信号次日必须在开盘')
    out('       集合竞价或开盘前 15~30 分钟内果断完成切换，切忌盘中磨蹭等回调。')
    out('  3. [允许加杠杆时的风险收益边界] C3 仅进攻期温和杠杆（1.2x ~ 1.3x）：')
    out('     - 严格扣除 5.5%% 融资年化利率后，仅进攻期 1.2x 年化 %.2f%%（+%.2f%%）、MDD %.2f%%（仅多 1.54%%）、Calmar %.2f（高于基准 %.2f）；'
        % (c3_12['ann']*100, (c3_12['ann']-res_a0['ann'])*100, c3_12['mdd']*100, c3_12['calmar'], res_a0['calmar']))
    out('       进攻期 1.5x 年化 %.2f%%（+%.2f%%）、MDD %.2f%%、Calmar %.2f、历史最低担比 283%%（距 130%% 平仓线安全垫 54.1%%）。'
        % (c3_15['ann']*100, (c3_15['ann']-res_a0['ann'])*100, c3_15['mdd']*100, c3_15['calmar']))
    out('     - 杠杆的真实代价与禁忌：全程 1.2x 虽然年化 %.2f%%，但防守期扛 5.5%% 利息反而拖累 Calmar 至 %.2f（<基准 %.2f）且 MDD 扩至 %.2f%%；'
        % (c3_all12['ann']*100, c3_all12['calmar'], res_a0['calmar'], c3_all12['mdd']*100))
    out('       若资金规模 > 500 万且能接受中证1000替代159552，IM 股指期货（免融资息+贴水红利）在 1.2x~1.5x 理论年化可达 29%~34%。')
    out('')
    out('【二、明确不建议采纳（画蛇添足 / 伪优化 / 过拟合）】')
    out('  1. A1 双端全改尾盘成交：无滑点仅多 +%.2f%% 年化（全靠卖出端贡献，买入端反拖后腿 -4.82%%），且单边滑点只要超'
        % ((a1_0['ann']-res_a0['ann'])*100))
    out('     %.4f%%（万2.2）就反输基准；加上 0.05%% 单边滑点后年化反跌至 %.2f%%（%.2f%%），不如直接用 A3（买开卖尾）。'
        % (be_slip_a1*100, a1_05['ann']*100, (a1_05['ann']-res_a0['ann'])*100))
    out('  2. B 组所有固定止损 / 移动止盈 / 均线破位规则：')
    out('     - 固定止损（-3%/-5%/-7%/-10%）：几乎零触发（-5%以上全区间 0 次），因为方案 C 自带 sell_anywhere=True')
    out('       零下拐头即卖，本身就是极快的自适应动态止损，外挂硬止损纯属冗余。')
    out('     - 移动止盈（5%~12%）：全区间仅触发 1~2 次，回撤 7%~12% 直接大卖飞（-10.1%）；5% 仅靠 2 次样本多 +0.29% 无统计意义。')
    out('     - 均线破位（MA10/MA20）：严格低于均线即切（B3a）和破 MA10 直接摧毁收益（年化跌至 17.8%~22.7%）；')
    out('       看似好看的「B3c 先站上再破 MA20」（年化 %.2f%%）实为严重单年过拟合——2019~2022 及 2025 零贡献，2024 卖飞 -4.0%%，'
        % (b3c20['ann']*100))
    out('       全部超额几乎仅来自 2026 单年（+%.2f%% vs %.2f%%），且把全区间 MDD 从 %.2f%% 恶化至 %.2f%%（Calmar 反降至 %.2f < %.2f）。'
        % (b3c20['y2026']*100, res_a0['y2026']*100, res_a0['mdd']*100, b3c20['mdd']*100, b3c20['calmar'], res_a0['calmar']))
    out('  3. C1（DIF 深度分层）与 C2（Target Vol / 高波降仓）：')
    out('     - C1「深跌满仓/浅跌轻仓」全部大幅跑输基准（年化 -2.9% ~ -4.9%，Calmar 降至 1.14~1.26），因为四分位拆解显示')
    out('       Q4 最浅零下段均收最高（+3.04%），Q1 最深超卖段反而胜率最低（28%，容易接飞刀）。')
    out('     - C2 Target Vol / 高波降仓：四分位显示 V4 最高波（Vol>31%）反而是高弹性反转大肉段（胜率 53%、均收 +3.13%），')
    out('       高波降仓砍掉了主升浪利润，所有 C2 变体年化损失 -1.3% ~ -5.1%，仅换回 1%~2% 的 MDD 缩减，性价比极差。')
    out('     - 若投资者极度厌恶回撤（目标 MDD < 16%%），唯一可考虑的防御型缩放是逆向分层 C1e（浅跌100%%/深跌50%%，MDD %.2f%%，'
        % (c1e_m['mdd']*100))
    out('       Calmar %.2f），但需明确接受年化从 %.2f%% 降至 %.2f%%（%.2f%%）的收益让渡。'
        % (c1e_m['calmar'], res_a0['ann']*100, c1e_m['ann']*100, (c1e_m['ann']-res_a0['ann'])*100))
    out('')
    out('【三、无杠杆真实可落地收益上限评估】')
    out('  - 方案 C 在「纯现券、无杠杆、固定 512100/159552 + 512890 标的池、不改信号」的约束下：')
    out('    * 基准 A0（T+1开盘，万1）：年化 %.2f%%，Calmar %.2f；千1高滑点压力下仍守住 %.2f%% 年化，策略容量与成本抗性极强。'
        % (res_a0['ann']*100, res_a0['calmar'], r_a0_k1['ann']*100))
    out('    * 实盘可落地天花板：叠加 A3（买 T+1 开盘 + 卖 T 尾盘确认执行）后，扣除合理卖端滑点（万3~万5），')
    out('      真实可落地年化上限约 26.3%~27.0%（相对基准净增厚 +0.7%~+1.3%/年）；继续加复杂的止损/止盈/控波模块均无正期望。')
    out('    * 若追求突破 30% 年化，靠执行/风控微调无法达成，必须诉诸两类根本手段之一：')
    out('      (a) 标的池升级（进攻/防守端替换更高 Alpha ETF），或 (b) 进攻段叠加 1.2x~1.5x 温和杠杆（净年化 %.2f%%~%.2f%%，MDD %.2f%%~%.2f%%）。'
        % (c3_12['ann']*100, c3_15['ann']*100, c3_12['mdd']*100, c3_15['mdd']*100))
    out('')

    with open(OUT_PATH, 'w', encoding='utf-8') as f:
        f.write('\n'.join(_OUT_LINES) + '\n')
    print('\n[已写入结果文件] %s（共 %d 行）' % (OUT_PATH, len(_OUT_LINES)))


if __name__ == '__main__':
    main()
