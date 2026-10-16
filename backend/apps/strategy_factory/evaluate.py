"""候选漏斗的判定纯函数（spec #19 D14 的**单一测试缝**）。

`evaluate_candidate(evidence) -> Verdict` 是整条流水线上唯一做判断的地方：**零 I/O、
不碰 DB、不读时钟、不发通知、不 import Django**。输入是纯数据（当前状态 + 各层证据 +
阈值），输出是「下一步该落在哪个状态 + 原因码 + 依据数字」。所有外部效应都在调用方
（#22 的 `advance()`）注入，所以这里可以用表驱动穷举把每一条边界钉死。

为什么值得这么严：漏斗的每一层都在决定「这条策略要不要花真钱」，而判定散进编排之后
就再也测不全了——一个 `if` 藏在取数据与建会话之间，没人能把它的每个分支都跑一遍。

## 三条与 spec 对齐的口径（读代码前先读这三条）

1. **边界是不对称的，且刻意如此**。夏普门槛是 `>= sharpe_min`（「夏普 ≥1.5」），
   衰减上限是 `< oos_decay_max_pct`（「衰减 <30% 才放行」）。两个数都由 CONTEXT.md
   的可靠标准原文给出，这里照抄原文的方向，不为了「好看」把它统一成同一种比较。
   于是夏普恰 1.5 放行、衰减恰 30% 拦下。

2. **证据不足与不合格是两件事，原因码必须分得开**。「29 笔」不是「夏普不够」，
   「分折缺 K 线」不是「衰减超标」——它们都会落 `*_failed`，但原因码一个
   `INSUFFICIENT_EVIDENCE`、一个具体指标不达标。混成一个码的后果是提出人被告知
   「你的策略不行」，而实际是「数据不够，无法判断」。（D6、以及 D3 对
   `oos_failed` 的说明。）

3. **「停在本状态」不是一次转移**。`Verdict.next_status == 当前状态` 表示不动
   （观察期没满、等人工账户、终态……），此时它**不是**一条合法边，
   `can_transition` 对它返回假。调用方若要落库，只有 `next_status` 不等于当前
   状态时才该走模型层的 `transition_to`。

4. **非有限数（NaN / ±inf）一律当「证据不足」**，不是「达标」。这不是洁癖：与阈值
   比较时 NaN 全假，于是 `sharpe=NaN` 会**静默通过** `sharpe < 门槛` 这道钱闸门，
   `decay=-inf` 会静默通过 `decay >= 上限`。`config.py` 里已经为「阈值取 0 会让判据
   恒假」立过规矩，NaN 是同一类失效（判据不成立，闸门却在放行），所以在**取值**这一
   端堵：不是有限数就是没有数。

5. **证据的形状与「拿到证据」是两件事**（#34 的 `RegimeEvidence`）。本模块只定义判定
   **读什么**（纯值：str / bool / datetime / dict），一行 Django 都不 import；证据从库里
   怎么取、取不到该给什么形状，在 `effects.read_regime`。判定层拿不到适用性结论时收到的
   是 `None`（「拿不到」），不是一份合成的「中性」——把「没覆盖」写成「中性」等于让闸门
   在证据缺失处自己编一个结论，而这正是 fail-closed 要防的那件事。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Mapping

from apps.strategy_factory.config import CONFIG, FactoryConfig
from apps.strategy_factory.status import (
    StrategyCandidateStatus,
    coerce_status,
    is_terminal,
)


class ReasonCode(str, Enum):
    """判定结论的原因码（落 `evidence`，也进通知文案）。

    做成枚举而不是自由字符串：原因码是**机器可读的结论**（通知、日报、事后归因都读它），
    拼错一次就变成一条谁都读不懂的记录；而自由字符串也拦不住「同一个意思两种写法」
    （`sharpe_below` / `sharpe_too_low`），那会让统计「哪一层刷掉最多」变成手工活。
    """

    # -- 与「判定」无关的三种结论 ------------------------------------------ #
    TERMINAL = "terminal"
    AUTO_ADVANCE_DISABLED = "auto_advance_disabled"
    AWAITING_HUMAN_APPROVAL = "awaiting_human_approval"
    AWAITING_REGISTRATION_RETRY = "awaiting_registration_retry"
    AWAITING_RERUN = "awaiting_rerun"

    # -- 人工卡点（REST 端点的否决 / 取消，#23） ---------------------------- #
    #: 不是判定结论，是**人的决定**：进转移记录的 reason_code，让审计面能区分
    #: 「机器判它不合格」与「人把它叫停」（D20：推进动作可归因）。
    HUMAN_REJECTED = "human_rejected"
    HUMAN_CANCELLED = "human_cancelled"

    # -- L1 样本内 --------------------------------------------------------- #
    IN_SAMPLE_RUN_STARTED = "in_sample_run_started"
    IN_SAMPLE_PENDING = "in_sample_pending"
    IN_SAMPLE_PASSED = "in_sample_passed"
    IN_SAMPLE_SHARPE_BELOW_MIN = "in_sample_sharpe_below_min"
    IN_SAMPLE_BACKTEST_FAILED = "in_sample_backtest_failed"

    # -- L2 样本外 --------------------------------------------------------- #
    OOS_PENDING = "oos_pending"
    OOS_PASSED = "oos_passed"
    OOS_DECAY_ABOVE_MAX = "oos_decay_above_max"
    OOS_BACKTEST_FAILED = "oos_backtest_failed"

    # -- L3 paper ---------------------------------------------------------- #
    PAPER_ACCOUNT_MISSING = "paper_account_missing"
    PAPER_QUEUED = "paper_queued"
    PAPER_OBSERVATION_STARTED = "paper_observation_started"
    PAPER_OBSERVING = "paper_observing"
    PAPER_OBSERVATION_INCOMPLETE = "paper_observation_incomplete"
    PAPER_RETURN_BELOW_MIN = "paper_return_below_min"
    PAPER_RISK_HITS_ABOVE_MAX = "paper_risk_hits_above_max"
    PAPER_PASSED = "paper_passed"

    # -- 跨层 -------------------------------------------------------------- #
    #: 数据不够/样本不够，**判不了**（≠ 判为不合格）。任何一层都能给出它。
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"

    # -- 行情闸门（spec #30 D1，跨 L2/L3 两个升档点） ---------------------- #
    #: 三个码都来自同一次行情判定，但**结论的落点不同**，所以必须分成三个而不是
    #: 一个带参数的 `regime_*`：
    #:
    #: - `REGIME_UNFIT` → 停进 `regime_hold`。行情状态明确判为「不适用」，这是
    #:   唯一一个**拦人**的码。
    #: - `REGIME_NO_EVIDENCE` → 也停进 `regime_hold`，但原因是「行情切片算不出来 /
    #:   没有数据」，即**判不了**而不是「判了不行」。与 `INSUFFICIENT_EVIDENCE`
    #:   分属两端却同一条纪律：证据不足永远读成「不放行」，绝不能读成「放行」。
    #:   合成一个码的后果是通知里写着「行情不适用」，而真实情况是行情还没算出来。
    #: - `REGIME_EXEMPT` → **不拦人**：人工豁免在期，候选照常放行。它是纯留痕码
    #:   （spec #30 D5），存在的唯一理由是让审计面能回答「这条当时是怎么过去的」。
    #:   刻意**不**做成 `REGIME_UNFIT` 的反义（如 `regime_fit`）：豁免放行与「行情
    #:   本来就适用」是两件不同的事，合并之后「有多少候选是靠豁免过去的」就统计不
    #:   出来了。所以它的 `next_status` 与被豁免的那一步的正常升档目标一致。
    REGIME_UNFIT = "regime_unfit"
    REGIME_NO_EVIDENCE = "regime_no_evidence"
    REGIME_EXEMPT = "regime_exempt"

    @property
    def display(self) -> str:
        return _REASON_DISPLAY[self]


_REASON_DISPLAY = {
    ReasonCode.TERMINAL: "终态，不再自动推进",
    ReasonCode.AUTO_ADVANCE_DISABLED: "流水线总开关关闭，不做任何自动推进",
    ReasonCode.AWAITING_HUMAN_APPROVAL: "等人工卡点确认",
    ReasonCode.AWAITING_REGISTRATION_RETRY: "登记失败，等修好代码后重新登记",
    ReasonCode.AWAITING_RERUN: "该层未过，等改参数重跑或人工收尾",
    ReasonCode.IN_SAMPLE_RUN_STARTED: "已登记，开始样本内回测",
    ReasonCode.IN_SAMPLE_PENDING: "样本内回测尚无结果",
    ReasonCode.IN_SAMPLE_PASSED: "样本内通过（夏普与样本量达标）",
    ReasonCode.IN_SAMPLE_SHARPE_BELOW_MIN: "样本内夏普低于门槛",
    ReasonCode.IN_SAMPLE_BACKTEST_FAILED: "样本内回测失败",
    ReasonCode.OOS_PENDING: "样本外分折尚无结果",
    ReasonCode.OOS_PASSED: "样本外通过（衰减在上限内）",
    ReasonCode.OOS_DECAY_ABOVE_MAX: "样本外衰减超过上限",
    ReasonCode.OOS_BACKTEST_FAILED: "样本外分折失败",
    ReasonCode.PAPER_ACCOUNT_MISSING: "等指定 paper（testnet）账户",
    ReasonCode.PAPER_QUEUED: "同账户同品种已有活跃会话，排队中",
    ReasonCode.PAPER_OBSERVATION_STARTED: "开始 paper 观察",
    ReasonCode.PAPER_OBSERVING: "paper 观察期未满",
    ReasonCode.PAPER_OBSERVATION_INCOMPLETE: "paper 会话在观察期满前结束",
    ReasonCode.PAPER_RETURN_BELOW_MIN: "paper 期盈亏不达标",
    ReasonCode.PAPER_RISK_HITS_ABOVE_MAX: "paper 期风控命中超过上限",
    ReasonCode.PAPER_PASSED: "paper 观察通过，等人工卡点升实盘",
    ReasonCode.INSUFFICIENT_EVIDENCE: "证据不足，判不了（不放行）",
    ReasonCode.HUMAN_REJECTED: "人工否决",
    ReasonCode.HUMAN_CANCELLED: "人工取消",
    ReasonCode.REGIME_UNFIT: "当前行情不适用，暂缓推进",
    ReasonCode.REGIME_NO_EVIDENCE: "行情核对不了（切片缺数据），暂缓推进",
    #: 放行口径的展示名也一样要说清是「豁免放行」而不是「行情本来就适用」：
    #: 读记录的人要能看出这一条是被人工开关放过去的。
    ReasonCode.REGIME_EXEMPT: "人工豁免在期，按放行处理（留痕）",
}


@dataclass(frozen=True)
class InSampleEvidence:
    """L1 样本内那一层的证据。

    `total_trades` 的口径是**已平仓**交易（`pnl is not None`），不是 `BacktestTrade`
    行数（含 open/add 行）——D6 点名的坑：行数会把一笔分批建仓算成好几笔，
    于是「29 笔」这个边界在两种口径下指向不同的东西。
    """

    sharpe_ratio: float | None = None
    total_trades: int | None = None
    #: 回测失败的原因（非空表示这一层没跑出结果，而不是跑出来不好）。
    error: str | None = None


@dataclass(frozen=True)
class OosEvidence:
    """L2 样本外那一层的证据（walk-forward 聚合，D4 的 `metrics["walk_forward"]`）。

    `decay_pct` 是**百分点**口径（`30.0` 表示衰减 30%），与 `oos_decay_max_pct` 同单位。
    """

    decay_pct: float | None = None
    fold_count: int | None = None
    #: 分折所需的数据够不够（缺 K 线 / 库里没有那段区间 → False）。
    #: 单独一个字段而不是靠 `decay_pct is None` 推断：**「没数据」与「算出来是空」
    #: 是两件事**，而 D3/测试决策都要求数据缺口判「证据不足」而不是「衰减超标」。
    data_sufficient: bool = True
    error: str | None = None


@dataclass(frozen=True)
class PaperEvidence:
    """L3 paper 那一层的证据（观察期口径 + 统计 + 前置条件）。

    - `wall_clock_days`：paper 会话 `started_at → now` 的墙钟时长（天）。
    - `running_days`：同一区间里**只计 `running`** 的时长（天）。`paused` 是「暂停止损/
      止盈」，不是「在观察」（D7/D8），所以它不计入——两个数都要 ≥ 观察期才算跑满。
    - `closed_trades` / `return_pct` / `risk_hits`：来自 `compute_session_stats`（#24
      复用既有纯函数）与 `RiskEvent.live_session` 的计数。
    - `account_ready`：候选是否已指定 `paper_account`（D10：整条流水线唯一的人工输入）。
    - `queued_behind_active_session`：同 `(账户, 品种)` 上是否已有活跃会话（D9 串行）。
    - `session_ended`：paper 会话是否已 `stopped`/`error`。**没有它就无法区分
      「还在观察」与「观察期没跑满就结束了」**——前者该停在原状态，后者该判未过。
    """

    wall_clock_days: float | None = None
    running_days: float | None = None
    closed_trades: int | None = None
    return_pct: float | None = None
    risk_hits: int | None = None
    account_ready: bool = False
    queued_behind_active_session: bool = False
    session_ended: bool = False


@dataclass(frozen=True)
class RegimePoolCellEvidence:
    """池化表里**一格**（策略 × 阶段）的结论摘录（#34）。

    名字刻意叫「格子的结论摘录」而不是「格子」：它不是 `apps.regime.models.RegimePoolCell`
    （那是一个 Django 模型，本模块一行 Django 都不 import），只是它上面那五个与判定有关
    的取值的**原样副本**。判定要的是「这一格说了什么」，不是「这一行在哪张表里」。

    四态一档的取值（`fit` / `unfit` / `neutral` / `unknown` / `blanket`）是**落库契约**，
    定义在 `apps.regime.slice`。这里不重新枚举它们：抄一份词表就等于给「新加一个状态却
    忘了改其中一处」留了门。所以字段类型是 `str`，取值由采集侧（`effects.read_regime`）
    原样递过来。

    `needs_review` 与 `state` 是**两件事**（CONTEXT.md 与模型 docstring 都钉过）：冲突格子
    照样有结论，只是那格不产生停用建议。所以它是一个独立的布尔，不是第六种 `state`——
    并进 `state` 等于宣称「这格没有结论」，而它明明有。
    """

    #: 这一格的结论（`apps.regime.slice.STATE_*` 的取值）。
    state: str
    #: 为什么落到这个状态（`slice.REASON_*` 的取值；空串表示没有原因要说明）。
    reason: str = ""
    #: 结论来源：`strategy`（这条策略自己的切片）/ `archetype`（原型兜底）。
    source: str = ""
    #: 冲突格子（不产生停用建议，但结论照常成立）。
    needs_review: bool = False


@dataclass(frozen=True)
class RegimeEvidence:
    """**跨层**行情证据：当前生效阶段 + 那一代池化表里这一格的结论 + 在期豁免（#34）。

    与另外三层证据的区别是它**不挂在某一层的状态下**：行情是漏斗的横切条件——样本外
    通过要问一次、paper 期满也要问一次，而它每次都来自同一处（当代池化表 + 当前生效
    判定 + 在期豁免）。所以它不进 `CandidateEvidence` 的层字段（那一处是「这一层的判定
    输入」），而由判定层按需向调用方索取。

    两个取值**必须同代**，这是本结构存在的第一理由：

    * `regime` 来自 `deactivation_run.current_regime_state`——与停机链**同源**（内部走
      `judgement.current_judgement`）。闸门**绝不**自己读行情判定表（D3）：另开一个读口
      就会与停机链对「现在是什么阶段」给出两个答案，而那种分叉在两边各自看都是自洽的。
    * `cell` 来自 `pool_rebuild.current_cells`，且必须与 `current_generation()` 在同一次
      读里配对。分两次读会落成「依据写的是第 N 代、引用的却是第 N+1 代那一格」——而
      `current_cells` 自己的 docstring 说的就是这件事。

    冷启动两段各自有各自的形状，且**都必须是「说不清」而不是「没风险」**（本仓库的
    fail-closed 纪律）：

    - `regime is None`：当前没有生效的行情判定（从没判出来过 / 判定链还没开始跑）。
      这不是「箱体震荡」。
    - `cell is None`：**当前阶段在这一代池化表里没有这一格的结论**——多品种 / 窗口未
      覆盖。按 spec 用户故事 10，那是「中性」，**不是**「放行」，也不是「没有证据」。
      做成 `None` 而不是合成一个 `state="neutral"` 的假格子：合成之后「确实覆盖到了、
      结论就是中性」与「压根没这一格」在结构上再也分不开，而这两件事在依据栏里必须能
      分开读。

    `exemptions` 只列**当前阶段**上的在期人工豁免（键是 `(策略 id, 阶段)`）。它单独一个
    字段而不是并进 `cell`：豁免是人的动作，池化结论是机制算出来的，混在一起会让
    「这一格算出来是不适用」与「有人把它豁免了」变成同一句话。
    """

    #: 当前**生效**的行情阶段（`apps.regime.quant.BaseRegime` 的取值）；
    #: `None` = 冷启动，当前没有生效判定。
    regime: str | None = None
    #: 当前生效判定的生效时刻。与 `regime` 一起从停机链同一个读口出来，不另查。
    effective_at: datetime | None = None
    #: 当代池化表里这一格（本策略 × `regime`）的结论摘录；
    #: `None` = 当代没有这一格（未覆盖），**不是**「结论是中性」。
    cell: RegimePoolCellEvidence | None = None
    #: 在期人工豁免：`{(策略 id, 阶段): 豁免 id}`，形状照 `in_force_exemptions` 的返回。
    exemptions: Mapping[tuple[Any, str], Any] = field(default_factory=dict)

    @property
    def cold(self) -> bool:
        """当前没有生效的行情判定（冷启动）。

        单独一个属性而不是让调用方写 `regime is None`：这两件事在语义上是同一句话，
        而「说不清现在什么阶段」与「阶段是箱体震荡」必须能一眼分开。
        """
        return self.regime is None

    @property
    def covered(self) -> bool:
        """当代池化表里有没有这一格的结论。

        与 `cold` 分得开是刻意的：**阶段说得清、格子没有**（未覆盖）既不是冷启动、
        也不是中性结论，是第三种状态，spec 用户故事 10 要的正是把它认出来。
        """
        return self.cell is not None


@dataclass(frozen=True)
class CandidateEvidence:
    """一次判定的**全部**输入（纯数据）。

    `thresholds` 默认取进程级单例（`config.CONFIG`），测试用
    `dataclasses.replace(CONFIG, sharpe_min=...)` 派生——既不改全局，也保证
    「判定读的阈值」与「线上生效的阈值」是同一个对象类型。
    """

    status: StrategyCandidateStatus | str
    in_sample: InSampleEvidence | None = None
    oos: OosEvidence | None = None
    paper: PaperEvidence | None = None
    thresholds: FactoryConfig = CONFIG


@dataclass(frozen=True)
class Verdict:
    """判定结论：下一步落在哪、为什么、依据是什么（D14）。

    `next_status` 等于当前状态时表示**不动**（见模块 docstring 第 3 条）。
    `detail` 只放数字与短标识（要进 `evidence` JSONField），不放句子。
    """

    next_status: StrategyCandidateStatus
    reason_code: ReasonCode
    detail: dict[str, Any] = field(default_factory=dict)

    def holds(self, current: StrategyCandidateStatus | str) -> bool:
        """结论是不是「停在当前状态」。"""
        return self.next_status is coerce_status(current)


def _verdict(next_status, reason_code, **detail) -> Verdict:
    return Verdict(
        next_status=coerce_status(next_status), reason_code=reason_code, detail=detail
    )


def _is_usable_number(value: Any) -> bool:
    """这个值是不是一个能拿去比大小的有限实数（见模块 docstring 第 4 条）。

    `None`、NaN、±inf 都不是结论：前两个是「没算出来」，NaN 还会让任何比较静默为假
    （闸门于是放行）。`bool` 显式排除——它是 `int` 的子类，而 `True >= 1.5` 这种比较
    不该被当成一个指标。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value)


