"""机制整体那一档（出 Shadow / 退回 Shadow）的人工确认与**准入体检页**。

CONTEXT.md 第 160 条：**出 Shadow 切到执行态必须人工确认，不自动切换**——到期只代表准入
条件可被校验，由系统把校验结果摆出来、人点头才切。第 161 条把反方向也钉住了：自熔断退回
Shadow 之后再回执行态同样要人工确认，且必须显式记录「这次是自熔断后的恢复」并把根因摆出来。
本模块就是那一步：算出一页数给人看，并在人点头时落下 `RegimeMechanismSwitch` 的那一行。

## 与另外两个开关同形，但这一页答的是**回溯**的问题

`breaker_switch` / `gate_switch` 的确认页回答「打开之后会拦住什么」（展望）；这一页回答
「Shadow 期已经跑出了什么」（回溯）：跑了多久、跨过几个事件窗口、触发频率多少。写方与留痕
的形状一致（同一个 `page()` + `flip_*()`），数的来源完全不同，所以它是一个独立模块，而不是
第三个 `flip_*` 挤在别处。

## 这一档今天**不是**执行开关——页面必须说这句话

拦人的是两个下游开关：`halt.HALT_TRIGGER_SWITCH` 把事件熔断映射到 `EVENT_BREAKER`、
把策略停用映射到 `REGIME_GATE`，映射到 `None` 的保命档恒生效；而
`MechanismKind.MECHANISM` 在全仓只有读者（日报第④段、`query_halt`）。所以「出 Shadow」
今天的实际效果是**留一条记录**：它记的是「第①段的判定与切片被验证过了」这件事，也是自熔断
退回时的落点（第 161 条）。不说这句话，这一页就成了最伤信任的那类输出——人以为按一下机制
就上线了，而它什么都没打开。

## 三条成功标准各自的今天（第 163 条）

- **① 阶段判定与人工标注的一致率**：可以算，但**要有人先把真值录进去**。真值是人在算法
  输出可见之前独立标注的已知历史区间（`RegimeTruthInterval`，入口 `/regime label`）；
  一段都没录时这一页如实写「无法判定」并**按未达标显示**：留白会被人读成「这条大概没
  问题」。比对与那句话本身都在 `truth.py` / `truth_run.py`（见下「不自己算一致率」）。
- **② 触发频率 < 10% 自然日**：可以算。分母是起算日到今天的自然日数，分子是三个触发源的
  并集（`_days_with_trigger`）。
- **③ 每次判定出的熔断有完整可读解释**：不可机械判定，这一页只把它列成人工核对的条目。

## 为什么这一页要印「调度表」那一段

这一页是**唯一**按需渲染、不经过 beat 的机制读面：判定、日报、对账全都由 beat 驱动，
beat 一哑，那些出口一起哑。而第 180 条要求的「任务跑了没」在今天**没有出口**——
`check_task_health` 扫的是建过 `TaskTracker` 的任务，beat 任务结构上不在其中（详见
`beat_health.py` 的模块 docstring）。所以那一行印在这里：它读 `PeriodicTask` 表与
`beat_schedule`，答「调度器此刻会发什么、上次发出去是什么时候」。

**它答不出「beat 自己死了」**：那一行也印不出来——beat 死了就没有页面之外的出口了，
而这正是这一页存在的理由（人问的时候它还在）。它同样答不出「任务失败了」：`last_run_at`
由 beat 在派发时盖，不由 worker 在成功时盖，所以这一行把「发出去了」与「成了」分开说。

**它的读数还是 3 分钟粒度的**：`last_run_at` 由调度器定期落库（`sync_every`，默认 180
秒），不是每次派发都写。所以那一行自己印着「粒度下限」那句话——少了它，「距今 3 分」
会被读成「3 分钟没跑」，而 30 秒一轮的任务正常情况下就是这个数（实测踩过：判据里不含
这一项时，三条 30 秒一轮的任务长期误报「已超期」，而 beat 日志显示它们一切正常）。

## 到期怎么算（第 159 条）

到期 =（≥`min_natural_days` 个自然日 **且** ≥`min_event_windows` 次高影响事件窗口）
**或**（≥`expiry_cap_days` 个自然日兜底）。**兜底那一条必须显式记录「事件窗口数不足」**：
否则一个没人维护的事件库能让 Shadow 无限期挂着，而挂着的样子与正常跑着完全一样。那句记录
落在切换流水的 `reason` 里——由同一份快照渲染，所以它不靠人记得写。

## 起算日取哪一天（第 159 条「Shadow 从资讯通道可用之后才开始」）

起算日 = **第一条成功的资讯判定**（`news_ref.status ∈ {ok, quiet}`）的运行日，由
`news_verdict.first_success_run_day` 回答（与采集窗口起点共用同一批判据）。取「第一条有
结论的 Shadow 记录」会漏掉「判定跑起来了、资讯还没接上」的那几天，而那正是条文特意要排除
的一段：先跑一轮纯量化的 Shadow，交出的是另一个函数的成绩单。

## 触发频率的分子为什么是三个源的并集（第 163 条）

条文写死了分子是「任一触发源当日被判定为应当生效的自然日——适用性停用、高波动档、事件熔断
都算」，并给了理由：只算适用性停用，会让一个「资讯抬升到高波动、全场天天停开新仓、却从不
产生适用性停用」的机制干干净净地通过准入。三类的天数**分行报**（超标之后人第一个要问的
就是「是哪一类把它顶上去的」），并集才是那个与 10% 比的数。

两类来源的日标签各按自己的口径取：Shadow 行按它的 `run_day`（业务时区的自然日），事件窗口
按窗口覆盖到的业务时区的日历日。两者都是自然日，但日界一个是 08:00、一个是 00:00，**跨过
08:00 的那几个小时里同一天会贴到相邻的两个标签上**——这一页是给人读的体检，不是审计流水；
要精确到那一层得读事件变更流水，本模块不做。

事件窗口那一类读的是**日历此刻的样子**（`halt_sync.high_impact_events` + `triggers_halt`，
与熔断器同一处实现）。所以一条真的开过窗口、事后又被取消的事件，会让那几天从计数里掉出去
（第 154 条保留已发生的动作，但日历本身变了）。同上，这是体检不是审计。

## 两个方向都不拒绝

未到期、频率超标、一致率不达标（或算不出来）——**这一页都不阻止人切**。设计里没有任何
一条说「不达标就不许上线」，而「不确定时往保守倒」在这里的具体形状是把没达标的项**摆在
人脸前**（页面正文写一遍、切完之后再写一遍），不是替人按着。硬拒绝还会教人去找绕过它的路。

## 这一版不做的事

* **不自动切换。** 两个方向都只有人工入口（`/regime mech …`），自熔断的触发器按裁定不实现。
* **不把自熔断的频率条款当门槛**：它要连续 3 个月才评估得出，而 Shadow 期的量级是几十个
  自然日——切换那一刻它必然无结论，所以页面**明写它不构成准入门槛**，而不是算成一个空栏。
* **不碰另外两个开关**：`kind` 在这里写死（与 `breaker_switch` / `gate_switch` 同一条）。
* **不发即时消息。** 到期是「可以谈了」而不是资金动作；日报第④段在到期之后每天说一句。
* **不自己算一致率。** 第①条标准的比对全在 `truth.py` / `truth_run.py`（人工标注的已知
  区间 × 历史量化标签），本模块只把 `truth.describe` 那段话摆进页面——「同一件事两个
  说法」在这里代价格外高：一致率是出 Shadow 的依据。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from django.utils import timezone

from apps.common.time_utils import to_business
from apps.regime import (
    beat_health_run,
    config,
    events,
    halt_sync,
    news_verdict,
    truth,
    truth_run,
)
from apps.regime.models import (
    ActorKind,
    BaseRegime,
    MechanismKind,
    MechanismMode,
    RegimeMechanismSwitch,
    ShadowDailyRecord,
    business_midnight,
)

logger = logging.getLogger(__name__)

#: 本模块唯一管得着的那个开关。写成常量而不是参数：把它做成参数就是在邀请下一个调用点
#: 顺手翻一个还没接线的开关（与 `breaker_switch.KIND` 同一条理由）。
KIND = MechanismKind.MECHANISM

#: 判定源。与 `judgement.SYMBOL` 同一个取值（那边也是从 `config.CANDLES` 读的），这里直接
#: 读配置而不是 import 判定模块：import 它会把整条判定链路（含资讯通道的网络依赖）拖进来，
#: 而本模块只查两张表。
SYMBOL = config.CANDLES.symbol


# --------------------------------------------------------------------------- #
# 算数。纯读，全部无偿副作用
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Achieved:
    """Shadow 期已经跑出来的四件事（体检页左侧：事实）。

    `started_on is None` 与 `ran_days == 0` 是两件事，**不许合成一个零**：「从没跑过」与
    「跑了不到一天」在页面上读起来必须是两句不同的话，否则第一次上线那天看到的就是一个
    看起来正常的 0。
    """

    #: 起算日 = 第一条成功资讯判定的运行日；还没有过就是 `None`。
    started_on: date | None
    #: 起算日到今天（业务时区）的自然日数，**含两端**。到期与频率的**分母**都用它。
    ran_days: int
    #: 期内**开启**（窗口起点落在期内）的高影响事件窗口数。
    windows: int
    #: 期内**真的出了结论**的自然日数（`base_regime` 非空的行）。见 `_start_line`：
    #: 它可能与 `ran_days` 不等，而那个差额会**把触发频率算低**（判定没跑的那天既不
    #: 触发、也不出错），所以它必须能被看见。
    recorded_days: int


# --- 到期与两个门槛：**判据只依赖 `Achieved`**，所以提到模块级 ---------------------- #
#
# 提出来是因为 `expiry_notice`（日报第④段）只需要「到期了没 / 窗口够不够」两个布尔，过去
# 它为此造了一个**带假零值的 `Confirmation`**（频率那三个数填 0）——为了让类型对上而写下
# 的一句假话。判据搬到这里之后那个构造就没了，将来给 `Confirmation` 加字段也不必再往那个
# 假对象里塞一个假值。`Confirmation` 的同名 property 委托给这几个函数，两处仍是同一个答案。


def _days_met(achieved: Achieved) -> bool:
    return achieved.ran_days >= config.SHADOW.min_natural_days


def _windows_met(achieved: Achieved) -> bool:
    return achieved.windows >= config.SHADOW.min_event_windows


def _expired(achieved: Achieved) -> bool:
    """到期 = 两条都满足，**或**兜底的日子到了。到期只说「可以谈了」，不说「达标了」。"""
    return (_days_met(achieved) and _windows_met(achieved)) or (
        achieved.ran_days >= config.SHADOW.expiry_cap_days
    )


def _windows_short(achieved: Achieved) -> bool:
    """兜底到期：日子够了、窗口没够。**切换流水里必须带上这一句**（第 159 条）。"""
    return _expired(achieved) and not _windows_met(achieved)


def _progress(*, now: datetime) -> Achieved:
    """起算日与它的三个派生数。**只查三张表**，给日报那条路复用（不跑频率那两个集合）。

    日报那条路每天调一次，所以这里的查询都刻意是「一次 count / 一次扫事件表」，不是
    `_days_with_trigger` 那套（那个要把窗口逐日展开）。
    """
    started_on = news_verdict.first_success_run_day(SYMBOL)
    if started_on is None:
        return Achieved(started_on=None, ran_days=0, windows=0, recorded_days=0)

    today = to_business(now).date()
    # 起算日可能落在未来（补录了一条更早的判定、或时钟回拨）：那不是一个可以相减的区间，
    # 如实按 1 天（就是起算那一天本身）而不是负数的天数。
    ran_days = max((today - started_on).days, 0) + 1

    recorded_days = (
        ShadowDailyRecord.objects.filter(
            symbol=SYMBOL, run_day__gte=started_on, run_day__lte=today
        )
        .exclude(base_regime="")
        .count()
    )

    windows = 0
    for event in halt_sync.high_impact_events():
        if not event.triggers_halt:
            continue
        opened = to_business(event.halt_at).date()
        if started_on <= opened <= today:
            windows += 1
    return Achieved(
        started_on=started_on,
        ran_days=ran_days,
        windows=windows,
        recorded_days=recorded_days,
    )


def _window_days(*, start: date, today: date) -> set[date]:
    """期内被高影响事件窗口盖到的自然日（业务时区）。

    **半开区间**：窗口是 `[halt_at, resume_at)`，所以终点那一秒不属于窗口。结束时刻正好
    压在 00:00 上的窗口（比如 23:00 开、次日 00:00 收）因此不会把第二天也算进来——用
    闭区间的话那个多出来的日子看起来完全正常，而它会让频率高一点点。
    """
    span_start = business_midnight(start)
    span_end = business_midnight(today + timedelta(days=1))
    days: set[date] = set()
    for event in halt_sync.high_impact_events():
        if not event.triggers_halt:
            continue
        begin = max(event.halt_at, span_start)
        end = min(event.resume_at, span_end)
        if begin >= end:
            continue
        day = to_business(begin).date()
        last = to_business(end - timedelta(seconds=1)).date()
        while day <= last:
            days.add(day)
            day += timedelta(days=1)
    return days


def _days_with_trigger(
    *, start: date, today: date, now: datetime
) -> tuple[int, int, int, int]:
    """三个触发源各自贡献的自然日数与**去重后的合计**（第 163 条的分子）。

    三类各按自己的日标签：适用性停用与高波动档读 `ShadowDailyRecord`（`run_day`），事件
    窗口读窗口覆盖到的日历日（`_window_days`）。见模块 docstring 里那一段口径说明。
    """
    rows = ShadowDailyRecord.objects.filter(
        symbol=SYMBOL, run_day__gte=start, run_day__lte=today
    ).values_list("run_day", "effective_regime", "suggested_count")
    deactivation = {day for day, _, count in rows if count > 0}
    high_vol = {day for day, regime, _ in rows if regime == BaseRegime.HIGH_VOL.value}
    windows = _window_days(start=start, today=today)
    union = deactivation | high_vol | windows
    return len(union), len(deactivation), len(high_vol), len(windows)


@dataclass(frozen=True)
class Confirmation:
    """体检页的那几个数。**纯值对象**，渲染在外面，测试可以直接断言数字。"""

    now: datetime
    achieved: Achieved
    #: 三个触发源的合计（并集）与各自的天数。
    trigger_days: int
    deactivation_days: int
    high_vol_days: int
    event_days: int
    #: 最近一次**退回 Shadow** 的那条流水（人工或自熔断），从没退回过是 `None`。
    last_back: RegimeMechanismSwitch | None
    #: 第①条标准（一致率）的比对结果。**没有默认值**：给它一个 `Agreement()` 的默认值，
    #: 就是让「忘了算」在页面上长得与「一段真值都没录」一模一样，而这两件事一个要查代码
    #: 一个要录真值。构造这个对象的地方必须显式回答「一致率从哪来」。
    agreement: truth.Agreement

    # -- 门槛，全部来自统一配置面（`config.SHADOW`，第 164 条要求它们在那里） -------- #

    @property
    def min_natural_days(self) -> int:
        return config.SHADOW.min_natural_days

    @property
    def min_event_windows(self) -> int:
        return config.SHADOW.min_event_windows

    @property
    def expiry_cap_days(self) -> int:
        return config.SHADOW.expiry_cap_days

    # ①一致率的那个阈值**不在这里**：它只被 `truth.describe` 用到，而那段话是页面与
    # `/regime label` 共用的（`truth.Agreement.min_rate` 读同一处配置）。在这儿再放一个
    # 同名的 property，会让人以为页面自己算那个百分比。

    @property
    def max_trigger_rate(self) -> float:
        return config.SHADOW.max_trigger_rate

    # -- 到期（第 159 条）。判据在模块级（见那一段注释），这里只是它的名字 ------------- #

    @property
    def days_met(self) -> bool:
        return _days_met(self.achieved)

    @property
    def windows_met(self) -> bool:
        return _windows_met(self.achieved)

    @property
    def expired(self) -> bool:
        return _expired(self.achieved)

    @property
    def windows_short(self) -> bool:
        return _windows_short(self.achieved)

    # -- 成功标准②：触发频率（第 163 条） ---------------------------------------- #

    @property
    def rate(self) -> float | None:
        """触发频率（自然日占比）。还没起算时是 `None`——**不是 0**。"""
        if not self.achieved.started_on or self.achieved.ran_days <= 0:
            return None
        return self.trigger_days / self.achieved.ran_days

    @property
    def rate_met(self) -> bool:
        """`< max_trigger_rate`。算不出来时按**未达标**：一个算不出来的数不该被读成通过。"""
        rate = self.rate
        return rate is not None and rate < self.max_trigger_rate


def confirmation(*, now: datetime | None = None) -> Confirmation:
    """把体检页要的数算出来。**只读**。

    一致率那一路（`truth_run.agreement()`）每次都会**全量重算一遍历史标签**
    （`slicing.load_tags` 的约定：标签不落库），十毫秒级。所以本函数的调用方要**一次算、
    多处用**——`page()` 正是这么做的（正文与摘要同源，`unmet_note` 也收同一份快照）。
    """
    at = now or timezone.now()
    achieved = _progress(now=at)

    if achieved.started_on is None:
        trigger_days = deactivation_days = high_vol_days = event_days = 0
    else:
        trigger_days, deactivation_days, high_vol_days, event_days = _days_with_trigger(
            start=achieved.started_on, today=to_business(at).date(), now=at
        )

    return Confirmation(
        now=at,
        achieved=achieved,
        trigger_days=trigger_days,
        deactivation_days=deactivation_days,
        high_vol_days=high_vol_days,
        event_days=event_days,
        last_back=_last_back(),
        agreement=truth_run.agreement(),
    )


def _last_back() -> RegimeMechanismSwitch | None:
    """最近一次「退回 Shadow」。人工退回与自熔断退回落同一张流水，靠 `actor_kind` 分辨。

    **`kind` 必须限定**：事件熔断、行情阶段 gate 关掉时写的也是 `to_mode=shadow` 的行
    （日报第④段那条注释记着这个坑）。
    """
    return (
        RegimeMechanismSwitch.objects.filter(
            kind=KIND.value, to_mode=MechanismMode.SHADOW.value
        )
        .order_by("-at", "-id")
        .first()
    )


# --------------------------------------------------------------------------- #
# 渲染
# --------------------------------------------------------------------------- #


def _percent(rate: float | None) -> str:
    return "—" if rate is None else f"{rate * 100:.1f}%"


def _start_line(data: Confirmation) -> list[str]:
    """起算日那一行。**「从没跑过」与「跑了 0 天」必须是两句不同的话。**

    天数**只在有差额时**补括号里的那半句：`ran_days` 是自然日数（到期与频率的分母都用
    它），而判定没跑的那几天照样占着分母、却既不触发也不算故障——于是「跑了 20 天」与
    「出了 20 天的结论」是两件事，而只有前者被印出来时，读的人会把一个被稀释过的频率
    当成真实频率。差额为零时不印，免得正常一轮多一句废话。
    """
    if not data.achieved.started_on:
        return [
            "Shadow 尚未开始：一条含资讯结论的判定都还没有。",
            "  起算日 = 第一条成功的资讯判定所在的运行日（第 159 条：Shadow 从资讯通道可用"
            "之后才开始）。",
            "  在那之前这一档没有任何可校验的东西——判定照跑、建议照发，但机制还没被看过。",
        ]
    line = (
        f"Shadow 起算：{data.achieved.started_on}（第一条含资讯结论的判定）"
        f"· 已跑 {data.achieved.ran_days} 个自然日"
    )
    if data.achieved.recorded_days != data.achieved.ran_days:
        line += f"（其中出了结论的 {data.achieved.recorded_days} 天）"
    return [line]


def _expiry_lines(data: Confirmation) -> list[str]:
    """到期条件那一段。兜底到期时**必须**把「事件窗口数不足」写出来。"""

    def mark(ok: bool) -> str:
        return "已满足" if ok else "未到"

    lines = [
        "到期条件（第 159 条）：",
        f"  · 自然日 {data.achieved.ran_days} / {data.min_natural_days} —— "
        f"{mark(data.days_met)}",
        f"  · 期内开启的高影响事件窗口 {data.achieved.windows} / "
        f"{data.min_event_windows} —— {mark(data.windows_met)}",
        f"  · 兜底：满 {data.expiry_cap_days} 个自然日即到期"
        f"（现在 {data.achieved.ran_days} 天）",
    ]
    if data.windows_short:
        lines.append(
            f"  → **已到期（兜底）：事件窗口数不足 {data.achieved.windows}/"
            f"{data.min_event_windows}**——这一句会写进切换流水。"
        )
    elif data.expired:
        lines.append("  → 已到期：准入条件可以校验了（到期不等于达标，见下）。")
    else:
        lines.append("  → 未到期。")
    return lines


def _criteria_lines(data: Confirmation) -> list[str]:
    """成功标准三条各自的今天（第 163 条）。③今天算不出来，**如实说**。

    ①那一段**整段委托给 `truth.describe`**，本模块一个字都不自己拼：同一段话还要出现在
    `/regime label` 的清单底下，而「一致率」是出 Shadow 的依据——两个入口给出两个说法，
    读到哪一个都会让人怀疑另一个。`describe` 返回的行不带缩进（两处的排版不一样），所以
    这里补缩进：首行跟着这一级的 `·` 走，续行再往右三列，措辞一个字不动。
    """
    lines = ["成功标准（第 163 条）："]
    criteria = truth.describe(data.agreement)
    lines.append("  · " + criteria[0])
    lines.extend("  " + extra for extra in criteria[1:])

    rate = data.rate
    if rate is None:
        lines.append(
            f"  · ② 触发频率 < {data.max_trigger_rate:.0%} 自然日：**还不可算**——"
            "Shadow 还没起算。"
        )
    else:
        lines.append(
            f"  · ② 触发频率 < {data.max_trigger_rate:.0%} 自然日："
            f"{_percent(rate)}（{data.trigger_days}/{data.achieved.ran_days} 天）—— "
            f"{'已满足' if data.rate_met else '未达标'}"
        )
        lines.append(
            f"      适用性停用 {data.deactivation_days} 天 / 高波动档 {data.high_vol_days} 天"
            f" / 事件窗口 {data.event_days} 天（去重后合计 {data.trigger_days} 天）"
        )
    lines.append("  · ③ 每次判定出的熔断有完整可读解释：**人工核对**——这一页算不出来。")
    return lines


def _back_lines(data: Confirmation) -> list[str]:
    """上一次退回 Shadow 是谁干的。**自熔断那一档要追根因**（第 161 条）。

    时刻走 `events.format_moment`（双口径）：确认页上的时刻要能与人手里的记录逐字核对，
    而「是哪一天」在跨时区时是个会差一天的答案。这也是另外两个确认页的口径。
    """
    row = data.last_back
    if row is None:
        return ["退回记录：没有（这一档从来没有从执行态退回 Shadow 过）"]
    actor = ActorKind(row.actor_kind)
    lines = [
        f"上一次退回 Shadow：{actor.display}·{row.actor_name}",
        f"  {events.format_moment(row.at)}",
        f"  原因：{row.reason}",
    ]
    if actor is ActorKind.TASK:
        lines.append(
            "  ⚠️ 这一次是**自熔断**退回的：回执行态要把根因一并说出来——是门槛太松，"
            "还是判定确实不行（第 161 条）。不重走完整 Shadow 期，但根因必须落地。"
        )
    return lines


def _beat_lines(data: Confirmation) -> list[str]:
    """调度表体检那一段。**紧跟在「Shadow 尚未开始」后面**，因为它是那句话的原因。

    这一页今天最容易被读错的地方就在这里：起算日为空时页面只说「一条含资讯结论的判定都
    还没有」，而读的人会往「资讯通道还没接上」「才跑了两天」上猜——真正的原因可能是
    **判定任务从来没有被调度过**（`beat_schedule` 只在 beat 启动时合并一次，往文件里加的
    条目不会自动生效）。两句话挨着印，第一句才不会被读成「再等等就好了」。

    `now` 取自快照（`data.now`）：一页上只有一个时钟，否则这一段的「距今」与正文里别的
    时刻会出自两次 `timezone.now()`。

    读数是 `beat_health_run` 的事（那两个来源都不属于本模块），措辞在 `beat_health`。
    """
    lines = beat_health_run.lines(now=data.now)
    return ["  · " + lines[0], *["  " + extra for extra in lines[1:]]]


def _render_body(data: Confirmation) -> str:
    """体检页正文。**只读，跑多少遍都不改变任何东西。**

    之所以是一整页：这一档决定的是「机制整体可不可信」，而它的三条依据分散在三张表里
    （判定、Shadow 记录、事件日历）。分成几条消息发，就会有人只看到其中一条。
    """
    mode = RegimeMechanismSwitch.current(KIND)
    lines = [
        "机制整体 · 出 Shadow 体检（这一步本身不改变任何东西）",
        f"当前档位：{mode.display}",
        "",
        *_start_line(data),
        "",
        *_beat_lines(data),
        "",
    ]
    if data.achieved.started_on:
        lines.extend(_expiry_lines(data))
        lines.append("")
        lines.extend(_criteria_lines(data))
        lines.append("")
    lines.extend(_back_lines(data))
    lines.extend(
        [
            "",
            "机制自熔断（连续 3 个月频率 > 20%）：此刻必然无结论——它是**切过去之后继续"
            "监控**的条款，不构成出 Shadow 的准入门槛（第 160 / 167 条）。",
            "",
            "⚠️ 这一档**不是执行开关**：拦人的是另外两个开关，它们各自开各自的——"
            "出 Shadow 的效果是**留一条记录**（第①段的判定与切片被验证过了），"
            "不是打开了什么。",
            "真要动作：/regime on（事件熔断）、/regime gate on（行情阶段停用）——"
            "这两个才拦人。",
            "真值录入：/regime label add <起> <终> <档位> [备注…]"
            "（第 164 条要求**先标完再看算法输出**，所以录入的那一次不回显任何算法结论）",
            "          /regime label 看清单与一致率",
            "操作：/regime mech exit 出 Shadow（切到执行态）",
            "      /regime mech back 退回 Shadow（只记录，不执行）",
        ]
    )
    return "\n".join(lines)


def _render_summary(data: Confirmation) -> str:
    """进 `reason` 的那一行摘要。**从同一份 `confirmation()` 算出来**。

    刻意是摘要而不是整页：`reason` 会被日报第④段和事后排查读，一行能读完。兜底到期那一刻
    的存在理由就在这几行里——「事件窗口数不足 N/M」必须落进流水，否则那句显式记录只活在
    命令的那条回复里，而回复会被划走。
    """
    if not data.achieved.started_on:
        return "人工出 Shadow：Shadow 尚未开始（一条含资讯结论的判定都没有）"

    rate = data.rate
    parts = [
        f"人工出 Shadow（上线确认）：已跑 {data.achieved.ran_days} 个自然日"
        f"（门槛 {data.min_natural_days}）",
        f"期内高影响事件窗口 {data.achieved.windows}/{data.min_event_windows}",
        f"触发频率 {_percent(rate)}（{data.trigger_days}/{data.achieved.ran_days} 天，"
        f"门槛 < {data.max_trigger_rate:.0%}）",
        # ①那一句取自 `truth.describe` 的首行（与体检页、`/regime label` 同一处措辞）。
        # 摘要是**一行能读完**的，所以只取首行：那几个支撑它的计数留在页面上。
        truth.describe(data.agreement)[0],
    ]
    if data.windows_short:
        parts.append(
            f"**兜底到期：事件窗口数不足 {data.achieved.windows}/{data.min_event_windows}**"
        )
    return "；".join(parts)


def closing_summary(data: Confirmation) -> str:
    """退回 Shadow 时进 `reason` 的摘要。**收已经算好的快照**，不自己再查一遍。

    与「出 Shadow」那份摘要问的不是同一件事：那个回答「能不能切」，这个回答「退回去的
    那一刻留下了什么」。最要紧的是**退回不会解除任何东西**——机制档不是执行开关，停下它
    不动停止声明表、不动策略停用决策，也不动人工豁免（那些各有各的开关与到期）。
    """
    mode = RegimeMechanismSwitch.current(KIND)
    where = (
        "退回前是执行态" if mode is MechanismMode.EXECUTING else "退回前本来就是 Shadow"
    )
    age = (
        f"Shadow 已跑 {data.achieved.ran_days} 个自然日"
        if data.achieved.started_on
        else "Shadow 尚未开始"
    )
    return (
        f"人工退回 Shadow：{where}；不解除任何已施加的停用（声明表与停用决策各有各的"
        f"开关与到期，人工豁免照旧）；{age}"
    )


@dataclass(frozen=True)
class Briefing:
    """一次快照 + 由它渲染出的两段文字。**三个字段同源**，见 `page`。"""

    data: Confirmation
    body: str
    summary: str


def page(*, now: datetime | None = None) -> Briefing:
    """体检页与它的摘要，**一次算出来**。

    两个出口只留这一个求值口：确认页上写的数与人点头之后落进 `reason` 的数必须是同一次
    快照。各算一遍的话，两次查询之间只要判定跑了一轮，页面上就会写着「16 天」而流水里
    记着「17 天」——两个数都出自本模块，却互相矛盾。
    """
    data = confirmation(now=now)
    return Briefing(data=data, body=_render_body(data), summary=_render_summary(data))


def confirmation_body(*, now: datetime | None = None) -> str:
    """只要正文（测试与「只想看一眼」的调用方用）。"""
    return page(now=now).body


def confirmation_summary(*, now: datetime | None = None) -> str:
    """只要摘要。"""
    return page(now=now).summary


def unmet_note(data: Confirmation) -> str | None:
    """切换之后紧接着再报一遍**没达标的那些项**；全达标/无结论时返回 `None`。

    页面里已经说过一次，但那一次在「人点头」之前；这一步是把同一张单子在动作之后再说
    一遍——照旧从**同一份快照**渲染，不重新取数（重新取数会让两个说法在日界附近分家）。
    """
    if not data.achieved.started_on:
        return "⚠️ Shadow 尚未开始：这一档什么都还没被校验过（一条含资讯结论的判定都没有）。"

    gaps: list[str] = []
    if not data.expired and not data.days_met:
        gaps.append(f"自然日 {data.achieved.ran_days}/{data.min_natural_days}")
    if not data.expired and not data.windows_met:
        gaps.append(f"高影响事件窗口 {data.achieved.windows}/{data.min_event_windows}")
    if data.windows_short:
        gaps.append(
            f"兜底到期，事件窗口数不足 {data.achieved.windows}/{data.min_event_windows}"
        )
    if not data.rate_met:
        gaps.append(
            f"触发频率 {_percent(data.rate)}（门槛 < {data.max_trigger_rate:.0%}）"
        )
    if not data.agreement.met:
        # ①与另外几条不同：它**没达标**才出现，达标了就整条不出现（其余几条同理）。
        # 从前这里无条件写「一致率无法判定（真值未录入）」——真值表建起来之后那句话就是
        # 假的：录了真值照样可能算不出来（区间互相冲突），而算得出来时它更不该出现。
        gaps.append(truth.describe(data.agreement)[0])
    gaps.append("解释完整没核对（这一页算不出来）")
    return (
        "⚠️ 还没达标的项："
        + "；".join(gaps)
        + "。这不是禁止，是让你知道这次切换带着什么。"
    )


def expiry_notice(*, now: datetime | None = None) -> str | None:
    """日报第④段在**到期之后**每天说的那一句；未到期返回 `None`。

    **到期前不天天报「还没到期」**：那是二十天的噪声，会训练人忽略第④段；而到期之后
    一直报到出 Shadow 为止，因为「一个没人管的库能让 Shadow 无限期挂着、挂着的样子与
    正常跑着完全一样」正是这条兜底要防的形状，而它只有一个出口：人看见。

    **不建 `Confirmation`**：这条路上只需要「到期了没 / 窗口够不够」两个布尔，判据在模块
    级（`_expired` / `_windows_short`）。从前这里为了让类型对上，造了一个把频率那三个数
    填 0 的假对象——「为了让签名过关而写下的一句假话」，加一个字段就要往里塞一个假值。
    """
    at = now or timezone.now()
    achieved = _progress(now=at)
    if achieved.started_on is None:
        return None
    if not _expired(achieved):
        return None
    if _windows_short(achieved):
        return (
            f"出 Shadow：**已到期（兜底）**——已跑 {achieved.ran_days} 个自然日，"
            f"但期内高影响事件窗口只有 {achieved.windows}/"
            f"{config.SHADOW.min_event_windows} 个"
            f"（事件窗口数不足）。用 /regime mech 看体检页。"
        )
    return (
        f"出 Shadow：**已到期**——已跑 {achieved.ran_days} 个自然日、"
        f"高影响事件窗口 {achieved.windows} 个。用 /regime mech 看体检页。"
    )


# --------------------------------------------------------------------------- #
# 写方：唯一的一次 INSERT
# --------------------------------------------------------------------------- #


def flip_mechanism(
    to_mode: MechanismMode,
    *,
    actor_kind: ActorKind,
    actor_name: str,
    reason: str,
    now: datetime | None = None,
) -> RegimeMechanismSwitch | None:
    """把机制整体那一档切到 `to_mode`，返回落下的那一行；**已经是那一档则不写、返回 `None`**。

    三处与 `breaker_switch.flip_event_breaker` 逐条一致的取舍：`from_mode` 真读出来（不写
    死 shadow）、「已经是这一档」不写第二行、`reason` 与 `actor_name` 必填且拒绝空串。

    **不做任何准入校验**：未到期、频率超标都照写。判据是给人看的（`page` 那一页），不是
    写路径的守卫——把门槛做进写路径，等于让机制替人否决一个明确的决定，而设计里没有任何
    一条给过它这个权力（见模块 docstring「两个方向都不拒绝」）。
    """
    if not str(reason).strip():
        raise ValueError("切换原因不能为空：RegimeMechanismSwitch 的 reason 是必填的")
    if not str(actor_name).strip():
        raise ValueError("触发方不能为空：一次没人认领的开关切换无法排查")

    at = now or timezone.now()
    current = RegimeMechanismSwitch.current(KIND)
    if current is to_mode:
        logger.info("[regime] 机制整体已经是 %s，未重复写流水", to_mode.value)
        return None

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
        "[regime] 机制整体 %s → %s（%s：%s）",
        current.value,
        to_mode.value,
        row.actor_name,
        row.reason,
    )
    return row
