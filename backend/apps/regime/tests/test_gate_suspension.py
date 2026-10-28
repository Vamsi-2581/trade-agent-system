"""「按当前生效阶段暂停开新仓」那一路的判定层（issue #51）。

本文件要钉住的不是「哪条策略该停」——那是停用决策那一路（`test_gate.py` 管着）——而是
**同一个策略档里的第二路**：

    （当前生效阶段 × 这一代那一格 × 有没有在期豁免 × 开关档位 × 保命档）
    → 该不该有一条 `regime_suspension` 声明 / 活行该记哪个解除原因

六条性质，每条都能在没有数据库的情况下单独钉住（全部 `SimpleTestCase`：判定层一旦偷偷
碰一次数据库，这里就红）：

1. **只放行 `fit`**：`unfit` / `neutral` / `unknown` / 无格 / 无生效阶段一律产出暂停声明。
   「判不了」不是「放它过去」——这是本票的裁决，也是它与候选漏斗侧同源的那一条。
2. **保命档与冷启动什么都不动**（`blocked`）：说不清阶段时不动，说得清而这一层不该动手
   （高波动）时同样不动。理由与停用路逐条同构。
3. **Shadow 档不声明、但要解除**：回到 Shadow 的意义就是不再拦，留一条活行会让「重新打开
   开关」那一刻的期望集与现状之间出现一段没人解释的空档（与停用路同款）。
4. **豁免是放行 + 一个说法**：人工按住的格子不该被这条路拦住；解除原因必须写 `exempted`，
   不能含混成「重新适配」——两者的正确处置不同（一个是去问人，一个是继续等）。
5. **两路合成一轮的期望集**（`merge`）：同策略双声明留停用那条、解除原因停用路优先、
   任意一路 `blocked` 即整轮不动。这三条各自挡着一类事故，见 `merge` 的 docstring。
6. **触发源字面量不飘**：`gate.py` 不能 import ORM 模型，只能抄一份取值；抄错的表现是一条
   声明落到表里一个谁也认不出的触发源上——而对账会把不认得的源当「不归我管」静默跳过，
   于是那条行永远解除不掉。※ `HaltTrigger` 是纯枚举（不带数据库访问），所以这里能直接
   与它比对；这一条是**唯一**该「跨层」的断言。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from django.test import SimpleTestCase

from apps.regime import gate as g
from apps.regime.models import HaltTrigger
from apps.regime.quant import BaseRegime
from apps.regime.slice import (
    REASON_INSUFFICIENT,
    REASON_NO_REGIME_DAYS,
    STATE_BLANKET,
    STATE_FIT,
    STATE_NEUTRAL,
    STATE_UNFIT,
    STATE_UNKNOWN,
)

RANGE = BaseRegime.RANGE.value
HIGH_VOL = BaseRegime.HIGH_VOL.value

DAY0 = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)


def at(days: int = 0) -> datetime:
    return DAY0 + timedelta(days=days)


def ref(
    strategy_id: int,
    *,
    cell_state: str | None = STATE_FIT,
    cell_reason: str = "",
    exempt: bool = False,
    retired: bool = False,
) -> g.SuspensionRef:
    return g.SuspensionRef(
        strategy_id=strategy_id,
        strategy_name=f"策略{strategy_id}",
        cell_state=cell_state,
        cell_reason=cell_reason,
        exempt=exempt,
        retired=retired,
    )


def situation(
    *,
    gate_open: bool = True,
    regime: str | None = RANGE,
    blocked: str = "",
    exemptions: dict | None = None,
    switch_at: datetime | None = at(),
    regime_effective_at: datetime | None = at(),
    generation_at: datetime | None = at(),
) -> g.Situation:
    """与 `test_gate.py` 的同名 helper 同款：默认是一轮最普通的——开关开着、阶段是箱体
    震荡、三个时刻都落在 `DAY0`（于是 `opened_at` 的期望值一眼可读）。"""
    return g.Situation(
        gate_open=gate_open,
        switch_at=switch_at,
        regime=regime,
        regime_effective_at=regime_effective_at,
        generation_at=generation_at,
        exemptions=exemptions or {},
        blocked=blocked,
    )


def plan(refs, **kwargs) -> g.GatePlan:
    return g.derive_suspensions(situation(**kwargs), refs)


def suspension_plan(*refs) -> g.GatePlan:
    """一份「只有暂停路」的 plan，用来单独测 `merge`。"""
    return g.GatePlan(
        declarations=tuple(
            g.Declaration(
                strategy_id=row.strategy_id,
                label=f"暂停-{row.strategy_id}",
                reason="r",
                opened_at=at(),
                trigger=g.TRIGGER_REGIME_SUSPENSION,
            )
            for row in refs
        )
    )


def deactivation_plan(*strategy_ids) -> g.GatePlan:
    """一份「只有停用路」的 plan。`trigger` 走默认值——停用路因此逐字未变。"""
    return g.GatePlan(
        declarations=tuple(
            g.Declaration(
                strategy_id=strategy_id,
                label=f"停用-{strategy_id}",
                reason="r",
                opened_at=at(),
            )
            for strategy_id in strategy_ids
        )
    )


# --------------------------------------------------------------------------- #
# 性质 1：只放行 fit
# --------------------------------------------------------------------------- #

#: 格子取值 → 该不该产出暂停声明 + 解除原因（不产出时）。
CELL_TABLE = [
    (STATE_FIT, False, g.CLOSE_BECAME_FIT),
    (STATE_UNFIT, True, None),
    (STATE_NEUTRAL, True, None),
    (STATE_UNKNOWN, True, None),
    (None, True, None),
    (STATE_BLANKET, False, g.CLOSE_BECAME_FIT),
]


class TestEveryCellStateHasOneLanding(SimpleTestCase):
    def test_each_cell_state_lands_on_exactly_one_outcome(self):
        for state, declared, close_reason in CELL_TABLE:
            with self.subTest(state=state):
                result = plan([ref(1, cell_state=state)])
                self.assertEqual(
                    [row.trigger for row in result.declarations],
                    [g.TRIGGER_REGIME_SUSPENSION] if declared else [],
                )
                if close_reason is None:
                    self.assertEqual(result.close_reasons, {})
                else:
                    self.assertEqual(result.close_reasons, {1: close_reason})

    def test_fit_cell_is_a_genuine_no_declaration_not_a_pass_with_a_reason(self):
        """`fit` 不产出声明，**也不合成一条「放行」的话**：本层只回答「拦不拦」，放行那句
        话由调用方原有那条路径说（候选侧那半边返回 `None` 而不是一份合成的结论，同一取向）。"""
        result = plan([ref(1, cell_state=STATE_FIT)])
        self.assertEqual(result.declarations, ())
        self.assertEqual(result.statuses, ())

    def test_unfit_and_no_evidence_are_told_apart_in_the_reason(self):
        """「判为不适用」与「判不了」必须是两句不同的话：两者的正确处置不同——一个是去看
        这个策略该不该换掉，一个是去看它的证据为什么这么薄。
        """
        unfit = plan([ref(1, cell_state=STATE_UNFIT, cell_reason="下行趋势表现不佳")])
        neutral = plan([ref(1, cell_state=STATE_NEUTRAL)])
        missing = plan([ref(1, cell_state=None)])

        self.assertIn("判为不适用", unfit.declarations[0].reason)
        self.assertIn("下行趋势表现不佳", unfit.declarations[0].reason)

        self.assertIn("判不了", neutral.declarations[0].reason)
        self.assertNotIn("判为不适用", neutral.declarations[0].reason)

        self.assertIn("没有这一格", missing.declarations[0].reason)

    def test_the_database_slug_never_reaches_the_users_eye(self):
        """库里存的是**短码**（`RegimePoolCell.reason` 落的是 `slice` 的键），而 `label` 与
        `reason` 进的是拒单理由、`query_halt` 与通知——贴一个 `insufficient_evidence` 到那里
        等于把「为什么拦我」答成一句只有写代码的人读得懂的话。

        展示名取自 `slice.REASON_DISPLAY`（不在这里另写中文：同一层在四处必须同措辞）。
        """
        result = plan(
            [ref(1, cell_state=STATE_NEUTRAL, cell_reason=REASON_INSUFFICIENT)]
        )
        declaration = result.declarations[0]
        self.assertIn("证据不足", declaration.label)
        self.assertIn("证据不足", declaration.reason)
        self.assertNotIn(REASON_INSUFFICIENT, declaration.label)
        self.assertNotIn(REASON_INSUFFICIENT, declaration.reason)

    def test_unknown_and_neutral_are_told_apart_in_the_reason(self):
        """`unknown`（该阶段在池化窗口里没出现过）与 `neutral`（有结论但证据不足）都要
        fail-closed，但读的人该知道去补哪一样：一个是「这段历史根本没覆盖到这个阶段」，
        一个是「覆盖到了但样本不够」。合成一句会让前者看起来像后者。
        """
        neutral = plan(
            [ref(1, cell_state=STATE_NEUTRAL, cell_reason=REASON_INSUFFICIENT)]
        ).declarations[0]
        unknown = plan(
            [ref(1, cell_state=STATE_UNKNOWN, cell_reason=REASON_NO_REGIME_DAYS)]
        ).declarations[0]

        self.assertIn("证据不足", neutral.reason)
        self.assertIn("窗口内该阶段未出现", unknown.reason)
        self.assertNotEqual(neutral.reason, unknown.reason)

    def test_a_missing_cell_says_so_instead_of_blaming_the_evidence(self):
        """「没有这一格」既不等于「中性」也不等于「未知」：那是池化表里压根没有这条策略
        这个阶段（新策略 / 新阶段 / 窗口未覆盖）。它同样 fail-closed，但理由必须说得出自己
        是这一种——否则读的人会去找一批根本不存在的样本。
        """
        missing = plan([ref(1, cell_state=None)]).declarations[0]
        self.assertIn("没有这一格", missing.reason)
        self.assertNotIn("证据不足", missing.reason)

    def test_the_label_says_which_tier_this_is(self):
        """`label` 进的是下单拒绝理由与 `query_halt`——「机制决定停用这条策略」与「这片
        行情里它没有资格开新仓」必须一眼分得开，因为前者要人工恢复、后者随阶段自己翻回去。
        """
        label = plan([ref(7, cell_state=STATE_UNFIT)]).declarations[0].label
        self.assertTrue(label.startswith(g.SUSPENSION_LABEL_PREFIX))
        self.assertNotEqual(g.SUSPENSION_LABEL_PREFIX, g.LABEL_PREFIX)
        self.assertIn("策略7", label)

    def test_every_managed_strategy_gets_an_answer(self):
        """**域是全部被管策略**，不是「被建议过停用的那些」：没有格子的策略也必须有落点
        （`no_evidence` 那一格），否则「只放行 fit」会在最新的那批策略上静默变成「放行」。"""
        result = plan([ref(1, cell_state=STATE_FIT), ref(2, cell_state=None)])
        self.assertEqual([row.strategy_id for row in result.declarations], [2])

    def test_opened_at_is_the_latest_fact_not_now(self):
        """`opened_at` 取库里事实里最晚的一个（与停用路同一个 `attitude_since`）：取 `now()`
        会让 `halt_sync._rewrite` 每 300 秒看见一次变化、反复清空开窗通知的账。"""
        result = plan(
            [ref(1, cell_state=STATE_UNFIT)],
            switch_at=at(),
            regime_effective_at=at(days=3),
            generation_at=at(days=1),
        )
        self.assertEqual(result.declarations[0].opened_at, at(days=3))


# --------------------------------------------------------------------------- #
# 性质 1b：人工退役的策略不参与求值
# --------------------------------------------------------------------------- #


class TestARetiredStrategyDoesNotJoinTheValuation(SimpleTestCase):
    def test_a_retired_strategy_is_released_by_name_not_suspended(self):
        """人工退役（`Strategy.is_active=False`）**不参与求值**——与停用路同一条纪律
        （那边 `_warrant` 判 `GONE`）。

        而且它必须**主动**写一条解除原因，不是「不产出」：只不产出的话，它名下可能活着的旧
        声明既不在期望集里、又不在解除名单里，`halt_sync._reconcile` 会按 fail-closed 把它
        原样留着——那条行继续拦一张**跑不起来**的策略的单，而它看起来完全正常、也永远不会
        自己好。
        """
        result = plan([ref(1, cell_state=STATE_UNFIT, retired=True)])
        self.assertEqual(result.declarations, ())
        self.assertEqual(result.close_reasons, {1: g.CLOSE_STRATEGY_GONE})

    def test_a_retired_strategy_is_not_judged_by_its_cell_at_all(self):
        """退役走在判据**之前**：一张没有格子的退役策略落 `strategy_gone`，不落
        `no_evidence`。两个码说的不是一件事——一个是「判不了」，一个是「它已经不归这套机制
        管了」——合成一个会让日报与通知里「我们不知道它适不适合」和「它自己不跑了」变成
        同一句话。
        """
        result = plan([ref(1, cell_state=None, retired=True)])
        self.assertEqual(result.declarations, ())
        self.assertEqual(result.close_reasons, {1: g.CLOSE_STRATEGY_GONE})

    def test_a_retired_strategy_never_gets_an_exemption_credit(self):
        """退役与在期豁免同时存在时仍然按退役算：豁免这个动作是「人按住了一条**在跑**的
        策略」，而退役已经让那条策略离开了求值面——两件事不该叠成一条「豁免生效中」的
        记录（那条记录会被读成「机制本来会拦它、是人放开了」）。
        """
        result = plan([ref(1, cell_state=STATE_UNFIT, exempt=True, retired=True)])
        self.assertEqual(result.close_reasons, {1: g.CLOSE_STRATEGY_GONE})


# --------------------------------------------------------------------------- #
# 性质 2：保命档与冷启动什么都不动
# --------------------------------------------------------------------------- #


class TestTheTwoBlockedOutcomes(SimpleTestCase):
    def test_a_blanket_stage_does_neither_declare_nor_release(self):
        """高波动期间声明与解除都不做。**不能**让它落进逐行判定：这一段里「按阶段比较」
        本身没有意义（阶段没离开，只是被叠了一层），而解除是不可逆的。"""
        result = plan([ref(1, cell_state=STATE_UNFIT)], regime=HIGH_VOL)
        self.assertEqual(result.blocked, g.BLOCKED_BLANKET)
        self.assertEqual(result.declarations, ())
        self.assertEqual(result.close_reasons, {})
        self.assertTrue(result.note)

    def test_a_blanket_cell_without_a_blanket_stage_does_not_suspend(self):
        """格子是 `blanket` 而阶段不是高波动（池化窗口里的高波日落在别处）时，**不发表
        意见**——保命档有它自己那条全场停机链，把它读成 `unfit` 会让这两条路的回边互不
        相遇（保命档会自己解除，而这一路要等格子回 `fit`）。"""
        result = plan([ref(1, cell_state=STATE_BLANKET)], regime=RANGE)
        self.assertEqual(result.declarations, ())
        self.assertEqual(result.close_reasons, {1: g.CLOSE_BECAME_FIT})

    def test_a_cold_start_moves_nothing(self):
        result = plan([ref(1, cell_state=STATE_UNFIT)], regime=None)
        self.assertEqual(result.blocked, "cold_start")
        self.assertEqual(result.declarations, ())

    def test_a_stale_state_moves_nothing(self):
        result = plan([ref(1, cell_state=STATE_UNFIT)], blocked="stale_state")
        self.assertEqual(result.blocked, "stale_state")
        self.assertEqual(result.declarations, ())


# --------------------------------------------------------------------------- #
# 性质 3：Shadow 档
# --------------------------------------------------------------------------- #


class TestTheShadowTierReleasesButDoesNotDeclare(SimpleTestCase):
    def test_shadow_declares_nothing_and_releases_everything(self):
        result = plan(
            [ref(1, cell_state=STATE_UNFIT), ref(2, cell_state=STATE_NEUTRAL)],
            gate_open=False,
        )
        self.assertEqual(result.declarations, ())
        self.assertEqual(
            result.close_reasons, {1: g.CLOSE_GATE_CLOSED, 2: g.CLOSE_GATE_CLOSED}
        )

    def test_shadow_still_releases_a_strategy_that_would_otherwise_be_fit(self):
        """`fit` 那一格在 Shadow 下也要写解除原因：活行是按「上一轮在拦」存在的，回到
        Shadow 的意义就是不再拦——漏掉它的话，重新打开开关那一刻会有一段没人解释的空档。"""
        result = plan([ref(1, cell_state=STATE_FIT)], gate_open=False)
        self.assertEqual(result.close_reasons, {1: g.CLOSE_GATE_CLOSED})


# --------------------------------------------------------------------------- #
# 性质 4：豁免
# --------------------------------------------------------------------------- #


class TestAnExemptionIsAHumanHoldingItBack(SimpleTestCase):
    def test_an_exempted_strategy_is_not_suspended(self):
        """人工按住了一格（哪怕它明确判为不适用）：这正是豁免这个动作的结构性用途——
        一个明确的人工决定要能覆盖机制算出来的结论。"""
        result = plan([ref(1, cell_state=STATE_UNFIT, exempt=True)])
        self.assertEqual(result.declarations, ())
        self.assertEqual(result.close_reasons, {1: g.CLOSE_EXEMPTED})

    def test_exempted_is_not_reported_as_became_fit(self):
        """解除原因必须写 `exempted`：`became_fit` 说的是「本阶段不再拦它」，而豁免说的是
        「有人按住了它」——前者让人继续等，后者让人去问那个人。"""
        result = plan([ref(1, cell_state=STATE_UNFIT, exempt=True)])
        self.assertNotIn(
            1,
            {k: v for k, v in result.close_reasons.items() if v == g.CLOSE_BECAME_FIT},
        )

    def test_a_blanket_cell_wins_over_an_exemption(self):
        """保命档与豁免同时命中时，保命档那一支赢（判据顺序是既有裁决，不是新决定）：
        高波动期间这一层根本不发表意见，豁免留痕由格子那一路去记。"""
        result = plan([ref(1, cell_state=STATE_BLANKET, exempt=True)])
        self.assertEqual(result.close_reasons, {1: g.CLOSE_BECAME_FIT})


# --------------------------------------------------------------------------- #
# 性质 5：两路合成
# --------------------------------------------------------------------------- #


class TestMergingTheTwoTiersIntoOneRound(SimpleTestCase):
    def test_a_strategy_declared_by_both_tiers_keeps_the_deactivation_one(self):
        """同一个策略两路都拦住时只留停用那条：两条都留的代价在用户侧——拒单理由会出现
        两句说同一件事的话，而它们的处置方式不同（一个要人工恢复、一个随阶段自动翻）。"""
        merged = g.merge(
            deactivation_plan(1), suspension_plan(ref(1, cell_state=STATE_UNFIT))
        )
        self.assertEqual(len(merged.declarations), 1)
        self.assertEqual(merged.declarations[0].trigger, g.TRIGGER_DEACTIVATION)

    def test_strategies_only_the_suspension_tier_covers_survive(self):
        merged = g.merge(
            deactivation_plan(1),
            suspension_plan(
                ref(1, cell_state=STATE_UNFIT), ref(2, cell_state=STATE_NEUTRAL)
            ),
        )
        self.assertEqual(
            [(row.strategy_id, row.trigger) for row in merged.declarations],
            [(1, g.TRIGGER_DEACTIVATION), (2, g.TRIGGER_REGIME_SUSPENSION)],
        )

    def test_the_deactivation_tier_wins_the_close_reason(self):
        """同一策略两路都给了不同的解除原因时取停用路那条：它更贴那条决策行的事实
        （「策略离场 / 阶段离开」），而暂停路只会说「本阶段不再拦」。"""
        base = g.GatePlan(close_reasons={1: g.CLOSE_STRATEGY_GONE})
        suspension = g.GatePlan(
            close_reasons={1: g.CLOSE_BECAME_FIT, 2: g.CLOSE_BECAME_FIT}
        )
        merged = g.merge(base, suspension)
        self.assertEqual(
            merged.close_reasons, {1: g.CLOSE_STRATEGY_GONE, 2: g.CLOSE_BECAME_FIT}
        )

    def test_any_blocked_side_blocks_the_whole_round(self):
        """`blocked` 的语义是「这一轮什么都没做」，而它那两个来源（说不清阶段 / 这一层
        不该动手）都**同时覆盖两路**——合成时留下一路的产出就成了「半个轮次」，那比不跑
        更坏。"""
        merged = g.merge(
            g.GatePlan(blocked=g.BLOCKED_BLANKET, note="blanket"),
            suspension_plan(ref(1, cell_state=STATE_UNFIT)),
        )
        self.assertEqual(merged.blocked, g.BLOCKED_BLANKET)
        self.assertEqual(merged.declarations, ())

    def test_statuses_only_come_from_the_deactivation_side(self):
        """`status` 是**停用决策行**的账，暂停路没有决策行可记（它连 `DeactivationDecision`
        都不产生）——这正是它不进日报「该停 N」的原因。"""
        base = g.GatePlan(
            statuses=(
                g.StatusWrite(
                    decision_id="d-1",
                    status=g.STATUS_APPLIED,
                    old_status=g.STATUS_SUGGESTED,
                ),
            )
        )
        merged = g.merge(base, suspension_plan(ref(1, cell_state=STATE_UNFIT)))
        self.assertEqual(len(merged.statuses), 1)


# --------------------------------------------------------------------------- #
# 性质 6：词表不飘
# --------------------------------------------------------------------------- #


class TestTheTriggerSlugsDoNotDrift(SimpleTestCase):
    def test_the_two_slugs_are_the_orm_ones(self):
        self.assertEqual(g.TRIGGER_DEACTIVATION, HaltTrigger.DEACTIVATION.value)
        self.assertEqual(
            g.TRIGGER_REGIME_SUSPENSION, HaltTrigger.REGIME_SUSPENSION.value
        )

    def test_the_two_slugs_are_distinct(self):
        """两路共用一个 slug 的后果不是文案错，而是**两件事坍成一条唯一键**：一条下个
        阶段会自己失效的暂停会被记成一次机制做过的停用决定。"""
        self.assertNotEqual(g.TRIGGER_DEACTIVATION, g.TRIGGER_REGIME_SUSPENSION)
