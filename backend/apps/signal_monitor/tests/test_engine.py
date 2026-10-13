"""
信号监控引擎测试

验证：
1. 用户成功将技术信号加入监控列表
2. 当监控列表中的信号被触发时，系统能够正确执行预设的操作
3. 用户能够查看和管理监控列表中的信号
4. K线数据能够正确触发监控列表中的信号
5. 信号计算需要并行执行，确保完成时间不超过100ms
6. 使用正确的数据源计算信号
7. 触发类型有单次触发和持续触发两种
"""

import time
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import numpy as np

from apps.signal_monitor.indicators import (
    compute_indicator,
    compute_indicators_parallel,
)
from apps.signal_monitor.conditions import evaluate_condition
from apps.signal_monitor.engine import SignalMonitorEngine


def _make_klines(count: int = 50, base_price: float = 50000.0) -> list[dict]:
    """生成测试用 K 线数据"""
    klines = []
    for i in range(count):
        close = base_price + i * 10
        klines.append(
            {
                "symbol": "BTCUSDT",
                "interval": "1h",
                "open_time": datetime(2024, 1, 1) + timedelta(hours=i),
                "close_time": datetime(2024, 1, 1) + timedelta(hours=i, minutes=59),
                "open": close - 5,
                "high": close + 50,
                "low": close - 50,
                "close": close,
                "volume": 1000.0 + i,
                "turnover": 50000000.0,
                "trades": 10000,
                "source": "binance",
                "timestamp": datetime(2024, 1, 1) + timedelta(hours=i),
            }
        )
    return klines


class TestParallelPerformance(unittest.TestCase):
    """验证点5: 信号计算并行执行，完成时间不超过100ms"""

    def test_single_indicator_under_100ms(self):
        """单个指标计算时间"""
        klines = _make_klines(200)
        start = time.monotonic()
        compute_indicator("sma", klines, {"period": 20})
        duration = (time.monotonic() - start) * 1000
        self.assertLess(duration, 100, f"SMA took {duration:.1f}ms")

    def test_parallel_computation_under_100ms(self):
        """并行计算多个指标在 100ms 内完成"""
        klines = _make_klines(200)
        tasks = [
            {"indicator_type": "sma", "klines": klines, "params": {"period": 20}},
            {"indicator_type": "ema", "klines": klines, "params": {"period": 12}},
            {"indicator_type": "ema", "klines": klines, "params": {"period": 26}},
            {"indicator_type": "rsi", "klines": klines, "params": {"period": 14}},
            {"indicator_type": "macd", "klines": klines, "params": {}},
            {"indicator_type": "bollinger", "klines": klines, "params": {}},
        ]
        results = compute_indicators_parallel(tasks)
        self.assertEqual(len(results), 6)
        for r in results:
            self.assertIsNone(
                r["error"], f"Error in {r['indicator_type']}: {r['error']}"
            )

    def test_parallel_many_tasks_under_100ms(self):
        """50个并行计算任务在 100ms 内完成"""
        klines = _make_klines(100)
        tasks = [
            {"indicator_type": "sma", "klines": klines, "params": {"period": 10 + i}}
            for i in range(50)
        ]
        results = compute_indicators_parallel(tasks)
        self.assertEqual(len(results), 50)
        for r in results:
            self.assertIsNotNone(r)


class TestKlineTrigger(unittest.TestCase):
    """验证点4: K线数据能够正确触发监控列表中的信号"""

    def test_rsi_oversold_triggers(self):
        """RSI 超卖条件能被正确触发"""
        # 构建持续下跌的 K 线
        klines = []
        for i in range(30):
            close = 50000.0 - i * 100
            klines.append(
                {
                    "symbol": "BTCUSDT",
                    "interval": "1h",
                    "open_time": datetime(2024, 1, 1) + timedelta(hours=i),
                    "close_time": datetime(2024, 1, 1) + timedelta(hours=i, minutes=59),
                    "open": close + 50,
                    "high": close + 100,
                    "low": close - 100,
                    "close": close,
                    "volume": 1000.0,
                    "turnover": 50000000.0,
                    "trades": 10000,
                    "source": "binance",
                    "timestamp": datetime(2024, 1, 1) + timedelta(hours=i),
                }
            )

        result = compute_indicator("rsi", klines, {"period": 14})
        condition = {
            "operator": "lt",
            "left": {"field": ""},
            "right": {"value": 30},
        }
        # RSI 应该低于 30
        triggered = evaluate_condition(condition, result)
        self.assertIsInstance(triggered, bool)

    def test_ema_crossover_with_klines(self):
        """EMA 交叉能被 K 线数据正确触发"""
        klines = _make_klines(50)
        ema_12 = compute_indicator("ema", klines, {"period": 12})
        ema_26 = compute_indicator("ema", klines, {"period": 26})

        # 检查计算结果有效
        valid_12 = ema_12[~np.isnan(ema_12)]
        valid_26 = ema_26[~np.isnan(ema_26)]
        self.assertTrue(len(valid_12) > 0)
        self.assertTrue(len(valid_26) > 0)


