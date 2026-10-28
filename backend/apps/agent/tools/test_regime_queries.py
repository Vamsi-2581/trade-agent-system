"""行情阶段机制的四个只读工具（第①段单元 8v）。

`test_report.py` 钉的是「日报写成什么样」；这个文件钉的是**同一个事实的第二个入口
说的是不是同一句话**。CONTEXT.md 第 183 条的正文就是这两条：

1. **边界跟着领域对象走**——四个对象回答四个不同问题，没有聚合的「机制状态」工具。
   下面 `TestTheBreak` 用签名把这条钉死：三个工具不吃 `user_id`，只有
   `query_deactivation_decisions` 吃。**破例不写在测试里，就会被下一次「统一一下」
   改回去**，而改回去的表现是用户看到一个按人裁剪过的「系统级判定」。
2. **措辞必须与日报一致**——`query_events` 与日报第③段、`query_regime` 的待生效组与
   日报第①段，都必须是**同一份渲染代码**。这是 `TestQueryEvents` 那条逐字相等的断言
   存在的全部理由：不是「尽量一致」，是「同一份代码」。

## 为什么打 `_render` 而不是 `execute`

`db_async` 走 `thread_sensitive=False`，在独立线程里取连接——`TestCase` 的事务在那个
线程里看不见（`tools/test_exchange_account.py` 常年红灯正是这个原因，与本模块无关）。
所以取数 + 渲染全在同步的 `_render` 里，测试直接打它。异步的 `execute` 只留两条**不碰
数据库**的用例（身份缺失、身份解析），它们走的是 `db_async` 之前的早退分支。

## 时刻是冻住的

工具自称的「此刻」全部来自 `timezone.now()`。不冻住的话，「生效中」与「待生效」的归属
会随跑测试的钟点漂移——这类用例会在某天早上莫名其妙地翻面。所以统一 patch
`regime_queries.timezone.now`，把 NOW 钉在北京 2026-09-23 12:00。
"""

from __future__ import annotations

import asyncio
import inspect
import uuid
from datetime import date, datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.agent.tools.base import ToolRegistry
from apps.agent.tools.regime_queries import (
    QueryDeactivationDecisionsTool,
    QueryEventsTool,
    QueryHaltTool,
    QueryRegimeTool,
)
from apps.regime import config, halt, halt_sync, report
from apps.regime.models import (
    ActorKind,
    CandidateEvent,
    CandidateOrigin,
    CandidateStatus,
    DeactivationDecision,
    DeactivationExemption,
    DecisionStatus,
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
    business_midnight,
)
from apps.regime.quant import BaseRegime
from apps.regime.tests.test_shadow import RUN_DAY, SYMBOL
from apps.trading.models import LiveSession, Strategy

#: 工具自称的「此刻」：北京 2026-09-23 12:00 == UTC 04:00。
#: 选这个钟点是为了让默认的 `concluded_payload()`（生效于北京 2026-09-23 08:00）落在
#: 「已生效」，而 `run_day + 1` 那一条落在「待生效」——两组都有东西可断言。
NOW = datetime(2026, 9, 23, 4, 0, tzinfo=dt_timezone.utc)

#: 日报第①段那句「本条还没咬人」。它是**两组各自成组**的判据：只有「待生效」那一组
#: 该有它，「生效中」那一组有它就是一句假话。
TODAY_DISCLAIMER = "（此刻生效中的仍是上一有效阶段，本条要到生效时刻才咬人）"

#: 保命档那一层的**特征串**，取触发源的显示名（`HaltTrigger.BLANKET.display` = 「保命档
#: （高波动）」），不是写入方给的那一列 `label`（`halt_sync.BLANKET_LABEL` = 「高波动」）。
#: 层的身份是 `HaltDeclaration.trigger` 这一列上的事实，`label` 随数据变——第②c 段之前
#: 这里用的是 `startswith("高波动档")`，那个前缀随 `label` 一起漂走了。具体的层标题措辞
#: 由 `test_halt.py::TestVerdict` 钉；这里只用来断言「事件层在场而保命档不在场」。
BLANKET_MARK = HaltTrigger.BLANKET.display


def _frozen(now: datetime = NOW):
    """把工具看到的「此刻」钉住。`timezone.now` 是模块属性，打在源模块上即全局生效。"""
    return patch("apps.agent.tools.regime_queries.timezone.now", return_value=now)


def _value(raw):
    """枚举成员或裸串都收——造数据时两种写法读起来都自然。"""
    return raw.value if hasattr(raw, "value") else raw


def _user(email: str):
    User = get_user_model()
    return User.objects.create_user(
        email=email, username=email.split("@")[0], password="pw12345"
    )


def _strategy(name: str) -> Strategy:
    return Strategy.objects.create(name=name, code_path=f"/t/{name}.py")


def _session(user, strategy, *, status: str = "running") -> LiveSession:
    return LiveSession.objects.create(
        user=user,
        strategy=strategy,
        symbol="BTC/USDT",
        mode="paper",
        status=status,
        initial_capital=Decimal("10000.00"),
    )


