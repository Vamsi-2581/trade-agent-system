"""五段日报的生成与落库（第①段单元 8iii）。

`test_timing.py` 钉的是接线（这条职责挂在哪、排在谁后面）；这个文件钉的是它的另一半——
**一天一条写成什么样**。判定与推导的逻辑在这里一行都不断言，那会是第二处真相。

本文件钉的六条性质，每一条坏了都不报警、只出错的东西：

1. **报的是「明日才生效」的那条**（CONTEXT.md 第 173 条）。第①段读 `effective_at`，正文里
   必须明写生效时刻，否则「今日判定」这个词就在撒谎——此刻咬人的是另一条。拿
   `current_judgement()`（今天生效的那条）糊过去，日子在正常跑，只是日报永远晚一天，
   而晚一天的结论看起来完全正常。
2. **判定缺失也是一条要说的话**。数据类失败（日线没到、预热未满）时判定正常返回、只是
   没有结论；那时日报**照发**（等到截止时刻），第①段明写「今日判定缺失，处于保持的上一
   有效状态」。反过来，在截止时刻之前不写——一天一条是唯一约束，先写一条「判定缺失」会
   把当天这条永久钉死，后面等到结论也改不回来。
3. **第②段是结构化的，且基准是昨天那份日报**。`DeactivationDecision` 的行是原地更新的，
   明天再去读读到的已经不是今天的世界；`landscape` 那份快照是唯一能回答「昨天说了什么」
   的东西。缺昨日日报时**必须明写「这是首次」**，不能渲染成「无变化」——后者读起来像
   「机制看了，没事」。
4. **裁剪按活跃会话，不按策略的 `is_active`**。后者是「这个策略退役了没有」的人工总开关
   （CONTEXT.md 第 106 条），与「谁在跑它」是两件事。裁完为空的用户**整节不出现**。
5. **跨模块的口径是同源抄来的，不是各写一份**：`ACTION_CARRIED` 与判定层的
   `_DECISION_CARRIED`、`REPORT.event_horizon_days` 与 `/event list` 的
   `DEFAULT_HORIZON_DAYS`、第③段的查询形状与 `event_commands._list`。三处漂开都会表现成
   「日报说有 2 条、追问时工具说有 5 条」，而这种不一致没人会当成 bug 报上来。
6. **候选那一节有两半，缺一半不报警**：待人工处置的，与到期未确认被丢掉的（第 152 条
   要的那份衰减证据）。清理任务一挂上 beat，过期候选就从「待人工处置」里整个消失——
   没有后一半，这一节只剩「没有」两个字，而「机制提过、没人处置」再没有读者。

**分层**：`TestContract` / `TestTheStructuredChange` / `TestCropForOneUser` 三个类是
`SimpleTestCase`——碰一下库就报错，所以「这几段不落库」是被强制的，不靠 docstring 声明。
`TestPureRendering` 只读（`_switch_lines` 会读切换流水表，所以它只能是 `TestCase`），
`TestTheDeclarationLine` 落几行声明（第③段 W4 加的那一行读的是声明表），
`TestTheCandidateSection` 落几行候选（第③段末尾那一节读候选表），
`TestTheWriteGate` 与后面五个类落真库。

**造数据用真 UUID**：策略主键是 `UUIDField`，而 `_strategy_names` 会拿建议里的
`strategy_id` 去 `filter(id__in=...)`。塞一个 `"s1"` 进去会当场 `ValidationError`，而不是
安静地取不到名字——所以凡是要流进 `write_daily_report` 的 id 一律用 `_new_id()`。
"""

from __future__ import annotations

import dataclasses
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase

from apps.agent import event_commands
from apps.common.time_utils import business_tz, format_business
from apps.regime import config, deactivation, deactivation_run, halt, judgement, report
from apps.regime.models import (
    CANDIDATE_DISCARD_EXPIRED,
    CANDIDATE_DISCARD_REJECTED,
    NO_ESCALATION_DISPLAY,
    ActorKind,
    CandidateEvent,
    CandidateOrigin,
    CandidateStatus,
    DailyReport,
    HaltDeclaration,
    HaltTrigger,
    MechanismKind,
    MechanismMode,
    RegimeJudgement,
    RegimeMechanismSwitch,
    business_midnight,
)
from apps.regime.quant import BaseRegime
from apps.regime.tests.test_shadow import (
    RUN_DAY,
    SYMBOL,
    concluded_payload,
    derivation,
    skipped_payload,
    suggestion,
)
from apps.trading.models import LiveSession, Strategy

#: 判定机制的名义时刻：北京 2026-09-22 08:00（= UTC 00:00），日界刚过、判定刚落库。
NOW = datetime.combine(RUN_DAY, time(8, 0), tzinfo=business_tz())
#: 截止时刻（`ReportConfig.watchdog_hour:watchdog_minute` = 09:00）之前 / 之后。
BEFORE_DEADLINE = datetime.combine(RUN_DAY, time(8, 30), tzinfo=business_tz())
AFTER_DEADLINE = datetime.combine(RUN_DAY, time(10, 0), tzinfo=business_tz())


# --------------------------------------------------------------------------- #
# 造数据
# --------------------------------------------------------------------------- #


def _new_id() -> str:
    """一个真的策略 id（UUID 字符串）。

    凡是要流进 `write_daily_report` 的 `strategy_id` 都必须是真的：`_strategy_names` 拿它
    去 `Strategy.objects.filter(id__in=[...])`，而那一列是 `UUIDField`——假 id 不是「差不到
    名字」，是当场炸。
    """
    return str(uuid4())


def _report(**over) -> DailyReport:
    """一份没写进库的日报，用来试 `crop_for` / `_summary`——两处都只读字段。"""
    fields = {
        "symbol": SYMBOL,
        "run_day": RUN_DAY,
        "sections": {
            report.SECTION_TODAY: "一的内容",
            report.SECTION_CHANGE: "落库那一份（未裁剪，不该被 crop_for 用到）",
            report.SECTION_EVENTS: "三的内容",
            report.SECTION_HEALTH: "四的内容",
            report.SECTION_DELIVERY: "五的内容",
        },
        "landscape": {
            report.SECTION_CHANGE: {
                "status": "diff",
                "items": [
                    {
                        "kind": "halt",
                        "regime": "downtrend",
                        "regime_display": "下行趋势",
                        "strategies": [
                            {
                                "strategy_id": "s1",
                                "name": "甲",
                                "running": True,
                                "exempt": False,
                            },
                            {
                                "strategy_id": "s2",
                                "name": "乙",
                                "running": True,
                                "exempt": False,
                            },
                        ],
                    }
                ],
            }
        },
    }
    fields.update(over)
    return DailyReport(**fields)


def _layer(*ids: str) -> list[dict]:
    """`by_regime` 里的值：一层里的策略条目。做差只读 `strategy_id`，其余字段按需。

    `crop_for` / `render_change` 的用例走的是**已经在库里的那份快照**，那边的 id 从来不会
    被拿去查库（名字是写快照时查好的），所以这里不要求真 UUID。
    """
    return [{"strategy_id": sid} for sid in ids]


# --------------------------------------------------------------------------- #
# 契约：与别的模块同源的那几个数
# --------------------------------------------------------------------------- #


