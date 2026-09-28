"""submit_order 的幂等键与可诊断错误（2026-09-21）。

事故背景：2026-09-21 00:05:44 的 DOGE 买单因适配器卡死而 10 秒超时失败，
库里 `error_message` 是**空字符串**（`str(httpx.ConnectTimeout(""))` 为空）。
且当时没有传 clientOrderId，一旦引入"传输层重试"就可能重复下单。

契约：
  1. 下单前必须把订单行的 `request_id`（唯一 UUID）作为 `client_order_id` 传给适配器
     → 交易所侧有幂等键，重试/对账都以它为准。
  2. 失败落库的 `error_message` 必须带异常类型名，不能是空串（否则以后翻单只能靠猜）。
  3. 适配器抛 `OrderPlacementUnknown`（对不上账）时落 **`unknown`** 而不是 `failed`
     （2026-09-22）：那张单可能已在交易所活着，记 failed 会让账面分叉、并诱导用户
     重下一张。`unknown` 在 `Order.ACTIVE_STATUSES` 里，悬挂扫描会继续找它。
"""

from __future__ import annotations

import asyncio
import unittest
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from apps.trading.adapters.base import OrderPlacementUnknown
from apps.trading.executor import OrderExecutor


class _FakeRiskGuard:
    """只实现下单流程真正会调到的三个方法。

    不能用 ``AsyncMock()`` 顶：``pre_trade_check`` 会被解包成
    ``approved, reason = await ...``，而 AsyncMock 返回一个裸 Mock → ValueError，
    下单根本没走到适配器，测试测的就不是目标分支了。
    """

    def __init__(self):
        self.failures: list[tuple] = []

    async def pre_trade_check(self, request, user_id):
        return True, "OK"

    async def record_order_failure(self, user_id, **kwargs):
        self.failures.append((user_id, kwargs))

    async def record_order_success(self, user_id):
        pass