def _judgement(
    *,
    effective_at: datetime,
    regime: BaseRegime | str = BaseRegime.DOWNTREND,
    attribute_date: date | None = None,
    symbol: str = SYMBOL,
) -> RegimeJudgement:
    """一条手搓的判定行。时刻由调用方给全，不吃 `RUN_DAY`——本文件要能造出
    「生效中 / 待生效 / 两者都不是」三种归属。"""
    value = regime.value if isinstance(regime, BaseRegime) else regime
    return RegimeJudgement.objects.create(
        symbol=symbol,
        attribute_date=attribute_date or RUN_DAY - timedelta(days=1),
        effective_at=effective_at,
        base_regime=value,
        escalation="",
        effective_regime=value,
    )


def _event(
    name: str = "FOMC",
    *,
    impact: EventImpact | str = EventImpact.HIGH,
    status: EventStatus | str = EventStatus.SCHEDULED,
    halt_at: datetime | None = None,
    resume_at: datetime | None = None,
    event_time: datetime | None = None,
    scope_kind: EventScope | str = EventScope.MARKET,
    symbols: list[str] | None = None,
) -> MajorEvent:
    """一条事件。默认落在「正在进行中、档位高、全市场」——也就是**唯一会成层**的形态。"""
    return MajorEvent.objects.create(
        name=name,
        scope_kind=_value(scope_kind),
        symbols=symbols if symbols is not None else [],
        event_time=event_time or (NOW + timedelta(hours=1)),
        impact=_value(impact),
        halt_at=halt_at or (NOW - timedelta(hours=1)),
        resume_at=resume_at or (NOW + timedelta(hours=1)),
        status=_value(status),
        created_by="tester",
    )


def _exemption(
    strategy: Strategy,
    *,
    regime: str = BaseRegime.DOWNTREND.value,
    granted_at: datetime | None = None,
    expires_at: datetime | None = None,
    closed_at: datetime | None = None,
) -> DeactivationExemption:
    return DeactivationExemption.objects.create(
        strategy=strategy,
        regime=regime,
        granted_at=granted_at or (NOW - timedelta(days=1)),
        expires_at=expires_at or (NOW + timedelta(days=9)),
        granted_by="tester",
        closed_at=closed_at,
    )


def _decision(
    strategy: Strategy,
    *,
    regime: str = BaseRegime.DOWNTREND.value,
    status: str = DecisionStatus.SUGGESTED.value,
    evidence: dict | None = None,
    exemption: DeactivationExemption | None = None,
) -> DeactivationDecision:
    return DeactivationDecision.objects.create(
        strategy=strategy,
        regime=regime,
        status=status,
        evidence=evidence if evidence is not None else {},
        exemption=exemption,
        first_decided_at=NOW - timedelta(days=3),
        last_confirmed_at=NOW - timedelta(hours=2),
    )


def _frozen_evidence(**over) -> dict:
    """一份形状照抄 `deactivation_run._frozen_evidence` 的冻结依据。"""
    row = {
        "regime": BaseRegime.DOWNTREND.value,
        "state": "unfit",
        "reason": "insufficient_evidence",
        "source": "strategy",
        "needs_review": False,
        "pool_rebuild_id": 7,
        "pool_version": "v1",
        "regime_effective_at": (NOW - timedelta(days=2)).isoformat(),
        "cell": {
            "trades": 12,
            "months": 4,
            "regime_days": 30,
            "threshold": {"min_trades": 20, "min_months": 6, "met": False},
            "metrics": {"calmar": -0.5, "max_drawdown_pct": 23.456},
            "symbols": ["BTC/USDT"],
        },
    }
    row.update(over)
    return row


# --------------------------------------------------------------------------- #
# query_regime
# --------------------------------------------------------------------------- #


