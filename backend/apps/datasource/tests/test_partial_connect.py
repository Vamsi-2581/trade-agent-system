"""
Binance WS 逐市场独立连接（partial connect）测试

覆盖场景：
- 现货可连、合约端点不可达：connect_websocket 仍返回 True，现货 WS 就绪并可收流写 store
  （修复前的"全有或全无"会整体失败：supervisor 的 close_http_client 会把已连上的现货
  连接掐断，SUBSCRIBE 帧从未发出，store 一直为空 → ticker 持续 503）
- 全部市场不可达：返回 False，状态 ERROR
- 两个市场都可达：创建两个 receiver 任务
- 部分连接时 _resubscribe_all 只向已连接市场补发 SUBSCRIBE 帧
- 部分连接 + WS 收流 → ticker 视图 200（store 就绪）
- ticker 503 响应携带诊断 detail（proxy_configured / auto_ws_enabled）
"""

import asyncio
import json
import time
from typing import Optional

from django.test import RequestFactory
from rest_framework.permissions import AllowAny

from apps.datasource.api.views import MarketDataViewSet
from apps.datasource.base import ConnectionStatus, DataType, MarketType
from apps.datasource.sources.crypto.binance import BinanceDataSource
from apps.datasource.store import get_data_store


class _FakeWS:
    """最小 ClientWebSocketResponse 替身（记录发送帧、模拟空闲接收）"""

    def __init__(self):
        self.closed = False
        self.sent: list = []

    async def close(self):
        self.closed = True

    async def send_str(self, data: str):
        self.sent.append(json.loads(data))

    async def receive(self):
        # 测试不驱动 receiver 循环：挂起直到被取消
        await asyncio.sleep(3600)


class _TimedFakeWS(_FakeWS):
    """_FakeWS + 记录每帧发送时刻（单调钟，与节流逻辑的 loop.time() 同源）"""

    def __init__(self):
        super().__init__()
        self.send_times: list = []

    async def send_str(self, data: str):
        self.send_times.append(time.monotonic())
        await super().send_str(data)


class _FakeHTTPClient:
    """逐市场可控的 ws_connect 替身（None = 该市场连接失败）"""

    def __init__(
        self,
        spot_ws: Optional[_FakeWS] = None,
        futures_ws: Optional[_FakeWS] = None,
    ):
        self.spot_ws = spot_ws
        self.futures_ws = futures_ws

    async def ws_connect(self, url: str, **kwargs):
        if "fstream" in url:
            if self.futures_ws is None:
                raise ConnectionError("futures endpoint unreachable")
            return self.futures_ws
        if self.spot_ws is None:
            raise ConnectionError("spot endpoint unreachable")
        return self.spot_ws


class _PartialConnectSource(BinanceDataSource):
    """替换 _ensure_http_client，用假客户端驱动逐市场连接结果"""

    def __init__(
        self,
        spot_ws: Optional[_FakeWS] = None,
        futures_ws: Optional[_FakeWS] = None,
    ):
        super().__init__()
        self._fake_http = _FakeHTTPClient(spot_ws=spot_ws, futures_ws=futures_ws)

    def _ensure_http_client(self):
        self._http_client = self._fake_http
        return self._http_client


def _run_connect(source: _PartialConnectSource, subs: Optional[list] = None) -> bool:
    """在一次性事件循环上执行订阅注册 + connect_websocket，并清理 receiver 任务"""

    async def scenario():
        if subs:
            for symbol, data_type, market_type in subs:
                await source.subscribe(symbol, data_type, market_type=market_type)
        ok = await source.connect_websocket()
        for task in source._ws_tasks:
            task.cancel()
        if source._ws_tasks:
            await asyncio.gather(*source._ws_tasks, return_exceptions=True)
        return ok

    return asyncio.run(scenario())


def _subscribe_frames(ws: _FakeWS) -> list:
    """从假 WS 已发帧中筛出 SUBSCRIBE 帧"""
    return [m for m in ws.sent if m.get("method") == "SUBSCRIBE"]


def _call_ticker(symbol: str):
    """构造 ticker 视图请求（绕过全局 JWT 认证/权限/限流）"""

    class _TickerView(MarketDataViewSet):
        permission_classes = [AllowAny]
        authentication_classes = []
        throttle_classes = []

    factory = RequestFactory()
    request = factory.get(
        f"/api/datasource/api/market/ticker/?source=binance&symbol={symbol}"
    )
    return _TickerView.as_view({"get": "ticker"})(request)


