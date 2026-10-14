"""平仓信号走 guard 的 reduce_only 豁免，不在入口预跳过（2026-10-12 豁免语义合一）。

合一前 `dispatch_with_risk_check` 对 `is_close_position=True` 直接 `dispatch`，不调
`pre_trade_check`——与 executor 的预跳过各成一个「答案」，而 guard 内旧分支只免 halt。
合一后三个入口（executor / signals / 直接调用）都只传标志，「这张单拦不拦」由
`RiskGuard.pre_trade_check` 的 `reduce_only` 分支单独定义。本文件钉 signals 这一入口：

1. 平仓信号仍然调用 `pre_trade_check`，且传下去的是 `reduce_only=True`；
2. 开仓信号传 `reduce_only=False`，guard 拒单时不分发；
3. 即使 guard 拒绝一张 reduce_only 单（语义变化时的防御），dispatcher 也不分发。
"""

from __future__ import annotations

import unittest
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from apps.strategy_engine.base import OrderSignal
from apps.strategy_engine.signals import SignalDispatcher


class _RecordingRiskGuard:
    """记录收到的 OrderRequest，按预设裁决。"""

    def __init__(self, approved: bool = True, reason: str = "OK"):
        self.requests: list = []
        self.approved = approved
        self.reason = reason

    async def pre_trade_check(self, request, user_id):
        self.requests.append(request)
        return self.approved, self.reason


def _signal(is_close: bool) -> OrderSignal:
    return OrderSignal(
        side="sell" if is_close else "buy",
        quantity=Decimal("1.37"),
        order_type="market",
        exchange="binance",
        signal_name="close" if is_close else "entry",
        is_close_position=is_close,
    )


class TestDispatchCloseExemption(unittest.IsolatedAsyncioTestCase):
    async def _run(self, signal: OrderSignal, guard: _RecordingRiskGuard):
        dispatcher = SignalDispatcher()
        with (
            patch("apps.riskguard.guard.RiskGuard.get_instance", return_value=guard),
            patch.object(
                dispatcher, "dispatch", AsyncMock(return_value="msg-1")
            ) as dispatch,
        ):
            msg_id = await dispatcher.dispatch_with_risk_check(
                signal=signal,
                symbol="BNBUSDT",
                exchange_account_id="acct-1",
                user_id="user-1",
            )
        return msg_id, dispatch

    async def test_close_signal_goes_through_guard_with_reduce_only(self):
        """平仓信号不再预跳过：走 pre_trade_check，标志是 reduce_only=True。"""
        guard = _RecordingRiskGuard()
        msg_id, dispatch = await self._run(_signal(is_close=True), guard)

        self.assertEqual(msg_id, "msg-1")
        self.assertEqual(
            len(guard.requests),
            1,
            "平仓信号必须仍走 pre_trade_check：入口预跳过会再造一个答案",
        )
        self.assertTrue(
            guard.requests[0].reduce_only,
            "平仓信号的豁免标志是 reduce_only，豁免语义在 guard 内定义",
        )
        self.assertEqual(guard.requests[0].exchange_account_id, "acct-1")
        dispatch.assert_awaited_once()

    async def test_open_signal_carries_reduce_only_false(self):
        guard = _RecordingRiskGuard()
        msg_id, dispatch = await self._run(_signal(is_close=False), guard)

        self.assertEqual(msg_id, "msg-1")
        self.assertEqual(len(guard.requests), 1)
        self.assertFalse(guard.requests[0].reduce_only)
        dispatch.assert_awaited_once()

    async def test_rejected_open_signal_is_not_dispatched(self):
        guard = _RecordingRiskGuard(approved=False, reason="日内回撤 8.0% 超过上限 5%")
        msg_id, dispatch = await self._run(_signal(is_close=False), guard)

        self.assertIsNone(msg_id)
        dispatch.assert_not_awaited()

    async def test_rejected_reduce_only_signal_is_not_dispatched(self):
        """防御：dispatcher 只信 guard 的裁决。若未来豁免语义变了（reduce_only 不再
        整单免），这里必须跟着拒单，而不是退回「入口预跳过」的旧行为。"""
        guard = _RecordingRiskGuard(approved=False, reason="语义变化示例")
        msg_id, dispatch = await self._run(_signal(is_close=True), guard)

        self.assertIsNone(msg_id)
        dispatch.assert_not_awaited()
