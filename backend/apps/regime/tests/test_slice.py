"""历史切片（第①段单元 6i）。

这个文件要钉住的不是「某段行情算出来 Calmar 等于几」，而是六条性质：

1. **归属只认开仓时刻，而 PnL 按持仓时长分摊**——两处刻意的不对称（CONTEXT.md）。
   一笔跨阶段的交易必须同时：在某格计 1 笔、在另一格贡献 PnL。写成「整笔都进开仓段」
   或「整笔都进平仓段」都能让数字看起来正常，而结论会错得毫无痕迹。
2. **归不到任何阶段的钱要被记账**，不能静默消失，也不能重分配。「这个阶段没有交易」
   与「这个阶段的交易全都没标签」在结果里必须长得不一样。
3. **「日期」是 UTC 自然日**，与日线标签的键同口径。取业务日会差一天，而差的方向
   恰好把凌晨的开仓归到一根尚未开盘的日线上。
4. **门槛是刹车**：稀有阶段按天数占比缩放笔数门槛，但 `min_months` 不缩放，所以
   缩放在任何情况下都不能让一个样本稀薄的格子「过门槛」。
5. **高波动档不做适用性判断**（`blanket`），且**门槛在这一档失效**——它与证据无关。
6. **四态各有各的成因**：`unknown`（该阶段没出现过）/ `neutral`（证据不足或方向矛盾）
   / `fit` / `unfit`。缺一格会在日报里变成「这一行没有」，而它有好几种意思。

用例里的门槛是**刻意缩小**的（`min_trades=3, min_months=1`）：默认的 30 笔 / 3 个月
要造几百天数据才出结论，断言只会被埋进噪声里。缩小的只是规模，规则一模一样；默认值
本身由 `test_config` 钉住。

**判定表（`_fitness`）直接测**：它是纯函数、参数是两本字典，构造出来比走一遍
`slice_backtest` 清楚得多。端到端只覆盖「表以外的部分确实接上了」——适用、不适用、
门槛拦下、高波动、未知各一条。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from django.test import SimpleTestCase

from apps.regime import slice as sl
from apps.regime.config import EVIDENCE, JUDGEMENT, EvidenceConfig
from apps.regime.quant import BaseRegime, label_series, latest_label

HIGH_VOL = BaseRegime.HIGH_VOL
DOWNTREND = BaseRegime.DOWNTREND
UPTREND = BaseRegime.UPTREND
RANGE = BaseRegime.RANGE

#: 缩小后的证据门槛。见模块 docstring。
SMALL = EvidenceConfig(min_trades=3, min_months=1)

#: 只缩笔数、保留 `min_months=3` 的门槛。用来单独测「覆盖月数」这条规则。
STRICT = EvidenceConfig(min_trades=3, min_months=3)

#: 2026-01-01。2026 不是闰年，所以 day 0/31/59 正好是 1 月 1 日 / 2 月 1 日 / 3 月 1 日。
DAY0 = date(2026, 1, 1)
CAP = 10000.0

#: 小窗口用的日线标签：前 6 天（1/1~1/6）下行趋势，后 6 天（1/7~1/12）箱体震荡。
#: 12 天且对半分，所以每档的 `day_share` 是 0.5、缩放后门槛是 round(3×0.5)=2。
SPLIT_WINDOW = (DAY0, DAY0 + timedelta(days=11))


def d(n: int) -> date:
    return DAY0 + timedelta(days=n)


def at(n: int, hour: int = 12, tz=timezone.utc) -> datetime:
    return datetime(2026, 1, 1, hour, tzinfo=tz) + timedelta(days=n)


def tags_of(mapping: dict[int, BaseRegime]) -> dict[date, BaseRegime]:
    return {d(offset): regime for offset, regime in mapping.items()}


def split_tags() -> dict[date, BaseRegime]:
    """`SPLIT_WINDOW` 的标签：`d0~d5` 下行趋势，`d6~d11` 箱体震荡。"""
    return tags_of(
        {i: DOWNTREND for i in range(0, 6)} | {i: RANGE for i in range(6, 12)}
    )


def trade(entry_offset: int, exit_offset: int, pnl: float, **kwargs) -> sl.SliceTrade:
    return sl.SliceTrade(
        entry_time=at(entry_offset, **kwargs),
        exit_time=at(exit_offset, **kwargs),
        pnl=pnl,
    )


def same_day_trades(offsets, pnl: float) -> list[sl.SliceTrade]:
    """每个偏移一笔「当日开平」的交易。PnL 于是整笔落在那一天，数字可直接手算。"""
    return [trade(offset, offset, pnl) for offset in offsets]


# --------------------------------------------------------------------------- #
# 归属
# --------------------------------------------------------------------------- #


class TestAttributionByEntryTime(SimpleTestCase):
    """归属只认开仓时刻；PnL 按持仓日分摊。两处不对称是刻意设计（模块 docstring）。"""

    def test_a_same_day_trade_lands_entirely_in_its_regime(self):
        out = sl.slice_backtest(
            same_day_trades([7], 250.0),
            split_tags(),
            initial_capital=CAP,
            window=SPLIT_WINDOW,
            params=SMALL,
        )
        cell = out["cells"]["range"]
        self.assertEqual(cell["trades"], 1)
        self.assertAlmostEqual(cell["total_pnl"], 250.0)
        self.assertEqual(out["cells"]["downtrend"]["trades"], 0)
        self.assertAlmostEqual(out["cells"]["downtrend"]["total_pnl"], 0.0)

    def test_a_cross_regime_trade_splits_pnl_but_counts_once_at_the_entry(self):
        """**本单元的核心规则**：钱按天分，笔数只归开仓段。"""
        out = sl.slice_backtest(
            [trade(5, 7, 300.0)],  # 开在 d5（下行趋势）→ 平在 d7（箱体震荡）
            split_tags(),
            initial_capital=CAP,
            window=SPLIT_WINDOW,
            params=SMALL,
        )
        down, rng = out["cells"]["downtrend"], out["cells"]["range"]

        # 3 个自然日（d5/d6/d7）→ 每天 100
        self.assertAlmostEqual(down["total_pnl"], 100.0)
        self.assertAlmostEqual(rng["total_pnl"], 200.0)

        # 笔数只算 1 笔，且只算在开仓段
        self.assertEqual(down["trades"], 1)
        self.assertEqual(rng["trades"], 0)

    def test_the_exit_regime_gets_the_money_but_never_the_count(self):
        """对照面：给了钱的格子**仍然**一笔都不计。混起来就是「在哪开仓」被判成了
        「在哪赚钱」，而那正是这套机制要不回答的问题。"""
        out = sl.slice_backtest(
            [trade(5, 7, 300.0)],
            split_tags(),
            initial_capital=CAP,
            window=SPLIT_WINDOW,
            params=SMALL,
        )
        rng = out["cells"]["range"]
        self.assertGreater(rng["total_pnl"], 0.0)
        self.assertEqual(rng["trades"], 0)
        # 一笔都不算 ⇒ 胜率无从谈起。报 0.0 会读成「全亏」。
        self.assertIsNone(rng["win_rate"])

    def test_the_count_regime_keeps_the_whole_trade_in_its_win_rate(self):
        """胜率与单笔均值走**整笔**口径（交易集合的统计量），不是分摊份额。"""
        out = sl.slice_backtest(
            [trade(5, 7, 300.0)],
            split_tags(),
            initial_capital=CAP,
            window=SPLIT_WINDOW,
            params=SMALL,
        )
        down = out["cells"]["downtrend"]
        self.assertEqual(down["trades"], 1)
        self.assertAlmostEqual(down["win_rate"], 1.0)

    def test_pnl_is_conserved_across_the_cells(self):
        trades = same_day_trades([1, 3, 7, 9], 100.0) + [trade(5, 7, 300.0)]
        out = sl.slice_backtest(
            trades, split_tags(), initial_capital=CAP, window=SPLIT_WINDOW, params=SMALL
        )
        total = sum(cell["total_pnl"] for cell in out["cells"].values())
        self.assertAlmostEqual(total + out["attribution"]["pnl_unattributed"], 700.0)

    def test_a_zero_duration_trade_is_one_day(self):
        out = sl.slice_backtest(
            [trade(7, 7, 50.0)],
            split_tags(),
            initial_capital=CAP,
            window=SPLIT_WINDOW,
            params=SMALL,
        )
        self.assertAlmostEqual(out["cells"]["range"]["total_pnl"], 50.0)
        self.assertEqual(out["cells"]["range"]["span_days"], 1)

    def test_an_inverted_window_is_rejected(self):
        with self.assertRaises(ValueError):
            sl.slice_backtest(
                [],
                split_tags(),
                initial_capital=CAP,
                window=(d(11), d(0)),
                params=SMALL,
            )


class TestUnattributableSharesAreAccounted(SimpleTestCase):
    """归不到阶段的钱要记账。静默丢弃与「这个阶段没有交易」在结果里长得一模一样。"""

    def _holed_tags(self):
        """`split_tags` 但挖掉 d6、d8 两天（模拟日线缺日或预热不够）。"""
        return {
            day: regime
            for day, regime in split_tags().items()
            if day not in (d(6), d(8))
        }

    def test_a_share_landing_on_an_unjudged_day_is_reported_not_dropped(self):
        out = sl.slice_backtest(
            [trade(5, 7, 300.0)],  # d5(下行) / d6(缺) / d7(箱体)
            self._holed_tags(),
            initial_capital=CAP,
            window=SPLIT_WINDOW,
            params=SMALL,
        )
        self.assertAlmostEqual(out["attribution"]["pnl_unattributed"], 100.0)
        self.assertAlmostEqual(out["cells"]["downtrend"]["total_pnl"], 100.0)
        self.assertAlmostEqual(out["cells"]["range"]["total_pnl"], 100.0)

    def test_unattributable_shares_are_never_redistributed(self):
        """落空的份额不许摊给别的阶段——那会让每一格都说「我这里有这些钱」。"""
        out = sl.slice_backtest(
            [trade(5, 7, 300.0)],
            self._holed_tags(),
            initial_capital=CAP,
            window=SPLIT_WINDOW,
            params=SMALL,
        )
        attributed = sum(cell["total_pnl"] for cell in out["cells"].values())
        self.assertAlmostEqual(attributed, 200.0)
        self.assertAlmostEqual(
            attributed + out["attribution"]["pnl_unattributed"], 300.0
        )

    def test_a_trade_entering_on_an_unjudged_day_counts_nowhere(self):
        """开仓那一刻说不出行情 ⇒ 这笔交易不足以支撑任何一格的结论，**一笔都不计**。
        但它的钱照分——这就是「求准 / 求保守」不对称的落点。"""
        out = sl.slice_backtest(
            [trade(6, 8, 300.0)],  # d6(缺) / d7(箱体) / d8(缺)
            self._holed_tags(),
            initial_capital=CAP,
            window=SPLIT_WINDOW,
            params=SMALL,
        )
        rng = out["cells"]["range"]
        self.assertAlmostEqual(rng["total_pnl"], 100.0)
        self.assertEqual(rng["trades"], 0)
        self.assertEqual(out["attribution"]["unjudged"], 1)
        self.assertEqual(out["attribution"]["attributed"], 0)

    def test_every_closed_trade_is_either_attributed_or_unjudged(self):
        trades = [trade(1, 1, 10.0), trade(6, 8, 30.0), trade(9, 9, 20.0)]
        out = sl.slice_backtest(
            trades,
            self._holed_tags(),
            initial_capital=CAP,
            window=SPLIT_WINDOW,
            params=SMALL,
        )
        attribution = out["attribution"]
        self.assertEqual(attribution["attributed"] + attribution["unjudged"], 3)
        self.assertEqual(attribution["closed_trades"], 3)

    def test_the_adapter_is_expected_to_fill_in_the_excluded_open_trades(self):
        """本函数看不到未平仓的行（没有 exit_time / pnl），所以这个计数留给适配层。
        `None` 是**待填**，不是 0——写成 0 就等于宣称「没有未平仓交易」。"""
        out = sl.slice_backtest(
            [], split_tags(), initial_capital=CAP, window=SPLIT_WINDOW, params=SMALL
        )
        self.assertIsNone(out["attribution"]["open_trades_excluded"])


class TestTheDayIsUtcNotBusinessDay(SimpleTestCase):
    """标签的键是 UTC 开盘日，归属必须同口径。"""

    def test_an_entry_just_after_beijing_midnight_belongs_to_the_previous_utc_day(self):
        """北京 01-05 02:00 = UTC 01-04 18:00 ⇒ 落在 d3（1/4）那根日线上。

        按业务日读会归到 1/5 = d4，而 d4 那根日线要到 1/5 00:00 UTC 才开盘——
        即在交易发生之后。差一天，而且方向恰好是错的。
        """
        cst = timezone(timedelta(hours=8))
        out = sl.slice_backtest(
            [
                sl.SliceTrade(
                    entry_time=datetime(2026, 1, 5, 2, 0, tzinfo=cst),
                    exit_time=datetime(2026, 1, 5, 12, 0, tzinfo=cst),
                    pnl=300.0,
                )
            ],
            tags_of({3: RANGE, 4: DOWNTREND}),
            initial_capital=CAP,
            window=(d(3), d(4)),
            params=SMALL,
        )
        self.assertEqual(out["cells"]["range"]["trades"], 1)
        self.assertEqual(out["cells"]["downtrend"]["trades"], 0)
        self.assertAlmostEqual(out["cells"]["range"]["total_pnl"], 150.0)
        self.assertAlmostEqual(out["cells"]["downtrend"]["total_pnl"], 150.0)

    def test_utc_midnight_is_the_boundary(self):
        tags = tags_of({2: RANGE, 3: DOWNTREND})
        before = sl.slice_backtest(
            [trade(3, 3, 10.0, hour=0)],
            tags,
            initial_capital=CAP,
            window=(d(2), d(3)),
            params=SMALL,
        )
        # datetime(2026,1,1,0) + 3 天 = 2026-01-04 00:00 UTC ⇒ d3
        self.assertEqual(before["cells"]["downtrend"]["trades"], 1)

        after = sl.slice_backtest(
            [sl.SliceTrade(at(3, 0) - timedelta(minutes=1), at(3, 0), 10.0)],
            tags,
            initial_capital=CAP,
            window=(d(2), d(3)),
            params=SMALL,
        )
        self.assertEqual(after["cells"]["range"]["trades"], 1)


# --------------------------------------------------------------------------- #
# 分段指标
# --------------------------------------------------------------------------- #


class TestSegmentMetrics(SimpleTestCase):
    def test_drawdown_is_measured_from_the_running_peak(self):
        out = sl._segment_metrics({d(0): 100.0, d(1): -300.0, d(2): 50.0}, 1000.0)
        # 权益 1100 → 800 → 850，峰值 1100，最大回撤 (1100−800)/1100 = 300/1100
        self.assertAlmostEqual(out["max_drawdown_pct"], 300.0 / 11.0, places=6)
        self.assertAlmostEqual(out["total_pnl"], -150.0)
        self.assertEqual(out["span_days"], 3)

    def test_an_untouched_peak_has_no_drawdown_and_therefore_no_calmar(self):
        out = sl._segment_metrics({d(0): 100.0, d(1): 200.0}, 1000.0)
        self.assertEqual(out["max_drawdown_pct"], 0.0)
        self.assertIsNone(out["calmar"])
        self.assertIsNotNone(out["annualized_return_pct"])

    def test_annualized_return_uses_the_initial_capital_as_its_denominator(self):
        """365 天赚 1000、本金 10000 ⇒ 正好 10%。"""
        out = sl._segment_metrics({d(0): 500.0, d(364): 500.0}, CAP)
        self.assertEqual(out["span_days"], 365)
        self.assertAlmostEqual(out["annualized_return_pct"], 10.0)

    def test_calmar_is_annualized_return_over_max_drawdown(self):
        out = sl._segment_metrics({d(0): 1000.0, d(364): -1000.0}, CAP)
        # 权益 11000 → 10000，峰值 11000 ⇒ 回撤 1000/11000。落库时按 `_ROUND` 保留
        # 6 位，所以比对到 6 位而不是默认的 7 位。
        expected_dd = 1000.0 / 11000.0 * 100.0
        self.assertAlmostEqual(out["max_drawdown_pct"], expected_dd, places=6)
        self.assertAlmostEqual(out["calmar"], 0.0, places=6)  # 净收益为 0 ⇒ 年化 0

    def test_no_capital_means_no_annualization(self):
        out = sl._segment_metrics({d(0): 100.0, d(1): -50.0}, 0.0)
        self.assertIsNone(out["annualized_return_pct"])
        self.assertIsNone(out["calmar"])
        # 回撤与本金无关，照样算得出来：权益 100 → 50，峰值 100 ⇒ 50%
        self.assertAlmostEqual(out["max_drawdown_pct"], 50.0)

    def test_an_empty_series_yields_no_numbers_instead_of_zeros(self):
        """没有交易的日子不是「零收益」——是「没有数据」。回撤报 0 会读成
        「这条策略在这段行情里毫无波动」。"""
        out = sl._segment_metrics({}, CAP)
        self.assertEqual(out["span_days"], 0)
        self.assertIsNone(out["max_drawdown_pct"])
        self.assertIsNone(out["calmar"])


class TestIntervals(SimpleTestCase):
    """置信区间是「门槛被缩放的格子」被看见的唯一凭据（CONTEXT.md 要求同时输出）。"""

    def test_the_wilson_interval_is_bounded_to_a_probability_range(self):
        for wins, n in [(0, 5), (1, 3), (10, 10), (5, 10), (30, 30)]:
            lo, hi = sl.wilson_interval(wins, n)
            self.assertGreaterEqual(lo, 0.0)
            self.assertLessEqual(hi, 1.0)
            self.assertLessEqual(lo, hi)

    def test_nobody_observed_has_no_interval(self):
        self.assertIsNone(sl.wilson_interval(0, 0))

    def test_all_wins_pushes_the_upper_bound_to_certainty(self):
        self.assertEqual(sl.wilson_interval(10, 10)[1], 1.0)
        self.assertGreater(sl.wilson_interval(10, 10)[0], 0.5)

    def test_more_evidence_narrows_the_interval(self):
        def width(wins, n):
            lo, hi = sl.wilson_interval(wins, n)
            return hi - lo

        self.assertLess(width(50, 100), width(5, 10))

    def test_a_single_trade_has_no_dispersion_to_estimate(self):
        self.assertIsNone(sl.mean_pnl_interval([42.0]))

    def test_the_mean_interval_is_centred_on_the_mean(self):
        lo, hi = sl.mean_pnl_interval([1.0, 2.0, 3.0, 4.0, 5.0])
        self.assertAlmostEqual((lo + hi) / 2, 3.0)

    def test_more_evidence_narrows_the_mean_interval(self):
        def width(values):
            lo, hi = sl.mean_pnl_interval(values)
            return hi - lo

        self.assertLess(width([1.0, 2.0, 3.0] * 10), width([1.0, 2.0, 3.0]))


# --------------------------------------------------------------------------- #
# 证据门槛
# --------------------------------------------------------------------------- #


class TestEvidenceThreshold(SimpleTestCase):
    def test_a_full_window_regime_keeps_the_base_threshold(self):
        self.assertEqual(sl.scaled_min_trades(1.0, SMALL), 3)
        self.assertEqual(sl.scaled_min_trades(1.0, EVIDENCE), EVIDENCE.min_trades)

    def test_a_rare_regime_scales_the_threshold_down(self):
        self.assertEqual(sl.scaled_min_trades(0.2, EVIDENCE), 6)

    def test_the_scaled_threshold_never_reaches_zero(self):
        """门槛为 0 等于「这一格不需要证据」——刹车就没了。"""
        for share in (0.0, 0.001, 1 / 365):
            self.assertGreaterEqual(sl.scaled_min_trades(share, EVIDENCE), 1)

    def test_the_scaled_threshold_never_rises_above_the_base(self):
        self.assertEqual(sl.scaled_min_trades(1.5, SMALL), SMALL.min_trades)

    def test_months_are_distinct_calendar_months(self):
        """挤在一个月里的交易「跨了月」但没铺开。逐月去重取的是严的那个。"""
        self.assertEqual(sl.covered_months([d(0), d(30)]), 1)  # 1/1 与 1/31
        self.assertEqual(sl.covered_months([d(0), d(31)]), 2)  # 1/1 与 2/1
        self.assertEqual(sl.covered_months([d(0), d(31), d(59)]), 3)

    def test_no_trades_cover_no_months(self):
        self.assertEqual(sl.covered_months([]), 0)


class TestScalingCannotRescueAThinSample(SimpleTestCase):
    """`min_months` 不参与缩放，这是缩放不会把门槛整个抹掉的关键。"""

    def test_a_regime_that_appeared_for_two_days_stays_neutral(self):
        # 120 天的窗口里只出现 2 天 ⇒ 占比 1/60 ⇒ 笔数门槛缩到 1
        tags = {d(i): RANGE for i in range(120)}
        tags[d(0)] = UPTREND
        tags[d(60)] = UPTREND
        out = sl.slice_backtest(
            [trade(0, 0, 100.0)],
            tags,
            initial_capital=CAP,
            window=(d(0), d(119)),
            params=EVIDENCE,
        )
        cell = out["cells"]["uptrend"]
        self.assertEqual(cell["threshold"]["min_trades"], 1)
        self.assertTrue(cell["threshold"]["scaled"])
        self.assertEqual(cell["trades"], 1)
        self.assertFalse(cell["threshold"]["met"])
        self.assertEqual(cell["state"], sl.STATE_NEUTRAL)
        self.assertEqual(cell["reason"], sl.REASON_INSUFFICIENT)

    def test_three_months_of_coverage_is_required_even_when_the_count_passes(self):
        """5 笔 ≥ 门槛 3，但全挤在 1 月里 ⇒ 中性；同样的 5 笔铺到 3 个月 ⇒ 适用。

        这里必须用 `min_months=3` 的门槛：`SMALL` 的 `min_months=1` 会让「挤在一个月」
        也过门槛，测出来的就不是「覆盖月数」这条规则了。
        """
        tags = {d(i): RANGE for i in range(60)}

        crowded = same_day_trades([0, 3, 6, 9, 11], 100.0)
        out = sl.slice_backtest(
            crowded, tags, initial_capital=CAP, window=(d(0), d(59)), params=STRICT
        )
        self.assertEqual(out["cells"]["range"]["trades"], 5)
        self.assertEqual(out["cells"]["range"]["months"], 1)
        self.assertTrue(out["cells"]["range"]["threshold"]["met"] is False)
        self.assertEqual(out["cells"]["range"]["state"], sl.STATE_NEUTRAL)

        spread = same_day_trades([0, 22, 30, 45, 59], 100.0)
        out = sl.slice_backtest(
            spread, tags, initial_capital=CAP, window=(d(0), d(59)), params=STRICT
        )
        self.assertEqual(out["cells"]["range"]["months"], 3)
        self.assertTrue(out["cells"]["range"]["threshold"]["met"])
        self.assertEqual(out["cells"]["range"]["state"], sl.STATE_FIT)


# --------------------------------------------------------------------------- #
# 判定表
# --------------------------------------------------------------------------- #


def _metrics(dd, calmar):
    return {"max_drawdown_pct": dd, "calmar": calmar}


class TestFitnessDecisionTable(SimpleTestCase):
    """`_fitness` 的完整判定表。回撤与 Calmar 各与全样本比一次，方向一致才下结论。"""

    def test_better_on_both_axes_is_fit(self):
        self.assertEqual(
            sl._fitness(_metrics(5.0, 2.0), _metrics(10.0, 1.0)), (sl.STATE_FIT, "")
        )

    def test_worse_on_both_axes_is_unfit(self):
        self.assertEqual(
            sl._fitness(_metrics(20.0, 0.5), _metrics(10.0, 1.0)), (sl.STATE_UNFIT, "")
        )

    def test_equal_on_both_axes_is_not_worse_so_it_is_fit(self):
        """「不更差」是 `<=` / `>=`：打平不该把一个策略判成不适用。"""
        self.assertEqual(
            sl._fitness(_metrics(10.0, 1.0), _metrics(10.0, 1.0)), (sl.STATE_FIT, "")
        )

    def test_a_smaller_drawdown_with_a_worse_calmar_is_sample_noise(self):
        state, reason = sl._fitness(_metrics(5.0, 0.5), _metrics(10.0, 1.0))
        self.assertEqual(
            (state, reason), (sl.STATE_NEUTRAL, sl.REASON_DIRECTION_CONFLICT)
        )

    def test_a_larger_drawdown_with_a_better_calmar_is_sample_noise(self):
        state, reason = sl._fitness(_metrics(20.0, 3.0), _metrics(10.0, 1.0))
        self.assertEqual(
            (state, reason), (sl.STATE_NEUTRAL, sl.REASON_DIRECTION_CONFLICT)
        )

    def test_never_drawing_down_is_the_strongest_form_of_not_worse(self):
        """零回撤让 Calmar 无从计算（分母为零），不该因此判成不可比。"""
        self.assertEqual(
            sl._fitness(_metrics(0.0, None), _metrics(10.0, 1.0)), (sl.STATE_FIT, "")
        )

    def test_an_unmeasurable_full_sample_leaves_nothing_to_compare_against(self):
        state, reason = sl._fitness(_metrics(5.0, 2.0), _metrics(0.0, None))
        self.assertEqual((state, reason), (sl.STATE_NEUTRAL, sl.REASON_INCOMPARABLE))

    def test_two_missing_drawdowns_do_not_crash(self):
        """跨三个函数的隐式不变量在栈里一个字都看不到，所以这里显式落中性。"""
        state, reason = sl._fitness(_metrics(None, None), _metrics(None, None))
        self.assertEqual((state, reason), (sl.STATE_NEUTRAL, sl.REASON_INCOMPARABLE))


# --------------------------------------------------------------------------- #
# 端到端（表以外的部分确实接上了）
# --------------------------------------------------------------------------- #


class TestEndToEndStates(SimpleTestCase):
    """一条覆盖 fit / unfit / 门槛拦下 / 高波动 / 未知的端到端。判定表本身见上一节。"""

    def _run(self, trades, tags, params=SMALL, window=SPLIT_WINDOW):
        return sl.slice_backtest(
            trades, tags, initial_capital=CAP, window=window, params=params
        )

    def test_a_profitable_regime_is_fit(self):
        out = self._run(
            same_day_trades([0, 2, 4], 100.0) + same_day_trades([6, 8, 10], 100.0),
            split_tags(),
        )
        self.assertEqual(out["cells"]["range"]["state"], sl.STATE_FIT)
        self.assertEqual(out["cells"]["range"]["reason"], "")

    def test_a_losing_regime_is_unfit(self):
        """箱体里连亏 3 笔，每笔 −400。该格回撤 12%，而全样本因为前段赚过钱（峰值
        10300）只有 11.65% —— 两个指标一致更差 ⇒ 不适用。

        亏得比前段赚得多是**构造上的必要**：全样本的峰值被前段的 +300 抬高了，若这
        三笔只亏 −300，全样本的峰值 10300 与谷底 9400 给出的回撤反而比箱体格更大，
        两个指标就会一好一坏——那测的是 `direction_conflict` 那条支路，不是这一条。
        """
        out = self._run(
            same_day_trades([0, 2, 4], 100.0) + same_day_trades([6, 8, 10], -400.0),
            split_tags(),
        )
        rng = out["cells"]["range"]
        self.assertAlmostEqual(rng["max_drawdown_pct"], 12.0)
        self.assertAlmostEqual(rng["calmar"], -73.0)
        self.assertAlmostEqual(
            out["full_sample"]["max_drawdown_pct"], 11.650485, places=6
        )
        self.assertEqual(rng["state"], sl.STATE_UNFIT)
        self.assertEqual(rng["reason"], "")

    def test_a_regime_below_the_threshold_is_neutral_and_its_numbers_survive(self):
        """中性 ≠ 没有数字。日报要能用数字回答「为什么这一格没结论」。"""
        out = self._run(same_day_trades([6], -500.0), split_tags())
        rng = out["cells"]["range"]
        self.assertEqual(rng["state"], sl.STATE_NEUTRAL)
        self.assertEqual(rng["reason"], sl.REASON_INSUFFICIENT)
        self.assertAlmostEqual(rng["total_pnl"], -500.0)
        self.assertAlmostEqual(rng["win_rate"], 0.0)
        self.assertEqual(rng["trades"], 1)

    def test_high_vol_is_a_blanket_halt_not_an_applicability_verdict(self):
        out = self._run(
            same_day_trades([6, 8, 10], -300.0),
            tags_of({i: HIGH_VOL for i in range(12)}),
        )
        cell = out["cells"]["high_vol"]
        self.assertEqual(cell["state"], sl.STATE_BLANKET)
        self.assertEqual(cell["reason"], sl.REASON_HIGH_VOL_BLANKET)
        # 数字照留：它们仍然说明这段行情里发生了什么
        self.assertAlmostEqual(cell["total_pnl"], -900.0)
        self.assertEqual(cell["trades"], 3)

    def test_the_evidence_threshold_is_void_in_the_high_vol_tier(self):
        """1 笔、1 个月，远在门槛之下——但高波动档与证据无关，仍是 blanket 而非中性。
        否则资讯抬升到高波动之后什么都停不掉（CONTEXT.md）。"""
        out = self._run(
            same_day_trades([6], -300.0), tags_of({i: HIGH_VOL for i in range(12)})
        )
        cell = out["cells"]["high_vol"]
        self.assertFalse(cell["threshold"]["met"])
        self.assertEqual(cell["state"], sl.STATE_BLANKET)

    def test_a_regime_absent_from_the_window_is_unknown(self):
        out = self._run(same_day_trades([6, 8, 10], 100.0), split_tags())
        cell = out["cells"]["uptrend"]
        self.assertEqual(cell["state"], sl.STATE_UNKNOWN)
        self.assertEqual(cell["reason"], sl.REASON_NO_REGIME_DAYS)
        self.assertEqual(cell["trades"], 0)
        self.assertIsNone(cell["max_drawdown_pct"])

    def test_a_regime_present_but_untraded_is_neutral_not_unknown(self):
        out = self._run(
            same_day_trades([0, 2, 4], 100.0),
            tags_of(
                {**{i: DOWNTREND for i in range(6)}, **{i: RANGE for i in range(6, 12)}}
            ),
        )
        rng = out["cells"]["range"]
        self.assertEqual(rng["state"], sl.STATE_NEUTRAL)
        self.assertEqual(rng["reason"], sl.REASON_INSUFFICIENT)
        self.assertEqual(rng["threshold"]["regime_days"], 6)

    def test_unknown_and_neutral_are_distinguishable(self):
        out = self._run(same_day_trades([0, 2, 4], 100.0), split_tags())
        self.assertNotEqual(sl.STATE_UNKNOWN, sl.STATE_NEUTRAL)
        self.assertEqual(out["cells"]["uptrend"]["state"], sl.STATE_UNKNOWN)
        self.assertEqual(out["cells"]["range"]["state"], sl.STATE_NEUTRAL)

    def test_every_regime_always_gets_a_cell(self):
        out = self._run([], split_tags())
        self.assertEqual(
            set(out["cells"]), {"high_vol", "downtrend", "uptrend", "range"}
        )


# --------------------------------------------------------------------------- #
# 载荷形状
# --------------------------------------------------------------------------- #


class TestPayloadShape(SimpleTestCase):
    def _payload(self):
        return sl.slice_backtest(
            same_day_trades([6, 8, 10], 100.0),
            split_tags(),
            initial_capital=CAP,
            window=SPLIT_WINDOW,
            params=SMALL,
        )

    def test_the_payload_carries_its_version(self):
        self.assertEqual(self._payload()["version"], sl.SLICE_VERSION)

    def test_the_window_is_recorded_so_the_conclusion_can_be_attributed(self):
        self.assertEqual(
            self._payload()["window"], {"start": "2026-01-01", "end": "2026-01-12"}
        )

    def test_the_thresholds_it_was_judged_against_travel_with_the_conclusion(self):
        """门槛将来会被调。结论不带着自己那套门槛，半年后就无法解释它为什么是中性。

        键名是 `evidence_threshold` 而不是 `params`：物化时会往同一份载荷里盖一个
        `config_snapshot`，它的 `judgement` 组里已经有一个 `params`。
        """
        self.assertEqual(
            self._payload()["evidence_threshold"], {"min_trades": 3, "min_months": 1}
        )

    def test_regime_days_add_up_to_the_window(self):
        attribution = self._payload()["attribution"]
        self.assertEqual(
            sum(attribution["regime_days"].values()), attribution["window_days"]
        )

    def test_the_payload_is_json_serialisable(self):
        """它要进 `BacktestResult.metrics`（JSONField）。"""
        import json

        json.dumps(self._payload(), ensure_ascii=False)


# --------------------------------------------------------------------------- #
# 池化输入（版本 2）
# --------------------------------------------------------------------------- #


class TestMergeInputs(SimpleTestCase):
    """每格带着「结论是从哪来的」那两样样本，供单元 7 拼起来重算。

    这一节钉的是**可重算性**：池化只靠载荷就能重建出全样本曲线与逐笔统计，不必回头
    读 `BacktestTrade`、也不必重算标签。载荷里的数都是聚合量，聚合量凑不出样本——
    这正是它们必须随结论一起落库的原因（见 `slice.py` 模块 docstring）。

    样本是**收益率**（÷ `initial_capital`）而不是金额：一次（策略 × 阶段）的证据来自
    多份 `initial_capital` 不同的回测，金额相加会按各家本金给权重。所以下面凡是拿样本
    与同格的金额指标对账，都要先乘回 `CAP`。
    """

    def _payload(self, trades=None, tags=None, window=SPLIT_WINDOW):
        return sl.slice_backtest(
            trades if trades is not None else same_day_trades([6, 8, 10], 100.0),
            tags if tags is not None else split_tags(),
            initial_capital=CAP,
            window=window,
            params=SMALL,
        )

    def test_every_cell_carries_its_sample(self):
        """空格子也带（空的那份）。「没有键」与「没有样本」不能长得一样。"""
        for name, cell in self._payload()["cells"].items():
            self.assertEqual(
                set(cell["merge_inputs"]), {"daily_return", "trades"}, name
            )

    def test_an_empty_cell_carries_empty_sample_not_none(self):
        cell = self._payload()["cells"]["uptrend"]  # 窗口内这个阶段没出现过
        self.assertEqual(cell["merge_inputs"]["daily_return"], {})
        self.assertEqual(cell["merge_inputs"]["trades"], [])

    def test_capital_at_or_below_zero_yields_no_sample_not_a_substitute(self):
        """本金 ≤ 0 时这一节是 `None`：不给分母、也不抛。

        `BacktestResult.initial_capital` 没有下界校验（`views.py` 直接
        `request.data.get("initial_capital", 10000)`），所以 0 与负数是**能**走到这里
        的输入。三种处理各自的下场写在 `_merge_inputs` 里；这里钉的是选中的那种：
        `None`，且它**没有**被读成「这一格没有交易」（那是空容器 `{}`/`[]`）。

        同时钉住：切片的**结论**照旧产出——退化的是池化样本，不是这一格的指标。
        """
        for capital in (0, -100):
            with self.subTest(capital=capital):
                out = sl.slice_backtest(
                    same_day_trades([6, 8, 10], 100.0),
                    split_tags(),
                    initial_capital=capital,
                    window=SPLIT_WINDOW,
                    params=SMALL,
                )
                for name, cell in out["cells"].items():
                    self.assertIsNone(cell["merge_inputs"], name)
                    # 结论与数字照旧：四态照给，笔数照数（这三笔全在 `range` 那格）。
                    self.assertIn("state", cell)
                self.assertEqual(out["cells"]["range"]["trades"], 3)
                self.assertEqual(out["full_sample"]["total_pnl"], 300.0)

    def test_the_daily_series_sums_to_the_cells_own_total_pnl(self):
        """样本是收益率，所以对账要先乘回本金。"""
        cell = self._payload()["cells"]["range"]
        self.assertAlmostEqual(
            sum(cell["merge_inputs"]["daily_return"].values()),
            cell["total_pnl"] / CAP,
            places=6,
        )

    def test_the_per_trade_samples_are_that_cells_trades(self):
        """胜率必须能从逐笔样本重算出来——它自己是个四舍五入过的比例，反推是近似。"""
        cell = self._payload()["cells"]["range"]
        samples = cell["merge_inputs"]["trades"]
        self.assertEqual(len(samples), cell["trades"])
        wins = sum(1 for _, pnl in samples if pnl > 0)
        self.assertAlmostEqual(wins / len(samples), cell["win_rate"], places=6)

    def test_the_opening_day_of_every_trade_travels_with_it(self):
        """覆盖月数**无法**从任何计数恢复（`months` 只说有几个），所以带上开仓日。

        开仓日与那笔的收益率成对落库，于是「笔数」「胜率」「覆盖月数」三个量都由这
        一份样本给出，且三者**必然互相一致**——分开存（比如另存一串月份）就多了一处
        能漂的地方。
        """
        out = self._payload(
            [trade(0, 0, 10.0), trade(35, 35, 20.0)],
            tags_of({**{i: RANGE for i in range(2)}, 35: RANGE}),
            window=(d(0), d(35)),
        )
        cell = out["cells"]["range"]["merge_inputs"]
        self.assertEqual(
            [iso for iso, _ in cell["trades"]], ["2026-01-01", "2026-02-05"]
        )
        self.assertEqual(
            len({iso[:7] for iso, _ in cell["trades"]}),
            out["cells"]["range"]["months"],
        )

    def test_a_trade_held_across_two_regimes_splits_its_amount_in_the_series(self):
        """跨阶段的一笔同时进两格的日序列（各一半），而只进一格的逐笔样本（全额）。

        两处不对称在池化输入上原样保留：`daily_return` 是「钱赚在哪」，`trades` 是
        「这笔交易归哪一格」。合成就丢掉了本模块开头那两处刻意的不对称。
        """
        out = self._payload([trade(5, 6, 200.0)])
        self.assertEqual(
            out["cells"]["downtrend"]["merge_inputs"]["daily_return"],
            {"2026-01-06": 0.01},
        )
        self.assertEqual(
            out["cells"]["range"]["merge_inputs"]["daily_return"], {"2026-01-07": 0.01}
        )
        self.assertEqual(
            out["cells"]["downtrend"]["merge_inputs"]["trades"], [["2026-01-06", 0.02]]
        )
        self.assertEqual(out["cells"]["range"]["merge_inputs"]["trades"], [])

    def test_the_full_sample_curve_is_the_sum_of_the_cells_daily_series(self):
        """全样本不单独存，因为它是一条恒等式而不是一个近似——存第二份就会漂。

        与旧版（金额）不同，这里不再能要求**精确相等**：曲线上的数是 `_ROUND` 位小数的
        收益率，逐日相加后再乘回本金，浮点表示误差就在那一档上。`places=6` 正好是
        `_ROUND`，等式一破到那个量级以上就说明某一格的钱漏进了别处或掉在地上。
        """
        out = self._payload(
            same_day_trades([0, 2, 4], 100.0) + same_day_trades([6, 8, 10], -400.0)
        )
        summed: dict[date, float] = {}
        for cell in out["cells"].values():
            for iso, ratio in cell["merge_inputs"]["daily_return"].items():
                day = date.fromisoformat(iso)
                summed[day] = summed.get(day, 0.0) + ratio
        amounts = {day: ratio * CAP for day, ratio in summed.items()}
        self.assertAlmostEqual(
            sl._segment_metrics(amounts, CAP)["total_pnl"],
            out["full_sample"]["total_pnl"],
            places=6,
        )

    def test_the_sample_survives_json(self):
        """它跟着载荷进 `BacktestResult.metrics`（JSONField），日期必须是字符串。"""
        import json

        json.dumps(self._payload()["cells"], ensure_ascii=False)


# --------------------------------------------------------------------------- #
# 标签
# --------------------------------------------------------------------------- #

#: 缩小规模后的判定参数（与 `test_quant` 同一套）。真实参数见 `test_config`。
QUANT_PARAMS = replace(
    JUDGEMENT,
    atr_period=2,
    ema_fast_period=3,
    ema_slow_period=8,
    quantile_window_days=5,
    high_vol_quantile=0.8,
    ema_slope_lookback_days=1,
    trend_separation_min_atr=0.5,
)


def candles(closes, bands=1.0):
    return [
        {
            "date": DAY0 + timedelta(days=i),
            "high": float(c) + bands,
            "low": float(c) - bands,
            "close": float(c),
        }
        for i, c in enumerate(closes)
    ]


RAMP = [100 + i for i in range(16)]


class TestBuildTags(SimpleTestCase):
    """标签是 `label_series` 的投影——只有一处实现，判不出来的日子不进映射。"""

    def test_tags_are_the_judged_days_of_the_series(self):
        rows = candles(RAMP)
        tags = sl.build_tags(rows, QUANT_PARAMS)
        labels = {label.date: label for label in label_series(rows, QUANT_PARAMS)}
        self.assertEqual(
            set(tags), {day for day, label in labels.items() if label.judged}
        )
        for day, regime in tags.items():
            self.assertIs(labels[day].regime, regime)

    def test_unjudged_days_are_absent_instead_of_defaulted(self):
        """预热没走完的日子**不进映射**。塞一个默认档位正是这套机制最该防的失败。"""
        tags = sl.build_tags(candles(RAMP), QUANT_PARAMS)
        self.assertNotIn(DAY0, tags)
        self.assertLess(len(tags), len(RAMP))
        self.assertGreater(len(tags), 0)

    def test_historical_tagging_and_live_judgement_agree_on_the_same_day(self):
        """切片拿标签、实时判定拿序列尾巴——两者必须得出同一个阶段。"""
        rows = candles(RAMP)
        tags = sl.build_tags(rows, QUANT_PARAMS)
        live = latest_label(rows, QUANT_PARAMS)
        self.assertTrue(live.judged)
        self.assertIs(tags[live.date], live.regime)

    def test_decimal_rows_produce_the_same_tags_as_float_rows(self):
        """落库读回来的是 Decimal。口径不能在转向量那一步变。"""
        floats = candles(RAMP)
        decimals = [
            {
                **row,
                **{key: Decimal(str(row[key])) for key in ("high", "low", "close")},
            }
            for row in floats
        ]
        self.assertEqual(
            sl.build_tags(floats, QUANT_PARAMS), sl.build_tags(decimals, QUANT_PARAMS)
        )
