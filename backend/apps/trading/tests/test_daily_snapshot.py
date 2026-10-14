"""净值快照写入方（第①段强制前置）的测试面。

CONTEXT.md 第 142 条要的是「给 `daily_snapshots` 补一个写入方，**不是放宽那条
检查**」。所以这个测试面钉的是写入方的三件硬约束，每一条都对应一种会让回撤线
静默失真的写法：

1. **期初不能编造**：任一账户余额取不到 → 本轮不写（既不写 0、也不写部分和），
   并把「日内回撤保护降级」告警给用户；
2. **分母不能失真**：同一账户挂两个会话只算一次（否则余额翻倍、回撤被腰斩），
   `stopped` 的会话一律不计（否则已平仓的账户还在撑分母）；
3. **幂等**：当日已有快照就不动它，所以 5 分钟一轮随便重试，不需要「日界必须
   准确跑」这种脆弱前提。

与 ``test_dangling_order_reconcile.py`` 同一风格：mock ORM，断言的是「写进去/没写
进去什么」与「用了哪些过滤条件」——那是「如实」与「假装送达」的分界。
"""

from __future__ import annotations

import unittest
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from apps.trading.daily_snapshot import (
    _active_session_statuses,
    collect_user_equity,
    write_daily_snapshots,
)

TODAY = date(2026, 9, 22)
NOW = datetime(2026, 9, 22, 3, 0, 0)


class _ValuesListQS:
    """``filter(...).values_list("user_id", flat=True).distinct()`` 的替身。"""

    def __init__(self, user_ids):
        self._user_ids = user_ids

    def values_list(self, *args, **kwargs):
        return self

    def distinct(self):
        return list(self._user_ids)


class _SessionQS:
    """``filter(...).select_related("exchange_account")`` 的替身。"""

    def __init__(self, sessions):
        self._sessions = sessions

    def select_related(self, *args, **kwargs):
        return list(self._sessions)


class _FakeLiveSessionManager:
    """把两种 filter 调用形状分开：带 ``user_id`` 的取会话，其余取用户列表。

    同时把 filter 的 kwargs 记下来，供「stopped 必须被排除」那条断言使用。
    """

    def __init__(self, user_ids=(), sessions_by_user=None):
        self._user_ids = list(user_ids)
        self._sessions_by_user = sessions_by_user or {}
        self.filter_kwargs: list[dict] = []

    def filter(self, **kwargs):
        self.filter_kwargs.append(kwargs)
        if "user_id" in kwargs:
            return _SessionQS(self._sessions_by_user.get(kwargs["user_id"], []))
        return _ValuesListQS(self._user_ids)


class _FakeSnapshotManager:
    """``DailySnapshot.objects`` 的替身：``exists()`` 可控，``create()`` 可断言。"""

    def __init__(self, already=False, create_side_effect=None):
        self.already = already
        self.create = MagicMock(side_effect=create_side_effect)
        self.exists = MagicMock(return_value=already)
        self.filter_kwargs: list[dict] = []

    def filter(self, **kwargs):
        self.filter_kwargs.append(kwargs)
        return SimpleNamespace(exists=self.exists)


def _session(account_id):
    account = None if account_id is None else SimpleNamespace(id=account_id)
    return SimpleNamespace(exchange_account=account)


def _adapter(balance):
    adapter = MagicMock()
    if isinstance(balance, Exception):
        adapter.get_balance = AsyncMock(side_effect=balance)
    else:
        adapter.get_balance = AsyncMock(return_value=balance)
    adapter.disconnect = AsyncMock()
    return adapter


class _SnapshotTestBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # 告警去重表是模块级状态，跨用例会互相干扰
        from apps.trading import daily_snapshot

        daily_snapshot._alerted_on.clear()
        self.addCleanup(daily_snapshot._alerted_on.clear)

    def _patch_live_sessions(self, manager):
        from apps.trading.models import LiveSession

        patcher = patch.object(LiveSession, "objects", manager)
        patcher.start()
        self.addCleanup(patcher.stop)
        return manager

    def _patch_snapshots(self, manager):
        from apps.trading.models import DailySnapshot

        patcher = patch.object(DailySnapshot, "objects", manager)
        patcher.start()
        self.addCleanup(patcher.stop)
        return manager

    def _patch_adapter(self, adapter):
        return patch(
            "apps.trading.pending_reconcile.build_account_adapter",
            new=AsyncMock(return_value=adapter),
        )

    def _patch_notify(self, delivered=True):
        mock = AsyncMock(return_value=delivered)
        patcher = patch("apps.trading.alerts.notify_user", mock)
        patcher.start()
        self.addCleanup(patcher.stop)
        return mock


