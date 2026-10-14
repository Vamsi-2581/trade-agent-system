"""重大事件的**维护命令**（第①段单元 8ii）。

`test_events.py` 钉的是「一条事件敲进来之后发生了什么」（`events.py` 那一层）；这个
文件钉的是**命令词被敲下来的那一刻**：谁被认成命令、参数有没有被丢掉、报错长什么样。
两层分开，因为坏掉的方式不同——`events.py` 坏掉会录进一条错的事件，而这一层坏掉的
表现多半是**什么都没发生**：

1. **命令词之后的那一段不许被丢掉**（CONTEXT.md 第 119 条点名的那条通路）。原来的
   分发只取 `split()[0]`，`/event add …` 会被当成一个不带参数的 `/event` 执行，看起
   来就像命令不生效。这里从分发器的角度钉住「参数原样到达 handler」。
2. **中文别名只做整串精确匹配**。别名匹配发生在意图解析**之前**，所以「把刚才那个
   取消了吧」这种句子一旦被前缀匹配劫持，用户看到的是一次莫名其妙的取消。
3. **落款是人**：命令路径写进流水的是 `chat` + 平台 sender id。这不是装饰——
   `events.py` 里「提升为『高』必须人工」的判据认的正是 `actor_kind`。
4. **打字打错是普通回复，不是故障**：走 `success=True`。消费端把 `success=False`
   渲染成 `[错误] …`（`apps/agent/consumer.py`），手滑与系统坏掉在用户眼里会长得
   一样。只有「认不出你是谁」才留着当故障。
5. **多给的词一律报错**。「我明明写了却什么都没发生」在事件维护里最坏：一条以为录进去
   了、实际不存在的熔断窗口，要等到那天什么都没发生才会被发现，而那天看起来与「今天
   没有事件」一模一样。
6. **窗口覆盖只挂在拥有窗口的命令上**：改档路径上的 `--停前` 必须是报错，而不是一次
   `TypeError`（那会被当成故障上报，而人看到的只是自己多打了一个词）。
7. **品种口径就是下单口径**：库里三张表造**真的**行，`SOL/USDT:USDT` 与未知写法在
   录入端被拒，报错里给出候选。
8. **两个回显在命令输出里仍然可分辨**：`/event list` 里的事件一定有「熔断窗口：」，
   候选一定没有。
9. **写入面是一份声明出来的清单**：`_SUBCOMMANDS` 的键集合本身也是契约——多出一个
   键，就等于多了一条没人审过的写入路径。
10. **收参数的命令也是一份声明出来的清单**（第②段 ②f 加 `/regime` 时补的）：
    `COMMANDS_WITH_ARGS` 用**精确相等**断言，因为漏登记的表现是「命令看起来没生效」
    ——`/regime on` 被当成裸 `/regime` 执行，屏幕上回来的确认页与「开关本来就是关的」
    长得一模一样。

词法那一层不起数据库（纯粹是 pytest 类）；DB 用例走 `test_cancel_task_audit.py` 的
同一条约定：**普通类 + `django_db(transaction=True)`**。async 入口里的 ORM 是
`sync_to_async` 挪到别的线程里跑的，那个连接必须看得见本用例造的行——而
`django.test.TestCase` 的外层事务恰好挡住这件事，所以这里不能用它。
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from django.contrib.auth import get_user_model

from apps.agent import event_commands
from apps.agent.base import AgentMessage, AgentResult
from apps.agent.event_commands import (
    handle_event_command,
    _parse_impact,
    _parse_scope,
    _split_flags,
    _take_time,
    _unquote,
)
from apps.agent.supervisor import (
    COMMAND_ALIASES,
    COMMANDS_WITH_ARGS,
    SLASH_COMMANDS,
    SupervisorAgent,
    _split_command,
)
from apps.regime import events
from apps.regime.events import EventInputError
from apps.regime.models import (
    CANDIDATE_DISCARD_REJECTED,
    ActorKind,
    CandidateEvent,
    CandidateOrigin,
    CandidateStatus,
    EventImpact,
    EventScope,
    EventStatus,
    MajorEvent,
    MajorEventChange,
)

#: 事件本身的名义时刻：北京时间 2026-09-25 20:30 == UTC 2026-09-25 12:30。
EVENT_TEXT = "2026-09-25 20:30"
EVENT_UTC = datetime(2026, 9, 25, 12, 30, tzinfo=timezone.utc)

SENDER = "tg:42"


def _run(coro):
    return asyncio.run(coro)


def _msg(text: str, user_id: str = SENDER) -> AgentMessage:
    return AgentMessage(
        sender="channel",
        recipient="supervisor",
        user_id=user_id,
        payload={"text": text},
    )


async def _call(text: str, user_id: str = SENDER) -> AgentResult:
    """按生产路径调用：命令词切下来，剩下那一段交给 `handle_event_command`。"""
    cmd, args = _split_command(text)
    assert cmd == "/event", text
    return await handle_event_command(_msg(text, user_id), args)


# --------------------------------------------------------------------------- #
# 词法：不起数据库
# --------------------------------------------------------------------------- #


class TestTheCommandWordKeepsItsArguments:
    def test_the_rest_of_the_line_is_handed_over_intact(self):
        cmd, args = _split_command("/event add 高 全市场 2026-09-25 20:30 FOMC 议息")
        assert cmd == "/event"
        assert args == "add 高 全市场 2026-09-25 20:30 FOMC 议息"

    def test_only_one_cut_is_made(self):
        # 只切一刀：内部的多余空格留给命令自己去读（事件名称就是靠这个把词接回去的）。
        _, args = _split_command("/event  add   高   全市场 2026-09-25 20:30 X")
        assert args == "add   高   全市场 2026-09-25 20:30 X"

    def test_the_command_word_is_case_insensitive(self):
        assert _split_command("/Event add 高")[0] == "/event"

    def test_a_bare_command_has_empty_args(self):
        assert _split_command("/event") == ("/event", "")
        assert _split_command("/event   ") == ("/event", "")

    def test_a_tab_separator_still_splits(self):
        assert _split_command("/event\tadd 高") == ("/event", "add 高")


class TestOnlyWholeTextAliasesAreCommands:
    """别名匹配发生在意图解析之前，所以「看起来像命令」的代价比「没认出命令」高得多。"""

    SENTENCES = ("把刚才那个取消了吧", "取消一下", "新建会话记录一下", "不用取消")

    def test_a_sentence_containing_an_alias_stays_a_sentence(self):
        for sentence in self.SENTENCES:
            normalized = COMMAND_ALIASES.get(sentence, sentence)
            assert not normalized.startswith("/"), sentence

    def test_the_aliases_themselves_still_work(self):
        assert COMMAND_ALIASES["取消"] == "/cancel"


class TestTheLexer:
    def test_flags_are_lifted_out_of_the_positional_arguments(self):
        positional, flags = _split_flags(
            ["高", "全市场", "2026-09-25", "20:30", "FOMC", "议息", "--停前", "30"]
        )
        assert positional == ["高", "全市场", "2026-09-25", "20:30", "FOMC", "议息"]
        assert flags == {"halt_before_minutes": 30}

    def test_the_note_swallows_everything_after_it(self):
        positional, flags = _split_flags(["全市场", "--备注", "注意", "有", "空格"])
        assert positional == ["全市场"]
        assert flags == {"note": "注意 有 空格"}

    def test_a_span_flag_without_a_value_is_an_input_error(self):
        with pytest.raises(EventInputError):
            _split_flags(["高", "--停前"])

    def test_a_non_integer_span_is_an_input_error(self):
        with pytest.raises(EventInputError):
            _split_flags(["高", "--停前", "两小时"])

    def test_impact_accepts_both_the_chinese_word_and_the_value(self):
        assert _parse_impact("高") == EventImpact.HIGH.value
        assert _parse_impact("high") == EventImpact.HIGH.value
        assert _parse_impact("低") == EventImpact.LOW.value
        with pytest.raises(EventInputError):
            _parse_impact("很高")

    def test_scope_market_has_no_symbols(self):
        assert _parse_scope("全市场") == (EventScope.MARKET.value, [])

    def test_scope_symbols_split_on_every_comma_flavour(self):
        for token in ("SOL/USDT,BTC/USDT", "SOL/USDT，BTC/USDT", "SOL/USDT、BTC/USDT"):
            assert _parse_scope(token) == (
                EventScope.SYMBOLS.value,
                ["SOL/USDT", "BTC/USDT"],
            ), token

    def test_naming_the_symbols_scope_without_symbols_is_refused(self):
        # 留空让它退化成「全市场」是最贵的一种猜错（CONTEXT.md 第 147 条）。
        with pytest.raises(EventInputError):
            _parse_scope(EventScope.SYMBOLS.display)

    def test_a_time_may_span_two_tokens(self):
        assert _take_time(["2026-09-25", "20:30", "FOMC"], 0) == (EVENT_UTC, 2)

    def test_a_time_may_be_one_token(self):
        assert _take_time(["2026-09-25 20:30", "FOMC"], 0) == (EVENT_UTC, 1)

    def test_the_clock_is_beijing_not_utc(self):
        # 同一个字面量按 UTC 读会得到 20:30Z；这里必须是 12:30Z。
        assert _take_time(["2026-09-25 20:30"], 0)[0].hour == 12

    def test_quotes_are_stripped_only_in_pairs(self):
        assert _unquote('"FOMC 议息"') == "FOMC 议息"
        assert _unquote("'FOMC'") == "FOMC"
        assert _unquote("“FOMC”") == "FOMC"
        # 词中间的撇号不是引号。
        assert _unquote("don't") == "don't"
        # 只开不闭（或只闭不开）不剥。
        assert _unquote('"FOMC') == '"FOMC'


class TestTheWriteSurfaceIsADeclaredList:
    def test_the_seven_subcommands_are_exactly_these(self):
        # 多出一个键 = 多一条没人审过的写入路径。提出方（`raise_candidate`）与定时清理
        # （`expire_candidates`）刻意不在这里：前者的出口是候选表，后者挂在 beat 上。
        assert set(event_commands._SUBCOMMANDS) == {
            "add",
            "impact",
            "reschedule",
            "cancel",
            "confirm",
            "reject",
            "list",
        }


# --------------------------------------------------------------------------- #
# 分发器：不起数据库，handler 全部换成 AsyncMock
# --------------------------------------------------------------------------- #


class TestTheDispatcher:
    def _fake_agent(self):
        sentinel = AgentResult(task_id="t", success=True, data="ok")
        return SimpleNamespace(
            _handle_new_session=AsyncMock(return_value=sentinel),
            _handle_cancel=AsyncMock(return_value=sentinel),
            _handle_event=AsyncMock(return_value=sentinel),
            _handle_regime=AsyncMock(return_value=sentinel),
            _normal_route=AsyncMock(return_value=sentinel),
            _sentinel=sentinel,
        )

    def test_arguments_reach_the_handler(self):
        fake = self._fake_agent()
        _run(
            SupervisorAgent.handle(
                fake, _msg("/event add 高 全市场 2026-09-25 20:30 FOMC")
            )
        )
        fake._handle_event.assert_awaited_once()
        assert (
            fake._handle_event.await_args.args[1]
            == "add 高 全市场 2026-09-25 20:30 FOMC"
        )

    def test_the_argument_free_commands_are_still_called_with_one_argument(self):
        # 无参数命令只收一个参数——`message` 本身，不多给一个 `args` 占位。这里钉的是
        # 位置个数与载荷：`AgentMessage.task_id` 是 uuid4 工厂，两次构造的实例永远不
        # 相等，拿整个对象去比等于在比两个随机号。
        fake = self._fake_agent()
        _run(SupervisorAgent.handle(fake, _msg("/new")))
        fake._handle_new_session.assert_awaited_once()
        assert len(fake._handle_new_session.await_args.args) == 1
        assert fake._handle_new_session.await_args.args[0].payload == {"text": "/new"}

    def test_an_unknown_command_is_feedback_not_a_failure(self):
        fake = self._fake_agent()
        result = _run(SupervisorAgent.handle(fake, _msg("/halt")))
        assert result.success, result.error
        assert "/halt" in result.data
        # 提示里必须带上现在真正存在的命令，否则人只知道敲错了、不知道敲什么对。
        assert "/event" in result.data
        assert "/regime" in result.data
        fake._handle_event.assert_not_awaited()

    def test_a_sentence_is_never_taken_for_a_command(self):
        mgr = SimpleNamespace(get_session_context=AsyncMock(return_value=None))
        with patch("apps.agent.supervisor.get_session_manager", return_value=mgr):
            fake = self._fake_agent()
            result = _run(SupervisorAgent.handle(fake, _msg("把刚才那个取消了吧")))
        assert result is fake._sentinel
        fake._handle_cancel.assert_not_awaited()
        fake._handle_event.assert_not_awaited()
        fake._handle_regime.assert_not_awaited()

    def test_the_event_command_is_registered_and_takes_arguments(self):
        assert SLASH_COMMANDS["/event"] == "_handle_event"
        # 整份清单的**精确相等**（不是「包含」）：`COMMANDS_WITH_ARGS` 决定哪条命令拿得到
        # 命令词之后的那一段，多列一条就是给一条命令开了参数口。加命令时这条必须手动改，
        # 那正是它的用处。
        assert COMMANDS_WITH_ARGS == {"/event", "/regime"}
        assert callable(SupervisorAgent._handle_event)

    def test_the_regime_command_is_registered_and_receives_its_arguments(self):
        # `/regime` 裸着是确认页、`on`/`off` 才切换，两种情况都由它自己解析，所以它必须
        # 拿得到参数段。上面那条断的是「清单里有它」，这条断的是「它真的接得到 args」——
        # 少登记在 `COMMANDS_WITH_ARGS` 里的话，`/regime on` 会被当成不带参数的 `/regime`
        # 执行：人敲了「打开」，屏幕上回来的却是确认页，而且看起来就像开关没生效。
        assert SLASH_COMMANDS["/regime"] == "_handle_regime"
        assert callable(SupervisorAgent._handle_regime)

        fake = self._fake_agent()
        _run(SupervisorAgent.handle(fake, _msg("/regime on")))
        fake._handle_regime.assert_awaited_once()
        assert fake._handle_regime.await_args.args[1] == "on"


# --------------------------------------------------------------------------- #
# 命令本身：真库，走完整路径（`sync_to_async` → 线程里跑 ORM）
# --------------------------------------------------------------------------- #


@pytest.fixture
def symbols():
    """库里三种品种来源各造一行。

    `known_symbols()` 的全部意义就是「从这三张表里读」（CONTEXT.md 第 147 条提到的
    那张品种目录不存在），所以这里造**真的**行——拿一个假的 `known=` 去测它，等于把
    这条性质测没了。
    """
    from apps.backtest.models import BacktestResult
    from apps.exchange.models import ExchangeAccount
    from apps.trading.models import LiveSession, Order, Strategy

    # `AUTH_USER_MODEL = "authentication.User"`：email 是必填的第一个位置参数。
    user = get_user_model().objects.create_user(
        email="commands@test.local", username="ops", password="pw12345"
    )
    strategy = Strategy.objects.create(
        name="AlphaStem", code_path="/t/alpha.py", git_commit_hash="aaaaaaa"
    )
    BacktestResult.objects.create(
        strategy=strategy,
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
        user=user,
        strategy=strategy,
        symbol="ETH/USDT",
        mode="paper",
        status="running",
        initial_capital=Decimal("10000.00"),
    )
    account = ExchangeAccount.objects.create(
        exchange="binance", label="t", api_key_enc=b"x", api_secret_enc=b"y"
    )
    Order.objects.create(
        user=user,
        exchange_account=account,
        symbol="SOL/USDT",
        side="buy",
        order_type="market",
        quantity=Decimal("1"),
    )
    return user


def _ingest(text: str = EVENT_TEXT, impact: str = "中") -> MajorEvent:
    """走一遍真实录入，返回落库的那一行。"""
    result = _run(_call(f"/event add {impact} 全市场 {text} FOMC 议息"))
    assert result.success, result.error
    return MajorEvent.objects.get(name="FOMC 议息")


def _candidate() -> CandidateEvent:
    return events.raise_candidate(
        name="某国议息",
        origin=CandidateOrigin.AGENT.value,
        raised_by="agent:supervisor",
        guessed_time=EVENT_UTC,
    )


@pytest.mark.usefixtures("symbols")
class TestTheCommandsAgainstARealDatabase:
    pytestmark = pytest.mark.django_db(transaction=True)

    # ---------------- 录入 ---------------- #

    def test_ingesting_an_event_writes_a_row_and_echoes_it(self):
        result = _run(_call(f"/event add 高 全市场 {EVENT_TEXT} FOMC 议息"))
        assert result.success, result.error

        event = MajorEvent.objects.get()
        assert event.name == "FOMC 议息"
        assert event.event_time == EVENT_UTC
        assert event.impact == EventImpact.HIGH.value
        assert event.scope_kind == EventScope.MARKET.value
        assert event.status == EventStatus.SCHEDULED.value
        # 默认窗口：事件前 2 小时停、后 1 小时恢复。
        assert event.halt_at == EVENT_UTC - timedelta(minutes=120)
        assert event.resume_at == EVENT_UTC + timedelta(minutes=60)
        # 两个绝对时刻都要回显（CONTEXT.md 第 149 条）。
        assert "熔断窗口：" in result.data
        assert "UTC" in result.data

    def test_the_actor_is_the_person_who_typed_it(self):
        _ingest()
        assert MajorEvent.objects.get().created_by == SENDER

        change = MajorEventChange.objects.get()
        assert change.actor_kind == ActorKind.CHAT.value
        assert change.actor_name == SENDER

    def test_a_symbol_scoped_event_uses_the_order_spelling(self):
        result = _run(_call(f"/event add 高 SOL/USDT,BTC/USDT {EVENT_TEXT} 某所维护"))
        assert result.success, result.error
        event = MajorEvent.objects.get()
        assert event.scope_kind == EventScope.SYMBOLS.value
        assert event.symbols == ["SOL/USDT", "BTC/USDT"]

    def test_the_window_override_is_honoured(self):
        _run(_call(f"/event add 高 全市场 {EVENT_TEXT} FOMC --停前 30 --停后 15"))
        event = MajorEvent.objects.get()
        assert event.halt_at == EVENT_UTC - timedelta(minutes=30)
        assert event.resume_at == EVENT_UTC + timedelta(minutes=15)

    def test_the_window_override_stays_inside_the_global_bounds(self):
        result = _run(_call(f"/event add 高 全市场 {EVENT_TEXT} FOMC --停前 99999"))
        assert result.success, result.error
        assert "上下限" in result.data
        assert not MajorEvent.objects.exists()

    def test_a_settlement_suffix_is_refused_at_ingest(self):
        result = _run(_call(f"/event add 高 SOL/USDT:USDT {EVENT_TEXT} 某所维护"))
        assert result.success, result.error
        assert "结算后缀" in result.data
        assert not MajorEvent.objects.exists()

    def test_an_unknown_symbol_is_refused_and_the_candidates_are_listed(self):
        result = _run(_call(f"/event add 高 SOLUSDT {EVENT_TEXT} 某所维护"))
        assert result.success, result.error
        assert "SOL/USDT" in result.data  # 报错里给出候选写法
        assert not MajorEvent.objects.exists()

    def test_a_missing_name_is_refused(self):
        result = _run(_call(f"/event add 高 全市场 {EVENT_TEXT}"))
        assert result.success, result.error
        assert "名称不能为空" in result.data

    def test_a_timezone_suffix_is_refused(self):
        result = _run(_call("/event add 高 全市场 2026-09-25T20:30Z FOMC"))
        assert result.success, result.error
        assert "时区偏移" in result.data
        assert not MajorEvent.objects.exists()

    def test_the_name_swallows_every_trailing_word(self):
        # 位置读的代价：名称之后的词都算名称的一部分。钉住它，是为了让「多给一个词」
        # 在**字段固定的命令**上仍然报错（见 `_reject_extras` 的那几个用例），而不是这里。
        _run(_call(f"/event add 高 全市场 {EVENT_TEXT} FOMC 议息 临时加开"))
        assert MajorEvent.objects.get().name == "FOMC 议息 临时加开"

    # ---------------- 改档 / 改期 / 取消 ---------------- #

    def test_raising_to_high_from_chat_is_allowed(self):
        event = _ingest()
        result = _run(_call(f"/event impact {event.id} 高"))
        assert result.success, result.error
        event.refresh_from_db()
        assert event.impact == EventImpact.HIGH.value

    def test_a_span_flag_on_the_impact_command_is_refused(self):
        # 放过它的表现不是「窗口没变」，而是一次 TypeError 被当成故障上报。
        event = _ingest()
        result = _run(_call(f"/event impact {event.id} 高 --停前 30"))
        assert result.success, result.error
        assert "不能顺带改窗口" in result.data
        event.refresh_from_db()
        assert event.impact == EventImpact.MEDIUM.value

    def test_rescheduling_moves_the_whole_window(self):
        event = _ingest()
        result = _run(_call(f"/event reschedule {event.id} 2026-09-26 20:30"))
        assert result.success, result.error
        event.refresh_from_db()
        assert event.event_time == EVENT_UTC + timedelta(days=1)
        assert event.halt_at == EVENT_UTC + timedelta(days=1, minutes=-120)
        assert "窗口内改期不会中止已经开启的窗口" in result.data

    def test_cancelling_requires_a_reason(self):
        event = _ingest()
        result = _run(_call(f"/event cancel {event.id}"))
        assert result.success, result.error
        assert "必须给出理由" in result.data
        event.refresh_from_db()
        assert event.status == EventStatus.SCHEDULED.value

    def test_cancelling_keeps_the_row_and_records_the_reason(self):
        event = _ingest()
        result = _run(_call(f"/event cancel {event.id} 交易所维护改期"))
        assert result.success, result.error
        event.refresh_from_db()
        assert event.status == EventStatus.CANCELLED.value
        assert MajorEventChange.objects.filter(kind="cancelled").count() == 1
        assert "不产生任何熔断" in result.data

    def test_an_unknown_id_says_where_to_look(self):
        result = _run(_call("/event cancel 9999 随便什么理由"))
        assert result.success, result.error
        assert "找不到编号" in result.data
        assert "/event list" in result.data

    def test_a_non_numeric_id_is_an_input_error(self):
        result = _run(_call("/event impact 第一条 高"))
        assert result.success, result.error
        assert "必须是数字" in result.data

    # ---------------- 候选 ---------------- #

    def test_confirming_a_candidate_turns_it_into_an_event(self):
        candidate = _candidate()
        result = _run(_call(f"/event confirm {candidate.id} 高 全市场 {EVENT_TEXT}"))
        assert result.success, result.error

        candidate.refresh_from_db()
        assert candidate.status == CandidateStatus.CONFIRMED.value
        assert candidate.decided_by == SENDER
        assert candidate.confirmed_event is not None
        assert candidate.confirmed_event.impact == EventImpact.HIGH.value

    def test_rejecting_a_candidate_records_the_human_verdict(self):
        candidate = _candidate()
        result = _run(_call(f"/event reject {candidate.id}"))
        assert result.success, result.error
        candidate.refresh_from_db()
        assert candidate.status == CandidateStatus.DISCARDED.value
        # 与「到期未确认」的那条自动路径必须可分辨：这一条有人处置过。
        assert candidate.discard_reason == CANDIDATE_DISCARD_REJECTED
        assert candidate.decided_by == SENDER

    def test_rejecting_with_extra_words_is_refused(self):
        # 候选表没有承载理由正文的地方；宁可直接报错，也好过人以为它被记下了。
        candidate = _candidate()
        result = _run(_call(f"/event reject {candidate.id} 因为已经涨过了"))
        assert result.success, result.error
        assert "多出来的参数" in result.data
        candidate.refresh_from_db()
        assert candidate.status == CandidateStatus.PENDING.value

    def test_a_terminal_candidate_cannot_be_confirmed_again(self):
        candidate = _candidate()
        _run(_call(f"/event reject {candidate.id}"))
        result = _run(_call(f"/event confirm {candidate.id} 高 全市场 {EVENT_TEXT}"))
        assert result.success, result.error
        assert "终态不可回退" in result.data
        assert not MajorEvent.objects.exists()

    # ---------------- 列表 ---------------- #

    def test_the_listing_keeps_events_and_candidates_structurally_apart(self):
        _ingest()
        _candidate()
        result = _run(_call("/event list"))
        assert result.success, result.error
        assert "熔断窗口：" in result.data
        assert "无熔断窗口——候选不产生任何动作" in result.data
        assert "候选 #" in result.data

    def test_the_listing_shows_the_ids_the_other_commands_take(self):
        event = _ingest()
        assert f"#{event.id} " in _run(_call("/event list")).data

    def test_the_listing_does_not_count_an_event_whose_window_has_passed(self):
        _ingest(text="2020-01-01 00:00")
        assert "0 条" in _run(_call("/event list")).data

    def test_a_bad_day_count_is_an_input_error(self):
        result = _run(_call("/event list 七天"))
        assert result.success, result.error
        assert "必须是整数" in result.data

    def test_a_bad_day_count_is_bounded(self):
        result = _run(_call("/event list 0"))
        assert result.success, result.error
        assert "至少是 1" in result.data

    # ---------------- 入口本身 ---------------- #

    def test_a_bare_command_prints_the_usage(self):
        result = _run(handle_event_command(_msg("/event"), ""))
        assert result.success, result.error
        assert "/event add" in result.data

    def test_an_unknown_subcommand_prints_the_usage(self):
        result = _run(handle_event_command(_msg("/event"), "expire"))
        assert result.success, result.error
        assert "未知的事件子命令" in result.data
        assert "/event add" in result.data

    def test_an_unknown_operator_is_a_failure_not_feedback(self):
        # 认不出「谁敲的」是管道出了问题，不是人打错了字——它该被当成故障看见。
        result = _run(handle_event_command(_msg("/event", user_id=""), "list"))
        assert not result.success
        assert "无法确定操作者身份" in result.error
