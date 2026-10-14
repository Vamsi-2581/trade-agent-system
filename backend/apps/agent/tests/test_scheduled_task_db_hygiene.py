"""P1 回归：celery 定时任务的 DB 连接卫生（入口/出口 close_all）。

事故（2026-09-18，定时任务 6e2d7793）：
- celery worker 的 Django DB 连接在 LLM 长生成期间处于 idle-in-transaction，
  恰好 30s 后被 PG 的 idle_in_transaction_session_timeout=30000ms 杀掉；
- 06:23:40 任务开始 → 06:24:10 notify_user 写库 "server closed the
  connection unexpectedly" → 最终报告未投递 + LLM 回退输出虚构指标。

修复（本文件覆盖）：
任务入口丢弃陈旧连接（close_all），任务出口（finally）同样丢弃，
保证每个 celery 任务都以全新连接开始，且不把坏连接留给下一个任务。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch


import apps.agent.tasks as tasks_mod
from apps.agent.base import AgentResult


def _call_task():
    # celery task 由框架绑定实例作为 self，直接传 4 个业务参数
    return tasks_mod.execute_scheduled_agent_task(
        "quant",
        "查询回测结果",
        "user1",
        "sched-0001",
    )


def test_entry_closes_stale_connections():
    """入口 close_all：即使 CAS 失败提前返回，也必须先丢弃陈旧连接。"""
    with (
        patch.object(tasks_mod, "_update_task_status", return_value=0),
        patch("django.db.connections.close_all") as close_all,
    ):
        result = _call_task()

    assert result["status"] == "SKIPPED"
    assert close_all.call_count >= 1


def test_finally_closes_connections_after_success(monkeypatch):
    """核心回归：成功路径 finally 必须 close_all（出口不留坏连接）。"""
    stub_supervisor = MagicMock()
    stub_supervisor._route_to_agent = AsyncMock(
        return_value=AgentResult(success=True, data={"r": 1})
    )

    class _StubTracker:
        def __init__(self, *a, **k):
            pass

        def start(self, *a, **k):
            pass

        def complete(self, *a, **k):
            pass

        def fail(self, *a, **k):
            pass

        def stop(self, *a, **k):
            pass

        def alive(self, *a, **k):
            pass

        def tool_call(self, *a, **k):
            pass

        def set_origin(self, *a, **k):
            pass

    finalize = MagicMock()
    monkeypatch.setattr(tasks_mod, "_update_task_status", lambda _id, _s: 1)
    monkeypatch.setattr(tasks_mod, "_finalize_task", finalize)
    monkeypatch.setattr(tasks_mod, "_session_manager_reset", lambda: None)
    monkeypatch.setattr("apps.agent.task_tracker.TaskTracker", _StubTracker)
    monkeypatch.setattr(
        "apps.agent.supervisor.SupervisorAgent.get_instance",
        classmethod(lambda cls: stub_supervisor),
    )

    with patch("django.db.connections.close_all") as close_all:
        result = _call_task()

    assert result["status"] == "SUCCESS"
    # tasks.py 将 result 存为 str(result.data)
    assert result["result"] == str({"r": 1})
    # 入口 + finally 至少各一次
    assert close_all.call_count >= 2
    finalize.assert_called_once()
    assert finalize.call_args.args[0] == "sched-0001"
    assert finalize.call_args.args[1] == "SUCCESS"
    stub_supervisor._route_to_agent.assert_awaited_once()
