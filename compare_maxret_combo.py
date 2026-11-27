#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
多方向最优策略组合交叉验证 & 稳健性压力测试
====================================================================
整合五大独立研究方向中通过反过拟合检验的正交真 Alpha 组件：
  S1 : 零下 DIF 回落幅度过滤 — DIF<=0 时要求 (max20(DIF)-DIF)/close > 0.0010 才卖；DIF>0 拐头即卖
  S2 : 风格比值防洗盘 — 880003 触发卖出时，若进攻/防守连续比值 R 的 DIF(12/26) 仍零上上行，屏蔽卖出
  D_CN  : 纯国内零参数防守 — 50% 512890(红利低波) + 50% 518880(黄金)（及 60日逆波加权对照）
  D_GLB : 全球零参数三驾马车防守 — 34% 512890 + 33% 518880 + 33% 513100(纳指)（及标普513500对照）
  D_201 : 现行 QMT 双择优防守 — 512890 / 159201 20日动量择优（2025-02-27起生效）
  O_2K  : 进攻双核 — 2024-06-28 起 60% 159552(中证2000增强) + 40% 588220(科创100)（及 50/50、逆波对照）
  E_A3  : 非对称执行 — 买在 T+1 开盘，卖在 T 日尾盘（同时测卖端 +0.05% 冲击滑点）
  LEV   : 仅进攻期温和杠杆（1.3x / 1.5x，严扣年化 5.5% 融资利息）

严格口径：
  - 真实成交拆段（旧仓昨收→今开 + 新仓今开→今收），按实际换手扣双边万1（及压力测试档）
  - 2024-06-28 拼接日无缝衔接，绝无跨标的失真
  - 2026-11-10 起在内存中用 2026-11-09 复权因子 × 不复权 OHLC 修复数据断层（不改动磁盘文件）
