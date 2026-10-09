# 本周(2026-09-14~09-18)股票池最优路径回测
# 规则：本金40万，仅股票(剔除ETF/港股)，周一收盘建仓，每日收盘换股(T+1可行：当日收盘买、次日收盘卖)，周五收盘清仓
# 可行性约束：买入日收盘涨停则买不进、卖出日收盘跌停则卖不出，该股当日剔除
import csv, os

POOL = """002636 金安国纪|603186 华正新材|600183 生益科技|301217 铜冠铜箔|002463 沪电股份|002916 深南电路
603228 景旺电子|002938 鹏鼎控股|002384 东山精密|603175 超颖电子|601869 长飞光纤|600522 中天科技
600487 亨通光电|600498 烽火通信|603688 石英股份|300395 菲利华|601138 工业富联|000977 浪潮信息
603019 中科曙光|002837 英维克|301018 申菱环境|300857 协创数据|603629 利通电子|000815 美利云
002929 润建股份|688256 寒武纪|688041 海光信息|300499 高澜股份|603256 宏和科技|301526 国际复材
605006 山东玻纤|600176 中国巨石|300308 中际旭创|300502 新易盛|300394 天孚通信|002281 光迅科技
603083 剑桥科技|603986 兆易创新|001309 德明利|301308 江波龙|688525 佰维存储|688008 澜起科技
300475 香农芯创|000021 深科技|000636 风华高科|300408 三环集团|300285 国瓷材料|002859 洁美科技
301566 达利凯普|603678 火炬电子"""

DAYS = ["2026-09-11", "2026-09-14", "2026-09-15", "2026-09-16", "2026-09-17", "2026-09-18"]
# 腿：周一收盘买→周二收盘卖, ..., 周四收盘买→周五收盘卖
LEGS = [("2026-09-14", "2026-09-15"), ("2026-09-15", "2026-09-16"),
        ("2026-09-16", "2026-09-17"), ("2026-09-17", "2026-09-18")]

def limit_pct(code):
    return 0.20 if code[:3] in ("300", "301", "688") else 0.10

stocks = {}
for part in POOL.split("|"):
    for tok in part.split("\n"):
        tok = tok.strip()
        if not tok: continue
        code, name = tok.split()
        rows = {}
        with open(f"data/ai_stocks/{code}.csv", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                rows[r["date"]] = {k: float(r[k]) for k in ("open", "high", "low", "close")}
        if all(d in rows for d in DAYS):
            stocks[code] = (name, rows)
        else:
            print(f"警告: {code} {name} 本周数据不全，剔除")

def run(exclude_688=False):
    pool = {c: v for c, v in stocks.items() if not (exclude_688 and c.startswith("688"))}
    capital, path = 400000.0, []
    for buy_d, sell_d in LEGS:
        cands = []
        for code, (name, rows) in pool.items():
            prev_d = DAYS[DAYS.index(buy_d) - 1]
            c_prev, c_buy, c_sell = rows[prev_d]["close"], rows[buy_d]["close"], rows[sell_d]["close"]
            lp = limit_pct(code)
            if c_buy >= c_prev * (1 + lp - 0.001):   # 买入日收盘涨停，买不进
                continue
            if c_sell <= c_buy * (1 - lp + 0.001):   # 卖出日收盘跌停，卖不出
                continue
            cands.append((c_sell / c_buy - 1, code, name, c_buy, c_sell))
        cands.sort(reverse=True)
        ret, code, name, c_buy, c_sell = cands[0]
        capital *= 1 + ret
        path.append((buy_d, sell_d, code, name, c_buy, c_sell, ret, capital))
    return path, capital

def report(tag, path, capital):
    print(f"\n===== {tag} =====")
    for buy_d, sell_d, code, name, c_buy, c_sell, ret, cap in path:
        print(f"  {buy_d} 收盘 {c_buy:>9.2f} 买 {name}({code}) → {sell_d} 收盘 {c_sell:>9.2f} 卖  当日 {ret*100:+6.2f}%  市值 {cap/10000:.2f}万")
    print(f"  一周总收益: {(capital/400000-1)*100:+.2f}%   期末 {capital/10000:.2f}万")

p1, c1 = run(False)
report("全池(含科创板)", p1, c1)
p2, c2 = run(True)
report("剔除科创板(40万达不到科创板50万门槛)", p2, c2)

# 对照1：全周持有一只不动（周一收盘买、周五收盘卖）
print("\n===== 对照：全周抱一只不动 top5 =====")
hold = []
for code, (name, rows) in stocks.items():
    r = rows["2026-09-18"]["close"] / rows["2026-09-14"]["close"] - 1
    hold.append((r, code, name))
hold.sort(reverse=True)
for r, code, name in hold[:5]:
    print(f"  {name}({code})  全周 {r*100:+.2f}%")

# 对照2：每日个股涨幅榜（看钱都在哪）
print("\n===== 每日池内涨幅前5（收盘对收盘） =====")
for buy_d, sell_d in LEGS:
    day_ret = sorted(((rows[sell_d]["close"] / rows[buy_d]["close"] - 1, code, name)
                      for code, (name, rows) in stocks.items()), reverse=True)
    print(f"  {sell_d}: " + "  ".join(f"{n} {r*100:+.1f}%" for r, _, n in day_ret[:5]))
