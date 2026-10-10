"""定时任务工具 — 向系统提交定时任务，定时给不同 Agent 派发任务。

设计原则：
  - 一次性定时任务：使用 Celery apply_async(eta=...) 在指定时间执行
  - 周期性定时任务：使用 django-celery-beat 数据库存储，celery-beat 自动读取
  - 任务内容会路由到指定的 SubAgent 执行
  - Agent 通过两步完成：
    1. submit_scheduled_task → 立即返回 task_id 或 schedule_id
    2. get_task_result       → 查询任务状态 / 结果
"""

from __future__ import annotations

import logging
from datetime import datetime

from celery_app import app as celery_app

from .base import BaseTool, ToolResult
from apps.common.time_utils import (
    business_now,
    business_tz_label,
    business_tz_name,
    format_business,
    to_business,
)
from apps.core.db_utils import db_async

logger = logging.getLogger(__name__)


def _routable_agent_names() -> list[str]:
    """当前真正可路由的 Agent 名单（Prompt 发现的那批 + supervisor）。

    向 `AgentRegistry` 取，不另立常量：`AgentRegistry.get()` 只认 Prompt 目录扫描出的
    `_classes`，另立一份名单迟早与目录分家，而分家的表现正是 2026-09-28 那次事故——
    「提交时说已提交、到期时 Agent not registered」。

    `supervisor` 不在注册中心里（它由 `SupervisorAgent.get_instance()` 直达），
    但 `_route_to_agent` 认它，所以它必须出现在名单里。
    """
    from apps.agent.registry import AgentRegistry

    names = set(AgentRegistry.all_names())
    names.add("supervisor")
    return sorted(names)


def _validate_agent_name(raw: str, *, field: str = "agent_name") -> str:
    """校验目标 Agent 是否真的可路由，可路由返回空串，否则返回给 LLM 的错误消息。

    2026-09-28 事故：supervisor 给「平掉当前BNB的仓位」填了 `agent_name='trader'`，
    本工具照单落库并投递 Celery，一分钟到期时 `AgentRegistry.get('trader')` 才抛
    `Agent not registered: 'trader'`。用户先收到「已提交平仓指令」的确认，再收到一条
    迟到的失败——而系统里根本没有下单/平仓能力，那个任务从落库那一刻起就注定失败。

    所以校验必须发生在**写库之前**：失败立刻回到 LLM 的上下文（工具错误会作为
    `Error: ...` 进入下一轮），它才有机会如实告诉用户，而不是先确认再报错。

    名单为空时**不放行**：那说明 Prompt 目录没部署，此刻没有任何名字能真的跑起来，
    与其把注定失败的任务写进库，不如当场说清楚。
    """
    names = _routable_agent_names()
    if not names:
        return (
            f"无法校验 {field}={raw!r}：没有发现任何可路由的 Agent"
            "（Prompt 目录未部署？）。为避免提交一个到点必然失败的任务，本次不提交。"
        )
    if raw in names:
        return ""
    return (
        f"{field}={raw!r} 不是已注册的 Agent，任务到点后必然失败（Agent not registered）。"
        f"可用的 Agent：{'、'.join(names)}。"
    )


def _validate_workflow_steps(steps: list) -> str:
    """校验多步骤 workflow 里每一步的 `agent`，返回第一条错误（无错返回空串）。

    与 `agent_name` 同一类失败、同一类代价：步骤是**串行**的，第 3 步的名字写错，
    前两步已经真的跑完并落了副作用，然后整条 workflow 才失败。
    """
    for idx, step in enumerate(steps or []):
        if not isinstance(step, dict):
            return f"steps[{idx}] 不是对象：{step!r}"
        err = _validate_agent_name(
            str(step.get("agent", "") or ""), field=f"steps[{idx}].agent"
        )
        if err:
            return err
    return ""


def _format_beat_schedule(row: dict) -> str:
    """把 PeriodicTask 的 values() 行格式化成可读的调度描述（用于取消审计）。"""
    if row.get("crontab__minute") is not None:
        tz = row.get("crontab__timezone") or "UTC"
        return (
            f"crontab {row.get('crontab__minute')} {row.get('crontab__hour')} "
            f"{row.get('crontab__day_of_month')} {row.get('crontab__month_of_year')} "
            f"{row.get('crontab__day_of_week')}（时区 {tz}）"
        )
    if row.get("interval__every"):
        return f"interval every {row.get('interval__every')} {row.get('interval__period')}"
    return "N/A"


