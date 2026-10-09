#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
进攻端多标的动量择优回测（与生产基线严格同窗对比）

基线（生产口径）：
- 信号：880003 收盘价 MACD-DIF 拐点，fast=17 slow=34 sig_n=9，sell_anywhere=True
- 进攻腿：2024-06-28 前 = 512100 后复权，之后 = 159552 后复权（拼接）
- 防守腿：512890 / 159201 双择优（切入防守时按近20日涨幅选高者，159201 数据缺失时取 512890）
- 成交口径：信号 T 日收盘产生，T+1 日开盘成交；换仓日拆旧仓隔夜段(昨收→今开)
  + 新仓日内段(今开→今收)，扣双边 2*fee（fee=0.0001）——同 export_json.py / reduce_loss.py

变体：进攻腿改为池内动量择优
- 进攻池 = {512100, 159552, 588220, 515880, 159915}
- 某成员须上市满 N+5 个交易日（按各自数据的逐日可得行数判断，禁止前视）才可被选中
- P1（入场选一次）：买入信号 T 日收盘按合格成员近 N 日动量(close/close.shift(N)-1)
  选最强者，T+1 开盘买入，持有到卖出信号
- P2（周度重排）：进攻持仓期间每周五收盘重排，最强者非当前持有且动量差 > h 时
  T+1 开盘换仓（扣双边费）；h ∈ {0, 0.05}
- N ∈ {10, 20, 60}

输出：累计收益 / 年化 / 最大回撤 / 换仓次数 / 进攻仓占比 / 分年度收益；
并按区间中点切前后两半分别算年化/回撤做稳健性检验。
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

from backtest import calc_signals, annualized, max_drawdown

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, 'data')

FEE = 0.0001
FAST, SLOW, SIG_N = 17, 34, 9
DEF_MOM_N = 20                     # 防守双择优动量窗口（生产口径）
START = '2019-01-18'               # 512890 上市日
SWITCH_ATK = pd.Timestamp('2024-06-28')   # 生产进攻腿拼接切换日

POOL_FILES = {
    '512100': 'etf_512100_hfq.csv',
    '159552': 'etf_159552_hfq.csv',
    '588220': 'etf_588220_hfq.csv',
    '515880': 'etf_515880_hfq.csv',
    '159915': 'etf_159915_hfq.csv',
}


def load(name):
    df = pd.read_csv(os.path.join(DATA_DIR, name), parse_dates=['date'])
    df = df.set_index('date').sort_index()
    df.index = df.index.normalize()
    return df


