"""会话级持仓隔离（修复 2）：ctx.position 是「本会话台账」，不再是账户持仓。

背景（2026-10-07）：box 与 donchian 绑同一 binance testnet 账户、同跑 DOGE/USDT。
runner 每根 bar 把**账户级**持仓同步进 ctx，close_position 平到账户级 0——box 平掉
了 donchian 开的 10401 DOGE/USDT（实现亏损 -79.78）。

修复 1 挡住了同账户同品种第二支会话的准入（见 test_live_session_conflict.py）；
本文件钉住 runner 侧对「已有两支会话并存 / 仓位归属不明」场景的行为：

  1. 首次成功同步 = 采用：同账户+同品种另有活跃会话 → 采用 0 + 通知（账户仓位
     无法归属，可能是别人的仓）；无其他会话 → 采用账户真值（传统行为）；
     同步失败不算采用，首次**成功**才定归属；
  2. 后续同步 = 对账：账户有持仓而本会话台账 0 → 外部持仓，保持 0（绝不平别的
     仓），在途窗口外通知一次；账户仓位 < 台账 → 台账下钳；
  3. 卖单按账户真值钳制：不得卖出超出账户持有的量（reduceOnly 超出部分会变
     反向开仓，2026-09-20 -17718 空头事故）。

桩与 test_position_sync_freshness 同一套；模型工厂与 test_live_session_conflict 同形
（真实 UUID 的 LiveSession，冲突检查走真库）。

**冲突检查的打桩边界**：runner 里的冲突检查经 `db_async`（线程池，thread_sensitive=False）
执行，而 Django TestCase 的行是**未提交**的——线程池的独立连接看不到本事务的行
（生产环境数据已提交，无此问题）。因此「同账户+同品种有另一活跃会话 → 采用 0」
的 3 个用例打桩 `LiveSession.conflicting_active_session`（classmethod 的真库语义由
test_live_session_conflict 覆盖，本文件的 test_conflict_classmethod_accepts_pk_and_instance
补 pk/实例双入口）；其余用例不依赖可见性——「无冲突」在两种可见性语义下结果一致。
"""

from __future__ import annotations

import asyncio
import datetime
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.backtest.models import BacktestResult
from apps.exchange.models import ExchangeAccount
from apps.strategy_engine.live_mode import LiveStrategyRunner
from apps.trading.executor import OrderExecutor
from apps.trading.models import LiveSession, Strategy

User = get_user_model()

KLINE = {
    "open": "0.0900",
    "high": "0.0920",
    "low": "0.0890",
    "close": "0.0915",
    "volume": "1",
    "timestamp": 1789000000000,
}

# ─────────────────────────── 最小替身（与 freshness 测试同款） ───────────────────────────


@dataclass
class _Signal:
    side: str
    quantity: Decimal
    signal_name: str
    is_close_position: bool = False


@dataclass
class _Target:
    symbol: str = "DOGE/USDT"
    target_quantity: Decimal = Decimal("0")
    reason: str = "channel_exit"


class _Ctx:
    """StrategyContext 替身（含 position/balance 的 setter，与真实实现一致）。"""

    def __init__(self, position="0", balance="10000"):
        self._position = Decimal(str(position))
        self._positions = {"DOGE/USDT": self._position}
        self._balance = Decimal(str(balance))
        self.price = Decimal("0")

    @property
    def position(self) -> Decimal:
        return self._position

    @position.setter
    def position(self, value) -> None:
        self._position = Decimal(str(value))

    def set_position(self, symbol, quantity) -> None:
        self._position = Decimal(str(quantity))

    @property
    def balance(self) -> Decimal:
        return self._balance

    @balance.setter
    def balance(self, value) -> None:
        self._balance = Decimal(str(value))

    def set_price(self, symbol, price) -> None:
        self.price = Decimal(str(price))

    def buy(self, quantity, **kw):
        return _Signal(
            "buy",
            Decimal(str(quantity)),
            kw.get("signal_name", "buy"),
            kw.get("is_close_position", False),
        )

    def sell(self, quantity, **kw):
        return _Signal(
            "sell",
            Decimal(str(quantity)),
            kw.get("signal_name", "sell"),
            kw.get("is_close_position", False),
        )

    def close_position(self, **kw):
        if self._position <= 0:
            return None
        return _Signal(
            "sell",
            self._position,
            kw.get("signal_name", "close"),
            True,
        )