class SubmitScheduledTaskTool(BaseTool):
    """
    提交一次性定时任务，在指定时间点触发 Agent 任务。
    """

    name = "submit_scheduled_task"
    description = (
        "提交定时任务到系统，在指定时间自动执行。任务会在指定时间点路由到指定 Agent 处理。"
        "立即返回 schedule_id，使用 get_task_result 查询执行状态和结果。"
    )

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "agent_name": {
                    "type": "string",
                    "description": (
                        "目标 Agent 名称，**只能从这个名单里选**："
                        + "、".join(_routable_agent_names())
                        + "。名单之外的名字（如 trader）不存在，提交会当场被拒。"
                    ),
                },
                "message": {
                    "type": "string",
                    "description": "要发送给 Agent 的任务内容/消息",
                },
                "run_at": {
                    "type": "string",
                    "description": (
                        "执行时间。支持 ISO 格式（带时区，如 2026-04-19T09:00:00+08:00）、"
                        "相对格式（'now+5m'）、口语格式（'tomorrow 09:00'）。"
                        f"**不带时区的时间按北京时间（{business_tz_label()}）解释**。"
                    ),
                },
                "user_id": {
                    "type": "string",
                    "description": "用户 ID（可选），用于关联用户上下文",
                },
            },
            "required": ["agent_name", "message", "run_at"],
        }

    def _parse_run_at(self, run_at: str) -> datetime:
        """解析执行时间，支持 ISO 格式和相对时间表达式。

        时区口径：**不带时区的口语时间（如 `tomorrow 09:00`）按业务时区解释**
        （默认北京时间，见 apps/common/time_utils.py），返回的时间一律是
        aware 的业务时区时间。
        """
        from datetime import timedelta

        run_at = run_at.strip()

        # 相对时间：now+5m, now+1h, now+30s, now+2d
        rel = __import__("re").match(
            r"^now\+(\d+)([smhd])$", run_at, __import__("re").IGNORECASE
        )
        if rel:
            value = int(rel.group(1))
            unit = rel.group(2).lower()
            deltas = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}
            return business_now() + timedelta(**{deltas[unit]: value})

        # 口语时间：tomorrow HH:MM —— 用户说的是他/她的本地时钟（北京时间），
        # 不能拿 UTC 的 now() 去 replace(hour=...)。
        tom_match = __import__("re").match(
            r"^tomorrow\s+(\d{1,2}):(\d{2})$", run_at, __import__("re").IGNORECASE
        )
        if tom_match:
            hour, minute = int(tom_match.group(1)), int(tom_match.group(2))
            from datetime import timedelta

            tomorrow = business_now().replace(
                hour=hour, minute=minute, second=0, microsecond=0
            ) + timedelta(days=1)
            return tomorrow

        # ISO 格式
        try:
            parsed = datetime.fromisoformat(run_at)
        except ValueError:
            parsed = None
        if parsed is not None:
            # naive ISO 同样按业务时区解释；带时区的按同一时刻换算到业务时区
            return to_business(parsed)

        raise ValueError(
            f"无法解析时间格式: {run_at}，支持 ISO 格式或 now+5m、tomorrow 09:00"
        )

    async def execute(
        self,
        agent_name: str = "",
        message: str = "",
        run_at: str = "",
        user_id: str = "",
        **kwargs,
    ) -> ToolResult:
        if not agent_name or not message or not run_at:
            return ToolResult(
                success=False,
                error="agent_name、message、run_at 均为必填项",
            )

        # 写库之前先确认「这个名字到点真的能路由」，否则就是一个注定失败的任务：
        # 用户此刻收到「已提交」，到期才收到失败（2026-09-28 平仓指令事故）。
        name_error = _validate_agent_name(agent_name)
        if name_error:
            logger.warning(
                "[SubmitScheduledTaskTool] rejected unregistered agent_name=%r",
                agent_name,
            )
            return ToolResult(success=False, error=name_error)

        try:
            from apps.agent.models import ScheduledOneTimeTask
            from apps.agent.tasks import execute_scheduled_agent_task

            eta = self._parse_run_at(run_at)

            # Step 1: Create DB record (pending)
            task_record = await db_async(ScheduledOneTimeTask.objects.create)(
                task_name=f"scheduled_{agent_name}",
                agent_name=agent_name,
                message=message,
                user_id=user_id or "",
                run_at=eta,
                status="pending",
            )
            db_uuid = str(task_record.id)

            # Step 2: Push to Celery with eta + scheduled_task_id
            celery_result = execute_scheduled_agent_task.apply_async(
                kwargs={
                    "agent_name": agent_name,
                    "message": message,
                    "user_id": user_id or "",
                    "scheduled_task_id": db_uuid,
                },
                eta=eta,
            )

            # Step 3: Backfill celery_task_id
            task_record.celery_task_id = celery_result.id
            await db_async(task_record.save)(update_fields=["celery_task_id", "updated_at"])

            eta_str = eta.isoformat()
            eta_local = format_business(eta)
            logger.info(
                "[SubmitScheduledTaskTool] scheduled db_id=%s celery_id=%s agent=%s eta=%s",
                db_uuid,
                celery_result.id,
                agent_name,
                eta_str,
            )
            return ToolResult(
                success=True,
                data={
                    "schedule_id": db_uuid,
                    "task_id": db_uuid,
                    "celery_task_id": celery_result.id,
                    "agent_name": agent_name,
                    "run_at": eta_str,
                    "run_at_local": eta_local,
                    "timezone": business_tz_name(),
                    "status": "SCHEDULED",
                    "message": (
                        f"定时任务已提交，schedule_id={db_uuid}，"
                        f"将在 {eta_local} 触发 Agent={agent_name}。"
                        f"使用 get_task_result 查询执行状态。"
                    ),
                },
            )
        except ValueError as e:
            return ToolResult(success=False, error=str(e))
        except Exception as e:
            logger.error(
                "[SubmitScheduledTaskTool] submit failed: %s", e, exc_info=True
            )
            return ToolResult(success=False, error=f"提交定时任务失败: {e}")


