"""行情阶段 gate 的开关（第③段单元 ③b）：确认页 + 那一次 INSERT + 随之而来的对账。

`test_gate.py` 钉判定（纯函数），`test_gate_run.py` 钉取数与落库，这个文件钉第三件事：
**人是怎么把这个机制打开、又是怎么关掉的**。与 `test_breaker_switch.py` 同构（那一档的
开关叫事件熔断），连分组都照抄；夹具则直接复用 `test_gate_run._Fixture`——两处各造一套
世界，迟早会出现「同一个页面在两个文件里算出两个数」。

六条性质，每一条坏了都不报警、只出错：

1. **确认页只读**：跑多少遍，一张表都不动。它是「先看一眼再决定」的那一眼。
2. **单一求值点**：页面上每个数出自同一次 `gate_run.preview`，正文与摘要共用它。两处
   各算一遍的表现是「页面上说 3 个、打开之后拦了 5 个」，而两边都正常。
3. **三种「打开也拦不住」各说各的后果**：`no_generation`（先跑 recompute）、
   冷启动 / 状态过期、`targets == 0`（真的没什么可拦）。含**保命档那一句**：高波动下
   「该停 0 个」不是「没什么可拦」。
4. **解除原因的词表只说这一档的事**：五个码逐条有人话；`regime_left` 与 `halt_sync` 那个
   同名**不同义**（这是 `halt_notify` 必须按 trigger 路由的理由）；渲染顺序固定，认不出
   的码排最后。
5. **档位不同才写流水，但无论档位变没变都要对一次账**：这是本档开关与 `flip_event_breaker`
   最明显的差别，也是「再敲一次」真的有用的原因（阶段说不清时关掉 gate 不会解除活行）。
6. **一个动作里只有一次 INSERT，且只碰自己那一档**：`KIND` 是常量；事件熔断与机制整体
   两个档位原样不动；`reason` 空 / `actor_name` 空在写任何东西之前就拒绝；永不 `DELETE`
   （决策行留着、`evidence` 一个字不动）；关闭时一个字都不写豁免表（撤防，不收回盾）。

第 5 条与第 6 条里的「永不 DELETE」「不碰豁免表」都是**纪律**而不是功能：它们没有报错
路径，只有事后翻表时才看得出来，所以各有一条用例专门钉住。
"""

from __future__ import annotations

import copy
import getpass
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command

from apps.regime import deactivation, deactivation_run, gate, halt, halt_sync
from apps.regime import gate_switch as gsw
from apps.regime import slice as sl
from apps.regime.management.commands import regime_gate
from apps.regime.models import (
    ActorKind,
    DecisionStatus,
    DeactivationDecision,
    DeactivationExemption,
    HaltDeclaration,
    HaltTrigger,
    MechanismKind,
    MechanismMode,
    RegimeMechanismSwitch,
)
from apps.regime.quant import BaseRegime
from apps.regime.tests.test_gate_run import (
    NOW,
    _Fixture as _GateRunFixture,
    days,
    hours,
    make_generation,
)

DOWNTREND = BaseRegime.DOWNTREND

#: 三种「这一轮算不出该停谁」的码。**从各层自己那里取，不在用例里写字符串字面量**：
#: 这一层要钉的是「页面把它原样报出来了吗」，不是「那个码拼成什么字」——钉字面量的话，
#: 改个名会红在等号的一边，而红的原因看起来与「三种收场各说各的」这条纪律无关。
BLOCKED_COLD_START = deactivation.BLOCKED_COLD_START
BLOCKED_STALE_STATE = deactivation.BLOCKED_STALE_STATE
SKIPPED_NO_GENERATION = deactivation_run.SKIPPED_NO_GENERATION

#: 页面上那句「这一步本身不改变任何东西」的两个方向。**命令行的用例拿它当渲染锚**
#: （照 `test_breaker_switch.PAGE_TITLE` 的用法）：命令一旦自己拼字符串，命令行看到的数
#: 与聊天里看到的数迟早分家。
PAGE_TITLE = gsw._OPENING
CLOSING_TITLE = gsw._CLOSING

#: 「一个都拦不住」的四种成因里，两种会印出这一句（真的没什么可拦 / 全在豁免期）。
NOTHING_WILL_BLOCK = "⚠️ 一个都不会拦"


