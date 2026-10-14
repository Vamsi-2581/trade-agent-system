"""Tests for the MemoryInjector seam.

Verifies the facade methods correctly delegate to the underlying
MemoryManager and that no-op behaviour holds when user_id is absent.
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from apps.agent.memory_injector import MemoryInjector


class _FakeMM:
    def __init__(self, *a, **kw):
        self.calls = []

    async def get_conv_history(self):
        self.calls.append(("get_conv_history",))
        return [{"role": "user", "text": "hi"}]

    async def append_conv_history(self, entries, keep_turns=None):
        self.calls.append(("append_conv_history", entries, keep_turns))

    async def clear_conv_history(self):
        self.calls.append(("clear_conv_history",))

    async def write_l2(self, content, memory_type, importance=1):
        self.calls.append(("write_l2", content, memory_type, importance))

    async def write_l3(self, content, memory_type):
        self.calls.append(("write_l3", content, memory_type))


class TestMemoryInjectorNoUserId(unittest.TestCase):
    """When user_id is missing, all operations should no-op."""

    def test_available_false(self):
        mi = MemoryInjector(None)
        self.assertFalse(mi.available)

    def test_get_conv_history_returns_empty(self):
        mi = MemoryInjector("")
        result = asyncio.run(mi.get_conv_history())
        self.assertEqual(result, [])

    def test_append_noop(self):
        mi = MemoryInjector(None)
        # Should not raise
        asyncio.run(mi.append_conv_history([{"role": "user", "text": "x"}]))

    def test_clear_noop(self):
        mi = MemoryInjector("")
        asyncio.run(mi.clear_conv_history())

    def test_write_l2_noop(self):
        mi = MemoryInjector(None)
        asyncio.run(mi.write_l2(content="x", memory_type="t"))

    def test_write_l3_noop(self):
        mi = MemoryInjector("")
        asyncio.run(mi.write_l3(content="x", memory_type="t"))


class TestMemoryInjectorDelegation(unittest.TestCase):
    """When user_id is present, operations delegate to MemoryManager."""

    def _make_injector(self):
        mi = MemoryInjector("user_123")
        # Inject fake manager directly to avoid the real MemoryManager import
        mi._mm = _FakeMM()
        return mi

    def test_get_conv_history(self):
        mi = self._make_injector()
        result = asyncio.run(mi.get_conv_history())
        self.assertEqual(result, [{"role": "user", "text": "hi"}])

    def test_append_without_keep_turns(self):
        mi = self._make_injector()
        entries = [{"role": "user", "text": "x"}]
        asyncio.run(mi.append_conv_history(entries))
        self.assertEqual(mi._mm.calls[-1], ("append_conv_history", entries, None))

    def test_append_with_keep_turns(self):
        mi = self._make_injector()
        entries = [{"role": "user", "text": "x"}]
        asyncio.run(mi.append_conv_history(entries, keep_turns=10))
        self.assertEqual(mi._mm.calls[-1], ("append_conv_history", entries, 10))

    def test_clear(self):
        mi = self._make_injector()
        asyncio.run(mi.clear_conv_history())
        self.assertEqual(mi._mm.calls[-1], ("clear_conv_history",))

    def test_write_l2(self):
        mi = self._make_injector()
        asyncio.run(mi.write_l2(content="c", memory_type="t", importance=3))
        self.assertEqual(mi._mm.calls[-1], ("write_l2", "c", "t", 3))

    def test_write_l3(self):
        mi = self._make_injector()
        asyncio.run(mi.write_l3(content="c", memory_type="t"))
        self.assertEqual(mi._mm.calls[-1], ("write_l3", "c", "t"))


class TestMemoryInjectorLazyInit(unittest.TestCase):
    def test_mm_created_once(self):
        fake_mm_instance = _FakeMM()
        ctor_calls = []

        class TrackingFakeMM:
            def __init__(self, *a, **kw):
                ctor_calls.append((a, kw))
                # Copy state from shared instance
                self.calls = fake_mm_instance.calls

            async def get_conv_history(self):
                return []

        with patch(
            "apps.agent.memory_injector.MemoryManager",
            TrackingFakeMM,
            create=True,
        ):
            # Patch via import path that MemoryInjector uses
            import apps.memory.manager as mm_mod

            original = mm_mod.MemoryManager
            mm_mod.MemoryManager = TrackingFakeMM
            try:
                mi = MemoryInjector("u1")
                asyncio.run(mi.get_conv_history())
                asyncio.run(mi.get_conv_history())
                # Only one MemoryManager created despite two calls
                self.assertEqual(len(ctor_calls), 1)
            finally:
                mm_mod.MemoryManager = original


if __name__ == "__main__":
    unittest.main()
