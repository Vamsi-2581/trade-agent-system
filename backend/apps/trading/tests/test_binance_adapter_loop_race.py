"""跨 event loop：同一个适配器被两个 loop 使用时，请求必须落在「本 loop 造」的客户端上。

线上事实（2026-09-26，故障仍在发生：当日 29 条用户通知，最近一条 17:58 北京时间）：
BNB/USDT 与 SOL/USDT 两个实盘会话分属两个 event loop（app 的 uvloop 与数据源线程的
selector loop），共享同一个 `BinanceAdapter` 实例。日志里
`[Binance] HTTP client bound to another event loop; rebuilding for the current loop`
几乎每个请求都刷一次 —— 两个 loop 互相作废对方的客户端。

**报错的确切来源**（已在容器内最小复现，与线上字符串一字不差）：

    RuntimeError: <asyncio.locks.Event object at 0x... [unset]> is bound to a different event loop

httpx 的连接是 anyio 的 `StreamProtocol`，其 `read_event` 是一个 `asyncio.Event`，
随 socket 一起**在建立连接的 loop 里**被创建、并在首次 `await wait()` 时与该 loop 绑定。
keep-alive 连接被另一个 loop 复用时，`StreamReaderWrapper.receive()` 要等数据 →
`read_event.wait()` → `_get_loop()` → RuntimeError。`[unset]` 正是「还没收到数据」。

于是决定性的不变式是：**一次请求用的客户端，必须是发起它的那个 loop 造出来的**
（这样它的连接池、socket、Event 全都属于本 loop）。

修复前 `_ensure_client_for_current_loop()` 是「检查 → 重建」两步，且重建中间
`await _sync_time()`（一次真实网络往返），重建后再由 `_ensure_connected()` 去读那个**共享
槽位**。两个 loop 同时判自己不是持有者时（持有者是别的 loop，或并发抖动打破轮转），双方
会在对方的 await 窗口里各自换掉 `self._client`，于是请求用上**别人 loop 的客户端** ——
正是报错的前提条件。

现在客户端按 loop 分开持有（`_clients[loop]`），且 `_request` 把解析结果直接用在一整次
请求上、不再重读共享槽位。本测试锁的就是这两条：**request 不带跨 loop 客户端** +
**每 loop 只建一次（不随请求数增长）**。
"""

from __future__ import annotations

import asyncio
import http.server
import json
import threading
import time

from django.test import SimpleTestCase, override_settings

from apps.trading.adapters.binance import BinanceAdapter


class _Handler(http.server.BaseHTTPRequestHandler):
    """只服务适配器要用的两条路径。

    `protocol_version = "HTTP/1.1"` 是必需的：HTTP/1.0 下每个请求都新开连接，
    socket 恰好落在发起请求的 loop 里，跨 loop 复用这条路径就永远走不到。
    """

    protocol_version = "HTTP/1.1"  # keep-alive：连接会跨 loop 复用（生产即如此）
    TIME_DELAY = 0.2  # 拉长重建窗口，把线上数百毫秒的往返放大成确定性
    POSITION_DELAY = 0.03

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler 接口
        if self.path.startswith("/fapi/v1/time"):
            time.sleep(self.TIME_DELAY)
            payload = json.dumps({"serverTime": int(time.time() * 1000)}).encode()
        elif self.path.startswith("/fapi/v2/positionRisk"):
            time.sleep(self.POSITION_DELAY)
            payload = b"[]"
        else:
            payload = b"{}"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # 测试输出保持干净
        pass


