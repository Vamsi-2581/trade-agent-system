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
    RegimeEvidence,
    RegimePoolCellEvidence,
    Verdict,
    evaluate_candidate,
    regime_verdict,
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
# 原因码词表：加一个成员就必须有展示名（spec #30 D1，票 #32）
#
# `_REASON_DISPLAY` 是**裸字典下标**（`display` 属性就是 `_REASON_DISPLAY[self]`），
# 仓库刻意不用 `.get(默认值)`——「加了一个原因码却忘了给它展示名」必须在 KeyError 上
# 立刻响，而不是在通知文案里显示一个 ascii slug。这一节把那条纪律钉成一条用例。
# --------------------------------------------------------------------------- #

REGIME_REASON_CODES = (
    ReasonCode.REGIME_UNFIT,
    ReasonCode.REGIME_NO_EVIDENCE,
    ReasonCode.REGIME_EXEMPT,
)


@pytest.mark.parametrize("code", REGIME_REASON_CODES, ids=lambda c: c.value)
def test_every_regime_reason_code_has_a_display_name(code):
    """三个新码都要有非空展示名，且必须走裸下标（缺键会 KeyError，不静默回退）。"""
    assert isinstance(code.display, str)
    assert code.display.strip(), f"{code.value} 的展示名是空串"
    # 展示名是中文、落库值是 ascii：两个方向都要对，否则「ascii 落库」这条纪律就破了。
    assert code.value.isascii()
    assert not code.display.isascii()


def test_the_reason_display_table_covers_the_whole_enum():
    """词表与枚举互为穷举——多一个成员、少一个条目都会在这里红。

    不写成「挑三个已知码」：那只能证明这三个码有名字，而漏掉第四个正是本用例要拦的事。
    """
    from apps.strategy_factory.evaluate import _REASON_DISPLAY

    assert set(_REASON_DISPLAY) == set(ReasonCode)
    assert len(_REASON_DISPLAY) == len(list(ReasonCode))


def test_regime_exempt_reads_as_a_pass_through_in_its_display_name():
    """豁免码的展示名必须说清「放行 + 留痕」，不能读起来像被拦下了（spec #30 D5）。

    它是**纯留痕**码：记的是「在期的人工豁免把这条放过去了」，不是「行情不适用」。
    展示名是唯一能把这两件事对读记录的人讲清的地方，所以单独钉一条。
    """
    assert "豁免" in ReasonCode.REGIME_EXEMPT.display
    assert "放行" in ReasonCode.REGIME_EXEMPT.display


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


# --------------------------------------------------------------------------- #
# 行情闸门：`regime_verdict`（spec #30 D1 / D2 / D4 / D5，子票 #37）
#
# 表驱动、零 DB、零时钟。四个入参全是纯值，所以每一条边界都能直接从一个具体取值
# 断言到「拦不拦 + 原因码 + 依据」。
#
# 这一节钉的口径（spec D1 原文的检查顺序，一处不许多、一处不许少）：
#
# 1. `blanket` → `None`（不发表意见）。保命档有自己的停机链，把它当 `unfit` 会让候选
#    在保命档期间一个都出不去。
# 2. `fit` → `None`（不发表意见）。**适用阶段时既有判定行为零变化**正是这一支。
# 3. 在期豁免（策略 × 当前阶段命中）→ 放行 + `regime_exempt` 留痕（D5）。
# 4. `unfit` → `regime_hold` + `regime_unfit`。
# 5. `neutral` / `unknown` / 没有格子 / 没有当前阶段 → `regime_hold` +
#    `regime_no_evidence`（fail-closed：「没证据」与「适用」不是同一件事）。
# --------------------------------------------------------------------------- #

