"""Memory assembly seam for SupervisorAgent.

Wraps the repetitive ``MemoryManager()`` instantiation pattern behind a
lazy, user-scoped injector. Callers request a memory operation; the
injector creates the underlying manager on first use and caches it for
the lifetime of the request.

This seam lets tests inject a fake manager and keeps the Supervisor
methods free of per-method import boilerplate.
"""

from __future__ import annotations


class MemoryInjector:
    """User-scoped memory facade for the Supervisor.

    Args:
        user_id: the channel user id (telegram / lark / web). May be
            ``None`` or empty — in that case all operations become
            no-ops, matching the existing ``if message.user_id`` guard
            pattern in SupervisorAgent.
        agent_type: logical owner of the memory (default ``"supervisor"``).
    """

    def __init__(self, user_id: str | None, agent_type: str = "supervisor"):
        self._user_id = user_id
        self._agent_type = agent_type
        self._mm = None

    # ------------------------------------------------------------------ #
    #  Lifecycle                                                           #
    # ------------------------------------------------------------------ #

    @property
    def available(self) -> bool:
        """True when the injector has a valid user_id to work with."""
        return bool(self._user_id)

    async def _get_mm(self):
        if not self.available:
            return None
        if self._mm is None:
            from apps.memory.manager import MemoryManager

            self._mm = MemoryManager(
                agent_type=self._agent_type,
                user_id=self._user_id,
            )
        return self._mm

    # ------------------------------------------------------------------ #
    #  Conversation history                                                #
    # ------------------------------------------------------------------ #

    async def get_conv_history(self) -> list:
        mm = await self._get_mm()
        if mm is None:
            return []
        return await mm.get_conv_history()

    async def append_conv_history(
        self,
        entries: list[dict],
        keep_turns: int | None = None,
    ) -> None:
        mm = await self._get_mm()
        if mm is None:
            return
        if keep_turns is not None:
            await mm.append_conv_history(entries, keep_turns=keep_turns)
        else:
            await mm.append_conv_history(entries)

    async def clear_conv_history(self) -> None:
        mm = await self._get_mm()
        if mm is None:
            return
        await mm.clear_conv_history()

    # ------------------------------------------------------------------ #
    #  Layer writes                                                        #
    # ------------------------------------------------------------------ #

    async def write_l2(
        self,
        content: str,
        memory_type: str,
        importance: int = 1,
    ) -> None:
        mm = await self._get_mm()
        if mm is None:
            return
        await mm.write_l2(
            content=content,
            memory_type=memory_type,
            importance=importance,
        )

    async def write_l3(self, content: str, memory_type: str) -> None:
        mm = await self._get_mm()
        if mm is None:
            return
        await mm.write_l3(content=content, memory_type=memory_type)
