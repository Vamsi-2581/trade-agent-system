# Ticket 3: RiskGuard 合约模式三条校验

## 依赖
无（与 Ticket 1/2 并行，文件域不相交：`apps/riskguard/` vs `apps/backtest/`）。

## 背景
现货口径风控（`guard.py:96-98`：单仓 20%、日回撤 5%、日交易 50 次）不适用于 ≤5x 合约。共识参数：杠杆 ≤5x、合约单仓 10%（名义 ≤50%）、组合总名义敞口硬顶 100%。

## 需求
1. 新常量：
   ```python
   MAX_LEVERAGE = 5
   MAX_POSITION_RATIO_PERP = Decimal("0.10")
   MAX_TOTAL_NOTIONAL_EXPOSURE = Decimal("1.00")
   ```
2. `OrderRequest` 补 `leverage: int = 1` 字段（`apps/trading` 适配层透传，Binance 下单参数/交易所规则均可来源）。
3. `pre_trade_check` 校验链扩展（插在现有 step 3 单仓检查附近）：
   - **3a 杠杆**：`request.leverage > MAX_LEVERAGE` → 拒。
   - **3b 合约单仓**：perp 标的（symbol 后缀或 request 标志）用 `MAX_POSITION_RATIO_PERP`；现货维持 0.20 不回退。
   - **3c 总名义敞口**：`Σ|position_value × leverage| / total_equity > MAX_TOTAL_NOTIONAL_EXPOSURE` → 拒。持仓查询复用/新增 `get_open_positions(user_id)`（优先复用 `apps/trading` 持仓同步现有查询）。
4. reduce_only 平仓单整段豁免语义不变（现有 `TestReduceOnly` 钉死的行为不回归）。
5. 每次新拒单写 `RiskEvent`（type=hard_limit），reason 带具体数字（如 `leverage 8x > MAX_LEVERAGE 5x`）。

## 验收
- [ ] 5x 过 / 5.1x（或 6x）拒。
- [ ] perp 单仓 10% 边界：9.9% 过 / 10.1% 拒；现货 20% 用例不回归。
- [ ] 总敞口：99% 过 / 101% 拒（构造两持仓场景）。
- [ ] reduce_only 全部现有测试通过。
- [ ] RiskEvent 记录含参数化 reason。
- [ ] ruff/pyright 干净；`pytest apps/riskguard/tests apps/trading/tests` 通过。

## 边界
- 不做杠杆动态建议，不做分策略敞口配额（P3 组合层的事）。
