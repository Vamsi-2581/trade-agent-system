from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch


def _make_mock_redis(keys_data: dict):
    """Build a MagicMock that acts like a sync redis client for scan_iter + hgetall."""
    mock_redis = MagicMock()
    # scan_iter returns an iterator over keys
    mock_redis.scan_iter.return_value = list(keys_data.keys())
    # hgetall returns data for the given key
    mock_redis.hgetall.side_effect = lambda k: keys_data.get(k, {})
    mock_redis.hset = MagicMock()
    return mock_redis


class TestCheckTaskHealthZombie:
    """Tests for check_task_health zombie detection with last_alive merge."""

    def test_detects_zombie_by_updated_at(self):
        from apps.agent.tasks import check_task_health

        stale = (datetime.now(timezone.utc) - timedelta(seconds=400)).isoformat()
        mock_data = {
            "status": "running",
            "task_id": "task-z1",
            "user_id": "user1",
            "updated_at": stale,
            "last_alive": stale,
            "retry_count": "0",
            "max_retries": "1",
            "original_task": '{"task_id":"task-z1","user_id":"user1","payload":{"text":"hello"}}',
        }
        mock_redis = _make_mock_redis({"task:progress:task-z1": mock_data})

        with (
            patch("redis.from_url", return_value=mock_redis),
            patch("apps.agent.task_tracker._send_notification_redis") as mock_notify,
            patch("apps.agent.tasks.app.send_task") as mock_send,
        ):
            mock_send.return_value = MagicMock(id="new-task-id")
            result = check_task_health.apply().get()

        assert result["zombies"] == 1
        mock_notify.assert_called()

    def test_ignores_fresh_task(self):
        from apps.agent.tasks import check_task_health

        recent = datetime.now(timezone.utc).isoformat()
        mock_data = {
            "status": "running",
            "task_id": "task-z2",
            "user_id": "user2",
            "updated_at": recent,
            "retry_count": "0",
            "max_retries": "1",
            "original_task": '{"task_id":"task-z2"}',
        }
        mock_redis = _make_mock_redis({"task:progress:task-z2": mock_data})

        with (
            patch("redis.from_url", return_value=mock_redis),
            patch("apps.agent.task_tracker._send_notification_redis") as mock_notify,
        ):
            result = check_task_health.apply().get()

        assert result["zombies"] == 0
        mock_notify.assert_not_called()

    def test_last_alive_prevents_false_positive(self):
        from apps.agent.tasks import check_task_health

        stale_updated = (
            datetime.now(timezone.utc) - timedelta(seconds=400)
        ).isoformat()
        recent_alive = datetime.now(timezone.utc).isoformat()
        mock_data = {
            "status": "running",
            "task_id": "task-z3",
            "user_id": "user3",
            "updated_at": stale_updated,
            "last_alive": recent_alive,
            "retry_count": "0",
            "max_retries": "1",
            "original_task": '{"task_id":"task-z3"}',
        }
        mock_redis = _make_mock_redis({"task:progress:task-z3": mock_data})

        with (
            patch("redis.from_url", return_value=mock_redis),
            patch("apps.agent.task_tracker._send_notification_redis") as mock_notify,
        ):
            result = check_task_health.apply().get()

        assert result["zombies"] == 0
        mock_notify.assert_not_called()

    def test_max_retries_notifies_user(self):
        from apps.agent.tasks import check_task_health

        stale = (datetime.now(timezone.utc) - timedelta(seconds=400)).isoformat()
        mock_data = {
            "status": "running",
            "task_id": "task-z4",
            "user_id": "user4",
            "updated_at": stale,
            "retry_count": "1",
            "max_retries": "1",
            "original_task": '{"task_id":"task-z4","user_id":"user4"}',
        }
        mock_redis = _make_mock_redis({"task:progress:task-z4": mock_data})

        with (
            patch("redis.from_url", return_value=mock_redis),
            patch("apps.agent.task_tracker._send_notification_redis") as mock_notify,
        ):
            result = check_task_health.apply().get()

        assert result["zombies"] == 1
        mock_notify.assert_called()

    def test_missing_original_task_notifies(self):
        from apps.agent.tasks import check_task_health

        stale = (datetime.now(timezone.utc) - timedelta(seconds=400)).isoformat()
        mock_data = {
            "status": "running",
            "task_id": "task-z5",
            "user_id": "user5",
            "updated_at": stale,
            "retry_count": "0",
            "max_retries": "1",
            "original_task": "",
        }
        mock_redis = _make_mock_redis({"task:progress:task-z5": mock_data})

        with (
            patch("redis.from_url", return_value=mock_redis),
            patch("apps.agent.task_tracker._send_notification_redis") as mock_notify,
        ):
            result = check_task_health.apply().get()

        assert result["zombies"] == 1
        mock_notify.assert_called()
        mock_redis.hset.assert_called_with("task:progress:task-z5", "status", "zombie")
