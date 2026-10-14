"""切片适配层与投递（第①段单元 6ii）。

`test_slice.py` 钉的是**纯函数**的算法（归属、分摊、门槛、四态）；这个文件钉的是
把那个纯函数接进系统的那一层。它要回答的问题与算法无关，而每一个都能悄悄错：

1. **适配层只认 `close` 行，且不重排 FIFO**——`open`/`add` 行被计数挡在外面，
   `close` 行的 `entry_time` 直接取用（它的 FIFO 语义正是归属要的那个时刻，见
   `slicing.py` 的模块 docstring）。重排就是在本层长出第二套成本口径。
2. **未平仓与「平仓行写坏了」是两件事**——前者是常态（回测结束时的持仓），后者是
   数据异常。两者都不进切片，但混成一个数会让「有一笔没平完」看起来像「有一行写脏了」，
   所以后者单独告警。
3. **指纹是窗口内的**——窗口外的标签变了不该让存量切片集体作废。这是「要不要重算」
   的判据，会比全量指纹多跑成百上千次无用重算，而重算入口的全部价值就是「只补该补的」。
4. **`is_stale` 只看输入，不看记录**——`computed_at` 天天在变，它不是输入。拿它当陈旧
   等于每次重算都是全表重写。
5. **投递失败绝不抛**——一次成功的回测不该因为一个附加产物投不出去而被标成失败。
6. **任务以「一批 id」为形状**——批本身就是那个计数，重投整批是安全的收敛策略。
7. **重算入口是两段串联：切片 → 池化**（单元 7）。池化读的就是切片落下的载荷，所以它
   必须排在后面；而它**不能**被切片失败挡住——让一条坏数据否决整张适用性表，比让那张
   表说明「这一轮少看了几个回测」更坏。范围参数只收窄切片那一段（池化是全表的），
   `--dry-run` 下两段都不写（既然一行都不写，就该一行都不写）。

载荷形状（`config_snapshot` 只取 judgement + evidence、`tags.symbol` 恒为判定源 BTC）
也在这里钉住：那两条都是「写错了照样跑」的约定，只有断言能挡住。

DB 用例用真事务回滚的 `TestCase`；纯逻辑一律 `SimpleTestCase`，不碰库。
"""

from __future__ import annotations

import getpass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase
from django.core.management import call_command

from apps.regime import config, pool_rebuild, slicing, tasks
from apps.regime import slice as sl
from apps.regime.judgement import SYMBOL
from apps.regime.models import ActorKind, RebuildStatus, RegimePoolRebuild
from apps.regime.quant import BaseRegime

RANGE = BaseRegime.RANGE
DOWNTREND = BaseRegime.DOWNTREND

#: 12 天窗口，端点 d0 / d11。与 `test_slice.py` 的 `SPLIT_WINDOW` 同形，便于对照。
DAY0 = date(2026, 1, 1)
WINDOW = (DAY0, DAY0 + timedelta(days=11))
CAP = 10000.0

#: 缩小后的门槛（默认 30 笔 / 3 个月要造几百天数据才出结论）。规则一模一样。
SMALL = config.EvidenceConfig(min_trades=3, min_months=1)


def d(n: int) -> date:
    return DAY0 + timedelta(days=n)


def at(n: int, hour: int = 12, tz=timezone.utc) -> datetime:
    return datetime(2026, 1, 1, hour, tzinfo=tz) + timedelta(days=n)


def all_range_tags() -> dict[date, BaseRegime]:
    """窗口内全是箱体震荡。用「单阶段」标签，好让断言的注意力留在适配层。"""
    return {d(i): RANGE for i in range(12)}


def row(
    trade_type: str = "close",
    entry: datetime | None = None,
    exit: datetime | None = None,
    pnl=None,
):
    """`BacktestTrade` 行的替身：适配层只用这四个属性（见 `adapt_trades`）。"""
    return SimpleNamespace(
        trade_type=trade_type,
        entry_time=entry if entry is not None else at(1),
        exit_time=exit,
        pnl=pnl,
    )


def close_row(n: int, pnl=Decimal("100.00")) -> SimpleNamespace:
    """一笔当日开平的已平仓成交。"""
    return row(entry=at(n), exit=at(n), pnl=pnl)


def result_stub(*, start=DAY0, end=WINDOW[1], metrics=None) -> SimpleNamespace:
    """`BacktestResult` 的替身：`is_stale` 只读窗口与 `metrics`。"""
    return SimpleNamespace(start_date=start, end_date=end, metrics=metrics)


# --------------------------------------------------------------------------- #
# 适配层：行 → SliceTrade
# --------------------------------------------------------------------------- #


