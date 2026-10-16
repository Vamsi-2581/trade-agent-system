"""候选策略的 REST 端点（spec #19 D21，子票 #23）。

六条路由（前缀 `api/strategy-factory/`），每条都是**薄适配**：

| 路由 | 方法 | 薄在哪 |
| --- | --- | --- |
| `candidates/` | GET | 列表（用户隔离：`proposed_by=request.user`） |
| `candidates/<uuid>/` | GET | 详情（evidence 全文 + 推进史 + 会话指针） |
| `candidates/register/` | POST | **复用** agent 工具的校验与落库（同一套 D17 闸门） |
| `candidates/<uuid>/promote/` | POST | 候选 → paper 会话映射，**整请求委托** `trading.live_session_promote` |
| `candidates/<uuid>/reject/` | POST | 模型状态机守门 + 审计记录 |
| `candidates/<uuid>/cancel/` | POST | 同上 |

## 为什么不在这里重写任何一道闸门

口径唯一出处是这张 map 的 Notes 写死的纪律。登记的注册键校验（D17）、升实盘的
「干跑摘要 → 显式确认 → 目标账户」（D11/D12）各自只有一处实现：登记在
`apps/agent/tools/register_strategy_candidate.py`，升实盘在
`apps/trading/views.py` 的 `live_session_promote`。这里要是把任一道闸门抄一遍，
两条路径迟早给出不同的放行结论——最坏的情况是旁路更宽松。

所以 promote 端点做三件事：**用户隔离校验**（候选是这条用户的）→
**候选 → paper 会话映射**（gate 读的是会话）→ **委托**。返回体也原样透传，
调用方看到的就是 trading 端点的契约（干跑摘要 / 201 建成）。

## 鉴权与用户隔离

全部 `IsAuthenticated` + `proposed_by` 过滤（`live_session_list` 同款惯例）。
详情取不到即 404：不区分「不存在」与「不是你的」，不向探测者泄露存在性。
D1 的 `proposed_by` 可空（`SET_NULL`），没有提出人的历史候选不属于任何 REST 用户，
只有提出人能看见自己的候选。

## 否决 / 取消的边界

只动**人工收尾那两条边**（`*_failed` / 停住态 → `rejected` / `cancelled`），
推进与判定仍是流水线（`orchestrator.advance`）的职责——这里不提供「重跑」、
不提供「指定账户」（那些走流水线与 agent 工具），契约面保持 D21 列的那六个。
"""

from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.strategy_factory.evaluate import ReasonCode
from apps.strategy_factory.models import (
    StrategyCandidate,
    StrategyCandidateTransition,
)
from apps.strategy_factory.serializers import (
    StrategyCandidateDetailSerializer,
    StrategyCandidateListSerializer,
)
from apps.strategy_factory.status import StrategyCandidateLayer

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# 通用小件
# --------------------------------------------------------------------------- #


def _get_own_candidate(request, pk) -> StrategyCandidate | None:
    """取**本用户**的候选；取不到返回 None（视图转 404）。

    `proposed_by` 过滤就是隔离本身：别人的候选与不存在的候选长得一样。
    """
    return StrategyCandidate.objects.filter(pk=pk, proposed_by=request.user).first()


def _record_manual_decision(
    candidate: StrategyCandidate,
    *,
    target_status: str,
    reason_code: ReasonCode,
    note: str,
) -> None:
    """落一次人工收尾：状态（模型守门）+ 审计记录 + 决策时刻，同一事务。

    `transition_to` 只改内存字段（编排层的既有约定），`save` 与转移记录在这里一起
    落库。转移记录的形状与编排层那侧一致（D20：卡住与推进都要可见），reason_code
    用人工专属的两个码，让审计面能区分「机器判它不合格」与「人把它叫停」。
    """
    now = timezone.now()
    from_status = candidate.status
    with transaction.atomic():
        candidate.transition_to(target_status)
        candidate.decided_at = now
        candidate.save(update_fields=["status", "decided_at", "updated_at"])
        StrategyCandidateTransition.objects.create(
            candidate=candidate,
            layer=StrategyCandidateLayer.PIPELINE.value,
            from_status=from_status,
            to_status=candidate.status,
            reason_code=reason_code.value,
            detail={"note": note} if note else {},
            at=now,
        )


