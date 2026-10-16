"""漏斗的生产接线（spec #19 D14 的 `deps` 默认实现，子票 #22）。

`orchestrator.advance()` 只认 `AdvanceDeps` 里那八个成员，本模块把它们接到**既有的缝**
上——不新增第二个投递机制、不新增第二个回测入口、不新增第二种会话创建方式：

| 成员 | 接到 |
|---|---|
| `resolve_run_context` | 候选上钉住的 `evidence["run_context"]` → `source_ref["evaluation"]` |
| `start_in_sample` | `create_empty_result`（占位行）+ 既有取数 `_load_ohlcv_for_backtest` + `StrategyRunner.run_backtest` |
| `read_in_sample` | 该占位行的 `BacktestResult` 行 |
| `start_oos` | `walk_forward_runner.run_walk_forward`（#29 的既有入口） |
| `read_oos` | 同一行的 `metrics["walk_forward"]` 载荷 |
| `create_paper_session` | `LiveSession.objects.create` + `conflicting_active_session` + 账户余额 |
| `notify` | `apps.trading.alerts.notify_user`（本仓库唯一的用户通知口） |

## 三条纪律

1. **样本内那一次是同步的**。形状照抄 `walk_forward_runner._default_fold_executor`：
   取数是同步的、回测是 async，所以自建事件循环驱动它。这样 `start_in_sample` 返回时
   结果已经落库，`read_in_sample` 读到的就是**结论**而不是「在飞」——默认路径上不存在
   「任务死了候选还挂在 `in_sample_running`」这种静默卡死。异步接线（占位行 + celery
   任务、D18 的 `celery_task_id` 反查模式）只需替换 `start_in_sample` / `read_in_sample`
   两个成员，编排层一行不用改。

2. **样本内只跑 `[evaluation_start, in_sample_end)`**（#29 的交接）。`in_sample_end`
   由编排层从 `plan_folds(...)` 算好传进来，这里不再自己推几何：主行的 `sharpe_ratio`
   是衰减的**分母**，让它覆盖到样本外那一段，衰减会被系统性低估（实测真值 30% 会算成
   25.5% 从而错误放行），而 `run_walk_forward` 的 `_validate_wiring` 会当场拒绝。

3. **paper 会话的 `config` 严格继承候选主行那一份 `parameters`**（用户故事 19）。
   2026-10 那次 BNB/USDT 事故就是「回测验过的参数」与「会话在跑的参数」静默分叉：
   回测用 `exit_at_mid=false` 通过、会话却是 `{}`、实盘按默认 `true` 跑了一周。所以这里
   只从主行取，不从别处补——要找「没继承到的键」，只能去主行找。

## 一处刻意的边界

paper 会话按既有 `live_session_create` 的形状建为 `status="pending"`：**启动**（接上
runner、开始累计 `running` 观察时长）走既有的 `live_session_start` 入口，不是本票的
能力。创建与启动分开是现状，也是 `LiveSession.ACTIVE_STATUSES` 与
`CONFLICT_STATUSES` 那一对区分的由来（`pending` 还没 runner、不占持仓）。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date
from decimal import Decimal
from typing import Any, Mapping

from django.utils import timezone

from apps.strategy_factory.config import CONFIG, FactoryConfig
from apps.strategy_factory.evaluate import (
    InSampleEvidence,
    OosEvidence,
    PaperEvidence,
)
from apps.strategy_factory.models import StrategyCandidate
from apps.strategy_factory.orchestrator import (
    LAYER_IN_SAMPLE,
    LAYER_OOS,
    LAYERS_KEY,
    AdvanceDeps,
    AdvanceWiringError,
    RunContext,
)
from apps.strategy_factory.walk_forward import WALK_FORWARD_METRICS_KEY, FoldPlan

logger = logging.getLogger(__name__)

#: 跑批判据在 `source_ref` 里的键：登记方（agent 工具 / API）可以把一次评估的窗口
#: 与标的写在这里，`advance` 第一次用到时把它**钉进 `evidence`**，此后不再回读。
SOURCE_REF_EVALUATION_KEY = "evaluation"


# --------------------------------------------------------------------------- #
# 跑批判据
# --------------------------------------------------------------------------- #


def resolve_run_context(candidate: StrategyCandidate) -> RunContext | None:
    """候选的跑批判据：钉住的那份优先，其次登记时写进 `source_ref` 的那份。

    两处都没有就返回 None，由编排层 Fail Loud（`MissingRunContextError`）——「没人告诉
    流水线该跑什么」不是「这条策略不行」，不能被落成一次 `*_failed` 判定。
    """
    from apps.strategy_factory.orchestrator import pinned_run_context

    pinned = pinned_run_context(candidate)
    if pinned is not None:
        return pinned

    payload = (candidate.source_ref or {}).get(SOURCE_REF_EVALUATION_KEY)
    if isinstance(payload, Mapping) and payload:
        return RunContext.from_payload(payload)
    if payload is not None and not isinstance(payload, Mapping):
        raise AdvanceWiringError(
            f"候选 {candidate.pk} 的 source_ref[{SOURCE_REF_EVALUATION_KEY!r}] 必须是对象，"
            f"当前 {type(payload).__name__}"
        )
    return None


# --------------------------------------------------------------------------- #
# L1：样本内
# --------------------------------------------------------------------------- #


def start_in_sample(
    candidate: StrategyCandidate, context: RunContext, plan: FoldPlan
) -> Mapping[str, Any]:
    """起跑样本内回测，**同步跑完**（见模块 docstring 第 1 条）。

    先建占位行再跑（D18 的既有模式）：占位行的 id 就是这一层的身份，编排层把它写进
    层记录，于是「同一层重复触发」有据可依（`create_empty_result` 的既有语义与
    `run_backtest_task` 的 `result_id` 参数完全一致）。
    """
    from apps.backtest.models import BacktestResult
    from apps.strategy_engine.backtest_mode import create_empty_result

    if candidate.strategy_id is None:
        raise AdvanceWiringError(
            f"候选 {candidate.pk}（{candidate.name}）没有关联策略行（strategy 为空）——"
            "回测结果必须挂在一条策略上（BacktestResult.strategy 非空）"
        )

    # 样本内只跑 [evaluation_start, in_sample_end)：多跑一段（含样本外）会把衰减的
    # 分母拉向样本外，见模块 docstring 第 2 条。
    start_date = context.evaluation_start.isoformat()
    end_date = plan.in_sample_end.isoformat()
    result_id = create_empty_result(
        strategy_id=str(candidate.strategy_id),
        symbol=context.symbol,
        timeframe=context.timeframe,
        start_date=start_date,
        end_date=end_date,
        initial_capital=context.initial_capital,
        parameters=dict(context.parameters or {}),
        user_id=str(candidate.proposed_by_id) if candidate.proposed_by_id else None,
    )
    if not result_id:
        raise AdvanceWiringError(
            f"候选 {candidate.pk} 的样本内占位行没能建出来（create_empty_result 返回空）"
        )
    # 分组键（D4/D13）：样本内行也带上候选 id，事后「这条候选依据哪几次回测」才答得出；
    # 它同时是 `clear_fold_results` 的排除对象（`keep_result_id`），不会被分折清理删掉。
    BacktestResult.objects.filter(id=result_id).update(candidate_id=str(candidate.pk))

    try:
        stats = _run_single_window(
            candidate=candidate,
            context=context,
            start_date=start_date,
            end_date=end_date,
            result_id=result_id,
        )
    except Exception as exc:  # noqa: BLE001 — 回测抛错是**结论**（`error`），不是崩溃
        logger.warning(
            "[StrategyFactory] 候选 %s 样本内回测抛错", candidate.pk, exc_info=True
        )
        return {"result_id": str(result_id), "error": str(exc)}

    if stats.get("error"):
        return {"result_id": str(result_id), "error": stats["error"]}
    return {"result_id": str(result_id), **stats}


def _run_single_window(
    *,
    candidate: StrategyCandidate,
    context: RunContext,
    start_date: str,
    end_date: str,
    result_id: str,
) -> Mapping[str, Any]:
    """跑一次单窗口回测并把结果写进 `result_id`（既有的同步外壳形状）。

    与 `run_backtest_task` / `_default_fold_executor` 同形：同步取数（它内部自己
    `asyncio.run`，必须在**没有**运行中事件循环的上下文里调）→ 自建事件循环驱动 async
    回测 → 关掉循环与异步资源 → 补写 `data_source`。

    资金费率只在「永续 + 计入费率」时组装，与 `run_backtest_task` 同一条件、同一函数：
    少了它，样本内那一行就是「不含费用」的，而各折含费用，衰减会在两套成本之间比较。
    """
    from django.db import close_old_connections

    from apps.backtest.tasks import (
        _close_async_resources,
        _load_ohlcv_for_backtest,
        load_funding_rates,
    )
    from apps.strategy_engine.runner import StrategyRunner

    ohlcv_data, source_stats = _load_ohlcv_for_backtest(
        context.symbol,
        context.timeframe,
        start_date=start_date,
        end_date=end_date,
        exchange=context.exchange,
    )
    if not ohlcv_data:
        return {
            "error": (
                f"未能获取 {context.symbol} {context.timeframe} 在 {start_date}~{end_date} "
                "的K线数据"
            )
        }

    funding_rates = (
        load_funding_rates(context.symbol, ohlcv_data)
        if (context.is_perp and context.include_funding)
        else {}
    )

    runner = StrategyRunner()
    loop = asyncio.new_event_loop()
    try:
        close_old_connections()
        stats = loop.run_until_complete(
            runner.run_backtest(
                strategy_name=candidate.name,
                symbol=context.symbol,
                timeframe=context.timeframe,
                ohlcv_data=ohlcv_data,
                initial_capital=context.initial_capital,
                parameters=dict(context.parameters or {}),
                strategy_id=str(candidate.strategy_id),
                result_id=result_id,
                is_perp=context.is_perp,
                funding_rates=funding_rates,
            )
        )
    finally:
        _close_async_resources(loop)
        loop.close()

    persisted_id = stats.get("result_id") or result_id
    if persisted_id:
        close_old_connections()
        from apps.backtest.models import BacktestResult

        BacktestResult.objects.filter(id=persisted_id).update(data_source=source_stats)
    return {**stats, "result_id": str(persisted_id or result_id)}


def _layers_from_db(candidate: StrategyCandidate) -> dict[str, Any]:
    """从**库里**取该候选的层记录。

    刻意不读传进来那个对象的 `evidence`：调用方手里那一份可能是过期的（`advance` 里
    拿到的是行锁内重新读的那一份，但别人——比如运维脚本、集成用例——手里的不是）。
    读一份过期的 `evidence` 会表现为「层记录不存在 → 这次起跑没产出」这种**假失败**，
    而它的根因（对象过期）离现场很远。一次按主键的取值查询换掉这个陷阱，值。
    """
    from apps.strategy_factory.models import StrategyCandidate as _Candidate

    evidence = (
        _Candidate.objects.filter(pk=candidate.pk)
        .values_list("evidence", flat=True)
        .first()
    )
    layers = (evidence or {}).get(LAYERS_KEY)
    return dict(layers) if isinstance(layers, dict) else {}


def read_in_sample(candidate: StrategyCandidate) -> InSampleEvidence | None:
    """读样本内那一层的结论。

    判据是**行里有没有引擎写进去的东西**（`equity_curve` 非空）：占位行是
    `create_empty_result` 建出来的（`final_capital=initial_capital`、
    `total_return_pct=0.0`、`equity_curve=[]`），它与「跑完了但没成交」在
    `total_trades` 上看不出区别，只有权益曲线能分开——而「跑完了没成交」必须能被判成
    `insufficient_evidence`，不能被当成「还没跑完」一直等。

    行不见了、或占位行**在同步起跑之后**仍然没有引擎产物，都判「这次起跑没产出」并把
    它落成 `in_sample_failed + in_sample_backtest_failed`——**失败可见**比挂在
    `in_sample_running` 上等一个不会来的结果好（后者无法归因，也无法重跑）。
    异步接线（只派发、不等）要自己实现本成员：在飞期间返回 `None`，判定死亡时返回
    带 `error` 的证据。
    """
    from apps.backtest.models import BacktestResult

    record = _layers_from_db(candidate).get(LAYER_IN_SAMPLE) or {}
    result_id = record.get("result_id") if isinstance(record, Mapping) else None
    reported = record.get("error") if isinstance(record, Mapping) else None
    if not result_id:
        return InSampleEvidence(
            error=f"样本内回测没有产出结果行（起跑记录：{reported or '无'}）"
        )

    row = BacktestResult.objects.filter(id=result_id).first()
    if row is None:
        return InSampleEvidence(
            error=f"样本内回测行 {result_id} 不存在（可能已被删除）"
        )
    if not row.equity_curve:
        return InSampleEvidence(
            error=(
                f"样本内回测行 {result_id} 没有引擎产物（权益曲线为空）"
                f"（起跑记录：{reported or '无'}）"
            )
        )
    return InSampleEvidence(
        sharpe_ratio=row.sharpe_ratio,
        total_trades=row.total_trades,
    )


# --------------------------------------------------------------------------- #
# L2：样本外分折
# --------------------------------------------------------------------------- #


def start_oos(
    candidate: StrategyCandidate,
    context: RunContext,
    plan: FoldPlan,
    in_sample_result_id: str,
) -> Mapping[str, Any]:
    """跑样本外分折（#29 的 `run_walk_forward`，同步外壳）。

    几何**由编排层算好传进来**（`fold_plan=plan`），不在这里重算：两处各算一次就会在
    有人换了分折配置时悄悄分家，而分家之后照样跑得出一个像模像样的 `decay_pct`。
    """
    from apps.strategy_factory.walk_forward_runner import run_walk_forward

    return run_walk_forward(
        candidate_id=str(candidate.pk),
        strategy_name=candidate.name,
        symbol=context.symbol,
        timeframe=context.timeframe,
        in_sample_result_id=in_sample_result_id,
        evaluation_start=context.evaluation_start,
        evaluation_end=context.evaluation_end,
        strategy_id=str(candidate.strategy_id) if candidate.strategy_id else None,
        exchange=context.exchange,
        is_perp=context.is_perp,
        include_funding=context.include_funding,
        config=CONFIG,
        fold_plan=plan,
    )


def read_oos(candidate: StrategyCandidate) -> OosEvidence | None:
    """读样本外那一层的结论：候选主行的 `metrics["walk_forward"]` 载荷。

    **不解析别的东西**：那份载荷是 `walk_forward.py` 唯一的输出形状，`decay_pct` 只在
    `data_sufficient=True` 时存在（缺一折就没有衰减），所以这里如实转述，判「证据不足」
    由判定层做。

    层记录在、载荷却没有 = 这次分折没产出结论（同步起跑崩在中途、或有人把 `metrics`
    键删了）。与 `read_in_sample` 同一条纪律：落 `oos_failed + oos_backtest_failed`，
    失败可见、可重跑，而不是挂在 `oos_running` 上等。
    """
    from apps.backtest.models import BacktestResult

    layers = _layers_from_db(candidate)
    record = layers.get(LAYER_OOS) or {}
    result_id = (
        record.get("in_sample_result_id") if isinstance(record, Mapping) else None
    )
    if not result_id:
        result_id = (layers.get(LAYER_IN_SAMPLE) or {}).get("result_id")
    if not result_id:
        return OosEvidence(error="样本外分折没有可读的主行 id（层记录缺失）")

    row = BacktestResult.objects.filter(id=result_id).only("id", "metrics").first()
    if row is None:
        return OosEvidence(error=f"样本内主行 {result_id} 不存在，分折结论无从读起")

    payload = (row.metrics or {}).get(WALK_FORWARD_METRICS_KEY)
    if not isinstance(payload, Mapping):
        return OosEvidence(
            error=f"样本内主行 {result_id} 上没有 {WALK_FORWARD_METRICS_KEY} 载荷（分折没产出结论）"
        )
    return OosEvidence(
        decay_pct=payload.get("decay_pct"),
        fold_count=payload.get("fold_count"),
        data_sufficient=bool(payload.get("data_sufficient", False)),
    )


# --------------------------------------------------------------------------- #
# L3：自动建 paper 会话
# --------------------------------------------------------------------------- #


def create_paper_session(candidate: StrategyCandidate) -> Any:
    """建一条 `mode="paper"` 会话：参数继承主行、初始权益取绑定账户余额（D10 / 故事 19）。

    这里对既有不变式**逐条显式检查**，不靠「调用方已经查过了」：`LiveSession` 自己
    没有任何模型级约束（没有 `clean()`、没有 `save()` 覆盖、没有 `Meta.constraints`），
    直接 `objects.create` 会**静默绕过** `mode_account_mismatch` 与
    `conflicting_active_session` 这两道闸门——正是 2026-10-07 那次两条会话互相平仓的
    那类事故的入口。
    """
    from apps.backtest.models import BacktestResult
    from apps.trading.models import LiveSession

    record = _layers_from_db(candidate).get(LAYER_IN_SAMPLE) or {}
    result_id = record.get("result_id") if isinstance(record, Mapping) else None
    if not result_id:
        raise AdvanceWiringError(
            f"候选 {candidate.pk} 没有样本内行 id，无法建立 paper 会话（参数继承自它）"
        )
    main_row = BacktestResult.objects.filter(id=result_id).first()
    if main_row is None:
        raise AdvanceWiringError(f"候选 {candidate.pk} 的样本内行 {result_id} 不存在")
    if main_row.review_status != "approved":
        # 漏斗在样本内通过时已经写过 approved（`orchestrator._approve_in_sample_row`）。
        # 走到这里说明中间有人把它改了回去，或者这个候选是被人手工挪到 paper_pending 的。
        raise AdvanceWiringError(
            f"候选 {candidate.pk} 的样本内行 {result_id} 的 review_status="
            f"{main_row.review_status!r}（不是 approved）——不允许凭一条未通过审核的回测建会话"
        )

    account = candidate.paper_account
    if account is None:
        raise AdvanceWiringError(f"候选 {candidate.pk} 没有指定 paper_account")
    if not account.testnet:
        raise AdvanceWiringError(
            f"候选 {candidate.pk} 绑的账户「{account.label}」testnet=False，"
            "而 paper 会话必须绑 testnet=True 的账户（mode_account_mismatch 不变式）"
        )
    if not account.is_active:
        raise AdvanceWiringError(
            f"候选 {candidate.pk} 绑的账户「{account.label}」已停用（is_active=False）"
        )

    conflict = LiveSession.conflicting_active_session(account, main_row.symbol)
    if conflict:
        # 既有不变式，**不放宽**（D9）：直接建会话会让 runner 在同账户同品种上互相平仓。
        raise AdvanceWiringError(
            f"候选 {candidate.pk} 建 paper 会话前发现同账户同品种冲突：{conflict}"
        )

    user_id = candidate.proposed_by_id or main_row.user_id
    if not user_id:
        raise AdvanceWiringError(
            f"候选 {candidate.pk} 与它的样本内行都没有归属用户，而 LiveSession.user 非空"
            "——没有接收人的会话连告警都发不出去"
        )

    initial_capital = fetch_account_balance(account)

    return LiveSession.objects.create(
        user_id=user_id,
        backtest_result=main_row,
        strategy=main_row.strategy,
        symbol=main_row.symbol,
        mode="paper",
        # 与既有 `live_session_create` 同形：建出来是 pending，**启动**走既有的
        # `live_session_start`（模块 docstring 末节）。
        status="pending",
        exchange_account=account,
        initial_capital=initial_capital,
        current_equity=initial_capital,
        # 严格继承主行那一份（故事 19）：一个键都不从别处补，避免 2026-10 那类静默漂移。
        config=dict(main_row.parameters or {}),
    )


def fetch_account_balance(account: Any) -> Decimal:
    """绑定账户的实时 USDT 余额（`testnet` 由适配器自己带上，所以 paper 读的是模拟盘）。

    用 `apps.trading.pending_reconcile.build_account_adapter` 而不是 views 里那个
    `_fetch_usdt_balance`：前者是**跨模块共用**的那一份（`daily_snapshot` 也在用），
    而后者是视图模块的私有函数，从这里伸手进去会把视图变成依赖。
    """
    from asgiref.sync import async_to_sync

    return async_to_sync(_fetch_usdt_balance)(account)


async def _fetch_usdt_balance(account: Any) -> Decimal:
    from apps.trading.pending_reconcile import build_account_adapter

    adapter = await build_account_adapter(account)
    if adapter is None:
        raise AdvanceWiringError(
            f"账户「{getattr(account, 'label', '')}」（{account.exchange}）取不到适配器，"
            "无法读取余额作为初始权益"
        )
    try:
        balance = await adapter.get_balance()
    finally:
        try:
            await adapter.disconnect()
        except Exception:  # noqa: BLE001 — 断开失败不该盖掉余额读取的结果
            logger.warning("[StrategyFactory] 断开适配器失败", exc_info=True)
    return Decimal(str((balance or {}).get("USDT", 0) or 0))


# --------------------------------------------------------------------------- #
# L3：paper 观察期判定所需的证据（#24）
# --------------------------------------------------------------------------- #

#: 风控命中计数的口径说明。**必须随结论一起透出**（交付 7 的同一条纪律）：
#: 它讲的是这个计数**覆盖了什么**，而这件事只在产生它的这一处知道。
RISK_HIT_CAVEAT = (
    "风控命中只计额度类拒单（RiskEvent.type=hard_limit，杠杆/单仓/总敞口）："
    "浮亏预警是账户级循环、手里没有会话，归集不到候选，未纳入计数。"
)

#: 观察期口径说明：两个数都要够，且 paused 不计入。
OBSERVATION_CAVEAT = (
    "观察期要求墙钟与 running 时长**同时**达标；paused（暂停止损/止盈）不计入 running。"
)

#: 一天 = 这个秒数。天数换算只用它，不在两处各写一次 86400。
_SECONDS_PER_DAY = 86400.0


def _days(delta: Any) -> float | None:
    """`timedelta` → 天数（float）；`None` 原样返回（「算不出来」不是 0）。"""
    if delta is None:
        return None
    return delta.total_seconds() / _SECONDS_PER_DAY


def read_paper(
    candidate: StrategyCandidate, *, now: Any | None = None
) -> PaperEvidence | None:
    """采 L3 paper 那一层的证据（spec #19 D8，子票 #24 的核心）。

    `None` 的语义与另外两层一致：**尚无结论**（还没建会话 / 会话行不见了），
    判定层据此停在 `paper_running` 等下一轮——不是「判为不合格」。

    ## 四个数的来源，每个都只有一处

    | 数 | 来源 | 为什么是它 |
    |---|---|---|
    | `wall_clock_days` | `started_at → now` | D8 原文；`started_at` 为空则算不出来 |
    | `running_days` | `LiveSessionStatusChange` 时间线逐段求和 | 见下 |
    | `closed_trades` / `return_pct` | **复用** `compute_session_stats(pnls)` | D7 明令「不重写一份统计」 |
    | `risk_hits` | `RiskEvent.objects.filter(live_session=..., type="hard_limit")` | D7 的可空外键 |

    ## `running_days` 为什么必须走时间线

    `LiveSession` 上只有一个 `status` 标量加两个时刻，**答不出「running 累计多久」**：
    一条 7 天跑 → 3 天暂停 → 7 天跑的会话，墙钟 17 天而其中 3 天不该算，库里原本
    没有任何东西记得那 3 天。时间线（#24 新建）让这个数第一次成为真值。它返回
    `None` 表示「时间线缺失、算不出来」，判定层会判**证据不足**（不放行）——而不是
    当成 0 天（那会让一条其实跑够了的会话永远等下去）。

    ## `return_pct` 怎么从 `compute_session_stats` 出来

    `compute_session_stats` 给的是**金额**（`realized_pnl_total`），而判定要的是
    **百分比**（`paper_min_return_pct`）。分母取 `initial_capital`：那是这次 paper
    观察的起点权益，也正是「这条策略这一段赚了百分之几」的分母。`initial_capital`
    为 0 或空 → 百分比算不出来 → 返回 `None`（判「证据不足」），**不做除零**。

    ## caveats 随结论一起走（交付 7）

    `compute_session_stats` 自带的 `caveats`（不含未实现部分、不含熔断减仓）原样带出，
    本层再加两条自己的（风控命中口径、观察期口径）。它们不参与判定，只随结论透出——
    少一句，「不含未实现部分」的数字就会被读成完整盈亏。
    """
    from apps.riskguard.models import RiskEvent
    from apps.trading.models import running_duration
    from apps.trading.session_stats import compute_session_stats

    session = candidate.paper_session
    if session is None:
        return None

    # `now` 可注入（与 `AdvanceDeps.now` 同一条纪律）：观察期的两个数都是「相对现在」
    # 的量，不给注入点就没法钉「恰 14 天」这条边界——用例只能靠把时间线造在真实的
    # now 之前若干天，而那样的断言会随着运行时刻漂移。生产传 None，走真实时钟。
    now = now if now is not None else timezone.now()

    # --- 观察期：两个数都要 --- #
    wall_clock_days = (
        _days(now - session.started_at) if session.started_at is not None else None
    )
    running_days = _days(running_duration(session, until=now))

    # --- 盈亏：复用既有纯函数（D7「不重写一份统计」） --- #
    from apps.trading.models import Order

    pnls = Order.objects.filter(
        live_session=session, realized_pnl__isnull=False
    ).values_list("realized_pnl", flat=True)
    stats = compute_session_stats(pnls)

    closed_trades = stats["closed_orders"]
    capital = session.initial_capital
    if capital:
        return_pct = float(
            Decimal(stats["realized_pnl_total"]) / Decimal(capital) * Decimal("100")
        )
    else:
        # 分母不可用 → 百分比没有意义。给 None 让判定判「证据不足」，
        # 而不是给一个 0.0（那是一条**看起来合规**的「没赚钱」，会直接把候选否掉）。
        return_pct = None

    # --- 风控命中：按 D7 的可空外键归集，只算额度类拒单 --- #
    risk_hits = RiskEvent.objects.filter(
        live_session=session, event_type="hard_limit"
    ).count()

    #: 会话是否已结束。`stopped` / `error` 都不再产生成交，停在观察里等它自己变好
    #: 等于永远不判定——判定层靠这一位把「还在观察」与「没跑满就结束了」分开。
    session_ended = session.status in ("stopped", "error")

    evidence = PaperEvidence(
        wall_clock_days=wall_clock_days,
        running_days=running_days,
        closed_trades=closed_trades,
        return_pct=return_pct,
        risk_hits=risk_hits,
        session_ended=session_ended,
    )
    # caveats 不参与判定（PaperEvidence 是 frozen 且判定不读它），所以单独挂在
    # 调用方能取到的地方：编排层把它们写进 evidence 与通知正文。
    _PAPER_CAVEATS[id(candidate)] = list(stats.get("caveats", [])) + [
        RISK_HIT_CAVEAT,
        OBSERVATION_CAVEAT,
    ]
    return evidence


#: 上一次 `read_paper` 采到的 caveats（按候选对象 id）。**进程内、一次性**：
#: 它只是把「纯函数拿不到的说明文字」从采集点递到编排点的传手，不作为状态使用——
#: 编排层读完即取走，不会跨请求留存。这样 `PaperEvidence` 不必为了携带说明文字而
#: 长出一个判定不读的字段（那会让「判定输入」与「展示素材」混在一个结构里）。
_PAPER_CAVEATS: dict[int, list[str]] = {}


def take_paper_caveats(candidate: StrategyCandidate) -> list[str]:
    """取走上一次为 `candidate` 采到的 caveats（取走即删，避免无界增长）。"""
    return _PAPER_CAVEATS.pop(id(candidate), [])


# --------------------------------------------------------------------------- #
# 通知与时钟
# --------------------------------------------------------------------------- #


def notify(candidate: StrategyCandidate, message: str) -> bool:
    """通知候选提出人（复用唯一的通知口 `notify_user`）。

    `proposed_by` 为空时照旧调用它：那个函数对「没有接收人」的处置是记 ERROR 并返回
    False（「没有接收人就不算告警」），而不是假装送达——这里不替它决定。
    """
    from asgiref.sync import async_to_sync

    from apps.trading.alerts import notify_user

    return bool(async_to_sync(notify_user)(candidate.proposed_by_id, message))


def now() -> Any:
    """现时刻（UTC aware）。`advance` 的转移记录与层记录都读它。"""
    return timezone.now()


# --------------------------------------------------------------------------- #
# 组装
# --------------------------------------------------------------------------- #


def default_deps(config: FactoryConfig | None = None) -> AdvanceDeps:
    """生产默认接线；`config` 可注入（测试改阈值不动全局单例）。"""
    return AdvanceDeps(
        resolve_run_context=resolve_run_context,
        start_in_sample=start_in_sample,
        read_in_sample=read_in_sample,
        start_oos=start_oos,
        read_oos=read_oos,
        create_paper_session=create_paper_session,
        read_paper=read_paper,
        notify=notify,
        now=now,
        config=config or CONFIG,
    )


def date_or_none(value: Any) -> date | None:
    """宽松解析 ISO 日期（给外部触发方用）。"""
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None
