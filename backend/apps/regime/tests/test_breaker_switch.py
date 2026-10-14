"""事件熔断开关：上线确认页与那一次 INSERT（第②段单元 ②f）。

`test_halt.py` 钉的是「一组声明怎么求值成挡不挡得住」；这个文件钉的是**人点头那一步**：
页面上算出来的那几个数、以及点头之后落下的那一行流水。

本文件里最要紧的三条性质，每一条坏了都不报警、只出错：

1. **两个数不是一个数。** 「未来 14 天」在熔断窗口上有两种读法（与其相交 / 在期内开启），
   而它们的差在真实数据上很大。判断「打开之后会不会空转」用的是第一个，所以两者必须
   分别标名、分别渲染——合成一个的话，「此刻正压在窗口里」这种最该被看见的情况会被
   算成 0（它的窗口起点在过去）。
2. **空库与平静期必须能分开读。** 两者都报「0 条」，但一个是「设备没接上」，一个是
   「这段确实平静」。空库那一段必须把**后果**说出来（打开之后不会拦住任何东西，而且
   从外面看与正常运行完全一样）——这是唯一说得出这句话的时刻（CONTEXT.md 第 169 条）。
3. **写方读的是真 `from_mode`，且重复按不动手。** 流水是事后回答「当时它到底开没开」的
   唯一材料；写死 `shadow` 会让「从执行态关掉再打开」记成「从 Shadow 打开」，而重复按
   一次 `on` 多出来的那条「executing → executing」会永久留在只增不改的表里。

窗口的边界一律用**半开区间**（`halt_at <= now < resume_at`），所以边界那几个时刻各有一条
用例——它们是两个数分叉的地方，也是「差一分钟」听起来无害的地方。

夹具锚点取**真实时钟再加 5 分钟**（`NOW`）：`MajorEvent.created_at` 是 `auto_now_add`，
注入不进去，所以「最近入库距今几天」只有在 `now` 晚于入库时刻时才是那个有意义的数
（早于它的话 Python 的 `timedelta.days` 会取整成 -1）。窗口一律相对 `NOW` 写，并**显式
注入**给页面，所以判定与真实时钟无关。
"""

from __future__ import annotations

import getpass
from datetime import datetime, timedelta
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone as django_timezone

from apps.regime import breaker_switch, config, halt
from apps.regime.models import (
    ActorKind,
    EventImpact,
    EventScope,
    EventStatus,
    HaltTrigger,
    MajorEvent,
    MechanismKind,
    MechanismMode,
    RegimeMechanismSwitch,
)

#: 本文件的「此刻」。比真实时钟晚 5 分钟，理由见模块 docstring。
NOW = django_timezone.now() + timedelta(minutes=5)

#: 确认页的天数取配置面那个数（**不是**日报的 `REPORT.event_horizon_days`）。
#: 值本身（14）由 `test_config.py` 钉住；这里跟着配置走，免得同一条纪律有两个地方各写一遍。
HORIZON = config.EVENTS.confirm_horizon_days

#: 确认页正文第一行。CLI 与 `/regime` 都渲染这一页，用它当「渲染过了」的锚。
PAGE_TITLE = "事件熔断 · 上线确认"


def _row(
    name: str = "FOMC 议息",
    *,
    halt_at: datetime,
    resume_at: datetime | None = None,
    impact: str = EventImpact.HIGH.value,
    status: str = EventStatus.SCHEDULED.value,
) -> MajorEvent:
    """落一条真事件。窗口默认两小时，需要测边界的用例自己传 `resume_at=`。"""
    return MajorEvent.objects.create(
        name=name,
        scope_kind=EventScope.MARKET.value,
        symbols=[],
        event_time=halt_at,
        impact=impact,
        halt_at=halt_at,
        resume_at=resume_at if resume_at is not None else halt_at + timedelta(hours=2),
        status=status,
        created_by="tester",
    )