def is_usable_metric_number(value: Any) -> bool:
    """公开口径：什么算「一个能拿去算钱闸门的指标数」（见模块 docstring 第 4 条）。

    存在的理由：walk-forward 的聚合层（`walk_forward.py`）也要判「这个夏普能不能拿去
    算衰减」，而它**绝不能自己再写一份判据**——两份判据迟早分家，然后一边认 NaN、
    一边不认，NaN 就从新那一侧漏过去。实现只有一处（`_is_usable_number`），这里只是
    把它提升成跨模块的契约。
    """
    return _is_usable_number(value)


def _is_usable_count(value: Any) -> bool:
    """计数类证据（成交笔数、风控命中次数）可用吗。

    必须是**非负整数**：`None` 是没算出来，`3.7` 是口径错了，负数是不可能的计数。
    计数不参与浮点比较，所以这里只防「不是数」与「数不可能是那个数」。
    """
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def evaluate_candidate(evidence: CandidateEvidence) -> Verdict:
    """按候选当前状态分派到对应那一层的判定。"""
    status = coerce_status(evidence.status)
    thresholds = evidence.thresholds

    # 终态先判：`live`/`rejected`/`cancelled` 不该因为总开关关着就报
    # 「总开关关闭」——那会让人以为它们还能被推进。
    if is_terminal(status):
        return _verdict(status, ReasonCode.TERMINAL, status=status.value)

    if not thresholds.auto_advance_enabled:
        # 总开关关掉的是**自动推进**，不是人工动作：人工否决/取消/升实盘不经这里。
        return _verdict(status, ReasonCode.AUTO_ADVANCE_DISABLED, status=status.value)

    if status is StrategyCandidateStatus.REGISTERED:
        # 登记完成后的下一步是自动的（起样本内回测），不需要等任何证据。
        return _verdict(
            StrategyCandidateStatus.IN_SAMPLE_RUNNING,
            ReasonCode.IN_SAMPLE_RUN_STARTED,
        )
    if status is StrategyCandidateStatus.REGISTRATION_FAILED:
        # 回 `registered` 的前提是代码被修好并重新登记——那是一次外部动作（#25），
        # 不是判定能凭现有证据下的结论，所以停住。
        return _verdict(status, ReasonCode.AWAITING_REGISTRATION_RETRY)
    if status is StrategyCandidateStatus.IN_SAMPLE_RUNNING:
        return _evaluate_in_sample(evidence.in_sample, thresholds)
    if status is StrategyCandidateStatus.OOS_RUNNING:
        return _evaluate_oos(evidence.oos, thresholds)
    if status is StrategyCandidateStatus.PAPER_PENDING:
        return _evaluate_paper_pending(evidence.paper)
    if status is StrategyCandidateStatus.PAPER_RUNNING:
        return _evaluate_paper_running(evidence.paper, thresholds)

    # in_sample_failed / oos_failed / paper_failed / awaiting_live_approval
    if status is StrategyCandidateStatus.AWAITING_LIVE_APPROVAL:
        return _verdict(status, ReasonCode.AWAITING_HUMAN_APPROVAL)
    return _verdict(status, ReasonCode.AWAITING_RERUN, status=status.value)


