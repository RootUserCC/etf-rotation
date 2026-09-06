#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
亏损结构分解 + 减亏变体回测（基线 = 生产口径方案C：MACD-DIF 17/34/9 拐点，sell_anywhere=True）

口径与 export_json.py 完全一致：
- 信号 T 日收盘产生，T+1 日开盘价成交，双边费用各万1
- 账户净值（真实成交口径）：普通日持仓 close→close；换仓日拆成
  旧仓隔夜段(昨收→今开) + 新仓日内段(今开→今收)，扣双边万1
- 持仓段（leg）：开盘价建仓 → 开盘价了结，扣双边万1
- 进攻仓拼接：2024-06-28 前 = 512100 后复权，之后 = 159552 后复权；防守仓 = 512890 后复权
- 回测窗口 = 512890 上市起（2019-01-18），信号用全部历史预热

第一部分：基线亏损结构分解（段统计 + 亏损段按持有交易日分桶）
第二部分：减亏变体 V1~V5 逐一回测 + 组合，及 2022-01-01 前后半区间稳健性
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

from backtest import calc_signals, run_backtest, max_drawdown, annualized

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, 'data')
FEE = 0.0001                       # 单边万1
SWITCH_ATK = pd.Timestamp('2024-06-28')
SPLIT_DATE = pd.Timestamp('2022-01-01')   # 稳健性前后半分界


def load(name):
    df = pd.read_csv(os.path.join(DATA_DIR, name), parse_dates=['date'])
    df = df.set_index('date').sort_index()
    df.index = df.index.normalize()
    return df


# ---------------------------------------------------------------------------
# 自定义回测循环（支持冷却期/止损/止盈；不带这些参数时必须复现基线）
# ---------------------------------------------------------------------------
def run_custom(idx, sig, oatk, catk, odiv, cdiv,
               cooldown=0, stop_x=None, trail_y=None):
    """
    idx: 交易日索引；sig: 含 buy_sig/sell_sig 列（已 loc[idx]）
    oatk/catk/odiv/cdiv: 进攻/防守仓开收盘价（与 idx 对齐）
    cooldown: 卖出进攻仓成交日（含）起 K 个交易日内的买入信号被忽略
    stop_x:   固定止损，收盘价 < 买入开盘价*(1-X) 则次日开盘切防守仓
    trail_y:  移动止盈，收盘价 < 买入后最高收盘价*(1-Y) 则次日开盘切防守仓
    返回 trades: [(i, 'buy'/'sell', 成交价=新仓开盘价)]
    """
    n = len(idx)
    state = 'div'                  # 'div'=防守仓, 'atk'=进攻仓
    pending = None
    trades = []
    entry_price = None             # 进攻仓买入开盘价
    max_close = None               # 买入后最高收盘价
    cooldown_until = -1            # 买入信号日被屏蔽的最后下标

    for i in range(n):
        # T+1 开盘执行昨日信号
        if pending == 'buy' and state == 'div':
            state = 'atk'
            p = float(oatk.iloc[i])
            trades.append((i, 'buy', p))
            entry_price = p
            max_close = float(catk.iloc[i])
        elif pending == 'sell' and state == 'atk':
            state = 'div'
            trades.append((i, 'sell', float(odiv.iloc[i])))
            if cooldown > 0:
                cooldown_until = i + cooldown - 1
            entry_price = None
            max_close = None
        pending = None

        # 收盘产生新信号
        if state == 'atk':
            c = float(catk.iloc[i])
            max_close = max(max_close, c)
            stopped = False
            if stop_x is not None and c < entry_price * (1 - stop_x):
                stopped = True
            if trail_y is not None and c < max_close * (1 - trail_y):
                stopped = True
            if stopped or sig['sell_sig'].iloc[i]:
                pending = 'sell'
            # 持有进攻仓期间的买入信号忽略（与基线一致）
        else:
            if sig['buy_sig'].iloc[i] and i > cooldown_until:
                pending = 'buy'
            elif sig['sell_sig'].iloc[i]:
                pending = 'sell'   # 防守仓中的卖出信号为无效挂单，次日不执行
    return trades


