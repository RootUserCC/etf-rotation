#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
R1~R5 切换规则的 T+1 约束回测（5分钟K线全窗口）

上一版 t_pattern_long.py 的致命问题：A股ETF是 T+1，当日买入份额当日不可卖，
模拟却允许一天内无限次来回切换，结果（两年+74万%）是不可实现的幻想。

本版规则：
  - 初始 50/50 底仓（价值各半）
  - 每日开盘，昨日买入的份额转为"可卖"
  - 信号要求切换时：只能卖出目标腿以外的"可卖"部分去买入目标腿；
    当日买入部分锁定，不可再卖
  - 换股成本：卖出单边万1（卖出收，买入也收，双边共万2/次）；另测千一保守档
  - 输出：全窗口 + 按月 + 按行情类型
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

from t_pattern_long import build_rules, FEES, DATA_DIR
from t_pattern import run_rule


def load_5min(code):
    path = os.path.join(DATA_DIR, 'etf_%s_5min.csv' % code)
    return pd.read_csv(path, parse_dates=['dt']).set_index('dt')


def simulate_t1(cond: pd.Series, ra: pd.Series, rb: pd.Series,
                fee_side: float = 0.0001):
    """T+1 库存约束下的切换模拟。
    cond=True → 目标全仓A(159552)；False → 目标全仓B(512890)。
    份额以价值比例记账：A_old/A_new/B_old/B_new（_old=可卖，_new=当日买入锁定）。
    返回：净值序列、换股(卖出)次数、每日目标切换受阻率。
    """
    idx = cond.index
    n = len(idx)
    # 初始 50/50 底仓，全部可卖
    A_old, A_new, B_old, B_new = 0.5, 0.0, 0.5, 0.0
    nav = np.empty(n)
    sells = 0
    blocked = 0
    days = pd.Series(idx.date, index=idx)
    cur_day = None

    cond_v = cond.fillna(False).values
    ra_v = ra.fillna(0.0).values
    rb_v = rb.fillna(0.0).values

    for i in range(n):
        d = days.iloc[i]
        if d != cur_day:              # 新交易日：解锁昨日买入
            A_old += A_new
            B_old += B_new
            A_new = B_new = 0.0
            cur_day = d
        total = A_old + A_new + B_old + B_new

        # 先记账本根K线收益（相对上一根收盘的涨跌作用于持仓）
        # —— 在切换前应用，简化处理：先结算收益再执行信号
        if i > 0:
            A_old *= (1 + ra_v[i]); A_new *= (1 + ra_v[i])
            B_old *= (1 + rb_v[i]); B_new *= (1 + rb_v[i])

        want_A = cond_v[i]
        if want_A and B_old > 1e-12:
            # 卖出全部可卖 B 买 A
            B_new_from = B_old * (1 - fee_side)   # 卖出扣费后买入A
            A_new += B_new_from * (1 - fee_side)
            B_old = 0.0
            sells += 1
        elif not want_A and A_old > 1e-12:
            A_new_from = A_old * (1 - fee_side)
            B_new += A_new_from * (1 - fee_side)
            A_old = 0.0
            sells += 1
        else:
            # 想切但可卖库存为0（当日已锁），记一次受阻
            if (want_A and B_old <= 1e-12 and B_new > 1e-12) or \
               (not want_A and A_old <= 1e-12 and A_new > 1e-12):
                blocked += 1
        nav[i] = A_old + A_new + B_old + B_new

    nav = pd.Series(nav, index=idx) / nav[0]
    return nav, sells, blocked


def pct(x):
    return '%+.2f%%' % (x * 100)


def main():
    m_a = load_5min('159552')
    m_b = load_5min('512890')
    common = m_a.index.intersection(m_b.index)
    pa, pb = m_a.loc[common, 'close'], m_b.loc[common, 'close']
    r5a, r5b = pa.pct_change(), pb.pct_change()
    days_n = pd.Series(common.date).nunique()
    print('区间: %s ~ %s，%d 个交易日 / %d 根K线'
          % (common[0].date(), common[-1].date(), days_n, len(common)))

    rules = build_rules(pa, pb)
    base_a = pa.iloc[-1] / pa.iloc[0] - 1
    base_b = pb.iloc[-1] / pb.iloc[0] - 1
    print('基准: 持有2000增强 %s / 持有红利低波 %s / 50-50不动 %s'
          % (pct(base_a), pct(base_b), pct((base_a + base_b) / 2)))

    print('\n===== T+1 约束 · 全窗口 =====')
    print('%-18s %10s %8s %8s %10s' % ('规则', '净值收益', '卖出次数', '受阻次数', '净@千一估算'))
    for name, cond in rules.items():
        nav, sells, blocked = simulate_t1(cond, r5a, r5b, fee_side=0.0001)
        ret = nav.iloc[-1] - 1
        # 千一保守档重跑
        nav2, _, _ = simulate_t1(cond, r5a, r5b, fee_side=0.0005)
        print('%-18s %10s %8d %8d %10s'
              % (name, pct(ret), sells, blocked, pct(nav2.iloc[-1] - 1)))

    # 按月（仅 R2/R3/R4，净收益=月末净值环比）
    print('\n===== T+1 约束 · 按月净值收益（万1双边）=====')
    month = pd.Series(common.strftime('%Y-%m'), index=common)
    navs = {}
    for name in ['R2 比值>MA20', 'R3 比值RSI>50', 'R4 比值>日内VWAP']:
        nav, _, _ = simulate_t1(rules[name], r5a, r5b, fee_side=0.0001)
        navs[name] = nav
    df = pd.DataFrame(navs)
    df['持有2000增强'] = pa / pa.iloc[0]
    df['持有红利低波'] = pb / pb.iloc[0]
    monthly = df.groupby(month).last() / df.groupby(month).first() - 1
    monthly.index.name = '月份'
    print((monthly * 100).round(2).astype(str).apply(lambda c: c + '%').to_string())


if __name__ == '__main__':
    main()