class _FakeStrategy:
    """on_bar 语义：ctx.position > 0 时产出"平仓"目标（与 donchian/box 同形）。"""

    name = "fake_isolation"
    min_kline_length = 5

    def __init__(self, ctx):
        self.ctx = ctx
        self.bar_calls = 0

    def on_start(self) -> None:
        pass

    def select_universe(self):
        return ["DOGE/USDT"]

    def generate_insights(self, kline, history):
        self.bar_calls += 1
        return (
            [SimpleNamespace(direction="sell", symbol="DOGE/USDT", confidence=1.0)]
            if self.ctx.position > 0
            else []
        )

    def construct_portfolio(self, insights, ctx):
        return [_Target()] if insights else []

    def apply_risk_filters(self, targets, ctx):
        return targets


class _FakeAdapter:
    def __init__(self, positions=None, error=None):
        self._positions = positions or []
        self._error = error
        self.calls = 0

    async def get_positions(self):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return list(self._positions)


class _FakeExecutor:
    """复用真实的按账户 id 解析逻辑（resolve_adapter）。"""

    resolve_adapter = OrderExecutor.resolve_adapter

    def __init__(self, adapter):
        self._account_adapters = {"acct-1": adapter}


def _make_runner(position: str, adapter: _FakeAdapter, *, account=None, session=None):
    """account/session 为 None 时用假 id（"acct-1"/"sess-1"）→ 冲突检查直接跳过。"""
    ctx = _Ctx(position=position)
    strategy = _FakeStrategy(ctx)
    runner = LiveStrategyRunner(
        strategy,
        "DOGE/USDT",
        "1h",
        str(account.id) if account else "acct-1",
        user_id="user-1",
        live_session_id=str(session.id) if session else "sess-1",
    )
    runner._test_dispatched = []  # type: ignore[attr-defined]

    async def _record(signal):
        runner._test_dispatched.append(signal)
        return True

    runner._dispatch_signal = _record  # type: ignore[assignment]

    # 绑定到 runner 的打桩入口：按账户 id 解析适配器（真实解析逻辑）需要
    # 本会话账户的 pk 在地图里，真账户的 UUID 由这里注册。
    @contextmanager
    def patch_exchange(fake: _FakeAdapter):
        ex = _FakeExecutor(fake)
        if account is not None:
            ex._account_adapters[str(account.id)] = fake
        with patch(
            "apps.trading.executor.OrderExecutor.get_instance",
            return_value=ex,
        ):
            yield

    runner.patch_exchange = patch_exchange  # type: ignore[attr-defined]
    return runner


def _long(qty: str):
    return [SimpleNamespace(symbol="DOGEUSDT", side="long", quantity=Decimal(qty))]


def _conflict_reason(account, other) -> str:
    """与真实 classmethod 返回值同形的拒绝原因（见 test_live_session_conflict）。"""
    return (
        f"交易所账户「{account.label}」已有 DOGE/USDT 的活跃会话："
        f"{other.strategy.name}（id {str(other.id)[:8]}…，status={other.status}）。"
        f"runner 共享该品种的账户级持仓，同账户同品种多策略会互相平仓，请先停止另一会话"
    )


# ─────────────────────────── 模型工厂（与 test_live_session_conflict 同形） ───────────────────────────


def _account(user, label: str = "shared-testnet"):
    return ExchangeAccount.objects.create(
        user=user,
        exchange="binance",
        label=label,
        api_key_enc=b"enc",
        api_secret_enc=b"enc",
        testnet=True,
    )


