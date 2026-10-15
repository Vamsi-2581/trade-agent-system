"""walk-forward 分折的**执行层**（spec #19 D4 / D15 / D18；子票 #29）。

钉住的不变式：

1. **每折一行 `BacktestResult`，`candidate_id` 一致、`metrics["walk_forward"]` 完整**
   （D4 前两条 + 票面验收第 1 条）。
2. **聚合与衰减写在候选主（样本内）那一行**（D4 第三条 + 验收第 2 条）。
3. **区间已入库时分折零交易所调用**（验收第 3 条），并带**反向对照**证明守卫不是空转
   ——沿用 `apps/backtest/tests/test_kline_consumption.py` 的 `build_exchange` 守卫模式。
4. **写 `metrics` 只动自己的键**：`metrics` 里已有 `regime_slice`（`apps/regime` 写的）
   时不许被抹掉。这是本仓库真实发生过的一类事故（读-改-写整个 JSON 字段）。
5. **幂等**：重跑分折收敛成一组折，不越跑越多（D18）。
"""

from __future__ import annotations

import uuid
from contextlib import ExitStack
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest

from apps.backtest.models import BacktestResult
from apps.datasource.models import KlineRecord
from apps.strategy_engine.base import BaseStrategy
from apps.strategy_engine.registry import StrategyRegistry
from apps.strategy_engine.runner import StrategyRunner
from apps.strategy_factory.walk_forward import (
    WALK_FORWARD_METRICS_KEY,
    WalkForwardWindowRole,
    plan_folds,
)
from apps.strategy_factory.walk_forward_runner import (
    clear_fold_results,
    run_walk_forward,
    tag_fold_result,
)
from apps.trading.models import Strategy

pytestmark = pytest.mark.django_db(transaction=True)

SYMBOL = "BTC/USDT:USDT"
TF = "1h"
STEP = timedelta(hours=1)
STEP_MS = 3_600_000
START = datetime(2026, 1, 1, tzinfo=timezone.utc)

#: 100 天区间 / 5 折 / 样本外 20% → 每折 4 天，样本内 80 天（与纯层用例同一组几何）。
#:
#: `IN_SAMPLE_END` 是**样本内区的右界**，由 `plan_folds` 从完整窗口推出：
#: 100 天 × (1 − 0.2) = 80 天 → 2026-03-22。候选主行（L1 样本内行）应当止于此，
#: 而**不是**止于 `END_DATE`——那是整个评估窗口的右界。
RANGE_DAYS = 100
START_DATE = date(2026, 1, 1)
END_DATE = date(2026, 4, 11)
IN_SAMPLE_END = date(2026, 3, 22)

STRATEGY_NAME = "wf_probe"


class _ProbeStrategy(BaseStrategy):
    """最小探针策略：每根 bar 交替买卖，产出真实的权益曲线与夏普。

    用**真实**策略而不是假执行器跑「零交易所调用」那条用例：那条断言的价值在于
    「整条链路（取数 → 引擎 → 落库）根本没试图出网」，假执行器会把它变成一句空话。
    """

    name = STRATEGY_NAME
    description = (
        "策略类型：趋势跟踪\n"
        "核心指标：无\n"
        "适用场景：分折集成测试探针\n"
        "入场逻辑：每根 bar 交替买卖"
    )

    def on_bar(self, kline, history):
        # 交替方向，保证有已平仓交易与一条非平凡的权益曲线（否则夏普会是 None）。
        if len(history) % 4 == 0:
            return self.ctx.buy(Decimal("1"))
        if len(history) % 4 == 2 and self.ctx.position > 0:
            return self.ctx.sell(Decimal("1"))
        return None


def _register_probe() -> None:
    """注册探针策略。

    直接注册而不依赖 `~/.tradelogx/strategies` 目录发现：目录内容随机器变化，
    测试不能建立在开发机的 home 目录上。
    """
    StrategyRegistry.register(_ProbeStrategy, name=STRATEGY_NAME)


def _strategy() -> Strategy:
    return Strategy.objects.create(
        name=STRATEGY_NAME, code_path="strategies/wf_probe.py"
    )


def _seed_days(start: date, days: int) -> None:
    """按 1h 粒度铺满 `[start, start+days)` 的 K 线（供真实取数路径消费）。

    收盘价走一条确定性的斜坡：恒定价格会让权益曲线标准差为 0，`_compute_sharpe`
    于是返回 `None`，那条用例就会因为「算不出夏普」而落进「证据不足」——测不到它想
    测的东西。
    """
    base = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
    rows = []
    for day in range(days):
        for hour in range(24):
            ts = base + timedelta(days=day, hours=hour)
            close = 100.0 + (day * 24 + hour) * 0.05
            rows.append(
                KlineRecord(
                    exchange="binance",
                    symbol=SYMBOL,
                    timeframe=TF,
                    timestamp=ts,
                    open=close,
                    high=close + 1.0,
                    low=close - 1.0,
                    close=close,
                    volume=1.0,
                )
            )
    KlineRecord.objects.bulk_create(rows)


