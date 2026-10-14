"""MemoryManager — 向后兼容入口，构造 MemoryFacade 并转发旧方法名。

调用方（supervisor / sub_agents）代码零改动：
    mm = MemoryManager(agent_type="supervisor", user_id=message.user_id)
    await mm.get_conv_history()
    await mm.write_l2(content, ...)
    await mm.retrieve(query, top_k=5)

新代码建议直接用 MemoryFacade + 注入 InMemory* 实现（见 test_facade.py）。

## 迁移状态
- 旧概念 L1（进程内 LRU）已删除。`_L1_CACHE` 保留为空 dict 以兼容其他测试文件的 import。
- `write_l1` / `_l1` 方法已删除。
- 旧 key `conv:supervisor:{user_id}` 兼容读取已删除（TTL 24h，老数据早过期）。
"""

from __future__ import annotations

import logging
from typing import Optional

from apps.memory.compressor import LLMCompressor
from apps.memory.conversation import RedisConversationLog
from apps.memory.facade import MemoryFacade
from apps.memory.longterm import PgvectorLongTerm
from apps.memory.protocols import (
    Compressor,
    ConversationLog,
    LongTermMemory,
    WorkingMemory,
)
from apps.memory.working import RedisWorkingMemory

logger = logging.getLogger(__name__)

# 向后兼容：旧测试文件 import 这个符号做隔离。新代码不要用它。
# L1 概念已删除，这个 dict 永远是空的。
_L1_CACHE: dict = {}


class MemoryManager:
    """向后兼容入口 — 构造 MemoryFacade 并转发旧方法名。

    构造参数与旧版一致（agent_type / user_id / agent_name），
    额外支持注入自定义实现（conv / working / longterm / compressor），用于测试。
    """

    def __init__(
        self,
        agent_type: str,
        user_id: str,
        agent_name: Optional[str] = None,
        *,
        conv: Optional[ConversationLog] = None,
        working: Optional[WorkingMemory] = None,
        longterm: Optional[LongTermMemory] = None,
        compressor: Optional[Compressor] = None,
    ) -> None:
        self.agent_type = agent_type
        self.user_id = user_id
        self.agent_name = agent_name or agent_type

        # 注入优先；否则用生产实现
        self._conv = conv or RedisConversationLog(
            user_id, compressor=compressor or LLMCompressor()
        )
        self._working = working or RedisWorkingMemory(user_id, self.agent_name)
        self._longterm = longterm or PgvectorLongTerm(
            user_id, self.agent_name, agent_type
        )

        self._facade = MemoryFacade(self._conv, self._working, self._longterm)

    # ====== 对话历史 shim ======
    async def get_conv_history(self, max_turns: Optional[int] = None) -> list:
        return await self._facade.get_conv(max_turns=max_turns)

    async def append_conv_history(
        self, entries: list, keep_turns: int | None = None
    ) -> list:
        return await self._facade.append_conv(entries, keep_turns=keep_turns)

    async def clear_conv_history(self) -> None:
        await self._facade.clear_conv()

    # ====== 写入 shim ======
    async def write_l2(
        self,
        content: str,
        memory_type: str = "general",
        importance: int = 1,
        shared: bool = True,
    ) -> None:
        scope = "both" if shared else "private"
        await self._facade.remember(
            content,
            persist_to="working",
            scope=scope,
            memory_type=memory_type,
            importance=importance,
        )

    async def write_l3(
        self,
        content: str,
        metadata: Optional[dict] = None,
        memory_type: str = "general",
        importance: int = 1,
    ) -> None:
        await self._facade.remember(
            content,
            persist_to="longterm",
            metadata=metadata,
            memory_type=memory_type,
            importance=importance,
        )

    # ====== 召回 shim ======
    async def retrieve(self, query: str, top_k: int = 5) -> list[dict]:
        """旧接口：返回 dict list。新代码应改用 facade.build_context → MemoryEntry。"""
        entries = await self._facade.build_context(query, top_k=top_k)
        return [e.to_dict() for e in entries]
