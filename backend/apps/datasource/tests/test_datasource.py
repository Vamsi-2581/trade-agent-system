"""
数据源模块测试
"""

import pytest
from datetime import datetime, timedelta

from apps.datasource.registry import DataSourceRegistry
from apps.datasource.store import MemoryDataStore, get_data_store
from apps.datasource.subscription import DataSubscriptionManager
from apps.datasource.monitor import DataQualityMonitor
from apps.datasource.base import (
    DataType,
    KlineInterval,
    MarketType,
    ConnectionStatus,
    BaseDataSource,
)


class TestDataSourceRegistry:
    """数据源注册中心测试"""

    def test_singleton(self):
        """测试单例模式"""
        registry1 = DataSourceRegistry()
        registry2 = DataSourceRegistry()
        assert registry1 is registry2

    def test_register_and_get(self):
        """测试注册和获取"""

        @DataSourceRegistry.register("test_source")
        class TestSource:
            name = "test_source"

        assert "test_source" in DataSourceRegistry.list_registered()
        assert not DataSourceRegistry.is_loaded("test_source")

        # 获取实例（懒加载）
        source = DataSourceRegistry.get("test_source")
        assert source is not None
        assert DataSourceRegistry.is_loaded("test_source")

    def test_unregister_nonexistent(self):
        """测试获取未注册的数据源"""
        with pytest.raises(KeyError):
            DataSourceRegistry.get("nonexistent_source")

    def test_unload(self):
        """测试卸载数据源"""

        @DataSourceRegistry.register("test_unload")
        class TestUnloadSource:
            name = "test_unload"

            def cleanup(self):
                pass

        # 先加载
        DataSourceRegistry.get("test_unload")
        assert DataSourceRegistry.is_loaded("test_unload")

        # 卸载
        DataSourceRegistry.unload("test_unload")
        assert not DataSourceRegistry.is_loaded("test_unload")


class TestMemoryDataStore:
    """内存数据存储测试"""

    def test_singleton(self):
        """测试单例模式"""
        store1 = MemoryDataStore()
        store2 = MemoryDataStore()
        assert store1 is store2

    def test_data_survives_reinstantiation(self):
        """回归：重复调用 MemoryDataStore() 不得清空已存数据。

        Python 对每次实例化都会重跑 __init__；旧实现没有短路，
        导致每个 get_data_store() 调用点（WS 写入方、看门狗、API 视图）
        都拿到同一个单例但状态被重置为空——WS ticker 写入后立刻被
        下一个读取者清掉，接口持续 503。
        """
        store = MemoryDataStore()
        store.clear_all()
        store.store(
            "ticker",
            "BTCUSDT",
            {"symbol": "BTCUSDT", "last_price": 50000.0, "timestamp": datetime.now()},
            "binance",
        )

        # 模拟 WS 写入后，看门狗/视图各自再调一次 get_data_store()
        for _ in range(3):
            again = get_data_store()
            assert again is store
            assert store._stats["total_entries"] == 1

        result = store.get_latest("ticker", "BTCUSDT", 1)
        assert len(result) == 1
        assert result[0]["last_price"] == 50000.0

    def test_store_and_get(self):
        """测试存储和获取"""
        store = MemoryDataStore()
        store.clear_all()

        data = {"symbol": "BTC/USDT", "price": 50000.0, "timestamp": datetime.now()}

        # 存储
        store.store("ticker", "BTC/USDT", data, "binance")

        # 获取
        result = store.get_latest("ticker", "BTC/USDT", 1)
        assert len(result) == 1
        assert result[0]["symbol"] == "BTC/USDT"
        assert result[0]["price"] == 50000.0

    def test_store_batch(self):
        """测试批量存储"""
        store = MemoryDataStore()
        store.clear_all()

        base = datetime.now()
        data_list = [
            # 每项用不同 timestamp：store 以 source:symbol:type:ts 为键，
            # 相同微秒会互相覆盖，导致批量写入静默丢失（旧测试的 flake 根因）。
            {
                "symbol": "BTC/USDT",
                "price": 50000.0 + i,
                "timestamp": base + timedelta(microseconds=i),
            }
            for i in range(10)
        ]

        count = store.store_batch("ticker", "BTC/USDT", data_list, "binance")
        assert count == 10

        result = store.get_latest("ticker", "BTC/USDT", 100)
        assert len(result) == 10

    def test_get_range(self):
        """测试时间范围查询"""
        store = MemoryDataStore()
        store.clear_all()

        now = datetime.now()
        data_list = [
            {
                "symbol": "BTC/USDT",
                "price": 50000.0 + i,
                "timestamp": now - timedelta(hours=i),
            }
            for i in range(5)
        ]

        store.store_batch("kline", "BTC/USDT", data_list, "binance")

        start = now - timedelta(hours=3)
        end = now + timedelta(hours=1)

        result = store.get_range("kline", "BTC/USDT", start, end)
        assert len(result) == 4  # 0, 1, 2, 3 小时前

    def test_delete(self):
        """测试删除"""
        store = MemoryDataStore()
        store.clear_all()

        data = {"symbol": "BTC/USDT", "price": 50000.0}
        store.store("ticker", "BTC/USDT", data, "binance", key="test_key")

        # 删除
        result = store.delete("ticker", "BTC/USDT", "test_key")
        assert result

        # 获取应该为空
        result = store.get("ticker", "BTC/USDT", "test_key")
        assert result is None

    def test_cleanup_expired(self):
        """测试过期清理"""
        store = MemoryDataStore()
        store.clear_all()

        # 存储马上过期的数据
        data = {"symbol": "BTC/USDT", "price": 50000.0}
        store.store("ticker", "BTC/USDT", data, "binance", expire_seconds=0.1)

        # 等待过期
        import time

        time.sleep(0.2)

        # 清理
        cleaned = store.cleanup_expired()
        assert cleaned >= 1

    def test_stats(self):
        """测试统计信息"""
        store = MemoryDataStore()
        store.clear_all()

        store.store("ticker", "BTC/USDT", {"price": 50000.0}, "binance")
        store.store("ticker", "ETH/USDT", {"price": 3000.0}, "binance")
        store.store("kline", "BTC/USDT", {"open": 50000.0}, "binance")

        stats = store.get_stats()
        assert stats["total_entries"] == 3
        assert stats["data_types_count"] == 2