def _main_row(
    strategy: Strategy,
    sharpe: float | None = 2.0,
    *,
    start: date = START_DATE,
    end: date = IN_SAMPLE_END,
    symbol: str = SYMBOL,
    timeframe: str = TF,
    funding: float = 0.0,
) -> BacktestResult:
    """候选主行（= L1 **样本内**那一次回测的产物）。

    默认区间是 `[START_DATE, IN_SAMPLE_END)`——**样本内区**，不是整个评估窗口。
    它提供衰减的分母（`is_sharpe`），必须只覆盖样本内那一段；覆盖整个窗口会让
    分母混进样本外的表现（见 `_validate_wiring`）。
    """
    return BacktestResult.objects.create(
        strategy=strategy,
        symbol=symbol,
        timeframe=timeframe,
        start_date=start,
        end_date=end,
        initial_capital=Decimal("10000"),
        final_capital=Decimal("11000"),
        total_return_pct=10.0,
        sharpe_ratio=sharpe,
        total_trades=120,
        parameters={"fast": 7, "slow": 25},
        cost_detail={"commission": 1.0, "slippage": 1.0, "funding": funding},
    )


class _ExchangeGuard:
    """交易所不可达：任何一次客户端构造都抛错并记账。"""

    def __init__(self):
        self.calls: list[tuple] = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        raise ConnectionError("exchange unreachable (walk-forward test)")


def _fold_result_row(
    strategy: Strategy, candidate_id, index: int, sharpe: float, trades: int = 40
) -> BacktestResult:
    return BacktestResult.objects.create(
        strategy=strategy,
        symbol=SYMBOL,
        timeframe=TF,
        start_date=date(2026, 2, 18 + index),
        end_date=date(2026, 2, 19 + index),
        initial_capital=Decimal("10000"),
        final_capital=Decimal("10100"),
        total_return_pct=1.0,
        sharpe_ratio=sharpe,
        total_trades=trades,
        candidate_id=candidate_id,
    )


def _fake_executor(sharpes, strategy, candidate_id, recorder=None):
    """假折执行器：把折序号换成预设夏普，并按序号造一行 `BacktestResult`。

    真实执行器的契约是「跑完一折并落库一行，返回 stats」，这里保持同一形状——
    接线用例断言的是**编排**，不是回测引擎。
    """
    state = {"n": 0}

    def _run(**kwargs):
        index = state["n"]
        state["n"] += 1
        if recorder is not None:
            recorder.append(kwargs)
        sharpe = sharpes[index]
        if sharpe is None:
            return {"error": "未能获取 K 线数据"}
        row = _fold_result_row(strategy, candidate_id, index, sharpe)
        return {
            "result_id": str(row.id),
            "sharpe_ratio": sharpe,
            "total_trades": 40,
        }

    return _run


# --------------------------------------------------------------------------- #
# D4：每折一行 + 分组键 + metrics 载荷
# --------------------------------------------------------------------------- #


def test_each_fold_gets_its_own_row_with_a_shared_candidate_id():
    strategy = _strategy()
    candidate_id = uuid.uuid4()
    main = _main_row(strategy)
    executor = _fake_executor([1.4] * 5, strategy, candidate_id)

    payload = _run(main, candidate_id, executor)

    fold_rows = BacktestResult.objects.filter(candidate_id=candidate_id)
    assert fold_rows.count() == 5, "每个 fold 一行 BacktestResult（D4）"
    seen: list[int] = []
    for row in fold_rows:
        assert row.candidate_id == candidate_id, "candidate_id 是分组键，折间必须一致"
        entry = (row.metrics or {})[WALK_FORWARD_METRICS_KEY]
        # D4 逐字点名的三个键必须在**每一折**的载荷里。少 `candidate_id` 时载荷就
        # 无法独立回答「这一行属于谁」，只能回查列——而列未必被带上（导出/审计）。
        assert entry["candidate_id"] == str(candidate_id)
        assert entry["window_role"] == WalkForwardWindowRole.OOS.value
        assert entry["fold_index"] in range(5)
        # 折身份与边界（D4 第三项：序号、窗口角色、窗口边界）
        assert entry["split_mode"] == "rolling"
        assert entry["start_date"] and entry["end_date"]
        assert entry["in_sample_start"] and entry["in_sample_end"]
        seen.append(entry["fold_index"])
    assert sorted(seen) == [0, 1, 2, 3, 4], "五折的 fold_index 齐全且不重复"
    assert payload["fold_count"] == 5


def test_aggregate_lands_on_the_candidate_main_row_not_a_fold_row():
    """D4：聚合与衰减写在候选**主**（样本内）那一行。"""
    strategy = _strategy()
    candidate_id = uuid.uuid4()
    main = _main_row(strategy)
    executor = _fake_executor([1.4] * 5, strategy, candidate_id)

    _run(main, candidate_id, executor)

    main.refresh_from_db()
    aggregate = (main.metrics or {})[WALK_FORWARD_METRICS_KEY]
    assert aggregate["window_role"] == WalkForwardWindowRole.IN_SAMPLE.value
    assert aggregate["is_sharpe"] == 2.0
    assert aggregate["oos_sharpe"] == pytest.approx(1.4)
    assert aggregate["decay_pct"] == 30.0
    # 主行本身不该被打上分组键：它是 L1 的产物，不是一折（D4 的角色区分）。
    assert main.candidate_id is None


def test_main_row_keeps_its_own_review_fields_untouched():
    """分折只写 `metrics` 的自己的键，不碰审核字段（T0 的职责边界）。"""
    strategy = _strategy()
    candidate_id = uuid.uuid4()
    main = _main_row(strategy)
    main.review_status = "approved"
    main.review_notes = "人工审核过"
    main.save(update_fields=["review_status", "review_notes"])
    executor = _fake_executor([1.4] * 5, strategy, candidate_id)

    _run(main, candidate_id, executor)

    main.refresh_from_db()
    assert main.review_status == "approved"
    assert main.review_notes == "人工审核过"


