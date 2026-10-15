"""判定表（spec #19 D14 的单一测试缝）。

`evaluate_candidate` 是整条漏斗唯一做判断的地方，所以这个文件是表驱动的穷举：每一条
边界都从一个具体数字出发，断言「下一步落在哪 + 原因码是什么」。

三条被刻意钉住的口径：

1. **边界是不对称的**：夏普恰 1.5 放行（`>=`），衰减恰 30% 拦下（`<`）。原文
   「夏普 ≥1.5 且样本外衰减 <30%」——照抄方向，不为了统一而改成同一种比较。
2. **证据不足 ≠ 不合格**：「29 笔」与「夏普 1.2」都落 `in_sample_failed`，但原因码
   一个是 `INSUFFICIENT_EVIDENCE`、一个是 `IN_SAMPLE_SHARPE_BELOW_MIN`。分折缺数据
   同理走「证据不足」而不是「衰减超标」。
3. **停在本状态不是一次转移**：`next_status == 当前状态` 的那些结论必须是
   `can_transition` 判为假的——否则「观察期没满」会看起来像一条合法边。

本文件**不用 `django_db`**：这正是「evaluate_candidate 无 DB 依赖」那条验收的形态。
另有一条 AST 守卫证明这三个纯模块连 Django 都没 import。
"""

from __future__ import annotations

import ast
import dataclasses
import json
from pathlib import Path

import pytest

from apps.strategy_factory import config as config_module
from apps.strategy_factory import evaluate as evaluate_module
from apps.strategy_factory.config import FactoryConfig
from apps.strategy_factory.evaluate import (
    CandidateEvidence,
    InSampleEvidence,
    OosEvidence,
    PaperEvidence,
    ReasonCode,
    Verdict,
    evaluate_candidate,
)
from apps.strategy_factory.status import StrategyCandidateStatus as S
from apps.strategy_factory.status import can_transition, is_terminal

#: 显式用默认配置，不吃环境变量——边界用例的数字必须与开发者机器上的 .env 无关。
CFG = FactoryConfig()


def _candidate_live_config(**overrides) -> FactoryConfig:
    return dataclasses.replace(CFG, **overrides)


# --------------------------------------------------------------------------- #
# 纯度守卫
# --------------------------------------------------------------------------- #

PURE_MODULES = ("status.py", "config.py", "evaluate.py", "walk_forward.py")
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


@pytest.mark.parametrize("module_file", PURE_MODULES)
def test_pure_modules_do_not_import_django(module_file):
    """判定缝必须能在没有 Django/DB 的情况下被读懂、被测试。

    用 AST 而不是字符串匹配：docstring 里到处都在讲 Django，字符串匹配会被它们骗过去。
    """
    path = Path(evaluate_module.__file__).parent / module_file
    imports = _imported_modules(path)
    roots = {name.split(".")[0] for name in imports}
    assert not (roots & set(FORBIDDEN_IMPORT_ROOTS)), (
        f"{module_file} 引入了 {sorted(roots & set(FORBIDDEN_IMPORT_ROOTS))}——"
        "判定缝一旦依赖 Django，D14 的单测缝就没了"
    )


def test_evaluate_does_not_import_the_model_module():
    imports = _imported_modules(Path(evaluate_module.__file__))
    assert not any(name.endswith("strategy_factory.models") for name in imports)


def test_evaluate_reaches_the_pure_state_module_only():
    assert evaluate_module.CONFIG is config_module.CONFIG


# --------------------------------------------------------------------------- #
# 结论形态
# --------------------------------------------------------------------------- #


def test_verdict_shape_and_json_safe_detail():
    verdict = evaluate_candidate(CandidateEvidence(status=S.REGISTERED))
    assert isinstance(verdict, Verdict)
    assert verdict.next_status is S.IN_SAMPLE_RUNNING
    assert verdict.reason_code is ReasonCode.IN_SAMPLE_RUN_STARTED
    assert isinstance(verdict.detail, dict)
    assert json.loads(json.dumps(verdict.detail)) == verdict.detail


