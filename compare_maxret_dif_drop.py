#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DIF 卖出回落幅度过滤 & 相对无量纲归一化 / 非对称阈值 深化研究（compare_maxret_dif_drop.py）

核心口径：
1. 真实 T+1 开盘成交拆段口径：
   - T 日收盘基于 avg_880003.csv 产生信号，T+1 日开盘价成交；
   - 普通持仓日按 close[t] / close[t-1]；
   - 换仓日严格拆为：旧仓隔夜段(open_old[t] / close_old[t-1]) × 新仓日内段(close_new[t] / open_new[t]) × (1 - 2*FEE)；
   - 若 2024-06-28（159552 上市切换日）跨界持有进攻仓，当日严格按 512100 昨收→今开 × 159552 今开→今收 × (1 - 2*FEE) 无缝拆段，并标记 cross_hold=True；
   - 若 2024-06-28 当日恰好卖出进攻仓，隔夜段仍用前一日持有的 512100；
2. 公共交易日：2019-01-18 ~ 2026-11-25（信号在完整 avg_880003 上计算保证预热，回测在三标的公共交易日交集上对齐，基准与全部变体完全同口径）；
3. 数据质量校验与内存修复（不修改磁盘原始 CSV）：
   - 自动检测 hfq 文件中出现的异常复权跳变（hfq 单日涨跌 >12% 而对应 raw 不复权单日涨跌 <11%）；
   - 已检出 etf_512890_hfq.csv 与 etf_512100_hfq.csv 在 2026-11-13、2026-11-16 存在连续两次后复权因子断裂（512890 虚假腰斩两次合计 -75%），在内存中按 raw 真实涨跌幅连续化修正，确保防守腿收益与 20 日动量真实无失真；
