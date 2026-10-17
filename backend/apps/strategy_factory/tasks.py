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
   `oos_running` / `paper_pending` / `paper_running` / `regime_hold` —— 其余状态
   （终态、各种 `*_failed` 等待态、`registration_failed`、`awaiting_live_approval`）
   都要**人来动或别处来动**，每轮扫它们只会白读一遍库。

   `regime_hold` 在集合里要特别注意（#37）：它自己不会好（得等池化表重算、或当前生效
   阶段变化），而这些变化**不通知漏斗**——所以扫描必须每轮回来问一次「翻回来了吗」。
   它的推进是幂等的：行情没变就停在原地，`_is_repeat` 把同一条结论的重复记录与通知
   吃掉，所以不会每 5 分钟刷一行。

3. **总开关关掉时连库都不扫**。`auto_advance_enabled=False` 时直接返回
   `{"skipped": "auto_advance_disabled"}`：判定层确实也会给出同一条结论，但那时已经
   为每条候选付过一遍查库与判定；开关的语义是「发现漏斗在乱推时我能立刻停住它」，
   停住就该是停住。

**排期不在本票**（写范围只有 `apps/strategy_factory/`）：任务可用、可手动投递，把它挂
进 beat_schedule 是 API/契约那一张票（T8）的事。

## 资金效率任务（spec #30 D7 / D8；子票 #35）

`compute_capital_efficiency_task(result_ids)` 与 `dispatch_capital_efficiency` 是一对，
形状照抄 `apps/regime/tasks.py` 的 `compute_regime_slice_task` / `dispatch_slice`。
**为什么照抄形状而不抽一个共用投递器**：那两条投的是两个不同的任务，共用一层会把
「谁投的、投丢了什么」压成一个数；而它们的调用点恰好并排（`apps/backtest/tasks.py` 的
两次 `dispatch_*`），读的人需要一眼看出它们是两条独立的附加产物。

三条纪律与切片那两条任务同源：

1. **投递绝不抛**（`dispatch_capital_efficiency`）。调用点是回测任务的收尾，而回测结果
   **已经落库了**——把一次成功的回测标成 failed，用户会以为白跑了。投递失败的代价
   （这次没算）由两条路兜底：`metrics` 里干脆没有 `capital_efficiency` 键这件事本身
   可查，以及 `recompute_regime_slices` 的全量重算。
2. **任务以「一批 id」为形状**。批本身就是那个计数，重投整批是最省事的重试策略，不必
   区分「哪几条没成」，也不会因为「第一次死在半路」而漏掉尾部。
3. **幂等**：同样的行 → 同样的载荷，收敛靠「已经有一份不陈旧的载荷就跳过」，而不是靠
   「只投一次」——后者在投递失败与 Celery 重试这两条路径上根本表达不出来。
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

from celery_app import app

from apps.strategy_factory.config import CONFIG
from apps.strategy_factory.orchestrator import AdvanceOutcome, advance
from apps.strategy_factory.status import StrategyCandidateStatus

logger = logging.getLogger(__name__)

#: 重试次数与首次退避秒数。与 `apps/regime/tasks.py` 取同一组数：这两个任务在同一个
#: 回测收尾里被投递，重试节奏不该因为「谁先被写出来」而不同。
MAX_CAPITAL_EFFICIENCY_RETRIES = 3
RETRY_BACKOFF_SECONDS = 60