class TestSubmitOrderSafety(unittest.TestCase):
    def setUp(self):
        self.executor = OrderExecutor()
        self.executor._running = True
        self.executor._riskguard = None
        self.executor._adapters = {}

    def tearDown(self):
        OrderExecutor._instance = None

    def _mock_response(self):
        resp = MagicMock()
        resp.exchange_order_id = "123456"
        resp.status = "NEW"
        resp.filled_qty = Decimal("0")
        resp.avg_price = None
        return resp

    @patch("apps.trading.models.Order.objects")
    def test_place_order_receives_request_id_as_client_order_id(self, mock_order_mgr):
        mock_adapter = AsyncMock()
        mock_adapter.place_order.return_value = self._mock_response()
        self.executor._adapters = {"binance": mock_adapter}

        rid = uuid.UUID("11111111-2222-3333-4444-555555555555")
        mock_order = MagicMock()
        mock_order.id = "order-uuid-1"
        mock_order.request_id = rid
        mock_order_mgr.create.return_value = mock_order
        mock_order_mgr.filter.return_value.update = MagicMock()

        asyncio.run(
            self.executor.submit_order(
                exchange="binance",
                symbol="DOGEUSDT",
                side="buy",
                order_type="market",
                quantity=Decimal("13119.21815906"),
                price=None,
                exchange_account_id="uuid-placeholder",
            )
        )

        sent = mock_adapter.place_order.call_args[0][0]
        self.assertEqual(
            sent.client_order_id,
            str(rid),
            "下单必须带订单行 request_id 作为幂等键（交易所据此去重/供对账）",
        )

    @patch("apps.trading.models.Order.objects")
    def test_failure_records_typed_error_message(self, mock_order_mgr):
        """httpx 超时异常 message 为空 → 落库也必须能看出异常类型。"""
        mock_adapter = AsyncMock()
        mock_adapter.place_order.side_effect = httpx.ConnectTimeout("")
        self.executor._adapters = {"binance": mock_adapter}
        riskguard = _FakeRiskGuard()
        self.executor._riskguard = riskguard

        mock_order = MagicMock()
        mock_order.id = "order-uuid-fail"
        mock_order.request_id = uuid.uuid4()
        mock_order_mgr.create.return_value = mock_order
        mock_order_mgr.filter.return_value.update = MagicMock()

        with self.assertRaises(httpx.ConnectTimeout):
            asyncio.run(
                self.executor.submit_order(
                    exchange="binance",
                    symbol="DOGEUSDT",
                    side="buy",
                    order_type="market",
                    quantity=Decimal("13119.21815906"),
                    price=None,
                    exchange_account_id="uuid-placeholder",
                )
            )

        updates = [
            call.kwargs
            for call in mock_order_mgr.filter.return_value.update.call_args_list
            if call.kwargs.get("status") == "failed"
        ]
        self.assertEqual(len(updates), 1, "必须落一条 failed")
        msg = updates[0]["error_message"]
        self.assertIn("ConnectTimeout", msg, "空消息异常也必须写清类型名")
        self.assertNotEqual(msg.strip(), "", "error_message 不能为空串")

        # 发送前错误（请求肯定没出去）→ 确定失败，通知不能带"可能已成交"的口径，
        # 否则用户会以为要去交易所核对一张根本不存在的单。
        self.assertEqual(len(riskguard.failures), 1)
        self.assertFalse(
            riskguard.failures[0][1].get("unknown"),
            "确定失败必须 unknown=False，措辞才不会被改成'结果未知'",
        )

    @patch("apps.trading.models.Order.objects")
    def test_placement_unknown_records_unknown_status(self, mock_order_mgr):
        """`OrderPlacementUnknown` → 落 ``unknown``（非终态），**不是** failed。

        那张单可能已在交易所活着。记 failed 有两个后果：账面与交易所分叉，以及用户
        看到"下单失败"再下一张 → 敞口变成两倍。所以状态必须是 unknown，且不带
        exchange_order_id，好让悬挂扫描继续找它。
        """
        mock_adapter = AsyncMock()
        mock_adapter.place_order.side_effect = OrderPlacementUnknown("对账不可达")
        self.executor._adapters = {"binance": mock_adapter}
        riskguard = _FakeRiskGuard()
        self.executor._riskguard = riskguard

        mock_order = MagicMock()
        mock_order.id = "order-uuid-unknown"
        mock_order.request_id = uuid.uuid4()
        mock_order_mgr.create.return_value = mock_order
        mock_order_mgr.filter.return_value.update = MagicMock()

        with self.assertRaises(OrderPlacementUnknown):
            asyncio.run(
                self.executor.submit_order(
                    exchange="binance",
                    symbol="DOGEUSDT",
                    side="buy",
                    order_type="market",
                    quantity=Decimal("13119.21815906"),
                    price=None,
                    exchange_account_id="uuid-placeholder",
                    user_id="user-uuid",
                )
            )

        statuses = [
            call.kwargs.get("status")
            for call in mock_order_mgr.filter.return_value.update.call_args_list
        ]
        self.assertIn("unknown", statuses)
        self.assertNotIn(
            "failed", statuses, "未知单绝不能落 failed：那样账面会与交易所分叉"
        )

        # 熔断计数照旧（未知同样是风险信号），但措辞必须改成"结果未知"
        self.assertEqual(len(riskguard.failures), 1, "未知单也必须喂熔断器")
        self.assertTrue(
            riskguard.failures[0][1].get("unknown"),
            "必须告诉风控这是未知单，否则通知会说'下单失败'诱导用户重下",
        )

    @patch("apps.trading.models.Order.objects")
    def test_unknown_row_is_visible_to_the_dangling_sweep(self, mock_order_mgr):
        """unknown 必须落在悬挂扫描的候选集里，否则这张单永远不会有人再找它。"""
        mock_order = MagicMock()
        mock_order.id = "order-uuid-unknown"
        mock_order.request_id = uuid.uuid4()
        mock_order_mgr.create.return_value = mock_order
        mock_order_mgr.filter.return_value.update = MagicMock()

        mock_adapter = AsyncMock()
        mock_adapter.place_order.side_effect = OrderPlacementUnknown("对账不可达")
        self.executor._adapters = {"binance": mock_adapter}

        with self.assertRaises(OrderPlacementUnknown):
            asyncio.run(
                self.executor.submit_order(
                    exchange="binance",
                    symbol="DOGEUSDT",
                    side="buy",
                    order_type="market",
                    quantity=Decimal("1"),
                    price=None,
                    exchange_account_id="uuid-placeholder",
                )
            )

        from apps.trading.models import Order

        self.assertIn(
            "unknown",
            Order.ACTIVE_STATUSES,
            "unknown 是非终态：漏掉它就等于把这张单丢出所有对账路径",
        )
        written = [
            call.kwargs.get("status")
            for call in mock_order_mgr.filter.return_value.update.call_args_list
        ]
        self.assertIn("unknown", written, "写入的状态必须正好是被扫描器捞起的那个")


