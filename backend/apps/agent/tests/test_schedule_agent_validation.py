"""定时任务的目标 Agent 必须在**写库之前**校验（2026-09-28 平仓指令事故）。

事故形态（真实取证）：用户说「平掉当前BNB的仓位」，supervisor 给
`submit_scheduled_task` 填了 `agent_name='trader'`。工具只校验了非空，于是：

  1. 落一行 `ScheduledOneTimeTask(status='pending')`
  2. `apply_async(eta=...)` 排到 1 分钟后
  3. 用户收到「已提交 BNB/USDT 平仓指令，任务将在约 1 分钟后执行」
  4. 到期 `AgentRegistry.get('trader')` 抛 `KeyError: Agent not registered: 'trader'`
  5. `_route_to_agent` 的宽 except 兜住 → `AgentResult(success=False, error=...)`
  6. `execute_scheduled_agent_task` → `tracker.fail(...)` → Telegram 上一条 ❌

**系统里从来没有 trader 这个 Agent**（Prompt 目录只发现 analyst / coach / quant /
researcher / risk_advisor，加上不在注册中心里的 supervisor），也没有任何下单/平仓工具，
所以那个任务从落库那一刻起就注定失败——而用户先看到的是一条确认。

本文件钉死两条契约：
  1. 名单之外的 agent_name **一律当场拒**，不落库、不投 Celery（失败要回到 LLM 的
     上下文里，它才有机会如实告诉用户「没有这个能力」）
  2. 名单必须来自 `AgentRegistry`（Prompt 目录发现），不另立常量——另立一份名单迟早
     与目录分家，而分家的表现正是这次事故。`supervisor` 必须在内（`_route_to_agent`
     认它，它只是不在注册中心的 `_classes` 里）。
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from apps.agent.models import ScheduledOneTimeTask
from apps.agent.tools.schedule_task import (
    SubmitRecurringTaskTool,
    SubmitScheduledTaskTool,
    _routable_agent_names,
)

pytestmark = pytest.mark.django_db


def _run(coro):
    return asyncio.run(coro)


def _sync_db_async(fn):
    """把工具的 `db_async` 换成同线程同步执行（只在「合法名字放行」那条用）。

    真的 `db_async` 走 `sync_to_async(thread_sensitive=False)`，落库发生在**另一条线程**
    的连接上：那条连接不参与本用例的外层 atomic，`TestCase` 的隔离托不住它——
    它把一行任务写进真实表，并在线程退出后被 `close_old_connections()` 放回池子，
    于是**后续用例**会看到这一行（实测：同文件里 `TestListScheduledTasksAPI` 拿到
    2 条 `source='one_time'` 而非 1 条）。

    这条用例要钉的是「合法名字不被误拒」，不是 `db_async` 的线程语义，所以在这里
    用同步垫片把写入收回本线程的 atomic 里；`db_async` 自身的接线由
    `test_scheduled_task_persistence.py` 覆盖。
    """

    async def _call(*args, **kwargs):
        return fn(*args, **kwargs)

    return _call


class TestRoutableNames:
    """名单取自注册中心，且必须含 supervisor。"""

    def test_names_match_registry_plus_supervisor(self):
        from apps.agent.registry import AgentRegistry

        names = _routable_agent_names()
        assert set(AgentRegistry.all_names()).issubset(set(names))
        assert "supervisor" in names
        # 事故里的那个名字不在名单里——它是这次修复的锚点。
        assert "trader" not in names
        # 名单是排序过的，直接进给 LLM 的 schema 描述，顺序稳定便于对比。
        assert names == sorted(names)


class TestSubmitScheduledTaskRejectsUnknownAgent:
    """核心回归：未知 agent_name 在写库之前被拒。"""

    @patch("apps.agent.tasks.execute_scheduled_agent_task.apply_async")
    def test_trader_is_rejected_without_side_effects(self, mock_apply):
        """事故原样重放：agent_name='trader' 既不落库也不投 Celery。"""
        result = _run(
            SubmitScheduledTaskTool().execute(
                agent_name="trader",
                message="平掉当前BNB的仓位",
                run_at="now+1m",
                user_id="u1",
            )
        )

        assert result.success is False
        # 报错必须点名「这个名字不存在」并给出可用名单，否则 LLM 无法自我纠正
        assert "trader" in result.error
        assert "analyst" in result.error
        assert "supervisor" in result.error
        # 零副作用：没有排期、没有库行
        mock_apply.assert_not_called()
        assert ScheduledOneTimeTask.objects.filter(agent_name="trader").count() == 0

    @patch("apps.agent.tasks.execute_scheduled_agent_task.apply_async")
    def test_valid_agent_still_goes_through(self, mock_apply):
        """校验不能误伤合法名字（含 supervisor，「平仓」类请求最终会落到它）。"""
        mock_apply.return_value = MagicMock(id="celery-ok")
        with patch(
            "apps.agent.tools.schedule_task.db_async", side_effect=_sync_db_async
        ):
            result = _run(
                SubmitScheduledTaskTool().execute(
                    agent_name="supervisor",
                    message="看一下当前持仓",
                    run_at="now+5m",
                    user_id="u1",
                )
            )

        assert result.success is True, result.error
        mock_apply.assert_called_once()
        assert ScheduledOneTimeTask.objects.filter(id=result.data["schedule_id"]).exists()
        # 这一行必须落在本用例的外层 atomic 里，随用例回滚——**不能**留在真实表上。
        #
        # 曾经这里打的是真的 `db_async`：`sync_to_async(thread_sensitive=False)` 让写入
        # 发生在另一条线程的连接上，那条连接不参与本用例的 atomic，于是这行任务真的落盘，
        # 并且在线程退出被 `close_old_connections()` 放回池子后再被别的用例读到——实测是同
        # 文件里 `test_scheduled_task_persistence.py::TestListScheduledTasksAPI` 拿到 2 条
        # `source='one_time'` 而非 1 条（本文件把它打成 F 也一样看得见，说明回滚根本没兜住）。
        # 所以 `db_async` 在本用例里被换成同线程同步垫片；它的线程语义由
        # `test_scheduled_task_persistence.py::TestSubmitScheduledTaskToolPersistence` 覆盖。
        assert (
            ScheduledOneTimeTask.objects.using("default")
            .filter(id=result.data["schedule_id"])
            .count()
            == 1
        )

    def test_schema_description_offers_the_real_names(self):
        """工具描述里给 LLM 的名单必须是真的（原先只举了 4 个例子，漏了 supervisor）。"""
        desc = SubmitScheduledTaskTool().parameters_schema["properties"]["agent_name"][
            "description"
        ]
        for name in _routable_agent_names():
            assert name in desc


class TestSubmitRecurringTaskRejectsUnknownAgent:
    """周期任务一旦注册就会**反复**触发，名字写错不是失败一次。"""

    def test_unknown_agent_is_rejected(self):
        result = _run(
            SubmitRecurringTaskTool().execute(
                agent_name="trader",
                message="每天检查仓位",
                cron_expression="0 9 * * *",
                task_name="daily_position_check",
                user_id="u1",
            )
        )

        assert result.success is False
        assert "trader" in result.error
        from django_celery_beat.models import PeriodicTask

        assert PeriodicTask.objects.filter(name="daily_position_check").count() == 0

    def test_unknown_workflow_step_agent_is_rejected(self):
        """步骤名写错要在注册之前拦下——步骤是串行的，前几步已经真的跑过了。"""
        result = _run(
            SubmitRecurringTaskTool().execute(
                agent_name="supervisor",
                message="每小时研究并回测",
                cron_expression="0 * * * *",
                task_name="hourly_research_bad_step",
                user_id="u1",
                steps=[
                    {"agent": "researcher", "message": "研究"},
                    {"agent": "trader", "message": "下单"},
                ],
                summary="研究并回测",
            )
        )

        assert result.success is False
        assert "steps[1].agent" in result.error
        assert "trader" in result.error
        from django_celery_beat.models import PeriodicTask

        assert PeriodicTask.objects.filter(name="hourly_research_bad_step").count() == 0