def _open_now(name: str = "正在开的窗口") -> MajorEvent:
    """一条此刻正压在里面的窗口（跨过 `NOW`）。"""
    return _row(
        name, halt_at=NOW - timedelta(hours=2), resume_at=NOW + timedelta(hours=2)
    )


class _Fixture(TestCase):
    def sweep(self) -> breaker_switch.Confirmation:
        return breaker_switch.confirmation(now=NOW)

    def body(self) -> str:
        return breaker_switch.confirmation_body(now=NOW)

    def summary(self) -> str:
        return breaker_switch.confirmation_summary(now=NOW)

    def switch(self, mode: MechanismMode) -> RegimeMechanismSwitch:
        """直接往流水表里落一条（测「读侧认得它」时用，绕开写方）。"""
        return RegimeMechanismSwitch.objects.create(
            kind=MechanismKind.EVENT_BREAKER.value,
            from_mode=MechanismMode.SHADOW.value,
            to_mode=mode.value,
            at=NOW - timedelta(days=1),
            actor_kind=ActorKind.CLI.value,
            actor_name="ops",
            reason="测试",
        )

    def flip(
        self,
        to_mode: MechanismMode = MechanismMode.EXECUTING,
        *,
        actor_kind: ActorKind = ActorKind.CLI,
        actor_name: str = "ops",
        reason: str = "测试",
    ) -> RegimeMechanismSwitch | None:
        return breaker_switch.flip_event_breaker(
            to_mode,
            actor_kind=actor_kind,
            actor_name=actor_name,
            reason=reason,
            now=NOW,
        )


# --------------------------------------------------------------------------- #
# 性质 1：两个数不是一个数
# --------------------------------------------------------------------------- #


class TestTheTwoCounts(_Fixture):
    def test_a_window_that_already_opened_counts_as_overlapping_only(self):
        """**这条是本文件的中心。** 此刻正压在窗口里的事件，窗口起点在过去。

        只看起点的话它会算成 0——而「打开开关之后会不会拦住东西」的答案恰好是「会，
        现在就拦着」。把两个数合成一个，这种最该被看见的情况反而最看不见。
        """
        _open_now()
        data = self.sweep()
        self.assertEqual(data.overlapping, 1)
        self.assertEqual(data.starting, 0)

    def test_a_window_opening_inside_the_horizon_counts_in_both(self):
        _row("三天后", halt_at=NOW + timedelta(days=3))
        data = self.sweep()
        self.assertEqual((data.overlapping, data.starting), (1, 1))

    def test_a_window_beyond_the_horizon_counts_in_neither(self):
        _row("很久以后", halt_at=NOW + timedelta(days=HORIZON + 6))
        data = self.sweep()
        self.assertEqual((data.overlapping, data.starting), (0, 0))

    def test_a_window_that_already_ended_counts_in_neither(self):
        _row(
            "上周的",
            halt_at=NOW - timedelta(days=7),
            resume_at=NOW - timedelta(days=7) + timedelta(hours=2),
        )
        data = self.sweep()
        self.assertEqual((data.overlapping, data.starting), (0, 0))

    def test_a_window_starting_exactly_at_the_horizon_is_not_starting(self):
        """`[now, until)` 是半开的：起点正好落在 `until` 上的窗口不算「14 天内开启」。

        顺带钉住「相交」也是同一条半开约定（`halt_at < until`）——两处若一处闭一处开，
        两个数就会在同一个时刻上各自给出一个「对」的答案。
        """
        edge = _row("压线", halt_at=NOW + timedelta(days=HORIZON))
        self.assertEqual(edge.halt_at, self.sweep().until)
        data = self.sweep()
        self.assertEqual((data.overlapping, data.starting), (0, 0))
        # 但它仍在库里、仍会开窗：所以页面走的是「平静期」那一档，不是空库那一档
        self.assertEqual((data.library_total, data.firing_total), (1, 1))

    def test_a_window_starting_exactly_now_is_starting_and_open(self):
        _row("刚刚", halt_at=NOW)
        data = self.sweep()
        self.assertEqual(data.starting, 1)
        self.assertEqual(data.open_now.name, "刚刚")

    def test_a_window_resuming_exactly_now_is_not_open(self):
        """`halt_at <= now < resume_at`：`resume_at == now` 已经不是「正压在里面」。

        判反的表现是「窗口刚结束的那一刻，页面还说此刻在窗口里」——而那一刻正是减仓
        执行器已经放手的时候。
        """
        _row("刚结束", halt_at=NOW - timedelta(hours=2), resume_at=NOW)
        data = self.sweep()
        self.assertIsNone(data.open_now)
        self.assertEqual(data.overlapping, 0)