def test_writing_metrics_does_not_clobber_other_keys():
    """`metrics` 里已有别人的键（`regime_slice`）时必须原样留存。

    这是本仓库真实发生过的一类事故：读-改-写整个 JSON 字段时，后写的一方会把前一方
    的键抹掉——那不是「我的结论被覆盖」，而是「别人的字段凭空消失」。
    """
    strategy = _strategy()
    candidate_id = uuid.uuid4()
    main = _main_row(strategy)
    main.metrics = {"regime_slice": {"version": 3, "payload": [1, 2, 3]}}
    main.save(update_fields=["metrics"])
    executor = _fake_executor([1.4] * 5, strategy, candidate_id)

    _run(main, candidate_id, executor)

    main.refresh_from_db()
    assert main.metrics["regime_slice"] == {"version": 3, "payload": [1, 2, 3]}
    assert WALK_FORWARD_METRICS_KEY in main.metrics


def test_parameters_are_inherited_from_the_main_row():
    """分折用候选主行那一份参数——否则衰减比较的是两个不同的策略。"""
    strategy = _strategy()
    candidate_id = uuid.uuid4()
    main = _main_row(strategy)
    recorded: list[dict] = []
    executor = _fake_executor([1.4] * 5, strategy, candidate_id, recorder=recorded)

    _run(main, candidate_id, executor)

    assert len(recorded) == 5
    for call in recorded:
        assert call["parameters"] == {"fast": 7, "slow": 25}
        assert call["strategy_id"] == str(strategy.id)
        assert call["initial_capital"] == Decimal("10000")


def test_perpetual_folds_include_funding_cost_like_the_in_sample_row():
    """永续分折必须带上历史资金费率，否则衰减是「含费率」对「不含费率」的比较。

    样本内那一行由 `run_backtest_task` 跑，`is_perp=True` 时注入资金费率回放（P0）。
    若各折不注入，分母（样本内成本）被抬高、分子（样本外）没有，衰减看起来更小、
    更容易放行——用户故事 13 要的「每层复用 P0 成本模型、口径一致」就是防这个。

    用**真实**默认执行器（不注入假执行器）：这条断言的整个价值在于「真实路径确实
    把费率表递进了引擎」，用假执行器会把它变成一句空话。
    """
    from apps.backtest.models import FundingRateRecord

    strategy = _strategy()
    candidate_id = uuid.uuid4()
    main = _main_row(strategy)
    _seed_days(START_DATE, RANGE_DAYS)
    _register_probe()
    # 铺资金费率：区间内每 8h 一条。没有它，`load_funding_rates` 会返回空表，
    # 「注入与否」就判别不出来。
    FundingRateRecord.objects.bulk_create(
        [
            FundingRateRecord(
                symbol=SYMBOL,
                funding_time=datetime(2026, 1, 1, tzinfo=timezone.utc)
                + timedelta(hours=8 * i),
                rate=0.0005,
            )
            for i in range(RANGE_DAYS * 3)
        ]
    )
    captured: list[dict] = []
    real_run_backtest = StrategyRunner.run_backtest

    async def _spy(self, **kwargs):
        captured.append(kwargs)
        return await real_run_backtest(self, **kwargs)

    with ExitStack() as stack:
        stack.enter_context(
            patch("apps.backtest.tasks.build_exchange", _ExchangeGuard())
        )
        stack.enter_context(
            patch("apps.strategy_engine.registry.StrategyRegistry.discover", lambda: [])
        )
        stack.enter_context(patch.object(StrategyRunner, "run_backtest", _spy))
        payload = _run(main, candidate_id, None, is_perp=True, include_funding=True)

    assert payload["data_sufficient"] is True, payload["insufficient_reason"]
    assert len(captured) == 5
    for call in captured:
        assert call["is_perp"] is True
        assert call["funding_rates"], (
            "永续分折的每一折都必须带上资金费率——空表意味着衰减在拿"
            "「含费率的样本内」比「不含费率的样本外」"
        )


def test_spot_folds_do_not_load_funding_rates():
    """非永续（或显式关掉）时不查资金费率表——省一次全表扫描，且口径正确。"""
    strategy = _strategy()
    candidate_id = uuid.uuid4()
    main = _main_row(strategy)
    _seed_days(START_DATE, RANGE_DAYS)
    _register_probe()
    captured: list[dict] = []
    real_run_backtest = StrategyRunner.run_backtest

    async def _spy(self, **kwargs):
        captured.append(kwargs)
        return await real_run_backtest(self, **kwargs)

    with ExitStack() as stack:
        stack.enter_context(
            patch("apps.backtest.tasks.build_exchange", _ExchangeGuard())
        )
        stack.enter_context(
            patch("apps.strategy_engine.registry.StrategyRegistry.discover", lambda: [])
        )
        stack.enter_context(patch.object(StrategyRunner, "run_backtest", _spy))
        _run(main, candidate_id, None, is_perp=False, include_funding=True)

    assert len(captured) == 5
    for call in captured:
        assert call["is_perp"] is False
        assert call["funding_rates"] == {}


