"""Tests for box time-extent (left/right) fields in detect_box_range.

语义（与 indicators.py 中 _box_time_stats 一致）：
- start_index = 第一根 close 落在 [lower×(1-tol), upper×(1+tol)] 内的 K 线
- end_index   = 最后一根 close 仍在箱内的 K 线
- duration_bars = start → end 的跨度（中间离箱再回来算一整段）
- started_in_box = 窗口开头就已处于箱体（真实进入点早于窗口）
"""

from __future__ import annotations

import pytest

from apps.agent.tools.box_range import DetectBoxRangeTool
from apps.strategy_engine.indicators import _box_time_stats, detect_box_range


def _klines(
    closes: list[float],
    timestamps: list | None = None,
) -> list[dict]:
    """构造箱体 K 线：high/low 形态与被识别为箱体的典型模式一致（箱体约 [100, 110]），
    close 由调用方指定以控制"在箱/离箱"时间点。"""
    n = len(closes)
    highs, lows = [], []
    for i in range(n):
        cycle = i % 5
        if cycle < 3:  # 上半部
            highs.append(110)
            lows.append(105)
        elif cycle == 3:  # 跌出
            highs.append(107)
            lows.append(103)
        else:  # 贴下
            highs.append(106)
            lows.append(100)
    return [
        {
            **({"timestamp": timestamps[i]} if timestamps is not None else {}),
            "open": (high + low) / 2,
            "high": high,
            "low": low,
            "close": closes[i],
            "volume": 1000,
        }
        for i, (high, low) in enumerate(zip(highs, lows))
    ]


def _run(closes, timestamps: list | None = None, **kwargs):
    result = _detect(closes, timestamps, **kwargs)
    assert result["is_ranging"] is True, result.get("reason")
    return result["box"]


def _detect(closes, timestamps: list | None = None, **kwargs):
    """返回完整结果（不假设一定是箱体），用于校验时长门槛与失败原因。"""
    return detect_box_range(
        klines=_klines(closes, timestamps),
        max_width_pct=0.15,
        pivot_window=2,
        atr_period=3,
        **kwargs,
    )


# 离箱边界：close <= 98 或 >= 112（无论 tolerance_pct 取 [0.001, 0.01] 中何值，
# 在箱区间均包含 99..111.1，绝不包含 98/112）
_OUT_HIGH = 112.0
_OUT_LOW = 98.0
_IN = 106.0


def test_entry_exit_reentry_span():
    """已知进出点：前 3 根在箱外 → 13 根在箱内 → 3 根离箱 → 6 根回箱 → 尾部离箱。

    结束取最后一根在箱内的 K 线，时长算整段跨度（含中间离箱间隙）。
    """
    closes = (
        [_OUT_HIGH] * 3
        + [_IN] * 13  # index 3..15
        + [_OUT_LOW] * 3  # index 16..18
        + [_IN] * 6  # index 19..24
        + [_OUT_HIGH] * 5  # index 25..29
    )
    timestamps = [f"2026-01-01T00:{i:02d}:00Z" for i in range(len(closes))]
    box = _run(closes, timestamps=timestamps)

    assert box["start_index"] == 3
    assert box["end_index"] == 24
    assert box["started_in_box"] is False
    assert box["duration_bars"] == 22  # 3 → 24 共 22 根，含 16..18 的离箱间隙
    assert box["bars_in_box"] == 19  # 13 + 6
    assert box["start_timestamp"] == "2026-01-01T00:03:00Z"
    assert box["end_timestamp"] == "2026-01-01T00:24:00Z"
    assert box["duration_seconds"] == 21 * 60  # 21 根 × 1 分钟


def test_started_inside_box():
    """窗口开头就在箱内：start=0 且 started_in_box=True，Box 覆盖整个窗口。"""
    closes = [_IN] * 30
    timestamps = [f"2026-01-01T00:{i:02d}:00Z" for i in range(len(closes))]
    box = _run(closes, timestamps=timestamps)

    assert box["start_index"] == 0
    assert box["end_index"] == 29
    assert box["started_in_box"] is True
    assert box["duration_bars"] == 30
    assert box["bars_in_box"] == 30
    assert box["start_timestamp"] == "2026-01-01T00:00:00Z"
    assert box["end_timestamp"] == "2026-01-01T00:29:00Z"


