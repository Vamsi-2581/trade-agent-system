"""适配器必须按**账户**解析，不能按交易所名（2026-09-22 / 2026-10-12 收口）。

事故形状：曾有一张以**交易所名**为键的适配器表（``_adapters``），同一交易所有两个活跃
账户时后加载的顶掉先加载的，而 ``_load_adapters`` 的日志只有一行 "Adapter loaded:
binance"，看不出谁顶了谁。于是下单、成交同步、持仓视图都可能拿着**另一个账户**的
密钥去做——订单落到错账户上是真实的资金错误，而日志里看不出来。

本文件钉的是收口后的口径（**只有一张表**）：

1. ``_account_adapters`` 以 ``ExchangeAccount.id`` 为键，每个活跃账户各有自己的
   适配器（包含各自的密钥与 testnet 属性）。
2. 同交易所有多个活跃账户**不再是异常**：每个账户各有一条，没有「谁顶掉谁」。
   按名字索引的表已删除，所以不需要再记「哪个交易所名有歧义」。
3. ``resolve_adapter(account_id)`` 是**唯一**的查找 interface，绝不回退到别的账户：
   拿不到就返回 None + WARNING。空账户 id 同样返回 None。
4. 成交同步按**订单自己的账户**取适配器：原先按交易所名分组，两个账户的订单都被
   交给同一个适配器去查，查不到就记 cancelled——把一张活着的单写成已撤销。
5. ``iter_adapters()`` 覆盖每个账户，标签形如 ``binance#<账户id>``。
6. 三个读接口（cancel / positions / balance）首参是**账户 id**；传交易所名 fail-loud。
"""

from __future__ import annotations

import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from apps.trading.adapters.base import OrderNotFoundError
from apps.trading.executor import OrderExecutor


class _FakeAdapter:
    """只记下解密出来的密钥与自己被 connect/disconnect 的次数。"""

    def __init__(self, api_key, api_secret, testnet):
        self.api_key = api_key
        self.api_secret = api_secret
        self.testnet = testnet
        self.connected = 0
        self.disconnected = 0

    async def connect(self):
        self.connected += 1

    async def disconnect(self):
        self.disconnected += 1


def _account(
    account_id,
    *,
    exchange="binance",
    label="",
    testnet=False,
    key=b"key",
    secret=b"secret",
):
    return SimpleNamespace(
        id=account_id,
        exchange=exchange,
        label=label,
        testnet=testnet,
        api_key_enc=key,
        api_secret_enc=secret,
    )


def _fake_fernet():
    """解密＝原样返回，于是适配器里的密钥就是夹具写的那串，能逐个断言。"""
    fernet = MagicMock()
    fernet.decrypt.side_effect = lambda raw: bytes(raw)
    return fernet


