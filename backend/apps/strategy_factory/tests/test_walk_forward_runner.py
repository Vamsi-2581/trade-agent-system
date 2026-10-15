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
RANGE_DAYS = 100
START_DATE = date(2026, 1, 1)
END_DATE = date(2026, 4, 11)

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


def _main_row(strategy: Strategy, sharpe: float | None = 2.0) -> BacktestResult:
    """候选主行（= L1 样本内那一次回测的产物）。"""
    return BacktestResult.objects.create(
        strategy=strategy,
        symbol=SYMBOL,
        timeframe=TF,
        start_date=START_DATE,
        end_date=END_DATE,
        initial_capital=Decimal("10000"),
        final_capital=Decimal("11000"),
        total_return_pct=10.0,
        sharpe_ratio=sharpe,
        total_trades=120,
        parameters={"fast": 7, "slow": 25},
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
    for row in fold_rows:
        assert row.candidate_id == candidate_id, "candidate_id 是分组键，折间必须一致"
        entry = (row.metrics or {})[WALK_FORWARD_METRICS_KEY]
        assert entry["fold_index"] in range(5)
        assert entry["window_role"] == WalkForwardWindowRole.OOS.value
        assert entry["split_mode"] == "rolling"
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
    """区间切不出折时**一次回测都不跑**（漏斗下限 = 什么都不发生）。"""
    strategy = _strategy()
    candidate_id = uuid.uuid4()
    main = _main_row(strategy)
    main.end_date = date(2026, 1, 2)  # 1 天区间，切不出 5 折
    main.save(update_fields=["end_date"])
    recorded: list[dict] = []
    executor = _fake_executor([1.4] * 5, strategy, candidate_id, recorder=recorded)

    payload = _run(main, candidate_id, executor)

    assert recorded == [], "切不出折就不该跑任何一折"
    assert payload["data_sufficient"] is False
    assert payload["decay_pct"] is None
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


def _run(main: BacktestResult, candidate_id, executor):
    """跑一次分折（同步外壳；统一从主行取参数，与 #22 的调用形状一致）。"""
    return run_walk_forward(
        candidate_id=candidate_id,
        strategy_name=STRATEGY_NAME,
        symbol=SYMBOL,
        timeframe=TF,
        in_sample_result_id=main.id,
        parameters=main.parameters or {},
        fold_executor=executor,
    )
