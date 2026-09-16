#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
结构改进对比（信号仍用方案C，不改信号本身）：
  V0 基线 : 两腿轮动，T+1 开盘成交（现行实盘口径）
  V1     : + 空仓状态（卖信号时防守腿收盘 < MA20 → 空仓；空仓中收复 MA20 → 回防守腿）
  V2     : 同 V1，均线改 MA60
  V3     : 两腿轮动，信号日尾盘（收盘）成交
  V4     : V1(MA20空仓) + 尾盘成交
  V5     : V2(MA60空仓) + 尾盘成交

口径：方案C（880003 MACD 17/34/9 DIF 拐点，零下买/任意卖，sell_anywhere=True）；
进攻仓 512100→159552 于 2024-06-28 拼接（跨界持有当日收益按 512100 口径，
同 optimize2/export_json 约定）；防守仓 512890；
窗口 2019-01-18（512890 上市）起；佣金万1（ETF↔ETF 双边、ETF↔现金 单边）；
空仓收益按 0 计（货币基金约 1.5%/年，偏保守）；
换仓日收益按真实成交拆段：旧仓隔夜段(昨收→今开) + 新仓日内段(今开→今收)，
与 export_json 的"真实成交口径"一致。
"""
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

from backtest import calc_signals, annualized, max_drawdown
from export_json import load

FEE = 0.0001
SWITCH_ATK = pd.Timestamp('2024-06-28')
START = pd.Timestamp('2019-01-18')


def leg_returns(o: pd.Series, c: pd.Series):
    """隔夜(昨收→今开) / 日内(今开→今收) / 收盘→收盘 三段日收益"""
    return o / c.shift(1) - 1, c / o - 1, c / c.shift(1) - 1


def run_variant(idx, sig, legs, div_c, div_ma, exec_mode='open', use_cash=False):
    """
    legs: {'atk': (on, iday, cc), 'div': (on, iday, cc)} 日收益 Series，已按 idx 对齐
    exec_mode: 'open'=信号次日开盘成交（现行）, 'close'=信号日尾盘成交
    use_cash : True 时卖信号且防守腿收盘 < div_ma → 空仓；
               空仓中防守腿收复均线 → 回防守腿（同一均线参数，无新增信号）
    返回 (nav, state_series, trades)
    """
    n = len(idx)
    nav = np.ones(n)
    states = []
    state = 'div'
    pending = None
    trades = 0

    def fee_for(old, new):
        # ETF↔ETF 卖一买一双边；有一侧是现金只扣单边
        return 2 * FEE if old != 'cash' and new != 'cash' else FEE

    for i in range(n):
        # ---- 当日收益（昨收 → 今收），T+1 开盘执行昨日信号 ----
        if i > 0:
            if exec_mode == 'open' and pending is not None and pending != state:
                old, new = state, pending
                if old == 'cash':
                    r = legs[new][1].iloc[i]                        # 今开买入，吃日内段
                elif new == 'cash':
                    r = legs[old][0].iloc[i]                        # 今开卖出，吃隔夜段
                else:
                    r = (1 + legs[old][0].iloc[i]) * (1 + legs[new][1].iloc[i]) - 1
                r -= fee_for(old, new)
                state = new
                trades += 1
            elif state == 'cash':
                r = 0.0
            else:
                r = legs[state][2].iloc[i]
            nav[i] = nav[i - 1] * (1 + r)

        # ---- 收盘：信号产生 / 空仓再评估 ----
        target = None
        if sig['buy_sig'].iloc[i]:
            target = 'atk'
        elif sig['sell_sig'].iloc[i]:
            if use_cash and not np.isnan(div_ma[i]) and div_c.iloc[i] < div_ma[i]:
                target = 'cash'
            else:
                target = 'div'
        elif (use_cash and state == 'cash' and not np.isnan(div_ma[i])
              and div_c.iloc[i] >= div_ma[i]):
            target = 'div'

        if target is not None and target != state:
            if exec_mode == 'close':
                # 尾盘成交：当日收益已由旧仓吃完，此处只扣费并切换
                nav[i] *= (1 - fee_for(state, target))
                state = target
                trades += 1
            else:
                pending = target
        states.append(state)

    nav = pd.Series(nav, index=idx)
    return nav, pd.Series(states, index=idx), trades


def main():
    avg = load('avg_880003.csv')
    etf1000 = load('etf_512100_hfq.csv')
    etf2000e = load('etf_159552_hfq.csv')
    etfdiv = load('etf_512890_hfq.csv')

    # 方案C 信号（全历史计算保证 DIF 预热，再按窗口切片）
    sig_full = calc_signals(avg['close'], sell_anywhere=True, fast=17, slow=34, sig_n=9)

    # 进攻腿拼接（同 export_json 口径）
    atk_o = pd.concat([etf1000['open'][etf1000.index < SWITCH_ATK],
                       etf2000e['open'][etf2000e.index >= SWITCH_ATK]])
    atk_c = pd.concat([etf1000['close'][etf1000.index < SWITCH_ATK],
                       etf2000e['close'][etf2000e.index >= SWITCH_ATK]])

    idx = etfdiv.index.intersection(atk_c.index).intersection(avg.index)
    idx = idx[idx >= START]

    on_a, id_a, cc_a = leg_returns(atk_o, atk_c)
    on_d, id_d, cc_d = leg_returns(etfdiv['open'], etfdiv['close'])
    # 拼接守卫：切换日跨界持有的当日收益按 512100 口径（同 optimize2 约定）
    on1, id1, cc1 = leg_returns(etf1000['open'], etf1000['close'])
    if SWITCH_ATK in idx:
        on_a.loc[SWITCH_ATK] = on1.loc[SWITCH_ATK]
        id_a.loc[SWITCH_ATK] = id1.loc[SWITCH_ATK]
        cc_a.loc[SWITCH_ATK] = cc1.loc[SWITCH_ATK]

    legs = {
        'atk': (on_a.loc[idx].fillna(0), id_a.loc[idx].fillna(0), cc_a.loc[idx].fillna(0)),
        'div': (on_d.loc[idx].fillna(0), id_d.loc[idx].fillna(0), cc_d.loc[idx].fillna(0)),
    }
    div_c = etfdiv['close']
    ma20 = div_c.rolling(20).mean()
    ma60 = div_c.rolling(60).mean()
    sig = sig_full.loc[idx]

    variants = [
        ('V0 基线(两腿,T+1开盘)', dict(exec_mode='open', use_cash=False), None),
        ('V1 +空仓MA20',          dict(exec_mode='open', use_cash=True), ma20),
        ('V2 +空仓MA60',          dict(exec_mode='open', use_cash=True), ma60),
        ('V3 尾盘成交',           dict(exec_mode='close', use_cash=False), None),
        ('V4 尾盘+空仓MA20',      dict(exec_mode='close', use_cash=True), ma20),
        ('V5 尾盘+空仓MA60',      dict(exec_mode='close', use_cash=True), ma60),
    ]

    print('回测区间: %s ~ %s  共 %d 个交易日' % (idx[0].date(), idx[-1].date(), len(idx)))
    print('口径: 方案C信号(17/34/9 零下买/任意卖) 进攻仓512100→159552拼接 防守仓512890')
    print('      换仓日按真实成交拆段(隔夜+日内), ETF↔ETF双边万1, ETF↔现金单边, 空仓收益0')
    print('=' * 88)
    fmt = '%-24s %9s %8s %9s %6s %7s %7s %7s'
    print(fmt % ('方案', '累计收益', '年化', '最大回撤', '换仓', '进攻%', '防守%', '空仓%'))
    print('-' * 88)

    navs = {}
    for name, kw, ma in variants:
        div_ma = ma.loc[idx].values if ma is not None else np.full(len(idx), np.nan)
        nav, st, trades = run_variant(idx, sig, legs, div_c.loc[idx], div_ma, **kw)
        navs[name] = nav
        occ = st.value_counts(normalize=True) * 100
        print(fmt % (name,
                     '%.1f%%' % ((nav.iloc[-1] - 1) * 100),
                     '%.2f%%' % (annualized(nav) * 100),
                     '%.1f%%' % (max_drawdown(nav) * 100),
                     trades,
                     '%.1f' % occ.get('atk', 0.0),
                     '%.1f' % occ.get('div', 0.0),
                     '%.1f' % occ.get('cash', 0.0)))
    print('-' * 88)

    # 分年度收益
    print('\n分年度收益:')
    years = sorted(set(d.year for d in idx))
    header = '%-24s' % '方案' + ''.join(' %8d' % y for y in years)
    print(header)
    for name, _kw, _ma in variants:
        nav = navs[name]
        y = nav.resample('YE').last()
        rets = y.pct_change()
        rets.iloc[0] = y.iloc[0] / nav.iloc[0] - 1
        print('%-24s' % name + ''.join(' %+7.1f%%' % (v * 100) for v in rets.values))

    # 空仓时段明细（V1/V2，看钱在什么时候躲过了什么）
    for name, kw, ma in [('V1', variants[1][1], ma20), ('V2', variants[2][1], ma60)]:
        div_ma = ma.loc[idx].values
        _nav, st, _t = run_variant(idx, sig, legs, div_c.loc[idx], div_ma, **kw)
        cash = st == 'cash'
        if not cash.any():
            continue
        print('\n%s 空仓时段:' % name)
        seg_start = None
        prev = False
        for i, d in enumerate(idx):
            if cash.iloc[i] and not prev:
                seg_start = d
            elif not cash.iloc[i] and prev:
                print('  %s ~ %s  (%d 个交易日)' % (seg_start.date(), idx[i - 1].date(),
                                                    (idx[i - 1] - seg_start).days))
            prev = cash.iloc[i]
        if prev:
            print('  %s ~ %s  (%d 个交易日, 持续中)' % (seg_start.date(), idx[-1].date(),
                                                        (idx[-1] - seg_start).days))


if __name__ == '__main__':
    main()
