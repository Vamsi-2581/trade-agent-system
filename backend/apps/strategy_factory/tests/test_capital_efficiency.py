"""资金效率纯模块的边界（spec #30 D6 / D7；子票 #33）。

半个文件钉**口径**（时间加权 vs 期末名义、时间口径与金额口径分开、空仓间隙显式、
边界归属），半个文件钉**判不了**（本金 / 窗口 / 笔数三道门都不是 0）。

写法照 `test_walk_forward.py`：表驱动 + 每条边界一个名字说清它防的是什么 bug。
全部用例不碰库（本模块零 DB、零 Django 模型），所以这里一个 fixture 都不需要。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from apps.strategy_factory.capital_efficiency import (
    CAPITAL_EFFICIENCY_METRICS_KEY,
    CAPITAL_EFFICIENCY_PRECISION,
    CAPITAL_EFFICIENCY_VERSION,
    CapitalEfficiencyReason,
    EfficiencyWindow,
    OccupancyInterval,
    OccupancyIntervalKind,
    capital_efficiency_keys,
    compute_capital_efficiency,
)
from apps.strategy_factory.config import CONFIG

#: 窗口与区间的时刻都钉死在一个确定的 UTC 时刻上：本模块的口径全是时长比，测试不能
#: 依赖运行时刻（否则「恰好在窗口边界上」这类断言会随运行时间漂移）。
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)

#: 全部用例共用的本金。
CAPITAL = Decimal("10000")


def _window(days: int = 1) -> EfficiencyWindow:
    return EfficiencyWindow(start=T0, end=T0 + timedelta(days=days))


def _held(start: datetime, end: datetime, notional: str, pnl: str | None = None):
    return OccupancyInterval(
        start=start,
        end=end,
        notional=Decimal(notional),
        pnl=None if pnl is None else Decimal(pnl),
        kind=OccupancyIntervalKind.HELD.value,
    )


def _point(at: datetime, notional: str, pnl: str | None = None):
    """会话侧那种零时长点观测。"""
    return OccupancyInterval(
        start=at,
        end=at,
        notional=Decimal(notional),
        pnl=None if pnl is None else Decimal(pnl),
        kind=OccupancyIntervalKind.POINT.value,
    )


# --------------------------------------------------------------------------- #
# 载荷契约：键名与版本
# --------------------------------------------------------------------------- #


def test_the_metrics_key_is_the_one_backend_and_frontend_share():
    """`metrics` 上那个键只有一处定义。

    两处消费方（#35 回测侧、#38 会话侧）与前端读的是同一个字符串；在这里写死一份，
    改名会先在这里红，而不是先在某一个界面悄悄变成 `undefined`。
    """
    assert CAPITAL_EFFICIENCY_METRICS_KEY == "capital_efficiency"


def test_the_payload_carries_a_version_and_a_threshold_snapshot():
    """载荷自足：口径快照跟着数字走（spec D8 对回测侧的同一条要求）。

    少了 `sample_threshold` 与版本号，半年后没人答得出「这个 sample_sufficient 是哪条
    门槛判的」，而门槛是会改的。
    """
    result = compute_capital_efficiency(
        [_held(T0, T0 + DAY, "10000", "500")], _window(), CAPITAL
    )
    payload = result.as_payload()

    assert CAPITAL_EFFICIENCY_VERSION >= 1
    assert CAPITAL_EFFICIENCY_PRECISION >= 4
    assert payload["sample_threshold"] == CONFIG.min_trades
    assert payload["sample_sufficient"] is False  # 1 笔 < 30 笔


def test_the_metric_key_set_is_the_fixed_contract():
    """六个指标键是**固定契约**：跨侧改名必须在这里红。

    #35 与 #38 各自 `payload[...]` 取用，没有任何东西会因为改名而报错——只有一个测试
    把这里与 `as_payload()` 的键集对齐时才会炸。
    """
    expected = capital_efficiency_keys()
    assert set(expected) == {
        "avg_capital_used",
        "capital_utilization",
        "idle_pct",
        "time_in_market_pct",
        "return_per_capital_used_pct",
        "sample_sufficient",
    }
    for key in expected:
        assert key in _full_position().as_payload()


def _full_position() -> object:
    return compute_capital_efficiency(
        [_held(T0, T0 + DAY, "10000", "500")], _window(), CAPITAL
    )


# --------------------------------------------------------------------------- #
# 口径 1：占用是时间加权的，不是期末名义值
# --------------------------------------------------------------------------- #


def test_full_position_all_window_is_100_percent_utilization():
    """满仓跑满全程 = 100% 利用率、0% 闲置（口径 1 的基准点）。"""
    result = compute_capital_efficiency(
        [_held(T0, T0 + DAY, "10000", "500")], _window(), CAPITAL
    )

    assert result.computed is True
    assert result.avg_capital_used == Decimal("10000")
    assert result.capital_utilization == pytest.approx(100.0)
    assert result.idle_pct == pytest.approx(0.0)
    assert result.time_in_market_pct == pytest.approx(100.0)


def test_half_position_yields_half_the_utilization_of_a_full_position():
    """满仓 vs 半仓：时间加权平均必须差一倍（用户故事 13 的核心场景）。

    同样的窗口、同样的本金，只有仓位大小不同。差额若不是恰好 2 倍，说明加权里漏了
    金额或漏了时长。
    """
    full = compute_capital_efficiency(
        [_held(T0, T0 + DAY, "10000")], _window(), CAPITAL
    )
    half = compute_capital_efficiency([_held(T0, T0 + DAY, "5000")], _window(), CAPITAL)

    assert full.avg_capital_used == Decimal("10000")
    assert half.avg_capital_used == Decimal("5000")
    assert half.capital_utilization == pytest.approx(full.capital_utilization / 2)
    assert half.idle_pct == pytest.approx(50.0)


def test_a_short_full_position_is_not_measured_as_half_utilization():
    """**本条钉住「期末名义值」这个 bug**（用户故事 13 原话）。

    满仓只开半天、之后空仓。期末名义值是 0（已清仓），若按它算会把「把全部本金压上去
    半个窗口」读成「几乎没用钱」——恰好把高风险读成高效率。正确值是 50%。
    """
    result = compute_capital_efficiency(
        [_held(T0, T0 + DAY / 2, "10000")], _window(), CAPITAL
    )

    assert result.avg_capital_used == Decimal("5000")
    assert result.capital_utilization == pytest.approx(50.0)


def test_time_weighting_not_equal_weighting():
    """等权（把每段持仓当成同样长）不等于时间加权。

    同样两笔、错开时点：「一笔 3/4 窗 + 一笔 1/4 窗」的真实资金压力与「两笔各半窗」
    不同。两者的时间加权平均必须能分开——这是口径 1 区别于「平均每笔仓位」的地方。
    """
    long_then_short = compute_capital_efficiency(
        [
            _held(T0, T0 + DAY * 3 / 4, "6000"),
            _held(T0 + DAY * 3 / 4, T0 + DAY, "2000"),
        ],
        _window(),
        CAPITAL,
    )
    even_split = compute_capital_efficiency(
        [
            _held(T0, T0 + DAY / 2, "6000"),
            _held(T0 + DAY / 2, T0 + DAY, "2000"),
        ],
        _window(),
        CAPITAL,
    )

    assert long_then_short.avg_capital_used == Decimal("5000")
    assert even_split.avg_capital_used == Decimal("4000")
    assert long_then_short.avg_capital_used != even_split.avg_capital_used


def test_overlapping_holds_are_summed_not_averaged():
    """两笔重叠持仓的占用是**相加**，不是平均。

    同一时间开着两笔各 5000 = 真金白银占着 10000。取平均会把「同时扛两份风险」读成
    「只扛一份」，而那是本模块最不该犯的一类错。
    """
    result = compute_capital_efficiency(
        [
            _held(T0, T0 + DAY, "5000"),
            _held(T0 + DAY / 2, T0 + DAY, "5000"),
        ],
        _window(),
        CAPITAL,
    )

    # 第一段半天 5000、后半天 10000 → 平均 7500。
    assert result.avg_capital_used == Decimal("7500")
    assert result.capital_utilization == pytest.approx(75.0)


# --------------------------------------------------------------------------- #
# 口径 3：空仓间隙是显式的，不是空隙
# --------------------------------------------------------------------------- #


def test_idle_gaps_count_as_zero_occupancy_not_as_skipped():
    """空仓那段时间占 0，且**仍按实际时长进加权**（口径 3）。

    把空仓间隙当「跳过」，`capital_utilization` 会变成「有仓位时的平均仓位大小」，
    于是「只开 1% 时间的满仓策略」与「全程半仓」拿到同一个数——而这两者的风险差 100 倍。
    """
    result = compute_capital_efficiency(
        [_held(T0, T0 + DAY / 10, "10000")], _window(), CAPITAL
    )

    assert result.time_in_market_pct == pytest.approx(10.0)
    assert result.capital_utilization == pytest.approx(10.0)
    assert result.idle_pct == pytest.approx(90.0)


def test_utilization_cannot_exceed_one_hundred_percent():
    """占用超过本金时利用率 >100%，但不该被悄悄截断。

    不截断是为了让「超额占用」可见（例如杠杆把两笔同时开的仓位加起来超过本金）。
    真截断了，超额的策略与规规矩矩的策略在同一个数上看起来一样。
    """
    result = compute_capital_efficiency(
        [
            _held(T0, T0 + DAY, "7000"),
            _held(T0, T0 + DAY, "7000"),
        ],
        _window(),
        CAPITAL,
    )

    assert result.capital_utilization == pytest.approx(140.0)
    assert result.idle_pct == pytest.approx(-40.0)


# --------------------------------------------------------------------------- #
# 口径 2：在仓时长与金额口径严格分开
# --------------------------------------------------------------------------- #


def test_time_in_market_and_capital_utilization_are_two_different_questions():
    """半仓开满全程：时间 100%、金额 50%（口径 2 的对照点）。

    混成一个数会让「轻仓长持」与「重仓短打」这两种风险完全不同的策略看起来一样。
    """
    result = compute_capital_efficiency(
        [_held(T0, T0 + DAY, "5000")], _window(), CAPITAL
    )

    assert result.time_in_market_pct == pytest.approx(100.0)
    assert result.capital_utilization == pytest.approx(50.0)


def test_full_position_half_the_time_is_time_fifty_percent():
    """满仓只开半天：时间 50%、金额 50%（按全天窗）。

    两个数在这里相等是**巧合**而不是恒等式——上面的用例已经证明它们能分开。
    """
    result = compute_capital_efficiency(
        [_held(T0, T0 + DAY / 2, "10000")], _window(), CAPITAL
    )

    assert result.time_in_market_pct == pytest.approx(50.0)
    assert result.capital_utilization == pytest.approx(50.0)


# --------------------------------------------------------------------------- #
# 口径 4：窗口边界归属
# --------------------------------------------------------------------------- #


def test_a_hold_opened_before_the_window_is_clipped_to_the_window():
    """窗口开始前已开仓 → 只计窗口内那一截。

    否则分子取自一个比窗口更大的区间，而分母是窗口总时长——`time_in_market_pct` 可能
    >100%，加权平均也不再与窗口同源。
    """
    window = _window()
    result = compute_capital_efficiency(
        [_held(T0 - DAY, T0 + DAY, "10000")], window, CAPITAL
    )

    assert result.time_in_market_pct == pytest.approx(100.0)
    assert result.avg_capital_used == Decimal("10000")


def test_a_hold_closed_after_the_window_is_clipped_to_the_window():
    """窗口结束后才平仓 → 只计窗口内那一截（与上一条对称）。"""
    window = _window()
    result = compute_capital_efficiency(
        [_held(T0 + DAY / 2, T0 + DAY * 3, "10000")], window, CAPITAL
    )

    assert result.time_in_market_pct == pytest.approx(50.0)
    assert result.avg_capital_used == Decimal("5000")


def test_an_interval_entirely_before_the_window_is_dropped():
    """整段在窗口外的区间不算「一笔窗口内的成交」。

    它不改变窗口里任何一秒的资金状态，却会让笔数与样本标注失真。
    """
    result = compute_capital_efficiency(
        [
            _held(T0 - DAY * 2, T0 - DAY, "10000"),
            _held(T0, T0 + DAY / 2, "10000"),
        ],
        _window(),
        CAPITAL,
    )

    assert result.trade_count == 1
    assert result.time_in_market_pct == pytest.approx(50.0)


def test_the_window_end_instant_belongs_to_the_next_window():
    """右开：`t == window_end` 那一瞬属于下一个窗口（口径 4）。

    与 `walk_forward` 的分折边界同一条约定——同一条口径在两处必须一致，否则一个跨日的
    持仓会被两个窗口各算一整段。
    """
    window = _window()
    result = compute_capital_efficiency([_point(T0 + DAY, "10000")], window, CAPITAL)

    assert result.computed is False
    assert result.reason == CapitalEfficiencyReason.NO_TRADES.value


def test_the_window_start_instant_belongs_to_this_window():
    """左闭：`t == window_start` 那一瞬属于本窗口（与上一条互为镜像）。"""
    window = _window()
    result = compute_capital_efficiency([_point(T0, "10000")], window, CAPITAL)

    assert result.computed is True
    assert result.trade_count == 1


def test_an_inverted_window_is_an_error_not_a_silent_zero():
    """窗口右端早于左端是构造错误：显式炸，不静默按空窗处理。"""
    with pytest.raises(ValueError, match="窗口右端早于左端"):
        EfficiencyWindow(start=T0 + DAY, end=T0)


def test_an_inverted_interval_is_an_error():
    """占用区间右端早于左端同样是构造错误。"""
    with pytest.raises(ValueError, match="占用区间右端早于左端"):
        OccupancyInterval(start=T0 + DAY, end=T0, notional=Decimal("10000"))


# --------------------------------------------------------------------------- #
# 口径 5：三道「判不了」的门
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("capital", "reason"),
    [
        (None, CapitalEfficiencyReason.INVALID_CAPITAL),
        (Decimal("0"), CapitalEfficiencyReason.INVALID_CAPITAL),
        (Decimal("-1"), CapitalEfficiencyReason.INVALID_CAPITAL),
    ],
    ids=["none", "zero", "negative"],
)
def test_an_unusable_capital_is_not_a_measurement(capital, reason):
    """本金不可用 → 整段 None + `invalid_capital`，绝不是 0。

    `0` 会被读成「完全没用钱」（于是「效率真高」），而真相是「分母不成立，这次没测」。
    """
    result = compute_capital_efficiency(
        [_held(T0, T0 + DAY, "10000", "500")], _window(), capital
    )

    assert result.computed is False
    assert result.reason == reason.value
    assert result.avg_capital_used is None
    assert result.capital_utilization is None
    assert result.idle_pct is None
    assert result.time_in_market_pct is None
    assert result.return_per_capital_used_pct is None


def test_a_float_capital_is_refused():
    """浮点本金被拒：这条链路上钱是 `Decimal`。

    收一个 float 意味着上游已经悄悄转过一次——那正是要修的那个 bug，在这里就拦下。
    """
    result = compute_capital_efficiency(
        [_held(T0, T0 + DAY, "10000")], _window(), 10000.0
    )

    assert result.computed is False
    assert result.reason == CapitalEfficiencyReason.INVALID_CAPITAL.value


def test_a_nan_capital_is_refused_rather_than_compared_false():
    """NaN 本金必须先判 `is_finite`。

    `Decimal("NaN") <= 0` 是 **False**，所以少了这一步 NaN 会一路走到
    `capital_utilization`，让「利用率超标」这类比较静默为假，而分母根本不成立。
    """
    result = compute_capital_efficiency(
        [_held(T0, T0 + DAY, "10000")], _window(), Decimal("NaN")
    )

    assert result.computed is False
    assert result.reason == CapitalEfficiencyReason.INVALID_CAPITAL.value


def test_an_empty_window_is_a_conclusion_not_a_crash():
    """空窗（`end <= start`）→ 整段 None + `empty_window`，不抛不除零。"""
    result = compute_capital_efficiency(
        [_held(T0, T0 + DAY, "10000")], EfficiencyWindow(T0, T0), CAPITAL
    )

    assert result.computed is False
    assert result.reason == CapitalEfficiencyReason.EMPTY_WINDOW.value


def test_zero_trades_is_none_not_zero():
    """零笔数 → 整段 None + `no_trades`。

    `0` 会被读成「完全没用钱、效率真高」，而真相是「这一段什么都没发生」。
    """
    result = compute_capital_efficiency([], _window(), CAPITAL)

    assert result.computed is False
    assert result.reason == CapitalEfficiencyReason.NO_TRADES.value
    assert result.time_in_market_pct is None


def test_rows_none_of_which_fall_inside_the_window_are_no_trades():
    """给了行、但一行都不落在窗口内：与零笔数同因同果。"""
    result = compute_capital_efficiency(
        [_held(T0 - DAY * 3, T0 - DAY * 2, "10000")], _window(), CAPITAL
    )

    assert result.computed is False
    assert result.reason == CapitalEfficiencyReason.NO_TRADES.value


def test_an_open_position_with_unknown_notional_is_not_zero():
    """开着仓位但金额不可知 → 判不了，**不是 0**。

    把 `None` 当 0 会让加权平均被系统性低估，而「占用更多钱 ≠ 效率更高」这件事会因此
    反过来——本模块唯一必须避免的方向性错误。
    """
    result = compute_capital_efficiency(
        [
            OccupancyInterval(
                start=T0,
                end=T0 + DAY,
                notional=None,
                pnl=Decimal("500"),
            )
        ],
        _window(),
        CAPITAL,
    )

    assert result.computed is False
    assert result.reason == CapitalEfficiencyReason.INVALID_CAPITAL.value


# --------------------------------------------------------------------------- #
# 单位占用收益
# --------------------------------------------------------------------------- #


def test_return_per_capital_used_uses_the_weighted_not_the_nominal():
    """`return_per_capital_used_pct` 的分母是**时间加权平均占用**。

    用期末名义值做分母时，一个只用 10% 时间、全额占用的策略会被算成「收益/全额本金」
    ——把一次高效率的短打读成低效率，正是用户故事 13 要防的那类错。
    """
    result = compute_capital_efficiency(
        [_held(T0, T0 + DAY / 10, "10000", "500")], _window(), CAPITAL
    )

    # 赚 500，平均占用 1000 → 单位占用收益 50%。
    assert result.avg_capital_used == Decimal("1000")
    assert result.return_per_capital_used_pct == pytest.approx(50.0)


def test_return_per_capital_used_needs_a_known_pnl():
    """一笔都没结算 → 占用算得出，单位占用收益是 `None`（不是 0）。

    「还在持仓」与「赚了 0」是两件事：写 0 会让一条正在跑的会话看起来像白干了。
    """
    result = compute_capital_efficiency(
        [_held(T0, T0 + DAY, "10000")], _window(), CAPITAL
    )

    assert result.computed is True
    assert result.avg_capital_used == Decimal("10000")
    assert result.return_per_capital_used_pct is None


def test_an_explicit_pnl_total_wins_over_per_interval_sums():
    """显式传入的总盈亏优先于逐笔相加。

    #38 的会话侧会传一个汇总口径；两边各自加一遍就有两个可能不一致的答案。
    """
    result = compute_capital_efficiency(
        [_held(T0, T0 + DAY, "10000", "500")],
        _window(),
        CAPITAL,
        pnl_total=Decimal("250"),
    )

    assert result.pnl_total == Decimal("250")
    assert result.return_per_capital_used_pct == pytest.approx(2.5)


def test_a_non_finite_pnl_total_is_not_silently_ignored():
    """`pnl_total` 给了但不是有限数 → 判不了。

    NaN 会让任何比较静默为假，于是「单位占用收益是正的」这句话永远无从成立。
    """
    result = compute_capital_efficiency(
        [_held(T0, T0 + DAY, "10000", "500")],
        _window(),
        CAPITAL,
        pnl_total=Decimal("NaN"),
    )

    assert result.computed is False
    assert result.reason == CapitalEfficiencyReason.INVALID_CAPITAL.value


# --------------------------------------------------------------------------- #
# 口径 6：样本标注
# --------------------------------------------------------------------------- #


def test_a_thin_sample_still_computes_but_is_flagged():
    """2 笔交易的指标照样能算，只被打上「样本不足」（用户故事 14）。

    反过来（因为样本薄就把数字抹成 None）会把「有 2 笔、每笔都满仓」这个真实观察删掉，
    而它恰恰是判断「要不要再跑久一点」的起点。
    """
    result = compute_capital_efficiency(
        [
            _held(T0, T0 + DAY / 2, "10000", "100"),
            _held(T0 + DAY / 2, T0 + DAY, "10000", "100"),
        ],
        _window(),
        CAPITAL,
    )

    assert result.computed is True
    assert result.trade_count == 2
    assert result.sample_sufficient is False
    assert result.avg_capital_used == Decimal("10000")
    assert result.capital_utilization == pytest.approx(100.0)


def test_enough_trades_clears_the_flag():
    """门槛用非负整数；显式传一个 2 就让 2 笔算够（证明门槛真的在读数）。"""
    result = compute_capital_efficiency(
        [
            _held(T0, T0 + DAY / 2, "10000", "100"),
            _held(T0 + DAY / 2, T0 + DAY, "10000", "100"),
        ],
        _window(),
        CAPITAL,
        sample_threshold=2,
    )

    assert result.sample_sufficient is True
    assert result.sample_threshold == 2


@pytest.mark.parametrize("bad", [-1, 1.5, True, "2"])
def test_a_bad_sample_threshold_is_rejected_at_the_boundary(bad):
    """显式传入的门槛也要守边界：负数与小数都会被拒（与 `config.__post_init__` 同一条）。"""
    with pytest.raises(ValueError, match="sample_threshold"):
        compute_capital_efficiency(
            [_held(T0, T0 + DAY, "10000")], _window(), CAPITAL, sample_threshold=bad
        )


# --------------------------------------------------------------------------- #
# 会话侧的点观测
# --------------------------------------------------------------------------- #


def test_a_point_observation_has_zero_weight_but_still_counts():
    """点观测对加权平均的分子贡献 0，但**计入笔数**。

    会话侧的 `Order` 行只能提供这个形状；把它标出来，是为了让消费方有机会说「这个数在
    会话侧不可与回测侧同读」，而不是让两边各自产出一个同名但不同义的 `time_in_market_pct`。
    """
    result = compute_capital_efficiency(
        [_point(T0 + DAY / 2, "10000", "500")], _window(), CAPITAL
    )

    assert result.computed is True
    assert result.trade_count == 1
    assert result.avg_capital_used == Decimal("0")
    assert result.time_in_market_pct == pytest.approx(0.0)
    assert result.occupied_seconds == 0.0
    assert result.return_per_capital_used_pct is None  # 0 分母
    assert any("点观测" in note for note in result.notes)


def test_a_point_observation_kind_is_part_of_the_payload_semantics():
    """`POINT` 不是「坏数据」，它是会话侧能给的全部信息。"""
    assert OccupancyIntervalKind.HELD.value == "held"
    assert OccupancyIntervalKind.POINT.value == "point"
    assert set(OccupancyIntervalKind.values()) == {"held", "point"}


# --------------------------------------------------------------------------- #
# 载荷形状
# --------------------------------------------------------------------------- #


def test_the_payload_is_json_safe_and_money_is_fixed_point_text():
    """载荷要进 `JSONField`：金额出成 8 位定点串，不是 `float`、也不是科学计数法。

    `str(Decimal("0").quantize(...))` 是 `"0E-8"`——它在 JS 侧恰好还能被 `parseFloat`
    读成 0，所以这个错不会以「显示不对」暴露，只会以「接口里躺着一个怪字符串」留着。
    """
    result = compute_capital_efficiency(
        [_held(T0, T0 + DAY, "10000", "500")], _window(), CAPITAL
    )
    payload = result.as_payload()

    assert payload["avg_capital_used"] == "10000.00000000"
    assert payload["pnl_total"] == "500.00000000"
    assert isinstance(payload["capital_utilization"], float)
    assert isinstance(payload["sample_sufficient"], bool)
    assert isinstance(payload["notes"], list)


def test_an_unresolvable_result_says_so_on_the_object_and_stays_key_complete():
    """判不了时整段 None，但键仍然齐全，且 `reason` 也在载荷上（spec D6）。

    「算过、结论是无从判断」与「从没算过」必须能分开，而分开的依据是 `computed` /
    `reason`；载荷那一侧保持与「算出来了」同一组键（`reason` 为 `None`，但键在），
    前端不必为判不了单独写一条分支。
    """
    result = compute_capital_efficiency([], _window(), CAPITAL)

    assert result.computed is False
    assert result.reason == CapitalEfficiencyReason.NO_TRADES.value

    payload = result.as_payload()
    assert set(payload) == set(_full_position().as_payload())
    assert payload["avg_capital_used"] is None
    assert payload["time_in_market_pct"] is None
    assert payload["sample_sufficient"] is False
    assert payload["reason"] == CapitalEfficiencyReason.NO_TRADES.value


# --------------------------------------------------------------------------- #
# D6 契约：判不了的那一段自带归因（`reason` / `computed` 进载荷）
# --------------------------------------------------------------------------- #


def test_the_payload_carries_reason_for_each_of_the_three_failure_reasons():
    """三个原因码**逐个**都要能只读载荷就读出来（spec D6）。

    参数化而不是只测一个 `no_trades`：三个码对应三个不同的补救动作，所以「载荷带得出
    归因」这件事必须对三者都成立。只测其中一个的话，将来谁把 `reason` 写成由
    `no_trades` 触发的一条分支，另外两条会安静退化成 `None`——而它们的补救动作
    （去修本金 / 去修窗口）恰恰是最不显眼的那两条。

    载荷是消费方**唯一**会读的东西（#35 写库、#38 组装、前端展示都从它出发）：把归因
    留在对象上等于要求每个消费方都记得手工补回来，少补一处，「判不了」就退化成
    「一堆 `None`」。
    """
    cases = {
        # 本金不成立：`None` 走 invalid_capital（分母都没有）。
        CapitalEfficiencyReason.INVALID_CAPITAL.value: (
            [_held(T0, T0 + DAY, "10000", "500")],
            _window(),
            None,
        ),
        # 空窗：`end <= start`，时间加权平均的分母不存在。
        CapitalEfficiencyReason.EMPTY_WINDOW.value: (
            [_held(T0, T0 + DAY, "10000", "500")],
            EfficiencyWindow(start=T0, end=T0),
            CAPITAL,
        ),
        # 零笔数：这一段什么都没发生（不是「没占资金」）。
        CapitalEfficiencyReason.NO_TRADES.value: ([], _window(), CAPITAL),
    }
    assert set(cases) == {r.value for r in CapitalEfficiencyReason}

    for reason, (intervals, window, total_capital) in cases.items():
        result = compute_capital_efficiency(intervals, window, total_capital)
        payload = result.as_payload()

        assert result.computed is False, reason
        assert payload["computed"] is False, reason
        assert payload["reason"] == reason, reason


def test_the_payload_reason_is_none_when_the_metrics_did_compute():
    """算得出时 `reason` 是 `None`，且**键在**——「没有原因」与「没有这个字段」是两回事。

    算得出时把 `reason` 整个省掉，消费方就得写 `payload.get("reason")` 才能不炸，
    而 `.get()` 的默认值恰好会把「字段被挪走了」读成「算出来了」——本票要防的正是
    这种「两处看起来都正常」的静默。
    """
    payload = _full_position().as_payload()

    assert "reason" in payload
    assert payload["reason"] is None
    assert payload["computed"] is True
