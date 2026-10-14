"""重算差异重放的判定（第①段单元 7 收尾的纯函数侧）。

`test_deactivation.py` 钉的是「当前阶段该停谁」；这个文件钉的是另一个问题——**新的一代
池化表翻出来之后，那些已经生效的停用决策还站得住吗**（CONTEXT.md 第 118 条）。

九条性质，每一条坏了都不报警、只出错的话：

1. **收敛是三档，不是五档**：池化的五个状态落进 `ReviewVerdict` 的三档，靠的是
   `deactivation.VERDICT_OF_STATE` 这一份现成的表——不在这里另抄一份状态字面量表，
   两处迟早会漂。
2. **`fit` 是唯一会惊动人的那一档**，其余（中性 / 没结论 / 保命档 / **那一格没了**）
   一律落到「没事」。反过来把「说不出来」落成「依据失效」，会让一次全表重算之后的
   每一格都变成告警。
3. **认不出来的状态取值走 `UNKNOWN`**，绝不可能是 `FIT`——与 `deactivation._verdict`
   同一个 fail-safe 方向：认不出来就什么都不说，既不说该停、也不说可以跑。
4. **词表恰好盖满，且 `unfit` 是被特殊照顾的那一格**：`VERDICT_OF_STATE` 只有四个键，
   **`unfit` 不在里面**——在 `deactivation._verdict` 里它走的是另一条路（`TARGET`／
   `NEEDS_REVIEW`，取决于冲突标记），所以它压根不是「一个状态对应一个结论」。
   `replay.review_verdict` 因此必须把 `unfit` 分支排在查表**之前**。两侧合起来
   （`set(VERDICT_OF_STATE) | {STATE_UNFIT}`）才等于 `slice` 的五个 `STATE_*`，
   测试把这条并集关系钉住，于是词表漂移在编译期之外还有一道拦截。
5. **`needs_review` 不改结论**：方向冲突的格子说的是「依据要人看一眼」，不是「变成
   适用了」。它如实记进证据、如实带进日报，但**不是第四档结论**。
6. **顺序稳定**：结论要进日报与告警，顺序不稳会让「今天和昨天有什么不同」多出一堆
   假差异。传进去的顺序不影响出来的顺序。
7. **首次重放也算「变了」**：`previous_verdict is None` 时 `changed` 为真——从没说过话
   到说了话，对读日报的人来说就是一条新信息。
8. **三档的计数键恒存在**（0 也留着），与 `pool_rebuild.EXCLUSION_KEYS` 同一条纪律。
9. **告警只描述、不含动作**：CONTEXT.md 明令不自动恢复，所以那句话里必须出现「不会
   自动恢复」，并把出口（人工豁免）指出来——否则收到告警的人唯一能做的就是来问。

纯逻辑，一律 `SimpleTestCase`。`strategy_id` 在这里只用小整数：这一层把它当**不透明的
键**（比较、原样带出去），不解析也不拼接——与 `deactivation.py` 同一条约定。
"""

from __future__ import annotations

import json

from django.test import SimpleTestCase

from apps.regime import deactivation as dea
from apps.regime import replay
from apps.regime import slice as sl
from apps.regime.models import ReviewVerdict
from apps.regime.pool import POOL_SOURCE_ARCHETYPE, POOL_SOURCE_STRATEGY
from apps.regime.quant import BaseRegime

DOWNTREND = BaseRegime.DOWNTREND.value
UPTREND = BaseRegime.UPTREND.value

#: `slice` 的四个状态取值。词表覆盖全靠它，所以从这里取而不是从 `pool` 抄一遍。
ALL_STATES = (
    sl.STATE_FIT,
    sl.STATE_UNFIT,
    sl.STATE_NEUTRAL,
    sl.STATE_UNKNOWN,
    sl.STATE_BLANKET,
)


def row(
    state: str,
    *,
    reason: str = "",
    source: str = POOL_SOURCE_STRATEGY,
    needs_review: bool = False,
    evidence: dict | None = None,
) -> dict:
    """`pool_rebuild.current_cells()` 里那一行的形状。"""
    return {
        "strategy_id": 1,
        "regime": DOWNTREND,
        "source": source,
        "state": state,
        "reason": reason,
        "evidence": {"trades": 42} if evidence is None else evidence,
        "needs_review": needs_review,
    }