def build_wealth(idx, trades, oatk, catk, odiv, cdiv):
    """export_json.py 真实成交口径：普通日 close→close；换仓日拆隔夜段+日内段，扣双边万1"""
    switch_at = {i: a for i, a, p in trades}
    wealth = [1.0]
    held_div = True
    for i in range(1, len(idx)):
        a = switch_at.get(i)
        if a is None:
            c = cdiv if held_div else catk
            w = wealth[-1] * float(c.iloc[i]) / float(c.iloc[i - 1])
        elif a == 'buy':
            w = (wealth[-1] * float(odiv.iloc[i]) / float(cdiv.iloc[i - 1])
                 * float(catk.iloc[i]) / float(oatk.iloc[i]) * (1 - 2 * FEE))
            held_div = False
        else:
            w = (wealth[-1] * float(oatk.iloc[i]) / float(catk.iloc[i - 1])
                 * float(cdiv.iloc[i]) / float(odiv.iloc[i]) * (1 - 2 * FEE))
            held_div = True
        wealth.append(w)
    return pd.Series(wealth, index=idx)


def build_legs(idx, trades, oatk, catk, odiv, cdiv):
    """export_json.py 段口径：开盘价建仓→开盘价了结，扣双边万1；首段视作首日收盘建仓防守仓"""
    legs = []
    leg_asset = 'div'
    leg_entry = float(cdiv.iloc[0])
    prev_i = 0
    for i, a, p in trades:
        closed_open = float(odiv.iloc[i]) if leg_asset == 'div' else float(oatk.iloc[i])
        ret = closed_open / leg_entry - 1 - 2 * FEE
        legs.append({
            'asset': leg_asset, 'buy_date': idx[prev_i], 'sell_date': idx[i],
            'days': i - prev_i + 1, 'ret': ret, 'open': False,
        })
        leg_asset = 'atk' if a == 'buy' else 'div'
        leg_entry = p
        prev_i = i
    last_close = float(cdiv.iloc[-1]) if leg_asset == 'div' else float(catk.iloc[-1])
    legs.append({
        'asset': leg_asset, 'buy_date': idx[prev_i], 'sell_date': None,
        'days': len(idx) - prev_i, 'ret': last_close / leg_entry - 1 - 2 * FEE, 'open': True,
    })
    return legs


# ---------------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------------
def leg_stats(legs, asset='atk'):
    """进攻仓（或防守仓）已平仓段统计"""
    ls = [l for l in legs if l['asset'] == asset and not l['open']]
    wins = [l for l in ls if l['ret'] > 0]
    losses = [l for l in ls if l['ret'] <= 0]
    avg_win = float(np.mean([l['ret'] for l in wins])) if wins else 0.0
    avg_loss = float(np.mean([l['ret'] for l in losses])) if losses else 0.0
    sum_win = sum(l['ret'] for l in wins)
    sum_loss = sum(l['ret'] for l in losses)
    return {
        'n': len(ls), 'n_win': len(wins), 'n_loss': len(losses),
        'win_rate': len(wins) / len(ls) if ls else float('nan'),
        'avg_win': avg_win, 'avg_loss': avg_loss,
        'pl_ratio': avg_win / abs(avg_loss) if avg_loss < 0 else float('nan'),
        'pf': sum_win / abs(sum_loss) if sum_loss < 0 else float('inf'),
        'sum_win': sum_win, 'sum_loss': sum_loss,
    }


def summary_row(name, wealth, trades, legs):
    st = leg_stats(legs, 'atk')
    return {
        'name': name,
        'cum': float(wealth.iloc[-1] - 1),
        'ann': annualized(wealth),
        'mdd': max_drawdown(wealth),
        'n_trades': len(trades),
        'win_rate': st['win_rate'],
        'avg_loss': st['avg_loss'],
        'pf': st['pf'],
        '_wealth': wealth, '_legs': legs,
    }


def fmt_pct(x, na='—'):
    return na if x is None or (isinstance(x, float) and np.isnan(x)) else '%.2f%%' % (x * 100)


def print_compare_table(rows):
    print('| 方案 | 累计收益 | 年化 | 最大回撤 | 换仓次数 | 段胜率 | 平均亏损段 | 利润因子 |')
    print('|---|---|---|---|---|---|---|---|')
    for r in rows:
        pf = '∞' if r['pf'] == float('inf') else '%.2f' % r['pf']
        print('| %s | %s | %s | %s | %d | %s | %s | %s |'
              % (r['name'], fmt_pct(r['cum']), fmt_pct(r['ann']), fmt_pct(r['mdd']),
                 r['n_trades'], fmt_pct(r['win_rate']), fmt_pct(r['avg_loss']), pf))


