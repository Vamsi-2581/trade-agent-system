"""Tests for ScheduledOneTimeTask model and persistence logic."""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from asgiref.sync import sync_to_async
from django.test import TestCase, TransactionTestCase
from unittest.mock import MagicMock, patch

os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "true")


class TestScheduledOneTimeTaskModel(TestCase):
    """Test ScheduledOneTimeTask model creation and fields."""

    def test_create_pending_task(self):
        """Should create a task with status=pending."""
        from apps.agent.models import ScheduledOneTimeTask

        run_at = datetime.now(timezone.utc) + timedelta(hours=1)
        task = ScheduledOneTimeTask.objects.create(
            task_name="test_reminder",
            agent_name="analyst",
            message="BTC price analysis",
            user_id="test-user-123",
            run_at=run_at,
        )
        self.assertEqual(task.status, "pending")
        self.assertIsInstance(task.id, UUID)
        self.assertEqual(task.celery_task_id, "")
        self.assertIsNone(task.executed_at)
        self.assertEqual(task.result, "")
        self.assertEqual(task.error, "")

    def test_terminal_statuses(self):
        """Should accept all valid terminal statuses."""
        from apps.agent.models import ScheduledOneTimeTask

        run_at = datetime.now(timezone.utc)
        for status in ("completed", "failed", "revoked", "missed"):
            task = ScheduledOneTimeTask.objects.create(
                task_name=f"test_{status}",
                agent_name="analyst",
                message="test",
                run_at=run_at,
                status=status,
            )
            self.assertEqual(task.status, status)


class TestExecuteScheduledTaskStateMachine(TestCase):
    """Test CAS state machine in execute_scheduled_agent_task."""

    def test_cas_marks_running_on_pending(self):
        """Pending task should be CAS'd to running when started."""
        from apps.agent.models import ScheduledOneTimeTask
        from apps.agent.tasks import _update_task_status

        run_at = datetime.now(timezone.utc)
        task = ScheduledOneTimeTask.objects.create(
            task_name="cas_test",
            agent_name="analyst",
            message="test",
            run_at=run_at,
        )
        affected = _update_task_status(str(task.id), "running")
        self.assertEqual(affected, 1)
        task.refresh_from_db()
        self.assertEqual(task.status, "running")
        self.assertIsNotNone(task.executed_at)

    def test_cas_skips_non_pending(self):
        """Non-pending task should be skipped (idempency)."""
        from apps.agent.models import ScheduledOneTimeTask
        from apps.agent.tasks import _update_task_status

        run_at = datetime.now(timezone.utc)
        task = ScheduledOneTimeTask.objects.create(
            task_name="cas_skip",
            agent_name="analyst",
            message="test",
            run_at=run_at,
            status="revoked",
        )
        affected = _update_task_status(str(task.id), "running")
        self.assertEqual(affected, 0)
        task.refresh_from_db()
        self.assertEqual(task.status, "revoked")

    def test_cas_concurrent_only_one_wins(self):
        """Only one concurrent CAS should succeed."""
        from apps.agent.models import ScheduledOneTimeTask
        from apps.agent.tasks import _update_task_status

        run_at = datetime.now(timezone.utc)
        task = ScheduledOneTimeTask.objects.create(
            task_name="cas_race",
            agent_name="analyst",
            message="test",
            run_at=run_at,
        )
        result1 = _update_task_status(str(task.id), "running")
        result2 = _update_task_status(str(task.id), "running")
        self.assertEqual(result1 + result2, 1)

    def test_mark_completed_on_success(self):
        """Successful task should be marked completed with result."""
        from apps.agent.models import ScheduledOneTimeTask
        from apps.agent.tasks import _finalize_task

        run_at = datetime.now(timezone.utc)
        task = ScheduledOneTimeTask.objects.create(
            task_name="finalize_ok",
            agent_name="analyst",
            message="test",
            run_at=run_at,
            status="running",
        )
        _finalize_task(str(task.id), "SUCCESS", result={"data": "ok"}, error=None)
        task.refresh_from_db()
        self.assertEqual(task.status, "completed")
        self.assertIn("ok", task.result)

    def test_mark_failed_on_error(self):
        """Task returning ERROR/FAILURE should be marked failed."""
        from apps.agent.models import ScheduledOneTimeTask
        from apps.agent.tasks import _finalize_task

        run_at = datetime.now(timezone.utc)
        task = ScheduledOneTimeTask.objects.create(
            task_name="finalize_fail",
            agent_name="analyst",
            message="test",
            run_at=run_at,
            status="running",
        )
        _finalize_task(str(task.id), "FAILURE", result=None, error="agent failed")
        task.refresh_from_db()
        self.assertEqual(task.status, "failed")
        self.assertIn("agent failed", task.error)