def _evaluate_in_sample(ev: InSampleEvidence | None, cfg: FactoryConfig) -> Verdict:
    """L1：夏普 ≥ 门槛 **且** 样本量 ≥ 下限，两条都过才去分折。

    检查顺序（回测失败 → 样本量 → 夏普 → 通过）是有意的：前面的都是「连判都判不了」，
    后面的才是「判了、不行」。全部不合格时给出的原因码应该是**最根本**的那一个，
    否则通知会告诉人「夏普不够」，而真实情况是那 4 笔里算出来的夏普本来就不算数。
    """
    current = StrategyCandidateStatus.IN_SAMPLE_RUNNING
    if ev is None:
        return _verdict(current, ReasonCode.IN_SAMPLE_PENDING)

    if ev.error:
        return _verdict(
            StrategyCandidateStatus.IN_SAMPLE_FAILED,
            ReasonCode.IN_SAMPLE_BACKTEST_FAILED,
            error=ev.error,
        )

    if not _is_usable_count(ev.total_trades) or ev.total_trades < cfg.min_trades:
        # 样本不足（或这个数根本不是个可用计数）→ 「证据不足」，不是「夏普不够」（D6）。
        return _verdict(
            StrategyCandidateStatus.IN_SAMPLE_FAILED,
            ReasonCode.INSUFFICIENT_EVIDENCE,
            total_trades=ev.total_trades,
            min_trades=cfg.min_trades,
        )

    if not _is_usable_number(ev.sharpe_ratio):
        # None / NaN / ±inf 都在这里：NaN 与门槛比较恒假，放它过去就是闸门静默消失。
        return _verdict(
            StrategyCandidateStatus.IN_SAMPLE_FAILED,
            ReasonCode.INSUFFICIENT_EVIDENCE,
            total_trades=ev.total_trades,
            sharpe_ratio=ev.sharpe_ratio,
        )

    if ev.sharpe_ratio < cfg.sharpe_min:
        return _verdict(
            StrategyCandidateStatus.IN_SAMPLE_FAILED,
            ReasonCode.IN_SAMPLE_SHARPE_BELOW_MIN,
            sharpe_ratio=ev.sharpe_ratio,
            sharpe_min=cfg.sharpe_min,
        )

    return _verdict(
        StrategyCandidateStatus.OOS_RUNNING,
        ReasonCode.IN_SAMPLE_PASSED,
        sharpe_ratio=ev.sharpe_ratio,
        total_trades=ev.total_trades,
    )