def _session(user, *, symbol, status, account, strategy_name):
    strategy = Strategy.objects.create(name=strategy_name, code_path="strategies/s.py")
    backtest = BacktestResult.objects.create(
        strategy=strategy,
        user=user,
        symbol=symbol,
        timeframe="4h",
        start_date=datetime.date(2026, 1, 1),
        end_date=datetime.date(2026, 2, 1),
        initial_capital=Decimal("1000"),
        final_capital=Decimal("1100"),
        total_return_pct=10.0,
        review_status="approved",
    )
    return LiveSession.objects.create(
        user=user,
        backtest_result=backtest,
        strategy=strategy,
        symbol=symbol,
        mode="paper",
        status=status,
        exchange_account=account,
        initial_capital=Decimal("1000"),
        current_equity=Decimal("1000"),
        config={},
    )


# ─────────────────────────── 用例 ───────────────────────────


class TestAdoption(TestCase):
    """首次成功同步 = 采用：定仓位归属。"""

    def setUp(self):
        self.user = User.objects.create_user(
            username="iso-adopt", email="iso@example.com", password="pw12345"
        )
        patcher = patch("apps.notify.models.Notification", MagicMock())
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_shared_account_adopts_zero(self):
        """同账户+同品种另有 running 会话 → 账户仓位无法归属 → 采用 0。"""
        account = _account(self.user)
        other = _session(
            self.user,
            symbol="DOGE/USDT",
            status="running",
            account=account,
            strategy_name="donchian_atr_trend_strategy",
        )
        me = _session(
            self.user,
            symbol="DOGE/USDT",
            status="running",
            account=account,
            strategy_name="box_time_range_breakout_strategy",
        )
        adapter = _FakeAdapter(positions=_long("1000"))
        runner = _make_runner("0", adapter, account=account, session=me)
        with (
            runner.patch_exchange(adapter),
            patch.object(
                LiveSession,
                "conflicting_active_session",
                staticmethod(lambda *a, **k: _conflict_reason(account, other)),
            ),
        ):
            asyncio.run(runner._sync_position_from_exchange())

        self.assertEqual(
            runner.strategy.ctx.position,
            Decimal("0"),
            "共享账户的仓位不得采进本会话台账",
        )
        self.assertEqual(
            runner._last_actual_position,
            Decimal("1000"),
            "账户真值仍要记录（卖出侧钳制依据）",
        )

    def test_solo_account_adopts_actual(self):
        """账户上只有本会话 → 采用账户真值（传统行为，重启可接管仓位）。

        冲突检查走 db_async 线程池，看不到 TestCase 未提交行（见文件头）；
        「无冲突」在两种可见性语义下结果一致（看到自己会被 exclude_pk 排除），
        所以本用例不打桩。
        """
        account = _account(self.user)
        me = _session(
            self.user,
            symbol="DOGE/USDT",
            status="running",
            account=account,
            strategy_name="box_time_range_breakout_strategy",
        )
        adapter = _FakeAdapter(positions=_long("1000"))
        runner = _make_runner("0", adapter, account=account, session=me)
        with runner.patch_exchange(adapter):
            asyncio.run(runner._sync_position_from_exchange())

        self.assertEqual(runner.strategy.ctx.position, Decimal("1000"))

    def test_restart_after_other_stopped_adopts_actual(self):
        """另一会话已停止（不再占持仓）→ 本会话重启后正常接管账户仓位。

        这是存量 DOGE 对的处置路径：停掉一支，重启另一支即恢复管理。
        """
        account = _account(self.user)
        _session(
            self.user,
            symbol="DOGE/USDT",
            status="stopped",
            account=account,
            strategy_name="donchian_atr_trend_strategy",
        )
        me = _session(
            self.user,
            symbol="DOGE/USDT",
            status="running",
            account=account,
            strategy_name="box_time_range_breakout_strategy",
        )
        adapter = _FakeAdapter(positions=_long("10401"))
        runner = _make_runner("0", adapter, account=account, session=me)
        with runner.patch_exchange(adapter):
            asyncio.run(runner._sync_position_from_exchange())

        self.assertEqual(runner.strategy.ctx.position, Decimal("10401"))

    def test_conflict_classmethod_accepts_pk_and_instance(self):
        """classmethod 双入口：runner 传 pk（账户 id 字符串）、视图传实例；
        账户行不存在（随机 pk）→ None。

        主线程同步调用能看到 TestCase 事务的行（runner 的异步路径走线程池，
        见文件头的打桩边界说明）。
        """
        account = _account(self.user)
        _session(
            self.user,
            symbol="DOGE/USDT",
            status="running",
            account=account,
            strategy_name="donchian_atr_trend_strategy",
        )
        me = _session(
            self.user,
            symbol="DOGE/USDT",
            status="running",
            account=account,
            strategy_name="box_time_range_breakout_strategy",
        )
        for acct_arg in (str(account.id), account):
            with self.subTest(entry=type(acct_arg).__name__):
                r = LiveSession.conflicting_active_session(
                    acct_arg, "DOGE/USDT", exclude_pk=me.pk
                )
                self.assertIsNotNone(r)
                self.assertIn("donchian_atr_trend_strategy", r)
                self.assertIn(account.label, r)
        self.assertIsNone(
            LiveSession.conflicting_active_session(uuid.uuid4(), "DOGE/USDT"),
            "账户行不存在 → 无冲突可判（退回传统采用）",
        )

    def test_failed_first_sync_defers_adoption(self):
        """同步失败不算采用：首次**成功**才定归属（失败时 ctx 不得被篡改）。"""
        account = _account(self.user)
        me = _session(
            self.user,
            symbol="DOGE/USDT",
            status="running",
            account=account,
            strategy_name="box_time_range_breakout_strategy",
        )
        adapter = _FakeAdapter(error=RuntimeError("positions API down"))
        runner = _make_runner("0", adapter, account=account, session=me)
        with runner.patch_exchange(adapter):
            self.assertFalse(asyncio.run(runner._sync_position_from_exchange()))
            self.assertFalse(runner._position_adopted, "失败不得置位已采用")

            adapter._error = None
            adapter._positions = _long("700")
            self.assertTrue(asyncio.run(runner._sync_position_from_exchange()))

        self.assertEqual(runner.strategy.ctx.position, Decimal("700"))

    def test_shared_account_notifies_once_with_reason(self):
        """采用 0 必须可见：一条通知，说清冲突会话是谁。"""
        account = _account(self.user)
        other = _session(
            self.user,
            symbol="DOGE/USDT",
            status="running",
            account=account,
            strategy_name="donchian_atr_trend_strategy",
        )
        me = _session(
            self.user,
            symbol="DOGE/USDT",
            status="running",
            account=account,
            strategy_name="box_time_range_breakout_strategy",
        )
        adapter = _FakeAdapter(positions=_long("1000"))
        notify_cls = MagicMock()
        runner = _make_runner("0", adapter, account=account, session=me)
        with (
            runner.patch_exchange(adapter),
            patch("apps.notify.models.Notification", notify_cls),
            patch.object(
                LiveSession,
                "conflicting_active_session",
                staticmethod(lambda *a, **k: _conflict_reason(account, other)),
            ),
        ):
            asyncio.run(runner._sync_position_from_exchange())

        calls = notify_cls.objects.create.call_args_list
        self.assertEqual(len(calls), 1, "共享账户冲突只通知一次")
        msg = calls[0].kwargs.get("message", "")
        self.assertIn("共享账户", msg)
        self.assertIn("donchian_atr_trend_strategy", msg, "必须点名冲突会话的策略")
        self.assertEqual(calls[0].kwargs.get("channel"), "web")

    def test_non_uuid_session_id_skips_db_check(self):
        """非 UUID 的 live_session_id（测试/独立 runner）不得触库，直接走独立采用。"""
        adapter = _FakeAdapter(positions=_long("1000"))
        runner = _make_runner("0", adapter)  # "acct-1" / "sess-1"，库里无行
        with (
            runner.patch_exchange(adapter),
            patch(
                "apps.core.db_utils.db_async",
                side_effect=AssertionError("非 UUID 会话 id 不得查询数据库"),
            ),
        ):
            asyncio.run(runner._sync_position_from_exchange())

        self.assertEqual(runner.strategy.ctx.position, Decimal("1000"))


