#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
近两周 159552/512890 来回切换的规律挖掘（5分钟K线，480根）

两部分：
  一、统计特征：相对强弱比(159552/512890)的 5 分钟收益自相关、
     日内时段规律（48根/日的平均相对收益）
  二、指标规则回测（无前视，信号当根收盘成交，换股扣双边万1）：
     R1 比值MACD(12,26,9)：DIF>DEA 持2000增强，否则红利低波
     R2 比值均线          ：比值 > MA20 持2000增强
     R3 比值RSI(14)       ：RSI>50 持2000增强
     R4 比值日内VWAP      ：比值 > 当日VWAP 持2000增强
     R5 单腿MACD(基准)     ：159552 自身5分MACD金叉持有/死叉切红利
对比基准：持有不动 / 日频完美换股 / 5分钟完美换股（见 t_ideal.py）
"""
import sys
import io
import os

import numpy as np
import pandas as pd

# pythonw（无窗口计划任务）下 sys.stdout 为 None，不能 reconfigure
if sys.platform == 'win32' and sys.stdout is not None and not getattr(sys.stdout, '_utf8_wrapped', False):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stdout._utf8_wrapped = True

from t_ideal import fetch_5min, load_daily, ema, DAYS

FEE = 0.0002


def rsi(s: pd.Series, n: int = 14) -> pd.Series:
    diff = s.diff()
    up = diff.clip(lower=0).ewm(alpha=1.0 / n, adjust=False).mean()
    dn = (-diff.clip(upper=0)).ewm(alpha=1.0 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def intraday_vwap(ratio: pd.Series) -> pd.Series:
    day = ratio.index.date
    cum = ratio.groupby(day).cumsum()
    cnt = ratio.groupby(day).cumcount() + 1
    return cum / cnt   # 等权均价近似 VWAP（分钟成交量权重对结论影响很小）


def run_rule(hold_a: pd.Series, r5a: pd.Series, r5b: pd.Series):
    """hold_a=True 持159552，否则512890；当根收盘切换，统计毛/净收益与换股次数"""
    strat_r = pd.Series(np.where(hold_a, r5a, r5b), index=hold_a.index).fillna(0.0)
    switches = int((hold_a != hold_a.shift(1)).sum()) - 1  # 首根不算切换
    switches = max(switches, 0)
    gross = (1 + strat_r).prod() - 1
    net = gross - switches * FEE
    return gross, switches, net, hold_a.mean() * 100


def pct(x):
    return '%+.2f%%' % (x * 100)


def main():
    a = load_daily('etf_159552_hfq.csv')
    b = load_daily('etf_512890_hfq.csv')
    idx = a.index.intersection(b.index)[-(DAYS + 1):]
    start_dt = pd.Timestamp(idx[1])

    m_a = fetch_5min('159552', 0)
    m_b = fetch_5min('512890', 1)
    common = m_a.index.intersection(m_b.index)
    common = common[common >= start_dt]
    pa, pb = m_a.loc[common, 'close'], m_b.loc[common, 'close']
    ratio = pa / pb
    r5a, r5b = pa.pct_change(), pb.pct_change()
    rel_r = ratio.pct_change()    # 相对收益的 5 分钟变化

    print('=' * 66)
    print('一、统计特征（%d 根 5分钟K线，%s ~ %s）' % (len(common), idx[1].date(), idx[-1].date()))
    print('=' * 66)
    ac1 = rel_r.autocorr(1)
    ac2 = rel_r.autocorr(2)
    ac4 = rel_r.autocorr(4)
    print('相对收益自相关: lag1 %+.3f  lag2 %+.3f  lag4(20分钟) %+.3f'
          % (ac1, ac2, ac4))
    print('（lag1<0=均值回归/反转占优，>0=趋势延续占优）')

    # 日内时段规律
    tod = rel_r.groupby(rel_r.index.time).mean() * 100
    tod_pairs = sorted(tod.items(), key=lambda x: -abs(x[1]))
    print('\n日内相对波动最大的时段（159552 相对 512890 的 5分钟平均收益%%）:')
    for t, v in tod_pairs[:6]:
        print('  %s  %+.3f%%' % (t.strftime('%H:%M'), v))

    print()
    print('=' * 66)
    print('二、指标规则回测（无前视，换股扣双边万1）')
    print('=' * 66)

    dif = ema(ratio, 12) - ema(ratio, 26)
    dea = ema(dif, 9)
    rules = {
        'R1 比值MACD': (dif > dea),
        'R2 比值>MA20': (ratio > ratio.rolling(20).mean()),
        'R3 比值RSI>50': (rsi(ratio) > 50),
        'R4 比值>日内VWAP': (ratio > intraday_vwap(ratio)),
    }
    # R5 单腿 MACD（与 t_ideal L5 相同口径）
    difa = ema(pa, 12) - ema(pa, 26)
    deaa = ema(difa, 9)
    rules['R5 单腿MACD(159552)'] = (difa > deaa)

    rows = []
    for name, cond in rules.items():
        hold_a = cond.fillna(False)
        gross, sw, net, pct_a = run_rule(hold_a, r5a, r5b)
        rows.append({'规则': name, '毛收益': pct(gross), '换股次数': sw,
                     '扣费后': pct(net), '2000持仓%': '%.0f%%' % pct_a})

    # 基准
    hold_a_full = (1 + r5a.fillna(0)).prod() - 1
    hold_b_full = (1 + r5b.fillna(0)).prod() - 1
    print('基准: 全程持2000增强 %s / 全程持红利低波 %s' % (pct(hold_a_full), pct(hold_b_full)))
    print(pd.DataFrame(rows).to_string(index=False))

    # 08-19 大跌日：各规则当天的表现
    print()
    print('-' * 66)
    print('关键日 08-19（2000增强 -6.16%%）各规则当日收益（毛）:')
    day_mask = common.date == pd.Timestamp('2026-08-19').date()
    dra, drb = r5a[day_mask], r5b[day_mask]
    for name, cond in rules.items():
        hold_a = cond.fillna(False)[day_mask]
        dr = pd.Series(np.where(hold_a, dra, drb), index=dra.index).fillna(0.0)
        in_a = hold_a.mean() * 100
        print('  %-22s 当日 %s（当日 %.0f%% 时间在2000增强）' % (name, pct((1 + dr).prod() - 1), in_a))


if __name__ == '__main__':
    main()