@override_settings(WEB_PROXY="")  # 本地回环不得走代理
class TestSharedAdapterAcrossLoops(SimpleTestCase):
    def _serve(self) -> None:
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        self.port = server.server_address[1]

    def _adapter(self) -> BinanceAdapter:
        adapter = BinanceAdapter("key", "secret", testnet=True)
        adapter.BASE_URL = f"http://127.0.0.1:{self.port}"
        adapter.TIMEOUT = 5.0
        return adapter

    def test_request_never_uses_a_foreign_loop_client(self):
        """两个 loop 并发调用时，每次请求都必须用「本 loop 造」的客户端。"""
        import httpx

        self._serve()
        adapter = self._adapter()

        maker_loop: dict[int, object] = {}  # id(client) → 造它的 loop
        cross_loop: list[str] = []  # 发起 loop ≠ 客户端所属 loop 的请求
        errors: list[str] = []
        rebuilds: list[object] = []

        real_init = httpx.AsyncClient.__init__
        real_get = httpx.AsyncClient.get
        real_create = BinanceAdapter._create_client

        def tagged_init(self, *a, **kw):
            real_init(self, *a, **kw)
            maker_loop[id(self)] = asyncio.get_running_loop()  # 构造时贴标签

        async def traced_get(self, url, *a, **kw):
            me = asyncio.get_running_loop()
            maker = maker_loop.get(id(self))
            if maker is not me:
                cross_loop.append(
                    f"{'time' if 'fapi/v1/time' in url else 'positions'}: "
                    f"issuer={id(me) % 1000} "
                    f"maker={id(maker) % 1000 if maker else None}"
                )
            return await real_get(self, url, *a, **kw)

        async def traced_create(self):
            rebuilds.append(asyncio.get_running_loop())
            return await real_create(self)

        httpx.AsyncClient.__init__ = tagged_init
        httpx.AsyncClient.get = traced_get
        BinanceAdapter._create_client = traced_create
        self.addCleanup(lambda: setattr(httpx.AsyncClient, "__init__", real_init))
        self.addCleanup(lambda: setattr(httpx.AsyncClient, "get", real_get))
        self.addCleanup(lambda: setattr(BinanceAdapter, "_create_client", real_create))

        # 预置一个「由别的 loop 造的」客户端：这是线上并发那一刻的形态（持有者既不是 A
        # 也不是 B）。修复前它让两个 loop **同时**判自己不是持有者、同时进入重建路径，
        # 把这条测试钉成确定性红；修复后它不再参与解析（客户端按 loop 分开持有），
        # 保留它是为了让「有人把共享槽位读回去」的改动重新变红。
        # 用局部变量持有强引用：id 一旦被回收就可能被新客户端复用，标签会张冠李戴（假红）。
        foreign_loop = asyncio.new_event_loop()
        self.addCleanup(foreign_loop.close)
        seeded = type("Seeded", (), {"aclose": staticmethod(lambda: None)})()
        adapter._client = seeded  # type: ignore[assignment]
        adapter._client_loop = foreign_loop
        maker_loop[id(seeded)] = foreign_loop

        connected = threading.Event()
        rounds = 6

        def worker(index: int) -> None:
            async def run() -> None:
                if index == 0:
                    await adapter.connect()
                    connected.set()
                else:
                    connected.wait(timeout=30)
                for _ in range(rounds):
                    try:
                        await adapter.get_positions()
                    except Exception as e:  # noqa: BLE001 - 收集后统一断言
                        errors.append(f"{type(e).__name__}: {e}")

            asyncio.run(run())

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)

        # 症状：用户看到的那条报错
        self.assertEqual(
            errors, [], "不得出现跨 loop 报错（持仓同步 fail-loud 的 reason）"
        )
        # 根因：请求落在了别的 loop 造的客户端上
        self.assertEqual(
            cross_loop,
            [],
            "请求用了别的 loop 造的客户端（其 keep-alive 连接属于那个 loop）",
        )
        # 重建必须收敛到「每 loop 一次」，而不是随请求数线性增长（线上 = 每个请求都刷）
        self.assertLessEqual(
            len(rebuilds),
            4,
            f"客户端重建不收敛：{len(rebuilds)} 次 / {2 * rounds} 个请求",
        )
