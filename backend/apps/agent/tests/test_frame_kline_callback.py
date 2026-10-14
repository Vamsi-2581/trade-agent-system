"""回归测试：_on_kline_data 长跑回调必须 close_old_connections。

Bug：K 线回调走 sync_to_async 线程池，pgbouncer/PG 空闲关闭连接后，下一次
ORM 操作抛 "connection already closed"，整个信号检查静默失败。
修复：在 _on_kline_data 进入时先调 close_old_connections() 刷新连接。
兼容同步和异步签名（HEAD 是同步 def，working tree 是 async def）。
"""

from __future__ import annotations

import asyncio
import inspect
from unittest.mock import MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_on_kline_data_refreshes_db_connection():
    """K 线回调进入时必须调用 close_old_connections()。"""
    from apps.agent.frame_manager import FrameManager

    fm = FrameManager.__new__(FrameManager)  # 跳过 __init__ 的 Redis 访问
    fm._kline_check_sem = asyncio.Semaphore(2)  # 异步签名需要

    with patch("apps.agent.frame_manager.close_old_connections") as mock_close:
        engine_mock = MagicMock()
        engine_mock._load_klines_for_monitors.return_value = {}
        engine_mock.check_signals_for_kline = MagicMock()

        with patch(
            "apps.signal_monitor.engine.SignalMonitorEngine.get_instance",
            return_value=engine_mock,
        ):
            result = fm._on_kline_data({"close": 1.0}, "BTC/USDT")
            if inspect.iscoroutine(result):
                await result

    assert mock_close.called, (
        "close_old_connections() 必须在 _on_kline_data 内部调用，"
        "否则长跑回调的 DB 连接会被 pgbouncer 关闭导致 'connection already closed'"
    )
