"""Binance WS 消息处理的时区一致性回归测试。

生产事故（2026-10-17 09:10:07）：
    [Binance] spot WS receive error: TypeError: can't subtract offset-naive
    and offset-aware datetimes

根因：`_handle_websocket_message` 用裸 `datetime.now()`（naive）作为
receive_time，而 `normalize_kline` / `normalize_trade` 产出的 `timestamp`
是 aware-UTC；`DataQualityMonitor.record_data` 计算延迟时
`receive_time - data_timestamp` 直接抛 TypeError。

该异常在 `_ws_spot_receiver` / `_ws_futures_receiver` 的宽泛 `except
Exception` 里被吞掉，表现为**每收到一条 K 线就报错一次**、该条数据在
写入 store 之后、进入清洗/延迟统计之前中断，因此不会断开连接、只在日志
里刷屏（生产上 ~2s 一条）。

本文件锁定：所有经 WS 分发的数据类型都必须能通过 record_data 的延迟
计算，且延迟值必须是真实量级（naive/aware 混算即便侥幸不抛，也会得到
天文数字）。fixture 的事件时间按「当前时刻」相对构造，避免陈旧时间戳
把延迟断言变成依赖绝对日期的脆弱断言。
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from apps.datasource.base import MarketType
from apps.datasource.monitor import DataQualityMonitor
from apps.datasource.sources.crypto.binance import BinanceDataSource

# 事件时间相对「现在」的偏移：模拟 WS 推送在途延迟
_EVENT_AGE = timedelta(seconds=1)


def _event_ms(age: timedelta = _EVENT_AGE) -> int:
    """构造相对当前时刻的 Binance 毫秒时间戳。"""
    return int((datetime.now(tz=timezone.utc) - age).timestamp() * 1000)


def _binance():
    """构造一个不经网络初始化的 BinanceDataSource。"""
    ds = BinanceDataSource.__new__(BinanceDataSource)
    ds.name = "binance"
    ds._store = MagicMock()
    ds._ticker_logged = set()
    ds._trigger_precise_callbacks = MagicMock()
    ds._last_data_time = 0
    ds._monitor = DataQualityMonitor()
    # 隔离监控器状态，避免用例间相互污染
    ds._monitor._latencies.clear()
    ds._monitor._reports.clear()
    return ds


def _kline_msg() -> dict:
    """Binance 现货 @kline 真实 payload 形状。"""
    open_ms = _event_ms()
    return {
        "e": "kline",
        "E": open_ms,
        "s": "BTCUSDT",
        "k": {
            "t": open_ms,
            "T": open_ms + 59_999,
            "s": "BTCUSDT",
            "i": "1m",
            "o": "46832.89000000",
            "c": "46860.00000000",
            "h": "46875.63000000",
            "l": "46832.88000000",
            "v": "12.3",
            "q": "576000.0",
            "n": 42,
            "x": False,
        },
    }


def _agg_trade_msg() -> dict:
    """Binance 现货 @aggTrade 真实 payload 形状。"""
    ts = _event_ms()
    return {
        "e": "aggTrade",
        "E": ts,
        "s": "BTCUSDT",
        "a": 26129,
        "p": "46860.0",
        "q": "0.01",
        "T": ts,
        "m": True,
    }


def _ticker_msg() -> dict:
    """Binance 现货 @ticker 真实 payload 形状（无事件时间字段，靠接收时刻兜底）。"""
    return {
        "e": "24hrTicker",
        "E": _event_ms(),
        "s": "BTCUSDT",
        "p": "12.0",
        "P": "0.025",
        "c": "46860.0",
        "b": "46859.0",
        "B": "1.0",
        "a": "46861.0",
        "A": "1.0",
        "h": "47000.0",
        "l": "46000.0",
        "v": "1000.0",
        "q": "46000000.0",
    }


@pytest.mark.parametrize(
    "msg_factory,data_type",
    [
        (_kline_msg, "kline"),
        (_agg_trade_msg, "trade"),
        (_ticker_msg, "ticker"),
    ],
    ids=["kline", "aggTrade", "24hrTicker"],
)
@pytest.mark.asyncio
async def test_ws_message_latency_does_not_mix_naive_and_aware(msg_factory, data_type):
    """每种 WS 消息都必须能进 record_data，且延迟是真实量级。"""
    ds = _binance()

    # 事故原貌：TypeError 会在 receive 循环里被吞，这里直接断言不抛
    await ds._handle_websocket_message(msg_factory(), MarketType.SPOT)

    bucket = ds._monitor._latencies.get("binance", {}).get("BTCUSDT", {})
    assert data_type in bucket, f"{data_type} 未写入延迟统计"
    latency = bucket[data_type][-1]
    # 事件时间距现在约 1s；naive/aware 混算会得到约 10^11 ms 的天文数字。
    # ticker 的 timestamp 就是接收时刻本身，两侧时钟读取顺序会带来微秒级负值，
    # 故比较绝对值。
    assert abs(latency) < 10_000, f"{data_type} 延迟异常: {latency}ms"


@pytest.mark.asyncio
async def test_ticker_timestamp_is_timezone_aware():
    """ticker 的 timestamp 必须是 aware-UTC，与 kline/trade 口径一致。"""
    ds = _binance()

    await ds._handle_websocket_message(_ticker_msg(), MarketType.SPOT)

    stored = ds._store.store.call_args[0][2]
    assert stored["timestamp"].tzinfo is not None
    assert stored["timestamp"].utcoffset().total_seconds() == 0


@pytest.mark.asyncio
async def test_kline_latency_is_sane_in_any_local_timezone(monkeypatch):
    """receive_time 必须是 aware：本机时区无关，naive 会直接抛 TypeError。"""
    ds = _binance()

    # 冻结"现在"，模拟非 UTC 本机（naive 分支按本地时区返回）
    import apps.datasource.sources.crypto.binance as binance_mod

    frozen_utc = datetime.now(tz=timezone.utc)

    class _FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen_utc if tz is not None else frozen_utc.replace(tzinfo=None)

        @classmethod
        def fromtimestamp(cls, ts, tz=None):
            return datetime.fromtimestamp(ts, tz=tz or timezone.utc)

    monkeypatch.setattr(binance_mod, "datetime", _FixedDateTime)

    msg = _kline_msg()
    # 让 K 线开盘时间正好比冻结时刻早 1 秒
    open_ms = int((frozen_utc - _EVENT_AGE).timestamp() * 1000)
    msg["k"]["t"] = open_ms
    msg["k"]["T"] = open_ms + 59_999

    await ds._handle_websocket_message(msg, MarketType.SPOT)

    latency = ds._monitor._latencies["binance"]["BTCUSDT"]["kline"][-1]
    assert 0 <= latency < 5_000, f"延迟异常: {latency}ms"
