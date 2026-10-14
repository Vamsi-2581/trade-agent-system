from __future__ import annotations

import json
import logging

from asgiref.sync import async_to_sync
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.pagination import PageNumberPagination

from apps.agent.base import AgentMessage, AgentResult
from apps.agent.supervisor import SupervisorAgent
from apps.agent.frame_manager import FrameManager
from apps.agent.models import WorkflowHistory

logger = logging.getLogger(__name__)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def chat(request: Request) -> Response:
    """REST入口：用户发送消息给SupervisorAgent"""
    text = request.data.get("text", "").strip()
    if not text:
        return Response({"error": "text is required"}, status=400)

    supervisor = SupervisorAgent.get_instance()
    result: AgentResult = async_to_sync(supervisor.handle)(
        AgentMessage(
            sender="user",
            recipient="supervisor",
            payload={"text": text},
            user_id=str(request.user.pk),
        )
    )

    # API responses (including failures) must also reach Web listeners.
    try:
        from apps.agent.reply_fanout import fan_out_reply

        content = (
            (
                result.data.get("content", str(result.data))
                if isinstance(result.data, dict)
                else str(result.data)
            )
            if result.success
            else f"[错误] {result.error}"
        )
        async_to_sync(fan_out_reply)(str(request.user.pk), content, origin="api")
    except Exception:
        logger.warning(
            "[AgentChat] fan_out_reply failed for user %s",
            request.user.pk,
            exc_info=True,
        )
    if result.success:
        return Response({"data": result.data, "task_id": result.task_id})
    return Response({"error": result.error, "task_id": result.task_id}, status=500)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def frame_status(request: Request) -> Response:
    """查询框架运行状态"""
    fm = FrameManager.get_instance()
    return Response(fm.status())


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def frame_control(request: Request) -> Response:
    """启动/停止框架: {"frame": "trading|assist", "action": "start|stop"}"""
    frame = request.data.get("frame")
    action = request.data.get("action")
    if frame not in ("trading", "assist") or action not in ("start", "stop"):
        return Response({"error": "Invalid frame or action"}, status=400)

    fm = FrameManager.get_instance()
    try:
        if action == "start":
            async_to_sync(fm.start)(frame)
        else:
            async_to_sync(fm.stop)(frame)
        return Response({"frame": frame, "action": action, "status": "ok"})
    except Exception as e:
        return Response({"error": str(e)}, status=500)


DISPLAY_NAME_MAP = {
    "supervisor": "主控编排",
    "analyst": "市场分析",
    "quant": "策略优化",
    "coach": "交易教练",
    "risk_advisor": "风险管理",
    "trading_frame": "交易执行",
    "assist_frame": "辅助监控",
}

ROLE_DESCRIPTIONS = {
    "supervisor": "任务调度 · 信号聚合",
    "analyst": "技术指标 · 形态识别",
    "quant": "参数调优 · 回测",
    "coach": "交易计划 · 复盘",
    "risk_advisor": "VaR · 仓位控制",
    "trading_frame": "订单路由 · 滑点控制",
    "assist_frame": "信号监控 · 风险预警",
}

COLOR_MAP = {
    "supervisor": ("var(--color-purple)", "var(--color-purple-dim)"),
    "analyst": ("var(--color-teal)", "var(--color-teal-dim)"),
    "quant": ("var(--color-green)", "var(--color-green-dim)"),
    "coach": ("var(--color-blue)", "var(--color-blue-dim)"),
    "risk_advisor": ("var(--color-amber)", "var(--color-amber-dim)"),
    "trading_frame": ("var(--color-green)", "var(--color-green-dim)"),
    "assist_frame": ("var(--color-teal)", "var(--color-teal-dim)"),
}

