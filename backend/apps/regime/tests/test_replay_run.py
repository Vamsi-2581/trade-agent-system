"""重算差异重放的取数与落库（第①段单元 7 收尾的 DB 侧）。

`test_replay.py` 钉的是**判定**（纯函数：新表那一格 → 三档结论）；这个文件钉的是它的
另一半——取哪些决策、上一次的结论从哪读、差异怎么写进 `DeactivationReview`、收件人怎么
筛。判定逻辑在这里一行都不断言，那会是第二处真相。

八条性质，每一条坏了都不报警、只出错的数：

1. **只重放生效中的决策**（`status != RELEASED`），且**已退役的策略不要**。前者照抄
   `DeactivationReview` 的 docstring 那句话本身（`exclude` 而不是白名单，将来真加了
   第四种状态时默认仍是「它在生效中」）；后者是 `is_active` 这个人工总开关
   （CONTEXT.md 第 106 条）——早就被人关掉的策略不该再收到「要不要放它跑」的问句。
2. **同一代只评一次**。第二次跑同一代，`written` 必须是 0——没有 `already` 那一读的话，
   `ignore_conflicts=True` 会拦下写入但返回值照报条数，于是「报告说评了 8 条」与
   「库里 0 行」同时成立，而这种谎在日报上看起来完全正常。
3. **重放不读「当前阶段」**。一条决策问的是**它自己记的那个阶段**，与今天生效的是哪
   个阶段无关——所以整个重放里一行 `RegimeJudgement` 都不需要（本文件用「库里一条
   判定都没有」钉住这件事）。同时这也是「不走 `deactivation.derive`」的实证：那条路
   的冷启动短路会让所有重放安静地什么都不做。
4. **`reviewed_at` 必须写**。模型上它没有默认值，忘了传就是 `IntegrityError`——这一条
   钉的是它落库之后真的读得回来，而不只是没炸。
5. **依据冻结的是新表那一格的原文副本**（`evidence["cell"]`），连同「决策当初冻的是
   哪一代」（`decision_pool_rebuild_id`）与「它是不是已经过期了」（`stale_basis`）。
   池化表日后还会换代，留指针就会读到别人的数据。
6. **`previous_verdict` 取的是最近一次**（不限哪一代），`changed` 由此而来；首次重放
   （`None`）算变了。
7. **收件人 = 受影响策略的活跃实盘会话所属用户**，去重、`mode` 只认 `live`、`status`
   只认 `LiveSession.ACTIVE_STATUSES`。**空元组是合法结果**（第①段是零执行的 Shadow），
   调用方要把这件事如实报出来，不能读成「已经通知过了」。
8. **返回值可 JSON 序列化且键集恒定**：它进日报与命令的 stdout，键集随路径漂移会让
   「今天和昨天有什么不同」多出一堆假差异。

另外钉一条**没有先例的桥**：告警由管理命令发（`_notify_invalidated`），而 `notify_user`
是 async、命令是同步的，两者用 `async_to_sync` 过桥。这是本项目里第一次从管理命令投递
告警，所以它单独有一组用例——包括「没有收件人」那句必须打出来。

DB 用例一律用真事务回滚的 `TestCase`；策略一律是**真的 `Strategy` 行**（UUID 主键）。
"""

from __future__ import annotations

import io
import itertools
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from django.contrib.auth import get_user_model
from django.core.management.base import OutputWrapper
from django.test import TestCase

from apps.regime import pool, replay_run
from apps.regime import slice as sl
from apps.regime.models import (
    DecisionStatus,
    DeactivationDecision,
    DeactivationReview,
    RebuildStatus,
    RegimePoolCell,
    RegimePoolRebuild,
    ReviewVerdict,
)
from apps.regime.pool import POOL_SOURCE_STRATEGY
from apps.regime.quant import BaseRegime
from apps.trading.models import Strategy

DOWNTREND = BaseRegime.DOWNTREND
UPTREND = BaseRegime.UPTREND