def test_numeric_epoch_timestamps():
    """数值时间戳（毫秒 epoch）也能给出 duration_seconds。"""
    closes = (
        [_OUT_HIGH] * 3
        + [_IN] * 13  # 3..15
        + [_OUT_LOW] * 3
        + [_IN] * 6  # 19..24
        + [_OUT_HIGH] * 5
    )
    epoch_ms = [1_700_000_000_000 + i * 60_000 for i in range(len(closes))]
    box = detect_box_range(
        klines=_klines(closes, timestamps=epoch_ms),
        max_width_pct=0.15,
        pivot_window=2,
        atr_period=3,
    )["box"]

    assert box["start_index"] == 3
    assert box["end_index"] == 24
    assert box["start_timestamp"] == 1_700_000_180_000
    assert box["end_timestamp"] == 1_700_001_440_000
    assert box["duration_seconds"] == 21 * 60.0


def test_numeric_epoch_seconds():
    """数值时间戳（秒级 epoch）走 scale=1.0 分支，时长单位为秒。"""
    closes = [_OUT_HIGH] * 3 + [_IN] * 13 + [_OUT_LOW] * 3 + [_IN] * 6 + [_OUT_HIGH] * 5
    epoch_s = [1_700_000_000 + i * 60 for i in range(len(closes))]
    box = detect_box_range(
        klines=_klines(closes, timestamps=epoch_s),
        max_width_pct=0.15,
        pivot_window=2,
        atr_period=3,
    )["box"]

    assert box["start_timestamp"] == 1_700_000_180
    assert box["end_timestamp"] == 1_700_001_440
    assert box["duration_seconds"] == 21 * 60.0


def test_iso_naive_aware_mix():
    """ISO 时间戳 naive/aware 混用：naive 按 UTC 归一化后正常计算，不抛 TypeError。"""
    closes = [_OUT_HIGH] * 3 + [_IN] * 13 + [_OUT_LOW] * 3 + [_IN] * 6 + [_OUT_HIGH] * 5
    timestamps = [f"2026-01-01T00:{i:02d}:00Z" for i in range(len(closes))]
    timestamps[3] = "2026-01-01T00:03:00"  # naive（无时区）
    timestamps[24] = "2026-01-01T00:24:00+00:00"  # aware
    box = _run(closes, timestamps=timestamps)

    assert box["start_index"] == 3
    assert box["end_index"] == 24
    assert box["duration_seconds"] == 21 * 60


def test_malformed_iso_and_mixed_types_degrade():
    """不可解析的 ISO 字符串 / 数值与字符串混用：不抛异常、duration_seconds 为 None。"""
    closes = [_OUT_HIGH] * 3 + [_IN] * 13 + [_OUT_LOW] * 3 + [_IN] * 6 + [_OUT_HIGH] * 5
    timestamps = [f"2026-01-01T00:{i:02d}:00Z" for i in range(len(closes))]

    bad_iso = list(timestamps)
    bad_iso[24] = "not-a-date"
    box = _run(closes, timestamps=bad_iso)
    assert box["start_index"] == 3
    assert box["end_index"] == 24
    assert box["duration_seconds"] is None

    mixed = list(timestamps)
    mixed[24] = 1_700_001_440  # 数值 vs 字符串
    box = _run(closes, timestamps=mixed)
    assert box["start_index"] == 3
    assert box["end_index"] == 24
    assert box["duration_seconds"] is None

    mixed_units = [1_700_000_000 + i * 60 for i in range(len(closes))]
    mixed_units[24] = 1_700_001_440_000  # 秒 vs 毫秒
    box = _run(closes, timestamps=mixed_units)
    assert box["start_index"] == 3
    assert box["end_index"] == 24
    assert box["duration_seconds"] is None


def test_inbox_boundary_equality():
    """在箱判定含边界：close 恰为 lower×(1-tol) / upper×(1+tol) 算在箱内。"""
    stats = _box_time_stats(
        [
            {"close": 99.0},  # == 100 × 0.99 → 在箱
            {"close": 111.1},  # == 110 × 1.01 → 在箱
            {"close": 98.99},  # < 下沿 → 离箱
            {"close": 111.11},  # > 上沿 → 离箱
        ],
        lower=100.0,
        upper=110.0,
        tolerance_pct=0.01,
    )

    assert stats["start_index"] == 0
    assert stats["end_index"] == 1
    assert stats["bars_in_box"] == 2
    assert stats["duration_bars"] == 2