class TestSubmitScheduledTaskToolPersistence(TransactionTestCase):
    """Test SubmitScheduledTaskTool creates DB record and pushes to Celery.

    必须是 ``TransactionTestCase``：工具用 ``db_async``（``sync_to_async(
    thread_sensitive=False)``）写库，那是在**另一个线程的连接**上提交的，不在本用例
    的事务里。用 ``TestCase`` 的话两件坏事同时发生：(1) 外层事务挡住异步线程，写不进去；
    (2) 即使写成功，这一行也是**真实提交**的，用例结束后不会被回滚，于是
    ``scheduled_analyst``（``task_name=f"scheduled_{agent_name}"``）会泄漏给后面所有
    用例——``TestListScheduledTasksAPI::test_api_returns_one_time_tasks`` 断言
    「只有 1 条 one_time」时看到 2 条，就是这么来的。
    """

    @patch("apps.agent.tasks.execute_scheduled_agent_task.apply_async")
    async def test_creates_db_record_and_pushes(self, mock_apply_async):
        """Should create ScheduledOneTimeTask and call apply_async with eta."""
        from apps.agent.tools.schedule_task import SubmitScheduledTaskTool
        from apps.agent.models import ScheduledOneTimeTask
        from datetime import datetime, timezone, timedelta

        mock_result = MagicMock()
        mock_result.id = "celery-test-id-123"
        mock_apply_async.return_value = mock_result

        tool = SubmitScheduledTaskTool()
        run_at = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        result = await tool.execute(
            agent_name="analyst",
            message="Analyze BTC",
            run_at=run_at,
            user_id="user-456",
        )

        self.assertTrue(result.success)
        db_uuid = result.data["schedule_id"]
        task = await sync_to_async(ScheduledOneTimeTask.objects.get)(id=db_uuid)
        self.assertEqual(task.status, "pending")
        self.assertEqual(task.agent_name, "analyst")
        self.assertEqual(task.celery_task_id, "celery-test-id-123")
        mock_apply_async.assert_called_once()
        call_kwargs = mock_apply_async.call_args[1]
        self.assertIn("scheduled_task_id", call_kwargs["kwargs"])
        self.assertEqual(call_kwargs["kwargs"]["scheduled_task_id"], db_uuid)
        self.assertEqual(result.data["celery_task_id"], "celery-test-id-123")


