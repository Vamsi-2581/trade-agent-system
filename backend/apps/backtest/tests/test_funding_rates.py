"""Ticket 002: FundingRateRecord model + sync task idempotency."""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from django.db import IntegrityError
from django.test import override_settings
from django.utils import timezone as dj_tz

from apps.backtest.models import FundingRateRecord

pytestmark = pytest.mark.django_db


def _dt(y, m, d, h, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=timezone.utc)


class TestFundingRateRecordModel:
    def test_create_and_str(self):
        rec = FundingRateRecord.objects.create(
            symbol="BTC/USDT:USDT",
            funding_time=_dt(2024, 1, 1, 8),
            rate=0.0001,
        )
        assert rec.pk is not None
        assert "BTC" in str(rec)

    def test_unique_symbol_funding_time(self):
        FundingRateRecord.objects.create(
            symbol="BTC/USDT:USDT",
            funding_time=_dt(2024, 1, 1, 8),
            rate=0.0001,
        )
        with pytest.raises(IntegrityError):
            FundingRateRecord.objects.create(
                symbol="BTC/USDT:USDT",
                funding_time=_dt(2024, 1, 1, 8),
                rate=0.0002,
            )

    def test_same_time_different_symbols_ok(self):
        FundingRateRecord.objects.create(
            symbol="BTC/USDT:USDT",
            funding_time=_dt(2024, 1, 1, 8),
            rate=0.0001,
        )
        FundingRateRecord.objects.create(
            symbol="ETH/USDT:USDT",
            funding_time=_dt(2024, 1, 1, 8),
            rate=0.0001,
        )
        assert FundingRateRecord.objects.count() == 2


class TestSyncFundingRates:
    def test_incremental_sync_uses_latest_timestamp(self):
        """Sync queries from latest existing funding_time + 1ms."""
        from apps.backtest import tasks as bt

        FundingRateRecord.objects.create(
            symbol="BTC/USDT:USDT",
            funding_time=_dt(2024, 1, 1, 8),
            rate=0.0001,
        )

        fetched = [
            {"dt": _dt(2024, 1, 9, 16), "rate": 0.0002},
        ]
        with patch.object(
            bt, "_fetch_funding_history", new=AsyncMock(return_value=fetched)
        ) as m:
            result = bt.sync_funding_rates(["BTC/USDT:USDT"])

        # since = latest + 1ms
        assert m.call_args.args[2] == int(_dt(2024, 1, 1, 8).timestamp() * 1000) + 1
        assert result["new_records"] == 1
        assert FundingRateRecord.objects.filter(rate=0.0002).exists()

    def test_sync_dedupes_repeats(self):
        """Re-syncing the same rows must not duplicate (get_or_create)."""
        from apps.backtest import tasks as bt

        fetched = [{"dt": _dt(2024, 1, 9, 16), "rate": 0.0002}]
        with patch.object(
            bt, "_fetch_funding_history", new=AsyncMock(return_value=fetched)
        ):
            bt.sync_funding_rates(["BTC/USDT:USDT"])
            result = bt.sync_funding_rates(["BTC/USDT:USDT"])

        assert result["new_records"] == 0
        assert FundingRateRecord.objects.count() == 1

    def test_sync_first_time_no_latest(self):
        """No existing records: since is None, full history fetched."""
        from apps.backtest import tasks as bt

        fetched = [{"dt": _dt(2024, 1, 1, 0), "rate": 0.0001}]
        with patch.object(
            bt, "_fetch_funding_history", new=AsyncMock(return_value=fetched)
        ) as m:
            bt.sync_funding_rates(["ETH/USDT:USDT"])

        assert m.call_args.args[2] is None
        assert FundingRateRecord.objects.count() == 1

    def test_sync_stores_aware_datetime(self):
        """Stored funding_time must be timezone-aware (no naive warning)."""
        from apps.backtest import tasks as bt

        fetched = [{"dt": _dt(2024, 1, 9, 16), "rate": 0.0002}]
        with patch.object(
            bt, "_fetch_funding_history", new=AsyncMock(return_value=fetched)
        ):
            bt.sync_funding_rates(["BTC/USDT:USDT"])

        rec = FundingRateRecord.objects.get(symbol="BTC/USDT:USDT")
        assert dj_tz.is_aware(rec.funding_time)

    def test_beat_schedule_registered(self):
        from celery_app import app

        entry = app.conf.beat_schedule.get("sync-funding-rates")
        assert entry is not None
        assert entry["task"] == "apps.backtest.tasks.sync_funding_rates"


