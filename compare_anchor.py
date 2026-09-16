#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
锚对比：信号源 = 平均股价(880003) vs 中证2000ETF(563300) vs 2000增强(159552)
口径同方案C：MACD 17/34/9, sell_anywhere=True, 信号T日收盘、T+1开盘成交、双边万1
窗口统一取 159552 数据可用起（2024-06-28 上市），三版同一窗口同窗对比：
  进攻腿固定 159552，防守腿固定 512890，只换信号锚。
"""
import sys
import io

if sys.platform == 'win32' and not getattr(sys.stdout, '_utf8_wrapped', False):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stdout._utf8_wrapped = True

from compare_variants import (load, std_variant, buy_hold, print_table)


def main():
    avg = load('avg_880003.csv')
    atk = load('etf_159552_hfq.csv')
    idx300 = load('etf_563300_hfq.csv')
    dfn = load('etf_512890_hfq.csv')

    start = atk.index[0]
    rows = []
    r, res1 = std_variant(avg, atk, dfn, start, '锚=平均股价(880003, 现有)')
    rows.append(r)
    r, _ = std_variant(idx300, atk, dfn, start, '锚=中证2000ETF(563300)')
    rows.append(r)
    r, res3 = std_variant(atk, atk, dfn, start, '锚=2000增强(159552, 自身)')
    rows.append(r)

    win = res3['idx']
    rows.append(buy_hold(atk, win, '159552'))
    rows.append(buy_hold(dfn, win, '512890'))

    # 信号差异：三个锚的换仓次数/持仓占比差异
    sig1, sig3 = res1, res3
    span_days = (win[-1] - win[0]).days
    notes = ['进攻腿固定159552、防守腿固定512890，仅信号锚不同，同窗对比',
             '159552 样本窗口仅约 %d 个月，增强alpha与信号效果混在一起，仅供参考'
             % round(span_days / 30.4)]
    print_table('锚对比 窗口 %s ~ %s' % (win[0].date(), win[-1].date()),
                rows, notes=notes)


if __name__ == '__main__':
    main()
