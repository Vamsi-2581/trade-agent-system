"""重大事件与候选事件的**录入端**（第①段单元 8ii）。

本模块是事件表在系统里**唯一的写入口**。它只做四件事：把人的输入校验成事实、把窗口
算好、落库、留一条流水。窗口怎么被读取、熔断期内谁被停、减仓怎么叠，全是第②段的事，
本模块一行都不写——`config.EventsConfig` 的 docstring 已经把这半边的归属说死了。

## 为什么所有校验都挤在录入那一刻

CONTEXT.md 第 147 条的口径是「把失效点前移到录入时刻，因为那是唯一能让人看见它的
位置」。所以这里的错误一律是 `EventInputError`，且**每条消息都要给出候选**：未知品种
要把已知品种列出来，覆盖值越界要把上下限写出来。这些输入来自 slash 命令，报错是唯一
的反馈面——一条「参数非法」的报错等于让人自己去猜。

本模块因此不做任何模糊匹配：不查别名表、不做大小写归一、不做前缀匹配。写错一个字母
的品种名在熔断窗口里静默不生效，而「不生效」看起来与「今天没有事件」完全一样。

## 谁在写

三种触发方，`ActorKind` 分得很清：`cli`（命令行）、`chat`（聊天渠道 slash 命令）、
`task`（定时任务）。**只有前两种是「人」**，而 CONTEXT.md 第 149 条要求「提升为『高』
必须人工」，第 152 条要求 Agent 侧不持有任何写权限。这两句话在代码里是同一条判据：
`_ensure_human()` 拦住一切 `task` 来源的「高」——包括直接以「高」录入，与事后升档。

## 「到期」是全模块唯一的自动写入

`expire_candidates()` 不需要人，因为它不改变任何**事实**：一条候选过了失效期还是
「提过、没人确认」，只是在库里的状态从 `pending` 变成 `discarded`。CONTEXT.md 第 37 条
要求到期即丢弃，第 152 条又要求拿「提过没人理」当覆盖率衰减的证据——两条只有同时满足
「到期改状态、不删行」才不打架。它也不产生任何熔断：候选表里连 `halt_at` 都没有一列。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta, timezone

from django.db import transaction
from django.utils import timezone as django_timezone

from apps.common.time_utils import business_tz_label, format_business, to_business
from apps.regime import config
from apps.regime.models import (
    CANDIDATE_DISCARD_EXPIRED,
    CANDIDATE_DISCARD_REJECTED,
    ActorKind,
    CandidateEvent,
    CandidateOrigin,
    CandidateStatus,
    EventChangeKind,
    EventImpact,
    EventScope,
    EventStatus,
    MajorEvent,
    MajorEventChange,
)

logger = logging.getLogger(__name__)

#: 录入时接受的口语时刻写法。日期分隔符两种、时分之间两种，秒可省。
#: 刻意不收「带时区偏移的 ISO」：`+08:00` 与「按北京时间解释」会给出同一个绝对时刻，
#: 而 `Z` 会给出另一个——两种写法并存的话，「我录的到底是几点」就要靠人记住哪个后缀
#: 被解释成了什么。带偏移的一律显式拒绝（见 `parse_business_time`）。
_BUSINESS_TIME_RE = re.compile(
    r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})[ T](\d{1,2}):(\d{2})(?::(\d{2}))?$"
)

_BUSINESS_TIME_HINT = (
    "需要「年-月-日 时:分」，如 2026-09-25 20:30，"
    f"一律按北京时间解释（{business_tz_label()}）"
)


class EventInputError(ValueError):
    """录入端的拒绝。**与故障分开**：这是「你给的东西不对」，不是「系统坏了」。

    调用方（slash 命令）把它当正常的用户反馈打印出来；其余异常照旧往上抛给任务健康
    检查。合并的话，一次手滑和一次数据库故障在用户眼里会长得一样。
    """


# --------------------------------------------------------------------------- #
# 品种口径：与下单同一套写法
# --------------------------------------------------------------------------- #


def known_symbols() -> list[str]:
    """本系统已知的品种，与订单同一套口径（ccxt 的 `BASE/QUOTE`）。

    **没有品种目录**（CONTEXT.md 第 147 条提到的那张表不存在），所以「已知」只能取
    库里的实际取值：回测结果、实盘会话、订单三张表的并集。`Strategy` 上没有 `symbol`
    字段，所以策略集合在这里帮不上忙。

    刻意**不做缓存**：这个函数只在录入端被调用（一天几次），而缓存的失效方式恰恰是
    「新开了 BTC 的会话，事件却录不进去」——一个要过很久才有人报的错。
    """
    from apps.backtest.models import BacktestResult
    from apps.trading.models import LiveSession, Order

    found: set[str] = set()
    for model in (BacktestResult, LiveSession, Order):
        found.update(
            symbol
            for symbol in model.objects.values_list("symbol", flat=True).distinct()
            if symbol
        )
    return sorted(found)


def validate_symbols(
    symbols: Iterable[str], *, known: Sequence[str] | None = None
) -> list[str]:
    """校验并归一品种列表：去空白、去重、保序。任何不认识的一项都直接拒绝。

    **遇到第一个不认识的就抛**，不是收集完再抛：录入场景里人是一次给一两条，一起报
    出来并不比逐条修更快，而「部分接受」是最坏的一种——留下的一半会让人以为整条写成了。

    带结算后缀的符号（`SOL/USDT:USDT`）走单独的报错分支，因为它的修法与「写错名字」
    完全不同：前者要去掉后缀，后者要去核对写法。
    """
    known = known_symbols() if known is None else list(known)
    known_set = set(known)

    out: list[str] = []
    for raw in symbols:
        token = str(raw).strip()
        if not token:
            continue
        if ":" in token:
            raise EventInputError(
                f"本系统暂不支持带结算后缀的符号：{token}\n"
                "请用与下单相同的现货口径重录（BASE/QUOTE，如 SOL/USDT）"
            )
        if token not in known_set:
            catalog = "、".join(known) if known else "（库里还没有任何品种）"
            raise EventInputError(
                f"未知品种：{token}\n"
                f"本系统已知的品种（来自回测结果 / 实盘会话 / 订单）：{catalog}\n"
                "录入端不做模糊匹配，请用与下单完全相同的写法重录"
            )
        if token not in out:
            out.append(token)
    return out


def validate_scope(
    scope_kind: str, symbols: Iterable[str] = (), *, known: Sequence[str] | None = None
) -> tuple[str, list[str]]:
    """校验作用域，返回归一后的 `(scope_kind, symbols)`。

    CONTEXT.md 第 147 条：**作用域必填，没有「默认全市场」这条退路**。「忘了填」与
    「确实影响全市场」在库里长得一模一样，而两者的代价差着几个量级——前者静默地什么都不
    影响，后者停掉全部策略。
    """
    if scope_kind not in {m.value for m in EventScope}:
        raise EventInputError(
            f"未知的作用域：{scope_kind!r}，只能是 "
            f"{'、'.join(f'{m.value}（{m.display}）' for m in EventScope)}"
        )

    tokens = [str(item).strip() for item in (symbols or []) if str(item).strip()]

    if scope_kind == EventScope.MARKET.value:
        if tokens:
            raise EventInputError(
                "作用域为「全市场」时不能再给品种列表："
                "两者的含义相反，同时给出等于让读的人自己挑一个信"
            )
        return scope_kind, []

    if not tokens:
        raise EventInputError(
            "作用域为「指定品种」时必须给出至少一个品种"
            "（全市场请显式选「全市场」，不要留空）"
        )
    return scope_kind, validate_symbols(tokens, known=known)


# --------------------------------------------------------------------------- #
# 时刻：存 UTC，录入按北京时间解释，回显两个绝对时刻
# --------------------------------------------------------------------------- #


def parse_business_time(text: str) -> datetime:
    """把口语时刻解析成**存库用的 UTC 时刻**（CONTEXT.md 第 149 条）。

    统一口径见 `apps/common/time_utils.py`：不带时区的时间一律按北京时间解释。这里
    刻意**显式拒绝**带偏移的写法，而不是「有偏移就按偏移解释」——两种解释并存的话，
    `2026-09-25T20:30+08:00` 与 `2026-09-25 20:30` 指同一刻、而 `...Z` 指另一刻，
    命令的语义就取决于一个后缀。
    """
    raw = (text or "").strip()
    if not raw:
        raise EventInputError(f"事件时刻不能为空。{_BUSINESS_TIME_HINT}")

    if "+" in raw or raw.endswith(("Z", "z")):
        raise EventInputError(
            f"事件时刻不要带时区偏移：{raw}\n"
            f"录入一律按北京时间解释，请写成不带后缀的写法。{_BUSINESS_TIME_HINT}"
        )

    match = _BUSINESS_TIME_RE.match(raw)
    if match is None:
        raise EventInputError(f"无法识别的事件时刻：{raw}\n{_BUSINESS_TIME_HINT}")

    year, month, day, hour, minute, second = match.groups()
    try:
        naive = datetime(
            int(year), int(month), int(day), int(hour), int(minute), int(second or 0)
        )
    except ValueError as exc:
        raise EventInputError(f"不是合法的时刻：{raw}（{exc}）") from exc

    # `to_business` 把 naive 当作「已经是业务时区时间」，正是这里的语义。
    return to_business(naive).astimezone(timezone.utc)


def format_moment(moment: datetime | None) -> str:
    """一个绝对时刻的**双口径回显**：北京时间与 UTC 各写一遍。

    CONTEXT.md 第 149 条只对「确认与日报」点了名，但录入端的回显同样需要——命令的
    输出就是人核对「我输的 20:30 是不是它理解的 20:30」的唯一机会。少掉任一边，
    核对就变成心算。
    """
    if moment is None:
        return "未定"
    utc = to_business(moment).astimezone(timezone.utc)
    return f"北京时间 {format_business(moment)} / UTC {utc:%Y-%m-%d %H:%M}"


def resolve_window(
    event_time: datetime,
    *,
    halt_before_minutes: int | None = None,
    resume_after_minutes: int | None = None,
    params: config.EventsConfig | None = None,
) -> tuple[datetime, datetime]:
    """事件时刻 + 窗口参数 → `(halt_at, resume_at)`。**只在录入端调用。**

    覆盖值（单事件放宽/收窄）必须落在全局上下限 `[window_floor_minutes,
    window_cap_minutes]` 之内：拦住的是一个手滑多打一个 0 的覆盖值把单条事件变成三天
    停摆。下限拦的是另一种：一个短到管道都来不及跑完的窗口，停与恢复会在同一分钟内
    前后发生。

    默认值与覆盖值都在这里一次性定死，返回值被原样存进 `MajorEvent.halt_at/resume_at`
    ——之后改配置不会追溯改变任何一条已入库事件的窗口。
    """
    params = params or config.EVENTS
    before = _resolve_span(
        "停止提前量", halt_before_minutes, params.default_halt_before_minutes, params
    )
    after = _resolve_span(
        "恢复延后量", resume_after_minutes, params.default_resume_after_minutes, params
    )
    return (
        event_time - timedelta(minutes=before),
        event_time + timedelta(minutes=after),
    )


def _resolve_span(
    label: str,
    override: int | None,
    default: int,
    params: config.EventsConfig,
) -> int:
    if override is None:
        return default
    if isinstance(override, bool) or not isinstance(override, int):
        raise EventInputError(f"{label}的覆盖值必须是整数分钟，当前 {override!r}")
    if not params.window_floor_minutes <= override <= params.window_cap_minutes:
        raise EventInputError(
            f"{label}的覆盖值 {override} 分钟超出全局上下限 "
            f"[{params.window_floor_minutes}, {params.window_cap_minutes}] 分钟"
        )
    return override


def _require_aware(moment: datetime, label: str) -> datetime:
    if not isinstance(moment, datetime):
        raise EventInputError(f"{label}必须是 datetime，当前 {type(moment).__name__}")
    if moment.tzinfo is None:
        raise EventInputError(
            f"{label}必须带时区：naive 时刻在这里没有任何可靠的解释方式"
            "（口语时刻请先走 parse_business_time）"
        )
    return moment.astimezone(timezone.utc)


# --------------------------------------------------------------------------- #
# 维护动作：录入 / 改期 / 改档 / 取消
# --------------------------------------------------------------------------- #


def create_event(
    *,
    name: str,
    event_time: datetime,
    impact: str,
    scope_kind: str,
    symbols: Iterable[str] = (),
    halt_before_minutes: int | None = None,
    resume_after_minutes: int | None = None,
    actor_kind: str,
    actor_name: str,
    note: str = "",
    now: datetime | None = None,
    params: config.EventsConfig | None = None,
) -> MajorEvent:
    """录入一条重大事件。落库前把窗口算好，并留一条 `created` 流水。"""
    now = now or django_timezone.now()
    params = params or config.EVENTS

    clean_name = (name or "").strip()
    if not clean_name:
        raise EventInputError("事件名称不能为空")

    impact = _validate_impact(impact)
    if impact == EventImpact.HIGH.value:
        # 直接以「高」录入与事后升档是同一个动作的两条路径，所以走同一条判据。
        _ensure_human(actor_kind, "录入「高」影响事件")

    scope_kind, clean_symbols = validate_scope(scope_kind, symbols)
    event_time = _require_aware(event_time, "事件时刻")
    halt_at, resume_at = resolve_window(
        event_time,
        halt_before_minutes=halt_before_minutes,
        resume_after_minutes=resume_after_minutes,
        params=params,
    )

    with transaction.atomic():
        event = MajorEvent.objects.create(
            name=clean_name,
            scope_kind=scope_kind,
            symbols=clean_symbols,
            event_time=event_time,
            impact=impact,
            halt_at=halt_at,
            resume_at=resume_at,
            status=EventStatus.SCHEDULED.value,
            created_by=actor_name,
            note=note or "",
        )
        _log_change(
            event,
            EventChangeKind.CREATED,
            before={},
            after=_event_snapshot(event),
            actor_kind=actor_kind,
            actor_name=actor_name,
            note=note or "",
            at=now,
        )
    return event


def reschedule_event(
    event: MajorEvent,
    *,
    event_time: datetime,
    halt_before_minutes: int | None = None,
    resume_after_minutes: int | None = None,
    actor_kind: str,
    actor_name: str,
    note: str = "",
    now: datetime | None = None,
    params: config.EventsConfig | None = None,
) -> MajorEvent:
    """改期：按新的事件时刻重算窗口，并留一条 `rescheduled` 流水。

    **已取消的事件不能改期**（CONTEXT.md 第 154 条不允许「只改期不删除」的反面）：
    取消是终态，要复活就重新录一条——否则「这条到底是取消了还是改期了」取决于哪条
    命令最后跑过。

    ## 窗口内改期不中止已开启的窗口

    落库的是**重算后的**窗口，所以「窗口进行中改期」会让这一行的 `resume_at` 变早或
    变晚。这**不**等于已开启的熔断被中止：第②段开启熔断时会各自留下自己的记录，那条
    记录才是「已经发生的动作」的真相，改这里的一行覆盖不了它。本表负责的是「从现在起
    这个窗口长什么样」，而被改动前的取值完整留在 `before` 里——「那天为什么从 10:00
    就停了」要靠那份流水回答。
    """
    now = now or django_timezone.now()
    params = params or config.EVENTS

    _ensure_scheduled(event, "改期")
    event_time = _require_aware(event_time, "事件时刻")
    halt_at, resume_at = resolve_window(
        event_time,
        halt_before_minutes=halt_before_minutes,
        resume_after_minutes=resume_after_minutes,
        params=params,
    )

    before = _event_snapshot(event)
    with transaction.atomic():
        event.event_time = event_time
        event.halt_at = halt_at
        event.resume_at = resume_at
        event.save()
        _log_change(
            event,
            EventChangeKind.RESCHEDULED,
            **_diff(before, _event_snapshot(event)),
            actor_kind=actor_kind,
            actor_name=actor_name,
            note=note or "",
            at=now,
        )
    return event


def change_impact(
    event: MajorEvent,
    *,
    impact: str,
    actor_kind: str,
    actor_name: str,
    note: str = "",
    now: datetime | None = None,
) -> MajorEvent:
    """改档（升档或降档），留一条 `impact_changed` 流水。

    升到「高」必须由人确认（CONTEXT.md 第 149 条）。**降档同样是人工动作**：唯一会
    自己改档的调用方不存在，所以这里不区分方向，两条都要求人来敲。
    """
    now = now or django_timezone.now()
    _ensure_scheduled(event, "改档")

    impact = _validate_impact(impact)
    if impact == EventImpact.HIGH.value:
        _ensure_human(actor_kind, "把事件提升为「高」")
    if impact == event.impact:
        raise EventInputError(
            f"这条事件的冲击档位已经是「{event.impact_display}」，没有可改的内容"
        )

    before = _event_snapshot(event)
    with transaction.atomic():
        event.impact = impact
        event.save()
        _log_change(
            event,
            EventChangeKind.IMPACT_CHANGED,
            **_diff(before, _event_snapshot(event)),
            actor_kind=actor_kind,
            actor_name=actor_name,
            note=note or "",
            at=now,
        )
    return event


def cancel_event(
    event: MajorEvent,
    *,
    reason: str,
    actor_kind: str,
    actor_name: str,
    now: datetime | None = None,
) -> MajorEvent:
    """取消：改状态而不是删行，并留一条 `cancelled` 流水。**理由是必填的。**

    改状态而不是删行，是因为窗口可能已经开启过——那条事件解释过一段真实发生过的停摆。
    删掉的话，第②段的熔断记录会指向一条不存在的事件，而那正是审计链断掉的地方。

    取消**不能豁免已经发生的动作**：本函数只改 `status`，已开启的窗口由第②段的记录
    继续承载。同理，`resume` 一类的命令只作用于行情阶段驱动的停用，对事件熔断无效
    （CONTEXT.md 第 148 条）——本模块连这样的入口都不提供。
    """
    now = now or django_timezone.now()
    _ensure_scheduled(event, "取消")

    clean_reason = (reason or "").strip()
    if not clean_reason:
        raise EventInputError(
            "取消必须给出理由：这条理由是日后唯一能回答「为什么没停」的东西"
        )

    before = _event_snapshot(event)
    with transaction.atomic():
        event.status = EventStatus.CANCELLED.value
        event.save()
        _log_change(
            event,
            EventChangeKind.CANCELLED,
            **_diff(before, _event_snapshot(event)),
            actor_kind=actor_kind,
            actor_name=actor_name,
            note=clean_reason,
            at=now,
        )
    return event


# --------------------------------------------------------------------------- #
# 候选事件：提出 / 转正 / 丢弃 / 到期
# --------------------------------------------------------------------------- #


def raise_candidate(
    *,
    name: str,
    origin: str,
    raised_by: str,
    guessed_time: datetime | None = None,
    news_item=None,
    note: str = "",
    now: datetime | None = None,
    params: config.EventsConfig | None = None,
) -> CandidateEvent:
    """提出一条候选事件。**它不产生任何熔断**，只进日报第③段末尾那一节。

    Agent 与资讯走的是这同一个入口（CONTEXT.md 第 94 条「同一条出口」），差别只在
    `origin` 与 `raised_by`：前者是当时那句对话的 sender id，后者是资讯源名。
    """
    now = now or django_timezone.now()
    params = params or config.EVENTS

    clean_name = (name or "").strip()
    if not clean_name:
        raise EventInputError("候选事件的名称不能为空")

    if origin not in {m.value for m in CandidateOrigin}:
        raise EventInputError(
            f"未知的提出方：{origin!r}，只能是 "
            f"{'、'.join(f'{m.value}（{m.display}）' for m in CandidateOrigin)}"
        )
    # 资讯提的候选必须带着原文条目：日报那一节要能点回原文，而「某条资讯说下周有大事」
    # 在几天后唯一可核对的依据就是那一条。Agent 提的候选没有对应资讯条目，它带的是对话。
    if origin == CandidateOrigin.NEWS.value and news_item is None:
        raise EventInputError("资讯判定提出的候选必须带上来源资讯条目")

    clean_by = (raised_by or "").strip()
    if not clean_by:
        raise EventInputError("必须记下提出方标识（资讯源名 / sender id）")

    if guessed_time is not None:
        guessed_time = _require_aware(guessed_time, "推测时刻")

    return CandidateEvent.objects.create(
        name=clean_name,
        origin=origin,
        guessed_time=guessed_time,
        # 提出日期用**业务日**：日报按「什么时候提的」计龄，而业务日的边界与日线换线
        # 是同一个绝对时刻（北京 08:00）。用本地自然日会让同一条候选在跨日那几小时里
        # 归属成两天。
        raised_at=to_business(now).date(),
        raised_by=clean_by,
        news_item=news_item,
        note=note or "",
        expires_at=now + timedelta(days=params.candidate_expiry_days),
        status=CandidateStatus.PENDING.value,
    )


def confirm_candidate(
    candidate: CandidateEvent,
    *,
    event_time: datetime,
    impact: str,
    scope_kind: str,
    symbols: Iterable[str] = (),
    halt_before_minutes: int | None = None,
    resume_after_minutes: int | None = None,
    actor_kind: str,
    actor_name: str,
    note: str = "",
    now: datetime | None = None,
    params: config.EventsConfig | None = None,
) -> MajorEvent:
    """把一条候选转正成重大事件（CONTEXT.md 第 37 条的唯一出路之一）。

    **时间、冲击档位、作用域必须由人重新说一遍**，不从候选上继承任何一项：候选表里
    这三样要么是空的（`guessed_time` 可空），要么是推测的。继承的话，一条「某条资讯
    说下周有大事」会带着一个编出来的钟点直接变成熔断窗口——而窗口是会自动停掉全场策略
    的东西。转正因此在 `create_event` 之上只有「补一条外键」这一层。

    也正因为它走 `create_event`，「提升为高必须人工」这条判据（含 `task` 来源的拦截）
    在这里一并生效，不需要第二处实现。
    """
    now = now or django_timezone.now()
    if not candidate.is_pending:
        raise EventInputError(
            f"这条候选已经处置过（{candidate.status_display}），终态不可回退"
        )

    with transaction.atomic():
        event = create_event(
            name=candidate.name,
            event_time=event_time,
            impact=impact,
            scope_kind=scope_kind,
            symbols=symbols,
            halt_before_minutes=halt_before_minutes,
            resume_after_minutes=resume_after_minutes,
            actor_kind=actor_kind,
            actor_name=actor_name,
            note=_confirm_note(candidate, note),
            now=now,
            params=params,
        )
        candidate.status = CandidateStatus.CONFIRMED.value
        candidate.decided_at = now
        candidate.decided_by = actor_name
        candidate.confirmed_event = event
        candidate.save()
    return event


def discard_candidate(
    candidate: CandidateEvent,
    *,
    reason: str,
    actor_name: str,
    now: datetime | None = None,
) -> CandidateEvent:
    """人工否决一条候选。转正过的候选不能再被丢弃（模型上有 CHECK 兜着）。

    `decided_by` 在这里**必须**写上人——它与 `expired` 那条自动路径的差别就靠这一栏：
    「到期未确认」的 `decided_by` 是空的，因为确实没人看过。
    """
    now = now or django_timezone.now()
    if reason not in (CANDIDATE_DISCARD_EXPIRED, CANDIDATE_DISCARD_REJECTED):
        raise EventInputError(
            f"未知的丢弃原因：{reason!r}，只能是 {CANDIDATE_DISCARD_EXPIRED}"
            f"（到期未确认）或 {CANDIDATE_DISCARD_REJECTED}（人工否决）"
        )
    if reason == CANDIDATE_DISCARD_EXPIRED:
        raise EventInputError(
            "「到期未确认」由定时清理自动落，不走人工入口——"
            "人工丢弃请用「否决」，好让日报分得清「没人看」与「看过了」"
        )
    if not candidate.is_pending:
        raise EventInputError(
            f"这条候选已经处置过（{candidate.status_display}），终态不可回退"
        )

    candidate.status = CandidateStatus.DISCARDED.value
    candidate.decided_at = now
    candidate.decided_by = (actor_name or "").strip()
    candidate.discard_reason = reason
    candidate.save()
    return candidate


def expire_candidates(*, now: datetime | None = None) -> int:
    """把过了失效期的候选标成 `discarded`，返回条数。**不删除任何行。**

    CONTEXT.md 第 37 条：到期未确认即丢弃并记日志。第 152 条又要求拿「提过、没人理」
    当覆盖率衰减的证据——「这条建议提过」本身就是证据，删掉就等于把「机制提过但没人看」
    这件事抹了。

    `decided_by` 留空是**有意的**：它记录的是「没有人处置」，而 `discarded` +
    空处置人 + `expired` 三个字段一起，才读得出「机制提过、没人理」。

    这是本模块唯一的非人工写入，理由见模块 docstring：它不改变任何事实。
    """
    now = now or django_timezone.now()
    stale = list(
        CandidateEvent.objects.filter(
            status=CandidateStatus.PENDING.value, expires_at__lte=now
        ).values_list("id", "name", "raised_at")
    )
    if not stale:
        return 0

    # 更新时再查一次 `status=pending`：两次读之间可能有人刚把它转正了，那时以先到者
    # 为准——转正是终态，不能被一次到期的清理顺手改回去。
    updated = CandidateEvent.objects.filter(
        id__in=[row[0] for row in stale], status=CandidateStatus.PENDING.value
    ).update(
        status=CandidateStatus.DISCARDED.value,
        decided_at=now,
        discard_reason=CANDIDATE_DISCARD_EXPIRED,
    )
    for candidate_id, name, raised_at in stale:
        logger.info(
            "候选事件到期未确认，已丢弃：#%s「%s」（%s 提出）",
            candidate_id,
            name,
            raised_at,
        )
    return updated


# --------------------------------------------------------------------------- #
# 回显：录入端是人核对输入的唯一机会
# --------------------------------------------------------------------------- #


def describe_event(event: MajorEvent, *, now: datetime | None = None) -> str:
    """一条事件的回显。**两个绝对时刻都要写出来**（CONTEXT.md 第 149 条）。"""
    now = now or django_timezone.now()
    lines = [
        f"#{event.id} {event.name}（{event.impact_display} / {event.status_display}）",
        f"  事件时刻：{format_moment(event.event_time)}",
        f"  熔断窗口：{format_moment(event.halt_at)} → {format_moment(event.resume_at)}",
        f"  作用域：{event.scope_display}",
    ]
    if event.status == EventStatus.CANCELLED.value:
        lines.append("  已取消：本条不产生任何熔断")
    elif not EventImpact(event.impact).triggers_halt:
        lines.append(f"  冲击档位为「{event.impact_display}」，只有「高」会触发熔断")
    elif event.resume_at <= now:
        # 「录了一条已经过去的事件」是人工维护最可能的空转形状，所以要在这里就说出来
        # ——不说的表现是「录进去了但什么都没发生」，而它与「今天没有事件」长得一样。
        lines.append("  ⚠ 该窗口已整体过去，本条不会产生任何熔断")
    elif event.halt_at <= now:
        lines.append("  ⚠ 该窗口正在进行中")
    if event.note:
        lines.append(f"  备注：{event.note}")
    return "\n".join(lines)


def describe_candidate(
    candidate: CandidateEvent, *, now: datetime | None = None
) -> str:
    """一条候选的回显。**必须与已入库事件在结构上可分辨**（CONTEXT.md 第 94 条）：

    这里没有「熔断窗口」这一行，而 `describe_event` 一定有——候选不产生熔断，读的人
    不该靠自己去记住哪张表带窗口。
    """
    now = now or django_timezone.now()
    guessed = (
        format_moment(candidate.guessed_time)
        if candidate.guessed_time
        else "未定（资讯里没有具体钟点）"
    )
    lines = [
        f"候选 #{candidate.id} {candidate.name}"
        f"（{candidate.origin} / {candidate.status_display}）",
        f"  推测时刻：{guessed}",
        f"  提出：{candidate.raised_at} 由 {candidate.raised_by}",
        "  无熔断窗口——候选不产生任何动作，转正需人工重新给出时间/档位/作用域",
    ]
    if candidate.is_pending and candidate.is_expired(now):
        lines.append(
            f"  ⚠ 已过失效期（{format_moment(candidate.expires_at)}），"
            "等下一次定时清理落成「已丢弃」——一直没有落成，说明清理任务自己没在跑"
            "（见 /regime mech 的「调度表（beat）」）"
        )
    elif candidate.is_pending:
        lines.append(f"  失效于：{format_moment(candidate.expires_at)}")
    elif candidate.discard_reason:
        lines.append(
            f"  处置：{candidate.discard_reason_display}"
            f"（{candidate.decided_by or '无人处置'}）"
        )
    if candidate.note:
        lines.append(f"  依据：{candidate.note}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 内部
# --------------------------------------------------------------------------- #


def _validate_impact(impact: str) -> str:
    if impact not in {m.value for m in EventImpact}:
        raise EventInputError(
            f"未知的冲击档位：{impact!r}，只能是 "
            f"{'、'.join(f'{m.value}（{m.display}）' for m in EventImpact)}"
        )
    return impact


def _ensure_human(actor_kind: str, action: str) -> None:
    """「必须人工」在代码里就是这一条：定时任务不能自己把东西升成「高」。

    `cli` 与 `chat` 都算人——它们的 `actor_name` 分别来自 `getpass.getuser()` 与平台
    sender id，区别只在于「谁干的」用哪套标识，可信度上都是「有人敲了这条命令」。
    只有 `task` 不是。
    """
    if actor_kind == ActorKind.TASK.value:
        raise EventInputError(
            f"「{action}」必须由人确认：定时任务不能自行升档"
            "（CONTEXT.md 第 149 条：提升为『高』必须人工）"
        )


def _ensure_scheduled(event: MajorEvent, action: str) -> None:
    if event.status == EventStatus.CANCELLED.value:
        raise EventInputError(
            f"已取消的事件不能{action}：取消是终态，要复活请重新录一条"
            "（CONTEXT.md 第 154 条不允许「只改期不删除」这类半途状态）"
        )


def _event_snapshot(event: MajorEvent) -> dict:
    """落进流水的那一份状态。只含会被维护动作改动的键。"""
    return {
        "name": event.name,
        "event_time": _iso(event.event_time),
        "impact": event.impact,
        "scope_kind": event.scope_kind,
        "symbols": list(event.symbols or []),
        "halt_at": _iso(event.halt_at),
        "resume_at": _iso(event.resume_at),
        "status": event.status,
    }


def _diff(before: dict, after: dict) -> dict[str, dict]:
    """只留**被改动的键**，返回 `{"before": ..., "after": ...}`。

    整行快照会让「这条流水改了什么」需要读者自己对照两行 JSON 求差，而求差的那个读者
    正是这条流水存在的理由。
    """
    keys = [key for key in after if before.get(key) != after[key]]
    return {
        "before": {key: before.get(key) for key in keys},
        "after": {key: after[key] for key in keys},
    }


def _log_change(
    event: MajorEvent,
    kind: EventChangeKind,
    *,
    before: dict,
    after: dict,
    actor_kind: str,
    actor_name: str,
    note: str,
    at: datetime,
) -> MajorEventChange:
    clean_actor = (actor_name or "").strip()
    if not clean_actor:
        # 「谁干的」是这条流水的一半，空着的话它退化成一条只有时间的日志。
        raise EventInputError("维护动作必须记下触发方（命令行用户名 / 平台 sender id）")
    return MajorEventChange.objects.create(
        event=event,
        kind=kind.value,
        at=at,
        actor_kind=actor_kind,
        actor_name=clean_actor,
        before=before,
        after=after,
        note=note or "",
    )


def _confirm_note(candidate: CandidateEvent, note: str) -> str:
    origin = (
        f"由候选 #{candidate.id} 转正"
        f"（{CandidateOrigin(candidate.origin).display}，{candidate.raised_by} 于 "
        f"{candidate.raised_at} 提出）"
    )
    return f"{origin}；{note}" if note else origin


def _iso(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    return to_business(moment).astimezone(timezone.utc).isoformat()
