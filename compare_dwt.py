#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""做T叠加轮动回测：方案C轮动底仓 + 部分仓位日内做T（1分钟线）

底仓：方案C信号（880003 MACD 17/34/9 拐点）在 进攻仓(512100→159552拼接)/512890 间轮动，
      T+1开盘成交（与 compare_improve V0 同口径）。
做T：对当日持有的那条腿，用 W 仓位按"做T偏离指标"规则做日内回转：
  反T: 偏离VWAP ≥ +0.5% 卖出，跌回卖价-0.2% 接回 / 涨破卖价+0.5% 止损接回 / 尾盘接回
  正T: 偏离VWAP ≤ -0.5% 买入，涨回买价+0.2% 卖出 / 跌破买价-0.5% 止损卖出 / 尾盘卖出
  反T趋势过滤: 10:00 已涨超昨收 1.5% 的日子不做反T
  每方向每日最多一次，触发即扣双边万1
限制：TDX 1分钟线仅有近数月历史，窗口短，结果仅供定性参考；
      正T 需要预留现金（或融资），本回测按"T盈亏叠加持仓收益"处理，
      若实际需常备 W 现金，正T 部分收益会相应摊薄。
数据缓存: data/min_159552.csv / data/min_512890.csv（首次运行从TDX拉取）
"""
import sys
import io
import os
import time
import numpy as np
import pandas as pd

if sys.platform == 'win32' and not getattr(sys.stdout, '_utf8_wrapped', False):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stdout._utf8_wrapped = True

from compare_improve import (load, calc_signals, leg_returns, run_variant,
                             SWITCH_ATK, START, FEE)

BACK, STOP, TH = 0.002, 0.005, 0.005   # 接回/止损/触发 阈值（同做T偏离指标）
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')


def fetch_min(code, mkt):
    """TDX 1分钟线全量分页拉取，带 CSV 缓存"""
    cache = os.path.join(DATA_DIR, 'min_%s.csv' % code)
    if os.path.exists(cache):
        df = pd.read_csv(cache, parse_dates=['dt'])
        return df
    from pytdx.hq import TdxHq_API
    api = TdxHq_API()
    for ip, port in [('180.153.18.170', 7709), ('218.6.170.47', 7709),
                     ('119.147.212.81', 7709)]:
        try:
            if api.connect(ip, port, time_out=5) \
                    and api.get_security_bars(8, mkt, code, 0, 1):
                break
            api.disconnect()
        except Exception:
            continue
    else:
        raise RuntimeError('无可用TDX服务器')
    rows, start = [], 0
    while True:
        b = api.get_security_bars(8, mkt, code, start, 800)
        if not b:
            break
        rows = b + rows
        if len(b) < 800:
            break
        start += len(b)
        time.sleep(0.2)
    api.disconnect()
    df = pd.DataFrame(rows)
    df['dt'] = pd.to_datetime(df['datetime'])
    df.to_csv(cache, index=False)
    print('已缓存 %s (%d 根1分钟线)' % (cache, len(df)))
    return df


def prep_day(df):
    """按交易日分组，计算 VWAP 与偏离（同 check_dwt 口径）"""
    df = df.copy()
    df['date'] = df['dt'].dt.date
    df['hm'] = df['dt'].dt.strftime('%H:%M')
    df['typ'] = (df['high'] + df['low'] + df['close']) / 3
    df['pv'] = df['typ'] * df['vol']
    g = df.groupby('date')
    df['vwap'] = g['pv'].cumsum() / g['vol'].cumsum()
    df['dev'] = df['close'] / df['vwap'] - 1
    return df


def sim_day(day, prev_close, side, trend_filter=True):
    """单日单侧做T盈亏（占做T仓位比例，毛收益）。未触发返回 None"""
    day = day.reset_index(drop=True)
    if len(day) < 60:
        return None
    if trend_filter and side == 'sell':
        p10 = day.loc[day['hm'] <= '10:00', 'close']
        if len(p10) and p10.iloc[-1] > prev_close * 1.015:
            return None
    dev = day['dev'].values
    t = np.where(dev >= TH)[0] if side == 'sell' else np.where(dev <= -TH)[0]
    if len(t) == 0 or t[0] >= len(day) - 10:
        return None
    i = t[0]
    px0 = day['close'].iloc[i]
    after = day['close'].iloc[i + 1:].values
    if side == 'sell':
        win = after <= px0 * (1 - BACK)
        stop = after >= px0 * (1 + STOP)
        sgn = 1
    else:
        win = after >= px0 * (1 + BACK)
        stop = after <= px0 * (1 - STOP)
        sgn = -1
    wi = np.where(win)[0]
    si = np.where(stop)[0]
    w = wi[0] if len(wi) else 10 ** 9
    s = si[0] if len(si) else 10 ** 9
    if w < s:
        px1, ok = after[w], True
    elif s < w:
        px1, ok = after[s], False
    else:
        px1, ok = after[-1], None     # 尾盘平仓：不算赢也不算止损
    return sgn * (px1 - px0) / px0, ok


def main():
    # ---- 底仓：方案C 轮动（V0 口径），取每日实际持仓腿 ----
    avg = load('avg_880003.csv')
    etf1000 = load('etf_512100_hfq.csv')
    etf2000e = load('etf_159552_hfq.csv')
    etfdiv = load('etf_512890_hfq.csv')
    sig_full = calc_signals(avg['close'], sell_anywhere=True, fast=17, slow=34, sig_n=9)
    atk_o = pd.concat([etf1000['open'][etf1000.index < SWITCH_ATK],
                       etf2000e['open'][etf2000e.index >= SWITCH_ATK]])
    atk_c = pd.concat([etf1000['close'][etf1000.index < SWITCH_ATK],
                       etf2000e['close'][etf2000e.index >= SWITCH_ATK]])
    idx = etfdiv.index.intersection(atk_c.index).intersection(avg.index)
    idx = idx[idx >= START]
    on_a, id_a, cc_a = leg_returns(atk_o, atk_c)
    on_d, id_d, cc_d = leg_returns(etfdiv['open'], etfdiv['close'])
    on1, id1, cc1 = leg_returns(etf1000['open'], etf1000['close'])
    on_a.loc[SWITCH_ATK], id_a.loc[SWITCH_ATK], cc_a.loc[SWITCH_ATK] = \
        on1.loc[SWITCH_ATK], id1.loc[SWITCH_ATK], cc1.loc[SWITCH_ATK]
    legs = {'atk': (on_a.loc[idx].fillna(0), id_a.loc[idx].fillna(0), cc_a.loc[idx].fillna(0)),
            'div': (on_d.loc[idx].fillna(0), id_d.loc[idx].fillna(0), cc_d.loc[idx].fillna(0))}
    nav0, st, _ = run_variant(idx, sig_full.loc[idx], legs, etfdiv['close'].loc[idx],
                              np.full(len(idx), np.nan), exec_mode='open', use_cash=False)

    # ---- 1分钟线（进攻腿=159552，防守腿=512890）----
    min_df = {'atk': prep_day(fetch_min('159552', 0)),
              'div': prep_day(fetch_min('512890', 1))}
    day_tbl = {}
    prev_close = {}
    for leg, df in min_df.items():
        closes = df.groupby('date')['close'].last()
        prev_close[leg] = closes.shift(1)
        day_tbl[leg] = dict(tuple(df.groupby('date')))
    days_atk = set(day_tbl['atk'])
    days_div = set(day_tbl['div'])
    win_days = sorted(days_atk | days_div)
    d0, d1 = win_days[0], win_days[-1]
    print('1分钟线窗口: %s ~ %s  进攻腿 %d 天 / 防守腿 %d 天'
          % (d0, d1, len(days_atk), len(days_div)))

    # ---- 逐日叠加 ----
    st_d = st.copy()
    st_d.index = st_d.index.date
    cc = {'atk': legs['atk'][2], 'div': legs['div'][2]}
    cc_d_series = {leg: s.rename_axis('date') for leg, s in cc.items()}

    recs = []
    for d in win_days:
        leg = st_d.get(d)
        if leg not in ('atk', 'div') or d not in day_tbl[leg]:
            continue
        pc = prev_close[leg].get(d)
        if pd.isna(pc):
            continue
        day = day_tbl[leg][d]
        base = float(cc_d_series[leg].get(pd.Timestamp(d), 0.0))
        r_sell = sim_day(day, pc, 'sell')
        r_buy = sim_day(day, pc, 'buy')
        recs.append({'date': d, 'leg': leg, 'base': base,
                     'sell': r_sell, 'buy': r_buy})
    R = pd.DataFrame(recs).set_index('date')

    def t_series(col):
        """净T收益序列（未触发=0，触发扣双边万1）与统计"""
        gross, stats = [], []
        for v in R[col]:
            if v is None:
                gross.append(0.0)
            else:
                r, ok = v
                gross.append(r - 2 * FEE)
                stats.append((r, ok))
        return np.array(gross), stats

    g_sell, st_sell = t_series('sell')
    g_buy, st_buy = t_series('buy')

    nav_base = np.prod(1 + R['base'].values)
    print('\n窗口内底仓（纯轮动）收益: %+.1f%%  共 %d 天（进攻 %d / 防守 %d）'
          % ((nav_base - 1) * 100, len(R), (R['leg'] == 'atk').sum(),
             (R['leg'] == 'div').sum()))

    print('\n%-22s %10s %10s %8s %8s %8s' % ('叠加方案', '总收益', '超额', '触发天', '胜率', '日均净T'))
    for label, tarr, stats in [
            ('反T(高卖) W=1/3', g_sell / 3, st_sell),
            ('反T(高卖) W=1/2', g_sell / 2, st_sell),
            ('正T(低买) W=1/3', g_buy / 3, st_buy),
            ('正T(低买) W=1/2', g_buy / 2, st_buy),
            ('正T+反T  W=1/3', (g_sell + g_buy) / 3, st_sell + st_buy),
            ('正T+反T  W=1/2', (g_sell + g_buy) / 2, st_sell + st_buy)]:
        tot = np.prod(1 + R['base'].values + tarr)
        ntr = len(stats)
        wins = sum(1 for r, ok in stats if ok is True)
        print('%-22s %+9.1f%% %+9.2f%% %8d %7.0f%% %+7.3f%%'
              % (label, (tot - 1) * 100, (tot - nav_base) * 100, ntr,
                 wins / ntr * 100 if ntr else 0,
                 np.mean([r for r, ok in stats]) * 100 if ntr else 0))

    # 分腿触发统计
    for leg, name in [('atk', '进攻仓'), ('div', '防守仓')]:
        m = (R['leg'] == leg).values
        ns = sum(1 for v, mm in zip(R['sell'], m) if mm and v is not None)
        nb = sum(1 for v, mm in zip(R['buy'], m) if mm and v is not None)
        print('%s: %d 天, 反T触发 %d 天, 正T触发 %d 天'
              % (name, m.sum(), ns, nb))


if __name__ == '__main__':
    main()
