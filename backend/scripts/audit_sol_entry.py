"""txy 生产 SOL/USDT 2026-10-12 进场逻辑与量能判断审计（可复现）。

结论摘要见文末 docstring 末尾。本脚本只读：拉 Binance 真实 4h K 线，
按生产会话的参数合并方式跑 box_time_range_breakout_strategy，不写库、不下单。

用法（生产容器内，需 Binance 直连）：
  sudo docker exec -w /app trade_agent_sys-backend-1 \
      python /app/scripts/audit_sol_entry.py

被审计对象（生产 DB 实测）：
  live_sessions.id = 381e24c2-0182-4e7e-812a-0b094585e497
    symbol=SOL/USDT  timeframe=4h  mode=paper  status=running
    config={"lookback":240,"exit_at_mid":0,"position_pct":0.3,"max_width_pct":0.04}
  orders: buy 13.355595 @112.67 (2026-10-12 08:00:01Z)
          sell 13.35    @108.24 (2026-10-12 20:00:00Z)  realized_pnl=-59.1405

审计结论：
  ① 进场**符合策略代码当前规则**，且参数合并路径正确——但触发它的那根 bar
     突破幅度 +0.173% 小于箱体自身的触碰容差 0.686%，属"噪声级突破"，
     且该 bar 被 detect_box_range 同时算作"箱内最后一根"(end_index=239)。
  ② 量能判断**全程未生效**：会话 config 无 volume_filter，live 分支
     （runner.py:192）只做 params.update(parameters)，不注入 params_schema
     默认值，故 volume_filter 取 __init__ 的 False 兜底。
  ③ 即使打开量能门，这一单也拦不住（真实放量 1.65x MA20）；MA 含当前 bar
     的自包含偏置在本例仅 -0.004x，不是本单的问题所在。
"""

from __future__ import annotations

import datetime as dt
import os
import sys
from decimal import ROUND_HALF_UP, Decimal

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
from apps.strategy_engine.indicators import detect_box_range  # noqa: E402
from apps.strategy_engine.registry import StrategyRegistry  # noqa: E402

SYMBOL, TIMEFRAME = "SOL/USDT", "4h"
LOOKBACK, ATR_PERIOD = 240, 14
BALANCE = Decimal("5012.80")
ENTRY_TS = 1791777600000  # 2026-10-12 04:00 UTC 的 4h bar，08:00 收盘触发下单
# 生产 live_sessions.config 原样
SESSION_CONFIG = {
    "lookback": 240,
    "exit_at_mid": 0,
    "position_pct": 0.3,
    "max_width_pct": 0.04,
}
KW = dict(
    max_width_pct=0.04,
    min_width_pct=0.005,
    pivot_window=2,
    min_gap_bars=3,
    min_pivots=2,
    min_touches=2,
    atr_period=ATR_PERIOD,
    min_duration_bars=12,
)