class TestWhatCountsAsAFiringWindow(_Fixture):
    def test_a_cancelled_high_impact_event_stays_in_the_library(self):
        """取消掉的事件仍算「录过的情报」，但不再产生窗口。

        `library_total` 回答的是「库里有多少条高影响事件」，所以含已取消的；窗口那几个
        数走 `triggers_halt`（它已经把「已排期 ∧ 档位高」写死在一处）。两个数的差就是
        在这里被看见的：页面会同时写着「库里 1 条（会开窗 0 条）」。
        """
        _row(
            "取消了的",
            halt_at=NOW + timedelta(days=1),
            status=EventStatus.CANCELLED.value,
        )
        data = self.sweep()
        self.assertEqual((data.library_total, data.firing_total), (1, 0))
        self.assertEqual((data.overlapping, data.starting), (0, 0))

    def test_a_low_impact_event_is_not_in_the_library_at_all(self):
        """档位不是「高」的事件根本不进这个页面的分母——它不会开窗，也不该稀释库况。

        若它被算进 `library_total`，空库那句警告就再也发不出来（库里永远「有东西」）。
        """
        _row("低档", halt_at=NOW + timedelta(days=1), impact=EventImpact.LOW.value)
        data = self.sweep()
        self.assertEqual(data.library_total, 0)
        self.assertIn("一条高影响事件都没有", self.body())


class TestTheNearestAndTheNext(_Fixture):
    def test_open_now_and_the_next_window_are_two_different_rows(self):
        _open_now("正开着")
        _row("下一个", halt_at=NOW + timedelta(days=2))
        data = self.sweep()
        self.assertEqual(data.open_now.name, "正开着")
        self.assertEqual(data.next_event.name, "下一个")

    def test_next_event_skips_the_window_open_right_now(self):
        """`next_event` 问的是**还没开始的**窗口，正开着那条不算「下一次」。"""
        _open_now("正开着")
        data = self.sweep()
        self.assertIsNone(data.next_event)

    def test_nearest_is_the_closest_window_by_start_on_either_side(self):
        """`nearest` 允许是过去的那条（给「平静期」那句用），`next_event` 不允许。"""
        _row(
            "上周的",
            halt_at=NOW - timedelta(days=2),
            resume_at=NOW - timedelta(days=2) + timedelta(hours=2),
        )
        _row("很久以后", halt_at=NOW + timedelta(days=30))
        data = self.sweep()
        self.assertEqual(data.nearest.name, "上周的")
        self.assertEqual(data.next_event.name, "很久以后")

    def test_an_empty_library_has_no_open_and_no_next_and_no_age(self):
        data = self.sweep()
        self.assertIsNone(data.open_now)
        self.assertIsNone(data.next_event)
        self.assertIsNone(data.nearest)
        self.assertIsNone(data.staleness_days)


# --------------------------------------------------------------------------- #
# 性质 2：空库与平静期分得开
# --------------------------------------------------------------------------- #