NOW = datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)

#: 就绪代的指纹有一条部分唯一约束，所以同一个类里每造一代都要一个新指纹。
_fingerprints = itertools.count(1)


def days(n: int) -> datetime:
    return NOW + timedelta(days=n)


def make_generation(
    *, status: str = RebuildStatus.READY.value, finished_at: datetime | None = None
) -> RegimePoolRebuild:
    """人工造一代。绕过 `rebuild_pool`，好让用例只问它真正在问的那一件事。"""
    return RegimePoolRebuild.objects.create(
        status=status,
        actor_kind="task",
        actor_name="任务",
        pool_version=pool.POOL_VERSION,
        input_fingerprint=f"f{next(_fingerprints)}",
        started_at=finished_at or NOW,
        finished_at=finished_at,
    )


def add_cell(
    generation,
    strategy,
    regime,
    *,
    state: str = sl.STATE_FIT,
    reason: str = "",
    evidence: dict | None = None,
) -> RegimePoolCell:
    return RegimePoolCell.objects.create(
        rebuild=generation,
        strategy=strategy,
        regime=regime.value,
        source=POOL_SOURCE_STRATEGY,
        state=state,
        reason=reason,
        evidence={} if evidence is None else evidence,
        needs_review=False,
    )


class _Fixture(TestCase):
    """两个策略（都是真的 `Strategy` 行）+ 一个用户，给决策与会话用。

    `is_active=True` 是**必须显式给的**：模型的默认值是 `False`（「已退役」），而重放的
    选取叠了一道 `strategy__is_active=True`。不给的话这里造的策略全被视为退役，选取一个
    都匹配不上，于是每一条断言都会看到「什么都没写」——而那看起来与「本来就没有要重放的
    决策」一模一样。这正是 CONTEXT.md 第 106 条要求回填存量策略的那个坑，测试里先显式
    立起来；`TestNothingToReplay.test_an_inactive_strategy_is_not_replayed` 再把它翻下去
    验一次反面。
    """

    @classmethod
    def setUpTestData(cls):
        cls.alpha = Strategy.objects.create(
            name="AlphaStem",
            code_path="/t/alpha.py",
            git_commit_hash="aaaaaaa",
            is_active=True,
        )
        cls.beta = Strategy.objects.create(
            name="BetaStem",
            code_path="/t/beta.py",
            git_commit_hash="bbbbbbb",
            is_active=True,
        )
        # `AUTH_USER_MODEL = "authentication.User"`：email 是必填的第一个位置参数。
        cls.user = get_user_model().objects.create_user(
            email="replay_run@test.local", username="replay_run", password="pw12345"
        )
        cls.other = get_user_model().objects.create_user(
            email="replay_run_other@test.local",
            username="replay_run_other",
            password="pw12345",
        )

    # --- 造数据的三件套 ---------------------------------------------------- #

    def decision(
        self,
        strategy,
        regime=DOWNTREND,
        *,
        status: str = DecisionStatus.SUGGESTED.value,
        pool_rebuild_id: int | None = None,
        decided_at: datetime | None = None,
    ) -> DeactivationDecision:
        moment = decided_at or NOW
        return DeactivationDecision.objects.create(
            strategy=strategy,
            regime=regime.value,
            status=status,
            evidence={},
            pool_rebuild_id=pool_rebuild_id,
            first_decided_at=moment,
            last_confirmed_at=moment,
        )

    def reviewed(self, decision, generation, *, verdict: str, at: datetime):
        """一条已经落在库里的历史重放记录（用来喂 `previous_verdict`）。"""
        return DeactivationReview.objects.create(
            decision=decision,
            rebuild=generation,
            verdict=verdict,
            evidence={},
            reviewed_at=at,
        )

    def session(
        self, strategy, user=None, *, mode: str = "live", status: str = "running"
    ):
        from apps.trading.models import LiveSession

        return LiveSession.objects.create(
            user=user or self.user,
            strategy=strategy,
            symbol="BTC/USDT",
            mode=mode,
            status=status,
            initial_capital=Decimal("10000.00"),
        )

    def replay(self, **kwargs):
        """跑一轮。**不叫 `run`**：`unittest.TestCase.run` 是框架自己的入口，覆盖掉它
        会让每条用例在框架调用时炸在参数个数上。"""
        kwargs.setdefault("now", NOW)
        return replay_run.run_replay(**kwargs)


