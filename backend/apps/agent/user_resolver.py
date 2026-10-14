from __future__ import annotations

import logging
import uuid as _uuid

logger = logging.getLogger(__name__)


def _looks_like_uuid(s: str) -> bool:
    """Check if string looks like a UUID."""
    try:
        _uuid.UUID(s)
        return True
    except (ValueError, AttributeError):
        return False


async def resolve_user_id(raw_id: str) -> str | None:
    """Resolve channel user_id to Django User UUID.

    Handles multiple formats:
    - Numeric: telegram_chat_id or Django User pk
    - UUID string: Django User pk
    - "ou_*": Feishu open_id
    - Other: telegram_id

    Returns None if user not found.
    """
    if not raw_id:
        return None

    from django.contrib.auth import get_user_model

    User = get_user_model()

    try:
        if raw_id.isdigit():
            try:
                user = await User.objects.aget(telegram_chat_id=int(raw_id))
            except User.DoesNotExist:
                user = await User.objects.aget(pk=int(raw_id))
        elif _looks_like_uuid(raw_id):
            user = await User.objects.aget(pk=_uuid.UUID(raw_id))
        elif raw_id.startswith("ou_"):
            user = await User.objects.aget(feishu_open_id=raw_id)
        else:
            user = await User.objects.aget(telegram_id=raw_id)
        return user.id
    except (
        User.DoesNotExist,
        User.MultipleObjectsReturned,
        AttributeError,
        ValueError,
    ):
        return None


async def get_scheduler_user_id() -> str | None:
    """Get the system_scheduler user ID for scheduled tasks without user context."""
    try:
        from django.contrib.auth import get_user_model

        User = get_user_model()
        scheduler = await User.objects.aget(username="system_scheduler")
        return scheduler.id
    except Exception:
        return None
