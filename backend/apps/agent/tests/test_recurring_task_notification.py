"""回归测试：周期任务（celery-beat）执行后必须通知用户。

Bug 背景（与 test_scheduled_task_notification.py 同类，2026-09-20 取证）：
  execute_recurring_agent_task 从未创建 TaskTracker ——
    - 带 steps 的周期 workflow 只落一条 web Notification（_record_workflow），
      主通道（Telegram/Lark）收不到任何结果；
    - 不带 steps 的单 Agent 周期任务连这条 web 通知都没有，全程静默执行。
  用户「每天10点全面分析加密货币市场行情并发送结果」因此每次都等不到结果。

修复方案：
  在 execute_recurring_agent_task 中集成 TaskTracker（对齐一次性定时任务的修复），
  由 tracker.complete() / tracker.fail() 把结果推送到 web + 主通道。

测试策略：
  1. Mock SupervisorAgent 执行，检查 Redis task:progress 记录是否存在
  2. 验证 _send_notification_redis 被调用、内容用户可读、推送对象是通道侧 user_id
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from apps.agent.base import AgentResult
from apps.agent.tasks import (
    _result_summary_for_user,
    execute_recurring_agent_task,
)

# 真实的周期任务 kwargs 里 user_id 是**通道侧** id（Telegram numeric / Feishu open_id），
# 不是 Django UUID。测试沿用这个形状，才能暴露「误用 resolved_user_id 推送」的回归。
CHANNEL_USER_ID = "123456789"


def _redis_client():
    """按 TaskTracker 相同的规则定位 Redis DB3。"""
    import redis
    from django.conf import settings

    url = settings.REDIS_URL
    if url.rsplit("/", 1)[-1].isdigit():
        url = url.rsplit("/", 1)[0] + "/3"
    return redis.from_url(url, decode_responses=True)


def _drop_tracker_key(task_name: str) -> None:
    _redis_client().delete(f"task:progress:recurring-{task_name}")


def _supervisor_patch(method: str, result: AgentResult):
    """让 SupervisorAgent.get_instance() 返回一个指定方法的 mock。"""
    mock_supervisor = MagicMock()
    setattr(mock_supervisor, method, AsyncMock(return_value=result))
    return patch(
        "apps.agent.supervisor.SupervisorAgent.get_instance",
        return_value=mock_supervisor,
    )


@pytest.mark.django_db(transaction=True)
class TestRecurringTaskNotification:
    """周期任务通知机制"""

    def test_recurring_workflow_writes_tracker_record(self):
        """周期 workflow 执行后应留下 TaskTracker 记录（此前完全没有）。"""
        task_name = "rec_tracker_workflow"
        mock_result = AgentResult(
            task_id="rec-1",
            success=True,
            data={
                "workflow_id": "wf-1",
                "workflow_summary": "工作流完成 (1/1 步, 12.0s)",
                "step_results": [],
                "elapsed_seconds": 12.0,
            },
        )

        try:
            with _supervisor_patch("_execute_workflow", mock_result):
                result = execute_recurring_agent_task(
                    agent_name="supervisor",
                    message="每天10点分析行情",
                    user_id=CHANNEL_USER_ID,
                    task_name=task_name,
                    workflow_steps=[{"agent": "analyst", "message": "分析行情"}],
                    workflow_summary="每天10点分析行情",
                )

            assert result["status"] == "SUCCESS"

            data = _redis_client().hgetall(f"task:progress:recurring-{task_name}")
            assert data, "TaskTracker should create Redis progress record"
            assert data.get("status") == "completed"
            assert data.get("user_id") == CHANNEL_USER_ID
        finally:
            _drop_tracker_key(task_name)

    def test_recurring_workflow_result_is_readable_and_channel_addressed(self):
        """完成通知内容应是 workflow_summary，且推送对象是通道侧 user_id。"""
        task_name = "rec_notify_workflow"
        mock_result = AgentResult(
            task_id="rec-2",
            success=True,
            data={
                "workflow_id": "wf-2",
                "workflow_summary": "BTC 现价 63000，趋势偏多",
                "step_results": [{"step": 1, "agent": "analyst"}],
            },
        )

        try:
            with patch(
                "apps.agent.task_tracker._send_notification_redis"
            ) as mock_notify:
                with _supervisor_patch("_execute_workflow", mock_result):
                    execute_recurring_agent_task(
                        agent_name="supervisor",
                        message="每天10点分析行情",
                        user_id=CHANNEL_USER_ID,
                        task_name=task_name,
                        workflow_steps=[{"agent": "analyst", "message": "分析行情"}],
                        workflow_summary="每天10点分析行情",
                    )

            assert mock_notify.called, "周期任务完成后必须推送通知"

            notify_user_id, notify_text = mock_notify.call_args[0]
            assert notify_user_id == CHANNEL_USER_ID
            assert "完成" in notify_text
            assert "BTC 现价 63000，趋势偏多" in notify_text
            assert "workflow_id" not in notify_text, (
                "不应把 data 的整份 dict repr 推给用户"
            )
        finally:
            _drop_tracker_key(task_name)

    def test_recurring_single_agent_failure_notifies_user(self):
        """单 Agent 周期任务失败时必须通知（此前全程静默）。"""
        task_name = "rec_fail_single"
        mock_result = AgentResult(
            task_id="rec-3",
            success=False,
            error="行情数据源不可用",
        )

        try:
            with patch(
                "apps.agent.task_tracker._send_notification_redis"
            ) as mock_notify:
                with _supervisor_patch("handle", mock_result):
                    result = execute_recurring_agent_task(
                        agent_name="analyst",
                        message="每天10点分析行情",
                        user_id=CHANNEL_USER_ID,
                        task_name=task_name,
                    )

            assert result["status"] == "FAILURE"
            assert mock_notify.called, "周期任务失败后必须推送通知"

            notify_user_id, notify_text = mock_notify.call_args[0]
            assert notify_user_id == CHANNEL_USER_ID
            assert "失败" in notify_text
            assert "行情数据源不可用" in notify_text
        finally:
            _drop_tracker_key(task_name)

    def test_recurring_task_exception_notifies_user(self):
        """执行抛异常（非 AgentResult 失败）也要通知，不能静默。"""
        task_name = "rec_exc_single"

        try:
            with patch(
                "apps.agent.task_tracker._send_notification_redis"
            ) as mock_notify:
                with patch(
                    "apps.agent.supervisor.SupervisorAgent.get_instance",
                    side_effect=RuntimeError("worker 挂了"),
                ):
                    result = execute_recurring_agent_task(
                        agent_name="analyst",
                        message="每天10点分析行情",
                        user_id=CHANNEL_USER_ID,
                        task_name=task_name,
                    )

            assert result["status"] == "ERROR"
            assert mock_notify.called, "周期任务异常后必须推送通知"
            assert "worker 挂了" in mock_notify.call_args[0][1]
        finally:
            _drop_tracker_key(task_name)

    def test_recurring_task_never_pushes_to_scheduler_user(self):
        """无用户上下文的周期任务不能把结果推给 system_scheduler。

        `_resolve_scheduler_user_id("")` 会回退到 system_scheduler 这个 Django UUID，
        那是「系统自己」，不是任何真实用户。曾经差点被当作通知目标 ——
        结果是随机某个账号收到别人的行情分析。
        """
        from django.contrib.auth import get_user_model

        scheduler = get_user_model().objects.create_user(
            email="scheduler@example.com",
            username="system_scheduler",
            password="x",
        )
        task_name = "rec_no_user"
        mock_result = AgentResult(
            task_id="rec-5", success=True, data={"content": "结论"}
        )

        try:
            with patch(
                "apps.agent.task_tracker._send_notification_redis"
            ) as mock_notify:
                with _supervisor_patch("handle", mock_result):
                    execute_recurring_agent_task(
                        agent_name="analyst",
                        message="每天10点分析行情",
                        user_id="",  # 无用户上下文
                        task_name=task_name,
                    )

            assert mock_notify.called
            for call in mock_notify.call_args_list:
                assert call[0][0] != str(scheduler.id), (
                    "不得把周期任务结果推给 system_scheduler 系统账号"
                )
        finally:
            _drop_tracker_key(task_name)


class TestResultSummaryForUser:
    """_result_summary_for_user 的压缩规则"""

    def test_prefers_workflow_summary(self):
        result = AgentResult(
            task_id="t",
            success=True,
            data={
                "workflow_id": "wf",
                "workflow_summary": "结论 A",
                "step_results": [],
            },
        )
        assert _result_summary_for_user(result) == "结论 A"

    def test_prefers_content_for_single_agent(self):
        result = AgentResult(task_id="t", success=True, data={"content": "结论 B"})
        assert _result_summary_for_user(result) == "结论 B"

    def test_plain_string_passthrough(self):
        result = AgentResult(task_id="t", success=True, data="结论 C")
        assert _result_summary_for_user(result) == "结论 C"

    def test_empty_data_falls_back(self):
        result = AgentResult(task_id="t", success=True, data=None)
        assert _result_summary_for_user(result) == "任务完成"

    def test_unknown_dict_is_truncated(self):
        """未知结构不应把整份 repr 塞进通知。"""
        result = AgentResult(
            task_id="t", success=True, data={"step_results": [{"x": "y" * 2000}]}
        )
        assert len(_result_summary_for_user(result)) == 500