class TestPartialConnect:
    """connect_websocket 逐市场独立连接"""

    def test_spot_ok_futures_down_connects(self):
        """现货通、合约挂：仍返回 True，现货 WS 就绪，只建 1 个 receiver"""
        spot_ws = _FakeWS()
        source = _PartialConnectSource(spot_ws=spot_ws, futures_ws=None)

        assert _run_connect(source) is True
        assert source._ws_spot is spot_ws
        assert source._ws_futures is None
        assert len(source._ws_tasks) == 1
        assert source.get_ws_status() == ConnectionStatus.CONNECTED

    def test_both_fail_returns_false(self):
        """全部市场不可达：返回 False，状态 ERROR"""
        source = _PartialConnectSource(spot_ws=None, futures_ws=None)

        assert _run_connect(source) is False
        assert source._ws_spot is None
        assert source._ws_futures is None
        assert source.get_ws_status() == ConnectionStatus.ERROR

    def test_both_ok_creates_two_receivers(self):
        """两个市场都可达：创建两个 receiver 任务"""
        spot_ws = _FakeWS()
        futures_ws = _FakeWS()
        source = _PartialConnectSource(spot_ws=spot_ws, futures_ws=futures_ws)

        assert _run_connect(source) is True
        assert source._ws_spot is spot_ws
        assert source._ws_futures is futures_ws
        assert len(source._ws_tasks) == 2

    def test_resubscribe_sends_spot_frame_on_partial_connect(self):
        """部分连接 + 已有现货订阅：只向现货 WS 补发 SUBSCRIBE 帧"""
        spot_ws = _FakeWS()
        source = _PartialConnectSource(spot_ws=spot_ws, futures_ws=None)

        assert (
            _run_connect(
                source,
                subs=[("BTC/USDT", DataType.TICKER, MarketType.SPOT)],
            )
            is True
        )
        subscribes = [m for m in spot_ws.sent if m.get("method") == "SUBSCRIBE"]
        assert any("btcusdt@ticker" in m.get("params", []) for m in subscribes)

    def test_resubscribe_batches_all_streams_single_frame(self):
        """重连补发：同市场全部活跃流合并为**一条** SUBSCRIBE 帧（params 多流）。

        逐订阅各发一帧时，订阅数超过约 5/秒会触发 Binance 每连接订阅限速，
        服务端以 1008 "Too many requests" 断连（重连后再次超限 → 无限重连抖动、
        ticker 断流 503）。批量后无论订阅多少都只占 1 次限速配额。
        """
        spot_ws = _FakeWS()
        source = _PartialConnectSource(spot_ws=spot_ws, futures_ws=None)
        subs = [
            ("BTC/USDT", DataType.TICKER, MarketType.SPOT),
            ("ETH/USDT", DataType.TICKER, MarketType.SPOT),
            ("SOL/USDT", DataType.TICKER, MarketType.SPOT),
        ]

        assert _run_connect(source, subs=subs) is True

        subscribes = _subscribe_frames(spot_ws)
        assert len(subscribes) == 1, (
            f"期望 1 条批量帧，实际 {len(subscribes)} 条: {subscribes}"
        )
        assert subscribes[0]["params"] == [
            "btcusdt@ticker",
            "ethusdt@ticker",
            "solusdt@ticker",
        ]

    def test_ticker_view_200_after_partial_connect(self):
        """部分连接后 WS 收流 → store 就绪 → ticker 视图 200"""
        store = get_data_store()
        store.clear_all()
        source = _PartialConnectSource(spot_ws=_FakeWS(), futures_ws=None)
        try:
            assert _run_connect(source) is True
            assert source._ws_futures is None

            async def feed():
                await source._handle_websocket_message(
                    {
                        "e": "24hrTicker",
                        "E": 1725700000000,
                        "s": "BTCUSDT",
                        "c": "79612.5",
                        "P": "1.25",
                        "h": "81000.0",
                        "l": "77000.0",
                        "v": "12345.6",
                        "q": "987654321.0",
                        "b": "79612.4",
                        "B": "79612.5",
                        "a": "79612.5",
                        "A": "79612.6",
                        "p": "980.0",
                    },
                    MarketType.SPOT,
                )

            asyncio.run(feed())

            response = _call_ticker("BTC/USDT")
            assert response.status_code == 200
            assert response.data["last_price"] == 79612.5
            assert response.data["change_pct_24h"] == 1.25
        finally:
            store.clear_all()

    def test_503_body_includes_diagnostic_detail(self):
        """store 为空时 503，响应携带诊断 detail（proxy_configured / auto_ws_enabled）"""
        store = get_data_store()
        store.clear_all()
        try:
            response = _call_ticker("SOL/USDT")
            assert response.status_code == 503
            assert "WebSocket ticker stream not ready" in response.data["error"]
            detail = response.data["detail"]
            assert "proxy_configured" in detail
            assert "auto_ws_enabled" in detail
        finally:
            store.clear_all()


