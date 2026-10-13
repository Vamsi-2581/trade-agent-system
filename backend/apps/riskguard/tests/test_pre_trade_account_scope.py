"""风控前置校验必须按**本单的账户**解析适配器（2026-09-22）。

背景：`_check_position_limit` 原按**交易所名**取一张以交易所名为键的适配器表。那张表
在同一交易所有两个活跃账户时只有「最后加载的」那个在里面。于是：

  * 给 A 账户下的单，可能拿 **B 账户的余额**当分母算仓位上限——上限是用别人的钱算的，
    而且两边日志都写着 `binance`，事后完全看不出来；
  * 更糟的是不报错，只是安静地用一个错的阈值放行或拒单。

修法是让 `OrderRequest` 带上 `exchange_account_id`（下单路径与 SignalDispatcher 都填），
风控用 `OrderExecutor.resolve_adapter(request.exchange_account_id)` 解析——与
`OrderExecutor.submit_order` 使用的是**同一个**适配器。

契约：
  1. 同一个 OrderRequest，只换账户 id，仓位判定可以不同（分母就是那个账户的余额）。
  2. 账户 id 明明给了却解析不到适配器 → **放行**，且绝不去动同交易所另一个账户。
  3. 拿不到账户 id 的调用方（纸面交易）**拿不到适配器** → 放行。按名字索引的那张表
     已删除（2026-10-12），所以这里不再有「按交易所名回退」这条路——那条路拿到的会是
     同交易所另一个账户的余额，正是本文件要消灭的东西。
  4. guard 只走公开 interface，不跨 app 摸 `OrderExecutor` 的私有方法。
"""

from __future__ import annotations

import unittest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from apps.riskguard.guard import RiskGuard
from apps.trading.adapters.base import OrderRequest
from apps.trading.executor import OrderExecutor


def _adapter(balance: str):
    adapter = MagicMock()
    adapter.get_balance = AsyncMock(return_value={"USDT": Decimal(balance)})
    return adapter


def _request(account_id: str | None):
    """qty 1 × price 50000 = 5 万 USDT 的单。"""
    return OrderRequest(
        exchange="binance",
        symbol="BTCUSDT",
        order_type="limit",
        side="buy",
        quantity=Decimal("1"),
        price=Decimal("50000"),
        exchange_account_id=account_id,
    )


class TestRiskCheckIsScopedToTheOrderAccount(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.guard = RiskGuard("trading")
        self.small = _adapter("100000")  # 5 万 / 10 万 = 50% > 20% → 拒
        self.big = _adapter("1000000")  # 5 万 / 100 万 = 5%  → 过
        self.executor = OrderExecutor()
        self.executor._account_adapters = {"acct-small": self.small, "acct-big": self.big}

    def tearDown(self):
        OrderExecutor._instance = None

    def _patch_executor(self):
        return patch(
            "apps.trading.executor.OrderExecutor.get_instance",
            return_value=self.executor,
        )

    async def test_same_order_verdict_follows_the_account_id(self):
        """只换账户 id，仓位判定必须跟着变——分母是**那个账户**的余额。"""
        with self._patch_executor():
            rejected, reason = await self.guard._check_position_limit(
                _request("acct-small"), "user-1"
            )
        self.assertFalse(rejected, "小账户下 5 万是 50%，必须拒")
        self.assertIn("超过上限", reason)

        with self._patch_executor():
            approved, _ = await self.guard._check_position_limit(
                _request("acct-big"), "user-1"
            )
        self.assertTrue(approved, "大账户下 5 万只有 5%")

        self.small.get_balance.assert_awaited_once()
        self.big.get_balance.assert_awaited_once()

    async def test_unknown_account_is_not_silently_replaced_by_another_one(self):
        """账户 id 解析不到适配器 → 放行，但**绝不动同交易所另一个账户**。

        这正是 `resolve_adapter` 拒绝按名字回退的原因：回退拿到的会是别人的余额，
        于是本单的风控结论建立在另一个账户的钱上，而日志里两边都叫 binance。
        """
        with self._patch_executor():
            approved, _ = await self.guard._check_position_limit(
                _request("acct-missing"), "user-1"
            )
        self.assertTrue(approved, "取不到适配器时与旧行为一致：放行")
        self.big.get_balance.assert_not_awaited()
        self.small.get_balance.assert_not_awaited()

    async def test_caller_without_account_id_gets_no_adapter(self):
        """纸面交易等拿不到账户 id 的调用方：解析不出适配器 → 放行。

        2026-10-12 之前这里会「按交易所名回退」拿到 acct-big 的余额；那条路已随按名字
        索引的表一起删除——它拿到的钱不是本单账户的。
        """
        with self._patch_executor():
            approved, _ = await self.guard._check_position_limit(
                _request(None), "user-1"
            )
        self.assertTrue(approved, "取不到适配器时放行（与旧行为一致）")
        self.big.get_balance.assert_not_awaited()
        self.small.get_balance.assert_not_awaited()

    async def test_guard_uses_the_public_resolve_interface(self):
        """guard 走公开 `resolve_adapter`；私有解析方法已收口（不再存在）。"""
        with self._patch_executor():
            await self.guard._check_position_limit(_request("acct-big"), "user-1")

        self.assertFalse(
            hasattr(self.executor, "_resolve_adapter"),
            "私有解析方法必须已收口成公开的 resolve_adapter",
        )
        self.assertTrue(hasattr(self.executor, "resolve_adapter"))
