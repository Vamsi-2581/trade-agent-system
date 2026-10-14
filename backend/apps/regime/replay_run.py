"""重算差异重放的取数与落库（`replay.py` 的另一半）。

重放的判定是纯的（`replay.plan_reviews`），本模块只做四件事：**取哪些决策要重放 →
取它们上一次的结论 → 落一条差异记录 → 报差异**。判定逻辑一行都不在这里。

## 什么时候跑

只在一代**新的**池化表翻成 `ready` 之后。`recompute_regime_slices._rebuild_pool` 卡三道
门：`dry_run` 不跑、`reused` 不跑、`duplicate` 不跑。三种都不跑的理由是同一个——**没有
新表可重放**。复用那一轮尤其要说清楚：指纹没变 ⇒ 结论逐格相同 ⇒ 重放出来的结论必然与
上一次逐条相同，落下去只会让「今天和昨天有什么不同」多出一批纯噪声的记录。

## 重放哪些决策：`status != RELEASED`

`DeactivationReview` 的 docstring 把判据写死了：只对**生效中的**决策重放（`status !=
RELEASED`），「已解除的决策重放没有意义：没有人在等它的答案」。这里照抄那句话本身
（`exclude`），不是照抄它当下展开的那两个取值（`status__in=(SUGGESTED, APPLIED)`）——
将来真加了第四种状态，`exclude` 的默认仍是「除非明确解除，否则它在生效中」，而白名单
的默认是「悄悄不重放」。前者是安静的多做一点，后者是安静的少做一件。

再叠一条 `strategy__is_active=True`：`is_active` 是人工总退役开关（CONTEXT.md 第 106 条），
已退役的策略不该再收到「你的停用依据失效了，要不要放它跑」这种问句——那个人早就把这条
策略关掉了，这条告警对他只是噪声。它与 `exclude(RELEASED)` 不是一回事：一个是「机制已经
放过它了」，一个是「人已经不要它了」。

## 上一次的结论要先读，这一代的要后写

「相对上一代的差异」要的是**同一条决策在上一次重放里的结论**，所以两张读各自独立：

- `previous`：每一条决策最近一次的结论（不限是哪一代），用于 `Review.changed`；
- `already`：**这一代**已经重放过的决策 id。

`already` 存在的意义是「这一代已经评过了就不评第二次」。没有它，同一代被重放两次会
落到 `ignore_conflicts=True` 上——写是写不进去，但返回值里的条数会说谎（报了一堆
「已重放」，实际一行没写）。`ignore_conflicts=True` 仍然留着，那是给**并发**兜底的，
不是给重复调用兜底的：它保证撞车的那一方不会在批量写入中途炸掉。

## 这里不调 `close_old_connections()`

三次读（决策、上次结论、已评过的）+ 一次批量写，中间没有长循环里的重复事务边界。长
循环的连接纪律属于**调用方**（`recompute_regime_slices` 已经自带），不属于这个被调用的
函数——理由与 `pool_rebuild` 的同名段落逐字相同。

## 本层不发告警

`apps/regime/tasks.py` 的模块纪律是「任务自己**不写告警**，失败往上抛」。本层不是任务，
但同一条道理在这里更硬：告警是「给一个具体的人的即时消息」（`apps/trading/alerts.py`），
而**收件人要读 `LiveSession`**——那是另一件事的取数，不该混进「算差异」这一步。所以这里
只把待告知的名单放进返回值，收件人由 `alert_recipients` 单独取，投递由**调用方**
（管理命令）做。CLI 那条路上，触发者自己就在 stdout 前面站着，所以「没有收件人」在
那里不等于「没人看见」。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Iterable, Sequence
from uuid import UUID

from django.utils import timezone

from apps.regime import deactivation_run, pool_rebuild, replay
from apps.regime.models import (
    DeactivationDecision,
    DeactivationReview,
    DecisionStatus,
    RegimePoolRebuild,
)
from apps.trading.models import LiveSession

logger = logging.getLogger(__name__)

#: 一次批量写的条数。**决策条数 = 被管策略数 × 最多 4 个阶段**，正常规模下一次
#: `bulk_create` 就写完了；`batch_size` 只是让策略表涨到几千条时内存不至于被一次请求
#: 撑爆，不是事务边界。
REVIEW_BATCH_SIZE = 200


def alert_recipients(strategy_ids: Iterable[UUID]) -> tuple[Any, ...]:
    """受影响策略的**活跃实盘会话**所属用户，去重后按 id 排序。

    `mode` 只认 `live`、`status` 用 `LiveSession.ACTIVE_STATUSES`——与
    `deactivation_run.running_strategy_ids` 同一个口径（判据二的定义只有一处）。
    模拟盘不持仓真钱，也就不会被停用决策影响到。

    返回空元组是**合法**的：可能一条实盘会话都没有（第①段是零执行的 Shadow，这是常态）。
    那时 `notify_user` 拿不到收件人，会说「没有接收人就不算告警」——调用方要把这件事
    如实报出来，不能把它读成「已经通知过了」。
    """
    ids = list(strategy_ids)
    if not ids:
        return ()
    return tuple(
        LiveSession.objects.filter(
            strategy_id__in=ids,
            mode="live",
            status__in=LiveSession.ACTIVE_STATUSES,
        )
        .values_list("user_id", flat=True)
        .distinct()
        .order_by("user_id")
    )


def _previous_verdicts(decision_ids: Sequence[int]) -> dict[int, str]:
    """每条决策**最近一次**重放的结论。按时间升序遍历，字典后写覆盖先写。"""
    if not decision_ids:
        return {}
    rows = (
        DeactivationReview.objects.filter(decision_id__in=decision_ids)
        .order_by("reviewed_at", "id")
        .values_list("decision_id", "verdict")
    )
    return {decision_id: verdict for decision_id, verdict in rows}


def _review_evidence(
    review: replay.Review, decision: DeactivationDecision, generation: RegimePoolRebuild
) -> dict[str, Any]:
    """落进 `DeactivationReview.evidence` 的那一份。

    只有一样东西必须在里面：**新表那一格的依据摘要原文**。池化表还会再换代，所以它得
    抄进来，不能留个指针——这条差异记录的全部意义就是「当时新表是这么说的」。

    `stale_basis` 记的是**决策自己冻的那一代**与这一代的差：它就是「依据已随重算失效」
    这句话的字面依据，而不只是结论恰好翻成了 `fit`。两者分开有用——结论仍是 `unfit`
    但依据已经换代，也是重算发生过的事实。
    """
    decision_basis = decision.pool_rebuild_id
    return {
        "regime": review.regime,
        "state": review.state,
        "reason": review.reason,
        "source": review.source,
        "needs_review": review.needs_review,
        # 新表那一格的依据摘要原文（`RegimePoolCell.evidence`）。
        "cell": dict(review.cell),
        "pool_rebuild_id": generation.id,
        "pool_version": generation.pool_version,
        # 决策当初冻的是哪一代、以及它是不是已经不是当前这一代了。
        "decision_pool_rebuild_id": decision_basis,
        "stale_basis": decision_basis != generation.id,
        # 上一次重放的结论（`None` = 从没重放过）。「相对上一代的差异」靠它。
        "previous_verdict": review.previous_verdict,
    }


def run_replay(
    *,
    generation: RegimePoolRebuild | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """对每一条生效中的停用决策重放当前代池化表，落一条 `DeactivationReview`。

    幂等：同一代已经评过的决策跳过（见模块 docstring）。返回一个**可 JSON 序列化**的
    摘要，键集在两条路径上一致，`skipped` 恒存在。

    Args:
        generation: 要重放的那一代。不传取**当前**那一代（最近翻成 `ready` 的）。
            调用方通常不传——`rebuild_pool` 刚翻完代，当前代就是新一代，而
            `pool_rebuild.current_cells(generation)` 内部只问一次当前代，配对读取
            这件事在那里已经钉住了。
        now: 注入时钟；不传取当前时刻。写成 `reviewed_at`——它**没有默认值**，必须显式传。
    """
    now = now or timezone.now()
    summary: dict[str, Any] = {
        "generation_id": None,
        "skipped": None,
        "note": "",
        "decisions": 0,
        "written": 0,
        "already": 0,
        "changed": 0,
        "verdicts": {verdict.value: 0 for verdict in replay.ReviewVerdict},
        "invalidated": [],
    }

    generation = generation or pool_rebuild.current_generation()
    if generation is None:
        # 与 `deactivation_run` 同一个取值、同一句话：这一层没有自己的「没有池化表」。
        summary["skipped"] = deactivation_run.SKIPPED_NO_GENERATION
        summary["note"] = "还没有任何一代池化表，本轮没有新表可重放"
        logger.info("[regime] 停用决策重放：%s", summary["note"])
        return summary

    summary["generation_id"] = generation.id

    decisions = list(
        DeactivationDecision.objects.exclude(status=DecisionStatus.RELEASED.value)
        .filter(strategy__is_active=True)
        .select_related("strategy")
        .order_by("strategy_id", "regime")
    )
    if not decisions:
        # 「没有要重放的决策」不是「问不出来」：新表明明在手里，只是没有人等答案。
        # 所以 `skipped` 留 `None`，与冷启动/没有池化表那两种收场分开。
        logger.info("[regime] 停用决策重放：没有生效中的决策，跳过")
        return summary

    reviewed = set(
        DeactivationReview.objects.filter(rebuild=generation).values_list(
            "decision_id", flat=True
        )
    )
    pending = [d for d in decisions if d.id not in reviewed]
    summary["decisions"] = len(decisions)
    summary["already"] = len(decisions) - len(pending)
    if not pending:
        logger.info(
            "[regime] 停用决策重放：第 %s 代已评过全部 %s 条，跳过",
            generation.id,
            len(decisions),
        )
        return summary

    cells = pool_rebuild.current_cells(generation)
    previous = _previous_verdicts([d.id for d in pending])
    planned = replay.plan_reviews(
        (
            replay.DecisionRef(id=d.id, strategy_id=d.strategy_id, regime=d.regime)
            for d in pending
        ),
        cells,
        previous=previous,
    )

    by_id = {d.id: d for d in pending}
    DeactivationReview.objects.bulk_create(
        [
            DeactivationReview(
                decision_id=review.decision_id,
                rebuild=generation,
                verdict=review.verdict,
                evidence=_review_evidence(
                    review, by_id[review.decision_id], generation
                ),
                reviewed_at=now,
            )
            for review in planned
        ],
        batch_size=REVIEW_BATCH_SIZE,
        # 并发兜底：同一 (决策, 世代) 的批量约束是 `uniq_deactivation_review_decision_rebuild`。
        ignore_conflicts=True,
    )

    summary["written"] = len(planned)
    summary["changed"] = sum(1 for review in planned if review.changed)
    summary["verdicts"] = replay.count_verdicts(planned)
    summary["invalidated"] = [
        {
            "strategy_id": str(review.strategy_id),
            # 名字进告警正文。已退役的决策不在这里（上面那道 `is_active` 闸），
            # 所以 `select_related` 取到的名字一定是给人看的那个。
            "name": by_id[review.decision_id].strategy.name,
            "regime": review.regime,
            "previous_verdict": review.previous_verdict,
        }
        for review in planned
        if review.invalidated
    ]

    logger.info(
        "[regime] 停用决策重放：第 %s 代，决策 %s 条（已评过 %s，本轮 %s），"
        "结论 %s，其中与上次不同 %s，依据失效 %s",
        generation.id,
        summary["decisions"],
        summary["already"],
        summary["written"],
        summary["verdicts"],
        summary["changed"],
        len(summary["invalidated"]),
    )
    return summary
