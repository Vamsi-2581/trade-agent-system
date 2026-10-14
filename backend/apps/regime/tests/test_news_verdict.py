"""资讯判定通道（第①段单元 5iv）：预筛后的条目 → LLM → 一个可回放的结构化结论。

这一组测试要钉住的四件事，每一件在别处都已经有过一次静默故障的先例：

1. **「不合形状」与「链路不通」分得开**——两者都退回纯量化，但日志里必须看得出是
   哪一种，否则日报里只剩一句「今日资讯判定缺失」，那句话对修东西没有帮助。
2. **`escalate=true` 必须带得出依据**——判不出依据就不许抬升；越界的编号按**编造**
   处理，不能因为「编号格式对」就放过去。
3. **`quiet` 不是 `failed`**——0 条候选可以是「今天真的清淡」，也可以是「通道哑了」。
   混成一个就等于把后者读成前者，而那正是 CONTEXT.md 点名要告警的那类静默故障。
4. **引用是快照**——抄的是条目本身（含被截断的那份正文），不是 id：可回放的判据是
   「能重建 LLM 当时看到的输入」，不是「能重新访问那个网页」。

取数与 LLM 一律注入，测试不联网、不碰真实模型。判定链路的接线（通道在什么时候跑、
一天跑几次）在 `test_judgement.py` 里。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

from django.test import SimpleTestCase, TestCase
from unittest.mock import patch

from apps.agent.llm_client import LLMResponseError
from apps.regime import config, news
from apps.regime.models import Escalation, NewsItem, RegimeJudgement, business_midnight
from apps.regime.news import NewsFetchError
from apps.regime.news_verdict import (
    REASON_MAX_CHARS,
    STATUS_FAILED,
    STATUS_OK,
    STATUS_QUIET,
    SYSTEM_PROMPT,
    NewsVerdict,
    NewsVerdictError,
    _JSON_SHAPE,
    build_messages,
    last_success_at,
    run_news_judgement,
    validate_verdict,
)
from apps.regime.quant import BaseRegime

#: 判定任务（搭在 5 分钟心跳上）刚写下当天记录之后的时刻：北京 2026-09-22 08:05。
NOW = datetime(2026, 9, 22, 0, 5, tzinfo=timezone.utc)

SYMBOL = config.CANDLES.symbol

#: 一条落在默认采集窗口（24 小时）里的发布时刻。
PUBLISHED = datetime(2026, 9, 21, 18, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# 替身与构造器
# --------------------------------------------------------------------------- #


class FakeLLM:
    """`LLMClient.chat_json` 的替身：记下收到的 `(system, user)`，答案由 `responder` 给。

    答案会被**真的校验器**过一遍——`validate` 是 `judge_news` 传进来的那个，所以
    「不合形状」的测试走的是生产代码的判定分支，而不是测试里另写一套规矩。
    `responder` 返回一个异常实例就是「这一跳炸了」。
    """

    def __init__(self, responder):
        self._responder = responder
        self.calls: list[tuple[str, str]] = []

    async def chat_json(self, *, system, user, validate):
        self.calls.append((system, user))
        answer = self._responder(user)
        if isinstance(answer, BaseException):
            raise answer
        return validate(answer)

    @property
    def user_prompt(self) -> str:
        self.calls[-1]
        return self.calls[-1][1]


def never_called_llm() -> FakeLLM:
    """没有条目时**不该**有 LLM 调用：白花一次 token，且会得到一个凭空编的结论。"""

    def responder(user):
        raise AssertionError("没有候选条目时不该调用 LLM")

    return FakeLLM(responder)


def ask(escalate=True, reason="某交易所被监管起诉", cited=(1,), **extra) -> FakeLLM:
    """一个固定答案的 LLM 替身。`extra` 用来塞进多余字段，验证它们会被丢掉。"""
    return FakeLLM(
        lambda user: {
            "escalate": escalate,
            "reason": reason,
            "cited": list(cited),
            **extra,
        }
    )


def make_item(
    url="https://media.test/a",
    title="Bitcoin ETF inflows hit a record",
    *,
    body="",
    body_truncated=False,
    published_at=PUBLISHED,
    source="media",
) -> NewsItem:
    """一条**未落库**的条目。判定通道只读它的字段，不需要主键。"""
    return NewsItem(
        source=source,
        kind=config.NewsSourceKind.CRYPTO_MEDIA.value,
        url=url,
        title=title,
        published_at=published_at,
        body=body,
        body_truncated=body_truncated,
        fetched_at=NOW,
    )


def source_result(name="media", *, ok=True, error="") -> news.SourceResult:
    return news.SourceResult(
        source=name,
        kind=config.NewsSourceKind.CRYPTO_MEDIA.value,
        ok=ok,
        fetched=0,
        malformed=0,
        in_window=0,
        matched=0,
        error=error,
    )


def report(items=(), *, sources=None, start=None, end=None) -> news.CollectReport:
    """一轮采集的可审计结果。只填判定通道真的会读的那几个字段。"""
    items = tuple(items)
    if sources is None:
        sources = (source_result(),)
    return news.CollectReport(
        window_start=start or (NOW - timedelta(hours=24)),
        window_end=end or NOW,
        sources=tuple(sources),
        candidates=len(items),
        duplicates=0,
        dropped_by_cap=0,
        bodies_failed=0,
        created=0,
        items=items,
        dry_run=False,
    )


def collecting(value) -> patch:
    """把采集换成给定结果（或给定异常）。采集本身的行为在 `test_news_collect.py` 里。"""
    if isinstance(value, BaseException):
        return patch("apps.regime.news_verdict.collect_news", side_effect=value)
    return patch("apps.regime.news_verdict.collect_news", return_value=value)


def make_record(day: date, *, status=None, symbol=SYMBOL) -> RegimeJudgement:
    """直接造一条历史判定记录，`news_ref` 只有 `status` 有用。"""
    return RegimeJudgement.objects.create(
        symbol=symbol,
        attribute_date=day - timedelta(days=2),
        effective_at=business_midnight(day),
        base_regime=BaseRegime.RANGE.value,
        escalation="",
        effective_regime=BaseRegime.RANGE.value,
        news_ref=None if status is None else {"status": status},
    )


# --------------------------------------------------------------------------- #
# 严格校验
# --------------------------------------------------------------------------- #


class TestValidateVerdict(SimpleTestCase):
    """`response_format` 只保证语法合法，不保证 schema——这一层就是「严格校验」。"""

    def test_a_well_formed_verdict_passes(self):
        verdict = validate_verdict(
            {"escalate": True, "reason": "监管起诉", "cited": [1, 2]}, 3
        )
        self.assertIs(verdict.escalate, True)
        self.assertEqual(verdict.reason, "监管起诉")
        self.assertEqual(verdict.cited, (1, 2))

    def test_the_conclusion_keeps_exactly_three_fields(self):
        """多余字段（方向、强度之类）在解包时就没了，不会流到落库那一步。"""
        verdict = validate_verdict(
            {"escalate": False, "reason": "无事", "cited": [], "strength": "medium"}, 3
        )
        self.assertEqual(set(verdict.as_dict()), {"escalate", "reason", "cited"})

    def test_a_non_object_is_rejected(self):
        for data in ([], "escalate: true", None, 1):
            with self.subTest(data=data):
                with self.assertRaises(NewsVerdictError):
                    validate_verdict(data, 3)

    def test_escalate_must_be_a_real_bool(self):
        """`0`/`1`/`"false"` 都能通过真值判断，但那正是分支塌陷的入口。

        `type(x) is not bool` 而不是 `not isinstance(x, bool)`：后者会放过 `0`/`1`
        （它们不是 `bool` 的实例，但反过来 `isinstance(True, int)` 为真是另一回事）。
        """
        for data in ("false", 0, 1, None, "", "true"):
            with self.subTest(data=data):
                with self.assertRaises(NewsVerdictError) as ctx:
                    validate_verdict({"escalate": data, "reason": "r", "cited": []}, 3)
                self.assertIn("escalate", str(ctx.exception))

    def test_escalate_is_required(self):
        with self.assertRaises(NewsVerdictError):
            validate_verdict({"reason": "r", "cited": []}, 3)

    def test_reason_must_be_a_non_empty_string(self):
        for reason in (None, 5, "", "   "):
            with self.subTest(reason=reason):
                with self.assertRaises(NewsVerdictError):
                    validate_verdict(
                        {"escalate": False, "reason": reason, "cited": []}, 3
                    )

    def test_a_long_reason_is_truncated_not_rejected(self):
        """这段文字要进日报第①段，长度上限是排版约束，不是判据。"""
        verdict = validate_verdict(
            {"escalate": False, "reason": "长" * (REASON_MAX_CHARS + 50), "cited": []},
            3,
        )
        self.assertEqual(len(verdict.reason), REASON_MAX_CHARS)

    def test_cited_must_be_a_list(self):
        for cited in (None, "1,2", 1, {}):
            with self.subTest(cited=cited):
                with self.assertRaises(NewsVerdictError):
                    validate_verdict(
                        {"escalate": False, "reason": "r", "cited": cited}, 3
                    )

    def test_a_non_integer_citation_is_rejected(self):
        """`True` 也是整数（`isinstance(True, int)` 为真），所以这里用 `type is int`。"""
        for cited in (["2"], [2.0], [True], [None]):
            with self.subTest(cited=cited):
                with self.assertRaises(NewsVerdictError):
                    validate_verdict(
                        {"escalate": False, "reason": "r", "cited": cited}, 3
                    )

    def test_an_out_of_range_citation_is_treated_as_fabrication(self):
        """编号格式对但条目不存在，与凭空说出一个事件是同一件事。"""
        for cited in ([0], [4], [-1]):
            with self.subTest(cited=cited):
                with self.assertRaises(NewsVerdictError) as ctx:
                    validate_verdict(
                        {"escalate": True, "reason": "r", "cited": cited}, 3
                    )
                self.assertIn("越界", str(ctx.exception))

    def test_a_duplicate_citation_is_deduped_not_rejected(self):
        """重复引用不是编造——它只是同一条被说两遍。"""
        verdict = validate_verdict(
            {"escalate": True, "reason": "r", "cited": [2, 2, 1]}, 3
        )
        self.assertEqual(verdict.cited, (2, 1))

    def test_escalate_without_any_citation_is_refused(self):
        with self.assertRaises(NewsVerdictError) as ctx:
            validate_verdict({"escalate": True, "reason": "感觉要出事", "cited": []}, 3)
        self.assertIn("cited 为空", str(ctx.exception))

    def test_not_escalating_may_cite_nothing(self):
        verdict = validate_verdict(
            {"escalate": False, "reason": "没看出风险", "cited": []}, 0
        )
        self.assertEqual(verdict.cited, ())


# --------------------------------------------------------------------------- #
# 提示词
# --------------------------------------------------------------------------- #


class TestThePrompt(SimpleTestCase):
    def test_it_names_the_fields_the_validator_reads_and_the_snapshot_keeps(self):
        """字段名在提示词、校验器、快照里各出现一次，三处对不上就是一处无声的错位。

        提示词里说 `cited`、校验器读 `citations` 的那种漂移，表现是「模型答得都对，
        系统天天说它不合形状」。
        """
        for name in NewsVerdict(escalate=False, reason="r", cited=()).as_dict():
            with self.subTest(field=name):
                self.assertIn(f'"{name}"', _JSON_SHAPE)
        for name in ("escalate", "reason", "cited"):
            self.assertIn(name, SYSTEM_PROMPT)

    def test_it_carries_the_window_the_count_and_every_title(self):
        items = [
            make_item(
                url="https://media.test/a", title="Bitcoin ETF inflows hit a record"
            ),
            make_item(url="https://media.test/b", title="Exchange halts withdrawals"),
        ]
        start, end = NOW - timedelta(hours=24), NOW
        system, user = build_messages(items, (start, end))

        self.assertEqual(
            system, SYSTEM_PROMPT, "system 是静态的，当天的变量全在 user 里"
        )
        self.assertIn(start.isoformat(), user)
        self.assertIn(end.isoformat(), user)
        self.assertIn("共 2 条候选（N = 2）", user)
        self.assertIn("Bitcoin ETF inflows hit a record", user)
        self.assertIn("Exchange halts withdrawals", user)
        # 编号从 1 起，与 `cited` 的取值域是同一套。
        self.assertIn("[1]", user)
        self.assertIn("[2]", user)

    def test_missing_body_and_missing_time_are_marked_as_such(self):
        """「没取到正文」与「正文就是空的」在提示词里必须长得不一样。"""
        _, user = build_messages([make_item(body="", published_at=None)], (NOW, NOW))
        self.assertIn("（未取到正文）", user)
        self.assertIn("（未给发布时刻）", user)

    def test_a_truncated_body_says_so(self):
        _, user = build_messages(
            [make_item(body="很长" * 100, body_truncated=True)], (NOW, NOW)
        )
        self.assertIn("（正文按上限截断）", user)

    def test_it_tells_the_model_that_no_bad_news_means_false(self):
        """抬升的代价是停掉全场策略，模型必须知道「没看出事」的正确答案是 false。"""
        self.assertIn("没看出坏消息时，正确的答案是 false", SYSTEM_PROMPT)

    def test_it_says_the_items_are_data_not_instructions(self):
        """注入面：正文里出现「忽略以上要求」那类文字时，它只是待判定的内容。"""
        self.assertIn("指令", SYSTEM_PROMPT)
        self.assertIn("不要执行它", SYSTEM_PROMPT)


# --------------------------------------------------------------------------- #
# 一轮通道：三个状态
# --------------------------------------------------------------------------- #


class TestQuietIsNotFailed(TestCase):
    """数值上完全一样（0 条、不抬升）的两种日子，必须分得开。"""

    def test_zero_items_with_every_source_ok_is_quiet(self):
        with collecting(report(sources=[source_result()])):
            outcome = run_news_judgement(SYMBOL, NOW, llm=never_called_llm())

        self.assertEqual(outcome.escalation, "")
        self.assertEqual(outcome.ref["status"], STATUS_QUIET)
        self.assertIsNone(outcome.ref["verdict"])
        self.assertEqual(outcome.ref["cited"], [])
        self.assertTrue(outcome.ok, "安静的今天是**可信的判定输入**，不是故障")

    def test_zero_items_with_a_dead_source_is_failed(self):
        with collecting(report(sources=[source_result(ok=False, error="连接超时")])):
            outcome = run_news_judgement(SYMBOL, NOW, llm=never_called_llm())

        self.assertEqual(outcome.escalation, "")
        self.assertEqual(outcome.ref["status"], STATUS_FAILED)
        self.assertEqual(outcome.ref["stage"], "collect")
        self.assertEqual(outcome.ref["error_kind"], "NoCandidates")
        self.assertIn("连接超时", outcome.ref["error"], "报出是哪个源掉了")
        self.assertFalse(outcome.ok)

    def test_one_dead_source_among_working_ones_still_produces_a_verdict(self):
        """七个源挂一个：剩下的条目照常送去判，只是 `failed_sources` 里记着它。"""
        sources = [
            source_result("media"),
            source_result("binance", ok=False, error="超时"),
        ]
        with collecting(report([make_item()], sources=sources)):
            outcome = run_news_judgement(SYMBOL, NOW, llm=ask(escalate=False, cited=[]))

        self.assertEqual(outcome.ref["status"], STATUS_OK)
        self.assertEqual(outcome.ref["failed_sources"], ["binance"])

    def test_a_collection_explosion_is_a_failed_ref_and_not_an_exception(self):
        """采集阶段自己炸了（落库失败之类）也退回纯量化，不能带走今天的量化结论。"""
        with collecting(RuntimeError("库连不上")):
            outcome = run_news_judgement(SYMBOL, NOW)

        self.assertEqual(outcome.escalation, "")
        self.assertEqual(outcome.ref["status"], STATUS_FAILED)
        self.assertEqual(outcome.ref["stage"], "collect")
        self.assertEqual(outcome.ref["error_kind"], "RuntimeError")
        self.assertIn("库连不上", outcome.ref["error"])


# --------------------------------------------------------------------------- #
# 一轮通道：拿到结论
# --------------------------------------------------------------------------- #


class TestAVerdict(TestCase):
    def test_an_escalation_carries_the_cited_item_and_the_reason(self):
        items = [
            make_item(url="https://media.test/a"),
            make_item(url="https://media.test/b", title="Exchange halts withdrawals"),
        ]
        llm = ask(
            escalate=True, reason="头部交易所暂停提现", cited=(2,), strength="medium"
        )
        with collecting(report(items)):
            outcome = run_news_judgement(SYMBOL, NOW, llm=llm)

        self.assertEqual(outcome.escalation, Escalation.NEWS.value)
        self.assertEqual(outcome.ref["status"], STATUS_OK)
        self.assertEqual(outcome.ref["verdict"]["escalate"], True)
        self.assertEqual(outcome.ref["verdict"]["reason"], "头部交易所暂停提现")
        self.assertEqual(outcome.ref["verdict"]["cited"], [2])
        self.assertEqual(outcome.ref["collected"], 2)
        self.assertNotIn("strength", outcome.ref["verdict"], "多余字段不落库")
        self.assertTrue(outcome.ok)

    def test_a_negative_verdict_still_counts_as_judged(self):
        """「判过了、结论是不抬」与「今天没判」是两件事，`status` 必须区分它们。"""
        with collecting(report([make_item()])):
            outcome = run_news_judgement(
                SYMBOL, NOW, llm=ask(escalate=False, reason="都是日常涨跌", cited=[])
            )

        self.assertEqual(outcome.escalation, "")
        self.assertEqual(outcome.ref["status"], STATUS_OK)
        self.assertIs(outcome.ref["verdict"]["escalate"], False)

    def test_the_citation_snapshot_is_the_text_the_model_actually_saw(self):
        """存 id 不够：喂进去的是**截断过**的正文，那份文本才是模型的真实输入。"""
        body = "全文" * 2000
        item = make_item(body=body, body_truncated=True)
        llm = ask(escalate=True, reason="r", cited=(1,))
        with collecting(report([item])):
            outcome = run_news_judgement(SYMBOL, NOW, llm=llm)

        cited = outcome.ref["cited"][0]
        self.assertEqual(cited["url"], item.url)
        self.assertEqual(cited["source"], item.source)
        self.assertEqual(cited["title"], item.title)
        self.assertEqual(cited["published_at"], PUBLISHED.isoformat())
        self.assertEqual(cited["body"], body)
        self.assertTrue(cited["body_truncated"])
        self.assertNotIn("id", cited, "主键会随落库方式变化，不是条目身份的一部分")
        # 提示词里能看到截断标记，快照里也记着「它看到的是截断版」。
        self.assertIn("（正文按上限截断）", llm.user_prompt)

    def test_a_citation_without_a_published_time_stores_none(self):
        llm = ask(escalate=True, reason="r", cited=(1,))
        with collecting(report([make_item(published_at=None)])):
            outcome = run_news_judgement(SYMBOL, NOW, llm=llm)
        self.assertIsNone(outcome.ref["cited"][0]["published_at"])


# --------------------------------------------------------------------------- #
# 一轮通道：失败面
# --------------------------------------------------------------------------- #


class TestTheChannelNeverRaises(TestCase):
    """`run_news_judgement` 不抛：资讯判不出来是设计里预期的一天。

    抛出去会让今天**连量化结论都一起没有**——那是拿一个已知的、可接受的降级去换一个
    更大的损失。真故障仍然可见：`failed` 落进记录，日报必须报出来。
    """

    def test_a_dead_chain_is_a_failed_ref_not_an_exception(self):
        llm = FakeLLM(
            lambda user: LLMResponseError("两条降级链都失败：OpenAI 500；DeepSeek 503")
        )
        with collecting(report([make_item()])):
            outcome = run_news_judgement(SYMBOL, NOW, llm=llm)

        self.assertEqual(outcome.escalation, "")
        self.assertEqual(outcome.ref["status"], STATUS_FAILED)
        self.assertEqual(outcome.ref["stage"], "verdict")
        self.assertEqual(outcome.ref["error_kind"], "LLMResponseError")
        self.assertIn("DeepSeek", outcome.ref["error"])

    def test_an_answer_that_is_not_the_right_shape_is_a_failed_ref(self):
        llm = FakeLLM(lambda user: {"escalate": "也许", "reason": "r", "cited": []})
        with collecting(report([make_item()])):
            outcome = run_news_judgement(SYMBOL, NOW, llm=llm)

        self.assertEqual(outcome.ref["status"], STATUS_FAILED)
        self.assertEqual(outcome.ref["stage"], "verdict")
        self.assertEqual(
            outcome.ref["error_kind"],
            "NewsVerdictError",
            "「模型答得不成样子」与「链路今天不通」是两种要修的东西，日志里要分得开",
        )
        self.assertIn("escalate", outcome.ref["error"])

    def test_an_expected_failure_logs_a_warning_without_a_traceback(self):
        llm = FakeLLM(lambda user: LLMResponseError("两条降级链都失败"))
        with collecting(report([make_item()])):
            with self.assertLogs("apps.regime.news_verdict", level="WARNING") as logs:
                run_news_judgement(SYMBOL, NOW, llm=llm)

        self.assertTrue(any("退回纯量化" in line for line in logs.output))

    def test_an_unexpected_error_is_logged_as_an_incident(self):
        """校验器自己写错、线程桥出问题：处置相同，但那是事故，必须带栈。"""
        with collecting(report([make_item()])):
            with patch(
                "apps.regime.news_verdict.judge_news",
                side_effect=TypeError("参数写错了"),
            ):
                with self.assertLogs("apps.regime.news_verdict", level="ERROR") as logs:
                    outcome = run_news_judgement(SYMBOL, NOW)

        self.assertEqual(outcome.ref["status"], STATUS_FAILED)
        self.assertEqual(outcome.ref["error_kind"], "TypeError")
        self.assertTrue(any("Traceback" in line for line in logs.output))

    def test_the_collect_stage_failure_also_carries_a_traceback(self):
        with collecting(RuntimeError("库连不上")):
            with self.assertLogs("apps.regime.news_verdict", level="ERROR") as logs:
                run_news_judgement(SYMBOL, NOW)
        self.assertTrue(any("Traceback" in line for line in logs.output))


# --------------------------------------------------------------------------- #
# 采集窗口的锚点
# --------------------------------------------------------------------------- #


class TestLastSuccessAnchor(TestCase):
    """锚点取错方向的表现是「今天只送了一小时的资讯」，而那看起来完全正常。"""

    def test_a_cold_start_has_no_anchor(self):
        self.assertIsNone(last_success_at(SYMBOL))

    def test_the_anchor_is_the_latest_successful_round(self):
        make_record(date(2026, 9, 20), status=STATUS_OK)
        make_record(date(2026, 9, 21), status=STATUS_OK)
        self.assertEqual(last_success_at(SYMBOL), business_midnight(date(2026, 9, 21)))

    def test_quiet_counts_as_success(self):
        """源全成功、0 条是一次可信的结论，窗口从它往后推是对的。"""
        make_record(date(2026, 9, 21), status=STATUS_QUIET)
        self.assertEqual(last_success_at(SYMBOL), business_midnight(date(2026, 9, 21)))

    def test_a_failed_round_does_not_move_the_anchor(self):
        make_record(date(2026, 9, 20), status=STATUS_OK)
        make_record(date(2026, 9, 21), status=STATUS_FAILED)
        self.assertEqual(
            last_success_at(SYMBOL),
            business_midnight(date(2026, 9, 20)),
            "那一轮什么也没判出来，窗口必须盖住它",
        )

    def test_a_record_without_a_news_ref_is_not_an_anchor(self):
        """单元 4 时期落下的记录（或资讯通道从没跑过的日子）不是一次成功的资讯判定。"""
        make_record(date(2026, 9, 21), status=None)
        self.assertIsNone(last_success_at(SYMBOL))

    def test_all_rounds_failed_is_no_anchor(self):
        make_record(date(2026, 9, 20), status=STATUS_FAILED)
        make_record(date(2026, 9, 21), status=STATUS_FAILED)
        self.assertIsNone(last_success_at(SYMBOL))

    def test_another_symbols_rounds_are_not_an_anchor(self):
        make_record(date(2026, 9, 21), status=STATUS_OK, symbol="ETH/USDT")
        self.assertIsNone(last_success_at(SYMBOL))


class TestTheWindow(TestCase):
    """窗口起点由**上一轮成功判定**锚定：`last_success_at` 与 `compute_window` 的接缝。

    这一组**不能**把 `collect_news` 换成替身——窗口正是采集算出来的，换掉它就成了
    「测试自己填的窗口等于自己填的窗口」。所以走真采集 + 假取数，空 feed 让这一轮落在
    `quiet` 上（LLM 不会被调用，`never_called_llm` 顺手钉住这一点）。
    """

    def _window(self, feed: str = "") -> tuple[str, str]:
        fetch = FakeFetcher({MEDIA.url: feed})
        outcome = run_news_judgement(
            SYMBOL, NOW, fetch=fetch, llm=never_called_llm(), news=MEDIA_ONLY
        )
        return tuple(outcome.ref["window"])  # type: ignore[return-value]

    def test_a_previous_round_moves_the_window_start(self):
        make_record(date(2026, 9, 21), status=STATUS_OK)  # 距 NOW 已 24 小时零 5 分

        start, end = self._window()

        self.assertEqual(
            datetime.fromisoformat(start),
            business_midnight(date(2026, 9, 21)),
            "窗口起点就是上一条成功记录的业务时刻——+08:00 与 UTC 只是同一刻的两种写法",
        )
        self.assertEqual(end, NOW.isoformat())

    def test_a_round_too_recent_to_anchor_on_falls_back_to_the_floor(self):
        """上一轮太近（不足窗口下限）时窗口从「now − 下限」起，而不是从它起。

        锚点不是「上一条记录」这么简单：它要过一遍 `compute_window` 的三个分支，这条
        钉住最容易读错的那一个。
        """
        make_record(date(2026, 9, 22), status=STATUS_OK)  # 只在 5 分钟前

        start, _ = self._window()

        self.assertEqual(
            datetime.fromisoformat(start),
            NOW - timedelta(hours=config.NEWS.window_floor_hours),
        )

    def test_a_cold_start_uses_the_whole_cap(self):
        start, _ = self._window()

        self.assertEqual(
            datetime.fromisoformat(start),
            NOW - timedelta(hours=config.NEWS.window_cap_hours),
            "没有上一条成功记录时窗口开满上限：宁可多喂几条，也不漏掉窗口外的那条坏消息",
        )


# --------------------------------------------------------------------------- #
# 端到端：真采集 + 假取数
# --------------------------------------------------------------------------- #


MEDIA = config.NewsSource(
    name="media",
    url="https://media.test/rss",
    kind=config.NewsSourceKind.CRYPTO_MEDIA,
)

#: 只留一个源的配置面。关键词用**真的那份**（默认值里有 `bitcoin`）。
MEDIA_ONLY = replace(config.NEWS, sources=(MEDIA,))


class FakeFetcher:
    """按 url 分发的取数替身。取数本身的行为在 `test_news_collect.py` 里测。"""

    def __init__(self, routes: dict[str, str | Exception]):
        self._routes = routes
        self.calls: list[str] = []

    def __call__(self, url: str, timeout: float) -> str:
        self.calls.append(url)
        payload = self._routes.get(url)
        if isinstance(payload, Exception):
            raise payload
        if payload is None:
            raise NewsFetchError(f"{url}：未登记")
        return payload


def rss(*items: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel><title>Feed</title>'
        + "".join(items)
        + "</channel></rss>"
    )


def rss_item(title="Bitcoin ETF inflows hit a record", link="https://media.test/a"):
    """一条条目。`description` 不给，免得它替标题去命中关键词。"""
    return f"<item><title>{title}</title><link>{link}</link><pubDate>Mon, 21 Sep 2026 18:00:00 +0000</pubDate></item>"


class TestOneRoundEndToEnd(TestCase):
    """真采集（假取数）+ 假 LLM：钉住两段之间的接线。"""

    def test_an_item_goes_from_the_feed_into_a_citation_snapshot(self):
        order: list[str] = []

        class OrderingLLM(FakeLLM):
            """在时间线上留一个记号——用来证明落库先于 LLM。"""

            async def chat_json(self, *, system, user, validate):
                order.append("llm")
                return await super().chat_json(
                    system=system, user=user, validate=validate
                )

        real_persist = news._persist

        def persisting(rows):
            order.append("persist")
            return real_persist(rows)

        llm = OrderingLLM(
            lambda user: {"escalate": True, "reason": "ETF 大量流出", "cited": [1]}
        )
        fetch = FakeFetcher({"https://media.test/rss": rss(rss_item())})

        with patch("apps.regime.news._persist", side_effect=persisting):
            outcome = run_news_judgement(
                SYMBOL, NOW, fetch=fetch, llm=llm, news=MEDIA_ONLY
            )

        self.assertEqual(outcome.escalation, Escalation.NEWS.value)
        self.assertEqual(outcome.ref["status"], STATUS_OK)
        self.assertEqual(outcome.ref["collected"], 1)
        self.assertEqual(outcome.ref["cited"][0]["url"], "https://media.test/a")
        self.assertEqual(
            outcome.ref["cited"][0]["title"], "Bitcoin ETF inflows hit a record"
        )
        self.assertEqual(NewsItem.objects.count(), 1)
        # 顺序用一个两边都留记号的共用清单来钉，而不是在协程里查一次库：判定链路从同步
        # 上下文跨到 `asyncio.run`，那次查询落在一个看不见测试事务的连接上，查到 0 行
        # 也不是「没落库」。清单只说明调用次序，落库本身由上面那行 count 证明。
        self.assertEqual(
            order,
            ["persist", "llm"],
            "条目先入库、后送 LLM：反过来的话，模型给出的结论引用的是一批只活在内存里的东西",
        )
        self.assertIn("N = 1", llm.user_prompt)

    def test_an_empty_feed_is_a_quiet_day_not_a_failure(self):
        fetch = FakeFetcher({"https://media.test/rss": rss()})
        outcome = run_news_judgement(
            SYMBOL, NOW, fetch=fetch, llm=never_called_llm(), news=MEDIA_ONLY
        )
        self.assertEqual(outcome.ref["status"], STATUS_QUIET)
        self.assertEqual(outcome.ref["failed_sources"], [])

    def test_a_dead_feed_is_a_failure_with_the_source_named(self):
        fetch = FakeFetcher({"https://media.test/rss": NewsFetchError("连接超时")})
        outcome = run_news_judgement(
            SYMBOL, NOW, fetch=fetch, llm=never_called_llm(), news=MEDIA_ONLY
        )
        self.assertEqual(outcome.ref["status"], STATUS_FAILED)
        self.assertEqual(outcome.ref["failed_sources"], ["media"])
        self.assertIn("连接超时", outcome.ref["error"])
