"""停用决策的取数与落库（第①段单元 7iii 的收尾）+ 人工豁免的读写（第③段 Q3）。

`deactivation.py` 是纯函数：给一份格子与一个阶段，算出该停谁。本模块是它的另一半——
四条输入各自从哪里来、算出来的结论怎么写进 `DeactivationDecision`。

## 四条输入

| 输入 | 出处 |
|------|------|
| 当前阶段 | `judgement.current_judgement()`（「生效中」的语义只有它一处定义） |
| 当前代格子 | `pool_rebuild.current_cells(generation)`（「当前」= 最近一代 **ready**，也是它定义的） |
| 在期豁免 | `DeactivationExemption`（「在期」要读库、要时钟，纯函数不该碰） |

「在期豁免」这一行的**读与写都在本模块**：读是 `in_force_exemptions`，写是末尾那一节
（`grant` / `revoke`）。人发出的恢复豁免只能由人写（Q7：「必须同时有写方」），推理路径
永远不自己发豁免给自己放行——那一节里没有任何函数会被 `run_deactivation` 调到，它的
两个入口（管理命令、`/regime exempt`）都只调那几个函数。两侧共用 `CLOSE_REASON_*` 与
`in_force_exemptions` 的「三条一起」口径，所以不存在两份定义。
| 被管策略集合 | `Strategy` × `StrategyRegistry` × `LiveSession`，见下 |

## 被管策略集合：两条判据在这里的取数

判据一（能在注册表解析到实现类）**复用池化那条路**（`pool_rebuild.strategy_raw_texts`），
而不是另写一遍 `list_registered()` 成员判断：「解析得到实现类」在池化里已经有一个定义
（原型归属也依赖它），第二处定义迟早会漂移。代价是这一层与池化绑在一起——但被管策略
集合本来就是池化表的读法：判据一筛掉的那些正是回测自动建出来的幽灵策略行，它们在池化
里落在「未归类」。

判据二（被至少一个**活跃实盘**会话引用）**只标记，不筛选**——理由见 `deactivation`
的模块 docstring。所以这里读的是一个 `set`，交给推导去标。

`mode` 只认 `live`：判据二存在的意义是让第③段的 gate 知道该去停谁，而模拟盘不持仓真钱、
不会被停。`status` 用 `LiveSession.ACTIVE_STATUSES` 这个常量（模型上的那一份），不在这里
手抄状态字面量。

## 落库：`last_confirmed_at` 往前走，`evidence` 不动

`DeactivationDecision` 的 docstring 说得清楚：同一个结论第二次推导出来**不新建行**，只把
`last_confirmed_at` 往前推，而 `evidence` 冻在第一次判定的那一刻。所以这里逐行
`update_or_create`：

- `create_defaults`（新建用）= `{evidence: 冻住的那份, exemption: 指针, pool_rebuild_id, first_decided_at, last_confirmed_at}`
- `defaults`（已存在用）= `{last_confirmed_at}`

`exemption` 与 `evidence`/`pool_rebuild_id` **只出现在 `create_defaults` 里**是刻意的：它们记的是
「当初它是不是被豁免过」「当初依据的是哪一代」。豁免到期关掉之后再重跑一次，那一行仍要答
得出这两个问题——落进 `defaults` 会每轮把它们抹成 `None`，正是它们要避免的。
留下 `pool_rebuild_id` 也让它与 `evidence["pool_rebuild_id"]` 永远同值（重算差异重放
靠后者判断「这条决策的依据是不是已经过期」）。

决策要冻的是**它自己那一刻**的依据，而池化表每一代整体替换、日后还会重算——所以依据从
本轮那一次读出来的格子冻（`cells`），不从 `Outcome.evidence` 再抄一遍：同一份数据两处取用，
迟早会有一处忘了跟着改。冻下去的是**副本**，不是指向池化表的指针。

不用 `bulk_create`：要的是「推进」而不是「跳过已存在的」（`ignore_conflicts=True` 只做后者）。
一行一次 `update_or_create` 各自原子，也就**不必**再包一层事务：多条决策之间没有跨行不变量，
中途断了下一轮把剩下的补上——「补上」这件事每轮都在做，幂等本来就是它的形状。

## 冷启动、状态过期、没有池化表：三种「什么都不动」的收场

`deactivation.Derivation.blocked` 非空时 `writable` 必空，这里一行都不写，并把
`blocked_note` 带进返回值——「日志不算被看见」，这句话要由任务与日报接着往上传。

本层另有一种收场：**阶段有了、却一代池化表都没有**（`SKIPPED_NO_GENERATION`）。那不是
「没有建议」，是**没法问**——适用性表还没建过。它与前两种一样落成 `skipped`，一样带一句话。

三种收场下**豁免的关闭也一并停手**（`close_left_regime_exemptions` 只在没被挡下时才调）。
关豁免问的是「当前阶段是不是已经离开那条豁免登记的那个阶段」，而前两种收场说的正是「这一轮
说不清当前阶段」——冷启动没有阶段可问，过期的那条已经不作数。豁免本身还有「到期」这条
失效条件兜底（10 个自然日），所以停手的代价是有界的；反过来误关一条在期豁免**不可逆**
（`closed_at` 写下去就关掉了，没有复活路径）。两个方向不对称，就往不会造成不可逆损失的那边倒。

同一个理由还给了 `close_left_regime_exemptions` 自己第三条停手：**保命档期间一条都不关**
（详见它的 docstring）。高波动是叠加层而不是另一个阶段，豁免登记的基础阶段并没有离开——
但这一层「上面没被挡下」是看不出来的，所以那道闸放在那个函数自己身上。

## 人工豁免的写路径为什么也在这里（第③段 Q3）

第③段给豁免开了第二个入口（`/regime exempt`）。两个入口要做的**判断**逐条相同：冷启动
不许猜阶段、保命档期间不许猜阶段、策略重名不许猜、收回先到者为准。这些判断各写一遍的话，
分叉会长成「一个入口说已发出豁免、另一个安静地少写一行」——两个入口看起来都正常。

所以本模块收下三类东西，管理命令与 slash 命令都**只调它们、不重写**：

1. **写路径**（`grant` / `revoke`）与状态判据（`state_of` / `STATE_*`）；
2. **入参解析**（`find_strategy` / `find_strategy_by_token` / `resolve_regime` /
   `parse_exemption_id`），错的时候抛 `ExemptionError`——**只说事实，不说怎么改**：
   它同时服务命令行与 Telegram，而「改哪个旗标」只有各入口自己知道；
3. **展示口径**（`exemption_line` / `exemption_standing` / `roster_report` 等），
   同一条豁免在终端里与在 Telegram 里必须长得一样。

写入仍然只由人触发：这一节没有任何函数被 `run_deactivation` 或任务调到（Q7 的
「推理路径永远不自己发豁免给自己放行」不是靠自觉，是靠没有调用点）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping
from uuid import UUID

from django.utils import timezone

from apps.regime import config, deactivation, judgement, pool_rebuild
from apps.regime.models import DeactivationDecision, DeactivationExemption
from apps.regime.quant import BaseRegime
from apps.regime.slice import REASON_DISPLAY, REASON_HIGH_VOL_BLANKET
from apps.trading.models import LiveSession, Strategy

logger = logging.getLogger(__name__)

#: 阶段有、池化表没有。**不是「没有建议」，是「没法问」**（见模块 docstring）。
SKIPPED_NO_GENERATION = "no_generation"

#: 豁免提前失效的原因码（`DeactivationExemption.closed_reason`，max_length=32）。
#: 「阶段离开」是**系统**观测到的；「人工收回」是**人**下的手。两者分开记，日报与
#: 事后复盘才分得清「这条豁免是自然失效的，还是被人撤掉的」。
CLOSE_REASON_REGIME_LEFT = "regime_left"
CLOSE_REASON_MANUAL = "manual_revoke"

#: 原因码 → 中文。与 `deactivation.py` 的 `*_DISPLAY` 同一条纪律：展示与取值分开，
#: 中文只出现在展示层。两个码都在这里点名，所以新增一个码时这张表会缺一项——
#: 缺项在展示时回落成原因码本身，而不是伪装成另一档。
CLOSE_REASON_DISPLAY = {
    CLOSE_REASON_REGIME_LEFT: "阶段离开（自然失效）",
    CLOSE_REASON_MANUAL: "人工收回",
}


@dataclass(frozen=True)
class ManagedSet:
    """被管策略集合的取数结果（CONTEXT.md 第 105 条）。

    `ids` 是**筛过**的（判据一）；`running` 只是标记（判据二）；`unresolved` 是被判据一
    筛掉的那些——回测自动建出来的幽灵策略行，日报只给它们一个数，不给动作。
    """

    ids: tuple[UUID, ...]
    running: frozenset[UUID]
    unresolved: tuple[UUID, ...]


# --------------------------------------------------------------------------- #
# 取数
# --------------------------------------------------------------------------- #


def running_strategy_ids(strategy_ids: Iterable[UUID]) -> tuple[UUID, ...]:
    """被至少一个活跃实盘会话引用的策略 id（判据二，**只标记不筛选**）。"""
    ids = list(strategy_ids)
    if not ids:
        return ()
    rows = (
        LiveSession.objects.filter(
            strategy_id__in=ids,
            mode="live",
            status__in=LiveSession.ACTIVE_STATUSES,
        )
        .order_by("strategy_id")
        .values_list("strategy_id", flat=True)
        .distinct()
    )
    return tuple(rows)


def managed_set() -> ManagedSet:
    """被管策略集合。

    先补一次注册表发现（`ensure_strategies_discovered`）：注册表是「解析到实现类」的唯一
    出处，而 `apps.ready()` 只在策略目录当时存在时才 discover。不补的话整个集合会安静地
    缩成空——空集合在日报上看起来像「没有策略」，不像「注册表没加载」。

    排序按 `created_at`：集合是要进日报与任务返回值的，顺序不稳会让「今天和昨天有什么
    不同」多出一堆假的差异。
    """
    pool_rebuild.ensure_strategies_discovered()
    all_ids = list(
        Strategy.objects.order_by("created_at", "id").values_list("id", flat=True)
    )
    _, unresolved = pool_rebuild.strategy_raw_texts(all_ids)
    unresolved_set = set(unresolved)
    managed = tuple(i for i in all_ids if i not in unresolved_set)
    return ManagedSet(
        ids=managed,
        running=frozenset(running_strategy_ids(managed)),
        unresolved=tuple(unresolved),
    )


def current_regime_state(
    symbol: str = judgement.SYMBOL, *, now: datetime | None = None
) -> deactivation.RegimeState:
    """当前**生效**的那条阶段。没有生效判定（冷启动）时是 `RegimeState()`（`cold`）。

    判定的 `effective_at` 被原样带过去——过期判据问的是「这个结论从哪天开始在生效」，
    不是「它哪一天算出来的」。
    """
    record = judgement.current_judgement(symbol, now=now)
    if record is None:
        return deactivation.RegimeState()
    return deactivation.RegimeState(
        regime=record.effective_regime, effective_at=record.effective_at
    )


def in_force_exemptions(*, now: datetime | None = None) -> dict[tuple[UUID, str], int]:
    """在期人工豁免：`{(策略 id, 阶段): 豁免 id}`。

    三条一起才算在期——已关闭、还没生效、已过期都不算。「阶段离开」那一条由
    `close_left_regime_exemptions` 写成 `closed_at`，所以这里不重复判它。
    """
    now = now or timezone.now()
    rows = (
        DeactivationExemption.objects.filter(
            closed_at__isnull=True,
            granted_at__lte=now,
            expires_at__gt=now,
        )
        .order_by("granted_at")
        .values_list("id", "strategy_id", "regime")
    )
    # 同一格若重复发过豁免，取**最近发出的**那条：按 `granted_at` 升序遍历，字典后写
    # 覆盖先写。取错的话日报会说「已豁免」而实际在期的是另一条（更晚发的那条）。
    return {(strategy_id, regime): eid for eid, strategy_id, regime in rows}


def close_left_regime_exemptions(
    regime: str | None, *, now: datetime | None = None
) -> tuple[int, ...]:
    """阶段离开该阶段 → 豁免自然失效（CONTEXT.md 第 120 条）。返回被关掉的豁免 id。

    写成一条记录而不是纯查询派生，理由见 `DeactivationExemption` 的 docstring：阶段当天
    离开又回来时，纯按 `now < expires_at` 判断会让它复活，而那句话是「自然失效」不是
    「暂停」。

    冷启动（`regime is None`）时**一条都不关**：说不清「离开了没有」，而误关的代价是
    一条本来在期的豁免无声消失。

    **保命档期间同样一条都不关**，原因一模一样：高波动是**叠加层**（CONTEXT.md 第 115
    条），不是「离开」。豁免登记的永远是**基础阶段**，而高波动抬升期间传进来的这个
    `regime` 是 `high_vol`——照 `!=` 比下去，用户手上每一条在期豁免都会在高波动第一天
    被关掉，且没有复活路径；等保命档过去、系统回落到适用性层，那条策略直接进停用，
    而用户什么都没做。代价只是「阶段离开」的判定在高波动期间延后一轮，且豁免本身还有
    「到期」兜底（10 个自然日）——两个方向不对称，就往不会造成不可逆损失的那边倒。
    """
    if regime is None:
        return ()
    if deactivation.is_blanket(regime):
        return ()
    now = now or timezone.now()
    stale = list(
        DeactivationExemption.objects.filter(closed_at__isnull=True, expires_at__gt=now)
        .exclude(regime=regime)
        .values_list("id", flat=True)
    )
    if not stale:
        return ()
    # 更新时再查一次 `closed_at__isnull=True`：两次读之间可能有别人（管理命令）关掉了
    # 同一条，那时以先到者为准——后手不覆盖前手的 `closed_reason`。
    DeactivationExemption.objects.filter(id__in=stale, closed_at__isnull=True).update(
        closed_at=now, closed_reason=CLOSE_REASON_REGIME_LEFT
    )
    return tuple(stale)


def _frozen_evidence(
    outcome: deactivation.Outcome,
    row: Mapping[str, Any] | None,
    generation,
    state: deactivation.RegimeState,
) -> dict[str, Any]:
    """落进 `DeactivationDecision.evidence` 的那一份。

    「为什么停我」要能只读这一行就答完：当时是哪个阶段（以及它是从哪一刻生效的）、池化
    那一格写了什么（连同它自己的依据摘要）、依据的是哪一代池化表。

    `row` 为 `None` 在正常路径上到不了——能写出一条决策的前提就是那格 `unfit`，而有格才
    谈得上状态。留着这个分支只是为了让「那条链子哪天断了」退化成一条依据偏薄的决策，
    而不是在循环中间抛出去、把当天剩下的决策一起带走。
    """
    return {
        "regime": outcome.regime,
        "state": outcome.state,
        "reason": outcome.reason,
        "source": outcome.source,
        "needs_review": bool(row.get("needs_review")) if row else False,
        "pool_rebuild_id": generation.id,
        "pool_version": generation.pool_version,
        "regime_effective_at": (
            state.effective_at.isoformat() if state.effective_at else None
        ),
        # 池化那一格的依据摘要原文（`RegimePoolCell.evidence`）。池化表会被下一代整体
        # 替换，所以它必须抄在这里，不能留个指针。
        "cell": dict(row.get("evidence") or {}) if row else {},
    }


# --------------------------------------------------------------------------- #
# 落库
# --------------------------------------------------------------------------- #


def run_deactivation(
    *,
    symbol: str = judgement.SYMBOL,
    now: datetime | None = None,
    params: config.JudgementLifecycleConfig | None = None,
) -> dict[str, Any]:
    """当前阶段 × 当前代池化格 → 停用决策（`DeactivationDecision`）。

    **本函数只写 `suggested`**：翻面（`applied` / `released`）是第③段 gate 层的事
    （`gate_run._write_statuses`，只改 `status` 那一列），与本函数不复述同一条判据——
    「这条策略该不该停」在那边是**按当前阶段重算**的，而这里的结论是**推导那一刻**的。
    所以「状态恒为 `suggested`」这句话对读这张表的人已经不成立，对本函数的**写入**仍然
    成立。

    幂等：同一个结论反复跑只会把 `last_confirmed_at` 往前推。返回一个**可 JSON 序列化**的
    摘要（Celery 用 JSON 序列化结果，所以 id 一律是字符串），键集在两条路径上一致。

    `skipped` 恒存在，取值 `None` / `cold_start` / `stale_state` / `no_generation`。
    与判定的「有问题才给 `skipped`」略有不同：这里日报要能直接问一句「这轮有没有产出」，
    而不是靠键在不在——**一个会因为缺键而变成「没事」的字段，迟早会被当成没事**。
    非空时 `note` 必然是一句给人看的话（`deactivation.Derivation.blocked_note`），
    由调用方带出去：日志不算被看见。

    Args:
        symbol: 判定与池化都挂在同一个品种上（`config.CANDLES.symbol`）。
        now: 注入时钟；不传取当前时刻。同时用于在期豁免与过期判据。
        params: 生命周期参数；不传取统一配置面的当前值。
    """
    now = now or timezone.now()
    params = params or config.JUDGEMENT_LIFECYCLE

    managed = managed_set()
    state = current_regime_state(symbol, now=now)
    generation = pool_rebuild.current_generation()

    summary: dict[str, Any] = {
        "symbol": symbol,
        "regime": state.regime,
        "generation_id": generation.id if generation is not None else None,
        "skipped": None,
        "note": "",
        "managed": len(managed.ids),
        "running": len(managed.running),
        "unresolved": [str(i) for i in managed.unresolved],
        "unmanaged": [],
        "targets": 0,
        "written": 0,
        "created": 0,
        "confirmed": 0,
        "exempt": 0,
        "needs_review": 0,
        "missing_cells": 0,
        "exemptions_closed": 0,
        "suggestions": [],
    }

    if generation is None:
        summary["skipped"] = SKIPPED_NO_GENERATION
        summary["note"] = (
            "还没有任何一代池化表，本轮不产出停用建议（先跑 recompute_regime_slices）"
        )
        logger.info("[regime] 停用决策：%s", summary["note"])
        return summary

    # 一次读出当前代（**这一代**）的全部格子，推导与冻证据都从这一份取。
    # 不分成两次读：两次各自去问一次当前代，之间可能正好翻了一代，于是落进决策的
    # 就成了「依据写的是第 N 代、引用的却是第 N+1 代那一格」。
    cells = pool_rebuild.current_cells(generation)
    derivation = deactivation.derive(
        cells,
        state=state,
        strategy_ids=managed.ids,
        running_ids=managed.running,
        exemptions=in_force_exemptions(now=now),
        now=now,
        params=params,
    )

    summary.update(
        {
            "unmanaged": [str(i) for i in derivation.unmanaged],
            "targets": len(derivation.targets),
            "exempt": len(derivation.exempt),
            "needs_review": len(derivation.needs_review),
            "missing_cells": len(derivation.missing_cells),
        }
    )

    if derivation.blocked:
        # 冷启动 / 状态过期：一条都不写，但**必须留下这句话**（见模块 docstring）。
        # 豁免的关闭也一并停手——见模块 docstring 里「三种什么都不动」那一段。
        summary["skipped"] = derivation.blocked
        summary["note"] = derivation.blocked_note
        logger.info("[regime] 停用决策：%s", summary["note"])
        return summary

    summary["exemptions_closed"] = len(
        close_left_regime_exemptions(state.regime, now=now)
    )
    for outcome in derivation.writable:
        row = cells.get((outcome.strategy_id, outcome.regime))
        if row is None:
            # 单次读取下这里到不了：`TARGET` 只能由一格 `unfit` 推出来，而有格才推得出。
            # 留着它只是让「那条链子哪天断了」退化成一行依据偏薄的决策，而不是在循环
            # 中间抛出去、把当天剩下的决策一起带走——`_frozen_evidence` 收 `None`。
            logger.warning(
                "[regime] 停用决策：当前代池化表里找不到策略 %s 在 %s 的格子，依据留空",
                outcome.strategy_id,
                outcome.regime,
            )
        _, created = DeactivationDecision.objects.update_or_create(
            strategy_id=outcome.strategy_id,
            regime=outcome.regime,
            # `defaults` 只管**更新**；`create_defaults` 只管**新建**（不传时新建也用
            # `defaults`）。两边分开正是要的形状：已存在的那一行只被推进 `last_confirmed_at`。
            defaults={"last_confirmed_at": now},
            create_defaults={
                "evidence": _frozen_evidence(outcome, row, generation, state),
                "exemption_id": outcome.exempt_id,
                "pool_rebuild_id": generation.id,
                "first_decided_at": now,
                "last_confirmed_at": now,
            },
        )
        summary["created" if created else "confirmed"] += 1
        summary["suggestions"].append(
            {
                "strategy_id": str(outcome.strategy_id),
                "regime": outcome.regime,
                "state": outcome.state,
                "reason": outcome.reason,
                "source": outcome.source,
                "running": outcome.running,
                "exempt": outcome.exempt_id is not None,
                "created": created,
            }
        )

    summary["written"] = len(summary["suggestions"])
    logger.info(
        "[regime] 停用决策：阶段 %s，被管 %s（在跑 %s），建议 %s（新建 %s / 确认 %s），"
        "已豁免 %s，关闭豁免 %s，待复核 %s，无此格 %s",
        summary["regime"],
        summary["managed"],
        summary["running"],
        summary["targets"],
        summary["created"],
        summary["confirmed"],
        summary["exempt"],
        summary["exemptions_closed"],
        summary["needs_review"],
        summary["missing_cells"],
    )
    return summary


# --------------------------------------------------------------------------- #
# 人工豁免：状态与展示口径
# --------------------------------------------------------------------------- #


#: 豁免在展示层的四种状态（互斥，判据见 `state_of`）。取值是「与推导同一口径」那句话的
#: 另一半：`in_force` 与 `in_force_exemptions` 的三条一起逐字同向（有测试逐条对）。
STATE_IN_FORCE = "in_force"
STATE_PENDING = "pending"
STATE_EXPIRED = "expired"
STATE_CLOSED = "closed"

STATE_DISPLAY = {
    STATE_IN_FORCE: "在期",
    STATE_PENDING: "未生效",
    STATE_EXPIRED: "已过期",
    STATE_CLOSED: "已关闭",
}


class ExemptionError(Exception):
    """人工豁免的入口共用的「这件事办不成」。

    **只说事实，不说怎么改。** 它同时服务管理命令与 Telegram 的 `/regime exempt`：
    「改哪个旗标」只有命令行知道，「换一条命令怎么写」只有 slash 入口知道。事实层
    写死一种措辞，另一个入口就得在用户看到的句子里塞一句不相干的旗标名。

    各入口把「怎么改」补在自己那一层（命令行补 `--regime`，slash 补用法串）。
    """


def state_of(exemption: DeactivationExemption, *, now: datetime) -> str:
    """一条豁免现在处于哪个状态。**收到豁免行本身**，不收四个字段。

    收行是为了让判据只有一处：谁想知道状态都调这个函数，而不是各自 `filter(...)` 一遍。
    """
    if exemption.closed_at is not None:
        return STATE_CLOSED
    if exemption.granted_at > now:
        return STATE_PENDING
    if exemption.expires_at <= now:
        return STATE_EXPIRED
    return STATE_IN_FORCE


def regime_display(value: str) -> str:
    """阶段取值 → 中文。**认不出来就原样返回**，不回落成某一档。

    模型上的 `choices` 只在表单校验里管用，手工写进去的行绕得过它。一条脏行不该让整个
    清单炸掉；但它也绝不能被显示成「下行趋势」——那会让人以为豁免覆盖的是另一档。
    """
    try:
        return BaseRegime(value).display
    except ValueError:
        return f"{value}（认不出的阶段）"


def regime_options_text() -> str:
    """四个阶段的中文名与 slug（`上行趋势（uptrend）、…`）。

    **两个入口的「可选：…」共用这一句**：分叉的话，人在 Telegram 上看到的可选项与在
    终端上看到的对不上，而两个列表都没错、只是不全——这种分叉没人会报。
    """
    return "、".join(f"{m.display}（{m.value}）" for m in BaseRegime)


# --------------------------------------------------------------------------- #
# 人工豁免：入参解析
# --------------------------------------------------------------------------- #


def _parse_uuid(raw: Any) -> UUID:
    """策略主键的解析。**不能复用 `parse_exemption_id`**：拿一个 UUID 去 `int()` 会把
    「格式不对」报成「豁免 id 必须是整数」，而这是给人看的提示。

    也不把解析交给 ORM：`Strategy.objects.filter(id="x")` 抛的是 `ValidationError`，
    在管理命令里它会显示成一段堆栈，在 Telegram 上会变成一句「出错了」。
    """
    text = str(raw).strip()
    try:
        return UUID(text)
    except ValueError:
        raise ExemptionError(f"策略 id 必须是 UUID，收到 {text!r}")


def parse_exemption_id(raw: Any) -> int:
    """豁免 id 是自增整数（策略主键才是 UUID），但让错误在参数这一层就响，
    而不是等一个 `ValueError` 从 ORM 里冒出来。"""
    text = str(raw).strip()
    try:
        return int(text)
    except ValueError:
        raise ExemptionError(f"豁免 id 必须是整数，收到 {text!r}")


def find_strategy(*, name: str = "", strategy_id: str = "") -> Strategy:
    """按名字或主键找一条策略。**恰好给一个**，多给少给都是错。

    名字**精确匹配**（`Strategy.name`）：模糊匹配在给人看的入口上比「找不到」危险得多
    ——它会安静地给另一条策略发豁免。重名时**不猜**，把两个 id 都写出来让人选：两条
    同名的策略从输出上完全分不开，只有 id 分得开。
    """
    raw_id = (strategy_id or "").strip()
    wanted = (name or "").strip()
    if raw_id and wanted:
        raise ExemptionError("策略名与策略 id 只能给一个，收到两个。")
    if not raw_id and not wanted:
        raise ExemptionError(
            "要指明给哪条策略：策略名与策略 id 恰好给一个，一个都没给。"
        )
    if raw_id:
        strategy = Strategy.objects.filter(id=_parse_uuid(raw_id)).first()
        if strategy is None:
            raise ExemptionError(f"找不到策略 id {raw_id}")
        return strategy

    matches = list(Strategy.objects.filter(name=wanted))
    if not matches:
        raise ExemptionError(f"找不到策略名 {wanted!r}（按名字精确匹配，不支持模糊）")
    if len(matches) > 1:
        raise ExemptionError(
            f"策略名 {wanted!r} 对应 {len(matches)} 行，请改用策略 id 指明其中一个："
            + "、".join(str(row.id) for row in matches)
        )
    return matches[0]


def find_strategy_by_token(token: str) -> Strategy:
    """一个词 → 一条策略（`/regime exempt grant <策略>` 用）。

    **先按主键嗅探**：`UUID(token)` 解析得出来就当成 `Strategy.id`，否则当名字。次序是
    刻意的——打出一个 UUID 的人要的就是哪一行，而「名字碰巧长得像 UUID」的策略在真库上
    不存在（注册表按模块名建行），这条歧义落在空集上。
    """
    text = (token or "").strip()
    try:
        UUID(text)
    except ValueError:
        return find_strategy(name=text)
    return find_strategy(strategy_id=text)


@dataclass(frozen=True)
class RegimeChoice:
    """阶段那一项解析出来的结果。

    `taken_from_current` 区分「人点名的」与「从当前生效阶段取的」：后者必须**打出来**
    （「未给 --regime，取当前生效阶段：…」），否则一条取来的阶段会被当成人的决定。
    """

    regime: str
    effective_at: datetime | None = None
    taken_from_current: bool = False


def resolve_regime(regime: str = "", *, now: datetime | None = None) -> RegimeChoice:
    """阶段这一项：人给了就照落，没给就从当前生效判定取——**取不到就拒绝**。

    两条拒绝都是「宁可让人多打几个字」：

    - **冷启动**（一条生效判定都没有）拒绝。回落成某个默认档会让一条猜出来的阶段获得
      10 天的实际效力（第①段的原话）。
    - **保命档期间拒绝**（第③段 Q4）。高波动是叠加层、不是可以登记豁免的基础阶段：
      保命档期间每个策略的结论都是 `blanket`、`targets` 恒空（`slice.py:185` 那一格
      「不做适用性判断」），所以落一条 `high_vol` 的豁免既不计入推导的 `exempt`、也不
      挡任何东西，10 天里只是一条空转记录——而人看到「已发出豁免」会以为自己办成了一次
      恢复。这正是管理命令过去的形态（`_current_regime` 直接取 `effective_regime`）。

    人在保命档期间**显式点名** `high_vol` 仍然照落，由调用方补一句警告
    （`blanket_grant_warning`）：显式就是知情，机制不该替人否决一个明确的选择。

    `effective_at` 原样带出去，调用方要把它打进提示里（「自 X 生效」）——否则「取到的
    是哪条判定」在屏幕上没有痕迹。
    """
    text = (regime or "").strip()
    if text:
        try:
            BaseRegime(text)
        except ValueError:
            raise ExemptionError(
                f"认不出的阶段 {text!r}，可选：{regime_options_text()}"
            )
        return RegimeChoice(regime=text)

    state = current_regime_state(now=now)
    if state.regime is None:
        raise ExemptionError(
            "当前没有生效中的阶段判定（冷启动），推断不出该登记哪个阶段，"
            f"可选：{regime_options_text()}"
        )
    if state.blanket:
        raise ExemptionError(
            "当前生效阶段是保命档（高波动），它不是可以登记豁免的基础阶段："
            "此刻登记会得到一条不计入豁免、也拦不住任何东西的记录，"
            f"可选：{regime_options_text()}"
        )
    return RegimeChoice(
        regime=state.regime,
        effective_at=state.effective_at,
        taken_from_current=True,
    )


# --------------------------------------------------------------------------- #
# 人工豁免：写路径
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class GrantOutcome:
    """一次发出：写进去的那一行 + 被它盖住的那几条 + 生效期天数。"""

    exemption: DeactivationExemption
    superseded: tuple[DeactivationExemption, ...]
    days: int


@dataclass(frozen=True)
class RevokeOutcome:
    """一次收回：真关掉的条数 + 本来就已关闭的条数（后者保持原样）。"""

    closed: int
    already_closed: int


def exemption_rows_for(strategy: Strategy, regime: str):
    """同一格已有的豁免（覆盖提示要它）。

    **显式排序**：模型的 `Meta.ordering` 是 `["strategy", "regime", "-granted_at"]`，在这一
    格里等价于「最近发出的在前」，但 `granted_at` 相同的两行之间没有 tiebreaker——提示的
    顺序会随数据库的返回顺序漂。同一时刻发出的两条豁免只在测试里出现，而**测试里恰恰
    最容易冻结时钟**。
    """
    return DeactivationExemption.objects.filter(
        strategy=strategy, regime=regime
    ).order_by("-granted_at", "id")


def grant(
    *,
    strategy: Strategy,
    regime: str,
    actor: str,
    note: str = "",
    now: datetime | None = None,
) -> GrantOutcome:
    """发出一条豁免：`expires_at` **在写入这一刻**按配置换算成绝对时刻。

    绝对时刻而不是「按配置查的时候算」——改配置不追溯已经发出的豁免（模型 docstring
    那条，有测试钉住）。

    同一个（策略 × 阶段）上**允许**有多条历史豁免（表上刻意没有唯一约束），因为在期那条
    被关闭之后还要留痕。在期时再发一条是「延长/覆盖」的语义，而推导认的是**最近发出的
    那条**（`in_force_exemptions` 按 `granted_at` 升序、后写覆盖）——所以 `superseded`
    必须由调用方说出来（`supersede_warnings`），否则人会以为豁免期被叠加了。

    **不在这里校验阶段**：阶段那一项的判断（冷启动 / 保命档 / 认不认得出）只在
    `resolve_regime` 一处，这里收它给出的结论。再判一遍就是给「哪个阶段能登记豁免」
    造第二个答案。
    """
    now = now or timezone.now()
    days = config.DEACTIVATION.exemption_days
    superseded = tuple(
        row
        for row in exemption_rows_for(strategy, regime)
        if state_of(row, now=now) == STATE_IN_FORCE
    )
    exemption = DeactivationExemption.objects.create(
        strategy=strategy,
        regime=regime,
        granted_at=now,
        expires_at=now + timedelta(days=days),
        granted_by=actor,
        note=note,
    )
    return GrantOutcome(exemption=exemption, superseded=superseded, days=days)


def revoke(raw_ids: Iterable[Any], *, now: datetime | None = None) -> RevokeOutcome:
    """按 id 收回。**先查存在性，缺一个就整批不写**。

    整批拒绝而不是「能关的关掉、剩下的报个错」：报错时人本来就已经在「我刚才写的是哪条」
    这件事上不确定了，这时候把其中几条悄悄关掉，收场是半张表被改过、屏幕上只有一句错误。

    条件更新 + 先到者为准（`closed_at__isnull=True`），与 `close_left_regime_exemptions`
    同形：已经关掉的（阶段离开 / 早先撤过）保持原样，连 `closed_reason` 都不覆盖——
    「人撤的」与「阶段离开的」正是事后复盘要看的那一格。
    """
    now = now or timezone.now()
    wanted = [parse_exemption_id(raw) for raw in raw_ids]
    found = set(
        DeactivationExemption.objects.filter(id__in=wanted).values_list("id", flat=True)
    )
    missing = [str(i) for i in wanted if i not in found]
    if missing:
        raise ExemptionError(
            f"这些豁免 id 不存在：{'、'.join(missing)}。"
            "不带动作列一次清单就能看到现有的 id。"
        )
    closed = DeactivationExemption.objects.filter(
        id__in=wanted, closed_at__isnull=True
    ).update(closed_at=now, closed_reason=CLOSE_REASON_MANUAL)
    return RevokeOutcome(closed=closed, already_closed=len(wanted) - closed)


# --------------------------------------------------------------------------- #
# 人工豁免：渲染（终端与 Telegram 逐字共用）
# --------------------------------------------------------------------------- #


def grant_summary(outcome: GrantOutcome) -> str:
    """发出之后的第一行。`expires_at` 到分钟：10 天之后回头看这一行，要知道它到哪一刻。"""
    row = outcome.exemption
    return (
        f"已发出豁免 #{row.id}：{row.strategy.name} × {regime_display(row.regime)}，"
        f"至 {row.expires_at:%Y-%m-%d %H:%M}（{outcome.days} 个自然日），"
        f"发出人 {row.granted_by}"
    )


def supersede_warnings(rows: Iterable[DeactivationExemption]) -> tuple[str, ...]:
    """「同一格原有在期豁免被盖住了」的那些行。

    **不能不报**：两条都在表里（没有唯一约束），但生效期不会被叠加——不说的话人会以为
    豁免延长到了 20 天。
    """
    return tuple(
        f"  注意：同一格原有在期豁免 #{row.id}（至 {row.expires_at:%Y-%m-%d}），"
        "推导只认最近发出的那条，它仍在表里留痕但已不再生效"
        for row in rows
    )


def revoke_summary(outcome: RevokeOutcome) -> tuple[str, ...]:
    """收回之后的那两行。第二行只在「有本来就已关闭的」时才出现。"""
    lines = [
        f"已收回 {outcome.closed} 条豁免（{CLOSE_REASON_DISPLAY[CLOSE_REASON_MANUAL]}）"
    ]
    if outcome.already_closed:
        lines.append(
            f"  另外 {outcome.already_closed} 条本来就已关闭，保持原样"
            "（先到者为准，不覆盖先手的 closed_reason）"
        )
    return tuple(lines)


def blanket_grant_warning(row: DeactivationExemption) -> str | None:
    """显式点名保命档时的那一句；别的阶段返回 `None`。

    `resolve_regime` **不拦**显式点名的 `high_vol`，代价是那条记录在保命档期间完全空转
    ——所以必须当场说出来。它也**不会**一直留着：保命档过去之后第一轮
    `close_left_regime_exemptions` 就把它按「阶段离开」关掉（那时传进来的 `regime` 不再是
    保命档，`!=` 判据恢复正常）。
    """
    if not deactivation.is_blanket(row.regime):
        return None
    return (
        f"  注意：{regime_display(row.regime)}是保命档，不是可以登记豁免的基础阶段——"
        "保命档期间没有任何策略会被判为停用，这条豁免在此期间不起作用；"
        "阶段回落之后它会被「阶段离开」自动关闭。"
    )


def exemption_line(row: DeactivationExemption, *, now: datetime) -> str:
    """清单里的一行。**行列格式只有这一处**（`roster_report` 与它逐字同源）。

    调用方要 `select_related("strategy")`：这一行一定要策略名，而清单是一屏几十行的
    地方，每行一次查询是白丢的。
    """
    state = state_of(row, now=now)
    return (
        f"  #{row.id} {row.strategy.name} × {regime_display(row.regime)}"
        f"  [{STATE_DISPLAY[state]}]"
        f"  {row.granted_at:%Y-%m-%d} → {row.expires_at:%Y-%m-%d}"
        f"  由 {row.granted_by}"
        + (
            f"  关闭原因：{CLOSE_REASON_DISPLAY.get(row.closed_reason, row.closed_reason)}"
            if state == STATE_CLOSED
            else ""
        )
        + (f"  备注：{row.note}" if row.note else "")
    )


def exemption_standing(*, now: datetime | None = None, blanket_live: bool) -> str:
    """在期豁免此刻**算不算数**这一句话（工具、清单、slash 命令共用）。

    **人工恢复豁免不穿透保命档**（CONTEXT.md 第 176 条）——这条必须能被读出来。否则用户
    会看到一个自己放行过、却又被停的策略，而那与「机制没听见我」在观感上无法区分。

    `blanket_live` 由调用方给，不收在这里算：它有两个来源（`query_halt` 问的是声明表里
    有没有保命档那一层，`query_deactivations` 问的是当前生效阶段是不是高波动），谁问的是
    哪个问题只有调用方知道。这一句不重复判它，只负责把「压住了」说出来。
    """
    count = len(in_force_exemptions(now=now))
    if not count:
        return "当前没有在期的人工豁免。"
    if blanket_live:
        return (
            f"{count} 条在期，但**当前一条都不生效**——"
            f"{REASON_DISPLAY[REASON_HIGH_VOL_BLANKET]}在拦，人工恢复豁免不穿透保命档。"
        )
    return f"{count} 条在期（当前没有保命档在拦，豁免照常生效）。"


def roster_report(*, now: datetime | None = None) -> tuple[str, ...]:
    """豁免清单的每一行。**管理命令与 `/regime exempt` 逐字共用。**

    两处各拼一遍的话，同一条豁免在终端里与在 Telegram 里长得不一样，而「同一件事两处
    说法不同」正是这一层要挡的东西。返回行列表而不是一整段：调用方各自决定怎么输出
    （`self.stdout.write` / Telegram 的一页消息）。

    保命档那一句（`exemption_standing`）紧跟在表头后面：它是这份清单里**最该先看到**的
    一句话——「在期」两个字本身不告诉人它此刻算不算数。

    空表返回一句「没有任何豁免记录」，不带汇总：四档全 0 的汇总行看起来像一份有内容的
    报告，而它其实什么都没说。
    """
    now = now or timezone.now()
    rows = list(
        DeactivationExemption.objects.select_related("strategy").order_by(
            "-granted_at", "id"
        )
    )
    if not rows:
        return ("没有任何豁免记录",)

    counts: dict[str, int] = {state: 0 for state in STATE_DISPLAY}
    lines = [f"豁免共 {len(rows)} 条："]
    lines.append(
        "  "
        + exemption_standing(
            now=now, blanket_live=current_regime_state(now=now).blanket
        )
    )
    for row in rows:
        state = state_of(row, now=now)
        counts[state] += 1
        lines.append(exemption_line(row, now=now))
    lines.append(
        "汇总：" + "，".join(f"{STATE_DISPLAY[s]} {counts[s]}" for s in STATE_DISPLAY)
    )
    return tuple(lines)
