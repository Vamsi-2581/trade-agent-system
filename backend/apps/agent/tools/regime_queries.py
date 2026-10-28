"""行情阶段机制的四个只读工具（第①段单元 8v）。

CONTEXT.md 第 183 条把边界钉在**领域对象**上，而不是「一个机制状态工具」：

- ``query_regime``                 生效中与待生效两条判定，各自成组
- ``query_halt``                   当前在拦的全部层（事件熔断层 / 高波动档 / 策略停用决策档 /
  按阶段暂停开仓档），带作用域、触发源、
  生效期与依据；**读的是停止声明表**（第②c 段起，订单通路认的就是这一份）
- ``query_deactivation_decisions`` 停用决策 + 证据摘要（**按当前用户的活跃会话裁剪**）
- ``query_events``                 未来 7 天的高影响事件与熔断窗口（**天数写死**）

**不做聚合的「机制状态」工具**：四个对象回答四个不同问题，聚合会让「为什么停我」这类追问
无法定向到证据摘要，Agent 只能把一大坨状态倒给用户。

## 一条对既有约定的显式破例

ToolRegistry 的约定是「``user_id`` 自动过滤，Agent 参数不暴露 ``user_id``」。本机制的判定与
保命档是**系统级的、全局唯一**的——``RegimeJudgement`` 的键是 ``(symbol, effective_at)``，
与用户无关。按 ``user_id`` 过滤会造出一个不存在的东西（「我的行情阶段」）。所以
``query_regime`` / ``query_halt`` / ``query_events`` **不过滤、返回全局状态**，只有
``query_deactivation_decisions`` 按当前用户的活跃会话裁剪——那是四个对象里唯一 per-user
为真的一个（「这条决策停的**是**我在跑的策略吗」）。

**破例必须写下来**：不写，实现时会默认套用过滤，然后用户看到一个互相矛盾的「系统级判定」。
裁剪的判据见 CONTEXT.md 第 172 条——**按「这个对象是不是 per-user 为真」决定，不按入口
一刀切**。

## 措辞必须与日报一致（CONTEXT.md 第 183 条）

``apps/agent/base.py`` 把工具返回值 ``str(result.data)`` 后原样贴进 tool 消息，所以四个
工具的 ``data`` 是**预渲染好的文本**，不是让模型自己复述的数字字典——交给模型转述，
「措辞一致」就成了一句祈祷。第①段的渲染（``report._section_today`` / ``_quant_lines`` /
``_regime_display`` / ``_escalation_display``）、第③段的渲染（``report._section_events``）
都是**同一份代码**：同一天两个入口对同一件事各说各话，正是本机制从头到尾在防的形态
（告警与真实状态不一致最伤信任）。

## 为什么测试打的是 ``_render`` 而不是 ``execute``

``db_async`` 走 ``thread_sensitive=False``，在独立线程里取连接——``TestCase`` 的事务在那个
线程里看不见（同环境的 ``tools/test_exchange_account.py`` 常年红灯正是这个原因）。所以四个
工具的取数 + 渲染全放在**同步**的 ``_render`` 里，异步的 ``execute`` 只是几行包装。
"""

from __future__ import annotations

import logging
import uuid

from django.utils import timezone

from apps.core.db_utils import db_async

from .base import BaseTool, ToolResult

logger = logging.getLogger(__name__)

_EMPTY_SCHEMA: dict = {"type": "object", "properties": {}, "required": []}


def _resolve_user_id(raw: str) -> str | None:
    """``user_id`` 可能是 UUID 也可能是 username（``list_orders.py`` 的先例）。

    解析不到返回 ``None``——**这不是一种错误**：一个还没有活跃会话的账号，在「哪些停用决策
    与你相关」这一问上与一个不存在的账号给出的是同一个答案（空集）。把两者报成不同的东西，
    只会让 Agent 去追一个假区别。
    """
    try:
        return str(uuid.UUID(str(raw)))
    except (ValueError, TypeError):
        from django.contrib.auth import get_user_model

        user = get_user_model().objects.filter(username=raw).first()
        return str(user.pk) if user else None


# --------------------------------------------------------------------------- #
# query_regime
# --------------------------------------------------------------------------- #


