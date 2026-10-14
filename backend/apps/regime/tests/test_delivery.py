"""日报的投递与投递看门狗（第①段单元 8iv）。

`test_report.py` 钉的是「一天一条写成什么样」；这个文件钉的是它**送不送得到**，以及
「送不到」这件事谁来发现。这里不重复断言日报的内容——那是 `test_report.py` 的事。

## 两条互相独立的路径（CONTEXT.md:175）

- **投递**（`report.deliver_daily_report`）负责把那一份送到每个人手上，只做这一件事；
- **看门狗**（`report.check_report_delivery`）负责**看它送到没有**，且在连续失败时升级告警。

合成一条就等于让看门狗去报自己的失败——它恰好是失败的那一个时，它不会响。所以下面两个
测试类刻意互不调用：`TestTheWatchdog` 里的日报行是**手搓的**，不经投递那条路。

## 看门狗的核心不变量

**它不能靠日报自己告警**（日报发不出去时它报不了自己），所以看门狗读的是 `DailyReport`
表本身；而它自己**只读**——`test_it_writes_nothing_back` 是这条的强制手段。它唯一的
副作用是往外发一条消息，同一运行日只成功发一次。

## 为什么「截止时刻之前什么都不说」要有两条用例

早判一轮的代价是**每天必然出现一条假告警**，而假告警的真实代价是真告警跟着一起被忽略。
所以既钉「到点之前不响」，也钉它的反面——「刚过点也不响」：截止那一刻心跳才被允许写日报，
投递还要等下一轮，同刻去判就是在抢它盯的那个写入者（`WATCHDOG_GRACE` 的全部理由）。

## 分层

全部是 `TestCase`（这一层要落真库、要跑任务体，`SimpleTestCase` 会当场炸）。投递口一律
换成 `AsyncMock`：真发一条 Telegram 消息不是测试该干的事，而 `notify_user` 正是
`test_daily_snapshot.py` 钉过的那个替换面。
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.common.time_utils import business_tz
from apps.regime import report
from apps.regime.models import DailyReport
from apps.regime.tests.test_shadow import (
    RUN_DAY,
    SYMBOL,
    concluded_payload,
    derivation,
    suggestion,
)
from apps.trading.models import LiveSession, Strategy

#: 判定机制的名义时刻：北京 2026-09-22 08:00（= UTC 00:00）。
NOW = datetime.combine(RUN_DAY, time(8, 0), tzinfo=business_tz())
#: 日报截止时刻 = `REPORT.watchdog_hour:watchdog_minute`（北京 09:00）。看门狗还要再加
#: `WATCHDOG_GRACE` 才动手，所以下面三个时刻分别落在「截止前」「宽限内」「宽限外」。
DEADLINE = datetime.combine(RUN_DAY, time(9, 0), tzinfo=business_tz())
INSIDE_GRACE = DEADLINE + timedelta(minutes=1)
AFTER_GRACE = DEADLINE + report.WATCHDOG_GRACE + timedelta(minutes=1)

#: 投递口（本仓库唯一的出站通知口）的替换面。函数内 import，所以打在源模块上即可。
NOTIFY = "apps.trading.alerts.notify_user"


def _notify(delivered=True):
    """一个假的出站口。`delivered` 可以是 bool（全体同命）或一串 bool（逐人不同）。"""
    if isinstance(delivered, bool):
        return patch(NOTIFY, AsyncMock(return_value=delivered))
    return patch(NOTIFY, AsyncMock(side_effect=list(delivered)))


def _user(email: str, *, active: bool = True):
    """造一个用户。`is_active` 不在 `create_user` 的签名里（自定义 UserManager 只收三个
    位置参数），所以停用要另走一步 `save`。"""
    User = get_user_model()
    user = User.objects.create_user(
        email=email, username=email.split("@")[0], password="pw12345"
    )
    if not active:
        user.is_active = False
        user.save(update_fields=["is_active"])
    return user


def _users(n: int) -> list:
    return [_user(f"delivery{i}@test.local") for i in range(n)]


def _row(run_day: date = RUN_DAY, **over) -> DailyReport:
    """一份**手搓的**日报行。看门狗的用例只关心投递那几列，与怎么生成的无关。"""
    fields = {"symbol": SYMBOL, "run_day": run_day, "sections": {}}
    fields.update(over)
    return DailyReport.objects.create(**fields)


def _clear_the_field() -> None:
    """把库里**已有的**活跃用户先请出场。

    每个测试库都带着两条数据迁移种下的系统用户（`admin@` / `scheduler@tradeclaw.local`），
    而受众是「全库所有 `is_active` 用户」——不请他们出场，`await_count` 就永远比自己造的
    人多两个，用例会以一个与它无关的数字失败。**这是测试要把场地清空，不是生产要排除谁**：
    CONTEXT.md:173 的受众定义就是所有 `is_active` 用户，系统用户也照着算。

    `TestCase` 会回滚，这里改的 `is_active` 不会外泄到别的用例。
    """
    get_user_model().objects.filter(is_active=True).update(is_active=False)


def _sent(mock, user) -> str:
    """这个用户收到的正文（没收到就是空串）。"""
    for args, _kwargs in mock.call_args_list:
        if args[0] == str(user.pk):
            return args[1]
    return ""


# --------------------------------------------------------------------------- #
# 投递
# --------------------------------------------------------------------------- #


class TestTheDeliverer(TestCase):
    def setUp(self):
        _clear_the_field()

    def test_it_sends_to_every_active_user(self):
        first, second = _users(2)
        _row()
        with _notify(True) as mock:
            summary = report.deliver_daily_report(now=AFTER_GRACE)
        self.assertEqual(mock.await_count, 2)
        for user in (first, second):
            self.assertTrue(_sent(mock, user), "每个人都该收到正文")
        self.assertTrue(summary["delivered"])
        self.assertEqual(summary["delivered_count"], 2)

    def test_an_inactive_user_is_not_a_recipient(self):
        """停用的人不算受众——`is_active` 就是这张名单的全部依据（CONTEXT.md:173）。"""
        live = _user("live@test.local")
        gone = _user("gone@test.local", active=False)
        _row()
        with _notify(True) as mock:
            report.deliver_daily_report(now=AFTER_GRACE)
        self.assertTrue(_sent(mock, live))
        self.assertEqual(_sent(mock, gone), "")

    def test_each_user_gets_their_own_cropped_body(self):
        """正文按人裁剪——**这正是正文不落库的理由**（同一份 `sections` 渲染出不同正文）。

        裁的是活跃会话（`LiveSession.ACTIVE_STATUSES`），不是策略的 `is_active`。
        这里刻意造一份**真的比对得出变化**的日报（昨天那份的清单是空的）：只有变化段
        才有可裁的东西，`first`/`blocked`/`unchanged` 三种收场是全员同一句话。
        """
        mine, theirs = _users(2)
        strategy = Strategy.objects.create(name="甲", code_path="/t/a.py")
        LiveSession.objects.create(
            user=mine,
            strategy=strategy,
            symbol="BTC/USDT",
            mode="paper",
            status="running",
            initial_capital=Decimal("10000.00"),
        )
        _row(
            RUN_DAY - timedelta(days=1),
            landscape={"by_regime": {}, "blocked": ""},
        )
        report.write_daily_report(
            concluded_payload(), derivation(suggestion(str(strategy.id))), {}, now=NOW
        )
        with _notify(True) as mock:
            report.deliver_daily_report(now=AFTER_GRACE)

        title = report._SECTION_TITLES[report.SECTION_CHANGE]
        self.assertIn(title, _sent(mock, mine), "在跑这个策略的人该看到停用段")
        self.assertNotIn(
            title,
            _sent(mock, theirs),
            "没在跑它的人不该看到——**整节不出现**，不是「无内容」",
        )

    def test_the_ledger_keys_are_strings(self):
        """明细是 JSON，键只能是字符串。UUID 直接塞进去会在 `save()` 时才炸。"""
        (user,) = _users(1)
        _row()
        with _notify(True):
            report.deliver_daily_report(now=AFTER_GRACE)
        row = DailyReport.objects.get(symbol=SYMBOL, run_day=RUN_DAY)
        self.assertEqual(set(row.delivery), {str(user.pk)})
        self.assertTrue(row.delivery[str(user.pk)]["ok"])

    def test_a_successful_round_stamps_delivered_at(self):
        _users(1)
        _row()
        with _notify(True):
            report.deliver_daily_report(now=AFTER_GRACE)
        row = DailyReport.objects.get(symbol=SYMBOL, run_day=RUN_DAY)
        self.assertEqual(row.delivered_at, AFTER_GRACE)
        self.assertEqual(row.delivery_attempts, 1)
        self.assertEqual(row.delivery_error, "")

    def test_a_failed_round_keeps_delivered_at_empty_and_says_why(self):
        _users(1)
        _row()
        with _notify(False):
            summary = report.deliver_daily_report(now=AFTER_GRACE)
        row = DailyReport.objects.get(symbol=SYMBOL, run_day=RUN_DAY)
        self.assertIsNone(
            row.delivered_at, "一格没送达就不算送达——这是第⑤段与看门狗的判据"
        )
        self.assertEqual(row.delivery_attempts, 1)
        self.assertTrue(row.delivery_error)
        self.assertFalse(summary["delivered"])
        self.assertIn("未送达", summary["error"])

    def test_a_second_round_only_retries_the_ones_that_missed(self):
        """明细是**只增不改**的账：已成功的人不重投。

        重投一份已经到手的日报是纯骚扰，而骚扰的结果是他不再看日报——那正是「必发」
        要避免的。所以第二轮的收件人恰好是那几个 `ok=False` 的格子。
        """
        first, second = _users(2)
        _row()
        with _notify([True, False]) as mock1:
            report.deliver_daily_report(now=AFTER_GRACE)
        self.assertEqual(mock1.await_count, 2)

        with _notify(True) as mock2:
            summary = report.deliver_daily_report(now=AFTER_GRACE)
        self.assertEqual(mock2.await_count, 1, "只该重投没收到的那一个")
        self.assertEqual(mock2.call_args_list[0][0][0], str(second.pk))
        self.assertEqual(_sent(mock2, first), "", "已经收到过的人不该再收到一次")
        self.assertEqual(summary["delivered_count"], 2)
        self.assertTrue(summary["delivered"])
        self.assertEqual(DailyReport.objects.get(run_day=RUN_DAY).delivery_attempts, 2)

    def test_an_already_delivered_report_is_left_alone(self):
        _users(1)
        _row(delivery={"x": {"ok": True}}, delivered_at=NOW)
        with _notify(True) as mock:
            summary = report.deliver_daily_report(now=AFTER_GRACE)
        self.assertEqual(mock.await_count, 0)
        self.assertTrue(summary["delivered"])
        self.assertEqual(summary["reason"], "already_delivered")

    def test_an_empty_audience_is_not_a_delivered_report(self):
        """**空集不能算成功**：一份永远显示「已投递」的投递报告，比不报还坏。

        `delivered_at` 只在「明细至少有一格、且每格都 ok」时才落，所以「无处可投」既
        不盖送达章，也不推轮次——把空集记成第 N 轮会让第⑤段那句「已尝试 N 轮」变成一句
        自己都解释不了的数字。
        """
        _row()
        with _notify(True) as mock:
            summary = report.deliver_daily_report(now=AFTER_GRACE)
        self.assertEqual(mock.await_count, 0)
        row = DailyReport.objects.get(run_day=RUN_DAY)
        self.assertIsNone(row.delivered_at)
        self.assertEqual(row.delivery_attempts, 0)
        self.assertFalse(summary["delivered"])
        self.assertIn("无处可投", row.delivery_error)

    def test_a_missing_report_is_not_an_error(self):
        """今天那份还没写出来（判定没结论、又没到截止时刻）——**不是失败**，是「还早」。"""
        _users(1)
        with _notify(True) as mock:
            summary = report.deliver_daily_report(now=NOW)
        self.assertEqual(mock.await_count, 0)
        self.assertFalse(summary["attempted"])
        self.assertEqual(summary["reason"], "no_report")

    def test_the_run_day_argument_picks_which_day_to_deliver(self):
        """手动补投某一天：入参是运行日，不是「日报摘要」——投递与日报怎么写的形状解耦。"""
        _users(1)
        yesterday = RUN_DAY - timedelta(days=1)
        _row(yesterday)
        with _notify(True) as mock:
            summary = report.deliver_daily_report(
                yesterday.isoformat(), now=AFTER_GRACE
            )
        self.assertEqual(mock.await_count, 1)
        self.assertEqual(summary["run_day"], yesterday.isoformat())
        self.assertEqual(
            DailyReport.objects.get(run_day=yesterday).delivery_attempts, 1
        )


# --------------------------------------------------------------------------- #
# 看门狗
# --------------------------------------------------------------------------- #


class TestTheWatchdog(TestCase):
    """看门狗**只读**、只在日报没到时响、且同一运行日只成功响一次。

    `_alerted_on` 是模块级的一本账，跨用例活着的——不在这里清掉，第二条用例会看到第一条
    记下的日子（进程重启会清零，见它的 docstring，代价是重新喊一遍）。
    """

    def setUp(self):
        _clear_the_field()
        report._alerted_on.clear()

    # -- 什么时候**不**响 ---------------------------------------------------- #

    def test_it_says_nothing_before_the_deadline(self):
        """到点之前一个字都不说。**这是 CONTEXT.md:175 直接禁止删掉的那条护栏**：
        少它一轮，机制会在日报本来还没到点的时刻宣布它没到。"""
        _users(1)
        with _notify(True) as mock:
            summary = report.check_report_delivery(now=DEADLINE)
        self.assertEqual(mock.await_count, 0)
        self.assertFalse(summary["checked"])
        self.assertFalse(summary["escalated"])
        self.assertEqual(summary["reason"], "before_deadline")

    def test_it_does_not_race_the_report_that_has_just_become_ready(self):
        """刚过截止时刻也不响——**这是上一条的反面，也是 `WATCHDOG_GRACE` 的全部理由**。

        截止那一刻心跳才被允许写日报，投递还要等下一轮投递任务；同刻去判等于让看门狗
        去抢它盯的那个写入者，而抢跑的代价是**每天必然出现**一条假告警。
        """
        _users(1)
        _row()
        with _notify(True) as mock:
            summary = report.check_report_delivery(now=INSIDE_GRACE)
        self.assertEqual(mock.await_count, 0)
        self.assertFalse(summary["escalated"])

    def test_it_stays_quiet_when_the_report_was_delivered(self):
        _users(1)
        _row(delivery={"x": {"ok": True}}, delivered_at=NOW, delivery_attempts=1)
        with _notify(True) as mock:
            summary = report.check_report_delivery(now=AFTER_GRACE)
        self.assertEqual(mock.await_count, 0)
        self.assertTrue(summary["checked"])
        self.assertFalse(summary["escalated"])
        self.assertEqual(summary["reason"], "delivered")

    def test_no_recipients_is_not_an_alert(self):
        """「告警」是给**某个具体的人**的即时消息。没有人可给时它就不是一条告警，
        也不该被记进 `_alerted_on`——将来有人了还得能响。"""
        _row()
        with _notify(True) as mock:
            summary = report.check_report_delivery(now=AFTER_GRACE)
        self.assertEqual(mock.await_count, 0)
        self.assertFalse(summary["escalated"])
        self.assertEqual(summary["reason"], "no_recipients")
        self.assertFalse(report._alerted_on)

    def test_an_inactive_user_is_not_a_recipient(self):
        _user("live2@test.local")
        _user("gone2@test.local", active=False)
        _row()
        with _notify(True) as mock:
            summary = report.check_report_delivery(now=AFTER_GRACE)
        self.assertEqual(summary["recipients"], 1)
        self.assertEqual(mock.await_count, 1)

    # -- 什么时候响 ---------------------------------------------------------- #

    def test_a_report_that_was_never_generated_is_reported_as_such(self):
        """「没生成」与「生成了没投出去」是两条不同的故障路径，读到的人要去查的地方不同。"""
        _users(1)
        with _notify(True) as mock:
            summary = report.check_report_delivery(now=AFTER_GRACE)
        self.assertTrue(summary["escalated"])
        self.assertEqual(summary["reason"], "no_report")
        text = mock.call_args_list[0][0][1]
        self.assertIn("根本没有生成", text)

    def test_an_undelivered_report_is_reported_as_such(self):
        _users(1)
        _row(
            delivery={"x": {"ok": False, "at": "", "error": "出站通知口返回未送达"}},
            delivery_attempts=3,
            delivery_error="1/1 人未送达：出站通知口返回未送达",
        )
        with _notify(True) as mock:
            summary = report.check_report_delivery(now=AFTER_GRACE)
        self.assertTrue(summary["escalated"])
        self.assertEqual(summary["reason"], "undelivered")
        text = mock.call_args_list[0][0][1]
        self.assertIn("已尝试 3 轮", text)
        self.assertIn("出站通知口返回未送达", text)

    def test_the_alert_says_where_it_came_from(self):
        """它必须自报家门：这条消息来自**独立的看门狗**，不是日报自己。

        没有这句话，读者会以为日报还能在自己发不出去的时候说话——那正好是这条通路要
        防的那个误会。
        """
        _users(1)
        with _notify(True) as mock:
            report.check_report_delivery(now=AFTER_GRACE)
        self.assertIn("独立的投递看门狗", mock.call_args_list[0][0][1])

    def test_every_recipient_gets_the_same_text(self):
        """这条消息说的是**机制自己的状态**，不是「你的策略怎么了」——所以不裁剪。

        与投递那边正好相反（那边逐人不同），这个差别是刻意的。
        """
        _users(3)
        _row()
        with _notify(True) as mock:
            summary = report.check_report_delivery(now=AFTER_GRACE)
        texts = {args[1] for args, _ in mock.call_args_list}
        self.assertEqual(len(texts), 1)
        self.assertEqual(summary["recipients"], 3)
        self.assertEqual(summary["delivered_count"], 3)

    def test_it_alerts_at_most_once_a_day(self):
        """五分钟一轮，逐轮告警会变成骚扰；而骚扰的结果是用户把通知静音——那又回到「沉默」。"""
        _users(2)
        with _notify(True) as mock:
            report.check_report_delivery(now=AFTER_GRACE)
            second = report.check_report_delivery(
                now=AFTER_GRACE + timedelta(minutes=5)
            )
        self.assertEqual(mock.await_count, 2, "第二轮一个人都不该再收到")
        self.assertFalse(second["escalated"])
        self.assertEqual(second["reason"], "already_alerted")

    def test_a_day_nobody_heard_is_not_booked(self):
        """**没人听见的喊话不算喊过**（与 `daily_snapshot._alerted_on` 同一条取舍）。

        否则出站口坏掉的那一刻，这声喊就永久地替今天结了账——而它恰恰是最该被重复的
        那一种失败。
        """
        _users(1)
        with _notify(False) as mock:
            first = report.check_report_delivery(now=AFTER_GRACE)
            second = report.check_report_delivery(
                now=AFTER_GRACE + timedelta(minutes=5)
            )
        self.assertEqual(mock.await_count, 2)
        self.assertTrue(first["escalated"])
        self.assertTrue(second["escalated"])
        self.assertEqual(second["delivered_count"], 0)

    def test_a_partially_delivered_alert_still_counts_as_spoken(self):
        """只要有一个人听见了就算喊过——再来一轮也只会吵到同一个人以外的那些人。"""
        _users(2)
        with _notify([True, False]) as mock:
            first = report.check_report_delivery(now=AFTER_GRACE)
            second = report.check_report_delivery(
                now=AFTER_GRACE + timedelta(minutes=5)
            )
        self.assertEqual(mock.await_count, 2)
        self.assertTrue(first["escalated"])
        self.assertFalse(second["escalated"])

    def test_it_writes_nothing_back(self):
        """**它只读。** 投递明细是投递任务的账；看门狗改它就会让两条路径不再独立。

        这条是「只读」二字的唯一强制手段——少了它，一个「顺手把 attempts 加一」的改动
        看起来完全正常。
        """
        _users(1)
        row = _row(
            delivery={"x": {"ok": False, "at": "", "error": "出站通知口返回未送达"}},
            delivery_attempts=3,
            delivery_error="1/1 人未送达：出站通知口返回未送达",
        )
        before = {
            "delivery": dict(row.delivery),
            "delivery_attempts": row.delivery_attempts,
            "delivery_error": row.delivery_error,
            "delivered_at": row.delivered_at,
        }
        with _notify(True):
            report.check_report_delivery(now=AFTER_GRACE)
        row.refresh_from_db()
        self.assertEqual(row.delivery, before["delivery"])
        self.assertEqual(row.delivery_attempts, before["delivery_attempts"])
        self.assertEqual(row.delivery_error, before["delivery_error"])
        self.assertEqual(row.delivered_at, before["delivered_at"])


# --------------------------------------------------------------------------- #
# 接线
# --------------------------------------------------------------------------- #


class TestTheBeatWiring(TestCase):
    """两条独立的 beat 条目，以及「宽限期必须盖得住两条间隔」这条接线约束。"""

    def _entry(self, name: str) -> dict:
        from celery_app import app

        self.assertIn(name, app.conf.beat_schedule)
        return app.conf.beat_schedule[name]

    def test_delivery_and_watchdog_are_two_entries(self):
        """合成一条等于让看门狗去报自己的失败——它恰好是失败的那一个时，它不会响。"""
        deliver = self._entry("regime-report-deliver")
        watchdog = self._entry("regime-report-delivery-watchdog")
        self.assertEqual(deliver["task"], "apps.regime.tasks.deliver_report")
        self.assertEqual(watchdog["task"], "apps.regime.tasks.check_report_delivery")
        self.assertNotEqual(deliver["task"], watchdog["task"])

    def test_the_grace_covers_the_two_intervals_it_bounds(self):
        """宽限期必须比「心跳 + 投递」两条间隔之和大。

        `WATCHDOG_GRACE` 的全部意义是「到点之后给写入者留够走完的时间」，留得比这个短
        就是让看门狗抢它盯的那个写入者——而抢跑的表现是每天一条假告警。把 beat 调**密**
        不违例，调稀就违例。
        """
        from celery_app import app

        intervals = (
            app.conf.beat_schedule["regime-report-deliver"]["schedule"]
            + app.conf.beat_schedule["snapshot-daily-equity"]["schedule"]
        )
        self.assertGreater(report.WATCHDOG_GRACE.total_seconds(), intervals)
