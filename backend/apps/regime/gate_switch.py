"""行情阶段 gate 的开关（第③段单元 ③b）：确认页 + 唯一写入函数。

`gate.py` 判、`gate_run.py` 取数与落库、本模块**翻开关**。三件套与事件熔断那一档
（`breaker_switch.py` + `/regime on|off` + `management/commands/event_breaker.py`）同构，
连确认页的形状都照抄——一个机制的开与关必须是**两个入口、同一种写法**，否则用户学到的是
「这一档的确认页长这样」，而不是「这个系统的确认页长这样」。

## 这个模块只做一件事：把档位翻过去，然后把声明表对到那个档位上

`flip_regime_gate(to_mode, …)` 是**唯一**能写 `MechanismKind.REGIME_GATE` 那一行的地方。
在 ③b 之前，全仓只有 `breaker_switch.flip_event_breaker` 一处
`RegimeMechanismSwitch.objects.create`，而它的 `KIND` 是写死的常量。本函数按顺序做两件事：

1. 档位不同 ⇒ 写一条 `RegimeMechanismSwitch` 流水。**这一行就是「机制开始拦」这个动作
   本身**：声明行的 `opened_at` 取它的 `at`，而不是 `now()`——只有一个存好的事实才能让
   每 300 秒重算一轮的期望值逐字稳定（`halt_sync._rewrite` 判「变了」就会把
   `opened_notified_at` 清掉，于是每轮重发一条「窗口已开启」）。档位本来就相同 ⇒ 不写，
   即「重复敲同一条命令不会在流水里留下一串一模一样的行」。
2. **无论档位变没变**，同步跑一次 `gate_run.sync(now=at)`，把声明表与决策行对到当前档位上。

第 2 步无条件跑，是本模块与 `flip_event_breaker` 最明显的差别，也是刻意的：`gate_run.sync`
是幂等的（期望值逐字来自库里存好的事实），「就地对一次账」永远不会写出第二条事实；换来的是
**这个人机入口同时也是一个「现在重算一轮」的按钮**。少了它，从 ③b 落地到 `regime-gate-sync`
那条定时任务接线之间，「阶段变了而表没跟上」只能靠等——而「打开的那一刻表就是对的」这件事
本来就是靠同一个动作里补这一次对账才成立的（只补写、不对账的话，阶段换过它也不管）。

顺带一个副作用值得写在这里：**阶段说不清时关掉 gate，活行本轮不会被解除**（`gate.derive`
的 `blocked` 分支排在 Shadow 分支之前，见它的 docstring）。那时再敲一次 `/regime gate off`
就会补上——第 2 步无条件跑，让这个「再敲一次」真的有用，而不是一句安慰。

## 五条纪律（这一层永远不会做的事）

1. **只碰 `trigger=DEACTIVATION` 这一档声明行。** 本模块经 `gate_run` →
   `halt_sync.sync(gate_plan=…)` 落行，而那一支只对账这一档（`sources=` 参数）。事件窗口
   与保命档各有自己的写入方，跨档写一行就是两个写方抢同一把唯一键。
2. **不碰 `Strategy.is_active`。** 「该跑哪些策略」由人改；机制只会声明「此刻别开新仓」。
   停用是**软**的：既有仓位不动、减仓照常放行。
3. **不碰事件熔断那一档的开关。** `KIND` 写成本模块的常量而不是 `flip_*(kind, …)` 的参数
   ——`breaker_switch` 那段 docstring 的理由在这里一字不差地成立：把档位做成参数，就是在
   邀请下一个调用点顺手打开一个还没接线的开关。
4. **永不 `DELETE`**。决策行是「机制曾经判过什么」的记录，删掉之后同一条判定会被当成新
   事实（`gate_run._refs` 的 docstring）；声明行的解除一律写 `closed_at`（`HaltDeclaration`
   的 docstring：解除只终结自己那一行）。
5. **一个字都不写豁免表。** 关闭 gate 是**撤防，不是收回人给的盾**：豁免是人按下的，要不要
   放开由人另说，所以关闭时在期豁免**原样留着**，重开时照旧按着。（「豁免在期就关掉声明行」
   是另一件事，而且方向相反——那是让豁免真的生效，不是撤回它。）

## 三种「打开也拦不住」，页面必须自己说出来

`gate_run` 的三种 `skipped` 与 `targets == 0` 都会让「打开之后会拦住 X 个」这句话变得可疑。
拦不住这件事本身不危险，**看起来像拦住了**才危险（日报照常、通知照常、日志照常），所以正文
里按成因各给一句后果，而且说的是后果、不是「没有数据」：

- `no_generation`：打开之后不会拦住任何东西，且与正常运行完全一样——先跑
  `recompute_regime_slices`。
- `cold_start` / `stale_state`：同样是拦不住，原因见 `deactivation.BLOCKED_DISPLAY`。
- `targets == 0` 且未被 skip：真的没什么可拦（当前阶段没有被判为不适配的被管策略）。
- `targets > 0` 但全在豁免期：一个都拦不住，因为人按着。

保命档（高波动）单独说一句：那一档下「该停」由保命档那一层负责，本机制不产出策略档声明，
所以「该停 0 个」不是「没什么可拦」。

## 关闭方向要说清：保命档没有开关

`halt.HALT_TRIGGER_SWITCH[HaltTrigger.BLANKET] is None`，而 `switch_open(None)` 恒真——
高波动那一层**没有**档位可翻。关掉 gate 对保命档毫无影响。正文与摘要里都单独写一句，
否则「关掉就安全了」是个会要命的误读。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from django.utils import timezone

from apps.regime import deactivation, deactivation_run, events, gate, gate_run, halt
from apps.regime.models import (
    ActorKind,
    HaltDeclaration,
    HaltTrigger,
    MechanismKind,
    MechanismMode,
    RegimeMechanismSwitch,
)
from apps.regime.quant import BaseRegime

logger = logging.getLogger(__name__)

#: 本模块管的档。**常量而不是参数**，理由见模块 docstring 第 3 条。
KIND = MechanismKind.REGIME_GATE

__all__ = [
    "KIND",
    "Briefing",
    "Confirmation",
    "Flip",
    "GATE_CLOSE_REASON_DISPLAY",
    "closing_summary",
    "confirmation",
    "flip_regime_gate",
    "gate_close_reason_display",
    "page",
    "reconcile_warning",
]


# --------------------------------------------------------------------------- #
# 解除原因的词表（策略停用这一档专有）
# --------------------------------------------------------------------------- #

#: 声明行被解除的五个原因。
#:
#: 取值用 `gate.CLOSE_*` 而不是在这里写字符串字面量：**算得出这五个码的是
#: `gate._close_reason`**（纯函数，`close_reasons` 的键与值都从那里来），本模块只负责
#: 把它们翻成人话。词表放这里而不放 `halt_sync`：这五个词只说策略停用一件事，
#: `halt_sync` 一个字都不该知道（它的 docstring 明写「这一层的名字与词表只说窗口」）。
#:
#: **`regime_left` 与 `halt_sync.CLOSE_REASON_REGIME_LEFT` 同名不同义**：那一个是保命档的
#: 「阶段已离开高波动」，本档的是「这条决策行记的阶段已不是当前阶段」。同名是两条线各说
#: 各的话的巧合，所以展示名各写各的——也正因为如此，`halt_notify` 必须**按 trigger 路由**
#: 到这里的词表，不能拿 `halt_sync.close_reason_display` 一把抓（它会原样返回认不出的码，
#: 于是保命档那边的「阶段已离开高波动」会被贴到一条策略档声明上）。
GATE_CLOSE_REASON_DISPLAY: Mapping[str, str] = {
    gate.CLOSE_REGIME_LEFT: "该决策行记的阶段已不是当前阶段",
    gate.CLOSE_BECAME_FIT: "该策略在本阶段重新适配",
    gate.CLOSE_EXEMPTED: "人工豁免在期",
    gate.CLOSE_GATE_CLOSED: "行情阶段 gate 已回 Shadow（机制不再拦）",
    gate.CLOSE_STRATEGY_GONE: "策略已不在被管集合里",
}


def gate_close_reason_display(reason: str) -> str:
    """原因码 → 人话。**认不出来就原样返回**（与 `halt_sync.close_reason_display` 同一口径）。

    认不出就原样返回，是为了让「词表少了一条」表现为「消息里出现一个短码」，而不是
    「消息里出现一句错的解释」。
    """
    return GATE_CLOSE_REASON_DISPLAY.get(reason, reason)


#: 渲染顺序：最常见、最能解释「为什么本轮忽然少了一批行」的排前面。**固定顺序而不是按
#: 字典序**，是为了让同一组解除在任何数据下都逐字写成同一行——页面上那行数逐字稳定，
#: 「今天和昨天有什么不同」才读得出来。认不出的码（词表少了一条）排在最后。
_CODE_ORDER = (
    gate.CLOSE_REGIME_LEFT,
    gate.CLOSE_BECAME_FIT,
    gate.CLOSE_EXEMPTED,
    gate.CLOSE_STRATEGY_GONE,
    gate.CLOSE_GATE_CLOSED,
)


def _code_sort_key(item: tuple[str, int]) -> tuple[int, str]:
    code = item[0]
    return (_CODE_ORDER.index(code) if code in _CODE_ORDER else len(_CODE_ORDER), code)


# --------------------------------------------------------------------------- #
# 取数与渲染
# --------------------------------------------------------------------------- #


def _regime_display(regime: str | None) -> str:
    """slug → 中文展示名；取不到就原样回显 slug，**不猜**（与 `report._regime_display`、
    `gate._regime_display` 同一口径，各写一遍是那两处的既定惯例：本层要 import 它们就得
    连 ORM 或判定层一起拖进来）。"""
    if not regime:
        return "（无）"
    try:
        return BaseRegime(regime).display
    except ValueError:
        return str(regime)


def _skipped_display(code: str) -> str:
    """`gate.GatePlan.blocked` 的人话。没有当前代、阶段说不清、保命档，三种都要说得出原因。

    最后那个不是「说不清」，是「说得很清楚、但这一层不该动手」（`gate.BLOCKED_BLANKET`），
    所以它的话来自本模块的 `_BLANKET_HOLD` 而不是 `deactivation.BLOCKED_DISPLAY`——那张表
    是取数层的收场词表，收的是「取不到数」的两种原因。
    """
    if code == gate.BLOCKED_BLANKET:
        return _BLANKET_HOLD
    if code == deactivation_run.SKIPPED_NO_GENERATION:
        return gate_run.NOTE_NO_GENERATION
    return deactivation.BLOCKED_DISPLAY.get(code, code)


@dataclass(frozen=True)
class Confirmation:
    """确认页上的每一个数。**全部出自同一次 `gate_run.preview`**（取数口只有一个）。

    「一次求值」是这一层的全部要点：页面上每个数与真正那一轮落库时报的是同一次判定，
    而不是另写一套「会拦住几个」的算法——两处判据分叉的表现是「页面上说 3 个、实际拦了
    5 个」，而两边看起来都正常。
    """

    at: datetime
    #: 开关**此刻**的档位（不是假设的那个）。
    mode: MechanismMode
    #: 页面上那些「会拦住」的数是**假设此刻打开**算出来的（此刻还没开）。
    as_if_open: bool
    #: 开关「开」的那一刻；没开时为 `None`（声明行的 `opened_at` 取它）。
    switch_at: datetime | None
    regime: str | None
    regime_display: str
    regime_effective_at: datetime | None
    #: 当前阶段是保命档（高波动）。那一层没有开关，本机制在这期间不产出策略档声明
    #: （`gate.derive` 的第三条早退分支把这句话变成了结构）。注意它说的是**阶段**，
    #: 「这一轮被保命档挡下」是另一件事——见 `blanket_hold`。
    blanket: bool
    #: 上一有效判定距今几个自然日；冷启动与没有判定时为 `None`。
    age_days: int | None
    #: 当前代池化表的口径版本（人话里的「哪一代」）与落地时刻。
    generation_version: int | None
    generation_at: datetime | None
    managed: int
    running: int
    #: 当前阶段上「该停」的条数，**含**在期豁免的。
    targets: int
    #: 其中在人工豁免期按着的条数——打开也拦不住。
    exempt: int
    #: 本轮会写/刷新的声明条数（= `targets - exempt`）——**只数策略停用决策那一档**
    #: （`gate.TRIGGER_DEACTIVATION`）。issue #51 之后 `GatePlan.declarations` 是**两路之和**，
    #: 一锅端地数会让下面那句「停止声明（策略停用决策档）：本轮写/刷新 N 条」把暂停路的行
    #: 算进停用档，而多出来的那个数在这张页面上没有对应的「该停」可对（`targets - exempt`
    #: 当场不成立）——一句话里同时出现两个互相矛盾的口径，读的人只会以为取数坏了。
    declared: int
    #: 本轮会写/刷新的**暂停路**声明条数（`gate.TRIGGER_REGIME_SUSPENSION`，issue #51）：
    #: 当前生效阶段逐个被管策略判适用性，判为不适配或判不了的各一条。与 `declared` **分开
    #: 数**：两档的解除方式不同（一个要人工恢复、一个随阶段自动翻转），加起来报就是替用户
    #: 把「要人管的事」与「自己会翻回去的事」混成一件——而这两句话的处置完全不同。
    suspended: int
    #: 声明表里此刻活着的、`trigger=DEACTIVATION` 的行数。
    live_declarations: int
    #: 本轮会被解除的**活行**数，按原因码分布，顺序见 `_CODE_ORDER`。**只数活行**：plan 的
    #: 解除名单是按**决策行**给的（在期那份覆盖全部决策行），拿它的规模去报「解除 N 条」
    #: 会报出一个远比表里真实行数大的数。
    closing_by_code: tuple[tuple[str, int], ...]
    #: 本轮之后仍然活着、且**不在解除名单里**的行数：对账会按 fail-closed 原样留着它们并
    #: 打一条 warning。正常一轮是 0——活行要么被刷新要么被解除。**两个分支下是同一个集合**
    #: （活行与决策行对不上：决策行被 `cleanup_ghost_strategies` 物理删掉时 CASCADE 走了，
    #: 而声明行按作用域字符串自立，不会跟着走），所以 `skipped` 时也照报。
    orphan: int
    #: `gate.GatePlan.blocked`：非空 ⇒ 本轮一个字都没动。空串 = 正常一轮。
    #: 取值是 `deactivation.BLOCKED_*` / `deactivation_run.SKIPPED_NO_GENERATION` /
    #: `gate.BLOCKED_BLANKET`（最后那个的理由与前几个不同：不是说不清，是不动手）。
    skipped: str

    @property
    def closing(self) -> int:
        return sum(count for _, count in self.closing_by_code)

    @property
    def skipped_display(self) -> str:
        return _skipped_display(self.skipped)

    @property
    def blanket_hold(self) -> bool:
        """这一轮**被保命档挡下**（`gate.derive` 的第三条早退分支）。

        与 `blanket` 不是同一个问题：`blanket` 说的是**阶段**（当前是不是高波动），这一条
        说的是**这一轮的结果**。两者在「开关开着 + 高波动」时才同时为真；开关关着时走的是
        Shadow 那一支（`blanket` 真而 `blanket_hold` 假）——那时按 `gate_closed` 解除活行
        是**真话**（机制确实不再拦），所以两支不能混。

        页面上分岔的只有「为什么这一轮不动」那几句：保命档不是「算不出该停哪些」，说成
        后者会让用户去查一条根本不存在的取数故障。
        """
        return self.skipped == gate.BLOCKED_BLANKET


def confirmation(*, now: datetime | None = None) -> Confirmation:
    """把「此刻打开会做什么」取全，**只读**。确认页与摘要的唯一取数口。

    `gate_run.preview(now=at)` 已经把「假设此刻打开」钉成与取数同一个时刻（那是它存在的
    全部理由）。本函数额外做的只有一件事：去声明表里数**此刻活着**的策略档行——那一栏
    是页面上唯一不在 `GatePlan` 里的数（`GatePlan` 说的是「这一轮要什么」，不是「表里现在
    有什么」），而人按 `off` 之前最需要看到的正是它。
    """
    at = now or timezone.now()
    round_ = gate_run.preview(now=at)
    plan = round_.plan

    # 活行的解除原因：`plan.close_reasons` 的键是**策略 id**（判定层的口径），而本表的
    # 唯一键另一半是 `strategy:<id>` 作用域。换算口是 `halt.strategy_scope`，与
    # `halt_sync._gate_planned` 用的是同一个函数——不是本模块另发明的第二套写法。
    reason_of = {
        halt.strategy_scope(strategy_id): code
        for strategy_id, code in plan.close_reasons.items()
    }
    desired = {
        halt.strategy_scope(declaration.strategy_id)
        for declaration in plan.declarations
    }
    live_scopes = list(
        HaltDeclaration.objects.filter(
            trigger=HaltTrigger.DEACTIVATION.value, closed_at__isnull=True
        ).values_list("scope", flat=True)
    )
    counts: dict[str, int] = {}
    orphan = 0
    for scope in live_scopes:
        code = reason_of.get(scope)
        if code is not None:
            counts[code] = counts.get(code, 0) + 1
            continue
        if scope not in desired:
            orphan += 1

    gate_open = round_.gate_open and not round_.hypothetical

    # 两路**按触发源分开数**（issue #51）：`plan.declarations` 是停用路与暂停路之和，而本页
    # 那两句各说自己那一档（一句是「策略停用决策档」、一句是「按阶段暂停开仓」）。判据取
    # `gate.TRIGGER_DEACTIVATION` 这个常量而不是本地字面量：写入方（`gate.Declaration.trigger`）
    # 用的就是它，抄一遍等于给「这一行是哪一路产出的」造第二个答案。
    #
    # **认不出的第三路不进这一栏**（而不是按 `len(...) - suspended` 把它塞给停用档）：停用档
    # 那一栏的数与「该停 M 个」成对出现，把一个来路不明的行算进去，页面上就多出一个没人能
    # 解释的数——而它看起来完全正常。
    declared = sum(
        1 for row in plan.declarations if row.trigger == gate.TRIGGER_DEACTIVATION
    )
    # 暂停路那一个数**不在这里重算**：`Round.suspensions` 已经是它唯一的换算口（`gate_run.py`，
    # 同一份 `plan` 上的同一条判据，Celery 摘要与日志用的也是它）。再写一遍求和就是给同一个数
    # 造第二个答案，而两处将来分叉的表现是「页面上说 3 条、流水的摘要里说 5 条」，两处都正常。
    suspended = round_.suspensions

    return Confirmation(
        at=at,
        mode=MechanismMode.EXECUTING if gate_open else MechanismMode.SHADOW,
        as_if_open=round_.hypothetical,
        switch_at=round_.switch_at,
        regime=round_.state.regime,
        regime_display=_regime_display(round_.state.regime),
        regime_effective_at=round_.state.effective_at,
        blanket=round_.state.blanket,
        age_days=round_.age_days,
        generation_version=(
            round_.generation.pool_version if round_.generation is not None else None
        ),
        generation_at=(
            round_.generation.finished_at if round_.generation is not None else None
        ),
        managed=len(round_.managed.ids),
        running=len(round_.managed.running),
        targets=round_.targets,
        exempt=round_.exempt,
        declared=declared,
        suspended=suspended,
        live_declarations=len(live_scopes),
        closing_by_code=tuple(sorted(counts.items(), key=_code_sort_key)),
        orphan=orphan,
        skipped=plan.blocked,
    )


# --- 渲染 ------------------------------------------------------------------- #

_OPENING = "行情阶段 gate · 上线确认（这一步本身不改变任何东西）"
_CLOSING = "行情阶段 gate · 下线确认（这一步本身不改变任何东西）"
_BLANKET_NOTE = (
    "当前阶段是保命档（高波动）：**它期间的全场停用由保命档那一层负责**，与这个开关无关"
    "（那一层没有开关，高波动算生效）。所以下面「该停」一栏是 0，那不是「没什么可拦」。"
)
#: `_BLANKET_NOTE` 在**保命档挡下那一轮**的替身。原话指着版面说（「下面『该停』一栏」），
#: 而那一轮判定层的第三条早退分支把「该停」那一栏整个抽掉了——照原话说，读的人会去找一个
#: 不在页面上的东西。两句的差别只在最后那一句：前半句（保命档没有开关）两处都成立。
_BLANKET_NOTE_HOLD = (
    "当前阶段是保命档（高波动）：**它期间的全场停用由保命档那一层负责**，与这个开关无关"
    "（那一层没有开关，高波动算生效）。所以本层这一轮一个字都不会动——"
    "那不是「没什么可拦」，是「这一层不该动手」。"
)
#: `_BLANKET_NOTE` 的**摘要版**：同样的一句话要进 `RegimeMechanismSwitch.reason`，而那里
#: 收不下 markdown 强调符、也收不下换行。措辞对两个方向都成立（「与这个开关无关」在打开
#: 与关闭方向上都是同一件事实），所以一个常量用在三处，省得三份措辞各自漂。
_BLANKET_SUMMARY = "保命档（高波动）那一层没有开关：它期间的全场停用与 gate 开关无关"
#: 保命档挡下的那一轮，`skipped` 一栏要说的话。与 `gate_run.NOTE_NO_GENERATION` 占同一格
#: （`_skipped_display` 的入参是 `gate.GatePlan.blocked`），但两句说的是两件相反的事：
#: 那一句是「没法问」，这一句是「问得清、这一层就是不动手」。所以不共用一句话——用户按
#: 「没法问」去查取数层，查到的会是一条根本不存在的故障。
_BLANKET_HOLD = (
    "保命档（高波动）期间本层不做任何解除——那些决策行记的阶段并没有离开，"
    "只是被叠了一层高波动"
)
_SHADOW_NO_SWITCH = (
    "⚠️ 关掉 gate 对保命档（高波动）毫无影响：那一层没有开关可翻。"
    "「关掉就安全了」在这里是错的——上面那段仍然是关掉之后的实情。"
)


def _mode_line(data: Confirmation) -> str:
    if data.mode is MechanismMode.EXECUTING:
        return "当前档位：执行态｜下面这些数是这一刻**真实在拦什么**"
    head = f"当前档位：{MechanismMode.SHADOW.display}"
    if data.as_if_open:
        return f"{head}｜此刻还没开——下面这些数是「假设此刻打开」算出来的"
    return f"{head}｜机制此刻不拦任何人；下面这些数是它「如果开着」会拦的"


def _regime_line(data: Confirmation) -> str:
    if data.regime is None:
        return "当前阶段：说不清（还没有过生效判定）"
    line = f"当前阶段：{data.regime_display}"
    if data.regime_effective_at is not None:
        line += f"（判定生效于 {events.format_moment(data.regime_effective_at)}）"
    return line


def _generation_line(data: Confirmation) -> str:
    if data.generation_version is None:
        return "当前代池化表：一代都还没有"
    return (
        f"当前代池化表：口径 v{data.generation_version}"
        f"（落地于 {events.format_moment(data.generation_at)}）"
    )


def _targets_line(data: Confirmation) -> str:
    if data.mode is MechanismMode.EXECUTING:
        verb = "此刻在拦"
    elif data.as_if_open:
        verb = "打开之后会拦住"
    else:
        verb = "机制此刻不拦人；若开着会拦住"
    return (
        f"{verb}：{data.declared} 个策略——当前阶段该停 {data.targets} 个，"
        f"其中 {data.exempt} 个在人工豁免期（人按着，拦不住）"
    )


def _suspension_line(data: Confirmation) -> list[str]:
    """暂停路的条数（issue #51）。**独立一行**——它与上一条「该停」不是一笔账。

    上一条数的是停用路（`targets - exempt`），而暂停路拦的人**不来自任何决策行**（它连
    `DeactivationDecision` 都不产出）。并进上一条的话，那个「会拦住 N 个策略」会为一批没有
    「该停」记录可对的策略变大，读的人拿「该停 M 个」一对，就会以为取数坏了。

    所以这一行自己报自己的条数，并**当场把两档的差别说出来**（随阶段自动翻转、不需要人工
    恢复）：这正是它存在的理由——用户看到又一批策略被拦住时，第一个要问的就是「要不要我
    去恢复它」，而这两档的答案相反。

    `suspended == 0` 时整行不出现：暂停路一条都不产出是常态（每个被管策略在当前阶段都有
    `fit` 结论时），每轮都印一句「0 条」会让它变成背景噪声，而这一行是要被读到的。
    """
    if data.suspended == 0:
        return []
    return [
        f"按阶段暂停开仓：本轮写/刷新 {data.suspended} 条"
        "（随阶段自动翻转，不需要人工恢复）"
    ]


def _no_effect_lines(data: Confirmation) -> list[str]:
    """「一个都拦不住」的四个成因，各说各的后果。见模块 docstring。

    **两条早退都要先看暂停路**（issue #51）：`targets` / `exempt` 只说停用那一档的数，而暂停
    路的行一个都不从这里来——两路合并之前，「该停 0 个」确实等于「一个都不会拦」，现在不等于
    了。所以暂停路有声明时这两句**一句都不说**：那一档此刻可能正拦着一批策略，而「⚠️ 一个
    都不会拦」在这种情况下是一句漏掉一半事实的假话。

    这里**不补兜底句子**（例如「但暂停路还在拦」）：那一件事已经由 `_suspension_line` 自己
    那一行说完，同一句话写两遍会让人去找它们的差别。这一处只负责把那句假话收回去。
    """
    if data.blanket:
        return []
    if data.suspended == 0 and data.targets == 0:
        return [
            "⚠️ 一个都不会拦：当前阶段没有被判为不适配的被管策略（这是真的没什么可拦）"
        ]
    if data.suspended == 0 and data.exempt == data.targets:
        return [f"⚠️ 一个都不会拦：该停的 {data.targets} 个全在人工豁免期"]
    return []


def _declaration_line(data: Confirmation) -> str:
    if data.skipped:
        # 「原因见上」而不是把那条句子再抄一遍：上一行刚说过同一个原因，而一个页面上
        # 同一句话出现两次，读的人会开始找「这两处说的是不是同一件事」。
        return (
            f"停止声明（策略停用决策档）：此刻活着 {data.live_declarations} 条，"
            "本轮一条都不动（原因见上）"
        )
    line = (
        f"停止声明（策略停用决策档）：此刻活着 {data.live_declarations} 条；"
        f"本轮写/刷新 {data.declared} 条、解除 {data.closing} 条"
    )
    if data.closing_by_code:
        line += (
            "（"
            + "、".join(
                f"{gate_close_reason_display(code)} {count} 条"
                for code, count in data.closing_by_code
            )
            + "）"
        )
    return line


def _blocking_note(data: Confirmation) -> str:
    """那些**没被解除**的活行还拦不拦人——**只有档位说了算**。

    这一句是 `gate.derive` 的 blocked-before-shadow 那处顺序在页面上的落点：阶段说不清时，
    它的 blocked 分支排在 Shadow 分支之前，于是**即使档位是执行态、即使你刚敲了 `off`**，
    活行也不会被解除。写作「它们已经不拦人了」会是假话——`halt.blocking_declarations` 是
    开关感知的，开关开着这些行就照样拦人。所以这一句必须按档位分岔，而不是按「你要关掉」
    这件事分岔：**关掉是一件已经请求、但这一刻还没有生效的事**。

    正文与摘要**共用这一句**（摘要那一侧还有一条「不出现 markdown 强调符」的硬要求：
    它的收件人可能是纯文本客户端，`**粗体**` 在那里会原样渲染成星号）。所以这里只用一个
    emoji 做强调，不写 `**`——两处各写一句的话，档位判据就有了两个答案。

    **保命档挡下的那一轮**（`blanket_hold`）档位判据一字不改，换掉的只有后半句：那一轮的
    「不动手」不是取数出了问题，而是暂时的（高波动一过，自动对账就把表对干净了），所以
    补救动作从「先让阶段说清楚、再敲一次」变成「等下一轮」。
    """
    if data.mode is MechanismMode.EXECUTING:
        if data.blanket_hold:
            # 保命档挡下的那一轮：档位判据一字不改（它们照样在拦人），但补救动作换了——
            # 「先让阶段说清楚再敲一次」在保命档下是个空动作（阶段已经很清楚），而这一轮的
            # 不动手本来就是暂时的：高波动一过，下一轮自动对账会把表对干净。
            return (
                "⚠️ 但档位还是执行态，所以它们仍然在拦人——活行等保命档过去之后由下一轮"
                "自动对账（每轮都会跑，不必再敲本命令）"
            )
        return (
            "⚠️ 但档位还是执行态，所以它们仍然在拦人——要把它们解干净得先让阶段说清楚"
            "（算不出阶段时本轮连对账都不发起：那是「保持现状」而不是「按空集解除」，"
            "与保命档同一取向）；阶段说清楚之后再敲一次本命令才会补上"
        )
    if data.blanket_hold:
        return "它们已经不拦人了（档位本来就是 Shadow）；活行等保命档过去之后由下一轮自动对账"
    return (
        "它们已经不拦人了（档位本来就是 Shadow）；"
        "阶段说清楚之后再敲一次本命令会把声明表对干净"
    )


def _closing_declaration_line(data: Confirmation) -> str:
    if data.skipped:
        return (
            f"停止声明（策略停用决策档）：此刻活着 {data.live_declarations} 条，"
            f"本轮一条都不会被解除（{data.skipped_display}）；"
            f"{_blocking_note(data)}"
        )
    tail = ""
    if data.orphan:
        # 「全部」这句话有一个例外，而且只有那一个：活行对不上任何决策行。正常一轮是 0，
        # 所以平时这句不出现；一旦出现，就不能再说「全部」。
        tail = (
            f"（其中 {data.orphan} 条对不出决策行，会按 fail-closed 原样留着，"
            "本轮日志里有一条对应的 warning）"
        )
    return (
        f"停止声明（策略停用决策档）：此刻活着 {data.live_declarations} 条，"
        f"本轮按「行情阶段 gate 已回 Shadow」解除{tail}——此后它们不再拦人"
    )


def _orphan_line(data: Confirmation) -> list[str]:
    """活行既不在期望集里、也不在解除名单里。

    正常一轮不会出现（活行要么对得上一条决策行，要么就是幽灵）。出现时它不是「有个 bug
    在处理中」，而是一个**没人会再管它**的活行：决策行被物理删掉之后，声明行没有跟着走，
    于是它既不会被刷新、也不会被解除，而它照样拦人（开关开着的时候）。所以这句话是给
    人工核对的，不是给日志的。
    """
    if data.orphan == 0:
        return []
    return [
        f"⚠️ 其中 {data.orphan} 条**对不出任何决策行**（决策行被物理删掉了，声明行按作用域"
        "字符串自立、不会跟着走）——对账会按 fail-closed 原样留着它们（本轮日志里有一条"
        "对应的 warning），所以它们既不会被刷新也不会被解除，而开关开着的时候照样拦人。"
        "请人工核对停止声明表。"
    ]


def _render_body(data: Confirmation, *, closing: bool) -> str:
    """确认页正文。`closing` 只改两处**方向相关的措辞**，数字全部照旧。

    两个方向共用一份取数结果（同一个 `Confirmation`），因为同一时刻的同一张表只有一个
    样子；会因方向而不同的只有「接下来这一轮打算对它做什么」，而那句话必须说得准——
    打开方向报的是「会拦住谁」，关闭方向报的是「不再拦谁」，把前者的句子印在关闭的页面上
    就是一句假话。
    """
    lines: list[str] = [
        _CLOSING if closing else _OPENING,
        _mode_line(data),
        _regime_line(data),
    ]
    if data.blanket:
        # 保命档挡下那一轮（`blanket_hold`）要换一句话：原话指着「该停」那一栏说，而那一轮
        # 判定层的第三条早退分支把整栏抽掉了——照原话说，读的人会去找一个不在页面上的东西。
        lines.append(_BLANKET_NOTE_HOLD if data.blanket_hold else _BLANKET_NOTE)
    if closing:
        lines.append(_SHADOW_NO_SWITCH)
        if data.mode is MechanismMode.EXECUTING:
            lines.append(
                "关闭之后不再拦任何人：现在被拦下的策略，开仓会被放行"
                "（减仓本来就放行，停用是软的——既有仓位不动）"
            )
        else:
            # 已经关着的时候，「关掉之后会怎样」是一句空话——这一步真正改变的是表，不是
            # 拦不拦。所以这一支只描述**这个动作本身**，而「那些活行现在什么状态」留给
            # 紧跟其后的那一行（它按归档位说话，两处说同一件事会让人去找它们的差别）。
            lines.append(
                "档位本来就是 Shadow：这一步不会改变「拦不拦」，只是把声明表对到它已经在的档位上"
            )
        # 回 Shadow 之后声明表会怎么收场，用的是 `gate.NOTE_SHADOW` 的原文而不是在这里
        # 复述一遍：那句话是「Shadow 档下活行怎么办」的唯一答案，抄一遍就有了两个答案。
        lines.append(f"回到 Shadow 之后：{gate.NOTE_SHADOW}")
        lines.append(_closing_declaration_line(data))
        lines.extend(_orphan_line(data))
        lines.append("操作：/regime gate off 关掉（回到 Shadow）")
        return "\n".join(lines)

    lines.append(f"被管策略：{data.managed} 个（其中实盘在跑 {data.running} 个）")
    lines.append(_generation_line(data))
    if data.skipped:
        lines.append(f"⚠️ 本轮一个字都不会动：{data.skipped_display}")
    else:
        lines.append(_targets_line(data))
        lines.extend(_suspension_line(data))
        lines.extend(_no_effect_lines(data))
    lines.append(_declaration_line(data))
    lines.extend(_orphan_line(data))
    lines.append(
        "操作：/regime gate on 打开（执行态） ／ /regime gate off 关掉（回到 Shadow）"
    )
    return "\n".join(lines)


def _render_summary(data: Confirmation) -> str:
    """打开方向的摘要。**它就是要写进 `RegimeMechanismSwitch.reason` 的那句话**，
    所以：纯文本、不换行、不出现 markdown 强调符（收件人可能是即时消息）。"""
    if data.skipped:
        why = (
            "打开的一刻是保命档（高波动）：本层不产出策略档声明，也不解除任何东西"
            if data.blanket_hold
            else f"打开的一刻算不出「该停哪些」——{data.skipped_display}"
        )
        return (
            f"人工打开行情阶段 gate（上线确认）：{why}；"
            f"此刻活着的策略档声明 {data.live_declarations} 条一个字都没动"
            + (f"；{_BLANKET_SUMMARY}" if data.blanket else "")
        )
    parts = [
        "人工打开行情阶段 gate（上线确认）",
        f"当前阶段 {data.regime_display}",
        f"该停 {data.targets} 个（其中 {data.exempt} 个在人工豁免期）",
    ]
    if data.blanket:
        parts.append(_BLANKET_SUMMARY)
    parts.append(f"写/刷新声明 {data.declared} 条")
    if data.suspended:
        # 暂停路的条数也进摘要（issue #51），措辞与页面那一行同源。这一句的理由与 `closing`
        # 那一句相同：摘要是**这次切换唯一的记录**（它写进 `RegimeMechanismSwitch.reason`），
        # 只报停用路的话，事后读流水的人会把「这一轮写了两条」读成一条。而漏掉的那一条恰恰是
        # 「不用人管」的那一档，看起来像机制少做了事。
        # `suspended == 0` 时不写：那是最常见的一轮，每行流水挂一句「0 条」是噪声。
        parts.append(f"按阶段暂停开仓 {data.suspended} 条")
    if data.closing:
        parts.append(f"解除活行 {data.closing} 条")
    parts.append(
        f"上一有效判定距今 {data.age_days} 天"
        if data.age_days is not None
        else "还没有过生效判定"
    )
    return "；".join(parts) + "。"


def closing_summary(data: Confirmation) -> str:
    """关闭方向的摘要（同样要写进 `reason`）：**收一份已经算好的快照**，不自己再查一次。

    与 `breaker_switch.closing_summary` 同一签名、同一理由：关闭动作要用它当流水行的
    `reason`，而流水行必须在开关翻过去之前就写好——所以它只能来自翻之前那一次求值。
    """
    blanket = f"；{_BLANKET_SUMMARY}" if data.blanket else ""
    if data.skipped:
        why = (
            "关闭的一刻是保命档（高波动）：本层不做任何解除，"
            f"此刻活着的策略档声明 {data.live_declarations} 条原样留着"
            if data.blanket_hold
            else "关闭的一刻算不出「该停哪些」——"
            f"{data.skipped_display}，此刻活着的策略档声明 {data.live_declarations} 条"
            "本轮一条都不会被解除"
        )
        return (
            f"人工关闭行情阶段 gate（回到 Shadow）：{why}；{_blocking_note(data)}；"
            "人工豁免不受影响（关闭是撤防，不收回人给的豁免）" + blanket
        )
    tail = f"，其中 {data.orphan} 条对不出决策行、会原样留着" if data.orphan else ""
    return (
        f"人工关闭行情阶段 gate（回到 Shadow）：此刻活着的策略档声明 "
        f"{data.live_declarations} 条按「行情阶段 gate 已回 Shadow」解除{tail}；"
        "人工豁免不受影响（关闭是撤防，不收回人给的豁免）；"
        f"关闭时阶段 {data.regime_display}，该停 {data.targets} 个"
        f"（其中 {data.exempt} 个在人工豁免期）" + blanket
    )


@dataclass(frozen=True)
class Briefing:
    """一次求值 + 两个方向的正文。与 `breaker_switch.Briefing` 同形。"""

    data: Confirmation
    body: str
    summary: str


def page(*, closing: bool = False, now: datetime | None = None) -> Briefing:
    """确认页。**单一求值点**：`data` 只算一次，正文与摘要都从它渲染。

    `closing=True` 给关闭方向用（正文换成「不再拦谁」那一套）。它**不是**第二次求值：
    同一时刻的表只有一个样子，两个方向只是对同一个样子说不同的话。
    """
    at = now or timezone.now()
    data = confirmation(now=at)
    return Briefing(
        data=data,
        body=_render_body(data, closing=closing),
        summary=closing_summary(data) if closing else _render_summary(data),
    )


def reconcile_warning(data: Confirmation, sync: Mapping[str, Any]) -> str | None:
    """「档位翻了，但表没对上」那句话；对上了返回 `None`。两个人工入口共用这一句。

    为什么这句话必须存在：`Confirmation` 里没有「这一步正压在什么东西上」那样的字段，
    gate 的敞口不在窗口上，而在**声明表没被对干净**上——阶段说不清时 `gate_run.sync` 走
    blocked 那一支（见 `gate_run.sync` 的 docstring），本轮连对账都不发起，活行一条都
    不会被解除。开关一关它们就不拦人了（`halt.blocking_declarations` 是开关感知的），
    所以此刻不是危险，是**不干净**；而「把同一次动作再敲一遍」正是能把它补上的动作，
    不说出来没人知道要敲。

    **按翻完之后的事实说**：正文里那句 `_blocking_note` 是在翻之前渲染的，它说的
    「仍然在拦人」在关掉之后就不再成立，抄过来会是一句假话。所以「那些活行此刻拦不拦人」
    取 `sync["gate_open"]`——**翻完之后**那个档位，而不是翻的方向。「翻一次就是把档位翻
    过去」在关掉方向上恰好让这两者同向，在打开方向上不是（`on` 之后表里的活行照样拦人，
    而那正是这里要让用户知道的事），所以判据只能取那次同步报回来的事实。

    保命档挡下的那一轮也会露出这句话（`skipped` 非空同样是它），但结论不同：那一轮活行
    原样留着**正是目的**，所以补救动作不是「再敲一次」，而是等保命档过去之后由自动对账
    接手——这一支必须说得出这个差别，否则用户会去敲一个敲不出名堂的命令。

    写在渲染层而不是各入口各拼一遍：CLI（`manage.py regime_gate`）与 `/regime gate`
    说的是**同一件事**，两处各写一句就是给「此刻表干不干净」造两个说法（第②f 段 Q8
    的同源纪律，本段一字不差地沿用）。

    Args:
        data: **翻之前**那一次求值的快照——`skipped` 与活行数都从这里来。
        sync: `Flip.sync`（`gate_run.sync` 的摘要）。`skipped` 非空 = 本轮没对账。
    """
    skipped = sync.get("skipped")
    if not skipped or not data.live_declarations:
        return None
    blocking = (
        "它们此刻照样拦人（开关是开着的）"
        if sync.get("gate_open")
        else "它们此刻已经不拦人了（开关是关的）"
    )
    if data.blanket_hold:
        return (
            f"⚠️ 档位已经切过去；这一轮是保命档（高波动），本层不做任何解除："
            f"{data.live_declarations} 条活声明原样留着。{blocking}，"
            "但表里还有它们——保命档过去之后下一轮会自动把表对干净（不必再敲一次）。"
        )
    return (
        f"⚠️ 档位已经切过去，但这一轮算不出「该停哪些」（{data.skipped_display}）："
        f"{data.live_declarations} 条活声明原样留着。{blocking}，"
        "但表里还有它们——阶段说清楚之后把同一次动作再敲一遍，表就会对干净。"
    )


# --------------------------------------------------------------------------- #
# 翻开关（唯一写入点）
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Flip:
    """一次开关动作的产物。

    `row is None` = 本来就是这一档，**没有写第二条流水**（对账照样跑过了）。调用方要
    分清「真的翻了一次」与「只是对了一次账」：前者有 `at`/`from_mode` 可回显，后者只有
    对账摘要。
    """

    row: RegimeMechanismSwitch | None
    #: `gate_run.sync` 的摘要——这一轮**真的做了什么**（它是事实，不是预览）。
    sync: dict[str, Any]

    @property
    def changed(self) -> bool:
        return self.row is not None


def flip_regime_gate(
    to_mode: MechanismMode | str,
    *,
    actor_kind: ActorKind | str,
    actor_name: str,
    reason: str,
    now: datetime | None = None,
) -> Flip:
    """把行情阶段 gate 翻到 `to_mode`，**并在同一个动作里对一次账**。

    Args:
        to_mode: `MechanismMode.EXECUTING`（打开）/ `SHADOW`（关掉，回到只记录）。
        actor_kind: 触发方类别（人工入口传 `ActorKind.CLI` 或 `CHAT`）。
        actor_name: 触发方（一律来自消息/命令行的人，不接受空）。
        reason: 切换原因。落进 `RegimeMechanismSwitch.reason`（`TextField`，必填）——
            确认页的摘要正是给人用的那句话，`actor_name` 之外的「为什么」全在这里。
        now: 注入时钟。**与取确认页时必须是同一个时刻**：声明行的 `opened_at` 取自这一行
            流水的 `at`，页面上的数取自 `preview(now=…)`，两者错开就是「页面上说的」
            与「写下去的」不是同一件事。

    Raises:
        ValueError: `reason` 或 `actor_name` 为空白时。这与 `halt_sync` 对
            `PlannedRow` 的必填字段同一取向：一次没人认领、或没写原因的开关切换，
            在事后排查里等于没有记录。
        Exception: `gate_run.sync` 的任何失败**原样往上抛**，由调用方（slash 命令 / CLI）
            让人看见——本模块不发告警（CONTEXT.md：告警是投递给一个具体的人的动作，
            而这里手上正好有一个具体的人）。半个动作比不动作更坏：流水行已经写下去了，
            而表还没对上，那正是「机制说自己打开了」与「实际拦不拦」分叉的形态。
    """
    if not str(reason).strip():
        raise ValueError("切换原因不能为空：RegimeMechanismSwitch 的 reason 是必填的")
    if not str(actor_name).strip():
        raise ValueError("触发方不能为空：一次没人认领的开关切换无法排查")
    # 收字符串：枚举与字符串的 `is` 比较恒为假，那会把「本来就是这一档」当成一次真切换，
    # 于是流水里多出一条一模一样的行，而它对排查的害处是「最近一行」仍是同一个档位。
    to_mode = MechanismMode(to_mode)
    at = now or timezone.now()

    current = RegimeMechanismSwitch.current(KIND)
    row: RegimeMechanismSwitch | None = None
    if current is to_mode:
        logger.info(
            "[regime] 行情阶段 gate 本来就是%s，没有写第二条流水；仍对一次账",
            to_mode.display,
        )
    else:
        row = RegimeMechanismSwitch.objects.create(
            from_mode=current.value,
            to_mode=to_mode.value,
            kind=KIND.value,
            at=at,
            actor_kind=ActorKind(actor_kind).value,
            actor_name=str(actor_name).strip()[:128],
            reason=reason,
        )
        logger.info(
            "[regime] 行情阶段 gate %s → %s（%s：%s）",
            current.display,
            to_mode.display,
            row.actor_name,
            reason,
        )

    # 无条件跑一轮，见模块 docstring 第 2 步：档位刚翻过 ⇒ 这一轮把表对到新档位上；
    # 档位没变 ⇒ 这是一个人工的「现在重算一轮」。异常往上抛。
    summary = gate_run.sync(now=at)
    logger.info(
        "[regime] 行情阶段 gate 翻到%s之后的对账：%s",
        to_mode.display,
        {
            "regime": summary.get("regime"),
            "skipped": summary.get("skipped"),
            "declarations": len(summary.get("declarations") or ()),
            "close_reasons": len(summary.get("close_reasons") or {}),
            "statuses": len(summary.get("statuses") or ()),
            "halt": summary.get("halt"),
        },
    )
    return Flip(row=row, sync=summary)
