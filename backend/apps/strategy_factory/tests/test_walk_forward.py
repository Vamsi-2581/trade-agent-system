"""walk-forward 分折的**纯计算层**（spec #19 D4 / D15；子票 #29）。

本文件不用 `django_db`：这正是「几何与衰减的算术能在没有 DB 的情况下被穷举」那条
要求的形态（与 `test_evaluate.py` 同一纪律）。I/O 那一半在
`test_walk_forward_runner.py`。

钉住的不变式（每条都对应一个能悄悄出错的失效）：

1. **窗口几何整日对齐、无重叠、无缝隙、覆盖满样本外区间**——少一天就是少一根 bar
   的证据，重叠一天就是同一天被算两次。
2. **样本内参照窗口绝不越过样本外起点**（口径 4）。越过去会让衰减被系统性低估，
   因为那是「用样本外自己的数据解释样本外」。
3. **衰减是相对量**，且「恰好 30%」在门槛上是确定答案而不是浮点抽签——spec 的边界
   是「衰减恰 30% 拦下」，`(2.0-1.4)/2.0*100` 在 IEEE 下是 30.000000000000004。
4. **缺一折 / 切不出折 = 判不了**（`data_sufficient=False`，无 `decay_pct`），
   绝不落 0——0 会一路放行，与 #26 修掉的 NaN fail-open 同类。
5. **样本外夏普为负/零有明确行为**：>100% 的衰减，不是「算不出来」。
"""

from __future__ import annotations

import ast
from datetime import date
from pathlib import Path

import pytest

from apps.strategy_factory import walk_forward as wf_module
from apps.strategy_factory.config import FactoryConfig, WalkForwardSplitMode
from apps.strategy_factory.walk_forward import (
    DECAY_PRECISION,
    WALK_FORWARD_METRICS_KEY,
    FoldPlanReason,
    WalkForwardWindowRole,
    aggregate_walk_forward,
    compute_decay_pct,
    plan_folds,
)

# --------------------------------------------------------------------------- #
# 纯度守卫（与 test_evaluate.py 同一条纪律：算术层不该依赖 Django）
# --------------------------------------------------------------------------- #

FORBIDDEN_IMPORT_ROOTS = ("django", "rest_framework", "celery")


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
    return found


def test_pure_module_does_not_import_django():
    """分折的算术必须能在没有 Django/DB 的情况下被读懂、被测试。

    用 AST 而不是字符串匹配：docstring 里到处在讲 Django，字符串匹配会被它们骗过去。
    """
    imports = _imported_modules(Path(wf_module.__file__))
    roots = {name.split(".")[0] for name in imports}
    assert not (roots & set(FORBIDDEN_IMPORT_ROOTS)), (
        f"walk_forward.py 引入了 {sorted(roots & set(FORBIDDEN_IMPORT_ROOTS))}"
    )


def test_does_not_import_the_runner_or_models():
    """纯层不依赖 I/O 层——否则「零 I/O」这条就只是文档里的一句话。"""
    imports = _imported_modules(Path(wf_module.__file__))
    assert not any("walk_forward_runner" in name for name in imports)
    assert not any(name.endswith("strategy_factory.models") for name in imports)


def test_reuses_the_shared_metric_usability_judgement():
    """不自己再写一份「这个数能不能用」：两份判据迟早分家，NaN 会从新那侧漏过去。"""
    from apps.strategy_factory import evaluate as evaluate_module

    imports = _imported_modules(Path(wf_module.__file__))
    assert any(name.endswith("strategy_factory.evaluate") for name in imports)
    assert wf_module.is_usable_metric_number is evaluate_module.is_usable_metric_number


# --------------------------------------------------------------------------- #
# 窗口几何
# --------------------------------------------------------------------------- #

#: 100 天区间 / 5 折 / 样本外占 20% → 样本外 20 天，正好每折 4 天，样本内 80 天。
GEOMETRY_START = date(2026, 1, 1)
GEOMETRY_END = date(2026, 4, 11)  # 100 天


def test_default_geometry_is_five_equal_folds_covering_the_oos_region():
    plan = plan_folds(GEOMETRY_START, GEOMETRY_END)

    assert plan.data_sufficient is True
    assert plan.total_days == 100
    assert plan.oos_total_days == 20
    assert plan.in_sample_span_days == 80
    assert len(plan.windows) == 5
    assert [w.days for w in plan.windows] == [4, 4, 4, 4, 4]


