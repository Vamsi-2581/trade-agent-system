"""行情阶段 gate 的取数与落库（第③段单元 ③c）。

`gate.py` 是**纯判定层**：给它「此刻的世界」（`Situation`）与「每条决策行的只读快照」
（`DecisionRef`），它还一张 `GatePlan`。本模块是它的另一半——世界从哪些表里读出来、
`GatePlan` 怎么落到两张表上。分工与 `deactivation.py` / `deactivation_run.py` 同构。

## 两处写方，各写一半

| 表 | 写方 | 写什么 |
|----|------|--------|
| `DeactivationDecision.status` | 本模块（`applied` / `released`）、`deactivation_run`（`suggested`） | 机制**做过什么** |
| `HaltDeclaration` | `halt_sync._reconcile`（**唯一**写方） | 机制**此刻在拦什么** |

所以本模块**不写声明表**：它把 `GatePlan` 递给 `halt_sync.sync(gate_plan=…)`，由那一个
写方落行。两个写方碰同一张表、又各按各的期望集对账，那条唯一键迟早会被两边各写一遍。

**也不写豁免表。**豁免的关闭（阶段离开）归 `run_deactivation` 那一轮；本模块只**读**
「这一格豁免着吗」这一个问题，问了不改。

## 配对读取：代与格子必须来自同一代

`pool_rebuild.current_cells(generation)` 收的是**已经取到的那一代**，不是内部再问一次。
两次各自去问当前代，之间可能正好翻了一代，于是声明行的 `opened_at` 与「判据在说哪一代」
分属两代——日报上看起来完全正常。

## 三种「本轮没有声明」，各自说得出理由

- `no_generation`：阶段有了，却一代池化表都没有。那是**没法问**，不是「没有该停的」——
  按空集解除会把每条活声明都解除掉，原因码还会写成「重新适配」。
- `blocked`（冷启动 / 状态过期）：机制说不清现在是什么阶段。解除了就再也回不来，
  而保持现状只是「今晚本不该拦的拦着」。取向与 `_halt_block_reason` 的 fail-closed 一致。

**两种都在这里结束**：期望集算不出来的时候，连对账都不发起。不是「递给对账一个空期望集」
——那与「按空集解除」之间只隔着 `halt_sync._reconcile` 里的一条 fail-closed 判断，而那条
判断是为「写入方漏了一行」准备的，不该拿来当判定层的主要出口。两种都落成 `skipped` +
一句给人看的话：**不静默**是这条纪律的一半，「日志不算被看见」是另一半。

Shadow 档是第三种「本轮没有声明」，它与上面两种相反、**要发起对账**：回到 Shadow 的意义
就是不再拦，活行按 `gate_closed` 解除。它也带一句话（`gate.NOTE_SHADOW`）——只说
`gate_open=False` 的话，读的人还得自己推出「那现有的活行呢」。

## 失败往上抛，这里不发告警

与 `run_deactivation` 同一条：任务与日报是「被看见」的渠道，本模块只把话说清楚、把
异常抛出去，让调用方决定怎么让人看见（CONTEXT.md 第 22 条：告警是投递给**一个具体的人**
的动作）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from django.utils import timezone

from apps.regime import (
    config,
    deactivation,
    deactivation_run,
    gate,
    halt,
    halt_sync,
    judgement,
    pool_rebuild,
)
from apps.regime.config import JudgementLifecycleConfig
from apps.regime.models import (
    DeactivationDecision,
    DeactivationExemption,
    HaltTrigger,
    RegimeMechanismSwitch,
    RegimePoolRebuild,
)
from apps.regime.slice import STATE_UNFIT
from apps.trading.models import Strategy

logger = logging.getLogger(__name__)

#: 落进 `HaltDeclaration.actor_name` 的名字。取自 `halt_sync` 而不是在这里重写一遍：
#: 这个名字是「谁写的这一行」的唯一答案，两处拼写分叉的表现是同一档声明在表里出现
#: 两个来源，而查起来要先把两个名字都认全。
ACTOR_NAME = halt_sync.GATE_ACTOR_NAME

#: 没有当前代时的说明（与 `deactivation_run` 的同名句子同一口径）。
NOTE_NO_GENERATION = (
    "还没有任何一代池化表，本轮不产出停用声明（先跑 recompute_regime_slices）"
)

__all__ = ["ACTOR_NAME", "Round", "plan_round", "preview", "sync"]


# --------------------------------------------------------------------------- #
# 取数：开关、豁免
# --------------------------------------------------------------------------- #


def gate_switch() -> tuple[bool, datetime | None]:
    """`regime_gate` 这个开关的档位，以及「开」的那一刻（没开时 `None`）。

    `switch_open` 是「哪个开关管哪条线」的唯一换算口，档位从它问；`switch_at` 单独再问
    一次流水。两次读之间可能正好发生一次切换，那样得到的偏差是 `opened_at` 取到更晚的
    那个时刻——`halt_sync._rewrite` 会把它记成一次窗口起点变化，下一轮就自己稳住了。
    换来的是「当前档位」这条判据不在本地抄第二遍：抄一遍分叉了没有自愈路径，两个答案
    看起来都正常。
    """
    kind = halt.HALT_TRIGGER_SWITCH[HaltTrigger.DEACTIVATION]
    open_now = halt.switch_open(HaltTrigger.DEACTIVATION)
    if not open_now:
        return False, None
    # 开着 ⇒ 流水里必有那一行（`RegimeMechanismSwitch.current` 的判据即「最近一行」），
    # 所以 `at` 拿得到；万一拿不到，`gate.attitude_since` 会为它大声抛出来。
    row = RegimeMechanismSwitch.latest(kind)
    return True, (row.at if row is not None else None)


def _regime_exemptions(regime: str, *, now: datetime) -> dict[UUID, gate.Exemption]:
    """**当前阶段**上每一格的豁免：在期的与刚结束的都要。

    其它阶段的豁免与本层无关：那些决策行的阶段对不上当前阶段，一律按 `regime_left`
    解除，豁免在这条路后面才轮到（`gate._close_reason` 的顺序）。

    「在期」复用 `deactivation_run.in_force_exemptions`（三条一起：未关闭 / 已生效 /
    未到期），不在本地重写——本模块与 `deactivation.derive` 会用同一个词回答「这一格
    豁免着吗」，两处判据分叉的表现是「没有声明」与「决策行写着已豁免」互相打架，而两边
    看起来都正常。本层额外要的只有 `ends_at`（刚结束那条豁免的窗口结束时刻，Q5），
    `in_force_exemptions` 的返回值里没有它——所以这里按同一套 `granted_at` 顺序再读一遍
    行，用在期集合**认领**每一条：认得领的按在期算，剩下的取**最后发出的**那一条。
    """
    in_force = deactivation_run.in_force_exemptions(now=now)
    rows = (
        DeactivationExemption.objects.filter(regime=regime, granted_at__lte=now)
        .order_by("granted_at", "id")
        .values_list("id", "strategy_id", "expires_at", "closed_at")
    )
    picked: dict[UUID, gate.Exemption] = {}
    ended: dict[UUID, gate.Exemption] = {}
    for exemption_id, strategy_id, expires_at, closed_at in rows:
        if in_force.get((strategy_id, regime)) == exemption_id:
            picked[strategy_id] = gate.Exemption(in_force=True, ends_at=expires_at)
        else:
            # `closed_at or expires_at`：提前收回的豁免，窗口结束于**收回那一刻**，
            # 不是原定的到期时刻。它是 `attitude_since` 的第四个候选，取错的后果是
            # 声明行声称机制在一段它并没有拦的时间里拦着。
            ended[strategy_id] = gate.Exemption(
                in_force=False, ends_at=closed_at or expires_at
            )
    for strategy_id, exemption in ended.items():
        picked.setdefault(strategy_id, exemption)
    return picked


# --------------------------------------------------------------------------- #
# 取数：决策行 → 只读快照
# --------------------------------------------------------------------------- #


def _warrant(decision: DeactivationDecision, *, managed: frozenset, cells: dict) -> str:
    """这一格此刻还算不算「该停用」。三态，取值见 `gate.GONE` / `LAPSED` / `STILL_TARGET`。

    两个来源缺一不可，所以先判归属再判判据：

    - **归属**（`gone`）：策略不在被管集合里（幽灵行、实现类解析不到），或已被人工退役
      （`is_active=False`，CONTEXT.md 第 131 条：退役即不再参与求值，记录保留）。两者都
      不是「判据变了」——策略都不归这套机制管了，写 `became_fit` 会把一次离场说成一次
      重新适配。
    - **判据**：查**这一行自己的**（策略, 阶段）那一格，而不是当前阶段那一列。行上的
      阶段与当前阶段不同时，换解除原因的时刻才轮到「阶段对不上」这一条（`gate._close_reason`
      的第一条），而那一刻需要知道的是「这条结论自己还成不成立」——写成「一律按当前阶段
      算」的话，一条阶段已经离开、但判据完好的行会被记成 `strategy_gone`。

    格子取不到（当前代没有这一格）落 `LAPSED` 而不是 `GONE`：`gate.LAPSED` 的定义就是
    「当前代不再产出这条停用建议」，而它的三个来源里明写了「这一格在当前代消失」。
    """
    if decision.strategy_id not in managed or not decision.strategy.is_active:
        return gate.GONE
    cell = cells.get((decision.strategy_id, decision.regime))
    if cell is None:
        return gate.LAPSED
    if cell.get("state") == STATE_UNFIT and not cell.get("needs_review"):
        return gate.STILL_TARGET
    # 其余一律「当前代不再产出这条建议」：`fit` / `neutral` / `unknown` / `blanket`
    # 与 `needs_review` 的冲突格子同属这一档。**没有任何一条路径**能把它们判成该停。
    return gate.LAPSED


def _refs(*, managed: frozenset, cells: dict) -> tuple[gate.DecisionRef, ...]:
    """全部决策行的只读快照。**已解除的也要**——那正是 `applied` / `released` 两个取值的
    用处：一条被机制撤回过、后来又因为阶段回来而重新成立的，必须能回到 `applied`
    （否则「撤回过的不能再声明」会让这一格永久哑掉）。

    一次查询带出策略名与 `is_active`（`select_related`）：`managed` 只有 id，而声明行的
    `label` 与 `reason` 都要策略名——那是**策略名唯一进得了用户眼睛的地方**（Q10 的核查：
    通知正文只打触发源、作用域与 `reason`）。
    """
    rows = DeactivationDecision.objects.select_related("strategy").order_by(
        "strategy_id", "regime"
    )
    return tuple(
        gate.DecisionRef(
            decision_id=row.pk,
            strategy_id=row.strategy_id,
            strategy_name=row.strategy.name,
            regime=row.regime,
            status=row.status,
            warrant=_warrant(row, managed=managed, cells=cells),
            cell_reason=(cells.get((row.strategy_id, row.regime)) or {}).get("reason")
            or "",
        )
        for row in rows
    )


def _suspension_refs(
    *, managed: deactivation_run.ManagedSet, cells: dict, situation: gate.Situation
) -> tuple[gate.SuspensionRef, ...]:
    """**全部被管策略**在本轮的只读快照——暂停路（「只放行 fit」）要的那一份。

    与 `_refs` 的关键差别：那一份由「有人建议过停用」的决策行派生，这一份覆盖**每一个**
    被管策略。用户那句话（「只放行适应箱体震荡的策略，其它的都拦住」）问的是每个在跑策略
    在这片行情里的资格，与它有没有被建议过停用无关。

    读法上只有三样东西：当前阶段是哪一列（`situation.regime`）、这一代在那一列上这一格
    是什么（`cells`）、以及这一格此刻有没有在期人工豁免（`situation.exemptions`——在期
    判据由取数侧算好，判定层只看见结论，与 `Exemption` 的既有纪律一致）。

    **没有当前生效阶段时给空集**（那一路的早退分支会整轮不动，这里不必先判）：空集与
    「每个策略都没有格」在下游是两件事，而前者才是事实——说不清阶段时，本层连「谁该被问」
    都答不出来。

    策略名一次批量查（`managed.ids` 有几十条，逐个查是几十次往返）：`label` 与 `reason`
    都要它，而那是策略名**唯一**进得了用户眼睛的地方。查不到名字的策略（理论上不会发生，
    FK 保证存在）退回 id 字面量，而不是抛——一份快照不该因为一句措辞缺失就让整轮停摆。
    """
    if situation.regime is None:
        return ()
    rows = {
        strategy_id: (name, is_active)
        for strategy_id, name, is_active in Strategy.objects.filter(
            id__in=managed.ids
        ).values_list("id", "name", "is_active")
    }
    out: list[gate.SuspensionRef] = []
    for strategy_id in managed.ids:
        cell = cells.get((strategy_id, situation.regime)) or {}
        exemption = situation.exemptions.get(strategy_id)
        # `is_active` 在这里读，而不是在 `managed_set()` 里筛：**被管集合的定义不滤它**
        # （CONTEXT.md 明写「`is_active` 不作为集合依据」），而「退不退役」是**求值**那一层
        # 的事。读不到行时按「仍在册」处理（`managed.ids` 本来就来自这张表，读到才有它）——
        # 一份快照不该因为一次缺读把一条在跑的策略**漏判成放行**。
        name, is_active = rows.get(strategy_id, (str(strategy_id), True))
        out.append(
            gate.SuspensionRef(
                strategy_id=strategy_id,
                strategy_name=name,
                cell_state=cell.get("state"),
                cell_reason=cell.get("reason") or "",
                exempt=bool(exemption and exemption.in_force),
                retired=not is_active,
            )
        )
    return tuple(out)


# --------------------------------------------------------------------------- #
# 落库：status 回写
# --------------------------------------------------------------------------- #


def _write_statuses(
    writes: tuple[gate.StatusWrite, ...], *, strategy_of: dict
) -> list[dict[str, Any]]:
    """把目标 status 写回决策行。**只写 `status` 这一列，`evidence` 一个字都不动。**

    条件更新（`filter(id=…, status=旧值).update(status=新值)`）而不是「读出来再 `save()`」：
    两轮之间有别的东西动过这一行时返回行数为 0，本模块就知道手里的判断已经过期，
    于是**什么都不做**（保留别人的写），而不是拿一份过期的判断覆盖它。这正是
    `gate.DecisionRef.status` 的 docstring 要求的用法。

    返回逐条的明细（进摘要与日志）：`written` 为假的那一条要说得出为什么没写成。
    """
    out: list[dict[str, Any]] = []
    for write in writes:
        written = DeactivationDecision.objects.filter(
            pk=write.decision_id, status=write.old_status
        ).update(status=write.status)
        if not written:
            logger.warning(
                "[regime] 行情阶段 gate：决策行 %s 的状态在两次读之间被改过"
                "（期望旧值 %s），本轮跳过它——下一轮重新判",
                write.decision_id,
                write.old_status,
            )
        out.append(
            {
                "decision_id": str(write.decision_id),
                "strategy_id": str(strategy_of.get(write.decision_id) or ""),
                "from": write.old_status,
                "to": write.status,
                "written": bool(written),
            }
        )
    return out


# --------------------------------------------------------------------------- #
# 取数与判定：一轮的产物（**一个字都没写**）
# --------------------------------------------------------------------------- #


def _is_target(ref: gate.DecisionRef, situation: gate.Situation) -> bool:
    """这条决策行此刻「该停」——落在当前阶段上，且判据还成立。**不含豁免这一条**。

    「该停」与「会被声明」的差别就是豁免（`gate._declared` 在此之上再排掉在期豁免）。
    两个数在页面上并排出现（「会拦住 N 个；另有 M 个该停但在人工豁免期」），所以这里
    给的是那个更大的 N + M。
    """
    return ref.regime == situation.regime and ref.warrant == gate.STILL_TARGET


def _is_exempt(ref: gate.DecisionRef, situation: gate.Situation) -> bool:
    """「该停，但人在豁免期按着」——打开开关也拦不住它。"""
    exemption = situation.exemptions.get(ref.strategy_id)
    return _is_target(ref, situation) and bool(exemption and exemption.in_force)


@dataclass(frozen=True)
class Round:
    """一轮的**取数与判定结果**——一个字都没写进库里。

    拆出这一层是为了让 `sync` 与确认页共用同一个求值点：页面上每个数都出自**真正那一轮
    的同一次判定**，而不是另写一套「会拦住几个」的算法（那是「两处判据分叉、两边看起来
    都正常」的标准形态）。`sync` 把它落库；`gate_switch.preview` 拿它回答「此刻打开
    会拦住什么」。

    `plan` 恒非空：没有当前代时用 `GatePlan.blocked = no_generation` 表示，而不是在这里
    另开一个 `skipped` 字段——`blocked` 的定义就是「本轮一个字都没动」，两条路（没有代 /
    阶段说不清）在这一层完全同形，下游只认一个字段。

    `hypothetical` 与 `gate_open` 的差别是**档位的来源**：`gate_open` 说的是「流水里现在
    是什么档」，`hypothetical` 说的是「这个档是假设出来的（此刻还没开）」。页面上
    「打开之后会拦住 X 个」这句话，只有在 `hypothetical` 为真时才是「打开之后」。
    """

    now: datetime
    symbol: str
    params: JudgementLifecycleConfig
    managed: deactivation_run.ManagedSet
    state: deactivation.RegimeState
    #: 当前代池化表；`None` = 一代都还没有。
    generation: RegimePoolRebuild | None
    #: 开关此刻的档位（从流水里读的，不是 `hypothetical` 那个假设）。
    gate_open: bool
    #: 开关「开」的那一刻；没开且没有假设时为 `None`。见 `gate.attitude_since`。
    switch_at: datetime | None
    #: 档位是**假设**出来的（确认页预览：此刻还没开）。
    hypothetical: bool
    plan: gate.GatePlan
    #: 上一有效判定距今几个自然日；冷启动与没有判定时为 `None`（`Derivation.age_days`）。
    age_days: int | None = None
    #: 当前代里有格子、但不在被管集合里的策略 id（幽灵行）。只报数（`Derivation.unmanaged`）。
    unmanaged: tuple[Any, ...] = ()
    #: 全部决策行的只读快照——回写 status 时要用它拼「决策行 → 策略」那张表。
    refs: tuple[gate.DecisionRef, ...] = ()
    #: 全部**被管策略**的只读快照——「按阶段暂停开新仓」那一路的输入
    #: （`_suspension_refs`）。它与 `refs` 的域不同：那边只有被建议过停用的策略。
    suspension_refs: tuple[gate.SuspensionRef, ...] = ()
    #: 判定用的世界状态。只读。
    situation: gate.Situation | None = None

    @property
    def suspensions(self) -> int:
        """当前阶段上**真的由本路拦住**的策略条数。

        数的是合成后期望声明里 `regime_suspension` 那一档，不是「所有非 fit 的被管策略」：
        同时被停用决策覆盖的策略只留一条声明（`gate.merge` 的去重规则），把它们再计一遍
        会让页面上出现「该停 3 个 / 暂停 5 个」而声明表里其实只有 3 行。
        """
        return sum(
            1
            for declaration in self.plan.declarations
            if declaration.trigger == gate.TRIGGER_REGIME_SUSPENSION
        )

    @property
    def targets(self) -> int:
        """当前阶段上「该停」的条数（**含**被豁免的）。"""
        if self.situation is None:
            return 0
        return sum(1 for ref in self.refs if _is_target(ref, self.situation))

    @property
    def exempt(self) -> int:
        """其中处于在期人工豁免的条数——打开也拦不住的那些。"""
        if self.situation is None:
            return 0
        return sum(1 for ref in self.refs if _is_exempt(ref, self.situation))


def plan_round(
    *,
    symbol: str = judgement.SYMBOL,
    now: datetime | None = None,
    params: JudgementLifecycleConfig | None = None,
    as_if_open_at: datetime | None = None,
) -> Round:
    """取数 + 判定，**一个字都不写**。

    `as_if_open_at` 非空 ⇒ 「假设开关在那一刻是开的」，供确认页预览。它只改 `gate_open`
    与 `switch_at` 两个入参，判定本身还是 `gate.derive` 那一条——预览因此**不是**第二套
    预言，而是同一条判定的另一次求值。

    真开着的档位**优先于**假设：那时这一轮说的就是它真会做的事，预览一个「如果打开」的
    反事实只会把话说拧。
    """
    at = now or timezone.now()
    params = params or config.JUDGEMENT_LIFECYCLE

    managed = deactivation_run.managed_set()
    state = deactivation_run.current_regime_state(symbol, now=at)
    generation = pool_rebuild.current_generation()
    gate_open, switch_at = gate_switch()
    hypothetical = False
    if not gate_open and as_if_open_at is not None:
        gate_open, switch_at, hypothetical = True, as_if_open_at, True

    if generation is None:
        return Round(
            now=at,
            symbol=symbol,
            params=params,
            managed=managed,
            state=state,
            generation=None,
            gate_open=gate_open,
            switch_at=switch_at,
            hypothetical=hypothetical,
            plan=gate.GatePlan(
                blocked=deactivation_run.SKIPPED_NO_GENERATION, note=NOTE_NO_GENERATION
            ),
        )

    # 一次读出当前代（**这一代**）的全部格子；代与格子配对传给判定层。
    cells = pool_rebuild.current_cells(generation)
    # `deactivation.derive` 在这里只为了三件事：blocked 的判据（冷启动 / 状态过期）、
    # `unmanaged` 的报数、`age_days`（页面上那句「上一有效判定距今几天」）。逐行的
    # `warrant` 不由它给——它的 `Outcome` 只覆盖当前阶段那一列，而非当前阶段的决策行
    # 同样要判（见 `_warrant`）。`blocked` 的判据只有一处，所以宁可多跑一次纯推导，
    # 也不在这里重写一遍「算不算过期」。
    derivation = deactivation.derive(
        cells,
        state=state,
        strategy_ids=managed.ids,
        running_ids=managed.running,
        exemptions=deactivation_run.in_force_exemptions(now=at),
        now=at,
        params=params,
    )
    situation = gate.Situation(
        gate_open=gate_open,
        switch_at=switch_at,
        regime=state.regime,
        # 判定的**生效时刻**（北京 08:00 那个业务日边界），不是它被算出来的时刻：
        # `attitude_since` 问的是「机制从哪一刻起有理由拦」。
        regime_effective_at=state.effective_at,
        generation_at=generation.finished_at,
        exemptions=(
            _regime_exemptions(state.regime, now=at) if state.regime is not None else {}
        ),
        blocked=derivation.blocked,
    )
    refs = _refs(managed=frozenset(managed.ids), cells=cells)
    suspension_refs = _suspension_refs(
        managed=managed, cells=cells, situation=situation
    )
    # 两路合成**一轮**的期望集：停用决策那一路（`_refs` 派生）与「按阶段暂停开新仓」那
    # 一路（全部被管策略派生）。合成而不是各跑各的，是因为两者交给**同一个**写方
    # （`halt_sync._reconcile`）落同一张表——分两轮跑的话，第二轮会把第一轮不在它期望集
    # 里的行当成「计划里没有」，而那是静默解除。
    plan = gate.merge(
        gate.derive(situation, refs),
        gate.derive_suspensions(situation, suspension_refs),
    )
    return Round(
        now=at,
        symbol=symbol,
        params=params,
        managed=managed,
        state=state,
        generation=generation,
        gate_open=gate_open,
        switch_at=switch_at,
        hypothetical=hypothetical,
        plan=plan,
        age_days=derivation.age_days,
        unmanaged=tuple(derivation.unmanaged),
        refs=refs,
        suspension_refs=suspension_refs,
        situation=situation,
    )


def preview(
    *,
    symbol: str = judgement.SYMBOL,
    now: datetime | None = None,
    params: JudgementLifecycleConfig | None = None,
) -> Round:
    """「如果此刻把开关打开，这一轮会做什么」——确认页用。**只读**。

    `as_if_open_at` 与 `now` 钉成同一个时刻，是本函数存在的全部理由：两者一旦能各填一个，
    确认页就会算出一个**从没人见过的世界**（「打开的那一刻是 T1、取数在 T2」），而它看起来
    和真的一样。这里**没有** `as_if_open_at` 参数，就是为了让那种填法根本写不出来。
    """
    at = now or timezone.now()
    return plan_round(symbol=symbol, now=at, params=params, as_if_open_at=at)


# --------------------------------------------------------------------------- #
# 一轮（落库）
# --------------------------------------------------------------------------- #


def sync(
    *,
    symbol: str = judgement.SYMBOL,
    now: datetime | None = None,
    params: JudgementLifecycleConfig | None = None,
) -> dict[str, Any]:
    """跑一轮 gate：取数 → 判定 → 落声明 → 回写 status。返回**可 JSON 序列化**的摘要。

    取数与判定在 `plan_round`（确认页上那些数就出自它，**同一次求值**），本函数只负责
    把 `Round` 落库。两件事拆开的理由见 `Round` 的 docstring。

    与 `run_deactivation` 同一套摘要约定：`skipped` 键恒存在（没事时是 `None`），
    键集在四条路径上一致（正常 / Shadow / blocked / no_generation），id 一律是字符串。
    它进 Celery 结果，键集随路径漂移会让「今天和昨天有什么不同」多出一堆假差异。

    幂等：期望值逐字来自库里存好的事实，所以连着跑两轮，第二轮一定是声明表
    `unchanged == 活行数`、且 `statuses` 为空。

    Args:
        symbol: 判定与池化都挂在同一个品种上（`config.CANDLES.symbol`）。
        now: 注入时钟；不传取当前时刻。同时用于豁免的在期判据与声明行的期望。
        params: 生命周期参数；不传取统一配置面的当前值。
    """
    round_ = plan_round(symbol=symbol, now=now, params=params)

    summary: dict[str, Any] = {
        "symbol": round_.symbol,
        "regime": round_.state.regime,
        "generation_id": round_.generation.pk
        if round_.generation is not None
        else None,
        "skipped": None,
        "note": round_.plan.note,
        "gate_open": round_.gate_open,
        "switch_at": (
            round_.switch_at.isoformat() if round_.switch_at is not None else None
        ),
        "managed": len(round_.managed.ids),
        "running": len(round_.managed.running),
        "unresolved": [str(i) for i in round_.managed.unresolved],
        "unmanaged": [str(i) for i in round_.unmanaged],
        "targets": 0,
        "exempt": 0,
        # 「按阶段暂停开新仓」那一路的条数（issue #51）。键在四条路径上都必须存在：
        # 它进 Celery 结果，键集随路径漂移会让「今天和昨天有什么不同」多出一堆假差异。
        "suspensions": 0,
        "declarations": [],
        "statuses": [],
        "close_reasons": {},
        "halt": None,
    }

    if round_.plan.blocked:
        # 冷启动 / 状态过期 / 没有当前代 / 保命档：期望集算不出来（前三个）或算得出来但
        # 这一层不该动手（保命档，见 `gate.BLOCKED_BLANKET`）⇒ 连对账都不发起。活行原样
        # 留着继续拦：那是这几个收场唯一诚实的动作。
        # `targets` / `exempt` 也就留在 0——本轮确实一个都没算（保命档那一档下池化层本来
        # 也不判适配，算出来同样是 0），报一个「该停 3 个」出来只会让人以为机制动过它们。
        summary["skipped"] = round_.plan.blocked
        logger.info("[regime] 行情阶段 gate：%s", summary["note"])
        return summary

    # 走到这里 `plan.note` 只可能是 Shadow 那一档（正常一轮是空串），而它必须进摘要：
    # `gate_open=False` 只说得出「开关关着」，说不出「这一轮拿活行怎么办」——那正是
    # `gate.NOTE_SHADOW` 那句话要交代的事。
    summary["targets"] = round_.targets
    summary["exempt"] = round_.exempt
    summary["suspensions"] = round_.suspensions
    summary["close_reasons"] = {
        str(strategy_id): code
        for strategy_id, code in round_.plan.close_reasons.items()
    }

    # **声明先写、记录后写**：声明是机制对市场的动作，status 是对「机制做过什么」的记录。
    # 写声明失败时整轮往上抛（调用方负责让人看见），那时一条 status 都还没动——反过来
    # 先写 status 的话，一次失败会留下一批「机制声明过它」而声明表里什么都没有的假历史。
    summary["halt"] = halt_sync.sync(
        gate_plan=round_.plan, actor_name=ACTOR_NAME, now=round_.now
    )
    summary["statuses"] = _write_statuses(
        round_.plan.statuses,
        strategy_of={ref.decision_id: ref.strategy_id for ref in round_.refs},
    )
    summary["declarations"] = [
        {
            "strategy_id": str(declaration.strategy_id),
            "label": declaration.label,
            "reason": declaration.reason,
            "opened_at": declaration.opened_at.isoformat(),
            "expires_at": (
                declaration.expires_at.isoformat()
                if declaration.expires_at is not None
                else None
            ),
        }
        for declaration in round_.plan.declarations
    ]

    logger.info(
        "[regime] 行情阶段 gate：阶段 %s，开关 %s，被管 %s（在跑 %s），该停 %s"
        "（已豁免 %s），按阶段暂停 %s，期望声明 %s，解除 %s，status 回写 %s，声明表 %s",
        summary["regime"],
        "开" if summary["gate_open"] else "Shadow",
        summary["managed"],
        summary["running"],
        summary["targets"],
        summary["exempt"],
        summary["suspensions"],
        len(summary["declarations"]),
        len(summary["close_reasons"]),
        len(summary["statuses"]),
        summary["halt"],
    )
    return summary
