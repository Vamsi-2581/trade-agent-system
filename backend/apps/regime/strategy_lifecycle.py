"""策略的「机制在册」总开关（`Strategy.is_active`）的人工写入口。

`/regime` 页面上那个「删除策略」按钮落到这里。它做的是 CONTEXT.md 第 106 条那件事：
把 `is_active` 翻成 `False`（= 已退役，机制不再管它、也不自动恢复它），**不是**在数据
库里删行——所以下面这些说法必须逐字出现在页面上，否则「删除」这个词在撒谎。

## 「删除」= 退役，不是删数据

| 这一步做 | 这一步不做 |
| --- | --- |
| 把策略移出机制的管辖（不再产出停用决策与 `strategy:<id>` 声明） | 不删回测、不删切片、不删池化格子、不删停用决策 |
| 落一条启停流水（谁、何时、为什么） | 不删 `Strategy` 行本身 |
| 让下一轮 gate 对账解除它当前的停用声明 | 不停正在跑它的 `LiveSession` |

**最后一格是这份实现里最容易被误读的一格，所以它必须在确认页上明写**：`is_active`
只被机制读（`gate_run._warrant`、`replay_run` 的选取），交易执行链路**不看它**——退役
一条策略不会停止任何正在运行的会话，却会让机制**不再拦它**。于是对一条正在跑的策略
来说，「删除」的净效果是**放开**（下一轮对账解除 `strategy:<id>` 声明之后，它可以重新
开新仓）。这与用户对「删除」的直觉相反，页面上不说，就是一次静默的放开。

## 两个方向都由人决定，机制永不改它

- 退役（`to_active=False`）：机制不再管它。
- 恢复启用（`to_active=True`）：放回被管集合。**它不产生任何豁免**——那是
  `DeactivationExemption` 那条通路（「人工恢复」），与这里的启停是两件事：恢复启用只
  把策略放回去，下一轮对账若仍判它不适用，它会再次被停。
- 机制自己一个方向都不做：`cleanup_ghost_strategies` 那条管理命令**只做 True → False**
  （它的 docstring 写明了理由：`is_active` 的语义里写着「永不被自动恢复」，替人恢复
  是在替人做决定）。本模块的恢复方向只由**人在页面上按下**触发，并且与退役一样落流水。

## 留痕是同一次求值的快照

`reason` 由服务端渲染（`summary()`），**不接受客户端手写**——与 `views.switch` 那条
纪律同一取向。写入时**重新求值一次**再落库（`flip` 里那一次 `page()`）：确认页上那份
是给人看的建议值，而落库的必须是「按下按钮那一刻的库里事实」，两者不是同一次快照时，
以落库那份为准，并把落库的那份回显给页面。

## 幂等：本来就是这一档不写第二条流水

与 `RegimeMechanismSwitch` 的「重复敲同一条命令不留一串一模一样的行」同源。写入走
**条件更新**（`filter(pk=…, is_active=not to_active).update(...)`）：读与写之间被人抢先
翻过时，更新影响 0 行 ⇒ 不写流水、回执说「本来就是这一档」。失败方向是安全的：宁可
少写一条流水，也不写一条描述不存在翻转的流水。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from django.db import transaction
from django.utils import timezone

from apps.regime import halt
from apps.regime import pool_rebuild
from apps.regime.models import (
    ActorKind,
    DeactivationDecision,
    HaltDeclaration,
    RegimePoolCell,
    StrategyLifecycleChange,
)
from apps.trading.models import LiveSession, Strategy

logger = logging.getLogger(__name__)

#: 两个方向的取值，与 `Strategy.is_active` 一一对应（`True` = 在册）。
ACTIVE = "active"
RETIRED = "retired"

#: 两个方向的值 → 人话。**值与人话在这里成对**（`serializers` 只查表不复制），前端不重拼。
LIFECYCLE_DISPLAY: dict[str, str] = {
    ACTIVE: "在册（机制会管它）",
    RETIRED: "已退役（机制不再管它）",
}

#: 「哪一个动作」的取值 → 人话。用在确认页标题与按钮上。
ACTION_DISPLAY: dict[bool, str] = {
    False: "退役（从机制里删除这条策略）",
    True: "恢复启用（放回机制管辖）",
}

#: 三个开关那边也有一句「本来就是这一档」；这里的措辞与它同形（同一件事的两个方向）。
ALREADY_DISPLAY: dict[bool, str] = {
    False: "本来就是退役状态，没有写第二条流水。",
    True: "本来就是启用状态，没有写第二条流水。",
}


def lifecycle_state(is_active: bool) -> str:
    """`is_active` → 取值。**只认布尔真值**：`None` / 脏值一律当「不在册」以外的含义。

    收成函数而不是到处写 `ACTIVE if x else RETIRED`：这条判据在三个地方要用（格子的
    `strategy` 载荷、确认页、回执），散开的写法迟早会有一处把 `None` 读成「在册」。
    """
    return ACTIVE if is_active is True else RETIRED


def state_display(is_active: bool) -> str:
    return LIFECYCLE_DISPLAY[lifecycle_state(is_active)]


def receipt_display(name: str, *, to_active: bool) -> str:
    """翻转成功之后那句话（回执）。**与 CLI / 页面同一批词**。"""
    if to_active:
        return f"「{name}」已恢复启用：机制重新开始管它（不产生任何豁免）。"
    return f"「{name}」已退役：机制不再管它，历史记录一条都没删。"


def already_display(name: str, *, to_active: bool) -> str:
    return f"「{name}」{ALREADY_DISPLAY[to_active]}"


@dataclass(frozen=True)
class Counts:
    """确认页上那几个数。**每一个都是「此刻库里的」**，不是缓存出来的。"""

    active_sessions: int
    declarations: int
    decisions: int
    pool_cells: int


@dataclass(frozen=True)
class Page:
    """一次退役/恢复的确认页。**只读**：同一个策略、同一批事实算两次给同样的结果。"""

    strategy_id: UUID
    name: str
    is_active: bool
    to_active: bool
    counts: Counts
    #: 这一次翻转**会不会**真的写东西。`False` = 本来就是目标档（回执说「本来就是」）。
    available: bool

    @property
    def action_display(self) -> str:
        return ACTION_DISPLAY[self.to_active]

    @property
    def summary(self) -> str:
        """落进 `StrategyLifecycleChange.reason` 的那句话。**由这一次求值渲染**。"""
        if self.to_active:
            return (
                f"人工恢复启用：{self.name}；机制重新开始管它（不产生豁免），"
                f"当前阶段若仍判它不适用会在下一轮对账再次停它。"
            )
        return (
            f"人工退役（从机制里删除）：{self.name}；"
            f"此刻活跃会话 {self.counts.active_sessions} 个、"
            f"待解除的停用声明 {self.counts.declarations} 条；"
            f"回测 / 切片 / 池化格 / 停用决策全部保留。"
        )

    @property
    def body(self) -> str:
        """确认页正文（后端渲染，前端原样显示）。"""
        if not self.available:
            # 本来就是这一档 ⇒ 正文就是那句话，**不是**一份「将要发生什么」的清单：
            # 把一份不会发生的动作的说明摆给人看，与 `reused` 被显示成「已完成」是同一种
            # 谎（ADR 0006）。前端据此把那一侧的按钮置灰并原样显示这一句。
            return already_display(self.name, to_active=self.to_active)
        return _restore_body(self) if self.to_active else _retire_body(self)


@dataclass(frozen=True)
class Flip:
    """一次 `flip()` 的收场。

    `row is None` = 本来就是这一档（没有写第二条流水）。`page` 是**写入那一刻**重算的
    那一页：`reason` 与它同源，回执也由它渲染。
    """

    page: Page
    row: StrategyLifecycleChange | None

    @property
    def changed(self) -> bool:
        return self.row is not None

    @property
    def display(self) -> str:
        if self.row is None:
            return already_display(self.page.name, to_active=self.page.to_active)
        return receipt_display(self.page.name, to_active=self.page.to_active)


# --------------------------------------------------------------------------- #
# 取数
# --------------------------------------------------------------------------- #


def counts_for(strategy_id: UUID) -> Counts:
    """确认页上的四个数。**四个都是计数查询**，不把行读进内存。

    `pool_cells` 只数**当前代**的格子：历史代的格子照样留着，而页面要回答的是「这一代
    池化表里还有几格」，历史代那几个数在代际历史里。
    """
    generation = pool_rebuild.current_generation()
    return Counts(
        active_sessions=LiveSession.objects.filter(
            strategy_id=strategy_id, status__in=LiveSession.ACTIVE_STATUSES
        ).count(),
        declarations=HaltDeclaration.objects.filter(
            closed_at__isnull=True, scope=halt.strategy_scope(strategy_id)
        ).count(),
        decisions=DeactivationDecision.objects.filter(strategy_id=strategy_id).count(),
        pool_cells=(
            0
            if generation is None
            else RegimePoolCell.objects.filter(
                strategy_id=strategy_id, rebuild=generation
            ).count()
        ),
    )


def changes_for(strategy_ids) -> dict:
    """一批策略各自最近一次启停流水（转发 `StrategyLifecycleChange.latest_per_strategy`）。

    存在的理由是让调用方（读面）不必知道「最近一次」是怎么算的；判据只有一处。
    """
    return StrategyLifecycleChange.latest_per_strategy(strategy_ids)


def page(strategy: Strategy, *, to_active: bool) -> Page:
    """确认页。**只读**，不写任何东西。"""
    is_active = bool(strategy.is_active)
    return Page(
        strategy_id=strategy.pk,
        name=str(strategy.name),
        is_active=is_active,
        to_active=bool(to_active),
        counts=counts_for(strategy.pk),
        available=is_active != bool(to_active),
    )


# --------------------------------------------------------------------------- #
# 写
# --------------------------------------------------------------------------- #


def flip(
    strategy: Strategy,
    *,
    to_active: bool,
    actor_kind: ActorKind | str,
    actor_name: str,
    now: datetime | None = None,
) -> Flip:
    """把 `is_active` 翻到 `to_active`，并在真的翻了时落一条启停流水。

    条件更新（`is_active=not to_active`）让「读到该翻」与「写下翻转」之间被人抢先的那一
    次返回 `changed=False`：宁可少写一条流水，也不写一条描述不存在翻转的流水。

    本函数**不碰**其它任何东西：不停会话、不写声明表、不改停用决策。声明表那边由下一轮
    `gate_run.sync` 按 `is_active=False ⇒ gate.GONE` 的既有判据自行解除——那是机制的既有
    语义（CONTEXT.md 第 131 条「退役即不再参与求值，记录保留」），不是这里补的。
    """
    if not str(actor_name).strip():
        raise ValueError("触发方不能为空：一次没人认领的启停无法排查")

    at = now or timezone.now()
    current = page(strategy, to_active=to_active)
    if not current.available:
        logger.info(
            "[regime] 策略 %s 本来就是%s，没有写第二条流水",
            current.name,
            "启用" if to_active else "退役",
        )
        return Flip(page=current, row=None)

    with transaction.atomic():
        updated = Strategy.objects.filter(
            pk=strategy.pk, is_active=not to_active
        ).update(is_active=to_active)
        if not updated:
            # 竞态：这一瞬间别人已经翻过了。重新读一次现状，按「本来就是」回执。
            refreshed = Strategy.objects.filter(pk=strategy.pk).first()
            if refreshed is None:
                # 策略行在两次读之间被删了（ghost 清理命令）。这不是本函数能修的状态，
                # 如实往上抛：调用方会把它转成 404 / 500，而不会写出一条悬空的流水。
                raise Strategy.DoesNotExist(strategy.pk)
            logger.warning(
                "[regime] 策略 %s 的启停在读到之后、写下去之前被别人抢先了，本次不写流水",
                strategy.pk,
            )
            # 内存里那一份同样要跟到**库里的真实值**（不是本次目标值）：调用方紧接着要用
            # 它渲染回执的 `lifecycle`，留着上面那次读到的旧值会说反一档。
            strategy.is_active = bool(refreshed.is_active)
            return Flip(page=page(refreshed, to_active=to_active), row=None)

        row = StrategyLifecycleChange.objects.create(
            strategy=strategy,
            from_active=not to_active,
            to_active=to_active,
            at=at,
            actor_kind=ActorKind(actor_kind).value,
            actor_name=str(actor_name).strip()[:128],
            reason=current.summary,
        )

    # 内存里这一份也要跟上：调用方（视图）紧接着就要用它的 `is_active` 渲染回执，
    # 而 `.update()` 不会回写这个实例。
    strategy.is_active = to_active
    logger.info(
        "[regime] 策略 %s（%s）%s（%s：%s）",
        current.name,
        strategy.pk,
        "恢复启用" if to_active else "退役",
        row.actor_name,
        current.summary,
    )
    return Flip(page=current, row=row)


# --------------------------------------------------------------------------- #
# 正文（后端渲染的叙述段落）
# --------------------------------------------------------------------------- #


def _retire_body(page_: Page) -> str:
    return "\n".join(
        [
            f"退役「{page_.name}」：把它移出机制的管辖范围（is_active: True → False）。",
            "",
            "这一步做的事：",
            "· 机制不再对它产出停用决策与 strategy:<id> 声明；它不会被自动停用，"
            "也不会被自动恢复（人工总开关）。",
            f"· 它当前的停用声明 {page_.counts.declarations} 条会在下一轮 gate 对账时解除"
            "（对账每 5 分钟一轮；若那一轮正处于「什么都不动」的状态——保命档或判定失效"
            "——解除会顺延到下一次对得上账的时候）。",
            "· **解除之后，正在跑它的会话可以重新开新仓**：「退役」是把机制的手从这条策略"
            "上拿开，不是「停止这条策略」。",
            f"· 正在跑的会话不会被停：退役不改 LiveSession。此刻引用它的活跃会话 "
            f"{page_.counts.active_sessions} 个。",
            "",
            "这一步不做的事：",
            f"· 不删除任何数据：回测、切片、池化格子（当前代 {page_.counts.pool_cells} 格）、"
            f"停用决策（{page_.counts.decisions} 条）、豁免记录全部保留。",
            "· 不写 gate 声明表、不碰事件熔断与保命档、不碰别的策略。",
            "",
            "确认之后会落一条启停流水（谁、何时、为什么）。",
        ]
    )


def _restore_body(page_: Page) -> str:
    return "\n".join(
        [
            f"恢复启用「{page_.name}」：把它放回机制管辖（is_active: False → True）。",
            "",
            "· 机制重新开始管它：下一轮 gate 对账会按当前证据重新判它——"
            "**若当前阶段仍判它不适用，它会再次被停**（恢复启用不是豁免）。",
            "· 已解除的停用声明不会因此「复活」：新一轮对账按当前证据重新声明。",
            "· 不产生任何豁免。豁免是「人工恢复」那条通路（is_active 之外的另一件事），"
            "与这里的启停不是一回事。",
            f"· 数据一直留着：回测、切片、池化格子（当前代 {page_.counts.pool_cells} 格）、"
            f"停用决策（{page_.counts.decisions} 条）都没动过。",
            "",
            "确认之后会落一条启停流水（谁、何时、为什么）。",
        ]
    )


__all__ = [
    "ACTIVE",
    "ACTION_DISPLAY",
    "Counts",
    "Flip",
    "LIFECYCLE_DISPLAY",
    "Page",
    "RETIRED",
    "already_display",
    "changes_for",
    "counts_for",
    "flip",
    "lifecycle_state",
    "page",
    "receipt_display",
    "state_display",
]
