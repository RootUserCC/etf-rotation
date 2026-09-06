#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
5分钟做T复盘演练工具（159552 单腿，T+1 规则内建）

用法：
  python practice.py              # 随机抽一天
  python practice.py --date 2026-08-19   # 指定某天（如专练暴跌日）
  python practice.py --seed 42    # 固定随机种子（可复现）

操作（每根K线显示后输入）：
  回车 = 下一根    s = 卖出一半底仓    b = 现金接回满仓
  q    = 结束当天  h = 帮助
规则与费用：只能卖隔夜底仓（T+1），买卖各扣万1；14:30 未接回强提醒。
当天结束后结算：你的收益 vs 持有不动 vs 当日理论最优（DP 天花板），
并追加战绩到 practice_log.csv。
"""
import sys
import io
import os
import argparse
import random
import datetime as dt

import numpy as np
import pandas as pd

# pythonw（无窗口计划任务）下 sys.stdout 为 None，不能 reconfigure
if sys.platform == 'win32' and sys.stdout is not None and not getattr(sys.stdout, '_utf8_wrapped', False):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stdout._utf8_wrapped = True

from t_pattern_long import DATA_DIR
from t_pattern_t1 import load_5min
from t_pattern import intraday_vwap
from t_ideal import ema
from t_optimal import optimal_t

FEE = 0.0001              # 单边万1（免5账户）
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'practice_log.csv')


def prepare(code='159552'):
    """加载数据并计算无前视指标（VWAP 仅当日；MACD 及买卖点与主站方案C
    同口径：EMA 通达信算法、17/34/9、sell_anywhere，全部历史预热）"""
    from backtest import calc_signals
    m = load_5min(code)
    close = m['close']
    sig = calc_signals(close, sell_anywhere=True, fast=17, slow=34, sig_n=9)
    m = m.assign(dif=sig['dif'], dea=sig['dea'], hist=sig['macdval'],
                 buy_sig=sig['buy_sig'].astype(int),
                 sell_sig=sig['sell_sig'].astype(int),
                 vwap=intraday_vwap(close))
    return m


def show_bar(t, row, day_open, pos_desc, cash, shares_total, price):
    """打印当前K线的"当时可见"信息"""
    chg = (price / day_open - 1) * 100
    dev = (price / row['vwap'] - 1) * 100
    print('%s  价 %.3f  日涨跌 %+.2f%%  偏离VWAP %+.2f%%  MACD柱 %+.4f  量 %.0f'
          % (t.strftime('%H:%M'), price, chg, dev, row['hist'], row['vol']))
    print('        仓位: %s | 现金 %.3f | 总市值 %.3f' % (pos_desc, cash, shares_total * price + cash))


def run_day(day_df, day_date):
    """演练一个交易日，返回结算 dict"""
    px = day_df['close'].values
    times = day_df.index
    n = len(px)
    day_open = day_df['open'].iloc[0]

    sh_old = 1.0 / px[0]     # 隔夜底仓（可卖）
    sh_new = 0.0             # 当日买入（锁定）
    cash = 0.0
    trips = []               # (卖时,卖价,买时,买价)
    pending = None
    ops = []

    print('=' * 64)
    print('演练日: %s（共 %d 根 5分钟K线）  开盘价 %.3f' % (day_date, n, day_open))
    print('输入: 回车=下一根  s=卖出一半底仓  b=接回满仓  q=结束  h=帮助')
    print('=' * 64)

    i = 0
    quit_early = False
    while i < n:
        t = times[i]
        price = px[i]
        total_sh = sh_old + sh_new
        pos_desc = '满仓' if cash < 1e-9 else '半仓(现金等待接回)'
        show_bar(t, day_df.iloc[i], day_open, pos_desc, cash, total_sh, price)

        # 14:30 纪律提醒
        if t.strftime('%H:%M') >= '14:30' and cash > 1e-9:
            print('        ⚠ 已过14:30，按纪律应立即接回满仓！')

        try:
            cmd = input('> ').strip().lower()
        except EOFError:
            cmd = 'q'
        if cmd == 'q':
            quit_early = True
            break
        elif cmd == 'h':
            print('  s=卖出一半隔夜底仓(仅可卖部分)  b=用现金接回  回车=下一根  q=结束')
            continue
        elif cmd == 's':
            sell_sh = total_sh * 0.5 - sh_new * 0.0   # 目标卖出一半总仓位
            sell_sh = min(sell_sh, sh_old)            # 只能卖隔夜可卖部分
            if sell_sh <= 1e-12:
                print('        ✗ 没有可卖的隔夜底仓（当日买入部分已锁定）')
                continue
            sh_old -= sell_sh
            cash += sell_sh * price * (1 - FEE)
            pending = (t, price)
            ops.append('%s 卖@%.3f' % (t.strftime('%H:%M'), price))
            print('        ✓ 卖出 %.4f 份 @ %.3f' % (sell_sh, price))
        elif cmd == 'b':
            if cash <= 1e-9:
                print('        ✗ 没有现金可接回')
                continue
            buy_val = cash * (1 - FEE)
            sh_new += buy_val / price
            if pending is not None:
                trips.append((pending[0], pending[1], t, price))
                pending = None
            ops.append('%s 买@%.3f' % (t.strftime('%H:%M'), price))
            print('        ✓ 接回 @ %.3f' % price)
            cash = 0.0
        elif cmd == '':
            i += 1
        else:
            print('        ? 无效输入（h 查看帮助）')

    # ---- 结算 ----
    final_px = px[min(i, n - 1)]
    user_ret = (sh_old + sh_new) * final_px + cash - 1.0
    hold_ret = final_px / px[0] - 1.0
    opt_ret, _ = optimal_t(day_df['close'])
    # 得分：最优空间>0时为 user/最优，最优≤0时看是否跑赢持有
    if opt_ret > 1e-6:
        score = max(user_ret, 0.0) / opt_ret * 100
        score_desc = '%.0f 分（吃到最优空间的 %.0f%%）' % (score, score)
    else:
        score_desc = '当日无T空间，%s' % ('守住了' if user_ret >= hold_ret - 1e-9 else '倒亏了')

    print()
    print('-' * 64)
    print('当日结算:')
    print('  你的操作: %s' % ('; '.join(ops) if ops else '（无操作）'))
    for st, sp, bt, bp in trips:
        print('    回合: %s %.3f → %s %.3f  净 %+0.2f%%'
              % (st.strftime('%H:%M'), sp, bt.strftime('%H:%M'), bp, (sp / bp - 1) * 100))
    print('  你的收益:  %+.2f%%' % (user_ret * 100))
    print('  持有不动:  %+.2f%%' % (hold_ret * 100))
    print('  当日最优:  %+.2f%%（后见之明天花板）' % (opt_ret * 100))
    print('  评分:      %s' % score_desc)

    return {
        'date': str(day_date),
        'ops': '|'.join(ops),
        'trips': len(trips),
        'user_ret': round(user_ret * 100, 3),
        'hold_ret': round(hold_ret * 100, 3),
        'optimal_ret': round(opt_ret * 100, 3),
        'quit_early': quit_early,
    }


def append_log(rec):
    df = pd.DataFrame([rec])
    header = not os.path.exists(LOG_FILE)
    df.to_csv(LOG_FILE, mode='a', header=header, index=False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', help='指定交易日 YYYY-MM-DD')
    ap.add_argument('--seed', type=int, default=None, help='随机种子')
    args = ap.parse_args()

    m = prepare()
    day_list = sorted(set(m.index.date))
    if args.seed is not None:
        random.seed(args.seed)

    print('数据区间: %s ~ %s，共 %d 个交易日' % (day_list[0], day_list[-1], len(day_list)))
    if args.date:
        day = dt.datetime.strptime(args.date, '%Y-%m-%d').date()
        if day not in day_list:
            print('该日无数据'); return
    else:
        day = random.choice(day_list)

    while True:
        day_df = m[m.index.date == day]
        rec = run_day(day_df, day)
        append_log(rec)
        again = input('\n回车再练一天（随机），或输入 q 退出: ').strip().lower()
        if again == 'q':
            break
        day = random.choice(day_list)
    print('战绩已写入 practice_log.csv，练满60个回合后可做汇总对比。')


if __name__ == '__main__':
    main()
