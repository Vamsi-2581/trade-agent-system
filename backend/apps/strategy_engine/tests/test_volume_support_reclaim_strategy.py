"""
Regression: volume_support_reclaim_strategy must not blow up with
'StrategyContext' object has no attribute 'calculate_quantity' on entry.

Failure history: backtest 7bd70c1e-... failed in <8s on
`self.ctx.calculate_quantity(risk_pct=...)`. 被测的是部署位策略文件
`~/.tradelogx/strategies/volume_support_reclaim_strategy.py`，路径见
`_strategy_path()`。
"""

import asyncio
import importlib.util
import os
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from apps.strategy_engine.backtest_mode import BacktestEngine
from apps.strategy_engine.base import StrategyContext


STRATEGY_FILENAME = "volume_support_reclaim_strategy.py"


def _strategy_path() -> str:
    """定位被测策略文件。

    策略的**部署位**是 `~/.tradelogx/strategies/`（见 `apps/skill/loader.py`
    的 `TRADELOGX_ROOT`；容器里 HOME=/root，开发机上是用户家目录）。
    此前的常量写死了 `/root/...`，于是本地开发机上 `_load_strategy()` 静默跳过、
    而 `test_strategy_module_uses_no_missing_context_api` 直接
    `open()` 一个不存在的路径抛 FileNotFoundError。

    这里按与 `test_box_breakout_threshold.py::_strategy_path` 相同的顺序解析：
    显式环境变量覆盖 → 部署位（/root 与当前用户家目录）→ 仓库镜像
    `backend/strategies/`。
    """
    override = os.environ.get("VOLUME_RECLAIM_STRATEGY_PATH")
    if override:
        return override

    repo_mirror = Path(__file__).resolve().parents[3] / "strategies" / STRATEGY_FILENAME
    candidates = [
        Path("/root/.tradelogx/strategies") / STRATEGY_FILENAME,
        Path.home() / ".tradelogx" / "strategies" / STRATEGY_FILENAME,
        repo_mirror,
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return str(candidates[1])


STRATEGY_PATH = _strategy_path()


def _load_strategy():
    if not os.path.isfile(STRATEGY_PATH):
        pytest.skip(f"strategy file not present at {STRATEGY_PATH}")
    spec = importlib.util.spec_from_file_location(
        "volume_support_reclaim_strategy", STRATEGY_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod.VolumeSupportReclaimStrategy


def _ctx(balance: Decimal = Decimal("10000")) -> StrategyContext:
    return StrategyContext(
        symbol="BTC/USDT",
        timeframe="1d",
        mode="backtest",
        params={
            "support_period": 7,
            "volume_period": 20,
            "volume_multiplier": 3.0,
            "take_profit_pct": 5.0,
            "stop_loss_pct": 3.0,
            "risk_per_trade_pct": 10.0,
        },
        balance=balance,
    )


def _kline(ts: str, o: float, h: float, l: float, c: float, v: float) -> dict:  # noqa: E741
    return {"timestamp": ts, "open": o, "high": h, "low": l, "close": c, "volume": v}


def test_entry_does_not_call_calculate_quantity():
    """Real bug: ctx.calculate_quantity was called and AttributeError killed the backtest."""
    StrategyCls = _load_strategy()
    ctx = _ctx()
    s = StrategyCls(ctx)
    s.ctx.buy = lambda **kw: None  # type: ignore[assignment]
    s.ctx.close_position = lambda **kw: None  # type: ignore[assignment]

    history = [
        _kline(f"2024-01-{i + 1:02d}", 100, 105, 95, 102, 1000.0) for i in range(20)
    ]
    history.append(_kline("2024-01-21", 90, 92, 88, 90, 3000.0))
    current = _kline("2024-01-22", 95, 106, 94, 101, 1000.0)

    # Must not raise AttributeError.
    s.on_bar(current, history)


def test_strategy_module_uses_no_missing_context_api():
    """Source-level guard: no `self.ctx.calculate_quantity` reference anywhere."""
    with open(STRATEGY_PATH, encoding="utf-8") as f:
        src = f.read()
    assert "self.ctx.calculate_quantity" not in src, (
        "Strategy references missing StrategyContext.calculate_quantity — backtest will fail."
    )


def test_engine_runs_end_to_end_and_produces_ohlcv_like_equity_curve():
    """End-to-end: BacktestEngine on 120 bars with engineered spring must finish and record equity."""
    StrategyCls = _load_strategy()
    ctx = _ctx()
    strategy = StrategyCls(ctx)

    ohlcv = []
    # 30 stable bars around 100, vol 1000
    for i in range(30):
        ohlcv.append(_kline(f"2024-01-{i + 1:02d}", 100, 105, 95, 100, 1000.0))
    # 1 spike-down bar with 3x volume (breakout candidate)
    ohlcv.append(_kline("2024-02-01", 95, 96, 88, 89, 3000.0))
    # 1 reclaim bar (closes above 95, the support computed from window i-1..i-8)
    ohlcv.append(_kline("2024-02-02", 90, 105, 89, 102, 1000.0))
    # 50 more bars — let take-profit hit
    for i in range(50):
        c = 102 + (i % 5) * 0.2
        ohlcv.append(
            _kline(f"2024-02-{i + 3:02d}", c - 0.5, c + 1.0, c - 1.0, c, 1000.0)
        )
    # pad to 120
    while len(ohlcv) < 120:
        ohlcv.append(
            _kline(f"2024-04-{len(ohlcv) + 1:02d}", 110, 112, 108, 111, 1000.0)
        )

    engine = BacktestEngine(
        strategy=strategy,
        ohlcv_data=ohlcv,
        initial_capital=Decimal("10000"),
        symbol="BTC/USDT",
        timeframe="1d",
    )
    result = asyncio.run(engine.run())
    # Sanity: no exception; stats object returned; equity curve non-empty.
    assert isinstance(result, dict)
    assert len(engine._equity_curve) == len(ohlcv)
    assert any(eq["equity"] != 10000.0 for eq in engine._equity_curve), (
        "Equity never moved — strategy did not trade after the fix."
    )