class SubmitRecurringTaskTool(BaseTool):
    """
    提交周期性定时任务，通过 django-celery-beat 在数据库层面注册，celery-beat 自动读取。
    """

    name = "submit_recurring_task"
    description = (
        "提交周期性定时任务，按 crontab 表达式重复执行。"
        "任务会持久化到数据库，celery-beat 进程自动读取。"
    )

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "agent_name": {
                    "type": "string",
                    "description": (
                        "目标 Agent 名称，**只能从这个名单里选**："
                        + "、".join(_routable_agent_names())
                        + "。多 Agent 协作任务（带 steps）必须设为 'supervisor'。"
                        "名单之外的名字（如 trader）不存在，提交会当场被拒。"
                    ),
                },
                "message": {
                    "type": "string",
                    "description": "要发送给 Agent 的任务内容/消息",
                },
                "cron_expression": {
                    "type": "string",
                    "description": (
                        "Crontab 表达式，5个字段：分 时 日 月 周。"
                        f"**按北京时间（{business_tz_label()}）解释**，"
                        "例如 '0 9 * * 1-5' 表示工作日北京时间早上9点。"
                    ),
                },
                "task_name": {
                    "type": "string",
                    "description": "定时任务名称（用于标识此任务，如 'daily_btc_analysis'）",
                },
                "user_id": {
                    "type": "string",
                    "description": "用户 ID（可选），用于关联用户上下文",
                },
                "steps": {
                    "type": "array",
                    "description": "多步骤 workflow 的步骤列表。每项包含 {agent: '<agent名>', message: '<步骤指令>'}。当提供 steps 时，agent_name 应设为 'supervisor'",
                    "items": {
                        "type": "object",
                        "properties": {
                            "agent": {"type": "string", "description": "负责此步骤的 Agent 名称"},
                            "message": {"type": "string", "description": "此步骤的执行指令"},
                        },
                        "required": ["agent", "message"],
                    },
                },
                "summary": {
                    "type": "string",
                    "description": "workflow 的一句话概括，如 '每小时研究高胜率策略并回测优化'",
                },
            },
            "required": ["agent_name", "message", "cron_expression", "task_name"],
        }

    @staticmethod
    def _describe_next_run(schedule) -> str:
        """尽力算出「下次触发时间」（业务时区），用于回给用户的确认消息。

        用 `is_due()` 而不是 `remaining_estimate()`：`TzAwareCrontab` 只在 `is_due()`
        里把参考时间 `astimezone(self.tz)`，直接调 `remaining_estimate()` 会拿 UTC 的
        钟点去匹配 crontab，结果少算 8 小时（实测）。

        纯展示用途：crontab 边界情况（例如 2 月 30 号这种永不匹配的表达式）不应
        影响任务注册本身，所以任何异常都吞掉并返回空串。
        """
        try:
            from datetime import timedelta
            from math import ceil

            from django.utils import timezone as djtz

            now = djtz.now()
            _, remaining_secs = schedule.schedule.is_due(now)
            # 向上取整到秒，避免浮点截断把 10:00 显示成 09:59
            return format_business(now + timedelta(seconds=ceil(max(remaining_secs, 0))))
        except Exception:
            logger.warning(
                "[SubmitRecurringTaskTool] 无法推算下次触发时间: %s", schedule, exc_info=True
            )
            return ""

    async def execute(
        self,
        agent_name: str = "",
        message: str = "",
        cron_expression: str = "",
        task_name: str = "",
        user_id: str = "",
        steps: list = None,
        summary: str = "",
        **kwargs,
    ) -> ToolResult:
        if not agent_name or not message or not cron_expression or not task_name:
            return ToolResult(
                success=False,
                error="agent_name、message、cron_expression、task_name 均为必填项",
            )

        parts = cron_expression.strip().split()
        if len(parts) != 5:
            return ToolResult(
                success=False,
                error=f"cron_expression 需要 5 个字段（分 时 日 月 周），当前: {cron_expression}",
            )

        # 与 SubmitScheduledTaskTool 同一道校验、同一条理由：周期任务一旦注册就会
        # **反复**触发，名字写错不是失败一次，而是每天都失败一次并每天推一条 ❌。
        name_error = _validate_agent_name(agent_name)
        if name_error:
            logger.warning(
                "[SubmitRecurringTaskTool] rejected unregistered agent_name=%r",
                agent_name,
            )
            return ToolResult(success=False, error=name_error)

        # workflow 步骤是串行的：名字写错时前面几步已经跑完并落了副作用，
        # 所以也必须在下单（注册）之前拦下来。
        steps_error = _validate_workflow_steps(steps)
        if steps_error:
            logger.warning(
                "[SubmitRecurringTaskTool] rejected invalid workflow step: %s",
                steps_error,
            )
            return ToolResult(success=False, error=steps_error)

        # Workflow tasks: force agent_name to supervisor so supervisor.handle()
        # is called on execution, which detects and runs the workflow steps.
        effective_agent = agent_name
        if steps and len(steps) > 0:
            effective_agent = "supervisor"

        try:
            from django_celery_beat.models import CrontabSchedule, PeriodicTask

            minute, hour, day_of_month, month_of_year, day_of_week = parts

            # 用户在 Telegram 上说「每天10点」指的是自己的本地时钟（北京时间），
            # 而 celery-beat 默认按 UTC 解释 crontab。这里显式带上业务时区，
            # 让 '0 10 * * *' 真的落在北京时间 10:00。
            # 注意：timezone 参与 get_or_create 的查找条件，因此不会复用（也不会
            # 改写）已有 tz=UTC 的调度行 —— 系统自带的几个 crontab 保持原样。
            schedule, _ = await db_async(CrontabSchedule.objects.get_or_create)(
                minute=minute,
                hour=hour,
                day_of_month=day_of_month,
                month_of_year=month_of_year,
                day_of_week=day_of_week,
                timezone=business_tz_name(),
            )
            next_run_desc = self._describe_next_run(schedule)

            import json as _json

            # 周期性定时任务由后台触发，无用户在场，强制 autonomous 模式
            # 跳过 skill 内的所有用户确认环节，全程使用 agent 推荐选项
            autonomous_message = (
                message
                + "\n\n<system_override>\n"
                "autonomous: true — 本次执行由定时任务后台触发，无用户在场。"
                "跳过所有用户确认环节，全程使用 agent 推荐选项，无需人工确认。\n"
                "</system_override>"
            )

            task_kwargs = {
                "agent_name": effective_agent,
                "message": autonomous_message,
                "user_id": user_id or "",
                "task_name": task_name,
            }
            if steps:
                task_kwargs["workflow_steps"] = steps
                task_kwargs["workflow_summary"] = summary

            await db_async(PeriodicTask.objects.update_or_create)(
                name=task_name,
                defaults={
                    "task": "apps.agent.tasks.execute_recurring_agent_task",
                    "crontab": schedule,
                    "kwargs": _json.dumps(task_kwargs),
                },
            )

            logger.info(
                "[SubmitRecurringTaskTool] registered task_name=%s agent=%s cron=%s tz=%s workflow=%s",
                task_name,
                effective_agent,
                cron_expression,
                business_tz_name(),
                "yes(%d steps)" % len(steps) if steps else "no",
            )
            return ToolResult(
                success=True,
                data={
                    "task_name": task_name,
                    "agent_name": effective_agent,
                    "cron_expression": cron_expression,
                    "timezone": business_tz_name(),
                    "timezone_label": business_tz_label(),
                    "next_run_local": next_run_desc,
                    "status": "ACTIVE",
                    "workflow": bool(steps),
                    "message": (
                        f"周期定时任务已注册，task_name={task_name}，"
                        f"Agent={effective_agent}，cron={cron_expression}"
                        f"（时区 {business_tz_label()}，即表达式中的小时数按北京时间计）。"
                        f"{'workflow 包含 %d 个步骤，' % len(steps) if steps else ''}"
                        f"{f'下次触发：{next_run_desc}。' if next_run_desc else ''}"
                        f"任务已持久化到数据库，celery-beat 将按 crontab 定时周期触发。"
                    ),
                },
            )
        except Exception as e:
            logger.error(
                "[SubmitRecurringTaskTool] register failed: %s", e, exc_info=True
            )
            return ToolResult(success=False, error=f"注册周期任务失败: {e}")


