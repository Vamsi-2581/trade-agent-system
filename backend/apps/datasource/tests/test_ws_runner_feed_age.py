"""ws_runner 看门狗数据龄计算的时区一致性回归测试。

生产事故（2026-10-17）的同一根因在 ws_runner 里还有第二处：
`_feed_data_age` 用裸 `datetime.now()`（naive）与 store 里的 aware-UTC
`timestamp` 相减，抛 "can't subtract offset-naive and offset-aware
datetimes"。

区别在于后果更隐蔽：该函数在 `_supervise_feed` 的 `while True` 看门狗里
调用，异常冒泡后整个循环退出——**断流重连被静默关掉**，只表现为
supervisor 协程异常结束，不会有 "receive error" 刷屏提示。

注：`MemoryDataStore` 是进程级单例（`__init__` 首启后短路），用例之间会
共享同一份数据，故每个用例写入独立的 data_type/符号，避免相互污染。
"""

from datetime import datetime, timedelta, timezone


from apps.datasource.base import DataType
from apps.datasource.store import get_data_store
from apps.datasource.ws_runner import _feed_data_age


def _seed(symbol: str, ts) -> None:
    store = get_data_store()
    store.store("kline", symbol, {"symbol": symbol, "timestamp": ts}, "binance")


def test_feed_age_with_aware_timestamp():
    """store 里是 aware-UTC 时间戳（数据源正常口径）时不应抛错。"""
    _seed("AWAREUSDT", datetime.now(tz=timezone.utc) - timedelta(seconds=30))

    age = _feed_data_age(get_data_store(), DataType.KLINE, ["AWAREUSDT"])

    assert age is not None
    assert 25 <= age <= 40


def test_feed_age_with_naive_timestamp():
    """历史/其他数据源写入的 naive 时间戳按 UTC 归一，不抛错。"""
    naive = datetime.now(tz=timezone.utc).replace(tzinfo=None)
    _seed("NAIVEUSDT", naive - timedelta(seconds=30))

    age = _feed_data_age(get_data_store(), DataType.KLINE, ["NAIVEUSDT"])

    assert age is not None
    assert 25 <= age <= 40


def test_feed_age_no_data_returns_none():
    """无数据时返回 None（看门狗据此走 feed_never 分支）。"""
    assert _feed_data_age(get_data_store(), DataType.KLINE, ["NOSUCHUSDT"]) is None


def test_feed_age_is_non_negative_for_future_timestamp():
    """时钟偏移导致 timestamp 超前时不返回负龄。"""
    _seed("FUTUREUSDT", datetime.now(tz=timezone.utc) + timedelta(seconds=30))

    assert _feed_data_age(get_data_store(), DataType.KLINE, ["FUTUREUSDT"]) == 0.0