class TestContract(SimpleTestCase):
    """跨模块同源的口径。**这几条断言就是「同源」二字的唯一强制手段。**"""

    def test_carried_action_matches_the_judgement_layer(self):
        # 第①段要认出「量化判出的候选被持续期拦下、于是沿用当前阶段」这一种收场，
        # 而它只存在于判定层的私有名里。两处必须一起改，所以钉住。
        self.assertEqual(report.ACTION_CARRIED, judgement._DECISION_CARRIED)

    def test_the_blanket_predicate_comes_from_the_deactivation_layer(self):
        # 「哪一档是保命档」只能有一处判据（`deactivation.is_blanket`），日报这边不自己写
        # `regime == "high_vol"`。判错了不会报错，只会把抬升日渲染成一片「将解除」——
        # 那是「写下去没有复活路径」的那一类。所以把条目里的 slug 钉在那个判据上。
        self.assertTrue(deactivation.is_blanket(report._blanket_item("halt")["regime"]))

    def test_the_horizon_matches_the_slash_command(self):
        # 日报第③段与 `/event list` 回答的是同一个问题。天数一旦分成两个数，
        # 「日报说有 2 条、追问时工具说有 5 条」就回来了。
        self.assertEqual(
            config.REPORT.event_horizon_days, event_commands.DEFAULT_HORIZON_DAYS
        )

    def test_the_horizon_is_configurable_not_hardcoded(self):
        # CONTEXT.md 第 80 条把「`query_events` 的查询天数」列进统一配置面。
        # 「写死的 7 天」（第 183 条）说的是**不给 Agent 传参**，不是不进配置面——
        # 所以这个数必须是 `ReportConfig` 的一个字段，而不是 `report.py` 里的常量。
        self.assertIs(config.GROUPS["report"], config.REPORT)
        self.assertIn(
            "event_horizon_days",
            {field.name for field in dataclasses.fields(config.REPORT)},
        )

    def test_the_five_sections_are_a_fixed_contract(self):
        # `sections` 是落库的 JSON，8v 的只读工具与审计会按键取段。
        self.assertEqual(
            report.SECTIONS,
            ("today", "change", "events", "health", "delivery"),
        )
        self.assertEqual(set(report._SECTION_TITLES), set(report.SECTIONS))

    def test_the_events_title_carries_the_horizon(self):
        self.assertIn("{horizon}", report._SECTION_TITLES[report.SECTION_EVENTS])

    def test_skip_display_covers_every_shape_the_judgement_returns(self):
        # `run_daily_judgement` 的四种「没结论」收场。缺一种，第①段就会把 slug 原样丢给
        # 用户（「今日判定缺失（stale_candles）」）——能读，但那不是给人看的字。
        self.assertEqual(
            set(report._SKIP_DISPLAY),
            {"no_candles", "stale_candles", "empty_series", "undecidable"},
        )
        for slug in report._SKIP_DISPLAY:
            self.assertEqual(
                report._SKIP_DISPLAY.get(slug, slug), report._SKIP_DISPLAY[slug]
            )

    def test_blocked_display_falls_back_to_the_raw_slug(self):
        # 推导层有三种收场，其中「还没有一代池化表」不在 `BLOCKED_DISPLAY` 里。两处都
        # 写 `BLOCKED_DISPLAY.get(x, x)`，于是它回显 slug 而**不是空字符串**——
        # 空字符串会让那一段看起来像「没有备注」。这里钉住这个兜底是故意的。
        self.assertEqual(deactivation_run.SKIPPED_NO_GENERATION, "no_generation")
        self.assertIn(deactivation.BLOCKED_COLD_START, deactivation.BLOCKED_DISPLAY)
        self.assertIn(deactivation.BLOCKED_STALE_STATE, deactivation.BLOCKED_DISPLAY)
        self.assertNotIn(
            deactivation_run.SKIPPED_NO_GENERATION, deactivation.BLOCKED_DISPLAY
        )
        self.assertTrue(
            deactivation.BLOCKED_DISPLAY.get(
                deactivation_run.SKIPPED_NO_GENERATION,
                deactivation_run.SKIPPED_NO_GENERATION,
            )
        )


# --------------------------------------------------------------------------- #
# 纯渲染：不落库的那几段
# --------------------------------------------------------------------------- #


class TestPureRendering(TestCase):
    """这段不写库。用 `concluded_payload(with_record=False)` 就是为了别在读的用例里写行。"""

    def test_deadline_is_read_in_the_business_timezone(self):
        # 「早九点还没收到就该响了」是给人看的钟点，不是 UTC 小时。
        deadline = report._deadline(RUN_DAY)
        self.assertEqual(deadline.hour, config.REPORT.watchdog_hour)
        self.assertEqual(deadline.minute, config.REPORT.watchdog_minute)
        self.assertEqual(deadline.tzinfo, business_tz())
        self.assertLess(deadline, business_midnight(RUN_DAY + timedelta(days=2)))

    def test_ready_is_true_the_moment_there_is_a_conclusion(self):
        self.assertTrue(
            report._ready(
                concluded_payload(with_record=False), RUN_DAY, BEFORE_DEADLINE
            )
        )

    def test_ready_waits_out_a_conclusion_less_day(self):
        pending = skipped_payload()
        self.assertFalse(report._ready(pending, RUN_DAY, BEFORE_DEADLINE))
        # 到点即写，边界是闭的。
        self.assertTrue(report._ready(pending, RUN_DAY, report._deadline(RUN_DAY)))
        self.assertTrue(report._ready(pending, RUN_DAY, AFTER_DEADLINE))

    def test_regime_display_never_guesses(self):
        self.assertEqual(report._regime_display("downtrend"), "下行趋势")
        self.assertEqual(report._regime_display(""), "（无）")
        self.assertEqual(report._regime_display(None), "（无）")
        # 认不出来就原样回显，不编一个中文名。
        self.assertEqual(report._regime_display("whatever"), "whatever")

    def test_escalation_display_comes_from_the_model_layer(self):
        # **`Escalation` 是 `str` 枚举**：`f"{escalation}"` 不报错，只是把 slug「news」
        # 端到用户眼前，而这一段的全部意义是回答「今天为什么更保守」。展示名的唯一出处
        # 是模型层（`NO_ESCALATION_DISPLAY` 的 docstring 明写「不要就地写中文」）。
        self.assertEqual(report._escalation_display("news"), "资讯抬升")
        self.assertEqual(report._escalation_display(""), NO_ESCALATION_DISPLAY)
        self.assertEqual(report._escalation_display(None), NO_ESCALATION_DISPLAY)
        self.assertEqual(report._escalation_display("whatever"), "whatever")

    def test_number_formatting(self):
        self.assertEqual(report._num(None), "未算")
        self.assertEqual(report._num(1.23456), "1.2346")
        self.assertEqual(report._num(1.23456, digits=6), "1.234560")
        self.assertEqual(report._pct(0.8123), "81.2%")
        self.assertEqual(report._pct(None), "None")

    def test_today_section_writes_the_effective_moment(self):
        # 「必须明写生效时刻」——这是第①段唯一的硬要求（CONTEXT.md 第 173 条）。
        result = concluded_payload(
            regime="range", effective_regime="downtrend", with_record=False
        )
        body = report._section_today(result, None, SYMBOL)
        self.assertIn("今日判定：下行趋势", body)
        self.assertIn("生效时刻：", body)
        self.assertIn("本条要到生效时刻才咬人", body)
        self.assertIn("基础阶段：箱体震荡", body)

    def test_today_section_calls_out_an_escalation(self):
        result = concluded_payload(
            regime="range",
            escalation="news",
            effective_regime="high_vol",
            with_record=False,
        )
        body = report._section_today(result, None, SYMBOL)
        self.assertIn("被抬升为 高波动", body)
        self.assertIn("资讯抬升", body)

    def test_today_section_reads_the_numbers_out_of_the_record_evidence(self):
        # 依据只从判定记录自己的 `evidence` 里读——那是判定当时亲手写下的原话。
        result = concluded_payload(regime="high_vol", with_record=False)
        record = SimpleNamespace(
            evidence={
                "quant": {
                    "regime": "high_vol",
                    "atr_pct": 4.2,
                    "atr_pct_rank": 0.91,
                    "quantile_sample": 250,
                    "ema_fast": 61000.0,
                    "ema_slow": 64000.0,
                    "ema_slope": -0.001234,
                    "separation": -0.75,
                },
                "decision": {"action": "adopted"},
            }
        )
        body = report._section_today(result, record, SYMBOL)
        self.assertIn("ATR% 4.2000，250 日分位 91.0%（高于高波动阈值）", body)
        self.assertIn("分离度 (EMA快−EMA慢)/ATR = -0.750", body)

    def test_the_high_vol_note_compares_the_rank_not_the_percentage(self):
        # 判据是「ATR% 分位 > 80%」，拿来比的是**分位**这个数。拿 ATR% 本身去比 0.80，
        # 两个量纲差着一个数量级，会把「今天波动很大」判成常态化。
        result = concluded_payload(regime="high_vol", with_record=False)
        record = SimpleNamespace(
            evidence={"quant": {"atr_pct": 0.9, "atr_pct_rank": 0.10}}
        )
        body = report._section_today(result, record, SYMBOL)
        self.assertIn("ATR% 0.9000，250 日分位 10.0%", body)
        self.assertNotIn("高于高波动阈值", body)

    def test_a_record_that_says_nothing_yields_no_numbers(self):
        # 取不到就不写，不拿别的量凑：一句编出来的「依据」比没有依据更坏。
        self.assertEqual(report._quant_lines(None), [])
        self.assertEqual(report._quant_lines(SimpleNamespace(evidence=None)), [])
        self.assertEqual(report._quant_lines(SimpleNamespace(evidence={})), [])

    def test_switch_lines_admits_what_it_cannot_know(self):
        # 第②段给切换流水加了 `kind` 之后，「各自最近一次生效时间」从「取不到」变成了
        # **一个开关查一次**——但这里一行流水都没有，所以三行都只能如实说「无切换流水」。
        # 断言按条数而不是按整段文本：三行各查各的这件事，只有数一数才看得出来；只断言
        # 「无切换流水」在不在的话，三个开关塌成一行也照样通过。
        body = "\n".join(report._switch_lines(now=NOW))
        self.assertIn("三个开关（各自独立、各自人工确认）：", body)
        self.assertEqual(body.count("无切换流水"), 4)  # 机制整体 1 + 三个开关各 1
        for kind in report._SWITCH_KINDS:
            self.assertIn(f"  {kind.display}：", body)
        self.assertIn("出 Shadow（判定 + 切片 + 自动停用）", body)
        self.assertIn("事件熔断", body)
        self.assertIn("行情阶段 gate", body)
        # 保命档**不在这三个开关里**：它是阶段本身的性质，不需要人工确认。不写这一行，
        # 读日报的人会以为高波动档也要等某个开关被打开。
        self.assertIn("保命档（高波动）：不在这三个开关里", body)

    def test_switch_lines_reads_each_switch_separately(self):
        # 三个开关各查各的流水：只切一个，另外两行必须还是「无切换流水」。写成一个查询
        # 的话，一次人工关掉事件熔断会被读成「机制整体」也动过。
        RegimeMechanismSwitch.objects.create(
            kind=MechanismKind.EVENT_BREAKER.value,
            from_mode=MechanismMode.SHADOW.value,
            to_mode=MechanismMode.EXECUTING.value,
            at=NOW,
            actor_kind=ActorKind.CLI,
            actor_name="ops",
            reason="事件熔断上线",
        )
        body = "\n".join(report._switch_lines(now=NOW))

        self.assertEqual(body.count("无切换流水"), 3)  # 机制整体 + 另两个开关
        self.assertIn(
            f"  事件熔断：执行态（最近一次生效 {format_business(NOW)}）", body
        )
        self.assertIn("机制当前档：Shadow（只记录，不执行）", body)

    def test_render_body_omits_the_change_section_when_it_is_empty(self):
        sections = {name: "内容" for name in report.SECTIONS}
        sections[report.SECTION_CHANGE] = ""
        body = report.render_body(sections, symbol=SYMBOL, run_day=RUN_DAY)
        self.assertNotIn("二、相对昨日的变化", body)
        self.assertIn("三、未来 7 天的高影响事件", body)

    def test_render_body_marks_an_absent_section_but_keeps_its_title(self):
        # 缺一节与「这一节没有内容」必须能被区分开。
        sections = {name: "内容" for name in report.SECTIONS}
        sections[report.SECTION_TODAY] = ""
        sections[report.SECTION_CHANGE] = ""
        body = report.render_body(sections, symbol=SYMBOL, run_day=RUN_DAY)
        self.assertIn("【一、今日判定】", body)
        self.assertIn("（本节无内容）", body)

    def test_render_body_carries_the_horizon_from_config(self):
        sections = {name: "内容" for name in report.SECTIONS}
        body = report.render_body(sections, symbol=SYMBOL, run_day=RUN_DAY)
        self.assertIn(
            f"【三、未来 {config.REPORT.event_horizon_days} 天的高影响事件】", body
        )