def test_oos_windows_are_contiguous_and_cover_the_region_exactly():
    """无缝隙、无重叠、且正好铺满 `[in_sample_end, end_date)`。

    这三条一起才说明「每折各测了一段互不相同的未来」：漏一段 = 有一段行情没人看，
    重叠 = 同一段行情被算两次（等权聚合下等于给它加权）。
    """
    plan = plan_folds(GEOMETRY_START, GEOMETRY_END)

    assert plan.windows[0].oos_start == plan.in_sample_end
    assert plan.windows[-1].oos_end == GEOMETRY_END
    for prev, cur in zip(plan.windows, plan.windows[1:]):
        assert prev.oos_end == cur.oos_start, "折之间既不能有缝隙也不能有重叠"
    assert sum(w.days for w in plan.windows) == plan.oos_total_days


def test_fold_indices_are_zero_based_and_sequential():
    plan = plan_folds(GEOMETRY_START, GEOMETRY_END)
    assert [w.fold_index for w in plan.windows] == [0, 1, 2, 3, 4]


def test_in_sample_window_never_crosses_into_the_oos_region():
    """口径 4：参照窗口的右端恒等于该折起点（滚动与锚定两种模式都要满足）。

    越过去 = 用样本外自己的数据去解释样本外 → 衰减被系统性低估。
    """
    for mode in WalkForwardSplitMode.values():
        plan = plan_folds(GEOMETRY_START, GEOMETRY_END, mode=mode)
        for window in plan.windows:
            assert window.in_sample_end == window.oos_start, (mode, window)
            assert window.in_sample_start < window.in_sample_end, (mode, window)


def test_rolling_mode_slides_a_fixed_length_in_sample_window():
    plan = plan_folds(GEOMETRY_START, GEOMETRY_END, mode="rolling")
    spans = {(w.in_sample_end - w.in_sample_start).days for w in plan.windows}
    assert spans == {plan.in_sample_span_days}, "滚动模式：样本内窗口长度固定"


def test_anchored_mode_grows_the_in_sample_window_from_the_origin():
    plan = plan_folds(GEOMETRY_START, GEOMETRY_END, mode="anchored")
    for window in plan.windows:
        assert window.in_sample_start == GEOMETRY_START, "锚定模式：起点不动"
    lengths = [(w.in_sample_end - w.in_sample_start).days for w in plan.windows]
    assert lengths == sorted(lengths) and lengths[0] < lengths[-1], "锚定：窗口递增"


def test_remainder_days_go_to_the_earliest_folds_so_lengths_stay_even():
    """余数前倾，而不是堆在末尾。

    衰减是各折**等权**聚合的。把余数堆到最后一折会让它明显更长，等于让聚合口径被
    窗口长度悄悄加权——而这件事没有任何一行代码会报错。
    """
    # 100 天 / 7 折 / 样本外 20 天 → 20 = 3+3+3+3+3+3+2，余数落在前面
    plan = plan_folds(GEOMETRY_START, GEOMETRY_END, folds=7)
    days = [w.days for w in plan.windows]
    assert sum(days) == plan.oos_total_days
    assert days == sorted(days, reverse=True), days
    assert max(days) - min(days) <= 1, days


def test_geometry_scales_with_config_not_hardcoded():
    """折数与样本外占比由配置驱动（票面验收第 5 条）。"""
    cfg = FactoryConfig(wf_folds=10, wf_oos_fraction=0.5)
    plan = plan_folds(GEOMETRY_START, GEOMETRY_END, config=cfg)
    assert plan.folds == 10
    assert plan.oos_total_days == 50
    assert len(plan.windows) == 10


@pytest.mark.parametrize("mode", ["rolling", "anchored"])
def test_explicit_arguments_override_config(mode):
    cfg = FactoryConfig(wf_folds=5, wf_oos_fraction=0.2)
    plan = plan_folds(
        GEOMETRY_START, GEOMETRY_END, folds=3, oos_fraction=0.3, mode=mode, config=cfg
    )
    assert plan.folds == 3
    assert plan.oos_fraction == 0.3
    assert plan.split_mode == mode
    assert plan.oos_total_days == 30


