"""减仓成交落库层（``apps/trading/reduce_ledger.py``）与交易所回填命令。

钉住的是 2026-09-28 事故的**修复面**：减仓成交必须落 ``Order`` 行、符号必须转成斜杠
口径、归属只在唯一活跃会话时成立、幂等靠交易所单号；回补行的 ``created_at`` 必须回填到
交易时刻（否则成本链修不回来）。

纯函数（``to_session_symbol`` / ``_group_trades_by_order``）用 ``SimpleTestCase``；
涉及 DB 的路径走 ``db_async``（``sync_to_async`` 那条线程要看得见 fixture 的行），
所以必须用 ``pytest.mark.django_db(transaction=True)``——见记忆「django_db 守卫」与
「容器内跑 pytest 要显式 DB_HOST/DB_PORT」。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import SimpleTestCase

from apps.exchange.models import ExchangeAccount
from apps.trading import reduce
from apps.trading.models import LiveSession, Order, Strategy
from apps.trading.reduce_ledger import (
    TRIGGER_MANUAL_REDUCE,
    _group_trades_by_order,
    persist_reduce_order,
    resolve_session_for,
    to_session_symbol,
)

_SYMBOL = "BNB/USDT"
_RAW = "BNBUSDT"


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# 纯函数
# --------------------------------------------------------------------------- #


class TestToSessionSymbol(SimpleTestCase):
    def test_known_quotes_get_a_slash(self):
        assert to_session_symbol("BNBUSDT") == "BNB/USDT"
        assert to_session_symbol("DOGEUSDT") == "DOGE/USDT"
        assert to_session_symbol("BTCUSDC") == "BTC/USDC"
        assert to_session_symbol("ethbtc") == "ETH/BTC"

    def test_longer_quote_wins(self):
        """``USDT`` 必须先于更短的后缀被尝试，否则 ``BTCUSDT`` 会被拆错。"""
        assert to_session_symbol("BTCUSDT") == "BTC/USDT"

    def test_already_slashed_is_returned_as_is(self):
        assert to_session_symbol("BNB/USDT") == "BNB/USDT"

    def test_bare_base_and_unknown_quote_are_returned_verbatim(self):
        """认不出 quote 就原样返回——猜错品种名的代价是落进错的成本桶。"""
        assert to_session_symbol("BTC") == "BTC"
        assert to_session_symbol("FOOBAR") == "FOOBAR"

    def test_empty_is_empty(self):
        assert to_session_symbol("") == ""
        assert to_session_symbol(None) == ""


class TestGroupTradesByOrder(SimpleTestCase):
    """一个 orderId 可能多笔 fill（10-01 那张 4 笔），必须聚合。"""

    @staticmethod
    def _trade(oid, qty, price, pnl, ts, side="BUY", symbol=_RAW):
        return {
            "orderId": oid,
            "qty": str(qty),
            "price": str(price),
            "realizedPnl": str(pnl),
            "time": ts,
            "side": side,
            "symbol": symbol,
        }

    def test_single_fill(self):
        grouped = _group_trades_by_order(
            [self._trade("1", "0.68", "762.57", "5.9296", 1_756_000_000_000)]
        )

        entry = grouped["1"]
        assert entry["qty"] == Decimal("0.68")
        assert entry["price"] == Decimal("762.57")
        assert entry["realized_pnl"] == Decimal("5.9296")
        assert entry["side"] == "buy"
        assert entry["symbol"] == _RAW
        assert entry["time"] == datetime.fromtimestamp(1_756_000_000, tz=timezone.utc)

    def test_multiple_fills_aggregate_qty_weighted_price_and_sum_pnl(self):
        grouped = _group_trades_by_order(
            [
                self._trade("1", "1", "100", "1", 1_000_000_000_000),
                self._trade("1", "3", "200", "2", 1_000_000_010_000),
            ]
        )

        entry = grouped["1"]
        assert entry["qty"] == Decimal("4")
        assert entry["price"] == Decimal("175")  # (100*1 + 200*3) / 4
        assert entry["realized_pnl"] == Decimal("3")
        # min time = 第一笔（回补行要靠它排在后续卖单之前）
        assert entry["time"] == datetime.fromtimestamp(1_000_000_000, tz=timezone.utc)

    def test_two_orders_stay_separate(self):
        grouped = _group_trades_by_order(
            [
                self._trade("1", "1", "100", "1", 1_000_000_000_000),
                self._trade("2", "2", "50", "0.5", 1_000_000_000_000),
            ]
        )
        assert set(grouped) == {"1", "2"}

    def test_missing_order_id_is_skipped(self):
        assert _group_trades_by_order([{"qty": "1", "price": "1"}]) == {}

    def test_zero_qty_has_no_price(self):
        """全为 0 成交量时不落 0 价——0 的含义是「成交价是 0」，那是一句假话。"""
        grouped = _group_trades_by_order(
            [self._trade("1", "0", "0", "0", 1_000_000_000_000)]
        )
        assert grouped["1"]["price"] is None


# --------------------------------------------------------------------------- #
# DB 路径
# --------------------------------------------------------------------------- #
# 只给下面三个 DB 类打 mark（`transaction=True`：`db_async` 走 `sync_to_async` 那条线程
# 要看得见 fixture 的行）。纯函数类不打，跑起来是秒回。


class _FakeAdapter:
    """只实现本层用到的那两个口：``fetch_user_trades`` / ``disconnect``。"""

    def __init__(self, trades=None, error: Exception | None = None):
        self.trades = list(trades or [])
        self.error = error
        self.disconnect = AsyncMock()

    async def fetch_user_trades(self, symbol, *, from_id=0, limit=1000):
        if self.error is not None:
            raise self.error
        return list(self.trades)


def _make_account(label="t"):
    return ExchangeAccount.objects.create(
        exchange="binance",
        label=label,
        testnet=True,
        api_key_enc=b"k",
        api_secret_enc=b"s",
    )


def _make_user(email=None):
    """建一个用户。email 唯一，所以不传时按调用次数生成。"""
    global _USER_SEQ
    _USER_SEQ += 1
    return get_user_model().objects.create_user(
        email=email or f"ledger{_USER_SEQ}@test.com",
        username=f"ledger{_USER_SEQ}",
        password="x",
    )


_USER_SEQ = 0


def _make_session(
    user, account, *, symbol=_SYMBOL, status="running", equity="10000.00"
):
    strategy = Strategy.objects.create(name="s", code_path="strategies/s.py")
    return LiveSession.objects.create(
        user=user,
        strategy=strategy,
        symbol=symbol,
        mode="paper",
        status=status,
        exchange_account=account,
        initial_capital=Decimal(equity),
        current_equity=Decimal(equity),
    )


def _shard(side="sell", qty="1", cid="cid-1"):
    return reduce.ShardOrder(
        client_order_id=cid, symbol=_SYMBOL, side=side, quantity=Decimal(qty)
    )


def _result(
    *,
    status=reduce.SHARD_STATUS_FILLED,
    oid="123",
    filled="1",
    price="100",
    cid="cid-1",
    side="sell",
):
    return reduce.ShardResult(
        client_order_id=cid,
        symbol=_SYMBOL,
        side=side,
        requested_qty=Decimal("1"),
        filled_qty=Decimal(filled),
        avg_price=Decimal(price) if price is not None else None,
        status=status,
        sends=1,
        exchange_order_id=oid,
    )


def _trade(oid, *, qty="1", price="100", pnl="9.9", side="SELL", ts=1_700_000_000_000):
    return {
        "orderId": oid,
        "qty": qty,
        "price": price,
        "realizedPnl": pnl,
        "time": ts,
        "side": side,
        "symbol": _RAW,
    }


class TestResolveSessionFor:
    pytestmark = pytest.mark.django_db(transaction=True)

    def test_zero_active_sessions_is_none(self):
        account = _make_account()
        assert _run(resolve_session_for(account.id, _SYMBOL)) is None

    def test_exactly_one_active_session_is_returned(self):
        account = _make_account()
        session = _make_session(_make_user(), account)
        got = _run(resolve_session_for(account.id, _SYMBOL))
        assert got.id == session.id

    def test_two_active_sessions_is_none(self):
        """0 或 >1 一律归空（fail-closed），绝不硬挑一条。"""
        account = _make_account()
        _make_session(_make_user(), account)
        _make_session(_make_user(), account)
        assert _run(resolve_session_for(account.id, _SYMBOL)) is None

    def test_stopped_session_does_not_count(self):
        account = _make_account()
        _make_session(_make_user(), account, status="stopped")
        assert _run(resolve_session_for(account.id, _SYMBOL)) is None

    def test_other_symbol_does_not_count(self):
        account = _make_account()
        _make_session(_make_user(), account, symbol="DOGE/USDT")
        assert _run(resolve_session_for(account.id, _SYMBOL)) is None


class TestPersistReduceOrder:
    pytestmark = pytest.mark.django_db(transaction=True)

    def test_settled_sell_is_persisted_with_slashed_symbol(self):
        """会话单存 ``BNB/USDT``，减仓子单是 ``BNBUSDT``——落库必须转回斜杠口径。"""
        account = _make_account()
        _make_session(_make_user(), account)
        adapter = _FakeAdapter(trades=[_trade("123", pnl="9.9")])

        order = _run(
            persist_reduce_order(account, _shard(), _result(), adapter=adapter)
        )

        assert order is not None
        order.refresh_from_db()
        assert order.symbol == _SYMBOL
        assert order.side == "sell"
        assert order.order_type == "market"
        assert order.status == "filled"
        assert order.exchange_order_id == "123"
        assert order.client_order_id == "cid-1"
        assert order.triggered_strategy == TRIGGER_MANUAL_REDUCE
        assert order.filled_quantity == Decimal("1")
        # 无历史买入 → 本地算不出 → 回退交易所 rpnl
        assert order.realized_pnl == Decimal("9.9")

    def test_partial_status_maps_to_partial(self):
        account = _make_account()
        _make_session(_make_user(), account)
        order = _run(
            persist_reduce_order(
                account,
                _shard(qty="2"),
                _result(status=reduce.SHARD_STATUS_PARTIAL, filled="1"),
                adapter=_FakeAdapter(),
            )
        )
        order.refresh_from_db()
        assert order.status == "partial"

    def test_unfinished_is_not_persisted(self):
        """「未知 ≠ 失败」，也 ≠ 一行「成交 0」的假账。"""
        account = _make_account()
        order = _run(
            persist_reduce_order(
                account,
                _shard(),
                _result(status=reduce.SHARD_STATUS_UNFINISHED, filled="0"),
                adapter=_FakeAdapter(),
            )
        )
        assert order is None
        assert Order.objects.count() == 0

    def test_rejected_is_not_persisted(self):
        account = _make_account()
        order = _run(
            persist_reduce_order(
                account,
                _shard(),
                _result(status=reduce.SHARD_STATUS_REJECTED, filled="0"),
                adapter=_FakeAdapter(),
            )
        )
        assert order is None
        assert Order.objects.count() == 0

    def test_missing_exchange_order_id_is_not_persisted(self):
        """没有交易所单号就没有幂等锚点，也没有可回查的成交——不落行。"""
        account = _make_account()
        order = _run(
            persist_reduce_order(
                account, _shard(), _result(oid=""), adapter=_FakeAdapter()
            )
        )
        assert order is None

    def test_idempotent_on_exchange_order_id(self):
        account = _make_account()
        _make_session(_make_user(), account)
        first = _run(
            persist_reduce_order(account, _shard(), _result(), adapter=_FakeAdapter())
        )
        second = _run(
            persist_reduce_order(account, _shard(), _result(), adapter=_FakeAdapter())
        )
        assert first.id == second.id
        assert Order.objects.count() == 1

    def test_unique_active_session_is_attributed_and_equity_bumped(self):
        account = _make_account()
        session = _make_session(_make_user(), account, equity="10000.00")
        adapter = _FakeAdapter(trades=[_trade("123", pnl="12.03")])

        _run(persist_reduce_order(account, _shard(), _result(), adapter=adapter))

        session.refresh_from_db()
        assert session.current_equity == Decimal("10012.03")
        assert Order.objects.get().live_session_id == session.id

    def test_two_active_sessions_is_attributed_to_nobody(self):
        """同一账户同一品种两条活跃会话 → 归属空，权益不动。"""
        account = _make_account()
        _make_session(_make_user(), account, equity="10000.00")
        _make_session(_make_user(), account, equity="20000.00")
        adapter = _FakeAdapter(trades=[_trade("123", pnl="12.03")])

        _run(persist_reduce_order(account, _shard(), _result(), adapter=adapter))

        assert Order.objects.get().live_session_id is None
        assert sorted(s.current_equity for s in LiveSession.objects.all()) == [
            Decimal("10000.00"),
            Decimal("20000.00"),
        ]

    def test_sell_pnl_prefers_local_cost_basis(self):
        """卖单有历史多头成本时本地算，不用交易所 rpnl（即时且可靠）。"""
        account = _make_account()
        _make_session(_make_user(), account)
        Order.objects.create(
            exchange_account_id=account.id,
            symbol=_SYMBOL,
            side="buy",
            order_type="market",
            quantity=Decimal("1"),
            status="filled",
            filled_quantity=Decimal("1"),
            avg_fill_price=Decimal("90"),
        )
        adapter = _FakeAdapter(trades=[_trade("123", pnl="999")])

        order = _run(
            persist_reduce_order(
                account, _shard(), _result(filled="1", price="100"), adapter=adapter
            )
        )
        order.refresh_from_db()
        assert order.realized_pnl == Decimal("10")  # (100 - 90) * 1

    def test_buy_pnl_comes_from_exchange(self):
        """买单（回补空头）本地推不出来 → 用交易所 rpnl（本次采用的口径）。"""
        account = _make_account()
        _make_session(_make_user(), account)
        adapter = _FakeAdapter(
            trades=[_trade("123", qty="0.69", price="762.45", pnl="6.0996", side="BUY")]
        )

        order = _run(
            persist_reduce_order(
                account,
                _shard(side="buy"),
                _result(side="buy", filled="0.69", price="762.45"),
                adapter=adapter,
            )
        )
        order.refresh_from_db()
        assert order.realized_pnl == Decimal("6.0996")

    def test_exchange_lookup_failure_leaves_pnl_null(self):
        """取不到就落 NULL——交给回填命令兜底，绝不落成 0。"""
        account = _make_account()
        _make_session(_make_user(), account)
        order = _run(
            persist_reduce_order(
                account,
                _shard(side="buy"),
                _result(side="buy"),
                adapter=_FakeAdapter(error=RuntimeError("boom")),
            )
        )
        order.refresh_from_db()
        assert order.realized_pnl is None


# --------------------------------------------------------------------------- #
# 回填命令
# --------------------------------------------------------------------------- #


class TestReconcileCommand:
    """``reconcile_realized_pnl_from_exchange`` 的 DB 路径。"""

    pytestmark = pytest.mark.django_db(transaction=True)

    @staticmethod
    def _trades():
        # 一张本地已有行（09-30 卖单，realized_pnl 为 NULL）
        # + 一张本地没有的回补买（09-28，须新建且 created_at 回填到交易时刻）
        sell_ts = int(
            datetime(2026, 9, 30, 5, 0, tzinfo=timezone.utc).timestamp() * 1000
        )
        cover_ts = int(
            datetime(2026, 9, 28, 15, 43, tzinfo=timezone.utc).timestamp() * 1000
        )
        return [
            _trade(
                "sell-1", qty="1", price="700", pnl="-3.4959", side="SELL", ts=sell_ts
            ),
            _trade(
                "cover-1",
                qty="0.69",
                price="762.45",
                pnl="6.0996",
                side="BUY",
                ts=cover_ts,
            ),
        ]

    @staticmethod
    def _seed_local_sell(account, session=None):
        return Order.objects.create(
            exchange_account_id=account.id,
            symbol=_SYMBOL,
            side="sell",
            order_type="market",
            quantity=Decimal("1"),
            status="filled",
            exchange_order_id="sell-1",
            filled_quantity=Decimal("1"),
            avg_fill_price=Decimal("700"),
            realized_pnl=None,
            live_session_id=getattr(session, "id", None),
        )

    def _run(self, account, *args):
        with patch(
            "apps.trading.pending_reconcile.build_account_adapter",
            new=AsyncMock(return_value=_FakeAdapter(trades=self._trades())),
        ):
            call_command(
                "reconcile_realized_pnl_from_exchange",
                *args,
                "--account",
                str(account.id),
            )

    def test_dry_run_writes_nothing(self):
        account = _make_account()
        _make_session(_make_user(), account)
        sell = self._seed_local_sell(account)
        self._run(account)  # 无 --apply

        sell.refresh_from_db()
        assert sell.realized_pnl is None
        assert Order.objects.count() == 1

    def test_apply_overwrites_existing_and_creates_backfill_row(self):
        account = _make_account()
        session = _make_session(_make_user(), account, equity="10000.00")
        sell = self._seed_local_sell(account, session)

        self._run(account, "--apply")

        sell.refresh_from_db()
        assert sell.realized_pnl == Decimal("-3.4959")

        backfill = Order.objects.get(exchange_order_id="cover-1")
        assert backfill.symbol == _SYMBOL
        assert backfill.side == "buy"
        assert backfill.status == "filled"
        assert backfill.realized_pnl == Decimal("6.0996")
        assert backfill.live_session_id == session.id
        assert backfill.triggered_strategy == "exchange_backfill"
        # **命门**：created_at 回填到交易时刻，必须排在 09-30 卖单之前。
        assert backfill.created_at.date().isoformat() == "2026-09-28"
        assert backfill.created_at < sell.created_at

        session.refresh_from_db()
        # delta = (-3.4959 - 0) + 6.0996 = 2.6037
        assert session.current_equity == Decimal("10002.60")

    def test_second_apply_is_idempotent(self):
        account = _make_account()
        _make_session(_make_user(), account)
        self._seed_local_sell(account)

        self._run(account, "--apply")
        counts = (
            Order.objects.count(),
            Order.objects.filter(realized_pnl__isnull=True).count(),
        )
        self._run(account, "--apply")

        assert (
            Order.objects.count(),
            Order.objects.filter(realized_pnl__isnull=True).count(),
        ) == counts
