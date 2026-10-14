"""回归测试：调度时间统一按北京时间解释（业务时区口径）。

Bug 背景（2026-09-20 实测）：
  全局 `TIME_ZONE` / `CELERY_TIMEZONE` 都是 UTC，而且 Django 加载 settings 时会用
  `TIME_ZONE` 覆盖进程的 `TZ` 环境变量（`django/conf/__init__.py` 里调了
  `time.tzset()`），所以 **Django 进程内 `datetime.now()` 拿到的是 UTC**，
  即使容器镜像是 `TZ=Asia/Shanghai`。于是用户说「每天10点」时：

  1. `SubmitRecurringTaskTool` 不传 `timezone`，`CrontabSchedule` 存成 UTC，
     `0 10 * * *` 实际在北京时间 18:00 触发；
  2. `SubmitScheduledTaskTool._parse_run_at` 用 UTC 的 `now()` 去
     `replace(hour=9)`，「tomorrow 09:00」实际指北京时间 17:00；
  3. prompt 注入的 `{{CURRENT_DATETIME}}` 是裸 UTC，提交确认也只回一句
     `cron=0 10 * * *`，没有任何时区标注 —— 用户无从发现自己理解错了。

修复：`apps/common/time_utils.py` 统一「用户口语时间 → 业务时区（默认
Asia/Shanghai）」；调度落地时显式带 `timezone`；把时区标注给用户和 LLM。

注意：**没有**改全局 `TIME_ZONE` / `CELERY_TIMEZONE`（会平移已有 DB 时间戳、
并让 celery-beat 用新时区重新解析 `last_run_at`，可能触发补跑风暴），
所以系统自带的几个 `tz=UTC` 调度行保持原样。
"""

import asyncio
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone as djtz

from apps.agent.tools.schedule_task import (
    ListScheduledTasksTool,
    SubmitRecurringTaskTool,
    SubmitScheduledTaskTool,
)
from apps.common.time_utils import (
    business_now,
    business_tz_label,
    business_tz_name,
    format_business,
    to_business,
)

BEIJING = "Asia/Shanghai"
CHANNEL_USER_ID = "123456789"  # 通道侧 id（telegram_id），不是 Django UUID


class TestBusinessTimezoneHelper:
    """apps/common/time_utils.py 的基础语义。"""

    def test_default_business_timezone_is_beijing(self):
        assert business_tz_name() == BEIJING
        assert "UTC+08:00" in business_tz_label()

    def test_business_now_is_aware_and_beijing(self):
        now = business_now()
        assert now.tzinfo is not None
        assert now.utcoffset() == timedelta(hours=8)

    def test_naive_datetime_is_read_as_beijing(self):
        """naive 时间按业务时区解释：北京 09:00 == UTC 01:00。"""
        got = to_business(datetime(2026, 9, 21, 9, 0))
        assert got.utcoffset() == timedelta(hours=8)
        assert got.astimezone(dt_timezone.utc).hour == 1

    def test_aware_datetime_keeps_same_instant(self):
        got = to_business(datetime(2026, 9, 21, 1, 0, tzinfo=dt_timezone.utc))
        assert got.astimezone(dt_timezone.utc).hour == 1
        assert got.hour == 9  # 展示成北京时间

    def test_format_business_labels_timezone(self):
        text = format_business(datetime(2026, 9, 21, 9, 0, tzinfo=dt_timezone.utc))
        assert "2026-09-21 17:00" in text  # UTC 9 点 = 北京 17 点
        assert BEIJING in text
        assert "UTC+08:00" in text

    def test_format_business_handles_none(self):
        assert format_business(None) == "未执行"


class TestParseRunAt:
    """口语时间必须按北京时间解释，不能拿 UTC 的 now() 去 replace(hour=...)。"""

    def _parse(self, raw: str) -> datetime:
        return SubmitScheduledTaskTool()._parse_run_at(raw)

    def test_tomorrow_hhmm_is_beijing_not_utc(self):
        got = self._parse("tomorrow 09:00")
        assert (got.hour, got.minute) == (9, 0)
        assert got.utcoffset() == timedelta(hours=8)
        # 修复前这里按 UTC 解释，UTC 小时数会是 9；现在必须是 1
        assert got.astimezone(dt_timezone.utc).hour == 1

    def test_tomorrow_lands_on_tomorrow_in_beijing(self):
        got = self._parse("tomorrow 00:30")
        assert got.date() == (business_now() + timedelta(days=1)).date()

    def test_naive_iso_is_read_as_beijing(self):
        got = self._parse("2026-09-21T09:00:00")
        assert got.astimezone(dt_timezone.utc).hour == 1
        assert got.hour == 9

    def test_explicit_offset_iso_keeps_same_instant(self):
        got = self._parse("2026-09-21T01:00:00+00:00")
        assert got.astimezone(dt_timezone.utc).hour == 1
        assert got.hour == 9  # 展示口径统一到北京时间

    def test_now_plus_offset_is_aware(self):
        got = self._parse("now+2h")
        assert got.tzinfo is not None
        assert (
            timedelta(hours=1, minutes=55)
            < got - business_now()
            < timedelta(hours=2, minutes=5)
        )

    def test_unparsable_raises(self):
        with pytest.raises(ValueError):
            self._parse("下周三下午")


