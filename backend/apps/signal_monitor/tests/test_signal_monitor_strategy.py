"""
测试策略信号注册到 SignalMonitor 的完整链路

验证：
1. BaseStrategy.get_watch_signals() 默认返回 []
2. donchian 指标计算正确
3. validate_strategy 动作发布事件到 Redis List
4. LiveStrategyRunner 注册/注销信号监控
5. 完整的初筛→验证→分发链路
"""

import json
import unittest
from unittest.mock import MagicMock, patch

import numpy as np

from apps.strategy_engine.base import BaseStrategy, StrategyContext
from apps.signal_monitor.indicators import compute_donchian
from apps.signal_monitor.engine import SignalMonitorEngine


# ——— 测试用策略 ———


class MockStrategy(BaseStrategy):
    """不带 watch_signals 的策略（默认行为）"""

    name = "mock_strategy"

    def on_bar(self, kline, history):
        return None


class StrategyWithWatchSignals(BaseStrategy):
    """带 watch_signals 声明的策略"""

    name = "strategy_with_watch"

    def on_bar(self, kline, history):
        return self.ctx.buy(
            quantity=self.ctx.balance,
            signal_name="entry",
        )

    def get_watch_signals(self):
        return [
            {
                "interval": "15m",
                "indicator_type": "donchian",
                "indicator_params": {"period": 20},
                "condition": {
                    "operator": "cross_above",
                    "left": {"field": "price"},
                    "right": {"field": "upper"},
                },
                "trigger_type": "continuous",
            }
        ]


# ——— 测试 ———


class TestBaseStrategy(unittest.TestCase):
    """验证点1: 默认 get_watch_signals 返回 []"""

    def test_default_watch_signals_empty(self):
        ctx = StrategyContext("BTC/USDT", "1h", "live", {})
        strategy = MockStrategy(ctx)
        self.assertEqual(strategy.get_watch_signals(), [])

    def test_custom_watch_signals(self):
        ctx = StrategyContext("BTC/USDT", "1h", "live", {})
        strategy = StrategyWithWatchSignals(ctx)
        signals = strategy.get_watch_signals()
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0]["indicator_type"], "donchian")
        self.assertEqual(signals[0]["interval"], "15m")


class TestDonchianIndicator(unittest.TestCase):
    """验证点2: donchian 指标计算"""

    def test_donchian_basic(self):
        highs = np.array([10, 12, 15, 13, 14, 16, 18, 17, 20, 19], dtype=np.float64)
        lows = np.array([5, 6, 7, 6, 8, 10, 12, 11, 14, 13], dtype=np.float64)
        result = compute_donchian(highs, lows, period=3)

        self.assertIn("upper", result)
        self.assertIn("lower", result)
        self.assertEqual(len(result["upper"]), 10)
        self.assertEqual(len(result["lower"]), 10)

        # 前 period-1 个值为 NaN
        self.assertTrue(np.isnan(result["upper"][0]))
        self.assertTrue(np.isnan(result["upper"][1]))

        # period=3: 第3个位置 (index 2) = max(highs[0:3]) = max(10,12,15) = 15
        self.assertAlmostEqual(result["upper"][2], 15.0)
        self.assertAlmostEqual(result["lower"][2], 5.0)

    def test_donchian_insufficient_data(self):
        highs = np.array([10, 12], dtype=np.float64)
        lows = np.array([5, 6], dtype=np.float64)
        result = compute_donchian(highs, lows, period=20)
        self.assertTrue(np.all(np.isnan(result["upper"])))
        self.assertTrue(np.all(np.isnan(result["lower"])))


class TestValidateStrategyAction(unittest.TestCase):
    """验证点3: validate_strategy 动作发布事件到 Redis List"""

    def setUp(self):
        self.engine = SignalMonitorEngine()
        SignalMonitorEngine._instance = None

    def tearDown(self):
        SignalMonitorEngine._instance = None

    def test_validate_strategy_publishes_event(self):
        from apps.signal_monitor.models import SignalMonitor

        monitor = SignalMonitor(
            name="test_validate",
            symbol="BTC/USDT",
            interval="15m",
            indicator_type="donchian",
            condition={
                "operator": "gt",
                "left": {"field": "price"},
                "right": {"value": 50000},
            },
            action_type="validate_strategy",
            strategy_name="turtle_strategy",
            live_session_id="test-session-123",
            status="active",
        )

        mock_r = MagicMock()
        # redis 在 _publish_strategy_validate 方法内 import，patch redis 包的 from_url
        with patch("redis.from_url", return_value=mock_r):
            result = self.engine._publish_strategy_validate(
                monitor, {"left": 51000, "right": 50000}
            )

            self.assertEqual(result["action_type"], "validate_strategy")
            self.assertEqual(result["status"], "published")

            # 验证 RPUSH 被调用
            mock_r.rpush.assert_called_once()
            args = mock_r.rpush.call_args[0]
            self.assertIn("strategy:validate:test-session-123", args[0])
            payload = json.loads(args[1])
            self.assertEqual(payload["strategy_name"], "turtle_strategy")
            self.assertEqual(payload["symbol"], "BTC/USDT")

    def test_validate_strategy_no_session_id_skips(self):
        from apps.signal_monitor.models import SignalMonitor

        monitor = SignalMonitor(
            name="test_no_session",
            symbol="BTC/USDT",
            interval="15m",
            indicator_type="donchian",
            condition={},
            action_type="validate_strategy",
            strategy_name="turtle_strategy",
            live_session_id="",  # 空
            status="active",
        )

        result = self.engine._publish_strategy_validate(monitor, {})
        self.assertEqual(result["status"], "skipped")


