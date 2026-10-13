"""guard 不可用时 signals 入口 fail-closed（架构评审 #5 · fail-open 家族收口）。

收口前 `dispatch_with_risk_check` 在 `RiskGuard.get_instance()` 返回 None 时只打
warning 照常分发——guard 本体是 fail-closed，家族边界却是 fail-open。收口后
guard 不可用 = 拒单（返回 None、不分发），与 guard 本体「查不出状态就拒单」
取向一致。
"""

from __future__ import annotations

import unittest
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from apps.strategy_engine.base import OrderSignal
from apps.strategy_engine.signals import SignalDispatcher


def _signal() -> OrderSignal:
    return OrderSignal(
        side="buy",
        quantity=Decimal("1.0"),
        order_type="limit",
        exchange="binance",
        signal_name="entry",
    )


class TestDispatchGuardUnavailable(unittest.IsolatedAsyncioTestCase):
    async def test_guard_none_rejects_signal(self):
        """guard 不实例化 → 拒单（返回 None），信号不得旁路 guard 出场。"""
        dispatcher = SignalDispatcher()
        with (
            patch("apps.riskguard.guard.RiskGuard.get_instance", return_value=None),
            patch.object(
                dispatcher, "dispatch", AsyncMock(return_value="msg-1")
            ) as dispatch,
        ):
            msg_id = await dispatcher.dispatch_with_risk_check(
                signal=_signal(),
                symbol="BNBUSDT",
                exchange_account_id="acct-1",
                user_id="user-1",
            )

        self.assertIsNone(msg_id)
        dispatch.assert_not_awaited()

    async def test_guard_still_none_keeps_none_semantics(self):
        """拒单口径是稳定返回值：None = 拒，调用方（live_mode）按 None 丢弃，
        不需要区分「被拦」和「guard 缺席」两种 None。"""
        dispatcher = SignalDispatcher()
        with (
            patch("apps.riskguard.guard.RiskGuard.get_instance", return_value=None),
            patch.object(dispatcher, "dispatch", AsyncMock()) as dispatch,
        ):
            first = await dispatcher.dispatch_with_risk_check(
                signal=_signal(),
                symbol="BNBUSDT",
                exchange_account_id="acct-1",
                user_id=None,
            )
            second = await dispatcher.dispatch_with_risk_check(
                signal=_signal(),
                symbol="BNBUSDT",
                exchange_account_id="acct-1",
                user_id=None,
            )

        self.assertIsNone(first)
        self.assertIsNone(second)
        dispatch.assert_not_awaited()