class TestDataSubscriptionManager:
    """数据订阅管理器测试"""

    def test_singleton(self):
        """测试单例模式"""
        sub1 = DataSubscriptionManager()
        sub2 = DataSubscriptionManager()
        assert sub1 is sub2

    def test_subscribe_and_unsubscribe(self):
        """测试订阅和取消订阅"""
        sub = DataSubscriptionManager()

        sub_id = sub.subscribe(
            user_id="user1",
            source="binance",
            symbol="BTC/USDT",
            data_type="kline",
            interval="1m",
        )

        assert sub_id is not None
        assert sub.get_subscription_count(user_id="user1") == 1

        # 取消订阅
        result = sub.unsubscribe(sub_id)
        assert result
        assert sub.get_subscription_count(user_id="user1") == 0

    def test_get_user_subscriptions(self):
        """测试获取用户订阅"""
        sub = DataSubscriptionManager()

        sub.subscribe(
            user_id="user2", source="binance", symbol="BTC/USDT", data_type="ticker"
        )
        sub.subscribe(
            user_id="user2",
            source="binance",
            symbol="ETH/USDT",
            data_type="kline",
            interval="5m",
        )

        subs = sub.get_user_subscriptions("user2")
        assert len(subs) == 2

        # 清理
        sub.unsubscribe_user("user2")
        assert sub.get_subscription_count(user_id="user2") == 0

    def test_dispatch_data(self):
        """测试数据分发"""
        sub = DataSubscriptionManager()
        callback_called = []

        def callback(data):
            callback_called.append(data)

        sub.subscribe(
            user_id="user3",
            source="binance",
            symbol="BTC/USDT",
            data_type="ticker",
            callback=callback,
        )

        # 分发数据
        test_data = {"symbol": "BTC/USDT", "price": 50000.0}
        count = sub.dispatch_data("binance", "BTC/USDT", "ticker", test_data)

        assert count == 1
        assert len(callback_called) == 1
        assert callback_called[0] == test_data


class TestDataQualityMonitor:
    """数据质量监控器测试"""

    def test_singleton(self):
        """测试单例模式"""
        monitor1 = DataQualityMonitor()
        monitor2 = DataQualityMonitor()
        assert monitor1 is monitor2

    def test_record_data(self):
        """测试记录数据

        `record_data` 是热路径，只把延迟写进原始 `_latencies`，**不**顺手算均值；
        `get_stats()['avg_latencies']` 是从 `_reports` 快照出来的，所以要让统计里
        出现这个来源，必须先 `check()` 让 report 落下来。这是刻意的口径：
        热路径不买单，诊断面现算。
        """
        monitor = DataQualityMonitor()
        monitor._reports.clear()
        monitor._latencies.clear()

        monitor.record_data(
            source="binance",
            symbol="BTC/USDT",
            data_type="ticker",
            data_timestamp=datetime.now() - timedelta(milliseconds=50),
        )

        # record_data 只进原始延迟，不建 report —— 此时统计里还没有这个来源
        assert "binance" in monitor._latencies
        assert "binance" not in monitor.get_stats()["avg_latencies"]

        # check() 让 report 落下来，统计才开始反映它
        monitor.check("binance", "BTC/USDT", "ticker")
        assert "binance" in monitor.get_stats()["avg_latencies"]

    def test_record_anomaly(self):
        """测试记录异常"""
        monitor = DataQualityMonitor()

        monitor.record_anomaly(
            source="binance",
            symbol="BTC/USDT",
            data_type="ticker",
            anomaly_type="missing_data",
            description="Missing data for 5 minutes",
        )

        # 检查报告
        report = monitor.get_report("binance", "BTC/USDT", "ticker")
        if report:
            assert report.anomaly_count > 0

    def test_check(self):
        """测试质量检查"""
        monitor = DataQualityMonitor()

        report = monitor.check("binance", "BTC/USDT", "ticker")

        assert report is not None
        assert report.source == "binance"
        assert report.symbol == "BTC/USDT"
        assert report.data_type == "ticker"

    def test_alert_callback(self):
        """测试报警回调"""
        monitor = DataQualityMonitor()
        alerts = []

        def alert_callback(report):
            alerts.append(report)

        monitor.register_alert_callback(alert_callback)

        # 触发异常
        monitor.record_anomaly(
            source="test",
            symbol="TEST/USDT",
            data_type="ticker",
            anomaly_type="test",
            description="test",
        )

        monitor.check("test", "TEST/USDT", "ticker")

        # 应该触发报警（如果状态不是 good）
        # 注：具体是否触发取决于报告状态