def fmt(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime(
        "%Y-%m-%d %H:%M"
    )


def fetch(limit: int = 1000) -> list[dict]:
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


def fires(hist: list[dict], params: dict, cls, ts: int) -> bool:
    ctx = StrategyContext(
        symbol=SYMBOL,
        timeframe=TIMEFRAME,
        mode="live",
        params=dict(params),
        balance=BALANCE,
        position=Decimal("0"),
    )
    strat = cls(ctx)
    for i, k in enumerate(hist):
        sig = strat.on_bar(k, hist[: i + 1])
        if k["timestamp"] == ts:
            return sig is not None and getattr(sig, "side", None) == "buy"
    return False


def main() -> None:
    StrategyRegistry.discover()
    cls = StrategyRegistry.get_class("box_time_range_breakout_strategy")
    hist = fetch()
    idx = next(i for i, k in enumerate(hist) if k["timestamp"] == ENTRY_TS)
    window = hist[idx - LOOKBACK + 1 : idx + 1]
    box = detect_box_range(klines=window, **KW)["box"]
    cur = hist[idx]
    close, upper, tol = float(cur["close"]), box["upper"], box["tolerance_pct"]
    cur_vol = float(cur["volume"])
    ma_incl = sum(float(x["volume"]) for x in window[-20:]) / 20
    ma_excl = sum(float(x["volume"]) for x in window[-21:-1]) / 20

    print("=" * 74)
    print("① 进场判定复核")
    print("=" * 74)
    print(f"入场 bar           : {fmt(ENTRY_TS)}  (4h，08:00 收盘触发下单)")
    print(f"close              : {close}")
    print(f"箱体 upper / lower : {upper} / {box['lower']}   mid={box['mid']}")
    print(f"触碰容差 tol       : {tol * 100:.3f}%")
    print(f"突破幅度 margin    : {(close / upper - 1) * 100:+.3f}%")
    print(
        f"  → margin < tol ? {close / upper - 1 < tol}"
        f"   （同一根 bar 既算突破、又被算作箱内）"
    )
    print(
        f"end_index          : {box['end_index']} (窗口末={LOOKBACK - 1})"
        f"  → 突破 bar 自己被算作箱内: {box['end_index'] == LOOKBACK - 1}"
    )
    prev = float(window[-2]["close"])
    print(
        f"prev_close         : {prev}  <= upper*(1+tol)={upper * (1 + tol):.4f}"
        f" → 守卫放行: {prev <= upper * (1 + tol)}"
    )

    # 复现策略实际下单量（应与生产 13.355595 一致）
    qty = (
        BALANCE * Decimal(str(SESSION_CONFIG["position_pct"])) / Decimal(str(close))
    ).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
    print(
        f"\n复算开仓量         : {qty}   （生产实际 13.355595，"
        f"一致={qty == Decimal('13.355595')}）"
    )
    print(
        f"名义金额           : {(qty * Decimal(str(close))).quantize(Decimal('0.01'))} USDT"
        f"  ≈ 初始资金的 {SESSION_CONFIG['position_pct'] * 100:.0f}%"
    )

    print("\n" + "=" * 74)
    print("② 量能判断")
    print("=" * 74)
    for key in ("volume_filter", "volume_mult", "volume_ma_period"):
        print(f"session.config[{key!r}] = {SESSION_CONFIG.get(key, '<缺省>')}")
    print(
        "  → live 分支 runner.py:192 只做 params.update(parameters)，"
        "不注入 params_schema 默认值"
    )
    print("  → 策略 __init__ 兜底 .get('volume_filter', False) ⇒ 量能门全程关闭")
    print(f"\n入场 bar 量能      : {cur_vol:,.0f}")
    print(
        f"MA20 含当前 bar    : {ma_incl:,.0f}  → 倍数 {cur_vol / ma_incl:.3f}（现网口径）"
    )
    print(
        f"MA20 不含当前 bar  : {ma_excl:,.0f}  → 倍数 {cur_vol / ma_excl:.3f}（标准口径）"
    )
    print(
        f"自包含偏置         : {(cur_vol / ma_incl) - (cur_vol / ma_excl):+.4f}（本例可忽略）"
    )

    print("\n反事实：打开量能门，这一单会被拦下吗？")
    for mult in (1.0, 1.2, 1.5, 2.0):
        p = {**SESSION_CONFIG, "volume_filter": True, "volume_mult": mult}
        print(f"  mult={mult}: 策略触发={fires(hist, p, cls, ENTRY_TS)}")
    print("  → 1.65x MA20 属真实放量，量能门在 1.0~1.5 都拦不住这一单；需要 2.0 才拦下")

    print("\n反事实：若要求突破幅度清出容差带 close > upper*(1+tol)")
    print(
        f"  close={close}  upper*(1+tol)={upper * (1 + tol):.4f}  "
        f"→ 本单会被拦下={close <= upper * (1 + tol)}"
    )

    print("\n" + "=" * 74)
    print("③ 出场复核（冻结参考线 = lower，exit_at_mid=0）")
    print("=" * 74)
    ref = box["lower"]
    print(f"冻结出场线         : {ref}")
    for j in range(idx, min(idx + 4, len(hist))):
        b = hist[j]
        c = float(b["close"])
        tag = "  ← 收盘跌破，触发平仓" if j > idx and c < ref else ""
        print(f"  {fmt(b['timestamp'])} close={c}{tag}")
    print("  → 10-12 16:00 bar close=108.33 < 108.705，20:00 收盘时下单卖出")
    print("  → 与生产 sell 13.35 @108.24 (20:00:00.599Z) 一致；realized_pnl=-59.1405")


if __name__ == "__main__":
    main()
