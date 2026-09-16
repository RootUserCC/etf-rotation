#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
防守仓换煤炭ETF(515220)/红利低波(512890) + "红黄柱"买入过滤 回测

- 信号：方案C不变——880003 平均股价 DIF(17,34) 拐点（零下拐头买进攻，拐头即卖换防守）
- 进攻腿：现行拼接口径（2024-06-28 前 512100，之后 159552 中证2000增强）
- 防守腿：515220 煤炭ETF 或 512890 红利低波
- 红黄柱过滤（通达信四色柱口径）：
    红柱 = MACD柱>0 且放大；黄柱 = MACD柱>0 且缩头 → 合并即 MACD柱>0（DIF>DEA）
    换入某条腿时，要求"被买入标的自身"前一收盘的 MACD柱>0，否则持仓等待，
    每天开盘检查一次，直到满足才执行（不产生空仓状态）
  MACD柱参数跑两版：12/26/9（通达信默认）与 17/34/9（方案C口径）
- 费用：ETF 双边各万1；T日收盘信号，满足条件的 T+k 日开盘成交
输出见 result_coal.txt
"""
import sys
import io
import os

import numpy as np
import pandas as pd

if sys.platform == 'win32' and sys.stdout is not None and not getattr(sys.stdout, '_utf8_wrapped', False):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stdout._utf8_wrapped = True

from backtest import calc_signals, max_drawdown, annualized, ema
from compare_stock515050 import load, splice_atk_etf

FEE = 0.0001                     # ETF 单边万1
COST_SWITCH = 2 * FEE


def macdval(close: pd.Series, fast: int, slow: int, sig_n: int) -> pd.Series:
    dif = ema(close, fast) - ema(close, slow)
    dea = ema(dif, sig_n)
    return (dif - dea) * 2


def simulate(idx, sig, atk, dfn, gate_atk=None, gate_def=None):
    """T收盘信号→满足过滤条件后的首个开盘成交。
    atk/dfn: dict(close_ret, oc_ret)；gate_*: bool Series（当日开盘是否允许换入该腿，已shift）
    返回 nav, trades, avg_delay(信号到成交的平均交易日数), hold_atk占比"""
    n = len(idx)
    rets = np.zeros(n)
    state = False
    pending = None
    pend_pos = None
    trades = 0
    delays = []
    hold = 0
    for i in range(n):
        r_atk = atk['close_ret'].iloc[i]
        r_def = dfn['close_ret'].iloc[i]
        if pending is not None:
            want_atk = (pending == 'atk')
            if state == want_atk:                    # 与现持仓同向的信号，无需操作
                pending = None
                rets[i] = r_atk if state else r_def
            else:
                gate = gate_atk if want_atk else gate_def
                ok = True if gate is None else bool(gate.iloc[i])
                if ok:
                    state = want_atk
                    trades += 1
                    delays.append(i - pend_pos)
                    oc = atk['oc_ret'].iloc[i] if state else dfn['oc_ret'].iloc[i]
                    rets[i] = oc - COST_SWITCH
                    pending = None
                else:                                # 过滤条件未满足，持仓等待
                    rets[i] = r_atk if state else r_def
        else:
            rets[i] = r_atk if state else r_def
        if state:
            hold += 1
        if pending is None:
            if sig['buy_sig'].iloc[i]:
                pending, pend_pos = 'atk', i
            elif sig['sell_sig'].iloc[i]:
                pending, pend_pos = 'def', i
    nav = (1 + pd.Series(rets, index=idx)).cumprod()
    avg_delay = float(np.mean(delays)) if delays else 0.0
    return nav, trades, avg_delay, hold / n * 100


def leg_series(close, open_):
    return {'close_ret': close.pct_change().fillna(0.0),
            'oc_ret': (close / open_ - 1).fillna(0.0)}


def main():
    avg = load('avg_880003.csv')
    ddiv = load('etf_512890_hfq.csv')
    dcoal = load('etf_515220_hfq.csv')

    sig_full = calc_signals(avg['close'], sell_anywhere=True, fast=17, slow=34, sig_n=9)

    # 日历不含煤炭（515220 2020-03-02 才上市），窗口2 再按上市日切片
    calendar = avg.index.intersection(ddiv.index)
    calendar = calendar[calendar >= '2019-01-18']

    # 进攻腿（拼接口径，全历史计算再切片）
    atk_ret_full, atk_oc_full = splice_atk_etf(calendar)

    defenses = {'512890红利低波': ddiv, '515220煤炭ETF': dcoal}
    # MACD柱>0 门限（按腿自身收盘，全历史算好后 shift(1)，当日开盘用前一收盘值）
    gates = {}
    for name, df in defenses.items():
        c = df['close']
        gates[name] = {
            '12/26/9': (macdval(c, 12, 26, 9) > 0).shift(1).fillna(False),
            '17/34/9': (macdval(c, 17, 34, 9) > 0).shift(1).fillna(False),
        }
    atk_close_full = None  # 拼接腿的门限：用拼接 close 序列重建
    # splice_atk_etf 只回了收益，这里重拼 close 用于 MACD
    d1000 = load('etf_512100_hfq.csv')
    d2000 = load('etf_159552_hfq.csv')
    cut = pd.Timestamp('2024-06-28')
    atk_close = pd.concat([d1000['close'][d1000.index < cut],
                           d2000['close'][d2000.index >= cut]])
    atk_close.index = atk_close.index.normalize()
    atk_close = atk_close[~atk_close.index.duplicated(keep='last')].sort_index()
    gates_atk = {
        '12/26/9': (macdval(atk_close, 12, 26, 9) > 0).shift(1).fillna(False),
        '17/34/9': (macdval(atk_close, 17, 34, 9) > 0).shift(1).fillna(False),
    }

    windows = [('2019-01-18', '窗口1：2019-01-18 起（仅红利低波，煤炭未上市）'),
               ('2020-03-02', '窗口2：515220 上市起'),
               ('2023-09-09', '窗口3：近3年')]
    for wstart, wnote in windows:
        idx = calendar[calendar >= wstart]
        sig = sig_full.reindex(idx).fillna(False)
        atk = {'close_ret': atk_ret_full.reindex(idx).fillna(0.0),
               'oc_ret': atk_oc_full.reindex(idx).fillna(0.0)}
        print('=' * 78)
        print('%s（%s ~ %s，%d 个交易日）' % (wnote, idx[0].date(), idx[-1].date(), len(idx)))
        print('=' * 78)
        print('%-34s %10s %9s %10s %8s %8s %8s'
              % ('组合', '累计收益', '年化', '最大回撤', '换仓', '均延迟', '进攻占比'))
        for dname, ddf in defenses.items():
            if wstart == '2019-01-18' and dname.startswith('515220'):
                continue
            dfn = {'close_ret': ddf['close'].reindex(idx).pct_change().fillna(0.0),
                   'oc_ret': (ddf['close'] / ddf['open'] - 1).reindex(idx).fillna(0.0)}
            variants = [('无过滤', None, None)]
            for pname in ('12/26/9', '17/34/9'):
                variants.append(('红黄柱过滤(%s)' % pname,
                                 gates_atk[pname].reindex(idx).fillna(False),
                                 gates[dname][pname].reindex(idx).fillna(False)))
            for vname, ga, gd in variants:
                nav, trades, delay, holdpct = simulate(idx, sig, atk, dfn, ga, gd)
                print('%-34s %10s %9s %10s %8d %7.1f天 %7.1f%%'
                      % ('%s防守·%s' % (dname, vname),
                         '%+.2f%%' % ((nav.iloc[-1] - 1) * 100),
                         '%+.2f%%' % (annualized(nav) * 100),
                         '%.2f%%' % (max_drawdown(nav) * 100),
                         trades, delay, holdpct))
            # 纯持有基准
            hold_nav = ddf['close'].reindex(idx) / ddf['close'].reindex(idx).iloc[0]
            print('%-34s %10s %9s %10s %8s %8s %8s'
                  % ('%s 纯持有' % dname,
                     '%+.2f%%' % ((hold_nav.iloc[-1] - 1) * 100),
                     '%+.2f%%' % (annualized(hold_nav) * 100),
                     '%.2f%%' % (max_drawdown(hold_nav) * 100), '-', '-', '-'))
        atk_hold = (1 + atk['close_ret']).cumprod()
        print('%-34s %10s %9s %10s %8s %8s %8s'
              % ('进攻腿(2000增强拼接) 纯持有',
                 '%+.2f%%' % ((atk_hold.iloc[-1] - 1) * 100),
                 '%+.2f%%' % (annualized(atk_hold) * 100),
                 '%.2f%%' % (max_drawdown(atk_hold) * 100), '-', '-', '-'))
        print()


if __name__ == '__main__':
    main()
