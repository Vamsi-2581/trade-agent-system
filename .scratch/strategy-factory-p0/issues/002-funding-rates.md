# Ticket 2: 资金费率历史落库 + 回测接入

## 依赖
Ticket 1（复用 `cost_detail.funding` 字段）。

## 需求
1. 新模型 `FundingRateRecord`（`backend/apps/backtest/models.py`）：
   ```python
   symbol = models.CharField(max_length=32)
   funding_time = models.DateTimeField()
   rate = models.FloatField()
   # 唯一约束 (symbol, funding_time)
   ```
   迁移一并生成。
2. 新 Celery 任务 `sync_funding_rates(symbol)`：
   - ccxt binance `fetchFundingRateHistory`；
   - 增量：从该 symbol 已有最新 `funding_time` 起拉取；
   - upsert（唯一约束去重，勿逐条 try/except 吞错——批量 get_or_create 或 bulk + ON CONFLICT）。
3. Beat 调度（`celery_app.py`）：每 8h 对配置的主流币永续列表（BTC/ETH USDT 永续起步）同步。
4. `BacktestEngine.run()` 接入：
   - 判定永续：请求参数 `is_perp: bool = False`（或 symbol 以 `-SWAP` 结尾）；
   - 持仓期间每个 8h 结算点：`funding_payment = ±rate × |position_notional|`，多头在正费率时支付（负号），空头收取；计入现金并累计到 `cost_detail.funding`；
   - 数据缺失时段：跳过结算并记录 warning，**不静默假设 0**（日志里能看到缺口）。
5. 回测请求参数新增 `include_funding: bool = True`（仅 perp 生效）。

## 验收
- [ ] 多头正费率付费、空头收费率、负费率方向反转。
- [ ] 空仓期间不计费。
- [ ] `(symbol, funding_time)` 重复同步不产生重复行。
- [ ] 增量同步只拉新增（用 mock 验证请求参数中的起始时间）。
- [ ] 回测中资金费累计与手算一致（构造 3 个结算点的用例）。
- [ ] ruff/pyright 干净；`pytest apps/backtest/tests` 通过。

## 边界
- 不做实时资金费率预警，不做 OKX 费率。