# --------------------------------------------------------------------------- #
# 三种「没有可重放的东西」
# --------------------------------------------------------------------------- #


class TestNothingToReplay(_Fixture):
    """性质 1 的前半 + 性质 2：每一种收场都说得出自己的理由，且不落行。"""

    def test_no_generation_is_its_own_outcome(self):
        """一代池化表都没有 = **没法问**，与「没有要问的决策」分开。"""
        summary = self.replay()
        self.assertEqual(
            summary["skipped"], replay_run.deactivation_run.SKIPPED_NO_GENERATION
        )
        self.assertTrue(summary["note"])
        self.assertEqual((summary["written"], summary["already"]), (0, 0))

    def test_no_decisions_at_all_is_not_a_skip(self):
        """新表明明在手里，只是没有人等答案——所以 `skipped` 留 `None`。"""
        generation = make_generation()
        summary = self.replay(generation=generation)
        self.assertIsNone(summary["skipped"])
        self.assertEqual(summary["generation_id"], generation.id)
        self.assertEqual(summary["decisions"], 0)
        self.assertEqual(DeactivationReview.objects.count(), 0)

    def test_a_released_decision_is_not_replayed(self):
        """性质 1：已解除的决策重放没有意义——没有人在等它的答案。"""
        generation = make_generation()
        add_cell(generation, self.alpha, DOWNTREND, state=sl.STATE_FIT)
        self.decision(self.alpha, status=DecisionStatus.RELEASED.value)
        summary = self.replay(generation=generation)
        self.assertEqual(summary["decisions"], 0)
        self.assertEqual(DeactivationReview.objects.count(), 0)

    def test_an_inactive_strategy_is_not_replayed(self):
        """性质 1：`is_active` 是人工总退役开关，退役的不该再收到「要不要放它跑」。"""
        generation = make_generation()
        add_cell(generation, self.alpha, DOWNTREND, state=sl.STATE_FIT)
        self.decision(self.alpha)
        # 走 `.update()` 而不是改属性再 `save()`：这里要的是**库里那一行**变成退役，
        # 与真人拨开关的效果一致。顺带避免 `_Fixture` 共享的那个 Python 对象被改坏后
        # 影响同类的后续用例（DB 会回滚，内存里的属性不会）。
        Strategy.objects.filter(pk=self.alpha.pk).update(is_active=False)
        summary = self.replay(generation=generation)
        self.assertEqual(summary["decisions"], 0)
        self.assertEqual(DeactivationReview.objects.count(), 0)

    def test_every_non_released_status_is_replayed(self):
        """用 `exclude(RELEASED)` 而不是白名单：将来加了第四种状态，默认仍是「它在
        生效中」。这里把当下两个取值都钉一遍。"""
        for status in (DecisionStatus.SUGGESTED.value, DecisionStatus.APPLIED.value):
            with self.subTest(status=status):
                generation = make_generation()
                add_cell(generation, self.alpha, DOWNTREND, state=sl.STATE_UNFIT)
                self.decision(self.alpha, status=status)
                self.assertEqual(self.replay(generation=generation)["decisions"], 1)
                DeactivationReview.objects.all().delete()
                DeactivationDecision.objects.all().delete()


# --------------------------------------------------------------------------- #
# 落了什么
# --------------------------------------------------------------------------- #


