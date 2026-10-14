"""停用决策推导（第①段单元 7iii）。

这个文件要钉住的不是「哪条策略该停」，而是**七条性质**：

1. **每个被管策略恰好一条结论**——不多不少。少了会让某条策略静默地逃出机制，多了
   会让「这条策略被停了几次」变成一个算不出来的数。
2. **`TARGET` 是唯一会落决策的取值**。`neutral`/`fit`/`unknown`/`blanket`/`no_cell`
   都不是停用理由：门槛没过是「没结论」，不是「不好」；该阶段没有交易日是「说不出来」，
   不是「不适用」。
3. **冲突格子阻塞建议、不阻塞结论**（Q3）：`unfit + needs_review` 落成 `NEEDS_REVIEW`，
   不在 `targets` 里，但那格的摘要照旧带着走。
4. **保命档不产出建议是结构性的**，不是一道额外的闸：高波动档的格子恒为 `blanket`，
   而 `blanket` 不是 `TARGET`。这里钉的是「这条路径到不了」，不是「我们记得跳过它」。
5. **冷启动与状态过期都不产出建议，但都必须留下可说的一句话**——不静默是这条纪律的
   一半，另一半是这句话得由调用方带出去。
6. **被管策略集合的两条判据是一筛一标**：解析不到实现类的（不在 `strategy_ids` 里）
   只报数不推导；有活跃会话的只是被标记。
7. **词表是落库契约**：`state`/`reason` 在模型上是裸 `CharField`，所以「池化的词表被
   覆盖全了、且认不出来的取值不会变成『该停』」必须由本文件钉住。

门槛一律用**缩小**的那组生命周期参数（与 `test_pool.py` 同一做法）：默认的 5 天待机 /
3 天过期要造很久的时钟才出结论，断言只会被埋进日期算术里。缩小的只是规模。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from django.test import SimpleTestCase

from apps.regime import deactivation as dea
from apps.regime import slice as sl
from apps.regime.config import JudgementLifecycleConfig
from apps.regime.quant import BaseRegime

HIGH_VOL = BaseRegime.HIGH_VOL.value
DOWNTREND = BaseRegime.DOWNTREND.value
UPTREND = BaseRegime.UPTREND.value

#: 生命周期参数：过期门槛 3 天（默认值）+ 一个不参与本模块的待机天数。
LIFE = JudgementLifecycleConfig(min_dwell_days=5, stale_after_days=3)

DAY0 = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)


def at(days: int = 0, hours: int = 0) -> datetime:
    return DAY0 + timedelta(days=days, hours=hours)


def cell(
    strategy_id: int,
    regime: str,
    state: str,
    *,
    reason: str = "",
    source: str = "strategy",
    needs_review: bool = False,
    evidence: dict | None = None,
) -> dict:
    """一格池化结论的字面量，形状与 `pool_rebuild.current_cells()` 一致。"""
    return {
        "strategy_id": strategy_id,
        "regime": regime,
        "source": source,
        "state": state,
        "reason": reason,
        "evidence": evidence if evidence is not None else {"trades": 42},
        "needs_review": needs_review,
    }


def derive(cells, *, state=DOWNTREND, ids=(1,), now=None, **kwargs):
    """把 `derive` 的调用压到一行，好让每个用例只剩它真正在问的那一件事。"""
    return dea.derive(
        cells,
        state=dea.RegimeState(regime=state, effective_at=at()),
        strategy_ids=ids,
        now=now or at(),
        params=LIFE,
        **kwargs,
    )


class TestOneOutcomePerManagedStrategy(SimpleTestCase):
    """性质 1：不重不漏。"""

    def test_every_managed_strategy_gets_exactly_one_outcome(self):
        cells = {
            (1, DOWNTREND): cell(1, DOWNTREND, sl.STATE_UNFIT),
            (2, DOWNTREND): cell(2, DOWNTREND, sl.STATE_FIT),
        }
        got = derive(cells, ids=(1, 2, 3))
        self.assertEqual([o.strategy_id for o in got.outcomes], [1, 2, 3])
        self.assertEqual(len(got.outcomes), 3)

    def test_a_managed_strategy_without_a_cell_is_no_cell_not_unknown(self):
        """没有格子与「格子结论是 unknown」是两件事：前者是没有结论，后者是结论就是
        「该阶段没有交易日」。混成一个会让日报说「该阶段没出现过」而其实是它的回测
        全被排除了。"""
        got = derive({}, ids=(7,))
        self.assertEqual(got.outcomes[0].verdict, dea.NO_CELL)
        self.assertEqual(got.outcomes[0].state, "")
        self.assertEqual(got.targets, ())

    def test_duplicate_ids_do_not_produce_duplicate_outcomes(self):
        got = derive({}, ids=(1, 1, 2))
        self.assertEqual([o.strategy_id for o in got.outcomes], [1, 2])

    def test_only_the_managed_set_is_derived(self):
        cells = {
            (1, DOWNTREND): cell(1, DOWNTREND, sl.STATE_UNFIT),
            (99, DOWNTREND): cell(99, DOWNTREND, sl.STATE_UNFIT),
        }
        got = derive(cells, ids=(1,))
        self.assertEqual([o.strategy_id for o in got.outcomes], [1])
        self.assertEqual(got.unmanaged, (99,))

    def test_a_cell_in_another_regime_neither_leaks_in_nor_counts_as_managed(self):
        """只读当前阶段那一列。别的阶段的行进来只是为了算 `unmanaged`，不是候选。"""
        cells = {
            (1, UPTREND): cell(1, UPTREND, sl.STATE_UNFIT),
            (99, UPTREND): cell(99, UPTREND, sl.STATE_FIT),
        }
        got = derive(cells, ids=(1,), state=DOWNTREND)
        self.assertEqual(got.outcomes[0].verdict, dea.NO_CELL)
        self.assertEqual(got.unmanaged, (99,))


class TestWhatIsNotAGroundForDeactivation(SimpleTestCase):
    """性质 2 + 7：只有 `unfit` 会变成建议，且池化的词表被覆盖全了。"""

    def test_every_pool_state_is_mapped_and_unfit_is_the_one_handled_apart(self):
        """池化的五个状态一个不落地有归宿，`unfit` 是唯一在表外单独处理的那个。

        这条红了通常意味着有人给 `pool` 加了第五种状态而没告诉这一层——那时
        `_verdict` 会把新状态当 `unknown`（fail-safe），而不会变成「该停」。
        """
        self.assertEqual(
            set(dea.VERDICT_OF_STATE) | {sl.STATE_UNFIT},
            {
                sl.STATE_FIT,
                sl.STATE_UNFIT,
                sl.STATE_NEUTRAL,
                sl.STATE_UNKNOWN,
                sl.STATE_BLANKET,
            },
        )
        self.assertNotIn(sl.STATE_UNFIT, dea.VERDICT_OF_STATE)

    def test_state_to_verdict_is_one_to_one(self):
        expected = {
            sl.STATE_FIT: dea.FIT,
            sl.STATE_NEUTRAL: dea.NEUTRAL,
            sl.STATE_UNKNOWN: dea.UNKNOWN,
            sl.STATE_BLANKET: dea.BLANKET,
            sl.STATE_UNFIT: dea.TARGET,
        }
        for state, verdict in expected.items():
            with self.subTest(state=state):
                got = derive({(1, DOWNTREND): cell(1, DOWNTREND, state)}, ids=(1,))
                self.assertEqual(got.outcomes[0].verdict, verdict)

    def test_only_target_reaches_the_suggestion_list(self):
        """逐状态过一遍 `targets`：这是「哪些结论构成停用理由」的全集断言。"""
        for state in (
            sl.STATE_FIT,
            sl.STATE_NEUTRAL,
            sl.STATE_UNKNOWN,
            sl.STATE_BLANKET,
        ):
            with self.subTest(state=state):
                got = derive({(1, DOWNTREND): cell(1, DOWNTREND, state)}, ids=(1,))
                self.assertEqual(got.targets, ())
                self.assertEqual(got.writable, ())

    def test_an_unrecognized_state_never_becomes_a_suggestion(self):
        """词表漂移时**失败的方向**是「不停」，不是「停」。"""
        got = derive({(1, DOWNTREND): cell(1, DOWNTREND, "brand_new_state")}, ids=(1,))
        self.assertEqual(got.outcomes[0].verdict, dea.UNKNOWN)
        self.assertEqual(got.targets, ())
        # 原样留着，好让人看见那一格到底写了什么。
        self.assertEqual(got.outcomes[0].state, "brand_new_state")

    def test_every_verdict_has_a_display(self):
        """日报直接抄这句话，所以每个取值都得有一句人话。"""
        for verdict in (
            dea.TARGET,
            dea.NEEDS_REVIEW,
            dea.FIT,
            dea.NEUTRAL,
            dea.UNKNOWN,
            dea.BLANKET,
            dea.NO_CELL,
        ):
            with self.subTest(verdict=verdict):
                self.assertIn(verdict, dea.VERDICT_DISPLAY)
                self.assertTrue(dea.VERDICT_DISPLAY[verdict])


class TestConflictBlocksTheSuggestionNotTheConclusion(SimpleTestCase):
    """性质 3。"""

    def test_a_conflicted_unfit_cell_is_not_a_target(self):
        got = derive(
            {(1, DOWNTREND): cell(1, DOWNTREND, sl.STATE_UNFIT, needs_review=True)},
            ids=(1,),
        )
        self.assertEqual(got.outcomes[0].verdict, dea.NEEDS_REVIEW)
        self.assertEqual(got.targets, ())
        self.assertEqual([o.strategy_id for o in got.needs_review], [1])

    def test_the_conclusion_still_comes_through(self):
        """结论照常产出、照常被引用：状态与原因原样带着走。"""
        got = derive(
            {
                (1, DOWNTREND): cell(
                    1,
                    DOWNTREND,
                    sl.STATE_UNFIT,
                    reason=sl.REASON_DIRECTION_CONFLICT,
                    needs_review=True,
                )
            },
            ids=(1,),
        )
        self.assertEqual(got.outcomes[0].state, sl.STATE_UNFIT)
        self.assertEqual(got.outcomes[0].reason, sl.REASON_DIRECTION_CONFLICT)

    def test_a_clean_unfit_cell_is_still_a_target(self):
        """对照组：同一条 `unfit`，只是不冲突。否则上一条可能是「unfit 都不产建议」。"""
        got = derive({(1, DOWNTREND): cell(1, DOWNTREND, sl.STATE_UNFIT)}, ids=(1,))
        self.assertEqual([o.strategy_id for o in got.targets], [1])


class TestBlanketIsStructural(SimpleTestCase):
    """性质 4：保命档到不了。"""

    def test_a_high_vol_regime_produces_no_target_even_from_an_unfit_shaped_cell(self):
        """高波动档下就算有人手工塞了一格「不适用」，那一格也不是 `blanket`——
        而这里要钉的是**真实形状**：`pool` 对高波动档恒返回 `blanket`。"""
        got = derive(
            {
                (1, HIGH_VOL): cell(
                    1, HIGH_VOL, sl.STATE_BLANKET, reason=sl.REASON_HIGH_VOL_BLANKET
                )
            },
            ids=(1,),
            state=HIGH_VOL,
        )
        self.assertEqual(got.outcomes[0].verdict, dea.BLANKET)
        self.assertEqual(got.targets, ())

    def test_the_state_knows_it_is_the_blanket(self):
        """日报要写一句「全场停用由保命档负责」，那句的依据是**阶段本身**，
        与这一代池化表算出了什么无关。"""
        state = dea.RegimeState(regime=HIGH_VOL, effective_at=at())
        self.assertTrue(state.blanket)
        self.assertFalse(dea.RegimeState(regime=DOWNTREND, effective_at=at()).blanket)


class TestBlocked(SimpleTestCase):
    """性质 5：不产出，但要说出来。"""

    def test_cold_start_produces_no_outcomes_at_all(self):
        """没有当前阶段可问：逐策略全落成「没有这一格」是一句真的废话，而且会把一次
        「没有阶段」摊成几十条噪声。收场只有一条 `blocked`。"""
        got = dea.derive(
            {(1, DOWNTREND): cell(1, DOWNTREND, sl.STATE_UNFIT)},
            state=dea.RegimeState(),
            strategy_ids=(1,),
            now=at(),
            params=LIFE,
        )
        self.assertEqual(got.blocked, dea.BLOCKED_COLD_START)
        self.assertEqual(got.outcomes, ())
        self.assertEqual(got.targets, ())
        self.assertEqual(got.writable, ())
        self.assertEqual(got.age_days, None)
        self.assertTrue(got.blocked_note)

    def test_a_stale_state_still_shows_what_it_would_have_suggested(self):
        """过期不是「算不出来」：格子还在，结论照样算出来，只是不可写。日报要能说
        「若状态仍有效则会有 N 条」——那正是让人知道该去查判定任务的数字。"""
        got = dea.derive(
            {(1, DOWNTREND): cell(1, DOWNTREND, sl.STATE_UNFIT)},
            state=dea.RegimeState(regime=DOWNTREND, effective_at=at()),
            strategy_ids=(1,),
            now=at(days=4),
            params=LIFE,
        )
        self.assertEqual(got.blocked, dea.BLOCKED_STALE_STATE)
        self.assertEqual(got.age_days, 4)
        self.assertEqual([o.strategy_id for o in got.targets], [1])
        self.assertEqual(got.writable, ())

    def test_the_note_carries_the_numbers(self):
        """「日志不算被看见」的另一半：这句话要带上能让人动手的数字。"""
        got = dea.derive(
            {},
            state=dea.RegimeState(regime=DOWNTREND, effective_at=at()),
            strategy_ids=(),
            now=at(days=4),
            params=LIFE,
        )
        self.assertIn("4", got.blocked_note)
        self.assertIn("3", got.blocked_note)

    def test_the_staleness_boundary_is_exclusive(self):
        """「过期**超** 3 个自然日」：第 3 天头上还来得及，第 4 天起不再新增。

        边界落在哪一边是会有人依赖的事实（判定任务挂了一次之后还剩几天补救），
        所以逐日钉住，而不是只测「4 天算过期」。
        """
        for age_days, blocked in (
            (0, ""),
            (1, ""),
            (3, ""),
            (4, dea.BLOCKED_STALE_STATE),
        ):
            with self.subTest(age_days=age_days):
                got = dea.derive(
                    {},
                    state=dea.RegimeState(regime=DOWNTREND, effective_at=at()),
                    strategy_ids=(),
                    now=at(days=age_days),
                    params=LIFE,
                )
                self.assertEqual(got.blocked, blocked)

    def test_a_state_without_an_effective_moment_is_treated_as_cold(self):
        """说不清从哪一刻开始的结论，不拿来停人。"""
        got = dea.derive(
            {},
            state=dea.RegimeState(regime=DOWNTREND, effective_at=None),
            strategy_ids=(),
            now=at(days=99),
            params=LIFE,
        )
        self.assertEqual(got.blocked, dea.BLOCKED_COLD_START)
        self.assertEqual(got.age_days, None)

    def test_no_note_when_nothing_is_blocked(self):
        got = derive({}, ids=())
        self.assertEqual(got.blocked, "")
        self.assertEqual(got.blocked_note, "")


class TestExemptionAndRunningAreMarks(SimpleTestCase):
    """性质 6 + 豁免：两者都不筛选。"""

    def test_an_exempted_target_is_still_a_target_and_is_listed_as_exempt(self):
        """豁免抑制的是**动作**（第③段的 gate 施加之前要查在期豁免），不是结论。

        所以它照样落一条决策并挂上豁免指针——「已豁免」这句话因此是可查的，
        而不是只活在某一期日报里。
        """
        got = derive(
            {(1, DOWNTREND): cell(1, DOWNTREND, sl.STATE_UNFIT)},
            ids=(1,),
            exemptions={(1, DOWNTREND): 77},
        )
        self.assertEqual([o.strategy_id for o in got.targets], [1])
        self.assertEqual([o.strategy_id for o in got.writable], [1])
        self.assertEqual([o.strategy_id for o in got.exempt], [1])
        self.assertEqual(got.targets[0].exempt_id, 77)

    def test_an_exemption_for_another_regime_does_not_apply(self):
        """豁免键是（策略 × 阶段）：在别的阶段被豁免过，不构成这个阶段的挡箭牌。"""
        got = derive(
            {(1, DOWNTREND): cell(1, DOWNTREND, sl.STATE_UNFIT)},
            ids=(1,),
            exemptions={(1, UPTREND): 77},
        )
        self.assertIsNone(got.targets[0].exempt_id)
        self.assertEqual(got.exempt, ())

    def test_running_is_a_mark_not_a_filter(self):
        """判据二只标记。若它变成筛选，第①段（零执行、可能没有任何活跃会话）
        会天天产出空记录——而每日记录是这一段唯一的产出。"""
        cells = {(1, DOWNTREND): cell(1, DOWNTREND, sl.STATE_UNFIT)}
        both = derive(cells, ids=(1, 2), running_ids=(2,))
        self.assertEqual([o.strategy_id for o in both.outcomes], [1, 2])
        by_id = {o.strategy_id: o for o in both.outcomes}
        self.assertFalse(by_id[1].running)
        self.assertTrue(by_id[2].running)
        # 标记不影响建议：没人跑的也知道该停。
        self.assertEqual([o.strategy_id for o in both.targets], [1])

    def test_cell_digest_is_carried_but_not_invented(self):
        """格子上的依据原样带着走；没有格子时是空字典，不是编一份出来。"""
        got = derive(
            {
                (1, DOWNTREND): cell(
                    1, DOWNTREND, sl.STATE_UNFIT, evidence={"trades": 7}
                )
            },
            ids=(1, 2),
        )
        self.assertEqual(got.outcomes[0].evidence, {"trades": 7})
        self.assertEqual(got.outcomes[1].evidence, {})

    def test_the_pool_vocabulary_is_carried_through_verbatim(self):
        got = derive(
            {
                (1, DOWNTREND): cell(
                    1,
                    DOWNTREND,
                    sl.STATE_UNFIT,
                    reason=sl.REASON_INSUFFICIENT,
                    source="archetype",
                )
            },
            ids=(1,),
        )
        out = got.outcomes[0]
        self.assertEqual(
            (out.state, out.reason, out.source),
            (sl.STATE_UNFIT, sl.REASON_INSUFFICIENT, "archetype"),
        )
