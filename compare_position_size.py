#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
仓位缩放变体回测（半仓中间态）——基线为生产口径方案C

基线口径（与 compare_offense_pool.py / export_json.py / reduce_loss.py 一致）：
- 信号：880003 收盘价 MACD-DIF 拐点，fast=17 slow=34 sig_n=9，sell_anywhere=True
- 进攻腿：2024-06-28 前 = 512100 后复权，之后 = 159552 后复权（拼接）
- 防守腿两套口径都跑：
    A) 512890 固定（= reduce_loss.py / site/data.json 对账口径，累计 +538.68%）
    B) 512890/159201 近20日动量双择优（159201 未上市/无动量时取 512890）
- 成交口径：信号 T 日收盘产生，T+1 日开盘成交；换仓日拆旧仓隔夜段(昨收→今开)
  + 新仓日内段(今开→今收)，扣双边 2*fee（fee=0.0001）
- 回测窗口 2019-01-18 ~ 2026-09-22，信号用全部历史预热

仓位缩放变体（只缩放进攻仓，防守仓始终满仓，不做防守侧缩放）：
- 仓位档位在换仓/调仓执行日（T+1 开盘）确定，只用 T 日及之前的数据
- 仓位变动本身按换仓扣双边费：100→50 卖一半，按 50%*2*fee 计；恢复满仓同理
- S1 波动率缩放：进攻持仓期间，进攻腿近20日年化波动率(日收益std*sqrt(252)) > θ
  则进攻仓降至 50%（其余 50% 放防守腿），回落后恢复满仓；入场日同样按信号日波动定档。
  θ ∈ {25%, 30%, 35%}
- S2 信号强度分档：买入信号日 T，拐头幅度 |ΔDIF_T| 在近60日 |ΔDIF| 中的分位 < 50%
  （弱于中位数）则首笔只进 50%；入场后 5 个交易日内若 DIF 连续两日日上行
  （DIF_t > DIF_{t-1} > DIF_{t-2}）则 T+1 开盘补到 100%，否则维持 50% 直到卖出信号
- S3 趋势过滤半仓：买入信号日 T，进攻腿收盘 < MA20 则只进 50%；之后某日 T' 收盘
  站上 MA20（close >= MA20）则 T'+1 开盘补到 100%

输出：累计/年化/最大回撤/换仓次数/分年度收益；按 2022-01-01 切前后两半分别算
年化和最大回撤做稳健性检验。结果写入 result_position_size.txt。
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
DEF_MOM_N = 20                          # 防守双择优动量窗口（生产口径）
START = '2019-01-18'                    # 512890 上市日
SWITCH_ATK = pd.Timestamp('2024-06-28')  # 进攻腿拼接切换日
SPLIT_DATE = pd.Timestamp('2022-01-01')  # 稳健性前后半分界
HALF_POS = 0.5                           # 半仓档位

_lines = []


def out(s=''):
    print(s)
    _lines.append(s)


def load(name):
    df = pd.read_csv(os.path.join(DATA_DIR, name), parse_dates=['date'])
    df = df.set_index('date').sort_index()
    df.index = df.index.normalize()
    return df


def align(s, idx):
    """对齐到交易日历 idx；上市区间内个别缺口前向填充（当日收益为0），
    上市前/数据截止后保持 NaN（同 compare_offense_pool.align）"""
    r = s.reindex(idx).ffill()
    r[(idx < s.index[0]) | (idx > s.index[-1])] = np.nan
    return r