def test_folds_are_contiguous_and_cover_the_oos_region_end_to_end():
    """编排真的按几何取数：各折区间首尾相接、并正好铺满样本外区。"""
    strategy = _strategy()
    candidate_id = uuid.uuid4()
    main = _main_row(strategy)
    recorded: list[dict] = []
    executor = _fake_executor([1.4] * 5, strategy, candidate_id, recorder=recorded)

    _run(main, candidate_id, executor)

    plan = plan_folds(START_DATE, END_DATE)
    assert [c["start_date"] for c in recorded] == [
        w.oos_start.isoformat() for w in plan.windows
    ]
    assert [c["end_date"] for c in recorded] == [
        w.oos_end.isoformat() for w in plan.windows
    ]


# --------------------------------------------------------------------------- #
# 零交易所调用（验收第 3 条）+ 反向对照
# --------------------------------------------------------------------------- #


def test_full_range_in_db_means_zero_exchange_calls_across_all_folds():
    """验收第 3 条：区间已入库时分折**零交易所调用**。

    守卫放在最低层（`tasks.build_exchange`，任何真实出网路径的必经之处），所以断言的
    不是「某个接缝没被调到」，而是「整条分折链路根本没试图构造交易所客户端」。
    """
    strategy = _strategy()
    candidate_id = uuid.uuid4()
    main = _main_row(strategy)
    # 铺满主行区间：样本内 80 天 + 样本外 20 天，各折全部落在库里
    _seed_days(START_DATE, RANGE_DAYS)
    _register_probe()
    guard = _ExchangeGuard()

    with ExitStack() as stack:
        stack.enter_context(patch("apps.backtest.tasks.build_exchange", guard))
        stack.enter_context(
            patch("apps.strategy_engine.registry.StrategyRegistry.discover", lambda: [])
        )
        payload = _run(main, candidate_id, None)

    assert guard.calls == [], "库完备时不该有任何一次交易所客户端构造"
    assert payload["data_sufficient"] is True, payload["insufficient_reason"]
    assert payload["fold_count"] == 5
    assert payload["decay_pct"] is not None
    # data_source 如实记录「全部来自库」
    fold_rows = BacktestResult.objects.filter(candidate_id=candidate_id)
    assert fold_rows.count() == 5
    for row in fold_rows:
        assert row.data_source["from_api"] == 0
        assert row.data_source["from_db"] > 0
        assert row.sharpe_ratio is not None, "斜坡行情下探针该产出真实的夏普"


def test_reverse_control_empty_db_with_unreachable_exchange_does_not_pass():
    """反向对照：库空 + 交易所不可达 → 判「证据不足」，**不放行**。

    没有这条，「零 API 调用」的断言可能是假的——它证明同一个守卫在库空时确实被调用、
    确实让取数失败，而分折不会拿空数据硬跑出一个「通过」。
    """
    strategy = _strategy()
    candidate_id = uuid.uuid4()
    main = _main_row(strategy)
    _register_probe()
    guard = _ExchangeGuard()

    with ExitStack() as stack:
        stack.enter_context(patch("apps.backtest.tasks.build_exchange", guard))
        stack.enter_context(
            patch("apps.strategy_engine.registry.StrategyRegistry.discover", lambda: [])
        )
        payload = _run(main, candidate_id, None)

    assert guard.calls, "库空时确实试图构造客户端（守卫不是空转）"
    assert payload["data_sufficient"] is False
    assert payload["decay_pct"] is None, "拿不到数据就不该有衰减数字"
    assert payload["insufficient_reason"] == "fold_sharpe_unusable"
    assert BacktestResult.objects.filter(candidate_id=candidate_id).count() == 0


# --------------------------------------------------------------------------- #
# 数据不足 / 一折失败 → 判不了
# --------------------------------------------------------------------------- #


def test_insufficient_geometry_runs_no_backtest_at_all():
    """评估窗口切不出折时**一次回测都不跑**（漏斗下限 = 什么都不发生）。

    判别力说明：上一版这条用例是**空转**的——它靠缩短主行区间来制造「切不出折」，
    而缩短主行现在会先被 `_validate_wiring` 判成接线错误。更根本的是：
    `plan.windows == ()` 时循环体本来就不会跑，所以「executor 没被调用」无论守卫
    在不在都成立。这里改成**显式注入一个切不出折的 plan**，并同时断言
    `plan.windows` 为空——这样「因为窗口是空的所以没跑」与「守卫拦住了」两件事
    被分开表达，守卫被删掉时 `data_sufficient` 那条断言会立刻转红。
    """
    strategy = _strategy()
    candidate_id = uuid.uuid4()
    # 1 天的**评估窗口**（不是 1 天的主行）：切不出 5 折。主行与之自洽
    # （`main_end == plan.in_sample_end`），这样本条测的纯粹是「几何不足」，
    # 不会先被接线校验拦下。
    short_start, short_end = date(2026, 1, 1), date(2026, 1, 2)
    short_plan = plan_folds(short_start, short_end)
    assert short_plan.data_sufficient is False
    assert short_plan.windows == ()
    main = _main_row(strategy, start=short_start, end=short_plan.in_sample_end)
    recorded: list[dict] = []
    executor = _fake_executor([1.4] * 5, strategy, candidate_id, recorder=recorded)

    payload = _run(main, candidate_id, executor, fold_plan=short_plan)

    assert recorded == [], "切不出折就不该跑任何一折"
    assert payload["data_sufficient"] is False
    assert payload["decay_pct"] is None
    assert payload["insufficient_reason"] is not None
    assert BacktestResult.objects.filter(candidate_id=candidate_id).count() == 0


