"""Binance 适配器传输层自愈 + 时钟重同步（2026-09-20 线上事故回归）。

事故取证结论（本文件锁定其修复契约）：
  - 线上长活适配器实例自 09-18 10:51:45 加载后未重载；09-20 17:00 起 guard 每 70 秒失败一次，
    共 681 次、持续约 10 小时，日志内容只有 "Failed to fetch positions from binance: "。
  - 那个"空消息"= httpx 超时异常（httpx 构造这类异常时 message 为空），所以只按 {e} 记日志
    完全看不出异常类型 —— 这次排查被迫靠现场探测才定性。
  - 同一容器内新建适配器实调完全正常（4 个真实持仓、连续 3 轮 3/3 成功）→ 故障在被卡死的
    长活客户端上；旧实现既不重连也不重试 → 永不恢复，只能重启进程。
  - 另有一批 400 的真实内容是 -1021（Timestamp outside recvWindow），根因是时钟偏移只在
    connect 时同步一次、之后永不刷新。

契约：
  1. 传输层故障（httpx.TransportError）→ 关闭旧客户端、重建、重试一次（重试必须重新签名，
     timestamp 重新生成）。
  2. 收到 -1021 → 重新同步时钟后重试一次。
  3. 重试后仍失败 → 抛出（有界重试，不做无限循环）。
  4. 非传输层错误（如 500）→ 不重连、不重试，交由调用方处理。
"""

from __future__ import annotations

import unittest
from decimal import Decimal
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

import httpx

from apps.trading.adapters.binance import BinanceAdapter

POSITION_PAYLOAD = [
    {
        "symbol": "BNBUSDT",
        "positionAmt": "2.0",
        "entryPrice": "600",
        "unRealizedProfit": "-12.5",
        "leverage": "10",
        "markPrice": "594",
    }
]
BALANCE_PAYLOAD = [{"asset": "USDT", "balance": "1000", "walletBalance": "1010"}]
ORDER_PAYLOAD = {"orderId": 12345, "status": "NEW", "executedQty": "0", "avgPrice": "0"}
_1021_TEXT = (
    '{"code":-1021,"msg":"Timestamp for this request is outside of the recvWindow."}'
)


class _Resp:
    """最小 httpx.Response 替身。"""

    def __init__(self, status_code: int = 200, data=None, text: str = ""):
        self.status_code = status_code
        self._data = data
        self.text = text

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"Client error '{self.status_code}'",
                request=httpx.Request("GET", "https://demo-fapi.binance.com/"),
                response=httpx.Response(self.status_code),
            )


class _FakeClient:
    """记录请求（method, path）的假客户端；脚本项为响应或要抛出的异常。"""

    def __init__(self, script: list, name: str = "client"):
        self.script = list(script)
        self.name = name
        self.requests: list[tuple[str, str]] = []
        self.closed = False

    async def _send(self, method: str, path: str, **kwargs):
        self.requests.append((method, path))
        if not self.script:
            raise AssertionError(f"{self.name}: 收到超出脚本的请求 {method} {path}")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def get(self, path: str, **kwargs):
        return await self._send("GET", path, **kwargs)

    async def post(self, path: str, **kwargs):
        return await self._send("POST", path, **kwargs)

    async def delete(self, path: str, **kwargs):
        return await self._send("DELETE", path, **kwargs)

    async def aclose(self):
        self.closed = True


def _adapter(script_old: list, script_new: list | None = None) -> tuple:
    """返回 (adapter, 老客户端, 新客户端)；connect 被替换为"换成新客户端"。"""
    a = BinanceAdapter("key", "secret", testnet=True)
    old = _FakeClient(script_old, name="老客户端")
    a._client = old  # type: ignore[assignment]
    if script_new is None:
        return a, old, None
    new = _FakeClient(script_new, name="新客户端")

    async def fake_connect():
        a._client = new  # type: ignore[assignment]

    return a, old, new


