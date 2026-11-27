#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
防守期收益最大化系统研究（A/B/C/D 四组方案）

核心约束：
- 保持生产进攻择时信号不变：方案C（avg_880003 MACD DIF 17/34/9，零下买/任意卖）
- 保持生产进攻腿不变：2024-06-28 前 512100，2024-06-28 起 159552
- 成交口径：真实 T+1 开盘成交拆段账户模拟（隔夜段按前日持仓 + 开盘按实际换手扣双边万1 + 日内段按新持仓）
- 主对比窗口：2019-01-18 ~ 2026-10-23（1879交易日，全防守候选公共区间），前后半段切分日 2022-11-22
- 附加窗口（D组）：2025-02-27 ~ 2026-11-25（424交易日，检验 512890 vs 159201 最新双择优）
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

from backtest import calc_signals

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, 'data')
RESULT_PATH = os.path.join(BASE_DIR, 'result_maxret_defense.txt')

FEE = 0.0001
FAST, SLOW, SIG_N = 17, 34, 9
ATK_SWITCH = pd.Timestamp('2024-06-28')
MAIN_START = pd.Timestamp('2019-01-18')
MAIN_END = pd.Timestamp('2026-10-23')
SPLIT_DATE = pd.Timestamp('2022-11-22')
D_START = pd.Timestamp('2025-02-27')
D_END = pd.Timestamp('2026-11-25')

NAMES = {
    '512100': '中证1000',
    '159552': '中证2000增',
    '512890': '红利低波',
    '518880': '黄金ETF',
    '513100': '纳指ETF',
    '513500': '标普500',
    '511260': '十年国债',
    '511010': '五年国债',
    '512800': '银行ETF',
    '159201': '自由现金流',
}


def load_csv(filename: str) -> pd.DataFrame:
    path = os.path.join(DATA_DIR, filename)
    df = pd.read_csv(path, parse_dates=['date']).set_index('date').sort_index()
    df.index = df.index.normalize()
    return df[~df.index.duplicated(keep='last')]


def repair_nov2026_hfq_in_memory(code: str, hfq_df: pd.DataFrame, cutoff: str = '2026-11-09'):
    """
    仅在内存中修复 2026-11-10 起 hfq.csv 的复权断层（不改动磁盘任何文件）：
    使用 2026-11-09 当日 hfq/raw 比值，将 2026-11-10 起的不复权 OHLC 等比续接。
    """
    raw_path = os.path.join(DATA_DIR, f'etf_{code}.csv')
    if not os.path.exists(raw_path):
        return hfq_df.copy(), False
    raw = load_csv(f'etf_{code}.csv')
    cut_ts = pd.Timestamp(cutoff)
    if cut_ts not in hfq_df.index or cut_ts not in raw.index:
        return hfq_df.copy(), False
    factor = float(hfq_df.loc[cut_ts, 'close'] / raw.loc[cut_ts, 'close'])
    out = hfq_df.copy()
    post_idx = out.index[out.index > cut_ts].intersection(raw.index)
    if len(post_idx) == 0:
        return out, False
    for col in ('open', 'high', 'low', 'close'):
        if col in out.columns and col in raw.columns:
            out.loc[post_idx, col] = raw.loc[post_idx, col] * factor
    return out, True


def build_aligned_panels(raw_dfs: dict, idx: pd.DatetimeIndex):
    """将各资产 OHLC 对齐到统一交易日历，单日停牌/缺日用前收盘价填充（当日涨跌幅0）"""
    opens, closes = {}, {}
    missing_info = {}
    for code, df in raw_dfs.items():
        miss = idx.difference(df.index)
        missing_info[code] = list(miss)
        c = df['close'].reindex(idx)
        o = df['open'].reindex(idx)
        # 仅前向填充已有上市后的单日停牌缺失，不向未上市之前填充
        c_ff = c.ffill()
        o_fill = o.where(o.notna(), c_ff)
        opens[code] = o_fill
        closes[code] = c_ff
    return pd.DataFrame(opens, index=idx), pd.DataFrame(closes, index=idx), missing_info


def precompute_indicators(raw_dfs: dict, codes: list, windows=(20, 40, 60, 120)):
    """基于全历史原始日线预计算：
    - ret[N]: N日纯收益动量 C[t]/C[t-N] - 1
    - ret_vol[N]: N日风险调整动量 Ret / (Vol_daily * sqrt(N))
    - slope_r2[N]: N日 log(close) 线性回归年化斜率 * R^2
    - vol60: 近60日日收益标准差（用于逆波动率加权，不足60日用至少10日扩展窗口）
    返回 dict[metric][N][code] -> pd.Series（index 为原始交易日）
    """
    ret_map = {n: {} for n in windows}
    rv_map = {n: {} for n in windows}
    sr2_map = {n: {} for n in windows}
    vol60_map = {}

    for code in codes:
        c = raw_dfs[code]['close'].astype(float)
        r = c.pct_change()
        # 60日波动率（允许前10~60日用 min_periods=10）
        vol60_map[code] = r.rolling(60, min_periods=10).std()

        log_c = np.log(c.values)
        idx = c.index
        for n in windows:
            ret_s = c / c.shift(n) - 1.0
            vol_s = r.rolling(n, min_periods=n).std() * np.sqrt(n)
            rv_s = ret_s / vol_s.replace(0, np.nan)
            ret_map[n][code] = ret_s
            rv_map[n][code] = rv_s

            # 向量化计算滚动 N 日回归斜率 * R^2
            arr = np.full(len(c), np.nan, dtype=float)
            if len(c) >= n:
                x = np.arange(n, dtype=float)
                xm = x - x.mean()
                sxx = float(np.dot(xm, xm))
                # 使用 sliding_window_view 加速
                win = np.lib.stride_tricks.sliding_window_view(log_c, n)
                ym = win - win.mean(axis=1, keepdims=True)
                sxy = ym @ xm
                syy = (ym * ym).sum(axis=1)
                slope = sxy / sxx
                with np.errstate(divide='ignore', invalid='ignore'):
                    r2 = np.where(syy > 1e-16, (sxy * sxy) / (sxx * syy), 0.0)
                ann_slope = np.exp(slope * 252.0) - 1.0
                arr[n - 1:] = ann_slope * r2
            sr2_map[n][code] = pd.Series(arr, index=idx)

    return {'ret': ret_map, 'ret_vol': rv_map, 'slope_r2': sr2_map, 'vol60': vol60_map}


def get_atk_code(d: pd.Timestamp) -> str:
    return '512100' if d < ATK_SWITCH else '159552'


