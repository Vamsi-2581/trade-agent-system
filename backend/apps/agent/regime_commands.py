"""行情阶段机制三个开关的斜杠命令（第②段单元 ②f → 第③段 W2 → 出 Shadow 那一档）。

CONTEXT.md 第 160 条：**出 Shadow 切到执行态必须人工确认，不自动切换**。本模块是那个
「人工」的入口，`SupervisorAgent.handle()` 在最前面把命令截下来交给这里——**不经过
LLM**。这与 `/event` 同一条理由：判据认的是人敲的，而只要分发发生在意图解析之前，
「谁敲的」就还是人（`ActorKind.CHAT` + 平台 sender id）。

## 语法

    /regime            事件熔断的上线确认页（只读，不改变任何东西）
    /regime on         打开事件熔断（先回显确认页，再落一条切换流水）
    /regime off        关掉事件熔断（回到 Shadow）
    /regime gate       行情阶段 gate 的上线确认页（只读）
    /regime gate on    打开行情阶段 gate（先回显确认页，再落流水 + 对一次账）
    /regime gate off   关掉行情阶段 gate（回到 Shadow）
    /regime exempt              人工恢复豁免的清单（只读，含「此刻算不算数」那一句）
    /regime exempt grant <策略> [阶段] [备注…]
                                发出一条豁免（阶段不给则取当前生效阶段）
    /regime exempt revoke <id> [id…]
                                按 id 收回（可逆：再发一条即可）
    /regime mech             机制整体的准入体检页（只读）
    /regime mech exit        出 Shadow：切到执行态（先回显体检页，再落一条切换流水）
    /regime mech back        退回 Shadow（只记录，不执行）
    /regime label            人工标注区间（真值）的清单与一致率（只读）
    /regime label add <起> <终> <档位> [备注…]
                             录入一段人工标注（成功标准①的比对基准）
    /regime label rm <id> [id…]
                             按 id 撤回（软删：行还在库里，原文不动）

`on` / `off` 也认「开 / 开启 / 打开 / open」与「关 / 关闭 / 关掉 / close」；豁免那一支的
三个动作也各有一套（`list` / 列 / 清单、`grant` / 发 / 发出、`revoke` / 收 / 收回）；机制
整体那一支认 `mech` / `机制` 与 `exit` / 出、`back` / 回；真值那一支认 `label` / `标注` 与
`list` / 列 / 清单、`add` / 录 / 录入、`rm` / 删 / 删除。第二级往后的组名（`gate` / `阶段`、
`exempt` / `豁免`、`mech` / `机制`、`label` / `标注`）**不进那些别名表**——它们是组名，
与「打开 / 关掉」不是一类东西。

两级共用一套解析纪律（`_word`）：整词匹配、多余一个词就拒绝并点名。裸的 `gate` 是**那一页
本身**，不是「gate 的开关」；不带动作的 `/regime` 仍然是事件熔断那一页，两个机制各有各的
只读页（CONTEXT.md 第169 条要求两个机制的动作都要过确认页，而确认页是**各自的**：它们的
敞口不是同一件事）。

## 两个机制为什么在一条命令下

`/regime` 的宾语是「机制开关」，两个开关（事件熔断 / 行情阶段 gate）的**打开与关闭**是
同一类动作，所以共用一条命令与一套解析；而它们的**判定与对账各在自己那一层**
（`breaker_switch` / `gate_switch`），本模块只做「谁敲的、敲的是什么」。

**回显口径有一处刻意的差别。** 两个机制的**打开**都回显整页——这一步会让机制在没有人的
时候动别人的仓位（gate 那一侧还重一层：它会把本阶段不适配的一批策略**停掉**，写进停止
声明表）。**关闭**则不同：事件熔断那一档不回显（`_off` 只说「关掉之后什么变了」），行情
阶段 gate **回显整页**——关闭页里有「关掉就安全了」在保命档（高波动）上是错的那句话
（那一层没有开关可翻），而它是撤防方向上唯一拦得住这个要命误读的东西；同时 CLI 入口
`manage.py regime_gate` 两个方向都印整页，聊天里少印一半就是同一个动作两个说法。

gate 的**两个方向**都还会多一句 `gate_switch.reconcile_warning`（档位翻了、但声明表没对
上），与 CLI 入口共用同一句话：那种轮次里活行一条都没被解除，而「把同一次动作再敲一遍」
正是能补上的动作。

## 与 `/event` 的分工

`/event` 管**事件本身**（录入、改档、改期、取消），`/regime` 管**开关**。两者不共用
任何一行写路径：一条 `/event cancel` 不会顺手关掉开关，一次 `/regime off` 也不会抹掉
事件表里的任何一行（第②f 段 Q3 —— 窗口同步是 `halt_sync` 的事，它「写与开关无关」）。

## 第三级：人工恢复豁免（第③段 W2）

`/regime exempt …` 是豁免的第二个入口（另一个是管理命令 `manage_deactivation_exemptions`）。
放在 `/regime` 下面，是因为它与 gate 是同一件事的两面：gate 按阶段停掉一批策略，豁免是
**人**把其中一条从当轮的停用里放出来（第③段 Q1）。**本模块只做「谁敲的、敲的是什么」**：
判据、写路径、渲染全在 `deactivation_run` 的「人工豁免」那一节，两个入口逐字共用——同一条
豁免在终端里与在 Telegram 里长得一样，靠的是同一次渲染，不是靠两处写得小心。

三条只有聊天这一侧才有的决定：

1. **不做二次确认**（Q6）。全库没有对话式二次确认，既有的人工确认是「裸命令先回显一页
   （只读）+ 人再敲一次带动作词的命令」——而豁免连那一页都不需要：两个动作都可逆
   （发出→收回，收回→再发一条），而「确认」在聊天里意味着记住上一句话的状态。为一个
   可逆动作引入这套状态，代价远超收益。
2. **不额外授权**（Q5）。`granted_by` 就是聊天渠道的 sender id（CLI 那条路写的是
   `getpass.getuser()`）——同一个字段两种取值，清单里「由 X」要认得出这是谁。机制不因为
   消息从 Telegram 来就放宽任何判据：冷启动与保命档期间照样拒绝猜阶段。
3. **阶段认中文名**（`/regime exempt grant 甲策略 下行趋势`）。中文名在**这里**翻成 slug，
   共享层只认 slug：在共享层再加一张中文表，就是给「阶段叫什么」造第二套答案。
   代价是**第二个词只能放阶段**——想只给备注就得先写阶段（`grant <策略> <阶段> <备注>`），
   把备注写在阶段的位置上会得到「认不出的阶段 '回测过得去'」。

**保命档期间显式点名 `high_vol` 仍然照落**（`resolve_regime` 只拒绝「替人猜」），但那次
发出会附一句 `blanket_grant_warning`：那条豁免在保命档期间完全空转。显式就是知情，
机制不该替人否决一个明确的选择——但它必须把人不知道的那件事说出来。

## 第四级：机制整体那一档（第①③段的出口）

`/regime mech …` 是三个开关里**最后一个**、也是唯一一直没有入口的那个：事件熔断与 gate
各管一样东西，机制整体这一档管「这套机制本身可不可信」。判据、写路径与体检页全在
`mechanism_switch`，本模块照旧只做「谁敲的、敲的是什么」。

三处与前三级**刻意不同**，都落在回复里让人看见：

1. **`exit` 的回显里有一句「这一档不是执行开关」。** `HALT_TRIGGER_SWITCH` 只把事件熔断
   映射到 `EVENT_BREAKER`、把停用映射到 `REGIME_GATE`，而 `MECHANISM` 在**全仓只有读者**
   （日报第④段、`query_halt`）。所以出 Shadow 的实际效果是**留一条记录**——它记的是
   「第①段的判定与切片被验证过了」。不说这句话，人敲完就以为机制上线了，而它什么都没
   打开；真要动作得各自开 `/regime on` 与 `/regime gate on`。
2. **`exit` 之后紧接着再报一遍没达标的项**（`mechanism_switch.unmet_note`，与体检页同一次
   快照）。页面里已经说过一次，但那一次在点头**之前**；把一个不达标的上线只说在动作之前，
   等于让「我已经点过头了」把这条信息盖掉。这不是禁止（页面不拒绝任何方向），是让人知道
   这次切换带着什么。
3. **`back` 不回显整页**（撤防方向，与 gate 的关闭一致），但要说清它**不解除任何东西**：
   声明表、停用决策、人工豁免各有各的开关与到期。撤防最危险的误读是「退回去就都停了」，
   而退回 Shadow 一个停用也不解除。

## 第五级：人工真值（第 164 条）

`/regime label …` 是成功标准①的**输入口**：人在这里把「我确定的那些历史区间是哪一档」写
下来，`truth_run.agreement()` 拿它与历史量化标签比。与第四级同一条理由——判据、写路径与
渲染全在 `truth.py` / `truth_run.py`，本模块只做「谁敲的、敲的是什么」。

三处只有这一层才有的决定：

1. **录入路径一个算法数字都不回显。** 第 164 条要求「先标完再看算法输出」，而那句话拦不
   住任何东西——人愿意就能去开体检页。机制唯一能做的是**不在录入的那一次交互里**把结论
   递到眼前：并排放着，人就会照着它改自己刚写下的判断，一致率随后测的是「人同不同意自己
   刚才看见的东西」。所以回复只报人自己输入的东西（段数、天数、跟已有区间打不打架）。
2. **档位认中文名**（ `/regime label add 2024-01-05 2024-03-20 下行趋势`），与 `exempt
   grant` 共用同一个翻译（`_regime_of`），共享层只认 slug。
3. **`rm` 是撤回，不是删除。** 回复里必须说清「行还在库里、原文一个字没动」：这是它与
   `/event cancel` 在观感上最容易混的地方（后者也不删行），而两者的收场不同——撤回会把
   这一段从分母里拿掉，取消只是把事件挪出日历。

## 报错一律是用户反馈

本模块**没有**自己的输入错误异常类，与 `event_commands.EventInputError` 不同：这里唯一
的输入错误是「多给了一个词」，它在解析那一步就知道，直接当回复返回（`success=True`，
打错一个字与系统坏掉在用户眼里不该长得一样）。写路径内部抛出的 `ValueError` 是程序 bug
（`reason` 由本模块自己生成，不可能为空），照旧往上抛给任务健康检查。

## 这一版不做的事

* **不收自由文本备注。** 切换原因就是确认页的摘要（第②f 段 Q8），摘要里已经有「打开
  时窗口相交几条、库里多少条、最近入库多久」。多一个 `--备注` 就是给「为什么开」造
  第二个说法，而两个说法分家时读的人分不出该信哪个；等有人能说出它比摘要多提供了什么
  再加。
* **不改天数、不改档位、不碰另外两个开关**（第183 条：`confirm_horizon_days` 不给
  Agent 传参；`MECHANISM` 与 `REGIME_GATE` 各自由第③段的入口管）。
* **不自动开关。** 自熔断退回 Shadow 之后再回执行态，必须由人重新敲一次（第161 条）。
"""