class _Fixture(_GateRunFixture):
    """在 ③c 的夹具上加两件事：一个「阶段说得清、有一条该停的决策行」的世界，一个 `flip`。"""

    def live_world(
        self,
        *,
        regime: str = DOWNTREND,
        strategy=None,
        state: str = sl.STATE_UNFIT,
        reason: str = "本阶段样本偏少",
        judgement: bool = True,
        switch: bool = False,
        extra_cells: tuple = (),
    ):
        """造一个**正常一轮**的世界：当前代里那一格判着不适配，于是这一行该停。

        `switch=True` 时预先落一条开关流水（档位=执行态）。确认页与 `flip` 的用例都不该
        用它——页面上那些「假设此刻打开」的数只有在档位真是 Shadow 时才是那个意思，而
        `flip` 自己会写那一条。

        `extra_cells` 是给**别的被管策略**补的格子（`make_generation` 的 `cells` 格式）。
        issue #51 之后「按阶段暂停开新仓」那一路要为**每一个**被管策略判，而判不了（当前代
        里没有这一格）就是悬置——所以「真的没什么可拦」这种世界必须让另一个被管策略也有一格
        判着 `fit`，只给主角一格的话，配角会以「判不了」的身份被拦下，而页面上的数看起来
        完全正常。
        """
        strategy = strategy or self.alpha
        if switch:
            self.switch()
        if judgement:
            self.judged(regime=regime)
        make_generation(
            cells=(
                (strategy, regime, {"state": state, "reason": reason}),
                *extra_cells,
            )
        )
        return self.decision(strategy, regime=regime)

    # --- 两个出口 ---------------------------------------------------------- #

    def preview(self, *, closing: bool = False):
        """确认页。与命令行走的是同一个函数。"""
        return gsw.page(closing=closing, now=NOW)

    def flip(
        self,
        to_mode: MechanismMode = MechanismMode.EXECUTING,
        *,
        actor_kind: ActorKind = ActorKind.CLI,
        actor_name: str = "ops",
        reason: str = "测试",
        now=None,
    ) -> gsw.Flip:
        return gsw.flip_regime_gate(
            to_mode,
            actor_kind=actor_kind,
            actor_name=actor_name,
            reason=reason,
            now=now or NOW,
        )

    def event_row(self) -> HaltDeclaration:
        """一条**别的档**的活声明（事件熔断，品种作用域）。它不是本档管的，页面不该数它。"""
        return HaltDeclaration.objects.create(
            trigger=HaltTrigger.EVENT.value,
            scope=halt.symbol_scope("BTC/USDT"),
            label="事件熔断：FOMC 议息",
            opened_at=hours(-5),
            reason="测试",
            actor_kind=ActorKind.TASK.value,
            actor_name="任务",
        )


# --------------------------------------------------------------------------- #
# 性质 4：解除原因的词表
# --------------------------------------------------------------------------- #


class TestTheWordList(_Fixture):
    """五个解除原因码只在 `gate._close_reason` 里算出来，人话只在本模块里写一遍。"""

    #: `gate._close_reason` 的全部取值。写在这里而不是从 `GATE_CLOSE_REASON_DISPLAY`
    #: 反推：反推的话，词表少一条、判定层的码多一条，两种用例都会「通过」。
    CODES = (
        gate.CLOSE_REGIME_LEFT,
        gate.CLOSE_BECAME_FIT,
        gate.CLOSE_EXEMPTED,
        gate.CLOSE_STRATEGY_GONE,
        gate.CLOSE_GATE_CLOSED,
    )

    def test_every_code_the_judgement_layer_can_emit_has_a_word(self):
        self.assertEqual(set(gsw.GATE_CLOSE_REASON_DISPLAY), set(self.CODES))
        for code in self.CODES:
            with self.subTest(code=code):
                self.assertNotEqual(gsw.gate_close_reason_display(code), code)

    def test_an_unknown_code_comes_back_verbatim(self):
        """认不出就原样返回：词表少一条的表现是「消息里出现一个短码」，而不是
        「消息里出现一句错的解释」。"""
        self.assertEqual(gsw.gate_close_reason_display("zzz"), "zzz")

    def test_the_same_short_code_means_something_else_next_door(self):
        """`regime_left` 在本档是「这条决策行记的阶段已不是当前阶段」，在事件熔断那一档是
        「阶段已离开高波动」（保命档的话）。同名是两条线各说各的话的巧合，不是同一个概念。

        这一条钉的是 `halt_notify` 将来必须**按 trigger 路由**的理由：一把抓地调
        `halt_sync.close_reason_display`，那条保命档的文案会被贴到一条策略档声明上。
        """
        self.assertEqual(gate.CLOSE_REGIME_LEFT, halt_sync.CLOSE_REASON_REGIME_LEFT)
        self.assertNotEqual(
            gsw.gate_close_reason_display(gate.CLOSE_REGIME_LEFT),
            halt_sync.close_reason_display(halt_sync.CLOSE_REASON_REGIME_LEFT),
        )

    def test_the_rendering_order_is_fixed_and_an_unknown_code_goes_last(self):
        ordered = [gsw._code_sort_key((code, 1)) for code in gsw._CODE_ORDER]
        self.assertEqual(ordered, sorted(ordered))
        self.assertEqual(len(set(ordered)), len(gsw._CODE_ORDER))
        self.assertGreater(
            gsw._code_sort_key(("zzz", 1)), gsw._code_sort_key((gsw._CODE_ORDER[-1], 1))
        )

    def test_the_page_renders_in_the_fixed_order_not_in_the_table_s_order(self):
        """两条活行、两个不同的解除原因，而它们在表里的先后与渲染顺序**相反**。

        按字典序或按查询顺序渲染的话，同一组解除在不同的数据下会写成不同的行，而那正是
        「今天和昨天有什么不同」读不出来的形态。
        """
        # gamma 的实现类没注册 ⇒ 离场（`strategy_gone`，`_CODE_ORDER` 里排第 4）；
        # alpha 那一格翻成 `fit` ⇒ 重新适配（`became_fit`，排第 2）。
        self.judged()
        gone = self.live_row(self.gamma)  # 先建，于是它在表里排在前
        readopted = self.live_row(self.alpha)
        self.decision(self.gamma)
        self.decision(self.alpha, status=DecisionStatus.APPLIED.value)
        make_generation(cells=((self.alpha, DOWNTREND, {"state": sl.STATE_FIT}),))

        data = gsw.confirmation(now=NOW)

        self.assertEqual(
            [code for code, _ in data.closing_by_code],
            [gate.CLOSE_BECAME_FIT, gate.CLOSE_STRATEGY_GONE],
        )
        self.assertEqual(data.live_declarations, 2)
        self.assertEqual(data.closing, 2)
        # 按原因码分档渲染的只有**打开方向**那一行（`_declaration_line`）：关闭方向的行
        # 说的是「本轮按『gate 已回 Shadow』解除」这一件事，那条路上五个码只可能落到一个
        # （`gate.derive` 的 Shadow 分支把所有活行都记成 `gate_closed`），所以它不分档。
        # 在这一页上（假设打开）按下关闭方向那一行去问，问的是另一件不存在的事。
        line = gsw._declaration_line(data)
        self.assertLess(
            line.index(gsw.gate_close_reason_display(gate.CLOSE_BECAME_FIT)),
            line.index(gsw.gate_close_reason_display(gate.CLOSE_STRATEGY_GONE)),
        )
        # 短码一个都不该出现在给人看的那一行里。
        for code in self.CODES:
            with self.subTest(code=code):
                self.assertNotIn(code, line)
        for row in (gone, readopted):
            self.assertIsNone(row.closed_at)  # 确认页只读