DESCRIPTIONS = {
    "supervisor": "核心编排器，负责任务分配、信号汇总和跨 Agent 通信协调。使用 LLM 进行意图理解和决策规划。",
}


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def list_agents(request: Request) -> Response:
    """返回所有 Agent 列表（含元数据和运行状态）"""
    from .prompt_loader import PromptLoader
    from .registry import AgentRegistry
    from .frame_manager import FrameManager

    prompt_agents = PromptLoader.list_agents()

    AgentRegistry.discover_from_prompts()
    instantiated = set(AgentRegistry._registry.keys())

    fm = FrameManager.get_instance()
    frame_st = fm.status()

    agents_list = []

    # Supervisor 条目
    agents_list.append(
        {
            "name": "supervisor",
            "display_name": DISPLAY_NAME_MAP["supervisor"],
            "role": ROLE_DESCRIPTIONS["supervisor"],
            "description": DESCRIPTIONS["supervisor"],
            "status": "ready",
            "tools": [],
            "intent": "route",
            "tag_color": COLOR_MAP["supervisor"][0],
            "tag_bg": COLOR_MAP["supervisor"][1],
        }
    )

    # Prompt 定义的子 Agent
    for agent_meta in prompt_agents:
        name = agent_meta["name"]
        is_loaded = name in instantiated
        tag_color, tag_bg = COLOR_MAP.get(
            name, ("var(--color-text)", "var(--color-text-dim)")
        )
        agents_list.append(
            {
                "name": name,
                "display_name": DISPLAY_NAME_MAP.get(name, name),
                "role": ROLE_DESCRIPTIONS.get(name, ""),
                "description": agent_meta.get("overview", ""),
                "status": "ready" if is_loaded else "standby",
                "tools": agent_meta.get("tools", []),
                "intent": agent_meta.get("intent", ""),
                "tag_color": tag_color,
                "tag_bg": tag_bg,
            }
        )

    # 框架条目
    for frame_id, frame_key in [
        ("trading_frame", "trading"),
        ("assist_frame", "assist"),
    ]:
        frame_state = frame_st.get(frame_key, "stopped")
        tag_color, tag_bg = COLOR_MAP[frame_id]
        agents_list.append(
            {
                "name": frame_id,
                "display_name": DISPLAY_NAME_MAP[frame_id],
                "role": ROLE_DESCRIPTIONS[frame_id],
                "description": f"框架状态: {frame_state}",
                "status": "running" if frame_state == "running" else "stopped",
                "tools": [],
                "intent": f"frame_{frame_key}",
                "tag_color": tag_color,
                "tag_bg": tag_bg,
                "frame_state": frame_state,
            }
        )

    return Response({"agents": agents_list})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def list_scheduled_tasks(request: Request) -> Response:
    """返回系统中已注册的周期定时任务列表（静态 beat_schedule + 数据库动态任务）"""
    from celery_app import app as celery_app
    from django_celery_beat.models import PeriodicTask

    results = []

    # 静态条目在库里**也有**行：`DatabaseScheduler.setup_schedule` 启动时把 `beat_schedule`
    # 合并进来（`update_or_create(name=…)`），`last_run_at` / `total_run_count` 由调度器每次
    # 派发时写在那一行上。所以这三格要**读真的**，不能写死。
    #
    # 原先写死成 `True/None/0` 的代价不是「少了个数」：`beat_schedule` 只在 beat 启动时合并
    # 一次，**往文件里后来加的条目在库里没有行**——于是「从未被调度过」与「正常跑着」在
    # 这个接口上完全一样（两行的 `last_run_at` 都是 null）。`scheduled` 这一格就是那个区别。
    tasks = list(
        PeriodicTask.objects.all().select_related("crontab", "interval").order_by("-id")
    )
    rows_by_name = {row.name: row for row in tasks}

    # 1. 静态定义（celery_app.py beat_schedule）
    beat_schedule = celery_app.conf.beat_schedule or {}
    for name, entry in beat_schedule.items():
        schedule = entry.get("schedule")
        schedule_str = _format_celery_schedule(schedule) if schedule else "N/A"
        task = entry.get("task", "")
        kwargs = entry.get("kwargs", {})
        # 验证 task 是否真实存在（避免显示无效条目）
        if not _task_exists(task):
            continue
        row = rows_by_name.get(name)
        results.append(
            {
                "id": None,
                "name": name,
                "agent_name": kwargs.get("agent_name", "system"),
                "message": kwargs.get("message", ""),
                "schedule": schedule_str,
                "enabled": row.enabled if row else True,
                "last_run_at": row.last_run_at.isoformat()
                if row and row.last_run_at
                else None,
                "total_run_count": row.total_run_count if row else 0,
                "expires": None,
                "start_time": None,
                "source": "static",
                # False = 这一条**没有被调度器收下**（beat 自上次启动以来没重启过），
                # 与「收下了但一次都没跑」是两件事，见上面那段注释。
                "scheduled": row is not None,
            }
        )

    # 2. 数据库动态任务（django-celery-beat）
    for task in tasks:
        schedule_desc = ""
        if task.crontab:
            schedule_desc = f"{task.crontab.minute} {task.crontab.hour} {task.crontab.day_of_month} {task.crontab.month_of_year} {task.crontab.day_of_week}"
        elif task.interval:
            schedule_desc = str(task.interval)

        try:
            kws = json.loads(task.kwargs or "{}")
        except Exception:
            kws = {}

        results.append(
            {
                "id": task.id,
                "name": task.name,
                "agent_name": kws.get("agent_name", "N/A"),
                "message": kws.get("message", ""),
                "schedule": schedule_desc,
                "enabled": task.enabled,
                "last_run_at": task.last_run_at.isoformat()
                if task.last_run_at
                else None,
                "total_run_count": task.total_run_count,
                "expires": task.expires.isoformat() if task.expires else None,
                "start_time": task.start_time.isoformat() if task.start_time else None,
                "source": "database",
            }
        )

    # 3. 一次性定时任务（ScheduledOneTimeTask）
    from apps.agent.models import ScheduledOneTimeTask

    one_time_tasks = ScheduledOneTimeTask.objects.all().order_by("-run_at")
    for task in one_time_tasks:
        results.append(
            {
                "id": str(task.id),
                "name": task.task_name,
                "agent_name": task.agent_name,
                "message": task.message,
                "schedule": task.run_at.isoformat(),
                "enabled": task.status not in ("revoked", "missed"),
                "last_run_at": task.executed_at.isoformat()
                if task.executed_at
                else None,
                "total_run_count": 1 if task.status in ("completed", "failed") else 0,
                "expires": None,
                "start_time": task.run_at.isoformat(),
                "source": "one_time",
                "status": task.status,
                "celery_task_id": task.celery_task_id or None,
                "result": task.result or None,
                "error": task.error or None,
            }
        )

    return Response({"tasks": results})


