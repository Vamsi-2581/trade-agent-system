"""停用决策的取数与落库（第①段单元 7iii 的 DB 侧）。

`test_deactivation.py` 钉的是**推导**（纯函数：给一份格子与一个阶段，算出该停谁）；
这个文件钉的是它的另一半——四条输入各自从哪里取、算出来的结论怎么写进
`DeactivationDecision`。判定逻辑在这里一行都不断言，那会是第二处真相。

八条性质，每一条坏了都不报警、只出错的数：

1. **三种「什么都不动」各自说得出理由**：冷启动（`cold_start`）、状态过期
   （`stale_state`）、阶段有了却一代池化表都没有（`no_generation`——那不是「没有
   建议」，是**没法问**）。三者都落成 `skipped` 并带一句给人看的话：**不静默**是这条
   纪律的一半，「日志不算被看见」是另一半，所以那句话必须在返回值里，由任务与日报
   接着往上传。
2. **`skipped` 键恒存在**（没事时是 `None`）。一个会因为缺键而变成「没事」的字段，
   迟早会被当成没事——所以这里与判定的「有问题才给 `skipped`」刻意不同。
3. **决策冻的是它自己那一刻的依据**：`evidence["cell"]` 是池化那一格依据摘要的**副本**，
   而池化表日后整体换代、还会重算——这一行必须永远答得出「当初依据的是哪一代、
   那一格写了什么」。
4. **重跑只推进 `last_confirmed_at`**：同一个结论第二次推导出来不新建行、不改证据、
   不动 `first_decied_at`、不动豁免指针，也**不动 `status`**：那一列被第③段的 gate 层
   翻过之后由它自己保管（Q9），本层重跑既不重置它、也不删那一行。后三条各自是「当初
   依据的是哪一代」「当初它是不是被豁免过」的唯一答案，被顺手抹掉就再也问不出来了。
5. **配对读取**：`evidence["pool_rebuild_id"]` 与它引用的那一格来自**同一代**。分两次
   读当前代（一次拿代、一次拿格子）会落成「依据写的是第 N 代、引用的却是第 N+1 代
   那一格」，而这种错在日报上看起来完全正常。
6. **被管策略集合是一筛一标**：判据一（解析得到实现类）筛掉幽灵策略行——它们**有格子
   也不产出建议**，只在 `unresolved`/`unmanaged` 里报数；判据二只标记，且 `mode` 只认
   `live`、`status` 只认 `LiveSession.ACTIVE_STATUSES`（不在这里手抄状态字面量）。
7. **豁免的「在期」是三条一起**（未关闭 / 已生效 / 未到期），而「阶段离开」的一次性
   关闭**不可逆**——所以三种「说不清当前阶段」的收场下关闭一律停手，**保命档期间也停手**
   （高波动是叠加层，不是「离开」）。两个方向不对称，就往不会造成不可逆损失的那边倒。
8. **返回值可 JSON 序列化且键集恒定**：它进 Celery 结果与日报，键集随路径漂移会让
   「今天和昨天有什么不同」多出一堆假差异。

DB 用例一律用真事务回滚的 `TestCase`；策略一律是**真的 `Strategy` 行**——`Strategy.id`
是 UUID 主键而不是整数，拿整数当主键写死会在真库上静默错位。注册表用替身装两个策略，
**不真跑 `discover()`**：它是进程级副作用、会把宿主机那份策略目录拖进来，让用例的结论
依赖运行环境（`ensure_strategies_discovered` 自己的 docstring 说的就是这件事）。
"""

from __future__ import annotations

import itertools
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.regime import deactivation as dea
from apps.regime import deactivation_run as dru
from apps.regime import judgement, pool, pool_rebuild
from apps.regime import slice as sl
from apps.regime.config import JudgementLifecycleConfig
from apps.regime.models import (
    DecisionStatus,
    DeactivationDecision,
    DeactivationExemption,
    RebuildStatus,
    RegimeJudgement,
    RegimePoolCell,
    RegimePoolRebuild,
)
from apps.regime.pool import POOL_SOURCE_STRATEGY
from apps.regime.quant import BaseRegime

DOWNTREND = BaseRegime.DOWNTREND
UPTREND = BaseRegime.UPTREND

#: 生命周期参数：过期门槛 3 天（默认值）。默认的 5 天待机不参与本模块，但参数是同一个
#: 配置类，这里一并给上——传半份配置会让「用错了哪一组参数」看不出来。
LIFE = JudgementLifecycleConfig(min_dwell_days=5, stale_after_days=3)