def _evaluate_oos(ev: OosEvidence | None, cfg: FactoryConfig) -> Verdict:
    """L2：衰减 < 上限才放行；数据不足判「证据不足」，**不放行**（D6 的边界）。

    衰减是 `>=` 上限就拦：原文是「衰减 <30% 才放行」，恰 30% 不在「小于」里。
    """
    current = StrategyCandidateStatus.OOS_RUNNING
    if ev is None:
        return _verdict(current, ReasonCode.OOS_PENDING)

    if ev.error:
        return _verdict(
            StrategyCandidateStatus.OOS_FAILED,
            ReasonCode.OOS_BACKTEST_FAILED,
            error=ev.error,
        )

    if not ev.data_sufficient:
        # 「分折缺 K 线 / 库无数据」走这一支，原因码必须是证据不足而不是衰减超标。
        return _verdict(
            StrategyCandidateStatus.OOS_FAILED,
            ReasonCode.INSUFFICIENT_EVIDENCE,
            data_sufficient=False,
            fold_count=ev.fold_count,
        )

    if not _is_usable_count(ev.fold_count) or ev.fold_count < 1:
        return _verdict(
            StrategyCandidateStatus.OOS_FAILED,
            ReasonCode.INSUFFICIENT_EVIDENCE,
            fold_count=ev.fold_count,
        )

    if not _is_usable_number(ev.decay_pct):
        # None / NaN / -inf 都在这里：`-inf >= 上限` 为假会让闸门静默放行。
        return _verdict(
            StrategyCandidateStatus.OOS_FAILED,
            ReasonCode.INSUFFICIENT_EVIDENCE,
            fold_count=ev.fold_count,
            decay_pct=ev.decay_pct,
        )

    if ev.decay_pct >= cfg.oos_decay_max_pct:
        return _verdict(
            StrategyCandidateStatus.OOS_FAILED,
            ReasonCode.OOS_DECAY_ABOVE_MAX,
            decay_pct=ev.decay_pct,
            oos_decay_max_pct=cfg.oos_decay_max_pct,
            fold_count=ev.fold_count,
        )

    return _verdict(
        StrategyCandidateStatus.PAPER_PENDING,
        ReasonCode.OOS_PASSED,
        decay_pct=ev.decay_pct,
        fold_count=ev.fold_count,
    )