# --------------------------------------------------------------------------- #
# 性质 1 + 2：确认页
# --------------------------------------------------------------------------- #


class TestTheRenderedPage(_Fixture):
    def setUp(self):
        super().setUp()
        self.world = self.live_world()

    def test_it_never_writes_anything(self):
        """跑多少遍，一张表都不动——它是「先看一眼再决定」的那一眼。"""
        for _ in range(2):
            self.preview()
            self.preview(closing=True)

        self.assertEqual(RegimeMechanismSwitch.objects.count(), 0)
        self.assertEqual(HaltDeclaration.objects.count(), 0)
        self.assertEqual(
            DeactivationDecision.objects.get().status, DecisionStatus.SUGGESTED.value
        )

    def test_the_body_and_the_summary_come_from_one_evaluation(self):
        """正文与摘要出自**同一次**求值：`page()` 只调一次 `confirmation`，而且 `data`、
        正文、摘要引用的就是它返回的那**一个**对象。

        各算一遍的话，两次查询之间只要有人翻了一下开关，回给用户的那句话与写进流水的
        那句话就会写着两个都出自本模块、却互相矛盾的数。
        """
        # **显式注入返回值**，而不是 `patch.object(..., wraps=...)`：带 `wraps` 的 mock
        # 其 `return_value` 恒是 `sentinel.DEFAULT`，于是「拿到的是不是同一个对象」这句话
        # 在 `wraps` 之下**根本写不出来**（断言 `is spy.return_value` 永远不成立，而它看起来
        # 只是「实现没共用返回值」）。注入一个真实的快照当哨兵，才真的钉住了这一点——
        # 「两次各自算出来恰好相等」那层窗户纸，只要有人给 `page` 加一个会漂的输入就破了。
        snapshot = gsw.confirmation(now=NOW)
        with patch.object(gsw, "confirmation", return_value=snapshot) as spy:
            briefing = self.preview()
        self.assertEqual(spy.call_count, 1)
        self.assertEqual(spy.call_args.kwargs["now"], NOW)
        self.assertIs(briefing.data, snapshot)

    def test_the_summary_is_the_word_for_word_thing_that_goes_into_the_reason(self):
        """摘要就是写进 `RegimeMechanismSwitch.reason` 的那句话，所以它是纯文本：
        不换行、不出现 markdown 强调符（收件人可能是即时消息客户端）。"""
        for closing in (False, True):
            with self.subTest(closing=closing):
                summary = self.preview(closing=closing).summary
                self.assertNotIn("**", summary)
                self.assertNotIn("\n", summary)
                self.assertTrue(summary.strip())

    def test_the_two_directions_are_not_the_same_page(self):
        """两个方向问的不是同一件事：打开方向报「会拦住谁」，关闭方向报「不再拦谁」。

        否则事后读流水的人分不出哪一条是撤防。
        """
        opening = self.preview()
        closing = self.preview(closing=True)

        self.assertIn(PAGE_TITLE, opening.body)
        self.assertIn(CLOSING_TITLE, closing.body)
        self.assertIn("操作：/regime gate off", closing.body)
        self.assertIn("人工打开行情阶段 gate", opening.summary)
        self.assertIn("人工关闭行情阶段 gate", closing.summary)
        self.assertNotEqual(opening.body, closing.body)
        self.assertNotEqual(opening.summary, closing.summary)
        # 数字是同一份：方向只改措辞，不改取数。
        self.assertEqual(opening.data, closing.data)

    def test_the_mode_line_reads_the_tier_from_the_table(self):
        """档位是从流水表现读的，不是写死的。写死的话，命令落了库而页面仍显示 Shadow，
        一边说「打开了」一边说「此刻不拦任何人」，两边都正常。"""
        self.assertIn(f"当前档位：{MechanismMode.SHADOW.display}", self.preview().body)
        self.switch(MechanismMode.EXECUTING)
        body = self.preview().body
        self.assertIn(f"当前档位：{MechanismMode.EXECUTING.display}", body)
        self.assertNotIn("假设此刻打开", body)

    def test_the_shadow_direction_says_what_it_is_previewing(self):
        """还没开的时候，页面上那些数是**假设**出来的——不说出来就成了「机制此刻在拦」。

        「这个档是假的」由 `as_if_open` 那一位说，不由 `switch_at` 的空值说。假设打开的这一
        路上 `switch_at` 是**假设的那个时刻**（`gate_run.preview` 把 `as_if_open_at` 与取数
        钉成同一个时刻，那是它存在的全部理由），不是 `None`：`Round.switch_at` 记的是
        「机制从哪一刻起有理由拦」，而「如果此刻打开」这句话的答案就是此刻。写成 `None`
        的话，页面上「打开之后会拦住谁」这一栏就少了它唯一的时间锚点。
        """
        data = self.preview().data
        self.assertTrue(data.as_if_open)
        self.assertEqual(data.switch_at, NOW)
        self.assertEqual(data.mode, MechanismMode.SHADOW)
        self.assertIn("假设此刻打开", self.preview().body)

    def test_the_body_echoes_the_numbers_of_this_very_round(self):
        """两路的条数**各报各的**（issue #51）：这一轮写的是 alpha 的停用声明 + beta 的暂停
        声明（beta 无格 ⇒ 悬置），而两个数分属两句话。

        合起来数的那一版（`declared = len(plan.declarations)`）会让同一屏上同时出现「该停
        1 个」与「写/刷新 2 条」——多出来的那条在这张页面上没有对应的「该停」可对，读的人
        只会以为取数坏了；而真正丢掉的信息是**两档的解除方式不同**（一个要人工恢复、一个
        随阶段自己翻回去），那才是这两句话必须分开的理由。
        """
        data = self.preview().data
        self.assertEqual(data.targets, 1)
        # 停用路：alpha 这一条。下面那条恒等式是本栏存在的理由，它必须重新成立。
        self.assertEqual(data.declared, 1)
        self.assertEqual(data.declared, data.targets - data.exempt)
        # 暂停路：beta 无格 ⇒ 悬置，各一条。它**不**进上面那个数。
        self.assertEqual(data.suspended, 1)
        self.assertEqual(data.exempt, 0)
        body = self.preview().body
        self.assertIn(f"该停 {data.targets} 个", body)
        self.assertIn(f"本轮写/刷新 {data.declared} 条", body)
        self.assertIn(f"此刻活着 {data.live_declarations} 条", body)
        # 两句必须**各自与自己的数对齐**（都用 f-string 里的同一个 `data` 拼）：这一轮两个数
        # 恰好都是 1，所以「本轮写/刷新 N 条」那句在正文里出现两次——只断言那一句的话，停用档
        # 那一行的数写错了也照样绿。带上各自的标签，钉子才落在那句话上。
        self.assertIn(
            f"停止声明（策略停用决策档）：此刻活着 {data.live_declarations} 条；"
            f"本轮写/刷新 {data.declared} 条",
            body,
        )
        self.assertIn(f"按阶段暂停开仓：本轮写/刷新 {data.suspended} 条", body)

    def test_a_live_row_of_another_trigger_is_not_counted(self):
        """本档的数只说本档的事：事件熔断那一行既不算「活着的策略档声明」，也不算幽灵行
        （它有自己的写入方，本档一个字都不该说它）。"""
        self.event_row()

        data = self.preview().data

        self.assertEqual(data.live_declarations, 0)
        self.assertEqual(data.orphan, 0)
        self.assertEqual(data.closing, 0)

    def test_the_closing_summary_does_not_requery(self):
        """关闭方向的摘要**收一份已经算好的快照**：它要在开关翻过去之前就写好当 `reason`，
        所以它只能来自翻之前那一次求值，不能自己再查一次。"""
        data = self.preview(closing=True).data
        with self.assertNumQueries(0):
            gsw.closing_summary(data)


