"""paper 观察期判定的证据采集与判定（spec #19 D6 / D7 / D8；子票 #24）。

这一份钉的是本票的核心口径，四条边界各有一组用例：

1. **观察期恰 14 天**——墙钟与 running **两个数都必须够**（D8）。两个数不够的**四种
   组合**逐个钉死，其中「墙钟够而 running 不够」是最容易漏掉的那一种（它正是
   `paused` 不计入这条口径的全部意义）。
2. **`paused` 的天数不计入**——走真实时间线求和，不 mock 那个数。7 天跑 → 3 天暂停
   → 7 天跑 = 墙钟 17 天而 running 只有 14 天：**恰好达标**。这条用例是「补时间线表」
   这个决定的验收证据：没有时间线，这个数算不出来。
3. **观察期未满的落点**——会话还在 → 停 `paper_running`；会话已结束 → `paper_failed`。
   两种**不是**同一个原因码，混起来会把「还在观察」判成失败。
4. **风控命中恰好等于上限**——`>` 而不是 `>=`，恰等于上限算通过。

外加两条口径透出的纪律：盈亏数字带 `compute_session_stats` 的 caveats，风控命中带
「只算额度类拒单」的 caveat（交付 7）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from apps.exchange.models import ExchangeAccount
from apps.riskguard.models import RiskEvent
from apps.strategy_factory import effects
from apps.strategy_factory.config import CONFIG
from apps.strategy_factory.evaluate import (
    CandidateEvidence,
    ReasonCode,
    evaluate_candidate,
)
from apps.strategy_factory.models import StrategyCandidate
from apps.strategy_factory.status import (
    StrategyCandidateOrigin,
    StrategyCandidateStatus,
)
from apps.trading.models import (
    LiveSession,
    LiveSessionStatusChange,
    Order,
    Strategy,
    running_duration,
)

NOW = datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)
OBS_DAYS = 14


def _user(username="paper-obs"):
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_user(
        username=username, email=f"{username}@example.com", password="pw12345"
    )


def _account(user, *, testnet=True, label="模拟盘"):
    return ExchangeAccount.objects.create(
        exchange="binance",
        label=label,
        api_key_enc=b"k",
        api_secret_enc=b"s",
        testnet=testnet,
    )


def _strategy(user, name="paper_probe"):
    return Strategy.objects.create(name=name, code_path=f"strategies/{name}.py")


def _session(
    user, strategy, account, *, status="running", started_at=None, capital="1000"
):
    return LiveSession.objects.create(
        user=user,
        strategy=strategy,
        symbol="BTC/USDT:USDT",
        mode="paper",
        status=status,
        exchange_account=account,
        initial_capital=Decimal(capital),
        started_at=started_at or NOW - timedelta(days=OBS_DAYS),
        config={},
    )


def _candidate(user, session):
    return StrategyCandidate.objects.create(
        name="cand",
        hypothesis="探针",
        code_path="strategies/x.py",
        origin=StrategyCandidateOrigin.AGENT,
        status=StrategyCandidateStatus.PAPER_RUNNING,
        proposed_by=user,
        paper_session=session,
    )


def _trace(session, *segments):
    """按 `(status, days)` 序列写时间线，起点 `NOW - 总和`（所有段都是过去时）。

    返回时间线的起点时刻；最后一段若是 `running`，它一直延续到 `NOW`。
    """
    total = timedelta(days=sum(days for _s, days in segments))
    cursor = NOW - total
    rows = []
    for status, days in segments:
        rows.append(
            LiveSessionStatusChange(
                live_session=session,
                from_status="",
                to_status=status,
                changed_at=cursor,
            )
        )
        cursor += timedelta(days=days)
    LiveSessionStatusChange.objects.bulk_create(rows)
    return NOW - total


def _read(candidate, *, now=NOW):
    """采证据并把时钟钉在 `NOW`（观察期的两个数都是相对量，不钉就会随运行时刻漂移）。"""
    return effects.read_paper(candidate, now=now)


def _fill_trades(session, count=None):
    """给会话补足够多的正盈亏订单，让样本量与盈亏两道闸门都过。

    观察期边界用例关心的是**时长**那一半，不该被「成交太少 → 证据不足」挡住：
    不补成交的话，那些用例会落 `INSUFFICIENT_EVIDENCE` 而不是走到时长判定，
    于是「恰 14 天放行」这条断言根本验不到它想验的东西（假绿）。
    """
    for _ in range(count or CONFIG.min_trades):
        _pnl(session, 1)


def _pnl(session, amount, *, when=None):
    """一张带已实现盈亏的订单（`compute_session_stats` 的口径：只看 `realized_pnl`）。"""
    return Order.objects.create(
        user=session.user,
        exchange_account=session.exchange_account,
        symbol=session.symbol,
        side="sell",
        order_type="market",
        quantity=Decimal("1"),
        status="filled",
        live_session=session,
        realized_pnl=Decimal(str(amount)),
        created_at=when or NOW - timedelta(days=1),
    )


# --------------------------------------------------------------------------- #
# 时间线求和本身（`running_duration`）
# --------------------------------------------------------------------------- #


class TestRunningDuration:
    """`running_duration` 是全仓关于「running 时长」的唯一算法，逐条边界钉住。"""

    def test_no_timeline_returns_none_not_zero(self, db):
        """**没有时间线 → None**，不是 0。

        「算不出来」与「跑了 0 秒」必须分开：返回 0 会让一条时间线缺失的会话被判成
        「还没跑满」而**永远等下去**（看起来完全正常），而 None 会让判定判「证据不足」。
        """
        user = _user("rd-none")
        session = _session(user, _strategy(user, "rd-none"), _account(user))
        assert running_duration(session, until=NOW) is None

    def test_never_ran_returns_zero(self, db):
        """时间线存在且明确记录「从没跑过」→ **已知的 0**，不是未知的 None。"""
        user = _user("rd-zero")
        session = _session(user, _strategy(user, "rd-zero"), _account(user))
        _trace(session, ("pending", 5))
        assert running_duration(session, until=NOW) == timedelta(0)

    def test_paused_time_is_not_counted(self, db):
        """**paused 不计入**（D8 的核心口径）：7 跑 + 3 暂停 + 7 跑 = running 14 天。"""
        user = _user("rd-paused")
        session = _session(user, _strategy(user, "rd-paused"), _account(user))
        _trace(session, ("running", 7), ("paused", 3), ("running", 7))
        assert running_duration(session, until=NOW) == timedelta(days=14)

    def test_only_running_segments_accumulate(self, db):
        """多段 running 逐段累加；stopped 段之后的 running 也算（重启场景）。"""
        user = _user("rd-multi")
        session = _session(user, _strategy(user, "rd-multi"), _account(user))
        _trace(
            session,
            ("running", 2),
            ("paused", 1),
            ("running", 3),
            ("stopped", 1),
            ("running", 4),
        )
        assert running_duration(session, until=NOW) == timedelta(days=9)

    def test_until_before_segment_start_never_goes_negative(self, db):
        """`until` 早于最后一段起点 → 那一段不计入，且**不出现负时长**把总额吃掉。"""
        user = _user("rd-early")
        session = _session(user, _strategy(user, "rd-early"), _account(user))
        start = _trace(session, ("running", 1), ("paused", 1), ("running", 2))
        # `until` 落在「暂停」那一段里：只有第一段 1 天该算。
        assert running_duration(session, until=start + timedelta(days=1, hours=12)) == (
            timedelta(days=1)
        )

    def test_paused_tail_does_not_extend_running(self, db):
        """末尾正处于 paused → 不再向外延伸（running 只到那一段的终点）。"""
        user = _user("rd-tail")
        session = _session(user, _strategy(user, "rd-tail"), _account(user))
        _trace(session, ("running", 5), ("paused", 100))
        assert running_duration(session, until=NOW) == timedelta(days=5)


# --------------------------------------------------------------------------- #
# 证据采集（`read_paper`）
# --------------------------------------------------------------------------- #


class TestReadPaper:
    def test_no_session_returns_none(self, db):
        """还没建会话 → None（尚无结论），与另外两层同义——不是「判不合格」。"""
        candidate = StrategyCandidate.objects.create(
            name="c",
            hypothesis="h",
            code_path="p",
            origin=StrategyCandidateOrigin.AGENT,
        )
        assert _read(candidate) is None

    def test_wall_clock_and_running_both_come_from_real_sources(self, db):
        """墙钟取 `started_at → now`，running 取时间线求和；两个数是**不同的数**。"""
        user = _user("rp-both")
        session = _session(user, _strategy(user, "rp-both"), _account(user))
        # started_at 是 14 天前；时间线里只有 11 天在 running。
        _trace(session, ("pending", 3), ("running", 11))
        candidate = _candidate(user, session)
        ev = _read(candidate)
        assert ev.wall_clock_days == pytest.approx(14.0)
        assert ev.running_days == pytest.approx(11.0)

    def test_missing_timeline_gives_none_running_days(self, db):
        """时间线缺失 → `running_days=None`（判「证据不足」），**不是 0**。"""
        user = _user("rp-notl")
        session = _session(user, _strategy(user, "rp-notl"), _account(user))
        candidate = _candidate(user, session)
        ev = _read(candidate)
        assert ev.wall_clock_days == pytest.approx(14.0)
        assert ev.running_days is None

    def test_started_at_null_gives_none_wall_clock(self, db):
        """`started_at` 为空（还没启动过）→ 墙钟算不出来 → None。"""
        user = _user("rp-nostart")
        session = _session(
            user, _strategy(user, "rp-nostart"), _account(user), status="pending"
        )
        session.started_at = None
        session.save(update_fields=["started_at"])
        candidate = _candidate(user, session)
        ev = _read(candidate)
        assert ev.wall_clock_days is None

    def test_stats_are_reused_from_compute_session_stats(self, db):
        """盈亏复用既有纯函数：成交笔数与总额与它逐字一致（D7「不重写一份统计」）。"""
        user = _user("rp-stats")
        session = _session(user, _strategy(user, "rp-stats"), _account(user))
        _trace(session, ("running", 14))
        for amt in (10, -5, 3):
            _pnl(session, amt)
        candidate = _candidate(user, session)
        ev = _read(candidate)
        assert ev.closed_trades == 3
        # 1000 本金，净 +8 → 0.8%
        assert ev.return_pct == pytest.approx(0.8)

    def test_orders_without_realized_pnl_are_excluded(self, db):
        """只有带 `realized_pnl` 的订单计入——开仓单不算（既有口径，不另立一套）。"""
        user = _user("rp-openonly")
        session = _session(user, _strategy(user, "rp-openonly"), _account(user))
        _trace(session, ("running", 14))
        Order.objects.create(
            user=user,
            exchange_account=session.exchange_account,
            symbol=session.symbol,
            side="buy",
            order_type="market",
            quantity=Decimal("1"),
            status="filled",
            live_session=session,
            realized_pnl=None,
        )
        candidate = _candidate(user, session)
        ev = _read(candidate)
        assert ev.closed_trades == 0

    def test_zero_capital_gives_none_return_not_zero(self, db):
        """本金为 0 → 百分比**没有意义** → None（判证据不足），而不是 0.0。

        0.0 是一个**看起来合规**的「没赚钱」，会直接把候选否掉；None 才会让它判
        「证据不足」并继续说「判不了」。
        """
        user = _user("rp-zerocap")
        session = _session(
            user, _strategy(user, "rp-zerocap"), _account(user), capital="0"
        )
        _trace(session, ("running", 14))
        _pnl(session, 5)
        candidate = _candidate(user, session)
        ev = _read(candidate)
        assert ev.closed_trades == 1
        assert ev.return_pct is None

    def test_risk_hits_only_count_hard_limit_for_this_session(self, db):
        """风控命中按 `live_session` 归集，**只算 `hard_limit`**（Q2 定稿口径）。"""
        user = _user("rp-hits")
        session = _session(user, _strategy(user, "rp-hits"), _account(user))
        other = _session(
            user,
            _strategy(user, "rp-hits-other"),
            _account(user, label="另一个"),
            status="pending",
        )
        _trace(session, ("running", 14))
        RiskEvent.objects.create(
            level="P0", event_type="hard_limit", message="a", live_session=session
        )
        RiskEvent.objects.create(
            level="P0", event_type="hard_limit", message="b", live_session=session
        )
        # 浮亏：账户级、不属于本会话 → 不计入（且它本来就不该被归到候选头上）
        RiskEvent.objects.create(
            level="P1", event_type="floating_loss", message="c", live_session=None
        )
        # 别的会话的命中：不能算到本候选头上
        RiskEvent.objects.create(
            level="P0", event_type="hard_limit", message="d", live_session=other
        )
        candidate = _candidate(user, session)
        ev = _read(candidate)
        assert ev.risk_hits == 2

    @pytest.mark.parametrize("status", ["stopped", "error"])
    def test_session_ended_flag(self, db, status):
        """`stopped` / `error` 都算「会话已结束」——判定层靠这一位区分「还在观察」。"""
        user = _user(f"rp-ended-{status}")
        session = _session(
            user, _strategy(user, f"rp-ended-{status}"), _account(user), status=status
        )
        _trace(session, ("running", 3))
        candidate = _candidate(user, session)
        assert _read(candidate).session_ended is True

    @pytest.mark.parametrize("status", ["running", "paused", "pending"])
    def test_session_not_ended_flag(self, db, status):
        """`running` / `paused` / `pending` 都还在观察（`paused` 只是暂停止损/止盈）。"""
        user = _user(f"rp-live-{status}")
        session = _session(
            user, _strategy(user, f"rp-live-{status}"), _account(user), status=status
        )
        _trace(session, ("running", 3))
        candidate = _candidate(user, session)
        assert _read(candidate).session_ended is False

    def test_caveats_include_stats_and_risk_hit_disclosure(self, db):
        """**交付 7**：既有两条 caveats + 风控命中口径 + 观察期口径，一条都不能少。"""
        user = _user("rp-caveats")
        session = _session(user, _strategy(user, "rp-caveats"), _account(user))
        _trace(session, ("running", 14))
        candidate = _candidate(user, session)
        _read(candidate)
        caveats = effects.take_paper_caveats(candidate)
        assert len(caveats) == 4
        joined = " ".join(caveats)
        assert "不含未实现部分" in joined
        assert "不含熔断减仓" in joined
        assert "hard_limit" in joined
        assert "paused" in joined

    def test_take_caveats_is_pop_once(self, db):
        """取走即删：第二次取是空的（它是传手，不是状态）。"""
        user = _user("rp-pop")
        session = _session(user, _strategy(user, "rp-pop"), _account(user))
        _trace(session, ("running", 14))
        candidate = _candidate(user, session)
        _read(candidate)
        assert effects.take_paper_caveats(candidate)
        assert effects.take_paper_caveats(candidate) == []


# --------------------------------------------------------------------------- #
# 判定（走真实采集 → 真实判定）
# --------------------------------------------------------------------------- #


def _verdict(candidate, *, config=None, now=NOW):
    """完整走一遍：采集 → 判定（这才是生产唯一的形状）。"""
    ev = effects.read_paper(candidate, now=now)
    return evaluate_candidate(
        CandidateEvidence(
            status=StrategyCandidateStatus.PAPER_RUNNING,
            paper=ev,
            thresholds=config or CONFIG,
        )
    )


class TestObservationBoundary:
    """观察期恰 14 天的边界——四种组合逐个钉。"""

    def test_exactly_14_days_both_passes(self, db):
        """恰 14 天且两个数都够 → **放行**（`>=` 而不是 `>`）。"""
        user = _user("ob-exact")
        session = _session(user, _strategy(user, "ob-exact"), _account(user))
        _trace(session, ("running", 14))
        _fill_trades(session)
        candidate = _candidate(user, session)
        v = _verdict(candidate)
        assert v.next_status is StrategyCandidateStatus.AWAITING_LIVE_APPROVAL
        assert v.reason_code is ReasonCode.PAPER_PASSED

    def test_13_99_days_stays_observing(self, db):
        """差一点点（13.99 天）→ 停在观察中，**不是**失败。"""
        user = _user("ob-1399")
        session = _session(
            user,
            _strategy(user, "ob-1399"),
            _account(user),
            started_at=NOW - timedelta(days=13, hours=23, minutes=45),
        )
        _trace(
            session,
            (
                "running",
                13,
            ),
        )
        candidate = _candidate(user, session)
        v = _verdict(candidate)
        assert v.next_status is StrategyCandidateStatus.PAPER_RUNNING
        assert v.reason_code is ReasonCode.PAPER_OBSERVING

    def test_paused_days_do_not_count_toward_observation(self, db):
        """**本票的枢纽用例**：7 跑 + 3 暂停 + 7 跑 = 墙钟 17 天，但 running 只有 14 天。

        没有时间线表时这个数算不出来，于是一条「实际只醒了 14 天」的会话会因为墙钟
        17 天而被当成跑满——`paused` 那 3 天被算了进去，误差方向偏乐观。
        """
        user = _user("ob-paused")
        session = _session(
            user,
            _strategy(user, "ob-paused"),
            _account(user),
            # started_at 覆盖整段（含暂停那 3 天）：墙钟 17 天
            started_at=NOW - timedelta(days=17),
        )
        _trace(session, ("running", 7), ("paused", 3), ("running", 7))
        _fill_trades(session)
        candidate = _candidate(user, session)
        ev = _read(candidate)
        assert ev.wall_clock_days == pytest.approx(17.0)
        assert ev.running_days == pytest.approx(14.0)
        # 恰 14 天 running → 放行
        assert _verdict(candidate).reason_code is ReasonCode.PAPER_PASSED

    def test_paused_pushes_below_threshold_and_blocks(self, db):
        """暂停把 running 压到 14 天以下 → **不放行**（这才是「暂停不计入」的意义）。

        与上一条构成对照：同样的墙钟（17 天），只因暂停多了一天，running 变成 13 天
        就拦下来。若 paused 被算进 running，这一条会错误放行。
        """
        user = _user("ob-paused-block")
        session = _session(
            user,
            _strategy(user, "ob-paused-block"),
            _account(user),
            started_at=NOW - timedelta(days=17),
        )
        _trace(session, ("running", 7), ("paused", 4), ("running", 6))
        candidate = _candidate(user, session)
        ev = _read(candidate)
        assert ev.wall_clock_days == pytest.approx(17.0)
        assert ev.running_days == pytest.approx(13.0)
        v = _verdict(candidate)
        assert v.next_status is StrategyCandidateStatus.PAPER_RUNNING
        assert v.reason_code is ReasonCode.PAPER_OBSERVING

    def test_enough_running_but_insufficient_wall_clock_blocks(self, db):
        """**只有「墙钟不够而 running 充足」才能钉住墙钟那半个条件。**

        running 是从时间线算的、墙钟是从 `started_at` 算的，两者独立：把 timeout 段
        说成 running（running 99 天）而 `started_at` 只有 13 天前 → 必须拦下。
        少了这条用例，删掉 `wall_clock_days >= required` 那半个条件不会有任何用例变红。
        """
        user = _user("ob-wallonly")
        session = _session(
            user,
            _strategy(user, "ob-wallonly"),
            _account(user),
            started_at=NOW - timedelta(days=13),
        )
        _trace(session, ("running", 99))
        candidate = _candidate(user, session)
        ev = _read(candidate)
        assert ev.running_days == pytest.approx(99.0)
        assert ev.wall_clock_days == pytest.approx(13.0)
        v = _verdict(candidate)
        assert v.next_status is StrategyCandidateStatus.PAPER_RUNNING
        assert v.reason_code is ReasonCode.PAPER_OBSERVING
        assert v.detail["wall_clock_days"] == pytest.approx(13.0)


class TestObservationIncomplete:
    """观察期未满时，会话**还在**与**已结束**是两种不同落点（验收第 3 条）。"""

    def test_not_full_and_still_running_stays_observing(self, db):
        """未满 + 会话还在 → 停 `paper_running` + `paper_observing`，**不是失败**。

        这一条防的是把验收那句「观察期未满 → paper_failed」字面执行：真那样，每个刚
        建好的候选都会立刻判失败。
        """
        user = _user("oi-alive")
        session = _session(
            user,
            _strategy(user, "oi-alive"),
            _account(user),
            started_at=NOW - timedelta(days=3),
        )
        _trace(session, ("running", 3))
        candidate = _candidate(user, session)
        v = _verdict(candidate)
        assert v.next_status is StrategyCandidateStatus.PAPER_RUNNING
        assert v.reason_code is ReasonCode.PAPER_OBSERVING

    def test_not_full_and_ended_fails_with_incomplete(self, db):
        """未满 + 会话**已结束** → `paper_failed` + `paper_observation_incomplete`。

        原因码必须是「观察期没跑满就结束了」，**不是**「盈亏不达标」——两者都是失败，
        但提出人要据此决定「重新观察」还是「改参数」，混起来他就不知道该做什么。
        """
        user = _user("oi-ended")
        session = _session(
            user,
            _strategy(user, "oi-ended"),
            _account(user),
            status="stopped",
            started_at=NOW - timedelta(days=3),
        )
        _trace(session, ("running", 3))
        candidate = _candidate(user, session)
        v = _verdict(candidate)
        assert v.next_status is StrategyCandidateStatus.PAPER_FAILED
        assert v.reason_code is ReasonCode.PAPER_OBSERVATION_INCOMPLETE
        assert v.reason_code is not ReasonCode.PAPER_RETURN_BELOW_MIN

    def test_missing_timeline_and_ended_fails_not_hangs(self, db):
        """时间线缺失（算不出 running）+ 会话已结束 → 判失败，**不挂在观察中**。

        这是「返回 None 而不是 0」的落点验证：一个已经 stopped 的会话不会再产生任何
        成交，停在观察里等它自己变好等于永远不判定。
        """
        user = _user("oi-notl-ended")
        session = _session(
            user, _strategy(user, "oi-notl-ended"), _account(user), status="stopped"
        )
        candidate = _candidate(user, session)
        v = _verdict(candidate)
        assert v.next_status is StrategyCandidateStatus.PAPER_FAILED
        assert v.reason_code is ReasonCode.INSUFFICIENT_EVIDENCE

    def test_missing_timeline_and_alive_stays_observing(self, db):
        """时间线缺失 + 会话还在 → 停观察中（等下一轮），不误判失败。"""
        user = _user("oi-notl-alive")
        session = _session(user, _strategy(user, "oi-notl-alive"), _account(user))
        candidate = _candidate(user, session)
        v = _verdict(candidate)
        assert v.next_status is StrategyCandidateStatus.PAPER_RUNNING


class TestRiskHitBoundary:
    """风控命中恰好等于上限的边界（验收第 4 条）。"""

    def _passing_session(self, user, name):
        """一条「观察期满、盈亏达标、成交够」的会话——只留风控命中作为唯一变量。"""
        session = _session(user, _strategy(user, name), _account(user))
        _trace(session, ("running", 14))
        for i in range(CONFIG.min_trades):
            _pnl(session, 1 if i % 2 == 0 else 2)
        return session

    def test_hits_equal_to_max_passes(self, db):
        """恰等于上限 → **通过**（「超标」= 超过，所以判据是 `>` 不是 `>=`）。"""
        user = _user("rh-equal")
        session = self._passing_session(user, "rh-equal")
        limit = CONFIG.paper_max_risk_hits
        for _ in range(limit):
            RiskEvent.objects.create(
                level="P0", event_type="hard_limit", message="x", live_session=session
            )
        candidate = _candidate(user, session)
        ev = _read(candidate)
        assert ev.risk_hits == limit
        assert _verdict(candidate).reason_code is ReasonCode.PAPER_PASSED

    def test_hits_above_max_fails(self, db):
        """超过上限一个 → `paper_failed` + `paper_risk_hits_above_max`。"""
        user = _user("rh-above")
        session = self._passing_session(user, "rh-above")
        for _ in range(CONFIG.paper_max_risk_hits + 1):
            RiskEvent.objects.create(
                level="P0", event_type="hard_limit", message="x", live_session=session
            )
        candidate = _candidate(user, session)
        v = _verdict(candidate)
        assert v.next_status is StrategyCandidateStatus.PAPER_FAILED
        assert v.reason_code is ReasonCode.PAPER_RISK_HITS_ABOVE_MAX

    def test_floating_loss_does_not_count_as_hit(self, db):
        """浮亏预警**不算**命中（Q2 定稿）：只算 `hard_limit`（额度类拒单）。

        **这些浮亏事件刻意挂在同一条会话上**：若挂 `live_session=None`，那么无论
        `event_type` 过滤在不在，它们都会被「按会话取」那一步排除掉，用例就是假绿
        ——把 `event_type="hard_limit"` 整个删掉它也照样通过。挂在会话上之后，
        唯一能把它们排除掉的东西就是那个 `event_type` 过滤，探针才真的被钉住。

        （今天生产里浮亏确实落 NULL——它是账户级循环、够不着会话。但判定读的那张表
        将来若有人给浮亏补上会话归属，本用例保证它仍然不计入。）
        """
        user = _user("rh-float")
        session = self._passing_session(user, "rh-float")
        for _ in range(5):
            RiskEvent.objects.create(
                level="P1",
                event_type="floating_loss",
                message="x",
                live_session=session,
            )
        candidate = _candidate(user, session)
        ev = _read(candidate)
        assert ev.risk_hits == 0
        assert _verdict(candidate).reason_code is ReasonCode.PAPER_PASSED

    def test_other_event_types_do_not_count_as_hits(self, db):
        """其余几种事件类型（熔断/对账/心跳）同样不计入——判据是类型白名单，不是「非浮亏」。"""
        user = _user("rh-other-types")
        session = self._passing_session(user, "rh-other-types")
        for event_type in ("circuit_breaker", "reconciliation", "heartbeat"):
            RiskEvent.objects.create(
                level="P0", event_type=event_type, message="x", live_session=session
            )
        candidate = _candidate(user, session)
        assert _read(candidate).risk_hits == 0


class TestReturnBoundary:
    """盈亏门槛与样本量的边界。"""

    def test_return_below_min_fails_with_its_own_code(self, db):
        """盈亏不达标 → `paper_return_below_min`（与「观察期未满」是两个原因码）。"""
        user = _user("rb-below")
        session = _session(user, _strategy(user, "rb-below"), _account(user))
        _trace(session, ("running", 14))
        for _ in range(CONFIG.min_trades):
            _pnl(session, -1)
        candidate = _candidate(user, session)
        v = _verdict(candidate)
        assert v.next_status is StrategyCandidateStatus.PAPER_FAILED
        assert v.reason_code is ReasonCode.PAPER_RETURN_BELOW_MIN
        assert v.detail["return_pct"] < 0

    def test_too_few_trades_is_insufficient_evidence(self, db):
        """观察期满但成交太少 → **证据不足**，不是「盈亏不达标」（D6 的同一把尺）。"""
        user = _user("rb-few")
        session = _session(user, _strategy(user, "rb-few"), _account(user))
        _trace(session, ("running", 14))
        _pnl(session, 100)
        candidate = _candidate(user, session)
        v = _verdict(candidate)
        assert v.reason_code is ReasonCode.INSUFFICIENT_EVIDENCE
        # 会话还在 → 停在观察中（等更多成交），不判失败
        assert v.next_status is StrategyCandidateStatus.PAPER_RUNNING

    def test_boundary_exactly_min_trades_passes(self, db):
        """恰 `min_trades` 笔 → 样本量达标（`<` 而不是 `<=`）。"""
        user = _user("rb-exact")
        session = _session(user, _strategy(user, "rb-exact"), _account(user))
        _trace(session, ("running", 14))
        for _ in range(CONFIG.min_trades):
            _pnl(session, 1)
        candidate = _candidate(user, session)
        v = _verdict(candidate)
        assert v.reason_code is ReasonCode.PAPER_PASSED

    def test_zero_return_passes_at_default_threshold(self, db):
        """`paper_min_return_pct` 默认 0，而判据是 `<`：不赚不亏**放行**。

        构造严格零和：一半 +1、一半 -1（`min_trades` 是偶数 30，除得尽）。
        """
        user = _user("rb-zero")
        session = _session(user, _strategy(user, "rb-zero"), _account(user))
        _trace(session, ("running", 14))
        for i in range(CONFIG.min_trades):
            _pnl(session, 1 if i < CONFIG.min_trades // 2 else -1)
        candidate = _candidate(user, session)
        ev = _read(candidate)
        assert ev.closed_trades == CONFIG.min_trades
        assert Decimal(str(ev.return_pct)) == Decimal("0")
        assert _verdict(candidate).reason_code is ReasonCode.PAPER_PASSED