from __future__ import annotations

import functools
import logging
from datetime import datetime

from asgiref.sync import sync_to_async
from django.db import close_old_connections
from django.utils import timezone

from apps.regime import (
    breaker_switch,
    deactivation_run,
    events,
    gate_switch,
    mechanism_switch,
    truth,
    truth_run,
)
from apps.regime.models import ActorKind, MechanismMode
from apps.regime.quant import BaseRegime

from .base import AgentMessage, AgentResult

logger = logging.getLogger(__name__)

#: 豁免那一支的用法行。**单独拎出来**：它的报错只印自己这几行（上面那些 `/regime on`
#: 打在一条「策略名打错了」的回复下面，只会把要改的那一行淹掉），而整页 `_USAGE` 由它
#: 拼成——两处各写一遍，改了一处就会有一处漏掉。
_EXEMPT_USAGE = (
    "  /regime exempt     人工恢复豁免的清单（只读）\n"
    "  /regime exempt grant <策略> [阶段] [备注…]\n"
    "                     发出一条豁免（阶段不给则取当前生效阶段）\n"
    "  /regime exempt revoke <id> [id…]\n"
    "                     按 id 收回（可逆：再发一条即可）"
)

#: 真值那一支的用法行。与 `_EXEMPT_USAGE` 同一条理由：报错只印自己这几行。
_LABEL_USAGE = (
    "  /regime label      人工标注区间（真值）的清单与一致率（只读）\n"
    "  /regime label add <起> <终> <档位> [备注…]\n"
    "                     录入一段人工标注，例如：\n"
    "                     /regime label add 2024-01-05 2024-03-20 下行趋势 某轮熊市\n"
    "  /regime label rm <id> [id…]\n"
    "                     按 id 撤回（软删：行还在库里，原文不动）"
)

