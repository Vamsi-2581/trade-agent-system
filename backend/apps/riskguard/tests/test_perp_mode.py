"""Ticket 3：RiskGuard 合约（perp）模式三条校验。

现货口径（单仓 20%）不适用于 ≤5x 合约。本文件钉死：

1. **3a 杠杆**：``request.leverage > MAX_LEVERAGE (5)`` → 拒，reason 带具体数字。
2. **3b 合约单仓**：perp 标的单仓上限 10%（``MAX_POSITION_RATIO_PERP``）；现货
   维持 20% 不回退（``test_guard.py`` 的现货用例照旧）。
3. **3c 总名义敞口**：``Σ|position_value × leverage| / total_equity >
   MAX_TOTAL_NOTIONAL_EXPOSURE (100%)`` → 拒。
4. reduce_only 平仓单整段豁免语义不变（``test_guard_halt.py::TestReduceOnly``）。
5. 每次新拒单写 ``RiskEvent``（type=hard_limit），message 带具体数字。

perp 判定：``OrderRequest.is_perp`` 标志，或 symbol 以 ``-SWAP`` 结尾（OKX 风格）。
"""

from __future__ import annotations

import unittest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apps.riskguard.guard import RiskGuard
from apps.trading.adapters.base import OrderRequest, Position
from apps.trading.executor import OrderExecutor


def _adapter(balance: str, positions: list[Position] | None = None):
    adapter = MagicMock()
    adapter.get_balance = AsyncMock(return_value={"USDT": Decimal(balance)})
    adapter.get_positions = AsyncMock(return_value=positions or [])
    return adapter


def _request(
    *,
    symbol: str = "BTCUSDT",
    leverage: int = 1,
    is_perp: bool = False,
    price: str = "50000",
    quantity: str = "1",
    account_id: str = "acct-1",
    reduce_only: bool = False,
) -> OrderRequest:
    return OrderRequest(
        exchange="binance",
        symbol=symbol,
        order_type="limit",
        side="buy",
        quantity=Decimal(quantity),
        price=Decimal(price),
        exchange_account_id=account_id,
        leverage=leverage,
        is_perp=is_perp,
        reduce_only=reduce_only,
    )


def _position(notional: str, leverage: int = 1) -> Position:
    """quantity × entry_price = notional 的一个持仓。"""
    qty = Decimal("1")
    entry = Decimal(notional) / qty
    return Position(
        symbol="ETHUSDT",
        side="long",
        quantity=qty,
        entry_price=entry,
        unrealized_pnl=Decimal("0"),
        leverage=leverage,
        mark_price=entry,
    )


