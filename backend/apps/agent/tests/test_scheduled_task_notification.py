"""
回归测试：验证定时任务执行完成后会通知用户

Bug 背景：
  execute_scheduled_agent_task 之前没有使用 TaskTracker，
  导致定时任务完成后用户不会收到通知。

修复方案：
  在 execute_scheduled_agent_task 中集成 TaskTracker，
  使用 tracker.complete() / tracker.fail() 推送通知。

测试策略：
  1. Mock execute_scheduled_agent_task 执行
  2. 检查 Redis task:progress 记录是否存在
  3. 验证 _send_notification_redis 是否被调用
"""

import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch, AsyncMock

from django.contrib.auth import get_user_model

from apps.agent.models import ScheduledOneTimeTask
from apps.agent.tasks import execute_scheduled_agent_task


@pytest.mark.django_db(transaction=True)
class TestScheduledTaskNotification:
    """测试定时任务通知机制"""

    def test_execute_scheduled_agent_task_creates_tracker_record(self):
        """
        验证：execute_scheduled_agent_task 应该创建 TaskTracker Redis 记录

        期望结果：
          - Redis task:progress:{task_id} 存在
          - status = "completed" 或 "failed"
          - user_id 正确记录
        """
        # 创建测试用户
        User = get_user_model()
        test_user = User.objects.create_user(
            email="tracker_record@test.local",
            username="test_notification_user",
            password="pw12345",
        )
        user_id = str(test_user.id)

        # 创建定时任务记录
        scheduled_task = ScheduledOneTimeTask.objects.create(
            task_name="test_tracker_integration",
            agent_name="analyst",
            message="测试 TaskTracker 集成",
            user_id=user_id,
            run_at=datetime.now(timezone.utc),
            status="pending",
        )
        scheduled_task_id = str(scheduled_task.id)

        # Mock supervisor._route_to_agent 返回成功结果
        from apps.agent.base import AgentResult

        mock_result = AgentResult(
            task_id="test-task-id",
            success=True,
            data={"content": "测试完成"},
        )

        with patch(
            "apps.agent.supervisor.SupervisorAgent.get_instance"
        ) as mock_supervisor_cls:
            mock_supervisor = MagicMock()
            mock_supervisor._route_to_agent = AsyncMock(return_value=mock_result)
            mock_supervisor_cls.return_value = mock_supervisor

            # 执行任务
            result = execute_scheduled_agent_task(
                agent_name="analyst",
                message="测试 TaskTracker 集成",
                user_id=user_id,
                scheduled_task_id=scheduled_task_id,
            )

        # 验证结果
        assert result["status"] == "SUCCESS"

        # 检查 Redis task:progress 记录
        import redis
        from django.conf import settings

        url = settings.REDIS_URL
        if url.rsplit("/", 1)[-1].isdigit():
            url = url.rsplit("/", 1)[0] + "/3"

        r = redis.from_url(url, decode_responses=True)

        # 查找最近的 task:progress 记录（通过 user_id）
        found_tracker_record = False
        for key in r.scan_iter("task:progress:*"):
            data = r.hgetall(key)
            if data.get("user_id") == user_id:
                found_tracker_record = True
                assert data.get("status") in ("completed", "running")
                break

        assert found_tracker_record, "TaskTracker should create Redis progress record"

        # 清理
        scheduled_task.delete()

    def test_execute_scheduled_agent_task_sends_notification(self):
        """
        验证：execute_scheduled_agent_task 应该推送通知

        期望结果：
          - _send_notification_redis 被调用
          - 通知内容包含任务结果
        """
        User = get_user_model()
        test_user = User.objects.create_user(
            email="notification_send@test.local",
            username="test_notification_send",
            password="pw12345",
        )
        user_id = str(test_user.id)

        scheduled_task = ScheduledOneTimeTask.objects.create(
            task_name="test_notification_send",
            agent_name="analyst",
            message="测试通知推送",
            user_id=user_id,
            run_at=datetime.now(timezone.utc),
            status="pending",
        )

        from apps.agent.base import AgentResult

        mock_result = AgentResult(
            task_id="test-task-id",
            success=True,
            data={"content": "测试完成，收益率 15%"},
        )

        # Mock _send_notification_redis 检查是否被调用
        with patch("apps.agent.task_tracker._send_notification_redis") as mock_notify:
            with patch(
                "apps.agent.supervisor.SupervisorAgent.get_instance"
            ) as mock_supervisor_cls:
                mock_supervisor = MagicMock()
                mock_supervisor._route_to_agent = AsyncMock(return_value=mock_result)
                mock_supervisor_cls.return_value = mock_supervisor

                _result = execute_scheduled_agent_task(
                    agent_name="analyst",
                    message="测试通知推送",
                    user_id=user_id,
                    scheduled_task_id=str(scheduled_task.id),
                )

            # 验证通知被推送
            assert mock_notify.called, (
                "TaskTracker should send notification on completion"
            )

            # 检查通知内容包含 "完成" 或 "成功"
            call_args = mock_notify.call_args
            if call_args:
                notification_text = call_args[0][1]  # 第二个参数是通知内容
                assert "完成" in notification_text or "成功" in notification_text

        # 清理
        scheduled_task.delete()

    def test_execute_scheduled_agent_task_failure_sends_notification(self):
        """
        验证：任务失败时也应该推送通知

        期望结果：
          - tracker.fail() 被调用
          - 通知内容包含错误信息
        """
        User = get_user_model()
        test_user = User.objects.create_user(
            email="notification_fail@test.local",
            username="test_notification_fail",
            password="pw12345",
        )
        user_id = str(test_user.id)

        scheduled_task = ScheduledOneTimeTask.objects.create(
            task_name="test_notification_fail",
            agent_name="analyst",
            message="测试失败通知",
            user_id=user_id,
            run_at=datetime.now(timezone.utc),
            status="pending",
        )

        from apps.agent.base import AgentResult

        mock_result = AgentResult(
            task_id="test-task-id",
            success=False,
            error="测试错误：模拟失败场景",
        )

        with patch("apps.agent.task_tracker._send_notification_redis") as mock_notify:
            with patch(
                "apps.agent.supervisor.SupervisorAgent.get_instance"
            ) as mock_supervisor_cls:
                mock_supervisor = MagicMock()
                mock_supervisor._route_to_agent = AsyncMock(return_value=mock_result)
                mock_supervisor_cls.return_value = mock_supervisor

                _result = execute_scheduled_agent_task(
                    agent_name="analyst",
                    message="测试失败通知",
                    user_id=user_id,
                    scheduled_task_id=str(scheduled_task.id),
                )

            # 验证通知被推送（失败时也应该通知）
            assert mock_notify.called, "TaskTracker should send notification on failure"

            # 检查通知内容包含 "失败" 或 "错误"
            call_args = mock_notify.call_args
            if call_args:
                notification_text = call_args[0][1]
                assert "失败" in notification_text or "错误" in notification_text

        # 清理
        scheduled_task.delete()