@pytest.mark.django_db(transaction=True)
class TestRecurringTaskTimezone:
    def _submit(
        self, task_name: str, cron: str = "0 10 * * *", steps: list | None = None
    ):
        tool = SubmitRecurringTaskTool()
        return asyncio.run(
            tool.execute(
                agent_name="supervisor" if steps else "analyst",
                message="每天10点全面分析加密货币市场行情并发送结果",
                cron_expression=cron,
                task_name=task_name,
                user_id=CHANNEL_USER_ID,
                steps=steps,
                summary="每天10点全面分析行情" if steps else "",
            )
        )

    @staticmethod
    def _cleanup(task_name: str) -> None:
        """删掉测试建的周期任务；顺手删掉它留下的孤儿调度行。"""
        from django_celery_beat.models import CrontabSchedule, PeriodicTask

        for task in PeriodicTask.objects.filter(name=task_name).select_related(
            "crontab"
        ):
            if task.crontab_id:
                CrontabSchedule.objects.filter(id=task.crontab_id).delete()
        PeriodicTask.objects.filter(name=task_name).delete()

    def test_crontab_is_created_with_beijing_timezone(self):
        task_name = "test_tz_recurring_beijing"
        try:
            result = self._submit(task_name)
            assert result.success, result.error

            from django_celery_beat.models import PeriodicTask

            task = PeriodicTask.objects.get(name=task_name)
            assert str(task.crontab.timezone) == BEIJING
            assert (task.crontab.hour, task.crontab.minute) == ("10", "0")
        finally:
            self._cleanup(task_name)

    def test_next_run_is_beijing_10am_not_18pm(self):
        """'0 10 * * *' + 北京时区 → 下次触发是北京 10:00（不是 UTC 10:00 = 北京 18:00）。"""
        task_name = "test_tz_recurring_next_run"
        try:
            result = self._submit(task_name)
            assert result.success, result.error

            from django_celery_beat.models import PeriodicTask

            task = PeriodicTask.objects.get(name=task_name)
            desc = SubmitRecurringTaskTool._describe_next_run(task.crontab)
            assert "10:00" in desc, desc
            assert BEIJING in desc, desc
        finally:
            self._cleanup(task_name)

    def test_existing_utc_crontab_is_neither_reused_nor_rewritten(self):
        """系统自带 crontab 是 tz=UTC；同字段的用户任务必须另建一行，两边都别动。"""
        task_name = "test_tz_recurring_no_reuse"
        from django_celery_beat.models import CrontabSchedule, PeriodicTask

        utc_row, row_created = CrontabSchedule.objects.get_or_create(
            minute="0",
            hour="10",
            day_of_month="*",
            month_of_year="*",
            day_of_week="*",
            timezone="UTC",
        )
        try:
            result = self._submit(task_name)
            assert result.success, result.error

            task = PeriodicTask.objects.get(name=task_name)
            utc_row.refresh_from_db()
            assert str(utc_row.timezone) == "UTC"  # 已有行没被改写
            assert task.crontab_id != utc_row.id  # 也没被复用
            assert str(task.crontab.timezone) == BEIJING
        finally:
            self._cleanup(task_name)
            # 只删本测试自己建的行；命中已有行时不动它
            if row_created:
                CrontabSchedule.objects.filter(id=utc_row.id).delete()

    def test_submit_message_declares_timezone(self):
        """用户看到的不再是裸 cron 字符串。"""
        task_name = "test_tz_recurring_message"
        try:
            result = self._submit(task_name)
            assert result.success, result.error

            assert result.data["timezone"] == BEIJING
            assert BEIJING in result.data["message"]
            assert "UTC+08:00" in result.data["message"]
            # 能给出下次触发时间，用户可以直接核对是不是自己要的「10点」
            assert result.data["next_run_local"]
        finally:
            self._cleanup(task_name)