NOW = datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)

#: 世代指纹的默认值。模型上有一条「就绪代的指纹唯一」的部分唯一约束，同一个类里的用例
#: 每次造代都得给一个新指纹——让计数器兜底，省得每个用例都去编一个字符串。
_fingerprints = itertools.count(1)


def days(n: int) -> datetime:
    return NOW + timedelta(days=n)


# --------------------------------------------------------------------------- #
# 注册表替身与夹具
# --------------------------------------------------------------------------- #


def probe_class(stem: str, archetype: str) -> type:
    """一个够用的策略替身：注册表只要 `description`（`validate_description` 的四字段）。

    与 `test_pool_rebuild.probe_class` 同形，`archetype` 之外一律写「基线」——这里其实
    不在乎原型，写全四字段只是为了让 `StrategyRegistry.register` 收下它。
    """
    return type(
        f"Probe_{stem}",
        (),
        {
            "name": stem,
            "description": (
                f"策略类型：{archetype}\n核心指标：基线\n适用场景：基线\n入场逻辑：基线\n"
            ),
        },
    )


def make_generation(
    *,
    status: str = RebuildStatus.READY.value,
    fingerprint: str | None = None,
    finished_at: datetime | None = None,
    cells: tuple = (),
) -> RegimePoolRebuild:
    """人工造一代。绕过 `rebuild_pool`，好让用例只问它真正在问的那一件事。"""
    generation = RegimePoolRebuild.objects.create(
        status=status,
        actor_kind="task",
        actor_name="任务",
        pool_version=pool.POOL_VERSION,
        input_fingerprint=fingerprint or f"f{next(_fingerprints)}",
        started_at=finished_at or NOW,
        finished_at=finished_at,
    )
    for strategy, regime, kwargs in cells:
        add_cell(generation, strategy, regime, **kwargs)
    return generation


def add_cell(
    generation,
    strategy,
    regime,
    *,
    state: str = sl.STATE_FIT,
    reason: str = "",
    source: str = POOL_SOURCE_STRATEGY,
    evidence: dict | None = None,
    needs_review: bool = False,
) -> RegimePoolCell:
    return RegimePoolCell.objects.create(
        rebuild=generation,
        strategy=strategy,
        regime=regime.value,
        source=source,
        state=state,
        reason=reason,
        evidence={} if evidence is None else evidence,
        needs_review=needs_review,
    )


class _Fixture(TestCase):
    """三个策略：两个解析得到实现类，一个（幽灵）解析不到；外加一个用户给会话用。"""

    @classmethod
    def setUpTestData(cls):
        from apps.trading.models import Strategy

        cls.alpha = Strategy.objects.create(
            name="AlphaStem", code_path="/t/alpha.py", git_commit_hash="aaaaaaa"
        )
        cls.beta = Strategy.objects.create(
            name="BetaStem", code_path="/t/beta.py", git_commit_hash="bbbbbbb"
        )
        cls.gamma = Strategy.objects.create(
            name="GammaStem", code_path="/t/gamma.py", git_commit_hash="ccccccc"
        )
        # `AUTH_USER_MODEL = "authentication.User"`：email 是必填的第一个位置参数。
        cls.user = get_user_model().objects.create_user(
            email="deactivation_run@test.local",
            username="deactivation_run",
            password="pw12345",
        )

    def setUp(self):
        from apps.strategy_engine.registry import StrategyRegistry

        # 补一次发现这件事**在这里被换掉**，不是被跳过：`managed_set` 的 docstring 说
        # 它是「注册表是唯一出处」的保证，那条保证另有一个用例专门钉住。
        self.discover = self.enterContext(
            patch.object(pool_rebuild, "ensure_strategies_discovered", return_value=[])
        )
        for name, archetype in (("AlphaStem", "均值回归"), ("BetaStem", "趋势跟踪")):
            StrategyRegistry.register(probe_class(name, archetype), name=name)
            self.addCleanup(StrategyRegistry._strategies.pop, name, None)

    # --- 造数据的三件套 ---------------------------------------------------- #

    def judged(
        self,
        *,
        regime: str = DOWNTREND,
        effective_regime: str | None = None,
        effective_at: datetime | None = None,
    ):
        """一条生效中的判定。`attribute_date` 比生效日早两天（今天的映射如此），但模型
        层不校验这个映射，本文件也不依赖它。

        `effective_regime` 与 `regime` 分开是给保命档用的：高波动是**被资讯抬上来的**，
        那一行的基础阶段仍是它原本那一档（`base_regime` 是纯量化口径，`effective_regime`
        是抬升后的值）。默认两者相同——多数用例问的不是这个分岔。
        """
        moment = effective_at or NOW
        return RegimeJudgement.objects.create(
            symbol=judgement.SYMBOL,
            attribute_date=(moment - timedelta(days=2)).date(),
            effective_at=moment,
            base_regime=regime,
            effective_regime=effective_regime or regime,
        )

    def exempt(
        self,
        strategy,
        regime: str = DOWNTREND,
        *,
        granted_at: datetime | None = None,
        expires_at: datetime | None = None,
        closed_at: datetime | None = None,
        closed_reason: str = "",
    ):
        return DeactivationExemption.objects.create(
            strategy=strategy,
            regime=regime,
            granted_at=granted_at or NOW,
            expires_at=expires_at or days(10),
            granted_by="测试",
            closed_at=closed_at,
            closed_reason=closed_reason,
        )

    def session(self, strategy, *, mode: str = "live", status: str = "running"):
        from apps.trading.models import LiveSession

        return LiveSession.objects.create(
            user=self.user,
            strategy=strategy,
            symbol="BTC/USDT",
            mode=mode,
            status=status,
            initial_capital=Decimal("10000.00"),
        )

    def decide(self, **kwargs):
        """跑一轮。**不叫 `run`**：`unittest.TestCase.run` 是框架自己的入口，覆盖掉它
        会让每条用例在框架调用 `self.decide(result)` 时炸在参数个数上。"""
        kwargs.setdefault("now", NOW)
        kwargs.setdefault("params", LIFE)
        return dru.run_deactivation(**kwargs)