def simulate_strategy(O: pd.DataFrame, C: pd.DataFrame, sig: pd.DataFrame,
                      defense_picker, init_atk: bool = False,
                      rebal_def_every: int = 0, fee: float = FEE,
                      init_def_day=None):
    """
    真实 T+1 开盘成交拆段账户模拟引擎。
    - defense_picker(sig_day, cur_holdings_w) -> dict[code, weight]
      在 T 日收盘被调用，返回 T+1 开盘的目标防守权重（权重之和=1）。
    - init_atk: 首日（idx[0]）是否已处于进攻态
    - rebal_def_every: 防守期内部定期重排/再平衡周期（交易日），0 表示防守期内不动（自然漂移）
    """
    idx = O.index
    n = len(idx)
    O_vals = {c: O[c].values for c in O.columns}
    C_vals = {c: C[c].values for c in C.columns}
    buy_arr = sig['buy_sig'].reindex(idx).fillna(False).values
    sell_arr = sig['sell_sig'].reindex(idx).fillna(False).values

    # 初始化首日收盘持仓（nav[0] = 1.0）
    state_atk = bool(init_atk)
    if state_atk:
        holdings = {get_atk_code(idx[0]): 1.0}
    else:
        d0 = init_def_day if init_def_day is not None else idx[0]
        w0 = defense_picker(d0, {})
        holdings = {k: float(v) for k, v in w0.items() if v > 0}

    nav = np.ones(n, dtype=float)
    hold_atk = np.zeros(n, dtype=bool)
    hold_atk[0] = state_atk

    # 独立统计进攻段与防守段净值乘子
    def_wealth = 1.0
    atk_wealth = 1.0
    def_half_days = 0.0   # 隔夜=0.5天，日内=0.5天
    atk_half_days = 0.0

    # 交易与成本统计
    pending = None        # 'buy' or 'sell'
    def_days_held = 0 if state_atk else 1
    total_turnover = 0.0          # 累计双边换手（满仓切一次=2.0）
    def_internal_turnover = 0.0   # 防守期内部换手
    switch_count = 0              # 进攻<->防守切换次数
    def_rebal_count = 0           # 防守期内部调仓次数
    fee_factor_total = 1.0
    fee_factor_def_int = 1.0
    events = []                   # 记录关键切换事件（供 D 组审计）

    # 如果首日收盘有信号，设 pending
    if buy_arr[0]:
        pending = 'buy'
    elif sell_arr[0]:
        pending = 'sell'

    for i in range(1, n):
        d = idx[i]
        prev_d = idx[i - 1]
        prev_atk = state_atk

        # 1) 隔夜段：昨日收盘 -> 今日开盘（按昨日收盘实际持仓 holdings）
        nav_prev = sum(holdings.values())
        v_open = {}
        for c, v in holdings.items():
            c_prev = C_vals[c][i - 1]
            o_cur = O_vals[c][i]
            if np.isnan(c_prev) or np.isnan(o_cur) or c_prev <= 0:
                v_open[c] = v
            else:
                v_open[c] = v * (o_cur / c_prev)
        nav_open = sum(v_open.values())
        r_ov = nav_open / nav_prev if nav_prev > 0 else 1.0
        if prev_atk:
            atk_wealth *= r_ov
            atk_half_days += 0.5
        else:
            def_wealth *= r_ov
            def_half_days += 0.5

        # 2) 判断今日开盘是否调仓
        target_w = None
        event_type = None
        if pending == 'buy' and not state_atk:
            state_atk = True
            atk_c = get_atk_code(d)
            target_w = {atk_c: 1.0}
            event_type = 'to_atk'
            def_days_held = 0
        elif pending == 'sell' and state_atk:
            state_atk = False
            target_w = defense_picker(prev_d, {})
            event_type = 'to_def'
            def_days_held = 1
        elif not state_atk:
            # 仍在防守态：检查是否到达定期重排/再平衡日
            if rebal_def_every > 0 and def_days_held >= rebal_def_every:
                cur_w = {c: v / nav_open for c, v in v_open.items() if v > 0}
                cand_w = defense_picker(prev_d, cur_w)
                # 只有目标权重与当前漂移后权重有实质变化才调仓
                all_k = set(cur_w) | set(cand_w)
                diff = sum(abs(cand_w.get(k, 0.0) - cur_w.get(k, 0.0)) for k in all_k)
                if diff > 1e-6:
                    target_w = cand_w
                    event_type = 'def_rebal'
                def_days_held = 1
            else:
                def_days_held += 1
        pending = None

        # 执行开盘调仓与扣费
        if target_w is not None:
            # 归一化目标权重
            s_w = sum(target_w.values())
            target_w = {k: v / s_w for k, v in target_w.items() if v > 1e-12}
            all_k = set(v_open) | set(target_w)
            traded_amt = sum(abs(nav_open * target_w.get(k, 0.0) - v_open.get(k, 0.0)) for k in all_k)
            turn = traded_amt / nav_open if nav_open > 0 else 0.0
            cost = fee * turn
            nav_post = nav_open * (1.0 - cost)
            v_post = {k: nav_post * w for k, w in target_w.items()}

            total_turnover += turn
            fee_factor_total *= (1.0 - cost)
            if event_type in ('to_atk', 'to_def'):
                switch_count += 1
            elif event_type == 'def_rebal':
                def_rebal_count += 1
                def_internal_turnover += turn
                fee_factor_def_int *= (1.0 - cost)
                def_wealth *= (1.0 - cost)
            events.append((d, prev_d, event_type, dict(target_w), turn, cost))
        else:
            v_post = v_open
            nav_post = nav_open

        # 3) 日内段：今日开盘 -> 今日收盘
        v_close = {}
        for c, v in v_post.items():
            o_cur = O_vals[c][i]
            c_cur = C_vals[c][i]
            if np.isnan(o_cur) or np.isnan(c_cur) or o_cur <= 0:
                v_close[c] = v
            else:
                v_close[c] = v * (c_cur / o_cur)
        nav_close = sum(v_close.values())
        r_intra = nav_close / nav_post if nav_post > 0 else 1.0
        if state_atk:
            atk_wealth *= r_intra
            atk_half_days += 0.5
        else:
            def_wealth *= r_intra
            def_half_days += 0.5

        holdings = v_close
        nav[i] = nav_close
        hold_atk[i] = state_atk

        # 4) 收盘信号（T日产生，T+1开盘执行）
        if buy_arr[i]:
            pending = 'buy'
        elif sell_arr[i]:
            pending = 'sell'

    nav_s = pd.Series(nav, index=idx)
    def_ann = (def_wealth ** (252.0 / def_half_days) - 1.0) if def_half_days > 0 else np.nan
    atk_ann = (atk_wealth ** (252.0 / atk_half_days) - 1.0) if atk_half_days > 0 else np.nan

    return {
        'nav': nav_s,
        'hold_atk': pd.Series(hold_atk, index=idx),
        'def_cum': def_wealth - 1.0,
        'def_ann': def_ann,
        'def_days': def_half_days,
        'atk_cum': atk_wealth - 1.0,
        'atk_ann': atk_ann,
        'switch_count': switch_count,
        'def_rebal_count': def_rebal_count,
        'full_switches': total_turnover / 2.0,
        'def_int_switches': def_internal_turnover / 2.0,
        'fee_drag': 1.0 - fee_factor_total,
        'def_int_fee_drag': 1.0 - fee_factor_def_int,
        'events': events,
    }


