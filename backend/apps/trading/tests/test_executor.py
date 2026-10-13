"""Tests for OrderExecutor (L2 — mock adapter + ORM)."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import unittest

from apps.trading.executor import OrderExecutor


class TestOrderExecutorL2(unittest.TestCase):
    """L2: 业务逻辑测试（mock adapter + ORM）"""

    def setUp(self):
        self.executor = OrderExecutor()
        self.executor._running = True
        self.executor._riskguard = None
        self.executor._account_adapters = {}

    def tearDown(self):
        OrderExecutor._instance = None

    def test_singleton_instance_set_on_initialize(self):
        """initialize() 应设置 _instance"""
        self.assertIsNone(OrderExecutor._instance)
        # Mock the internal methods
        with patch.object(self.executor, "_load_adapters", new_callable=AsyncMock):
            with patch.object(self.executor, "_load_riskguard"):
                asyncio.run(self.executor.initialize())
        self.assertIs(OrderExecutor._instance, self.executor)

    def test_submit_order_requires_running(self):
        """submit_order() 在未运行时抛 RuntimeError"""
        self.executor._running = False
        with self.assertRaises(RuntimeError):
            asyncio.run(
                self.executor.submit_order(
                    exchange="binance",
                    symbol="BTCUSDT",
                    side="buy",
                    order_type="market",
                    quantity=Decimal("0.001"),
                    price=None,
                    exchange_account_id="uuid-placeholder",
                )
            )

    def test_submit_order_requires_adapter(self):
        """submit_order() 在 adapter 不存在时抛 ValueError"""
        self.executor._account_adapters = {}
        self.executor._riskguard = None
        with self.assertRaises(ValueError):
            asyncio.run(
                self.executor.submit_order(
                    exchange="binance",
                    symbol="BTCUSDT",
                    side="buy",
                    order_type="market",
                    quantity=Decimal("0.001"),
                    price=None,
                    exchange_account_id="uuid-placeholder",
                )
            )

    def test_submit_order_riskguard_rejection(self):
        """RiskGuard 拒绝时抛 PermissionError"""
        mock_adapter = AsyncMock()
        self.executor._account_adapters = {"uuid-placeholder": mock_adapter}

        mock_riskguard = AsyncMock()
        mock_riskguard.pre_trade_check.return_value = (False, "日内交易次数已达上限")
        self.executor._riskguard = mock_riskguard

        with self.assertRaises(PermissionError) as ctx:
            asyncio.run(
                self.executor.submit_order(
                    exchange="binance",
                    symbol="BTCUSDT",
                    side="buy",
                    order_type="market",
                    quantity=Decimal("0.001"),
                    price=None,
                    exchange_account_id="uuid-placeholder",
                )
            )
        self.assertIn("RiskGuard拒绝下单", str(ctx.exception))

    @patch("apps.trading.models.Order.objects")
    def test_submit_order_success_path(self, mock_order_mgr):
        """下单成功流程：RiskGuard通过 → 写DB → 发交易所 → 更新DB"""
        mock_adapter = AsyncMock()
        mock_response = MagicMock()
        mock_response.exchange_order_id = "123456"
        mock_response.status = "NEW"
        mock_adapter.place_order.return_value = mock_response
        self.executor._account_adapters = {"uuid-placeholder": mock_adapter}
        self.executor._riskguard = None  # 跳过风控

        # Mock Order.objects.create
        mock_order = MagicMock()
        mock_order.id = "order-uuid-123"
        mock_order_mgr.create.return_value = mock_order
        mock_order_mgr.filter.return_value.update = MagicMock()

        result = asyncio.run(
            self.executor.submit_order(
                exchange="binance",
                symbol="BTCUSDT",
                side="buy",
                order_type="market",
                quantity=Decimal("0.001"),
                price=None,
                exchange_account_id="uuid-placeholder",
            )
        )

        self.assertEqual(result["order_id"], "order-uuid-123")
        self.assertEqual(result["exchange_order_id"], "123456")
        mock_adapter.place_order.assert_called_once()

    @patch("apps.trading.models.LiveSession.objects")
    @patch("apps.trading.models.Order.objects")
    def test_submit_order_persists_triggered_strategy(self, mock_order_mgr, mock_ls_mgr):
        """带 live_session_id 下单时，应把会话策略名快照到 triggered_strategy。"""
        mock_adapter = AsyncMock()
        mock_response = MagicMock()
        mock_response.exchange_order_id = "123456"
        mock_response.status = "NEW"
        mock_response.filled_qty = None
        mock_adapter.place_order.return_value = mock_response
        self.executor._account_adapters = {"uuid-placeholder": mock_adapter}
        self.executor._riskguard = None  # 跳过风控

        mock_strategy = MagicMock()
        mock_strategy.name = "ema_trend_weekday"
        mock_ls = MagicMock()
        mock_ls.strategy = mock_strategy
        mock_ls_mgr.select_related.return_value.get.return_value = mock_ls

        mock_order = MagicMock()
        mock_order.id = "order-uuid-ts"
        mock_order_mgr.create.return_value = mock_order
        mock_order_mgr.filter.return_value.update = MagicMock()

        result = asyncio.run(
            self.executor.submit_order(
                exchange="binance",
                symbol="BTCUSDT",
                side="buy",
                order_type="market",
                quantity=Decimal("0.001"),
                price=None,
                exchange_account_id="uuid-placeholder",
                live_session_id="session-uuid-1",
            )
        )

        self.assertEqual(result["order_id"], "order-uuid-ts")
        kwargs = mock_order_mgr.create.call_args.kwargs
        self.assertEqual(kwargs["live_session_id"], "session-uuid-1")
        self.assertEqual(kwargs["triggered_strategy"], "ema_trend_weekday")

    @patch("apps.trading.models.Order.objects")
    def test_submit_order_without_session_has_no_triggered_strategy(self, mock_order_mgr):
        """无 live_session_id 下单时，不写 triggered_strategy（手动单）。"""
        mock_adapter = AsyncMock()
        mock_response = MagicMock()
        mock_response.exchange_order_id = "123456"
        mock_response.status = "NEW"
        mock_response.filled_qty = None
        mock_adapter.place_order.return_value = mock_response
        self.executor._account_adapters = {"uuid-placeholder": mock_adapter}
        self.executor._riskguard = None

        mock_order = MagicMock()
        mock_order.id = "order-uuid-manual"
        mock_order_mgr.create.return_value = mock_order
        mock_order_mgr.filter.return_value.update = MagicMock()

        result = asyncio.run(
            self.executor.submit_order(
                exchange="binance",
                symbol="BTCUSDT",
                side="buy",
                order_type="market",
                quantity=Decimal("0.001"),
                price=None,
                exchange_account_id="uuid-placeholder",
            )
        )

        self.assertEqual(result["order_id"], "order-uuid-manual")
        kwargs = mock_order_mgr.create.call_args.kwargs
        self.assertNotIn("triggered_strategy", kwargs)

    @patch("apps.trading.models.Order.objects")
    def test_submit_order_exchange_error_updates_status(self, mock_order_mgr):
        """交易所发送失败时订单状态应更新为 failed"""
        mock_adapter = AsyncMock()
        mock_adapter.place_order.side_effect = Exception("network error")
        self.executor._account_adapters = {"uuid-placeholder": mock_adapter}
        self.executor._riskguard = None

        mock_order = MagicMock()
        mock_order.id = "order-uuid-fail"
        mock_order_mgr.create.return_value = mock_order
        mock_order_mgr.filter.return_value.update = MagicMock()

        with self.assertRaises(Exception):
            asyncio.run(
                self.executor.submit_order(
                    exchange="binance",
                    symbol="BTCUSDT",
                    side="buy",
                    order_type="market",
                    quantity=Decimal("0.001"),
                    price=None,
                    exchange_account_id="uuid-placeholder",
                )
            )

        # 验证 update 被调用了（failed 状态）
        mock_order_mgr.filter.return_value.update.assert_called()

    def test_cancel_order_requires_running(self):
        """cancel_order() 在未运行时抛 RuntimeError"""
        self.executor._running = False
        with self.assertRaises(RuntimeError):
            asyncio.run(self.executor.cancel_order("acc-1", "123", "BTCUSDT"))

    def test_cancel_order_no_adapter(self):
        """cancel_order() 在账户没有适配器时抛 ValueError"""
        self.executor._account_adapters = {}
        with self.assertRaises(ValueError):
            asyncio.run(self.executor.cancel_order("acc-1", "123", "BTCUSDT"))

    def test_read_interfaces_take_an_account_not_an_exchange_name(self):
        """三个读接口的首参是**账户 id**；传交易所名不再解析出任何东西。

        2026-10-12 收口：原先首参是交易所名，按名字索引的表在「同一交易所两个账户」
        时只留最后加载的那个，于是撤单/查持仓会落到**别人**的账户上。现在传
        "binance" 只会得到一个「账户 binance 没有适配器」的 ValueError——fail-loud，
        而不是静默拿错账户。
        """
        adapter = AsyncMock()
        self.executor._account_adapters = {"acc-1": adapter}

        self.assertEqual(
            asyncio.run(self.executor.get_positions("acc-1")), adapter.get_positions.return_value
        )
        adapter.get_positions.assert_awaited_once()
        asyncio.run(self.executor.get_balance("acc-1"))
        adapter.get_balance.assert_awaited_once()
        asyncio.run(self.executor.cancel_order("acc-1", "123", "BTCUSDT"))
        adapter.cancel_order.assert_awaited_once_with("123", "BTCUSDT")

        with self.assertRaises(ValueError):
            asyncio.run(self.executor.get_positions("binance"))
        with self.assertRaises(ValueError):
            asyncio.run(self.executor.get_balance("binance"))

    def test_get_positions_requires_running(self):
        """get_positions() 在未运行时抛 RuntimeError"""
        self.executor._running = False
        with self.assertRaises(RuntimeError):
            asyncio.run(self.executor.get_positions("acc-1"))

    def test_get_balance_requires_running(self):
        """get_balance() 在未运行时抛 RuntimeError"""
        self.executor._running = False
        with self.assertRaises(RuntimeError):
            asyncio.run(self.executor.get_balance("acc-1"))

    def test_shutdown_resets_instance(self):
        """shutdown() 应重置单例"""
        mock_adapter = AsyncMock()
        self.executor._account_adapters = {"uuid-placeholder": mock_adapter}
        self.executor._running = True
        OrderExecutor._instance = self.executor

        asyncio.run(self.executor.shutdown())

        self.assertFalse(self.executor._running)
        self.assertEqual(len(self.executor._account_adapters), 0)
        self.assertIsNone(OrderExecutor._instance)

    def test_shutdown_disconnects_all_adapters(self):
        """shutdown() 应断开所有 adapter"""
        mock_adapter1 = AsyncMock()
        mock_adapter2 = AsyncMock()
        self.executor._account_adapters = {"acc-1": mock_adapter1, "acc-2": mock_adapter2}
        self.executor._running = True
        OrderExecutor._instance = self.executor

        asyncio.run(self.executor.shutdown())

        mock_adapter1.disconnect.assert_called_once()
        mock_adapter2.disconnect.assert_called_once()


class TestOrderExecutorSingleton(unittest.TestCase):
    """单例模式验证"""

    def test_get_instance_returns_none_when_not_running(self):
        """get_instance() 在未初始化时返回 None"""
        OrderExecutor._instance = None
        self.assertIsNone(OrderExecutor.get_instance())

    def test_multiple_instances_share_same_singleton(self):
        """多个实例化应共享同一个 _instance"""
        OrderExecutor._instance = None
        executor1 = OrderExecutor()
        executor2 = OrderExecutor()
        # 两者引用同一 _instance
        self.assertIs(executor1._instance, executor2._instance)
        OrderExecutor._instance = None
