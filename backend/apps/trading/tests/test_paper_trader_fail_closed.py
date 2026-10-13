"""PaperTrader 风控入口 fail-closed（架构评审 #5 · fail-open 家族收口）。

收口前：guard 不可用 / 校验抛错时回落到 `_hardcoded_risk_check`（≤30次/日、≤20%、
无 halt）——guard 本体是 fail-closed，家族边界却是 fail-open，且系统里同时存在
两套风控阈值。收口后第二套风控实现删除：

1. guard 不可用 → 拒单，理由里带 fail-closed 口径；
2. guard 校验抛错 → 拒单，不回落任何 fallback；
3. guard 正常放行 → 照常成交（不掉单）。
"""

from __future__ import annotations

import unittest
from decimal import Decimal
from unittest.mock import patch

from apps.trading.adapters.base import OrderRequest
from apps.trading.paper_trader import PaperTrader


def _request() -> OrderRequest:
    return OrderRequest(
        exchange="binance",
        symbol="BTCUSDT",
        order_type="limit",
        side="buy",
        quantity=Decimal("0.1"),
        price=Decimal("50000"),
    )


class _StubGuard:
    def __init__(self, approved: bool = True, reason: str = "OK"):
        self.approved = approved
        self.reason = reason
        self.checks: list = []

    async def pre_trade_check(self, request, user_id):
        self.checks.append((request, user_id))
        if self.approved:
            return True, self.reason
        return False, self.reason


class _BrokenGuard:
    async def pre_trade_check(self, request, user_id):
        raise RuntimeError("guard 内部故障")


class TestPaperTraderFailClosed(unittest.IsolatedAsyncioTestCase):
    def _trader(self):
        trader = PaperTrader()
        trader._running = True
        return trader

    async def test_guard_unavailable_rejects_order(self):
        """guard 不实例化 → 拒单，不再有第二套风控兜底。"""
        trader = self._trader()
        with patch("apps.riskguard.guard.RiskGuard.get_instance", return_value=None):
            approved, reason = await trader._pre_trade_risk_check(_request())
            response = await trader.place_order(_request())

        self.assertFalse(approved)
        self.assertIn("fail-closed", reason)
        self.assertEqual(response.status, "rejected")

    async def test_guard_error_rejects_order_without_fallback(self):
        """guard 校验抛错 → 拒单；硬编码 fallback 已删除。"""
        trader = self._trader()
        with patch(
            "apps.riskguard.guard.RiskGuard.get_instance",
            return_value=_BrokenGuard(),
        ):
            approved, reason = await trader._pre_trade_risk_check(_request())
            response = await trader.place_order(_request())

        self.assertFalse(approved)
        self.assertIn("fail-closed", reason)
        self.assertEqual(response.status, "rejected")

    async def test_guard_approval_still_fills(self):
        """guard 放行 → 照常走模拟成交，拒单口径没有误伤正常路径。"""
        trader = self._trader()
        stub = _StubGuard(approved=True)
        with patch("apps.riskguard.guard.RiskGuard.get_instance", return_value=stub):
            response = await trader.place_order(_request())

        self.assertEqual(response.status, "filled")
        self.assertEqual(len(stub.checks), 1)
        # user_id 固定为 "paper_trader"：guard 按它归集次数/回撤额度
        self.assertEqual(stub.checks[0][1], "paper_trader")

    async def test_guard_rejection_propagates_reason(self):
        """guard 拒单 → 拒单理由原样上抛，带 RiskGuard 前缀。"""
        trader = self._trader()
        stub = _StubGuard(approved=False, reason="日内交易次数已达上限 50")
        with patch("apps.riskguard.guard.RiskGuard.get_instance", return_value=stub):
            approved, reason = await trader._pre_trade_risk_check(_request())

        self.assertFalse(approved)
        self.assertIn("日内交易次数已达上限 50", reason)

    def test_hardcoded_fallback_is_gone(self):
        """第二套风控实现必须不存在：类上没有 fallback 方法与阈值兜底状态。"""
        self.assertFalse(hasattr(PaperTrader, "_hardcoded_risk_check"))
        self.assertFalse(hasattr(PaperTrader, "_calculate_total_assets"))
        self.assertFalse(hasattr(PaperTrader, "_increment_daily_trade_count"))
