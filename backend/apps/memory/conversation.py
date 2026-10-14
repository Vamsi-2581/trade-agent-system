"""ConversationLog 的两个实现：Redis（生产）+ 内存（测试）。

设计要点：
- user 级 seam，构造参数只需 user_id
- 压缩逻辑在这里实现，但 LLM 调用通过注入的 Compressor 协议
- append 用 asyncio.Lock 保护（每次新建，避免 Celery fork 绑定到旧事件循环）
- 不读旧 key（conv:supervisor:{user_id}）— TTL 24h，老数据早已过期
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from apps.memory.protocols import Compressor
from apps.memory.keys import MemoryKey
from apps.memory.redis_client import RedisPool

logger = logging.getLogger(__name__)


class RedisConversationLog:
    """生产实现：Redis LIST + 可选压缩。

    Attributes:
        _redis: Redis 客户端；初始化失败则为 None，所有操作静默降级
        _compressor: 注入的压缩器；None 表示不压缩
    """

    def __init__(
        self,
        user_id: str,
        *,
        compressor: Compressor | None = None,
        compress_threshold_tokens: int = 32_000,
        keep_recent_rounds: int = 10,
        ttl_seconds: int = 86_400,
    ) -> None:
        self._user_id = user_id
        self._key = MemoryKey.conv_history(user_id)
        self._compressor = compressor
        self._compress_threshold_tokens = compress_threshold_tokens
        self._keep_recent_rounds = keep_recent_rounds
        self._ttl = ttl_seconds

        self._redis = None
        try:
            self._redis = RedisPool.get_client()
        except Exception as e:
            logger.warning(
                "RedisConversationLog: Redis unavailable, degenerate mode: %s", e
            )

    # ====== 核心接口 ======
    async def append(
        self, entries: list[dict], *, keep_turns: int | None = None
    ) -> list[dict]:
        """原子追加：读-拼接-截断-写，返回最终历史。"""
        lock = asyncio.Lock()
        async with lock:
            current = await self.get(max_turns=None)
            updated = current + entries
            if keep_turns is not None:
                updated = updated[-keep_turns:]
            await self._save(updated)
            return updated

    async def get(self, *, max_turns: int | None = None) -> list[dict]:
        """读取历史，自动触发压缩。"""
        raw = await self._load()
        raw = await self._auto_compress_if_needed(raw)
        return raw if max_turns is None else raw[-max_turns:]

    async def clear(self) -> None:
        if self._redis is None:
            return
        try:
            await self._redis.delete(self._key)
        except Exception as e:
            logger.debug("RedisConversationLog.clear failed: %s", e)

    # ====== 内部 ======
    async def _load(self) -> list[dict]:
        if self._redis is None:
            return []
        try:
            raw = await self._redis.get(self._key)
            return json.loads(raw) if raw else []
        except Exception as e:
            logger.debug("RedisConversationLog._load failed: %s", e)
            return []

    async def _save(self, history: list[dict]) -> None:
        if self._redis is None:
            logger.debug("RedisConversationLog._save skipped (Redis unavailable)")
            return
        try:
            await self._redis.set(self._key, json.dumps(history), ex=self._ttl)
        except Exception as e:
            logger.debug("RedisConversationLog._save failed: %s", e)

    async def _auto_compress_if_needed(self, conv: list[dict]) -> list[dict]:
        """超过阈值时压缩旧轮次，保留最近 keep_recent_rounds*2 条。

        压缩失败（LLM 错误等）时降级：返回原历史，不中断调用。
        """
        if self._compressor is None:
            return conv

        total_chars = sum(len(json.dumps(item)) for item in conv)
        estimated_tokens = total_chars // 2
        if estimated_tokens <= self._compress_threshold_tokens:
            return conv

        keep_messages = self._keep_recent_rounds * 2
        if len(conv) <= keep_messages:
            return conv

        to_compress = conv[:-keep_messages]
        to_keep = conv[-keep_messages:]

        try:
            summary = await self._compressor.compress(to_compress)
        except Exception as e:
            logger.warning("ConversationLog compression failed, using original: %s", e)
            return conv

        compressed = [
            {
                "role": "system",
                "text": summary,
                "ts": int(time.time()),
                "compressed": True,
                "original_turns": len(to_compress),
            }
        ] + to_keep

        await self._save(compressed)
        logger.info(
            "ConversationLog compressed | user=%s | turns=%d → %d",
            self._user_id,
            len(conv),
            len(compressed),
        )
        return compressed


class InMemoryConversationLog:
    """测试实现：纯内存 dict，行为与 RedisConversationLog 对齐。

    同样支持 Compressor 注入，便于测试压缩路径。
    """

    def __init__(
        self,
        *,
        compressor: Compressor | None = None,
        compress_threshold_tokens: int = 32_000,
        keep_recent_rounds: int = 10,
    ) -> None:
        self._history: list[dict] = []
        self._compressor = compressor
        self._compress_threshold_tokens = compress_threshold_tokens
        self._keep_recent_rounds = keep_recent_rounds

    async def append(
        self, entries: list[dict], *, keep_turns: int | None = None
    ) -> list[dict]:
        self._history = self._history + entries
        if keep_turns is not None:
            self._history = self._history[-keep_turns:]
        self._history = await self._auto_compress_if_needed(self._history)
        return list(self._history)

    async def get(self, *, max_turns: int | None = None) -> list[dict]:
        result = await self._auto_compress_if_needed(list(self._history))
        return result if max_turns is None else result[-max_turns:]

    async def clear(self) -> None:
        self._history = []

    async def _auto_compress_if_needed(self, conv: list[dict]) -> list[dict]:
        if self._compressor is None:
            return conv
        total_chars = sum(len(json.dumps(item)) for item in conv)
        if total_chars // 2 <= self._compress_threshold_tokens:
            return conv
        keep_messages = self._keep_recent_rounds * 2
        if len(conv) <= keep_messages:
            return conv
        summary = await self._compressor.compress(conv[:-keep_messages])
        return [
            {
                "role": "system",
                "text": summary,
                "ts": int(time.time()),
                "compressed": True,
                "original_turns": len(conv) - keep_messages,
            }
        ] + conv[-keep_messages:]