# --------------------------------------------------------------------------- #
# 性质 3：三种「打开也拦不住」
# --------------------------------------------------------------------------- #


class TestWhatThePageSaysWhenItWouldNotBlock(_Fixture):
    def test_a_cold_start_says_the_round_will_do_nothing(self):
        """先跑 recompute 的那种「没法问」在这里也是一句话，而不是一次静默的空转动。"""
        make_generation(cells=((self.alpha, DOWNTREND, {"state": sl.STATE_UNFIT}),))
        self.decision(self.alpha)

        data = self.preview().data
        body = self.preview().body

        self.assertEqual(data.skipped, BLOCKED_COLD_START)
        self.assertIn("⚠️ 本轮一个字都不会动", body)
        self.assertIn(data.skipped_display, body)
        self.assertEqual(data.declared, 0)
        self.assertEqual(data.targets, 0)
        # 摘要那一侧也要说得出同一件事：它进流水，事后读流水的人只有那一句话。
        self.assertIn("一个字都没动", self.preview().summary)

    def test_no_generation_names_the_command_to_run_first(self):
        """「一代池化表都没有」不是「没有该停的」——它连问都问不了，而按空集解除会把每条
        活行都解除掉。页面上给出的是那一条要跑的命令。"""
        self.judged()
        self.decision(self.alpha)
        live = self.live_row(self.alpha)

        data = self.preview().data

        self.assertEqual(data.skipped, SKIPPED_NO_GENERATION)
        self.assertIn("recompute_regime_slices", data.skipped_display)
        self.assertIn("recompute_regime_slices", self.preview().body)
        self.assertNotEqual(data.skipped, "")
        live.refresh_from_db()
        self.assertIsNone(live.closed_at)

    def test_a_stale_state_says_so_with_the_lifecycle_word(self):
        self.live_world(judgement=False)
        self.judged(effective_at=NOW - timedelta(days=4))

        data = self.preview().data

        self.assertEqual(data.skipped, BLOCKED_STALE_STATE)
        self.assertIn("过期", data.skipped_display)

    def test_nothing_to_stop_is_not_the_same_as_cannot_tell(self):
        """「真的没什么可拦」与「算不出来」是两件事：前者 `skipped` 是空串，后者不是。

        合成一件事的话，冷启动那几轮会在页面上写着「当前阶段没有被判为不适配的被管策略」
        ——一句听起来完全正常的假话。

        issue #51：暂停路为**每个**被管策略判，判不了就是悬置。所以「真的没什么可拦」这个
        世界现在要求每一个被管策略都有格判着 `fit`，不能让另一个策略以「无格」的身份顶掉
        这句话——那样页面报出的「会拦住 N 个」才是这个用例真正要看的那个 0。
        """
        self.live_world(
            state=sl.STATE_FIT,
            extra_cells=((self.beta, DOWNTREND, {"state": sl.STATE_FIT}),),
        )

        data = self.preview().data

        self.assertEqual(data.skipped, "")
        self.assertEqual(data.targets, 0)
        self.assertEqual(data.declared, 0)
        self.assertIn(NOTHING_WILL_BLOCK, self.preview().body)

    def test_a_suspension_declaration_takes_back_the_nothing_will_block_line(self):
        """`targets == 0` 不再等于「一个都不会拦」（issue #51）：暂停路此刻可能正拦着一批策略。

        与上面那条的差别就是这一条要钉的东西：上面那个世界**每一个**被管策略都有 `fit` 格，
        所以暂停路也一条都不产出；这里 beta 没有格（判不了 ⇒ 悬置），于是「该停 0 个」与
        「暂停路拦着 1 个」同时为真——而那句「⚠️ 一个都不会拦」在这种世界里是一句漏掉一半
        事实的假话：用户读到它就不会再去看那批被暂停的策略，而它们照样拦着开仓。

        所以这一句必须消失，**而暂停路自己那一行必须出现**：它能同时回答「有多少条」与
        「要不要人来恢复」，那正是看这一页的人此刻要做的判断。
        """
        self.live_world(state=sl.STATE_FIT)

        data = self.preview().data
        body = self.preview().body

        self.assertEqual(data.skipped, "")
        self.assertEqual(data.targets, 0)
        self.assertEqual(data.declared, 0)
        self.assertEqual(data.suspended, 1)
        self.assertNotIn(NOTHING_WILL_BLOCK, body)
        self.assertIn(f"按阶段暂停开仓：本轮写/刷新 {data.suspended} 条", body)
        # 那一行必须当场把两档的差别说出来——报数之外，它要回答的是「要不要人工恢复」，
        # 而这两档的答案相反（停用要人给豁免或改判据，暂停随阶段自己翻回去）。
        self.assertIn("随阶段自动翻转，不需要人工恢复", body)

    def test_all_targets_exempt_says_the_human_is_holding_them(self):
        """打开也拦不住，因为人按着——这一句与上一条不能互相借用措辞。

        issue #51：同一个理由——另一个被管策略在这一代里判着 `fit`，才谈得上「全在人工
        豁免期」：让它以「无格」的身份站着的话，暂停路会拦住它，而这一页说的「一个都不会拦」
        就成了一句漏掉了一半事实的话。
        """
        self.exemption(self.alpha)
        self.live_world(extra_cells=((self.beta, DOWNTREND, {"state": sl.STATE_FIT}),))

        data = self.preview().data

        self.assertEqual(data.targets, 1)
        self.assertEqual(data.exempt, 1)
        self.assertEqual(data.declared, 0)
        body = self.preview().body
        self.assertIn("全在人工豁免期", body)
        self.assertNotIn("没什么可拦", body)

    def test_a_blanket_regime_does_not_claim_there_is_nothing_to_stop(self):
        """保命档（高波动）期间「该停 0 个」不是「没什么可拦」：那一层的全场停用由保命档
        自己负责，而它没有开关。两句话混用是要命的误读。

        格子落 `blanket` 这一档（保命档下池化层不做适用性判断），于是「该停 0 个」是
        **判据真的算出 0**；拿一个 `unfit` 格子被别的什么挡掉凑出的 0 会让这条用例去钉
        一个与保命档无关的巧合。
        """
        self.live_world(regime=BaseRegime.HIGH_VOL, state=sl.STATE_BLANKET)

        data = self.preview().data
        body = self.preview().body

        self.assertTrue(data.blanket)
        self.assertEqual(data.targets, 0)
        self.assertEqual(data.declared, 0)
        # `_no_effect_lines` 在保命档下**一个字都不说**：这一档不需要「一个都不会拦」
        # 那句解释——拦不拦根本不归本开关管，说了反而像「本开关放行了它们」。
        self.assertNotIn(NOTHING_WILL_BLOCK, body)
        self.assertIn("保命档", body)

    def test_the_closing_direction_says_the_blanket_has_no_switch(self):
        """「关掉就安全了」在保命档上是错的，而它错的那一半从别处看不见。

        **不钉「有没有出现『没有开关』这四个字」**：打开方向的 `_BLANKET_NOTE` 里也有这四
        个字，但它解释的是「为什么该停是 0」，不是「关掉之后会怎样」。那句话为真，钉错了
        却会让一条只该在关闭方向出现的警告被别处的巧合顶替掉。
        """
        self.live_world(regime=BaseRegime.HIGH_VOL, state=sl.STATE_BLANKET)

        closing = self.preview(closing=True)

        self.assertIn("关掉就安全了", closing.body)
        self.assertIn("保命档", closing.body)
        self.assertNotIn("关掉就安全了", self.preview().body)
        # 摘要进流水，事后读流水的人只有那一句话：两个方向都得在里面（模块 docstring 说的
        # 「正文与摘要里都单独写一句」）。
        self.assertIn("保命档", closing.summary)
        self.assertIn("保命档", self.preview().summary)