# --------------------------------------------------------------------------- #
# 被管策略集合：一筛一标
# --------------------------------------------------------------------------- #


class TestManagedSet(_Fixture):
    """性质 6。判据一筛掉幽灵行，判据二只标记。"""

    def test_a_strategy_without_an_implementation_class_is_not_managed(self):
        got = dru.managed_set()
        self.assertEqual(set(got.ids), {self.alpha.id, self.beta.id})
        self.assertEqual(got.unresolved, (self.gamma.id,))

    def test_the_registry_is_refreshed_before_the_set_is_read(self):
        """`apps.ready()` 只在策略目录当时存在时才 discover；长驻 worker 可能在那之前
        就起来了，于是整批策略解析不到实现类、集合安静地缩成空。入口自己补一次。"""
        dru.managed_set()
        self.discover.assert_called_once()

    def test_the_order_is_stable_across_calls(self):
        """集合要进日报与任务返回值：顺序不稳会让「今天和昨天有什么不同」多出一堆
        假差异。`created_at` 在本例里同微秒，兜底排的是主键，所以钉的是**可重复**。"""
        self.assertEqual(dru.managed_set().ids, dru.managed_set().ids)

    def test_running_needs_a_live_active_session(self):
        self.session(self.alpha, mode="live", status="running")
        self.session(self.beta, mode="paper", status="running")
        got = dru.running_strategy_ids((self.alpha.id, self.beta.id))
        self.assertEqual(got, (self.alpha.id,))

    def test_every_active_status_counts_and_stopped_does_not(self):
        """`status` 读的是模型上那个常量，不是这里手抄的一份字面量。"""
        from apps.trading.models import LiveSession

        for status in LiveSession.ACTIVE_STATUSES:
            with self.subTest(status=status):
                session = self.session(self.alpha, mode="live", status=status)
                self.assertEqual(
                    dru.running_strategy_ids((self.alpha.id,)), (self.alpha.id,)
                )
                session.delete()
        self.session(self.alpha, mode="live", status="stopped")
        self.assertEqual(dru.running_strategy_ids((self.alpha.id,)), ())

    def test_no_ids_asks_the_database_nothing(self):
        self.assertEqual(dru.running_strategy_ids(()), ())


# --------------------------------------------------------------------------- #
# 三种「什么都不动」
# --------------------------------------------------------------------------- #