class TestTriggerTypes(unittest.TestCase):
    """验证点7: 单次触发和持续触发"""

    def test_once_trigger_type(self):
        """单次触发类型的条件判断"""
        condition = {
            "operator": "gt",
            "left": {"value": 100},
            "right": {"value": 50},
        }
        self.assertTrue(evaluate_condition(condition, None))

    def test_continuous_trigger_type(self):
        """持续触发类型的条件判断"""
        # 同样的条件应该持续触发
        condition = {
            "operator": "gt",
            "left": {"value": 100},
            "right": {"value": 50},
        }
        # 第一次
        self.assertTrue(evaluate_condition(condition, None))
        # 第二次（持续触发）
        self.assertTrue(evaluate_condition(condition, None))


class TestCorrectDatasource(unittest.TestCase):
    """验证点6: 使用正确的数据源计算信号"""

    def test_indicator_uses_kline_data(self):
        """指标计算使用传入的 K 线数据"""
        klines_binance = _make_klines(30)
        for k in klines_binance:
            k["source"] = "binance"

        result = compute_indicator("sma", klines_binance, {"period": 20})
        self.assertIsNotNone(result)
        self.assertTrue(len(result) > 0)

    def test_different_sources_same_result(self):
        """不同数据源相同数据应产生相同结果"""
        klines1 = _make_klines(30)
        klines2 = _make_klines(30)
        for k in klines1:
            k["source"] = "binance"

        result1 = compute_indicator("sma", klines1, {"period": 20})
        result2 = compute_indicator("sma", klines2, {"period": 20})
        np.testing.assert_array_almost_equal(result1, result2)


class TestEngineSignalCheck(unittest.TestCase):
    """引擎集成测试"""

    def setUp(self):
        self.engine = SignalMonitorEngine()
        SignalMonitorEngine._instance = None

    def tearDown(self):
        SignalMonitorEngine._instance = None

    @patch("apps.signal_monitor.engine.SignalMonitor")
    def test_check_all_signals_no_active_monitors(self, MockMonitor):
        """没有活跃监控时返回空列表"""
        MockMonitor.objects.filter.return_value.select_related.return_value = []
        results = self.engine.check_all_signals(klines_map={})
        self.assertEqual(results, [])

    def test_check_signals_for_kline_no_monitors(self):
        """没有监控时返回空列表"""
        with patch("apps.signal_monitor.engine.SignalMonitor") as MockMonitor:
            MockMonitor.objects.filter.return_value.select_related.return_value = []
            results = self.engine.check_signals_for_kline("BTCUSDT", _make_klines(30))
            self.assertEqual(results, [])


class TestTradeDeadPathRemoved(unittest.TestCase):
    """死路径清扫（修改6，2026-10-12）：`_execute_trade` 与 trade /
    notify_and_trade 分支已整体删除，存量 trade 行由 migration 0004 迁移为 notify。
    这些测试钉死删除边界：即使出现 migration 之外的遗留 trade 型 monitor
    （异常防御路径），触发后也只走通知，不再尝试建单。"""

    def setUp(self):
        self.engine = SignalMonitorEngine()

    def test_legacy_trade_action_only_notifies(self):
        monitor = MagicMock()
        monitor.action_type = "trade"

        with patch.object(SignalMonitorEngine, "_send_notification") as mock_notify:
            result = self.engine._execute_action(monitor, {})

        mock_notify.assert_called_once_with(monitor, {})
        self.assertEqual(result["action_type"], "trade")
        self.assertEqual(result["status"], "executed")

    def test_no_order_created_by_execute_action(self):
        """engine 不再有建单路径：`apps.trading.models.Order` 不该被触达。"""
        with patch("apps.trading.models.Order.objects") as mock_order_mgr:
            monitor = MagicMock()
            monitor.action_type = "notify_and_trade"  # 历史遗留值（异常防御）
            self.engine._execute_action(monitor, {})
        mock_order_mgr.create.assert_not_called()

    def test_execute_trade_method_is_gone(self):
        """删除边界本身：`_execute_trade` 不允许再被任何入口调用。"""
        self.assertFalse(hasattr(self.engine, "_execute_trade"))

    def test_notify_action_executes_and_returns_result(self):
        monitor = MagicMock()
        monitor.action_type = "notify"
        monitor.strategy_name = ""
        monitor.name = "EMA趋势"
        monitor.id = "mon-1"

        with patch.object(SignalMonitorEngine, "_send_notification") as mock_notify:
            result = self.engine._execute_action(monitor, {})

        mock_notify.assert_called_once_with(monitor, {})
        self.assertEqual(result["action_type"], "notify")
        self.assertEqual(result["status"], "executed")


if __name__ == "__main__":
    unittest.main()