# --------------------------------------------------------------------------- #
# 第④段里「停止声明：此刻 …」那一行（第③段 W4）
# --------------------------------------------------------------------------- #


def _declare(
    *,
    trigger: HaltTrigger = HaltTrigger.EVENT,
    scope: str | None = None,
    label: str = "FOMC 议息",
) -> HaltDeclaration:
    """一条此刻活着的声明。（触发源 × 作用域）是唯一键，所以同一档要多行就给不同作用域。"""
    return HaltDeclaration.objects.create(
        trigger=trigger.value,
        scope=scope or halt.global_scope(),
        label=label,
        opened_at=NOW - timedelta(hours=2),
        expires_at=None,
        closed_at=None,
        reason="测试",
        actor_kind=ActorKind.TASK.value,
        actor_name="regime.sync_halt_windows",
    )


def _open_switch(kind: MechanismKind) -> RegimeMechanismSwitch:
    """把这个开关拨到执行态。`at` 早于 `NOW`，所以 `current()` 读得到它。"""
    return RegimeMechanismSwitch.objects.create(
        kind=kind.value,
        from_mode=MechanismMode.SHADOW.value,
        to_mode=MechanismMode.EXECUTING.value,
        at=NOW - timedelta(days=1),
        actor_kind=ActorKind.CLI.value,
        actor_name="ops",
        reason="测试",
    )


class TestTheDeclarationLine(TestCase):
    """第③段 W4：日报第④段加了「停止声明表此刻的实数」这一行。

    这一行的要点是**它说的时刻**：声明表吃的是**生效中**的判定，而第④段上面那行
    「上次判定成功（结论 …）」报的是**待生效**的那条。所以断言按**整行**比对——句子里的
    「此刻」「按生效中的阶段」与层名、条数是一个整体，少了任何一半，这一行就成了
    「今天那条判定已经在拦」的假话（而它读起来完全正常）。
    """

    def test_each_layer_is_counted_and_the_effective_phase_is_named(self):
        _declare(trigger=HaltTrigger.BLANKET, label="保命档（高波动）")
        _declare(trigger=HaltTrigger.EVENT)
        _declare(trigger=HaltTrigger.DEACTIVATION, scope=f"strategy:{_new_id()}")
        _declare(trigger=HaltTrigger.DEACTIVATION, scope=f"strategy:{_new_id()}")
        _open_switch(MechanismKind.EVENT_BREAKER)
        _open_switch(MechanismKind.REGIME_GATE)

        self.assertEqual(
            report._declaration_lines(now=NOW, regime=BaseRegime.RANGE.value),
            [
                "停止声明：此刻 4 条在拦（保命档（高波动） 1 条、事件熔断 1 条、"
                "策略停用决策 2 条；按生效中的阶段：箱体震荡）"
            ],
        )

    def test_the_blanket_counts_with_no_switch_to_open(self):
        # `HALT_TRIGGER_SWITCH[BLANKET] = None`：一个开关都不拨，保命档照样算在拦——
        # 「高波动算生效」这句话在日报上就是这个形状。
        _declare(trigger=HaltTrigger.BLANKET, label="保命档（高波动）")

        self.assertEqual(
            report._declaration_lines(now=NOW, regime=BaseRegime.HIGH_VOL.value),
            [
                "停止声明：此刻 1 条在拦（保命档（高波动） 1 条；按生效中的阶段：高波动）"
            ],
        )

    def test_nothing_at_all_is_a_statement_not_a_blank(self):
        self.assertEqual(
            report._declaration_lines(now=NOW, regime=BaseRegime.RANGE.value),
            ["停止声明：此刻没有任何一层在拦（按生效中的阶段：箱体震荡）"],
        )

    def test_cold_start_says_there_is_no_effective_phase(self):
        # `regime` 为空（冷启动）时不能说「按生效中的阶段：（无）」——那读起来像有个阶段
        # 叫「（无）」。这一行要答的是「它依据的是哪条判定」，所以如实说没有。
        self.assertEqual(
            report._declaration_lines(now=NOW, regime=None),
            ["停止声明：此刻没有任何一层在拦（此刻没有生效中的阶段判定）"],
        )

    def test_alive_rows_behind_a_closed_switch_are_reported_as_a_difference(self):
        # 同一批行只拨开关：Shadow 期它们不拦人（与 `query_halt`、下单通路同一个口径），
        # 但「表里躺着、开开关就咬人」必须在日报里看得见——出 Shadow 之前，这是唯一一份
        # 能让人提前看见「它准备拦哪些」的日频文档。
        _declare(trigger=HaltTrigger.DEACTIVATION, scope=f"strategy:{_new_id()}")
        _declare(trigger=HaltTrigger.DEACTIVATION, scope=f"strategy:{_new_id()}")

        self.assertEqual(
            report._declaration_lines(now=NOW, regime=BaseRegime.UPTREND.value),
            [
                "停止声明：此刻没有任何一层在拦（按生效中的阶段：上行趋势）",
                "  另有 2 条活着，开关关着所以不拦",
            ],
        )

        _open_switch(MechanismKind.REGIME_GATE)
        self.assertEqual(
            report._declaration_lines(now=NOW, regime=BaseRegime.UPTREND.value),
            ["停止声明：此刻 2 条在拦（策略停用决策 2 条；按生效中的阶段：上行趋势）"],
        )

    def test_the_section_carries_the_line(self):
        # 接线：这一行真的在 `_section_health` 里。少了这条断言，把那一行 `extend` 删掉
        # 也能让上面所有用例通过——它们调的是 `_declaration_lines` 本身。
        body = report._section_health(
            SYMBOL, {}, {"regime": BaseRegime.RANGE.value}, now=NOW
        )
        self.assertIn(
            "停止声明：此刻没有任何一层在拦（按生效中的阶段：箱体震荡）", body
        )