# ---------------------------------------------------------------------------
# 仓位缩放引擎
# ---------------------------------------------------------------------------
def sizing_backtest(avg, etf_atk, def1, def2, start_date,
                    mode='base', theta=None, label=''):
    """
    mode: 'base'（永远满仓）/ 'S1'（波动率缩放，需 theta）/ 'S2'（信号强度分档）/ 'S3'（趋势过滤）
    def2 为 None 时防守 = 固定 def1。
    返回 dict: nav/trades(执行事件)/n_switch(完整换仓次数)/n_resize(调仓次数)/
               pos_atk(每日收盘进攻仓位)/idx/s2_log
    """
    sig_full = calc_signals(avg['close'], sell_anywhere=True,
                            fast=FAST, slow=SLOW, sig_n=SIG_N)
    # 交易日历：防守主腿 × 信号（def2 不进交集，缺失时回退 def1，同生产口径）
    idx = def1.index.intersection(sig_full.index)
    start_date = pd.Timestamp(start_date)
    idx = idx[idx >= start_date]
    sig = sig_full.loc[idx]

    o_atk, c_atk = align(etf_atk['open'], idx), align(etf_atk['close'], idx)
    o_d1, c_d1 = def1.loc[idx, 'open'], def1.loc[idx, 'close']
    has_d2 = def2 is not None and len(def2) > 0
    o_d2 = align(def2['open'], idx) if has_d2 else None
    c_d2 = align(def2['close'], idx) if has_d2 else None

    # ---- 指标（全历史预计算，rolling 只用当日及之前数据，无前视） ----
    r_atk_full = etf_atk['close'].pct_change()
    vol20 = (r_atk_full.rolling(20).std() * np.sqrt(252)).reindex(idx)
    ma20 = etf_atk['close'].rolling(20).mean().reindex(idx)
    dif = sig_full['dif']
    abs_ddif = dif.diff().abs()
    # |ΔDIF| 在近60日窗口（含当日）中的分位
    ddif_pct = abs_ddif.rolling(60).apply(lambda x: (x <= x[-1]).mean(), raw=True)

    # 防守双择优动量（生产口径：近20日涨幅选高，def2 缺失/无动量时取 def1）
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

    def def_prices(tag):
        return (o_d1, c_d1) if tag == 'd1' else (o_d2, c_d2)

    n = len(idx)
    state_atk = False
    pos = 0.0                     # 当前进攻仓位（收盘状态）
    cur_def = pick_def(idx[0])
    pending = None                # ('enter'/'exit'/'resize', p_new, def_old, def_new)
    trades = []                   # (执行日, 动作, 旧仓位, 新仓位)
    exec_at = {}                  # 执行日 -> (p_old, p_new, def_old, def_new)
    def_hold = []                 # 每日收盘防守标的 tag
    pos_atk = np.zeros(n)
    # S2 状态
    s2_entry_i = -1               # 入场执行日下标
    s2_armed = False              # 是否处于 5 日确认窗口
    s2_log = []                   # 每次弱信号入场：{信号日, 分位, 是否补仓, 补仓日}
    s2_cur = None

    for i in range(n):
        day = idx[i]
        # ---- T+1 开盘执行昨日决定 ----
        if pending is not None:
            kind, p_new, d_old, d_new = pending
            exec_ok = not pd.isna(o_atk.iloc[i])
            if kind == 'enter' or kind == 'exit' or kind == 'resize':
                o_dn, _ = def_prices(d_new)
                o_do, _ = def_prices(d_old)
                exec_ok = exec_ok and not pd.isna(o_dn.iloc[i]) and not pd.isna(o_do.iloc[i])
            if exec_ok:
                if kind == 'enter' and not state_atk:
                    state_atk = True
                    trades.append((day, '买入进攻(%d%%)/卖出防守' % round(p_new * 100), 0.0, p_new))
                    exec_at[day] = (0.0, p_new, d_old, d_new)
                    pos = p_new
                    if mode == 'S2' and p_new < 1.0:
                        s2_entry_i = i
                        s2_armed = True
                elif kind == 'exit' and state_atk:
                    state_atk = False
                    trades.append((day, '卖出进攻(%d%%)/买入防守:%s' % (round(pos * 100), d_new),
                                   pos, 0.0))
                    exec_at[day] = (pos, 0.0, d_old, d_new)
                    pos = 0.0
                    if mode == 'S2' and s2_cur is not None:
                        s2_log.append(s2_cur)
                        s2_cur = None
                    s2_armed = False
                elif kind == 'resize' and state_atk and p_new != pos:
                    trades.append((day, '调仓 %d%%→%d%%' % (round(pos * 100), round(p_new * 100)),
                                   pos, p_new))
                    exec_at[day] = (pos, p_new, d_old, d_new)
                    if mode == 'S2' and p_new == 1.0 and s2_cur is not None:
                        s2_cur['topped'] = True
                        s2_cur['top_day'] = day
                    pos = p_new
                    if mode == 'S2':
                        s2_armed = False
            pending = None
        pos_atk[i] = pos
        def_hold.append(cur_def)

        # ---- 收盘决策（只用当日及之前数据） ----
        if sig['buy_sig'].iloc[i]:
            if not state_atk:
                p0 = 1.0
                if mode == 'S1':
                    v = vol20.iloc[i]
                    if not pd.isna(v) and v > theta:
                        p0 = HALF_POS
                elif mode == 'S2':
                    pct = ddif_pct.loc[day]
                    if not pd.isna(pct) and pct < 0.5:
                        p0 = HALF_POS
                        s2_cur = {'sig_day': day, 'pct': float(pct),
                                  'topped': False, 'top_day': None}
                elif mode == 'S3':
                    m = ma20.iloc[i]
                    if not pd.isna(m) and c_atk.iloc[i] < m:
                        p0 = HALF_POS
                pending = ('enter', p0, cur_def, cur_def)
            # 持有进攻仓期间的买入信号忽略（与基线一致）
        elif sig['sell_sig'].iloc[i]:
            if state_atk:
                new_def = pick_def(day)          # 信号日收盘选防守标的
                pending = ('exit', 0.0, cur_def, new_def)
                cur_def = new_def
            # 防守仓中的卖出信号无效（与基线一致）
        elif state_atk:
            # 持仓期间的仓位调整（无信号日）
            if mode == 'S1':
                v = vol20.iloc[i]
                if not pd.isna(v):
                    tgt = HALF_POS if v > theta else 1.0
                    if tgt != pos:
                        pending = ('resize', tgt, cur_def, cur_def)
            elif mode == 'S2':
                if s2_armed and pos < 1.0:
                    if i - s2_entry_i >= 5:      # 5 个交易日窗口已过，维持 50%
                        s2_armed = False
                    elif i >= 2 and dif.iloc[i] > dif.iloc[i - 1] > dif.iloc[i - 2]:
                        pending = ('resize', 1.0, cur_def, cur_def)  # DIF 连续两日上行，补仓
            elif mode == 'S3':
                m = ma20.iloc[i]
                if pos < 1.0 and not pd.isna(m) and c_atk.iloc[i] >= m:
                    pending = ('resize', 1.0, cur_def, cur_def)      # 站上 MA20，补仓

    # ---- 净值（export_json 真实成交口径推广到分数仓位） ----
    wealth = [1.0]
    for i in range(1, n):
        d = idx[i]
        ev = exec_at.get(d)
        if ev is None:
            p = pos_atk[i]
            _, c_d = def_prices(def_hold[i])
            w = wealth[-1] * (p * float(c_atk.iloc[i]) / float(c_atk.iloc[i - 1])
                              + (1 - p) * float(c_d.iloc[i]) / float(c_d.iloc[i - 1]))
        else:
            p_old, p_new, d_old, d_new = ev
            o_do, c_do = def_prices(d_old)
            o_dn, c_dn = def_prices(d_new)
            overnight = p_old * float(o_atk.iloc[i]) / float(c_atk.iloc[i - 1]) \
                + (1 - p_old) * float(o_do.iloc[i]) / float(c_do.iloc[i - 1])
            intraday = p_new * float(c_atk.iloc[i]) / float(o_atk.iloc[i]) \
                + (1 - p_new) * float(c_dn.iloc[i]) / float(o_dn.iloc[i])
            cost = abs(p_new - p_old) * 2 * FEE
            w = wealth[-1] * overnight * intraday * (1 - cost)
        wealth.append(w)
    nav = pd.Series(wealth, index=idx)
    n_switch = sum(1 for t in trades if not t[1].startswith('调仓'))
    n_resize = sum(1 for t in trades if t[1].startswith('调仓'))
    return {'nav': nav, 'trades': trades, 'idx': idx, 'label': label,
            'n_switch': n_switch, 'n_resize': n_resize,
            'pos_atk': pd.Series(pos_atk, index=idx), 's2_log': s2_log}


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


