"""幽灵策略清理命令（第①段单元 7 收尾）。

`cleanup_ghost_strategies` 的判据是三件叠在一起的：**注册表解析不到实现类**（幽灵）**且
零反向引用**（可删）/ 有引用（只退役）。它做的是不可逆的删除，所以这里钉的不是「删得对
不对」这种运行一次就知道的事，而是**它在什么情况下会安静地删错**。

七条性质，每一条坏了都不报警、只出错的删：

1. **零引用才删**。CONTEXT.md 第 105 条那两句（「从未被引用 → 删」「有引用 → 保留并
   停用」）读起来是两个判据，实际落成的是「**零引用**才删」——比并集更保守一档。方向
   是刻意选的：多留一行看得见，多删一行看不见。所以「有引用」那一条**不是**「有会话
   引用」，而是任何一张表指着它。
2. **引用清单是反射出来的，不是手抄的**。`Strategy._meta.related_objects` 上一共七条
   反向关系，其中四条是 `CASCADE`——手抄一张「LiveSession + BacktestResult」的清单
   （CONTEXT.md 的散文就是这么写的）会在写下它的当天漏掉 `GridSearchJob` 与
   `RegimePoolCell`，而漏掉的代价是**静默级联删除**：回测数据是这个仓库里更贵的资产。
   本文件因此对**每一条**反向关系各有一条用例，再加一条钉住「工厂表与反射逐字相符」——
   将来新增一张指向 `Strategy` 的表，那条用例会红，逼着人回答「这张表算不算引用」。
3. **只做 `True → False` 这一个方向**。`is_active` 的语义里写着「永不被自动恢复」
   （第 106 条），所以清理**永不**置回 `True`；退役过的行再跑一次仍然是 `False`。
4. **默认一行不写**。dry-run 打印两份清单但不落笔，「待删」的那批连行都还在。
5. **注册表为空时 `--apply` 必须被挡住**。那个目录不在（容器里很常见）时**每一行**都
   会被判成幽灵，`--apply` 会把全库删掉。dry-run 照打（那是你要看的东西），`--apply`
   以 `CommandError` 收场——而判据取**注册表自身**、不取「本库一条都没解析到」：后者在
   「库里的行恰好全是幽灵、注册表其实好好的」时同样成立，那是一个合法且该被执行的清单。
6. **能解析到实现类的策略不参与**：既不在待删里，也不在待退役里，`is_active` 一动不动。
7. **删除行数对不上要吼**。零引用时 `delete()` 的影响行数应当等于待删条数；不等就是有
   级联发生，意味着反射漏了东西——这个警告是最后一道闸。

夹具一律用真 `Strategy` 行（`Strategy.id` 是 UUID 主键，拿整数当主键写死会在真库上静默
错位）。注册表用替身，**不真跑 `discover()`**：它是进程级副作用、会把宿主机那份策略目录
拖进来，让用例的结论依赖运行环境。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from apps.regime import pool_rebuild
from apps.regime.management.commands import cleanup_ghost_strategies as cleanup
from apps.regime.models import (
    DeactivationDecision,
    DeactivationExemption,
    RegimePoolCell,
    RegimePoolRebuild,
)
from apps.trading.models import LiveSession, Strategy

#: 解析得到实现类的那个策略名。注册表替身只装它一个。
REAL = "RealStem"

NOW = datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)


def probe_class(stem: str) -> type:
    """够用的策略替身：注册表只要四字段 `description`（`validate_description`）。"""
    return type(
        f"Probe_{stem}",
        (),
        {
            "name": stem,
            "description": (
                "策略类型：趋势跟踪\n核心指标：基线\n适用场景：基线\n入场逻辑：基线\n"
            ),
        },
    )


# --------------------------------------------------------------------------- #
# 每条反向关系各一个工厂
# --------------------------------------------------------------------------- #

#: `(app_label, model_name) → 造一行指向该策略的记录`。键用的是 `related_objects` 上的
#: `(model._meta.app_label, model._meta.model_name)`，所以「工厂表与反射逐字相符」那条
#: 用例可以直接比对两个集合。
FACTORIES: dict[tuple[str, str], object] = {}


def _factory(model_key):
    def wrapper(func):
        FACTORIES[model_key] = func
        return func

    return wrapper


@_factory(("trading", "livesession"))
def _make_session(user, strategy):
    return LiveSession.objects.create(
        user=user,
        strategy=strategy,
        symbol="BTC/USDT",
        mode="paper",
        status="running",
        initial_capital=Decimal("10000.00"),
    )


@_factory(("backtest", "backtestresult"))
def _make_backtest_result(user, strategy):
    from apps.backtest.models import BacktestResult

    return BacktestResult.objects.create(
        strategy=strategy,
        symbol="BTC/USDT",
        timeframe="1d",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 3, 1),
        initial_capital=Decimal("10000.00"),
        final_capital=Decimal("10500.00"),
        total_return_pct=5.0,
        metrics={},
    )


@_factory(("backtest", "gridsearchjob"))
def _make_grid_search_job(user, strategy):
    from apps.backtest.models import GridSearchJob

    return GridSearchJob.objects.create(
        strategy=strategy,
        symbol="BTC/USDT",
        timeframe="1d",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 3, 1),
        initial_capital=Decimal("10000.00"),
    )


@_factory(("regime", "regimepoolcell"))
def _make_pool_cell(user, strategy):
    """池化格不是直接指向策略的——它挂在世代上。工厂仍然造一行引用，因为要问的是
    「删这条策略会不会带走格子」。"""
    generation = RegimePoolRebuild.objects.create(
        status="ready",
        actor_kind="task",
        actor_name="测试",
        pool_version=1,
        input_fingerprint=f"ghost-{uuid.uuid4().hex[:8]}",
        started_at=NOW,
    )
    return RegimePoolCell.objects.create(
        rebuild=generation,
        strategy=strategy,
        regime="downtrend",
        source="strategy",
        state="fit",
        reason="",
        evidence={},
    )


@_factory(("regime", "deactivationdecision"))
def _make_decision(user, strategy):
    return DeactivationDecision.objects.create(
        strategy=strategy,
        regime="downtrend",
        status="suggested",
        evidence={},
        first_decided_at=NOW,
        last_confirmed_at=NOW,
    )


@_factory(("regime", "deactivationexemption"))
def _make_exemption(user, strategy):
    return DeactivationExemption.objects.create(
        strategy=strategy,
        regime="downtrend",
        granted_at=NOW,
        expires_at=NOW + timedelta(days=10),
        granted_by="测试",
    )


@_factory(("regime", "archetypeoverride"))
def _make_override(user, strategy):
    from apps.regime.models import ArchetypeOverride

    return ArchetypeOverride.objects.create(
        strategy=strategy,
        archetype="趋势跟踪",
        created_by="测试",
    )


# --------------------------------------------------------------------------- #
# 共用夹具
# --------------------------------------------------------------------------- #


class _Fixture(TestCase):
    """三个策略：`REAL` 解析得到实现类，`GHOST_*` 两条解析不到（真幽灵）。"""

    def setUp(self):
        from apps.backtest.models import BacktestResult
        from apps.strategy_engine.registry import StrategyRegistry

        # 幽灵清理按「有没有反面引用」判定，而读的是全表（`pool_rebuild._result_rows()`
        # 不带过滤——生产里它就该读全部回测结果）。库里有既存回测结果时，本用例造的
        # 幽灵就不再是「零引用」。跑在 `db_dev.sqlite3` 上时库里有真实回测结果，
        # 先清干净；`TestCase` 的事务会连这一步一起回滚，真实库毫发无损。
        BacktestResult.objects.all().delete()

        self.discover = self.enterContext(
            patch.object(pool_rebuild, "ensure_strategies_discovered", return_value=[])
        )
        StrategyRegistry.register(probe_class(REAL), name=REAL)
        self.addCleanup(StrategyRegistry._strategies.pop, REAL, None)

        self.real = Strategy.objects.create(name=REAL, code_path="/t/real.py")
        self.ghost_doomed = Strategy.objects.create(
            name="GhostDoomedStem", code_path="/t/g1.py"
        )
        self.ghost_kept = Strategy.objects.create(
            name="GhostKeptStem", code_path="/t/g2.py"
        )
        # `AUTH_USER_MODEL = "authentication.User"`：email 是必填的第一个位置参数。
        self.user = get_user_model().objects.create_user(
            email="cleanup@test.local", username="cleanup", password="pw12345"
        )

    # --- 帮手 -------------------------------------------------------------- #

    def reference(self, model_key: str, strategy):
        """按工厂造一行引用。`model_key` 用 `(app_label, model_name)`。"""
        return FACTORIES[model_key](self.user, strategy)

    def cleanup(self, *args, expect_error: bool = False) -> tuple[str, str]:
        """跑命令，返回 `(stdout, stderr)`。

        **不叫 `run`**：`unittest.TestCase.run` 是框架自己的入口，覆盖掉它会让每条用例
        在框架调用 `self.run(result)` 时炸在参数个数上。
        """
        out, err = StringIO(), StringIO()
        kwargs = {"stdout": out, "stderr": err}
        if expect_error:
            with self.assertRaises(CommandError):
                call_command("cleanup_ghost_strategies", *args, **kwargs)
        else:
            call_command("cleanup_ghost_strategies", *args, **kwargs)
        return out.getvalue(), err.getvalue()

    def ghost_ids(self, *names: str) -> set:
        return set(Strategy.objects.filter(name__in=names).values_list("id", flat=True))


# --------------------------------------------------------------------------- #
# 性质 2：引用清单是反射出来的
# --------------------------------------------------------------------------- #


class TestReferenceReflection(_Fixture):
    """每一条反向关系都被算进引用数——包括那四条会静默级联的 `CASCADE`。"""

    def test_factory_table_matches_reflection(self):
        """工厂表与 `Strategy._meta.related_objects` **逐字相符**。

        这条用例是给未来的人看的：新增一张指向 `Strategy` 的表，它会红，而红的意思是
        「请回答这张表算不算引用」。它红了不代表命令坏了——命令走的是反射，会自动把新表
        算进来；红的是**这份用例的覆盖面**。
        """
        reflected = {
            (
                relation.related_model._meta.app_label,
                relation.related_model._meta.model_name,
            )
            for relation in Strategy._meta.related_objects
        }
        self.assertEqual(reflected, set(FACTORIES))

    def test_every_relation_counts_as_reference(self):
        """一张表一行引用 → 引用数 1。**逐条参数化**，漏掉哪条就红在哪条上。"""
        for model_key in sorted(FACTORIES):
            with self.subTest(model=model_key):
                strategy = Strategy.objects.create(
                    name=f"RefStem_{model_key[0]}_{model_key[1]}",
                    code_path="/t/ref.py",
                )
                counts = cleanup.reference_counts([strategy.id])
                self.assertEqual(counts[strategy.id], 0, "还没造引用，应当是 0")
                self.reference(model_key, strategy)
                counts = cleanup.reference_counts([strategy.id])
                self.assertEqual(counts[strategy.id], 1)

    def test_counts_sum_across_relations(self):
        """跨表求和：一条策略同时被会话与回测引用 → 2，不是「有/没有」两档。"""
        self.reference(("trading", "livesession"), self.ghost_kept)
        self.reference(("backtest", "backtestresult"), self.ghost_kept)
        counts = cleanup.reference_counts([self.ghost_kept.id])
        self.assertEqual(counts[self.ghost_kept.id], 2)

    def test_counts_are_zero_for_untouched_strategy(self):
        self.assertEqual(
            cleanup.reference_counts([self.ghost_doomed.id])[self.ghost_doomed.id], 0
        )

    def test_empty_input_returns_empty(self):
        self.assertEqual(cleanup.reference_counts([]), {})


# --------------------------------------------------------------------------- #
# 性质 1：零引用才删
# --------------------------------------------------------------------------- #


class TestClassify(_Fixture):
    def test_zero_reference_goes_to_doomed(self):
        doomed, retiring = cleanup.classify([(self.ghost_doomed.id, "GhostDoomedStem")])
        self.assertEqual([row[0] for row in doomed], [self.ghost_doomed.id])
        self.assertEqual(retiring, [])
        self.assertEqual(doomed[0][2], 0, "引用数要一并带出来给人看")

    def test_referenced_goes_to_retiring(self):
        """「有引用」不是「有会话引用」——回测引用同样只退役不删。"""
        self.reference(("backtest", "backtestresult"), self.ghost_kept)
        doomed, retiring = cleanup.classify([(self.ghost_kept.id, "GhostKeptStem")])
        self.assertEqual(doomed, [])
        self.assertEqual([row[0] for row in retiring], [self.ghost_kept.id])
        self.assertEqual(retiring[0][2], 1)

    def test_input_order_is_preserved(self):
        """两份清单要给人看：顺序不稳会让两次 dry-run 的 diff 多出一堆假差异。"""
        # 让第一条有引用（进待退役），第二条零引用（进待删）——顺序才有得比。
        self.reference(("trading", "livesession"), self.ghost_kept)
        rows = [
            (self.ghost_kept.id, "GhostKeptStem"),
            (self.ghost_doomed.id, "GhostDoomedStem"),
        ]
        doomed, retiring = cleanup.classify(rows)
        self.assertEqual([row[1] for row in doomed], ["GhostDoomedStem"])
        self.assertEqual([row[1] for row in retiring], ["GhostKeptStem"])


# --------------------------------------------------------------------------- #
# 性质 4 + 3：dry-run 一行不写；只做 True → False
# --------------------------------------------------------------------------- #


class TestDryRun(_Fixture):
    def test_dry_run_writes_nothing(self):
        self.reference(("trading", "livesession"), self.ghost_kept)
        before = Strategy.objects.count()
        out, _ = self.cleanup()  # 不加 --apply
        self.assertEqual(Strategy.objects.count(), before, "dry-run 不该删行")
        self.assertTrue(Strategy.objects.filter(id=self.ghost_doomed.id).exists())
        self.assertIn("dry-run", out)
        self.assertIn("--apply", out, "要告诉人怎么真的执行")

    def test_dry_run_prints_both_lists(self):
        self.reference(("trading", "livesession"), self.ghost_kept)
        out, _ = self.cleanup()
        self.assertIn("幽灵）2 条", out)
        self.assertIn("零引用待删 1", out)
        self.assertIn("有引用待退役 1", out)
        self.assertIn("GhostDoomedStem", out)
        self.assertIn("GhostKeptStem", out)
        self.assertIn("引用 1 行", out)

    def test_dry_run_does_not_touch_is_active(self):
        self.real.is_active = True
        self.real.save(update_fields=["is_active"])
        self.cleanup()
        self.real.refresh_from_db()
        self.assertTrue(self.real.is_active)


class TestApply(_Fixture):
    def test_apply_deletes_zero_reference_and_retires_referenced(self):
        self.reference(("trading", "livesession"), self.ghost_kept)
        out, _ = self.cleanup("--apply")

        self.assertFalse(Strategy.objects.filter(id=self.ghost_doomed.id).exists())
        self.ghost_kept.refresh_from_db()
        self.assertFalse(self.ghost_kept.is_active, "有引用的只退役，不删")
        self.assertIn("已删除 1 条", out)
        self.assertIn("已退役 1 条", out)

    def test_apply_never_restores(self):
        """性质 3：`True → False` 是单向的。已是 `False` 的行不会被置回 `True`。

        这条看着平淡，但它是「永不被自动恢复」（第 106 条）在命令里的落点：哪天有人在清理
        里顺手把「看起来还活着」的策略激活，那是在替人做决定。
        """
        self.ghost_kept.is_active = False
        self.ghost_kept.save(update_fields=["is_active"])
        self.reference(("trading", "livesession"), self.ghost_kept)

        self.cleanup("--apply")

        self.ghost_kept.refresh_from_db()
        self.assertFalse(self.ghost_kept.is_active)
        self.assertFalse(
            Strategy.objects.filter(is_active=True).exists(), "一行都不该被激活"
        )

    def test_apply_leaves_resolvable_strategy_alone(self):
        """性质 6：解析得到实现类的策略不参与——不删、不停用。"""
        self.real.is_active = True
        self.real.save(update_fields=["is_active"])

        self.cleanup("--apply")

        self.real.refresh_from_db()
        self.assertTrue(self.real.is_active)

    def test_apply_carries_the_cascade_warning(self):
        """性质 7：删除行数对不上就吼。

        这里**故意让 `reference_counts` 说谎**（一律返回零），模拟「新增了一张指向
        `Strategy` 的表而反射没算到」——命令把有引用的那条也当成零引用去删，`delete()`
        于是级联带走了那张表里的行。警告是最后一道闸，它必须响。

        用的是 `BacktestResult` 而**不是** `LiveSession`：后者是 `PROTECT`，会抛
        `ProtectedError` 而不是静默级联——拿它来演这场戏根本演不成。
        """
        from apps.backtest.models import BacktestResult

        self.reference(("backtest", "backtestresult"), self.ghost_doomed)
        before_results = BacktestResult.objects.count()

        with patch.object(
            cleanup, "reference_counts", side_effect=lambda ids: {i: 0 for i in ids}
        ):
            _, err = self.cleanup("--apply")

        self.assertLess(
            BacktestResult.objects.count(),
            before_results,
            "级联真的发生了才叫这条用例有效",
        )
        self.assertIn("有级联删除发生", err)


# --------------------------------------------------------------------------- #
# 性质 5：注册表为空时挡住 --apply
# --------------------------------------------------------------------------- #


class TestRegistryGuard(_Fixture):
    def _empty_registry(self):
        from apps.strategy_engine.registry import StrategyRegistry

        return patch.object(StrategyRegistry, "list_registered", return_value=[])

    def test_empty_registry_apply_is_refused(self):
        """注册表没加载起来时 `--apply` 会把全库删掉——必须挡住，且一行不写。"""
        before = set(Strategy.objects.values_list("id", flat=True))
        with self._empty_registry():
            out, err = self.cleanup("--apply", expect_error=True)

        self.assertEqual(set(Strategy.objects.values_list("id", flat=True)), before)
        self.assertIn("注册表", err)

    def test_empty_registry_still_prints_dry_run_lists(self):
        """dry-run 照打——清单正是要看的东西，挡住执行不等于不让人看。"""
        with self._empty_registry():
            out, err = self.cleanup()

        self.assertIn("GhostDoomedStem", out)
        self.assertIn("GhostKeptStem", out)
        self.assertIn("一个实现类都没解析到", err)

    def test_all_ghosts_is_a_legitimate_executable_verdict(self):
        """判据取**注册表自身**，不取「本库一条都没解析到」。

        库里的行恰好全是幽灵、而注册表其实好好的（装着别的实现类）——那是一个合法且该被
        执行的清单。若守卫写成「本库一条都没解析到就拒绝」，这里就会误伤。
        """
        # 只留幽灵：把 `REAL` 那行删掉（它零引用，删得掉），此时本库一条都解析不到。
        self.real.delete()

        out, _ = self.cleanup("--apply")

        self.assertFalse(Strategy.objects.filter(id=self.ghost_doomed.id).exists())
        self.assertFalse(Strategy.objects.filter(id=self.ghost_kept.id).exists())
        self.assertIn("已删除 2 条", out)

    def test_guard_reads_registry_not_local_outcome(self):
        """反向：注册表能解析到实现类时，同一份「本库全是幽灵」的清单**可以**执行。

        与上一条是同一件事的两面，分开写是因为它们会因不同的改动而红：上一条红在「守卫
        取错了判据」，这一条红在「守卫收得太紧」。
        """
        Strategy.objects.filter(id=self.real.id).delete()
        self.assertEqual(
            Strategy.objects.filter(name=REAL).count(), 0, "本库确实一条都解析不到"
        )

        from apps.strategy_engine.registry import StrategyRegistry

        self.assertTrue(
            StrategyRegistry.list_registered(), "而注册表非空——这两种状态必须能分开"
        )


# --------------------------------------------------------------------------- #
# 没事可做的两种收场
# --------------------------------------------------------------------------- #


class TestNothingToDo(_Fixture):
    def test_no_rows_at_all(self):
        Strategy.objects.all().delete()
        out, _ = self.cleanup()
        self.assertIn("没有任何策略行", out)

    def test_all_resolvable(self):
        self.ghost_doomed.delete()
        self.ghost_kept.delete()
        out, _ = self.cleanup()
        self.assertIn("没有幽灵行", out)