def _fake_order(**overrides):
    base = {
        "id": "order-uuid-1",
        "side": "buy",
        "symbol": "DOGE/USDT",
        "exchange_order_id": "111",
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


class _LoadAdaptersTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.executor = OrderExecutor()
        self.addCleanup(setattr, OrderExecutor, "_instance", None)

    async def _load(self, *accounts):
        manager = MagicMock()
        manager.filter.return_value = list(accounts)
        with (
            patch("apps.exchange.models.ExchangeAccount.objects", manager),
            patch.dict("apps.trading.executor.ADAPTER_MAP", {"binance": _FakeAdapter}),
            patch.object(
                OrderExecutor, "_get_fernet", MagicMock(return_value=_fake_fernet())
            ),
        ):
            await self.executor._load_adapters()


class TestAccountKeyedAdapters(_LoadAdaptersTest):
    async def test_each_active_account_gets_its_own_adapter(self):
        """两个同交易所账户 → 两个适配器，密钥各归各的（钱就卡在这条边界上）。"""
        await self._load(
            _account("acc-1", label="main", key=b"key-1", secret=b"secret-1"),
            _account(
                "acc-2", label="sub", key=b"key-2", secret=b"secret-2", testnet=True
            ),
        )

        self.assertEqual(set(self.executor._account_adapters), {"acc-1", "acc-2"})
        first = self.executor._account_adapters["acc-1"]
        second = self.executor._account_adapters["acc-2"]
        self.assertIsNot(first, second)
        self.assertEqual((first.api_key, first.api_secret), ("key-1", "secret-1"))
        self.assertEqual((second.api_key, second.api_secret), ("key-2", "secret-2"))
        self.assertFalse(first.testnet)
        self.assertTrue(second.testnet)
        self.assertEqual((first.connected, second.connected), (1, 1))

    async def test_two_accounts_on_one_exchange_keep_both(self):
        """同交易所两个账户不再是「顶掉」：两条都在，各自可取。

        2026-10-12 之前这里会额外记一条「binance 有歧义」的 ERROR——那是按名字解析
        存在的代价。名字这条路已删除，两个账户并存就是正常状态，不需要告警。
        """
        with self.assertNoLogs("apps.trading.executor", level="ERROR"):
            await self._load(_account("acc-1"), _account("acc-2"))

        self.assertEqual(set(self.executor._account_adapters), {"acc-1", "acc-2"})
        self.assertIs(
            self.executor.resolve_adapter("acc-1"),
            self.executor._account_adapters["acc-1"],
        )
        self.assertIs(
            self.executor.resolve_adapter("acc-2"),
            self.executor._account_adapters["acc-2"],
        )

    async def test_no_by_name_map_exists(self):
        """按交易所名索引的那张表必须**不存在**（收口的实质）。

        留着它就意味着「同一个问题有两个答案」：一处按账户、一处按名字，而按名字那处
        在同交易所两账户时必然选错人。
        """
        await self._load(_account("acc-1"), _account("acc-2"))

        self.assertFalse(hasattr(self.executor, "_adapters"))
        self.assertFalse(hasattr(self.executor, "_unique_adapters"))
        self.assertFalse(hasattr(self.executor, "_resolve_adapter"))
        self.assertFalse(hasattr(self.executor, "is_ambiguous_exchange"))


class TestResolveAdapter(unittest.TestCase):
    def setUp(self):
        self.executor = OrderExecutor()
        self.addCleanup(setattr, OrderExecutor, "_instance", None)

    def test_each_account_resolves_to_its_own_adapter(self):
        """同交易所两个账户各拿各的——按名字只会拿到其中一个。"""
        mine, theirs = MagicMock(), MagicMock()
        self.executor._account_adapters = {"acc-1": mine, "acc-2": theirs}

        self.assertIs(self.executor.resolve_adapter("acc-1"), mine)
        self.assertIs(self.executor.resolve_adapter("acc-2"), theirs)

    def test_unknown_account_never_falls_back_to_another_account(self):
        """账户清单非空却没有这个账户 → None + WARNING。回退等于用别人的密钥下单。"""
        theirs = MagicMock()
        self.executor._account_adapters = {"acc-2": theirs}

        with self.assertLogs("apps.trading.executor", level="WARNING") as logs:
            self.assertIsNone(self.executor.resolve_adapter("acc-missing"))
        self.assertIn("acc-missing", "\n".join(logs.output))

    def test_empty_account_id_resolves_to_nothing(self):
        """空账户 id → None（不再有「按交易所名回退」这条路）。"""
        self.executor._account_adapters = {"acc-1": MagicMock()}

        self.assertIsNone(self.executor.resolve_adapter(None))
        self.assertIsNone(self.executor.resolve_adapter(""))

    def test_exchange_name_is_not_an_account_id(self):
        """传交易所名解析不出东西——fail-loud，而不是静默拿错账户。"""
        self.executor._account_adapters = {"acc-1": MagicMock()}

        self.assertIsNone(self.executor.resolve_adapter("binance"))

    def test_iter_adapters_labels_every_account(self):
        """`iter_adapters()` 覆盖每个账户，标签带交易所名与账户 id。"""
        mine, theirs = MagicMock(), MagicMock()
        self.executor._account_adapters = {"acc-1": mine, "acc-2": theirs}
        self.executor._account_exchanges = {"acc-1": "binance", "acc-2": "binance"}

        self.assertEqual(
            self.executor.iter_adapters(),
            [("binance#acc-1", mine), ("binance#acc-2", theirs)],
        )

    def test_account_adapters_is_a_copy(self):
        """`account_adapters()` 返回副本：调用方改不动内部状态。"""
        adapter = MagicMock()
        self.executor._account_adapters = {"acc-1": adapter}

        snapshot = self.executor.account_adapters()
        snapshot["acc-2"] = MagicMock()

        self.assertEqual(set(self.executor._account_adapters), {"acc-1"})


class TestReadInterfacesAreAccountScoped(unittest.IsolatedAsyncioTestCase):
    """cancel / positions / balance 的首参是**账户 id**（2026-10-12 收口）。"""

    async def asyncSetUp(self):
        self.executor = OrderExecutor()
        self.executor._running = True
        self.addCleanup(setattr, OrderExecutor, "_instance", None)

    async def test_each_read_interface_resolves_by_account(self):
        mine, theirs = AsyncMock(), AsyncMock()
        self.executor._account_adapters = {"acc-1": mine, "acc-2": theirs}

        await self.executor.cancel_order("acc-1", "111", "DOGE/USDT")
        mine.cancel_order.assert_awaited_once_with("111", "DOGE/USDT")
        theirs.cancel_order.assert_not_awaited()

        await self.executor.get_positions("acc-2")
        theirs.get_positions.assert_awaited_once()
        mine.get_positions.assert_not_awaited()

        await self.executor.get_balance("acc-2")
        theirs.get_balance.assert_awaited_once()

    async def test_exchange_name_is_rejected_loudly(self):
        """传交易所名 → ValueError，绝不落到「同交易所另一个账户」。"""
        self.executor._account_adapters = {"acc-1": AsyncMock()}

        for coro in (
            self.executor.cancel_order("binance", "111", "DOGE/USDT"),
            self.executor.get_positions("binance"),
            self.executor.get_balance("binance"),
        ):
            with self.assertRaises(ValueError):
                await coro


class TestShutdownDisconnectsOnce(unittest.IsolatedAsyncioTestCase):
    async def test_every_account_adapter_is_disconnected_once(self):
        """每个账户各断一次（一张表，不再有指向同一实例的重复条目）。"""
        executor = OrderExecutor()
        first, second = _FakeAdapter("k", "s", False), _FakeAdapter("k", "s", False)
        executor._account_adapters = {"acc-1": first, "acc-2": second}
        executor._account_exchanges = {"acc-1": "binance", "acc-2": "binance"}
        executor._running = True
        self.addCleanup(setattr, OrderExecutor, "_instance", None)

        await executor.shutdown()

        self.assertEqual((first.disconnected, second.disconnected), (1, 1))
        self.assertEqual(executor._account_adapters, {})


class TestFillSyncUsesTheOrdersOwnAccount(unittest.IsolatedAsyncioTestCase):
    """同交易所两个账户的订单各用**自己那个账户**的适配器查成交。

    原先按交易所名分组 → 两个账户的订单都交给同一个（后加载的）适配器去查，
    查不到就记 cancelled：本地账面把一张活着的单写成已撤销。
    """

    async def test_each_order_is_queried_through_its_own_account_adapter(self):
        executor = OrderExecutor()
        executor._running = True
        mine, theirs = MagicMock(), MagicMock()
        mine.fetch_order = AsyncMock(side_effect=OrderNotFoundError("no such order"))
        theirs.fetch_order = AsyncMock(side_effect=OrderNotFoundError("no such order"))
        executor._account_adapters = {"acc-1": mine, "acc-2": theirs}
        self.addCleanup(setattr, OrderExecutor, "_instance", None)

        manager = MagicMock()
        manager.filter.return_value.select_related.return_value = [
            _fake_order(id="o-1", exchange_account_id="acc-1", exchange_order_id="111"),
            _fake_order(id="o-2", exchange_account_id="acc-2", exchange_order_id="222"),
        ]

        with patch("apps.trading.models.Order.objects", manager):
            await executor._sync_active_orders()

        mine.fetch_order.assert_awaited_once_with("111", "DOGE/USDT")
        theirs.fetch_order.assert_awaited_once_with("222", "DOGE/USDT")

    async def test_order_without_a_loaded_adapter_is_not_queried_by_another(self):
        """账户没有已加载的适配器（密钥缺失/已停用）→ 跳过，绝不借用别人的。"""
        executor = OrderExecutor()
        executor._running = True
        theirs = MagicMock()
        theirs.fetch_order = AsyncMock(side_effect=OrderNotFoundError("no such order"))
        executor._account_adapters = {"acc-2": theirs}
        self.addCleanup(setattr, OrderExecutor, "_instance", None)

        manager = MagicMock()
        manager.filter.return_value.select_related.return_value = [
            _fake_order(id="o-1", exchange_account_id="acc-1"),
        ]

        with patch("apps.trading.models.Order.objects", manager):
            await executor._sync_active_orders()

        theirs.fetch_order.assert_not_awaited()