_USAGE = (
    "机制开关：\n"
    "  /regime            事件熔断的上线确认页（只读，不改变任何东西）\n"
    "  /regime on         打开事件熔断（先回显确认页，再落一条切换流水）\n"
    "  /regime off        关掉事件熔断（回到 Shadow）\n"
    "  /regime gate       行情阶段 gate 的上线确认页（只读）\n"
    "  /regime gate on    打开行情阶段 gate（先回显确认页，再落流水 + 对一次账）\n"
    "  /regime gate off   关掉行情阶段 gate（回到 Shadow）\n"
    "  /regime mech       机制整体的准入体检页（只读）\n"
    "  /regime mech exit  出 Shadow（先回显体检页，再落一条切换流水）\n"
    "  /regime mech back  退回 Shadow（只记录，不执行）\n"
    + _EXEMPT_USAGE
    + "\n"
    + _LABEL_USAGE
)

#: 子命令别名。只认整词，不做前缀匹配——「/regime onx」不是「on」的笔误而是另一个词。
_ALIASES = {
    "on": "on",
    "开": "on",
    "开启": "on",
    "打开": "on",
    "open": "on",
    "off": "off",
    "关": "off",
    "关闭": "off",
    "关掉": "off",
    "close": "off",
}

#: 第二级的那个词：`/regime gate …`。它**不进 `_ALIASES`**——那张表是「打开 / 关掉」的
#: 同义词表，把组名混进去会让 `/regime gate` 在解析上长得像一次开关动作。只认整词。
_GATE_WORDS = {"gate", "阶段"}

#: 第三级的那个词：`/regime exempt …`。与 `_GATE_WORDS` 同一条：组名不进动作别名表。
_EXEMPT_WORDS = {"exempt", "豁免"}

#: 第四级的那个词：`/regime mech …`。同上。
_MECH_WORDS = {"mech", "机制"}