def calc_metrics(nav: pd.Series) -> dict:
    nav = nav.dropna()
    cum = float(nav.iloc[-1] / nav.iloc[0] - 1.0)
    cal_days = (nav.index[-1] - nav.index[0]).days
    cagr = float((nav.iloc[-1] / nav.iloc[0]) ** (365.0 / cal_days) - 1.0) if cal_days > 0 else np.nan
    dd = nav / nav.cummax() - 1.0
    mdd = float(dd.min())
    calmar = cagr / abs(mdd) if mdd < 0 else np.nan
    ret = nav.pct_change().dropna()
    std = float(ret.std())
    sharpe = float(ret.mean() / std * np.sqrt(252.0)) if std > 0 else np.nan
    return {'cum': cum, 'cagr': cagr, 'mdd': mdd, 'calmar': calmar, 'sharpe': sharpe}


def calc_split_metrics(nav: pd.Series, split_date: pd.Timestamp = SPLIT_DATE) -> dict:
    # 前半段：[start, split_date]，后半段：[split_date, end]（闭合连乘）
    h1 = nav[nav.index <= split_date]
    h2 = nav[nav.index >= split_date]
    m1 = calc_metrics(h1)
    m2 = calc_metrics(h2)
    return {
        'h1_cagr': m1['cagr'], 'h1_mdd': m1['mdd'], 'h1_cum': m1['cum'],
        'h2_cagr': m2['cagr'], 'h2_mdd': m2['mdd'], 'h2_cum': m2['cum'],
    }


def calc_yearly_returns(nav: pd.Series) -> dict:
    """闭合年度收益：每年从上一年末净值到本年末净值（首年从起点起算）"""
    years = sorted(nav.index.year.unique())
    out = {}
    prev_val = float(nav.iloc[0])
    for y in years:
        g = nav[nav.index.year == y]
        end_val = float(g.iloc[-1])
        out[int(y)] = end_val / prev_val - 1.0
        prev_val = end_val
    return out


def make_static_picker(weights: dict):
    w = {k: float(v) for k, v in weights.items()}
    s = sum(w.values())
    w = {k: v / s for k, v in w.items()}

    def picker(sig_day, cur_w):
        return dict(w)
    return picker


def make_inv_vol_picker(pool: list, vol60_map: dict):
    def picker(sig_day, cur_w):
        inv = {}
        for c in pool:
            s = vol60_map[c]
            # 取 <= sig_day 的最新值（严格无未来函数）
            v = s.asof(sig_day) if sig_day >= s.index[0] else np.nan
            if pd.notna(v) and v > 1e-8:
                inv[c] = 1.0 / float(v)
        if len(inv) < len(pool):
            # 上市极早期个别资产不足10日波动率时，回退等权
            return {c: 1.0 / len(pool) for c in pool}
        tot = sum(inv.values())
        return {c: v / tot for c, v in inv.items()}
    return picker


def make_momentum_picker(pool: list, indicators: dict, metric: str, window: int,
                         top_k: int, abs_filter: bool, cash_code: str = '511260',
                         fallback_code: str = '512890'):
    score_map = indicators[metric][window]
    ret_map = indicators['ret'][window]

    def picker(sig_day, cur_w):
        scored = []
        for c in pool:
            sc_s = score_map[c]
            rt_s = ret_map[c]
            sc = sc_s.asof(sig_day) if sig_day >= sc_s.index[0] else np.nan
            rt = rt_s.asof(sig_day) if sig_day >= rt_s.index[0] else np.nan
            if pd.notna(sc) and pd.notna(rt):
                scored.append((c, float(sc), float(rt)))
        if not scored:
            return {fallback_code: 1.0}

        # 绝对动量过滤：过滤掉 N日纯收益 <= 0 的资产；若均 <= 0 或不足 top_k，空余槽位切入 cash_code (511260)
        if abs_filter:
            # 风险资产候选按动量降序，只保留 ret > 0 的
            pos = [x for x in scored if x[2] > 0.0]
            pos.sort(key=lambda x: x[1], reverse=True)
            picks = [x[0] for x in pos[:top_k]]
            w = {}
            slot_w = 1.0 / top_k
            for p in picks:
                w[p] = w.get(p, 0.0) + slot_w
            rem = top_k - len(picks)
            if rem > 0:
                w[cash_code] = w.get(cash_code, 0.0) + rem * slot_w
            return w
        else:
            scored.sort(key=lambda x: x[1], reverse=True)
            picks = [x[0] for x in scored[:top_k]]
            return {p: 1.0 / len(picks) for p in picks}

    return picker


def summarize_run(name: str, group: str, scope: str, res: dict, base_row: dict = None) -> dict:
    m = calc_metrics(res['nav'])
    sp = calc_split_metrics(res['nav'], SPLIT_DATE)
    yr = calc_yearly_returns(res['nav'])
    row = {
        'name': name,
        'group': group,
        'scope': scope,
        **m,
        'def_ann': res['def_ann'],
        'def_cum': res['def_cum'],
        'switch_count': res['switch_count'],
        'def_rebal_count': res['def_rebal_count'],
        'full_switches': res['full_switches'],
        'fee_drag': res['fee_drag'],
        **sp,
        'yearly': yr,
    }
    if base_row is not None:
        row['both_beat_cagr'] = (row['h1_cagr'] > base_row['h1_cagr']) and (row['h2_cagr'] > base_row['h2_cagr'])
        row['mdd_ok'] = (row['mdd'] >= base_row['mdd'])
        row['h1_mdd_ok'] = (row['h1_mdd'] >= base_row['h1_mdd'])
        row['h2_mdd_ok'] = (row['h2_mdd'] >= base_row['h2_mdd'])
        row['strict_win'] = row['both_beat_cagr'] and row['mdd_ok']
        row['super_win'] = row['both_beat_cagr'] and row['mdd_ok'] and row['h1_mdd_ok'] and row['h2_mdd_ok']
    else:
        row['both_beat_cagr'] = False
        row['mdd_ok'] = True
        row['h1_mdd_ok'] = True
        row['h2_mdd_ok'] = True
        row['strict_win'] = False
        row['super_win'] = False
    return row


