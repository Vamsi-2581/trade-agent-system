"""Tests for TaskTracker: milestone throttling, lifecycle, notification, and retry fields."""

from __future__ import annotations

import time
import uuid
from unittest.mock import MagicMock, patch

import pytest

from apps.agent.task_tracker import TaskTracker, tracker_context

REDIS_PATCH = "redis.from_url"
NOTIFY_PATCH = "apps.agent.task_tracker._send_notification_redis"


def _make_tracker(**kwargs):
    defaults = {
        "task_id": str(uuid.uuid4()),
        "user_id": "test-user",
    }
    defaults.update(kwargs)
    return TaskTracker(**defaults)


@pytest.fixture(autouse=True)
def _clear_context():
    """Reset context before and after each test."""
    tracker_context.set(None)
    yield
    tracker_context.set(None)


@pytest.fixture()
def _mock_deps():
    """Mock Redis and notification."""
    mock_r = MagicMock()
    mock_r.pipeline.return_value = MagicMock()
    with patch(REDIS_PATCH, return_value=mock_r), patch(NOTIFY_PATCH):
        yield mock_r


class TestTaskTrackerLifecycle:
    @pytest.mark.usefixtures("_mock_deps")
    def test_start_writes_redis_and_notifies(self):
        tracker = _make_tracker()
        tracker.start()
        assert len(tracker._milestones) == 0

    @pytest.mark.usefixtures("_mock_deps")
    def test_complete_records_milestone(self):
        tracker = _make_tracker()
        tracker.start()
        tracker.milestone("下载数据完成", progress=0.5)
        tracker.complete("回测完成")

        assert len(tracker._milestones) == 1
        assert tracker._milestones[0]["message"] == "下载数据完成"

    @pytest.mark.usefixtures("_mock_deps")
    def test_fail_records_error(self):
        tracker = _make_tracker()
        tracker.start()
        tracker.fail("Connection timeout")
        assert len(tracker._milestones) == 0

    @pytest.mark.usefixtures("_mock_deps")
    def test_start_writes_retry_fields(self):
        """start() should write retry_count=0 and max_retries to Redis."""
        tracker = _make_tracker(max_retries=3)
        tracker.start()
        assert tracker.max_retries == 3

    @pytest.mark.usefixtures("_mock_deps")
    def test_start_stores_original_task(self):
        """start() should store original_task in Redis."""
        original = {"celery_task": "some.task", "args": [], "kwargs": {"user_id": "u1"}}
        tracker = _make_tracker(original_task=original)
        tracker.start()
        assert tracker.original_task == original


class TestTaskTrackerThrottling:
    @pytest.mark.usefixtures("_mock_deps")
    def test_milestone_throttled_within_interval(self):
        tracker = _make_tracker(heartbeat_interval=60)
        tracker.start()

        for i in range(5):
            tracker.milestone(f"step {i}", progress=i * 0.1)

        assert len(tracker._milestones) == 5

    @pytest.mark.usefixtures("_mock_deps")
    def test_milestone_pushed_after_interval(self):
        tracker = _make_tracker(heartbeat_interval=1)
        tracker.start()

        time.sleep(1.1)
        tracker.milestone("after wait", progress=0.5)


class TestTaskTrackerContext:
    @pytest.mark.usefixtures("_mock_deps")
    def test_context_var_get_current(self):
        tracker = _make_tracker()
        token = tracker_context.set(tracker)
        try:
            assert TaskTracker.get_current() is tracker
        finally:
            tracker_context.reset(token)

    def test_get_current_returns_none_without_tracker(self):
        assert TaskTracker.get_current() is None


class TestTaskTrackerFormat:
    def test_format_progress_message(self):
        from apps.agent.task_tracker import _format_progress_message

        msg = _format_progress_message(
            "abc12345-xxxx", "下载数据", progress=0.3, elapsed="2分钟"
        )
        assert "abc12345" in msg
        assert "下载数据" in msg
        assert "30%" in msg
        assert "2分钟" in msg

    def test_format_duration(self):
        from apps.agent.task_tracker import _format_duration

        now = time.time()
        assert _format_duration(now) == "0秒"


