"""候选漏斗的**薄编排**（spec #19 D14 的第二半，子票 #22）。

与 `evaluate.py` 的分工是刻意的一刀：

* `evaluate_candidate(evidence) -> Verdict` 做**判断**（零 I/O、纯数据、可穷举测试）；
* `advance(candidate, *, deps)` 做**串线**——取证据 → 调判定 → 落状态 + 写转移记录 +
  （必要时）起跑下一层 / 建 paper 会话 / 通知提出人。

所以这个模块里**没有任何阈值判定**：一个 `if sharpe >= 1.5` 都不该出现在这里。所有的
「要不要放行」都在 `evaluate.py`，这里只负责「把哪份证据递给它、把它的结论落到哪」。

## 三条纪律

1. **所有外部效应经 `deps` 注入**（D14）。跑回测、建会话、发通知、取现时刻，四件事
   都不在这里直接做。这换来的是 D14 要的那条**接线用例**：注进假回测执行与假会话
   创建，就能断言「状态落库 + 转移记录 + 建了/没建会话 + 通知发了」四件事，而不必
   真的跑一次回测、连一次交易所。

2. **一步一层，且同层不重复起跑**。一次 `advance` 只推进「一层的一步」：要么起跑该层
   的活儿，要么读该层的结论并落状态。判断某层是否已经起跑的依据是**候选 `evidence`
   里的层记录**（`layers.<层>`），它与状态在**同一个事务**里落库。D18 的幂等键是
   「候选 × 层」，这里就是它的实现：同层第二次推进看到层记录就不再起跑，于是
   「同层推进两次 → 一组 fold 回测、一条 paper 会话」。

3. **并发靠候选行的行锁 + 状态前置条件**（D18）。整个 `advance` 在
   `select_for_update` 的候选行锁里跑：同一候选同一时刻只有一个推进（第二个推进会在
   锁上等，醒来后**重新读**到新状态、从新状态继续）。`transition_to` 的合法边表是第二
   道闸门——非法边直接抛 `InvalidTransitionError`，不静默吞掉。

## 为什么行锁里包含「跑回测」

延迟写（先记「已起跑」、出锁再跑）会在进程中途死掉时留下**认领了却没产出**的层记录，
而那种状态与「在飞」在库上长得一模一样——又是一个静默卡死。默认接线是**同步**的
（`effects.py`），起跑返回时结果已落库，所以锁内跑完是可行且最简单的：崩溃即整体回滚，
不留半个状态。代价是一次推进期间该候选的其它推进阻塞等锁，而那正是 D18 要的串行。

## 通知为什么在 `on_commit` 里发

通知是「这一步真的发生了」的对外承诺。放进事务里发，一次随后回滚的推进就会告诉用户
一件没发生的事（而回滚后记录也没落，重试还会再发一次）。所以通知挂 `transaction.on_commit`：
只有这一步真的提交了才发。测试里用 pytest-django 的
`django_capture_on_commit_callbacks(execute=True)` 把它跑出来。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping

from django.db import transaction
from django.utils import timezone

from apps.strategy_factory.config import CONFIG, FactoryConfig
from apps.strategy_factory.evaluate import (
    CandidateEvidence,
    InSampleEvidence,
    OosEvidence,
    PaperEvidence,
    ReasonCode,
    Verdict,
    evaluate_candidate,
)
from apps.strategy_factory.models import (
    StrategyCandidate,
    StrategyCandidateTransition,
)
from apps.strategy_factory.status import (
    StrategyCandidateLayer,
    StrategyCandidateStatus,
    is_terminal,
)
from apps.strategy_factory.walk_forward import FoldPlan, plan_folds

#: 证据 JSON 里放层记录与跑批判据的键。都是字面量常量，**不许在别处再抄一份**：
#: `effects.py` 的默认实现要按同一组键读写，抄两份就会在改一处时静默对不上。
RUN_CONTEXT_KEY = "run_context"
LAYERS_KEY = "layers"
VERDICT_KEY = "verdict"

LAYER_IN_SAMPLE = "in_sample"
LAYER_OOS = "oos"
LAYER_PAPER = "paper"


class MissingRunContextError(ValueError):
    """候选没有可用的跑批判据（标的 / 周期 / 评估窗口 / 初始资金）。

    **刻意 Fail Loud**：跑批判据不是判定结论，缺了它连「跑哪一段」都不知道。若把它
    当成一次「证据不足」的判定落库，提出人会被告知「你的策略不行」，而真实原因是
    「没人告诉流水线该跑什么」——这正是 D12 要修的那类「把配置缺口伪装成结论」。
    """


class AdvanceWiringError(ValueError):
    """接线错误（层记录指向的行不存在、paper 账户不满足不变式……）。

    与 `MissingRunContextError` 同一条纪律：这些都不是「候选不合格」，而是**机制被接错
    了**。抛出去让任务失败并可见，比让漏斗把一个接错的候选放行要好。
    """


@dataclass(frozen=True)
class RunContext:
    """候选的**完整评估窗口**与跑批上下文（D15 的几何由它推出）。

    为什么不在 `StrategyCandidate` 上加这几列（spec D1 的字段形状里也没有）：评估窗口
    是**一次跑批的输入**，不是候选的身份。它由触发方（agent 工具 / API / 定时任务）
    给出，经 `deps.resolve_run_context` 注入，第一次用到时**钉进 `evidence`**——
    这样后续每一层跑的都是同一段窗口，中途改 deps 也不会让样本内与样本外的几何分家
    （#29 交接点名的那类低估：样本内行覆盖到样本外段，衰减被系统性低估）。

    `is_perp` / `include_funding` / `exchange` 也在这里：它们是成本模型口径的输入，
    而 `_validate_wiring` 会因为「主行计了资金费率、折却没计」当场拒绝——两边必须
    来自同一份上下文，不能各传各的。
    """

    symbol: str
    timeframe: str
    evaluation_start: date
    evaluation_end: date
    initial_capital: Decimal
    parameters: Mapping[str, Any] = field(default_factory=dict)
    exchange: str = "binance"
    is_perp: bool = False
    include_funding: bool = True

    def __post_init__(self) -> None:
        if not self.symbol or not self.timeframe:
            raise ValueError(
                f"跑批判据缺标的或周期：symbol={self.symbol!r} timeframe={self.timeframe!r}"
            )
        if self.evaluation_end <= self.evaluation_start:
            raise ValueError(
                f"评估窗口必须 end > start，当前 [{self.evaluation_start}, {self.evaluation_end}]"
                "——空窗口或反窗口切不出折，而它会安静地跑出一堆看起来正常的数字"
            )
        if not isinstance(self.initial_capital, Decimal):
            # Decimal 之外的输入（float）会在成本模型里变成浮点误差，且不可序列化。
            raise ValueError(
                f"initial_capital 必须是 Decimal（不要 float），当前 {type(self.initial_capital).__name__}"
            )
        if not self.initial_capital.is_finite() or self.initial_capital <= 0:
            raise ValueError(
                f"initial_capital 必须是正的有限数，当前 {self.initial_capital}"
            )

    def as_payload(self) -> dict[str, Any]:
        """JSON-safe 快照，落 `evidence[RUN_CONTEXT_KEY]`。"""
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "evaluation_start": self.evaluation_start.isoformat(),
            "evaluation_end": self.evaluation_end.isoformat(),
            "initial_capital": str(self.initial_capital),
            "parameters": dict(self.parameters or {}),
            "exchange": self.exchange,
            "is_perp": bool(self.is_perp),
            "include_funding": bool(self.include_funding),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "RunContext":
        """从 `evidence` 里取回。**不宽容**：残缺的快照直接报错。

        宽容地取回（缺键就回退默认值）会让「窗口被改过一半」的候选继续跑，而它的
        样本内行与分折几何来自两个不同的窗口——衰减照样算得出来，只是没有意义。
        """
        try:
            return cls(
                symbol=str(payload["symbol"]),
                timeframe=str(payload["timeframe"]),
                evaluation_start=date.fromisoformat(str(payload["evaluation_start"])),
                evaluation_end=date.fromisoformat(str(payload["evaluation_end"])),
                initial_capital=Decimal(str(payload["initial_capital"])),
                parameters=dict(payload.get("parameters") or {}),
                exchange=str(payload.get("exchange") or "binance"),
                is_perp=bool(payload.get("is_perp", False)),
                include_funding=bool(payload.get("include_funding", True)),
            )
        except (KeyError, InvalidOperation, TypeError, ValueError) as exc:
            raise AdvanceWiringError(
                f"evidence 里的跑批判据无法解析：{payload!r}（{exc}）"
            ) from exc


@dataclass(frozen=True)
class AdvanceDeps:
    """`advance` 的全部外部效应（D14）。默认实现见 `effects.default_deps()`。

    每个成员都是**一个**副作用，签名刻意窄：接线用例可以只替换其中一个（比如只假
    「建会话」），其余走真实实现——这正是「薄到只需要一条接线用例」的落地方式。
    """

    #: 取候选的跑批判据；返回 None 表示触发方没给（编排层据此 Fail Loud）。
    resolve_run_context: Callable[[StrategyCandidate], RunContext | None]
    #: 起跑该候选的**样本内**回测；返回至少含 `result_id` 的映射（可含 `error`）。
    #: 只在「该层尚未起跑」时被调用（幂等由编排层的层记录保证）。
    start_in_sample: Callable[
        [StrategyCandidate, RunContext, FoldPlan], Mapping[str, Any]
    ]
    #: 读样本内结论；None = 尚无结论（在跑 / 还没跑）。
    read_in_sample: Callable[[StrategyCandidate], InSampleEvidence | None]
    #: 起跑**样本外分折**；返回分折载荷（与 `metrics["walk_forward"]` 同形）。
    start_oos: Callable[
        [StrategyCandidate, RunContext, FoldPlan, str], Mapping[str, Any]
    ]
    #: 读样本外结论；None = 尚无结论。
    read_oos: Callable[[StrategyCandidate], OosEvidence | None]
    #: 建 `mode="paper"` 会话（含余额、参数继承、不变式与冲突检查）。
    create_paper_session: Callable[[StrategyCandidate], Any]
    #: 读 **paper 观察期**的证据（#24）：观察期的两个数、盈亏统计、风控命中。
    #: None = 尚无会话 / 会话行不见了（停在观察中），与另外两层的 None 同义。
    read_paper: Callable[[StrategyCandidate], PaperEvidence | None]
    #: 通知候选提出人；返回「是否投递到一个具体的人」。
    notify: Callable[[StrategyCandidate, str], Any]
    #: 现时刻（转移记录的 `at`、层记录的 `started_at` 都读它，测试可注入固定值）。
    now: Callable[[], datetime]
    #: 判定阈值与总开关。默认取进程级单例。
    config: FactoryConfig = CONFIG


@dataclass(frozen=True)
class AdvanceOutcome:
    """一次 `advance` 干了什么（任务的返回值、接线用例的断言面）。"""

    candidate_id: str
    layer: StrategyCandidateLayer
    from_status: StrategyCandidateStatus
    to_status: StrategyCandidateStatus
    reason_code: ReasonCode
    #: 状态是否真的动了（False = 停住：等账户 / 排队 / 观察中 / 总开关关闭 / 终态）。
    changed: bool = False
    #: 这次调用是否**起跑**了该层的活儿（起跑与判定在同一个调用里，见模块 docstring）。
    started_run: bool = False
    #: 这次调用是否新建了 paper 会话。
    created_paper_session: bool = False
    paper_session_id: str | None = None
    #: 是否写了转移记录（同一结论连来时**不重复写**，见 `_is_repeat`）。
    recorded: bool = False
    #: 排队到 `on_commit` 的通知正文；None = 这次不发通知。
    notify_message: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class _Step:
    """一层一步的中间结果：结论 + 要一并落库的层记录 / 会话。"""

    layer: StrategyCandidateLayer
    verdict: Verdict
    layer_key: str | None = None
    layer_record: dict[str, Any] | None = None
    context: RunContext | None = None
    started_run: bool = False
    paper_session: Any = None
    extra_detail: dict[str, Any] = field(default_factory=dict)


def advance(
    candidate: StrategyCandidate, *, deps: AdvanceDeps | None = None
) -> AdvanceOutcome:
    """把候选往前推**一步**（D14 / D18）。

    传进来的 `candidate` 只用来**认行**：函数在候选行锁里重新读一次，之后的一切都基于
    库里那一份。所以调用方手里那份可能是过期的（甚至只关心 `pk`），不会被当成真相。

    不抛「候选不合格」——那是结论（落 `*_failed` 状态 + 原因码）。抛的是**接线错误**：
    缺跑批判据、层记录指向的行不见、paper 账户不满足 `mode_account_mismatch` 这类。
    """
    if deps is None:
        deps = _default_deps()

    cfg = deps.config
    with transaction.atomic():
        fresh = StrategyCandidate.objects.select_for_update().get(pk=candidate.pk)
        from_status = fresh.status_enum

        # 终态不再推进：**不写记录、不发通知**。轮询会把终态候选一遍遍扫到，若每次都
        # 落一行「终态不再自动推进」，审计面就被噪音淹掉，真正要看的那几行找不出来了。
        if is_terminal(from_status):
            verdict = evaluate_candidate(
                CandidateEvidence(status=from_status, thresholds=cfg)
            )
            return AdvanceOutcome(
                candidate_id=str(fresh.pk),
                layer=StrategyCandidateLayer.PIPELINE,
                from_status=from_status,
                to_status=from_status,
                reason_code=verdict.reason_code,
            )

        step = _step(fresh, deps)
        at = deps.now()

        # ---- 落状态 + 证据（一次 save，同一个事务） -------------------------- #
        evidence = dict(fresh.evidence or {})
        if step.context is not None:
            evidence[RUN_CONTEXT_KEY] = step.context.as_payload()
        layers = dict(evidence.get(LAYERS_KEY) or {})
        key = step.layer_key or _layer_key_of(step.layer)
        spent = key is not None and _run_is_spent(step.layer, step.verdict.next_status)
        if spent:
            # 「这一层的这次起跑已经花掉了」：留着它会让状态机里那条「改参数重跑」的边
            # （in_sample_failed → in_sample_running 等）变成一句空话——重进同一层时
            # 编排层看到旧记录就不起跑，直接把上一次的失败结论再念一遍。
            #
            # 判据取自**这一步的结论**（而不是「当前状态是不是失败态」）：失败常常是在
            # 「起跑 + 读结论」的同一次推进里定下来的，那时状态还没落库。
            #
            # 注意这一步**同时**起了跑的情形（起跑 → 结论是失败）：记录不能写回去，
            # 否则刚作废的又原地复活。那次起跑的 id / 时刻进转移记录的 `detail`，
            # 审计不丢（`_detail_payload` 会把层记录带进去）。
            layers.pop(key, None)
        if step.layer_record is not None and step.layer_key is not None and not spent:
            layers[step.layer_key] = step.layer_record
        evidence[LAYERS_KEY] = layers
        evidence[VERDICT_KEY] = {
            "layer": step.layer.value,
            "from": from_status.value,
            "to": step.verdict.next_status.value,
            "reason_code": step.verdict.reason_code.value,
            "detail": _detail_payload(step),
            "at": at.isoformat(),
        }
        fresh.evidence = evidence

        changed = step.verdict.next_status is not from_status
        if changed:
            # 非法边在这里 Fail Loud（`InvalidTransitionError`）：判定层只可能给出
            # 转移表里的边，给不出来就说明有人把两边的表改分家了，不能静默跳过。
            fresh.transition_to(step.verdict.next_status)

        update_fields = ["status", "evidence", "updated_at"]
        if step.paper_session is not None:
            fresh.paper_session = step.paper_session
            update_fields.append("paper_session")
        if step.paper_session is not None or changed:
            fresh.save(update_fields=update_fields)
        else:
            fresh.save(update_fields=["evidence", "updated_at"])

        # ---- 写转移记录（同一结论连来时不重复写） ---------------------------- #
        detail = _detail_payload(step)
        recorded = False
        if not _is_repeat(fresh, from_status, step.verdict):
            StrategyCandidateTransition.objects.create(
                candidate=fresh,
                layer=step.layer.value,
                from_status=from_status.value,
                to_status=step.verdict.next_status.value,
                reason_code=step.verdict.reason_code.value,
                detail=detail,
                at=at,
            )
            recorded = True

        # ---- 通知（只在「真的发生了」时发，且挂 on_commit） ------------------ #
        notify_message = None
        if recorded and _should_notify(from_status, step.verdict):
            notify_message = _message(fresh, from_status, step, cfg)
            transaction.on_commit(
                lambda message=notify_message, target=fresh: deps.notify(
                    target, message
                )
            )

        return AdvanceOutcome(
            candidate_id=str(fresh.pk),
            layer=step.layer,
            from_status=from_status,
            to_status=step.verdict.next_status,
            reason_code=step.verdict.reason_code,
            changed=changed,
            started_run=step.started_run,
            created_paper_session=step.paper_session is not None,
            paper_session_id=(
                str(step.paper_session.pk) if step.paper_session is not None else None
            ),
            recorded=recorded,
            notify_message=notify_message,
            detail=detail,
        )


# --------------------------------------------------------------------------- #
# 分层推进
# --------------------------------------------------------------------------- #


def _step(fresh: StrategyCandidate, deps: AdvanceDeps) -> _Step:
    """按候选当前状态分派到对应那一层的一步。"""
    status = fresh.status_enum
    cfg = deps.config

    if not cfg.auto_advance_enabled:
        # 总开关关掉的是**自动推进**，不是人工动作（否决 / 取消 / 升实盘不经这里）。
        return _Step(
            StrategyCandidateLayer.PIPELINE,
            evaluate_candidate(CandidateEvidence(status=status, thresholds=cfg)),
        )

    if status is StrategyCandidateStatus.REGISTERED:
        # 登记完成的下一步是自动的（起样本内回测），不需要等任何证据。
        return _Step(
            StrategyCandidateLayer.IN_SAMPLE,
            evaluate_candidate(CandidateEvidence(status=status, thresholds=cfg)),
        )

    if status is StrategyCandidateStatus.IN_SAMPLE_RUNNING:
        return _in_sample_step(fresh, deps)

    if status is StrategyCandidateStatus.OOS_RUNNING:
        return _oos_step(fresh, deps)

    if status is StrategyCandidateStatus.PAPER_PENDING:
        return _paper_pending_step(fresh, deps)

    if status is StrategyCandidateStatus.PAPER_RUNNING:
        return _paper_running_step(fresh, deps)

    # registration_failed / in_sample_failed / oos_failed / paper_failed /
    # awaiting_live_approval：都不采证据，判定层各自给出「等谁动」的结论。
    #
    # 三个 `*_failed` 顺带把该层的起跑记录作废：它们是「这一层这次没成」的落点，而
    # 状态机给它们留了回边的（`in_sample_failed → in_sample_running` 等，D3「改参数
    # 重跑」）。不作废的话，重进同一层会被旧记录挡住、连回测都不跑，
    # 「重跑」两个字在代码上不存在。
    return _Step(
        _layer_of(status),
        evaluate_candidate(CandidateEvidence(status=status, thresholds=cfg)),
    )


def _in_sample_step(fresh: StrategyCandidate, deps: AdvanceDeps) -> _Step:
    """L1 的那一步：还没起跑就起跑；起了就读结论并判定。"""
    cfg = deps.config
    layers = _layers(fresh)
    ctx = _resolve_context(fresh, deps)
    plan = plan_folds(ctx.evaluation_start, ctx.evaluation_end, config=cfg)

    if plan.data_sufficient is False and LAYER_IN_SAMPLE not in layers:
        # 窗口切不出 N 折（每折至少一天）→ **不浪费一次样本内回测**：它过了也没用，
        # 下一层的几何照样切不出折。落「证据不足」，理由与几何原文一起写进转移记录，
        # 提出人据此知道该拉长窗口而不是改参数。
        verdict = evaluate_candidate(
            CandidateEvidence(
                status=StrategyCandidateStatus.IN_SAMPLE_RUNNING,
                in_sample=InSampleEvidence(total_trades=None),
                thresholds=cfg,
            )
        )
        return _Step(
            StrategyCandidateLayer.IN_SAMPLE,
            verdict,
            context=ctx,
            extra_detail={
                "geometry": plan.as_payload(),
                "insufficient_reason": plan.reason,
                "note": "评估窗口切不出样本外折，未起跑样本内回测",
            },
        )

    if LAYER_IN_SAMPLE not in layers:
        started = dict(deps.start_in_sample(fresh, ctx, plan) or {})
        record = {
            "result_id": _as_str(started.get("result_id")),
            "started_at": deps.now().isoformat(),
            "in_sample_end": plan.in_sample_end.isoformat(),
        }
        if started.get("error"):
            record["error"] = str(started["error"])
        # 起跑之后**立刻**读结论：默认接线是同步的（跑完才返回），异步接线则读到 None
        # 而停在 in_sample_running 等下一轮——两条路径共用同一段代码。
        verdict = _in_sample_verdict(fresh, deps, cfg, record)
        return _Step(
            StrategyCandidateLayer.IN_SAMPLE,
            verdict,
            layer_key=LAYER_IN_SAMPLE,
            layer_record=record,
            context=ctx,
            started_run=True,
            extra_detail={
                "started": _public_start(started),
                "in_sample_end": record["in_sample_end"],
            },
        )

    # 层记录已在：只读结论，**绝不重跑**（D18 的「候选 × 层」幂等键）。
    return _Step(
        StrategyCandidateLayer.IN_SAMPLE,
        _in_sample_verdict(fresh, deps, cfg, layers[LAYER_IN_SAMPLE]),
        context=ctx,
        extra_detail={"layer_record": _public_layer(layers[LAYER_IN_SAMPLE])},
    )


def _in_sample_verdict(
    fresh: StrategyCandidate,
    deps: AdvanceDeps,
    cfg: FactoryConfig,
    record: Mapping[str, Any],
) -> Verdict:
    """读 L1 结论并判定；**判定通过时由漏斗写审核字段**（D12 的另一半）。

    审核字段的写入职责是本票的核心交付之一：回测保存路径（T0，`save_backtest_result`）
    已经不再写 `approved`，所以「自动通过」只能在这里发生。写的时候**只把 `pending`
    推进为 `approved`**——人工已经给出过结论（批准或否决）的行不留痕迹地覆盖，等于把
    一次人工审核抹掉，那正是 T0 修掉的那类缺陷的镜像。

    `result_id` **由调用方传进来**，不从 `evidence` 读：默认接线是同步的，「起跑」与
    「读结论」发生在同一次推进里，那时这一层的记录还在内存里、还没落库——从 `evidence`
    读会读到上一次那一份（首次更是读不到），审核于是静默没写，而候选照样往前走，
    直到建 paper 会话那一步才炸在「主行没 approved」上。这个缺陷是用例先发现的。
    """
    evidence = deps.read_in_sample(fresh)
    verdict = evaluate_candidate(
        CandidateEvidence(
            status=StrategyCandidateStatus.IN_SAMPLE_RUNNING,
            in_sample=evidence,
            thresholds=cfg,
        )
    )
    if verdict.reason_code is ReasonCode.IN_SAMPLE_PASSED:
        _approve_in_sample_row(record.get("result_id"), deps.now())
    return verdict


def _approve_in_sample_row(result_id: Any, at: datetime) -> int:
    """把样本内行从 `pending` 推进为 `approved` 并落 `reviewed_at`；返回改了几行。"""
    from apps.backtest.models import BacktestResult

    if not result_id:
        return 0
    return BacktestResult.objects.filter(id=result_id, review_status="pending").update(
        review_status="approved", reviewed_at=at
    )


def _oos_step(fresh: StrategyCandidate, deps: AdvanceDeps) -> _Step:
    """L2 的那一步：还没跑分折就跑；跑了就读结论并判定。"""
    cfg = deps.config
    layers = _layers(fresh)
    ctx = _resolve_context(fresh, deps)
    plan = plan_folds(ctx.evaluation_start, ctx.evaluation_end, config=cfg)
    in_sample_result_id = _layer_value(fresh, LAYER_IN_SAMPLE, "result_id")
    if not in_sample_result_id:
        raise AdvanceWiringError(
            f"候选 {fresh.pk} 到了样本外层，却没有样本内行 id（evidence.layers.in_sample.result_id）"
            "——它正是衰减的分母，缺了它分折无从比较"
        )

    if LAYER_OOS not in layers:
        payload = dict(deps.start_oos(fresh, ctx, plan, in_sample_result_id) or {})
        record = {
            "started_at": deps.now().isoformat(),
            "in_sample_result_id": str(in_sample_result_id),
        }
        verdict = _oos_verdict(fresh, deps, cfg, record)
        return _Step(
            StrategyCandidateLayer.OOS,
            verdict,
            layer_key=LAYER_OOS,
            layer_record=record,
            context=ctx,
            started_run=True,
            extra_detail={
                "start": _public_payload(payload),
                "in_sample_result_id": str(in_sample_result_id),
            },
        )

    return _Step(
        StrategyCandidateLayer.OOS,
        _oos_verdict(fresh, deps, cfg, layers[LAYER_OOS]),
        context=ctx,
        extra_detail={"layer_record": _public_layer(layers[LAYER_OOS])},
    )


def _oos_verdict(
    fresh: StrategyCandidate,
    deps: AdvanceDeps,
    cfg: FactoryConfig,
    record: Mapping[str, Any],
) -> Verdict:
    evidence = deps.read_oos(fresh)
    return evaluate_candidate(
        CandidateEvidence(
            status=StrategyCandidateStatus.OOS_RUNNING, oos=evidence, thresholds=cfg
        )
    )


def _paper_pending_step(fresh: StrategyCandidate, deps: AdvanceDeps) -> _Step:
    """L3 入口那一步：等一次人工输入（D10）与串行排队（D9），都就绪才建会话。"""
    from apps.trading.models import LiveSession

    cfg = deps.config
    account = fresh.paper_account
    symbol = _resolve_symbol(fresh, deps)

    account_problem = _paper_account_problem(account)
    conflict = (
        LiveSession.conflicting_active_session(account, symbol)
        if account_problem is None
        else None
    )
    evidence = PaperEvidence(
        account_ready=account_problem is None,
        queued_behind_active_session=bool(conflict),
    )
    verdict = evaluate_candidate(
        CandidateEvidence(
            status=StrategyCandidateStatus.PAPER_PENDING,
            paper=evidence,
            thresholds=cfg,
        )
    )

    extra: dict[str, Any] = {
        "symbol": symbol,
        "account_id": str(account.pk) if account is not None else None,
    }
    if account_problem is not None:
        extra["account_problem"] = account_problem
    if conflict:
        # 拒绝理由**原文**落库（D20 的「依据」）：它是既有不变式的原话，抄一遍就会漂。
        extra["conflict"] = conflict

    step = _Step(
        StrategyCandidateLayer.PAPER,
        verdict,
        context=_pinned_context(fresh),
        extra_detail=extra,
    )
    if verdict.next_status is StrategyCandidateStatus.PAPER_RUNNING:
        session = deps.create_paper_session(fresh)
        step.paper_session = session
        step.layer_key = LAYER_PAPER
        step.layer_record = {
            "session_id": str(session.pk),
            "started_at": deps.now().isoformat(),
            "mode": "paper",
        }
    return step


def _paper_running_step(fresh: StrategyCandidate, deps: AdvanceDeps) -> _Step:
    """L3 观察期那一步：采证据 → 判定（#24，spec D8）。

    与另外两层同形：证据由 `deps.read_paper` 采（默认实现读时间线 / 订单 / 风控事件），
    交判定纯函数，结论落状态。`None` 表示**尚无结论**（会话行不见了），判定层据此
    停在 `paper_running`。

    ## 为什么 caveats 要单独递一次

    `PaperEvidence` 是判定的**输入**，而 caveats 是给人读的**说明文字**（判定一个字都
    不读它）。把说明文字塞进判定输入，会让「判定依赖什么」与「结论展示什么」混在同一个
    结构里，将来有人照着 `PaperEvidence` 加字段时说不清该加在哪一侧。所以采集侧把它
    暂存、这里取走并放进 `extra_detail`（转移记录与通知正文都读它）。

    取了就删（`take_paper_caveats` 是 pop）：它是「从采集点到编排点」的一次性传手，
    不是状态。

    ## 时钟只有一个

    `deps.now()` 既喂给这里采证据、也喂给 `advance` 写转移记录的 `at`。两处各读一次
    `timezone.now()` 也能跑，但那样「结论说 13.99 天」与「记录写在这一刻」可能差一个
    毫秒级的不同时刻，而边界用例正是要钉这两者的一致性。默认实现接得住这个参数；
    注入的 `read_paper` 若只接受一个位置参数，撤回老签名也不会炸——见下面的 `TypeError`
    兜底。
    """
    try:
        evidence = deps.read_paper(fresh, now=deps.now())
    except TypeError:
        # 注入的假实现可能只接受 `(candidate)`。退化成不传时钟——判定照样跑，
        # 只是那个假实现自己决定怎么算时长（它本来就是假的，不该被签名卡住）。
        evidence = deps.read_paper(fresh)
    caveats = _take_paper_caveats(fresh)
    extra: dict[str, Any] = {}
    if caveats:
        # 交付 7：口径边界必须随结论透出，不得被读成完整盈亏。
        extra["caveats"] = caveats
    return _Step(
        StrategyCandidateLayer.PAPER,
        evaluate_candidate(
            CandidateEvidence(
                status=StrategyCandidateStatus.PAPER_RUNNING,
                paper=evidence,
                thresholds=deps.config,
            )
        ),
        extra_detail=extra,
    )


def _take_paper_caveats(fresh: StrategyCandidate) -> list[str]:
    """取走采集侧为这个候选暂存的 caveats（默认实现才有；注入的 `read_paper` 不必提供）。"""
    from apps.strategy_factory.effects import take_paper_caveats

    return take_paper_caveats(fresh)


def _paper_account_problem(account: Any) -> str | None:
    """`paper_account` 能不能用；能用返回 None，否则返回给人看的原因。

    `account_ready` 的口径是「**已指定且可用**」，不是「非空」：`LiveSession` 的不变式
    是 `paper ⇒ testnet=True`（`mode_account_mismatch`），一个绑了实盘账户的候选若被
    当成「就绪」，下一步就会撞在不变式上抛接线错误——而正确的落点是**停在
    `paper_pending` 等人工改绑**，并告诉他改什么。
    """
    if account is None:
        return "未指定 paper 账户"
    if not account.testnet:
        return (
            f"账户「{account.label}」不是 testnet（testnet=False）——"
            "paper 会话必须绑模拟账户（LiveSession.mode_account_mismatch 不变式）"
        )
    if not account.is_active:
        return f"账户「{account.label}」已停用（is_active=False）"
    return None


# --------------------------------------------------------------------------- #
# 证据的读写（`evidence` JSON 的访问口）
# --------------------------------------------------------------------------- #


def _layers(fresh: StrategyCandidate) -> dict[str, Any]:
    layers = (fresh.evidence or {}).get(LAYERS_KEY)
    return dict(layers) if isinstance(layers, dict) else {}


def _layer_value(fresh: StrategyCandidate, layer: str, key: str) -> Any:
    record = _layers(fresh).get(layer)
    if isinstance(record, dict):
        return record.get(key)
    return None


def _pinned_context(fresh: StrategyCandidate) -> RunContext | None:
    payload = (fresh.evidence or {}).get(RUN_CONTEXT_KEY)
    if isinstance(payload, dict) and payload:
        return RunContext.from_payload(payload)
    return None


def _resolve_context(fresh: StrategyCandidate, deps: AdvanceDeps) -> RunContext:
    ctx = _pinned_context(fresh)
    if ctx is not None:
        return ctx
    ctx = deps.resolve_run_context(fresh)
    if ctx is None:
        raise MissingRunContextError(
            f"候选 {fresh.pk}（{fresh.name}）没有跑批判据：标的 / 周期 / 评估窗口 / 初始"
            "资金都没有落点。触发方需在 deps.resolve_run_context 里给出（或在登记时写进"
            f" source_ref['evaluation']）——{_RUN_CONTEXT_HINT}"
        )
    return ctx


_RUN_CONTEXT_HINT = (
    "需要的键：symbol / timeframe / evaluation_start / evaluation_end / initial_capital"
    "（可选 parameters / exchange / is_perp / include_funding）"
)


def _resolve_symbol(fresh: StrategyCandidate, deps: AdvanceDeps) -> str:
    """paper 层只需要标的，不需要整段窗口。

    三级回退，全部**显式**：钉住的跑批判据 → 注入的跑批判据 → 样本内行的标的。
    都拿不到才 Fail Loud。中间不猜（猜出来的标的会去和另一个品种的活跃会话比冲突，
    冲突检查于是悄悄失效）。
    """
    ctx = _pinned_context(fresh)
    if ctx is not None:
        return ctx.symbol
    ctx = deps.resolve_run_context(fresh)
    if ctx is not None:
        return ctx.symbol
    result_id = _layer_value(fresh, LAYER_IN_SAMPLE, "result_id")
    if result_id:
        from apps.backtest.models import BacktestResult

        symbol = (
            BacktestResult.objects.filter(id=result_id)
            .values_list("symbol", flat=True)
            .first()
        )
        if symbol:
            return str(symbol)
    raise MissingRunContextError(
        f"候选 {fresh.pk}（{fresh.name}）无法确定标的：没有跑批判据，也读不到样本内行"
        f"的标的（result_id={result_id!r}）。同账户同品种的串行判定（D9）没有标的就"
        "无从做起"
    )


def _detail_payload(step: _Step) -> dict[str, Any]:
    """转移记录的 `detail`：判定依据 + 这一步的动作事实。"""
    detail: dict[str, Any] = dict(step.verdict.detail)
    detail["next_status"] = step.verdict.next_status.value
    if step.started_run:
        detail["started_run"] = True
    if step.layer_record is not None and step.layer_key is not None:
        detail[step.layer_key] = _public_layer(step.layer_record)
    detail.update(step.extra_detail)
    return detail


def _public_layer(record: Mapping[str, Any]) -> dict[str, Any]:
    """层记录进 `detail` 时只带可读的那几个键（不把整份 start 返回值抄第二遍）。"""
    return {
        key: record[key]
        for key in ("result_id", "session_id", "started_at", "in_sample_end", "error")
        if record.get(key) is not None
    }


def _public_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """起跑返回值进 `detail` 时只留结论性字段：整份 stats 含几千点权益曲线。"""
    keep = (
        "result_id",
        "error",
        "decay_pct",
        "oos_sharpe",
        "is_sharpe",
        "fold_count",
        "expected_fold_count",
        "data_sufficient",
        "insufficient_reason",
        "sharpe_ratio",
        "total_trades",
    )
    return {key: payload[key] for key in keep if key in payload}


def _public_start(started: Mapping[str, Any]) -> dict[str, Any]:
    return _public_payload(started)


def _as_str(value: Any) -> str | None:
    return None if value is None else str(value)


#: 每一层的「这次没成」落点（D3）。它们都留了回边（改参数重跑），所以到达它们时
#: 该层的起跑记录必须作废。
_FAILED_STATUSES = frozenset(
    {
        StrategyCandidateStatus.IN_SAMPLE_FAILED,
        StrategyCandidateStatus.OOS_FAILED,
        StrategyCandidateStatus.PAPER_FAILED,
    }
)


def _run_is_spent(
    layer: StrategyCandidateLayer, next_status: StrategyCandidateStatus
) -> bool:
    """这一步的结论是不是「该层这次起跑已经花掉」（落 `*_failed`）。"""
    return next_status in _FAILED_STATUSES and _layer_key_of(layer) is not None


def _layer_key_of(layer: StrategyCandidateLayer) -> str | None:
    """层 → `evidence[LAYERS_KEY]` 里的键（`pipeline` 不属于任何单层）。"""
    return {
        StrategyCandidateLayer.IN_SAMPLE: LAYER_IN_SAMPLE,
        StrategyCandidateLayer.OOS: LAYER_OOS,
        StrategyCandidateLayer.PAPER: LAYER_PAPER,
    }.get(layer)


def _layer_of(status: StrategyCandidateStatus) -> StrategyCandidateLayer:
    """状态 → 层（只用于「等谁动」的那些状态的归属列）。"""
    if status in (
        StrategyCandidateStatus.IN_SAMPLE_RUNNING,
        StrategyCandidateStatus.IN_SAMPLE_FAILED,
    ):
        return StrategyCandidateLayer.IN_SAMPLE
    if status in (
        StrategyCandidateStatus.OOS_RUNNING,
        StrategyCandidateStatus.OOS_FAILED,
    ):
        return StrategyCandidateLayer.OOS
    if status in (
        StrategyCandidateStatus.PAPER_RUNNING,
        StrategyCandidateStatus.PAPER_FAILED,
    ):
        return StrategyCandidateLayer.PAPER
    return StrategyCandidateLayer.PIPELINE


# --------------------------------------------------------------------------- #
# 审计与通知
# --------------------------------------------------------------------------- #


def _is_repeat(
    fresh: StrategyCandidate,
    from_status: StrategyCandidateStatus,
    verdict: Verdict,
) -> bool:
    """这一条结论是不是**紧邻的上一条**的同一条（同起止状态 + 同原因码）。

    轮询会把「等账户」「排队中」「观察未满」这些停住态一遍遍扫到。每次落一行、发一条
    通知，审计面与收件箱就被噪音淹掉，真正要看的转移反而找不出来。所以：**同一条结论
    连续出现时不重复记录、不重复通知**；一旦结论变了（比如账户指定好了、开始观察），
    再记一行、再发一条。
    """
    last = (
        StrategyCandidateTransition.objects.filter(candidate=fresh)
        .order_by("-at", "-created_at", "-id")
        .values("from_status", "to_status", "reason_code")
        .first()
    )
    if last is None:
        return False
    return (
        last["from_status"] == from_status.value
        and last["to_status"] == verdict.next_status.value
        and last["reason_code"] == verdict.reason_code.value
    )


#: 「卡住也必须可见」的结论（D20）：它们停在原地不改变状态，但**需要有人动**或者
#: **正在等外部条件**，静默下去就是「候选凭空消失」。
_STUCK_REASONS = frozenset({ReasonCode.PAPER_ACCOUNT_MISSING, ReasonCode.PAPER_QUEUED})


def _should_notify(from_status: StrategyCandidateStatus, verdict: Verdict) -> bool:
    """要不要为这条结论通知提出人。

    两种情形发：**状态真的动了**（一个结论），或**卡在了需要人/等外部条件的地方**
    （D20 明写：停在 `paper_pending` 也必须可见）。中间那几种「还在跑、别管」的
    停住态（样本内回测尚无结果、分折尚无结论、paper 观察未满）不发——它们不是结论，
    发了就是催人去看一件他插不上手的事。
    """
    if verdict.next_status is not from_status:
        return True
    return verdict.reason_code in _STUCK_REASONS


#: 通知正文里按这个顺序列依据数字（只列结论里真有的），顺序固定 = 消息可比对。
_DETAIL_ORDER = (
    "sharpe_ratio",
    "sharpe_min",
    "total_trades",
    "min_trades",
    "decay_pct",
    "oos_decay_max_pct",
    "fold_count",
    "closed_trades",
    "return_pct",
    "risk_hits",
    "wall_clock_days",
    "running_days",
    "paper_observation_days",
    "insufficient_reason",
    "error",
    "account_problem",
    "conflict",
)

#: 结论 → 「下一步谁做什么」。写在这里而不是让提出人自己猜，是 D20 对可见性的要求：
#: 卡住的通知必须说清该谁动（等账户 = 你动；排队中 = 等）。
_NEXT_STEP = {
    ReasonCode.IN_SAMPLE_RUN_STARTED: "正在跑样本内回测，跑完自动进入下一层，无需你操作。",
    ReasonCode.IN_SAMPLE_PENDING: "样本内回测尚未产出结果，跑完自动进入下一层。",
    ReasonCode.IN_SAMPLE_PASSED: "样本内通过，正在跑样本外分折（walk-forward），无需你操作。",
    ReasonCode.IN_SAMPLE_SHARPE_BELOW_MIN: "夏普不达标：改参数后重新推进（in_sample_failed → in_sample_running）。",
    ReasonCode.IN_SAMPLE_BACKTEST_FAILED: "回测失败：先看依据里的 error，修好后再重新推进。",
    ReasonCode.OOS_PENDING: "样本外分折尚未产出结论，跑完自动进入下一层。",
    ReasonCode.OOS_PASSED: "样本外通过，正在等 paper 账户 / 排队。",
    ReasonCode.OOS_DECAY_ABOVE_MAX: "样本外衰减超标：改参数或分折配置后重跑（oos_failed → oos_running）。",
    ReasonCode.OOS_BACKTEST_FAILED: "分折失败：先看依据里的 error，修好后再重跑。",
    ReasonCode.PAPER_ACCOUNT_MISSING: "**需要你**：给这条候选指定一个 testnet 账户，下一轮会自动前进。",
    ReasonCode.PAPER_QUEUED: "同账户同品种已有活跃会话，排队中：那条会话结束后下一轮会自动前进。",
    ReasonCode.PAPER_OBSERVING: "观察期未满（墙钟与 running 两个数都要求达标），下一轮自动重判，无需你操作。",
    ReasonCode.PAPER_OBSERVATION_INCOMPLETE: "paper 会话在观察期满前结束了：这条候选判未过。要再试就重新观察（paper_failed → paper_running）。",
    ReasonCode.PAPER_RETURN_BELOW_MIN: "paper 期盈亏不达标：改参数后重新走漏斗。",
    ReasonCode.PAPER_RISK_HITS_ABOVE_MAX: "paper 期风控命中超过上限：这条策略在模拟期被风控拦过，改参数后重新走漏斗。",
    ReasonCode.PAPER_PASSED: "**需要你**：这条候选跑满观察期且判定通过，等你确认是否升实盘（人工卡点）。",
    ReasonCode.INSUFFICIENT_EVIDENCE: "证据不足，判不了（不放行）：补数据或改窗口后重新推进。",
}


def _message(
    fresh: StrategyCandidate,
    from_status: StrategyCandidateStatus,
    step: _Step,
    cfg: FactoryConfig,
) -> str:
    """给提出人看的一条通知（D20）。"""
    verdict = step.verdict
    lines = [
        f"【候选流水线】{fresh.name}",
        f"{from_status.display} → {verdict.next_status.display}"
        + ("" if verdict.next_status is not from_status else "（停住）"),
        f"结论：{verdict.reason_code.display}",
    ]
    facts = _detail_payload(step)
    shown = [f"{key}={_fmt(facts[key])}" for key in _DETAIL_ORDER if key in facts]
    if step.paper_session is not None:
        shown.append(f"paper 会话={str(step.paper_session.pk)[:8]}…")
    if shown:
        lines.append("依据：" + "｜".join(shown))
    if verdict.reason_code is ReasonCode.PAPER_OBSERVATION_STARTED:
        lines.append(
            f"下一步：观察期 {cfg.paper_observation_days} 天（只计 running），"
            "期满后自动判定，无需你操作。"
        )
    elif verdict.reason_code in _NEXT_STEP:
        lines.append(f"下一步：{_NEXT_STEP[verdict.reason_code]}")
    # 口径边界随结论一起透出（交付 7）：这里的数字**不是**完整盈亏，少这一句它就会被
    # 当成完整盈亏读。只在真有 caveats 时列（样本内那几层没有）。
    caveats = facts.get("caveats")
    if isinstance(caveats, list) and caveats:
        lines.append("口径说明：")
        lines.extend(f"- {c}" for c in caveats)
    return "\n".join(lines)


def _fmt(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.4g}"
    return str(value)


def _default_deps() -> AdvanceDeps:
    """生产默认接线（`effects.py`）。

    在这里 import 而不是在模块顶部：`effects` 要 import 本模块的 `RunContext` /
    `AdvanceDeps`，顶部互相 import 会成环。
    """
    from apps.strategy_factory.effects import default_deps

    return default_deps()


def pinned_run_context(candidate: StrategyCandidate) -> RunContext | None:
    """读候选上钉住的跑批判据（`effects` 与 API 层都要，所以是公开的）。"""
    return _pinned_context(candidate)


def with_config(deps: AdvanceDeps, config: FactoryConfig) -> AdvanceDeps:
    """派生一份换掉阈值/总开关的 deps（测试与「单次重跑」用，不动全局单例）。"""
    return replace(deps, config=config)


def _default_now() -> datetime:
    return timezone.now()
