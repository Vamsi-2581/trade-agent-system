import json
import logging
import uuid
from unittest.mock import patch, MagicMock

from django.test import TestCase, Client
from django.urls import reverse
from rest_framework.settings import api_settings

from apps.logging_app.models import SystemLog
from apps.logging_app.handler import SystemLogHandler
from apps.logging_app.mapper import resolve_module


class ModuleMapperTest(TestCase):
    def test_known_modules(self):
        self.assertEqual(resolve_module("apps.agent.supervisor"), "agent")
        self.assertEqual(resolve_module("apps.trading.executor"), "trading")
        self.assertEqual(resolve_module("apps.riskguard.guard"), "riskguard")
        self.assertEqual(resolve_module("apps.signal_monitor.engine"), "signal_monitor")
        self.assertEqual(resolve_module("apps.memory.manager"), "memory")
        self.assertEqual(resolve_module("apps.channel.telegram"), "channel")
        self.assertEqual(resolve_module("apps.notify.consumers"), "notify")
        self.assertEqual(resolve_module("apps.exchange.binance"), "exchange")
        self.assertEqual(resolve_module("apps.datasource.manager"), "datasource")
        self.assertEqual(resolve_module("apps.authentication.views"), "auth")
        self.assertEqual(resolve_module("apps.backtest.runner"), "backtest")
        self.assertEqual(resolve_module("apps.skill.loader"), "skill")
        self.assertEqual(resolve_module("apps.core.apps"), "core")
        self.assertEqual(resolve_module("core.asgi"), "core")

    def test_unknown_module(self):
        self.assertEqual(resolve_module("some.unknown.package"), "unknown")
        self.assertEqual(resolve_module(""), "unknown")


class SystemLogModelTest(TestCase):
    def test_create_log(self):
        log = SystemLog.objects.create(
            level="INFO",
            module="agent",
            logger_name="apps.agent.base",
            message="Agent started",
            trace_id="abc-123",
            extra_data={"key": "value"},
        )
        self.assertIsNotNone(log.id)
        self.assertEqual(str(log)[:6], "[INFO]")

    def test_ordering(self):
        SystemLog.objects.create(level="INFO", module="agent", message="First")
        import time as _time

        _time.sleep(0.01)
        SystemLog.objects.create(level="ERROR", module="trading", message="Second")
        logs = list(SystemLog.objects.all())
        self.assertEqual(logs[0].message, "Second")
        self.assertEqual(logs[1].message, "First")


class SystemLogHandlerTest(TestCase):
    @patch("apps.logging_app.handler._get_redis")
    def test_handler_emits_log(self, mock_redis):
        mock_redis.return_value = MagicMock()
        SystemLogHandler()
        record = logging.LogRecord(
            name="apps.agent.base",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="Test log message",
            args=(),
            exc_info=None,
        )
        # Use DBOnlyLogHandler to avoid async thread race in tests
        from apps.logging_app.handler import DBOnlyLogHandler

        db_handler = DBOnlyLogHandler()
        db_handler.emit(record)
        self.assertEqual(
            SystemLog.objects.filter(message__contains="Test log message").count(), 1
        )

    @patch("apps.logging_app.handler._get_redis")
    def test_handler_resolves_module(self, mock_redis):
        mock_redis.return_value = MagicMock()
        from apps.logging_app.handler import DBOnlyLogHandler

        db_handler = DBOnlyLogHandler()
        record = logging.LogRecord(
            name="apps.trading.executor",
            level=logging.ERROR,
            pathname="test.py",
            lineno=1,
            msg="Order failed",
            args=(),
            exc_info=None,
        )
        db_handler.emit(record)
        log = SystemLog.objects.get(message__contains="Order failed")
        self.assertEqual(log.module, "trading")
        self.assertEqual(log.level, "ERROR")