"""
import os
import sys
import io
import numpy as np
import pandas as pd

if sys.platform == 'win32' and not getattr(sys.stdout, '_utf8_wrapped', False):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stdout._utf8_wrapped = True

from backtest import ema, annualized, max_drawdown

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, 'data')
SWITCH_ATK = pd.Timestamp('2024-06-28')
SPLIT_DATE = pd.Timestamp('2022-11-22')
CLEAN_ANCHOR = pd.Timestamp('2026-11-09')
OUT_PATH = os.path.join(BASE, 'result_maxret_combo.txt')


def load_csv(name: str) -> pd.DataFrame:
    df = pd.read_csv(os.path.join(DATA_DIR, name), parse_dates=['date'])
    df = df.set_index('date').sort_index()
    df.index = df.index.normalize()
    return df


def load_etf_clean(code: str) -> pd.DataFrame:
    """加载 ETF 后复权数据；若日期超过 2026-11-09 且存在不复权文件，在内存中按 11-09 固定复权因子续接"""
    hfq = load_csv(f'etf_{code}_hfq.csv')
    raw_path = os.path.join(DATA_DIR, f'etf_{code}.csv')
    if hfq.index[-1] > CLEAN_ANCHOR and os.path.exists(raw_path) and CLEAN_ANCHOR in hfq.index:
        raw = load_csv(f'etf_{code}.csv')
        if CLEAN_ANCHOR in raw.index:
            factor = float(hfq.loc[CLEAN_ANCHOR, 'close']) / float(raw.loc[CLEAN_ANCHOR, 'close'])
            before = hfq.loc[hfq.index <= CLEAN_ANCHOR].copy()
            after_raw = raw.loc[raw.index > CLEAN_ANCHOR].copy()
            after = after_raw.copy()
            for col in ['open', 'high', 'low', 'close']:
                if col in after.columns:
                    after[col] = after_raw[col] * factor
            common_cols = [c for c in before.columns if c in after.columns]
            return pd.concat([before[common_cols], after[common_cols]]).sort_index()
    return hfq


def build_continuous_nav(assets: dict, idx: pd.DatetimeIndex, switch_date=SWITCH_ATK) -> pd.Series:
    """构造 512100 -> 159552 连续无跳变价格序列（用于 R 比值信号计算）"""
    a1 = assets['512100'].reindex(idx).ffill()
    a2 = assets['159552'].reindex(idx).ffill()
    vals = [1.0]
    for i in range(1, len(idx)):
        d = idx[i]
        if d < switch_date:
            r = float(a1['close'].iloc[i]) / float(a1['close'].iloc[i - 1])
        elif d == switch_date:
            r = (float(a1['open'].iloc[i]) / float(a1['close'].iloc[i - 1])
                 * float(a2['close'].iloc[i]) / float(a2['open'].iloc[i]))
        else:
            r = float(a2['close'].iloc[i]) / float(a2['close'].iloc[i - 1])
        vals.append(vals[-1] * r)
    return pd.Series(vals, index=idx)


def build_signals(avg: pd.DataFrame, assets: dict, idx: pd.DatetimeIndex,
                  use_s1: bool = False, s1_th: float = 0.0010, s1_w: int = 20,
                  use_s2: bool = False, s2_fast: int = 12, s2_slow: int = 26) -> pd.DataFrame:
    """在全历史 avg 上计算 DIF(17/34/9)，并可选叠加 S1(零下回落过滤) 与 S2(R-DIF防洗盘)"""
    close = avg['close']
    dif = ema(close, 17) - ema(close, 34)
    dea = ema(dif, 9)
    dif_p1 = dif.shift(1)
    dif_p2 = dif.shift(2)
    dif_up = (dif_p1 < dif_p2) & (dif > dif_p1)
    dif_dn = (dif_p1 > dif_p2) & (dif < dif_p1)

    buy_sig = dif_up & (dif < 0)
    sell_sig = dif_dn.copy()

    if use_s1:
        rel_drop = (dif.rolling(s1_w, min_periods=1).max() - dif) / close
        # 零上坚持拐头即卖，仅零下要求回落幅度 > s1_th
        sell_sig = sell_sig & ((dif > 0) | (rel_drop > s1_th))

    sig = pd.DataFrame({'dif': dif, 'dea': dea, 'buy_sig': buy_sig, 'sell_sig': sell_sig}).loc[idx].copy()

    if use_s2:
        atk_nav = build_continuous_nav(assets, idx)
        div_c = assets['512890'].reindex(idx).ffill()['close']
        ratio = atk_nav / (div_c / div_c.iloc[0])
        r_dif = ema(ratio, s2_fast) - ema(ratio, s2_slow)
        r_strong = (r_dif > 0) & (r_dif > r_dif.shift(1))
        sig['sell_sig'] = sig['sell_sig'] & (~r_strong)

    return sig


def get_offense_weights(day: pd.Timestamp, mode: str, assets: dict, idx: pd.DatetimeIndex, pos: int) -> dict:
    """返回进攻腿权重字典 {code: weight}"""
    if mode == 'base':
        return {'512100': 1.0} if day < SWITCH_ATK else {'159552': 1.0}
    elif mode == 'dual_60_40':
        if day < SWITCH_ATK:
            return {'512100': 1.0}
        return {'159552': 0.6, '588220': 0.4}
    elif mode == 'dual_50_50':
        if day < SWITCH_ATK:
            return {'512100': 1.0}
        return {'159552': 0.5, '588220': 0.5}
    elif mode == 'dual_invvol':
        if day < SWITCH_ATK:
            return {'512100': 1.0}
        # 用截至前一交易日(信号日)的20日波动率倒数加权
        sig_pos = max(pos - 1, 0)
        sig_day = idx[sig_pos]
        v1 = assets['159552']['close'].pct_change().loc[:sig_day].tail(20).std()
        v2 = assets['588220']['close'].pct_change().loc[:sig_day].tail(20).std()
        if pd.isna(v1) or pd.isna(v2) or v1 <= 0 or v2 <= 0:
            return {'159552': 0.6, '588220': 0.4}
        w1 = (1.0 / v1) / (1.0 / v1 + 1.0 / v2)
        return {'159552': w1, '588220': 1.0 - w1}
    raise ValueError(mode)


def get_defense_weights(sig_day: pd.Timestamp, mode: str, assets: dict) -> dict:
    """返回防守腿权重字典 {code: weight}（基于信号日 sig_day 决策，无前视）"""
    if mode == '512890':
        return {'512890': 1.0}
    elif mode == 'cn_50_50':
        return {'512890': 0.5, '518880': 0.5}
    elif mode == 'cn_invvol':
        v1 = assets['512890']['close'].pct_change().loc[:sig_day].tail(60).std()
        v2 = assets['518880']['close'].pct_change().loc[:sig_day].tail(60).std()
        if pd.isna(v1) or pd.isna(v2) or v1 <= 0 or v2 <= 0:
            return {'512890': 0.5, '518880': 0.5}
        w1 = (1.0 / v1) / (1.0 / v1 + 1.0 / v2)
        return {'512890': w1, '518880': 1.0 - w1}
    elif mode == 'glb_333':
        return {'512890': 0.34, '518880': 0.33, '513100': 0.33}
    elif mode == 'glb_spx':
        return {'512890': 0.34, '518880': 0.33, '513500': 0.33}
    elif mode == 'glb_invvol':
        codes = ['512890', '518880', '513100']
        ivs = {}
        for c in codes:
            v = assets[c]['close'].pct_change().loc[:sig_day].tail(60).std()
            ivs[c] = 1.0 / v if (not pd.isna(v) and v > 0) else 0.0
        s = sum(ivs.values())
        if s <= 0:
            return {'512890': 0.34, '518880': 0.33, '513100': 0.33}
        return {c: ivs[c] / s for c in codes}
    elif mode == 'qmt_201':
        if '159201' not in assets or sig_day not in assets['159201'].index:
            return {'512890': 1.0}
        s1 = assets['512890']['close'].loc[:sig_day]
        s2 = assets['159201']['close'].loc[:sig_day]
        if len(s2) < 21 or len(s1) < 21:
            return {'512890': 1.0}
        m1 = float(s1.iloc[-1]) / float(s1.iloc[-21]) - 1
        m2 = float(s2.iloc[-1]) / float(s2.iloc[-21]) - 1
        return {'512890': 1.0} if m1 >= m2 else {'159201': 1.0}
    elif mode == 'cn_201_gold':
        # 50% (512890/159201 20日动量择优) + 50% 黄金
        sub = get_defense_weights(sig_day, 'qmt_201', assets)
        w = {k: v * 0.5 for k, v in sub.items()}
        w['518880'] = 0.5
        return w
    elif mode == 'glb_201_333':
        # 34% (512890/159201 20日动量择优) + 33% 黄金 + 33% 纳指
        sub = get_defense_weights(sig_day, 'qmt_201', assets)
        w = {k: v * 0.34 for k, v in sub.items()}
        w['518880'] = 0.33
        w['513100'] = 0.33
        return w
    raise ValueError(mode)


def simulate(sig: pd.DataFrame, assets: dict, idx: pd.DatetimeIndex,
             offense_mode: str = 'base', defense_mode: str = '512890',
             exec_mode: str = 't1_open',
             fee_oneway: float = 0.0001, sell_close_slip: float = 0.0,
             atk_leverage: float = 1.0, fin_rate: float = 0.055) -> dict:
    """
    通用多资产精确拆段回测引擎：
      - exec_mode == 't1_open': 买/卖均于 T+1 开盘执行（旧仓昨收→今开，扣费用，建新仓今开→今收）
      - exec_mode == 'a3_asym': 买入于 T+1 开盘执行；卖出于 T 日收盘执行（T 日持仓收盘后立刻以收盘价切换至防守，
        可额外叠加 sell_close_slip 模拟尾盘冲击成本）
      - 2024-06-28 若处于进攻态，当日开盘自动完成 512100 -> 159552(或双核) 无缝切换（不计额外策略换仓，无跳变失真）
      - 防守多资产组合在防守期内漂移持有，不额外日度再平衡
    """
    # 预先对齐所有资产的 open/close 面板
    opens = {}
    closes = {}
    for code, df in assets.items():
        al = df.reindex(idx).ffill()
        opens[code] = al['open'].values.astype(float)
        closes[code] = al['close'].values.astype(float)

    buy_arr = sig['buy_sig'].values.astype(bool)
    sell_arr = sig['sell_sig'].values.astype(bool)
    n = len(idx)

    state = False  # False=防守, True=进攻
    weights = get_defense_weights(idx[0], defense_mode, assets)
    nav = np.ones(n, dtype=float)
    pending = None  # 'buy' or 'sell'
    trade_count = 0
    atk_days = 0
    cross_switch = False
    daily_fin_cost = (atk_leverage - 1.0) * fin_rate / 252.0 if atk_leverage > 1.0 else 0.0

    # 记录每笔进攻段用于统计打脸段
    atk_segs = []
    seg_start_i = None
    seg_start_nav = None

    for i in range(1, n):
        d = idx[i]
        # 1) 检查今日开盘是否有待执行切换（或 2024-06-28 拼接切换）
        did_open_switch = False
        new_weights = None
        is_strategy_trade = False

        if pending == 'buy' and not state:
            new_weights = get_offense_weights(d, offense_mode, assets, idx, i)
            did_open_switch = True
            is_strategy_trade = True
            state = True
            pending = None
        elif pending == 'sell' and state:
            new_weights = get_defense_weights(idx[i - 1], defense_mode, assets)
            did_open_switch = True
            is_strategy_trade = True
            state = False
            pending = None
        elif d == SWITCH_ATK and state:
            # 2024-06-28 持进攻仓时，开盘无缝从 512100 换到新进攻仓，避免跨标的跳变
            cross_switch = True
            new_weights = get_offense_weights(d, offense_mode, assets, idx, i)
            did_open_switch = True
            is_strategy_trade = False
        else:
            pending = None

        if did_open_switch:
            # 旧仓隔夜段 (昨收 -> 今开)
            r_ovn = sum(w * (opens[c][i] / closes[c][i - 1] - 1.0) for c, w in weights.items())
            lev_old = atk_leverage if (not state if is_strategy_trade else True) else 1.0
            # 注意：若刚从防守切进攻(state变True)，隔夜持的是防守(lev=1)；若从进攻切防守(state变False)，隔夜持的是进攻(lev=atk_leverage)
            if is_strategy_trade:
                lev_old = 1.0 if state else atk_leverage
                lev_new = atk_leverage if state else 1.0
            else:
                lev_old = atk_leverage
                lev_new = atk_leverage

            w_open = nav[i - 1] * (1.0 + lev_old * r_ovn)
            if is_strategy_trade:
                # 扣双边交易费用（按买入后和卖出前名义杠杆取较大端近似，或按 2*fee_oneway*max(lev_old, lev_new)）
                # 精确：卖旧仓名义 lev_old * fee_oneway + 买新仓名义 lev_new * fee_oneway
                cost_rate = (lev_old + lev_new) * fee_oneway
                w_open *= (1.0 - cost_rate)
                trade_count += 1
                if state:
                    seg_start_i = i
                    seg_start_nav = w_open
                else:
                    if seg_start_i is not None:
                        atk_segs.append((idx[seg_start_i], d, i - seg_start_i, w_open / seg_start_nav - 1.0))
                        seg_start_i = None

            # 新仓日内段 (今开 -> 今收)
            r_intra = sum(w * (closes[c][i] / opens[c][i] - 1.0) for c, w in new_weights.items())
            nav[i] = w_open * (1.0 + lev_new * r_intra)
            if lev_new > 1.0:
                nav[i] *= (1.0 - daily_fin_cost)

            # 更新收盘漂移权重
            denom = sum(w * (closes[c][i] / opens[c][i]) for c, w in new_weights.items())
            weights = {c: (w * (closes[c][i] / opens[c][i])) / denom for c, w in new_weights.items()}
        else:
            # 普通持有日 (昨收 -> 今收)
            lev_cur = atk_leverage if state else 1.0
            r_day = sum(w * (closes[c][i] / closes[c][i - 1] - 1.0) for c, w in weights.items())
            nav[i] = nav[i - 1] * (1.0 + lev_cur * r_day)
            if lev_cur > 1.0:
                nav[i] *= (1.0 - daily_fin_cost)
            denom = sum(w * (closes[c][i] / closes[c][i - 1]) for c, w in weights.items())
            weights = {c: (w * (closes[c][i] / closes[c][i - 1])) / denom for c, w in weights.items()}

        if state:
            atk_days += 1

        # 2) 收盘信号判断
        if buy_arr[i] and not state:
            pending = 'buy'
        elif sell_arr[i] and state:
            if exec_mode == 'a3_asym':
                # 卖出直接于今日(T日)收盘成交切换至防守！
                cost_rate = (atk_leverage + 1.0) * fee_oneway + atk_leverage * sell_close_slip
                nav[i] *= (1.0 - cost_rate)
                trade_count += 1
                if seg_start_i is not None:
                    atk_segs.append((idx[seg_start_i], d, i - seg_start_i + 1, nav[i] / seg_start_nav - 1.0))
                    seg_start_i = None
                weights = get_defense_weights(d, defense_mode, assets)
                state = False
                pending = None
            else:
                pending = 'sell'

    nav_s = pd.Series(nav, index=idx)
    return {
        'nav': nav_s,
        'trades': trade_count,
        'atk_pct': atk_days / (n - 1) * 100.0,
        'cross_switch': cross_switch,
        'atk_segs': atk_segs,
    }


def calc_metrics(res: dict, split_date=SPLIT_DATE) -> dict:
    nav = res['nav']
    dr = nav.pct_change().dropna()
    ann = annualized(nav)
    mdd = max_drawdown(nav)
    calmar = ann / abs(mdd) if mdd < 0 else float('nan')
    sharpe = (dr.mean() / dr.std() * np.sqrt(252)) if dr.std() > 0 else float('nan')

    # 前后半段
    h1_ann = h1_mdd = h2_ann = h2_mdd = float('nan')
    if nav.index[0] < split_date < nav.index[-1]:
        n1 = nav.loc[:split_date]
        n1 = n1 / n1.iloc[0]
        n2 = nav.loc[split_date:]
        n2 = n2 / n2.iloc[0]
        h1_ann, h1_mdd = annualized(n1), max_drawdown(n1)
        h2_ann, h2_mdd = annualized(n2), max_drawdown(n2)

    # 打脸段 (<=5交易日且亏损)
    whips = [s for s in res['atk_segs'] if s[2] <= 5 and s[3] < 0]
    whip_cnt = len(whips)
    whip_sum = sum(s[3] for s in whips) * 100.0

    # 分年度
    yearly = {}
    for yr, g in nav.groupby(nav.index.year):
        pos0 = nav.index.get_loc(g.index[0])
        prev = nav.iloc[pos0 - 1] if pos0 > 0 else 1.0
        yearly[yr] = (g.iloc[-1] / prev - 1.0) * 100.0

    # 关键月度
    months = {}
    for ym in ['2026-07', '2026-09', '2026-10', '2026-11']:
        sub = nav.loc[nav.index.strftime('%Y-%m') == ym]
        if len(sub):
            pos0 = nav.index.get_loc(sub.index[0])
            prev = nav.iloc[pos0 - 1] if pos0 > 0 else 1.0
            months[ym] = (sub.iloc[-1] / prev - 1.0) * 100.0
        else:
            months[ym] = float('nan')

    return {
        'cum': (nav.iloc[-1] - 1.0) * 100.0,
        'ann': ann * 100.0,
        'mdd': mdd * 100.0,
        'calmar': calmar,
        'sharpe': sharpe,
        'trades': res['trades'],
        'atk_pct': res['atk_pct'],
        'h1_ann': h1_ann * 100.0,
        'h1_mdd': h1_mdd * 100.0,
        'h2_ann': h2_ann * 100.0,
        'h2_mdd': h2_mdd * 100.0,
        'whip_cnt': whip_cnt,
        'whip_sum': whip_sum,
        'cross': res['cross_switch'],
        'yearly': yearly,
        'months': months,
    }


def main():
    avg = load_csv('avg_880003.csv')
    codes = ['512100', '159552', '512890', '159201', '588220', '518880', '513100', '513500']
    assets = {c: load_etf_clean(c) for c in codes}

    lines = []
    def log(s=''):
        print(s)
        lines.append(s)

    log('=' * 116)
    log('五大正交方向联合组合交叉验证 & 稳健性压力测试报告 (compare_maxret_combo.py)')
    log('=' * 116)

    # =========================================================================
    # 实验一：长周期跨资产全洁净窗口 (2019-01-18 ~ 2026-10-23, 1879 交易日, 100%原生无断层)
    # =========================================================================
    idx_w1 = avg.index.intersection(assets['512890'].index).intersection(assets['518880'].index).intersection(assets['513100'].index)
    # 剔除 159552 缺失日以与生产严格对齐
    valid_atk = assets['512100'].index[assets['512100'].index < SWITCH_ATK].union(
        assets['159552'].index[assets['159552'].index >= SWITCH_ATK]
    )
    idx_w1 = idx_w1.intersection(valid_atk)
    idx_w1 = idx_w1[idx_w1 <= pd.Timestamp('2026-10-23')]

    log(f'\n[窗口一 · 长周期全洁净跨资产窗口] {idx_w1[0].date()} ~ {idx_w1[-1].date()} ({len(idx_w1)} 交易日, 双边万1)')
    log('说明：此窗口所有国内/黄金/纳指/标普/159201数据 100% 原生无11月断层，可直接横向比对。')

    # 预计算 4 组信号
    sig_base = build_signals(avg, assets, idx_w1, use_s1=False, use_s2=False)
    sig_s1   = build_signals(avg, assets, idx_w1, use_s1=True,  use_s2=False)
    sig_s2   = build_signals(avg, assets, idx_w1, use_s1=False, use_s2=True)
    sig_s12  = build_signals(avg, assets, idx_w1, use_s1=True,  use_s2=True)

    configs_w1 = [
        # (名称, sig, off, def, exec, fee, slip, lev)
        ('0. 基准 方案C (T+1开盘/512890)',             sig_base, 'base',       '512890',     't1_open', 0.0001, 0.0,    1.0),
        # 单项消融
        ('1a. 单项 S1 (零下回落>0.0010)',              sig_s1,   'base',       '512890',     't1_open', 0.0001, 0.0,    1.0),
        ('1b. 单项 S2 (风格比值R-DIF12防洗盘)',        sig_s2,   'base',       '512890',     't1_open', 0.0001, 0.0,    1.0),
        ('1c. 单项 D_CN (50%红利+50%黄金)',            sig_base, 'base',       'cn_50_50',   't1_open', 0.0001, 0.0,    1.0),
        ('1d. 单项 D_GLB (34红+33黄+33纳)',            sig_base, 'base',       'glb_333',    't1_open', 0.0001, 0.0,    1.0),
        ('1e. 单项 D_SPX (34红+33黄+33标普)',          sig_base, 'base',       'glb_spx',    't1_open', 0.0001, 0.0,    1.0),
        ('1f. 单项 E_A3 (买T+1开/卖T尾盘)',            sig_base, 'base',       '512890',     'a3_asym', 0.0001, 0.0,    1.0),
        ('1g. 单项 O_2K (24.06起60%2000+40%科100)',    sig_base, 'dual_60_40', '512890',     't1_open', 0.0001, 0.0,    1.0),
        # 信号层叠加
        ('2a. 信号组合 S1 + S2',                       sig_s12,  'base',       '512890',     't1_open', 0.0001, 0.0,    1.0),
        ('2b. S1 + S2 + E_A3 (无滑点)',                sig_s12,  'base',       '512890',     'a3_asym', 0.0001, 0.0,    1.0),
        ('2c. S1 + S2 + E_A3 (卖端+0.05%滑点)',        sig_s12,  'base',       '512890',     'a3_asym', 0.0001, 0.0005, 1.0),
        # 纯国内组合（无QDII，无杠杆）
        ('3a. 纯国内稳健: S1 + D_CN(50红50黄)',        sig_s1,   'base',       'cn_50_50',   't1_open', 0.0001, 0.0,    1.0),
        ('3b. 纯国内进阶: S1+S2 + D_CN(50红50黄)',     sig_s12,  'base',       'cn_50_50',   't1_open', 0.0001, 0.0,    1.0),
        ('3c. 纯国内进阶: S1+S2 + D_CN逆波 + O_2K',    sig_s12,  'dual_60_40', 'cn_invvol',  't1_open', 0.0001, 0.0,    1.0),
        ('3d. 纯国内全栈: S1+S2 + D_CN逆波+201 + O_2K',sig_s12,  'dual_60_40', 'cn_201_gold','t1_open', 0.0001, 0.0,    1.0),
        ('3e. 纯国内全栈+A3(含卖端万5滑点)',           sig_s12,  'dual_60_40', 'cn_201_gold','a3_asym', 0.0001, 0.0005, 1.0),
        # 含QDII全球组合（无杠杆）
        ('4a. 全球稳健: S1 + D_GLB(34红33黄33纳)',     sig_s1,   'base',       'glb_333',    't1_open', 0.0001, 0.0,    1.0),
        ('4b. 全球核心: S1+S2 + D_GLB(34红33黄33纳)',  sig_s12,  'base',       'glb_333',    't1_open', 0.0001, 0.0,    1.0),
        ('4c. 全球标普: S1+S2 + D_SPX(34红33黄33标)',  sig_s12,  'base',       'glb_spx',    't1_open', 0.0001, 0.0,    1.0),
        ('4d. 全球双核: S1+S2 + D_GLB + O_2K(60/40)',  sig_s12,  'dual_60_40', 'glb_333',    't1_open', 0.0001, 0.0,    1.0),
        ('4e. 全球全栈: S1+S2 + D_GLB201 + O_2K',      sig_s12,  'dual_60_40', 'glb_201_333','t1_open', 0.0001, 0.0,    1.0),
        ('4f. 全球全栈+A3(无滑点)',                    sig_s12,  'dual_60_40', 'glb_201_333','a3_asym', 0.0001, 0.0,    1.0),
        ('4g. 全球全栈+A3(含卖端万5滑点)',             sig_s12,  'dual_60_40', 'glb_201_333','a3_asym', 0.0001, 0.0005, 1.0),
        # 进攻期温和杠杆边界（严扣5.5%融资息）
        ('5a. 纯国内全栈(3c) + 进攻1.3x融资',          sig_s12,  'dual_60_40', 'cn_invvol',  't1_open', 0.0001, 0.0,    1.3),
        ('5b. 全球核心(4b) + 进攻1.3x融资',            sig_s12,  'base',       'glb_333',    't1_open', 0.0001, 0.0,    1.3),
        ('5c. 全球全栈(4e) + 进攻1.3x融资',            sig_s12,  'dual_60_40', 'glb_201_333','t1_open', 0.0001, 0.0,    1.3),
        ('5d. 全球全栈(4e) + 进攻1.5x融资',            sig_s12,  'dual_60_40', 'glb_201_333','t1_open', 0.0001, 0.0,    1.5),
    ]

    hdr = f"{'方案':<38} {'累计%':>8} {'年化%':>7} {'MDD%':>7} {'Calmar':>6} {'Sharpe':>6} {'换仓':>4} {'打脸':>9} {'前半Ann/MDD':>14} {'后半Ann/MDD':>14} {'两半'}"
    log('-' * len(hdr))
    log(hdr)
    log('-' * len(hdr))

    m_w1 = {}
    base_m = None
    for name, sg, om, dm, em, fee, slip, lev in configs_w1:
        r = simulate(sg, assets, idx_w1, om, dm, em, fee, slip, lev)
        m = calc_metrics(r)
        m_w1[name] = m
        if base_m is None:
            base_m = m
        both_ret = (m['h1_ann'] > base_m['h1_ann']) and (m['h2_ann'] > base_m['h2_ann'])
        both_mdd = (m['h1_mdd'] >= base_m['h1_mdd'] - 0.05) and (m['h2_mdd'] >= base_m['h2_mdd'] - 0.05)
        tag = '★四优' if (both_ret and both_mdd) else ('✓双益' if both_ret else ('-' if name.startswith('0.') else '单半'))
        log(f"{name:<38} {m['cum']:>8.1f} {m['ann']:>7.2f} {m['mdd']:>7.2f} {m['calmar']:>6.2f} {m['sharpe']:>6.2f} "
            f"{m['trades']:>4d} {m['whip_cnt']:>2d}/{m['whip_sum']:>6.1f} "
            f"{m['h1_ann']:>6.2f}/{m['h1_mdd']:>6.2f} {m['h2_ann']:>6.2f}/{m['h2_mdd']:>6.2f} {tag}")

    # 分年度收益表（挑选代表性方案）
    log('\n[窗口一 · 代表性方案分年度收益 (%)]')
    rep_names = [
        '0. 基准 方案C (T+1开盘/512890)',
        '1a. 单项 S1 (零下回落>0.0010)',
        '1b. 单项 S2 (风格比值R-DIF12防洗盘)',
        '2a. 信号组合 S1 + S2',
        '3b. 纯国内进阶: S1+S2 + D_CN(50红50黄)',
        '3d. 纯国内全栈: S1+S2 + D_CN逆波+201 + O_2K',
        '4b. 全球核心: S1+S2 + D_GLB(34红33黄33纳)',
        '4c. 全球标普: S1+S2 + D_SPX(34红33黄33标)',
        '4e. 全球全栈: S1+S2 + D_GLB201 + O_2K',
        '4g. 全球全栈+A3(含卖端万5滑点)',
        '5c. 全球全栈(4e) + 进攻1.3x融资',
    ]
    yrs = list(range(2019, 2027))
    log(f"{'方案':<40}" + ''.join(f"{y:>8d}" for y in yrs) + f"{'胜基准年':>9}")
    log('-' * (40 + 8 * len(yrs) + 9))
    for nm in rep_names:
        y = m_w1[nm]['yearly']
        wins = sum(1 for k in yrs if y[k] > m_w1[rep_names[0]]['yearly'][k] + 0.05)
        ties = sum(1 for k in yrs if abs(y[k] - m_w1[rep_names[0]]['yearly'][k]) <= 0.05)
        wt = '-' if nm == rep_names[0] else f'{wins}胜{ties}平'
        log(f"{nm:<40}" + ''.join(f"{y[k]:>8.2f}" for k in yrs) + f"{wt:>9}")

    # =========================================================================
    # 实验二：最新盲测全周期 (2019-01-18 ~ 2026-11-25, 1900 交易日, 内存清洗11月断层)
    # 检验纯核心池(无518880/513100，因后两者只到10-23)在最新 10-24 ~ 11-25 的表现
    # =========================================================================
    idx_w2 = avg.index.intersection(assets['512890'].index).intersection(valid_atk)
    log(f'\n[窗口二 · 核心池最新全周期 (含10-24~11-25最新盲测, 内存清洗)] {idx_w2[0].date()} ~ {idx_w2[-1].date()} ({len(idx_w2)} 交易日)')

    sg2_base = build_signals(avg, assets, idx_w2, False, use_s2=False)
    sg2_s1   = build_signals(avg, assets, idx_w2, True,  use_s2=False)
    sg2_s2   = build_signals(avg, assets, idx_w2, False, use_s2=True)
    sg2_s12  = build_signals(avg, assets, idx_w2, True,  use_s2=True)

    configs_w2 = [
        ('W2-0. 基准 方案C (512890)',                sg2_base, 'base',       '512890',  't1_open', 0.0001, 0.0,    1.0),
        ('W2-1. 基准 + QMT双择优(512890/159201)',  sg2_base, 'base',       'qmt_201', 't1_open', 0.0001, 0.0,    1.0),
        ('W2-2. S1 (零下回落>0.0010)',             sg2_s1,   'base',       '512890',  't1_open', 0.0001, 0.0,    1.0),
        ('W2-3. S2 (风格比值R-DIF12)',             sg2_s2,   'base',       '512890',  't1_open', 0.0001, 0.0,    1.0),
        ('W2-4. S1 + S2 (纯信号叠加)',             sg2_s12,  'base',       '512890',  't1_open', 0.0001, 0.0,    1.0),
        ('W2-5. S1 + S2 + QMT双择优',              sg2_s12,  'base',       'qmt_201', 't1_open', 0.0001, 0.0,    1.0),
        ('W2-6. S1 + S2 + QMT双择优 + O_2K(60/40)',sg2_s12,  'dual_60_40', 'qmt_201', 't1_open', 0.0001, 0.0,    1.0),
        ('W2-7. W2-6 + A3(卖端+万5滑点)',          sg2_s12,  'dual_60_40', 'qmt_201', 'a3_asym', 0.0001, 0.0005, 1.0),
        ('W2-8. W2-6 + 进攻1.3x融资(5.5%息)',      sg2_s12,  'dual_60_40', 'qmt_201', 't1_open', 0.0001, 0.0,    1.3),
    ]
    hdr2 = f"{'方案':<40} {'累计%':>8} {'年化%':>7} {'MDD%':>7} {'Calmar':>6} {'换仓':>4} {'2026YTD':>8} {'26-07':>7} {'26-09':>7} {'26-10':>7} {'26-11':>7}"
    log('-' * len(hdr2))
    log(hdr2)
    log('-' * len(hdr2))
    for name, sg, om, dm, em, fee, slip, lev in configs_w2:
        r = simulate(sg, assets, idx_w2, om, dm, em, fee, slip, lev)
        m = calc_metrics(r)
        mo = m['months']
        log(f"{name:<40} {m['cum']:>8.1f} {m['ann']:>7.2f} {m['mdd']:>7.2f} {m['calmar']:>6.2f} {m['trades']:>4d} "
            f"{m['yearly'][2026]:>8.2f} {mo['2026-07']:>7.2f} {mo['2026-09']:>7.2f} {mo['2026-10']:>7.2f} {mo['2026-11']:>7.2f}")

    # =========================================================================
    # 实验三：现代实盘池窗口 (2024-06-28 ~ 2026-10-23, 含全部新标的 159552/588220/159201/黄金/纳指)
    # =========================================================================
    idx_w3 = idx_w1[idx_w1 >= SWITCH_ATK]
    log(f'\n[窗口三 · 现代实盘完整池窗口] {idx_w3[0].date()} ~ {idx_w3[-1].date()} ({len(idx_w3)} 交易日, 约2.3年)')
    sg3_base = sig_base.loc[idx_w3]
    sg3_s1   = sig_s1.loc[idx_w3]
    sg3_s12  = sig_s12.loc[idx_w3]
    configs_w3 = [
        ('M0. 基准 159552 / 512890',                sg3_base, 'base',       '512890',     't1_open', 0.0001, 0.0,    1.0),
        ('M1. S1+S2 / 159552 / 512890',             sg3_s12,  'base',       '512890',     't1_open', 0.0001, 0.0,    1.0),
        ('M2. 纯国内全栈 (S1+S2+双核+红利201/黄金)', sg3_s12,  'dual_60_40', 'cn_201_gold','t1_open', 0.0001, 0.0,    1.0),
        ('M3. 全球核心 (S1+S2+159552+三驾马车)',    sg3_s12,  'base',       'glb_333',    't1_open', 0.0001, 0.0,    1.0),
        ('M4. 全球全栈 (S1+S2+双核+三驾201)',       sg3_s12,  'dual_60_40', 'glb_201_333','t1_open', 0.0001, 0.0,    1.0),
        ('M5. 全球全栈 + A3(卖端万5滑点)',          sg3_s12,  'dual_60_40', 'glb_201_333','a3_asym', 0.0001, 0.0005, 1.0),
        ('M6. 全球全栈 + 进攻1.3x融资',             sg3_s12,  'dual_60_40', 'glb_201_333','t1_open', 0.0001, 0.0,    1.3),
        ('M7. 全球全栈 + 进攻1.5x融资',             sg3_s12,  'dual_60_40', 'glb_201_333','t1_open', 0.0001, 0.0,    1.5),
    ]
    hdr3 = f"{'方案':<40} {'累计%':>8} {'年化%':>7} {'MDD%':>7} {'Calmar':>6} {'Sharpe':>6} {'换仓':>4}"
    log('-' * len(hdr3))
    log(hdr3)
    log('-' * len(hdr3))
    for name, sg, om, dm, em, fee, slip, lev in configs_w3:
        r = simulate(sg, assets, idx_w3, om, dm, em, fee, slip, lev)
        m = calc_metrics(r)
        log(f"{name:<40} {m['cum']:>8.1f} {m['ann']:>7.2f} {m['mdd']:>7.2f} {m['calmar']:>6.2f} {m['sharpe']:>6.2f} {m['trades']:>4d}")

    # =========================================================================
    # 实验四：严格稳健性检验（参数邻域高原 + 交易成本压力测试）
    # =========================================================================
    log('\n[稳健性检验一 · S1×S2 参数高原矩阵（窗口一，全球三驾马车防守 D_GLB，T+1开盘）]')
    s1_grid = [0.0, 0.0008, 0.0010, 0.0012, 0.0015, 0.0020]
    s2_grid = [(0, 0), (10, 20), (11, 23), (12, 26), (13, 27)]
    log(f"{'S1_th \\ S2_Rdif':<18}" + ''.join(f"{('无S2' if a==0 else f'R({a}/{b})'):>18}" for a, b in s2_grid))
    log('-' * (18 + 18 * len(s2_grid)))
    for th in s1_grid:
        row_s = f"{('无S1' if th==0 else f'{th:.4f}'):<18}"
        for a, b in s2_grid:
            sg = build_signals(avg, assets, idx_w1, use_s1=(th > 0), s1_th=th, use_s2=(a > 0), s2_fast=a, s2_slow=b)
            r = simulate(sg, assets, idx_w1, 'base', 'glb_333', 't1_open', 0.0001, 0.0, 1.0)
            m = calc_metrics(r)
            row_s += f"  {m['ann']:>5.2f}%/{m['mdd']:>6.2f}%"
        log(row_s)

    log('\n[稳健性检验二 · 交易成本/滑点压力测试（窗口一，年化% / Calmar）]')
    fee_grid = [0.0001, 0.0003, 0.0005, 0.0010, 0.0020]
    stress_cfgs = [
        ('0. 基准 方案C',                   sig_base, 'base',       '512890',     't1_open', 0.0,    1.0),
        ('2a. 信号 S1+S2 (512890)',         sig_s12,  'base',       '512890',     't1_open', 0.0,    1.0),
        ('3b. 纯国内 S1+S2 + 50红50黄',     sig_s12,  'base',       'cn_50_50',   't1_open', 0.0,    1.0),
        ('4b. 全球核心 S1+S2 + 三驾马车',   sig_s12,  'base',       'glb_333',    't1_open', 0.0,    1.0),
        ('4e. 全球全栈 S1+S2+双核+三驾201', sig_s12,  'dual_60_40', 'glb_201_333','t1_open', 0.0,    1.0),
        ('4f. 全球全栈 + A3(卖端同费)',     sig_s12,  'dual_60_40', 'glb_201_333','a3_asym', 0.0,    1.0),
    ]
    log(f"{'方案':<32}" + ''.join(f"{f'单边{f*10000:.0f}bp':>15}" for f in fee_grid))
    log('-' * (32 + 15 * len(fee_grid)))
    for nm, sg, om, dm, em, sl, lv in stress_cfgs:
        row_s = f"{nm:<32}"
        for f in fee_grid:
            r = simulate(sg, assets, idx_w1, om, dm, em, f, sl, lv)
            m = calc_metrics(r)
            row_s += f"  {m['ann']:>5.2f}% / {m['calmar']:>4.2f}"
        log(row_s)

    with open(OUT_PATH, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
    print(f'\n已保存完整报告至: {OUT_PATH}')


if __name__ == '__main__':
    main()