class CancelScheduledTaskTool(BaseTool):
    """
    取消已提交的定时任务或周期任务。
    """

    name = "cancel_scheduled_task"
    description = (
        "取消已提交的一次性定时任务或周期性定时任务。"
        "对一次性任务调用 revoke，对周期任务从数据库中移除。"
    )

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "task_id_or_name": {
                    "type": "string",
                    "description": "一次性任务的 task_id（如 xxx-xxx-xxx），或周期任务的 task_name（如 daily_btc_analysis）",
                },
                "task_type": {
                    "type": "string",
                    "description": "任务类型：scheduled=一次性定时任务，recurring=周期性任务",
                    "enum": ["scheduled", "recurring"],
                    "default": "scheduled",
                },
            },
            "required": ["task_id_or_name"],
        }

    async def _record_cancellation(
        self,
        *,
        task_type: str,
        identifier: str,
        detail: str,
        user_id: str = "",
        agent_name: str = "",
    ) -> None:
        """记录一次成功的取消动作（审计 + 用户可见通知）。

        为什么必须有（2026-07-05 事故取证）：周期任务「每小时研究一个高频交易策略」
        从 django_celery_beat_periodictask 中消失后**无任何痕迹可查**——AgentAuditLog
        当时全表 0 行、无 HTTP 删除端点、无持久化日志，事后无法定位谁在何时取消。
        本工具是全库唯一的 PeriodicTask 删除路径，因此取消必须留痕：
          - AgentAuditLog：机器取证，无 user 上下文也写（谁=user 记在 input_summary）
          - Notification：有 user 时额外写入，用户当场可见（带 user + created_at）
        审计失败只记 ERROR，绝不影响取消结果本身。
        """
        try:
            from apps.agent.models import AgentAuditLog

            await db_async(
                lambda: AgentAuditLog.objects.create(
                    agent_type=(agent_name or "system")[:16],
                    action="cancel_scheduled_task",
                    input_summary=(
                        f"type={task_type} task={identifier} user={user_id or '-'}"
                    ),
                    output_summary=f"CANCELLED: {detail}",
                )
            )()

            if user_id:
                from apps.notify.models import Notification

                kind = "周期任务" if task_type == "recurring" else "一次性定时任务"
                await db_async(
                    lambda: Notification.objects.create(
                        user_id=user_id,
                        channel="web",
                        message=f"⏹️ 已取消{kind}: {identifier}｜{detail}",
                    )
                )()
        except Exception as e:  # noqa: BLE001
            logger.error(
                "[CancelScheduledTaskTool] 审计写入失败（取消本身已完成，"
                "task=%s type=%s user=%s）: %s",
                identifier,
                task_type,
                user_id or "-",
                e,
                exc_info=True,
            )

    async def _cleanup_orphan_schedules(
        self, *, crontab_id: int | None = None, interval_id: int | None = None
    ) -> None:
        """删除取消后不再被任何 PeriodicTask 引用的调度行。

        django-celery-beat 删 PeriodicTask **不会**清它引用的 CrontabSchedule /
        IntervalSchedule，线上因此累积了 8 行孤儿（含 2026-07-05 消失的那条
        hourly research 的 `0 * * * *`）。只删**引用计数为 0** 的，共享调度
        （仍被别的任务引用）一律保留。失败只告警，不影响取消结果。
        """
        try:
            from django_celery_beat.models import CrontabSchedule, IntervalSchedule
            from django_celery_beat.models import PeriodicTask as _PT

            if crontab_id:
                left = await db_async(
                    lambda: _PT.objects.filter(crontab_id=crontab_id).count()
                )()
                if left == 0:
                    await db_async(
                        lambda: CrontabSchedule.objects.filter(id=crontab_id).delete()
                    )()
                    logger.info(
                        "[CancelScheduledTaskTool] 清理孤儿调度 crontab_id=%s", crontab_id
                    )

            if interval_id:
                left = await db_async(
                    lambda: _PT.objects.filter(interval_id=interval_id).count()
                )()
                if left == 0:
                    await db_async(
                        lambda: IntervalSchedule.objects.filter(id=interval_id).delete()
                    )()
                    logger.info(
                        "[CancelScheduledTaskTool] 清理孤儿调度 interval_id=%s",
                        interval_id,
                    )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "[CancelScheduledTaskTool] 清理孤儿调度失败（不影响取消）: %s", e
            )

    async def execute(
        self,
        task_id_or_name: str = "",
        task_type: str = "scheduled",
        **kwargs,
    ) -> ToolResult:
        if not task_id_or_name:
            return ToolResult(success=False, error="task_id_or_name 为必填项")

        user_id = kwargs.get("user_id", "") or ""
        agent_name = kwargs.get("agent_name", "") or kwargs.get("agent_type", "") or ""

        try:
            if task_type == "recurring":
                from django_celery_beat.models import PeriodicTask

                # 删除前先取调度描述（删掉之后就无从得知了）
                rows = await db_async(
                    lambda: list(
                        PeriodicTask.objects.filter(name=task_id_or_name).values(
                            "crontab_id",
                            "interval_id",
                            "crontab__minute",
                            "crontab__hour",
                            "crontab__day_of_month",
                            "crontab__month_of_year",
                            "crontab__day_of_week",
                            "crontab__timezone",
                            "interval__every",
                            "interval__period",
                        )[:1]
                    )
                )()
                sched_desc = _format_beat_schedule(rows[0]) if rows else "N/A"
                crontab_id = rows[0].get("crontab_id") if rows else None
                interval_id = rows[0].get("interval_id") if rows else None

                deleted, _ = await db_async(
                    lambda: PeriodicTask.objects.filter(name=task_id_or_name).delete()
                )()
                if deleted:
                    logger.info(
                        "[CancelScheduledTaskTool] removed recurring task_name=%s "
                        "schedule=%s user=%s",
                        task_id_or_name,
                        sched_desc,
                        user_id or "-",
                    )
                    await self._record_cancellation(
                        task_type="recurring",
                        identifier=task_id_or_name,
                        detail=f"原调度: {sched_desc}",
                        user_id=user_id,
                        agent_name=agent_name,
                    )
                    try:
                        await self._cleanup_orphan_schedules(
                            crontab_id=crontab_id, interval_id=interval_id
                        )
                    except Exception as e:  # noqa: BLE001
                        # 清理是尽力而为：方法体内部已兜底，这里再兜一层，
                        # 保证"清理出任何问题都不影响取消结果"是硬契约。
                        logger.warning(
                            "[CancelScheduledTaskTool] 孤儿调度清理异常（不影响取消）: %s",
                            e,
                        )
                    return ToolResult(
                        success=True,
                        data={
                            "task_name": task_id_or_name,
                            "status": "CANCELLED",
                            "message": f"周期任务 {task_id_or_name} 已取消。",
                        },
                    )
                else:
                    return ToolResult(
                        success=False,
                        error=f"未找到周期任务: {task_id_or_name}",
                    )
            else:
                # 撤销一次性任务（按 DB UUID）
                from apps.agent.models import ScheduledOneTimeTask

                # Fetch celery_task_id first, then CAS update
                task_info = await db_async(
                    lambda: ScheduledOneTimeTask.objects.filter(
                        id=task_id_or_name
                    ).values("celery_task_id", "status").first()
                )()

                if not task_info or task_info["status"] not in ("pending", "running"):
                    return ToolResult(
                        success=False,
                        error=f"无法取消任务: {task_id_or_name}（任务已处于终态）",
                    )

                celery_id = task_info.get("celery_task_id") or ""

                # CAS: pending/running → revoked
                affected = await db_async(
                    lambda: ScheduledOneTimeTask.objects.filter(
                        id=task_id_or_name,
                        status__in=["pending", "running"],
                    ).update(status="revoked")
                )()

                if celery_id:
                    celery_app.control.revoke(celery_id, terminate=True)

                logger.info(
                    "[CancelScheduledTaskTool] revoked one-time task db_id=%s user=%s",
                    task_id_or_name,
                    user_id or "-",
                )
                await self._record_cancellation(
                    task_type="scheduled",
                    identifier=task_id_or_name,
                    detail=f"revoked celery_id={celery_id or '-'}",
                    user_id=user_id,
                    agent_name=agent_name,
                )
                return ToolResult(
                    success=True,
                    data={
                        "task_id": task_id_or_name,
                        "status": "CANCELLED",
                        "message": f"一次性定时任务 {task_id_or_name} 已取消。",
                    },
                )
        except Exception as e:
            logger.error(
                "[CancelScheduledTaskTool] cancel failed: %s", e, exc_info=True
            )
            return ToolResult(success=False, error=f"取消任务失败: {e}")


