"""量化判定核心（第①段单元 3）。

这个文件要钉住的不是「某个阈值算出来等于几」，而是四条性质：

1. **没有前视**——第 i 天的结论只由 `[0, i]` 决定。历史标注将来要拿去切片，
   一旦某天的标签用到了它之后的价格，切片就「预知」了那段行情，整套结论作废。
2. **四档固定且互斥**，优先级 高波动 > 下行趋势 > 上行趋势 > 箱体震荡。
   箱体震荡是兜底，不是「检出了箱体」。
3. **判不出来就是判不出来**——预热没走完、分位窗口没攒满、波动恒为 0 的日子，
   `regime` 是 `None`，**不是箱体震荡**。拿一个安静的假标签去喂切片，正是这套
   机制最该防的那类失败。
4. **历史标注与实时判定是同一个函数**——`latest_label` 只是 `label_series` 的尾巴，
   单元 4 不得另写一份「只算最后一天」的实现。

用例里的参数是**刻意缩小**的（ATR 2 / EMA 3,8 / 窗口 5）：真实参数（14/20/60/250）
要 250 根以上 K 线才出第一个结论，用真实参数写断言只会把结论埋进噪声里。缩小的只是
规模，规则一模一样；真实参数本身由 `test_config` 钉住。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import numpy as np
from django.test import SimpleTestCase

from apps.regime import quant
from apps.regime.config import JUDGEMENT
from apps.regime.quant import (
    BaseRegime,
    label_series,
    latest_label,
    percentile_rank,
)

PARAMS = replace(
    JUDGEMENT,
    atr_period=2,
    ema_fast_period=3,
    ema_slow_period=8,
    quantile_window_days=5,
    high_vol_quantile=0.8,
    ema_slope_lookback_days=1,
    trend_separation_min_atr=0.5,
)

DAY0 = date(2026, 1, 1)


def candles(closes, bands=1.0):
    """按收盘价序列造日线；`bands` 是每日 (high−close) 的幅度，用来单独控制振幅。

    high/low 与 close 的关系是这三条不变量：high > close > low，所以振幅恒为正，
    ATR 不会因为 K 线自身矛盾而变成 0。
    """
    widths = bands if isinstance(bands, list) else [bands] * len(closes)
    return [
        {
            "date": DAY0 + timedelta(days=i),
            "high": float(c) + widths[i],
            "low": float(c) - widths[i],
            "close": float(c),
        }
        for i, c in enumerate(closes)
    ]


#: 稳步上行：每日 +1，振幅恒定
RAMP = [100 + i for i in range(16)]
#: 先急跌后收敛：跌幅收窄、振幅不变 → 趋势仍在，但波动分位从高往低走
DOWN_CALMING = [
    200,
    195,
    190,
    185,
    182,
    180,
    178,
    176,
    174,
    172,
    170,
    168,
    166,
    164,
    162,
    160,
]
#: 稳定急跌 -3/日 → 价格越跌 ATR% 越高，分位必然顶到 1.0
STEADY_DOWN = [200 - 3 * i for i in range(16)]
#: 几乎不动的漂移：斜率符号在，幅度不够
TINY_DRIFT = [100 + 0.1 * i for i in range(16)]
#: 平静 15 天，最后一天一根巨幅 K 线
QUIET_THEN_SPIKE = [100.0] * 16

#: 三条预热阶梯里最高的一条决定第一天能判定的日子（见 TestUndecidable 一节）
EXPECTED_ONSET = max(
    PARAMS.quantile_window_days + PARAMS.atr_period - 1,
    PARAMS.ema_slow_period - 1,
    PARAMS.ema_slope_lookback_days,
)


class TestFourTiersAreFixed(SimpleTestCase):
    """四档是落库与日报的枚举契约，中文只在展示层出现。"""

    def test_the_enum_has_exactly_four_members(self):
        self.assertEqual(len(BaseRegime), 4)

    def test_values_are_ascii_slugs(self):
        """落库、日志、查询条件里用的都是这些字符串；改名等于一次数据迁移。"""
        self.assertEqual(
            {m.value for m in BaseRegime},
            {"high_vol", "downtrend", "uptrend", "range"},
        )

    def test_priority_is_the_documented_order(self):
        self.assertEqual(
            quant.PRIORITY,
            (
                BaseRegime.HIGH_VOL,
                BaseRegime.DOWNTREND,
                BaseRegime.UPTREND,
                BaseRegime.RANGE,
            ),
        )

    def test_every_member_has_a_chinese_display_name(self):
        self.assertEqual(
            {m.display for m in BaseRegime},
            {"高波动", "下行趋势", "上行趋势", "箱体震荡"},
        )


class TestDecisionTable(SimpleTestCase):
    """规则本身。

    `_classify` 是这条规则的唯一一处陈述，所以直接测它：端到端用例（下一节）证明
    它接到了真实指标上，这一节证明规则在四个方向上都对，包括「符号与间距打架」那格。
    """

    def test_high_vol_beats_both_trends(self):
        """优先级第一档：先问该不该保命，再问往哪走。"""
        for slope, separation in ((1.0, 2.0), (-1.0, -2.0), (0.0, 0.0)):
            with self.subTest(slope=slope, separation=separation):
                self.assertIs(
                    quant._classify(0.95, slope, separation, PARAMS),
                    BaseRegime.HIGH_VOL,
                )

    def test_threshold_is_strictly_exceeded(self):
        """分位**恰好**等于阈值不算高波动：「超过第 80 百分位」不等于「达到」。"""
        self.assertIs(
            quant._classify(PARAMS.high_vol_quantile, 0.0, 0.0, PARAMS),
            BaseRegime.RANGE,
        )
        self.assertIs(
            quant._classify(PARAMS.high_vol_quantile + 0.01, 0.0, 0.0, PARAMS),
            BaseRegime.HIGH_VOL,
        )

    def test_trend_needs_sign_and_separation_to_agree(self):
        threshold = PARAMS.trend_separation_min_atr
        self.assertIs(
            quant._classify(0.5, +1.0, +(threshold + 0.1), PARAMS), BaseRegime.UPTREND
        )
        self.assertIs(
            quant._classify(0.5, -1.0, -(threshold + 0.1), PARAMS), BaseRegime.DOWNTREND
        )

    def test_disagreeing_sign_and_separation_is_not_a_trend(self):
        """间距说「下行」而斜率说「刚拐头」——两边都不算，落回箱体震荡。

        这就是 (EMA20−EMA60) 单独用会出的错：它是带符号的，负值会被读成下行趋势，
        而它到底是一路下行还是刚拐头向下，只有斜率能分辨。
        """
        threshold = PARAMS.trend_separation_min_atr
        self.assertIs(
            quant._classify(0.5, +1.0, -(threshold + 0.1), PARAMS), BaseRegime.RANGE
        )
        self.assertIs(
            quant._classify(0.5, -1.0, +(threshold + 0.1), PARAMS), BaseRegime.RANGE
        )

    def test_separation_exactly_at_the_threshold_is_not_a_trend(self):
        """符号对但幅度只有刚好一个门槛：噪声喊了一声方向，不算趋势。"""
        self.assertIs(
            quant._classify(0.5, +1.0, +PARAMS.trend_separation_min_atr, PARAMS),
            BaseRegime.RANGE,
        )

    def test_box_detection_is_not_a_gate(self):
        """箱体判定**不参与**档位归属：检不检出都落到箱体震荡（它是兜底）。

        把它改成一道闸会多出一个「既非四档、又不成形箱体」的第五态，与「四档固定
        枚举、互斥」直接冲突。它的用途只是给日报写依据，等单元 8 需要时才引入。
        """
        self.assertFalse(hasattr(quant, "detect_box_range"))


class TestClassificationEndToEnd(SimpleTestCase):
    """同一套规则接到真实指标上的表现。"""

    def test_steady_downtrend_is_reported_as_high_vol(self):
        """急跌行情里波动分位必然顶格，所以优先级把它判成高波动而不是下行趋势。

        这条同时是优先级的证明：先断言趋势那一支**确实成立**（斜率与间距都过），
        再断言最终落到的是高波动——否则「判为高波动」可能只是因为趋势压根没成立。
        """
        labels = label_series(candles(STEADY_DOWN), PARAMS)
        last = labels[-1]

        self.assertLess(last.separation, -PARAMS.trend_separation_min_atr)
        self.assertLess(last.ema_slope, 0)
        self.assertGreater(last.atr_pct_rank, PARAMS.high_vol_quantile)
        self.assertIs(last.regime, BaseRegime.HIGH_VOL)

    def test_calming_downtrend_is_a_downtrend(self):
        """跌幅收窄但方向未变：波动分位掉下来，趋势那一支才露出来。"""
        labels = label_series(candles(DOWN_CALMING), PARAMS)

        self.assertIs(labels[EXPECTED_ONSET].regime, BaseRegime.DOWNTREND)
        self.assertLessEqual(
            labels[EXPECTED_ONSET].atr_pct_rank, PARAMS.high_vol_quantile
        )

    def test_uptrend_is_the_mirror_image(self):
        labels = label_series(candles(RAMP), PARAMS)
        first = labels[EXPECTED_ONSET]

        self.assertGreater(first.ema_slope, 0)
        self.assertGreater(first.separation, PARAMS.trend_separation_min_atr)
        self.assertIs(first.regime, BaseRegime.UPTREND)

    def test_small_drift_with_the_right_sign_is_still_range(self):
        """斜率符号在，但均线间距走不满门槛 → 箱体震荡，不是「弱上行趋势」。"""
        labels = label_series(candles(TINY_DRIFT), PARAMS)
        last = labels[-1]

        self.assertGreater(last.ema_slope, 0)
        self.assertLess(last.separation, PARAMS.trend_separation_min_atr)
        self.assertIs(last.regime, BaseRegime.RANGE)

    def test_a_volatility_spike_flips_the_label_to_high_vol(self):
        """平静 15 天之后一根巨幅 K 线：只有它自己（和之后）判为高波动。

        分位是含当日的，所以巨幅那一刻 ATR% 就是窗口里的最大值 → 分位 1.0。
        """
        labels = label_series(
            candles(QUIET_THEN_SPIKE, bands=[1.0] * 15 + [30.0]), PARAMS
        )

        self.assertIs(labels[-1].regime, BaseRegime.HIGH_VOL)
        self.assertEqual(labels[-1].atr_pct_rank, 1.0)


class TestUndecidableDaysAreNotRange(SimpleTestCase):
    """数据不足是「还没有资格判定」，不是某一种阶段。

    与单元 1「失败必须能与『没有新数据』区分」是同一条纪律：把没有结论的日子算成
    箱体震荡，等于拿一个安静的假标签去喂切片，而切片正是拿这些标签停策略的地方。
    """

    def test_days_before_the_warmup_are_unjudged(self):
        labels = label_series(candles(RAMP), PARAMS)

        self.assertEqual(
            [lb.judged for lb in labels],
            [False] * EXPECTED_ONSET + [True] * (len(RAMP) - EXPECTED_ONSET),
        )

    def test_onset_is_the_tallest_warmup_ladder(self):
        """三条阶梯：ATR(2) 要 3 根、分位窗口要 窗口+ATR−1 = 6 根、EMA(8) 要 8 根。

        取最高的一条（EMA 慢线）→ 下标 7。写成算式的理由不是偷懒：这条用例红了
        就说明有人改了参数或改了某条阶梯，而两者的后果都是「标注序列开头少了一段」
        ——那种症状看起来像数据缺失，不像参数配错。
        """
        self.assertEqual(EXPECTED_ONSET, 7)

    def test_an_unjudged_day_still_carries_the_numbers_it_does_have(self):
        """缺的是趋势轴（EMA 慢线还没预热完），不是整天的数据。

        未判定 ≠ 什么都不记：`atr_pct_rank` 与 `quantile_sample` 都在。事后要解释
        「那天为什么判不出来」，答案同样得在这一行里。
        """
        label = label_series(candles(DOWN_CALMING), PARAMS)[EXPECTED_ONSET - 1]

        self.assertIsNone(label.regime)
        self.assertIsNotNone(label.atr_pct)
        self.assertEqual(label.atr_pct_rank, 0.2)
        self.assertEqual(label.quantile_sample, PARAMS.quantile_window_days)
        self.assertIsNone(label.ema_slow)
        self.assertIsNone(label.separation)

    def test_zero_volatility_is_unjudged_not_range(self):
        """价格一动不动（high=low=close）时 ATR 恒为 0，分位无从谈起。

        这是个数据异常形态（停牌、K 线被重复返回），不是「最平静的箱体」。若在这里
        给个箱体震荡，停牌行情就会被当成正常震荡行情放策略继续跑。
        """
        flat = [
            {
                "date": DAY0 + timedelta(days=i),
                "high": 100.0,
                "low": 100.0,
                "close": 100.0,
            }
            for i in range(12)
        ]
        labels = label_series(flat, PARAMS)

        self.assertTrue(all(lb.regime is None for lb in labels))
        # 前两根连 ATR 都算不出来（`compute_atr` 要 period+1 根），是 None 不是 0；
        # 之后 ATR 确实是 0，而 0 不能当分母，所以分位也一路是 None。
        self.assertEqual([lb.atr for lb in labels[:2]], [None, None])
        self.assertTrue(all(lb.atr == 0.0 for lb in labels[2:]))
        self.assertTrue(all(lb.atr_pct_rank is None for lb in labels))

    def test_series_shorter_than_the_window_is_never_judged(self):
        """窗口比序列还长 ⇒ 一天都攒不满 ⇒ 一天都不判。"""
        labels = label_series(candles(RAMP), replace(PARAMS, quantile_window_days=50))

        self.assertTrue(all(lb.regime is None for lb in labels))
        self.assertTrue(all(lb.regime != BaseRegime.RANGE for lb in labels))

    def test_series_shorter_than_the_indicators_does_not_crash(self):
        """指标库在样本不足时返回**空数组**而不是全 NaN，逐日索引会直接 IndexError。

        这里连同默认参数（EMA 60 / 窗口 250）一起测：单元 4 拿到的就是刚上线时
        只有几十根 K 线的库，它必须安静地给出「还没资格判定」，而不是炸掉当天任务。
        """
        for length in (1, 2, 3, 59):
            with self.subTest(length=length):
                labels = label_series(candles([100 + i for i in range(length)]))
                self.assertEqual(len(labels), length)
                self.assertTrue(all(lb.regime is None for lb in labels))


class TestNoLookahead(SimpleTestCase):
    """第 i 天的结论只由 `[0, i]` 决定。"""

    def test_adding_later_data_does_not_change_earlier_labels(self):
        full = label_series(candles(DOWN_CALMING), PARAMS)
        truncated = label_series(candles(DOWN_CALMING[:12]), PARAMS)

        self.assertEqual(len(truncated), 12)
        self.assertEqual(full[:12], truncated)

    def test_mutating_later_data_does_not_change_earlier_labels(self):
        """把后半段改成一段平静的横盘：前面的标签必须一个字节都不动。

        改的是数值而不是「让标签换个说法」——平静下来会让末段的 ATR% 分位塌到低位，
        末段标签随之离开高波动，于是这条用例同时证明了「前缀没动」和「后缀确实动了」
        （后者是前者不空转的凭据）。
        """
        original = label_series(candles(DOWN_CALMING), PARAMS)
        calm_tail = DOWN_CALMING[:12] + [166.0, 165.5, 165.2, 165.0]
        mutated = label_series(candles(calm_tail), PARAMS)

        self.assertEqual(original[:12], mutated[:12])
        self.assertIs(original[-1].regime, BaseRegime.HIGH_VOL)
        self.assertNotEqual(original[-1].regime, mutated[-1].regime)


class TestSeriesShape(SimpleTestCase):
    def test_output_is_one_label_per_candle_in_order(self):
        series = candles(RAMP)
        labels = label_series(series, PARAMS)

        self.assertEqual([lb.date for lb in labels], [row["date"] for row in series])

    def test_empty_input_yields_no_labels(self):
        self.assertEqual(label_series([]), [])

    def test_latest_label_is_the_tail_of_the_series(self):
        """单元 4 走 `latest_label`，单元 6 走 `label_series`。

        两者必须是同一次计算的两端——若各写一份，量化口径就有了第二处实现，而它们
        之间的任何一点差异都会静默存在（两边看起来都很正常）。
        """
        series = candles(DOWN_CALMING)
        self.assertEqual(latest_label(series, PARAMS), label_series(series, PARAMS)[-1])

    def test_latest_label_distinguishes_empty_from_undecidable(self):
        """`None` = 一根 K 线都没有（取数问题）；`DayLabel(regime=None)` = 有数据但
        还判不出来（等预热）。单元 4 要分开处理这两件事，所以不能合并成一个值。
        """
        undecidable = latest_label(candles([100.0, 101.0]), PARAMS)

        self.assertIsNotNone(undecidable)
        self.assertIsNone(undecidable.regime)
        self.assertIsNone(latest_label([]))


class TestPercentileRank(SimpleTestCase):
    """分位是整套阈值的标定基准，所以它的口径本身就是规格。"""

    def test_counts_values_less_than_or_equal(self):
        """`<=`（含相等）：并列时计入更高分位。"""
        self.assertEqual(percentile_rank(np.array([1.0, 2.0, 2.0, 3.0]), 2.0), 0.75)

    def test_the_maximum_of_a_rising_window_is_one(self):
        self.assertEqual(percentile_rank(np.array([1.0, 2.0, 3.0, 4.0]), 4.0), 1.0)

    def test_a_value_below_everything_is_zero(self):
        self.assertEqual(percentile_rank(np.array([1.0, 2.0, 3.0, 4.0]), 0.5), 0.0)

    def test_all_ties_give_one(self):
        """全部并列时恒为 1.0——这是「并列往保守倒」的直接后果。

        真实日线的 ATR% 撞上同一浮点数的概率极低，这条只在退化数据（价格被重复返回、
        停牌）上出现；那时判成高波动意味着停开新仓，方向是对的。
        """
        self.assertEqual(percentile_rank(np.array([5.0, 5.0, 5.0]), 5.0), 1.0)

    def test_is_an_exact_integer_count_not_an_interpolation(self):
        """分数秩：k/n 的精确有理数，跨机器、跨 numpy 版本完全一致。

        插值分位会引入实现定义的自由度，而这里是阈值的标定基准。
        """
        window = np.array([3.0, 1.0, 4.0, 1.0, 5.0, 9.0, 2.0])
        self.assertEqual(percentile_rank(window, 4.0), 5 / 7)

    def test_empty_window_raises(self):
        """空窗口没有分位可言；返回 0 或 0.5 都会变成一个安静的假结论。"""
        with self.assertRaises(ValueError):
            percentile_rank(np.array([]), 1.0)


class TestInputHandling(SimpleTestCase):
    def test_decimal_and_string_rows_give_the_same_labels(self):
        """落库读回是 Decimal，ccxt 给的是 float，JSON 里可能是字符串。

        三条来源必须是同一口径，否则「同一段行情」在实时判定与历史标注两条路上
        会得出不同的阶段——而那正是切片结论不可信的开端。
        """
        plain = candles(RAMP)
        typed = [
            {
                "date": row["date"],
                "high": Decimal(str(row["high"])),
                "low": Decimal(str(row["low"])),
                "close": str(row["close"]),
            }
            for row in plain
        ]

        self.assertEqual(label_series(typed, PARAMS), label_series(plain, PARAMS))
        self.assertIs(label_series(typed, PARAMS)[-1].regime, BaseRegime.UPTREND)

    def test_datetime_open_times_are_accepted_and_reduced_to_a_date(self):
        """上游给的是日线开盘时刻（UTC 零点），标签要的是日粒度。"""
        rows = [
            {
                "date": datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=i),
                "high": 100.0 + i + 1,
                "low": 100.0 + i - 1,
                "close": 100.0 + i,
            }
            for i in range(16)
        ]
        labels = label_series(rows, PARAMS)

        self.assertEqual(labels[0].date, date(2026, 1, 1))
        self.assertEqual(labels[-1].date, date(2026, 1, 16))

    def test_a_row_without_a_date_is_rejected(self):
        """日期是必填的：标签序列的整个用途就是「日期 → 阶段」，单元 6 按开仓日期 join。

        缺日期还能悄悄产出一条标签——那条标签永远 join 不上，而它的存在看起来
        完全正常。
        """
        with self.assertRaises(ValueError) as ctx:
            label_series([{"high": 101.0, "low": 99.0, "close": 100.0}], PARAMS)
        self.assertIn("date", str(ctx.exception))

    def test_an_unparseable_price_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            label_series(
                [{"date": DAY0, "high": 101.0, "low": 99.0, "close": "abc"}], PARAMS
            )
        self.assertIn("high/low/close", str(ctx.exception))


class TestParametersAreHonoured(SimpleTestCase):
    """参数真的在起作用——否则「改参数必须改代码发版」这句就是空话。"""

    def test_raising_the_separation_threshold_demotes_a_trend_to_range(self):
        calm = candles(DOWN_CALMING)
        self.assertIs(
            label_series(calm, PARAMS)[EXPECTED_ONSET].regime, BaseRegime.DOWNTREND
        )

        strict = replace(PARAMS, trend_separation_min_atr=3.0)
        self.assertIs(
            label_series(calm, strict)[EXPECTED_ONSET].regime, BaseRegime.RANGE
        )

    def test_high_vol_threshold_is_exclusive_at_the_rank_that_lands_on_it(self):
        """分位恰好落在阈值上那一天：不算高波动，趋势那一支照常生效。"""
        calm = candles(DOWN_CALMING)
        # 收敛段的排名按 5 日窗口是 0.2/0.4/0.6/…，取阈值 0.6 让某一天正好压线。
        on_the_line = replace(PARAMS, high_vol_quantile=0.6)
        labels = label_series(calm, on_the_line)

        exactly = [lb for lb in labels if lb.atr_pct_rank == 0.6]
        self.assertTrue(exactly)
        self.assertTrue(all(lb.regime is BaseRegime.DOWNTREND for lb in exactly))

    def test_a_longer_quantile_window_delays_the_first_judgement(self):
        """窗口拉长到 12 天，第一次判定推迟到下标 13——**不是** 11。

        这里是两条约束的叠加，容易只想到前一条：既要窗口「够长」（i+1 >= 12 → i >= 11），
        还要窗口里 12 个 ATR% **全是有限值**，而 ATR% 自己要到下标 2 才有值。所以真正
        的开判日是 2 + 12 - 1 = 13。漏掉这一层，代码会在标注序列开头多写几天假标签。
        """
        labels = label_series(candles(RAMP), replace(PARAMS, quantile_window_days=12))

        self.assertTrue(all(not lb.judged for lb in labels[:13]))
        self.assertTrue(labels[13].judged)
        self.assertEqual(labels[13].quantile_sample, 12)