#: 表驱动：格子结论 → （期望的 next_status 或 None，期望的原因码或 None）。
#: `None / None` = 闸门不发表意见（`regime_verdict` 返回 `None`）。
CELL_STATE_TABLE = [
    # (格子结论, 期望 next_status, 期望原因码)
    (None, S.REGIME_HOLD, ReasonCode.REGIME_NO_EVIDENCE),
    ("fit", None, None),
    ("blanket", None, None),
    ("neutral", S.REGIME_HOLD, ReasonCode.REGIME_NO_EVIDENCE),
    ("unknown", S.REGIME_HOLD, ReasonCode.REGIME_NO_EVIDENCE),
    ("unfit", S.REGIME_HOLD, ReasonCode.REGIME_UNFIT),
]


@pytest.mark.parametrize(
    ("state", "expected_status", "expected_reason"),
    CELL_STATE_TABLE,
    ids=[str(state) if state is None else state for state, _, _ in CELL_STATE_TABLE],
)
def test_regime_verdict_covers_every_cell_state(
    state, expected_status, expected_reason
):
    """格子结论的每一种取值都有唯一的落点（D1 的五枝，一枝不多一枝不少）。

    注意 `blanket` 与 `fit` **都**返回 `None`，但理由完全不同：`fit` 是「适用」，
    `blanket` 是「保命档，不发表意见」——两者在通知与依据栏里读起来不一样，所以
    上面各有一条独立用例钉住（见 `test_blanket_is_carried_...`）。
    """
    verdict = regime_verdict(
        "strat-1", "range", RegimePoolCellEvidence(state=state), {}
    )
    if expected_status is None:
        assert verdict is None, f"{state} 该不发表意见，实际 {verdict}"
    else:
        assert verdict is not None, f"{state} 该被拦下，实际返回了 None"
        assert verdict.next_status is expected_status
        assert verdict.reason_code is expected_reason


def test_fit_cell_is_a_genuine_no_opinion_not_a_pass():
    """`fit` 返回 `None` 而不是一份合成的「放行」结论。

    合成一份（`next_status=paper_pending, reason_code=regime_fit`）的后果：通知里出现
    一个来历不明的 reason code，而审计面再也分不清「这一步是谁放的行」——是回测证据、
    还是闸门、还是豁免。`None` 让调用方照**原有**的那条路径走，原因码仍是
    `oos_passed` / `paper_passed`。
    """
    assert regime_verdict("s", "range", RegimePoolCellEvidence(state="fit"), {}) is None
    assert not hasattr(ReasonCode, "REGIME_FIT"), (
        "刻意没有 regime_fit：豁免与适用是两件事，合并之后「靠人放过去的」统计不出来"
    )


def test_blanket_lifts_itself_so_the_gate_must_not_park_on_it():
    """`blanket` 必须不发表意见——**保命档会自己解除，而这个闸门不会**。

    这是本节最贵的一条：把 `blanket` 当 `unfit` 会让候选在保命档期间一个都出不去
    （闸门这一侧不会因为行情恢复而翻回来，等格子回 `fit` 才行，而保命档一解除格子
    就是 `blanket` 而不是 `fit`——两条路径互不相遇，候选就地长死）。
    """
    verdict = regime_verdict(
        "s", "high_vol", RegimePoolCellEvidence(state="blanket"), {}
    )
    assert verdict is None
    # 与 `unfit` 的对照：同一个 `high_vol`，不同的格子结论，结论必须相反。
    assert (
        regime_verdict("s", "high_vol", RegimePoolCellEvidence(state="unfit"), {})
        is not None
    )


def test_hold_is_never_a_failure():
    """两个 Hold 原因码都不许落任何 `*_failed`（用户故事 3 的第一句）。

    「拦」是「现在这片行情不宜打钱」，不是「这条策略不行」。所以
    `next_status` 只能是 `regime_hold`，而 `regime_hold` 在转移表里没有任何通向
    `*_failed` 的边（这一条在 `test_status.py` 里另有钉子，这里从判定的角度再看一次）。
    """
    for cell in (
        RegimePoolCellEvidence(state="unfit"),
        RegimePoolCellEvidence(state="neutral"),
        RegimePoolCellEvidence(state="unknown"),
        None,
    ):
        verdict = regime_verdict("s", "range", cell, {})
        assert verdict is not None
        assert verdict.next_status is S.REGIME_HOLD
        assert verdict.next_status not in (
            S.OOS_FAILED,
            S.PAPER_FAILED,
            S.IN_SAMPLE_FAILED,
        )
        # 它也不是调用方的原状态：否则调用方会以为「什么都没发生」。
        assert verdict.next_status not in (S.OOS_RUNNING, S.PAPER_RUNNING)