# --------------------------------------------------------------------------- #
# 第③段末尾：候选事件那一节（含「到期被丢弃」那半）
# --------------------------------------------------------------------------- #


def _candidate(
    *,
    name: str = "某事件",
    status: str = CandidateStatus.PENDING.value,
    raised_at: date | None = None,
    discard_reason: str = "",
    decided_at: datetime | None = None,
) -> CandidateEvent:
    """一条候选。默认是**待确认**的；`discarded` 由调用方点名（状态与原因一起给）。

    `expires_at` 不参与本文件的任何断言（窗口判据读的是 `decided_at` 与丢弃原因），
    所以给一个还在有效期内的值，免得待确认那条走 `describe_candidate` 的 ⚠ 分支。
    """
    return CandidateEvent.objects.create(
        name=name,
        origin=CandidateOrigin.AGENT.value,
        raised_at=raised_at or RUN_DAY - timedelta(days=1),
        raised_by="u-1",
        expires_at=NOW + timedelta(days=13),
        status=status,
        discard_reason=discard_reason,
        decided_at=decided_at,
    )


def _dropped(
    *, name: str = "某事件", raised_days_ago: int = 20, decided_days_ago: int = 1
) -> CandidateEvent:
    """一条**到期未确认、已被清理丢弃**的候选。"""
    return _candidate(
        name=name,
        status=CandidateStatus.DISCARDED.value,
        raised_at=RUN_DAY - timedelta(days=raised_days_ago),
        discard_reason=CANDIDATE_DISCARD_EXPIRED,
        decided_at=NOW - timedelta(days=decided_days_ago),
    )


class TestTheCandidateSection(TestCase):
    """第③段末尾那一节：待确认的那些（要人动手）+ 到期被丢的那些（第 152 条的证据）。

    两半**必须成对**：清理任务（`apps.regime.tasks.expire_candidates`）一跑起来，过期
    候选就从「待人工处置」里消失，只剩这一节说得清「机制提过、没人处置」。少了后半，
    它会连「曾经存在过」都不留。
    """

    def test_a_clean_section_is_the_empty_line_and_nothing_else(self):
        """没有人被丢时**不印**「近 14 天没有丢弃」。

        「清理任务还活着吗」在体检页的「调度表（beat）」那一段有唯一的家；在日报里补一
        句 0，就是给同一个问题造第二个答案。
        """
        self.assertEqual(
            report._candidate_lines(now=NOW), ["待人工处置的候选事件：没有"]
        )

    def test_a_dropped_candidate_is_named_with_when_it_was_raised(self):
        """整行比对：「提出于」是判「最近才衰减」还是「清理坏了一阵子」的分界。

        按丢弃时刻算的窗口会把「很久以前提出、今天才被丢」印得像最近的衰减——而清理
        第一次真正跑起来时，那正是它的形状。
        """
        raised_at = RUN_DAY - timedelta(days=20)
        candidate = _dropped(name="某宏观事件", raised_days_ago=20)

        self.assertEqual(
            report._candidate_lines(now=NOW),
            [
                "待人工处置的候选事件：没有",
                "到期未确认而自动丢弃（近 14 天）：1 条——机制提过、没人处置",
                f"候选 #{candidate.id}「某宏观事件」（{raised_at} 提出）",
            ],
        )

    def test_a_human_rejection_is_not_decay_evidence(self):
        """`rejected` 是有人处置过——把它算进「没人处置」里，这一行就在诬告自己人。"""
        _candidate(
            name="被人否决的",
            status=CandidateStatus.DISCARDED.value,
            discard_reason=CANDIDATE_DISCARD_REJECTED,
            decided_at=NOW - timedelta(days=1),
        )
        self.assertEqual(
            report._candidate_lines(now=NOW), ["待人工处置的候选事件：没有"]
        )

    def test_a_drop_outside_the_window_is_not_counted(self):
        _dropped(decided_days_ago=20)
        self.assertEqual(
            report._candidate_lines(now=NOW), ["待人工处置的候选事件：没有"]
        )

    def test_the_two_halves_share_one_section_with_the_pending_first(self):
        """待办在前、证据在后：先给人要动手的，再给「上一批没人动」的账。"""
        pending = _candidate(name="还没人理的")
        _dropped(name="已经没人理的")

        lines = report._candidate_lines(now=NOW)
        self.assertEqual(
            lines[0],
            "待人工处置的候选事件：1 条（**不产生任何熔断**，转正需人工重新给出时间/档位/作用域）",
        )
        self.assertIn(f"候选 #{pending.id} 还没人理的", lines[1])
        self.assertEqual(
            lines[2], "到期未确认而自动丢弃（近 14 天）：1 条——机制提过、没人处置"
        )

    def test_the_events_section_carries_it(self):
        # 接线：这一节真的在 `_section_events` 里（第③段与 `query_events` 同源，所以
        # 两处一起得到它）。删掉那个 `extend` 能骗过上面所有用例。
        _dropped()
        self.assertIn("到期未确认而自动丢弃", report._section_events(now=NOW))


# --------------------------------------------------------------------------- #
# 第②段：结构化做差 + 渲染
# --------------------------------------------------------------------------- #


class TestTheStructuredChange(SimpleTestCase):
    """做差与渲染都是纯函数：给出的两份 `by_regime` 都是普通字典。"""

    def test_a_new_strategy_in_a_layer_is_a_halt(self):
        items = report.diff_regimes(
            {"downtrend": _layer("s1")}, {"downtrend": _layer("s1", "s2")}
        )
        self.assertEqual([i["kind"] for i in items], ["halt"])
        self.assertEqual([s["strategy_id"] for s in items[0]["strategies"]], ["s2"])

    def test_a_strategy_leaving_a_layer_is_a_release(self):
        # 「将解除」是第②段的一半，而它在决策表上永远不可能出现（那边的行只增不减）。
        items = report.diff_regimes(
            {"downtrend": _layer("s1", "s2")}, {"downtrend": _layer("s1")}
        )
        self.assertEqual([i["kind"] for i in items], ["release"])
        self.assertEqual([s["strategy_id"] for s in items[0]["strategies"]], ["s2"])

    def test_moving_across_layers_is_two_facts_not_one(self):
        # 「它其实换了个阶段继续被停着」正是读的人要的判断，报成一件会掩盖它。
        items = report.diff_regimes(
            {"downtrend": _layer("s1")}, {"high_vol": _layer("s1")}
        )
        self.assertEqual(
            {(i["kind"], i["regime"]) for i in items},
            {("release", "downtrend"), ("halt", "high_vol")},
        )

    def test_no_movement_is_no_items(self):
        same = {"downtrend": _layer("s1", "s2")}
        self.assertEqual(
            report.diff_regimes(same, {"downtrend": _layer("s2", "s1")}), []
        )

    def test_headline_names_the_layer_not_just_the_count(self):
        items = report.diff_regimes({}, {"downtrend": _layer("s1", "s2")})
        headline = report._change_headline(items)
        self.assertIn("将新停用「下行趋势」2 个", headline)

    def test_the_headline_does_not_count_the_blanket_layer(self):
        # 保命档答不出一份策略清单（`_blanket_item`），所以标题里也不出现「N 个」：
        # 「0 个」会被读成「这条不计」，而它恰恰是那天最重的一条。
        self.assertEqual(
            report._change_headline([report._blanket_item("halt")]),
            "将新停用「高波动」档",
        )

    def test_the_blanket_line_is_kept_for_a_user_who_has_something_else_to_see(self):
        # 保命档不按策略裁——它答不出「跟你有关的那几条」。但**段可见性它不是例外**：
        # 只要这个人还在被机制管着（`strategy_ids` 非空），这条就得出现，否则「全场将
        # 停手」只会推给恰好有一条策略变动的人。
        change = {"status": "diff", "items": [report._blanket_item("halt")]}
        self.assertEqual(
            report.render_change(change, strategy_ids={"s9"}),
            "将新停用「高波动」档（全市场一律，与证据无关）\n（以上自明日 08:00 起生效）",
        )

    def test_render_change_speaks_in_the_future_tense(self):
        # 预告口径：这些变化**将在明日 08:00 生效**，所以写「将停用」，不写「已停用」。
        change = {
            "status": "diff",
            "note": "与上一份日报（2026-09-21）相比：…",
            "items": [
                {
                    "kind": "halt",
                    "regime": "downtrend",
                    "regime_display": "下行趋势",
                    "strategies": [
                        {
                            "strategy_id": "s1",
                            "name": "甲",
                            "running": False,
                            "exempt": True,
                        }
                    ],
                }
            ],
        }
        body = report.render_change(change)
        self.assertIn("将新停用「下行趋势」：甲（人工豁免中，未在跑）", body)
        self.assertIn("（以上自明日 08:00 起生效）", body)

    def test_render_change_passes_the_notes_through_verbatim(self):
        for status, note in (
            ("first", "无昨日日报可比对，这是首次"),
            ("blocked", "本轮推导未产出停用建议（冷启动：…），故本日不做今昨比对"),
            ("unchanged", "与 2026-09-21 的日报相比无变化"),
        ):
            self.assertEqual(
                report.render_change({"status": status, "note": note, "items": []}),
                note,
            )
        self.assertEqual(report.render_change(None), "")

    def test_render_change_with_no_surviving_entry_returns_nothing(self):
        change = {
            "status": "diff",
            "items": [
                {
                    "kind": "halt",
                    "regime": "downtrend",
                    "regime_display": "下行趋势",
                    "strategies": [{"strategy_id": "s1", "name": "甲"}],
                }
            ],
        }
        self.assertEqual(report.render_change(change, strategy_ids={"s9"}), "")

    def test_entry_label_falls_back_to_the_id_prefix(self):
        self.assertEqual(
            report._entry_label({"strategy_id": "abcdefghijkl", "name": ""}), "abcdefgh"
        )