@pytest.mark.parametrize(
    ("folds", "fraction", "mode"),
    [
        (1, 0.2, "rolling"),
        (0, 0.2, "rolling"),
        (5, 0, "rolling"),
        (5, 1, "rolling"),
        (5, -0.1, "rolling"),
        (5, 1.5, "rolling"),
        (5, 0.2, "rollling"),
        (5, 0.2, ""),
        (5, 0.2, None),
    ],
)
def test_illegal_geometry_arguments_raise(folds, fraction, mode):
    """显式入参再守一遍边界：不能只依赖配置面的 `__post_init__`。

    `None` 的 mode 走的是配置默认（合法），所以它在下面单独验，不进这个参数表。
    """
    if mode is None:
        plan = plan_folds(
            GEOMETRY_START, GEOMETRY_END, folds=folds, oos_fraction=fraction
        )
        assert plan.split_mode == WalkForwardSplitMode.ROLLING.value
        return
    with pytest.raises(ValueError):
        plan_folds(
            GEOMETRY_START, GEOMETRY_END, folds=folds, oos_fraction=fraction, mode=mode
        )


def test_split_mode_fraction_uses_exact_arithmetic_not_float_multiplication():
    """样本外天数用 `Fraction` 算，不用浮点乘。

    `0.29 * 100` 在 IEEE 下是 28.999999999999996，`int()` 会静默少一天。少一天就是
    少一根 bar 的证据，而没有任何一行代码会报错。
    """
    plan = plan_folds(date(2026, 1, 1), date(2026, 4, 11), oos_fraction=0.29)
    assert plan.oos_total_days == 29
    assert 0.29 * 100 < 29  # 记录这个浮点事实：换回浮点乘就会少一天


# --------------------------------------------------------------------------- #
# 数据不足 → 判不了（不是判通过）
# --------------------------------------------------------------------------- #


def test_zero_length_range_is_insufficient_not_an_exception():
    """「数据不够」是一个结论，不是一次崩溃。"""
    plan = plan_folds(date(2026, 1, 1), date(2026, 1, 1))
    assert plan.data_sufficient is False
    assert plan.windows == ()
    assert plan.reason == FoldPlanReason.RANGE_TOO_SHORT.value


def test_inverted_range_is_insufficient():
    plan = plan_folds(date(2026, 2, 1), date(2026, 1, 1))
    assert plan.data_sufficient is False
    assert plan.windows == ()


@pytest.mark.parametrize(
    ("total_days", "folds", "fraction"),
    [
        (3, 5, 0.2),  # 样本外 0 天
        (10, 5, 0.2),  # 样本外 2 天 < 5 折
        (20, 5, 0.2),  # 样本外 4 天 < 5 折
    ],
)
def test_too_short_oos_region_is_insufficient(total_days, folds, fraction):
    """样本外区切不出「每折至少一天」的 N 折 → 判不了，不放行。

    每折至少要有一天，否则「折」这个单位就没有意义（0 天的折测不出任何东西，却会
    以「一折」的身份进入等权平均）。
    """
    start = date(2026, 1, 1)
    plan = plan_folds(
        start,
        date.fromordinal(start.toordinal() + total_days),
        folds=folds,
        oos_fraction=fraction,
    )
    assert plan.data_sufficient is False
    assert plan.windows == ()
    assert plan.reason == FoldPlanReason.OOS_REGION_TOO_SHORT.value


def test_insufficient_geometry_aggregates_without_a_decay_number():
    """切不出折时聚合层**绝不产出** `decay_pct`——它是「判不了」，不是「衰减 0」。"""
    plan = plan_folds(date(2026, 1, 1), date(2026, 1, 1))
    payload = aggregate_walk_forward(
        candidate_id="c-1", is_sharpe=2.0, plan=plan, fold_rows=()
    )
    assert payload["data_sufficient"] is False
    assert payload["decay_pct"] is None
    assert payload["oos_sharpe"] is None
    assert payload["insufficient_reason"] == FoldPlanReason.RANGE_TOO_SHORT.value


def test_short_oos_region_reason_is_distinguishable_from_short_range():
    """两种「数据不足」的原因码必须分得开。

    它们要人做的事不同：`range_too_short` 是「这段区间本身没数据」，而
    `oos_region_too_short` 是「区间有数据，但末尾那段不够切 N 折」——后者通常该调
    `wf_folds` 或 `wf_oos_fraction`，前者该去补数据。混成一个码会让归因变成猜。
    """
    plan = plan_folds(date(2026, 1, 1), date(2026, 1, 2))
    payload = aggregate_walk_forward(
        candidate_id="c-1", is_sharpe=2.0, plan=plan, fold_rows=()
    )
    assert payload["insufficient_reason"] == FoldPlanReason.OOS_REGION_TOO_SHORT.value


