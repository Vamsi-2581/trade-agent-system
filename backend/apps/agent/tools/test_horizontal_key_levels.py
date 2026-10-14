"""Tests for GetHorizontalKeyLevelsTool."""

from __future__ import annotations

import pytest

from apps.agent.tools.horizontal_key_levels import GetHorizontalKeyLevelsTool


def _history():
    lows = [
        100,
        102,
        99,
        101,
        103,
        100,
        104,
        106,
        99,
        105,
        107,
        100,
        108,
        110,
        99,
        109,
        111,
        100,
        112,
        114,
    ]
    highs = [
        110,
        112,
        115,
        113,
        111,
        116,
        114,
        112,
        115,
        113,
        111,
        116,
        114,
        112,
        115,
        113,
        111,
        116,
        114,
        112,
    ]
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


@pytest.mark.asyncio
async def test_tool_returns_levels():
    result = await GetHorizontalKeyLevelsTool().execute(
        klines=_history(), current_price=106, atr_period=3, max_levels=20
    )

    assert result.success is True
    assert result.data["levels"]
    assert result.data["supports"]
    assert result.data["resistances"]


@pytest.mark.asyncio
async def test_tool_rejects_bad_klines():
    result = await GetHorizontalKeyLevelsTool().execute(klines=[], atr_period=3)

    assert result.success is False
    assert "K线数量不足" in result.error


@pytest.mark.asyncio
async def test_tool_rejects_invalid_max_levels():
    result = await GetHorizontalKeyLevelsTool().execute(
        klines=_history(), atr_period=3, max_levels=21
    )

    assert result.success is False
    assert "max_levels" in result.error
