"""日内回撤检查的**取数与降级口径**，以及拒单必须让用户看得见。

CONTEXT.md 第 142 条记的是一条活的静默故障：``daily_snapshots`` 没有写入方，而
``_check_drawdown`` 拿它当期初净值——于是「当日已实现亏损」时必然走「无法获取期初
资金数据」分支拒单，且不产生任何告警（任何 paper 会话当天亏一次，之后所有开仓被
无声拦掉）。写入方补在 ``apps/trading/daily_snapshot.py``，本文件钉的是检查侧：

1. **分母只取当日快照**（``date=today``）：当日还没写出来（beat 未起 / 刚过日界）
   就降级放行，**而不是拒绝下单**。原实现取 ``date__lt=today``，结构上保证
   「第一次会话当天一定没有分母」，于是每一次开仓都被无声拦掉。不向前回退到昨天
   也是刻意的：昨天的权益配今天的盈亏是两个口径。
2. **当日确实没有快照**（新用户第一次会话，或当日首条还没写入）→ 降级放行 +
   WARNING。拒绝会让「新用户第一天」变成静默禁止下单，那正是本单元要消灭的故障。
3. **降级与拦截都必须被看见**：走 ``notify_user``，不是 ``logger``——日志不算被
   看见。通知投递失败只记日志，绝不改变「拒绝」这个决定本身。
"""

from __future__ import annotations

import unittest
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from apps.riskguard.guard import RiskGuard
from apps.trading.adapters.base import OrderRequest

TODAY = date(2026, 9, 22)
NOW = datetime(2026, 9, 22, 6, 0, 0)


class _FakeOrderManager:
    """``Order.objects`` 的替身：只关心 aggregate 出来的当日已实现盈亏。"""

    def __init__(self, pnl=None, error=None):
        self.pnl = pnl
        self.error = error
        self.filter_kwargs: dict = {}

    def filter(self, **kwargs):
        self.filter_kwargs = kwargs
        if self.error is not None:
            raise self.error
        return SimpleNamespace(aggregate=lambda **kw: {"total_pnl": self.pnl})


class _FakeSnapshotManager:
    """``DailySnapshot.objects`` 的替身：记下过滤条件，回放一条快照或 None。"""

    def __init__(self, snapshot=None):
        self.snapshot = snapshot
        self.filter_kwargs: dict = {}

    def filter(self, **kwargs):
        self.filter_kwargs = kwargs
        snap = self.snapshot
        return SimpleNamespace(
            order_by=lambda *a, **kw: SimpleNamespace(first=lambda: snap)
        )


def _snapshot(equity, when=None):
    return SimpleNamespace(total_equity=Decimal(str(equity)), date=when or TODAY)


class _DrawdownTestBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.guard = RiskGuard(mode="trading")
        RiskGuard._instance = None
        self.addCleanup(setattr, RiskGuard, "_instance", None)

    def _install(self, orders, snapshots):
        from apps.trading.models import DailySnapshot, Order

        for model, manager in ((Order, orders), (DailySnapshot, snapshots)):
            patcher = patch.object(model, "objects", manager)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _patch_notify(self, delivered=True):
        mock = AsyncMock(return_value=delivered)
        patcher = patch("apps.trading.alerts.notify_user", mock)
        patcher.start()
        self.addCleanup(patcher.stop)
        return mock

    async def _check(self, orders, snapshots, notify=None):
        """跑一轮 `_check_drawdown`，把「当前时间」钉在 NOW（否则跨日会飘）。"""
        self._install(orders, snapshots)
        if notify is None:
            notify = self._patch_notify()
        with patch("django.utils.timezone.now", return_value=NOW):
            result = await self.guard._check_drawdown("user-1")
        return result, notify


