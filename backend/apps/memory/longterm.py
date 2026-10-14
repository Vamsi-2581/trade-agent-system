"""LongTermMemory 的两个实现：Pgvector/PostgreSQL（生产）+ 内存（测试）。

设计要点：
- per-(user, agent) seam
- store 时自动合并 memory_type / importance 到 metadata（与旧 write_l3 行为一致）
- search 走 Django ORM：content__icontains + order_by -created_at + limit
- PgvectorLongTerm 需要 agent_type（AgentMemory 模型的 CharField choices）
- InMemory 实现支持 keyword 搜索，便于测试不依赖 PG
"""

from __future__ import annotations

import calendar
import logging
from typing import Any

from apps.memory.entries import MemoryEntry

logger = logging.getLogger(__name__)


class PgvectorLongTerm:
    """生产实现：PostgreSQL + pgvector 字段（当前 search 仍走 icontains 关键词）。"""

    def __init__(self, user_id: str, agent_name: str, agent_type: str) -> None:
        self._user_id = user_id
        self._agent_name = agent_name
        self._agent_type = agent_type

    async def store(
        self,
        content: str,
        *,
        metadata: dict[str, Any] | None = None,
        memory_type: str = "general",
        importance: int = 1,
    ) -> None:
        from apps.agent.models import AgentMemory
        from apps.core.db_utils import db_async

        merged = dict(metadata or {})
        merged.setdefault("memory_type", memory_type)
        merged.setdefault("importance", importance)

        try:
            obj = await db_async(AgentMemory.objects.create)(
                agent_type=self._agent_type,
                agent_name=self._agent_name,
                user_id=self._user_id,
                content=content,
                metadata=merged,
            )
            logger.debug(
                "LongTermMemory.store | id=%s | user=%s", obj.id, self._user_id
            )
        except Exception as e:
            logger.warning("LongTermMemory.store failed: %s", e)

    async def search(self, query: str, top_k: int = 5) -> list[MemoryEntry]:
        from apps.agent.models import AgentMemory
        from apps.core.db_utils import db_async

        try:
            qs = await db_async(
                lambda: list(
                    AgentMemory.objects.filter(
                        user_id=self._user_id,
                        content__icontains=query,
                    ).order_by("-created_at")[:top_k]
                )
            )()
            return [
                MemoryEntry(
                    source="longterm",
                    content=obj.content,
                    ts=calendar.timegm(obj.created_at.timetuple())
                    if obj.created_at
                    else 0,
                    metadata={
                        **(obj.metadata or {}),
                        "agent_name": getattr(obj, "agent_name", ""),
                    },
                )
                for obj in qs
            ]
        except Exception as e:
            logger.debug("LongTermMemory.search failed: %s", e)
            return []


class InMemoryLongTerm:
    """测试实现：纯内存列表 + icontains 关键词搜索。"""

    def __init__(self, user_id: str, agent_name: str) -> None:
        self._user_id = user_id
        self._agent_name = agent_name
        self._entries: list[dict[str, Any]] = []

    async def store(
        self,
        content: str,
        *,
        metadata: dict[str, Any] | None = None,
        memory_type: str = "general",
        importance: int = 1,
    ) -> None:
        import time

        merged = dict(metadata or {})
        merged.setdefault("memory_type", memory_type)
        merged.setdefault("importance", importance)

        self._entries.append(
            {
                "content": content,
                "metadata": merged,
                "agent_name": self._agent_name,
                "ts": time.time(),
            }
        )

    async def search(self, query: str, top_k: int = 5) -> list[MemoryEntry]:
        query_lower = query.lower()
        matches = [
            e for e in reversed(self._entries) if query_lower in e["content"].lower()
        ][:top_k]
        return [
            MemoryEntry(
                source="longterm",
                content=e["content"],
                ts=e["ts"],
                metadata={**(e["metadata"] or {}), "agent_name": e["agent_name"]},
            )
            for e in matches
        ]
