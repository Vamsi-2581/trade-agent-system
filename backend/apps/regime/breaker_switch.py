"""事件熔断开关的**上线确认页**与它的写方（第②段单元 ②f）。

CONTEXT.md 第 169 条把上线这一步的要求写死了：**确认那一步必须显式回显「未来 14 天
高影响事件：N 条」，N=0 时给出明确警告，但不阻止上线**。第 160 条则说「出 Shadow 切到
执行态必须人工确认，不自动切换」。本模块就是那一步：把「打开之后到底会不会拦住东西」
算出来给人看，并且在人点头时落下 `RegimeMechanismSwitch` 的那一行。

## 为什么确认页与写方在同一个模块里

因为**写必须经过那一页**。落一条 `to_mode=executing` 的流水时，`reason` 里要写清
「打开的时候前面是什么」（Q8 的摘要就是从这一页算出来的数），而如果两个入口各自渲染一遍
这一页，那么「确认页说 N=0、流水里记着 N=3」这种分歧是可能出现的，且没有任何东西会红。
同一份计算供三个地方用：slash 命令的回显、管理命令的回显、流水的 `reason`。

## 两个数，不是一个

「未来 14 天」这句话在熔断窗口上可以有两种读法，而它们的差在真实数据上很大（一场
跨周末的事件、一条从现在起算第 13 天开启的窗口）。所以两个都报、分别标名：

- **熔断窗口与这段相交的**（``overlapping``）——包住「此刻正压在窗口里」那种。
- **未来 14 天内开启的窗口**（``starting``）——只看窗口起点。

判断「会不会空转」用的是第一个：一条此刻开着的窗口当然拦得住东西。

## 天数与日报第③段的 7 天**刻意不同**（第 183 条）

那是天天要读的简报，这是一个人一辈子点几次的体检；同一个数被拿去做两件事，两边就都
没法各自调。所以它取 `config.EVENTS.confirm_horizon_days`（14），不是
`config.REPORT.event_horizon_days`。

## 这一版不做的事

* **不写停止声明表。** 窗口同步是 `halt_sync` 的事，它「写与开关无关」；开关只决定
  「拦不拦」。所以打开这个开关不会凭空多出窗口，关掉它也不会抹掉已经写好的声明。
* **不自动开关。** 唯一的写方是两个人工入口（`/regime` 与 `manage.py event_breaker`）。
  自熔断退回 Shadow 之后再回执行态，必须由人重新走一遍这一步（CONTEXT.md 第 161 条）。
* **不碰另外两个开关。** `MECHANISM` 与 `REGIME_GATE` 各自由它们自己的入口管
  （第③段），`kind` 在这里是写死的。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.utils import timezone

from apps.regime import config, events, halt_sync, report
from apps.regime.models import (
    ActorKind,
    MechanismKind,
    MechanismMode,
    RegimeMechanismSwitch,
)

logger = logging.getLogger(__name__)

#: 本模块唯一管得着的那个开关。写成常量而不是参数：②f 只上线事件熔断，
#: 把它做成参数就是在邀请下一个调用点顺手打开一个还没接线的开关。
KIND = MechanismKind.EVENT_BREAKER


@dataclass(frozen=True)
class Confirmation:
    """上线确认页的那几个数。**纯值对象**，渲染在外面，测试可以直接断言数字。"""

    now: datetime
    until: datetime
    horizon_days: int
    #: 档位「高」的事件总条数（**不论状态**，含已取消的）。
    library_total: int
    #: 其中会真的开出窗口的（`MajorEvent.triggers_halt`）。
    firing_total: int
    #: 熔断窗口与 `[now, until)` 相交的条数。
    overlapping: int
    #: 窗口起点落在 `[now, until)` 的条数。
    starting: int
    #: 此刻正压在窗口里的那条（`halt_at <= now < resume_at`）。
    open_now: object | None
    #: 库里离 `now` 最近的一条未来窗口（`halt_at > now`），没有则 `None`。
    next_event: object | None
    #: 会开窗的那些里、窗口起点离 `now` 最近的一条（过去或未来），给「平静期」那句用。
    nearest: object | None
    #: 事件库最近一次入库距今多少天；从未录入过为 `None`。
    staleness_days: int | None
    #: 求值那一刻的档位。**由 `confirmation()` 记下来，渲染层不再各读一次**：正文印
    #: 「当前档位」，而 `closing_summary` 印的是「关掉的那一刻」——两处各读一次的话，
    #: 中间落一次切换就会让同一份快照说出两个档位。一次求值里只读一次，值随快照走。
    mode: MechanismMode


def confirmation(*, now: datetime | None = None) -> Confirmation:
    """把确认页要的数算出来。**只读**。

    高影响事件取 ``halt_sync.high_impact_events()``（公开、不按状态过滤）之后再用
    ``triggers_halt`` 过一遍，而不是自己写 ``filter(status=SCHEDULED, impact=HIGH)``：
    「已取消的事件不产生窗口」这条规则只有一处实现，第二个 filter 就是它的一份复制品。
    不过滤状态也是刻意的——「库里共有 N 条」那句要说的是**录了多少**，而一条已经取消
    的高影响事件仍然是录过的情报。
    """
    at = now or timezone.now()
    horizon_days = config.EVENTS.confirm_horizon_days
    until = at + timedelta(days=horizon_days)

    library = halt_sync.high_impact_events()
    firing = [event for event in library if event.triggers_halt]

    overlapping = [e for e in firing if e.halt_at < until and e.resume_at > at]
    starting = [e for e in firing if at <= e.halt_at < until]
    open_now = next((e for e in firing if e.halt_at <= at < e.resume_at), None)
    next_event = next((e for e in firing if e.halt_at > at), None)
    nearest = min(
        firing, key=lambda e: abs((e.halt_at - at).total_seconds()), default=None
    )

    _, age = report.event_library_staleness(now=at)

    return Confirmation(
        now=at,
        # 档位与其它数取自同一次求值。
        mode=RegimeMechanismSwitch.current(KIND),
        until=until,
        horizon_days=horizon_days,
        library_total=len(library),
        firing_total=len(firing),
        overlapping=len(overlapping),
        starting=len(starting),
        open_now=open_now,
        next_event=next_event,
        nearest=nearest,
        staleness_days=age,
    )


# --------------------------------------------------------------------------- #
# 渲染。空库那两句刻意与 `halt_notify.write_failure_body` 同一种写法：
# 说的是**后果**（打开之后会怎样），而不是「没有数据」。
# --------------------------------------------------------------------------- #


def empty_library_warning() -> str:
    """库里一条高影响事件都没有时的那段话（CONTEXT.md 第 169 条的「N=0 必须被看见」）。

    必须把后果说完：这个开关的作用是让下单通路在窗口内拦人，而**空库打开它不会产生
    任何可观察的差别**——日报照常、通知照常、日志照常。一个「装了但没有用」的熔断与
    一个「装了且正常待命」的熔断，从外面看是同一个东西，所以这句话只能在打开的那一刻说。
    """
    return (
        "⚠️ 事件库里一条高影响事件都没有：这个开关打开之后，事件熔断层**不会拦住任何"
        "东西**，而且看起来与正常运行完全一样——日报、通知、日志里都不会出现任何异常。\n"
        "  先录一条再打开：/event add 高 全市场 <时刻> <名称>\n"
        "  （也可以先不打开。这不是错误，只是你要知道打开的是什么。）"
    )


def _quiet_horizon_line(data: Confirmation) -> str:
    """库里还有事件、但 14 天内一条窗口都没有。**句子里必须带上「库里还有」。**

    只说「14 天内 0 条」会与空库读起来一模一样，而两者的含义差了十万八千里：一个是
    「设备没接上」，一个是「这段确实平静」。所以平静的那一档要把「库里有什么、最近一次
    是什么时候」一并说出来，好让人自己判断这个平静可不可信。
    """
    text = (
        f"{data.horizon_days} 天内没有任何窗口开启，但事件库里还有 {data.library_total} 条"
        f"高影响事件（其中 {data.firing_total} 条会真的开窗"
    )
    if data.nearest is not None:
        text += (
            f"；离现在最近的一条是「{data.nearest.name}」，"
            f"窗口起点 {events.format_moment(data.nearest.halt_at)}"
        )
    text += "）。平静期本来就会这样；如果不是，用 /event list 核对一下。"
    return text


def _window_line(data: Confirmation) -> str:
    """此刻在窗口里 / 下一次窗口什么时候 / 一条都没有——三选一，必须说满。"""
    if data.open_now is not None:
        return (
            f"⏳ 此刻正压在「{data.open_now.name}」的熔断窗口里："
            f"{events.format_moment(data.open_now.halt_at)} → "
            f"{events.format_moment(data.open_now.resume_at)}"
        )
    if data.next_event is not None:
        beyond = data.next_event.halt_at >= data.until
        prefix = f"下次窗口在 {data.horizon_days} 天之外" if beyond else "下次窗口"
        return (
            f"{prefix}：{events.format_moment(data.next_event.halt_at)}"
            f"（{data.next_event.name}）"
        )
    return "事件库里没有未开启的窗口（全部已成过去，或一条都没录）"


#: 上线前必须已经接通的能力（第②段 ②a 逐个核过，这里只如实回显）。
#: 写下来的理由与 `write_failure_body` 一样：这三件事是「熔断会不会把仓越减越大」
#: 的答案，而它们的状态只存在于代码里，不看一眼代码就答不出来。
PREREQUISITES = (
    "减仓只减不增（reduce_only 硬前置）",
    "确定性 client_order_id（同一次减仓重跑不会下第二遍）",
    "未成交开仓挂单的撤销（先撤后减，串行）",
)


@dataclass(frozen=True)
class Briefing:
    """一次快照 + 由它渲染出的两段文字。**三个字段同源**，见 `page`。"""

    data: Confirmation
    body: str
    summary: str


def page(*, now: datetime | None = None) -> Briefing:
    """上线确认页与它的摘要，**一次算出来**。

    两个出口只留这一个求值口，是因为「回显给人看的数」与「记进 `reason` 的数」必须是
    同一次快照。各算一遍的话，两次查询之间只要有人录了一条事件，确认页上就会写着
    「0 条」而流水里记着「1 条」——两个数都出自本模块，却互相矛盾。

    `data` 一并带出来，供「关掉」那条路复用同一份快照（`closing_summary`）。
    """
    data = confirmation(now=now)
    return Briefing(data=data, body=_render_body(data), summary=_render_summary(data))


def confirmation_body(*, now: datetime | None = None) -> str:
    """只要正文（测试与「只想看一眼」的调用方用）。"""
    return page(now=now).body


def confirmation_summary(*, now: datetime | None = None) -> str:
    """只要摘要。"""
    return page(now=now).summary


def _render_body(data: Confirmation) -> str:
    """上线确认页的正文。**只读，能重复跑多少遍都不改变任何东西。**

    之所以是一整页而不是一句话：打开这个开关之后，机制会在没有人的时候自动对市场动手
    （②d 的减仓执行器）。人在这之前要能一次看完「会不会拦住东西」「拦不住时的坏处」和
    「动手的能力接通了没有」——分成三条消息发，就会有人只看到其中一条。
    """
    at = data.now

    lines = [
        "事件熔断 · 上线确认（这一步本身不改变任何东西）",
        # 档位随快照走（`Confirmation.mode`），不在这里再读一次。
        f"当前档位：{data.mode.display}",
        f"未来 {data.horizon_days} 天（{events.format_moment(at)} 起，"
        f"截至 {events.format_moment(data.until)}）：",
        f"  熔断窗口与这段相交的高影响事件：{data.overlapping} 条",
        f"  未来 {data.horizon_days} 天内开启的窗口：{data.starting} 条",
        "  " + _window_line(data),
        report.event_library_line(now=at),
    ]

    if data.library_total == 0:
        lines.append(empty_library_warning())
    elif data.overlapping == 0:
        lines.append(_quiet_horizon_line(data))

    lines.append(
        "前置（上线前必须已接通，②a 逐个核过；这里只回显，不重复校验）：\n"
        + "\n".join(f"  · {item} —— 已接通" for item in PREREQUISITES)
    )
    lines.append(
        "操作：/regime on 打开事件熔断（先回显这一页，再落一条切换流水）\n"
        "      /regime off 关掉它（回到 Shadow；停止声明表不动，保命档不受影响）"
    )
    return "\n".join(lines)


def _render_summary(data: Confirmation) -> str:
    """进 `reason` 的那一行摘要（Q8）。**从同一份 `confirmation()` 算出来**。

    刻意是摘要而不是整页：`reason` 会被日报第④段和后续的排查读，一行能读完；整页正文
    只活在命令的那条回复里（那才是人当场读的东西）。天数若为 `None`（从没录过事件），
    如实写「从未录入」——写成 0 会让「空库」与「今天刚录过」看起来一样。
    """
    age = data.staleness_days
    age_text = "从未录入过事件" if age is None else f"最近入库距今 {age} 天"
    return (
        f"人工打开事件熔断（上线确认）：未来 {data.horizon_days} 天窗口相交 "
        f"{data.overlapping} 条 / {data.horizon_days} 天内开启 {data.starting} 条；"
        f"库里高影响事件 {data.library_total} 条（会开窗 {data.firing_total} 条）；{age_text}"
    )


def closing_summary(data: Confirmation) -> str:
    """关掉时进 `reason` 的摘要。**收已经算好的快照**，不自己再查一遍。

    与打开那份摘要问的不是同一件事：打开要回答「会不会拦住东西」（所以报条数与库况），
    关闭要回答「关掉的那一刻放掉了什么」——最要紧的是**此刻正压在窗口里**这一种。那种
    关法在外面看不出来：表里窗口还在、日报照旧，只是下单不再被拦。

    `reason` 会被日报第④段读，所以两个方向都写成一句话（不说「同上」）。
    """
    where = (
        f"关闭时正压在「{data.open_now.name}」的窗口内"
        if data.open_now is not None
        else "关闭时无窗口开启"
    )
    return (
        f"人工关闭事件熔断：窗口不再拦人（停止声明表未改动，保命档不受影响）；"
        f"关闭时未来 {data.horizon_days} 天窗口相交 {data.overlapping} 条；{where}；"
        f"库里高影响事件 {data.library_total} 条（会开窗 {data.firing_total} 条）"
    )


# --------------------------------------------------------------------------- #
# 写方：唯一的一次 INSERT
# --------------------------------------------------------------------------- #


def flip_event_breaker(
    to_mode: MechanismMode,
    *,
    actor_kind: ActorKind,
    actor_name: str,
    reason: str,
    now: datetime | None = None,
) -> RegimeMechanismSwitch | None:
    """把事件熔断开关切到 `to_mode`，返回落下的那一行；**已经是那一档则不写、返回 `None`**。

    两个刻意的取舍：

    - **`from_mode` 是真读出来的**（`current(KIND)`），不是写死 `shadow`。写死的话，
      「从执行态关掉、再打开」这一段在流水里会记成「从 Shadow 打开」，而这条流水正是
      事后回答「当时它到底开没开」的唯一材料。
    - **「已经是这一档」不写第二行。** 重复按一次 `on` 不该在流水里多出一条「executing →
      executing」：流水是只增不改的，一条无信息的行会永久留在那里，而「切过几次」从此
      多算一次。

    `reason` 与 `actor_name` 都**必填且拒绝空串**。模型的 `reason` 是 TextField（没有
    长度下限），漏填空串不会在数据库那一层被拦住，而一张「切了但不知道为什么」的流水
    与没有流水几乎等价——那正是留痕四件事里最贵的一件。
    """
    if not str(reason).strip():
        raise ValueError("切换原因不能为空：RegimeMechanismSwitch 的 reason 是必填的")
    if not str(actor_name).strip():
        raise ValueError("触发方不能为空：一次没人认领的开关切换无法排查")

    at = now or timezone.now()
    current = RegimeMechanismSwitch.current(KIND)
    if current is to_mode:
        logger.info("[regime] 事件熔断已经是 %s，未重复写流水", to_mode.value)
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
        "[regime] 事件熔断 %s → %s（%s：%s）",
        current.value,
        to_mode.value,
        row.actor_name,
        row.reason,
    )
    return row