class TestWhatIsWritten(_Fixture):
    """性质 3-6：落行的内容与去重。"""

    def test_it_writes_one_row_per_live_decision(self):
        generation = make_generation()
        add_cell(generation, self.alpha, DOWNTREND, state=sl.STATE_UNFIT)
        add_cell(generation, self.beta, DOWNTREND, state=sl.STATE_FIT)
        self.decision(self.alpha)
        self.decision(self.beta)
        summary = self.replay(generation=generation)
        self.assertEqual(summary["decisions"], 2)
        self.assertEqual(summary["written"], 2)
        self.assertEqual(DeactivationReview.objects.count(), 2)

    def test_reviewed_at_round_trips(self):
        """性质 4：`reviewed_at` 没有默认值，必须显式传——落库之后要读得回来。"""
        generation = make_generation()
        add_cell(generation, self.alpha, DOWNTREND, state=sl.STATE_UNFIT)
        self.decision(self.alpha)
        self.replay(generation=generation)
        row = DeactivationReview.objects.get()
        self.assertEqual(row.reviewed_at, NOW)

    def test_the_same_generation_is_replayed_only_once(self):
        """性质 2：第二次跑同一代，`written` 是 0 而不是「报了 1 条」。"""
        generation = make_generation()
        add_cell(generation, self.alpha, DOWNTREND, state=sl.STATE_UNFIT)
        self.decision(self.alpha)
        self.assertEqual(self.replay(generation=generation)["written"], 1)

        second = self.replay(generation=generation)
        self.assertEqual(second["written"], 0)
        self.assertEqual(second["decisions"], 1)
        self.assertEqual(second["already"], 1)
        self.assertEqual(DeactivationReview.objects.count(), 1)

    def test_a_new_generation_replays_the_same_decision_again(self):
        """换代之后同一条决策要重放第二次——`already` 只挡同一代。"""
        self.decision(self.alpha)
        for _ in range(2):
            generation = make_generation()
            add_cell(generation, self.alpha, DOWNTREND, state=sl.STATE_UNFIT)
            self.assertEqual(self.replay(generation=generation)["written"], 1)
        self.assertEqual(DeactivationReview.objects.count(), 2)

    def test_the_evidence_carries_the_cell_verbatim_and_the_stale_basis(self):
        """性质 5：依据是**副本**，且「决策冻的是哪一代」与「它过期了没有」都在。"""
        old = make_generation()
        generation = make_generation()
        add_cell(
            generation,
            self.alpha,
            DOWNTREND,
            state=sl.STATE_UNFIT,
            reason="segment_calmar",
            evidence={"trades": 41, "months": 6},
        )
        self.decision(self.alpha, pool_rebuild_id=old.id)
        self.replay(generation=generation)

        row = DeactivationReview.objects.get()
        evidence = row.evidence
        self.assertEqual(evidence["cell"], {"trades": 41, "months": 6})
        self.assertEqual(evidence["reason"], "segment_calmar")
        self.assertEqual(evidence["state"], sl.STATE_UNFIT)
        self.assertEqual(evidence["pool_rebuild_id"], generation.id)
        self.assertEqual(evidence["pool_version"], generation.pool_version)
        self.assertEqual(evidence["decision_pool_rebuild_id"], old.id)
        self.assertTrue(evidence["stale_basis"])
        self.assertIsNone(evidence["previous_verdict"])

    def test_stale_basis_is_false_when_the_decision_froze_this_generation(self):
        generation = make_generation()
        add_cell(generation, self.alpha, DOWNTREND, state=sl.STATE_UNFIT)
        self.decision(self.alpha, pool_rebuild_id=generation.id)
        self.replay(generation=generation)
        self.assertFalse(DeactivationReview.objects.get().evidence["stale_basis"])

    def test_the_verdict_is_the_replay_tier(self):
        generation = make_generation()
        add_cell(generation, self.alpha, DOWNTREND, state=sl.STATE_FIT)
        add_cell(generation, self.beta, DOWNTREND, state=sl.STATE_UNFIT)
        self.decision(self.alpha)
        self.decision(self.beta)
        self.replay(generation=generation)
        by_strategy = {
            row.decision.strategy_id: row.verdict
            for row in DeactivationReview.objects.select_related("decision")
        }
        self.assertEqual(by_strategy[self.alpha.id], ReviewVerdict.BECAME_FIT.value)
        self.assertEqual(by_strategy[self.beta.id], ReviewVerdict.STILL_UNFIT.value)

    def test_it_reads_the_cell_of_the_decisions_own_regime(self):
        """性质 3 的 DB 侧：决策记在上涨段，读的就是上涨段那一格。"""
        generation = make_generation()
        add_cell(generation, self.alpha, DOWNTREND, state=sl.STATE_UNFIT)
        add_cell(generation, self.alpha, UPTREND, state=sl.STATE_FIT)
        self.decision(self.alpha, regime=UPTREND)
        self.replay(generation=generation)
        self.assertEqual(
            DeactivationReview.objects.get().verdict, ReviewVerdict.BECAME_FIT.value
        )

    def test_a_missing_cell_is_still_neutral_and_says_the_state_is_empty(self):
        generation = make_generation()
        self.decision(self.alpha)  # 池化表里没有这一格
        summary = self.replay(generation=generation)
        self.assertEqual(summary["written"], 1)
        row = DeactivationReview.objects.get()
        self.assertEqual(row.verdict, ReviewVerdict.STILL_NEUTRAL.value)
        self.assertEqual((row.evidence["state"], row.evidence["cell"]), ("", {}))

    def test_previous_verdict_comes_from_the_latest_replay(self):
        """性质 6：`previous_verdict` 取最近一次（不限哪一代）。"""
        decision = self.decision(self.alpha)
        first = make_generation(finished_at=days(0))
        second = make_generation(finished_at=days(1))
        self.reviewed(
            decision, first, verdict=ReviewVerdict.STILL_UNFIT.value, at=days(0)
        )
        self.reviewed(
            decision, second, verdict=ReviewVerdict.STILL_NEUTRAL.value, at=days(1)
        )

        generation = make_generation()
        add_cell(generation, self.alpha, DOWNTREND, state=sl.STATE_UNFIT)
        summary = self.replay(generation=generation)
        self.assertEqual(
            DeactivationReview.objects.get(rebuild=generation).evidence[
                "previous_verdict"
            ],
            ReviewVerdict.STILL_NEUTRAL.value,
        )
        # 从中性回到不适用 = 结论变了。
        self.assertEqual(summary["changed"], 1)

    def test_first_replay_counts_as_changed(self):
        generation = make_generation()
        add_cell(generation, self.alpha, DOWNTREND, state=sl.STATE_UNFIT)
        self.decision(self.alpha)
        self.assertEqual(self.replay(generation=generation)["changed"], 1)

    def test_an_unchanged_verdict_is_not_counted_as_changed(self):
        decision = self.decision(self.alpha)
        history = make_generation(finished_at=days(0))
        self.reviewed(
            decision, history, verdict=ReviewVerdict.STILL_UNFIT.value, at=days(0)
        )
        generation = make_generation()
        add_cell(generation, self.alpha, DOWNTREND, state=sl.STATE_UNFIT)
        summary = self.replay(generation=generation)
        self.assertEqual(summary["changed"], 0)

    def test_the_invalidated_list_names_the_affected_strategies(self):
        generation = make_generation()
        add_cell(generation, self.alpha, DOWNTREND, state=sl.STATE_FIT)
        add_cell(generation, self.beta, DOWNTREND, state=sl.STATE_UNFIT)
        self.decision(self.alpha)
        self.decision(self.beta)
        summary = self.replay(generation=generation)
        self.assertEqual(len(summary["invalidated"]), 1)
        item = summary["invalidated"][0]
        self.assertEqual(item["strategy_id"], str(self.alpha.id))
        self.assertEqual(item["name"], "AlphaStem")
        self.assertEqual(item["regime"], DOWNTREND.value)
        self.assertIsNone(item["previous_verdict"])

    def test_replay_does_not_need_a_judgement(self):
        """性质 3：重放一条 `RegimeJudgement` 都不读——库里一条判定都没有也照跑。

        反过来说，若哪天有人把它接回 `deactivation.derive`，那条路的冷启动短路会让这
        条用例从「重放 1 条」变成「重放 0 条」。
        """
        from apps.regime.models import RegimeJudgement

        self.assertEqual(RegimeJudgement.objects.count(), 0)
        generation = make_generation()
        add_cell(generation, self.alpha, DOWNTREND, state=sl.STATE_FIT)
        self.decision(self.alpha)
        self.assertEqual(self.replay(generation=generation)["written"], 1)

    def test_the_default_generation_is_the_current_one(self):
        """不传 `generation` 时取**当前**那一代（最近翻成 ready 的），与
        `deactivation_run` 同一个口径。"""
        stale = make_generation(status=RebuildStatus.BUILDING.value, finished_at=None)
        add_cell(stale, self.alpha, DOWNTREND, state=sl.STATE_FIT)
        current = make_generation(finished_at=days(1))
        add_cell(current, self.alpha, DOWNTREND, state=sl.STATE_UNFIT)
        self.decision(self.alpha)

        summary = self.replay()
        self.assertEqual(summary["generation_id"], current.id)
        # 读到的是当前代（unfit）而不是 building 那代（fit）——否则会误报「依据失效」。
        self.assertEqual(
            DeactivationReview.objects.get().verdict, ReviewVerdict.STILL_UNFIT.value
        )