class TestNothingIsWrittenWhenThereIsNothingToAskAbout(_Fixture):
    """性质 1 + 2：不产出，但要说出来，且每条收场各有各的理由。"""

    def test_cold_start_produces_nothing_and_says_so(self):
        make_generation(cells=((self.alpha, DOWNTREND, {"state": sl.STATE_UNFIT}),))
        summary = self.decide()
        self.assertEqual(summary["skipped"], dea.BLOCKED_COLD_START)
        self.assertTrue(summary["note"])
        self.assertIsNone(summary["regime"])
        self.assertEqual(summary["written"], 0)
        self.assertFalse(DeactivationDecision.objects.exists())

    def test_a_stale_state_produces_nothing_and_says_so(self):
        self.judged(effective_at=days(-4))
        make_generation(cells=((self.alpha, DOWNTREND, {"state": sl.STATE_UNFIT}),))
        summary = self.decide()
        self.assertEqual(summary["skipped"], dea.BLOCKED_STALE_STATE)
        self.assertFalse(DeactivationDecision.objects.exists())
        # 「日志不算被看见」的另一半：这句话要带上能让人动手的数字。
        self.assertIn("4", summary["note"])
        self.assertIn("3", summary["note"])

    def test_a_missing_generation_is_its_own_outcome(self):
        """那不是「没有建议」，是**没法问**——适用性表还没建过。"""
        self.judged()
        summary = self.decide()
        self.assertEqual(summary["skipped"], dru.SKIPPED_NO_GENERATION)
        self.assertIsNone(summary["generation_id"])
        self.assertIn("recompute_regime_slices", summary["note"])
        self.assertFalse(DeactivationDecision.objects.exists())

    def test_skipped_is_present_and_none_on_the_normal_path(self):
        self.judged()
        make_generation(cells=((self.alpha, DOWNTREND, {"state": sl.STATE_FIT}),))
        summary = self.decide()
        self.assertIn("skipped", summary)
        self.assertIsNone(summary["skipped"])
        self.assertEqual(summary["note"], "")

    def test_the_surviving_state_still_reports_what_it_would_have_suggested(self):
        """过期不是「算不出来」：日报要能说「若状态仍有效则会有 N 条」——那正是让人
        知道该去查判定任务的数字。"""
        self.judged(effective_at=days(-4))
        make_generation(cells=((self.alpha, DOWNTREND, {"state": sl.STATE_UNFIT}),))
        summary = self.decide()
        self.assertEqual(summary["targets"], 1)
        self.assertEqual(summary["written"], 0)


# --------------------------------------------------------------------------- #
# 落下的那一条决策
# --------------------------------------------------------------------------- #