def test_a_fold_that_fails_makes_the_whole_verdict_insufficient():
    """任一折取数/回测失败 → 整轮判不了，且不落一个 0 去顶。"""
    strategy = _strategy()
    candidate_id = uuid.uuid4()
    main = _main_row(strategy)
    executor = _fake_executor([1.4, 1.4, None, 1.4, 1.4], strategy, candidate_id)

    payload = _run(main, candidate_id, executor)

    assert payload["data_sufficient"] is False
    assert payload["decay_pct"] is None
    assert payload["insufficient_reason"] == "fold_sharpe_unusable"
    # 成功的折仍然落库（证据要留），只是结论是「判不了」
    assert BacktestResult.objects.filter(candidate_id=candidate_id).count() == 4


def test_fold_executor_exception_is_recorded_not_raised():
    """一折抛错不该炸掉整轮分折——它是这一折的结论，不是整轮的失败。"""
    strategy = _strategy()
    candidate_id = uuid.uuid4()
    main = _main_row(strategy)

    def _boom(**kwargs):
        raise RuntimeError("引擎炸了")

    payload = _run(main, candidate_id, _boom)

    assert payload["data_sufficient"] is False
    assert payload["decay_pct"] is None
    assert len(payload["folds"]) == 5
    assert all(entry["sharpe_ratio"] is None for entry in payload["folds"])


# --------------------------------------------------------------------------- #
# 幂等（D18）
# --------------------------------------------------------------------------- #


def test_rerunning_converges_to_one_set_of_folds():
    """D18 的幂等键落点：重跑分折收敛成一组折，不越跑越多。"""
    strategy = _strategy()
    candidate_id = uuid.uuid4()
    main = _main_row(strategy)

    _run(main, candidate_id, _fake_executor([1.4] * 5, strategy, candidate_id))
    first = set(
        BacktestResult.objects.filter(candidate_id=candidate_id).values_list(
            "id", flat=True
        )
    )
    assert len(first) == 5

    _run(main, candidate_id, _fake_executor([1.4] * 5, strategy, candidate_id))
    second = set(
        BacktestResult.objects.filter(candidate_id=candidate_id).values_list(
            "id", flat=True
        )
    )

    assert len(second) == 5, "重跑后仍是一组折，不是两组"
    assert first.isdisjoint(second), "旧折行被清掉，换成本轮新跑的"


def test_clear_existing_false_keeps_previous_fold_rows():
    """`clear_existing=False` 时不动既有折行——给 #22 的「续跑」留一个开关。

    默认（清理）服务于「重跑收敛」；但编排层若想在同一次推进里分批补跑，需要一个
    「别把我已有的折删掉」的口子。这条钉住那个开关真的有效，而不是一个被忽略的入参。
    """
    strategy = _strategy()
    candidate_id = uuid.uuid4()
    main = _main_row(strategy)

    _run(main, candidate_id, _fake_executor([1.4] * 5, strategy, candidate_id))
    before = set(
        BacktestResult.objects.filter(candidate_id=candidate_id).values_list(
            "id", flat=True
        )
    )
    assert len(before) == 5

    _run(
        main,
        candidate_id,
        _fake_executor([1.4] * 5, strategy, candidate_id),
        clear_existing=False,
    )
    after = set(
        BacktestResult.objects.filter(candidate_id=candidate_id).values_list(
            "id", flat=True
        )
    )

    assert before <= after, "关闭清理时旧折行必须留存"
    assert len(after) == 10, "新一轮又落了 5 行（未清理）"


def test_injected_fold_plan_is_used_instead_of_recomputing_geometry():
    """`fold_plan` 可注入：编排层能自己定几何，而不必让分折再算一遍。

    这条同时证明「配置驱动」不是唯一入口——#22 若要按候选自己的历史长度定制窗口，
    有地方放。断言用折**数**（3）判别：默认配置是 5，注入生效才会是 3。
    """
    strategy = _strategy()
    candidate_id = uuid.uuid4()
    main = _main_row(strategy)
    plan = plan_folds(START_DATE, END_DATE, folds=3)
    assert plan.folds == 3

    payload = _run(
        main,
        candidate_id,
        _fake_executor([1.4] * 3, strategy, candidate_id),
        fold_plan=plan,
    )

    assert payload["expected_fold_count"] == 3
    assert payload["fold_count"] == 3
    assert payload["decay_pct"] == 30.0
    assert BacktestResult.objects.filter(candidate_id=candidate_id).count() == 3


def test_clear_fold_results_never_touches_the_main_row():
    """清理只针对样本外折行：主行是 L1 的产物，分折无权处置它。

    主行免疫靠的是**结构**而不是一个标记：它不带 `candidate_id`（由
    `in_sample_result_id` 显式指认，不是「一折」）。所以即便它的 `metrics` 里写着
    `in_sample`，清理也不会碰它。
    """
    strategy = _strategy()
    candidate_id = uuid.uuid4()
    main = _main_row(strategy)
    main.metrics = {WALK_FORWARD_METRICS_KEY: {"window_role": "in_sample"}}
    main.save(update_fields=["metrics"])
    fold = _fold_result_row(strategy, candidate_id, 0, 1.4)
    tag_fold_result(
        fold.id,
        candidate_id=candidate_id,
        fold_payload={"fold_index": 0, "window_role": "oos"},
    )

    removed = clear_fold_results(candidate_id)

    assert removed == 1
    assert BacktestResult.objects.filter(id=fold.id).exists() is False
    assert BacktestResult.objects.filter(id=main.id).exists() is True


