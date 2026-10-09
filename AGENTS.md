# AGENTS.md

## 子代理调度偏好

- 遇到相互独立的子任务（如多个优化方向的回测对比、多文件分析），默认拆成多个**后台并行** agent 执行，不要串行排队等待。
- 同类批量任务（对多个标的/参数/文件做同一种分析）优先使用 AgentSwarm 一次派发。
- 并行 agent 只新建各自的分析脚本和结果文件（`compare_*.py` / `result_*.txt`），不要改动生产文件（`export_json.py`、`backtest.py`、`gen_signals.py`、`site/`），集成由主 agent 统一做。
- 简单任务（一两步能完成的查找、小改）不要派 agent，直接做。