class TestTheSuggestionWritten(_Fixture):
    """性质 3 + 4 + 5：冻什么、推进什么、从哪一代取。"""

    def setUp(self):
        super().setUp()
        self.judged()

    def test_an_unfit_cell_produces_one_suggested_decision(self):
        generation = make_generation(
            cells=(
                (
                    self.alpha,
                    DOWNTREND,
                    {
                        "state": sl.STATE_UNFIT,
                        "reason": sl.REASON_INSUFFICIENT,
                        "evidence": {"trades": 7},
                    },
                ),
            )
        )
        summary = self.decide()
        row = DeactivationDecision.objects.get()
        self.assertEqual(row.strategy_id, self.alpha.id)
        self.assertEqual(row.regime, DOWNTREND)
        self.assertEqual(row.status, DecisionStatus.SUGGESTED.value)
        self.assertEqual(row.pool_rebuild_id, generation.id)
        self.assertEqual(row.evidence["pool_rebuild_id"], generation.id)
        self.assertEqual(row.evidence["pool_version"], pool.POOL_VERSION)
        self.assertEqual(row.evidence["cell"], {"trades": 7})
        self.assertEqual(row.evidence["state"], sl.STATE_UNFIT)
        self.assertEqual(row.evidence["reason"], sl.REASON_INSUFFICIENT)
        self.assertEqual(row.evidence["regime"], DOWNTREND)
        self.assertEqual(row.evidence["regime_effective_at"], NOW.isoformat())
        self.assertIs(row.evidence["needs_review"], False)
        self.assertEqual(row.first_decided_at, NOW)
        self.assertEqual(row.last_confirmed_at, NOW)
        self.assertEqual(summary["targets"], 1)
        self.assertEqual(summary["created"], 1)
        self.assertEqual(summary["confirmed"], 0)
        self.assertEqual(summary["written"], 1)
        self.assertEqual(summary["suggestions"][0]["strategy_id"], str(self.alpha.id))

    def test_a_status_the_gate_layer_flipped_is_never_reset_here(self):
        """Q9：`applied` / `released` 是第③段 gate 层翻的，本层不认那两档、也不翻回去。

        重跑走的是 `update_or_create` 的 `defaults` 那一支（只有 `last_confirmed_at`），
        新建那一支才写得出 `suggested`——所以「机制领过它」这件事在重跑里一个字都不动。
        把 `status` 顺手放回 `defaults`，表现会是「一轮推导悄悄把一条正在拦人的决策标回
        `suggested`」：日报与 gate 的 `_untouched` 都读这一列，而两边看起来都正常。
        """
        make_generation(cells=((self.alpha, DOWNTREND, {"state": sl.STATE_UNFIT}),))
        self.decide()
        for flipped in (DecisionStatus.APPLIED, DecisionStatus.RELEASED):
            DeactivationDecision.objects.update(status=flipped.value)

            summary = self.decide(now=days(1))

            self.assertEqual(summary["confirmed"], 1, flipped)
            self.assertEqual(summary["created"], 0, flipped)
            # 行不新建、不删除，`status` 原样。
            self.assertEqual(DeactivationDecision.objects.count(), 1, flipped)
            row = DeactivationDecision.objects.get()
            self.assertEqual(row.status, flipped.value, flipped)
            self.assertEqual(row.last_confirmed_at, days(1))

    def test_a_conflicted_cell_produces_no_decision(self):
        """冲突格子照常产出池化结论、照常被引用，但**不产生停用建议**（Q3）。"""
        make_generation(
            cells=(
                (
                    self.alpha,
                    DOWNTREND,
                    {
                        "state": sl.STATE_UNFIT,
                        "reason": sl.REASON_DIRECTION_CONFLICT,
                        "needs_review": True,
                    },
                ),
            )
        )
        summary = self.decide()
        self.assertFalse(DeactivationDecision.objects.exists())
        self.assertEqual(summary["needs_review"], 1)
        self.assertEqual(summary["written"], 0)

    def test_a_strategy_without_a_cell_is_counted_not_suggested(self):
        make_generation(cells=((self.beta, DOWNTREND, {"state": sl.STATE_FIT}),))
        summary = self.decide()
        self.assertEqual(summary["missing_cells"], 1)
        self.assertEqual(summary["targets"], 0)
        self.assertFalse(DeactivationDecision.objects.exists())

    def test_a_ghost_with_a_cell_is_only_counted(self):
        """幽灵策略行有格子也不产出建议——对它产出建议是给幽灵加动作。"""
        make_generation(
            cells=(
                (self.gamma, DOWNTREND, {"state": sl.STATE_UNFIT}),
                (self.alpha, DOWNTREND, {"state": sl.STATE_FIT}),
            )
        )
        summary = self.decide()
        self.assertEqual(summary["unmanaged"], [str(self.gamma.id)])
        self.assertFalse(DeactivationDecision.objects.exists())

    def test_rerunning_confirms_without_rewriting_the_frozen_evidence(self):
        """同一个结论第二次推导出来：不新建行、不改证据、不动 `first_decided_at`。"""
        generation = make_generation(
            cells=(
                (
                    self.alpha,
                    DOWNTREND,
                    {"state": sl.STATE_UNFIT, "evidence": {"trades": 7}},
                ),
            )
        )
        self.decide()
        # 换代之后那一格的依据变了，但已经冻下的那一行不该跟着动。
        RegimePoolCell.objects.filter(rebuild=generation).update(
            evidence={"trades": 999}
        )

        later = days(1)
        summary = dru.run_deactivation(now=later, params=LIFE)
        row = DeactivationDecision.objects.get()
        self.assertEqual(summary["created"], 0)
        self.assertEqual(summary["confirmed"], 1)
        self.assertEqual(row.evidence["cell"], {"trades": 7})
        self.assertEqual(row.first_decided_at, NOW)
        self.assertEqual(row.last_confirmed_at, later)

    def test_a_cell_from_another_strategy_does_not_leak_into_the_decision(self):
        make_generation(
            cells=(
                (
                    self.alpha,
                    DOWNTREND,
                    {"state": sl.STATE_UNFIT, "evidence": {"trades": 1}},
                ),
                (
                    self.beta,
                    DOWNTREND,
                    {"state": sl.STATE_UNFIT, "evidence": {"trades": 2}},
                ),
            )
        )
        self.decide()
        rows = {row.strategy_id: row for row in DeactivationDecision.objects.all()}
        self.assertEqual(rows[self.alpha.id].evidence["cell"], {"trades": 1})
        self.assertEqual(rows[self.beta.id].evidence["cell"], {"trades": 2})

    def test_the_evidence_and_the_generation_id_come_from_the_same_read(self):
        """把「两次读当前代」这个故障形状摆出来：第一次问给 gen1，第二次问给 gen2。

        钉住之后 `current_cells` 收到的是手里那一代，第二次问根本不会发生——所以落进
        决策的是 gen1 的代 id **与** gen1 那一格的依据，而不是「gen1 的 id + gen2 的
        格子」。那种错在日报上看起来完全正常。
        """
        gen1 = make_generation(
            finished_at=days(-1),
            cells=(
                (
                    self.alpha,
                    DOWNTREND,
                    {"state": sl.STATE_UNFIT, "evidence": {"trades": 1}},
                ),
            ),
        )
        gen2 = make_generation(
            finished_at=NOW,
            cells=(
                (
                    self.alpha,
                    DOWNTREND,
                    {"state": sl.STATE_UNFIT, "evidence": {"trades": 2}},
                ),
            ),
        )
        with patch.object(pool_rebuild, "current_generation", side_effect=[gen1, gen2]):
            summary = self.decide()
        row = DeactivationDecision.objects.get()
        self.assertEqual(summary["generation_id"], gen1.id)
        self.assertEqual(row.pool_rebuild_id, gen1.id)
        self.assertEqual(row.evidence["cell"], {"trades": 1})

    def test_only_the_current_generation_is_read(self):
        """上一代那一格是 `unfit`、当前代是 `fit`：读者只认当前代。"""
        make_generation(
            fingerprint="old",
            finished_at=days(-1),
            cells=((self.alpha, DOWNTREND, {"state": sl.STATE_UNFIT}),),
        )
        make_generation(
            fingerprint="new",
            finished_at=NOW,
            cells=((self.alpha, DOWNTREND, {"state": sl.STATE_FIT}),),
        )
        summary = self.decide()
        self.assertEqual(summary["targets"], 0)
        self.assertFalse(DeactivationDecision.objects.exists())

    def test_a_building_generation_is_not_current(self):
        make_generation(
            status=RebuildStatus.BUILDING.value,
            finished_at=None,
            cells=((self.alpha, DOWNTREND, {"state": sl.STATE_UNFIT}),),
        )
        summary = self.decide()
        self.assertEqual(summary["skipped"], dru.SKIPPED_NO_GENERATION)
        self.assertFalse(DeactivationDecision.objects.exists())