# ---------------------------------------------------------------------------
# 进攻池动量择优引擎（基线 = 池内仅"拼接腿"一个成员的退化情形，同一引擎同一口径）
# ---------------------------------------------------------------------------
def pool_backtest(avg, pool, def1, def2, start_date, mom_n,
                  mode='P1', hysteresis=0.0, label=''):
    """
    pool: {代码: DataFrame(open/close, 后复权)}，成员可为 None 表示数据缺失
    mode: 'P1'=入场选一次持有到卖出信号; 'P2'=持仓期间每周五收盘重排(滞后阈值 hysteresis)
    返回 dict: nav/trades/hold_atk/hold_label/idx/choice_log
    """
    pool = {c: df for c, df in pool.items() if df is not None and len(df) > 0}
    sig_full = calc_signals(avg['close'], sell_anywhere=True,
                            fast=FAST, slow=SLOW, sig_n=SIG_N)
    # 交易日历：信号 × 防守主腿（def2 缺失时回退 def1，不进交集）
    idx = def1.index.intersection(sig_full.index)
    start_date = pd.Timestamp(start_date)
    idx = idx[idx >= start_date]
    sig = sig_full.loc[idx]

    o_d1, c_d1 = def1.loc[idx, 'open'], def1.loc[idx, 'close']
    has_d2 = def2 is not None and len(def2) > 0

    def align(s):
        """对齐到交易日历 idx；上市区间内的个别缺口（如 159552 缺 2025-04-23，停牌/数据孔洞）
        前向填充（等价于当日无价格变动，收益为0），上市前/数据截止后保持 NaN。"""
        out = s.reindex(idx).ffill()
        out[(idx < s.index[0]) | (idx > s.index[-1])] = np.nan
        return out

    o_d2 = align(def2['open']) if has_d2 else None
    c_d2 = align(def2['close']) if has_d2 else None

    o_atk = {c: align(df['open']) for c, df in pool.items()}
    c_atk = {c: align(df['close']) for c, df in pool.items()}

    # 各成员动量与上市资格（按各自数据日历逐日计算，只用当日及之前数据）
    mom = {}
    elig = {}
    for c, df in pool.items():
        mom[c] = df['close'] / df['close'].shift(mom_n) - 1
        cnt = pd.Series(np.arange(1, len(df) + 1), index=df.index)
        elig[c] = cnt >= (mom_n + 5)

    def pick_atk(day):
        """day 收盘后按近 mom_n 日动量选最强合格成员，返回 (code, mom)"""
        best, best_m = None, -np.inf
        for c in pool:
            if not bool(elig[c].get(day, False)):
                continue
            m = mom[c].get(day, np.nan)
            if pd.isna(m):
                continue
            if m > best_m:
                best, best_m = c, m
        return best, best_m

    # 防守双择优（生产口径：近20日涨幅选高，def2 缺失/无动量时取 def1）
    mom_d1 = def1['close'] / def1['close'].shift(DEF_MOM_N) - 1
    mom_d2 = (def2['close'] / def2['close'].shift(DEF_MOM_N) - 1) if has_d2 else None

    def pick_def(day):
        m1 = mom_d1.get(day, np.nan)
        m2 = mom_d2.get(day, np.nan) if has_d2 else np.nan
        if pd.isna(m2):
            return 'd1'
        if pd.isna(m1):
            return 'd2'
        return 'd1' if m1 >= m2 else 'd2'

    n = len(idx)
    state_atk = False
    cur_def = pick_def(idx[0])
    cur_atk = None
    pending = None                # ('buy', code) / ('sell', dcode) / ('rebal', code)
    trades = []                   # (执行日, 动作描述, 成交价, 卖出侧, 买入侧)
    choice_log = []               # (信号日, 类型, 选中代码, 各成员动量快照)
    hold_atk = np.zeros(n, dtype=bool)
    hold_label = []               # 每日收盘持仓：'d1'/'d2'/池内代码

    for i in range(n):
        if pending is not None:
            kind, tgt = pending
            if kind == 'buy' and not state_atk:
                if not pd.isna(o_atk[tgt].iloc[i]):
                    trades.append((idx[i], '买入进攻:%s/卖出防守:%s' % (tgt, cur_def),
                                   float(o_atk[tgt].iloc[i]), cur_def, tgt))
                    state_atk, cur_atk = True, tgt
            elif kind == 'sell' and state_atk:
                o_new = o_d1 if tgt == 'd1' else o_d2
                if not pd.isna(o_new.iloc[i]):
                    trades.append((idx[i], '卖出进攻:%s/买入防守:%s' % (cur_atk, tgt),
                                   float(o_new.iloc[i]), cur_atk, tgt))
                    state_atk, cur_atk = False, None
                    cur_def = tgt
            elif kind == 'rebal' and state_atk:
                if not pd.isna(o_atk[tgt].iloc[i]):
                    trades.append((idx[i], '进攻换仓:%s→%s' % (cur_atk, tgt),
                                   float(o_atk[tgt].iloc[i]), cur_atk, tgt))
                    cur_atk = tgt
            pending = None
        hold_atk[i] = state_atk
        hold_label.append(cur_atk if state_atk else cur_def)
        # 收盘产生新信号/重排（与 run_backtest 同一优先级：买 > 卖）
        if sig['buy_sig'].iloc[i]:
            c, m = pick_atk(idx[i])
            if c is not None:
                pending = ('buy', c)
                if not state_atk:
                    choice_log.append((idx[i], '入场选',
                                       {k: mom[k].get(idx[i], np.nan) for k in pool}))
        elif sig['sell_sig'].iloc[i]:
            pending = ('sell', pick_def(idx[i]))
        elif mode == 'P2' and state_atk and idx[i].weekday() == 4:
            c, m = pick_atk(idx[i])
            if c is not None and c != cur_atk:
                m_cur = mom[cur_atk].get(idx[i], np.nan)
                if not pd.isna(m_cur) and m - m_cur > hysteresis:
                    pending = ('rebal', c)
                    choice_log.append((idx[i], '周度重排→%s' % c,
                                       {k: mom[k].get(idx[i], np.nan) for k in pool}))

    # ---- 净值（export_json 真实成交口径） ----
    def prices(label):
        if label == 'd1':
            return o_d1, c_d1
        if label == 'd2':
            return o_d2, c_d2
        return o_atk[label], c_atk[label]

    switch_at = {}
    for d, a, p, old, new in trades:
        switch_at[d] = (old, new)
    wealth = [1.0]
    for i in range(1, n):
        d = idx[i]
        sw = switch_at.get(d)
        if sw is None:
            c = prices(hold_label[i])[1]
            w = wealth[-1] * float(c.iloc[i]) / float(c.iloc[i - 1])
        else:
            old, new = sw
            o_old, c_old = prices(old)
            o_new, c_new = prices(new)
            w = (wealth[-1] * float(o_old.iloc[i]) / float(c_old.iloc[i - 1])
                 * float(c_new.iloc[i]) / float(o_new.iloc[i]) * (1 - 2 * FEE))
        wealth.append(w)
    nav = pd.Series(wealth, index=idx)
    return {'nav': nav, 'trades': trades, 'idx': idx,
            'hold_atk': pd.Series(hold_atk, index=idx),
            'hold_label': pd.Series(hold_label, index=idx),
            'choice_log': choice_log, 'label': label}


