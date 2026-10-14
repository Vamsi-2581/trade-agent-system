"""Shadow 每日记录的写入方（第①段单元 8i 的收尾）。

`test_timing.py` 钉的是**接线**（这条职责挂在哪、排在谁后面）；这个文件钉的是它的
另一半——**一行一天写成什么样**。判定与推导的逻辑在这里一行都不断言，那会是第二处真相。

五条性质，每一条坏了都不报警、只出错的数。而这张表是**成功标准①的一致率、自熔断频率
条款、日报第②段的今昨做差**三个东西共同的唯一数据源，所以它错的方式全都是「静默」。

1. **三值内联、外键下钻**：三值从 `payload` 抄一份在本地，外键指向**抄来的那一行**
   （`(symbol, effective_at)` 定位）。外键若指向 `current_judgement()`（今天**生效**的
   那条），它会天天比内联三值早一天——因为今天的判定按 `business_midnight(run_day+1)`
   生效，于是「今天生效的」永远是昨天判的。那种错位处处自洽，最难查。
2. **「还没有结论」不算结论**：`base_regime` 为空的行是**占位**，可以被当天后续心跳补写；
   有结论的行是**定论**，此后任何心跳都不改写它。业务日 D 的第一轮心跳落在北京 08:00，
   而 D−1 那根日线正是在那一刻收盘——管道晚几分钟，这一轮就回 `stale_candles`。少了这条
   区分，D 在表里就永久是一行空三值，而这一整天机制其实是有结论的。
3. **建议清单冻结**：清单是第二天做差的基准，所以同一天后续推导产出不同的清单**不改写**
   它（只留一行日志）。改写了的话，「机制本来会做的事」就成了一份会自己漂的历史。
4. **`run_day` 从 `payload` 取**：判定层的 `run_day` 是权威（它才决定 `effective_at`，
   也就是记录的唯一键）。本层自己在业务日界附近再取一次 `timezone.now()`，就会落成
   「三值属于 D、行记在 D+1」——**两个值都合法**，没有任何断言会响。
5. **推导层的收场要归一**：正常推导时 `skipped` 是 `None`，模型约定空串表示「正常推导」，
   落库前归一（不然「没跑推导」与「推导正常」在表里长得一样）。

DB 用例一律用真事务回滚的 `TestCase`；判定行是**真的 `RegimeJudgement` 行**——外键指向
它，而「外键非空 ⟺ 三值非空」这条不变量的验证正需要真行。
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from django.test import TestCase

from apps.common.time_utils import business_tz
from apps.regime import config, shadow
from apps.regime.models import (
    Escalation,
    RegimeJudgement,
    ShadowDailyRecord,
    business_midnight,
)
from apps.regime.quant import BaseRegime

#: 判定机制的名义运行时刻：北京 2026-09-22 08:00 == UTC 2026-09-22 00:00。
RUN_DAY = date(2026, 9, 22)
SYMBOL = config.CANDLES.symbol


# --------------------------------------------------------------------------- #
# 造数据：判定 / 推导两个上层各自的返回值
# --------------------------------------------------------------------------- #


def concluded_payload(
    *,
    run_day: date = RUN_DAY,
    regime: BaseRegime | str = BaseRegime.DOWNTREND,
    escalation: str = "",
    effective_regime: str | None = None,
    symbol: str = SYMBOL,
    with_record: bool = True,
) -> dict:
    """`run_daily_judgement()` 成功时的返回值，并按需落一条对应的判定记录。

    记录与返回值**一起造**：它们本来就是同一个东西的两面（判定层先落库、再按库里的
    那一行描述），分开造会让用例能构造出生产上不存在的组合——而本文件恰恰有几条用例
    需要那种不可能的组合（`with_record=False`），所以那个口子是显式的。
    """
    value = regime.value if isinstance(regime, BaseRegime) else regime
    effective_at = business_midnight(run_day + timedelta(days=1))
    if with_record:
        RegimeJudgement.objects.create(
            symbol=symbol,
            attribute_date=run_day - timedelta(days=1),
            effective_at=effective_at,
            base_regime=value,
            escalation=escalation,
            effective_regime=effective_regime or value,
        )
    return {
        "symbol": symbol,
        "run_day": run_day.isoformat(),
        "attribute_date": (run_day - timedelta(days=1)).isoformat(),
        "effective_at": effective_at.isoformat(),
        "base_regime": value,
        "escalation": escalation,
        "effective_regime": effective_regime or value,
        "decision": "adopted",
        "reason": "",
        "recorded": True,
    }


def skipped_payload(
    kind: str = "no_candles",
    *,
    run_day: date = RUN_DAY,
    symbol: str = SYMBOL,
) -> dict:
    """`run_daily_judgement()` 数据不足时的返回值——**没有 `effective_at`，没有三值**。

    三种收场（`no_candles` / `stale_candles` / `undecidable`）的键集刻意不同（`stale_candles`
    多两个日线日期、`undecidable` 多一个分位），这里只留共同的三个：本文件关心的不是
    收场细节，而是「判定没结论时这一行该长什么样」。
    """
    return {"skipped": kind, "symbol": symbol, "run_day": run_day.isoformat()}


def suggestion(strategy_id: str = "s1", regime: str = "downtrend", **over) -> dict:
    """`run_deactivation()` 返回的建议清单里的一条（形状照抄 `deactivation_run`）。"""
    row = {
        "strategy_id": strategy_id,
        "regime": regime,
        "state": "frozen",
        "reason": "该阶段表现不达标",
        "source": "strategy",
        "running": True,
        "exempt": False,
        "created": True,
    }
    row.update(over)
    return row


def derivation(*rows: dict, skipped: str | None = None, note: str = "") -> dict:
    """`run_deactivation()` 的返回值。`skipped` 恒存在（这是它刻意与判定不同的地方）。"""
    return {
        "symbol": SYMBOL,
        "skipped": skipped,
        "note": note,
        "suggestions": list(rows),
    }


# --------------------------------------------------------------------------- #
# 成功路径
# --------------------------------------------------------------------------- #


class TestTheConcludedRow(TestCase):
    """判定出了结论时，这一行抄下三个值，并把外键指向抄来的那一行。"""

    def test_the_three_values_are_inlined(self):
        """三个值都进本地副本——少一个就再也回答不了「那天那个阶段是判出来的还是抬上来的」。"""
        shadow.write_shadow_record(
            concluded_payload(
                regime=BaseRegime.RANGE, escalation=Escalation.NEWS.value
            ),
            derivation(),
        )
        row = ShadowDailyRecord.objects.get(symbol=SYMBOL, run_day=RUN_DAY)
        self.assertEqual(row.base_regime, BaseRegime.RANGE.value)
        self.assertEqual(row.escalation, Escalation.NEWS.value)
        self.assertEqual(row.effective_regime, BaseRegime.RANGE.value)
        # 生效阶段与基础阶段在本用例里相同，所以上面那条断言不足以证明「抄的是生效阶段」
        # 这个字段而不是抄错了列；下面这条把两者分开。
        self.assertNotEqual(row.escalation, row.base_regime)

    def test_the_effective_value_is_not_the_base_value(self):
        """抬升之后的生效阶段与基础阶段必须各存各的。

        合成或省略一个，「那天是被抬上来的吗」就不可回答了。这里刻意让两者不同值。
        """
        shadow.write_shadow_record(
            concluded_payload(
                regime=BaseRegime.UPTREND,
                escalation=Escalation.NEWS.value,
                effective_regime=BaseRegime.HIGH_VOL.value,
            ),
            derivation(),
        )
        row = ShadowDailyRecord.objects.get(symbol=SYMBOL, run_day=RUN_DAY)
        self.assertEqual(row.base_regime, BaseRegime.UPTREND.value)
        self.assertEqual(row.effective_regime, BaseRegime.HIGH_VOL.value)

    def test_the_fk_points_at_the_row_the_values_came_from(self):
        payload = concluded_payload()
        shadow.write_shadow_record(payload, derivation())
        row = ShadowDailyRecord.objects.get(symbol=SYMBOL, run_day=RUN_DAY)
        record = RegimeJudgement.objects.get(
            symbol=SYMBOL, effective_at=datetime.fromisoformat(payload["effective_at"])
        )
        self.assertEqual(row.judgement_id, record.pk)

    def test_the_fk_is_not_the_currently_effective_judgement(self):
        """外键必须指向**待生效**的那条（今天判的），不是今天生效的那条（昨天判的）。

        这条如果不成立，外键与内联三值会天天差一天。造两条判定把两者分开：昨天判的
        （今天生效）与今天判的（明天生效）。
        """
        RegimeJudgement.objects.create(
            symbol=SYMBOL,
            attribute_date=RUN_DAY - timedelta(days=2),
            effective_at=business_midnight(RUN_DAY),
            base_regime=BaseRegime.UPTREND.value,
            effective_regime=BaseRegime.UPTREND.value,
        )
        payload = concluded_payload(regime=BaseRegime.DOWNTREND)
        shadow.write_shadow_record(payload, derivation())

        row = ShadowDailyRecord.objects.get(symbol=SYMBOL, run_day=RUN_DAY)
        self.assertEqual(
            row.judgement.effective_at,
            business_midnight(RUN_DAY + timedelta(days=1)),
            "外键指向的必须是今天判的那条（明早生效），不是今天生效的那条",
        )
        self.assertEqual(row.base_regime, BaseRegime.DOWNTREND.value)
        self.assertEqual(row.judgement.base_regime, row.base_regime)

    def test_the_suggestion_list_is_frozen_with_its_count(self):
        rows = [suggestion("s1"), suggestion("s2", regime="high_vol")]
        shadow.write_shadow_record(concluded_payload(), derivation(*rows))
        row = ShadowDailyRecord.objects.get(symbol=SYMBOL, run_day=RUN_DAY)
        self.assertEqual(row.suggestions, rows)
        self.assertEqual(row.suggested_count, 2)

    def test_the_derivations_own_note_is_carried_verbatim(self):
        """推导层已经给了一句解释，就原样带上——第二处重写迟早会跟它漂开。"""
        shadow.write_shadow_record(
            concluded_payload(),
            derivation(skipped="no_generation", note="还没有任何一代池化表"),
        )
        row = ShadowDailyRecord.objects.get(symbol=SYMBOL, run_day=RUN_DAY)
        self.assertIn("还没有任何一代池化表", row.note)
        self.assertIn(BaseRegime.DOWNTREND.value, row.note)

    def test_a_note_is_synthesised_when_the_derivation_gives_none(self):
        shadow.write_shadow_record(concluded_payload(), derivation())
        row = ShadowDailyRecord.objects.get(symbol=SYMBOL, run_day=RUN_DAY)
        self.assertTrue(row.note)
        self.assertIn("无停用建议", row.note)

    def test_executed_is_always_false(self):
        """Shadow 期机制不施加任何动作——这个字段照落，且第①段恒为 False。"""
        shadow.write_shadow_record(concluded_payload(), derivation(suggestion()))
        row = ShadowDailyRecord.objects.get(symbol=SYMBOL, run_day=RUN_DAY)
        self.assertFalse(row.executed)

    def test_the_summary_says_it_was_written(self):
        summary = shadow.write_shadow_record(
            concluded_payload(), derivation(suggestion(), suggestion("s2"))
        )
        self.assertEqual(summary["outcome"], shadow.OUTCOME_CREATED)
        self.assertTrue(summary["written"])
        self.assertTrue(summary["judged"])
        self.assertIsNone(summary["judgement_skipped"])
        self.assertEqual(summary["derivation_skipped"], "")
        self.assertEqual(summary["suggested_count"], 2)
        self.assertEqual(summary["run_day"], RUN_DAY.isoformat())
        self.assertEqual(summary["symbol"], SYMBOL)


# --------------------------------------------------------------------------- #
# 判定没有结论
# --------------------------------------------------------------------------- #


class TestJudgementWithoutConclusion(TestCase):
    """判定跑了但没表态：照落一行，三个值留空。"""

    def test_the_three_skip_paths_all_land_as_empty_values(self):
        """三种收场都要落行——「判定跑了但机制没表态」正是这张表要能数出来的东西。"""
        for offset, kind in enumerate(("no_candles", "stale_candles", "undecidable")):
            run_day = RUN_DAY + timedelta(days=offset)
            with self.subTest(kind=kind):
                summary = shadow.write_shadow_record(
                    skipped_payload(kind, run_day=run_day), derivation()
                )
                row = ShadowDailyRecord.objects.get(symbol=SYMBOL, run_day=run_day)
                self.assertEqual(row.base_regime, "")
                self.assertEqual(row.escalation, "")
                self.assertEqual(row.effective_regime, "")
                self.assertIsNone(row.judgement_id)
                self.assertEqual(summary["judgement_skipped"], kind)
                self.assertFalse(summary["judged"])
                self.assertTrue(
                    summary["written"], "没有结论也要落行，否则这一天查不出发生过什么"
                )

    def test_the_note_names_the_skip_code(self):
        """那一行要自己说得出为什么是空的——日志不算被看见，`note` 的正经出口是日报。"""
        shadow.write_shadow_record(skipped_payload("stale_candles"), derivation())
        row = ShadowDailyRecord.objects.get(symbol=SYMBOL, run_day=RUN_DAY)
        self.assertIn("stale_candles", row.note)
        self.assertIn("保持上一有效状态", row.note)


class TestDerivationSkippedIsNormalised(TestCase):
    """`skipped` 是 `None`（正常推导）还是码（三种「什么都不动」）——空串只留给前者。"""

    def test_the_three_codes_round_trip(self):
        for offset, code in enumerate(("no_generation", "cold_start", "stale_state")):
            run_day = RUN_DAY + timedelta(days=offset)
            with self.subTest(code=code):
                shadow.write_shadow_record(
                    concluded_payload(run_day=run_day),
                    derivation(skipped=code, note="一句话"),
                )
                row = ShadowDailyRecord.objects.get(symbol=SYMBOL, run_day=run_day)
                self.assertEqual(row.derivation_skipped, code)

    def test_none_becomes_the_empty_string(self):
        """正常推导落成空串。让 `None` 落库的话，CharField 会把两个不同的意思混成一个。"""
        summary = shadow.write_shadow_record(
            concluded_payload(), derivation(skipped=None)
        )
        row = ShadowDailyRecord.objects.get(symbol=SYMBOL, run_day=RUN_DAY)
        self.assertEqual(row.derivation_skipped, "")
        self.assertEqual(summary["derivation_skipped"], "")


# --------------------------------------------------------------------------- #
# 占位与定论
# --------------------------------------------------------------------------- #


class TestTheRowCanBeFilledButNeverBlanked(TestCase):
    """一天里哪一轮心跳的内容留下来，判据只有一条：那一行里有没有结论。"""

    def test_a_placeholder_is_filled_by_a_later_tick(self):
        """业务日 D 的第一轮心跳落在北京 08:00，而 D−1 那根日线正是在那一刻收盘。

        管道晚几分钟，第一轮就回 `stale_candles`。少了「补写」这条，D 这一天在表里就
        永久是一行空三值——而这一整天机制其实是有结论的。
        """
        first = shadow.write_shadow_record(
            skipped_payload("stale_candles"), derivation(skipped="no_generation")
        )
        self.assertEqual(first["outcome"], shadow.OUTCOME_CREATED)
        self.assertFalse(first["judged"])

        payload = concluded_payload()
        second = shadow.write_shadow_record(payload, derivation(suggestion()))
        self.assertEqual(second["outcome"], shadow.OUTCOME_FILLED)
        self.assertTrue(second["written"])
        self.assertEqual(second["suggested_count"], 1)

        row = ShadowDailyRecord.objects.get(symbol=SYMBOL, run_day=RUN_DAY)
        self.assertEqual(row.base_regime, BaseRegime.DOWNTREND.value)
        self.assertEqual(row.effective_regime, BaseRegime.DOWNTREND.value)
        self.assertEqual(row.suggested_count, 1)
        self.assertEqual(
            row.derivation_skipped, "", "补写要把占位那一轮的空收场一起改掉"
        )
        self.assertIsNotNone(row.judgement_id, "补写要把外键一起补上")

    def test_a_conclusion_is_never_blanked_by_a_later_tick(self):
        """反向不补：已有结论之后又来一轮「没有结论」，那一行照旧。

        那是任务停了一天又恢复的形状，不是「今天推翻了今天」。
        """
        shadow.write_shadow_record(concluded_payload(), derivation(suggestion()))
        again = shadow.write_shadow_record(
            skipped_payload("no_candles"), derivation(skipped="stale_state")
        )
        self.assertEqual(again["outcome"], shadow.OUTCOME_KEPT_CONCLUSION)
        self.assertFalse(again["written"])

        row = ShadowDailyRecord.objects.get(symbol=SYMBOL, run_day=RUN_DAY)
        self.assertEqual(row.base_regime, BaseRegime.DOWNTREND.value)
        self.assertEqual(row.suggested_count, 1)
        self.assertEqual(row.derivation_skipped, "")

    def test_a_conclusion_is_not_rewritten_when_suggestions_change(self):
        """清单是第二天做差的基准，所以它冻结——同一天后续推导产出不同清单只留一行日志。

        不冻结的话，「机制本来会做的事」就成了一份会自己漂的历史。
        """
        first = [suggestion("s1"), suggestion("s2"), suggestion("s3")]
        shadow.write_shadow_record(concluded_payload(), derivation(*first))

        with self.assertLogs("apps.regime.shadow", level=logging.INFO) as logs:
            # 同一天第二次成功的判定不新建记录（判定层走 `_describe` 读库里那一行），
            # 所以这里 `with_record=False`——否则造的是一条生产上不存在的第二个判定行。
            again = shadow.write_shadow_record(
                concluded_payload(with_record=False), derivation(suggestion("s9"))
            )

        self.assertEqual(again["outcome"], shadow.OUTCOME_KEPT_CONCLUSION)
        self.assertEqual(
            again["suggested_count"],
            3,
            "摘要说的是现实（库里那一行），不是这一轮的产出",
        )
        row = ShadowDailyRecord.objects.get(symbol=SYMBOL, run_day=RUN_DAY)
        self.assertEqual(row.suggestions, first)
        self.assertTrue(any("冻结" in line for line in logs.output))

    def test_a_placeholder_repeated_is_left_alone(self):
        """占位行遇到「仍然没有结论」的那一轮：不动，也不新建。"""
        shadow.write_shadow_record(skipped_payload("stale_candles"), derivation())
        again = shadow.write_shadow_record(
            skipped_payload("stale_candles"), derivation()
        )
        self.assertEqual(again["outcome"], shadow.OUTCOME_KEPT_PLACEHOLDER)
        self.assertFalse(again["written"])
        self.assertEqual(
            ShadowDailyRecord.objects.filter(symbol=SYMBOL, run_day=RUN_DAY).count(), 1
        )

    def test_one_row_per_day_even_across_many_ticks(self):
        """心跳 5 分钟一轮——一天里绝大多数 tick 都走到这里，必须只留一行。"""
        payload = concluded_payload()
        for _ in range(5):
            shadow.write_shadow_record(payload, derivation())
        self.assertEqual(
            ShadowDailyRecord.objects.filter(symbol=SYMBOL, run_day=RUN_DAY).count(), 1
        )

    def test_two_days_are_two_rows(self):
        """补写只发生在同一天之内：昨天的空行不会被今天的心跳填上。

        填上的话，第②段的今昨做差就会拿今天的内容去比今天的内容。
        """
        shadow.write_shadow_record(
            skipped_payload("no_candles", run_day=RUN_DAY), derivation()
        )
        shadow.write_shadow_record(
            concluded_payload(run_day=RUN_DAY + timedelta(days=1)), derivation()
        )
        self.assertEqual(
            ShadowDailyRecord.objects.get(symbol=SYMBOL, run_day=RUN_DAY).base_regime,
            "",
        )


# --------------------------------------------------------------------------- #
# run_day 的出处
# --------------------------------------------------------------------------- #


class TestRunDayComesFromThePayload(TestCase):
    """`run_day` 的权威是判定层——它才决定 `effective_at`，也就是记录的唯一键。"""

    def test_the_payload_wins_over_the_local_clock(self):
        """本层自己在业务日界附近再取一次 `timezone.now()`，会落成「三值属于 D、行记在 D+1」。

        **两个值都合法**，没有任何断言会响：三值看着对、日期看着对，只是差了一天。
        造法就是让本地时刻落在 D 的 08:00 之后（业务日已是 D+1），而 payload 说 D。
        """
        payload = concluded_payload(run_day=RUN_DAY)
        # 北京 2026-09-23 09:00 —— 本地业务日已是 D+1
        local = datetime(2026, 9, 23, 9, 0, tzinfo=business_tz())
        summary = shadow.write_shadow_record(payload, derivation(), now=local)
        self.assertEqual(summary["run_day"], RUN_DAY.isoformat())
        self.assertTrue(
            ShadowDailyRecord.objects.filter(symbol=SYMBOL, run_day=RUN_DAY).exists()
        )
        self.assertFalse(
            ShadowDailyRecord.objects.filter(
                symbol=SYMBOL, run_day=RUN_DAY + timedelta(days=1)
            ).exists()
        )

    def test_the_local_clock_is_only_a_fallback(self):
        """判定返回值缺 `run_day`（测试 / 数据订正）时才回落到本地时刻，并留痕。"""
        payload = concluded_payload()
        payload.pop("run_day")
        local = datetime(2026, 9, 22, 9, 0, tzinfo=business_tz())
        with self.assertLogs("apps.regime.shadow", level=logging.WARNING):
            summary = shadow.write_shadow_record(payload, derivation(), now=local)
        self.assertEqual(summary["run_day"], RUN_DAY.isoformat())


# --------------------------------------------------------------------------- #
# 取数口径漂开时要看得见
# --------------------------------------------------------------------------- #


class TestMismatchesAreLoud(TestCase):
    """外键只是下钻的线索，真正的不变量是「外键非空 ⟺ 三值非空」。"""

    def test_a_conclusion_without_a_record_still_writes_the_row(self):
        """判定出了结论却找不到对应记录：内联三值照落，只留一行警告。

        真出这种事说明本层的取数口径已经跟判定层漂开（比如有人改了 `effective_at` 的
        算法）——那正是要立刻看见的事，但**不该因此丢掉这一天**。
        """
        payload = concluded_payload(with_record=False)
        with self.assertLogs("apps.regime.shadow", level=logging.WARNING) as logs:
            summary = shadow.write_shadow_record(payload, derivation())
        self.assertTrue(summary["judged"])
        row = ShadowDailyRecord.objects.get(symbol=SYMBOL, run_day=RUN_DAY)
        self.assertEqual(row.base_regime, BaseRegime.DOWNTREND.value)
        self.assertIsNone(row.judgement_id)
        self.assertTrue(any("找不到对应记录" in line for line in logs.output))

    def test_an_inconsistent_record_is_reported_but_the_payload_is_kept(self):
        """内联三值与判定记录不一致：以 `payload` 为准（它是推导那一刻看到的世界），报警。"""
        payload = concluded_payload()
        RegimeJudgement.objects.filter(
            symbol=SYMBOL, effective_at=datetime.fromisoformat(payload["effective_at"])
        ).update(base_regime=BaseRegime.UPTREND.value)

        with self.assertLogs("apps.regime.shadow", level=logging.WARNING) as logs:
            shadow.write_shadow_record(payload, derivation())

        row = ShadowDailyRecord.objects.get(symbol=SYMBOL, run_day=RUN_DAY)
        self.assertEqual(row.base_regime, BaseRegime.DOWNTREND.value)
        self.assertTrue(any("不一致" in line for line in logs.output))
