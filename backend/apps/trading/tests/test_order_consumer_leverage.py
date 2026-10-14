"""Ticket 003 补充：order_consumer 的 leverage 透传契约。

Redis 消息 fields 里的 leverage（字符串）应原样传给 executor.submit_order 的
leverage 参数（int 类型）；现货消息无该字段时缺省 1。
"""

from unittest.mock import AsyncMock, patch

import pytest

from apps.trading.order_consumer import _process_order


@pytest.mark.asyncio
async def test_leverage_string_passthrough():
    """leverage='3' → submit_order(leverage=3)."""
    fields = {
        "exchange": "binance",
        "symbol": "BTC/USDT:USDT",
        "side": "buy",
        "order_type": "market",
        "quantity": "0.01",
        "exchange_account_id": "acc-1",
        "leverage": "3",
    }
    with patch("apps.trading.order_consumer.OrderExecutor.get_instance") as mock_get:
        mock_exec = AsyncMock()
        mock_exec.submit_order = AsyncMock(return_value={"status": "ok"})
        mock_get.return_value = mock_exec
        await _process_order(fields)
    assert mock_exec.submit_order.call_args.kwargs["leverage"] == 3


@pytest.mark.asyncio
async def test_leverage_default_one_when_absent():
    """现货消息无 leverage 字段 → submit_order(leverage=1)."""
    fields = {
        "exchange": "binance",
        "symbol": "BTC/USDT",
        "side": "buy",
        "order_type": "market",
        "quantity": "0.01",
        "exchange_account_id": "acc-1",
    }
    with patch("apps.trading.order_consumer.OrderExecutor.get_instance") as mock_get:
        mock_exec = AsyncMock()
        mock_exec.submit_order = AsyncMock(return_value={"status": "ok"})
        mock_get.return_value = mock_exec
        await _process_order(fields)
    assert mock_exec.submit_order.call_args.kwargs["leverage"] == 1


@pytest.mark.asyncio
async def test_leverage_empty_string_defaults_one():
    """leverage='' (falsy) → 缺省 1，避免 int('') 崩。"""
    fields = {
        "exchange": "binance",
        "symbol": "BTC/USDT",
        "side": "buy",
        "order_type": "market",
        "quantity": "0.01",
        "exchange_account_id": "acc-1",
        "leverage": "",
    }
    with patch("apps.trading.order_consumer.OrderExecutor.get_instance") as mock_get:
        mock_exec = AsyncMock()
        mock_exec.submit_order = AsyncMock(return_value={"status": "ok"})
        mock_get.return_value = mock_exec
        await _process_order(fields)
    assert mock_exec.submit_order.call_args.kwargs["leverage"] == 1
