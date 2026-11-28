#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
导出网站数据：平均股价(880003) + T1 / T2+ / T3 / T3+ 四策略完整回测与信号
====================================================================
四策略统一采用增强信号（S1 + S2）：
  - 基础买点：880003 MACD-DIF(17/34) 零轴下方拐头向上
  - 基础卖点：880003 MACD-DIF(17/34) 任意位置拐头向下
  - S1 零下回落过滤：DIF > 0 拐头即卖；DIF <= 0 须满足 (近20日最高DIF - 当日DIF)/close > 0.0010 才卖
  - S2 风格比值防洗盘：进攻/防守连续比值 R 的 DIF(12/26) 处于零上上行时屏蔽卖出

四策略标的配置与执行口径（2024-06-28 前进攻腿以 512100 拼接历史，此后切至实盘标的）：
  - T1  ：进攻 100% 159552 ｜ 防守 100% 512890 ｜ 买卖均 T+1 开盘
  - T2+ ：进攻 60% 159552 + 40% 588220 ｜ 防守 100% 512890 ｜ 买 T+1 开盘、卖 T 日尾盘
  - T3  ：进攻 100% 159552 ｜ 防守 34% 512890 + 33% 518880 + 33% 513100 ｜ 买卖均 T+1 开盘
  - T3+ ：进攻 60% 159552 + 40% 588220 ｜ 防守 34% 512890 + 33% 518880 + 33% 513100 ｜ 买 T+1 开盘、卖 T 日尾盘

