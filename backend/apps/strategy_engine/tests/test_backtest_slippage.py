"""Ticket 001: Backtest slippage model tests.

Slippage: buys fill worse (price * (1 + slippage)), sells fill worse
(price * (1 - slippage)). cost_detail accumulates per-trade costs.
"""

from datetime import datetime
from decimal import Decimal

import pytest

from apps.strategy_engine.backtest_mode import BacktestEngine, DEFAULT_SLIPPAGE_RATE
from apps.strategy_engine.base import BaseStrategy, OrderSignal, StrategyContext

SLIP = Decimal("0.0005")


class _FakeStrategy(BaseStrategy):
    name = "test_slippage"
    description = "Test slippage"
    params_schema = {}

    def on_bar(self, kline: dict, history: list[dict]):
        return None

    def on_start(self):
        pass

    def on_stop(self):
        pass


def _make_ctx() -> StrategyContext:
    return StrategyContext(
        symbol="BTC/USDT",
        timeframe="1h",
        mode="backtest",
        params={},
    )


def _make_kline(ts: str, close: float) -> dict:
    return {
        "timestamp": ts,
        "open": close - 10,
        "high": close + 20,
        "low": close - 20,
        "close": close,
        "volume": 100.0,
    }


def _make_engine(ohlcv: list[dict], capital: str = "1000000", **kwargs) -> BacktestEngine:
    return BacktestEngine(
        strategy=_FakeStrategy(_make_ctx()),
        ohlcv_data=ohlcv,
        initial_capital=Decimal(capital),
        symbol="BTC/USDT",
        timeframe="1h",
        **kwargs,
    )


def _buy_signal(price: float, qty: str = "0.1") -> OrderSignal:
    return OrderSignal(
        side="buy",
        quantity=Decimal(qty),
        order_type="market",
        price=Decimal(str(price)),
        signal_name="buy",
        exchange="binance",
        metadata={},
    )


def _sell_signal(price: float, qty: str = "0.1") -> OrderSignal:
    return OrderSignal(
        side="sell",
        quantity=Decimal(qty),
        order_type="market",
        price=Decimal(str(price)),
        signal_name="sell",
        exchange="binance",
        metadata={},
    )


@pytest.fixture()
def ohlcv() -> list[dict]:
    base = datetime(2024, 1, 1, 0, 0)
    return [
        _make_kline(ts=datetime.isoformat(base.replace(hour=i)), close=42000.0 + i * 100)
        for i in range(10)
    ]


class TestSlippageFillPrice:
    def test_buy_fills_above_signal_price(self, ohlcv):
        engine = _make_engine(ohlcv, slippage_rate=SLIP)
        kline = ohlcv[2]
        close = Decimal(str(kline["close"]))
        engine._process_signal(_buy_signal(float(close)), kline)

        expected_fill = close * (1 + SLIP)
        assert Decimal(str(engine._trades[0]["entry_price"])) == expected_fill
        assert engine._avg_entry_price == expected_fill

    def test_sell_fills_below_signal_price(self, ohlcv):
        engine = _make_engine(ohlcv, slippage_rate=SLIP)
        kline_buy = ohlcv[2]
        kline_sell = ohlcv[5]
        close_buy = Decimal(str(kline_buy["close"]))
        close_sell = Decimal(str(kline_sell["close"]))

        engine._process_signal(_buy_signal(float(close_buy)), kline_buy)
        engine._process_signal(_sell_signal(float(close_sell)), kline_sell)

        expected_exit = close_sell * (1 - SLIP)
        assert Decimal(str(engine._trades[1]["exit_price"])) == expected_exit

    def test_close_position_uses_slippage(self, ohlcv):
        engine = _make_engine(ohlcv, slippage_rate=SLIP)
        kline_buy = ohlcv[2]
        close_buy = Decimal(str(kline_buy["close"]))
        engine._process_signal(_buy_signal(float(close_buy)), kline_buy)

        last = ohlcv[-1]
        close_last = Decimal(str(last["close"]))
        engine._close_position(last, "end_of_backtest")

        expected_exit = close_last * (1 - SLIP)
        assert Decimal(str(engine._trades[1]["exit_price"])) == expected_exit

    def test_default_slippage_rate_constant(self):
        assert DEFAULT_SLIPPAGE_RATE == Decimal("0.0005")
        engine = _make_engine([])
        assert engine.slippage_rate == DEFAULT_SLIPPAGE_RATE

    def test_zero_slippage_preserves_old_behavior(self, ohlcv):
        """slippage_rate=0 must reproduce the pre-change fill price exactly."""
        engine = _make_engine(ohlcv, slippage_rate=Decimal("0"))
        kline = ohlcv[2]
        close = Decimal(str(kline["close"]))
        engine._process_signal(_buy_signal(float(close)), kline)
        assert Decimal(str(engine._trades[0]["entry_price"])) == close


