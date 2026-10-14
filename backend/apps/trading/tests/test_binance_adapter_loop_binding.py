"""HTTP 客户端按 event loop 持有：每个 loop 一个，谁都不换谁的。

2026-09-21 这里先记录「客户端的 loop」、发现不一致就重建（**共享槽位**）；2026-09-26 的
故障证明「检查 → 重建共享槽位」本身就会互相作废：两个 loop 同时判自己不是持有者时，
双方在对方的 await 窗口里各换一次槽位，请求于是用上别人 loop 的客户端，其 keep-alive
连接一被复用就抛
`RuntimeError: <asyncio.locks.Event object at 0x... [unset]> is bound to a different
event loop`（当日 29 次持仓同步 fail-loud，两个实盘会话共享同一适配器）。

现在的契约（本文件逐条锁定）：

1. 每个 loop 有自己的客户端（``_clients[loop]``）：同 loop 复用，请求数不导致重建；
2. 别的 loop 的客户端**不碰**——不替换、也不从错误的 loop 去关它（它对自己那个 loop
   仍然有效，跨 loop 关它正是本类要修的那类错误）；
3. owner loop 已关闭的条目会被清理并关闭（``async_to_sync`` / ``asyncio.run`` 都是
   每次调用新建 loop、用完即关）；
4. 测试注入的替身（无 loop 归属）不参与按 loop 重建；
5. ``disconnect()`` 关掉所有客户端，且**每个都只从它自己的 loop** 关。
"""

from __future__ import annotations

import asyncio
import threading
import time

from django.test import SimpleTestCase

from apps.trading.adapters.binance import BinanceAdapter


class _FakeClient:
    def __init__(self, name: str):
        self.name = name
        self.closed = False
        self.created_in = None  # 在哪个 loop 里被建出来
        self.closed_in = None  # 在哪个 loop 里被关掉

    async def aclose(self):
        self.closed = True
        self.closed_in = asyncio.get_running_loop()


def _adapter_with_fakes() -> tuple[BinanceAdapter, list]:
    """适配器 + 「建客户端」发号器（不需要真的 httpx 客户端）。

    只替换实例属性，不 patch 类：真实 ``_create_client`` 会入池，这里照做。
    """
    adapter = BinanceAdapter("key", "secret", testnet=True)
    created: list[_FakeClient] = []

    async def fake_create() -> _FakeClient:
        client = _FakeClient(f"c{len(created)}")
        client.created_in = asyncio.get_running_loop()
        created.append(client)
        adapter._clients[adapter._running_loop()] = client
        return client

    adapter._create_client = fake_create  # type: ignore[method-assign]
    return adapter, created


