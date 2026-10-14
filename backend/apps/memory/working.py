"""WorkingMemory 的两个实现：Redis（生产）+ 内存（测试）。

设计要点：
- per-(user, agent) seam
- scope 三态：private / shared / both（默认 both，与旧 write_l2(shared=True) 一致）
- recall 走关键词匹配：先按分词匹配，回退到整串子串；shared 排除同 agent 自写
- Redis 实现用 pipeline 保证 private+shared 原子写
- InMemory 实现通过 shared_pool 参数支持多实例共享（测 supervisor 多 agent 协调）
"""

from __future__ import annotations

import json
import logging
import time
from typing import Literal

from apps.memory.entries import MemoryEntry
from apps.memory.keys import MemoryKey
from apps.memory.redis_client import RedisPool

logger = logging.getLogger(__name__)


def _parse_query(query: str) -> tuple[str, list[str]]:
    """分词：仅保留长度 > 2 的小写 token。"""
    query_lower = query.lower()
    query_words = [w for w in query_lower.split() if len(w) > 2]
    return query_lower, query_words


def _matches(text: str, query_lower: str, query_words: list[str]) -> bool:
    """与旧 _retrieve_l2_* 行为对齐：先词匹配，回退到子串。"""
    text_lower = text.lower()
    if query_words and any(w in text_lower for w in query_words):
        return True
    if query_lower in text_lower:
        return True
    return False


class RedisWorkingMemory:
    """生产实现：Redis LIST 做私有区 + 共享区。"""

    def __init__(self, user_id: str, agent_name: str) -> None:
        self._user_id = user_id
        self._agent_name = agent_name
        self._private_key = MemoryKey.l2_private(agent_name, user_id)
        self._shared_key = MemoryKey.l2_shared(user_id)

        self._redis = None
        try:
            self._redis = RedisPool.get_client()
        except Exception as e:
            logger.warning("RedisWorkingMemory: Redis unavailable: %s", e)

    async def remember(
        self,
        content: str,
        *,
        scope: Literal["private", "shared", "both"] = "both",
        memory_type: str = "general",
        importance: int = 1,
    ) -> None:
        if self._redis is None:
            logger.debug("WorkingMemory.remember skipped (Redis unavailable)")
            return

        entry = {
            "content": content,
            "type": memory_type,
            "importance": importance,
            "ts": time.time(),
            "source_agent": self._agent_name,
        }
        entry_json = json.dumps(entry)

        try:
            pipe = self._redis.pipeline()
            if scope in ("private", "both"):
                pipe.lpush(self._private_key, entry_json)
                pipe.ltrim(self._private_key, 0, 99)
                pipe.expire(self._private_key, 86_400)
            if scope in ("shared", "both"):
                pipe.lpush(self._shared_key, entry_json)
                pipe.ltrim(self._shared_key, 0, 199)
                pipe.expire(self._shared_key, 86_400)
            await pipe.execute()
        except Exception as e:
            logger.warning("WorkingMemory.remember failed: %s", e)

    async def recall(self, query: str, top_k: int = 5) -> list[MemoryEntry]:
        if self._redis is None:
            return []
        query_lower, query_words = _parse_query(query)
        results: list[MemoryEntry] = []
        seen: set[str] = set()

        results = await self._recall_private(
            query_lower, query_words, top_k, results, seen
        )
        if len(results) < top_k:
            results = await self._recall_shared(
                query_lower, query_words, top_k, results, seen
            )
        return results

    async def _recall_private(
        self,
        query_lower: str,
        query_words: list[str],
        top_k: int,
        results: list[MemoryEntry],
        seen: set[str],
    ) -> list[MemoryEntry]:
        try:
            raw_list = await self._redis.lrange(self._private_key, 0, 99)
            for raw in raw_list:
                if len(results) >= top_k:
                    break
                entry = json.loads(raw)
                content = entry.get("content", "")
                if not _matches(content, query_lower, query_words):
                    continue
                if content in seen:
                    continue
                seen.add(content)
                results.append(
                    MemoryEntry(
                        source="working.private",
                        content=content,
                        ts=entry.get("ts", 0),
                        metadata={
                            "memory_type": entry.get("type"),
                            "importance": entry.get("importance"),
                            "source_agent": entry.get("source_agent"),
                        },
                    )
                )
        except Exception as e:
            logger.debug("WorkingMemory.recall private failed: %s", e)
        return results

    async def _recall_shared(
        self,
        query_lower: str,
        query_words: list[str],
        top_k: int,
        results: list[MemoryEntry],
        seen: set[str],
    ) -> list[MemoryEntry]:
        try:
            raw_list = await self._redis.lrange(self._shared_key, 0, 199)
            for raw in raw_list:
                if len(results) >= top_k:
                    break
                entry = json.loads(raw)
                if entry.get("source_agent") == self._agent_name:
                    continue
                content = entry.get("content", "")
                if not _matches(content, query_lower, query_words):
                    continue
                if content in seen:
                    continue
                seen.add(content)
                results.append(
                    MemoryEntry(
                        source="working.shared",
                        content=content,
                        ts=entry.get("ts", 0),
                        metadata={
                            "memory_type": entry.get("type"),
                            "importance": entry.get("importance"),
                            "source_agent": entry.get("source_agent"),
                        },
                    )
                )
        except Exception as e:
            logger.debug("WorkingMemory.recall shared failed: %s", e)
        return results