def test_clear_fold_results_removes_rows_even_without_a_metrics_marker():
    """清理的判据是 `candidate_id` 非空，**不解析 `metrics`**。

    按 metrics 里的 `window_role` 判是脆的：一行若因任何原因没写上那个标记，就变成
    永远清不掉的孤儿——而「同一层重复触发」正是靠清理来收敛的。这条钉住那个决定。
    """
    strategy = _strategy()
    candidate_id = uuid.uuid4()
    bare = _fold_result_row(strategy, candidate_id, 0, 1.4)  # 没有任何 metrics 标记

    removed = clear_fold_results(candidate_id)

    assert removed == 1
    assert BacktestResult.objects.filter(id=bare.id).exists() is False


def test_clear_fold_results_with_no_candidate_id_is_a_noop():
    """空 `candidate_id` 绝不能变成「删掉所有不带分组键的行」。

    `filter(candidate_id=None)` 在 Django 里是 `IS NULL`——若不在入口挡住，一次误传
    空值就会把所有非分折的回测行删光。
    """
    strategy = _strategy()
    _main_row(strategy)
    _main_row(strategy)

    assert clear_fold_results(None) == 0
    assert clear_fold_results("") == 0
    assert BacktestResult.objects.count() == 2


def test_clear_fold_results_counts_fold_rows_not_cascaded_trades():
    """返回值是**折行**条数，不是 `QuerySet.delete()` 那个含级联的计数。

    真实的一折有几十笔 `BacktestTrade`（`on_delete=CASCADE`），所以直接返回
    `delete()` 的结果会把「删了 3 折」报成 18——那个数字要进日志、也要被幂等断言读，
    必须是折数。这条用例刻意给折行挂上成交，才能判别它（没有成交时两者恰好相等，
    判别不出来）。
    """
    from apps.backtest.models import BacktestTrade

    strategy = _strategy()
    candidate_id = uuid.uuid4()
    for index in range(3):
        row = _fold_result_row(strategy, candidate_id, index, 1.4)
        for _ in range(5):
            BacktestTrade.objects.create(
                backtest=row,
                entry_time=datetime(2026, 2, 18, tzinfo=timezone.utc),
                side="long",
                entry_price=Decimal("100"),
                quantity=Decimal("1"),
            )
    assert BacktestTrade.objects.count() == 15

    removed = clear_fold_results(candidate_id)

    assert removed == 3, "返回折数，不是 3 折 + 15 笔成交 = 18"
    assert BacktestResult.objects.filter(candidate_id=candidate_id).count() == 0
    assert BacktestTrade.objects.count() == 0, "成交随折行级联删掉"


def test_clear_fold_results_is_scoped_to_one_candidate():
    strategy = _strategy()
    mine, theirs = uuid.uuid4(), uuid.uuid4()
    _fold_result_row(strategy, mine, 0, 1.4)
    other = _fold_result_row(strategy, theirs, 0, 1.4)

    clear_fold_results(mine)

    assert BacktestResult.objects.filter(candidate_id=mine).count() == 0
    assert BacktestResult.objects.filter(id=other.id).exists() is True


# --------------------------------------------------------------------------- #
# 与 #22 的交接：样本内窗口的边界
# --------------------------------------------------------------------------- #


def test_plan_exposes_the_in_sample_end_that_l1_should_stop_at():
    """交接给 #22：L1 的样本内回测应跑 `[start, plan.in_sample_end)`。

    参照窗口不含样本外那一段，否则衰减会被系统性低估（用样本外自己的数据去解释
    样本外）。这条把该边界变成可读的事实，而不是一句散落的约定。
    """
    plan = plan_folds(START_DATE, END_DATE)
    assert plan.in_sample_end == plan.windows[0].oos_start
    assert plan.in_sample_end == date(2026, 3, 22)


def _run(main: BacktestResult, candidate_id, executor, **overrides):
    """跑一次分折（同步外壳，与 #22 的调用形状一致）。

    **刻意不传 `parameters`**：让「参数从候选主行继承」这条回退路径成为默认，
    否则每条用例都在断言调用方自己传的实参，而继承逻辑坏掉也测不出来
    （`test_parameters_are_inherited_from_the_main_row` 钉的就是它）。

    **完整评估窗口显式传入**：它不等于主行的区间（主行只是样本内那一段）。
    """
    kwargs = {
        "candidate_id": candidate_id,
        "strategy_name": STRATEGY_NAME,
        "symbol": SYMBOL,
        "timeframe": TF,
        "in_sample_result_id": main.id,
        "evaluation_start": START_DATE,
        "evaluation_end": END_DATE,
        "fold_executor": executor,
    }
    kwargs.update(overrides)
    return run_walk_forward(**kwargs)


# --------------------------------------------------------------------------- #
# 独立对抗式复核找出的接线缺陷（B1–B4）：共同特征是「接错了照样放行」
#
# 这几条都不是「数据不足」类的结论，而是**接线错误**——它们的危险恰恰在于：不修也
# 能跑出一个 `data_sufficient=True` 且像模像样的 `decay_pct`，于是错误变成一条通过
# 记录。所以每一条都断言**当场炸**，而不是断言某个字段的值。
# --------------------------------------------------------------------------- #