class TestReconcile(TestCase):
    """后续同步 = 对账：不采别人的仓，可下钳台账。"""

    def setUp(self):
        self.user = User.objects.create_user(
            username="iso-reconcile", email="iso@example.com", password="pw12345"
        )
        account = _account(self.user)
        self.me = _session(
            self.user,
            symbol="DOGE/USDT",
            status="running",
            account=account,
            strategy_name="box_time_range_breakout_strategy",
        )
        self.account = account
        patcher = patch("apps.notify.models.Notification", MagicMock())
        patcher.start()
        self.addCleanup(patcher.stop)

    def _runner(self, adapter):
        return _make_runner("0", adapter, account=self.account, session=self.me)

    def test_foreign_position_kept_zero_and_notified(self):
        """账户凭空出现本会话没开的仓位（手工/另一会话）→ 保持 0 + 通知一次。"""
        adapter = _FakeAdapter(positions=[])
        runner = self._runner(adapter)
        with runner.patch_exchange(adapter):
            asyncio.run(runner._sync_position_from_exchange())  # 采用 0
            # 账户出现 500，且远超在途窗口
            adapter._positions = _long("500")
            runner._last_order_dispatch_ts = time.monotonic() - 999
            notify_cls = MagicMock()
            with patch("apps.notify.models.Notification", notify_cls):
                asyncio.run(runner._sync_position_from_exchange())

        self.assertEqual(
            runner.strategy.ctx.position,
            Decimal("0"),
            "外部持仓不得采进本会话台账（采进来 close_position 就会平掉它）",
        )
        calls = notify_cls.objects.create.call_args_list
        self.assertEqual(len(calls), 1)
        self.assertIn("非本会话持仓", calls[0].kwargs.get("message", ""))

    def test_foreign_position_notified_once(self):
        """外部持仓持续存在 → 只通知一次，不按 bar 刷屏。"""
        adapter = _FakeAdapter(positions=[])
        runner = self._runner(adapter)
        notify_cls = MagicMock()
        with (
            runner.patch_exchange(adapter),
            patch("apps.notify.models.Notification", notify_cls),
        ):
            asyncio.run(runner._sync_position_from_exchange())  # 采用 0
            adapter._positions = _long("500")
            runner._last_order_dispatch_ts = time.monotonic() - 999
            asyncio.run(runner._sync_position_from_exchange())
            asyncio.run(runner._sync_position_from_exchange())

        calls = notify_cls.objects.create.call_args_list
        self.assertEqual(len(calls), 1, f"外部持仓通知应只发一次，实际 {len(calls)} 次")

    def test_foreign_position_silent_within_inflight_window(self):
        """本会话平仓单在途（交易所态未反映）→「账户有仓+台账 0」是正常态，
        保持 0 且**不**发外部持仓通知（否则误报）。"""
        adapter = _FakeAdapter(positions=[])
        runner = self._runner(adapter)
        with runner.patch_exchange(adapter):
            asyncio.run(runner._sync_position_from_exchange())  # 采用 0
            # 刚下发过平仓单：在途窗口内
            runner._last_order_dispatch_ts = time.monotonic()
            adapter._positions = _long("100")  # 交易所还没反映成交
            notify_cls = MagicMock()
            with patch("apps.notify.models.Notification", notify_cls):
                asyncio.run(runner._sync_position_from_exchange())

        self.assertEqual(runner.strategy.ctx.position, Decimal("0"))
        self.assertEqual(
            len(notify_cls.objects.create.call_args_list), 0, "在途窗口内不得误报"
        )

    def test_ledger_clamped_down_when_account_shrinks(self):
        """账户仓位被外部减少（另一会话部分平掉）→ 台账下钳到账户真值。"""
        adapter = _FakeAdapter(positions=_long("1000"))
        runner = self._runner(adapter)
        with runner.patch_exchange(adapter):
            asyncio.run(runner._sync_position_from_exchange())  # 采用 1000
            adapter._positions = _long("400")
            asyncio.run(runner._sync_position_from_exchange())

        self.assertEqual(
            runner.strategy.ctx.position,
            Decimal("400"),
            "台账不得高于账户真值（否则卖出超出 → 反向开仓）",
        )
        # 下钳后卖出侧同样被钳制
        sig = runner._target_to_order(_Target(target_quantity=Decimal("0")))
        self.assertEqual(sig.quantity, Decimal("400"))

    def test_ledger_not_inflated_by_account_growth(self):
        """账户仓位变多（别人加仓）→ 台账**不上调**：别人的仓不是本会话的。"""
        adapter = _FakeAdapter(positions=_long("100"))
        runner = self._runner(adapter)
        with runner.patch_exchange(adapter):
            asyncio.run(runner._sync_position_from_exchange())  # 采用 100
            adapter._positions = _long("900")
            asyncio.run(runner._sync_position_from_exchange())

        self.assertEqual(runner.strategy.ctx.position, Decimal("100"))