@pytest.mark.django_db(transaction=True)
class TestScheduledOneTimeTimezone:
    def test_submit_message_shows_beijing_time(self):
        from apps.agent.models import ScheduledOneTimeTask

        with patch(
            "apps.agent.tasks.execute_scheduled_agent_task.apply_async"
        ) as mock_apply:
            mock_apply.return_value = MagicMock(id="fake-celery-id")
            result = asyncio.run(
                SubmitScheduledTaskTool().execute(
                    agent_name="analyst",
                    message="盘前分析",
                    run_at="tomorrow 09:00",
                    user_id=CHANNEL_USER_ID,
                )
            )

        assert result.success, result.error
        try:
            assert result.data["timezone"] == BEIJING
            assert "09:00" in result.data["message"]
            assert BEIJING in result.data["message"]
            # 落库 / 传给 celery 的仍是有时区信息的 aware datetime
            assert result.data["run_at_local"]
            record = ScheduledOneTimeTask.objects.get(id=result.data["schedule_id"])
            assert record.run_at.tzinfo is not None
            assert to_business(record.run_at).hour == 9
        finally:
            ScheduledOneTimeTask.objects.filter(id=result.data["schedule_id"]).delete()


@pytest.mark.django_db(transaction=True)
class TestToolsSurfaceTimezone:
    def test_list_marks_timezone_on_crontab_and_one_time_task(self):
        from apps.agent.models import ScheduledOneTimeTask
        from django_celery_beat.models import CrontabSchedule, PeriodicTask

        schedule = CrontabSchedule.objects.create(
            minute="0",
            hour="10",
            day_of_month="*",
            month_of_year="*",
            day_of_week="*",
            timezone=BEIJING,
        )
        PeriodicTask.objects.create(
            name="test_tz_list_recurring",
            task="apps.agent.tasks.execute_recurring_agent_task",
            crontab=schedule,
            kwargs='{"agent_name": "analyst"}',
        )
        one_time = ScheduledOneTimeTask.objects.create(
            task_name="test_tz_list_one_time",
            agent_name="analyst",
            message="盘前分析",
            user_id=CHANNEL_USER_ID,
            run_at=datetime(2026, 9, 21, 1, 0, tzinfo=dt_timezone.utc),  # 北京 09:00
            status="pending",
        )
        try:
            from django_celery_beat.models import PeriodicTask as _PT

            result = asyncio.run(ListScheduledTasksTool().execute())
            assert result.success, result.error
            text = result.data

            assert "test_tz_list_recurring" in text
            assert f"时区 {BEIJING}" in text
            assert "test_tz_list_one_time" in text
            # 一次性任务展示北京时间 + 时区标注（UTC 1 点 → 北京 9 点）
            assert "2026-09-21 09:00" in text
            assert BEIJING in text
        finally:
            _PT.objects.filter(name="test_tz_list_recurring").delete()
            CrontabSchedule.objects.filter(id=schedule.id).delete()
            ScheduledOneTimeTask.objects.filter(id=one_time.id).delete()

    def test_prompt_datetime_is_timezone_labeled(self):
        """注入 prompt 的当前时间必须带时区，否则 LLM 会把 UTC 钟点当用户钟点。"""
        from apps.agent.prompt_loader import PROMPT_BASE, PromptLoader

        if not (PROMPT_BASE / "v1" / "supervisor.txt").exists():
            pytest.skip("prompt 文件不在本机")

        body = PromptLoader.load("supervisor")
        assert "{{CURRENT_DATETIME}}" not in body
        assert BEIJING in body
        assert "UTC+08:00" in body


class TestBusinessTimezoneIsIndependentOfDjangoTimezone:
    """业务时区不能跟着 `TIME_ZONE` 走。

    成因回顾：Django 加载 settings 时会用 `TIME_ZONE` 覆盖进程 `TZ`（`time.tzset()`），
    所以「进程本地时间」在 Django 里根本不是用户的钟点。业务时区必须由
    `settings.BUSINESS_TIMEZONE` 单独决定，与 `TIME_ZONE` 解耦。
    """

    def test_business_timezone_ignores_django_time_zone(self):
        from django.conf import settings

        if getattr(settings, "TIME_ZONE", None) == BEIJING:
            pytest.skip("TIME_ZONE 已改成北京，无法验证解耦")

        assert business_now().utcoffset() == timedelta(hours=8)
        assert djtz.localtime().utcoffset() != timedelta(hours=8)