def _in_force_lines(record) -> list[str]:
    """生效中那条判定的正文。

    与第①段**同一套词**（``_regime_display`` / ``_escalation_display`` / ``_quant_lines``，
    连「基础阶段」「依据：」这些行首都是照抄的），但**时态不同**：第①段报的是「今天刚产出、
    明天 08:00 才咬人」的那条，所以它必须写「（此刻生效中的仍是上一有效阶段，本条要到生效
    时刻才咬人）」；这一组报的正是此刻在咬人的那条，那句话在这里是**假的**。所以两组各自
    成组、各写各的，而不是把 ``_section_today`` 调两遍——调两遍就是把一句假话贴进半个输出。
    """
    from apps.regime import report

    effective = report._regime_display(record.effective_regime)
    base = report._regime_display(record.base_regime)
    lines = [f"当前阶段：{effective}"]
    lines.append(f"生效时刻：{report.format_business(record.effective_at)}")
    escalation = record.escalation or ""
    if escalation:
        lines.append(
            f"基础阶段：{base}；被抬升为 {effective}"
            f"（抬升标志：{report._escalation_display(escalation)}）"
        )
    elif base != effective:
        lines.append(f"基础阶段：{base}")
    numbers = report._quant_lines(record)
    if numbers:
        lines.append("依据：")
        lines.extend(numbers)
    return lines


def _pending_lines(record) -> list[str]:
    """待生效那条判定的正文——**直接调第①段的渲染**。

    日报第①段报的就是这一条，所以这里逐字同源不是「尽量一致」而是「同一份代码」。代价是
    它会带上「标的：…」那一行（外层头部已经写过标的了），这是刻意接受的冗余：同一句话
    重复两遍无害，而两处措辞漂开有害。
    """
    from apps.regime import judgement, report

    payload = judgement._describe(record, record.run_day)
    return report._section_today(payload, record, record.symbol).splitlines()


class QueryRegimeTool(BaseTool):
    name = "query_regime"
    description = (
        "查询行情阶段机制当前判定的阶段。**同时给出两条**：此刻生效中的、与下一个日界起生效的，"
        "各自带三值（基础阶段 / 抬升标志 / 生效阶段）与依据数字（ATR% 分位、EMA 斜率、分离度）。"
        "回答「为什么我现在被停」用生效中那条，回答「明天我为什么会被停」用待生效那条。"
        "只读；返回的是系统级判定，全局唯一，不按人裁剪。"
    )

    @property
    def parameters_schema(self) -> dict:
        return dict(_EMPTY_SCHEMA)

    def _render(self) -> str:
        from apps.regime import judgement, report

        symbol = judgement.SYMBOL
        now = timezone.now()
        lines = [f"行情阶段判定（标的：{symbol}）", ""]

        current = judgement.current_judgement(symbol, now=now)
        lines.append("【生效中】此刻正在咬人的那条判定")
        if current is None:
            lines.append(
                "库中还没有任何生效过的判定（冷启动），当前没有任何阶段在生效。"
            )
        else:
            lines.extend(_in_force_lines(current))

        lines.append("")
        pending = judgement.pending_judgement(symbol, now=now)
        lines.append("【待生效】下一个日界起生效的那条判定")
        if pending is None:
            # **不写「最近那条已经生效」**：它在正常情形下为真，可一旦判定行是补写的
            # （先写了 D 那条、之后才补 D−2 那条）就变成假话——而紧接着的「最近写下的
            # 一条是…」正是为那种情形准备的，两行挨着自相矛盾，恰恰是本机制从头到尾在防
            # 的形态。生效与否由上面【生效中】那一组回答，这里不重复断言。
            lines.append("没有待生效的判定（下一条要等判定任务下一次跑）。")
            latest = judgement.last_judgement(symbol)
            if latest is not None and (current is None or latest.pk != current.pk):
                # 只有它既不是生效中、也不是待生效时才补这一句：那种情况下上面两段都没提它，
                # 而「机制上一次成功写下了什么」恰恰是这一问的收尾。措辞照抄
                # `report._missing_judgement_lines` 的那一行。
                lines.append(
                    f"最近写下的一条是 {report.format_business(latest.created_at)} 那次"
                    f"（运行日 {latest.run_day}，自 "
                    f"{report.format_business(latest.effective_at)} 起生效）"
                )
        else:
            lines.extend(_pending_lines(pending))

        return "\n".join(lines)

    async def execute(self, **kwargs) -> ToolResult:
        try:
            text = await db_async(self._render)()
        except Exception as e:  # noqa: BLE001 — 工具边界，任何异常都要变成一句人话
            logger.error("[QueryRegimeTool] 查询失败: %s", e, exc_info=True)
            return ToolResult(success=False, error=f"查询行情阶段判定失败: {e}")
        return ToolResult(success=True, data=text)


