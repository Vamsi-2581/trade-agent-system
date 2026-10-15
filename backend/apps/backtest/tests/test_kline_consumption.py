"""回测消费 K 线库数据的**整链路**集成测试（spec §10.2 / §13-4、§13-5）。

与 ``test_load_ohlcv_for_backtest.py`` 的分工：那份钉的是
``_load_ohlcv_for_backtest`` 单函数的取数/缺口语义；这份把整条
``run_backtest_task`` 链路跑起来——策略注册 → 取数 → 引擎回放 → 结果落库 →
``data_source`` 回写。

「零 API 依赖」的守卫放在**最低层**：把 ``build_exchange``（ccxt 客户端构造，任何
真实出网路径的必经之处）替成「一调用就抛 + 记账」，而**不**替换
``_fetch_ohlcv_sync``。这样断言的不是「某个接缝没被调到」，而是「整条任务链路根本
没试图构造交易所客户端」。反向对照（库空时同一个守卫必须真的炸）证明这个断言不是
空转。

区间约定与 ``test_load_ohlcv_for_backtest.py`` 一致：
``start_date=2026-01-01`` / ``end_date=2026-01-02`` → 有效 bar 是 0h..23h 共 24 根
（``end_date`` 当天 0 点那根按未收盘规则不进回测）。
"""

from contextlib import ExitStack
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from apps.backtest import tasks
from apps.backtest.models import BacktestResult
from apps.datasource.models import KlineRecord
from apps.strategy_engine.base import BaseStrategy
from apps.strategy_engine.registry import StrategyRegistry

pytestmark = pytest.mark.django_db(transaction=True)

SYMBOL = "BTC/USDT:USDT"
TF = "1h"
STEP = timedelta(hours=1)
STEP_MS = 3_600_000
START = datetime(2026, 1, 1, tzinfo=timezone.utc)
END = datetime(2026, 1, 2, tzinfo=timezone.utc)
START_DATE = "2026-01-01"
END_DATE = "2026-01-02"
FULL_BARS = 24  # 0h..23h

STRATEGY_NAME = "p1_kline_consumption_probe"


class _ProbeStrategy(BaseStrategy):
    """最小探针策略：不下单，只为把整条回测链路跑通（0 笔交易是合法结果）。"""

    name = STRATEGY_NAME
    description = (
        "策略类型：趋势跟踪\n"
        "核心指标：无\n"
        "适用场景：集成测试探针\n"
        "入场逻辑：不下单，仅验证数据链路"
    )


def _register_probe() -> None:
    """注册探针策略。

    直接注册而不依赖 ``~/.tradelogx/strategies`` 目录发现：目录内容随机器变化，
    测试不能建立在开发机的 home 目录上。
    """
    StrategyRegistry.register(_ProbeStrategy, name=STRATEGY_NAME)


def _bar(i: int) -> datetime:
    return START + i * STEP


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


class _ExchangeGuard:
    """交易所不可达：任何一次客户端构造都抛错并记账。"""

    def __init__(self):
        self.calls: list[tuple] = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        raise ConnectionError("exchange unreachable (integration test)")


class _PagedFetchSpy:
    """替换 ``_fetch_ohlcv_sync``：按 ``[since_ms, end_ms]`` 生成 bar 并记账。

    只用于「缺口补拉」用例——那里需要可控的补拉返回值。其余用例一律走真实的
    ``_fetch_ohlcv_sync``，让 ``_ExchangeGuard`` 兜住出网。
    """

    def __init__(self):
        self.calls: list[dict] = []

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
        self.calls.append({"since_ms": since_ms, "end_ms": end_ms})
        if since_ms is None or end_ms is None:
            return []
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


def _run_backtest(
    guard: _ExchangeGuard, fetch: _PagedFetchSpy | None = None, **overrides
) -> dict:
    """跑一次 ``run_backtest_task`` 任务体。

    直接调任务体 ``.run()`` 而不用 ``.apply()``：``.apply()`` 会把结果写进 Celery
    result backend（Redis），CI 里没有 Redis，测试不该因此变红。任务体的全部分支都
    被执行，只有 ``update_state``（纯进度上报）被替成 no-op。

    ``StrategyRegistry.discover`` 也替成 no-op：任务体里会无条件调它，真实实现会去
    扫开发机的 ``~/.tradelogx/strategies``，测试不该依赖该目录。
    """
    _register_probe()
    kwargs = {
        "strategy_name": STRATEGY_NAME,
        "symbol": SYMBOL,
        "timeframe": TF,
        "initial_capital": 10000,
        "start_date": START_DATE,
        "end_date": END_DATE,
    }
    kwargs.update(overrides)

    with ExitStack() as stack:
        stack.enter_context(patch.object(tasks, "build_exchange", guard))
        stack.enter_context(patch.object(tasks, "dispatch_slice", lambda ids: False))
        stack.enter_context(
            patch.object(tasks.run_backtest_task, "update_state", lambda **kwargs: None)
        )
        stack.enter_context(
            patch.object(StrategyRegistry, "discover", classmethod(lambda cls: []))
        )
        if fetch is not None:
            stack.enter_context(patch.object(tasks, "_fetch_ohlcv_sync", fetch))
        return tasks.run_backtest_task.run(**kwargs)


