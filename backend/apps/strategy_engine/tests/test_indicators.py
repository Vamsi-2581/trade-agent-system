"""Tests for strategy engine indicators module."""

from __future__ import annotations

import pytest
import numpy as np

from apps.strategy_engine.indicators import atr, horizontal_key_levels


class TestAtr:
    """测试 atr 指标的多种调用方式"""

    def setup_method(self):
        """准备测试用 K 线数据"""
        self.history = [
            {"high": 105, "low": 95, "close": 100, "open": 98},
            {"high": 108, "low": 97, "close": 103, "open": 100},
            {"high": 110, "low": 99, "close": 107, "open": 103},
            {"high": 112, "low": 100, "close": 105, "open": 107},
            {"high": 115, "low": 102, "close": 110, "open": 105},
            {"high": 118, "low": 105, "close": 113, "open": 110},
            {"high": 120, "low": 108, "close": 116, "open": 113},
            {"high": 122, "low": 110, "close": 118, "open": 116},
            {"high": 125, "low": 112, "close": 120, "open": 118},
            {"high": 128, "low": 115, "close": 123, "open": 120},
            {"high": 130, "low": 118, "close": 125, "open": 123},
            {"high": 132, "low": 120, "close": 128, "open": 125},
            {"high": 135, "low": 122, "close": 130, "open": 128},
            {"high": 138, "low": 125, "close": 133, "open": 130},
            {"high": 140, "low": 128, "close": 135, "open": 133},
            {"high": 142, "low": 130, "close": 138, "open": 135},
        ]
        self.highs = np.array([k["high"] for k in self.history], dtype=np.float64)
        self.lows = np.array([k["low"] for k in self.history], dtype=np.float64)
        self.closes = np.array([k["close"] for k in self.history], dtype=np.float64)
        # 策略实际使用 list[float] 而非 np.ndarray
        self.highs_list = [k["high"] for k in self.history]
        self.lows_list = [k["low"] for k in self.history]
        self.closes_list = [k["close"] for k in self.history]

    def test_atr_history_form(self):
        """atr(history, period) — K 线 dict 列表形式"""
        result = atr(self.history, 7)
        assert isinstance(result, np.ndarray)
        assert len(result) == len(self.history)
        # 最后一个值应该是合理的正数
        assert result[-1] > 0

    def test_atr_history_default_period(self):
        """atr(history) — 使用默认周期 14"""
        result = atr(self.history)
        assert isinstance(result, np.ndarray)
        assert len(result) == len(self.history)

    def test_atr_three_arrays_with_period_keyword(self):
        """atr(highs, lows, closes, period=N) — 三数组 + keyword period

        这是之前报错 "got multiple values for argument 'period'" 的调用方式。
        """
        result = atr(self.highs, self.lows, self.closes, period=7)
        assert isinstance(result, np.ndarray)
        assert len(result) == len(self.history)
        assert result[-1] > 0

    def test_atr_three_arrays_custom_period(self):
        """atr(highs, lows, closes, period=10) — 非默认周期"""
        result = atr(self.highs, self.lows, self.closes, period=10)
        assert isinstance(result, np.ndarray)
        assert len(result) == len(self.history)

    def test_atr_consistency(self):
        """两种调用方式应该产生相同结果"""
        result_history = atr(self.history, 7)
        result_arrays = atr(self.highs, self.lows, self.closes, period=7)
        # 最后一个值应该相同（或非常接近，考虑到浮点精度）
        assert abs(result_history[-1] - result_arrays[-1]) < 1e-10

    def test_atr_three_list_floats(self):
        """atr([float], [float], [float], period=N) — list[float] 三数组形式

        这是策略文件中实际使用的调用方式：
        highs = [bar["high"] for bar in history]
        atr(highs, lows, closes, period=self.atr_period)
        """
        result = atr(self.highs_list, self.lows_list, self.closes_list, period=7)
        assert isinstance(result, np.ndarray)
        assert len(result) == len(self.history)
        assert result[-1] > 0

    def test_atr_list_floats_consistency(self):
        """list[float] 形式应该与 np.ndarray 形式产生相同结果"""
        result_np = atr(self.highs, self.lows, self.closes, period=7)
        result_list = atr(self.highs_list, self.lows_list, self.closes_list, period=7)
        assert abs(result_np[-1] - result_list[-1]) < 1e-10


class TestHorizontalKeyLevels:
    def _history(self):
        lows = [
            100,
            102,
            99,
            101,
            103,
            100,
            104,
            106,
            99,
            105,
            107,
            100,
            108,
            110,
            99,
            109,
            111,
            100,
            112,
            114,
        ]
        highs = [
            110,
            112,
            115,
            113,
            111,
            116,
            114,
            112,
            115,
            113,
            111,
            116,
            114,
            112,
            115,
            113,
            111,
            116,
            114,
            112,
        ]
        return [
            {
                "timestamp": f"2026-01-01T00:{i:02d}:00Z",
                "open": (high + low) / 2,
                "high": high,
                "low": low,
                "close": (high + low) / 2,
                "volume": 1000,
            }
            for i, (high, low) in enumerate(zip(highs, lows))
        ]

    def test_returns_levels_from_local_pivots(self):
        result = horizontal_key_levels(
            self._history(), current_price=106, atr_period=3, max_levels=20
        )

        assert result["levels"]
        assert result["supports"]
        assert result["resistances"]
        assert result["levels"][0]["touches"] >= 3

    def test_consecutive_hits_count_as_one_touch_cluster(self):
        history = self._history()
        history[8]["low"] = 99
        history[9]["low"] = 99
        history[10]["low"] = 99

        result = horizontal_key_levels(
            history, current_price=106, atr_period=3, min_gap_bars=3
        )
        low_level = min(result["levels"], key=lambda level: level["price"])

        assert low_level["touches"] == 3

    def test_dynamic_tolerance_is_clamped(self):
        result = horizontal_key_levels(self._history(), atr_period=3)

        assert 0.001 <= result["tolerance_pct"] <= 0.01

    def test_without_current_price_does_not_classify(self):
        result = horizontal_key_levels(self._history(), atr_period=3)

        assert result["levels"]
        assert result["supports"] == []
        assert result["resistances"] == []

    def test_invalid_kline_raises(self):
        history = self._history()
        del history[0]["low"]

        with pytest.raises(ValueError):
            horizontal_key_levels(history, atr_period=3)

    def test_insufficient_klines_raises(self):
        with pytest.raises(ValueError):
            horizontal_key_levels(self._history()[:5], atr_period=14)