class TestTradeAdaptation(SimpleTestCase):
    """只认 `close` 行，且 `entry_time` 直接取用（不重排 FIFO）。"""

    def test_a_closed_row_becomes_a_slice_trade(self):
        trades, excluded = slicing.adapt_trades([close_row(1, Decimal("250.00"))])

        self.assertEqual(excluded, 0)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0].pnl, 250.0)
        self.assertEqual(trades[0].entry_time, at(1))

    def test_open_and_add_rows_are_excluded_and_counted(self):
        """一笔从未平仓的交易静默消失，看起来与「这个阶段没有交易」一模一样。"""
        rows = [
            row(trade_type="open", exit=None, pnl=None),
            row(trade_type="add", exit=None, pnl=None),
            close_row(3),
        ]

        trades, excluded = slicing.adapt_trades(rows)

        self.assertEqual(len(trades), 1)
        self.assertEqual(excluded, 2)

    def test_a_close_row_missing_pnl_is_excluded_and_flagged(self):
        """平仓行缺 pnl 是数据异常，与「还没平仓」不是一回事——所以单独吼一声。"""
        rows = [close_row(1, Decimal("100.00")), row(entry=at(2), exit=at(2), pnl=None)]

        with self.assertLogs("apps.regime.slicing", level="WARNING") as captured:
            trades, excluded = slicing.adapt_trades(rows)

        self.assertEqual(len(trades), 1)
        self.assertEqual(excluded, 1)
        self.assertIn("缺 exit_time 或 pnl", "\n".join(captured.output))

    def test_a_close_row_missing_exit_time_is_also_flagged(self):
        rows = [row(entry=at(2), exit=None, pnl=Decimal("50.00"))]

        with self.assertLogs("apps.regime.slicing", level="WARNING"):
            trades, excluded = slicing.adapt_trades(rows)

        self.assertEqual(trades, [])
        self.assertEqual(excluded, 1)

    def test_pnl_is_carried_as_float(self):
        """Decimal(20,2) 转 float：切片全程是统计计算，最终要进 JSON。"""
        trades, _ = slicing.adapt_trades([close_row(1, Decimal("-123.45"))])

        self.assertIsInstance(trades[0].pnl, float)
        self.assertAlmostEqual(trades[0].pnl, -123.45)

    def test_a_naive_moment_is_read_as_utc(self):
        """naive 交给 `astimezone()` 会按进程 TZ 解释，而进程 TZ 是 Django 的副作用。"""
        naive = datetime(2026, 1, 2, 12, 0)

        trades, _ = slicing.adapt_trades([row(entry=naive, exit=naive, pnl=1)])

        self.assertEqual(trades[0].entry_time.tzinfo, timezone.utc)
        self.assertEqual(trades[0].entry_time.hour, 12)

    def test_an_aware_moment_from_another_offset_is_normalised_to_utc(self):
        """DB 读回来是 UTC 瞬时，但写进来的可能是 +08:00——口径必须归一。"""
        beijing = timezone(timedelta(hours=8))
        moment = datetime(2026, 1, 2, 20, 0, tzinfo=beijing)

        trades, _ = slicing.adapt_trades([row(entry=moment, exit=moment, pnl=1)])

        self.assertEqual(
            trades[0].entry_time, datetime(2026, 1, 2, 12, 0, tzinfo=timezone.utc)
        )

    def test_an_empty_batch_is_not_an_error(self):
        self.assertEqual(slicing.adapt_trades([]), ([], 0))


# --------------------------------------------------------------------------- #
# 标签指纹
# --------------------------------------------------------------------------- #


class TestFingerprint(SimpleTestCase):
    """窗口内的哈希：窗口外的标签变了不算变。"""

    def test_labels_outside_the_window_do_not_move_the_fingerprint(self):
        inside = {d(i): RANGE for i in range(12)}
        with_outside = dict(inside)
        with_outside[d(-5)] = DOWNTREND
        with_outside[d(40)] = DOWNTREND

        self.assertEqual(
            slicing.tag_fingerprint(inside, WINDOW),
            slicing.tag_fingerprint(with_outside, WINDOW),
        )

    def test_a_label_inside_the_window_does_move_it(self):
        before = {d(i): RANGE for i in range(12)}
        after = dict(before)
        after[d(4)] = DOWNTREND

        self.assertNotEqual(
            slicing.tag_fingerprint(before, WINDOW),
            slicing.tag_fingerprint(after, WINDOW),
        )

    def test_the_fingerprint_does_not_depend_on_construction_order(self):
        forward = {d(i): RANGE for i in range(12)}
        backward = {d(i): RANGE for i in range(11, -1, -1)}

        self.assertEqual(
            slicing.tag_fingerprint(forward, WINDOW),
            slicing.tag_fingerprint(backward, WINDOW),
        )

    def test_an_empty_window_has_a_stable_fingerprint(self):
        """空窗口不是「没有指纹」，而是一个表达「这几天没有任何标签」的合法值。"""
        empty = slicing.tag_fingerprint({}, WINDOW)

        self.assertEqual(len(empty), slicing.FINGERPRINT_LENGTH)
        self.assertEqual(empty, slicing.tag_fingerprint({}, WINDOW))
        self.assertNotEqual(empty, slicing.tag_fingerprint({d(0): RANGE}, WINDOW))

    def test_the_window_actually_narrows_the_hash(self):
        """同一份标签、两个不同的窗口 = 两个指纹（否则窗口边界就没被看着）。"""
        tags = {d(i): RANGE for i in range(12)}

        self.assertNotEqual(
            slicing.tag_fingerprint(tags, (DAY0, DAY0 + timedelta(days=5))),
            slicing.tag_fingerprint(tags, WINDOW),
        )