def _task_exists(task_name: str) -> bool:
    """检查 Celery task 是否在当前环境中注册"""
    from celery_app import app as celery_app

    return task_name in celery_app.tasks


def _format_celery_schedule(schedule) -> str:
    """将 Celery schedule 对象格式化为可读字符串"""
    from celery.schedules import crontab as celery_crontab, schedule as celery_schedule

    if isinstance(schedule, celery_crontab):

        def _cron_field(val):
            """将 crontab 的 set 转为简洁表达式"""
            if val == getattr(celery_crontab, "_meta", {}).get("fields", [{}])[0].get(
                "default", None
            ):
                return "*"
            # Check common patterns
            vals = sorted(val)
            if vals == list(range(0, 24)):
                return "*"  # all hours
            if vals == list(range(1, 32)):
                return "*"  # all days
            if vals == list(range(1, 13)):
                return "*"  # all months
            if vals == list(range(0, 7)):
                return "*"  # all weekdays
            if len(vals) == 1:
                return str(vals[0])
            # Check if it's a simple range
            if len(vals) > 1 and vals == list(range(vals[0], vals[-1] + 1)):
                return f"{vals[0]}-{vals[-1]}"
            return ",".join(str(v) for v in vals)

        m = _cron_field(schedule.minute)
        h = _cron_field(schedule.hour)
        dom = _cron_field(schedule.day_of_month)
        moy = _cron_field(schedule.month_of_year)
        dow = _cron_field(schedule.day_of_week)
        return f"{m} {h} {dom} {moy} {dow}"
    elif isinstance(schedule, celery_schedule):
        secs = schedule.run_every.total_seconds()
        if secs < 60:
            return f"每 {int(secs)} 秒"
        elif secs < 3600:
            return f"每 {int(secs / 60)} 分钟"
        else:
            return f"每 {int(secs / 3600)} 小时"
    elif isinstance(schedule, (int, float)):
        secs = schedule
        if secs < 60:
            return f"每 {int(secs)} 秒"
        elif secs < 3600:
            return f"每 {int(secs / 60)} 分钟"
        else:
            return f"每 {int(secs / 3600)} 小时"
    return str(schedule)


# ── Workflow History API ──


class WorkflowHistoryPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def list_workflow_history(request: Request) -> Response:
    """列出当前用户的工作流执行历史"""
    status_filter = request.query_params.get("status")

    qs = WorkflowHistory.objects.filter(user=request.user).order_by("-created_at")
    if status_filter:
        qs = qs.filter(status=status_filter)

    paginator = WorkflowHistoryPagination()
    page = paginator.paginate_queryset(qs, request)

    items = [
        {
            "id": str(h.id),
            "workflow_id": h.workflow_id,
            "summary": h.summary,
            "status": h.status,
            "total_steps": h.total_steps,
            "completed_steps": h.completed_steps,
            "elapsed_seconds": round(h.elapsed_seconds, 1),
            "error": h.error,
            "created_at": h.created_at.isoformat(),
            "completed_at": h.completed_at.isoformat() if h.completed_at else None,
        }
        for h in page
    ]

    return Response(
        {
            "count": paginator.page.paginator.count,
            "num_pages": paginator.page.paginator.num_pages,
            "current_page": paginator.page.number,
            "items": items,
        }
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_workflow_history(request: Request, workflow_id: str) -> Response:
    """获取单个工作流执行的详细信息"""
    try:
        h = WorkflowHistory.objects.get(user=request.user, workflow_id=workflow_id)
    except WorkflowHistory.DoesNotExist:
        return Response({"error": "工作流记录不存在"}, status=404)

    return Response(
        {
            "id": str(h.id),
            "workflow_id": h.workflow_id,
            "summary": h.summary,
            "status": h.status,
            "total_steps": h.total_steps,
            "completed_steps": h.completed_steps,
            "step_results": h.step_results,
            "elapsed_seconds": round(h.elapsed_seconds, 1),
            "error": h.error,
            "created_at": h.created_at.isoformat(),
            "completed_at": h.completed_at.isoformat() if h.completed_at else None,
        }
    )