class TestTheWriteGate(TestCase):
    """写不写、写几次。截止时刻与「一天一条」都在这里。"""

    def test_waiting_reports_do_not_touch_the_database(self):
        summary = report.write_daily_report(
            skipped_payload(), derivation(), {}, now=BEFORE_DEADLINE
        )
        self.assertEqual(summary["outcome"], report.OUTCOME_WAITING)
        self.assertFalse(summary["written"])
        self.assertFalse(DailyReport.objects.exists())

    def test_waiting_is_not_an_error(self):
        # 不是失败，是「再等等」——心跳 5 分钟一轮，多数 tick 在白天根本走不到写。
        summary = report.write_daily_report(
            skipped_payload(), derivation(), {}, now=BEFORE_DEADLINE
        )
        self.assertNotIn("error", summary)
        self.assertTrue(summary["note"])


# --------------------------------------------------------------------------- #
# 裁剪：谁看到哪一节
# --------------------------------------------------------------------------- #


class TestCropForOneUser(SimpleTestCase):
    """`crop_for` 只读字段与 `landscape`，不查库——所以它拿得住「未落库的一行」。"""

    def test_the_change_section_is_cropped_to_the_users_strategies(self):
        sections = report.crop_for(_report(), {"s1"})
        body = sections[report.SECTION_CHANGE]
        self.assertIn("甲", body)
        self.assertNotIn("乙", body)

    def test_a_user_with_nothing_in_this_change_sees_no_section_at_all(self):
        # **整节不出现**，而不是出现一节写着「无变化」——后者会被读成「机制看了我的策略，
        # 说没事」，而事实是这一节里压根没有他的策略。
        sections = report.crop_for(_report(), {"s9"})
        self.assertNotIn(report.SECTION_CHANGE, sections)

    def test_a_user_with_no_active_session_sees_no_section_at_all(self):
        sections = report.crop_for(_report(), set())
        self.assertNotIn(report.SECTION_CHANGE, sections)

    def test_a_blanket_only_change_follows_the_same_visibility_rule(self):
        # 保命档不按策略裁（它没有清单可裁，`_blanket_item`），但**段可见性它不是例外**：
        # `strategy_ids` 是空集时整节省掉。给一个机制压根没在管的人推「全场将停手」，
        # 下一次他就开始忽略日报了——而「必发」正是靠这个被读的。
        landscape = {
            report.SECTION_CHANGE: {
                "status": "diff",
                "items": [report._blanket_item("halt")],
            }
        }
        self.assertNotIn(
            report.SECTION_CHANGE,
            report.crop_for(_report(landscape=landscape), set()),
        )
        self.assertIn(
            "高波动",
            report.crop_for(_report(landscape=landscape), {"s9"})[
                report.SECTION_CHANGE
            ],
        )

    def test_the_other_four_sections_are_untouched(self):
        sections = report.crop_for(_report(), {"s1"})
        for name in report.SECTIONS:
            if name == report.SECTION_CHANGE:
                continue
            self.assertEqual(sections[name], _report().sections[name])

    def test_cropping_does_not_mutate_the_stored_sections(self):
        # 落库的那一份是「机制说了什么」的存档，裁剪只发生在渲染给某个人之前。
        fresh = _report()
        before = dict(fresh.sections)
        report.crop_for(fresh, {"s1"})
        self.assertEqual(fresh.sections, before)


# --------------------------------------------------------------------------- #
# 落库（真库）
# --------------------------------------------------------------------------- #