class TestQueryRegime(TestCase):
    """`query_regime` **必须同时给出「生效中」与「待生效」两条**（CONTEXT.md:183）。

    它们回答的是两个不同问题：用户刚在日报里读到「明日起下行趋势」之后追问的是生效态
    （「为什么我现在被停」），而停用决策恰恰由待生效那条产生（「明天我为什么会被停」）。
    只给一条，就有一个问题永远答不出来——而「同一件事的两个入口给两种说法」正是本机制
    从头到尾在防的形态。
    """

    def test_cold_start_says_no_phase_is_in_force(self):
        text = ""
        with _frozen():
            text = QueryRegimeTool()._render()

        self.assertIn("【生效中】", text)
        self.assertIn("【待生效】", text)
        self.assertIn("库中还没有任何生效过的判定（冷启动）", text)
        self.assertIn("没有待生效的判定", text)
        self.assertNotIn(
            "最近写下的一条是", text, "一条都没有时不该谈「最近写下的一条」"
        )

    def test_the_two_groups_are_two_states_not_one_sentence(self):
        """生效中那条**不许**带第①段那句「本条要到生效时刻才咬人」——它已经咬了。

        两段共用 `_section_today` 就会把一句假话贴进半个输出，所以它们各自成组、各写
        各的。这条断言用「【待生效】」把输出切成两半，分别看那句在不在。
        """
        _judgement(effective_at=business_midnight(RUN_DAY + timedelta(days=1)))
        _judgement(
            effective_at=business_midnight(RUN_DAY + timedelta(days=2)),
            regime=BaseRegime.HIGH_VOL,
        )
        with _frozen():
            text = QueryRegimeTool()._render()

        in_force, _, pending = text.partition("【待生效】")
        self.assertIn("当前阶段：下行趋势", in_force)
        self.assertNotIn(TODAY_DISCLAIMER, in_force, "已生效的那条不许再说「还没咬人」")
        self.assertNotIn("今日判定：高波动", in_force)

        self.assertIn("今日判定：高波动", pending, "待生效那组逐字用日报第①段的渲染")
        self.assertIn(TODAY_DISCLAIMER, pending)
        self.assertNotIn("当前阶段：高波动", pending)

    def test_a_row_that_is_neither_in_force_nor_pending_is_still_named(self):
        """补写的判定（先写 D、后补 D−2）会造出「既不在生效中、也不在待生效」的一条。

        这种情况下上面两段都没提到它，而「机制上一次成功写下了什么」正是这一问的收尾。
        这里**刻意不去断言「最近那条已经生效」**——那句话在补写情形下是假的。
        """
        backfilled = _judgement(
            effective_at=business_midnight(RUN_DAY + timedelta(days=1))
            - timedelta(days=2),
            attribute_date=date(2026, 9, 20),
        )
        in_force = _judgement(
            effective_at=business_midnight(RUN_DAY + timedelta(days=1))
        )
        # `last_judgement` 按 `-created_at` 取，两条在同一微秒内造出来会分不出先后。
        RegimeJudgement.objects.filter(pk=backfilled.pk).update(
            created_at=NOW - timedelta(hours=1)
        )
        RegimeJudgement.objects.filter(pk=in_force.pk).update(
            created_at=NOW - timedelta(hours=2)
        )

        with _frozen():
            text = QueryRegimeTool()._render()

        self.assertIn("没有待生效的判定", text)
        self.assertIn("最近写下的一条是", text)
        self.assertIn(str(backfilled.run_day), text)

    def test_it_carries_the_evidence_numbers(self):
        """「为什么」必须带数字，而不是「市场波动加大」这类话（CONTEXT.md:173）。"""
        record = _judgement(effective_at=business_midnight(RUN_DAY + timedelta(days=1)))
        record.evidence = {
            "quant": {
                "atr_pct": 3.21456,
                "atr_pct_rank": 0.91,
                "quantile_sample": 120,
                "ema_fast": 60123.4567,
                "ema_slow": 61234.5678,
                "ema_slope": -0.000123,
                "separation": -0.4321,
            }
        }
        record.save(update_fields=["evidence"])

        with _frozen():
            text = QueryRegimeTool()._render()

        self.assertIn("依据：", text)
        self.assertIn("ATR% 3.2146", text)
        self.assertIn("120 日分位 91.0%", text)
        self.assertIn("高于高波动阈值", text)
        self.assertIn("分离度 (EMA快−EMA慢)/ATR = -0.432", text)


# --------------------------------------------------------------------------- #
# query_halt
# --------------------------------------------------------------------------- #


