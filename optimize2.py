#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
参数寻优 v2（当前网站口径，不影响方案C）：
  进攻仓 = 512100（2024-06-28 前）/ 159552（之后）拼接，防守仓 = 512890 红利低波
  回测窗口 = 2019-01-18（512890 上市）起，含双边万1佣金
搜索空间：
  MACD 参数 fast/slow/sig
  信号类型 turn(DIF拐点) / cross(DIF与DEA金叉死叉)
  买入阈值 buy_th（DIF 低于该值才买）/ 卖出阈值 sell_th（DIF 高于该值才卖）
排名按全区间累计收益；样本内外以 SPLIT 分界；对头部组合做邻域稳健性检查。
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

from backtest import ema, annualized, max_drawdown
from export_json import load

FEE = 0.0001               # 单边万1，换仓扣双边
MIN_TRADES = 6
SPLIT = '2023-01-01'       # 样本内外分界
SWITCH_ATK = pd.Timestamp('2024-06-28')   # 进攻仓拼接日（512100 → 159552）


def run(avg_close, idx, r_atk, rdiv, fast, slow, sig_n, mode, buy_th, sell_th):
    dif_full = ema(avg_close, fast) - ema(avg_close, slow)
    dif = dif_full.loc[idx]
    d1 = dif.shift(1)
    d2 = dif.shift(2)
    if mode == 'turn':
        up = (d1 < d2) & (dif > d1)
        down = (d1 > d2) & (dif < d1)
    else:  # cross：金叉/死叉
        dea = ema(dif_full, sig_n).loc[idx]
        e1 = dea.shift(1)
        up = (d1 <= e1) & (dif > dea)
        down = (d1 >= e1) & (dif < dea)
    buy_sig = (up & (dif < buy_th)).fillna(False).values
    sell_sig = (down & (dif > sell_th)).fillna(False).values

    n = len(idx)
    hold = np.zeros(n, dtype=bool)
    state = False
    pending = None
    trades = 0
    for i in range(n):
        if pending is not None:
            if pending and not state:
                state = True
                trades += 1
            elif not pending and state:
                state = False
                trades += 1
            pending = None
        hold[i] = state
        if buy_sig[i]:
            pending = True
        elif sell_sig[i]:
            pending = False

    if trades < MIN_TRADES:
        return None
    sr = np.where(hold, r_atk, rdiv)
    switch = np.zeros(n)
    switch[1:] = np.abs(hold[1:].astype(int) - hold[:-1].astype(int))
    sr = sr - switch * 2 * FEE
    nav = pd.Series(np.cumprod(1 + sr), index=idx)
    return nav, trades


def oos_stats(nav):
    oos = nav.loc[nav.index >= pd.Timestamp(SPLIT)]
    oos = oos / oos.iloc[0]
    return annualized(oos) * 100, max_drawdown(oos) * 100


