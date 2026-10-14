"""停止声明的窗口通知与声明写入失败告警（第②段单元 ②e）。

`test_halt_sync.py` 钉的是**表被写成了什么**（事实 → 声明行）；这个文件钉的是**那些行
有没有说给人听**。两者分开，是因为它们坏掉的样子不同：写侧坏掉是「表里根本没有那一行」，
通知侧坏掉是「行在表里、没人知道」——后者与「现在没有事件」看起来一模一样（`halt_notify`
的模块 docstring），而这正是 ②e 存在的全部理由。

## 三条消息、一个出口

三条消息（窗口开启 / 窗口结束 / 声明写入失败）共用 `alerts.notify_user` 这一个出口，所以
下面的投递口一律换成 `AsyncMock`（`test_delivery.py` 的同一个替换面）——真发一条 Telegram
消息不是测试该干的事。**收件人口径是三条里唯一分叉的地方**：窗口消息发给受影响策略的
活跃实盘会话（求值），失败告警发给全体 `is_active` 用户（机制自身健康）。这两条各有一个
测试类盯着。

「声明写入失败」这一条到第③段分成了**两个档**（事件熔断 / 策略停用决策），受众与流程
一模一样、只有正文不同：前者不可人工豁免，后者的豁免人可以给。所以正文那一层有两组
「说什么」与一组「不说什么」的断言，接线那一层还要钉「哪条任务走哪一档」。

## 分层的界线

`notify_body` 是纯函数（收 `shadow` 而不是自己去读开关），所以它跑在 `SimpleTestCase` 上
——那个类**就是**「这条不许碰库」的执行形态。其余都要真库：收件人要读 `LiveSession`，两个
待通知谓词读的是声明表上的两列账，而「送达了才记账」的全部内容就是那两列有没有被写。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase

from apps.regime import events, gate, gate_switch, halt, halt_notify, halt_sync
from apps.regime.models import (
    ActorKind,
    HaltDeclaration,
    HaltTrigger,
    MechanismKind,
    MechanismMode,
    RegimeMechanismSwitch,
)
from apps.trading.models import LiveSession, Strategy

#: 「此刻」：北京 2026-09-23 12:00 == UTC 04:00（与 `test_halt_sync.py` 同一个场地）。
NOW = datetime(2026, 9, 23, 4, 0, tzinfo=dt_timezone.utc)

#: 投递口（本仓库唯一的出站通知口）的替换面。函数内 import，所以打在源模块上即可。
NOTIFY = "apps.trading.alerts.notify_user"


def _notify(delivered=True):
    """一个假的出站口。`delivered` 可以是 bool（全体同命）或一串 bool（逐人不同）。"""
    if isinstance(delivered, bool):
        return patch(NOTIFY, AsyncMock(return_value=delivered))
    return patch(NOTIFY, AsyncMock(side_effect=list(delivered)))


def _user(email: str, *, active: bool = True):
    """造一个用户。`is_active` 不在 `create_user` 的签名里，所以停用要另走一步 `save`。"""
    User = get_user_model()
    user = User.objects.create_user(
        email=email, username=email.split("@")[0], password="pw12345"
    )
    if not active:
        user.is_active = False
        user.save(update_fields=["is_active"])
    return user


def _clear_the_field() -> None:
    """把库里**已有的**活跃用户先请出场（`test_delivery.py` 同款）。

    每个测试库都带着两条数据迁移种下的系统用户（`admin@` / `scheduler@tradeclaw.local`），
    而「声明写入失败」的受众是「全库所有 `is_active` 用户」——不请他们出场，`await_count`
    就永远比自己造的人多两个。**这是测试要把场地清空，不是生产要排除谁**（CONTEXT.md:173
    的受众定义就是所有 `is_active` 用户）。`TestCase` 会回滚，不外泄。

    **窗口通知那几条用例不需要它**：那边的受众由 `LiveSession` 求值，系统用户没有会话。
    """
    get_user_model().objects.filter(is_active=True).update(is_active=False)


def _strategy(name: str = "甲") -> Strategy:
    return Strategy.objects.create(name=name, code_path=f"/t/{name}.py")


def _session(
    user,
    *,
    strategy: Strategy | None = None,
    symbol: str = "BTC/USDT",
    mode: str = "live",
    status: str = "running",
) -> LiveSession:
    return LiveSession.objects.create(
        user=user,
        strategy=strategy or _strategy(),
        symbol=symbol,
        mode=mode,
        status=status,
        initial_capital=Decimal("10000.00"),
    )


def _declaration(
    *,
    trigger: HaltTrigger = HaltTrigger.EVENT,
    scope: str | None = None,
    label: str = "FOMC 议息",
    reason: str = "高影响事件熔断窗口",
    opened_at: datetime = NOW - timedelta(hours=1),
    expires_at: datetime | None = None,
    closed_at: datetime | None = None,
    closed_reason: str = "",
    opened_notified_at: datetime | None = None,
    closed_notified_at: datetime | None = None,
) -> HaltDeclaration:
    """一条声明行。两个通知时刻默认都空 = 「还没发」，那正是待通知队列的形状。"""
    return HaltDeclaration.objects.create(
        trigger=trigger.value,
        scope=halt.global_scope() if scope is None else scope,
        label=label,
        reason=reason,
        opened_at=opened_at,
        expires_at=expires_at,
        closed_at=closed_at,
        closed_reason=closed_reason,
        opened_notified_at=opened_notified_at,
        closed_notified_at=closed_notified_at,
        actor_kind=ActorKind.TASK.value,
        actor_name="regime.sync_halt_windows",
    )


def _unpersisted(
    *,
    trigger: HaltTrigger = HaltTrigger.EVENT,
    scope: str | None = None,
    label: str = "FOMC 议息",
    reason: str = "高影响事件熔断窗口",
    opened_at: datetime = NOW - timedelta(hours=1),
    expires_at: datetime | None = None,
    closed_at: datetime | None = None,
    closed_reason: str = "",
) -> HaltDeclaration:
    """一条**不落库**的声明行——`notify_body` 只读那几列，不必先造一遍库。

    `label` / `reason` 可以改，是因为有些用例要断言的正是**某句话不出现**（「窗口」在
    保命档那一档不许出现），而默认文案里带「窗口」二字。
    """
    return HaltDeclaration(
        trigger=trigger.value,
        scope=halt.global_scope() if scope is None else scope,
        label=label,
        reason=reason,
        opened_at=opened_at,
        expires_at=expires_at,
        closed_at=closed_at,
        closed_reason=closed_reason,
    )


def _layer(
    *,
    trigger: HaltTrigger = HaltTrigger.BLANKET,
    scope: str | None = None,
    label: str = "高波动",
) -> halt.HaltLayer:
    """一层（**求值结果**，纯值对象）——`notify_body` 收的就是它，不必落库。"""
    return halt.HaltLayer(
        trigger=trigger,
        scope=halt.global_scope() if scope is None else scope,
        label=label,
        reason="阶段处于高波动",
        opened_at=NOW - timedelta(hours=3),
        expires_at=None,
    )


def _switch(kind: MechanismKind, mode: MechanismMode) -> RegimeMechanismSwitch:
    return RegimeMechanismSwitch.objects.create(
        kind=kind.value,
        from_mode=MechanismMode.SHADOW.value,
        to_mode=mode.value,
        at=NOW - timedelta(days=1),
        actor_kind=ActorKind.CLI.value,
        actor_name="ops",
        reason="测试",
    )


def _texts(mock) -> list[str]:
    """这一轮实际发出去的正文，按调用顺序。"""
    return [args[1] for args, _kwargs in mock.call_args_list]


# --------------------------------------------------------------------------- #
# 收件人：受影响策略的活跃实盘会话（求值，不是订阅）
# --------------------------------------------------------------------------- #


class TestRecipientsForScope(TestCase):
    """三档作用域 → 一串用户 id。判据只有一条：活跃实盘会话（`mode="live"` ∧
    `LiveSession.ACTIVE_STATUSES`），与 `replay_run` / `deactivation_run` 同一处口径。
    """

    def test_global_scope_hits_every_live_session(self):
        first, second = _user("a@t.local"), _user("b@t.local")
        _session(first)
        _session(second)

        self.assertEqual(
            halt_notify.recipients_for_scope(halt.global_scope()),
            tuple(sorted([str(first.pk), str(second.pk)])),
        )

    def test_a_paper_session_is_not_a_recipient(self):
        """模拟盘不持仓真钱，所以不在收件人里——与「停用决策不影响模拟盘」同一个理由。"""
        paper = _user("paper@t.local")
        _session(paper, mode="paper")

        self.assertEqual(halt_notify.recipients_for_scope(halt.global_scope()), ())

    def test_a_stopped_session_is_not_a_recipient(self):
        """已结束的会话不在 `ACTIVE_STATUSES` 里：它将来不会持仓，通知它没有意义。"""
        gone = _user("gone@t.local")
        _session(gone, status="stopped")

        self.assertEqual(halt_notify.recipients_for_scope(halt.global_scope()), ())

    def test_every_active_status_is_a_recipient(self):
        """`pending` / `paused` 都算——口径是「这个会话还在、将来还可能持仓」。"""
        users = [
            _user(f"s{i}@t.local") for i in range(len(LiveSession.ACTIVE_STATUSES))
        ]
        for user, status in zip(users, LiveSession.ACTIVE_STATUSES):
            _session(user, status=status)

        self.assertEqual(
            halt_notify.recipients_for_scope(halt.global_scope()),
            tuple(sorted(str(user.pk) for user in users)),
        )

    def test_a_symbol_scope_hits_only_that_symbol(self):
        """品种是**逐字比**、不做归一化：声明作用域与 `LiveSession.symbol` 同一套写法
        （`BTC/USDT`），换算成交易所口径是 `reduce_run` 那一步的事。"""
        btc, eth = _user("btc@t.local"), _user("eth@t.local")
        _session(btc, symbol="BTC/USDT")
        _session(eth, symbol="ETH/USDT")

        self.assertEqual(
            halt_notify.recipients_for_scope(halt.symbol_scope("BTC/USDT")),
            (str(btc.pk),),
        )

    def test_a_strategy_scope_hits_only_that_strategy(self):
        mine, theirs = _user("mine@t.local"), _user("theirs@t.local")
        strategy = _strategy("甲")
        _session(mine, strategy=strategy)
        _session(theirs, strategy=_strategy("乙"))

        self.assertEqual(
            halt_notify.recipients_for_scope(halt.strategy_scope(strategy.pk)),
            (str(mine.pk),),
        )

    def test_one_user_with_two_sessions_gets_one_message(self):
        """去重：同一件事说一遍就够，而一个人可以同时跑几个会话。"""
        user = _user("twice@t.local")
        _session(user)
        _session(user, symbol="ETH/USDT")

        self.assertEqual(
            halt_notify.recipients_for_scope(halt.global_scope()), (str(user.pk),)
        )

    def test_no_session_is_an_empty_tuple_not_an_error(self):
        """**空收件人是合法的**（第②段整个是 Shadow，一条活跃实盘会话都没有是常态）。

        它不是「已经通知过了」，也不是失败——`notify_pending` 把它单独计数（`no_recipients`），
        调用方把它报出去。
        """
        self.assertEqual(halt_notify.recipients_for_scope(halt.global_scope()), ())


# --------------------------------------------------------------------------- #
# 待通知的两个谓词（「送达了才记账」的记账口）
# --------------------------------------------------------------------------- #


class TestPendingQueues(TestCase):
    def test_opened_picks_rows_whose_start_has_arrived(self):
        due = _declaration(opened_at=NOW - timedelta(minutes=5))
        # 还没到点的那一行必须落在**另一个作用域**上：活行的唯一键是（触发源 × 作用域），
        # 同源同作用域的两行在库里根本并立不了（`uniq_live_halt_declaration`）。
        _declaration(
            scope=halt.symbol_scope("ETH/USDT"), opened_at=NOW + timedelta(minutes=5)
        )

        self.assertEqual(halt_notify.pending_opened(now=NOW), [due])

    def test_an_already_notified_row_is_not_picked_again(self):
        """账上的时刻就是「不再重发」的全部依据——这是 300 秒一轮敢重跑的前提。"""
        already = _declaration(
            opened_at=NOW - timedelta(minutes=5), opened_notified_at=NOW
        )
        self.assertNotIn(already, halt_notify.pending_opened(now=NOW))

    def test_opened_does_not_require_the_row_to_still_be_alive(self):
        """**短窗口也不能漏消息**：开与关落在同一轮任务之间的窗口，那一轮没跑成的话，
        下一轮要先补「已开启」再补「已结束」，两条都在。要求活着的话这种窗口一条都不发。
        """
        short = _declaration(
            opened_at=NOW - timedelta(minutes=5), closed_at=NOW - timedelta(minutes=4)
        )
        self.assertIn(short, halt_notify.pending_opened(now=NOW))

    def test_closed_requires_the_opening_to_have_been_told(self):
        """只有对一个人说过「它开了」，才有资格对他说「它结束了」。

        这条谓词顺带把上线首轮的补发限制在一处：②e 之前解除掉的历史行没有开窗账，
        于是不会被补一条莫名其妙的「已结束」。
        """
        _never_opened = _declaration(
            opened_at=NOW - timedelta(hours=2),
            closed_at=NOW - timedelta(hours=1),
            opened_notified_at=None,
        )
        self.assertEqual(halt_notify.pending_closed(), [])

        told = _declaration(
            opened_at=NOW - timedelta(hours=2),
            closed_at=NOW - timedelta(hours=1),
            opened_notified_at=NOW - timedelta(hours=2),
        )
        self.assertEqual(halt_notify.pending_closed(), [told])

    def test_closed_ignores_rows_that_are_still_open(self):
        _declaration(opened_at=NOW - timedelta(hours=1), opened_notified_at=NOW)
        self.assertEqual(halt_notify.pending_closed(), [])

    def test_an_already_notified_close_is_not_picked_again(self):
        _declaration(
            opened_at=NOW - timedelta(hours=2),
            closed_at=NOW - timedelta(hours=1),
            opened_notified_at=NOW - timedelta(hours=2),
            closed_notified_at=NOW - timedelta(hours=1),
        )
        self.assertEqual(halt_notify.pending_closed(), [])

    def test_a_moved_window_start_clears_the_opening_ledger(self):
        """窗口起点被**原地改写**时把开窗账清回空（`halt_sync._rewrite`，②e）。

        不清的话，改期之后新起点到点不会有任何消息——用户手上留着一条已经不对的时刻，
        而机制以为通知过了。这是本模块之外唯一一处替 ②e 做的写入，所以在这里钉死。
        """
        row = _declaration(
            opened_at=NOW - timedelta(hours=1),
            opened_notified_at=NOW - timedelta(hours=1),
        )
        halt_sync_rewrite(row, opened_at=NOW + timedelta(hours=3))

        row.refresh_from_db()
        self.assertIsNone(row.opened_notified_at)
        self.assertIn(row, halt_notify.pending_opened(now=NOW + timedelta(hours=3)))

    def test_an_unchanged_window_keeps_its_ledger(self):
        """只是 label / reason 变了（例如多了一条事件并进这一段）而起点没动时**不清账**：
        已经通知过的那条消息没有被推翻，第二轮必须是整表空转（`halt_sync` 的幂等）。
        """
        row = _declaration(
            opened_at=NOW - timedelta(hours=1),
            opened_notified_at=NOW - timedelta(hours=1),
        )
        halt_sync_rewrite(row, label="FOMC 议息、CPI 公布")

        row.refresh_from_db()
        self.assertEqual(row.opened_notified_at, NOW - timedelta(hours=1))
        self.assertEqual(row.label, "FOMC 议息、CPI 公布")


def halt_sync_rewrite(row: HaltDeclaration, **over) -> None:
    """借 `halt_sync._rewrite` 改一行。参数是 `PlannedRow` 的那几个字段。"""
    from apps.regime.halt_sync import PlannedRow, _rewrite

    fields = {
        "trigger": halt.trigger_of(row),
        "scope": row.scope,
        "label": row.label,
        "reason": row.reason,
        "opened_at": row.opened_at,
        "expires_at": row.expires_at,
    }
    fields.update(over)
    _rewrite(row, PlannedRow(**fields))


# --------------------------------------------------------------------------- #
# 渲染（纯函数：收 `shadow` 而不是自己去读开关）
# --------------------------------------------------------------------------- #


class TestNotifyBody(SimpleTestCase):
    def test_the_opening_says_the_trigger_the_scope_and_the_window(self):
        row = _unpersisted(expires_at=NOW + timedelta(hours=2))
        body = halt_notify.notify_body(row, halt_notify.KIND_OPENED, shadow=False)
        lines = body.splitlines()

        self.assertEqual(
            lines[0],
            f"🔒 【{HaltTrigger.EVENT.display}】窗口已开启｜作用域 全市场（global）",
        )
        # 窗口两个时刻都走 `events.format_moment`：同一个时刻在事件库、日报与这里必须是
        # 同一句话，而读者是同一批人。
        self.assertEqual(
            lines[1],
            f"窗口：{events.format_moment(row.opened_at)}"
            f" → {events.format_moment(row.expires_at)}",
        )
        self.assertIn(row.reason, body)

    def test_the_blanket_line_says_how_it_ends_instead_of_calling_it_a_window(self):
        """保命档的行 `expires_at` 恒为 `None`（它随阶段起落），但它**不是日历窗口**
        （第③段 Q5）：说「截止时刻不定」读起来像「不知道什么时候解除」，而它的截止条件
        说得出来——阶段离开高波动。标题与正文是**同一个分岔**，所以两处都不许出现「窗口」。
        """
        row = _unpersisted(
            trigger=HaltTrigger.BLANKET, label="高波动", reason="阶段进入高波动"
        )
        body = halt_notify.notify_body(row, halt_notify.KIND_OPENED, shadow=False)

        self.assertIn("已生效", body)  # 标题：不是「窗口已开启」
        self.assertIn("生效：", body)
        self.assertIn("随阶段离开高波动而结束", body)
        self.assertNotIn("窗口", body)
        self.assertNotIn("截止时刻不定", body)

    def test_the_deactivation_line_says_the_end_is_not_a_fixed_moment(self):
        """策略停用那一档的解除条件有好几个（阶段离开 / 判据翻面 / 豁免 / 开关回 Shadow），
        一句说不完——所以只有它留着「截止时刻不定」。同样地，标题说「已生效」而正文说
        「生效：… 起」，两处不许一处分岔另一处不分岔。
        """
        row = _unpersisted(
            trigger=HaltTrigger.DEACTIVATION,
            label="本阶段不适配",
            reason="趋势里逆势策略",
        )
        body = halt_notify.notify_body(row, halt_notify.KIND_OPENED, shadow=False)

        self.assertIn("已生效", body)
        self.assertIn("截止时刻不定", body)
        self.assertNotIn("窗口", body)

    def test_an_event_window_without_a_resume_time_still_says_its_start(self):
        """事件本身没给恢复时刻（`resume_at` 空）时，说的是「起点已知、终点不定」，
        **不是**「这不是窗口」——那一档仍然是事件熔断，标题照旧说「窗口已开启」。"""
        row = _unpersisted(expires_at=None, reason="事件未给恢复时刻")
        body = halt_notify.notify_body(row, halt_notify.KIND_OPENED, shadow=False)

        self.assertIn("窗口已开启", body)
        self.assertIn("窗口：", body)
        self.assertIn("截止时刻不定", body)

    def test_a_lone_layer_gets_no_layer_roster(self):
        """只有一层时不写层集合：「集合」与「这一条」是同一个答案，写了是噪声。"""
        row = _unpersisted(expires_at=NOW + timedelta(hours=2))
        body = halt_notify.notify_body(row, halt_notify.KIND_OPENED, shadow=False)

        self.assertNotIn("层在拦", body)

    def test_the_opening_counts_the_layers_when_there_is_more_than_one(self):
        """两层以上时开启那条要报总数**且把本行算进去**（`others` 已经排掉本行）：
        CONTEXT.md:177 里用户看到的始终是一个集合，说「还有另一层」会让人以为总共两层。
        """
        row = _unpersisted(expires_at=NOW + timedelta(hours=2))
        other = _layer(label="高波动")

        body = halt_notify.notify_body(
            row, halt_notify.KIND_OPENED, shadow=False, others=(other,)
        )

        self.assertIn("此刻共 2 层在拦（本条在内）", body)
        # 层名走 `halt.HaltLayer.text`：通知里读到的层，与 `query_halt` 里查到、下单被拒时
        # 看到的那句话必须逐字同源。
        self.assertIn(other.text, body)
        self.assertIn(halt.layer_of(row).text, body)

    def test_the_closing_says_how_many_layers_are_left(self):
        """结束那条**必须说清还剩几层**（第③段 Q4）。单层世界里「本条结束」等于「恢复」，
        两层共存时它什么都不等于——把「仍有另一层在拦」说成「已恢复」是谎报，而
        「用户以为能下单了却下不出去」正是本机制反复要防的那类输出。
        """
        row = _unpersisted(closed_at=NOW, closed_reason="window_ended", expires_at=NOW)
        other = _layer(label="高波动")

        body = halt_notify.notify_body(
            row, halt_notify.KIND_CLOSED, shadow=False, others=(other,)
        )

        self.assertIn("仍被 1 层拦住", body)
        self.assertIn("开新仓仍不可用", body)
        self.assertIn(other.text, body)
        self.assertNotIn("已恢复", body)

    def test_the_closing_claims_recovery_only_when_nothing_is_left(self):
        """「恢复可用」这个断言**只在一层都不剩时**出现（Q132）：它是这条消息里唯一一处
        说「能下单了」，而那个断言错了的代价是用户去下一个必然被拒的单。"""
        row = _unpersisted(closed_at=NOW, closed_reason="window_ended", expires_at=NOW)

        body = halt_notify.notify_body(row, halt_notify.KIND_CLOSED, shadow=False)

        self.assertIn("已无任何层在拦", body)
        self.assertIn("开新仓恢复可用", body)

    def test_the_closing_says_when_and_why(self):
        row = _unpersisted(
            closed_at=NOW,
            closed_reason="window_ended",
            expires_at=NOW,
        )
        body = halt_notify.notify_body(row, halt_notify.KIND_CLOSED, shadow=False)

        self.assertIn("窗口已结束", body)
        self.assertIn(f"解除：{events.format_moment(NOW)}", body)
        self.assertIn("熔断窗口已结束", body)

    def test_the_shadow_note_is_the_first_line(self):
        """Shadow 提示**单独占第一行**（②e Q6）：不写的话「窗口已记录」会被读成「已经
        拦住了」，而 Shadow 期这两件事恰好相反——用户会以为下单被挡住了，实际没有。
        """
        row = _unpersisted()
        body = halt_notify.notify_body(row, halt_notify.KIND_OPENED, shadow=True)

        self.assertTrue(body.splitlines()[0].startswith("⚠️"))
        self.assertIn("Shadow", body.splitlines()[0])

    def test_an_executing_line_has_no_shadow_note(self):
        row = _unpersisted()
        body = halt_notify.notify_body(row, halt_notify.KIND_OPENED, shadow=False)

        self.assertNotIn("⚠️", body)

    def test_a_deactivation_close_reason_is_read_from_the_gate_word_list(self):
        """解除原因的人话**按触发源路由**（第③段 Q7）：策略停用档那五个码的词表在
        `gate_switch`，不在 `halt_sync`。

        这条钉的是一个**同名不同义**的坑：两个词表里都有 `regime_left`，而它们说的不是
        同一件事。一把抓地调 `halt_sync.close_reason_display` 不会报错、也不会返回短码
        ——它会返回另一档的文案，于是用户读到一句错话却看不出哪里不对。
        """
        # 先钉住「这个坑还在」：哪一天两个码不再同值，这条用例就该被删掉或改写，
        # 而不是变成一条永远绿的摆设。
        self.assertEqual(gate.CLOSE_REGIME_LEFT, halt_sync.CLOSE_REASON_REGIME_LEFT)

        row = _unpersisted(
            trigger=HaltTrigger.DEACTIVATION,
            closed_at=NOW,
            closed_reason=gate.CLOSE_REGIME_LEFT,
        )
        body = halt_notify.notify_body(row, halt_notify.KIND_CLOSED, shadow=False)

        self.assertIn(
            gate_switch.GATE_CLOSE_REASON_DISPLAY[gate.CLOSE_REGIME_LEFT], body
        )
        self.assertNotIn(
            halt_sync.CLOSE_REASON_DISPLAY[halt_sync.CLOSE_REASON_REGIME_LEFT], body
        )

    def test_an_event_close_reason_is_read_from_the_window_word_list(self):
        """反向：事件熔断那几档的码归 `halt_sync` 的词表——路由不能反过来把窗口消息
        也推到 gate 那一侧。"""
        row = _unpersisted(
            closed_at=NOW,
            closed_reason=halt_sync.CLOSE_REASON_REGIME_LEFT,
        )
        body = halt_notify.notify_body(row, halt_notify.KIND_CLOSED, shadow=False)

        self.assertIn(
            halt_sync.CLOSE_REASON_DISPLAY[halt_sync.CLOSE_REASON_REGIME_LEFT], body
        )
        self.assertNotIn(
            gate_switch.GATE_CLOSE_REASON_DISPLAY[gate.CLOSE_REGIME_LEFT], body
        )

    def test_the_failure_body_says_the_consequence_not_just_the_error(self):
        """要说的不是「有个任务挂了」（那件事塞不进这条消息的正文），而是「此刻没人知道
        该不该拦」——一个「表里没有窗口」的系统看起来与「现在没有事件」一模一样。"""
        body = halt_notify.write_failure_body("could not connect to server")

        self.assertIn("停止声明窗口同步失败", body)
        self.assertIn("事件熔断不可人工豁免", body)
        self.assertIn("could not connect to server", body)

    def test_the_gate_failure_body_points_at_the_way_out(self):
        """同一张表、同一个失败面，**两个档**（第③段 Q1）。差别只有一句，而那一句是
        决定性的：事件熔断那一档说「不可人工豁免」，而这一档的豁免**人可以给**
        （`manage_deactivation_exemptions`）。照抄那一句会让读的人以为无路可走。

        所以这里钉的是**分岔**本身：两段话都点名自己的身份、都说得出后果、都带上错误
        原文；而「不可人工豁免」这句只许留在事件熔断那一档，补救动作只许出现在这一档。
        """
        body = halt_notify.gate_write_failure_body("could not connect to server")

        self.assertIn("策略停用决策档", body)
        self.assertIn("manage_deactivation_exemptions", body)
        self.assertIn("could not connect to server", body)
        # 「不可人工豁免」在这一档是**假话**：说了它，读的人就不会去找那条豁免。
        self.assertNotIn("不可人工豁免", body)
        self.assertNotEqual(
            body, halt_notify.write_failure_body("could not connect to server")
        )

    def test_the_event_tier_body_does_not_point_at_the_deactivation_exemption(self):
        """反向：事件熔断那一档**不许**出现策略停用那一路的补救动作。

        两个词表各有一个同名短码那种错（`_close_reason_display`）是「看起来都正常」；
        这一条错得更钝也更要紧——它会指着一个**不存在的出路**（人工豁免不穿透保命档、
        事件熔断不可豁免），而照做的人会以为给一次豁免就把事办了。
        """
        body = halt_notify.write_failure_body("boom")

        self.assertIn("事件熔断不可人工豁免", body)
        self.assertNotIn("策略停用决策档", body)
        self.assertNotIn("manage_deactivation_exemptions", body)


class TestShadowFlag(TestCase):
    """`shadow` 由调用方给，但**给出它的那个换算**必须与读侧同一处（`halt.switch_open`）：
    各查一遍就是给「哪个开关管哪条线」造第二个答案。
    """

    def test_it_is_read_from_the_switch_table(self):
        event = _unpersisted()
        _switch(MechanismKind.EVENT_BREAKER, MechanismMode.EXECUTING)
        self.assertFalse(halt_notify._shadowed(event))

    def test_no_switch_row_means_shadow(self):
        """开关表的默认档是 Shadow（`RegimeMechanismSwitch.current` 查不到行时返回它），
        所以「没记录」与「明确切到 Shadow」在求值上是同一件事。"""
        self.assertTrue(halt_notify._shadowed(_unpersisted()))

    def test_the_blanket_line_has_no_switch_and_is_never_shadow(self):
        """`HALT_TRIGGER_SWITCH[BLANKET] = None`：保命档没有开关、永远作数
        （「高波动算生效」）。**这条行为不得改变**，所以在这里钉死。"""
        self.assertFalse(
            halt_notify._shadowed(_unpersisted(trigger=HaltTrigger.BLANKET))
        )


# --------------------------------------------------------------------------- #
# 投递一轮：送达了才记账
# --------------------------------------------------------------------------- #


class TestNotifyPending(TestCase):
    def setUp(self):
        self.user = _user("live@t.local")
        _session(self.user)

    def test_delivered_rows_are_marked_and_counted(self):
        row = _declaration(opened_at=NOW - timedelta(minutes=5))
        with _notify(True) as mock:
            summary = halt_notify.notify_pending(now=NOW)

        self.assertEqual(
            summary, {"opened": 1, "closed": 0, "failed": 0, "no_recipients": 0}
        )
        self.assertEqual(mock.await_count, 1)
        self.assertEqual(mock.call_args.args[0], str(self.user.pk))
        row.refresh_from_db()
        self.assertEqual(row.opened_notified_at, NOW)

    def test_the_second_round_does_nothing(self):
        """记号就是幂等：300 秒一轮的重跑不该重复发同一句话。"""
        _declaration(opened_at=NOW - timedelta(minutes=5))
        with _notify(True):
            halt_notify.notify_pending(now=NOW)
        with _notify(True) as mock:
            second = halt_notify.notify_pending(now=NOW + timedelta(minutes=5))

        self.assertEqual(
            second, {"opened": 0, "closed": 0, "failed": 0, "no_recipients": 0}
        )
        self.assertEqual(mock.await_count, 0)

    def test_no_recipient_is_terminal_and_counted_separately(self):
        """空收件人**即时记terminal账**：不记的话，一个没人在听的窗口会每 300 秒重试
        一整段窗口期。它是终局的「没人受影响」，与「推送失败」在记不记账上恰好相反。
        """
        row = _declaration(scope=halt.symbol_scope("DOGE/USDT"), opened_at=NOW)
        with _notify(True) as mock:
            summary = halt_notify.notify_pending(now=NOW)

        self.assertEqual(summary["no_recipients"], 1)
        self.assertEqual(summary["failed"], 0)
        self.assertEqual(mock.await_count, 0)
        row.refresh_from_db()
        self.assertEqual(row.opened_notified_at, NOW)

    def test_a_failed_push_is_not_marked_and_is_retried(self):
        """`notify_user` 返回 `False`（推送通路失败）是**可重试**的：不记账，下一轮再发。
        与「收件人为空」相反——那一件是终局的。
        """
        row = _declaration(opened_at=NOW - timedelta(minutes=5))
        with _notify(False) as mock:
            first = halt_notify.notify_pending(now=NOW)

        self.assertEqual(first["failed"], 1)
        self.assertEqual(first["opened"], 0)
        row.refresh_from_db()
        self.assertIsNone(row.opened_notified_at)

        with _notify(True):
            second = halt_notify.notify_pending(now=NOW + timedelta(minutes=5))
        self.assertEqual(second["opened"], 1)
        self.assertEqual(mock.await_count, 1, "重试的那一轮只发一次")

    def test_a_partial_push_counts_as_failed(self):
        """部分成功时那些已经收到的人会再收到一遍——账是行级的一对时刻，记不出「哪几个人
        收到了」。为一条 300 秒重试的窗口通知开一张按人的表不值（`halt_notify` docstring）。
        """
        _session(_user("second@t.local"))
        row = _declaration(opened_at=NOW - timedelta(minutes=5))
        with _notify([True, False]):
            summary = halt_notify.notify_pending(now=NOW)

        self.assertEqual(summary["failed"], 1)
        row.refresh_from_db()
        self.assertIsNone(row.opened_notified_at)

    def test_a_short_window_gets_both_notices_in_one_round_in_order(self):
        """开与关落在同一轮里时，「开启」在「结束」之前——为此结束那一批的查询在开窗那批
        **写完之后**才发（`pending_closed` 要求开窗已记账，这条谓词本身就是那个顺序）。
        """
        _declaration(
            opened_at=NOW - timedelta(minutes=5),
            closed_at=NOW - timedelta(minutes=4),
            closed_reason="window_ended",
        )
        with _notify(True) as mock:
            summary = halt_notify.notify_pending(now=NOW)

        self.assertEqual(summary["opened"], 1)
        self.assertEqual(summary["closed"], 1)
        first, second = _texts(mock)
        self.assertIn("窗口已开启", first)
        self.assertIn("窗口已结束", second)

    def test_the_notice_carries_the_shadow_note_while_the_line_is_shadow(self):
        """②e Q6：Shadow 期尤其要说——用户会以为下单已经被挡住了。"""
        _declaration(opened_at=NOW - timedelta(minutes=5))
        with _notify(True) as mock:
            halt_notify.notify_pending(now=NOW)

        self.assertTrue(_texts(mock)[0].startswith("⚠️"))

        _switch(MechanismKind.EVENT_BREAKER, MechanismMode.EXECUTING)
        _declaration(
            scope=halt.symbol_scope("BTC/USDT"), opened_at=NOW - timedelta(minutes=5)
        )
        with _notify(True) as mock:
            halt_notify.notify_pending(now=NOW)

        self.assertFalse(_texts(mock)[0].startswith("⚠️"))

    def test_a_closing_notice_names_the_layer_that_still_blocks(self):
        """两层共存时关掉一层（第③段 Q4）→ 用户在消息里读到「仍被 1 层拦住」并看到是
        哪一层。**这条走真库**：`still_blocking` 从受众算起，口径与收件人同源。

        保命档那一行不需要开关（`HALT_TRIGGER_SWITCH[BLANKET] = None`），所以这条用例
        里的事件熔断声明**不开开关**也照样成立——上面那层是关掉的那条，它已经解除。
        """
        _declaration(
            opened_at=NOW - timedelta(hours=2),
            closed_at=NOW - timedelta(minutes=5),
            closed_reason="window_ended",
            opened_notified_at=NOW - timedelta(hours=2),
        )
        _declaration(
            trigger=HaltTrigger.BLANKET,
            label="高波动",
            reason="阶段进入高波动",
            opened_at=NOW - timedelta(hours=3),
            opened_notified_at=NOW - timedelta(hours=3),
        )

        with _notify(True) as mock:
            summary = halt_notify.notify_pending(now=NOW)

        self.assertEqual(summary["closed"], 1)
        self.assertEqual(mock.await_count, 1)
        body = _texts(mock)[0]
        self.assertIn("仍被 1 层拦住", body)
        self.assertIn("高波动（保命档（高波动），作用域 全市场（global））", body)
        self.assertNotIn("已恢复", body)

    def test_a_layer_outside_the_audience_is_not_counted_against_them(self):
        """Q3 的 (c)：层集合按**这批人**求闭包，不按全表。

        收件人只跑 BTC，而表里此刻有一条 `symbol:SOL/USDT` 的层。按全表报会说出
        「仍被〈SOL 解锁〉拦住」——那是一句对收件人为假的话，而这条消息的全部意义就是
        不撒谎（假话与真话在这里长得一模一样，所以只有用例能分开它们）。
        """
        _switch(MechanismKind.EVENT_BREAKER, MechanismMode.EXECUTING)
        _declaration(
            scope=halt.symbol_scope("BTC/USDT"),
            label="BTC 解锁",
            opened_at=NOW - timedelta(hours=2),
            closed_at=NOW - timedelta(minutes=5),
            closed_reason="window_ended",
            opened_notified_at=NOW - timedelta(hours=2),
        )
        _declaration(
            scope=halt.symbol_scope("SOL/USDT"),
            label="SOL 解锁",
            opened_at=NOW - timedelta(hours=1),
            opened_notified_at=NOW - timedelta(minutes=30),
        )

        with _notify(True) as mock:
            summary = halt_notify.notify_pending(now=NOW)

        self.assertEqual(summary["closed"], 1)
        body = _texts(mock)[0]
        self.assertNotIn("SOL", body)
        self.assertIn("已无任何层在拦", body)

    def test_a_layer_the_recipient_really_hits_is_counted(self):
        """同一张表、同一条 `symbol:SOL/USDT` 的层，落在**真的跑 SOL 的人**身上就是一层
        ——哪怕这条消息说的是 BTC 那个作用域。

        两句断言的落点是**账户**（「开新仓仍不可用」/「开新仓恢复可用」），而一个人可以
        同时跑几个会话：只按本行作用域上那几条会话算，会对一个在 SOL 上仍被拦住的人说出
        「恢复可用」。所以闭包按**这些人的全部会话**算，收件人按本行作用域算。
        """
        _switch(MechanismKind.EVENT_BREAKER, MechanismMode.EXECUTING)
        _session(self.user, symbol="SOL/USDT")
        _declaration(
            scope=halt.symbol_scope("BTC/USDT"),
            label="BTC 解锁",
            opened_at=NOW - timedelta(hours=2),
            closed_at=NOW - timedelta(minutes=5),
            closed_reason="window_ended",
            opened_notified_at=NOW - timedelta(hours=2),
        )
        _declaration(
            scope=halt.symbol_scope("SOL/USDT"),
            label="SOL 解锁",
            opened_at=NOW - timedelta(hours=1),
            opened_notified_at=NOW - timedelta(minutes=30),
        )

        with _notify(True) as mock:
            halt_notify.notify_pending(now=NOW)

        self.assertEqual(mock.await_count, 1, "两个会话属于同一个人，只该收到一条")
        body = _texts(mock)[0]
        self.assertIn("仍被 1 层拦住", body)
        self.assertIn("SOL 解锁", body)
        self.assertNotIn("已恢复", body)

    def test_the_opening_does_not_list_itself_twice(self):
        """`_others` 必须排掉本行：**开启那一批里本行正在拦**（它就是刚生效的那一层），
        不排的话用户读到的是「此刻共 2 层在拦：A；A」——两个来源各自都对，合起来是一句错话。
        """
        _switch(MechanismKind.EVENT_BREAKER, MechanismMode.EXECUTING)
        _declaration(
            trigger=HaltTrigger.BLANKET,
            label="高波动",
            reason="阶段进入高波动",
            opened_at=NOW - timedelta(hours=3),
            opened_notified_at=NOW - timedelta(hours=3),
        )
        _declaration(opened_at=NOW - timedelta(minutes=5))

        with _notify(True) as mock:
            summary = halt_notify.notify_pending(now=NOW)

        self.assertEqual(summary["opened"], 1)
        body = _texts(mock)[0]
        self.assertIn("此刻共 2 层在拦（本条在内）", body)
        self.assertEqual(body.count("FOMC 议息（"), 1)


# --------------------------------------------------------------------------- #
# 声明写入失败：全体 is_active，发完再抛
# --------------------------------------------------------------------------- #


class TestWriteFailureAlert(TestCase):
    def setUp(self):
        _clear_the_field()

    def test_it_goes_to_every_active_user(self):
        """机制自身健康 → 全体 `is_active` 用户（CONTEXT.md:66 的两类落点之一）。

        它不是「你的单出事了」，而是「机制现在说不出自己在拦什么了」，而这件事对所有人
        都有后果——所以受众与窗口消息不同，与 `reduce_run` 的「未归属账户」同一取法。
        """
        first, second = _user("a@t.local"), _user("b@t.local")
        _user("gone@t.local", active=False)

        with _notify(True) as mock:
            summary = halt_notify.alert_declaration_write_failure(
                RuntimeError("表写不进去")
            )

        self.assertEqual(summary, {"recipients": 2, "delivered": 2})
        self.assertEqual(mock.await_count, 2)
        self.assertEqual(
            {call.args[0] for call in mock.call_args_list},
            {str(first.pk), str(second.pk)},
        )

    def test_the_body_names_the_error_and_its_consequence(self):
        _user("a@t.local")
        with _notify(True) as mock:
            halt_notify.alert_declaration_write_failure(RuntimeError("表写不进去"))

        body = _texts(mock)[0]
        self.assertIn("停止声明窗口同步失败", body)
        self.assertIn("表写不进去", body)

    def test_an_undelivered_alert_only_logs(self):
        """②e Q8：不递归告警。**但它绝不吞掉原异常**——这里只返回计数，替换掉异常是
        `tasks.sync_halt_windows` 那边「发完再抛」的事（那个用例在下面）。
        """
        _user("a@t.local")
        with _notify(False):
            summary = halt_notify.alert_declaration_write_failure(RuntimeError("boom"))

        self.assertEqual(summary, {"recipients": 1, "delivered": 0})

    def test_the_gate_alert_goes_to_every_active_user_with_its_own_body(self):
        """策略停用决策档那一条（第③段 Q1）：受众与流程与上一条一模一样，**只有正文
        不同**。受众写错的后果是「机制说不出它在按阶段停谁」只讲给恰好有实盘会话的人听，
        而那件事对每个人都有后果。
        """
        first, second = _user("a@t.local"), _user("b@t.local")

        with _notify(True) as mock:
            summary = halt_notify.alert_gate_write_failure(RuntimeError("表写不进去"))

        self.assertEqual(summary, {"recipients": 2, "delivered": 2})
        self.assertEqual(
            {call.args[0] for call in mock.call_args_list},
            {str(first.pk), str(second.pk)},
        )
        body = _texts(mock)[0]
        self.assertIn("策略停用决策档", body)
        self.assertIn("表写不进去", body)
        self.assertIn("manage_deactivation_exemptions", body)


# --------------------------------------------------------------------------- #
# 接线：三条消息挂在同一条任务上，各自在对的时机
# --------------------------------------------------------------------------- #


class TestTheTaskWiring(TestCase):
    """`tasks.sync_halt_windows` 是声明表唯一的写入方，所以三条消息都挂在它身上。

    这里把三个协作者换成假的，只钉**时机与顺序**——它们各自的内容由上面的用例负责。
    """

    def _patch(self, **over):
        from contextlib import ExitStack

        stack = ExitStack()
        defaults = {
            "apps.regime.halt_sync.sync": lambda *a, **kw: {
                "created": 0,
                "updated": 0,
                "unchanged": 0,
                "closed": 0,
            },
            "apps.regime.halt_notify.notify_pending": lambda *a, **kw: {
                "opened": 0,
                "closed": 0,
                "failed": 0,
                "no_recipients": 0,
            },
            "apps.regime.reduce_run.dispatch": lambda *a, **kw: {"rows": 0},
            "apps.regime.halt_notify.alert_declaration_write_failure": lambda exc: {
                "recipients": 0,
                "delivered": 0,
            },
        }
        defaults.update(over)
        for target, replacement in defaults.items():
            stack.enter_context(patch(target, replacement))
        return stack

    def test_the_notice_goes_between_sync_and_dispatch(self):
        """窗口通知在 `sync()` 之后（「窗口开着」这个事实的写入方是它）、`dispatch()`
        之前（通知与减仓互不相干，②e Q5）。

        顺序写反的表现是「窗口开的那一轮通知晚一轮」——而窗口通知的全部意义就是及时。
        """
        from apps.regime import tasks

        order: list[str] = []
        with self._patch(
            **{
                "apps.regime.halt_sync.sync": lambda *a, **kw: (
                    order.append("sync") or {}
                ),
                "apps.regime.halt_notify.notify_pending": lambda *a, **kw: (
                    order.append("notify") or {}
                ),
                "apps.regime.reduce_run.dispatch": lambda *a, **kw: (
                    order.append("dispatch") or {"rows": 3}
                ),
            }
        ):
            summary = tasks.sync_halt_windows.run()

        self.assertEqual(order, ["sync", "notify", "dispatch"])
        self.assertEqual(summary["reduce_rows"], 3)
        self.assertIn("window_notify", summary)

    def test_the_notice_is_not_behind_the_reduce_gate(self):
        """②d 那道闸门问的是「这一轮要不要真对市场动手」，而「窗口开了」永远要说。

        这里让 `dispatch` 抛——通知不该因此受影响，因为它在它**之前**。
        """
        from apps.regime import tasks

        sent: list[str] = []
        with self._patch(
            **{
                "apps.regime.halt_notify.notify_pending": lambda *a, **kw: (
                    sent.append("sent") or {}
                ),
                "apps.regime.reduce_run.dispatch": lambda *a, **kw: (
                    _ for _ in ()
                ).throw(RuntimeError("减仓那边炸了")),
            }
        ):
            with self.assertRaises(RuntimeError):
                tasks.sync_halt_windows.run()
        self.assertEqual(sent, ["sent"])

    def test_a_write_failure_alerts_and_then_still_raises(self):
        """②e Q7：catch → 发 → 再抛。

        吞掉异常会让真实故障连 FAILURE 那条记录都没有——「任务在跑、但表没更新」会变成
        一件看起来正常的事。而失败的是机制本身，对账没完成就该让任务标 FAILURE。
        """
        from apps.regime import tasks

        alerted: list[Exception] = []

        def boom(*_a, **_kw):
            raise RuntimeError("表写不进去")

        with self._patch(
            **{
                "apps.regime.halt_sync.sync": boom,
                "apps.regime.halt_notify.alert_declaration_write_failure": lambda exc: (
                    alerted.append(exc) or {"recipients": 1, "delivered": 1}
                ),
            }
        ):
            with self.assertRaises(RuntimeError):
                tasks.sync_halt_windows.run()

        self.assertEqual(len(alerted), 1)
        self.assertIsInstance(alerted[0], RuntimeError)
        self.assertIn("表写不进去", str(alerted[0]))

    def test_a_failed_round_neither_notifies_nor_reduces(self):
        """写入失败的那一轮，表是旧的——按旧表发窗口消息与减仓都是在拿过期的事实说话。"""
        from apps.regime import tasks

        def boom(*_a, **_kw):
            raise RuntimeError("表写不进去")

        touched: list[str] = []
        with self._patch(
            **{
                "apps.regime.halt_sync.sync": boom,
                "apps.regime.halt_notify.notify_pending": lambda *a, **kw: (
                    touched.append("notify")
                ),
                "apps.regime.reduce_run.dispatch": lambda *a, **kw: touched.append(
                    "dispatch"
                ),
            }
        ):
            with self.assertRaises(RuntimeError):
                tasks.sync_halt_windows.run()

        self.assertEqual(touched, [])

    def test_the_gate_task_uses_the_gate_body_and_never_the_event_one(self):
        """`sync_gate` 的失败面与 `sync_halt_windows` 同构（catch → 发 → 再抛），**只有那
        一档的正文不同**（第③段 Q1）——所以这里同时钉两件事：它走的是 gate 那一条，且它
        **不走**事件熔断那一条。

        走错的表现很具体：一条说「事件熔断不可人工豁免、请人工核对事件表与停止声明表」的
        消息，落在一个「人可以给在期豁免」的故障上——读的人会去找事件表，而那条路是对的
        地方没有这一档的解法。
        """
        from apps.regime import tasks

        gate_alerted: list[Exception] = []
        event_alerted: list[Exception] = []

        def boom(*_a, **_kw):
            raise RuntimeError("表写不进去")

        with self._patch(
            **{
                "apps.regime.gate_run.sync": boom,
                "apps.regime.halt_notify.alert_gate_write_failure": lambda exc: (
                    gate_alerted.append(exc) or {"recipients": 1, "delivered": 1}
                ),
                "apps.regime.halt_notify.alert_declaration_write_failure": lambda exc: (
                    event_alerted.append(exc) or {"recipients": 1, "delivered": 1}
                ),
            }
        ):
            with self.assertRaises(RuntimeError):
                tasks.sync_gate.run()

        self.assertEqual(len(gate_alerted), 1)
        self.assertIn("表写不进去", str(gate_alerted[0]))
        self.assertEqual(event_alerted, [])