@pytest.mark.parametrize(
    "status", [S.LIVE, S.REJECTED, S.CANCELLED], ids=lambda s: s.value
)
def test_terminal_states_never_move(status):
    verdict = evaluate_candidate(CandidateEvidence(status=status))
    assert verdict.next_status is status
    assert verdict.reason_code is ReasonCode.TERMINAL
    assert verdict.holds(status)


@pytest.mark.parametrize(
    "status", [s for s in S if not is_terminal(s)], ids=lambda s: s.value
)
def test_master_switch_holds_every_non_terminal_candidate(status):
    """总开关关掉的是**自动推进**：任何非终态都停在原地，原因码说得清是为什么。"""
    verdict = evaluate_candidate(
        CandidateEvidence(
            status=status,
            thresholds=_candidate_live_config(auto_advance_enabled=False),
            in_sample=InSampleEvidence(sharpe_ratio=9.0, total_trades=999),
            oos=OosEvidence(decay_pct=0.0, fold_count=5),
            paper=PaperEvidence(
                wall_clock_days=99,
                running_days=99,
                closed_trades=99,
                return_pct=9.0,
                risk_hits=0,
                account_ready=True,
            ),
        )
    )
    assert verdict.next_status is status
    assert verdict.reason_code is ReasonCode.AUTO_ADVANCE_DISABLED


def test_registered_starts_the_in_sample_run():
    verdict = evaluate_candidate(CandidateEvidence(status=S.REGISTERED))
    assert verdict.next_status is S.IN_SAMPLE_RUNNING
    assert can_transition(S.REGISTERED, verdict.next_status)


def test_registration_failed_waits_for_a_retry():
    """回 registered 的前提是代码被修好并重新登记——那是外部动作，不是判定能下的结论。"""
    verdict = evaluate_candidate(CandidateEvidence(status=S.REGISTRATION_FAILED))
    assert verdict.next_status is S.REGISTRATION_FAILED
    assert verdict.reason_code is ReasonCode.AWAITING_REGISTRATION_RETRY


# --------------------------------------------------------------------------- #
# L1 样本内
# --------------------------------------------------------------------------- #


def test_in_sample_without_evidence_yet_holds():
    verdict = evaluate_candidate(CandidateEvidence(status=S.IN_SAMPLE_RUNNING))
    assert verdict.next_status is S.IN_SAMPLE_RUNNING
    assert verdict.reason_code is ReasonCode.IN_SAMPLE_PENDING


def test_in_sample_sharpe_exactly_at_the_threshold_passes():
    """夏普恰 1.5 → 放行（原文「夏普 ≥1.5」）。"""
    verdict = evaluate_candidate(
        CandidateEvidence(
            status=S.IN_SAMPLE_RUNNING,
            in_sample=InSampleEvidence(sharpe_ratio=1.5, total_trades=30),
        )
    )
    assert verdict.next_status is S.OOS_RUNNING
    assert verdict.reason_code is ReasonCode.IN_SAMPLE_PASSED
    assert verdict.detail["sharpe_ratio"] == 1.5


def test_in_sample_just_below_the_threshold_fails():
    verdict = evaluate_candidate(
        CandidateEvidence(
            status=S.IN_SAMPLE_RUNNING,
            in_sample=InSampleEvidence(sharpe_ratio=1.4999, total_trades=30),
        )
    )
    assert verdict.next_status is S.IN_SAMPLE_FAILED
    assert verdict.reason_code is ReasonCode.IN_SAMPLE_SHARPE_BELOW_MIN


@pytest.mark.parametrize("total_trades", [30, 31, 500])
def test_in_sample_sample_size_at_or_above_the_floor_passes(total_trades):
    verdict = evaluate_candidate(
        CandidateEvidence(
            status=S.IN_SAMPLE_RUNNING,
            in_sample=InSampleEvidence(sharpe_ratio=2.0, total_trades=total_trades),
        )
    )
    assert verdict.next_status is S.OOS_RUNNING


