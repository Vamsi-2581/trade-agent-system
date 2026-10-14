"""重大事件与候选事件的**录入端**（第①段单元 8ii）。

`test_config.py` 钉的是 `EventsConfig` 那几个数各自的含义；这个文件钉的是**人把一条
事件敲进来之后发生了什么**。这里一行都不断言第②段的读取行为——窗口怎么被读、熔断期
内谁被停，本模块刻意一行都不写。

十一条性质，坏掉的症状全都不是报错，而是「录进去了，但那天什么都没停」：

1. **「已知品种」只有一个出处**：回测结果 ∪ 实盘会话 ∪ 订单。没有品种目录
   （CONTEXT.md 第 147 条提到的那张表不存在），所以新开一个 BTC 会话要能立刻录 BTC
   的事件，而库里没有的写法一律拒绝——**不做模糊匹配**这一条如果退化成前缀匹配，
   错的品种会静默地什么也不影响，看起来与「今天没有事件」一模一样。
2. **带结算后缀的符号走单独分支**：`SOL/USDT:USDT` 的修法（去掉后缀）与写错名字的
   修法（核对写法）不同，合成一条报错会让人去核对一个其实写对了的名字。
3. **作用域必填，且没有「默认全市场」**：「忘了填」与「确实影响全市场」在图里长得
   一样，代价却差着几个量级。
4. **覆盖值受全局上下限约束**：拦的是手滑多打一个 0（单条事件变三天停摆）与短到
   管道跑不完的窗口。
5. **窗口在录入那一刻算死并存下来**：之后改配置不得追溯改变任何一条已入库事件的
   窗口，同 `DeactivationExemption.expires_at`。
6. **口语时刻一律按北京时间解释，带偏移的写法显式拒绝**：并存的话，「我录的是几点」
   要取决于那个后缀。
7. **「提升为高必须人工」只有一处实现**：`task` 来源的「高」被拦，且录入（`create_event`）
   与升档（`change_impact`）以及转正（`confirm_candidate`）三条路径都拦得住——后一条
   靠走前一条实现，所以这里也要有用例钉住它没有变成第二处实现。
8. **取消是终态**：改状态而不是删行，且不能改期/改档/再取消。
9. **候选转正时三件事必须由人重新说一遍**：不从候选继承时间、档位、作用域。
10. **到期清理是唯一的自动写入，且不删行**：`decided_by` 留空才读得出「机制提过、
    没人理」；它与人工否决必须可分辨。
11. **两个回显在结构上可分辨**：`describe_event` 一定有熔断窗口那一行，
    `describe_candidate` 一定没有。

DB 用例一律用真事务回滚的 `TestCase`；品种口径的用例造**真的** `BacktestResult` /
`LiveSession` / `Order` 行——`known_symbols()` 的全部意义就是「从这三张表里读」，
用假的 `known=` 参数去测它等于把这条性质测没了。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.common.time_utils import business_tz_label
from apps.regime import config, events
from apps.regime.events import EventInputError
from apps.regime.models import (
    CANDIDATE_DISCARD_EXPIRED,
    CANDIDATE_DISCARD_REJECTED,
    ActorKind,
    CandidateEvent,
    CandidateOrigin,
    CandidateStatus,
    EventChangeKind,
    EventImpact,
    EventScope,
    EventStatus,
    MajorEvent,
    MajorEventChange,
    NewsItem,
)
from apps.regime.tests.test_slicing import _StubbedConnectionReset

#: 事件本身的名义时刻：北京时间 2026-09-25 20:30 == UTC 2026-09-25 12:30。
EVENT_TEXT = "2026-09-25 20:30"
EVENT_UTC = datetime(2026, 9, 25, 12, 30, tzinfo=timezone.utc)

HUMAN = ActorKind.CLI.value
TASK = ActorKind.TASK.value


# --------------------------------------------------------------------------- #
# 造数据
# --------------------------------------------------------------------------- #


def moment(text: str) -> datetime:
    """口语时刻 → UTC。用例里到处都要用，且**必须走被断言的那条路径**。"""
    return events.parse_business_time(text)


class _WithSymbols(TestCase):
    """库里三种品种来源各造一行，用来测 `known_symbols()` 的并集口径。"""

    @classmethod
    def setUpTestData(cls):
        from apps.backtest.models import BacktestResult
        from apps.exchange.models import ExchangeAccount
        from apps.trading.models import LiveSession, Order, Strategy

        # `AUTH_USER_MODEL = "authentication.User"`：email 是必填的第一个位置参数。
        cls.user = get_user_model().objects.create_user(
            email="events@test.local", username="ops", password="pw12345"
        )
        cls.strategy = Strategy.objects.create(
            name="AlphaStem", code_path="/t/alpha.py", git_commit_hash="aaaaaaa"
        )
        BacktestResult.objects.create(
            strategy=cls.strategy,
            symbol="BTC/USDT",
            timeframe="1d",
            start_date=date(2026, 1, 1),
            end_date=date(2026, 3, 1),
            initial_capital=Decimal("10000.00"),
            final_capital=Decimal("10500.00"),
            total_return_pct=5.0,
            metrics={},
        )
        LiveSession.objects.create(
            user=cls.user,
            strategy=cls.strategy,
            symbol="ETH/USDT",
            mode="paper",
            status="running",
            initial_capital=Decimal("10000.00"),
        )
        account = ExchangeAccount.objects.create(
            exchange="binance",
            label="t",
            api_key_enc=b"x",
            api_secret_enc=b"y",
        )
        Order.objects.create(
            user=cls.user,
            exchange_account=account,
            symbol="SOL/USDT",
            side="buy",
            order_type="market",
            quantity=Decimal("1"),
        )

    def create(self, **over):
        """一条最普通的入库动作：全市场 / 高 / 人工。每处都用例里再覆盖。"""
        kwargs = {
            "name": "FOMC 议息",
            "event_time": EVENT_UTC,
            "impact": EventImpact.HIGH.value,
            "scope_kind": EventScope.MARKET.value,
            "actor_kind": HUMAN,
            "actor_name": "xl",
        }
        kwargs.update(over)
        return events.create_event(**kwargs)


# --------------------------------------------------------------------------- #
# 品种口径
# --------------------------------------------------------------------------- #


class TestKnownSymbolsComeFromTheDatabase(_WithSymbols):
    """「已知」= 三张表的并集，且**新开一个会话要能立刻录它的品种**。"""

    def test_the_union_of_three_tables(self):
        self.assertEqual(events.known_symbols(), ["BTC/USDT", "ETH/USDT", "SOL/USDT"])

    def test_no_caching_so_a_new_session_is_visible_at_once(self):
        """缓存失效的方式正是「新开了 BTC 的会话，事件却录不进去」。

        那是一个要过很久才有人报的错（录事件的人只会以为自己写错了名字），所以这里
        刻意不做缓存。用例的形状是：先录一次，再新增品种，再录一次。
        """
        self.create(scope_kind=EventScope.SYMBOLS.value, symbols=["ETH/USDT"])
        from apps.trading.models import LiveSession

        LiveSession.objects.create(
            user=self.user,
            strategy=self.strategy,
            symbol="DOGE/USDT",
            mode="paper",
            status="running",
            initial_capital=Decimal("10000.00"),
        )
        event = self.create(scope_kind=EventScope.SYMBOLS.value, symbols=["DOGE/USDT"])
        self.assertEqual(event.symbols, ["DOGE/USDT"])

    def test_the_empty_library_says_so_instead_of_listing_nothing(self):
        """库里一条都没有时，报错要说出「库里还没有任何品种」。

        只说「未知品种：BTC/USDT」会让人以为是自己拼错了。
        """
        MajorEvent.objects.all().delete()
        from apps.backtest.models import BacktestResult
        from apps.trading.models import LiveSession, Order

        BacktestResult.objects.all().delete()
        LiveSession.objects.all().delete()
        Order.objects.all().delete()
        with self.assertRaises(EventInputError) as ctx:
            events.validate_symbols(["BTC/USDT"])
        self.assertIn("库里还没有任何品种", str(ctx.exception))


class TestSymbolSpellingIsNotGuessed(_WithSymbols):
    def test_a_settlement_suffix_gets_its_own_error(self):
        """带结算后缀的符号在录入端直接拒绝，且报错要说清怎么改。"""
        with self.assertRaises(EventInputError) as ctx:
            events.validate_symbols(["SOL/USDT:USDT"])
        message = str(ctx.exception)
        self.assertIn("本系统暂不支持带结算后缀的符号", message)
        self.assertIn("SOL/USDT:USDT", message)
        self.assertIn("BASE/QUOTE", message)

    def test_the_suffix_is_rejected_even_when_the_bare_symbol_is_known(self):
        """`SOL/USDT` 已知不代表 `SOL/USDT:USDT` 可以蒙混过关——**不做前缀匹配**。"""
        with self.assertRaises(EventInputError):
            events.validate_symbols(["SOL/USDT:USDT"])

    def test_an_unknown_symbol_lists_the_candidates(self):
        """报错里必须给出候选（CONTEXT.md 第 147 条：这是唯一能让人看见失效点的位置）。"""
        with self.assertRaises(EventInputError) as ctx:
            events.validate_symbols(["SOLUSDT"])
        message = str(ctx.exception)
        self.assertIn("未知品种：SOLUSDT", message)
        for known in ("BTC/USDT", "ETH/USDT", "SOL/USDT"):
            self.assertIn(known, message)

    def test_case_is_not_normalised(self):
        """大小写归一也是一种模糊匹配——`btc/usdt` 直接拒绝。"""
        with self.assertRaises(EventInputError):
            events.validate_symbols(["btc/usdt"])

    def test_the_first_offender_wins_and_nothing_is_partially_accepted(self):
        """遇到第一个不认识的就抛，「部分接受」会让人以为整条写成了。"""
        with self.assertRaises(EventInputError) as ctx:
            events.validate_symbols(["BTC/USDT", "NOPE/USDT", "ETH/USDT"])
        self.assertIn("NOPE/USDT", str(ctx.exception))
        self.assertNotIn("ETH/USDT", str(ctx.exception).split("本系统已知")[0])

    def test_blanks_are_dropped_and_duplicates_collapse_in_order(self):
        self.assertEqual(
            events.validate_symbols([" ETH/USDT ", "", "BTC/USDT", "ETH/USDT"]),
            ["ETH/USDT", "BTC/USDT"],
        )


class TestScopeMustBeExplicit(_WithSymbols):
    def test_an_unknown_scope_kind_lists_the_choices(self):
        with self.assertRaises(EventInputError) as ctx:
            events.validate_scope("global")
        message = str(ctx.exception)
        self.assertIn("未知的作用域", message)
        self.assertIn("market（全市场）", message)
        self.assertIn("symbols（指定品种）", message)

    def test_market_with_a_symbol_list_is_contradictory(self):
        with self.assertRaises(EventInputError) as ctx:
            events.validate_scope(EventScope.MARKET.value, ["BTC/USDT"])
        self.assertIn("不能再给品种列表", str(ctx.exception))

    def test_symbols_without_a_list_is_refused(self):
        """「忘了填」与「确实影响全市场」在库里长得一样，所以这里必须拒绝。"""
        with self.assertRaises(EventInputError) as ctx:
            events.validate_scope(EventScope.SYMBOLS.value, [])
        self.assertIn("必须给出至少一个品种", str(ctx.exception))

    def test_market_normalises_to_an_empty_list(self):
        self.assertEqual(
            events.validate_scope(EventScope.MARKET.value, ["  "]), ("market", [])
        )


# --------------------------------------------------------------------------- #
# 时刻
# --------------------------------------------------------------------------- #


class TestTheInputClockIsBeijing(TestCase):
    def test_a_verbal_time_is_read_as_beijing(self):
        """北京 20:30 是 UTC 12:30——现在正是北半球的夏令时边界之外，+08:00 恒定。"""
        self.assertEqual(moment("2026-09-25 20:30"), EVENT_UTC)

    def test_the_other_separators_are_accepted(self):
        for text in ("2026/9/25 20:30", "2026-09-25T20:30", "2026-09-25 20:30:00"):
            with self.subTest(text=text):
                self.assertEqual(moment(text), EVENT_UTC)

    def test_an_offset_suffix_is_explicitly_refused(self):
        """`+08:00` 与不带后缀指同一刻、`Z` 指另一刻——并存的话语义取决于后缀。

        所以这里**显式拒绝**带偏移的写法，而不是「有偏移就按偏移解释」。
        """
        for text in (
            "2026-09-25T20:30+08:00",
            "2026-09-25T12:30Z",
            "2026-09-25t12:30z",
        ):
            with self.subTest(text=text):
                with self.assertRaises(EventInputError) as ctx:
                    events.parse_business_time(text)
                self.assertIn("不要带时区偏移", str(ctx.exception))

    def test_garbage_says_what_shape_was_expected(self):
        with self.assertRaises(EventInputError) as ctx:
            events.parse_business_time("下周三晚上")
        message = str(ctx.exception)
        self.assertIn("无法识别的事件时刻", message)
        self.assertIn("年-月-日 时:分", message)
        self.assertIn(business_tz_label(), message)

    def test_an_impossible_date_is_not_a_shape_error(self):
        """`2026-02-30` 形状对、日期不存在——报错要说得出是「不是合法的时刻」。"""
        with self.assertRaises(EventInputError) as ctx:
            events.parse_business_time("2026-02-30 10:00")
        self.assertIn("不是合法的时刻", str(ctx.exception))

    def test_an_empty_time_says_so(self):
        with self.assertRaises(EventInputError) as ctx:
            events.parse_business_time("   ")
        self.assertIn("不能为空", str(ctx.exception))


class TestTheEchoWritesBothClocks(TestCase):
    def test_both_absolute_moments_appear(self):
        """录入端的回显是人核对输入的唯一机会；少掉一边，核对就变成心算。"""
        text = events.format_moment(EVENT_UTC)
        self.assertIn("北京时间 2026-09-25 20:30", text)
        self.assertIn("UTC 2026-09-25 12:30", text)

    def test_a_missing_moment_is_written_as_undecided(self):
        self.assertEqual(events.format_moment(None), "未定")


# --------------------------------------------------------------------------- #
# 窗口
# --------------------------------------------------------------------------- #


class TestWindowArithmetic(TestCase):
    def test_the_defaults_are_before_two_hours_and_after_one(self):
        halt_at, resume_at = events.resolve_window(EVENT_UTC)
        self.assertEqual(halt_at, EVENT_UTC - timedelta(minutes=120))
        self.assertEqual(resume_at, EVENT_UTC + timedelta(minutes=60))

    def test_an_override_inside_the_bounds_is_taken_verbatim(self):
        halt_at, resume_at = events.resolve_window(
            EVENT_UTC, halt_before_minutes=30, resume_after_minutes=15
        )
        self.assertEqual(halt_at, EVENT_UTC - timedelta(minutes=30))
        self.assertEqual(resume_at, EVENT_UTC + timedelta(minutes=15))

    def test_the_bounds_themselves_are_inclusive(self):
        params = config.EVENTS
        for value in (params.window_floor_minutes, params.window_cap_minutes):
            with self.subTest(value=value):
                halt_at, _ = events.resolve_window(EVENT_UTC, halt_before_minutes=value)
                self.assertEqual(halt_at, EVENT_UTC - timedelta(minutes=value))

    def test_an_override_outside_the_bounds_names_both_sides(self):
        """报错要同时写出越界的值与上下限——只说「参数非法」等于让人自己去猜。"""
        for value in (5, 0, 1441, 60 * 72):
            with self.subTest(value=value):
                with self.assertRaises(EventInputError) as ctx:
                    events.resolve_window(EVENT_UTC, halt_before_minutes=value)
                message = str(ctx.exception)
                self.assertIn(str(value), message)
                self.assertIn("[15, 1440]", message)

    def test_the_floor_stops_a_zero_length_window(self):
        """窗口短到管道来不及跑完，停与恢复会在同一分钟内前后发生。"""
        with self.assertRaises(EventInputError):
            events.resolve_window(EVENT_UTC, resume_after_minutes=0)

    def test_a_non_integer_override_is_refused(self):
        for value in (True, "30", 30.5):
            with self.subTest(value=value):
                with self.assertRaises(EventInputError) as ctx:
                    events.resolve_window(EVENT_UTC, halt_before_minutes=value)
                self.assertIn("必须是整数分钟", str(ctx.exception))

    def test_the_two_sides_are_read_separately(self):
        """停止提前量与恢复延后量是两个字段，写错一个不该把另一个也改掉。"""
        halt_at, resume_at = events.resolve_window(EVENT_UTC, halt_before_minutes=15)
        self.assertEqual(halt_at, EVENT_UTC - timedelta(minutes=15))
        self.assertEqual(resume_at, EVENT_UTC + timedelta(minutes=60))


# --------------------------------------------------------------------------- #
# 录入
# --------------------------------------------------------------------------- #


class TestCreateEvent(_WithSymbols):
    def test_the_window_is_computed_and_stored(self):
        event = self.create()
        self.assertEqual(event.event_time, EVENT_UTC)
        self.assertEqual(event.halt_at, EVENT_UTC - timedelta(minutes=120))
        self.assertEqual(event.resume_at, EVENT_UTC + timedelta(minutes=60))
        self.assertEqual(event.status, EventStatus.SCHEDULED.value)
        self.assertTrue(event.triggers_halt)

    def test_a_later_config_change_does_not_move_an_existing_window(self):
        """窗口存成列而不是每次查询现算——同 `DeactivationExemption.expires_at`。

        现算的话，某天有人把默认前 2 小时调成 4 小时，昨天那条事件的窗口就跟着变了，
        而它当时可能已经执行过动作：「那天为什么从 10:00 就停了」从此答不出来。
        """
        event = self.create()
        changed = config.EventsConfig(
            default_halt_before_minutes=240, default_resume_after_minutes=240
        )
        # 新配置确实会给出另一个窗口——两者必须不同，否则本用例什么也没证明
        self.assertNotEqual(
            events.resolve_window(EVENT_UTC, params=changed)[0], event.halt_at
        )
        fresh = MajorEvent.objects.get(pk=event.pk)
        self.assertEqual(fresh.halt_at, EVENT_UTC - timedelta(minutes=120))
        self.assertEqual(fresh.resume_at, EVENT_UTC + timedelta(minutes=60))

    def test_an_override_is_used_but_the_window_still_lands_on_the_row(self):
        event = self.create(halt_before_minutes=15, resume_after_minutes=15)
        self.assertEqual(event.halt_at, EVENT_UTC - timedelta(minutes=15))
        self.assertEqual(event.resume_at, EVENT_UTC + timedelta(minutes=15))

    def test_a_created_row_leaves_a_change_record(self):
        event = self.create(note="盘中议息")
        change = MajorEventChange.objects.get(event=event)
        self.assertEqual(change.kind, EventChangeKind.CREATED.value)
        self.assertEqual(change.actor_kind, HUMAN)
        self.assertEqual(change.actor_name, "xl")
        self.assertEqual(change.before, {})
        self.assertEqual(change.after["event_time"], EVENT_UTC.isoformat())
        self.assertEqual(change.after["status"], EventStatus.SCHEDULED.value)

    def test_the_scope_is_stored_and_readable(self):
        event = self.create(
            scope_kind=EventScope.SYMBOLS.value, symbols=["SOL/USDT", "SOL/USDT"]
        )
        self.assertEqual(event.symbols, ["SOL/USDT"])
        self.assertEqual(event.scope_display, "SOL/USDT")
        self.assertTrue(event.applies_to("SOL/USDT"))
        self.assertFalse(event.applies_to("BTC/USDT"))

    def test_a_market_event_applies_to_everything(self):
        event = self.create()
        self.assertEqual(event.scope_display, "全市场")
        self.assertTrue(event.applies_to("WHATEVER/USDT"))

    def test_a_blank_name_is_refused(self):
        with self.assertRaises(EventInputError) as ctx:
            self.create(name="   ")
        self.assertIn("名称不能为空", str(ctx.exception))
        self.assertFalse(MajorEvent.objects.exists())

    def test_an_unknown_impact_lists_the_three_grades(self):
        with self.assertRaises(EventInputError) as ctx:
            self.create(impact="critical")
        message = str(ctx.exception)
        self.assertIn("未知的冲击档位", message)
        for display in ("high（高）", "medium（中）", "low（低）"):
            self.assertIn(display, message)

    def test_a_naive_event_time_is_refused(self):
        """naive 时刻在这里没有任何可靠的解释方式——口语时刻请先走 parse_business_time。"""
        with self.assertRaises(EventInputError) as ctx:
            self.create(event_time=datetime(2026, 9, 25, 20, 30))
        self.assertIn("必须带时区", str(ctx.exception))

    def test_a_non_datetime_event_time_is_refused(self):
        with self.assertRaises(EventInputError) as ctx:
            self.create(event_time="2026-09-25 20:30")
        self.assertIn("必须是 datetime", str(ctx.exception))

    def test_a_blank_actor_rolls_the_whole_thing_back(self):
        """「谁干的」是流水的一半，空着的话它退化成一条只有时间的日志。"""
        with self.assertRaises(EventInputError) as ctx:
            self.create(actor_name="  ")
        self.assertIn("必须记下触发方", str(ctx.exception))
        self.assertFalse(MajorEvent.objects.exists(), "流水写不出来时那一行不该留下")

    def test_an_unknown_symbol_rolls_the_whole_thing_back(self):
        with self.assertRaises(EventInputError):
            self.create(scope_kind=EventScope.SYMBOLS.value, symbols=["SOL/USDT:USDT"])
        self.assertFalse(MajorEvent.objects.exists())

    def test_the_caller_can_pin_the_clock_used_for_the_record(self):
        """`now` 是给「留痕时刻」用的，与 `event_time` 是两件事。"""
        pinned = datetime(2026, 9, 20, 3, 0, tzinfo=timezone.utc)
        event = self.create(now=pinned)
        self.assertEqual(MajorEventChange.objects.get(event=event).at, pinned)


# --------------------------------------------------------------------------- #
# 「必须人工」
# --------------------------------------------------------------------------- #


class TestOnlyAHumanCanSayHigh(_WithSymbols):
    def test_a_task_may_not_ingest_a_high_event(self):
        with self.assertRaises(EventInputError) as ctx:
            self.create(actor_kind=TASK, actor_name="ingest_task")
        self.assertIn("必须由人确认", str(ctx.exception))
        self.assertFalse(MajorEvent.objects.exists())

    def test_a_task_may_still_ingest_a_medium_event(self):
        """拦的是「高」，不是「task 不许写」——中低档不触发熔断，Agent 的日历同步要用。"""
        event = self.create(
            actor_kind=TASK, actor_name="ingest_task", impact=EventImpact.MEDIUM.value
        )
        self.assertFalse(event.triggers_halt)

    def test_a_task_may_not_promote_an_event_to_high(self):
        event = self.create(impact=EventImpact.MEDIUM.value)
        with self.assertRaises(EventInputError) as ctx:
            events.change_impact(
                event,
                impact=EventImpact.HIGH.value,
                actor_kind=TASK,
                actor_name="audit_task",
            )
        # 报错里的措辞是「必须由人确认」+ 引用 CONTEXT.md 第 149 条，
        # 两处都要落在里面：只说「不能升档」而不说「谁来升」的报错，读的人无从下手。
        message = str(ctx.exception)
        self.assertIn("必须由人确认", message)
        self.assertIn("提升为『高』必须人工", message)
        event.refresh_from_db()
        self.assertEqual(event.impact, EventImpact.MEDIUM.value)

    def test_a_task_may_not_confirm_a_candidate_into_high(self):
        """转正走 `create_event`，所以这条判据在这里**一并生效**，没有第二处实现。"""
        candidate = events.raise_candidate(
            name="某交易所下线公告",
            origin=CandidateOrigin.AGENT.value,
            raised_by="12345",
        )
        with self.assertRaises(EventInputError) as ctx:
            events.confirm_candidate(
                candidate,
                event_time=EVENT_UTC,
                impact=EventImpact.HIGH.value,
                scope_kind=EventScope.MARKET.value,
                actor_kind=TASK,
                actor_name="some_task",
            )
        self.assertIn("必须由人确认", str(ctx.exception))
        candidate.refresh_from_db()
        self.assertEqual(candidate.status, CandidateStatus.PENDING.value)
        self.assertFalse(MajorEvent.objects.exists())

    def test_a_human_may_do_all_three(self):
        self.create()
        event = self.create(impact=EventImpact.LOW.value, name="一条低档")
        events.change_impact(
            event,
            impact=EventImpact.HIGH.value,
            actor_kind=ActorKind.CHAT.value,
            actor_name="u-99",
        )
        candidate = events.raise_candidate(
            name="下周有大事", origin=CandidateOrigin.AGENT.value, raised_by="u-99"
        )
        events.confirm_candidate(
            candidate,
            event_time=EVENT_UTC,
            impact=EventImpact.HIGH.value,
            scope_kind=EventScope.MARKET.value,
            actor_kind=ActorKind.CHAT.value,
            actor_name="u-99",
        )


# --------------------------------------------------------------------------- #
# 改期 / 改档 / 取消
# --------------------------------------------------------------------------- #


class TestReschedule(_WithSymbols):
    def test_the_window_moves_with_the_new_time(self):
        event = self.create()
        moved = events.reschedule_event(
            event,
            event_time=moment("2026-09-26 20:30"),
            actor_kind=HUMAN,
            actor_name="xl",
        )
        self.assertEqual(moved.event_time, moment("2026-09-26 20:30"))
        self.assertEqual(
            moved.halt_at, moment("2026-09-26 20:30") - timedelta(minutes=120)
        )

    def test_the_change_record_keeps_only_what_moved(self):
        """只留被改动的键：整行快照会让读者自己去求差，而那个读者正是这条流水的理由。"""
        event = self.create()
        events.reschedule_event(
            event,
            event_time=moment("2026-09-26 20:30"),
            actor_kind=HUMAN,
            actor_name="xl",
        )
        change = MajorEventChange.objects.get(
            event=event, kind=EventChangeKind.RESCHEDULED.value
        )
        self.assertEqual(set(change.before), {"event_time", "halt_at", "resume_at"})
        self.assertEqual(
            change.before["halt_at"], (EVENT_UTC - timedelta(minutes=120)).isoformat()
        )
        self.assertEqual(
            change.after["halt_at"],
            (moment("2026-09-26 20:30") - timedelta(minutes=120)).isoformat(),
        )

    def test_the_old_window_is_recoverable_from_the_record(self):
        """「那天为什么从 10:00 就停了」要靠这份流水回答。"""
        event = self.create()
        events.reschedule_event(
            event,
            event_time=moment("2026-09-26 20:30"),
            actor_kind=HUMAN,
            actor_name="xl",
        )
        change = MajorEventChange.objects.get(
            event=event, kind=EventChangeKind.RESCHEDULED.value
        )
        self.assertEqual(
            datetime.fromisoformat(change.before["halt_at"]),
            EVENT_UTC - timedelta(minutes=120),
        )

    def test_a_cancelled_event_cannot_be_rescheduled(self):
        event = events.cancel_event(
            self.create(), reason="推迟了", actor_kind=HUMAN, actor_name="xl"
        )
        with self.assertRaises(EventInputError) as ctx:
            events.reschedule_event(
                event,
                event_time=moment("2026-09-26 20:30"),
                actor_kind=HUMAN,
                actor_name="xl",
            )
        self.assertIn("取消是终态", str(ctx.exception))


class TestChangeImpact(_WithSymbols):
    def test_the_grade_moves_and_is_recorded(self):
        event = self.create(impact=EventImpact.MEDIUM.value)
        events.change_impact(
            event,
            impact=EventImpact.HIGH.value,
            actor_kind=HUMAN,
            actor_name="xl",
        )
        change = MajorEventChange.objects.get(
            event=event, kind=EventChangeKind.IMPACT_CHANGED.value
        )
        self.assertEqual(change.before, {"impact": "medium"})
        self.assertEqual(change.after, {"impact": "high"})

    def test_a_downgrade_is_also_a_human_action_and_is_recorded(self):
        """不记降档的话，一次「高 → 中」会在历史里消失，而它正是「这条事件当初为什么
        熔断过」的答案。"""
        event = self.create()
        events.change_impact(
            event,
            impact=EventImpact.LOW.value,
            actor_kind=HUMAN,
            actor_name="xl",
        )
        event.refresh_from_db()
        self.assertFalse(event.triggers_halt)
        self.assertTrue(
            MajorEventChange.objects.filter(
                event=event, kind=EventChangeKind.IMPACT_CHANGED.value
            ).exists()
        )

    def test_setting_the_same_grade_is_refused(self):
        event = self.create(impact=EventImpact.MEDIUM.value)
        with self.assertRaises(EventInputError) as ctx:
            events.change_impact(
                event,
                impact=EventImpact.MEDIUM.value,
                actor_kind=HUMAN,
                actor_name="xl",
            )
        self.assertIn("没有可改的内容", str(ctx.exception))

    def test_a_cancelled_event_cannot_be_regraded(self):
        event = events.cancel_event(
            self.create(), reason="推迟了", actor_kind=HUMAN, actor_name="xl"
        )
        with self.assertRaises(EventInputError):
            events.change_impact(
                event,
                impact=EventImpact.LOW.value,
                actor_kind=HUMAN,
                actor_name="xl",
            )


class TestCancelIsTerminal(_WithSymbols):
    def test_cancelling_keeps_the_row_and_moves_the_status(self):
        """改状态而不是删行：窗口可能已经开启过，那条事件解释过一段真实发生过的停摆。

        删掉的话第②段的熔断记录会指向一条不存在的事件，而那正是审计链断掉的地方。
        """
        event = self.create()
        events.cancel_event(
            event, reason="议息推迟到下月", actor_kind=HUMAN, actor_name="xl"
        )
        event.refresh_from_db()
        self.assertEqual(event.status, EventStatus.CANCELLED.value)
        self.assertFalse(event.triggers_halt)
        self.assertEqual(MajorEvent.objects.count(), 1)

    def test_the_reason_is_mandatory(self):
        event = self.create()
        with self.assertRaises(EventInputError) as ctx:
            events.cancel_event(event, reason="  ", actor_kind=HUMAN, actor_name="xl")
        self.assertIn("必须给出理由", str(ctx.exception))
        event.refresh_from_db()
        self.assertEqual(event.status, EventStatus.SCHEDULED.value)

    def test_the_reason_lands_on_the_change_record(self):
        event = self.create()
        events.cancel_event(
            event,
            reason="议息推迟到下月",
            actor_kind=HUMAN,
            actor_name="xl",
        )
        change = MajorEventChange.objects.get(
            event=event, kind=EventChangeKind.CANCELLED.value
        )
        self.assertEqual(change.note, "议息推迟到下月")
        self.assertEqual(change.after["status"], EventStatus.CANCELLED.value)

    def test_cancelling_twice_is_refused(self):
        event = events.cancel_event(
            self.create(), reason="推迟了", actor_kind=HUMAN, actor_name="xl"
        )
        with self.assertRaises(EventInputError) as ctx:
            events.cancel_event(
                event, reason="再取消一次", actor_kind=HUMAN, actor_name="xl"
            )
        self.assertIn("取消是终态", str(ctx.exception))


# --------------------------------------------------------------------------- #
# 候选事件
# --------------------------------------------------------------------------- #


class TestRaisingACandidate(_WithSymbols):
    def test_a_candidate_has_no_window_at_all(self):
        """候选表里连 `halt_at` 都没有一列——这是它与重大事件的分界线。"""
        candidate = events.raise_candidate(
            name="下周某交易所下线某币",
            origin=CandidateOrigin.NEWS.value,
            raised_by="某加密媒体",
            news_item=self._news(),
            guessed_time=moment("2026-10-01 09:00"),
        )
        self.assertFalse(hasattr(candidate, "halt_at"))
        self.assertEqual(candidate.status, CandidateStatus.PENDING.value)
        self.assertTrue(candidate.is_pending)

    def test_the_proposal_date_is_the_business_day(self):
        """业务日的边界与日线换线是同一个绝对时刻（北京 08:00）。

        用本地自然日会让同一条候选在跨日那几小时里归属成两天。
        """
        candidate = events.raise_candidate(
            name="x",
            origin=CandidateOrigin.AGENT.value,
            raised_by="u-1",
            now=datetime(2026, 9, 25, 1, 0, tzinfo=timezone.utc),  # 北京 09:00
        )
        self.assertEqual(candidate.raised_at, date(2026, 9, 25))

    def test_the_expiry_is_computed_at_write_time(self):
        """改配置不得追溯延长或缩短一条已经提出的候选。"""
        now = datetime(2026, 9, 25, 1, 0, tzinfo=timezone.utc)
        candidate = events.raise_candidate(
            name="x", origin=CandidateOrigin.AGENT.value, raised_by="u-1", now=now
        )
        self.assertEqual(
            candidate.expires_at,
            now + timedelta(days=config.EVENTS.candidate_expiry_days),
        )

    def test_the_guessed_time_may_be_missing(self):
        """资讯里常常只说「下周」，为它编一个钟点等于给未知量填一个像事实的数。"""
        candidate = events.raise_candidate(
            name="月末有大事", origin=CandidateOrigin.AGENT.value, raised_by="u-1"
        )
        self.assertIsNone(candidate.guessed_time)

    def test_a_guessed_time_without_a_zone_is_refused(self):
        with self.assertRaises(EventInputError) as ctx:
            events.raise_candidate(
                name="x",
                origin=CandidateOrigin.AGENT.value,
                raised_by="u-1",
                guessed_time=datetime(2026, 10, 1, 9, 0),
            )
        self.assertIn("必须带时区", str(ctx.exception))

    def test_a_news_candidate_must_carry_its_source_item(self):
        """「某条资讯说下周有大事」在几天后唯一可核对的依据就是那一条原文。"""
        with self.assertRaises(EventInputError) as ctx:
            events.raise_candidate(
                name="x", origin=CandidateOrigin.NEWS.value, raised_by="某源"
            )
        self.assertIn("必须带上来源资讯条目", str(ctx.exception))

    def test_an_agent_candidate_needs_no_news_item(self):
        candidate = events.raise_candidate(
            name="x", origin=CandidateOrigin.AGENT.value, raised_by="u-1"
        )
        self.assertIsNone(candidate.news_item_id)

    def test_an_unknown_origin_lists_both(self):
        with self.assertRaises(EventInputError) as ctx:
            events.raise_candidate(name="x", origin="llm", raised_by="u-1")
        message = str(ctx.exception)
        self.assertIn("未知的提出方", message)
        self.assertIn("news（资讯判定）", message)
        self.assertIn("agent（Agent 建议）", message)

    def test_a_blank_name_or_proposer_is_refused(self):
        for over in ({"name": "  "}, {"raised_by": "  "}):
            with self.subTest(over=over):
                kwargs = {
                    "name": "x",
                    "origin": CandidateOrigin.AGENT.value,
                    "raised_by": "u-1",
                }
                kwargs.update(over)
                with self.assertRaises(EventInputError):
                    events.raise_candidate(**kwargs)

    @staticmethod
    def _news() -> NewsItem:
        return NewsItem.objects.create(
            source="某加密媒体",
            kind=config.NewsSourceKind.CRYPTO_MEDIA.value,
            url="https://example.com/a",
            title="某交易所公告",
            body="正文",
            fetched_at=datetime(2026, 9, 25, 1, 0, tzinfo=timezone.utc),
        )


class TestConfirmingACandidate(_WithSymbols):
    def _candidate(self, **over) -> CandidateEvent:
        kwargs = {
            "name": "某交易所下线某币",
            "origin": CandidateOrigin.AGENT.value,
            "raised_by": "u-1",
            "guessed_time": moment("2026-10-01 09:00"),
        }
        kwargs.update(over)
        return events.raise_candidate(**kwargs)

    def test_the_event_is_built_from_what_the_human_said(self):
        """**时间、档位、作用域必须由人重新说一遍**，不从候选上继承任何一项。

        继承的话，一条「某条资讯说下周有大事」会带着一个编出来的钟点直接变成熔断
        窗口——而窗口是会自动停掉全场策略的东西。
        """
        candidate = self._candidate()
        event = events.confirm_candidate(
            candidate,
            event_time=moment("2026-10-02 03:00"),
            impact=EventImpact.MEDIUM.value,
            scope_kind=EventScope.SYMBOLS.value,
            symbols=["BTC/USDT"],
            actor_kind=HUMAN,
            actor_name="xl",
        )
        self.assertEqual(event.event_time, moment("2026-10-02 03:00"))
        self.assertNotEqual(event.event_time, candidate.guessed_time)
        self.assertEqual(event.impact, EventImpact.MEDIUM.value)
        self.assertEqual(event.scope_kind, EventScope.SYMBOLS.value)
        self.assertEqual(event.symbols, ["BTC/USDT"])

    def test_the_origin_is_written_into_the_events_note(self):
        """转正之后「这条是谁提的」必须还答得出来——候选表那边有外键，事件这边只有
        这一句。"""
        candidate = self._candidate()
        event = events.confirm_candidate(
            candidate,
            event_time=EVENT_UTC,
            impact=EventImpact.HIGH.value,
            scope_kind=EventScope.MARKET.value,
            actor_kind=HUMAN,
            actor_name="xl",
            note="已与交易所公告核对",
        )
        self.assertIn(f"候选 #{candidate.id} 转正", event.note)
        self.assertIn("u-1", event.note)
        self.assertIn("已与交易所公告核对", event.note)

    def test_the_candidate_is_closed_and_points_at_the_event(self):
        candidate = self._candidate()
        event = events.confirm_candidate(
            candidate,
            event_time=EVENT_UTC,
            impact=EventImpact.HIGH.value,
            scope_kind=EventScope.MARKET.value,
            actor_kind=HUMAN,
            actor_name="xl",
        )
        candidate.refresh_from_db()
        self.assertEqual(candidate.status, CandidateStatus.CONFIRMED.value)
        self.assertEqual(candidate.confirmed_event_id, event.pk)
        self.assertEqual(candidate.decided_by, "xl")
        self.assertIsNotNone(candidate.decided_at)
        self.assertFalse(candidate.is_pending)

    def test_a_decision_cannot_be_taken_back(self):
        candidate = self._candidate()
        events.confirm_candidate(
            candidate,
            event_time=EVENT_UTC,
            impact=EventImpact.HIGH.value,
            scope_kind=EventScope.MARKET.value,
            actor_kind=HUMAN,
            actor_name="xl",
        )
        with self.assertRaises(EventInputError) as ctx:
            events.confirm_candidate(
                candidate,
                event_time=EVENT_UTC,
                impact=EventImpact.LOW.value,
                scope_kind=EventScope.MARKET.value,
                actor_kind=HUMAN,
                actor_name="xl",
            )
        self.assertIn("终态不可回退", str(ctx.exception))

    def test_a_rejected_candidate_cannot_be_confirmed_later(self):
        candidate = self._candidate()
        events.discard_candidate(
            candidate, reason=CANDIDATE_DISCARD_REJECTED, actor_name="xl"
        )
        with self.assertRaises(EventInputError):
            events.confirm_candidate(
                candidate,
                event_time=EVENT_UTC,
                impact=EventImpact.HIGH.value,
                scope_kind=EventScope.MARKET.value,
                actor_kind=HUMAN,
                actor_name="xl",
            )

    def test_a_failed_confirmation_leaves_no_event_behind(self):
        """转正在一个事务里：半条转正（事件建了、候选还挂着）是最坏的一种。"""
        candidate = self._candidate()
        with self.assertRaises(EventInputError):
            events.confirm_candidate(
                candidate,
                event_time=EVENT_UTC,
                impact=EventImpact.HIGH.value,
                scope_kind=EventScope.SYMBOLS.value,
                symbols=["NOPE/USDT"],
                actor_kind=HUMAN,
                actor_name="xl",
            )
        self.assertFalse(MajorEvent.objects.exists())
        candidate.refresh_from_db()
        self.assertEqual(candidate.status, CandidateStatus.PENDING.value)


class TestDiscardingACandidate(_WithSymbols):
    def _candidate(self) -> CandidateEvent:
        return events.raise_candidate(
            name="x", origin=CandidateOrigin.AGENT.value, raised_by="u-1"
        )

    def test_a_human_rejection_records_who_looked(self):
        candidate = self._candidate()
        events.discard_candidate(
            candidate, reason=CANDIDATE_DISCARD_REJECTED, actor_name="xl"
        )
        candidate.refresh_from_db()
        self.assertEqual(candidate.status, CandidateStatus.DISCARDED.value)
        self.assertEqual(candidate.decided_by, "xl")
        self.assertEqual(candidate.discard_reason, CANDIDATE_DISCARD_REJECTED)

    def test_a_human_cannot_write_the_expiry_reason(self):
        """人工走「到期未确认」会把「没人看」与「看过了」混成一条。

        日报第④段要靠这个差别读覆盖率衰减，分不清的话那条提醒就失去意义。
        """
        candidate = self._candidate()
        with self.assertRaises(EventInputError) as ctx:
            events.discard_candidate(
                candidate, reason=CANDIDATE_DISCARD_EXPIRED, actor_name="xl"
            )
        self.assertIn("由定时清理自动落", str(ctx.exception))
        candidate.refresh_from_db()
        self.assertEqual(candidate.status, CandidateStatus.PENDING.value)

    def test_an_unknown_reason_lists_both_codes(self):
        with self.assertRaises(EventInputError) as ctx:
            events.discard_candidate(
                self._candidate(), reason="whatever", actor_name="xl"
            )
        message = str(ctx.exception)
        self.assertIn(CANDIDATE_DISCARD_EXPIRED, message)
        self.assertIn(CANDIDATE_DISCARD_REJECTED, message)

    def test_discarding_twice_is_refused(self):
        candidate = self._candidate()
        events.discard_candidate(
            candidate, reason=CANDIDATE_DISCARD_REJECTED, actor_name="xl"
        )
        with self.assertRaises(EventInputError) as ctx:
            events.discard_candidate(
                candidate, reason=CANDIDATE_DISCARD_REJECTED, actor_name="xl"
            )
        self.assertIn("终态不可回退", str(ctx.exception))


class TestCandidatesExpireWithoutAnyoneLooking(_WithSymbols):
    def _candidate(self, *, days_ago: int) -> CandidateEvent:
        now = datetime(2026, 9, 25, 1, 0, tzinfo=timezone.utc)
        return events.raise_candidate(
            name=f"候选 {days_ago} 天前提的",
            origin=CandidateOrigin.AGENT.value,
            raised_by="u-1",
            now=now - timedelta(days=days_ago),
        )

    def test_only_the_ones_past_their_expiry_are_touched(self):
        stale = self._candidate(days_ago=20)
        fresh = self._candidate(days_ago=1)
        moved = events.expire_candidates(
            now=datetime(2026, 9, 25, 1, 0, tzinfo=timezone.utc)
        )
        self.assertEqual(moved, 1)
        stale.refresh_from_db()
        fresh.refresh_from_db()
        self.assertEqual(stale.status, CandidateStatus.DISCARDED.value)
        self.assertEqual(fresh.status, CandidateStatus.PENDING.value)

    def test_nothing_is_deleted(self):
        """「这条建议提过、没人理它」正是覆盖率衰减的证据，删掉就等于把它抹了。"""
        candidate = self._candidate(days_ago=20)
        events.expire_candidates(now=datetime(2026, 9, 25, 1, 0, tzinfo=timezone.utc))
        self.assertTrue(CandidateEvent.objects.filter(pk=candidate.pk).exists())

    def test_the_decider_is_left_blank_on_purpose(self):
        """`discarded` + 空处置人 + `expired` 三个一起，才读得出「机制提过、没人理」。"""
        candidate = self._candidate(days_ago=20)
        events.expire_candidates(now=datetime(2026, 9, 25, 1, 0, tzinfo=timezone.utc))
        candidate.refresh_from_db()
        self.assertEqual(candidate.decided_by, "")
        self.assertEqual(candidate.discard_reason, CANDIDATE_DISCARD_EXPIRED)
        self.assertIsNotNone(candidate.decided_at)

    def test_a_promotion_wins_the_race_against_the_cleanup(self):
        """两次读之间可能有人刚把它转正了——转正是终态，不能被一次到期的清理顺手改回去。"""
        candidate = self._candidate(days_ago=20)
        events.confirm_candidate(
            candidate,
            event_time=EVENT_UTC,
            impact=EventImpact.MEDIUM.value,
            scope_kind=EventScope.MARKET.value,
            actor_kind=HUMAN,
            actor_name="xl",
        )
        moved = events.expire_candidates(
            now=datetime(2026, 9, 25, 1, 0, tzinfo=timezone.utc)
        )
        self.assertEqual(moved, 0)
        candidate.refresh_from_db()
        self.assertEqual(candidate.status, CandidateStatus.CONFIRMED.value)

    def test_a_clean_run_says_zero_and_writes_nothing(self):
        self.assertEqual(events.expire_candidates(), 0)

    def test_expiry_logs_one_line_per_row(self):
        self._candidate(days_ago=20)
        self._candidate(days_ago=21)
        with self.assertLogs("apps.regime.events", level="INFO") as logs:
            events.expire_candidates(
                now=datetime(2026, 9, 25, 1, 0, tzinfo=timezone.utc)
            )
        self.assertEqual(sum("已丢弃" in line for line in logs.output), 2)

    def test_an_expired_candidate_says_it_is_waiting_for_the_cleanup(self):
        """到期与「已丢弃」之间的那个窗口也要读得出来。

        清理每 5 分钟一轮，所以正常时这个窗口几乎见不到；见得到它，就等于看到**清理任务
        自己没在跑**——这句话必须印出来，否则这一行会被读成「明天就好了」。
        """
        candidate = self._candidate(days_ago=20)
        text = events.describe_candidate(
            candidate, now=datetime(2026, 9, 25, 1, 0, tzinfo=timezone.utc)
        )
        self.assertIn("已过失效期", text)
        self.assertIn("等下一次定时清理", text)
        self.assertIn("说明清理任务自己没在跑", text)


class TestTheCleanupIsAScheduledTask(_StubbedConnectionReset, _WithSymbols):
    """清理此前**一个调用方都没有**：实现、测试、注释全在，就是没人调它。

    `events.expire_candidates` 的行为在上面那个类里钉着，这里只钉另外三件不重复的事：
    任务**真的挂上了 beat**（且是可判定固定间隔的条目）、任务体真的把行翻过去了、
    以及**任务不吞异常**（CONTEXT.md 第 180 条：吞掉会让一次真故障连 FAILURE 那条记录
    都没有）。

    要 `_StubbedConnectionReset` 是因为任务体的 `finally` 里有 `close_old_connections()`
    ——它在 `TestCase` 的事务里必然把连接关掉，理由见那个类的 docstring。
    """

    def _stale(self, *, days_ago: int = 20) -> CandidateEvent:
        """`now` 用真实时钟：任务自己取当前时刻，注入不了。"""
        return events.raise_candidate(
            name=f"候选 {days_ago} 天前提的",
            origin=CandidateOrigin.AGENT.value,
            raised_by="u-1",
            now=datetime.now(timezone.utc) - timedelta(days=days_ago),
        )

    def test_the_beat_entry_points_at_this_task_and_has_a_fixed_interval(self):
        """固定间隔而不是 crontab：体检页读不出 crontab 的「下一次该在什么时候」，
        于是它会落进「不定罪」那一档——「清理没在跑」这件事就没人说得出了。"""
        from celery_app import app

        entry = app.conf.beat_schedule["regime-candidate-expiry"]
        self.assertEqual(entry["task"], "apps.regime.tasks.expire_candidates")
        self.assertIsInstance(entry["schedule"], (int, float))

    def test_the_task_reports_how_many_rows_it_moved(self):
        from apps.regime import tasks

        self._stale()
        self._stale(days_ago=21)
        self.assertEqual(tasks.expire_candidates.run(), {"expired": 2})
        self.assertEqual(
            CandidateEvent.objects.filter(
                status=CandidateStatus.DISCARDED.value
            ).count(),
            2,
        )

    def test_a_clean_round_says_zero(self):
        from apps.regime import tasks

        self._stale(days_ago=1)  # 还在失效期内
        self.assertEqual(tasks.expire_candidates.run(), {"expired": 0})

    def test_a_failure_is_not_swallowed(self):
        from unittest.mock import patch

        from apps.regime import tasks

        with patch.object(
            events, "expire_candidates", side_effect=RuntimeError("boom")
        ):
            with self.assertRaises(RuntimeError):
                tasks.expire_candidates.run()


# --------------------------------------------------------------------------- #
# 回显
# --------------------------------------------------------------------------- #


class TestTheEchoDistinguishesTheTwoTables(_WithSymbols):
    def test_an_event_always_prints_its_window(self):
        text = events.describe_event(self.create())
        self.assertIn("熔断窗口：", text)
        self.assertIn("北京时间 2026-09-25 18:30", text)
        self.assertIn("UTC 2026-09-25 10:30", text)

    def test_a_candidate_never_prints_a_window(self):
        """读的人不该靠自己去记住哪张表带窗口。

        判据是「熔断窗口：」这一行（带冒号、带两个时刻）而不是「熔断窗口」四个字——
        候选那一行刻意写了「无熔断窗口」来把差别说出来，那是它的反面而不是它的重复。
        """
        candidate = events.raise_candidate(
            name="下周有大事", origin=CandidateOrigin.AGENT.value, raised_by="u-1"
        )
        text = events.describe_candidate(candidate)
        self.assertNotIn("熔断窗口：", text)
        self.assertIn("无熔断窗口", text)

    def test_an_event_that_already_happened_says_so(self):
        """「录了一条已经过去的事件」是人工维护最可能的空转形状——不说的表现是
        「录进去了但什么都没发生」，它与「今天没有事件」长得一样。"""
        text = events.describe_event(self.create(), now=EVENT_UTC + timedelta(days=1))
        self.assertIn("该窗口已整体过去", text)

    def test_an_event_inside_its_window_says_so(self):
        text = events.describe_event(
            self.create(), now=EVENT_UTC - timedelta(minutes=30)
        )
        self.assertIn("该窗口正在进行中", text)

    def test_a_non_high_event_says_it_will_not_halt(self):
        text = events.describe_event(
            self.create(impact=EventImpact.MEDIUM.value), now=EVENT_UTC
        )
        self.assertIn("只有「高」会触发熔断", text)

    def test_a_cancelled_event_says_it_will_not_halt(self):
        event = events.cancel_event(
            self.create(), reason="推迟了", actor_kind=HUMAN, actor_name="xl"
        )
        text = events.describe_event(event)
        self.assertIn("已取消：本条不产生任何熔断", text)

    def test_an_undecided_guessed_time_is_written_out(self):
        candidate = events.raise_candidate(
            name="月末有大事", origin=CandidateOrigin.AGENT.value, raised_by="u-1"
        )
        self.assertIn("未定", events.describe_candidate(candidate))

    def test_a_discarded_candidate_shows_who_disposed_of_it(self):
        candidate = events.raise_candidate(
            name="x", origin=CandidateOrigin.AGENT.value, raised_by="u-1"
        )
        events.discard_candidate(
            candidate, reason=CANDIDATE_DISCARD_REJECTED, actor_name="xl"
        )
        text = events.describe_candidate(candidate)
        self.assertIn("人工否决", text)
        self.assertIn("xl", text)
