"""下单/撤单的重试安全策略（2026-09-21，DOGE 事故衍生 P0）。

背景：`_request()` 引入的"传输层故障→重建客户端+重试一次"对**读接口**完全安全，
但对**下单**不安全——若请求已被交易所受理、只是响应丢失（读超时/协议错误），
盲目重试就是重复下单。订单行 `request_id` 现在作为 `newClientOrderId` 传给适配器，
因此含糊错误可以**按 clientOrderId 对账**（查到=已受理，查不到=没出去），而不是重发。

契约：
  1. 发送前错误（ConnectError / ConnectTimeout / PoolTimeout：请求肯定没出去）→ 可以重连重发。
  2. 含糊错误（ReadTimeout/WriteError/RemoteProtocolError 等）→ **不重发**；
     有 client_order_id 时按它向交易所对账，查到则返回交易所真实状态当成功，查不到才抛。
  3. 交易所报重复单（-4116 / "Duplicate order sent"）→ 同上对账，视为已受理（不是失败）。
  4. 读接口（持仓/余额等）不受影响：读超时照旧重连重试。

出口只有三种，第三种是 2026-09-22 新增的（此前"对不上账"被并进"确定没这张单"，
于是未知被记成 failed）：
  - 对账命中 → OrderResponse（已受理）
  - 交易所明确回答 -2013 → 返回 None，调用方按原错误抛（确定失败）
  - **对不上账**（无幂等键 / 反查不可达 / 两处证据矛盾）→ 抛 ``OrderPlacementUnknown``，
    调用方必须落 ``status="unknown"``（非终态），绝不能当成 failed。
"""

from __future__ import annotations

import unittest
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import httpx

from apps.trading.adapters.binance import BinanceAdapter
from apps.trading.adapters.base import OrderPlacementUnknown, OrderRequest
from apps.trading.tests.test_binance_adapter_selfheal import (
    POSITION_PAYLOAD,
    _FakeClient,
    _Resp,
)

ORDER_RAW = {
    "orderId": 2347999999,
    "status": "FILLED",
    "executedQty": "13119",
    "avgPrice": "0.089610",
    "clientOrderId": "cid-abc",
}
DUP_BODY = '{"code":-4116,"msg":"clientOrderId is duplicated"}'
NOT_FOUND_BODY = '{"code":-2013,"msg":"Order does not exist."}'


def _req(client_order_id: str | None = "cid-abc") -> OrderRequest:
    return OrderRequest(
        exchange="binance",
        symbol="DOGEUSDT",
        order_type="market",
        side="buy",
        quantity=Decimal("13119.21815906"),
        client_order_id=client_order_id,
    )


def _adapter_with(script: list) -> tuple[BinanceAdapter, _FakeClient]:
    a = BinanceAdapter("key", "secret", testnet=True)
    client = _FakeClient(script, name="客户端")
    a._client = client  # type: ignore[assignment]
    a._symbol_rules = {
        "DOGEUSDT": {
            "stepSize": Decimal("0.00000001"),
            "tickSize": Decimal("0.00000001"),
            "minQty": Decimal("0"),
        }
    }  # 本文件只测传输层重试语义，规则取"不改变数量"的精度
    a._leverage_set.add("DOGEUSDT")
    return a, client


