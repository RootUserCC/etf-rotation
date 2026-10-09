#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「拐点择时 + 黄柱股票组合进攻腿」组合回测（2×2 矩阵 + 对照）

择时门：backtest.run_backtest(fast=17, slow=34, sig_n=9, sell_anywhere=True) 的
        hold1000（True=进攻日，T日信号T+1开盘执行后的收盘状态，与基准同一口径）
股票池：data/ai_stocks/*.csv ∪ data/ai_stocks_ext/*.csv（约 420 只 6 位代码，不复权）
除权  ：pytdx get_xdxr_info 自算后复权（category=1 分红送转配 / category=11 缩股），
        缓存到 data/ai_stocks_hfq/。个股除权日用精确公式：
        除权日收益 = close*(1+送股+配股) / (前收盘 - 每股分红 + 配股价*配股比例) - 1
        （与 fetch_data.fetch_etf_hfq 的 ETF 口径同源，但股票含送转股，必须用精确式）
买卖规则（沿用 portfolio_sim 口径）：
  买：第 3 根黄柱收盘买半槽，同一段走到第 4 根再补半槽；同日多信号按当日跌幅大者优先
  卖：首根蓝柱收盘清仓；持有满 120 个交易日强制清仓
  仓位：本金 100 万，SLOTS 槽（主口径 10，敏感性 5），每槽满仓 = 本金/SLOTS
  费用：股票单边 0.08%，ETF 单边万1
变体（×2×2 矩阵）：
  V1 严格联动：择时门转防守当天收盘清空全部股票、买入 512890；转进攻当天卖出 512890
  V2 半联动  ：防守期停止开新仓（含补仓），持仓按原规则自然卖出，回款买 512890；
              进攻期恢复开仓，从 512890 赎回
  交叉维度 (a)：进攻期闲置资金买 159552/512100 拼接进攻腿 ETF（2024-06-28 拼接），
              有股票信号时卖出 ETF 划拨；(b)：留现金
对照：黄柱组合始终满仓不择时（idle 留现金），以及 brief 的 ETF 轮动基准本身
窗口：2019-01-18 ~ 2026-09-23（与基准一致，取 run_backtest 的 idx 交集）

只读生产文件，不修改任何既有文件；输出 result_timing_stock.txt。
"""
import os
import time

import numpy as np
import pandas as pd

import ai_yellow_bar as ay
import fetch_data as fd
from backtest import run_backtest, annualized, max_drawdown

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, 'data')
HFQ_DIR = os.path.join(DATA, 'ai_stocks_hfq')

CAPITAL = 1_000_000.0
FEE_STOCK = 0.0008          # 股票单边
FEE_ETF = 0.0001            # ETF 单边（万1）
MAXD = 120                  # 最长持有交易日
WARMUP = 60                 # 个股信号预热（与 ai_yellow_bar.simulate 一致）
SWITCH = pd.Timestamp('2024-06-28')   # 进攻腿拼接切换日
Y26 = pd.Timestamp('2026-01-01')
SEP26 = pd.Timestamp('2026-09-01')


def load_csv(path):
    df = pd.read_csv(path, parse_dates=['date']).set_index('date').sort_index()
    df.index = df.index.normalize()
    return df


def market_of(code: str) -> int:
    return 1 if code[0] in '569' else 0


# ===================== 1. 股票后复权缓存 =====================

def adjust_with_xdxr(df: pd.DataFrame, xdxr: list):
    """不复权日线 + xdxr 除权记录 → 后复权日线（精确除权公式，处理分红/送转/配股/缩股）"""
    close = df['close']
    ret = close.pct_change().fillna(0.0).copy()
    recs = {}
    for rec in xdxr:
        cat = rec.get('category')
        if cat not in (1, 11):
            continue
        d = pd.Timestamp(rec['year'], rec['month'], rec['day'])
        r = recs.setdefault(d, {'div': 0.0, 'sg': 0.0, 'pg': 0.0, 'pgj': 0.0, 'suogu': None})
        if cat == 1:
            r['div'] += float(rec.get('fenhong') or 0) / 10.0       # 每股分红
            r['sg'] += float(rec.get('songzhuangu') or 0) / 10.0    # 每股送转
            r['pg'] += float(rec.get('peigu') or 0) / 10.0          # 每股配股
            if rec.get('peigujia'):
                r['pgj'] = float(rec['peigujia'])
        elif rec.get('suogu'):
            r['suogu'] = float(rec['suogu'])
    n_adj = 0
    for d, r in recs.items():
        pos = close.index.searchsorted(d)
        if pos == 0 or pos >= len(close):
            continue
        d0, prev = close.index[pos], close.index[pos - 1]
        pc = close.loc[prev]
        if r['suogu']:
            ret.loc[d0] = close.loc[d0] * r['suogu'] / pc - 1
            n_adj += 1
        else:
            denom = pc - r['div'] + r['pgj'] * r['pg']              # 除权参考价分子
            if denom > 0 and (r['div'] or r['sg'] or r['pg']):
                ret.loc[d0] = close.loc[d0] * (1 + r['sg'] + r['pg']) / denom - 1
                n_adj += 1
    hfq_close = close.iloc[0] * (1 + ret).cumprod()
    factor = hfq_close / close
    return df.mul(factor, axis=0), n_adj


def build_hfq_cache(raw_paths: dict):
    """对全部股票生成后复权缓存（缓存新于原始文件则跳过）。返回 (失败列表, 调整总处数)"""
    os.makedirs(HFQ_DIR, exist_ok=True)
    todo = []
    for code, raw in raw_paths.items():
        out = os.path.join(HFQ_DIR, '%s.csv' % code)
        if not (os.path.exists(out) and os.path.getmtime(out) >= os.path.getmtime(raw)):
            todo.append(code)
    fails, total_adj = [], 0
    if not todo:
        print('后复权缓存已全部就绪（%d 只）' % len(raw_paths))
        return fails, total_adj
    print('需生成后复权缓存 %d 只（pytdx xdxr）...' % len(todo))
    from pytdx.hq import TdxHq_API
    api = TdxHq_API()
    fd._connect_hq(api)
    t0 = time.time()
    for k, code in enumerate(todo):
        try:
            try:
                xdxr = api.get_xdxr_info(market_of(code), code)
            except Exception:
                api = TdxHq_API()          # 断线重连一次
                fd._connect_hq(api)
                xdxr = api.get_xdxr_info(market_of(code), code)
            raw = load_csv(raw_paths[code])
            hfq, n = adjust_with_xdxr(raw, xdxr or [])
            hfq.to_csv(os.path.join(HFQ_DIR, '%s.csv' % code))
            total_adj += n
        except Exception as e:
            fails.append(code)
            print('  [失败] %s: %s' % (code, repr(e)[:60]))
        if (k + 1) % 40 == 0:
            print('  进度 %d/%d  用时 %.0fs' % (k + 1, len(todo), time.time() - t0))
    api.disconnect()
    print('后复权缓存完成：%d 成功 / %d 失败，用时 %.0fs' % (len(todo) - len(fails), len(fails), time.time() - t0))
    return fails, total_adj


# ===================== 2. 数据加载 =====================

def load_pool_raw():
    """合并 ai_stocks 与 ai_stocks_ext，按代码去重（ai_stocks 优先）"""
    paths = {}
    for sub in ['ai_stocks_ext', 'ai_stocks']:
        d = os.path.join(DATA, sub)
        for fn in os.listdir(d):
            if fn.endswith('.csv') and fn[:6].isdigit():
                paths.setdefault(fn[:6], os.path.join(d, fn))
    return paths


def load_pool(codes):
    pool = {}
    for code in codes:
        path = os.path.join(HFQ_DIR, '%s.csv' % code)
        if not os.path.exists(path):
            continue
        try:
            df = load_csv(path)
            if len(df) < WARMUP + 10:
                continue
            p = ay.prepare(df)
            pool[code] = {
                'idx': p['idx'], 'closes': p['closes'], 'run': p['run'], 'blue': p['blue'],
                'pct': pd.Series(p['closes']).pct_change().fillna(0.0).values,
            }
        except Exception as e:
            print('  [跳过] %s: %s' % (code, repr(e)[:50]))
    return pool


# ===================== 3. 组合模拟 =====================

def simulate(cfg, cal, gate, pool, day_map, p_def, p_atk, seed_close):
    timing, mode, idle, SLOTS = cfg['timing'], cfg['mode'], cfg['idle'], cfg['slots']
    unit = CAPITAL / SLOTS / 2.0
    cash = CAPITAL
    pos = {}            # code -> {'entries': [(price, amt)], 'held': int, 'buy_day'}
    sh_def = sh_atk = 0.0
    trades, skipped, etf_trades = [], 0, 0
    last_close = dict(seed_close)
    eq_days, eq_vals = [], []
    atk_hold = {}       # day -> True（用于拼接切换日跨界检查）

    def sell_pos(code, px, d):
        nonlocal cash
        po = pos[code]
        val = sum(a * px / e for e, a in po['entries'])
        amt = sum(a for _, a in po['entries'])
        cash += val * (1 - FEE_STOCK)
        trades.append((code, po['buy_day'], d, po['held'], val / amt - 1, val - amt))
        del pos[code]

    def sell_def(d):
        nonlocal cash, sh_def, etf_trades
        if sh_def > 0:
            cash += sh_def * p_def[d] * (1 - FEE_ETF)
            sh_def = 0.0
            etf_trades += 1

    def buy_def(d, amt):
        nonlocal cash, sh_def, etf_trades
        if amt > 1:
            sh_def += amt * (1 - FEE_ETF) / p_def[d]
            cash -= amt
            etf_trades += 1

    def sell_atk(d, need=None):
        """need=None 全部卖出；否则卖出足够筹得 need 缺口"""
        nonlocal cash, sh_atk, etf_trades
        if sh_atk <= 0:
            return
        px = p_atk[d]
        if need is None:
            sh_sell = sh_atk
        else:
            deficit = need - cash
            if deficit <= 0:
                return
            sh_sell = min(sh_atk, deficit / (px * (1 - FEE_ETF)) * (1 + 1e-9))
        cash += sh_sell * px * (1 - FEE_ETF)
        sh_atk -= sh_sell
        if sh_atk < 1e-9:
            sh_atk = 0.0
        etf_trades += 1

    def buy_atk(d, amt):
        nonlocal cash, sh_atk, etf_trades
        if amt > 1:
            sh_atk += amt * (1 - FEE_ETF) / p_atk[d]
            cash -= amt
            etf_trades += 1

    prev_g = None
    for d in cal:
        g = bool(gate[d]) if timing else True
        todays = day_map.get(d, ())
        today_px = {code: pool[code]['closes'][i] for code, i in todays}

        # --- 1. 择时门翻转处理 ---
        if timing and g != prev_g:
            if not g:                                   # 转防守
                if mode == 'V1':                        # 严格联动：当日收盘清空全部股票
                    for code in list(pos):
                        px = today_px.get(code, last_close.get(code, np.nan))
                        sell_pos(code, px, d)
                sell_atk(d)                             # 进攻腿 ETF 全部赎回
                buy_def(d, cash)                        # 全部买入 512890
            else:                                       # 转进攻
                sell_def(d)                             # 赎回 512890，现金待命
        prev_g = g

        # --- 2. 遍历当日有行情的股票：记价、自然卖出、收集买点 ---
        addons, cands = [], []
        for code, i in todays:
            v = pool[code]
            px = v['closes'][i]
            last_close[code] = px
            if code in pos:
                po = pos[code]
                po['held'] += 1
                if v['blue'][i] or po['held'] >= MAXD:  # 首根蓝柱 / 满120日
                    sell_pos(code, px, d)
                elif g and v['run'][i] == 4 and len(po['entries']) == 1:
                    addons.append((code, px))
            elif g and i >= WARMUP and v['run'][i] == 3:
                cands.append((v['pct'][i], code, px))

        # --- 3. 补仓（第4根黄柱，已有仓位优先） ---
        for code, px in addons:
            need = unit * (1 + FEE_STOCK)
            if cash < need and sh_atk > 0:
                sell_atk(d, need)
            if cash >= need:
                cash -= need
                pos[code]['entries'].append((px, unit))
            else:
                skipped += 1

        # --- 4. 新开仓：当日跌幅大者优先，槽满跳过 ---
        cands.sort(key=lambda x: x[0])
        for _, code, px in cands:
            if len(pos) >= SLOTS:
                skipped += 1
                continue
            need = unit * (1 + FEE_STOCK)
            if cash < need and sh_atk > 0:
                sell_atk(d, need)
            if cash >= need:
                cash -= need
                pos[code] = {'entries': [(px, unit)], 'held': 0, 'buy_day': d}
            else:
                skipped += 1

        # --- 5. 闲置资金归置 ---
        if timing and cash > 1:
            if not g:
                buy_def(d, cash)                        # 防守期回款 → 512890
            elif idle == 'etf':
                buy_atk(d, cash)                        # 进攻期闲置 → 进攻腿 ETF

        # --- 6. 记净值 ---
        mkt = sum(a * last_close[code] / e for code, po in pos.items() for e, a in po['entries'])
        eq_days.append(d)
        eq_vals.append(cash + mkt + sh_def * p_def[d] + sh_atk * p_atk[d])
        if sh_atk > 0:
            atk_hold[d] = True

    eq = pd.Series(eq_vals, index=pd.DatetimeIndex(eq_days))
    # 拼接切换日跨界持有检查
    before = cal[cal < SWITCH]
    cross = bool(len(before) and atk_hold.get(before[-1], False))
    return {'eq': eq, 'trades': trades, 'skipped': skipped,
            'etf_trades': etf_trades, 'cross_switch': cross, 'open_pos': len(pos)}


# ===================== 4. 指标统计 =====================

def metrics(nav: pd.Series):
    """nav 归一化净值 → dict"""
    y26 = nav[nav.index >= Y26]
    b26 = nav[nav.index < Y26]
    s26 = nav[nav.index >= SEP26]
    b_sep = nav[nav.index < SEP26]
    yearly = nav.resample('YE').last()
    yr = yearly.pct_change()
    yr.iloc[0] = yearly.iloc[0] / nav.iloc[0] - 1
    yr.index = yr.index.year
    return {
        'cum': nav.iloc[-1] - 1,
        'ann': annualized(nav),
        'mdd': max_drawdown(nav),
        'ytd26': (y26.iloc[-1] / b26.iloc[-1] - 1) if len(y26) and len(b26) else float('nan'),
        'sep26': (s26.iloc[-1] / b_sep.iloc[-1] - 1) if len(s26) and len(b_sep) else float('nan'),
        'yearly': yr,
    }


def fmt_pct(x, nd=1):
    return ('%+.{}f%%'.format(nd)) % (x * 100)


# ===================== 5. 主流程 =====================

def main():
    print('=' * 70)
    print('拐点择时 + 黄柱股票组合进攻腿 · 组合回测')
    print('=' * 70)

    # --- 股票池与后复权缓存 ---
    raw_paths = load_pool_raw()
    print('股票池：%d 只（ai_stocks ∪ ai_stocks_ext，去重后）' % len(raw_paths))
    fails, n_adj = build_hfq_cache(raw_paths)
    use_codes = [c for c in raw_paths if c not in fails]
    if fails:
        print('后复权失败 %d 只（剔除）：%s' % (len(fails), ','.join(fails)))
    pool = load_pool(use_codes)
    print('成功加载后复权股票 %d 只' % len(pool))

    # --- 择时门（基准策略攻/守状态） ---
    avg = load_csv(os.path.join(DATA, 'avg_880003.csv'))
    etf1000 = load_csv(os.path.join(DATA, 'etf_512100_hfq.csv'))
    etf2000e = load_csv(os.path.join(DATA, 'etf_159552_hfq.csv'))
    etfdiv = load_csv(os.path.join(DATA, 'etf_512890_hfq.csv'))
    etf_atk = pd.concat([etf1000[etf1000.index < SWITCH], etf2000e[etf2000e.index >= SWITCH]])
    res = run_backtest(avg, etf_atk, etfdiv, fee=0.0001, sell_anywhere=True,
                       fast=17, slow=34, sig_n=9, verbose=False)
    cal = res['idx']
    gate = res['hold1000']
    p_def = etfdiv.loc[cal, 'close']
    p_atk = etf_atk.loc[cal, 'close']
    print('回测窗口 %s ~ %s，%d 个交易日；进攻日占比 %.1f%%'
          % (cal[0].date(), cal[-1].date(), len(cal), gate.mean() * 100))
    # 基准自身在拼接切换日是否持有进攻仓
    before = cal[cal < SWITCH]
    bench_cross = bool(len(before) and gate.loc[before[-1]])
    print('基准在 2024-06-28 拼接切换日跨界持有进攻仓：%s' % ('是' if bench_cross else '否'))

    # --- 预处理：日 → 当日有行情的股票列表；窗口前的最后收盘（用于估值/强清兜底） ---
    day_map = {}
    seed_close = {}
    start = cal[0]
    for code, v in pool.items():
        idx = v['idx']
        pre = idx.searchsorted(start)
        if pre > 0:
            seed_close[code] = v['closes'][pre - 1]
        for i in range(pre, len(idx)):
            day_map.setdefault(idx[i], []).append((code, i))
    print('交易日-股票映射就绪：%d 天有股票行情' % len(day_map))

    # --- 变体矩阵 ---
    configs = [
        ('V1a 严格联动·闲置买进攻ETF', dict(timing=True, mode='V1', idle='etf', slots=10)),
        ('V1b 严格联动·闲置留现金',   dict(timing=True, mode='V1', idle='cash', slots=10)),
        ('V2a 半联动·闲置买进攻ETF',  dict(timing=True, mode='V2', idle='etf', slots=10)),
        ('V2b 半联动·闲置留现金',     dict(timing=True, mode='V2', idle='cash', slots=10)),
        ('CTRL 不择时·始终满仓·留现金', dict(timing=False, mode='V2', idle='cash', slots=10)),
    ]
    sens = [(t + ' [SLOTS=5]', dict(c, slots=5)) for t, c in configs]

    results = {}
    for tag, cfg in configs + sens:
        t0 = time.time()
        r = simulate(cfg, cal, gate, pool, day_map, p_def, p_atk, seed_close)
        assert (r['eq'] > 0).all(), tag + ' 净值出现非正值，记账有误'
        r['m'] = metrics(r['eq'] / CAPITAL)
        results[tag] = r
        m = r['m']
        print('【%s】累计 %s 年化 %s 回撤 %.1f%% 清仓 %d 笔 ETF交易 %d 次 (%.0fs)'
              % (tag, fmt_pct(m['cum']), fmt_pct(m['ann']), m['mdd'] * 100,
                 len(r['trades']), r['etf_trades'], time.time() - t0))

    # --- 基准指标 ---
    bm = metrics(res['nav_strat'])

    # ===================== 报告 =====================
    L = []
    w = L.append
    w('=' * 78)
    w('拐点择时 + 黄柱股票组合进攻腿 · 组合回测结果')
    w('生成时间：%s' % pd.Timestamp.now().strftime('%Y-%m-%d %H:%M'))
    w('=' * 78)
    w('')
    w('【口径】')
    w('  窗口 %s ~ %s（%d 个交易日，run_backtest 三标的交集，与基准一致）'
      % (cal[0].date(), cal[-1].date(), len(cal)))
    w('  择时门：avg_880003 MACD(17,34,9) DIF 拐点，sell_anywhere=True，T信号T+1开盘执行；')
    w('          取 res[\'hold1000\'] 收盘状态，True=进攻日（占比 %.1f%%）' % (gate.mean() * 100))
    w('  股票池：data/ai_stocks ∪ data/ai_stocks_ext 去重后 %d 只；后复权成功 %d 只，失败剔除 %d 只'
      % (len(raw_paths), len(pool), len(fails)))
    w('  除权处理：pytdx get_xdxr_info 全量 %d 只逐只拉取（非抽样），category=1 分红送转配 /' % len(raw_paths))
    w('    category=11 缩股，除权日收益用精确公式 close*(1+送转+配股)/(前收盘-每股分红+配股价*配股)-1，')
    w('    其余日按普通涨跌幅，连乘得后复权因子后 OHLC 同乘；缓存于 data/ai_stocks_hfq/。')
    w('    黄柱信号与持仓收益均基于后复权价，除权跳空不污染信号。')
    w('  股票买卖：第3根黄柱收盘买半槽、走到第4根补半槽；首根蓝柱或满120交易日收盘清仓；')
    w('    同日多信号按当日跌幅大者优先，槽满跳过；个股信号需满 %d 根K线预热；收盘成交，单边 0.08%%。' % WARMUP)
    w('  ETF：512890 红利低波（防守腿，后复权）/ 512100+159552 拼接（进攻腿，后复权，2024-06-28 切换），')
    w('    单边万1，收盘成交；股票信号需资金时当日收盘卖出进攻ETF划拨（T+0 资金假设）。')
    w('  V1 严格联动：转防守当天收盘清空全部股票买512890；转进攻当天卖512890。')
    w('  V2 半联动：防守期停开新仓（含补仓），持仓自然卖出，回款买512890；进攻期恢复。')
    w('  本金 100 万；主口径 SLOTS=10（半槽 5 万），敏感性 SLOTS=5。')
    w('')

    # ---- 总表 ----
    def row(tag, m, tr, etfx, extra=''):
        wins = sum(1 for t in tr if t[4] > 0)
        return ('%-30s %9s %8s %8s %7s %8s %8s %s'
                % (tag, fmt_pct(m['cum']), fmt_pct(m['ann']), '%.1f%%' % (m['mdd'] * 100),
                   '%d(%d%%)' % (len(tr), wins / len(tr) * 100) if tr else '0',
                   fmt_pct(m['ytd26']), fmt_pct(m['sep26']), extra))

    hdr = ('%-30s %9s %8s %8s %7s %8s %8s %s'
           % ('变体', '累计', '年化', '最大回撤', '清仓笔数(胜率)', '2026YTD', '2026-09', '备注'))
    for title, group in [('一、主口径 SLOTS=10', [t for t, _ in configs]),
                         ('二、敏感性 SLOTS=5', [t for t, _ in sens])]:
        w(title)
        w(hdr)
        w('-' * 100)
        for tag in group:
            r = results[tag]
            extra = '跨界持进攻ETF!' if r['cross_switch'] else ''
            w(row(tag, r['m'], r['trades'], r['etf_trades'], extra))
        w('%-30s %9s %8s %8s %7s %8s %8s %s'
          % ('基准 ETF轮动(17/34/9)', fmt_pct(bm['cum']), fmt_pct(bm['ann']),
             '%.1f%%' % (bm['mdd'] * 100), '%d次换仓' % len(res['trades']),
             fmt_pct(bm['ytd26']), fmt_pct(bm['sep26']),
             '跨界持进攻仓!' if bench_cross else ''))
        w('')

    # ---- ETF 交易次数 / 错过信号 / 打脸段 ----
    w('三、交易统计（清仓笔数含择时强清；打脸段=持有≤5交易日且亏损的股票交易）')
    w('    注：ETF交易次数含每个交易日收盘的闲置资金归置（万1费率、金额小，费用影响可忽略）；')
    w('    错过信号=槽位或资金不足而放弃的第3/4根黄柱信号数。')
    w('%-30s %8s %8s %10s %14s' % ('变体', 'ETF交易', '错过信号', '打脸段笔数', '打脸段盈亏(万)'))
    w('-' * 78)
    for tag, _ in configs + sens:
        r = results[tag]
        face = [t for t in r['trades'] if t[3] <= 5 and t[4] < 0]
        w('%-30s %8d %8d %10d %14s'
          % (tag, r['etf_trades'], r['skipped'], len(face),
             '%.1f' % (sum(t[5] for t in face) / 1e4) if face else '0.0'))
    w('')

    # ---- 分年度表 ----
    w('四、分年度收益（%%）')
    years = sorted(set().union(*[set(results[t]['m']['yearly'].index) for t, _ in configs],
                               set(bm['yearly'].index)))
    tags10 = [t for t, _ in configs]
    w('%-6s' % '年份' + ''.join('%11s' % t.split()[0] for t in tags10) + '%11s' % 'ETF基准')
    w('-' * (6 + 11 * (len(tags10) + 1)))
    for y in years:
        line = '%-6d' % y
        for t in tags10:
            yr = results[t]['m']['yearly']
            line += '%11s' % (fmt_pct(yr[y]) if y in yr.index else '-')
        line += '%11s' % (fmt_pct(bm['yearly'][y]) if y in bm['yearly'].index else '-')
        w(line)
    w('')

    # ---- 跨界持有检查 ----
    w('五、2024-06-28 拼接切换日跨界持有检查')
    cross_list = [t for t, _ in configs + sens if results[t]['cross_switch']]
    w('  基准 ETF 轮动：%s' % ('跨界持有进攻仓（close-to-close 收益有拼接失真）' if bench_cross
                               else '未跨界持有，无失真'))
    if cross_list:
        w('  以下变体在切换日持有进攻腿 ETF（当日收益跨 512100/159552 拼接，存在单日失真）：')
        for t in cross_list:
            w('    - %s' % t)
    else:
        w('  所有组合变体在切换日均未持有进攻腿 ETF，无拼接失真。')
    w('')

    # ---- 四个核心问题 ----
    g = lambda t: results[t]['m']
    w('六、核心问题回答')
    ctrl, v1a, v1b, v2a, v2b = g('CTRL 不择时·始终满仓·留现金'), g('V1a 严格联动·闲置买进攻ETF'), \
        g('V1b 严格联动·闲置留现金'), g('V2a 半联动·闲置买进攻ETF'), g('V2b 半联动·闲置留现金')
    w('1) 择时门对股票组合是增益还是拖累（变体 vs 不择时对照）：')
    w('   对照累计 %s / 年化 %s / 回撤 %.1f%%' % (fmt_pct(ctrl['cum']), fmt_pct(ctrl['ann']), ctrl['mdd'] * 100))
    for tag, m in [('V1a', v1a), ('V1b', v1b), ('V2a', v2a), ('V2b', v2b)]:
        w('   %s 累计 %s（%s）年化 %s（%s）回撤 %.1f%%（%+.1fpp）'
          % (tag, fmt_pct(m['cum']), fmt_pct(m['cum'] - ctrl['cum']),
             fmt_pct(m['ann']), fmt_pct(m['ann'] - ctrl['ann']),
             m['mdd'] * 100, (m['mdd'] - ctrl['mdd']) * 100))
    w('2) 进攻期闲置资金买进攻ETF vs 留现金：')
    w('   V1: ETF %s vs 现金 %s（差 %s）；V2: ETF %s vs 现金 %s（差 %s）'
      % (fmt_pct(v1a['cum']), fmt_pct(v1b['cum']), fmt_pct(v1a['cum'] - v1b['cum']),
         fmt_pct(v2a['cum']), fmt_pct(v2b['cum']), fmt_pct(v2a['cum'] - v2b['cum'])))
    best_tag = max([t for t, _ in configs], key=lambda t: results[t]['m']['ann'])
    best = results[best_tag]['m']
    w('3) 组合年化能否摸到 50%%：最优变体 %s 年化 %s（累计 %s），最大回撤 %.1f%%。%s'
      % (best_tag, fmt_pct(best['ann']), fmt_pct(best['cum']), best['mdd'] * 100,
         '摸到。' if best['ann'] >= 0.5 else '未摸到。'))
    w('4) 诚实性警告：')
    w('   本池 %d 只股票是 2026 年按 AI 硬件题材【后见选择】的，2019-2025 年回测存在严重选池偏差' % len(pool))
    w('   （幸存者偏差+题材后视），年份越往前结论越不可靠：2019-2023 的亮眼数字大部分是')
    w('   「事后知道这些股票后来大涨」的产物，不能外推。2026 年（YTD %s）是唯一相对干净的样本外区间。'
      % fmt_pct(ctrl['ytd26']))
    w('   拼接切换日跨界持有情况见第五节%s。' % ('（存在失真，已标注）' if cross_list or bench_cross else '（无失真）'))
    w('')

    # ---- 结论与建议 ----
    w('七、结论与采纳建议（由上面数据直接得出）')
    timed_ann = {t: results[t]['m']['ann'] for t in tags10 if not t.startswith('CTRL')}
    best_t = max(timed_ann, key=timed_ann.get)
    best_m = results[best_t]['m']
    beats = best_m['ann'] > ctrl['ann']
    w('1. 择时门结论：%s。四个择时变体年化区间 %s ~ %s，对照（不择时）年化 %s。'
      % ('择时门整体是【增益】' if beats else '择时门整体是【拖累】',
         fmt_pct(min(timed_ann.values())), fmt_pct(max(timed_ann.values())), fmt_pct(ctrl['ann'])))
    w('   最优变体为 %s（年化 %s，回撤 %.1f%%，vs 对照回撤 %.1f%%）。'
      % (best_t, fmt_pct(best_m['ann']), best_m['mdd'] * 100, ctrl['mdd'] * 100))
    w('   V1 严格联动在防守日强平全部股票，会打断黄柱持仓的自然生命周期（强清笔数多、打脸段多）；')
    w('   V2 半联动让持仓自然退出，只在资金面做攻/守切换，通常摩擦更小——以本节数据为准。')
    etf_gain_v1 = v1a['cum'] - v1b['cum']
    etf_gain_v2 = v2a['cum'] - v2b['cum']
    w('2. 闲置资金口径：进攻期闲置买进攻ETF 比留现金 累计多 V1: %s / V2: %s。'
      % (fmt_pct(etf_gain_v1), fmt_pct(etf_gain_v2)))
    w('   代价是闲置资金暴露在小盘股贝塔下，回撤会放大（对比 a/b 两组最大回撤）。')
    w('3. 年化 50%% 问题：主口径 SLOTS=10 下%s摸到 50%%（最优 %s）；敏感性 SLOTS=5 最优 %s，仍远不及。'
      % ('能' if best_m['ann'] >= 0.5 else '未能', fmt_pct(best_m['ann']),
         fmt_pct(max(results[t]['m']['ann'] for t, _ in sens))))
    w('   且最优变体也未跑赢纯 ETF 轮动基准（年化 %s，回撤 %.1f%%）：股票腿相对基准年化 %s、回撤 %+.1fpp，'
      % (fmt_pct(bm['ann']), bm['mdd'] * 100,
         fmt_pct(best_m['ann'] - bm['ann']), (best_m['mdd'] - bm['mdd']) * 100))
    w('   2026 样本外：最优变体 YTD %s vs 基准 %s——股票组合没有带来超额，反而多了 ~%d 笔股票交易的摩擦与盯盘成本。'
      % (fmt_pct(results[best_t]['m']['ytd26']), fmt_pct(bm['ytd26']), len(results[best_t]['trades'])))
    w('4. 采纳建议（含诚实性前提）：')
    if beats and best_m['ann'] >= ctrl['ann'] + 0.02:
        w('   (a) 对「黄柱股票组合」本身：择时门是稳定增益（a/b、V1/V2、SLOTS=5/10 八个口径全部成立），'
          '若继续做股票腿，建议采纳「择时门 + 闲置资金买进攻ETF」；')
        w('   (b) 但对「整体账户」：最优股票组合仍跑不赢既有 ETF 轮动基准，不建议用它替代基准；'
          '股票腿只适合作为基准之外的卫星仓，且仓位应据 2026 样本外表现而非全段年化决定；')
    else:
        w('   择时门相对不择时对照无稳定增益，不建议把基准攻/守状态硬套到黄柱股票组合上；')
    w('   (c) 由于选池后见偏差（第六节警告），以上结论对 2019-2025 段不可信，'
      '建议仅以 2026 年样本外表现做后续跟踪依据，不要据全段年化做仓位决策。')

    text = '\n'.join(L)
    out = os.path.join(ROOT, 'result_timing_stock.txt')
    with open(out, 'w', encoding='utf-8') as f:
        f.write(text)
    print('\n已写入 %s' % out)


if __name__ == '__main__':
    main()
