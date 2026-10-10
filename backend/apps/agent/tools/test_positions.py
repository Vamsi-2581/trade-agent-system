"""仓位操作工具：``apps/agent/tools/positions.py``。

这个文件钉的是**安全门的接线**，不是下单本身（下单的下场由
``apps/trading/tests/test_reduce_mechanics.py`` 用一个照脚本回答的假适配器整层测完）。
所以要防的是「错了也不会报错、只会静默变成另一个意思」：

* **干跑被读成「已经下了单」**——2026-09-28 事故的同款形态：用户先收到一条「已提交平仓
  指令」，一分钟后才收到失败。这里的第一条契约就是 ``confirmed=false`` 时
  ``place_reduce_shard`` **一次都没被调用**。
* **多账户时替用户挑一个**——减错账户不可逆。未指定且有多个候选时必须报错列候选。
* **别人的账户被减**——跨用户护栏。
* **``unfinished`` 被说成「确定没成交」**——``reduce.py`` 模块 docstring 的核心纪律。
* **「持仓取不到」被说成「空仓」**——``list_positions`` 的两分。

## 为什么要造适配器工厂、而不是 patch 工具的 import

``execute`` 里的 ``from apps.trading.pending_reconcile import build_account_adapter`` 是
**函数内**导入（这条链路上 ``apps.trading`` 会间接 import ``apps.agent``，模块级导入会成环）。
函数内导入的绑定发生在每次调用时，所以打在 ``positions`` 模块属性上的 patch 对它无效——
打补丁的正确位置是 ``apps.trading.pending_reconcile.build_account_adapter`` 本身。
"""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from unittest.mock import AsyncMock, patch

from apps.agent.tools.positions import (
    ListPositionsTool,
    ReducePositionTool,
    _confirmed,
    _match_symbol,
    _parse_fraction,
    _resolve_accounts,
)
from apps.exchange.models import ExchangeAccount
from apps.trading.adapters.base import Position
from apps.trading.reduce import ShardResult
from cryptography.fernet import Fernet

#: 账户密钥的 Fernet 口令。模块级生成一次：加解密都走 ``settings.FERNET_KEY``，
#: 与 ``apps/regime/tests/test_reduce_run.py`` 同一手法。
FERNET_KEY = Fernet.generate_key().decode()

ADAPTER_FACTORY = "apps.trading.pending_reconcile.build_account_adapter"


def _run(coro):
    return asyncio.run(coro)


def _account(
    *,
    label: str = "主账户",
    exchange: str = "binance",
    user=None,
    active: bool = True,
    testnet: bool = False,
) -> ExchangeAccount:
    fernet = Fernet(FERNET_KEY.encode())
    return ExchangeAccount.objects.create(
        exchange=exchange,
        label=label,
        api_key_enc=fernet.encrypt(b"k" * 32),
        api_secret_enc=fernet.encrypt(b"s" * 32),
        is_active=active,
        testnet=testnet,
        user=user,
    )


def _position(
    symbol: str = "BNBUSDT",
    side: str = "long",
    qty: str = "10",
    entry: str = "600",
    mark: str = "610",
    pnl: str = "100",
) -> Position:
    return Position(
        symbol=symbol,
        side=side,
        quantity=Decimal(qty),
        entry_price=Decimal(entry),
        unrealized_pnl=Decimal(pnl),
        leverage=5,
        mark_price=Decimal(mark),
    )


def _shard_result(
    *,
    status: str = "filled",
    requested: str = "10",
    filled: str = "10",
    avg: str | None = "612",
    oid: str = "888",
    error: str = "",
) -> ShardResult:
    return ShardResult(
        client_order_id="cid",
        symbol="BNBUSDT",
        side="sell",
        requested_qty=Decimal(requested),
        filled_qty=Decimal(filled),
        avg_price=Decimal(avg) if avg is not None else None,
        status=status,
        sends=1,
        exchange_order_id=oid,
        error=error,
    )