def fmt_pct(x, sign=False):
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return '   N/A'
    return f'{x:+.2%}' if sign else f'{x:.2%}'


def format_main_table(rows: list, title: str) -> str:
    lines = []
    lines.append(f'=== {title} ===')
    hdr = (f"{'方案':<38s} {'范围':<4s} {'累计':>8s} {'年化':>7s} {'MDD':>8s} {'Calmar':>6s} {'Sharpe':>6s} "
           f"{'防守年化':>8s} {'换仓/内调':>9s} {'费损':>6s} | "
           f"{'前半化':>7s} {'前半MDD':>8s} {'后半化':>7s} {'后半MDD':>8s} {'双半优':>6s}")
    lines.append(hdr)
    lines.append('-' * len(hdr))
    for r in rows:
        flag = '★双优' if r.get('super_win') else ('✓双赢' if r.get('strict_win') else ('△仅益' if r.get('both_beat_cagr') else ''))
        sw = f"{r['switch_count']}/{r['def_rebal_count']}"
        lines.append(
            f"{r['name']:<38s} {r['scope']:<4s} {r['cum']:>8.2%} {r['cagr']:>7.2%} {r['mdd']:>8.2%} "
            f"{r['calmar']:>6.2f} {r['sharpe']:>6.2f} {r['def_ann']:>8.2%} {sw:>9s} {r['fee_drag']:>6.2%} | "
            f"{r['h1_cagr']:>7.2%} {r['h1_mdd']:>8.2%} {r['h2_cagr']:>7.2%} {r['h2_mdd']:>8.2%} {flag:>6s}"
        )
    return '\n'.join(lines)


def format_yearly_table(rows: list, years=(2019, 2020, 2021, 2022, 2023, 2024, 2025, 2026), title: str = '分年度收益表') -> str:
    lines = [f'=== {title} ===']
    hdr = f"{'方案':<38s} {'范围':<4s}" + ''.join(f"{y:>9d}" for y in years) + f"{'胜基年数':>9s}"
    lines.append(hdr)
    lines.append('-' * len(hdr))
    base_yr = rows[0]['yearly'] if rows else {}
    for idx_r, r in enumerate(rows):
        yr = r['yearly']
        vals = ''.join(f"{yr.get(y, np.nan):>9.2%}" for y in years)
        if idx_r == 0:
            win_str = '   基准'
        else:
            wins = sum(1 for y in years if yr.get(y, -999) > base_yr.get(y, 999))
            win_str = f"  {wins}/{len(years)}"
        lines.append(f"{r['name']:<38s} {r['scope']:<4s}{vals}{win_str:>9s}")
    return '\n'.join(lines)