# --------------------------------------------------------------------------- #
# 衰减：相对口径 + 边界确定性
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("is_sharpe", "oos_sharpe", "expected"),
    [
        (2.0, 2.0, 0.0),
        (2.0, 1.4, 30.0),
        (2.0, 1.0, 50.0),
        (1.5, 1.05, 30.0),
        (1.5, 1.5, 0.0),
        (4.0, 3.0, 25.0),
        (1.0, 0.5, 50.0),
    ],
)
def test_decay_is_relative_not_absolute(is_sharpe, oos_sharpe, expected):
    """相对口径：`(is - oos) / is × 100`。

    绝对差读法下 `(2.0, 1.4)` 会是 60（点），而门槛 30 就变成「允许夏普掉 30」——
    本系统的夏普量级在 1.5 上下，那等于闸门永不触发，正是 spec D12 要修的那类空转。
    """
    assert compute_decay_pct(is_sharpe, oos_sharpe) == pytest.approx(expected, abs=1e-9)


@pytest.mark.parametrize(
    ("is_sharpe", "oos_sharpe"),
    [(2.0, 1.4), (1.5, 1.05), (4.0, 2.8), (1.0, 0.7), (3.0, 2.1), (7.0, 4.9)],
)
def test_exactly_thirty_percent_is_blocked_and_float_stable(is_sharpe, oos_sharpe):
    """「衰减恰 30%」必须在门槛上给出**确定的**答案，而不是一次浮点抽签。

    未定精度时 `(2.0-1.4)/2.0*100` 是 `30.000000000000004`，而 `(1.5-1.05)/1.5*100`
    是 `30.0`——同一个「恰好 30%」在不同数字上落在门槛两侧。spec 的边界是「恰 30%
    拦下」，所以两边都必须是 30.0 且 `>= 30` 成立。
    """
    decay = compute_decay_pct(is_sharpe, oos_sharpe)
    assert decay == 30.0, decay
    assert decay >= 30.0, "恰 30% 必须被拦下"


def test_just_below_thirty_percent_passes():
    assert compute_decay_pct(2.0, 1.4002) < 30.0


def test_rounding_only_tightens_the_gate_never_loosens_it():
    """定精度对**放行**方向是保守的：舍入只会让边界更严。

    这条钉的是方向安全性——如果舍入能把一个 30.0000004% 的衰减变成 29.9999% 放行，
    那它就是一次静默的 fail-open。
    """
    # 真实值略高于 30%：必须仍被判为 >= 30（拦下），不能被舍入到门槛以下。
    for is_sharpe, oos_sharpe in [(2.0, 1.3999999), (3.0, 2.0999999)]:
        decay = compute_decay_pct(is_sharpe, oos_sharpe)
        assert decay >= 30.0, (is_sharpe, oos_sharpe, decay)


@pytest.mark.parametrize("oos_sharpe", [-0.5, -2.0, 0.0])
def test_negative_or_zero_oos_sharpe_gives_a_meaningful_over_hundred_decay(oos_sharpe):
    """样本外亏钱时衰减该是 >100%，不是「算不出来」。

    票面验收第 4 条点名了「样本外夏普为负/为零」要有明确行为。把它当 `None` 会让
    一条在样本外亏钱的策略落进「证据不足」，读起来像「数据不够」——而真相是
    「它在样本外不行」，那是更该被看见的结论。
    """
    decay = compute_decay_pct(2.0, oos_sharpe)
    assert decay is not None
    assert decay >= 100.0
    assert decay >= 30.0, "必然超过任何合法上限"


@pytest.mark.parametrize("is_sharpe", [None, 0.0, -1.0, float("nan"), float("inf")])
def test_unusable_in_sample_sharpe_has_no_decay(is_sharpe):
    """分母不可用 → 判不了。

    `<=0` 也算不可用：负夏普的比值会把「衰减」的符号翻转，变成「越差衰减越小」——
    比算不出来更危险。`inf` 会让比值恒为 -inf。
    """
    assert compute_decay_pct(is_sharpe, 1.0) is None