@pytest.mark.parametrize("regime_state", [None, "range", "high_vol"], ids=str)
def test_no_current_generation_holds_and_never_releases(regime_state):
    """冷启动两段（没有格子 / 没有当前阶段）都 Hold，都**不**放行。

    刻意把 `regime_state=None` 也并进表里：`RegimeEvidence.cold` 与 `covered` 是
    #34 的两个独立属性，而它们在判定侧的落点是**同一个** fail-closed 结论。写成
    两个分支只会让「加了第三种『拿不到』」时漏改一处。
    """
    verdict = regime_verdict("s", regime_state, None, {})
    assert verdict is not None
    assert verdict.next_status is S.REGIME_HOLD
    assert verdict.reason_code is ReasonCode.REGIME_NO_EVIDENCE
    assert verdict.detail["regime"] == regime_state
    assert verdict.detail["regime_hold_reason"] == "no_usable_conclusion"
    # 依据栏要说清是「判不了」而不是「判了不行」——两个展示名分得开才有得讲。
    assert ReasonCode.REGIME_NO_EVIDENCE.display != ReasonCode.REGIME_UNFIT.display


def test_regime_verdict_is_pure_and_needs_no_clock_or_db():
    """四个纯参数进、一个纯结论出：**没有时钟、没有 I/O**。

    `exemptions` 是唯一一个可能"看起来需要时钟"的东西——「在期」由采集侧按
    `deps.now()` 判完，判定层只看见「已经在期」。所以这里用一个字面量映射就能跑完
    豁免那一支，这正是 D14 测试缝要的形状。
    """
    verdict = regime_verdict(
        "strat-uuid",
        "downtrend",
        RegimePoolCellEvidence(state="unfit"),
        {("strat-uuid", "downtrend"): "exemption-id"},
        resume=S.PAPER_PENDING,
    )
    assert verdict.reason_code is ReasonCode.REGIME_EXEMPT
    # detail 必须是 JSON-safe（它要进 `evidence` JSONField 与转移记录）。
    assert json.loads(json.dumps(verdict.detail)) == verdict.detail


@pytest.mark.parametrize(
    "bad_exemption_key",
    [
        ("other-strategy", "downtrend"),  # 同阶段、别的策略
        ("strat-1", "range"),  # 同策略、别的阶段
        ("other-strategy", "range"),  # 两个都不对
    ],
)
def test_exemptions_only_match_strategy_and_current_regime(bad_exemption_key):
    """豁免的键是「策略 × 阶段」，两边都要对上（D5）。

    少任一半的后果是**别的策略的豁免把这一条放过去了**——一次人工动作越过了它的
    适用范围，而这种错在库上完全看不出来（依据栏里只写「豁免在期」）。
    """
    verdict = regime_verdict(
        "strat-1",
        "downtrend",
        RegimePoolCellEvidence(state="unfit"),
        {bad_exemption_key: 1},
    )
    assert verdict is not None
    assert verdict.next_status is S.REGIME_HOLD
    assert verdict.reason_code is ReasonCode.REGIME_UNFIT