class TestCloseOrderIsReduceOnly(unittest.TestCase):
    """2026-09-27 BNB 双平手账：平仓单必须翻译成 reduceOnly。

    本地持仓与交易所分叉时（在途单窗口、同步滞后），一张超持仓的卖单在合约上
    不会被拒，而是**反向开空**——reduceOnly 是唯一能拦住它的交易所级手段。
    开仓单必须保持非 reduceOnly，否则开仓会被交易所直接拒掉。
    """

    def setUp(self):
        self.executor = OrderExecutor()
        self.executor._running = True
        self.executor._riskguard = None
        self.executor._adapters = {}

    def tearDown(self):
        OrderExecutor._instance = None

    def _mock_response(self):
        resp = MagicMock()
        resp.exchange_order_id = "123456"
        resp.status = "NEW"
        resp.filled_qty = Decimal("0")
        resp.avg_price = None
        return resp

    @patch("apps.trading.models.Order.objects")
    def test_close_order_sends_reduce_only(self, mock_order_mgr):
        mock_adapter = AsyncMock()
        mock_adapter.place_order.return_value = self._mock_response()
        self.executor._adapters = {"binance": mock_adapter}

        mock_order = MagicMock()
        mock_order.id = "order-uuid-close"
        mock_order.request_id = uuid.uuid4()
        mock_order_mgr.create.return_value = mock_order
        mock_order_mgr.filter.return_value.update = MagicMock()

        asyncio.run(
            self.executor.submit_order(
                exchange="binance",
                symbol="BNBUSDT",
                side="sell",
                order_type="market",
                quantity=Decimal("1.37"),
                price=None,
                exchange_account_id="uuid-placeholder",
                is_close_position=True,
            )
        )

        sent = mock_adapter.place_order.call_args[0][0]
        self.assertTrue(
            sent.reduce_only,
            "平仓单必须 reduceOnly：超持仓部分由交易所拒单，而不是反向开仓",
        )

    @patch("apps.trading.models.Order.objects")
    def test_normal_order_is_not_reduce_only(self, mock_order_mgr):
        mock_adapter = AsyncMock()
        mock_adapter.place_order.return_value = self._mock_response()
        self.executor._adapters = {"binance": mock_adapter}

        mock_order = MagicMock()
        mock_order.id = "order-uuid-open"
        mock_order.request_id = uuid.uuid4()
        mock_order_mgr.create.return_value = mock_order
        mock_order_mgr.filter.return_value.update = MagicMock()

        asyncio.run(
            self.executor.submit_order(
                exchange="binance",
                symbol="BNBUSDT",
                side="buy",
                order_type="market",
                quantity=Decimal("1.37"),
                price=None,
                exchange_account_id="uuid-placeholder",
            )
        )

        sent = mock_adapter.place_order.call_args[0][0]
        self.assertFalse(
            sent.reduce_only,
            "开仓单不得 reduceOnly：带了开仓会被交易所直接拒掉",
        )
