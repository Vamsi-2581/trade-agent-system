"""`StrategyCandidate` 模型与状态守门（spec #19 D1 / D2 / D3）。

这个文件测的是**持久化那一段**：字段形状、外键的删除语义、以及「非法转移一律拒绝」
在模型这一层也成立。

D2 单独有一条钉子：**无论候选被推到哪一层，`Strategy.is_active` 都不许变**。
「候选已上 paper」与「策略已退役」是两根互不干扰的轴——自动化侵蚀人工总开关，
是这份代码里最贵的一类错误（人会以为退役了，实际还在跑）。
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.db.models.deletion import SET_NULL

from apps.exchange.models import ExchangeAccount
from apps.strategy_factory.models import StrategyCandidate
from apps.strategy_factory.status import (
    ALLOWED_TRANSITIONS,
    InvalidTransitionError,
    StrategyCandidateOrigin,
    StrategyCandidateStatus as S,
)
from apps.trading.models import LiveSession, Strategy

User = get_user_model()

#: D1 点名的字段形状。少一个（或名字漂了）都要在这里红。
EXPECTED_FIELDS = {
    "id",
    "name",
    "hypothesis",
    "code_path",
    "origin",
    "source_ref",
    "proposed_by",
    "strategy",
    "status",
    "evidence",
    "paper_session",
    "live_session",
    "paper_account",
    "live_account",
    "created_at",
    "updated_at",
    "decided_at",
}


@pytest.fixture
def user(db):
    return User.objects.create_user(
        email="candidate@example.com", username="candidate", password="pw"
    )


@pytest.fixture
def strategy(db):
    # is_active=False：D2 的「已退役」样本，用来证明流水线不会把它改回来。
    return Strategy.objects.create(name="donchian_atr", code_path="strategies/x.py")


@pytest.fixture
def account(db):
    return ExchangeAccount.objects.create(
        exchange="binance",
        label="paper-testnet",
        api_key_enc=b"k",
        api_secret_enc=b"s",
        testnet=True,
    )


def _candidate(strategy=None, **kwargs) -> StrategyCandidate:
    defaults = dict(
        name="ema_crossover_v9",
        hypothesis="快线上穿慢线时做多，趋势行情里赚取中间段",
        code_path="~/.tradelogx/strategies/ema_crossover_v9.py",
        origin=StrategyCandidateOrigin.AGENT.value,
        source_ref={"agent_session": "sess-1"},
        strategy=strategy,
    )
    defaults.update(kwargs)
    return StrategyCandidate.objects.create(**defaults)


# --------------------------------------------------------------------------- #
# 字段形状
# --------------------------------------------------------------------------- #


def test_field_shape_matches_the_spec(db):
    concrete = {f.name for f in StrategyCandidate._meta.get_fields() if f.concrete}
    assert concrete == EXPECTED_FIELDS


def test_defaults(db, strategy):
    candidate = _candidate(strategy=strategy)
    assert candidate.status == S.REGISTERED.value
    assert candidate.status_enum is S.REGISTERED
    assert candidate.evidence == {}
    assert candidate.source_ref == {"agent_session": "sess-1"}
    assert candidate.created_at is not None
    assert candidate.updated_at is not None
    assert candidate.decided_at is None
    assert candidate.paper_session is None
    assert candidate.live_session is None
    assert candidate.paper_account is None
    assert candidate.live_account is None


def test_choices_follow_the_enums(db):
    status_field = StrategyCandidate._meta.get_field("status")
    origin_field = StrategyCandidate._meta.get_field("origin")
    assert [value for value, _ in status_field.choices] == [m.value for m in S]
    assert [value for value, _ in origin_field.choices] == [
        m.value for m in StrategyCandidateOrigin
    ]


def test_all_nullable_links_use_set_null(db):
    """删掉被引用的行不该连候选一起删：失败过的候选要能回答「为什么当初没上」（D26）。"""
    for field_name in (
        "proposed_by",
        "strategy",
        "paper_session",
        "live_session",
        "paper_account",
        "live_account",
    ):
        field = StrategyCandidate._meta.get_field(field_name)
        assert field.null is True, field_name
        assert field.remote_field.on_delete is SET_NULL, field_name


def test_candidate_is_registration_born_without_a_strategy_row(db):
    """登记失败时没有 `strategy` 行也必须能存（D1：可空）。"""
    candidate = _candidate(strategy=None, status=S.REGISTRATION_FAILED.value)
    assert candidate.strategy is None
    assert candidate.status_enum is S.REGISTRATION_FAILED


# --------------------------------------------------------------------------- #
# 状态守门
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("status", list(S), ids=lambda s: s.value)
def test_legal_edges_are_applied(db, status):
    candidate = _candidate(status=status.value, strategy=None)
    for target in ALLOWED_TRANSITIONS[status]:
        fresh = StrategyCandidate.objects.get(pk=candidate.pk)
        assert fresh.can_transition_to(target) is True
        assert fresh.transition_to(target) is target
        assert fresh.status == target.value


@pytest.mark.parametrize("status", list(S), ids=lambda s: s.value)
def test_illegal_edges_are_rejected_by_the_model(db, status):
    """169 对里除合法边以外的全部（含自环）都必须在模型层被拒。

    这条与 `test_status.py` 的分工：那边用 spec 原文手抄的期望值钉住「表本身对不对」，
    这边用表本身钉住「模型有没有绕过表」。
    """
    candidate = _candidate(status=status.value, strategy=None)
    for target in S:
        if target in ALLOWED_TRANSITIONS[status]:
            continue
        with pytest.raises(InvalidTransitionError):
            candidate.transition_to(target)
        assert candidate.status == status.value, "被拒绝的转移不许改动状态"


def test_transition_accepts_plain_strings(db):
    """状态值从库里取回是字符串（视图层、任务层手里都是字符串）。"""
    candidate = _candidate(status=S.IN_SAMPLE_RUNNING.value, strategy=None)
    assert candidate.transition_to("oos_running") is S.OOS_RUNNING


def test_unknown_status_value_fails_loud(db):
    candidate = _candidate(strategy=None)
    candidate.status = "passed"  # 手工写坏（迁移/脏数据的形态）
    with pytest.raises(ValueError, match="未登记的候选状态"):
        _ = candidate.status_enum
    with pytest.raises(ValueError, match="未登记的候选状态"):
        candidate.allowed_next_statuses()


def test_terminal_candidates_cannot_move(db):
    for status in (S.LIVE, S.REJECTED, S.CANCELLED):
        candidate = _candidate(status=status.value, strategy=None)
        assert candidate.allowed_next_statuses() == frozenset()
        for target in S:
            with pytest.raises(InvalidTransitionError):
                candidate.transition_to(target)


# --------------------------------------------------------------------------- #
# D2：绝不碰 Strategy.is_active
# --------------------------------------------------------------------------- #


def test_full_funnel_never_touches_strategy_is_active(db, strategy, user):
    """把候选一路推到实盘，`Strategy.is_active` 必须还是原值（这里是 False）。"""
    assert strategy.is_active is False
    candidate = _candidate(strategy=strategy, proposed_by=user)
    for target in (
        S.IN_SAMPLE_RUNNING,
        S.OOS_RUNNING,
        S.PAPER_PENDING,
        S.PAPER_RUNNING,
        S.AWAITING_LIVE_APPROVAL,
        S.LIVE,
    ):
        candidate.transition_to(target)
        candidate.save()

    strategy.refresh_from_db()
    assert strategy.is_active is False, (
        "候选生命周期与人工总开关是两根互不干扰的轴（D2）——"
        "流水线把退役策略改回启用是自动化侵蚀人工开关"
    )


def test_recording_a_first_layer_verdict_never_touches_is_active(db, strategy):
    """登记后落第一条判定结论（写 evidence）同样不许碰到 `Strategy`。"""
    candidate = _candidate(strategy=strategy)
    candidate.evidence = {
        "in_sample": {"reason_code": "in_sample_passed", "sharpe_ratio": 2.1}
    }
    candidate.transition_to(S.IN_SAMPLE_RUNNING)
    candidate.save()

    strategy.refresh_from_db()
    assert strategy.is_active is False


# --------------------------------------------------------------------------- #
# 外键删除语义
# --------------------------------------------------------------------------- #


def test_deleting_the_referenced_rows_nulls_the_links(db, user, strategy, account):
    paper = LiveSession.objects.create(
        user=user,
        strategy=strategy,
        symbol="BTC/USDT",
        mode="paper",
        status="running",
        exchange_account=account,
        initial_capital=Decimal("1000"),
    )
    candidate = _candidate(
        strategy=strategy,
        proposed_by=user,
        paper_session=paper,
        paper_account=account,
    )
    candidate_id = candidate.pk

    live = LiveSession.objects.create(
        user=user,
        strategy=strategy,
        symbol="BTC/USDT",
        mode="live",
        status="running",
        initial_capital=Decimal("1000"),
    )
    candidate.live_session = live
    candidate.save()

    paper.delete()
    live.delete()
    strategy.delete()
    account.delete()
    user.delete()

    stored = StrategyCandidate.objects.get(pk=candidate_id)
    assert stored.paper_session is None
    assert stored.live_session is None
    assert stored.strategy is None
    assert stored.paper_account is None
    assert stored.proposed_by is None


def test_table_name_is_stable(db):
    """表名是迁移契约：改名等于让既有数据找不到家。"""
    assert StrategyCandidate._meta.db_table == "strategy_candidates"
    assert "strategy_candidates" in connection.introspection.table_names()
