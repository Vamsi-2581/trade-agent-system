#!/usr/bin/env python3
"""4h 箱体突破·回踩进场 策略回测（独立运行，不依赖 Django）。

策略规则（机械化解读）：
1. 箱体 = 过去 7 天（42 根 4h K线）的最高价 box_high / 最低价 box_low（Donchian 语义）。
2. 突破：收盘价 > box_high 且 成交量 >= 3 × 前 20 根 4h 均量。
3. 进场：突破后 18 根 4h K线内，价格回踩触及 box_high（限价成交于上边价；
   若开盘已低于上边价则以开盘价成交）。
4. 止损 = box_low；止盈 = 进场价 + 2 × (进场价 - 止损)，盈亏比 1:2。

执行假设：
- 单边做多（现货式模拟），同一时间最多一个挂单/持仓。
- 持仓期间逐根检查：同根同时触及止损与止盈按止损先触发（保守口径）。
- 手续费双边 0.1%；每笔风险 = 当时权益的 1%（风险平价仓位）。
- 数据源：Binance 公开 K 线（data-api.binance.vision 优先，api.binance.com 回退）。

用法：
    python3 backend/scripts/backtest_box_breakout_retest.py            # 默认 BTC/ETH/BNB/SOL
    python3 backend/scripts/backtest_box_breakout_retest.py BTCUSDT    # 指定交易对
    python3 backend/scripts/backtest_box_breakout_retest.py --sweep    # 额外跑参数敏感性
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import statistics
import time
import urllib.request
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------- 参数
BOX_BARS = 42  # 7 日 = 7 * 6 根 4h
VOL_PERIOD = 20  # 20 根均量
VOL_MULT = 3.0  # 放量倍数
RETEST_BARS = 18  # 突破后回踩进场窗口（根）
RR = 2.0  # 盈亏比
FEE = 0.001  # 单边手续费
RISK_PER_TRADE = 0.01  # 每笔风险占权益比例
INITIAL_EQUITY = 10000.0


# ---------------------------------------------------------------- 数据
def _http_get_json(url: str, timeout: int = 30, proxy: str | None = None) -> list:
    if proxy:
        handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        opener = urllib.request.build_opener(handler)
    else:
        opener = urllib.request.build_opener()
    req = urllib.request.Request(url, headers={"User-Agent": "box-backtest/1.0"})
    with opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def fetch_klines(
    symbol: str, interval: str = "4h", since: datetime | None = None
) -> list[dict]:
    """分页拉取全部 4h K 线，返回按时间升序的 dict 列表。"""
    since_ms = int(since.timestamp() * 1000) if since else None
    sources = [
        ("https://data-api.binance.vision/api/v3/klines", None),
        ("https://api.binance.com/api/v3/klines", "http://127.0.0.1:7890"),
        ("https://api.binance.com/api/v3/klines", None),
    ]
    errors: list[str] = []
    all_rows: list[list] = []
    for base, proxy in sources:
        rows_chunk: list[list] = []
        cursor = since_ms
        try:
            while True:
                url = f"{base}?symbol={symbol}&interval={interval}&limit=1000"
                if cursor:
                    url += f"&startTime={cursor}"
                rows = None
                for attempt in range(4):  # 重试（瞬时网络错误/限流）
                    try:
                        rows = _http_get_json(url, proxy=proxy)
                        break
                    except Exception as exc:  # noqa: BLE001
                        time.sleep(1.5 * (attempt + 1))
                        rows = None
                        errors.append(f"{base}: {exc}")
                if rows is None:
                    raise RuntimeError(
                        f"重试 4 次仍失败：{errors[-1] if errors else '未知错误'}"
                    )
                rows_chunk.extend(rows)
                cursor = rows[-1][0] + 1
                if len(rows) < 1000:
                    break
                time.sleep(0.2)
            all_rows = rows_chunk
            break
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{base}(proxy={proxy}): {exc}")
            continue
    if not all_rows:
        raise RuntimeError(f"无法获取 {symbol} K线数据：{' | '.join(errors[-3:])}")

    # 去重（分页边界可能重叠），按时间升序
    dedup: dict[int, list] = {}
    for r in all_rows:
        dedup[r[0]] = r
    rows = [dedup[k] for k in sorted(dedup)]

    return [
        {
            "ts": r[0],
            "open": float(r[1]),
            "high": float(r[2]),
            "low": float(r[3]),
            "close": float(r[4]),
            "volume": float(r[5]),  # 基础资产成交量
            "quote_volume": float(r[7]),  # 计价资产成交额
        }
        for r in rows
    ]


# ---------------------------------------------------------------- 回测引擎
def run_backtest(
    klines: list[dict],
    box_bars: int = BOX_BARS,
    vol_period: int = VOL_PERIOD,
    vol_mult: float = VOL_MULT,
    retest_bars: int = RETEST_BARS,
    rr: float = RR,
    fee: float = FEE,
    risk_per_trade: float = RISK_PER_TRADE,
    initial_equity: float = INITIAL_EQUITY,
    quote_volume: bool = False,
) -> dict:
    """执行回测，返回统计与逐笔交易记录。"""
    n = len(klines)
    highs = [k["high"] for k in klines]
    lows = [k["low"] for k in klines]
    closes = [k["close"] for k in klines]
    opens = [k["open"] for k in klines]
    vols = [k["quote_volume"] if quote_volume else k["volume"] for k in klines]

    # 前置缓存：箱体上下边、20 根均量
    box_highs = [None] * n
    box_lows = [None] * n
    vol_mas = [None] * n
    for i in range(box_bars, n):
        window_h = highs[i - box_bars : i]
        window_l = lows[i - box_bars : i]
        box_highs[i] = max(window_h)
        box_lows[i] = min(window_l)
        if i >= box_bars + vol_period:
            vol_mas[i] = sum(vols[i - vol_period : i]) / vol_period

    equity = initial_equity
    equity_curve: list[float] = []
    trades: list[dict] = []
    pending: dict | None = None
    pos: dict | None = None

    def close_pos(exit_price: float, exit_ts: int, reason: str) -> None:
        nonlocal equity, pos
        p = pos
        qty = p["qty"]
        gross = (exit_price - p["entry"]) * qty
        cost = (p["entry"] + exit_price) * qty * fee
        equity += gross - cost
        risk = p["entry"] - p["stop"]
        r_multiple = (exit_price - p["entry"]) / risk if risk > 0 else 0.0
        trades.append(
            {
                "entry_ts": p["entry_ts"],
                "exit_ts": exit_ts,
                "entry": round(p["entry"], 8),
                "stop": round(p["stop"], 8),
                "target": round(p["target"], 8),
                "exit": round(exit_price, 8),
                "r": round(r_multiple, 4),
                "pnl_pct": round((exit_price / p["entry"] - 1) * 100, 4),
                "pnl_abs": round(gross - cost, 2),
                "bars_held": p["bars"],
                "reason": reason,
            }
        )
        pos = None

    for i in range(box_bars, n):
        bar = klines[i]
        hi, lo = highs[i], lows[i]

        if pos is not None:
            pos["bars"] += 1
            if lo <= pos["stop"] and hi >= pos["target"]:
                close_pos(pos["stop"], bar["ts"], "stop_first")  # 同根双触：保守按止损
            elif lo <= pos["stop"]:
                close_pos(pos["stop"], bar["ts"], "stop")
            elif hi >= pos["target"]:
                close_pos(pos["target"], bar["ts"], "target")
            equity_curve.append(equity)
            continue

        # 挂单窗口：突破后 18 根内回踩箱体上边 → 进场
        if pending is not None:
            if i <= pending["deadline"]:
                if lo <= pending["box_high"]:
                    entry = (
                        pending["box_high"]
                        if opens[i] >= pending["box_high"]
                        else opens[i]
                    )
                    stop = pending["box_low"]
                    target = entry + rr * (entry - stop)
                    if entry - stop > 0:
                        risk_amount = equity * risk_per_trade
                        qty = risk_amount / (entry - stop)
                        pos = {
                            "entry": entry,
                            "stop": stop,
                            "target": target,
                            "entry_ts": bar["ts"],
                            "entry_idx": i,
                            "qty": qty,
                            "bars": 0,
                        }
                        pending = None
                        # 进场同一根立刻检查止损/止盈（保守同根双触按止损）
                        if lo <= stop and hi >= target:
                            close_pos(stop, bar["ts"], "stop_first")
                        elif lo <= stop:
                            close_pos(stop, bar["ts"], "stop")
                        elif hi >= target:
                            close_pos(target, bar["ts"], "target")
                pending = None if pos is not None else pending
                if pending is not None and i > pending["deadline"]:
                    pending = None
            else:
                pending = None
            equity_curve.append(equity)
            continue

        # 突破信号（要求已有均量）
        if vol_mas[i] is None:
            equity_curve.append(equity)
            continue
        if closes[i] > box_highs[i] and vols[i] >= vol_mult * vol_mas[i]:
            pending = {
                "box_high": box_highs[i],
                "box_low": box_lows[i],
                "deadline": i + retest_bars,
            }
        equity_curve.append(equity)

    # 数据结束：强制平仓
    if pos is not None:
        close_pos(closes[-1], klines[-1]["ts"], "end_of_data")

    return {
        "trades": trades,
        "equity_curve": equity_curve,
        "final_equity": equity,
    }


# ---------------------------------------------------------------- 统计
def summarize(result: dict, initial_equity: float = INITIAL_EQUITY) -> dict:
    trades = result["trades"]
    equity_curve = result["equity_curve"]
    if not trades:
        return {
            "n_trades": 0,
            "win_rate": 0.0,
            "avg_r": 0.0,
            "median_r": 0.0,
            "profit_factor": 0.0,
            "expectancy_r": 0.0,
            "max_cons_loss": 0,
            "total_return_pct": 0.0,
            "max_drawdown_pct": 0.0,
            "avg_bars": 0.0,
            "reasons": {},
            "by_year": [],
        }
    rs = [t["r"] for t in trades]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    max_cons_loss = cur = 0
    for r in rs:
        cur = cur + 1 if r <= 0 else 0
        max_cons_loss = max(max_cons_loss, cur)

    peak = -math.inf
    max_dd = 0.0
    for eq in equity_curve:
        peak = max(peak, eq)
        if peak > 0:
            max_dd = min(max_dd, (eq - peak) / peak)

    reasons: dict[str, int] = {}
    for t in trades:
        reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1

    years: dict[int, list[float]] = {}
    for t in trades:
        y = datetime.fromtimestamp(t["entry_ts"] / 1000, tz=timezone.utc).year
        years.setdefault(y, []).append(t["r"])

    return {
        "n_trades": len(trades),
        "win_rate": round(len(wins) / len(trades) * 100, 2),
        "avg_r": round(statistics.mean(rs), 4),
        "median_r": round(statistics.median(rs), 4),
        "profit_factor": round(gross_win / gross_loss, 3)
        if gross_loss > 0
        else math.inf,
        "expectancy_r": round(statistics.mean(rs), 4),
        "max_cons_loss": max_cons_loss,
        "total_return_pct": round(
            (result["final_equity"] / initial_equity - 1) * 100, 2
        ),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "avg_bars": round(statistics.mean([t["bars_held"] for t in trades]), 1),
        "reasons": reasons,
        "by_year": [
            {
                "year": y,
                "n": len(rs2),
                "win_rate": round(sum(1 for r in rs2 if r > 0) / len(rs2) * 100, 1),
                "sum_r": round(sum(rs2), 2),
            }
            for y, rs2 in sorted(years.items())
        ],
    }


def load_data(
    symbol: str, since_days: int | None = None, cache_dir: str | None = None
) -> list[dict]:
    if cache_dir:
        cache_path = os.path.join(cache_dir, f"{symbol}_4h.json")
        if os.path.exists(cache_path):
            with open(cache_path) as f:
                return json.load(f)
    since = (
        datetime(2018, 1, 1, tzinfo=timezone.utc)
        if since_days is None
        else (datetime.now(timezone.utc) - timedelta(days=since_days))
    )
    klines = fetch_klines(symbol, since=since)
    if cache_dir:
        os.makedirs(cache_dir, exist_ok=True)
        with open(cache_path, "w") as f:
            json.dump(klines, f)
    return klines


# ---------------------------------------------------------------- 主流程
def main() -> None:
    parser = argparse.ArgumentParser(description="4h 箱体突破·回踩进场回测")
    parser.add_argument(
        "symbols", nargs="*", default=["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"]
    )
    parser.add_argument("--sweep", action="store_true", help="参数敏感性扫描")
    parser.add_argument(
        "--since-days", type=int, default=None, help="仅取最近 N 天数据"
    )
    args = parser.parse_args()

    out_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "backtest_results",
        "box_breakout_retest",
    )
    cache_dir = os.path.join(out_dir, "cache")
    os.makedirs(out_dir, exist_ok=True)

    base_params = {
        "box_bars": BOX_BARS,
        "vol_period": VOL_PERIOD,
        "vol_mult": VOL_MULT,
        "retest_bars": RETEST_BARS,
        "rr": RR,
        "fee": FEE,
        "risk_per_trade": RISK_PER_TRADE,
    }
    print(
        f"策略参数: 箱体={BOX_BARS}根(7日) 均量={VOL_PERIOD}根 放量={VOL_MULT}x "
        f"回踩窗口={RETEST_BARS}根 盈亏比=1:{RR} 手续费={FEE * 100}%/边 单笔风险={RISK_PER_TRADE * 100}%"
    )
    print(f"初始资金: ${INITIAL_EQUITY:,.0f} | 数据源: Binance 4h K线\n")

    all_stats: dict[str, dict] = {}
    for symbol in args.symbols:
        klines = load_data(symbol, since_days=args.since_days, cache_dir=cache_dir)
        span_days = (klines[-1]["ts"] - klines[0]["ts"]) / 86400000
        result = run_backtest(klines, **base_params)
        stats = summarize(result)
        all_stats[symbol] = {
            "stats": stats,
            "span_days": span_days,
            "bars": len(klines),
        }
        _print_symbol(symbol, stats, span_days, len(klines))

        trades_path = os.path.join(out_dir, f"trades_{symbol}.csv")
        with open(trades_path, "w", newline="") as f:
            w = csv.DictWriter(
                f,
                fieldnames=[
                    "entry_ts",
                    "exit_ts",
                    "entry",
                    "stop",
                    "target",
                    "exit",
                    "r",
                    "pnl_pct",
                    "pnl_abs",
                    "bars_held",
                    "reason",
                ],
            )
            w.writeheader()
            for t in result["trades"]:
                w.writerow(t)
        print(f"  逐笔交易已保存: {trades_path}\n")

    # 汇总 JSON
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "params": base_params,
        "initial_equity": INITIAL_EQUITY,
        "symbols": {
            s: {
                **all_stats[s]["stats"],
                "span_days": round(all_stats[s]["span_days"], 0),
                "bars": all_stats[s]["bars"],
            }
            for s in all_stats
        },
    }
    with open(os.path.join(out_dir, "summary.json"), "w") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"汇总已保存: {os.path.join(out_dir, 'summary.json')}")

    if args.sweep:
        print("\n" + "=" * 100)
        print("参数敏感性扫描（默认 BTCUSDT，列 = 总收益率% / 胜率% / 交易数 / 平均R）")
        print("=" * 100)
        symbol = "BTCUSDT"
        klines = load_data(symbol, since_days=args.since_days, cache_dir=cache_dir)
        sweeps = [
            (
                "box_bars",
                "箱体窗口",
                [BOX_BARS // 2, BOX_BARS, BOX_BARS * 2],
                {"BOX_BARS//2": "3日", "BOX_BARS": "7日", "BOX_BARS*2": "14日"},
            ),
            ("vol_mult", "放量倍数", [2.0, 3.0, 4.0], None),
            ("retest_bars", "回踩窗口", [6, 12, 18, 24], None),
        ]
        for key, label, values, _labels in sweeps:
            print(f"\n[{label}]")
            for v in values:
                p = dict(base_params)
                p[key] = v
                r = run_backtest(klines, **p)
                s = summarize(r)
                print(
                    f"  {key}={v:<4} → 收益率 {s['total_return_pct']:+8.2f}% | 胜率 {s['win_rate']:5.1f}% | "
                    f"交易 {s['n_trades']:4d} | 平均R {s['avg_r']:+.3f} | 最大回撤 {s['max_drawdown_pct']:.1f}%"
                )


def _print_symbol(symbol: str, s: dict, span_days: float, bars: int) -> None:
    print(f"═══ {symbol} ═══ (数据 {span_days / 365:.1f} 年 / {bars} 根 4h K线)")
    print(
        f"  总收益率: {s['total_return_pct']:+.2f}%    最大回撤: {s['max_drawdown_pct']:.2f}%"
    )
    print(
        f"  交易数: {s['n_trades']}    胜率: {s['win_rate']:.1f}%    平均盈亏比(R): {s['avg_r']:+.3f}"
    )
    print(f"  期望值: {s['expectancy_r']:+.4f}R/笔    盈利因子: {s['profit_factor']}")
    print(f"  最大连亏: {s['max_cons_loss']} 笔    平均持仓: {s['avg_bars']} 根")
    print(f"  平仓原因: {s['reasons']}")
    if s["by_year"]:
        print(
            "  分年度: "
            + " | ".join(
                f"{y['year']}:{y['n']}笔/{y['win_rate']}%/{y['sum_r']:+.1f}R"
                for y in s["by_year"]
            )
        )


if __name__ == "__main__":
    main()
