"""推进任务的用例（子票 #22）。

钉住三条：

1. **一条候选失败不拖垮整批**：失败的候选进 `failed` 列表，其余照常推进、结果可读。
2. **总开关关掉时连库都不扫**。
3. **状态集合就是「正在自动推进中」的那几个**：终态与各种等待态不在里面（它们要人来动）。

任务自己不重测漏斗逻辑（那是 `test_advance.py` 的事），只测「怎么把 `advance` 组织起来」。
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from apps.strategy_factory import tasks
from apps.strategy_factory.config import CONFIG
from apps.strategy_factory.models import StrategyCandidate, StrategyCandidateTransition
from apps.strategy_factory.status import (
    StrategyCandidateOrigin,
    StrategyCandidateStatus,
)
from apps.strategy_factory.tasks import (
    ADVANCEABLE_STATUSES,
    advance_candidate_task,
    advance_candidates_task,
    advanceable_ids,
)
from apps.trading.models import Strategy

pytestmark = pytest.mark.django_db


def _strategy(name: str) -> Strategy:
    return Strategy.objects.create(name=name, code_path=f"strategies/{name}.py")


def _candidate(
    strategy: Strategy,
    status: StrategyCandidateStatus = StrategyCandidateStatus.REGISTERED,
) -> StrategyCandidate:
    return StrategyCandidate.objects.create(
        name=strategy.name,
        hypothesis="探针",
        code_path=f"~/.tradelogx/strategies/{strategy.name}.py",
        origin=StrategyCandidateOrigin.AGENT.value,
        source_ref={},
        strategy=strategy,
        status=status.value,
    )


class TestAdvanceCandidateTask:
    def test_missing_candidate_is_reported_not_raised(self):
        result = advance_candidate_task("00000000-0000-0000-0000-000000000000")
        assert result["ok"] is False
        assert "不存在" in result["error"]

    def test_registered_candidate_moves_one_step(self):
        strategy = _strategy("task_one_step")
        candidate = _candidate(strategy)

        result = advance_candidate_task(str(candidate.pk))

        assert result["ok"] is True
        assert result["from_status"] == "registered"
        assert result["to_status"] == "in_sample_running"
        assert result["reason_code"] == "in_sample_run_started"
        assert result["changed"] is True
        assert result["recorded"] is True
        candidate.refresh_from_db()
        assert candidate.status == "in_sample_running"
        assert (
            StrategyCandidateTransition.objects.filter(candidate=candidate).count() == 1
        )

    def test_wiring_error_propagates(self):
        """缺跑批判据是接线错误：任务照实失败（可见），不落成一次判定。"""
        from apps.strategy_factory.orchestrator import MissingRunContextError

        strategy = _strategy("task_no_context")
        candidate = _candidate(strategy, StrategyCandidateStatus.IN_SAMPLE_RUNNING)

        with pytest.raises(MissingRunContextError):
            advance_candidate_task(str(candidate.pk))

        candidate.refresh_from_db()
        assert candidate.status == "in_sample_running"


class TestAdvanceCandidatesTask:
    def test_disabled_switch_skips_scanning(self, monkeypatch):
        monkeypatch.setattr(
            tasks, "CONFIG", replace(CONFIG, auto_advance_enabled=False)
        )
        strategy = _strategy("task_switch_off")
        candidate = _candidate(strategy)

        result = advance_candidates_task()

        assert result["skipped"] == "auto_advance_disabled"
        candidate.refresh_from_db()
        assert candidate.status == "registered", "总开关关掉时状态不得变化"

    def test_advances_every_ready_candidate(self):
        first = _candidate(_strategy("task_batch_a"))
        second = _candidate(_strategy("task_batch_b"))

        result = advance_candidates_task()

        assert result["candidates"] == 2
        assert result["failed"] == []
        assert {row["candidate_id"] for row in result["advanced"]} == {
            str(first.pk),
            str(second.pk),
        }

    def test_one_failure_does_not_stop_the_batch(self, monkeypatch):
        """一条候选缺跑批判据 → 记进 `failed`，其余照常推进。"""
        broken = _candidate(
            _strategy("task_broken"), StrategyCandidateStatus.IN_SAMPLE_RUNNING
        )
        healthy = _candidate(_strategy("task_healthy"))

        result = advance_candidates_task()

        assert [row["candidate_id"] for row in result["failed"]] == [str(broken.pk)]
        assert "MissingRunContextError" in result["failed"][0]["error"]
        assert [row["candidate_id"] for row in result["advanced"]] == [str(healthy.pk)]
        broken.refresh_from_db()
        healthy.refresh_from_db()
        assert broken.status == "in_sample_running", "失败的那条不动"
        assert healthy.status == "in_sample_running", "其余的照常前进"

    def test_limit_caps_the_scan(self):
        for index in range(3):
            _candidate(_strategy(f"task_limit_{index}"))

        result = advance_candidates_task(limit=2)

        assert result["candidates"] == 2

    def test_waiting_and_terminal_states_are_not_scanned(self):
        """要人来动 / 已结束的候选不进扫描集合（白读一遍库没有意义）。"""
        skip = [
            StrategyCandidateStatus.REGISTRATION_FAILED,
            StrategyCandidateStatus.IN_SAMPLE_FAILED,
            StrategyCandidateStatus.OOS_FAILED,
            StrategyCandidateStatus.PAPER_FAILED,
            StrategyCandidateStatus.AWAITING_LIVE_APPROVAL,
            StrategyCandidateStatus.LIVE,
            StrategyCandidateStatus.REJECTED,
            StrategyCandidateStatus.CANCELLED,
        ]
        for index, status in enumerate(skip):
            _candidate(_strategy(f"task_skip_{index}"), status)
        ready = _candidate(_strategy("task_ready"))

        assert advanceable_ids() == [str(ready.pk)]
        assert set(ADVANCEABLE_STATUSES) == {
            "registered",
            "in_sample_running",
            "oos_running",
            "paper_pending",
            "paper_running",
        }

    def test_paper_running_candidate_is_scanned_but_holds(self):
        """`paper_running` 的判定归 #24；现在扫到它只会停在观察中（不落噪音记录）。"""
        strategy = _strategy("task_observing")
        candidate = _candidate(strategy, StrategyCandidateStatus.PAPER_RUNNING)

        result = advance_candidates_task()

        assert result["failed"] == []
        assert result["advanced"][0]["reason_code"] == "paper_observing"
        assert result["advanced"][0]["changed"] is False
        candidate.refresh_from_db()
        assert candidate.status == "paper_running"


def test_every_scanned_status_has_a_verdict():
    """扫描集合里的每个状态，判定层都能给出一个结论（不会在任务里抛未知状态）。"""
    from apps.strategy_factory.evaluate import CandidateEvidence, evaluate_candidate

    for value in ADVANCEABLE_STATUSES:
        verdict = evaluate_candidate(
            CandidateEvidence(status=StrategyCandidateStatus(value))
        )
        assert verdict.reason_code.value
