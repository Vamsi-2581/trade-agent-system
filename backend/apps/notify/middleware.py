"""
Custom ASGI middleware that authenticates WebSocket connections
using JWT token passed via query parameter (?token=xxx).

Central token validation lives here (authenticate_token) and is shared with
ChatConsumer so the backend logs the SAME precise rejection reason everywhere:
expired / signature mismatch / malformed / user missing or inactive.
"""

import logging
from urllib.parse import parse_qs

from asgiref.sync import sync_to_async
from channels.middleware import BaseMiddleware
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import AccessToken

logger = logging.getLogger(__name__)


def _token_failure_reason(exc):
    """Classify a JWT validation failure into an actionable log message."""
    from jwt import DecodeError, ExpiredSignatureError, InvalidSignatureError
    from rest_framework_simplejwt.backends import TokenBackendError

    # AccessToken() wraps TokenBackendError -> TokenError, chaining the
    # original PyJWT error. Walk the chain to find the root cause.
    cause = getattr(exc, "__context__", None) or getattr(exc, "__cause__", None)
    inner = getattr(cause, "__context__", None) or getattr(cause, "__cause__", None)

    if isinstance(inner, ExpiredSignatureError) or "expired" in str(exc).lower():
        return "token expired"
    if isinstance(inner, InvalidSignatureError):
        return "invalid signature (SECRET_KEY mismatch or tampered token)"
    if isinstance(inner, DecodeError):
        return "malformed token (decode error)"
    if isinstance(cause, TokenBackendError):
        return f"token decode failed: {inner or cause}"
    return f"token rejected: {exc}"


def authenticate_token(token_string):
    """Validate a JWT token and resolve its user. Returns (user, reason).

    - On success user is not None and reason is None.
    - On failure user is None and reason explains the failure
      (expired / signature / malformed / user missing or inactive).
    """
    try:
        access_token = AccessToken(token_string)
    except TokenError as exc:
        return None, _token_failure_reason(exc)
    except Exception as exc:  # defensive: never let auth errors propagate
        return None, f"unexpected token error: {exc!r}"

    user_id = access_token.get("user_id")
    if user_id is None:
        return None, "token missing user_id claim"

    from django.contrib.auth import get_user_model

    User = get_user_model()
    try:
        user = User.objects.get(id=user_id, is_active=True)
        return user, None
    except User.DoesNotExist:
        return None, f"user {user_id} not found or inactive"
    except Exception as exc:
        return None, f"user {user_id} lookup error: {exc!r}"


class TokenAuthMiddleware(BaseMiddleware):
    """
    ASGI middleware that reads JWT token from query parameter 'token'
    and sets scope['user']. Falls back to AnonymousUser if no token.
    """

    async def resolve_scope(self, scope):
        from django.contrib.auth.models import AnonymousUser

        query_string = scope.get("query_string", b"").decode()
        query_params = parse_qs(query_string)
        token_list = query_params.get("token", [])

        user = None
        if token_list:
            token = token_list[0]
            user, reason = await sync_to_async(authenticate_token)(token)
            if user:
                logger.info(f"[TokenAuthMiddleware] Authenticated user: {user.id}")
            else:
                logger.warning(
                    "[TokenAuthMiddleware] Auth failed for token %s...: %s",
                    token[:20],
                    reason,
                )
        else:
            logger.debug("[TokenAuthMiddleware] No token in query string")

        scope["user"] = user or AnonymousUser()
        return scope