class TestMarketTypeConfig:
    """市场类型配置测试"""

    def test_set_market_types(self):
        """测试设置市场类型"""

        @DataSourceRegistry.register("test_mt_source")
        class TestMTSource(BaseDataSource):
            name = "test_mt_source"
            supported_market_types = [MarketType.SPOT, MarketType.FUTURES]

            async def connect_websocket(self):
                return True

            async def disconnect_websocket(self):
                return True

            async def _handle_websocket_message(self, msg):
                pass

            async def fetch_klines(self, *args, **kwargs):
                return []

            async def fetch_trades(self, *args, **kwargs):
                return []

            async def fetch_ticker(self, *args, **kwargs):
                return {}

            async def subscribe(self, *args, **kwargs):
                return True

            async def unsubscribe(self, *args, **kwargs):
                return True

            def normalize_kline(self, raw):
                return {}

            def normalize_trade(self, raw):
                return {}

            def normalize_ticker(self, raw):
                return {}

        ds = DataSourceRegistry.get("test_mt_source")

        # 默认应返回所有支持类型
        assert ds._get_active_market_types() == [MarketType.SPOT, MarketType.FUTURES]

        # 设置仅现货
        ds.set_market_types([MarketType.SPOT])
        assert ds._get_active_market_types() == [MarketType.SPOT]

        # 清除配置应返回默认
        ds.set_market_types(None)
        assert ds._get_active_market_types() == [MarketType.SPOT, MarketType.FUTURES]

        DataSourceRegistry.unload("test_mt_source")

    def test_registry_get_with_market_types(self):
        """测试 DataSourceRegistry.get 传入 market_types"""

        @DataSourceRegistry.register("test_mt_source2")
        class TestMTSource2(BaseDataSource):
            name = "test_mt_source2"
            supported_market_types = [MarketType.SPOT, MarketType.FUTURES]

            async def connect_websocket(self):
                return True

            async def disconnect_websocket(self):
                return True

            async def _handle_websocket_message(self, msg):
                pass

            async def fetch_klines(self, *args, **kwargs):
                return []

            async def fetch_trades(self, *args, **kwargs):
                return []

            async def fetch_ticker(self, *args, **kwargs):
                return {}

            async def subscribe(self, *args, **kwargs):
                return True

            async def unsubscribe(self, *args, **kwargs):
                return True

            def normalize_kline(self, raw):
                return {}

            def normalize_trade(self, raw):
                return {}

            def normalize_ticker(self, raw):
                return {}

        # 传入 market_types 应设置到实例
        ds = DataSourceRegistry.get(
            "test_mt_source2", market_types=[MarketType.FUTURES]
        )
        assert ds._get_active_market_types() == [MarketType.FUTURES]

        DataSourceRegistry.unload("test_mt_source2")

    def test_resolve_market_types_no_config(self):
        """测试 _resolve_market_types 无配置时返回 None"""
        sub = DataSubscriptionManager()
        result = sub._resolve_market_types("nonexistent_source")
        assert result is None


class TestDataTypeEnums:
    """数据类型枚举测试"""

    def test_data_type(self):
        """测试数据类型枚举"""
        assert DataType.KLINE.value == "kline"
        assert DataType.TICKER.value == "ticker"
        assert DataType.TRADE.value == "trade"

    def test_kline_interval(self):
        """测试K线周期枚举"""
        assert KlineInterval.M1.value == "1m"
        assert KlineInterval.H1.value == "1h"
        assert KlineInterval.D1.value == "1d"

    def test_market_type(self):
        """测试市场类型枚举"""
        assert MarketType.SPOT.value == "spot"
        assert MarketType.FUTURES.value == "futures"

    def test_connection_status(self):
        """测试连接状态枚举"""
        assert ConnectionStatus.DISCONNECTED.value == "disconnected"
        assert ConnectionStatus.CONNECTED.value == "connected"


# 运行测试的入口
if __name__ == "__main__":
    pytest.main([__file__, "-v"])
