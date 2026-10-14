"""人工真值与成功标准①的比对（第 163 / 164 条）：纯函数那一半。

`truth_run`（取数、落库、渲染）与 `/regime label` 那一条命令各自有自己的文件；本文件只钉
`truth.py`——给它一串区间与一张「日期 → 阶段」表，它算得对不对。

本文件里最要紧的五条性质，每一条坏了都不报警、只出错：

1. **缺口按不匹配计，但要单列。** 算法当天没有输出的日子进分母、不进分子（第 163 条的口径：
   分母是「有真值的天」，算法答不上来就是没答对），可它必须与「判成了别的档」分开计数——
   混成一个数，会让日线没回填看起来像量化判反了。
2. **冲突日不进分母。** 人工真值自己打架的那天，算法答什么都是错的；把它算进分母，等于让
   「标注矛盾」去惩罚算法。**同档位的重叠不算冲突**（两段都说熊市是互相佐证），而覆盖天数
   按**日集合**算，不是各段天数相加。
3. **系统性错向是单向的，一次就否掉。** 人工标极端档、算法判箱体震荡 ⇒ 计一次；反过来、
   或判成上行趋势 ⇒ 只算普通不一致。这一半正是箱体震荡这个兜底档的退化之路。
4. **`rate is None` 不是 0。** 一天都比不了（没录真值 / 全在冲突日）与「全判错了」必须能
   分开——前者读作「还没有真值」，后者读作「一条都没对」。
5. **①那段话只有一处渲染。** 体检页与 `/regime label` 共用 `describe`；本文件钉住它的
   首行与那些计数行的措辞，因为两个入口给出两个说法时，读到哪一个都会让人怀疑另一个。
"""

from __future__ import annotations

from datetime import date

from django.test import SimpleTestCase

from apps.regime import config, truth
from apps.regime.quant import BaseRegime

MIN_RATE = config.SHADOW.min_agreement_rate


def _d(day: str) -> date:
    return date.fromisoformat(day)


def _iv(start: str, end: str, regime: BaseRegime = BaseRegime.DOWNTREND, **kw):
    return truth.Interval(start=_d(start), end=_d(end), regime=regime, **kw)


CALM = BaseRegime.RANGE
UP = BaseRegime.UPTREND
DOWN = BaseRegime.DOWNTREND
VOL = BaseRegime.HIGH_VOL


class TestInterval(SimpleTestCase):
    def test_days_are_inclusive(self):
        """区间是闭的，两端都算。差一天在页面上看不出来，只让一致率悄悄地偏。"""
        self.assertEqual(_iv("2024-01-05", "2024-01-05").days, 1)
        self.assertEqual(_iv("2024-01-05", "2024-01-06").days, 2)
        # 跨月、跨年各来一条：`each_day` 的循环要是按「加 30 天」走，这两条会当场露馅。
        self.assertEqual(_iv("2024-01-30", "2024-02-02").days, 4)
        self.assertEqual(_iv("2023-12-30", "2024-01-02").days, 4)

    def test_a_reversed_interval_is_refused(self):
        """录反了的区间一天的并集都不贡献，于是它会以「录进去了、一致率没变」的形状静默
        存在——这正是 `ck_truth_interval_order` 与这个校验要一起挡住的形状。"""
        with self.assertRaises(truth.TruthInputError):
            _iv("2024-03-20", "2024-01-05")

    def test_the_regime_must_be_one_of_the_four(self):
        """档位只认枚举。传字符串进来的话，`==` 在 str Enum 上会「碰巧」成立一部分，
        而那正是最难查的一类错。"""
        with self.assertRaises(truth.TruthInputError):
            truth.Interval(
                start=_d("2024-01-05"), end=_d("2024-01-06"), regime="downtrend"
            )

    def test_each_day_is_inclusive(self):
        self.assertEqual(
            list(truth.each_day(_d("2024-01-05"), _d("2024-01-07"))),
            [_d("2024-01-05"), _d("2024-01-06"), _d("2024-01-07")],
        )


