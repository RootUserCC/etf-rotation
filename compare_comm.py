#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
进攻仓候选对比：通信ETF（方案C：MACD 17/34/9, sell_anywhere=True, 双边万1）

515880 通信ETF（国泰CES通信设备，沪市，2019-08 上市）作为进攻腿，
与现有进攻腿 512100（中证1000）/159552（中证2000增强）同窗对比：
  1) 长窗口：515880 上市起 vs 512100（同一防守腿 512890 红利低波）
  2) 159552 可比窗口：2024-06-28 起 三腿对比（512100 / 159552 / 515880）

新拉数据存 data/etf_515880_hfq.csv。
"""
import sys
import io

import pandas as pd

if sys.platform == 'win32' and not getattr(sys.stdout, '_utf8_wrapped', False):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stdout._utf8_wrapped = True

from compare_variants import (load, load_or_fetch_etf, std_variant,
                              print_table, START_2019)
from compare_variants import metrics


def buy_hold(df, idx, name):
    """同 compare_variants.buy_hold，但容忍标的缺个别交易日（ffill）"""
    nav = df['close'].reindex(idx).ffill()
    nav = nav / nav.iloc[0]
    cum, ann, mdd = metrics(nav)
    return (name + ' 买入持有', cum, ann, mdd, '-')


def blend_legs(df_a, df_b, w_a):
    """按 w_a/(1-w_a) 日频再平衡合成进攻腿（A=159552, B=515880）
    close: 日收益加权累积；open: 用各自(开/昨收-1)加权还原，保证与 run_backtest 口径兼容"""
    idx = df_a.index.intersection(df_b.index)
    ca, cb = df_a.loc[idx, 'close'], df_b.loc[idx, 'close']
    rc = w_a * ca.pct_change() + (1 - w_a) * cb.pct_change()
    close = (1 + rc.fillna(0.0)).cumprod()
    oa, ob = df_a.loc[idx, 'open'], df_b.loc[idx, 'open']
    ro = w_a * (oa / ca.shift(1) - 1) + (1 - w_a) * (ob / cb.shift(1) - 1)
    open_ = close.shift(1).fillna(1.0) * (1 + ro.fillna(0.0))
    return pd.DataFrame({'open': open_, 'close': close}, index=idx)


def main():
    avg = load('avg_880003.csv')
    etf1000 = load('etf_512100_hfq.csv')
    etfdiv = load('etf_512890_hfq.csv')
    etf159552 = load('etf_159552_hfq.csv')

    print('--- 拉取 通信ETF(515880[沪]) 后复权 ---')
    df515880 = load_or_fetch_etf('515880', 'etf_515880_hfq.csv')

    # ---- 1) 长窗口：515880 上市起 ----
    start1 = max(df515880.index[0], pd.Timestamp(START_2019))
    rows = []
    r, _ = std_variant(avg, etf1000, etfdiv, start1, 'C: 进攻=512100(同窗)')
    rows.append(r)
    r, res1 = std_variant(avg, df515880, etfdiv, start1, 'C: 进攻=515880 通信')
    rows.append(r)
    win = res1['idx']
    rows.append(buy_hold(etf1000, win, '512100'))
    rows.append(buy_hold(df515880, win, '515880'))
    rows.append(buy_hold(etfdiv, win, '512890'))
    print_table('对比一 长窗口 %s ~ %s（515880 上市起，防守腿=512890 红利低波）'
                % (win[0].date(), win[-1].date()), rows)

    # ---- 2) 159552 可比窗口：2024-06-28 起 ----
    start2 = max(etf159552.index[0], pd.Timestamp('2024-06-28'))
    rows = []
    r, _ = std_variant(avg, etf1000, etfdiv, start2, 'C: 进攻=512100')
    rows.append(r)
    r, _ = std_variant(avg, etf159552, etfdiv, start2, 'C: 进攻=159552(增强)')
    rows.append(r)
    r, res2 = std_variant(avg, df515880, etfdiv, start2, 'C: 进攻=515880 通信')
    rows.append(r)
    win = res2['idx']
    rows.append(buy_hold(etf1000, win, '512100'))
    rows.append(buy_hold(etf159552, win, '159552'))
    rows.append(buy_hold(df515880, win, '515880'))
    span_days = (win[-1] - win[0]).days
    notes = []
    if span_days < 730:
        notes.append('*** 样本窗口仅约 %d 个月，样本太短仅供参考 ***'
                     % round(span_days / 30.4))
    print_table('对比二 159552 可比窗口 %s ~ %s（约 %d 个月）'
                % (win[0].date(), win[-1].date(), round(span_days / 30.4)),
                rows, notes=notes)

    # ---- 3) 混合进攻腿：159552 + 515880 按比例 ----
    print('#' * 78)
    print('# 对比三：进攻腿 = 159552/515880 混合（日频再平衡），窗口同上')
    print('#' * 78)
    blend = blend_legs(etf159552, df515880, 0.5)
    start3 = blend.index[0]
    rows = []
    for w in [1.0, 0.75, 0.5, 0.25, 0.0]:
        leg = blend_legs(etf159552, df515880, w)
        label = 'C: 进攻=%d%%159552+%d%%515880' % (round(w * 100), round((1 - w) * 100))
        r, res3 = std_variant(avg, leg, etfdiv, start3, label)
        rows.append(r)
    win = res3['idx']
    rows.append(buy_hold(blend, win, '50/50混合'))
    span_days = (win[-1] - win[0]).days
    notes = ['混合腿按日频再平衡近似（每日恢复目标比例），未计再平衡交易费用']
    if span_days < 730:
        notes.insert(0, '*** 样本窗口仅约 %d 个月，样本太短仅供参考 ***'
                     % round(span_days / 30.4))
    print_table('对比三 混合进攻腿 %s ~ %s（约 %d 个月，防守腿=512890）'
                % (win[0].date(), win[-1].date(), round(span_days / 30.4)),
                rows, notes=notes)

    print('全部完成。')


if __name__ == '__main__':
    main()