@pytest.mark.parametrize("total_trades", [29, 5, 0])
def test_in_sample_sample_size_below_the_floor_is_insufficient_evidence(total_trades):
    """29 笔不是「夏普不够」，是「证据不足」——原因码必须分得开（D6）。"""
    verdict = evaluate_candidate(
        CandidateEvidence(
            status=S.IN_SAMPLE_RUNNING,
            in_sample=InSampleEvidence(sharpe_ratio=5.0, total_trades=total_trades),
        )
    )
    assert verdict.next_status is S.IN_SAMPLE_FAILED
    assert verdict.reason_code is ReasonCode.INSUFFICIENT_EVIDENCE
    assert verdict.detail["min_trades"] == 30


def test_missing_sample_size_is_insufficient_evidence_not_a_pass():
    verdict = evaluate_candidate(
        CandidateEvidence(
            status=S.IN_SAMPLE_RUNNING,
            in_sample=InSampleEvidence(sharpe_ratio=5.0, total_trades=None),
        )
    )
    assert verdict.next_status is S.IN_SAMPLE_FAILED
    assert verdict.reason_code is ReasonCode.INSUFFICIENT_EVIDENCE


def test_missing_sharpe_is_insufficient_evidence_even_with_enough_trades():
    verdict = evaluate_candidate(
        CandidateEvidence(
            status=S.IN_SAMPLE_RUNNING,
            in_sample=InSampleEvidence(sharpe_ratio=None, total_trades=100),
        )
    )
    assert verdict.next_status is S.IN_SAMPLE_FAILED
    assert verdict.reason_code is ReasonCode.INSUFFICIENT_EVIDENCE


def test_in_sample_backtest_error_is_its_own_reason_code():
    verdict = evaluate_candidate(
        CandidateEvidence(
            status=S.IN_SAMPLE_RUNNING,
            in_sample=InSampleEvidence(error="策略文件语法错误", total_trades=0),
        )
    )
    assert verdict.next_status is S.IN_SAMPLE_FAILED
    assert verdict.reason_code is ReasonCode.IN_SAMPLE_BACKTEST_FAILED
    assert verdict.detail["error"] == "策略文件语法错误"


def test_in_sample_threshold_is_configurable():
    cfg = _candidate_live_config(sharpe_min=2.0)
    below = evaluate_candidate(
        CandidateEvidence(
            status=S.IN_SAMPLE_RUNNING,
            in_sample=InSampleEvidence(sharpe_ratio=1.9, total_trades=30),
            thresholds=cfg,
        )
    )
    at = evaluate_candidate(
        CandidateEvidence(
            status=S.IN_SAMPLE_RUNNING,
            in_sample=InSampleEvidence(sharpe_ratio=2.0, total_trades=30),
            thresholds=cfg,
        )
    )
    assert below.next_status is S.IN_SAMPLE_FAILED
    assert at.next_status is S.OOS_RUNNING


# --------------------------------------------------------------------------- #
# L2 样本外
# --------------------------------------------------------------------------- #


def test_oos_without_evidence_yet_holds():
    verdict = evaluate_candidate(CandidateEvidence(status=S.OOS_RUNNING))
    assert verdict.next_status is S.OOS_RUNNING
    assert verdict.reason_code is ReasonCode.OOS_PENDING


def test_oos_decay_just_below_the_cap_passes():
    verdict = evaluate_candidate(
        CandidateEvidence(
            status=S.OOS_RUNNING, oos=OosEvidence(decay_pct=29.99, fold_count=5)
        )
    )
    assert verdict.next_status is S.PAPER_PENDING
    assert verdict.reason_code is ReasonCode.OOS_PASSED


def test_oos_decay_exactly_at_the_cap_is_rejected():
    """原文「衰减 <30% 才放行」：恰 30% 不在「小于」里。"""
    verdict = evaluate_candidate(
        CandidateEvidence(
            status=S.OOS_RUNNING, oos=OosEvidence(decay_pct=30.0, fold_count=5)
        )
    )
    assert verdict.next_status is S.OOS_FAILED
    assert verdict.reason_code is ReasonCode.OOS_DECAY_ABOVE_MAX