def test_started_inside_then_exits():
    """窗口开头在箱内，中途离箱后不再回：started_in_box=True 且 end < len-1。"""
    closes = [_IN] * 12 + [_OUT_LOW] * 18  # 0..11 在箱，12..29 离箱
    timestamps = [f"2026-01-01T00:{i:02d}:00Z" for i in range(len(closes))]
    box = _run(closes, timestamps=timestamps)

    assert box["start_index"] == 0
    assert box["end_index"] == 11
    assert box["started_in_box"] is True
    assert box["duration_bars"] == 12
    assert box["bars_in_box"] == 12
    assert box["duration_seconds"] == 11 * 60


def test_approaching_stage_in_expansion_band():
    """容差扩展带语义：close 尚未触及 lower（略低于 100）但 ≥ lower×(1-tol) 即算在箱。"""
    tol = _run([_IN] * 30)["tolerance_pct"]
    close_in_band = 100.0 * (1 - tol / 2)  # 位于 (lower×(1-tol), lower) 之间
    assert close_in_band < 100.0  # 确实还没到 lower
    closes = [_OUT_HIGH] * 3 + [close_in_band] * 15 + [_OUT_HIGH] * 12  # 3..17 在箱
    box = _run(closes)

    assert box["start_index"] == 3
    assert box["end_index"] == 17
    assert box["bars_in_box"] == 15


def test_single_bar_in_box():
    """退化输入：只有一根 close 在箱（duration_bars == 1）。

    默认 min_duration_bars=12 会否掉这种伪箱体；显式放宽到 1 时保留原有统计语义。
    """
    closes = [_OUT_HIGH] * 5 + [_IN] + [_OUT_LOW] * 14  # index 5 唯一在箱

    rejected = _detect(closes)
    assert rejected["is_ranging"] is False
    assert "持续时间不足" in rejected["reason"]
    assert rejected["params"]["min_duration_bars"] == 12

    box = _run(closes, min_duration_bars=1)
    assert box["start_index"] == 5
    assert box["end_index"] == 5
    assert box["duration_bars"] == 1
    assert box["bars_in_box"] == 1
    assert box["started_in_box"] is False


def test_missing_timestamps():
    """K 线无 timestamp 时不报错：时间戳相关字段为 None，index/bar 统计仍可用。"""
    closes = [_OUT_HIGH] * 3 + [_IN] * 13 + [_OUT_LOW] * 3 + [_IN] * 6 + [_OUT_HIGH] * 5
    box = _run(closes)

    assert box["start_index"] == 3
    assert box["end_index"] == 24
    assert box["duration_bars"] == 22
    assert box["start_timestamp"] is None
    assert box["end_timestamp"] is None
    assert box["duration_seconds"] is None


def test_close_never_enters_box():
    """close 全程在箱外（只靠影线构成箱体）：时间字段优雅降级为 None（bars_in_box=0）。

    duration_bars 为 None → 默认门槛下判为非箱体；min_duration_bars=0 关闭门槛时
    保留原有的优雅降级语义。
    """
    closes = [_OUT_HIGH] * 15 + [_OUT_LOW] * 15

    rejected = _detect(closes)
    assert rejected["is_ranging"] is False
    assert "持续时间不足" in rejected["reason"]

    box = _run(closes, min_duration_bars=0)
    assert box["upper"] > box["lower"]
    assert box["start_index"] is None
    assert box["end_index"] is None
    assert box["duration_bars"] is None
    assert box["bars_in_box"] == 0
    assert box["started_in_box"] is False
    assert box["duration_seconds"] is None


# ── 箱体持续时间门槛（min_duration_bars，默认 12 根）────────────────────────


def test_min_duration_bars_gate_default_12():
    """默认门槛：11 根箱体被否且给出原因，12 根箱体通过。"""
    short = (
        [_OUT_HIGH] * 5 + [_IN] * 11 + [_OUT_LOW] * 10
    )  # start 5 → end 15，duration 11
    rejected = _detect(short)
    assert rejected["is_ranging"] is False
    assert "持续时间不足" in rejected["reason"]
    assert rejected["params"]["min_duration_bars"] == 12

    ok = _detect([_OUT_HIGH] * 5 + [_IN] * 12 + [_OUT_LOW] * 10)  # duration 12
    assert ok["is_ranging"] is True
    assert ok["box"]["duration_bars"] == 12
    assert ok["params"]["min_duration_bars"] == 12