# ---------------------------------------------------------------------------
# 统计输出
# ---------------------------------------------------------------------------
def metrics_of(nav):
    return ((nav.iloc[-1] - 1) * 100, annualized(nav) * 100, max_drawdown(nav) * 100)


def yearly_returns(nav):
    y = nav.resample('YE').last()
    r = y.pct_change()
    r.iloc[0] = y.iloc[0] / nav.iloc[0] - 1
    r.index = r.index.year
    return r


def half_metrics(nav):
    """按交易日中点切前后两半，返回 [(前半ann, 前半mdd), (后半ann, 后半mdd)]"""
    n = len(nav)
    mid = n // 2
    out = []
    for seg in (nav.iloc[:mid], nav.iloc[mid:]):
        seg = seg / seg.iloc[0]
        out.append((annualized(seg) * 100, max_drawdown(seg) * 100))
    return out


def main():
    avg = load('avg_880003.csv')
    def1 = load('etf_512890_hfq.csv')
    def2 = load('etf_159201_hfq.csv')
    pool = {}
    for code, fname in POOL_FILES.items():
        pool[code] = load(fname)
        print('  %s: %d 行 %s ~ %s' % (code, len(pool[code]),
                                       pool[code].index[0].date(), pool[code].index[-1].date()))

    # 基线进攻腿 = 512100(切换日前) 拼接 159552(切换日起)
    etf_atk = pd.concat([pool['512100'][pool['512100'].index < SWITCH_ATK],
                         pool['159552'][pool['159552'].index >= SWITCH_ATK]])

    print()
    print('=' * 96)
    print('进攻池动量择优 vs 生产基线（%s 起，信号 MACD-DIF %d/%d/%d sell_anywhere，双边万1，'
          '换仓日拆隔夜+日内段）' % (START, FAST, SLOW, SIG_N))
    print('=' * 96)

    results = {}

    # ---- 基线：池内仅拼接腿（同一引擎退化）----
    res = pool_backtest(avg, {'ATK拼接': etf_atk}, def1, def2, START, mom_n=20,
                        mode='P1', label='基线(生产口径)')
    results['基线'] = res

    # ---- 变体 ----
    for n_mom in (10, 20, 60):
        results['P1-N%d' % n_mom] = pool_backtest(
            avg, pool, def1, def2, START, mom_n=n_mom, mode='P1',
            label='P1-N%d' % n_mom)
    for h in (0.0, 0.05):
        for n_mom in (10, 20, 60):
            key = 'P2-N%d-h%.2f' % (n_mom, h)
            results[key] = pool_backtest(
                avg, pool, def1, def2, START, mom_n=n_mom, mode='P2',
                hysteresis=h, label=key)

    # ---- 汇总表 ----
    idx = results['基线']['idx']
    print('回测窗口: %s ~ %s  共 %d 个交易日' % (idx[0].date(), idx[-1].date(), len(idx)))
    print()
    fmt = '%-16s %10s %10s %10s %8s %10s'
    print(fmt % ('方案', '累计收益', '年化收益', '最大回撤', '换仓', '进攻仓占比'))
    print('-' * 96)
    for name, res in results.items():
        cum, ann, mdd = metrics_of(res['nav'])
        print(fmt % (name, '%.2f%%' % cum, '%.2f%%' % ann, '%.2f%%' % mdd,
                     len(res['trades']), '%.1f%%' % (res['hold_atk'].mean() * 100)))
    print()

    # ---- 分年度收益 ----
    print('分年度收益(%):')
    years = yearly_returns(results['基线']['nav']).index
    names = list(results.keys())
    header = '%-6s' % '年份' + ''.join('%14s' % n for n in names)
    print(header)
    yr_table = {n: yearly_returns(r['nav']) for n, r in results.items()}
    for y in years:
        line = '%-6d' % y
        for n in names:
            line += '%13.2f%%' % (yr_table[n].get(y, np.nan) * 100)
        print(line)
    print()

    # ---- 前后半稳健性 ----
    print('前后半稳健性（按交易日中点 %s 切分；净值归一化后算年化/回撤）:'
          % idx[len(idx) // 2].date())
    print('%-16s | %9s %9s | %9s %9s | 两半均优于基线?' % ('方案', '前半年化', '前半回撤', '后半年化', '后半回撤'))
    print('-' * 96)
    base_half = half_metrics(results['基线']['nav'])
    for name, res in results.items():
        hh = half_metrics(res['nav'])
        win = (hh[0][0] > base_half[0][0]) and (hh[1][0] > base_half[1][0])
        print('%-16s | %8.2f%% %8.2f%% | %8.2f%% %8.2f%% | %s'
              % (name, hh[0][0], hh[0][1], hh[1][0], hh[1][1], '是' if win else '否'))
    print()

    # ---- 变体持仓标的分布与换仓明细（仅 P1/P2 池变体）----
    for name, res in results.items():
        if name == '基线':
            continue
        hl = res['hold_label']
        dist = hl[hl.isin(pool.keys())].value_counts()
        total_atk = res['hold_atk'].sum()
        print('[%s] 进攻持仓分布(占全部交易日):' % name
              + '  '.join('%s %.1f%%' % (c, v / len(hl) * 100) for c, v in dist.items()))
    print()

    for name, res in results.items():
        if name == '基线':
            continue
        n_rebal = sum(1 for t in res['trades'] if t[1].startswith('进攻换仓'))
        if n_rebal:
            print('[%s] 进攻池内换仓 %d 次（周度重排触发）:' % (name, n_rebal))
            for d, a, p, old, new in res['trades']:
                if a.startswith('进攻换仓'):
                    print('  %s  %s  成交价 %.3f' % (d.date(), a, p))
            print()


if __name__ == '__main__':
    main()
