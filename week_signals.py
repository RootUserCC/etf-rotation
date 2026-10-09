# 本周(2026-09-14~09-18)触发买点信号的股票 + 买点之后到周五的收益
# 信号口径复用 ai_yellow_bar.py：黄柱=绿柱缩头第N根 / 红柱=红柱放大第N根，当日收盘买
import csv
import pandas as pd
from ai_yellow_bar import prepare

POOL = {}
for part in open("week_optimal.py", encoding="utf-8").read().split('POOL = """')[1].split('"""')[0].replace("|", "\n").split("\n"):
    part = part.strip()
    if part:
        code, name = part.split()
        POOL[code] = name

WEEK = pd.date_range("2026-09-14", "2026-09-18")
FRI = pd.Timestamp("2026-09-18")

# 三套买点口径：严格第4根黄柱(回测最优) / 放宽第3根黄柱(当前生产) / 第2根红柱(红柱最优)
RULES = [("黄柱第4根", "run", 4), ("黄柱第3根", "run", 3), ("红柱第2根", "red_run", 2)]

rows_out = []
for code, name in POOL.items():
    try:
        df = pd.read_csv(f"data/ai_stocks/{code}.csv", parse_dates=["date"], index_col="date")
    except FileNotFoundError:
        continue
    if FRI not in df.index:
        continue
    p = prepare(df)
    idx, closes = p["idx"], p["closes"]
    highs, lows = df["high"].values, df["low"].values
    for rule_name, key, n_bar in RULES:
        run = p[key]
        for i in range(len(idx)):
            if run[i] == n_bar and idx[i] in set(WEEK):
                entry = closes[i]
                after = [(j, idx[j]) for j in range(i + 1, len(idx)) if idx[j] <= FRI]
                ret_fri = closes[after[-1][0]] / entry - 1 if after else 0.0
                hold_days = len(after)
                max_high = max((highs[j] for j, _ in after), default=entry)
                min_low = min((lows[j] for j, _ in after), default=entry)
                # 蓝柱止盈/止损8%在周五前是否触发
                blue_hit = any(p["blue"][j] for j, _ in after)
                tp8 = next((idx[j] for j, _ in after if closes[j] >= entry * 1.08), None)
                sl8 = next((idx[j] for j, _ in after if closes[j] <= entry * 0.92), None)
                rows_out.append(dict(
                    rule=rule_name, code=code, name=name, buy_date=str(idx[i].date()),
                    entry=entry, hold=hold_days, ret_fri=ret_fri,
                    max_up=max_high / entry - 1, max_dn=min_low / entry - 1,
                    blue=blue_hit, tp8=str(tp8.date()) if tp8 is not None else "",
                    sl8=str(sl8.date()) if sl8 is not None else ""))

df_out = pd.DataFrame(rows_out)
for rule_name, _, _ in RULES:
    sub = df_out[df_out.rule == rule_name].sort_values("ret_fri", ascending=False)
    print(f"\n===== {rule_name}买入：本周触发 {len(sub)} 只 =====")
    if len(sub) == 0:
        continue
    print(f"{'买点日':<11}{'名称':<8}{'买入价':>8} {'持有':>3} {'至周五':>8} {'期间最高':>8} {'期间最低':>8}  事件")
    for _, r in sub.iterrows():
        ev = []
        if r.tp8: ev.append(f"{r.tp8}止盈8%")
        if r.sl8: ev.append(f"{r.sl8}止损-8%")
        if r.blue: ev.append("现蓝柱")
        print(f"{r.buy_date:<11}{r['name']:<8}{r.entry:>8.2f} {r.hold:>3}天 "
              f"{r.ret_fri*100:>+7.2f}% {r.max_up*100:>+7.2f}% {r.max_dn*100:>+7.2f}%  {' '.join(ev)}")
    print(f"  汇总: 胜率 {(sub.ret_fri > 0).mean()*100:.0f}%  平均 {sub.ret_fri.mean()*100:+.2f}%  "
          f"中位 {sub.ret_fri.median()*100:+.2f}%")