# --------------------------------------------------------------------------- #
# 豁免：在期判据与一次性关闭
# --------------------------------------------------------------------------- #


class TestExemptionIsAReadBack(_Fixture):
    """性质 7 的前一半：三条一起才算在期。"""

    def setUp(self):
        super().setUp()
        self.judged()
        make_generation(cells=((self.alpha, DOWNTREND, {"state": sl.STATE_UNFIT}),))

    def test_an_in_force_exemption_is_carried_as_a_pointer(self):
        """豁免抑制的是**动作**（第③段的 gate 施加之前要查在期豁免），不是结论：
        所以决策照落、指针照挂——「已豁免」这句话因此是可查的。"""
        exemption = self.exempt(self.alpha)
        summary = self.decide()
        self.assertEqual(DeactivationDecision.objects.get().exemption_id, exemption.id)
        self.assertEqual(summary["exempt"], 1)
        self.assertEqual(summary["targets"], 1)

    def test_a_not_yet_granted_exemption_does_not_apply(self):
        self.exempt(self.alpha, granted_at=days(1), expires_at=days(10))
        self.decide()
        self.assertIsNone(DeactivationDecision.objects.get().exemption_id)

    def test_an_expired_exemption_does_not_apply(self):
        self.exempt(self.alpha, granted_at=days(-20), expires_at=days(-1))
        self.decide()
        self.assertIsNone(DeactivationDecision.objects.get().exemption_id)

    def test_a_closed_exemption_does_not_apply(self):
        self.exempt(self.alpha, closed_at=days(-1), closed_reason="regime_left")
        self.decide()
        self.assertIsNone(DeactivationDecision.objects.get().exemption_id)

    def test_an_exemption_for_another_regime_does_not_apply(self):
        """豁免键是（策略 × 阶段）：在别的阶段被豁免过，不构成这个阶段的挡箭牌。"""
        self.exempt(self.alpha, regime=UPTREND)
        self.decide()
        self.assertIsNone(DeactivationDecision.objects.get().exemption_id)

    def test_the_latest_grant_on_the_same_cell_wins(self):
        """同一格重复发过豁免时取最近发出的那条：取错的话日报会说「已豁免」而实际
        在期的是另一条。"""
        self.exempt(self.alpha, granted_at=days(-5), expires_at=days(5))
        later = self.exempt(self.alpha, granted_at=days(-1), expires_at=days(9))
        self.decide()
        self.assertEqual(DeactivationDecision.objects.get().exemption_id, later.id)

    def test_the_exemption_pointer_survives_a_later_run_after_it_closed(self):
        """`exemption` 只在 `defaults` 里：豁免到期关掉之后再跑一轮，那一行仍要答得出
        「当初它是不是被豁免过」。落进 `update_defaults` 会把它抹成 `None`。"""
        exemption = self.exempt(self.alpha)
        self.decide()
        DeactivationExemption.objects.filter(id=exemption.id).update(closed_at=NOW)
        dru.run_deactivation(now=days(1), params=LIFE)
        row = DeactivationDecision.objects.get()
        self.assertEqual(row.exemption_id, exemption.id)
        self.assertEqual(row.evidence["cell"], {})