def ref(
    decision_id: int, strategy_id: int = 1, regime: str = DOWNTREND
) -> replay.DecisionRef:
    return replay.DecisionRef(id=decision_id, strategy_id=strategy_id, regime=regime)


class TestReviewVerdict(SimpleTestCase):
    """性质 1-5：一格池化结论 → 一档重放结论。"""

    def test_unfit_is_still_unfit(self):
        self.assertEqual(
            replay.review_verdict(row(sl.STATE_UNFIT)),
            ReviewVerdict.STILL_UNFIT.value,
        )

    def test_fit_is_the_only_alarming_verdict(self):
        self.assertEqual(
            replay.review_verdict(row(sl.STATE_FIT)), ReviewVerdict.BECAME_FIT.value
        )

    def test_neutral_unknown_and_blanket_are_all_still_neutral(self):
        for state in (sl.STATE_NEUTRAL, sl.STATE_UNKNOWN, sl.STATE_BLANKET):
            with self.subTest(state=state):
                self.assertEqual(
                    replay.review_verdict(row(state)),
                    ReviewVerdict.STILL_NEUTRAL.value,
                )
                # 反过来钉一次：这三档里没有一档会被读成「依据失效」。
                self.assertNotEqual(
                    replay.review_verdict(row(state)), ReviewVerdict.BECAME_FIT.value
                )

    def test_missing_cell_is_still_neutral(self):
        """那一格没了（整条策略的回测全被排除）不是「变成适用了」。"""
        self.assertEqual(replay.review_verdict(None), ReviewVerdict.STILL_NEUTRAL.value)

    def test_unknown_state_value_falls_back_to_still_neutral(self):
        """性质 3：词表漂移（出现一个没见过的状态）时绝不能读成「依据失效」。"""
        self.assertEqual(
            replay.review_verdict(row("some_future_state")),
            ReviewVerdict.STILL_NEUTRAL.value,
        )

    def test_verdict_of_state_leaves_unfit_out_on_purpose(self):
        """性质 4a：`unfit` **不在**那张表里，且这不是漏写。

        它在 `deactivation._verdict` 里走的是另一条路：同是 `unfit`，`needs_review=False`
        判 `TARGET`、`=True` 判 `NEEDS_REVIEW`。一个状态对应两档，塞不进「状态 → 结论」
        这张表。哪天有人「好心」把它补进去，这条会红——补进去只能补成其中一个取值，
        而那个取值对另一半情况是错的。
        """
        self.assertNotIn(sl.STATE_UNFIT, dea.VERDICT_OF_STATE)
        from_unfit = {
            dea._verdict({"state": sl.STATE_UNFIT, "needs_review": flag})[0]
            for flag in (False, True)
        }
        self.assertEqual(from_unfit, {dea.TARGET, dea.NEEDS_REVIEW})

    def test_table_plus_unfit_covers_every_slice_state(self):
        """性质 4b：两侧合起来才盖满 `slice` 的五个状态。少一个键，那个状态就会走
        fail-safe（`UNKNOWN`）而不是被判定——安静地少判一格，而不是报错。"""
        self.assertEqual(set(dea.VERDICT_OF_STATE) | {sl.STATE_UNFIT}, set(ALL_STATES))
        self.assertFalse(set(dea.VERDICT_OF_STATE) & {sl.STATE_UNFIT})

    def test_every_state_lands_in_the_three_tier_table(self):
        """五个状态逐个走一遍：都必须落进三档，且只有 `fit` 会惊动人。"""
        for state in ALL_STATES:
            with self.subTest(state=state):
                verdict = replay.review_verdict(row(state))
                self.assertIn(verdict, {v.value for v in ReviewVerdict})
                if state != sl.STATE_FIT:
                    self.assertNotEqual(verdict, ReviewVerdict.BECAME_FIT.value)

    def test_unfit_is_decided_before_the_table_lookup(self):
        """性质 4c：`unfit` 分支必须排在查表之前。

        排在之后它会落到 `.get(state, UNKNOWN)` 的兜底 ⇒ `STILL_NEUTRAL`，于是一条明明
        仍成立的停用决策被重放成「中性」。这条用反证钉住：把 `unfit` 交给那张表，得到的
        是「无话可说」（`UNKNOWN`），而不是任何一档说得上话的结论。
        """
        via_table = dea.VERDICT_OF_STATE.get(sl.STATE_UNFIT, dea.UNKNOWN)
        self.assertEqual(via_table, dea.UNKNOWN)
        self.assertNotEqual(via_table, dea.TARGET)
        self.assertEqual(
            replay.review_verdict(row(sl.STATE_UNFIT)),
            ReviewVerdict.STILL_UNFIT.value,
        )

    def test_needs_review_keeps_still_unfit(self):
        """性质 5：冲突格子照常判「仍不适用」，只是标记如实带出来。"""
        conflicts = row(sl.STATE_UNFIT, reason="direction_conflict", needs_review=True)
        self.assertEqual(
            replay.review_verdict(conflicts), ReviewVerdict.STILL_UNFIT.value
        )


