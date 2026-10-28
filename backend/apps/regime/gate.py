"""行情阶段 gate 的判定层——**纯函数**（第③段单元 ③a）。

与 `deactivation.py` / `deactivation_run.py` 的分工同构：本模块不 import ORM 模型、不查库、
不写日志、不发通知。输入是一组 frozen dataclass 值对象，输出是「这一轮期望的声明集 +
每条决策行的目标 status + 每个该被解除的作用域该写的解除原因」。取数与落库在
`gate_run.py`；开关的确认页与唯一写入口在 `gate_switch.py`。

**为什么把判定单独切出来（Q1）**：容易错的是**三态逻辑**——「有在期豁免」「开关开着」
「判定翻面」三个量组合出的那一张表——而它必须能在没有数据库、不付 `transaction=True`
那 70 秒代价的情况下逐格钉住。取数与落库是另一件事，也是另一类风险。

## 这一层在「停止」这件事里的位置

`HaltDeclaration` 表的唯一对账方是 `halt_sync._reconcile`；**本模块不写表**，它只回答
「期望集是什么」。事件与保命档的期望集 `halt_sync` 自己算得出来，策略停用决策的算不出来
（那要一整条池化推导），所以 `halt_sync.sync` 只在调用方递了期望集时才替这一档说话——
见它的 `gate_plan` 参数。

## 声明 ⟺ 「机制此刻在拦」，而 `status` 记的是机制**做过什么**

`DeactivationDecision.status` 由两个写方各写一半（Q9 的事实核查）：`deactivation_run`
只写 `suggested`，本层只写 `applied` / `released`。本层的规则是：

    机制声明了它 → `applied`；机制解除过它 → `released`

所以 `released` 在这里的意思是「机制**撤回过**这条」，不是「这条判定不成立」——一条被
撤回过、后来又因为阶段回来而重新成立的，会被重新声明（回到 `applied`）。这条区分有一处
代价：本层**不给「机制从没碰过」的行写 `released`**（见 `_untouched`），否则那张表会开始
记「它当时在拦」的假历史——与 Q3 拒绝在 Shadow 期预写声明行是同一条理由，换到 status 上
一样成立。

**冷启动与状态过期时一个字都不动**（`deactivation.BLOCKED_*`）：机制此刻说不清「现在是
什么阶段」，而「说不清就解除」会让状态过期变成一次无声的机制失效——那正是这套机制最
需要生效的时刻。保持现状（活行继续拦）是这里唯一的保守方向。

**保命档（高波动）期间同样一个字都不动**：高波动是**叠加层**（CONTEXT.md 第 115 条），
不是「离开了原阶段」——而本层的逐行判定拿的是 `regime == 当前阶段` 这把尺子，高波动一
抬上来，每一行都会被判成「阶段离开」而解除。那是假话：阶段并没有离开，只是被叠了一层。
保命档那一层自己有全场作用域的声明（并且它期间整个市场一律停），所以策略档这一层无事
可做。取向与 `deactivation_run.close_left_regime_exemptions` 的第三条停手完全同构：两个
方向不对称（误解除不可逆、晚一轮可逆），就往不会造成不可逆损失的那边倒。

## 两路判定：停用决策 与 按阶段暂停（issue #51）

本模块产**两路**期望声明，由 `merge` 合成一轮交给同一个写方：

- `derive()` —— **停用决策**那一路（第③段）：由 `DeactivationDecision` 行派生，回答「机制
  是否决定停用这条策略」。它写决策行的 `status`（`applied` / `released`）、进日报的
  「该停 N」、解除要人工确认。
- `derive_suspensions()` —— **按当前生效阶段暂停开新仓**那一路：由全部被管策略 × 当前代
  那一格派生，回答「此刻它有没有资格开新仓」。它**不产生决策行、不写 status、不进那个
  计数**，并且随阶段自己翻回去。

这一路为什么是**另一条触发源**而不是复用停用那条：两者的唯一键都是「触发源 × 作用域」，
复用同一条会在「停用须人工恢复」这条地基上开一个洞——一条下个阶段自己就失效的暂停，会被
记成一次机制做过的停用决定。两路的判据**同源**（都出自 `admission.py` 那一个纯函数，这是
本票的另一条纪律：判据只有一处）。

**`neutral` 在两路里的含义必须分开读**：暂停路拦住它，说的是「证据不足 ⇒ 先别开新仓」；
deactivation 维度那句「中性不构成停用理由」讲的是「不该把证据不足当成一条停用决策」。同一
格、两个问题、两个答案，各自都对。

## 五个解除原因码

词表本体在这里（本层是唯一的产出方），展示名在 `gate_switch.py`（Q7）：
`regime_left` / `became_fit` / `exempted` / `gate_closed` / `strategy_gone`。
取值与 `halt_sync.CLOSE_REASON_*`、`deactivation_run.CLOSE_REASON_*` 同一口径——
**各定义各的**，不互相 import：三张表的词是三个不同的东西，共用常量只会让改一处
动三处。有一条测试钉住「五个码与 `DecisionStatus` 的取值都不飘」。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Mapping

from apps.regime import admission, deactivation
from apps.regime.quant import BaseRegime
from apps.regime.slice import REASON_DISPLAY, STATE_DISPLAY

# --------------------------------------------------------------------------- #
# 词表
# --------------------------------------------------------------------------- #

#: 决策行的 status。与 `DecisionStatus` 的取值一致（那边是 ORM 模型，本层不能 import）。
STATUS_SUGGESTED = "suggested"
STATUS_APPLIED = "applied"
STATUS_RELEASED = "released"

#: 这一格此刻还算不算「该停用」——由 `gate_run` 把 `deactivation.Derivation` 与在册/
#: 被管状态翻译过来。**三态而不是布尔**：`lapsed` 与 `gone` 要在解除原因上分开
#: （一个是判据变了，一个是它已经不归这套机制管了），合成布尔就再也分不出来。
#:
#: `lapsed` 是个统称，它盖住「重新适配」「依据方向冲突（`needs_review`）」「这一格在当前代
#: 消失」三种收场：它们共同的定义是**当前代不再产出这条停用建议**。对用户说的那句话取
#: 其代表（「该策略在本阶段重新适配」），更细的那一层在日报的池化段里。
STILL_TARGET = "still_target"
LAPSED = "lapsed"
GONE = "gone"

#: 解除原因码。展示名在 `gate_switch.GATE_CLOSE_REASON_DISPLAY`。
#: `regime_left` **不复用** `halt_sync` 的同名常量（那边的文案是「阶段已离开高波动」，
#: 保命档的话），也不复用 `deactivation_run` 的（那边是豁免行自己的收场）。
CLOSE_REGIME_LEFT = "regime_left"
CLOSE_BECAME_FIT = "became_fit"
CLOSE_EXEMPTED = "exempted"
CLOSE_GATE_CLOSED = "gate_closed"
CLOSE_STRATEGY_GONE = "strategy_gone"

#: 本层的两路声明各自的触发源字面量。**必须与 `models.HaltTrigger` 的取值一致**，
#: 而本层不能 import 它（那边连着 ORM 模型）——所以按 `STATUS_*` 的同一套做法在这里
#: 立一份字面量，并由一条测试钉住「两边不飘」。
#:
#: 为什么是**两个**触发源而不是一个（本票的裁决）：`deactivation` 记的是「机制决定
#: 停用这条策略」——它进日报的「该停 N」，而且解除要人工确认，是那条地基的产物；
#: `regime_suspension` 记的是「按当前生效阶段暂停开新仓」——它随阶段自动翻转，不进
#: 停用口径。两件事的唯一键（触发源 × 作用域）因此各占一行，同一个策略可以同时被
#: 两者拦（那时拒绝理由里两条理由都在，`HaltVerdict.reason` 本来就逐层都写）。
TRIGGER_DEACTIVATION = "deactivation"
TRIGGER_REGIME_SUSPENSION = "regime_suspension"

#: 声明行的 `label` 前缀。与 `HaltTrigger.DEACTIVATION.display`（「策略停用决策」）同一套话，
#: 而 `label` 进的是 `HaltLayer.text`，也就是**下单拒绝理由**与 `query_halt`：在那里
#: 「拦全场」与「只拦某个策略」的差别必须一眼看得出来。
LABEL_PREFIX = "策略停用决策："

#: 机制回 Shadow 时这一层要说的话（进任务摘要与日志）。
NOTE_SHADOW = (
    "机制在 Shadow 档：不再拦，活行按「机制已回 Shadow」解除；决策行的状态一个字都不写"
)

#: 当前阶段是保命档（高波动）时这一层要说的话（进任务摘要与日志）。与 `NOTE_SHADOW`
#: 有着**相反**的取向：Shadow 期是「不声明、但要解除」，保命档期是「声明与解除都不做」。
#: 两句分开写而不是共用一句，因为它们是两个不同的理由，页面上要能分得清是哪一个。
NOTE_BLANKET = (
    "当前阶段是保命档（高波动）：全场停用由保命档那一层负责，本层不产出策略档声明，"
    "也不解除任何东西（阶段并没有离开，只是被叠了一层）"
)

#: `GatePlan.blocked` 的第四个码。前三个（`cold_start` / `stale_state` /
#: `deactivation_run.SKIPPED_NO_GENERATION`）都是「这一轮**说不清**」；这一个不同：
#: **阶段说得很清楚，是这一层不该动手**。放进同一个字段是因为下游（`gate_run.sync`
#: 的 `skipped`、确认页的「本轮一个字都不会动」分支）要的语义正是「本轮什么都没做」，
#: 而那个语义只有一个出处。取值与 `deactivation.BLANKET` 同字而**各定义各的**：那边是
#: 池化格子的判决词，这边是判定层的收场码，两个词表不是同一个东西（同 Q7 的纪律）。
BLOCKED_BLANKET = "blanket"


# --------------------------------------------------------------------------- #
# 输入
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Exemption:
    """当前阶段某一格的人工豁免。**在期与刚结束的都算。**

    判定只问两个问题：「现在豁免着吗」（`in_force`）与「豁免窗口结束于什么时候」
    （`ends_at`）。后者只在豁免**已经结束**时才用得上，而它恰恰是那时候最难拿到的
    事实——所以由 `gate_run` 现查一次带过来（Q5）。
    """

    in_force: bool
    #: 在期时是 `expires_at`；已结束时是 `closed_at or expires_at`（提前收回的豁免，
    #: 窗口结束于收回那一刻，不是原定的到期时刻）。
    ends_at: datetime


@dataclass(frozen=True)
class DecisionRef:
    """一条决策行的只读快照（`DeactivationDecision` 的直译，不是那一行本身）。

    `decision_id` 只为回写定位，不参与判定；`status` 是**写之前**的值——回写要拿它当
    比对值做条件更新（`filter(id=…, status=旧值).update(status=新值)`），否则两轮之间
    有别的东西动过这一行时，本层会拿一份过期的判断覆盖它。
    """

    decision_id: Any
    strategy_id: Any
    strategy_name: str
    regime: str
    status: str
    #: `STILL_TARGET` / `LAPSED` / `GONE`。
    warrant: str
    #: 当前代这一格「为什么判它不适用」的原文（`Outcome.reason`）。只进给人看的那两句话，
    #: 不参与判定；取不到时留空。
    cell_reason: str = ""


@dataclass(frozen=True)
class Situation:
    """这一轮的世界状态。**全是从库里读出来的事实**（或事实的直译），本模块一个都不去查。"""

    gate_open: bool = False
    #: 开关「开」的那一刻——`RegimeMechanismSwitch` 最新一行的 `at`。`gate_open` 时必有值
    #: （没有那条流水就开不了，见 `RegimeMechanismSwitch.current`）。
    switch_at: datetime | None = None
    #: 当前生效阶段；`None` = 冷启动。
    regime: str | None = None
    #: 当前那条判定的生效时刻（北京 08:00 那个业务日边界）。
    regime_effective_at: datetime | None = None
    #: 当前代池化表落地的时刻。
    generation_at: datetime | None = None
    #: **当前阶段**上的豁免，按策略 id 索引（其它阶段的豁免与本层无关：非当前阶段的行
    #: 一律按 `regime_left` 解除）。
    exemptions: Mapping[Any, Exemption] = field(default_factory=dict)
    #: `deactivation.BLOCKED_*`；非空 ⇒ 本轮什么都不动。
    blocked: str = ""


# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Declaration:
    """一条期望的声明行。作用域由 `gate_run` 拼（`halt.strategy_scope`）——本层不 import
    `halt`，它连着 ORM 模型。"""

    strategy_id: Any
    label: str
    reason: str
    #: 见 `attitude_since`。
    opened_at: datetime
    #: 策略停用没有预先知道的截止时刻（Q10 的「… 起（截止时刻不定）」；第③段 Q5 之后
    #: 这句话只对**事件熔断**那一档说「窗口：」，这一档说的是「生效：」——
    #: `halt_notify._period_line` 按触发源分岔。
    expires_at: datetime | None = None
    #: 这一行是哪一路产出的（`TRIGGER_DEACTIVATION` / `TRIGGER_REGIME_SUSPENSION`）。
    #: 默认值是停用决策那一档：`derive` 那条老路逐字未变，它不需要知道还有第二路。
    trigger: str = TRIGGER_DEACTIVATION


@dataclass(frozen=True)
class SuspensionRef:
    """一个**被管策略**在这一轮里的只读快照，供「按阶段暂停」那一路判适用性。

    与 `DecisionRef` 的区别是**它不由任何决策行派生**：停用路只为「有人建议过停用」的
    策略判，而暂停路要覆盖**全部被管策略**——「只放行 fit」问的正是「每一个在跑的策略
    在这片行情里有没有资格开新仓」，与它有没有被建议停用无关。

    `cell_state` 取**当前代 × 当前生效阶段**那一格；`None` = 这一代里没有这一格
    （新策略 / 新阶段 / 窗口未覆盖）。豁免是算好的纯值（在期与否）——与
    `exemption` 的既有纪律一致：在期由取数侧判完，判定层只看见结论。
    """

    strategy_id: Any
    strategy_name: str
    #: `slice.STATE_*` 之一；`None` = 当前代没有这一格。
    cell_state: str | None
    #: 当前代这一格「为什么是这个结论」的原文（`Outcome.reason`），只进给人看的句子。
    cell_reason: str = ""
    #: 在期人工豁免（键为「策略 × 当前阶段」）。豁免是**人按住了它**。
    exempt: bool = False
    #: 人工退役（`Strategy.is_active=False`）。**退役即不再参与求值**（CONTEXT.md：记录保留、
    #: 求值不再算它）——与停用路同一条纪律（那边 `_warrant` 判 `GONE`）。它不是「这一格判成
    #: 什么」的问题，所以走在判据之前：一条跑不起来的策略在这片行情里有没有资格开仓，问不出
    #: 答案（没有会话、没有单），而给它写一条声明会让声明表与「按阶段暂停 N 条」那个数多出
    #: 一批空动作。
    retired: bool = False


@dataclass(frozen=True)
class StatusWrite:
    """一条**真的会改**的 status 回写（目标值 == 旧值的那些不进这张表）。"""

    decision_id: Any
    status: str
    #: 写之前的值：条件更新的比对值。
    old_status: str


@dataclass(frozen=True)
class GatePlan:
    """一轮判定的全部产出。

    `close_reasons` 按**策略 id** 索引（一条活声明的唯一键就是策略 id：作用域
    `strategy:<id>`，一个策略同时只有一条活声明），`gate_run` 把它翻成作用域交给
    `halt_sync`。只列「本层要解除的行」；一条活行既不在期望集里又不在这张表里，
    是写入方的 bug（`halt_sync._reconcile` 按 fail-closed 处理：不动它）。
    """

    declarations: tuple[Declaration, ...] = ()
    statuses: tuple[StatusWrite, ...] = ()
    close_reasons: Mapping[Any, str] = field(default_factory=dict)
    #: 非空 ⇒ 本轮**一个字都没动**（含期望集为空、没有任何回写）。
    blocked: str = ""
    #: 给人看的一句话（任务摘要与日志）。空串 = 正常一轮。
    note: str = ""


# --------------------------------------------------------------------------- #
# 判定
# --------------------------------------------------------------------------- #


def _regime_display(regime: str | None) -> str:
    """slug → 中文展示名；取不到就原样回显 slug，**不猜**（与 `report._regime_display`
    同一口径，各写一遍是因为 `report` 连着 ORM 与本层无关）。"""
    if not regime:
        return "（无）"
    try:
        return BaseRegime(regime).display
    except ValueError:
        return str(regime)


def _label(row: DecisionRef) -> str:
    """声明行的 `label`：进 `HaltLayer.text`（下单拒绝理由与 `query_halt`），带上策略名。"""
    head = f"{LABEL_PREFIX}{row.strategy_name}"
    return f"{head}（{row.cell_reason}）" if row.cell_reason else head


def _reason(situation: Situation, row: DecisionRef) -> str:
    """声明行的 `reason`：进通知正文（`依据：…`）与日报。

    **策略名只在这里进得了用户的眼睛**（Q10 的核查：通知正文只打触发源、作用域与
    `reason`，`label` 不在正文里）。所以这句话要自带主语——读的人手上只有一行
    「作用域 策略 <uuid>」。
    """
    detail = f"：{row.cell_reason}" if row.cell_reason else ""
    return (
        f"{row.strategy_name} 在当前「{_regime_display(situation.regime)}」阶段判为不适用"
        f"{detail}。机制按策略停用决策拦它的开仓（减仓放行）。"
    )


def _declared(situation: Situation, row: DecisionRef) -> bool:
    """这一行此刻该不该有一条声明。三个条件缺一不可。"""
    if row.regime != situation.regime:
        return False
    if row.warrant != STILL_TARGET:
        return False
    exemption = situation.exemptions.get(row.strategy_id)
    return not (exemption and exemption.in_force)


def _untouched(row: DecisionRef, target: str) -> bool:
    """「机制从没碰过这一行」——**不给它们写 `released`**。

    判据只看得见 `status`，与阶段无关：`suggested` 是**只有 `deactivation_run` 写过**的
    值（本层只写 `applied` / `released`），所以「这一行的 status 还是 `suggested`」与
    「机制从没碰过它」是同一件事。反过来，机制真声明过它（`applied`）就必须能收回——
    那正是「它当时在拦」这条记录的正确收场。

    于是这一格只有一种形状：**要写 `released`、而旧值还是 `suggested`** ⇒ 不写。
    它与「这一行为什么不该有声明」无关：阶段对不上、判据变了、策略离场、豁免在期，
    四条路都到不了这一行——一条没人认领的**建议**在哪种收场下都不该被记成「机制解除过
    它」。它同时还在 `replay_run` 的重放集合里（`exclude(status=RELEASED)`），那是它该
    待的地方：一条没人认领的建议值得复核，一条没人认领的停用不值得。
    """
    return target == STATUS_RELEASED and row.status == STATUS_SUGGESTED


def _close_reason(situation: Situation, row: DecisionRef) -> str:
    """这一行此刻不该有声明时，活行该记的解除原因。**顺序有意义**：

    阶段先于一切（行上的阶段不是当前阶段时，「它还算不算建议」都没意义了），豁免次之
    （豁免是「人按住了它」，比「判据变了」更靠前也更可执行），最后才分判据与归属。
    """
    if row.regime != situation.regime:
        return CLOSE_REGIME_LEFT
    exemption = situation.exemptions.get(row.strategy_id)
    if exemption and exemption.in_force:
        return CLOSE_EXEMPTED
    if row.warrant == GONE:
        return CLOSE_STRATEGY_GONE
    return CLOSE_BECAME_FIT


def attitude_since(situation: Situation, strategy_id: Any) -> datetime:
    """**机制对这个作用域「拦」的态度是从哪一刻开始的**——声明行的 `opened_at`。

    取库里存好的事实里**最晚**的那一个。这是 Q4 的纪律（状态行里的时刻取自事实，不取自
    任务跑起来的时刻）在「态度被中断过」这种情况下的推广；取 `now()` 的表现是
    `halt_sync._rewrite` 每 300 秒看见一次变化，于是 `opened_notified_at` 被反复清空、
    用户被反复通知同一条窗口开启。

    四个候选，各自是「机制从这一刻起有资格/有理由拦」的一个必要条件：

    - 开关「开」的那一刻——机制**有能力**拦；
    - 当前阶段判定的 `effective_at`——机制**有理由**拦；
    - 当前代池化表落地的时刻——**这条判据**成立的起点。代与代之间判定不会翻面（格子的
      `state` 是代里的存量字段），所以「翻面又翻回来」必然落在一代新的表上，这个事实总是
      取得到；
    - 这一格最近一次豁免结束的时刻——豁免期间机制的态度是「不拦」，态度从这里**重新**
      开始（Q5(iii)：这一行记的是态度不是动作，机制晚跑了多久是机制的问题）。

    四者取最晚 = 「当前这一段不间断的『拦』从什么时候开始」。少算任何一个，声明行都会
    声称机制在某段时间里拦着、而事实是那段时间它没拦。
    """
    if situation.switch_at is None:
        # 开关开着却没有开启时刻，是取数层的 bug：声明行的 `opened_at` 是必填的，
        # 而这里没有任何可以拿出来的事实（与 `halt_sync._blanket_row` 的 RuntimeError 同款）。
        raise RuntimeError("声称机制在拦，却拿不到开关的开启时刻：取数层漏了 switch_at")
    candidates = [
        situation.switch_at,
        situation.regime_effective_at,
        situation.generation_at,
    ]
    exemption = situation.exemptions.get(strategy_id)
    if exemption is not None and not exemption.in_force:
        candidates.append(exemption.ends_at)
    return max(at for at in candidates if at is not None)


def derive(situation: Situation, decisions: Iterable[DecisionRef]) -> GatePlan:
    """一轮判定。**纯函数**：同样的输入永远给同样的输出，不读时钟、不碰库。

    四条早退分支，每一条都是「什么都不做」而不是「按空集解除」：

    1. `blocked` / 冷启动——机制说不清当前是什么阶段。解除了就再也回不来（那段时间事后
       无法重建），而保持现状只是「今晚本不该拦的拦着」。取向与 `_halt_block_reason`
       的 fail-closed 一致。
    2. 开关关着（Shadow）——**不声明，也不写 status**。理由见模块 docstring：Shadow 期
       没有「机制开始拦」这个动作，写 `applied`/`released` 都是假历史（Q3）。活行照样要
       解除：回到 Shadow 的意义就是不再拦，留着活行既拦不住（`halt.switch_open` 关着）
       又会让「重新打开开关」那一刻的期望集与现状之间出现一段没人解释的空档。
    3. 当前阶段是保命档（高波动）——**声明与解除都不做**（见模块 docstring）。排在逐行
       判定之前是必须的：逐行那一支拿 `regime == 当前阶段` 当尺子，高波动一抬上来它会把
       每一行都判成「阶段离开」并写进 `close_reasons`，于是活行被解除、决策行被翻成
       `released`——一句假话，而解除的代价是不可逆的。排在第 2 条**之后**：Shadow 那一支
       说的「机制确实不再拦」在高波动期间同样为真，而它要挡的是**按阶段比较得出的理由**，
       那个比较才是假的。
    4. 其余：逐行判。
    """
    rows = tuple(decisions)

    if situation.blocked or situation.regime is None:
        code = situation.blocked or deactivation.BLOCKED_COLD_START
        return GatePlan(
            blocked=code,
            note=f"本轮什么都不动：{deactivation.BLOCKED_DISPLAY.get(code, code)}",
        )

    if not situation.gate_open:
        return GatePlan(
            close_reasons={row.strategy_id: CLOSE_GATE_CLOSED for row in rows},
            note=NOTE_SHADOW,
        )

    if deactivation.is_blanket(situation.regime):
        return GatePlan(blocked=BLOCKED_BLANKET, note=NOTE_BLANKET)

    declarations: list[Declaration] = []
    statuses: list[StatusWrite] = []
    close_reasons: dict[Any, str] = {}

    for row in rows:
        declared = _declared(situation, row)
        if declared:
            declarations.append(
                Declaration(
                    strategy_id=row.strategy_id,
                    label=_label(row),
                    reason=_reason(situation, row),
                    opened_at=attitude_since(situation, row.strategy_id),
                )
            )
        else:
            close_reasons[row.strategy_id] = _close_reason(situation, row)

        target = STATUS_APPLIED if declared else STATUS_RELEASED
        if target != row.status and not _untouched(row, target):
            statuses.append(
                StatusWrite(
                    decision_id=row.decision_id, status=target, old_status=row.status
                )
            )

    return GatePlan(
        declarations=tuple(declarations),
        statuses=tuple(statuses),
        close_reasons=close_reasons,
    )


# --------------------------------------------------------------------------- #
# 第二路：按当前生效阶段暂停开新仓（issue #51）
# --------------------------------------------------------------------------- #

#: 暂停路声明行的 `label` 前缀。与 `LABEL_PREFIX`（「策略停用决策：」）**必须能一眼分开**：
#: `label` 进的是 `HaltLayer.text`，也就是下单拒绝理由与 `query_halt`——「机制决定停用
#: 这条策略」与「这片行情里它没有资格开新仓」是两句不同的话，混成一句会让「为什么拦我」
#: 答成另一个问题（前者要人工恢复，后者随阶段自己翻回去）。
SUSPENSION_LABEL_PREFIX = "按阶段暂停开仓："


def _cell_detail(ref: SuspensionRef) -> str:
    """这一格「为什么是这个结论」——**展示名取自 `slice` 的词表**，不在这里另写一句中文。

    与 `halt_sync._blanket_reason` 同一条纪律：同一层在拒单理由、日报与 `query_halt` 里必须
    同措辞。而库里存的是**短码**（`RegimePoolCell.reason` 落的是 `slice` 的键），所以这一翻
    是必需的：少了它，`insufficient_evidence` 会原样贴进用户的拒单理由里——那是一句给机器
    看的话，而这条 `label` 恰恰是「为什么拦我」的答案。

    两层兜底都不猜：`cell_reason` 取不到就退到**状态词**的展示名（`neutral` → 「中性」）；
    两者都没有（无格）留空串，由调用方去说那句「池化表里没有这一格」。词表里查不到的码
    原样回显：一个陌生的码是给人查的线索，凭空编一句中文会把「这里有个我们不认识的结论」
    掩盖掉。
    """
    if ref.cell_reason:
        return REASON_DISPLAY.get(ref.cell_reason, ref.cell_reason)
    if ref.cell_state is not None:
        return STATE_DISPLAY.get(ref.cell_state, ref.cell_state)
    return ""


def _suspension_label(ref: SuspensionRef) -> str:
    head = f"{SUSPENSION_LABEL_PREFIX}{ref.strategy_name}"
    detail = _cell_detail(ref)
    return f"{head}（{detail}）" if detail else head


def _suspension_reason(
    situation: Situation, ref: SuspensionRef, verdict: admission.Verdict
) -> str:
    """暂停路声明行的 `reason`。**「判不了」与「判为不适用」要说成两句不同的话**：

    前者是证据不足（会被下一轮重算翻回去，也可能永远翻不回去——那正是要让人看见的
    那个事实），后者是池化结论明确说不行。合成一句会让用户按「策略不行」去处置一个
    只是没证据的策略，而两者的正确处置完全不同（一个是去补证据或换策略，一个是这片
    行情它确实不该开仓）。

    尾巴也分两种：`unfit` 是**明确结论**，不欠那句「判不了 ≠ 放它过去」；其余三种
    （中性 / 未知 / 无格）都是「判不了」，那句话正是它们要防的误读。

    **「没有这一格」单说一句**：它与「格子是中性」不是同一件事——一个是「池化表里压根
    没有这条策略这一阶段」（新策略 / 新阶段 / 窗口未覆盖），一个是「有结论但证据不足」。
    两者都 fail-closed，但读的人该知道该去补哪一样。
    """
    regime = _regime_display(situation.regime)
    detail = _cell_detail(ref)
    if ref.cell_state is None:
        judged = "判不了（池化表里没有这一格：策略新、阶段新、或窗口未覆盖）"
        tail = "机制暂停它的开仓；减仓照常放行——「判不了」不等于「放它过去」。"
    elif verdict.code == admission.CODE_UNFIT:
        judged = f"判为不适用（{detail}）" if detail else "判为不适用"
        tail = "机制暂停它的开仓；减仓照常放行。"
    else:
        judged = f"判不了（{detail}）" if detail else "判不了"
        tail = "机制暂停它的开仓；减仓照常放行——「判不了」不等于「放它过去」。"
    return f"{ref.strategy_name} 在当前「{regime}」阶段{judged}。{tail}"


def _suspension_close_reason(verdict: admission.Verdict) -> str:
    """暂停行此刻不该有时，该记的解除原因。

    只可能是三种收场：**重新适配**（格子回 `fit`）、**有人豁免了它**、**保命档那一层
    接手了**。第三种归到 `became_fit` 这个统称上，与 `STILL_TARGET` / `LAPSED` 的既有
    口径同构——`LAPSED` 是「当前代不再产出这条建议」的统称，对它说的那句话取其代表。
    不新增一个解除原因码：那张词表是 `gate_switch.py` 的展示面，多一个只为解释一个
    角落情形的词，会让「今天和昨天有什么不同」多出一行没人看得懂的话。
    """
    if verdict.code == admission.CODE_EXEMPT:
        return CLOSE_EXEMPTED
    return CLOSE_BECAME_FIT


def derive_suspensions(situation: Situation, refs: Iterable[SuspensionRef]) -> GatePlan:
    """暂停路的一轮判定。**纯函数**，与 `derive` 同一套形状与早退纪律。

    回答的是「此刻哪些被管策略没有资格开新仓」：全部被管策略逐个问
    `admission.admission`（当前生效阶段 × 它在这一代里那一格），suspend 的产出一条
    期望声明，放行的记一条解除原因。

    四条早退分支与 `derive` **逐条对齐**（理由那边写得比这里细，这里只说差异）：

    1. `blocked` / 冷启动——说不清现在是什么阶段，什么都不动。取向是 fail-closed：
       活行继续拦着。**注意「什么都不动」在这里不等于 fail-open**：暂停路要产出的正是
       拦人的行，不动它等于保持上一轮的拦。
    2. 开关关着（Shadow）——不声明，但要解除：回到 Shadow 的意义就是不再拦。
    3. 保命档（高波动）——声明与解除都不做。它自己那一层有全场声明，而且这一段里
       「按阶段比较」这件事本身没有意义（阶段没离开，只是被叠了一层）。
    4. 其余：逐个被管策略判。

    **与停用路的关系不是替代**：停用路记「机制决定停用这条策略」（人工恢复的地基，
    进日报「该停 N」），本路记「按当前阶段没有资格开新仓」（随阶段自动翻转，不进那个
    计数）。两路在 `merge` 里合成一轮的期望集，同一个策略同时被两者拦住时，**留停用
    那条**——理由更强的那个先说，免得拒单理由里出现两条说同一件事的话。
    """
    rows = tuple(refs)

    if situation.blocked or situation.regime is None:
        code = situation.blocked or deactivation.BLOCKED_COLD_START
        return GatePlan(
            blocked=code,
            note=f"本轮什么都不动：{deactivation.BLOCKED_DISPLAY.get(code, code)}",
        )

    if not situation.gate_open:
        return GatePlan(
            close_reasons={ref.strategy_id: CLOSE_GATE_CLOSED for ref in rows},
            note=NOTE_SHADOW,
        )

    if deactivation.is_blanket(situation.regime):
        return GatePlan(blocked=BLOCKED_BLANKET, note=NOTE_BLANKET)

    declarations: list[Declaration] = []
    close_reasons: dict[Any, str] = {}
    for ref in rows:
        if ref.retired:
            # 退役的策略**不参与求值**，但必须**主动**写一条解除原因：只「不产出」的话，
            # 它名下可能活着的旧声明既不在期望集里、又不在解除名单里——`halt_sync._reconcile`
            # 会按 fail-closed 把它原样留着，于是那条行继续拦一张根本跑不起来的策略的单
            # （看起来完全正常，且永远不会自己好）。取向与停用路的 `GONE` 逐字一致。
            close_reasons[ref.strategy_id] = CLOSE_STRATEGY_GONE
            continue
        verdict = admission.admission(
            situation.regime, ref.cell_state, exempt=ref.exempt
        )
        if verdict.suspend:
            declarations.append(
                Declaration(
                    strategy_id=ref.strategy_id,
                    label=_suspension_label(ref),
                    reason=_suspension_reason(situation, ref, verdict),
                    opened_at=attitude_since(situation, ref.strategy_id),
                    trigger=TRIGGER_REGIME_SUSPENSION,
                )
            )
        else:
            close_reasons[ref.strategy_id] = _suspension_close_reason(verdict)

    return GatePlan(declarations=tuple(declarations), close_reasons=close_reasons)


def merge(base: GatePlan, suspension: GatePlan) -> GatePlan:
    """把停用路与本路合成**一轮**的期望集，交给同一个写方（`halt_sync._reconcile`）。

    三条合成规则，每条都有它挡着的事故：

    1. **任一 plan `blocked` ⇒ 整轮什么都不动**，产出丢弃。`blocked` 的语义就是「这一轮
       算不出来」或「这一层不该动手」，而它的两个来源（冷启动 / 状态过期 / 没有当前代 /
       保命档）都是**同时覆盖两路**的——合成时把一方的产出留下而另一方说 blocked，
       就成了「半个轮次」，那是比不跑更坏的状态。
    2. **同一个策略两路都声明时留停用那条**（`deactivation` 在前）。两条都留的代价在
       用户侧：拒单理由会出现两句说同一件事的话，而它们的处置方式不同（一个要人工恢复、
       一个随阶段自动翻），读的人得先猜是哪一种。留强的那个说清楚了这件事。
    3. **解除原因以停用路优先**（`base.close_reasons` 覆盖 `suspension` 的）。两路对同一
       策略给出不同原因的情形只有一种：停用路说「策略离场 / 阶段离开」（那条决策行自己
       的收场），而暂停路说「本阶段不再拦」——前者更贴那条决策行的事实，而活行要的是
       「它为什么被解除」，取更贴切的。两路都不会为**同一策略**给出矛盾的事实级原因
       （暂停路的期望行永远只针对当前阶段，它没有「阶段离开」这一路）。

    `statuses` 只来自停用路：`applied` / `released` 是**停用决策行**的账，本路没有决策行
    可记（它连 `DeactivationDecision` 都不产生）——这正是它不进「该停 N」的原因。
    """
    if base.blocked or suspension.blocked:
        return GatePlan(
            blocked=base.blocked or suspension.blocked,
            note=base.note or suspension.note,
        )

    declared = {row.strategy_id for row in base.declarations}
    declarations = tuple(base.declarations) + tuple(
        row for row in suspension.declarations if row.strategy_id not in declared
    )
    return GatePlan(
        declarations=declarations,
        statuses=tuple(base.statuses),
        close_reasons={**suspension.close_reasons, **base.close_reasons},
        note=base.note or suspension.note,
    )
