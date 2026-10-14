"""WebSocket 断线待发消息缓存。

消息以 Redis LIST (FIFO) 存储，key = ws:pending:{user_id}。
connect 时取出并投递，投递成功即删除。
"""

import asyncio
import json
import logging
import os

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

_CLIENTS: dict[int, tuple[aioredis.Redis, asyncio.AbstractEventLoop | None]] = {}
_MAX_CONNECTIONS = 10

TTL_SECONDS = 3600  # 待发消息 1 小时后过期


def _get_client() -> aioredis.Redis:
    """获取 PID 隔离的 Redis 客户端。"""
    from django.conf import settings

    pid = os.getpid()
    if pid not in _CLIENTS:
        url = f"{settings.REDIS_URL}/{settings.REDIS_DB_WS_PENDING}"
        client = aioredis.from_url(
            url,
            max_connections=_MAX_CONNECTIONS,
            decode_responses=True,
        )
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = None
        _CLIENTS[pid] = (client, loop)
    return _CLIENTS[pid][0]


def _reset_client() -> None:
    """关闭并清除当前 PID 的 Redis 客户端。

    必须在事件循环仍然可用时调用（loop 不能已经 close）——
    否则连接对象的 __del__ 在 GC 时会访问已关闭的 loop，
    触发 ``RuntimeError: Event loop is closed``。
    """
    pid = os.getpid()
    entry = _CLIENTS.pop(pid, None)
    if entry is None:
        return
    client, stored_loop = entry

    try:
        running_loop = asyncio.get_running_loop()
    except RuntimeError:
        running_loop = None

    if running_loop is not None and running_loop.is_running():
        try:
            running_loop.create_task(client.aclose())
        except Exception:
            pass
        return

    loop = stored_loop
    if loop is None or loop.is_closed():
        try:
            loop = asyncio.get_event_loop_policy().get_event_loop()
        except RuntimeError:
            loop = None

    if loop is not None and not loop.is_closed():
        try:
            loop.run_until_complete(client.aclose())
            return
        except Exception:
            pass

    # Last resort: sync-close the underlying transports.
    try:
        pool = client.connection_pool
        for conn in list(getattr(pool, "_available_connections", [])):
            _sync_close_connection(conn)
        for conn in list(getattr(pool, "_in_use_connections", set())):
            _sync_close_connection(conn)
        pool.reset()
    except Exception:
        pass


def _sync_close_connection(conn) -> None:
    try:
        writer = getattr(conn, "_writer", None)
        if writer is not None:
            transport = getattr(writer, "_transport", None)
            if transport is not None and not transport.is_closing():
                transport.close()
    except Exception:
        pass


def _key(user_id: str) -> str:
    return f"ws:pending:{user_id}"


async def store(user_id: str, message: dict) -> None:
    """存储一条待发消息。"""
    try:
        r = _get_client()
        k = _key(user_id)
        pipe = r.pipeline()
        pipe.rpush(k, json.dumps(message, ensure_ascii=False))
        pipe.expire(k, TTL_SECONDS)
        await pipe.execute()
    except Exception:
        logger.warning("[WSPending] store failed for user %s", user_id, exc_info=True)


async def peek(user_id: str) -> list[dict]:
    """Read without deleting; only acknowledge after a successful socket send."""
    r = _get_client()
    values = await r.lrange(_key(user_id), 0, -1)
    return [json.loads(value) for value in values]


async def ack(user_id: str, message: dict) -> None:
    """Remove this exact envelope, scoped to the authenticated user."""
    r = _get_client()
    await r.lrem(_key(user_id), 1, json.dumps(message, ensure_ascii=False))


async def ack_by_id(user_id: str, delivery_id: str) -> None:
    """按 delivery_id 移除待投条目（浏览器确认收到后调用，幂等）。

    与 ``ack``（整包匹配）互补：客户端只回传 delivery_id，
    这里扫描列表找到匹配条目后按原始 JSON 精确删除。
    """
    if not user_id or not delivery_id:
        return
    try:
        r = _get_client()
        k = _key(user_id)
        entries = await r.lrange(k, 0, -1)
        for raw in entries:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if msg.get("delivery_id") == delivery_id:
                await r.lrem(k, 1, raw)
                return
    except Exception:
        logger.warning(
            "[WSPending] ack_by_id failed for user %s", user_id, exc_info=True
        )


async def drain(user_id: str) -> list[dict]:
    """取出并删除用户的所有待发消息，按入队顺序返回。"""
    try:
        r = _get_client()
        k = _key(user_id)
        pipe = r.pipeline()
        pipe.lrange(k, 0, -1)
        pipe.delete(k)
        results, _ = await pipe.execute()
        if results:
            return [json.loads(m) for m in results]
        return []
    except Exception:
        logger.warning("[WSPending] drain failed for user %s", user_id, exc_info=True)
        return []