收益按后复权真实成交拆段计算（含双边万1佣金），价格展示使用不复权真实价。
输出 site/data.json
"""
import sys
import io
import os
import json

import numpy as np
import pandas as pd

if sys.platform == 'win32' and sys.stdout is not None and not getattr(sys.stdout, '_utf8_wrapped', False):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stdout._utf8_wrapped = True

from backtest import ema
import market_data as md

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, 'data')
SITE_DIR = os.path.join(BASE, 'site')
os.makedirs(SITE_DIR, exist_ok=True)

SWITCH_ATK = pd.Timestamp('2024-06-28')
FEE_ONEWAY = 0.0001
S1_WIN = 20
S1_TH = 0.0010
S2_FAST = 12
S2_SLOW = 26

ETF_NAMES = {
    '512100': '中证1000ETF',
    '159552': '中证2000增强',
    '588220': '科创100ETF',
    '512890': '红利低波ETF',
    '518880': '黄金ETF',
    '513100': '纳指ETF',
}

STRATEGY_SPECS = [
    {
        'id': 'T1',
        'name': 'T1 · 纯信号升级',
        'short_name': 'T1 纯信号升级',
        'badge': 'S1+S2 · T+1开盘',
        'offense_mode': 'single',
        'defense_mode': '512890',
        'exec_mode': 't1_open',
        'offense_desc': '100% 159552 中证2000增强',
        'defense_desc': '100% 512890 红利低波',
        'exec_desc': '买卖均 T+1 开盘成交',
        'summary': '零新增标的，仅升级 S1+S2 卖出防洗盘过滤',
    },
    {
        'id': 'T2+',
        'name': 'T2+ · 纯国内无杠杆',
        'short_name': 'T2+ 纯国内全栈',
        'badge': 'S1+S2 · A3尾盘卖',
        'offense_mode': 'dual_60_40',
        'defense_mode': '512890',
        'exec_mode': 'a3_asym',
        'offense_desc': '60% 159552 + 40% 588220',
        'defense_desc': '100% 512890 红利低波',
        'exec_desc': '买 T+1 开盘 · 卖 T 日尾盘',
        'summary': '纯国内极简防守，微盘/科创双核 + 单一红利低波防守',
    },
    {
        'id': 'T3',
        'name': 'T3 · 全球风险收益比',
        'short_name': 'T3 全球低波优选',
        'badge': 'S1+S2 · T+1开盘',
        'offense_mode': 'single',
        'defense_mode': 'glb_333',
        'exec_mode': 't1_open',
        'offense_desc': '100% 159552 中证2000增强',
        'defense_desc': '34% 512890 + 33% 518880 + 33% 513100',
        'exec_desc': '买卖均 T+1 开盘成交',
        'summary': '无杠杆 Calmar 天花板，红利低波+黄金+纳指三资产防守',
    },
    {
        'id': 'T3+',
        'name': 'T3+ · 全球全栈最高',
        'short_name': 'T3+ 全球全栈',
        'badge': 'S1+S2 · A3尾盘卖',
        'offense_mode': 'dual_60_40',
        'defense_mode': 'glb_333',
        'exec_mode': 'a3_asym',
        'offense_desc': '60% 159552 + 40% 588220',
        'defense_desc': '34% 512890 + 33% 518880 + 33% 513100',
        'exec_desc': '买 T+1 开盘 · 卖 T 日尾盘',
        'summary': '无杠杆最高收益全栈配置（513100 高溢价可切 513500）',
    },
]


def load(name: str) -> pd.DataFrame:
    df = pd.read_csv(os.path.join(DATA_DIR, name), parse_dates=['date'])
    df = df.set_index('date').sort_index()
    df.index = df.index.normalize()
    return df


def _status() -> dict:
    try:
        with open(os.path.join(DATA_DIR, '_status.json'), encoding='utf-8') as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def format_weights(weights: dict) -> str:
    """将权重字典格式化为清晰代码标签，如 '60% 159552 + 40% 588220' 或 '159552 中证2000增强'"""
    items = [(c, w) for c, w in weights.items() if w > 1e-6]
    if len(items) == 1:
        c = items[0][0]
        return f'{c} {ETF_NAMES.get(c, c)}'
    return ' + '.join(f'{round(w * 100)}% {c}' for c, w in items)


def format_weights_full(weights: dict) -> str:
    items = [(c, w) for c, w in weights.items() if w > 1e-6]
    if len(items) == 1:
        c = items[0][0]
        return f'100% {c}({ETF_NAMES.get(c, c)})'
    return ' + '.join(f'{round(w * 100)}% {c}({ETF_NAMES.get(c, c)})' for c, w in items)


def build_continuous_atk_nav(hfq: dict, idx: pd.DatetimeIndex) -> pd.Series:
    """构造 512100 -> 159552 无缝连续进攻净值（供 S2 风格比值 R-DIF 计算）"""
    a1 = hfq['512100'].reindex(idx).ffill()
    a2 = hfq['159552'].reindex(idx).ffill()
    vals = [1.0]
    for i in range(1, len(idx)):
        d = idx[i]
        if d < SWITCH_ATK:
            r = float(a1['close'].iloc[i]) / float(a1['close'].iloc[i - 1])
        elif d == SWITCH_ATK:
            r = (float(a1['open'].iloc[i]) / float(a1['close'].iloc[i - 1])
                 * float(a2['close'].iloc[i]) / float(a2['open'].iloc[i]))
        else:
            r = float(a2['close'].iloc[i]) / float(a2['close'].iloc[i - 1])
        vals.append(vals[-1] * r)
    return pd.Series(vals, index=idx)


def compute_enhanced_signals(avg: pd.DataFrame, hfq: dict, idx: pd.DatetimeIndex) -> pd.DataFrame:
    """在全历史 avg 上计算 DIF(17/34/9) 与 S1+S2 增强信号"""
    close = avg['close']
    dif = ema(close, 17) - ema(close, 34)
    dea = ema(dif, 9)
    macdval = (dif - dea) * 2
    dif_p1 = dif.shift(1)
    dif_p2 = dif.shift(2)
    dif_up = (dif_p1 < dif_p2) & (dif > dif_p1)
    dif_dn = (dif_p1 > dif_p2) & (dif < dif_p1)
    rel_drop = (dif.rolling(S1_WIN, min_periods=1).max() - dif) / close

    df = pd.DataFrame({
        'dif': dif,
        'dea': dea,
        'macdval': macdval,
        'dif_up': dif_up,
        'dif_dn': dif_dn,
        'rel_drop': rel_drop,
    }).loc[idx].copy()

    # S2: 进攻/防守相对强弱比值 R 的 DIF(12/26)
    atk_nav = build_continuous_atk_nav(hfq, idx)
    div_c = hfq['512890'].reindex(idx).ffill()['close']
    ratio = atk_nav / (div_c / div_c.iloc[0])
    r_dif = ema(ratio, S2_FAST) - ema(ratio, S2_SLOW)
    r_strong = (r_dif > 0) & (r_dif > r_dif.shift(1))

    df['r_dif'] = r_dif
    df['r_strong'] = r_strong.fillna(False)
    df['buy_sig'] = df['dif_up'] & (df['dif'] < 0)
    df['raw_sell_sig'] = df['dif_dn']
    df['s1_pass'] = (df['dif'] > 0) | (df['rel_drop'] > S1_TH)
    df['s1_blocked'] = df['raw_sell_sig'] & (~df['s1_pass'])
    df['s2_blocked'] = df['raw_sell_sig'] & df['s1_pass'] & df['r_strong']
    df['sell_sig'] = df['raw_sell_sig'] & df['s1_pass'] & (~df['r_strong'])
    return df


def offense_weights(day: pd.Timestamp, mode: str) -> dict:
    if day < SWITCH_ATK:
        return {'512100': 1.0}
    if mode == 'dual_60_40':
        return {'159552': 0.6, '588220': 0.4}
    return {'159552': 1.0}


def defense_weights(mode: str) -> dict:
    if mode == '512890':
        return {'512890': 1.0}
    if mode == 'cn_gold':
        return {'512890': 0.5, '518880': 0.5}
    if mode == 'glb_333':
        return {'512890': 0.34, '518880': 0.33, '513100': 0.33}
    raise ValueError(mode)


def simulate_strategy(spec: dict, sig: pd.DataFrame, hfq: dict, raw: dict,
                      idx: pd.DatetimeIndex) -> dict:
    """运行单个策略的精确拆段回测，输出 nav_strat / hold_spans / trades / legs / last_signal"""
    dates = [d.strftime('%Y-%m-%d') for d in idx]
    n = len(idx)

    o_hfq, c_hfq, o_raw, c_raw = {}, {}, {}, {}
    for code in ETF_NAMES:
        dfh = hfq[code].reindex(idx).ffill()
        dfr = raw[code].reindex(idx).ffill()
        o_hfq[code] = dfh['open'].values.astype(float)
        c_hfq[code] = dfh['close'].values.astype(float)
        o_raw[code] = dfr['open'].values.astype(float)
        c_raw[code] = dfr['close'].values.astype(float)

    buy_arr = sig['buy_sig'].values.astype(bool)
    sell_arr = sig['sell_sig'].values.astype(bool)
    exec_mode = spec['exec_mode']
    off_mode = spec['offense_mode']
    def_mode = spec['defense_mode']

    state = False  # False=防守, True=进攻
    init_w = defense_weights(def_mode)
    weights = dict(init_w)
    target_w = dict(init_w)
    nav = np.ones(n, dtype=float)
    hold_atk = np.zeros(n, dtype=bool)
    pending = None

    trades = []
    legs = []

    def price_summary(w_dict: dict, px_map: dict, i_pos: int):
        items = [(c, w) for c, w in w_dict.items() if w > 1e-6]
        if len(items) == 1:
            c = items[0][0]
            v = round(float(px_map[c][i_pos]), 3)
            return v, f'{v:.3f}'
        parts = [f'{c}:{float(px_map[c][i_pos]):.3f}' for c, _ in items]
        return round(float(px_map[items[0][0]][i_pos]), 3), ' / '.join(parts)

    def build_codes_detail(w_dict: dict, buy_px_map: dict, buy_i: int, cur_px_map: dict, cur_i: int):
        res = []
        for c, w in w_dict.items():
            if w <= 1e-6:
                continue
            bp = float(buy_px_map[c][buy_i])
            cp = float(cur_px_map[c][cur_i])
            res.append({
                'code': c,
                'name': ETF_NAMES.get(c, c),
                'weight': round(float(w), 4),
                'buy_price': round(bp, 3),
                'last_price': round(cp, 3),
            })
        return res

    leg_start_i = 0
    leg_start_nav = 1.0
    leg_entry_px_map = c_raw
    leg_asset = 'div'
    leg_target_w = dict(init_w)

    for i in range(1, n):
        d = idx[i]
        did_open = False
        new_w = None
        is_trade = False

        if pending == 'buy' and not state:
            new_w = offense_weights(d, off_mode)
            did_open = True
            is_trade = True
            state = True
            pending = None
        elif pending == 'sell' and state:
            new_w = defense_weights(def_mode)
            did_open = True
            is_trade = True
            state = False
            pending = None
        elif d == SWITCH_ATK and state:
            new_w = offense_weights(d, off_mode)
            did_open = True
            is_trade = False
        else:
            pending = None

        if did_open:
            r_ovn = sum(w * (o_hfq[c][i] / c_hfq[c][i - 1] - 1.0) for c, w in weights.items())
            w_open = nav[i - 1] * (1.0 + r_ovn)
            if is_trade:
                w_open *= (1.0 - 2.0 * FEE_ONEWAY)
                leg_ret = w_open / leg_start_nav - 1.0
                si = i - 1
                bp_val, bp_txt = price_summary(leg_target_w, leg_entry_px_map, leg_start_i)
                sp_val, sp_txt = price_summary(leg_target_w, o_raw, i)
                legs.append({
                    'asset': leg_asset,
                    'asset_name': format_weights(leg_target_w),
                    'buy_date': dates[leg_start_i],
                    'buy_price': bp_val,
                    'buy_price_text': bp_txt,
                    'sell_date': dates[i],
                    'sell_price': sp_val,
                    'sell_price_text': sp_txt,
                    'days': i - leg_start_i + 1,
                    'ret': round(leg_ret, 4),
                    'cum': round(w_open - 1.0, 4),
                    'open': False,
                })
                nb_val, nb_txt = price_summary(new_w, o_raw, i)
                trades.append({
                    'date': dates[i],
                    'sig_date': dates[si],
                    'action': 'buy' if state else 'sell',
                    'exec_timing': 'T+1开盘',
                    'sell_desc': format_weights(leg_target_w),
                    'buy_desc': format_weights(new_w),
                    'asset_name': format_weights(new_w if state else leg_target_w),
                    'price': nb_val,
                    'price_text': nb_txt,
                    'dif': round(float(sig['dif'].iloc[si]), 4),
                    'dea': round(float(sig['dea'].iloc[si]), 4),
                    'macdval': round(float(sig['macdval'].iloc[si]), 4),
                    'leg_ret': round(leg_ret, 4),
                    'cum_ret': round(w_open - 1.0, 4),
                })
                leg_start_i = i
                leg_start_nav = w_open
                leg_entry_px_map = o_raw
                leg_asset = '1000' if state else 'div'
                leg_target_w = dict(new_w)
            else:
                leg_target_w = dict(new_w)

            r_intra = sum(w * (c_hfq[c][i] / o_hfq[c][i] - 1.0) for c, w in new_w.items())
            nav[i] = w_open * (1.0 + r_intra)
            denom = sum(w * (c_hfq[c][i] / o_hfq[c][i]) for c, w in new_w.items())
            weights = {c: (w * (c_hfq[c][i] / o_hfq[c][i])) / denom for c, w in new_w.items()}
            target_w = dict(new_w)
        else:
            r_day = sum(w * (c_hfq[c][i] / c_hfq[c][i - 1] - 1.0) for c, w in weights.items())
            nav[i] = nav[i - 1] * (1.0 + r_day)
            denom = sum(w * (c_hfq[c][i] / c_hfq[c][i - 1]) for c, w in weights.items())
            weights = {c: (w * (c_hfq[c][i] / c_hfq[c][i - 1])) / denom for c, w in weights.items()}

        # 当日收盘前状态记入持仓区间（A3 尾盘卖出当天仍属进攻段最后一日，次日起转防守）
        hold_atk[i] = state

        # 收盘信号判断
        if buy_arr[i] and not state:
            pending = 'buy'
        elif sell_arr[i] and state:
            if exec_mode == 'a3_asym':
                new_w = defense_weights(def_mode)
                nav[i] *= (1.0 - 2.0 * FEE_ONEWAY)
                leg_ret = nav[i] / leg_start_nav - 1.0
                bp_val, bp_txt = price_summary(leg_target_w, leg_entry_px_map, leg_start_i)
                sp_val, sp_txt = price_summary(leg_target_w, c_raw, i)
                legs.append({
                    'asset': leg_asset,
                    'asset_name': format_weights(leg_target_w),
                    'buy_date': dates[leg_start_i],
                    'buy_price': bp_val,
                    'buy_price_text': bp_txt,
                    'sell_date': dates[i],
                    'sell_price': sp_val,
                    'sell_price_text': sp_txt,
                    'days': i - leg_start_i + 1,
                    'ret': round(leg_ret, 4),
                    'cum': round(nav[i] - 1.0, 4),
                    'open': False,
                })
                nb_val, nb_txt = price_summary(new_w, c_raw, i)
                trades.append({
                    'date': dates[i],
                    'sig_date': dates[i],
                    'action': 'sell',
                    'exec_timing': 'T日尾盘',
                    'sell_desc': format_weights(leg_target_w),
                    'buy_desc': format_weights(new_w),
                    'asset_name': format_weights(leg_target_w),
                    'price': nb_val,
                    'price_text': nb_txt,
                    'dif': round(float(sig['dif'].iloc[i]), 4),
                    'dea': round(float(sig['dea'].iloc[i]), 4),
                    'macdval': round(float(sig['macdval'].iloc[i]), 4),
                    'leg_ret': round(leg_ret, 4),
                    'cum_ret': round(nav[i] - 1.0, 4),
                })
                leg_start_i = i
                leg_start_nav = nav[i]
                leg_entry_px_map = c_raw
                leg_asset = 'div'
                leg_target_w = dict(new_w)
                weights = dict(new_w)
                target_w = dict(new_w)
                state = False
                pending = None
            else:
                pending = 'sell'

    # 尾段当前持仓（open=True）
    bp_val, bp_txt = price_summary(leg_target_w, leg_entry_px_map, leg_start_i)
    cp_val, cp_txt = price_summary(leg_target_w, c_raw, n - 1)
    open_ret = nav[-1] / leg_start_nav - 1.0
    legs.append({
        'asset': leg_asset,
        'asset_name': format_weights(leg_target_w),
        'asset_full': format_weights_full(leg_target_w),
        'codes': build_codes_detail(leg_target_w, leg_entry_px_map, leg_start_i, c_raw, n - 1),
        'buy_date': dates[leg_start_i],
        'buy_price': bp_val,
        'buy_price_text': bp_txt,
        'sell_date': None,
        'sell_price': cp_val,
        'sell_price_text': cp_txt,
        'days': n - leg_start_i,
        'ret': round(open_ret, 4),
        'cum': round(nav[-1] - 1.0, 4),
        'open': True,
    })

    # 持仓区间（进攻仓连续段）
    spans = []
    start_i = None
    for i, h in enumerate(hold_atk):
        if h and start_i is None:
            start_i = i
        elif not h and start_i is not None:
            spans.append([dates[start_i], dates[i - 1]])
            start_i = None
    if start_i is not None:
        spans.append([dates[start_i], dates[-1]])

    # 最新收盘信号详情
    last_i = n - 1
    next_off = offense_weights(idx[last_i], off_mode)
    next_def = defense_weights(def_mode)
    blocked_reason = ''
    if sig['s1_blocked'].iloc[last_i]:
        blocked_reason = 'S1 零下回落不足 0.0010（屏蔽假拐头）'
    elif sig['s2_blocked'].iloc[last_i]:
        blocked_reason = 'S2 风格比值 R-DIF(12/26) 零上走强（屏蔽洗盘）'

    last_sig_action = 'none'
    if buy_arr[last_i] and not state:
        last_sig_action = 'buy'
    elif sell_arr[last_i] and state:
        last_sig_action = 'sell'

    # 摘要指标（便于前端顶部四策略卡片直接展示）
    nav_s = pd.Series(nav, index=idx)
    dr = nav_s.pct_change().dropna()
    years = len(idx) / 252.0
    cum_ret = float(nav[-1] - 1.0)
    ann_ret = float(nav[-1] ** (1.0 / years) - 1.0) if years > 0 else 0.0
    peak = nav_s.cummax()
    mdd = float(((nav_s - peak) / peak).min())
    calmar = ann_ret / abs(mdd) if mdd < 0 else 0.0
    sharpe = float(dr.mean() / dr.std() * np.sqrt(252)) if dr.std() > 0 else 0.0
    cur_yr = idx[-1].year
    yr_mask = idx.year == cur_yr
    first_yr_pos = int(np.argmax(yr_mask))
    prev_yr_nav = float(nav[first_yr_pos - 1]) if first_yr_pos > 0 else 1.0
    ytd = float(nav[-1] / prev_yr_nav - 1.0)

    return {
        'id': spec['id'],
        'name': spec['name'],
        'short_name': spec['short_name'],
        'badge': spec['badge'],
        'exec_mode': exec_mode,
        'offense_desc': spec['offense_desc'],
        'defense_desc': spec['defense_desc'],
        'exec_desc': spec['exec_desc'],
        'summary': spec['summary'],
        'metrics': {
            'cum_ret': round(cum_ret, 4),
            'ann_ret': round(ann_ret, 4),
            'mdd': round(mdd, 4),
            'calmar': round(calmar, 2),
            'sharpe': round(sharpe, 2),
            'trades': len(trades),
            'ytd': round(ytd, 4),
            'final_nav': round(float(nav[-1]), 4),
        },
        'nav_strat': [round(float(v), 4) for v in nav],
        'hold_spans': spans,
        'trades': trades,
        'legs': legs,
        'last_signal': {
            'date': dates[last_i],
            'action': last_sig_action,
            'blocked_reason': blocked_reason,
            'next_offense': format_weights(next_off),
            'next_defense': format_weights(next_def),
        },
    }


def main():
    avg = load('avg_880003.csv')
    hfq = {c: load(f'etf_{c}_hfq.csv') for c in ETF_NAMES}
    raw = {c: load(f'etf_{c}.csv') for c in ETF_NAMES}

    # 公共交易日：与 512890 / 518880 / 513100 / avg / 进攻拼接交集对齐
    atk_idx = hfq['512100'].index[hfq['512100'].index < SWITCH_ATK].union(
        hfq['159552'].index[hfq['159552'].index >= SWITCH_ATK]
    )
    idx = (avg.index
           .intersection(hfq['512890'].index)
           .intersection(hfq['518880'].index)
           .intersection(hfq['513100'].index)
           .intersection(atk_idx))

    sig = compute_enhanced_signals(avg, hfq, idx)

    strategies = {}
    for spec in STRATEGY_SPECS:
        strategies[spec['id']] = simulate_strategy(spec, sig, hfq, raw, idx)

    dates = [d.strftime('%Y-%m-%d') for d in idx]

    def series(df):
        s = df.reindex(idx).ffill()['close']
        return [round(float(v), 3) for v in s.values]

    def series_k(df):
        al = df.reindex(idx).ffill()
        return [[round(float(a), 3), round(float(b), 3), round(float(l), 3), round(float(h), 3)]
                for a, b, l, h in zip(al['open'].values, al['close'].values, al['low'].values, al['high'].values)]

    def series_sparse(df):
        s = df['close']
        return [round(float(s[d]), 3) if d in s.index else None for d in idx]

    defense_pick = None

    st = _status()
    etf_last = max(md.completed_daily(hfq['512890']).index[-1],
                   md.completed_daily(hfq['159552']).index[-1])
    avg_stale = bool(st.get('avg_stale') or avg.index[-1] < etf_last)

    # 下一交易日盘中分时买点拐点价预告（基于最新收盘 EMA17/34，开盘前即已知）
    avg_c_all = avg['close']
    e17_all = ema(avg_c_all, 17)
    e34_all = ema(avg_c_all, 34)
    dif_all = e17_all - e34_all
    a17, a34 = 2.0 / 18.0, 2.0 / 35.0
    last_avg_c = float(avg_c_all.iloc[-1])
    dif_last = float(dif_all.iloc[-1])
    dif_prev = float(dif_all.iloc[-2]) if len(dif_all) > 1 else dif_last
    c_next = (1.0 - a17) * float(e17_all.iloc[-1]) - (1.0 - a34) * float(e34_all.iloc[-1])
    x_turn_next = (dif_last - c_next) / (a17 - a34)
    x_turn_buf_next = x_turn_next * 1.005
    x_zero_next = (0.0 - c_next) / (a17 - a34)
    intraday_buy = {
        'base_date': avg.index[-1].strftime('%Y-%m-%d'),
        'base_close': round(last_avg_c, 2),
        'dif_last': round(dif_last, 4),
        'dif_prev': round(dif_prev, 4),
        'dif_below_zero': bool(dif_last < 0),
        'dif_falling': bool(dif_last < dif_prev),
        'eligible_buy': bool(dif_last < 0 and dif_last < dif_prev),
        'x_turn': round(x_turn_next, 2),
        'x_turn_pct': round(x_turn_next / last_avg_c - 1.0, 4) if last_avg_c > 0 else None,
        'x_turn_buf': round(x_turn_buf_next, 2),
        'x_turn_buf_pct': round(x_turn_buf_next / last_avg_c - 1.0, 4) if last_avg_c > 0 else None,
        'x_zero': round(x_zero_next, 2),
        'x_zero_pct': round(x_zero_next / last_avg_c - 1.0, 4) if last_avg_c > 0 else None,
        'buf_pct': 0.005,
        'checkpoints': ['10:30', '11:30', '14:00'],
    }

    # 顶层保留 T1 的核心字段，保证 live.py 等旧读取方无缝兼容
    t1 = strategies['T1']
    out = {
        'dates': dates,
        'avg': series(avg),
        'avg_k': series_k(avg),
        'etf1000': series(raw['512100']),
        'etfdiv': series(raw['512890']),
        'etf1000_hfq': series(hfq['512100']),
        'etfdiv_hfq': series(hfq['512890']),
        'etf2000e': series_sparse(raw['159552']),
        'etf2000e_hfq': series_sparse(hfq['159552']),
        'etf588220': series_sparse(raw['588220']),
        'etf518880': series(raw['518880']),
        'etf513100': series(raw['513100']),
        'switch_atk': SWITCH_ATK.strftime('%Y-%m-%d'),
        'dif': [round(float(v), 4) for v in sig['dif'].values],
        'dea': [round(float(v), 4) for v in sig['dea'].values],
        's1_rel_drop': [round(float(v), 5) if not pd.isna(v) else None for v in sig['rel_drop'].values],
        's1_threshold': S1_TH,
        'r_dif': [round(float(v), 4) if not pd.isna(v) else None for v in sig['r_dif'].values],
        'r_strong': [bool(v) for v in sig['r_strong'].values],
        # 顶层默认镜像 T1（兼容 live.py）
        'trades': t1['trades'],
        'legs': t1['legs'],
        'hold_spans': t1['hold_spans'],
        'nav_strat': t1['nav_strat'],
        # 四策略完整数据
        'default_strategy': 'T1',
        'strategy_order': [s['id'] for s in STRATEGY_SPECS],
        'strategies': strategies,
        'defense_pick': defense_pick,
        'intraday_buy': intraday_buy,
        'updated': pd.Timestamp.now().strftime('%Y-%m-%d %H:%M'),
        'avg_date': avg.index[-1].strftime('%Y-%m-%d'),
        'avg_source': st.get('avg_source', ''),
        'avg_stale': avg_stale,
        'data_errors': st.get('errors', []),
    }

    path = os.path.join(SITE_DIR, 'data.json')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False)
    print(f'已导出 {path}  ({len(dates)} 个交易日, 截至 {dates[-1]})')
    for sid in out['strategy_order']:
        m = strategies[sid]['metrics']
        print(f"  {sid:<4} 累计 {m['cum_ret']*100:>6.1f}% | 年化 {m['ann_ret']*100:>5.2f}% | "
              f"MDD {m['mdd']*100:>6.2f}% | Calmar {m['calmar']:>4.2f} | 换仓 {m['trades']:>3d} 次")


if __name__ == '__main__':
    main()
