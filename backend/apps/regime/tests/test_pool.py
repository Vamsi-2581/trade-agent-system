"""池化（第①段单元 7）。

这个文件要钉住的不是「合并后的 Calmar 等于几」，而是六条性质：

1. **合并是加权合并**——把成交合起来按合并后的样本重算，不是投票、也不是把各家的
   PnL 直接相加。归一化到收益率之后叠加，所以两笔本金差十倍的回测在合并里等权：
   这正是「5 笔的切片不该与 100 笔的切片等权」的落点（等权的是**回测**，不是**单笔**）。
2. **池化门槛不缩放**，而且两条门槛**并列**不是乘积。单元 6 按 `day_share` 缩放笔数
   门槛，池化没有这个量，于是用未缩放的 `min_trades`；而并列判据挡掉「300 笔挤在
   一个月里也能过」。两条都是「门槛是唯一天然刹车」的具体形态。
3. **判定顺序与单元 6 逐条对齐**：该阶段没出现 → 保命档 → 门槛 → 比对。四态各有
   各的成因，两层同序日报才能用同一句话解释。
4. **冲突阻塞建议、不阻塞结论**：方向相反且**两侧都过门槛**才标 `needs_review`，
   而 `state` 照常产出。一侧不过门槛是样本噪声，不是冲突。
5. **原型兜底**：样本不足时改用「原型 × 阶段」的样本，覆盖表优先于映射表，映射表
   按声明顺序取第一个命中，未归类要计数（日报第④段）。
6. **词表是落库契约**：`state`/`reason` 在模型上是裸 `CharField`（模型的 import
   链不能碰 `slice`），所以「两处同集合」这件事必须由本文件钉住。

用例里的门槛是**刻意缩小**的（`min_trades=3, min_months=1`），与 `test_slice.py`
同一做法：默认的 30 笔 / 3 个月要造几百天数据才出结论，断言只会被埋进噪声里。
缩小的只是规模，规则一模一样；默认值本身由 `test_config` 钉住。

样本一律**真的走一遍 `slice_backtest`** 造出来，不手写载荷字典：池化读的键是切片写
的，手写一份「我以为的载荷」会让两边的漂移永远测不出来——那正是这个适配层最容易
坏掉的地方。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from django.test import SimpleTestCase

from apps.regime import pool
from apps.regime import slice as sl
from apps.regime.config import EvidenceConfig
from apps.regime.models import PoolSource, RegimePoolCell
from apps.regime.pool import (
    ARCHETYPE_DECLARATION_PREFIX,
    ARCHETYPE_SOURCE_KEYWORD,
    ARCHETYPE_SOURCE_OVERRIDE,
    ARCHETYPE_SOURCE_UNCLASSIFIED,
    ARCHETYPE_UNCLASSIFIED,
    EXCLUDED_NO_MERGE_INPUTS,
    EXCLUDED_NO_PAYLOAD,
    EXCLUDED_UNUSABLE_CAPITAL,
    POOL_SOURCE_ARCHETYPE,
    POOL_SOURCE_STRATEGY,
    build_pool,
    load_sample,
    normalize_archetype,
    resolve_archetype,
)
from apps.regime.quant import PRIORITY, BaseRegime

HIGH_VOL = BaseRegime.HIGH_VOL
DOWNTREND = BaseRegime.DOWNTREND
UPTREND = BaseRegime.UPTREND
RANGE = BaseRegime.RANGE

#: 缩小后的证据门槛：3 笔 / 1 个自然月。见模块 docstring。
SMALL = EvidenceConfig(min_trades=3, min_months=1)

#: 只缩笔数、保留 `min_months=3`：用来单独测「两条门槛并列」。
TWO_MONTHS = EvidenceConfig(min_trades=3, min_months=2)

#: 2026-01-01。2026 不是闰年：d(0)~d(30) 落在一月，d(31) 起落在二月。
DAY0 = date(2026, 1, 1)
CAP = 10000.0


def d(n: int) -> date:
    return DAY0 + timedelta(days=n)


def at(n: int, hour: int = 12, tz=timezone.utc) -> datetime:
    return datetime(2026, 1, 1, hour, tzinfo=tz) + timedelta(days=n)


def same_day(n: int, pnl: float) -> sl.SliceTrade:
    """当日开平：PnL 整笔落在那一天，数字可直接手算，合并也只需按日相加。"""
    return sl.SliceTrade(entry_time=at(n), exit_time=at(n), pnl=pnl)


def tags_span(span: int, overrides: dict[int, BaseRegime] | None = None) -> dict:
    """`d(0)~d(span-1)` 的标签，默认全是箱体震荡，`overrides` 按偏移覆盖。

    默认全标签是刻意的：没被 `overrides` 提到的阶段在窗口里**一天都没有**，于是
    那些格子正好是「该阶段未出现」（`unknown`）的用例。
    """
    tags = {d(i): RANGE for i in range(span)}
    tags.update({d(offset): regime for offset, regime in (overrides or {}).items()})
    return tags


def payload(
    trades: list[sl.SliceTrade],
    tags: dict,
    span: int,
    *,
    capital: float = CAP,
    params: EvidenceConfig = SMALL,
) -> dict:
    return sl.slice_backtest(
        trades,
        tags,
        initial_capital=capital,
        window=(d(0), d(span - 1)),
        params=params,
    )


def sample(
    data: dict,
    *,
    result_id: str = "r1",
    strategy_id: int = 1,
    symbol: str = "BTC/USDT",
) -> pool.SliceSample:
    loaded = load_sample(
        data, result_id=result_id, strategy_id=strategy_id, symbol=symbol
    )
    assert loaded.sample is not None, f"样本应当可用，实际被排除：{loaded.excluded}"
    return loaded.sample


def matches_for(*pairs: tuple[int, str]) -> dict[int, pool.ArchetypeMatch]:
    return {sid: resolve_archetype(text) for sid, text in pairs}


def cell_of(
    result: pool.PoolResult, strategy_id: int, regime: BaseRegime
) -> pool.PooledCell:
    return result.cells[(strategy_id, regime.value)]


def span_with(regime: BaseRegime, days: int = 6) -> int:
    """前 `days` 天是 `regime`、其余是箱体震荡的窗口长度。"""
    return days * 2


def downtrend_profit_then_range_loss(
    *, symbol: str = "BTC/USDT", result_id: str = "r1", strategy_id: int = 1
) -> pool.SliceSample:
    """一份「箱体不适用、下行趋势适用」的样本：下行趋势三笔平稳盈利，箱体三笔连亏。

    构造的关键是**让箱体那格比全样本更差**，而不是让全样本也亏：下行趋势那三笔
    +10% 把全样本曲线的峰抬到 1.3，箱体那三笔 -5% 从这个峰上下来，回撤 15% 大于
    全样本的 11.54%，Calmar 也从正翻负——两个维度一致指向「更差」，`_fitness` 才落
    `unfit`。

    写成「分段亏、全样本也亏」是不行的：同一笔亏损落在同一个起点上，两边回撤会一样
    大，两个指标一个不差一个更差，落的是中性（方向矛盾），而不是不适用。这个区别在
    测试里很容易糊过去——`unfit` 断言会红成一个看不出原因的中性。
    """
    span = span_with(DOWNTREND)
    tags = tags_span(span, {i: DOWNTREND for i in range(6)})
    trades = [same_day(0, 1000.0), same_day(1, 1000.0), same_day(2, 1000.0)]
    trades += [same_day(n, -500.0) for n in (7, 8, 9)]
    return sample(
        payload(trades, tags, span),
        result_id=result_id,
        symbol=symbol,
        strategy_id=strategy_id,
    )


# --------------------------------------------------------------------------- #
# 加权合并
# --------------------------------------------------------------------------- #


class TestWeightedMerge(SimpleTestCase):
    """把成交合起来、按合并后的样本重算（CONTEXT.md 的「加权合并」）。"""

    def test_pooled_trades_are_the_sum_of_the_parts(self):
        span = span_with(DOWNTREND)
        tags = tags_span(span, {i: DOWNTREND for i in range(6)})
        a = sample(
            payload(
                [same_day(7, 250.0), same_day(8, 250.0), same_day(9, 250.0)], tags, span
            )
        )
        b = sample(
            payload(
                [same_day(7, 250.0), same_day(8, 250.0), same_day(9, 250.0)], tags, span
            ),
            result_id="r2",
            symbol="ETH/USDT",
        )

        result = build_pool([a, b], matches_for((1, "海龟突破")), params=SMALL)
        cell = cell_of(result, 1, RANGE)

        self.assertEqual(cell.evidence["trades"], 6)
        self.assertEqual(cell.evidence["symbols"], ["BTC/USDT", "ETH/USDT"])
        self.assertEqual(
            [r["result_id"] for r in cell.evidence["results"]], ["r1", "r2"]
        )

    def test_pooling_is_normalised_to_returns_not_absolute_amounts(self):
        """两份本金差十倍的回测，在合并里等权。

        A：本金 1 万，三天各 +250（每天 +2.5%）→ 合并后 +7.5%。
        B：本金 10 万，三天各 +250（每天 +0.25%）→ 合并后 +0.75%。

        合并后的 `total_pnl` 必须是 **2.75% + 0.75% …** 等等：这里只有两份，所以是
        `0.025×3 + 0.0025×3 = 0.0825`。若按金额合并（把两笔 250 当成同一口径相加），
        得到的会是一个被本金加权的数（0.5% 量级），与这里差一个数量级——这条用例
        就是那个数量级。
        """
        span = span_with(DOWNTREND)
        tags = tags_span(span, {i: DOWNTREND for i in range(6)})
        days = [7, 8, 9]
        a = sample(
            payload([same_day(n, 250.0) for n in days], tags, span, capital=10000.0)
        )
        b = sample(
            payload([same_day(n, 250.0) for n in days], tags, span, capital=100000.0),
            result_id="r2",
            symbol="ETH/USDT",
        )

        result = build_pool([a, b], matches_for((1, "海龟突破")), params=SMALL)
        cell = cell_of(result, 1, RANGE)

        self.assertAlmostEqual(cell.evidence["metrics"]["total_pnl"], 0.0825)
        self.assertEqual(cell.evidence["metrics"]["span_days"], 3)

    def test_the_full_sample_baseline_is_the_same_batch(self):
        """分段与全样本必须覆盖同一批样本，否则「分段比全样本差」里混进了
        「这家的全样本比那家的分段」这种不可比的比较。"""
        span = span_with(DOWNTREND)
        tags = tags_span(span, {i: DOWNTREND for i in range(6)})
        # A 只在下行趋势里交易，B 只在箱体里交易：合并后的全样本必须两天都算上。
        a = sample(payload([same_day(0, 200.0)], tags, span))
        b = sample(
            payload([same_day(7, 400.0)], tags, span), result_id="r2", symbol="ETH/USDT"
        )

        result = build_pool([a, b], matches_for((1, "海龟突破")), params=SMALL)
        cell = cell_of(result, 1, RANGE)

        # 全样本 = 0.02 + 0.04 = 0.06，跨 d0 与 d7 两天；分段（箱体）只有 d7 那天。
        self.assertAlmostEqual(cell.evidence["full_sample"]["total_pnl"], 0.06)
        self.assertEqual(cell.evidence["full_sample"]["span_days"], 8)
        self.assertAlmostEqual(cell.evidence["metrics"]["total_pnl"], 0.04)


# --------------------------------------------------------------------------- #
# 门槛
# --------------------------------------------------------------------------- #


class TestThresholdIsConjunctive(SimpleTestCase):
    """两条门槛并列，不是乘积（模块 docstring 第 2 条）。"""

    def test_trades_alone_cannot_buy_a_pass(self):
        """6 笔全挤在一月：`min_trades=3` 够了，`min_months=2` 不够。

        乘积读法（`trades × months >= 3 × 2`）会算出 `6 × 1 = 6 >= 6` 而放行——
        这条用例的存在就是为了让那个读法红。"""
        span = span_with(DOWNTREND)
        tags = tags_span(span, {i: DOWNTREND for i in range(6)})
        one_month = [same_day(7, 300.0) for _ in range(6)]
        data = sample(payload(one_month, tags, span, params=TWO_MONTHS))

        result = build_pool([data], matches_for((1, "海龟突破")), params=TWO_MONTHS)
        cell = cell_of(result, 1, RANGE)

        self.assertEqual(cell.evidence["trades"], 6)
        self.assertEqual(cell.evidence["months"], 1)
        self.assertFalse(cell.evidence["threshold"]["met"])
        self.assertEqual(cell.state, sl.STATE_NEUTRAL)
        self.assertEqual(cell.reason, sl.REASON_INSUFFICIENT)

    def test_months_across_two_calendar_months_pass(self):
        """同样的 6 笔，摊到一月与二月两个月上就过门槛——差别只在覆盖月数。"""
        span = 40  # d(0)~d(39)：跨过 d(31) 的一月/二月分界
        tags = tags_span(span)
        trades = [same_day(7, 300.0), same_day(8, 300.0), same_day(9, 300.0)]
        trades += [same_day(33, 300.0), same_day(34, 300.0), same_day(35, 300.0)]
        data = sample(payload(trades, tags, span, params=TWO_MONTHS))

        result = build_pool([data], matches_for((1, "海龟突破")), params=TWO_MONTHS)
        cell = cell_of(result, 1, RANGE)

        self.assertEqual(cell.evidence["months"], 2)
        self.assertTrue(cell.evidence["threshold"]["met"])


class TestPoolingDoesNotScaleTheThreshold(SimpleTestCase):
    """池化不接受 `day_share`（没有可做分母的窗口），所以门槛是未缩放的那一个。

    这不是「单元 6 的缩放被丢了」：缩放是每格自己的事。这条用例把**同一个格子**
    在两层里的门槛并排放，让差异可见。
    """

    def test_a_rare_stage_passes_the_slice_but_not_the_pool(self):
        # d(5) 是下行趋势（1/12 天），其余是箱体震荡。
        span = span_with(DOWNTREND)
        tags = tags_span(span, {5: DOWNTREND})
        # 箱体里三笔盈利先把曲线的峰抬起来（d0~d2），下行趋势那两笔亏损落在这个峰
        # 之后 → 下行趋势这一格在切片层被判「不适用」。
        trades = [same_day(n, 1000.0) for n in (0, 1, 2)]
        trades += [same_day(5, -500.0), same_day(5, -500.0)]
        data = payload(trades, tags, span)

        # 切片层：day_share = 1/12，门槛缩到 max(1, round(3/12)) = 1 → 2 笔即过。
        slice_cell = data["cells"][DOWNTREND.value]
        self.assertTrue(slice_cell["threshold"]["scaled"])
        self.assertEqual(slice_cell["threshold"]["min_trades"], 1)
        self.assertTrue(slice_cell["threshold"]["met"])
        self.assertEqual(slice_cell["state"], sl.STATE_UNFIT)

        # 池化层：同一格、同样的 2 笔，未缩放的门槛是 3 → 不够。
        result = build_pool([sample(data)], matches_for((1, "海龟突破")), params=SMALL)
        pooled = cell_of(result, 1, DOWNTREND)

        self.assertEqual(pooled.evidence["threshold"]["min_trades"], 3)
        self.assertFalse(pooled.evidence["threshold"]["met"])
        self.assertEqual(pooled.evidence["trades"], 2)
        self.assertEqual(pooled.state, sl.STATE_NEUTRAL)
        self.assertEqual(pooled.reason, sl.REASON_INSUFFICIENT)

    def test_the_unscaled_threshold_is_stated_in_the_evidence(self):
        """证据里写出用的是哪一套门槛，读的人不必去猜是哪一层的数字。"""
        span = span_with(DOWNTREND)
        tags = tags_span(span, {i: DOWNTREND for i in range(6)})
        data = sample(payload([same_day(7, 250.0)], tags, span))

        result = build_pool([data], matches_for((1, "海龟突破")), params=SMALL)
        threshold = cell_of(result, 1, RANGE).evidence["threshold"]

        self.assertEqual(threshold["min_trades"], 3)
        self.assertEqual(threshold["min_months"], 1)
        self.assertNotIn("day_share", threshold)


# --------------------------------------------------------------------------- #
# 判定顺序与四态
# --------------------------------------------------------------------------- #


class TestStateVocabulary(SimpleTestCase):
    """四态与保命档各有各的成因，顺序与 `slice._cell` 逐条对齐。"""

    def test_a_stage_that_never_appeared_is_unknown(self):
        span = span_with(DOWNTREND)
        tags = tags_span(span, {i: DOWNTREND for i in range(6)})
        data = sample(payload([same_day(0, 300.0)], tags, span))

        result = build_pool([data], matches_for((1, "海龟突破")), params=SMALL)

        for regime in (UPTREND, HIGH_VOL):
            with self.subTest(regime=regime.value):
                cell = cell_of(result, 1, regime)
                self.assertEqual(cell.state, sl.STATE_UNKNOWN)
                self.assertEqual(cell.reason, sl.REASON_NO_REGIME_DAYS)
                self.assertEqual(cell.evidence["regime_days"], 0)

    def test_a_stage_that_appeared_without_trades_is_insufficient_not_unknown(self):
        """「这个阶段出现了但没交易」与「这个阶段没出现」是两件事，靠 `regime_days`
        分开——聚合层的 `merge_inputs` 只有成交日，看不到前者。"""
        span = span_with(DOWNTREND)
        tags = tags_span(span, {i: DOWNTREND for i in range(6)})
        # 只在下行趋势里交易，箱体那 6 天有标签、没成交。
        data = sample(payload([same_day(0, 300.0)], tags, span))

        result = build_pool([data], matches_for((1, "海龟突破")), params=SMALL)
        cell = cell_of(result, 1, RANGE)

        self.assertEqual(cell.evidence["regime_days"], 6)
        self.assertEqual(cell.evidence["trades"], 0)
        self.assertEqual(cell.state, sl.STATE_NEUTRAL)
        self.assertEqual(cell.reason, sl.REASON_INSUFFICIENT)

    def test_high_vol_is_blanket_regardless_of_sample_size(self):
        """保命档不做适用性判断（ADR 0002）：样本再薄也是 `blanket`。

        这一档**不等门槛**：被门槛改写成中性等于说「样本够了就能判适用性」，
        而那正是保命档要否掉的话。"""
        span = span_with(HIGH_VOL)
        tags = tags_span(span, {i: HIGH_VOL for i in range(6)})
        thin = sample(payload([same_day(0, -500.0)], tags, span))
        thick = sample(
            payload([same_day(i, -500.0) for i in range(3)], tags, span),
            result_id="r2",
            symbol="ETH/USDT",
        )

        result = build_pool([thin, thick], matches_for((1, "海龟突破")), params=SMALL)
        cell = cell_of(result, 1, HIGH_VOL)

        self.assertEqual(cell.state, sl.STATE_BLANKET)
        self.assertEqual(cell.reason, sl.REASON_HIGH_VOL_BLANKET)

    def test_a_worse_stage_is_unfit(self):
        """分段两维都比全样本差 → 不适用。这是唯一会推出停用建议的状态。

        样本的构造见 `downtrend_profit_then_range_loss`。同一份样本里两个格子状态
        并存（下行趋势适用、箱体不适用），所以这两种状态确实来自比对，而不是来自
        某个默认值。"""
        result = build_pool(
            [downtrend_profit_then_range_loss()],
            matches_for((1, "海龟突破")),
            params=SMALL,
        )
        cell = cell_of(result, 1, RANGE)

        self.assertEqual(cell.evidence["trades"], 3)
        self.assertEqual(cell.state, sl.STATE_UNFIT)
        self.assertEqual(cell.reason, "")
        self.assertEqual(cell.source, POOL_SOURCE_STRATEGY)
        self.assertFalse(cell.needs_review)
        self.assertEqual(cell_of(result, 1, DOWNTREND).state, sl.STATE_FIT)


# --------------------------------------------------------------------------- #
# 冲突
# --------------------------------------------------------------------------- #


class TestConflictNeedsReview(SimpleTestCase):
    """方向相反**且**两侧都过门槛 → 待复核；结论照常产出（Q3）。"""

    def _two_symbols(self, eth_range_days: list[int]) -> pool.PoolResult:
        span = span_with(DOWNTREND)
        tags = tags_span(span, {i: DOWNTREND for i in range(6)})
        btc = [
            same_day(0, 2000.0),
            same_day(1, 2000.0),
            same_day(7, -500.0),
            same_day(8, -500.0),
            same_day(9, -500.0),
        ]
        eth = [same_day(0, -2000.0), same_day(1, -2000.0)]
        eth += [same_day(n, 500.0) for n in eth_range_days]
        return build_pool(
            [
                sample(payload(btc, tags, span, params=SMALL)),
                sample(
                    payload(eth, tags, span, params=SMALL),
                    result_id="r2",
                    symbol="ETH/USDT",
                ),
            ],
            matches_for((1, "海龟突破")),
            params=SMALL,
        )

    def test_opposite_directions_above_both_thresholds_flag_for_review(self):
        # 两个品种在箱体里净额为零 → 合并后这一格**相对比较**既不更好也不更差；
        # 而 BTC 自己那一份是「不适用」。方向相反，两侧都过门槛。
        #
        # 2026-10-22：这一格最终状态是 `neutral / not_profitable`，不是 `fit`——
        # 合并后年化恰好 0.0（净额为零 ⇒ 打平），绝对水平门要求**严格为正**。
        # 但冲突判定问的是**相对**方向，与绝对门无关：`pool._pool_cell` 显式把
        # `relative_fitness` 的结果传给 `_conflict`，所以 `needs_review` 与
        # `conflict.pooled_state` 仍然是相对比较的 `fit`。
        #
        # 这条用例同时钉住那个易犯的回归：把绝对门后的状态传给 `_conflict`，
        # `_direction("neutral")` 返回 0，冲突判定直接静默退出。
        result = self._two_symbols([7, 8, 9])
        cell = cell_of(result, 1, RANGE)

        self.assertTrue(cell.needs_review)
        self.assertEqual(cell.state, sl.STATE_NEUTRAL)
        self.assertEqual(cell.reason, sl.REASON_NOT_PROFITABLE)
        conflict = cell.evidence["conflict"]
        self.assertEqual(conflict["symbol"], "BTC/USDT")
        self.assertEqual(conflict["symbol_state"], sl.STATE_UNFIT)
        self.assertEqual(conflict["pooled_state"], sl.STATE_FIT)  # 相对比较的结论

    def test_a_side_below_the_threshold_is_noise_not_conflict(self):
        """一侧只有 2 笔（`min_trades=3`）→ 是样本噪声，不是冲突。

        CONTEXT.md 明写「分段结论与全样本方向矛盾时视为样本噪声」，而门槛就是
        噪声与信号的判据。把不过门槛的那一侧也算成冲突，待复核会变成一个没人看的
        标记——每条策略都能因为某个只跑了 3 笔的品种挂上它。"""
        result = self._two_symbols([7, 8])  # ETH 只有 2 笔箱体成交
        cell = cell_of(result, 1, RANGE)

        self.assertFalse(cell.needs_review)
        self.assertIsNone(cell.evidence["conflict"])

    def test_a_single_symbol_cannot_conflict_with_itself(self):
        """一个品种不与「自己」冲突。

        池化结论就是它自己的结论，方向必然一致，所以「跨品种冲突」这个概念在这里
        不成立。用的是**有方向**的那份样本（箱体落 `unfit`），否则这条用例可能只是
        因为「没方向就无从冲突」而通过——那是另一条规则，测不到这一条。"""
        result = build_pool(
            [downtrend_profit_then_range_loss()],
            matches_for((1, "海龟突破")),
            params=SMALL,
        )
        cell = cell_of(result, 1, RANGE)

        self.assertEqual(cell.state, sl.STATE_UNFIT)
        self.assertFalse(cell.needs_review)
        self.assertIsNone(cell.evidence["conflict"])


# --------------------------------------------------------------------------- #
# 原型兜底
# --------------------------------------------------------------------------- #


class TestArchetypeNormalisation(SimpleTestCase):
    """关键词映射表 + 覆盖表，不用 LLM（CONTEXT.md）。"""

    def test_the_real_strategy_description_lands_in_mean_reversion(self):
        """现网唯一真实策略的原文：`strategies/rsi_cross.py`。

        它同时命中「金叉/死叉」（趋势跟踪）与「超卖/超买」（均值回归）——这是映射表
        **有序**的原因，也是这条用例存在的理由：顺序一旦被调换，这条策略会静默地
        换一个原型，而池化兜底引用的样本也跟着换一批。
        """
        self.assertEqual(
            normalize_archetype("RSI 超卖区金叉买入，超买区死叉卖出"),
            ("均值回归", "超卖"),
        )

    def test_all_five_archetypes_are_reachable(self):
        cases = {
            "资金费率套利": "套利",
            "布林带均值回归": "均值回归",
            "海龟突破新高": "突破",
            "动量加速": "动量",
            "双均线趋势跟踪": "趋势跟踪",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                archetype, matched = normalize_archetype(text)
                self.assertEqual(archetype, expected)
                self.assertNotEqual(matched, "")

    def test_bare_ma_does_not_match_inside_other_words(self):
        """映射表里的片段不能被无关词包含：`market` 里没有「均线」。"""
        self.assertEqual(
            normalize_archetype("market making"), (ARCHETYPE_UNCLASSIFIED, "")
        )

    def test_unclassified_carries_an_explicit_value_plus_no_matched_word(self):
        for text in ("随便写点什么", "", None):
            with self.subTest(text=text):
                self.assertEqual(
                    normalize_archetype(text), (ARCHETYPE_UNCLASSIFIED, "")
                )

    def test_the_override_table_wins_and_is_traceable(self):
        match = resolve_archetype("RSI 超卖区金叉买入", override="网格")
        self.assertEqual(match.archetype, "网格")
        self.assertEqual(match.source, ARCHETYPE_SOURCE_OVERRIDE)
        self.assertEqual(match.matched_word, "")  # 覆盖表没有走映射表
        self.assertEqual(match.raw_text, "RSI 超卖区金叉买入")


class TestDeclarationLineWins(SimpleTestCase):
    """`策略类型：` 那一行是作者写下的判断，其余行是背景描述。

    整篇扫子串会把**不声明类型的地方**当成声明。现网实测 18 条有描述的策略里 5 条
    归错，方向一致地错向「均值回归」——因为「震荡」这个词在各种「不适合震荡」
    「震荡后启动」的句子里到处都是，于是均值回归组变成所有组的垃圾桶
    （实测 10 条 / 755 份样本，四组里最胖）。

    每条断言都用**现网真实 description**，不是编的字符串：这些句子是从
    `~/.tradelogx/strategies/` 与 `backend/strategies/` 里逐字抄来的。
    """

    def test_a_negated_scenario_does_not_override_the_declaration(self):
        """「不适合震荡盘整」里的「震荡」不是类型声明。"""
        desc = (
            "策略类型：趋势跟踪\n"
            "核心指标：EMA\n"
            "适用场景：趋势行情，不适合震荡盘整\n"
            "入场逻辑：快线上穿慢线时买入"
        )
        self.assertEqual(normalize_archetype(desc), ("趋势跟踪", "趋势"))

    def test_a_background_phrase_does_not_override_the_declaration(self):
        """「震荡后启动的突破行情」里的「震荡」也是背景，不是类型。"""
        desc = (
            "策略类型：突破\n"
            "核心指标：箱体\n"
            "适用场景：震荡后启动的突破行情\n"
            "入场逻辑：收盘价突破箱体上沿"
        )
        self.assertEqual(normalize_archetype(desc), ("突破", "突破"))

    def test_an_exit_rule_word_does_not_override_the_declaration(self):
        """「Tick Imbalance 回归中性」是出场逻辑，不是「均值回归」这个类型。"""
        desc = (
            "策略类型：Tick失衡动量\n"
            "核心指标：Tick Imbalance\n"
            "适用场景：5m 高流动性\n"
            "入场逻辑：Tick Imbalance 超过阈值时跟随方向\n"
            "出场逻辑：Tick Imbalance 回归中性或 ATR 止损"
        )
        self.assertEqual(normalize_archetype(desc), ("动量", "动量"))

    def test_a_market_making_declaration_is_read_as_its_own_type(self):
        """「做市+动量」，适用场景里的「震荡」不该把它变成均值回归。"""
        desc = (
            "策略类型：做市+动量\n"
            "核心指标：ATR, EMA\n"
            "适用场景：高流动性震荡市场，不适合极端趋势行情\n"
            "入场逻辑：基于 Avellaneda-Stoikov 模型计算保留价格"
        )
        self.assertEqual(normalize_archetype(desc), ("动量", "动量"))

    def test_a_declaration_without_any_keyword_does_not_fall_back_to_the_body(self):
        """声明行一个关键词都没有时**不退回整篇**：那会拿背景描述覆盖作者的声明。

        这条钉的是「不退回」这个决定本身。模型里 `ARCHETYPE_UNCLASSIFIED` 是**如实**
        的结论（要计数、要进日报第④段），而不是一个需要用背景文本去猜的缺口。
        """
        desc = "策略类型：自定义形态\n核心指标：无\n适用场景：震荡行情\n入场逻辑：无"
        self.assertEqual(normalize_archetype(desc), (ARCHETYPE_UNCLASSIFIED, ""))

    def test_without_a_declaration_line_the_whole_text_is_still_scanned(self):
        """没有声明行的历史文本仍按整篇扫——`rsi_cross.py` 是那条设计的真实依据。

        它是**有序映射**（均值回归排在趋势跟踪前）唯一被真实数据要求的场合：
        同时命中「超卖/超买」与「金叉/死叉」。改成行扫描不能把它判成未归类。
        """
        self.assertEqual(
            normalize_archetype("RSI 超卖区金叉买入，超买区死叉卖出"),
            ("均值回归", "超卖"),
        )

    def test_the_prefix_matches_the_registry_template(self):
        """扫描用的前缀必须与注册期四字段模板里的那个字段**逐字相同**。

        两边若漂开（有人把模板改成「策略类别：」），行扫描会对所有策略失配，
        然后**静默**退回整篇——也就是悄悄退回这次修掉的那个 bug。
        """
        from apps.strategy_engine.base import BaseStrategy

        self.assertIn("策略类型：", BaseStrategy.description_template)
        self.assertIn("策略类型：", ARCHETYPE_DECLARATION_PREFIX)

    def test_a_second_declaration_line_does_not_win(self):
        """只认**第一个**声明行：模板把它排在最前，后面再出现的不是声明位置。"""
        desc = (
            "策略类型：突破\n"
            "核心指标：箱体\n"
            "适用场景：无\n"
            "入场逻辑：无\n"
            "策略类型：均值回归（这是注释里引用的模板，不是声明）"
        )
        self.assertEqual(normalize_archetype(desc), ("突破", "突破"))

    def test_the_keyword_path_says_so(self):
        match = resolve_archetype("海龟突破")
        self.assertEqual(match.source, ARCHETYPE_SOURCE_KEYWORD)
        self.assertEqual(match.matched_word, "突破")

    def test_an_unclassified_strategy_is_marked_as_such(self):
        self.assertEqual(
            resolve_archetype("随便写点什么").source, ARCHETYPE_SOURCE_UNCLASSIFIED
        )


class TestArchetypeFallback(SimpleTestCase):
    """样本不足时改用「原型 × 阶段」的样本，并标记来源（Q4/Q5）。"""

    def _pool(self) -> pool.PoolResult:
        span = span_with(DOWNTREND)
        tags = tags_span(span, {i: DOWNTREND for i in range(6)})
        # s1：均值回归，只 1 笔 → 自己不够。
        s1 = sample(
            payload([same_day(7, -500.0)], tags, span),
            strategy_id=1,
            symbol="BTC/USDT",
        )
        # s2：同原型，3 笔 → 两家合起来 4 笔。
        s2 = sample(
            payload(
                [same_day(7, 200.0), same_day(8, 200.0), same_day(9, 200.0)], tags, span
            ),
            result_id="r2",
            strategy_id=2,
            symbol="ETH/USDT",
        )
        # s3：**另一个原型**，样本很厚——它不该被算进 s1 的兜底样本。
        s3 = sample(
            payload(
                [same_day(10, 900.0), same_day(11, 900.0), same_day(10, 900.0)],
                tags,
                span,
            ),
            result_id="r3",
            strategy_id=3,
            symbol="SOL/USDT",
        )
        return build_pool(
            [s1, s2, s3],
            matches_for((1, "RSI 超卖反转"), (2, "布林带均值回归"), (3, "海龟突破")),
            params=SMALL,
        )

    def test_a_thin_strategy_borrows_its_archetype_samples(self):
        """兜底那一格借同原型的样本，但**借来的样本不出「适用」结论**（2026-10-22）。

        s1 与 s2 的成交全部落在箱体里，所以合并后的箱体分段与合并后的全样本是同一批
        日子、同一条曲线——两个指标都不更差。旧行为在这里落 `fit`，而这个测试自己的
        docstring 已经写出了它的毛病：「借用来的样本自己与自己比，出不了别的结论」。

        那不是证据，是构造方式的副产品。现网实测过它的后果：一条自己只有 0 笔样本的
        策略，因为借了均值回归组（344 笔）而判 `range / fit`；换一代借到「未归类」组
        （11693 笔）就判 `unfit`。同一条策略、同样稀薄的证据，换个组从不适用翻成适用。

        所以兜底的 `fit` 一律降成中性并写明 `archetype_evidence`：`fit` 是会被拿去做
        放行依据的值，而这条策略自己没有任何证据支撑它。`unfit` 保持原样——那个方向
        是安全的（见 `pool._pool_cell` 的兜底分支）。
        """
        result = self._pool()
        cell = cell_of(result, 1, RANGE)

        self.assertEqual(cell.source, POOL_SOURCE_ARCHETYPE)
        self.assertEqual(cell.state, sl.STATE_NEUTRAL)
        self.assertEqual(cell.reason, sl.REASON_ARCHETYPE_NOT_FIT)
        self.assertEqual(cell.evidence["archetype"]["name"], "均值回归")
        self.assertEqual(cell.evidence["archetype"]["matched_word"], "超卖")
        self.assertEqual(cell.evidence["archetype"]["source"], ARCHETYPE_SOURCE_KEYWORD)

    def test_the_fallback_sample_excludes_other_archetypes(self):
        """兜底引用的是同原型的样本，且**含自己**：自己那几笔也是这类策略的真实观测。"""
        result = self._pool()
        cell = cell_of(result, 1, RANGE)

        self.assertEqual(cell.evidence["trades"], 4)  # 1 + 3，不含 s3 的 3 笔
        self.assertEqual(cell.evidence["symbols"], ["BTC/USDT", "ETH/USDT"])
        self.assertEqual(cell.evidence["own_sample"]["trades"], 1)

    def test_the_rejected_own_sample_stays_readable(self):
        """「本策略自己为什么不够格」必须能在同一条记录里读到。"""
        result = self._pool()
        own = cell_of(result, 1, RANGE).evidence["own_sample"]

        self.assertEqual(own["trades"], 1)
        self.assertEqual(own["months"], 1)
        self.assertEqual([r["result_id"] for r in own["results"]], ["r1"])

    def test_a_strategy_of_a_healthy_archetype_does_not_borrow(self):
        result = self._pool()
        cell = cell_of(result, 3, RANGE)

        self.assertEqual(cell.source, POOL_SOURCE_STRATEGY)
        self.assertNotIn("archetype", cell.evidence)

    def test_unclassified_strategies_are_counted_not_swallowed(self):
        """未归类要计数并进日报第④段（Q5）——它是一条结论，不是噪声。"""
        span = span_with(DOWNTREND)
        tags = tags_span(span, {i: DOWNTREND for i in range(6)})
        data = sample(
            payload([same_day(7, 300.0)], tags, span), strategy_id=7, symbol="BTC/USDT"
        )

        result = build_pool([data], matches_for((7, "随便写点什么")), params=SMALL)
        cell = cell_of(result, 7, RANGE)

        self.assertEqual(result.unclassified, {7})
        self.assertEqual(result.unclassified_strategy_ids, (7,))
        self.assertEqual(cell.source, POOL_SOURCE_ARCHETYPE)
        self.assertEqual(cell.evidence["archetype"]["name"], ARCHETYPE_UNCLASSIFIED)
        self.assertEqual(
            cell.evidence["archetype"]["source"], ARCHETYPE_SOURCE_UNCLASSIFIED
        )
        # 原型也样本不足 → 照写「中性 + 证据不足」，不引入第三层（Q4）。
        self.assertEqual(cell.state, sl.STATE_NEUTRAL)
        self.assertEqual(cell.reason, sl.REASON_INSUFFICIENT)

    def test_the_fallback_does_not_borrow_the_blanket_state(self):
        """兜底这一格不产生待复核：它的争议不是「有争议」，是「没证据」。"""
        span = span_with(DOWNTREND)
        tags = tags_span(span, {i: DOWNTREND for i in range(6)})
        data = sample(payload([same_day(7, -500.0)], tags, span), strategy_id=7)

        result = build_pool([data], matches_for((7, "随便写点什么")), params=SMALL)

        for regime in PRIORITY:
            with self.subTest(regime=regime.value):
                self.assertFalse(cell_of(result, 7, regime).needs_review)


# --------------------------------------------------------------------------- #
# 载荷的三种不可用
# --------------------------------------------------------------------------- #


class TestLoadSampleExclusions(SimpleTestCase):
    """「没有载荷」「版本 1 的载荷」「本金 ≤ 0」是三个不同的原因，不共用一个数。"""

    def test_a_missing_payload_is_its_own_reason(self):
        for empty in (None, {}, {"cells": {}}):
            with self.subTest(payload=empty):
                loaded = load_sample(
                    empty, result_id="r1", strategy_id=1, symbol="BTC/USDT"
                )
                self.assertIsNone(loaded.sample)
                self.assertEqual(loaded.excluded, EXCLUDED_NO_PAYLOAD)

    def test_a_version_one_payload_is_its_own_reason(self):
        """版本 1 没有 `merge_inputs` 这一节：跑一次重算入口就好，与本金为 0 不同。"""
        span = span_with(DOWNTREND)
        tags = tags_span(span, {i: DOWNTREND for i in range(6)})
        data = payload([same_day(7, 300.0)], tags, span)
        for cell in data["cells"].values():
            del cell["merge_inputs"]

        loaded = load_sample(data, result_id="r1", strategy_id=1, symbol="BTC/USDT")

        self.assertIsNone(loaded.sample)
        self.assertEqual(loaded.excluded, EXCLUDED_NO_MERGE_INPUTS)

    def test_an_unusable_capital_is_its_own_reason(self):
        span = span_with(DOWNTREND)
        tags = tags_span(span, {i: DOWNTREND for i in range(6)})
        data = payload([same_day(7, 300.0)], tags, span, capital=0.0)

        loaded = load_sample(data, result_id="r1", strategy_id=1, symbol="BTC/USDT")

        self.assertIsNone(loaded.sample)
        self.assertEqual(loaded.excluded, EXCLUDED_UNUSABLE_CAPITAL)

    def test_one_broken_cell_excludes_the_whole_slice(self):
        """整份可用或整份不用：半份样本合并出来的轨迹不属于任何东西，而它会带着
        一个看起来完整的笔数进证据。

        两种「坏」走两条不同的原因：「这一格没有可合并的样本」（`None`）与「这一格
        的样本形状不对」（少了 `trades` 键）。后者尤其不能按缺省空值读下去——那会得到
        一份「这一格没有交易」的样本，笔数与胜率都算得出来，看起来完全正常。
        """
        span = span_with(DOWNTREND)
        tags = tags_span(span, {i: DOWNTREND for i in range(6)})

        nulled = payload([same_day(7, 300.0)], tags, span)
        nulled["cells"][HIGH_VOL.value]["merge_inputs"] = None
        self.assertEqual(
            load_sample(
                nulled, result_id="r1", strategy_id=1, symbol="BTC/USDT"
            ).excluded,
            EXCLUDED_UNUSABLE_CAPITAL,
        )

        truncated = payload([same_day(7, 300.0)], tags, span)
        del truncated["cells"][HIGH_VOL.value]["merge_inputs"]["trades"]
        self.assertEqual(
            load_sample(
                truncated, result_id="r1", strategy_id=1, symbol="BTC/USDT"
            ).excluded,
            EXCLUDED_NO_MERGE_INPUTS,
        )

    def test_a_usable_payload_round_trips(self):
        span = span_with(DOWNTREND)
        tags = tags_span(span, {i: DOWNTREND for i in range(6)})
        data = payload([same_day(7, 300.0)], tags, span)

        loaded = load_sample(data, result_id="r9", strategy_id=4, symbol="BTC/USDT")

        self.assertEqual(loaded.excluded, "")
        self.assertIsNotNone(loaded.sample)
        self.assertEqual(loaded.sample.result_id, "r9")
        self.assertEqual(loaded.sample.strategy_id, 4)
        self.assertEqual(loaded.sample.window, (d(0), d(11)))
        # 日收益的键必须是 `date`：`_segment_metrics` 要拿它做日期减法。
        for regime in PRIORITY:
            for day in loaded.sample.cell_daily[regime.value]:
                self.assertIsInstance(day, date)


# --------------------------------------------------------------------------- #
# 词表是落库契约
# --------------------------------------------------------------------------- #


class TestVocabularyMatchesTheModel(SimpleTestCase):
    """`state`/`reason` 在模型上是裸 `CharField`（模型的 import 链不能碰 `slice`，
    见 `RegimePoolCell` 的 docstring），所以「两处同集合」由这里钉住。"""

    #: 池化可能产出的全部状态与原因。前三个直接来自本模块的常量，后两个由
    #: `slice._fitness` 返回——池化复用它，所以它也在池化的取值范围内。
    POOL_STATES = (
        sl.STATE_FIT,
        sl.STATE_UNFIT,
        sl.STATE_NEUTRAL,
        sl.STATE_UNKNOWN,
        sl.STATE_BLANKET,
    )
    POOL_REASONS = (
        sl.REASON_INSUFFICIENT,
        sl.REASON_DIRECTION_CONFLICT,
        sl.REASON_INCOMPARABLE,
        sl.REASON_HIGH_VOL_BLANKET,
        sl.REASON_NO_REGIME_DAYS,
        sl.REASON_NOT_PROFITABLE,
        sl.REASON_ARCHETYPE_NOT_FIT,
    )

    def test_states_and_reasons_are_exactly_the_displayed_vocabulary(self):
        self.assertEqual(set(self.POOL_STATES), set(sl.STATE_DISPLAY))
        self.assertEqual(set(self.POOL_REASONS), set(sl.REASON_DISPLAY))

    def test_the_columns_deliberately_have_no_choices(self):
        """加 `choices` 需要先把整份词表搬过来；没搬之前这里必须一直是空的，
        否则「词表在哪」就有两个答案，而落库的取值只认其中一个。"""
        state_field = RegimePoolCell._meta.get_field("state")
        reason_field = RegimePoolCell._meta.get_field("reason")
        self.assertFalse(state_field.choices)
        self.assertFalse(reason_field.choices)

    def test_pool_source_values_are_the_model_values(self):
        """模型那一列存的是 `PoolSource` 的取值，池化写进去的也必须是同一个集合。

        比的是**模型的 `choices` 列出来的取值**，不是 `PoolSource` 成员本身：前者才是
        「这一列允许落什么」，后者只是它的来源。
        """
        field_values = {
            value for value, _ in RegimePoolCell._meta.get_field("source").choices
        }
        self.assertEqual(field_values, {member.value for member in PoolSource})
        self.assertEqual(field_values, {POOL_SOURCE_STRATEGY, POOL_SOURCE_ARCHETYPE})

    def test_every_emitted_cell_stays_inside_the_vocabulary(self):
        """行为面：真跑一遍，产出的每一格都在词表里。上面那条是静态的集合相等，
        这条挡住「新加一个状态却忘了进词表」。"""
        span = span_with(DOWNTREND)
        tags = tags_span(span, {i: DOWNTREND for i in range(6)})
        trades = [same_day(0, -500.0) for _ in range(3)]
        trades += [same_day(7, 2000.0) for _ in range(3)]
        data = sample(payload(trades, tags, span))

        result = build_pool([data], matches_for((1, "海龟突破")), params=SMALL)

        self.assertTrue(result.cells)
        for key, cell in result.cells.items():
            with self.subTest(cell=key):
                self.assertIn(cell.state, sl.STATE_DISPLAY)
                self.assertTrue(cell.reason == "" or cell.reason in sl.REASON_DISPLAY)
                self.assertIn(
                    cell.source, (POOL_SOURCE_STRATEGY, POOL_SOURCE_ARCHETYPE)
                )


class TestCoverage(SimpleTestCase):
    """每个策略的每一格都产出——缺格在日报里表现为「这一行没有」，而它有好几种意思。"""

    def test_every_strategy_gets_all_four_cells(self):
        span = span_with(DOWNTREND)
        tags = tags_span(span, {i: DOWNTREND for i in range(6)})
        data = sample(payload([same_day(7, 300.0)], tags, span))

        result = build_pool([data], matches_for((1, "海龟突破")), params=SMALL)

        self.assertEqual(len(result.cells), len(PRIORITY))
        for regime in PRIORITY:
            self.assertIn((1, regime.value), result.cells)

    def test_an_empty_sample_still_says_why(self):
        span = span_with(DOWNTREND)
        tags = tags_span(span, {i: DOWNTREND for i in range(6)})
        data = sample(payload([], tags, span))

        result = build_pool([data], matches_for((1, "海龟突破")), params=SMALL)
        cell = cell_of(result, 1, RANGE)

        self.assertEqual(cell.evidence["trades"], 0)
        self.assertEqual(cell.state, sl.STATE_NEUTRAL)
        self.assertEqual(cell.reason, sl.REASON_INSUFFICIENT)