#: 机制整体的两个动作。只有两个：**出**（切执行态）与**回**（退回 Shadow）。两个方向都
#: 留痕，但都不带参数——切换原因由 `mechanism_switch` 从体检页快照生成（理由见那边
#: 模块 docstring：原因要与人看过的那些数同源，不能由人手写）。
_MECH_ALIASES = {
    "exit": "exit",
    "出": "exit",
    "back": "back",
    "回": "back",
}

#: 豁免那一支的动作。与 `_ALIASES` 分开：这张表里的词后面**还能跟参数**
#: （`grant <策略> [阶段] [备注]`），而 `_ALIASES` 那一支是「多一个词就拒绝」。
_EXEMPT_ALIASES = {
    "list": "list",
    "列": "list",
    "清单": "list",
    "grant": "grant",
    "发": "grant",
    "发出": "grant",
    "revoke": "revoke",
    "收": "revoke",
    "收回": "revoke",
}

#: 第五级的那个词：`/regime label …`。同上：组名不进动作别名表。
_LABEL_WORDS = {"label", "标注"}

#: 真值那一支的动作。`rm` 收的是 id，`add` 收的是三个位置参数加一段自由文本——与
#: `_EXEMPT_ALIASES` 同类（词后面还能跟参数），所以不共用 `_ALIASES`。
_LABEL_ALIASES = {
    "list": "list",
    "列": "list",
    "清单": "list",
    "add": "add",
    "录": "add",
    "录入": "add",
    "rm": "rm",
    "删": "rm",
    "删除": "rm",
}


def _actor(message: AgentMessage) -> str:
    return (message.user_id or "").strip()


def _word(
    tokens: list[str], table: dict[str, str], *, prefix: str = ""
) -> tuple[str | None, str | None]:
    """从一张别名表里认出一个规范名。返回 `(规范名, 给用户看的那句话)`，恰好一个非空。

    「多给了一个词」是手滑（`success=True` 的普通回复），但绝不静默丢掉——开关切换尤其
    不能容忍：「我明明写了 --备注 X」而它被丢掉，流水里就少了一条人以为写进去了的原因。
    与 ②f 的写法一字不差，只是把两级解析共用成一个函数。
    """
    name = table.get(tokens[0].lower())
    if name is None:
        return None, f"未知的子命令：{prefix}{tokens[0]}\n\n{_USAGE}"
    if len(tokens) > 1:
        return None, f"这条命令不吃参数，多出来的词：{' '.join(tokens[1:])}\n\n{_USAGE}"
    return name, None


# --------------------------------------------------------------------------- #
# 子命令。签名统一是同步的 `(actor, now) -> str`：同步跑，入口用 `sync_to_async` 把它
# 挪到线程里，ORM 因此永远不在 async 上下文里执行（CLAUDE.md 的硬规则）。
# --------------------------------------------------------------------------- #


def _status(actor: str, now: datetime) -> str:
    """裸 `/regime`：确认页本身。**只读**——连一条流水都不写。"""
    return breaker_switch.confirmation_body(now=now)


def _on(actor: str, now: datetime) -> str:
    """打开事件熔断。**确认页与流水取自同一次快照**（`breaker_switch.page`）。"""
    briefing = breaker_switch.page(now=now)
    body = briefing.body
    row = breaker_switch.flip_event_breaker(
        MechanismMode.EXECUTING,
        actor_kind=ActorKind.CHAT,
        actor_name=actor,
        reason=briefing.summary,
        now=now,
    )
    if row is None:
        # 已经是执行态：不写第二条流水（第②f 段 Q7）。仍把整页回显出来——人敲 on 的
        # 时候想知道的是「现在拦不拦得住」，而不是「有没有新写一行」。
        return body + "\n\n事件熔断**本来就是执行态**，没有写第二条流水。"

    tail = (
        f"✅ 事件熔断已打开：{MechanismMode(row.from_mode).display} → "
        f"{MechanismMode(row.to_mode).display}（{events.format_moment(row.at)}）\n"
        "停止声明表**未改动**：打开开关不会凭空多出窗口，它只决定已有的窗口拦不拦人。"
    )
    return body + "\n\n" + tail


def _off(actor: str, now: datetime) -> str:
    """关掉事件熔断。**撤防也要留痕，但不重复整页。**"""
    data = breaker_switch.confirmation(now=now)
    row = breaker_switch.flip_event_breaker(
        MechanismMode.SHADOW,
        actor_kind=ActorKind.CHAT,
        actor_name=actor,
        reason=breaker_switch.closing_summary(data),
        now=now,
    )
    if row is None:
        return (
            "事件熔断**本来就是 Shadow**（只记录、不真拦），没有写第二条流水。\n"
            "要看当前状态：/regime"
        )

    lines = [
        f"✅ 事件熔断已关闭：{MechanismMode(row.from_mode).display} → "
        f"{MechanismMode(row.to_mode).display}（{events.format_moment(row.at)}）",
        "停止声明表**未改动**：窗口仍在表里，只是不再拦住下单；保命档（高波动）不受影响。",
    ]
    if data.open_now is not None:
        # 关掉的那一刻正压在窗口里：这是最需要被看见的一种关法，而且外面看不出来。
        lines.append(
            f"⚠️ 此刻正压在「{data.open_now.name}」的熔断窗口里"
            f"（{events.format_moment(data.open_now.halt_at)} → "
            f"{events.format_moment(data.open_now.resume_at)}）："
            "关掉之后这个窗口**不再拦人**。"
        )
    return "\n".join(lines)


