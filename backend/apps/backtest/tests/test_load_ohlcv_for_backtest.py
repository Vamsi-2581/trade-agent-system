"""回测 OHLCV 获取（库优先 + 缺口回退）与 data_source 暴露测试。

覆盖 #14 验收：库完备零 API 调用、头/尾/中间缺口补拉+回写+合并去重、库全空全量
拉取、未收盘 bar 排除、资金费率覆盖告警（不改失败行为）、data_source 经 serializer
可读。

区间约定：``start_date=2026-01-01`` / ``end_date=2026-01-02`` → 有效 bar 集合是
``timestamp + 1h <= 2026-01-02T00:00Z``，即 0h..23h 共 24 根（end_dt 那根算未收盘，
不进回测）。
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest

from apps.backtest import tasks
from apps.backtest.models import BacktestResult, FundingRateRecord
from apps.backtest.serializers import (
    BacktestDetailSerializer,
    BacktestResultSerializer,
)
from apps.datasource.models import KlineRecord
from apps.trading.models import Strategy

pytestmark = pytest.mark.django_db(transaction=True)

SYMBOL = "BTC/USDT:USDT"
TF = "1h"
STEP = timedelta(hours=1)
STEP_MS = 3_600_000
START = datetime(2026, 1, 1, tzinfo=timezone.utc)
END = datetime(2026, 1, 2, tzinfo=timezone.utc)  # 次日 0 点
START_DATE = "2026-01-01"
END_DATE = "2026-01-02"
FULL_BARS = 24  # 0h..23h


def _bar(i: int) -> datetime:
    return START + i * STEP


def _ohlcv_at(i: int) -> dict:
    return {
        "timestamp": _bar(i).isoformat(),
        "open": 100.0,
        "high": 110.0,
        "low": 90.0,
        "close": 200.0,
        "volume": 1.0,
    }


def _seed(indices) -> None:
    KlineRecord.objects.bulk_create(
        [
            KlineRecord(
                exchange="binance",
                symbol=SYMBOL,
                timeframe=TF,
                timestamp=_bar(i),
                open=100.0,
                high=110.0,
                low=90.0,
                close=100.0 + i,
                volume=1.0,
            )
            for i in indices
        ]
    )


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


class _FetchSpy:
    """替换 ``_fetch_ohlcv_sync``：按 ``[since_ms, end_ms]`` 生成 bar 并记录调用。

    ``since_ms`` / ``end_ms`` 缺失时（空日期回退路径）返回 ``fixed``。
    """

    def __init__(self, fixed: list | None = None):
        self.calls: list[dict] = []
        self.fixed = fixed or []

    def __call__(
        self,
        symbol,
        timeframe,
        exchange="binance",
        limit=1000,
        start_date="",
        end_date="",
        since_ms=None,
        end_ms=None,
    ):
        self.calls.append(
            {
                "symbol": symbol,
                "timeframe": timeframe,
                "exchange": exchange,
                "start_date": start_date,
                "end_date": end_date,
                "since_ms": since_ms,
                "end_ms": end_ms,
            }
        )
        if since_ms is None or end_ms is None:
            return list(self.fixed)
        count = (end_ms - since_ms) // STEP_MS + 1
        return [
            {
                "timestamp": datetime.fromtimestamp(
                    (since_ms + i * STEP_MS) / 1000, tz=timezone.utc
                ).isoformat(),
                "open": 100.0,
                "high": 110.0,
                "low": 90.0,
                "close": 200.0,
                "volume": 1.0,
            }
            for i in range(count)
        ]


def _load(spy: _FetchSpy, *args):
    with patch.object(tasks, "_fetch_ohlcv_sync", spy):
        return tasks._load_ohlcv_for_backtest(*args)


class TestCompleteDatabase:
    def test_zero_api_calls_when_db_complete(self):
        _seed(range(FULL_BARS))
        spy = _FetchSpy()

        data, stats = _load(spy, SYMBOL, TF, START_DATE, END_DATE)

        assert spy.calls == []  # 库完备 → 零 API 调用
        assert len(data) == FULL_BARS
        assert stats == {"from_db": FULL_BARS, "from_api": 0, "gaps": []}

    def test_unclosed_bar_excluded_and_not_treated_as_gap(self):
        # 库里多一根落在 end_dt 的 bar（收盘时刻 > end）→ 不进返回数据
        _seed(range(FULL_BARS + 1))  # 0h..24h
        spy = _FetchSpy()

        data, stats = _load(spy, SYMBOL, TF, START_DATE, END_DATE)

        assert len(data) == FULL_BARS
        assert data[-1]["timestamp"] == _bar(FULL_BARS - 1).isoformat()
        # 未收盘 bar 不算缺口，否则每轮都会白拉一次
        assert spy.calls == []
        assert stats["gaps"] == []

    def test_output_is_sorted_ascending(self):
        _seed(reversed(range(FULL_BARS)))
        spy = _FetchSpy()

        data, _ = _load(spy, SYMBOL, TF, START_DATE, END_DATE)

        stamps = [r["timestamp"] for r in data]
        assert stamps == sorted(stamps)


class TestGapFill:
    def test_tail_gap_fills_writes_back_and_merges(self):
        _seed(range(19))  # 0h..18h，缺 19h..23h（5 根）
        spy = _FetchSpy()

        data, stats = _load(spy, SYMBOL, TF, START_DATE, END_DATE)

        assert len(spy.calls) == 1
        assert spy.calls[0]["since_ms"] == _ms(_bar(19))
        assert spy.calls[0]["end_ms"] == _ms(END)
        assert len(data) == FULL_BARS
        assert stats["from_db"] == 19
        assert stats["from_api"] == 5
        # 回写生效：19 根 → 25 根（补拉含 end_ms 端点那根；它不在回测窗口内，
        # 但确实是真实 bar，落库无害）
        assert KlineRecord.objects.filter(symbol=SYMBOL, timeframe=TF).count() == 25

    def test_head_gap_fills(self):
        _seed(range(10, FULL_BARS))  # 10h..23h，缺 0h..9h（10 根）
        spy = _FetchSpy()

        data, stats = _load(spy, SYMBOL, TF, START_DATE, END_DATE)

        assert len(spy.calls) == 1
        assert spy.calls[0]["since_ms"] == _ms(START)
        assert spy.calls[0]["end_ms"] == _ms(_bar(9))
        assert len(data) == FULL_BARS
        assert stats["from_db"] == 14
        assert stats["from_api"] == 10

    def test_middle_gap_fills_and_dedupes(self):
        _seed(list(range(8)) + list(range(11, FULL_BARS)))  # 断 8h..10h（3 根）
        spy = _FetchSpy()

        data, stats = _load(spy, SYMBOL, TF, START_DATE, END_DATE)

        assert len(spy.calls) == 1
        assert spy.calls[0]["since_ms"] == _ms(_bar(8))
        assert spy.calls[0]["end_ms"] == _ms(_bar(10))
        assert len(data) == FULL_BARS
        assert stats["from_db"] == 21
        assert stats["from_api"] == 3
        stamps = [r["timestamp"] for r in data]
        assert len(stamps) == len(set(stamps))  # 合并按 timestamp 去重

    def test_second_run_makes_zero_api_calls(self):
        """缺口补一次即收敛：第二轮不再调 API（不会每轮报同一个尾部缺口）。"""
        _seed(range(19))
        spy = _FetchSpy()

        with patch.object(tasks, "_fetch_ohlcv_sync", spy):
            tasks._load_ohlcv_for_backtest(SYMBOL, TF, START_DATE, END_DATE)
            assert len(spy.calls) == 1
            data2, stats2 = tasks._load_ohlcv_for_backtest(
                SYMBOL, TF, START_DATE, END_DATE
            )

        assert len(spy.calls) == 1
        assert stats2["gaps"] == []
        assert stats2["from_api"] == 0
        assert len(data2) == FULL_BARS

    def test_empty_db_fetches_full_range_and_writes_back(self):
        spy = _FetchSpy()

        data, stats = _load(spy, SYMBOL, TF, START_DATE, END_DATE)

        assert len(spy.calls) == 1
        assert spy.calls[0]["since_ms"] == _ms(START)
        assert spy.calls[0]["end_ms"] == _ms(END)
        assert stats["from_db"] == 0
        assert stats["from_api"] == FULL_BARS  # end_dt 那根被未收盘规则滤掉
        assert len(data) == FULL_BARS
        assert (
            KlineRecord.objects.filter(symbol=SYMBOL, timeframe=TF).count()
            == FULL_BARS + 1
        )

    def test_empty_dates_fall_back_to_full_api(self):
        """日期为空 → 无法界定区间与缺口 → 退回全量 API（保持既有行为）。"""
        spy = _FetchSpy(fixed=[_ohlcv_at(0), _ohlcv_at(1), _ohlcv_at(2)])

        data, stats = _load(spy, SYMBOL, TF)

        assert len(spy.calls) == 1
        assert spy.calls[0]["start_date"] == ""
        assert spy.calls[0]["end_date"] == ""
        assert len(data) == 3
        assert stats == {"from_db": 0, "from_api": 3, "gaps": []}

    def test_fetch_failure_leaves_db_rows_usable(self):
        """补拉失败（返回空）不应吞掉库里已有的 bar。"""
        _seed(range(19))
        spy = _FetchSpy()

        def _failing(*args, **kwargs):
            spy(*args, **kwargs)
            return []

        with patch.object(tasks, "_fetch_ohlcv_sync", _failing):
            data, stats = tasks._load_ohlcv_for_backtest(
                SYMBOL, TF, START_DATE, END_DATE
            )

        assert len(data) == 19  # 库里的 bar 照常返回
        assert stats["from_db"] == 19
        assert stats["from_api"] == 0
        assert stats["gaps"]  # 缺口如实记录，便于排查


class TestDateParsing:
    """日期按 UTC 解释——naive 的 ``.timestamp()`` 会按本机时区偏移（本机 CST+8）。"""

    def test_parse_date_utc_is_utc_not_local(self):
        dt = tasks._parse_date_utc("2026-01-31")
        assert dt == datetime(2026, 1, 31, tzinfo=timezone.utc)
        assert tasks._date_to_epoch_ms("2026-01-31", "end_date") == 1769817600000

    def test_invalid_date_returns_none(self):
        assert tasks._parse_date_utc("not-a-date") is None
        assert tasks._date_to_epoch_ms("not-a-date", "start_date") is None


class TestFundingCoverageWarning:
    def _window(self) -> list[dict]:
        # 30 天窗口：0h → 720h
        return [_ohlcv_at(0), _ohlcv_at(24 * 30)]

    def test_missing_funding_warns_and_behavior_unchanged(self, caplog):
        data = self._window()

        with caplog.at_level("WARNING"):
            tasks._warn_funding_coverage(SYMBOL, data)
            rates = tasks.load_funding_rates(SYMBOL, data)

        assert "funding rate coverage gap" in caplog.text
        assert SYMBOL in caplog.text
        assert rates == {}  # 一期不改失败行为：缺费率仍是空 dict

    def test_full_coverage_does_not_warn(self, caplog):
        data = self._window()
        FundingRateRecord.objects.bulk_create(
            [
                FundingRateRecord(symbol=SYMBOL, funding_time=_bar(8 * k), rate=0.0001)
                for k in range(91)
            ]
        )

        with caplog.at_level("WARNING"):
            tasks._warn_funding_coverage(SYMBOL, data)

        assert "funding rate coverage gap" not in caplog.text

    def test_empty_ohlcv_is_noop(self, caplog):
        with caplog.at_level("WARNING"):
            tasks._warn_funding_coverage(SYMBOL, [])
        assert "funding rate coverage gap" not in caplog.text


class TestDataSourceSerializerExposure:
    def _make_result(self, data_source: dict) -> BacktestResult:
        strategy = Strategy.objects.create(name="T", code_path="strategies/t.py")
        return BacktestResult.objects.create(
            strategy=strategy,
            symbol=SYMBOL,
            timeframe=TF,
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 2),
            initial_capital=Decimal("10000.00"),
            final_capital=Decimal("10000.00"),
            total_return_pct=0.0,
            data_source=data_source,
        )

    def test_result_serializer_exposes_data_source(self):
        stats = {
            "from_db": 19,
            "from_api": 5,
            "gaps": [["2026-01-01T19:00:00+00:00", "2026-01-02T00:00:00+00:00"]],
        }
        result = self._make_result(stats)
        assert BacktestResultSerializer(result).data["data_source"] == stats

    def test_detail_serializer_exposes_data_source(self):
        stats = {"from_db": 24, "from_api": 0, "gaps": []}
        result = self._make_result(stats)
        assert BacktestDetailSerializer(result).data["data_source"] == stats

    def test_default_is_empty_dict(self):
        result = self._make_result({})
        assert BacktestResultSerializer(result).data["data_source"] == {}