class TestOrderRetrySafety(unittest.IsolatedAsyncioTestCase):
    async def test_pre_send_error_reconnects_and_resends(self):
        """连接类错误（请求没出去）→ 重连后重发，安全。"""
        a, client = _adapter_with([httpx.ConnectTimeout("")])
        new = _FakeClient([_Resp(200, ORDER_RAW)], name="新客户端")

        async def fake_connect():
            a._client = new  # type: ignore[assignment]

        with patch.object(
            BinanceAdapter, "connect", AsyncMock(side_effect=fake_connect)
        ) as recon:
            resp = await a.place_order(_req())

        self.assertEqual(resp.exchange_order_id, str(ORDER_RAW["orderId"]))
        self.assertEqual(recon.await_count, 1, "发送前错误应重连")
        self.assertEqual(
            len([r for r in new.requests if r[0] == "POST"]), 1, "并重发一次"
        )

    async def test_read_timeout_does_not_resend_and_reconciles(self):
        """读超时（可能已成交）→ 绝不重发；按 clientOrderId 对账并采用交易所真实状态。"""
        a, client = _adapter_with([httpx.ReadTimeout(""), _Resp(200, ORDER_RAW)])

        with patch.object(BinanceAdapter, "connect", AsyncMock()) as recon:
            resp = await a.place_order(_req("cid-abc"))

        self.assertEqual(resp.exchange_order_id, str(ORDER_RAW["orderId"]))
        self.assertEqual(resp.status, "FILLED")
        self.assertEqual(recon.await_count, 0, "含糊错误不能重连重发")
        posts = [r for r in client.requests if r[0] == "POST"]
        self.assertEqual(len(posts), 1, "必须只有一次提交（否则可能重复下单）")
        gets = [r for r in client.requests if r[0] == "GET"]
        self.assertEqual(len(gets), 1, "必须按 clientOrderId 查一次")
        self.assertIn("origClientOrderId=cid-abc", gets[0][1])

    async def test_read_timeout_without_client_order_id_raises_unknown(self):
        """没有幂等键就无法对账 → 抛「结果未知」（绝不盲重发，也绝不记成确定失败）。"""
        a, client = _adapter_with([httpx.ReadTimeout("")])

        with patch.object(BinanceAdapter, "connect", AsyncMock()) as recon:
            with self.assertRaises(OrderPlacementUnknown):
                await a.place_order(_req(None))

        self.assertEqual(recon.await_count, 0)
        self.assertEqual(len([r for r in client.requests if r[0] == "POST"]), 1)
        self.assertEqual(
            [r for r in client.requests if r[0] == "GET"], [], "无幂等键时不做对账"
        )

    async def test_reconcile_unreachable_raises_unknown(self):
        """对账查询本身失败（网络不可达）→ **未知**，不是失败：那张单可能活着。"""
        a, client = _adapter_with(
            [httpx.ReadTimeout(""), httpx.ConnectError("lookup down")]
        )

        with self.assertRaises(OrderPlacementUnknown) as ctx:
            await a.place_order(_req("cid-abc"))

        self.assertIn(
            "cid-abc", str(ctx.exception), "未知单必须带上幂等键，否则无法人工核对"
        )
        self.assertEqual(
            len([r for r in client.requests if r[0] == "POST"]), 1, "绝不重发"
        )
        self.assertEqual(
            len([r for r in client.requests if r[0] == "GET"]), 1, "必须对账一次"
        )

    async def test_duplicate_reject_with_missing_lookup_raises_unknown(self):
        """交易所说单号重复、反查却说没有 → 两处证据矛盾，如实记未知，不挑一边信。"""
        a, client = _adapter_with(
            [
                _Resp(
                    400,
                    {"code": -4116, "msg": "clientOrderId is duplicated"},
                    text=DUP_BODY,
                ),
                _Resp(
                    400,
                    {"code": -2013, "msg": "Order does not exist."},
                    text=NOT_FOUND_BODY,
                ),
            ]
        )

        with self.assertRaises(OrderPlacementUnknown):
            await a.place_order(_req("cid-abc"))

        self.assertEqual(
            len([r for r in client.requests if r[0] == "POST"]), 1, "绝不重发"
        )

    async def test_read_timeout_reconcile_not_found_raises(self):
        """对账查不到（-2013）→ 说明没被受理，抛出原始错误。"""
        a, client = _adapter_with(
            [
                httpx.ReadTimeout(""),
                _Resp(
                    400,
                    {"code": -2013, "msg": "Order does not exist."},
                    text=NOT_FOUND_BODY,
                ),
            ]
        )
        with self.assertRaises(httpx.ReadTimeout):
            await a.place_order(_req("cid-abc"))
        self.assertEqual(len([r for r in client.requests if r[0] == "GET"]), 1)

    async def test_duplicate_order_rejection_is_treated_as_accepted(self):
        """交易所报 clientOrderId 重复 → 说明前一次已受理：对账取真实状态，不当作失败。"""
        a, client = _adapter_with(
            [
                _Resp(
                    400,
                    {"code": -4116, "msg": "clientOrderId is duplicated"},
                    text=DUP_BODY,
                ),
                _Resp(200, ORDER_RAW),
            ]
        )
        resp = await a.place_order(_req("cid-abc"))

        self.assertEqual(resp.exchange_order_id, str(ORDER_RAW["orderId"]))
        self.assertEqual(resp.status, "FILLED")
        self.assertEqual(len([r for r in client.requests if r[0] == "POST"]), 1)
        self.assertEqual(len([r for r in client.requests if r[0] == "GET"]), 1)

    async def test_read_endpoints_still_retry_on_read_timeout(self):
        """读接口不受此策略影响：读超时照旧重连重试。"""
        a = BinanceAdapter("key", "secret", testnet=True)
        old = _FakeClient([httpx.ReadTimeout("")], name="老客户端")
        new = _FakeClient([_Resp(200, POSITION_PAYLOAD)], name="新客户端")
        a._client = old  # type: ignore[assignment]

        async def fake_connect():
            a._client = new  # type: ignore[assignment]

        with patch.object(
            BinanceAdapter, "connect", AsyncMock(side_effect=fake_connect)
        ) as recon:
            positions = await a.get_positions()

        self.assertEqual(len(positions), 1)
        self.assertEqual(recon.await_count, 1)