class InMemoryWorkingMemory:
    """测试实现：纯内存，支持多实例共享 shared_pool。

    构造参数：
    - user_id, agent_name: 同 RedisWorkingMemory
    - shared_pool: 多个 InMemoryWorkingMemory 实例传入同一个 dict 即模拟跨 agent 共享
    """

    def __init__(
        self,
        user_id: str,
        agent_name: str,
        *,
        shared_pool: dict[str, list[dict]] | None = None,
    ) -> None:
        self._user_id = user_id
        self._agent_name = agent_name
        self._private: list[dict] = []
        self._shared_pool = shared_pool if shared_pool is not None else {}
        if user_id not in self._shared_pool:
            self._shared_pool[user_id] = []

    @property
    def _shared(self) -> list[dict]:
        return self._shared_pool[self._user_id]

    async def remember(
        self,
        content: str,
        *,
        scope: Literal["private", "shared", "both"] = "both",
        memory_type: str = "general",
        importance: int = 1,
    ) -> None:
        entry = {
            "content": content,
            "type": memory_type,
            "importance": importance,
            "ts": time.time(),
            "source_agent": self._agent_name,
        }
        if scope in ("private", "both"):
            self._private.append(entry)
        if scope in ("shared", "both"):
            self._shared.append(entry)

    async def recall(self, query: str, top_k: int = 5) -> list[MemoryEntry]:
        query_lower, query_words = _parse_query(query)
        results: list[MemoryEntry] = []
        seen: set[str] = set()

        for entry in reversed(self._private):
            if len(results) >= top_k:
                break
            content = entry.get("content", "")
            if not _matches(content, query_lower, query_words):
                continue
            if content in seen:
                continue
            seen.add(content)
            results.append(
                MemoryEntry(
                    source="working.private",
                    content=content,
                    ts=entry.get("ts", 0),
                    metadata={
                        "memory_type": entry.get("type"),
                        "importance": entry.get("importance"),
                        "source_agent": entry.get("source_agent"),
                    },
                )
            )

        for entry in reversed(self._shared):
            if len(results) >= top_k:
                break
            if entry.get("source_agent") == self._agent_name:
                continue
            content = entry.get("content", "")
            if not _matches(content, query_lower, query_words):
                continue
            if content in seen:
                continue
            seen.add(content)
            results.append(
                MemoryEntry(
                    source="working.shared",
                    content=content,
                    ts=entry.get("ts", 0),
                    metadata={
                        "memory_type": entry.get("type"),
                        "importance": entry.get("importance"),
                        "source_agent": entry.get("source_agent"),
                    },
                )
            )
        return results