class TestQueryHalt(TestCase):
    """`query_halt` 返回**全部在拦的层**，不裁剪（CONTEXT.md:172）。

    第②c 段之后这里列的层**就是 `HaltDeclaration` 里的行**——工具与 `pre_trade_check` 从
    同一个 ``halt.blocking_declarations`` 出发，所以它不会说出一个订单通路上不存在的层。
    造数据的方式因此跟着变了：**事件表里有一条高影响事件不等于此刻在拦**，中间那一步
    （事实 → 声明行）是这一层要钉的东西，所以先塞事实、再跑一轮 `halt_sync.sync(now=NOW)`。

    **两层可以同时生效**，所以回显的是一个集合而不是一条（CONTEXT.md:177）。

    声明与开关是**两件事**（`halt.py` 的模块 docstring）：声明说「这个源想拦」，开关说
    「这个源启用了没有」。所以「事件层真的在拦」要两件都给——下面每条事件用例都先拨开关，
    否则它会因为「开关关着」而通过，看起来与「事件不该拦」一模一样。
    """

    def _enable_event_breaker(self) -> None:
        RegimeMechanismSwitch.objects.create(
            kind=MechanismKind.EVENT_BREAKER.value,
            from_mode=MechanismMode.SHADOW.value,
            to_mode=MechanismMode.EXECUTING.value,
            at=NOW - timedelta(days=1),
            actor_kind=ActorKind.CLI.value,
            actor_name="ops",
            reason="测试",
        )

    def test_nothing_is_blocking(self):
        with _frozen():
            text = QueryHaltTool()._render()

        self.assertIn(
            "当前没有任何层在拦：事件熔断层、高波动档、策略停用决策档与按阶段暂停开仓档"
            "都没有生效。",
            text,
        )
        self.assertIn("人工豁免：当前没有在期的人工豁免。", text)
        self.assertIn("机制当前档：Shadow（只记录，不执行）", text)
        # 第②段把停止判定接到了下单拦截上，所以这句话从「尚未接线」改成了「接线到哪一步」；
        # 第③段把第三档（策略停用决策）也接上之后，「差额正是还没接线的那一档」那句不再
        # 成立，换成了不变量本身；issue #51 又加了第四档（按阶段暂停开仓），这一档同样在
        # `blocking_declarations` 里，所以数法不变、只是档数从三变四。数字取声明表的实数
        # （此处为空表 = 0），与上面两句同源。
        self.assertIn("停止声明表此刻 0 条在生效", text)
        self.assertIn("已接到下单拦截", text)
        self.assertIn("四档都算在内", text)
        self.assertNotIn("还没接线", text)
        self.assertNotIn("要到第③段才接线", text)

    def test_both_numbers_come_from_the_same_evaluation(self):
        """层数与条数出自同一次求值——``_gate_line`` 必须把 ``now`` 传下去。

        这是一条**接线测试**，理由与「不传 `now` 的版本读起来完全正常」有关：冻结时钟是
        打在 `django.utils.timezone` 上的全局补丁，所以两条路都看得到同一个时刻，缺了
        `now=` 的那一版在这一整套用例里**一模一样地通过**。只能问「那次调用拿到了什么」。
        不钉住的话，缝会在某条声明恰好到期的那一刻张开，而那一刻的表现正好是「有一档
        没对上」——最费人的一种症状。
        """
        from apps.regime import halt as halt_module

        seen: list[object] = []
        real = halt_module.blocking_declarations

        def _spy(*args, **kwargs):
            seen.append(kwargs.get("now"))
            return real(*args, **kwargs)

        with _frozen(), patch.object(halt_module, "blocking_declarations", _spy):
            QueryHaltTool()._render()

        self.assertTrue(seen, "这一轮没有问过声明表")
        self.assertEqual(set(seen), {NOW}, "层数与条数不是在同一次求值上算的")

    def test_it_echoes_the_current_mode_instead_of_hardcoding_shadow(self):
        """档位那一行取 `RegimeMechanismSwitch.current()`，不写死「Shadow」。

        单元 8 **刻意一行都不写**这张表（Q4：只读它，状态恒为 shadow），所以这里的
        「一条流水都没有」本身就是那条决定的可执行形态。
        """
        self.assertEqual(RegimeMechanismSwitch.objects.count(), 0)
        with _frozen():
            text = QueryHaltTool()._render()
        self.assertIn("机制当前档：Shadow（只记录，不执行）", text)

    # -- 事件层：事实先落成声明，声明才成层 ---------------------------------- #

    def test_an_event_that_was_never_synced_is_not_a_layer(self):
        """**事件表里有事件 ≠ 此刻在拦。**

        这是第②c 段带来的最锋利的一条：判定函数不读事件表（CONTEXT.md:134），所以一条
        高影响事件在窗口里、档位为高、开关也拨到了执行态，**只要搬运没跑过，就一层都没有**。
        这条用例就是那个搬运环节的可执行形态——少了它，`halt_sync` 整个模块坏掉也不一定有
        人发现：表空着的样子，与「本来就没有事件」一模一样。
        """
        self._enable_event_breaker()
        _event("FOMC 议息")
        with _frozen():
            text = QueryHaltTool()._render()

        self.assertIn("当前没有任何层在拦", text)
        self.assertIn("停止声明表此刻 0 条在生效", text)

    def test_a_synced_event_is_one_layer(self):
        self._enable_event_breaker()
        _event("FOMC 议息")
        with _frozen():
            halt_sync.sync(now=NOW)
            text = QueryHaltTool()._render()

        self.assertIn("当前在拦的层：1 层", text)
        self.assertIn("停止声明表此刻 1 条在生效", text)
        # 层标题 = `halt.layer_of(row).text` = 「触发源名称（触发源档，作用域 人话）」。
        # 在这里写死一遍，让措辞漂移在**工具这一侧**也红一次；同一句话的
        # `pre_trade_check` 那一侧由 `test_halt.py::TestVerdict` 钉。
        self.assertIn(
            "【第 1 层｜FOMC 议息（事件熔断，作用域 全市场（global））】", text
        )
        # 正文第一段是生效期，两个绝对时刻都写出来（`events.format_moment`，与事件库、
        # 日报第③段同口径）。这里只钉「北京时间」那半句的换算：事件窗口起于 UTC 03:00。
        self.assertIn("生效期：北京时间 2026-09-23 11:00", text)
        # 第二段是声明自己落库时写下的依据，不是工具现编的。
        self.assertIn("高影响事件熔断窗口（档位为「高」的事件才触发熔断）：", text)
        self.assertNotIn(BLANKET_MARK, text)

    def test_the_layer_title_is_the_same_line_as_the_rejection_reason(self):
        """层标题与 `pre_trade_check` 的拒绝理由**逐字同源**。

        `_layer_block` 取 `halt.layer_of(row).text` 而不是在这里重拼一遍「触发源 + 作用域」
        ——理由就是这个断言：用户被拒时看到的那句话，与他自己去查到的，必须是同一句。
        断言拿 `block_reason` 去比而不是再写死一遍标题串，因为被测的是**两者的同源关系**；
        标题的具体措辞由上面那条用例钉。
        """
        self._enable_event_breaker()
        _event("FOMC 议息")
        with _frozen():
            halt_sync.sync(now=NOW)
            text = QueryHaltTool()._render()
            reason = halt.block_reason("BTC/USDT", now=NOW)

        self.assertNotEqual(reason, "")
        layer_text = reason.removeprefix("停止判定命中：")
        self.assertIn(f"【第 1 层｜{layer_text}】", text)

    def test_the_event_layer_needs_its_switch(self):
        """行先写、开关后拨——「声明与开关分开」的可执行形态。

        Shadow 期不写行的话，「出 Shadow」那一刻表是空的：什么都不拦，一直到下一个窗口
        才有行，等于把出 Shadow 的时点本身变成一段敞口。所以这里断言的是：**行一直在，
        只是不算数**；开关一拨，同一份行立刻开始拦，不必等下一轮同步。
        """
        _event("FOMC 议息")
        with _frozen():
            halt_sync.sync(now=NOW)
            shadow = QueryHaltTool()._render()

        self.assertIn("当前没有任何层在拦", shadow)
        self.assertIn("停止声明表此刻 0 条在生效", shadow)
        self.assertEqual(
            len(halt.live_declarations(now=NOW)), 1, "行是活的，只是开关还没拨"
        )

        self._enable_event_breaker()
        with _frozen():
            text = QueryHaltTool()._render()

        self.assertIn("当前在拦的层：1 层", text)
        self.assertIn("停止声明表此刻 1 条在生效", text)

    # -- 保命档 -------------------------------------------------------------- #

    def test_a_high_vol_phase_is_the_blanket_layer(self):
        """保命档**不需要开关**：`HALT_TRIGGER_SWITCH[BLANKET] = None`（「高波动算生效」）。

        拨开关这一条不写进用例的话，「高波动在拦」与「高波动在拦但开关关着」就分不开，
        而后者在真实运行里根本不存在——那张映射表里 `BLANKET` 的值就是 `None`。
        """
        _judgement(
            effective_at=business_midnight(RUN_DAY + timedelta(days=1)),
            regime=BaseRegime.HIGH_VOL,
        )
        with _frozen():
            halt_sync.sync(now=NOW)
            text = QueryHaltTool()._render()

        self.assertEqual(RegimeMechanismSwitch.objects.count(), 0)
        self.assertIn("当前在拦的层：1 层", text)
        self.assertIn(
            "【第 1 层｜高波动（保命档（高波动），作用域 全市场（global））】", text
        )
        self.assertIn("保命档不做适用性判断，与证据无关", text)
        # 不定 = 没有预先知道的截止时刻（`_layer_block` 用 `events.format_moment` 渲染，
        # None → 「未定」）。保命档的失效由「阶段离开高波动」那一轮写 `closed_at`。
        self.assertIn("→ 未定", text)

    def test_both_layers_can_be_live_at_once(self):
        """**用户眼里始终是一个集合，不是一条流**（CONTEXT.md:177）。

        两层同时生效时折成一条，就会让「还剩几层」这个数答不出来——而那正是用户唯一
        能据以判断「我什么时候能开新仓」的东西。
        """
        self._enable_event_breaker()
        _event("非农")
        _judgement(
            effective_at=business_midnight(RUN_DAY + timedelta(days=1)),
            regime=BaseRegime.HIGH_VOL,
        )
        with _frozen():
            halt_sync.sync(now=NOW)
            text = QueryHaltTool()._render()

        self.assertIn("当前在拦的层：2 层", text)
        self.assertIn("停止声明表此刻 2 条在生效", text)
        # **编号按生效时刻排，不按触发源类型排**（`live_declarations` 的
        # `order_by("opened_at", "id")`）：保命档这一行的 `opened_at` 是日界（UTC 00:00），
        # 比事件窗口的起点（UTC 03:00）早，所以它在第 1 层。顺序是稳定的，用户两次问到的
        # 编号不会翻面——这正是不按类型排的意义。
        self.assertIn(
            "【第 1 层｜高波动（保命档（高波动），作用域 全市场（global））】", text
        )
        self.assertIn("【第 2 层｜非农（事件熔断，作用域 全市场（global））】", text)

    # -- 不成为层的事件：每一条都拨了开关，否则会因为别的原因通过 ------------- #

    def test_a_medium_impact_event_is_not_a_layer(self):
        """档位不是「高」的事件**不产生熔断**。少这一条的表现是「一条只提醒不熔断的
        事件被报成了在拦」，比漏报更坏。"""
        self._enable_event_breaker()
        _event("低影响事件", impact=EventImpact.MEDIUM)
        with _frozen():
            halt_sync.sync(now=NOW)
            text = QueryHaltTool()._render()
        self.assertIn("当前没有任何层在拦", text)
        self.assertIn("停止声明表此刻 0 条在生效", text)

    def test_a_cancelled_event_is_not_a_layer(self):
        self._enable_event_breaker()
        _event("已取消的会议", status=EventStatus.CANCELLED)
        with _frozen():
            halt_sync.sync(now=NOW)
            text = QueryHaltTool()._render()
        self.assertIn("当前没有任何层在拦", text)
        self.assertIn("停止声明表此刻 0 条在生效", text)

    def test_an_event_whose_window_has_passed_is_not_a_layer(self):
        self._enable_event_breaker()
        _event(
            "上周的事件",
            halt_at=NOW - timedelta(days=3),
            resume_at=NOW - timedelta(days=2),
        )
        with _frozen():
            halt_sync.sync(now=NOW)
            text = QueryHaltTool()._render()
        self.assertIn("当前没有任何层在拦", text)
        self.assertIn("停止声明表此刻 0 条在生效", text)

    def test_an_event_whose_window_has_not_opened_is_not_a_layer(self):
        """窗口还没开：**行已经在表里了**，只是 `opened_at` 在未来。

        这一条钉的是第②c 段那个「一行 = 这个源在此作用域上此刻的态度」的取舍：下一段窗口
        在开启前就写进表，所以一段窗口结束到下一段开始之间不会有「表里没窗口」的敞口；
        代价是这一行在生效前就存在——而 `live_declarations` 的生效期过滤把它挡在门外，
        所以它此刻拦不住任何东西。
        """
        self._enable_event_breaker()
        _event(
            "下周的事件",
            halt_at=NOW + timedelta(days=2),
            resume_at=NOW + timedelta(days=3),
            event_time=NOW + timedelta(days=3),
        )
        with _frozen():
            halt_sync.sync(now=NOW)
            text = QueryHaltTool()._render()

        self.assertEqual(HaltDeclaration.objects.count(), 1, "下一段窗口已经排进表里")
        self.assertEqual(halt.live_declarations(now=NOW), [], "但它此刻还不生效")
        self.assertIn("当前没有任何层在拦", text)
        self.assertIn("停止声明表此刻 0 条在生效", text)

    # -- 人工豁免（CONTEXT.md:176） ------------------------------------------ #

    def test_an_in_force_exemption_is_echoed(self):
        strategy = _strategy("甲")
        _exemption(strategy)
        with _frozen():
            text = QueryHaltTool()._render()
        self.assertIn("人工豁免：1 条在期（当前没有保命档在拦，豁免照常生效）。", text)

    def test_the_blanket_layer_suppresses_every_exemption(self):
        """**人工恢复豁免不穿透保命档，且这条必须能在这条输出里被读出来。**

        否则用户会看到一个自己放行过、却又被停的策略，而那与「机制没听见我」在观感上
        无法区分。判据是**保命档那一层真的在拦**（声明表里的 BLANKET 行），不是「生效中的
        判定是纸面上的高波动」——所以这里要跑一轮 `halt_sync.sync`。
        """
        _exemption(_strategy("甲"))
        _exemption(_strategy("乙"))
        _judgement(
            effective_at=business_midnight(RUN_DAY + timedelta(days=1)),
            regime=BaseRegime.HIGH_VOL,
        )
        with _frozen():
            halt_sync.sync(now=NOW)
            text = QueryHaltTool()._render()

        self.assertIn("当前在拦的层：1 层", text)
        self.assertIn("2 条在期，但**当前一条都不生效**", text)
        self.assertIn("人工恢复豁免不穿透保命档", text)

    def test_an_expired_exemption_is_not_in_force(self):
        _exemption(
            _strategy("甲"),
            granted_at=NOW - timedelta(days=20),
            expires_at=NOW - timedelta(days=10),
        )
        with _frozen():
            text = QueryHaltTool()._render()
        self.assertIn("人工豁免：当前没有在期的人工豁免。", text)

    def test_a_not_yet_granted_exemption_is_not_in_force(self):
        _exemption(_strategy("甲"), granted_at=NOW + timedelta(days=1))
        with _frozen():
            text = QueryHaltTool()._render()
        self.assertIn("人工豁免：当前没有在期的人工豁免。", text)

    def test_a_closed_exemption_is_not_in_force(self):
        """「阶段离开」写成 `closed_at` 而不是纯查询派生，就是为了让它**不复活**。"""
        _exemption(_strategy("甲"), closed_at=NOW - timedelta(hours=1))
        with _frozen():
            text = QueryHaltTool()._render()
        self.assertIn("人工豁免：当前没有在期的人工豁免。", text)