# --------------------------------------------------------------------------- #
# 收件人
# --------------------------------------------------------------------------- #


class TestAlertRecipients(_Fixture):
    """性质 7：只认实盘 + 活跃状态，去重；空是合法结果。"""

    def test_no_ids_asks_the_database_nothing(self):
        self.assertEqual(replay_run.alert_recipients(()), ())

    def test_a_paper_session_is_not_a_recipient(self):
        self.session(self.alpha, mode="paper")
        self.assertEqual(replay_run.alert_recipients([self.alpha.id]), ())

    def test_every_active_status_counts_and_stopped_does_not(self):
        from apps.trading.models import LiveSession

        for status in LiveSession.ACTIVE_STATUSES:
            with self.subTest(status=status):
                session = self.session(self.alpha, status=status)
                self.assertEqual(
                    replay_run.alert_recipients([self.alpha.id]), (self.user.id,)
                )
                session.delete()
        self.session(self.alpha, status="stopped")
        self.assertEqual(replay_run.alert_recipients([self.alpha.id]), ())

    def test_users_are_deduplicated_and_only_for_the_given_strategies(self):
        self.session(self.alpha)
        self.session(self.alpha)  # 同一个人、同一个策略的两条会话
        self.session(self.beta)  # 不在名单里
        self.assertEqual(replay_run.alert_recipients([self.alpha.id]), (self.user.id,))

    def test_several_users_are_ordered_by_id(self):
        self.session(self.alpha, user=self.other)
        self.session(self.alpha, user=self.user)
        got = replay_run.alert_recipients([self.alpha.id])
        self.assertEqual(got, tuple(sorted({self.user.id, self.other.id})))
        self.assertEqual(len(got), 2)


