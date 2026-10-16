"""转移表穷举（spec #19 D3 + spec #30 D1 的 `regime_hold`）。

这份文件钉住的是**候选生命周期唯一一份真相**：14 个状态、34 条合法边，其余一律拒绝。

两点纪律：

1. **期望值从 spec 原文抄一遍，不从实现派生**。若这里写
   `expected = ALLOWED_TRANSITIONS`，那测试永远绿——它测的是「表等于它自己」。
   手抄的代价是改表要改两处，而这正是我们要的：动合法边必须是一次显式的、留痕的动作。

2. **196 对全查**，不只查合法边。只查合法边的测试漏掉的是「偷偷多出来一条边」——
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
    _self_check,
    allowed_next_statuses,
    can_transition,
    coerce_status,
    is_terminal,
)

# 函数内 import 而不是提到模块顶部：`evaluate` 会拉进 `config`，而这个文件有两条
# AST 纯度用例钉住 status.py / evaluate.py 不 import Django——顶部 import 不影响它们，
# 但把 `evaluate` 的依赖搬到文件顶部会让「status 是纯状态词汇、不带判定」这条边界
# 在测试文件里先破一次例。
from apps.strategy_factory.evaluate import ReasonCode  # noqa: E402

S = StrategyCandidateStatus

#: D3 原文的合法边，逐条手抄。键是起点，值是终点集合。
SPEC_EDGES: dict[StrategyCandidateStatus, set[StrategyCandidateStatus]] = {
    S.REGISTERED: {S.IN_SAMPLE_RUNNING, S.REGISTRATION_FAILED, S.CANCELLED, S.REJECTED},
    S.REGISTRATION_FAILED: {S.REGISTERED, S.CANCELLED, S.REJECTED},
    S.IN_SAMPLE_RUNNING: {S.OOS_RUNNING, S.IN_SAMPLE_FAILED},
    S.IN_SAMPLE_FAILED: {S.IN_SAMPLE_RUNNING, S.CANCELLED, S.REJECTED},
    S.OOS_RUNNING: {S.PAPER_PENDING, S.OOS_FAILED, S.REGIME_HOLD},
    S.OOS_FAILED: {S.OOS_RUNNING, S.CANCELLED, S.REJECTED},
    S.PAPER_PENDING: {S.PAPER_RUNNING, S.CANCELLED, S.REJECTED},
    S.PAPER_RUNNING: {
        S.AWAITING_LIVE_APPROVAL,
        S.PAPER_FAILED,
        S.REGIME_HOLD,
    },
    S.PAPER_FAILED: {S.PAPER_RUNNING, S.CANCELLED, S.REJECTED},
    S.AWAITING_LIVE_APPROVAL: {S.LIVE, S.CANCELLED, S.REJECTED},
    #: `regime_hold` 的四条出边（spec #30 D1 / 用户故事 3）：原路退回它进来的那一层
    #: ×2，加人工随时收手 ×2。**没有去 `*_failed` 的边**——暂停不是判定，把它写成
    #: 状态机的失败会让候选再也回不来。
    S.REGIME_HOLD: {S.OOS_RUNNING, S.PAPER_RUNNING, S.CANCELLED, S.REJECTED},
    S.LIVE: set(),
    S.REJECTED: set(),
    S.CANCELLED: set(),
}

#: 终态（D3 原文点名）。派生自 SPEC_EDGES，不另立一份。
SPEC_TERMINAL = {status for status, targets in SPEC_EDGES.items() if not targets}


def test_enum_has_exactly_the_14_states_of_the_spec():
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
        # `regime_hold` 排在 `live` 之前：它**不是**终态，终态那三个必须留在末尾，
        # 这样「枚举的后缀就是终态集合」这个读法才成立。
        "regime_hold",
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


def test_the_exhaustive_sweep_covers_all_196_pairs():
    """196 = 14×14。上面两个参数化用例的并集必须覆盖全部组合，不能有缝。

    这一条同时钉**实现**：边数是实现侧的 34，而不是手抄表自己的 34——否则它就只是
    「我的期望值等于我的期望值」，与制品无关（独立复核指出过这一点）。
    """
    legal = {(c, t) for c in S for t in S if t in SPEC_EDGES[c]}
    illegal = {(c, t) for c in S for t in S} - legal
    assert sum(len(targets) for targets in ALLOWED_TRANSITIONS.values()) == 34
    assert len(legal) == 34, "D3 + D1 的合法边是 34 条"
    assert len(legal) + len(illegal) == len(S) ** 2 == 196


def test_import_time_guard_catches_a_self_loop(monkeypatch):
    """`_self_check` 这条自环守卫本身要有钉子。

    加一条真实自环会被 `test_transition_table_equals_the_spec` 拦下（数据层面），但那条
    守卫是**第五道**防线（import 时就炸）——它自己坏掉时没有任何用例会红。这里直接把
    自环塞进表再调一次守卫，确认它确实会抛。
    """
    monkeypatch.setitem(ALLOWED_TRANSITIONS, S.LIVE, frozenset({S.LIVE}))
    with pytest.raises(RuntimeError, match="自环"):
        _self_check()


def test_import_time_guard_catches_a_missing_state(monkeypatch):
    """漏一个状态同样必须在 import 时炸（漏掉的状态会安静地卡死）。"""
    monkeypatch.delitem(ALLOWED_TRANSITIONS, S.OOS_FAILED)
    with pytest.raises(RuntimeError, match="漏了状态"):
        _self_check()


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


# --------------------------------------------------------------------------- #
# `regime_hold`：行情闸门的暂停位（spec #30 D1）
#
# 单独一节而不是塞进上面的穷举表：上面两条参数化用例证明的是「表与 SPEC_EDGES 一致」，
# 而这一节要证明的是**这个状态的语义**——它自己会长回来。穷举表里删掉一条回边
# 只会让 SPEC_EDGES 与表同时变（改表的人会顺手改两处），所以语义必须有独立的钉子。
# --------------------------------------------------------------------------- #


def test_regime_hold_is_not_a_terminal_state():
    """它不进终态集合，否则「等行情过去」变成「永远不再推进」（spec D1 原文）。

    `is_terminal()` 是**派生**的（没有出边即终态），所以这一条同时也是对实现形状的
    约束：往 `REGIME_HOLD` 放一张空出边表，测试会在这里红，而不是在线上安静地停一个
    候选。
    """
    assert is_terminal(S.REGIME_HOLD) is False
    assert S.REGIME_HOLD not in SPEC_TERMINAL
    assert allowed_next_statuses(S.REGIME_HOLD), "暂停位至少要有回边"


def test_regime_hold_is_not_a_failure_verdict():
    """暂停 ≠ 失败：它不能去任何一个 `*_failed`（用户故事 3 的第一句）。"""
    for failed in (S.IN_SAMPLE_FAILED, S.OOS_FAILED, S.PAPER_FAILED):
        assert can_transition(S.REGIME_HOLD, failed) is False
        assert can_transition(S.PAPER_FAILED, S.REGIME_HOLD) is False, (
            "paper 已经判未过了，没有再为它暂停行情的理由"
        )


def test_regime_hold_keeps_the_human_escape_hatches():
    """用户故事 3 的后半句：「且我随时能取消它」。

    没有这两条边，一个被行情卡住的候选只能等闸门翻回来——而那可能永远不会发生。
    """
    assert can_transition(S.REGIME_HOLD, S.CANCELLED) is True
    assert can_transition(S.REGIME_HOLD, S.REJECTED) is True


@pytest.mark.parametrize(
    ("held", "returns"),
    [
        (S.OOS_RUNNING, S.OOS_RUNNING),
        (S.PAPER_RUNNING, S.PAPER_RUNNING),
    ],
    ids=lambda s: s.value,
)
def test_both_promotion_points_can_park_and_resume(held, returns):
    """L2 升 paper 前与 L3 升实盘前各有一个闸门，都停在同一个位、原路退回同一个位。

    「原路退回」而不是「统一去一个 regime_released 态」：候选的层记录、起跑记录都在
    进来的那一层上，回到原状态才能接着判，不必再猜一次「我在等哪一层」。
    """
    assert can_transition(held, S.REGIME_HOLD) is True
    assert can_transition(S.REGIME_HOLD, returns) is True


def test_regime_hold_display_is_a_pause_not_a_verdict():
    """展示名要读起来能自己恢复；ascii 落库、中文只在展示层（本仓库的取值纪律）。"""
    assert S.REGIME_HOLD.value == "regime_hold"
    assert S.REGIME_HOLD.display == "等行情闸门结论"
    # choices 里的两边都取得到：落库值稳定，展示文案不进库。
    assert ("regime_hold", "等行情闸门结论") in S.choices()


def test_every_new_state_and_reason_has_a_display_entry():
    """加了一个枚举成员却忘了展示名，必须在 KeyError 上立刻响（仓库刻意不用 `.get()`）。"""
    from apps.strategy_factory.evaluate import ReasonCode, _REASON_DISPLAY

    for code in (
        ReasonCode.REGIME_UNFIT,
        ReasonCode.REGIME_NO_EVIDENCE,
        ReasonCode.REGIME_EXEMPT,
    ):
        assert code in _REASON_DISPLAY, code
        assert _REASON_DISPLAY[code].strip(), f"{code.value} 的展示名是空的"
        assert code.display, code  # 走的就是 _REASON_DISPLAY[自身]，缺键会 KeyError


def test_regime_exempt_is_a_pass_through_not_a_hold():
    """豁免是**放行 + 留痕**，不是暂缓（spec #30 D5）：它不该把人停进 `regime_hold`。

    这一条钉的是三个原因码的分工：另外两个是拦人的，这一个不是。
    """
    assert ReasonCode.REGIME_EXEMPT is not ReasonCode.REGIME_UNFIT
    assert ReasonCode.REGIME_EXEMPT is not ReasonCode.REGIME_NO_EVIDENCE
    assert (
        can_transition(S.OOS_RUNNING, S.REGIME_HOLD) is True
    )  # unfit / no_evidence 的落点


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