class _FakeAdapter:
    """只回答这两个工具真的会问的四个问题的假适配器。"""

    def __init__(self, *, positions=None, mid="611.5", positions_error=None):
        self._positions = list(positions or [])
        self._mid = Decimal(mid) if mid is not None else None
        self._positions_error = positions_error
        self.disconnected = False
        self.mid_queries: list[str] = []
        self.position_calls = 0

    async def get_positions(self):
        self.position_calls += 1
        if self._positions_error is not None:
            raise self._positions_error
        return list(self._positions)

    async def fetch_mid_price(self, symbol):
        self.mid_queries.append(symbol)
        return self._mid

    async def disconnect(self):
        self.disconnected = True


def _adapter_factory(adapter):
    """``build_account_adapter`` 的替身：既可以是适配器，也可以是 ``None`` 或异常。"""

    async def _build(account):
        if isinstance(adapter, Exception):
            raise adapter
        return adapter

    return _build


def _patch_accounts(accounts: list):
    """把「活跃账户」这一问换成给定列表（``db_async`` 的线程语义不参与这些用例）。

    ``_load_active_accounts`` 被整个换掉，而调用点写的是 ``await db_async(fn)()`` 两步；
    所以 ``db_async`` 的替身要**返回一个工厂**（返回协程的函数），工厂再去调那个被换掉的
    同步函数。直接让它返回协程就成了 `await coroutine()` —— 一个 `not callable` 的错。
    """

    def _call(fn):
        async def _invoke(*args, **kwargs):
            return fn(*args, **kwargs)

        return _invoke

    return patch("apps.agent.tools.positions.db_async", side_effect=_call), patch(
        "apps.agent.tools.positions._load_active_accounts", return_value=accounts
    )


# --------------------------------------------------------------------------- #
# 纯函数：账户解析 / 品种匹配 / fraction
# --------------------------------------------------------------------------- #


class _FakeAccount:
    def __init__(self, label: str, hex_id: str = None):
        self.label = label
        self.id = uuid.UUID(hex_id or uuid.uuid4().hex)
        self.exchange = "binance"
        self.testnet = False
        self.user_id = None


class TestResolveAccounts:
    def test_empty_raw_returns_all(self):
        accounts = [_FakeAccount("a"), _FakeAccount("b")]
        got, err = _resolve_accounts(accounts, "")
        assert err == ""
        assert got == accounts

    def test_exact_label_wins_over_substring(self):
        first = _FakeAccount("主账户")
        second = _FakeAccount("主账户-备份")
        got, err = _resolve_accounts([first, second], "主账户")
        assert err == ""
        assert got == [first]

    def test_substring_fallback(self):
        only = _FakeAccount("binance_main")
        got, err = _resolve_accounts([only], "main")
        assert err == ""
        assert got == [only]

    def test_uuid_match(self):
        target = _FakeAccount("x", "0123456789abcdef0123456789abcdef")
        other = _FakeAccount("y")
        got, err = _resolve_accounts([target, other], str(target.id))
        assert err == ""
        assert got == [target]

    def test_unknown_label_lists_candidates(self):
        got, err = _resolve_accounts([_FakeAccount("binance_main")], "nope")
        assert got == []
        assert "binance_main" in err

    def test_uuid_shaped_but_absent_does_not_substring_collide(self):
        """形如 UUID 却找不到时不要退化成拿它当 label 关键词乱撞。"""
        got, err = _resolve_accounts([_FakeAccount("abc")], str(uuid.uuid4()))
        assert got == []
        assert "没有找到" in err


class TestMatchSymbol:
    def test_native_and_slash_both_hit(self):
        positions = {"BNBUSDT": ("long", Decimal("10"))}
        assert _match_symbol(positions, "BNB/USDT")[0] == "BNBUSDT"
        assert _match_symbol(positions, "bnb")[0] == "BNBUSDT"

    def test_bare_base_without_usdt_suffix(self):
        """用户常说裸 base；``BTCUSDT`` 已在 USDT 里，不该再补一次。"""
        positions = {"BTCUSDT": ("long", Decimal("1"))}
        assert _match_symbol(positions, "BTC")[0] == "BTCUSDT"

    def test_miss_returns_none(self):
        assert _match_symbol({"ETHUSDT": ("long", Decimal("1"))}, "BNB") is None