class TestActiveSessionStatuses(_SnapshotTestBase):
    def test_stopped_is_the_only_excluded_status(self):
        """用排除法从 STATUS_CHOICES 推导：漏计一个状态会让分母偏小、回撤被高估。"""
        from apps.trading.models import LiveSession

        all_statuses = [s for s, _ in LiveSession.STATUS_CHOICES]
        self.assertEqual(
            all_statuses, ["pending", "running", "paused", "stopped", "error"]
        )
        self.assertEqual(
            set(_active_session_statuses()), {"pending", "running", "paused", "error"}
        )


class TestCollectUserEquity(_SnapshotTestBase):
    async def test_sums_distinct_accounts(self):
        """两个不同账户 → 相加（各读各的余额）。"""
        self._patch_live_sessions(
            _FakeLiveSessionManager(
                sessions_by_user={"u1": [_session("acc-1"), _session("acc-2")]}
            )
        )
        with patch(
            "apps.trading.pending_reconcile.build_account_adapter",
            new=AsyncMock(
                side_effect=[
                    _adapter({"USDT": Decimal("100")}),
                    _adapter({"USDT": Decimal("50")}),
                ]
            ),
        ):
            equity = await collect_user_equity("u1")
        self.assertEqual(equity, Decimal("150"))

    async def test_two_sessions_on_one_account_are_counted_once(self):
        """同一账户挂两个会话：余额只取一次，不翻倍。

        建会话时两个会话的 ``current_equity`` 都等于该账户的**完整**余额，按会话
        求和会把分母翻倍、回撤被腰斩——这正是这条检查最怕的失真。
        """
        self._patch_live_sessions(
            _FakeLiveSessionManager(
                sessions_by_user={"u1": [_session("acc-1"), _session("acc-1")]}
            )
        )
        adapter = _adapter({"USDT": Decimal("100")})
        with self._patch_adapter(adapter) as build:
            equity = await collect_user_equity("u1")
        self.assertEqual(equity, Decimal("100"))
        self.assertEqual(build.await_count, 1)
        adapter.get_balance.assert_awaited_once()

    async def test_no_active_sessions_yields_none_not_zero(self):
        """没有账户可引用 → None。写 0 会让检查走 initial<=0 分支拒单，等于换个入口搬回静默故障。"""
        self._patch_live_sessions(_FakeLiveSessionManager(sessions_by_user={"u1": []}))
        with self._patch_adapter(None) as build:
            equity = await collect_user_equity("u1")
        self.assertIsNone(equity)
        build.assert_not_awaited()

    async def test_any_failed_balance_yields_none_not_partial_sum(self):
        """两个账户，其中一个取不到 → None。部分和是一个编出来的期初数字。"""
        self._patch_live_sessions(
            _FakeLiveSessionManager(
                sessions_by_user={"u1": [_session("acc-1"), _session("acc-2")]}
            )
        )
        ok = _adapter({"USDT": Decimal("100")})
        broken = _adapter(RuntimeError("network down"))
        with patch(
            "apps.trading.pending_reconcile.build_account_adapter",
            new=AsyncMock(side_effect=[ok, broken]),
        ):
            equity = await collect_user_equity("u1")
        self.assertIsNone(equity)

    async def test_unparseable_balance_is_none(self):
        """余额解析不出来 → None，不当作 0。"""
        self._patch_live_sessions(
            _FakeLiveSessionManager(sessions_by_user={"u1": [_session("acc-1")]})
        )
        with self._patch_adapter(_adapter({"USDT": "not-a-number"})):
            self.assertIsNone(await collect_user_equity("u1"))

    async def test_adapter_disconnects_after_balance_read(self):
        """取完余额必须断开适配器，否则每 5 分钟泄一个连接。"""
        self._patch_live_sessions(
            _FakeLiveSessionManager(sessions_by_user={"u1": [_session("acc-1")]})
        )
        adapter = _adapter({"USDT": Decimal("1")})
        with self._patch_adapter(adapter):
            await collect_user_equity("u1")
        adapter.disconnect.assert_awaited_once()

    async def test_session_without_account_is_ignored(self):
        """会话未绑账户 → 不计入，也不因此判定失败（它本来就没有余额可读）。"""
        self._patch_live_sessions(
            _FakeLiveSessionManager(
                sessions_by_user={"u1": [_session(None), _session("acc-1")]}
            )
        )
        with self._patch_adapter(_adapter({"USDT": Decimal("7")})) as build:
            equity = await collect_user_equity("u1")
        self.assertEqual(equity, Decimal("7"))
        self.assertEqual(build.await_count, 1)


