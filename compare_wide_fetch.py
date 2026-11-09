#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扩展标的池日线抓取：宽基 / 行业 / 跨境 / 商品 / 债券 ETF。

- 数据源：腾讯 newfqkline（后复权），与现有 data/etf_*_hfq.csv 同一口径
- 已存在文件走增量合并（历史不动），新标的全量回补
- 输出：data/wide_universe.json（代码/名称/起止/行数/可用性）
"""
import json
import os
import sys
import io
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

import market_data as md

if sys.platform == 'win32' and sys.stdout is not None:
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, 'data')

# (code, name, market, group)
UNIVERSE = [
    # 宽基
    ('510050', '上证50ETF', 1, 'broad'),
    ('510300', '沪深300ETF', 1, 'broad'),
    ('510500', '中证500ETF', 1, 'broad'),
    ('512100', '中证1000ETF', 1, 'broad'),
    ('159915', '创业板ETF', 0, 'broad'),
    ('159949', '创业板50ETF', 0, 'broad'),
    ('588000', '科创50ETF', 1, 'broad'),
    ('588220', '科创100ETF', 1, 'broad'),
    ('159552', '中证2000增强', 0, 'broad'),
    ('510880', '红利ETF', 1, 'broad'),
    ('512890', '红利低波ETF', 1, 'broad'),
    ('159901', '深证100ETF', 0, 'broad'),
    ('159919', '沪深300ETF(深)', 0, 'broad'),
    # 行业/主题
    ('512480', '半导体ETF', 1, 'sector'),
    ('512660', '军工ETF', 1, 'sector'),
    ('512880', '证券ETF', 1, 'sector'),
    ('512800', '银行ETF', 1, 'sector'),
    ('512000', '券商ETF', 1, 'sector'),
    ('512010', '医药ETF', 1, 'sector'),
    ('512170', '医疗ETF', 1, 'sector'),
    ('512690', '酒ETF', 1, 'sector'),
    ('512400', '有色ETF', 1, 'sector'),
    ('515220', '煤炭ETF', 1, 'sector'),
    ('515030', '新能源车ETF', 1, 'sector'),
    ('515790', '光伏ETF', 1, 'sector'),
    ('516160', '新能源ETF', 1, 'sector'),
    ('159869', '游戏ETF', 0, 'sector'),
    ('512980', '传媒ETF', 1, 'sector'),
    ('159928', '消费ETF', 0, 'sector'),
    ('515000', '科技ETF', 1, 'sector'),
    ('512580', '环保ETF', 1, 'sector'),
    ('515880', '通信ETF', 1, 'sector'),
    ('513050', '中概互联ETF', 1, 'overseas'),
    ('513100', '纳指ETF', 1, 'overseas'),
    ('513500', '标普500ETF', 1, 'overseas'),
    ('159941', '纳指ETF(深)', 0, 'overseas'),
    ('518880', '黄金ETF', 1, 'commodity'),
    ('511260', '十年国债ETF', 1, 'bond'),
    ('511010', '国债ETF', 1, 'bond'),
]


def sync(code, market):
    path = os.path.join(DATA, 'etf_%s_hfq.csv' % code)
    try:
        new = md.tx_daily(code, market, 'hfq', n=320)
        if os.path.exists(path):
            old = md.read_price_csv(path)
            try:
                out = md.merge_append(old, new, label=code)
            except ValueError:
                out = md.merge_append(old, md.tx_history(code, market, 'hfq'), label=code)
        else:
            out = md.tx_history(code, market, 'hfq', start_year=2015)
        md.write_price_csv(out, path)
        return code, True, len(out), str(out.index[0].date()), str(out.index[-1].date()), ''
    except Exception as e:  # noqa: BLE001
        return code, False, 0, '', '', repr(e)[:90]


def main():
    meta = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(sync, c, m): (c, n, m, g) for c, n, m, g in UNIVERSE}
        for fut in as_completed(futs):
            code, name, market, group = futs[fut]
            c, ok, rows, first, last, err = fut.result()
            meta.append({'code': code, 'name': name, 'market': market, 'group': group,
                         'ok': ok, 'rows': rows, 'first': first, 'last': last, 'error': err})
            print('%-6s %-14s %-9s %5d 行  %s ~ %s %s' %
                  (code, name, group, rows, first, last, ('ERR ' + err) if err else ''))
    meta.sort(key=lambda x: (x['group'], x['code']))
    with open(os.path.join(DATA, 'wide_universe.json'), 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)
    ok = [m for m in meta if m['ok'] and m['rows'] > 500]
    print('\n可用（>500 行）：%d / %d' % (len(ok), len(meta)))
    for m in meta:
        if not m['ok']:
            print('  不可用:', m['code'], m['name'], m['error'])


if __name__ == '__main__':
    main()