class TestParseFraction:
    def test_default_is_full_close(self):
        value, why = _parse_fraction(None)
        assert value == Decimal("1") and why == ""

    def test_string_number_is_accepted(self):
        """DSML 降级路径会把数字传成字符串。"""
        value, why = _parse_fraction("0.5")
        assert value == Decimal("0.5") and why == ""

    @pytest.mark.parametrize("bad", ["0", "-0.2", "1.5", "abc"])
    def test_out_of_range_or_unparsable_is_rejected(self, bad):
        value, why = _parse_fraction(bad)
        assert value is None
        assert why


class TestConfirmedCoercion:
    """``confirmed`` 必须 fail-closed——字符串 ``"false"`` 不许被读成「已确认」。

    降级 DSML 路径（``_parse_dsml_parameters`` 默认 ``string="true"``）会把布尔参数传成
    **字符串**，而 ``bool("false") is True``。若照单全收，LLM 说了「不下单」反而会下一张
    真实资金单。与 ``cb010a2`` 修的七处同根因，但这一处的代价是下单本身。
    """

    @pytest.mark.parametrize("raw", ["false", "False", "FALSE", "no", "0", "", "  ", "nope"])
    def test_negative_strings_do_not_confirm(self, raw):
        assert _confirmed(raw) is False

    @pytest.mark.parametrize("raw", ["true", "True", "TRUE", "yes", "1", "on"])
    def test_affirmative_strings_confirm(self, raw):
        assert _confirmed(raw) is True

    def test_native_bool_and_numbers(self):
        assert _confirmed(True) is True
        assert _confirmed(False) is False
        assert _confirmed(1) is True
        assert _confirmed(0) is False
        assert _confirmed(None) is False

    def test_unrecognized_type_does_not_confirm(self):
        """认不出的一律干跑：多跑一次确认是廉价的，误下单不可逆。"""
        assert _confirmed({"x": 1}) is False


# --------------------------------------------------------------------------- #
# list_positions
# --------------------------------------------------------------------------- #


@pytest.mark.django_db
class TestListPositions:
    def test_requires_user_id(self):
        result = _run(ListPositionsTool().execute())
        assert result.success is False
        assert "无法确定用户身份" in result.error

    def test_renders_account_and_position_fields(self):
        account = _account(label="主账户", testnet=True)
        adapter = _FakeAdapter(positions=[_position()])
        patches = _patch_accounts([account])
        with patches[0], patches[1], patch(ADAPTER_FACTORY, _adapter_factory(adapter)):
            result = _run(ListPositionsTool().execute(user_id="u1"))

        assert result.success is True
        text = result.data
        assert "主账户" in text
        assert "binance" in text
        assert "测试网" in text
        assert "BNBUSDT" in text
        assert "多" in text
        assert "10" in text  # 数量
        assert "610" in text  # 标记价
        assert "100" in text  # 未实现盈亏
        # 借了就还：适配器必须被断开
        assert adapter.disconnected is True

    def test_multiple_accounts_are_all_listed(self):
        first, second = _account(label="A"), _account(label="B")
        adapter = _FakeAdapter(positions=[_position()])
        patches = _patch_accounts([first, second])
        with patches[0], patches[1], patch(ADAPTER_FACTORY, _adapter_factory(adapter)):
            result = _run(ListPositionsTool().execute(user_id="u1"))

        assert "2 个活跃账户" in result.data
        assert "A" in result.data and "B" in result.data

    def test_no_adapter_is_not_an_empty_position(self):
        """「取不到」与「确实空仓」必须分开——后者才能据以行动。"""
        account = _account(label="无密钥")
        patches = _patch_accounts([account])
        with patches[0], patches[1], patch(ADAPTER_FACTORY, _adapter_factory(None)):
            result = _run(ListPositionsTool().execute(user_id="u1"))

        assert result.success is True
        assert "持仓取不到" in result.data
        assert "这不代表没有持仓" in result.data
        assert "1 个取不到持仓" in result.data
        assert "没有持仓" not in result.data.replace("这不代表没有持仓", "")

    def test_empty_position_says_so(self):
        account = _account(label="空仓账户")
        adapter = _FakeAdapter(positions=[])
        patches = _patch_accounts([account])
        with patches[0], patches[1], patch(ADAPTER_FACTORY, _adapter_factory(adapter)):
            result = _run(ListPositionsTool().execute(user_id="u1"))

        assert "空仓账户" in result.data
        assert "：没有持仓" in result.data
        assert "持仓取不到" not in result.data

    def test_position_fetch_failure_is_not_empty(self):
        account = _account(label="拉取失败")
        adapter = _FakeAdapter(positions_error=RuntimeError("boom"))
        patches = _patch_accounts([account])
        with patches[0], patches[1], patch(ADAPTER_FACTORY, _adapter_factory(adapter)):
            result = _run(ListPositionsTool().execute(user_id="u1"))

        assert "持仓取不到" in result.data
        assert "boom" in result.data
        assert adapter.disconnected is True

    def test_no_active_accounts(self):
        patches = _patch_accounts([])
        with patches[0], patches[1]:
            result = _run(ListPositionsTool().execute(user_id="u1"))
        assert result.success is True
        assert "没有已激活" in result.data

    def test_account_filter_miss_reports_candidates(self):
        account = _account(label="binance_main")
        patches = _patch_accounts([account])
        with patches[0], patches[1]:
            result = _run(ListPositionsTool().execute(account="nope", user_id="u1"))
        assert result.success is False
        assert "binance_main" in result.error


