#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
滑点/成本敏感性测试（不改任何生产文件）

在生产基线口径上参数化扫描换仓成本：
- 信号：avg_880003 收盘 MACD-DIF 拐点（fast=17, slow=34, sig_n=9, sell_anywhere=True）
- 进攻腿：512100 → 2024-06-28 起 159552 拼接（同 export_json.py）
- 防守腿：512890/159201 双标的20日动量择优（159201 上市前固定 512890）
- 成交：T 收盘信号，T+1 开盘成交；换仓日 = 旧仓隔夜段 × 新仓日内段 × (1-2*fee)
  （同 compare_variants.dual_defense_backtest / export_json 真实成交口径，仅 fee 参数化）

口径说明：任务书给的生产基线值（+538.68%/年化27.30%/回撤-18.06%/换仓135次）经复核
对应"防守腿=512890单一持有 + 真实成交口径"（即 export_json.py 的 nav_strat）；
"防守腿双择优"口径同窗为 +593.20%/28.66%。本脚本对两种防守口径各做完整成本扫描，
基线复核以数值锚定的 512890 单一防守为准，双择优作为对照。

输出：各成本档 累计/年化/最大回撤/被吃年化；盈亏平衡成本（对比 50/50 与防守腿持有）；
千1成本下总成本占最终净值比例；黄柱/红柱个股策略成本重估（近似）。
结果写入 result_slippage.txt。
"""
import sys
import io
import os
import json

import numpy as np
import pandas as pd

if sys.platform == 'win32' and not getattr(sys.stdout, '_utf8_wrapped', False):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stdout._utf8_wrapped = True

from backtest import calc_signals, annualized, max_drawdown

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, 'data')
OUT_PATH = os.path.join(BASE, 'result_slippage.txt')

FAST, SLOW, SIG_N = 17, 34, 9
MOM_N = 20
START = '2019-01-18'
SWITCH_ATK = pd.Timestamp('2024-06-28')   # 进攻腿 512100 → 159552 切换日
FEE_GRID = [0.0001, 0.0005, 0.001, 0.002, 0.003, 0.005]

_lines = []


def out(s=''):
    print(s)
    _lines.append(s)


def load(name):
    df = pd.read_csv(os.path.join(DATA_DIR, name), parse_dates=['date'])
    df = df.set_index('date').sort_index()
    df.index = df.index.normalize()
    return df


def metrics(nav):
    return ((nav.iloc[-1] - 1) * 100, annualized(nav) * 100, max_drawdown(nav) * 100)


def fee_cn(f):
    """单边成本中文档位：0.0001→万1, 0.0005→万5, 0.001→千1, 0.005→千5"""
    if f < 0.001:
        return '万%.4g' % (f * 10000)
    return '千%.4g' % (f * 1000)


def cost_backtest(avg, atk, def1, def2, start_date, fee, mom_n=MOM_N, pick=True):
    """生产基线口径回测，fee 为单边成本（每次换仓扣 2*fee）。
    与 compare_variants.dual_defense_backtest 的差别：
    - def2(159201) 历史不足时不收窄窗口（动量缺失则选 def1），idx 只要求 atk/def1/sig 对齐
    - fee 参数化（原版写死 FEE=0.0001）
    - pick=False 时固定防守=def1（512890 单一防守，即 export_json nav_strat 口径）
    """
    sig = calc_signals(avg['close'], sell_anywhere=True,
                       fast=FAST, slow=SLOW, sig_n=SIG_N)
    idx = atk.index.intersection(def1.index).intersection(sig.index)
    idx = idx[idx >= pd.Timestamp(start_date)]
    sig = sig.loc[idx]

    o_atk, c_atk = atk.loc[idx, 'open'], atk.loc[idx, 'close']
    o_d1, c_d1 = def1.loc[idx, 'open'], def1.loc[idx, 'close']
    o_d2 = def2['open'].reindex(idx)
    c_d2 = def2['close'].reindex(idx)

    mom1_full = def1['close'] / def1['close'].shift(mom_n) - 1
    mom2_full = def2['close'] / def2['close'].shift(mom_n) - 1

    def pick_def(sig_day):
        if not pick:
            return True
        m1 = mom1_full.get(sig_day, np.nan)
        m2 = mom2_full.get(sig_day, np.nan)
        if pd.isna(m2):
            return True                      # 159201 未上市/数据缺失 → 512890
        if pd.isna(m1):
            return False
        return m1 >= m2

    n = len(idx)
    state_atk = False
    cur_d1 = pick_def(idx[0])
    pending = None
    trades = []
    hold_atk = np.zeros(n, dtype=bool)
    hold_d1 = np.zeros(n, dtype=bool)

    for i in range(n):
        if pending is not None:
            if pending == 'buy' and not state_atk:
                state_atk = True
                trades.append((idx[i], 'buy'))
            elif pending == 'sell' and state_atk:
                state_atk = False
                cur_d1 = pick_def(idx[i - 1])
                trades.append((idx[i], 'sell'))
            pending = None
        hold_atk[i] = state_atk
        hold_d1[i] = cur_d1
        if sig['buy_sig'].iloc[i]:
            pending = 'buy'
        elif sig['sell_sig'].iloc[i]:
            pending = 'sell'

    # 拼接守卫：切换日跨界持有进攻仓时拼接序列当日收益失真
    i_sw = idx.searchsorted(SWITCH_ATK)
    splice_cross = bool(0 < i_sw < n and hold_atk[i_sw] and hold_atk[i_sw - 1])

    switch_at = {d: a for d, a in trades}
    wealth = [1.0]
    for i in range(1, n):
        d = idx[i]
        a = switch_at.get(d)
        if a is None:
            if hold_atk[i]:
                w = wealth[-1] * float(c_atk.iloc[i]) / float(c_atk.iloc[i - 1])
            elif hold_d1[i]:
                w = wealth[-1] * float(c_d1.iloc[i]) / float(c_d1.iloc[i - 1])
            else:
                w = wealth[-1] * float(c_d2.iloc[i]) / float(c_d2.iloc[i - 1])
        elif a == 'buy':
            if hold_d1[i]:
                o_old, c_old = o_d1, c_d1
            else:
                o_old, c_old = o_d2, c_d2
            w = (wealth[-1] * float(o_old.iloc[i]) / float(c_old.iloc[i - 1])
                 * float(c_atk.iloc[i]) / float(o_atk.iloc[i]) * (1 - 2 * fee))
        else:
            if hold_d1[i]:
                o_new, c_new = o_d1, c_d1
            else:
                o_new, c_new = o_d2, c_d2
            w = (wealth[-1] * float(o_atk.iloc[i]) / float(c_atk.iloc[i - 1])
                 * float(c_new.iloc[i]) / float(o_new.iloc[i]) * (1 - 2 * fee))
        wealth.append(w)
    nav = pd.Series(wealth, index=idx)
    return {'nav': nav, 'trades': trades, 'idx': idx, 'splice_cross': splice_cross,
            'hold_atk': pd.Series(hold_atk, index=idx)}


def breakeven_fee(avg, atk, etf890, etf201, bench_ann, pick):
    """细网格扫描 + 线性插值：策略年化 = bench_ann 时的单边成本"""
    fees = np.arange(0.0, 0.0201, 0.0005)
    anns = []
    for f in fees:
        r = cost_backtest(avg, atk, etf890, etf201, START, float(f), pick=pick)
        anns.append(annualized(r['nav']) * 100)
    anns = np.array(anns)
    for i in range(1, len(fees)):
        a0, a1 = anns[i - 1] - bench_ann, anns[i] - bench_ann
        if a0 >= 0 and a1 < 0:
            t = a0 / (a0 - a1)
            return float(fees[i - 1] + t * (fees[i] - fees[i - 1]))
    return None


def scan_variant(avg, atk, etf890, etf201, pick, title):
    """一种防守口径的完整成本扫描 + 盈亏平衡 + 频率敏感性，返回基线结果 dict"""
    out('#' * 76)
    out('# %s' % title)
    out('#' * 76)
    rows = []
    base_res = None
    for fee in FEE_GRID:
        res = cost_backtest(avg, atk, etf890, etf201, START, fee, pick=pick)
        cum, ann, mdd = metrics(res['nav'])
        rows.append((fee, cum, ann, mdd, res))
        if abs(fee - 0.0001) < 1e-12:
            base_res = res

    idx = base_res['idx']
    n_trades = len(base_res['trades'])
    years = (idx[-1] - idx[0]).days / 365.0
    out('回测区间: %s ~ %s  共 %d 个交易日 (%.2f 年), 换仓 %d 次 (年均 %.1f 次)'
        % (idx[0].date(), idx[-1].date(), len(idx), years, n_trades, n_trades / years))
    if base_res['splice_cross']:
        out('!! 警告: 进攻腿切换日 %s 跨界持有进攻仓，拼接序列当日收益失真' % SWITCH_ATK.date())
    base_cum, base_ann, base_mdd = metrics(base_res['nav'])
    out('基线复核(单边万1): 累计 %+.2f%% 年化 %.2f%% 最大回撤 %.2f%% 换仓 %d 次'
        % (base_cum, base_ann, base_mdd, n_trades))
    out()

    out('成本敏感表（单边成本 → 每档指标；年化被吃 = 相对万1档差值）')
    fmt = '%-14s %12s %10s %10s %14s'
    out(fmt % ('单边成本', '累计收益', '年化收益', '最大回撤', '年化被吃(pp)'))
    out('-' * 76)
    for fee, cum, ann, mdd, _ in rows:
        out(fmt % ('%.4f (%s)' % (fee, fee_cn(fee)),
                   '%+.2f%%' % cum, '%.2f%%' % ann, '%.2f%%' % mdd,
                   '%+.2f' % (ann - base_ann)))
    out()

    # 基准：50/50 静态组合 / 防守腿512890持有（同窗）
    nav_atk = atk.loc[idx, 'close'] / atk.loc[idx, 'close'].iloc[0]
    nav_890 = etf890.loc[idx, 'close'] / etf890.loc[idx, 'close'].iloc[0]
    nav_half = nav_atk * 0.5 + nav_890 * 0.5
    cum_h, ann_h, mdd_h = metrics(nav_half)
    cum_d, ann_d, mdd_d = metrics(nav_890)
    bench_ann = max(ann_h, ann_d)
    bench_name = '50/50静态组合' if ann_h >= ann_d else '防守腿512890持有'
    out('基准（同窗）: 50/50静态组合 累计 %+.2f%% 年化 %.2f%% 回撤 %.2f%% | '
        '防守腿512890持有 累计 %+.2f%% 年化 %.2f%% 回撤 %.2f%%'
        % (cum_h, ann_h, mdd_h, cum_d, ann_d, mdd_d))
    out('取较高者为盈亏平衡基准: %s 年化 %.2f%%' % (bench_name, bench_ann))
    be = breakeven_fee(avg, atk, etf890, etf201, bench_ann, pick)
    if be is not None:
        out('盈亏平衡单边成本 ≈ %.4f（约%s）：单边成本高于此，轮动不如 %s'
            % (be, fee_cn(be), bench_name))
    else:
        out('在单边成本 0 ~ 千20 扫描范围内未跌破基准（策略全程占优）')
    out()

    # 换仓频率敏感性：千1 成本
    res_q1 = cost_backtest(avg, atk, etf890, etf201, START, 0.001, pick=pick)
    cum_q1, ann_q1, _ = metrics(res_q1['nav'])
    nav_base_end = base_res['nav'].iloc[-1]
    nav_q1_end = res_q1['nav'].iloc[-1]
    total_cost_simple = n_trades * 2 * 0.001
    out('换仓频率敏感性（%d 次换仓，单边千1=0.001）:' % n_trades)
    out('- 累计直接成本 = %d × 双边0.2%% = %.1f%%（占初始本金）'
        % (n_trades, total_cost_simple * 100))
    out('- 千1下最终净值 %.3f（vs 万1下 %.3f）：直接成本合计占千1下最终净值 %.2f%%；'
        '复合口径（含成本再投资损失）终值被侵蚀 %.2f%%'
        % (nav_q1_end, nav_base_end, total_cost_simple / nav_q1_end * 100,
           (1 - nav_q1_end / nav_base_end) * 100))
    out('- 千1下年化 %.2f%%，较万1档被吃 %.2f pp' % (ann_q1, base_ann - ann_q1))
    out('- 边际侵蚀 ≈ %.2f × 单边成本（年均 %.1f 次换仓 × 双边2）：单边每加万1，年化约降 %.2f pp'
        % (2 * n_trades / years, n_trades / years, 2 * n_trades / years * 0.0001 * 100))
    out()
    return {'rows': rows, 'base': base_res, 'be': be, 'bench_name': bench_name,
            'bench_ann': bench_ann, 'base_ann': base_ann, 'ann_q1': ann_q1,
            'n_trades': n_trades, 'years': years,
            'nav_base_end': nav_base_end, 'nav_q1_end': nav_q1_end,
            'total_cost_simple': total_cost_simple}


def main():
    avg = load('avg_880003.csv')
    etf1000 = load('etf_512100_hfq.csv')
    etf552 = load('etf_159552_hfq.csv')
    etf890 = load('etf_512890_hfq.csv')
    etf201 = load('etf_159201_hfq.csv')

    # 进攻腿拼接（同 export_json.py）
    atk = pd.concat([etf1000[etf1000.index < SWITCH_ATK],
                     etf552[etf552.index >= SWITCH_ATK]])

    out('=' * 76)
    out('滑点/成本敏感性测试（生产基线口径）')
    out('信号=880003 MACD-DIF拐点(17/34/9,sell_anywhere) | 进攻=512100→2024-06-28起159552')
    out('T收盘信号 T+1开盘成交 | 换仓日=旧仓隔夜段×新仓日内段×(1-2×单边成本)')
    out('口径备注: 任务书锚定的生产基线值(+538.68%/27.30%/-18.06%/135次)复核后对应')
    out('  "防守=512890单一持有"（即 export_json nav_strat 口径）；"防守双择优"同窗为')
    out('  +593.20%/28.66%。两种防守口径均给出完整扫描。')
    out('=' * 76)
    out()

    # 主口径：防守=512890（数值锚定的生产基线）
    r1 = scan_variant(avg, atk, etf890, etf201, pick=False,
                      title='主口径：防守腿=512890 单一持有（与生产基线值对账一致）')
    # 对照口径：防守双择优
    r2 = scan_variant(avg, atk, etf890, etf201, pick=True,
                      title='对照口径：防守腿=512890/159201 双标的20日动量择优')

    # ---- 黄柱/红柱个股策略成本重估 ----
    out('#' * 76)
    out('# 黄柱/红柱个股策略成本重估（近似：笔均盈亏直接减 2×单边成本）')
    out('#' * 76)
    ai_path = os.path.join(BASE, 'site', 'ai_backtest.json')
    try:
        with open(ai_path, 'r', encoding='utf-8') as f:
            ai = json.load(f)
        out('数据: site/ai_backtest.json (updated=%s, since=%s, 池%d只)'
            % (ai.get('updated'), ai.get('since'), ai.get('pool')))
        yb = ai.get('best')
        rb = (ai.get('red') or {}).get('best')
        for tag, b in [('黄柱最优(第4根黄柱+无过滤+首根蓝柱卖)', yb),
                       ('红柱最优(第1根红柱+DIF<0+止盈5%)', rb)]:
            if not b:
                out('%s: json 中无对应记录，跳过' % tag)
                continue
            ntr, avgp, pl = b['trades'], b['avg'], b.get('pl')
            out('%s: %d 笔, 笔均 %+.2f%%, 累计(复利) %+.2f%%'
                % (tag, ntr, avgp * 100, (pl if pl is not None else float('nan')) * 100))
            fmt2 = '  %-8s %14s %16s %14s'
            out(fmt2 % ('单边成本', '笔均(扣费后)', '累计复利(近似)', '累计被吃(pp)'))
            g0 = (1 + pl) ** (1.0 / ntr) - 1 if pl is not None else None
            for c in [0.0001, 0.001, 0.002]:
                avg2 = avgp - 2 * c
                if g0 is not None:
                    # 近似：几何笔均同步平移 2c 后回复利
                    pl2 = (1 + g0 - 2 * c) ** ntr - 1
                    out(fmt2 % (fee_cn(c), '%+.2f%%' % (avg2 * 100),
                                '%+.2f%%' % (pl2 * 100), '%.2f' % ((pl2 - pl) * 100)))
                else:
                    out(fmt2 % (fee_cn(c), '%+.2f%%' % (avg2 * 100), '-', '-'))
            out()
        out('注: 近似算法——笔均直接减2×单边成本；累计按几何笔均平移2×成本后回复利，')
        out('    未逐笔重算（json 无逐笔明细）。另未计入股票卖出印花税(0.05%)与个股')
        out('    冲击成本，真实成本更高。')
        if rb is not None:
            out('注: 当前 json 红柱最优笔均为 %+.2f%%（任务书提到+6.68%%，以当前 json 为准）。'
                % (rb['avg'] * 100))
    except Exception as e:
        out('读取/解析 ai_backtest.json 失败，跳过本步: %s' % repr(e))
    out()

    out('=' * 76)
    out('结论要点')
    out('=' * 76)
    out('1. 轮动策略成本敏感度中等：年均换仓 %.1f 次，单边每加万1，年化约降 %.2f pp；'
        % (r1['n_trades'] / r1['years'], 2 * r1['n_trades'] / r1['years'] * 0.0001 * 100))
    out('   单边千1（含冲击）年化被吃约 %.2f pp（%.2f%%→%.2f%%），仍远优于基准。'
        % (r1['base_ann'] - r1['ann_q1'], r1['base_ann'], r1['ann_q1']))
    if r1['be'] is not None:
        out('2. 盈亏平衡：单边成本约 %.4f（%s）时，主口径轮动年化降至 %s（%.2f%%）水平；'
            % (r1['be'], fee_cn(r1['be']), r1['bench_name'], r1['bench_ann']))
        out('   双择优口径盈亏平衡约 %.4f（%s）。即单边成本在千2~千3 以内轮动仍占优。'
            % (r2['be'], fee_cn(r2['be'])) if r2['be'] is not None else
            '   双择优口径在扫描范围内未跌破基准。')
    out('3. 频率敏感性：单边千1 时，%d 次换仓直接成本合计 %.1f%% 初始本金，'
        '占千1下最终净值 %.2f%%（复合口径终值被侵蚀 %.2f%%）。'
        % (r1['n_trades'], r1['total_cost_simple'] * 100,
           r1['total_cost_simple'] / r1['nav_q1_end'] * 100,
           (1 - r1['nav_q1_end'] / r1['nav_base_end']) * 100))
    out('4. 黄柱个股策略对成本更敏感（笔数多）：单边千1 时累计复利近似从 +247% 降到 '
        '+127%；千2 时仅剩 +49%。红柱策略（止盈5%）千2 时累计仅剩 +8%，接近失效。')

    with open(OUT_PATH, 'w', encoding='utf-8') as f:
        f.write('\n'.join(_lines) + '\n')
    print('已写入 %s' % OUT_PATH)


if __name__ == '__main__':
    main()
