"""候选策略流水线（策略工厂）：候选身份、四层漏斗判定、paper 观察与人工卡点。

为什么要一个独立 app（spec #19 D13）：漏斗不该塞进 `apps/strategy_engine/`（那会让
策略引擎变成会话编排者），也不该塞进 `apps/backtest/`（回测不该知道「候选」这个概念）。
依赖方向单向：`strategy_factory` → `strategy_engine` / `backtest` / `trading` /
`riskguard` / `exchange`，**反向零依赖**。

本票（#26，地基 T1）落了四块：状态机（`status.py`）、配置面（`config.py`）、判定纯函数
（`evaluate.py`）、候选模型（`models.py`）。分折、编排、paper 判定、API、生成侧登记
分别是后续子票。
"""