def _data_source_of(stats: dict) -> dict:
    return BacktestResult.objects.get(id=stats["result_id"]).data_source


class TestBacktestConsumesDbKlines:
    def test_full_backtest_zero_api_calls_when_db_complete(self):
        """spec §13-4：库完备 → 整条回测链路零交易所调用，且 data_source 落库。"""
        _seed(range(FULL_BARS))
        guard = _ExchangeGuard()  # 一构造客户端就抛

        stats = _run_backtest(guard)

        assert guard.calls == []  # 没试图出网 → 交易所不可达也不影响回测
        assert stats["result_id"]
        assert _data_source_of(stats) == {
            "from_db": FULL_BARS,
            "from_api": 0,
            "gaps": [],
        }
        # 库没被多写：完整区间不需要回写
        assert (
            KlineRecord.objects.filter(symbol=SYMBOL, timeframe=TF).count() == FULL_BARS
        )

    def test_same_backtest_replays_with_zero_api_calls(self):
        """票面要求：回填后「同一回测可零 API 调用复现」——第二次跑同样零调用。"""
        _seed(range(FULL_BARS))
        guard = _ExchangeGuard()

        first = _run_backtest(guard)
        second = _run_backtest(guard)

        assert guard.calls == []
        assert _data_source_of(first)["from_api"] == 0
        assert _data_source_of(second) == {
            "from_db": FULL_BARS,
            "from_api": 0,
            "gaps": [],
        }
        assert first["result_id"] != second["result_id"]

    def test_full_backtest_fills_gap_and_writes_back(self):
        """spec §13-5：库有尾部缺口 → 任务内补拉 + 回写，data_source 如实记录。"""
        _seed(range(19))  # 0h..18h，缺 19h..23h
        guard = _ExchangeGuard()
        fetch = _PagedFetchSpy()

        stats = _run_backtest(guard, fetch)

        assert len(fetch.calls) == 1
        assert fetch.calls[0]["since_ms"] == int(_bar(19).timestamp() * 1000)
        assert _data_source_of(stats)["from_db"] == 19
        assert _data_source_of(stats)["from_api"] == 5
        assert _data_source_of(stats)["gaps"]  # 缺口如实记录，便于排查
        # 回写生效：19 根 + 补拉的 6 根（含 end_dt 端点那根，不在回测窗口内）
        assert KlineRecord.objects.filter(symbol=SYMBOL, timeframe=TF).count() == 25

    def test_unreachable_exchange_with_empty_db_fails(self):
        """反向对照：库空 + 交易所不可达 → 任务失败。

        没有这条，「零 API 调用」的断言可能是假的——它证明同一个 ``_ExchangeGuard``
        在库空时确实被调用、确实让取数失败，回测不会拿空数据硬跑出结果。
        """
        guard = _ExchangeGuard()

        with pytest.raises(ValueError, match="未能获取"):
            _run_backtest(guard)

        assert guard.calls  # 确实试图构造客户端，且失败
        assert not KlineRecord.objects.filter(symbol=SYMBOL, timeframe=TF).exists()

    def test_persisted_result_matches_seeded_bars(self):
        """``data_source.from_db`` 与库行数一致，且回测区间落在库覆盖范围内。"""
        _seed(range(FULL_BARS))
        guard = _ExchangeGuard()

        stats = _run_backtest(guard)

        result = BacktestResult.objects.get(id=stats["result_id"])
        db_bars = KlineRecord.objects.filter(
            symbol=SYMBOL,
            timeframe=TF,
            timestamp__gte=START,
            timestamp__lte=END,
        ).count()
        assert result.data_source["from_db"] == db_bars == FULL_BARS
        assert result.start_date == date(2026, 1, 1)
        assert result.end_date == date(2026, 1, 2)