class TestTheThreeEchoes(_Fixture):
    def test_an_empty_library_says_what_that_costs(self):
        """N=0 必须被看见，且必须说到**后果**（第169 条）。

        只报「0 条」会被读成「这段时间很平静」——而它真正说的是「这个开关装上之后什么
        都不会做，并且从外面完全看不出来」。所以断言落在后果那两句上，不是落在数字上。
        """
        text = self.body()
        self.assertIn("不会拦住任何", text)
        self.assertIn("看起来与正常运行完全一样", text)
        self.assertIn("先录一条再打开", text)

    def test_a_quiet_horizon_says_the_library_is_not_empty(self):
        """平静期那一句必须带上「库里还有 N 条」，否则它与空库读起来一模一样。"""
        _row("很久以后", halt_at=NOW + timedelta(days=30))
        text = self.body()
        self.assertIn("事件库里还有 1 条", text)
        self.assertIn("离现在最近的一条是「很久以后」", text)
        self.assertIn("平静期本来就会这样", text)

    def test_the_two_quiet_shapes_do_not_borrow_each_other_s_wording(self):
        """两档互斥，**逐字互斥**：一句只该出现在它自己那一档里。

        这是本文件里唯一一条同时检查两边的用例。两句话都写「0 条」，所以混淆它们不会
        让任何断言变红——只会让读页面的人把「没接上」当成「很平静」。
        """
        empty = self.body()
        self.assertIn("一条高影响事件都没有", empty)
        self.assertNotIn("平静期本来就会这样", empty)

        _row("很久以后", halt_at=NOW + timedelta(days=30))
        quiet = self.body()
        self.assertNotIn("一条高影响事件都没有", quiet)
        self.assertIn("平静期本来就会这样", quiet)

    def test_a_busy_horizon_gets_no_extra_warning_at_all(self):
        """有窗口时不该再多说一句：这一段只在「打开之后可能什么都不做」时才有用。"""
        _open_now()
        text = self.body()
        self.assertNotIn("平静期本来就会这样", text)
        self.assertNotIn("一条高影响事件都没有", text)


class TestTheWindowLine(_Fixture):
    def test_it_says_when_a_window_is_open_right_now(self):
        _open_now("FOMC 议息")
        self.assertIn("此刻正压在「FOMC 议息」的熔断窗口里", self.body())

    def test_it_says_when_the_next_window_is(self):
        _row("CPI", halt_at=NOW + timedelta(days=3))
        self.assertIn("下次窗口：", self.body())
        self.assertIn("（CPI）", self.body())

    def test_it_says_so_when_the_next_window_is_beyond_the_horizon(self):
        """14 天之外的下一次要**说清楚它在 14 天之外**。

        否则页面上「14 天内开启 0 条」与紧接着的一个具体时刻会互相矛盾：读的人会以为
        那一条漏算了。
        """
        _row("很久以后", halt_at=NOW + timedelta(days=HORIZON + 3))
        self.assertIn(f"下次窗口在 {HORIZON} 天之外", self.body())

    def test_it_says_so_when_there_is_no_window_left_at_all(self):
        _row(
            "上周的",
            halt_at=NOW - timedelta(days=9),
            resume_at=NOW - timedelta(days=9) + timedelta(hours=2),
        )
        self.assertIn("事件库里没有未开启的窗口", self.body())


# --------------------------------------------------------------------------- #
# 渲染：前置、档位、双口径
# --------------------------------------------------------------------------- #


