# 分仓衰减验证：N 只均仓的最优路径 vs 通信ETF(515880) / 科创半导体设备ETF(588710)
import csv, sys
sys.path.insert(0, ".")
from fetch_data import fetch_etf_hq

POOL_CODES = [l.split()[0] for l in open("week_optimal.py", encoding="utf-8").read()
              .split('POOL = """')[1].split('"""')[0].replace("|", "\n").split("\n") if l.strip()]

DAYS = ["2026-09-11", "2026-09-14", "2026-09-15", "2026-09-16", "2026-09-17", "2026-09-18"]
LEGS = [("2026-09-14", "2026-09-15"), ("2026-09-15", "2026-09-16"),
        ("2026-09-16", "2026-09-17"), ("2026-09-17", "2026-09-18")]

def limit_pct(code):
    return 0.20 if code[:3] in ("300", "301", "688") else 0.10

stocks = {}
for code in POOL_CODES:
    rows = {}
    with open(f"data/ai_stocks/{code}.csv", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows[r["date"]] = {k: float(r[k]) for k in ("open", "high", "low", "close")}
    if all(d in rows for d in DAYS):
        stocks[code] = rows

# ---- 本周每日候选腿（含涨停买不进/跌停卖不出约束）----
daily = []
for buy_d, sell_d in LEGS:
    cands = []
    for code, rows in stocks.items():
        prev_d = DAYS[DAYS.index(buy_d) - 1]
        c_prev, c_buy, c_sell = rows[prev_d]["close"], rows[buy_d]["close"], rows[sell_d]["close"]
        lp = limit_pct(code)
        if c_buy >= c_prev * (1 + lp - 0.001) or c_sell <= c_buy * (1 - lp + 0.001):
            continue
        cands.append((c_sell / c_buy - 1, code))
    daily.append(sorted(cands, reverse=True))

# ---- N 仓均分最优路径 ----
print("===== 本周 N 仓均分最优路径（40万） =====")
for n in (1, 2, 3, 5, 8, 10, 20):
    cap = 400000.0
    for cands in daily:
        day_ret = sum(r for r, _ in cands[:n]) / n
        cap *= 1 + day_ret
    print(f"  {n:>2} 只均仓: 周收益 {(cap/400000-1)*100:+7.2f}%   期末 {cap/10000:6.2f}万")

# ---- ETF 对比 ----
print("\n===== ETF 同期表现 =====")
for code, name, mkt in (("515880", "通信ETF国泰", 1), ("588710", "科创半导体设备ETF", 1),
                        ("588170", "科创半导体ETF华夏", 1), ("512100", "中证1000ETF", 1)):
    try:
        df = fetch_etf_hq(code, total=300, market=mkt)
    except Exception as e:
        print(f"  {name}({code}) 拉取失败: {e}")
        continue
    rows = {str(d)[:10]: r for d, r in df.iterrows()}
    if "2026-09-14" not in rows or "2026-09-18" not in rows:
        print(f"  {name}({code}) 数据区间 {min(rows)} ~ {max(rows)}，本周不全")
        continue
    wk = rows["2026-09-18"]["close"] / rows["2026-09-14"]["close"] - 1
    daily_str = "  ".join(f"{dt[5:]} {rows[dt]['close']/rows[prev]['close']-1:+.1%}"
                          for prev, dt in zip(DAYS[1:-1], DAYS[2:]))
    print(f"  {name}({code}): 本周(周一收盘→周五收盘) {wk*100:+.2f}%   日线: {daily_str}")
    # 近一月对照
    if "2026-08-18" in rows:
        m1 = rows["2026-09-18"]["close"] / rows["2026-08-18"]["close"] - 1
        print(f"      近一月(8/18→9/18): {m1*100:+.2f}%")

# ---- 对照：股票池等权本周 ----
rets = [rows["2026-09-18"]["close"] / rows["2026-09-14"]["close"] - 1 for rows in stocks.values()]
print(f"\n===== 股票池整体({len(stocks)}只) 本周: 等权 {sum(rets)/len(rets)*100:+.2f}%  "
      f"中位数 {sorted(rets)[len(rets)//2]*100:+.2f}%  最好 {max(rets)*100:+.2f}%  最差 {min(rets)*100:+.2f}%")