class TestTransportSelfHeal(unittest.IsolatedAsyncioTestCase):
    """传输层故障 → 重建客户端并重试一次。"""

    async def test_positions_transport_timeout_reconnects_and_retries_once(self):
        a, old, new = _adapter(
            [httpx.ConnectTimeout("")], [_Resp(200, POSITION_PAYLOAD)]
        )
        with patch.object(
            BinanceAdapter,
            "connect",
            AsyncMock(side_effect=lambda: _set_client(a, new)),
        ) as recon:
            positions = await a.get_positions()

        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0].symbol, "BNBUSDT")
        self.assertEqual(recon.await_count, 1, "传输层故障必须重建客户端")
        self.assertTrue(old.closed, "旧客户端必须被关闭，否则连接池一直烂着")
        self.assertIs(a._client, new, "重试必须走新客户端")

    async def test_closed_resource_error_on_read_retries_after_rebuild(self):
        """anyio ClosedResourceError（空消息，非 httpx.TransportError）也按连接层故障处理。

        线上实测（2026-09-21）：适配器客户端被并发重建关闭时，在途读请求会报
        `ClosedResourceError:`（消息为空）。读接口必须重建后重试一次，而不是把这次
        同步失败直接抛给上层（那会让策略在持仓未知时被迫跳过一轮）。
        """
        from anyio import ClosedResourceError

        a, old, new = _adapter(
            [ClosedResourceError(""), _Resp(200, POSITION_PAYLOAD)],
            [_Resp(200, POSITION_PAYLOAD)],
        )
        with patch.object(
            BinanceAdapter,
            "connect",
            AsyncMock(side_effect=lambda: _set_client(a, new)),
        ) as recon:
            positions = await a.get_positions()

        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0].symbol, "BNBUSDT")
        self.assertEqual(recon.await_count, 1, "连接层错误必须重建客户端后重试")
        self.assertIs(a._client, new)

    async def test_positions_timeout_after_reconnect_raises_and_retries_only_once(self):
        a, old, new = _adapter([httpx.ConnectTimeout("")], [httpx.ConnectTimeout("")])
        with patch.object(
            BinanceAdapter,
            "connect",
            AsyncMock(side_effect=lambda: _set_client(a, new)),
        ) as recon:
            with self.assertRaises(httpx.ConnectTimeout):
                await a.get_positions()

        self.assertEqual(recon.await_count, 1, "只允许重连一次")
        self.assertEqual(len(new.requests), 1, "只允许重试一次")
        self.assertEqual(len(old.requests), 1)

    async def test_retry_request_is_resigned_with_fresh_timestamp(self):
        a, old, new = _adapter(
            [httpx.ConnectTimeout("")], [_Resp(200, POSITION_PAYLOAD)]
        )
        with patch.object(
            BinanceAdapter,
            "connect",
            AsyncMock(side_effect=lambda: _set_client(a, new)),
        ):
            await a.get_positions()

        first = parse_qs(urlparse(old.requests[0][1]).query)
        second = parse_qs(urlparse(new.requests[0][1]).query)
        self.assertIn("signature", first)
        self.assertIn("signature", second, "重试请求必须重新签名")
        self.assertGreaterEqual(
            int(second["timestamp"][0]),
            int(first["timestamp"][0]),
            "重试必须重新生成 timestamp（否则 -1021 类错误重试也无意义）",
        )

    async def test_balance_transport_timeout_reconnects_and_returns_balance(self):
        a, old, new = _adapter(
            [httpx.ConnectTimeout("")], [_Resp(200, BALANCE_PAYLOAD)]
        )
        with patch.object(
            BinanceAdapter,
            "connect",
            AsyncMock(side_effect=lambda: _set_client(a, new)),
        ):
            balance = await a.get_balance()

        self.assertEqual(balance["USDT"], 1000)
        self.assertTrue(old.closed)

    async def test_place_order_transport_timeout_reconnects_and_places(self):
        a, old, new = _adapter([httpx.ConnectTimeout("")], [_Resp(200, ORDER_PAYLOAD)])
        a._symbol_rules = {
            "BNBUSDT": {
                "stepSize": Decimal("1"),
                "tickSize": Decimal("0.01"),
                "minQty": Decimal("0"),
            }
        }  # 本文件只测自愈语义，规则取"不改变下单参数"的精度
        a._leverage_set.add("BNBUSDT")
        request = _order_request()

        with patch.object(
            BinanceAdapter,
            "connect",
            AsyncMock(side_effect=lambda: _set_client(a, new)),
        ) as recon:
            resp = await a.place_order(request)

        self.assertEqual(resp.exchange_order_id, "12345")
        self.assertEqual(recon.await_count, 1)

    async def test_http_error_is_not_retried(self):
        a, old, _ = _adapter([_Resp(500, None, text="boom")])
        with patch.object(BinanceAdapter, "connect", AsyncMock()) as recon:
            with self.assertRaises(httpx.HTTPStatusError):
                await a.get_positions()

        self.assertEqual(recon.await_count, 0, "非传输层错误不该触发重连")
        self.assertEqual(len(old.requests), 1, "非传输层错误不该重试")


class TestTimestampResync(unittest.IsolatedAsyncioTestCase):
    """-1021（timestamp 超出 recvWindow）→ 重新同步时钟并重试一次。"""

    async def test_1021_triggers_clock_resync_and_retry(self):
        a, old, _ = _adapter(
            [
                _Resp(400, {"code": -1021, "msg": "..."}, text=_1021_TEXT),
                _Resp(200, POSITION_PAYLOAD),
            ]
        )
        with patch.object(BinanceAdapter, "_sync_time", AsyncMock()) as sync:
            positions = await a.get_positions()

        self.assertEqual(len(positions), 1)
        self.assertEqual(sync.await_count, 1, "-1021 必须重新同步时钟")
        self.assertEqual(len(old.requests), 2, "必须重试一次")
        self.assertIs(a._client, old, "-1021 不需要换客户端")

    async def test_other_400_is_not_retried(self):
        a, old, _ = _adapter(
            [_Resp(400, {"code": -2015, "msg": "Invalid API-key"}, text="x")]
        )
        with patch.object(BinanceAdapter, "_sync_time", AsyncMock()) as sync:
            with self.assertRaises(httpx.HTTPStatusError):
                await a.get_positions()

        self.assertEqual(sync.await_count, 0)
        self.assertEqual(len(old.requests), 1, "非 -1021 的 400 不该重试")


def _set_client(adapter: BinanceAdapter, client: _FakeClient) -> None:
    """模拟 connect() 的效果：换上新客户端（协程函数，供 AsyncMock.side_effect 使用）。"""
    adapter._client = client  # type: ignore[assignment]


def _order_request():
    from decimal import Decimal

    from apps.trading.adapters.base import OrderRequest

    return OrderRequest(
        exchange="binance",
        symbol="BNBUSDT",
        order_type="limit",
        side="buy",
        quantity=Decimal("2"),
        price=Decimal("600"),
    )