# --------------------------------------------------------------------------- #
# 陈旧判定
# --------------------------------------------------------------------------- #


class TestStaleness(SimpleTestCase):
    """三条输入各看一条，记录类字段一律不看。"""

    def _stored(self, **overrides) -> dict:
        payload = {
            "version": sl.SLICE_VERSION,
            "evidence_threshold": {"min_trades": 3, "min_months": 1},
            "computed_at": "2020-01-01T00:00:00+00:00",
            "config_snapshot": {},
            "tags": {
                "symbol": SYMBOL,
                "fingerprint": slicing.tag_fingerprint(all_range_tags(), WINDOW),
            },
        }
        payload.update(overrides)
        return payload

    def test_no_slice_at_all_is_stale(self):
        self.assertTrue(
            slicing.is_stale(result_stub(metrics={}), all_range_tags(), SMALL)
        )

    def test_a_missing_metrics_field_is_not_an_error(self):
        self.assertTrue(
            slicing.is_stale(result_stub(metrics=None), all_range_tags(), SMALL)
        )

    def test_a_matching_slice_is_not_stale(self):
        stored = self._stored()
        result = result_stub(metrics={slicing.SLICE_KEY: stored})

        self.assertFalse(slicing.is_stale(result, all_range_tags(), SMALL))

    def test_a_version_bump_makes_everything_stale(self):
        stored = self._stored(version=sl.SLICE_VERSION + 1)
        result = result_stub(metrics={slicing.SLICE_KEY: stored})

        self.assertTrue(slicing.is_stale(result, all_range_tags(), SMALL))

    def test_a_label_change_inside_the_window_makes_it_stale(self):
        stored = self._stored()
        result = result_stub(metrics={slicing.SLICE_KEY: stored})
        moved = all_range_tags() | {d(3): DOWNTREND}

        self.assertTrue(slicing.is_stale(result, moved, SMALL))

    def test_a_label_change_outside_the_window_does_not(self):
        """存量切片不该因为窗口之外的历史被重算而集体作废。"""
        stored = self._stored()
        result = result_stub(metrics={slicing.SLICE_KEY: stored})
        moved = all_range_tags() | {d(-30): DOWNTREND, d(300): DOWNTREND}

        self.assertFalse(slicing.is_stale(result, moved, SMALL))

    def test_a_threshold_change_makes_it_stale(self):
        stored = self._stored()
        result = result_stub(metrics={slicing.SLICE_KEY: stored})
        stricter = config.EvidenceConfig(min_trades=30, min_months=3)

        self.assertTrue(slicing.is_stale(result, all_range_tags(), stricter))

    def test_only_min_months_changing_is_enough(self):
        stored = self._stored()
        result = result_stub(metrics={slicing.SLICE_KEY: stored})

        self.assertTrue(
            slicing.is_stale(
                result,
                all_range_tags(),
                config.EvidenceConfig(min_trades=3, min_months=3),
            )
        )

    def test_a_missing_threshold_block_is_stale(self):
        """老载荷没有这一块：读作「不知道当初用的什么门槛」→ 重算。"""
        stored = self._stored()
        del stored["evidence_threshold"]
        result = result_stub(metrics={slicing.SLICE_KEY: stored})

        self.assertTrue(slicing.is_stale(result, all_range_tags(), SMALL))

    def test_computed_at_alone_does_not_make_it_stale(self):
        """**这一条是重算入口存在的理由**：算得早不是陈旧，否则每次都是全表重写。"""
        stored = self._stored(computed_at="1999-01-01T00:00:00+00:00")
        result = result_stub(metrics={slicing.SLICE_KEY: stored})

        self.assertFalse(slicing.is_stale(result, all_range_tags(), SMALL))

    def test_the_config_snapshot_alone_does_not_make_it_stale(self):
        stored = self._stored(config_snapshot={"judgement": {"warmup_days": 999}})
        result = result_stub(metrics={slicing.SLICE_KEY: stored})

        self.assertFalse(slicing.is_stale(result, all_range_tags(), SMALL))


# --------------------------------------------------------------------------- #
# 载荷
# --------------------------------------------------------------------------- #


