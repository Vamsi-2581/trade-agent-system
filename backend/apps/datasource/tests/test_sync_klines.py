"""sync_klines 增量任务 + 回填 command + beat 调度测试。

覆盖 #13 验收：mock ccxt 端到端（行数正确 / 重复执行幂等）、回填断流重跑不重复、
单 pair 失败隔离、beat 配置生效。

用 ``django_db(transaction=True)``：回填 command 经 ``db_async`` 在**另一个线程**里
落库，普通 ``django_db`` 的外层事务对那个连接不可见，断言会假阴性。
"""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from io import StringIO
from unittest.mock import patch

import pytest
from django.conf import settings
from django.core.management import call_command
from django.test import override_settings

from apps.datasource.models import KlineRecord
from apps.datasource.tasks import sync_klines

pytestmark = pytest.mark.django_db(transaction=True)

HOUR_MS = 3_600_000
SYMBOL_A = "BTC/USDT:USDT"
SYMBOL_B = "ETH/USDT:USDT"

# 冻结的「现在」：故意偏离 bar 边界 30 分钟，使 30 天窗口两端都不落在 bar 边界上，
# 回填行数因此是确定的 720（若窗口端恰好压在 bar 边界，含端点的区间会数出 721）。
_ALIGNED = datetime(2026, 6, 1, tzinfo=timezone.utc)
_FROZEN_NOW = _ALIGNED + timedelta(minutes=30)


@contextmanager
def _frozen_now(fixed: datetime = _FROZEN_NOW):
    """冻结 command 里的 ``timezone.now()``，让窗口划分可预期。"""
    with patch("django.utils.timezone.now", return_value=fixed):
        yield


class _FixedExchange:
    """按 ``since`` 切片返回固定 K 线序列；可让第 N 次调用起抛错（模拟断流）。"""

    def __init__(self, candles: list, fail_from_call: int | None = None):
        self.candles = candles
        self.calls: list = []
        self.fail_from_call = fail_from_call
        self.closed = False

    async def fetch_ohlcv(self, symbol, timeframe, since=None, limit=1000):
        self.calls.append({"symbol": symbol, "timeframe": timeframe, "since": since})
        if self.fail_from_call is not None and len(self.calls) >= self.fail_from_call:
            raise RuntimeError("mock stream broken")
        if since is None:
            start = 0
        else:
            start = next(
                (i for i, c in enumerate(self.candles) if c[0] >= since),
                len(self.candles),
            )
        return self.candles[start : start + limit]

    async def close(self):
        self.closed = True


class _GeneratingExchange:
    """模拟真实交易所：从 >= ``since`` 的第一个 step 对齐 bar 起，最多 ``limit`` 根。

    两条约束都是必需的，否则 ``fetch_ohlcv_paged`` 会死循环：

    - **向前对齐**（``base + (-base) % step``，绝不回退）：回退会让下一页拿到同一根
      bar，``batch_since`` 原地打转；
    - **在「现在」截断**：永远返回满页会让 ``end_ms=None`` 的增量任务看不到
      「不足一页」这个收工信号。
    """

    def __init__(
        self,
        step_ms: int = HOUR_MS,
        fail_from_call: int | None = None,
        now_ms: int | None = None,
    ):
        self.step_ms = step_ms
        self.calls: list = []
        self.fail_from_call = fail_from_call
        self.now_ms = (
            now_ms
            if now_ms is not None
            else int(datetime.now(timezone.utc).timestamp() * 1000)
        )

    async def fetch_ohlcv(self, symbol, timeframe, since=None, limit=1000):
        self.calls.append({"symbol": symbol, "timeframe": timeframe, "since": since})
        if self.fail_from_call is not None and len(self.calls) >= self.fail_from_call:
            raise RuntimeError("mock stream broken")
        base = since if since is not None else 0
        start = base + (-base) % self.step_ms  # 对齐到 >= base 的第一个边界
        return [
            [start + i * self.step_ms, 100.0, 110.0, 90.0, 100.0 + i, 1.0]
            for i in range(limit)
            if start + i * self.step_ms <= self.now_ms
        ]

    async def close(self):
        pass


def _fixed_candles(count: int, end_ms: int, step_ms: int = HOUR_MS) -> list:
    """count 根 bar，最后一根开盘时间为 end_ms 向下对齐的步长边界。"""
    last = end_ms - (end_ms % step_ms)
    start = last - (count - 1) * step_ms
    return [
        [start + i * step_ms, 100.0, 110.0, 90.0, 100.0 + i, 1.0] for i in range(count)
    ]