def test_min_duration_bars_explicit_value():
    """显式门槛生效：同一段 11 根箱体，门槛 11 通过、12 被否。"""
    closes = [_OUT_HIGH] * 5 + [_IN] * 11 + [_OUT_LOW] * 10
    assert _detect(closes, min_duration_bars=11)["is_ranging"] is True
    assert _detect(closes, min_duration_bars=12)["is_ranging"] is False


def test_min_duration_bars_zero_disables_gate():
    """0 = 关闭门槛：duration_bars == 1 的箱体也返回。"""
    closes = [_OUT_HIGH] * 5 + [_IN] + [_OUT_LOW] * 14
    assert _detect(closes, min_duration_bars=0)["is_ranging"] is True
    assert _run(closes, min_duration_bars=0)["duration_bars"] == 1


def test_min_duration_bars_negative_raises():
    """负数非法：越界参数直接抛 ValueError。"""
    with pytest.raises(ValueError):
        _detect([_IN] * 30, min_duration_bars=-1)


# ── 箱体宽度下限（min_width_pct，默认 0.005 = 0.5%）────────────────────────


def _tight_klines(closes, hi=100.40, lo=100.00):
    """极窄箱体 K 线（宽度 ≈ 0.4%：上沿 100.40 / 下沿 100.00）。

    几何要点：非 pivot 的 high/low 必须离容差带（ATR 推导，约 0.1%）足够远。
    否则连续落在带内的 K 线会被 _count_touches 的 min_gap_bars 合并成 1 次触碰
    （last_touch_index 每根都更新，间隔恒为 1），cluster 会被判"触碰不足"——
    那是既有语义，不是本用例要测的东西。close 全程取 100.20（在箱内）。
    """
    out = []
    for i, c in enumerate(closes):
        m = i % 4
        if m == 0:  # 上沿 pivot
            h, l = hi, 100.25  # noqa: E741
        elif m == 1:  # 下沿 pivot
            h, l = 100.28, lo  # noqa: E741
        elif m == 2:
            h, l = 100.22, 100.20  # noqa: E741
        else:
            h, l = 100.25, 100.18  # noqa: E741
        out.append(
            {"open": (h + l) / 2, "high": h, "low": l, "close": c, "volume": 1000}
        )
    return out


def _tight_case(**kwargs):
    """宽度 ≈ 0.4%、时长满窗（27 根）的极窄箱体：宽度下限是唯一的变量。"""
    return detect_box_range(
        klines=_tight_klines([100.20] * 27),
        max_width_pct=0.15,
        pivot_window=2,
        atr_period=3,
        **kwargs,
    )


def test_min_width_pct_default_blocks_knife_edge_box():
    """默认 0.005：~0.4% 宽的刀锋薄箱体被判为非箱体，且原因是宽度低于下限。"""
    r = _tight_case()
    assert r["is_ranging"] is False
    assert "相对宽度低于下限" in r["reason"]
    assert r["params"]["min_width_pct"] == 0.005


def test_min_width_pct_zero_disables_floor():
    """0 = 关闭宽度下限：同一段极窄箱体通过，width_pct ≈ 0.4%。"""
    r = _tight_case(min_width_pct=0)
    assert r["is_ranging"] is True
    assert 0.003 < r["box"]["width_pct"] < 0.005


def test_min_width_pct_explicit_threshold():
    """显式阈值生效：同一窄箱体，阈值 0.003 通过、0.005 被否。"""
    assert _tight_case(min_width_pct=0.003)["is_ranging"] is True
    assert _tight_case(min_width_pct=0.005)["is_ranging"] is False


@pytest.mark.asyncio
async def test_tool_passthrough_time_fields():
    """Agent 工具入口同样返回时间起止字段。"""
    closes = [_OUT_HIGH] * 3 + [_IN] * 13 + [_OUT_LOW] * 3 + [_IN] * 6 + [_OUT_HIGH] * 5
    result = await DetectBoxRangeTool().execute(
        klines=_klines(closes),
        max_width_pct=0.15,
        pivot_window=2,
        atr_period=3,
    )

    assert result.success is True
    box = result.data["box"]
    assert box["start_index"] == 3
    assert box["end_index"] == 24
    assert box["duration_bars"] == 22
    assert box["started_in_box"] is False