class TestTheWrittenRow(TestCase):
    def test_it_inlines_the_three_values_and_points_at_the_judgement(self):
        summary = report.write_daily_report(
            concluded_payload(regime="range", effective_regime="downtrend"),
            derivation(suggestion(_new_id())),
            {},
            now=NOW,
        )
        self.assertTrue(summary["written"])
        self.assertEqual(summary["outcome"], report.OUTCOME_CREATED)

        row = DailyReport.objects.get(symbol=SYMBOL, run_day=RUN_DAY)
        self.assertEqual(row.base_regime, "range")
        self.assertEqual(row.effective_regime, "downtrend")
        self.assertEqual(row.escalation, "")
        self.assertFalse(row.judgement_missing)
        # 外键指向**抄来的那一行**（`(symbol, effective_at)` 定位）。指向
        # `current_judgement()`（今天生效的那条）会天天比内联三值早一天，而那种错位处处自洽。
        self.assertIsNotNone(row.judgement)
        self.assertEqual(
            row.judgement.effective_at, business_midnight(RUN_DAY + timedelta(days=1))
        )
        self.assertEqual(set(row.sections), set(report.SECTIONS))

    def test_the_first_section_reports_the_next_day_effective_moment(self):
        report.write_daily_report(concluded_payload(), derivation(), {}, now=NOW)
        row = DailyReport.objects.get(symbol=SYMBOL, run_day=RUN_DAY)
        body = row.sections[report.SECTION_TODAY]
        # 「自明日 08:00 起生效」那句话的字面出处。
        self.assertIn(
            report.format_business(business_midnight(RUN_DAY + timedelta(days=1))), body
        )

    def test_it_snapshots_the_days_suggestions(self):
        # Q3：`DeactivationDecision` 的行是原地更新的，所以「昨天说了什么」只能靠这份快照，
        # 缺了它第②段做差永远得零——而那看起来像「机制很稳定」。
        sid = _new_id()
        report.write_daily_report(
            concluded_payload(), derivation(suggestion(sid, "downtrend")), {}, now=NOW
        )
        row = DailyReport.objects.get(symbol=SYMBOL, run_day=RUN_DAY)
        landscape = row.landscape
        self.assertEqual(landscape["count"], 1)
        self.assertEqual(
            [e["strategy_id"] for e in landscape["by_regime"]["downtrend"]], [sid]
        )
        self.assertEqual(landscape[report.SECTION_CHANGE]["status"], "first")
        self.assertIn(
            "无昨日日报可比对，这是首次", landscape[report.SECTION_CHANGE]["note"]
        )

    def test_a_strategy_id_that_no_longer_resolves_is_tolerated(self):
        # 策略行被删了（幽灵策略清理、或这条建议来自已经不存在的一代）而建议清单还留着
        # 那个 id 时，日报不能因为「查不到名字」就写不出来——名字查不到就留空，渲染那里
        # 还有 id 前缀兜底（`_entry_label`）。
        missing = _new_id()
        self.assertEqual(report._strategy_names([missing]), {})
        report.write_daily_report(
            concluded_payload(),
            derivation(suggestion(missing, "downtrend")),
            {},
            now=NOW,
        )
        row = DailyReport.objects.get(symbol=SYMBOL, run_day=RUN_DAY)
        self.assertEqual(row.landscape["by_regime"]["downtrend"][0]["name"], "")

    def test_the_second_call_keeps_the_first_row(self):
        # 一天一份。心跳 5 分钟一轮，绝大多数 tick 走到这里只确认已有那一份。
        report.write_daily_report(concluded_payload(), derivation(), {}, now=NOW)
        # 第二次刻意换一个阶段，好让「没被改写」这件事有区别可验。`with_record=False`：
        # 判定行是 `(symbol, effective_at)` 唯一的，心跳的第二轮走 `get_or_create` 拿到的是
        # 同一行；`concluded_payload` 的工厂用的是 `create()`，再建一次会撞唯一约束。
        summary = report.write_daily_report(
            concluded_payload(regime="uptrend", with_record=False),
            derivation(),
            {},
            now=NOW,
        )
        self.assertEqual(summary["outcome"], report.OUTCOME_KEPT)
        self.assertFalse(summary["written"])
        row = DailyReport.objects.get(symbol=SYMBOL, run_day=RUN_DAY)
        # 第一次那份写的是 `concluded_payload()` 的默认阶段（下行趋势），第二次是上行趋势。
        self.assertEqual(row.base_regime, "downtrend")  # 第一次写下的那份没被改写
        self.assertEqual(DailyReport.objects.count(), 1)

    def test_a_conclusion_less_day_still_ships_a_report(self):
        # 数据类失败照发。「沉默必须能被识别为异常」，而一份永不出现的日报与
        # 「今天没什么事」在聊天窗口里长得一样。
        summary = report.write_daily_report(
            skipped_payload("stale_candles"), derivation(), {}, now=AFTER_DEADLINE
        )
        self.assertEqual(summary["outcome"], report.OUTCOME_CREATED)
        row = DailyReport.objects.get(symbol=SYMBOL, run_day=RUN_DAY)
        self.assertTrue(row.judgement_missing)
        self.assertIsNone(row.judgement)
        body = row.sections[report.SECTION_TODAY]
        self.assertIn("今日判定缺失", body)
        self.assertIn("处于保持的上一有效状态", body)
        self.assertIn("数据管道比日界慢", body)
        self.assertIn("库中还没有任何生效过的判定（冷启动）", body)

    def test_the_change_section_reads_yesterdays_report(self):
        DailyReport.objects.create(
            symbol=SYMBOL,
            run_day=RUN_DAY - timedelta(days=1),
            landscape={"by_regime": {"downtrend": _layer("s_old")}},
        )
        report.write_daily_report(
            concluded_payload(),
            derivation(suggestion(_new_id(), "downtrend")),
            {},
            now=NOW,
        )
        row = DailyReport.objects.get(symbol=SYMBOL, run_day=RUN_DAY)
        change = row.landscape[report.SECTION_CHANGE]
        self.assertEqual(change["status"], "diff")
        self.assertEqual(
            {(i["kind"], i["regime"]) for i in change["items"]},
            {("halt", "downtrend"), ("release", "downtrend")},
        )
        body = report.render_change(change)
        self.assertIn("将新停用「下行趋势」", body)
        self.assertIn("将解除「下行趋势」", body)

    def test_a_blocked_derivation_does_not_read_as_all_released(self):
        # 本轮的推导没产出建议时今天的集合是**空**的，直接做差会把「这轮没说话」读成
        # 「把这些全解除了」。
        DailyReport.objects.create(
            symbol=SYMBOL,
            run_day=RUN_DAY - timedelta(days=1),
            landscape={"by_regime": {"downtrend": _layer("s1")}},
        )
        report.write_daily_report(
            concluded_payload(), derivation(skipped="cold_start"), {}, now=NOW
        )
        row = DailyReport.objects.get(symbol=SYMBOL, run_day=RUN_DAY)
        change = row.landscape[report.SECTION_CHANGE]
        self.assertEqual(change["status"], "blocked")
        self.assertEqual(change["items"], [])
        self.assertIn("冷启动", change["note"])

    def test_a_blocked_baseline_does_not_manufacture_halts(self):
        # 上一份日报本身是阻塞日时，它那份空集合不代表「那天没有建议」——拿它当基准会
        # 凭空造出一堆「将停用」。
        DailyReport.objects.create(
            symbol=SYMBOL,
            run_day=RUN_DAY - timedelta(days=1),
            landscape={"blocked": "cold_start", "by_regime": {}},
        )
        report.write_daily_report(
            concluded_payload(), derivation(suggestion(_new_id())), {}, now=NOW
        )
        row = DailyReport.objects.get(symbol=SYMBOL, run_day=RUN_DAY)
        change = row.landscape[report.SECTION_CHANGE]
        self.assertEqual(change["status"], "blocked")
        self.assertEqual(change["items"], [])

    def test_the_baseline_is_the_latest_report_not_strictly_yesterday(self):
        # 昨天那一行缺失（比如任务停了一天）时，基准该退到**最近的那一份**并说明距今几天。
        DailyReport.objects.create(
            symbol=SYMBOL,
            run_day=RUN_DAY - timedelta(days=3),
            landscape={"by_regime": {"downtrend": _layer("s_old")}},
        )
        report.write_daily_report(
            concluded_payload(),
            derivation(suggestion(_new_id(), "downtrend")),
            {},
            now=NOW,
        )
        row = DailyReport.objects.get(symbol=SYMBOL, run_day=RUN_DAY)
        change = row.landscape[report.SECTION_CHANGE]
        self.assertEqual(
            change["baseline_run_day"], (RUN_DAY - timedelta(days=3)).isoformat()
        )
        self.assertEqual(change["status"], "diff")


