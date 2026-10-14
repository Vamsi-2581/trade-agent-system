"""会话归因已实现盈亏的统计（CONTEXT.md Glossary「会话归因已实现盈亏」）。

半个文件测纯函数、半个文件钉住**两种取数是同一个数**：列表接口走 SQL 聚合
（`annotate_realized_pnl`），详情接口走逐单汇总（`session_stats_for`）。它们一旦分叉，
用户会在同一屏上看到卡片与弹窗两个不一样的「盈亏」，而两处看起来都正常——本仓最贵的
那类错误。纯函数那部分不碰库，所以 `-k "not database"` 能秒回。
"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest
from django.contrib.auth import get_user_model

from apps.exchange.models import ExchangeAccount
from apps.trading.models import LiveSession, Order, Strategy
from apps.trading.session_stats import (
    SAMPLE_THRESHOLD,
    annotate_realized_pnl,
    compute_session_stats,
    money,
    session_stats_for,
)

User = get_user_model()


# --------------------------------------------------------------------------- #
# 纯函数
# --------------------------------------------------------------------------- #


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