class PerpModeTestBase(unittest.IsolatedAsyncioTestCase):
    """公共桩：halt 放行、日次数放行、回撤放行；只暴露被测的三条校验。"""

    def setUp(self):
        self.guard = RiskGuard(mode="trading")
        RiskGuard._instance = None
        self.adapter = _adapter("100000")
        self.executor = OrderExecutor()
        self.executor._account_adapters = {"acct-1": self.adapter}

        for target, value in (
            ("_halt_block_reason", AsyncMock(return_value="")),
            ("_get_daily_trade_count", AsyncMock(return_value=0)),
            ("_check_drawdown", AsyncMock(return_value=(True, ""))),
        ):
            patcher = patch.object(self.guard, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

        patcher = patch(
            "apps.trading.executor.OrderExecutor.get_instance",
            return_value=self.executor,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        # 拒单通知不真写库
        notify_patcher = patch(
            "apps.trading.alerts.notify_user", AsyncMock(return_value=True)
        )
        notify_patcher.start()
        self.addCleanup(notify_patcher.stop)

    def tearDown(self):
        RiskGuard._instance = None
        OrderExecutor._instance = None


class TestLeverageLimit(PerpModeTestBase):
    """3a：杠杆 ≤5x 过，>5x 拒，reason 带具体数字，并写 RiskEvent。"""

    async def test_5x_passes(self):
        # 名义 100×1=100，×5x 杠杆 = 500 敞口，占比 0.5% —— 远低于各上限
        ok, reason = await self.guard.pre_trade_check(
            _request(price="100", quantity="1", leverage=5, is_perp=True), "user-1"
        )
        self.assertTrue(ok, reason)
        self.assertEqual(reason, "OK")

    @pytest.mark.django_db
    async def test_6x_rejected_with_numbers(self):
        ok, reason = await self.guard.pre_trade_check(
            _request(leverage=6, is_perp=True), "user-1"
        )
        self.assertFalse(ok)
        self.assertIn("6x", reason)
        self.assertIn("5x", reason)

    @pytest.mark.django_db
    async def test_rejection_writes_hard_limit_risk_event(self):
        from apps.riskguard.models import RiskEvent

        before = RiskEvent.objects.count()
        await self.guard.pre_trade_check(_request(leverage=8, is_perp=True), "user-1")
        events = list(RiskEvent.objects.all())
        self.assertEqual(RiskEvent.objects.count(), before + 1)
        event = events[-1]
        self.assertEqual(event.event_type, "hard_limit")
        self.assertIn("8x", event.message)


class TestPerpPositionRatio(PerpModeTestBase):
    """3b：perp 单仓 10% 边界；现货 20% 不回退。"""

    async def test_perp_9_9_percent_passes(self):
        # 100000 × 9.9% = 9900 名义
        ok, reason = await self.guard.pre_trade_check(
            _request(price="9900", quantity="1", is_perp=True), "user-1"
        )
        self.assertTrue(ok, reason)

    async def test_perp_10_1_percent_rejected(self):
        ok, reason = await self.guard.pre_trade_check(
            _request(price="10100", quantity="1", is_perp=True), "user-1"
        )
        self.assertFalse(ok)
        self.assertIn("10.1%", reason)

    async def test_spot_15_percent_still_passes(self):
        """现货上限是 20% 不是 10%：15% 必须放行（不回退）。"""
        ok, reason = await self.guard.pre_trade_check(
            _request(price="15000", quantity="1", is_perp=False), "user-1"
        )
        self.assertTrue(ok, reason)

    async def test_perp_by_swap_suffix(self):
        """symbol 后缀 -SWAP 也判为 perp（无 is_perp 标志时）。"""
        ok, reason = await self.guard.pre_trade_check(
            _request(symbol="BTC-USDT-SWAP", price="10100", is_perp=False), "user-1"
        )
        self.assertFalse(ok)

    @pytest.mark.django_db
    async def test_perp_rejection_writes_risk_event(self):
        from apps.riskguard.models import RiskEvent

        before = RiskEvent.objects.count()
        await self.guard.pre_trade_check(
            _request(price="20000", quantity="1", is_perp=True), "user-1"
        )
        self.assertEqual(RiskEvent.objects.count(), before + 1)


class TestTotalNotionalExposure(PerpModeTestBase):
    """3c：Σ|position_value × leverage| / equity ≤ 100%。"""

    async def test_99_percent_passes(self):
        # 50000×1 + 49000×1 = 99000 / 100000 = 99%
        self.adapter.get_positions = AsyncMock(
            return_value=[_position("50000"), _position("49000")]
        )
        ok, reason = await self.guard.pre_trade_check(
            _request(price="100", quantity="1", is_perp=True), "user-1"
        )
        self.assertTrue(ok, reason)

    async def test_101_percent_rejected(self):
        # 50000×1 + 52000×1 = 102000 / 100000 = 102%
        self.adapter.get_positions = AsyncMock(
            return_value=[_position("50000"), _position("52000")]
        )
        ok, reason = await self.guard.pre_trade_check(
            _request(price="100", quantity="1", is_perp=True), "user-1"
        )
        self.assertFalse(ok)
        self.assertIn("102", reason)

    @pytest.mark.django_db
    async def test_rejection_writes_risk_event(self):
        from apps.riskguard.models import RiskEvent

        self.adapter.get_positions = AsyncMock(
            return_value=[_position("50000"), _position("52000")]
        )
        before = RiskEvent.objects.count()
        await self.guard.pre_trade_check(
            _request(price="100", quantity="1", is_perp=True), "user-1"
        )
        self.assertEqual(RiskEvent.objects.count(), before + 1)


class TestReduceOnlyStillExempt(PerpModeTestBase):
    """reduce_only 平仓单整段豁免：新三条校验同样不得查它。"""

    async def test_reduce_only_bypasses_all_new_checks(self):
        # 杠杆 20x、仓位超限、持仓敞口爆表 —— 全都不该被问到
        self.adapter.get_positions = AsyncMock(
            return_value=[_position("500000"), _position("500000")]
        )
        ok, reason = await self.guard.pre_trade_check(
            _request(
                leverage=20,
                is_perp=True,
                price="90000",
                quantity="10",
                reduce_only=True,
            ),
            "user-1",
        )
        self.assertTrue(ok, reason)
        self.adapter.get_positions.assert_not_awaited()


class TestPerpDetectionHelper(unittest.TestCase):
    """_is_perp_request 的判定口径。"""

    def test_flag_true(self):
        self.assertTrue(
            RiskGuard._is_perp_request(_request(is_perp=True, symbol="BTCUSDT"))
        )

    def test_swap_suffix(self):
        self.assertTrue(
            RiskGuard._is_perp_request(_request(is_perp=False, symbol="BTC-SWAP"))
        )

    def test_spot_default(self):
        self.assertFalse(
            RiskGuard._is_perp_request(_request(is_perp=False, symbol="BTCUSDT"))
        )
