"""已实现盈亏（realized_pnl）测试。

L2 单元测试，覆盖：
- compute_realized_pnl 纯函数（移动平均成本法，含实盘真实数据锚点）
- _apply_fill 平仓卖单成交时重算盈亏 + 会话权益增量（部分→完全成交按增量修正）
- submit_order 市价卖单即时成交时计算盈亏 + 会话权益增量
- _bump_session_equity 用 F 表达式更新权益 + 容错
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import unittest

from apps.trading.adapters.base import OrderFill
from apps.trading.executor import OrderExecutor, compute_realized_pnl


def _row(side, q, p):
    return {
        "side": side,
        "filled_quantity": Decimal(str(q)),
        "avg_fill_price": Decimal(str(p)),
    }


def _buy(q, p):
    return _row("buy", q, p)


def _sell(q, p):
    return _row("sell", q, p)


def _fake_order(**overrides):
    """构造一个轻量订单对象（仅含 pnl/fill-sync 需要的字段）。"""
    base = {
        "id": "order-sell-1",
        "side": "sell",
        "symbol": "DOGE/USDT",
        "status": "submitted",
        "filled_quantity": Decimal("0"),
        "avg_fill_price": None,
        "realized_pnl": None,
        "error_message": "",
        "exchange_account": SimpleNamespace(exchange="binance"),
        "exchange_account_id": "acc-1",
        "live_session_id": "session-1",
        "quantity": Decimal("100"),
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class TestComputeRealizedPnl(unittest.TestCase):
    """compute_realized_pnl 纯函数：移动平均成本法。"""

    def test_single_buy_single_sell(self):
        pnl = compute_realized_pnl(
            [_buy("100", "0.1")], "sell", Decimal("100"), Decimal("0.11")
        )
        self.assertEqual(pnl, Decimal("1.0"))

    def test_two_buys_average_cost(self):
        history = [_buy("100", "0.1"), _buy("100", "0.2")]
        pnl = compute_realized_pnl(
            history, "sell", Decimal("100"), Decimal("0.2")
        )
        self.assertEqual(pnl, Decimal("5.0"))

    def test_partial_sell_reduces_position(self):
        history = [_buy("100", "0.1"), _sell("50", "0.12")]
        pnl = compute_realized_pnl(
            history, "sell", Decimal("50"), Decimal("0.13")
        )
        self.assertEqual(pnl, Decimal("1.5"))

    def test_over_close_caps_at_position(self):
        pnl = compute_realized_pnl(
            [_buy("100", "0.1")], "sell", Decimal("150"), Decimal("0.11")
        )
        self.assertEqual(pnl, Decimal("1.0"))

    def test_loss(self):
        pnl = compute_realized_pnl(
            [_buy("100", "0.1")], "sell", Decimal("100"), Decimal("0.09")
        )
        self.assertEqual(pnl, Decimal("-1.0"))

    def test_no_history_returns_none(self):
        self.assertIsNone(
            compute_realized_pnl([], "sell", Decimal("100"), Decimal("0.11"))
        )

    def test_buy_side_returns_none(self):
        self.assertIsNone(
            compute_realized_pnl(
                [_sell("100", "0.1")], "buy", Decimal("100"), Decimal("0.11")
            )
        )

    def test_no_avg_price_returns_none(self):
        self.assertIsNone(
            compute_realized_pnl(
                [_buy("100", "0.1")], "sell", Decimal("100"), None
            )
        )

    def test_real_donchian_case(self):
        """实盘真实数据：买 11579@0.08629 → 卖 11579@0.08898 = 31.14751 USDT。"""
        pnl = compute_realized_pnl(
            [_buy("11579", "0.08629")],
            "sell",
            Decimal("11579"),
            Decimal("0.08898"),
        )
        self.assertEqual(pnl, Decimal("31.14751"))

    def test_short_position_buy_reopens_not_first_buy(self):
        """卖超仓位 = 反手开空，其后的买入是「回补」而不是「空仓下的首笔买入」。

        实盘真实数据（账户 1fbff916 / DOGE/USDT）：2026-09-20 事故把 donchian
        会话卖成 -17718 空头，9/21 用 13119@0.09134 + 4599@0.09182 回补并反手
        做多，9/27 再买 10130@0.09861，9/28 卖 10130@0.09619。

        修复前这里算出 +21.53831957（亏损显示为收益）：`max(0, pos - q)` 把空头
        截断成 0 仓位，于是 9/21 两笔买入被当成空仓下的首笔买入直接取价，成本
        基准被拉到 0.094064，低于平仓价 0.09619，符号翻正。
        """
        history = [
            _sell("17718", "0.08654"),  # 事故：卖成空头
            _buy("13119", "0.09134"),  # 回补 + 反手做多
            _buy("4599", "0.09182"),
            _buy("10130", "0.09861"),  # 真正被平掉的那批
        ]
        pnl = compute_realized_pnl(
            history, "sell", Decimal("10130"), Decimal("0.09619")
        )
        self.assertEqual(pnl, Decimal("-24.51460"))

    def test_short_cover_keeps_remaining_short_cost(self):
        """空头未回补满时继续买入，剩余空头的成本不应被后来的买入价改写。"""
        history = [
            _sell("10", "0.20"),
            _buy("4", "0.10"),  # 回补 4，仍空 6，成本仍是 0.20
            _buy("10", "0.30"),  # 回补 6 + 反手做多 4，做多成本 = 0.30
        ]
        pnl = compute_realized_pnl(
            history, "sell", Decimal("4"), Decimal("0.40")
        )
        self.assertEqual(pnl, Decimal("0.4"))


class _ExecutorTestCase(unittest.TestCase):
    """共享 setUp/tearDown：构造未运行单例 + 影子 _bump_session_equity。"""

    def setUp(self):
        self.executor = OrderExecutor()
        self.executor._running = True
        self.executor._riskguard = None
        self.executor._adapters = {}
        self.executor._bump_session_equity = AsyncMock()

    def tearDown(self):
        OrderExecutor._instance = None

    @staticmethod
    def _mock_order_objects(mock_order_mgr, history, need_create=False):
        """配置 Order.objects mock：create（可选）/ update / 历史查询链。"""
        if need_create:
            mock_order = MagicMock()
            mock_order.id = "order-uuid-1"
            mock_order_mgr.create.return_value = mock_order
        mock_update = MagicMock()
        mock_order_mgr.filter.return_value.update = mock_update
        (
            mock_order_mgr.filter.return_value.exclude.return_value.order_by.return_value.values.return_value
        ) = history
        return mock_update


class TestApplyFillRealizedPnl(_ExecutorTestCase):
    """_apply_fill 平仓卖单成交时计算已实现盈亏 + 会话权益增量。"""

    @patch("apps.trading.models.Order.objects")
    def test_sell_filled_computes_pnl_and_bumps_equity(self, mock_order_mgr):
        order = _fake_order()
        mock_update = self._mock_order_objects(
            mock_order_mgr, [_buy("100", "0.1")]
        )
        fill = OrderFill(
            status="filled",
            filled_quantity=Decimal("100"),
            avg_fill_price=Decimal("0.11"),
        )
        asyncio.run(self.executor._apply_fill(order, fill))

        update_kwargs = mock_update.call_args.kwargs
        self.assertEqual(update_kwargs["status"], "filled")
        self.assertEqual(update_kwargs["filled_quantity"], Decimal("100"))
        self.assertEqual(update_kwargs["realized_pnl"], Decimal("1.0"))
        self.executor._bump_session_equity.assert_called_once_with(
            "session-1", Decimal("1.0")
        )

    @patch("apps.trading.models.Order.objects")
    def test_sell_filled_no_cost_basis(self, mock_order_mgr):
        order = _fake_order()
        mock_update = self._mock_order_objects(mock_order_mgr, [])
        fill = OrderFill(
            status="filled",
            filled_quantity=Decimal("100"),
            avg_fill_price=Decimal("0.11"),
        )
        asyncio.run(self.executor._apply_fill(order, fill))

        update_kwargs = mock_update.call_args.kwargs
        self.assertEqual(update_kwargs["status"], "filled")
        self.assertNotIn("realized_pnl", update_kwargs)
        self.executor._bump_session_equity.assert_not_called()

    @patch("apps.trading.models.Order.objects")
    def test_partial_to_full_bumps_delta_only(self, mock_order_mgr):
        order = _fake_order(
            status="partial",
            filled_quantity=Decimal("50"),
            avg_fill_price=Decimal("0.12"),
            realized_pnl=Decimal("1.0"),
        )
        mock_update = self._mock_order_objects(
            mock_order_mgr, [_buy("100", "0.1")]
        )
        fill = OrderFill(
            status="filled",
            filled_quantity=Decimal("100"),
            avg_fill_price=Decimal("0.13"),
        )
        asyncio.run(self.executor._apply_fill(order, fill))

        update_kwargs = mock_update.call_args.kwargs
        self.assertEqual(update_kwargs["realized_pnl"], Decimal("3.0"))
        self.executor._bump_session_equity.assert_called_once_with(
            "session-1", Decimal("2.0")
        )

    @patch("apps.trading.models.Order.objects")
    def test_buy_order_skips_pnl(self, mock_order_mgr):
        order = _fake_order(side="buy")
        mock_update = self._mock_order_objects(
            mock_order_mgr, [_sell("100", "0.1")]
        )
        fill = OrderFill(
            status="filled",
            filled_quantity=Decimal("100"),
            avg_fill_price=Decimal("0.11"),
        )
        asyncio.run(self.executor._apply_fill(order, fill))

        update_kwargs = mock_update.call_args.kwargs
        self.assertEqual(update_kwargs["status"], "filled")
        self.assertNotIn("realized_pnl", update_kwargs)
        self.executor._bump_session_equity.assert_not_called()

    @patch("apps.trading.models.Order.objects")
    def test_no_change_skips_update(self, mock_order_mgr):
        order = _fake_order(
            status="filled",
            filled_quantity=Decimal("100"),
            avg_fill_price=Decimal("0.11"),
            realized_pnl=Decimal("1.0"),
        )
        mock_update = self._mock_order_objects(
            mock_order_mgr, [_buy("100", "0.1")]
        )
        fill = OrderFill(
            status="filled",
            filled_quantity=Decimal("100"),
            avg_fill_price=Decimal("0.11"),
        )
        asyncio.run(self.executor._apply_fill(order, fill))

        mock_update.assert_not_called()
        self.executor._bump_session_equity.assert_not_called()


class TestSubmitClosePnl(_ExecutorTestCase):
    """submit_order 市价卖单即时成交时计算已实现盈亏 + 会话权益增量。"""

    def _mock_adapter(
        self,
        status="FILLED",
        filled_qty=Decimal("100"),
        avg_price=Decimal("0.11"),
    ):
        mock_adapter = AsyncMock()
        mock_response = MagicMock()
        mock_response.exchange_order_id = "123456"
        mock_response.status = status
        mock_response.filled_qty = filled_qty
        mock_response.avg_price = avg_price
        mock_adapter.place_order.return_value = mock_response
        self.executor._adapters = {"binance": mock_adapter}
        return mock_adapter

    def _run_submit(self, **kwargs):
        return asyncio.run(self.executor.submit_order(**kwargs))

    @patch("apps.trading.models.LiveSession.objects")
    @patch("apps.trading.models.Order.objects")
    def test_market_sell_filled_with_session(
        self, mock_order_mgr, mock_ls_mgr
    ):
        self._mock_adapter()
        mock_update = self._mock_order_objects(
            mock_order_mgr, [_buy("100", "0.1")], need_create=True
        )
        mock_session = MagicMock()
        mock_session.strategy.name = "donchian_atr_trend_strategy"
        mock_ls_mgr.select_related.return_value.get.return_value = mock_session

        result = self._run_submit(
            exchange="binance",
            symbol="DOGE/USDT",
            side="sell",
            order_type="market",
            quantity=Decimal("100"),
            price=None,
            exchange_account_id="acc-1",
            live_session_id="session-1",
        )

        self.assertEqual(result["status"], "FILLED")
        self.assertEqual(result["order_id"], "order-uuid-1")
        update_kwargs = mock_update.call_args.kwargs
        self.assertEqual(update_kwargs["status"], "filled")
        self.assertEqual(update_kwargs["realized_pnl"], Decimal("1.0"))
        self.executor._bump_session_equity.assert_called_once_with(
            "session-1", Decimal("1.0")
        )

    @patch("apps.trading.models.Order.objects")
    def test_market_sell_filled_no_session(self, mock_order_mgr):
        self._mock_adapter()
        mock_update = self._mock_order_objects(
            mock_order_mgr, [_buy("100", "0.1")], need_create=True
        )

        result = self._run_submit(
            exchange="binance",
            symbol="DOGE/USDT",
            side="sell",
            order_type="market",
            quantity=Decimal("100"),
            price=None,
            exchange_account_id="acc-1",
        )

        self.assertEqual(result["status"], "FILLED")
        update_kwargs = mock_update.call_args.kwargs
        self.assertEqual(update_kwargs["realized_pnl"], Decimal("1.0"))
        self.executor._bump_session_equity.assert_not_called()

    @patch("apps.trading.models.Order.objects")
    def test_market_buy_no_pnl(self, mock_order_mgr):
        self._mock_adapter()
        mock_update = self._mock_order_objects(
            mock_order_mgr, [], need_create=True
        )

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
        self.assertEqual(update_kwargs["status"], "filled")
        self.assertNotIn("realized_pnl", update_kwargs)
        self.executor._bump_session_equity.assert_not_called()

    @patch("apps.trading.models.Order.objects")
    def test_pnl_query_failure_does_not_block_order(self, mock_order_mgr):
        self._mock_adapter()
        mock_update = self._mock_order_objects(
            mock_order_mgr, [], need_create=True
        )
        mock_order_mgr.filter.return_value.exclude.side_effect = RuntimeError(
            "db down"
        )

        result = self._run_submit(
            exchange="binance",
            symbol="DOGE/USDT",
            side="sell",
            order_type="market",
            quantity=Decimal("100"),
            price=None,
            exchange_account_id="acc-1",
        )

        self.assertEqual(result["status"], "FILLED")
        update_kwargs = mock_update.call_args.kwargs
        self.assertEqual(update_kwargs["status"], "filled")
        self.assertNotIn("realized_pnl", update_kwargs)
        self.executor._bump_session_equity.assert_not_called()


class TestBumpSessionEquity(unittest.TestCase):
    """_bump_session_equity：F 表达式更新权益 + 容错。"""

    def setUp(self):
        self.executor = OrderExecutor()

    @patch("apps.trading.models.LiveSession.objects")
    def test_bump_updates_equity_with_f_expression(self, mock_ls_mgr):
        asyncio.run(
            self.executor._bump_session_equity("session-1", Decimal("1.0"))
        )

        mock_ls_mgr.filter.assert_called_once_with(id="session-1")
        mock_update = mock_ls_mgr.filter.return_value.update
        mock_update.assert_called_once()
        expr = mock_update.call_args.kwargs["current_equity"]
        self.assertIn("current_equity", str(expr))

    @patch("apps.trading.models.LiveSession.objects")
    def test_bump_swallows_db_error(self, mock_ls_mgr):
        mock_ls_mgr.filter.return_value.update.side_effect = RuntimeError(
            "db down"
        )
        # 不应抛出
        asyncio.run(
            self.executor._bump_session_equity("session-1", Decimal("1.0"))
        )


if __name__ == "__main__":
    unittest.main()
