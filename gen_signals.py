#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每日扫描自选股池 MACD 四色柱状态，导出 site/ai_signals.json 供网页端提醒。

提醒口径（与 compare_ymix.py 放宽版一致）：
  从绿转黄（第 1 根黄柱）开始计数，之后柱不回落即可（翻红不重置，变绿/回落作废），
  计到第 4 根 → 买点提醒（state=buy）；第 1~3 根 → 观察名单（state=watch）。

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
        board.append({
            'code': code, 'name': name,
            'date': str(df.index[-1].date()),
            'close': round(float(df['close'].iloc[-1]), 2),
            'bar': bar_color(h, h1),
            'yrun': y,                     # 放宽计数（黄起、可翻红）
            'srun': srun,                  # 严格黄柱计数
            'state': 'buy' if y == 4 else ('watch' if 1 <= y <= 3 else ''),
        })
        if (k + 1) % 10 == 0:
            print('  已扫描 %d/%d ...' % (k + 1, len(stocks)))
    api.disconnect()

    out = {
        'updated': str(pd.Timestamp.now()),
        'buy': [r for r in board if r['state'] == 'buy'],
        'watch': [r for r in board if r['state'] == 'watch'],
        'board': board,
    }
    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print('买点 %d 只，观察 %d 只，已导出 %s' % (len(out['buy']), len(out['watch']), OUT))
    for r in out['buy']:
        print('  [买点] %s %s 第4根(%s) 收盘 %.2f' % (r['name'], r['code'], r['bar'], r['close']))
    for r in out['watch']:
        print('  [观察] %s %s 第%d根(%s) 收盘 %.2f' % (r['name'], r['code'], r['yrun'], r['bar'], r['close']))


if __name__ == '__main__':
    main()
