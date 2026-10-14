from __future__ import annotations

import asyncio
import json
import logging
import socket


logger = logging.getLogger(__name__)

_CONSUMER_NAME = f"dlq-recovery-{socket.gethostname()}"
_DLQ_STREAM = "agent:tasks:dlq"
_DLQ_GROUP = "agents-dlq"


class DeadLetterConsumer:
    """Standalone stream consumer for the Dead Letter Queue.

    Uses persistent XREADGROUP with a long BLOCK.  Does NOT auto-republish —
    instead notifies the user and ACKs.  The user must /retry manually.
    """

    def __init__(self, block_ms: int = 10000):
        self._block_ms = block_ms
        self._running = False
        self._poll_task: asyncio.Task | None = None

    async def start(self) -> None:
        await self._ensure_group()
        self._running = True
        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info("[DeadLetterConsumer] started consumer=%s", _CONSUMER_NAME)

    async def stop(self) -> None:
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except (asyncio.CancelledError, RuntimeError):
                pass
        logger.info("[DeadLetterConsumer] stopped")

    async def _ensure_group(self) -> None:
        r = await self._get_redis()
        try:
            await r.xgroup_create(_DLQ_STREAM, _DLQ_GROUP, id="0", mkstream=True)
        except Exception as e:
            if "BUSYGROUP" not in str(e):
                raise
        finally:
            await r.aclose()

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                r = await self._get_redis()
                try:
                    results = await r.xreadgroup(
                        groupname=_DLQ_GROUP,
                        consumername=_CONSUMER_NAME,
                        streams={_DLQ_STREAM: ">"},
                        count=4,
                        block=self._block_ms,
                    )
                finally:
                    await r.aclose()

                if not results:
                    continue

                for _stream, entries in results:
                    for msg_id, fields in entries:
                        await self._handle_dlq_message(msg_id, fields)

            except asyncio.CancelledError:
                break
            except RuntimeError:
                break
            except Exception:
                logger.error("[DeadLetterConsumer] poll error", exc_info=True)
                try:
                    await asyncio.sleep(5)
                except (asyncio.CancelledError, RuntimeError):
                    break

    async def _handle_dlq_message(self, msg_id: str, fields: dict) -> None:
        decoded = {}
        for k, v in fields.items():
            try:
                decoded[k] = json.loads(v)
            except (json.JSONDecodeError, TypeError):
                decoded[k] = v

        user_id = decoded.get("user_id", "")
        task_id = decoded.get("task_id", msg_id)[:8]
        dlq_reason = decoded.get("dlq_reason", "unknown")

        r = await self._get_redis()
        try:
            await r.xack(_DLQ_STREAM, _DLQ_GROUP, msg_id)
        finally:
            await r.aclose()

        if user_id:
            await _async_send_notification(
                user_id,
                f"任务 #{task_id} 处理失败（{dlq_reason}）\n请使用 /retry 命令重新提交",
            )

        logger.info(
            "[DeadLetterConsumer] DLQ message %s notified user=%s reason=%s",
            msg_id,
            user_id,
            dlq_reason,
        )

    async def _get_redis(self):
        import redis.asyncio as aioredis
        from django.conf import settings

        url = settings.REDIS_URL
        if url.rsplit("/", 1)[-1].isdigit():
            url = url.rsplit("/", 1)[0] + "/3"
        return aioredis.from_url(url, decode_responses=True)


async def _async_send_notification(user_id: str, text: str) -> None:
    """Async notification via Redis pubsub (fire-and-forget)."""
    import redis.asyncio as aioredis
    from datetime import datetime, timezone
    from django.conf import settings

    url = settings.REDIS_URL
    if url.rsplit("/", 1)[-1].isdigit():
        url = url.rsplit("/", 1)[0] + "/3"

    try:
        r = aioredis.from_url(url, decode_responses=True)
        await r.publish(
            "task:progress:notifications",
            json.dumps(
                {
                    "user_id": user_id,
                    "text": text,
                    "at": datetime.now(timezone.utc).isoformat(),
                }
            ),
        )
        await r.aclose()
    except Exception:
        logger.warning(
            "[DeadLetterConsumer] failed to publish notification", exc_info=True
        )
