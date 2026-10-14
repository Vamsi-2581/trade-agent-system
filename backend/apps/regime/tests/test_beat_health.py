"""调度表体检（`beat_health.py`）：纯函数那一半。

给它一张「调度表里此刻有哪些条目、各自上一次是什么时候发的」加上「文件里声明了哪些」，
它答得对不对。取数（`beat_health_run.py`）与页面那一段（`test_mechanism_switch.py` 的
端到端）各自钉在别处。

本文件里最要紧的五条性质，每一条坏了都不报警、只出错：

1. **「没在跑」的四种形状分开说，因为下一步动作不同**：`unscheduled`（文件里声明了、
   调度表里没有这一行——重启 beat 才会有）≠ `disabled`（行在、被人关掉了）≠ `never`
   （行在、一次都没发过）≠ `late`（发过、超期）。合成一句「异常」就等于把「去重启
   beat」和「去看它为什么失败」混成同一条指令。
2. **宽限是轮数，不是秒数；而轮数之外还有一条 3 分钟的下限。** 60 秒一轮的任务晚 5 分钟
   是真出事，3600 秒一轮的任务晚 5 分钟什么都没发生，所以判据是「错过几轮」；但
   `last_run_at` 由调度器**定期**落库（`sync_every` = 180 秒，不是每次派发都写），于是
   库里的数天生滞后 3 分钟量级。少了这一项，30 秒一轮的任务会**每时每刻**都显示超期
   ——实测过，而 beat 日志显示它每 30 秒都在正常派发。
3. **没有固定间隔的条目不定罪。** crontab 给不出「下一次该在什么时候」——硬套一个
   「24 小时内跑过就算正常」是拿日历猜调度意图，猜错就规律性报假警，而假警比沉默更快地
   教会人忽略这一页。但它**照旧出现在计数里**，不能从这一页上消失。
4. **这一页只答「beat 把它发出去了没有」。** 那句话必须印着（它答不出「任务成功了没
   有」），而它自己也不该顺手宣称别的——本文件钉住这两句话，因为读的人会把这一行当成
   「一切正常」的全部证据。
5. **喊人的那条消息比这一页窄**（第③段单元 U3）：`never` 不喊（库里没有「这一行什么
   时候进表」这一格，于是「刚被合并进来」与「真的从没跑过」长得一模一样，每次重启都会
   假警），`late` 要有旁证（全表最近的一条自己也超期 = beat 整体没在跑，那不是「哪一条
   坏了」）。**页照旧四档都印**——收窄的只是「喊人」，不是判据；两处各有一条用例钉着
   这个「只窄在消息层」。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Sequence

from django.test import SimpleTestCase

from apps.regime import beat_health
from apps.regime.beat_health import BeatProblem, Entry

#: 注入的时钟。**纯层不吃时钟**，所以这里随便挑一个，用例只按相对距离算。
NOW = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)

DECLARED = ("alpha", "beta", "gamma")


def _entry(
    name: str,
    *,
    period_seconds: int | None = 300,
    ago: float | None = 60.0,
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


def _kinds(
    entries: Sequence[Entry], declared: Sequence[str] = DECLARED
) -> list[tuple[str, BeatProblem]]:
    found = beat_health.findings(entries, declared, now=NOW)
    return [(finding.name, finding.problem) for finding in found]


def _fresh(**overrides) -> Entry:
    """一条正常的行：300 秒一轮、一分钟前刚发过。"""
    return _entry("alpha", **overrides)


class TestTheThreeShapesOfNotRunning(SimpleTestCase):
    """性质 1：三种形状分开说，且**判据是事实、不是这一页的观感**。"""

    def test_a_row_missing_from_the_table_is_unscheduled(self):
        """文件里声明了、调度表里没有这一行 —— 它是「永远不会被发出去」。"""
        self.assertEqual(
            _kinds([_entry("beta"), _entry("gamma")]),
            [("alpha", BeatProblem.UNSCHEDULED)],
        )

    def test_a_row_that_never_ran_is_never(self):
        self.assertEqual(
            _kinds([_entry("alpha", ago=None), _entry("beta"), _entry("gamma")]),
            [("alpha", BeatProblem.NEVER_RAN)],
        )

    def test_a_row_past_its_grace_is_late(self):
        """300 秒一轮、宽限 3 轮 ⇒ 15 分钟，再加 3 分钟的记账下限。」

        3600 秒（12 轮）早超了；1200 秒（4 轮）刚过界；900 秒（正好 3 轮）还没到。
        """
        self.assertEqual(
            _kinds(
                [
                    _entry("alpha", ago=3600),
                    _entry("beta", ago=900),
                    _entry("gamma", ago=1200),
                ]
            ),
            [("alpha", BeatProblem.LATE), ("gamma", BeatProblem.LATE)],
        )

    def test_exactly_at_the_boundary_is_not_late(self):
        """界上不算超期：worker 忙一轮、容器重启一次都在这个量级里。"""
        self.assertEqual(_kinds([_entry("alpha", ago=1080)], ("alpha",)), [])

    def test_nothing_is_reported_when_everything_is_fresh(self):
        self.assertEqual(
            _kinds([_fresh(), _entry("beta", ago=30), _entry("gamma")]), []
        )

    def test_a_disabled_row_says_disabled_not_late(self):
        """停用是一种**不同的**「没在跑」：去启用它，而不是去查它为什么失败。"""
        self.assertEqual(
            _kinds(
                [
                    _entry("alpha", enabled=False, ago=99_999),
                    _entry("beta"),
                    _entry("gamma"),
                ]
            ),
            [("alpha", BeatProblem.DISABLED)],
        )

    def test_a_disabled_row_that_never_ran_says_disabled(self):
        """两种都成立时说那个**动作唯一**的：启用之后才会轮到「它还没发过」。"""
        self.assertEqual(
            _kinds([_entry("alpha", enabled=False, ago=None)], ("alpha",)),
            [("alpha", BeatProblem.DISABLED)],
        )

    def test_a_row_missing_from_the_table_is_not_also_reported_as_never(self):
        """空表（beat 一次都没起来过）：三条都是 `unscheduled`，不是「从未发出去过」。"""
        self.assertEqual(
            _kinds([]),
            [
                ("alpha", BeatProblem.UNSCHEDULED),
                ("beta", BeatProblem.UNSCHEDULED),
                ("gamma", BeatProblem.UNSCHEDULED),
            ],
        )


class TestNothingIsConvictedWithoutAFixedInterval(SimpleTestCase):
    """性质 3：crontab 不定罪，但也不能从这一页上消失。"""

    def test_a_crontab_entry_is_never_late(self):
        self.assertEqual(
            _kinds([_entry("alpha", period_seconds=None, ago=86400 * 30)], ("alpha",)),
            [],
        )

    def test_but_a_crontab_entry_that_never_ran_is_still_never(self):
        """「不定罪」管的是**超期**那一档；一次都没发过与间隔无关。"""
        self.assertEqual(
            _kinds([_entry("alpha", period_seconds=None, ago=None)], ("alpha",)),
            [("alpha", BeatProblem.NEVER_RAN)],
        )

    def test_the_count_says_how_many_were_left_unjudged(self):
        """要有计数，否则「本页不定罪」会读成「它们没问题」。"""
        stat = beat_health.counts(
            [
                _entry("alpha", period_seconds=None, ago=86400 * 30),
                _entry("beta"),
                _entry("gamma"),
            ],
            DECLARED,
            now=NOW,
        )
        self.assertEqual(stat["crontab"], 1)
        self.assertEqual(stat["problems"], 0)


class TestTheExtraRowsAreNotProblems(SimpleTestCase):
    """表里有、文件里没有的（DB 建的动态任务）不是「没在跑」，只是不归这份文件管。"""

    def test_a_dynamic_row_is_not_a_finding(self):
        self.assertEqual(
            _kinds([_fresh(), _entry("beta"), _entry("gamma"), _entry("db_only")]), []
        )

    def test_but_it_is_counted_and_named(self):
        stat = beat_health.counts(
            [_fresh(), _entry("beta"), _entry("gamma"), _entry("db_only")],
            DECLARED,
            now=NOW,
        )
        self.assertEqual(stat["extra"], 1)
        self.assertEqual(stat["scheduled"], 3)
        self.assertEqual(stat["declared"], 3)

    def test_the_newest_row_of_the_whole_table_is_the_one_named(self):
        """「最近一条」是**活着的证据**，所以取全表（含动态任务）的最新那条。"""
        lines = beat_health.describe(
            [
                _fresh(ago=3600),
                _entry("beta"),
                _entry("gamma"),
                _entry("db_only", ago=30),
            ],
            DECLARED,
            now=NOW,
        )
        self.assertIn("最近一条 db_only 距今 30 秒", lines[0])


class TestTheOrderIsTheDeclaredOrder(SimpleTestCase):
    """这一页是给人对照 `beat_schedule` 读的：两处顺序不一致会让人以为漏了几条。"""

    def test_findings_follow_the_declared_order_not_the_table_order(self):
        self.assertEqual(
            _kinds(
                [
                    _entry("gamma", ago=None),
                    _entry("beta", ago=99_999),
                    # alpha 缺行
                ]
            ),
            [
                ("alpha", BeatProblem.UNSCHEDULED),
                ("beta", BeatProblem.LATE),
                ("gamma", BeatProblem.NEVER_RAN),
            ],
        )


class TestAgeIsNoneNotZero(SimpleTestCase):
    """0 会被读成「刚刚发过」——这是「从没发过」与「刚发过」被混成一句的那个错。"""

    def test_age_of_a_never_run_entry_is_none(self):
        self.assertIsNone(_entry("alpha", ago=None).age_seconds(now=NOW))

    def test_age_of_a_finding_without_a_row_is_none(self):
        finding = beat_health.findings([], ["alpha"], now=NOW)[0]
        self.assertIsNone(finding.age_seconds(now=NOW))


class TestWhatThePageSays(SimpleTestCase):
    """性质 4：这一页只答「发出去了没有」，且那句话每次都印。"""

    def test_a_clean_table_is_a_head_and_one_reading_note(self):
        """体检不是流水：每一轮都把 11 条念一遍，读者就会开始跳过它。

        所以正常时只有两行——总括，加上那句「怎么读这一行」（它每次都在，见下一条）。
        """
        lines = beat_health.describe(
            [_fresh(ago=60), _entry("beta", ago=120), _entry("gamma", ago=180)],
            DECLARED,
            now=NOW,
        )
        self.assertEqual(
            lines[0],
            "调度表（beat）：声明的 3 条都在调度表里、都没有超期（最近一条 alpha 距今 1 分）",
        )
        self.assertEqual(len(lines), 2)

    def test_it_always_says_it_only_answers_whether_beat_dispatched(self):
        """这是全篇最要紧的一句：把「发了」读成「好了」比没有这一行更坏。"""
        for entries in (
            [_fresh(), _entry("beta"), _entry("gamma")],
            [_entry("alpha", ago=None)],
            [],
        ):
            with self.subTest(entries=len(entries)):
                last = beat_health.describe(entries, DECLARED, now=NOW)[-1]
                self.assertIn("只答「beat 把它**发出去了没有**」", last)
                self.assertIn("日报第④段", last)
                # 「距今 3 分」不是「3 分钟没跑」：没有这一句，读数会被当成实时的。
                self.assertIn("粒度下限", last)

    def test_the_restart_hint_appears_only_when_something_is_wrong(self):
        """它是 `unscheduled` 那个状态的**唯一**出路，所以只在真有事时才占地方。"""
        clean = beat_health.describe(
            [_fresh(), _entry("beta"), _entry("gamma")], DECLARED, now=NOW
        )
        broken = beat_health.describe(
            [_entry("beta"), _entry("gamma")], DECLARED, now=NOW
        )

        self.assertFalse(any("重启 beat" in line for line in clean))
        self.assertTrue(any("重启 beat" in line for line in broken))

    def test_each_problem_gets_a_line_with_its_numbers(self):
        """「已超期」必须带上「多少一轮、上次多久前」——否则人无从判断严重程度。"""
        lines = beat_health.describe(
            [
                _entry("alpha", ago=3600),  # 12 轮
                _entry("beta", ago=None),
                # gamma 缺行
            ],
            DECLARED,
            now=NOW,
        )
        self.assertIn("调度表（beat）：声明的 3 条里 3 条没在正常跑", lines[0])
        self.assertIn("alpha：已超期（每 5 分一轮，上次距今 1 小时）", lines[1])
        self.assertIn("beta：从未发出去过（每 5 分一轮）", lines[2])
        self.assertIn("gamma：从未进入调度表", lines[3])

    def test_the_unjudged_and_the_extra_are_named_in_the_head_line(self):
        lines = beat_health.describe(
            [
                _entry("alpha", period_seconds=None),
                _entry("beta"),
                _entry("gamma"),
                _entry("db_only"),
            ],
            DECLARED,
            now=NOW,
        )
        self.assertIn("1 条无固定间隔（crontab，本页不定罪）", lines[0])
        self.assertIn("1 条不在文件里（DB 建的动态任务）", lines[0])

    def test_an_empty_table_names_every_declared_entry(self):
        """新环境里 beat 一次都没起来过：这一页必须说得出「一条都没有」。"""
        lines = beat_health.describe([], DECLARED, now=NOW)
        self.assertIn("声明的 3 条里 3 条没在正常跑", lines[0])
        self.assertNotIn("最近一条", lines[0])
        self.assertIn("alpha：从未进入调度表", "\n".join(lines))


class TestTheGraceIsCountedInRounds(SimpleTestCase):
    """性质 2 的前一半：同一句话在 60 秒一轮与 3600 秒一轮上必须是同一个意思。"""

    def test_a_slow_task_is_not_convicted_for_a_delay_that_would_doom_a_fast_one(self):
        # 同样晚了 600 秒：300 秒一轮的错过 2 轮（界是 3 轮，不算），30 秒一轮的错过 20 轮。
        self.assertEqual(
            _kinds([_entry("alpha", period_seconds=300, ago=600)], ("alpha",)), []
        )
        self.assertEqual(
            _kinds([_entry("alpha", period_seconds=30, ago=600)], ("alpha",)),
            [("alpha", BeatProblem.LATE)],
        )

    def test_the_grace_is_a_parameter_so_a_caller_can_widen_it(self):
        found = beat_health.findings(
            [_entry("alpha", ago=3600)], ("alpha",), now=NOW, missed_rounds=100
        )
        self.assertEqual(found, [])


class TestTheWriteLagIsInsideTheThreshold(SimpleTestCase):
    """性质 2 的后一半：库里的数天生滞后 3 分钟量级，判据必须含这一项。

    少了它，一条 30 秒一轮的任务在**每一个瞬间**都是「错过 3 轮以上」——它会在这一页上
    长期红着，而 beat 那边一切正常。这一页存在的全部理由是让人相信它。
    """

    def test_a_30_second_task_at_the_measured_real_age_is_not_late(self):
        """206 秒是实测值：beat 日志显示它每 30 秒正常派发，而库里就是这个数。"""
        self.assertEqual(
            _kinds([_entry("alpha", period_seconds=30, ago=206)], ("alpha",)), []
        )

    def test_but_it_is_still_late_once_it_is_far_enough_past_the_lag(self):
        """下限不是免罪符：真停下来之后，这一页说得出的时刻是「下限 + 宽限轮数」。"""
        self.assertEqual(
            _kinds([_entry("alpha", period_seconds=30, ago=400)], ("alpha",)),
            [("alpha", BeatProblem.LATE)],
        )

    def test_the_lag_is_a_parameter_so_the_arithmetic_is_pinned(self):
        """把下限调成 0，同一个数就红了——证明压住它的正是这一项，不是别的东西。"""
        entries = [_entry("alpha", period_seconds=30, ago=206)]
        self.assertEqual(beat_health.findings(entries, ("alpha",), now=NOW), [])
        self.assertNotEqual(
            beat_health.findings(entries, ("alpha",), now=NOW, write_lag=0), []
        )


class TestDuration(SimpleTestCase):
    """只是给这一页读的措辞，不参与任何判断——但它决定读的人看到的是哪个量级。"""

    def test_it_picks_the_unit_that_keeps_the_number_small(self):
        for seconds, expected in [
            (0, "0 秒"),
            (59, "59 秒"),
            (60, "1 分"),
            (3599, "59 分"),
            (3600, "1 小时"),
            (86_399, "23 小时"),
            (86_400, "1 天"),
            (86_400 * 12, "12 天"),
            (86_400 + 3600, "1 天"),
        ]:
            with self.subTest(seconds=seconds):
                self.assertEqual(beat_health.duration(seconds), expected)

    def test_a_negative_age_reads_as_zero_rather_than_as_a_future_date(self):
        """worker 与 beat 的两个时钟略有偏差时，`now - last_run_at` 可以是负的。"""
        self.assertEqual(beat_health.duration(-30), "0 秒")


class TestWhoGetsWokenUp(SimpleTestCase):
    """性质 5：消息层比这一页窄，而收窄只发生在「喊人」这一步。"""

    def _alerted(self, rows, declared) -> list[tuple[str, BeatProblem]]:
        found = beat_health.findings(rows, declared, now=NOW)
        return [
            (finding.name, finding.problem)
            for finding in beat_health.alerts(
                found, newest=beat_health.newest_entry(rows), now=NOW
            )
        ]

    def test_a_row_that_never_ran_is_on_the_page_but_not_in_the_message(self):
        """`never` 不喊人：库里分不开「刚被合并进来、落库还没轮到」与「真的从没发过」。

        `PeriodicTask` 没有「这一行什么时候进表」这一格（`date_changed` 是 `auto_now`，
        跟着落库走）。每次 beat 重启、每加一条新条目都会落进这个窗口——拿它喊人，等于
        让这条机制的头一条消息说假话。
        """
        rows = [_entry("alpha", ago=None)]
        self.assertEqual(_kinds(rows, ("alpha",)), [("alpha", BeatProblem.NEVER_RAN)])
        self.assertEqual(self._alerted(rows, ("alpha",)), [])

    def test_missing_and_disabled_rows_do_get_a_message(self):
        """两档都是「文件说要跑，而它不会被发出去」，且都不会自愈。"""
        rows = [_entry("beta", ago=10), _entry("gamma", enabled=False, ago=None)]
        self.assertEqual(
            self._alerted(rows, DECLARED),
            [("alpha", BeatProblem.UNSCHEDULED), ("gamma", BeatProblem.DISABLED)],
        )

    def test_a_stuck_entry_is_worth_a_message_while_the_rest_is_healthy(self):
        """`late` 的旁证：最近的一条在正常跑，所以这次说得成「是**它**坏了」。"""
        rows = [_entry("alpha", ago=10), _entry("beta", ago=3600)]
        self.assertEqual(
            self._alerted(rows, ("alpha", "beta")), [("beta", BeatProblem.LATE)]
        )

    def test_nothing_is_said_while_the_whole_table_looks_stale(self):
        """全表都超期 = beat 整体没在跑（或刚重启还没落库）：那不是说「哪一条坏了」。

        而 `unscheduled` **不受这条旁证影响**：文件说要跑、它不会被发出去，与 beat 此刻
        是不是整体停着无关——两件事的修理动作（重启 beat）正好还是同一个。
        """
        rows = [_entry("alpha", ago=3600), _entry("beta", ago=3600)]
        self.assertEqual(
            _kinds(rows, DECLARED),
            [
                ("alpha", BeatProblem.LATE),
                ("beta", BeatProblem.LATE),
                ("gamma", BeatProblem.UNSCHEDULED),
            ],
        )
        self.assertEqual(
            self._alerted(rows, DECLARED), [("gamma", BeatProblem.UNSCHEDULED)]
        )


class TestWhatTheMessageSays(SimpleTestCase):
    """正文：清单与页同源、只说用得上的出路、末尾两句边界必须在。"""

    def _body(self, rows, declared) -> str:
        found = beat_health.findings(rows, declared, now=NOW)
        return beat_health.alert_body(found, now=NOW)

    def test_the_headline_counts_what_this_message_is_about(self):
        self.assertIn(
            "🚨 调度表体检：1 条 beat 条目没在正常跑",
            self._body([_entry("beta", ago=3600)], ("beta",)),
        )

    def test_the_list_reuses_the_pages_own_line_for_each_entry(self):
        """同一件事在页上与消息里必须逐字同源，否则会被读成两件事。"""
        rows = [_entry("beta", ago=3600), _entry("gamma", enabled=False, ago=None)]
        body = self._body(rows, ("beta", "gamma"))
        self.assertIn("· beta：已超期（每 5 分一轮，上次距今 1 小时）", body)
        self.assertIn("· gamma：已被停用", body)

    def test_a_row_that_never_entered_the_table_has_no_period_to_report(self):
        body = self._body([_entry("beta", ago=10)], ("alpha", "beta"))
        self.assertIn("· alpha：从未进入调度表", body)

    def test_only_the_remedies_that_apply_are_printed(self):
        """三档的下一步动作不同：印一条用不上的出路，读的人会去找一个不存在的开关。"""
        missing_only = self._body([_entry("beta", ago=10)], ("alpha", "beta"))
        self.assertIn("· 从未进入调度表 → ", missing_only)
        self.assertNotIn("· 已被停用 → ", missing_only)
        self.assertNotIn("· 已超期 → ", missing_only)

        disabled_only = self._body([_entry("beta", enabled=False, ago=None)], ("beta",))
        self.assertIn("重新启用", disabled_only)
        self.assertNotIn("· 从未进入调度表 → ", disabled_only)

    def test_the_shot_boundary_is_stated(self):
        """与体检页末尾那句同一个理由：不说，读者会把「发出去了」读成「好了」。"""
        body = self._body([_entry("beta", ago=10)], ("alpha", "beta"))
        self.assertIn("派出去了", body)
        self.assertIn("第④段", body)
        self.assertIn("不会每轮重喊", body)
