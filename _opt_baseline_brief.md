# ETF 轮动策略优化实验：统一基准说明（所有对比实验共用）

你在 Windows 上的 `C:\Users\ccf\etf_rotation` 项目里做一个对比回测实验。运行 Python 用项目虚拟环境，在 Bash 工具里执行：`.venv/Scripts/python -X utf8 your_script.py`

## 现行策略（基准，所有对比以它为锚）

数据文件都在 `data/` 下，CSV 含 `date/open/high/low/close` 列。加载方式统一为：

```python
df = pd.read_csv(path, parse_dates=['date']).set_index('date').sort_index()
df.index = df.index.normalize()
```

- 信号源：`avg_880003.csv`（通达信"平均股价"指数）
- 进攻腿（后复权拼接）：2024-06-28 前 = `etf_512100_hfq.csv`（中证1000ETF），之后 = `etf_159552_hfq.csv`（中证2000增强ETF）：

```python
SWITCH = pd.Timestamp('2024-06-28')
etf_atk = pd.concat([etf1000[etf1000.index < SWITCH], etf2000e[etf2000e.index >= SWITCH]])
```

- 防守腿：`etf_512890_hfq.csv`（红利低波ETF，后复权）
- 另有 `etf_159201_hfq.csv`（自由现金流ETF，2025-02-27 上市）可用
- 信号：项目根目录 `backtest.py` 中的
  `calc_signals(avg['close'], sell_anywhere=True, fast=17, slow=34, sig_n=9)`
  ——MACD DIF 拐点：买 = DIF 零下拐头向上；卖 = DIF 转降（sell_anywhere=True 时零下也卖）
- 执行口径：T 日收盘出信号，T+1 开盘价成交，双边佣金各万1（fee=0.0001，run_backtest 在换仓日扣 2*fee）
- 回测调用：

```python
from backtest import run_backtest
res = run_backtest(avg, etf_atk, etfdiv, fee=0.0001, sell_anywhere=True,
                   fast=17, slow=34, sig_n=9, verbose=False)
```

  窗口自动取标的交集（2019-01-18 起至今 2026-09-23）。

- **重要**：`run_backtest` 支持 `signals=` 参数传入外部信号 DataFrame（index=日期，含 `buy_sig`/`sell_sig` 布尔列），传入后忽略内置 MACD 参数——你的自定义信号都通过它注入，保证执行/费用口径与基准完全一致。状态类策略（如动量持有强者）可转成点信号：只在状态翻转的日子置 buy_sig/sell_sig。
- **失真风险**：run_backtest 按 close-to-close 算收益，若在 2024-06-28 拼接切换日持有进攻仓，当日收益会跨标的失真。基准本身无此问题；你的变体若出现跨界持有，必须在结果中标注。
- 每个实验先复跑一次基准拿到锚点数字，再跑变体。

## 统一报告口径（写入 result 文件）

累计收益、年化收益、最大回撤、换仓次数、2026年YTD收益、2026-09当月收益、分年度收益表、≤5个交易日且亏损的"打脸段"数量与合计收益、切换日是否跨界持有进攻仓。与基准逐项对比。

## 反过拟合纪律（重要）

本项目历史上已做过 668/1740 组参数的大规模网格，结论是现行参数排名靠前，暴力网格收益递减。因此：

- 规则要简单、有经济学解释，参数组合控制在几十组以内；
- 报告分年度表现，某变体若只靠单一年份取胜，必须在结论里明说；
- 给出明确结论：是否优于基准、优在哪、代价是什么（换仓次数/回撤）、是否建议采纳。

## 交付纪律

- 只新建你自己的 `compare_*.py` 分析脚本和 `result_*.txt` 结果文件（文件名见你的实验方向）；
- **绝对不要修改** `export_json.py`、`backtest.py`、`gen_signals.py`、`ai_yellow_bar.py`、`server.py`、`site/` 下任何文件，也不要修改他人创建的 compare/result 文件；
- 脚本里打印关键过程，结果文件包含完整对比表和结论。
