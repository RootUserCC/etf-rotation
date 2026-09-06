#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
收益最大化信号搜索（不改任何现有文件；复用 reduce_loss.py 已对账的回测循环）

信号家族（均为 T 日收盘信号、T+1 开盘成交、双边万1）：
  F1 DIF 拐点家族    ：fast∈10..24(step2), slow∈26..44(step2)；买∈{零下拐头,任意拐头}，卖∈{任意拐头,零上拐头}
  F2 DIF/DEA 金叉死叉：粗网格 fast∈{10,12,14,17,20,24}, slow∈{26,30,34,38,44}, sig=9；
                       买∈{零上,零下,任意}金叉，卖∈{任意,零上}死叉
  F3 四色柱 bar_mode  ：同 F2 粗网格，buy_anywhere∈{False,True}（对应方案D/E）
  F4 均线家族         ：收盘 vs MA(N) 站上买/跌破卖 N∈{10,20,30,40,60}；
                       双均线金叉死叉 fast∈{5,10,15,20}, slow∈{20,30,40,60,90}(slow>fast)
  F5 通道突破         ：N 日新高买 / M 日新低卖，N∈{10,20,40,60}，M∈{5,10,20}
  F6 混合家族         ：(a) DIF 拐点买（零下/任意）+ 死叉卖（任意），F2 粗网格
                       (b) N 日新高买 + DIF(f,s) 任意拐头卖，N∈{10,20,40,60}，(f,s)∈{(12,26),(17,34),(20,38)}

评估口径（双窗口交叉验证）：
  窗口A（生产口径）：2019-01-18 起，进攻仓拼接(2024-06-28 前=512100，后=159552)，防守=512890；
                    真实成交口径净值（换仓日拆 旧仓隔夜段+新仓日内段，扣双边万1）
  窗口B（十年口径）：2016-11 起，进攻=512100，防守=510880；同一真实成交口径（与窗口A完全一致，便于同窗可比）
  每个组合输出：年化/最大回撤/Calmar/换仓次数，并按时间前后两半分别算年化检验稳定性

筛选规则：
  窗口A Top15 按年化排序；换仓<10 标"样本太少"；前后半年化异号或差距>15pp 标"不稳定"
  交集 = 窗口A Top15 且 窗口B 年化 > 同窗口基线方案C
