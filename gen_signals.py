#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每日扫描自选股池 MACD 四色柱状态，导出 site/ai_signals.json 供网页端提醒。

提醒口径（放宽版，绿转黄起算、翻红不重置）：
  从第 1 根黄柱（绿柱缩头）开始计数，之后 hist 不回落即可（可变红，翻红不重置），
  计到第 3 根 → 买点提醒（state=buy）；第 1~2 根 → 观察名单（state=watch）。
  卖出（6 仓方案）：柱回落（变色）即卖 或 止损 -8%；卖出前为持有中（hold）。
  多个买点同日出现时按近 20 日跌幅深→浅排序（buy 列表已排好）。
  注：变色快出规则下第 3 根组合收益最优（2026 回测 +130.8%，回撤 -18.1%），按用户指定口径执行。

数据：pytdx 日线，缓存 data/ai_stocks/（当日已抓则直接用缓存）。
"""
import json
import os

import pandas as pd

import ai_yellow_bar as ay
import fetch_data as fd
from compare_ymix import mixed_run

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, 'site', 'ai_signals.json')


def bar_color(h, h1):
    """当根柱颜色：黄=零下回升，绿=零下回落，红=零上放大，蓝=零上缩小"""
    if h < 0:
        return '黄' if h > h1 else '绿'
    return '红' if h > h1 else '蓝'


def hold_state(p, run, start):
    """按现行口径（放宽计数第3根买、柱回落(变色)或止损-8%卖）判断当前是否持仓中。

    run 为逐日放宽计数数组（compare_ymix.mixed_run 的 yrun），run[j]==0 即 hist 回落。
    返回 None 或 {'entry_date', 'entry', 'days', 'ret', 'sl'}
    （ret 为浮动盈亏 %，sl 为止损价）。
    """
    idx, closes = p['idx'], p['closes']
    n = len(closes)
    i = 60  # 预热，与 ay.simulate 一致
    while i < n:
        if run[i] == 3 and idx[i] >= start:
            entry = closes[i]
            j = i + 1
            while j < n:
                c = closes[j]
                if run[j] == 0 or c <= entry * 0.92:
                    break
                j += 1
            if j >= n:
                return {'entry_date': str(idx[i].date()),
                        'entry': round(float(entry), 2),
                        'days': n - 1 - i,
                        'ret': round(float(closes[-1] / entry - 1) * 100, 2),
                        'sl': round(float(entry * 0.92), 2)}
            i = j + 1  # 卖出后才能再开仓
        else:
            i += 1
    return None


def main():
    stocks = ay.load_stock_list()

    from pytdx.hq import TdxHq_API
    api = TdxHq_API()
    fd._connect_hq(api)

    board = []
    for k, (code, name, _note) in enumerate(stocks):
        try:
            df = ay.fetch_daily(api, code)
        except Exception as e:
            print('  [跳过] %s %s: %s' % (code, name, repr(e)[:50]))
            continue
        p = ay.prepare(df)
        hist, yrun = mixed_run(df)
        h, h1 = hist[-1], hist[-2]
        srun = int(p['run'][-1])
        y = int(yrun[-1])
        closes = df['close']
        ret20 = closes.iloc[-1] / closes.iloc[-21] - 1 if len(closes) > 21 else float('nan')
        board.append({
            'code': code, 'name': name,
            'date': str(df.index[-1].date()),
            'close': round(float(closes.iloc[-1]), 2),
            'chg': round(float(closes.iloc[-1] / closes.iloc[-2] - 1) * 100, 2),  # 今日涨跌幅 %
            'ret20': None if ret20 != ret20 else round(float(ret20) * 100, 1),    # 近20日涨跌幅 %
            'bar': bar_color(h, h1),
            'yrun': y,                     # 放宽计数（黄起、可翻红）
            'srun': srun,                  # 严格黄柱计数
            'state': 'buy' if y == 3 else ('watch' if 1 <= y <= 2 else ''),
            'hold': hold_state(p, yrun, ay.SIGNAL_START),   # 买点已过、蓝柱未出 → 持仓中
        })
        if (k + 1) % 10 == 0:
            print('  已扫描 %d/%d ...' % (k + 1, len(stocks)))
    api.disconnect()

    out = {
        'updated': str(pd.Timestamp.now()),
        # 买点按近20日跌幅深→浅排序（满仓时优先买跌得深的）
        'buy': sorted([r for r in board if r['state'] == 'buy'],
                      key=lambda r: (r['ret20'] is None, r['ret20'])),
        'watch': [r for r in board if r['state'] == 'watch'],
        'hold': sorted([r for r in board if r['hold']],
                       key=lambda r: r['hold']['entry_date'], reverse=True),
        'board': board,
    }
    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print('买点 %d 只，观察 %d 只，持仓 %d 只，已导出 %s'
          % (len(out['buy']), len(out['watch']), len(out['hold']), OUT))
    for r in out['buy']:
        print('  [买点] %s %s 第3根(%s) 收盘 %.2f' % (r['name'], r['code'], r['bar'], r['close']))
    for r in out['watch']:
        print('  [观察] %s %s 第%d根(%s) 收盘 %.2f' % (r['name'], r['code'], r['yrun'], r['bar'], r['close']))
    for r in out['hold']:
        print('  [持仓] %s %s %s 买入 %.2f，持有 %d 日，浮盈 %+.2f%%'
              % (r['name'], r['code'], r['hold']['entry_date'], r['hold']['entry'],
                 r['hold']['days'], r['hold']['ret']))


if __name__ == '__main__':
    main()