class _UnthrottledAPITestCase(TestCase):
    """TestCase with DRF rate throttling disabled.

    `/api/logs/` is AllowAny, so DRF's `AnonRateThrottle` ("anon": "20/min")
    applies to the test client like to any other anonymous caller. Its cache key
    is `throttle_anon_<REMOTE_ADDR>` and the counter lives in the *shared*
    process cache (dev/test settings use RedisCache), so the counter survives
    across TestCase instances and across test classes — after 20 requests in a
    minute every request below answers HTTP 429 instead of exercising the view.

    Disabling the default throttle classes for this module's API tests keeps the
    endpoint under test reachable regardless of how often the module (or the
    whole suite) runs; it does not touch what the tests assert.
    """

    def setUp(self):
        super().setUp()
        # Client sends REMOTE_ADDR="127.0.0.1"; every test below would otherwise
        # share one `throttle_anon_127.0.0.1` bucket.
        self._throttle_settings = patch.object(
            api_settings,
            "DEFAULT_THROTTLE_CLASSES",
            [],
            create=True,
        )
        self._throttle_settings.start()
        self.addCleanup(self._throttle_settings.stop)


class LogAPITest(_UnthrottledAPITestCase):
    def setUp(self):
        super().setUp()
        self.client = Client()
        # Scope assertions to this test's own rows: SystemLog may already hold
        # rows written by other tests in the same run.
        self.tag = f"logapi-{uuid.uuid4().hex}"
        SystemLog.objects.create(
            level="INFO", module="agent", message=f"Agent started {self.tag}"
        )
        SystemLog.objects.create(
            level="ERROR", module="trading", message=f"Order failed {self.tag}"
        )
        SystemLog.objects.create(
            level="WARNING",
            module="riskguard",
            message=f"High leverage detected {self.tag}",
        )
        SystemLog.objects.create(
            level="INFO",
            module="agent",
            message=f"Skill registered {self.tag}",
            trace_id="trace-001",
        )

    def _rows(self, query=""):
        """GET /api/logs/ and return the paginated `results` payload."""
        resp = self.client.get(reverse("log-list") + query)
        self.assertEqual(resp.status_code, 200)
        return resp.json()["results"]

    def _own_rows(self, query=""):
        """只留本用例自己写的行（消息里带本用例的 `self.tag`）。

        `/api/logs/` 读的是**全表**，不带任何属于「本次测试」的过滤——这正是接口该
        有的行为。所以断言绝对条数时必须自己收窄到本用例造的那几行，否则库里既存的
        日志（dev 库上真实存在）会把 `count` 抬到几十，而失败看起来像接口坏了。
        """
        return [r for r in self._rows(query) if self.tag in r["message"]]

    def test_list_all_logs(self):
        resp = self.client.get(reverse("log-list"))
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        # 分页封套的形状（键集）与「本用例这 4 行都在」都要钉住。
        self.assertEqual(
            set(body), {"count", "page", "page_size", "has_next", "results"}
        )
        self.assertEqual(len(self._own_rows()), 4)

    def test_filter_by_level(self):
        rows = self._own_rows("?level=ERROR")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["level"], "ERROR")

    def test_filter_by_module(self):
        rows = self._own_rows("?module=agent")
        self.assertEqual(len(rows), 2)

    def test_filter_by_search(self):
        rows = self._own_rows("?search=failed")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["level"], "ERROR")

    def test_filter_by_trace_id(self):
        rows = self._own_rows("?trace_id=trace-001")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["trace_id"], "trace-001")

    def test_test_log_endpoint(self):
        # Patch the logger to use DBOnlyLogHandler (synchronous)
        import logging as py_logging

        test_logger = py_logging.getLogger("apps.test")
        original_handlers = test_logger.handlers[:]
        from apps.logging_app.handler import DBOnlyLogHandler

        handler = DBOnlyLogHandler()
        test_logger.handlers = [handler]
        try:
            resp = self.client.post(
                reverse("log-test"),
                data=json.dumps(
                    {"level": "WARNING", "message": "Test warning", "module": "test"}
                ),
                content_type="application/json",
            )
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json()["status"], "ok")
            self.assertTrue(
                SystemLog.objects.filter(message__contains="Test warning").exists()
            )
        finally:
            test_logger.handlers = original_handlers