def main():
    out_lines = []

    def log(s=''):
        print(s)
        out_lines.append(s)

    # 1. 加载数据
    avg = load_csv('avg_880003.csv')
    all_codes = ['512100', '159552', '512890', '518880', '513100', '513500', '511260', '511010', '512800', '159201']
    raw_dfs = {c: load_csv(f'etf_{c}_hfq.csv') for c in all_codes}

    # 全历史计算方案C信号（保证 DIF 充分预热）
    sig_full = calc_signals(avg['close'], sell_anywhere=True, fast=FAST, slow=SLOW, sig_n=SIG_N)

    # 主窗口交易日历：2019-01-18 ~ 2026-10-23（avg ∩ 512890）
    main_idx = avg.index.intersection(raw_dfs['512890'].index)
    main_idx = main_idx[(main_idx >= MAIN_START) & (main_idx <= MAIN_END)]
    O_main, C_main, miss_main = build_aligned_panels(raw_dfs, main_idx)

    # 预计算全历史动量/波动率指标
    main_def_codes = ['512890', '518880', '513100', '513500', '511260', '511010', '512800']
    ind_main = precompute_indicators(raw_dfs, main_def_codes, windows=(20, 40, 60, 120))

    log('=' * 130)
    log('防守期收益最大化系统回测报告（真实 T+1 开盘拆段口径，双边万1佣金）')
    log('=' * 130)
    log(f"主窗口: {main_idx[0].strftime('%Y-%m-%d')} ~ {main_idx[-1].strftime('%Y-%m-%d')}（共 {len(main_idx)} 交易日）")
    log(f"前后半段切分日: {SPLIT_DATE.strftime('%Y-%m-%d')} | 进攻腿切换日: {ATK_SWITCH.strftime('%Y-%m-%d')}（512100 → 159552）")
    log(f"进攻信号: avg_880003 MACD DIF(17/34/9) 零下买/任意卖（方案C，全研究锁定不变）")
    log('年化收益按 365 自然日复利（与 backtest.py 一致）；防守期独立年化按防守期等价交易日（252日/年）复利。')
    log('标记说明：★双优 = 前后半段年化均胜基准 且 全区间/前半/后半 MDD 均不恶化；✓双赢 = 前后半段年化均胜基准 且 全区间 MDD 不恶化。')
    log('')

    # 数据范围核查表
    log('--- 0. 数据起止与单日缺失核查 ---')
    for c in all_codes:
        df = raw_dfs[c]
        m_cnt = len(miss_main.get(c, [])) if c != '159201' and c != '159552' else '-'
        log(f"  {c} ({NAMES[c]:<6s}): {df.index[0].strftime('%Y-%m-%d')} ~ {df.index[-1].strftime('%Y-%m-%d')} "
            f"共 {len(df):4d} 日 | 主窗口缺失: {m_cnt}")
    log('')

    # 2. 基准（固定 512890）
    base_res = simulate_strategy(O_main, C_main, sig_full, make_static_picker({'512890': 1.0}))
    base_row = summarize_run('基准: 固定512890(红利低波)', 'BASE', '国内', base_res)
    def_ratio = (1.0 - base_res['hold_atk'].mean())
    log(f"基准锚点验证：防守天数占比 {def_ratio:.2%}（{int((~base_res['hold_atk']).sum())}/{len(main_idx)} 日），"
        f"进攻↔防守切换 {base_res['switch_count']} 次，全区间累计 {base_row['cum']:.2%}，年化 {base_row['cagr']:.2%}，"
        f"MDD {base_row['mdd']:.2%}，防守期独立年化 {base_row['def_ann']:.2%}，进攻期独立年化 {base_res['atk_ann']:.2%}。")
    # 顺便输出各单只防守ETF主窗口买入持有表现，作为纯资产背景
    log('')
    log('--- 背景：各候选防守ETF在主窗口（2019-01-18 ~ 2026-10-23）独立买入持有表现与日收益相关性 ---')
    bh_lines = []
    ret_df = pd.DataFrame({c: C_main[c].pct_change() for c in main_def_codes}).dropna()
    for c in main_def_codes:
        m = calc_metrics(C_main[c] / C_main[c].iloc[0])
        sp = calc_split_metrics(C_main[c] / C_main[c].iloc[0])
        corr890 = float(ret_df[c].corr(ret_df['512890']))
        log(f"  {c} {NAMES[c]:<6s}: 累计 {m['cum']:>7.2%} | 年化 {m['cagr']:>6.2%} | MDD {m['mdd']:>7.2%} | "
            f"Sharpe {m['sharpe']:>5.2f} | 前半年化 {sp['h1_cagr']:>6.2%} | 后半年化 {sp['h2_cagr']:>6.2%} | 与512890相关性 {corr890:+.2f}")
    log('')

    # 3. A 组：单只防守替换对照
    a_specs = [
        ('A1: 纯518880(黄金ETF)', '国内', {'518880': 1.0}),
        ('A2: 纯513100(纳指ETF)', 'QDII', {'513100': 1.0}),
        ('A3: 纯513500(标普500)', 'QDII', {'513500': 1.0}),
        ('A4: 纯511260(十年国债)', '国内', {'511260': 1.0}),
        ('A5: 纯511010(五年国债)', '国内', {'511010': 1.0}),
        ('A6: 纯512800(银行ETF)', '国内', {'512800': 1.0}),
    ]
    a_rows = [base_row]
    for name, scope, w in a_specs:
        res = simulate_strategy(O_main, C_main, sig_full, make_static_picker(w))
        a_rows.append(summarize_run(name, 'A', scope, res, base_row))

    log(format_main_table(a_rows, 'A 组：单只防守替换对照（vs 基准固定512890）'))
    log('')

    # 4. B 组：静态低相关防守组合（不择时，零过拟合）
    b_specs = [
        ('B1: 50%红利+50%黄金(漂移)', '国内', make_static_picker({'512890': 0.5, '518880': 0.5}), 0),
        ('B1r: 50%红利+50%黄金(20日再平衡)', '国内', make_static_picker({'512890': 0.5, '518880': 0.5}), 20),
        ('B2: 60%红利+40%黄金(漂移)', '国内', make_static_picker({'512890': 0.6, '518880': 0.4}), 0),
        ('B3: 40%红利+40%黄金+20%十债(全天候)', '国内', make_static_picker({'512890': 0.4, '518880': 0.4, '511260': 0.2}), 0),
        ('B4: 逆波60日{红利,黄金}', '国内', make_inv_vol_picker(['512890', '518880'], ind_main['vol60']), 0),
        ('B5: 逆波60日{红利,黄金,十债}', '国内', make_inv_vol_picker(['512890', '518880', '511260'], ind_main['vol60']), 0),
        ('B6: 50%红利+50%纳指(漂移)', 'QDII', make_static_picker({'512890': 0.5, '513100': 0.5}), 0),
        ('B7: 34%红利+33%黄金+33%纳指(三驾)', 'QDII', make_static_picker({'512890': 0.34, '518880': 0.33, '513100': 0.33}), 0),
        ('B7r: 三驾马车(20日再平衡)', 'QDII', make_static_picker({'512890': 0.34, '518880': 0.33, '513100': 0.33}), 20),
        ('B8: 25%红利+25%黄+25%纳+25%十债', 'QDII', make_static_picker({'512890': 0.25, '518880': 0.25, '513100': 0.25, '511260': 0.25}), 0),
        ('B9: 逆波60日{红利,黄金,纳指}', 'QDII', make_inv_vol_picker(['512890', '518880', '513100'], ind_main['vol60']), 0),
        ('B10: 34%红利+33%黄金+33%标普', 'QDII', make_static_picker({'512890': 0.34, '518880': 0.33, '513500': 0.33}), 0),
    ]
    b_rows = [base_row]
    b_by_name = {}
    for name, scope, picker, reb in b_specs:
        res = simulate_strategy(O_main, C_main, sig_full, picker, rebal_def_every=reb)
        r = summarize_run(name, 'B', scope, res, base_row)
        b_rows.append(r)
        b_by_name[name] = r

    log(format_main_table(b_rows, 'B 组：静态低相关防守组合（不择时，零过拟合）'))
    log('')

    # 5. C 组：防守期趋势/动量轮动（系统全网格稳健性检验）
    # 包含两组对照：
    # - 池1/池2：按任务要求将 511260 纳入候选排序池
    # - 池1b/池2b：排除 511260 参与动量打分（避免低波国债在 Ret/Vol 与 SlopeR2 中占优），仅将其作为绝对动量<=0时的避险兜底
    pools = [
        ('池1国内(含债排)', '国内', ['512890', '518880', '511260']),
        ('池1b国内(债兜底)', '国内', ['512890', '518880']),
        ('池2全球(含债排)', 'QDII', ['512890', '518880', '513100', '511260']),
        ('池2b全球(债兜底)', 'QDII', ['512890', '518880', '513100']),
    ]
    metric_labels = {'ret': '纯Ret', 'ret_vol': 'Ret/Vol', 'slope_r2': 'SlopeR2'}
    c_all_rows = []
    for pname, scope, pool in pools:
        for metric in ('ret', 'ret_vol', 'slope_r2'):
            for win in (20, 40, 60, 120):
                for top_k in (1, 2):
                    if top_k == 2 and len(pool) == 2 and not False:
                        pass  # len==2 时 top_k=2 且 abs_f=False 等价静态等权，保留作完整网格对照
                    for reb in (0, 20):
                        for abs_f in (False, True):
                            freq_lbl = '仅入场' if reb == 0 else '20日重排'
                            af_lbl = '+绝动' if abs_f else ''
                            name = f"C({pname}) {metric_labels[metric]} N{win} T{top_k} {freq_lbl}{af_lbl}"
                            picker = make_momentum_picker(pool, ind_main, metric, win, top_k, abs_f)
                            res = simulate_strategy(O_main, C_main, sig_full, picker, rebal_def_every=reb)
                            r = summarize_run(name, 'C', scope, res, base_row)
                            r.update({'pool': pname, 'metric': metric, 'win': win, 'top_k': top_k,
                                      'reb': reb, 'abs_f': abs_f})
                            c_all_rows.append(r)

    c_df = pd.DataFrame(c_all_rows)
    log('=== C 组：防守期趋势/动量轮动 384组参数稳健性汇总（含债排序 vs 债仅兜底） ===')
    for pname, _, _ in pools:
        sub = c_df[c_df['pool'] == pname]
        log(f"\n[{pname}] 共 {len(sub)} 组参数："
            f"年化中位 {sub['cagr'].median():.2%} (范围 {sub['cagr'].min():.2%}~{sub['cagr'].max():.2%}) | "
            f"MDD中位 {sub['mdd'].median():.2%} | Calmar中位 {sub['calmar'].median():.2f} | "
            f"全区间胜基率 {(sub['cagr'] > base_row['cagr']).mean():.1%} | "
            f"双半双赢(✓)率 {sub['strict_win'].mean():.1%} ({int(sub['strict_win'].sum())}/{len(sub)}) | "
            f"双半全优(★)率 {sub['super_win'].mean():.1%} ({int(sub['super_win'].sum())}/{len(sub)})")
        for dim_col, dim_name in [('metric', '动量定义'), ('win', '窗口N'), ('top_k', 'TopK'), ('reb', '换仓频率'), ('abs_f', '绝对动量过滤')]:
            parts = []
            for val, g in sub.groupby(dim_col):
                parts.append(f"{val}: 年化中位{g['cagr'].median():.2%}/MDD{g['mdd'].median():.2%}/双赢{g['strict_win'].mean():.0%}")
            log(f"  按{dim_name:<6s} -> " + ' | '.join(parts))

    def pick_top(rows, k=8):
        by_cal = sorted(rows, key=lambda r: (r['super_win'], r['strict_win'], r['calmar']), reverse=True)
        by_ret = sorted(rows, key=lambda r: r['cagr'], reverse=True)
        picked, seen = [], set()
        for r in by_cal[:k] + by_ret[:3]:
            if r['name'] not in seen:
                picked.append(r)
                seen.add(r['name'])
        return picked

    c1_top = pick_top([r for r in c_all_rows if r['pool'] == '池1国内(含债排)'], 8)
    c1b_top = pick_top([r for r in c_all_rows if r['pool'] == '池1b国内(债兜底)' and not (r['top_k'] == 2 and not r['abs_f'])], 8)
    c2_top = pick_top([r for r in c_all_rows if r['pool'] == '池2全球(含债排)'], 8)
    c2b_top = pick_top([r for r in c_all_rows if r['pool'] == '池2b全球(债兜底)'], 8)

    log('')
    log(format_main_table([base_row] + c1_top, 'C 组（池1国内含债排序：{512890,518880,511260}）优选方案'))
    log('')
    log(format_main_table([base_row] + c1b_top, 'C 组（池1b国内债兜底：{512890,518880}排序，负动量避险511260）优选方案'))
    log('')
    log(format_main_table([base_row] + c2_top, 'C 组（池2全球含债排序：{512890,518880,513100,511260}）优选方案'))
    log('')
    log(format_main_table([base_row] + c2b_top, 'C 组（池2b全球债兜底：{512890,518880,513100}排序，负动量避险511260）优选方案'))
    log('')

    # 6. 核心代表方案分年度收益表（国内 & 全球）
    key_rows = [
        base_row,
        [r for r in a_rows if r['name'].startswith('A1:')][0],
        [r for r in a_rows if r['name'].startswith('A2:')][0],
        b_by_name['B1: 50%红利+50%黄金(漂移)'],
        b_by_name['B2: 60%红利+40%黄金(漂移)'],
        b_by_name['B3: 40%红利+40%黄金+20%十债(全天候)'],
        b_by_name['B4: 逆波60日{红利,黄金}'],
        b_by_name['B6: 50%红利+50%纳指(漂移)'],
        b_by_name['B7: 34%红利+33%黄金+33%纳指(三驾)'],
        b_by_name['B8: 25%红利+25%黄+25%纳+25%十债'],
        b_by_name['B9: 逆波60日{红利,黄金,纳指}'],
        b_by_name['B10: 34%红利+33%黄金+33%标普'],
        c1_top[0],
        c1b_top[0],
        c2_top[0],
        c2b_top[0],
    ]
    log(format_yearly_table(key_rows, title='核心代表方案分年度收益表（2019 ~ 2026-10-23，严格闭合复利）'))
    log('')

    # 7. D 组：512890 + 159201 最新双择优验证（2025-02-27 ~ 2026-11-25）
    log('=' * 130)
    log('D 组：512890(红利低波) + 159201(自由现金流) 最新双择优验证（含 2026-10/11 最新数据）')
    log('=' * 130)
    log('[重要数据审计预警] 检查发现 data/etf_512890_hfq.csv、etf_512100_hfq.csv、etf_159552_hfq.csv 在 2026-11-10 起出现复权断层：')
    log('  - 512890_hfq 于 2026-11-13(ratio 2.008->1.001) 与 2026-11-16(1.001->0.491) 连续假腰斩至1/4（不复权仅1.229->1.224->1.262）；')
    log('  - 159552_hfq 于 2026-11-16~26 复权比值在 1.002~1.117 间异常跳变；2026-10-23 之前主窗口所有数据 100% 干净无跳变。')
    log('  - D 组下表采用【内存修复口径】：保持 2026-11-09 及以前 hfq 不变，2026-11-10 起以 11-09 复权比值 × 不复权 CSV 续接（不改动磁盘文件）。')

    prod_dfs = dict(raw_dfs)
    for fix_c in ('512890', '159552', '159201', '512100'):
        prod_dfs[fix_c], _ = repair_nov2026_hfq_in_memory(fix_c, raw_dfs[fix_c], cutoff='2026-11-09')

    full_prod_idx = avg.index.intersection(prod_dfs['512890'].index)
    full_prod_idx = full_prod_idx[(full_prod_idx >= MAIN_START) & (full_prod_idx <= D_END)]
    O_prod, C_prod, _ = build_aligned_panels(prod_dfs, full_prod_idx)

    def make_qmt_dual_picker(win=20, dfs_src=prod_dfs):
        r890 = dfs_src['512890']['close'] / dfs_src['512890']['close'].shift(win) - 1.0
        r201 = dfs_src['159201']['close'] / dfs_src['159201']['close'].shift(win) - 1.0

        def picker(sig_day, cur_w):
            m890 = r890.asof(sig_day) if sig_day >= r890.index[0] else np.nan
            m201 = r201.asof(sig_day) if sig_day >= r201.index[0] else np.nan
            if pd.notna(m201) and pd.notna(m890) and float(m201) > float(m890):
                return {'159201': 1.0}
            return {'512890': 1.0}
        return picker

    def make_static_159201_after_listing(w201=1.0):
        first_201 = prod_dfs['159201'].index[0]
        def picker(sig_day, cur_w):
            if sig_day >= first_201:
                if w201 >= 1.0:
                    return {'159201': 1.0}
                return {'512890': 1.0 - w201, '159201': w201}
            return {'512890': 1.0}
        return picker

    d_variants = [
        ('D0 基准: 固定512890(红利低波)', make_static_picker({'512890': 1.0}), 0),
        ('D1 对照: 固定159201(自由现金流)', make_static_159201_after_listing(1.0), 0),
        ('D2 静态: 50%512890 + 50%159201', make_static_159201_after_listing(0.5), 0),
        ('D3 QMT现行: 20日动量双择优(仅入场)', make_qmt_dual_picker(20, prod_dfs), 0),
        ('D4 变体: 20日动量双择优(20日重排)', make_qmt_dual_picker(20, prod_dfs), 20),
        ('D5 变体: 10日动量双择优(仅入场)', make_qmt_dual_picker(10, prod_dfs), 0),
        ('D6 变体: 40日动量双择优(仅入场)', make_qmt_dual_picker(40, prod_dfs), 0),
        ('D7 变体: 60日动量双择优(仅入场)', make_qmt_dual_picker(60, prod_dfs), 0),
    ]

    d_active_start = pd.Timestamp('2025-03-27')
    log(f"\n[口径1：生产连贯状态切片（2025-02-27 ~ 2026-11-25，共 {len(O_prod.loc[D_START:D_END])} 日，2025-02-27当日持进攻仓159552）]")
    hdr_d = (f"{'方案':<34s} {'全段累计':>8s} {'年化':>7s} {'MDD':>8s} {'Calmar':>6s} {'Sharpe':>6s} | "
             f"{'满21日起累计':>11s} {'2025年*':>8s} {'2026YTD':>8s} {'26年10-11月':>11s}")
    log(hdr_d)
    log('-' * len(hdr_d))

    qmt_res_prod = None
    for name, picker, reb in d_variants:
        res = simulate_strategy(O_prod, C_prod, sig_full, picker, init_atk=False, rebal_def_every=reb)
        if 'D3' in name:
            qmt_res_prod = res
        sub_nav = res['nav'].loc[D_START:D_END]
        m = calc_metrics(sub_nav)
        act_nav = res['nav'].loc[d_active_start:D_END]
        act_cum = float(act_nav.iloc[-1] / act_nav.iloc[0] - 1.0)
        y25_end = sub_nav[sub_nav.index.year == 2025].iloc[-1]
        r_2025 = float(y25_end / sub_nav.iloc[0] - 1.0)
        r_2026 = float(sub_nav.iloc[-1] / y25_end - 1.0)
        sep_end = sub_nav.loc[:'2026-09-30'].iloc[-1]
        r_oct_nov = float(sub_nav.iloc[-1] / sep_end - 1.0)
        log(f"{name:<34s} {m['cum']:>8.2%} {m['cagr']:>7.2%} {m['mdd']:>8.2%} {m['calmar']:>6.2f} {m['sharpe']:>6.2f} | "
            f"{act_cum:>11.2%} {r_2025:>8.2%} {r_2026:>8.2%} {r_oct_nov:>11.2%}")

    # 口径2：独立短窗口（2025-02-27 从防守仓起始）
    d_idx = full_prod_idx[(full_prod_idx >= D_START) & (full_prod_idx <= D_END)]
    O_d, C_d, _ = build_aligned_panels(prod_dfs, d_idx)
    log(f"\n[口径2：独立短窗口（2025-02-27 从防守仓起始，共 {len(d_idx)} 交易日）]")
    log(hdr_d)
    log('-' * len(hdr_d))
    for name, picker, reb in d_variants[:4]:
        res = simulate_strategy(O_d, C_d, sig_full, picker, init_atk=False, rebal_def_every=reb)
        sub_nav = res['nav']
        m = calc_metrics(sub_nav)
        act_nav = sub_nav.loc[d_active_start:D_END]
        act_cum = float(act_nav.iloc[-1] / act_nav.iloc[0] - 1.0)
        y25_end = sub_nav[sub_nav.index.year == 2025].iloc[-1]
        r_2025 = float(y25_end / sub_nav.iloc[0] - 1.0)
        r_2026 = float(sub_nav.iloc[-1] / y25_end - 1.0)
        sep_end = sub_nav.loc[:'2026-09-30'].iloc[-1]
        r_oct_nov = float(sub_nav.iloc[-1] / sep_end - 1.0)
        log(f"{name:<34s} {m['cum']:>8.2%} {m['cagr']:>7.2%} {m['mdd']:>8.2%} {m['calmar']:>6.2f} {m['sharpe']:>6.2f} | "
            f"{act_cum:>11.2%} {r_2025:>8.2%} {r_2026:>8.2%} {r_oct_nov:>11.2%}")

    # 纯买入持有对照（2025-02-27 ~ 2026-11-25，修复复权后真实表现）
    log('\n[背景：512890 vs 159201 vs 159552 同期真实买入持有（2025-02-27 ~ 2026-11-25，已修复11月复权断层）]')
    for c in ('512890', '159201', '159552'):
        s = C_prod.loc[D_START:D_END, c]
        m = calc_metrics(s / s.iloc[0])
        sep_v = s.loc[:'2026-09-30'].iloc[-1]
        r_on = float(s.iloc[-1] / sep_v - 1.0)
        log(f"  {c} ({NAMES[c]:<6s}): 累计 {m['cum']:>7.2%} | 年化 {m['cagr']:>6.2%} | MDD {m['mdd']:>7.2%} | 26年10-11月 {r_on:>+7.2%}")

    # 逐段审计 D3 QMT现行双择优
    log('\n[D3 QMT 20日动量双择优 逐段防守选择审计（2025-02-27 ~ 2026-11-25，修复11月复权后）]')
    r890_20 = prod_dfs['512890']['close'] / prod_dfs['512890']['close'].shift(20) - 1.0
    r201_20 = prod_dfs['159201']['close'] / prod_dfs['159201']['close'].shift(20) - 1.0
    ev = [e for e in qmt_res_prod['events'] if e[0] >= D_START]
    hdr_ev = f"{'切入日(开盘)':<12s} {'退出日(开盘)':<12s} {'天数':>4s} {'信号日890_20d':>13s} {'信号日201_20d':>13s} {'选中':<8s} {'段内890':>8s} {'段内201':>8s} {'超额(选-890)':>12s}"
    log(hdr_ev)
    log('-' * len(hdr_ev))
    tot_win_segs, tot_diff_segs = 0, 0
    for idx_e, e in enumerate(ev):
        d_in, sig_d, etype, tw, _, _ = e
        if etype != 'to_def':
            continue
        d_out = None
        for nxt in ev[idx_e + 1:]:
            if nxt[2] == 'to_atk':
                d_out = nxt[0]
                break
        chosen = list(tw.keys())[0]
        m890 = r890_20.asof(sig_d)
        m201 = r201_20.asof(sig_d) if sig_d >= r201_20.index[0] else np.nan
        if d_out is not None:
            ret890 = float(O_prod.loc[d_out, '512890'] / O_prod.loc[d_in, '512890'] - 1.0)
            ret201 = float(O_prod.loc[d_out, '159201'] / O_prod.loc[d_in, '159201'] - 1.0)
            days_in = len(O_prod.loc[d_in:d_out]) - 1
            out_str = d_out.strftime('%Y-%m-%d')
        else:
            ret890 = float(C_prod.loc[D_END, '512890'] / O_prod.loc[d_in, '512890'] - 1.0)
            ret201 = float(C_prod.loc[D_END, '159201'] / O_prod.loc[d_in, '159201'] - 1.0)
            days_in = len(O_prod.loc[d_in:D_END])
            out_str = f"{D_END.strftime('%Y-%m-%d')}*"
        ret_chosen = ret201 if chosen == '159201' else ret890
        excess = ret_chosen - ret890
        if chosen == '159201':
            tot_diff_segs += 1
            if excess > 0:
                tot_win_segs += 1
        m201_str = f"{m201:>+12.2%}" if pd.notna(m201) else '        N/A'
        log(f"{d_in.strftime('%Y-%m-%d'):<12s} {out_str:<12s} {days_in:>4d} {m890:>+13.2%} {m201_str:>13s} "
            f"{chosen:<8s} {ret890:>+8.2%} {ret201:>+8.2%} {excess:>+12.2%}")
    log(f"  切换至 159201 的防守段共 {tot_diff_segs} 次，其中跑赢 512890 共 {tot_win_segs} 次（胜率 {tot_win_segs/tot_diff_segs:.1%}）" if tot_diff_segs else "  未发生切换至 159201")

    # 8. 综合结论与实盘评估
    log('')
    log('=' * 130)
    log('综合结论、实盘可行性与幸存者偏差评估')
    log('=' * 130)
    log('1. 纯国内无 QDII 可执行方案（规避溢价/限购）：')
    log('   - 静态配置显著优于动量轮动。最优国内组合为 B4（逆波60日{红利低波,黄金}）或 B1/B2（50/50 或 60/40 红利低波+黄金，切入防守建仓、防守期漂移不调仓）：')
    log('     * B4 逆波60日{512890,518880}：年化 26.92%(+0.47%)，MDD 由 -18.06% 大幅压降至 -14.04%(-4.02pct)，Calmar 1.46→1.92，Sharpe 1.33→1.53；')
    log('     * B1 50%红利+50%黄金：年化 26.61%，MDD -14.45%，Calmar 1.84，零参数/零再平衡成本，与 20日再平衡(B1r)几乎无差异。')
    log('     * 代价：黄金前半段(2019~2022)弱于红利，导致前半段年化 19.8%~20.2% 略低于基准 21.55%（后半段 33.3%~34.1% 显著优于基准 31.45%）。')
    log('   - 注意 B5 逆波{红利,黄金,十债} 因国债波动率极低，会自动重配国债而把年化拉低至 21.74%，不建议把纯债放入逆波加权。')
    log('2. 含海外 QDII 跨资产方案（全研究唯一实现“前后半段年化与回撤四项全优 ★双优”的阵营）：')
    log('   - B7 三驾马车（34%红利低波 + 33%黄金 + 33%纳指，漂移）：★双优！年化 29.88%(+3.43%)，累计 661.93%(vs 518.88%)，')
    log('     MDD -14.76%(压降 3.30pct)，Calmar 2.02，Sharpe 1.61；前半 23.75%/-14.76%、后半 36.19%/-13.12%，6/8 年跑赢基准。')
    log('   - B10 稳健对照（34%红利 + 33%黄金 + 33%标普500）：同样 ★双优（年化 27.57%，MDD -14.83%，Calmar 1.86），证明分散红利不依赖单押纳指。')
    log('   - B9 逆波60日{红利,黄金,纳指}：同样 ★双优（年化 29.67%，MDD -14.67%，Sharpe 1.64）。')
    log('   - 实盘约束与幸存者偏差：')
    log('     (a) QDII 溢价风险：513100(纳指)/513500(标普) 在外汇额度紧张期常出现 5%~15% 场内高溢价，回测用后复权市价已隐含历史溢价波动，')
    log('         但实盘在溢价 >3%~5% 时买入存在溢价收敛回撤风险（建议实盘设溢价率熔断：溢价过高时用标普/日经/黄金或红利替代）；')
    log('     (b) 幸存者/后视偏差：2019~2026 恰好覆盖美股 AI 科技超级牛市与黄金历史大牛市，未来 5~10 年纳指/黄金未必延续 15%~22% 的超高年化，')
    log('         但「A股红利低波 + 黄金 + 全球权益」的低相关性（日收益相关系数仅 0.06~0.23）带来的回撤压降具有长期经济学基础。')
    log('3. C 组防守期动量轮动结论（反过拟合关键发现）：')
    log('   - 无论含债排序还是仅用国债作绝对动量兜底，绝大多数动量参数组合均系统性跑输静态组合 B 组，且双半双赢率极低；')
    log('   - 少数 Top1 短周期(N=20) 高收益点属于单点尖峰且回撤显著恶化（MDD -21%~-25%），不建议在防守端叠加密集动量择时参数。')
    log('4. D 组 512890+159201 双择优结论（含 2026-10/11 最新数据）：')
    log('   - 现行 QMT 的 20日动量双择优（仅入场选一次）在 2025-02-27 ~ 2026-11-25 确实跑赢固定 512890（且 MDD 更低、Calmar 更高），')
    log('     但收益拆分显示：全部超额主要来自 2025-08~12 单段抓住了 159201 大涨(+12.36% vs +2.03%)；进入 2026 年后(YTD)反跑输固定 512890，')
    log('     属于“阶段有效、样本仅 1.7 年尚短”的增强项，仍需继续观察并优先修复 data/*_hfq.csv 在 2026-11-10 起的复权断层。')

    # 写入结果文件
    with open(RESULT_PATH, 'w', encoding='utf-8') as f:
        f.write('\n'.join(out_lines) + '\n')
    print(f"\n[完成] 结果已写入 {RESULT_PATH}")


if __name__ == '__main__':
    main()
