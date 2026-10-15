"""行情阶段 gate 的取数与落库（第③段单元 ③c 的 DB 侧）。

`test_gate.py` 钉的是**判定**（纯函数：一个 `Situation` + 一组 `DecisionRef` → 一张
`GatePlan`）；这个文件钉的是它的另一半——那个 `Situation` 从哪几张表里读出来、那张
`GatePlan` 怎么落到两张表上。判定逻辑在这里一行都不断言，那会是第二处真相。

九条性质，每一条坏了都不报警、只出错的数：

1. **四种「什么都不动」各自说得出理由**：`no_generation`（阶段有了却一代池化表都没有
   ——那不是「没有该停的」，是**没法问**）、冷启动（`cold_start`）、状态过期
   （`stale_state`）、保命档（`gate.BLOCKED_BLANKET`——前三个是「说不清」，这一个是
   **说得清、而判定层不该动手**，理由见 `test_gate.TestTheBlanketTier`）。四种都落成
   `skipped` + 一句给人看的话，且**都不发起对账**：期望集算不出来的时候，活行原样继续拦
   是唯一诚实的动作。
2. **`skipped` 键恒存在，键集在五条路径上一致**，返回值可 JSON 序列化（它进 Celery
   结果与日报）。
3. **声明先写、记录后写**：一次失败的 `status` 回写不该留下「机制声明过它」而声明表里
   空无一物的假历史——反过来才是不可收拾的那一半。
4. **声明表只有一个写方**：本模块把 `GatePlan` 递给 `halt_sync.sync`，自己一行都不建。
5. **Shadow 档解除但不记录**：`gate_closed` 关掉活行，`status` 一个字都不写（Q3）。
   「解除」是回到 Shadow 的意义（不再拦），「不记录」是 Shadow 期没有「机制开始拦」
   这个动作（写 `applied`/`released` 都是假历史）。
6. **三态 warrant 不串味**：`regime_left`（阶段对不上）与 `strategy_gone`（策略离场）
   分得开，判据只看**这一行自己的**那一格；`is_active=False` 走的是离场那一条。
7. **窗口起点取自事实**：四个候选（开关开启 / 判定生效 / 池化落代 / 上次豁免结束）取
   最晚，**永不取 `now()`**（Q4——取 `now()` 会让 `halt_sync._rewrite` 每 300 秒看见
   一次变化，把 `opened_notified_at` 反复清空、用户被反复通知）。豁免提前收回时窗口
   结束于**收回那一刻**而不是原定到期时刻（Q5）。
8. **status 回写只碰一列**：`evidence` 一个字都不动；比对旧值不匹配时**什么都不做**
   （保留别人的写），并留下一条说得出为什么的 warning。
9. **一轮只碰自己那一档**：事件熔断与保命档的行原样留着；一条在册策略之外的活声明行
   既不在期望集里又不在解除名单里时，写入方按 fail-closed 不动它。

与 `test_deactivation_run.py` 同构：真事务回滚的 `TestCase`、真的 `Strategy` 行
（`Strategy.id` 是 UUID 主键而不是整数，拿整数当主键写死会在真库上静默错位）、注册表用
替身装两个策略而**不真跑 `discover()`**（进程级副作用，会把宿主机那份策略目录拖进来）。

**与那份模板有一处刻意的不同**：这里的三个策略都建 `is_active=True`。`Strategy.is_active`
默认 `False`（「回测自动建出的策略行默认 False，需由迁移/管理命令回填」），而
`gate_run._warrant` 把 `not is_active` 判成**离场**（CONTEXT.md 第 131 条：退役即不再
参与求值）。照抄模板的话每一行的 warrant 都会是 `GONE`，于是每条用例都在断言「策略
离场了」——一片假通过。默认值那条路另有一条用例专门钉住。
"""

from __future__ import annotations

import itertools
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.regime import deactivation as dea
from apps.regime import deactivation_run as dru
from apps.regime import gate, halt, halt_sync, judgement, pool, pool_rebuild
from apps.regime import gate_run as grun
from apps.regime import slice as sl
from apps.regime.config import JudgementLifecycleConfig
from apps.regime.models import (
    ActorKind,
    DecisionStatus,
    DeactivationDecision,
    DeactivationExemption,
    HaltDeclaration,
    HaltTrigger,
    MechanismKind,
    MechanismMode,
    RebuildStatus,
    RegimeJudgement,
    RegimeMechanismSwitch,
    RegimePoolCell,
    RegimePoolRebuild,
)
from apps.regime.pool import POOL_SOURCE_STRATEGY
from apps.regime.quant import BaseRegime

DOWNTREND = BaseRegime.DOWNTREND
UPTREND = BaseRegime.UPTREND

#: 生命周期参数（与 `test_deactivation_run` 同一份）：过期门槛 3 天。本模块自己不用它
#: 算任何东西，但 `deactivation.derive` 会——传半份配置会让「用错了哪一组参数」看不出来。
LIFE = JudgementLifecycleConfig(min_dwell_days=5, stale_after_days=3)

NOW = datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)

#: 世代指纹的默认值。模型上有一条「就绪代的指纹唯一」的部分唯一约束，同一个类里的用例
#: 每次造代都得给一个新指纹——让计数器兜底。
_fingerprints = itertools.count(1)


def days(n: int) -> datetime:
    return NOW + timedelta(days=n)


def hours(n: int) -> datetime:
    return NOW + timedelta(hours=n)


def _slug(regime) -> str:
    """`BaseRegime.DOWNTREND` 与 `"downtrend"` 都能收。"""
    return getattr(regime, "value", regime)


# --------------------------------------------------------------------------- #
# 注册表替身与造数据
# --------------------------------------------------------------------------- #


