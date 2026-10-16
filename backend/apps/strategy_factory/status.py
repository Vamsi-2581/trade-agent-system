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
   集合，穷举测试（196 对）才能既无歧义又无遗漏。

4. **终态是派生的，不是手列的**。`is_terminal()` 读转移表（没有出边就是终态），
   所以「给一个非终态状态配一张空出边表」会让它安静地变成终态——`_self_check()`
   查得出「漏了状态」，查不出「漏了出边」。这条对 `regime_hold` 尤其致命：它
   看上去只是暂停了一下，写成空出边就再也没有人把它放出来。
"""

from __future__ import annotations

from enum import Enum


class StrategyCandidateStatus(str, Enum):
    """候选策略的 14 态（D3 + spec #30 D1 的行情闸门）。

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
    #: **非终态中间态**：候选撞上行情闸门（当前 `apps/regime` 的 cell 状态判为
    #: 不适用）时停在这里，等闸门结论翻回「适用」再被放回它进来的那一层（spec #30
    #: D1 / 用户故事 3）。三件事必须一起成立，缺一件这个状态就不再是「暂停」：
    #:
    #: 1. **不是失败、不是否决、不是取消**。它不进 `*_failed` 那三个终态方向的
    #:    结论——候选没被判定为不合格，只是「现在这片行情不宜打钱」。
    #: 2. **必须有出边**。`is_terminal()` 是派生的（没有出边即终态），所以给这张
    #:    表配空出边会让它**安静地变成终态**：候选停在这里，而 nobody 再推它——
    #:    表现与「还没轮到它」一模一样，只能靠人肉翻库发现。
    #: 3. **保留人工收手的两条边**（`cancelled` / `rejected`）。「等行情过去」不
    #:    能剥夺「我现在就想停掉它」的权利——用户故事 3 原文的后半句是「且我随时
    #:    能取消它」。
    #:
    #: 名字不用 `RegimeGate` / 裸 `Efficiency`（spec #30 D12 的词汇避让表）：前者
    #: 与 `apps/regime.gate` 同名不同义，后者没有主词、读不出「谁停在这里」。
    REGIME_HOLD = "regime_hold"
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
    #: 「行情闸门结论是暂缓」而不是「不适用所以作废」——这条状态在语义上是一次暂停，
    #: 文案必须让它读起来能自己恢复（对比 `oos_failed` 的「样本外未过」）。
    StrategyCandidateStatus.REGIME_HOLD: "等行情闸门结论",
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


class StrategyCandidateLayer(str, Enum):
    """一次推进落在**哪一层**（转移记录的归属列，spec #19 D20 / D3）。

    为什么要单独一列，而不是从 `reason_code` 推：`INSUFFICIENT_EVIDENCE` 是跨层的
    原因码（样本内样本量不足、分折缺 K 线、paper 成交太少都用它），光看它答不出
    「是哪一层刷掉的」；而「统计哪一层刷掉最多」正是漏斗自身可归因的入口。
    状态也推不出层：`in_sample_failed` 是「L1 判完的结果」，但 `registered →
    in_sample_running` 这一步同样是 L1 的动作。

    `pipeline` 是不属于任何单层判定的那一档（登记失败、等人工卡点、总开关关闭）——
    它存在，是为了让本列在每一条转移记录上都有值，而不是留一片空。
    """

    IN_SAMPLE = "in_sample"
    OOS = "oos"
    PAPER = "paper"
    PIPELINE = "pipeline"

    @property
    def display(self) -> str:
        return _LAYER_DISPLAY[self]

    @classmethod
    def choices(cls) -> list[tuple[str, str]]:
        return [(m.value, m.display) for m in cls]


_LAYER_DISPLAY = {
    StrategyCandidateLayer.IN_SAMPLE: "L1 样本内",
    StrategyCandidateLayer.OOS: "L2 样本外",
    StrategyCandidateLayer.PAPER: "L3 paper",
    StrategyCandidateLayer.PIPELINE: "流水线",
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
#: - `oos_running` → 样本外过了等 paper 账户/排队；没过落 `oos_failed`；
#:   撞上行情闸门时暂停进 `regime_hold`（spec #30 D1，**不是不合格**）。
#: - `oos_failed` → 改分折配置重跑，或人工收尾。
#: - `paper_pending` → 账户就绪且不排队就开始观察，或人工收尾。
#: - `paper_running` → 观察期满判定：过 → 等人工卡点；没过 → `paper_failed`；
#:   升实盘前再撞一次行情闸门，同样暂停进 `regime_hold`。
#: - `paper_failed` → 重新观察，或人工收尾。
#: - `awaiting_live_approval` → 人工确认升实盘，否决，或取消。
#: - `regime_hold` → 闸门结论翻回「适用」时**原路退回**它进来的那一层
#:   （`oos_running` / `paper_running`，由编排层的 5 分钟扫描释放），
#:   或人工随时收手。它**没有**去 `*_failed` 的边：暂停不是判定。
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
            # 升 paper 之前的行情闸门。放在这里而不是替换 `paper_pending`：闸门结论
            # 会翻回来，而「排队等账户」翻回来也是回到这一层，两者共用同一个回边。
            StrategyCandidateStatus.REGIME_HOLD,
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
            # 升实盘前的第二道行情闸门。与 `oos_running` 那侧对称：同一份判决，
            # 同一个暂存处，同一个回边。
            StrategyCandidateStatus.REGIME_HOLD,
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
    #: **非终态**：回边两条 + 人工收手两条，共 4 条出边。
    #:
    #: 回边是「原路退回」而不是「重头再来」：候选从 `oos_running` 停进来的，翻回
    #: 去还是 `oos_running`（该层的证据与起跑记录都还在，重判一次即可）；从
    #: `paper_running` 停进来的同理。走一条新的「regime_released」态会丢掉「我在
    #: 等哪一层」这个信息，而调用方只能再猜一次。
    #:
    #: `cancelled` / `rejected` 必须在这里：用户故事 3 的后半句「且我随时能取消它」
    #: 是一句验收，不是一句愿望。
    StrategyCandidateStatus.REGIME_HOLD: frozenset(
        {
            StrategyCandidateStatus.OOS_RUNNING,
            StrategyCandidateStatus.PAPER_RUNNING,
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
