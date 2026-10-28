"""窗口同步：把「事件表 + 判定表」上该拦的事搬运成 ``HaltDeclaration`` 行（第②段单元 ②c）。

这是 ADR 0001 里那条分界线的**写方**：`halt.py` 的判定函数只读 ``HaltDeclaration`` 与
``RegimeMechanismSwitch`` 两张状态表、绝不 import ``MajorEvent``（CONTEXT.md:134「判定函数
只读状态，不读事件表」），于是「事件表 → 状态行」的搬运必须有人做。做的就是这个模块：
每次**整表重算**一遍，把事件表与判定表上的事实投影成一组声明行。

## 一行 = 一个源在一个作用域上的当前（或下一个）态度

`uniq_live_halt_declaration` 是（触发源 × 作用域）上的**活行唯一键**，而它只看
`closed_at IS NULL`——它不知道 `expires_at`。所以同一个源在同一个作用域上，**同一时刻只能
有一行活着**，哪怕两件事的窗口根本不重叠。这不是可以绕开的实现细节：两行并存的话，第二行
写入当场撞唯一键；而「一次触发一行」的写法又要求「解除」一次终结多行，那与
``HaltDeclaration`` docstring 定的「解除只终结自己那一行」直接冲突。

于是这一行记的是**这个源在这个作用域上此刻的态度**（正在拦 → 窗口是当前这一段；现在没拦
→ 窗口是**下一段**）。窗口在两段之间推进时是**原地改写**，不关一行再开一行：关开一次会在
这张表里留下「声明—解除」的假历史，而这张表是排查停用原因的依据。

**由此得到的一条硬性质：窗口永远不早于事实。** `opened_at` 取的是事实里**已经存好的那个
时刻**（事件的 `halt_at`、判定的 `effective_at`），不是任务跑起来的 `timezone.now()`。
好处有两层：一是逐次重算出来的期望值**逐字相同**，于是绝大多数轮次是零写入的空转（幂等的
对账，不是一次性投递）；二是任务延迟不会把「什么时候开始拦的」写歪——那会让日报与事件表
对同一件事给出两个时刻。唯一例外见下面的「提前写入的边界」。

## 为什么「下一段」要提前写进表里

任务是 300 秒一轮（`celery_app.beat_schedule`），**任何一轮都可能没跑**。若只写「此刻正在
拦」的那一段，两段之间哪怕只差一轮，第二段的开头就会有一段时间谁都拦不住——而事件熔断
**不可人工豁免**，那段敞口没有任何补救手段。提前写入换来的性质是：只要上一轮跑过，下一个
窗口开启的那一刻它已经在表里了，判定函数直接命中，不依赖那一轮任务是否准时。

**提前写入的边界（接受的代价）**：一行还没生效时，它的窗口可以被**无痕改写**（事件改期就
会改），因为这张表刻意没有「底层事件是谁」这一列，改写的痕迹只能留在事件表那边（事件改期
本身有流水）。这是上面那条唯一键换来的，写在 `HaltDeclaration` 的 docstring 里。

## 两段之间的小空隙：敞口上限是一轮任务

事实上的窗口是**半开区间** `[halt_at, resume_at)`。相邻或相接的窗口会被**合并成一段**（相接
也算：`[10:00,12:00)` 与 `[12:00,14:00)` 之间没有一刻是不拦的）。所以真正会漏的只有「空隙
窄于一轮任务间隔」的那种：段 A 结束后、段 B 开始前只隔了几十秒，任务在空隙里没跑，段 B 的
开头就会空一小段。代价有界（≤ 一轮任务间隔），且只在两件高影响事件挨得极近时出现。**不
把这种空隙也合成一段**（把 gap 抹掉的写法会真的多拦一段时间）——多拦是「用户眼里机制莫名
在拦」，比一小段敞口更难排查，而事件熔断又没有豁免通道。

## 合并、作用域、解除

- **重叠/相接的事件合成一行**：`label` 是各事件名按开窗时刻连起来（`、`），`reason` 把每个
  事件的事件时刻与熔断窗口都写出来。合并在写入方做，因为只有写入方知道此刻一共有哪些事件
  在窗口里（`HaltDeclaration` docstring 明写这一点）。
- **`market` 作用域 = 一行 `global`；`symbols` 作用域 = 每个品种各一行** `symbol:<品种>`。
  不把多个品种塞进一行：`scope` 存的是一个记号，而 `halt._matches` 是**按品种逐个比**的，
  塞进去的行会谁都匹配不上——表现为「声明写了却拦不住」。
- **`symbols` 为空 = 不写任何行**，而不是退回全市场。``EventScope`` 没有「默认全市场」这条
  退路（`applies_to` 对空列表恒为假），这里跟着它走：一条没有作用域的事件不该拦住全场。
- **没什么可写时解除**：写 `closed_at` + 一个原因码，不删行。原因码区分「窗口结束 / 事件已
  取消 / 改期改档后不再覆盖此刻 / 阶段离开高波动」，口径与 `deactivation_run` 的
  `CLOSE_REASON_*` 一致（短码 + `*_DISPLAY` 词表）。
- **写与开关无关**：声明说的是「这个源想拦」，开关说的是「这个源启用了没有」。就算
  `EVENT_BREAKER` 还停在 Shadow，事件窗口照样要进表——两者分开的理由写在 `HaltDeclaration`
  的 docstring 里；而反过来的表现（Shadow 期不写行、出 Shadow 时表是空的）会让「出了
  Shadow」那一刻**什么都不拦**，一直到下一个窗口才有行，等于把出 Shadow 的时点变成敞口。

## 四档，两个写方，一轮一组

这张表上的声明有四个触发源，而它们的期望集来自两处互不相干的计算：

| 档 | 期望值谁算 | 谁驱动 |
|----|-----------|--------|
| `EVENT` / `BLANKET` | 本模块的 `_plan`（事件表 + 判定表） | `regime-sync-halt-windows`，300 秒 |
| `DEACTIVATION` | `gate.derive`（停用决策行 + 池化表 + 豁免 + 开关） | `regime-gate-sync`，300 秒 |
| `REGIME_SUSPENSION` | `gate.derive_suspensions`（**全部被管策略** × 当前代那一格） | 同上，**同一个** plan |

本模块**算不出**策略档那两路的期望值（那要一整条池化推导），所以它们的期望集由
`gate_run` 算好、经 `sync` 的 `gate_plan` 参数递进来。两路在 `gate.merge` 里合成**一个**
plan，因为它们的唯一键（触发源 × 作用域）虽然不同，期望集却必须由同一轮一起管：拆成两轮
的话，第二轮会把第一轮不在它期望集里的行当成「计划里没有」而静默解除。

**一轮只对账一组**（策略档两路算一组），这是刻意的：两个 300 秒任务若都做全表对账，
它们会同时去 `create()` 同一把唯一键上的行——那是一条 `UniqueViolation`，出现与否取决于
两个任务这一轮隔了多少毫秒。一组一个写方，这个问题就不存在。

于是 `_OWNED_TRIGGERS` 的含义是「本模块**认得**哪些源」（认得的源才会被本模块解除，将来
新增的不认得的一律跳过），而「这一轮对账哪几档」由那一轮的入参决定。

**剩下的那个缺口是显式的**：`gate_plan` 没递进来的那些轮次（以及「这一行既不在期望集里
又不在解除名单里」那种写入方 bug）里，策略档的活行一律**不动**。把它们当成「计划里没有」
就会把别人的声明解除掉——一条停用决策静默失效，而它看起来与「本来就没停」一模一样。
这与下面 `_reconcile` 对未知键的 fail-closed 是同一条取向。

## 失败可见性与那两条即时消息（第②e 段落定）

本模块自己**不发告警**（CONTEXT.md:66 的「告警」= 给具体某个人的即时消息，日志不算被
看见；CONTEXT.md:180「不让每个新任务自己写告警」）：失败**往上抛**。第 180 条那两条路
里今天活着的是**日报第④段机制健康**（结论新不新）；「跑了没」那一条此前没有出口——体检
页的调度表那一段是补上的第一半，只答 beat 把它发出去了没有，事实与理由见
`tasks.py` 模块 docstring 的「跑了没」那一节。

而给**人**看的两条消息落在调用方（`tasks.sync_halt_windows`），不在本模块：一条是
「声明写入失败」（②e 的 Q7），另一条是每次窗口开/关的窗口通知。理由与「本层只把待告知
的名单放进返回值」同源（`replay_run` 的 docstring）：收件人要读 `LiveSession`，那是另一
件事的取数，不该混进「算差异」这一步。发送方与那两列的账在 `apps/regime/halt_notify.py`。

**本模块唯一替 ②e 做的事**：`_rewrite` 在窗口起点被改写时清掉 `opened_notified_at`。
这不是发送，是「这一行的事实变了」的一部分，只有本模块知道。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Iterable

from django.utils import timezone

from apps.regime import deactivation_run, events, halt
from apps.regime.models import (
    ActorKind,
    EventImpact,
    EventScope,
    EventStatus,
    HaltDeclaration,
    HaltTrigger,
    MajorEvent,
)
from apps.regime.slice import REASON_DISPLAY, REASON_HIGH_VOL_BLANKET

if TYPE_CHECKING:
    from apps.regime import gate

logger = logging.getLogger(__name__)

# 本任务的署名。写入方的身份是这张表的一半价值（「谁投的」）——排查时「谁」答不出来，
# 声明表就只剩一个看不出真假的布尔。
ACTOR_KIND = ActorKind.TASK
ACTOR_NAME = "regime.sync_halt_windows"
#: 策略停用那一档的署名（第③段）。**两个名字而不是一个**：这张表里
#: `trigger=deactivation` 的行只可能由 `regime-gate-sync` 写，事件与保命档的行只可能由
#: `regime-sync-halt-windows` 写。共用一个署名的话，「行是谁写的」这个问题的答案就变成
#: 「两个任务之一」，而排查时真正想知道的恰恰是哪一个。
GATE_ACTOR_NAME = "regime.sync_gate"

# 保命档那一行的触发源名称。**不用「保命档」**：触发源显示名已经是「保命档（高波动）」，
# 名称再用同一个词会读成「保命档（保命档（高波动）…）」。
BLANKET_LABEL = "高波动"

# 解除原因码。短码 + 词表，与 `deactivation_run.CLOSE_REASON_*` 同一口径（那里同样是
# 「短码进库、人话在 `*_DISPLAY`」）。`regime_left` 与那边的常量同值但**各定义各的**：
# 两张表解除的是两种不同的东西（人工豁免 / 停止声明），共用一个常量会让「改一处影响另一处」
# 变成看不见的耦。
CLOSE_REASON_WINDOW_ENDED = "window_ended"
CLOSE_REASON_EVENT_CANCELLED = "event_cancelled"
CLOSE_REASON_NO_LONGER_COVERS = "no_longer_covers_now"
CLOSE_REASON_REGIME_LEFT = "regime_left"

CLOSE_REASON_DISPLAY = {
    CLOSE_REASON_WINDOW_ENDED: "熔断窗口已结束",
    CLOSE_REASON_EVENT_CANCELLED: "事件已取消",
    CLOSE_REASON_NO_LONGER_COVERS: "改期或改档后不再覆盖此刻",
    CLOSE_REASON_REGIME_LEFT: "阶段已离开高波动",
}


def close_reason_display(reason: str) -> str:
    """原因码 → 人话。认不出来**原样返回**，不吞成一个「未知」：认不出来只可能是这份词表
    落后于写入方，那时看得见那个码比看见「未知」有用得多（同 `deactivation_run` 的写法）。
    """
    return CLOSE_REASON_DISPLAY.get(reason, reason)


# 本模块**认得**的触发源——认得的源才会被本模块解除，不认得的（将来新增的）直接跳过。
#
# **它不是「每一轮都对账这些」**：每一轮对账哪几档由那轮的入参决定（见 `sync` 的
# `gate_plan`），因为这两组的期望集来自两处互不相干的计算、也由两个 300 秒任务分别驱动
# （`regime-halt-window-sync` 管事件与保命档；`regime-gate-sync` 管策略档的两路）。
# 一个任务只碰自己那一组，这张表就不会出现两个写方抢同一把唯一键的局面。
#
# **`MANUAL`（手动熔断）刻意不在其中**：它的写方是 `apps/regime/manual_halt.py`（人工入口），
# 活到人工解除（或自带 `expires_at` 到点失效）为止——「自动解除的存在本身就是给『没人记得
# 清了』开门」，这条理由写在那边的模块 docstring 里。对账收到这种行时照实跳过：`_reconcile`
# 第一道 `trigger not in _OWNED_TRIGGERS` 分支会原样留着它们，既不算「计划里没有」也不报解除。
_OWNED_TRIGGERS = (
    HaltTrigger.EVENT,
    HaltTrigger.BLANKET,
    HaltTrigger.DEACTIVATION,
    # 按当前生效阶段暂停开新仓（issue #51）。它与 `DEACTIVATION` 由**同一个** 300 秒任务
    # 驱动（`regime-gate-sync` 把两路合成一个 `gate.GatePlan`），但**仍是独立的一档**：
    # 两件事各有自己的唯一键与收场（一个要人工恢复、一个随阶段自动翻转）。不写进这里的
    # 后果是它写下的行**永远不会被解除**——对账会把这个源当成「不认得」而静默跳过，那正是
    # 「格子回了 fit、声明还在长死」这个事故的唯一入口。
    HaltTrigger.REGIME_SUSPENSION,
)


# --------------------------------------------------------------------------- #
# 规划：事实 → 期望的那组行（纯函数，不吃 DB、不读时钟）
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PlannedRow:
    """期望表里应该活着的一行。字段与 `HaltDeclaration` 一一对应（少了 `closed_*`：计划里
    的行按定义都是活的）。"""

    trigger: HaltTrigger
    scope: str
    label: str
    reason: str
    opened_at: datetime
    expires_at: datetime | None


def plan_rows(*, now: datetime | None = None) -> list[PlannedRow]:
    """此刻应有的一组声明行。取数口只有两个：事件表与生效判定。"""
    at = now or timezone.now()
    return _plan(
        high_impact_events(),
        at,
        state=deactivation_run.current_regime_state(now=at),
    )


def _plan(candidates: list[MajorEvent], at: datetime, *, state) -> list[PlannedRow]:
    """``candidates`` 是**所有档位为「高」的事件**（含已取消的，取消的要用来写解除原因），
    本函数自己挑出该拦的那批。

    刻意把事件列表与 `RegimeState` 当参数传进来而不是自己去读：这样整段规划是**纯函数**，
    合并与推进的语义能用构造出来的对象直接测，不必先往库里造一批事件——唯一键、生效期
    边界那些真需要库的性质由 `test_halt_sync.py` 里的 `TestCase` 负责。
    """
    rows = _event_rows(candidates, at)
    blanket = _blanket_row(at, state=state)
    if blanket is not None:
        rows.append(blanket)
    return rows


def high_impact_events() -> list[MajorEvent]:
    """所有档位为「高」的事件，按窗口起点排好。

    **不按 `status` 过滤**：取消与改期都要读（前者用来写解除原因，后者本来就会从窗口里
    掉出去）。谁该拦由 `MajorEvent.triggers_halt` 判——它已经是「已排期 ∧ 档位为高」的
    合取，这里再手写一遍条件就是给「什么算该拦」造第二个答案。

    **公开**：`reduce_run` 要问「此刻真压在某个作用域上的事件是谁」，而它问的那一批与
    本模块写声明行时看的那一批必须是同一批——另写一条 `impact="high"` 的查询，分歧的
    表现是「声明写出来了、减仓不动」，两边看起来都正常。
    """
    return list(
        MajorEvent.objects.filter(impact=EventImpact.HIGH.value).order_by(
            "halt_at", "id"
        )
    )


def _event_rows(candidates: list[MajorEvent], at: datetime) -> list[PlannedRow]:
    """事件熔断层：**每个作用域一行**。

    按作用域分组而不是按事件分组，是因为唯一键的另一半是作用域——两件事件作用域不同就
    是两行，作用域相同才谈得上合并。
    """
    by_scope: dict[str, list[MajorEvent]] = {}
    for event in candidates:
        if not event.triggers_halt:
            continue
        for scope in _scopes_of(event):
            by_scope.setdefault(scope, []).append(event)

    rows: list[PlannedRow] = []
    for scope, group in by_scope.items():
        segment = _pick_segment(_segments(group), at)
        if segment is None:
            continue
        rows.append(
            PlannedRow(
                trigger=HaltTrigger.EVENT,
                scope=scope,
                label="、".join(event.name for event in segment),
                reason=_event_reason(segment),
                opened_at=segment[0].halt_at,
                expires_at=max(event.resume_at for event in segment),
            )
        )
    return rows


def _scopes_of(event: MajorEvent) -> list[str]:
    """这个事件压在哪些作用域上。与 `_touches` 互为逆运算——两边共用这一个函数，
    「写入的作用域」与「解除原因里去认的作用域」就不可能出现两套口径。
    """
    if event.scope_kind == EventScope.MARKET.value:
        return [halt.global_scope()]
    return [halt.symbol_scope(symbol) for symbol in (event.symbols or [])]


def touches(event: MajorEvent, scope: str) -> bool:
    """这个事件压在 ``scope`` 这个作用域上吗。与 ``_scopes_of`` 互为逆运算。

    **公开**：写侧（本模块挑该拦的事件、写解除原因）与读侧（`reduce_run`——「此刻到底
    有没有事件压在这个作用域上」）问的是同一个问题。各写一份的话，分歧会落在最不该
    出错的地方：声明行写着「熔断中」，而减仓那一侧认不出这是哪个事件，于是一动不动。
    """
    return scope in _scopes_of(event)


def _segments(group: list[MajorEvent]) -> list[list[MajorEvent]]:
    """把一组事件按窗口并成若干**互不相接**的段。

    **相接也算并**（`halt_at <= 段末`）：`[10:00,12:00)` 与 `[12:00,14:00)` 之间没有任何
    一刻是不拦的，拆成两段会凭空造出一个「空隙」，而空隙的后果是把「下一段」提前写进表
    的时机推迟到上一段结束之后（见模块 docstring 的敞口那一段）。

    `resume_at >= halt_at` 由 `ck_major_event_window_ordered` 保证，所以段的结束不会早于
    它的开始；`halt_at` 相同的两件事互相包含，同样并。
    """
    ordered = sorted(group, key=lambda event: (event.halt_at, event.id or 0))
    segments: list[list[MajorEvent]] = []
    end: datetime | None = None
    for event in ordered:
        if segments and end is not None and event.halt_at <= end:
            segments[-1].append(event)
            end = max(end, event.resume_at)
        else:
            segments.append([event])
            end = event.resume_at
    return segments


def _pick_segment(
    segments: list[list[MajorEvent]], at: datetime
) -> list[MajorEvent] | None:
    """此刻该写进表里的那一段：**正在拦的那段，没有就取下一段**。

    提前取下一段是这套写法的全部要点（见模块 docstring）：一行只能有一个窗口，而窗口若
    只覆盖「正在拦」的那段，两段之间任何一轮任务没跑都会留下敞口。取到的那段**必然还没
    结束**（含 `at` 的段 `end > at`；未来段 `start > at` 且 `end >= start`），所以规划出来
    的行永远不会是「已经过期」的行。
    """
    for segment in segments:
        start = segment[0].halt_at
        end = max(event.resume_at for event in segment)
        if start <= at < end:
            return segment
        if start > at:
            return segment
    return None


def _event_reason(segment: list[MajorEvent]) -> str:
    """一行声明的依据正文。

    每个事件写两句：事件时刻与熔断窗口，两个时刻都用 `events.format_moment`（北京 + UTC
    双写）。**复用那个函数而不是自己拼一行**：「事件时刻」在事件库、日报第③段和这里必须
    是同一句话，各写一遍就是「同一件事三处说法不同」——而这三处的读者恰好是同一批人。

    多事件时把「为什么是一行」写出来：不写的话，用户看到 label 里三个事件名会以为机制把
    三件事混成了一件，而真相是唯一键只允许同源同作用域活一行。
    """
    if len(segment) == 1:
        header = "高影响事件熔断窗口（档位为「高」的事件才触发熔断）："
    else:
        header = (
            f"{len(segment)} 条高影响事件的窗口在这个作用域上重叠或相接，"
            "按（触发源 × 作用域）的逻辑合成一行："
        )
    lines = [header]
    for event in segment:
        lines.append(
            f"  · {event.name}｜事件时刻 {events.format_moment(event.event_time)}"
        )
        lines.append(
            f"    熔断窗口 {events.format_moment(event.halt_at)}"
            f" → {events.format_moment(event.resume_at)}"
        )
    return "\n".join(lines)


def _blanket_row(at: datetime, *, state) -> PlannedRow | None:
    """高波动挡（保命档）：**只在生效中的判定本身就是高波动时写**。

    判据取自 `deactivation.RegimeState.blanket`（``regime == HIGH_VOL``），不走
    ``deactivation.derive`` 那条路：那条路要读池化表、在池化结论缺失时会给 `needs_review`，
    而保命档是**阶段本身的性质**，与这一代池化表算出了什么无关（`RegimeState.blanket` 的
    docstring 明写这一点）。所以只看判定表这一列，答得出就是答得出。

    `expires_at` 恒为 `None`（不定）：它随阶段起落，没有预先知道的截止时刻，失效由「阶段
    离开高波动」那一轮写出 `closed_at`。
    """
    if not state.blanket:
        return None
    if state.effective_at is None:
        # 「阶段是高波动、但不知道它从哪一刻起」是判定表不可能造出来的形状
        # （`effective_at` 非空），走到这里说明取数坏了。**不用 now() 兜底**：那会把写入方
        # 自己的钟点混进事实里，而这一行的 `opened_at` 正是「什么时候开始拦的」的唯一答案。
        raise RuntimeError(
            "生效判定是高波动，但没有生效时刻——判定表的 effective_at 不该为空"
        )
    return PlannedRow(
        trigger=HaltTrigger.BLANKET,
        scope=halt.global_scope(),
        label=BLANKET_LABEL,
        reason=_blanket_reason(),
        opened_at=state.effective_at,
        expires_at=None,
    )


def _blanket_reason() -> str:
    """保命档那一行的依据。**取自 `slice` 的词表**，不在这里另写一句中文：同一层在日报、
    `query_halt` 与这条声明里必须同措辞（第①段就定了这条纪律）。
    """
    return (
        f"{REASON_DISPLAY[REASON_HIGH_VOL_BLANKET]}——保命档不做适用性判断，与证据无关"
    )


# --------------------------------------------------------------------------- #
# 落库：期望 → 状态表（对账）
# --------------------------------------------------------------------------- #


def sync(
    *,
    now: datetime | None = None,
    extra_open: Iterable[HaltDeclaration] | None = None,
    gate_plan: "gate.GatePlan | None" = None,
    actor_name: str = ACTOR_NAME,
) -> dict:
    """跑一轮对账，返回 ``{created, updated, unchanged, closed}``。

    **幂等**：期望值逐字来自事实里存好的时刻，所以连着跑两轮，第二轮一定是
    `unchanged == 行数`。这一点是这套写法的可测形态，也是 300 秒一轮敢全表重算的前提。

    失败**往上抛**：下一轮 300 秒的对账就是重试（CONTEXT.md:181 把任务按「读安全 / 写危险」
    区分，而这张表是本模块独占写的、且每次都是全量重算，重试没有副作用）。吞掉异常会让
    「任务在跑、但表没更新」变成一件看起来正常的事。

    ``extra_open`` 是**同一次调度内、本函数读表之后**由别的写方插进来的活行（②d 的减仓
    认领行就是这种）。它们必须在**同一轮**参与对账，见 `_reconcile`。

    ## ``gate_plan``：本轮的期望集从哪来

    这个参数决定的是**期望集的来源**，于是也决定了这一轮对账哪一档：

    - **不给**（默认）——本模块自己算：事件表 + 判定表 → 事件与保命档两档。**这是今天的
      路径，逐字未变**。
    - **给**（``gate.GatePlan``，由 `gate_run` 算好）——本轮对账**策略档的两路**
      （停用决策 / 按当前生效阶段暂停开新仓）。两者的期望行与解除原因都取自这个
      plan，本模块一行都不自己算；plan 里每条声明自带触发源
      （`gate.Declaration.trigger`，本层据此落到各自的唯一键上）。

    「给了 plan 就不算事件与保命档」不是省事，是**必须**：那一档由
    `regime-sync-halt-windows` 那一轮负责，两个写方各管各的档，才不会在同一把唯一键上
    撞车（见模块 docstring 的「四档，两个写方」）。``extra_open`` 于是只在默认那一支有意义
    ——它讲的是「本模块自己算的那些行里，有哪几行是我刚插进去的」。

    ``gate_plan`` 是**纯判定层**（`gate.py`）的产出，本模块负责把它的策略 id 翻成
    `strategy:<id>` 作用域——作用域怎么写是这张表的事，判定层不该知道。
    """
    at = now or timezone.now()

    if gate_plan is not None:
        planned, close_reasons = _gate_planned(gate_plan)
        return _reconcile(
            planned,
            at,
            [],
            # 两档一起对账：它们由**同一个** plan 产出（`gate.merge` 合成的），所以也只能
            # 由同一轮一起管。只写其中一档的话，另一档的活行会在这一轮里被判成「计划里没有」
            # ——那是静默解除。
            sources=(HaltTrigger.DEACTIVATION, HaltTrigger.REGIME_SUSPENSION),
            close_reasons=close_reasons,
            actor_name=actor_name,
        )

    candidates = high_impact_events()
    state = deactivation_run.current_regime_state(now=at)
    planned = {
        _key(row.trigger, row.scope): row for row in _plan(candidates, at, state=state)
    }
    return _reconcile(
        planned,
        at,
        candidates,
        sources=(HaltTrigger.EVENT, HaltTrigger.BLANKET),
        extra_open=extra_open,
        actor_name=actor_name,
    )


def _gate_planned(
    plan: "gate.GatePlan",
) -> tuple[dict[tuple[str, str], PlannedRow], dict[str, str]]:
    """``gate.GatePlan`` → 本表要的两件东西：期望行（按唯一键）与解除原因（按作用域）。

    **作用域在这里拼**，不在判定层：`gate.Declaration` 只带策略 id（它的 docstring 明写
    了这一点），而 `strategy:<id>` 是这张表唯一键的另一半，属于本模块。

    解除原因同样翻成**作用域**键，而不是沿用 plan 里的策略 id 键：下游 `_close_reason`
    手上只有一行活声明，它答得出来的只有 `row.scope`。两边统一成作用域，就消掉了
    「plan 的键是 UUID 还是 str」这个迟早会踩到的坑。
    """
    rows: dict[tuple[str, str], PlannedRow] = {}
    for declaration in plan.declarations:
        # 触发源由**声明自带**（`gate.Declaration.trigger`）：本层据此把同一个 plan 里的
        # 「停用决策」与「按阶段暂停」两路落到各自的唯一键上。判定层用的是字面量
        # （它不能 import ORM 模型），两边取值一致由测试钉住。
        trigger = HaltTrigger(declaration.trigger)
        scope = halt.strategy_scope(declaration.strategy_id)
        rows[_key(trigger, scope)] = PlannedRow(
            trigger=trigger,
            scope=scope,
            label=declaration.label,
            reason=declaration.reason,
            opened_at=declaration.opened_at,
            expires_at=declaration.expires_at,
        )
    reasons = {
        halt.strategy_scope(strategy_id): code
        for strategy_id, code in plan.close_reasons.items()
    }
    return rows, reasons


def _reconcile(
    planned: dict[tuple[str, str], PlannedRow],
    at: datetime,
    candidates: list[MajorEvent],
    *,
    sources: tuple[HaltTrigger, ...],
    close_reasons: dict[str, str] | None = None,
    extra_open: Iterable[HaltDeclaration] | None = None,
    actor_name: str = ACTOR_NAME,
) -> dict:
    """期望的一组行 → 状态表，返回 ``{created, updated, unchanged, closed}``。

    ``sources`` = **这一轮对账哪几档**。表里另外那些档的行必须原样留着：它们不是「计划里
    没有」，是「这一轮不归我管」（见模块 docstring 的「四档，两个写方」）。

    ## 为什么 ``extra_open`` 是**对的**做法，而不是给测试开的方便门

    本函数按唯一键（触发源 × 作用域）对账，而 ``planned`` 只包含本轮的期望集。别的写方
    往这张表插的行一旦不在 ``live`` 里被看见，``planned`` 里剩下的那个键就会被无条件
    ``create()``，撞上唯一键 ``uniq_live_halt_declaration`` —— 那是一条
    ``UniqueViolation``，而且在「事件窗口刚打开、②d 刚认领」这个**每次减仓都会走到**的
    时刻稳定复现。

    ## 为什么是「传进来」而不是「本函数自己重读一次表」

    ``sync`` 读表与写表之间隔着 ``_plan`` 的取数（事件表 + 判定表）。再读一次表只是把
    窗口缩短，并没有消掉它；而调用方（``sync_halt_windows``）恰恰**知道**自己在这次调度
    里刚插了哪几行——它手上有那些对象。所以这个参数是「把已知事实说清楚」，不是「补一个
    竞态修补」。时序上仍然是：②d 的认领行先落库，本函数随后在**同一轮**里把它当活行看。
    """
    summary = {"created": 0, "updated": 0, "unchanged": 0, "closed": 0}

    # 先按 id 去重：调用方既可能在 ``extra_open`` 里传进来一行**已经在表里**的（它自己
    # 也是从表里读的），也可能传进来一行还没落库的（``pk is None``，例如纯规划出来的行）。
    # 后者的存在是这套写法必须容下的，见参数说明。
    live: list[HaltDeclaration] = []
    seen: set = set()
    for row in list(HaltDeclaration.objects.filter(closed_at__isnull=True)) + list(
        extra_open or []
    ):
        marker = row.pk if row.pk is not None else id(row)
        if marker in seen:
            continue
        seen.add(marker)
        live.append(row)

    for row in live:
        trigger = halt.trigger_of(row)
        if trigger not in _OWNED_TRIGGERS:
            # 不认得这个源——本模块对它的期望值一个字的判断都做不出来，跳过。
            continue
        if trigger not in sources:
            # 认得，但**不是这一轮的账**（另一档由另一个 300 秒任务驱动）。把它当成
            # 「计划里没有」就把它解除了，而那正是模块 docstring 里说的那个静默失效。
            continue
        desired = planned.pop(_key(trigger, row.scope), None)
        if desired is None:
            # 还没落库的行（``pk is None``）在这里没有可改的东西——它的 ``closed_at``
            # 只存在于内存里，写下去只会造一行没有 pk 的新记录。调用方传进来的活行按
            # 定义都是「刚刚写进去的」，所以这条分支只可能是它自己搞错了。
            if row.pk is None:
                logger.warning(
                    "[regime] 对账时收到一行未落库的声明（触发源 %s，作用域 %s），跳过",
                    trigger.value,
                    row.scope,
                )
                continue
            reason = _close_reason(
                row, at, candidates, gate_close_reasons=close_reasons
            )
            if reason is None:
                # **本模块对这一行做不出判断**（策略档本轮没有期望集、或者这一行既不在
                # 期望集里又不在解除名单里——后者是写入方的 bug）。与「查不到状态就当成
                # 没在拦」相反：这里保持现状，让活行继续拦。一条解除不掉的声明看起来就
                # 像「本来就没声明」，而多拦一轮是吵闹但看得见的。
                logger.warning(
                    "[regime] 触发源 %s 的活行（作用域 %s）不在本轮的期望集里，"
                    "也不在解除名单里，按 fail-closed 原样留着",
                    trigger.value,
                    row.scope,
                )
                continue
            row.closed_at = at
            row.closed_reason = reason
            row.save(update_fields=["closed_at", "closed_reason"])
            summary["closed"] += 1
        elif row.pk is None:
            continue
        elif _rewrite(row, desired):
            summary["updated"] += 1
        else:
            summary["unchanged"] += 1

    for desired in planned.values():
        HaltDeclaration.objects.create(
            trigger=desired.trigger.value,
            scope=desired.scope,
            label=desired.label,
            opened_at=desired.opened_at,
            expires_at=desired.expires_at,
            reason=desired.reason,
            actor_kind=ACTOR_KIND.value,
            actor_name=actor_name,
        )
        summary["created"] += 1

    logger.info("[regime] 停止声明窗口同步：%s", summary)
    return summary


def _key(trigger, scope: str) -> tuple[str, str]:
    """唯一键的 Python 形态，与 `uniq_live_halt_declaration` 同一口径（触发源 × 作用域）。"""
    return (getattr(trigger, "value", trigger), scope)


def _rewrite(row: HaltDeclaration, desired: PlannedRow) -> bool:
    """把一行改写成期望的样子；**没变化就一个字段都不碰，返回 False**。

    不比对就写的话，每 300 秒会给每一行来一次无意义的 UPDATE——而这张表的
    `closed_at` / `closed_reason` 之外没有时间戳，看不出「它上次真的变了是什么时候」。
    比对 `opened_at` / `expires_at` 是安全的：两者都取自事实（事件的 `halt_at` /
    `resume_at`、判定的 `effective_at`），逐次重算逐字相同。

    **窗口起点被改写时，顺手把开窗通知的账清掉**（`opened_notified_at`，第②e 段）：
    那条消息讲的就是「从这一刻起开始拦」，起点变了它就过期了。不清的话，改期之后新起点
    到点不会有任何消息——用户手上留着一条已经不对的时刻，而机制以为通知过了。
    """
    moved = row.opened_at != desired.opened_at
    if (
        row.label == desired.label
        and row.reason == desired.reason
        and not moved
        and row.expires_at == desired.expires_at
    ):
        return False
    row.label = desired.label
    row.reason = desired.reason
    row.opened_at = desired.opened_at
    row.expires_at = desired.expires_at
    fields = ["label", "reason", "opened_at", "expires_at"]
    if moved:
        row.opened_notified_at = None
        fields.append("opened_notified_at")
    row.save(update_fields=fields)
    return True


def _close_reason(
    row: HaltDeclaration,
    at: datetime,
    candidates: list[MajorEvent],
    *,
    gate_close_reasons: dict[str, str] | None = None,
) -> str | None:
    """这一行为什么被解除；**``None`` = 本模块答不出来，调用方原样留着它**。

    四个码按「先看事实自己，再看是不是有人撤了它」排：

    - 保命档的行没了 → 只可能是阶段离开了高波动（它没有截止时刻）。
    - `expires_at <= at` → 窗口自己到点了。与 `halt.live_declarations` 的
      `expires_at__gt` 同一个取等号方向：**截止那一刻已不再拦**，所以那一刻解除。
    - 有一条**取消掉的**高影响事件压在这个作用域上、且它的窗口还没过 → 是这次取消导致的。
      加「窗口还没过」这一条，是为了不把「一件早就取消过、窗口也早过了的事」报成现在的原因。
    - 都不是 → 改期或改档（`HIGH` 降到 `MEDIUM`）之后不再覆盖此刻。

    这是**解释性**的字段，不是判据：判定只读 `closed_at`。所以对事件与保命档那两档，宁可
    归到一个说不清的码上，也不为了「报得准」去做第二次求值。

    **策略停用那一档不适用上面这条让步**：它为什么解除（阶段离开了 / 判据不成立了 / 有人
    豁免了 / 开关关了 / 策略离场了）是 `gate.derive` 判定出来的，本模块看不见阶段、豁免与
    判据，一个字都猜不出来——而这些码会原样进通知正文与日报，猜错就是给用户一句反话。
    所以 ``None`` 是这一档唯一诚实的答案：调用方跳过这一行，活行继续拦着。
    """
    if halt.trigger_of(row) in (
        # 策略档的两路（停用决策 / 按阶段暂停）同源：为什么解除（阶段离开了 / 判据不成立了
        # / 有人豁免了 / 开关关了 / 策略离场了）都是 `gate` 判定层算出来的，本模块看不见
        # 阶段、豁免与判据——所以它俩一起在这里取 `gate_close_reasons`。
        HaltTrigger.DEACTIVATION,
        HaltTrigger.REGIME_SUSPENSION,
    ):
        return (gate_close_reasons or {}).get(row.scope)
    if halt.trigger_of(row) is HaltTrigger.BLANKET:
        return CLOSE_REASON_REGIME_LEFT
    if row.expires_at is not None and row.expires_at <= at:
        return CLOSE_REASON_WINDOW_ENDED
    cancelled = any(
        event.status == EventStatus.CANCELLED.value
        and event.resume_at > at
        and touches(event, row.scope)
        for event in candidates
    )
    return CLOSE_REASON_EVENT_CANCELLED if cancelled else CLOSE_REASON_NO_LONGER_COVERS
