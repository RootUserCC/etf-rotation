#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
进攻腿对比：中证2000增强(159552) vs 主线板块（算力/通信）
（方案C口径：MACD 17/34/9, sell_anywhere=True, 信号T日收盘产生、
T+1开盘成交、双边万1；防守腿固定 512890 红利低波）

主线板块口径（对照 site/ai.html 的“算力硬件=市场主线”分类）：
  515880 通信ETF（国泰CES通信设备，2019-09 上市）
  515050 5GETF （华夏中证5G通信主题，2019-10 上市）
  主线组合 = 515880/515050 各50% 日频再平衡合成腿

窗口一（长窗）：515050 上市起，512100 / 515880 / 515050 / 主线组合
窗口二（同窗）：159552 上市（2024-06-28）起，加 159552 基准对比
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
from compare_comm import buy_hold, blend_legs   # 容忍缺日的买入持有 / 合成腿


def main():
    avg = load('avg_880003.csv')
    etf1000 = load('etf_512100_hfq.csv')
    etfdiv = load('etf_512890_hfq.csv')
    df552 = load('etf_159552_hfq.csv')

    print('--- 拉取 通信ETF(515880[沪]) / 5GETF(515050[沪]) 后复权 ---')
    df880 = load_or_fetch_etf('515880', 'etf_515880_hfq.csv')
    df050 = load_or_fetch_etf('515050', 'etf_515050_hfq.csv')
    blend = blend_legs(df880, df050, 0.5)
    print()

    legs = [('512100 中证1000', etf1000),
            ('515880 通信', df880),
            ('515050 5G', df050),
            ('主线组合 通信/5G各半', blend)]

    # ---- 窗口一：长窗口（515050 上市起，159552 尚未上市不参与） ----
    start1 = max(df050.index[0], pd.Timestamp(START_2019))
    rows = []
    res1 = None
    for name, df in legs:
        r, res1 = std_variant(avg, df, etfdiv, start1, 'C: 进攻=%s' % name)
        rows.append(r)
    win = res1['idx']
    for name, df in legs:
        rows.append(buy_hold(df, win, name))
    rows.append(buy_hold(etfdiv, win, '512890 红利低波'))
    print_table('窗口一 长窗口 %s ~ %s（515050 上市起，防守腿=512890）'
                % (win[0].date(), win[-1].date()), rows)

    # ---- 窗口二：159552 可比窗口（2024-06-28 起） ----
    start2 = df552.index[0]
    rows = []
    r, _ = std_variant(avg, df552, etfdiv, start2, 'C: 进攻=159552 2000增强(基准)')
    rows.append(r)
    res2 = None
    for name, df in legs[1:]:          # 512100 不再重复，只看主线三腿
        r, res2 = std_variant(avg, df, etfdiv, start2, 'C: 进攻=%s' % name)
        rows.append(r)
    win = res2['idx']
    rows.append(buy_hold(df552, win, '159552 2000增强'))
    for name, df in legs[1:]:
        rows.append(buy_hold(df, win, name))
    span_days = (win[-1] - win[0]).days
    notes = ['成交口径：信号T日收盘产生，T+1开盘成交，双边万1',
             '主线组合按日频再平衡近似，未计再平衡交易费用']
    if span_days < 730:
        notes.insert(0, '*** 样本窗口仅约 %d 个月，样本太短仅供参考 ***'
                     % round(span_days / 30.4))
    print_table('窗口二 159552 可比窗口 %s ~ %s（约 %d 个月）'
                % (win[0].date(), win[-1].date(), round(span_days / 30.4)),
                rows, notes=notes)

    print('全部完成。')


if __name__ == '__main__':
    main()
