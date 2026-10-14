# P0：回测成本模型 + RiskGuard 合约模式

Spec 共识：见会话 grilling 访谈（2026-06-17）与 CONTEXT.md「策略工厂共识」。
可靠标准：夏普 ≥1.5 + 样本外衰减 <30% + paper-trade 2 周过风控。

## 工单（按依赖顺序）

| # | 工单 | 依赖 | 状态 |
|---|------|------|------|
| 1 | [回测滑点模型 + cost_detail](001-backtest-slippage.md) | 无 | ✅ done |
| 2 | [资金费率落库 + 回测接入](002-funding-rates.md) | #1 | ✅ done |
| 3 | [RiskGuard 合约模式三条校验](003-riskguard-perp-mode.md) | 无（并行） | ✅ done |

## 派生路线（P0 之后）
- P1：K线/资金费率落库 + 增量同步（#2 的 FundingRateRecord 是先行件）
- P2：候选策略流水线（生成→回测漏斗→paper→人工卡点）
- P3：regime 分类器（ADX+ATR 三态）+ 组合资金分配（等权→波动率目标）