class TestExemptionClosing(_Fixture):
    """性质 7 的后一半：阶段离开时一次性关闭，且不可逆。"""

    def test_an_exemption_for_a_regime_we_have_left_is_closed(self):
        self.judged()
        make_generation(cells=((self.alpha, DOWNTREND, {"state": sl.STATE_FIT}),))
        exemption = self.exempt(self.alpha, regime=UPTREND)
        summary = self.decide()
        exemption.refresh_from_db()
        self.assertEqual(exemption.closed_at, NOW)
        self.assertEqual(exemption.closed_reason, dru.CLOSE_REASON_REGIME_LEFT)
        self.assertEqual(summary["exemptions_closed"], 1)

    def test_an_exemption_for_the_current_regime_survives(self):
        self.judged()
        make_generation(cells=((self.alpha, DOWNTREND, {"state": sl.STATE_FIT}),))
        exemption = self.exempt(self.alpha)
        summary = self.decide()
        exemption.refresh_from_db()
        self.assertIsNone(exemption.closed_at)
        self.assertEqual(summary["exemptions_closed"], 0)

    def test_an_expired_one_is_not_closed_again(self):
        """到期与「阶段离开」是两条独立的失效条件：已经过了期的既不重关，也不该被
        写成「阶段离开」——那会把一条正常到期的记录说成一次观测。"""
        self.judged()
        make_generation(cells=((self.alpha, DOWNTREND, {"state": sl.STATE_FIT}),))
        exemption = self.exempt(
            self.alpha, regime=UPTREND, granted_at=days(-20), expires_at=days(-1)
        )
        summary = self.decide()
        exemption.refresh_from_db()
        self.assertIsNone(exemption.closed_at)
        self.assertEqual(summary["exemptions_closed"], 0)

    def test_an_already_closed_one_keeps_its_own_reason(self):
        """两次读之间可能有别人（管理命令）关掉了同一条，那时以先到者为准。"""
        self.judged()
        make_generation(cells=((self.alpha, DOWNTREND, {"state": sl.STATE_FIT}),))
        exemption = self.exempt(
            self.alpha, regime=UPTREND, closed_at=days(-1), closed_reason="manual"
        )
        summary = self.decide()
        exemption.refresh_from_db()
        self.assertEqual(exemption.closed_at, days(-1))
        self.assertEqual(exemption.closed_reason, "manual")
        self.assertEqual(summary["exemptions_closed"], 0)

    def test_the_close_is_suspended_when_the_state_is_stale(self):
        """误关一条在期豁免**不可逆**（`closed_at` 写下去没有复活路径），而豁免本身
        还有「到期」兜底——两个方向不对称，就往不会造成不可逆损失的那边倒。"""
        self.judged(effective_at=days(-4))
        exemption = self.exempt(self.alpha, regime=UPTREND)
        summary = self.decide()
        exemption.refresh_from_db()
        self.assertIsNone(exemption.closed_at)
        self.assertEqual(summary["exemptions_closed"], 0)

    def test_the_close_is_suspended_on_cold_start(self):
        exemption = self.exempt(self.alpha, regime=UPTREND)
        summary = self.decide()
        exemption.refresh_from_db()
        self.assertIsNone(exemption.closed_at)
        self.assertEqual(summary["exemptions_closed"], 0)

    def test_the_close_is_suspended_when_there_is_no_generation(self):
        self.judged()
        exemption = self.exempt(self.alpha, regime=UPTREND)
        summary = self.decide()
        exemption.refresh_from_db()
        self.assertIsNone(exemption.closed_at)
        self.assertEqual(summary["exemptions_closed"], 0)

    def test_no_exemption_is_closed_while_the_blanket_is_live(self):
        """保命档是**叠加层**（CONTEXT.md 第 115 条），不是「阶段离开」。

        高波动抬升期间生效阶段是 `high_vol`，而豁免登记的永远是**基础阶段**——照 `!=`
        比下去，用户手上每一条在期豁免都会在高波动第一天被关掉，而 `closed_at` 写下去
        没有复活路径。用户什么都没做，豁免就没了；等保命档过去、系统回落到适用性层，
        那条策略直接进停用。这条路径是活的：高波动本身不 blocked，判定照跑。
        """
        self.judged(regime=DOWNTREND, effective_regime=BaseRegime.HIGH_VOL)
        make_generation(cells=((self.alpha, DOWNTREND, {"state": sl.STATE_FIT}),))
        exemption = self.exempt(self.alpha, regime=DOWNTREND)
        summary = self.decide()
        exemption.refresh_from_db()
        self.assertIsNone(exemption.closed_at)
        self.assertEqual(summary["exemptions_closed"], 0)

    def test_the_close_resumes_once_the_blanket_has_passed(self):
        """高波动只是**延后**了「阶段离开」的判定，不是取消它。

        保命档过去后生效阶段回落到基础阶段；它若已经换了（下行趋势 → 上行趋势），旧豁免
        照常按「阶段离开」关闭。没有这一条，一个「高波动一来就再也不关了」的实现也能让
        上面那条用例全绿。
        """
        self.judged(regime=DOWNTREND, effective_regime=BaseRegime.HIGH_VOL)
        make_generation(cells=((self.alpha, DOWNTREND, {"state": sl.STATE_FIT}),))
        exemption = self.exempt(self.alpha, regime=DOWNTREND)
        self.decide()

        self.judged(regime=UPTREND, effective_at=days(1))
        summary = self.decide(now=days(1))

        exemption.refresh_from_db()
        self.assertEqual(exemption.closed_reason, dru.CLOSE_REASON_REGIME_LEFT)
        self.assertEqual(summary["exemptions_closed"], 1)


