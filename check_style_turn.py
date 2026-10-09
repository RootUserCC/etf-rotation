#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""风格拐点检验：科技/红利相对强弱的中期拐点，是否伴随"科技+红利+指数"三者同涨？

命题：科技与红利的拐点，一般会出现 科技/红利/指数 同涨。

口径：
  R = 科技腿收盘 / 红利腿收盘（后复权），三条中期拐点定义：
    S1 均线金叉：R 的 20 日均线上穿 60 日均线
    S2 动量转正：R 的 60 日动量（R/R[-60]-1）由负转正
    S3 周线拐头：R 的周线 MACD(12/26/9) DIF 拐头向上（信号周次一交易日生效）
  统计拐点当日、以及拐点次日起 h 个交易日内，三条腿的涨跌组合，
  并以全样本"任意日往后 h 日三者同涨"的基准概率作对照。
"""
import sys
import io
import pandas as pd
import numpy as np

if sys.platform == 'win32' and not getattr(sys.stdout, '_utf8_wrapped', False):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stdout._utf8_wrapped = True


def load(name):
    df = pd.read_csv('data/%s.csv' % name)
    df['date'] = pd.to_datetime(df['date'].astype(str).str[:10])
    return df.drop_duplicates('date').set_index('date')['close'].astype(float)


def ema(s, n):
    return s.ewm(alpha=2.0 / (n + 1), adjust=False).mean()


def sig_ma(df, fast=20, slow=60):
    r = df['tech'] / df['div']
    f, s = r.rolling(fast).mean(), r.rolling(slow).mean()
    cross = (f > s) & (f.shift(1) <= s.shift(1))
    return cross.fillna(False)


def sig_mom(df, n=60):
    r = df['tech'] / df['div']
    m = r / r.shift(n) - 1
    return ((m > 0) & (m.shift(1) <= 0)).fillna(False)


def sig_wdif(df, fast=12, slow=26):
    r = (df['tech'] / df['div'])
    wk = r.resample('W-FRI').last().dropna()
    dif = ema(wk, fast) - ema(wk, slow)
    p1, p2 = dif.shift(1), dif.shift(2)
    up = ((p1 < p2) & (dif > p1)).fillna(False)
    out = pd.Series(False, index=df.index)
    for d in wk.index[up.values]:
        k = df.index.searchsorted(d, side='right')
        if k < len(df):
            out.iloc[k] = True
    return out


def analyze(tag, df, ret, sig, idx_cols, horizons=(1, 3, 5, 10)):
    cols = ['tech', 'div'] + idx_cols

    def cum(r, i, h, use):
        """从下标 i+1 起 h 个交易日（不含信号当日）的累计收益"""
        if i + h >= len(df):
            return None
        seg = r.iloc[i + 1:i + 1 + h]
        return {c: float((1 + seg[c]).prod() - 1) for c in use}

    def combo(r, i, h, use):
        v = cum(r, i, h, use)
        if v is None:
            return None
        if all(x > 0 for x in v.values()):
            return '三涨'
        if all(x < 0 for x in v.values()):
            return '三跌'
        return '混合'

    pos = [i for i in range(len(df)) if sig.iloc[i]]
    print('\n=== %s ===' % tag)
    print('样本 %s ~ %s，%d 个交易日；拐点信号 %d 次' %
          (df.index[0].date(), df.index[-1].date(), len(df), len(pos)))
    # 基准概率
    base = {}
    marg = {}
    for h in horizons:
        for c in idx_cols:
            tot = cnt = 0
            m = {'tech': 0, 'div': 0, c: 0}
            for i in range(len(df) - h - 1):
                v = cum(ret, i, h, ['tech', 'div', c])
                tot += 1
                for k in m:
                    if v[k] > 0:
                        m[k] += 1
                if all(x > 0 for x in v.values()):
                    cnt += 1
            base[(h, c)] = cnt / float(tot)
            marg[(h, c)] = {k: v / float(tot) for k, v in m.items()}
    print('基准：任意日往后 h 日 单腿上涨率 科技/红利/指数，及三者同涨率')
    for h in horizons:
        c = idx_cols[0]
        print('  h=%-3d  %4.0f%%/%4.0f%%/%4.0f%%  → 三者同涨 %4.0f%%'
              % (h, 100 * marg[(h, c)]['tech'], 100 * marg[(h, c)]['div'],
                 100 * marg[(h, c)][c], 100 * base[(h, c)]))
    # 明细：拐点日 + 前20日两腿 + 次日 1 日/5 日三腿表现
    print('%-11s %-15s %-21s %-21s' % ('拐点日', '前20日 科技/红利', '当日 科技/红利/指数', '次日起5日 科技/红利/指数'))
    for i in pos:
        t = df.index[i]
        p20 = '-'
        if i >= 20:
            p20 = '%+.1f%%/%+.1f%%' % (100 * (df['tech'].iloc[i] / df['tech'].iloc[i - 20] - 1),
                                       100 * (df['div'].iloc[i] / df['div'].iloc[i - 20] - 1))
        d0 = '%+.2f%%/%+.2f%%/%+.2f%%' % (100 * ret['tech'].iloc[i], 100 * ret['div'].iloc[i],
                                          100 * ret[idx_cols[0]].iloc[i])
        c5 = cum(ret, i, 5, cols)
        d5 = '-' if c5 is None else '%+.1f%%/%+.1f%%/%+.1f%%' % (100 * c5['tech'], 100 * c5['div'],
                                                                 100 * c5[idx_cols[0]])
        print('%-11s %-15s %-21s %-21s' % (str(t.date()), p20, d0, d5))
    n = float(len(pos))
    print('%-6s %10s %10s %10s %10s' % ('窗口', '科技涨', '红利涨', '指数涨', '三者同涨'))
    for h in horizons:
        row = []
        for c in idx_cols:
            cnts = {'tech': 0, 'div': 0, c: 0, 'all': 0}
            for i in pos:
                v = cum(ret, i, h, ['tech', 'div', c])
                if v is None:
                    continue
                if v['tech'] > 0:
                    cnts['tech'] += 1
                if v['div'] > 0:
                    cnts['div'] += 1
                if v[c] > 0:
                    cnts[c] += 1
                if all(x > 0 for x in v.values()):
                    cnts['all'] += 1
            row.append('%s: 科技%4.0f%%(基%3.0f%%) 红利%4.0f%%(基%3.0f%%) 指数%4.0f%%(基%3.0f%%) 三涨%4.0f%%(基%3.0f%%)'
                       % (c.replace('idx_', ''), 100 * cnts['tech'] / n, 100 * marg[(h, c)]['tech'],
                          100 * cnts['div'] / n, 100 * marg[(h, c)]['div'],
                          100 * cnts[c] / n, 100 * marg[(h, c)][c],
                          100 * cnts['all'] / n, 100 * base[(h, c)]))
        print('h=%-4d %s' % (h, '   '.join(row)))
    # 符号形态（h=5）
    from collections import Counter
    cc = Counter()
    for i in pos:
        s = combo(ret, i, 5, ['tech', 'div', idx_cols[0]])
        if s:
            cc[s] += 1
    print('次日起5日形态：' + '，'.join('%s %d 次' % (k, v) for k, v in cc.items()))
    # 量级：拐点后各腿涨幅的均值/中位数（剔除极端值影响看中位）
    for h in (5, 10):
        vs = [cum(ret, i, h, cols) for i in pos]
        vs = [v for v in vs if v]
        if not vs:
            continue
        print('次日起%-2d日涨幅  %s' % (h, '  '.join(
            '%s 均值%+5.2f%% 中位%+5.2f%%' % (c, 100 * np.mean([v[c] for v in vs]),
                                              100 * np.median([v[c] for v in vs])) for c in cols)))


def rev_test(tag, df, ret, idx_col):
    """反向检验：近5日出现"三涨"之后，科技相对红利是否走强（这是把该说法当信号用）"""
    cols = ['tech', 'div', idx_col]
    hit = []
    for i in range(5, len(df) - 20):
        v = {c: float((1 + ret[c].iloc[i - 4:i + 1]).prod() - 1) for c in cols}
        fwd_t = float((1 + ret['tech'].iloc[i + 1:i + 21]).prod() - 1)
        fwd_d = float((1 + ret['div'].iloc[i + 1:i + 21]).prod() - 1)
        if all(x > 0 for x in v.values()):
            hit.append(fwd_t - fwd_d)
    base = [float((1 + ret['tech'].iloc[i + 1:i + 21]).prod() - 1) -
            float((1 + ret['div'].iloc[i + 1:i + 21]).prod() - 1)
            for i in range(5, len(df) - 20)]
    if not hit:
        return
    print('%s 近5日三涨 后20日 科技-红利 超额：均值 %+.2f%% 中位 %+.2f%% 胜率 %.0f%%（%d 次）'
          % (tag, 100 * np.mean(hit), 100 * np.median(hit),
             100 * np.mean([x > 0 for x in hit]), len(hit)))
    print('%s 全样本      后20日 科技-红利 超额：均值 %+.2f%% 中位 %+.2f%% 胜率 %.0f%%'
          % (' ' * len(tag), 100 * np.mean(base), 100 * np.median(base),
             100 * np.mean([x > 0 for x in base])))


def main():
    AVG, SH = load('avg_880003'), load('index_000001')
    idx_cols = ['idx_avg', 'idx_sh']

    def run(tag, tech, div, start, end):
        df = pd.DataFrame({'tech': tech, 'div': div, 'idx_avg': AVG, 'idx_sh': SH}).loc[start:end].dropna()
        ret = df.pct_change().fillna(0.0)
        for name, fn in (('S1 均线金叉(20/60)', sig_ma), ('S2 动量转正(60日)', sig_mom),
                         ('S3 周线DIF拐头', sig_wdif)):
            analyze('%s  %s' % (tag, name), df, ret, fn(df), idx_cols)
        rev_test(tag, df, ret, 'idx_avg')

    run('长窗 2011-2026  科技=159915创业板 / 红利=510880',
        load('etf_159915_hfq'), load('etf_510880_hfq'), '2011-12-09', '2026-08-11')
    run('近窗 2019-2026  科技=515050 5G通信 / 红利=512890 红利低波',
        load('etf_515050_hfq'), load('etf_512890_hfq'), '2019-10-16', '2026-09-08')


if __name__ == '__main__':
    main()
