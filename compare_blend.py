#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
进攻腿对比：159552(2000增强) vs 512100(中证1000) vs 50/50 混合
方案C口径（MACD 17/34/9, sell_anywhere=True, 信号T日收盘产生、T+1开盘成交, 双边万1）
混合腿：每日再平衡 50/50 合成序列（open/close 按隔夜段/日内段各半加权合成）。
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

from compare_variants import load, std_variant, buy_hold, print_table


def make_blend(a: pd.DataFrame, b: pd.DataFrame) -> pd.DataFrame:
    """每日再平衡 50/50 合成 OHLC（在共同交易日上，按收益率加权）"""
    idx = a.index.intersection(b.index)
    a, b = a.loc[idx], b.loc[idx]
    # 隔夜段：昨收→今开；日内段：今开→今收，各取两只的等权平均
    on = 0.5 * (a['open'] / a['close'].shift(1) - 1) + 0.5 * (b['open'] / b['close'].shift(1) - 1)
    iday = 0.5 * (a['close'] / a['open'] - 1) + 0.5 * (b['close'] / b['open'] - 1)
    on.iloc[0] = 0.0
    close = (1 + on.fillna(0)).cumprod() * (1 + iday).cumprod() / (1 + iday).cumprod().shift(1).fillna(1)
    # 上面写法绕，直接逐日合成：
    cl = [1.0]
    op = [1.0]
    for i in range(1, len(idx)):
        o = cl[-1] * (1 + on.iloc[i])
        c = o * (1 + iday.iloc[i])
        op.append(o)
        cl.append(c)
    out = pd.DataFrame({'open': op, 'close': cl}, index=idx)
    out['high'] = out[['open', 'close']].max(axis=1)
    out['low'] = out[['open', 'close']].min(axis=1)
    return out


def main():
    avg = load('avg_880003.csv')
    etf1000 = load('etf_512100_hfq.csv')
    etf552 = load('etf_159552_hfq.csv')
    etfdiv = load('etf_512890_hfq.csv')

    start = etf552.index[0]
    blend = make_blend(etf1000, etf552)
    print('混合腿合成区间: %s ~ %s' % (blend.index[0].date(), blend.index[-1].date()))

    rows = []
    r, _ = std_variant(avg, etf552, etfdiv, start, 'C: 进攻=159552(2000增强)')
    rows.append(r)
    r, _ = std_variant(avg, etf1000, etfdiv, start, 'C: 进攻=512100(中证1000)')
    rows.append(r)
    r, res = std_variant(avg, blend, etfdiv, start, 'C: 进攻=50/50混合')
    rows.append(r)
    win = res['idx']
    rows.append(buy_hold(etf552, win, '159552'))
    rows.append(buy_hold(etf1000, win, '512100'))
    rows.append(buy_hold(blend, win, '50/50混合'))

    span_days = (win[-1] - win[0]).days
    print_table('进攻腿对比 窗口 %s ~ %s（159552 上市起，约 %d 个月）'
                % (win[0].date(), win[-1].date(), round(span_days / 30.4)),
                rows,
                notes=['混合腿为每日再平衡 50/50 合成序列，含轻微再平衡红利；'
                       '实盘中只在每次切入进攻仓时配平即可，结果接近',
                       '*** 窗口约 %d 个月，样本偏短仅供参考 ***' % round(span_days / 30.4)])


if __name__ == '__main__':
    main()