class TestDrawdownDenominator(_DrawdownTestBase):
    async def test_reads_todays_snapshot_only(self):
        """过滤条件必须恰好是当日（``date=today``），不是 ``date__lt``。

        `date__lt` 会把当日快照排除掉，于是第一次会话的当天永远没有分母 →
        必然拒单；而回退到昨天（``date__lte``）又是拿旧权益配今天的盈亏，两个
        口径。所以钉的是过滤条件本身：只认当天那一条。
        """
        orders = _FakeOrderManager(pnl=Decimal("-10"))
        snapshots = _FakeSnapshotManager(_snapshot("1000"))
        (approved, reason), _ = await self._check(orders, snapshots)

        self.assertTrue(approved, reason)
        self.assertEqual(snapshots.filter_kwargs["user_id"], "user-1")
        self.assertEqual(snapshots.filter_kwargs["date"], TODAY)
        self.assertNotIn("date__lt", snapshots.filter_kwargs)
        self.assertNotIn("date__lte", snapshots.filter_kwargs)

    async def test_todays_snapshot_is_the_denominator(self):
        """当日快照存在时就用它：亏损 4% 放行，6% 拦截（上限 5%）。"""
        snapshots = _FakeSnapshotManager(_snapshot("1000"))
        (approved, _), _ = await self._check(
            _FakeOrderManager(pnl=Decimal("-40")), snapshots
        )
        self.assertTrue(approved)

        snapshots = _FakeSnapshotManager(_snapshot("1000"))
        (approved, reason), notify = await self._check(
            _FakeOrderManager(pnl=Decimal("-60")), snapshots
        )
        self.assertFalse(approved)
        self.assertIn("日内回撤", reason)
        notify.assert_awaited_once()
        self.assertIn("日内回撤", notify.await_args.args[1])

    async def test_profit_or_flat_short_circuits_before_any_snapshot_read(self):
        """当日是盈利/无成交时不读快照——这也让「刚过 UTC 日界」天然无害。"""
        snapshots = _FakeSnapshotManager(None)
        (approved, _), _ = await self._check(
            _FakeOrderManager(pnl=Decimal("5")), snapshots, notify=self._patch_notify()
        )
        self.assertTrue(approved)
        self.assertEqual(snapshots.filter_kwargs, {})

        snapshots = _FakeSnapshotManager(None)
        (approved, _), _ = await self._check(
            _FakeOrderManager(pnl=None), snapshots, notify=self._patch_notify()
        )
        self.assertTrue(approved)
        self.assertEqual(snapshots.filter_kwargs, {})


class TestDrawdownDegradation(_DrawdownTestBase):
    async def test_no_snapshot_for_today_degrades_open_with_a_warning(self):
        """当日没有快照（新用户第一次会话 / 当日首条还没写入）→ 放行 + WARNING，
        而不是拒单。

        写入方 ≤5 分钟就把分母补上；在这之前拒单等于「新用户第一天禁止下单」，
        而那正是一条静默故障——用户只会看到「下单失败」。
        """
        with self.assertLogs("apps.riskguard.guard", level="WARNING") as logs:
            (approved, _), notify = await self._check(
                _FakeOrderManager(pnl=Decimal("-10")), _FakeSnapshotManager(None)
            )

        self.assertTrue(approved)
        notify.assert_not_awaited()
        self.assertTrue(any("降级放行" in line for line in logs.output))

    async def test_zero_equity_is_rejected_loudly(self):
        """净值真的是 0 与「取不到」是两件事：前者拒绝，但必须说出来。"""
        (approved, reason), notify = await self._check(
            _FakeOrderManager(pnl=Decimal("-1")), _FakeSnapshotManager(_snapshot("0"))
        )

        self.assertFalse(approved)
        self.assertIn("净值为 0", reason)
        notify.assert_awaited_once()

    async def test_pnl_query_failure_rejects_and_notifies(self):
        """分子读不出来 → 拒绝（保守），并且让用户看见原因。"""
        (approved, reason), notify = await self._check(
            _FakeOrderManager(error=RuntimeError("db down")), _FakeSnapshotManager(None)
        )

        self.assertFalse(approved)
        self.assertIn("已实现盈亏", reason)
        notify.assert_awaited_once()

    async def test_snapshot_query_failure_rejects_and_notifies(self):
        """分母查询抛错（与「查不到」不同）→ 拒绝并告知。"""
        snapshots = _FakeSnapshotManager(None)
        snapshots.filter = MagicMock(side_effect=RuntimeError("db down"))
        (approved, reason), notify = await self._check(
            _FakeOrderManager(pnl=Decimal("-1")), snapshots
        )

        self.assertFalse(approved)
        self.assertIn("净值快照", reason)
        notify.assert_awaited_once()