class TestLiveStrategyRunnerSignalRegistration(unittest.TestCase):
    """验证点4: LiveStrategyRunner 注册/注销信号 — 用 mock 避免 test DB 问题"""

    def test_register_signal_monitors(self):
        import asyncio
        from apps.strategy_engine.live_mode import LiveStrategyRunner

        ctx = StrategyContext("BTC/USDT", "1h", "live", {})
        strategy = StrategyWithWatchSignals(ctx)
        runner = LiveStrategyRunner(
            strategy=strategy,
            symbol="BTC/USDT",
            timeframe="1h",
            exchange_account_id="ex-123",
            user_id="user-123",
            live_session_id="test-session-456",
        )

        async def _run():
            with patch("apps.signal_monitor.models.SignalMonitor") as MockModel:
                await runner._register_signal_monitors()

                # 验证 update_or_create 被调用，参数正确
                MockModel.objects.update_or_create.assert_called_once()
                call_kwargs = MockModel.objects.update_or_create.call_args.kwargs
                self.assertEqual(call_kwargs["strategy_name"], "strategy_with_watch")
                self.assertEqual(call_kwargs["live_session_id"], "test-session-456")
                self.assertEqual(call_kwargs["symbol"], "BTC/USDT")
                self.assertEqual(call_kwargs["interval"], "15m")
                self.assertEqual(call_kwargs["defaults"]["indicator_type"], "donchian")
                self.assertEqual(
                    call_kwargs["defaults"]["action_type"], "validate_strategy"
                )
                self.assertEqual(call_kwargs["defaults"]["status"], "active")
                self.assertEqual(call_kwargs["defaults"]["user_id"], "user-123")

        asyncio.run(_run())

    def test_default_strategy_no_registration(self):
        import asyncio
        from apps.strategy_engine.live_mode import LiveStrategyRunner

        ctx = StrategyContext("BTC/USDT", "1h", "live", {})
        strategy = MockStrategy(ctx)
        runner = LiveStrategyRunner(
            strategy=strategy,
            symbol="BTC/USDT",
            timeframe="1h",
            exchange_account_id="ex-123",
            live_session_id="test-session-789",
        )

        async def _run():
            with patch("apps.signal_monitor.models.SignalMonitor") as MockModel:
                await runner._register_signal_monitors()
                # 默认策略无 watch_signals，不应调用 DB
                MockModel.objects.update_or_create.assert_not_called()

        asyncio.run(_run())

    def test_unregister_expires_monitors(self):
        import asyncio
        from apps.strategy_engine.live_mode import LiveStrategyRunner

        ctx = StrategyContext("BTC/USDT", "1h", "live", {})
        strategy = StrategyWithWatchSignals(ctx)
        runner = LiveStrategyRunner(
            strategy=strategy,
            symbol="BTC/USDT",
            timeframe="1h",
            exchange_account_id="ex-123",
            live_session_id="test-session-cleanup",
        )

        async def _run():
            with patch("apps.signal_monitor.models.SignalMonitor") as MockModel:
                await runner._unregister_signal_monitors()

                # 验证 filter().update() 被调用
                mock_filter = MockModel.objects.filter
                mock_filter.assert_called_once_with(
                    live_session_id="test-session-cleanup",
                    strategy_name="strategy_with_watch",
                )
                mock_filter.return_value.update.assert_called_once_with(
                    status="expired"
                )

        asyncio.run(_run())

    def test_unregister_empty_session_id_skips(self):
        """live_session_id 为空时不应调用 DB，避免误匹配其他 runner 的记录"""
        import asyncio
        from apps.strategy_engine.live_mode import LiveStrategyRunner

        ctx = StrategyContext("BTC/USDT", "1h", "live", {})
        strategy = StrategyWithWatchSignals(ctx)
        runner = LiveStrategyRunner(
            strategy=strategy,
            symbol="BTC/USDT",
            timeframe="1h",
            exchange_account_id="ex-123",
            live_session_id="",  # 空 session
        )

        async def _run():
            with patch("apps.signal_monitor.models.SignalMonitor") as MockModel:
                await runner._unregister_signal_monitors()
                # 空 session 应该直接返回，不调用 DB
                MockModel.objects.filter.assert_not_called()

        asyncio.run(_run())

    def test_register_empty_user_id_skips(self):
        """user_id 为空时不应注册信号监控"""
        import asyncio
        from apps.strategy_engine.live_mode import LiveStrategyRunner

        ctx = StrategyContext("BTC/USDT", "1h", "live", {})
        strategy = StrategyWithWatchSignals(ctx)
        runner = LiveStrategyRunner(
            strategy=strategy,
            symbol="BTC/USDT",
            timeframe="1h",
            exchange_account_id="ex-123",
            live_session_id="test-session-nouser",
        )

        async def _run():
            with patch("apps.signal_monitor.models.SignalMonitor") as MockModel:
                await runner._register_signal_monitors()
                # 无 user_id 应直接返回，不调用 DB
                MockModel.objects.update_or_create.assert_not_called()

        asyncio.run(_run())