class TestCancelScheduledTaskTool(TransactionTestCase):
    """Test CancelScheduledTaskTool with real ORM (TransactionTestCase for async DB writes)."""

    @pytest.mark.asyncio
    async def test_cancel_pending_task(self):
        """Should revoke a pending task via CAS + Celery revoke."""
        from apps.agent.models import ScheduledOneTimeTask
        from apps.agent.tools import schedule_task as st_module
        from apps.agent.tools.schedule_task import CancelScheduledTaskTool

        task_uuid = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
        # Create a real pending task in DB
        run_at = datetime.now(timezone.utc)
        ScheduledOneTimeTask.objects.create(
            task_name="cancel_test",
            agent_name="analyst",
            message="test",
            celery_task_id="fake-celery-id",
            run_at=run_at,
            status="pending",
            id=task_uuid,
        )

        with patch.object(st_module, "celery_app") as mock_celery:
            tool = CancelScheduledTaskTool()
            result = await tool.execute(
                task_id_or_name=task_uuid, task_type="scheduled"
            )
            mock_celery.control.revoke.assert_called_once_with(
                "fake-celery-id", terminate=True
            )

        assert result.success
        assert "CANCELLED" in result.data["status"]
        assert result.data["task_id"] == task_uuid
        # Verify DB state changed to revoked
        task = ScheduledOneTimeTask.objects.get(id=task_uuid)
        self.assertEqual(task.status, "revoked")

    @pytest.mark.asyncio
    async def test_cancel_already_completed(self):
        """Should return 'cannot cancel' for terminal tasks."""
        from apps.agent.models import ScheduledOneTimeTask
        from apps.agent.tools.schedule_task import CancelScheduledTaskTool

        task_uuid = "00000000-0000-0000-0000-000000000001"
        run_at = datetime.now(timezone.utc)
        ScheduledOneTimeTask.objects.create(
            task_name="cancel_completed",
            agent_name="analyst",
            message="test",
            run_at=run_at,
            status="completed",
            id=task_uuid,
        )

        tool = CancelScheduledTaskTool()
        result = await tool.execute(task_id_or_name=task_uuid, task_type="scheduled")

        assert not result.success
        assert "无法取消" in result.error

    @pytest.mark.asyncio
    async def test_cancel_task_without_celery_id(self):
        """Should revoke even if celery_task_id is empty."""
        from apps.agent.models import ScheduledOneTimeTask
        from apps.agent.tools import schedule_task as st_module
        from apps.agent.tools.schedule_task import CancelScheduledTaskTool

        task_uuid = "00000000-0000-0000-0000-000000000002"
        run_at = datetime.now(timezone.utc)
        ScheduledOneTimeTask.objects.create(
            task_name="cancel_no_celery",
            agent_name="analyst",
            message="test",
            run_at=run_at,
            status="pending",
            id=task_uuid,
        )

        with patch.object(st_module, "celery_app") as mock_celery:
            tool = CancelScheduledTaskTool()
            result = await tool.execute(
                task_id_or_name=task_uuid, task_type="scheduled"
            )
            # revoke should NOT be called since celery_task_id is empty
            mock_celery.control.revoke.assert_not_called()

        assert result.success
        assert "CANCELLED" in result.data["status"]


class TestStartupRecoveryCheck(TestCase):
    """Test startup_recovery_check reconciliation logic."""

    def setUp(self):
        self.now = datetime.now(timezone.utc)

    @patch("apps.agent.tasks._acquire_recovery_lock")
    @patch("apps.agent.tasks.execute_scheduled_agent_task")
    @patch("apps.agent.tasks._notify_user")
    def test_recovery_future_requeued(self, mock_notify, mock_task, mock_lock):
        """Future pending tasks should be re-queued with eta."""
        mock_lock.return_value = "worker:12345:1.0"
        mock_task.apply_async.return_value = MagicMock(id="new-celery-id-future")
        from apps.agent.tasks import startup_recovery_check
        from apps.agent.models import ScheduledOneTimeTask

        ScheduledOneTimeTask.objects.create(
            task_name="future",
            agent_name="analyst",
            message="future task",
            run_at=self.now + timedelta(hours=1),
            status="pending",
            celery_task_id="old-celery-id",
        )

        _result = startup_recovery_check()
        task = ScheduledOneTimeTask.objects.get(task_name="future")
        self.assertNotEqual(task.celery_task_id, "old-celery-id")
        self.assertEqual(task.status, "pending")
        call_kwargs = mock_task.apply_async.call_args[1]
        self.assertIn("eta", call_kwargs)

    @patch("apps.agent.tasks._acquire_recovery_lock")
    @patch("apps.agent.tasks.execute_scheduled_agent_task")
    @patch("apps.agent.tasks._notify_user")
    def test_recovery_grace_immediate(self, mock_notify, mock_task, mock_lock):
        """Grace-period tasks should be immediately executed."""
        mock_lock.return_value = "worker:12345:1.0"
        mock_task.apply_async.return_value = MagicMock(id="new-celery-id-grace")
        from apps.agent.tasks import startup_recovery_check
        from apps.agent.models import ScheduledOneTimeTask

        ScheduledOneTimeTask.objects.create(
            task_name="grace",
            agent_name="analyst",
            message="grace task",
            run_at=self.now - timedelta(minutes=3),
            status="pending",
        )

        _result = startup_recovery_check()
        task = ScheduledOneTimeTask.objects.get(task_name="grace")
        self.assertEqual(task.status, "pending")
        mock_task.apply_async.assert_called()
        call_kwargs = mock_task.apply_async.call_args[1]
        self.assertNotIn("eta", call_kwargs)

    @patch("apps.agent.tasks._acquire_recovery_lock")
    @patch("apps.agent.tasks.execute_scheduled_agent_task")
    @patch("apps.agent.tasks._notify_user")
    def test_recovery_missed_marked(self, mock_notify, mock_task, mock_lock):
        """Beyond-grace tasks should be marked missed."""
        mock_lock.return_value = "worker:12345:1.0"
        mock_task.apply_async.return_value = MagicMock(id="new-celery-id-missed")
        from apps.agent.tasks import startup_recovery_check
        from apps.agent.models import ScheduledOneTimeTask

        ScheduledOneTimeTask.objects.create(
            task_name="missed",
            agent_name="analyst",
            message="missed task",
            run_at=self.now - timedelta(hours=2),
            status="pending",
            user_id="user-123",
        )

        _result = startup_recovery_check()
        task = ScheduledOneTimeTask.objects.get(task_name="missed")
        self.assertEqual(task.status, "missed")
        mock_notify.assert_called()

    @patch("apps.agent.tasks._acquire_recovery_lock")
    @patch("apps.agent.tasks.execute_scheduled_agent_task")
    @patch("apps.agent.tasks._notify_user")
    def test_recovery_zombie_running(self, mock_notify, mock_task, mock_lock):
        """Stale running tasks should be recovered like pending."""
        mock_lock.return_value = "worker:12345:1.0"
        mock_task.apply_async.return_value = MagicMock(id="new-celery-id-zombie")
        from apps.agent.tasks import startup_recovery_check
        from apps.agent.models import ScheduledOneTimeTask

        _zombie_task = ScheduledOneTimeTask.objects.create(
            task_name="zombie",
            agent_name="analyst",
            message="zombie task",
            run_at=self.now - timedelta(minutes=15),
            status="running",
            celery_task_id="zombie-celery-id",
        )

        _result = startup_recovery_check()
        task = ScheduledOneTimeTask.objects.get(task_name="zombie")
        self.assertEqual(task.status, "pending")
        mock_task.apply_async.assert_called()


