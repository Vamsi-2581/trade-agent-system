import secrets
import uuid
from datetime import datetime, timedelta, timezone
import logging

from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from .models import FeishuAuthSession, FeishuUserToken
from .oauth import FeishuOAuthClient

logger = logging.getLogger(__name__)


def _get_oauth_client() -> FeishuOAuthClient:
    """构造 OAuth 客户端，读取 settings 配置。"""
    client_id = getattr(settings, "LARK_APP_ID", "")
    client_secret = getattr(settings, "LARK_APP_SECRET", "")
    redirect_uri = getattr(
        settings,
        "LARK_OAUTH_REDIRECT_URI",
        "http://localhost:8000/api/channel/auth/lark/callback/",
    )
    if not client_id or not client_secret:
        raise RuntimeError("LARK_APP_ID / LARK_APP_SECRET not configured")
    return FeishuOAuthClient(client_id, client_secret, redirect_uri)


# ── OAuth 端点 ──


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def feishu_auth_url(request):
    """发起 URL 重定向授权，返回跳转链接。"""
    try:
        client = _get_oauth_client()
    except RuntimeError as e:
        return Response({"error": str(e)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

    state = secrets.token_urlsafe(32)
    session = FeishuAuthSession.objects.create(
        user=request.user,
        state=state,
        flow_type="url",
    )
    scope = getattr(settings, "LARK_OAUTH_SCOPES", "")
    auth_url = client.build_authorize_url(state, scope=scope)

    return Response(
        {
            "session_id": str(session.id),
            "authorize_url": auth_url,
        }
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def feishu_auth_qr(request):
    """发起 QR 码授权，返回 session_id 和授权 URL。"""
    try:
        client = _get_oauth_client()
    except RuntimeError as e:
        return Response({"error": str(e)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

    state = secrets.token_urlsafe(32)
    session = FeishuAuthSession.objects.create(
        user=request.user,
        state=state,
        flow_type="qr",
    )
    scope = getattr(settings, "LARK_OAUTH_SCOPES", "")
    auth_url = client.build_authorize_url(state, scope=scope)
    session.qr_url = auth_url
    session.qr_expires_at = datetime.now(timezone.utc) + timedelta(seconds=600)
    session.save(update_fields=["qr_url", "qr_expires_at", "updated_at"])

    return Response(
        {
            "session_id": str(session.id),
            "qr_url": auth_url,
            "expires_in": 600,
        }
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def feishu_auth_qr_status(request, session_id: str):
    """轮询 QR 授权状态。"""
    try:
        session = FeishuAuthSession.objects.get(
            id=uuid.UUID(session_id), user=request.user
        )
    except (FeishuAuthSession.DoesNotExist, ValueError):
        return Response(
            {"error": "Session not found"}, status=status.HTTP_404_NOT_FOUND
        )

    return Response(
        {
            "session_id": str(session.id),
            "status": session.status,
        }
    )


@api_view(["GET"])
@permission_classes([AllowAny])
def feishu_oauth_callback(request):
    """OAuth 回调端点 — 飞书重定向到此 URL，携带 code 和 state。"""
    code = request.query_params.get("code", "")
    state = request.query_params.get("state", "")

    if not code or not state:
        return JsonResponse({"error": "Missing code or state"}, status=400)

    try:
        session = FeishuAuthSession.objects.select_related("user").get(
            state=state, status="pending"
        )
    except FeishuAuthSession.DoesNotExist:
        return JsonResponse({"error": "Invalid or expired state"}, status=400)

    try:
        client = _get_oauth_client()
        token_data = client.exchange_code(code)
    except RuntimeError as e:
        session.status = "failed"
        session.save(update_fields=["status", "updated_at"])
        logger.warning("[FeishuOAuth] exchange_code failed: %s", e)
        return JsonResponse({"error": "Token exchange failed"}, status=400)

    open_id = token_data.get("open_id", "")
    if not open_id:
        # v2 token endpoint doesn't return user info; call user_info API
        try:
            user_info = client.get_user_info(token_data["access_token"])
            open_id = user_info.get("open_id", "")
            token_data["name"] = user_info.get("name", "")
            token_data["avatar_url"] = user_info.get("avatar_url", "")
        except RuntimeError as e:
            session.status = "failed"
            session.save(update_fields=["status", "updated_at"])
            logger.warning("[FeishuOAuth] get_user_info failed: %s", e)
            return JsonResponse({"error": "Failed to get user identity"}, status=400)

    if not open_id:
        session.status = "failed"
        session.save(update_fields=["status", "updated_at"])
        return JsonResponse({"error": "No open_id in token response"}, status=400)

    access_token = token_data["access_token"]
    refresh_token = token_data["refresh_token"]
    if not refresh_token:
        session.status = "failed"
        session.save(update_fields=["status", "updated_at"])
        logger.error(
            "[FeishuOAuth] empty refresh_token in exchange response for user %s",
            session.user.username,
        )
        return JsonResponse(
            {"error": "OAuth response missing refresh_token"}, status=400
        )
    expires_in = token_data.get("expires_in", 7200)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

    # 用临时实例加密 token
    _tmp = FeishuUserToken()
    FeishuUserToken.objects.update_or_create(
        open_id=open_id,
        defaults={
            "user": session.user,
            "access_token_enc": _tmp.encrypt_access_token(access_token),
            "refresh_token_enc": _tmp.encrypt_refresh_token(refresh_token),
            "expires_at": expires_at,
            "is_active": True,
        },
    )

    session.status = "completed"
    session.save(update_fields=["status", "updated_at"])

    logger.info(
        "[FeishuOAuth] user %s authorized open_id=%s",
        session.user.username,
        open_id[:12],
    )

    return JsonResponse({"status": "ok", "message": "Authorization successful"})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def feishu_refresh_token(request):
    """手动刷新用户飞书 token。"""
    try:
        token = FeishuUserToken.objects.select_for_update().get(
            user=request.user, is_active=True
        )
    except FeishuUserToken.DoesNotExist:
        return Response(
            {"error": "No active token found"}, status=status.HTTP_404_NOT_FOUND
        )

    try:
        client = _get_oauth_client()
        refresh_token = token.decrypt_refresh_token()
        token_data = client.refresh_token(refresh_token)
    except Exception as e:
        return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    expires_at = datetime.now(timezone.utc) + timedelta(
        seconds=token_data.get("expires_in", 7200)
    )

    token.access_token_enc = token.encrypt_access_token(token_data["access_token"])
    token.refresh_token_enc = token.encrypt_refresh_token(token_data["refresh_token"])
    token.expires_at = expires_at
    token.save(
        update_fields=[
            "access_token_enc",
            "refresh_token_enc",
            "expires_at",
            "updated_at",
        ]
    )

    return Response({"status": "ok", "expires_at": expires_at.isoformat()})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def feishu_auth_status(request):
    """检查用户是否有活跃的飞书授权。"""
    token = FeishuUserToken.objects.filter(user=request.user, is_active=True).first()

    if not token:
        return Response({"authorized": False})

    return Response(
        {
            "authorized": True,
            "open_id": token.open_id,
            "expires_at": token.expires_at.isoformat(),
            "created_at": token.created_at.isoformat(),
        }
    )


@api_view(["DELETE"])
@permission_classes([IsAuthenticated])
def feishu_revoke(request):
    """撤销用户飞书授权。"""
    count, _ = FeishuUserToken.objects.filter(user=request.user, is_active=True).update(
        is_active=False
    )

    if count == 0:
        return Response(
            {"error": "No active token found"}, status=status.HTTP_404_NOT_FOUND
        )

    return Response({"status": "ok", "message": "Authorization revoked"})


# ── 原有端点 ──


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def channel_status(request):
    """Return channel/framework status"""
    from apps.agent.frame_manager import FrameManager

    fm = FrameManager.get_instance()
    return Response(fm.status())


@csrf_exempt
@require_POST
def lark_webhook(request):
    """飞书事件回调端点。

    飞书开放平台会将事件推到此地址：POST /api/channel/webhook/lark/
    """
    import os
    from django.conf import settings
    from .lark import LarkChannel

    # Prefer os.environ (load_dotenv populates this); fallback to Django settings
    app_id = os.environ.get("LARK_APP_ID", "")
    app_secret = os.environ.get("LARK_APP_SECRET", "")
    if not app_id or not app_secret:
        app_id = getattr(settings, "LARK_APP_ID", "")
        app_secret = getattr(settings, "LARK_APP_SECRET", "")
    verification_token = os.environ.get(
        "LARK_VERIFICATION_TOKEN",
        getattr(settings, "LARK_VERIFICATION_TOKEN", ""),
    )
    encrypt_key = os.environ.get(
        "LARK_ENCRYPT_KEY",
        getattr(settings, "LARK_ENCRYPT_KEY", ""),
    )

    if not app_id or not app_secret:
        return JsonResponse({"error": "Lark not configured"}, status=503)

    channel = LarkChannel(
        app_id=app_id,
        app_secret=app_secret,
        verification_token=verification_token,
        encrypt_key=encrypt_key,
    )
    return channel.handle_webhook(request)