@pytest.mark.parametrize(
    "oos_sharpe", [None, float("nan"), float("inf"), float("-inf"), True]
)
def test_unusable_oos_sharpe_has_no_decay(oos_sharpe):
    """NaN 会让任何比较静默为假（闸门于是放行），所以它不是「一个数」。

    `True` 也在这里：`bool` 是 `int` 的子类，`True` 不该被当成夏普 1.0。
    """
    assert compute_decay_pct(2.0, oos_sharpe) is None


def test_decay_precision_is_declared_and_reasonable():
    assert isinstance(DECAY_PRECISION, int)
    assert 0 < DECAY_PRECISION <= 12


# --------------------------------------------------------------------------- #
# 聚合载荷（D4 的形状）
# --------------------------------------------------------------------------- #


def _fold_row(index: int, sharpe: float | None, trades: int = 40) -> dict:
    return {
        "fold_index": index,
        "window_role": WalkForwardWindowRole.OOS.value,
        "result_id": f"r-{index}",
        "start_date": f"2026-02-{18 + index:02d}",
        "end_date": f"2026-02-{19 + index:02d}",
        "sharpe_ratio": sharpe,
        "total_trades": trades,
    }


def test_aggregate_shape_matches_d4():
    """D4 点名的键一个不少：`is_sharpe` / `oos_sharpe` / `decay_pct` / `folds[]`。"""
    plan = plan_folds(GEOMETRY_START, GEOMETRY_END)
    rows = [_fold_row(i, 1.4) for i in range(5)]

    payload = aggregate_walk_forward(
        candidate_id="cand-uuid", is_sharpe=2.0, plan=plan, fold_rows=rows
    )

    assert payload["is_sharpe"] == 2.0
    assert payload["oos_sharpe"] == pytest.approx(1.4)
    assert payload["decay_pct"] == 30.0
    assert payload["data_sufficient"] is True
    assert payload["fold_count"] == 5
    assert len(payload["folds"]) == 5
    for entry in payload["folds"]:
        # D4 逐字要求的五个键
        assert {
            "fold_index",
            "start_date",
            "end_date",
            "sharpe_ratio",
            "total_trades",
        } <= set(entry)
        # 用户故事 11：每个 fold 的结果可复核、可重算
        assert entry["result_id"]


def test_oos_sharpe_is_the_equal_weighted_mean_of_folds():
    """各折等权算术均值。

    刻意不用中位数：中位数会把「五折里两折崩了」平滑掉，而那正是风险承担者最该看见
    的信号。每折原始值都留在 `folds[]` 里供事后复核。
    """
    plan = plan_folds(GEOMETRY_START, GEOMETRY_END)
    rows = [_fold_row(i, s) for i, s in enumerate([2.0, 2.0, 2.0, 0.0, -4.0])]

    payload = aggregate_walk_forward(
        candidate_id="c", is_sharpe=2.0, plan=plan, fold_rows=rows
    )

    assert payload["oos_sharpe"] == pytest.approx(0.4)
    assert payload["decay_pct"] == 80.0
    # 中位数读法会给出 2.0（衰减 0% → 放行），那是这个用例存在的理由。
    assert payload["decay_pct"] > 30.0


def test_payload_is_json_safe():
    """载荷要能原样进 `JSONField`。"""
    import json

    plan = plan_folds(GEOMETRY_START, GEOMETRY_END)
    rows = [_fold_row(i, 1.4) for i in range(5)]
    payload = aggregate_walk_forward(
        candidate_id="c", is_sharpe=2.0, plan=plan, fold_rows=rows
    )
    assert json.loads(json.dumps(payload, sort_keys=True)) == payload


def test_payload_carries_the_metrics_key_name_used_by_the_runner():
    assert WALK_FORWARD_METRICS_KEY == "walk_forward"


# --------------------------------------------------------------------------- #
# 折数不齐 / 某折算不出夏普 → 判不了
# --------------------------------------------------------------------------- #


def test_fold_count_mismatch_is_insufficient():
    """少跑了折 → 不给结论。

    在缺折的集合上算衰减，等于用一个谁都不知道的样本集去回答「样本外还行不行」。
    """
    plan = plan_folds(GEOMETRY_START, GEOMETRY_END)
    rows = [_fold_row(i, 1.4) for i in range(4)]  # 只有 4 折，期望 5

    payload = aggregate_walk_forward(
        candidate_id="c", is_sharpe=2.0, plan=plan, fold_rows=rows
    )

    assert payload["data_sufficient"] is False
    assert payload["decay_pct"] is None
    assert payload["insufficient_reason"] == "fold_count_mismatch"


