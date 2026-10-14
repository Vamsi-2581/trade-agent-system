"""验证 min_breakout_tol=0 是否**逐字复现**旧策略的入场集合（真回退开关）。

旧策略：
    if close > upper and prev_close <= upper * (1 + tol):
新策略（k=0）：
    breakout_threshold = upper * (1 + tol * 0) = upper
    in_box_ceiling     = upper * (1 + tol)
    if close > breakout_threshold and prev_close <= in_box_ceiling:
⇒ 两行逐字等价。

用真实 SOL 4h 数据逐 bar 对比两者的入场 bar 集合（信号级，不跑引擎）。

用法（生产容器内；旧版策略需显式给路径——它已不在部署位）：
  sudo docker exec -w /app -e PYTHONPATH=/app \
      -e BOX_OLD=/root/.tradelogx/strategies/box_time_range_breakout_strategy.py.orig-20261013 \
      trade_agent_sys-backend-1 python /app/scripts/verify_rollback.py
"""

from __future__ import annotations

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

from apps.strategy_engine.base import StrategyContext  # noqa: E402

SYMBOL, TIMEFRAME = "SOL/USDT", "4h"
BASE = {"lookback": 240, "exit_at_mid": 0, "position_pct": 0.3, "max_width_pct": 0.04}


def load_cls(path, modname):
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod.BoxTimeRangeBreakoutStrategy


def fetch(limit=1000):
    ex = ccxt.binance({"enableRateLimit": True})
    return [
        {
            "timestamp": r[0],
            "open": r[1],
            "high": r[2],
            "low": r[3],
            "close": r[4],
            "volume": r[5],
        }
        for r in ex.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=limit)
    ]


def entry_bars(hist, params, cls):
    """逐 bar 走策略，返回产生 buy 信号的 bar 时间戳集合。"""
    ctx = StrategyContext(
        symbol=SYMBOL,
        timeframe=TIMEFRAME,
        mode="backtest",
        params=dict(params),
        balance=Decimal("5012.80"),
        position=Decimal("0"),
    )
    s = cls(ctx)
    out = []
    for i, k in enumerate(hist):
        sig = s.on_bar(k, hist[: i + 1])
        if sig is not None and getattr(sig, "side", None) == "buy":
            out.append(k["timestamp"])
    return out


def main():
    old_path = os.environ.get("BOX_OLD", "")
    if not old_path or not os.path.isfile(old_path):
        raise SystemExit(
            "需要修复前的策略文件：设 BOX_OLD=<路径>（部署机上可用 "
            "~/deploy_backup_*/box_time_range_breakout_strategy.py.orig）"
        )
    new_path = os.environ.get(
        "BOX_NEW",
        str(
            pathlib.Path(__file__).resolve().parents[1]
            / "strategies"
            / "box_time_range_breakout_strategy.py"
        ),
    )
    old = load_cls(old_path, "box_old_rb")
    new = load_cls(new_path, "box_new_rb")
    hist = fetch()

    def f(ms):
        return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime(
            "%m-%d %H:%M"
        )

    print(f"@@ bars={len(hist)}")

    e_old = entry_bars(hist, {**BASE, "volume_filter": False}, old)
    e_k0 = entry_bars(
        hist, {**BASE, "min_breakout_tol": 0.0, "volume_filter": False}, new
    )
    e_k1 = entry_bars(hist, {**BASE, "volume_filter": False}, new)

    print(f"@@ old        entries={len(e_old)}")
    print(f"@@ new(k=0)   entries={len(e_k0)}")
    print(f"@@ new(k=1.0) entries={len(e_k1)}")
    print(f"@@ 回退开关为真? old == new(k=0) -> {e_old == e_k0}")
    if e_old != e_k0:
        only_old = sorted(set(e_old) - set(e_k0))
        only_k0 = sorted(set(e_k0) - set(e_old))
        print(f"   仅旧有: {[f(t) for t in only_old]}")
        print(f"   仅k=0有: {[f(t) for t in only_k0]}")

    print("\n@@ 默认 k=1.0 相对旧行为拦下的入场")
    blocked = sorted(set(e_old) - set(e_k1))
    print(f"   拦下 {len(blocked)}/{len(e_old)}")
    for t in blocked:
        print(f"     {f(t)}")


if __name__ == "__main__":
    main()