_SUBCOMMANDS = {"on": _on, "off": _off}


# --------------------------------------------------------------------------- #
# 第二级：行情阶段 gate（第③段单元 ③b）。与事件熔断**同一个形状**：裸命令只读、
# `on` 回显整页、`off` 只回一句「关掉之后什么变了」。
#
# 打开要回显整页的理由在这里更重：它会让机制按行情阶段**停掉一批策略**（写停止声明
# 表），而不只是「拦住下单」。关闭那一侧多一句警告，那句话与 CLI 入口共用
# （`gate_switch.reconcile_warning`）——两个入口说的是同一件事。
# --------------------------------------------------------------------------- #


def _gate_status(actor: str, now: datetime) -> str:
    """裸 `/regime gate`：行情阶段 gate 的上线确认页。**只读**——连一条流水都不写。"""
    return gate_switch.page(now=now).body


def _gate_on(actor: str, now: datetime) -> str:
    """打开行情阶段 gate。**确认页与流水取自同一次快照**（`gate_switch.page`）。"""
    briefing = gate_switch.page(now=now)
    flip = gate_switch.flip_regime_gate(
        MechanismMode.EXECUTING,
        actor_kind=ActorKind.CHAT,
        actor_name=actor,
        reason=briefing.summary,
        now=now,
    )
    if flip.row is None:
        # 已经是执行态：不写第二条流水，但仍对了一次账（那正是「再敲一次」的用处）。
        # 那句警告照样要走：CLI 入口在**两个分支上都**打它（`regime_gate.handle` 的
        # `_warn_if_not_reconciled` 在 `return` 之前就调了），走 `on` 这条路的人看到的
        # 话必须与走 CLI 的人一样——「对了一次账」在这一轮可能什么都没对上。
        return _joined(
            briefing,
            flip,
            "行情阶段 gate**本来就是执行态**，没有写第二条流水（仍然对了一次账）。",
        )

    tail = (
        f"✅ 行情阶段 gate 已打开：{MechanismMode(flip.row.from_mode).display} → "
        f"{MechanismMode(flip.row.to_mode).display}（{events.format_moment(flip.row.at)}）\n"
        f"本阶段（{briefing.data.regime_display}）被判为不适配的被管策略会被**停用**"
        "（写进停止声明表）；保命档（高波动）那一层与它无关。"
    )
    return _joined(briefing, flip, tail)


def _gate_off(actor: str, now: datetime) -> str:
    """关掉行情阶段 gate。**撤防也要留痕，但不重复整页。**"""
    briefing = gate_switch.page(closing=True, now=now)
    flip = gate_switch.flip_regime_gate(
        MechanismMode.SHADOW,
        actor_kind=ActorKind.CHAT,
        actor_name=actor,
        reason=briefing.summary,
        now=now,
    )
    if flip.row is None:
        # 撤销方向**不回显整页**，但同样是「再敲一次」的那条路——而「表还没对上」最可能
        # 就发生在这条路上（阶段说不清时关掉 gate 不解除活行，于是人会再敲一次）。所以
        # 那句警告必须跟着这句「仍然对了一次账」一起出现，否则这个分支里唯一的好消息
        # 会把读的人骗成「表已经干净了」。CLI 入口两个分支都打这句，这里对齐。
        lines = [
            "行情阶段 gate**本来就是 Shadow**（只记录、不真拦），没有写第二条流水"
            "（仍然对了一次账）。",
            "要看当前状态：/regime gate",
        ]
        warning = gate_switch.reconcile_warning(briefing.data, flip.sync)
        if warning is not None:
            lines.append(warning)
        return "\n".join(lines)

    return _joined(
        briefing,
        flip,
        f"✅ 行情阶段 gate 已关闭：{MechanismMode(flip.row.from_mode).display} → "
        f"{MechanismMode(flip.row.to_mode).display}（{events.format_moment(flip.row.at)}）\n"
        f"本轮的策略档声明按「行情阶段 gate 已回 Shadow」解除"
        f"（{briefing.data.closing} 条）；人工豁免不受影响（关闭是撤防，不收回人给的豁免）。",
    )


