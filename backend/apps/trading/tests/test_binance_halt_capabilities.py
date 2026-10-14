"""②a：熔断/减仓所需的四项适配器能力。

这一批改动本身**不含任何业务决策**，纯粹是补齐「事件熔断要用的手」：只减不增标志、
最小名义价值、挂单枚举、中间价。所以这些测试全部钉在**适配器发出的请求与回给调用方
的返回值**上——它们是 ②b–②d 的地基，地基错了上层每一条判据都是错的。

四个测试类对应四处能力缺口，另一类钉住两处顺手修掉的老 bug（撤单符号未归一化、
未签名请求丢参数）——那两处此前无生产调用方所以从未显形，而熔断动作恰好是它们的
第一个调用方。
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock

from django.test import SimpleTestCase

from apps.trading.adapters.base import (
    BaseExchangeAdapter,
    OrderRequest,
)
from apps.trading.adapters.binance import BinanceAdapter


class _Resp:
    def __init__(self, status: int, payload=None, text: str = ""):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


_SYMBOL = "DOGE/USDT"
_RAW_SYMBOL = "DOGEUSDT"


def _adapter() -> BinanceAdapter:
    """已就绪的适配器：规则与杠杆都预热好，测试只关心本次要验的那一件事。"""
    a = BinanceAdapter("key", "secret", testnet=True)
    a._leverage_set.add(_RAW_SYMBOL)
    a._symbol_rules = {
        _RAW_SYMBOL: {
            "stepSize": Decimal("1"),
            "tickSize": Decimal("0.01"),
            "minQty": Decimal("1"),
            "minNotional": Decimal("5"),
        }
    }
    return a


def _req(*, qty: str = "100", reduce_only: bool = False) -> OrderRequest:
    return OrderRequest(
        exchange="binance",
        symbol=_SYMBOL,
        order_type="market",
        side="sell",
        quantity=Decimal(qty),
        price=None,
        reduce_only=reduce_only,
    )


def _posts_to(client, fragment: str) -> list:
    return [c for c in client.post.call_args_list if fragment in str(c.args[0])]


class TestReduceOnlyFlag(SimpleTestCase):
    """只减不增：CONTEXT.md:128 点名它是自动减仓的硬前置。"""

    def test_flag_is_translated_to_the_exchange_parameter(self):
        client = AsyncMock()
        client.post.return_value = _Resp(
            200, {"orderId": 1, "status": "NEW", "executedQty": "0", "avgPrice": "0"}
        )
        a = _adapter()
        a._client = client

        asyncio.run(a.place_order(_req(reduce_only=True)))

        posts = _posts_to(client, "/order")
        self.assertEqual(len(posts), 1, client.post.call_args_list)
        self.assertIn("reduceOnly=true", posts[0].args[0])

    def test_flag_absent_means_an_ordinary_order(self):
        """默认 False 时**不许**出现 reduceOnly：开仓单被交易所强制只减不增的话，
        会静默变成一张废单——而它看起来和成功下单一模一样。
        """
        client = AsyncMock()
        client.post.return_value = _Resp(
            200, {"orderId": 1, "status": "NEW", "executedQty": "0", "avgPrice": "0"}
        )
        a = _adapter()
        a._client = client

        asyncio.run(a.place_order(_req(reduce_only=False)))

        posts = _posts_to(client, "/order")
        self.assertEqual(len(posts), 1, client.post.call_args_list)
        self.assertNotIn("reduceOnly", posts[0].args[0])

    def test_the_base_contract_defaults_to_off(self):
        """基类默认必须是关的：任何漏传该标志的新调用点都退化成普通下单，
        而不是悄悄获得「只减不增」——后者会让一张本该开仓的单莫名被拒。
        """
        self.assertFalse(
            OrderRequest(
                exchange="binance",
                symbol=_SYMBOL,
                order_type="market",
                side="sell",
                quantity=Decimal("1"),
            ).reduce_only
        )


class TestMinNotional(SimpleTestCase):
    """最小名义价值：CONTEXT.md:127 要求适配器补读取，减仓分片据此降片。"""

    def test_min_notional_is_read_from_the_filter(self):
        client = AsyncMock()
        client.get.return_value = _Resp(
            200,
            {
                "symbols": [
                    {
                        "symbol": _RAW_SYMBOL,
                        "filters": [
                            {"filterType": "LOT_SIZE", "stepSize": "1", "minQty": "1"},
                            {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                            {"filterType": "MIN_NOTIONAL", "notional": "5"},
                        ],
                    }
                ]
            },
        )
        a = BinanceAdapter("key", "secret", testnet=True)
        a._client = client

        self.assertEqual(asyncio.run(a.min_notional(_SYMBOL)), Decimal("5"))
        self.assertEqual(a._symbol_rules[_RAW_SYMBOL]["minNotional"], Decimal("5"))

    def test_absent_filter_reads_as_zero(self):
        """没声明该 filter ⇒ 读到 0（=「本适配器不声明该约束」），不是抛错、也不是 None。"""
        a = _adapter()
        self.assertEqual(asyncio.run(a.min_notional("BTC/USDT")), Decimal("0"))

    def test_load_failure_raises_instead_of_returning_zero(self):
        """取不到规则必须抛：返回 0 会被上层读成「交易所什么都收」，于是每片都按
        不降片发出，然后被交易所逐张拒掉（-4164）——失败率被污染，滑点被间接污染。
        """
        client = AsyncMock()
        client.get.return_value = _Resp(500, text="boom")
        a = BinanceAdapter("key", "secret", testnet=True)
        a._client = client

        with self.assertRaises(RuntimeError):
            asyncio.run(a.min_notional(_SYMBOL))


class TestFetchOpenOrders(SimpleTestCase):
    """挂单枚举：CONTEXT.md:122 规定撤单依据只能来自交易所侧。"""

    def test_orders_are_parsed_into_responses(self):
        client = AsyncMock()
        client.get.return_value = _Resp(
            200,
            [
                {
                    "orderId": 11,
                    "status": "NEW",
                    "executedQty": "0",
                    "avgPrice": "0",
                    "side": "BUY",
                },
                {
                    "orderId": 12,
                    "status": "PARTIALLY_FILLED",
                    "executedQty": "3",
                    "avgPrice": "0.0710",
                    "side": "SELL",
                },
            ],
        )
        a = _adapter()
        a._client = client

        orders = asyncio.run(a.fetch_open_orders(_SYMBOL))

        self.assertEqual([o.exchange_order_id for o in orders], ["11", "12"])
        self.assertEqual(orders[1].status, "PARTIALLY_FILLED")
        self.assertEqual(orders[1].filled_qty, Decimal("3"))
        self.assertEqual(orders[1].avg_price, Decimal("0.0710"))

    def test_empty_list_means_truly_none(self):
        client = AsyncMock()
        client.get.return_value = _Resp(200, [])
        a = _adapter()
        a._client = client

        self.assertEqual(asyncio.run(a.fetch_open_orders(_SYMBOL)), [])

    def test_non_200_raises(self):
        """「没查到」与「没有挂单」是两件事：前者必须抛，绝不能退化成空列表。"""
        client = AsyncMock()
        client.get.return_value = _Resp(500, text="boom")
        a = _adapter()
        a._client = client

        with self.assertRaises(RuntimeError):
            asyncio.run(a.fetch_open_orders(_SYMBOL))

    def test_error_body_shaped_as_object_raises(self):
        """限额/权限类错误币安会回一个 dict，逐字段解析会静默产出空列表。"""
        client = AsyncMock()
        client.get.return_value = _Resp(200, {"code": -1121, "msg": "Invalid symbol."})
        a = _adapter()
        a._client = client

        with self.assertRaises(RuntimeError):
            asyncio.run(a.fetch_open_orders(_SYMBOL))

    def test_symbol_is_normalized(self):
        client = AsyncMock()
        client.get.return_value = _Resp(200, [])
        a = _adapter()
        a._client = client

        asyncio.run(a.fetch_open_orders(_SYMBOL))

        self.assertIn(
            f"symbol={_RAW_SYMBOL}", str(client.get.call_args_list[0].args[0])
        )


class TestFetchMidPrice(SimpleTestCase):
    """中间价：CONTEXT.md:53/:129 规定滑点基线取 (best bid + best ask)/2。"""

    def _book(self, bid: str, ask: str):
        client = AsyncMock()
        client.get.return_value = _Resp(200, {"bidPrice": bid, "askPrice": ask})
        a = _adapter()
        a._client = client
        return a, client

    def test_mid_is_the_average_of_bid_and_ask(self):
        a, _ = self._book("0.0700", "0.0702")
        self.assertEqual(asyncio.run(a.fetch_mid_price(_SYMBOL)), Decimal("0.0701"))

    def test_symbol_is_passed_on_an_unsigned_request(self):
        """未签名请求原先会把 params 整个丢掉——那会查到**全市场**盘口，
        再把结果解析成「取价失败」。这条钉住那处修复。
        """
        a, client = self._book("0.0700", "0.0702")

        asyncio.run(a.fetch_mid_price(_SYMBOL))

        url = str(client.get.call_args_list[0].args[0])
        self.assertIn("/fapi/v1/ticker/bookTicker", url)
        self.assertIn(f"symbol={_RAW_SYMBOL}", url)

    def test_crossed_book_reads_as_unavailable(self):
        """买价高于卖价 = 盘口瞬间异常，拿它算出的「滑点」不反映任何真实成交代价。"""
        a, _ = self._book("0.0702", "0.0700")
        self.assertIsNone(asyncio.run(a.fetch_mid_price(_SYMBOL)))

    def test_zero_or_negative_quote_reads_as_unavailable(self):
        for bid, ask in (("0", "0.0702"), ("0.0700", "0"), ("-1", "0.0702")):
            with self.subTest(bid=bid, ask=ask):
                a, _ = self._book(bid, ask)
                self.assertIsNone(asyncio.run(a.fetch_mid_price(_SYMBOL)))

    def test_http_failure_reads_as_unavailable_never_zero(self):
        """**绝不返回 0**：0 会在滑点公式里变成「完美的价格」，而这条记录正是
        「减仓成本失控」的唯一告警依据（CONTEXT.md:129）。
        """
        client = AsyncMock()
        client.get.return_value = _Resp(500, text="boom")
        a = _adapter()
        a._client = client

        self.assertIsNone(asyncio.run(a.fetch_mid_price(_SYMBOL)))

    def test_unparseable_payload_reads_as_unavailable(self):
        for payload in ({"msg": "ok"}, {"bidPrice": "abc", "askPrice": "1"}):
            with self.subTest(payload=payload):
                client = AsyncMock()
                client.get.return_value = _Resp(200, payload)
                a = _adapter()
                a._client = client
                self.assertIsNone(asyncio.run(a.fetch_mid_price(_SYMBOL)))


class TestCancelOrderSymbolNormalization(SimpleTestCase):
    """撤单是 halt 的第一步，而它的符号归一化此前独独漏了 ``.replace("/", "")``。"""

    def test_slash_is_stripped(self):
        client = AsyncMock()
        client.delete.return_value = _Resp(200, {})
        a = _adapter()
        a._client = client

        self.assertTrue(asyncio.run(a.cancel_order("11", _SYMBOL)))

        url = str(client.delete.call_args_list[0].args[0])
        self.assertIn(f"symbol={_RAW_SYMBOL}", url)
        self.assertNotIn("DOGE%2FUSDT", url)

    def test_non_200_is_false_not_an_exception(self):
        """撤单失败是可以继续下去的事实（Q8：撤单失败不阻断减仓），所以这里是 False。"""
        client = AsyncMock()
        client.delete.return_value = _Resp(400, text="-2011 Unknown order")
        a = _adapter()
        a._client = client

        self.assertFalse(asyncio.run(a.cancel_order("11", _SYMBOL)))


class TestBaseAdapterContract(SimpleTestCase):
    """基类是未来适配器作者唯一会读的东西，能力缺口必须在那里写清楚。"""

    def test_halt_capabilities_are_declared_on_the_base(self):
        for name in ("fetch_open_orders", "fetch_mid_price", "min_notional"):
            with self.subTest(method=name):
                self.assertTrue(
                    hasattr(BaseExchangeAdapter, name),
                    f"基类缺少 {name}：能力缺口会退化成「没人想到要做这件事」",
                )

    def test_enumeration_is_mandatory_but_min_notional_is_optional(self):
        """枚举挂单是保命档的必经之路（能力缺失必须显式抛错），
        而「不声明最小额约束」是一种合法状态（默认 0）。
        """
        self.assertIn("fetch_open_orders", BaseExchangeAdapter.__abstractmethods__)
        self.assertIn("fetch_mid_price", BaseExchangeAdapter.__abstractmethods__)
        self.assertNotIn("min_notional", BaseExchangeAdapter.__abstractmethods__)
