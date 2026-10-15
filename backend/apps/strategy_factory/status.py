"""候选策略的状态枚举与**唯一合法转移表**（spec #19 D3）。

这个模块刻意**不 import Django**：它是判定纯函数（`evaluate.py`）与模型（`models.py`）
共同依赖的最底层词汇，判定那侧要做到「零 I/O、不碰 DB」，就不能让状态定义本身把
Django 的模型图拉进来。

三条纪律：

1. **状态只表达「现在在哪」，不表达「通过没通过」**。D3 明确不设
   `in_sample_passed` / `oos_passed` 这类通过态——「通过了」这件事记在候选的
   `evidence` 里。设了通过态就会出现「passed 但不知道下一步」的悬空组合。

2. **转移表是穷举的、显式的**，且在 import 时自校验「每个状态都在表里」。
   漏一个状态不能表现为「它没有出边」（那会让一个本该能前进的候选安静地卡死，
   看起来跟「还没轮到它」一模一样），必须在进程启动时就炸。

3. **自环不算合法边**。`can_transition(x, x)` 为假：判定结论里的「停在本状态」
   不是一次转移，而是「不转移」。这样「合法边」与「判定结果」是两个互不包含的
   集合，穷举测试（169 对）才能既无歧义又无遗漏。
"""

from __future__ import annotations

from enum import Enum


class StrategyCandidateStatus(str, Enum):
    """候选策略的 13 态（D3）。

    ascii slug 落库（`CharField` + `choices`），中文展示名走 `display`——与
    `apps/regime` 的 `BaseRegime` / `Escalation` 同一约定：库里存的是稳定的
    ascii 标识，改文案不必写数据迁移。
    """

    REGISTERED = "registered"
    REGISTRATION_FAILED = "registration_failed"
    IN_SAMPLE_RUNNING = "in_sample_running"
    IN_SAMPLE_FAILED = "in_sample_failed"
    OOS_RUNNING = "oos_running"
    OOS_FAILED = "oos_failed"
    PAPER_PENDING = "paper_pending"
    PAPER_RUNNING = "paper_running"
    PAPER_FAILED = "paper_failed"
    AWAITING_LIVE_APPROVAL = "awaiting_live_approval"
    LIVE = "live"
    REJECTED = "rejected"
    CANCELLED = "cancelled"

    @property
    def display(self) -> str:
        return _STATUS_DISPLAY[self]

    @classmethod
    def choices(cls) -> list[tuple[str, str]]:
        return [(m.value, m.display) for m in cls]


#: 状态 → 展示名。落进模型 choices 与通知文案，不就地写中文（同一个词只写一遍）。
_STATUS_DISPLAY = {
    StrategyCandidateStatus.REGISTERED: "已登记",
    StrategyCandidateStatus.REGISTRATION_FAILED: "登记失败",
    StrategyCandidateStatus.IN_SAMPLE_RUNNING: "样本内回测中",
    StrategyCandidateStatus.IN_SAMPLE_FAILED: "样本内未过",
    StrategyCandidateStatus.OOS_RUNNING: "样本外分折中",
    StrategyCandidateStatus.OOS_FAILED: "样本外未过",
    StrategyCandidateStatus.PAPER_PENDING: "等指定账户/排队中",
    StrategyCandidateStatus.PAPER_RUNNING: "paper 观察中",
    StrategyCandidateStatus.PAPER_FAILED: "paper 未过",
    StrategyCandidateStatus.AWAITING_LIVE_APPROVAL: "等人工卡点",
    StrategyCandidateStatus.LIVE: "已上实盘",
    StrategyCandidateStatus.REJECTED: "人工否决",
    StrategyCandidateStatus.CANCELLED: "人工取消",
}


class StrategyCandidateOrigin(str, Enum):
    """候选的三个来源（D1，Q3 三来源）。

    **刻意不叫 `source`**：`GridSearchJob.source` 已有 `agent/cron/api` 三态，同名
    不同义会误导（spec 的词汇避让表）。形状参照它，名字避开它。
    """

    AGENT = "agent"
    GRID_VARIANT = "grid_variant"
    MANUAL = "manual"

    @property
    def display(self) -> str:
        return _ORIGIN_DISPLAY[self]

    @classmethod
    def choices(cls) -> list[tuple[str, str]]:
        return [(m.value, m.display) for m in cls]