class TestPayloadEnvelope(SimpleTestCase):
    """`slice_result` 盖上去的那四样，每一样都有人依赖。"""

    def _payload(self, *, rows=None, tags=None, params=SMALL) -> dict:
        return slicing.slice_result(
            rows=rows if rows is not None else [close_row(1, Decimal("100.00"))],
            window=WINDOW,
            initial_capital=CAP,
            tags=tags if tags is not None else all_range_tags(),
            params=params,
            now=datetime(2026, 9, 22, 0, 0, tzinfo=timezone.utc),
        )

    def test_computed_at_is_an_iso_instant_we_can_read_back(self):
        payload = self._payload()

        self.assertEqual(
            datetime.fromisoformat(payload["computed_at"]),
            datetime(2026, 9, 22, 0, 0, tzinfo=timezone.utc),
        )

    def test_the_snapshot_covers_judgement_and_evidence_but_not_news(self):
        """资讯那组不是切片的输入；把它塞进来，改一个关键词就会让历史切片全体作废。"""
        snapshot = self._payload()["config_snapshot"]

        self.assertEqual(sorted(snapshot), ["evidence", "judgement"])

    def test_the_snapshot_records_the_config_face_it_ran_against(self):
        """快照取的是**配置面当期值**，不是调用方传进来的那次覆盖。

        `params` 是给批量重算/测试用的显式覆盖，它落进 `evidence_threshold`（那才是
        「这次用的门槛」）；快照回答的是另一个问题——「当期系统是按什么跑的」。两者
        在覆盖路径上会不一致，而这是有意的：覆盖不该伪装成配置面改过。
        """
        snapshot = self._payload()["config_snapshot"]

        self.assertEqual(snapshot["evidence"]["min_trades"], config.EVIDENCE.min_trades)
        self.assertEqual(snapshot["evidence"]["min_months"], config.EVIDENCE.min_months)

    def test_the_threshold_block_is_named_evidence_threshold(self):
        """不叫 `params`：`config_snapshot.judgement` 里已经有一个 `params` 了。"""
        payload = self._payload()

        self.assertEqual(
            payload["evidence_threshold"], {"min_trades": 3, "min_months": 1}
        )
        self.assertNotIn("params", payload)

    def test_the_tags_block_says_where_the_labels_came_from(self):
        """判定源恒为 BTC，与回测的 symbol 无关（阶段是全市场的宏观描述）。"""
        block = self._payload()["tags"]

        self.assertEqual(block["symbol"], SYMBOL)
        self.assertEqual(block["count"], 12)
        self.assertEqual(block["first"], DAY0.isoformat())
        self.assertEqual(block["last"], WINDOW[1].isoformat())
        self.assertEqual(
            block["fingerprint"], slicing.tag_fingerprint(all_range_tags(), WINDOW)
        )

    def test_the_tags_block_survives_an_empty_label_set(self):
        """日线还没回填时照落载荷（「算过，结论是无从判断」与「从没算过」必须能分开）。"""
        with self.assertLogs("apps.regime.slicing", level="WARNING"):
            payload = self._payload(tags={})

        self.assertIsNone(payload["tags"]["first"])
        self.assertIsNone(payload["tags"]["last"])
        self.assertEqual(payload["tags"]["count"], 0)
        self.assertEqual(payload["attribution"]["window_days"], 0)

    def test_an_empty_label_set_is_loud(self):
        """全 unknown 看起来像算法坏了，而成因通常是日线没回填。"""
        with self.assertLogs("apps.regime.slicing", level="WARNING") as captured:
            self._payload(tags={})

        self.assertIn("日线回填", "\n".join(captured.output))

    def test_the_excluded_count_is_filled_in_by_the_adapter(self):
        """`slice_backtest` 看不到未平仓的行，占位由本层补上。"""
        payload = self._payload(
            rows=[
                row(trade_type="open", exit=None, pnl=None),
                close_row(1, Decimal("100.00")),
            ]
        )

        self.assertEqual(payload["attribution"]["open_trades_excluded"], 1)
        self.assertEqual(payload["attribution"]["closed_trades"], 1)

    def test_the_threshold_travels_with_the_conclusion(self):
        payload = self._payload(
            params=config.EvidenceConfig(min_trades=30, min_months=3)
        )

        self.assertEqual(
            payload["evidence_threshold"], {"min_trades": 30, "min_months": 3}
        )


# --------------------------------------------------------------------------- #
# 投递
# --------------------------------------------------------------------------- #


class TestDispatchNeverRaises(SimpleTestCase):
    """调用点是回测的收尾：投递失败不能把成功回测变成失败回测。"""

    def test_an_empty_batch_is_not_dispatched(self):
        with patch.object(tasks.compute_regime_slice_task, "delay") as delay:
            self.assertFalse(tasks.dispatch_slice([]))
            self.assertFalse(tasks.dispatch_slice(None))

        delay.assert_not_called()

    def test_a_batch_is_dispatched_as_one_task(self):
        with patch.object(tasks.compute_regime_slice_task, "delay") as delay:
            self.assertTrue(tasks.dispatch_slice(["a", "b"]))

        delay.assert_called_once_with(["a", "b"])

    def test_a_broker_failure_is_swallowed_and_logged(self):
        with patch.object(
            tasks.compute_regime_slice_task,
            "delay",
            side_effect=RuntimeError("broker down"),
        ):
            with self.assertLogs("apps.regime.tasks", level="ERROR") as captured:
                self.assertFalse(tasks.dispatch_slice(["a"]))

        self.assertIn("投递失败", "\n".join(captured.output))

    def test_a_single_id_is_normalised_into_a_list(self):
        with patch.object(tasks.compute_regime_slice_task, "delay") as delay:
            tasks.dispatch_slice("abc")

        delay.assert_called_once_with(["abc"])

    def test_empty_ids_are_dropped_rather_than_dispatched(self):
        with patch.object(tasks.compute_regime_slice_task, "delay") as delay:
            self.assertTrue(tasks.dispatch_slice(["a", None, "", "b"]))

        delay.assert_called_once_with(["a", "b"])

    def test_id_normalisation_coerces_to_str(self):
        self.assertEqual(tasks._as_id_list([1, "b"]), ["1", "b"])
        self.assertEqual(tasks._as_id_list([]), [])