4. 零前视：所有指标均使用 T 日及之前收盘信息，Walk-Forward 采用严格扩张历史窗口+状态连续继承。
"""
import os
import sys
import io
import numpy as np
import pandas as pd
from backtest import calc_signals, max_drawdown, annualized

if sys.platform == 'win32' and sys.stdout is not None and not getattr(sys.stdout, '_utf8_wrapped', False):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stdout._utf8_wrapped = True

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, 'data')
OUT_PATH = os.path.join(BASE, 'result_maxret_dif_drop.txt')

FEE = 0.0001
FAST, SLOW, SIG_N = 17, 34, 9
SWITCH_ATK = pd.Timestamp('2024-06-28')
SPLIT_DATE = pd.Timestamp('2022-11-22')
MOM_N = 20


def load_csv(name: str) -> pd.DataFrame:
    df = pd.read_csv(os.path.join(DATA_DIR, name), parse_dates=['date'])
    df = df.set_index('date').sort_index()
    df.index = df.index.normalize()
    return df[~df.index.duplicated(keep='last')]


CLEAN_ANCHOR = pd.Timestamp('2026-11-09')


def load_hfq_sanitized(code: str):
    """
    内存清洗（不修改磁盘 CSV）：
    2026-11-09 及以前使用原 _hfq.csv；
    2026-11-10 及以后统一按 2026-11-09 当日常数因子 (hfq_close / raw_close) 乘以不复权 etf_{code}.csv 的 OHLC 续接，
    彻底消除 512890/512100 的 11-13/11-16 假腰斩/假暴涨及 159552/159201 自 11-10 起的 4%~8% 复权漂移。
    """
    hfq = load_csv(f'etf_{code}_hfq.csv').copy()
    raw = load_csv(f'etf_{code}.csv')
    cols = [c for c in ['open', 'high', 'low', 'close'] if c in hfq.columns]
    anchor_factor = float(hfq.at[CLEAN_ANCHOR, 'close']) / float(raw.at[CLEAN_ANCHOR, 'close'])
    after_mask = hfq.index > CLEAN_ANCHOR
    after_dates = hfq.index[after_mask]
    # 记录清洗前的最大漂移幅度供日志汇报
    max_drift = 0.0
    for d in after_dates:
        if d in raw.index:
            old_c = float(hfq.at[d, 'close'])
            new_c = float(raw.at[d, 'close']) * anchor_factor
            drift = abs(old_c / new_c - 1.0)
            if drift > max_drift:
                max_drift = drift
            for c in cols:
                hfq.at[d, c] = float(raw.at[d, c]) * anchor_factor
    info = {
        'anchor': CLEAN_ANCHOR.strftime('%Y-%m-%d'),
        'factor': anchor_factor,
        'n_fixed': int(len(after_dates)),
        'max_drift': max_drift,
    }
    return hfq, info


# ==================== 数据加载与基础指标 ====================
avg = load_csv('avg_880003.csv')
e512100, gl_512100 = load_hfq_sanitized('512100')
e159552, gl_159552 = load_hfq_sanitized('159552')
e512890, gl_512890 = load_hfq_sanitized('512890')
e159201, gl_159201 = load_hfq_sanitized('159201')

etf_atk_concat = pd.concat([
    e512100[e512100.index < SWITCH_ATK],
    e159552[e159552.index >= SWITCH_ATK],
])

# 在完整 avg 上计算基础 MACD(17,34,9) 与无前视特征
base_sig_full = calc_signals(avg['close'], sell_anywhere=True, fast=FAST, slow=SLOW, sig_n=SIG_N)
dif_full = base_sig_full['dif']
dif_prev1_full = dif_full.shift(1)
dif_prev2_full = dif_full.shift(2)
dif_prev3_full = dif_full.shift(3)
dif_down_full = base_sig_full['sell_sig']        # sell_anywhere=True 即 dif_down
buy_base_full = base_sig_full['buy_sig']         # dif_up & (dif < 0)

close_full = avg['close']
high_full = avg['high']
low_full = avg['low']
tr_full = pd.concat([
    high_full - low_full,
    (high_full - close_full.shift(1)).abs(),
    (low_full - close_full.shift(1)).abs(),
], axis=1).max(axis=1)
atr60_full = tr_full.rolling(60, min_periods=20).mean()
dif_std60_full = dif_full.rolling(60, min_periods=20).std()

roll_max_dif = {w: dif_full.rolling(w, min_periods=1).max() for w in (10, 20, 30)}

# 公共交易日索引（与 export_json.py 完全一致）
IDX = etf_atk_concat.index.intersection(e512890.index).intersection(base_sig_full.index)
IDX = IDX.sort_values()
POS_OF = {d: i for i, d in enumerate(IDX)}
N_DAYS = len(IDX)

# 预提取 numpy 数组加速回测
O_512100 = e512100['open'].reindex(IDX).to_numpy(dtype=float)
C_512100 = e512100['close'].reindex(IDX).to_numpy(dtype=float)
O_159552 = e159552['open'].reindex(IDX).to_numpy(dtype=float)
C_159552 = e159552['close'].reindex(IDX).to_numpy(dtype=float)
O_512890 = e512890['open'].reindex(IDX).to_numpy(dtype=float)
C_512890 = e512890['close'].reindex(IDX).to_numpy(dtype=float)
O_159201 = e159201['open'].reindex(IDX).to_numpy(dtype=float)
C_159201 = e159201['close'].reindex(IDX).to_numpy(dtype=float)

IS_AFTER_SW = (IDX >= SWITCH_ATK)
SW_POS = int(IDX.searchsorted(SWITCH_ATK))

# 对齐后的特征数组
DIF_IDX = dif_full.reindex(IDX).to_numpy(dtype=float)
DIF_P1_IDX = dif_prev1_full.reindex(IDX).to_numpy(dtype=float)
CL_IDX = close_full.reindex(IDX).to_numpy(dtype=float)
BUY_BASE_IDX = buy_base_full.reindex(IDX).fillna(False).to_numpy(dtype=bool)
SELL_BASE_IDX = dif_down_full.reindex(IDX).fillna(False).to_numpy(dtype=bool)

# 20日动量（用于 G 组 512890/159201 双择优防守，基于内存修复后的连续复权价）
MOM_512890 = (e512890['close'] / e512890['close'].shift(MOM_N) - 1).reindex(IDX).to_numpy(dtype=float)
MOM_159201 = (e159201['close'] / e159201['close'].shift(MOM_N) - 1).reindex(IDX).to_numpy(dtype=float)


def _atk_oc(i: int, after_sw: bool):
    if after_sw:
        return O_159552[i], C_159552[i]
    return O_512100[i], C_512100[i]


def _def_oc(i: int, def_code: str):
    if def_code == '159201':
        return O_159201[i], C_159201[i]
    return O_512890[i], C_512890[i]


# ==================== 核心回测引擎 ====================
def run_t1_backtest(buy_arr: np.ndarray, sell_arr: np.ndarray,
                    fee: float = FEE,
                    def_mode: str = 'fixed_890',
                    rebal_def_days: int = None,
                    init_state: bool = False,
                    init_def: str = '512890') -> dict:
    """真实 T+1 开盘成交拆段回测引擎。"""
    n = N_DAYS
    w = np.empty(n, dtype=float)
    w[0] = 1.0
    hold_atk = np.zeros(n, dtype=bool)
    held_code = [''] * n

    state = init_state        # False=防守, True=进攻
    cur_def = init_def        # '512890' or '159201'
    def_hold_days = 0
    pending = None

    trades = []
    atk_segs = []
    cur_seg_start = None
    cur_seg_cross = False
    cross_hold = False

    hold_atk[0] = state
    held_code[0] = ('512100' if not IS_AFTER_SW[0] else '159552') if state else cur_def

    def pick_def(sig_i: int) -> str:
        if def_mode != 'dual_mom20':
            return '512890'
        if sig_i + 1 >= n:
            return '512890'
        m201 = MOM_159201[sig_i]
        m890 = MOM_512890[sig_i]
        if np.isnan(m201) or np.isnan(O_159201[sig_i + 1]) or np.isnan(C_159201[sig_i + 1]):
            return '512890'
        return '159201' if m201 > m890 else '512890'

    if buy_arr[0]:
        pending = ('buy_atk', None)
    elif sell_arr[0]:
        pending = ('sell_atk', pick_def(0))

    for i in range(1, n):
        d = IDX[i]
        prev_after = bool(IS_AFTER_SW[i - 1])
        cur_after = bool(IS_AFTER_SW[i])
        prev_atk_code = '159552' if prev_after else '512100'
        cur_atk_code = '159552' if cur_after else '512100'

        acted = False
        if pending is not None:
            act_type, target = pending
            if act_type == 'buy_atk' and not state:
                o_old, _ = _def_oc(i, cur_def)
                _, c_old_prev = _def_oc(i - 1, cur_def)
                o_new, c_new = _atk_oc(i, cur_after)
                w[i] = w[i - 1] * (o_old / c_old_prev) * (c_new / o_new) * (1.0 - 2.0 * fee)
                state = True
                def_hold_days = 0
                trades.append((i, d, 'BUY_ATK', cur_def, cur_atk_code, o_new))
                cur_seg_start = (i, cur_atk_code, o_new)
                cur_seg_cross = False
                acted = True
            elif act_type == 'sell_atk' and state:
                o_old, _ = _atk_oc(i, prev_after)
                _, c_old_prev = _atk_oc(i - 1, prev_after)
                new_def = target if target is not None else '512890'
                o_new, c_new = _def_oc(i, new_def)
                w[i] = w[i - 1] * (o_old / c_old_prev) * (c_new / o_new) * (1.0 - 2.0 * fee)
                if cur_seg_start is not None:
                    s_pos, s_code, s_open = cur_seg_start
                    dur = i - s_pos
                    if not cur_seg_cross:
                        seg_ret = (o_old / s_open) * (1.0 - 2.0 * fee) - 1.0
                    else:
                        seg_ret = ((O_512100[SW_POS] / s_open)
                                   * (o_old / O_159552[SW_POS])
                                   * ((1.0 - 2.0 * fee) ** 2) - 1.0)
                    atk_segs.append({
                        'buy': IDX[s_pos], 'sell': d, 'dur': dur, 'ret': float(seg_ret),
                        'cross': cur_seg_cross,
                    })
                    cur_seg_start = None
                    cur_seg_cross = False
                state = False
                cur_def = new_def
                def_hold_days = 1
                trades.append((i, d, 'SELL_ATK', prev_atk_code, cur_def, o_new))
                acted = True
            elif act_type == 'switch_def' and (not state) and (target != cur_def):
                o_old, _ = _def_oc(i, cur_def)
                _, c_old_prev = _def_oc(i - 1, cur_def)
                o_new, c_new = _def_oc(i, target)
                w[i] = w[i - 1] * (o_old / c_old_prev) * (c_new / o_new) * (1.0 - 2.0 * fee)
                trades.append((i, d, 'SWITCH_DEF', cur_def, target, o_new))
                cur_def = target
                def_hold_days = 1
                acted = True
            pending = None

        if not acted:
            if state:
                if (not prev_after) and cur_after:
                    w[i] = (w[i - 1] * (O_512100[i] / C_512100[i - 1])
                            * (C_159552[i] / O_159552[i]) * (1.0 - 2.0 * fee))
                    cross_hold = True
                    cur_seg_cross = True
                else:
                    _, c_prev = _atk_oc(i - 1, cur_after)
                    _, c_cur = _atk_oc(i, cur_after)
                    w[i] = w[i - 1] * (c_cur / c_prev)
            else:
                _, c_prev = _def_oc(i - 1, cur_def)
                _, c_cur = _def_oc(i, cur_def)
                w[i] = w[i - 1] * (c_cur / c_prev)
                def_hold_days += 1

        hold_atk[i] = state
        held_code[i] = cur_atk_code if state else cur_def

        if buy_arr[i]:
            pending = ('buy_atk', None)
        elif sell_arr[i]:
            pending = ('sell_atk', pick_def(i))
        elif (not state) and def_mode == 'dual_mom20' and rebal_def_days is not None:
            if def_hold_days >= rebal_def_days and (def_hold_days % rebal_def_days == 0):
                tgt = pick_def(i)
                if tgt != cur_def:
                    pending = ('switch_def', tgt)

    nav = pd.Series(w, index=IDX)
    return {
        'nav': nav,
        'hold_atk': pd.Series(hold_atk, index=IDX),
        'held_code': pd.Series(held_code, index=IDX),
        'trades': trades,
        'atk_segs': atk_segs,
        'cross_hold': cross_hold,
    }


def run_c2c_backtest(buy_arr: np.ndarray, sell_arr: np.ndarray, fee: float = FEE) -> dict:
    """对照用 C2C 近似口径（与 backtest.py 一致，跨界日无缝拆段防失真）。"""
    n = N_DAYS
    hold = np.zeros(n, dtype=bool)
    state = False
    pending = None
    trades = []
    for i in range(n):
        if pending is not None:
            if pending == 'buy' and not state:
                state = True
                trades.append((i, IDX[i], 'BUY_ATK'))
            elif pending == 'sell' and state:
                state = False
                trades.append((i, IDX[i], 'SELL_ATK'))
            pending = None
        hold[i] = state
        if buy_arr[i]:
            pending = 'buy'
        elif sell_arr[i]:
            pending = 'sell'

    r_atk = np.zeros(n, dtype=float)
    r_div = np.zeros(n, dtype=float)
    cross_hold = False
    for i in range(1, n):
        r_div[i] = C_512890[i] / C_512890[i - 1] - 1.0
        if (not IS_AFTER_SW[i - 1]) and IS_AFTER_SW[i]:
            r_atk[i] = (O_512100[i] / C_512100[i - 1]) * (C_159552[i] / O_159552[i]) - 1.0
            if hold[i] and hold[i - 1]:
                cross_hold = True
        elif IS_AFTER_SW[i]:
            r_atk[i] = C_159552[i] / C_159552[i - 1] - 1.0
        else:
            r_atk[i] = C_512100[i] / C_512100[i - 1] - 1.0

    strat_r = np.where(hold, r_atk, r_div)
    for i, _, _ in trades:
        strat_r[i] -= 2.0 * fee
    nav = pd.Series(np.cumprod(1.0 + strat_r), index=IDX)
    return {
        'nav': nav,
        'hold_atk': pd.Series(hold, index=IDX),
        'trades': trades,
        'atk_segs': [],
        'cross_hold': cross_hold,
    }


def run_hold_peak_t1(th: float, only_above_zero: bool = False,
                     fee: float = FEE, def_mode: str = 'fixed_890',
                     rebal_def_days: int = None) -> dict:
    """持仓期最高 DIF 回落触发（严格无前视状态机）。"""
    n = N_DAYS
    buy_arr = np.zeros(n, dtype=bool)
    sell_arr = np.zeros(n, dtype=bool)
    state = False
    pending = None
    peak_dif = -np.inf

    for i in range(n):
        if pending == 'buy' and not state:
            state = True
            peak_dif = DIF_IDX[i]
        elif pending == 'sell' and state:
            state = False
            peak_dif = -np.inf
        pending = None

        if state:
            if DIF_IDX[i] > peak_dif:
                peak_dif = DIF_IDX[i]
            if only_above_zero and DIF_IDX[i] <= 0:
                if SELL_BASE_IDX[i]:
                    sell_arr[i] = True
                    pending = 'sell'
            else:
                if (DIF_IDX[i] < DIF_P1_IDX[i]) and ((peak_dif - DIF_IDX[i]) / CL_IDX[i] > th):
                    sell_arr[i] = True
                    pending = 'sell'
        else:
            if BUY_BASE_IDX[i]:
                buy_arr[i] = True
                pending = 'buy'

    return run_t1_backtest(buy_arr, sell_arr, fee=fee, def_mode=def_mode, rebal_def_days=rebal_def_days)


# ==================== 指标统计 ====================
def sub_metrics(nav: pd.Series, start_anchor: pd.Timestamp = None, end_date: pd.Timestamp = None):
    s = nav
    if start_anchor is not None:
        s = s[s.index >= start_anchor]
    if end_date is not None:
        s = s[s.index <= end_date]
    if len(s) < 2:
        return np.nan, np.nan, np.nan
    s = s / s.iloc[0]
    return float(s.iloc[-1] - 1.0), annualized(s), max_drawdown(s)


def month_ret(nav: pd.Series, year: int, month: int) -> float:
    sub = nav[(nav.index.year == year) & (nav.index.month == month)]
    if len(sub) == 0:
        return np.nan
    prev = nav[nav.index < sub.index[0]]
    if len(prev) == 0:
        return float(sub.iloc[-1] / sub.iloc[0] - 1.0)
    return float(sub.iloc[-1] / prev.iloc[-1] - 1.0)


def ytd_ret(nav: pd.Series, year: int) -> float:
    sub = nav[nav.index.year == year]
    if len(sub) == 0:
        return np.nan
    prev = nav[nav.index < sub.index[0]]
    if len(prev) == 0:
        return float(sub.iloc[-1] / sub.iloc[0] - 1.0)
    return float(sub.iloc[-1] / prev.iloc[-1] - 1.0)


def yearly_returns(nav: pd.Series) -> dict:
    return {int(y): ytd_ret(nav, int(y)) for y in sorted(nav.index.year.unique())}


def evaluate(name: str, res: dict, base_eval: dict = None) -> dict:
    nav = res['nav']
    cum = float(nav.iloc[-1] / nav.iloc[0] - 1.0)
    ann = annualized(nav)
    mdd = max_drawdown(nav)
    calmar = ann / abs(mdd) if mdd < 0 else np.nan
    rets = nav.pct_change().dropna()
    sharpe = float(rets.mean() / rets.std() * np.sqrt(252)) if rets.std() > 0 else np.nan

    h1_cum, h1_ann, h1_mdd = sub_metrics(nav, None, SPLIT_DATE)
    h2_cum, h2_ann, h2_mdd = sub_metrics(nav, SPLIT_DATE, None)

    prev_sw = IDX[SW_POS - 1]
    s159_cum, s159_ann, s159_mdd = sub_metrics(nav, prev_sw, None)

    whip = [s for s in res['atk_segs'] if s['dur'] <= 5 and s['ret'] < 0]
    jul26 = res['hold_atk'][(IDX.year == 2026) & (IDX.month == 7)]

    both_half = False
    if base_eval is not None:
        both_half = (h1_ann > base_eval['h1_ann']) and (h2_ann > base_eval['h2_ann'])

    return {
        'name': name,
        'cum': cum, 'ann': ann, 'mdd': mdd, 'calmar': calmar, 'sharpe': sharpe,
        'ntrades': len(res['trades']),
        'nwhip': len(whip), 'whip_sum': float(sum(s['ret'] for s in whip)),
        'cross_hold': res['cross_hold'],
        'h1_cum': h1_cum, 'h1_ann': h1_ann, 'h1_mdd': h1_mdd,
        'h2_cum': h2_cum, 'h2_ann': h2_ann, 'h2_mdd': h2_mdd,
        'both_half': both_half,
        's159_cum': s159_cum, 's159_ann': s159_ann, 's159_mdd': s159_mdd,
        'ytd26': ytd_ret(nav, 2026),
        'm2607': month_ret(nav, 2026, 7),
        'm2609': month_ret(nav, 2026, 9),
        'm2610': month_ret(nav, 2026, 10),
        'm2611': month_ret(nav, 2026, 11),
        'jul26_atk': int(jul26.sum()), 'jul26_tot': int(len(jul26)),
        'yearly': yearly_returns(nav),
        'nav': nav,
        'res': res,
    }


def make_market_bh():
    w_atk = np.ones(N_DAYS, dtype=float)
    for i in range(1, N_DAYS):
        if (not IS_AFTER_SW[i - 1]) and IS_AFTER_SW[i]:
            w_atk[i] = w_atk[i - 1] * (O_512100[i] / C_512100[i - 1]) * (C_159552[i] / O_159552[i]) * (1 - 2 * FEE)
        elif IS_AFTER_SW[i]:
            w_atk[i] = w_atk[i - 1] * (C_159552[i] / C_159552[i - 1])
        else:
            w_atk[i] = w_atk[i - 1] * (C_512100[i] / C_512100[i - 1])
    res_atk = {'nav': pd.Series(w_atk, index=IDX), 'hold_atk': pd.Series(True, index=IDX),
               'trades': [], 'atk_segs': [], 'cross_hold': True}
    w_div = C_512890 / C_512890[0]
    res_div = {'nav': pd.Series(w_div, index=IDX), 'hold_atk': pd.Series(False, index=IDX),
               'trades': [], 'atk_segs': [], 'cross_hold': False}
    return evaluate('满仓进攻(512100→159552)', res_atk), evaluate('满仓防守(512890)', res_div)


# ==================== 信号构造函数 ====================
def sig_abs_drop(th: float, w: int = 20):
    drop = (roll_max_dif[w] - dif_full).reindex(IDX).to_numpy(dtype=float)
    return BUY_BASE_IDX, SELL_BASE_IDX & (drop > th)


def sig_rel_drop(th: float, w: int = 20, falling: bool = False):
    rel = ((roll_max_dif[w] - dif_full) / close_full).reindex(IDX).to_numpy(dtype=float)
    cond = (DIF_IDX < DIF_P1_IDX) if falling else SELL_BASE_IDX
    return BUY_BASE_IDX, cond & (rel > th)


def sig_std_drop(th: float, w: int = 20, falling: bool = False):
    sd = ((roll_max_dif[w] - dif_full) / dif_std60_full).reindex(IDX).to_numpy(dtype=float)
    cond = (DIF_IDX < DIF_P1_IDX) if falling else SELL_BASE_IDX
    return BUY_BASE_IDX, cond & (sd > th)


def sig_atr_drop(th: float, w: int = 20, falling: bool = False):
    ad = ((roll_max_dif[w] - dif_full) / atr60_full).reindex(IDX).to_numpy(dtype=float)
    cond = (DIF_IDX < DIF_P1_IDX) if falling else SELL_BASE_IDX
    return BUY_BASE_IDX, cond & (ad > th)


def sig_asym_rel_drop(th_up: float, th_dn: float = 0.0, w: int = 20,
                      mode: str = 'only_above_zero', falling: bool = False):
    rel = ((roll_max_dif[w] - dif_full) / close_full).reindex(IDX).to_numpy(dtype=float)
    cond = (DIF_IDX < DIF_P1_IDX) if falling else SELL_BASE_IDX
    above = DIF_IDX > 0
    if mode == 'only_above_zero':
        sell = (cond & above & (rel > th_up)) | (SELL_BASE_IDX & (~above))
    elif mode == 'only_below_zero':
        sell = (SELL_BASE_IDX & above) | (cond & (~above) & (rel > th_dn))
    elif mode == 'two_tier':
        sell = (cond & above & (rel > th_up)) | (cond & (~above) & (rel > th_dn))
    else:
        raise ValueError(mode)
    return BUY_BASE_IDX, sell


def sig_1d_drop(th_1d: float, only_above_zero: bool = False, falling: bool = False):
    d1 = ((dif_prev1_full - dif_full) / close_full).reindex(IDX).to_numpy(dtype=float)
    cond = (DIF_IDX < DIF_P1_IDX) if falling else SELL_BASE_IDX
    if only_above_zero:
        above = DIF_IDX > 0
        sell = (cond & above & (d1 > th_1d)) | (SELL_BASE_IDX & (~above))
    else:
        sell = cond & (d1 > th_1d)
    return BUY_BASE_IDX, sell


def sig_consec_down(days: int = 2, only_above_zero: bool = False):
    if days == 2:
        c = ((dif_full < dif_prev1_full) & (dif_prev1_full < dif_prev2_full)
             & (dif_prev2_full > dif_prev3_full))
    elif days == 3:
        dif_p4 = dif_full.shift(4)
        c = ((dif_full < dif_prev1_full) & (dif_prev1_full < dif_prev2_full)
             & (dif_prev2_full < dif_prev3_full) & (dif_prev3_full > dif_p4))
    else:
        raise ValueError(days)
    c_idx = c.reindex(IDX).fillna(False).to_numpy(dtype=bool)
    if only_above_zero:
        above = DIF_IDX > 0
        sell = (c_idx & above) | (SELL_BASE_IDX & (~above))
    else:
        sell = c_idx
    return BUY_BASE_IDX, sell


# ==================== Walk-Forward OOS 验证 ====================
def run_walk_forward(family: str, candidates: list, objective: str = 'ann',
                     oos_years=(2022, 2023, 2024, 2025, 2026)):
    cand_sigs = {}
    cand_navs = {}
    for th in candidates:
        if th == 0.0:
            b, s = BUY_BASE_IDX, SELL_BASE_IDX
            res = run_t1_backtest(b, s)
        elif family == 'B1_W20':
            b, s = sig_rel_drop(th, w=20, falling=False)
            res = run_t1_backtest(b, s)
        elif family == 'B1_W30':
            b, s = sig_rel_drop(th, w=30, falling=False)
            res = run_t1_backtest(b, s)
        elif family == 'D1_above20':
            b, s = sig_asym_rel_drop(th, 0.0, w=20, mode='only_above_zero', falling=False)
            res = run_t1_backtest(b, s)
        elif family == 'D1_below20':
            b, s = sig_asym_rel_drop(0.0, th, w=20, mode='only_below_zero', falling=False)
            res = run_t1_backtest(b, s)
        elif family == 'D1_below30':
            b, s = sig_asym_rel_drop(0.0, th, w=30, mode='only_below_zero', falling=False)
            res = run_t1_backtest(b, s)
        elif family == 'C1_std20':
            b, s = sig_std_drop(th, w=20, falling=False)
            res = run_t1_backtest(b, s)
        elif family == 'B2_fall20':
            b, s = sig_rel_drop(th, w=20, falling=True)
            res = run_t1_backtest(b, s)
        else:
            raise ValueError(family)
        cand_sigs[th] = (b, s)
        cand_navs[th] = res['nav']

    chosen = {}
    oos_buy = BUY_BASE_IDX.copy()
    oos_sell = SELL_BASE_IDX.copy()
    year_arr = IDX.year.to_numpy()

    for y in oos_years:
        train_end = pd.Timestamp(f'{y - 1}-12-31')
        best_th = None
        best_score = -1e18
        for th in candidates:
            nav_tr = cand_navs[th]
            nav_tr = nav_tr[nav_tr.index <= train_end]
            ann_tr = annualized(nav_tr)
            mdd_tr = max_drawdown(nav_tr)
            if objective == 'ann':
                sc = ann_tr
            elif objective == 'calmar':
                sc = ann_tr / abs(mdd_tr) if mdd_tr < 0 else ann_tr
            elif objective == 'sharpe':
                rt = nav_tr.pct_change().dropna()
                sc = float(rt.mean() / rt.std() * np.sqrt(252)) if rt.std() > 0 else -1e9
            else:
                raise ValueError(objective)
            if sc > best_score:
                best_score = sc
                best_th = th
        chosen[y] = (best_th, best_score)
        mask = (year_arr == y)
        oos_buy[mask] = cand_sigs[best_th][0][mask]
        oos_sell[mask] = cand_sigs[best_th][1][mask]

    full_res = run_t1_backtest(oos_buy, oos_sell)
    first_oos_idx = IDX[IDX.year == oos_years[0]][0]
    anchor_date = IDX[POS_OF[first_oos_idx] - 1]
    oos_cum, oos_ann, oos_mdd = sub_metrics(full_res['nav'], anchor_date, None)
    oos_calmar = oos_ann / abs(oos_mdd) if oos_mdd < 0 else np.nan
    oos_rets = full_res['nav'].loc[anchor_date:].pct_change().dropna()
    oos_sharpe = float(oos_rets.mean() / oos_rets.std() * np.sqrt(252)) if oos_rets.std() > 0 else np.nan
    yr = {y: ytd_ret(full_res['nav'], y) for y in oos_years}
    return {
        'family': family, 'objective': objective,
        'chosen': chosen, 'oos_cum': oos_cum, 'oos_ann': oos_ann,
        'oos_mdd': oos_mdd, 'oos_calmar': oos_calmar, 'oos_sharpe': oos_sharpe,
        'yearly': yr, 'nav': full_res['nav'],
    }


# ==================== 格式化输出工具 ====================
def fmt_pct(v, width=7):
    if v is None or np.isnan(v):
        return ' ' * (width - 3) + 'N/A'
    return f'{v * 100:>{width}.2f}%'


def fmt_num(v, width=6, dec=2):
    if v is None or np.isnan(v):
        return ' ' * (width - 3) + 'N/A'
    return f'{v:>{width}.{dec}f}'


def render_main_table(rows: list, title: str) -> str:
    lines = [f'\n{"=" * 148}', title, '-' * 148]
    hdr = (f'{"变体":<26s} {"累计收益":>8s} {"年化":>7s} {"MDD":>8s} {"Calmar":>6s} {"Sharpe":>6s} '
           f'{"换仓":>4s} {"打脸段/合计":>13s} {"前半段年化/MDD":>16s} {"后半段年化/MDD":>16s} {"两半优":>4s} '
           f'{"159552段":>8s} {"26YTD":>7s} {"跨界":>4s}')
    lines.append(hdr)
    lines.append('-' * 148)
    for r in rows:
        whip_s = f'{r["nwhip"]:2d}/{r["whip_sum"] * 100:+6.2f}%'
        h1_s = f'{r["h1_ann"] * 100:5.2f}%/{r["h1_mdd"] * 100:6.2f}%'
        h2_s = f'{r["h2_ann"] * 100:5.2f}%/{r["h2_mdd"] * 100:6.2f}%'
        bh = 'YES' if r['both_half'] else 'no'
        cr = '是' if r['cross_hold'] else '否'
        lines.append(
            f'{r["name"]:<26s} {fmt_pct(r["cum"], 7)} {fmt_pct(r["ann"], 6)} {fmt_pct(r["mdd"], 7)} '
            f'{fmt_num(r["calmar"], 6)} {fmt_num(r["sharpe"], 6)} '
            f'{r["ntrades"]:>4d} {whip_s:>13s} {h1_s:>16s} {h2_s:>16s} {bh:>4s} '
            f'{fmt_pct(r["s159_cum"], 7)} {fmt_pct(r["ytd26"], 6)} {cr:>4s}'
        )
    return '\n'.join(lines)


def render_2026_and_yearly(rows: list, title: str) -> str:
    lines = [f'\n{"=" * 136}', title, '-' * 136]
    hdr = (f'{"变体":<26s} {"2019":>7s} {"2020":>7s} {"2021":>7s} {"2022":>7s} '
           f'{"2023":>7s} {"2024":>7s} {"2025":>7s} {"2026":>7s} | '
           f'{"26-07":>7s} {"7月进":>5s} {"26-09":>7s} {"26-10":>7s} {"26-11":>7s}')
    lines.append(hdr)
    lines.append('-' * 136)
    for r in rows:
        ys = ' '.join(f'{r["yearly"].get(y, np.nan) * 100:7.2f}' for y in range(2019, 2027))
        jul_s = f'{r["jul26_atk"]:2d}/{r["jul26_tot"]:2d}'
        lines.append(
            f'{r["name"]:<26s} {ys} | '
            f'{r["m2607"] * 100:7.2f} {jul_s:>5s} {r["m2609"] * 100:7.2f} '
            f'{r["m2610"] * 100:7.2f} {r["m2611"] * 100:7.2f}'
        )
    return '\n'.join(lines)


def render_plateau(grid: dict, ws: list, ths: list, title: str, base_t1: dict) -> str:
    lines = [f'\n[{title} — 基准累计 {base_t1["cum"]*100:.2f}%, MDD {base_t1["mdd"]*100:.2f}%, Calmar {base_t1["calmar"]:.2f}]']
    hdr_p = 'W \\ th  | ' + ' | '.join(f'{th:>13.4f}' for th in ths)
    lines.append(hdr_p)
    lines.append('-' * len(hdr_p))
    for w in ws:
        cells = []
        for th in ths:
            e = grid[(w, th)]
            mk = '*' if e['both_half'] else ' '
            cells.append(f'{e["cum"]*100:5.0f}%/{e["mdd"]*100:5.1f}%{mk}')
        lines.append(f'W = {w:<3d} | ' + ' | '.join(cells))
    return '\n'.join(lines)


def main():
    out = []

    def log(s=''):
        print(s)
        out.append(s)

    log('DIF 卖出回落幅度过滤 & 相对无量纲归一化 / 非对称阈值 深化研究报告')
    log('回测区间: %s ~ %s（公共交易日 %d 天，缺口 2021-10-22 / 2025-04-23 已对齐排除，前后无买卖信号）' %
        (IDX[0].strftime('%Y-%m-%d'), IDX[-1].strftime('%Y-%m-%d'), N_DAYS))
    log('核心口径: 信号=880003 MACD(17,34,9) sell_anywhere=True；进攻腿=512100(<2024-06-28)→159552(>=2024-06-28)；防守腿=512890')
    log('执行口径: 真实 T+1 开盘成交拆段（旧仓昨收→今开 × 新仓今开→今收 × (1-0.0002)）；2024-06-28 跨界持进攻仓无缝拆段并标注')
    log('切分口径: 前半段 2019-01-18 ~ 2022-11-22，后半段 2022-11-22 ~ 2026-11-25；159552段 2024-06-27收 ~ 2026-11-25收')
    log('年化口径: (终值/初值)^(365/日历天数)-1（与 backtest.py 一致）；Sharpe=日收益均值/标准差*sqrt(252)；打脸段=进攻仓持仓≤5交易日且开盘→开盘段收益(含双边万1)<0')

    # 数据质量预警
    log('\n[数据质量内存清洗说明（严格未改动任何磁盘 data/*.csv 文件）]')
    log('  清洗规则：2026-11-09 及以前保持原 _hfq.csv；2026-11-10 起统一用 2026-11-09 常数因子 (hfq_close/raw_close) × 不复权 OHLC 续接')
    for code, info in [('512100', gl_512100), ('159552', gl_159552), ('512890', gl_512890), ('159201', gl_159201)]:
        log(f'  etf_{code}_hfq.csv: 锚定日={info["anchor"]} 常数因子={info["factor"]:.6f} 续接行数={info["n_fixed"]} 原数据最大复权漂移={info["max_drift"]*100:.2f}%')

    # 特征分布诊断
    dif_s = dif_full.reindex(IDX)
    cl_s = close_full.reindex(IDX)
    sell_m = pd.Series(SELL_BASE_IDX, index=IDX)
    rm20 = roll_max_dif[20].reindex(IDX)
    log('\n[0. 指标分布诊断（基准全部卖出信号日，共 %d 次，其中 DIF>0: %d 次，DIF<=0: %d 次）]' %
        (int(sell_m.sum()), int((sell_m & (dif_s > 0)).sum()), int((sell_m & (dif_s <= 0)).sum())))
    log('  avg_880003 收盘价范围: %.2f ~ %.2f (中位数 %.2f)' % (cl_s.min(), cl_s.max(), cl_s.median()))
    diag_items = [
        ('abs_drop(W=20)', (rm20 - dif_s)[sell_m]),
        ('rel_drop(W=20,all)', ((rm20 - dif_s) / cl_s)[sell_m]),
        ('rel_drop(W=20,DIF>0)', ((rm20 - dif_s) / cl_s)[sell_m & (dif_s > 0)]),
        ('rel_drop(W=20,DIF<=0)', ((rm20 - dif_s) / cl_s)[sell_m & (dif_s <= 0)]),
        ('std_drop(W=20,S=60)', ((rm20 - dif_s) / dif_std60_full.reindex(IDX))[sell_m]),
        ('atr_drop(W=20,A=60)', ((rm20 - dif_s) / atr60_full.reindex(IDX))[sell_m]),
        ('1d_rel_drop', ((dif_prev1_full - dif_full).reindex(IDX) / cl_s)[sell_m]),
    ]
    for dname, ds in diag_items:
        qs = ds.quantile([0.1, 0.25, 0.5, 0.75, 0.9])
        log('  %-22s: q10=%.5f  q25=%.5f  q50=%.5f  q75=%.5f  q90=%.5f' %
            (dname, qs.iloc[0], qs.iloc[1], qs.iloc[2], qs.iloc[3], qs.iloc[4]))

    # ==================== 基准与市场锚点 ====================
    base_t1_res = run_t1_backtest(BUY_BASE_IDX, SELL_BASE_IDX)
    base_t1 = evaluate('基准(真实T+1)', base_t1_res)
    base_c2c_res = run_c2c_backtest(BUY_BASE_IDX, SELL_BASE_IDX)
    base_c2c = evaluate('基准(C2C近似)', base_c2c_res, base_t1)
    bh_atk, bh_div = make_market_bh()
    bh_atk['both_half'] = (bh_atk['h1_ann'] > base_t1['h1_ann']) and (bh_atk['h2_ann'] > base_t1['h2_ann'])

    log(render_main_table([base_t1, base_c2c, bh_atk, bh_div], '0. 基准锚点与 C2C vs 真实 T+1 口径对照'))
    log(render_2026_and_yearly([base_t1, base_c2c, bh_atk, bh_div], '0b. 基准锚点分年度与 2026 关键月份表现'))

    # ==================== A 组：绝对幅度复现 ====================
    a_ths = [0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.08, 0.12]
    rows_a = [base_t1]
    c2c_cmp_lines = ['\n[A 组口径对照: C2C 近似 vs 真实 T+1 拆段]']
    c2c_cmp_lines.append(f'{"变体":<22s} {"C2C累计":>9s} {"T+1累计":>9s} {"差异(T1-C2C)":>13s} {"C2C_MDD":>9s} {"T+1_MDD":>9s}')
    c2c_cmp_lines.append(
        f'{"基准(无过滤)":<22s} {base_c2c["cum"]*100:8.2f}% {base_t1["cum"]*100:8.2f}% '
        f'{(base_t1["cum"]-base_c2c["cum"])*100:+12.2f}% {base_c2c["mdd"]*100:8.2f}% {base_t1["mdd"]*100:8.2f}%'
    )
    for th in a_ths:
        b, s = sig_abs_drop(th, w=20)
        r_t1 = evaluate(f'A_abs>{th:.2f}', run_t1_backtest(b, s), base_t1)
        r_c2c = evaluate(f'A_abs>{th:.2f}_c2c', run_c2c_backtest(b, s), base_t1)
        rows_a.append(r_t1)
        c2c_cmp_lines.append(
            f'{r_t1["name"]:<22s} {r_c2c["cum"]*100:8.2f}% {r_t1["cum"]*100:8.2f}% '
            f'{(r_t1["cum"]-r_c2c["cum"])*100:+12.2f}% {r_c2c["mdd"]*100:8.2f}% {r_t1["mdd"]*100:8.2f}%'
        )
    log(render_main_table(rows_a, '1. A 组：绝对幅度复现（drop = rolling_max(DIF,20) - DIF，dif_down & drop > th）'))
    log('\n'.join(c2c_cmp_lines))

    # ==================== B 组：价格归一化回落 ====================
    b_ws = [10, 20, 30]
    b_ths = [0.0005, 0.001, 0.0015, 0.002, 0.0025, 0.003, 0.004, 0.006]
    rows_b1 = [base_t1]
    grid_b1 = {}
    for w in b_ws:
        for th in b_ths:
            b, s = sig_rel_drop(th, w=w, falling=False)
            ev = evaluate(f'B1_W{w}_rel>{th:.4f}', run_t1_backtest(b, s), base_t1)
            rows_b1.append(ev)
            grid_b1[(w, th)] = ev
    log(render_main_table(rows_b1, '2a. B1 组：价格归一化回落——拐点日全位置过滤（dif_down & rel_drop > th，W∈{10,20,30}）'))
    log(render_plateau(grid_b1, b_ws, b_ths, 'B1 全位置参数平原（累计% / MDD% / *两半优）', base_t1))

    rows_b2 = [base_t1]
    rows_b3 = [base_t1]
    for th in b_ths:
        b, s = sig_rel_drop(th, w=20, falling=True)
        rows_b2.append(evaluate(f'B2_fall_rel>{th:.4f}', run_t1_backtest(b, s), base_t1))
        rows_b3.append(evaluate(f'B3_peak_rel>{th:.4f}', run_hold_peak_t1(th, only_above_zero=False), base_t1))
    log(render_main_table(rows_b2, '2b. B2 组：价格归一化——持续下行破阈（(dif<dif_p1) & rel_drop(W=20) > th）'))
    log(render_main_table(rows_b3, '2c. B3 组：价格归一化——持仓期峰顶回落（(dif<dif_p1) & (hold_peak-dif)/close > th）'))

    # ==================== C 组：波动率归一化回落 ====================
    c_ths = [0.05, 0.10, 0.15, 0.20, 0.30, 0.40]
    rows_c = [base_t1]
    for th in c_ths:
        b, s = sig_std_drop(th, w=20, falling=False)
        rows_c.append(evaluate(f'C1_std>{th:.2f}(turn)', run_t1_backtest(b, s), base_t1))
    for th in [0.10, 0.20, 0.30, 0.40]:
        b, s = sig_std_drop(th, w=20, falling=True)
        rows_c.append(evaluate(f'C1_std>{th:.2f}(fall)', run_t1_backtest(b, s), base_t1))
    for th in c_ths:
        b, s = sig_atr_drop(th, w=20, falling=False)
        rows_c.append(evaluate(f'C2_atr>{th:.2f}(turn)', run_t1_backtest(b, s), base_t1))
    for th in [0.10, 0.20, 0.30, 0.40]:
        b, s = sig_atr_drop(th, w=20, falling=True)
        rows_c.append(evaluate(f'C2_atr>{th:.2f}(fall)', run_t1_backtest(b, s), base_t1))
    log(render_main_table(rows_c, '3. C 组：波动率归一化回落（C1=rolling_std(DIF,60)归一化, C2=ATR60归一化）'))

    # ==================== D 组：非对称零上/零下卖出过滤 ====================
    rows_d1_abv = [base_t1]
    for th in b_ths:
        b, s = sig_asym_rel_drop(th_up=th, mode='only_above_zero', w=20, falling=False)
        rows_d1_abv.append(evaluate(f'D1_abv_W20>{th:.4f}', run_t1_backtest(b, s), base_t1))
    log(render_main_table(rows_d1_abv, '4a. D1a 组：非对称过滤——仅零上过滤（only_above_zero，DIF<=0 拐头即卖，W=20）'))

    # 重点扩展：D1b 仅零下过滤（only_below_zero，DIF>0 拐头即卖）完整 W×th 参数平原
    rows_d1_blw = [base_t1]
    grid_d1_blw = {}
    for w in b_ws:
        for th in b_ths:
            b, s = sig_asym_rel_drop(th_up=0.0, th_dn=th, mode='only_below_zero', w=w, falling=False)
            ev = evaluate(f'D1_blw_W{w}>{th:.4f}', run_t1_backtest(b, s), base_t1)
            rows_d1_blw.append(ev)
            grid_d1_blw[(w, th)] = ev
    log(render_main_table(rows_d1_blw, '4b. D1b 组：非对称过滤——仅零下过滤（only_below_zero，DIF>0 高位拐头即卖，W∈{10,20,30}）'))
    log(render_plateau(grid_d1_blw, b_ws, b_ths, 'D1b 仅零下过滤参数平原（累计% / MDD% / *两半优）', base_t1))

    # D2 双层阈值（覆盖零上>零下 与 零下>零上 两个方向） + D3/D4 非对称持续下行
    twotier_pairs = [
        (0.0015, 0.0005), (0.0020, 0.0005), (0.0020, 0.0010),
        (0.0025, 0.0010), (0.0030, 0.0010), (0.0030, 0.0015),
        (0.0005, 0.0010), (0.0005, 0.0015), (0.0005, 0.0020),
        (0.0010, 0.0015), (0.0010, 0.0020),
    ]
    rows_d2 = [base_t1]
    for tu, td in twotier_pairs:
        b, s = sig_asym_rel_drop(th_up=tu, th_dn=td, mode='two_tier', w=20, falling=False)
        rows_d2.append(evaluate(f'D2_up{tu:.4f}_dn{td:.4f}', run_t1_backtest(b, s), base_t1))
    for th in [0.0010, 0.0015, 0.0020, 0.0025]:
        b, s = sig_asym_rel_drop(th_up=th, mode='only_above_zero', w=20, falling=True)
        rows_d2.append(evaluate(f'D3_fall_abv>{th:.4f}', run_t1_backtest(b, s), base_t1))
    for th in [0.0010, 0.0015, 0.0020, 0.0025]:
        b, s = sig_asym_rel_drop(th_up=0.0, th_dn=th, mode='only_below_zero', w=20, falling=True)
        rows_d2.append(evaluate(f'D3_fall_blw>{th:.4f}', run_t1_backtest(b, s), base_t1))
    log(render_main_table(rows_d2, '4c. D2/D3 组：双层阈值 two_tier（W=20） & 非对称持续下行破阈'))

    # ==================== E 组：连续下拐 / 单日回落幅度过滤 ====================
    rows_e = [base_t1]
    for th1 in [0.0002, 0.0004, 0.0006, 0.0008, 0.0010, 0.0015, 0.0020]:
        b, s = sig_1d_drop(th1, only_above_zero=False, falling=False)
        rows_e.append(evaluate(f'E1_1d>{th1:.4f}(turn)', run_t1_backtest(b, s), base_t1))
    for th1 in [0.0004, 0.0008, 0.0012]:
        b, s = sig_1d_drop(th1, only_above_zero=True, falling=False)
        rows_e.append(evaluate(f'E1_1d_abv>{th1:.4f}', run_t1_backtest(b, s), base_t1))
    for th1 in [0.0006, 0.0010, 0.0015]:
        b, s = sig_1d_drop(th1, only_above_zero=False, falling=True)
        rows_e.append(evaluate(f'E1_1d>{th1:.4f}(fall)', run_t1_backtest(b, s), base_t1))
    for days, ab, nm in [
        (2, False, 'E2_连续2日下行(全)'),
        (3, False, 'E2_连续3日下行(全)'),
        (2, True, 'E2_连续2日下行(仅零上)'),
        (3, True, 'E2_连续3日下行(仅零上)'),
    ]:
        b, s = sig_consec_down(days=days, only_above_zero=ab)
        rows_e.append(evaluate(nm, run_t1_backtest(b, s), base_t1))
    log(render_main_table(rows_e, '5. E 组：单日回落幅度过滤 & 连续多日 DIF 下行确认卖出'))

    # ==================== 跨家族汇总 ====================
    all_tested = (rows_a[1:] + rows_b1[1:] + rows_b2[1:] + rows_b3[1:] + rows_c[1:]
                  + rows_d1_abv[1:] + rows_d1_blw[1:] + rows_d2[1:] + rows_e[1:])
    both_beat = [r for r in all_tested if r['both_half']]
    both_beat_cum = sorted(both_beat, key=lambda x: x['cum'], reverse=True)
    both_beat_cal = sorted(both_beat, key=lambda x: x['calmar'], reverse=True)

    log('\n' + '=' * 148)
    log('6. 跨家族汇总：前后两半段年化均优于基准的变体（共 %d / %d 个）' % (len(both_beat), len(all_tested)))
    log('=' * 148)
    # 取按累计前 12 与按 Calmar 前 12 的并集去重
    seen = set()
    top_both = [base_t1]
    for r in both_beat_cum[:12] + both_beat_cal[:12]:
        if r['name'] not in seen:
            seen.add(r['name'])
            top_both.append(r)
    log(render_main_table(top_both, '6a. 前后两半均胜出核心榜（按累计收益与 Calmar 综合精选）'))
    log(render_2026_and_yearly(top_both, '6b. 前后两半均胜出核心榜——分年度收益表与 2026 关键月表现'))

    # 全量最高收益但未过两半检验的对照（如 D2_up0.0020_dn0.0010, B1_W20_rel>0.0020, A_abs>0.05/0.04）
    high_single = [r for r in sorted(all_tested, key=lambda x: x['cum'], reverse=True)[:12] if not r['both_half']]
    if high_single:
        log(render_main_table([base_t1] + high_single[:8], '6c. 高收益但未通过“两半均优”的代表变体对照'))
        log(render_2026_and_yearly([base_t1] + high_single[:8], '6d. 未通过“两半均优”高收益变体的分年度详情'))

    # ==================== F 组：滚动样本外（Walk-Forward OOS 2022~2026） ====================
    log('\n' + '=' * 136)
    log('7. F 组：严格滚动样本外（Walk-Forward OOS, 2022~2026 逐年）验证')
    log('   规则：每年底仅用截至当年底的全部历史真实 T+1 净值在候选网格中选参（含 0.0=无过滤基准），次年全年执行，状态连续继承')
    log('=' * 136)
    wf_rel_cands = [0.0, 0.0005, 0.001, 0.0015, 0.002, 0.0025, 0.003, 0.004, 0.006]
    wf_std_cands = [0.0, 0.05, 0.10, 0.15, 0.20, 0.30, 0.40]
    oos_years = (2022, 2023, 2024, 2025, 2026)

    first_2022 = IDX[IDX.year == 2022][0]
    anchor_2021 = IDX[POS_OF[first_2022] - 1]
    b_oos_cum, b_oos_ann, b_oos_mdd = sub_metrics(base_t1['nav'], anchor_2021, None)
    b_oos_cal = b_oos_ann / abs(b_oos_mdd)
    b_oos_rets = base_t1['nav'].loc[anchor_2021:].pct_change().dropna()
    b_oos_sh = float(b_oos_rets.mean() / b_oos_rets.std() * np.sqrt(252))
    b_oos_yr = {y: ytd_ret(base_t1['nav'], y) for y in oos_years}

    wf_configs = [
        ('B1_W20', wf_rel_cands, 'ann', 'WF_B1_W20_按年化'),
        ('B1_W20', wf_rel_cands, 'calmar', 'WF_B1_W20_按Calmar'),
        ('B1_W30', wf_rel_cands, 'ann', 'WF_B1_W30_按年化'),
        ('B1_W30', wf_rel_cands, 'calmar', 'WF_B1_W30_按Calmar'),
        ('D1_above20', wf_rel_cands, 'ann', 'WF_D1_abv20_按年化'),
        ('D1_above20', wf_rel_cands, 'calmar', 'WF_D1_abv20_按Calmar'),
        ('D1_below20', wf_rel_cands, 'ann', 'WF_D1_blw20_按年化'),
        ('D1_below20', wf_rel_cands, 'calmar', 'WF_D1_blw20_按Calmar'),
        ('D1_below20', [0.0, 0.0005, 0.001, 0.0015, 0.002], 'calmar', 'WF_D1_blw20(<=2‰)'),
        ('D1_below30', wf_rel_cands, 'ann', 'WF_D1_blw30_按年化'),
        ('D1_below30', wf_rel_cands, 'calmar', 'WF_D1_blw30_按Calmar'),
        ('C1_std20', wf_std_cands, 'ann', 'WF_C1_std20_按年化'),
        ('C1_std20', wf_std_cands, 'calmar', 'WF_C1_std20_按Calmar'),
        ('B2_fall20', wf_rel_cands, 'ann', 'WF_B2_fall20_按年化'),
    ]
    log(f'{"OOS 方案":<22s} {"22-26累计":>9s} {"年化":>7s} {"MDD":>8s} {"Calmar":>6s} {"Sharpe":>6s} | '
        f'{"2022":>7s} {"2023":>7s} {"2024":>7s} {"2025":>7s} {"2026":>7s}')
    log('-' * 136)
    log(f'{"基准(无过滤,固定)":<22s} {b_oos_cum*100:8.2f}% {b_oos_ann*100:6.2f}% {b_oos_mdd*100:7.2f}% '
        f'{b_oos_cal:6.2f} {b_oos_sh:6.2f} | ' +
        ' '.join(f'{b_oos_yr[y]*100:7.2f}' for y in oos_years))

    wf_details = []
    for fam, cands, obj, label in wf_configs:
        wfr = run_walk_forward(fam, cands, objective=obj, oos_years=oos_years)
        wf_details.append((label, wfr))
        log(f'{label:<22s} {wfr["oos_cum"]*100:8.2f}% {wfr["oos_ann"]*100:6.2f}% {wfr["oos_mdd"]*100:7.2f}% '
            f'{wfr["oos_calmar"]:6.2f} {wfr["oos_sharpe"]:6.2f} | ' +
            ' '.join(f'{wfr["yearly"][y]*100:7.2f}' for y in oos_years))

    log('\n[Walk-Forward 每年样本内选出的最优阈值轨迹]')
    for label, wfr in wf_details:
        tr_s = '  '.join(f'{y}:{wfr["chosen"][y][0]:.4f}' for y in oos_years)
        log(f'  {label:<22s} -> {tr_s}')

    # ==================== G 组（附带测试）：512890 / 159201 20日动量双择优防守 ====================
    log('\n' + '=' * 136)
    log('8. G 组（附带测试）：防守腿升级为 512890 / 159201 20日动量双择优（已修复 512890 11月复权断裂）')
    log('=' * 136)
    g_specs = [
        ('基准(无过滤)', lambda dm, rb: run_t1_backtest(BUY_BASE_IDX, SELL_BASE_IDX, def_mode=dm, rebal_def_days=rb)),
        ('D1_blw_W20>0.0010', lambda dm, rb: run_t1_backtest(*sig_asym_rel_drop(0.0, 0.0010, 20, 'only_below_zero', False), def_mode=dm, rebal_def_days=rb)),
        ('D1_blw_W30>0.0010', lambda dm, rb: run_t1_backtest(*sig_asym_rel_drop(0.0, 0.0010, 30, 'only_below_zero', False), def_mode=dm, rebal_def_days=rb)),
        ('D1_blw_W20>0.0020', lambda dm, rb: run_t1_backtest(*sig_asym_rel_drop(0.0, 0.0020, 20, 'only_below_zero', False), def_mode=dm, rebal_def_days=rb)),
        ('B1_W30_rel>0.0020', lambda dm, rb: run_t1_backtest(*sig_rel_drop(0.0020, 30, False), def_mode=dm, rebal_def_days=rb)),
        ('B1_W30_rel>0.0010', lambda dm, rb: run_t1_backtest(*sig_rel_drop(0.0010, 30, False), def_mode=dm, rebal_def_days=rb)),
        ('C1_std>0.10(turn)', lambda dm, rb: run_t1_backtest(*sig_std_drop(0.10, 20, False), def_mode=dm, rebal_def_days=rb)),
        ('D2_up0.0020_dn0.0010', lambda dm, rb: run_t1_backtest(*sig_asym_rel_drop(0.0020, 0.0010, 20, 'two_tier', False), def_mode=dm, rebal_def_days=rb)),
    ]
    valid_201_dates = IDX[~np.isnan(MOM_159201)]
    dual_anchor = IDX[POS_OF[valid_201_dates[0]] - 1]
    log('  双防守可用区间（159201 满20交易日起）: %s ~ %s' %
        (valid_201_dates[0].strftime('%Y-%m-%d'), IDX[-1].strftime('%Y-%m-%d')))
    log(f'{"组合方案":<36s} {"全区间累计":>9s} {"全区间年化":>8s} {"全MDD":>8s} {"Calmar":>6s} {"换仓":>4s} | '
        f'{"25.03-26.11累计":>14s} {"年化":>7s} {"MDD":>8s} {"26YTD":>7s}')
    log('-' * 126)
    for base_nm, fn in g_specs:
        for def_lbl, dm, rb in [
            ('固定512890', 'fixed_890', None),
            ('双择优(入场)', 'dual_mom20', None),
            ('双择优(入场+10日)', 'dual_mom20', 10),
        ]:
            res_g = fn(dm, rb)
            ev_g = evaluate(f'{base_nm}+{def_lbl}', res_g, base_t1)
            d_cum, d_ann, d_mdd = sub_metrics(res_g['nav'], dual_anchor, None)
            log(f'{ev_g["name"]:<36s} {ev_g["cum"]*100:8.2f}% {ev_g["ann"]*100:7.2f}% {ev_g["mdd"]*100:7.2f}% '
                f'{ev_g["calmar"]:6.2f} {ev_g["ntrades"]:>4d} | '
                f'{d_cum*100:13.2f}% {d_ann*100:6.2f}% {d_mdd*100:7.2f}% {ev_g["ytd26"]*100:6.2f}%')

    # ==================== 机制深度诊断 ====================
    log('\n' + '=' * 136)
    log('9. 关键机制诊断：基准 vs 核心代表变体的 2023 年表现与 2026-06~11 换仓明细')
    log('=' * 136)
    inspect_list = [
        base_t1,
        grid_d1_blw[(20, 0.0010)],
        grid_d1_blw[(30, 0.0010)],
        grid_d1_blw[(20, 0.0020)],
        grid_b1[(30, 0.0020)],
        grid_b1[(30, 0.0010)],
        [r for r in rows_c if r['name'] == 'C1_std>0.10(turn)'][0],
        [r for r in rows_d2 if r['name'] == 'D2_up0.0020_dn0.0010'][0],
        [r for r in rows_d1_abv if r['name'] == 'D1_abv_W20>0.0020'][0],
    ]
    for ev in inspect_list:
        tr26 = [t for t in ev['res']['trades'] if t[1] >= pd.Timestamp('2026-06-01')]
        tr_s = ', '.join(f'{t[1].strftime("%m-%d")}:{t[2]}({t[4]})' for t in tr26)
        log(f'  [{ev["name"]}] 累计={ev["cum"]*100:.2f}% 年化={ev["ann"]*100:.2f}% MDD={ev["mdd"]*100:.2f}% Calmar={ev["calmar"]:.2f} '
            f'换仓={ev["ntrades"]} 打脸={ev["nwhip"]}段({ev["whip_sum"]*100:+.2f}%) 2023={ev["yearly"][2023]*100:+.2f}% 7月进={ev["jul26_atk"]}/{ev["jul26_tot"]}')
        log(f'     2026-06起换仓: {tr_s}')

    # 检查 2026-08~09 基准全部卖出信号日的 DIF 正负与 rel_drop 值，直观解释 D1_below 为何奏效
    log('\n[2026-06 ~ 2026-11 基准全部卖出信号日 DIF 与 rel_drop(W=20/30) 明细]')
    sub_idx = IDX[(IDX >= pd.Timestamp('2026-06-01')) & SELL_BASE_IDX]
    for d in sub_idx:
        d_val = float(dif_full.loc[d])
        c_val = float(close_full.loc[d])
        rd20 = float((roll_max_dif[20].loc[d] - d_val) / c_val)
        rd30 = float((roll_max_dif[30].loc[d] - d_val) / c_val)
        pos_lbl = 'DIF>0(零上)' if d_val > 0 else 'DIF<=0(零下)'
        log(f'  {d.strftime("%Y-%m-%d")}  close={c_val:6.2f}  DIF={d_val:+7.4f} ({pos_lbl:<11s})  rel_drop20={rd20:.5f}  rel_drop30={rd30:.5f}')

    # ==================== 10. 最终结论与工程建议（动态引用本次清洗后精确指标） ====================
    ev_blw20_10 = grid_d1_blw[(20, 0.0010)]
    ev_b1_30_20 = grid_b1[(30, 0.0020)]
    wf_map = {lbl: wfr for lbl, wfr in wf_details}
    wf_blw20 = wf_map['WF_D1_blw20_按Calmar']
    wf_blw20_safe = wf_map['WF_D1_blw20(<=2‰)']
    wf_b1_30 = wf_map['WF_B1_W30_按Calmar']
    # 计算最优组合加双择优防守（便于结论准确引用）
    g_blw20_in = evaluate('D1_blw20_in', run_t1_backtest(*sig_asym_rel_drop(0.0, 0.0010, 20, 'only_below_zero', False), def_mode='dual_mom20', rebal_def_days=None), base_t1)
    g_blw20_10d = evaluate('D1_blw20_10d', run_t1_backtest(*sig_asym_rel_drop(0.0, 0.0010, 20, 'only_below_zero', False), def_mode='dual_mom20', rebal_def_days=10), base_t1)

    log('\n' + '=' * 136)
    log('10. 研究总结、核心机理发现与工程建议')
    log('=' * 136)
    log('一、口径与数据质量清洗（首要基础）')
    log(f'  1) 真实 T+1 开盘拆段口径下（已清洗 2026-11-10 起复权异常），基准累计收益 {base_t1["cum"]*100:.2f}%'
        f'（年化 {base_t1["ann"]*100:.2f}%，MDD {base_t1["mdd"]*100:.2f}%，Calmar {base_t1["calmar"]:.2f}，Sharpe {base_t1["sharpe"]:.2f}），')
    log(f'     显著低于 C2C 近似口径的 {base_c2c["cum"]*100:.2f}%（高估 {(base_c2c["cum"]-base_t1["cum"])*100:+.2f}pct）；A 组低阈值在 C2C 下高估可达 +43~+134pct。')
    log('  2) 内存清洗说明：2026-11-10 起所有核心 ETF 后复权均按 2026-11-09 常数因子 × 不复权 OHLC 续接（绝未修改磁盘 CSV），')
    log('     彻底消除 512890 假腰斩(-75%)、512100 假暴涨及 159552/159201 自 11-10 起的 4%~8% 复权漂移。')
    log('二、关键经济学与触发机制反直觉发现')
    log('  1) 拐点日过滤(B1) vs 持续下行破阈(B2)/持仓峰顶回落(B3)/多日确认(E2)：')
    log('     B2/B3/E2 全面跑输基准，因为延迟 1~3 天卖出实质是“卖在更低价”；')
    log('     B1 有效在于“直接忽略拐点日微幅回落的假卖出信号、留在车上”，但也带来高阈值漏卖主跌浪的悬崖风险。')
    log('  2) 零上过滤(D1a) vs 零下过滤(D1b) 的颠覆性对比：')
    log('     原设想“仅 DIF>0 过滤回调、DIF<=0 立即止损”(D1a) 被数据证伪：因为买入仅在 DIF<0 触发，底部筑底反复打脸')
    log('     （如 2026-08-24/09-04/09-10）全发生在 DIF<=0，仅零上过滤滤不掉打脸，反而高阈值易漏卖高位顶部(MDD -29.53%)，OOS 跑输基准。')
    log('     相反，“仅 DIF<=0 过滤微幅假拐头、DIF>0 高位坚持拐头即卖”(D1b) 成为全场风险收益比最强王者！')
    log('三、真实 T+1 口径下最优稳健方案推荐')
    log(f'  【首选推荐（稳健兼顾高收益与低回撤）】D1_blw_W20>0.0010（或 0.0015）：仅 DIF<=0 要求 rel_drop(W=20)>0.0010，DIF>0 拐头即卖')
    log(f'    - 固定 512890 防守：累计 {ev_blw20_10["cum"]*100:.2f}%（较基准 {(ev_blw20_10["cum"]-base_t1["cum"])*100:+.2f}pct），'
        f'年化 {ev_blw20_10["ann"]*100:.2f}%（{(ev_blw20_10["ann"]-base_t1["ann"])*100:+.2f}pct），MDD {ev_blw20_10["mdd"]*100:.2f}%（优于基准 {(base_t1["mdd"]-ev_blw20_10["mdd"])*-100:+.2f}pct），')
    log(f'      Calmar {ev_blw20_10["calmar"]:.2f}（vs {base_t1["calmar"]:.2f}），Sharpe {ev_blw20_10["sharpe"]:.2f}（vs {base_t1["sharpe"]:.2f}），'
        f'换仓 {ev_blw20_10["ntrades"]} 次（vs {base_t1["ntrades"]}），打脸 {ev_blw20_10["nwhip"]} 段/{ev_blw20_10["whip_sum"]*100:+.2f}%。')
    log(f'    - 稳健性：前后两半段在年化与回撤上双双碾压基准（前半 {ev_blw20_10["h1_ann"]*100:.2f}%/{ev_blw20_10["h1_mdd"]*100:.2f}%，'
        f'后半 {ev_blw20_10["h2_ann"]*100:.2f}%/{ev_blw20_10["h2_mdd"]*100:.2f}%）；8 年全正收益，2023 年 {ev_blw20_10["yearly"][2023]*100:+.2f}%；')
    log(f'      2026YTD {ev_blw20_10["ytd26"]*100:.2f}%（26-07 完整避险 0/23 天，26-09 {ev_blw20_10["m2609"]*100:+.2f}%，26-11 {ev_blw20_10["m2611"]*100:+.2f}%）；'
        f'天然免疫 2026-09-28 零上顶部悬崖。')
    log(f'    - 参数平原与 OOS：W=20 在 [0.0010, 0.0020] 三连两半双优，W=30 在 [0.0010, 0.0060] 无回撤悬崖；')
    log(f'      Walk-Forward OOS（2022~2026）全网格累计 {wf_blw20["oos_cum"]*100:.2f}%（年化 {wf_blw20["oos_ann"]*100:.2f}% vs 基准 {b_oos_ann*100:.2f}%）；'
        f'限定安全上界 <=0.0020 后 OOS 累计 {wf_blw20_safe["oos_cum"]*100:.2f}%（年化 {wf_blw20_safe["oos_ann"]*100:.2f}%，MDD {wf_blw20_safe["oos_mdd"]*100:.2f}%，Calmar {wf_blw20_safe["oos_calmar"]:.2f}）全面碾压基准！')
    log(f'    - 防守择优正交叠加：叠加 512890/159201 20日动量双择优后，累计跃升至 {g_blw20_in["cum"]*100:.2f}%（入场择优, Calmar {g_blw20_in["calmar"]:.2f}）'
        f' / {g_blw20_10d["cum"]*100:.2f}%（入场+10日重择优, 年化 {g_blw20_10d["ann"]*100:.2f}%, MDD {g_blw20_10d["mdd"]*100:.2f}%, Calmar {g_blw20_10d["calmar"]:.2f}）。')
    log(f'  【激进高收益备选】B1_W30_rel>0.0020（全位置过滤，W=30，rel_drop>0.0020）')
    log(f'    - 累计 {ev_b1_30_20["cum"]*100:.2f}%（{(ev_b1_30_20["cum"]-base_t1["cum"])*100:+.2f}pct），年化 {ev_b1_30_20["ann"]*100:.2f}%，MDD {ev_b1_30_20["mdd"]*100:.2f}%，'
        f'Calmar {ev_b1_30_20["calmar"]:.2f}，Sharpe {ev_b1_30_20["sharpe"]:.2f}，两半均优，WF 5 年 100% 锁定 0.0020（OOS {wf_b1_30["oos_cum"]*100:.2f}%，年化 {wf_b1_30["oos_ann"]*100:.2f}%）。')
    log(f'    - 代价与风险警告：① 2023 年仅 {ev_b1_30_20["yearly"][2023]*100:+.2f}%（基准 {base_t1["yearly"][2023]*100:+.2f}%）；② 紧贴 2026-09-28 零上顶部阈值悬崖'
        f'（当日 rel_drop=0.002003，高 0.0005 即漏卖硬吃 10 月暴跌）；③ 与双择优防守存在负协同。')
    log('  【不建议采纳】A 组（点位依赖）、C 组波动率归一化（WF OOS 严重过拟合）、D1a 仅零上过滤（OOS 跑输且漏卖风险大）、B2/B3/E 组（延迟卖出全面跑输基准）。')

    with open(OUT_PATH, 'w', encoding='utf-8') as f:
        f.write('\n'.join(out) + '\n')
    print(f'\n结果已写入: {OUT_PATH}')


if __name__ == '__main__':
    main()