class TestTheRenderedPage(_Fixture):
    def test_the_body_echoes_every_prerequisite(self):
        """三件前置是「熔断会不会把仓越减越大」的答案，它们只存在于代码里。

        页面上逐条回显（并逐条写「已接通」）是为了让人不必去翻代码就能回答这个问题。
        """
        text = self.body()
        for item in breaker_switch.PREREQUISITES:
            with self.subTest(item=item):
                self.assertIn(item, text)

    def test_the_body_carries_the_horizon_and_both_calibers(self):
        """天数取 `config.EVENTS.confirm_horizon_days`（14），**不是**日报的 7 天。

        时刻一律双口径回显（北京 + UTC）：挑错一档的表，窗口就整体错几小时。
        """
        text = self.body()
        self.assertIn(f"未来 {HORIZON} 天", text)
        self.assertIn("北京时间", text)
        self.assertIn("UTC", text)

    def test_the_body_names_the_current_mode_and_reads_it_from_the_table(self):
        """档位是从流水表现读的，不是写死的。

        ②f 的写方让这一行第一次可能不是 Shadow；写死的话，命令落了库而页面仍显示
        「Shadow（只记录，不执行）」，两边都「正常」。
        """
        self.assertIn(f"当前档位：{MechanismMode.SHADOW.display}", self.body())
        self.switch(MechanismMode.EXECUTING)
        self.assertIn(f"当前档位：{MechanismMode.EXECUTING.display}", self.body())

    def test_the_summary_carries_both_counts_and_the_age(self):
        _open_now()
        text = self.summary()
        self.assertIn(f"未来 {HORIZON} 天窗口相交 1 条", text)
        self.assertIn(f"{HORIZON} 天内开启 0 条", text)
        self.assertIn("最近入库距今 0 天", text)

    def test_the_summary_says_never_when_nothing_was_ever_entered(self):
        """`None` 天必须写「从未录入」——写成 0 会让「空库」与「今天刚录过」一样。"""
        self.assertIn("从未录入过事件", self.summary())

    def test_the_read_only_helpers_are_the_page_s_own_text(self):
        """`confirmation_body` / `confirmation_summary` 与 `page()` 同源（同一次求值口）。

        三个出口各算一遍的话，两次查询之间只要有人录了一条事件，回复与流水就会写着两个
        都出自本模块、却互相矛盾的数。
        """
        briefing = breaker_switch.page(now=NOW)
        self.assertEqual(briefing.body, breaker_switch.confirmation_body(now=NOW))
        self.assertEqual(briefing.summary, breaker_switch.confirmation_summary(now=NOW))

    def test_it_never_writes_anything(self):
        """这一页是只读的：无论跑多少遍，一张表都不动。"""
        _open_now()
        for _ in range(2):
            self.body()
        self.assertEqual(RegimeMechanismSwitch.objects.count(), 0)


class TestTheClosingSummary(_Fixture):
    def test_it_says_no_window_was_open(self):
        self.assertIn("关闭时无窗口开启", breaker_switch.closing_summary(self.sweep()))

    def test_it_names_the_window_that_gets_released(self):
        """「关掉的那一刻正压在窗口里」是最需要被看见的一种关法，而且外面看不出来。

        表里窗口还在、日报照旧，只是下单不再被拦——所以这一句必须落到 `reason` 里。
        """
        _open_now("FOMC 议息")
        text = breaker_switch.closing_summary(self.sweep())
        self.assertIn("关闭时正压在「FOMC 议息」的窗口内", text)

    def test_it_asks_a_different_question_than_the_opening_summary(self):
        """两个方向的 `reason` 问的不是同一件事，所以两份摘要必须逐字不同。

        都写成「同上」或者同一句模板的话，事后读流水的人分不出哪一条是撤防。
        """
        opening = breaker_switch.confirmation_summary(now=NOW)
        closing = breaker_switch.closing_summary(self.sweep())
        self.assertIn("人工打开事件熔断", opening)
        self.assertIn("人工关闭事件熔断", closing)
        self.assertNotEqual(opening, closing)


# --------------------------------------------------------------------------- #
# 写方：唯一的一次 INSERT
# --------------------------------------------------------------------------- #


