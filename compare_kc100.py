#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
进攻腿对比：512100 中证1000 vs 159552 中证2000增强 vs 588220 科创100ETF
（方案C口径：MACD 17/34/9, sell_anywhere=True, 信号T日收盘产生、
T+1开盘成交、双边万1；防守腿固定 512890 红利低波）

窗口一（长窗）：588220 上市（2023-09）起，512100 vs 588220
窗口二（同窗）：159552 上市（2024-06-28）起，三者同窗对比
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
    etf1000 = load('etf_512100_hfq.csv')
    etfdiv = load('etf_512890_hfq.csv')

    print('--- 拉取 科创100ETF基金(588220[沪], 2023-09上市) / 中证2000增强ETF(159552[深]) 后复权 ---')
    dfkc = load_or_fetch_etf('588220', 'etf_588220_hfq.csv')
    df552 = load_or_fetch_etf('159552', 'etf_159552_hfq.csv')
    print()

    # ---- 窗口一：588220 上市起，512100 vs 588220 ----
    start1 = dfkc.index[0]
    rows = []
    r, _ = std_variant(avg, etf1000, etfdiv, start1, 'C: 进攻=512100')
    rows.append(r)
    r, res1 = std_variant(avg, dfkc, etfdiv, start1, 'C: 进攻=588220 科创100')
    rows.append(r)
    win = res1['idx']
    rows.append(buy_hold(etf1000, win, '512100'))
    rows.append(buy_hold(dfkc, win, '588220'))
    span_days = (win[-1] - win[0]).days
    notes = []
    if span_days < 730:
        notes.append('*** 样本窗口仅约 %d 个月，样本太短仅供参考 ***'
                     % round(span_days / 30.4))
    print_table('窗口一 %s ~ %s（588220 上市起，约 %d 个月）'
                % (win[0].date(), win[-1].date(), round(span_days / 30.4)),
                rows, notes=notes)

    # ---- 窗口二：159552 上市起，三者同窗 ----
    start2 = df552.index[0]
    rows = []
    r, _ = std_variant(avg, etf1000, etfdiv, start2, 'C: 进攻=512100')
    rows.append(r)
    r, _ = std_variant(avg, dfkc, etfdiv, start2, 'C: 进攻=588220 科创100')
    rows.append(r)
    r, res2 = std_variant(avg, df552, etfdiv, start2, 'C: 进攻=159552 2000增强')
    rows.append(r)
    win = res2['idx']
    rows.append(buy_hold(etf1000, win, '512100'))
    rows.append(buy_hold(dfkc, win, '588220'))
    rows.append(buy_hold(df552, win, '159552'))
    span_days = (win[-1] - win[0]).days
    notes = ['成交口径：信号T日收盘产生，T+1开盘成交，双边万1']
    if span_days < 730:
        notes.insert(0, '*** 样本窗口仅约 %d 个月，样本太短仅供参考 ***'
                     % round(span_days / 30.4))
    print_table('窗口二 %s ~ %s（159552 上市起，约 %d 个月）'
                % (win[0].date(), win[-1].date(), round(span_days / 30.4)),
                rows, notes=notes)


if __name__ == '__main__':
    main()
