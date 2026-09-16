#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
方案C信号迁移个股验证：进攻腿换成 515050 通信ETF 最新十大重仓等权篮子

- 信号不变：880003 平均股价 DIF(17,34) 拐点（方案C，零下拐头买/拐头即卖）
- 进攻腿A（基准）：现行 ETF 腿（2024-06-28 前 512100，之后 159552，拼接口径）
- 进攻腿B：515050 十大重仓等权篮子（日再平衡等权近似，后复权）
- 进攻腿C（窗口2附加）：515050 ETF 本身
- 防守腿统一为 512890 红利低波
- 费用：ETF 双边各万1；个股 买万1、卖万1+印花税0.05%（每次切出个股成本 万6+万1=万7）
- 信号 T 日收盘产生，T+1 开盘成交；换仓日收益=新腿 开盘→收盘 - 双边费用
- 个股停牌：价格前向填充（当日收益记0，换仓按填充价近似），脚本统计换仓日含停牌股的次数

重要口径提示：十大重仓取 2026Q2 截面，回测全程用同一篮子，存在幸存者/前视偏差，
结果偏乐观，仅供方向性参考。输出见 result_515050.txt
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

from backtest import calc_signals, max_drawdown, annualized

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')

# 515050 十大重仓（天天基金 2026-06-30 截面）
STOCKS = [
    ('300502', '新易盛'), ('603986', '兆易创新'), ('300308', '中际旭创'),
    ('002475', '立讯精密'), ('002384', '东山精密'), ('601138', '工业富联'),
    ('600183', '生益科技'), ('600487', '亨通光电'), ('002463', '沪电股份'),
    ('300394', '天孚通信'),
]

FEE_ETF = 0.0001                    # ETF 单边万1
FEE_STOCK_BUY = 0.0001              # 个股买入 万1
FEE_STOCK_SELL = 0.0001 + 0.0005    # 个股卖出 万1 + 印花税0.05%
COST_TO_ATK_STOCK = FEE_ETF + FEE_STOCK_BUY        # 卖红利买篮子 = 万2
COST_TO_DEF_STOCK = FEE_STOCK_SELL + FEE_ETF       # 卖篮子买红利 = 万7
COST_SWITCH_ETF = 2 * FEE_ETF                      # ETF腿切换 = 万2


def load(name):
    df = pd.read_csv(os.path.join(DATA, name), parse_dates=['date']).set_index('date')
    df.index = df.index.normalize()          # 通达信通道带 15:00 时间戳，统一归零
    return df


def build_basket(calendar):
    """十大重仓等权篮子：对齐交易日历，停牌前向填充。
    返回 close_ret(日收益), oc_ret(开盘→收盘), suspended(当日有停牌股的掩码)"""
    closes, opens = [], []
    for code, _name in STOCKS:
        df = load('stock_%s_hfq.csv' % code)
        closes.append(df['close'].reindex(calendar).ffill())
        opens.append(df['open'].reindex(calendar).ffill())
    close = pd.concat(closes, axis=1)
    open_ = pd.concat(opens, axis=1)
    suspended = close.isna().any(axis=1) | (close.diff().abs().sum(axis=1) == 0)
    close_ret = close.pct_change().mean(axis=1)
    oc_ret = (close / open_ - 1).mean(axis=1)
    return close_ret.fillna(0.0), oc_ret.fillna(0.0), suspended.fillna(False)


def splice_atk_etf(calendar):
    """现行进攻腿拼接口径：2024-06-28 前 512100，之后 159552（后复权）"""
    d1000 = load('etf_512100_hfq.csv').reindex(calendar)
    d2000 = load('etf_159552_hfq.csv').reindex(calendar)
    cut = pd.Timestamp('2024-06-28')
    close = pd.concat([d1000['close'][d1000.index < cut],
                       d2000['close'][d2000.index >= cut]])
    open_ = pd.concat([d1000['open'][d1000.index < cut],
                       d2000['open'][d2000.index >= cut]])
    # 拼接点收益率校准：cut 当日用 159552 自身收益，跨段不产生虚假涨跌
    close_ret = close.pct_change()
    cut_pos = close.index.searchsorted(cut)
    if cut_pos < len(close):
        d = close.index[cut_pos]
        close_ret.loc[d] = d2000['close'].pct_change().reindex(calendar).loc[d]
    oc_ret = close / open_ - 1
    return close_ret.fillna(0.0), oc_ret.fillna(0.0)