def test_oos_data_shortage_is_insufficient_evidence_not_decay():
    """分折缺 K 线 → 证据不足，**不是**衰减超标（D3/测试决策点名的一条）。"""
    verdict = evaluate_candidate(
        CandidateEvidence(
            status=S.OOS_RUNNING,
            oos=OosEvidence(decay_pct=None, fold_count=0, data_sufficient=False),
        )
    )
    assert verdict.next_status is S.OOS_FAILED
    assert verdict.reason_code is ReasonCode.INSUFFICIENT_EVIDENCE
    assert verdict.reason_code is not ReasonCode.OOS_DECAY_ABOVE_MAX


@pytest.mark.parametrize("fold_count", [None, 0])
def test_oos_without_a_single_fold_is_insufficient_evidence(fold_count):
    verdict = evaluate_candidate(
        CandidateEvidence(
            status=S.OOS_RUNNING,
            oos=OosEvidence(decay_pct=1.0, fold_count=fold_count),
        )
    )
    assert verdict.next_status is S.OOS_FAILED
    assert verdict.reason_code is ReasonCode.INSUFFICIENT_EVIDENCE


def test_oos_missing_decay_is_insufficient_evidence():
    verdict = evaluate_candidate(
        CandidateEvidence(
            status=S.OOS_RUNNING, oos=OosEvidence(decay_pct=None, fold_count=5)
        )
    )
    assert verdict.next_status is S.OOS_FAILED
    assert verdict.reason_code is ReasonCode.INSUFFICIENT_EVIDENCE


def test_oos_backtest_error_is_its_own_reason_code():
    verdict = evaluate_candidate(
        CandidateEvidence(
            status=S.OOS_RUNNING,
            oos=OosEvidence(error="分折回测异常", fold_count=5),
        )
    )
    assert verdict.next_status is S.OOS_FAILED
    assert verdict.reason_code is ReasonCode.OOS_BACKTEST_FAILED


def test_oos_decay_cap_is_configurable():
    cfg = _candidate_live_config(oos_decay_max_pct=10.0)
    verdict = evaluate_candidate(
        CandidateEvidence(
            status=S.OOS_RUNNING,
            oos=OosEvidence(decay_pct=10.0, fold_count=5),
            thresholds=cfg,
        )
    )
    assert verdict.next_status is S.OOS_FAILED
    assert verdict.detail["oos_decay_max_pct"] == 10.0


# --------------------------------------------------------------------------- #
# L3 入口：等账户 / 排队
# --------------------------------------------------------------------------- #


def test_paper_pending_without_an_account_waits_for_the_human():
    """D10：整条流水线唯一的人工输入。缺它必须停下并说清楚缺什么。"""
    verdict = evaluate_candidate(CandidateEvidence(status=S.PAPER_PENDING))
    assert verdict.next_status is S.PAPER_PENDING
    assert verdict.reason_code is ReasonCode.PAPER_ACCOUNT_MISSING
    empty = evaluate_candidate(
        CandidateEvidence(status=S.PAPER_PENDING, paper=PaperEvidence())
    )
    assert empty.reason_code is ReasonCode.PAPER_ACCOUNT_MISSING


def test_paper_pending_queues_behind_an_active_session():
    """D9：同账户同品种串行，不放宽既有不变式——排队原因与缺账户必须分得开。"""
    verdict = evaluate_candidate(
        CandidateEvidence(
            status=S.PAPER_PENDING,
            paper=PaperEvidence(account_ready=True, queued_behind_active_session=True),
        )
    )
    assert verdict.next_status is S.PAPER_PENDING
    assert verdict.reason_code is ReasonCode.PAPER_QUEUED


def test_paper_pending_with_account_and_free_slot_starts_observing():
    verdict = evaluate_candidate(
        CandidateEvidence(
            status=S.PAPER_PENDING, paper=PaperEvidence(account_ready=True)
        )
    )
    assert verdict.next_status is S.PAPER_RUNNING
    assert verdict.reason_code is ReasonCode.PAPER_OBSERVATION_STARTED