def test_B1_full_range_in_sample_row_is_rejected():
    """B1(a)：样本内行覆盖了整个评估窗口 → 分母被样本外污染 → 必须炸。

    这是本票最危险的一条：主行 `sharpe_ratio` 是衰减的**分母**，而它若覆盖到样本外
    各折那一段，分母就被拉向样本外，衰减被系统性低估。实测真值 30%（应拦下）会算成
    ~25.5%（放行）——一个完全静默的 fail-open。
    """
    strategy = _strategy()
    candidate_id = uuid.uuid4()
    main = _main_row(strategy, start=START_DATE, end=END_DATE)  # 整窗口，越界
    executor = _fake_executor([1.4] * 5, strategy, candidate_id)

    with pytest.raises(ValueError, match="越过了样本内区右界"):
        _run(main, candidate_id, executor)


def test_B1_in_sample_row_ending_exactly_at_the_boundary_is_accepted():
    """边界：`main_end == plan.in_sample_end` 是**合法**的（样本内区右开）。

    反向对照，证明上一条不是「凡长区间一律拒绝」的空转断言。
    """
    strategy = _strategy()
    candidate_id = uuid.uuid4()
    main = _main_row(strategy, end=IN_SAMPLE_END)
    executor = _fake_executor([1.4] * 5, strategy, candidate_id)

    payload = _run(main, candidate_id, executor)

    assert payload["data_sufficient"] is True
    assert payload["decay_pct"] == 30.0


def test_B1_geometry_comes_from_the_evaluation_window_not_the_main_row():
    """B1(b)：几何必须由**完整评估窗口**推出，不能由主行反推。

    反推的后果：折被切在样本内区里，真正要测的样本外一段一次都没跑——而结果看起来
    完全正常（`data_sufficient=True` + 一个像模像样的 `decay_pct`）。这条断言各折
    实际区间落在**真正的样本外区**内。
    """
    strategy = _strategy()
    candidate_id = uuid.uuid4()
    main = _main_row(strategy, end=IN_SAMPLE_END)
    recorded: list[dict] = []
    executor = _fake_executor([1.4] * 5, strategy, candidate_id, recorder=recorded)

    _run(main, candidate_id, executor)

    assert len(recorded) == 5
    for call in recorded:
        assert date.fromisoformat(call["start_date"]) >= IN_SAMPLE_END, (
            "每折都必须落在样本外区（>= 样本内区右界）"
        )
    assert recorded[0]["start_date"] == IN_SAMPLE_END.isoformat()
    assert recorded[-1]["end_date"] == END_DATE.isoformat()


def test_B2_empty_candidate_id_is_rejected_before_it_can_delete_anything():
    """B2：`candidate_id` 为空 → 炸，且报的是**空值**那条原因。

    空值不是「少写个参数」：`clear_fold_results` 的判据是「非空即为一折」，而
    `filter(candidate_id=None)` 在 Django 里是 `IS NULL`——放空值进去会一次删光所有
    **不属于任何候选**的行（历史回测、样本内行……）。它是一条指向全表删除的路径。

    **必须匹配空值专属的原因**：先前写成宽泛的 `match="candidate_id"`，于是把非空
    校验整个删掉后测试照样通过——因为后面的 `uuid.UUID(str(None))` 也会抛
    `ValueError`，而消息里同样含 `candidate_id`。那测的是「最终会抛」，不是「空值在
    走到删除路径之前就被挡住」。
    """
    strategy = _strategy()
    main = _main_row(strategy)
    executor = _fake_executor([1.4] * 5, strategy, uuid.uuid4())

    for bad in (None, ""):
        with pytest.raises(ValueError, match="不能为空"):
            _run(main, bad, executor)


def test_B2_non_uuid_candidate_id_is_rejected():
    strategy = _strategy()
    main = _main_row(strategy)
    executor = _fake_executor([1.4] * 5, strategy, uuid.uuid4())

    with pytest.raises(ValueError, match="必须是 UUID"):
        _run(main, "not-a-uuid", executor)


def test_B3_main_row_carrying_the_candidate_id_survives_rerun():
    """B3：主行带 `candidate_id` 是合法的，且**必须活过重跑**。

    调用方把样本内行也标上它所属的候选，是最自然的做法。上一版假设它「结构上不带」，
    于是清理阶段把它删掉、随后 `DoesNotExist` 崩在半路，样本内证据就这么没了。
    """
    strategy = _strategy()
    candidate_id = uuid.uuid4()
    main = _main_row(strategy)
    main.candidate_id = candidate_id
    main.save(update_fields=["candidate_id"])

    _run(main, candidate_id, _fake_executor([1.4] * 5, strategy, candidate_id))
    assert BacktestResult.objects.filter(id=main.id).exists(), "第一次跑不得删掉主行"

    # 重跑（清理阶段会面对一个带 candidate_id 的主行）
    payload = _run(
        main, candidate_id, _fake_executor([1.4] * 5, strategy, candidate_id)
    )

    assert BacktestResult.objects.filter(id=main.id).exists(), "重跑也不得删掉主行"
    main.refresh_from_db()
    assert WALK_FORWARD_METRICS_KEY in (main.metrics or {})
    assert payload["decay_pct"] == 30.0