class TestPlanReviews(SimpleTestCase):
    """性质 5-8：逐条决策算结论、带原样取值、排定顺序、数出条数。"""

    def test_carries_the_cell_values_verbatim(self):
        """报告要照抄池化的话，所以 `state`/`reason`/`source`/`cell` 都必须原样带出来。"""
        cells = {
            (1, DOWNTREND): row(
                sl.STATE_UNFIT,
                reason="archetype_fallback",
                source=POOL_SOURCE_ARCHETYPE,
                needs_review=True,
                evidence={"trades": 7, "archetype": {"name": "趋势跟随"}},
            )
        }
        (review,) = replay.plan_reviews([ref(11)], cells)
        self.assertEqual(review.state, sl.STATE_UNFIT)
        self.assertEqual(review.reason, "archetype_fallback")
        self.assertEqual(review.source, POOL_SOURCE_ARCHETYPE)
        self.assertTrue(review.needs_review)
        self.assertEqual(review.cell["trades"], 7)
        # 依据摘要必须是**副本**：池化表会再换代，留指针会读到别人的数据。
        self.assertEqual(review.cell["archetype"]["name"], "趋势跟随")
        self.assertEqual(review.display, ReviewVerdict.STILL_UNFIT.display)

    def test_missing_cell_leaves_the_verbatim_fields_empty(self):
        (review,) = replay.plan_reviews([ref(11)], {})
        self.assertEqual(review.verdict, ReviewVerdict.STILL_NEUTRAL.value)
        self.assertEqual((review.state, review.reason, review.source), ("", "", ""))
        self.assertFalse(review.needs_review)
        self.assertEqual(dict(review.cell), {})

    def test_reads_the_cell_of_the_decisions_own_regime(self):
        """重放问的是**那条决策记的那个阶段**那一格，与传入的其它列无关。"""
        cells = {
            (1, DOWNTREND): row(sl.STATE_UNFIT),
            (1, UPTREND): {**row(sl.STATE_FIT), "regime": UPTREND},
        }
        (review,) = replay.plan_reviews([ref(11, regime=DOWNTREND)], cells)
        # 若错取了上涨段那一格，结论会变成 `BECAME_FIT`——那正是这条要拦住的错。
        self.assertEqual(review.verdict, ReviewVerdict.STILL_UNFIT.value)

    def test_order_is_stable_and_independent_of_input_order(self):
        """性质 6：同一批输入换一个传入顺序，产出顺序必须逐条相同。"""
        cells = {
            (1, DOWNTREND): row(sl.STATE_FIT),
            (2, DOWNTREND): row(sl.STATE_UNFIT),
            (2, UPTREND): row(sl.STATE_NEUTRAL),
        }
        decisions = [
            ref(3, strategy_id=2, regime=UPTREND),
            ref(1, strategy_id=1),
            ref(2, strategy_id=2),
        ]
        forward = replay.plan_reviews(decisions, cells)
        backward = replay.plan_reviews(list(reversed(decisions)), cells)
        self.assertEqual(forward, backward)
        self.assertEqual(
            [(r.strategy_id, r.regime) for r in forward],
            [(1, DOWNTREND), (2, DOWNTREND), (2, UPTREND)],
        )

    def test_first_replay_counts_as_changed(self):
        """性质 7：从没重放过（`None`）→ 说了话，就是一条新信息。"""
        cells = {(1, DOWNTREND): row(sl.STATE_UNFIT)}
        (review,) = replay.plan_reviews([ref(11)], cells)
        self.assertIsNone(review.previous_verdict)
        self.assertTrue(review.changed)

    def test_unchanged_verdict_is_not_changed(self):
        cells = {(1, DOWNTREND): row(sl.STATE_UNFIT)}
        (review,) = replay.plan_reviews(
            [ref(11)], cells, previous={11: ReviewVerdict.STILL_UNFIT.value}
        )
        self.assertEqual(review.previous_verdict, ReviewVerdict.STILL_UNFIT.value)
        self.assertFalse(review.changed)

    def test_invalidated_is_exactly_the_became_fit_tier(self):
        cells = {(1, DOWNTREND): row(sl.STATE_FIT)}
        (review,) = replay.plan_reviews([ref(11)], cells)
        self.assertTrue(review.invalidated)
        unchanged = replay.plan_reviews(
            [ref(11)], {(1, DOWNTREND): row(sl.STATE_UNFIT)}
        )
        self.assertFalse(unchanged[0].invalidated)