class ListScheduledTasksTool(BaseTool):
    """
    查看当前系统中的定时任务列表。
    """

    name = "list_scheduled_tasks"
    description = "查看当前系统中已注册的周期定时任务列表（数据库）。一次性任务可通过 get_task_result 查询。"

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {},
        }

    async def execute(self, **kwargs) -> ToolResult:
        try:
            from django_celery_beat.models import PeriodicTask
            from apps.agent.models import ScheduledOneTimeTask

            # Periodic tasks (existing logic)
            tasks = await db_async(list)(
                PeriodicTask.objects.filter(enabled=True).select_related(
                    "crontab", "interval"
                )
            )

            import json as _json

            lines = [f"共 {len(tasks)} 个周期定时任务（数据库）："]
            for task in tasks:
                schedule_desc = ""
                if task.crontab:
                    # 带上时区，否则用户看到 '0 10 * * *' 无从判断是北京时间还是 UTC
                    schedule_desc = (
                        f"crontab({task.crontab.minute} {task.crontab.hour} "
                        f"{task.crontab.day_of_month} {task.crontab.month_of_year} "
                        f"{task.crontab.day_of_week}，时区 {task.crontab.timezone})"
                    )
                elif task.interval:
                    schedule_desc = str(task.interval)

                try:
                    kws = _json.loads(task.kwargs or "{}")
                except Exception:
                    kws = {}

                agent = kws.get("agent_name", "N/A")
                lines.append(
                    f"- **{task.name}** | Agent: {agent} | Schedule: {schedule_desc} | Enabled: {task.enabled}"
                )

            # One-time tasks
            one_time = await db_async(list)(
                ScheduledOneTimeTask.objects.filter(
                    status__in=["pending", "running", "completed", "failed", "missed"]
                ).order_by("run_at")
            )
            if one_time:
                lines.append(f"\n共 {len(one_time)} 个一次性任务：")
                for task in one_time:
                    status_icon = {
                        "pending": "⏳",
                        "running": "🔄",
                        "completed": "✅",
                        "failed": "❌",
                        "missed": "⚠️",
                    }.get(task.status, "")
                    lines.append(
                        f"- {status_icon} **{task.task_name}** | Agent: {task.agent_name} | "
                        f"Run at: {format_business(task.run_at)} | Status: {task.status}"
                    )

            if not tasks and not one_time:
                return ToolResult(
                    success=True,
                    data="当前没有已注册的定时任务。",
                )

            return ToolResult(success=True, data="\n".join(lines))
        except Exception as e:
            logger.error("[ListScheduledTasksTool] list failed: %s", e, exc_info=True)
            return ToolResult(success=False, error=f"查询任务列表失败: {e}")
