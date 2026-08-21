#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
变体对比回测（方案C：MACD 17/34/9, sell_anywhere=True, 双边万1）

【对比一】进攻腿：512100 vs 小盘2000
  1) 长窗口（2019-01-18 起）：2000指数（932000 中证2000，拉不到则用 399303 国证2000）
     代替 ETF 作进攻腿，与 512100 后复权基准对比。
     注意：指数为价格指数、不含分红，会略低估进攻腿收益。
  2) 可交易窗口：国证2000ETF(159628, 2022-06上市) / 中证2000ETF(563300, 2023-09上市)
     后复权，自上市日起与 512100 版本同窗对比。

【对比二】防守腿：512890 红利低波 vs 159201 自由现金流 vs 双标的20日动量择优
  自 159201 上市日起，a/b/c 三版同一成交口径（信号T日收盘产生、T+1开盘成交、
  换仓日拆旧仓隔夜段+新仓日内段、双边万1）。c 版每次切入防守仓时比较两者近20日
  涨幅选高者，防守期间不换仓。

新拉数据存 data/ 目录（etf_XXXXXX_hfq.csv / index_XXXXXX.csv）。
"""
import sys
import io
import os
import time

import numpy as np
import pandas as pd

if sys.platform == 'win32' and not getattr(sys.stdout, '_utf8_wrapped', False):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stdout._utf8_wrapped = True

from backtest import run_backtest, calc_signals, annualized, max_drawdown
import fetch_data as fd   # 复用 HQ_SERVERS / _connect_hq（不改动其逻辑）

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, 'data')
os.makedirs(DATA_DIR, exist_ok=True)

FEE = 0.0001
FAST, SLOW, SIG_N = 17, 34, 9
MOM_N = 20                     # 防守择优动量窗口（交易日）
START_2019 = '2019-01-18'      # 512890 上市日，现有长窗口口径


def load(name):
    df = pd.read_csv(os.path.join(DATA_DIR, name), parse_dates=['date'])
    df = df.set_index('date').sort_index()
    df.index = df.index.normalize()
    return df


# ---------------------------------------------------------------- 数据拉取

def _connect_hq_any(api):
    """按 fd.HQ_SERVERS 顺序探测可用服务器（probe 用 600519，与市场无关）"""
    fd._connect_hq(api)


def _market_of(code: str) -> int:
    """通达信市场代码：1=上海，0=深圳。5/6/9 开头为上海，0/1/2/3/4 开头为深圳"""
    return 1 if code[0] in ('5', '6', '9') else 0


def fetch_index_tdx(code: str, market: int, total: int = 8000) -> pd.DataFrame:
    """get_index_bars 拉指数日线（价格指数，不含分红）"""
    from pytdx.hq import TdxHq_API
    api = TdxHq_API()
    _connect_hq_any(api)
    rows = []
    start = 0
    while len(rows) < total:
        batch = api.get_index_bars(9, market, code, start, 800)
        if not batch:
            break
        rows = batch + rows
        if len(batch) < 800:
            break
        start += len(batch)
        time.sleep(0.3)
    api.disconnect()
    if not rows:
        raise RuntimeError('指数 %s 无数据' % code)
    df = pd.DataFrame(rows)
    df['date'] = pd.to_datetime(df['datetime'])
    df = df.set_index('date')[['open', 'high', 'low', 'close']]
    df = df[~df.index.duplicated(keep='last')].sort_index()
    df.index = df.index.normalize()
    return df


def fetch_etf_hfq_mkt(code: str, market: int) -> pd.DataFrame:
    """与 fetch_data.fetch_etf_hfq_tdx 相同的 TDX 自算后复权逻辑，仅市场代码参数化"""
    from pytdx.hq import TdxHq_API
    api = TdxHq_API()
    _connect_hq_any(api)
    rows = []
    start = 0
    while len(rows) < 8000:
        batch = api.get_security_bars(9, market, code, start, 800)
        if not batch:
            break
        rows = batch + rows
        if len(batch) < 800:
            break
        start += len(batch)
        time.sleep(0.3)
    try:
        xdxr = api.get_xdxr_info(market, code) or []
    except Exception:
        xdxr = []
    api.disconnect()
    if not rows:
        raise RuntimeError('ETF %s 无数据' % code)

    df = pd.DataFrame(rows)
    df['date'] = pd.to_datetime(df['datetime'])
    df = df.set_index('date')[['open', 'high', 'low', 'close']]
    df = df[~df.index.duplicated(keep='last')].sort_index()

    close = df['close']
    ret = close.pct_change().fillna(0.0)
    n_adj = 0
    for rec in xdxr:
        d = pd.Timestamp(rec['year'], rec['month'], rec['day'])
        pos = close.index.searchsorted(d)
        if pos >= len(close):
            continue
        d = close.index[pos]
        if pos == 0:
            continue
        prev = close.index[pos - 1]
        if rec['category'] == 1 and rec.get('fenhong'):
            div = float(rec['fenhong']) / 10.0
            ret.loc[d] = (close.loc[d] + div) / close.loc[prev] - 1
            n_adj += 1
        elif rec['category'] == 11 and rec.get('suogu'):
            s = float(rec['suogu'])
            ret.loc[d] = close.loc[d] * s / close.loc[prev] - 1
            n_adj += 1

    hfq_close = close.iloc[0] * (1 + ret).cumprod()
    factor = hfq_close / close
    out = df.mul(factor, axis=0)
    out.index = out.index.normalize()
    print('  [TDX复权] %s: 除权记录修正 %d 处' % (code, n_adj))
    return out


def fetch_etf_hfq_em_mkt(code: str, market: int) -> pd.DataFrame:
    """东财后备通道（secid 市场参数化），失败再切腾讯"""
    import requests
    url = 'https://push2his.eastmoney.com/api/qt/stock/kline/get'
    params = {
        'fields1': 'f1,f2,f3,f4,f5,f6',
        'fields2': 'f51,f52,f53,f54,f55,f56,f57,f58',
        'ut': '7eea3edcaed734bea9cbfc24409ed989',
        'klt': '101', 'fqt': '2',
        'beg': '20160101', 'end': '20991231',
        'secid': '%d.%s' % (market, code),
    }
    ua = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    sess = requests.Session()
    sess.trust_env = False
    last_err = None
    for _ in range(4):
        try:
            r = sess.get(url, params=params, headers=ua, timeout=15)
            kl = r.json()['data']['klines']
            rows = [k.split(',') for k in kl]
            df = pd.DataFrame(rows, columns=['date', 'open', 'close', 'high', 'low', 'vol', 'amount', 'amplitude'])
            df['date'] = pd.to_datetime(df['date'])
            for c in ['open', 'close', 'high', 'low']:
                df[c] = df[c].astype(float)
            return df.set_index('date')[['open', 'high', 'low', 'close']].sort_index()
        except Exception as e:
            last_err = e
            time.sleep(2)
    print('  东财通道失败(%s)，切换腾讯后备通道...' % repr(last_err)[:60])
    import requests as _rq
    sess = _rq.Session()
    sess.trust_env = False
    url = 'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get'
    symbol = ('sh' if market == 1 else 'sz') + code
    rows = []
    this_year = pd.Timestamp.now().year
    for year in range(2016, this_year + 1):
        param = '%s,day,%d-01-01,%d-12-31,400,hfq' % (symbol, year, year)
        r = sess.get(url, params={'param': param}, timeout=15)
        kl = r.json()['data'][symbol]
        key = 'hfqday' if 'hfqday' in kl else 'day'
        rows.extend(kl.get(key) or [])
        time.sleep(0.3)
    df = pd.DataFrame(rows, columns=['date', 'open', 'close', 'high', 'low', 'vol'][:len(rows[0])])
    df['date'] = pd.to_datetime(df['date'])
    for c in ['open', 'close', 'high', 'low']:
        df[c] = df[c].astype(float)
    df = df.set_index('date')[['open', 'high', 'low', 'close']]
    return df[~df.index.duplicated(keep='last')].sort_index()


def fetch_etf_hfq_auto(code: str) -> pd.DataFrame:
    """后复权日线：TDX 自算（主）→ 东财 → 腾讯"""
    market = _market_of(code)
    try:
        return fetch_etf_hfq_mkt(code, market)
    except Exception as e:
        print('  TDX复权通道失败(%s)，切东财/腾讯...' % repr(e)[:60])
        return fetch_etf_hfq_em_mkt(code, market)


def load_or_fetch_etf(code: str, name: str):
    """data/ 已有则直接读，否则拉取并保存"""
    path = os.path.join(DATA_DIR, name)
    if os.path.exists(path):
        df = load(name)
        print('  已存在 %s (%d 行 %s ~ %s)，直接使用'
              % (name, len(df), df.index[0].date(), df.index[-1].date()))
        return df
    df = fetch_etf_hfq_auto(code)
    df.to_csv(path)
    print('  已保存 %s (%d 行 %s ~ %s)'
          % (name, len(df), df.index[0].date(), df.index[-1].date()))
    return df


# ---------------------------------------------------------------- 指标与输出

def metrics(nav: pd.Series):
    return ((nav.iloc[-1] - 1) * 100, annualized(nav) * 100, max_drawdown(nav) * 100)


def print_table(title, rows, notes=None):
    """rows: [(名称, 累计%, 年化%, 最大回撤%, 换仓次数或'-'), ...]"""
    print('=' * 78)
    print(title)
    print('=' * 78)
    fmt = '%-30s %10s %10s %10s %8s'
    print(fmt % ('方案', '累计收益', '年化收益', '最大回撤', '换仓'))
    print('-' * 78)
    for name, cum, ann, mdd, ntr in rows:
        print(fmt % (name, '%.2f%%' % cum, '%.2f%%' % ann, '%.2f%%' % mdd, str(ntr)))
    if notes:
        print('-' * 78)
        for n in notes:
            print('注: ' + n)
    print()


def std_variant(avg, atk, dfn, start_date, label):
    """标准 run_backtest（方案C口径），返回表格行"""
    res = run_backtest(avg, atk, dfn, fee=FEE, sell_anywhere=True,
                       fast=FAST, slow=SLOW, sig_n=SIG_N,
                       label=label, start_date=start_date, verbose=False)
    cum, ann, mdd = metrics(res['nav_strat'])
    return (label, cum, ann, mdd, len(res['trades'])), res


def buy_hold(df, idx, name):
    nav = df.loc[idx, 'close']
    nav = nav / nav.iloc[0]
    cum, ann, mdd = metrics(nav)
    return (name + ' 买入持有', cum, ann, mdd, '-')


# ---------------------------------------------------------------- 双防守择优引擎

def dual_defense_backtest(avg, atk, def1, def2, start_date,
                          mom_n=MOM_N, pick=True, label='轮动'):
    """
    防守腿双标的择优回测。成交口径与 export_json.py 真实成交口径一致：
    - 信号 T 日收盘产生（calc_signals 同 run_backtest），T+1 日开盘成交
    - 换仓日收益 = 旧仓隔夜段(昨收→今开) × 新仓日内段(今开→今收) × (1-2*fee)
    - 普通日按持仓 close-to-close
    - 初始(回测首日前)持有防守仓，视作首日收盘建仓（同 export_json 约定）
    pick=True 时每次切入防守仓比较 def1/def2 近 mom_n 个交易日涨幅（截至信号日收盘），
    选高者持有至下次切换；pick=False 时固定 def1（用于 a/b 单标的版，保证同一引擎口径）。
    初始防守标的同样按首日动量选取（不足窗口则取 def1）。
    返回 dict: nav/trades/hold/idx/def_choice_log
    """
    sig = calc_signals(avg['close'], sell_anywhere=True,
                       fast=FAST, slow=SLOW, sig_n=SIG_N)
    idx = atk.index.intersection(def1.index).intersection(def2.index).intersection(sig.index)
    start_date = pd.Timestamp(start_date)
    idx = idx[idx >= start_date]
    sig = sig.loc[idx]

    o_atk, c_atk = atk.loc[idx, 'open'], atk.loc[idx, 'close']
    o_d1, c_d1 = def1.loc[idx, 'open'], def1.loc[idx, 'close']
    o_d2, c_d2 = def2.loc[idx, 'open'], def2.loc[idx, 'close']

    # 动量（用全历史计算，避免窗口起点前数据缺失；截至当日收盘的近 mom_n 日涨幅）
    mom1_full = def1['close'] / def1['close'].shift(mom_n) - 1
    mom2_full = def2['close'] / def2['close'].shift(mom_n) - 1

    def pick_def(sig_day):
        """信号日收盘后选择防守标的：True→def1, False→def2"""
        if not pick:
            return True
        m1 = mom1_full.get(sig_day, np.nan)
        m2 = mom2_full.get(sig_day, np.nan)
        if pd.isna(m1) and pd.isna(m2):
            return True
        if pd.isna(m1):
            return False
        if pd.isna(m2):
            return True
        return m1 >= m2

    n = len(idx)
    # ---- 状态机（同 run_backtest）：T日收盘信号，T+1开盘执行 ----
    state_atk = False          # True=进攻仓, False=防守仓
    cur_d1 = pick_def(idx[0])  # 初始防守标的
    pending = None
    trades = []                # (执行日, 动作, 成交价, 防守标的选择)
    choice_log = []            # (执行日, 选中防守标的, mom1, mom2)
    hold_atk = np.zeros(n, dtype=bool)
    hold_d1 = np.zeros(n, dtype=bool)   # 防守仓内是否持有 def1（进攻日无意义）

    for i in range(n):
        if pending is not None:
            if pending == 'buy' and not state_atk:
                state_atk = True
                trades.append((idx[i], '买入进攻/卖出防守', float(o_atk.iloc[i]), None))
            elif pending == 'sell' and state_atk:
                state_atk = False
                cur_d1 = pick_def(idx[i - 1])      # 信号日 = T+1 的前一交易日
                trades.append((idx[i], '卖出进攻/买入防守',
                               float((o_d1 if cur_d1 else o_d2).iloc[i]),
                               'def1' if cur_d1 else 'def2'))
                choice_log.append((idx[i], 'def1' if cur_d1 else 'def2',
                                   mom1_full.get(idx[i - 1], np.nan),
                                   mom2_full.get(idx[i - 1], np.nan)))
            pending = None
        hold_atk[i] = state_atk
        hold_d1[i] = cur_d1
        if sig['buy_sig'].iloc[i]:
            pending = 'buy'
        elif sig['sell_sig'].iloc[i]:
            pending = 'sell'

    # ---- 净值（export_json 真实成交口径） ----
    switch_at = {}
    for d, a, p, choice in trades:
        switch_at[d] = (a, choice)
    wealth = [1.0]
    for i in range(1, n):
        d = idx[i]
        if d not in switch_at:
            c = c_atk if hold_atk[i] else (c_d1 if hold_d1[i] else c_d2)
            w = wealth[-1] * float(c.iloc[i]) / float(c.iloc[i - 1])
        else:
            a, choice = switch_at[d]
            if a.startswith('买入进攻'):
                o_old, c_old = (o_d1, c_d1) if hold_d1[i] else (o_d2, c_d2)  # 换仓前的防守仓
                w = (wealth[-1] * float(o_old.iloc[i]) / float(c_old.iloc[i - 1])
                     * float(c_atk.iloc[i]) / float(o_atk.iloc[i]) * (1 - 2 * FEE))
            else:
                o_new, c_new = (o_d1, c_d1) if hold_d1[i] else (o_d2, c_d2)  # 换入的防守仓
                w = (wealth[-1] * float(o_atk.iloc[i]) / float(c_atk.iloc[i - 1])
                     * float(c_new.iloc[i]) / float(o_new.iloc[i]) * (1 - 2 * FEE))
        wealth.append(w)
    nav = pd.Series(wealth, index=idx)
    return {'nav': nav, 'trades': trades, 'idx': idx,
            'hold_atk': pd.Series(hold_atk, index=idx),
            'choice_log': choice_log, 'label': label}


def dual_row(res, label):
    cum, ann, mdd = metrics(res['nav'])
    return (label, cum, ann, mdd, len(res['trades']))


# ---------------------------------------------------------------- 主流程

def main():
    avg = load('avg_880003.csv')
    etf1000 = load('etf_512100_hfq.csv')
    etfdiv = load('etf_512890_hfq.csv')
    print('本地数据: 880003 %s ~ %s | 512100 %s ~ %s | 512890 %s ~ %s'
          % (avg.index[0].date(), avg.index[-1].date(),
             etf1000.index[0].date(), etf1000.index[-1].date(),
             etfdiv.index[0].date(), etfdiv.index[-1].date()))
    print()

    # ======================= 对比一：进攻腿换小盘2000 =======================
    print('#' * 78)
    print('# 对比一：进攻腿 = 512100 vs 小盘2000')
    print('#' * 78)

    # ---- 1) 长窗口：指数代替 ETF ----
    idx2000 = None
    idx2000_name = None
    fallback2000 = None   # (df, name) 历史不足 2019 但可兜底
    print('--- 拉取 2000 指数（先 932000 中证2000[沪]，失败则 399303 国证2000[深]） ---')
    for code, market, name in [('932000', 1, '中证2000指数(932000)'),
                               ('399303', 0, '国证2000指数(399303)')]:
        try:
            df = fetch_index_tdx(code, market)
            df.to_csv(os.path.join(DATA_DIR, 'index_%s.csv' % code))
            print('  %s: %d 行 %s ~ %s'
                  % (name, len(df), df.index[0].date(), df.index[-1].date()))
            if df.index[0] <= pd.Timestamp(START_2019):
                idx2000, idx2000_name = df, name
                break
            print('  %s 历史起点 %s 晚于 %s，长窗口不够，尝试下一指数'
                  % (name, df.index[0].date(), START_2019))
            if fallback2000 is None:
                fallback2000 = (df, name)
        except Exception as e:
            print('  %s 拉取失败: %s' % (name, repr(e)[:80]))
    if idx2000 is None and fallback2000 is not None:
        idx2000, idx2000_name = fallback2000
        print('  !! 无覆盖2019的2000指数，兜底使用 %s（窗口将从 %s 起）'
              % (idx2000_name, idx2000.index[0].date()))
    if idx2000 is None:
        print('  !! 两个 2000 指数都拉不到，长窗口对比一跳过')

    if idx2000 is not None:
        rows = []
        r, _ = std_variant(avg, etf1000, etfdiv, START_2019, 'C: 进攻=512100(基准)')
        rows.append(r)
        r, res_idx = std_variant(avg, idx2000, etfdiv, START_2019,
                                 'C: 进攻=%s' % idx2000_name)
        rows.append(r)
        win = res_idx['idx']
        rows.append(buy_hold(etf1000, win, '512100'))
        rows.append(buy_hold(idx2000, win, idx2000_name))
        print_table('对比一(1) 长窗口 %s ~ %s（指数不含分红，略低估进攻腿收益）'
                    % (win[0].date(), win[-1].date()), rows,
                    notes=['指数进攻腿为价格指数，成分股分红未计入，实际可投资工具(ETF)会略好'])

    # ---- 2) 可交易窗口：2000ETF ----
    fetched = []
    print('--- 拉取 2000ETF 后复权（159628 国证2000ETF[深] / 563300 中证2000ETF[沪]） ---')
    for code, name, fname in [('159628', '国证2000ETF(159628)', 'etf_159628_hfq.csv'),
                              ('563300', '中证2000ETF(563300)', 'etf_563300_hfq.csv')]:
        try:
            df = load_or_fetch_etf(code, fname)
            if len(df) > 30:
                fetched.append((code, name, df))
            else:
                print('  %s 数据过少(%d行)，跳过' % (name, len(df)))
        except Exception as e:
            print('  %s 拉取失败: %s' % (name, repr(e)[:80]))

    for code, name, df in fetched:
        start = max(df.index[0], pd.Timestamp(START_2019))
        rows = []
        r, _ = std_variant(avg, etf1000, etfdiv, start, 'C: 进攻=512100(同窗)')
        rows.append(r)
        r, res2 = std_variant(avg, df, etfdiv, start, 'C: 进攻=%s' % name)
        rows.append(r)
        win = res2['idx']
        rows.append(buy_hold(etf1000, win, '512100'))
        rows.append(buy_hold(df, win, name))
        print_table('对比一(2) 可交易窗口 %s ~ %s（%s 上市起）'
                    % (win[0].date(), win[-1].date(), name), rows)

    # ======================= 对比二：防守腿双标的择优 =======================
    print('#' * 78)
    print('# 对比二：防守腿 = 512890 vs 159201 vs 双标的20日动量择优')
    print('#' * 78)
    df159201 = None
    print('--- 拉取 自由现金流ETF(159201[深], 2025年初上市) 后复权 ---')
    try:
        df159201 = load_or_fetch_etf('159201', 'etf_159201_hfq.csv')
    except Exception as e:
        print('  159201 拉取失败: %s' % repr(e)[:80])

    if df159201 is not None and len(df159201) > 30:
        start2 = df159201.index[0]
        rows = []
        res_a = dual_defense_backtest(avg, etf1000, etfdiv, etfdiv, start2,
                                      pick=False, label='a')
        rows.append(dual_row(res_a, 'C-a: 防守=512890 红利低波(现有)'))
        res_b = dual_defense_backtest(avg, etf1000, df159201, df159201, start2,
                                      pick=False, label='b')
        rows.append(dual_row(res_b, 'C-b: 防守=159201 自由现金流'))
        res_c = dual_defense_backtest(avg, etf1000, etfdiv, df159201, start2,
                                      pick=True, label='c')
        rows.append(dual_row(res_c, 'C-c: 防守=双标的20日动量择优'))
        win = res_c['idx']
        rows.append(buy_hold(etf1000, win, '512100'))
        rows.append(buy_hold(etfdiv, win, '512890'))
        rows.append(buy_hold(df159201, win, '159201'))
        span_days = (win[-1] - win[0]).days
        notes = ['a/b/c 同一成交口径：信号T日收盘产生，T+1开盘成交，'
                 '换仓日拆旧仓隔夜段+新仓日内段，双边万1',
                 'c 版防守期间不换仓，仅切入防守时按近%d日涨幅择优' % MOM_N]
        if span_days < 730:
            notes.insert(0, '*** 样本窗口仅约 %d 个月，样本太短仅供参考 ***'
                         % round(span_days / 30.4))
        print_table('对比二 窗口 %s ~ %s（159201 上市起）'
                    % (win[0].date(), win[-1].date()), rows, notes=notes)

        if res_c['choice_log']:
            print('C-c 防守择优明细（切入防守仓时的选择）:')
            for d, choice, m1, m2 in res_c['choice_log']:
                picked = '512890' if choice == 'def1' else '159201'
                m1s = 'NA' if pd.isna(m1) else '%+.2f%%' % (m1 * 100)
                m2s = 'NA' if pd.isna(m2) else '%+.2f%%' % (m2 * 100)
                print('  %s  选中 %s   (512890近20日 %s, 159201近20日 %s)'
                      % (d.date(), picked, m1s, m2s))
            print()
    else:
        print('  !! 159201 数据不可用或过少，对比二跳过')

    # ======================= 对比三：进攻腿加 中证2000增强ETF =======================
    print('#' * 78)
    print('# 对比三：进攻腿 = 512100 vs 563300 中证2000ETF vs 159552 中证2000增强ETF(招商)')
    print('#' * 78)
    df159552 = None
    df563300_3 = None
    print('--- 拉取 中证2000增强ETF(159552[深], 2024-06-28上市) 后复权 ---')
    try:
        df159552 = load_or_fetch_etf('159552', 'etf_159552_hfq.csv')
    except Exception as e:
        print('  159552 拉取失败: %s' % repr(e)[:80])
    try:
        df563300_3 = load_or_fetch_etf('563300', 'etf_563300_hfq.csv')
    except Exception as e:
        print('  563300 拉取失败: %s' % repr(e)[:80])

    if df159552 is not None and len(df159552) > 30 and df563300_3 is not None:
        start3 = df159552.index[0]     # 实际可用起始日（上市首月若稀疏则体现为更晚）
        print('  159552 实际数据起点: %s（上市日 2024-06-28，若晚于该日即首月数据缺失/稀疏）'
              % start3.date())
        rows = []
        r, _ = std_variant(avg, etf1000, etfdiv, start3, 'C: 进攻=512100')
        rows.append(r)
        r, _ = std_variant(avg, df563300_3, etfdiv, start3, 'C: 进攻=563300')
        rows.append(r)
        r, res3 = std_variant(avg, df159552, etfdiv, start3, 'C: 进攻=159552(增强)')
        rows.append(r)
        win = res3['idx']
        rows.append(buy_hold(etf1000, win, '512100'))
        rows.append(buy_hold(df563300_3, win, '563300'))
        rows.append(buy_hold(df159552, win, '159552'))

        # 159552 相对 563300 的买入持有区间超额（增强 alpha 直观体现）
        nav552 = df159552.loc[win, 'close'] / df159552.loc[win, 'close'].iloc[0]
        nav300 = df563300_3.loc[win, 'close'] / df563300_3.loc[win, 'close'].iloc[0]
        excess_pp = ((nav552.iloc[-1] - 1) - (nav300.iloc[-1] - 1)) * 100
        excess_rel = (nav552.iloc[-1] / nav300.iloc[-1] - 1) * 100

        span_days = (win[-1] - win[0]).days
        notes = ['159552 相对 563300 买入持有区间超额: %+0.2f 个百分点（相对净值 %+.2f%%）'
                 % (excess_pp, excess_rel)]
        if span_days < 730:
            notes.insert(0, '*** 样本窗口仅约 %d 个月，样本太短仅供参考 ***'
                         % round(span_days / 30.4))
        print_table('对比三 窗口 %s ~ %s（159552 数据可用起，约 %d 个月）'
                    % (win[0].date(), win[-1].date(), round(span_days / 30.4)),
                    rows, notes=notes)
    else:
        print('  !! 159552/563300 数据不可用或过少，对比三跳过')

    print('全部完成。')


if __name__ == '__main__':
    main()
