"""T2（[#27](https://github.com/eazlong/trade-agent-system/issues/27)）/ spec D5：
夏普按 K 线周期年化。

修复前的口径：``_compute_sharpe`` 用每根 bar 的权益收益率 × ``sqrt(252)``，与
``timeframe`` 无关 → 「夏普 ≥1.5」在 1h 与 1d 上不是同一条线。修复后年化系数由
``bars_per_year(timeframe)`` 推出，1d 仍是 252（口径不变）。

本文件钉住四件事：
1. ``bars_per_year`` 的取值（1d→252 口径不变，其余按 252×每日 bar 数）；
2. 未知 / 非法 ``timeframe`` 报错，**不静默按 252 算**；
3. 1d 的夏普与「修复前的 ``sqrt(252)`` 公式」逐值一致 ⇒ 日频口径没漂移；
4. 同一份 K 线在 1d / 1h / 4h / 15m 下的夏普比值 == 每年 bar 数比值的平方根。
"""

from __future__ import annotations

import asyncio
import math
from datetime import datetime
from decimal import Decimal

import pytest

from apps.strategy_engine.backtest_mode import (
    SECONDS_PER_DAY,
    TRADING_DAYS_PER_YEAR,
    BacktestEngine,
    bars_per_year,
)
from apps.strategy_engine.base import BaseStrategy, OrderSignal, StrategyContext

CAPITAL = Decimal("1000000")


class _TradingStrategy(BaseStrategy):
    """固定节奏买卖的策略：只为造出一条非平坦的权益曲线。"""

    name = "test_sharpe_trading"
    description = "交替买卖，造出非平坦权益曲线"
    params_schema = {}
    QTY = Decimal("0.1")

    def on_bar(self, kline: dict, history: list[dict]):
        idx = len(history) - 1
        if idx == 0:
            return None
        if idx % 4 == 0:
            return OrderSignal(
                side="buy",
                quantity=self.QTY,
                order_type="market",
                price=Decimal(str(kline["close"])),
                signal_name="entry",
                exchange="binance",
                metadata={},
            )
        if idx % 4 == 2:
            return OrderSignal(
                side="sell",
                quantity=self.QTY,
                order_type="market",
                price=Decimal(str(kline["close"])),
                signal_name="exit",
                exchange="binance",
                metadata={},
            )
        return None

    def on_start(self):
        pass

    def on_stop(self):
        pass


def _ohlcv(bars: int = 24) -> list[dict]:
    """确定性锯齿价格序列（同一份 K 线在所有用例间复用）。"""
    base = datetime(2024, 1, 1, 0, 0)
    rows = []
    for i in range(bars):
        close = 42000.0 * (1 + 0.01 * (((i * 7) % 11) - 5))
        rows.append(
            {
                "timestamp": datetime.isoformat(base.replace(hour=i % 24)),
                "open": close - 10,
                "high": close + 20,
                "low": close - 20,
                "close": close,
                "volume": 100.0,
            }
        )
    return rows


def _run(timeframe: str, ohlcv: list[dict]) -> dict:
    engine = BacktestEngine(
        strategy=_TradingStrategy(
            StrategyContext(
                symbol="BTC/USDT",
                timeframe=timeframe,
                mode="backtest",
                params={"quantity": 0.1},
            )
        ),
        ohlcv_data=list(ohlcv),
        initial_capital=CAPITAL,
        symbol="BTC/USDT",
        timeframe=timeframe,
    )
    return asyncio.run(engine.run())


def _legacy_sharpe(equity_curve: list[dict]) -> float:
    """修复前的公式：每根 bar 权益收益率 × sqrt(252)（独立重算，不用被测代码）。"""
    returns = [
        (equity_curve[i]["equity"] - equity_curve[i - 1]["equity"])
        / equity_curve[i - 1]["equity"]
        for i in range(1, len(equity_curve))
        if equity_curve[i - 1]["equity"] > 0
    ]
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / len(returns)
    return (mean / math.sqrt(var)) * math.sqrt(252)


