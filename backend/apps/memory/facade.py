"""MemoryFacade — 组合 ConversationLog + WorkingMemory + LongTermMemory 的薄层。

给 supervisor / sub_agents 使用，提供"一步到位"的高层接口。
本身不含业务逻辑，只做：
- build_context: recall + search → 去重 → sort(ts desc) → 截断
- remember: 按 persist_to 派发到 working / longterm
- append_conv / get_conv / clear_conv: 透传到 ConversationLog
"""

from __future__ import annotations

import logging
from typing import Literal

from apps.memory.entries import MemoryEntry
from apps.memory.protocols import ConversationLog, LongTermMemory, WorkingMemory

logger = logging.getLogger(__name__)


class MemoryFacade:
    def __init__(
        self,
        conv: ConversationLog,
        working: WorkingMemory,
        longterm: LongTermMemory,
    ) -> None:
        self._conv = conv
        self._working = working
        self._longterm = longterm

    # ====== 召回 ======
    async def build_context(self, query: str, top_k: int = 5) -> list[MemoryEntry]:
        """跨 working + longterm 召回，按 ts 降序，去重，截断 top_k。"""
        working_hits = await self._working.recall(query, top_k=top_k)
        longterm_hits: list[MemoryEntry] = []
        if len(working_hits) < top_k:
            longterm_hits = await self._longterm.search(query, top_k=top_k)

        combined: list[MemoryEntry] = []
        seen: set[str] = set()
        for entry in working_hits + longterm_hits:
            if entry.content in seen:
                continue
            seen.add(entry.content)
            combined.append(entry)

        combined.sort(key=lambda e: e.ts, reverse=True)
        return combined[:top_k]

    # ====== 写入 ======
    async def remember(
        self,
        content: str,
        *,
        persist_to: Literal["working", "longterm", "both"] = "both",
        scope: Literal["private", "shared", "both"] = "both",
        memory_type: str = "general",
        importance: int = 1,
        metadata: dict | None = None,
    ) -> None:
        """按 persist_to 写到 working / longterm / 两者。

        metadata 仅传给 longterm（working 不接受任意 metadata）。
        """
        if persist_to in ("working", "both"):
            await self._working.remember(
                content, scope=scope, memory_type=memory_type, importance=importance
            )
        if persist_to in ("longterm", "both"):
            await self._longterm.store(
                content,
                metadata=metadata,
                memory_type=memory_type,
                importance=importance,
            )

    # ====== 对话历史透传 ======
    async def append_conv(
        self, entries: list[dict], *, keep_turns: int | None = None
    ) -> list[dict]:
        return await self._conv.append(entries, keep_turns=keep_turns)

    async def get_conv(self, *, max_turns: int | None = None) -> list[dict]:
        return await self._conv.get(max_turns=max_turns)

    async def clear_conv(self) -> None:
        await self._conv.clear()
