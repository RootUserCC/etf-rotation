#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
防守腿对比：512890 红利低波 vs 512800 银行ETF（方案C口径：MACD 17/34/9,
sell_anywhere=True, 信号T日收盘产生、T+1开盘成交、双边万1）
窗口 2019-01-18（512890 上市）起；512800 银行ETF 2017-07 上市，可全覆盖。
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
                              print_table, dual_defense_backtest, dual_row,
                              START_2019)


def main():
    avg = load('avg_880003.csv')
    etf1000 = load('etf_512100_hfq.csv')
    etfdiv = load('etf_512890_hfq.csv')

    print('--- 拉取 银行ETF(512800[沪], 2017-07上市) 后复权 ---')
    dfbank = load_or_fetch_etf('512800', 'etf_512800_hfq.csv')
    print()

    rows = []
    r, _ = std_variant(avg, etf1000, etfdiv, START_2019, '防守=512890 红利低波(现有)')
    rows.append(r)
    r, res_b = std_variant(avg, etf1000, dfbank, START_2019, '防守=512800 银行ETF')
    rows.append(r)
    res_c = dual_defense_backtest(avg, etf1000, etfdiv, dfbank, START_2019,
                                  pick=True, label='c')
    rows.append(dual_row(res_c, '防守=512890/512800 20日动量择优'))
    win = res_b['idx']
    rows.append(buy_hold(etf1000, win, '512100'))
    rows.append(buy_hold(etfdiv, win, '512890'))
    rows.append(buy_hold(dfbank, win, '512800'))
    print_table('防守腿对比 窗口 %s ~ %s' % (win[0].date(), win[-1].date()), rows,
                notes=['成交口径：信号T日收盘产生，T+1开盘成交，双边万1',
                       '择优版：每次切入防守仓时比较两者近20日涨幅选高者，防守期间不换仓'])

    if res_c['choice_log']:
        import pandas as pd
        print('择优明细（切入防守仓时的选择）:')
        for d, choice, m1, m2 in res_c['choice_log']:
            picked = '512890' if choice == 'def1' else '512800'
            m1s = 'NA' if pd.isna(m1) else '%+.2f%%' % (m1 * 100)
            m2s = 'NA' if pd.isna(m2) else '%+.2f%%' % (m2 * 100)
            print('  %s  选中 %s   (512890近20日 %s, 512800近20日 %s)'
                  % (d.date(), picked, m1s, m2s))


if __name__ == '__main__':
    main()
