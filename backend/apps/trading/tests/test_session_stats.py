"""会话归因已实现盈亏的统计（CONTEXT.md Glossary「会话归因已实现盈亏」）。

半个文件测纯函数、半个文件钉住**两种取数是同一个数**：列表接口走 SQL 聚合
（`annotate_realized_pnl`），详情接口走逐单汇总（`session_stats_for`）。它们一旦分叉，
用户会在同一屏上看到卡片与弹窗两个不一样的「盈亏」，而两处看起来都正常——本仓最贵的
那类错误。纯函数那部分不碰库，所以 `-k "not database"` 能秒回。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone as django_timezone

from apps.exchange.models import ExchangeAccount
from apps.strategy_factory.capital_efficiency import (
    EfficiencyWindow,
    OccupancyInterval,
    OccupancyIntervalKind,
    capital_efficiency_keys,
)
from apps.trading.models import LiveSession, Order, Strategy
from apps.trading.serializers import LiveSessionSerializer
from apps.trading.session_stats import (
    CAPITAL_EFFICIENCY_AVAILABLE_KEY,
    CAPITAL_EFFICIENCY_KEY,
    CAPITAL_EFFICIENCY_NOTES_KEY,
    SAMPLE_THRESHOLD,
    annotate_efficiency_input,
    annotate_realized_pnl,
    compute_session_stats,
    money,
    row_efficiency_keys,
    session_stats_for,
)

User = get_user_model()


# --------------------------------------------------------------------------- #
# 纯函数
# --------------------------------------------------------------------------- #


#: 会话起点。**必须是「现在之前一点点」而不是一个写死的 2024 日期**：
#: `Order.created_at` 是 `auto_now_add`，写的是**真实挂钟**。把窗口钉在 2024 而订单落在
#: 2026，点观测就全部落在窗口外、被 `_clip_to_window` 丢掉，于是 `no_trades`——那不是
#: 被测代码的错，是一份自相矛盾的测试数据（真实系统里不会有「订单在会话开始前两年成交」）。
#:
#: 取一个相对量是为了**窗口秒数可复现**：`_START` 与注入的 `now` 之间差一个已知的天数，
#: 所以 `window_seconds` 的断言不随运行时刻漂移。
_START = django_timezone.now() - timedelta(days=1)


def _at(days: float) -> datetime:
    """相对 `_START` 的时刻。所有窗口边界都从这里推，没有断言挂在真实时钟上。"""
    return _START + timedelta(days=days)


def test_a_session_with_no_closed_order_has_no_ratio_and_not_zero():
    """没成交过 ≠ 一笔没赢过：比率类字段一律 None，只有总盈亏是 0。

    写 0 的表现是「胜率 0%」「平均每笔 0.00」——一条还没开张的会话看起来像一条一直
    在亏的会话。
    """
    stats = compute_session_stats([])

    assert stats["closed_orders"] == 0
    assert stats["realized_pnl_total"] == "0.00000000"
    assert stats["win_rate"] is None
    assert stats["avg_pnl"] is None
    assert stats["best_pnl"] is None
    assert stats["worst_pnl"] is None
    assert stats["sample_sufficient"] is False


def test_zero_pnl_counts_as_a_loss_not_a_win():
    """盈亏恰好为 0 的那笔与亏损同列。

    把它算进胜率只会让胜率比实际好看，而这个数没有任何一个用途需要它偏高。
    """
    stats = compute_session_stats([Decimal("12"), Decimal("0"), Decimal("-6")])

    assert stats["closed_orders"] == 3
    assert stats["winning_orders"] == 1
    assert stats["win_rate"] == pytest.approx(1 / 3, abs=1e-4)
    assert stats["realized_pnl_total"] == "6.00000000"


def test_the_extremes_and_the_mean_are_about_the_same_set():
    stats = compute_session_stats([Decimal("12"), Decimal("-6")])

    assert stats["avg_pnl"] == "3.00000000"
    assert stats["best_pnl"] == "12.00000000"
    assert stats["worst_pnl"] == "-6.00000000"


def test_none_values_are_not_orders():
    """`None` 是「没有已实现盈亏」（开仓单），不是一笔 0 盈亏的成交。

    过滤必须发生在算 n 之前：混进去会把分母撑大，而胜率是个分数。
    """
    stats = compute_session_stats([Decimal("5"), None, None])

    assert stats["closed_orders"] == 1
    assert stats["win_rate"] == 1.0


@pytest.mark.parametrize(
    "count,sufficient",
    [(SAMPLE_THRESHOLD - 1, False), (SAMPLE_THRESHOLD, True)],
)
def test_the_sample_threshold_boundary(count, sufficient):
    """门槛是「够」而不是「超过」——边界取等号的方向要钉住。"""
    stats = compute_session_stats([Decimal("1")] * count)

    assert stats["sample_sufficient"] is sufficient
    assert stats["sample_threshold"] == SAMPLE_THRESHOLD


def test_the_caveats_ride_along_with_the_numbers():
    """两条边界随数字一起返回，由前端原样显示。

    它们讲的是「这个数没覆盖什么」，而这件事只在产生数字的这一处知道——放在前端写死的
    话，将来谁改了减仓的归属，没有一个地方会变红。
    """
    caveats = compute_session_stats([])["caveats"]

    assert any("未实现" in c for c in caveats)
    assert any("熔断减仓" in c for c in caveats)


def test_money_quantises_to_the_order_precision():
    assert money(Decimal("1.5")) == "1.50000000"
    assert money(None) is None


def test_money_never_goes_scientific():
    """零必须写成 `0.00000000`，不是 `0E-8`。

    科学计数法在 JS 侧恰好还能被 `parseFloat` 读成 0，所以这个错**不会**以「显示不对」
    的形式暴露——它只会以「接口里躺着一个怪字符串」的形式留着，直到某个不经过
    `parseFloat` 的消费方读了它（比如直接比较字符串的测试、或别的语言的客户端）。
    """
    assert money(Decimal("0")) == "0.00000000"
    assert money(Decimal("0E-8")) == "0.00000000"


# --------------------------------------------------------------------------- #
# 两种取数是同一个数
# --------------------------------------------------------------------------- #


def _account() -> ExchangeAccount:
    return ExchangeAccount.objects.create(
        exchange="binance",
        label="stats",
        api_key_enc=b"x",
        api_secret_enc=b"y",
        testnet=True,
    )


def _session_with_orders(pnls: list[Decimal]) -> LiveSession:
    suffix = uuid4().hex[:8]
    user = User.objects.create_user(
        email=f"stats-{suffix}@test.com",
        username=f"stats_{suffix}",
        password="testpass123",
    )
    strategy = Strategy.objects.create(
        name=f"s_{suffix}", code_path=f"strategies/s_{suffix}.py"
    )
    session = LiveSession.objects.create(
        user=user,
        strategy=strategy,
        symbol="BTC/USDT",
        initial_capital=Decimal("1000"),
        current_equity=Decimal("1000"),
    )
    account = _account()
    for pnl in pnls:
        Order.objects.create(
            exchange_account=account,
            live_session=session,
            symbol="BTC/USDT",
            side="sell",
            order_type="market",
            quantity=Decimal("0.01"),
            status="filled",
            realized_pnl=pnl,
        )
    return session


class TestTheTwoReadingsAgree:
    """列表的 SQL 聚合与详情的逐单汇总。"""

    @pytest.mark.django_db
    def test_they_agree_on_a_session_with_orders(self):
        session = _session_with_orders([Decimal("12.5"), Decimal("-2.25")])

        annotated = annotate_realized_pnl(
            LiveSession.objects.filter(pk=session.pk)
        ).get()

        assert (
            money(annotated.realized_pnl)
            == session_stats_for(session)["realized_pnl_total"]
        )

    @pytest.mark.django_db
    def test_a_session_without_orders_reads_zero_not_null_on_both_sides(self):
        """空会话在两处都得是 0，不能一处 0 一处 null：卡片拿 null 会显示「—」，
        而弹窗拿 0 会显示「0.00」——同一条会话两种说法。"""
        session = _session_with_orders([])

        annotated = annotate_realized_pnl(
            LiveSession.objects.filter(pk=session.pk)
        ).get()

        assert money(annotated.realized_pnl) == "0.00000000"
        assert session_stats_for(session)["realized_pnl_total"] == "0.00000000"

    @pytest.mark.django_db
    def test_another_sessions_orders_do_not_leak_into_the_sum(self):
        mine = _session_with_orders([Decimal("10")])
        other = _session_with_orders([Decimal("999")])

        annotated = annotate_realized_pnl(LiveSession.objects.filter(pk=mine.pk)).get()

        assert money(annotated.realized_pnl) == "10.00000000"
        assert session_stats_for(mine)["realized_pnl_total"] == "10.00000000"
        assert session_stats_for(other)["realized_pnl_total"] == "999.00000000"

    @pytest.mark.django_db
    def test_orders_without_realized_pnl_are_excluded_from_both(self):
        """开仓单（没有已实现盈亏）在两处都不算数。"""
        session = _session_with_orders([Decimal("10")])
        Order.objects.create(
            exchange_account=_account(),
            live_session=session,
            symbol="BTC/USDT",
            side="buy",
            order_type="market",
            quantity=Decimal("0.01"),
            status="filled",
            realized_pnl=None,
        )

        annotated = annotate_realized_pnl(
            LiveSession.objects.filter(pk=session.pk)
        ).get()

        assert money(annotated.realized_pnl) == "10.00000000"
        assert session_stats_for(session)["closed_orders"] == 1


# --------------------------------------------------------------------------- #
# 资金效率段（spec #30 D6/D9，子票 #38）
# --------------------------------------------------------------------------- #


def _window(days: int = 10) -> EfficiencyWindow:
    """一个固定的 10 天窗口。**固定**：用真实 now 造窗口的断言会随运行时刻漂移。"""
    start = _at(0)
    return EfficiencyWindow(start=start, end=start + timedelta(days=days))


def _points(window: EfficiencyWindow, *pnls: Decimal) -> list[OccupancyInterval]:
    """一串点观测（会话侧唯一能提供的东西）：每个平仓时刻一项，零时长。"""
    return [
        OccupancyInterval(
            start=window.start,
            end=window.start,
            notional=abs(p),
            pnl=p,
            kind=OccupancyIntervalKind.POINT.value,
        )
        for p in pnls
    ]


def _flattened_efficiency_keys() -> tuple[str, ...]:
    """被摊到返回字典**顶层**的那几个契约键。

    `sample_sufficient` **不在内**：顶层那个数的是**订单笔数**，而契约里那个数的是**区间
    笔数**。点观测下两者恒等，所以这个撞名不会以「数字不对」暴露——它只会让顶层那个键悄悄
    换一套口径（`**efficiency` 展开时后写的赢）。摊平时跳过它，这条纪律由
    `TestThePinnedKeyContract.test_the_order_count_threshold_is_not_shadowed_by_the_interval_one`
    钉着，这里只是跟着它走。
    """
    return tuple(k for k in capital_efficiency_keys() if k != "sample_sufficient")


class TestThePinnedKeyContract:
    """跨侧同键名（spec D6 / 用户故事 22）。**纯函数**，不碰库。"""

    def test_the_session_side_speaks_the_backtest_sides_key_names(self):
        """六个契约键必须逐字同回测侧——`capital_efficiency_keys()` 是钉住这件事的契约。

        会话侧与回测侧写的是同一组键，两边各自 `payload[...]` 取用时没有任何东西会因改名
        而报错。这条断言是唯一会因改名而变红的地方，所以它必须存在。
        """
        stats = compute_session_stats(
            [Decimal("12"), Decimal("-2")],
            intervals=_points(_window(), Decimal("12"), Decimal("-2")),
            window=_window(),
            total_capital=Decimal("1000"),
        )

        for key in capital_efficiency_keys():
            assert key in stats, f"会话侧缺了回测侧的契约键 {key!r}"

    def test_the_sub_payload_carries_exactly_the_six_metrics(self):
        """子载荷里的指标键集**等于**契约键集，不多不少。

        「不多」这一半同样重要：子载荷多出来的键不会被本仓的任何东西拦住，而它会让人以为
        两侧的口径又多了一项。
        """
        stats = compute_session_stats(
            [Decimal("12")],
            intervals=_points(_window(), Decimal("12")),
            window=_window(),
            total_capital=Decimal("1000"),
        )
        payload = stats[CAPITAL_EFFICIENCY_KEY]

        assert set(payload) >= set(capital_efficiency_keys())
        assert payload["trade_count"] == 1  # 中间量另有一套键，与指标键分开

    def test_the_order_count_threshold_is_not_shadowed_by_the_interval_one(self):
        """顶层 `sample_sufficient` 仍然是**订单笔数**口径，没被效率段那个覆盖。

        两个同名的数：本模块顶层数订单笔数，纯模块里数区间笔数。点观测下两者恒等，所以
        覆盖**不会**以「数字不对」的形式暴露——它只会让顶层的键悄悄换一套口径，而
        `**efficiency` 展开时后写的赢。这条断言钉着它没被换掉。
        """
        stats = compute_session_stats([Decimal("1")] * SAMPLE_THRESHOLD)

        assert stats["sample_sufficient"] is True
        assert stats["closed_orders"] == SAMPLE_THRESHOLD


class TestTheEfficiencySegmentOnASession:
    """效率段在会话侧**算得出，但只是粗口径**——这是本票最重要的一条。"""

    def test_point_observations_yield_zero_capital_not_none(self):
        """点观测下 `avg_capital_used` 是 0、`capital_utilization` 是 0.0。

        **这不是「会话没用钱」**，是「这一侧量不了持仓时长」。因为纯点观测的时间权重是 0，
        加权平均的分子（占用 × 时长）恒为 0。断言具体到数值，是为了让任何「顺手把它改成
        None 或换成别的分母」都变红。
        """
        stats = compute_session_stats(
            [Decimal("12"), Decimal("-2")],
            intervals=_points(_window(), Decimal("12"), Decimal("-2")),
            window=_window(),
            total_capital=Decimal("1000"),
        )

        assert stats[CAPITAL_EFFICIENCY_AVAILABLE_KEY] is True
        assert stats["avg_capital_used"] == "0.00000000"
        assert stats["capital_utilization"] == 0.0
        assert stats["time_in_market_pct"] == 0.0
        # 分母为 0 → 除零，纯模块给 None（那是 T4 修掉的一个真 bug）。
        assert stats["return_per_capital_used_pct"] is None

    def test_the_coarse_calibre_is_stated_even_when_the_numbers_exist(self):
        """**算得出时 `capital_efficiency_notes` 也必须非空。**

        空列表会被消费方读成「没有任何保留意见」——于是 `capital_utilization = 0` 就被当成
        回测侧那个数读，把重仓读成高效。这句保留意见不随成败消失。
        """
        stats = compute_session_stats(
            [Decimal("12")],
            intervals=_points(_window(), Decimal("12")),
            window=_window(),
            total_capital=Decimal("1000"),
        )

        notes = stats[CAPITAL_EFFICIENCY_NOTES_KEY]
        assert notes, "算得出时也必须说清楚这是点观测口径"
        assert any("点观测" in n for n in notes)

    def test_the_threshold_comes_from_one_place(self):
        """效率段的门槛与顶层的 `SAMPLE_THRESHOLD` 是同一个数——不许有第二个默认值。"""
        stats = compute_session_stats(
            [Decimal("12")],
            intervals=_points(_window(), Decimal("12")),
            window=_window(),
            total_capital=Decimal("1000"),
        )

        assert stats[CAPITAL_EFFICIENCY_KEY]["sample_threshold"] == SAMPLE_THRESHOLD

    def test_pnl_total_equals_the_top_level_realized_pnl(self):
        """两个键名下的同一个数：不让两处各加一遍。

        两处各加一次就有两个可能不一致的答案，而它们会显示在同一屏上。
        """
        stats = compute_session_stats(
            [Decimal("12"), Decimal("-2")],
            intervals=_points(_window(), Decimal("12"), Decimal("-2")),
            window=_window(),
            total_capital=Decimal("1000"),
        )

        assert stats[CAPITAL_EFFICIENCY_KEY]["pnl_total"] == stats["realized_pnl_total"]


class TestTheEfficiencySegmentWhenItCannotBeMeasured:
    """判不了就是判不了：整段 None + 原因，**绝不是 0**，也绝不炸。"""

    def test_a_session_without_capital_is_none_not_zero(self):
        """没有本金 → 效率段整段 None + `invalid_capital`，不是崩、也不是 0。

        0 会被读成「占用为 0」→「效率真高」；而真相是这一段根本没有可用的分母。
        """
        stats = compute_session_stats(
            [Decimal("12")],
            intervals=_points(_window(), Decimal("12")),
            window=_window(),
            total_capital=None,
        )

        assert stats[CAPITAL_EFFICIENCY_AVAILABLE_KEY] is False
        assert stats[CAPITAL_EFFICIENCY_KEY]["reason"] == "invalid_capital"
        for key in _flattened_efficiency_keys():
            assert stats[key] is None, f"{key} 判不了时必须是 None，不是 0"
        assert stats[CAPITAL_EFFICIENCY_NOTES_KEY], "判不了必须有话说"

    def test_zero_capital_is_also_invalid_not_a_division(self):
        """本金恰为 0 与没有本金**同码同果**：利用率的分母都不成立。"""
        stats = compute_session_stats(
            [Decimal("12")],
            intervals=_points(_window(), Decimal("12")),
            window=_window(),
            total_capital=Decimal("0"),
        )

        assert stats[CAPITAL_EFFICIENCY_AVAILABLE_KEY] is False
        assert stats[CAPITAL_EFFICIENCY_KEY]["reason"] == "invalid_capital"

    def test_a_float_capital_is_refused_not_truncated(self):
        """浮点本金被拒（不是悄悄接受）。钱在这条链路上全程 Decimal。

        收一个 float 意味着上游已经悄悄转过一次——那正是要修的 bug，不是要兼容的输入。
        """
        stats = compute_session_stats(
            [Decimal("12")],
            intervals=_points(_window(), Decimal("12")),
            window=_window(),
            total_capital=1000.0,
        )

        assert stats[CAPITAL_EFFICIENCY_AVAILABLE_KEY] is False
        assert stats[CAPITAL_EFFICIENCY_KEY]["reason"] == "invalid_capital"

    def test_an_empty_window_is_not_a_division_by_zero(self):
        """零时长的窗口判 `empty_window`，不是除零。

        窗口两端相等时加权平均的分母不存在——与 `walk_forward` 的 `total_days <= 0`
        同一条纪律：「数据不够」是一个结论，不是一次崩溃。
        """
        window = _window(days=0)
        stats = compute_session_stats(
            [Decimal("12")],
            intervals=_points(window, Decimal("12")),
            window=window,
            total_capital=Decimal("1000"),
        )

        assert stats[CAPITAL_EFFICIENCY_AVAILABLE_KEY] is False
        assert stats[CAPITAL_EFFICIENCY_KEY]["reason"] == "empty_window"

    def test_an_empty_session_is_no_trades_not_zero_utilisation(self):
        """零笔数判 `no_trades`——与 `invalid_capital` 是**两个**原因码。

        「这一段什么都没发生」与「分母不成立」对应两个不同的补救动作（补时间 vs 修本金），
        混成一个码会让归因变成猜。
        """
        stats = compute_session_stats(
            [],
            intervals=[],
            window=_window(),
            total_capital=Decimal("1000"),
        )

        assert stats["closed_orders"] == 0
        assert stats[CAPITAL_EFFICIENCY_AVAILABLE_KEY] is False
        assert stats[CAPITAL_EFFICIENCY_KEY]["reason"] == "no_trades"
        assert stats["realized_pnl_total"] == "0.00000000"

    def test_the_efficiency_keys_exist_even_when_the_caller_has_not_adapted(self):
        """不传那三个参数时键**仍然在**，值为 None——绝不缺键。

        缺键会让「没适配」与「没数据」在消费侧长得一模一样，而它们是两件事。
        """
        stats = compute_session_stats([Decimal("12")])

        for key in capital_efficiency_keys():
            assert key in stats, f"{key} 缺了——「没适配」与「没数据」会因此长得一样"
        for key in _flattened_efficiency_keys():
            assert stats[key] is None
        assert stats[CAPITAL_EFFICIENCY_KEY]["reason"] == "invalid_capital"

    def test_the_empty_session_branch_also_carries_the_efficiency_keys(self):
        """空会话那个**早返回**分支也要带上效率段——两个分支缺一不可。

        只在正常分支加键的话，一条还没成交的会话会在前端读到 `undefined`，而那条路径
        恰恰是最常被打开的（新建的会话）。
        """
        stats = compute_session_stats([])

        assert CAPITAL_EFFICIENCY_KEY in stats
        assert CAPITAL_EFFICIENCY_AVAILABLE_KEY in stats
        assert CAPITAL_EFFICIENCY_NOTES_KEY in stats
        for key in capital_efficiency_keys():
            assert key in stats, f"{key} 缺了——空会话分支漏加了效率键"


# --------------------------------------------------------------------------- #
# 会话侧取数：详情（逐单）与列表（SQL 聚合）给同一套效率键
# --------------------------------------------------------------------------- #


#: 「没传这个参数」的哨兵。`started_at=None` 是要测的一条**真实路径**（建了会话还没启动），
#: 所以不能用 `None` 兼作缺省值——那样就测不出它了。
_UNSET = object()


def _session_with_timed_orders(
    pnls: list[Decimal],
    *,
    initial_capital: Decimal | None = Decimal("1000"),
    started_at: object = _UNSET,
    stopped_at: datetime | None = None,
    status: str = "stopped",
) -> LiveSession:
    """一条带**时间边界**的会话 + 每笔一个平仓单。

    `started_at` 缺省取 `_START`（不是真实 now）：用真实时钟造的窗口边界会让断言随运行
    时刻漂移，而那正是「必须能注入时钟」要防的事。**显式传 `None`** 才表示「这条会话
    还没启动」，那是另一个用例。
    """
    suffix = uuid4().hex[:8]
    user = User.objects.create_user(
        email=f"eff-{suffix}@test.com",
        username=f"eff_{suffix}",
        password="testpass123",
    )
    strategy = Strategy.objects.create(
        name=f"e_{suffix}", code_path=f"strategies/e_{suffix}.py"
    )
    session = LiveSession.objects.create(
        user=user,
        strategy=strategy,
        symbol="BTC/USDT",
        initial_capital=(
            initial_capital if initial_capital is not None else Decimal("0")
        ),
        current_equity=Decimal("1000"),
        status=status,
        started_at=_START if started_at is _UNSET else started_at,
        stopped_at=stopped_at,
    )
    account = _account()
    for pnl in pnls:
        Order.objects.create(
            exchange_account=account,
            live_session=session,
            symbol="BTC/USDT",
            side="sell",
            order_type="market",
            quantity=Decimal("0.01"),
            status="filled",
            realized_pnl=pnl,
        )
    return session


class TestTheTwoReadingsAgreeOnEfficiency:
    """详情（逐单）与列表（SQL 聚合）在效率段上必须是同一个数。"""

    @pytest.mark.django_db
    def test_both_paths_agree_on_a_stopped_session(self):
        session = _session_with_timed_orders(
            [Decimal("12.5"), Decimal("-2.25")],
            stopped_at=_at(10),
        )

        detailed = session_stats_for(session)
        row = annotate_efficiency_input(LiveSession.objects.filter(pk=session.pk)).get()
        listed = row_efficiency_keys(row)

        for key in _flattened_efficiency_keys():
            assert listed[key] == detailed[key], f"{key} 两条路分叉了"
        assert listed[CAPITAL_EFFICIENCY_AVAILABLE_KEY] is True
        assert listed[CAPITAL_EFFICIENCY_KEY]["trade_count"] == 2

    @pytest.mark.django_db
    def test_both_paths_agree_on_an_empty_session(self):
        """空会话在两条路上都得是同一段：`no_trades` + 整段 None，不是一处 0 一处 null。"""
        session = _session_with_timed_orders([], stopped_at=_at(10))

        detailed = session_stats_for(session)
        row = annotate_efficiency_input(LiveSession.objects.filter(pk=session.pk)).get()
        listed = row_efficiency_keys(row)

        assert detailed[CAPITAL_EFFICIENCY_KEY]["reason"] == "no_trades"
        assert listed[CAPITAL_EFFICIENCY_KEY]["reason"] == "no_trades"
        for key in _flattened_efficiency_keys():
            assert listed[key] is None
            assert listed[key] == detailed[key]

    @pytest.mark.django_db
    def test_both_paths_agree_when_the_session_has_no_capital(self):
        """本金为 0 的会话在两条路上都判 `invalid_capital`（不是一处判不了、一处给 0）。"""
        session = _session_with_timed_orders(
            [Decimal("12.5")],
            initial_capital=Decimal("0"),
            stopped_at=_at(10),
        )

        detailed = session_stats_for(session)
        row = annotate_efficiency_input(LiveSession.objects.filter(pk=session.pk)).get()
        listed = row_efficiency_keys(row)

        # 判不了的是**效率**（分母不成立），不是收益——盈亏照算，两处都算得出。
        assert detailed[CAPITAL_EFFICIENCY_KEY]["reason"] == "invalid_capital"
        assert listed[CAPITAL_EFFICIENCY_KEY]["reason"] == "invalid_capital"
        assert detailed["realized_pnl_total"] == "12.50000000"
        assert listed[CAPITAL_EFFICIENCY_AVAILABLE_KEY] is False
        # `row_efficiency_keys` 只回效率段；列表那个盈亏走序列化器（同一条注解）。
        assert LiveSessionSerializer(row).data["realized_pnl"] == "12.50000000"

    @pytest.mark.django_db
    def test_the_two_paths_agree_on_the_capital_used_figure(self):
        """两条路算出的 `avg_capital_used` 都是 0 —— **点观测的诚实答案**。

        它不是「没用钱」。这条断言把「两条路一致」与「这个数在会话侧的含义」分开钉住：
        一致性靠上面一条，含义靠这一条与 `notes`。
        """
        session = _session_with_timed_orders(
            [Decimal("12.5"), Decimal("-2.25")],
            stopped_at=_at(10),
        )

        detailed = session_stats_for(session)
        listed = row_efficiency_keys(
            annotate_efficiency_input(LiveSession.objects.filter(pk=session.pk)).get()
        )

        assert detailed["avg_capital_used"] == "0.00000000"
        assert listed["avg_capital_used"] == "0.00000000"
        assert listed[CAPITAL_EFFICIENCY_NOTES_KEY], "列表侧也要带上那句保留意见"


class TestThePartialFillDiscipline:
    """部分成交补齐的行**不重复计数**（既有纪律，`_apply_close_pnl` 写回同一行）。"""

    @pytest.mark.django_db
    def test_a_row_corrected_by_a_partial_fill_counts_once(self):
        """补齐发生在**同一行**上，所以按行计数不会重复。

        这个用例把那张单改两次（模拟 0.5 → 0.8 → 满仓的增量修正），行数始终是 1。它钉的
        是「本模块按行计数」这个前提——那条前提一旦被上游改成「每次补一行」，胜率与笔数
        会一起变大，而两处看起来都正常。
        """
        session = _session_with_timed_orders([Decimal("10")], stopped_at=_at(10))
        order = Order.objects.get(live_session=session)
        # 部分成交：先只填一半，再把余下的补上——**同一行**被改两次。
        Order.objects.filter(pk=order.pk).update(filled_quantity=Decimal("0.005"))
        Order.objects.filter(pk=order.pk).update(
            filled_quantity=Decimal("0.01"), realized_pnl=Decimal("10")
        )

        stats = session_stats_for(session)
        listed = row_efficiency_keys(
            annotate_efficiency_input(LiveSession.objects.filter(pk=session.pk)).get()
        )

        assert Order.objects.filter(live_session=session).count() == 1
        assert stats["closed_orders"] == 1
        assert stats["realized_pnl_total"] == "10.00000000"
        assert listed[CAPITAL_EFFICIENCY_KEY]["trade_count"] == 1

    @pytest.mark.django_db
    def test_open_orders_do_not_enter_the_trade_count_on_either_path(self):
        """开仓单（没有已实现盈亏）在两处都不算一笔。"""
        session = _session_with_timed_orders([Decimal("10")], stopped_at=_at(10))
        Order.objects.create(
            exchange_account=_account(),
            live_session=session,
            symbol="BTC/USDT",
            side="buy",
            order_type="market",
            quantity=Decimal("0.01"),
            status="filled",
            realized_pnl=None,
        )

        stats = session_stats_for(session)
        listed = row_efficiency_keys(
            annotate_efficiency_input(LiveSession.objects.filter(pk=session.pk)).get()
        )

        assert stats["closed_orders"] == 1
        assert stats[CAPITAL_EFFICIENCY_KEY]["trade_count"] == 1
        assert listed[CAPITAL_EFFICIENCY_KEY]["trade_count"] == 1


class TestTheWindowBoundary:
    """窗口边界：`started_at → stopped_at`（已停）/ `now()`（未停）。"""

    @pytest.mark.django_db
    def test_a_running_session_uses_the_injected_clock(self):
        """未停止的会话，窗口右端 = **注入的** now。

        用真实时钟的话，这条断言只能靠「造一条 started_at 在现在之前的会话」，而窗口秒数
        会随运行时刻漂移。注入之后它是可复现的。
        """
        session = _session_with_timed_orders([Decimal("10")], status="running")

        pinned = _at(3)
        stats = session_stats_for(session, now=lambda: pinned)

        assert stats[CAPITAL_EFFICIENCY_KEY]["window_seconds"] == 3 * 86400.0

    @pytest.mark.django_db
    def test_a_stopped_session_uses_stopped_at_not_now(self):
        """已停止的会话窗口右端是 `stopped_at`——**不是** now。

        用 now 的话，一条停了一年的会话窗口会一直变长，利用率随之被稀释下去（分母变大、
        分子不变），于是「历史上占用很低」这个假象会随时间的流逝自动生成。
        """
        session = _session_with_timed_orders([Decimal("10")], stopped_at=_at(10))

        # 注入一个远在 stopped_at 之后的 now：它**不该**被用上。
        far_future = _at(365)
        stats = session_stats_for(session, now=lambda: far_future)

        assert stats[CAPITAL_EFFICIENCY_KEY]["window_seconds"] == 10 * 86400.0

    @pytest.mark.django_db
    def test_a_session_that_never_started_cannot_be_measured(self):
        """`started_at` 为空的会话判不了（`invalid_capital`）而不是崩。

        没有起点就没有窗口。**不造一个「从第一笔成交起」的窗口**：那会让分母随成交分布
        变化，利用率就不再是「占用 / 窗口」。
        """
        session = _session_with_timed_orders(
            [Decimal("10")], started_at=None, status="pending"
        )

        stats = session_stats_for(session)

        assert stats["closed_orders"] == 1  # 盈亏照算——判不了的是效率，不是收益
        assert stats[CAPITAL_EFFICIENCY_AVAILABLE_KEY] is False
        assert stats[CAPITAL_EFFICIENCY_KEY]["reason"] == "invalid_capital"

    @pytest.mark.django_db
    def test_an_inverted_window_is_judged_not_raised(self):
        """`stopped_at < started_at`（脏数据）判不了，**不抛 ValueError**。

        纯模块的 `ValueError` 是给构造错误的纪律；库里的一行脏数据该判不了，不该把整个
        详情接口打成 500。
        """
        session = _session_with_timed_orders(
            [Decimal("10")], stopped_at=_START, started_at=_at(1)
        )

        stats = session_stats_for(session)

        assert stats[CAPITAL_EFFICIENCY_AVAILABLE_KEY] is False
        assert stats[CAPITAL_EFFICIENCY_KEY]["reason"] == "invalid_capital"