# --------------------------------------------------------------------------- #
# 性质 5 + 6：那一次 INSERT 与随之而来的对账
# --------------------------------------------------------------------------- #


class TestFlipRegimeGate(_Fixture):
    """本档开关只有一个写入方，而且它落完流水行**同步**对一次账。"""

    def test_the_flip_and_the_reconciliation_are_one_action(self):
        """打开的那一刻声明表就是对的：不是「先开关、等下一轮任务补上」。

        中间那段敞口不是「暂时没拦住」——它是一句**假话**：开关开着而声明表里没有行，
        读表的人只会得出「此刻没有该停的策略」。
        """
        self.live_world()

        flip = self.flip()

        self.assertTrue(flip.changed)
        row = flip.row
        self.assertEqual(row.kind, MechanismKind.REGIME_GATE.value)
        self.assertEqual(row.from_mode, MechanismMode.SHADOW.value)
        self.assertEqual(row.to_mode, MechanismMode.EXECUTING.value)
        self.assertEqual(row.actor_kind, ActorKind.CLI.value)
        self.assertEqual(row.actor_name, "ops")
        self.assertEqual(row.at, NOW)
        # 同一次动作里对完了账：两张表的变化都是**这一轮**落下的。
        live = HaltDeclaration.objects.get(trigger=HaltTrigger.DEACTIVATION.value)
        self.assertEqual(live.scope, halt.strategy_scope(self.alpha.id))
        self.assertEqual(live.opened_at, NOW)
        self.assertIsNone(live.closed_at)
        # Q10：策略名进 `label`（人在即时消息里读到的作用域记号是一个 uuid，那是
        # `scope_display` 该待的地方，名字归 `label`）。
        self.assertIn(self.alpha.name, live.label)
        self.assertEqual(
            DeactivationDecision.objects.get().status, DecisionStatus.APPLIED.value
        )

    def test_pressing_the_same_button_again_reconciles_anyway(self):
        """档位没变 ⇒ 不写第二条流水；但**对账照样跑**。

        这是本档与事件熔断最明显的差别，也是「再敲一次」真的有用的全部理由：阶段说不清
        的那几轮开关翻不动声明表，等阶段说清楚之后人要能用同一条命令把表对干净——而那时
        开关已经在执行态了；按「档位没变就什么都不做」实现的话，这一步永远做不了。
        """
        self.live_world()
        self.flip()
        live = HaltDeclaration.objects.get(trigger=HaltTrigger.DEACTIVATION.value)
        # 判据翻面：新的一代里这一格重新适配。代与代之间判定不会翻面，所以「翻回来」必然
        # 落在一代新的表上——这正是 `attitude_since` 拿 `generation_at` 当候选的理由。
        make_generation(
            cells=((self.alpha, DOWNTREND, {"state": sl.STATE_FIT}),),
            finished_at=NOW + timedelta(minutes=1),
        )

        again = self.flip(now=NOW + timedelta(minutes=2))

        self.assertFalse(again.changed)
        self.assertIsNone(again.row)
        self.assertEqual(RegimeMechanismSwitch.objects.count(), 1)
        live.refresh_from_db()
        self.assertEqual(live.closed_at, NOW + timedelta(minutes=2))
        self.assertEqual(live.closed_reason, gate.CLOSE_BECAME_FIT)
        self.assertEqual(
            DeactivationDecision.objects.get().status, DecisionStatus.RELEASED.value
        )

    def test_an_empty_reason_or_actor_is_refused_before_anything_is_written(self):
        """名字与理由不是装饰：流水行是「谁、凭什么、把开关翻到哪」的唯一记录，缺一格就
        等于一次来源不可考的切换。

        **在写任何东西之前**拒绝。先写再校验的形态是「库里留下一条半截记录 + 一个异常」，
        而那条记录看起来完全正常——它是排查时第一眼会看到的东西。
        """
        self.live_world()

        for kwargs in (
            {"reason": ""},
            {"reason": "   "},
            {"actor_name": ""},
            {"actor_name": "   "},
        ):
            with self.subTest(**kwargs):
                with self.assertRaises(ValueError):
                    self.flip(**kwargs)
                self.assertEqual(RegimeMechanismSwitch.objects.count(), 0)
                self.assertEqual(HaltDeclaration.objects.count(), 0)
                self.assertEqual(
                    DeactivationDecision.objects.get().status,
                    DecisionStatus.SUGGESTED.value,
                )

    def test_it_only_ever_touches_its_own_tier(self):
        """`KIND` 是常量而不是参数（与 `breaker_switch` 同一个理由：做成参数就是在邀请
        下一个调用点顺手翻一个还没接线的开关）。另外两档的档位与流水必须原样不动。

        这一条不是形式主义：`MECHANISM` 那个档位管的是整个机制要不要参与求值，被本档顺手
        翻一次，表现是「拦住的东西与流水里记的那一档对不上」。
        """
        self.switch(MechanismMode.EXECUTING, kind=MechanismKind.EVENT_BREAKER)
        self.switch(MechanismMode.EXECUTING, kind=MechanismKind.MECHANISM)
        self.live_world()
        before = set(RegimeMechanismSwitch.objects.values_list("pk", flat=True))

        flip = self.flip()

        after = set(RegimeMechanismSwitch.objects.values_list("pk", flat=True))
        self.assertEqual(after - before, {flip.row.pk})
        self.assertEqual(flip.row.kind, MechanismKind.REGIME_GATE.value)
        for row in RegimeMechanismSwitch.objects.exclude(pk=flip.row.pk):
            with self.subTest(kind=row.kind):
                self.assertNotEqual(row.kind, MechanismKind.REGIME_GATE.value)
                self.assertEqual(row.to_mode, MechanismMode.EXECUTING.value)

    def test_closing_never_takes_back_the_human_exemption(self):
        """关闭是撤防，不是收回人给的盾：豁免表一个字都不写，重开时照旧按着。

        反过来写（关闭时把与窗口重叠的豁免一并收回）看起来更「干净」，代价是人在做一次
        与豁免无关的运维动作时把别人的盾弄丢了，而且丢得没有任何提示。
        """
        exemption = self.exemption(self.alpha)
        self.live_world()

        self.flip()  # 打开：该停，但人在豁免期
        # issue #51：豁免挡住的是停用路；暂停路照样会为 beta 落一条（它无格 ⇒ 悬置），
        # 所以「一条都不写」这句话现在要按策略来问——alpha 名下仍然一条都没有。
        self.assertEqual(HaltDeclaration.objects.count(), 1)
        self.assertEqual(
            HaltDeclaration.objects.filter(
                scope=halt.strategy_scope(self.alpha.id)
            ).count(),
            0,
        )
        # `_untouched`：一条机制从没碰过的**建议**不该被记成「机制解除过它」（它同时还在
        # `replay_run` 的重放集合里，那才是它该待的地方）。
        self.assertEqual(
            DeactivationDecision.objects.get().status, DecisionStatus.SUGGESTED.value
        )

        flip = self.flip(MechanismMode.SHADOW)

        self.assertTrue(flip.changed)
        self.assertEqual(DeactivationExemption.objects.count(), 1)
        exemption.refresh_from_db()
        self.assertIsNone(exemption.closed_at)
        self.assertEqual(exemption.closed_reason, "")
        self.assertEqual(exemption.expires_at, days(10))

    def test_it_never_deletes_a_decision_row_nor_touches_its_evidence(self):
        """纪律而不是功能：它没有报错路径，只有事后翻表时才看得出来。

        决策行是「机制曾经判过什么」的记录，`replay_run` 的重放集合就建在它的连续性上；
        `evidence` 是判定的冻结快照，回写 status 时顺手改一格，事后没人分得清「当时的判据
        是什么」与「现在是什么」。
        """
        decision = self.live_world()
        frozen = copy.deepcopy(decision.evidence)

        self.flip()
        self.flip(MechanismMode.SHADOW)

        self.assertEqual(DeactivationDecision.objects.count(), 1)
        decision.refresh_from_db()
        self.assertEqual(decision.evidence, frozen)