_ORIGIN_DISPLAY = {
    StrategyCandidateOrigin.AGENT: "agent 生成",
    StrategyCandidateOrigin.GRID_VARIANT: "网格变体",
    StrategyCandidateOrigin.MANUAL: "人工投喂",
}


class InvalidTransitionError(ValueError):
    """非法状态转移。

    继承 `ValueError` 而不是自造一个 `Exception` 子类：调用方（编排层）要 Fail Loud，
    而 `ValueError` 是仓库里「输入不合法就炸」的既有形状，except 子句不必新学一个名字。
    消息里带上**当前状态的全部合法目标**——被拒绝的人第一句想问的就是「那我能去哪」。
    """

    def __init__(self, current, target) -> None:
        self.current = current
        self.target = target
        allowed = sorted(s.value for s in ALLOWED_TRANSITIONS[current])
        super().__init__(
            f"候选策略不允许从 {current.value!r} 转移到 {target.value!r}；"
            f"该状态的合法目标只有 {allowed}"
        )


#: 唯一合法边（D3）。**这是全仓关于候选生命周期的唯一一份真相**：
#: 判定纯函数只能产出这里的边（或不产出边），模型层只接受这里的边。
#:
#: 每条边的理由（spec D3 原文）：
#: - `registered` → 起样本内回测；也可以直接登记失败 / 被人工叫停。
#: - `registration_failed` → 修好代码后重试回到 `registered`。
#: - `in_sample_running` → 样本内过了去分折，没过落 `in_sample_failed`。
#: - `in_sample_failed` → 改参数重跑（回到 `in_sample_running`），或人工收尾。
#: - `oos_running` → 样本外过了等 paper 账户/排队；没过落 `oos_failed`。
#: - `oos_failed` → 改分折配置重跑，或人工收尾。
#: - `paper_pending` → 账户就绪且不排队就开始观察，或人工收尾。
#: - `paper_running` → 观察期满判定：过 → 等人工卡点；没过 → `paper_failed`。
#: - `paper_failed` → 重新观察，或人工收尾。
#: - `awaiting_live_approval` → 人工确认升实盘，否决，或取消。
#: - `live` / `rejected` / `cancelled` → 终态。**退役不走这里**，走 `Strategy.is_active`
#:   （D2：候选生命周期与人工总开关是两根互不干扰的轴）。
ALLOWED_TRANSITIONS: dict[
    StrategyCandidateStatus, frozenset[StrategyCandidateStatus]
] = {
    StrategyCandidateStatus.REGISTERED: frozenset(
        {
            StrategyCandidateStatus.IN_SAMPLE_RUNNING,
            StrategyCandidateStatus.REGISTRATION_FAILED,
            StrategyCandidateStatus.CANCELLED,
            StrategyCandidateStatus.REJECTED,
        }
    ),
    StrategyCandidateStatus.REGISTRATION_FAILED: frozenset(
        {
            StrategyCandidateStatus.REGISTERED,
            StrategyCandidateStatus.CANCELLED,
            StrategyCandidateStatus.REJECTED,
        }
    ),
    StrategyCandidateStatus.IN_SAMPLE_RUNNING: frozenset(
        {
            StrategyCandidateStatus.OOS_RUNNING,
            StrategyCandidateStatus.IN_SAMPLE_FAILED,
        }
    ),
    StrategyCandidateStatus.IN_SAMPLE_FAILED: frozenset(
        {
            StrategyCandidateStatus.IN_SAMPLE_RUNNING,
            StrategyCandidateStatus.CANCELLED,
            StrategyCandidateStatus.REJECTED,
        }
    ),
    StrategyCandidateStatus.OOS_RUNNING: frozenset(
        {
            StrategyCandidateStatus.PAPER_PENDING,
            StrategyCandidateStatus.OOS_FAILED,
        }
    ),
    StrategyCandidateStatus.OOS_FAILED: frozenset(
        {
            StrategyCandidateStatus.OOS_RUNNING,
            StrategyCandidateStatus.CANCELLED,
            StrategyCandidateStatus.REJECTED,
        }
    ),
    StrategyCandidateStatus.PAPER_PENDING: frozenset(
        {
            StrategyCandidateStatus.PAPER_RUNNING,
            StrategyCandidateStatus.CANCELLED,
            StrategyCandidateStatus.REJECTED,
        }
    ),
    StrategyCandidateStatus.PAPER_RUNNING: frozenset(
        {
            StrategyCandidateStatus.AWAITING_LIVE_APPROVAL,
            StrategyCandidateStatus.PAPER_FAILED,
        }
    ),
    StrategyCandidateStatus.PAPER_FAILED: frozenset(
        {
            StrategyCandidateStatus.PAPER_RUNNING,
            StrategyCandidateStatus.CANCELLED,
            StrategyCandidateStatus.REJECTED,
        }
    ),
    StrategyCandidateStatus.AWAITING_LIVE_APPROVAL: frozenset(
        {
            StrategyCandidateStatus.LIVE,
            StrategyCandidateStatus.CANCELLED,
            StrategyCandidateStatus.REJECTED,
        }
    ),
    StrategyCandidateStatus.LIVE: frozenset(),
    StrategyCandidateStatus.REJECTED: frozenset(),
    StrategyCandidateStatus.CANCELLED: frozenset(),
}