# --------------------------------------------------------------------------- #
# L3 观察期
# --------------------------------------------------------------------------- #


def _paper(**overrides) -> PaperEvidence:
    base = dict(
        wall_clock_days=14.0,
        running_days=14.0,
        closed_trades=30,
        return_pct=1.0,
        risk_hits=0,
        account_ready=True,
    )
    base.update(overrides)
    return PaperEvidence(**base)


def test_paper_without_evidence_yet_holds():
    verdict = evaluate_candidate(CandidateEvidence(status=S.PAPER_RUNNING))
    assert verdict.next_status is S.PAPER_RUNNING
    assert verdict.reason_code is ReasonCode.PAPER_OBSERVING


def test_paper_observation_exactly_14_days_passes():
    """观察期恰 14 天算跑满（口径是「≥ 配置值」）。"""
    verdict = evaluate_candidate(
        CandidateEvidence(status=S.PAPER_RUNNING, paper=_paper())
    )
    assert verdict.next_status is S.AWAITING_LIVE_APPROVAL
    assert verdict.reason_code is ReasonCode.PAPER_PASSED


def test_paper_observation_one_minute_short_keeps_observing():
    verdict = evaluate_candidate(
        CandidateEvidence(
            status=S.PAPER_RUNNING,
            paper=_paper(wall_clock_days=13.99, running_days=13.99),
        )
    )
    assert verdict.next_status is S.PAPER_RUNNING
    assert verdict.reason_code is ReasonCode.PAPER_OBSERVING
    assert verdict.detail["paper_observation_days"] == 14


def test_paused_days_do_not_count_towards_the_observation_period():
    """D8：`paused` 是「暂停止损/止盈」，不是「在观察」——墙钟够、running 不够也不行。

    这是本层最重要的一条口径：只看墙钟的话，一条暂停了两周的会话会被算成「跑满两周」。
    """
    verdict = evaluate_candidate(
        CandidateEvidence(
            status=S.PAPER_RUNNING,
            paper=_paper(wall_clock_days=20.0, running_days=10.0),
        )
    )
    assert verdict.next_status is S.PAPER_RUNNING
    assert verdict.reason_code is ReasonCode.PAPER_OBSERVING
    assert verdict.detail["running_days"] == 10.0


def test_wall_clock_clause_is_required_too():
    """观察期的**墙钟**那一半也必须被单独钉住。

    这条用例的输入在现实中不可能出现（running 时长不会超过墙钟），它是**故意**的合成
    探针：只有「墙钟不够而 running 充足」才能把 `wall_clock_days >= required` 这半个
    条款与另一半分开判别。原先把两个数都写成不够（13.0 / 13.0），名字说着墙钟、数据
    却测不出它——独立复核用 import-time 变异删掉墙钟条款后全套 338 例仍然全绿，
    缺口正在这里。
    """
    verdict = evaluate_candidate(
        CandidateEvidence(
            status=S.PAPER_RUNNING,
            paper=_paper(wall_clock_days=13.0, running_days=99.0),
        )
    )
    assert verdict.next_status is S.PAPER_RUNNING
    assert verdict.reason_code is ReasonCode.PAPER_OBSERVING
    assert verdict.detail["wall_clock_days"] == 13.0


def test_paper_session_that_ended_early_fails_the_observation():
    verdict = evaluate_candidate(
        CandidateEvidence(
            status=S.PAPER_RUNNING,
            paper=_paper(wall_clock_days=3.0, running_days=3.0, session_ended=True),
        )
    )
    assert verdict.next_status is S.PAPER_FAILED
    assert verdict.reason_code is ReasonCode.PAPER_OBSERVATION_INCOMPLETE


def test_paper_observation_period_is_configurable():
    cfg = _candidate_live_config(paper_observation_days=7)
    verdict = evaluate_candidate(
        CandidateEvidence(
            status=S.PAPER_RUNNING,
            paper=_paper(wall_clock_days=7.0, running_days=7.0),
            thresholds=cfg,
        )
    )
    assert verdict.next_status is S.AWAITING_LIVE_APPROVAL


