"""KlineRecord 幂等落库 + 共享交易所函数测试。

覆盖 #12 验收：迁移往返、upsert 幂等 / 批量混合 / 分批边界 / 未收盘 bar 覆写，
以及抽到 apps.datasource.exchange 的共享函数本身。
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import override_settings

from apps.datasource.exchange import (
    build_exchange,
    fetch_ohlcv_paged,
    normalize_binance_symbol,
    timeframe_to_seconds,
)
from apps.datasource.models import KlineRecord
from apps.datasource.services import upsert_klines

# 仓库约定：普通类 + django_db(transaction=True)
# （迁移往返用例需要真实 DDL，故模块级统一开启）
pytestmark = pytest.mark.django_db(transaction=True)


BASE_TS = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _row(
    i: int,
    *,
    close: float = 100.0,
    symbol: str = "BTC/USDT:USDT",
    timeframe: str = "1h",
    exchange: str = "binance",
) -> dict:
    """构造一行 KlineRecord 参数；i 为 bar 序号，按 timeframe 步长递增。"""
    step = timedelta(seconds=timeframe_to_seconds(timeframe))
    return {
        "exchange": exchange,
        "symbol": symbol,
        "timeframe": timeframe,
        "timestamp": BASE_TS + step * i,
        "open": 100.0,
        "high": 110.0,
        "low": 90.0,
        "close": close,
        "volume": 1.0,
    }


class TestKlineRecordSchema:
    """模型约束：幂等键与索引是 upsert 正确性的前提，必须钉住。"""

    def test_unique_together_includes_exchange(self):
        # exchange 建表即入唯一键：未来扩 OKX/Bybit 不必改约束（改唯一键是高风险 DDL）
        assert KlineRecord._meta.unique_together == (
            ("exchange", "symbol", "timeframe", "timestamp"),
        )

    def test_db_table_and_ordering(self):
        assert KlineRecord._meta.db_table == "datasource_klines"
        assert KlineRecord._meta.ordering == ["timestamp"]

    def test_index_covers_symbol_timeframe_timestamp(self):
        indexed = {tuple(idx.fields) for idx in KlineRecord._meta.indexes}
        assert ("symbol", "timeframe", "timestamp") in indexed

    def test_duplicate_key_rejected_by_db(self):
        """约束在数据库层真实生效（不只是 Django 侧声明）。"""
        from django.db import IntegrityError, transaction

        KlineRecord.objects.create(**_row(0))
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                KlineRecord.objects.create(**_row(0, close=999.0))


class TestUpsertKlines:
    """幂等写入语义。"""

    def test_idempotent_same_key_written_once(self):
        """同键写两次 → 1 行，值以第二次为准。"""
        upsert_klines([_row(0, close=100.0)])
        upsert_klines([_row(0, close=200.0)])

        assert KlineRecord.objects.count() == 1
        assert KlineRecord.objects.get().close == 200.0

    def test_unclosed_bar_overwritten_by_closed_value(self):
        """未收盘 bar：先落中间值、收盘后再落最终值 → 行内为收盘值。

        这是增量同步的必需语义：交易所返回的最后一根可能是进行中的 bar。
        """
        upsert_klines([_row(0, close=100.0)])  # bar 进行中，中间值
        assert KlineRecord.objects.get().close == 100.0

        upsert_klines([_row(0, close=123.45)])  # bar 收盘，最终值
        assert KlineRecord.objects.count() == 1
        assert KlineRecord.objects.get().close == 123.45

    def test_batch_mixed_new_and_existing(self):
        """100 行混合新/旧：行数正确、旧值被更新、无 IntegrityError。"""
        upsert_klines([_row(i, close=1.0) for i in range(50)])

        # 前 50 行与已有键重叠（改 close），后 50 行为新键
        mixed = [_row(i, close=2.0) for i in range(50)] + [
            _row(i, close=3.0) for i in range(50, 100)
        ]
        written = upsert_klines(mixed)

        assert written == 100
        assert KlineRecord.objects.count() == 100
        # 重叠部分被覆盖成第二次的值
        assert KlineRecord.objects.filter(close=2.0).count() == 50
        assert KlineRecord.objects.filter(close=3.0).count() == 50
        assert not KlineRecord.objects.filter(close=1.0).exists()

    def test_batch_boundary_2500_rows_not_lost(self):
        """2500 行 + batch_size=1000 → 3 批，不丢行。"""
        rows = [_row(i, symbol="ETH/USDT:USDT", timeframe="15m") for i in range(2500)]

        written = upsert_klines(rows, batch_size=1000)

        assert written == 2500
        assert KlineRecord.objects.count() == 2500
        # 边界处（第 1000、2000 行）也在库里——分批若写错区间这里会缺
        assert KlineRecord.objects.filter(timestamp=rows[999]["timestamp"]).exists()
        assert KlineRecord.objects.filter(timestamp=rows[1999]["timestamp"]).exists()
        assert KlineRecord.objects.filter(timestamp=rows[-1]["timestamp"]).exists()

    def test_batch_boundary_reupsert_is_idempotent(self):
        """跨批重跑同一批 2500 行仍不增行、不报错。"""
        rows = [_row(i, timeframe="4h") for i in range(2500)]
        upsert_klines(rows, batch_size=1000)
        written = upsert_klines(rows, batch_size=1000)

        assert written == 2500
        assert KlineRecord.objects.count() == 2500

    def test_empty_rows_returns_zero(self):
        assert upsert_klines([]) == 0
        assert KlineRecord.objects.count() == 0

    def test_invalid_batch_size_raises(self):
        with pytest.raises(ValueError):
            upsert_klines([_row(0)], batch_size=0)

    def test_distinct_timeframes_and_exchanges_coexist(self):
        """幂等键含 timeframe/exchange，同 symbol 同 timestamp 不互相覆盖。"""
        upsert_klines(
            [
                _row(0, timeframe="1h", close=1.0),
                _row(0, timeframe="4h", close=2.0),
                _row(0, exchange="okx", close=3.0),
            ]
        )
        assert KlineRecord.objects.count() == 3
        assert KlineRecord.objects.get(timeframe="1h", exchange="binance").close == 1.0
        assert KlineRecord.objects.get(timeframe="4h").close == 2.0
        assert KlineRecord.objects.get(exchange="okx").close == 3.0


class TestMigrationReversible:
    """迁移可正向 / 反向执行。"""

    def test_migration_forward_and_backward(self):
        executor = MigrationExecutor(connection)

        # 反向：0003 -> 0002，表应被删除
        executor.migrate([("datasource", "0002_datasourceconfig_market_types")])
        executor.loader.build_graph()
        assert KlineRecord._meta.db_table not in connection.introspection.table_names()

        # 正向：0002 -> 0003，表应重建
        executor.migrate([("datasource", "0003_klinerecord")])
        executor.loader.build_graph()
        assert KlineRecord._meta.db_table in connection.introspection.table_names()


class TestTimeframeToSeconds:
    def test_supported_timeframes(self):
        assert timeframe_to_seconds("15m") == 900
        assert timeframe_to_seconds("1h") == 3600
        assert timeframe_to_seconds("4h") == 14400
        assert timeframe_to_seconds("1d") == 86400
        assert timeframe_to_seconds("1w") == 604800
        # ccxt 约定 M=月（30 天），且大小写敏感
        assert timeframe_to_seconds("1M") == 2592000

    @pytest.mark.parametrize("bad", ["", "1", "h", "1x", "abc", "1h30m"])
    def test_invalid_timeframe_raises(self, bad):
        with pytest.raises(ValueError):
            timeframe_to_seconds(bad)


class TestNormalizeBinanceSymbol:
    def test_cases(self):
        assert normalize_binance_symbol("SOL/USDT") == "SOL/USDT"
        assert normalize_binance_symbol("SOLUSDT") == "SOL/USDT"
        assert normalize_binance_symbol("SOL-USDT") == "SOL/USDT"
        assert normalize_binance_symbol("SOL_USDT") == "SOL/USDT"
        assert normalize_binance_symbol("SOL") == "SOL/USDT"
        assert normalize_binance_symbol("btcusdt") == "BTC/USDT"

    def test_bare_quote_coin_not_split(self):
        # WBTC 不能被后缀规则切成 W/BTC
        assert normalize_binance_symbol("WBTC") == "WBTC/USDT"

    def test_futures_format_is_idempotent(self):
        assert normalize_binance_symbol("BTC/USDT:USDT") == "BTC/USDT:USDT"

    def test_empty(self):
        assert normalize_binance_symbol("") == ""

    def test_reexported_from_backtest_tasks(self):
        """搬迁后 backtest 侧 re-export 必须是同一个函数（既有调用方依赖）。"""
        from apps.backtest.tasks import normalize_binance_symbol as from_backtest

        assert from_backtest is normalize_binance_symbol


class TestBuildExchange:
    """代理注入 / rate limit 统一在共享层。"""

    @staticmethod
    def _captured_options(exchange_id: str = "binance", **kwargs):
        with patch("ccxt.async_support.binance") as mock_cls:
            build_exchange(exchange_id, **kwargs)
        return mock_cls.call_args[0][0]

    @override_settings(WEB_PROXY="http://host.docker.internal:7890")
    def test_proxy_injected_when_configured(self):
        assert (
            self._captured_options()["aiohttp_proxy"]
            == "http://host.docker.internal:7890"
        )

    @override_settings(WEB_PROXY="")
    def test_no_proxy_key_when_unset(self):
        assert "aiohttp_proxy" not in self._captured_options()

    @override_settings(WEB_PROXY="")
    def test_rate_limit_always_enabled(self):
        assert self._captured_options()["enableRateLimit"] is True

    @override_settings(WEB_PROXY="http://p:1")
    def test_extra_options_merged_and_proxy_not_dropped(self):
        options = self._captured_options(timeout=12345)
        assert options["timeout"] == 12345
        assert options["aiohttp_proxy"] == "http://p:1"
        assert options["enableRateLimit"] is True

    def test_unsupported_exchange_raises(self):
        with pytest.raises(ValueError):
            build_exchange("not_a_real_exchange_xyz")


class _FakeExchange:
    """最小 ccxt 替身：按 since 切片返回，语义与 fetch_ohlcv 一致。"""

    def __init__(self, candles: list):
        self.candles = candles
        self.calls: list = []

    async def fetch_ohlcv(self, symbol, timeframe, since=None, limit=1000):
        self.calls.append({"symbol": symbol, "timeframe": timeframe, "since": since})
        if since is None:
            start = 0
        else:
            start = next(
                (i for i, c in enumerate(self.candles) if c[0] >= since),
                len(self.candles),
            )
        return self.candles[start : start + limit]


def _candles(n: int, step_ms: int = 3_600_000, start_ms: int = 1_767_225_600_000):
    """n 根 1h K 线（start_ms = 2026-01-01T00:00:00Z）。"""
    return [
        [start_ms + i * step_ms, 100.0, 110.0, 90.0, 100.5 + i, 1.0] for i in range(n)
    ]


class TestFetchOhlcvPaged:
    def test_single_page_returns_all_rows(self):
        ex = _FakeExchange(_candles(5))
        rows = asyncio.run(fetch_ohlcv_paged(ex, "BTC/USDT:USDT", "1h"))

        assert len(rows) == 5
        assert len(ex.calls) == 1
        assert rows[0]["timestamp"] == datetime(2026, 1, 1, tzinfo=timezone.utc)
        assert rows[0]["timestamp"].tzinfo is not None
        assert rows[0]["close"] == 100.5
        assert isinstance(rows[0]["close"], float)

    def test_pagination_advances_and_loses_no_rows(self):
        ex = _FakeExchange(_candles(5))
        rows = asyncio.run(fetch_ohlcv_paged(ex, "BTC/USDT:USDT", "1h", limit=2))

        # 2 + 2 + 1：最后一页不足 limit 即收工
        assert len(ex.calls) == 3
        assert len(rows) == 5
        assert len({r["timestamp"] for r in rows}) == 5  # 无重复
        assert rows[-1]["close"] == 104.5

    def test_end_ms_filters_out_later_bars(self):
        candles = _candles(5)
        ex = _FakeExchange(candles)
        end_ms = candles[2][0]

        rows = asyncio.run(fetch_ohlcv_paged(ex, "BTC/USDT:USDT", "1h", end_ms=end_ms))

        assert len(rows) == 3
        assert rows[-1]["timestamp"] == datetime(2026, 1, 1, 2, tzinfo=timezone.utc)

    def test_since_ms_skips_earlier_bars(self):
        candles = _candles(5)
        ex = _FakeExchange(candles)

        rows = asyncio.run(
            fetch_ohlcv_paged(ex, "BTC/USDT:USDT", "1h", since_ms=candles[3][0])
        )

        assert len(rows) == 2

    def test_empty_response_returns_empty(self):
        ex = _FakeExchange([])
        assert asyncio.run(fetch_ohlcv_paged(ex, "BTC/USDT:USDT", "1h")) == []
