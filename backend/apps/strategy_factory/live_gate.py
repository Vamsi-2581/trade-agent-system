"""升实盘前置闸门（spec #19 D11 / D12 后半，子票 #28）。

本模块是**「这条 paper 会话算不算跑满观察期且判定通过」的唯一出处**，给
`apps/trading/views.py` 的两个入口用：

* `live_session_promote` —— 把一条 paper 会话升成 live 会话；
* `live_session_create(mode="live")` —— 直接从回测建 live 会话（D12 后半要收紧的那个口）。

## 为什么闸门读状态，而不是在这里重算一遍观察期

「跑满观察期且判定通过」这个结论**已经由 L3 判定过了**（`evaluate.py` 的
`_evaluate_paper_running`，子票 #24），落点是 `StrategyCandidate.status ==
awaiting_live_approval`。它的依据是：`LiveSessionStatusChange` 时间线算出的 `running`
时长、`compute_session_stats` 的盈亏、`RiskEvent.live_session` 归集的风控命中。

如果这里再读一遍那些表、把阈值再判一次，同一口径就有了**第二处实现**。而 map 的
Notes 写死了这条纪律：「判定细节一律引 spec 的决策编号，不复制口径（复制会漂移）」。
漂移的后果不是报错，是**两条路径给出不同的放行结论**——最坏的情况是旁路更宽松。
所以这里只读结论，不重算。

## fail-closed（用户已定夺，Q4）

三种「查不出通过」的情形**一律拒绝**，且各有可读原因：查不到候选主行、候选状态不是
`awaiting_live_approval`、候选与传入的 paper 会话对不上。理由：这是一个**收紧密**，
宁可要求人工核对，也不放行一条没走完观察期的候选。历史数据里那些在 P2 流水线之前
建的 paper 会话本来就没有候选主行，它们被拒绝是**预期行为**——它们确实没有走过四层判定。

## 延迟 import（用户已定夺，Q2）

`apps/trading/` 是订单执行链路（热路径），`StrategyCandidate` 属于漏斗。依赖方向保持
`trading → strategy_factory` 单向，且 import 放在函数内：让 `trading` 模块在**不建候选
表**的环境下照样能 import（测试、脚本、迁移期）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: 唯一放行态。**字面量只有这一处**：`apps/trading/` 要靠它比对，而拼错一次的表现是
#: 「所有候选都被拒绝」——那种 bug 看起来像闸门太严，不像打字错。
APPROVED_STATUS = "awaiting_live_approval"


@dataclass(frozen=True)
class PromotionGate:
    """升实盘前置闸门的结论。

    `candidate` / `paper_session` 在放行时非空，`reason` 在拒绝时非空——两者**恰好一个**
    有值（`allowed` 就是那个判据）。做成一个对象而不是 `(bool, str)` 元组：调用方需要
    的不只是「能不能」，还有「拿哪条候选、哪条会话去建新会话」（继承权益与 config）。
    """

    allowed: bool
    reason: str = ""
    candidate: Any = None
    paper_session: Any = None

    def __post_init__(self) -> None:
        if self.allowed and self.candidate is None:
            raise ValueError("闸门放行却没有候选主行：调用方拿不到继承来源")
        if self.allowed and self.paper_session is None:
            raise ValueError("闸门放行却没有 paper 会话：调用方拿不到权益与 config")


def _status_display(status: str) -> str:
    """状态的**人话**说法。查不到映射时原样回显，绝不返回 None。

    回显而不是留空：闸门拒绝时那句原因会直接进 API 响应给用户看，一个 `None` 会变成
    「候选状态是 None」这种把人引向错误方向的文案。
    """
    from apps.strategy_factory.status import StrategyCandidateStatus

    try:
        return StrategyCandidateStatus(status).display
    except ValueError:
        return str(status)


def evaluate_live_gate(paper_session: Any) -> PromotionGate:
    """判断一条 `mode="paper"` 会话能否升实盘；见模块 docstring 的三条纪律。

    `paper_session` 是 `apps.trading.models.LiveSession` 实例。**本函数不查账户、
    不查冲突、不建会话**——那些是调用方（视图）自己的既有闸门，一道都不放松。
    """
    from apps.strategy_factory.models import StrategyCandidate

    # 候选主行经 `paper_session` 反查：`StrategyCandidate.paper_session` 是外键，
    # 一个候选只绑一条 paper 会话。用 `filter(...).first()` 而不是 `.get()`：
    # 历史数据里可能有两条候选指向同一条会话（人工挪过状态），那时该**拒绝**并说清，
    # 而不是抛 `MultipleObjectsReturned`——后者的表现是 500，把闸门问题伪装成服务故障。
    candidates = list(
        StrategyCandidate.objects.filter(paper_session=paper_session).order_by(
            "created_at"
        )
    )
    if not candidates:
        return PromotionGate(
            allowed=False,
            reason=(
                "这条 paper 会话没有对应的候选主行（StrategyCandidate.paper_session），"
                "无从确认它是否跑满观察期并判定通过。"
                "升实盘要求候选经四层漏斗判定："
                "请确认它是经候选流水线建的会话。"
            ),
        )
    candidate = candidates[0]

    if candidate.status != APPROVED_STATUS:
        return PromotionGate(
            allowed=False,
            reason=(
                f"候选「{candidate.name}」当前状态是「{_status_display(candidate.status)}」，"
                f"不是「{_status_display(APPROVED_STATUS)}」（paper 观察通过、等人工卡点）。"
                "升实盘的前置是**跑满观察期且判定通过**："
                "观察期没跑满、盈亏不达标、样本外衰减超标、风控命中超限，都会停在这一步之前。"
                "请先看该候选的判定结论与原因码。"
            ),
        )

    return PromotionGate(allowed=True, candidate=candidate, paper_session=paper_session)


def find_approved_paper_session(*, user: Any, backtest_result: Any) -> PromotionGate:
    """D12 后半：`live_session_create(mode="live")` 的前置查询。

    口径与 `evaluate_live_gate` **同一处**：找该用户、该回测下状态为
    `awaiting_live_approval` 的候选，它的 `paper_session` 就是要继承权益与 config 的那条。

    为什么按 `backtest_result` 而不是按 symbol / strategy 找：`live_session_create` 的
    其余前置全都是围绕这条回测的（`review_status == "approved"`、`backtest.strategy`、
    `backtest.symbol`），闸门跟着同一个键才不会有「策略对了但回测是另一条」的缝。
    """
    from apps.strategy_factory.models import StrategyCandidate

    candidate = (
        StrategyCandidate.objects.filter(
            status=APPROVED_STATUS,
            paper_session__isnull=False,
            paper_session__mode="paper",
            paper_session__user=user,
            paper_session__backtest_result=backtest_result,
        )
        .select_related("paper_session")
        .order_by("created_at")
        .first()
    )
    if candidate is None:
        # 分两种情形给原因：**有** paper 会话但没通过 vs **连** paper 会话都没有。
        # 合并成一句「必须先跑 paper」会把「跑过但没通过」也说成「没跑过」，
        # 用户据此会去重跑一条已经跑过的会话。
        from apps.trading.models import LiveSession

        has_paper = LiveSession.objects.filter(
            user=user, backtest_result=backtest_result, mode="paper"
        ).exists()
        if has_paper:
            return PromotionGate(
                allowed=False,
                reason=(
                    "该回测有 paper 会话，但没有任何候选跑满观察期且判定通过"
                    "（候选状态需为「paper 观察通过、等人工卡点」）。"
                    "观察期没跑满、盈亏不达标、样本外衰减超标、风控命中超限都会停在这一步之前；"
                    "请查候选的判定结论与原因码，确认卡在哪一层。"
                ),
            )
        return PromotionGate(
            allowed=False,
            reason=(
                "必须先跑完 paper 观察期并通过判定才能升实盘："
                "该回测下没有任何 paper 会话。"
                "请走候选流水线（登记 → 样本内 → 样本外 → paper 观察）。"
            ),
        )

    return PromotionGate(
        allowed=True,
        candidate=candidate,
        paper_session=candidate.paper_session,
    )