def _patch_build_exchange(exchange, target: str):
    return patch(target, return_value=exchange)


class TestSyncKlinesTask:
    """增量任务：行数、幂等、增量游标、失败隔离、配置来源。"""

    @override_settings(KLINE_SYNC_SYMBOLS=[SYMBOL_A], KLINE_SYNC_TIMEFRAMES=["1h"])
    def test_upserts_expected_rows_and_is_idempotent(self):
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        fake = _FixedExchange(_fixed_candles(5, now_ms))

        with _patch_build_exchange(fake, "apps.datasource.exchange.build_exchange"):
            first = sync_klines()
            assert KlineRecord.objects.count() == 5
            assert first["new_records"] == 5
            assert fake.closed is True  # finally 里关了交易所

            second = sync_klines()

        # 重复执行幂等：仍是 5 行，且第二轮的增量起点已推进 → 无新增
        assert KlineRecord.objects.count() == 5
        assert second["new_records"] == 0

    @override_settings(KLINE_SYNC_SYMBOLS=[SYMBOL_A], KLINE_SYNC_TIMEFRAMES=["1h"])
    def test_incremental_cursor_advances_to_latest_plus_1ms(self):
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        candles = _fixed_candles(5, now_ms)
        fake = _FixedExchange(candles)

        with _patch_build_exchange(fake, "apps.datasource.exchange.build_exchange"):
            sync_klines()
            fake.calls.clear()
            sync_klines()

        assert fake.calls[0]["since"] == candles[-1][0] + 1

    @override_settings(
        KLINE_SYNC_SYMBOLS=[SYMBOL_A],
        KLINE_SYNC_TIMEFRAMES=["1h"],
        KLINE_BACKFILL_DAYS=30,
    )
    def test_no_records_starts_from_backfill_days(self):
        fake = _FixedExchange([])
        expected = int(
            (datetime.now(timezone.utc) - timedelta(days=30)).timestamp() * 1000
        )

        with _patch_build_exchange(fake, "apps.datasource.exchange.build_exchange"):
            sync_klines()

        assert abs(fake.calls[0]["since"] - expected) < 10_000  # 容忍几秒执行漂移

    @override_settings(
        KLINE_SYNC_SYMBOLS=[SYMBOL_A, SYMBOL_B], KLINE_SYNC_TIMEFRAMES=["1h"]
    )
    def test_single_pair_failure_is_isolated_and_logged(self, caplog):
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        # 第 2 次调用（= 第二个 pair）起抛错
        fake = _FixedExchange(_fixed_candles(5, now_ms), fail_from_call=2)

        with caplog.at_level("ERROR"):
            with _patch_build_exchange(fake, "apps.datasource.exchange.build_exchange"):
                result = sync_klines()

        # 第一个 pair 正常落库，第二个失败但没拖垮整轮
        assert KlineRecord.objects.filter(symbol=SYMBOL_A).count() == 5
        assert KlineRecord.objects.filter(symbol=SYMBOL_B).count() == 0
        assert result["new_records"] == 5
        assert "fetch failed" in caplog.text
        assert SYMBOL_B in caplog.text

    @override_settings(
        KLINE_SYNC_SYMBOLS=[SYMBOL_A, SYMBOL_B],
        KLINE_SYNC_TIMEFRAMES=["15m", "1h"],
    )
    def test_pairs_come_from_settings(self):
        fake = _FixedExchange([])

        with _patch_build_exchange(fake, "apps.datasource.exchange.build_exchange"):
            result = sync_klines()

        assert result["pairs"] == 4  # 2 symbols × 2 timeframes
        assert {(c["symbol"], c["timeframe"]) for c in fake.calls} == {
            (SYMBOL_A, "15m"),
            (SYMBOL_A, "1h"),
            (SYMBOL_B, "15m"),
            (SYMBOL_B, "1h"),
        }

    @override_settings(KLINE_SYNC_SYMBOLS=[SYMBOL_A], KLINE_SYNC_TIMEFRAMES=["1h"])
    def test_explicit_args_override_settings(self):
        fake = _FixedExchange([])

        with _patch_build_exchange(fake, "apps.datasource.exchange.build_exchange"):
            sync_klines(symbols=["SOL/USDT:USDT"], timeframes=["4h"])

        assert fake.calls[0]["symbol"] == "SOL/USDT:USDT"
        assert fake.calls[0]["timeframe"] == "4h"