class TestAmbiguousAnyioErrors(unittest.IsolatedAsyncioTestCase):
    """anyio 资源类错误（ClosedResourceError 等）在下单路径属于**含糊错误**。

    2026-09-21 加固：这类错误不是 httpx.TransportError，日志里同样只有空消息；
    连接可能是在请求写出去之后才被关闭 → 必须按 clientOrderId 对账，绝不盲目重发。
    """

    async def test_closed_resource_error_on_order_reconciles(self):
        from anyio import ClosedResourceError

        # 脚本：下单 POST 抛 ClosedResourceError → 对账 GET 命中真实订单
        a, client = _adapter_with([ClosedResourceError(""), _Resp(200, ORDER_RAW)])

        resp = await a.place_order(_req("cid-abc"))

        self.assertEqual(resp.exchange_order_id, "2347999999")
        posts = [c for c in client.requests if c[0] == "POST"]
        gets = [c for c in client.requests if c[0] == "GET"]
        self.assertEqual(len(posts), 1, f"绝不重发：{client.requests}")
        self.assertEqual(len(gets), 1, f"必须对账一次：{client.requests}")

    async def test_broken_resource_error_on_order_without_match_raises(self):
        """交易所明确回答 -2013（订单不存在）→ **确定没受理**，按原错误抛出，不臆造成功。

        与 `test_reconcile_unreachable_raises_unknown` 成对：同样是"查不到"，
        交易所说"没有"是事实（→ failed），问不到交易所只是"不知道"（→ unknown）。
        """
        from anyio import BrokenResourceError

        a, client = _adapter_with(
            [
                BrokenResourceError(""),
                _Resp(
                    400,
                    {"code": -2013, "msg": "Order does not exist."},
                    text=NOT_FOUND_BODY,
                ),
            ]
        )

        with self.assertRaises(BrokenResourceError):
            await a.place_order(_req("cid-abc"))

        posts = [c for c in client.requests if c[0] == "POST"]
        self.assertEqual(len(posts), 1, "绝不重发")
