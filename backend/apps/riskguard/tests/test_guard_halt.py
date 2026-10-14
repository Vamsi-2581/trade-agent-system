"""前置校验第 0 步：停止判定（halt 状态机）在**下单通路**上的接线。

第②段把 halt 状态机接到了 `RiskGuard.pre_trade_check` 的最前面。这个文件钉的是那条线
上的四件事，每一件都只会在出事的时候才看得出来：

1. **它排在 ``user_id`` 判空之前**。后面四步问的都是「这个人还能不能下单」（次数、仓位、
   回撤都是按人的额度），而停止判定问的是「这个系统此刻还让不让开新仓」——不按人裁剪
   （CONTEXT.md:172）。放在判空之后的话，任何拿不到 ``user_id`` 的调用点（纸面交易、
   内部路径）都能绕过熔断，而熔断恰恰是最不该有例外的那个。
2. **减仓单整单免除前置校验**（豁免语义在 guard 内唯一定义，2026-10-12 合一）。熔断想停
   的是「加仓」，而减仓是熔断时唯一想让它动起来的事（CONTEXT.md:47）；次数/仓位/回撤问的
   都是「这个人还能不能开新仓」，减仓单不是开仓。这里也顺带把「查询失败怎么办」的例外
   消灭了：豁免写在入口，不写在判定里。
3. **查不出状态就按保守方向处理**（Q2 的 fail-closed）。反方向会让「数据库挂了」变成一次
   无声的机制失效，而那正是熔断最需要生效的时刻。**这条只对开仓单成立**——减仓在入口就
   被免除了，所以数据库挂了也拦不住一次减仓。
4. **拒绝必须让用户看见**。走 `_reject`（不是裸返回），与后面四步同一条出口；但无
   ``user_id`` 时不走——「发给谁」那时答不出来，而判定的结果不变。

**本文件是 `unittest`（没有数据库访问）**，这不是省事，是**强制**：第 0 步唯一真读库的
地方是 `_halt_block_reason` 这一个方法，别处碰库就会当场 RuntimeError。所以本文件里
「没打桩就必然 fail-closed」这件事本身，就是上面第 3 条的实证（见
`TestLookupFailure`）。
"""

from __future__ import annotations

import unittest
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from apps.riskguard.guard import HALT_LOOKUP_FAILED_REASON, RiskGuard
from apps.trading.adapters.base import OrderRequest

REASON = "停止判定命中：FOMC 议息（事件熔断，作用域 全市场（global））"


def _request(*, reduce_only: bool = False) -> OrderRequest:
    return OrderRequest(
        exchange="binance",
        symbol="BTCUSDT",
        order_type="market",
        side="buy",
        quantity=Decimal("0.001"),
        price=None,
        reduce_only=reduce_only,
    )


class _HaltGuardTestBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.guard = RiskGuard(mode="trading")
        RiskGuard._instance = None
        self.addCleanup(setattr, RiskGuard, "_instance", None)

    def _patch_notify(self, delivered=True):
        mock = AsyncMock(return_value=delivered)
        patcher = patch("apps.trading.alerts.notify_user", mock)
        patcher.start()
        self.addCleanup(patcher.stop)
        return mock

    def _patch_later_steps(self):
        """把第 1~4 步全部桩成放行：本文件只关心第 0 步。"""
        for name, value in (
            ("_get_daily_trade_count", 0),
            ("_check_position_limit", (True, "")),
            ("_check_drawdown", (True, "")),
        ):
            patcher = patch.object(self.guard, name, AsyncMock(return_value=value))
            patcher.start()
            self.addCleanup(patcher.stop)


class TestBlockedOrder(_HaltGuardTestBase):
    """有层在拦时：拒单，且理由原样来自 halt 状态机。"""

    async def test_a_blocking_declaration_rejects_and_notifies(self):
        notify = self._patch_notify()
        self._patch_later_steps()
        with patch.object(
            self.guard, "_halt_block_reason", AsyncMock(return_value=REASON)
        ):
            approved, reason = await self.guard.pre_trade_check(_request(), "user-1")

        self.assertFalse(approved)
        # 理由原样透传，不在这里改写：改写会让「工具里说 A、下单被拒时说 B」。
        self.assertEqual(reason, REASON)
        notify.assert_awaited_once()
        self.assertIn("风控拦截", notify.await_args.args[1])
        self.assertIn("FOMC 议息", notify.await_args.args[1])

    async def test_a_blocking_declaration_rejects_even_without_a_user(self):
        """**停止判定不按人裁剪**，所以无 user_id 也拦。

        这条是第 0 步排在判空之前那个决定的可执行形态。反过来的话，任何拿不到 user_id
        的调用点都是一条绕过熔断的路。
        """
        notify = self._patch_notify()
        self._patch_later_steps()
        with patch.object(
            self.guard, "_halt_block_reason", AsyncMock(return_value=REASON)
        ):
            approved, reason = await self.guard.pre_trade_check(_request(), None)

        self.assertFalse(approved)
        self.assertEqual(reason, REASON)
        # 判定的结果不变，但不走 `_reject`：那时「发给谁」答不出来。
        notify.assert_not_awaited()

    async def test_the_rest_of_the_chain_is_not_reached(self):
        """被第 0 步拒掉之后，后面四步一次都不该跑。

        不只是省几次查询：后面几步的拒绝理由（次数/仓位/回撤）会覆盖掉真正的停机原因，
        于是用户看到「日内交易次数已达上限」而实际是熔断——两个都对不上的口径。
        """
        self._patch_notify()
        self._patch_later_steps()
        with patch.object(
            self.guard, "_halt_block_reason", AsyncMock(return_value=REASON)
        ):
            with patch.object(
                self.guard, "_get_daily_trade_count", AsyncMock(return_value=999)
            ) as count:
                approved, reason = await self.guard.pre_trade_check(
                    _request(), "user-1"
                )

        self.assertFalse(approved)
        self.assertEqual(reason, REASON)
        count.assert_not_awaited()