def test_exemption_beats_an_unfit_cell():
    """有人看过这一格并决定放行 → 照办，哪怕格子本身是 `unfit`（D5）。

    次序是这里唯一的东西：豁免**排在格子之前**判。反过来（先判格子）会让一个明确的
    人工决定输给机制算出来的结论，而豁免这个动作在结构上就是用来覆盖它的。
    """
    verdict = regime_verdict(
        "strat-1",
        "downtrend",
        RegimePoolCellEvidence(state="unfit", reason="direction_conflict"),
        {("strat-1", "downtrend"): 7},
        resume=S.PAPER_PENDING,
    )
    assert verdict is not None
    assert verdict.next_status is S.PAPER_PENDING
    assert verdict.reason_code is ReasonCode.REGIME_EXEMPT
    # 格子照抄：豁免覆盖的是**结论**，不是事实。依据栏里两个数都要能读到。
    assert verdict.detail["cell_state"] == "unfit"
    assert verdict.detail["cell_reason"] == "direction_conflict"
    assert verdict.detail["exempt"] == [
        {"strategy_id": "strat-1", "regime": "downtrend"}
    ]


def test_exempt_trace_carries_only_the_matching_pair():
    """留痕只点命中的那一对，不把整个在期集合抄一遍。

    集合里可能有别的策略在别的阶段上的豁免。全抄会让读记录的人以为这一条
    「被好几个豁免覆盖」，而事实是只有一个命中。
    """
    exemptions = {
        ("strat-1", "downtrend"): 1,
        ("strat-2", "downtrend"): 2,
        ("strat-1", "range"): 3,
    }
    verdict = regime_verdict(
        "strat-1",
        "downtrend",
        RegimePoolCellEvidence(state="unfit"),
        exemptions,
        resume=S.AWAITING_LIVE_APPROVAL,
    )
    assert verdict.detail["exempt"] == [
        {"strategy_id": "strat-1", "regime": "downtrend"}
    ]
    assert verdict.next_status is S.AWAITING_LIVE_APPROVAL


def test_exempt_is_a_pure_trace_not_a_hold():
    """豁免放行不进 `regime_hold`（D5 / spec 用户故事 7）。

    这一条同时钉住「`resume` 由调用方给」：闸门不猜「我在等哪一层」，调用方传什么
    它就用什么，而调用方正是那个知道来处的人。
    """
    for resume in (S.PAPER_PENDING, S.AWAITING_LIVE_APPROVAL):
        verdict = regime_verdict(
            "strat-1",
            "downtrend",
            RegimePoolCellEvidence(state="unfit"),
            {("strat-1", "downtrend"): 1},
            resume=resume,
        )
        assert verdict.next_status is resume, (
            "放行目标必须照调用方给的来，闸门自己不许挑"
        )
        assert verdict.next_status is not S.REGIME_HOLD


def test_candidate_without_a_strategy_row_is_not_an_error():
    """候选没绑策略行（登记失败，FK 为空）→ 没有任何东西可豁免，照常判。

    刻意不 Fail Loud：那不是一次接线错误，是这条候选**登记就没成功**。把它抛出去
    会让批扫描把它记进 `failed` 列表，而真实情况是它该照常走完行情判定。
    """
    # 豁免集合里有别人的条目：没有 `strategy_id` 就撞不上，也不许抛。
    verdict = regime_verdict(
        None, "downtrend", RegimePoolCellEvidence(state="fit"), {("x", "downtrend"): 1}
    )
    assert verdict is None
    for cell in (RegimePoolCellEvidence(state="unfit"), None):
        assert (
            regime_verdict(None, "downtrend", cell, {("x", "downtrend"): 1}) is not None
        )


def test_neutral_and_unknown_are_not_reasons_to_halt_on_their_own():
    """`neutral` 与 `unknown` 在 `apps.regime` 里**不是停用理由**——但在本闸门里是。

    这条差异是刻意的，也是本节最容易被"改对"的一处：池化表那侧 `neutral` 只表示
    「证据不足 / 样本噪声，不构成停用建议」，因为那一侧问的是「要不要把策略停掉」；
    本闸门问的是「现在能不能往上升一档」，而**证据不足必须读成不放行**（fail-closed）。
    于是同一个 `neutral` 在两侧有不同处置，而这个不同正是 spec Further Notes 点名
    要的：「没证据」与「适用」不是同一件事。
    """
    for state in ("neutral", "unknown"):
        verdict = regime_verdict("s", "range", RegimePoolCellEvidence(state=state), {})
        assert verdict is not None, state
        assert verdict.next_status is S.REGIME_HOLD
        assert verdict.reason_code is ReasonCode.REGIME_NO_EVIDENCE
        # 但原因码必须说清「判不了」——不是「判了不行」。
        assert verdict.detail["cell_state"] == state


