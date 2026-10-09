# 红柱第2根买入 → 红柱第3根收盘卖出（未到第3根就变色则变色日卖）全样本回测
# 对比 ai_yellow_bar.py 中现有红柱卖出规则
import pandas as pd
from ai_yellow_bar import prepare

POOL = {}
for part in open("week_optimal.py", encoding="utf-8").read().split('POOL = """')[1].split('"""')[0].replace("|", "\n").split("\n"):
    part = part.strip()
    if part:
        code, name = part.split()
        POOL[code] = name

START = pd.Timestamp("2026-01-01")
data = {}
for code in POOL:
    try:
        df = pd.read_csv(f"data/ai_stocks/{code}.csv", parse_dates=["date"], index_col="date")
        data[code] = prepare(df)
    except FileNotFoundError:
        pass


def entry_red2(p):
    """红柱第2根信号日索引"""
    return [i for i in range(60, len(p["idx"])) if p["red_run"][i] == 2 and p["idx"][i] >= START]


def stat(rets, days):
    if not rets:
        return "0笔"
    s = pd.Series(rets)
    win, loss = s[s > 0], s[s <= 0]
    pf = win.sum() / abs(loss.sum()) if len(loss) and win.sum() else float('nan')
    return (f"{len(s):>4}笔  胜率 {(s>0).mean()*100:>4.1f}%  平均 {s.mean()*100:>+6.2f}%  "
            f"中位 {s.median()*100:>+6.2f}%  盈亏比 {win.mean()/abs(loss.mean()):>4.2f}  "
            f"PF {pf:>4.2f}  平均持有 {sum(days)/len(days):>4.1f}天")


def run_rule(rule):
    """rule: 函数(p, i) -> (exit_idx, reason)"""
    rets, days, reasons, trades = [], [], {}, []
    for code, p in data.items():
        idx, closes = p["idx"], p["closes"]
        i = 60
        n = len(idx)
        while i < n:
            if p["red_run"][i] == 2 and idx[i] >= START:
                exit_j, why = rule(p, i)
                r = closes[exit_j] / closes[i] - 1
                rets.append(r)
                days.append(exit_j - i)
                reasons[why] = reasons.get(why, 0) + 1
                trades.append((code, POOL[code], idx[i], idx[exit_j], r, exit_j - i, why))
                i = exit_j + 1
            else:
                i += 1
    return rets, days, reasons, trades


def rule_red3(p, i):
    """第3根红柱收盘卖；未到第3根就变色（红柱段结束）则变色日收盘卖"""
    n = len(p["idx"])
    j = i + 1
    while j < n:
        if p["red_run"][j] == 3:
            return j, "到第3根红柱"
        if p["red_run"][j] == 0:
            return j, "未到3根即变色"
        j += 1
    return n - 1, "到期"


def rule_red4(p, i):
    n = len(p["idx"])
    j = i + 1
    while j < n:
        if p["red_run"][j] == 4:
            return j, "到第4根红柱"
        if p["red_run"][j] == 0:
            return j, "未到4根即变色"
        j += 1
    return n - 1, "到期"


def rule_tp(p, i, tp):
    n = len(p["idx"])
    entry = p["closes"][i]
    j = i + 1
    while j < n and j - i <= 60:
        if p["closes"][j] >= entry * (1 + tp):
            return j, f"止盈{tp:.0%}"
        j += 1
    return min(j, n - 1), "未止盈持有到60日"


def rule_blue(p, i):
    n = len(p["idx"])
    j = i + 1
    while j < n:
        if p["blue"][j]:
            return j, "首根蓝柱"
        j += 1
    return n - 1, "到期"


print("===== 红柱第2根买入，不同卖出规则（2026-01-01 ~ 2026-09-18，池内 %d 只） =====" % len(data))
for name, rule in [("红柱第3根收盘卖", rule_red3), ("红柱第4根收盘卖", rule_red4),
                   ("止盈8%卖", lambda p, i: rule_tp(p, i, 0.08)),
                   ("止盈5%卖", lambda p, i: rule_tp(p, i, 0.05)),
                   ("止盈10%卖", lambda p, i: rule_tp(p, i, 0.10)),
                   ("首根蓝柱卖", rule_blue)]:
    rets, days, reasons, trades = run_rule(rule)
    print(f"\n【{name}】{stat(rets, days)}")
    print("   出场原因: " + "  ".join(f"{k} {v}笔" for k, v in sorted(reasons.items(), key=lambda x: -x[1])))

# 红3规则逐笔分布
rets, days, reasons, trades = run_rule(rule_red3)
tr = pd.DataFrame(trades, columns=["code", "name", "buy", "sell", "ret", "days", "why"])
print("\n===== 红柱第3根卖：最好/最差各5笔 =====")
for _, r in tr.nlargest(5, "ret").iterrows():
    print(f"  {r.buy.date()} 买 {r['name']} → {r.sell.date()} 卖 {r.ret*100:+.2f}% ({r.days}天, {r.why})")
for _, r in tr.nsmallest(5, "ret").iterrows():
    print(f"  {r.buy.date()} 买 {r['name']} → {r.sell.date()} 卖 {r.ret*100:+.2f}% ({r.days}天, {r.why})")

# 市场环境依赖：按月汇总
tr["month"] = pd.to_datetime(tr.buy).dt.to_period("M")
print("\n===== 红柱第3根卖：按月表现 =====")
for m, g in tr.groupby("month"):
    print(f"  {m}: {len(g):>3}笔  胜率 {(g.ret>0).mean()*100:>4.1f}%  平均 {g.ret.mean()*100:>+6.2f}%  合计 {g.ret.sum()*100:>+7.1f}%")

# 本周信号在该规则下的结果
print("\n===== 本周(9/14~9/18)红柱第2根信号 → 红柱第3根卖 的结果 =====")
wk = tr[(tr.buy >= pd.Timestamp("2026-09-14")) & (tr.buy <= pd.Timestamp("2026-09-18"))]
for _, r in wk.sort_values("ret", ascending=False).iterrows():
    print(f"  {r.buy.date()} 买 {r['name']:<6} → {r.sell.date()} 卖 {r.ret*100:>+6.2f}% ({r.days}天, {r.why})")
print(f"  本周均: {wk.ret.mean()*100:+.2f}%  胜率 {(wk.ret>0).mean()*100:.0f}%  ({len(wk)}笔)")
