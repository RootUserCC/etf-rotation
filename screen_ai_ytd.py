#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫描 AI 相关概念板块成分股，按 2026 年以来涨幅排序，找出池外强势标的。

概念板块成分来自东方财富 push2 接口；YTD 用 pytdx 日线计算。
输出 data/ai_ytd_screen.csv，并打印 TOP 40（POOL = 已在信号池）。
"""
import os
import sys

import pandas as pd
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fetch_data as fd
import ai_yellow_bar as ay

# AI 相关概念板块关键词（东财概念板块名）
KEYWORDS = ['CPO', '光模块', '光通信', '算力', '液冷', 'PCB', '铜缆', '存储',
            '先进封装', '人工智能', 'AIGC', 'ChatGPT', '英伟达', '数据中心',
            '东数西算', '云计算', '边缘计算', '服务器', '昇腾', 'Sora', '光芯片',
            '硅光', '国资云', 'AI']

HEADERS = {'User-Agent': 'Mozilla/5.0'}
CLIST = 'https://push2.eastmoney.com/api/qt/clist/get'


def em_clist(fs, pz=500):
    """东财板块/成分列表：返回 [{'code':..., 'name':...}]"""
    r = requests.get(CLIST, headers=HEADERS, timeout=10, params={
        'pn': 1, 'pz': pz, 'po': 1, 'np': 1, 'fltt': 2, 'invt': 2,
        'fid': 'f3', 'fs': fs, 'fields': 'f12,f14'})
    data = r.json().get('data') or {}
    return [{'code': d.get('f12'), 'name': d.get('f14')}
            for d in (data.get('diff') or [])]


def main():
    # ---- 1. 找 AI 相关概念板块 ----
    boards = em_clist('m:90+t:3')          # 东财概念板块
    hits = [b for b in boards if b['name'] and any(k in b['name'] for k in KEYWORDS)]
    print('命中板块 %d 个: %s' % (len(hits), '、'.join(b['name'] for b in hits)))

    # ---- 2. 拉成分股 ----
    pool_codes = {code for code, _, _ in ay.load_stock_list()}
    cand = {}
    for b in hits:
        for s in em_clist('b:%s' % b['code']):
            c, n = s['code'], s['name'] or ''
            if not c or len(c) != 6 or 'ST' in n or '退' in n:
                continue
            if c[:3] in ('000', '001', '002', '003', '300', '301',
                         '600', '601', '603', '605'):          # 剔除 688 科创板/ETF
                cand[c] = n
    print('候选成分股 %d 只（去重、剔除 ST/688）' % len(cand))

    # ---- 3. pytdx 日线算 YTD ----
    from pytdx.hq import TdxHq_API
    api = TdxHq_API()
    fd._connect_hq(api)
    rows = []
    codes = sorted(cand)
    for k, code in enumerate(codes):
        try:
            bars = fd._fetch_bars(api, code, 260, ay.market_of(code))
            if bars:
                df = fd._bars_to_df(bars, ['close'])
                closes = df['close']
                base = closes[closes.index <= pd.Timestamp('2025-12-31')]
                if len(base):
                    ytd = closes.iloc[-1] / base.iloc[-1] - 1
                    rows.append((code, cand[code], ytd, float(closes.iloc[-1]),
                                 code in pool_codes))
        except Exception:
            pass
        if (k + 1) % 50 == 0:
            print('  已计算 %d/%d ...' % (k + 1, len(codes)))
    api.disconnect()

    df = pd.DataFrame(rows, columns=['code', 'name', 'ytd', 'close', 'in_pool'])
    df = df.sort_values('ytd', ascending=False)
    out = os.path.join(ay.ROOT, 'data', 'ai_ytd_screen.csv')
    df.to_csv(out, index=False, encoding='utf-8-sig')

    print('\n===== 2026 年 YTD 涨幅 TOP 40（AI 概念成分，46 只池外重点看） =====')
    print('%-8s %-8s %8s %9s  %s' % ('代码', '名称', 'YTD', '收盘', '备注'))
    for _, r in df.head(40).iterrows():
        print('%-8s %-8s %+7.1f%% %9.2f  %s'
              % (r['code'], r['name'], r['ytd'] * 100, r['close'],
                 'POOL' if r['in_pool'] else ''))
    print('\n全部 %d 只已存 %s' % (len(df), out))


if __name__ == '__main__':
    main()