def probe_class(stem: str, archetype: str) -> type:
    """一个够用的策略替身：注册表只要 `description`（`validate_description` 的四字段）。"""
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
    """人工造一代。绕过 `rebuild_pool`，好让用例只问它真正在问的那一件事。

    `finished_at` 不传时落 `NOW` 而不是留空：`current_generation` 按 `-finished_at` 倒序，
    而 PostgreSQL 的 `DESC` 把 NULL 排在**最前**——留空的话，任何「先造一代、再造一代」
    的用例都会拿到先造的那一代，看起来完全正常。`status=BUILDING` 那一类仍然进不了
    `current_generation`（它按 status 过滤），所以这里补上时刻不影响它。
    """
    generation = RegimePoolRebuild.objects.create(
        status=status,
        actor_kind="task",
        actor_name="任务",
        pool_version=pool.POOL_VERSION,
        input_fingerprint=fingerprint or f"f{next(_fingerprints)}",
        started_at=finished_at or NOW,
        finished_at=finished_at or NOW,
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
        regime=_slug(regime),
        source=source,
        state=state,
        reason=reason,
        evidence={} if evidence is None else evidence,
        needs_review=needs_review,
    )


class _Fixture(TestCase):
    """三个策略：两个解析得到实现类，一个（幽灵）解析不到；外加一个用户。

    三个都建 `is_active=True`——见模块 docstring 里那段「与模板刻意的不同」。
    """

    @classmethod
    def setUpTestData(cls):
        from apps.backtest.models import BacktestResult
        from apps.trading.models import Strategy

        # 闸门这一层读的是全表（`pool_rebuild._result_rows()` 不带过滤——生产里它就
        # 该读全部回测结果），所以库里存在既存结果时，「受管集合」就不再只是本用例造
        # 的账。跑在 `db_dev.sqlite3` 上时库里有真实回测结果，先清干净。
        # `TestCase` 的事务会把这一步连同用例一起回滚，真实库毫发无损。
        BacktestResult.objects.all().delete()

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
        cls.gamma = Strategy.objects.create(
            name="GammaStem",
            code_path="/t/gamma.py",
            git_commit_hash="ccccccc",
            is_active=True,
        )
        # `AUTH_USER_MODEL = "authentication.User"`：email 是必填的第一个位置参数。
        cls.user = get_user_model().objects.create_user(
            email="gate_run@test.local",
            username="gate_run",
            password="pw12345",
        )

    def setUp(self):
        from apps.strategy_engine.registry import StrategyRegistry

        # 「补一次发现」在这里被换掉，不是被跳过：`managed_set` 的 docstring 说它是
        # 「注册表是唯一出处」的保证，那条保证另有别的用例钉住（`test_deactivation_run`）。
        self.enterContext(
            patch.object(pool_rebuild, "ensure_strategies_discovered", return_value=[])
        )
        for name, archetype in (("AlphaStem", "均值回归"), ("BetaStem", "趋势跟踪")):
            StrategyRegistry.register(probe_class(name, archetype), name=name)
            self.addCleanup(StrategyRegistry._strategies.pop, name, None)

    # --- 造数据 ------------------------------------------------------------ #

    def judged(
        self,
        *,
        regime: str = DOWNTREND,
        effective_regime: str | None = None,
        effective_at: datetime | None = None,
    ):
        """一条生效中的判定（`gate_run` 只读它的 `effective_regime` 与 `effective_at`）。"""
        moment = effective_at or NOW
        return RegimeJudgement.objects.create(
            symbol=judgement.SYMBOL,
            attribute_date=(moment - timedelta(days=2)).date(),
            effective_at=moment,
            base_regime=_slug(regime),
            effective_regime=_slug(effective_regime or regime),
        )

    def switch(
        self,
        mode: MechanismMode = MechanismMode.EXECUTING,
        *,
        at: datetime | None = None,
        kind: MechanismKind = MechanismKind.REGIME_GATE,
    ):
        """一行机制开关流水。默认是**打开**（`to_mode=EXECUTING`）那一行。"""
        return RegimeMechanismSwitch.objects.create(
            kind=kind.value,
            from_mode=MechanismMode.SHADOW.value,
            to_mode=mode.value,
            at=at or days(-2),
            actor_kind=ActorKind.CLI.value,
            actor_name="ops",
            reason="测试",
        )

    def exemption(
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
            regime=_slug(regime),
            granted_at=granted_at or days(-10),
            expires_at=expires_at or days(10),
            granted_by="测试",
            closed_at=closed_at,
            closed_reason=closed_reason,
        )

    def decision(
        self,
        strategy,
        regime: str = DOWNTREND,
        *,
        status: str = DecisionStatus.SUGGESTED.value,
        evidence: dict | None = None,
        pool_rebuild_id: int | None = None,
    ) -> DeactivationDecision:
        """一条决策行。默认状态是 `suggested`——那是**只有 `deactivation_run` 写过**的值，
        也就是「机制从没碰过它」。"""
        return DeactivationDecision.objects.create(
            strategy=strategy,
            regime=_slug(regime),
            status=status,
            evidence={"cell": {"trades": 7}} if evidence is None else evidence,
            pool_rebuild_id=pool_rebuild_id,
            first_decided_at=NOW,
            last_confirmed_at=NOW,
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

    def live_row(
        self,
        strategy,
        *,
        opened_at: datetime | None = None,
        reason: str = "旧一轮写的",
        actor_name: str = halt_sync.GATE_ACTOR_NAME,
        trigger: HaltTrigger = HaltTrigger.DEACTIVATION,
    ) -> HaltDeclaration:
        """一条上一轮留下的活声明行（策略档）。`opened_at` 默认远早于本轮的窗口起点，
        所以任何「起点变了」的判断都只可能来自本轮自己算出来的东西。"""
        return HaltDeclaration.objects.create(
            trigger=trigger.value,
            scope=halt.strategy_scope(strategy.id),
            label=f"{gate.LABEL_PREFIX}{strategy.name}",
            opened_at=opened_at or hours(-5),
            reason=reason,
            actor_kind=ActorKind.TASK.value,
            actor_name=actor_name,
        )

    def gate_round(self, **kwargs):
        """跑一轮。**不叫 `run`**：`unittest.TestCase.run` 是框架自己的入口。"""
        kwargs.setdefault("now", NOW)
        kwargs.setdefault("params", LIFE)
        return grun.sync(**kwargs)


# --------------------------------------------------------------------------- #
# 性质 1 + 2：三种「什么都不动」
# --------------------------------------------------------------------------- #


class TestTheEarlyExits(_Fixture):
    """期望集算不出来的时候，本条轮次**连对账都不发起**；活行原样继续拦。"""

    def test_a_strategy_round_without_a_generation_does_not_touch_the_live_row(self):
        self.judged()
        row = self.live_row(self.alpha)
        summary = self.gate_round()

        self.assertEqual(summary["skipped"], dru.SKIPPED_NO_GENERATION)
        self.assertEqual(summary["note"], grun.NOTE_NO_GENERATION)
        self.assertIsNone(summary["halt"])
        self.assertEqual(summary["declarations"], [])
        self.assertEqual(summary["statuses"], [])
        self.assertEqual(summary["close_reasons"], {})
        self.assertIsNone(summary["generation_id"])

        row.refresh_from_db()
        self.assertIsNone(row.closed_at)

    def test_a_cold_start_leaves_the_live_row_blocking(self):
        make_generation(cells=((self.alpha, DOWNTREND, {"state": sl.STATE_UNFIT}),))
        self.decision(self.alpha, status=DecisionStatus.APPLIED.value)
        row = self.live_row(self.alpha)

        summary = self.gate_round()

        self.assertEqual(summary["skipped"], dea.BLOCKED_COLD_START)
        self.assertIsNone(summary["halt"])
        row.refresh_from_db()
        self.assertIsNone(row.closed_at)
        # 「说不清阶段」不是「机制撤回过」：记录一个字都不动。
        self.assertEqual(
            DeactivationDecision.objects.get().status, DecisionStatus.APPLIED.value
        )

    def test_a_stale_state_leaves_the_live_row_blocking(self):
        make_generation(cells=((self.alpha, DOWNTREND, {"state": sl.STATE_UNFIT}),))
        self.judged(effective_at=days(-4))
        row = self.live_row(self.alpha)

        summary = self.gate_round()

        self.assertEqual(summary["skipped"], dea.BLOCKED_STALE_STATE)
        self.assertIsNone(summary["halt"])
        row.refresh_from_db()
        self.assertIsNone(row.closed_at)

    def test_no_generation_is_asked_before_anything_else_is_read(self):
        """一代池化表都没有时，连开关与豁免都不必问——那两问的答案无处可用。"""
        self.switch()
        self.judged()
        self.exemption(self.alpha)
        summary = self.gate_round()
        self.assertEqual(summary["skipped"], dru.SKIPPED_NO_GENERATION)


# --------------------------------------------------------------------------- #
# 性质 5：Shadow 档
# --------------------------------------------------------------------------- #


class TestTheShadowTier(_Fixture):
    """开关默认是关的（一条流水都没有 = `SHADOW`）。"""

    def setUp(self):
        super().setUp()
        self.judged()
        make_generation(cells=((self.alpha, DOWNTREND, {"state": sl.STATE_UNFIT}),))

    def test_the_live_row_is_closed_with_gate_closed(self):
        self.decision(self.alpha, status=DecisionStatus.APPLIED.value)
        row = self.live_row(self.alpha)

        summary = self.gate_round()

        self.assertFalse(summary["gate_open"])
        self.assertIsNone(summary["skipped"])
        self.assertEqual(summary["note"], gate.NOTE_SHADOW)
        self.assertEqual(
            summary["close_reasons"], {str(self.alpha.id): gate.CLOSE_GATE_CLOSED}
        )
        self.assertEqual(summary["halt"]["closed"], 1)

        row.refresh_from_db()
        self.assertEqual(row.closed_reason, gate.CLOSE_GATE_CLOSED)
        self.assertEqual(row.closed_at, NOW)

    def test_not_one_status_is_written(self):
        self.decision(self.alpha, status=DecisionStatus.APPLIED.value)
        self.gate_round()
        self.assertEqual(
            DeactivationDecision.objects.get().status, DecisionStatus.APPLIED.value
        )

    def test_no_live_declaration_is_created(self):
        self.decision(self.alpha)
        summary = self.gate_round()
        self.assertEqual(summary["declarations"], [])
        self.assertEqual(
            HaltDeclaration.objects.filter(closed_at__isnull=True).count(), 0
        )


# --------------------------------------------------------------------------- #
# 性质 3 + 4：一轮正常跑起来
# --------------------------------------------------------------------------- #


class TestTheLiveRound(_Fixture):
    """开关打开、阶段说得清、有一条该停的决策行。"""

    def setUp(self):
        super().setUp()
        self.switch()
        self.judged()
        make_generation(
            cells=(
                (
                    self.alpha,
                    DOWNTREND,
                    {"state": sl.STATE_UNFIT, "reason": "本阶段样本偏少"},
                ),
            )
        )

    def test_the_row_carries_the_gate_signature_and_the_strategy_name(self):
        self.decision(self.alpha)

        summary = self.gate_round()

        row = HaltDeclaration.objects.get()
        self.assertEqual(row.trigger, HaltTrigger.DEACTIVATION.value)
        self.assertEqual(row.scope, halt.strategy_scope(self.alpha.id))
        self.assertEqual(row.actor_kind, ActorKind.TASK.value)
        self.assertEqual(row.actor_name, halt_sync.GATE_ACTOR_NAME)
        # 两个名字不是一个：同一个触发源在两轮里出自谁，是排查时的第一个问题。
        self.assertNotEqual(halt_sync.GATE_ACTOR_NAME, halt_sync.ACTOR_NAME)
        # 策略停用没有预先知道的截止时刻。
        self.assertIsNone(row.expires_at)
        # `label` 进下单拒绝理由与 `query_halt`，`reason` 进通知正文与日报——策略名两处都要有。
        self.assertIn("AlphaStem", row.label)
        self.assertIn("本阶段样本偏少", row.label)
        self.assertIn("AlphaStem", row.reason)
        self.assertEqual(summary["halt"]["created"], 1)
        self.assertEqual(summary["targets"], 1)

    def test_the_window_starts_at_the_latest_fact_and_never_at_now(self):
        """Q4：起点取自库里存好的事实（开关开启 / 判定生效 / 池化落代 / 上次豁免结束），
        取最晚。四条事实都发生在 `NOW`，所以在一小时后跑这一轮，起点仍是 `NOW`。"""
        self.decision(self.alpha)

        self.gate_round(now=hours(1))

        self.assertEqual(HaltDeclaration.objects.get().opened_at, NOW)

    def test_the_row_actually_blocks_that_strategy_down_the_order_path(self):
        """声明行不是给报表看的：它要真的在下单通路上拦住那个策略。"""
        self.decision(self.alpha)

        self.gate_round()

        verdict = halt.halt_layers("BTC/USDT", str(self.alpha.id), now=NOW)
        self.assertTrue(verdict.blocked)
        self.assertIn("AlphaStem", verdict.reason)
        # 别人的单照放。
        self.assertFalse(
            halt.halt_layers("BTC/USDT", str(self.beta.id), now=NOW).blocked
        )

    def test_a_second_identical_round_changes_nothing(self):
        self.decision(self.alpha)
        first = self.gate_round()
        self.assertEqual(first["halt"]["created"], 1)

        second = self.gate_round()

        self.assertEqual(
            second["halt"],
            {"created": 0, "updated": 0, "unchanged": 1, "closed": 0},
        )
        self.assertEqual(second["statuses"], [])
        self.assertEqual(len(second["declarations"]), 1)
        self.assertEqual(HaltDeclaration.objects.count(), 1)

    def test_an_unchanged_window_does_not_clear_the_notification_stamp(self):
        """`_rewrite` 只在 `opened_at` 真的动过的时候清 `opened_notified_at`。这条钉的是
        Q4 的动机：起点一旦取自 `now()`，这条戳记每 300 秒被清一次、用户被反复通知。"""
        self.decision(self.alpha)
        self.gate_round()
        HaltDeclaration.objects.update(opened_notified_at=NOW)

        self.gate_round()

        self.assertEqual(HaltDeclaration.objects.get().opened_notified_at, NOW)

    def test_a_declaration_lands_even_when_the_status_write_explodes(self):
        """性质 3：声明是机制对市场的动作，status 是对「机制做过什么」的记录。反过来的
        顺序会在一次失败后留下「机制声明过它」而声明表里空无一物的假历史。"""
        self.decision(self.alpha)

        with patch.object(grun, "_write_statuses", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.gate_round()

        self.assertEqual(HaltDeclaration.objects.count(), 1)
        self.assertEqual(
            DeactivationDecision.objects.get().status, DecisionStatus.SUGGESTED.value
        )

    def test_the_declaration_table_is_written_by_the_sync_and_not_here(self):
        """性质 4：本模块只把期望集递过去，自己不建行——两个写方碰同一张表、各按各的
        期望集对账，那条唯一键迟早会被两边各写一遍。"""
        self.decision(self.alpha)
        planted = {"created": 1, "updated": 0, "unchanged": 0, "closed": 0}

        with patch.object(halt_sync, "sync", return_value=planted) as fake:
            summary = self.gate_round()

        self.assertEqual(fake.call_args.kwargs["actor_name"], halt_sync.GATE_ACTOR_NAME)
        self.assertEqual(
            [d.strategy_id for d in fake.call_args.kwargs["gate_plan"].declarations],
            [self.alpha.id],
        )
        self.assertEqual(HaltDeclaration.objects.count(), 0)
        self.assertEqual(summary["halt"], planted)

    def test_the_mechanism_name_is_imported_not_respelled(self):
        self.assertEqual(grun.ACTOR_NAME, halt_sync.GATE_ACTOR_NAME)


# --------------------------------------------------------------------------- #
# 性质 6：三态 warrant
# --------------------------------------------------------------------------- #


class TestWhatTheWarrantSays(_Fixture):
    """`still_target` / `lapsed` / `gone` 三态，以及它翻译成的解除原因码。

    两层各钉一半：`gate._close_reason` 先看阶段（阶段对不上的行一律 `regime_left`），
    所以「这一行自己那一格还算不算」在整轮的结果里够不着——那三条直接问 `_warrant`；
    三态**落成什么**则由整轮钉。
    """

    def setUp(self):
        super().setUp()
        self.switch()  # 整轮的那些用例需要开关真的开着

    def _cells(self) -> dict:
        return pool_rebuild.current_cells(pool_rebuild.current_generation())

    def _warrant(self, decision, *, managed=None) -> str:
        return grun._warrant(
            decision,
            managed=frozenset({self.alpha.id}) if managed is None else managed,
            cells=self._cells(),
        )

    # --- 判据本身 ---------------------------------------------------------- #

    def test_the_warrant_reads_the_rows_own_cell_not_the_current_regime_column(self):
        """判据查的是**这一行自己的**那一格，不是当前阶段那一列。"""
        make_generation(cells=((self.alpha, UPTREND, {"state": sl.STATE_UNFIT}),))
        decision = self.decision(self.alpha, regime=UPTREND)
        self.assertEqual(self._warrant(decision), gate.STILL_TARGET)

    def test_a_fit_cell_on_another_regime_does_not_rescue_the_row(self):
        """别的阶段那一列写着 `fit`，与这一行无关：它自己那一格没有了 ⇒ `lapsed`。"""
        make_generation(cells=((self.alpha, DOWNTREND, {"state": sl.STATE_FIT}),))
        decision = self.decision(self.alpha, regime=UPTREND)
        self.assertEqual(self._warrant(decision), gate.LAPSED)

    def test_a_strategy_outside_the_managed_set_departs_before_any_cell_is_read(self):
        """归属先于判据：格子还在、还判着不适用，但策略已经不归这套机制管了。"""
        make_generation(cells=((self.alpha, DOWNTREND, {"state": sl.STATE_UNFIT}),))
        decision = self.decision(self.alpha)
        self.assertEqual(self._warrant(decision, managed=frozenset()), gate.GONE)

    def test_a_conflicting_cell_never_earns_a_declaration(self):
        """`needs_review` 的冲突格子归 `lapsed`：没有任何一条路径能把它们判成该停。"""
        make_generation(
            cells=(
                (
                    self.alpha,
                    DOWNTREND,
                    {"state": sl.STATE_UNFIT, "needs_review": True},
                ),
            )
        )
        decision = self.decision(self.alpha)
        self.assertEqual(self._warrant(decision), gate.LAPSED)

        self.judged()
        row = self.live_row(self.alpha)
        summary = self.gate_round()

        self.assertEqual(summary["declarations"], [])
        self.assertEqual(
            summary["close_reasons"], {str(self.alpha.id): gate.CLOSE_BECAME_FIT}
        )
        row.refresh_from_db()
        self.assertEqual(row.closed_reason, gate.CLOSE_BECAME_FIT)

    def test_a_cell_that_left_the_current_generation_is_a_lapse(self):
        """格子取不到是「当前代不再产出这条建议」，不是「策略离场」。"""
        self.judged()
        row = self.live_row(self.alpha)
        self.decision(self.alpha)
        make_generation()  # 一代空表：这一格的结论没了

        summary = self.gate_round()

        self.assertEqual(summary["declarations"], [])
        self.assertEqual(
            summary["close_reasons"], {str(self.alpha.id): gate.CLOSE_BECAME_FIT}
        )
        row.refresh_from_db()
        self.assertEqual(row.closed_reason, gate.CLOSE_BECAME_FIT)

    # --- 三态落成什么 ------------------------------------------------------ #

    def test_a_dead_strategy_is_a_departure_not_a_re_fit(self):
        """`is_active=False` 是人工退役（CONTEXT.md 第 131 条）。判成 `became_fit` 会把
        一次离场说成一次重新适配。"""
        self.judged()
        row = self.live_row(self.alpha)
        self.decision(self.alpha)
        make_generation(cells=((self.alpha, DOWNTREND, {"state": sl.STATE_UNFIT}),))
        # 用 `update` 而不是改内存里那一个：`_warrant` 那一侧走的是 `select_related`
        # 现查出来的行，改内存实例不会影响它。
        self.alpha.__class__.objects.filter(pk=self.alpha.pk).update(is_active=False)

        summary = self.gate_round()

        self.assertEqual(summary["declarations"], [])
        self.assertEqual(
            summary["close_reasons"], {str(self.alpha.id): gate.CLOSE_STRATEGY_GONE}
        )
        row.refresh_from_db()
        self.assertEqual(row.closed_reason, gate.CLOSE_STRATEGY_GONE)

    def test_a_strategy_outside_the_managed_set_is_a_departure_too(self):
        """幽灵策略（实现类解析不到）有格子也判成离场。"""
        self.judged()
        row = self.live_row(self.gamma)
        self.decision(self.gamma)
        make_generation(cells=((self.gamma, DOWNTREND, {"state": sl.STATE_UNFIT}),))

        summary = self.gate_round()

        self.assertEqual(summary["declarations"], [])
        self.assertEqual(summary["unresolved"], [str(self.gamma.id)])
        self.assertEqual(
            summary["close_reasons"], {str(self.gamma.id): gate.CLOSE_STRATEGY_GONE}
        )
        row.refresh_from_db()
        self.assertEqual(row.closed_reason, gate.CLOSE_STRATEGY_GONE)

    def test_an_off_regime_row_is_a_departure_of_the_regime_not_of_the_strategy(self):
        """两档解除原因不串味：策略活得好好的、它自己那一格也还判着不适用，只是阶段
        对不上了——写 `strategy_gone` 会把一次阶段离开说成一次策略离场。"""
        self.judged()
        row = self.live_row(self.alpha)
        self.decision(self.alpha, regime=UPTREND)
        make_generation(cells=((self.alpha, UPTREND, {"state": sl.STATE_UNFIT}),))

        summary = self.gate_round()

        self.assertEqual(
            summary["close_reasons"], {str(self.alpha.id): gate.CLOSE_REGIME_LEFT}
        )
        self.assertEqual(summary["declarations"], [])
        row.refresh_from_db()
        self.assertEqual(row.closed_reason, gate.CLOSE_REGIME_LEFT)

    def test_a_fit_cell_takes_the_declaration_away_and_puts_the_row_back_to_released(
        self,
    ):
        self.judged()
        decision = self.decision(self.alpha, status=DecisionStatus.APPLIED.value)
        make_generation(cells=((self.alpha, DOWNTREND, {"state": sl.STATE_UNFIT}),))
        first = self.gate_round()
        self.assertEqual(first["halt"]["created"], 1)

        make_generation(
            finished_at=days(1),
            cells=((self.alpha, DOWNTREND, {"state": sl.STATE_FIT}),),
        )
        second = self.gate_round(now=days(1))

        self.assertEqual(
            second["close_reasons"], {str(self.alpha.id): gate.CLOSE_BECAME_FIT}
        )
        self.assertEqual(second["halt"]["closed"], 1)
        self.assertEqual(
            second["statuses"],
            [
                {
                    "decision_id": str(decision.pk),
                    "strategy_id": str(self.alpha.id),
                    "from": DecisionStatus.APPLIED.value,
                    "to": DecisionStatus.RELEASED.value,
                    "written": True,
                }
            ],
        )
        self.assertEqual(
            DeactivationDecision.objects.get().status, DecisionStatus.RELEASED.value
        )


# --------------------------------------------------------------------------- #
# 性质 7：窗口起点
# --------------------------------------------------------------------------- #


class TestTheWindowStart(_Fixture):
    """`attitude_since` 的四个候选取最晚；豁免那一条尤其有两副面孔。"""

    def setUp(self):
        super().setUp()
        self.switch()  # 开启于 days(-2)

    def _declare(self, *, exemption=None):
        """造一个「该声明」的世界，返回台账那一行。"""
        if exemption is not None:
            self.exemption(self.alpha, **exemption)
        self.judged(effective_at=days(-3))
        self.decision(self.alpha)
        make_generation(
            finished_at=days(-5),
            cells=((self.alpha, DOWNTREND, {"state": sl.STATE_UNFIT}),),
        )
        self.gate_round()
        return HaltDeclaration.objects.get()

    def test_the_open_moment_is_the_floor(self):
        """四条事实里开关最晚开启 ⇒ 起点就是它。机制在那之前没有能力拦。"""
        row = self._declare()
        self.assertEqual(row.opened_at, days(-2))

    def test_an_early_revoked_exemption_ends_the_window_when_it_was_revoked(self):
        """Q5：提前收回的豁免，窗口结束于**收回那一刻**，不是原定的到期时刻。取错的话
        声明行会声称机制在一段它并没有拦的时间里拦着。"""
        row = self._declare(
            exemption={
                "granted_at": days(-10),
                "expires_at": days(50),
                "closed_at": days(-1),
                "closed_reason": dru.CLOSE_REASON_MANUAL,
            }
        )
        self.assertEqual(row.opened_at, days(-1))

    def test_an_exemption_that_ran_out_ends_the_window_at_its_expiry(self):
        row = self._declare(exemption={"granted_at": days(-10), "expires_at": days(-1)})
        self.assertEqual(row.opened_at, days(-1))

    def test_an_exemption_for_another_regime_does_not_move_the_window(self):
        """「当前阶段」是这一层唯一关心的口径：别的阶段上的豁免与本轮无关。"""
        row = self._declare(
            exemption={
                "regime": UPTREND,
                "granted_at": days(-10),
                "expires_at": days(50),
                "closed_at": days(-1),
            }
        )
        self.assertEqual(row.opened_at, days(-2))

    def test_an_exemption_still_in_force_takes_the_declaration_away(self):
        """在期的豁免不声明，活行按 `exempted` 解除——那是「人按住了它」，比「判据变了」
        更靠前也更可执行。"""
        self.exemption(self.alpha, granted_at=days(-10), expires_at=days(10))
        self.judged(effective_at=days(-3))
        row = self.live_row(self.alpha)
        decision = self.decision(self.alpha, status=DecisionStatus.APPLIED.value)
        make_generation(
            finished_at=days(-5),
            cells=((self.alpha, DOWNTREND, {"state": sl.STATE_UNFIT}),),
        )

        summary = self.gate_round()

        self.assertEqual(summary["declarations"], [])
        self.assertEqual(
            summary["close_reasons"], {str(self.alpha.id): gate.CLOSE_EXEMPTED}
        )
        # 该停的是一个、被豁免的也是一个：`targets` 与 `exempt` 是同一批行上的两层读数，
        # 不是两个不相交的集合。「目标里被豁免的」恒为 0 会让一个被豁免的目标在日报上
        # 看起来像「本轮没有该停的」。
        self.assertEqual(summary["targets"], 1)
        self.assertEqual(summary["exempt"], 1)
        row.refresh_from_db()
        self.assertEqual(row.closed_reason, gate.CLOSE_EXEMPTED)
        # 被豁免的格子照样要回写：机制确实不必再声明它了。
        self.assertEqual(
            DeactivationDecision.objects.get(pk=decision.pk).status,
            DecisionStatus.RELEASED.value,
        )

    def test_the_switch_moment_survives_a_second_round_unchanged(self):
        """起点取自事实，所以第二轮起点不动、行也不动——一轮换个起点的表现是每轮都
        重新通知一次。"""
        self._declare()
        second = self.gate_round()
        self.assertEqual(second["halt"]["unchanged"], 1)
        self.assertEqual(HaltDeclaration.objects.get().opened_at, days(-2))


# --------------------------------------------------------------------------- #
# 性质 8：status 回写
# --------------------------------------------------------------------------- #


class TestTheStatusWrite(_Fixture):
    def setUp(self):
        super().setUp()
        self.switch()
        self.judged()

    def _applied_world(self, **cell):
        decision = self.decision(
            self.alpha,
            status=DecisionStatus.APPLIED.value,
            evidence={"cell": {"trades": 3}, "frozen": True},
        )
        make_generation(
            cells=((self.alpha, DOWNTREND, dict({"state": sl.STATE_UNFIT}, **cell)),)
        )
        return decision

    def test_the_evidence_is_never_touched(self):
        """决策冻的是它自己那一刻的依据。status 写了两次，`evidence` 一个字都不动。"""
        decision = self._applied_world()
        before = DeactivationDecision.objects.get(pk=decision.pk).evidence

        self.gate_round()
        make_generation(
            finished_at=days(1),
            cells=((self.alpha, DOWNTREND, {"state": sl.STATE_FIT}),),
        )
        self.gate_round(now=days(1))

        after = DeactivationDecision.objects.get(pk=decision.pk)
        self.assertEqual(after.status, DecisionStatus.RELEASED.value)
        self.assertEqual(after.evidence, before)
        self.assertEqual(after.evidence, {"cell": {"trades": 3}, "frozen": True})

    def test_a_row_nobody_claimed_is_not_recorded_as_released(self):
        """`suggested` 是只有 `deactivation_run` 写过的值 = 机制从没碰过它。给它写
        `released` 等于往表里记一条「它当时在拦」的假历史（Q3 的同一条理由）。"""
        decision = self.decision(self.alpha)  # suggested
        make_generation(cells=((self.alpha, DOWNTREND, {"state": sl.STATE_FIT}),))

        summary = self.gate_round()

        self.assertEqual(summary["statuses"], [])
        self.assertEqual(
            DeactivationDecision.objects.get(pk=decision.pk).status,
            DecisionStatus.SUGGESTED.value,
        )
        # 不写 status 不等于不解除活行：两件事。
        self.assertEqual(
            summary["close_reasons"], {str(self.alpha.id): gate.CLOSE_BECAME_FIT}
        )

    def test_a_row_that_moved_between_the_read_and_the_write_is_left_alone(self):
        """条件更新就是那把锁：比对旧值不匹配时**什么都不做**（保留别人的写），而不是拿
        一份过期的判断覆盖它。比不中时要说得出为什么。"""
        decision = self._applied_world()
        real = grun._refs

        def stale(*, managed, cells):
            return tuple(
                replace(ref, status=gate.STATUS_RELEASED)
                if ref.strategy_id == self.alpha.id
                else ref
                for ref in real(managed=managed, cells=cells)
            )

        with patch.object(grun, "_refs", side_effect=stale):
            with self.assertLogs("apps.regime.gate_run", level="WARNING") as caught:
                summary = self.gate_round()

        self.assertEqual(
            summary["statuses"],
            [
                {
                    "decision_id": str(decision.pk),
                    "strategy_id": str(self.alpha.id),
                    "from": gate.STATUS_RELEASED,
                    "to": gate.STATUS_APPLIED,
                    "written": False,
                }
            ],
        )
        self.assertTrue(any("被改过" in line for line in caught.output))
        # 库里的值原样保留（写之前是 `applied`，本轮的目标也是 `applied`，但手里那份
        # 判断说旧值是 `released` ⇒ 一次都不该改）。
        self.assertEqual(
            DeactivationDecision.objects.get(pk=decision.pk).status,
            DecisionStatus.APPLIED.value,
        )

    def test_a_released_row_is_flipped_back_when_it_is_a_target_again(self):
        """Q9：翻面是**双向**的，且只写 `status`。

        `released` 的行重新成为目标（阶段回来 / 判据又成立）时要回到 `applied`——否则
        「撤回过的不能再声明」会让那一格永久哑掉。写的是同一列，行本身、`evidence`、
        `first_decided_at` 一个字都不动，也没有任何一行被删。
        """
        decision = self.decision(
            self.alpha,
            status=DecisionStatus.RELEASED.value,
            evidence={"cell": {"trades": 3}, "frozen": True},
        )
        make_generation(cells=((self.alpha, DOWNTREND, {"state": sl.STATE_UNFIT}),))

        summary = self.gate_round()

        self.assertEqual(summary["halt"]["created"], 1)
        self.assertEqual(
            summary["statuses"],
            [
                {
                    "decision_id": str(decision.pk),
                    "strategy_id": str(self.alpha.id),
                    "from": DecisionStatus.RELEASED.value,
                    "to": DecisionStatus.APPLIED.value,
                    "written": True,
                }
            ],
        )
        self.assertEqual(DeactivationDecision.objects.count(), 1)
        row = DeactivationDecision.objects.get(pk=decision.pk)
        self.assertEqual(row.status, DecisionStatus.APPLIED.value)
        self.assertEqual(row.evidence, {"cell": {"trades": 3}, "frozen": True})

    def test_the_write_is_matched_on_the_row_not_on_the_pair(self):
        """同一个策略的两行（不同阶段）各写各的目标：按 `strategy_id` 一把写的实现会把
        两行都写成同一个值。"""
        first = self.decision(self.alpha, regime=DOWNTREND)
        # 第二行是**被机制领过**的（`applied`）：只有这样它才既进得了回写集合
        # （`_untouched` 会把「没人认领的建议」挡在外面），又能与本轮的目标值不同。
        second = self.decision(
            self.alpha, regime=UPTREND, status=DecisionStatus.APPLIED.value
        )
        # 判定不另建：`setUp` 已经落了一条 `downtrend` 的（`uniq_regime_judgement_symbol_effective_at`
        # 钉着「同一品种同一生效时刻只有一条」）。
        make_generation(
            cells=(
                (self.alpha, DOWNTREND, {"state": sl.STATE_UNFIT}),
                (self.alpha, UPTREND, {"state": sl.STATE_UNFIT}),
            )
        )

        summary = self.gate_round()

        written = {row["decision_id"] for row in summary["statuses"]}
        self.assertEqual(written, {str(first.pk), str(second.pk)})
        self.assertEqual(summary["halt"]["created"], 1)  # 只有当前阶段那一条声明
        self.assertEqual(
            DeactivationDecision.objects.get(pk=first.pk).status,
            DecisionStatus.APPLIED.value,
        )
        self.assertEqual(
            DeactivationDecision.objects.get(pk=second.pk).status,
            DecisionStatus.RELEASED.value,
        )


# --------------------------------------------------------------------------- #
# 性质 9：一轮只碰自己那一档
# --------------------------------------------------------------------------- #


class TestTheScopeOfATurn(_Fixture):
    def setUp(self):
        super().setUp()
        self.switch()
        self.judged()
        make_generation(cells=((self.alpha, DOWNTREND, {"state": sl.STATE_UNFIT}),))

    def test_the_event_and_blanket_rows_are_left_alone(self):
        for trigger in (HaltTrigger.EVENT, HaltTrigger.BLANKET):
            HaltDeclaration.objects.create(
                trigger=trigger.value,
                scope=halt.global_scope(),
                label=f"{trigger.display}",
                opened_at=days(-1),
                reason="别的档写的",
                actor_kind=ActorKind.TASK.value,
                actor_name=halt_sync.ACTOR_NAME,
            )
        self.decision(self.alpha)

        self.gate_round()

        others = HaltDeclaration.objects.exclude(trigger=HaltTrigger.DEACTIVATION.value)
        self.assertEqual(others.count(), 2)
        for row in others:
            self.assertIsNone(row.closed_at)

    def test_a_live_deactivation_row_without_a_decision_row_is_left_blocking(self):
        """一行在册之外（没有任何决策行）的活声明，既不在期望集里也不在解除名单里——
        写入方按 fail-closed 不动它。「查不到就当成没在拦」会把一条别人的声明解除掉，
        而一条解除不掉的声明看起来就像「本来就没声明」。"""
        row = self.live_row(self.beta)
        self.decision(self.alpha)

        with self.assertLogs("apps.regime.halt_sync", level="WARNING") as caught:
            summary = self.gate_round()

        row.refresh_from_db()
        self.assertIsNone(row.closed_at)
        self.assertNotIn(str(self.beta.id), summary["close_reasons"])
        self.assertTrue(
            any(halt.strategy_scope(self.beta.id) in line for line in caught.output)
        )

    def test_a_row_written_by_another_actor_is_reconciled_not_abandoned(self):
        """别人开的策略档活行（`actor_name` 不是本层）同样进对账：要不要继续拦的判据是
        期望集，不是「谁写的这一行」。只有「不在期望集里、也不在解除名单里」才落到
        fail-closed（上一条用例）。"""
        row = self.live_row(self.alpha, actor_name="regime.sync_halt_windows")
        self.decision(self.alpha)

        summary = self.gate_round()

        # 期望集里有它 ⇒ 本轮会按自己的期望改它（起点、文案），但绝不解除。
        self.assertEqual(summary["halt"]["closed"], 0)
        self.assertEqual(summary["halt"]["updated"], 1)
        row.refresh_from_db()
        self.assertIsNone(row.closed_at)


# --------------------------------------------------------------------------- #
# 性质 2：摘要形状
# --------------------------------------------------------------------------- #


class TestTheSummaryShape(_Fixture):
    """键集恒定，且能直接进 Celery 结果与日报。"""

    def summaries(self) -> dict[str, dict]:
        """五条路径各来一份：三种「什么都不动」、Shadow、正常一轮。"""
        make_generation(cells=((self.alpha, DOWNTREND, {"state": sl.STATE_UNFIT}),))
        cold = self.gate_round()

        self.judged(effective_at=days(-4))
        stale = self.gate_round()

        self.judged()
        self.decision(self.alpha)
        shadow = self.gate_round()

        self.switch()
        live = self.gate_round()

        RegimePoolRebuild.objects.all().delete()
        no_generation = self.gate_round()
        return {
            "cold": cold,
            "stale": stale,
            "shadow": shadow,
            "live": live,
            "no_generation": no_generation,
        }

    def test_the_key_set_is_constant_across_paths(self):
        summaries = self.summaries()
        keys = set(summaries["live"])
        for name, summary in summaries.items():
            with self.subTest(path=name):
                self.assertEqual(set(summary), keys)

    def test_every_summary_is_json_serialisable(self):
        """它进 Celery 结果（JSON 序列化）。id 一律是字符串。"""
        for name, summary in self.summaries().items():
            with self.subTest(path=name):
                json.dumps(summary)

    def test_every_skipped_path_carries_a_human_readable_note(self):
        """`skipped` 只说得出「没动」，说不出「为什么」——那句为什么在 `note` 里。

        `note` 不是「跳过」的专属：Shadow 那一档没被跳过（它照常对账、照常解除活行），
        但它同样是「本轮没有声明」的一种收场，也带话（`gate.NOTE_SHADOW`）——只说
        `gate_open=False` 的话，读的人还得自己推出「那现有的活行呢」。只有正常一轮
        是空的：那一轮的产出全在 `declarations` 里。
        """
        summaries = self.summaries()
        for name, summary in summaries.items():
            with self.subTest(path=name):
                if summary["skipped"] is not None:
                    self.assertTrue(summary["note"])
        self.assertEqual(summaries["shadow"]["note"], gate.NOTE_SHADOW)
        self.assertEqual(summaries["live"]["note"], "")

    def test_the_paths_are_distinguishable(self):
        summaries = self.summaries()
        self.assertEqual(
            {name: s["skipped"] for name, s in summaries.items()},
            {
                "cold": dea.BLOCKED_COLD_START,
                "stale": dea.BLOCKED_STALE_STATE,
                "shadow": None,
                "live": None,
                "no_generation": dru.SKIPPED_NO_GENERATION,
            },
        )
        # Shadow 与正常一轮靠开关与产出分开——两者的 `skipped` 都是 `None`。
        self.assertFalse(summaries["shadow"]["gate_open"])
        self.assertTrue(summaries["live"]["gate_open"])
        self.assertEqual(summaries["shadow"]["declarations"], [])
        self.assertEqual(len(summaries["live"]["declarations"]), 1)
        self.assertEqual(
            summaries["shadow"]["close_reasons"],
            {str(self.alpha.id): gate.CLOSE_GATE_CLOSED},
        )