# --------------------------------------------------------------------------- #
# query_halt
# --------------------------------------------------------------------------- #


def _layer_block(row) -> tuple[str, list[str]]:
    """一行停止声明 → （层标题，正文）。

    标题取 ``halt.layer_of(row).text``——**行 → 层的唯一换算口**。这一层从前是「推」出来的
    （直接读 ``MajorEvent`` 与生效判定），第②c 段起改为读 ``HaltDeclaration``：判定函数
    只认这张表（CONTEXT.md:134），所以「此刻在拦」的唯一答案就是它。取 ``layer_of`` 而不是
    在这里重拼一遍「触发源 + 作用域」，是为了让层标题与 ``pre_trade_check`` 的拒绝理由
    **逐字同源**：用户看到的拒绝理由，与他在工具里查到的东西，必须是同一句话。

    正文两段：生效期（两个绝对时刻都写出来，用 ``events.format_moment`` 所以与事件库、
    日报第③段同口径），与声明自己的依据（``reason``，写入方落库时写的那一份）。
    """
    from apps.regime import events, halt

    layer = halt.layer_of(row)
    body = [
        f"  生效期：{events.format_moment(layer.opened_at)}"
        f" → {events.format_moment(layer.expires_at)}"
    ]
    body.extend(f"  {line}" for line in (row.reason or "").splitlines())
    return (layer.text, body)


def _exemption_lines(now, *, blanket_live: bool) -> list[str]:
    """在期豁免的处境（CONTEXT.md 第 176 条）。

    **人工恢复豁免不穿透保命档**——这条必须能在这条输出里被读出来。否则用户会看到一个自己
    放行过、却又被停的策略，而那与「机制没听见我」在观感上无法区分。

    那句话本身在 `deactivation_run.exemption_standing`：同一句还要出现在豁免清单（终端与
    `/regime exempt`）里，两处各写一份的话，「当前一条都不生效」这个警告迟早只在其中一处
    跟着改。这里只加前缀——工具的输出每一行都带话题名。
    """
    from apps.regime import deactivation_run

    standing = deactivation_run.exemption_standing(now=now, blanket_live=blanket_live)
    return [f"人工豁免：{standing}"]


def _gate_line(now) -> str:
    """机制当前档 + 停止声明表此刻的实数。

    ``now`` 由调用方给（与 ``_exemption_lines`` 同款）：这一行里的条数取自声明表的生效期
    过滤，而**工具自称的「此刻」必须只有一个**。少了这个入参，上面几层按冻结的时刻算、
    这一行按墙上时钟算，测试里就会出现「层数 1、声明表 0 条」这种在真实运行中不可能出现
    的组合，而那种断言红了只会把人引到错的地方。

    没有这一行，「现在在拦什么」会被读成「这些层已经在拦下单了」。第②c 段之后上面列的层
    **就是**声明表的行（``query_halt`` 与 ``pre_trade_check`` 从同一个 ``blocking_declarations``
    出发），**四档全在内**：事件熔断、保命档、第③段接上的策略停用决策、以及 issue #51 的
    按阶段暂停开仓。所以报的是
    **声明表的实数**，而它与上面列出的层数是**同一个函数、同一次求值上的同一个数**——
    两个数相等是一条结构上的不变量，报出来是为了让它**看得见**。

    **``now`` 一路传下去是这条不变量的前提**（这里原先漏传，只对上面几层生效）：少了它，
    上面几层按冻结的时刻算、这一行按墙上时钟算，差额会在某条声明恰好到期的那个瞬间冒出来
    ——而它长得就像「有一档没对上」。这句话原先写的是「差额正是还没接线的那一档」：③b/③c
    把那一档接上之后那句话已经不成立，而它当时掩盖的正是这条双时钟的缝。

    ``blocking_declarations`` 而不是 ``live_declarations``：要报的是真的在拦的条数；少一层
    开关过滤，就会把「开关关着但行还留着」读成一个正在拦的层。

    两个开关都回显：出 Shadow 那个是机制整体，事件熔断那个直接决定事件层的声明作不作数
    ——只报前者的话，「声明表非空但开关关着」会被读成「已经在拦」。

    **减仓不受停止判定拦截**：``RiskGuard.pre_trade_check`` 的这一步只管开新仓
    （``reduce_only`` 的单直接放行）。不说这一句，「停止」会被读成「什么都动不了」，而减仓
    恰恰是熔断时唯一想让它动起来的事。
    """
    from apps.regime import halt
    from apps.regime.models import MechanismKind, RegimeMechanismSwitch

    declared = len(halt.blocking_declarations(now=now))
    return (
        f"机制当前档：{RegimeMechanismSwitch.current().display}"
        f"；事件熔断开关："
        f"{RegimeMechanismSwitch.current(MechanismKind.EVENT_BREAKER).display}"
        f"；停止声明表此刻 {declared} 条在生效（拦住的是开新仓，减仓放行）"
        "——停止判定已接到下单拦截（第②段 halt 状态机）：上面列的层就是这张表里的行，"
        "**四档都算在内**（事件熔断 / 保命档 / 策略停用决策 / 按阶段暂停开仓），订单通路认的也是它。"
        "上面列了几层、这里就是几条——同一个 ``blocking_declarations``、同一个时刻，"
        "两个数本该恒等；不等只可能是取数坏了。"
    )


