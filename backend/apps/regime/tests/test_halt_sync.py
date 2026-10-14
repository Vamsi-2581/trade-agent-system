"""窗口同步：事实 → 声明行（第②段单元 ②c）。

`test_halt.py` 钉的是**读**的一侧（一组声明怎么求值成「挡不挡得住」）；这个文件钉的是
**写**的一侧（事件表与判定表上的事实怎么变成那组声明）。两侧分开测，是因为它们坏掉的
样子完全不同：读侧写坏的表现是「该拦的没拦住」，写侧写坏的表现是「表里根本没有那一行」
——后者看起来与「本来就没有事件」一模一样，没有任何别的信号能暴露它。

## 两段：纯规划 + 落库

`_plan` / `_event_rows` 刻意把事件列表与 `RegimeState` 当参数收（`halt_sync.py` 模块
docstring 写了理由），所以合并、推进、作用域这些**语义**能用构造出来的对象直接测，不必
先往库里造一批事件。这一半跑在 `SimpleTestCase` 上：那个类**就是**「这几条不许碰库」的
执行形态——哪天规划函数忍不住自己去查库，这些用例会当场红，而不是悄悄变慢。

落库那一半（幂等、唯一键、解除原因、开关）必须真库：`uniq_live_halt_declaration` 是
数据库约束，而 `sync()` 的全部难处都长在它上面。

## 第三段：策略停用那一档从哪来（第③段）

事件与保命档的期望集本模块自己算得出来；策略停用决策的算不出来（那要一整条池化推导），
所以 `sync(gate_plan=…)` 接收 `gate.derive` 的产出，本轮**只**对账那一档。这两件事的坏法
也不同：本模块算错的表现是「拦错了」，而「一轮里管了两档」的表现是两个 300 秒任务在同一
把唯一键上撞 `UniqueViolation`——后者取决于两个任务相差几毫秒，比读代码难得多的多。
`TestTheGateRound` 钉的就是这条分界。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone as dt_timezone
from uuid import uuid4

from django.test import SimpleTestCase, TestCase

from apps.regime import deactivation, events, gate, halt, halt_sync
from apps.regime.halt_sync import (
    ACTOR_NAME,
    BLANKET_LABEL,
    CLOSE_REASON_EVENT_CANCELLED,
    CLOSE_REASON_NO_LONGER_COVERS,
    CLOSE_REASON_REGIME_LEFT,
    CLOSE_REASON_WINDOW_ENDED,
    GATE_ACTOR_NAME,
    PlannedRow,
)
from apps.regime.models import (
    ActorKind,
    EventImpact,
    EventScope,
    EventStatus,
    HaltDeclaration,
    HaltTrigger,
    MajorEvent,
    MechanismKind,
    MechanismMode,
    RegimeJudgement,
    RegimeMechanismSwitch,
)
from apps.regime.quant import BaseRegime
from apps.regime.slice import REASON_DISPLAY, REASON_HIGH_VOL_BLANKET

#: 「此刻」：北京 2026-09-23 12:00 == UTC 04:00。所有窗口都相对它写。
NOW = datetime(2026, 9, 23, 4, 0, tzinfo=dt_timezone.utc)


# --------------------------------------------------------------------------- #
# 造数据：内存里的事件（不落库）
# --------------------------------------------------------------------------- #


def _event(
    name: str = "FOMC 议息",
    *,
    halt_at: datetime,
    resume_at: datetime | None = None,
    event_time: datetime | None = None,
    impact: str = EventImpact.HIGH.value,
    status: str = EventStatus.SCHEDULED.value,
    scope_kind: str = EventScope.MARKET.value,
    symbols: list[str] | None = None,
) -> MajorEvent:
    """一条**没有落库**的事件。`triggers_halt` 只读 `impact` / `status` 两列，所以不落库
    也判得出该不该拦——这正是规划函数被写成纯函数换来的。

    `resume_at` 的默认值取 `max(halt_at, NOW) + 1h`，而不是「`halt_at` 加一小时」：
    `_pick_segment` 用的是**半开区间** `start <= at < end`，所以 `halt_at + 1h` 这种看着
    「还有一小时」的写法，在 `halt_at` 落在 NOW 之前时会造出一个**在 NOW 已经结束**的
    窗口，行反而一条都规划不出来（红的样子是「没计划出那一行」，与本文件要测的东西完全
    无关）。这里保证默认窗口**一定跨越 NOW**，需要测边界的用例自己传 `resume_at=`。
    """
    return MajorEvent(
        name=name,
        scope_kind=scope_kind,
        symbols=symbols if symbols is not None else [],
        event_time=event_time or halt_at,
        impact=impact,
        halt_at=halt_at,
        resume_at=(
            resume_at
            if resume_at is not None
            else max(halt_at, NOW) + timedelta(hours=1)
        ),
        status=status,
        created_by="tester",
    )


def _state(
    regime: str | BaseRegime | None = None, *, effective_at: datetime | None = None
) -> deactivation.RegimeState:
    """一份生效判定。`regime=None` = 冷启动（`RegimeState()` 的默认形状）。"""
    value = regime.value if isinstance(regime, BaseRegime) else regime
    return deactivation.RegimeState(regime=value, effective_at=effective_at)


def _keys(rows: list[PlannedRow]) -> set[tuple[str, str]]:
    return {(row.trigger.value, row.scope) for row in rows}


# --------------------------------------------------------------------------- #
# 事件窗口：合并、推进、作用域（纯函数）
# --------------------------------------------------------------------------- #


class TestEventWindows(SimpleTestCase):
    """一条事件 → 一行声明；重叠或相接的合成一行；空隙不抹。"""

    def test_one_event_makes_one_row(self):
        event = _event("FOMC 议息", halt_at=NOW - timedelta(hours=1))
        rows = halt_sync._event_rows([event], NOW)

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertIs(row.trigger, HaltTrigger.EVENT)
        self.assertEqual(row.scope, halt.global_scope())
        self.assertEqual(row.label, "FOMC 议息")
        # **窗口取自事实里存好的时刻**，不是跑任务的那一刻（halt_sync 模块 docstring）：
        # 记成 now() 的话，任务延迟会让日报与事件表对「什么时候开始拦的」给出两个答案。
        self.assertEqual(row.opened_at, event.halt_at)
        self.assertEqual(row.expires_at, event.resume_at)

    def test_the_reason_writes_the_event_moment_and_the_window(self):
        event = _event("CPI 公布", halt_at=NOW - timedelta(hours=1))
        row = halt_sync._event_rows([event], NOW)[0]

        self.assertIn("高影响事件熔断窗口", row.reason)
        self.assertIn("CPI 公布", row.reason)
        # 两个时刻都用 `events.format_moment`（北京 + UTC 双写）：事件库、日报第③段与
        # 这条依据的读者是同一批人，各写一句就是「同一件事三处说法不同」。
        self.assertIn(events.format_moment(event.event_time), row.reason)
        self.assertIn(events.format_moment(event.halt_at), row.reason)
        self.assertIn(events.format_moment(event.resume_at), row.reason)

    def test_a_medium_impact_event_is_not_planned(self):
        """档位不是「高」的事件**不产生熔断**（ADR 0002 的两轴：档位那一轴才管熔断）。"""
        event = _event(
            "低影响事件",
            halt_at=NOW - timedelta(hours=1),
            impact=EventImpact.MEDIUM.value,
        )
        self.assertEqual(halt_sync._event_rows([event], NOW), [])

    def test_a_cancelled_event_is_not_planned(self):
        event = _event(
            "已取消的会议",
            halt_at=NOW - timedelta(hours=1),
            status=EventStatus.CANCELLED.value,
        )
        self.assertEqual(halt_sync._event_rows([event], NOW), [])

    def test_windows_that_touch_are_merged_into_one_segment(self):
        """**相接也算并**：`[10:00,12:00)` 与 `[12:00,14:00)` 之间没有一刻是不拦的。

        拆成两段会凭空造出一个空隙，而空隙的后果是把「下一段提前写进表」的时机推迟到
        上一段结束之后——那正是这套写法要消掉的敞口。
        """
        first = _event("甲", halt_at=NOW - timedelta(hours=2), resume_at=NOW)
        second = _event("乙", halt_at=NOW, resume_at=NOW + timedelta(hours=2))

        rows = halt_sync._event_rows([first, second], NOW)

        self.assertEqual(len(rows), 1, "相接的两段必须并成一行，否则中间那一刻谁都不拦")
        self.assertEqual(rows[0].label, "甲、乙")
        self.assertEqual(rows[0].opened_at, first.halt_at)
        self.assertEqual(rows[0].expires_at, second.resume_at)
        # 多事件时把「为什么是一行」写出来：只看到 label 里两个事件名的话，会以为机制把
        # 两件事混成了一件，而真相是唯一键只允许同源同作用域活一行。
        self.assertIn("2 条高影响事件的窗口在这个作用域上重叠或相接", rows[0].reason)
        self.assertIn("甲", rows[0].reason)
        self.assertIn("乙", rows[0].reason)

    def test_a_gap_between_two_windows_is_not_merged_away(self):
        """有空隙的两段**不并**，而且表里留下的是**下一段**。

        把 gap 抹掉的写法（比如取 `min(halt_at)` 到 `max(resume_at)`）会真的多拦一段
        时间——多拦是「用户眼里机制莫名在拦」，比一段有界的敞口难排查得多，而事件熔断
        又没有豁免通道。
        """
        past = _event(
            "刚过去的",
            halt_at=NOW - timedelta(hours=2),
            resume_at=NOW - timedelta(hours=1),
        )
        upcoming = _event(
            "待开的",
            halt_at=NOW + timedelta(hours=1),
            resume_at=NOW + timedelta(hours=2),
        )

        rows = halt_sync._event_rows([past, upcoming], NOW)

        self.assertEqual(len(rows), 1)
        self.assertEqual(
            rows[0].label, "待开的", "空隙期写的是**下一段**，不是刚结束的那段"
        )
        self.assertEqual(
            rows[0].opened_at, upcoming.halt_at, "窗口不许往前伸去盖住空隙"
        )

    def test_the_resume_moment_is_already_out_of_the_window(self):
        """**截止那一刻已不再拦**（`halt.live_declarations` 的 `expires_at__gt` 同向）。

        第②段之前那个口径写的是 `resume_at__gte now`（闭区间），改判据的这一拍是这条
        单元唯一一处**行为变化**：`now == resume_at` 从「拦」变成「不拦」。写 `<=` 的话，
        多出来的那一拍正好落在事件结果已经出来之后。
        """
        event = _event("正好到点", halt_at=NOW - timedelta(hours=1), resume_at=NOW)
        self.assertEqual(
            halt_sync._event_rows([event], NOW), [], "取等号方向错了就是多拦一拍"
        )

    def test_a_window_entirely_in_the_past_leaves_no_row(self):
        event = _event(
            "上周的", halt_at=NOW - timedelta(days=3), resume_at=NOW - timedelta(days=2)
        )
        self.assertEqual(halt_sync._event_rows([event], NOW), [])

    def test_the_next_window_is_planned_before_it_opens(self):
        """**提前写入**是这套写法的全部要点**：一行只有一个窗口，窗口若只覆盖「正在拦」
        的那段，两段之间任何一轮任务没跑都会留下敞口，而事件熔断不可人工豁免。

        代价是「还没生效的行可以被无痕改写」，写在 `HaltDeclaration` 的 docstring 里。
        """
        later = _event("稍后", halt_at=NOW + timedelta(hours=3))
        rows = halt_sync._event_rows([later], NOW)

        self.assertEqual(len(rows), 1)
        self.assertGreater(rows[0].opened_at, NOW, "提前写的行，窗口起点在未来")


class TestEventScopes(SimpleTestCase):
    """`market` 一行 `global`；`symbols` **每个品种一行**；空列表什么都不写。"""

    def test_a_market_event_covers_the_global_scope(self):
        event = _event("非农", halt_at=NOW - timedelta(hours=1))
        rows = halt_sync._event_rows([event], NOW)
        self.assertEqual(_keys(rows), {(HaltTrigger.EVENT.value, halt.global_scope())})

    def test_a_symbol_event_makes_one_row_per_symbol(self):
        """**不把多个品种塞进一行**：`scope` 存的是一个记号，而 `halt._matches` 是按品种
        逐个比的——塞进去的行谁都匹配不上，表现为「声明写了却拦不住」。
        """
        event = _event(
            "某币种事件",
            halt_at=NOW - timedelta(hours=1),
            scope_kind=EventScope.SYMBOLS.value,
            symbols=["BTC/USDT", "ETH/USDT"],
        )
        rows = halt_sync._event_rows([event], NOW)

        self.assertEqual(
            _keys(rows),
            {
                (HaltTrigger.EVENT.value, halt.symbol_scope("BTC/USDT")),
                (HaltTrigger.EVENT.value, halt.symbol_scope("ETH/USDT")),
            },
        )
        self.assertEqual({row.label for row in rows}, {"某币种事件"})

    def test_a_symbol_event_with_no_symbols_writes_nothing(self):
        """**空列表 ≠ 全市场**：`EventScope` 没有「默认全市场」这条退路（`applies_to` 对空
        列表恒为假），这里跟着它走——一条没有作用域的事件不该拦住全场。
        """
        event = _event(
            "没写品种",
            halt_at=NOW - timedelta(hours=1),
            scope_kind=EventScope.SYMBOLS.value,
            symbols=[],
        )
        self.assertEqual(halt_sync._event_rows([event], NOW), [])

    def test_scopes_are_partitioned_not_merged(self):
        """作用域不同就是两行——唯一键的另一半是作用域，作用域相同才谈得上合并。"""
        wide = _event("全市场", halt_at=NOW - timedelta(hours=1))
        narrow = _event(
            "只压 BTC",
            halt_at=NOW - timedelta(hours=1),
            scope_kind=EventScope.SYMBOLS.value,
            symbols=["BTC/USDT"],
        )
        rows = halt_sync._event_rows([wide, narrow], NOW)

        self.assertEqual(len(rows), 2)
        self.assertEqual({row.label for row in rows}, {"全市场", "只压 BTC"})


class TestBlanketRow(SimpleTestCase):
    """高波动档（保命档）：只在**生效判定本身就是高波动**时写，`expires_at` 恒为不定。"""

    def test_a_high_vol_state_is_the_blanket_row(self):
        effective = NOW - timedelta(days=1)
        rows = halt_sync._plan(
            [], NOW, state=_state(BaseRegime.HIGH_VOL, effective_at=effective)
        )

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertIs(row.trigger, HaltTrigger.BLANKET)
        self.assertEqual(row.scope, halt.global_scope())
        self.assertEqual(row.label, BLANKET_LABEL)
        self.assertEqual(row.opened_at, effective)
        # 不定：它随阶段起落，没有预先知道的截止时刻。失效由「阶段离开高波动」那一轮写
        # `closed_at`，不靠到期。
        self.assertIsNone(row.expires_at)
        self.assertIn(REASON_DISPLAY[REASON_HIGH_VOL_BLANKET], row.reason)
        self.assertIn("保命档不做适用性判断，与证据无关", row.reason)

    def test_a_non_high_vol_state_writes_no_blanket_row(self):
        for regime in (BaseRegime.DOWNTREND, BaseRegime.UPTREND, BaseRegime.RANGE):
            with self.subTest(regime=regime):
                rows = halt_sync._plan(
                    [], NOW, state=_state(regime, effective_at=NOW - timedelta(days=1))
                )
                self.assertEqual(rows, [])

    def test_a_cold_state_writes_no_blanket_row(self):
        self.assertEqual(halt_sync._plan([], NOW, state=_state()), [])

    def test_a_high_vol_state_without_an_effective_moment_raises(self):
        """「阶段是高波动、但不知道它从哪一刻起」是判定表不可能造出来的形状。

        **不用 now() 兜底**：那会把写入方自己的钟点混进事实里，而这一行的 `opened_at`
        正是「什么时候开始拦的」的唯一答案。宁可炸。
        """
        with self.assertRaises(RuntimeError):
            halt_sync._plan(
                [], NOW, state=_state(BaseRegime.HIGH_VOL, effective_at=None)
            )

    def test_the_blanket_row_joins_the_events_rather_than_replacing_them(self):
        """两层各占一把键（触发源不同），所以同时存在——用户眼里是一个集合。"""
        event = _event("FOMC", halt_at=NOW - timedelta(hours=1))
        rows = halt_sync._plan(
            [event],
            NOW,
            state=_state(BaseRegime.HIGH_VOL, effective_at=NOW - timedelta(days=1)),
        )

        self.assertEqual(
            _keys(rows),
            {
                (HaltTrigger.EVENT.value, halt.global_scope()),
                (HaltTrigger.BLANKET.value, halt.global_scope()),
            },
        )


# --------------------------------------------------------------------------- #
# 落库：幂等、唯一键、解除原因、开关（真库）
# --------------------------------------------------------------------------- #


class _SyncTestBase(TestCase):
    #: 每个用例都把 `now` 钉住：`sync` 的解除时刻与原因都取自它，不钉就没法断言。
    AT = NOW

    def _create_event(self, name: str = "FOMC 议息", **over) -> MajorEvent:
        over.setdefault("halt_at", NOW - timedelta(hours=1))
        over.setdefault("resume_at", NOW + timedelta(hours=1))
        over.setdefault("event_time", NOW)
        over.setdefault("scope_kind", EventScope.MARKET.value)
        over.setdefault("symbols", [])
        over.setdefault("impact", EventImpact.HIGH.value)
        over.setdefault("status", EventStatus.SCHEDULED.value)
        return MajorEvent.objects.create(name=name, created_by="tester", **over)

    def _judge(self, regime, *, effective_at: datetime) -> RegimeJudgement:
        return RegimeJudgement.objects.create(
            symbol="BTC/USDT",
            attribute_date=date(2026, 9, 22),
            effective_at=effective_at,
            base_regime=regime.value if hasattr(regime, "value") else regime,
            escalation="",
            effective_regime=regime.value if hasattr(regime, "value") else regime,
        )

    def _switch(
        self, kind: MechanismKind, mode: MechanismMode
    ) -> RegimeMechanismSwitch:
        return RegimeMechanismSwitch.objects.create(
            kind=kind.value,
            from_mode=MechanismMode.SHADOW.value,
            to_mode=mode.value,
            at=NOW - timedelta(days=1),
            actor_kind=ActorKind.CLI.value,
            actor_name="ops",
            reason="测试",
        )


class TestSyncPersistence(_SyncTestBase):
    def test_sync_creates_the_row_then_turns_into_a_no_op(self):
        """**幂等是对账的全部前提**：期望值逐字来自事实里存好的时刻，所以连着跑两轮，
        第二轮必然整表空转。300 秒一轮全表重算就是靠这一条才敢跑。
        """
        event = self._create_event()
        first = halt_sync.sync(now=self.AT)

        self.assertEqual(
            first, {"created": 1, "updated": 0, "unchanged": 0, "closed": 0}
        )
        row = HaltDeclaration.objects.get()
        self.assertEqual(row.trigger, HaltTrigger.EVENT.value)
        self.assertEqual(row.opened_at, event.halt_at)
        self.assertEqual(row.expires_at, event.resume_at)
        self.assertIsNone(row.closed_at)
        # 写入方的署名是这张表的一半价值（「谁投的」）：答不出「谁」，剩下就是一个看不出
        # 真假的布尔。
        self.assertEqual(row.actor_kind, ActorKind.TASK.value)
        self.assertEqual(row.actor_name, ACTOR_NAME)

        second = halt_sync.sync(now=self.AT)
        self.assertEqual(
            second, {"created": 0, "updated": 0, "unchanged": 1, "closed": 0}
        )

    def test_the_row_is_rewritten_in_place_when_the_event_moves(self):
        """改期 → **原地改写**，不关一行再开一行。

        关开一次会在这张表里留下「声明—解除」的假历史，而这张表是排查停用原因的依据。
        代价（还没生效的行可以被无痕改写）写在 `HaltDeclaration` 的 docstring 里。
        """
        event = self._create_event()
        halt_sync.sync(now=self.AT)
        before = HaltDeclaration.objects.get()

        event.resume_at = NOW + timedelta(hours=5)
        event.save(update_fields=["resume_at"])

        summary = halt_sync.sync(now=self.AT)
        after = HaltDeclaration.objects.get()

        self.assertEqual(
            summary, {"created": 0, "updated": 1, "unchanged": 0, "closed": 0}
        )
        self.assertEqual(after.pk, before.pk, "同一把键上只能有一行活着——原地改写")
        self.assertEqual(after.expires_at, NOW + timedelta(hours=5))

    def test_two_windows_on_the_same_key_never_coexist(self):
        """**唯一键逼出来的写法**（`uniq_live_halt_declaration` 只看 `closed_at`，不知道
        `expires_at`）：同一个源在同一个作用域上，同一时刻只能有一行——哪怕两件事的窗口
        根本不重叠。

        于是这一行记的是「这个源此刻的态度」：正在拦就是当前这段，没在拦就是**下一段**。
        """
        _early = self._create_event(
            "早的", halt_at=NOW - timedelta(hours=2), resume_at=NOW + timedelta(hours=1)
        )
        later = self._create_event(
            "晚的", halt_at=NOW + timedelta(hours=2), resume_at=NOW + timedelta(hours=4)
        )

        halt_sync.sync(now=self.AT)
        self.assertEqual(HaltDeclaration.objects.count(), 1)
        self.assertEqual(HaltDeclaration.objects.get().label, "早的")

        # 空隙期（早的已结束、晚的还没开）：表里的那一行推进到**下一段**，窗口起点在未来
        # ⇒ `live_declarations` 不认它生效，所以它不拦；而到点时它已经在表里了。
        gap = halt_sync.sync(now=NOW + timedelta(hours=1, minutes=30))

        self.assertEqual(gap, {"created": 0, "updated": 1, "unchanged": 0, "closed": 0})
        row = HaltDeclaration.objects.get()
        self.assertEqual(row.label, "晚的")
        self.assertEqual(row.opened_at, later.halt_at)
        self.assertEqual(
            halt.live_declarations(now=NOW + timedelta(hours=1, minutes=30)), []
        )
        # ↑ 空隙期不拦：这一条与「提前写」是同一次改写的两面，缺一条就说不清代价。

        self.assertEqual(
            halt_sync.sync(now=NOW + timedelta(hours=3)),
            {"created": 0, "updated": 0, "unchanged": 1, "closed": 0},
        )
        self.assertEqual(HaltDeclaration.objects.count(), 1)

    def test_a_row_whose_window_ended_is_closed_with_its_reason(self):
        self._create_event()
        halt_sync.sync(now=self.AT)

        summary = halt_sync.sync(now=NOW + timedelta(hours=2))

        self.assertEqual(summary["closed"], 1)
        row = HaltDeclaration.objects.get()
        self.assertEqual(row.closed_at, NOW + timedelta(hours=2))
        # **解除靠写 `closed_at`，不靠删行**：「这条声明存在过」必须留着（ADR 0001）。
        self.assertEqual(row.closed_reason, CLOSE_REASON_WINDOW_ENDED)

    def test_a_cancelled_event_is_a_different_close_reason(self):
        event = self._create_event()
        halt_sync.sync(now=self.AT)

        event.status = EventStatus.CANCELLED.value
        event.save(update_fields=["status"])
        halt_sync.sync(now=self.AT)

        self.assertEqual(
            HaltDeclaration.objects.get().closed_reason, CLOSE_REASON_EVENT_CANCELLED
        )

    def test_a_downgraded_event_is_a_different_close_reason_again(self):
        """改档（高 → 中）之后不再覆盖此刻。四个码按「先看事实自己，再看是不是有人撤了它」
        排——分不出是哪种的话，排查时就得自己去事件表里逐个比对。
        """
        event = self._create_event()
        halt_sync.sync(now=self.AT)

        event.impact = EventImpact.MEDIUM.value
        event.save(update_fields=["impact"])
        halt_sync.sync(now=self.AT)

        self.assertEqual(
            HaltDeclaration.objects.get().closed_reason, CLOSE_REASON_NO_LONGER_COVERS
        )

    def test_the_blanket_row_appears_and_leaves_with_the_phase(self):
        self._judge(BaseRegime.HIGH_VOL, effective_at=NOW - timedelta(days=1))
        halt_sync.sync(now=self.AT)

        row = HaltDeclaration.objects.get()
        self.assertEqual(row.trigger, HaltTrigger.BLANKET.value)
        self.assertEqual(row.label, BLANKET_LABEL)
        self.assertIsNone(row.expires_at)

        # 阶段离开高波动：保命档的行没有截止时刻，解除只可能是「阶段走了」。
        self._judge(BaseRegime.DOWNTREND, effective_at=NOW - timedelta(hours=1))
        halt_sync.sync(now=self.AT)

        self.assertEqual(HaltDeclaration.objects.count(), 1)
        self.assertEqual(
            HaltDeclaration.objects.get().closed_reason, CLOSE_REASON_REGIME_LEFT
        )

    def test_a_deactivation_row_is_left_alone(self):
        """`DEACTIVATION` 的行归第③段（`REGIME_GATE` 管辖），本模块**算不出**它的期望值。

        把它当成「计划里没有」就会在每天第一轮把别人的声明解除掉——一条停用决策会静默
        失效，而它看起来与「本来就没停」一模一样。
        """
        HaltDeclaration.objects.create(
            trigger=HaltTrigger.DEACTIVATION.value,
            scope=halt.strategy_scope("s-1"),
            label="某策略",
            opened_at=NOW - timedelta(hours=1),
            reason="停用决策",
            actor_kind=ActorKind.TASK.value,
            actor_name="regime.deactivation",
        )

        summary = halt_sync.sync(now=self.AT)

        self.assertEqual(summary["closed"], 0)
        row = HaltDeclaration.objects.get()
        self.assertIsNone(row.closed_at)


class TestSyncWritesBeforeTheSwitchIsOn(_SyncTestBase):
    """**写与开关无关**：声明说「这个源想拦」，开关说「这个源启用了没有」。"""

    def test_the_row_is_written_even_while_the_switch_is_shadow(self):
        """Shadow 期不写行的话，「出 Shadow」那一刻表是空的——什么都不拦，一直到下一个
        窗口才有行，等于把出 Shadow 的时点本身变成一段敞口。
        """
        self._create_event()
        self.assertEqual(RegimeMechanismSwitch.objects.count(), 0)

        halt_sync.sync(now=self.AT)

        self.assertEqual(HaltDeclaration.objects.count(), 1)
        self.assertEqual(halt.live_declarations(now=self.AT).__len__(), 1, "行是活的")
        self.assertEqual(
            halt.blocking_declarations(now=self.AT), [], "但开关关着，不拦"
        )
        self.assertEqual(halt.block_reason("BTC/USDT", now=self.AT), "")

    def test_flipping_the_switch_on_mid_window_blocks_immediately(self):
        """上一条的推论：出 Shadow **不需要等下一轮同步**——行已经在表里了，判定函数当场
        命中。「出 Shadow 之后要先等 300 秒」这种表现会让人以为开关没生效。
        """
        self._create_event()
        halt_sync.sync(now=self.AT)

        self._switch(MechanismKind.EVENT_BREAKER, MechanismMode.EXECUTING)

        self.assertIn("FOMC 议息", halt.block_reason("BTC/USDT", now=self.AT))


class TestTheGateRound(_SyncTestBase):
    """`sync(gate_plan=…)`：策略停用那一档。

    **一轮只对账一档**不是优化，是正确性：事件与保命档由 `regime-sync-halt-windows` 驱动，
    策略停用档由 `regime-gate-sync` 驱动，两个 300 秒任务各自全表重算。若两边都认为「表里
    不在我期望集里的行就是我该解除的」，它们会在同一把唯一键上同时 `create()`——撞的是
    `uniq_live_halt_declaration`，而撞不撞取决于两个任务相差几毫秒。这里钉住的那条分界
    （trigger 不在本轮 `sources` 里就整行跳过）就是唯一挡着它的东西。
    """

    def _live_deactivation_row(self, strategy_id) -> HaltDeclaration:
        return HaltDeclaration.objects.create(
            trigger=HaltTrigger.DEACTIVATION.value,
            scope=halt.strategy_scope(strategy_id),
            label=f"策略停用决策：{strategy_id}",
            opened_at=NOW - timedelta(hours=5),
            reason="旧一轮写的",
            actor_kind=ActorKind.TASK.value,
            actor_name=GATE_ACTOR_NAME,
        )

    def test_a_gate_round_writes_the_row_with_its_own_signature(self):
        """这一档的行**只可能**由 `regime-gate-sync` 写，所以署名是另一个名字。

        共用一个署名的话，「这行是谁写的」的答案就变成「两个任务之一」——而排查时真正想
        知道的恰恰是哪一个（两档坏掉的样子完全不同）。
        """
        plan = gate.GatePlan(
            declarations=(
                gate.Declaration(
                    strategy_id="s-1",
                    label="策略停用决策：均值回归（高波动不适配）",
                    reason="均值回归 在当前「高波动」阶段判为不适用。",
                    opened_at=NOW - timedelta(hours=3),
                ),
            )
        )

        summary = halt_sync.sync(
            now=self.AT, gate_plan=plan, actor_name=GATE_ACTOR_NAME
        )

        self.assertEqual(
            summary, {"created": 1, "updated": 0, "unchanged": 0, "closed": 0}
        )
        row = HaltDeclaration.objects.get()
        self.assertEqual(row.trigger, HaltTrigger.DEACTIVATION.value)
        self.assertEqual(row.scope, halt.strategy_scope("s-1"))
        # **`opened_at` 取自 plan，不取自 `now`**：策略停用没有预先知道的截止时刻，所以
        # 起点是这一行唯一的时间事实，而它必须幂等（否则 300 秒一轮会把窗口起点一路推后）。
        self.assertEqual(row.opened_at, NOW - timedelta(hours=3))
        self.assertIsNone(row.expires_at)
        self.assertEqual(row.actor_kind, ActorKind.TASK.value)
        self.assertEqual(row.actor_name, GATE_ACTOR_NAME)
        self.assertNotEqual(GATE_ACTOR_NAME, ACTOR_NAME)

        again = halt_sync.sync(now=self.AT, gate_plan=plan, actor_name=GATE_ACTOR_NAME)
        self.assertEqual(
            again, {"created": 0, "updated": 0, "unchanged": 1, "closed": 0}
        )

    def test_a_gate_round_leaves_the_event_and_blanket_rows_alone(self):
        """本轮的 `sources` 只有 `DEACTIVATION`：另外两档的行既不是「计划里没有」，也不是
        「该解除」，而是**下一轮别的任务的事**。把它们当成前者就会静默解除一条事件熔断。
        """
        self._create_event()
        self._judge(BaseRegime.HIGH_VOL, effective_at=NOW - timedelta(days=1))
        halt_sync.sync(now=self.AT)
        self.assertEqual(
            HaltDeclaration.objects.filter(closed_at__isnull=True).count(), 2
        )

        plan = gate.GatePlan(
            declarations=(
                gate.Declaration(
                    strategy_id="s-1",
                    label="策略停用决策：某策略",
                    reason="某策略 在当前阶段判为不适用。",
                    opened_at=NOW - timedelta(hours=3),
                ),
            ),
            close_reasons={"s-9": gate.CLOSE_BECAME_FIT},
        )
        summary = halt_sync.sync(
            now=self.AT, gate_plan=plan, actor_name=GATE_ACTOR_NAME
        )

        self.assertEqual(
            summary, {"created": 1, "updated": 0, "unchanged": 0, "closed": 0}
        )
        self.assertEqual(
            HaltDeclaration.objects.filter(closed_at__isnull=True).count(), 3
        )
        # 事件与保命档那两行连字段都没被碰过（`_rewrite` 的「没变化就不写」）。
        self.assertEqual(
            HaltDeclaration.objects.filter(
                trigger__in=[HaltTrigger.EVENT.value, HaltTrigger.BLANKET.value],
                closed_at__isnull=True,
            ).count(),
            2,
        )

    def test_the_plan_keying_is_translated_to_the_scope_before_it_is_used(self):
        """**plan 的键是策略 id（可能是 UUID），表里的键是 `strategy:<id>`。**

        两个写方各说各的话：判定层只认策略，作用域是这张表的事（`gate.Declaration` 的
        docstring 明写了）。翻译在 `_gate_planned` 一处发生，所以解除原因也跟着翻成作用域
        ——否则「`close_reasons` 拿得到码、行却匹配不上」这种失配会表现成「活行解除不掉」，
        也就是一条停用决策一直拦着，看不出是 bug。
        """
        sid = uuid4()
        row = self._live_deactivation_row(sid)
        plan = gate.GatePlan(close_reasons={sid: gate.CLOSE_BECAME_FIT})

        summary = halt_sync.sync(
            now=self.AT, gate_plan=plan, actor_name=GATE_ACTOR_NAME
        )

        self.assertEqual(
            summary, {"created": 0, "updated": 0, "unchanged": 0, "closed": 1}
        )
        row.refresh_from_db()
        self.assertEqual(row.closed_at, self.AT)
        self.assertEqual(row.closed_reason, gate.CLOSE_BECAME_FIT)

    def test_a_row_the_plan_says_nothing_about_is_left_blocking(self):
        """**fail-closed**：本轮在管这一档，却对某一行既没说「声明」也没给解除原因。

        这是写入方的 bug，而两种收场不对称：猜一个码把它解除掉，表里就只剩一条「机制解除
        过它」的假记录（看起来与「本来就没声明」一样）；原样留着只是多拦一轮——吵闹，但
        看得见，而且下一轮就修正了。
        """
        row = self._live_deactivation_row("s-1")

        with self.assertLogs("apps.regime.halt_sync", level="WARNING") as captured:
            summary = halt_sync.sync(
                now=self.AT, gate_plan=gate.GatePlan(), actor_name=GATE_ACTOR_NAME
            )

        self.assertEqual(summary["closed"], 0)
        self.assertIsNone(HaltDeclaration.objects.get(pk=row.pk).closed_at)
        self.assertEqual(len(captured.records), 1)
        self.assertIn("fail-closed", captured.records[0].getMessage())
        self.assertIn(halt.strategy_scope("s-1"), captured.records[0].getMessage())