# --------------------------------------------------------------------------- #
# query_deactivation_decisions
# --------------------------------------------------------------------------- #


class TestQueryDeactivationDecisions(TestCase):
    """这是四个工具里**唯一按人裁剪**的一个。

    裁剪的判据是 CONTEXT.md:172 的那条——按「这个对象是不是 per-user 为真」决定，不按
    入口一刀切。「这条决策停的**是**我在跑的策略吗」是唯一 per-user 为真的问题。
    """

    def test_a_user_with_no_live_session_sees_nothing(self):
        _decision(_strategy("甲"))
        with _frozen():
            text = QueryDeactivationDecisionsTool()._render(str(uuid.uuid4()))
        self.assertIn("你正在跑的策略：0 个", text)
        self.assertIn("你没有活跃会话，所以没有任何停用决策与你相关。", text)
        self.assertNotIn("甲", text)

    def test_an_unknown_identity_is_the_same_answer_as_an_empty_one(self):
        """解析不到的 `user_id` **不是错误**——一个还没有活跃会话的账号，在「哪些停用
        决策与你相关」这一问上与一个不存在的账号给的是同一个答案（空集）。"""
        with _frozen():
            text = QueryDeactivationDecisionsTool()._render("no-such-username")
        self.assertIn("你没有活跃会话", text)

    def test_it_shows_only_my_strategies(self):
        mine, theirs = _user("mine@test.local"), _user("theirs@test.local")
        a, b = _strategy("甲"), _strategy("乙")
        _session(mine, a)
        _session(theirs, b)
        _decision(a, evidence=_frozen_evidence())
        _decision(b, evidence=_frozen_evidence())

        with _frozen():
            text = QueryDeactivationDecisionsTool()._render(str(mine.pk))

        self.assertIn("你正在跑的策略：1 个", text)
        self.assertIn("甲", text)
        self.assertNotIn("乙", text)

    def test_a_stopped_session_does_not_count_as_running(self):
        user = _user("stopped@test.local")
        strategy = _strategy("甲")
        _session(user, strategy, status="stopped")
        _decision(strategy)
        with _frozen():
            text = QueryDeactivationDecisionsTool()._render(str(user.pk))
        self.assertIn("你没有活跃会话", text)

    def test_an_applied_decision_still_appears(self):
        """**不按 `status` 过滤**。第③段的 gate 会把决策改成 `APPLIED`，那时一条正在
        停人的决策若被筛掉，用户问「为什么停我」会得到「没有与你相关的停用决策」——
        而他的策略正被停着。"""
        user = _user("applied@test.local")
        strategy = _strategy("甲")
        _session(user, strategy)
        _decision(
            strategy, status=DecisionStatus.APPLIED.value, evidence=_frozen_evidence()
        )

        with _frozen():
            text = QueryDeactivationDecisionsTool()._render(str(user.pk))

        self.assertIn("与你相关的停用决策：1 条", text)
        self.assertIn("已施加", text)

    def test_a_released_decision_still_appears(self):
        """`RELEASED` 同理：豁免指针就挂在这一行上，筛掉它，「我不是恢复过它吗」也一并
        答不出来。"""
        user = _user("released@test.local")
        strategy = _strategy("甲")
        _session(user, strategy)
        _decision(strategy, status=DecisionStatus.RELEASED.value)

        with _frozen():
            text = QueryDeactivationDecisionsTool()._render(str(user.pk))

        self.assertIn("已解除", text)

    def test_the_frozen_evidence_is_read_not_recomputed(self):
        """数字全部取自决策行自己冻下来的 `evidence`——池化表会换代，现算出来的数会与
        这条决策当初凭什么成立对不上，而那正是审计要问的。"""
        user = _user("evidence@test.local")
        strategy = _strategy("甲")
        _session(user, strategy)
        _decision(strategy, evidence=_frozen_evidence())

        with _frozen():
            text = QueryDeactivationDecisionsTool()._render(str(user.pk))

        self.assertIn("池化结论：不适用：证据不足", text)
        self.assertIn(
            "12 笔 / 4 个月 / 该阶段 30 个交易日（门槛 20 笔 / 6 个月，未达）", text
        )
        self.assertIn("Calmar -0.500", text)
        self.assertIn("分段最大回撤 23.456%", text)
        self.assertIn("品种 BTC/USDT", text)
        self.assertIn("世代：#7（池化口径 v1）", text)

    def test_an_exemption_in_force_is_reported_as_such(self):
        user = _user("exempt@test.local")
        strategy = _strategy("甲")
        _session(user, strategy)
        exemption = _exemption(strategy)
        _decision(strategy, exemption=exemption)

        with _frozen():
            text = QueryDeactivationDecisionsTool()._render(str(user.pk))

        self.assertIn("由 tester 授予", text)
        self.assertIn("；在期生效中", text)
        self.assertNotIn("当前不生效", text)

    def test_the_blanket_layer_suppresses_it_here_too(self):
        """同一条豁免，在**这个**入口也必须说得出「当前不生效，因为保命档在拦」。"""
        user = _user("exempt2@test.local")
        strategy = _strategy("甲")
        _session(user, strategy)
        exemption = _exemption(strategy)
        _decision(strategy, exemption=exemption)
        # 保命档是**阶段本身**的性质，不是哪一格池化结论——所以它压在决策上，
        # 哪怕这条决策自己的阶段是下行趋势。
        _judgement(
            effective_at=business_midnight(RUN_DAY + timedelta(days=1)),
            regime=BaseRegime.HIGH_VOL,
        )

        with _frozen():
            text = QueryDeactivationDecisionsTool()._render(str(user.pk))

        self.assertIn("**当前不生效**", text)
        self.assertIn("人工恢复豁免不穿透保命档", text)

    def test_a_closed_exemption_is_named_as_closed(self):
        user = _user("closed@test.local")
        strategy = _strategy("甲")
        _session(user, strategy)
        exemption = _exemption(strategy, closed_at=NOW - timedelta(hours=1))
        _decision(strategy, exemption=exemption)

        with _frozen():
            text = QueryDeactivationDecisionsTool()._render(str(user.pk))

        self.assertIn("已提前失效", text)

    def test_a_decision_without_an_exemption_says_so(self):
        user = _user("plain@test.local")
        strategy = _strategy("甲")
        _session(user, strategy)
        _decision(strategy)
        with _frozen():
            text = QueryDeactivationDecisionsTool()._render(str(user.pk))
        self.assertIn("人工豁免：无", text)

    def test_a_username_is_accepted_as_well_as_a_uuid(self):
        """`user_id` 可能是 UUID 也可能是 username（`list_orders.py` 的先例）。"""
        user = _user("by-name@test.local")
        strategy = _strategy("甲")
        _session(user, strategy)
        _decision(strategy)
        with _frozen():
            text = QueryDeactivationDecisionsTool()._render("by-name")
        self.assertIn("与你相关的停用决策：1 条", text)


