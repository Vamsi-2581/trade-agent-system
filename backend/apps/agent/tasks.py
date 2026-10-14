"""Celery tasks for scheduled agent task execution, task progress archival, and health checks."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone, timedelta

from celery_app import app

logger = logging.getLogger(__name__)
_wf_logger = logging.getLogger(f"{__name__}.workflow")

# ---------------------------------------------------------------------------
# Health check constants
# ---------------------------------------------------------------------------
_HEARTBEAT_INTERVAL = 60  # seconds — push heartbeat if last_hb older than this
_ZOMBIE_TIMEOUT = 3600  # seconds — mark as zombie if no update for this long (1h)


def _session_manager_reset() -> None:
    """Clear session-level Redis connection caches.

    Must be called with the event loop still usable (i.e. before
    ``loop.close()`` in the Celery-task finally block).  Failing to
    properly close these clients leaves connections bound to the loop;
    when the loop is then closed and Python GC collects the connection
    objects, their ``__del__`` hits a closed loop and logs
    ``RuntimeError: Event loop is closed``.
    """
    from apps.memory.redis_client import RedisPool

    RedisPool.close_client()

    try:
        from apps.agent import ws_pending

        ws_pending._reset_client()
    except Exception:
        pass

    try:
        from apps.agent.session_manager import _session_manager

        old = _session_manager._redis
        _session_manager._redis = None
        if old is not None:
            # Best-effort sync close via the still-alive thread loop.
            try:
                loop = asyncio.get_event_loop()
                if loop is not None and not loop.is_closed():
                    loop.run_until_complete(old.aclose())
            except Exception:
                pass
    except Exception:
        pass


@app.task(bind=True, acks_late=True, track_started=True)
def execute_scheduled_agent_task(
    self,
    agent_name: str,
    message: str,
    user_id: str = "",
    scheduled_task_id: str = "",
) -> dict:
    """
    在指定时间执行 Agent 任务（一次性定时任务）。

    由 SubmitScheduledTaskTool 通过 apply_async(eta=...) 触发。
    任务到达指定时间后，路由到目标 SubAgent 执行。

    Args:
        agent_name: 目标 Agent 名称（如 analyst、quant、researcher）
        message: 任务消息内容
        user_id: 用户 ID（可选）
        scheduled_task_id: ScheduledOneTimeTask DB UUID（可选，用于状态追踪）

    Returns:
        Agent 执行结果
    """
    from apps.agent.base import AgentMessage, AgentResult
    from apps.agent.supervisor import SupervisorAgent
    from apps.agent.task_tracker import TaskTracker, tracker_context

    logger.info(
        "[execute_scheduled_agent_task] agent=%s user_id=%s task_id=%s",
        agent_name,
        user_id,
        scheduled_task_id,
    )

    # Celery worker 连接长寿复用，长 LLM 调用期间可能被 PG
    # idle_in_transaction_session_timeout 杀死（2026-09-18 事故：
    # notify_user "server closed the connection unexpectedly"，30s 时间线吻合）。
    # 入口丢弃陈旧连接，保证本任务从全新连接开始。
    try:
        from django.db import connections

        connections.close_all()
    except Exception:  # noqa: BLE001
        logger.warning(
            "[execute_scheduled_agent_task] entry close_all failed",
            exc_info=True,
        )

    # CAS: pending -> running (idempency guard against duplicate delivery)
    if scheduled_task_id:
        cas_rows = _update_task_status(scheduled_task_id, "running")
        if cas_rows == 0:
            logger.warning(
                "[execute_scheduled_agent_task] CAS failed for %s -- "
                "task not pending, skipping (idempency)",
                scheduled_task_id,
            )
            return {
                "status": "SKIPPED",
                "agent_name": agent_name,
                "reason": "not_pending",
            }

    # Use celery_task_id as tracker task_id, fallback to scheduled_task_id
    tracker_task_id = self.request.id or scheduled_task_id or str(uuid.uuid4())

    # Initialize TaskTracker for scheduled task execution
    tracker = TaskTracker(
        task_id=tracker_task_id,
        user_id=str(user_id or ""),
        task_type="scheduled_agent",
    )
    token = tracker_context.set(tracker)
    tracker.start(f"定时任务已触发：{agent_name}")

    msg = AgentMessage(
        sender="scheduler",
        recipient=agent_name,
        payload={"text": message, "scheduled": True},
        user_id=user_id,
    )

    task_result = None

    try:
        loop = asyncio.new_event_loop()
        try:
            supervisor = SupervisorAgent.get_instance()
            result: AgentResult = loop.run_until_complete(
                supervisor._route_to_agent(agent_name, msg)
            )
        finally:
            # Close Redis asyncio client before closing the event loop,
            # otherwise connection.__del__ fires on a closed loop and logs
            # "RuntimeError: Event loop is closed" warnings.
            _session_manager_reset()
            loop.close()

        if result.success:
            logger.info(
                "[execute_scheduled_agent_task] agent=%s completed, data=%s",
                agent_name,
                str(result.data)[:200],
            )

            # Format result summary for notification
            result_summary = str(result.data)[:4000] if result.data else "任务完成"
            tracker.complete(f"{agent_name} 执行完成：{result_summary}")

            task_result = {
                "status": "SUCCESS",
                "agent_name": agent_name,
                "result": str(result.data),
            }
        else:
            logger.error(
                "[execute_scheduled_agent_task] agent=%s failed: %s",
                agent_name,
                result.error,
            )

            tracker.fail(f"{agent_name} 执行失败：{result.error}")

            task_result = {
                "status": "FAILURE",
                "agent_name": agent_name,
                "error": result.error,
            }
    except Exception as e:
        logger.error(
            "[execute_scheduled_agent_task] error: %s",
            e,
            exc_info=True,
        )

        tracker.fail(f"任务执行异常：{str(e)}")

        task_result = {
            "status": "ERROR",
            "agent_name": agent_name,
            "error": str(e),
        }
    finally:
        tracker.stop()
        tracker_context.reset(token)

        # Always finalize DB status if scheduled_task_id provided
        if scheduled_task_id:
            status = task_result.get("status", "ERROR") if task_result else "ERROR"
            try:
                _finalize_task(
                    scheduled_task_id,
                    status,
                    result=task_result if status == "SUCCESS" else None,
                    error=task_result.get("error") if task_result else None,
                )
            except Exception:
                logger.error(
                    "[execute_scheduled_agent_task] finalize DB failed for %s",
                    scheduled_task_id,
                    exc_info=True,
                )

        # 出口丢弃连接：不把（可能已被 PG 杀死的）连接留给下一个任务
        try:
            from django.db import connections

            connections.close_all()
        except Exception:  # noqa: BLE001
            logger.warning(
                "[execute_scheduled_agent_task] exit close_all failed",
                exc_info=True,
            )

    return task_result


# ---------------------------------------------------------------------------
# CAS helpers for ScheduledOneTimeTask state machine
# ---------------------------------------------------------------------------


def _update_task_status(
    scheduled_task_id: str,
    new_status: str,
) -> int:
    """CAS: atomically update task status with a condition.

    Returns the number of rows affected (0 or 1).
    When transitioning to 'running', also sets executed_at.
    """
    from apps.agent.models import ScheduledOneTimeTask

    now = datetime.now(timezone.utc)
    if new_status == "running":
        return ScheduledOneTimeTask.objects.filter(
            id=scheduled_task_id, status="pending"
        ).update(status="running", executed_at=now)
    else:
        return (
            ScheduledOneTimeTask.objects.filter(
                id=scheduled_task_id,
            )
            .exclude(status__in=("completed", "failed", "revoked", "missed"))
            .update(status=new_status)
        )


def _finalize_task(
    scheduled_task_id: str,
    task_status: str,
    result: dict | None = None,
    error: str | None = None,
) -> None:
    """Write terminal status to the DB record with CAS guard (only update if status is 'running')."""
    from apps.agent.models import ScheduledOneTimeTask
    import json as _json

    now = datetime.now(timezone.utc)
    if task_status in ("SUCCESS",):
        ScheduledOneTimeTask.objects.filter(
            id=scheduled_task_id, status="running"
        ).update(
            status="completed",
            executed_at=now,
            result=_json.dumps(result) if result else "",
        )
    else:
        ScheduledOneTimeTask.objects.filter(
            id=scheduled_task_id, status="running"
        ).update(
            status="failed",
            executed_at=now,
            error=error if error else (str(result) if result else "Unknown error"),
        )


# ---------------------------------------------------------------------------
# System scheduler user resolution
# ---------------------------------------------------------------------------


def _resolve_scheduler_user_id(user_id: str) -> str:
    """Return the Django User UUID for scheduled tasks.

    If ``user_id`` is non-empty, pass through unchanged.
    Otherwise resolve to the ``system_scheduler`` system user so that
    WorkflowHistory and BacktestResult always have a valid user FK.
    """
    if user_id:
        return user_id
    try:
        from django.contrib.auth import get_user_model

        User = get_user_model()
        scheduler = User.objects.get(username="system_scheduler")
        logger.info("Resolved empty user_id to system_scheduler (%s)", scheduler.id)
        return str(scheduler.id)
    except Exception as exc:
        logger.warning(
            "Could not resolve system_scheduler user: %s — using empty user_id", exc
        )
        return ""


# ---------------------------------------------------------------------------
# Execute recurring agent task
# ---------------------------------------------------------------------------


def _result_summary_for_user(result) -> str:
    """把 AgentResult 压成用户可读的结果文本（用于任务完成通知）。

    workflow 分支的 data 是
    ``{"workflow_id", "workflow_summary", "step_results", "elapsed_seconds"}``，
    直接 ``str()`` 会把带嵌套 dict 的整份 repr 塞进通知（还极易被截断），
    所以优先取 workflow_summary / content。
    """
    data = getattr(result, "data", None)
    if isinstance(data, dict):
        for key in ("workflow_summary", "content"):
            if data.get(key):
                return str(data[key])
        # 未知结构：宁可截断，也不要把整份 repr 塞进通知
        return str(data)[:500]
    return str(data or "任务完成")


@app.task(bind=True, acks_late=True, track_started=True)
def execute_recurring_agent_task(
    self,
    agent_name: str,
    message: str,
    user_id: str = "",
    task_name: str = "",
    workflow_steps: list = None,
    workflow_summary: str = "",
) -> dict:
    """
    周期性执行 Agent 任务（Celery beat 调度）。

    由 SubmitRecurringTaskTool 动态注册到 beat_schedule。

    Args:
        agent_name: 目标 Agent 名称
        message: 任务消息内容
        user_id: 用户 ID（可选）
        task_name: 定时任务名称
        workflow_steps: workflow 步骤列表（可选，由 supervisor 预先分解）
        workflow_summary: workflow 一句话概括（可选）

    Returns:
        Agent 执行结果
    """
    from apps.agent.base import AgentMessage, AgentResult
    from apps.agent.supervisor import SupervisorAgent
    from apps.agent.task_tracker import TaskTracker, tracker_context

    is_workflow = bool(workflow_steps)
    _wf_logger.info(
        "[TASK] task_name=%s agent=%s user_id=%s workflow=%s steps=%s",
        task_name,
        agent_name,
        user_id,
        is_workflow,
        len(workflow_steps) if is_workflow else 0,
    )

    logger.info(
        "[execute_recurring_agent_task] task_name=%s agent=%s user_id=%s",
        task_name,
        agent_name,
        user_id,
    )

    resolved_user_id = _resolve_scheduler_user_id(user_id)

    # 用户可见性：周期任务此前**完全没有** TaskTracker。
    # workflow 分支只落一条 web Notification（_record_workflow），单 Agent 分支连这条
    # 都没有——用户在主通道（Telegram/Lark）收不到任何结果，失败也无人知晓。
    # （与 execute_scheduled_agent_task 的同类修复保持一致。）
    #
    # 注意 user_id 用**通道侧** id（telegram_id / feishu open_id），不能用
    # resolved_user_id：后者在无用户上下文时是 system_scheduler 这个 Django UUID，
    # 推给用户是错的。
    tracker_task_id = self.request.id or f"recurring-{task_name or agent_name}"
    tracker = TaskTracker(
        task_id=tracker_task_id,
        user_id=str(user_id or ""),
        task_type="recurring_agent",
    )
    token = tracker_context.set(tracker)
    tracker.start(f"周期任务已触发：{task_name or agent_name}")

    msg = AgentMessage(
        sender="scheduler",
        recipient=agent_name,
        payload={"text": message, "scheduled": True, "task_name": task_name},
        user_id=resolved_user_id,
    )

    try:
        # 重置所有单例和缓存实例，防止跨任务复用绑定到旧事件循环的组件
        SupervisorAgent._instance = None
        from apps.agent.llm_client import LLMClient

        LLMClient._instance = None
        from apps.agent.frame_manager import FrameManager

        FrameManager._instance = None
        from apps.agent.registry import AgentRegistry

        AgentRegistry._registry.clear()  # 清除已实例化的 Agent（含旧 loop 绑定）
        from apps.agent.supervisor import IntentRouter

        IntentRouter._instance = None
        _session_manager_reset()

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            supervisor = SupervisorAgent.get_instance()

            if workflow_steps:
                # Pre-decomposed workflow: execute directly without re-parsing.
                # The supervisor already split the workflow when submitting the task,
                # so we skip _parse_intent and call _execute_workflow directly.
                workflow_plan = {
                    "summary": workflow_summary or task_name,
                    "steps": workflow_steps,
                }
                result: AgentResult = loop.run_until_complete(
                    supervisor._execute_workflow(workflow_plan, msg)
                )
            else:
                # No workflow: normal single-agent or supervisor routing
                result: AgentResult = loop.run_until_complete(supervisor.handle(msg))
        finally:
            # Close Redis asyncio client before closing the event loop,
            # otherwise connection.__del__ fires on a closed loop and logs
            # "RuntimeError: Event loop is closed" warnings.
            _session_manager_reset()
            loop.close()
            asyncio.set_event_loop(None)

        if result.success:
            logger.info(
                "[execute_recurring_agent_task] task_name=%s agent=%s completed",
                task_name,
                agent_name,
            )
            tracker.complete(_result_summary_for_user(result))
            return {
                "status": "SUCCESS",
                "task_name": task_name,
                "agent_name": agent_name,
                "result": str(result.data),
            }
        else:
            logger.error(
                "[execute_recurring_agent_task] task_name=%s agent=%s failed: %s",
                task_name,
                agent_name,
                result.error,
            )
            tracker.fail(f"{task_name or agent_name} 执行失败：{result.error}")
            return {
                "status": "FAILURE",
                "task_name": task_name,
                "agent_name": agent_name,
                "error": result.error,
            }
    except Exception as e:
        logger.error(
            "[execute_recurring_agent_task] error: %s",
            e,
            exc_info=True,
        )
        tracker.fail(f"{task_name or agent_name} 执行异常：{e}")
        return {
            "status": "ERROR",
            "task_name": task_name,
            "agent_name": agent_name,
            "error": str(e),
        }
    finally:
        tracker.stop()
        tracker_context.reset(token)


@app.task(bind=True, acks_late=True)
def archive_task_progress(
    self,
    task_id: str,
    user_id: str,
    task_type: str,
    status: str,
    progress: float,
    milestones: list[dict],
    result: str,
) -> dict:
    """
    异步归档：将 Redis 中的已完成任务进度写入 PostgreSQL。

    由 TaskTracker.complete() / fail() 触发。
    """
    from datetime import datetime, timezone

    from apps.agent.models import TaskProgress

    logger.info(
        "[archive_task_progress] archiving task_id=%s status=%s", task_id, status
    )

    try:
        TaskProgress.objects.get_or_create(
            task_id=task_id,
            defaults={
                "task_type": task_type,
                "status": status,
                "progress": progress,
                "milestones": milestones,
                "result": result,
                "completed_at": datetime.now(timezone.utc),
                "user_id": user_id if len(user_id) == 36 else None,  # UUID format check
            },
        )
        return {"status": "SUCCESS", "task_id": task_id}
    except Exception as e:
        logger.error("[archive_task_progress] failed: %s", e, exc_info=True)
        return {"status": "ERROR", "error": str(e)}


# ---------------------------------------------------------------------------
# Task health check — centralized zombie detection, heartbeat, and auto-retry
# This is the SOLE zombie detection mechanism for all task types.
# ---------------------------------------------------------------------------


@app.task(bind=True)
def check_task_health(self) -> dict:
    """集中巡检所有 running 任务，推送心跳 + 检测僵尸任务并自动重试。"""
    import redis
    from django.conf import settings

    url = settings.REDIS_URL
    if url.rsplit("/", 1)[-1].isdigit():
        url = url.rsplit("/", 1)[0] + "/3"

    r = redis.from_url(url, decode_responses=True)
    zombie_ids: list[tuple[str, str, float]] = []
    heartbeat_ids: list[tuple[str, str, float]] = []

    for key in r.scan_iter("task:progress:*"):
        data = r.hgetall(key)
        if not data or data.get("status") != "running":
            continue

        task_id = data.get("task_id", "")
        user_id = data.get("user_id", "")
        updated_str = data.get("updated_at", "")
        last_hb_str = data.get("last_heartbeat", "")
        last_alive_str = data.get("last_alive", "")

        try:
            updated = datetime.fromisoformat(updated_str) if updated_str else None
            last_hb = datetime.fromisoformat(last_hb_str) if last_hb_str else None
            last_alive = (
                datetime.fromisoformat(last_alive_str) if last_alive_str else None
            )
        except ValueError:
            continue

        if updated is None:
            continue

        age = (datetime.now(timezone.utc) - updated).total_seconds()
        # 取 updated_at 和 last_alive 中较新的时间戳判定僵尸
        # last_alive 由 tracker.alive() 在工具执行后显式更新，比 updated_at 更精确
        alive_age = None
        if last_alive:
            alive_age = (datetime.now(timezone.utc) - last_alive).total_seconds()
            age = min(age, alive_age)

        if age > _ZOMBIE_TIMEOUT:
            zombie_ids.append((task_id, user_id, age))
            continue

        hb_age = (
            (datetime.now(timezone.utc) - last_hb).total_seconds() if last_hb else age
        )
        if hb_age >= _HEARTBEAT_INTERVAL:
            heartbeat_ids.append((task_id, user_id, age))

    for task_id, user_id, age in zombie_ids:
        _handle_zombie_task(r, task_id, user_id, age)

    for task_id, user_id, age in heartbeat_ids:
        redis_key = f"task:progress:{task_id}"
        extra = r.hgetall(redis_key)
        progress = float(extra.get("progress", 0) or 0)
        step = extra.get("step", "")
        _send_heartbeat_notification(task_id, user_id, age, progress, step)
        r.hset(
            redis_key,
            "last_heartbeat",
            datetime.now(timezone.utc).isoformat(),
        )

    return {
        "checked": len(heartbeat_ids) + len(zombie_ids),
        "heartbeats": len(heartbeat_ids),
        "zombies": len(zombie_ids),
    }


def _handle_zombie_task(
    r,
    task_id: str,
    user_id: str,
    age: float,
) -> None:
    """处理僵尸任务：自动重试（retry_count < max_retries）或通知用户。"""
    from apps.agent.task_tracker import _send_notification_redis

    redis_key = f"task:progress:{task_id}"
    data = r.hgetall(redis_key)
    retry_count = int(data.get("retry_count", 0))
    max_retries = int(data.get("max_retries", 1))
    original_task_str = data.get("original_task", "")

    short_id = task_id
    age_str = f"{int(age)}秒"

    # original_task 缺失：无法重试，直接标记僵尸并通知用户
    if not original_task_str:
        r.hset(redis_key, "status", "zombie")
        _send_notification_redis(
            user_id,
            f"任务 #{short_id} 异常中断，请重新发送指令",
        )
        return

    if retry_count < max_retries:
        # 自动重试
        try:
            original_task = json.loads(original_task_str)
            celery_task = original_task.get("celery_task", "")

            if celery_task:
                # Celery task — resend via app.send_task
                task_args = original_task.get("args", [])
                task_kwargs = original_task.get("kwargs", {})
                new_result = app.send_task(
                    celery_task, args=task_args, kwargs=task_kwargs
                )
                new_task_id = new_result.id
            else:
                # Stream task — republish to agent:tasks
                import asyncio
                from apps.agent import bus as agent_bus

                retry_payload = dict(original_task)
                retry_payload["retry_count"] = str(retry_count + 1)

                async def _republish():
                    return await agent_bus.publish("agent:tasks", retry_payload)

                loop = asyncio.new_event_loop()
                try:
                    new_task_id = loop.run_until_complete(_republish())
                finally:
                    _session_manager_reset()
                    loop.close()

            r.hset(
                redis_key,
                mapping={
                    "status": "zombie_retrying",
                    "retry_count": retry_count + 1,
                    "new_task_id": new_task_id,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
            )

            _send_notification_redis(
                user_id,
                f"⚠️ 任务 #{short_id} 超时无响应（{age_str}）\n"
                f"🔄 已自动重试（第 {retry_count + 1}/{max_retries} 次）\n"
                f"新任务 ID: {new_task_id}",
            )

            archive_task_progress.delay(
                task_id=task_id,
                user_id=user_id,
                task_type=data.get("task_type", "agent"),
                status="zombie_retrying",
                progress=0.0,
                milestones=[],
                result=f"Zombie, auto-retry #{retry_count + 1}, new_task_id={new_task_id}",
            )

            logger.info(
                "[check_task_health] zombie %s auto-retried → %s",
                task_id,
                new_task_id,
            )
        except Exception:
            logger.error(
                "[check_task_health] auto-retry failed for %s", task_id, exc_info=True
            )
            r.hset(redis_key, "status", "zombie")
            _send_notification_redis(
                user_id,
                f"❌ 任务 #{short_id} 自动重试失败\n请手动处理",
            )
    else:
        # 通知用户，由用户决定重试或继续
        r.hset(
            redis_key,
            mapping={
                "status": "zombie",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        )

        _send_notification_redis(
            user_id,
            f"🚨 任务 #{short_id} 超时无响应（{age_str}）\n"
            f"已自动重试 {retry_count}/{max_retries} 次，请手动处理\n"
            f"使用 /retry {task_id} 或 /continue {task_id}",
        )

        archive_task_progress.delay(
            task_id=task_id,
            user_id=user_id,
            task_type=data.get("task_type", "agent"),
            status="zombie",
            progress=0.0,
            milestones=[],
            result=f"Zombie after {retry_count} retries, age={age_str}",
        )

        logger.warning("[check_task_health] zombie %s — awaiting user action", task_id)


def _send_heartbeat_notification(
    task_id: str, user_id: str, age: float, progress: float = 0.0, step: str = ""
) -> None:
    """推送心跳通知——含当前进度和阶段。"""
    from apps.agent.task_tracker import _send_notification_redis

    short_id = task_id
    mins = int(age) // 60
    secs = int(age) % 60
    elapsed = f"{mins}分{secs}秒" if mins else f"{secs}秒"

    parts = [f"⏳ 任务 #{short_id} 运行中 | 耗时：{elapsed}"]
    if step:
        parts.append(f"阶段：{step}")
    if progress > 0:
        parts.append(f"进度：{int(progress * 100)}%")
    _send_notification_redis(user_id, "\n".join(parts))


# ---------------------------------------------------------------------------
# Session expiry check — warns users before multi-turn sessions expire
# ---------------------------------------------------------------------------

_SESSION_WARN_BEFORE = 300  # warn when session TTL drops below this (seconds)


@app.task(bind=True)
def check_session_expiry(self) -> dict:
    """扫描 session:*:context，对即将过期的 multi_turn 会话发提醒。"""
    import json
    import time
    import redis
    from django.conf import settings
    from apps.agent.task_tracker import _send_notification_redis

    url = settings.REDIS_URL
    r = redis.from_url(url, decode_responses=True)
    warned = 0
    for key in r.scan_iter("session:*:context"):
        raw = r.get(key)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if not data or data.get("state") != "multi_turn":
            continue

        logger.debug("[check_session_expiry] checking %s", key)
        expires_at = data.get("expires_at", 0)
        remaining = expires_at - time.time()
        if 0 < remaining < _SESSION_WARN_BEFORE:
            user_id = key.split(":")[1]
            _send_notification_redis(
                user_id,
                f"会话即将过期（{int(remaining)}秒）\n请继续对话以保持会话",
            )
            warned += 1

    return {"warned": warned}


# ---------------------------------------------------------------------------
# Startup recovery — reconcile DB vs broker on worker boot
# ---------------------------------------------------------------------------

SCHEDULED_TASK_GRACE_PERIOD = 1800  # 30 min default, overridden via Django settings
RECOVERY_LOCK_TTL = 120  # seconds for Redis SETNX lock
STALE_RUNNING_THRESHOLD = 600  # 10 min — running tasks older than this are "stale"


def _get_grace_period() -> int:
    """Get grace period from Django settings, falling back to module default."""
    from django.conf import settings

    return getattr(settings, "SCHEDULED_TASK_GRACE_PERIOD", SCHEDULED_TASK_GRACE_PERIOD)


def _notify_user(user_id: str, message: str) -> None:
    """Send a notification to the user (Telegram/Web)."""
    try:
        from apps.agent.task_tracker import _send_notification_redis

        _send_notification_redis(user_id, message)
    except Exception:
        logger.warning(
            "[startup_recovery_check] failed to notify user %s",
            user_id,
            exc_info=True,
        )


def _acquire_recovery_lock() -> str | None:
    """Try to acquire a distributed lock via Redis SETNX.

    Returns the lock value if acquired, None if another worker holds it.
    """
    import redis
    from django.conf import settings

    url = settings.REDIS_URL
    if url.rsplit("/", 1)[-1].isdigit():
        url = url.rsplit("/", 1)[0] + "/5"
    r = redis.from_url(url, decode_responses=True)
    lock_key = "recovery:startup_lock"
    lock_value = f"worker:{os.getpid()}:{time.time()}"
    acquired = r.set(lock_key, lock_value, nx=True, ex=RECOVERY_LOCK_TTL)
    return lock_value if acquired else None


def _release_recovery_lock(lock_value: str) -> None:
    """Release the distributed lock if we hold it (atomic via Lua)."""
    import redis
    from django.conf import settings

    url = settings.REDIS_URL
    if url.rsplit("/", 1)[-1].isdigit():
        url = url.rsplit("/", 1)[0] + "/5"
    r = redis.from_url(url, decode_responses=True)
    try:
        lock_key = "recovery:startup_lock"
        lua = """
        if redis.call("GET", KEYS[1]) == ARGV[1] then
            return redis.call("DEL", KEYS[1])
        else
            return 0
        end
        """
        r.eval(lua, 1, lock_key, lock_value)
    finally:
        r.close()


@app.task(bind=True)
def startup_recovery_check(self) -> dict:
    """One-time reconciliation on worker startup.

    Scans DB for pending/running one-time tasks and:
    - Future tasks: revoke old id -> re-apply_async(eta)
    - Grace-period expired: immediate apply_async
    - Beyond grace: mark missed + notify user
    - Stale running (crash residue): treat as pending for reconciliation
    Also confirms Beat has loaded all PeriodicTasks.
    """
    from apps.agent.models import ScheduledOneTimeTask
    from django_celery_beat.models import PeriodicTask

    # Distributed lock: only one worker performs recovery
    lock_value = _acquire_recovery_lock()
    if lock_value is None:
        logger.info("[startup_recovery_check] another worker holds lock, skipping")
        return {"skipped": True, "reason": "lock_held"}

    try:
        logger.info("[startup_recovery_check] starting recovery (lock=%s)", lock_value)
        now = datetime.now(timezone.utc)
        grace_cutoff = now - timedelta(seconds=_get_grace_period())
        stale_threshold = now - timedelta(seconds=STALE_RUNNING_THRESHOLD)

        stats = {"future": 0, "grace": 0, "missed": 0, "running": 0, "errors": 0}

        # --- Recover stale 'running' tasks (crash residue) ---
        # Use run_at as staleness indicator: a running task whose scheduled time
        # is well in the past is almost certainly a crash residue (auto_now on
        # updated_at would mask true last-activity time).
        stale_running = ScheduledOneTimeTask.objects.filter(
            status="running",
            run_at__lt=stale_threshold,
        )
        for task in stale_running:
            try:
                task.status = "pending"
                task.celery_task_id = ""
                task.save(update_fields=["status", "celery_task_id", "updated_at"])
                stats["running"] += 1
                logger.info(
                    "[startup_recovery_check] recovered stale running %s -> pending",
                    task.id,
                )
            except Exception:
                stats["errors"] += 1
                logger.error(
                    "[startup_recovery_check] failed to recover %s",
                    task.id,
                    exc_info=True,
                )

        # --- Reconcile pending tasks ---
        pending_tasks = ScheduledOneTimeTask.objects.filter(status="pending")
        for task in pending_tasks:
            try:
                if task.run_at > now:
                    # Future: revoke old, re-queue with eta
                    if task.celery_task_id:
                        app.control.revoke(task.celery_task_id)
                    celery_result = execute_scheduled_agent_task.apply_async(
                        kwargs={
                            "agent_name": task.agent_name,
                            "message": task.message,
                            "user_id": task.user_id,
                            "scheduled_task_id": str(task.id),
                        },
                        eta=task.run_at,
                    )
                    task.celery_task_id = celery_result.id
                    task.save(update_fields=["celery_task_id", "updated_at"])
                    stats["future"] += 1
                    logger.info(
                        "[startup_recovery_check] re-queued future task %s eta=%s",
                        task.id,
                        task.run_at.isoformat(),
                    )

                elif task.run_at >= grace_cutoff:
                    # Within grace: immediate execution
                    celery_result = execute_scheduled_agent_task.apply_async(
                        kwargs={
                            "agent_name": task.agent_name,
                            "message": task.message,
                            "user_id": task.user_id,
                            "scheduled_task_id": str(task.id),
                        },
                    )
                    task.celery_task_id = celery_result.id
                    task.save(update_fields=["celery_task_id", "updated_at"])
                    stats["grace"] += 1
                    logger.info(
                        "[startup_recovery_check] immediate execution for grace task %s",
                        task.id,
                    )

                else:
                    # Beyond grace: mark missed + notify
                    task.status = "missed"
                    task.error = (
                        f"Missed: run_at={task.run_at.isoformat()}, beyond grace period"
                    )
                    task.save(update_fields=["status", "error", "updated_at"])
                    stats["missed"] += 1
                    if task.user_id:
                        _notify_user(
                            task.user_id,
                            f"定时任务「{task.task_name}」已错过执行时间（{task.run_at.isoformat()}），"
                            f"超出宽限窗口，未自动补执行。",
                        )
                    logger.info(
                        "[startup_recovery_check] marked task %s as missed",
                        task.id,
                    )
            except Exception:
                stats["errors"] += 1
                logger.error(
                    "[startup_recovery_check] failed to reconcile task %s",
                    task.id,
                    exc_info=True,
                )

        # --- Confirm Beat has loaded PeriodicTasks ---
        periodic_count = PeriodicTask.objects.filter(enabled=True).count()
        try:
            ping_result = app.control.ping(timeout=5)
            beat_online = len(ping_result) > 0
        except Exception:
            beat_online = False

        logger.info(
            "[startup_recovery_check] done -- stats=%s, periodic_tasks=%d, beat_online=%s",
            stats,
            periodic_count,
            beat_online,
        )

        return {
            "recovered": stats,
            "periodic_tasks": periodic_count,
            "beat_online": beat_online,
        }
    finally:
        _release_recovery_lock(lock_value)