class TestTaskTrackerToolCall:
    """Tests for tool_call notification feature."""

    @pytest.mark.usefixtures("_mock_deps")
    def test_tool_call_basic(self):
        """Test basic tool_call notification with arguments."""
        tracker = _make_tracker(channel="telegram")
        tracker.start("任务启动")

        # Mock _notify to capture notification text
        with patch.object(tracker, "_notify") as mock_notify:
            tracker.tool_call("get_kline_data", {"symbol": "BTCUSDT", "interval": "1h"})

            # Verify _notify was called
            mock_notify.assert_called_once()

            # Verify notification format
            notification_text = mock_notify.call_args[0][0]
            assert "🔧 执行工具：get_kline_data" in notification_text
            assert "symbol='BTCUSDT'" in notification_text
            assert "interval='1h'" in notification_text

    @pytest.mark.usefixtures("_mock_deps")
    def test_tool_call_filters_internal_args(self):
        """Test user_id and agent_name are filtered out."""
        tracker = _make_tracker()
        tracker.start()

        with patch.object(tracker, "_notify") as mock_notify:
            tracker.tool_call(
                "resolve_user",
                {"user_id": "secret-123", "agent_name": "supervisor", "query": "John"},
            )

            notification_text = mock_notify.call_args[0][0]

            # Verify sensitive args NOT in notification
            assert "user_id" not in notification_text
            assert "agent_name" not in notification_text
            assert "secret-123" not in notification_text
            assert "supervisor" not in notification_text

            # Verify non-sensitive args ARE in notification
            assert "query='John'" in notification_text

    @pytest.mark.usefixtures("_mock_deps")
    def test_tool_call_truncates_long_values(self):
        """Test long parameter values are truncated to 50 chars."""
        tracker = _make_tracker()
        tracker.start()

        long_query = "a" * 100  # 100 characters

        with patch.object(tracker, "_notify") as mock_notify:
            tracker.tool_call("search", {"query": long_query})

            notification_text = mock_notify.call_args[0][0]

            # Verify value truncated to repr(v)[:50] -> 'aaaaaaaa...' (50 chars total, includes opening quote)
            # repr("a"*100) = "'aaaa...'" (102 chars: 2 quotes + 100 a's)
            # repr(v)[:50] = first 50 chars: ' + 49 a's (no closing quote)
            assert len(notification_text) < 150  # Ensure not too long
            assert "query=" in notification_text
            # Check truncated value has 49 a's after opening quote
            assert (
                "'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
                in notification_text[:100]
            )

    @pytest.mark.usefixtures("_mock_deps")
    def test_tool_call_without_tracker_context(self):
        """Test tool_call when tracker not in context (should not raise)."""
        # Clear context
        tracker_context.set(None)

        tracker = _make_tracker()
        tracker.start()

        # Should not raise even though context is None
        with patch.object(tracker, "_notify") as mock_notify:
            tracker.tool_call("get_kline_data", {"symbol": "BTC"})

            # Notification should still be sent (tracker exists)
            mock_notify.assert_called_once()


class TestRecordSubmitted:
    """Tests for submission reliability: record_submitted + get_submission_status."""

    def _make_mock_redis(self, data: dict | None = None):
        mock_r = MagicMock()
        mock_r.pipeline.return_value = MagicMock()
        if data is not None:
            mock_r.exists.return_value = True
            mock_r.hget.return_value = data.get("status", "submitted")
        else:
            mock_r.exists.return_value = False
        return mock_r

    def test_record_submitted_writes_redis(self):
        task_id = "test-submitted-task-001"
        mock_r = self._make_mock_redis()

        with patch(REDIS_PATCH, return_value=mock_r):
            TaskTracker.record_submitted(
                task_id=task_id,
                user_id="user1",
                task_type="backtest",
                metadata={"symbol": "BTC/USDT"},
            )

        # Verify hset was called with submitted status
        mock_r.pipeline.return_value.hset.assert_called()
        call_args = mock_r.pipeline.return_value.hset.call_args
        assert call_args[0][0] == f"task:progress:{task_id}"

    def test_get_submission_status_returns_none_for_unknown(self):
        mock_r = self._make_mock_redis(data=None)

        with patch(REDIS_PATCH, return_value=mock_r):
            status = TaskTracker.get_submission_status("nonexistent-task")
            assert status is None

    def test_get_submission_status_returns_status(self):
        mock_r = self._make_mock_redis(data={"status": "submitted"})

        with patch(REDIS_PATCH, return_value=mock_r):
            status = TaskTracker.get_submission_status("existing-task")
            assert status == "submitted"

    def test_redis_failure_is_graceful_record(self):
        """record_submitted must not raise on Redis failure."""
        mock_r = MagicMock()
        mock_r.pipeline.side_effect = Exception("Connection refused")

        with patch(REDIS_PATCH, return_value=mock_r):
            # Should not raise
            TaskTracker.record_submitted(
                task_id="task-fail",
                user_id="user1",
            )

    def test_redis_failure_is_graceful_query(self):
        """get_submission_status must not raise on Redis failure."""
        mock_r = MagicMock()
        mock_r.exists.side_effect = Exception("Connection refused")

        with patch(REDIS_PATCH, return_value=mock_r):
            status = TaskTracker.get_submission_status("task-fail")
            assert status is None