def simulate(idx, sig, atk_close_ret, atk_oc_ret, def_close_ret, def_oc_ret,
             cost_to_atk, cost_to_def):
    """T收盘信号→T+1开盘切换。返回 nav, hold_atk, n_trades"""
    n = len(idx)
    hold = np.zeros(n, dtype=bool)
    state = False          # False=防守(512890), True=进攻
    pending = None
    trades = 0
    rets = np.zeros(n)
    for i in range(n):
        r_atk = atk_close_ret.iloc[i]
        r_def = def_close_ret.iloc[i]
        if pending is not None:                      # 今日开盘执行切换
            new_state = (pending == 'atk')
            if new_state != state:                   # 同方向重复信号不换仓、不计费
                state = new_state
                trades += 1
                if state:
                    rets[i] = atk_oc_ret.iloc[i] - cost_to_atk
                else:
                    rets[i] = def_oc_ret.iloc[i] - cost_to_def
            else:
                rets[i] = r_atk if state else r_def
            pending = None
        else:
            rets[i] = r_atk if state else r_def
        hold[i] = state
        if sig['buy_sig'].iloc[i]:
            pending = 'atk'
        elif sig['sell_sig'].iloc[i]:
            pending = 'def'
    if pending is not None:                          # 末日信号无法执行，丢弃
        pass
    nav = (1 + pd.Series(rets, index=idx)).cumprod()
    return nav, pd.Series(hold, index=idx), trades


def report(label, nav, trades):
    print('%-26s %10s %10s %10s %8s'
          % (label, '%+.2f%%' % ((nav.iloc[-1] - 1) * 100),
             '%+.2f%%' % (annualized(nav) * 100),
             '%.2f%%' % (max_drawdown(nav) * 100), trades))


def main():
    avg = load('avg_880003.csv')
    ddiv = load('etf_512890_hfq.csv')

    # 方案C 信号（17/34，拐头即卖），用全部历史算 DIF 保证预热
    sig = calc_signals(avg['close'], sell_anywhere=True, fast=17, slow=34, sig_n=9)

    calendar = avg.index.intersection(ddiv.index)
    sig = sig.reindex(calendar).fillna(False)

    def_close_ret = ddiv['close'].reindex(calendar).pct_change().fillna(0.0)
    def_oc_ret = (ddiv['close'] / ddiv['open'] - 1).reindex(calendar).fillna(0.0)

    basket_ret, basket_oc, suspended = build_basket(calendar)
    etf_ret, etf_oc = splice_atk_etf(calendar)

    print('=' * 76)
    print('口径：信号=880003 DIF(17,34)拐点（方案C），防守腿=512890；')
    print('篮子=515050十大重仓(2026Q2截面)等权日再平衡，含个股印花税，存在前视偏差')
    print('=' * 76)

    for start, note in [('2019-01-18', '窗口1：与主回测同窗口'),
                        ('2019-10-16', '窗口2：515050上市起'),
                        ('2026-01-01', '窗口3：今年以来')]:
        idx = calendar[calendar >= start]
        s = sig.loc[idx]
        print('\n--- %s（%s ~ %s，%d 个交易日）---'
              % (note, idx[0].date(), idx[-1].date(), len(idx)))

        nav_b, hold_b, tr_b = simulate(idx, s, basket_ret.loc[idx], basket_oc.loc[idx],
                                       def_close_ret.loc[idx], def_oc_ret.loc[idx],
                                       COST_TO_ATK_STOCK, COST_TO_DEF_STOCK)
        nav_e, hold_e, tr_e = simulate(idx, s, etf_ret.loc[idx], etf_oc.loc[idx],
                                       def_close_ret.loc[idx], def_oc_ret.loc[idx],
                                       COST_SWITCH_ETF, COST_SWITCH_ETF)

        print('%-26s %10s %10s %10s %8s' % ('组合', '累计收益', '年化', '最大回撤', '换仓次数'))
        report('方案C·进攻=个股篮子', nav_b, tr_b)
        report('方案C·进攻=ETF拼接(现行)', nav_e, tr_e)

        if start == '2019-10-16':
            d515050 = load('etf_515050_hfq.csv')
            r5 = d515050['close'].reindex(calendar).pct_change().fillna(0.0).loc[idx]
            oc5 = (d515050['close'] / d515050['open'] - 1).reindex(calendar).fillna(0.0).loc[idx]
            nav_5, _, tr_5 = simulate(idx, s, r5, oc5, def_close_ret.loc[idx],
                                      def_oc_ret.loc[idx], COST_SWITCH_ETF, COST_SWITCH_ETF)
            report('方案C·进攻=515050ETF', nav_5, tr_5)
            p5 = d515050['close'].reindex(idx).ffill()
            report('515050ETF 纯持有', p5 / p5.iloc[0], '-')

        # 纯持有基准
        report('个股篮子 纯持有', (1 + basket_ret.loc[idx]).cumprod(), '-')
        report('512890红利低波 纯持有', (1 + def_close_ret.loc[idx]).cumprod(), '-')
        print('进攻时间占比: 篮子腿 %.1f%% / ETF腿 %.1f%%'
              % (hold_b.mean() * 100, hold_e.mean() * 100))
        sw_days = [d for d in idx if suspended.loc[d]]
        print('篮子停牌填充日: %d 天（换仓若落在此类日期为近似成交）' % len(sw_days))

        # 逐年收益
        yearly = pd.DataFrame({'篮子腿': nav_b, 'ETF腿': nav_e}).groupby(idx.year)
        ytab = yearly.last() / yearly.first() - 1
        print('分年收益:')
        for y, row in ytab.iterrows():
            print('  %d  篮子腿 %+8.2f%%   ETF腿 %+8.2f%%' % (y, row['篮子腿'] * 100, row['ETF腿'] * 100))


if __name__ == '__main__':
    main()