class TestFlipEventBreaker(_Fixture):
    def test_the_first_flip_is_shadow_to_executing(self):
        row = self.flip(actor_kind=ActorKind.CHAT, actor_name="tg:42")
        self.assertEqual(row.kind, MechanismKind.EVENT_BREAKER.value)
        self.assertEqual(row.from_mode, MechanismMode.SHADOW.value)
        self.assertEqual(row.to_mode, MechanismMode.EXECUTING.value)
        self.assertEqual(row.actor_kind, ActorKind.CHAT.value)
        self.assertEqual(row.actor_name, "tg:42")
        self.assertEqual(row.at, NOW)
        self.assertTrue(row.reason)

    def test_from_mode_is_read_not_assumed(self):
        """从执行态关掉时，`from_mode` 必须真的是「执行态」。

        写死 `shadow` 的话，「从执行态关掉、再打开」这一段在流水里会记成「从 Shadow
        打开」——而这条流水正是事后回答「当时它到底开没开」的唯一材料。
        """
        self.flip(MechanismMode.EXECUTING)
        row = self.flip(MechanismMode.SHADOW)
        self.assertEqual(row.from_mode, MechanismMode.EXECUTING.value)
        self.assertEqual(row.to_mode, MechanismMode.SHADOW.value)

    def test_pressing_the_same_button_again_writes_nothing(self):
        """重复按一次不该在只增不改的表里多出一条「executing → executing」。

        那条行没有任何信息，却会永久留在那里，并让「切过几次」从此多算一次。
        """
        self.flip(MechanismMode.EXECUTING)
        self.assertIsNone(self.flip(MechanismMode.EXECUTING))
        self.assertEqual(RegimeMechanismSwitch.objects.count(), 1)

    def test_an_empty_reason_is_refused_before_anything_is_written(self):
        """`reason` 是 TextField，空串在数据库那一层拦不住。

        一张「切了但不知道为什么」的流水与没有流水几乎等价——那正是留痕四件事里最贵
        的一件。所以这里拒绝，而且**一行都不写**。
        """
        for bad in ("", "   ", "\n"):
            with self.subTest(reason=bad):
                with self.assertRaises(ValueError):
                    self.flip(reason=bad)
        self.assertEqual(RegimeMechanismSwitch.objects.count(), 0)

    def test_an_empty_actor_is_refused_before_anything_is_written(self):
        for bad in ("", "  "):
            with self.subTest(actor=bad):
                with self.assertRaises(ValueError):
                    self.flip(actor_name=bad)
        self.assertEqual(RegimeMechanismSwitch.objects.count(), 0)

    def test_the_actor_name_is_trimmed_and_capped_to_the_column(self):
        row = self.flip(actor_name="  " + "长" * 200 + "  ")
        self.assertEqual(len(row.actor_name), 128)
        self.assertFalse(row.actor_name.startswith(" "))

    def test_it_touches_only_the_event_breaker_switch(self):
        """`kind` 是写死的：另外两个开关各自有入口，这里顺手打开一个还没接线的开关
        就是替第③段做决定。"""
        self.flip(MechanismMode.EXECUTING)
        self.assertEqual(
            RegimeMechanismSwitch.current(MechanismKind.MECHANISM), MechanismMode.SHADOW
        )
        self.assertEqual(
            RegimeMechanismSwitch.current(MechanismKind.REGIME_GATE),
            MechanismMode.SHADOW,
        )

    def test_the_flip_is_what_the_halt_side_reads(self):
        """**端到端的那一条**：这一行流水就是「事件熔断层挡不挡得住」的那个开关。

        写对了但没人读（或者读到另一档）的表现是：开关在流水里是执行态，而实际下单
        通路照放行——这不会红任何别的东西。
        """
        self.assertFalse(halt.switch_open(HaltTrigger.EVENT))
        self.flip(MechanismMode.EXECUTING)
        self.assertTrue(halt.switch_open(HaltTrigger.EVENT))
        self.flip(MechanismMode.SHADOW)
        self.assertFalse(halt.switch_open(HaltTrigger.EVENT))

    def test_it_leaves_the_other_triggers_alone(self):
        """保命档没有开关（恒为真），策略档的开关归第③段（此刻仍关）。

        这条是 `HALT_TRIGGER_SWITCH` 那处刻意不对称的守卫：②f 不该顺手改动它。
        """
        self.flip(MechanismMode.EXECUTING)
        self.assertTrue(halt.switch_open(HaltTrigger.BLANKET))
        self.assertFalse(halt.switch_open(HaltTrigger.DEACTIVATION))