class TestSlippageCashBoundary:
    def test_max_qty_accounts_for_slippage(self, ohlcv):
        """Insufficient cash: max_qty must fit price*(1+slip) AND commission."""
        engine = _make_engine(ohlcv, slippage_rate=SLIP)
        kline = ohlcv[2]
        close = Decimal(str(kline["close"]))
        fill = close * (1 + SLIP)

        # Ask for far more than cash allows
        big = _buy_signal(float(close), qty="1000")
        engine._process_signal(big, kline)

        # Verify total cost did not exceed cash
        assert engine._cash >= 0
        # And that the fill price used includes slippage
        assert Decimal(str(engine._trades[0]["entry_price"])) == fill
        # qty * fill * (1 + commission) <= cash, with commission 0.001
        cost = Decimal(str(engine._trades[0]["quantity"])) * fill * (1 + Decimal("0.001"))
        assert Decimal(str(cost)) <= Decimal("1000000")

    def test_exact_cash_buy_succeeds_with_costs(self, ohlcv):
        """Cash exactly equal to cost incl. slippage + commission buys fully."""
        kline = ohlcv[2]
        close = Decimal(str(kline["close"]))
        fill = close * (1 + SLIP)
        qty = Decimal("0.1")
        total = qty * fill * (1 + Decimal("0.001"))

        eng = BacktestEngine(
            strategy=_FakeStrategy(_make_ctx()),
            ohlcv_data=ohlcv,
            initial_capital=total,
            symbol="BTC/USDT",
            timeframe="1h",
            slippage_rate=SLIP,
        )
        eng._process_signal(_buy_signal(float(close)), kline)
        assert eng._position == qty
        assert eng._cash == Decimal("0")


class TestCostDetail:
    def test_cost_detail_accumulates(self, ohlcv):
        engine = _make_engine(ohlcv, slippage_rate=SLIP)
        k1, k2 = ohlcv[1], ohlcv[4]
        c1 = Decimal(str(k1["close"]))
        c2 = Decimal(str(k2["close"]))

        engine._process_signal(_buy_signal(float(c1)), k1)
        engine._process_signal(_sell_signal(float(c2)), k2)

        # 买入滑点 = qty * base * slip（cost 含 (1+slip) 因子，除回 (1+slip) 后抵消）
        buy_slip = Decimal("0.1") * c1 * SLIP
        sell_slip = Decimal("0.1") * (c2 * (1 - SLIP)) * SLIP / (1 - SLIP)
        expected = float(buy_slip + sell_slip)

        assert engine.cost_detail["slippage"] == pytest.approx(expected, rel=1e-9)
        assert engine.cost_detail["commission"] > 0
        assert engine.cost_detail.get("funding", 0.0) == 0.0

    def test_cost_detail_in_stats(self, ohlcv):
        engine = _make_engine(ohlcv, slippage_rate=SLIP)
        engine._process_signal(_buy_signal(float(ohlcv[1]["close"])), ohlcv[1])
        engine._close_position(ohlcv[-1], "end_of_backtest")

        stats = engine._compute_stats()
        assert "cost_detail" in stats
        assert stats["cost_detail"]["slippage"] > 0

    def test_zero_slippage_cost_detail_zero(self, ohlcv):
        engine = _make_engine(ohlcv, slippage_rate=Decimal("0"))
        engine._process_signal(_buy_signal(float(ohlcv[1]["close"])), ohlcv[1])
        engine._close_position(ohlcv[-1], "end_of_backtest")
        assert engine.cost_detail["slippage"] == 0.0