class TestPerLoopClients(SimpleTestCase):
    def test_same_loop_reuses_its_client(self):
        """同 loop 内两次解析必须是同一个客户端（否则每请求一次 TLS + 一次 time 往返）。"""
        adapter, created = _adapter_with_fakes()

        async def run():
            return (
                await adapter._client_for_current_loop(),
                await adapter._client_for_current_loop(),
            )

        first, second = asyncio.run(run())

        self.assertIs(first, second)
        self.assertEqual(len(created), 1, "同一个 loop 只该建一次")

    def test_connect_registers_client_for_its_loop(self):
        adapter, created = _adapter_with_fakes()

        async def run():
            await adapter.connect()
            return await adapter._client_for_current_loop()

        resolved = asyncio.run(run())

        self.assertIs(resolved, created[0])
        self.assertEqual(len(created), 1, "connect() 之后请求不该再建一个")

    def test_two_live_loops_get_one_client_each(self):
        """两个**同时活着**的 loop：各拿一个，互不替换（线上形态：持仓同步的两个 loop）。"""
        adapter, created = _adapter_with_fakes()
        both_ready = threading.Barrier(3, timeout=60)  # A、B、主线程
        release = threading.Event()
        seen: dict[str, _FakeClient] = {}

        def worker(name: str) -> None:
            async def run() -> None:
                sink: dict[str, _FakeClient] = {}
                await _resolve_into(adapter, sink, name)
                seen[name] = sink[
                    name
                ]  # 先记录，再挂住 loop（断言要在两个 loop 都活着时做）
                both_ready.wait()
                release.wait(timeout=60)

            asyncio.run(run())

        threads = [threading.Thread(target=worker, args=(n,)) for n in ("A", "B")]
        for t in threads:
            t.start()
        both_ready.wait()
        try:
            self.assertEqual(len(created), 2, "每 loop 一次——不是每次调用一次")
            self.assertIsNot(seen["A"], seen["B"])
            self.assertFalse(
                any(c.closed for c in created), "两个 loop 都在跑，谁的客户端都不该被关"
            )
            self.assertEqual(len(adapter._clients), 2)
        finally:
            release.set()
            for t in threads:
                t.join(timeout=60)

    def test_other_live_loop_client_is_kept(self):
        """另一个活着的 loop 的客户端条目必须原样保留（不是被换掉、也不是被关掉）。"""
        adapter, created = _adapter_with_fakes()
        other_loop = asyncio.new_event_loop()
        self.addCleanup(other_loop.close)
        theirs = _FakeClient("theirs")
        adapter._clients[other_loop] = theirs

        resolved = asyncio.run(adapter._client_for_current_loop())

        self.assertIs(resolved, created[0])
        self.assertIs(adapter._clients.get(other_loop), theirs)
        self.assertFalse(theirs.closed)

    def test_foreign_loop_client_is_neither_used_nor_closed(self):
        """持有者是别的 loop 时：不复用它，也不从错误的 loop 关它。"""
        adapter, created = _adapter_with_fakes()
        foreign_loop = asyncio.new_event_loop()
        self.addCleanup(foreign_loop.close)
        seeded = _FakeClient("seeded")
        adapter._client = seeded  # type: ignore[assignment]
        adapter._client_loop = foreign_loop

        resolved = asyncio.run(adapter._client_for_current_loop())

        self.assertIsNot(resolved, seeded, "别的 loop 的客户端不得复用")
        self.assertIs(resolved, created[0])
        self.assertFalse(seeded.closed, "不得从错误的 loop 关它")

    def test_injected_client_without_loop_is_untouched(self):
        """测试注入的替身（未记录归属 loop）不参与按 loop 重建。"""
        adapter, created = _adapter_with_fakes()
        injected = _FakeClient("injected")
        adapter._client = injected  # type: ignore[assignment]  # _client_loop 保持 None

        resolved = asyncio.run(adapter._client_for_current_loop())

        self.assertIs(resolved, injected)
        self.assertFalse(injected.closed)
        self.assertEqual(created, [], "注入替身时不该再建真客户端")

    def test_client_of_closed_loop_is_pruned_and_closed(self):
        """owner loop 已关闭的客户端必须被清掉：否则 ``async_to_sync`` 每次调用漏一条连接。"""
        adapter, created = _adapter_with_fakes()
        dead_loop = asyncio.new_event_loop()
        dead = _FakeClient("dead")
        adapter._clients[dead_loop] = dead
        dead_loop.close()

        resolved = asyncio.run(adapter._client_for_current_loop())

        self.assertTrue(dead.closed, "死 loop 的客户端必须关掉")
        self.assertNotIn(dead_loop, adapter._clients)
        self.assertIs(resolved, created[0])
        self.assertEqual(len(adapter._clients), 1, "池子里只该剩下活 loop 的客户端")

    def test_disconnect_closes_current_loop_client_in_its_own_loop(self):
        adapter, created = _adapter_with_fakes()

        async def run():
            mine = await adapter._client_for_current_loop()
            await adapter.disconnect()
            return mine

        mine = asyncio.run(run())

        self.assertTrue(mine.closed)
        self.assertIs(mine.closed_in, mine.created_in, "必须在本 loop 里关")
        self.assertEqual(adapter._clients, {})
        self.assertIsNone(adapter._client)
        self.assertIsNone(adapter._client_loop)

    def test_disconnect_does_not_close_from_a_foreign_loop(self):
        """归属 loop 已停的客户端：丢弃引用，但**不**从当前 loop 去关它。"""
        adapter, _ = _adapter_with_fakes()
        stopped_loop = asyncio.new_event_loop()
        self.addCleanup(stopped_loop.close)
        orphan = _FakeClient("orphan")
        adapter._clients[stopped_loop] = orphan

        asyncio.run(adapter.disconnect())

        self.assertFalse(orphan.closed, "跨 loop 关它会抛 loop-bound 错误，只能丢弃")
        self.assertEqual(adapter._clients, {})

    def test_disconnect_closes_live_foreign_loop_client_in_that_loop(self):
        """归属 loop 还在跑的：投递到它自己的 loop 上去关（而不是从当前 loop 关）。"""
        adapter, _ = _adapter_with_fakes()
        started, _stop = threading.Event(), threading.Event()
        holder: dict[str, object] = {}

        def thread_main():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            holder["loop"] = loop
            started.set()
            loop.run_forever()
            loop.close()

        thread = threading.Thread(target=thread_main, daemon=True)
        thread.start()
        self.assertTrue(started.wait(timeout=30), "第二个 loop 没起来")
        foreign_loop = holder["loop"]

        def shutdown():
            foreign_loop.call_soon_threadsafe(foreign_loop.stop)
            thread.join(timeout=30)

        self.addCleanup(shutdown)
        client = _FakeClient("foreign")
        adapter._clients[foreign_loop] = client

        asyncio.run(adapter.disconnect())

        deadline = time.monotonic() + 10
        while not client.closed and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(client.closed, "投递出去的关闭没跑成")
        self.assertIs(client.closed_in, foreign_loop, "必须在它自己的 loop 里关")


async def _resolve_into(adapter: BinanceAdapter, sink: dict, name: str) -> None:
    sink[name] = await adapter._client_for_current_loop()
