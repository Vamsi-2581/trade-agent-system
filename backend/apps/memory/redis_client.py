"""Redis connection pool with PID-based isolation for Celery fork safety."""

import asyncio
import logging
import os

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

# PID -> (client, loop_used).  The loop reference is captured at creation
# time so close_client() can run_until_complete(client.aclose()) on the
# correct loop even when no loop is currently running (e.g. after a
# Celery task's run_until_complete returned).  Without this, we'd either
# call the async Redis.close() synchronously (returns an unawaited
# coroutine — connections stay open) or close the loop first (connections
# then emit "RuntimeError: Event loop is closed" from __del__ on GC).
_CLIENTS: dict[int, tuple[aioredis.Redis, asyncio.AbstractEventLoop | None]] = {}
_MAX_CONNECTIONS = 20


class RedisPool:
    """PID-isolated Redis connection pool.

    Each process (identified by PID) gets its own Redis client instance.
    This prevents connection sharing issues when Celery forks workers.
    """

    @staticmethod
    def get_client() -> aioredis.Redis:
        """Get or create a Redis client for the current process."""
        from django.conf import settings

        pid = os.getpid()
        if pid not in _CLIENTS:
            url = getattr(settings, "REDIS_MEMORY_URL", None)
            if not url:
                url = settings.REDIS_URL + "/6"
            client = aioredis.from_url(
                url,
                max_connections=_MAX_CONNECTIONS,
                decode_responses=True,
            )
            # Capture the loop the client will bind its connections to.
            # Connections are created lazily on first await, but they
            # bind to whatever loop is running at that moment — which
            # is the same loop that's current when we return to the
            # caller's run_until_complete.
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = None
            _CLIENTS[pid] = (client, loop)
            logger.debug("[RedisPool] Created client for PID %d", pid)
        return _CLIENTS[pid][0]

    @staticmethod
    def close_client() -> None:
        """Close the Redis client for the current process.

        Handles three scenarios:
        1. Running event loop → schedule aclose() asynchronously (fire-and-forget)
        2. Stopped but not closed loop → run_until_complete(aclose())
        3. Closed / no loop → best-effort sync disconnect of underlying sockets
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
            # Loop is actively running (e.g. event loop thread) — can't block
            # with run_until_complete. Schedule aclose() as a background task.
            try:
                running_loop.create_task(client.aclose())
            except Exception as e:
                logger.warning(
                    "[RedisPool] Failed to schedule client close for PID %d: %s",
                    pid,
                    e,
                )
            return

        # No running loop.  Prefer the loop we captured at creation time —
        # it's the one the client's connections are bound to.  Fall back to
        # any non-closed loop discoverable on this thread.
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
            except Exception as e:
                logger.warning(
                    "[RedisPool] Failed to close client for PID %d: %s", pid, e
                )
                # Fall through to sync best-effort.

        # Last resort: synchronously tear down the underlying sockets.
        # `client.close()` and `connection_pool.disconnect()` are both
        # async coroutines in redis-py 5.x and cannot be called here —
        # iterate the pool's connections directly and close their
        # transports.  This is sync-safe because each connection's
        # `_transport.close()` just schedules I/O teardown on the loop
        # the transport was bound to.
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
    """Best-effort sync close of an asyncio Redis connection.

    Called only when no event loop is available to await aclose().  Closes
    the underlying transport directly so __del__ on the connection later
    won't try to call into a closed loop.
    """
    try:
        writer = getattr(conn, "_writer", None)
        if writer is not None:
            transport = getattr(writer, "_transport", None)
            if transport is not None and not transport.is_closing():
                transport.close()
    except Exception:
        pass