# --------------------------------------------------------------------------- #
# reduce_position
# --------------------------------------------------------------------------- #


@pytest.mark.django_db
class TestReducePositionGuards:
    def test_requires_user_id(self):
        result = _run(ReducePositionTool().execute(symbol="BNB", confirmed=True))
        assert result.success is False
        assert "无法确定用户身份" in result.error

    def test_requires_symbol(self):
        result = _run(ReducePositionTool().execute(user_id="u1"))
        assert result.success is False
        assert "symbol" in result.error

    def test_bad_fraction_is_rejected(self):
        result = _run(
            ReducePositionTool().execute(symbol="BNB", fraction=2, user_id="u1")
        )
        assert result.success is False
        assert "fraction" in result.error

    def test_multiple_accounts_without_account_arg_refuses_and_lists(self):
        """绝不默认挑一个——减错账户不可逆。"""
        first, second = _account(label="A"), _account(label="B")
        adapter = _FakeAdapter(positions=[_position()])
        patches = _patch_accounts([first, second])
        with patches[0], patches[1], patch(ADAPTER_FACTORY, _adapter_factory(adapter)), patch(
            "apps.trading.reduce.place_reduce_shard", AsyncMock()
        ) as place:
            result = _run(
                ReducePositionTool().execute(symbol="BNB", confirmed=True, user_id="u1")
            )

        assert result.success is False
        assert "必须指定其中一个" in result.error
        assert "A" in result.error and "B" in result.error
        place.assert_not_called()

    def test_other_users_account_is_refused(self):
        User = get_user_model()
        owner = User.objects.create_user(
            email="owner@example.com", username="owner", password="pw12345"
        )
        caller = User.objects.create_user(
            email="caller@example.com", username="caller", password="pw12345"
        )
        account = _account(label="别人的账户", user=owner)
        adapter = _FakeAdapter(positions=[_position()])
        patches = _patch_accounts([account])

        async def _resolve(raw):
            return str(caller.id)

        with patches[0], patches[1], patch(ADAPTER_FACTORY, _adapter_factory(adapter)), patch(
            "apps.agent.tools.positions._resolve_owner_id", _resolve
        ), patch("apps.trading.reduce.place_reduce_shard", AsyncMock()) as place:
            result = _run(
                ReducePositionTool().execute(
                    symbol="BNB", confirmed=True, user_id=str(caller.id)
                )
            )

        assert result.success is False
        assert "不属于当前用户" in result.error
        place.assert_not_called()

    def test_unowned_account_is_allowed(self):
        account = _account(label="未归属")
        adapter = _FakeAdapter(positions=[_position()])
        result_shard = _shard_result()
        patches = _patch_accounts([account])
        with patches[0], patches[1], patch(ADAPTER_FACTORY, _adapter_factory(adapter)), patch(
            "apps.trading.reduce.place_reduce_shard", AsyncMock(return_value=result_shard)
        ) as place, patch.object(ReducePositionTool, "_notify", AsyncMock()):
            result = _run(
                ReducePositionTool().execute(
                    symbol="BNB", confirmed=True, user_id="u1"
                )
            )

        assert result.success is True, result.error
        place.assert_called_once()