def half_metrics(nav, lo, hi):
    w = nav[(nav.index >= lo) & (nav.index < hi)]
    if len(w) < 2:
        return None
    w = w / w.iloc[0]
    return annualized(w) * 100, max_drawdown(w) * 100


def run_panel(title, avg, etf_atk, def1, def2):
    """跑一个防守口径下的 基线 + 全部变体，输出汇总/分年度/前后半"""
    out('=' * 96)
    out(title)
    out('=' * 96)

    results = {}
    results['基线(满仓)'] = sizing_backtest(avg, etf_atk, def1, def2, START,
                                           mode='base', label='基线')
    for th in (0.25, 0.30, 0.35):
        results['S1 波动率缩放 θ=%d%%' % (th * 100)] = sizing_backtest(
            avg, etf_atk, def1, def2, START, mode='S1', theta=th, label='S1')
    results['S2 信号强度分档'] = sizing_backtest(avg, etf_atk, def1, def2, START,
                                                mode='S2', label='S2')
    results['S3 趋势过滤半仓'] = sizing_backtest(avg, etf_atk, def1, def2, START,
                                                mode='S3', label='S3')

    idx = results['基线(满仓)']['idx']
    out('回测窗口: %s ~ %s  共 %d 个交易日' % (idx[0].date(), idx[-1].date(), len(idx)))
    out()
    fmt = '%-24s %10s %10s %10s %8s %8s %12s'
    out(fmt % ('方案', '累计收益', '年化收益', '最大回撤', '完整换仓', '调仓', '平均进攻仓位'))
    out('-' * 96)
    for name, res in results.items():
        cum, ann, mdd = metrics_of(res['nav'])
        out(fmt % (name, '%.2f%%' % cum, '%.2f%%' % ann, '%.2f%%' % mdd,
                   res['n_switch'], res['n_resize'],
                   '%.1f%%' % (res['pos_atk'].mean() * 100)))
    out()

    # ---- 分年度收益 ----
    out('分年度收益(%):')
    names = list(results.keys())
    yr_table = {nm: yearly_returns(r['nav']) for nm, r in results.items()}
    years = yr_table[names[0]].index
    out('%-6s' % '年份' + ''.join('%14s' % nm[:13] for nm in names))
    for y in years:
        line = '%-6d' % y
        for nm in names:
            line += '%13.2f%%' % (yr_table[nm].get(y, np.nan) * 100)
        out(line)
    out()

    # ---- 前后半稳健性（2022-01-01 切分） ----
    halves = [('前半 %s~2021-12-31' % idx[0].date(), idx[0], SPLIT_DATE),
              ('后半 2022-01-01~%s' % idx[-1].date(), SPLIT_DATE, idx[-1] + pd.Timedelta(days=1))]
    out('前后半稳健性（按 %s 切分；净值归一化后算年化/回撤）:' % SPLIT_DATE.date())
    out('%-24s | %9s %9s | %9s %9s | 两半均不差于基线?' % ('方案', '前半年化', '前半回撤',
                                                          '后半年化', '后半回撤'))
    out('-' * 96)
    base_h = [half_metrics(results['基线(满仓)']['nav'], lo, hi) for _, lo, hi in halves]
    robust_yes = []
    for name, res in results.items():
        hh = [half_metrics(res['nav'], lo, hi) for _, lo, hi in halves]
        ok = all(hh[k][0] >= base_h[k][0] and hh[k][1] >= base_h[k][1] for k in (0, 1))
        if ok and name != '基线(满仓)':
            robust_yes.append(name)
        mark = '（基线）' if name == '基线(满仓)' else ('是' if ok else '否')
        out('%-24s | %8.2f%% %8.2f%% | %8.2f%% %8.2f%% | %s'
            % (name, hh[0][0], hh[0][1], hh[1][0], hh[1][1], mark))
    out()
    return results, robust_yes