class TestCrossLoopDelegation:
    """非引擎循环调用数据源方法时，必须委托到 ws_runner 引擎循环执行。

    订阅管理器（旧 new_event_loop 模式）、交易框架（asgiref 临时循环）、
    策略实盘（独立循环）都可能从各自的事件循环触碰共享数据源；若直接在
    这些循环上 connect/send，会产生 aiohttp "attached to a different
    loop" / "Cannot write to closing transport" 类错误（ticker 持久 503
    的根因之一）。本测试验证接收任务被创建在引擎循环上。
    """

    def test_connect_from_foreign_loop_runs_on_engine(self):
        """临时循环调用 connect：接收任务绑定引擎循环，断开无跨循环错误"""
        from apps.datasource.ws_runner import get_engine, run_ws_coroutine

        engine = get_engine()
        engine.start()
        engine_loop = engine.loop
        source = _PartialConnectSource(spot_ws=_FakeWS(), futures_ws=None)

        # 一次性临时循环 = 模拟订阅管理器/frame_manager 的调用方循环
        ok = asyncio.run(source.connect_websocket())
        assert ok is True
        assert source._ws_spot is not None
        assert source._ws_futures is None
        # 接收任务必须创建在引擎循环上（否则临时循环关闭即被销毁）
        assert len(source._ws_tasks) == 1
        assert source._ws_tasks[0].get_loop() is engine_loop

        # 临时循环已关闭后，仍可在引擎循环上正常断开（任务并未夭折）
        assert run_ws_coroutine(source.disconnect_websocket()) is True
        assert source._ws_spot is None

    def test_foreign_connect_failure_reports_false(self):
        """临时循环调用且全部市场不可达：委托后仍正确返回 False + ERROR"""
        from apps.datasource.ws_runner import get_engine

        engine = get_engine()
        engine.start()
        source = _PartialConnectSource(spot_ws=None, futures_ws=None)

        ok = asyncio.run(source.connect_websocket())
        assert ok is False
        assert source.get_ws_status() == ConnectionStatus.ERROR
        assert source._ws_tasks == []


class TestSubscribeRateLimit:
    """SUBSCRIBE 帧限速保护：杜绝 Binance 每连接约 5 次/秒订阅限速触发的
    1008 "Too many requests" 断连（该断连是 ticker 断流 503 的根因）。

    三道保护，各对应一段回归：
    1. 批量补发——重连时同市场全部流合并为一条帧（TestPartialConnect 已覆盖）。
    2. 重复订阅去重——同一活跃 key 再次 subscribe 不重发帧。
    3. 帧率节流——启动期多个*新*订阅同一秒内注册时，帧按最小间隔拉开。
    """

    def test_resubscribe_existing_active_sends_no_frame(self):
        """重复订阅同一活跃 key：只更新订阅条目，不重发 SUBSCRIBE 帧
        （重发会空耗 Binance 每连接订阅速率配额，触发 1008 断连）"""
        from apps.datasource.ws_runner import run_ws_coroutine

        spot_ws = _FakeWS()
        source = _PartialConnectSource(spot_ws=spot_ws, futures_ws=None)

        async def scenario():
            await source.connect_websocket()  # 先连上（无订阅，不发帧）
            await source.subscribe(
                "BTC/USDT", DataType.TICKER, market_type=MarketType.SPOT
            )
            first = len(_subscribe_frames(spot_ws))
            # 重复订阅同一 key（如仅回调变更）：不应新增帧
            await source.subscribe(
                "BTC/USDT", DataType.TICKER, market_type=MarketType.SPOT
            )
            second = len(_subscribe_frames(spot_ws))
            for task in source._ws_tasks:
                task.cancel()
            await asyncio.gather(*source._ws_tasks, return_exceptions=True)
            return first, second

        # 整个场景跑在引擎循环上（同 TestCrossLoopDelegation）：接收任务建在
        # 引擎循环，清理也须在引擎循环，否则跨循环 gather 报 "different loop"。
        first, second = run_ws_coroutine(scenario())
        assert first == 1
        assert second == 1, "重复订阅同一活跃 key 不应重发 SUBSCRIBE 帧"

    def test_rapid_new_subscribes_are_throttled(self):
        """启动期多个新订阅同一秒内注册：SUBSCRIBE 帧按最小间隔节流，
        帧率恒定低于 Binance 每连接限速，不再 1008 断连"""
        from apps.datasource.ws_runner import run_ws_coroutine

        orig = BinanceDataSource.WS_SUBSCRIBE_MIN_INTERVAL
        BinanceDataSource.WS_SUBSCRIBE_MIN_INTERVAL = 0.05  # 加速测试
        spot_ws = _TimedFakeWS()
        source = _PartialConnectSource(spot_ws=spot_ws, futures_ws=None)
        try:

            async def scenario():
                await source.connect_websocket()  # 先连上（无订阅，不发帧）
                for sym in (
                    "BTC/USDT",
                    "ETH/USDT",
                    "SOL/USDT",
                    "AVAX/USDT",
                    "DOGE/USDT",
                ):
                    await source.subscribe(
                        sym, DataType.TICKER, market_type=MarketType.SPOT
                    )
                for task in source._ws_tasks:
                    task.cancel()
                await asyncio.gather(*source._ws_tasks, return_exceptions=True)

            run_ws_coroutine(scenario())

            frames = _subscribe_frames(spot_ws)
            assert len(frames) == 5, f"期望 5 帧（每个新订阅一帧），实际 {len(frames)}"
            gaps = [
                spot_ws.send_times[i + 1] - spot_ws.send_times[i]
                for i in range(len(spot_ws.send_times) - 1)
            ]
            assert gaps, "至少应有 4 个帧间隔"
            for gap in gaps:
                assert gap >= 0.045, f"帧间隔 {gap:.3f}s 低于节流间隔，限速保护失效"
        finally:
            BinanceDataSource.WS_SUBSCRIBE_MIN_INTERVAL = orig
