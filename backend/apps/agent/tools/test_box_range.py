"""Tests for DetectBoxRangeTool."""

from __future__ import annotations

import pytest

from apps.agent.tools.box_range import DetectBoxRangeTool


def _ranging_klines(n: int = 40) -> list[dict]:
    """构造典型箱体 K 线：每 5 根一个周期（3 根贴 upper → 1 根跌出 → 1 根贴 lower），
    使触碰间隔 ≥ min_gap_bars，从而被识别为多次触碰。"""
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
            "timestamp": f"2026-01-01T00:{i:02d}:00Z",
            "open": (high + low) / 2,
            "high": high,
            "low": low,
            "close": (high + low) / 2,
            "volume": 1000,
        }
        for i, (high, low) in enumerate(zip(highs, lows))
    ]


def _trending_klines(n: int = 40) -> list[dict]:
    """构造强趋势 K 线：价格单调上涨。"""
    return [
        {
            "timestamp": f"2026-01-01T00:{i:02d}:00Z",
            "open": 100 + i,
            "high": 102 + i,
            "low": 99 + i,
            "close": 101 + i,
            "volume": 1000,
        }
        for i in range(n)
    ]


@pytest.mark.asyncio
async def test_typical_ranging_market_detected():
    """用例 1：典型箱体 → is_ranging=True，box 字段完整。"""
    result = await DetectBoxRangeTool().execute(
        klines=_ranging_klines(),
        max_width_pct=0.15,
        pivot_window=2,
        atr_period=3,
    )

    assert result.success is True
    data = result.data
    assert data["is_ranging"] is True
    assert data["box"] is not None
    box = data["box"]
    assert box["upper"] > box["lower"]
    assert box["upper_pivot_count"] >= 2
    assert box["lower_pivot_count"] >= 2
    assert box["upper_touches"] >= 2
    assert box["lower_touches"] >= 2
    assert data["reason"] == ""


@pytest.mark.asyncio
async def test_trending_market_not_detected():
    """用例 2：强趋势行情 → is_ranging=False，box=None，reason 给出原因。"""
    result = await DetectBoxRangeTool().execute(
        klines=_trending_klines(),
        max_width_pct=0.15,
        pivot_window=2,
        atr_period=3,
    )

    assert result.success is True
    data = result.data
    assert data["is_ranging"] is False
    assert data["box"] is None
    assert data["reason"]  # 必须给出失败原因


@pytest.mark.asyncio
async def test_pivot_count_insufficient():
    """用例 3：单边 pivot 不足 → reason 明确指向 pivot 数量。"""
    # high 单调阶梯递增（无局部高点 → high pivot = 0）；
    # low 每 5 根一个周期反复回到 100（保证 low pivot 充足）。
    n = 30
    highs, lows = [], []
    for i in range(n):
        highs.append(108 + i * 0.1)  # 单调递增
        cycle = i % 5
        lows.append(100 if cycle == 4 else 105)
    klines = [
        {
            "timestamp": f"2026-01-01T00:{i:02d}:00Z",
            "open": (h + lo) / 2,
            "high": h,
            "low": lo,
            "close": (h + lo) / 2,
            "volume": 1000,
        }
        for i, (h, lo) in enumerate(zip(highs, lows))
    ]
    result = await DetectBoxRangeTool().execute(
        klines=klines,
        max_width_pct=0.50,
        pivot_window=2,
        atr_period=3,
    )

    assert result.success is True
    assert result.data["is_ranging"] is False
    assert "pivot" in result.data["reason"]


@pytest.mark.asyncio
async def test_width_params_missing():
    """用例 4：宽度参数都没传 → success=False，error 明确提示。"""
    result = await DetectBoxRangeTool().execute(
        klines=_ranging_klines(),
        pivot_window=2,
        atr_period=3,
    )

    assert result.success is False
    assert "max_width_abs" in result.error or "max_width_pct" in result.error


@pytest.mark.asyncio
async def test_width_exceeded():
    """用例 5：宽度阈值过小 → is_ranging=False，reason 包含"宽度"。"""
    result = await DetectBoxRangeTool().execute(
        klines=_ranging_klines(),
        max_width_pct=0.01,  # 1% 远小于实际宽度
        pivot_window=2,
        atr_period=3,
    )

    assert result.success is True
    assert result.data["is_ranging"] is False
    assert "宽度" in result.data["reason"]


@pytest.mark.asyncio
async def test_width_exceeded_absolute():
    """用例 6：max_width_abs 绝对宽度超限 → is_ranging=False，reason 包含"宽度"。"""
    result = await DetectBoxRangeTool().execute(
        klines=_ranging_klines(),
        max_width_abs=1.0,  # 1 远小于实际宽度 10
        pivot_window=2,
        atr_period=3,
    )

    assert result.success is True
    assert result.data["is_ranging"] is False
    assert "宽度" in result.data["reason"]
    assert "绝对" in result.data["reason"]


@pytest.mark.asyncio
async def test_width_params_both_set():
    """用例 7：max_width_abs 和 max_width_pct 都传 → success=False，error 提示二选一。"""
    result = await DetectBoxRangeTool().execute(
        klines=_ranging_klines(),
        max_width_abs=5.0,
        max_width_pct=0.1,
        pivot_window=2,
        atr_period=3,
    )

    assert result.success is False
    assert "二选一" in result.error