def half_metrics(wealth, legs, lo, hi):
    """[lo, hi) 区间：年化 / 最大回撤 / 进攻仓段胜率（段按建仓日归属，净值归一化）"""
    w = wealth[(wealth.index >= lo) & (wealth.index < hi)]
    if len(w) < 2:
        return None
    w = w / w.iloc[0]
    ls = [l for l in legs if l['asset'] == 'atk' and not l['open']
          and lo <= l['buy_date'] < hi]
    wr = np.mean([l['ret'] > 0 for l in ls]) if ls else float('nan')
    return {'ann': annualized(w), 'mdd': max_drawdown(w),
            'win_rate': float(wr), 'n_legs': len(ls)}


# ---------------------------------------------------------------------------
# 信号变体
# ---------------------------------------------------------------------------
def make_sig_frame(buy_sig, sell_sig):
    return pd.DataFrame({'buy_sig': buy_sig, 'sell_sig': sell_sig})


def sig_v1_confirm(sig_full):
    """V1 买入确认：拐头次日 DIF 仍上行才确认，信号日顺延1天（T拐头→T+1确认→T+2开盘买入）"""
    dif = sig_full['dif']
    buy2 = sig_full['buy_sig'].shift(1).fillna(False) & (dif.diff() > 0)
    return make_sig_frame(buy2, sig_full['sell_sig'])


def sig_v5_threshold(sig_full, theta):
    """V5 拐头幅度门槛：买入要求拐头当日 ΔDIF ≥ θ"""
    d = sig_full['dif'].diff()
    buy5 = sig_full['buy_sig'] & (d >= theta)
    return make_sig_frame(buy5, sig_full['sell_sig'])


