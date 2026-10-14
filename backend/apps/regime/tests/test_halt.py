"""halt 状态机：声明 → 「这一张单此刻挡不挡得住」（ADR 0001 的唯一停止抽象）。

第②段引入的 `HaltDeclaration` 是**一组声明**，不是一行布尔（CONTEXT.md:130）：唯一键是
（触发源 × 作用域），每行带自己的生效期，「当前是否拦」= 存在任一命中的生效行。这个文件
钉的就是那次求值，四件事各一组：

1. **生效期的边界**。`opened_at` 是闭区间、`expires_at` 是开区间；解除靠 `closed_at`
   而不是删行。三处的取等号方向错了，表现都是「停早了一刻」或「晚解除一刻」——都在
   边界上，日常跑不出来。
2. **作用域三档的命中**。global 拦全场、symbol 只拦那个品种、strategy 只在调用方给出
   `strategy_id` 时才算数（Q3）。读不懂的写法当 global（fail-closed）。
3. **开关与声明是两件事**。声明说「这个源想拦」，开关说「这个源启用了没有」，两者都为真
   才作数。**保命档没有开关**（`HALT_TRIGGER_SWITCH[BLANKET] = None`）——「高波动算生效」，
   它不由任何人确认。
4. **层是一个集合**。几层同时生效时理由必须全部写出来：只报第一层会让人按那层去排查，
   而解除它之后单还是下不出去。

用 `TestCase`（真库），因为这个模块存在的意义就是「状态在表里、扛过进程重启」
（CONTEXT.md:132）——把表换成内存字典就绕过了它唯一的性质。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_timezone

from django.test import TestCase

from apps.regime import halt
from apps.regime.models import (
    ActorKind,
    HaltDeclaration,
    HaltTrigger,
    MechanismKind,
    MechanismMode,
    RegimeMechanismSwitch,
)

NOW = datetime(2026, 9, 23, 0, 0, tzinfo=dt_timezone.utc)
SYMBOL = "DOGE/USDT"


def _declare(
    *,
    trigger: HaltTrigger = HaltTrigger.EVENT,
    scope: str = halt.global_scope(),
    label: str = "FOMC 议息",
    opened_at: datetime | None = None,
    expires_at: datetime | None = None,
    closed_at: datetime | None = None,
    reason: str = "高影响事件窗口",
) -> HaltDeclaration:
    return HaltDeclaration.objects.create(
        trigger=trigger.value,
        scope=scope,
        label=label,
        opened_at=opened_at or (NOW - timedelta(hours=2)),
        expires_at=expires_at,
        closed_at=closed_at,
        reason=reason,
        actor_kind=ActorKind.TASK.value,
        actor_name="regime.sync_halt_windows",
    )


def _switch(kind: MechanismKind, mode: MechanismMode) -> RegimeMechanismSwitch:
    return RegimeMechanismSwitch.objects.create(
        kind=kind.value,
        from_mode=MechanismMode.SHADOW.value,
        to_mode=mode.value,
        at=NOW - timedelta(days=1),
        actor_kind=ActorKind.CLI.value,
        actor_name="ops",
        reason="测试",
    )


class _HaltTestBase(TestCase):
    """默认把**事件熔断那个开关打开**：本文件里多数用例问的是判定的形状（作用域命中、
    生效期边界、层的集合），不是开关。

    不打开的话，每条 `EVENT` 声明都会被静默过滤掉（开关默认 Shadow），用例测的其实成了
    「开关关着的时候什么都不拦」——那样写出来的断言在实现把 `switch_open` 整个删掉时
    也照样通过。开关自己的行为集中在 `TestSwitchGating`，那一类**不**调这个方法。
    """

    def setUp(self):
        super().setUp()
        self._enable_event_breaker()

    def _enable_event_breaker(self):
        _switch(MechanismKind.EVENT_BREAKER, MechanismMode.EXECUTING)


# --------------------------------------------------------------------------- #
# 作用域
# --------------------------------------------------------------------------- #


class TestScopeHelpers(TestCase):
    def test_the_three_scopes_round_trip(self):
        self.assertEqual(halt.parse_scope(halt.global_scope()), ("global", None))
        self.assertEqual(
            halt.parse_scope(halt.symbol_scope(SYMBOL)), ("symbol", SYMBOL)
        )
        self.assertEqual(
            halt.parse_scope(halt.strategy_scope("s-1")), ("strategy", "s-1")
        )

    def test_an_unreadable_scope_is_treated_as_global(self):
        """读不懂的作用域当**全市场**，而不是「不拦」。

        看不懂的写法只能来自写入方的 bug，而一条写坏了的声明静默失效是没有任何别的信号
        能暴露的——它看起来与「本来就没声明」一模一样。当成全市场会拦住所有开仓，吵闹
        但看得见。
        """
        for weird in ("", "   ", "SYMBOL:DOGE/USDT", "品种 DOGE/USDT", "symbol"):
            self.assertEqual(halt.parse_scope(weird), ("global", None), weird)

    def test_scope_display_is_one_sentence_for_all_callers(self):
        # 日报、query_halt 与拒绝理由共用同一句人话；各写一句就是「同一件事两处说法不同」。
        self.assertEqual(halt.scope_display(halt.global_scope()), "全市场（global）")
        self.assertEqual(
            halt.scope_display(halt.symbol_scope(SYMBOL)), f"品种 {SYMBOL}"
        )
        self.assertEqual(halt.scope_display(halt.strategy_scope("s-1")), "策略 s-1")


class TestScopeMatching(_HaltTestBase):
    def test_a_global_row_blocks_every_symbol(self):
        _declare(scope=halt.global_scope())
        verdict = halt.halt_layers("BTC/USDT", now=NOW)
        self.assertTrue(verdict.blocked)

    def test_a_symbol_row_blocks_only_that_symbol(self):
        _declare(scope=halt.symbol_scope(SYMBOL))

        self.assertTrue(halt.halt_layers(SYMBOL, now=NOW).blocked)
        self.assertFalse(halt.halt_layers("BTC/USDT", now=NOW).blocked)

    def test_a_strategy_row_does_not_block_when_the_caller_gives_no_strategy(self):
        """Q3 定死的判据：调用方没给 `strategy_id` 时，策略档行**不认它生效**。

        认成全市场就等于「一条针对某个策略的停用把别人的单也拦了」；而第③段接漏时的
        表现是**停用不生效**（池化日报里看得见），比「全场莫名停摆」容易发现得多。
        """
        _declare(scope=halt.strategy_scope("s-1"))

        self.assertFalse(halt.halt_layers(SYMBOL, now=NOW).blocked)
        self.assertFalse(halt.halt_layers(SYMBOL, "s-2", now=NOW).blocked)
        self.assertTrue(halt.halt_layers(SYMBOL, "s-1", now=NOW).blocked)


# --------------------------------------------------------------------------- #
# 生效期
# --------------------------------------------------------------------------- #


class TestLiveWindow(_HaltTestBase):
    def test_an_open_ended_row_is_live(self):
        _declare(opened_at=NOW - timedelta(hours=1), expires_at=None)
        self.assertTrue(halt.halt_layers(SYMBOL, now=NOW).blocked)

    def test_opened_at_is_inclusive(self):
        # 生效的**那一刻**就该拦：写 `opened_at__lt` 会让窗口起点漏一拍。
        _declare(opened_at=NOW, expires_at=None)
        self.assertTrue(halt.halt_layers(SYMBOL, now=NOW).blocked)

    def test_a_row_opened_in_the_future_is_not_live(self):
        # 保命档的窗口是「明天 08:00 起」——判定表里已经写了行，但此刻还不该拦。
        _declare(opened_at=NOW + timedelta(seconds=1))
        self.assertFalse(halt.halt_layers(SYMBOL, now=NOW).blocked)

    def test_expires_at_is_exclusive(self):
        # 截止时刻那一秒**不再拦**：写成 `expires_at__gte` 会让窗口多出一拍，
        # 而那一拍正好是事件结果出来之后。
        _declare(expires_at=NOW)
        self.assertFalse(halt.halt_layers(SYMBOL, now=NOW).blocked)

    def test_a_row_expiring_later_still_blocks(self):
        """与上一条配成一对。只钉「到点就停」的话，把整行当成不生效也照样通过——
        两条一起才说得清判据在 `expires_at` 上。

        两条各自建一行而不是一行里跑两次：唯一键是（触发源 × 作用域）且只管
        `closed_at IS NULL`，同源同作用域的第二行会直接撞 `uniq_live_halt_declaration`
        （它不知道 `expires_at`）。
        """
        _declare(expires_at=NOW + timedelta(seconds=1))
        self.assertTrue(halt.halt_layers(SYMBOL, now=NOW).blocked)

    def test_a_closed_row_is_not_live(self):
        """解除靠写 `closed_at`，不靠删行——「这条声明存在过」必须留着（ADR 0001）。"""
        _declare(closed_at=NOW - timedelta(minutes=1))
        self.assertFalse(halt.halt_layers(SYMBOL, now=NOW).blocked)


class TestLiveVersusBlocking(TestCase):
    """`live_declarations`（这个源想拦）与 `blocking_declarations`（此刻在拦）的分工。

    **本类刻意不打开开关**：两者只有在开关关着的时候才看得出差别。开关开着时它们返回
    同一批行，任何把两个函数实现成同一个的写法都能通过。
    """

    def test_live_declarations_ignores_the_switch(self):
        """窗口同步任务要按 `live_declarations` 对账（「我上次开的窗口还在不在表里」），
        而它不该受开关影响——否则一次人工关开关就会让任务以为窗口关了、把它解除掉，
        再开时又变成一条新声明。
        """
        _declare(trigger=HaltTrigger.EVENT)

        self.assertEqual(RegimeMechanismSwitch.objects.count(), 0)
        self.assertEqual(len(halt.live_declarations(now=NOW)), 1)
        self.assertEqual(halt.blocking_declarations(now=NOW), [])

    def test_the_two_agree_once_the_switch_is_open(self):
        # 反方向：开关打开后两者同一批行。上一条单独看的话，「`blocking_declarations`
        # 永远返回空」这种写坏了的实现也能通过。
        _declare(trigger=HaltTrigger.EVENT)
        _switch(MechanismKind.EVENT_BREAKER, MechanismMode.EXECUTING)

        self.assertEqual(len(halt.live_declarations(now=NOW)), 1)
        self.assertEqual(len(halt.blocking_declarations(now=NOW)), 1)


# --------------------------------------------------------------------------- #
# 开关
# --------------------------------------------------------------------------- #


class TestSwitchGating(TestCase):
    def test_an_event_row_does_not_block_while_the_switch_is_shadow(self):
        """**没有切换流水 = Shadow**（`RegimeMechanismSwitch.current` 的默认）。

        第②段落地时事件熔断开关默认就是关的，所以这一段一行声明都不该真的拦住下单。
        """
        _declare(trigger=HaltTrigger.EVENT)

        self.assertEqual(RegimeMechanismSwitch.objects.count(), 0)
        self.assertFalse(halt.halt_layers(SYMBOL, now=NOW).blocked)

    def test_an_event_row_blocks_once_the_switch_is_executing(self):
        _declare(trigger=HaltTrigger.EVENT)
        _switch(MechanismKind.EVENT_BREAKER, MechanismMode.EXECUTING)

        self.assertTrue(halt.halt_layers(SYMBOL, now=NOW).blocked)

    def test_turning_the_switch_back_off_stops_the_blocking(self):
        """关开关要能**立刻**停止拦截，但那些行必须留着。

        这就是「开关不写进声明表」的理由：若关开关等于解除声明，一开一关就会在流水里
        制造一堆「声明—解除」的假历史，而这张表是排查停用原因的依据。
        """
        _declare(trigger=HaltTrigger.EVENT)
        _switch(MechanismKind.EVENT_BREAKER, MechanismMode.EXECUTING)
        _switch(MechanismKind.EVENT_BREAKER, MechanismMode.SHADOW)

        self.assertFalse(halt.halt_layers(SYMBOL, now=NOW).blocked)
        self.assertEqual(HaltDeclaration.objects.count(), 1)
        self.assertEqual(halt.live_declarations(now=NOW)[0].closed_at, None)

    def test_the_blanket_row_has_no_switch_and_always_counts(self):
        """**保命档不在三个开关里**：高波动一到就生效，不需要人工确认。

        这条是「高波动算生效」的可执行形态。给它挂上 `REGIME_GATE`（第③段才接线、默认
        关）的话，保命档会被一个默认关闭的开关关掉——而它是阶段本身的性质。
        """
        self.assertIsNone(halt.HALT_TRIGGER_SWITCH[HaltTrigger.BLANKET])
        _declare(trigger=HaltTrigger.BLANKET, label="高波动")

        self.assertEqual(RegimeMechanismSwitch.objects.count(), 0)
        self.assertTrue(halt.halt_layers(SYMBOL, now=NOW).blocked)

    def test_the_deactivation_row_is_gated_by_the_regime_gate_switch(self):
        """策略停用决策归第③段的 `REGIME_GATE` 开关（映射先写下来，现在先求值）。"""
        self.assertIs(
            halt.HALT_TRIGGER_SWITCH[HaltTrigger.DEACTIVATION],
            MechanismKind.REGIME_GATE,
        )
        _declare(trigger=HaltTrigger.DEACTIVATION, scope=halt.strategy_scope("s-1"))

        self.assertFalse(halt.halt_layers(SYMBOL, "s-1", now=NOW).blocked)
        _switch(MechanismKind.REGIME_GATE, MechanismMode.EXECUTING)
        self.assertTrue(halt.halt_layers(SYMBOL, "s-1", now=NOW).blocked)


# --------------------------------------------------------------------------- #
# 结论的形状
# --------------------------------------------------------------------------- #


class TestVerdict(_HaltTestBase):
    def test_the_reason_names_every_layer(self):
        """同时生效时**每层都写出来**。

        只报第一层，会让人按报出来的那层去排查，而解除它之后单还是下不出去——那时会
        以为是别的问题（CONTEXT.md:177：用户眼里始终是一个集合）。
        """
        _declare(label="FOMC 议息", scope=halt.global_scope())
        _declare(
            trigger=HaltTrigger.BLANKET,
            label="高波动",
            scope=halt.global_scope(),
            opened_at=NOW - timedelta(hours=3),
        )

        verdict = halt.halt_layers(SYMBOL, now=NOW)

        self.assertEqual(len(verdict.layers), 2)
        self.assertIn("FOMC 议息（事件熔断，作用域 全市场（global））", verdict.reason)
        self.assertIn(
            "高波动（保命档（高波动），作用域 全市场（global））", verdict.reason
        )

    def test_block_reason_is_empty_when_nothing_blocks(self):
        self.assertEqual(halt.block_reason(SYMBOL, now=NOW), "")

        _declare(scope=halt.symbol_scope("BTC/USDT"))
        self.assertEqual(halt.block_reason(SYMBOL, now=NOW), "")

    def test_layers_carry_the_window_they_were_opened_for(self):
        """层上带着生效期：排查「为什么昨天没拦」时，要的是那条声明自己的时间，
        不是「现在是几点」。"""
        opened = NOW - timedelta(hours=5)
        expires = NOW + timedelta(hours=1)
        _declare(opened_at=opened, expires_at=expires)

        layer = halt.halt_layers(SYMBOL, now=NOW).layers[0]

        self.assertEqual(layer.opened_at, opened)
        self.assertEqual(layer.expires_at, expires)


class TestLayersTouching(_HaltTestBase):
    """一组「（品种, 策略）」上此刻在拦的层——`halt_layers` 的一对多版本（第③段 Q3）。

    调用方是窗口通知（`halt_notify.still_blocking`：一条消息的受众还会被什么拦住）。
    钉两件事：**判据与 `halt_layers` 同源**（同一个 `_matches`），以及**每层只出现一次**
    ——层是声明表的行，不是（行 × 命中的 pair）的组合；按 pair 拼出来的话，一条全市场
    声明会在「此刻共 N 层在拦」里被数成几个人那么多层。
    """

    def test_no_pairs_covers_no_layers(self):
        """一条活跃会话都没有 ⇒ 没有任何单子会被拦住。（也只有这一支不该查库：没有 pair
        就没有判定，`layers_touching` 直接返回。）"""
        _declare(scope=halt.global_scope())
        self.assertEqual(halt.layers_touching((), now=NOW), ())

    def test_the_criteria_are_the_same_as_halt_layers(self):
        """作用域三档的命中必须与下单口径逐档相同——两边各写一遍的话，通知里说的
        「还剩几层」与订单通路上真实的拦法会分叉，而两处看起来都正常。"""
        _declare(scope=halt.global_scope(), label="全市场那层")
        _declare(scope=halt.symbol_scope(SYMBOL), label="品种那层")
        _declare(scope=halt.strategy_scope("s-1"), label="策略那层")

        hit = halt.layers_touching(((SYMBOL, "s-1"),), now=NOW)
        self.assertEqual(
            {layer.label for layer in hit}, {"全市场那层", "品种那层", "策略那层"}
        )

        # 一条会话跑的是别的品种、别的策略 ⇒ 只剩全市场那层。
        self.assertEqual(
            [
                layer.label
                for layer in halt.layers_touching((("BTC/USDT", "s-2"),), now=NOW)
            ],
            ["全市场那层"],
        )

    def test_a_layer_that_hits_several_pairs_appears_once(self):
        _declare(scope=halt.global_scope(), label="全市场那层")

        hit = halt.layers_touching(
            ((SYMBOL, "s-1"), (SYMBOL, "s-2"), ("BTC/USDT", None)), now=NOW
        )

        self.assertEqual([layer.label for layer in hit], ["全市场那层"])

    def test_a_layer_that_blocks_nobody_is_not_in_the_set(self):
        _declare(scope=halt.symbol_scope("BTC/USDT"))

        self.assertEqual(halt.layers_touching(((SYMBOL, "s-1"),), now=NOW), ())