# --------------------------------------------------------------------------- #
# 离线兜底入口
# --------------------------------------------------------------------------- #


class _CliFixture(_Fixture):
    """命令行入口的夹具。`manage` 只返回 stdout——命令的产物就是它的输出与那两张表。"""

    def manage(self, *args) -> str:
        out = StringIO()
        # 命令自己取时钟（`timezone.now()`），而本文件的夹具世界挂在 `NOW` 上——一个写死的
        # 2026-06-01。不钉的话，每个命令行用例都跑在一个「判定早就过期」的世界里（今天与
        # NOW 隔着几个月），于是「打开之后拦住了谁」全部落空，而输出看起来完全正常
        # （`stale_state` 那一支照样印一整页）。这里钉的是命令模块里那个 `timezone`，
        # 也就是 `django.utils.timezone` 本身——命令把 `now` 一路显式传下去，所以一次
        # 补丁就够，不需要去 patch 下游任何一层。
        with patch.object(regime_gate.timezone, "now", return_value=NOW):
            call_command("regime_gate", *args, stdout=out, stderr=StringIO())
        return out.getvalue()


class TestTheManagementCommand(_CliFixture):
    """`/regime` 是主入口（CONTEXT.md 第 152 条：不引入 admin），命令行是离线兜底。

    两条路必须走同一个渲染口与同一个写入口。离线兜底自己拼一套字符串的话，「在服务器上
    看到的那一页」与「在聊天里看到的那一页」迟早写着两个都出自本模块、却互相矛盾的数。
    """

    def test_no_action_parameter_is_read_only(self):
        """不带 `--on` / `--off` 时是「看一眼」：它必须与确认页一样一个字都不写。"""
        self.live_world()

        out = self.manage()

        self.assertIn(PAGE_TITLE, out)
        self.assertIn("没有动作参数", out)
        self.assertNotIn(CLOSING_TITLE, out)
        self.assertEqual(RegimeMechanismSwitch.objects.count(), 0)
        self.assertEqual(HaltDeclaration.objects.count(), 0)
        self.assertEqual(
            DeactivationDecision.objects.get().status, DecisionStatus.SUGGESTED.value
        )

    def test_it_renders_the_page_through_the_shared_entry(self):
        """`page` 只被调一次。命令自己再算一遍「该停几个」是本模块最贵的分叉。"""
        self.live_world()

        with patch.object(gsw, "page", wraps=gsw.page) as spy:
            out = self.manage("--on", "--actor", "张三")

        self.assertEqual(spy.call_count, 1)
        self.assertIn(PAGE_TITLE, out)
        # `flip_regime_gate` 内部调的是 `gate_run.sync` 而不是 `page`，所以这一次计数不会被
        # 那一次对账污染——否则「命令多渲染了一次」与「对账顺路渲染了一次」就分不清了。

    def test_on_flips_to_executing_and_records_the_cli_actor(self):
        self.live_world()

        out = self.manage("--on", "--actor", "张三")

        row = RegimeMechanismSwitch.objects.get()
        self.assertEqual(row.kind, MechanismKind.REGIME_GATE.value)
        self.assertEqual(row.to_mode, MechanismMode.EXECUTING.value)
        self.assertEqual(row.actor_kind, ActorKind.CLI.value)
        self.assertEqual(row.actor_name, "张三")
        self.assertIn("已切换", out)
        # 流水行的 `reason` 就是那一页的摘要：事后读流水的人靠它知道当时看到的是什么。
        self.assertIn("人工打开行情阶段 gate", row.reason)
        # issue #51：命令落完流水就同步对了一次账，所以这一轮**两路**的行都在表里
        # （alpha 的停用声明 + beta 的暂停声明）；下面那一条钉住 alpha 那条确实落了地。
        self.assertEqual(HaltDeclaration.objects.count(), 2)
        self.assertEqual(
            HaltDeclaration.objects.filter(
                trigger=HaltTrigger.DEACTIVATION.value,
                scope=halt.strategy_scope(self.alpha.id),
            ).count(),
            1,
        )

    def test_the_actor_defaults_to_the_system_user(self):
        self.live_world()

        with patch.object(getpass, "getuser", return_value="本机用户"):
            self.manage("--on")

        self.assertEqual(RegimeMechanismSwitch.objects.get().actor_name, "本机用户")

    def test_off_uses_the_closing_page_and_the_closing_reason(self):
        self.live_world()
        self.manage("--on", "--actor", "张三")

        out = self.manage("--off", "--actor", "张三")

        self.assertIn(CLOSING_TITLE, out)
        self.assertIn("已切换", out)
        self.assertIn(MechanismMode.SHADOW.display, out)
        row = RegimeMechanismSwitch.objects.order_by("-at", "-id").first()
        self.assertEqual(row.to_mode, MechanismMode.SHADOW.value)
        self.assertIn("人工关闭行情阶段 gate", row.reason)

    def test_pressing_the_same_button_again_writes_no_second_row(self):
        self.live_world()
        self.manage("--on", "--actor", "张三")

        out = self.manage("--on", "--actor", "张三")

        self.assertEqual(RegimeMechanismSwitch.objects.count(), 1)
        self.assertIn("本来就是", out)

    def test_off_when_it_was_never_on_says_so_and_writes_nothing(self):
        self.live_world()

        out = self.manage("--off", "--actor", "张三")

        self.assertEqual(RegimeMechanismSwitch.objects.count(), 0)
        self.assertIn("本来就是", out)