class TestWriteDailySnapshots(_SnapshotTestBase):
    async def test_writes_snapshot_labelled_today(self):
        """快照标当日（UTC，与检查侧 today 同口径），值 = 去重后账户余额之和。"""
        self._patch_live_sessions(
            _FakeLiveSessionManager(
                user_ids=["u1"], sessions_by_user={"u1": [_session("acc-1")]}
            )
        )
        snapshots = self._patch_snapshots(_FakeSnapshotManager())
        with self._patch_adapter(_adapter({"USDT": Decimal("123.45")})):
            result = await write_daily_snapshots(now=NOW)

        self.assertEqual(result.users, 1)
        self.assertEqual(result.written, 1)
        self.assertEqual(result.skipped_no_equity, 0)
        kwargs = snapshots.create.call_args.kwargs
        self.assertEqual(kwargs["user_id"], "u1")
        self.assertEqual(kwargs["date"], TODAY)
        self.assertEqual(kwargs["total_equity"], Decimal("123.45"))
        self.assertEqual(snapshots.filter_kwargs[0], {"user_id": "u1", "date": TODAY})

    async def test_stopped_sessions_are_excluded_from_the_query(self):
        """查询必须把 stopped 排除在外——用 kwargs 断言，mock 不会替我们过滤。"""
        manager = self._patch_live_sessions(_FakeLiveSessionManager(user_ids=[]))
        self._patch_snapshots(_FakeSnapshotManager())
        await write_daily_snapshots(now=NOW)

        self.assertEqual(len(manager.filter_kwargs), 1)
        self.assertEqual(
            set(manager.filter_kwargs[0]["status__in"]),
            {"pending", "running", "paused", "error"},
        )

    async def test_existing_snapshot_is_left_untouched(self):
        """当日已有快照 → 跳过，不读余额、不覆盖、不告警。"""
        self._patch_live_sessions(_FakeLiveSessionManager(user_ids=["u1"]))
        snapshots = self._patch_snapshots(_FakeSnapshotManager(already=True))
        notify = self._patch_notify()
        with self._patch_adapter(_adapter({"USDT": Decimal("9")})) as build:
            result = await write_daily_snapshots(now=NOW)

        self.assertEqual(result.already_written, 1)
        self.assertEqual(result.written, 0)
        build.assert_not_awaited()
        snapshots.create.assert_not_called()
        notify.assert_not_awaited()

    async def test_failed_balance_writes_nothing_and_alerts_the_user(self):
        """取不到余额：不写快照，并把「回撤保护降级」告警出去。"""
        self._patch_live_sessions(
            _FakeLiveSessionManager(
                user_ids=["u1"], sessions_by_user={"u1": [_session("acc-1")]}
            )
        )
        snapshots = self._patch_snapshots(_FakeSnapshotManager())
        notify = self._patch_notify(delivered=True)
        with self._patch_adapter(_adapter(RuntimeError("network down"))):
            result = await write_daily_snapshots(now=NOW)

        self.assertEqual(result.skipped_no_equity, 1)
        self.assertEqual(result.written, 0)
        self.assertEqual(result.alert_undelivered, 0)
        snapshots.create.assert_not_called()
        notify.assert_awaited_once()
        message = notify.await_args.args[1]
        self.assertIn("净值快照", message)
        self.assertIn("降级", message)

    async def test_alert_is_sent_at_most_once_per_day(self):
        """5 分钟一轮，账户持续不可达时逐轮告警会变成骚扰 → 骚扰的结果是被静音。"""
        self._patch_live_sessions(
            _FakeLiveSessionManager(
                user_ids=["u1"], sessions_by_user={"u1": [_session("acc-1")]}
            )
        )
        self._patch_snapshots(_FakeSnapshotManager())
        notify = self._patch_notify(delivered=True)
        with self._patch_adapter(_adapter(RuntimeError("network down"))):
            first = await write_daily_snapshots(now=NOW)
            second = await write_daily_snapshots(now=NOW)

        self.assertEqual(first.skipped_no_equity, 1)
        self.assertEqual(second.skipped_no_equity, 1)
        notify.assert_awaited_once()

    async def test_undelivered_alert_is_counted_not_swallowed(self):
        """没人可送（用户已注销/无接收人）→ 计入结果，不假装送达，也不记为已告警。"""
        self._patch_live_sessions(
            _FakeLiveSessionManager(
                user_ids=["u1"], sessions_by_user={"u1": [_session("acc-1")]}
            )
        )
        self._patch_snapshots(_FakeSnapshotManager())
        notify = self._patch_notify(delivered=False)
        with self._patch_adapter(_adapter(RuntimeError("network down"))):
            result = await write_daily_snapshots(now=NOW)

        self.assertEqual(result.alert_undelivered, 1)
        # 未送达就不算已告警：下一轮还要再试
        with self._patch_adapter(_adapter(RuntimeError("network down"))):
            again = await write_daily_snapshots(now=NOW)
        self.assertEqual(again.alert_undelivered, 1)
        self.assertEqual(notify.await_count, 2)

    async def test_concurrent_write_is_counted_as_already_written(self):
        """两个 worker 同时写同一天 → 唯一约束报错，按「已有」计，不炸任务。"""
        self._patch_live_sessions(
            _FakeLiveSessionManager(
                user_ids=["u1"], sessions_by_user={"u1": [_session("acc-1")]}
            )
        )
        self._patch_snapshots(
            _FakeSnapshotManager(
                create_side_effect=Exception("UNIQUE constraint failed")
            )
        )
        self._patch_notify()
        with self._patch_adapter(_adapter({"USDT": Decimal("5")})):
            result = await write_daily_snapshots(now=NOW)

        self.assertEqual(result.written, 0)
        self.assertEqual(result.already_written, 1)

    async def test_multiple_users_are_each_snapshotted(self):
        """每个用户一条，互不影响：一个用户的账户失联不牵连另一个。"""
        self._patch_live_sessions(
            _FakeLiveSessionManager(
                user_ids=["u1", "u2"],
                sessions_by_user={"u1": [_session("acc-1")], "u2": [_session("acc-2")]},
            )
        )
        snapshots = self._patch_snapshots(_FakeSnapshotManager())
        self._patch_notify()
        with patch(
            "apps.trading.pending_reconcile.build_account_adapter",
            new=AsyncMock(
                side_effect=[
                    _adapter(RuntimeError("network down")),
                    _adapter({"USDT": Decimal("42")}),
                ]
            ),
        ):
            result = await write_daily_snapshots(now=NOW)

        self.assertEqual(result.users, 2)
        self.assertEqual(result.skipped_no_equity, 1)
        self.assertEqual(result.written, 1)
        self.assertEqual(snapshots.create.call_args.kwargs["user_id"], "u2")

    async def test_result_dict_carries_the_counters(self):
        """结果进任务健康检查：计数不能只活在日志里。"""
        self._patch_live_sessions(_FakeLiveSessionManager(user_ids=["u1"]))
        self._patch_snapshots(_FakeSnapshotManager(already=True))
        result = await write_daily_snapshots(now=NOW)
        self.assertEqual(
            result.as_dict(),
            {
                "users": 1,
                "written": 0,
                "already_written": 1,
                "skipped_no_equity": 0,
                "alert_undelivered": 0,
            },
        )


