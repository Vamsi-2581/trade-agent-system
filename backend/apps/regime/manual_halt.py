"""手动熔断——ADR 0001 欠账的「手动触发」入口（2026-10-12 兑现）。

ADR 0001 把停止统一成 halt 状态机之后留了一句「唯一保留的改动是给既有熔断器接通
手动触发入口」，而那个入口一直没有落进代码——``CircuitBreaker`` 的 Redis 状态写完就
没人读（``pre_trade_check`` 的第 1 步刻意保持禁用），等于 ADR 承诺的开关只存在在文档里。
本模块就是那笔欠账的兑现：**手动触发的落点是一行 ``HaltTrigger.MANUAL`` 声明**，走
``HaltDeclaration`` 表、由 `halt.py` 现行读——不是 Redis，也**不是**把熔断接回
``pre_trade_check``（ADR 0001 已明确否决后者；手动行拦单走的是第 0 步的停止判定，
与事件熔断同一个口）。

## 为什么落在声明表而不是 `CircuitBreaker` 的 Redis

三档既有触发源（事件 / 保命档 / 策略停用）的落点都是声明表，判定函数只读那两张状态表
（CONTEXT.md:134）。若手动触发落 Redis，它就必须有一个自己的读点才能拦单——那个读点
就是 ADR 要防的「绕过 halt 状态机的隐藏停机开关」。落声明表则白得三样东西：

1. **有读者**：`halt.halt_layers` 直接求值，拒单理由与 `query_halt` / 通知 / 日报同源。
2. **有流水**：开、解除（`closed_at` + 原因）都留在行上，事后排查「为什么当时单下不出去」时
   答得出是哪个源、什么时候、依据什么——这正是 ADR 0001 把停止改写成触发源的全部理由。
3. **有通知**：`halt_notify.notify_pending` 对声明行一律发开/解除两条即时消息（第②e 段），
   不用本模块再造一个投递通路（全仓不新投第二套）。

## 与 300 秒窗口同步的关系：**两个写方，互不相干**

`halt_sync._reconcile` 只对它 `_OWNED_TRIGGERS` 里认得的源做解除；``MANUAL`` 不在其中，
对账永不碰本模块写下的行——它们活到人工解除为止（或按自带的 `expires_at` 到点失效）。
这条缝隙是刻意的：保命档由窗口同步自动解除，是因为它的「事实」（阶段）自己在变；手动
熔断的事实是**人的决定，只有人能收回**——自动解除的存在本身就是给「没人记得清了」
开门，所以没有它。到点失效（`expires_at`）是**求值**层面的不拦，不是解除：行留着，
`closed_at` 为空，未来回看「当时按过一次、按到几点」仍然答得出来。

## re-trip 是原地改写，不是关一行开一行

同一个（触发源 × 作用域）只能有一行活着（`uniq_live_halt_declaration`）。再次手动熔断时
**改写**那一行（新原因、新起点、新的到期时刻），不关旧行再开新行：关开一次会在这张排查
依据里留下一条「声明—解除」的假历史（`HaltDeclaration` docstring 原话），而「又按了一次」
不是「先解除了一件别的事」。改写起点意味着通知的账清零（`opened_notified_at = None`），
300 秒一轮的通知任务会按新的一轮重新说「已生效」。

## 同步函数

读库写库都是同步的，调用方（管理命令 / 将来的 slash 命令 / 测试）直接调即可；放进
async 上下文里时与 `halt._halt_verdict` 同一条纪律——外套 `sync_to_async` / `db_async`。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Sequence

from django.utils import timezone

from apps.regime import halt
from apps.regime.models import ActorKind, HaltDeclaration, HaltTrigger

logger = logging.getLogger(__name__)

#: 手动熔断的默认时长。**取自被退役的 Redis 语义**（`CircuitBreaker.CIRCUIT_BREAKER_TTL`
#: = 86400 秒 = 24 小时）：那把「按到几点」的尺已经用了很多次，换了落点，
#: 换尺子不属于这次改动的事。
DEFAULT_TRIP_HOURS = 24

#: 解除原因码。短码 + 词表，与 `halt_sync.CLOSE_REASON_*` 同一口径；`halt_notify` 的
#: 解除原因**按触发源路由**到这里（那条分岔在 `halt_notify._close_reason_display`）。
CLOSE_REASON_LIFTED = "manual_lifted"

CLOSE_REASON_DISPLAY = {
    CLOSE_REASON_LIFTED: "人工解除",
}


class ManualHaltError(ValueError):
    """人给的输入不合法（目的层里唯一的「用户反馈」型错误）。

    管理命令把它当**正常回复**渲染（打错与系统坏了在用户眼里不该长得一样），与
    `event_commands.EventInputError` 同一条纪律；命令内部把用法行拼在它后面。
    """


def close_reason_display(reason: str) -> str:
    """原因码 → 人话。认不出来**原样返回**（`halt_sync.close_reason_display` 同一口径）。"""
    return CLOSE_REASON_DISPLAY.get(reason, reason)


def resolve_scope(kind: str, value: str | None = None) -> str:
    """调用方给的（档, 值）→ 本表作用域写法。**这是 scope 字符串唯一的拼装点。**

    三个档各自落到 `halt.scope_*` 的那一个助手；**不归一化任何值**（品种写法与
    ``OrderRequest.symbol`` 同一套，见 `halt.symbol_scope`）。``kind`` 只认
    ``global`` / ``symbol`` / ``strategy``，别的一律拒绝——求值层把读不懂的写法当
    ``global`` 是 fail-closed，但那是对**库里已有行**的兜底；写入方在这里就该把写法
    门槛立起来，靠求值层兜底的手动熔断会「想要拦全品种却拦了全场」。
    """
    if kind == "global":
        return halt.global_scope()
    if kind == "symbol":
        if not (value or "").strip():
            raise ManualHaltError(
                "symbol 作用域必须给出品种（例如 --symbol DOGE/USDT）"
            )
        return halt.symbol_scope(value.strip())
    if kind == "strategy":
        given = (value or "").strip()
        if not given:
            raise ManualHaltError("strategy 作用域必须给出策略 id（--strategy <uuid>）")
        try:
            uuid.UUID(given)
        except ValueError as exc:
            raise ManualHaltError(f"不是一个可认的 uuid：{given}") from exc
        return halt.strategy_scope(given)
    raise ManualHaltError(
        f"认不出作用域档 {kind!r}：只认 global / symbol:<品种> / strategy:<id>"
    )


@dataclass(frozen=True)
class TripOutcome:
    """一次手动熔断的结果。``rewritten`` 说的是「改写了已活的那一行」——两个入口（命令）
    要在回复里说清这一句：改写不是新开一条，之前的行还在原地。"""

    row: HaltDeclaration
    rewritten: bool


def _require_canonical_scope(scope: str) -> None:
    """写入前确认作用域写法是三档规范形之一。

    这是对 `_matches` 那句「读不懂的写法当 global」的**写入端**对账：求值层把
    「symbol:BTC-USDT（想拦合约）」这类写歪的记号 fail-closed 成「拦全场」，多拦是吵闹但
    看得见；可对手动熔断来说，拦错方向与该拦没拦都由人事后自己发现——当场炸
    （``ManualHaltError``）比悄悄拦错便宜得多。检查放在库外（不做任何写），所以 `trip`
    先炸它再干活。
    """
    kind, value = halt.parse_scope(scope)
    if scope == halt.global_scope():
        return
    if kind in ("symbol", "strategy") and value:
        return
    raise ManualHaltError(
        f"作用域写法无法被求值层认出：{scope!r}。"
        "只认 global / symbol:<品种> / strategy:<id> 三种写法"
    )


def _live_manual_row(scope: str) -> HaltDeclaration | None:
    """这个作用域上活着的 MANUAL 声明行（唯一键 `uniq_live_halt_declaration` 的 python 口径）。"""
    return (
        HaltDeclaration.objects.filter(
            trigger=HaltTrigger.MANUAL.value,
            scope=scope,
            closed_at__isnull=True,
        )
        .order_by("opened_at", "id")
        .first()
    )


def trip(
    scope: str,
    reason: str,
    *,
    actor_kind: str | ActorKind,
    actor_name: str,
    hours: int | None = DEFAULT_TRIP_HOURS,
    now: datetime | None = None,
) -> TripOutcome:
    """在 ``scope`` 上按下手动熔断，返回落下的那一行。

    - **`reason` 与 `actor_name` 都必填且拒绝空串**（`breaker_switch.flip_event_breaker`
      同一条理由：一条「切了但不知道为什么/谁切的」留痕与没有留痕几乎等价）。
    - `hours`：`None` 或 `0` = 不定，直到人工解除；正数 = 到点失效但不自动解除（行留着
      当记录）。默认 24 小时（`DEFAULT_TRIP_HOURS`）。
    - 该作用域上**已有活行** → 原地改写（新原因 / 新起点 / 新到期），不新开一行；
      ``rewritten=True``。
    - `opened_notified_at` 在改写时清零：起点变了，上一轮的「已生效」就过期了
      （`halt_sync._rewrite` 同一条账）。
    """
    reason = (reason or "").strip()
    if not reason:
        raise ManualHaltError(
            "手动熔断必须给出原因：事后排查「为什么单下不出去」要能答出依据什么"
        )
    if not (actor_name or "").strip():
        raise ManualHaltError("手动熔断必须记下触发方：没人认领的停止无法排查")
    _require_canonical_scope(scope)
    if hours is not None and hours < 0:
        raise ManualHaltError("hours 不能是负数；0 = 到人工解除为止")
    at = now or timezone.now()
    if hours:
        expires_at = at + timedelta(hours=hours)
    else:
        expires_at = None
        logger.warning(
            "[regime] 手动熔断未带时长（作用域 %s）：它会活到人工解除为止，"
            "解除指令是 manage.py manual_halt --lift",
            scope,
        )

    row = _live_manual_row(scope)
    if row is None:
        row = HaltDeclaration.objects.create(
            trigger=HaltTrigger.MANUAL.value,
            scope=scope,
            label=HaltTrigger.MANUAL.display,
            opened_at=at,
            expires_at=expires_at,
            reason=reason,
            actor_kind=ActorKind(actor_kind).value,
            actor_name=actor_name,
        )
        rewritten = False
    else:
        row.reason = reason
        row.opened_at = at
        row.expires_at = expires_at
        row.actor_kind = ActorKind(actor_kind).value
        row.actor_name = actor_name
        row.opened_notified_at = None
        row.save(
            update_fields=[
                "reason",
                "opened_at",
                "expires_at",
                "actor_kind",
                "actor_name",
                "opened_notified_at",
            ]
        )
        rewritten = True

    logger.info(
        "[regime] 手动熔断 %s（作用域 %s，%s，actor=%s，原因：%s）",
        "改写" if rewritten else "落下",
        scope,
        "直到人工解除" if expires_at is None else f"到点 {expires_at.isoformat()} 失效",
        actor_name,
        reason,
    )
    return TripOutcome(row=row, rewritten=rewritten)


def lift(
    scope: str,
    *,
    actor_kind: str | ActorKind,
    actor_name: str,
    now: datetime | None = None,
) -> HaltDeclaration | None:
    """人工解除 ``scope`` 上的手动熔断；**没有活行时返回 `None`**（调用方照实回话）。

    解除是写 `closed_at` + 原因码，不删行（``HaltDeclaration`` docstring：「这条声明
    存在过」要留得住）。`closed_notified_at` 留空 → 通知任务会补发「已解除」。
    `actor_*` 记的是**解除的人**——与落行时的触发方是两笔事实，各归各的列。
    """
    if not (actor_name or "").strip():
        raise ManualHaltError("人工解除必须记下触发方：没人认领的解除无法排查")
    _require_canonical_scope(scope)
    at = now or timezone.now()
    row = _live_manual_row(scope)
    if row is None:
        return None
    row.closed_at = at
    row.closed_reason = CLOSE_REASON_LIFTED
    row.actor_kind = ActorKind(actor_kind).value
    row.actor_name = actor_name
    row.save(update_fields=["closed_at", "closed_reason", "actor_kind", "actor_name"])
    logger.info("[regime] 手动熔断解除（作用域 %s，actor=%s）", scope, actor_name)
    return row


@dataclass(frozen=True)
class ManualHaltView:
    """概览页的一行：一条活着的 MANUAL 声明 + 它「此刻算不算数」。"""

    row: HaltDeclaration
    #: 此刻真的在拦（未到期 ∧ 未解除）。到点失效但未解除的行这里是 False，**不是**「没什么
    #: 可看」：那人从此处能看出「按过但已失效、还没人去解除」，与概览页把它列出来是同一件事。
    in_force: bool

    @property
    def expiry_line(self) -> str:
        if self.row.expires_at is None:
            return "直到人工解除"
        return f"到 {self.row.expires_at.isoformat()} 失效" + (
            "" if self.in_force else "（已到期）"
        )


def overview(*, now: datetime | None = None) -> Sequence[ManualHaltView]:
    """全部活着的 MANUAL 声明（含已到期未解除的），按起点排。只读，供两个入口共用。"""
    at = now or timezone.now()
    rows = HaltDeclaration.objects.filter(
        trigger=HaltTrigger.MANUAL.value,
        closed_at__isnull=True,
    ).order_by("opened_at", "id")
    views = []
    for row in rows:
        expired = row.expires_at is not None and row.expires_at <= at
        views.append(ManualHaltView(row=row, in_force=not expired))
    return views