def _self_check() -> None:
    """import 时校验转移表与枚举**互为穷举**。

    两个方向都要查：

    - 枚举里有、表里没有 → 该状态没有出边，前进会**静默地**卡死。
    - 表里有、枚举里没有 → 不可能发生（键是枚举成员），但一旦有人把键改成字符串
      就会漏过去，所以顺手也查一次类型。

    自环也在这里拦下：它会让「合法边」与「停在本状态」混成同一个东西，穷举测试
    就没法把「判定说不动」与「判定说能走」分开断言。
    """
    missing = set(StrategyCandidateStatus) - set(ALLOWED_TRANSITIONS)
    if missing:
        raise RuntimeError(
            f"转移表漏了状态：{sorted(s.value for s in missing)}"
            "——漏掉的状态会安静地卡死（表现为「还没轮到它」），必须在启动时炸"
        )
    for current, targets in ALLOWED_TRANSITIONS.items():
        if not isinstance(current, StrategyCandidateStatus):
            raise RuntimeError(
                f"转移表的键必须是 StrategyCandidateStatus，当前 {current!r}"
            )
        for target in targets:
            if not isinstance(target, StrategyCandidateStatus):
                raise RuntimeError(
                    f"{current.value} 的目标必须是 StrategyCandidateStatus，当前 {target!r}"
                )
            if target is current:
                raise RuntimeError(
                    f"转移表里 {current.value} 有自环——自环不是转移，"
                    "它会让「停在本状态」与「合法边」混成一个集合"
                )


_self_check()


def coerce_status(value: StrategyCandidateStatus | str) -> StrategyCandidateStatus:
    """把库里取回的字符串收敛成枚举；未登记的值直接报错。

    不宽容：一个没登记的状态值若被放行，`ALLOWED_TRANSITIONS[value]` 会 KeyError，
    而 KeyError 的现场离根因很远。这里在入口就说清楚是哪个值不认识。
    """
    if isinstance(value, StrategyCandidateStatus):
        return value
    try:
        return StrategyCandidateStatus(value)
    except ValueError as exc:
        raise ValueError(
            f"未登记的候选状态 {value!r}；已登记：{[s.value for s in StrategyCandidateStatus]}"
        ) from exc


def allowed_next_statuses(
    current: StrategyCandidateStatus | str,
) -> frozenset[StrategyCandidateStatus]:
    """`current` 的全部合法目标（终态返回空集）。"""
    return ALLOWED_TRANSITIONS[coerce_status(current)]


def can_transition(
    current: StrategyCandidateStatus | str, target: StrategyCandidateStatus | str
) -> bool:
    """`current → target` 是不是一条合法边。

    **自环返回假**（见模块 docstring）：`current → current` 是「不转移」，不是边。
    调用方想表达「停在本状态」时不该调它，而该什么都不做。
    """
    return coerce_status(target) in allowed_next_statuses(current)


def is_terminal(status: StrategyCandidateStatus | str) -> bool:
    """终态：没有任何出边。

    派生自转移表而不是手列一份 `{live, rejected, cancelled}`：手列的那份会在
    转移表加边的那天变成谎话，而它恰好是判定函数「终态不再动」的依据。
    """
    return not allowed_next_statuses(status)