#: 批扫描的状态集合：**正在自动推进中**的候选才需要被推。
ADVANCEABLE_STATUSES: tuple[str, ...] = (
    StrategyCandidateStatus.REGISTERED.value,
    StrategyCandidateStatus.IN_SAMPLE_RUNNING.value,
    StrategyCandidateStatus.OOS_RUNNING.value,
    StrategyCandidateStatus.PAPER_PENDING.value,
    # paper_running 的判定（观察期满 / 盈亏 / 风控命中）归 #24；先留在扫描集合里，
    # 那一层落地时不必回来改这里。
    StrategyCandidateStatus.PAPER_RUNNING.value,
    #: 被行情闸门拦下的候选（#37）。**必须在这里**，否则它永远不会被再扫到：
    #: `advance` 是唯一会问「行情翻回来了吗」的地方，而批扫描是它唯一的自动驱动。
    #: 漏掉这一行的后果是候选安静地停在 `regime_hold`——转移表给了它回边、判定层
    #: 也会放行，但没有任何东西去推那一下。用户故事 4 整条折在这一行上。
    StrategyCandidateStatus.REGIME_HOLD.value,
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


# --------------------------------------------------------------------------- #
# 资金效率（spec #30 D7 / D8；子票 #35）
# --------------------------------------------------------------------------- #


def _as_id_list(result_ids) -> list[str]:
    """把「一个 id / 一串 id / 一个 queryset」统一成一串非空字符串。

    与 `apps/regime/tasks.py` 的同名函数逐字一致，包括那个 `isinstance(x, (str, bytes))`
    分支：一个裸字符串是**可迭代**的，`list("abc")` 会变成 `["a", "b", "c"]`——三个不
    存在的 id，任务照跑、three 次 `missing`，而投递方一切正常。这个 bug 只在有人手工
    调 `dispatch_capital_efficiency("某个-uuid")` 时才出现，而那时它看起来像「结果被删了」。
    """
    if not result_ids:
        return []
    if isinstance(result_ids, (str, bytes)):
        candidates = [result_ids]
    else:
        candidates = list(result_ids)
    return [str(rid) for rid in candidates if rid]


def dispatch_capital_efficiency(result_ids) -> bool:
    """投递一批资金效率计算。**绝不抛异常。**

    调用点是两个回测任务的收尾（`apps/backtest/tasks.py`），紧挨着 `dispatch_slice`。
    一次成功的回测不该因为一个附加产物投不出去而被标成 failed——回测结果已经落库了，
    把它标成 failed 会让用户以为白跑了。

    投递失败的代价（这次没算）由两条路兜底：`metrics` 里没有 `capital_efficiency` 键
    这件事本身可查（前端因此显示降级态，见 #36），以及 `recompute_regime_slices`
    的全量重算入口。

    返回 `bool` 而不是抛：调用方**不该**为它写 `try`——那正是「附加产物拖垮主产物」的
    入口。要观测失败就看日志（这里 `exc_info=True` 打了完整栈）。

    **「绝不抛」的边界是 `Exception`，不是 `BaseException`**（与 `dispatch_slice` 的
    `except Exception` 同一个语义）：`KeyboardInterrupt` / `SystemExit` 仍然穿过去。
    那两条不是「投递失败」，是「进程现在要停」——把它们也吞掉会让一次 Ctrl-C 变成一次
    静默的成功投递，比一次失败的附加产物坏得多。
    """
    ids = _as_id_list(result_ids)
    if not ids:
        return False
    try:
        compute_capital_efficiency_task.delay(ids)
        return True
    except Exception:
        logger.error(
            "[strategy_factory] 资金效率任务投递失败（回测本身已成功）result_ids=%s",
            ids,
            exc_info=True,
        )
        return False


@app.task(
    bind=True,
    max_retries=MAX_CAPITAL_EFFICIENCY_RETRIES,
    default_retry_delay=RETRY_BACKOFF_SECONDS,
    retry_backoff=True,
    acks_late=True,
)
def compute_capital_efficiency_task(self, result_ids, only_stale: bool = True) -> dict:
    """给一批回测结果算资金效率并写进 `metrics["capital_efficiency"]`。

    Args:
        result_ids: 一个 id 或一串 id。空/全空值时直接返回（收敛闸）。
        only_stale: 已有不陈旧载荷的结果跳过。默认开——它同时兼任「不要重复劳动」与
            「重复投递不会互相打架」；重投整批是安全的收敛策略全靠它。

    幂等：同样的 `close` 行 + 同样的窗口 + 同样的本金 → 同样的载荷（纯模块是纯函数，
    适配层只加时间戳）。所以重试、重投、手动重算都不会叠加副作用。

    单条失败不中断整批：其余结果照样该被算。失败清单进 summary，批末统一抛出去交给
    Celery 重试——这与 `advance_candidates_task` 的「一条失败不拖垮整批」是同一条纪律，
    差别只在收尾：这里**重投整批**比「记住哪几条没成」更省事，而幂等保证了重投的代价
    只是几轮白干（见模块 docstring 第 2 条）。
    """
    from django.db import close_old_connections

    from apps.backtest.models import BacktestResult
    from apps.strategy_factory import capital_efficiency_slicing

    ids = _as_id_list(result_ids)
    if not ids:
        # 收敛闸：没东西可算就干净地结束，不报错——这正是「重投一批已完成的」该有的样子。
        return {"computed": 0, "skipped": 0, "missing": [], "failed": []}

    summary: dict = {"computed": 0, "skipped": 0, "missing": [], "failed": []}
    try:
        for result_id in ids:
            # 长循环里的连接纪律（CLAUDE.md）：批可能上百条（一次网格搜索的全部组合），
            # 每条都在上一次事务边界之外。
            close_old_connections()

            result = BacktestResult.objects.filter(id=result_id).first()
            if result is None:
                # 被删掉了。不是错误，但要记下来——「投了却找不到」与「投漏了」看起来一样。
                summary["missing"].append(result_id)
                continue
            if only_stale and not capital_efficiency_slicing.is_stale(result):
                summary["skipped"] += 1
                continue

            try:
                capital_efficiency_slicing.compute_capital_efficiency_for(result)
                summary["computed"] += 1
            except Exception as exc:
                summary["failed"].append({"result_id": result_id, "error": str(exc)})
                logger.error(
                    "[strategy_factory] 资金效率计算失败 result_id=%s",
                    result_id,
                    exc_info=True,
                )

        if summary["failed"]:
            raise RuntimeError(
                f"{len(summary['failed'])}/{len(ids)} 个结果资金效率计算失败："
                f"{summary['failed'][:3]}"
            )
    except Exception as exc:
        logger.error(
            "[strategy_factory] 资金效率任务失败，将重试（第 %s 次）",
            self.request.retries,
            exc_info=True,
        )
        raise self.retry(exc=exc)

    logger.info(
        "[strategy_factory] 资金效率任务完成 computed=%s skipped=%s missing=%s",
        summary["computed"],
        summary["skipped"],
        len(summary["missing"]),
    )
    return summary
