"""Tests for user_id resolution in scheduled agent tasks."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import unittest

from apps.agent.tasks import _resolve_scheduler_user_id


class TestResolveSchedulerUserId(unittest.TestCase):
    """Test _resolve_scheduler_user_id in tasks.py."""

    def test_non_empty_user_id_passthrough(self):
        """Non-empty user_id should pass through unchanged."""
        self.assertEqual(
            _resolve_scheduler_user_id("some-user-id"),
            "some-user-id",
        )

    def test_valid_uuid_passthrough(self):
        """UUID-format user_id should pass through unchanged."""
        self.assertEqual(
            _resolve_scheduler_user_id("a1b2c3d4-e5f6-7890-abcd-ef1234567890"),
            "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
        )

    def test_empty_user_id_resolves_to_scheduler(self):
        """Empty user_id should resolve to system_scheduler UUID."""
        mock_user = MagicMock()
        mock_user.id = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"

        mock_user_model = MagicMock()
        mock_user_model.objects.get.return_value = mock_user

        with patch("django.contrib.auth.get_user_model", return_value=mock_user_model):
            result = _resolve_scheduler_user_id("")
            self.assertEqual(result, "a1b2c3d4-e5f6-7890-abcd-ef1234567890")
            mock_user_model.objects.get.assert_called_once_with(
                username="system_scheduler"
            )

    def test_empty_user_id_fallback_on_error(self):
        """If system_scheduler user not found, return empty string."""
        mock_user_model = MagicMock()
        mock_user_model.objects.get.side_effect = Exception("user not found")

        with patch("django.contrib.auth.get_user_model", return_value=mock_user_model):
            result = _resolve_scheduler_user_id("")
            self.assertEqual(result, "")


class TestResolveDjangoUserIdFallback(unittest.IsolatedAsyncioTestCase):
    """Test _resolve_django_user_id fallback to system_scheduler in backtest.py."""

    async def test_empty_channel_user_id_uses_scheduler(self):
        """Empty channel_user_id should fallback to system_scheduler."""
        from apps.agent.tools.backtest import _resolve_django_user_id

        mock_user = MagicMock()
        mock_user.id = "scheduler-uuid-1234"

        async def _mock_get_scheduler():
            return str(mock_user.id)

        with patch(
            "apps.agent.tools.backtest._get_scheduler_user_id",
            side_effect=_mock_get_scheduler,
        ):
            result = await _resolve_django_user_id("")
            self.assertEqual(result, "scheduler-uuid-1234")

    async def test_none_channel_user_id_uses_scheduler(self):
        """None channel_user_id should also fallback to system_scheduler."""
        from apps.agent.tools.backtest import _resolve_django_user_id

        mock_user = MagicMock()
        mock_user.id = "scheduler-uuid-5678"

        async def _mock_get_scheduler():
            return str(mock_user.id)

        with patch(
            "apps.agent.tools.backtest._get_scheduler_user_id",
            side_effect=_mock_get_scheduler,
        ):
            result = await _resolve_django_user_id(None)
            self.assertEqual(result, "scheduler-uuid-5678")


if __name__ == "__main__":
    unittest.main()