# --------------------------------------------------------------------------- #
# query_events
# --------------------------------------------------------------------------- #


class TestQueryEvents(TestCase):
    """`query_events` 与日报第③段回答的是**同一个问题**。

    所以天数写死 7 天、不吃参数（CONTEXT.md:183）：一旦可传，Agent 就会按用户措辞猜一个
    数（「这周」「近期」「下个月」各滑向不同的 N），而这是个可以写死的常量。
    """

    def test_it_is_the_daily_report_third_section_verbatim(self):
        _event("CPI 公布", event_time=NOW + timedelta(days=2))
        _event(
            "低影响事件", impact=EventImpact.MEDIUM, event_time=NOW + timedelta(days=1)
        )
        CandidateEvent.objects.create(
            name="疑似议息",
            origin=CandidateOrigin.NEWS.value,
            guessed_time=NOW + timedelta(days=3),
            raised_at=RUN_DAY,
            raised_by="资讯通道",
            expires_at=NOW + timedelta(days=5),
            status=CandidateStatus.PENDING.value,
        )

        with _frozen():
            self.assertEqual(
                QueryEventsTool()._render(), report._section_events(now=NOW)
            )

    def test_an_empty_horizon_still_answers(self):
        with _frozen():
            text = QueryEventsTool()._render()
        self.assertIn(f"未来 {config.REPORT.event_horizon_days} 天", text)
        self.assertIn("会触发熔断的（档位「高」）：没有", text)
        self.assertIn("待人工处置的候选事件：没有", text)

    def test_the_horizon_is_not_a_parameter(self):
        """schema 是空的——天数没有入口可以传。"""
        self.assertEqual(QueryEventsTool().parameters_schema["properties"], {})