class TestLoadFundingRates:
    """load_funding_rates: DB 记录 → 引擎要的 {ISO UTC: rate} 字典。"""

    def test_returns_iso_utc_keys(self):
        from apps.backtest.tasks import load_funding_rates

        FundingRateRecord.objects.create(
            symbol="BTC/USDT:USDT", funding_time=_dt(2024, 1, 1, 8), rate=0.0001
        )
        ohlcv = [
            {"timestamp": "2024-01-01T00:00:00+00:00"},
            {"timestamp": "2024-01-01T16:00:00+00:00"},
        ]
        rates = load_funding_rates("BTC/USDT:USDT", ohlcv)
        assert rates == {"2024-01-01T08:00:00+00:00": 0.0001}

    def test_filters_outside_ohlcv_range(self):
        from apps.backtest.tasks import load_funding_rates

        FundingRateRecord.objects.create(
            symbol="BTC/USDT:USDT", funding_time=_dt(2023, 12, 31, 16), rate=0.0009
        )
        FundingRateRecord.objects.create(
            symbol="BTC/USDT:USDT", funding_time=_dt(2024, 1, 1, 8), rate=0.0001
        )
        ohlcv = [
            {"timestamp": "2024-01-01T00:00:00+00:00"},
            {"timestamp": "2024-01-01T16:00:00+00:00"},
        ]
        rates = load_funding_rates("BTC/USDT:USDT", ohlcv)
        assert list(rates.values()) == [0.0001]

    def test_other_symbol_excluded(self):
        from apps.backtest.tasks import load_funding_rates

        FundingRateRecord.objects.create(
            symbol="ETH/USDT:USDT", funding_time=_dt(2024, 1, 1, 8), rate=0.0001
        )
        ohlcv = [{"timestamp": "2024-01-01T00:00:00+00:00"}]
        assert load_funding_rates("BTC/USDT:USDT", ohlcv) == {}

    def test_empty_ohlcv_returns_empty(self):
        from apps.backtest.tasks import load_funding_rates

        assert load_funding_rates("BTC/USDT:USDT", []) == {}

    def test_naive_ohlcv_timestamp_treated_as_utc(self):
        """naive 时间戳按 UTC 解释（与生产者修复后的口径一致）。"""
        from apps.backtest.tasks import load_funding_rates

        FundingRateRecord.objects.create(
            symbol="BTC/USDT:USDT", funding_time=_dt(2024, 1, 1, 8), rate=0.0001
        )
        ohlcv = [
            {"timestamp": "2024-01-01T00:00:00"},
            {"timestamp": "2024-01-01T16:00:00"},
        ]
        assert load_funding_rates("BTC/USDT:USDT", ohlcv) == {
            "2024-01-01T08:00:00+00:00": 0.0001
        }


