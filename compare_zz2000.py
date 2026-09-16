#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
中证2000ETF(563300 华泰柏瑞, 被动) vs 中证2000增强ETF(159552 招商, 指数增强)
同窗对比（159552 上市 2024-06-28 起）：
  1) 买入持有收益对比（含相对超额）
  2) 轮动策略回测对比（方案C口径：MACD 17/34/9, sell_anywhere=True,
     信号T日收盘产生、T+1开盘成交、双边万1；防守腿固定 512890 红利低波）
"""
import sys
import io

if sys.platform == 'win32' and not getattr(sys.stdout, '_utf8_wrapped', False):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stdout._utf8_wrapped = True

from compare_variants import (load, load_or_fetch_etf, std_variant, buy_hold,
                              print_table)


def main():
    avg = load('avg_880003.csv')
    etfdiv = load('etf_512890_hfq.csv')
    df300 = load_or_fetch_etf('563300', 'etf_563300_hfq.csv')
    df552 = load_or_fetch_etf('159552', 'etf_159552_hfq.csv')
    print()

    start = df552.index[0]
    rows = []
    r, _ = std_variant(avg, df300, etfdiv, start, 'C: 进攻=563300 中证2000ETF')
    rows.append(r)
    r, res = std_variant(avg, df552, etfdiv, start, 'C: 进攻=159552 2000增强')
    rows.append(r)
    win = res['idx']
    rows.append(buy_hold(df300, win, '563300'))
    rows.append(buy_hold(df552, win, '159552'))

    # 159552 相对 563300 的买入持有区间超额（增强 alpha 直观体现）
    nav552 = df552.loc[win, 'close'] / df552.loc[win, 'close'].iloc[0]
    nav300 = df300.loc[win, 'close'] / df300.loc[win, 'close'].iloc[0]
    excess_pp = ((nav552.iloc[-1] - 1) - (nav300.iloc[-1] - 1)) * 100
    excess_rel = (nav552.iloc[-1] / nav300.iloc[-1] - 1) * 100

    span_days = (win[-1] - win[0]).days
    notes = ['成交口径：信号T日收盘产生，T+1开盘成交，双边万1；防守腿=512890',
             '159552 相对 563300 买入持有区间超额: %+0.2f 个百分点（相对净值 %+.2f%%）'
             % (excess_pp, excess_rel)]
    if span_days < 730:
        notes.insert(0, '*** 样本窗口仅约 %d 个月，样本太短仅供参考 ***'
                     % round(span_days / 30.4))
    print_table('窗口 %s ~ %s（159552 上市起，约 %d 个月）'
                % (win[0].date(), win[-1].date(), round(span_days / 30.4)),
                rows, notes=notes)


if __name__ == '__main__':
    main()
