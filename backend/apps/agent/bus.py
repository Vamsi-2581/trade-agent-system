from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# Stream 键名
AGENT_TASKS = "agent:tasks"
TRADING_ORDERS = "trading:orders"
TRADING_POSITIONS = "trading:positions"
RISK_EVENTS = "risk:events"
SYSTEM_HEARTBEAT = "system:heartbeat"

# Consumer groups
CG_AGENTS = "agents"
CG_EXECUTOR = "executor"
CG_MONITOR = "monitor"

# DB3: agent:tasks / system:heartbeat
# DB4: trading:* / risk:events
_AGENT_DB_STREAMS = {AGENT_TASKS, SYSTEM_HEARTBEAT}
_TRADING_DB_STREAMS = {TRADING_ORDERS, TRADING_POSITIONS, RISK_EVENTS}

_MAX_RETRY = 3
_RETRY_DELAYS = [1, 5, 30]  # seconds
_MAX_LEN = 10_000  # MAXLEN for each stream

# Stream → consumer group mapping
_DEFAULT_GROUPS: dict[str, str] = {
    AGENT_TASKS: CG_AGENTS,
    TRADING_ORDERS: CG_EXECUTOR,
    RISK_EVENTS: CG_MONITOR,
}


def _redis_url(stream: str) -> str:
    """根据 stream 返回对应 Redis DB 的 URL"""
    from django.conf import settings

    base = settings.REDIS_URL.rstrip("/")
    # strip existing db suffix if present
    if base.rsplit("/", 1)[-1].isdigit():
        base = base.rsplit("/", 1)[0]
    if stream in _AGENT_DB_STREAMS:
        db = getattr(settings, "REDIS_DB_AGENT_STREAM", 3)
    else:
        db = getattr(settings, "REDIS_DB_TRADING_STREAM", 4)
    return f"{base}/{db}"


async def _get_redis(stream: str):
    """返回对应 DB 的 aioredis 连接（调用方负责 aclose）"""
    import redis.asyncio as aioredis

    return aioredis.from_url(_redis_url(stream), decode_responses=True)


async def ensure_groups() -> None:
    """幂等创建所有 consumer group（系统启动时调用一次）"""
    for stream, group in _DEFAULT_GROUPS.items():
        r = await _get_redis(stream)
        try:
            # MKSTREAM 确保 stream 不存在时自动创建
            await r.xgroup_create(stream, group, id="0", mkstream=True)
            logger.info("[bus] created consumer group %s on %s", group, stream)
        except Exception as e:
            if "BUSYGROUP" in str(e):
                pass  # group 已存在，忽略
            else:
                logger.warning(
                    "[bus] ensure_groups error on %s/%s: %s", stream, group, e
                )
        finally:
            await r.aclose()


async def publish(stream: str, payload: dict[str, Any]) -> str:
    """
    发布消息到 stream，返回 message_id。
    payload 中不需要包含 task_id/created_at，会自动注入。
    """
    if "task_id" not in payload:
        payload["task_id"] = str(uuid.uuid4())
    if "created_at" not in payload:
        payload["created_at"] = datetime.now(timezone.utc).isoformat()

    r = await _get_redis(stream)
    try:
        # Redis Stream 要求所有字段为字符串
        fields = {
            k: json.dumps(v) if not isinstance(v, str) else v
            for k, v in payload.items()
        }
        msg_id = await r.xadd(stream, fields, maxlen=_MAX_LEN, approximate=True)
        logger.debug(
            "[bus] published to %s id=%s task_id=%s",
            stream,
            msg_id,
            payload.get("task_id"),
        )
        return msg_id
    finally:
        await r.aclose()


async def consume(
    stream: str,
    group: str,
    consumer: str,
    count: int = 10,
    block_ms: int = 5000,
) -> list[tuple[str, dict]]:
    """
    从 consumer group 读取消息。
    返回 [(message_id, fields_dict), ...]
    """
    r = await _get_redis(stream)
    try:
        results = await r.xreadgroup(
            groupname=group,
            consumername=consumer,
            streams={stream: ">"},
            count=count,
            block=block_ms,
        )
        if not results:
            return []
        messages = []
        for _stream, entries in results:
            for msg_id, fields in entries:
                decoded = {}
                for k, v in fields.items():
                    try:
                        decoded[k] = json.loads(v)
                    except (json.JSONDecodeError, TypeError):
                        decoded[k] = v
                messages.append((msg_id, decoded))
        return messages
    finally:
        await r.aclose()


