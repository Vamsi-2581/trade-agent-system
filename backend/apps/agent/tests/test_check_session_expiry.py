import json
import time
from unittest.mock import MagicMock, patch


class TestCheckSessionExpiry:
    """Tests for check_session_expiry Celery beat task."""

    def test_warns_near_expiry(self):
        from apps.agent.tasks import check_session_expiry

        near_expiry = time.time() + 200
        session_data = json.dumps(
            {
                "state": "multi_turn",
                "active_agent": "quant",
                "expires_at": near_expiry,
            }
        )

        mock_redis = MagicMock()
        mock_redis.scan_iter.return_value = ["session:user123:context"]
        mock_redis.get.return_value = session_data

        with (
            patch("redis.from_url", return_value=mock_redis),
            patch("apps.agent.task_tracker._send_notification_redis") as mock_notify,
        ):
            result = check_session_expiry.apply().get()

        assert result["warned"] == 1
        mock_notify.assert_called_once()

    def test_ignores_fresh_session(self):
        from apps.agent.tasks import check_session_expiry

        far_expiry = time.time() + 1000
        session_data = json.dumps(
            {
                "state": "multi_turn",
                "active_agent": "quant",
                "expires_at": far_expiry,
            }
        )

        mock_redis = MagicMock()
        mock_redis.scan_iter.return_value = ["session:user456:context"]
        mock_redis.get.return_value = session_data

        with (
            patch("redis.from_url", return_value=mock_redis),
            patch("apps.agent.task_tracker._send_notification_redis") as mock_notify,
        ):
            result = check_session_expiry.apply().get()

        assert result["warned"] == 0
        mock_notify.assert_not_called()

    def test_ignores_non_multi_turn_session(self):
        from apps.agent.tasks import check_session_expiry

        session_data = json.dumps(
            {
                "state": "none",
                "active_agent": None,
                "expires_at": time.time() + 200,
            }
        )

        mock_redis = MagicMock()
        mock_redis.scan_iter.return_value = ["session:user789:context"]
        mock_redis.get.return_value = session_data

        with (
            patch("redis.from_url", return_value=mock_redis),
            patch("apps.agent.task_tracker._send_notification_redis") as mock_notify,
        ):
            result = check_session_expiry.apply().get()

        assert result["warned"] == 0
        mock_notify.assert_not_called()

    def test_handles_malformed_json(self):
        from apps.agent.tasks import check_session_expiry

        mock_redis = MagicMock()
        mock_redis.scan_iter.return_value = ["session:userBad:context"]
        mock_redis.get.return_value = "not-valid-json{{{"

        with (
            patch("redis.from_url", return_value=mock_redis),
            patch("apps.agent.task_tracker._send_notification_redis") as mock_notify,
        ):
            result = check_session_expiry.apply().get()

        assert result["warned"] == 0
        mock_notify.assert_not_called()