class QueryHaltTool(BaseTool):
    name = "query_halt"
    description = (
        "查询行情阶段机制当前**全部在拦的层**（事件熔断层、高波动档又称保命档、"
        "策略停用决策档、按阶段暂停开仓档），"
        "每层带作用域、触发源、生效期与依据。多层可以同时生效，所以返回的是一个集合而不是"
        "一条。**读的是停止声明表**——订单通路真正认的那一份，所以这里列出的层就是此刻会"
        "拦住开新仓的层。**不按人裁剪**：用户问的是「现在系统在拦什么」，裁剪会让他怀疑"
        "工具在瞒他。也会说出在期人工豁免当前是否被保命档压住（豁免不穿透保命档）。只读。"
    )

    @property
    def parameters_schema(self) -> dict:
        return dict(_EMPTY_SCHEMA)

    def _render(self) -> str:
        from apps.regime import halt
        from apps.regime.models import HaltTrigger

        now = timezone.now()
        # **按触发源判，不按层标题的字面前缀判**：层标题是写入方给的 `label`，前缀随数据
        # 变，而「这一层是不是保命档」是触发源这一列上的事实。
        rows = halt.blocking_declarations(now=now)
        blanket_live = any(halt.trigger_of(row) is HaltTrigger.BLANKET for row in rows)

        lines: list[str] = []
        if not rows:
            lines.append(
                "当前没有任何层在拦：事件熔断层、高波动档、策略停用决策档与按阶段暂停开仓档"
                "都没有生效。"
            )
        else:
            lines.append(f"当前在拦的层：{len(rows)} 层（系统级状态，不按人裁剪）")
            for index, row in enumerate(rows, start=1):
                label, body = _layer_block(row)
                lines.append("")
                lines.append(f"【第 {index} 层｜{label}】")
                lines.extend(body)

        lines.append("")
        lines.extend(_exemption_lines(now, blanket_live=blanket_live))
        lines.append("")
        lines.append(_gate_line(now))
        return "\n".join(lines)

    async def execute(self, **kwargs) -> ToolResult:
        try:
            text = await db_async(self._render)()
        except Exception as e:  # noqa: BLE001
            logger.error("[QueryHaltTool] 查询失败: %s", e, exc_info=True)
            return ToolResult(success=False, error=f"查询熔断层失败: {e}")
        return ToolResult(success=True, data=text)


# --------------------------------------------------------------------------- #
# query_deactivation_decisions
# --------------------------------------------------------------------------- #


def _threshold_text(cell: dict) -> str:
    """格子的门槛留痕（``pool._evidence`` 里的 ``threshold``）。

    ``met`` 由 ``merged.enough`` 现算并**存进证据**——这里只读它，绝不重算：重算就是给
    「这格达没达门槛」造第二个真相，而池化层已经把那个真相冻在证据里了。
    """
    threshold = cell.get("threshold") or {}
    parts: list[str] = []
    if cell.get("trades") is not None:
        parts.append(f"{cell['trades']} 笔")
    if cell.get("months") is not None:
        parts.append(f"{cell['months']} 个月")
    if cell.get("regime_days") is not None:
        parts.append(f"该阶段 {cell['regime_days']} 个交易日")
    text = " / ".join(parts) if parts else "（未记）"
    if threshold:
        text += (
            f"（门槛 {threshold.get('min_trades')} 笔 / {threshold.get('min_months')} 个月，"
            f"{'已达' if threshold.get('met') else '未达'}）"
        )
    return text