class TestRejectIsVisible(unittest.IsolatedAsyncioTestCase):
    """pre_trade_check 返回 False 时 OrderExecutor 抛 PermissionError，而这发生在
    订单落库**之前**——既没有订单行，也不会走 `record_order_failure` 的通知。
    用户意图于是被静默丢弃。所以每一条拦截路径都必须自己把话说出去。
    """

    def setUp(self):
        self.guard = RiskGuard(mode="trading")
        RiskGuard._instance = None
        self.addCleanup(setattr, RiskGuard, "_instance", None)
        self.request = OrderRequest(
            exchange="binance",
            symbol="BTCUSDT",
            order_type="market",
            side="buy",
            quantity=Decimal("0.001"),
            price=None,
        )
        # 第②段给前置校验加了第 0 步（halt 状态机），它是唯一真读库的一步，而没有数据库
        # 的测试里它必然抛错、进而 fail-closed 拒单。本类测的是**后面四步的拒绝也要被
        # 看见**，所以把第 0 步桩成放行；第 0 步自己的行为在 `test_guard_halt.py`。
        patcher = patch.object(
            self.guard, "_halt_block_reason", AsyncMock(return_value="")
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _patch_notify(self, delivered=True, error=None):
        mock = AsyncMock(return_value=delivered, side_effect=error)
        patcher = patch("apps.trading.alerts.notify_user", mock)
        patcher.start()
        self.addCleanup(patcher.stop)
        return mock

    async def test_daily_trade_limit_rejection_notifies(self):
        notify = self._patch_notify()
        with patch.object(
            self.guard, "_get_daily_trade_count", AsyncMock(return_value=999)
        ):
            approved, reason = await self.guard.pre_trade_check(self.request, "user-1")

        self.assertFalse(approved)
        self.assertIn("交易次数", reason)
        notify.assert_awaited_once()
        message = notify.await_args.args[1]
        self.assertIn("风控拦截", message)
        self.assertIn("交易次数", message)

    async def test_position_limit_rejection_notifies(self):
        notify = self._patch_notify()
        with patch.object(
            self.guard, "_get_daily_trade_count", AsyncMock(return_value=0)
        ):
            with patch.object(
                self.guard,
                "_check_position_limit",
                AsyncMock(return_value=(False, "单笔仓位 50% 超过上限 20%")),
            ):
                approved, _ = await self.guard.pre_trade_check(self.request, "user-1")

        self.assertFalse(approved)
        notify.assert_awaited_once()
        self.assertIn("单笔仓位", notify.await_args.args[1])

    async def test_drawdown_rejection_notifies(self):
        notify = self._patch_notify()
        with patch.object(
            self.guard, "_get_daily_trade_count", AsyncMock(return_value=0)
        ):
            with patch.object(
                self.guard, "_check_position_limit", AsyncMock(return_value=(True, ""))
            ):
                with patch.object(
                    self.guard,
                    "_check_drawdown",
                    AsyncMock(return_value=(False, "日内回撤 8.0% 超过上限 5%")),
                ):
                    approved, _ = await self.guard.pre_trade_check(
                        self.request, "user-1"
                    )

        self.assertFalse(approved)
        notify.assert_awaited_once()
        self.assertIn("日内回撤", notify.await_args.args[1])

    async def test_approved_order_does_not_notify(self):
        notify = self._patch_notify()
        with patch.object(
            self.guard, "_get_daily_trade_count", AsyncMock(return_value=0)
        ):
            with patch.object(
                self.guard, "_check_position_limit", AsyncMock(return_value=(True, ""))
            ):
                with patch.object(
                    self.guard, "_check_drawdown", AsyncMock(return_value=(True, ""))
                ):
                    approved, _ = await self.guard.pre_trade_check(
                        self.request, "user-1"
                    )

        self.assertTrue(approved)
        notify.assert_not_awaited()

    async def test_notification_failure_does_not_flip_the_decision(self):
        """通知是旁路，不是判定的一部分：投递炸了也必须返回「拒绝」。"""
        self._patch_notify(error=RuntimeError("redis down"))
        with patch.object(
            self.guard, "_get_daily_trade_count", AsyncMock(return_value=999)
        ):
            with self.assertLogs("apps.riskguard.guard", level="ERROR"):
                approved, reason = await self.guard.pre_trade_check(
                    self.request, "user-1"
                )

        self.assertFalse(approved)
        self.assertIn("交易次数", reason)

    async def test_notify_returning_false_does_not_flip_the_decision(self):
        """`notify_user` 返回 False 只表示「没人可送」，不是「放行」。"""
        self._patch_notify(delivered=False)
        result = await self.guard._reject("user-1", "原始原因")
        self.assertEqual(result, (False, "原始原因"))

    async def test_reject_without_user_does_not_write_anything(self):
        """走真实的通知口：无 user_id → 不落库、不推送，记 ERROR，仍然拒绝。

        `_reject` 里那条 except 是为了「通知炸了也不改判定」；这里反向确认它没有
        把「无接收人」误当成异常路径吞掉——那种沉默正是本单元要消灭的。
        """
        with (
            patch("apps.trading.alerts._persist") as persist,
            patch("apps.trading.alerts._publish") as publish,
        ):
            with self.assertLogs("apps.trading.alerts", level="ERROR"):
                approved, reason = await self.guard._reject(None, "原始原因")

        self.assertFalse(approved)
        self.assertEqual(reason, "原始原因")
        persist.assert_not_called()
        publish.assert_not_called()