class TestBeatSchedule:
    def test_sync_klines_registered_with_settings_interval(self):
        from celery_app import app

        entry = app.conf.beat_schedule["sync-klines"]
        assert entry["task"] == "apps.datasource.tasks.sync_klines"
        # 间隔与 settings 同源，避免两处硬编码漂移
        assert entry["schedule"] == settings.KLINE_SYNC_INTERVAL

    def test_funding_sync_symbols_moved_to_settings(self):
        """标的集合单一来源：资金费率同步也读 settings（spec §8/§9）。"""
        assert settings.FUNDING_SYNC_SYMBOLS == settings.KLINE_SYNC_SYMBOLS

        import apps.backtest.tasks as bt

        assert not hasattr(bt, "FUNDING_SYNC_SYMBOLS")


class TestBackfillCommand:
    """回填 command：行数、按窗口落库、断流后重跑不重复。"""

    NOW_MS = int(_FROZEN_NOW.timestamp() * 1000)

    def _run(self, *args) -> str:
        out = StringIO()
        call_command("sync_klines", *args, stdout=out)
        return out.getvalue()

    def _args(self, *extra) -> tuple:
        return (
            "--backfill",
            "--days",
            "30",
            "--symbol",
            SYMBOL_A,
            "--timeframe",
            "1h",
            *extra,
        )

    def test_backfill_days_30_row_count(self):
        """30 天 × 24 根/天 = 720 根 1h bar。"""
        fake = _GeneratingExchange(step_ms=HOUR_MS, now_ms=self.NOW_MS)

        with _frozen_now():
            with _patch_build_exchange(
                fake, "apps.datasource.management.commands.sync_klines.build_exchange"
            ):
                self._run(*self._args("--window-days", "30"))

        assert KlineRecord.objects.count() == 720

    def test_backfill_rerun_after_break_does_not_duplicate(self):
        # 15 天/片 × 30 天 = 2 片；第 2 次调用（= 第 2 片）断流
        broken = _GeneratingExchange(
            step_ms=HOUR_MS, fail_from_call=2, now_ms=self.NOW_MS
        )
        args = self._args("--window-days", "15")

        with _frozen_now():
            with _patch_build_exchange(
                broken, "apps.datasource.management.commands.sync_klines.build_exchange"
            ):
                with pytest.raises(RuntimeError):
                    self._run(*args)

        # 第 1 片已落库（每片拉完立即写）→ 断流不丢已完成进度
        assert KlineRecord.objects.count() == 360

        # 重跑：健康交易所；第 1 片重拉但幂等，第 2 片补齐
        healthy = _GeneratingExchange(step_ms=HOUR_MS, now_ms=self.NOW_MS)
        with _frozen_now():
            with _patch_build_exchange(
                healthy,
                "apps.datasource.management.commands.sync_klines.build_exchange",
            ):
                out = self._run(*args)

        assert KlineRecord.objects.count() == 720
        assert "汇总" in out

    def test_backfill_pages_when_limit_is_small(self):
        """limit 小于窗口 bar 数时必须翻页，且不重不漏。"""
        fake = _GeneratingExchange(step_ms=HOUR_MS, now_ms=self.NOW_MS)

        with _frozen_now():
            with _patch_build_exchange(
                fake, "apps.datasource.management.commands.sync_klines.build_exchange"
            ):
                self._run(*self._args("--window-days", "30", "--limit", "100"))

        assert KlineRecord.objects.count() == 720
        assert len(fake.calls) > 1  # 确实翻页了

    def test_backfill_is_idempotent_when_rerun_clean(self):
        fake = _GeneratingExchange(step_ms=HOUR_MS, now_ms=self.NOW_MS)
        args = self._args("--window-days", "30")

        with _frozen_now():
            with _patch_build_exchange(
                fake, "apps.datasource.management.commands.sync_klines.build_exchange"
            ):
                self._run(*args)
                self._run(*args)

        assert KlineRecord.objects.count() == 720

    @override_settings(KLINE_BACKFILL_DAYS=1)
    def test_without_backfill_runs_incremental_once(self):
        fake = _GeneratingExchange(step_ms=HOUR_MS)

        with _patch_build_exchange(fake, "apps.datasource.exchange.build_exchange"):
            out = self._run("--symbol", SYMBOL_A, "--timeframe", "1h")

        assert "[KlineSync]" in out
        assert KlineRecord.objects.count() == 24  # 1 天 × 24 根/天
