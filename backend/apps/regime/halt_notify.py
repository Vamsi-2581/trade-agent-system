"""停止声明的窗口通知（第②段单元 ②e）。

`HaltDeclaration` 是「机制此刻在拦什么」唯一说得出口的东西，而这个模块负责把它
**说给人听**：窗口开了发一条、窗口结束了发一条。加上 `tasks.sync_halt_windows` 里
那条「声明写入失败」的即时告警，②e 一共三条消息，共用这里的收件人口径与那一个出口。

第③段又加了第四条：`tasks.sync_gate` 写失败时的那一条（策略停用档）。它与②e 那条是
**同一件事的两个档**——同一张表、同一个失败面、同样的受众与流程，只有正文分岔：事件
熔断不可人工豁免，而策略停用档的豁免**人可以给**。所以这里是两个函数
（`alert_declaration_write_failure` / `alert_gate_write_failure`）而不是一个带 `body`
参数的：调用方各认领自己那一档，读代码的人不必跳进来才知道用户会读到哪一段话。

第③段单元 U3 再加第五条：**调度表体检**（`tasks.check_beat_health` →
`beat_health_run.check`）。它不是「停止声明」那一族的事，进这个模块只为一件事——**受众
与出口**：它要说的同样是「机制自身健康出了问题」（有几条 beat 条目不会被派发出去），
落点与上面两条一样是全体 `is_active` 用户，正文在它自己的纯层（`beat_health.alert_body`）。
所以出站口 `alert_everyone` 现在是**公开的**：它服务的正文有三处，本模块只管「发给谁、
从哪儿出去」。

## 为什么必须有这一层（而不是日志）

一个「表里没有窗口」的系统与「现在没有事件」的系统，从外面看起来一模一样——用户不看
日志，而 CONTEXT.md:66 把「告警」定义成**给一个具体的人的即时消息**。事件熔断又**不可
人工豁免**，所以「窗口开了」这件事如果没有主动说出来，用户遇到的是一个不声不响就不让
下单的系统。这条对「窗口结束」同样成立：不说的话，用户会以为熔断还在。

## 有一档不发即时消息：「按阶段暂停开新仓」（issue #51）

`HaltTrigger.REGIME_SUSPENSION` 的声明**不进这两条消息**，而这不是漏了一条接线：

- **一次阶段切换会同时产出多条同因声明**（被管策略里所有此刻不是 `fit` 的格子各一条）。
  逐条即时消息把这个源变成洪水，而它们说的是同一件事——「阶段变了」。事件熔断或保命档
  一次只开一条（`global` 一行），所以那一档一条一条发是对的。
- **不发 ≠ 看不见**。这一档的用处是拦单，而拦单时的拒绝理由逐单可见（`HaltLayer.text`
  把那句话贴在拒单理由里，那是被拦的那个人当下唯一需要读到的）；日报第④段按触发源计数、
  `query_halt` 报全部在拦的层、`/regime gate` 的确认页另有条数。缺的是「主动推送」，不是
  「说得出自己在拦什么」。
- **`DEACTIVATION` 那一档仍逐条通知**：它是「停用须人工恢复」的地基，一条声明就是一次
  动作，而且它有豁免那条人工出路要说——那条话没人主动说，用户就以为无路可走。

排除落在**两个取数谓词**（`_NO_NOTICE_TRIGGERS`）里，不在渲染层：这两个谓词就是「待通知」
的定义，排在那里之后这些行根本进不了队列。**必须两个谓词一起排**——这一类行上的通知账
永远空着，而「空账 = 待发」正是这一层的口径（`models.HaltDeclaration` 上那两列的注释）；
`pending_closed` 今天靠「开窗账非空」顺带把它们排掉，那是一个副作用，不是政策，写死它
才是。

## 「还剩几层」按受众算，不按全表算（第③段 Q3/Q4）

层可以同时生效（CONTEXT.md:177），所以窗口消息不能只说「本条结束」：单层世界里那句话等于
「恢复」，两层共存时它什么都不等于——而把「仍有另一层在拦」说成「已恢复」正是这份设计反复
点名的谎报。两条消息因此都带上「此刻还有哪些层」（`still_blocking`），口径是**受众这些人的
会话**：先按这一行的作用域求出收件人，再把这群人**全部**活跃实盘会话的（品种, 策略）取出来
问 `halt.layers_touching`，得到的是**这批人真会被拦住的集合**。

两端都会说出假话，所以要的是中间那条线：按全表报会说出「仍被〈SOL 解锁〉拦住」而收件人只跑
BTC；按这一行自己的作用域报，`strategy:<id>` 那一档又答不出「他们会话在跑什么」（一条策略
可以跑好几个品种）；只按本行作用域上那几条会话报，会对一个在别的品种上仍被拦住的人说出
「恢复可用」——而这两句话的落点都是**账户**（`still_blocking`）。

## 收件人：受影响策略的活跃实盘会话（不是订阅者）

口径与 `replay_run.alert_recipients` / `deactivation_run.running_strategy_ids` **同一处**
（`mode="live"` ∧ `LiveSession.ACTIVE_STATUSES`）：作用域 → 该作用域上的活跃实盘策略 →
这些策略的用户。三件事决定了这个取法：

- **没有订阅表**。「该通知谁」是**求值**出来的，不是谁订了这个窗口。事件熔断不可人工
  豁免，通知是机制的责任而不是订阅的产物；而一张订阅表会与声明行各自漂移（声明行可以
  被无痕改写、可以被解除，订阅关系不会跟着动）。
- **模拟盘不持仓真钱**，所以不在收件人里——与「停用决策不影响模拟盘」同一个理由。
- **作用域三档共用一条换算**：`global` = 全体活跃实盘会话；`symbol:X` = 这条会话的
  品种恰好是 `X`；`strategy:<id>` = 那条策略。品种是**逐字比**，不做归一化——声明
  作用域与 `LiveSession.symbol` 是同一套写法（`BTC/USDT`），换算成交易所口径是
  `reduce_run.normalize_scope_symbol` 那一步的事，在这里再换一次就是给「这个品种叫
  什么」造第二个答案（`halt.symbol_scope` 的 docstring 同一条纪律）。

**空收件人是合法的、且必须被如实报出来**（与 `reduce_run` 的减仓通知不同）：第②段整个
是 Shadow，一条活跃实盘会话都没有是常态。这不是失败，也不该读成「已经通知过了」——
`notify_pending` 把这类行单独计数（`no_recipients`），调用方把它报出去。

## 账记在行上、只记成功的那一次

`HaltDeclaration.opened_notified_at` / `closed_notified_at`（第②e 段加的）是这两条消息的
账。规矩只有一条：**送达了才记账**。于是「任务在半路崩了」不会吞掉一条消息——下一轮
（300 秒后）那一行还在待通知里，重发一遍。这与 `report.deliver_daily_report` 的
「已成功送达的人不再重投」是同一种写法。

两类「没送成」在**记不记账**上是相反的，因为一件是终局的、一件是可重试的：

- **收件人为空** → 终局：此刻没人受影响，记账，计数 `no_recipients`。不记账的话，一个
  没人在听的窗口会每 300 秒重试一整段窗口期。
- **`notify_user` 返回 `False`**（推送通路失败）→ 可重试：**不记账**，下一轮再发。代价是
  部分成功时（N 个收件人里挂了 1 个）那些已经收到的人会再收到一遍——账是行级的一对
  时刻，记不出「哪几个人收到了」，而按人记账是另一张表（`DailyReport.delivery` 那样的
  JSON）。为一条 300 秒重试的窗口通知开那张表不值，重复一条消息的代价比漏掉一条小。
- **失败不再往上告警**（第②e 段 Q8）：递归告警没有底，通知发不出去这件事只记日志、并把
  计数放进任务返回值。**那不代表有谁看得见**：这个返回值落进今天没有读者的 Celery 结果
  后端（`tasks.py` 模块 docstring 的「跑了没」那一节）。

## 这一层是同步的，出站那一步是 async

出站口 `alerts.notify_user` 是 async，而调用方（`tasks.sync_halt_windows`）是同步的
Celery 任务，所以这里用 `asyncio.run` 包一层——与 `report.deliver_daily_report` 同一个
写法（`report._send_to_each`）。渲染与挑选是**纯函数/普通查询**，测试可以直接调，
不必绕过 async 那一跳。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Sequence

from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.regime import events, gate_switch, halt, halt_sync, manual_halt
from apps.regime.models import HaltDeclaration, HaltTrigger
from apps.trading.models import LiveSession

logger = logging.getLogger(__name__)

#: 两类窗口消息。字符串也是摘要里的键，别改着玩。
KIND_OPENED = "opened"
KIND_CLOSED = "closed"

_SHADOW_NOTE = "当前为 Shadow（只记录、不真拦）：下面这条窗口已记进停止声明，但下单通路尚未按它拦截。"


# --------------------------------------------------------------------------- #
# 收件人
# --------------------------------------------------------------------------- #


def _active_live_sessions():
    """**活跃实盘会话**的唯一谓词（`mode="live"` ∧ `LiveSession.ACTIVE_STATUSES`）。

    与 `replay_run.alert_recipients` / `deactivation_run.running_strategy_ids` 同一处口径。
    抽出来是因为「收件人」与「这些人还会被什么拦住」两步都从这里取数——各写一遍谓词的话，
    两句话可以在同一条消息里各说各话（比如一条会话刚被停，收件人少了，而层数还按老名单
    算），而两个数看起来都正常。
    """
    return LiveSession.objects.filter(
        mode="live", status__in=LiveSession.ACTIVE_STATUSES
    )


def _audience_sessions(scope: str):
    """这一行作用域上的活跃实盘会话——收件人是从它算的。"""
    kind, value = halt.parse_scope(scope)
    sessions = _active_live_sessions()
    if kind == "symbol":
        sessions = sessions.filter(symbol=value)
    elif kind == "strategy":
        sessions = sessions.filter(strategy_id=value)
    return sessions


def recipients_for_scope(scope: str) -> tuple[str, ...]:
    """一个作用域上「受影响用户」的收件人，**字符串化**、去重后按 id 排序。

    空元组是**合法**的（见模块 docstring）：它意味着此刻没有活跃实盘会话，而不是
    「通知失败了」。

    `user_id` 那一列是 UUID，直接 `values_list` 出来的是 UUID 实例。这里统一成 `str`：
    与 `all_active_user_ids`（同一个模块）和 `reduce_run.recipients_for` 同一形状，而
    出站口的推送通道本来就把 id 当字符串用（`alerts.notify_user` 里那句 `str(user_id)`）。
    两个收件人函数返回不同的类型只会让「出去的是什么」这件事多一个说不清的地方。
    """
    return tuple(
        str(pk)
        for pk in _audience_sessions(scope)
        .values_list("user_id", flat=True)
        .distinct()
        .order_by("user_id")
    )


def still_blocking(scope: str, *, now: datetime) -> tuple[halt.HaltLayer, ...]:
    """这批人此刻还会被哪些层拦住（第③段 Q3）。**含本行自己**——排掉它是调用方的事。

    两步，第一步与收件人同源：

    1. **收件人是哪些人** = 这一行作用域上的活跃实盘会话的用户（`_audience_sessions`）。
    2. **这些人还会被什么拦住** = 这些用户的**全部**活跃实盘会话的「（品种, 策略）」，
       问 `halt.layers_touching`。

    第 2 步按**人**展开、而不是只用第 1 步那批会话，是因为这两条断言的落点是**账户**：
    「开新仓仍不可用」/「开新仓恢复可用」。一个人可以同时跑几个会话，只按本行作用域上那
    几条算，会对一个在别的品种上仍被停用的策略拦住的人说出「恢复可用」——正是这条设计
    反复要防的那类假话，而那句话看起来完全正常。

    按**全表**算则反着错：一条 `symbol:SOL/USDT` 的层对一群只跑 BTC 的人**不是**一层，
    对他们说「仍被那层拦住」同样是假话。求值走 `halt` 那一个判定口（`_matches`），不在
    这里重写一遍匹配规则：通知里说的「还剩几层」与订单通路上真实的拦法必须同源。
    """
    pairs = tuple(
        _active_live_sessions()
        .filter(user_id__in=_audience_sessions(scope).values_list("user_id", flat=True))
        .values_list("symbol", "strategy_id")
        .distinct()
        .order_by("symbol")
    )
    return halt.layers_touching(pairs, now=now)


def all_active_user_ids() -> list[str]:
    """全体启用用户。**只给「机制自身健康」那类消息用**（第②e 段 Q7）：

    CONTEXT.md:66 把落点分成两类——业务动作失败发给**受影响用户**，机制自身健康发给
    **全体 `is_active` 用户**。声明写入失败属于后者：它不是「你的单出事了」，而是
    「机制现在说不出自己在拦什么了」，而这件事对所有人都有后果。

    与 `reduce_run.recipients_for` 的「未归属账户」那一路同一个取法、同一条理由。
    """
    return [
        str(pk)
        for pk in get_user_model()
        .objects.filter(is_active=True)
        .values_list("pk", flat=True)
    ]


# --------------------------------------------------------------------------- #
# 待通知的行（普通查询；「送达了才记账」的记账口就是这里的两个谓词）
# --------------------------------------------------------------------------- #

#: **不发即时消息的触发源**。理由见模块 docstring 的「有一档不发即时消息」那节。
#:
#: 过滤写在取数谓词里（而不是 `notify_pending` 的循环里早退）：这两个谓词就是「待通知」
#: 的定义，排在这里这些行根本进不了队列——写在循环里的话，它们每 300 秒仍会被查出来一次
#: 再丢掉，而「队列里没有」与「队列里有但我不发」是两件不同的事实。
_NO_NOTICE_TRIGGERS = (HaltTrigger.REGIME_SUSPENSION.value,)


def pending_opened(*, now: datetime | None = None) -> list[HaltDeclaration]:
    """窗口起点已到、但还没通知过的行。

    **不要求它还活着**：一个短到「开与关落在同一轮任务之间」的窗口，那一轮如果任务没跑
    成，下一轮会先补发「已开启」再发「已结束」（`pending_closed` 的谓词要求先有过开窗
    通知），两条都在、顺序也对。要求活着的话这种窗口会一条消息都不发。

    代价是**上线首轮的一次性补发**：②e 之前就已经在表里的行（①/②c/②d 期间写下的）都
    没有账，于是各补一条「已开启」。那几条消息说的都是真事（那些窗口确实开着），且只补
    一次。

    `_NO_NOTICE_TRIGGERS` 那一档排除在外（模块 docstring）：它们的账**永远空着**，那不是
    待发而是「不用发」。
    """
    at = now or timezone.now()
    return list(
        HaltDeclaration.objects.filter(
            opened_at__lte=at, opened_notified_at__isnull=True
        )
        .exclude(trigger__in=_NO_NOTICE_TRIGGERS)
        .order_by("opened_at", "id")
    )


def pending_closed(*, now: datetime | None = None) -> list[HaltDeclaration]:
    """已经解除、但还没通知过的行。

    **要求 `opened_notified_at` 非空**：只有对一个人说过「它开了」，才有资格对他说
    「它结束了」。这条谓词顺带把上线首轮的补发限制在一处——②e 之前解除掉的历史行没有
    开窗账，于是不会被补一条莫名其妙的「已结束」。

    `_NO_NOTICE_TRIGGERS` 那一档排除在外（模块 docstring）。**这一条排除今天是冗余的**
    ——那一档的开窗账永远空着，上面那条谓词本来就会把它们排掉——但它写出来是政策而不是
    副作用：靠另一条谓词的副产品维持「不发」，改那条谓词的形状时就会静默把它放回来。
    """
    return list(
        HaltDeclaration.objects.filter(
            closed_at__isnull=False,
            closed_notified_at__isnull=True,
            opened_notified_at__isnull=False,
        )
        .exclude(trigger__in=_NO_NOTICE_TRIGGERS)
        .order_by("closed_at", "id")
    )


# --------------------------------------------------------------------------- #
# 渲染（纯函数——收 `shadow` 而不是自己去读开关，测试不必碰库）
# --------------------------------------------------------------------------- #


def _trigger_display(row: HaltDeclaration) -> str:
    return halt.trigger_of(row).display


def _period_line(row: HaltDeclaration) -> str:
    """一行说清这一层的生效期。**两个时刻都走 `events.format_moment`**：同一个时刻在事件库、
    日报与这里必须是同一句话（各写一遍就是「同一件事三处说法不同」，而读者是同一批人）。

    **按触发源分岔：只有事件熔断是一个「窗口」**（第③段 Q5）。窗口的判据是**两头都在日历
    上**（`halt_at` / `resume_at`），而其余几档都是机制施加的状态、由状态派生：

    - 保命档的起止是阶段的性质，写「截止时刻不定」读起来像「不知道什么时候解除」——
      它的截止条件**说得出来**（阶段离开高波动），所以说出来。
    - 策略停用那一档的解除条件有好几个（阶段离开 / 判据翻面 / 豁免 / 开关回 Shadow），
      一句说不完，所以它留着「截止时刻不定」。
    - 按阶段暂停开仓那一档（issue #51）的解除条件**是一个会自己翻回去的判据**，所以它说得
      出「随阶段变化自动翻转」，与策略停用那一档的「截止时刻不定」要能分辨：那一档等着人
      （豁免或改判据），这一档等的是下一次阶段判定。
    - 手动熔断的截止条件只有一个人（操作者），但若带着自带时长就**两头都在日历上**——
      那种情形说两头，与起止都由人定的「截止时刻不定」要能分辨。

    这一分岔与 `_headline` 是**同一个**（非窗口那几档说「已生效 / 已解除」，只有事件熔断说
    「窗口已开启 / 窗口已结束」）：标题说「已生效」而正文说「窗口：… 起」，读的人会去猜
    这两句说的是不是一件事——那正是这条分岔要消掉的东西，所以两处必须一起改。
    """
    start = events.format_moment(row.opened_at)
    trigger = halt.trigger_of(row)
    if trigger is HaltTrigger.EVENT:
        # 两头都在日历上才是完整的窗口；事件本身没给恢复时刻时（`resume_at` 空），
        # 说的是「起点已知、终点不定」，而不是「这不是窗口」。
        if row.expires_at is None:
            return f"窗口：{start} 起（截止时刻不定）"
        return f"窗口：{start} → {events.format_moment(row.expires_at)}"
    if trigger is HaltTrigger.BLANKET:
        return f"生效：{start} 起（随阶段离开高波动而结束）"
    if trigger is HaltTrigger.REGIME_SUSPENSION:
        return f"生效：{start} 起（随阶段变化自动翻转）"
    if trigger is HaltTrigger.MANUAL:
        if row.expires_at is None:
            return f"生效：{start} 起（到人工解除为止）"
        return f"生效：{start} → {events.format_moment(row.expires_at)}"
    return f"生效：{start} 起（截止时刻不定）"


def _headline(row: HaltDeclaration, kind: str) -> str:
    """消息的第一行。「窗口」这个词与 `_period_line` **同一分岔**（只有事件熔断有窗口）：
    标题说「窗口已开启」而正文说「生效：… 起」，读的人会去猜这两句说的是不是一件事。"""
    trigger = _trigger_display(row)
    scope = halt.scope_display(row.scope)
    is_window = halt.trigger_of(row) is HaltTrigger.EVENT
    if kind == KIND_OPENED:
        state = "窗口已开启" if is_window else "已生效"
        return f"🔒 【{trigger}】{state}｜作用域 {scope}"
    state = "窗口已结束" if is_window else "已解除"
    return f"🔓 【{trigger}】{state}｜作用域 {scope}"


def _layers_line(head: str, layers: Sequence[halt.HaltLayer]) -> str:
    """「此刻还剩几层」那一行（第③段 Q3/Q4）。``head`` 自带条数——两支的句式不同。

    层名走 `halt.HaltLayer.text`（行 → 层的唯一换算口）：用户在通知里读到的层，与他在
    `query_halt` 里查到、以及下单被拒时看到的那句话必须逐字同源。
    """
    return f"{head}：{'；'.join(layer.text for layer in layers)}"


def _close_reason_display(row: HaltDeclaration) -> str:
    """解除原因的人话——**按触发源路由**（第③段 Q7）。

    两个词表里都有一个短码叫 `regime_left`，而它们**不是同一个概念**：`halt_sync` 那个说的是
    「阶段已离开高波动」（保命档的话），`gate_switch` 那个说的是「这条决策行记的阶段已不是
    当前阶段」。所以这里必须按 `trigger` 分岔——一把抓地调 `halt_sync.close_reason_display`
    的表现是：两个词表都认得出这个码，谁都不报错，而一条策略档声明上贴着别档的文案。
    认不出的码两条路都原样返回（`close_reason_display` 的口径），所以路由写错只会错在
    同名的那一个码上，正是最难靠肉眼发现的那一个。

    **按阶段暂停开仓那一档（issue #51）也路由到 gate 那张词表**：它的解除码出自 `gate.py`
    的暂停路（`CLOSE_GATE_CLOSED` / `became_fit` / `exempted`），全是 `gate.CLOSE_*`——
    `halt_sync` 那张只说窗口的词表认不出它们，会原样回显一个短码。与停用那一档合在一支而
    不是各写一句：两路用的本来就是同一张 `gate.CLOSE_*` 词表，分开写会让「同源」这件事
    看起来像巧合。
    """
    trigger = halt.trigger_of(row)
    if trigger in (HaltTrigger.DEACTIVATION, HaltTrigger.REGIME_SUSPENSION):
        return gate_switch.gate_close_reason_display(row.closed_reason)
    if trigger is HaltTrigger.MANUAL:
        return manual_halt.close_reason_display(row.closed_reason)
    return halt_sync.close_reason_display(row.closed_reason)


def notify_body(
    row: HaltDeclaration,
    kind: str,
    *,
    shadow: bool,
    others: Sequence[halt.HaltLayer] = (),
) -> str:
    """一条窗口消息的正文。``shadow`` 是**发这条消息的此刻**这条线在不在 Shadow。

    Shadow 提示**单独占第一行**（第②e 段 Q6）：不写的话，「窗口已记录」会被读成「已经
    拦住了」，而 Shadow 期这两件事恰好相反——用户会以为下单被挡住了，实际没有。
    它说的是「此刻」，不是「窗口开的时候」：开关在这中间被打开过的话，事实以此刻为准。

    ``others`` 是**这批人此刻还会被拦住的其它层**（调用方按受众算好，见
    `still_blocking`）。层可以同时生效（CONTEXT.md:177），所以：

    - **开启那条**：只有一层时不写层集合（「集合」与「这条」是同一个答案，写了是噪声），
      两层以上才补一行「此刻共 N 层在拦」。
    - **结束那条**：**必须说清还剩几层**。单层世界里「本条结束」等于「恢复」，而两层共存
      时它不等于任何事——把「仍有另一层在拦」说成「已恢复」是谎报，而「用户以为能下单了
      却下不出去」正是本机制反复要防的那类输出。「已恢复」这个词只在一层都不剩时出现。
    """
    lines: list[str] = []
    if shadow:
        lines.append(f"⚠️ {_SHADOW_NOTE}")
    lines.append(_headline(row, kind))
    lines.append(_period_line(row))
    if kind == KIND_OPENED:
        lines.append(f"依据：\n{row.reason}")
        if others:
            lines.append(
                _layers_line(
                    f"此刻共 {len(others) + 1} 层在拦（本条在内）",
                    [halt.layer_of(row), *others],
                )
            )
    else:
        lines.append(
            f"解除：{events.format_moment(row.closed_at)}"
            f"（{_close_reason_display(row)}）"
        )
        if others:
            lines.append(
                _layers_line(
                    f"⚠️ 本条已解除，但**仍被 {len(others)} 层拦住**，开新仓仍不可用",
                    others,
                )
            )
        else:
            lines.append("✅ 已无任何层在拦，开新仓恢复可用。")
    return "\n".join(lines)


def write_failure_body(error: str) -> str:
    """「声明写入失败」那条消息的正文。

    必须把**后果**写出来，而不只是报一个异常：一个「表里没有窗口」的系统看起来与「现在
    没有事件」一模一样，而事件熔断不可人工豁免——所以这段文字要说的不是「有个任务挂了」
    （那件事今天只有体检页的调度表那一段看得见，且只看得出「beat 发出去了没有」），
    而是「此刻没人知道该不该拦，请人工看一眼」。
    """
    return (
        "🚨 停止声明窗口同步失败：机制此刻说不出自己在拦什么\n"
        "后果：停止声明表**没有被本轮对账更新**。事件熔断不可人工豁免，所以在这条恢复"
        "之前，本该被熔断的窗口可能不在表里——表现与「现在没有事件」完全一样，"
        "日志里看不出来。\n"
        f"错误：{error}\n"
        "下一轮（300 秒后）会自动重来；连续失败请人工核对事件表与停止声明表。"
    )


def gate_write_failure_body(error: str) -> str:
    """「策略停用决策档的声明写入失败」那条消息的正文（第③段 Q1）。

    与 `write_failure_body` 是**同一件事的两个档**（同一张表、同一个失败面），所以形状照抄
    ——要说的不是「有个任务挂了」，而是一个**看起来完全正常的后果**：「表里没有这条声明」
    与「本阶段没有被判为不适配的策略」在读表的人眼里一模一样。

    换掉的只有那一句：那一档说「事件熔断不可人工豁免」，而**这一档的豁免是人可以给的**
    （`DeactivationExemption`，写它的是管理命令 `manage_deactivation_exemptions`）。照抄
    那一句会让读的人以为无路可走，而这里正确的补救动作恰恰是给一次在期豁免把时间买回来
    ——那条路不受这条故障影响，是这一档与那一档在**能不能救**上的差别。
    """
    return (
        "🚨 停止声明窗口同步失败（策略停用决策档）：机制此刻说不出它在按阶段停谁\n"
        "后果：策略停用决策的停止声明表**没有被本轮对账更新**。本轮该被拦下的策略此刻"
        "可能不在表里、开仓不会被拦——表现与「当前阶段没有被判为不适配的策略」完全一样，"
        "日志里看不出来。\n"
        f"错误：{error}\n"
        "下一轮（300 秒后）会自动重来；连续失败请人工核对当前阶段与停止声明表。"
        "这一档的豁免是人可以给的（管理命令 `manage_deactivation_exemptions`），"
        "所以真要停手不必等这条恢复——给一次在期豁免即可。"
    )


# --------------------------------------------------------------------------- #
# 出站（唯一出口：`alerts.notify_user`）
# --------------------------------------------------------------------------- #


async def _send_to_each(user_ids: Sequence[Any], text: str) -> int:
    """投给一串人，返回送达人数。写法与 `reduce_run._alert` / `report._alert_each` 同款。"""
    from apps.trading.alerts import notify_user

    sent = 0
    for user_id in user_ids:
        if await notify_user(user_id, text):
            sent += 1
    return sent


def _mark(row: HaltDeclaration, kind: str, at: datetime) -> None:
    """记下这一类的通知已送达。**只在送达之后调**。"""
    field = "opened_notified_at" if kind == KIND_OPENED else "closed_notified_at"
    setattr(row, field, at)
    row.save(update_fields=[field])


def _shadowed(row: HaltDeclaration) -> bool:
    """这条线此刻在不在 Shadow。`halt.switch_open` 是「哪个开关管哪条线」的唯一换算口，
    所以保命档在这里恒为「不在 Shadow」（它没有开关、永远作数）。"""
    return not halt.switch_open(halt.trigger_of(row))


def _others(row: HaltDeclaration, *, now: datetime) -> tuple[halt.HaltLayer, ...]:
    """这批人此刻还会被拦住的**其它**层（本行自己排掉）。

    结束那一批排不排本行都没关系（它已经解除，`live_declarations` 已经不认它）；**开启
    那一批必须排**——不排的话用户读到的是「此刻共 2 层在拦：A；A」：两个来源各自都对，
    合起来是一句错话。
    """
    here = (halt.trigger_of(row), row.scope)
    return tuple(
        layer
        for layer in still_blocking(row.scope, now=now)
        if (layer.trigger, layer.scope) != here
    )


def notify_pending(*, now: datetime | None = None) -> dict:
    """把该发的窗口消息发出去，返回 ``{opened, closed, failed, no_recipients}``。

    **两批按顺序走**：先开窗、后结束。顺序有意义——`pending_closed` 的谓词要求开窗已
    记账，所以同一条短窗口在**同一轮**里补发时会先「开启」后「结束」，读起来是顺的。
    为此结束那一批的查询在开窗那一批**写完之后**才发。

    **不抛异常**：发不出去是「这条消息没送到」，不是「对账失败」——后者才该让任务标
    FAILURE（`sync` 的异常往上抛）。这里只记日志并把计数放进返回值。
    """
    at = now or timezone.now()
    summary = {"opened": 0, "closed": 0, "failed": 0, "no_recipients": 0}

    # 两批的取数口是**惰性**的（lambda），不是先把两个列表都查出来：`pending_closed`
    # 的谓词要求 `opened_notified_at` 非空，所以它必须在开窗那一批**写完账之后**才查。
    # 预先求值的话，同一轮里的短窗口只会发出「已开启」——那条谓词当场失效，而这个 bug
    # 没有任何外部迹象（`closed` 计数为 0 读起来就是「没有窗口结束」）。
    for kind, fetch in (
        (KIND_OPENED, lambda: pending_opened(now=at)),
        (KIND_CLOSED, lambda: pending_closed()),
    ):
        for row in fetch():
            recipients = recipients_for_scope(row.scope)
            if not recipients:
                # 终局：此刻没有人受影响。记账，免得一个没人在听的窗口每 300 秒重试一遍。
                _mark(row, kind, at)
                summary["no_recipients"] += 1
                logger.info(
                    "[regime] 停止声明窗口通知：%s 无收件人（作用域 %s），只记账",
                    kind,
                    row.scope,
                )
                continue

            text = notify_body(
                row, kind, shadow=_shadowed(row), others=_others(row, now=at)
            )
            sent = asyncio.run(_send_to_each(recipients, text))
            if sent == len(recipients):
                _mark(row, kind, at)
                summary[kind] += 1
            else:
                # 不记账 → 下一轮重发（可能给已收到的人重复一条，见模块 docstring）。
                summary["failed"] += 1
                logger.warning(
                    "[regime] 停止声明窗口通知未送达 kind=%s scope=%s 送达 %s/%s",
                    kind,
                    row.scope,
                    sent,
                    len(recipients),
                )

    if summary["opened"] or summary["closed"] or summary["failed"]:
        logger.info("[regime] 停止声明窗口通知：%s", summary)
    return summary


def alert_everyone(text: str) -> dict:
    """把一条「机制自身故障」的消息投给全体 `is_active` 用户，返回
    ``{"recipients", "delivered"}``。

    受众是**全体**而不是受影响的那些人（CONTEXT.md:66 的两类落点之一）：这条消息不是
    「你的单出事了」，而是「机制本身出了你看不见的事」——那对每个人都有后果。

    三条消息共用它，**正文都在各自的纯层**（`write_failure_body` /
    `gate_write_failure_body` / `beat_health.alert_body`）：这里只管受众与出口。
    所以它是公开的，而两个 `alert_*` 是包装——那两条把「哪一档的正文」写进函数名里，
    为的是读 `tasks.py` 的人不必跳进来才知道用户会读到哪一段话。

    发不出去只记日志（第②e 段 Q8：不递归告警，但也不静默成功）：原本那个要往上抛的东西
    才是主角，替换掉它会让真实故障连 FAILURE 那条记录都没有。
    """
    recipients = all_active_user_ids()
    sent = asyncio.run(_send_to_each(recipients, text))
    if sent != len(recipients):
        logger.error("[regime] 告警自身未送达：%s/%s", sent, len(recipients))
    return {"recipients": len(recipients), "delivered": sent}


def alert_declaration_write_failure(error: Exception) -> dict:
    """声明写入失败（事件熔断 / 保命档那一档）→ 全体 `is_active` 用户（第②e 段 Q7）。

    **流程是「发完再往上抛」**，与 `reduce_run._alert` 的「发了就继续」不同：那条路失败
    的是**一个动作的结果**，任务本身还活得下去；这条说的是**机制说不出自己在拦什么**，
    对账没完成就该让任务标 FAILURE（下一轮会自动重来，见 `tasks.py` 的模块 docstring）。

    Returns:
        ``{"recipients": n, "delivered": n}``。
    """
    return alert_everyone(write_failure_body(str(error)))


def alert_gate_write_failure(error: Exception) -> dict:
    """声明写入失败（**策略停用决策档**）→ 全体 `is_active` 用户（第③段 Q1）。

    受众与流程与上一条一模一样（同一个失败面、同一条「发完再往上抛」），**只有正文不同**：
    见 `gate_write_failure_body`。两条分开而不是给上一条加一个 `body=` 参数：调用方
    （`tasks.sync_halt_windows` / `tasks.sync_gate`）各自认领自己那一档，读 `tasks.py` 的
    人就看得出来「这条任务失败时用户读到的是哪一段话」。
    """
    return alert_everyone(gate_write_failure_body(str(error)))