# --------------------------------------------------------------------------- #
# 返回值是给下游看的
# --------------------------------------------------------------------------- #


class TestTheSummaryShape(_Fixture):
    """性质 2 + 8：键集恒定，且能直接进 Celery 结果与日报。"""

    def summaries(self) -> dict[str, dict]:
        """三条收场各来一份，外加正常路径一份。"""
        make_generation(cells=((self.alpha, DOWNTREND, {"state": sl.STATE_UNFIT}),))
        cold = self.decide()
        DeactivationDecision.objects.all().delete()

        self.judged(effective_at=days(-4))
        stale = self.decide()

        self.judged()
        dry = self.decide()  # 这一代还是那条 unfit 格子，正常产出

        RegimePoolRebuild.objects.all().delete()
        no_generation = self.decide()
        return {
            "cold": cold,
            "stale": stale,
            "normal": dry,
            "no_generation": no_generation,
        }

    def test_the_key_set_is_constant_across_paths(self):
        summaries = self.summaries()
        keys = set(summaries["normal"])
        for name, summary in summaries.items():
            with self.subTest(path=name):
                self.assertEqual(set(summary), keys)

    def test_every_summary_is_json_serialisable(self):
        """它进 Celery 结果（JSON 序列化）。id 一律是字符串。"""
        for name, summary in self.summaries().items():
            with self.subTest(path=name):
                json.dumps(summary)

    def test_every_skipped_path_carries_a_human_readable_note(self):
        summaries = self.summaries()
        for name, summary in summaries.items():
            with self.subTest(path=name):
                if summary["skipped"] is None:
                    self.assertEqual(summary["note"], "")
                else:
                    self.assertTrue(summary["note"])

    def test_the_three_skipped_paths_are_distinguishable(self):
        summaries = self.summaries()
        self.assertEqual(
            {name: s["skipped"] for name, s in summaries.items()},
            {
                "cold": dea.BLOCKED_COLD_START,
                "stale": dea.BLOCKED_STALE_STATE,
                "normal": None,
                "no_generation": dru.SKIPPED_NO_GENERATION,
            },
        )