# --------------------------------------------------------------------------- #
# 返回值形状
# --------------------------------------------------------------------------- #


class TestTheSummaryShape(_Fixture):
    """性质 8：键集恒定、可 JSON 序列化、三档计数恒在。"""

    def summaries(self) -> dict[str, dict]:
        empty = self.replay()  # 没有池化表

        generation = make_generation()
        no_decision = self.replay(generation=generation)

        add_cell(generation, self.alpha, DOWNTREND, state=sl.STATE_FIT)
        self.decision(self.alpha)
        with_decision = self.replay(generation=generation)
        return {
            "no_generation": empty,
            "no_decisions": no_decision,
            "normal": with_decision,
        }

    def test_the_key_set_is_constant_across_paths(self):
        """三条收场（没有池化表 / 没有要重放的决策 / 正常落库）的键集必须逐字相同。

        读返回值的人不该为了「有没有池化表」分两次写取键的代码——`skipped` 恒存在正是
        这条的落地：它不是「成功时才有的字段」，而是「这一轮属于哪种收场」的答案。
        """
        keys = {name: set(summary) for name, summary in self.summaries().items()}
        reference = keys["normal"]
        for name, key_set in keys.items():
            with self.subTest(path=name):
                self.assertEqual(key_set, reference)
        self.assertIn("skipped", reference)

    def test_every_summary_is_json_serialisable(self):
        for name, summary in self.summaries().items():
            with self.subTest(path=name):
                json.dumps(summary)

    def test_the_three_verdict_keys_always_exist(self):
        for name, summary in self.summaries().items():
            with self.subTest(path=name):
                self.assertEqual(
                    set(summary["verdicts"]),
                    {verdict.value for verdict in ReviewVerdict},
                )

    def test_the_verdict_counts_add_up_to_what_was_written(self):
        generation = make_generation()
        add_cell(generation, self.alpha, DOWNTREND, state=sl.STATE_FIT)
        add_cell(generation, self.beta, DOWNTREND, state=sl.STATE_UNFIT)
        self.decision(self.alpha)
        self.decision(self.beta)
        summary = self.replay(generation=generation)
        self.assertEqual(sum(summary["verdicts"].values()), summary["written"])
        self.assertEqual(summary["verdicts"][ReviewVerdict.BECAME_FIT.value], 1)
        self.assertEqual(summary["verdicts"][ReviewVerdict.STILL_UNFIT.value], 1)

    def test_the_invalidated_list_matches_the_became_fit_count(self):
        generation = make_generation()
        add_cell(generation, self.alpha, DOWNTREND, state=sl.STATE_FIT)
        add_cell(generation, self.beta, DOWNTREND, state=sl.STATE_UNFIT)
        self.decision(self.alpha)
        self.decision(self.beta)
        summary = self.replay(generation=generation)
        self.assertEqual(
            len(summary["invalidated"]),
            summary["verdicts"][ReviewVerdict.BECAME_FIT.value],
        )