class TestGetTaskResultDBUUID(TransactionTestCase):
    """Test GetTaskResultTool recognizes DB UUID for one-time tasks."""

    def test_query_pending_one_time_task(self):
        """Should return SCHEDULED status for a DB UUID."""
        from apps.agent.tools.backtest import GetTaskResultTool
        from apps.agent.models import ScheduledOneTimeTask
        from django.db import connection

        run_at = datetime.now(timezone.utc) + timedelta(hours=1)
        task = ScheduledOneTimeTask.objects.create(
            task_name="query_test",
            agent_name="analyst",
            message="test",
            run_at=run_at,
            status="pending",
        )
        task_id = str(task.id)
        connection.close()

        tool = GetTaskResultTool()
        result = asyncio.get_event_loop().run_until_complete(
            tool.execute(task_id=task_id)
        )

        self.assertTrue(result.success)
        self.assertEqual(result.data["status"], "SCHEDULED")
        self.assertEqual(result.data["source"], "one_time")

    def test_query_completed_one_time_task(self):
        """Should return completed status with result."""
        from apps.agent.tools.backtest import GetTaskResultTool
        from apps.agent.models import ScheduledOneTimeTask
        from django.db import connection

        run_at = datetime.now(timezone.utc)
        task = ScheduledOneTimeTask.objects.create(
            task_name="query_done",
            agent_name="analyst",
            message="test",
            run_at=run_at,
            status="completed",
            result='{"status": "SUCCESS", "result": "analysis done"}',
            executed_at=datetime.now(timezone.utc),
        )
        task_id = str(task.id)
        connection.close()

        tool = GetTaskResultTool()
        result = asyncio.get_event_loop().run_until_complete(
            tool.execute(task_id=task_id)
        )

        self.assertTrue(result.success)
        self.assertEqual(result.data["status"], "COMPLETED")
        self.assertIn("analysis done", result.data["result"])