# --------------------------------------------------------------------------- #
# 落库与任务（碰 DB）
# --------------------------------------------------------------------------- #


class _StubbedConnectionReset:
    """把长循环里的 `close_old_connections()` 换成假的。

    它在 `TestCase` 里**必然**把测试事务的连接关掉：Django 5 的
    `close_if_unusable_or_obsolete` 第一条检查就是「连接的 autocommit 状态与设置不符
    就关连接」，而测试事务里 autocommit 恒为 False → 关 → 之后的 ORM 全炸
    （`InterfaceError: connection already closed`）。生产里没有外层事务，它是正确且
    必要的（见 CLAUDE.md 的长循环连接纪律），所以测试里只换掉它，**另用一条用例断言
    它确实被调到了**，免得把「删掉这行」这种回归一起放行。

    打的是 `django.db.close_old_connections`（**定义处**）：受它盖住的模块都在**函数体内**
    `from django.db import ...`，模块命名空间里没有第二个名字可打。于是这条桩的有效性
    依赖一件事——**受测模块不许在模块顶部绑定这个名字**。绑了的话它自己那份绑定打不着，
    真函数照跑，上面那条 `InterfaceError` 会以一种「看哪个测试模块先导入」的方式回来
    （`recompute_regime_slices` 当初就是这么写坏的）。那条命令的模块 docstring 与调用处
    各留了一处注释，就是为了挡住下一次。
    """

    def setUp(self):
        super().setUp()
        patcher = patch("django.db.close_old_connections")
        patcher.start()
        self.addCleanup(patcher.stop)


class TestMaterialisation(TestCase):
    @classmethod
    def setUpTestData(cls):
        from apps.trading.models import Strategy

        cls.strategy = Strategy.objects.create(
            name="SliceTest", code_path="/test", git_commit_hash="abc123"
        )

    def make_result(self, *, start=DAY0, end=WINDOW[1], metrics=None, trades=2):
        from apps.backtest.models import BacktestResult

        result = BacktestResult.objects.create(
            strategy=self.strategy,
            symbol="BTC/USDT",
            timeframe="1d",
            start_date=start,
            end_date=end,
            initial_capital=Decimal("10000.00"),
            final_capital=Decimal("10500.00"),
            total_return_pct=5.0,
            metrics=metrics if metrics is not None else {},
        )
        for i in range(trades):
            self.add_trade(result, day=i + 1, pnl=Decimal("100.00"))
        return result

    def add_trade(self, result, *, day, pnl, trade_type="close"):
        from apps.backtest.models import BacktestTrade

        return BacktestTrade.objects.create(
            backtest=result,
            entry_time=at(day),
            exit_time=at(day) if trade_type == "close" else None,
            side="long",
            entry_price=Decimal("100.00000000"),
            exit_price=Decimal("101.00000000") if trade_type == "close" else None,
            quantity=Decimal("1.00000000"),
            pnl=pnl if trade_type == "close" else None,
            trade_type=trade_type,
        )

    def test_save_slice_replaces_only_its_own_key(self):
        """`metrics` 里还要放别的东西；读-改-写整个字段不能顺手抹掉别人的键。"""
        result = self.make_result(metrics={"sharpe": 1.5, "note": "keep me"})

        slicing.save_slice(result.id, {"version": sl.SLICE_VERSION})

        result.refresh_from_db()
        self.assertEqual(result.metrics["sharpe"], 1.5)
        self.assertEqual(result.metrics["note"], "keep me")
        self.assertEqual(
            result.metrics[slicing.SLICE_KEY], {"version": sl.SLICE_VERSION}
        )

    def test_compute_slice_writes_the_payload_it_returns(self):
        result = self.make_result()

        payload = slicing.compute_slice(result, all_range_tags(), params=SMALL)

        result.refresh_from_db()
        self.assertEqual(slicing.stored_slice(result), payload)
        self.assertEqual(payload["cells"]["range"]["trades"], 2)

    def test_compute_slice_skips_the_open_rows_of_an_unclosed_position(self):
        result = self.make_result(trades=1)
        self.add_trade(result, day=5, pnl=None, trade_type="open")

        payload = slicing.compute_slice(result, all_range_tags(), params=SMALL)

        self.assertEqual(payload["attribution"]["closed_trades"], 1)
        self.assertEqual(payload["attribution"]["open_trades_excluded"], 1)

    def test_a_second_compute_is_not_stale_and_so_is_skipped(self):
        result = self.make_result()

        slicing.compute_slice(result, all_range_tags(), params=SMALL)
        result.refresh_from_db()

        self.assertFalse(slicing.is_stale(result, all_range_tags(), SMALL))