@pytest.mark.django_db
class TestReducePositionDryRun:
    def test_dry_run_does_not_place_any_order(self):
        """**本文件最重要的一条**：没确认时一次下单调用都不许发出。"""
        account = _account(label="主账户")
        adapter = _FakeAdapter(positions=[_position()])
        patches = _patch_accounts([account])
        with patches[0], patches[1], patch(ADAPTER_FACTORY, _adapter_factory(adapter)), patch(
            "apps.trading.reduce.place_reduce_shard", AsyncMock()
        ) as place:
            result = _run(ReducePositionTool().execute(symbol="BNB", user_id="u1"))

        assert result.success is True
        assert result.data["status"] == "DRY_RUN"
        assert "未下单" in result.data["message"]
        assert "confirmed=true" in result.data["message"]
        place.assert_not_called()
        # 干跑也要把「将执行什么」说全
        assert result.data["symbol"] == "BNBUSDT"
        assert result.data["order_side"] == "sell"
        assert result.data["planned_qty"] == "10.0"
        assert result.data["mid_price"] == "611.5"

    def test_dry_run_reports_half_for_half_fraction(self):
        account = _account(label="主账户")
        adapter = _FakeAdapter(positions=[_position(qty="10")])
        patches = _patch_accounts([account])
        with patches[0], patches[1], patch(ADAPTER_FACTORY, _adapter_factory(adapter)), patch(
            "apps.trading.reduce.place_reduce_shard", AsyncMock()
        ) as place:
            result = _run(
                ReducePositionTool().execute(symbol="BNB", fraction=0.5, user_id="u1")
            )

        assert result.data["planned_qty"] == "5.0"
        place.assert_not_called()

    def test_string_false_still_dry_runs(self):
        """DSML 降级路径把 confirmed 传成字符串 ``"false"`` 时**绝不能下单**。

        ``bool("false") is True``——照单全收就是把「不要下单」读成「已确认」，直接发一张
        真实资金单。这条钉的是端到端的那一步（``_confirmed`` 自身的表在
        ``TestConfirmedCoercion``）。
        """
        account = _account(label="主账户")
        adapter = _FakeAdapter(positions=[_position(qty="10")])
        patches = _patch_accounts([account])
        with patches[0], patches[1], patch(ADAPTER_FACTORY, _adapter_factory(adapter)), patch(
            "apps.trading.reduce.place_reduce_shard", AsyncMock()
        ) as place:
            result = _run(
                ReducePositionTool().execute(
                    symbol="BNB", confirmed="false", user_id="u1"
                )
            )

        assert result.success is True
        assert result.data["status"] == "DRY_RUN"
        place.assert_not_called()

    def test_string_true_places_the_order(self):
        """反过来也要通：字符串 ``"true"``（DSML 的肯定形态）必须真的下单。"""
        account = _account(label="主账户")
        adapter = _FakeAdapter(positions=[_position(qty="10")])
        patches = _patch_accounts([account])
        with patches[0], patches[1], patch(ADAPTER_FACTORY, _adapter_factory(adapter)), patch(
            "apps.trading.reduce.place_reduce_shard",
            AsyncMock(return_value=_shard_result()),
        ) as place, patch.object(ReducePositionTool, "_notify", AsyncMock()):
            result = _run(
                ReducePositionTool().execute(
                    symbol="BNB", confirmed="true", user_id="u1"
                )
            )

        assert result.success is True, result.error
        place.assert_called_once()