class TestSellClamp(TestCase):
    """卖单按账户真值钳制：不得卖出超出账户持有的量。"""

    def _bare(self, position: str) -> LiveStrategyRunner:
        ctx = _Ctx(position=position)
        strategy = _FakeStrategy(ctx)
        return LiveStrategyRunner(
            strategy,
            "DOGE/USDT",
            "1h",
            "acct-1",
            user_id="user-1",
            live_session_id="sess-1",
        )

    def test_sell_clamped_by_account_actual(self):
        runner = self._bare("1000")
        runner._last_actual_position = Decimal("300")
        sig = runner._target_to_order(_Target(target_quantity=Decimal("0")))
        self.assertIsNotNone(sig)
        self.assertEqual(sig.quantity, Decimal("300"), "不得卖出超出账户持有的量")
        self.assertTrue(
            sig.is_close_position,
            "卖到空仓仍是平仓单（reduceOnly 兜底不变）",
        )

    def test_partial_reduce_also_clamped(self):
        runner = self._bare("1000")
        runner._last_actual_position = Decimal("300")
        sig = runner._target_to_order(_Target(target_quantity=Decimal("200")))
        self.assertIsNotNone(sig)
        self.assertEqual(sig.quantity, Decimal("300"))
        self.assertFalse(sig.is_close_position)

    def test_sell_skipped_when_account_flat(self):
        """账户已 0（被平掉了）→ 不发明知是 0 的空头卖单。"""
        runner = self._bare("1000")
        runner._last_actual_position = Decimal("0")
        self.assertIsNone(
            runner._target_to_order(_Target(target_quantity=Decimal("0")))
        )

    def test_sell_not_clamped_without_actual(self):
        """账户真值未知（从未同步成功）→ 维持传统行为，不猜。"""
        runner = self._bare("1000")
        self.assertIsNone(runner._last_actual_position)
        sig = runner._target_to_order(_Target(target_quantity=Decimal("0")))
        self.assertEqual(sig.quantity, Decimal("1000"))


