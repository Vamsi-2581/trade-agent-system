"""调度表体检的取数与消息（`beat_health_run.py`）＋ 那一条 beat 条目（第③段单元 U3）。

纯层（`beat_health.py` 的判据、清单、正文）钉在 `test_beat_health.py`。这里钉它管不着的
三件事：

1. **那本账**：同一批问题、同一业务日只喊一次；问题集合变了再喊；旧日子的账被清掉；而
   **没人听见的喊话不算喊过**（空收件人、送达 0 两条路都不记账，下一轮还得喊）。
2. **喊的判据来自纯层**，不在这里重写一遍——所以下面的用例都拿真实的一行去触发，而不是
   伪造一个「该喊」的结论。
3. **那条 beat 条目**指向这个任务、有固定间隔；任务失败**不吞**。

**不碰库**：`entries()` / `declared()` / 收件人 / 出口全被换成桩。真实取数那条路
（`PeriodicTask` 表那一头）由体检页的端到端（`test_mechanism_switch.py`）与
`test_scheduled_task_persistence.py` 钉着。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from django.test import SimpleTestCase

from apps.regime import beat_health_run
from apps.regime.beat_health import Entry
from apps.regime.tests.test_slicing import _StubbedConnectionReset

#: 注入的时钟：北京时间 2026-09-26 20:00（业务日就是 9 月 26 日）。
NOW = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)


def _entry(
    name: str,
    *,
    period_seconds: int = 300,
    ago: float | None = 10.0,
    enabled: bool = True,
) -> Entry:
    """调度表里的一行。`ago` = 上次派发距今多少秒；`None` = 从未发过。"""
    return Entry(
        name=name,
        task=f"apps.regime.tasks.{name}",
        period_seconds=period_seconds,
        last_run_at=None if ago is None else NOW - timedelta(seconds=ago),
        enabled=enabled,
    )


class _WithStubbedSend(SimpleTestCase):
    """取数、收件人、出口都换成桩；`check()` 返回摘要，发出去的正文落在 `self.sent`。"""

    def setUp(self):
        super().setUp()
        beat_health_run._alerted_on.clear()
        self.sent: list[str] = []

    def check(
        self,
        rows,
        declared,
        *,
        now: datetime = NOW,
        recipients: tuple[str, ...] = ("u-1",),
        delivered: int | None = None,
    ) -> dict:
        delivered = len(recipients) if delivered is None else delivered

        def _send(text: str) -> dict:
            self.sent.append(text)
            return {"recipients": len(recipients), "delivered": delivered}

        with (
            patch.object(beat_health_run, "entries", return_value=rows),
            patch.object(beat_health_run, "declared", return_value=list(declared)),
            patch(
                "apps.regime.halt_notify.all_active_user_ids",
                return_value=list(recipients),
            ),
            patch("apps.regime.halt_notify.alert_everyone", side_effect=_send),
        ):
            return beat_health_run.check(now=now)


class TestTheAlertAccount(_WithStubbedSend):
    """同一批问题、同一业务日只喊一次；没听见的不算喊过。"""

    def test_the_first_round_shouts_and_the_second_is_silent(self):
        """`alpha` 声明了但不在表里——这是不会自愈的那一档，喊一次就该够。"""
        rows = [_entry("beta")]
        first = self.check(rows, ("alpha", "beta"))
        self.assertEqual(first["reason"], "sent")
        self.assertTrue(first["alerted"])
        self.assertEqual(len(self.sent), 1)

        second = self.check(rows, ("alpha", "beta"))
        self.assertEqual(second["reason"], "already_alerted_today")
        self.assertFalse(second["alerted"])
        self.assertEqual(len(self.sent), 1, "同一批问题不该再喊一遍")

    def test_a_changed_problem_set_is_news_again(self):
        """「修好一条、又坏一条」是新的一批：当天不该把它咽下去（那是沉默）。"""
        self.check([_entry("beta")], ("alpha", "beta"))
        again = self.check([_entry("beta", enabled=False, ago=None)], ("alpha", "beta"))
        self.assertEqual(again["reason"], "sent")
        self.assertEqual(len(self.sent), 2)

    def test_the_same_problem_shouts_again_the_next_business_day(self):
        rows = [_entry("beta")]
        self.check(rows, ("alpha", "beta"))
        later = self.check(rows, ("alpha", "beta"), now=NOW + timedelta(days=1))
        self.assertEqual(later["reason"], "sent")
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(len(beat_health_run._alerted_on), 1, "旧日子的账该被清掉")

    def test_nobody_to_tell_is_not_a_shout_that_counted(self):
        """空 `is_active` 是「此刻没人在听」，不是「已经通知过了」（日报看门狗同一条）。"""
        rows = [_entry("beta")]
        nobody = self.check(rows, ("alpha", "beta"), recipients=())
        self.assertEqual(nobody["reason"], "no_recipients")
        self.assertEqual(self.sent, [])

        someone = self.check(rows, ("alpha", "beta"), recipients=("u-1",))
        self.assertEqual(someone["reason"], "sent")

    def test_an_undelivered_shout_does_not_count_either(self):
        """没人听见的喊话不算喊过（`report._alerted_on` 同一条取舍）。"""
        rows = [_entry("beta")]
        failed = self.check(rows, ("alpha", "beta"), delivered=0)
        self.assertEqual(failed["reason"], "send_failed")
        self.assertFalse(failed["alerted"])
        self.assertEqual(failed["delivered"], 0)

        retried = self.check(rows, ("alpha", "beta"), delivered=1)
        self.assertEqual(retried["reason"], "sent")


class TestWhatIsWorthARound(_WithStubbedSend):
    """两个数分开报、空转的两档分得开——判据全在纯层。"""

    def test_a_quiet_round_says_so(self):
        summary = self.check([_entry("alpha")], ("alpha",))
        self.assertEqual(summary["reason"], "no_problems")
        self.assertEqual(summary["problems"], 0)
        self.assertEqual(self.sent, [])

    def test_a_row_that_never_ran_is_not_worth_waking_anyone(self):
        summary = self.check([_entry("alpha", ago=None)], ("alpha",))
        self.assertEqual(summary["reason"], "nothing_to_say")
        self.assertEqual(summary["problems"], 1)
        self.assertEqual(summary["worth_alerting"], 0)
        self.assertEqual(self.sent, [])

    def test_the_two_counts_stay_apart(self):
        """「页上有几条」与「喊了哪几条」本来就该能不一样（`beat_health.alerts`）。"""
        summary = self.check([_entry("beta", ago=None)], ("alpha", "beta"))
        self.assertEqual(summary["problems"], 2)  # alpha 未进表 + beta 从未发过
        self.assertEqual(summary["worth_alerting"], 1)  # 只有 alpha 值得喊
        self.assertEqual(summary["recipients"], 1)
        self.assertEqual(summary["delivered"], 1)
        self.assertIn("· alpha：从未进入调度表", self.sent[0])


class TestTheBeatEntryAndTheTask(_StubbedConnectionReset, SimpleTestCase):
    """条目指向这个任务、任务把摘要带回来、失败往上抛。

    `celery_app` 与 `apps.regime.tasks` 都在方法里 import：`tasks.py` 顶部就
    `from celery_app import app`，而 `celery_app` 又 import 一圈任务模块（其中
    `apps.backtest.tasks` 反过来 import `apps.regime.tasks`）——在测试模块顶部 import
    `tasks` 会撞上那个环（只有「碰巧有别的模块先 import 了 celery_app」时才不炸）。
    """

    def test_the_beat_entry_points_at_this_task_and_has_a_fixed_interval(self):
        from celery_app import app

        entry = app.conf.beat_schedule["regime-beat-health"]
        self.assertEqual(entry["task"], "apps.regime.tasks.check_beat_health")
        self.assertIsInstance(entry["schedule"], (int, float))  # 固定间隔，不是 crontab

    def test_the_task_returns_the_summary(self):
        from apps.regime import tasks

        summary = {"checked": True, "reason": "no_problems"}
        with patch.object(beat_health_run, "check", return_value=summary):
            self.assertEqual(tasks.check_beat_health.run(), summary)

    def test_a_failure_is_not_swallowed(self):
        """读不到库就说不出「beat 把它派出去了没有」：吞掉只会连 FAILURE 都没有。"""
        from apps.regime import tasks

        with patch.object(
            beat_health_run, "check", side_effect=RuntimeError("库读不到")
        ):
            with self.assertRaises(RuntimeError):
                tasks.check_beat_health.run()