@pytest.mark.django_db
class TestReducePositionExecution:
    def _execute(self, *, positions, shard, symbol="BNB", fraction=1.0):
        account = _account(label="主账户")
        adapter = _FakeAdapter(positions=positions)
        patches = _patch_accounts([account])
        place = AsyncMock(return_value=shard)
        with patches[0], patches[1], patch(ADAPTER_FACTORY, _adapter_factory(adapter)), patch(
            "apps.trading.reduce.place_reduce_shard", place
        ), patch.object(ReducePositionTool, "_notify", AsyncMock()) as notify:
            result = _run(
                ReducePositionTool().execute(
                    symbol=symbol,
                    fraction=fraction,
                    confirmed=True,
                    user_id="u1",
                )
            )
        return result, place, adapter, notify

    def test_long_position_is_closed_with_sell(self):
        result, place, _, _ = self._execute(
            positions=[_position(side="long", qty="10")], shard=_shard_result()
        )
        assert result.success is True
        order = place.call_args[0][1]
        assert order.side == "sell"
        assert order.quantity == Decimal("10")
        # reduce_only 与确定的重发锚点由 place_reduce_shard 带着走，这里只能确认单号形态
        assert order.client_order_id.startswith("cl")
        assert len(order.client_order_id) <= 36

    def test_short_position_is_closed_with_buy(self):
        result, place, _, _ = self._execute(
            positions=[_position(side="short", qty="3")],
            shard=_shard_result(requested="3", filled="3"),
        )
        assert result.success is True
        assert place.call_args[0][1].side == "buy"

    def test_fraction_scales_the_order(self):
        result, place, _, _ = self._execute(
            positions=[_position(qty="10")],
            shard=_shard_result(requested="2.5", filled="2.5"),
            fraction=0.25,
        )
        assert result.success is True
        assert place.call_args[0][1].quantity == Decimal("2.5")

    def test_bare_base_matches_native_symbol(self):
        result, place, _, _ = self._execute(
            positions=[_position(symbol="BNBUSDT")], shard=_shard_result()
        )
        assert result.success is True
        assert place.call_args[0][1].symbol == "BNBUSDT"
        assert "BNBUSDT" in result.data

    def test_unknown_symbol_lists_actual_positions(self):
        result, place, _, _ = self._execute(
            positions=[_position(symbol="ETHUSDT")], shard=_shard_result(), symbol="BNB"
        )
        assert result.success is False
        assert "ETHUSDT" in result.error
        place.assert_not_called()

    def test_positions_unavailable_refuses_to_order(self):
        """不知道持有多少时绝不下单（``reduce_run`` 第 3 步的 fail-closed）。"""
        result, place, _, _ = self._execute(
            positions=[], shard=_shard_result()
        )
        # 空持仓 = 找不到匹配品种 → 拒（而不是下一张 0 数量的单）
        assert result.success is False
        place.assert_not_called()

    def test_unrecognized_side_refuses_to_order(self):
        result, place, _, _ = self._execute(
            positions=[_position(side="both")], shard=_shard_result()
        )
        assert result.success is False
        assert "方向无法识别" in result.error
        place.assert_not_called()

    def test_filled_result_reports_qty_after(self):
        positions = [_position(qty="10")]
        # 第一次读持仓（下单前）、第二次读余量（下单后）
        result, place, adapter, notify = self._execute(
            positions=positions, shard=_shard_result(requested="10", filled="10")
        )
        assert result.success is True
        assert "filled" in result.data
        assert "成交量：10" in result.data
        assert "余量：10" in result.data  # fake 适配器两次都返回同一份持仓
        assert "交易所单号：888" in result.data
        notify.assert_awaited_once()

    def test_unfinished_names_the_live_order_and_skips_qty_after(self):
        """``unfinished`` 必须单独点名，且**不读余量**（同 ``reduce_run``）。"""
        positions = [_position(qty="10")]
        result, place, adapter, notify = self._execute(
            positions=positions,
            shard=_shard_result(status="unfinished", filled="0", avg=None, oid=""),
        )
        assert result.success is True
        assert "没有拿到结论" in result.data
        assert "可能仍有一张活单" in result.data
        assert "余量：未读取" in result.data
        # 下单前读一次持仓；unfinished 时不再读第二次
        assert adapter.position_calls == 1

    def test_rejected_says_confirmed_no_fill(self):
        result, place, adapter, _ = self._execute(
            positions=[_position(qty="10")],
            shard=_shard_result(status="rejected", filled="0", avg=None, error="below minQty"),
        )
        assert result.success is True
        assert "rejected" in result.data
        assert "below minQty" in result.data
        # 有结论 ⇒ 读余量
        assert adapter.position_calls == 2

    def test_partial_says_partial_with_avg_price(self):
        result, place, _, _ = self._execute(
            positions=[_position(qty="10")],
            shard=_shard_result(status="partial", requested="10", filled="4", avg="609"),
        )
        assert result.success is True
        assert "partial" in result.data
        assert "成交均价：609" in result.data