def _joined(briefing, flip, tail: str) -> str:
    """正文 + 一句「这一步做完了什么」+ 那句「表没对上」的警告（有才加）。

    警告与 CLI 入口共用 `gate_switch.reconcile_warning`：两个入口说的是同一件事。
    """
    lines = [briefing.body, "", tail]
    warning = gate_switch.reconcile_warning(briefing.data, flip.sync)
    if warning is not None:
        lines.append(warning)
    return "\n".join(lines)


_GATE_SUBCOMMANDS = {"on": _gate_on, "off": _gate_off}


# --------------------------------------------------------------------------- #
# 第三级：人工恢复豁免（第③段 W2）。本组只做「谁敲的、敲的是什么」——判据、写路径与渲染
# 全在 `deactivation_run` 的「人工豁免」那一节（模块 docstring「第三级」那一段）。
#
# 签名比上面两级多一个入参：`(args, actor, now) -> str`。`args` 是动作词**之后**的那一段
# （`grant <策略> [阶段] [备注]`），因为这一组的动作带参数——上面两组是「多一个词就拒绝」，
# 这里多出来的词是参数本身。分发处用 `functools.partial` 把它绑成一个 `(actor, now)`。
# --------------------------------------------------------------------------- #


def _exempt_usage() -> str:
    """这一组的用法（只有它自己那几行）。

    共享层抛的 `ExemptionError` 只说事实（它同时服务终端），「怎么改」补在这里——两处
    分别在**两个入口各自的那一层**，谁都没有对方的措辞。
    """
    return f"用法：\n{_EXEMPT_USAGE}"


def _regime_of(text: str) -> str:
    """阶段那一项：认 slug，也认中文名（`/regime exempt grant 甲策略 下行趋势`）。

    中文名**只在这一层**翻成 slug（`resolve_regime` 收的是 slug，认不出的会拒绝）：在共享
    层再加一张中文表，就是给「阶段叫什么」造第二套答案，而终端那边只认 slug。
    """
    wanted = (text or "").strip()
    for member in BaseRegime:
        if wanted == member.display:
            return member.value
    return wanted


def _exempt_list(args: list[str], actor: str, now: datetime) -> str:
    """裸 `/regime exempt`（或 `list` / `列` / `清单`）：清单本身。**只读**。"""
    if args:
        return f"列清单不吃参数，多出来的词：{' '.join(args)}\n\n{_exempt_usage()}"
    return "\n".join(deactivation_run.roster_report(now=now))


def _exempt_grant(args: list[str], actor: str, now: datetime) -> str:
    """`grant <策略> [阶段] [备注…]`：发出一条豁免。

    **策略那一个词必给**：不给就没人知道豁免谁，没有任何合理的默认值。阶段不给则取当前
    生效阶段（冷启动与保命档期间**拒绝猜**，由共享层判），备注是第三个词之后的全部。
    """
    if not args:
        return (
            "要指明给哪条策略：/regime exempt grant <策略名或 id> [阶段] [备注]\n\n"
            + _exempt_usage()
        )

    try:
        strategy = deactivation_run.find_strategy_by_token(args[0])
        choice = deactivation_run.resolve_regime(
            _regime_of(args[1]) if len(args) > 1 else "", now=now
        )
    except deactivation_run.ExemptionError as exc:
        return f"{exc}\n\n{_exempt_usage()}"

    outcome = deactivation_run.grant(
        strategy=strategy,
        regime=choice.regime,
        # 落款是人：`granted_by` 在聊天这条路上是 sender id（CLI 那条路是系统用户名）。
        # 这个字段是 10 天之后回头看那条决策时唯一的「谁放行的」，所以不给默认值、不省略。
        actor=actor,
        note=" ".join(args[2:]),
        now=now,
    )

    lines = []
    if choice.taken_from_current:
        # 「取的是当前生效阶段」必须打出来，否则一条取来的阶段会被当成人的决定。
        lines.append(
            f"未给阶段，取当前生效阶段：{deactivation_run.regime_display(choice.regime)}"
            f"（自 {choice.effective_at:%Y-%m-%d %H:%M} 生效）"
        )
    lines.append(deactivation_run.grant_summary(outcome))
    lines.extend(deactivation_run.supersede_warnings(outcome.superseded))
    warning = deactivation_run.blanket_grant_warning(outcome.exemption)
    if warning:
        lines.append(warning)
    lines.append("要看现在的清单：/regime exempt")
    return "\n".join(lines)


def _exempt_revoke(args: list[str], actor: str, now: datetime) -> str:
    """`revoke <id> [id…]`：按 id 收回。整批拒绝语义（缺一个就一行不写）在共享层。"""
    if not args:
        return f"要指明收回哪几条，例如：/regime exempt revoke 3 4\n\n{_exempt_usage()}"
    try:
        outcome = deactivation_run.revoke(args, now=now)
    except deactivation_run.ExemptionError as exc:
        return f"{exc}\n\n{_exempt_usage()}"
    return "\n".join(deactivation_run.revoke_summary(outcome))


_EXEMPT_SUBCOMMANDS = {
    "list": _exempt_list,
    "grant": _exempt_grant,
    "revoke": _exempt_revoke,
}