class TestTally(SimpleTestCase):
    def test_all_matched(self):
        tags = {_d(f"2024-01-0{i}"): DOWN for i in range(5, 8)}
        data = truth.tally([_iv("2024-01-05", "2024-01-07")], tags)
        self.assertEqual(data.counted, 3)
        self.assertEqual(data.matched, 3)
        self.assertEqual(data.missing, 0)
        self.assertAlmostEqual(data.rate, 1.0)
        self.assertTrue(data.met)

    def test_missing_days_count_as_mismatches_but_are_listed_apart(self):
        """性质 1。分母是**有真值的天**：算法答不上来就是没答对。但它与「判成了别的档」
        分开计数——混成一个数会让「日线没回填」看起来像「量化判反了」。"""
        tags = {_d("2024-01-05"): DOWN, _d("2024-01-06"): DOWN}
        data = truth.tally([_iv("2024-01-05", "2024-01-08")], tags)
        self.assertEqual(data.counted, 4, "缺口要进分母")
        self.assertEqual(data.matched, 2)
        self.assertEqual(data.differed, 0)
        self.assertEqual(data.missing, 2, "缺口要单列")
        self.assertAlmostEqual(data.rate, 0.5)

    def test_the_denominator_is_the_day_set_not_the_sum_of_intervals(self):
        """性质 2 的前半。同档位重叠的两段是互相佐证：重叠的天只算一天，覆盖天数**不是**
        两段相加。按相加算的话，标重一段就会把一致率的权重搬过去。"""
        tags = {_d(f"2024-01-{i:02d}"): DOWN for i in range(10, 21)}  # 01-10 ~ 01-20
        data = truth.tally(
            [_iv("2024-01-10", "2024-01-17"), _iv("2024-01-13", "2024-01-20")], tags
        )
        self.assertEqual(data.intervals, 2)
        self.assertEqual(data.days, 11, "01-10 ~ 01-20 是 11 天，不是 8+8")
        self.assertEqual(data.conflict_days, 0, "同档位重叠不是冲突")
        self.assertEqual(data.counted, 11)

    def test_conflicting_days_leave_the_denominator(self):
        """性质 2 的后半。人工真值自己打架的那天，算法答什么都是错的——它不进分母。"""
        tags = {
            _d("2024-01-10"): DOWN,
            _d("2024-01-11"): DOWN,
            _d("2024-01-14"): CALM,
            _d("2024-01-15"): CALM,
        }
        data = truth.tally(
            [_iv("2024-01-10", "2024-01-13"), _iv("2024-01-12", "2024-01-15", CALM)],
            tags,
        )
        self.assertEqual(data.days, 6)
        self.assertEqual(data.conflict_days, 2, "01-12、01-13 两天档位不同")
        self.assertEqual(data.counted, 4, "冲突的两天不进分母")
        self.assertEqual(data.matched, 4, "剩下四天都对上了")

    def test_conflicting_days_follow_the_one_definition(self):
        """`conflicting_days` 与 `tally` 报的必须是同一个数：录入时当场报的冲突，与后来
        从分母里扣掉的天数，两处各写一遍迟早会对不上。"""
        intervals = [
            _iv("2024-01-10", "2024-01-13"),
            _iv("2024-01-12", "2024-01-15", CALM),
        ]
        self.assertEqual(
            len(truth.conflicting_days(intervals)),
            truth.tally(intervals, {}).conflict_days,
        )

    def test_a_fully_conflicting_roster_has_no_rate_at_all(self):
        """一天都比不了时 `rate` 是 `None` 而不是 0——「还没有真值」与「一条都没对」在
        页面上必须能分开。这里两段**完全重合且档位不同**，所以一天都不剩。"""
        data = truth.tally(
            [_iv("2024-01-10", "2024-01-12"), _iv("2024-01-10", "2024-01-12", CALM)], {}
        )
        self.assertEqual(data.conflict_days, 3)
        self.assertIsNone(data.rate)
        self.assertEqual(data.counted, 0)
        self.assertFalse(data.met, "算不出来按未达标计")
        self.assertIn("按未达标计", truth.describe(data)[0])