class TestSliceTask(_StubbedConnectionReset, TestCase):
    """任务以「一批 id」为形状：批就是那个计数，重投整批是安全的收敛策略。"""

    @classmethod
    def setUpTestData(cls):
        from apps.trading.models import Strategy

        cls.strategy = Strategy.objects.create(
            name="SliceTask", code_path="/test", git_commit_hash="abc123"
        )

    def make_result(self, **overrides):
        from apps.backtest.models import BacktestResult

        fields = {
            "strategy": self.strategy,
            "symbol": "BTC/USDT",
            "timeframe": "1d",
            "start_date": DAY0,
            "end_date": WINDOW[1],
            "initial_capital": Decimal("10000.00"),
            "final_capital": Decimal("10500.00"),
            "total_return_pct": 5.0,
            "metrics": {},
        }
        fields.update(overrides)
        return BacktestResult.objects.create(**fields)

    def test_an_empty_batch_is_a_clean_no_op(self):
        """收敛闸：没东西可切就干净地结束——这正是「重投一批已完成的」该有的样子。"""
        self.assertEqual(
            tasks.compute_regime_slice_task([]),
            {"sliced": 0, "skipped": 0, "missing": [], "failed": []},
        )
        self.assertEqual(
            tasks.compute_regime_slice_task(None),
            {"sliced": 0, "skipped": 0, "missing": [], "failed": []},
        )

    def test_a_fresh_result_gets_a_payload(self):
        result = self.make_result()

        summary = tasks.compute_regime_slice_task([str(result.id)])

        self.assertEqual(summary["sliced"], 1)
        result.refresh_from_db()
        self.assertIsNotNone(slicing.stored_slice(result))

    def test_a_vanished_result_is_recorded_not_raised(self):
        """「投了却找不到」与「投漏了」看起来一样，所以它得进 summary。"""
        ghost = "00000000-0000-0000-0000-000000000000"

        summary = tasks.compute_regime_slice_task([ghost])

        self.assertEqual(summary["missing"], [ghost])
        self.assertEqual(summary["sliced"], 0)

    def test_an_up_to_date_result_is_skipped_on_a_second_pass(self):
        result = self.make_result()

        tasks.compute_regime_slice_task([str(result.id)])
        summary = tasks.compute_regime_slice_task([str(result.id)])

        self.assertEqual(
            summary, {"sliced": 0, "skipped": 1, "missing": [], "failed": []}
        )

    def test_only_stale_off_forces_a_rewrite(self):
        result = self.make_result()

        tasks.compute_regime_slice_task([str(result.id)])
        summary = tasks.compute_regime_slice_task([str(result.id)], only_stale=False)

        self.assertEqual(summary["sliced"], 1)

    def test_the_batch_loop_refreshes_the_connection_per_item(self):
        """长循环的连接纪律（CLAUDE.md）：批可能上百条，每条都在上一次事务边界之外。

        这一条单独断言「确实调了」——上面所有用例都把它换成了假的，删掉那行也不会红。
        """
        first, second = self.make_result(), self.make_result()

        with patch("django.db.close_old_connections") as reset:
            tasks.compute_regime_slice_task([str(first.id), str(second.id)])

        self.assertEqual(reset.call_count, 2)

    def test_a_broken_result_does_not_stop_the_rest_of_the_batch(self):
        """坏的那一条不该挡住其余几百条；批末统一决定要不要重投。"""
        good = self.make_result()
        bad = self.make_result()
        real_compute = slicing.compute_slice
        calls = []

        def flaky_compute(result, tags=None, **kwargs):
            calls.append(str(result.id))
            if str(result.id) == str(bad.id):
                raise ValueError("坏数据")
            return real_compute(result, tags, **kwargs)

        with patch("apps.regime.slicing.compute_slice", side_effect=flaky_compute):
            with self.assertLogs("apps.regime.tasks", level="ERROR"):
                # 直接调用（非 worker）时 Celery 把重试原样抛回来——这正是「交给
                # Celery 决定重试」在测试里的样子（见 tasks.py 的失败可见性一节）。
                with self.assertRaises(RuntimeError):
                    tasks.compute_regime_slice_task([str(bad.id), str(good.id)])

        self.assertEqual(len(calls), 2)
        good.refresh_from_db()
        self.assertIsNotNone(slicing.stored_slice(good))