class TestReduceOnly(_HaltGuardTestBase):
    """减仓单在入口就整单免除前置校验（豁免语义定义在 guard 内）。"""

    async def test_reduce_only_never_consults_the_halt_state(self):
        self._patch_notify()
        self._patch_later_steps()
        with patch.object(
            self.guard, "_halt_block_reason", AsyncMock(return_value=REASON)
        ) as halt:
            approved, reason = await self.guard.pre_trade_check(
                _request(reduce_only=True), "user-1"
            )

        self.assertTrue(approved, reason)
        halt.assert_not_awaited()

    async def test_reduce_only_is_exempt_from_every_step(self):
        """豁免是**整单**的：次数/仓位/回撤全拒，减仓单也必须放行。

        2026-10-12 合一前的旧语义是「只跳第 0 步，减仓仍要过后面的步」——那正是
        「这张单拦不拦」的第二个答案。若这条变红，说明实现退回了旧语义（例如把
        豁免挪回了某个入口的预跳过），而那个改动不会让别的测试变红。
        """
        self._patch_notify()
        mocks = {}
        for name, value in (
            ("_halt_block_reason", REASON),
            ("_get_daily_trade_count", 999),
            ("_check_position_limit", (False, "单笔仓位 50.0% 超过上限 20%")),
            ("_check_drawdown", (False, "日内回撤 8.0% 超过上限 5%")),
        ):
            mock = AsyncMock(return_value=value)
            patcher = patch.object(self.guard, name, mock)
            patcher.start()
            self.addCleanup(patcher.stop)
            mocks[name] = mock

        approved, reason = await self.guard.pre_trade_check(
            _request(reduce_only=True), "user-1"
        )

        self.assertTrue(approved, reason)
        for name, mock in mocks.items():
            mock.assert_not_awaited()  # type: ignore[attr-defined]

    async def test_reduce_only_survives_a_lookup_failure(self):
        """数据库挂了也拦不住一次减仓——因为压根没查。

        **不打桩 `_halt_block_reason`**：没有数据库的测试里它必然抛错。开仓单此时会被
        fail-closed 拒掉（见 `TestLookupFailure`），减仓单必须照常通过。
        """
        self._patch_notify()
        self._patch_later_steps()
        approved, reason = await self.guard.pre_trade_check(
            _request(reduce_only=True), "user-1"
        )

        self.assertTrue(approved, reason)


class TestLookupFailure(_HaltGuardTestBase):
    """查不出停止状态 → 按保守方向处理，**只拦开新仓**。"""

    async def test_a_failed_lookup_rejects_an_open_order(self):
        notify = self._patch_notify()
        self._patch_later_steps()
        with self.assertLogs("apps.riskguard.guard", level="ERROR") as logs:
            # 刻意不走 `_halt_block_reason` 的打桩：真实实现要读库，而这个测试类没有
            # 数据库访问权限。**它抛错这件事就是「查询失败」的实证**。
            approved, reason = await self.guard.pre_trade_check(_request(), "user-1")

        self.assertFalse(approved)
        self.assertEqual(reason, HALT_LOOKUP_FAILED_REASON)
        self.assertTrue(any("按保守方向处理" in line for line in logs.output))
        # 拒绝同样要让用户看见，不能因为「理由是我方故障」就闭嘴。
        notify.assert_awaited_once()
        self.assertIn("按保守方向处理", notify.await_args.args[1])

    async def test_a_failed_lookup_rejects_even_without_a_user(self):
        notify = self._patch_notify()
        self._patch_later_steps()
        with self.assertLogs("apps.riskguard.guard", level="ERROR"):
            approved, reason = await self.guard.pre_trade_check(_request(), None)

        self.assertFalse(approved)
        self.assertEqual(reason, HALT_LOOKUP_FAILED_REASON)
        notify.assert_not_awaited()


class TestTheSeam(_HaltGuardTestBase):
    """`_halt_block_reason` 是唯一取数口，它问的是**这一张单**。"""

    async def test_it_asks_about_this_orders_symbol(self):
        seen = []

        async def track(request):
            seen.append(request.symbol)
            return ""

        self._patch_notify()
        self._patch_later_steps()
        with patch.object(self.guard, "_halt_block_reason", track):
            await self.guard.pre_trade_check(_request(), "user-1")

        self.assertEqual(seen, ["BTCUSDT"])

    async def test_the_real_seam_asks_the_halt_state_machine(self):
        """真实现必须把问题交给 `apps.regime.halt`（ADR 0001 的唯一停止抽象）。

        钉的是**调用形状**：符号透传、`strategy_id` 为 `None`（第②段的下单通路上拿不到
        它）。第③段把 id 从 `live_session_id` 带下来时，这条会红——那正是它该红的时候。
        """
        from apps.regime import halt

        with patch.object(halt, "halt_layers") as layers:
            layers.return_value.reason = REASON
            reason = await self.guard._halt_block_reason(_request())

        self.assertEqual(reason, REASON)
        layers.assert_called_once_with("BTCUSDT", None)
