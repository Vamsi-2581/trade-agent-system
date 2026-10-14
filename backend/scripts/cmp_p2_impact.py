"""P2/P3 影响量化：新策略 vs 旧策略，SOL/USDT 4h。

对比 4 种配置在近 1000 根 / 4 年上的表现，确认修复方向正确且无意外：
  old            : 旧策略文件（close>upper 即突破，守卫 upper*(1+tol)）
  new_default    : 新策略，min_breakout_tol=1.0（必须清出容差带）
  new_tol0       : 新策略，min_breakout_tol=0（应等价于旧行为 → 回归对照）
  new_tol1_vol12 : 新策略 + 量能门 1.2

用法（生产容器内；旧版策略需显式给路径——它已不在部署位）：
  sudo docker exec -w /app -e PYTHONPATH=/app \
      -e BOX_OLD=/root/.tradelogx/strategies/box_time_range_breakout_strategy.py.orig-20261013 \
      trade_agent_sys-backend-1 python /app/scripts/cmp_p2_impact.py
"""

from __future__ import annotations

import asyncio
import datetime as dt
import importlib.util
import os
import pathlib
import sys
from decimal import Decimal

# 让脚本能 import apps.* —— 走与 manage.py 相同的路径（容器里 __file__ 在
# /app/scripts/ 下，同一式子也成立；/app 兜底用于被拷到 /tmp 执行的场景）
_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _root in (_BACKEND_ROOT, "/app"):
    if os.path.isdir(os.path.join(_root, "apps")) and _root not in sys.path:
        sys.path.insert(0, _root)
        break
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings.dev")

import django  # noqa: E402

django.setup()

import ccxt  # noqa: E402

from apps.strategy_engine.backtest_mode import BacktestEngine  # noqa: E402
from apps.strategy_engine.base import StrategyContext  # noqa: E402

SYMBOL, TIMEFRAME = "SOL/USDT", "4h"
INITIAL = Decimal("5012.80")
BASE = {"lookback": 240, "exit_at_mid": 0, "position_pct": 0.3, "max_width_pct": 0.04}
# 旧版（修复前）策略：不再位于部署位，必须显式提供；新版即仓库内那份镜像
OLD_PATH = os.environ.get("BOX_OLD", "")
NEW_PATH = os.environ.get(
    "BOX_NEW",
    str(
        pathlib.Path(__file__).resolve().parents[1]
        / "strategies"
        / "box_time_range_breakout_strategy.py"
    ),
)


def load_cls(path, modname):
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod.BoxTimeRangeBreakoutStrategy


def fetch_paged(years):
    ex = ccxt.binance({"enableRateLimit": True})
    now = ex.milliseconds()
    since = now - int(years * 365.25 * 24 * 3600 * 1000)
    out = []
    while since < now:
        b = ex.fetch_ohlcv(SYMBOL, TIMEFRAME, since=since, limit=1000)
        if not b:
            break
        out.extend(b)
        since = b[-1][0] + 1
        if len(b) < 2:
            break
    seen, uniq = set(), []
    for r in out:
        if r[0] not in seen:
            seen.add(r[0])
            uniq.append(r)
    return [
        {
            "timestamp": r[0],
            "open": r[1],
            "high": r[2],
            "low": r[3],
            "close": r[4],
            "volume": r[5],
        }
        for r in uniq
    ]


async def run(hist, params, cls):
    ctx = StrategyContext(
        symbol=SYMBOL,
        timeframe=TIMEFRAME,
        mode="backtest",
        params=dict(params),
        balance=INITIAL,
        position=Decimal("0"),
    )
    eng = BacktestEngine(
        strategy=cls(ctx),
        ohlcv_data=hist,
        initial_capital=INITIAL,
        symbol=SYMBOL,
        timeframe=TIMEFRAME,
    )
    stats = await eng.run()
    return stats, eng


def count_entries(hist, params, cls):
    ctx = StrategyContext(
        symbol=SYMBOL,
        timeframe=TIMEFRAME,
        mode="backtest",
        params=dict(params),
        balance=INITIAL,
        position=Decimal("0"),
    )
    s = cls(ctx)
    n = 0
    for i, k in enumerate(hist):
        sig = s.on_bar(k, hist[: i + 1])
        if sig is not None and getattr(sig, "side", None) == "buy":
            n += 1
    return n


def main():
    if not OLD_PATH or not os.path.isfile(OLD_PATH):
        raise SystemExit(
            "需要修复前的策略文件：设 BOX_OLD=<路径>（部署机上可用 "
            "~/deploy_backup_*/box_time_range_breakout_strategy.py.orig）"
        )
    old = load_cls(OLD_PATH, "box_old_mod")
    new = load_cls(NEW_PATH, "box_new_mod")

    hist = fetch_paged(1)  # 近 1 年足够看清方向；4 年在下方单独跑引擎

    def f(ms):
        return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime(
            "%Y-%m-%d"
        )

    print(
        f"@@ RANGE {len(hist)} bars {f(hist[0]['timestamp'])} .. {f(hist[-1]['timestamp'])}"
    )

    cases = [
        ("old(1y)", old, {**BASE, "volume_filter": False}),
        ("new_default(1y)", new, {**BASE, "volume_filter": False}),
        (
            "new_tol0(1y)",
            new,
            {**BASE, "min_breakout_tol": 0.0, "volume_filter": False},
        ),
        (
            "new_tol1_vol12(1y)",
            new,
            {**BASE, "volume_filter": True, "volume_mult": 1.2},
        ),
    ]
    for tag, cls, p in cases:
        stats, eng = asyncio.run(run(hist, p, cls))
        n_entry = count_entries(hist, p, cls)
        print(
            f"@@RES {tag:20s} entries={n_entry:>3} "
            f"trades={stats.get('total_trades'):>3} "
            f"ret%={float(stats.get('total_return_pct') or 0):>9.3f} "
            f"win%={float(stats.get('win_rate') or 0):>7.3f} "
            f"dd%={float(stats.get('max_drawdown_pct') or 0):>8.3f}"
        )

    # 回归对照：new_tol0 应与 old 完全一致
    print("\n@@ 回归对照（new_tol0 应复现 old 的入场集合）")
    hist1k = hist[-800:]
    e_old = count_entries(hist1k, {**BASE, "volume_filter": False}, old)
    e_t0 = count_entries(
        hist1k, {**BASE, "min_breakout_tol": 0.0, "volume_filter": False}, new
    )
    e_new = count_entries(hist1k, {**BASE, "volume_filter": False}, new)
    print(f"@@ 近1000根 入场数: old={e_old}  new_tol0={e_t0}  new_default={e_new}")
    print(f"@@ new_tol0 == old ? {e_t0 == e_old}   (True=旧行为可完全回退)")


if __name__ == "__main__":
    main()
