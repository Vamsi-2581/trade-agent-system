"""BTC 日线落库与拉取（第①段单元 1）。

这一组测试里真正重要的是三条不变量，其余都是在钉住它们不被重构悄悄丢掉：

1. **失败与「没有新 K 线」可区分**——失败抛 `CandleFetchError`，空列表只可能来自
   一次成功的、交易所说「没有」的调用。
2. **正在形成中的日线不落库**——判定任务排在 UTC 00:00，那一刻最后一根日线刚
   开盘，写进去就是一根零幅假 K 线。
3. **幂等**——重复同步不产生新行、不改写未变行；这条依赖「解析值与列精度一致」，
   所以量化也得单独测。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from apps.regime import config
from apps.regime.candles import (
    CandleFetchError,
    fetch_daily_candles,
    latest_complete_date,
    sync_daily_candles,
)
from apps.regime.models import DailyCandle

# 一个固定的「现在」：判定任务的真实触发时刻是 UTC 00:00（北京 08:00），
# 刻意选这个时刻测，边界 bug 才不会被一个中午的时间戳掩盖。
NOW = datetime(2026, 9, 22, 0, 0, tzinfo=timezone.utc)
TODAY = NOW.date()  # 2026-09-22，日线刚开盘


def ms_of(day: date) -> int:
    return int(
        datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp() * 1000
    )


def row(day: date, *, o=100.0, h=110.0, low=90.0, c=105.0, v=1000.0):
    return [ms_of(day), o, h, low, c, v]


class FakePages:
    """按 since 返回预设页的取数替身：记录每次调用参数，便于断言分页游标。"""

    def __init__(self, pages):
        self._pages = list(pages)
        self.calls: list[tuple] = []

    def __call__(self, symbol, timeframe, since_ms, limit):
        self.calls.append((symbol, timeframe, since_ms, limit))
        return self._pages.pop(0) if self._pages else []


class TestFailureIsDistinguishableFromEmpty(TestCase):
    """CONTEXT.md：判定失败/数据缺失时保持上一有效状态 + 告警。

    这条决策在数据层的前提就是本节：拿不到数据必须是**异常**，不能是一个空列表。
    仓库里既有的两条拉取路径都在异常时 `return []`，日线不能再走那条老路。
    """

    def test_page_fetcher_failure_propagates_as_candle_fetch_error(self):
        def boom(*a, **k):
            raise CandleFetchError("连接超时")

        with self.assertRaises(CandleFetchError):
            fetch_daily_candles(
                start=date(2026, 1, 1), end=date(2026, 1, 3), fetch_page=boom
            )

    def test_shipped_ccxt_path_raises_instead_of_returning_empty(self):
        """默认取数实现（ccxt）自身也必须抛——测替身不算数，线上跑的是它。"""

        class BoomExchange:
            def __init__(self, options):
                pass

            async def fetch_ohlcv(self, *a, **k):
                raise ConnectionError("dns 解析失败")

            async def close(self):
                pass

        with patch("ccxt.async_support.binance", BoomExchange):
            with self.assertRaises(CandleFetchError) as ctx:
                fetch_daily_candles(start=date(2026, 1, 1), end=date(2026, 1, 3))

        self.assertIn("ConnectionError", str(ctx.exception))

    def test_a_genuinely_empty_answer_is_not_an_error(self):
        """交易所说「这段时间没有 K 线」是可信结论，不是失败。"""
        result = fetch_daily_candles(
            start=date(2026, 1, 1),
            end=date(2026, 1, 3),
            fetch_page=FakePages([[]]),
            now=NOW,
        )
        self.assertEqual(result.candles, ())
        self.assertEqual(result.fetched, 0)

    def test_command_exits_nonzero_on_fetch_failure(self):
        from django.core.management.base import CommandError

        with patch(
            "apps.regime.candles._ccxt_fetch_page",
            side_effect=CandleFetchError("限频"),
        ):
            with self.assertRaises(CommandError):
                call_command(
                    "backfill_daily_candles",
                    "--start",
                    "2026-01-01",
                    "--end",
                    "2026-01-03",
                )


class TestIncompleteCandleIsNeverPersisted(TestCase):
    """北京 08:00 = UTC 00:00，此刻「今天」那根日线刚开盘。"""

    def test_newest_forming_candle_is_dropped(self):
        result = fetch_daily_candles(
            start=TODAY - timedelta(days=1),
            end=TODAY,
            fetch_page=FakePages([[row(TODAY - timedelta(days=1)), row(TODAY)]]),
            now=NOW,
        )

        self.assertEqual([c.date for c in result.candles], [TODAY - timedelta(days=1)])
        self.assertEqual(result.incomplete, 1)

    def test_just_closed_candle_is_included_at_the_boundary(self):
        """日线 D-1 恰在 UTC 00:00 收盘；判定任务就在那一刻跑，不能漏掉它。"""
        self.assertEqual(latest_complete_date(NOW), TODAY - timedelta(days=1))

        result = fetch_daily_candles(
            start=TODAY - timedelta(days=1),
            end=TODAY - timedelta(days=1),
            fetch_page=FakePages([[row(TODAY - timedelta(days=1))]]),
            now=NOW,
        )
        self.assertEqual(result.accepted, 1)

    def test_completeness_rule_agrees_with_latest_complete_date(self):
        """两条写法必须同一条规则，否则「续拉起点」与「取数过滤」会各说各话。"""
        for offset in (0, 1, 5, 60, 1439, 1440, 1441):
            now = NOW + timedelta(minutes=offset)
            expected = latest_complete_date(now)
            days = [
                expected - timedelta(days=1),
                expected,
                expected + timedelta(days=1),
            ]
            result = fetch_daily_candles(
                start=days[0],
                end=days[-1],
                fetch_page=FakePages([[row(d) for d in days]]),
                now=now,
            )
            self.assertEqual(
                [c.date for c in result.candles],
                [d for d in days if d <= expected],
                f"offset={offset}",
            )


class TestCandleValidation(TestCase):
    def test_candle_not_opening_at_utc_midnight_is_rejected(self):
        """`date` 是由开盘时刻导出的，所以「日线在 UTC 零点开盘」是必须校验的前提。"""
        bad = row(TODAY)
        bad[0] = bad[0] + 3 * 3_600_000

        with self.assertRaises(CandleFetchError) as ctx:
            fetch_daily_candles(
                start=TODAY,
                end=TODAY,
                fetch_page=FakePages([[bad]]),
                now=NOW + timedelta(days=2),
            )
        self.assertIn("UTC 零点", str(ctx.exception))

    def test_inconsistent_ohlc_is_rejected(self):
        with self.assertRaises(CandleFetchError):
            fetch_daily_candles(
                start=TODAY - timedelta(days=1),
                end=TODAY - timedelta(days=1),
                fetch_page=FakePages(
                    [[row(TODAY - timedelta(days=1), h=80.0, low=90.0)]]
                ),
                now=NOW,
            )

    def test_high_must_cover_open_and_close(self):
        with self.assertRaises(CandleFetchError):
            fetch_daily_candles(
                start=TODAY - timedelta(days=1),
                end=TODAY - timedelta(days=1),
                fetch_page=FakePages(
                    [[row(TODAY - timedelta(days=1), h=101.0, c=105.0)]]
                ),
                now=NOW,
            )

    def test_short_row_is_rejected(self):
        with self.assertRaises(CandleFetchError):
            fetch_daily_candles(
                start=TODAY - timedelta(days=1),
                end=TODAY - timedelta(days=1),
                fetch_page=FakePages([[[ms_of(TODAY - timedelta(days=1)), 1, 2, 3]]]),
                now=NOW,
            )


class TestPagination(TestCase):
    def test_cursor_advances_from_last_open_time(self):
        with patch.object(config, "CANDLES", replace(config.CANDLES, page_limit=2)):
            days = [TODAY - timedelta(days=n) for n in (3, 2, 1)]
            pages = FakePages(
                [
                    [row(days[0]), row(days[1])],  # 第一页满 → 继续
                    [row(days[2])],  # 第二页不满 → 停止
                ]
            )
            result = fetch_daily_candles(
                start=days[0],
                end=days[2],
                fetch_page=pages,
                now=NOW,
            )

        self.assertEqual([c.date for c in result.candles], sorted(days))
        self.assertEqual(pages.calls[0][2], ms_of(days[0]))
        self.assertEqual(
            pages.calls[1][2],
            ms_of(days[1]) + 1,
            "下一页必须从上一页最后一根的开盘时刻 +1ms 起",
        )

    def test_stuck_cursor_raises_instead_of_looping_forever(self):
        """交易所忽略 since 时会原地返回同一页；不设防就是死循环。"""

        def stuck(symbol, timeframe, since_ms, limit):
            return [row(TODAY - timedelta(days=30 + i)) for i in range(limit)]

        with patch.object(config, "CANDLES", replace(config.CANDLES, page_limit=3)):
            with self.assertRaises(CandleFetchError) as ctx:
                fetch_daily_candles(
                    start=TODAY - timedelta(days=40),
                    end=TODAY,
                    fetch_page=stuck,
                    now=NOW,
                )
        self.assertIn("游标未推进", str(ctx.exception))


class TestSyncIsIdempotent(TestCase):
    def _sync(self, days, **kw):
        return sync_daily_candles(
            start=min(days),
            end=max(days),
            fetch_page=FakePages([[row(d) for d in sorted(days)]]),
            now=NOW,
            **kw,
        )

    def test_second_run_writes_nothing(self):
        days = [TODAY - timedelta(days=n) for n in (2, 1)]

        first = self._sync(days)
        second = self._sync(days)

        self.assertEqual((first.created, first.updated), (2, 0))
        self.assertEqual((second.created, second.updated, second.unchanged), (0, 0, 2))
        self.assertEqual(DailyCandle.objects.count(), 2)

    def test_changed_value_is_updated_in_place(self):
        day = TODAY - timedelta(days=1)
        self._sync([day])

        changed = sync_daily_candles(
            start=day,
            end=day,
            fetch_page=FakePages([[row(day, h=200.0, c=123.0)]]),
            now=NOW,
        )

        self.assertEqual((changed.created, changed.updated), (0, 1))
        self.assertEqual(DailyCandle.objects.count(), 1)
        self.assertEqual(DailyCandle.objects.get().close, Decimal("123.00000000"))

    def test_values_are_quantized_to_the_column_scale(self):
        """不量化 → 读回的值 ≠ 刚解析的值 → 每次同步都「更新」同一批行。

        这里用一个 15 位有效数字的价格（float 解析的典型产物）验证落库后稳定。
        """
        day = TODAY - timedelta(days=1)
        noisy = row(day, h=70000.0, c=62345.123456789012)
        sync_daily_candles(start=day, end=day, fetch_page=FakePages([[noisy]]), now=NOW)
        stored = DailyCandle.objects.get().close
        self.assertEqual(stored, Decimal("62345.12345679"))

        again = sync_daily_candles(
            start=day, end=day, fetch_page=FakePages([[noisy]]), now=NOW
        )
        self.assertEqual((again.created, again.updated, again.unchanged), (0, 0, 1))

    def test_date_matches_open_time(self):
        hour = TODAY - timedelta(days=1)
        self._sync([hour])

        candle = DailyCandle.objects.get()
        self.assertTrue(candle.open_time.tzinfo is not None)
        self.assertEqual(candle.date, candle.open_time.astimezone(timezone.utc).date())


class TestIncrementalSync(TestCase):
    def test_nothing_new_makes_no_network_call(self):
        days = [TODAY - timedelta(days=2), TODAY - timedelta(days=1)]
        sync_daily_candles(
            start=min(days),
            end=max(days),
            fetch_page=FakePages([[row(d) for d in days]]),
            now=NOW,
        )

        pages = FakePages([])  # 被调用就会 IndexError/产生 calls
        report = sync_daily_candles(fetch_page=pages, now=NOW)

        self.assertEqual(pages.calls, [], "库里已到最新已收盘日时不该发起请求")
        self.assertEqual(report.wrote, 0)
        self.assertIn("无新数据", report.summary())

    def test_increment_resumes_from_last_stored_day(self):
        first = TODAY - timedelta(days=3)
        sync_daily_candles(
            start=first,
            end=first,
            fetch_page=FakePages([[row(first)]]),
            now=NOW,
        )

        pages = FakePages([[row(TODAY - timedelta(days=1))]])
        report = sync_daily_candles(fetch_page=pages, now=NOW)

        self.assertEqual(pages.calls[0][2], ms_of(first + timedelta(days=1)))
        self.assertEqual(report.created, 1)

    def test_cold_start_without_start_is_an_error(self):
        """冷启动回填窗口是人为决定，代码不替它挑一个魔数。"""
        with self.assertRaises(ValueError) as ctx:
            sync_daily_candles(fetch_page=FakePages([]), now=NOW)
        self.assertIn("冷启动", str(ctx.exception))

    def test_naive_now_is_rejected(self):
        with self.assertRaises(ValueError):
            sync_daily_candles(start=date(2026, 1, 1), now=datetime(2026, 9, 22))


class TestBackfillCommand(TestCase):
    def test_dry_run_writes_nothing(self):
        day = TODAY - timedelta(days=1)
        with patch(
            "apps.regime.candles._ccxt_fetch_page",
            return_value=[row(day)],
        ):
            call_command(
                "backfill_daily_candles",
                "--start",
                day.isoformat(),
                "--end",
                day.isoformat(),
                "--dry-run",
            )

        self.assertEqual(DailyCandle.objects.count(), 0)

    def test_backfill_then_rerun_is_a_no_op(self):
        day = TODAY - timedelta(days=1)
        with patch(
            "apps.regime.candles._ccxt_fetch_page", return_value=[row(day)]
        ) as fetch:
            call_command(
                "backfill_daily_candles",
                "--start",
                day.isoformat(),
                "--end",
                day.isoformat(),
            )
            self.assertEqual(DailyCandle.objects.count(), 1)

            # 第二次运行：库里的最后一根已经就是终点 → 一次请求都不该发。
            # 显式给 --end 而非依赖「现在」：否则这条用例的结果会随真实时钟漂移
            # （跑到明天，续拉起点又落进窗口里，请求就发了）。
            call_command("backfill_daily_candles", "--end", day.isoformat())

        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(DailyCandle.objects.count(), 1)

    def test_bad_date_format_is_a_command_error(self):
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            call_command("backfill_daily_candles", "--start", "2026/01/01")


class TestConfigAgreesWithTheRestOfTheRepo(TestCase):
    def test_timeframe_literal_matches_the_datasource_enum(self):
        """config 里写的是字面量 "1d"（刻意不 import datasource，免得为了一根字符串
        把它的模块图拉进本 app）。字面量会漂移，所以在这里对一次账。"""
        from apps.datasource.base import KlineInterval

        self.assertEqual(config.CANDLES.timeframe, KlineInterval.D1.value)

    def test_page_limit_fits_a_single_ccxt_page(self):
        """分页上限只影响往返次数，不影响正确性；但超过交易所单页上限会被静默截断。"""
        self.assertLessEqual(config.CANDLES.page_limit, 1000)


class TestRangeFiltering(TestCase):
    def test_rows_outside_the_requested_range_are_counted_not_stored(self):
        """分页最后一页会带回区间外的根；它们既不该落库，也不该被静默忽略。"""
        days = [TODAY - timedelta(days=n) for n in (3, 2, 1)]
        result = fetch_daily_candles(
            start=days[0],
            end=days[1],
            fetch_page=FakePages([[row(d) for d in days]]),
            now=NOW,
        )

        self.assertEqual([c.date for c in result.candles], [days[0], days[1]])
        self.assertEqual(result.out_of_range, 1)