class TestListScheduledTasksAPI(TestCase):
    """Test that API returns both periodic and one-time tasks.

    直接调用 view 函数（不起 HTTP 栈），所以必须**关掉 DRF 限流**：限流计数器按
    「匿名访问者 IP」存在**共享 Redis** 里（实测键 ``:1:throttle_anon_127.0.0.1``，
    滑动窗口 60 秒），`REST_FRAMEWORK.DEFAULT_THROTTLE_RATES["anon"]` 是 20/min。
    每次调用 view 都从 127.0.0.1 出发、都算匿名，于是反复跑本文件（或与其它直接调
    view 的用例同跑）会攒满窗口 → 返回 429，而这条链路上被测的是**任务列表的组装**，
    跟限流无关。这里把限流类换成空元组，只在本类生效。
    """

    @pytest.fixture(autouse=True)
    def _no_throttling(self):
        """只在本类生效地关掉限流。

        用 `patch.object(api_settings, ...)` 而不是 `settings.REST_FRAMEWORK = {...}`：
        后者会触发 Django 的 `setting_changed` → DRF 的 `reload_api_settings` →
        `APISettings.reload()`，而它在 `delattr` 自己尚未实例化的属性时会抛
        `AttributeError: 'APISettings' object has no attribute ...`，把**同进程后续**
        的用例一并打挂（实测：本类的 3 个用例 setup ERROR）。`patch.object` 只改限流
        类本身，退出时原样还原，不惊动任何全局设置。
        """
        from rest_framework.settings import api_settings

        patched = patch.object(
            api_settings, "DEFAULT_THROTTLE_CLASSES", [], create=True
        )
        patched.start()
        self.addCleanup(patched.stop)

    @patch(
        "rest_framework.permissions.IsAuthenticated.has_permission", return_value=True
    )
    def test_api_returns_one_time_tasks(self, mock_auth):
        """Should include one-time tasks with source='one_time'."""
        from apps.agent.models import ScheduledOneTimeTask
        from rest_framework.test import APIRequestFactory

        # 列表接口读的是全表（不带「本次测试」这种过滤，这是它该有的行为），所以断
        # 绝对条数前要能认出自己那一行。库里既存的一次性任务（dev 库上真实存在）会
        # 把条数抬高，而失败看起来像接口漏了过滤。`task_name` 带上本用例独有的标记。
        marker = f"api_test_{uuid.uuid4().hex}"
        run_at = datetime.now(timezone.utc) + timedelta(hours=1)
        ScheduledOneTimeTask.objects.create(
            task_name=marker,
            agent_name="analyst",
            message="test",
            run_at=run_at,
            status="pending",
        )

        from apps.agent.views import list_scheduled_tasks

        request = APIRequestFactory().get("/api/agent/tasks/scheduled/")
        response = list_scheduled_tasks(request)
        self.assertEqual(response.status_code, 200)
        one_time_tasks = [
            t
            for t in response.data["tasks"]
            if t.get("source") == "one_time" and t.get("name") == marker
        ]
        self.assertEqual(len(one_time_tasks), 1)
        self.assertEqual(one_time_tasks[0]["agent_name"], "analyst")
        self.assertEqual(one_time_tasks[0]["status"], "pending")

    @patch(
        "rest_framework.permissions.IsAuthenticated.has_permission", return_value=True
    )
    def test_a_static_entry_reads_its_own_row_instead_of_a_hardcoded_null(
        self, mock_auth
    ):
        """静态条目的 `last_run_at` / `total_run_count` / `enabled` 取自库里**那一行**。

        原先写死成 `None / 0 / True`，代价不是「少了个数」：`beat_schedule` 只在 beat
        启动时合并进 `PeriodicTask` 表一次，所以「文件里声明了、调度器从没收到过它」与
        「收下了、只是还没跑」在这个接口上完全一样（两边的 `last_run_at` 都是 null）。
        这两件事的下一步动作不同——前者要重启 beat，后者只要等。`scheduled` 就是那个区别。
        """
        from celery_app import app as celery_app
        from django_celery_beat.models import IntervalSchedule, PeriodicTask
        from rest_framework.test import APIRequestFactory

        from apps.agent.views import list_scheduled_tasks

        request = APIRequestFactory().get("/api/agent/tasks/scheduled/")
        static = [
            t
            for t in list_scheduled_tasks(request).data["tasks"]
            if t["source"] == "static"
        ]
        self.assertTrue(static, "静态条目一条都没有：本用例的前提不成立")

        # 表是空的（beat 从没起来过）⇒ 每一条静态条目都必须被标成「没被调度器收下」。
        self.assertTrue(all(t["scheduled"] is False for t in static))

        name = static[0]["name"]
        PeriodicTask.objects.create(
            name=name,
            task=celery_app.conf.beat_schedule[name]["task"],
            interval=IntervalSchedule.objects.create(every=30, period="seconds"),
            last_run_at=datetime.now(timezone.utc) - timedelta(minutes=5),
            total_run_count=7,
        )

        row = next(
            t for t in list_scheduled_tasks(request).data["tasks"] if t["name"] == name
        )
        self.assertTrue(row["scheduled"])
        self.assertEqual(row["total_run_count"], 7)
        self.assertIsNotNone(row["last_run_at"], "库里那一行有值，接口不该回 null")

    @patch(
        "rest_framework.permissions.IsAuthenticated.has_permission", return_value=True
    )
    def test_a_disabled_static_entry_does_not_claim_to_be_enabled(self, mock_auth):
        """`enabled` 那一格原先也写死成 `True`：一条**被人停用的**静态任务照旧显示为正常。

        顺带记一条库的行为：`PeriodicTask.save()` 对停用行会把 `last_run_at` 强制清成
        null（`if not self.enabled: self.last_run_at = None`），所以「停用」与「从未跑过」
        在库里本来就分不开——分开它们的是 `enabled` 这一格本身。
        """
        from celery_app import app as celery_app
        from django_celery_beat.models import IntervalSchedule, PeriodicTask
        from rest_framework.test import APIRequestFactory

        from apps.agent.views import list_scheduled_tasks

        request = APIRequestFactory().get("/api/agent/tasks/scheduled/")
        static = [
            t
            for t in list_scheduled_tasks(request).data["tasks"]
            if t["source"] == "static"
        ]
        self.assertTrue(static, "静态条目一条都没有：本用例的前提不成立")

        name = static[0]["name"]
        PeriodicTask.objects.create(
            name=name,
            task=celery_app.conf.beat_schedule[name]["task"],
            interval=IntervalSchedule.objects.create(every=30, period="seconds"),
            enabled=False,
        )

        row = next(
            t for t in list_scheduled_tasks(request).data["tasks"] if t["name"] == name
        )
        self.assertFalse(row["enabled"])
        self.assertTrue(row["scheduled"], "行在表里：停用不等于没被调度器收下")