def test_paper_return_below_the_floor_fails():
    verdict = evaluate_candidate(
        CandidateEvidence(status=S.PAPER_RUNNING, paper=_paper(return_pct=-0.5))
    )
    assert verdict.next_status is S.PAPER_FAILED
    assert verdict.reason_code is ReasonCode.PAPER_RETURN_BELOW_MIN


def test_paper_return_exactly_at_the_floor_passes():
    """下限是 `>=`：「不亏」就算过，`0.0` 不算「低于门槛」。"""
    verdict = evaluate_candidate(
        CandidateEvidence(status=S.PAPER_RUNNING, paper=_paper(return_pct=0.0))
    )
    assert verdict.next_status is S.AWAITING_LIVE_APPROVAL


def test_paper_risk_hits_exactly_at_the_cap_pass():
    """「超标」= 超过上限，所以恰等于上限算通过（边界必须钉住比较方向）。"""
    verdict = evaluate_candidate(
        CandidateEvidence(status=S.PAPER_RUNNING, paper=_paper(risk_hits=0))
    )
    assert verdict.next_status is S.AWAITING_LIVE_APPROVAL

    cfg = _candidate_live_config(paper_max_risk_hits=2)
    at_cap = evaluate_candidate(
        CandidateEvidence(
            status=S.PAPER_RUNNING, paper=_paper(risk_hits=2), thresholds=cfg
        )
    )
    assert at_cap.next_status is S.AWAITING_LIVE_APPROVAL


def test_paper_risk_hits_above_the_cap_fail():
    verdict = evaluate_candidate(
        CandidateEvidence(status=S.PAPER_RUNNING, paper=_paper(risk_hits=1))
    )
    assert verdict.next_status is S.PAPER_FAILED
    assert verdict.reason_code is ReasonCode.PAPER_RISK_HITS_ABOVE_MAX

    cfg = _candidate_live_config(paper_max_risk_hits=2)
    above = evaluate_candidate(
        CandidateEvidence(
            status=S.PAPER_RUNNING, paper=_paper(risk_hits=3), thresholds=cfg
        )
    )
    assert above.next_status is S.PAPER_FAILED
    assert above.reason_code is ReasonCode.PAPER_RISK_HITS_ABOVE_MAX


@pytest.mark.parametrize("closed_trades", [29, 4, 0])
def test_paper_with_too_few_closed_trades_is_insufficient_evidence(closed_trades):
    """沿用既有 ≥30 笔门槛：观察期满但成交太少，胜率读不成结论（D6）。"""
    verdict = evaluate_candidate(
        CandidateEvidence(
            status=S.PAPER_RUNNING, paper=_paper(closed_trades=closed_trades)
        )
    )
    assert verdict.reason_code is ReasonCode.INSUFFICIENT_EVIDENCE
    assert verdict.next_status is S.PAPER_RUNNING
    assert verdict.detail["min_trades"] == 30


def test_paper_with_missing_stats_and_an_ended_session_fails_instead_of_hanging():
    """会话已结束又判不了 → 判未过。停在「观察中」等于永远不判定（静默卡死）。"""
    verdict = evaluate_candidate(
        CandidateEvidence(
            status=S.PAPER_RUNNING,
            paper=_paper(closed_trades=None, session_ended=True),
        )
    )
    assert verdict.next_status is S.PAPER_FAILED
    assert verdict.reason_code is ReasonCode.INSUFFICIENT_EVIDENCE


def test_paper_with_missing_observation_numbers_does_not_pass():
    verdict = evaluate_candidate(
        CandidateEvidence(
            status=S.PAPER_RUNNING,
            paper=_paper(wall_clock_days=None, running_days=None),
        )
    )
    assert verdict.next_status is S.PAPER_RUNNING
    assert verdict.reason_code is ReasonCode.INSUFFICIENT_EVIDENCE