class TestTheBlanketLayerInTheChange(TestCase):
    """保命档那一层怎么进第②段（Q10）。**这个类落库**，而这是刻意的。

    它钉的是 `_change` 与「上一份日报」一起求值的结果，而「上一份说了什么」只在
    `DailyReport` 表里。**不塞进 `TestTheStructuredChange`**：那个类是 `SimpleTestCase`，
    而「这几段不落库」正是靠它强制的（模块 docstring）——放一条要读库的用例进去，那份
    强制就当场作废，而且是静悄悄地作废。

    保命档**不在 `diff_regimes` 的坐标系里**：`by_regime` 的键是**建议清单里的阶段**，
    而保命档是阶段本身的性质（池化对 `high_vol` 那一格给 `blanket`，`deactivation._verdict`
    映成 `BLANKET`，而 `BLANKET ∉ targets`）。所以抬升日做差做出来的是**一片「将解除」**
    ——读起来正是「可以交易了」，而事实是此刻谁都不该开新仓。压制因此落在 `_change` 里，
    **不在 `diff_regimes` 里做**：后者「跨层搬动是两件事」的那条语义
    （`test_moving_across_layers_is_two_facts_not_one`）是它自己的契约，逐字未动。
    """

    def _yesterday(
        self, by_regime: dict, *, regime: str | None = None, blocked: str | None = None
    ) -> None:
        """昨天那一份日报。``regime=None`` 造的是**没有 ``regime`` 键**的旧格式行。"""
        landscape: dict = {"by_regime": by_regime}
        if regime is not None:
            landscape["regime"] = regime
        if blocked is not None:
            landscape["blocked"] = blocked
        DailyReport.objects.create(
            symbol=SYMBOL, run_day=RUN_DAY - timedelta(days=1), landscape=landscape
        )

    def _change_of_today(
        self, *, today_regime: str, by_regime: dict, today_blocked: str = ""
    ) -> dict:
        return report._change(
            by_regime,
            today_blocked=today_blocked,
            today_regime=today_regime,
            symbol=SYMBOL,
            run_day=RUN_DAY,
        )

    def test_an_escalation_day_drops_the_releases_and_adds_one_blanket_halt(self):
        # 抬升日：今天进了保命档，于是当天的建议清单里**各层的策略全部消失**（清单只按新
        # 阶段产出），而 `by_regime` 里连 `high_vol` 这个键都不会有。只做差会渲染成一片
        # 「将解除〈某层〉」，那正是「可以交易了」——与事实相反。
        self._yesterday({"downtrend": _layer("s_old")}, regime="downtrend")
        change = self._change_of_today(today_regime="high_vol", by_regime={})

        self.assertEqual(change["status"], "diff")
        self.assertEqual(len(change["items"]), 1)
        item = change["items"][0]
        self.assertEqual(item["kind"], "halt")
        self.assertTrue(item["blanket"])
        # 空清单不是「暂时填不上」：保命档答不出一份策略清单（`_blanket_item`）。
        self.assertEqual(item["strategies"], [])
        self.assertIn("将新停用「高波动」档", change["note"])

    def test_a_de_escalation_day_keeps_the_halts_and_appends_one_blanket_release(self):
        # 降级日反过来：原有的变化照报，另补一条「已出保命档」。丢掉 diff 的那一侧就错了
        # ——那会把「同时出档」说成「只出档」，而两件事的处置完全不同。
        self._yesterday({}, regime="high_vol")
        change = self._change_of_today(
            today_regime="downtrend",
            by_regime={"downtrend": _layer("s_old", "s_new")},
        )

        self.assertEqual(
            [(i["kind"], bool(i.get("blanket"))) for i in change["items"]],
            [("halt", False), ("release", True)],
        )
        self.assertIn("将新停用「下行趋势」2 个", change["note"])
        self.assertIn("将解除「高波动」档", change["note"])

    def test_a_blanket_only_move_is_a_diff_not_an_unchanged(self):
        # **压制排在判空之前**（Q4）。只有保命档动了的那些天，做差做完正好是空的：先判空
        # 就会把「今天全场停手」报成「无变化」——这两句对读的人是天差地别的两件事，
        # 而后者看起来完全正常。
        same = {"downtrend": _layer("s1")}
        self._yesterday(same, regime="downtrend")
        change = self._change_of_today(today_regime="high_vol", by_regime=same)

        self.assertEqual(report.diff_regimes(same, same), [])  # 做差确实是空的
        self.assertEqual(change["status"], "diff")
        self.assertEqual(len(change["items"]), 1)

    def test_the_three_early_returns_are_not_looked_through(self):
        """三条早退路径说的是「今天不比」，保命档的补写绝不越过它们（Q5）。

        越过「首次」的表现是：机制第一次跑就说「将新停用「高波动」档」，而它连昨天说过
        什么都还不知道。
        """
        first = self._change_of_today(today_regime="high_vol", by_regime={})
        self.assertEqual((first["status"], first["items"]), ("first", []))

        self._yesterday({}, regime="downtrend")
        blocked = self._change_of_today(
            today_regime="high_vol", by_regime={}, today_blocked="cold_start"
        )
        self.assertEqual((blocked["status"], blocked["items"]), ("blocked", []))

    def test_a_blocked_baseline_is_not_looked_through_either(self):
        # 上一份是阻塞日、而它那天恰好判的是高波动：拿它当基准会凭空补一条「将解除
        # 「高波动」档」。它那份清单是空的，不代表「那天在保命档里没事」。
        self._yesterday({}, regime="high_vol", blocked="cold_start")
        change = self._change_of_today(today_regime="downtrend", by_regime={})

        self.assertEqual((change["status"], change["items"]), ("blocked", []))

    def test_a_previous_report_without_a_regime_is_not_read_as_blanket(self):
        # 旧格式行（`regime` 键是后来才加的）没有那个键 ⇒ 当作**不是**保命档。反方向
        # （取不到就当作是）会在升级后的第一份日报上凭空写一条「将解除「高波动」档」。
        self._yesterday({"downtrend": _layer("s_old")})
        change = self._change_of_today(
            today_regime="downtrend", by_regime={"downtrend": _layer("s_old")}
        )

        self.assertEqual((change["status"], change["items"]), ("unchanged", []))

    def test_the_suppression_lives_in_the_change_not_in_the_diff(self):
        # 分界线的钉子：同一组输入，`diff_regimes` 照旧给出跨层的两件事（release），
        # `_change` 才把它丢掉。压制写进 `diff_regimes` 的话，`test_moving_across_layers_
        # is_two_facts_not_one` 那条契约就没了，而它的失效表现是「策略换了个阶段继续被
        # 停着」被报成「它被解除了」。
        prev = {"downtrend": _layer("s_old")}
        self.assertEqual(
            [i["kind"] for i in report.diff_regimes(prev, {})], ["release"]
        )

        self._yesterday(prev, regime="downtrend")
        change = self._change_of_today(today_regime="high_vol", by_regime={})

        self.assertEqual([i["kind"] for i in change["items"]], ["halt"])

    def test_the_archived_section_keeps_the_blanket_line(self):
        # 存档那一份是**未裁剪**的（`_compose` 调 `render_change` 不传 `strategy_ids`），
        # 所以保命档那条必须在里面——它是这天最重的一条，而「按人裁」是渲染时的事，
        # 不该影响机制说了什么的存档。
        self._yesterday({"downtrend": _layer("s_old")}, regime="downtrend")
        report.write_daily_report(
            concluded_payload(effective_regime="high_vol"),
            derivation(),
            {},
            now=NOW,
        )
        row = DailyReport.objects.get(symbol=SYMBOL, run_day=RUN_DAY)
        body = row.sections[report.SECTION_CHANGE]

        self.assertIn("将新停用「高波动」档（全市场一律，与证据无关）", body)
        self.assertNotIn("将解除", body)


# --------------------------------------------------------------------------- #
# 第④段里「出 Shadow 到期」那一行 / 自熔断那一行（出 Shadow 那个单元）
# --------------------------------------------------------------------------- #


def _started_shadow(days_ago: int) -> RegimeJudgement:
    """把 Shadow 的起算日放到「RUN_DAY 往前 `days_ago` 天」——即已跑 `days_ago + 1` 天。

    **必须是含资讯结论的判定**（`news_ref.status` 为 `ok`）：起算日取的是第一条成功的
    资讯判定，不是第一条判定。`concluded_payload()` 造的那条没有 `news_ref`，所以它起不了
    算——那正好是本仓今天的形状（判定跑过、资讯没接上）。
    """
    run_day = RUN_DAY - timedelta(days=days_ago)
    return RegimeJudgement.objects.create(
        symbol=SYMBOL,
        attribute_date=run_day - timedelta(days=1),
        effective_at=business_midnight(run_day + timedelta(days=1)),
        base_regime=BaseRegime.RANGE.value,
        escalation="",
        effective_regime=BaseRegime.RANGE.value,
        news_ref={"status": "ok"},
    )


def _back(kind: ActorKind) -> RegimeMechanismSwitch:
    """一条「退回 Shadow」。`actor_kind` 是人工与自熔断的唯一分界。"""
    return RegimeMechanismSwitch.objects.create(
        kind=MechanismKind.MECHANISM.value,
        from_mode=MechanismMode.EXECUTING.value,
        to_mode=MechanismMode.SHADOW.value,
        at=NOW - timedelta(hours=3),
        actor_kind=kind.value,
        actor_name="alice" if kind is ActorKind.CHAT else "regime.self_fuse",
        reason="测试",
    )


class TestTheExpiryLine(TestCase):
    """第④段在**到期之后**每天加一行「出 Shadow」（第 159 条的兜底需要一个人能看见它）。

    这一行的位置与缺席都是要点：到期前天天报一句「还没到期」是二十天的噪声（会训练人跳过
    第④段），而到期之后它必须紧跟在「机制当前档」下面——离了半个屏幕就会被读成别的东西。
    """

    def health(self) -> str:
        return report._section_health(
            SYMBOL, {}, {"regime": BaseRegime.RANGE.value}, now=NOW
        )

    def test_nothing_at_all_is_printed_before_the_deadline(self):
        """本仓今天就是这个形状：一条判定都没有。到期那一行不该出现。

        锚用「/regime mech」而不是「出 Shadow」——**后者是那个档位自己的名字**
        （`MechanismKind.MECHANISM` 的 display 就叫「出 Shadow（判定 + 切片 + 自动停用）」），
        三个开关那一段里天天都有它。
        """
        self.assertNotIn("/regime mech", self.health())

    def test_a_running_shadow_period_is_not_nagged_about(self):
        _started_shadow(days_ago=2)
        self.assertNotIn("/regime mech", self.health(), "还没到期就不该天天报一句")

    def test_the_cap_line_appears_after_the_deadline(self):
        """兜底到期：日子满了、窗口一个都没有。这一句必须带着「事件窗口数不足」。"""
        _started_shadow(days_ago=config.SHADOW.expiry_cap_days - 1)
        body = self.health()
        self.assertIn("出 Shadow", body)
        self.assertIn("兜底", body)
        self.assertIn("事件窗口数不足", body)
        self.assertIn("/regime mech", body, "要指出下一步该敲哪条命令")

    def test_it_sits_right_below_the_current_mode_line(self):
        _started_shadow(days_ago=config.SHADOW.expiry_cap_days - 1)
        lines = self.health().splitlines()
        at = next(i for i, line in enumerate(lines) if line.startswith("机制当前档："))
        self.assertTrue(lines[at + 1].startswith("出 Shadow"), lines[at : at + 2])
        # 而且要在三个开关之前：它说的是「机制整体」那一档的事，不是某一个开关的事。
        switches = next(
            i for i, line in enumerate(lines) if line.startswith("三个开关")
        )
        self.assertLess(at, switches)