def _evidence_text(evidence: dict) -> list[str]:
    """「依据摘要」的正文。

    数字全部取自决策行自己冻下来的 ``evidence``（``deactivation_run._frozen_evidence`` 写下
    的那一份），**不现算**：池化表会换代，现算出来的数会与这条决策当初凭什么成立对不上，
    而那正是审计要问的。
    """
    from apps.regime import report
    from apps.regime.slice import REASON_DISPLAY, STATE_DISPLAY

    cell = evidence.get("cell") or {}
    state = evidence.get("state") or ""
    reason = evidence.get("reason") or ""
    state_text = STATE_DISPLAY.get(state) or state or "（无）"
    if reason:
        state_text += f"：{REASON_DISPLAY.get(reason) or reason}"

    lines = [f"    池化结论：{state_text}"]
    lines.append(f"    依据：{_threshold_text(cell)}")

    metrics = cell.get("metrics") or {}
    bits: list[str] = []
    if metrics.get("calmar") is not None:
        bits.append(f"Calmar {report._num(metrics['calmar'], digits=3)}")
    if metrics.get("max_drawdown_pct") is not None:
        bits.append(
            f"分段最大回撤 {report._num(metrics['max_drawdown_pct'], digits=3)}%"
        )
    symbols = "、".join(cell.get("symbols") or [])
    if symbols:
        bits.append(f"品种 {symbols}")
    if bits:
        lines.append("    " + "；".join(bits))

    generation = evidence.get("pool_rebuild_id")
    version = evidence.get("pool_version")
    stamp = f"    世代：#{generation}" if generation else "    世代：（未记）"
    if version:
        stamp += f"（池化口径 {version}）"
    raw_effective_at = evidence.get("regime_effective_at")
    if raw_effective_at:
        try:
            from datetime import datetime

            stamp += f"；判定生效时刻 {report.format_business(datetime.fromisoformat(raw_effective_at))}"
        except (TypeError, ValueError):
            stamp += f"；判定生效时刻 {raw_effective_at}"
    lines.append(stamp)

    if evidence.get("needs_review"):
        # 会走到这里说明这一行是**不该被写出来的那种**（TARGET 与 needs_review 互斥）。
        # 照实说，不掩盖：`evidence` 是冻结的，它比现在这条路径更接近当时的事实。
        lines.append(
            "    ⚠ 依据当时标了「方向冲突、待人工复核」，与「已写成停用决策」矛盾，请复核"
        )
    return lines


def _exemption_text(exemption, *, now, blanket_live: bool) -> str:
    """这一行的人工豁免处境（CONTEXT.md 第 176 条的后半句）。"""
    from apps.regime import report
    from apps.regime.slice import REASON_DISPLAY, REASON_HIGH_VOL_BLANKET

    if exemption is None:
        return "    人工豁免：无"
    text = (
        f"    人工豁免：{report.format_business(exemption.granted_at)} 由 "
        f"{exemption.granted_by or '（未记）'} 授予，"
        f"{report.format_business(exemption.expires_at)} 到期"
    )
    if exemption.closed_at is not None:
        text += f"；已提前失效（{report.format_business(exemption.closed_at)}）"
    elif exemption.expires_at <= now:
        text += "；已过期"
    elif exemption.granted_at > now:
        text += "；尚未生效"
    elif blanket_live:
        text += (
            f"——**当前不生效**：{REASON_DISPLAY[REASON_HIGH_VOL_BLANKET]}在拦，"
            "人工恢复豁免不穿透保命档"
        )
    else:
        text += "；在期生效中"
    return text


def _decision_lines(decision, *, now, blanket_live: bool) -> list[str]:
    from apps.regime import deactivation as deact
    from apps.regime import report

    evidence = decision.evidence or {}
    lines = [
        f"- {decision.strategy.name}｜{report._regime_display(decision.regime)}："
        f"{decision.get_status_display()}"
    ]
    lines.append(f"    结论：{deact.VERDICT_DISPLAY[deact.TARGET]}")
    lines.extend(_evidence_text(evidence))
    lines.append(
        f"    首次判出：{report.format_business(decision.first_decided_at)}；"
        f"最近确认：{report.format_business(decision.last_confirmed_at)}"
    )
    lines.append(
        _exemption_text(decision.exemption, now=now, blanket_live=blanket_live)
    )
    return lines


