"""停用决策推导（第①段单元 7iii 的正文）：当前阶段 + 当前代池化格 → 现在该停谁。

## 它回答的问题与池化表不是同一个

池化表是一张**适用性**表：「这条策略在这个阶段适不适用」。停用决策是一张**当前状态**
表：「现在该停谁」。差别不只是粒度，是时间。池化表每一代整体替换，而每条停用决策都
要能回答「为什么停我」——所以它把判据成立那一刻的依据冻在自己身上
（`DeactivationDecision` 的 docstring），而推导只是**在每一代上重新问一次**。

## 纯函数

进的是：当前生效阶段、当前代的池化格、在期的人工豁免、被管策略集合。出的是逐策略的
结论。**不碰数据库**（取数与落库在 `deactivation_run` 里）、不写日志、不投通知。于是
「同一份格子 + 同一个阶段 ⇒ 同一批建议」可以被测试直接钉住。

时钟按仓库既有约定处理（与 `slicing.compute_slice` 同形）：`now` 显式传入，不传则取
`timezone.now()`。**「不读时钟」在这里的实现方式是「可注入」**，不是「拒绝看时间」——
过期判据本身就是一个时间差。

## 被管策略集合：两条判据在这里是**一筛一标**

CONTEXT.md 的被管策略集合是「能在策略注册表解析到实现类 **且** 被至少一个活跃实盘
会话引用」。本模块把第一条当**筛选**、第二条当**标记**（`Outcome.running`）：

- 第一条不筛不行——回测自动建出来的幽灵策略行没有实现类，带着它们日报会变成几十条
  「未知」（CONTEXT.md 原话）。
- 第二条若也当筛选，第①段会**天天产出空记录**：这一段是零执行的 Shadow，可能一个
  活跃实盘会话都没有，而每日 Shadow 记录恰恰是这一段唯一的产出。空记录意味着这段
  阶段永远无法被验证，也就永远走不到第③段。

所以推导覆盖**全部有实现类的策略**，把「有没有人正在跑」如实标出来；第③段的 gate 再
按这个标记收窄。这条是把一个判据从「筛」放松到「标」，是**可逆的一处收窄**（见
`deactivation_run.managed_set` 与 `deactivation_run.running_strategy_ids`）。

## 三件事不构成停用理由

- `unknown`（该阶段一个交易日都没有）与 `no_cell`（当前代没有这一格）——**说不出来就
  说说不出来**，不拿「没结论」当「不适用」（与 `quant.py` 拒绝把数据不足算成箱体震荡
  是同一条）。
- `blanket`（保命档）。高波动档下全场停用由保命档那一层负责，本机制不重复产出建议。
  这一条在结构上已经是真的：`pool._pooled_state` 对高波动档恒返回 `blanket`，而
  `blanket` 不是 `TARGET`，所以**没有任何一条代码路径**能让高波动档产出停用建议。
  这里不另设一道「若是保命档则跳过」的闸——多一道闸就多一处两套判据漂移的地方，
  测试里钉死这条结构性事实即可。
- `neutral`。门槛没过就是没结论，不是「不好」。

## 冲突格子

Q3：方向冲突的格子（`needs_review`）**照常产出池化结论、照常被引用，但不产生停用
建议**。所以那一格在这里落成 `NEEDS_REVIEW` 而不是 `TARGET`——结论在，动作不在。

## 状态过期与冷启动：不产出，但要说出来

CONTEXT.md：判定失败/数据缺失时保持上一有效状态；**状态过期超 3 个自然日则不再新增
停用决策**；冷启动默认不执行任何停用。两者都落成 `Derivation.blocked`，由
`blocked_note` 渲染成一句给人看的话——**不静默**是这条纪律的一半，另一半是「日志不算
被看见」，所以这句话由调用方（`deactivation_run`）带进任务返回值与日报，本模块只负责
把它造出来。

过期判据用的是**生效时刻**（`effective_at`）而不是 `created_at`：一条判定「今天还有效
吗」问的是它从哪一刻开始生效。

`strategy_id` 是策略主键（`Strategy.id`，UUID）。本模块只把它当**不透明的键**——比较、
去重、原样带出去，不解析也不拼接，所以测试里拿整数代替就够了。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Mapping

from django.utils import timezone

from apps.regime import config
from apps.regime.quant import BaseRegime
from apps.regime.slice import (
    STATE_BLANKET,
    STATE_FIT,
    STATE_NEUTRAL,
    STATE_UNFIT,
    STATE_UNKNOWN,
)

# --------------------------------------------------------------------------- #
# 逐策略结论
# --------------------------------------------------------------------------- #

#: 该停用。**唯一**会写出一条停用决策的取值。
TARGET = "target"
#: 判为不适用，但那一格的依据方向冲突（`pool` 的 `needs_review`）→ 转人工，不产出建议。
NEEDS_REVIEW = "needs_review"
FIT = "fit"
NEUTRAL = "neutral"
#: 该阶段一个交易日都没有（`pool` 的 `no_regime_days`）。
UNKNOWN = "unknown"
#: 保命档。见模块 docstring：不是「跳过」，是这条路径本来就到不了。
BLANKET = "blanket"
#: 当前代根本没有（策略, 阶段）这一格——通常是它的回测全被排除了。
NO_CELL = "no_cell"

VERDICT_DISPLAY = {
    TARGET: "判为不适用",
    NEEDS_REVIEW: "判为不适用（依据方向冲突，待人工复核）",
    FIT: "判为适用",
    NEUTRAL: "中性",
    UNKNOWN: "该阶段没有结论",
    BLANKET: "保命档",
    NO_CELL: "当前代没有这一格",
}

#: 池化状态 → 逐策略结论。`unfit` 的两种分岔由 `needs_review` 决定，不在这张表里。
#: 单独拎出来是为了让「池化的词表被覆盖全了」可以逐项断言（测试钉住键集）。
VERDICT_OF_STATE = {
    STATE_FIT: FIT,
    STATE_NEUTRAL: NEUTRAL,
    STATE_UNKNOWN: UNKNOWN,
    STATE_BLANKET: BLANKET,
}

# --------------------------------------------------------------------------- #
# 不产出建议的原因
# --------------------------------------------------------------------------- #

#: 从没有过生效判定。冷启动默认不执行任何停用（CONTEXT.md）。
BLOCKED_COLD_START = "cold_start"
#: 上一有效判定距今超过 `stale_after_days` 个自然日。
BLOCKED_STALE_STATE = "stale_state"

BLOCKED_DISPLAY = {
    "": "",
    BLOCKED_COLD_START: "冷启动：还没有过生效判定，本轮不产出停用建议",
    BLOCKED_STALE_STATE: "状态过期：本轮不产出停用建议，请先查判定任务",
}


def is_blanket(regime: str | None) -> bool:
    """这个阶段是不是**保命档**（高波动）。

    保命档的判据只能有一处。做成模块级函数而不只留 `RegimeState.blanket` 一个属性，是
    因为还有调用方**手里只有阶段 slug、没有 `RegimeState`**（`deactivation_run.
    close_left_regime_exemptions` 收的就是 slug）：那里判错的表现是把每一条在期豁免都
    关掉，而 `closed_at` 写下去没有复活路径。
    """
    return regime == BaseRegime.HIGH_VOL.value


@dataclass(frozen=True)
class RegimeState:
    """当前**生效**的那条阶段。`regime is None` = 冷启动。

    `effective_at` 是那条判定的生效时刻（北京时间 08:00 那个业务日边界），不是它被
    算出来的时刻。过期判据问的是「这个结论从哪天开始在生效」，两者不是一回事。
    """

    regime: str | None = None
    effective_at: datetime | None = None

    @property
    def cold(self) -> bool:
        return self.regime is None

    @property
    def blanket(self) -> bool:
        """当前是不是保命档（高波动）。日报要拿它写一句「全场停用由保命档负责」。

        放在这里而不是从格子上推：这是**阶段本身**的性质，与这一代池化表算出了什么无关。
        """
        return is_blanket(self.regime)


@dataclass(frozen=True)
class Outcome:
    """一个被管策略在当前阶段上的结论。**每个被管策略恰好一条**。"""

    strategy_id: int
    regime: str
    verdict: str
    #: 池化那一格的原样取值（`state`/`reason`/`source`），报告要照抄池化的一句话，
    #: 而不是在这里重写一句可能与之不一致的话。
    state: str = ""
    reason: str = ""
    source: str = ""
    #: 该策略是否被至少一个**活跃实盘会话**引用（被管策略集合的第二条判据）。
    running: bool = False
    #: 在期人工豁免的 id（若有）。有值时它仍然落决策，只是日报记「已豁免」。
    exempt_id: int | None = None
    #: 池化那一格的依据摘要原文。落决策时**不从这里取**——决策要冻的是它自己那一刻的
    #: 依据，由 `deactivation_run` 在写之前现读一格。放这里是为了报告能直接引用。
    evidence: Mapping[str, Any] = field(default_factory=dict)

    @property
    def display(self) -> str:
        return VERDICT_DISPLAY.get(self.verdict, self.verdict)


@dataclass(frozen=True)
class Derivation:
    """一轮推导的全部产出。`writable` 与 `targets` 分开，是这一段的边界本身。"""

    regime: str | None
    #: `""` / `cold_start` / `stale_state`。非空时 `writable` 必为空。
    blocked: str = ""
    #: 上一有效判定距今几个自然日；冷启动时为 `None`。
    age_days: int | None = None
    outcomes: tuple[Outcome, ...] = ()
    #: 当前代里有格子、但不在被管策略集合里的策略 id。**只报数，不推导**——
    #: 它们要么没有实现类，要么已经不在册了，对它们产出建议是给幽灵加动作。
    unmanaged: tuple[int, ...] = ()
    #: 本轮用的是哪一组生命周期参数（留痕用）。
    stale_after_days: int = 0

    @property
    def targets(self) -> tuple[Outcome, ...]:
        """**判为不适用**的那些，含被豁免的。过期/冷启动时照样算出来，只是不可写。"""
        return tuple(o for o in self.outcomes if o.verdict == TARGET)

    @property
    def writable(self) -> tuple[Outcome, ...]:
        """真正该落成决策的那些。被挡下时为空——这就是「冷启动不执行任何停用」落点。"""
        return () if self.blocked else self.targets

    @property
    def exempt(self) -> tuple[Outcome, ...]:
        return tuple(o for o in self.targets if o.exempt_id is not None)

    @property
    def needs_review(self) -> tuple[Outcome, ...]:
        return tuple(o for o in self.outcomes if o.verdict == NEEDS_REVIEW)

    @property
    def missing_cells(self) -> tuple[Outcome, ...]:
        return tuple(o for o in self.outcomes if o.verdict == NO_CELL)

    @property
    def blocked_note(self) -> str:
        """给人看的一句话。**必须由调用方带出去**（任务返回值 / 日报 / stdout）：
        被挡下这件事本身就是一条要人看见的事实，不是「这轮没事」。"""
        if not self.blocked:
            return ""
        note = BLOCKED_DISPLAY[self.blocked]
        if self.blocked == BLOCKED_STALE_STATE and self.age_days is not None:
            note = f"{note}（上一有效判定距今 {self.age_days} 个自然日，上限 {self.stale_after_days}）"
        return note


# --------------------------------------------------------------------------- #
# 推导
# --------------------------------------------------------------------------- #


def _gate(
    state: RegimeState, now: datetime, params: config.JudgementLifecycleConfig
) -> tuple[int | None, str]:
    """当前阶段够不够格产出建议。返回 `(age_days, blocked)`。

    「有阶段却没有生效时刻」不是一种正常状态（`RegimeJudgement.effective_at` 非空），
    但它一旦出现，本函数必须给一个**说得出理由**的收场——按冷启动处理：宁可不出建议，
    也不要拿一个说不清从哪一刻开始的结论去停人。
    """
    if state.cold or state.effective_at is None:
        return None, BLOCKED_COLD_START
    age_days = (now - state.effective_at).days
    if age_days > params.stale_after_days:
        return age_days, BLOCKED_STALE_STATE
    return age_days, ""


def _verdict(row: Mapping[str, Any] | None) -> tuple[str, str, str, str]:
    """一格池化结论 → `(verdict, state, reason, source)`。

    取不到的那一格（`None`）是 `NO_CELL`：当前代没有这一格，与「这一格结论是 unknown」
    是两件事——前者是**没有结论**，后者是**结论就是「该阶段没有交易日」**。
    """
    if row is None:
        return NO_CELL, "", "", ""
    state = row.get("state") or ""
    reason = row.get("reason") or ""
    source = row.get("source") or ""
    if state == STATE_UNFIT:
        # 冲突格子的池化结论照常产出、照常被引用，只是**不产生停用建议**（Q3）。
        verdict = NEEDS_REVIEW if row.get("needs_review") else TARGET
        return verdict, state, reason, source
    # 不在表里的取值落到 `unknown`——**这是 fail-safe 的方向**：认不出来的结论绝不能
    # 变成「该停」。词表漂移由测试（这张表并上 `unfit` 必须等于 `slice` 的状态集合）
    # 在编译期之外拦住。
    return VERDICT_OF_STATE.get(state, UNKNOWN), state, reason, source


def derive(
    cells: Mapping[tuple[int, str], Mapping[str, Any]],
    *,
    state: RegimeState,
    strategy_ids: Iterable[int],
    running_ids: Iterable[int] = (),
    exemptions: Mapping[tuple[int, str], int] | None = None,
    now: datetime | None = None,
    params: config.JudgementLifecycleConfig | None = None,
) -> Derivation:
    """当前阶段 × 当前代池化格 → 逐被管策略的结论。

    Args:
        cells: `pool_rebuild.current_cells()` 的形状：`{(策略 id, 阶段): 行字典}`。
            只读当前阶段那一列，其余阶段的行进来只是为了算 `unmanaged`。
        state: 当前**生效**的阶段。
        strategy_ids: 被管策略集合（判据一，见模块 docstring）。
        running_ids: 有活跃实盘会话的那些（判据二，只标记不筛选）。
        exemptions: 在期豁免，`{(策略 id, 阶段): 豁免 id}`。过期或已关闭的**不要**传进来
            ——「在期」的判据属于取数那一层（要读库、要时钟），不属于这里。
        now: 注入时钟；不传取当前时刻。
        params: 生命周期参数；不传取统一配置面的当前值。
    """
    params = params or config.JUDGEMENT_LIFECYCLE
    now = now or timezone.now()
    exemptions = exemptions or {}
    managed = tuple(dict.fromkeys(strategy_ids))
    managed_set = set(managed)
    running = frozenset(running_ids)
    age_days, blocked = _gate(state, now, params)

    unmanaged = tuple(
        sorted(
            {strategy_id for strategy_id, _ in cells if strategy_id not in managed_set}
        )
    )

    # 冷启动下没有「当前阶段」可问：逐策略全落成「当前代没有这一格」是一句真的废话，
    # 而且会把一次「没有阶段」摊成几十条噪声。收场只有一条 `blocked`，见其 docstring。
    if state.cold:
        return Derivation(
            regime=None,
            blocked=blocked,
            age_days=age_days,
            outcomes=(),
            unmanaged=unmanaged,
            stale_after_days=params.stale_after_days,
        )

    outcomes = []
    for strategy_id in managed:
        row = cells.get((strategy_id, state.regime))
        verdict, cell_state, reason, source = _verdict(row)
        outcomes.append(
            Outcome(
                strategy_id=strategy_id,
                regime=state.regime,
                verdict=verdict,
                state=cell_state,
                reason=reason,
                source=source,
                running=strategy_id in running,
                exempt_id=exemptions.get((strategy_id, state.regime)),
                evidence=dict(row.get("evidence") or {}) if row else {},
            )
        )

    return Derivation(
        regime=state.regime,
        blocked=blocked,
        age_days=age_days,
        outcomes=tuple(outcomes),
        unmanaged=unmanaged,
        stale_after_days=params.stale_after_days,
    )