class TestTheSelfFuseLine(TestCase):
    """「自熔断：…」那一行必须把**人工退回**与**自熔断**分开（出 Shadow 那个单元）。

    加 `/regime mech back` 之前，`to_mode=shadow` 的行只可能来自自熔断，所以「有一条退回
    Shadow 的行」与「机制自熔断过」是同一句话。有了人工退回之后它们不是了——而报错的
    方向很坏：一次人工退回会被日报报成「机制自熔断过」，而自熔断是「机制不可信」的结论，
    接下来会被当成重新上线的依据。
    """

    def health(self) -> str:
        return report._section_health(
            SYMBOL, {}, {"regime": BaseRegime.RANGE.value}, now=NOW
        )

    def test_a_human_back_is_not_a_self_fuse(self):
        _back(ActorKind.CHAT)
        body = self.health()
        self.assertIn("自熔断：未触发过", body)
        self.assertIn("人工", body)
        self.assertIn("alice", body, "谁退回去的必须写在日报里")

    def test_a_task_back_is_the_self_fuse(self):
        _back(ActorKind.TASK)
        body = self.health()
        self.assertIn("自熔断：触发过", body)
        self.assertIn("regime.self_fuse", body)

    def test_no_back_record_at_all_says_so(self):
        self.assertIn(
            "自熔断：未触发过（切换流水里没有一条退回 Shadow 的记录）", self.health()
        )

    def test_a_row_from_another_switch_is_not_a_back_at_all(self):
        """事件熔断被关掉写的也是 `to_mode=shadow` 的行——那不是机制退回过 Shadow。"""
        RegimeMechanismSwitch.objects.create(
            kind=MechanismKind.EVENT_BREAKER.value,
            from_mode=MechanismMode.EXECUTING.value,
            to_mode=MechanismMode.SHADOW.value,
            at=NOW - timedelta(hours=3),
            actor_kind=ActorKind.CHAT.value,
            actor_name="alice",
            reason="关掉事件熔断",
        )
        self.assertIn(
            "自熔断：未触发过（切换流水里没有一条退回 Shadow 的记录）", self.health()
        )


class TestTheHealthSection(TestCase):
    def test_it_reports_never_judged_and_never_switched(self):
        report.write_daily_report(concluded_payload(), derivation(), {}, now=NOW)
        row = DailyReport.objects.get(symbol=SYMBOL, run_day=RUN_DAY)
        body = row.sections[report.SECTION_HEALTH]
        self.assertIn("上次判定成功：", body)
        self.assertIn("自熔断：未触发过", body)
        self.assertIn("本轮判定：已出结论", body)
        self.assertIn("事件库：从未录入过任何事件", body)

    def test_it_reports_the_derivations_blocked_reason(self):
        report.write_daily_report(
            concluded_payload(), derivation(skipped="stale_state"), {}, now=NOW
        )
        row = DailyReport.objects.get(symbol=SYMBOL, run_day=RUN_DAY)
        body = row.sections[report.SECTION_HEALTH]
        self.assertIn("状态过期", body)
        self.assertIn("本轮推导：", body)

    def test_it_counts_the_managed_set(self):
        report.write_daily_report(
            concluded_payload(),
            {
                "symbol": SYMBOL,
                "skipped": None,
                "note": "",
                "suggestions": [suggestion(_new_id())],
                "managed": 4,
                "running": 2,
                "unmanaged": ["ghost-a"],
                "unresolved": ["ghost-b", "ghost-c"],
                "needs_review": 1,
                "missing_cells": 2,
            },
            {},
            now=NOW,
        )
        row = DailyReport.objects.get(symbol=SYMBOL, run_day=RUN_DAY)
        body = row.sections[report.SECTION_HEALTH]
        self.assertIn("被管策略：4 个（其中在跑 2 个）", body)
        self.assertIn("未归类 1 个（不产生任何动作）", body)
        self.assertIn("实现类解析不到 2 个", body)
        self.assertIn("待人工复核：1 条", body)
        self.assertIn("当前代缺格：2 条", body)


class TestTheDeliverySection(TestCase):
    """第⑤段读的是**昨天那一行自己**，而且要按三种收场分得开。

    **它是回复式的**——只在今天这份投得出去时才说得到人，所以它报什么都救不了连续失败；
    第⑤段与看门狗是两条路径，这里钉的是「第⑤段如实说」，看门狗自己由
    `test_delivery.py` 盯。
    """

    def _yesterday(self, **over) -> DailyReport:
        fields = {
            "symbol": SYMBOL,
            "run_day": RUN_DAY - timedelta(days=1),
            "note": "昨天那份",
        }
        fields.update(over)
        return DailyReport.objects.create(**fields)

    def _body(self) -> str:
        report.write_daily_report(concluded_payload(), derivation(), {}, now=NOW)
        row = DailyReport.objects.get(symbol=SYMBOL, run_day=RUN_DAY)
        return row.sections[report.SECTION_DELIVERY]

    def test_a_delivered_yesterday_is_reported_as_delivered(self):
        self._yesterday(
            delivery={"u1": {"ok": True, "at": "", "error": ""}},
            delivery_attempts=2,
            delivered_at=NOW - timedelta(days=1),
        )
        body = self._body()
        self.assertIn("投递成功于", body)
        self.assertIn("共 1 人，用了 2 轮", body)
        self.assertNotIn("未投递成功", body)

    def test_an_empty_audience_counts_as_not_delivered(self):
        # **空集不能算成功**：一份永远显示「已投递」的投递报告，与一个哑掉却从不报警的
        # 看门狗是同一类东西。
        self._yesterday(delivery={}, delivery_attempts=0)
        body = self._body()
        self.assertIn("无处可投", body)
        self.assertIn("按「没投出去」记", body)

    def test_an_undelivered_yesterday_points_at_the_watchdog(self):
        self._yesterday(
            delivery={"u1": {"ok": False, "at": "", "error": "出站通知口返回未送达"}},
            delivery_attempts=7,
            delivery_error="1/1 人未送达：出站通知口返回未送达",
        )
        body = self._body()
        self.assertIn("至今未投递成功", body)
        self.assertIn("已尝试 7 轮", body)
        self.assertIn("出站通知口返回未送达", body)
        # 它必须说清「这句话不该由我来发现」——否则读者会以为第⑤段就是那条告警通路。
        self.assertIn("独立的投递看门狗", body)

    def test_a_missing_yesterday_is_called_out_as_a_generation_problem(self):
        body = self._body()
        self.assertIn("没有日报可查", body)
        self.assertIn("生成环节的问题", body)


class TestTheAudienceQuery(TestCase):
    """受众是活跃会话，不是策略的 `is_active`（CONTEXT.md 第 106 条是另一件事）。"""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.user = User.objects.create_user(
            email="report@test.local", username="report", password="pw12345"
        )
        cls.other = User.objects.create_user(
            email="report-other@test.local", username="reportother", password="pw12345"
        )
        cls.running = Strategy.objects.create(name="Running", code_path="/t/r.py")
        cls.stopped = Strategy.objects.create(name="Stopped", code_path="/t/s.py")
        cls.retired = Strategy.objects.create(
            name="Retired", code_path="/t/x.py", is_active=False
        )

    def _session(self, user, strategy, status):
        return LiveSession.objects.create(
            user=user,
            strategy=strategy,
            symbol="BTC/USDT",
            mode="paper",
            status=status,
            initial_capital=Decimal("10000.00"),
        )

    def test_only_active_sessions_count(self):
        self._session(self.user, self.running, "running")
        self._session(self.user, self.stopped, "stopped")
        self.assertEqual(
            report.user_affected_strategy_ids(self.user.id), {str(self.running.id)}
        )

    def test_the_strategys_own_is_active_flag_is_irrelevant(self):
        # `is_active=False` 是「已退役」，但会话还在跑就仍然受影响——两件事。
        self._session(self.user, self.retired, "paused")
        self.assertEqual(
            report.user_affected_strategy_ids(self.user.id), {str(self.retired.id)}
        )

    def test_another_users_sessions_do_not_leak_in(self):
        self._session(self.other, self.running, "running")
        self.assertEqual(report.user_affected_strategy_ids(self.user.id), set())