class TestSystematicMisdirection(SimpleTestCase):
    """性质 3：单向，一次即不达标（第 164 条）。"""

    def _one(self, human: BaseRegime, algo: BaseRegime):
        return truth.tally(
            [_iv("2024-01-05", "2024-01-05", human)], {_d("2024-01-05"): algo}
        )

    def test_extreme_read_as_calm_is_misdirection(self):
        for extreme in (VOL, DOWN):
            with self.subTest(extreme=extreme):
                data = self._one(extreme, CALM)
                self.assertEqual(data.misdirected, 1)
                self.assertFalse(data.met, "一次就不达标，不看一致率多高")

    def test_the_other_directions_are_not_misdirection(self):
        """反过来的错（把箱体判成极端）是保守方向，代价是多停几次策略；判成上行趋势是普通
        的不一致。两者都只算普通错——单向不是漏了一半，而是要堵的恰好是那一半。"""
        for human, algo in ((CALM, VOL), (CALM, DOWN), (DOWN, UP), (VOL, UP)):
            with self.subTest(human=human, algo=algo):
                data = self._one(human, algo)
                self.assertEqual(data.differed, 1)
                self.assertEqual(data.misdirected, 0)

    def test_a_missing_day_is_not_misdirection(self):
        """算法当天没有输出与「判成了箱体震荡」是两件事：前者已经在 `missing` 里单列，
        再说成错向是把一个数据缺口记成一次误判。"""
        data = truth.tally([_iv("2024-01-05", "2024-01-05", VOL)], {})
        self.assertEqual(data.missing, 1)
        self.assertEqual(data.misdirected, 0)

    def test_a_high_rate_still_fails_on_a_single_misdirection(self):
        """第 163 条的两条标准**同时**要满足：一致率再高，错向一次也否掉。"""
        days = [_d(f"2024-01-{i:02d}") for i in range(1, 11)]
        tags = {day: DOWN for day in days}
        tags[days[-1]] = CALM  # 十天里只有最后一天是「该保命却说不必保命」
        data = truth.tally([_iv("2024-01-01", "2024-01-10")], tags)
        self.assertAlmostEqual(data.rate, 0.9)
        self.assertGreater(data.rate, MIN_RATE, "一致率是够的")
        self.assertEqual(data.misdirected, 1)
        self.assertFalse(data.met)

    def test_the_threshold_comes_from_the_config_surface(self):
        """第 164 条要求达标阈值进统一配置面。这里钉住「它读的是配置那一份」，而不是
        在纯层里复述一个 0.80。"""
        self.assertEqual(truth.Agreement().min_rate, MIN_RATE)


