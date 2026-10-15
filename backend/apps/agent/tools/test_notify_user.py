"""Tests for NotifyUserTool."""

import json
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth import get_user_model

from apps.agent.tools.notify_user import NotifyUserTool
from apps.notify.models import Notification

User = get_user_model()


@pytest.fixture
def tool():
    return NotifyUserTool()


@pytest.fixture
def user(db):
    """Create a test user.

    ``UserManager.create_user`` only accepts ``(email, username, password)``;
    profile fields like ``telegram_chat_id`` are set through the plain manager.
    """
    return User.objects.create(
        email="test_notify_user@example.com",
        username="test_notify_user",
        telegram_chat_id=123456789,
    )


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
class TestNotifyUserTool:
    async def test_valid_message_with_user_id(self, tool, user, db):
        """Valid message + user_id → success, notification persisted, Redis published."""
        with patch("redis.from_url") as mock_from_url:
            mock_r = MagicMock()
            mock_from_url.return_value = mock_r

            result = await tool.execute(message="BTC 突破 10 万", user_id=str(user.id))

        assert result.success is True
        assert result.data == {"message": "通知已发送"}

        # Verify Notification model persisted
        notifications = Notification.objects.filter(user=user)
        assert notifications.count() == 1
        assert "BTC 突破 10 万" in notifications.first().message
        assert notifications.first().channel == "web"

        # Verify Redis publish called
        mock_from_url.assert_called_once()
        mock_r.publish.assert_called_once()
        publish_args = mock_r.publish.call_args[0]
        payload = json.loads(publish_args[1])
        assert payload["user_id"] == str(user.id)
        assert "BTC 突破 10 万" in payload["text"]

    async def test_empty_message_returns_error(self, tool):
        """Empty message → error."""
        result = await tool.execute(message="", user_id="some-user-id")
        assert result.success is False
        assert "不能为空" in result.error

    async def test_whitespace_message_returns_error(self, tool):
        """Whitespace-only message → error."""
        result = await tool.execute(message="   ", user_id="some-user-id")
        assert result.success is False
        assert "不能为空" in result.error

    async def test_missing_user_id_returns_error(self, tool):
        """No user_id → error."""
        result = await tool.execute(message="hello")
        assert result.success is False
        assert "无法确定通知目标用户" in result.error

    async def test_message_truncated_at_max_length(self, tool, user, db):
        """Message longer than 4000 chars is truncated."""
        long_message = "A" * 5000

        with patch("redis.from_url") as mock_from_url:
            mock_r = MagicMock()
            mock_from_url.return_value = mock_r

            result = await tool.execute(message=long_message, user_id=str(user.id))

        assert result.success is True

        # Notification stored with truncation marker
        notification = Notification.objects.filter(user=user).first()
        assert notification is not None
        assert len(notification.message) <= 4010  # 4000 + "…（已截断）"
        assert "已截断" in notification.message

    async def test_redis_failure_does_not_crash(self, tool, user, db):
        """Redis pubsub failure → tool still returns success (best-effort delivery)."""
        with patch("redis.from_url") as mock_from_url:
            mock_from_url.side_effect = Exception("Redis unavailable")

            result = await tool.execute(message="test message", user_id=str(user.id))

        # Notification should still be persisted
        assert Notification.objects.filter(user=user).count() == 1
        # Tool should still succeed (Redis failure is logged but not surfaced)
        assert result.success is True

    async def test_user_lookup_by_username(self, tool, db):
        """When user_id is a username string, lookup works."""
        user = User.objects.create(
            email="notify_by_name@example.com",
            username="notify_by_name",
            telegram_chat_id=987654321,
        )

        with patch("redis.from_url") as mock_from_url:
            mock_r = MagicMock()
            mock_from_url.return_value = mock_r

            result = await tool.execute(message="hello", user_id="notify_by_name")

        assert result.success is True
        assert Notification.objects.filter(user=user).count() == 1