def main():
    avg = load('avg_880003.csv')
    etf1000 = load('etf_512100_hfq.csv')
    etfdiv = load('etf_512890_hfq.csv')
    etf2000e = load('etf_159552_hfq.csv')
    etf_atk = pd.concat([etf1000[etf1000.index < SWITCH_ATK],
                         etf2000e[etf2000e.index >= SWITCH_ATK]])

    # 全历史信号（预热），回测窗口 = 三只标的交易日交集（512890 上市起 2019-01-18）
    sig_full = calc_signals(avg['close'], sell_anywhere=True, fast=17, slow=34, sig_n=9)
    idx = etf_atk.index.intersection(etfdiv.index).intersection(sig_full.index)
    sig_base = sig_full.loc[idx]
    oatk, catk = etf_atk.loc[idx, 'open'], etf_atk.loc[idx, 'close']
    odiv, cdiv = etfdiv.loc[idx, 'open'], etfdiv.loc[idx, 'close']

    # ---- 校验：自定义循环复现基线，与 run_backtest / export_json 口径对账 ----
    res = run_backtest(avg, etf_atk, etfdiv, fee=FEE, sell_anywhere=True,
                       fast=17, slow=34, sig_n=9, label='C', verbose=False)
    rb_trades = [(list(idx).index(d), 'buy' if a.startswith('买入') else 'sell', p)
                 for d, a, p in res['trades']]
    my_trades = run_custom(idx, sig_base, oatk, catk, odiv, cdiv)
    same = [(i, a) for i, a, p in rb_trades] == [(i, a) for i, a, p in my_trades]
    print('【口径校验】自定义循环 vs run_backtest 换仓点一致: %s （各 %d 次）'
          % (same, len(my_trades)))
    if not same:
        print('  run_backtest:', [(str(idx[i].date()), a) for i, a, p in rb_trades])
        print('  run_custom  :', [(str(idx[i].date()), a) for i, a, p in my_trades])
        raise SystemExit('基线复现失败，中止')

    wealth_base = build_wealth(idx, my_trades, oatk, catk, odiv, cdiv)
    legs_base = build_legs(idx, my_trades, oatk, catk, odiv, cdiv)

    # 与网站 data.json（生产导出）对账
    json_path = os.path.join(BASE, 'site', 'data.json')
    if os.path.exists(json_path):
        with open(json_path, encoding='utf-8') as f:
            site = json.load(f)
        site_cum = site['nav_strat'][-1] - 1
        site_legs = [l for l in site['legs'] if l['asset'] == '1000' and not l['open']]
        site_wr = np.mean([l['ret'] > 0 for l in site_legs]) if site_legs else float('nan')
        my_st = leg_stats(legs_base, 'atk')
        print('【口径校验】vs site/data.json：累计收益 本脚本 %.2f%% / 网站 %.2f%%；'
              '进攻段胜率 本脚本 %.1f%%(%d段) / 网站 %.1f%%(%d段)'
              % ((wealth_base.iloc[-1] - 1) * 100, site_cum * 100,
                 my_st['win_rate'] * 100, my_st['n'], site_wr * 100, len(site_legs)))

    print()
    print('=' * 72)
    print('第一部分 · 基线亏损结构分解（方案C：MACD-DIF 17/34/9，%s ~ %s）'
          % (idx[0].date(), idx[-1].date()))
    print('=' * 72)
    row_base = summary_row('基线(方案C)', wealth_base, my_trades, legs_base)
    print('累计收益 %s，年化 %s，最大回撤 %s，换仓 %d 次'
          % (fmt_pct(row_base['cum']), fmt_pct(row_base['ann']),
             fmt_pct(row_base['mdd']), row_base['n_trades']))
    print()

    st_atk = leg_stats(legs_base, 'atk')
    st_div = leg_stats(legs_base, 'div')
    open_leg = legs_base[-1] if legs_base[-1]['open'] else None
    print('【进攻仓段】（一次买入→卖出为一段，仅已平仓段；段收益=开盘价口径含双边万1）')
    print('  段数 %d：盈利 %d 段 / 亏损 %d 段，胜率 %s'
          % (st_atk['n'], st_atk['n_win'], st_atk['n_loss'], fmt_pct(st_atk['win_rate'])))
    print('  平均盈利段 %s，平均亏损段 %s，盈亏比 %.2f，利润因子 %.2f'
          % (fmt_pct(st_atk['avg_win']), fmt_pct(st_atk['avg_loss']),
             st_atk['pl_ratio'], st_atk['pf']))
    print('  盈利段合计 %s，亏损段合计 %s（简单加总）'
          % (fmt_pct(st_atk['sum_win']), fmt_pct(st_atk['sum_loss'])))
    print('【防守仓段】段数 %d：盈利 %d / 亏损 %d，亏损段合计 %s'
          % (st_div['n'], st_div['n_win'], st_div['n_loss'], fmt_pct(st_div['sum_loss'])))
    if open_leg:
        print('【当前持有中】%s 仓，自 %s 起 %d 个交易日，浮动盈亏 %s（未计入上面段统计）'
              % ('进攻' if open_leg['asset'] == 'atk' else '防守',
                 open_leg['buy_date'].date(), open_leg['days'], fmt_pct(open_leg['ret'])))
    print()

    # 亏损段按持有交易日分桶
    print('【亏损进攻段按持有交易日分桶】（金额=段收益简单加总）')
    loss_legs = [l for l in legs_base if l['asset'] == 'atk' and not l['open'] and l['ret'] <= 0]
    total_loss = sum(l['ret'] for l in loss_legs)
    print('| 持有交易日 | 次数 | 亏损合计 | 占总亏损 |')
    print('|---|---|---|---|')
    for label, pred in [('≤3天', lambda d: d <= 3),
                        ('4-10天', lambda d: 4 <= d <= 10),
                        ('>10天', lambda d: d > 10)]:
        sub = [l for l in loss_legs if pred(l['days'])]
        s = sum(l['ret'] for l in sub)
        share = s / total_loss if total_loss < 0 else float('nan')
        print('| %s | %d | %s | %s |' % (label, len(sub), fmt_pct(s), fmt_pct(share)))
    print('| 合计 | %d | %s | 100%% |' % (len(loss_legs), fmt_pct(total_loss)))
    print()
    print('亏损段明细（按亏损额升序）:')
    for l in sorted(loss_legs, key=lambda x: x['ret'])[:20]:
        print('  %s → %s  持有 %2d 天  段收益 %s'
              % (l['buy_date'].date(), l['sell_date'].date(), l['days'], fmt_pct(l['ret'])))
    print()

    # ------------------------------------------------------------------
    print('=' * 72)
    print('第二部分 · 减亏变体回测')
    print('=' * 72)

    # V5 门槛：该指数全历史 |ΔDIF| 分布的分位数
    abs_ddif = sig_full['dif'].diff().abs().dropna()
    theta25 = float(abs_ddif.quantile(0.25))
    theta50 = float(abs_ddif.quantile(0.50))
    print('V5 门槛：|ΔDIF| 25%%分位 = %.4f，50%%分位 = %.4f' % (theta25, theta50))
    print()

    variants = [
        ('V1 买入确认(延迟1天)', dict(sig=sig_v1_confirm(sig_full))),
        ('V2 冷却期 K=3',        dict(cooldown=3)),
        ('V2 冷却期 K=5',        dict(cooldown=5)),
        ('V2 冷却期 K=10',       dict(cooldown=10)),
        ('V3 固定止损 X=3%',     dict(stop_x=0.03)),
        ('V3 固定止损 X=5%',     dict(stop_x=0.05)),
        ('V3 固定止损 X=8%',     dict(stop_x=0.08)),
        ('V4 移动止盈 Y=5%',     dict(trail_y=0.05)),
        ('V4 移动止盈 Y=8%',     dict(trail_y=0.08)),
        ('V4 移动止盈 Y=12%',    dict(trail_y=0.12)),
        ('V5 幅度门槛 θ=25%分位', dict(sig=sig_v5_threshold(sig_full, theta25))),
        ('V5 幅度门槛 θ=50%分位', dict(sig=sig_v5_threshold(sig_full, theta50))),
        # ---- 组合（按单变体结果挑选：V1 / V2(K=3) / V4(Y=5%) 三个最优单变体的组合）----
        ('组合 V1+V2(3)',              dict(sig=sig_v1_confirm(sig_full), cooldown=3)),
        ('组合 V1+V4(5%)',             dict(sig=sig_v1_confirm(sig_full), trail_y=0.05)),
        ('组合 V2(3)+V4(5%)',          dict(cooldown=3, trail_y=0.05)),
        ('组合 V1+V2(3)+V4(5%)',       dict(sig=sig_v1_confirm(sig_full), cooldown=3, trail_y=0.05)),
    ]

    rows = [row_base]
    variant_results = {}
    for name, kw in variants:
        sig = kw.pop('sig', sig_base).loc[idx] if 'sig' in kw else sig_base
        trades = run_custom(idx, sig, oatk, catk, odiv, cdiv,
                            cooldown=kw.get('cooldown', 0),
                            stop_x=kw.get('stop_x'),
                            trail_y=kw.get('trail_y'))
        wealth = build_wealth(idx, trades, oatk, catk, odiv, cdiv)
        legs = build_legs(idx, trades, oatk, catk, odiv, cdiv)
        row = summary_row(name, wealth, trades, legs)
        rows.append(row)
        variant_results[name] = row
    print_compare_table(rows)
    print()

    # ------------------------------------------------------------------
    # 稳健性：2022-01-01 前后两半（基线 + 指定变体）
    ROBUST = ['基线(方案C)', 'V1 买入确认(延迟1天)', 'V2 冷却期 K=3', 'V4 移动止盈 Y=5%']
    print('=' * 72)
    print('稳健性检验：按 %s 切前后两半（年化 / 最大回撤 / 进攻段胜率，段按建仓日归属）'
          % SPLIT_DATE.date())
    print('=' * 72)
    print('| 方案 | 区间 | 年化 | 最大回撤 | 段胜率 | 段数 |')
    print('|---|---|---|---|---|---|')
    for name in ROBUST:
        r = rows[0] if name == '基线(方案C)' else variant_results[name]
        wealth, legs = r['_wealth'], r['_legs']
        for label, lo, hi in [('前半 %s~%s' % (idx[0].date(), '2021-12-31'),
                               idx[0], SPLIT_DATE),
                              ('后半 %s~%s' % ('2022-01-01', idx[-1].date()),
                               SPLIT_DATE, idx[-1] + pd.Timedelta(days=1))]:
            m = half_metrics(wealth, legs, lo, hi)
            if m:
                print('| %s | %s | %s | %s | %s | %d |'
                      % (name, label, fmt_pct(m['ann']), fmt_pct(m['mdd']),
                         fmt_pct(m['win_rate']), m['n_legs']))
    print()


if __name__ == '__main__':
    main()