# --------------------------------------------------------------------------- #
# 失败态与人工卡点
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "status", [S.IN_SAMPLE_FAILED, S.OOS_FAILED, S.PAPER_FAILED], ids=lambda s: s.value
)
def test_failed_states_wait_for_a_rerun_or_a_human(status):
    """重跑是一次外部动作（改参数/改分折配置），判定不该自己把人推进去。"""
    verdict = evaluate_candidate(CandidateEvidence(status=status))
    assert verdict.next_status is status
    assert verdict.reason_code is ReasonCode.AWAITING_RERUN


def test_awaiting_live_approval_stays_for_the_human():
    verdict = evaluate_candidate(CandidateEvidence(status=S.AWAITING_LIVE_APPROVAL))
    assert verdict.next_status is S.AWAITING_LIVE_APPROVAL
    assert verdict.reason_code is ReasonCode.AWAITING_HUMAN_APPROVAL


# --------------------------------------------------------------------------- #
# 非有限数（NaN / ±inf）不许 fail open
# --------------------------------------------------------------------------- #


NON_FINITE = [float("nan"), float("inf"), float("-inf")]


@pytest.mark.parametrize("value", NON_FINITE, ids=["nan", "+inf", "-inf"])
def test_non_finite_sharpe_is_insufficient_evidence_not_a_pass(value):
    """`NaN < 门槛` 恒假：不堵住的话，一个算崩了的夏普能静默通过样本内闸门。

    `-inf` 走的是同一支（比任何门槛都小 → 本来就会拦下），但它同样不是结论，原因码
    该说「证据不足」而不是「夏普不够」。
    """
    verdict = evaluate_candidate(
        CandidateEvidence(
            status=S.IN_SAMPLE_RUNNING,
            in_sample=InSampleEvidence(sharpe_ratio=value, total_trades=100),
        )
    )
    assert verdict.next_status is S.IN_SAMPLE_FAILED
    assert verdict.reason_code is ReasonCode.INSUFFICIENT_EVIDENCE
    assert verdict.reason_code is not ReasonCode.IN_SAMPLE_PASSED


@pytest.mark.parametrize("value", NON_FINITE, ids=["nan", "+inf", "-inf"])
def test_non_finite_decay_is_insufficient_evidence_not_a_pass(value):
    """`-inf >= 上限` 为假 → 不堵住的话，一个算崩了的衰减能静默通过样本外闸门。"""
    verdict = evaluate_candidate(
        CandidateEvidence(
            status=S.OOS_RUNNING, oos=OosEvidence(decay_pct=value, fold_count=5)
        )
    )
    assert verdict.next_status is S.OOS_FAILED
    assert verdict.reason_code is ReasonCode.INSUFFICIENT_EVIDENCE


@pytest.mark.parametrize("value", NON_FINITE, ids=["nan", "+inf", "-inf"])
def test_non_finite_paper_return_is_insufficient_evidence_not_a_pass(value):
    """`NaN < 0` 为假 → 不堵住的话，算崩了的 paper 收益能一路走到人工卡点。"""
    verdict = evaluate_candidate(
        CandidateEvidence(status=S.PAPER_RUNNING, paper=_paper(return_pct=value))
    )
    assert verdict.next_status is not S.AWAITING_LIVE_APPROVAL
    assert verdict.reason_code is ReasonCode.INSUFFICIENT_EVIDENCE


@pytest.mark.parametrize("field", ["wall_clock_days", "running_days"])
def test_non_finite_observation_lengths_are_insufficient_evidence(field):
    verdict = evaluate_candidate(
        CandidateEvidence(status=S.PAPER_RUNNING, paper=_paper(**{field: float("nan")}))
    )
    assert verdict.next_status is S.PAPER_RUNNING
    assert verdict.reason_code is ReasonCode.INSUFFICIENT_EVIDENCE


@pytest.mark.parametrize("closed_trades", [-1, 3.7, True])
def test_nonsense_trade_counts_are_insufficient_evidence(closed_trades):
    """计数必须是**非负整数**：`3.7` 是口径错了，`-1` 是不可能的计数，都不是结论。"""
    verdict = evaluate_candidate(
        CandidateEvidence(
            status=S.PAPER_RUNNING, paper=_paper(closed_trades=closed_trades)
        )
    )
    assert verdict.next_status is S.PAPER_RUNNING
    assert verdict.reason_code is ReasonCode.INSUFFICIENT_EVIDENCE