@pytest.mark.parametrize("bad", [None, float("nan"), float("inf"), float("-inf")])
def test_any_unusable_fold_sharpe_makes_the_whole_verdict_insufficient(bad):
    """任一折算不出有限夏普 → 整轮判不了（**不放行**）。

    把它当 0 参与均值会让衰减静默变小 → 放行，正是 #26 修掉的那类 fail-open。
    """
    plan = plan_folds(GEOMETRY_START, GEOMETRY_END)
    rows = [_fold_row(i, 1.4) for i in range(5)]
    rows[3] = _fold_row(3, bad)

    payload = aggregate_walk_forward(
        candidate_id="c", is_sharpe=2.0, plan=plan, fold_rows=rows
    )

    assert payload["data_sufficient"] is False
    assert payload["decay_pct"] is None
    assert payload["insufficient_reason"] == "fold_sharpe_unusable"


def test_a_failed_fold_row_without_result_id_still_makes_the_verdict_insufficient():
    """一折取数失败（没有 result_id、sharpe 为 None）同样落「证据不足」。"""
    plan = plan_folds(GEOMETRY_START, GEOMETRY_END)
    rows = [_fold_row(i, 1.4) for i in range(5)]
    rows[1] = {
        "fold_index": 1,
        "window_role": WalkForwardWindowRole.OOS.value,
        "result_id": None,
        "sharpe_ratio": None,
        "total_trades": None,
        "error": "未能获取 K 线",
    }

    payload = aggregate_walk_forward(
        candidate_id="c", is_sharpe=2.0, plan=plan, fold_rows=rows
    )

    assert payload["data_sufficient"] is False
    assert payload["decay_pct"] is None


@pytest.mark.parametrize("is_sharpe", [None, 0.0, -2.0, float("nan")])
def test_unusable_in_sample_sharpe_aggregates_to_insufficient(is_sharpe):
    plan = plan_folds(GEOMETRY_START, GEOMETRY_END)
    rows = [_fold_row(i, 1.4) for i in range(5)]

    payload = aggregate_walk_forward(
        candidate_id="c", is_sharpe=is_sharpe, plan=plan, fold_rows=rows
    )

    assert payload["data_sufficient"] is False
    assert payload["decay_pct"] is None
    assert payload["insufficient_reason"] == "in_sample_sharpe_unusable"


def test_insufficient_payload_still_records_the_evidence_it_had():
    """判不了也要留下依据：用户故事 5/11 要求「为什么被刷掉」答得出来。"""
    plan = plan_folds(GEOMETRY_START, GEOMETRY_END)
    rows = [_fold_row(i, 1.4) for i in range(4)]

    payload = aggregate_walk_forward(
        candidate_id="c", is_sharpe=2.0, plan=plan, fold_rows=rows
    )

    assert payload["fold_count"] == 4
    assert payload["expected_fold_count"] == 5
    assert len(payload["folds"]) == 4
    assert payload["config_snapshot"]["wf_folds"] == 5
    assert payload["split"]["data_sufficient"] is True


def test_config_snapshot_records_which_thresholds_were_in_force():
    """判定结论要回答「当时用的是哪套参数」（配置面可快照的理由）。"""
    cfg = FactoryConfig(wf_folds=3, wf_oos_fraction=0.5)
    plan = plan_folds(GEOMETRY_START, GEOMETRY_END, config=cfg)
    rows = [_fold_row(i, 1.4) for i in range(3)]

    payload = aggregate_walk_forward(
        candidate_id="c", is_sharpe=2.0, plan=plan, fold_rows=rows, config=cfg
    )

    assert payload["config_snapshot"]["wf_folds"] == 3
    assert payload["config_snapshot"]["oos_decay_max_pct"] == 30.0
    assert payload["decay_pct"] == 30.0


def test_split_payload_is_json_safe_and_self_describing():
    import json

    plan = plan_folds(GEOMETRY_START, GEOMETRY_END)
    payload = plan.as_payload()
    assert json.loads(json.dumps(payload, sort_keys=True)) == payload
    assert payload["split_mode"] == "rolling"
    assert payload["data_sufficient"] is True
    assert payload["in_sample_end"] == plan.windows[0].oos_start.isoformat()