def main():
    avg = load('avg_880003.csv')
    etf1000 = load('etf_512100_hfq.csv')
    etf2000e = load('etf_159552_hfq.csv')
    etfdiv = load('etf_512890_hfq.csv')

    # 进攻腿拼接（同 export_json 口径）
    atk_close = pd.concat([etf1000['close'][etf1000.index < SWITCH_ATK],
                           etf2000e['close'][etf2000e.index >= SWITCH_ATK]])

    idx = etfdiv.index.intersection(atk_close.index).intersection(avg.index)
    idx = idx[idx >= pd.Timestamp('2019-01-18')]
    avg_close = avg['close']

    r_atk = atk_close.pct_change()
    r1000 = etf1000['close'].pct_change()
    if SWITCH_ATK in r_atk.index:
        # 拼接日边界收益失真（159552收盘/512100前收），跨界持有当日实际仍为512100
        r_atk.loc[SWITCH_ATK] = r1000.loc[SWITCH_ATK]
    r_atk = r_atk.loc[idx].fillna(0).values
    rdiv = etfdiv.loc[idx, 'close'].pct_change().fillna(0).values

    # 基准净值
    nav_atk = pd.Series(np.cumprod(1 + r_atk), index=idx)
    nav_div = pd.Series(np.cumprod(1 + rdiv), index=idx)

    fasts = [5, 6, 8, 10, 12, 14, 16, 17, 18, 20, 22]
    slows = [21, 26, 30, 34, 38, 45, 52, 60]
    signos = [5, 7, 9, 11]
    buy_ths = [0.0, np.inf]        # 零下才买 / 任意位置
    sell_ths = [-np.inf, 0.0]      # 任意位置卖 / 零上才卖

    results = []
    tested = 0
    for fast in fasts:
        for slow in slows:
            if fast >= slow:
                continue
            for mode in ['turn', 'cross']:
                # turn 模式不依赖 DEA，sig 只取 9 避免重复
                sig_list = [9] if mode == 'turn' else signos
                for sig_n in sig_list:
                    for bt in buy_ths:
                        for st in sell_ths:
                            tested += 1
                            out = run(avg_close, idx, r_atk, rdiv,
                                      fast, slow, sig_n, mode, bt, st)
                            if out is None:
                                continue
                            nav, trades = out
                            results.append({
                                'fast': fast, 'slow': slow, 'sig': sig_n,
                                'mode': mode,
                                'buy': '零下' if bt == 0 else '任意',
                                'sell': '任意' if st == -np.inf else '零上',
                                'cum': float(nav.iloc[-1] - 1),
                                'ann': annualized(nav),
                                'mdd': max_drawdown(nav),
                                'trades': trades,
                                'nav': nav,
                            })
    print('回测区间: %s ~ %s  共 %d 个交易日' % (idx[0].date(), idx[-1].date(), len(idx)))
    print('共测试 %d 个参数组合，%d 个有效（换仓>=%d次）'
          % (tested, len(results), MIN_TRADES))
    print('基准: 进攻仓(拼接)持有 累计 %.1f%% 年化 %.2f%% 回撤 %.1f%% | '
          '512890持有 累计 %.1f%% 年化 %.2f%% 回撤 %.1f%%'
          % ((nav_atk.iloc[-1] - 1) * 100, annualized(nav_atk) * 100,
             max_drawdown(nav_atk) * 100,
             (nav_div.iloc[-1] - 1) * 100, annualized(nav_div) * 100,
             max_drawdown(nav_div) * 100))

    results.sort(key=lambda r: r['cum'], reverse=True)

    # 方案C基准：17/34/9 turn 零下买 任意卖
    base_key = (17, 34, 9, 'turn', '零下', '任意')
    for r in results:
        if (r['fast'], r['slow'], r['sig'], r['mode'], r['buy'], r['sell']) == base_key:
            r['is_C'] = True

    print('\n%-4s %-14s %-6s %-14s %9s %8s %9s %6s | 样本外(%s后) %8s %9s'
          % ('排名', 'MACD', '信号', '买/卖阈值', '累计收益', '年化', '最大回撤', '换仓',
             SPLIT[:4], '年化', '最大回撤'))
    for rank, r in enumerate(results[:15], 1):
        oos_ann, oos_mdd = oos_stats(r['nav'])
        print('%-4d %-14s %-6s %-14s %8.1f%% %7.2f%% %8.1f%% %6d | %16.2f%% %8.1f%%'
              % (rank, '%d/%d/%d' % (r['fast'], r['slow'], r['sig']),
                 '拐点' if r['mode'] == 'turn' else '金叉叉',
                 '%s买/%s卖' % (r['buy'], r['sell']),
                 r['cum'] * 100, r['ann'] * 100, r['mdd'] * 100, r['trades'],
                 oos_ann, oos_mdd))

    for rank, r in enumerate(results, 1):
        if r.get('is_C'):
            oos_ann, oos_mdd = oos_stats(r['nav'])
            print('\n现行方案C(17/34/9 拐点 零下买/任意卖) 全量排名 %d/%d: '
                  '累计 %.1f%% 年化 %.2f%% 回撤 %.1f%% 换仓 %d | 样本外年化 %.2f%% 回撤 %.1f%%'
                  % (rank, len(results), r['cum'] * 100, r['ann'] * 100,
                     r['mdd'] * 100, r['trades'], oos_ann, oos_mdd))
            break
    else:
        print('\n现行方案C不在有效结果中')

    # 头部组合邻域稳健性：同模式同阈值、fast±2 / slow±4 内的所有组合
    print('\n邻域稳健性（前3名，fast±2/slow±4 同模式同阈值的全部组合）:')
    for rank, r in enumerate(results[:3], 1):
        neigh = [x for x in results
                 if x['mode'] == r['mode'] and x['buy'] == r['buy']
                 and x['sell'] == r['sell']
                 and abs(x['fast'] - r['fast']) <= 2
                 and abs(x['slow'] - r['slow']) <= 4]
        cums = sorted(x['cum'] for x in neigh)
        print('  第%d名 %d/%d/%d %s %s买/%s卖: 邻域%d组 累计收益 %.0f%%~%.0f%% (中位 %.0f%%)'
              % (rank, r['fast'], r['slow'], r['sig'],
                 '拐点' if r['mode'] == 'turn' else '金叉叉',
                 r['buy'], r['sell'], len(neigh),
                 cums[0] * 100, cums[-1] * 100,
                 float(np.median(cums)) * 100))

    # 前3名分年度收益
    print('\n前3名分年度收益:')
    for rank, r in enumerate(results[:3], 1):
        nav = r['nav']
        y = nav.resample('YE').last()
        rets = y.pct_change()
        rets.iloc[0] = y.iloc[0] / nav.iloc[0] - 1
        line = '  第%d名 %d/%d %s: ' % (rank, r['fast'], r['slow'],
                                        '拐点' if r['mode'] == 'turn' else '金叉叉')
        line += '  '.join('%d:%+.1f%%' % (d.year, v * 100)
                          for d, v in rets.items())
        print(line)


if __name__ == '__main__':
    main()