class QueryDeactivationDecisionsTool(BaseTool):
    name = "query_deactivation_decisions"
    description = (
        "查询**与你相关**的停用决策（停用哪个策略、在哪个阶段、为什么、依据数字是什么、"
        "有没有人工豁免），每条都带状态（建议中 / 已施加 / 已解除）——**不过滤状态**，"
        "否则第③段把一条决策改成「已施加」之后，「为什么停我」会答成「没有相关决策」。"
        "按你活跃会话里实际在跑的策略裁剪——这是本机制四个只读查询里"
        "唯一按人裁剪的一个，因为「这条决策停的是我在跑的策略吗」是唯一 per-user 为真的问题。"
        "会说出在期豁免当前是否被保命档压住（豁免不穿透保命档）。只读。"
    )

    @property
    def parameters_schema(self) -> dict:
        return dict(_EMPTY_SCHEMA)

    def _render(self, user_id: str) -> str:
        from apps.regime import report
        from apps.regime.deactivation_run import current_regime_state
        from apps.regime.models import DeactivationDecision

        resolved = _resolve_user_id(user_id)
        mine = report.user_affected_strategy_ids(resolved) if resolved else set()
        now = timezone.now()
        blanket_live = current_regime_state(now=now).blanket

        lines = [f"你正在跑的策略：{len(mine)} 个（停用决策按这个集合裁剪）", ""]
        if not mine:
            lines.append(
                "你没有活跃会话，所以没有任何停用决策与你相关。"
                "（退出 Shadow 之后，被停用的也只会是你实际在跑的策略。）"
            )
            lines.append("")
            lines.append(_gate_line(now))
            return "\n".join(lines)

        # **不按 `status` 过滤**（初版写的是 `status=SUGGESTED`，一个会长成陷阱的筛子）：
        # 第③段的 gate 会把决策改成 `APPLIED`，那时一条正在停人的决策若被筛掉，用户问
        # 「为什么停我」会得到「没有与你相关的停用决策」——而他的策略正被停着。`RELEASED`
        # 同理：豁免指针就挂在这一行上，筛掉它，「我不是恢复过它吗」也一并答不出来。
        # 三种状态各自的含义由 `_decision_lines` 第一行原样回显（建议中 / 已施加 / 已解除），
        # 所以「这条到底停没停」永远有一个字面答案，不靠读者去推。
        rows = list(
            DeactivationDecision.objects.filter(strategy_id__in=mine)
            .select_related("strategy", "exemption")
            .order_by("strategy_id", "regime")
        )
        if not rows:
            lines.append("没有与你相关的停用决策。")
        else:
            lines.append(f"与你相关的停用决策：{len(rows)} 条")
            for decision in rows:
                lines.append("")
                lines.extend(
                    _decision_lines(decision, now=now, blanket_live=blanket_live)
                )

        lines.append("")
        lines.append(_gate_line(now))
        return "\n".join(lines)

    async def execute(self, user_id: str = "", **kwargs) -> ToolResult:
        if not user_id:
            return ToolResult(success=False, error="无法确定用户身份")
        try:
            text = await db_async(self._render)(user_id)
        except Exception as e:  # noqa: BLE001
            logger.error(
                "[QueryDeactivationDecisionsTool] 查询失败: %s", e, exc_info=True
            )
            return ToolResult(success=False, error=f"查询停用决策失败: {e}")
        return ToolResult(success=True, data=text)


# --------------------------------------------------------------------------- #
# query_events
# --------------------------------------------------------------------------- #


class QueryEventsTool(BaseTool):
    name = "query_events"
    description = (
        "查询未来 **7 天**的高影响事件与熔断窗口（档位「高」的事件才会触发熔断），"
        "以及待人工处置的候选事件（候选不产生任何熔断）。"
        "**天数固定 7 天，不接受参数**：用户说「这周」「近期」「下个月」时不要改天数，"
        "它与日报第三段回答的是同一个问题，两处必须是同一个数。只读，不按人裁剪。"
    )

    @property
    def parameters_schema(self) -> dict:
        return dict(_EMPTY_SCHEMA)

    def _render(self) -> str:
        from apps.regime import report

        # 第③段的渲染函数**本身就是**这个工具要的答案：查询形状与
        # `apps.agent.event_commands._list` 逐字同源，天数取 `config.REPORT.event_horizon_days`
        # 而不吃参数。重写一遍就是给「未来 7 天有什么事件」造第二个实现。
        return report._section_events(now=timezone.now())

    async def execute(self, **kwargs) -> ToolResult:
        try:
            text = await db_async(self._render)()
        except Exception as e:  # noqa: BLE001
            logger.error("[QueryEventsTool] 查询失败: %s", e, exc_info=True)
            return ToolResult(success=False, error=f"查询事件失败: {e}")
        return ToolResult(success=True, data=text)