# --------------------------------------------------------------------------- #
# 第四级：机制整体那一档。判据、写路径与体检页全在 `mechanism_switch`（见模块 docstring
# 「第四级」）。这一档**不是执行开关**：出 Shadow 只留一条记录，真正拦人的是上面两个。
# --------------------------------------------------------------------------- #


def _mech_status(actor: str, now: datetime) -> str:
    """裸 `/regime mech`：准入体检页本身。**只读**——连一条流水都不写。"""
    return mechanism_switch.page(now=now).body


def _mech_exit(actor: str, now: datetime) -> str:
    """出 Shadow。**体检页与流水取自同一次快照**（`mechanism_switch.page`）。"""
    briefing = mechanism_switch.page(now=now)
    row = mechanism_switch.flip_mechanism(
        MechanismMode.EXECUTING,
        actor_kind=ActorKind.CHAT,
        actor_name=actor,
        reason=briefing.summary,
        now=now,
    )
    if row is None:
        # 这一支**不补** `unmet_note`：那句话说的是「这次切换带着什么」，而这次什么都没切。
        # （整页里已经有全部缺口，所以信息没丢。）
        return briefing.body + "\n\n机制整体**本来就是执行态**，没有写第二条流水。"

    lines = [
        briefing.body,
        "",
        f"✅ 机制整体已出 Shadow：{MechanismMode(row.from_mode).display} → "
        f"{MechanismMode(row.to_mode).display}（{events.format_moment(row.at)}）",
        # 与确认页正文、Web 档位卡片同一处字面量（`mechanism_switch.NOT_EXECUTION_SWITCH`）：
        # 三个入口各写一份时，改词必然漏掉一份。
        mechanism_switch.NOT_EXECUTION_SWITCH,
        "真要动作：/regime on（事件熔断）、/regime gate on（行情阶段停用）——"
        "这两个才拦人。",
    ]
    note = mechanism_switch.unmet_note(briefing.data)
    if note is not None:
        lines.append(note)
    return "\n".join(lines)


def _mech_back(actor: str, now: datetime) -> str:
    """退回 Shadow。**撤防也要留痕，但不重复整页**（与 gate 的关闭一致）。"""
    data = mechanism_switch.confirmation(now=now)
    row = mechanism_switch.flip_mechanism(
        MechanismMode.SHADOW,
        actor_kind=ActorKind.CHAT,
        actor_name=actor,
        reason=mechanism_switch.closing_summary(data),
        now=now,
    )
    if row is None:
        return (
            "机制整体**本来就是 Shadow**（只记录、不执行），没有写第二条流水。\n"
            "要看现在的体检页：/regime mech"
        )

    lines = [
        f"✅ 机制整体已退回 Shadow：{MechanismMode(row.from_mode).display} → "
        f"{MechanismMode(row.to_mode).display}（{events.format_moment(row.at)}）",
        "⚠️ 退回 Shadow **不解除任何东西**：停止声明表原样留着，策略停用决策与人工豁免"
        "各有各的开关与到期——它只管「机制整体这一档」自己。",
        "下一次出 Shadow 仍然要人工确认（第 161 条），并且那一页会把这一次退回的原因"
        "摆出来。要看：/regime mech",
    ]
    return "\n".join(lines)


_MECH_SUBCOMMANDS = {"exit": _mech_exit, "back": _mech_back}


# --------------------------------------------------------------------------- #
# 第五级：人工真值（第 164 条）。本组只做「谁敲的、敲的是什么」——比对、写路径与渲染全在
# `truth.py` / `truth_run.py`（模块 docstring「第五级」那一段）。
#
# 签名与第三级同形：`(args, actor, now) -> str`，`args` 是动作词**之后**的那一段。
# --------------------------------------------------------------------------- #


def _label_usage() -> str:
    """这一组的用法（只有它自己那几行）。

    共享层抛的 `TruthInputError` 只说事实（它同时服务将来可能的终端入口），「怎么改」补在
    这里——两处分别在两个入口各自的那一层。
    """
    return f"用法：\n{_LABEL_USAGE}"


def _label_list(args: list[str], actor: str, now: datetime) -> str:
    """裸 `/regime label`（或 `list` / `列` / `清单`）：清单与一致率。**只读**。"""
    if args:
        return f"列清单不吃参数，多出来的词：{' '.join(args)}\n\n{_label_usage()}"
    return "\n".join(truth_run.roster_lines())