def _evaluate_paper_pending(
    ev: PaperEvidence | None,
) -> Verdict:
    """L3 入口：等一次人工输入（绑哪个 testnet 账户），再排队（D9/D10）。

    两个「停住」的原因码必须分开：`PAPER_ACCOUNT_MISSING` 要人做点什么，
    `PAPER_QUEUED` 只要等——混成一个码会让「卡住也要可见」的通知（D20）说不清
    到底该谁动。
    """
    current = StrategyCandidateStatus.PAPER_PENDING
    paper = ev or PaperEvidence()

    if not paper.account_ready:
        return _verdict(current, ReasonCode.PAPER_ACCOUNT_MISSING)

    if paper.queued_behind_active_session:
        return _verdict(current, ReasonCode.PAPER_QUEUED)

    return _verdict(
        StrategyCandidateStatus.PAPER_RUNNING,
        ReasonCode.PAPER_OBSERVATION_STARTED,
    )


def _session_ended_verdict(
    reason_code: ReasonCode, session_ended: bool, **detail
) -> Verdict:
    """「判不了」时的落点：会话还在 → 继续观察；已经结束 → 判未过。

    这条兜底是为了不让候选**静默地卡在观察中**：一个已经 stopped 的 paper 会话不会
    再产生任何成交，停在 `paper_running` 等它自己变好，等于永远不判定。
    """
    return _verdict(
        StrategyCandidateStatus.PAPER_FAILED
        if session_ended
        else StrategyCandidateStatus.PAPER_RUNNING,
        reason_code,
        **detail,
    )