class TestBarsPerYear:
    """每年 bar 数：唯一出处，1d 口径钉死为 252。"""

    @pytest.mark.parametrize(
        ("timeframe", "expected"),
        [
            ("1d", 252),  # 修复前口径，必须不变
            ("4h", 252 * 6),  # 1512
            ("1h", 252 * 24),  # 6048
            ("15m", 252 * 96),  # 24192
            ("30m", 252 * 48),
            ("5m", 252 * 288),
            ("1w", 252 / 7),  # 36.0（252 交易日 × 每周 1/7 根）
        ],
    )
    def test_named_timeframes(self, timeframe: str, expected: float):
        assert bars_per_year(timeframe) == pytest.approx(expected, rel=1e-12)

    def test_derived_from_single_timeframe_source(self):
        """口径来自既有的 timeframe→秒 唯一出处，不是第二张手写表。"""
        assert bars_per_year("1d") == (TRADING_DAYS_PER_YEAR * SECONDS_PER_DAY / 86400)
        assert bars_per_year("3m") == pytest.approx(252 * 480)

    @pytest.mark.parametrize("bad", ["banana", "", "1x", "1 h", "h", "-1h", "0h"])
    def test_unknown_timeframe_is_loud(self, bad: str):
        """未知 / 非法 / 零步长周期一律报错——绝不静默回退 252。"""
        with pytest.raises(ValueError):
            bars_per_year(bad)


class TestSharpeScalesWithTimeframe:
    """同一份 K 线：夏普只随年化系数变化，比值 == 每年 bar 数比值的平方根。"""

    @pytest.fixture(scope="class")
    def per_timeframe(self) -> dict[str, dict]:
        ohlcv = _ohlcv()
        return {tf: _run(tf, ohlcv) for tf in ("1d", "4h", "1h", "15m")}

    def test_equity_curve_is_timeframe_independent(self, per_timeframe):
        """比值用例的前提：模拟本身不看 timeframe，权益曲线逐点相同。"""
        base = per_timeframe["1d"]["equity_curve"]
        for tf in ("4h", "1h", "15m"):
            assert per_timeframe[tf]["equity_curve"] == base

    def test_1d_unchanged_versus_legacy_252(self, per_timeframe):
        """1d 的既有口径逐个不变：与修复前的 sqrt(252) 公式逐值一致。"""
        stats = per_timeframe["1d"]
        assert stats["sharpe_ratio"] == pytest.approx(
            _legacy_sharpe(stats["equity_curve"]), rel=1e-9
        )

    @pytest.mark.parametrize("timeframe", ["4h", "1h", "15m"])
    def test_ratio_equals_bars_per_year_ratio(self, per_timeframe, timeframe: str):
        day = per_timeframe["1d"]["sharpe_ratio"]
        other = per_timeframe[timeframe]["sharpe_ratio"]
        expected_ratio = math.sqrt(bars_per_year(timeframe) / bars_per_year("1d"))

        assert other / day == pytest.approx(expected_ratio, rel=1e-12)
        # 1h 的比值就是 sqrt(24)
        if timeframe == "1h":
            assert other / day == pytest.approx(math.sqrt(24), rel=1e-12)

    def test_non_1d_values_move_off_the_day_line(self, per_timeframe):
        """修复前这四个周期会算出同一个数（同一权益曲线 × sqrt(252)）；现在必须分开。"""
        legacy = _legacy_sharpe(per_timeframe["1d"]["equity_curve"])
        assert per_timeframe["1h"]["sharpe_ratio"] != pytest.approx(legacy, rel=1e-6)
        assert per_timeframe["15m"]["sharpe_ratio"] != pytest.approx(legacy, rel=1e-6)
        # 修复后的非 1d 值 == 修复前公式 × sqrt(该周期每年 bar 数 / 252)
        for tf in ("4h", "1h", "15m"):
            assert per_timeframe[tf]["sharpe_ratio"] == pytest.approx(
                legacy * math.sqrt(bars_per_year(tf) / 252), rel=1e-9
            )


class TestUnknownTimeframeFailsLoudInRun:
    def test_full_run_raises_on_unknown_timeframe(self):
        """整条回测链路上也是报错，不是静默按日频算出一朵花。"""
        with pytest.raises(ValueError):
            _run("banana", _ohlcv())

    def test_normal_timeframe_still_runs(self):
        stats = _run("1h", _ohlcv())
        assert stats["sharpe_ratio"] is not None