def test_B4_symbol_mismatch_is_rejected():
    """B4：主行与各折必须是同一个标的——否则衰减在比两个不同的东西。"""
    strategy = _strategy()
    candidate_id = uuid.uuid4()
    main = _main_row(strategy, symbol=SYMBOL)
    executor = _fake_executor([1.4] * 5, strategy, candidate_id)

    with pytest.raises(ValueError, match="标的不一致"):
        _run(main, candidate_id, executor, symbol="ETH/USDT")


def test_B4_timeframe_mismatch_is_rejected():
    """B4：周期不同 → 年化系数不同 → 夏普不可比。"""
    strategy = _strategy()
    candidate_id = uuid.uuid4()
    main = _main_row(strategy, timeframe=TF)
    executor = _fake_executor([1.4] * 5, strategy, candidate_id)

    with pytest.raises(ValueError, match="周期不一致"):
        _run(main, candidate_id, executor, timeframe="15m")


def test_B4_perp_in_sample_row_with_spot_folds_is_rejected():
    """B4：主行计了资金费率（永续成本口径），而本次按现货跑 → 成本模型分叉。"""
    strategy = _strategy()
    candidate_id = uuid.uuid4()
    main = _main_row(strategy, funding=12.5)  # 样本内那一行是永续
    executor = _fake_executor([1.4] * 5, strategy, candidate_id)

    with pytest.raises(ValueError, match="成本口径分叉"):
        _run(main, candidate_id, executor, is_perp=False)


def test_B4_perp_in_sample_row_with_perp_folds_is_accepted():
    """反向对照：两边都是永续 → 合法，证明上一条不是「凡有资金费率一律拒绝」。"""
    strategy = _strategy()
    candidate_id = uuid.uuid4()
    main = _main_row(strategy, funding=12.5)
    executor = _fake_executor([1.4] * 5, strategy, candidate_id)

    payload = _run(main, candidate_id, executor, is_perp=True)

    assert payload["data_sufficient"] is True


def test_W3_fold_payload_records_the_in_sample_boundary_it_was_compared_against():
    """W3：折行载荷里的样本内边界必须是真的边界，不是随手写的另一个日期。

    上一个版本把 `in_sample_end` 写成了 `window.oos_end`（变异存活），因为没有任何
    用例看它。它记错就意味着事后复核时「这一折是跟哪一段样本内比的」是错的。
    """
    strategy = _strategy()
    candidate_id = uuid.uuid4()
    main = _main_row(strategy)
    executor = _fake_executor([1.4] * 5, strategy, candidate_id)

    _run(main, candidate_id, executor)

    for row in BacktestResult.objects.filter(candidate_id=candidate_id):
        entry = row.metrics[WALK_FORWARD_METRICS_KEY]
        assert entry["in_sample_end"] == entry["start_date"], (
            "样本内参照窗口的右界必须正是该折的起点（口径 4）"
        )
        assert entry["in_sample_start"] < entry["in_sample_end"]


def test_W4_an_errored_fold_never_contributes_a_sharpe_even_if_one_is_present():
    """W4（守卫 2）：带 `error` 的折**绝不能**贡献夏普——哪怕执行器同时给了夏普。

    上一版这条纪律由两个守卫共同承担（抛错时置 `error`；见 `error` 就置
    `sharpe_ratio=None`），于是**每一个单独删掉都不转红**：真实错误 dict 里本来就没有
    `sharpe_ratio` 键，读出来自然是 `None`，把守卫 2 删掉也看不出差别。

    这里直接喂一个「既报错、又带着夏普」的返回（模拟行为不端的执行器），把守卫 2
    单独钉住：若它被删，这个 1.5 会被当成真实证据，衰减算成 25%（放行）。
    """
    strategy = _strategy()
    candidate_id = uuid.uuid4()
    main = _main_row(strategy)

    def _lying(**kwargs):
        # 报错，却同时塞了一个诱人的夏普
        return {"error": "引擎中途炸了", "sharpe_ratio": 1.5, "total_trades": 40}

    payload = _run(main, candidate_id, _lying)

    assert payload["data_sufficient"] is False
    assert payload["decay_pct"] is None, "带 error 的折不得参与衰减"
    for entry in payload["folds"]:
        assert entry["sharpe_ratio"] is None


def test_W4_a_raising_fold_records_its_error_for_attribution():
    """W4（守卫 1）：折抛错时必须**记下原因**，否则失败不可归因（D20「失败可见」）。"""
    strategy = _strategy()
    candidate_id = uuid.uuid4()
    main = _main_row(strategy)

    def _boom(**kwargs):
        raise RuntimeError("引擎炸了")

    payload = _run(main, candidate_id, _boom)

    assert payload["data_sufficient"] is False
    assert len(payload["folds"]) == 5
    for entry in payload["folds"]:
        assert entry["sharpe_ratio"] is None
        assert entry.get("error"), "每一折都要留下失败原因"


def test_W4_one_lying_fold_makes_the_whole_verdict_insufficient():
    """W4 的组合形态：五个折都报错却都带着 1.5 时，绝不能算出 25% 放行。"""
    strategy = _strategy()
    candidate_id = uuid.uuid4()
    main = _main_row(strategy)

    def _lying(**kwargs):
        return {"error": "boom", "sharpe_ratio": 1.5, "total_trades": 40}

    payload = _run(main, candidate_id, _lying)

    # 若两个守卫都没了，这里会是 True + 25.0（恰好低于 30 门槛 → 放行）
    assert payload["data_sufficient"] is False
    assert payload["decay_pct"] is None