@pytest.mark.parametrize("total_trades", [-1, 3.7])
def test_nonsense_in_sample_trade_counts_are_insufficient_evidence(total_trades):
    verdict = evaluate_candidate(
        CandidateEvidence(
            status=S.IN_SAMPLE_RUNNING,
            in_sample=InSampleEvidence(sharpe_ratio=9.0, total_trades=total_trades),
        )
    )
    assert verdict.next_status is S.IN_SAMPLE_FAILED
    assert verdict.reason_code is ReasonCode.INSUFFICIENT_EVIDENCE


def test_nonsense_sharpe_values_are_insufficient_evidence():
    """守门本身也要有钉子：它坏掉的话上面那组 NaN 用例会一起静默失效。

    刻意用**公开入口**间接验它（测试决策：主要测外部行为；私有 import 只留给
    「没有公开入口的防御机制」，如 `_self_check`）：一个 `True`（`bool` 是 `int`
    子类）或字符串 `"1.5"` 被当成夏普时，必须落「证据不足」而不是参与比较。
    """
    for bad in (True, "1.5"):
        verdict = evaluate_candidate(
            CandidateEvidence(
                status=S.IN_SAMPLE_RUNNING,
                in_sample=InSampleEvidence(sharpe_ratio=bad, total_trades=100),
            )
        )
        assert verdict.next_status is S.IN_SAMPLE_FAILED, bad
        assert verdict.reason_code is ReasonCode.INSUFFICIENT_EVIDENCE, bad


# --------------------------------------------------------------------------- #
# 判定与转移表的一致性（对抗式）
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("status", list(S), ids=lambda s: s.value)
def test_every_verdict_is_either_a_hold_or_a_legal_edge(status):
    """判定只能产出「不动」或转移表里的边——多出来的边是最危险的一类 bug。"""
    aggressive = CandidateEvidence(
        status=status,
        in_sample=InSampleEvidence(sharpe_ratio=99.0, total_trades=999),
        oos=OosEvidence(decay_pct=0.0, fold_count=10),
        paper=_paper(closed_trades=999, return_pct=99.0, risk_hits=0),
    )
    for evidence in (
        CandidateEvidence(status=status),
        aggressive,
        CandidateEvidence(
            status=status,
            in_sample=InSampleEvidence(error="boom"),
            oos=OosEvidence(error="boom", data_sufficient=False),
            paper=_paper(return_pct=-99.0, session_ended=True),
        ),
        CandidateEvidence(
            status=status,
            in_sample=InSampleEvidence(sharpe_ratio=float("nan"), total_trades=100),
            oos=OosEvidence(decay_pct=float("nan"), fold_count=5),
            paper=_paper(return_pct=float("nan"), risk_hits=1),
        ),
    ):
        verdict = evaluate_candidate(evidence)
        if verdict.next_status is status:
            assert not can_transition(status, status), "自环不是边"
            assert verdict.holds(status)
        else:
            assert can_transition(status, verdict.next_status), (
                f"{status.value} → {verdict.next_status.value} "
                f"（{verdict.reason_code.value}）不在转移表里"
            )


def test_fully_passing_evidence_walks_the_whole_funnel():
    """一条顺利的候选从 registered 一路走到 awaiting_live_approval，四层全通。"""
    status = S.REGISTERED
    for expected in (
        S.IN_SAMPLE_RUNNING,
        S.OOS_RUNNING,
        S.PAPER_PENDING,
        S.PAPER_RUNNING,
        S.AWAITING_LIVE_APPROVAL,
    ):
        verdict = evaluate_candidate(
            CandidateEvidence(
                status=status,
                in_sample=InSampleEvidence(sharpe_ratio=2.0, total_trades=100),
                oos=OosEvidence(decay_pct=5.0, fold_count=5),
                paper=_paper(),
            )
        )
        assert verdict.next_status is expected, (
            status.value,
            verdict.reason_code.value,
        )
        status = verdict.next_status
