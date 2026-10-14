"""行情阶段 gate 的判定层（第③段单元 ③a）。

本文件要钉住的不是「哪条策略该停」——那是第①段池化的结论，`test_deactivation.py` 管着
——而是**把「该停」翻译成「拦」的那张三态表**：

    一条决策行 ×（阶段是否当前）×（判据是否仍成立）×（有没有在期豁免）
    → 该不该有一条声明行 / 那条决策行该写什么 status / 活行该记哪个解除原因

这张表的每一格都必须能在**没有数据库**的情况下单独钉住，这正是 `gate.py` 与
`gate_run.py` 切开的原因（Q1）：取数与落库是另一类风险，而它们要付
`transaction=True` 那 70 秒一次的代价。所以本文件全部是 `SimpleTestCase`——它不只是
「本轮没去建库」，它是**执行机制**：判定层一旦偷偷碰一次数据库，这里就红。

八条性质：

1. **声明 ⟺ 机制此刻在拦**：阶段对、判据仍成立、没有在期豁免，三条缺一不可。
2. **`status` 记的是机制做过什么**，不是结论对不对：声明了写 `applied`，撤回过写
   `released`；目标值等于旧值的行一个字都不写（条件更新的比对值因此总是新鲜的）。
3. **机制从没碰过的行不给它写 `released`**（`_untouched`）——否则那张表会开始记
   「它当时在拦」的假历史，而那些行正该留在 `replay_run` 的重放集合里。判据只看得见
   `status`：机制只写 `applied` / `released`，所以「还是 `suggested`」就是「没碰过」的
   全部含义，**与阶段无关**——四条收场路径一视同仁。
4. **`opened_at` 一律取库里事实里最晚的一个**，不取 `now()`：取 `now()` 会让
   `halt_sync._rewrite` 每 300 秒看见一次变化，于是 `opened_notified_at` 被反复清空、
   用户被反复通知同一条窗口开启（Q4）。
5. **Shadow 档不声明、也不写 status**，但活行照样解除（Q3）。
6. **三种「什么都不动」各有各的理由**：冷启动与状态过期（`deactivation.BLOCKED_*`）是
   「说不清现在是什么阶段」；保命档（高波动）是**说得清、而这一层不该动手**——它要是落进
   逐行判定，每一行都会被判成「阶段离开」而解除，而那是假话（高波动是叠加层，不是离开了
   原阶段，CONTEXT.md 第 115 条）。三者共同的一点：**都不是「按空集解除」**。
7. **五个解除原因码**互不重复、取值不飘，且有一处**故意的短码重名**（`regime_left`）：
   它只有在 `halt_notify` 按触发源路由时才是安全的。
8. **给用户看的那两句话自带主语**：`label` 进下单拒绝理由与 `query_halt`，`reason` 进
   通知正文；正文里没有 `label`，所以策略名必须出现在 `reason` 里（Q10）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from django.test import SimpleTestCase

from apps.regime import deactivation as dea
from apps.regime import gate as g
from apps.regime import halt_sync
from apps.regime.models import DecisionStatus, HaltTrigger
from apps.regime.quant import BaseRegime

DOWNTREND = BaseRegime.DOWNTREND.value
UPTREND = BaseRegime.UPTREND.value

DAY0 = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)

#: 与 `apps/riskguard/tests/test_guard_halt_strategy_scope.py` 的 `LABEL` 逐字相同：
#: 那边是**读**这条声明的人，这边是**写**它的人，两处对不上就是「下单拒绝理由里
#: 认不出是哪条策略」。
LABEL = "策略停用决策：趋势跟踪（下行趋势表现不佳）"


def at(days: int = 0, hours: int = 0) -> datetime:
    return DAY0 + timedelta(days=days, hours=hours)


def row(
    strategy_id: int,
    *,
    regime: str = DOWNTREND,
    status: str = g.STATUS_SUGGESTED,
    warrant: str = g.STILL_TARGET,
    name: str = "",
    cell_reason: str = "",
) -> g.DecisionRef:
    """一条决策行的字面量。`decision_id` 只为回写定位，用可读的假值就够了。"""
    return g.DecisionRef(
        decision_id=f"d-{strategy_id}",
        strategy_id=strategy_id,
        strategy_name=name or f"策略{strategy_id}",
        regime=regime,
        status=status,
        warrant=warrant,
        cell_reason=cell_reason,
    )


def situation(
    *,
    gate_open: bool = True,
    regime: str | None = DOWNTREND,
    blocked: str = "",
    exemptions: dict | None = None,
    switch_at: datetime | None = at(),
    regime_effective_at: datetime | None = at(),
    generation_at: datetime | None = at(),
) -> g.Situation:
    """这一轮的世界状态。默认是一轮**最普通**的：开关开着、阶段是下行趋势、
    三个时刻都落在 `DAY0`（于是 `opened_at` 的期望值一眼可读）。

    默认值是 `at()` 而**不是** `None`：`None` 在 `Situation` 里是有意义的值
    （「这一项取不到」），所以这里不能拿它当「没传」。要表达「取不到」就显式传 `None`。
    """
    return g.Situation(
        gate_open=gate_open,
        switch_at=switch_at,
        regime=regime,
        regime_effective_at=regime_effective_at,
        generation_at=generation_at,
        exemptions=exemptions or {},
        blocked=blocked,
    )


def derive(rows, **kwargs) -> g.GatePlan:
    """把 `derive` 的调用压到一行，好让每个用例只剩它真正在问的那一件事。"""
    return g.derive(situation(**kwargs), rows)


# --------------------------------------------------------------------------- #
# 性质 7：词表
# --------------------------------------------------------------------------- #


class TestTheVocabularyDoesNotDrift(SimpleTestCase):
    def test_the_three_status_constants_are_the_orm_ones(self):
        """`gate.py` 不能 import ORM 模型，只能把取值抄一遍——抄错的表现是 gate 写一个
        `DecisionStatus` 里不存在的值，而 `status` 在模型上是裸 `CharField`：不报错、
        不回滚，只是从此「这条决策到底停没停」多了一个谁也认不出来的答案。"""
        self.assertEqual(g.STATUS_SUGGESTED, DecisionStatus.SUGGESTED.value)
        self.assertEqual(g.STATUS_APPLIED, DecisionStatus.APPLIED.value)
        self.assertEqual(g.STATUS_RELEASED, DecisionStatus.RELEASED.value)

    def test_every_close_reason_is_a_slug_and_they_are_pairwise_distinct(self):
        codes = (
            g.CLOSE_REGIME_LEFT,
            g.CLOSE_BECAME_FIT,
            g.CLOSE_EXEMPTED,
            g.CLOSE_GATE_CLOSED,
            g.CLOSE_STRATEGY_GONE,
        )
        self.assertEqual(len(set(codes)), 5, codes)
        for code in codes:
            self.assertRegex(code, r"^[a-z][a-z_]*$")

    def test_regime_left_is_the_only_code_shared_with_the_blanket_tier(self):
        """**同一张表里的两个不同意思共用了一个短码，而且只此一处。**

        `halt_sync` 的 `regime_left` 是保命档的话（「阶段已离开高波动」），本层的是
        「这条策略在别的阶段判过不适用，那已经不是当前阶段了」。短码重名而语义不同，
        **只有在展示层按触发源路由时才是安全的**——那条测试在 `test_halt_notify.py`
        里钉着（Q7）。这里钉两件事：这个重名是**故意的**（不是抄错的），且**只有它
        一个**——多出第二个就意味着 `halt_notify` 的路由要重新想一遍。
        """
        gate_codes = {
            g.CLOSE_REGIME_LEFT,
            g.CLOSE_BECAME_FIT,
            g.CLOSE_EXEMPTED,
            g.CLOSE_GATE_CLOSED,
            g.CLOSE_STRATEGY_GONE,
        }
        blanket_codes = {
            halt_sync.CLOSE_REASON_WINDOW_ENDED,
            halt_sync.CLOSE_REASON_EVENT_CANCELLED,
            halt_sync.CLOSE_REASON_NO_LONGER_COVERS,
            halt_sync.CLOSE_REASON_REGIME_LEFT,
        }
        self.assertEqual(gate_codes & blanket_codes, {g.CLOSE_REGIME_LEFT})

    def test_the_label_prefix_is_the_trigger_display_name(self):
        """`label` 进的是 `HaltLayer.text`（下单拒绝理由与 `query_halt`），人在那里看到
        的抬头必须是「策略停用决策」——与 `HaltTrigger.DEACTIVATION.display` 同一套话，
        否则同一条停用在两个地方有两个名字。"""
        self.assertEqual(g.LABEL_PREFIX, f"{HaltTrigger.DEACTIVATION.display}：")


# --------------------------------------------------------------------------- #
# 性质 1 + 2：那张三态表
# --------------------------------------------------------------------------- #


class TestWhatEarnsADeclaration(SimpleTestCase):
    """三个条件缺一不可，且每一格都同时决定 status 与解除原因。"""

    def test_a_matching_still_true_row_is_declared_and_marked_applied(self):
        plan = derive([row(1, name="趋势跟踪", cell_reason="下行趋势表现不佳")])

        self.assertEqual([d.strategy_id for d in plan.declarations], [1])
        self.assertEqual(plan.declarations[0].opened_at, at())
        self.assertIsNone(plan.declarations[0].expires_at)
        self.assertEqual(plan.close_reasons, {})
        self.assertEqual(
            [(w.status, w.old_status) for w in plan.statuses],
            [(g.STATUS_APPLIED, g.STATUS_SUGGESTED)],
        )
        self.assertEqual(plan.blocked, "")
        self.assertEqual(plan.note, "")

    def test_an_already_applied_row_is_declared_again_without_rewriting_status(self):
        """幂等：第二轮看见的还是同一条声明，而 `status` 一个字都不写。

        写下去虽然值一样，却会让 `_rewrite` 看见一次变化（它比对的是行本身），
        于是每 300 秒清一次 `opened_notified_at`。
        """
        plan = derive([row(1, status=g.STATUS_APPLIED)])

        self.assertEqual(len(plan.declarations), 1)
        self.assertEqual(plan.statuses, ())

    def test_a_released_row_that_comes_back_is_declared_and_marked_applied_again(self):
        """`released` 的意思是「机制**撤回过**这条」，不是「这条不成立」——阶段回来
        之后它回到 `applied`，并且回写带着旧值当比对条件。"""
        plan = derive([row(1, status=g.STATUS_RELEASED)])

        self.assertEqual(len(plan.declarations), 1)
        self.assertEqual(
            [(w.status, w.old_status) for w in plan.statuses],
            [(g.STATUS_APPLIED, g.STATUS_RELEASED)],
        )

    def test_a_row_from_another_regime_is_not_declared(self):
        plan = derive([row(1, regime=UPTREND, status=g.STATUS_APPLIED)])

        self.assertEqual(plan.declarations, ())
        self.assertEqual(plan.close_reasons, {1: g.CLOSE_REGIME_LEFT})

    def test_a_row_whose_judgement_lapsed_is_not_declared(self):
        plan = derive([row(1, warrant=g.LAPSED, status=g.STATUS_APPLIED)])

        self.assertEqual(plan.declarations, ())
        self.assertEqual(plan.close_reasons, {1: g.CLOSE_BECAME_FIT})

    def test_a_row_whose_strategy_left_the_managed_set_says_so(self):
        """`gone` 与 `lapsed` 必须分得开：一个说「它已经不归这套机制管了」，一个说
        「判据变了」。合成布尔之后，用户会看到一条策略被「重新适配」地解除，而其实
        它已经不在被管集合里了。"""
        plan = derive([row(1, warrant=g.GONE, status=g.STATUS_APPLIED)])

        self.assertEqual(plan.close_reasons, {1: g.CLOSE_STRATEGY_GONE})

    def test_an_in_force_exemption_holds_the_row_back(self):
        """豁免在期 ⇒ 关掉声明行、记 `exempted`、决策行转 `RELEASED`（Q5）。

        关掉声明行不是形式：`halt_layers` 只看得见声明表，留着一条活行就是「用户按了
        豁免、机制照拦」，于是「恢复豁免」变成一个按了没反应的动作。
        """
        exemptions = {1: g.Exemption(in_force=True, ends_at=at(days=3))}
        plan = derive([row(1, status=g.STATUS_APPLIED)], exemptions=exemptions)

        self.assertEqual(plan.declarations, ())
        self.assertEqual(plan.close_reasons, {1: g.CLOSE_EXEMPTED})
        self.assertEqual(
            [(w.status, w.old_status) for w in plan.statuses],
            [(g.STATUS_RELEASED, g.STATUS_APPLIED)],
        )

    def test_an_exemption_that_already_ended_does_not_hold_the_row_back(self):
        """已结束的豁免不再按住任何东西——它只贡献 `opened_at` 的下界（性质 4）。"""
        exemptions = {1: g.Exemption(in_force=False, ends_at=at(days=1))}
        plan = derive([row(1)], exemptions=exemptions)

        self.assertEqual(len(plan.declarations), 1)
        self.assertEqual(plan.declarations[0].opened_at, at(days=1))

    def test_the_two_reasons_are_mutually_exclusive_per_row(self):
        """一条行要么进期望集、要么进解除表，不会两边都有——否则「它现在拦没拦」
        就成了要看调用方读了哪一张表。"""
        plan = derive(
            [
                row(1),
                row(2, warrant=g.LAPSED, status=g.STATUS_APPLIED),
            ]
        )

        declared = {d.strategy_id for d in plan.declarations}
        self.assertEqual(declared & set(plan.close_reasons), set())
        self.assertEqual(declared | set(plan.close_reasons), {1, 2})


class TestWhichReasonWins(SimpleTestCase):
    """顺序不是随手排的：阶段 → 豁免 → 判据/归属。混在一处比对会让用户看到
    「重新适配」而其实是阶段换了。"""

    def test_the_regime_beats_everything(self):
        豁免 = {1: g.Exemption(in_force=True, ends_at=at(days=3))}
        plan = derive(
            [row(1, regime=UPTREND, warrant=g.GONE, status=g.STATUS_APPLIED)],
            exemptions=豁免,
        )

        self.assertEqual(plan.close_reasons, {1: g.CLOSE_REGIME_LEFT})

    def test_the_exemption_beats_the_warrant(self):
        """豁免是「人按住了它」，比「判据变了」更靠前也更可执行：用户刚按下的动作
        必须是他看得见的那个原因。"""
        exemptions = {1: g.Exemption(in_force=True, ends_at=at(days=3))}
        plan = derive(
            [row(1, warrant=g.GONE, status=g.STATUS_APPLIED)], exemptions=exemptions
        )

        self.assertEqual(plan.close_reasons, {1: g.CLOSE_EXEMPTED})


# --------------------------------------------------------------------------- #
# 性质 3：机制从没碰过的行
# --------------------------------------------------------------------------- #


class TestNeverTouchedRowsKeepTheirStatus(SimpleTestCase):
    def test_a_suggested_row_in_another_regime_is_not_marked_released(self):
        """它只能来自机制没写 status 的那段时间（Shadow 档），而「机制解除过它」是
        假话。它同时还在 `replay_run` 的重放集合里（`exclude(status=RELEASED)`），
        那是它该待的地方：一条没人认领的**建议**值得复核。
        """
        plan = derive([row(1, regime=UPTREND, status=g.STATUS_SUGGESTED)])

        self.assertEqual(plan.close_reasons, {1: g.CLOSE_REGIME_LEFT})
        self.assertEqual(plan.statuses, ())

    def test_an_applied_row_in_another_regime_is_marked_released(self):
        """机制**碰过**的那一条（`applied`）必须收回——那正是「它当时在拦」这条
        记录的正确收场。"""
        plan = derive([row(1, regime=UPTREND, status=g.STATUS_APPLIED)])

        self.assertEqual(
            [(w.status, w.old_status) for w in plan.statuses],
            [(g.STATUS_RELEASED, g.STATUS_APPLIED)],
        )

    def test_an_in_force_exemption_on_a_suggested_row_still_writes_nothing(self):
        """豁免在期、机制从没碰过、阶段又对不上——三条同时成立时仍然不写。
        「不写」是这一格的默认，任何一条别的判据都不该把它翻过来。"""
        exemptions = {1: g.Exemption(in_force=True, ends_at=at(days=3))}
        plan = derive(
            [row(1, regime=UPTREND, status=g.STATUS_SUGGESTED)], exemptions=exemptions
        )

        self.assertEqual(plan.statuses, ())

    def test_a_suggested_row_in_the_current_regime_is_also_left_alone(self):
        """**同一格里最省事的那条路**：阶段对上、判据仍成立，只有豁免在期把它按住。

        它比上一条更容易写成假历史——「阶段对不上」那半个条件在上一条里顺手替我们挡住
        了写回，这里没有东西挡：`_close_reason` 给的是 `exempted`，目标值是 `released`，
        而这一行从没被机制施加过（`applied` 是机制写的，它还是 `suggested`）。于是判据
        必须是「目标值是 `released` 且旧值是 `suggested`」**而不是**「阶段对不上」——
        否则用户一按豁免，那条从没生效过的建议就被记成「机制解除过它」。
        """
        exemptions = {1: g.Exemption(in_force=True, ends_at=at(days=3))}
        plan = derive([row(1, status=g.STATUS_SUGGESTED)], exemptions=exemptions)

        self.assertEqual(plan.declarations, ())
        self.assertEqual(plan.close_reasons, {1: g.CLOSE_EXEMPTED})
        self.assertEqual(plan.statuses, ())


# --------------------------------------------------------------------------- #
# 性质 4：opened_at
# --------------------------------------------------------------------------- #


class TestAttitudeSince(SimpleTestCase):
    def test_with_nothing_interrupting_the_switch_wins_when_it_is_latest(self):
        plan = derive(
            [row(1)],
            switch_at=at(days=3),
            regime_effective_at=at(days=2),
            generation_at=at(days=1),
        )

        self.assertEqual(plan.declarations[0].opened_at, at(days=3))

    def test_the_latest_fact_wins(self):
        """少算任何一个，声明行都会声称机制在某段时间里拦着、而事实是那段时间它没拦。"""
        plan = derive(
            [row(1)],
            switch_at=at(days=1),
            regime_effective_at=at(days=5),
            generation_at=at(days=2),
        )

        self.assertEqual(plan.declarations[0].opened_at, at(days=5))

    def test_an_ended_exemption_restarts_the_attitude(self):
        """豁免期间机制的态度是「不拦」，态度从豁免结束那一刻**重新**开始。
        这一行记的是**态度不是动作**：豁免一到期，机制的态度就是「拦」，机制晚跑了
        多久是机制的问题（Q5(iii)）。"""
        exemptions = {1: g.Exemption(in_force=False, ends_at=at(days=9))}
        plan = derive(
            [row(1)],
            exemptions=exemptions,
            switch_at=at(days=1),
            regime_effective_at=at(days=2),
            generation_at=at(days=3),
        )

        self.assertEqual(plan.declarations[0].opened_at, at(days=9))

    def test_an_in_force_exemption_is_not_a_candidate(self):
        """在期豁免的 `ends_at` 落在**未来**：若把它算进去，`opened_at` 会变成未来，
        而那时候机制还没开始拦（更糟：每过一轮它就再远一点）。

        在期豁免下这一行根本不产出声明（三态表的另一格），所以直接问取时刻的函数。
        """
        exemptions = {1: g.Exemption(in_force=True, ends_at=at(days=9))}
        world = situation(
            exemptions=exemptions,
            switch_at=at(days=1),
            regime_effective_at=at(days=2),
            generation_at=at(days=3),
        )

        self.assertEqual(g.attitude_since(world, 1), at(days=3))
        self.assertEqual(g.derive(world, [row(1)]).declarations, ())

    def test_a_switch_without_a_moment_is_a_loud_bug(self):
        """开关开着却拿不到开启时刻，是取数层的漏项。声明行的 `opened_at` 是必填的，
        而这里没有任何事实可以拿出来——**不猜一个 `now()` 顶上**。"""
        with self.assertRaises(RuntimeError):
            g.derive(situation(switch_at=None), [row(1)])

    def test_the_clock_is_never_read(self):
        """同一份输入连问两次必须逐字相同。这条是「纯函数」最便宜的观测方式：
        `derive` 里任何一处 `timezone.now()` 都会让两次结果差一点点。"""
        self.assertEqual(derive([row(1)]), derive([row(1)]))
        self.assertEqual(
            derive([row(1)], switch_at=at(days=4)),
            derive([row(1)], switch_at=at(days=4)),
        )


# --------------------------------------------------------------------------- #
# 性质 5：Shadow
# --------------------------------------------------------------------------- #


class TestTheShadowBranch(SimpleTestCase):
    """回到 Shadow 的意义是「不再拦」：活行必须解除，而决策行的历史一个字都不许添。"""

    def test_scenario_produces_reasons_but_no_declarations_and_no_status_writes(self):
        rows = [
            row(1, status=g.STATUS_APPLIED),
            row(2, regime=UPTREND, status=g.STATUS_APPLIED),
            row(3, status=g.STATUS_SUGGESTED),
        ]
        plan = derive(rows, gate_open=False)

        self.assertEqual(plan.declarations, ())
        self.assertEqual(plan.statuses, ())
        self.assertEqual(
            plan.close_reasons,
            {
                1: g.CLOSE_GATE_CLOSED,
                2: g.CLOSE_GATE_CLOSED,
                3: g.CLOSE_GATE_CLOSED,
            },
        )
        self.assertEqual(plan.note, g.NOTE_SHADOW)
        self.assertEqual(plan.blocked, "")

    def test_with_no_rows_it_is_a_quiet_no_op(self):
        plan = derive([], gate_open=False)

        self.assertEqual(plan.declarations, ())
        self.assertEqual(plan.statuses, ())
        self.assertEqual(plan.close_reasons, {})


# --------------------------------------------------------------------------- #
# 性质 6：三种「什么都不动」
# --------------------------------------------------------------------------- #


class TestBlockedAndColdStartTouchNothing(SimpleTestCase):
    """「说不清现在是什么阶段」的两种收场。解除了就再也回不来（那段时间事后无法
    重建），保持现状只是「今晚本不该拦的拦着」——取向与 `_halt_block_reason` 的
    fail-closed 一致。"""

    def test_a_cold_start_derives_nothing_at_all(self):
        plan = derive(
            [row(1), row(2, regime=UPTREND, status=g.STATUS_APPLIED)], regime=None
        )

        self.assertEqual(plan.declarations, ())
        self.assertEqual(plan.statuses, ())
        self.assertEqual(plan.close_reasons, {})
        self.assertEqual(plan.blocked, dea.BLOCKED_COLD_START)
        self.assertIn(dea.BLOCKED_DISPLAY[dea.BLOCKED_COLD_START], plan.note)

    def test_a_stale_state_derives_nothing_at_all(self):
        """**尤其不能按空集解除**：状态过期正是这套机制最需要继续生效的时刻，而
        「说不清就解除」会让一次任务停摆变成一次无声的机制失效。"""
        plan = derive(
            [row(1, status=g.STATUS_APPLIED)], blocked=dea.BLOCKED_STALE_STATE
        )

        self.assertEqual(plan.declarations, ())
        self.assertEqual(plan.statuses, ())
        self.assertEqual(plan.close_reasons, {})
        self.assertEqual(plan.blocked, dea.BLOCKED_STALE_STATE)
        self.assertIn(dea.BLOCKED_DISPLAY[dea.BLOCKED_STALE_STATE], plan.note)

    def test_the_blocked_branch_does_not_need_a_switch_moment(self):
        """这一条顺带钉住 `blocked` 排在最前：它连 `attitude_since` 都不该走到——
        否则一条取数层的漏项会把「本轮什么都不动」变成一次异常。"""
        plan = g.derive(
            situation(switch_at=None, blocked=dea.BLOCKED_STALE_STATE), [row(1)]
        )

        self.assertEqual(plan.blocked, dea.BLOCKED_STALE_STATE)


class TestTheBlanketTier(SimpleTestCase):
    """保命档（高波动）期间声明与解除**两样都不做**。

    高波动是**叠加层**而不是「离开了原阶段」，而逐行判定的尺子是 `regime == 当前阶段`
    ——高波动一抬上来，每一行都会被判成「阶段离开」：活行被解除、决策行被翻成 `released`。
    那是假话，而解除不可逆（`CONTEXT.md` 第 115 条；取向与 `deactivation_run
    .close_left_regime_exemptions` 的第三条停手同构）。保命档那一层自己有全场作用域的
    声明，所以这一层无事可做。
    """

    def test_a_blanket_regime_touches_nothing_at_all(self):
        rows = [
            row(1, status=g.STATUS_APPLIED),
            row(2, regime=UPTREND, status=g.STATUS_APPLIED),
            row(3, status=g.STATUS_SUGGESTED),
        ]
        plan = derive(rows, regime=BaseRegime.HIGH_VOL.value)

        # 三条路各试探一种收场：1 该被声明、2 该按 `regime_left` 解除、3 该写 status。
        # 保命档下三样都不许出现——出现任何一样都是「按阶段比较得出的」那句假话。
        self.assertEqual(plan.declarations, ())
        self.assertEqual(plan.statuses, ())
        self.assertEqual(plan.close_reasons, {})
        self.assertEqual(plan.blocked, g.BLOCKED_BLANKET)
        self.assertEqual(plan.note, g.NOTE_BLANKET)

    def test_the_blanket_branch_beats_the_row_by_row_verdict(self):
        """一行「阶段对、判据仍成立」的活行本该被声明——保命档下它原样留着，
        **而这不是「没什么可拦」**（`BLOCKED_BLANKET` 非空，页面据此说话）。"""
        plan = derive(
            [row(1, status=g.STATUS_APPLIED)], regime=BaseRegime.HIGH_VOL.value
        )

        self.assertNotEqual(plan.blocked, "")
        self.assertEqual(plan.declarations, ())

    def test_the_shadow_branch_still_wins_when_the_gate_is_closed(self):
        """开关关着 + 高波动走的是 **Shadow** 那一支，不是这一支。

        Shadow 说的「机制确实不再拦」在高波动期间同样为真，而它要挡的是**按阶段比较得出
        的理由**——那个比较才是假的。口径不同的表现是：一行活行在这里被解除（`gate_closed`），
        在开关开着时反而不动。
        """
        plan = derive(
            [row(1, status=g.STATUS_APPLIED)],
            gate_open=False,
            regime=BaseRegime.HIGH_VOL.value,
        )

        self.assertEqual(plan.close_reasons, {1: g.CLOSE_GATE_CLOSED})
        self.assertEqual(plan.blocked, "")
        self.assertEqual(plan.note, g.NOTE_SHADOW)

    def test_a_round_that_cannot_tell_keeps_talking_about_that(self):
        """「说不清」排在保命档之前：状态过期时连高波动这个判断本身都是过期的，
        报成「保命档所以不动手」会把用户支去等一个不一定会来的下一轮。
        """
        plan = derive(
            [row(1)], regime=BaseRegime.HIGH_VOL.value, blocked=dea.BLOCKED_STALE_STATE
        )

        self.assertEqual(plan.blocked, dea.BLOCKED_STALE_STATE)
        self.assertIn(dea.BLOCKED_DISPLAY[dea.BLOCKED_STALE_STATE], plan.note)

    def test_the_blanket_code_is_not_one_of_the_cannot_tell_codes(self):
        """四个码共用 `Confirmation.skipped` 那一格，而页面按码分岔说不同的话：
        重字的表现是「保命档被报成一次取数故障」，用户会去查一个不存在的故障。
        """
        self.assertNotIn(
            g.BLOCKED_BLANKET, (dea.BLOCKED_COLD_START, dea.BLOCKED_STALE_STATE)
        )


# --------------------------------------------------------------------------- #
# 性质 8：给用户看的两句话
# --------------------------------------------------------------------------- #


class TestWhatTheUserSees(SimpleTestCase):
    def test_the_label_matches_the_read_side(self):
        """与 `test_guard_halt_strategy_scope.LABEL` 逐字相同。那边是读的人，这边是
        写的人——对不上的表现是「下单被拦了，理由里认不出是哪条策略」。"""
        plan = derive([row(1, name="趋势跟踪", cell_reason="下行趋势表现不佳")])

        self.assertEqual(plan.declarations[0].label, LABEL)

    def test_the_label_has_no_empty_parentheses_without_a_cell_reason(self):
        plan = derive([row(1, name="趋势跟踪")])

        self.assertEqual(plan.declarations[0].label, "策略停用决策：趋势跟踪")

    def test_the_reason_carries_the_subject_because_the_body_does_not(self):
        """通知正文只打触发源、作用域与 `reason`（Q10 的核查），读的人手上只有一行
        「作用域 策略 <uuid>」——所以策略名与阶段名只能出现在这句话里。"""
        plan = derive([row(1, name="趋势跟踪", cell_reason="下行趋势表现不佳")])
        reason = plan.declarations[0].reason

        self.assertIn("趋势跟踪", reason)
        self.assertIn("下行趋势", reason)
        self.assertIn("下行趋势表现不佳", reason)
        self.assertIn("减仓放行", reason)

    def test_an_unknown_regime_slug_is_echoed_not_guessed(self):
        """`BaseRegime(...)` 对认不出的 slug 抛 `ValueError`——展示层不能因此炸掉一轮
        判定，也不该猜一个「箱体震荡」上去（与 `report._regime_display` 同一口径）。"""
        self.assertEqual(g._regime_display("meltdown"), "meltdown")
        self.assertEqual(g._regime_display(None), "（无）")
        self.assertEqual(g._regime_display(DOWNTREND), "下行趋势")

    def test_the_shadow_note_says_both_halves(self):
        """Shadow 的一轮要说清两件事：不拦了，而且决策行的状态一个字没写——
        后者是排查时最容易误判成 bug 的那一半。"""
        self.assertIn("不再拦", g.NOTE_SHADOW)
        self.assertIn("一个字都不写", g.NOTE_SHADOW)

    def test_the_blanket_note_says_both_halves(self):
        """保命档的一轮要说清两件事，而两件都与 Shadow 相反：不产出声明，也不解除。

        与 `NOTE_SHADOW` 共用一句话会是错的：那一句说「活行按 gate_closed 解除」，贴到
        保命档这一轮上正好是本层最不该做的那件事。
        """
        self.assertIn("不产出策略档声明", g.NOTE_BLANKET)
        self.assertIn("不解除", g.NOTE_BLANKET)
        self.assertNotEqual(g.NOTE_BLANKET, g.NOTE_SHADOW)
