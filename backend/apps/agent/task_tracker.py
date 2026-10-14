"""
Unified task progress tracker for long-running async tasks.

Supports Celery tasks and Redis Stream Agent tasks with:
- Milestone notifications (key stages, throttled)
- Redis Hash for running state + PG archive for completed tasks
- Health checks via centralized Celery beat task (check_task_health)
"""

from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

tracker_context: ContextVar[TaskTracker | None] = ContextVar("tracker", default=None)

# ---------------------------------------------------------------------------
# Notification bridge — Celery worker sends via Redis pubsub, ASGI consumer routes to active channel
# ---------------------------------------------------------------------------
_PROGRESS_CHANNEL = "task:progress:notifications"


def _send_notification_redis(user_id: str, text: str) -> None:
    """Publish notification request to Redis pubsub for ASGI to consume."""
    import redis

    from django.conf import settings

    url = settings.REDIS_URL
    if url.rsplit("/", 1)[-1].isdigit():
        url = url.rsplit("/", 1)[0] + "/3"

    try:
        r = redis.from_url(url, decode_responses=True)
        r.publish(
            _PROGRESS_CHANNEL,
            json.dumps(
                {
                    "user_id": user_id,
                    "text": text,
                    "at": datetime.now(timezone.utc).isoformat(),
                }
            ),
        )
    except Exception:
        logger.warning(
            "[TaskTracker] failed to publish notification to Redis", exc_info=True
        )


def _format_progress_message(
    task_id: str, message: str, progress: float | None = None, elapsed: str = ""
) -> str:
    short_id = task_id
    parts = [f"\U0001f4ca 任务 #{short_id}", f"阶段：{message}"]
    if progress is not None:
        parts.append(f"进度：{int(progress * 100)}%")
    if elapsed:
        parts.append(f"耗时：{elapsed}")
    return "\n".join(parts)


def _format_duration(start_ts: float) -> str:
    secs = int(time.time() - start_ts)
    if secs < 60:
        return f"{secs}秒"
    mins = secs // 60
    secs = secs % 60
    return f"{mins}分{secs}秒" if secs else f"{mins}分钟"


# ---------------------------------------------------------------------------
# TaskTracker
# ---------------------------------------------------------------------------


