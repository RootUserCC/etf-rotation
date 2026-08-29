#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ETF 轮动回测：基于通达信"平均股价"指数的 MACD-DIF 拐点信号
- 买入信号：DIF 零轴下方拐头向上（DIF转升 AND DIF<0）→ 持有 512100 中证1000ETF
- 卖出信号：DIF 零轴上方拐头向下（DIF转降 AND DIF>0）→ 切换至 510880 红利ETF
- 信号于收盘产生，次一交易日开盘价成交，双边佣金各万1（ETF 无印花税）
"""
import sys
import io
import pandas as pd
import numpy as np

# pythonw（无窗口计划任务）下 sys.stdout 为 None，不能 reconfigure
if sys.platform == 'win32' and sys.stdout is not None and not getattr(sys.stdout, '_utf8_wrapped', False):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stdout._utf8_wrapped = True


def ema(s: pd.Series, n: int) -> pd.Series:
    """与通达信 EMA 一致：EMA(X,N) = 2*X/(N+1) + (N-1)/(N+1)*REF(EMA,1)，首值取首个有效 X"""
    return s.ewm(alpha=2.0 / (n + 1), adjust=False).mean()


def calc_signals(close: pd.Series, sell_anywhere: bool = False,
                 fast: int = 12, slow: int = 26, sig_n: int = 9,
                 bar_mode: bool = False, buy_anywhere: bool = False) -> pd.DataFrame:
    """按通达信 MACD 公式计算 DIF/DEA/MACDVAL 与拐点信号
    sell_anywhere=False: 原版，卖出要求 DIF>0
    sell_anywhere=True : 对称版，DIF 转降即切红利（零下也卖）
    fast/slow/sig_n: MACD 参数，默认 12/26/9（方案B），方案C 用 17/34/9
    bar_mode=True    : 四色柱模式（方案D）——
        卖出：MACD柱由红变蓝（零上红柱首次缩头）
        买入：DIF 零下拐头向上（同原买点）或 MACD柱由蓝变红（零上红柱重新放大）
    """
    dif = ema(close, fast) - ema(close, slow)
    dea = ema(dif, sig_n)
    macdval = (dif - dea) * 2
    dif_prev1 = dif.shift(1)
    dif_prev2 = dif.shift(2)
    dif_up = (dif_prev1 < dif_prev2) & (dif > dif_prev1)      # DIF 转升
    dif_down = (dif_prev1 > dif_prev2) & (dif < dif_prev1)    # DIF 转降
    df = pd.DataFrame({'dif': dif, 'dea': dea, 'macdval': macdval})
    if bar_mode:
        m = macdval
        m_prev = m.shift(1)
        # 四色柱（通达信口径）：蓝=零上红柱缩头（m>=0 且 前柱>0 且 m<前柱），其余零上为红
        blue = (m >= 0) & (m_prev > 0) & (m < m_prev)
        red = (m >= 0) & ~blue
        if buy_anywhere:
            df['buy_sig'] = dif_up                            # 方案E：DIF 拐头即买
        else:
            df['buy_sig'] = (dif_up & (dif < 0)) | (red & blue.shift(1).fillna(False))
        df['sell_sig'] = blue & red.shift(1).fillna(False)
    else:
        df['buy_sig'] = dif_up & (dif < 0)                       # 低位买入
        df['sell_sig'] = dif_down if sell_anywhere else (dif_down & (dif > 0))
    return df


def max_drawdown(nav: pd.Series) -> float:
    peak = nav.cummax()
    return float(((nav - peak) / peak).min())


def annualized(nav: pd.Series) -> float:
    days = (nav.index[-1] - nav.index[0]).days
    if days <= 0:
        return float('nan')
    return float(nav.iloc[-1] ** (365.0 / days) - 1)


def run_backtest(avg: pd.DataFrame, etf1000: pd.DataFrame, etfdiv: pd.DataFrame,
                 fee: float = 0.0001, sell_anywhere: bool = False,
                 label: str = '轮动策略', start_date=None,
                 fast: int = 12, slow: int = 26, sig_n: int = 9,
                 bar_mode: bool = False, buy_anywhere: bool = False,
                 signals: pd.DataFrame = None,
                 verbose: bool = True) -> dict:
    """
    avg/etf1000/etfdiv: index=日期, 列至少含 open/close
    信号基于 avg 的 close 在 T 日收盘产生，T+1 日以被切换 ETF 的 open 成交
    start_date: 仅统计该日期之后的区间（信号用全部历史计算，保证 DIF 预热）
    fast/slow/sig_n: MACD 参数，默认 12/26/9（方案B），方案C 用 17/34/9
    bar_mode: True 时改用四色柱信号（方案D，见 calc_signals）；
              配合 buy_anywhere=True 即方案E（DIF拐头即买、MACD变蓝即卖）
    signals: 外部信号 DataFrame（index=日期, 含 buy_sig/sell_sig 列），
             传入后忽略 fast/slow/sig_n/bar_mode，用于 KDJ 等自定义信号对比
    返回结果字典：nav/trades/sig/hold1000/idx
    """
    if signals is not None:
        sig = signals
    else:
        sig = calc_signals(avg['close'], sell_anywhere=sell_anywhere,
                           fast=fast, slow=slow, sig_n=sig_n, bar_mode=bar_mode,
                           buy_anywhere=buy_anywhere)

    # 对齐三只标的的交易日
    idx = etf1000.index.intersection(etfdiv.index).intersection(sig.index)
    if start_date is not None:
        start_date = pd.Timestamp(start_date)
        idx = idx[idx >= start_date]
    sig = sig.loc[idx]
    p1000_o = etf1000.loc[idx, 'open']
    p1000_c = etf1000.loc[idx, 'close']
    pdiv_o = etfdiv.loc[idx, 'open']
    pdiv_c = etfdiv.loc[idx, 'close']

    n = len(idx)
    hold1000 = np.zeros(n, dtype=bool)   # 当日持仓是否为1000ETF（收盘状态）
    trades = []                          # (日期, 动作, 价格)
    state = False                        # False=红利ETF, True=1000ETF
    pending = None                       # T日信号，T+1开盘执行

    for i in range(n):
        # 先执行昨日信号（今日开盘切换）
        if pending is not None:
            if pending == 'buy1000' and not state:
                state = True
                trades.append((idx[i], '买入1000ETF/卖出红利ETF', float(p1000_o.iloc[i])))
            elif pending == 'sell1000' and state:
                state = False
                trades.append((idx[i], '卖出1000ETF/买入红利ETF', float(pdiv_o.iloc[i])))
            pending = None
        # 收盘记录状态
        hold1000[i] = state
        # 收盘产生新信号
        if sig['buy_sig'].iloc[i]:
            pending = 'buy1000'
        elif sig['sell_sig'].iloc[i]:
            pending = 'sell1000'

    hold1000 = pd.Series(hold1000, index=idx)

    # 策略日收益：持仓按 close-to-close，切换日扣双边费用
    r1000 = p1000_c.pct_change()
    rdiv = pdiv_c.pct_change()
    strat_r = np.where(hold1000, r1000, rdiv)
    strat_r = pd.Series(strat_r, index=idx).fillna(0.0)

    trade_days = pd.Series([t[0] for t in trades])
    cost = pd.Series(0.0, index=idx)
    for d in trade_days:
        cost[d] = 2 * fee            # 卖一买一，双边
    strat_r = strat_r - cost

    nav_strat = (1 + strat_r).cumprod()
    nav_1000 = (p1000_c / p1000_c.iloc[0])
    nav_div = (pdiv_c / pdiv_c.iloc[0])
    nav_half = (nav_1000 * 0.5 + nav_div * 0.5)

    # ---- 输出 ----
    if verbose:
        print('=' * 64)
        print('回测区间: %s ~ %s  共 %d 个交易日' % (idx[0].date(), idx[-1].date(), n))
        print('信号统计: 买入信号 %d 次, 卖出信号 %d 次, 实际换仓 %d 次'
              % (int(sig['buy_sig'].sum()), int(sig['sell_sig'].sum()), len(trades)))
        print('=' * 64)
        fmt = '%-22s %10s %10s %10s'
        print(fmt % ('标的', '累计收益', '年化收益', '最大回撤'))
        for name, nav in [(label, nav_strat), ('中证1000ETF 持有', nav_1000),
                          ('红利ETF 持有', nav_div), ('50/50 静态组合', nav_half)]:
            print(fmt % (name, '%.2f%%' % ((nav.iloc[-1] - 1) * 100),
                         '%.2f%%' % (annualized(nav) * 100),
                         '%.2f%%' % (max_drawdown(nav) * 100)))
        print('-' * 64)

        print('历次换仓明细:')
        for d, action, price in trades:
            print('  %s  %-22s  成交价 %.3f' % (d.date(), action, price))

        # 逐年收益
        print('-' * 64)
        print('分年度收益对比:')
        yearly = pd.DataFrame({
            '轮动策略': nav_strat.resample('YE').last().pct_change(),
            '1000ETF': nav_1000.resample('YE').last().pct_change(),
            '红利ETF': nav_div.resample('YE').last().pct_change(),
        })
        for name in yearly.columns:
            first_val = {'轮动策略': nav_strat, '1000ETF': nav_1000, '红利ETF': nav_div}[name]
            y0 = first_val.resample('YE').last()
            yearly.loc[yearly.index[0], name] = y0.iloc[0] / first_val.iloc[0] - 1
        for dt, row in yearly.iterrows():
            print('  %d  策略 %8.2f%%   1000ETF %8.2f%%   红利ETF %8.2f%%'
                  % (dt.year, row['轮动策略'] * 100, row['1000ETF'] * 100, row['红利ETF'] * 100))

        # 持仓占比
        pct = hold1000.mean() * 100
        print('-' * 64)
        print('持仓时间占比: 中证1000ETF %.1f%% / 红利ETF %.1f%%' % (pct, 100 - pct))

    return {
        'nav_strat': nav_strat, 'nav_1000': nav_1000,
        'nav_div': nav_div, 'nav_half': nav_half,
        'trades': trades, 'sig': sig, 'hold1000': hold1000,
        'p1000_c': p1000_c, 'pdiv_c': pdiv_c, 'idx': idx,
    }