# --------------------------------------------------------------------------- #
# 命令那一层：告警由入口发
# --------------------------------------------------------------------------- #


class TestTheCommandAlerts(_Fixture):
    """「没有先例的桥」：管理命令用 `async_to_sync` 过桥投递告警。

    告警不在 `replay_run` 里发（那一层只算差异），理由是 `apps/regime/tasks.py` 的
    「任务自己不写告警」——同一条纪律在更硬的版本上：收件人要读 `LiveSession`，那是
    另一件事的取数。所以这里钉的是入口的三件事：**发出去、没发出去要说、发失败不
    影响重算结果本身**。
    """

    def command(self):
        from apps.regime.management.commands.recompute_regime_slices import Command

        cmd = Command()
        cmd.stdout = OutputWrapper(io.StringIO())
        cmd.stderr = OutputWrapper(io.StringIO())
        return cmd

    def summary(self, invalidated) -> dict:
        return {
            "generation_id": 7,
            "skipped": None,
            "note": "",
            "decisions": len(invalidated),
            "written": len(invalidated),
            "already": 0,
            "changed": len(invalidated),
            "verdicts": {
                ReviewVerdict.STILL_UNFIT.value: 0,
                ReviewVerdict.STILL_NEUTRAL.value: 0,
                ReviewVerdict.BECAME_FIT.value: len(invalidated),
            },
            "invalidated": invalidated,
        }

    def item(self, strategy, *, name: str | None = None) -> dict:
        return {
            "strategy_id": str(strategy.id),
            "name": strategy.name if name is None else name,
            "regime": DOWNTREND.value,
            "previous_verdict": None,
        }

    def test_it_delivers_to_every_affected_user(self):
        self.session(self.alpha, user=self.user)
        self.session(self.alpha, user=self.other)
        send = AsyncMock(return_value=True)
        cmd = self.command()
        with (
            patch.object(
                replay_run,
                "run_replay",
                return_value=self.summary([self.item(self.alpha)]),
            ),
            patch("apps.trading.alerts.notify_user", send),
        ):
            cmd._replay_decisions()
        self.assertEqual(send.await_count, 2)
        self.assertEqual(
            {call.args[0] for call in send.await_args_list},
            {self.user.id, self.other.id},
        )
        # 正文就是 `format_alert` 那一句：只描述、不含动作、把出口指出来。
        self.assertIn("不会自动恢复", send.await_args_list[0].args[1])
        self.assertIn("已通知 2 人", cmd.stdout.getvalue())

    def test_no_recipient_is_reported_not_silently_dropped(self):
        """第①段是零执行的 Shadow，一条实盘会话都没有是常态。那时必须**说出来**，
        否则一次没送出去的告警与一次送出去的告警长得一模一样。"""
        send = AsyncMock(return_value=True)
        cmd = self.command()
        with (
            patch.object(
                replay_run,
                "run_replay",
                return_value=self.summary([self.item(self.alpha)]),
            ),
            patch("apps.trading.alerts.notify_user", send),
        ):
            cmd._replay_decisions()
        send.assert_not_awaited()
        out = cmd.stdout.getvalue()
        self.assertIn("没有可通知的收件人", out)
        # 清单仍然打出来了——触发者就站在 stdout 前面，「没有收件人」不等于「没人看见」。
        self.assertIn("AlphaStem", out)

    def test_a_failed_delivery_is_reported_and_does_not_raise(self):
        self.session(self.alpha, user=self.user)
        cmd = self.command()
        with (
            patch.object(
                replay_run,
                "run_replay",
                return_value=self.summary([self.item(self.alpha)]),
            ),
            patch(
                "apps.trading.alerts.notify_user",
                AsyncMock(side_effect=RuntimeError("boom")),
            ),
        ):
            cmd._replay_decisions()  # 不抛
        self.assertIn("投递异常", cmd.stderr.getvalue())

    def test_a_false_return_is_reported_as_undelivered(self):
        """`notify_user` 拿不到接收人时返回 `False`（「没有接收人就不算告警」）。"""
        self.session(self.alpha, user=self.user)
        cmd = self.command()
        with (
            patch.object(
                replay_run,
                "run_replay",
                return_value=self.summary([self.item(self.alpha)]),
            ),
            patch("apps.trading.alerts.notify_user", AsyncMock(return_value=False)),
        ):
            cmd._replay_decisions()
        self.assertIn("未送达", cmd.stderr.getvalue())
        self.assertNotIn("已通知", cmd.stdout.getvalue())

    def test_nothing_invalidated_means_no_alert_at_all(self):
        send = AsyncMock(return_value=True)
        cmd = self.command()
        with (
            patch.object(replay_run, "run_replay", return_value=self.summary([])),
            patch("apps.trading.alerts.notify_user", send),
        ):
            cmd._replay_decisions()
        send.assert_not_awaited()
        self.assertIn("本轮没有新的重放", cmd.stdout.getvalue())

    def test_a_skip_is_reported_and_does_not_alert(self):
        send = AsyncMock(return_value=True)
        summary = self.summary([])
        summary["skipped"] = replay_run.deactivation_run.SKIPPED_NO_GENERATION
        summary["note"] = "还没有任何一代池化表，本轮没有新表可重放"
        cmd = self.command()
        with (
            patch.object(replay_run, "run_replay", return_value=summary),
            patch("apps.trading.alerts.notify_user", send),
        ):
            cmd._replay_decisions()
        send.assert_not_awaited()
        self.assertIn("还没有任何一代池化表", cmd.stdout.getvalue())
