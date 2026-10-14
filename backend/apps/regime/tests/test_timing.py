"""判定链路的时区与时刻不变量（第①段单元 4）。

这个文件测的不是「某个常量等于某个值」，而是**「北京 08:00 出结论、次日 08:00 生效」
这句话在时刻上是否真的成立**。它成立靠三层：

1. 业务时区的日界与日线换线时刻是**同一个绝对时刻**（北京 08:00 == UTC 00:00）。
   少了这条，签署日 / 运行日 / 生效时刻就会错开半天，而错开的表现是切片把结论归到
   相邻的一天——完全看不出来。
2. 判定挂在那条既有的 5 分钟心跳上（`snapshot_daily_equity`），所以日界之后必然很快
   出结论，且心跳的既有职责（日度权益快照）不被新职责顶掉——停用决策推导、Shadow 每日
   记录、日报都搭在同一条心跳上，顺序是**上下游**而不是并列的：判定 → 推导（读判定刚
   落下的「当前生效阶段」）→ Shadow（要落的建议清单正是推导的产物）→ 日报（第②段与
   Shadow 同源，还要读昨天那份日报做差）。
3. 心跳的节拍足够密，日界不会被跳过。

时区这条特别容易在后面被「简化」掉：把 `BUSINESS_TIMEZONE` 设成 UTC 能让所有
`to_business()` 变成空操作，代码看起来更简单，而 08:00 与换线时刻的对应关系就断了。
所以这里钉的是**偏移量与绝对时刻**，不是时区名字——换成任何 +08:00 的时区都应当通过。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from django.conf import settings
from django.test import TestCase
from django.utils import timezone as django_timezone

from apps.common.time_utils import business_tz, business_tz_name
from apps.regime.candles import latest_complete_date
from apps.regime.models import business_midnight

#: 判定机制的名义运行时刻：北京 2026-09-22 08:00 == UTC 2026-09-22 00:00。
RUN_DAY = date(2026, 9, 22)


class TestBusinessDayBoundaryIsTheCandleBoundary(TestCase):
    """业务日界（08:00 北京）必须落在日线换线的那一刻上。"""

    def test_business_midnight_is_exactly_utc_midnight(self):
        for day in (date(2026, 1, 1), date(2026, 9, 22), date(2027, 3, 1)):
            with self.subTest(day=day):
                moment = business_midnight(day)
                self.assertEqual(
                    moment,
                    datetime(day.year, day.month, day.day, tzinfo=timezone.utc),
                    "业务日界必须等于该自然日的 UTC 00:00（日线换线时刻），"
                    "否则签署日/运行日/生效时刻会整体错开",
                )

    def test_business_timezone_is_utc_plus_eight(self):
        """偏移量是 +08:00 才成立；名字无所谓。

        断言偏移而不是断言时区名：任何 +08:00 的时区在行为上等价，而**任何**别的偏移
        都会让上面那条相等关系失败——中国自 1991 年起无夏令时，+08:00 是常量。
        """
        midwinter = business_midnight(date(2026, 1, 15))
        midsummer = business_midnight(date(2026, 7, 15))
        self.assertEqual(midwinter.utcoffset(), timedelta(hours=8))
        self.assertEqual(
            midsummer.utcoffset(),
            timedelta(hours=8),
            "业务时区的偏移不得随季节变化——判定记录里的 effective_at 是永久留存的事实，"
            "夏令时会让同一句「次日 08:00」在不同季节指两个不同的绝对时刻",
        )

    def test_business_timezone_is_not_the_process_timezone(self):
        """业务时区与进程时区不是同一个，且 Django 不会把业务时区当成进程时区。

        Django 在加载 settings 时会把进程 `TZ` 改成 `TIME_ZONE`（见 time_utils 模块
        docstring），所以「业务时区」这件事只能靠显式构造，不能靠进程环境。这条测试
        第一次失败就意味着有人把两者合并了——那时 `business_midnight()` 的显式时区
        构造还在，但已经没人需要它，下一步就会被删掉。
        """
        self.assertNotEqual(
            business_tz_name(),
            settings.TIME_ZONE,
            "业务时区若与 TIME_ZONE 相同，判定链路的显式时区构造就成了死代码，"
            "下一步必然被删；而 TIME_ZONE 改成非 UTC 时结论就会整体平移",
        )
        self.assertNotEqual(
            str(django_timezone.get_current_timezone()), business_tz_name()
        )

    def test_business_timezone_is_a_real_iana_zone(self):
        """必须是可解析的 IANA 时区：将来真要挂 crontab 时，它要能直接写进
        `CrontabSchedule.timezone`（TimeZoneField 只收 IANA 名）。"""
        self.assertEqual(business_tz().key, business_tz_name())
        self.assertTrue(business_tz().key and "/" in business_tz().key)


class TestSignedCandleIsTheLastClosedOne(TestCase):
    """在日界那一刻运行，签署的必须是刚收盘的那一根（D−1），生效时刻是 D+1。"""

    def test_running_at_the_boundary_signs_yesterday(self):
        boundary = business_midnight(RUN_DAY)
        self.assertEqual(latest_complete_date(boundary), RUN_DAY - timedelta(days=1))

    def test_effective_moment_skips_exactly_one_day(self):
        """运行日 D → 签署日 D−1 → 生效时刻 D+1 北京 08:00。

        「今天出的结论明天才咬人」在这里就是 `effective_at - business_midnight(D) == 1 天`：
        整整一天的可见期，异常能在生效前被拦下。
        """
        attribute_date = RUN_DAY - timedelta(days=1)
        effective_at = business_midnight(attribute_date + timedelta(days=2))
        self.assertEqual(effective_at, business_midnight(RUN_DAY + timedelta(days=1)))
        self.assertEqual(effective_at - business_midnight(RUN_DAY), timedelta(days=1))

    def test_the_mapping_survives_a_late_tick(self):
        """心跳晚几分钟不影响签署日与生效时刻——它们只是运行日的函数。

        这是「名义运行日」而不是「实际执行时刻」的意义：同一天的记录不会因为
        「这是第几个 tick 跑成的」而不同。
        """
        on_time = business_midnight(RUN_DAY)
        late = on_time + timedelta(minutes=7)
        self.assertEqual(latest_complete_date(on_time), latest_complete_date(late))
        self.assertEqual(latest_complete_date(late), RUN_DAY - timedelta(days=1))


def run_heartbeat(
    *,
    candles_error=None,
    judgement_error=None,
    deactivation_error=None,
    shadow_error=None,
    report_error=None,
):
    """跑一次 `snapshot_daily_equity` 的任务体，返回按序记录的事件名。

    六个被调用方都是**函数内 import**（任务模块不在 import 期就把交易/判定链路拉起来），
    所以替身要打在**源模块**上，而不是 ``tasks`` 模块的属性上——``tasks`` 上根本没有
    这些名字。

    `sync_daily_candles` 一定要换成替身：它是这条链里**唯一会发网络请求**的一步，
    真跑等于把交易所 API 拉进一条调度顺序测试。其余各段的理由同理——
    `run_deactivation` 内部会走 `deactivation_run.managed_set()` →
    `ensure_strategies_discovered()`，那是**进程级副作用**，会把宿主机那份策略目录拖进
    注册表，让本模块的结论取决于运行环境。

    `write_shadow_record` 同理换成替身：它要读判定记录、写 Shadow 记录（本模块只关心
    **调度顺序**，让它真跑等于把 DB 拉进来，而它的正确性由 `test_shadow.py` 自己盯）。
    `write_daily_report` 也一样——本单元只关心它**排在哪**，一天一条写成什么样由
    `test_report.py` 盯。**而且它非换不可**：这一段的 `finally` 里有
    `close_old_connections()`，让真的写一次会把本用例所处的事务连接在断言之前收掉。
    """
    from apps.regime import candles, deactivation_run, judgement, report, shadow
    from apps.regime.candles import SyncReport
    from apps.trading import daily_snapshot, tasks

    events: list[str] = []
    snapshot = MagicMock()
    snapshot.as_dict.return_value = {}

    async def fake_snapshot():
        events.append("snapshot")
        return snapshot

    def fake_candles(*args, **kwargs):
        events.append("candles")
        if candles_error is not None:
            raise candles_error
        # 全零报告：``wrote`` 为 0（不触发落库日志），``last_date`` 为 None，
        # 走的是任务体真正会调的 ``.wrote`` 与 ``last_date.isoformat() if … else None`` 两条路径
        return SyncReport(
            symbol="BTC/USDT",
            requested_start=None,
            requested_end=None,
            fetched=0,
            accepted=0,
            incomplete=0,
            out_of_range=0,
            created=0,
            updated=0,
            unchanged=0,
            first_date=None,
            last_date=None,
            dry_run=False,
        )

    def fake_judgement(*args, **kwargs):
        events.append("judgement")
        if judgement_error is not None:
            raise judgement_error
        return {"skipped": "no_candles"}

    def fake_deactivation(*args, **kwargs):
        events.append("deactivation")
        if deactivation_error is not None:
            raise deactivation_error
        # `skipped` 恒存在于真实返回值里（这是它刻意与判定不同的地方），替身照抄这一形状。
        return {
            "skipped": "no_generation",
            "note": "还没有任何一代池化表",
            "targets": 0,
        }

    def fake_shadow(*args, **kwargs):
        events.append("shadow")
        if shadow_error is not None:
            raise shadow_error
        return {"outcome": "created", "note": "替身"}

    def fake_report(*args, **kwargs):
        events.append("report")
        if report_error is not None:
            raise report_error
        return {"written": True, "outcome": "created", "note": "替身"}

    with (
        patch.object(daily_snapshot, "write_daily_snapshots", new=fake_snapshot),
        patch.object(candles, "sync_daily_candles", new=fake_candles),
        patch.object(judgement, "run_daily_judgement", new=fake_judgement),
        patch.object(deactivation_run, "run_deactivation", new=fake_deactivation),
        patch.object(shadow, "write_shadow_record", new=fake_shadow),
        patch.object(report, "write_daily_report", new=fake_report),
    ):
        payload = tasks.snapshot_daily_equity.run()
    return events, payload


class TestJudgementRidesTheSnapshotHeartbeat(TestCase):
    """判定挂在既有的 5 分钟心跳上（对 CONTEXT.md 字面要求的一处有意偏离）。"""

    def _run_task(self):
        return run_heartbeat()[0]

    def test_the_heartbeat_runs_the_daily_judgement(self):
        self.assertIn("judgement", self._run_task())

    def test_the_existing_snapshot_duty_runs_first(self):
        """既有职责先跑，新职责后跑；新职责之间按上下游排。

        判定失败会往上抛（真故障必须让任务标 FAILURE，而不是被吞成一行日志），若它排在快照之前就会连带
        吞掉这一次快照。快照本来就是 5 分钟一轮的幂等写入，晚一轮无所谓，但顺序反过来
        等于让新机制有能力打断一条已在生产上运行的链路——没有理由付这个代价。

        后三段之间的顺序各有硬理由（见各自用例），这里把整条链一次钉死：任何一段被挪
        到上游，都会有下游拿到「上一轮的世界」而**看起来完全正常**。
        """
        self.assertEqual(
            self._run_task(),
            ["snapshot", "candles", "judgement", "deactivation", "shadow", "report"],
        )

    def test_a_failing_judgement_still_stops_the_task(self):
        """判定抛异常时任务必须失败（而不是被吞掉）。

        失败是**期望**行为：心跳 5 分钟后再来一次，判定写入是幂等的 `get_or_create`，
        属 CONTEXT.md「读安全」那一类，可以无脑重试；而吞掉异常等于判定静默死掉。
        """
        from apps.regime import candles, judgement
        from apps.trading import daily_snapshot, tasks

        snapshot = MagicMock()
        snapshot.as_dict.return_value = {}
        with (
            patch.object(
                daily_snapshot,
                "write_daily_snapshots",
                new=AsyncMock(return_value=snapshot),
            ),
            # 续拉也必须换掉：真身会读库、且库里若有数据就会真发网络请求——这条用例
            # 只关心「判定失败往上抛」，别让 candles 一步把它变成环境相关的测试。
            # 返回值不被检视（任务在判定处就抛了），MagicMock 的自动属性足够。
            patch.object(candles, "sync_daily_candles", new=MagicMock()),
            patch.object(
                judgement, "run_daily_judgement", side_effect=RuntimeError("boom")
            ),
        ):
            with self.assertRaises(RuntimeError):
                tasks.snapshot_daily_equity.run()


class TestTheCandleSyncRidesTheSameHeartbeat(TestCase):
    """日线续拉搭在同一条心跳上，且**排在快照之后、判定之前**（第①段判定取数前置）。

    它与其它四段的关键差别是**失败不上抛**：取不到新日线是「上游数据暂不可得」，
    不是代码故障。保持上一有效状态的降级由判定的 ``stale_candles`` / ``no_candles``
    收场承担，日报会明写「今日判定缺失」；若这里往上抛，等于让一个数据源抖动就能掐断
    日报这条「被看见」的渠道。
    """

    def test_the_heartbeat_runs_the_candle_sync(self):
        self.assertIn("candles", run_heartbeat()[0])

    def test_a_failing_candle_fetch_does_not_stop_the_task(self):
        """续拉抛 ``CandleFetchError`` 时任务**不得**失败，且要一直跑到日报。

        与判定/推导/Shadow/日报四段相反：那几段失败上抛是**期望**，这里失败上抛才是
        bug——它会让这一轮日报不发，而日报正是「今日判定缺失」这句话的正经出口。
        """
        from apps.regime.candles import CandleFetchError

        events, payload = run_heartbeat(candles_error=CandleFetchError("boom"))
        # 链条必须完整跑到底：日报这一环被触达了
        self.assertIn("candles", events)
        self.assertEqual(events[-1], "report")
        # 判定照跑（按库内现状降级），日报照发
        self.assertEqual(payload["regime"]["skipped"], "no_candles")
        self.assertTrue(payload["report"]["written"])


class TestDeactivationRidesTheSameHeartbeat(TestCase):
    """停用决策推导搭在同一条心跳上，且**排在判定之后**（第①段单元 7）。"""

    def test_the_heartbeat_runs_the_deactivation(self):
        self.assertIn("deactivation", run_heartbeat()[0])

    def test_the_deactivation_runs_after_the_judgement(self):
        """顺序有硬理由：推导读的是判定刚落下的那条「当前生效阶段」。

        反过来会永远慢一拍，而慢的那一拍**看起来完全正常**——只是每天晚一天停用。
        """
        events, _ = run_heartbeat()
        self.assertLess(events.index("judgement"), events.index("deactivation"))

    def test_the_summary_lands_in_the_payload(self):
        """`skipped` 与 `note` 必须从任务返回值里出得来。

        「日志不算被看见」——三种「什么都不动」的收场（冷启动 / 状态过期 / 没有池化表）
        全靠这句话往日报上传；任务层把它丢了，日报就只能显示「今天没有建议」。
        """
        _, payload = run_heartbeat()
        self.assertEqual(payload["deactivation"]["skipped"], "no_generation")
        self.assertTrue(payload["deactivation"]["note"])

    def test_a_failing_deactivation_still_stops_the_task(self):
        """推导抛异常时任务必须失败（而不是被吞掉）。

        失败是**期望**行为：心跳 5 分钟后再来一次，写入是幂等的 `update_or_create`，
        可以无脑重试；吞掉异常则会让停用决策静默死掉。判定失败与推导失败是**两件事**，
        所以两条用例各自的替身要能单独抛。
        """
        with self.assertRaises(RuntimeError):
            run_heartbeat(deactivation_error=RuntimeError("boom"))


class TestShadowRidesTheSameHeartbeat(TestCase):
    """Shadow 每日记录搭在同一条心跳上，且**排在推导之后**（第①段单元 8i）。"""

    def test_the_heartbeat_writes_the_shadow_record(self):
        self.assertIn("shadow", run_heartbeat()[0])

    def test_the_shadow_record_runs_after_the_deactivation(self):
        """顺序有硬理由：要落的建议清单正是推导的产物。

        **它是推导的下游，不是并列的一段**——挂在推导之前只会永远写空清单。而且这条
        顺序看不出问题：空清单和「今天确实没有建议」在表里长得一模一样。
        """
        events, _ = run_heartbeat()
        self.assertLess(events.index("deactivation"), events.index("shadow"))

    def test_the_summary_lands_in_the_payload(self):
        """摘要必须从任务返回值里出得来。

        「日志不算被看见」——判定层没出结论、推导层三种「什么都不动」的收场，全靠这句
        话往日报上传；任务层把它丢了，日报就只能显示「今天没有记录」。
        """
        _, payload = run_heartbeat()
        self.assertEqual(payload["shadow"]["outcome"], "created")
        self.assertTrue(payload["shadow"]["note"])

    def test_a_judgement_without_a_conclusion_still_writes_a_row(self):
        """判定没有结论时**不抛**：那是「今天没有结论」，不是本层的失败。

        判定层已经把收场说清楚了（`stale_candles` / `no_candles` / `undecidable`），
        这一层照落一行——「判定跑了但机制没表态」正是这张表要能数出来的东西。
        """
        events, payload = run_heartbeat()
        self.assertIn("shadow", events)
        self.assertEqual(payload["regime"]["skipped"], "no_candles")

    def test_a_failing_shadow_writer_still_stops_the_task(self):
        """写入失败（DB 故障）时任务必须失败（而不是被吞掉）。

        失败是**期望**行为：心跳 5 分钟后再来一次，占位行补写是幂等的，可以无脑重试；
        吞掉异常则会让这张表静默停在某一天，而「这张表停在某一天」正是它要负责发现的
        事情。
        """
        with self.assertRaises(RuntimeError):
            run_heartbeat(shadow_error=RuntimeError("boom"))


class TestTheReportRidesTheSameHeartbeat(TestCase):
    """日报搭在同一条心跳上，且**排在 Shadow 之后**（第①段单元 8iii）。"""

    def test_the_heartbeat_writes_the_daily_report(self):
        self.assertIn("report", run_heartbeat()[0])

    def test_the_report_runs_after_the_shadow_record(self):
        """顺序有硬理由：第②段与 Shadow 的建议清单**同源**（都是这一轮推导的产物），
        而且还要读**昨天那份日报的结构化快照**做差——两天的差要到「今天也说完了」
        才成立。

        反过来排就会拿「上一轮的世界」去做差，而那种错位看起来完全正常：日报每天照出，
        只是第②段永远比实际晚一拍。
        """
        events, _ = run_heartbeat()
        self.assertLess(events.index("shadow"), events.index("report"))

    def test_the_summary_lands_in_the_payload(self):
        """摘要必须从任务返回值里出得来。

        「日志不算被看见」——日报是第①段唯一**直接说给用户听**的东西，任务层把摘要
        丢了，投递那一环就没有东西可投。
        """
        _, payload = run_heartbeat()
        self.assertTrue(payload["report"]["written"])
        self.assertTrue(payload["report"]["note"])

    def test_a_conclusion_less_judgement_still_reaches_the_report(self):
        """判定没有结论时**照发**：那是「今天没有结论」，不是本层的失败。

        日报一天一条、必发——「沉默必须能被识别为异常」。判定缺失正是它要写出来的事情
        之一（第①段明写「今日判定缺失，处于保持的上一有效状态」）。至于「有结论就写、
        没结论就等到截止时刻再写」这条闸门，由 ``test_report.py`` 盯。
        """
        events, payload = run_heartbeat()
        self.assertIn("report", events)
        self.assertEqual(payload["regime"]["skipped"], "no_candles")

    def test_a_failing_report_writer_still_stops_the_task(self):
        """写入失败（DB 故障）时任务必须失败（而不是被吞掉）。

        失败是**期望**行为：心跳 5 分钟后再来一次，`get_or_create` 幂等，可以无脑重试；
        吞掉异常则会让这张表静默停在某一天，而「这张表停在某一天」正是它要负责发现的
        事情。
        """
        with self.assertRaises(RuntimeError):
            run_heartbeat(report_error=RuntimeError("boom"))


class TestTheBeatIsDenseEnoughForTheDayBoundary(TestCase):
    """心跳节拍决定「日界之后多久出结论」。"""

    def test_snapshot_interval_bounds_the_delay(self):
        from celery_app import app

        entry = app.conf.beat_schedule["snapshot-daily-equity"]
        interval = entry["schedule"]
        self.assertLessEqual(
            interval,
            timedelta(hours=1).total_seconds(),
            "判定搭在这条心跳上，所以心跳间隔就是「日界之后最晚多久出结论」。"
            "放宽到小时级会让结论落在当天的后半段，与「日频、每天出结论」的语义不符",
        )