基线：方案C = DIF(17,34) 零下拐头买 / 任意拐头卖
"""
import sys
import io
import os

import numpy as np
import pandas as pd

if sys.platform == 'win32' and not getattr(sys.stdout, '_utf8_wrapped', False):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stdout._utf8_wrapped = True

from backtest import ema, annualized, max_drawdown
from reduce_loss import run_custom, build_wealth, load, FEE, SWITCH_ATK

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
START_A = pd.Timestamp('2019-01-18')     # 512890 上市
START_B = pd.Timestamp('2016-11-01')     # 512100 上市（README 十年窗口口径）

# MACD 网格
FASTS_F1 = list(range(10, 25, 2))        # F1 细网格
SLOWS_F1 = list(range(26, 45, 2))
FASTS_F2 = [10, 12, 14, 17, 20, 24]      # F2/F3/F6a 粗网格（控制组合总数）
SLOWS_F2 = [26, 30, 34, 38, 44]


# ---------------------------------------------------------------------------
# 信号家族生成：每个组合 = (family, name, desc, buy_sig, sell_sig)
# ---------------------------------------------------------------------------
def gen_combos(avg_close):
    combos = []
    dif_cache = {}

    def get_dif(f, s):
        if (f, s) not in dif_cache:
            dif_cache[(f, s)] = ema(avg_close, f) - ema(avg_close, s)
        return dif_cache[(f, s)]

    def turns(dif):
        d1, d2 = dif.shift(1), dif.shift(2)
        up = (d1 < d2) & (dif > d1)
        down = (d1 > d2) & (dif < d1)
        return up.fillna(False), down.fillna(False)

    def crosses(dif, sig_n=9):
        dea = ema(dif, sig_n)
        d1, e1 = dif.shift(1), dea.shift(1)
        golden = (d1 <= e1) & (dif > dea)
        dead = (d1 >= e1) & (dif < dea)
        return golden.fillna(False), dead.fillna(False)

    # ---- F1 DIF 拐点家族 ----
    for f in FASTS_F1:
        for s in SLOWS_F1:
            if f >= s:
                continue
            dif = get_dif(f, s)
            up, down = turns(dif)
            for buy_mode, buy_sig in [('零下', up & (dif < 0)), ('任意', up)]:
                for sell_mode, sell_sig in [('任意', down), ('零上', down & (dif > 0))]:
                    name = 'F1 DIF(%d,%d)拐点 %s买/%s卖' % (f, s, buy_mode, sell_mode)
                    desc = 'DIF(%d,%d) %s拐头向上即买，%s拐头向下即卖' % (
                        f, s, '零下' if buy_mode == '零下' else '任意位置',
                        '任意位置' if sell_mode == '任意' else '零上')
                    combos.append(('F1拐点', name, desc, buy_sig, sell_sig))

    # ---- F2 金叉死叉家族 ----
    for f in FASTS_F2:
        for s in SLOWS_F2:
            if f >= s:
                continue
            dif = get_dif(f, s)
            golden, dead = crosses(dif)
            for buy_mode, buy_sig in [('零上', golden & (dif > 0)),
                                      ('零下', golden & (dif < 0)),
                                      ('任意', golden)]:
                for sell_mode, sell_sig in [('任意', dead), ('零上', dead & (dif > 0))]:
                    name = 'F2 MACD(%d,%d,9)叉 %s金叉买/%s死叉卖' % (f, s, buy_mode, sell_mode)
                    desc = 'DIF(%d,%d) %s位置金叉DEA买，%s位置死叉DEA卖' % (
                        f, s, buy_mode, sell_mode)
                    combos.append(('F2金叉叉', name, desc, buy_sig, sell_sig))

    # ---- F3 四色柱 bar_mode 家族 ----
    for f in FASTS_F2:
        for s in SLOWS_F2:
            if f >= s:
                continue
            dif = get_dif(f, s)
            dea = ema(dif, 9)
            m = (dif - dea) * 2
            m_prev = m.shift(1)
            blue = ((m >= 0) & (m_prev > 0) & (m < m_prev)).fillna(False)
            red = ((m >= 0) & ~blue).fillna(False)
            up, _ = turns(dif)
            sell_sig = (blue & red.shift(1).fillna(False)).fillna(False)
            for ba, buy_sig in [(False, ((up & (dif < 0)) | (red & blue.shift(1).fillna(False)))),
                                (True, up)]:
                tag = '方案E口径' if ba else '方案D口径'
                name = 'F3 四色柱(%d,%d,9) %s' % (f, s, tag)
                desc = ('四色柱(%d,%d)：MACD柱红变蓝卖；%s买'
                        % (f, s, 'DIF任意位置拐头向上即' if ba else 'DIF零下拐头或柱蓝变红'))
                combos.append(('F3四色柱', name, desc, buy_sig.fillna(False), sell_sig))

    # ---- F4 均线家族 ----
    c = avg_close
    c1 = c.shift(1)
    for n in [10, 20, 30, 40, 60]:
        ma = c.rolling(n).mean()
        ma1 = ma.shift(1)
        buy_sig = ((c > ma) & (c1 <= ma1)).fillna(False)
        sell_sig = ((c < ma) & (c1 >= ma1)).fillna(False)
        name = 'F4 MA%d 站上买/跌破卖' % n
        desc = '收盘价上穿MA%d买，下穿MA%d卖' % (n, n)
        combos.append(('F4单均线', name, desc, buy_sig, sell_sig))
    for f in [5, 10, 15, 20]:
        for s in [20, 30, 40, 60, 90]:
            if f >= s:
                continue
            maf, mas = c.rolling(f).mean(), c.rolling(s).mean()
            f1, s1 = maf.shift(1), mas.shift(1)
            buy_sig = ((maf > mas) & (f1 <= s1)).fillna(False)
            sell_sig = ((maf < mas) & (f1 >= s1)).fillna(False)
            name = 'F4 MA(%d,%d)金叉买/死叉卖' % (f, s)
            desc = 'MA%d上穿MA%d买，下穿卖' % (f, s)
            combos.append(('F4双均线', name, desc, buy_sig, sell_sig))

    # ---- F5 通道突破家族 ----
    for n in [10, 20, 40, 60]:
        hh = c.shift(1).rolling(n).max()
        buy_sig = (c > hh).fillna(False)
        for m in [5, 10, 20]:
            ll = c.shift(1).rolling(m).min()
            sell_sig = (c < ll).fillna(False)
            name = 'F5 通道(%d,%d) 新高买/新低卖' % (n, m)
            desc = '收盘价创%d日新高买，创%d日新低卖' % (n, m)
            combos.append(('F5通道', name, desc, buy_sig, sell_sig))

    # ---- F6 混合家族 ----
    for f in FASTS_F2:
        for s in SLOWS_F2:
            if f >= s:
                continue
            dif = get_dif(f, s)
            up, _ = turns(dif)
            _, dead = crosses(dif)
            for buy_mode, buy_sig in [('零下', up & (dif < 0)), ('任意', up)]:
                name = 'F6a DIF(%d,%d)%s拐点买+死叉卖' % (f, s, buy_mode)
                desc = 'DIF(%d,%d) %s拐头向上买，下穿DEA(死叉)卖' % (
                    f, s, '零下' if buy_mode == '零下' else '任意位置')
                combos.append(('F6混合', name, desc, buy_sig, dead))
    for n in [10, 20, 40, 60]:
        hh = c.shift(1).rolling(n).max()
        buy_sig = (c > hh).fillna(False)
        for f, s in [(12, 26), (17, 34), (20, 38)]:
            _, down = turns(get_dif(f, s))
            name = 'F6b %d日新高买+DIF(%d,%d)拐头卖' % (n, f, s)
            desc = '收盘价创%d日新高买，DIF(%d,%d)任意位置拐头向下卖' % (n, f, s)
            combos.append(('F6混合', name, desc, buy_sig, down))

    return combos


# ---------------------------------------------------------------------------
# 评估
# ---------------------------------------------------------------------------
def evaluate(combo, idx, oatk, catk, odiv, cdiv):
    family, name, desc, buy_sig, sell_sig = combo
    sig = pd.DataFrame({'buy_sig': buy_sig, 'sell_sig': sell_sig}).reindex(idx).fillna(False)
    trades = run_custom(idx, sig, oatk, catk, odiv, cdiv)
    wealth = build_wealth(idx, trades, oatk, catk, odiv, cdiv)
    ann = annualized(wealth)
    mdd = max_drawdown(wealth)
    mid = idx[0] + (idx[-1] - idx[0]) / 2
    w1 = wealth[wealth.index < mid]
    w2 = wealth[wealth.index >= mid]
    ann1 = annualized(w1 / w1.iloc[0]) if len(w1) > 1 else float('nan')
    ann2 = annualized(w2 / w2.iloc[0]) if len(w2) > 1 else float('nan')
    return {
        'family': family, 'name': name, 'desc': desc,
        'cum': float(wealth.iloc[-1] - 1), 'ann': ann, 'mdd': mdd,
        'calmar': ann / abs(mdd) if mdd < 0 else float('nan'),
        'n_trades': len(trades), 'ann1': ann1, 'ann2': ann2,
    }


def fmt(x, pct=True):
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return '—'
    return ('%.2f%%' % (x * 100)) if pct else ('%.2f' % x)


def print_top(rows, title, topn, baseline_ann):
    print('=' * 100)
    print(title)
    print('=' * 100)
    print('| 排名 | 组合 | 年化 | 最大回撤 | Calmar | 换仓 | 前半年化 | 后半年化 | 标注 |')
    print('|---|---|---|---|---|---|---|---|---|')
    rows = sorted(rows, key=lambda r: r['ann'], reverse=True)
    for rank, r in enumerate(rows[:topn], 1):
        flags = []
        if r['n_trades'] < 10:
            flags.append('样本太少')
        if np.sign(r['ann1']) != np.sign(r['ann2']) or abs(r['ann1'] - r['ann2']) > 0.15:
            flags.append('不稳定')
        if r['ann'] <= baseline_ann:
            flags.append('≤基线')
        print('| %d | %s | %s | %s | %s | %d | %s | %s | %s |'
              % (rank, r['name'], fmt(r['ann']), fmt(r['mdd']), fmt(r['calmar'], False),
                 r['n_trades'], fmt(r['ann1']), fmt(r['ann2']),
                 '；'.join(flags) if flags else ''))
    return rows[:topn]


def main():
    avg = load('avg_880003.csv')
    etf1000 = load('etf_512100_hfq.csv')
    etf2000e = load('etf_159552_hfq.csv')
    etfdivA = load('etf_512890_hfq.csv')
    etfdivB = load('etf_510880_hfq.csv')

    # ---- 窗口A：进攻仓拼接。对 159552 段整体乘 k = 512100收盘/159552收盘（切换日），
    # 使序列在切换日连续：切换日当天收益 = 512100 自身收益（与 optimize2.py 口径一致，
    # 跨界持有进攻仓时当日实际仍持 512100）；段内相对收益不受影响 ----
    k = float(etf1000['close'].loc[SWITCH_ATK]) / float(etf2000e['close'].loc[SWITCH_ATK])
    atk_c_A = pd.concat([etf1000['close'][etf1000.index < SWITCH_ATK],
                         etf2000e['close'][etf2000e.index >= SWITCH_ATK] * k])
    atk_o_A = pd.concat([etf1000['open'][etf1000.index < SWITCH_ATK],
                         etf2000e['open'][etf2000e.index >= SWITCH_ATK] * k])

    windows = {}
    for label, atk_c, atk_o, div, start in [
            ('A', atk_c_A, atk_o_A, etfdivA, START_A),
            ('B', etf1000['close'], etf1000['open'], etfdivB, START_B)]:
        idx = atk_c.index.intersection(div.index).intersection(avg.index)
        idx = idx[idx >= start]
        windows[label] = {
            'idx': idx,
            'oatk': atk_o.loc[idx], 'catk': atk_c.loc[idx],
            'odiv': div.loc[idx, 'open'], 'cdiv': div.loc[idx, 'close'],
        }
        print('窗口%s: %s ~ %s  共 %d 个交易日' % (label, idx[0].date(), idx[-1].date(), len(idx)))

    combos = gen_combos(avg['close'])
    print('共 %d 个信号组合\n' % len(combos))

    # ---- 基线方案C：DIF(17,34) 零下拐头买 / 任意拐头卖（17 不在 F1 网格内，单独构造）----
    dif_c = ema(avg['close'], 17) - ema(avg['close'], 34)
    d1, d2 = dif_c.shift(1), dif_c.shift(2)
    up_c = ((d1 < d2) & (dif_c > d1)).fillna(False)
    down_c = ((d1 > d2) & (dif_c < d1)).fillna(False)
    base_combo = ('F1拐点', '基线方案C DIF(17,34)拐点 零下买/任意卖',
                  'DIF(17,34) 零下拐头向上买，任意位置拐头向下卖',
                  up_c & (dif_c < 0), down_c)
    baselines = {}
    for label, w in windows.items():
        r = evaluate(base_combo, w['idx'], w['oatk'], w['catk'], w['odiv'], w['cdiv'])
        baselines[label] = r
        print('基线方案C 窗口%s：累计 %s 年化 %s 最大回撤 %s 换仓 %d 次（前半年化 %s / 后半 %s）'
              % (label, fmt(r['cum']), fmt(r['ann']), fmt(r['mdd']), r['n_trades'],
                 fmt(r['ann1']), fmt(r['ann2'])))
    print('（对照：生产 data.json 累计约 553.67%%/换仓131/年化约27.9%%；README 窗口B 口径年化 17.71%%）\n')

    # ---- 全组合评估 ----
    results = {}
    for label, w in windows.items():
        rows = []
        for combo in combos:
            rows.append(evaluate(combo, w['idx'], w['oatk'], w['catk'], w['odiv'], w['cdiv']))
        results[label] = rows

    topA = print_top(results['A'], '窗口A（生产口径 2019-01 起）Top 15 按年化', 15,
                     baselines['A']['ann'])
    print()
    print_top(results['B'], '窗口B（十年口径 2016-11 起，512100+510880）Top 10 按年化', 10,
              baselines['B']['ann'])
    print()

    # ---- 双窗口交叉验证：窗口A Top15 且 窗口B 年化 > 基线 ----
    bmap = {r['name']: r for r in results['B']}
    print('=' * 100)
    print('双窗口交叉验证：窗口A Top15 的窗口B 表现（窗口B 基线方案C 年化 %s）'
          % fmt(baselines['B']['ann']))
    print('=' * 100)
    print('| A排名 | 组合 | A年化 | B年化 | B回撤 | B换仓 | B前/后半年化 | 交集 |')
    print('|---|---|---|---|---|---|---|---|')
    intersection = []
    for rank, ra in enumerate(topA, 1):
        rb = bmap[ra['name']]
        ok = rb['ann'] > baselines['B']['ann']
        if ok:
            intersection.append((rank, ra, rb))
        print('| %d | %s | %s | %s | %s | %d | %s / %s | %s |'
              % (rank, ra['name'], fmt(ra['ann']), fmt(rb['ann']), fmt(rb['mdd']),
                 rb['n_trades'], fmt(rb['ann1']), fmt(rb['ann2']),
                 '✔' if ok else '✘'))
    print()
    if intersection:
        print('交集内组合（窗口A Top15 且 窗口B 年化高于基线）：')
        for rank, ra, rb in intersection:
            print('  [A第%d] %s：A年化 %s（回撤 %s、换仓 %d）/ B年化 %s（回撤 %s、换仓 %d）'
                  % (rank, ra['name'], fmt(ra['ann']), fmt(ra['mdd']), ra['n_trades'],
                     fmt(rb['ann']), fmt(rb['mdd']), rb['n_trades']))
            print('      规则：%s' % ra['desc'])
    else:
        print('交集为空：窗口A Top15 没有任何组合在窗口B 跑赢基线方案C。')
    print()

    # ---- 相对基线的提升 ----
    print('=' * 100)
    print('相对基线方案C 的提升（窗口A 口径）')
    print('=' * 100)
    base_a = baselines['A']
    for rank, r in enumerate(topA[:5], 1):
        print('  A第%d %s：年化 %+0.2fpp（%s → %s），回撤 %s（基线 %s），换仓 %d（基线 %d）'
              % (rank, r['name'], (r['ann'] - base_a['ann']) * 100,
                 fmt(base_a['ann']), fmt(r['ann']), fmt(r['mdd']),
                 fmt(base_a['mdd']), r['n_trades'], base_a['n_trades']))


if __name__ == '__main__':
    main()