class TestCountVerdicts(SimpleTestCase):
    """性质 8：三档的计数键恒存在（0 也留着）。"""

    def test_all_three_keys_exist_even_when_empty(self):
        counts = replay.count_verdicts(())
        self.assertEqual(
            counts,
            {
                ReviewVerdict.STILL_UNFIT.value: 0,
                ReviewVerdict.STILL_NEUTRAL.value: 0,
                ReviewVerdict.BECAME_FIT.value: 0,
            },
        )

    def test_counts_each_tier(self):
        cells = {
            (1, DOWNTREND): row(sl.STATE_FIT),
            (2, DOWNTREND): row(sl.STATE_UNFIT),
            (3, DOWNTREND): row(sl.STATE_NEUTRAL),
        }
        decisions = [ref(1, 1), ref(2, 2), ref(3, 3)]
        counts = replay.count_verdicts(replay.plan_reviews(decisions, cells))
        self.assertEqual(counts[ReviewVerdict.BECAME_FIT.value], 1)
        self.assertEqual(counts[ReviewVerdict.STILL_UNFIT.value], 1)
        self.assertEqual(counts[ReviewVerdict.STILL_NEUTRAL.value], 1)


class TestFormatAlert(SimpleTestCase):
    """性质 9：只描述、不含动作，且把出口指出来。"""

    def test_says_it_does_not_auto_restore(self):
        message = replay.format_alert(
            [
                {
                    "strategy_id": "11111111-1111-1111-1111-111111111111",
                    "name": "趋势跟随A",
                    "regime": DOWNTREND,
                    "previous_verdict": None,
                }
            ]
        )
        self.assertIn("不会自动恢复", message)
        # 出口必须写出来，否则收到告警的人唯一能做的就是来问。
        self.assertIn("人工豁免", message)
        # 策略名与阶段的中文名（不是 `downtrend` 这种内部取值）。
        self.assertIn("趋势跟随A", message)
        self.assertIn(BaseRegime(DOWNTREND).display, message)
        self.assertNotIn(DOWNTREND, message)

    def test_falls_back_to_the_id_when_there_is_no_name(self):
        message = replay.format_alert([{"strategy_id": "abc", "regime": DOWNTREND}])
        self.assertIn("abc", message)

    def test_counts_the_invalidated_ones(self):
        message = replay.format_alert(
            [
                {"strategy_id": "a", "regime": DOWNTREND},
                {"strategy_id": "b", "regime": UPTREND},
            ]
        )
        self.assertIn("2 条", message)
        self.assertIn(BaseRegime(UPTREND).display, message)


class TestSerialisable(SimpleTestCase):
    """`plan_reviews` 的产出原样进日报，所以它必须能被当成普通数据搬运。"""

    def test_reviews_are_json_serialisable(self):
        cells = {(1, DOWNTREND): row(sl.STATE_FIT, evidence={"trades": 3})}
        planned = replay.plan_reviews([ref(11)], cells)
        import dataclasses

        blob = json.dumps([dataclasses.asdict(r) for r in planned], ensure_ascii=False)
        self.assertIn("became_fit", blob)