class TestScheduledTaskIntegration(TestCase):
    """Integration tests for the full scheduled task lifecycle."""

    def test_cas_double_acquire_only_one_wins(self):
        """Only one CAS should succeed (idempency guard)."""
        from apps.agent.models import ScheduledOneTimeTask
        from apps.agent.tasks import _update_task_status

        task = ScheduledOneTimeTask.objects.create(
            task_name="cas_double",
            agent_name="analyst",
            message="test",
            run_at=datetime.now(timezone.utc),
            status="pending",
        )
        r1 = _update_task_status(str(task.id), "running")
        r2 = _update_task_status(str(task.id), "running")
        self.assertEqual(r1 + r2, 1)
        self.assertEqual(r1, 1)
        self.assertEqual(r2, 0)


class TestRunAtParsingTimezone(TestCase):
    """Test _parse_run_at timezone handling."""

    def test_tomorrow_is_interpreted_as_beijing(self):
        """tomorrow HH:MM 是用户口语时间，按业务时区（北京时间）解释。

        原实现拿 UTC 的 now() 去 replace(hour=9)，于是「tomorrow 09:00」实际落在
        北京时间 17:00。详细回归见 test_schedule_timezone.py。
        """
        from apps.agent.tools.schedule_task import SubmitScheduledTaskTool
        from apps.common.time_utils import business_tz_name

        tool = SubmitScheduledTaskTool()
        result = tool._parse_run_at("tomorrow 09:00")

        self.assertIsNotNone(result)
        # 返回的就是业务时区时刻
        self.assertEqual(getattr(result.tzinfo, "key", None), business_tz_name())
        self.assertEqual((result.hour, result.minute), (9, 0))
        # 北京 09:00 == UTC 01:00；修复前这里会是 UTC 09:00
        self.assertEqual(result.astimezone(timezone.utc).hour, 1)
        # 且必须落在「明天」（按业务时区算）
        self.assertEqual(
            result.date(), datetime.now(result.tzinfo).date() + timedelta(days=1)
        )