class TestTaskWiring(unittest.TestCase):
    def test_beat_schedule_runs_the_writer(self):
        """写入方必须是 beat 任务：进程内没有别的调用者。"""
        from celery_app import app

        entry = app.conf.beat_schedule["snapshot-daily-equity"]
        self.assertEqual(entry["task"], "apps.trading.tasks.snapshot_daily_equity")
        self.assertGreater(entry["schedule"], 0)

    def test_task_is_registered(self):
        from celery_app import app

        self.assertIn("apps.trading.tasks.snapshot_daily_equity", app.tasks)

    def test_task_runs_the_writer(self):
        """任务体必须真的调到写入方，而不是只返回一个空 dict。

        日线续拉、判定、停用决策推导、Shadow 记录写入、日报生成也一并替掉：五者都碰
        DB（日线表 / 池化表 / 判定记录 / 日报表），而这里是 ``unittest.TestCase``，
        不该为一条接线测试去开数据库；续拉更要换掉——它是这条链里**唯一会发网络请求**
        的一步，真跑等于把交易所 API 拉进一条接线测试。六者之间的先后由
        ``apps.regime.tests.test_timing`` 负责，这里只确认六条职责都被调到、都进了返回值。

        替身打在**源模块**上：任务体是函数内 import（不在 import 期就把交易/判定
        链路拉起来），``apps.trading.tasks`` 上根本没有这六个名字。
        """
        from apps.regime.candles import SyncReport
        from apps.trading.tasks import snapshot_daily_equity

        fake = MagicMock()
        fake.as_dict.return_value = {"users": 1, "written": 1}
        # 真身是 frozen dataclass，这里照实构造：``wrote`` 与 ``last_date.isoformat()``
        # 走的是任务体真的会调的那两条路径
        candle_report = SyncReport(
            symbol="BTC/USDT",
            requested_start=date(2026, 9, 22),
            requested_end=date(2026, 9, 22),
            fetched=1,
            accepted=1,
            incomplete=0,
            out_of_range=0,
            created=1,
            updated=0,
            unchanged=0,
            first_date=date(2026, 9, 22),
            last_date=date(2026, 9, 22),
            dry_run=False,
        )
        with (
            patch(
                "apps.trading.daily_snapshot.write_daily_snapshots",
                new=AsyncMock(return_value=fake),
            ) as writer,
            patch(
                "apps.regime.candles.sync_daily_candles",
                new=MagicMock(return_value=candle_report),
            ) as candles_sync,
            patch(
                "apps.regime.judgement.run_daily_judgement",
                new=MagicMock(return_value={"skipped": "no_candles"}),
            ) as judgement,
            patch(
                "apps.regime.deactivation_run.run_deactivation",
                new=MagicMock(
                    return_value={
                        "skipped": "no_generation",
                        "note": "还没有任何一代池化表",
                    }
                ),
            ) as deactivation,
            patch(
                "apps.regime.shadow.write_shadow_record",
                new=MagicMock(
                    return_value={"outcome": "created", "note": "还没有任何一代池化表"}
                ),
            ) as shadow,
            patch(
                "apps.regime.report.write_daily_report",
                new=MagicMock(
                    return_value={
                        "outcome": "waiting",
                        # 判定没结论、又没到截止时刻时，真身返回的就是这个形状。
                        "written": False,
                        "note": "今日判定尚无结论，且未到日报截止时刻，本轮不写日报",
                    }
                ),
            ) as daily_report,
        ):
            payload = snapshot_daily_equity()

        writer.assert_awaited_once()
        candles_sync.assert_called_once()
        judgement.assert_called_once()
        deactivation.assert_called_once()
        shadow.assert_called_once()
        daily_report.assert_called_once()
        self.assertEqual(
            daily_report.call_args.args,
            (
                payload["regime"],
                payload["deactivation"],
                payload["shadow"],
            ),
            "日报必须拿到前面三段**刚落下的产物**，而不是三个空 dict——"
            'stub 忽略入参时，`payload.get("shadow")` 写错成 `{}` 也能过，'
            "而那正是「日报说没有、实际有」这类假话的入口",
        )
        self.assertEqual(
            payload,
            {
                "users": 1,
                "written": 1,
                "candles": {
                    "created": 1,
                    "updated": 0,
                    "unchanged": 0,
                    "last_date": "2026-09-22",
                },
                "regime": {"skipped": "no_candles"},
                "deactivation": {
                    "skipped": "no_generation",
                    "note": "还没有任何一代池化表",
                },
                "shadow": {"outcome": "created", "note": "还没有任何一代池化表"},
                "report": {
                    "outcome": "waiting",
                    "written": False,
                    "note": "今日判定尚无结论，且未到日报截止时刻，本轮不写日报",
                },
            },
            "返回值是任务健康检查唯一看得到的东西，六条职责都必须在里面；"
            "`skipped`/`note` 尤其不能丢——「日志不算被看见」，那句话的正经出口是日报",
        )


class TestTrackedRealRun(_SnapshotTestBase):
    """不 mock 写入方，跑通 asyncio 入口，确认没有「只在 mock 下成立」的写法。"""

    async def test_direct_call_returns_result(self):
        self._patch_live_sessions(_FakeLiveSessionManager(user_ids=[]))
        self._patch_snapshots(_FakeSnapshotManager())
        result = await write_daily_snapshots(now=NOW)
        self.assertEqual(result.users, 0)
