"""三张确认页的统一求值口（P5 子票 #48，map #40）。

## 这里只有一个 dict、一次求值

三个开关各有一页「先看一眼再决定」的确认页，它们的**求值口与形状完全同形**（同一个
`page()` 返回 `body` + `summary` + `data` 三件套），所以 kind → 模块的映射写成一张表
`BRIEFING_SOURCES`，而不是在视图里 `if/elif` 三次。加第四个开关那天，要改的是这张表加
一行；忘掉登记会红在 `test_briefing.test_every_kind_is_registered`——它断言这张表的键集
与 `MechanismKind` 相等。

**`body` 与 `summary` 必须来自同一次 `page()` 求值**，这是本模块存在的全部理由，也是
`mechanism_switch.page()` docstring 钉住的那条不变量：确认页上写的数与人点头之后落进
`reason` 的数必须是同一次快照。所以这里**绝不**分别调一次「取正文」和「取摘要」的便捷
函数（`confirmation_body` / `confirmation_summary` 那两个各调一次 `page()`），而是把
`page()` 返回的那个对象整个收下，再从它身上取两样东西。

**T4（写 API）必须走这同一个 `evaluate()`**：它要在自己的请求里求一次值来取 `reason`，
而「网页上看到的理由」与「流水里记的理由」逐字一致这件事，只能靠两处共用同一段取数
逻辑来保证，不能靠两处各写一遍相同的表达式。

## 方向：与当前档位相反的那一个

页面上那一个动作永远只有一个——当前是 Shadow 就只能出 Shadow，当前是执行态就只能退回
Shadow。所以方向由**当前档位**推出来（`to_mode`），前端不用传：`GET briefing/<kind>/`
问的就是「此刻我能做的那一步，会写进去什么」。这一点对 gate 尤其重要：它支持两个方向的
正文（`page(closing=True/False)` 渲染两套措辞，见 `gate_switch._render_body`），而两个方向
必须是**同一次求值的两种说法**，不是两次求值。另外两个开关今天只有一个方向的正文（关闭
方向在 slash 通路里只回一句「关掉之后什么变了」），所以它们的方向差异只体现在 `summary`
上——`closing_summary(data)` 收的仍是同一份 `data`。

**不缓存**：`mechanism_switch.page()` 会经 `truth_run.agreement(tags=None)` →
`slicing.load_tags()` 全量重算所有历史标签；`gate_switch.page()` 会经
`gate_run.preview()` → `deactivation_run.managed_set()` →
`pool_rebuild.ensure_strategies_discovered()` → `StrategyRegistry.discover()` **重导入全部
策略模块**（进程级副作用）。缓存一个带进程副作用的结果，会让「什么时候重导入」变成一个
无法解释的问题——所以响应里带 `heavy: true` 明说它是重请求，视图再发
`Cache-Control: no-store`（HTTP 层的「不缓存」），前端只在人要看时才拉。

## `details` 是给排版的，不是用来重拼正文的

`body` 是人读的全部叙述；`details` 只是把同一份 `data` 里的**数**摆出来（计数、时刻、
布尔、成对的枚举 display），供前端做角标与分行。**前端不许拿 `details` 重拼一遍 `body`**
——那会造出「同一件事的第二个说法」，而本 map 从头到尾在防的正是这个形态（见
`serializers` 的模块 docstring）。所以这里一个中文字面量都没有：每个 display 都从枚举或
它自己模块的词表取。

## 一条已知的、与 CLI / slash 同款的边界

`mechanism_switch.closing_summary(data)` 在自己内部再读一次
`RegimeMechanismSwitch.current(KIND)`（它要说出「退回前是执行态 / 本来就是 Shadow」），而
`page()` 的正文里也读了一次档位。两个读之间若正好落了一次切换，正文的「当前档位」与摘要
的「退回前是」就会说得不一样。**这一条不是本模块引入的**：CLI（`event_breaker.py`）与
slash（`regime_commands._mech_back`）走的是同一对函数、同样的两次读，本模块只是把它们搬
到了 HTTP 上。所以这里**不改**它（改它会同时改变 CLI 与 slash 的语义，那属于机制本体，
是本 map 的范围外），只记下来：要根治得让 `closing_summary` 收「翻之前的档位」而不是自己
去读，那是另一次改造。

## 冷启动不是一个错误

三张页面各自都写了冷启动收场（`gate_switch` 的 `skipped=cold_start`、`breaker_switch` 的
`empty_library_warning`、`mechanism_switch` 的「Shadow 尚未开始」）。本模块**一个字都不补**
——只在它们自己的话前面加一层 JSON。替它们编一句，就是给冷启动造第二个说法。

## 错误形状

`kind` 不是枚举取值 → **400**（不是 404）：它是请求拼错（枚举取值错），不是「这一页不存在」。
校验在视图里、用 `MechanismKind(slug)` 做，认不出来就 400；`{"error": ...}` 与
`apps/strategy_factory/views.py` 一致。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable, Mapping

from apps.regime import breaker_switch, gate_switch, mechanism_switch
from apps.regime import serializers as ser
from apps.regime.models import MechanismKind, MechanismMode, RegimeMechanismSwitch


# --------------------------------------------------------------------------- #
# 小件
# --------------------------------------------------------------------------- #


def _iso_day(day: date | None) -> str | None:
    """日期 → ISO 串；没有就是 `None`。`serializers.iso` 收的是时刻，日期这条路另走一个。

    不把 `date` 塞给 `ser.iso`：它的 docstring 写着它收的是 `DateTimeField` 列的运行时形状，
    顺手让它也认 `date` 会把这个契约变成「两种形状都可以」。
    """
    return None if day is None else day.isoformat()


# --------------------------------------------------------------------------- #
# 三份 details
# --------------------------------------------------------------------------- #


def _mechanism_details(data: Any) -> dict[str, Any]:
    """机制整体：Shadow 期跑出来的四件事 + 三条门槛的今天。

    这些数全部是 `Confirmation` 的字段或它的 property——**不在这里算任何东西**。一致率那
    一组尤其如此：它来自 `truth.Agreement`，而「一致率怎么算」只有 `truth.tally` 一处。
    """
    achieved = data.achieved
    agreement = data.agreement
    return {
        "achieved": {
            "started_on": _iso_day(achieved.started_on),
            "ran_days": achieved.ran_days,
            "windows": achieved.windows,
            "recorded_days": achieved.recorded_days,
        },
        "thresholds": {
            "min_natural_days": data.min_natural_days,
            "min_event_windows": data.min_event_windows,
            "expiry_cap_days": data.expiry_cap_days,
            "max_trigger_rate": data.max_trigger_rate,
        },
        "expiry": {
            "days_met": data.days_met,
            "windows_met": data.windows_met,
            "expired": data.expired,
            # 兜底到期（日子够了、事件窗口没够）是唯一必须显式说出来的缺口（第 159 条）。
            "windows_short": data.windows_short,
        },
        "trigger_days": {
            # 三个触发源**分行报**、`total` 是并集：超标之后人第一个要问的就是「是哪一类
            # 把它顶上去的」（`mechanism_switch._days_with_trigger`）。
            "total": data.trigger_days,
            "deactivation": data.deactivation_days,
            "high_vol": data.high_vol_days,
            "event": data.event_days,
        },
        "rate": {
            # 门槛只在上面 `thresholds` 里出现一次：`rate["max"]` 与
            # `thresholds["max_trigger_rate"]` 是同一个数，两处各放一份就是「门槛是多少」
            # 的第二个说法（改配置时改一处、漏一处，页面就会自相矛盾）。
            "value": data.rate,
            "met": data.rate_met,
        },
        "agreement": {
            "intervals": agreement.intervals,
            "days": agreement.days,
            "conflict_days": agreement.conflict_days,
            "escalated": agreement.escalated,
            "escalated_matched": agreement.escalated_matched,
            "counted": agreement.counted,
            "matched": agreement.matched,
            "differed": agreement.differed,
            "missing": agreement.missing,
            "misdirected": agreement.misdirected,
            # 一致率的门槛**不放这里**：它读的是 `config.SHADOW.min_agreement_rate`，而本块
            # 说的是「这次比对算出了什么」。要门槛的话它有自己那一行（`truth.describe` 的
            # 首行由 `body` 承载），在这里再放一份就是第三个说法。
            "rate": agreement.rate,
            "met": agreement.met,
        },
    }


def _event_summary(event: Any) -> dict[str, Any] | None:
    """一条重大事件的**摘要**（不是全文）：名字、窗口两端、三个成对的枚举。

    `impact` / `status` / `scope` 的 display 取自模型自己的 property（`impact_display` 之类），
    不查 `EventImpact` 之类的枚举表：模型那几个 property 已经是「这条事件怎么说法」的
    唯一出处，第三处抄一遍就是第三份词表。
    """
    if event is None:
        return None
    return {
        "name": event.name,
        "halt_at": ser.iso(event.halt_at),
        "resume_at": ser.iso(event.resume_at),
        # 三对枚举都走 `serializers` 的同形助手：**成对是结构保证，不是纪律**，而在这里
        # 另写一个 `{"value": ..., "display": ...}` 就是这条保证的第二个实现。
        "impact": ser.event_impact_payload(event),
        "status": ser.event_status_payload(event),
        "scope": ser.event_scope_payload(event),
    }


def _breaker_details(data: Any) -> dict[str, Any]:
    """事件熔断：未来窗口的那几个数与库里最相关的三条事件。"""
    return {
        "now": ser.iso(data.now),
        "until": ser.iso(data.until),
        "horizon_days": data.horizon_days,
        "library_total": data.library_total,
        "firing_total": data.firing_total,
        "overlapping": data.overlapping,
        "starting": data.starting,
        # 三条都带出来：`open_now` 是「关掉的这一刻正压在窗口里」，`next_event` 是「接下来
        # 一条」，`nearest` 是平静期那句话的锚——三个问题三个对象，合成一个就答不全。
        "open_now": _event_summary(data.open_now),
        "next_event": _event_summary(data.next_event),
        "nearest": _event_summary(data.nearest),
        "staleness_days": data.staleness_days,
    }


def _gate_details(data: Any) -> dict[str, Any]:
    """行情阶段 gate：举手的那几个计数 + 声明表的收场。

    `closing_by_code` 的 display 取 `gate_switch.gate_close_reason_display`（那个词表只说策略
    停用一件事，与 `halt_sync` 的同名码不同义）；`skipped_display` 取 `Confirmation` 自己的
    property——两处都已存在，不在这里另写。

    **这里没有 `mode`**（虽然 `gate_switch.Confirmation` 有那个字段）：顶层那个 `mode` 已经
    回答了同一件事，而两处各答一次就是「此刻是哪一档」的两个读（`Confirmation.mode` 来自
    `gate_run.preview()` 内部那次读档，顶层那个来自 `evaluate()` 那次）。两次读之间落一次
    切换，页面上就会出现两个互相矛盾的档位。所以档位只留顶层那一个出口。
    """
    return {
        "as_if_open": data.as_if_open,
        # 冷启动时 `regime` 是 `None`，但那句话仍然要有人给：`regime_display` 在那种情况下
        # 是「（无）」（`gate_switch._regime_display`），与 T2 的 `overview.regime_state.cold`
        # 是同一取向——`null` 只说了「没有」，没说是「从没判定过」。
        "regime": None if data.regime is None else ser.regime_payload(data.regime),
        "regime_display": data.regime_display,
        "regime_effective_at": ser.iso(data.regime_effective_at),
        "blanket": data.blanket,
        "blanket_hold": data.blanket_hold,
        "age_days": data.age_days,
        "generation_version": data.generation_version,
        "generation_at": ser.iso(data.generation_at),
        "managed": data.managed,
        "running": data.running,
        "targets": data.targets,
        "exempt": data.exempt,
        "declared": data.declared,
        "live_declarations": data.live_declarations,
        "orphan": data.orphan,
        "closing_by_code": [
            {
                "code": code,
                "display": gate_switch.gate_close_reason_display(code),
                "count": count,
            }
            for code, count in data.closing_by_code
        ],
        # `skipped` 非空 ⇒ 本轮一个字都没动。码与它的人话都要给：码是给人去日志里对的，
        # 人话是给页面直接显示的（`Confirmation.skipped_display`）。
        "skipped": data.skipped,
        "skipped_display": data.skipped_display,
    }


# --------------------------------------------------------------------------- #
# kind → 模块
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Source:
    """一个开关的确认页取数口。

    `page_accepts_direction` 是这三个模块之间**唯一**的形状差异：只有 `gate_switch.page()`
    认 `closing=`（它的关闭方向有另一套正文，见 `_render_body`）。把它写成显式字段而不是
    `try: page(closing=...) except TypeError`——后者会把「参数拼错了」也吞成「这个模块不
    支持方向」，而那两声是同一种沉默。
    """

    module: Any
    page_accepts_direction: bool
    details: Callable[[Any], dict[str, Any]]


#: kind → 模块。**加第四个开关时改这一张表**（`test_briefing` 断言键集 == `MechanismKind`）。
#: 三个模块的 `page()` / `closing_summary()` 同名同形，所以这里只登记模块本身——于是测试
#: 里 `patch.object(gate_switch, "page")` 这类替身仍然生效（属性在调用那一刻才取）。
BRIEFING_SOURCES: Mapping[MechanismKind, Source] = {
    MechanismKind.MECHANISM: Source(
        module=mechanism_switch,
        page_accepts_direction=False,
        details=_mechanism_details,
    ),
    MechanismKind.EVENT_BREAKER: Source(
        module=breaker_switch,
        page_accepts_direction=False,
        details=_breaker_details,
    ),
    MechanismKind.REGIME_GATE: Source(
        module=gate_switch,
        page_accepts_direction=True,
        details=_gate_details,
    ),
}


# --------------------------------------------------------------------------- #
# 求值
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Briefing:
    """一次求值的全貌：档位、这一页对应的动作、以及页面自己的三件套。

    **`body` 与 `summary` 出自同一次 `page()` 调用**（见模块 docstring）。`mode` / `to_mode`
    是这次求值发生时读到的档位——`to_mode` 就是人点头之后 `flip_*` 会切到的那一档，T4 拿
    它决定 `reason` 取哪一份摘要，与 CLI / slash 的取法逐字一致。
    """

    kind: MechanismKind
    mode: MechanismMode
    to_mode: MechanismMode
    data: Any
    body: str
    summary: str


def opposite(mode: MechanismMode) -> MechanismMode:
    """另一个档位。两个档位是一枚硬币的两面，所以「另一个」不需要第三个分支。"""
    return (
        MechanismMode.SHADOW
        if mode is MechanismMode.EXECUTING
        else MechanismMode.EXECUTING
    )


def evaluate(
    kind: MechanismKind | str,
    *,
    to_mode: MechanismMode | str | None = None,
    now: datetime | None = None,
) -> Briefing:
    """求一张确认页。**一次 `page()` 调用**，正文与摘要都从它身上取。

    方向（`to_mode`）决定两件事：取哪一套正文（gate 有两个方向）、以及 `summary` 取
    `page.summary` 还是 `closing_summary(page.data)`。

    **省略 `to_mode` 时取「与当前档位相反的那一档」**——只读端点问的正是这个（「此刻我能
    做的那一步，会写进去什么」），所以它不需要传方向，也传不了：`GET briefing/<kind>/`
    没有第二个参数。

    T4（写 API）**显式传**客户端请求的那一档，而不是靠当前档位反推：它问的是「你要做的
    这一步」。两者在「本来就是这一档」那种请求上会分家（反推给出的是反向，而请求的是原地
    不动），而 `reason` 只在真正翻过去时才写——所以让调用方说清它问的是哪一步。

    Args:
        kind: 三个开关之一（`MechanismKind` 或其取值）。
        to_mode: 这一页对应的动作；省略见上。
        now: 注入时钟（测试用）；不传由各模块自己取当前时刻。

    Raises:
        ValueError: `kind` 不是 `MechanismKind` 的取值（视图转成 400），或 `to_mode` 不是
            `MechanismMode` 的取值（写 API 转成 400）。
    """
    switch = MechanismKind(kind)
    source = BRIEFING_SOURCES[switch]

    # `mode` 这一栏永远说「此刻是哪一档」（页面上两个数都要）；方向另由 `target` 说。
    mode = RegimeMechanismSwitch.current(switch)
    target = opposite(mode) if to_mode is None else MechanismMode(to_mode)
    closing = target is MechanismMode.SHADOW

    if source.page_accepts_direction:
        page = source.module.page(closing=closing, now=now)
    else:
        page = source.module.page(now=now)

    # 关闭方向的 `reason` 问的是另一件事（「关掉的那一刻放掉了什么」），所以换一份摘要；
    # 但快照仍是上面那一份——`closing_summary` 收的正是这个 `data`（T4 照同一句取）。
    summary = source.module.closing_summary(page.data) if closing else page.summary

    return Briefing(
        kind=switch,
        mode=mode,
        to_mode=target,
        data=page.data,
        body=page.body,
        summary=summary,
    )


def payload(
    kind: MechanismKind | str, *, now: datetime | None = None
) -> dict[str, Any]:
    """端点要返回的整块 JSON：三件套 + 档位 + 排版用的数。

    形状（键集恒定，与路径无关——「今天和昨天有什么不同」不该多出一堆假差异）：

    | 键 | 是什么 |
    | --- | --- |
    | `kind` | 哪个开关，`{value, display}` |
    | `mode` | **此刻**的档位，`{value, display}` |
    | `to_mode` | 这一页对应的动作（与 `mode` 相反的那一档），`{value, display}` |
    | `body` | 叙述正文，原样文本 |
    | `summary` | 这一动作会写进 `reason` 的审计串，与 `body` 同源 |
    | `details` | 排版用的数（见各 `_*_details`），**不用于重拼 `body`** |
    | `heavy` | 恒 `true`：这是一次重请求，不许进轮询（见模块 docstring） |
    """
    result = evaluate(kind, now=now)
    return {
        "kind": ser.kind_payload(result.kind),
        "mode": ser.mode_payload(result.mode),
        "to_mode": ser.mode_payload(result.to_mode),
        "body": result.body,
        "summary": result.summary,
        "details": BRIEFING_SOURCES[result.kind].details(result.data),
        "heavy": True,
    }