# --------------------------------------------------------------------------- #
# 只读
# --------------------------------------------------------------------------- #


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def candidate_list(request):
    """本用户的候选列表（US32：我的候选现在都在哪一层）。"""
    queryset = StrategyCandidate.objects.filter(
        proposed_by=request.user
    ).select_related("strategy", "paper_session", "live_session")
    status_filter = request.query_params.get("status")
    if status_filter:
        queryset = queryset.filter(status=status_filter)
    origin_filter = request.query_params.get("origin")
    if origin_filter:
        queryset = queryset.filter(origin=origin_filter)
    return Response(
        StrategyCandidateListSerializer(
            queryset.order_by("-created_at"), many=True
        ).data
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def candidate_detail(request, pk):
    """候选详情：当前层 + 每层判定与依据 + 关联回测/会话 + 推进史（D21 只读口径）。"""
    candidate = _get_own_candidate(request, pk)
    if candidate is None:
        return Response({"error": "Strategy candidate not found"}, status=404)
    return Response(StrategyCandidateDetailSerializer(candidate).data)


# --------------------------------------------------------------------------- #
# 写：登记（薄适配 agent 工具，不新写 D17 闸门）
# --------------------------------------------------------------------------- #


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def candidate_register(request):
    """登记候选。**复用** agent 工具的同一套校验与落库（D16/D17）。

    `register_strategy_candidate` 工具的 `execute()` 已把「文件级校验 → 注册键校验 →
    落库（失败也建行）」串成一条链，REST 端点把 HTTP 载荷映射成它的参数、把
    `ToolResult` 映射回响应码——**校验逻辑一行不抄**。用户隔离：提出人就是
    `request.user`（工具侧的 `user_id` 走 `resolve_user_id`，认 UUID——传登录用户的
    pk 即解析回同一个人，与渠道侧共用同一条落库路径）。
    """
    import asyncio

    from apps.agent.tools.register_strategy_candidate import (
        RegisterStrategyCandidateTool,
    )

    body = request.data or {}
    strategy_name = str(body.get("strategy_name") or "").strip()
    strategy_file_path = str(body.get("strategy_file_path") or "").strip()
    if not strategy_name or not strategy_file_path:
        return Response(
            {"error": "strategy_name 与 strategy_file_path 都是必填"}, status=400
        )

    tool = RegisterStrategyCandidateTool()
    result = asyncio.run(
        tool.execute(
            strategy_name=strategy_name,
            strategy_file_path=strategy_file_path,
            hypothesis=str(body.get("hypothesis") or "").strip(),
            origin=str(body.get("origin") or "agent").strip(),
            source_ref=body.get("source_ref") or {},
            paper_account_id=str(body.get("paper_account_id") or "").strip() or None,
            user_id=str(request.user.pk),
        )
    )

    if not result.success:
        # 登记失败也建了行（D1/US26）：把行带回去，调用方知道失败记录在哪。
        data = dict(result.data or {})
        data["error"] = result.error
        return Response(data, status=400)

    return Response(result.data, status=201)


# --------------------------------------------------------------------------- #
# 写：人工卡点（升实盘，委托 trading 的唯一实现）
# --------------------------------------------------------------------------- #


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def candidate_promote(request, pk):
    """人工卡点升实盘（D11）：候选 → paper 会话映射后**整请求委托** trading 端点。

    干跑摘要、`confirmed` fail-closed、目标账户 testnet=False 校验、
    `mode_account_mismatch` 不变式——全部住在 `live_session_promote`，这里一件都不抄。
    """
    candidate = _get_own_candidate(request, pk)
    if candidate is None:
        return Response({"error": "Strategy candidate not found"}, status=404)

    if candidate.paper_session_id is None:
        return Response(
            {
                "error": (
                    "这条候选还没有 paper 会话，升实盘的前置（跑满观察期且判定通过）"
                    "无从谈起。请先让流水线推进到 paper 层。"
                )
            },
            status=400,
        )

    # 委托：trading 端点自带「会话属于该用户」的校验（`get(pk, user=request.user)`）。
    # 这里不再预查一遍会话归属——预查就是第二处口径；候选的用户隔离已在上面完成，
    # 会话归属由被委托方负责。
    # 传底层 `HttpRequest`（`request._request`）：被委托端点有自己的 `@api_view`
    # 装饰器，会把请求**再包一层** `Request`；直接传 DRF `Request` 会撞上
    # "must be an instance of HttpRequest" 的断言。
    from apps.trading import views as trading_views

    return trading_views.live_session_promote(
        request._request, candidate.paper_session_id
    )


# --------------------------------------------------------------------------- #
# 写：否决 / 取消（模型状态机守门，非法边 400）
# --------------------------------------------------------------------------- #


def _manual_decision(request, pk, *, target_status, reason_code, action_label):
    """否决与取消的公共体：取候选 → 状态机守门 → 落审计。非法边当场 400。"""
    candidate = _get_own_candidate(request, pk)
    if candidate is None:
        return Response({"error": "Strategy candidate not found"}, status=404)

    note = str((request.data or {}).get("note") or "").strip()
    try:
        _record_manual_decision(
            candidate,
            target_status=target_status,
            reason_code=reason_code,
            note=note,
        )
    except ValueError as exc:
        # `InvalidTransitionError`（ValueError 子类）：状态机说这条边不合法。
        return Response({"error": str(exc)}, status=400)

    logger.info(
        "[strategy_factory.views] candidate %s %s by user %s",
        candidate.pk,
        action_label,
        request.user.pk,
    )
    return Response(
        {
            "id": str(candidate.pk),
            "name": candidate.name,
            "status": candidate.status,
            "decided_at": candidate.decided_at.isoformat()
            if candidate.decided_at
            else None,
        }
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def candidate_reject(request, pk):
    """人工否决（US26：被否决的候选保留在系统里并带原因）。"""
    return _manual_decision(
        request,
        pk,
        target_status="rejected",
        reason_code=ReasonCode.HUMAN_REJECTED,
        action_label="rejected",
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def candidate_cancel(request, pk):
    """人工取消流水线（US15：可取消）。"""
    return _manual_decision(
        request,
        pk,
        target_status="cancelled",
        reason_code=ReasonCode.HUMAN_CANCELLED,
        action_label="cancelled",
    )
