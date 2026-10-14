"""Channel resolution utility — determines which channel (Telegram/Feishu) is active for a user."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class ChannelTarget:
    """Resolved channel info for sending a notification to a user."""

    channel_type: str  # "telegram" | "lark"
    chat_id: str  # Telegram chat_id or Feishu chat_id
    user_open_id: str = ""  # Feishu open_id (empty for Telegram)


def get_user_active_channel(user_id: str) -> Optional[ChannelTarget]:
    """Determine the active notification channel for a user.

    Only returns a channel if it matches the configured MAIN_CHANNEL setting
    (default: "lark"). If the user does not have the main channel configured,
    returns None and the notification is dropped.

    Args:
        user_id: Telegram user ID (numeric string) or Feishu open_id.

    Returns:
        ChannelTarget if a channel is found and matches MAIN_CHANNEL, None otherwise.
    """
    if not user_id or user_id == "unknown":
        return None

    from django.conf import settings

    from apps.authentication.models import User

    main_channel = getattr(settings, "MAIN_CHANNEL", "lark")

    # Try to find user by multiple fields (telegram_id, feishu_open_id, id, username)
    user = User.objects.filter(telegram_id=user_id).first()
    if not user:
        user = User.objects.filter(feishu_open_id=user_id).first()
    if not user:
        # Try by Django User UUID
        try:
            import uuid

            uuid.UUID(user_id)  # Validate it's a UUID
            user = User.objects.filter(id=user_id).first()
        except (ValueError, AttributeError):
            pass
    if not user:
        # Try by username
        user = User.objects.filter(username=user_id).first()
    if not user:
        logger.debug("[ChannelResolver] user %s not found in DB", user_id)
        return None

    # Only return the channel if it matches MAIN_CHANNEL
    if main_channel == "lark" and user.feishu_open_id:
        return ChannelTarget(
            channel_type="lark",
            chat_id="",  # Resolved at send time from LarkChannel instance
            user_open_id=user.feishu_open_id,
        )

    if main_channel == "telegram" and user.telegram_chat_id:
        return ChannelTarget(
            channel_type="telegram",
            chat_id=str(user.telegram_chat_id),
        )

    if main_channel == "tui":
        # TUI is in-process; return a marker so the caller can handle it
        return ChannelTarget(
            channel_type="tui",
            chat_id="",
        )

    logger.debug(
        "[ChannelResolver] user %s has no active channel for main_channel=%s",
        user_id,
        main_channel,
    )
    return None


def resolve_user_channel_for_task(task_data: dict) -> Optional[ChannelTarget]:
    """Resolve channel from a task's Redis data or user_id.

    The task tracker stores `channel` and `user_id` in the Redis hash.
    This function combines those with DB lookup to find the right target.

    If the stored channel does not match MAIN_CHANNEL, it is overridden
    to the main channel.

    Args:
        task_data: Dict from TaskTracker Redis hash (contains user_id, channel).

    Returns:
        ChannelTarget or None.
    """
    from django.conf import settings

    user_id = task_data.get("user_id", "")
    stored_channel = task_data.get("channel", "telegram")
    main_channel = getattr(settings, "MAIN_CHANNEL", "lark")

    if not user_id or user_id == "unknown":
        return None

    # If the stored channel does not match MAIN_CHANNEL, ignore it
    if stored_channel != main_channel:
        logger.debug(
            "[ChannelResolver] task channel=%s overridden by MAIN_CHANNEL=%s",
            stored_channel,
            main_channel,
        )

    # DB-based resolution (enforces MAIN_CHANNEL)
    return get_user_active_channel(user_id)