class TestNewsEscalatedDays(SimpleTestCase):
    """性质 6：资讯抬升日不进分母（ADR 0002 / 第 77 条）。

    这些天机制用的**不是**纯量化结论，拿纯量化标签去比是不公平的。最要紧的形状是极端
    行情：人工标〈高波动〉、纯量化标签是〈箱体震荡〉——不摘的话，**机制靠资讯判对了的
    那一天反而落进 `misdirected`**，一次就把①否掉。
    """

    #: 那天在生效的判定：基础〈箱体震荡〉、生效〈高波动〉。
    ESCALATED = (CALM, VOL)

    def test_an_escalated_day_leaves_the_denominator(self):
        tags = {_d("2024-01-05"): CALM, _d("2024-01-06"): CALM}
        data = truth.tally(
            [_iv("2024-01-05", "2024-01-07")],
            tags,
            in_force={_d("2024-01-07"): self.ESCALATED},
        )
        self.assertEqual(data.escalated, 1)
        self.assertEqual(data.counted, 2, "只有没被抬升的那两天进分母")
        self.assertEqual(data.days, 3)
        self.assertEqual(
            data.days,
            data.conflict_days + data.escalated + data.counted,
            "四摊互斥：这几个数会被人加起来验算",
        )

    def test_the_would_be_misdirection_disappears(self):
        """不摘的话这就是一次系统性错向——第 164 条最重的那一句，会落在机制判对的日子里。"""
        intervals = [_iv("2024-01-05", "2024-01-05", VOL)]
        tags = {_d("2024-01-05"): CALM}
        before = truth.tally(intervals, tags)
        self.assertEqual(before.misdirected, 1)
        after = truth.tally(
            intervals, tags, in_force={_d("2024-01-05"): self.ESCALATED}
        )
        self.assertEqual(after.misdirected, 0)
        self.assertEqual(after.counted, 0)

    def test_the_matched_count_says_whether_the_news_got_it_right(self):
        """`escalated_matched` 是给「摘掉 N 天」留的底：人工标的就是当天的生效阶段时记一个。

        它**不参与达标判断**（①是量化判定的准入门槛）——所以这里同时钉住「K 很大也不
        达标」，否则那几天会被悄悄算成判对。
        """
        days = [_d(f"2024-01-{i:02d}") for i in range(5, 10)]
        in_force = {day: self.ESCALATED for day in days}
        data = truth.tally(
            [_iv("2024-01-05", "2024-01-09", VOL)], {}, in_force=in_force
        )
        self.assertEqual((data.escalated, data.escalated_matched), (5, 5))
        self.assertEqual(data.counted, 0)
        self.assertFalse(data.met, "K 不进分子：一天都没比过，不该因为资讯判对了就达标")
        self.assertIsNone(data.rate)

    def test_a_no_op_escalation_is_not_excluded(self):
        """基础阶段已经是〈高波动〉时资讯照抬升、标志照记，但生效阶段与基础相同。

        那天机制给出的**就是**纯量化答案，拿它比是公平的；摘掉它等于把最极端的日子从
        分母里挑走，一致率只会被抬上去——而这是准入门槛，往哪个方向骗都不行。
        """
        data = truth.tally(
            [_iv("2024-01-05", "2024-01-05", VOL)],
            {_d("2024-01-05"): VOL},
            in_force={_d("2024-01-05"): (VOL, VOL)},
        )
        self.assertEqual(data.escalated, 0)
        self.assertEqual(data.matched, 1)

    def test_absent_days_are_compared_as_usual(self):
        """映射里没有那天 = 判定链还没开始（历史区间大多如此），照旧按纯量化口径比。"""
        data = truth.tally(
            [_iv("2024-01-05", "2024-01-05", VOL)],
            {_d("2024-01-05"): VOL},
            in_force={_d("2024-01-07"): self.ESCALATED},
        )
        self.assertEqual((data.escalated, data.matched), (0, 1))

    def test_a_conflict_day_is_not_also_an_escalated_day(self):
        """冲突优先：人工真值自己都没定义的那天，说「不能比」的理由更强，也只该记一次。"""
        data = truth.tally(
            [
                _iv("2024-01-05", "2024-01-05", VOL),
                _iv("2024-01-05", "2024-01-05", CALM),
            ],
            {},
            in_force={_d("2024-01-05"): self.ESCALATED},
        )
        self.assertEqual((data.conflict_days, data.escalated, data.counted), (1, 0, 0))
        self.assertEqual(data.days, 1)

    def test_an_escalated_day_with_no_tag_is_not_a_gap(self):
        """抬升优先于缺口：那天本来就不参与比对，记成「算法当天没有输出」会把一个口径
        问题摊到数据的账上。"""
        data = truth.tally(
            [_iv("2024-01-05", "2024-01-05", VOL)],
            {},
            in_force={_d("2024-01-05"): self.ESCALATED},
        )
        self.assertEqual((data.missing, data.escalated), (0, 1))

    def test_by_default_nothing_is_excluded(self):
        """不传 `in_force` 就是没有判定链的时代：与抬升这件事被引入之前逐字一致。"""
        data = truth.tally(
            [_iv("2024-01-05", "2024-01-05", VOL)], {_d("2024-01-05"): VOL}
        )
        self.assertEqual((data.escalated, data.matched, data.met), (0, 1, True))


