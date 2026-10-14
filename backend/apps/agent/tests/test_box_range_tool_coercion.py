"""Regression: DetectBoxRangeTool must accept a fetch_ohlcv temp_file path string.

The original bug: tool rejected any string klines with
"klines 必须是 K线 dict 列表", so the agent could not pass fetch_ohlcv's
temp_file output through. This test pins the new behavior.
"""

import json

import pytest

from apps.agent.tools.box_range import DetectBoxRangeTool


@pytest.mark.asyncio
async def test_klines_as_temp_file_path_is_accepted(tmp_path):
    sample = [
        {
            "timestamp": "2025-01-01T00:00:00",
            "open": 100,
            "high": 102,
            "low": 99,
            "close": 101,
            "volume": 1,
        },
        {
            "timestamp": "2025-01-02T00:00:00",
            "open": 101,
            "high": 103,
            "low": 100,
            "close": 102,
            "volume": 1,
        },
        {
            "timestamp": "2025-01-03T00:00:00",
            "open": 102,
            "high": 104,
            "low": 101,
            "close": 103,
            "volume": 1,
        },
        {
            "timestamp": "2025-01-04T00:00:00",
            "open": 103,
            "high": 105,
            "low": 102,
            "close": 104,
            "volume": 1,
        },
        {
            "timestamp": "2025-01-05T00:00:00",
            "open": 104,
            "high": 106,
            "low": 103,
            "close": 105,
            "volume": 1,
        },
    ]
    path = tmp_path / "ohlcv.json"
    path.write_text(json.dumps(sample))

    tool = DetectBoxRangeTool()
    result = await tool.execute(klines=str(path), max_width_pct=0.5)

    # pre-fix: result.error == "klines 必须是 K线 dict 列表"
    assert "klines 必须是 K线 dict 列表" not in (result.error or ""), (
        f"temp_file path string must be accepted, got: {result.error}"
    )
    # the underlying call may still fail for other reasons (e.g. ATR length),
    # but it must NOT be the coercion error
    assert result.error is None or "klines" not in result.error.lower()


@pytest.mark.asyncio
async def test_klines_as_list_still_works():
    tool = DetectBoxRangeTool()
    sample = [
        {"open": 100, "high": 102, "low": 99, "close": 101},
        {"open": 101, "high": 103, "low": 100, "close": 102},
        {"open": 102, "high": 104, "low": 101, "close": 103},
    ]
    result = await tool.execute(klines=sample, max_width_pct=0.5)
    # should not be a coercion error
    assert (result.error or "") != "klines 必须是 K线 dict 列表"


@pytest.mark.asyncio
async def test_nonexistent_path_gives_actionable_error():
    tool = DetectBoxRangeTool()
    result = await tool.execute(
        klines="/tmp/does_not_exist_xyz.json", max_width_pct=0.5
    )
    assert result.success is False
    assert "路径不存在" in (result.error or "")


@pytest.mark.asyncio
async def test_none_klines_gives_clear_error():
    tool = DetectBoxRangeTool()
    result = await tool.execute(klines=None, max_width_pct=0.5)
    assert result.success is False
    assert "K线 dict 列表" in (result.error or "")