def main():
    avg = load('avg_880003.csv')
    def1 = load('etf_512890_hfq.csv')
    def2 = load('etf_159201_hfq.csv')
    e1000 = load('etf_512100_hfq.csv')
    e552 = load('etf_159552_hfq.csv')
    etf_atk = pd.concat([e1000[e1000.index < SWITCH_ATK],
                         e552[e552.index >= SWITCH_ATK]])
    out('本地数据: 880003 %s ~ %s | 512890 %s ~ %s | 159201 %s ~ %s | 进攻腿拼接 %s ~ %s'
        % (avg.index[0].date(), avg.index[-1].date(),
           def1.index[0].date(), def1.index[-1].date(),
           def2.index[0].date(), def2.index[-1].date(),
           etf_atk.index[0].date(), etf_atk.index[-1].date()))
    out()

    # ---- 基线对账 ----
    res_a = sizing_backtest(avg, etf_atk, def1, None, START, mode='base', label='A')
    cum, ann, mdd = metrics_of(res_a['nav'])
    out('【口径对账A】防守=512890固定: 累计 %.2f%% 年化 %.2f%% 回撤 %.2f%% 换仓 %d'
        '（对账基准 +538.68%% / 27.30%% / -18.06%% / 135 次 → %s）'
        % (cum, ann, mdd, res_a['n_switch'],
           '复现一致' if (abs(cum - 538.68) < 0.01 and res_a['n_switch'] == 135) else '不一致!'))
    res_b = sizing_backtest(avg, etf_atk, def1, def2, START, mode='base', label='B')
    cum, ann, mdd = metrics_of(res_b['nav'])
    out('【口径对账B】防守=512890/159201双择优: 累计 %.2f%% 年化 %.2f%% 回撤 %.2f%% 换仓 %d'
        % (cum, ann, mdd, res_b['n_switch']))
    out('注：任务书所述"生产基线"数值(+538.68%%/27.30%%/-18.06%%/135)实际对应口径A(防守固定512890，'
        '与 site/data.json 对账一致)；口径B(双择优)为 %+.2f%%/%+.2f%%。两套口径均完整对比。'
        % (cum, ann))
    out()

    _, robust_a = run_panel('面板A · 防守腿 = 512890 固定（对账口径）',
                            avg, etf_atk, def1, None)
    res_b_full, robust_b = run_panel('面板B · 防守腿 = 512890/159201 近20日动量双择优（生产口径）',
                                     avg, etf_atk, def1, def2)

    # ---- S2 分档细节 ----
    s2 = res_b_full['S2 信号强度分档']['s2_log']
    if s2:
        out('S2 弱信号入场明细（防守=双择优口径，共 %d 次弱信号首笔50%%）:' % len(s2))
        for r in s2:
            out('  信号日 %s  |ΔDIF|分位 %.2f  %s'
                % (r['sig_day'].date(), r['pct'],
                   ('5日内DIF连续上行→' + r['top_day'].strftime('%Y-%m-%d') + ' 补仓100%')
                   if r['topped'] else '未确认，维持50%至卖出'))
        out()

    # ---- 结论 ----
    out('=' * 96)
    out('结论')
    out('=' * 96)
    out('前后两半（2022-01-01 切分）年化与最大回撤均不差于基线的变体:')
    out('  面板A(防守固定512890): %s' % ('、'.join(robust_a) if robust_a else '无'))
    out('  面板B(防守双择优):     %s' % ('、'.join(robust_b) if robust_b else '无'))
    both = sorted(set(robust_a) & set(robust_b))
    out('  两套防守口径下均通过:  %s' % ('、'.join(both) if both else '无'))
    out()

    path = os.path.join(BASE, 'result_position_size.txt')
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(_lines) + '\n')
    out('结果已写入 %s' % path)


if __name__ == '__main__':
    main()
