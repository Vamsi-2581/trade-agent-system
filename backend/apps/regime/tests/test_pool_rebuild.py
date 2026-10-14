"""池化表重建（第①段单元 7i）。

`test_pool.py` 钉的是池化的**判定**（合并、门槛、冲突、原型兜底）；这个文件钉的是把
那个纯函数接进库的那一层，也就是 `pool_rebuild.py` 的四件事：**取样本 → 认指纹 →
落一代 → 报差异**。判定逻辑在这里一行都不断言——那会是第二处真相。

七条性质，每一条坏了都不报警、只出错的数：

1. **换代是原子替换**：跑到一半崩溃留下的是永远 `building`/`failed` 的一代，读者
   （`current_cells()`）在那之前之后看到的都是上一代，而不是「一张新旧混合的表」。
   失败的一代**不删格子**：残骸与原因一起才回答得了「为什么会这样」。
2. **指纹相同就复用，不落新代**——代价是这一轮没有记录，所以复用那一次必须把
   「复用第 N 代」说出来（返回值 + stdout），不能静默。
3. **指纹盖的是本次实际生效的门槛**，不是 `GROUPS["evidence"]` 那份全局值：
   `build_pool` 用 `params`，指纹就必须跟着 `params` 走，否则两次不同门槛的重算会
   算出同一个指纹，第二次把第一次那一代当成「输入没变」直接复用。
4. **差异数比的是结论三元组** `(状态, 原因, 来源)`，不比证据里的数字——证据每天都在
   动（多一笔成交、窗口多一天），拿它当差异会让这个数恒等于格子总数，而它的全部
   用处就是「这次重算到底改变了什么」。
5. **排除按切片数、按原因分开计**，且键集恒定：「本金为 0」要人去查回测参数，
   「没切过片」只要跑一次重算入口——混成一个数就没法决定该做哪个。
6. **并发触发的第二方撞唯一约束是约束在起作用**，不是故障：记成一条带原因的
   `failed` 行并如实返回，而不是把整条命令炸掉；真故障则留痕后上抛。
7. **重算不回头读 `BacktestTrade`**：载荷是自足的（`slice_backtest` 的 docstring），
   所以切片落库之后成交怎么变都与池化无关——这一条用「删掉成交再重算」钉住。

DB 用例用真事务回滚的 `TestCase`；纯逻辑用 `SimpleTestCase`。样本一律**真的走一遍
`slice_result` 落进 `metrics`**（DB 用例）或 `slice_backtest`（纯函数用例），不手写
「我以为的载荷」：池化读的键是切片写的，手写一份会让两边的漂移永远测不出来。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from apps.regime import config, pool, pool_rebuild, slicing
from apps.regime import slice as sl
from apps.regime.config import EvidenceConfig
from apps.regime.models import (
    ActorKind,
    ArchetypeOverride,
    RebuildStatus,
    RegimePoolCell,
    RegimePoolRebuild,
)
from apps.regime.pool import (
    ARCHETYPE_SOURCE_KEYWORD,
    ARCHETYPE_SOURCE_OVERRIDE,
    ARCHETYPE_SOURCE_UNCLASSIFIED,
    ARCHETYPE_UNCLASSIFIED,
    EXCLUDED_NO_MERGE_INPUTS,
    EXCLUDED_NO_PAYLOAD,
    EXCLUDED_UNUSABLE_CAPITAL,
    POOL_SOURCE_ARCHETYPE,
    POOL_SOURCE_STRATEGY,
)
from apps.regime.quant import BaseRegime

RANGE = BaseRegime.RANGE
DOWNTREND = BaseRegime.DOWNTREND

#: 12 天窗口（端点 d0 / d11），与 `test_pool.py` / `test_slicing.py` 同形。
DAY0 = date(2026, 1, 1)
WINDOW = (DAY0, DAY0 + timedelta(days=11))
CAP = Decimal("10000.00")

#: 缩小后的证据门槛（默认 30 笔 / 3 个月要造几百天数据才出结论）。规则一模一样。
SMALL = EvidenceConfig(min_trades=3, min_months=1)

#: 同一组样本、只换门槛：用来钉「指纹跟着 `params` 走」。
TWO_MONTHS = EvidenceConfig(min_trades=3, min_months=2)

#: 造记录的基准时刻。所有人工建的世代都从这里派生，避免 `_finished_at` 的比较落到
#: 「同一微秒」上（那会让「按完成时刻取最近」的用例变成靠 id 兜底）。
T0 = datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)

#: `down-trend 那三笔平稳盈利把全样本曲线的峰抬起来，箱体那三笔连亏从峰上下来`——
#: 与 `test_pool.downtrend_profit_then_range_loss` 同一份数据，于是「下行趋势适用、
#: 箱体不适用」这个结论在两个文件里是同一个（本文件另有一处直接钉住它）。
DOWNTREND_TRADES = (
    (0, 1000.0),
    (1, 1000.0),
    (2, 1000.0),
    (7, -500.0),
    (8, -500.0),
    (9, -500.0),
)


def d(n: int) -> date:
    return DAY0 + timedelta(days=n)


def at(n: int, hour: int = 12) -> datetime:
    return datetime(2026, 1, 1, hour, tzinfo=timezone.utc) + timedelta(days=n)


def same_day(n: int, pnl: float) -> sl.SliceTrade:
    """当日开平：PnL 整笔落在那一天，数字可直接手算。"""
    return sl.SliceTrade(entry_time=at(n), exit_time=at(n), pnl=pnl)


def all_range_tags() -> dict[date, BaseRegime]:
    return {d(i): RANGE for i in range(12)}


def downtrend_tags() -> dict[date, BaseRegime]:
    """前六天是下行趋势、其余是箱体震荡，与 `DOWNTREND_TRADES` 的分段对齐。"""
    tags = all_range_tags()
    tags.update({d(i): DOWNTREND for i in range(6)})
    return tags


def tags_span(span: int, overrides: dict[int, BaseRegime] | None = None) -> dict:
    tags = {d(i): RANGE for i in range(span)}
    tags.update({d(offset): regime for offset, regime in (overrides or {}).items()})
    return tags


def pure_sample(
    trades: list[sl.SliceTrade],
    tags: dict,
    span: int,
    *,
    result_id: str = "r1",
    strategy_id: int = 1,
    symbol: str = "BTC/USDT",
    capital: float = float(CAP),
    params: EvidenceConfig = SMALL,
) -> pool.SliceSample:
    """纯函数路径的一份样本：`slice_backtest` → `load_sample`。"""
    data = sl.slice_backtest(
        trades,
        tags,
        initial_capital=capital,
        window=(d(0), d(span - 1)),
        params=params,
    )
    loaded = pool.load_sample(
        data, result_id=result_id, strategy_id=strategy_id, symbol=symbol
    )
    assert loaded.sample is not None, f"样本应当可用，实际被排除：{loaded.excluded}"
    return loaded.sample


# --------------------------------------------------------------------------- #
# 注册表替身
# --------------------------------------------------------------------------- #


def probe_class(stem: str, archetype: str) -> type:
    """一个够用的策略替身：注册表只要 `description`（`validate_description` 的四字段）。

    `archetype` 之外的三个字段一律写「基线」——**刻意不含任何原型关键词**：原型是按
    声明顺序取第一个命中的，替身里多一个「震荡」就会让「趋势跟踪」那条也归到均值回归，
    而那种失败看起来像映射表坏了，实际是夹具写脏了。
    """
    return type(
        f"Probe_{stem}",
        (),
        {
            "name": stem,
            "description": (
                f"策略类型：{archetype}\n"
                "核心指标：基线\n"
                "适用场景：基线\n"
                "入场逻辑：基线\n"
            ),
        },
    )


# --------------------------------------------------------------------------- #
# 纯逻辑：差异数与指纹
# --------------------------------------------------------------------------- #


def cell(
    strategy_id: int = 1,
    *,
    regime: str = RANGE.value,
    state: str = "fit",
    reason: str = "",
    source: str = POOL_SOURCE_STRATEGY,
    evidence: dict | None = None,
    needs_review: bool = False,
) -> pool.PooledCell:
    return pool.PooledCell(
        strategy_id=strategy_id,
        regime=regime,
        source=source,
        state=state,
        reason=reason,
        evidence=evidence if evidence is not None else {},
        needs_review=needs_review,
    )


class TestCellChanges(SimpleTestCase):
    """差异数只数结论三元组。"""

    def test_no_previous_generation_makes_every_cell_a_change(self):
        cells = {(1, "range"): cell(), (1, "uptrend"): cell(regime="uptrend")}

        self.assertEqual(pool_rebuild._cell_changes({}, cells), 2)

    def test_identical_conclusions_are_not_changes(self):
        cells = {(1, "range"): cell()}
        previous = {(1, "range"): ("fit", "", POOL_SOURCE_STRATEGY)}

        self.assertEqual(pool_rebuild._cell_changes(previous, cells), 0)

    def test_a_flipped_state_is_one_change(self):
        cells = {(1, "range"): cell(state="unfit")}
        previous = {(1, "range"): ("fit", "", POOL_SOURCE_STRATEGY)}

        self.assertEqual(pool_rebuild._cell_changes(previous, cells), 1)

    def test_a_flipped_reason_is_one_change(self):
        cells = {(1, "range"): cell(reason="direction_conflict")}
        previous = {(1, "range"): ("fit", "", POOL_SOURCE_STRATEGY)}

        self.assertEqual(pool_rebuild._cell_changes(previous, cells), 1)

    def test_a_flipped_source_is_one_change(self):
        """来源从「本策略切片」变成「原型兜底」是结论的变更，不是同一结论换个说法。"""
        cells = {(1, "range"): cell(source=POOL_SOURCE_ARCHETYPE)}
        previous = {(1, "range"): ("fit", "", POOL_SOURCE_STRATEGY)}

        self.assertEqual(pool_rebuild._cell_changes(previous, cells), 1)

    def test_a_new_cell_is_a_change(self):
        cells = {(1, "range"): cell(), (2, "range"): cell(strategy_id=2)}
        previous = {(1, "range"): ("fit", "", POOL_SOURCE_STRATEGY)}

        self.assertEqual(pool_rebuild._cell_changes(previous, cells), 1)

    def test_a_vanished_cell_is_a_change(self):
        previous = {
            (1, "range"): ("fit", "", POOL_SOURCE_STRATEGY),
            (2, "range"): ("fit", "", POOL_SOURCE_STRATEGY),
        }

        self.assertEqual(pool_rebuild._cell_changes(previous, {}), 2)

    def test_evidence_moving_does_not_count_as_a_change(self):
        """证据每天都在动（多一笔成交、窗口多一天），拿它当差异等于恒等于格子总数。"""
        cells = {(1, "range"): cell(evidence={"trades": 99, "results": ["r9"]})}
        previous = {(1, "range"): ("fit", "", POOL_SOURCE_STRATEGY)}

        self.assertEqual(pool_rebuild._cell_changes(previous, cells), 0)

    def test_needs_review_alone_does_not_count(self):
        """待复核由冲突推出，冲突的变化必然已经体现在状态或原因上。"""
        cells = {(1, "range"): cell(needs_review=True)}
        previous = {(1, "range"): ("fit", "", POOL_SOURCE_STRATEGY)}

        self.assertEqual(pool_rebuild._cell_changes(previous, cells), 0)


class TestInputFingerprint(SimpleTestCase):
    """指纹是「这次看进去了什么」的摘要，不是记录。"""

    def setUp(self):
        span = 12
        tags = tags_span(span, {i: DOWNTREND for i in range(6)})
        self.samples = [
            pure_sample(
                [same_day(0, 1000.0), same_day(1, 1000.0), same_day(2, 1000.0)],
                tags,
                span,
            ),
            pure_sample(
                [same_day(7, -500.0), same_day(8, -500.0), same_day(9, -500.0)],
                tags,
                span,
                result_id="r2",
                strategy_id=2,
            ),
        ]
        self.exclusions = {EXCLUDED_NO_PAYLOAD: 1, EXCLUDED_NO_MERGE_INPUTS: 0}

    def fingerprint(self, *, samples=None, exclusions=None, params=SMALL):
        return pool_rebuild.input_fingerprint(
            self.samples if samples is None else samples,
            self.exclusions if exclusions is None else exclusions,
            params,
        )

    def test_it_is_a_sha256_hex_digest(self):
        value = self.fingerprint()

        self.assertEqual(len(value), 64)
        self.assertEqual(value, value.lower())
        int(value, 16)  # 不是十六进制就会在这里炸

    def test_the_same_inputs_give_the_same_fingerprint(self):
        self.assertEqual(self.fingerprint(), self.fingerprint())

    def test_the_order_of_samples_does_not_matter(self):
        """样本的到来顺序由回测的创建顺序决定，它不是输入的一部分。"""
        self.assertEqual(
            self.fingerprint(samples=list(reversed(self.samples))), self.fingerprint()
        )

    def test_a_changed_sample_changes_the_fingerprint(self):
        span = 12
        tags = tags_span(span)
        other = pure_sample([same_day(3, 100.0)], tags, span)

        self.assertNotEqual(
            self.fingerprint(samples=[*self.samples, other]), self.fingerprint()
        )

    def test_a_changed_exclusion_count_changes_the_fingerprint(self):
        self.assertNotEqual(
            self.fingerprint(exclusions={EXCLUDED_NO_PAYLOAD: 2}),
            self.fingerprint(exclusions={EXCLUDED_NO_PAYLOAD: 1}),
        )

    def test_a_changed_threshold_changes_the_fingerprint(self):
        """`build_pool` 用的是 `params`，指纹就必须跟着 `params` 走。

        这一条坏掉的样子是**静默**的：两次不同门槛的重算算出同一个指纹，第二次把
        第一次那一代当成「输入没变」直接复用，于是换了门槛却什么都没发生。
        """
        self.assertNotEqual(
            self.fingerprint(params=TWO_MONTHS), self.fingerprint(params=SMALL)
        )
        self.assertNotEqual(
            self.fingerprint(params=config.EVIDENCE), self.fingerprint(params=SMALL)
        )

    def test_a_different_parameters_class_is_a_different_caliber(self):
        """「换了一个类、字段恰好同名同值」也是换了一套口径。"""

        @dataclass(frozen=True)
        class Lookalike:
            min_trades: int = 3
            min_months: int = 1

        self.assertNotEqual(
            self.fingerprint(params=Lookalike()), self.fingerprint(params=SMALL)
        )

    def test_a_changed_pooling_version_changes_the_fingerprint(self):
        with patch.object(pool, "POOL_VERSION", pool.POOL_VERSION + 1):
            bumped = self.fingerprint()

        self.assertNotEqual(bumped, self.fingerprint())

    def test_it_reads_the_effective_params_not_the_registered_group(self):
        """登记在 `GROUPS` 里那一份变了、而本次生效的 `params` 没变，指纹不该动。"""
        with patch.dict(config.GROUPS, {"evidence": TWO_MONTHS}):
            self.assertEqual(
                self.fingerprint(params=SMALL), self.fingerprint(params=SMALL)
            )
            self.assertNotEqual(
                self.fingerprint(params=SMALL), self.fingerprint(params=TWO_MONTHS)
            )


# --------------------------------------------------------------------------- #
# 纯逻辑以外的 DB 侧
# --------------------------------------------------------------------------- #


def make_generation(
    *,
    status: str = RebuildStatus.READY.value,
    fingerprint: str = "f0",
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
    actor_kind: str = ActorKind.TASK.value,
    actor_name: str = "任务",
) -> RegimePoolRebuild:
    """人工造一代。绕过 `rebuild_pool`，好让「当前代是哪一个」的用例不被别的东西影响。"""
    return RegimePoolRebuild.objects.create(
        status=status,
        actor_kind=actor_kind,
        actor_name=actor_name,
        pool_version=pool.POOL_VERSION,
        input_fingerprint=fingerprint,
        started_at=started_at or T0,
        finished_at=finished_at,
    )


def add_cell(
    generation, strategy, regime, *, state="fit", reason="", source=POOL_SOURCE_STRATEGY
):
    return RegimePoolCell.objects.create(
        rebuild=generation,
        strategy=strategy,
        regime=regime.value,
        source=source,
        state=state,
        reason=reason,
        evidence={},
    )


class _Fixture(TestCase):
    """三个策略：两个有实现类（原型不同）、一个没有（未归类的来源）。"""

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

    def setUp(self):
        from apps.strategy_engine.registry import StrategyRegistry

        self.registry = StrategyRegistry
        for name, archetype in (("AlphaStem", "均值回归"), ("BetaStem", "趋势跟踪")):
            StrategyRegistry.register(probe_class(name, archetype), name=name)
            self.addCleanup(StrategyRegistry._strategies.pop, name, None)

    # -- 造数据 ---------------------------------------------------------- #

    def add_result(self, strategy, *, trades, symbol="BTC/USDT", capital=CAP):
        from apps.backtest.models import BacktestResult, BacktestTrade

        result = BacktestResult.objects.create(
            strategy=strategy,
            symbol=symbol,
            timeframe="1d",
            start_date=WINDOW[0],
            end_date=WINDOW[1],
            initial_capital=capital,
            final_capital=Decimal("10500.00"),
            total_return_pct=5.0,
            metrics={},
        )
        for day, pnl in trades:
            BacktestTrade.objects.create(
                backtest=result,
                entry_time=at(day),
                exit_time=at(day),
                side="long",
                entry_price=Decimal("100.00000000"),
                exit_price=Decimal("101.00000000"),
                quantity=Decimal("1.00000000"),
                pnl=Decimal(str(pnl)),
                trade_type="close",
            )
        return result

    def sliced(
        self, strategy, *, trades=DOWNTREND_TRADES, tags=None, params=SMALL, **kwargs
    ):
        """造一个结果并把切片落库（真走 `slice_result`，不手写载荷）。

        落完刷新一次内存里的 `metrics`：`save_slice` 写的是它自己取出来的那一行，手上
        这个对象仍然是「切片之前」的样子。生产路径读的是刚查出来的行，所以这是测试
        夹具的账，不该让用例去记得重新取一次。
        """
        result = self.add_result(strategy, trades=trades, **kwargs)
        slicing.compute_slice(result, tags or downtrend_tags(), params=params)
        result.refresh_from_db(fields=["metrics"])
        return result

    def unsliced(self, strategy, **kwargs):
        return self.add_result(strategy, trades=((0, 100.0),), **kwargs)


class TestCurrentGeneration(_Fixture):
    """「当前」= 最近一次**翻成 ready** 的那一代。"""

    def test_cold_start_is_not_a_fault(self):
        self.assertIsNone(pool_rebuild.current_generation())
        self.assertEqual(pool_rebuild.current_cells(), {})

    def test_building_and_failed_generations_are_not_current(self):
        make_generation(status=RebuildStatus.BUILDING.value, fingerprint="b")
        make_generation(
            status=RebuildStatus.FAILED.value, fingerprint="f", finished_at=T0
        )

        self.assertIsNone(pool_rebuild.current_generation())

    def test_current_is_the_latest_finished_not_the_latest_started(self):
        """并发时先起跑的那一代可能后完成，而「当前」问的是哪一代最后咬人。"""
        early_start_late_finish = make_generation(
            fingerprint="early",
            started_at=T0,
            finished_at=T0 + timedelta(hours=5),
        )
        make_generation(
            fingerprint="late",
            started_at=T0 + timedelta(hours=1),
            finished_at=T0 + timedelta(hours=2),
        )

        self.assertEqual(
            pool_rebuild.current_generation().pk, early_start_late_finish.pk
        )

    def test_cells_are_keyed_by_strategy_and_regime(self):
        generation = make_generation(finished_at=T0)
        add_cell(generation, self.alpha, RANGE, state="fit")
        add_cell(generation, self.beta, DOWNTREND, state="unfit")

        cells = pool_rebuild.current_cells()

        self.assertEqual(
            set(cells), {(self.alpha.id, RANGE.value), (self.beta.id, DOWNTREND.value)}
        )
        self.assertEqual(cells[(self.alpha.id, RANGE.value)]["state"], "fit")
        self.assertEqual(cells[(self.beta.id, DOWNTREND.value)]["reason"], "")

    def test_cells_of_an_older_generation_are_not_current(self):
        old = make_generation(fingerprint="old", started_at=T0, finished_at=T0)
        add_cell(old, self.alpha, RANGE, state="unfit")
        fresh = make_generation(
            fingerprint="new", started_at=T0, finished_at=T0 + timedelta(hours=1)
        )
        add_cell(fresh, self.alpha, RANGE, state="fit")

        cells = pool_rebuild.current_cells()

        self.assertEqual(cells[(self.alpha.id, RANGE.value)]["state"], "fit")

    def test_the_finished_at_tie_falls_back_to_the_id(self):
        """同一时刻的两代（测试里的常见形状）按 id 取后落的那一代，不是随机的。"""
        first = make_generation(fingerprint="a", started_at=T0, finished_at=T0)
        second = make_generation(fingerprint="b", started_at=T0, finished_at=T0)

        self.assertEqual(pool_rebuild.current_generation().pk, second.pk)
        self.assertNotEqual(first.pk, second.pk)

    def test_a_building_row_with_no_finished_at_is_never_picked(self):
        make_generation(
            status=RebuildStatus.BUILDING.value, fingerprint="x", finished_at=None
        )

        self.assertIsNone(pool_rebuild.current_generation())


class TestCollectSamples(_Fixture):
    """取样本这一层：三种排除各有各的原因，分开计数。"""

    def test_a_result_without_a_slice_is_counted_and_is_not_a_sample(self):
        self.unsliced(self.alpha)

        samples, exclusions, candidates = pool_rebuild.collect_samples()

        self.assertEqual(samples, [])
        self.assertEqual(candidates, 1)
        self.assertEqual(exclusions[EXCLUDED_NO_PAYLOAD], 1)

    def test_a_version_one_payload_is_counted_as_no_merge_inputs(self):
        """版本 1 的载荷没有 `merge_inputs`——跑一次重算入口就好，与本金为 0 不同。"""
        result = self.sliced(self.alpha)
        stored = slicing.stored_slice(result)
        for cell_payload in stored["cells"].values():
            del cell_payload["merge_inputs"]
        slicing.save_slice(result.id, stored)

        samples, exclusions, _ = pool_rebuild.collect_samples()

        self.assertEqual(samples, [])
        self.assertEqual(exclusions[EXCLUDED_NO_MERGE_INPUTS], 1)

    def test_an_unusable_capital_is_its_own_reason(self):
        """本金 ≤ 0 要人去查回测参数，所以它不能与「没切过片」混成一个数。"""
        self.sliced(self.alpha, capital=Decimal("0.00"))

        samples, exclusions, _ = pool_rebuild.collect_samples()

        self.assertEqual(samples, [])
        self.assertEqual(exclusions[EXCLUDED_UNUSABLE_CAPITAL], 1)

    def test_the_three_reasons_coexist_and_the_key_set_is_constant(self):
        """三个原因同场出现，计数互不串味，且键集恒定（0 的键也留着）。"""
        self.unsliced(self.alpha)
        self.sliced(self.beta, capital=Decimal("0.00"))
        result = self.sliced(self.gamma)
        stored = slicing.stored_slice(result)
        for cell_payload in stored["cells"].values():
            # 删键而不是置 `None`：`None` 是「算不出归一化」（本金那一类），
            # 删键是「这份载荷里根本没有」，两者在 `load_sample` 里走的是不同的分支。
            del cell_payload["merge_inputs"]
        slicing.save_slice(result.id, stored)

        _, exclusions, candidates = pool_rebuild.collect_samples()

        self.assertEqual(candidates, 3)
        self.assertEqual(
            {k: v for k, v in exclusions.items() if v},
            {
                EXCLUDED_NO_PAYLOAD: 1,
                EXCLUDED_UNUSABLE_CAPITAL: 1,
                EXCLUDED_NO_MERGE_INPUTS: 1,
            },
        )
        self.assertEqual(set(exclusions), set(pool_rebuild.EXCLUSION_KEYS))
        self.assertIn(EXCLUDED_NO_MERGE_INPUTS, pool_rebuild.EXCLUSION_KEYS)

    def test_a_usable_result_becomes_one_sample_named_after_the_result(self):
        result = self.sliced(self.alpha)

        samples, exclusions, candidates = pool_rebuild.collect_samples()

        self.assertEqual(candidates, 1)
        self.assertEqual(sum(exclusions.values()), 0)
        self.assertEqual(len(samples), 1)
        sample = samples[0]
        self.assertEqual(sample.result_id, str(result.id))
        self.assertEqual(sample.strategy_id, self.alpha.id)
        self.assertEqual(sample.symbol, "BTC/USDT")
        self.assertEqual(sample.window, WINDOW)

    def test_two_backtests_of_one_strategy_are_two_samples(self):
        self.sliced(self.alpha)
        self.sliced(self.alpha)
        self.sliced(self.beta)

        samples, _, candidates = pool_rebuild.collect_samples()

        self.assertEqual(candidates, 3)
        self.assertEqual(len(samples), 3)
        self.assertEqual(
            sorted(s.strategy_id for s in samples),
            sorted([self.alpha.id, self.alpha.id, self.beta.id]),
        )

    def test_the_sample_comes_from_the_stored_payload_not_from_the_trades(self):
        """载荷是自足的：切片落库之后成交怎么变都与池化无关。"""
        result = self.sliced(self.alpha)
        from apps.backtest.models import BacktestTrade

        BacktestTrade.objects.filter(backtest=result).delete()

        samples, _, _ = pool_rebuild.collect_samples()

        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0].cell_regime_days[DOWNTREND.value], 6)

    def test_the_regime_days_travel_with_the_sample(self):
        self.sliced(self.alpha)

        samples, _, _ = pool_rebuild.collect_samples()

        self.assertEqual(samples[0].cell_regime_days[RANGE.value], 6)
        self.assertEqual(samples[0].cell_regime_days[DOWNTREND.value], 6)


class TestStrategyRawTexts(_Fixture):
    """注册表是「这条策略还有代码在跑吗」的唯一出处。"""

    def test_a_registered_strategy_yields_its_description(self):
        texts, unresolved = pool_rebuild.strategy_raw_texts([self.alpha.id])

        self.assertEqual(unresolved, [])
        self.assertIn("均值回归", texts[self.alpha.id])

    def test_an_unregistered_strategy_yields_nothing_and_is_reported(self):
        """回测自动建出来的幽灵策略行本来就没有实现类——如实报，不发明一个原型。"""
        texts, unresolved = pool_rebuild.strategy_raw_texts([self.gamma.id])

        self.assertEqual(texts[self.gamma.id], "")
        self.assertEqual(unresolved, [self.gamma.id])

    def test_both_kinds_come_back_together(self):
        texts, unresolved = pool_rebuild.strategy_raw_texts(
            [self.alpha.id, self.gamma.id, self.beta.id]
        )

        self.assertEqual(set(texts), {self.alpha.id, self.gamma.id, self.beta.id})
        self.assertEqual(unresolved, [self.gamma.id])

    def test_no_ids_is_an_empty_answer(self):
        self.assertEqual(pool_rebuild.strategy_raw_texts([]), ({}, []))


class TestArchetypeMatches(_Fixture):
    """覆盖表优先于映射表；没登记过等于未归类。"""

    def test_the_mapping_table_is_the_default(self):
        matches = pool_rebuild.archetype_matches([self.alpha.id, self.beta.id])

        self.assertEqual(matches[self.alpha.id].archetype, "均值回归")
        self.assertEqual(matches[self.alpha.id].source, ARCHETYPE_SOURCE_KEYWORD)
        self.assertEqual(matches[self.beta.id].archetype, "趋势跟踪")

    def test_an_unregistered_strategy_falls_to_unclassified(self):
        matches = pool_rebuild.archetype_matches([self.gamma.id])

        self.assertEqual(matches[self.gamma.id].archetype, ARCHETYPE_UNCLASSIFIED)
        self.assertEqual(matches[self.gamma.id].source, ARCHETYPE_SOURCE_UNCLASSIFIED)

    def test_every_id_gets_an_entry_even_if_unused(self):
        """样本足的策略当下用不到它，但「归到哪一类」不该随样本多寡变成一个没人记录的事实。"""
        matches = pool_rebuild.archetype_matches(
            [self.alpha.id, self.beta.id, self.gamma.id]
        )

        self.assertEqual(set(matches), {self.alpha.id, self.beta.id, self.gamma.id})

    def test_the_override_table_wins_over_the_mapping_table(self):
        ArchetypeOverride.objects.create(
            strategy=self.alpha, archetype="自定义原型", created_by="xl"
        )

        matches = pool_rebuild.archetype_matches([self.alpha.id])

        self.assertEqual(matches[self.alpha.id].archetype, "自定义原型")
        self.assertEqual(matches[self.alpha.id].source, ARCHETYPE_SOURCE_OVERRIDE)

    def test_an_explicit_override_mapping_skips_the_table(self):
        ArchetypeOverride.objects.create(
            strategy=self.alpha, archetype="表里的", created_by="xl"
        )

        matches = pool_rebuild.archetype_matches(
            [self.alpha.id], overrides={self.alpha.id: "传进来的"}
        )

        self.assertEqual(matches[self.alpha.id].archetype, "传进来的")

    def test_explicit_raw_texts_skip_the_registry(self):
        matches = pool_rebuild.archetype_matches(
            [self.gamma.id], raw_texts={self.gamma.id: "策略类型：套利\n"}
        )

        self.assertEqual(matches[self.gamma.id].archetype, "套利")

    def test_no_ids_is_an_empty_answer(self):
        self.assertEqual(pool_rebuild.archetype_matches([]), {})


class TestRebuildPool(_Fixture):
    """端到端：取样本 → 认指纹 → 落一代 → 报差异。"""

    def setUp(self):
        super().setUp()
        self.alpha_result = self.sliced(self.alpha)
        self.beta_result = self.sliced(
            self.beta, trades=((3, 200.0), (4, 200.0), (5, 200.0))
        )

    # -- 正常一代 ------------------------------------------------------- #

    def test_a_rebuild_produces_one_ready_generation(self):
        summary = pool_rebuild.rebuild_pool(actor_name="tester")

        generation = RegimePoolRebuild.objects.get(pk=summary["rebuild_id"])
        self.assertEqual(generation.status, RebuildStatus.READY.value)
        self.assertIsNotNone(generation.finished_at)
        self.assertEqual(generation.pool_version, pool.POOL_VERSION)
        self.assertEqual(generation.actor_kind, ActorKind.TASK.value)
        self.assertEqual(generation.actor_name, "tester")
        self.assertEqual(generation.failure, "")
        self.assertFalse(summary["reused"])
        self.assertFalse(summary["duplicate"])

    def test_the_counts_are_the_ones_the_summary_reports(self):
        summary = pool_rebuild.rebuild_pool(actor_name="tester")

        generation = RegimePoolRebuild.objects.get(pk=summary["rebuild_id"])
        self.assertEqual(summary["candidates"], 2)
        self.assertEqual(summary["results_used"], 2)
        self.assertEqual(summary["cells_total"], 8)
        self.assertEqual(generation.candidates, 2)
        self.assertEqual(generation.results_used, 2)
        self.assertEqual(generation.cells_total, 8)
        self.assertEqual(
            RegimePoolCell.objects.filter(rebuild=generation).count(),
            generation.cells_total,
        )

    def test_every_sample_bearing_strategy_gets_all_four_regimes(self):
        """缺失的格子在日报里表现为「这一行没有」，而它有两种意思——所以不留空格。"""
        pool_rebuild.rebuild_pool()

        cells = pool_rebuild.current_cells()

        self.assertEqual(len(cells), 8)
        for strategy in (self.alpha, self.beta):
            for regime in BaseRegime:
                with self.subTest(strategy=strategy.name, regime=regime.value):
                    self.assertIn((strategy.id, regime.value), cells)

    def test_the_persisted_cells_are_the_pooled_cells(self):
        """落库这一层不重新判定：库里那一代必须与纯函数算出来的逐格相同。"""
        pool_rebuild.rebuild_pool(params=SMALL)

        samples, _, _ = pool_rebuild.collect_samples()
        matches = pool_rebuild.archetype_matches([s.strategy_id for s in samples])
        expected = pool.build_pool(samples, matches, params=SMALL)
        actual = {
            key: (row["source"], row["state"], row["reason"], row["needs_review"])
            for key, row in pool_rebuild.current_cells().items()
        }

        self.assertEqual(
            actual,
            {
                key: (cell.source, cell.state, cell.reason, cell.needs_review)
                for key, cell in expected.cells.items()
            },
        )
        self.assertEqual(len(actual), len(expected.cells))

    def test_a_known_fixture_keeps_its_conclusions(self):
        """与 `test_pool.downtrend_profit_then_range_loss` 同一份数据，结论也应当同一份。

        门槛跟着切片走（`sliced` 用哪一组，这里就用哪一组）：这一格要问的是「池化
        有没有忠实地搬运切片结论」，拿另一组门槛去问，问到的会是原型兜底那一层。
        """
        pool_rebuild.rebuild_pool(params=SMALL)

        cells = pool_rebuild.current_cells()

        self.assertEqual(cells[(self.alpha.id, DOWNTREND.value)]["state"], sl.STATE_FIT)
        self.assertEqual(cells[(self.alpha.id, RANGE.value)]["state"], sl.STATE_UNFIT)

    def test_the_evidence_is_persisted_with_the_cell(self):
        """依据不进差异计数，但它必须落下来——它是「凭什么这么判」的唯一答案。"""
        pool_rebuild.rebuild_pool(params=SMALL)

        cells = pool_rebuild.current_cells()
        evidence = cells[(self.alpha.id, RANGE.value)]["evidence"]

        # 逐条带 id、品种、窗口：只有这样，池化表上那一格才能在没有原始回测行的情况下
        # 回答「是哪些回测算出来的」。
        self.assertEqual(
            [entry["result_id"] for entry in evidence["results"]],
            [str(self.alpha_result.id)],
        )
        self.assertEqual(evidence["results"][0]["symbol"], "BTC/USDT")

    def test_unclassified_strategies_are_reported_not_hidden(self):
        self.sliced(self.gamma, trades=((3, 100.0),))

        summary = pool_rebuild.rebuild_pool()

        self.assertEqual(summary["unclassified"], [self.gamma.id])
        cells = pool_rebuild.current_cells()
        for regime in BaseRegime:
            with self.subTest(regime=regime.value):
                row = cells[(self.gamma.id, regime.value)]
                self.assertEqual(row["source"], POOL_SOURCE_ARCHETYPE)
                self.assertEqual(
                    row["evidence"]["archetype"]["name"], ARCHETYPE_UNCLASSIFIED
                )
                self.assertFalse(row["needs_review"])

    def test_the_exclusions_land_on_the_generation(self):
        self.unsliced(self.gamma)

        summary = pool_rebuild.rebuild_pool()

        generation = RegimePoolRebuild.objects.get(pk=summary["rebuild_id"])
        self.assertEqual(generation.exclusions[EXCLUDED_NO_PAYLOAD], 1)
        self.assertEqual(set(generation.exclusions), set(pool_rebuild.EXCLUSION_KEYS))

    def test_the_actor_defaults_to_the_scheduled_task(self):
        summary = pool_rebuild.rebuild_pool()

        generation = RegimePoolRebuild.objects.get(pk=summary["rebuild_id"])
        self.assertEqual(generation.actor_kind, ActorKind.TASK.value)

    def test_the_command_line_actor_is_recorded_as_such(self):
        summary = pool_rebuild.rebuild_pool(
            actor_kind=ActorKind.CLI.value, actor_name="xl"
        )

        generation = RegimePoolRebuild.objects.get(pk=summary["rebuild_id"])
        self.assertEqual(generation.actor_kind, ActorKind.CLI.value)
        self.assertEqual(generation.actor_name, "xl")

    def test_the_injected_now_becomes_the_started_and_finished_time(self):
        moment = T0 + timedelta(days=3)

        summary = pool_rebuild.rebuild_pool(now=moment)

        generation = RegimePoolRebuild.objects.get(pk=summary["rebuild_id"])
        self.assertEqual(generation.started_at, moment)
        self.assertEqual(generation.finished_at, moment)

    # -- 指纹复用 ------------------------------------------------------- #

    def test_a_second_rebuild_with_the_same_inputs_reuses_the_generation(self):
        first = pool_rebuild.rebuild_pool()

        second = pool_rebuild.rebuild_pool()

        self.assertTrue(second["reused"])
        self.assertEqual(second["rebuild_id"], first["rebuild_id"])
        self.assertEqual(RegimePoolRebuild.objects.count(), 1)
        self.assertEqual(second["cells_total"], first["cells_total"])

    def test_the_reused_generation_is_still_the_current_one(self):
        first = pool_rebuild.rebuild_pool()
        before = pool_rebuild.current_generation().pk

        pool_rebuild.rebuild_pool()

        self.assertEqual(pool_rebuild.current_generation().pk, before)
        self.assertEqual(before, first["rebuild_id"])

    def test_a_reused_generation_reports_the_numbers_read_back_from_its_cells(self):
        """复用那一轮没有 `PoolResult` 可问，这两个数只能从格子上读回来。

        返回恒定的 `[]`/`0` 就是拿「本轮压根没算」冒充「算出来是空」：两者在日报上
        长得一模一样，而它们要求的事情相反——一个是「去看策略描述」，一个是「没事」。
        """
        self.sliced(self.gamma, trades=((3, 100.0),))
        first = pool_rebuild.rebuild_pool()
        self.assertEqual(first["unclassified"], [self.gamma.id])
        self.assertEqual(first["needs_review"], 0)

        # 直接改库，好让读回来的数不等于「永远返回 0」的那个常量——否则这个用例
        # 对「复用分支写死 []/0」的实现也是绿的。
        one = (
            RegimePoolCell.objects.filter(rebuild_id=first["rebuild_id"])
            .order_by("id")
            .first()
        )
        RegimePoolCell.objects.filter(pk=one.pk).update(needs_review=True)

        second = pool_rebuild.rebuild_pool()

        self.assertTrue(second["reused"])
        self.assertEqual(second["rebuild_id"], first["rebuild_id"])
        self.assertEqual(second["unclassified"], [self.gamma.id])
        self.assertEqual(second["needs_review"], 1)

    def test_a_changed_threshold_is_a_new_generation(self):
        """换了门槛却复用了上一代，是「指纹去读了全局配置」的典型症状。"""
        first = pool_rebuild.rebuild_pool(params=SMALL)

        second = pool_rebuild.rebuild_pool(params=TWO_MONTHS)

        self.assertFalse(second["reused"])
        self.assertNotEqual(second["rebuild_id"], first["rebuild_id"])
        self.assertEqual(RegimePoolRebuild.objects.count(), 2)
        self.assertEqual(
            RegimePoolRebuild.objects.get(pk=second["rebuild_id"]).pool_version,
            pool.POOL_VERSION,
        )

    def test_a_new_backtest_is_a_new_generation(self):
        first = pool_rebuild.rebuild_pool()

        self.sliced(self.gamma, trades=((3, 100.0),))
        second = pool_rebuild.rebuild_pool()

        self.assertFalse(second["reused"])
        self.assertNotEqual(second["rebuild_id"], first["rebuild_id"])

    # -- 差异计数 ------------------------------------------------------- #

    def test_the_first_generation_changes_every_cell(self):
        """从「什么都没有」变成一整代结论，那确实每一格都变了。"""
        summary = pool_rebuild.rebuild_pool()

        self.assertEqual(summary["cells_changed"], 8)

    def test_new_cells_count_as_changes(self):
        pool_rebuild.rebuild_pool()
        self.sliced(self.gamma, trades=((3, 100.0),))

        summary = pool_rebuild.rebuild_pool()

        self.assertEqual(summary["cells_changed"], 4)
        generation = RegimePoolRebuild.objects.get(pk=summary["rebuild_id"])
        self.assertEqual(generation.cells_changed, 4)

    def test_an_exclusion_only_round_changes_nothing(self):
        """新增一个没切过片的回测会换指纹、会重算，但一个格子都不该动。"""
        before = pool_rebuild.rebuild_pool()
        self.unsliced(self.gamma)

        after = pool_rebuild.rebuild_pool()

        self.assertFalse(after["reused"])
        self.assertEqual(after["cells_changed"], 0)
        self.assertEqual(after["cells_total"], before["cells_total"])

    # -- 失败与并发 ----------------------------------------------------- #

    def test_a_failure_leaves_a_failed_generation_with_the_reason(self):
        with patch.object(
            pool_rebuild, "_mark_ready", side_effect=RuntimeError("boom")
        ):
            with self.assertRaises(RuntimeError):
                pool_rebuild.rebuild_pool()

        generation = RegimePoolRebuild.objects.get()
        self.assertEqual(generation.status, RebuildStatus.FAILED.value)
        self.assertEqual(generation.failure, "boom")
        self.assertIsNotNone(generation.finished_at)

    def test_a_failed_generation_keeps_its_cells_as_evidence(self):
        """残骸与原因一起才回答得了「为什么会这样」，所以不删。"""
        with patch.object(
            pool_rebuild, "_mark_ready", side_effect=RuntimeError("boom")
        ):
            with self.assertRaises(RuntimeError):
                pool_rebuild.rebuild_pool()

        generation = RegimePoolRebuild.objects.get()
        self.assertEqual(RegimePoolCell.objects.filter(rebuild=generation).count(), 8)

    def test_a_failed_generation_never_becomes_current(self):
        with patch.object(
            pool_rebuild, "_mark_ready", side_effect=RuntimeError("boom")
        ):
            with self.assertRaises(RuntimeError):
                pool_rebuild.rebuild_pool()

        self.assertIsNone(pool_rebuild.current_generation())
        self.assertEqual(pool_rebuild.current_cells(), {})

    def test_a_duplicate_trigger_is_a_recorded_outcome_not_a_crash(self):
        """并发触发的第二方撞上 `uniq_pool_rebuild_ready_fingerprint`——那是约束在起作用。

        模拟方式是真的跑到那一步：在翻代之前插一代同指纹的 `ready`，于是翻代时那条
        部分唯一索引把它拦下来。整条路径（写入 → 撞约束 → 记原因 → 如实返回）都走到。
        """
        real_mark_ready = pool_rebuild._mark_ready
        competing: list[int] = []

        def race(rebuild, changed, now):
            rival = make_generation(
                fingerprint=rebuild.input_fingerprint,
                finished_at=now,
                actor_name="对方",
            )
            competing.append(rival.pk)
            return real_mark_ready(rebuild, changed, now)

        with patch.object(pool_rebuild, "_mark_ready", race):
            summary = pool_rebuild.rebuild_pool()

        self.assertTrue(summary["duplicate"])
        self.assertFalse(summary["reused"])
        generation = RegimePoolRebuild.objects.get(pk=summary["rebuild_id"])
        self.assertEqual(generation.status, RebuildStatus.FAILED.value)
        self.assertIn("重复触发", generation.failure)
        self.assertEqual(pool_rebuild.current_generation().pk, competing[0])

    def test_a_duplicate_trigger_does_not_leave_a_second_cell_set_in_play(self):
        """残骸的格子留着，但它们挂在一代 `failed` 上，`current_cells()` 看不到。"""
        real_mark_ready = pool_rebuild._mark_ready

        def race(rebuild, changed, now):
            make_generation(fingerprint=rebuild.input_fingerprint, finished_at=now)
            return real_mark_ready(rebuild, changed, now)

        with patch.object(pool_rebuild, "_mark_ready", race):
            summary = pool_rebuild.rebuild_pool()

        self.assertEqual(
            RegimePoolCell.objects.filter(rebuild_id=summary["rebuild_id"]).count(), 8
        )
        self.assertEqual(len(pool_rebuild.current_cells()), 0)

    def test_the_rebuild_does_not_reset_database_connections(self):
        """连接纪律属于调用方（`recompute_regime_slices`），不属于这个被调用的函数。

        它在 `TestCase` 里会直接掐掉测试那条连接（`CONN_MAX_AGE=0` 时 `close_at`
        就是连接建立的时刻），在生产里也是无用的——这里没有长循环的重复事务边界。
        """
        with patch("django.db.close_old_connections") as reset:
            pool_rebuild.rebuild_pool()

        reset.assert_not_called()