def test_regime_evidence_cold_and_covered_are_two_different_no_opinions():
    """`RegimeEvidence` 的两个「说不清」属性在判定侧的落点相同，但来源不同。

    单独一条 AST/形状钉子：`evaluate` 不 import Django，而 `RegimeEvidence` 是纯
    值结构——所以判定层能直接构造它来喂 `regime_verdict`。这一条钉的是那个结构
    **现在真的存在**（#34 的交付），而不是让测试自己去造一个 duck-typed 替身。
    """
    cold = RegimeEvidence(regime=None, cell=None)
    uncovered = RegimeEvidence(regime="range", cell=None)
    for evidence in (cold, uncovered):
        verdict = regime_verdict(
            "s", evidence.regime, evidence.cell, dict(evidence.exemptions)
        )
        assert verdict is not None, evidence
        assert verdict.next_status is S.REGIME_HOLD
        assert verdict.reason_code is ReasonCode.REGIME_NO_EVIDENCE


def test_regime_verdict_never_invents_a_state_outside_the_transition_table():
    """闸门只能产出转移表里的边（含 `regime_hold` 这一条入边）。

    与上面那条 `test_every_verdict_is_either_a_hold_or_a_legal_edge` 同一条纪律，
    这里是「行情结论」的版本：**每一种格子结论 × 有无豁免 × 有无阶段**的组合都试过。

    两个非 None 的分支落点不同，但它们都必须是**合法边**：

    * 豁免放行（`regime_exempt`）去的是被豁免那一步的正常升档目标，**不是** `regime_hold`；
    * 另外两个码（`regime_unfit` / `regime_no_evidence`）一律停 `regime_hold`。

    写成「凡非 None 必是 regime_hold」会把「豁免放行」读成「拦人」，而那正好把用户故事 7
    （在期豁免不再拦它）反过来——测试会把正确实现判红。

    豁免放行的目标**由调用方给**（`resume=`），所以它的合法边要按调用点分别断言：
    从 `oos_running` 进来时目标是 `paper_pending`，从 `paper_running` 进来时是
    `awaiting_live_approval`。两个 Hold 结论则与调用点无关，都停 `regime_hold`。
    """
    for state in ("fit", "unfit", "neutral", "unknown", "blanket", None):
        for regime in ("range", None, "high_vol"):
            for exemptions in ({}, {("s", "range"): 1}):
                for source, target in (
                    (S.OOS_RUNNING, S.PAPER_PENDING),
                    (S.PAPER_RUNNING, S.AWAITING_LIVE_APPROVAL),
                ):
                    verdict = regime_verdict(
                        "s",
                        regime,
                        None if state is None else RegimePoolCellEvidence(state=state),
                        exemptions,
                        target,
                    )
                    if verdict is None:
                        continue
                    assert verdict.reason_code in (
                        ReasonCode.REGIME_UNFIT,
                        ReasonCode.REGIME_NO_EVIDENCE,
                        ReasonCode.REGIME_EXEMPT,
                    ), (state, regime, verdict.reason_code)

                    if verdict.reason_code is ReasonCode.REGIME_EXEMPT:
                        assert verdict.next_status is target, (state, regime, source)
                    else:
                        assert verdict.next_status is S.REGIME_HOLD, (state, regime)

                    # 无论哪一支，结论都必须是一条从**进来那一层**出发的合法边。
                    assert can_transition(source, verdict.next_status), (
                        state,
                        regime,
                        source,
                        verdict.next_status,
                    )