class TestSlippageColumnRoundTrip:
    """save_backtest_result 的 slippage/cost_detail 键能 round-trip 到 BacktestTrade。

    这是 Standards 审查硬问题 #1 的回归护栏：引擎写 slippage 进 trade dict，
    持久化层必须落到 BacktestTrade.slippage 列，而不是静默丢弃。

    `transaction=True` 是必需的：这个用例用 `acreate` 在事件循环线程里写库，
    而 pytest-django 的普通 `django_db` 把事务包在**测试线程**上——异步体跑在
    别的线程，写落在事务之外，于是 `Strategy(name="rt_test")` 会**永久提交**，
    在整轮测试里对其它读全表的用例（如 regime 的幽灵清理）可见。改用
    `TransactionTestCase` 语义后每例结束即截断，行不再泄漏。
    """

    pytestmark = pytest.mark.django_db(transaction=True)

    @pytest.mark.asyncio
    async def test_slippage_persisted_to_backtest_trade(self):
        from apps.trading.models import Strategy
        from apps.backtest.models import BacktestTrade
        from apps.strategy_engine.backtest_mode import save_backtest_result

        strategy = await Strategy.objects.acreate(
            name="rt_test", code_path="strategies/rt_test.py", is_active=False
        )
        stats = {
            "final_equity": 10000.0,
            "total_return_pct": 0.0,
            "max_drawdown_pct": 0.0,
            "win_rate": 1.0,
            "sharpe_ratio": 1.0,
            "total_trades": 1,
            "cost_detail": {"commission": 5.0, "slippage": 2.0, "funding": 0.0},
            "equity_curve": [],
            "trades": [
                {
                    "entry_time": "2024-01-01T00:00:00+00:00",
                    "exit_time": "2024-01-01T01:00:00+00:00",
                    "side": "long",
                    "entry_price": 42000.0,
                    "exit_price": 42100.0,
                    "quantity": 1.0,
                    "pnl": 100.0,
                    "pnl_pct": 0.002,
                    "commission": 5.0,
                    "slippage": 2.0,
                    "signal": "buy",
                    "exit_reason": "signal",
                    "trade_type": "open",
                }
            ],
        }
        result = await save_backtest_result(
            strategy_id=str(strategy.id),
            symbol="BTC/USDT",
            timeframe="1h",
            start_date="2024-01-01",
            end_date="2024-01-02",
            initial_capital=10000.0,
            parameters={},
            stats=stats,
        )
        assert result["trade_count"] == 1
        trade = BacktestTrade.objects.get(backtest_id=result["result_id"])
        assert float(trade.slippage) == pytest.approx(2.0)
        assert float(trade.commission) == pytest.approx(5.0)


class TestFundingSyncProxyInjection:
    """_fetch_all_symbols 必须与 _fetch_ohlcv_sync 一样注入 WEB_PROXY。

    回归护栏：该路径曾漏注入代理 → 在需要代理出网的环境下 K 线能拉、资金费率
    拉不到；而 sync_funding_rates 不检查失败、beat 也不告警，缺陷是静默的。
    """

    @staticmethod
    def _options_passed_to_binance() -> dict:
        from apps.backtest import tasks as bt

        captured: dict = {}

        class _FakeExchange:
            async def close(self):
                return None

        def fake_binance(options=None, *args, **kwargs):
            captured["options"] = options
            return _FakeExchange()

        # plans 为空 → 不进网络循环；只验证构造参数与 close() 收尾
        with patch("ccxt.async_support.binance", side_effect=fake_binance):
            asyncio.run(bt._fetch_all_symbols([]))

        return captured["options"]

    @override_settings(WEB_PROXY="http://host.docker.internal:7890")
    def test_proxy_injected_when_configured(self):
        options = self._options_passed_to_binance()
        assert options["aiohttp_proxy"] == "http://host.docker.internal:7890"

    @override_settings(WEB_PROXY="")
    def test_no_proxy_key_when_unset(self):
        options = self._options_passed_to_binance()
        assert "aiohttp_proxy" not in options

    @override_settings(WEB_PROXY="")
    def test_rate_limit_still_enabled(self):
        """注入代理不得挤掉既有的 enableRateLimit。"""
        options = self._options_passed_to_binance()
        assert options["enableRateLimit"] is True


class TestBeatScheduleTimezoneIntent:
    """钉死 sync-funding-rates 的 beat 时区意图：UTC 结算点 +30min。"""

    def test_beat_fires_at_utc_settlement_plus_30min(self):
        from celery_app import app

        entry = app.conf.beat_schedule.get("sync-funding-rates")
        assert entry is not None
        sched = entry["schedule"]
        # crontab(hour='1,9,17', minute=30)：1/9/17 点 30 分（CELERY_TIMEZONE=UTC）
        # = 每个 UTC 结算点（0/8/16）后 30 分钟，给交易所落定数据留窗口。
        # crontab 把数字字段解析成 frozenset（minute=30 → {30}，hour='1,9,17' → {1,9,17}）
        assert sched.minute == {30}
        assert sched.hour == {1, 9, 17}
