"""
ws_runner 持久 WS 事件循环与行情供给测试

覆盖：
- 持久事件循环单例与跨线程协程调度（模拟 API 请求线程调用）
- start_market_feed 守卫（pytest 环境不建立真实外网连接）
- ticker 端点纯 WS 存储读取（无 REST 回退）：
  命中返回 200（含 Binance 原始符号兼容），WS 未就绪返回 503
"""

import asyncio
import threading

from django.test import RequestFactory
from rest_framework.permissions import AllowAny

from apps.datasource.api.views import MarketDataViewSet
from apps.datasource.store import get_data_store
from apps.datasource.ws_runner import (
    get_engine,
    run_ws_coroutine,
    start_market_feed,
)


class TestWSEngine:
    """持久事件循环测试"""

    def test_engine_singleton(self):
        """引擎进程内唯一"""
        assert get_engine() is get_engine()

    def test_run_coroutine(self):
        """协程在持久循环上运行并返回结果"""

        async def coro():
            await asyncio.sleep(0.01)
            return 42

        assert run_ws_coroutine(coro(), timeout=5) == 42

    def test_run_coroutine_from_other_thread(self):
        """可从非 asyncio 线程（如 API 请求线程）调用"""
        result_box: list = []

        def worker():
            async def coro():
                return "from-thread"

            result_box.append(run_ws_coroutine(coro(), timeout=5))

        t = threading.Thread(target=worker)
        t.start()
        t.join(5)
        assert not t.is_alive()
        assert result_box == ["from-thread"]


class TestStartMarketFeed:
    """行情供给启动守卫测试"""

    def test_skipped_in_pytest(self):
        """pytest 环境不启动供给（避免真实外网连接），且不污染幂等标记"""
        assert start_market_feed() is False
        assert start_market_feed() is False


class TestTickerViewWSOnly:
    """ticker 端点纯 WS 存储读取，无 REST 回退"""

    def _call_ticker(self, symbol: str):
        class _TickerView(MarketDataViewSet):
            # 测试中绕过全局 JWT 认证/权限/限流，聚焦存储读取路径
            permission_classes = [AllowAny]
            authentication_classes = []
            throttle_classes = []

        factory = RequestFactory()
        request = factory.get(
            f"/api/datasource/api/market/ticker/?source=binance&symbol={symbol}"
        )
        return _TickerView.as_view({"get": "ticker"})(request)

    def test_returns_ws_store_data_with_raw_symbol(self):
        """WS 按 Binance 原始符号（BTCUSDT）写存储，请求用斜杠形式（BTC/USDT）"""
        store = get_data_store()
        store.clear_all()
        try:
            store.store(
                "ticker",
                "BTCUSDT",
                {
                    "symbol": "BTCUSDT",
                    "last_price": 79612.5,
                    "change_pct_24h": 1.25,
                    "source": "binance",
                },
                "binance",
            )
            response = self._call_ticker("BTC/USDT")
            assert response.status_code == 200
            assert response.data["last_price"] == 79612.5
            assert response.data["change_pct_24h"] == 1.25
        finally:
            store.clear_all()

    def test_returns_store_data_with_slashed_symbol(self):
        """请求符号与存储符号一致（斜杠形式）时直接命中"""
        store = get_data_store()
        store.clear_all()
        try:
            store.store(
                "ticker",
                "ETH/USDT",
                {"symbol": "ETH/USDT", "last_price": 3200.0, "source": "binance"},
                "binance",
            )
            response = self._call_ticker("ETH/USDT")
            assert response.status_code == 200
            assert response.data["last_price"] == 3200.0
        finally:
            store.clear_all()

    def test_503_when_ws_not_ready(self):
        """WS 流未就绪时返回 503（不再回退 REST fetch）"""
        store = get_data_store()
        store.clear_all()
        try:
            response = self._call_ticker("SOL/USDT")
            assert response.status_code == 503
            assert "WebSocket ticker stream not ready" in response.data["error"]
        finally:
            store.clear_all()