def _label_add(args: list[str], actor: str, now: datetime) -> str:
    """`add <起> <终> <档位> [备注…]`：录入一段人工标注。

    三个位置参数**都要给**：区间少了任一端就没法比，档位不给则没有任何合理的默认值
    ——「大概是箱体震荡吧」正是第 164 条要挡的那种猜测。备注是第四个词之后的全部。
    """
    if len(args) < 3:
        return (
            "要指明区间与档位：/regime label add <起> <终> <档位> [备注…]\n"
            "例如：/regime label add 2024-01-05 2024-03-20 下行趋势 某轮熊市\n\n"
            + _label_usage()
        )
    try:
        start = truth_run.parse_day(args[0])
        end = truth_run.parse_day(args[1])
        regime = truth_run.parse_regime(_regime_of(args[2]))
        outcome = truth_run.add(
            start=start,
            end=end,
            regime=regime,
            actor_kind=ActorKind.CHAT,
            # 落款是人：聊天这条路上是 sender id（与豁免的 `granted_by` 同一口径）。
            # 「谁在什么时候标了什么」是事后判断标注有没有被锚定的唯一线索。
            actor_name=actor,
            note=" ".join(args[3:]),
            now=now,
        )
    except truth.TruthInputError as exc:
        return f"{exc}\n\n{_label_usage()}"
    return "\n".join(truth_run.add_summary(outcome))


def _label_rm(args: list[str], actor: str, now: datetime) -> str:
    """`rm <id> [id…]`：按 id 撤回。整批拒绝语义（缺一个就一行不写）在共享层。"""
    if not args:
        return f"要指明撤回哪几段，例如：/regime label rm 3\n\n{_label_usage()}"
    try:
        outcome = truth_run.retract(
            args, actor_kind=ActorKind.CHAT, actor_name=actor, now=now
        )
    except truth.TruthInputError as exc:
        return f"{exc}\n\n{_label_usage()}"
    return "\n".join(truth_run.retract_summary(outcome, now=now))


_LABEL_SUBCOMMANDS = {"list": _label_list, "add": _label_add, "rm": _label_rm}


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #


async def handle_regime_command(message: AgentMessage, args: str) -> AgentResult:
    """`/regime …` 的入口。`args` 是命令词之后的那一段（可能为空）。"""
    actor = _actor(message)
    if not actor:
        return AgentResult(
            task_id=message.task_id,
            success=False,
            error="无法确定操作者身份，开关切换必须记下是谁操作的（聊天渠道的 sender id）",
        )

    tokens = args.split()
    if not tokens:
        handler = _status
    elif tokens[0].lower() in _GATE_WORDS:
        # `/regime gate [on|off]`：第二级的词之后才是动作，裸的 `gate` 是那一页本身。
        if len(tokens) == 1:
            handler = _gate_status
        else:
            name, error = _word(tokens[1:], _ALIASES, prefix=f"{tokens[0]} ")
            if error is not None:
                return AgentResult(task_id=message.task_id, success=True, data=error)
            handler = _GATE_SUBCOMMANDS[name]
    elif tokens[0].lower() in _MECH_WORDS:
        # `/regime mech [exit|back]`：裸的 `mech` 是那一页本身。
        if len(tokens) == 1:
            handler = _mech_status
        else:
            name, error = _word(tokens[1:], _MECH_ALIASES, prefix=f"{tokens[0]} ")
            if error is not None:
                return AgentResult(task_id=message.task_id, success=True, data=error)
            handler = _MECH_SUBCOMMANDS[name]
    elif tokens[0].lower() in _EXEMPT_WORDS:
        # `/regime exempt [list|grant|revoke] …`：裸的 `exempt` 是清单本身。这一组与上面两组
        # 有一处**解析上的差别**：动作词之后的那一段是**参数**，不是「多出来的词」——所以
        # `_word` 只拿动作词那一个去对表，剩下的原样交给子命令。
        rest = tokens[1:]
        if not rest:
            handler = functools.partial(_exempt_list, [])
        else:
            name, error = _word(rest[:1], _EXEMPT_ALIASES, prefix=f"{tokens[0]} ")
            if error is not None:
                return AgentResult(task_id=message.task_id, success=True, data=error)
            handler = functools.partial(_EXEMPT_SUBCOMMANDS[name], rest[1:])
    elif tokens[0].lower() in _LABEL_WORDS:
        # `/regime label [list|add|rm] …`：与豁免那一支同形——裸的 `label` 是清单本身，
        # 动作词之后的那一段是**参数**，所以 `_word` 只拿动作词那一个去对表。
        rest = tokens[1:]
        if not rest:
            handler = functools.partial(_label_list, [])
        else:
            name, error = _word(rest[:1], _LABEL_ALIASES, prefix=f"{tokens[0]} ")
            if error is not None:
                return AgentResult(task_id=message.task_id, success=True, data=error)
            handler = functools.partial(_LABEL_SUBCOMMANDS[name], rest[1:])
    else:
        name, error = _word(tokens, _ALIASES)
        if error is not None:
            return AgentResult(task_id=message.task_id, success=True, data=error)
        handler = _SUBCOMMANDS[name]

    now = timezone.now()

    def _work() -> str:
        close_old_connections()
        return handler(actor, now)

    text = await sync_to_async(_work)()
    logger.info("[regime] %s by %s", tokens[0] if tokens else "(页)", actor)
    return AgentResult(task_id=message.task_id, success=True, data=text)