class TestOnValidateTrigger(unittest.TestCase):
    """验证点5: 触发后验证完整策略"""

    def test_validate_trigger_confirms_signal(self):
        import asyncio
        from decimal import Decimal
        from apps.strategy_engine.live_mode import LiveStrategyRunner

        ctx = StrategyContext(
            "BTC/USDT",
            "1h",
            "live",
            {},
            balance=Decimal("10000"),
            position=Decimal("0"),
        )
        strategy = StrategyWithWatchSignals(ctx)
        runner = LiveStrategyRunner(
            strategy=strategy,
            symbol="BTC/USDT",
            timeframe="1h",
            exchange_account_id="ex-123",
            live_session_id="test-session-trigger",
        )

        # 填充历史 K 线
        runner._kline_history = [
            {
                "open": 50000 + i * 100,
                "high": 50100 + i * 100,
                "low": 49900 + i * 100,
                "close": 50050 + i * 100,
                "volume": 100,
                "timestamp": i,
            }
            for i in range(50)
        ]
        runner._dispatcher = MagicMock()

        async def _run():
            event = {
                "monitor_id": "test-monitor-1",
                "strategy_name": "strategy_with_watch",
                "symbol": "BTC/USDT",
                "interval": "15m",
                "trigger_value": {"left": 51000, "right": 50000},
            }

            await runner._on_validate_trigger(event)

            # 策略应确认信号并 dispatch
            runner._dispatcher.dispatch_with_risk_check.assert_called_once()
            call_kwargs = runner._dispatcher.dispatch_with_risk_check.call_args.kwargs
            self.assertEqual(call_kwargs["symbol"], "BTC/USDT")
            self.assertEqual(call_kwargs["signal"].side, "buy")
            self.assertEqual(call_kwargs["signal"].signal_name, "entry")

        asyncio.run(_run())

    def test_validate_trigger_no_signal(self):
        """策略拒绝信号时不应 dispatch"""
        import asyncio
        from decimal import Decimal
        from apps.strategy_engine.live_mode import LiveStrategyRunner

        ctx = StrategyContext(
            "BTC/USDT",
            "1h",
            "live",
            {},
            balance=Decimal("10000"),
            position=Decimal("0"),
        )
        strategy = StrategyWithWatchSignals(ctx)
        runner = LiveStrategyRunner(
            strategy=strategy,
            symbol="BTC/USDT",
            timeframe="1h",
            exchange_account_id="ex-123",
            live_session_id="test-session-noop",
        )
        runner._dispatcher = MagicMock()

        # 空 K 线历史 — 策略应返回 None（不满足条件）
        runner._kline_history = []

        async def _run():
            event = {
                "monitor_id": "test-monitor-2",
                "strategy_name": "strategy_with_watch",
                "symbol": "BTC/USDT",
                "interval": "15m",
                "trigger_value": {},
            }

            with patch.object(
                runner, "load_initial_history", new_callable=MagicMock
            ) as mock_load:
                mock_load.return_value = None
                await runner._on_validate_trigger(event)

            # 数据不足，不应 dispatch
            runner._dispatcher.dispatch_with_risk_check.assert_not_called()

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
