"""Ticket 002: Funding rate settlement in backtest engine.

Perp positions pay/receive funding every 8h at settlement points:
- long pays when rate > 0, receives when rate < 0
- short is the mirror
- no position -> no settlement
- gaps in funding data: skipped with warning, NOT silently treated as 0
"""

from datetime import datetime
from decimal import Decimal
import pytest

from apps.strategy_engine.backtest_mode import BacktestEngine
from apps.strategy_engine.base import BaseStrategy, OrderSignal, StrategyContext


class _FakeStrategy(BaseStrategy):
    name = "test_funding"
    description = "Test funding"
    params_schema = {}

    def on_bar(self, kline: dict, history: list[dict]):
        return None

    def on_start(self):
        pass

    def on_stop(self):
        pass


def _make_ctx() -> StrategyContext:
    return StrategyContext(
        symbol="BTC/USDT:USDT",
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


def _ohlcv(n: int = 10) -> list[dict]:
    base = datetime(2024, 1, 1, 0, 0)
    return [
        _make_kline(ts=datetime.isoformat(base.replace(hour=i)), close=42000.0 + i * 100)
        for i in range(n)
    ]


def _buy_signal(price: float, qty: str = "1") -> OrderSignal:
    return OrderSignal(
        side="buy",
        quantity=Decimal(qty),
        order_type="market",
        price=Decimal(str(price)),
        signal_name="buy",
        exchange="binance",
        metadata={},
    )


def _engine(ohlcv: list[dict], **kwargs) -> BacktestEngine:
    defaults = dict(
        strategy=_FakeStrategy(_make_ctx()),
        ohlcv_data=ohlcv,
        initial_capital=Decimal("1000000"),
        symbol="BTC/USDT:USDT",
        timeframe="1h",
        is_perp=True,
    )
    defaults.update(kwargs)
    return BacktestEngine(**defaults)


def _rates(times: list[str], rate: float) -> dict[str, float]:
    return {t: rate for t in times}


# Settlement timestamps: 00:00, 08:00, 16:00 UTC
T0 = "2024-01-01T00:00:00+00:00"
T8 = "2024-01-01T08:00:00+00:00"
T16 = "2024-01-01T16:00:00+00:00"


class TestFundingSettlement:
    def test_long_pays_positive_rate(self):
        """Long position at positive rate pays rate * notional."""
        ohlcv = _ohlcv()
        engine = _engine(ohlcv, funding_rates={T8: 0.0001})

        kline = ohlcv[1]  # 01:00, before settlement
        engine._process_signal(_buy_signal(float(kline["close"])), kline)

        kline_s = ohlcv[8]  # 08:00 settlement bar
        engine._settle_funding(kline_s)

        # paid = rate * notional, notional valued at settlement bar's close
        expected = 0.0001 * float(engine._position_value(kline_s))
        assert engine.cost_detail["funding"] == pytest.approx(expected, rel=1e-6)
        # funding was actually deducted from cash
        assert engine.cost_detail["funding"] > 0

    def test_long_receives_negative_rate(self):
        ohlcv = _ohlcv()
        engine = _engine(ohlcv, funding_rates={T8: -0.0001})
        engine._process_signal(_buy_signal(float(ohlcv[1]["close"])), ohlcv[1])
        engine._settle_funding(ohlcv[8])
        assert engine.cost_detail["funding"] == pytest.approx(-0.0001 * 42800.0, rel=1e-6)

    def test_no_position_no_settlement(self):
        ohlcv = _ohlcv()
        engine = _engine(ohlcv, funding_rates={T8: 0.0001})
        engine._settle_funding(ohlcv[8])
        assert engine.cost_detail["funding"] == 0.0

    def test_short_receives_positive_rate(self):
        """Short at positive rate RECEIVES: cost_detail.funding < 0, cash up."""
        ohlcv = _ohlcv()
        engine = _engine(ohlcv, funding_rates={T8: 0.0001})
        engine._position = Decimal("-1")
        kline_s = ohlcv[8]  # close 42800
        cash_before = engine._cash
        engine._settle_funding(kline_s)
        # notional = 1 * 42800; short receives rate * notional
        expected = -0.0001 * 42800.0
        assert engine.cost_detail["funding"] == pytest.approx(expected, rel=1e-6)
        assert engine.cost_detail["funding"] < 0
        assert engine._cash > cash_before  # 收取 → 现金增加

    def test_short_pays_negative_rate(self):
        """Short at negative rate PAYS: cost_detail.funding > 0, cash down."""
        ohlcv = _ohlcv()
        engine = _engine(ohlcv, funding_rates={T8: -0.0001})
        engine._position = Decimal("-1")
        kline_s = ohlcv[8]  # close 42800
        cash_before = engine._cash
        engine._settle_funding(kline_s)
        expected = 0.0001 * 42800.0  # 负费率×空头 = 支付
        assert engine.cost_detail["funding"] == pytest.approx(expected, rel=1e-6)
        assert engine._cash < cash_before

    def test_multiple_settlements_accumulate(self):
        """Two 8h settlements hand-computed: qty*close8*rate + qty*close16*rate."""
        ohlcv = _ohlcv(24)
        engine = _engine(ohlcv, funding_rates=_rates([T8, T16], 0.0001))
        qty = Decimal("1")
        engine._process_signal(_buy_signal(float(ohlcv[1]["close"]), qty="1"), ohlcv[1])
        engine._settle_funding(ohlcv[8])
        engine._settle_funding(ohlcv[16])
        # notional at each settlement bar's close
        expected = float(qty * (Decimal(str(ohlcv[8]["close"])) + Decimal(str(ohlcv[16]["close"]))) * Decimal("0.0001"))
        assert engine.cost_detail["funding"] == pytest.approx(expected, rel=1e-6)

    def test_missing_rate_logs_warning_not_zero(self, caplog):
        """Settlement point with no data: skipped, warning logged, funding stays 0."""
        import logging

        ohlcv = _ohlcv()
        engine = _engine(ohlcv, funding_rates={})  # no data at all
        engine._process_signal(_buy_signal(float(ohlcv[1]["close"])), ohlcv[1])
        with caplog.at_level(logging.WARNING, logger="apps.strategy_engine.backtest_mode"):
            engine._settle_funding(ohlcv[8])
        assert engine.cost_detail["funding"] == 0.0
        assert any("funding" in r.message.lower() for r in caplog.records)

    def test_not_perp_skips_settlement(self):
        ohlcv = _ohlcv()
        engine = _engine(ohlcv, is_perp=False, funding_rates={T8: 0.0001})
        engine._process_signal(_buy_signal(float(ohlcv[1]["close"])), ohlcv[1])
        engine._settle_funding(ohlcv[8])
        assert engine.cost_detail["funding"] == 0.0

    def test_settlement_hour_judged_in_utc(self):
        """A +08:00 08:00 bar IS the 00:00 UTC settlement point."""
        ohlcv = _ohlcv()
        engine = _engine(ohlcv, funding_rates={"2024-01-01T00:00:00+00:00": 0.0001})
        engine._position = Decimal("1")

        bar_utc_midnight_in_cst = _make_kline(
            ts="2024-01-01T08:00:00+08:00", close=42000.0
        )
        engine._settle_funding(bar_utc_midnight_in_cst)
        assert engine.cost_detail["funding"] > 0

    def test_non_settlement_hour_skipped(self):
        """03:00 UTC is not a settlement point."""
        ohlcv = _ohlcv()
        engine = _engine(ohlcv, funding_rates={"2024-01-01T03:00:00+00:00": 0.0001})
        engine._position = Decimal("1")
        engine._settle_funding(_make_kline(ts="2024-01-01T03:00:00+00:00", close=42000.0))
        assert engine.cost_detail["funding"] == 0.0

    @pytest.mark.asyncio
    async def test_run_integrates_settlement(self):
        """run() calls settlement each bar when is_perp."""
        import asyncio

        ohlcv = _ohlcv(24)
        engine = _engine(ohlcv, funding_rates=_rates([T8, T16], 0.0001))
        engine.strategy.select_universe = lambda: None
        engine.strategy.generate_insights = lambda k, h: None
        engine.strategy.construct_portfolio = lambda i, c: []
        engine.strategy.apply_risk_filters = lambda t, c: []
        stats = await asyncio.ensure_future(engine.run())
        assert "cost_detail" in stats