class TestIncidentEndToEnd(TestCase):
    """2026-10-07 事故形态端到端：同账户同品种两支 running 会话并存时，
    后启动的一支不得把先启动那支开的仓平掉。"""

    def setUp(self):
        self.user = User.objects.create_user(
            username="iso-incident", email="iso@example.com", password="pw12345"
        )
        patcher = patch("apps.notify.models.Notification", MagicMock())
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_late_session_does_not_close_earlier_sessions_position(self):
        account = _account(self.user)
        other = _session(
            self.user,
            symbol="DOGE/USDT",
            status="running",
            account=account,
            strategy_name="donchian_atr_trend_strategy",
        )
        me = _session(
            self.user,
            symbol="DOGE/USDT",
            status="running",
            account=account,
            strategy_name="box_time_range_breakout_strategy",
        )
        adapter = _FakeAdapter(positions=_long("10401"))  # donchian 开的仓
        runner = _make_runner("0", adapter, account=account, session=me)
        with (
            runner.patch_exchange(adapter),
            patch.object(
                LiveSession,
                "conflicting_active_session",
                staticmethod(lambda *a, **k: _conflict_reason(account, other)),
            ),
        ):
            asyncio.run(runner._process_kline(dict(KLINE)))

        self.assertEqual(
            runner._test_dispatched,
            [],
            "不得对别会话的仓位发平仓单（事故中 box 平掉 donchian 的 10401）",
        )
        self.assertEqual(runner.strategy.ctx.position, Decimal("0"))
        self.assertEqual(
            runner.strategy.bar_calls,
            1,
            "决策路径本身照常运行（是「无仓可平」，不是「被跳过」）",
        )
