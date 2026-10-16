"""候选漏斗的推进任务（spec #19 D14 / D18；子票 #22）。

两个入口，一大一小：

* `advance_candidate_task(candidate_id)` —— 推**一条**候选一步。手动/API/agent 触发，
  也是批任务的单元。
* `advance_candidates_task(limit=None)` —— 扫一遍**正在推进中的**候选，逐个推一步。

## 三条纪律

1. **一条候选失败不拖垮整批**。批任务逐条 `try/except`，失败的候选记进返回值的
   `failed` 列表并继续下一条：一次接线错误（比如某个候选缺跑批判据）不该让其余候选
   集体停摆，而**哪几条失败了**必须能从返回值里读出来（失败可见，D20）。

2. **批扫描只碰「正在推进中」的状态**。`registered` / `in_sample_running` /
   `oos_running` / `paper_pending` / `paper_running` —— 其余状态（终态、各种 `*_failed`
   等待态、`registration_failed`、`awaiting_live_approval`）都要**人来动或别处来动**，
   每轮扫它们只会白读一遍库。

3. **总开关关掉时连库都不扫**。`auto_advance_enabled=False` 时直接返回
   `{"skipped": "auto_advance_disabled"}`：判定层确实也会给出同一条结论，但那时已经
   为每条候选付过一遍查库与判定；开关的语义是「发现漏斗在乱推时我能立刻停住它」，
   停住就该是停住。

**排期不在本票**（写范围只有 `apps/strategy_factory/`）：任务可用、可手动投递，把它挂
进 beat_schedule 是 API/契约那一张票（T8）的事。
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

from celery_app import app

from apps.strategy_factory.config import CONFIG
from apps.strategy_factory.orchestrator import AdvanceOutcome, advance
from apps.strategy_factory.status import StrategyCandidateStatus

logger = logging.getLogger(__name__)

#: 批扫描的状态集合：**正在自动推进中**的候选才需要被推。
ADVANCEABLE_STATUSES: tuple[str, ...] = (
    StrategyCandidateStatus.REGISTERED.value,
    StrategyCandidateStatus.IN_SAMPLE_RUNNING.value,
    StrategyCandidateStatus.OOS_RUNNING.value,
    StrategyCandidateStatus.PAPER_PENDING.value,
    # paper_running 的判定（观察期满 / 盈亏 / 风控命中）归 #24；先留在扫描集合里，
    # 那一层落地时不必回来改这里。
    StrategyCandidateStatus.PAPER_RUNNING.value,
)


def _outcome_payload(outcome: AdvanceOutcome) -> dict[str, Any]:
    return {
        "candidate_id": outcome.candidate_id,
        "layer": outcome.layer.value,
        "from_status": outcome.from_status.value,
        "to_status": outcome.to_status.value,
        "reason_code": outcome.reason_code.value,
        "changed": outcome.changed,
        "started_run": outcome.started_run,
        "created_paper_session": outcome.created_paper_session,
        "paper_session_id": outcome.paper_session_id,
        "recorded": outcome.recorded,
        "notified": outcome.notify_message is not None,
    }


@app.task(bind=True, max_retries=0, track_started=True)
def advance_candidate_task(self, candidate_id: str) -> dict[str, Any]:
    """推一条候选一步；接线错误照实抛出（任务失败 = 可见）。"""
    from apps.strategy_factory.models import StrategyCandidate

    candidate = StrategyCandidate.objects.filter(pk=candidate_id).first()
    if candidate is None:
        return {"ok": False, "candidate_id": str(candidate_id), "error": "候选不存在"}
    outcome = advance(candidate)
    payload = _outcome_payload(outcome)
    logger.info(
        "[StrategyFactory] advance %s: %s → %s (%s)",
        payload["candidate_id"],
        payload["from_status"],
        payload["to_status"],
        payload["reason_code"],
    )
    return {"ok": True, **payload}


@app.task(bind=True, max_retries=0, track_started=True)
def advance_candidates_task(self, limit: int | None = None) -> dict[str, Any]:
    """扫一遍正在推进中的候选，各推一步。"""
    from apps.strategy_factory.models import StrategyCandidate

    if not CONFIG.auto_advance_enabled:
        return {"skipped": "auto_advance_disabled", "advanced": [], "failed": []}

    queryset = StrategyCandidate.objects.filter(
        status__in=ADVANCEABLE_STATUSES
    ).order_by("created_at")
    if limit:
        queryset = queryset[: int(limit)]

    advanced: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for candidate in queryset:
        try:
            payload = _outcome_payload(advance(candidate))
            advanced.append(payload)
        except Exception as exc:  # noqa: BLE001 — 一条失败不拖垮整批，但必须可见
            logger.warning(
                "[StrategyFactory] advance 候选 %s 失败", candidate.pk, exc_info=True
            )
            failed.append(
                {
                    "candidate_id": str(candidate.pk),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
    return {
        "candidates": len(advanced) + len(failed),
        "advanced": advanced,
        "failed": failed,
    }


def advanceable_ids(limit: int | None = None) -> Iterable[str]:
    """批扫描会碰的那些候选 id（给运维/测试读一眼「下一轮会推谁」）。"""
    from apps.strategy_factory.models import StrategyCandidate

    queryset = StrategyCandidate.objects.filter(
        status__in=ADVANCEABLE_STATUSES
    ).order_by("created_at")
    if limit:
        queryset = queryset[: int(limit)]
    return [str(pk) for pk in queryset.values_list("pk", flat=True)]