@pytest.mark.asyncio
async def test_klines_too_short():
    """用例 8：K 线数 < 2*pivot_window+1 → success=False，error 提示。"""
    klines = [
        {
            "timestamp": f"2026-01-01T00:{i:02d}:00Z",
            "open": 105,
            "high": 110,
            "low": 100,
            "close": 105,
            "volume": 1000,
        }
        for i in range(3)  # pivot_window=2 需要至少 5 根
    ]
    result = await DetectBoxRangeTool().execute(
        klines=klines,
        max_width_pct=0.15,
        pivot_window=2,
        atr_period=3,
    )

    assert result.success is False
    assert "K线数量不足" in result.error


@pytest.mark.asyncio
async def test_kline_missing_field():
    """用例 9：某根 K 线缺 high/low/close → success=False，error 指向具体字段。"""
    klines = _ranging_klines()
    klines[5] = {  # 删除 close 字段
        "timestamp": "2026-01-01T00:05:00Z",
        "open": 105,
        "high": 110,
        "low": 100,
        "volume": 1000,
    }
    result = await DetectBoxRangeTool().execute(
        klines=klines,
        max_width_pct=0.15,
        pivot_window=2,
        atr_period=3,
    )

    assert result.success is False
    assert "close" in result.error
    assert "5" in result.error


@pytest.mark.asyncio
async def test_kline_invalid_value():
    """用例 10：K 线 high/low/close 为 0 或负数 → success=False，error 提示必须为正数。"""
    klines = _ranging_klines()
    klines[3]["high"] = 0  # 无效值
    result = await DetectBoxRangeTool().execute(
        klines=klines,
        max_width_pct=0.15,
        pivot_window=2,
        atr_period=3,
    )

    assert result.success is False
    assert "正数" in result.error
    assert "3" in result.error


@pytest.mark.asyncio
async def test_coarse_filter_rejects_low_upper():
    """用例 11：窗口高点 200，cluster 上沿 ~110（远低于 200）→ 粗筛拒绝。"""
    # 构造：高 200 大箱体（仅 1 个） + 高 110 小箱体（多次 cluster）
    # 算法应拒绝，因为 upper 必须 >= 200 × (1 - 0.15) = 170
    n = 40
    klines = []
    for i in range(n):
        if i < 10:
            # 大高点 200
            klines.append(
                {
                    "timestamp": f"2026-01-01T{i:02d}:00Z",
                    "open": 195,
                    "high": 200,
                    "low": 190,
                    "close": 195,
                    "volume": 1000,
                }
            )
        elif i < 15:
            # 跌到 110
            klines.append(
                {
                    "timestamp": f"2026-01-01T{i:02d}:00Z",
                    "open": 150,
                    "high": 155,
                    "low": 110,
                    "close": 130,
                    "volume": 1000,
                }
            )
        elif i < 30:
            # 小箱体 110-130
            cycle = (i - 15) % 5
            h = 130 if cycle < 3 else 125
            lo = 110 if cycle < 3 else 108
            klines.append(
                {
                    "timestamp": f"2026-01-01T{i:02d}:00Z",
                    "open": (h + lo) / 2,
                    "high": h,
                    "low": lo,
                    "close": (h + lo) / 2,
                    "volume": 1000,
                }
            )
        else:
            # 继续小箱体
            cycle = (i - 30) % 5
            h = 130 if cycle < 3 else 125
            lo = 110 if cycle < 3 else 108
            klines.append(
                {
                    "timestamp": f"2026-01-01T{i:02d}:00Z",
                    "open": (h + lo) / 2,
                    "high": h,
                    "low": lo,
                    "close": (h + lo) / 2,
                    "volume": 1000,
                }
            )
    result = await DetectBoxRangeTool().execute(
        klines=klines,
        max_width_pct=0.30,  # 宽阈值，避免宽度拦截
        pivot_window=2,
        atr_period=3,
    )
    assert result.success is True
    assert result.data["is_ranging"] is False
    assert "上沿" in result.data["reason"]


@pytest.mark.asyncio
async def test_min_width_pct_rejects_tight_cluster():
    """用例 12：min_width_pct=0.05 → 宽 1% 的小箱体被拒。"""
    # 典型小箱体：高 105/低 100（5% 宽 = 5/102.5 ≈ 4.9%，在 0.05 边界）
    n = 40
    klines = []
    for i in range(n):
        cycle = i % 5
        if cycle < 3:
            h, lo = 105, 102
        elif cycle == 3:
            h, lo = 103, 100
        else:
            h, lo = 102, 100
        klines.append(
            {
                "timestamp": f"2026-01-01T{i:02d}:00Z",
                "open": (h + lo) / 2,
                "high": h,
                "low": lo,
                "close": (h + lo) / 2,
                "volume": 1000,
            }
        )
    # 不带 min_width_pct 应该能识别为箱体
    r_ok = await DetectBoxRangeTool().execute(
        klines=klines,
        max_width_pct=0.15,
        pivot_window=2,
        atr_period=3,
    )
    assert r_ok.data["is_ranging"] is True
    # 带 min_width_pct=0.05 (5%) 5% 临界 → 实际 ~3% 宽，应被拒
    r_tight = await DetectBoxRangeTool().execute(
        klines=klines,
        max_width_pct=0.15,
        min_width_pct=0.05,
        pivot_window=2,
        atr_period=3,
    )
    assert r_tight.data["is_ranging"] is False
    assert "宽度" in r_tight.data["reason"]
