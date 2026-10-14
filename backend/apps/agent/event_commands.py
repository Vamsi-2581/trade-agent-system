"""事件维护的斜杠命令（第①段单元 8ii）。

CONTEXT.md 第 152 条：**全仓不引入 Django admin**，事件的人工维护入口一律走 slash
命令。本模块是那套命令的实现，`SupervisorAgent.handle()` 在最前面把命令截下来交给
这里——**不经过 LLM**。这不是实现细节：`events.py` 里那条「提升为『高』必须人工」的
判据认的是 `ActorKind`，而只要命令分发发生在意图解析之前，「谁敲的」就还是人。

## 语法

    /event add    <档位> <作用域> <时刻> <名称…>       [--停前 N] [--停后 N] [--备注 …]
    /event impact <编号> <档位>                        [--备注 …]
    /event reschedule <编号> <时刻>                    [--停前 N] [--停后 N] [--备注 …]
    /event cancel <编号> <理由…>
    /event confirm <候选编号> <档位> <作用域> <时刻>    [--停前 N] [--停后 N] [--备注 …]
    /event reject <候选编号>
    /event list   [天数]

档位收「高 / 中 / 低」（也认 high / medium / low）。作用域是**一个词**：`全市场`，
或逗号分隔的品种列表 `SOL/USDT,BTC/USDT`——与下单同一套口径，多写一个结算后缀会被
`events.validate_symbols` 直接拒掉。时刻写「2026-09-25 20:30」，按北京时间解释。

## 为什么要参数、为什么不引进参数解析库

CONTEXT.md 第 119 条把「修正 Telegram 斜杠通路」列为 v1 前置，而那条通路原本只做
`split()[0]`：命令词之后的东西全被丢掉。这里因此手写一个小解析器，规则只有三条——
`--` 开头的词是选项、选项吃掉紧跟的一个词（`--备注` 吃到最后）、剩下的按位置读。
绝不静默忽略多余的词：多给一个词就报错，因为「我明明写了却什么都没发生」在事件维护
里等于一条以为录进去了、实际不存在的熔断窗口。

## 报错一律是用户反馈

`EventInputError` 是「你给的东西不对」，不是故障（见 `events.py` 的类 docstring）。
这里把它原样接住、当成命令的回复打出去，**而且走 `success=True`**——消费端把
`success=False` 渲染成 `[错误] …`，打错一个字与系统坏掉在用户眼里会长得一样。
**其余异常照旧往上抛**给任务健康检查；合并的话，一次手滑和一次数据库故障同样分不开。

## 这一版不做的事

* **不给用户发即时通知。** 第①段零执行，窗口不会真的停掉任何东西，「受影响用户」
  因此为空集；改期/取消要通知受影响用户，是第②段接线时才成立的硬要求。
* **不收否决的自由文本理由。** `discard_candidate()` 的 `reason` 是一个**代码**
  （`rejected` / `expired`），候选表上也没有承载理由正文的字段。这里宁可直接报错也
  不把人写的理由悄悄丢掉——真要留理由，那是给候选表加一列的事，不是顺手改的东西。
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

from asgiref.sync import sync_to_async
from django.db import close_old_connections
from django.utils import timezone as django_timezone

from apps.regime import events
from apps.regime.events import EventInputError
from apps.regime.models import (
    CANDIDATE_DISCARD_REJECTED,
    ActorKind,
    CandidateEvent,
    CandidateStatus,
    EventImpact,
    EventScope,
    EventStatus,
    MajorEvent,
)

from .base import AgentMessage, AgentResult

logger = logging.getLogger(__name__)

#: `query_events` 工具与日报第③段用的是同一个天数。这里也用它，好让「我在命令里查了
#: 没有」与「日报里没有」指的是同一件事。
DEFAULT_HORIZON_DAYS = 7

_USAGE = (
    "重大事件维护命令：\n"
    "  /event add    <档位> <作用域> <时刻> <名称…>     录入一条事件\n"
    "  /event impact <编号> <档位>                      改档（升为「高」必须人工）\n"
    "  /event reschedule <编号> <时刻>                  改期\n"
    "  /event cancel <编号> <理由…>                      取消\n"
    "  /event confirm <候选编号> <档位> <作用域> <时刻>  候选转正\n"
    "  /event reject <候选编号>                          否决候选\n"
    "  /event list   [天数]                             看现有编号\n"
    "\n"
    "档位：高 / 中 / 低（只有「高」会触发熔断）\n"
    "作用域：全市场，或逗号分隔的品种列表（如 SOL/USDT,BTC/USDT，与下单同一套写法）\n"
    "时刻：如 2026-09-25 20:30，一律按北京时间解释\n"
    "窗口可在 add / reschedule / confirm 上用 --停前 N / --停后 N 覆盖（单位分钟）"
)

_ADD_USAGE = (
    "用法：/event add <档位> <作用域> <时刻> <名称…> [--停前 N] [--停后 N] [--备注 …]"
)
_IMPACT_USAGE = "用法：/event impact <编号> <档位> [--备注 …]"
_RESCHEDULE_USAGE = (
    "用法：/event reschedule <编号> <时刻> [--停前 N] [--停后 N] [--备注 …]"
)
_CANCEL_USAGE = "用法：/event cancel <编号> <理由…>"
_CONFIRM_USAGE = "用法：/event confirm <候选编号> <档位> <作用域> <时刻> [--停前 N] [--停后 N] [--备注 …]"
_REJECT_USAGE = "用法：/event reject <候选编号>"
_LIST_USAGE = "用法：/event list [天数]"

#: 选项词 → `resolve_window` 的关键字。只作用于窗口，因为窗口是唯一「允许单事件覆盖」
#: 的量（CONTEXT.md 第 147 条），其余字段要么必填、要么全局统一。
_SPAN_FLAGS = {"--停前": "halt_before_minutes", "--停后": "resume_after_minutes"}

#: 吃到最后的一个选项：名称/理由里可能有空格，用位置读会把它们切成两半。
_NOTE_FLAG = "--备注"

#: 「2026-09-25」后面紧跟的那一个词，用来把分开写的时刻接回去。
_TIME_TAIL_RE = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?$")

#: 品种列表的分隔符。全角的也认——中文输入法下逗号是随手打出来的那一个。
_SYMBOL_SEPARATORS = re.compile(r"[,，、\s]+")

#: 成对的引号。**只收这四对**，不含 `「」`/`『』`：那两对是本项目消息里自己的标点，
#: 一条名叫「XX 上线」的事件剥掉引号会真的改掉名字，而 ASCII 引号几乎只可能是
#: 手滑打出来的包裹。
_QUOTE_PAIRS = {'"': '"', "'": "'", "“": "”", "‘": "’"}


def _unquote(token: str) -> str:
    """剥掉成对的引号——两端成套时才剥，词中间的撇号（`don't`）不动。"""
    if len(token) >= 2 and _QUOTE_PAIRS.get(token[0]) == token[-1]:
        return token[1:-1]
    return token


# --------------------------------------------------------------------------- #
# 词法
# --------------------------------------------------------------------------- #


def _split_flags(tokens: list[str]) -> tuple[list[str], dict]:
    """把选项从参数里摘出来，返回 `(位置参数, 关键字)`。

    `--备注` 之后的词全归它，所以「名称里含 `--备注`」这种写法是不成立的——名称里带
    双横线的现实概率远低于「备注里带空格」，把这一侧的代价留给前者。
    """
    positional: list[str] = []
    flags: dict = {}

    index = 0
    while index < len(tokens):
        token = tokens[index]

        field = _SPAN_FLAGS.get(token)
        if field is not None:
            if index + 1 >= len(tokens):
                raise EventInputError(f"{token} 后面要跟一个分钟数，例如 {token} 120")
            raw = tokens[index + 1]
            try:
                flags[field] = int(raw)
            except ValueError:
                raise EventInputError(
                    f"{token} 的值必须是整数分钟，当前是 {raw!r}"
                ) from None
            index += 2
            continue

        if token == _NOTE_FLAG:
            flags["note"] = " ".join(tokens[index + 1 :]).strip()
            break

        positional.append(token)
        index += 1

    return positional, flags


def _parse_impact(token: str) -> str:
    """档位词 → `EventImpact` 的值。收口语（高/中/低）也收英文值。"""
    for member in EventImpact:
        if token == member.display or token.lower() == member.value:
            return member.value
    raise EventInputError(
        f"未知的冲击档位：{token!r}，只能是 "
        + "、".join(f"{m.display}（{m.value}）" for m in EventImpact)
        + "。只有「高」会触发熔断"
    )


def _parse_scope(token: str) -> tuple[str, list[str]]:
    """作用域词 → `(scope_kind, symbols)`。

    作用域是**一个词**：写成 `指定品种` 而不带品种时直接报错，而不是留空让它退化成
    「全市场」——那个方向恰好是最贵的一种猜错（CONTEXT.md 第 147 条）。
    """
    if token == EventScope.MARKET.display or token.lower() == EventScope.MARKET.value:
        return EventScope.MARKET.value, []
    if token == EventScope.SYMBOLS.display or token.lower() == EventScope.SYMBOLS.value:
        raise EventInputError(
            "作用域写成「指定品种」时要把品种写在同一个词里：\n"
            "  指定品种：SOL/USDT,BTC/USDT\n"
            "  全市场：全市场"
        )
    return EventScope.SYMBOLS.value, [
        item for item in _SYMBOL_SEPARATORS.split(token) if item
    ]


def _take_time(tokens: list[str], index: int) -> tuple[datetime, int]:
    """从 `index` 处读一个事件时刻，返回 `(utc 时刻, 吃掉的词数)`。

    时刻是唯一允许**跨两个词**的字段（「2026-09-25 20:30」），因为把日期与钟点分开
    写是命令行里的习惯。写法校验一律交给 `events.parse_business_time`：那里对「带
    时区偏移」「不是合法日期」各有各的报错，在这里再判一次等于把同一套规则写两遍。
    """
    if index >= len(tokens):
        raise EventInputError("缺少事件时刻")

    head = tokens[index]
    tail = tokens[index + 1] if index + 1 < len(tokens) else ""
    if _TIME_TAIL_RE.match(tail):
        return events.parse_business_time(f"{head} {tail}"), 2
    return events.parse_business_time(head), 1


def _parse_pk(token: str, label: str) -> int:
    """编号收 `#3` 与 `3` 两种写法——回显里带 `#`，人照着抄的正是那个样子。"""
    try:
        return int(str(token).lstrip("#"))
    except ValueError:
        raise EventInputError(f"{label}编号必须是数字，当前是 {token!r}") from None


def _require_event(token: str) -> MajorEvent:
    pk = _parse_pk(token, "事件")
    event = MajorEvent.objects.filter(pk=pk).first()
    if event is None:
        raise EventInputError(f"找不到编号为 {pk} 的事件。先跑 /event list 看编号。")
    return event


def _require_candidate(token: str) -> CandidateEvent:
    pk = _parse_pk(token, "候选")
    candidate = CandidateEvent.objects.filter(pk=pk).first()
    if candidate is None:
        raise EventInputError(
            f"找不到编号为 {pk} 的候选事件。先跑 /event list 看编号。"
        )
    return candidate


def _reject_extras(extra: list[str], usage: str) -> None:
    """多给的词一律报错，不静默忽略。

    「我明明写了却什么都没发生」在事件维护里最坏：一条以为录进去了、实际不存在的熔断
    窗口，要等到那天什么都没发生才会被发现——而那天看起来与「今天没有事件」一样。
    """
    if extra:
        raise EventInputError(f"多出来的参数：{' '.join(extra)}\n{usage}")


def _reject_spans(flags: dict, action: str) -> None:
    """把改档路径上的 `--停前 / --停后` 顶回去。

    不是洁癖：`change_impact` 的签名里根本没有这两个参数，放过它们会变成一次
    `TypeError`，被当成故障上报，而人看到的只是自己多打了一个词。顺带改窗口这件事
    也确实不该挂在改档上——窗口锚在**事件时刻**上，与档位无关（CONTEXT.md 第 147 条
    只允许「单事件覆盖」，入口是录入与改期）。
    """
    spans = [flag for flag, field in _SPAN_FLAGS.items() if field in flags]
    if spans:
        raise EventInputError(
            f"「{action}」不能顺带改窗口：{'、'.join(spans)}。"
            "窗口锚在事件时刻上、与档位无关，要调窗口请用 /event reschedule。"
        )


# --------------------------------------------------------------------------- #
# 子命令。签名统一是同步的 `(message, tokens) -> str`：同步跑，调用方
# `handle_event_command` 用 `sync_to_async` 把它挪到线程里，ORM 因此永远不在
# async 上下文里执行（CLAUDE.md 的硬规则）。
# --------------------------------------------------------------------------- #


def _add(message: AgentMessage, tokens: list[str]) -> str:
    positional, flags = _split_flags(tokens)
    if len(positional) < 4:
        raise EventInputError(f"参数不够。\n{_ADD_USAGE}")

    impact = _parse_impact(positional[0])
    scope_kind, symbols = _parse_scope(positional[1])
    event_time, used = _take_time(positional, 2)

    name = " ".join(positional[2 + used :]).strip()
    if not name:
        raise EventInputError(f"事件名称不能为空。\n{_ADD_USAGE}")

    event = events.create_event(
        name=name,
        event_time=event_time,
        impact=impact,
        scope_kind=scope_kind,
        symbols=symbols,
        actor_kind=ActorKind.CHAT.value,
        actor_name=message.user_id,
        **flags,
    )
    return "已录入。\n" + events.describe_event(event)


def _impact(message: AgentMessage, tokens: list[str]) -> str:
    positional, flags = _split_flags(tokens)
    if len(positional) < 2:
        raise EventInputError(f"参数不够。\n{_IMPACT_USAGE}")

    event = _require_event(positional[0])
    impact = _parse_impact(positional[1])
    _reject_extras(positional[2:], _IMPACT_USAGE)
    _reject_spans(flags, "改档")

    events.change_impact(
        event,
        impact=impact,
        actor_kind=ActorKind.CHAT.value,
        actor_name=message.user_id,
        **flags,
    )
    return "已改档。\n" + events.describe_event(event)


def _reschedule(message: AgentMessage, tokens: list[str]) -> str:
    positional, flags = _split_flags(tokens)
    if len(positional) < 2:
        raise EventInputError(f"参数不够。\n{_RESCHEDULE_USAGE}")

    event = _require_event(positional[0])
    event_time, used = _take_time(positional, 1)
    _reject_extras(positional[1 + used :], _RESCHEDULE_USAGE)

    events.reschedule_event(
        event,
        event_time=event_time,
        actor_kind=ActorKind.CHAT.value,
        actor_name=message.user_id,
        **flags,
    )
    return (
        "已改期。**窗口内改期不会中止已经开启的窗口**，新时间从下一个窗口起生效。\n"
        + events.describe_event(event)
    )


def _cancel(message: AgentMessage, tokens: list[str]) -> str:
    if len(tokens) < 2:
        raise EventInputError(f"取消必须给出理由。\n{_CANCEL_USAGE}")
    # 这里不摘选项：理由是一段自由文本，`--停前 30` 出现在句子里是正常的中文。
    event = _require_event(tokens[0])
    reason = " ".join(tokens[1:]).strip()

    events.cancel_event(
        event,
        reason=reason,
        actor_kind=ActorKind.CHAT.value,
        actor_name=message.user_id,
    )
    return "已取消。\n" + events.describe_event(event)


def _confirm(message: AgentMessage, tokens: list[str]) -> str:
    positional, flags = _split_flags(tokens)
    if len(positional) < 4:
        raise EventInputError(f"参数不够。\n{_CONFIRM_USAGE}")

    candidate = _require_candidate(positional[0])
    impact = _parse_impact(positional[1])
    scope_kind, symbols = _parse_scope(positional[2])
    event_time, used = _take_time(positional, 3)
    _reject_extras(positional[3 + used :], _CONFIRM_USAGE)

    # 名称继承候选，时间/档位/作用域由人重说一遍——`confirm_candidate` 只收后三样，
    # 所以这条约束在类型上就成立，这里不重复校验。
    event = events.confirm_candidate(
        candidate,
        event_time=event_time,
        impact=impact,
        scope_kind=scope_kind,
        symbols=symbols,
        actor_kind=ActorKind.CHAT.value,
        actor_name=message.user_id,
        **flags,
    )
    return "候选已转正。\n" + events.describe_event(event)


def _reject(message: AgentMessage, tokens: list[str]) -> str:
    if len(tokens) < 1:
        raise EventInputError(f"参数不够。\n{_REJECT_USAGE}")
    # 候选表的「否决」只有处置结果、没有理由正文（见模块 docstring）。多写的词直接
    # 报错退回，好过人以为它被记下了。
    _reject_extras(tokens[1:], _REJECT_USAGE)

    candidate = _require_candidate(tokens[0])
    events.discard_candidate(
        candidate,
        reason=CANDIDATE_DISCARD_REJECTED,
        actor_name=message.user_id,
    )
    return "已否决。\n" + events.describe_candidate(candidate)


def _list(message: AgentMessage, tokens: list[str]) -> str:
    if len(tokens) > 1:
        raise EventInputError(f"多出来的参数：{' '.join(tokens[1:])}\n{_LIST_USAGE}")

    days = DEFAULT_HORIZON_DAYS
    if tokens:
        try:
            days = int(tokens[0])
        except ValueError:
            raise EventInputError(f"天数必须是整数，当前是 {tokens[0]!r}") from None
        if days < 1:
            raise EventInputError(f"天数必须至少是 1，当前是 {days}")

    now = django_timezone.now()
    until = now + timedelta(days=days)
    upcoming = list(
        MajorEvent.objects.filter(
            status=EventStatus.SCHEDULED.value,
            event_time__lte=until,
            resume_at__gte=now,
        ).order_by("event_time", "id")
    )
    pending = list(
        CandidateEvent.objects.filter(status=CandidateStatus.PENDING.value).order_by(
            "-raised_at", "-id"
        )
    )

    lines = [f"未来 {days} 天内、尚未整体过去的事件：{len(upcoming)} 条"]
    lines += [events.describe_event(event, now=now) for event in upcoming]
    if not upcoming:
        lines.append("  （没有）")
    lines.append("")
    lines.append(f"待人工处置的候选事件：{len(pending)} 条（不产生任何熔断）")
    lines += [events.describe_candidate(item, now=now) for item in pending]
    if not pending:
        lines.append("  （没有）")
    lines.append("")
    lines.append(
        "操作：/event impact <编号> <档位>、/event reschedule <编号> <时刻>、"
        "/event cancel <编号> <理由>、/event confirm <候选编号> …、"
        "/event reject <候选编号>"
    )
    return "\n".join(lines)


_SUBCOMMANDS = {
    "add": _add,
    "impact": _impact,
    "reschedule": _reschedule,
    "cancel": _cancel,
    "confirm": _confirm,
    "reject": _reject,
    "list": _list,
}


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #


async def handle_event_command(message: AgentMessage, args: str) -> AgentResult:
    """`/event …` 的入口。`args` 是命令词之后的那一段（可能为空）。"""
    actor = (message.user_id or "").strip()
    if not actor:
        # 先拦一道，好让报错说的是「你是谁没认出来」而不是 `_log_change` 那句更泛的
        # 「必须记下触发方」。事件维护的留痕认的是这个人，认不出就不该往下走。
        return AgentResult(
            task_id=message.task_id,
            success=False,
            error="无法确定操作者身份，事件维护必须记下是谁操作的（聊天渠道的 sender id）",
        )

    tokens = [_unquote(token) for token in args.split()]
    if not tokens:
        return AgentResult(task_id=message.task_id, success=True, data=_USAGE)

    subcommand = tokens[0].lower()
    handler = _SUBCOMMANDS.get(subcommand)
    if handler is None:
        return AgentResult(
            task_id=message.task_id,
            success=True,
            data=f"未知的事件子命令：{subcommand}\n\n{_USAGE}",
        )

    def _work() -> str:
        # 长驻进程里的老连接先收掉再干活（Django async 上下文里默认拿不到可用的连接）。
        close_old_connections()
        return handler(message, tokens[1:])

    try:
        text = await sync_to_async(_work)()
    except EventInputError as exc:
        # 「你给的东西不对」是命令的正常回复，不是故障。
        #
        # 走 success=True 而不是 error=：消费端把 `success=False` 渲染成
        # `[错误] …`（`apps/agent/consumer.py`），而打字打错了与「系统坏了」在用户
        # 眼里会长得一样。其余异常照旧往上抛给任务健康检查——那才是真故障。
        logger.info("[event] 命令被拒：%s", exc)
        return AgentResult(task_id=message.task_id, success=True, data=str(exc))

    logger.info("[event] %s by %s", subcommand, actor)
    return AgentResult(task_id=message.task_id, success=True, data=text)
