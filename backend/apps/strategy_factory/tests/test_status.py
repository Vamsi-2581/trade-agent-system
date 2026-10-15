"""转移表穷举（spec #19 D3）。

这份文件钉住的是**候选生命周期唯一一份真相**：13 个状态、28 条合法边，其余一律拒绝。

两点纪律：

1. **期望值从 spec 原文抄一遍，不从实现派生**。若这里写
   `expected = ALLOWED_TRANSITIONS`，那测试永远绿——它测的是「表等于它自己」。
   手抄的代价是改表要改两处，而这正是我们要的：动合法边必须是一次显式的、留痕的动作。

2. **169 对全查**，不只查合法边。只查合法边的测试漏掉的是「偷偷多出来一条边」——
   而多出来的边恰恰是最危险的（一条不该有的 `paper_running → live` 能让未经人工卡点
   的候选直接上实盘）。
"""

from __future__ import annotations

import pytest

from apps.strategy_factory.status import (
    ALLOWED_TRANSITIONS,
    InvalidTransitionError,
    StrategyCandidateOrigin,
    StrategyCandidateStatus,
    allowed_next_statuses,
    can_transition,
    coerce_status,
    is_terminal,
)

S = StrategyCandidateStatus

#: D3 原文的合法边，逐条手抄。键是起点，值是终点集合。
SPEC_EDGES: dict[StrategyCandidateStatus, set[StrategyCandidateStatus]] = {
    S.REGISTERED: {S.IN_SAMPLE_RUNNING, S.REGISTRATION_FAILED, S.CANCELLED, S.REJECTED},
    S.REGISTRATION_FAILED: {S.REGISTERED, S.CANCELLED, S.REJECTED},
    S.IN_SAMPLE_RUNNING: {S.OOS_RUNNING, S.IN_SAMPLE_FAILED},
    S.IN_SAMPLE_FAILED: {S.IN_SAMPLE_RUNNING, S.CANCELLED, S.REJECTED},
    S.OOS_RUNNING: {S.PAPER_PENDING, S.OOS_FAILED},
    S.OOS_FAILED: {S.OOS_RUNNING, S.CANCELLED, S.REJECTED},
    S.PAPER_PENDING: {S.PAPER_RUNNING, S.CANCELLED, S.REJECTED},
    S.PAPER_RUNNING: {S.AWAITING_LIVE_APPROVAL, S.PAPER_FAILED},
    S.PAPER_FAILED: {S.PAPER_RUNNING, S.CANCELLED, S.REJECTED},
    S.AWAITING_LIVE_APPROVAL: {S.LIVE, S.CANCELLED, S.REJECTED},
    S.LIVE: set(),
    S.REJECTED: set(),
    S.CANCELLED: set(),
}

#: 终态（D3 原文点名）。派生自 SPEC_EDGES，不另立一份。
SPEC_TERMINAL = {status for status, targets in SPEC_EDGES.items() if not targets}


def test_enum_has_exactly_the_13_states_of_the_spec():
    assert [s.value for s in S] == [
        "registered",
        "registration_failed",
        "in_sample_running",
        "in_sample_failed",
        "oos_running",
        "oos_failed",
        "paper_pending",
        "paper_running",
        "paper_failed",
        "awaiting_live_approval",
        "live",
        "rejected",
        "cancelled",
    ]


def test_transition_table_keys_cover_every_state():
    """漏一个状态不能表现为「它没有出边」——那会让候选静默卡死。"""
    assert set(ALLOWED_TRANSITIONS) == set(S)


def test_transition_table_equals_the_spec():
    """表与 spec 原文逐状态比对（相等，不多不少）。"""
    actual = {status: set(targets) for status, targets in ALLOWED_TRANSITIONS.items()}
    assert actual == SPEC_EDGES


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (current, target)
        for current in S
        for target in S
        if target in SPEC_EDGES[current]
    ],
)
def test_every_legal_edge_is_accepted(current, target):
    assert can_transition(current, target) is True
    assert target in allowed_next_statuses(current)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (current, target)
        for current in S
        for target in S
        if target not in SPEC_EDGES[current]
    ],
)
def test_every_illegal_edge_is_rejected(current, target):
    assert can_transition(current, target) is False
    assert target not in allowed_next_statuses(current)


def test_the_exhaustive_sweep_covers_all_169_pairs():
    """169 = 13×13。上面两个参数化用例的并集必须覆盖全部组合，不能有缝。"""
    legal = {(c, t) for c in S for t in S if t in SPEC_EDGES[c]}
    illegal = {(c, t) for c in S for t in S} - legal
    assert len(legal) == 28, "D3 的合法边是 28 条"
    assert len(legal) + len(illegal) == len(S) ** 2 == 169


@pytest.mark.parametrize("status", sorted(SPEC_TERMINAL, key=lambda s: s.value))
def test_terminal_states_have_no_outgoing_edges(status):
    assert allowed_next_statuses(status) == frozenset()
    assert is_terminal(status) is True


@pytest.mark.parametrize(
    "status",
    sorted(set(S) - SPEC_TERMINAL, key=lambda s: s.value),
)
def test_non_terminal_states_have_somewhere_to_go(status):
    """非终态至少有一条出边：否则它是个安静的坟墓。"""
    assert allowed_next_statuses(status)
    assert is_terminal(status) is False


def test_self_loop_is_not_an_edge():
    """自环不是转移（见 status.py 模块 docstring）。"""
    for status in S:
        assert can_transition(status, status) is False


def test_error_message_lists_the_legal_targets():
    """被拒绝的人第一句想问的是「那我能去哪」，所以异常里必须带上合法目标。"""
    with pytest.raises(InvalidTransitionError) as excinfo:
        raise InvalidTransitionError(S.REGISTERED, S.LIVE)
    message = str(excinfo.value)
    assert "registered" in message
    assert "live" in message
    for target in SPEC_EDGES[S.REGISTERED]:
        assert target.value in message


def test_strings_are_coerced_and_unknown_values_fail_loud():
    assert coerce_status("paper_running") is S.PAPER_RUNNING
    assert can_transition("registered", "in_sample_running") is True
    with pytest.raises(ValueError, match="未登记的候选状态"):
        coerce_status("passed")


def test_origin_enum_has_the_three_sources_and_avoids_the_name_source():
    """D1：三来源，且字段名避开 `source`（`GridSearchJob.source` 已占用同名不同义）。"""
    assert [m.value for m in StrategyCandidateOrigin] == [
        "agent",
        "grid_variant",
        "manual",
    ]
    assert [value for value, _ in StrategyCandidateOrigin.choices()] == [
        "agent",
        "grid_variant",
        "manual",
    ]
    assert [value for value, _ in S.choices()] == [m.value for m in S]