# --------------------------------------------------------------------------- #
# 边界：破例 + 注册
# --------------------------------------------------------------------------- #


class TestTheBreak(TestCase):
    """**破例必须写下来**（CONTEXT.md:183、172）。

    ToolRegistry 的约定是「`user_id` 自动过滤」。本机制的判定与保命档是系统级、全局唯一
    的，按 `user_id` 过滤会造出一个不存在的东西（「我的行情阶段」）。所以三个工具不过滤，
    只有 `query_deactivation_decisions` 按人裁剪。

    这条约定**用签名钉住**：签名是唯一不会随文案漂移的载体，而「统一一下，全都不过滤」
    或「统一一下，全都过滤」这两种改动都会在这里当场响。
    """

    def test_three_tools_take_no_identity_and_one_does(self):
        cropped = {
            QueryDeactivationDecisionsTool,
        }
        for tool_cls in (
            QueryRegimeTool,
            QueryHaltTool,
            QueryDeactivationDecisionsTool,
            QueryEventsTool,
        ):
            params = inspect.signature(tool_cls()._render).parameters
            if tool_cls in cropped:
                self.assertEqual(
                    list(params), ["user_id"], f"{tool_cls.name} 该按人裁剪"
                )
            else:
                self.assertEqual(
                    list(params), [], f"{tool_cls.name} 是系统级状态，不许按人裁剪"
                )

    def test_every_tool_has_an_empty_schema_and_a_read_only_execute(self):
        for tool_cls in (
            QueryRegimeTool,
            QueryHaltTool,
            QueryDeactivationDecisionsTool,
            QueryEventsTool,
        ):
            tool = tool_cls()
            self.assertEqual(
                tool.parameters_schema,
                {"type": "object", "properties": {}, "required": []},
            )
            self.assertIn("**kwargs", str(inspect.signature(tool.execute)))

    def test_all_four_are_registered(self):
        """`tools/__init__.py` 是按文件自动发现的——掉一个文件进去就注册上了，
        而语法错误会被吞成一条 warning（注册零个）。所以这里钉的是名字，不是条数。"""
        names = {tool.name for tool in ToolRegistry.all()}
        for expected in (
            "query_regime",
            "query_halt",
            "query_deactivation_decisions",
            "query_events",
        ):
            self.assertIn(expected, names)
            self.assertIsNotNone(ToolRegistry.get(expected))


class TestTheAsyncEdges(TestCase):
    """两条**不碰数据库**的早退路径。走 `db_async` 之前就返回，所以 `TestCase` 的线程
    问题在这里不存在。"""

    def test_a_call_without_an_identity_refuses(self):
        result = asyncio.run(QueryDeactivationDecisionsTool().execute())
        self.assertFalse(result.success)
        self.assertEqual(result.error, "无法确定用户身份")

    def test_the_identity_is_still_required_with_other_arguments(self):
        result = asyncio.run(
            QueryDeactivationDecisionsTool().execute(user_id="", limit=10)
        )
        self.assertFalse(result.success)