# --------------------------------------------------------------------------- #
# 命令行兜底入口：与 `/regime` 同一份页、同一份写方
# --------------------------------------------------------------------------- #


class _CliFixture(_Fixture):
    def manage(self, *args) -> str:
        """跑命令，返回 stdout。**不叫 `run`**（框架自己的入口）。"""
        out = StringIO()
        call_command("event_breaker", *args, stdout=out, stderr=StringIO())
        return out.getvalue()


class TestTheManagementCommand(_CliFixture):
    #: 覆盖提示的开头。关掉时正压在窗口里是最需要被看见的一种关法，而它从别处看不见。
    RELEASE_WARNING = "关掉的这一刻正压在窗口里"

    def test_no_action_is_read_only(self):
        """不带动作参数时是只读的：这条命令要能在「只是想看一眼」的时候安全地跑。"""
        out = self.manage()
        self.assertIn(PAGE_TITLE, out)
        self.assertIn("没有动作参数", out)
        self.assertEqual(RegimeMechanismSwitch.objects.count(), 0)

    def test_it_opens_the_switch_with_the_cli_actor(self):
        out = self.manage("--on", "--actor", "张三")
        row = RegimeMechanismSwitch.objects.get()
        self.assertEqual(row.to_mode, MechanismMode.EXECUTING.value)
        self.assertEqual(row.actor_kind, ActorKind.CLI.value)
        self.assertEqual(row.actor_name, "张三")
        self.assertIn("人工打开事件熔断", row.reason)
        self.assertIn("已切换", out)
        self.assertIn("张三", out)

    def test_the_actor_defaults_to_the_system_user(self):
        """CLI 通路的「谁」是这台机器上的人，不是任务名。"""
        self.manage("--on")
        self.assertEqual(
            RegimeMechanismSwitch.objects.get().actor_name, getpass.getuser()
        )

    def test_off_uses_the_closing_reason(self):
        """两个方向的 `reason` 由两个函数生成，不是同一份模板。"""
        self.manage("--on")
        self.manage("--off")
        rows = list(RegimeMechanismSwitch.objects.order_by("at", "id"))
        self.assertEqual(len(rows), 2)
        self.assertIn("人工打开事件熔断", rows[0].reason)
        self.assertIn("人工关闭事件熔断", rows[1].reason)
        self.assertEqual(rows[1].to_mode, MechanismMode.SHADOW.value)

    def test_pressing_the_same_button_again_says_so(self):
        self.manage("--on")
        out = self.manage("--on")
        self.assertIn("本来就是", out)
        self.assertEqual(RegimeMechanismSwitch.objects.count(), 1)

    def test_it_warns_loudly_when_closing_inside_a_window(self):
        """关掉的那一刻正压在窗口里：窗口不再拦人，而这件事从别处看不出来。"""
        _open_now("FOMC 议息")
        self.manage("--on")
        out = self.manage("--off")
        self.assertIn(self.RELEASE_WARNING, out)

    def test_it_does_not_warn_when_no_window_was_open(self):
        """误报的代价是人对这句提示脱敏，而它正是唯一会说出这件事的地方。"""
        self.manage("--on")
        out = self.manage("--off")
        self.assertNotIn(self.RELEASE_WARNING, out)

    def test_it_renders_through_the_shared_page(self):
        """页与它的话都由 `breaker_switch.page` 出：命令里一旦自己拼字符串，命令行看到
        的数与聊天里看到的数迟早会分家。"""
        with patch.object(breaker_switch, "page", wraps=breaker_switch.page) as spy:
            out = self.manage("--on")
        self.assertEqual(spy.call_count, 1)
        self.assertIn(PAGE_TITLE, out)