class TaskTracker:
    """Track long-running task progress and notify users via their active channel (Telegram/Lark)."""

    def __init__(
        self,
        task_id: str,
        user_id: str,
        channel: str = "",
        task_type: str = "agent",
        heartbeat_interval: int = 60,
        original_task: dict | None = None,
        max_retries: int = 1,
        expected_duration: int = 240,
    ):
        self.task_id = task_id
        self.user_id = user_id
        self.task_type = task_type
        self.heartbeat_interval = heartbeat_interval
        self.original_task = original_task
        self.max_retries = max_retries
        self.expected_duration = expected_duration
        self._last_push: float = 0
        self._start_ts: float = 0
        self._milestones: list[dict] = []
        self._redis_key = f"task:progress:{task_id}"

        # Auto-detect channel if not explicitly specified
        if channel:
            self.channel = channel
        else:
            self.channel = self._resolve_channel()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self, initial_message: str = "任务已启动") -> None:
        """Mark task as started: write Redis, push notification."""
        self._start_ts = time.time()
        self._last_push = self._start_ts
        data: dict[str, Any] = {
            "step": initial_message,
            "progress": 0,
            "last_heartbeat": datetime.now(timezone.utc).isoformat(),
            "last_alive": datetime.now(timezone.utc).isoformat(),
            "retry_count": 0,
            "max_retries": self.max_retries,
            "expected_duration": str(self.expected_duration),
        }
        if self.original_task:
            data["original_task"] = json.dumps(self.original_task, ensure_ascii=False)
        self._save_redis("running", data)
        self._notify(_format_progress_message(self.task_id, initial_message))
        logger.info(
            "[TaskTracker] task %s started for user %s", self.task_id, self.user_id
        )

    def milestone(self, message: str, progress: float | None = None) -> None:
        """Record a milestone. Pushes immediately if throttle allows, else just logs to Redis."""
        now = time.time()
        data: dict[str, Any] = {
            "step": message,
            "last_heartbeat": datetime.now(timezone.utc).isoformat(),
        }
        if progress is not None:
            data["progress"] = progress

        self._milestones.append(
            {
                "time": datetime.now(timezone.utc).isoformat(),
                "message": message,
                "progress": progress,
            }
        )
        self._save_redis("running", data)

        if now - self._last_push >= self.heartbeat_interval:
            elapsed = _format_duration(self._start_ts)
            self._notify(
                _format_progress_message(self.task_id, message, progress, elapsed)
            )
            self._last_push = now
        else:
            logger.debug("[TaskTracker] milestone throttled: %s", message)

    def tool_call(self, tool_name: str, arguments: dict) -> None:
        """Push tool execution notification (no throttle, always immediate).

        Args:
            tool_name: Tool name (e.g., "get_kline_data")
            arguments: Tool arguments dict (user_id will be filtered out)
        """
        # Filter out internal args
        display_args = {
            k: v for k, v in arguments.items() if k not in ("user_id", "agent_name")
        }

        # Format args: key=value, key=value
        args_str = ", ".join(
            f"{k}={repr(v)[:50]}"  # Truncate long values
            for k, v in display_args.items()
        )

        # Build notification text
        text = f"🔧 执行工具：{tool_name}（{args_str}）"

        # Push immediately (no throttle check)
        self._notify(text)

        # Update last_alive (existing behavior)
        self.alive()

        logger.debug("[TaskTracker] tool_call pushed: %s(%s)", tool_name, args_str)

    def alive(self) -> None:
        """Update last_alive timestamp (call after long tool executions)."""
        self._save_redis(
            "running",
            {
                "last_alive": datetime.now(timezone.utc).isoformat(),
            },
        )

    def complete(self, result: str) -> None:
        """Mark task completed: push final result, update Redis, trigger async archive."""
        elapsed = _format_duration(self._start_ts)
        short_id = self.task_id
        text = f"✅ 任务 #{short_id} 完成\n耗时：{elapsed}\n结果：{result}"
        self._notify(text)
        self._save_redis(
            "completed",
            {
                "result": result,
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "progress": 1.0,
                "milestones": json.dumps(self._milestones, ensure_ascii=False),
            },
        )
        self._trigger_archive(result)
        logger.info("[TaskTracker] task %s completed", self.task_id)

    def fail(self, error: str) -> None:
        """Mark task failed: push error, update Redis, archive."""
        short_id = self.task_id
        text = f"❌ 任务 #{short_id} 失败\n错误：{error}"
        self._notify(text)
        self._save_redis(
            "failed",
            {
                "result": f"ERROR: {error}",
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "milestones": json.dumps(self._milestones, ensure_ascii=False),
            },
        )
        self._trigger_archive(f"ERROR: {error}")
        logger.error("[TaskTracker] task %s failed: %s", self.task_id, error)

    def stop(self) -> None:
        """Clean up tracker state (called in finally block)."""
        logger.info("[TaskTracker] task %s stopped", self.task_id)

    @staticmethod
    def cancel(task_id: str) -> bool:
        """Mark a running task as cancelled.

        Returns True if the task was found and marked, False otherwise.
        """
        import redis
        from django.conf import settings

        url = settings.REDIS_URL
        if url.rsplit("/", 1)[-1].isdigit():
            url = url.rsplit("/", 1)[0] + "/3"

        try:
            r = redis.from_url(url, decode_responses=True)
            key = f"task:progress:{task_id}"
            if not r.exists(key):
                return False
            r.hset(key, "status", "cancelled")
            logger.info("[TaskTracker] task %s cancelled", task_id)
            return True
        except Exception:
            logger.warning(
                "[TaskTracker] failed to cancel task %s", task_id, exc_info=True
            )
            return False

    def is_cancelled(self) -> bool:
        """Check if this task has been cancelled by the user."""
        import redis
        from django.conf import settings

        url = settings.REDIS_URL
        if url.rsplit("/", 1)[-1].isdigit():
            url = url.rsplit("/", 1)[0] + "/3"

        try:
            r = redis.from_url(url, decode_responses=True)
            status = r.hget(self._redis_key, "status")
            return status == "cancelled"
        except Exception:
            return False

    @classmethod
    def get_current(cls) -> TaskTracker | None:
        """Get tracker from current context (for use in nested helper functions)."""
        return tracker_context.get()

    @staticmethod
    def record_submitted(
        task_id: str,
        user_id: str = "",
        task_type: str = "celery",
        metadata: dict | None = None,
    ) -> None:
        """Write a submission record to Redis Hash immediately after apply_async.

        This allows get_task_result to distinguish "never submitted" from
        "submitted but not yet started" — Celery AsyncResult returns PENDING
        for both cases.

        Called from submit_backtest / submit_grid_search right after
        apply_async succeeds.
        """
        import redis
        from django.conf import settings

        url = settings.REDIS_URL
        if url.rsplit("/", 1)[-1].isdigit():
            url = url.rsplit("/", 1)[0] + "/3"

        data: dict[str, Any] = {
            "task_id": task_id,
            "user_id": user_id,
            "task_type": task_type,
            "status": "submitted",
            "submitted_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if metadata:
            data["metadata"] = json.dumps(metadata, ensure_ascii=False)

        try:
            r = redis.from_url(url, decode_responses=True)
            pipe = r.pipeline()
            pipe.hset(f"task:progress:{task_id}", mapping=data)
            pipe.expire(f"task:progress:{task_id}", 86400)  # 24h TTL
            pipe.execute()
            logger.info(
                "[TaskTracker] recorded submission for task %s (type=%s)",
                task_id,
                task_type,
            )
        except Exception:
            logger.warning(
                "[TaskTracker] failed to record submission for %s",
                task_id,
                exc_info=True,
            )

    @staticmethod
    def get_submission_status(task_id: str) -> str | None:
        """Check Redis Hash for task submission status.

        Returns:
            'submitted' | 'running' | 'completed' | 'failed' | 'zombie' |
            'zombie_retrying' | 'dlq' | None (key not found)
        """
        import redis
        from django.conf import settings

        url = settings.REDIS_URL
        if url.rsplit("/", 1)[-1].isdigit():
            url = url.rsplit("/", 1)[0] + "/3"

        try:
            r = redis.from_url(url, decode_responses=True)
            key = f"task:progress:{task_id}"
            if not r.exists(key):
                return None
            return r.hget(key, "status") or "unknown"
        except Exception:
            logger.warning(
                "[TaskTracker] failed to read submission status for %s",
                task_id,
                exc_info=True,
            )
            return None

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _resolve_channel(self) -> str:
        """Auto-detect user's active channel from DB."""
        try:
            from apps.channel.channel_resolver import get_user_active_channel

            target = get_user_active_channel(self.user_id)
            if target:
                return target.channel_type
        except Exception:
            pass
        return "telegram"

    def _save_redis(self, status: str, data: dict) -> None:
        """Write progress to Redis Hash with TTL."""
        import redis

        from django.conf import settings

        url = settings.REDIS_URL
        if url.rsplit("/", 1)[-1].isdigit():
            url = url.rsplit("/", 1)[0] + "/3"

        try:
            r = redis.from_url(url, decode_responses=True)
            pipe = r.pipeline()
            pipe.hset(
                self._redis_key,
                mapping={
                    "task_id": self.task_id,
                    "user_id": self.user_id,
                    "channel": self.channel,
                    "task_type": self.task_type,
                    "status": status,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    **data,
                },
            )
            pipe.expire(self._redis_key, 86400)  # 24h TTL
            pipe.execute()
        except Exception:
            logger.warning(
                "[TaskTracker] failed to write Redis progress", exc_info=True
            )

    def _notify(self, text: str) -> None:
        """Send notification to user via Redis pubsub (consumed by ASGI process)."""
        try:
            _send_notification_redis(self.user_id, text)
        except Exception:
            logger.warning("[TaskTracker] notification delivery failed", exc_info=True)

    def _trigger_archive(self, result: str) -> None:
        """Persist tracker data to PostgreSQL.

        If called from an async context (e.g. Redis Stream consumer), the ORM
        write is scheduled via sync_to_async to avoid SynchronousOnlyOperation.
        In a sync context (e.g. Celery task), the ORM is called directly.

        Connection recovery: On OperationalError (e.g. PostgreSQL restart),
        close the stale connection and retry once before falling back to Celery.

        Always fires the Celery task as a best-effort backup.
        """
        import asyncio
        from django.db import connection, OperationalError
        from apps.agent.models import TaskProgress

        defaults = {
            "task_type": self.task_type,
            "status": self._get_current_status(),
            "progress": self._get_current_progress(),
            "milestones": self._milestones,
            "result": result,
            "completed_at": datetime.now(timezone.utc),
            "user_id": self.user_id if len(self.user_id) == 36 else None,
        }

        def _do_sync_archive() -> None:
            """Sync archive with one retry on connection error."""
            try:
                TaskProgress.objects.update_or_create(
                    task_id=self.task_id,
                    defaults=defaults,
                )
            except OperationalError:
                # Connection lost (e.g. PostgreSQL restart). Close stale
                # connection and retry once — Django will reopen automatically.
                logger.info(
                    "[TaskTracker] connection lost for %s, retrying after reconnect",
                    self.task_id,
                )
                connection.close()
                TaskProgress.objects.update_or_create(
                    task_id=self.task_id,
                    defaults=defaults,
                )

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Sync context: call ORM directly
            try:
                _do_sync_archive()
            except Exception:
                logger.info(
                    "[TaskTracker] direct PG archive failed for %s, falling back to Celery",
                    self.task_id,
                    exc_info=True,
                )
                self._archive_via_celery(result)
        else:
            # Async context: schedule via sync_to_async
            async def _do_archive() -> None:
                try:
                    from asgiref.sync import sync_to_async as sta

                    await sta(_do_sync_archive)()
                except Exception:
                    logger.info(
                        "[TaskTracker] async PG archive failed for %s, "
                        "falling back to Celery",
                        self.task_id,
                        exc_info=True,
                    )
                    self._archive_via_celery(result)

            loop.create_task(_do_archive())

    def _archive_via_celery(self, result: str) -> None:
        """Dispatch archive to Celery as a fallback."""
        try:
            from apps.agent.tasks import archive_task_progress

            archive_task_progress.delay(
                task_id=self.task_id,
                user_id=self.user_id,
                task_type=self.task_type,
                status=self._get_current_status(),
                progress=self._get_current_progress(),
                milestones=self._milestones,
                result=result,
            )
        except Exception:
            logger.warning(
                "[TaskTracker] Celery archive dispatch also failed for %s",
                self.task_id,
                exc_info=True,
            )

    def _get_current_status(self) -> str:
        try:
            import redis
            from django.conf import settings

            url = settings.REDIS_URL
            if url.rsplit("/", 1)[-1].isdigit():
                url = url.rsplit("/", 1)[0] + "/3"
            r = redis.from_url(url, decode_responses=True)
            return r.hget(self._redis_key, "status") or "unknown"
        except Exception:
            return "unknown"

    def _get_current_progress(self) -> float:
        try:
            import redis
            from django.conf import settings

            url = settings.REDIS_URL
            if url.rsplit("/", 1)[-1].isdigit():
                url = url.rsplit("/", 1)[0] + "/3"
            r = redis.from_url(url, decode_responses=True)
            val = r.hget(self._redis_key, "progress")
            return float(val) if val else 0.0
        except Exception:
            return 0.0


# ---------------------------------------------------------------------------
# Context manager for clean setup/teardown
# ---------------------------------------------------------------------------


@contextmanager
def track_task(
    task_id: str,
    user_id: str,
    task_type: str = "agent",
    initial_message: str = "任务已启动",
):
    """Context manager that creates tracker, sets context, and cleans up."""
    tracker = TaskTracker(task_id=task_id, user_id=user_id, task_type=task_type)
    token = tracker_context.set(tracker)
    tracker.start(initial_message)
    try:
        yield tracker
    except Exception as e:
        tracker.fail(str(e))
        raise
    finally:
        tracker_context.reset(token)