class TestRecomputeCommand(_StubbedConnectionReset, TestCase):
    """全量重算入口：「全量」是把全部结果过一遍，不是无条件全部重写。"""

    @classmethod
    def setUpTestData(cls):
        from apps.trading.models import Strategy

        cls.strategy = Strategy.objects.create(
            name="RecomputeTest", code_path="/test", git_commit_hash="abc123"
        )

    def make_result(self, **overrides):
        from apps.backtest.models import BacktestResult

        fields = {
            "strategy": self.strategy,
            "symbol": "BTC/USDT",
            "timeframe": "1d",
            "start_date": DAY0,
            "end_date": WINDOW[1],
            "initial_capital": Decimal("10000.00"),
            "final_capital": Decimal("10500.00"),
            "total_return_pct": 5.0,
            "metrics": {},
        }
        fields.update(overrides)
        return BacktestResult.objects.create(**fields)

    def run_command(self, *args):
        out = StringIO()
        call_command("recompute_regime_slices", *args, stdout=out)
        return out.getvalue()

    def test_a_scope_is_required(self):
        """没有范围就全表重写——那是误操作，不是默认值。"""
        with self.assertRaises(CommandError):
            self.run_command()

    def test_all_slices_every_result(self):
        a, b = self.make_result(), self.make_result()

        report = self.run_command("--all")

        self.assertIn("重算 2", report)
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertIsNotNone(slicing.stored_slice(a))
        self.assertIsNotNone(slicing.stored_slice(b))

    def test_the_scan_never_hauls_the_heavy_columns(self):
        """`--all` 跑不跑得动就压在那行 `defer` 上，所以它得有测试看着。

        判据取**行上真的被推迟了哪些列**（`get_deferred_fields`），不取内存、也不取
        SQL 文本：内存随批量与 GC 浮动，做不了断言；SQL 文本则是绕远路去猜同一件事。
        这条读法直接问「进到扫描循环里的行，是不是真的没把那四列搬进来」——`defer`
        被删掉、被 `.only()` 顶掉、或者哪天被挪到只有一条分支生效的位置，它都会红。

        `BacktestResult` 这四个大列（`HEAVY_COLUMNS`）每行约 600 KB，全表 4067 行
        超过容器 2 GiB 上限，越限的现场是「进程卡住不动」而不是一条报错——所以要一条
        测试，而不是靠下一个人记得。要往这条扫描里加列，先把那条注释读一遍。
        """
        from apps.regime.management.commands.recompute_regime_slices import (
            HEAVY_COLUMNS,
        )

        self.make_result()
        seen: list[set] = []
        real_is_stale = slicing.is_stale

        def spy(row, tags, params=None):
            seen.append(row.get_deferred_fields())
            return real_is_stale(row, tags, params)

        # 打 `is_stale` 而不是打个「顺路看一眼」的桩：它是每一行都要过的那道闸，而且它
        # 拿到的是**iterator 交出来的那个实例**——正是要断言的对象。
        with patch("apps.regime.slicing.is_stale", side_effect=spy):
            self.run_command("--all")

        self.assertTrue(seen, "这次运行一行都没经过扫描循环，下面的断言无意义")
        missing = [sorted(set(HEAVY_COLUMNS) - deferred) for deferred in seen]
        self.assertEqual(
            [m for m in missing if m],
            [],
            f"有行没把那四个大列推迟掉：{missing}（见 recompute_regime_slices.HEAVY_COLUMNS）",
        )

    def test_a_second_run_over_the_same_set_skips_everything(self):
        """幂等：反复运行不产生副作用。"""
        self.make_result()
        self.run_command("--all")

        report = self.run_command("--all")

        self.assertIn("重算 0", report)
        self.assertIn("跳过（不陈旧）1", report)

    def test_force_rewrites_even_the_fresh_ones(self):
        result = self.make_result()
        self.run_command("--all")
        result.refresh_from_db()
        first = slicing.stored_slice(result)

        report = self.run_command("--all", "--force")

        # 判据是「重算而不是跳过」（比 `computed_at` 之类的记录字段稳），
        # 但载荷确实被换过也要看住——否则「报重算、其实没写」照样绿。
        self.assertIn("重算 1", report)
        result.refresh_from_db()
        second = slicing.stored_slice(result)
        self.assertEqual(second["version"], first["version"])
        self.assertIsNotNone(second)

    def test_a_forced_rewrite_does_not_lose_neighbouring_metric_keys(self):
        """`metrics` 是别人的字段容器，重算切片只能动自己那一格。"""
        result = self.make_result(metrics={"sharpe": 1.5})

        self.run_command("--all", "--force")

        result.refresh_from_db()
        self.assertEqual(result.metrics["sharpe"], 1.5)
        self.assertIsNotNone(slicing.stored_slice(result))

    def test_dry_run_writes_nothing(self):
        result = self.make_result()

        report = self.run_command("--all", "--dry-run")

        self.assertIn("[dry-run]", report)
        result.refresh_from_db()
        self.assertIsNone(slicing.stored_slice(result))
        # 池化那一段也一行不写。一个「不写库」的开关下面挂两段，第二段偷偷写，
        # 是那种跑完才发现「dry-run 把表换代了」的故障。
        self.assertIn("会全表重建池化表", report)
        self.assertEqual(RegimePoolRebuild.objects.count(), 0)

    def test_only_the_named_result_is_touched(self):
        named, other = self.make_result(), self.make_result()

        report = self.run_command("--result", str(named.id))

        self.assertIn("重算 1", report)
        named.refresh_from_db()
        other.refresh_from_db()
        self.assertIsNotNone(slicing.stored_slice(named))
        self.assertIsNone(slicing.stored_slice(other))

    def test_a_strategy_name_selects_its_results(self):
        from apps.trading.models import Strategy

        other_strategy = Strategy.objects.create(
            name="OtherStrategy", code_path="/test", git_commit_hash="abc123"
        )
        mine = self.make_result()
        theirs = self.make_result(strategy=other_strategy)

        self.run_command("--strategy", "RecomputeTest")

        mine.refresh_from_db()
        theirs.refresh_from_db()
        self.assertIsNotNone(slicing.stored_slice(mine))
        self.assertIsNone(slicing.stored_slice(theirs))

    def test_limit_caps_the_batch(self):
        self.make_result()
        self.make_result()

        report = self.run_command("--all", "--limit", "1")

        self.assertIn("重算 1", report)

    def test_an_unknown_id_is_a_quiet_zero(self):
        report = self.run_command("--result", "00000000-0000-0000-0000-000000000000")

        self.assertIn("重算 0", report)

    def test_one_bad_result_does_not_stop_the_batch_but_does_fail_the_command(self):
        self.make_result()
        self.make_result()

        with patch(
            "apps.regime.slicing.compute_slice", side_effect=ValueError("坏数据")
        ):
            with self.assertRaises(CommandError):
                self.run_command("--all")

    # -- 池化那一段（单元 7） --------------------------------------------- #

    def test_a_slice_failure_does_not_stop_the_pool_stage(self):
        """一条坏切片不该否决整张适用性表：池化天生容忍样本缺口。

        缺口本身是有出口的——没切片的那些进了 `exclusions` 落在这一代上，命令还会
        额外说一句「这一代是按部分刷新的输入建的」。顺序上池化必须排在抛出之前，
        否则「切片有一条坏的」就等于「池化表停在上一代」，而那正是这张表最没用的时刻。
        """
        self.make_result()
        out = StringIO()

        with patch(
            "apps.regime.slicing.compute_slice", side_effect=ValueError("坏数据")
        ):
            with self.assertRaises(CommandError):
                call_command("recompute_regime_slices", "--all", stdout=out)

        report = out.getvalue()
        self.assertIn("失败 1", report)
        self.assertIn("池化：", report)
        self.assertIn("按部分刷新的输入建的", report)

        generation = RegimePoolRebuild.objects.get()
        self.assertEqual(generation.status, RebuildStatus.READY.value)
        self.assertEqual(generation.candidates, 1)
        self.assertEqual(generation.results_used, 0)

    def test_the_actor_is_recorded_on_the_generation(self):
        """Q8：重算记录要回答「谁让它重算的」，否则「昨天还好好的」无从追起。"""
        self.make_result()

        self.run_command("--all", "--actor", "xl")

        generation = RegimePoolRebuild.objects.get()
        self.assertEqual(generation.actor_kind, ActorKind.CLI.value)
        self.assertEqual(generation.actor_name, "xl")

    def test_the_actor_defaults_to_the_system_user(self):
        self.make_result()

        self.run_command("--all")

        self.assertEqual(RegimePoolRebuild.objects.get().actor_name, getpass.getuser())

    def test_the_registry_is_refreshed_at_the_entry_point(self):
        """注册表的新鲜度由**入口**负责：`apps.ready()` 只在目录当时存在时才 discover。

        长驻进程在 `~/.tradelogx/strategies` 建起来之前就启动的话，整批策略解析不到
        实现类、全落到「未归类」兜底——池化照跑、日报照出，只有一个数字在说话。
        """
        self.make_result()

        with patch.object(pool_rebuild, "ensure_strategies_discovered") as discover:
            self.run_command("--all")

        discover.assert_called_once_with()

    def test_the_range_parameters_do_not_narrow_the_pool_stage(self):
        """`--result` 只收窄切片那一段：池化是全表的，少收窄一次就换代换代到一半。"""
        named, other = self.make_result(), self.make_result()

        report = self.run_command("--result", str(named.id), "--limit", "1")

        self.assertIn("全表重建", report)
        # 候选数看的是**全部**回测结果，不是被 --result/--limit 收窄后的那一条。
        self.assertEqual(RegimePoolRebuild.objects.get().candidates, 2)
        named.refresh_from_db()
        other.refresh_from_db()
        self.assertIsNotNone(slicing.stored_slice(named))
        self.assertIsNone(slicing.stored_slice(other))