class TestDescribe(SimpleTestCase):
    """性质 5：①那段话的唯一渲染器。"""

    def test_without_any_truth_it_points_at_the_entry(self):
        """一段真值都没有时，这一行给出**入口**：留白（或只说「无法判定」）会被人读成
        「这条大概没问题」，而这一段正是出 Shadow 的依据。"""
        line = truth.describe(truth.Agreement())[0]
        self.assertIn("无法判定", line)
        self.assertIn("按未达标计", line)
        self.assertIn("/regime label add", line)
        self.assertIn(f"{MIN_RATE:.0%}", line)

    def test_the_verdict_line_carries_the_ratio_and_the_rate(self):
        line = truth.describe(
            truth.Agreement(intervals=1, days=4, counted=4, matched=3, differed=1)
        )[0]
        self.assertIn("未达标", line)
        self.assertIn("3/4 天一致（75%）", line)

    def test_a_met_result_says_so(self):
        lines = truth.describe(
            truth.Agreement(intervals=2, days=10, counted=10, matched=9, differed=1)
        )
        self.assertIn("达标", lines[0])
        self.assertFalse(
            [line for line in lines if "系统性错向" in line],
            "没有错向的时候不该出现这一行——它一出现就是最重的一句话",
        )

    def test_the_misdirection_line_says_one_is_enough(self):
        """这一行得自己说清「哪怕一次」（第 164 条）：不然读的人会以为它也有个阈值。"""
        lines = truth.describe(
            truth.Agreement(
                intervals=1, days=10, counted=10, matched=9, differed=1, misdirected=1
            )
        )
        line = next(line for line in lines if "系统性错向" in line)
        self.assertIn("1 天", line)
        self.assertIn("哪怕一次", line)

    def test_a_gap_is_reported_as_a_gap_not_as_a_wrong_answer(self):
        line = next(
            line
            for line in truth.describe(
                truth.Agreement(
                    intervals=1, days=4, counted=4, matched=2, differed=0, missing=2
                )
            )
            if "分母" in line
        )
        self.assertIn("2 天一致", line)
        self.assertIn("2 天算法当天没有输出", line)

    def test_an_escalated_day_is_reported_apart(self):
        """摘掉的天必须报出来，否则它就是一个看不见底的黑洞。"""
        line = next(
            line
            for line in truth.describe(
                truth.Agreement(intervals=1, days=5, escalated=2, counted=3, matched=3)
            )
            if "资讯抬升" in line
        )
        self.assertIn("2 天", line)
        self.assertIn("不计入分母", line)
        self.assertIn("不是纯量化结论", line)

    def test_the_escalated_line_says_how_many_the_news_got_right(self):
        line = next(
            line
            for line in truth.describe(
                truth.Agreement(
                    intervals=1,
                    days=5,
                    escalated=2,
                    escalated_matched=1,
                    counted=3,
                    matched=3,
                )
            )
            if "资讯抬升" in line
        )
        self.assertIn("其中 1 天人工标注与当天的生效阶段一致", line)

    def test_all_escalated_names_which_kind_of_nothing(self):
        """没有分母时那句话是人唯一的解释，而「标注自相矛盾」与「那几天不能比」对应的是
        两个完全不同的下一步动作。"""
        lines = truth.describe(truth.Agreement(intervals=1, days=4, escalated=4))
        self.assertIn("无法判定", lines[0])
        line = next(line for line in lines if "没有一天可用于比对" in line)
        self.assertIn("资讯抬升日", line)
        self.assertNotIn("冲突", line)

    def test_both_causes_are_named_when_both_apply(self):
        line = next(
            line
            for line in truth.describe(
                truth.Agreement(
                    intervals=2, days=6, conflict_days=3, escalated=3, counted=0
                )
            )
            if "没有一天可用于比对" in line
        )
        self.assertIn("互相冲突的档位或资讯抬升日", line)

    def test_no_escalated_line_when_there_are_none(self):
        lines = truth.describe(
            truth.Agreement(intervals=1, days=3, counted=3, matched=3)
        )
        self.assertFalse([line for line in lines if "资讯抬升" in line])

    def test_a_conflicting_roster_explains_the_missing_denominator(self):
        lines = truth.describe(
            truth.Agreement(intervals=2, days=6, conflict_days=6, counted=0)
        )
        self.assertIn("无法判定", lines[0])
        self.assertTrue(any("冲突日" in line for line in lines))
        self.assertTrue(any("没有一天可用于比对" in line for line in lines))
