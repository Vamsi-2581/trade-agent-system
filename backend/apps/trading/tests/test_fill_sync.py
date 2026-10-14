"""Tests for OrderExecutor fill-sync (成交同步).

L2 单元测试：mock adapter + mock ORM，覆盖：
- 下单响应即时成交信息落库（市价单同步成交场景）
- 后台成交同步循环对各状态的映射
- 异常容错（adapter 异常 / 订单不存在）
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import unittest

from apps.trading.adapters.base import OrderFill, OrderNotFoundError
from apps.trading.executor import OrderExecutor


def _fake_order(**overrides):
    """构造一个轻量订单对象（仅含 fill-sync 需要的字段）。"""
    base = {
        "id": "order-uuid-1",
        "side": "buy",
        "symbol": "DOGE/USDT",
        "exchange_order_id": "2336219508",
        "status": "submitted",
        "filled_quantity": Decimal("0"),
        "avg_fill_price": None,
        "realized_pnl": None,
        "error_message": "",
        "exchange_account": SimpleNamespace(exchange="binance"),
        "exchange_account_id": "acc-1",
        "live_session_id": None,
        "quantity": Decimal("100"),
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class TestSubmitImmediateFill(unittest.TestCase):
    """下单响应包含即时成交信息时应立即落库。"""

    def setUp(self):
        self.executor = OrderExecutor()
        self.executor._running = True
        self.executor._riskguard = None
        self.executor._account_adapters = {}

    def tearDown(self):
        OrderExecutor._instance = None

    def _run_submit(self, **kwargs):
        return asyncio.run(self.executor.submit_order(**kwargs))

    @patch("apps.trading.models.Order.objects")
    def test_market_order_filled_synchronously(self, mock_order_mgr):
        """市价单同步成交：filled_qty==quantity → status=filled + 均价落库。"""
        mock_adapter = AsyncMock()
        mock_response = MagicMock()
        mock_response.exchange_order_id = "123456"
        mock_response.status = "FILLED"
        mock_response.filled_qty = Decimal("100")
        mock_response.avg_price = Decimal("0.1099")
        mock_adapter.place_order.return_value = mock_response
        self.executor._account_adapters = {"acc-1": mock_adapter}

        mock_order = MagicMock()
        mock_order.id = "order-uuid-1"
        mock_order_mgr.create.return_value = mock_order
        mock_update = MagicMock()
        mock_order_mgr.filter.return_value.update = mock_update

        result = self._run_submit(
            exchange="binance",
            symbol="DOGE/USDT",
            side="buy",
            order_type="market",
            quantity=Decimal("100"),
            price=None,
            exchange_account_id="acc-1",
        )

        self.assertEqual(result["order_id"], "order-uuid-1")
        mock_update.assert_called_once()
        update_kwargs = mock_update.call_args.kwargs
        self.assertEqual(update_kwargs["status"], "filled")
        self.assertEqual(update_kwargs["exchange_order_id"], "123456")
        self.assertEqual(update_kwargs["filled_quantity"], Decimal("100"))
        self.assertEqual(update_kwargs["avg_fill_price"], Decimal("0.1099"))

    @patch("apps.trading.models.Order.objects")
    def test_partial_fill_maps_to_partial(self, mock_order_mgr):
        """部分成交：filled_qty < quantity → status=partial。"""
        mock_adapter = AsyncMock()
        mock_response = MagicMock()
        mock_response.exchange_order_id = "123456"
        mock_response.status = "PARTIALLY_FILLED"
        mock_response.filled_qty = Decimal("40")
        mock_response.avg_price = Decimal("0.11")
        mock_adapter.place_order.return_value = mock_response
        self.executor._account_adapters = {"acc-1": mock_adapter}

        mock_order = MagicMock()
        mock_order.id = "order-uuid-1"
        mock_order_mgr.create.return_value = mock_order
        mock_update = MagicMock()
        mock_order_mgr.filter.return_value.update = mock_update

        self._run_submit(
            exchange="binance",
            symbol="DOGE/USDT",
            side="buy",
            order_type="market",
            quantity=Decimal("100"),
            price=None,
            exchange_account_id="acc-1",
        )

        update_kwargs = mock_update.call_args.kwargs
        self.assertEqual(update_kwargs["status"], "partial")
        self.assertEqual(update_kwargs["filled_quantity"], Decimal("40"))

    @patch("apps.trading.models.Order.objects")
    def test_no_fill_keeps_submitted(self, mock_order_mgr):
        """未成交：filled_qty==0 → 保持 submitted。"""
        mock_adapter = AsyncMock()
        mock_response = MagicMock()
        mock_response.exchange_order_id = "123456"
        mock_response.status = "NEW"
        mock_response.filled_qty = Decimal("0")
        mock_response.avg_price = None
        mock_adapter.place_order.return_value = mock_response
        self.executor._account_adapters = {"acc-1": mock_adapter}

        mock_order = MagicMock()
        mock_order.id = "order-uuid-1"
        mock_order_mgr.create.return_value = mock_order
        mock_update = MagicMock()
        mock_order_mgr.filter.return_value.update = mock_update

        self._run_submit(
            exchange="binance",
            symbol="DOGE/USDT",
            side="buy",
            order_type="limit",
            quantity=Decimal("100"),
            price=Decimal("0.10"),
            exchange_account_id="acc-1",
        )

        update_kwargs = mock_update.call_args.kwargs
        self.assertEqual(update_kwargs["status"], "submitted")
        self.assertNotIn("filled_quantity", update_kwargs)


class TestFillSyncLoop(unittest.TestCase):
    """后台成交同步：_sync_active_orders + _apply_fill。"""

    def setUp(self):
        self.executor = OrderExecutor()
        self.executor._running = True
        self.executor._riskguard = None
        self.executor._account_adapters = {}

    def tearDown(self):
        OrderExecutor._instance = None

    @patch("apps.trading.models.Order.objects")
    def test_sync_updates_submitted_to_filled(self, mock_order_mgr):
        """submitted 订单在交易所已成交 → 落库为 filled。"""
        order = _fake_order()
        mock_order_mgr.filter.return_value.select_related.return_value = [order]
        mock_update = MagicMock()
        mock_order_mgr.filter.return_value.update = mock_update

        mock_adapter = AsyncMock()
        mock_adapter.fetch_order.return_value = OrderFill(
            status="filled",
            filled_quantity=Decimal("100"),
            avg_fill_price=Decimal("0.1099"),
        )
        self.executor._account_adapters = {"acc-1": mock_adapter}

        asyncio.run(self.executor._sync_active_orders())

        mock_adapter.fetch_order.assert_called_once_with("2336219508", "DOGE/USDT")
        update_kwargs = mock_update.call_args.kwargs
        self.assertEqual(update_kwargs["status"], "filled")
        self.assertEqual(update_kwargs["filled_quantity"], Decimal("100"))
        self.assertEqual(update_kwargs["avg_fill_price"], Decimal("0.1099"))

    @patch("apps.trading.models.Order.objects")
    def test_sync_no_change_skips_update(self, mock_order_mgr):
        """状态与数量无变化 → 不写库（避免无意义更新）。"""
        order = _fake_order(
            status="filled",
            filled_quantity=Decimal("100"),
            avg_fill_price=Decimal("0.1099"),
        )
        mock_order_mgr.filter.return_value.select_related.return_value = [order]
        mock_update = MagicMock()
        mock_order_mgr.filter.return_value.update = mock_update

        mock_adapter = AsyncMock()
        mock_adapter.fetch_order.return_value = OrderFill(
            status="filled",
            filled_quantity=Decimal("100"),
            avg_fill_price=Decimal("0.1099"),
        )
        self.executor._account_adapters = {"acc-1": mock_adapter}

        asyncio.run(self.executor._sync_active_orders())

        mock_update.assert_not_called()

    @patch("apps.trading.models.Order.objects")
    def test_sync_cancelled_when_order_missing(self, mock_order_mgr):
        """交易所返回订单不存在 → 标记 cancelled 并记录原因。"""
        order = _fake_order()
        mock_order_mgr.filter.return_value.select_related.return_value = [order]
        mock_update = MagicMock()
        mock_order_mgr.filter.return_value.update = mock_update

        mock_adapter = AsyncMock()
        mock_adapter.fetch_order.side_effect = OrderNotFoundError("order gone")
        self.executor._account_adapters = {"acc-1": mock_adapter}

        asyncio.run(self.executor._sync_active_orders())

        update_kwargs = mock_update.call_args.kwargs
        self.assertEqual(update_kwargs["status"], "cancelled")
        self.assertIn("order gone", update_kwargs["error_message"])

    @patch("apps.trading.models.Order.objects")
    def test_sync_survives_adapter_error(self, mock_order_mgr):
        """adapter 查询抛错 → 订单保持原状，同步循环不中断。"""
        order = _fake_order()
        mock_order_mgr.filter.return_value.select_related.return_value = [order]
        mock_update = MagicMock()
        mock_order_mgr.filter.return_value.update = mock_update

        mock_adapter = AsyncMock()
        mock_adapter.fetch_order.side_effect = RuntimeError("network down")
        self.executor._account_adapters = {"acc-1": mock_adapter}

        asyncio.run(self.executor._sync_active_orders())

        mock_update.assert_not_called()

    @patch("apps.trading.models.Order.objects")
    def test_sync_partial_updated(self, mock_order_mgr):
        """部分成交数量增长 → partial 状态与数量同步。"""
        order = _fake_order(status="partial", filled_quantity=Decimal("40"))
        mock_order_mgr.filter.return_value.select_related.return_value = [order]
        mock_update = MagicMock()
        mock_order_mgr.filter.return_value.update = mock_update

        mock_adapter = AsyncMock()
        mock_adapter.fetch_order.return_value = OrderFill(
            status="partial",
            filled_quantity=Decimal("70"),
            avg_fill_price=Decimal("0.11"),
        )
        self.executor._account_adapters = {"acc-1": mock_adapter}

        asyncio.run(self.executor._sync_active_orders())

        update_kwargs = mock_update.call_args.kwargs
        self.assertEqual(update_kwargs["filled_quantity"], Decimal("70"))
        self.assertNotIn("status", update_kwargs)

    def test_map_exchange_status(self):
        """交易所状态字符串映射到本地状态。"""
        cases = {
            "NEW": "submitted",
            "PARTIALLY_FILLED": "partial",
            "FILLED": "filled",
            "CANCELED": "cancelled",
            "EXPIRED": "cancelled",
            "REJECTED": "failed",
            "UNKNOWN_STATUS": "submitted",
        }
        for exchange_status, expected in cases.items():
            self.assertEqual(
                self.executor._map_exchange_status(exchange_status), expected
            )


if __name__ == "__main__":
    unittest.main()
