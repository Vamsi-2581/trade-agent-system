# Ticket 1: 回测滑点模型 + cost_detail 成本明细

## 依赖
无（tracer-bullet 第一张）。

## 背景
`BacktestEngine`（`backend/apps/strategy_engine/backtest_mode.py`）目前只有 `commission_rate`（默认 0.1%），无滑点。"可靠策略"的验收（夏普≥1.5）在无成本模型上不成立。

## 需求
1. `BacktestEngine.__init__` 新增 `slippage_rate: Decimal = Decimal("0.0005")`。
2. 成交价模型：
   - 买入 `fill_price = signal_price * (1 + slippage_rate)`
   - 卖出 `fill_price = signal_price * (1 - slippage_rate)`
   - `_execute_buy` 中 `max_qty = self._cash / (fill_price * (1 + slippage_rate + commission_rate))`，保证现金边界含全部成本。
3. `_compute_stats` 与每笔 trade 记录新增 `slippage` 成本项。
4. `BacktestResult`（`backend/apps/backtest/models.py`）新增：
   ```python
   cost_detail = models.JSONField(default=dict, blank=True)
   # {"commission": 12.3, "slippage": 4.5, "funding": 0.0}
   ```
   迁移文件一并生成。
5. 回测请求参数/API schema 新增 `slippage_rate`（默认 `0.0005`），透传到引擎。

## 验收
- [ ] 买入/卖出成交价含滑点，方向正确（买贵卖便宜）。
- [ ] 现金刚好不足（含滑点+佣金）时 `max_qty` 正确缩量。
- [ ] `cost_detail.slippage` 累计等于各笔滑点之和。
- [ ] 现有回测测试全部通过（默认滑点会改变既有期望值——逐个核对并更新，不允许删除断言）。
- [ ] `pytest apps/backtest/tests apps/strategy_engine/tests` 通过；ruff/pyright 干净。

## 边界
- 只做固定比例滑点，不做成交量相关滑点模型。