async def ack(stream: str, group: str, message_id: str) -> None:
    """ACK 消息，从 PEL 中移除"""
    r = await _get_redis(stream)
    try:
        await r.xack(stream, group, message_id)
    finally:
        await r.aclose()


async def nack_and_retry(
    stream: str,
    payload: dict[str, Any],
    retry_count: int,
) -> None:
    """
    消费失败时处理：
    - retry_count < MAX_RETRY: 等待退避时间后重新发布
    - retry_count >= MAX_RETRY: 写入 DLQ（{stream}:dlq）
    调用方先 ack 原消息再调用此函数。
    """
    if retry_count >= _MAX_RETRY:
        # Save original_task to Redis Hash so check_task_health can recover it.
        # Best-effort: must not block DLQ publish if Redis write fails.
        task_id = payload.get("task_id", "")
        if task_id:
            redis_key = f"task:progress:{task_id}"
            try:
                r = await _get_redis(stream)
                try:
                    await r.hset(
                        redis_key,
                        mapping={
                            "status": "dlq",
                            "retry_count": str(retry_count),
                            "original_task": json.dumps(payload),
                            "updated_at": datetime.now(timezone.utc).isoformat(),
                        },
                    )
                    await r.expire(redis_key, 86400)
                except Exception:
                    logger.exception(
                        "[bus] failed to save original_task for %s, continuing to DLQ",
                        task_id,
                    )
                finally:
                    await r.aclose()
            except Exception:
                logger.exception(
                    "[bus] failed to get Redis for %s, continuing to DLQ", task_id
                )

        dlq = f"{stream}:dlq"
        dlq_payload = dict(payload)
        dlq_payload["dlq_reason"] = f"exceeded {_MAX_RETRY} retries"
        dlq_payload["original_stream"] = stream
        dlq_payload["retry_count"] = str(retry_count)
        await publish(dlq, dlq_payload)
        logger.error(
            "[bus] message moved to DLQ %s task_id=%s", dlq, payload.get("task_id")
        )
    else:
        delay = _RETRY_DELAYS[min(retry_count, len(_RETRY_DELAYS) - 1)]
        logger.warning(
            "[bus] retry %d/%d in %ds for task_id=%s",
            retry_count + 1,
            _MAX_RETRY,
            delay,
            payload.get("task_id"),
        )
        await asyncio.sleep(delay)
        retry_payload = dict(payload)
        retry_payload["retry_count"] = str(retry_count + 1)
        await publish(stream, retry_payload)


async def publish_reply(task_id: str, result: str, ttl: int = 60) -> None:
    """
    写入任务结果到 agent:reply:{task_id}（供 Channel 等待消费）。
    使用 Redis List + EXPIRE 实现一次性信箱。
    """
    r = await _get_redis(AGENT_TASKS)
    try:
        key = f"agent:reply:{task_id}"
        await r.rpush(key, result)
        await r.expire(key, ttl)
    finally:
        await r.aclose()


async def wait_reply(task_id: str, timeout: int = 30) -> str | None:
    """
    阻塞等待任务结果，timeout 秒后返回 None。
    由 Channel 调用，等待 SupervisorConsumer 处理完成。
    """
    r = await _get_redis(AGENT_TASKS)
    try:
        key = f"agent:reply:{task_id}"
        res = await r.blpop(key, timeout=timeout)
        if res:
            return res[1]  # (key, value)
        return None
    finally:
        await r.aclose()


def build_agent_task(
    user_id: str,
    payload: dict,
    task_id: str | None = None,
    priority: int = 2,
    timeout_ms: int = 30_000,
    origin: str = "",
) -> dict:
    """
    构造 agent:tasks 消息体。
    返回 dict 包含 task_id，调用方可用于 wait_reply()。

    origin: 下达命令的 gateway（web / lark / telegram / tui / ...），
            供 AgentTaskConsumer 在回复/通知时做多通道扇出（下发到 origin
            之外的通道，如 web WS 与主通道飞书）。
    """
    tid = task_id or str(uuid.uuid4())
    return {
        "task_id": tid,
        "user_id": user_id,
        "priority": str(priority),
        "payload": json.dumps(payload),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "timeout_ms": str(timeout_ms),
        "origin": origin,
    }