def _evaluate_paper_running(ev: PaperEvidence | None, cfg: FactoryConfig) -> Verdict:
    """L3：观察期满（墙钟与 running 两个数都要够）后按盈亏与风控命中判定（D7/D8）。

    观察期用**两个**时长同时约束：`wall_clock_days` 保证「确实过了两周」，
    `running_days` 保证「这两周里它真的在跑」（D8：`paused` 的天数不计入）。
    只看墙钟会让一条暂停两周的会话算跑满；只看 running 会让「跑了 14 天但一直
    在暂停与恢复之间抖」看起来也算数。
    """
    current = StrategyCandidateStatus.PAPER_RUNNING
    if ev is None:
        return _verdict(current, ReasonCode.PAPER_OBSERVING)

    required = float(cfg.paper_observation_days)
    if not (
        _is_usable_number(ev.wall_clock_days) and _is_usable_number(ev.running_days)
    ):
        return _session_ended_verdict(
            ReasonCode.INSUFFICIENT_EVIDENCE,
            ev.session_ended,
            wall_clock_days=ev.wall_clock_days,
            running_days=ev.running_days,
        )

    elapsed = ev.wall_clock_days >= required and ev.running_days >= required
    if not elapsed:
        detail = dict(
            wall_clock_days=ev.wall_clock_days,
            running_days=ev.running_days,
            paper_observation_days=cfg.paper_observation_days,
        )
        if ev.session_ended:
            return _verdict(
                StrategyCandidateStatus.PAPER_FAILED,
                ReasonCode.PAPER_OBSERVATION_INCOMPLETE,
                **detail,
            )
        return _verdict(current, ReasonCode.PAPER_OBSERVING, **detail)

    # 观察期满：先要判得了（样本量与三个统计齐不齐），再判好不好。
    # 「齐」= 有限实数 / 非负整数（NaN 与负数都不是结论，见模块 docstring 第 4 条）。
    unusable = [
        name
        for name, usable in (
            ("closed_trades", _is_usable_count(ev.closed_trades)),
            ("return_pct", _is_usable_number(ev.return_pct)),
            ("risk_hits", _is_usable_count(ev.risk_hits)),
        )
        if not usable
    ]
    if unusable:
        return _session_ended_verdict(
            ReasonCode.INSUFFICIENT_EVIDENCE,
            ev.session_ended,
            unusable=unusable,
        )

    if ev.closed_trades < cfg.min_trades:
        # 沿用既有 ≥30 笔门槛：观察期满但成交太少，胜率读不成结论（D6 的同一把尺）。
        return _session_ended_verdict(
            ReasonCode.INSUFFICIENT_EVIDENCE,
            ev.session_ended,
            closed_trades=ev.closed_trades,
            min_trades=cfg.min_trades,
        )

    if ev.return_pct < cfg.paper_min_return_pct:
        return _verdict(
            StrategyCandidateStatus.PAPER_FAILED,
            ReasonCode.PAPER_RETURN_BELOW_MIN,
            return_pct=ev.return_pct,
            paper_min_return_pct=cfg.paper_min_return_pct,
        )

    if ev.risk_hits > cfg.paper_max_risk_hits:
        # 恰等于上限算通过（「超标」= 超过），所以这里是 `>` 而不是 `>=`。
        return _verdict(
            StrategyCandidateStatus.PAPER_FAILED,
            ReasonCode.PAPER_RISK_HITS_ABOVE_MAX,
            risk_hits=ev.risk_hits,
            paper_max_risk_hits=cfg.paper_max_risk_hits,
        )

    return _verdict(
        StrategyCandidateStatus.AWAITING_LIVE_APPROVAL,
        ReasonCode.PAPER_PASSED,
        closed_trades=ev.closed_trades,
        return_pct=ev.return_pct,
        risk_hits=ev.risk_hits,
    )
