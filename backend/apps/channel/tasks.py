"""Celery 定时任务 — 飞书用户 Token 刷新。"""

import logging
from datetime import datetime, timedelta, timezone

from celery_app import app
from django.conf import settings
from django.db import transaction

logger = logging.getLogger(__name__)


def _get_oauth_client():
    from .oauth import FeishuOAuthClient

    client_id = getattr(settings, "LARK_APP_ID", "")
    client_secret = getattr(settings, "LARK_APP_SECRET", "")
    redirect_uri = getattr(
        settings,
        "LARK_OAUTH_REDIRECT_URI",
        "http://localhost:8000/api/channel/auth/lark/callback/",
    )
    return FeishuOAuthClient(client_id, client_secret, redirect_uri)


@app.task(
    name="apps.channel.tasks.refresh_feishu_user_tokens",
    bind=True,
    max_retries=3,
    default_retry_delay=300,
)
def refresh_feishu_user_tokens(self):
    """刷新所有即将过期的飞书用户 token。

    每 15 分钟运行一次，刷新未来 30 分钟内到期的 token。
    """
    from .models import FeishuUserToken

    soon = datetime.now(timezone.utc) + timedelta(minutes=30)
    with transaction.atomic():
        tokens = list(
            FeishuUserToken.objects.filter(
                is_active=True,
                expires_at__lte=soon,
            ).select_for_update(skip_locked=True)
        )

    refreshed = 0
    failed = 0

    for token in tokens:
        try:
            with transaction.atomic():
                client = _get_oauth_client()
                refresh_token_str = token.decrypt_refresh_token()

                if not refresh_token_str:
                    logger.error(
                        "[FeishuTokenRefresh] empty refresh_token for %s, deactivating",
                        token.open_id[:12],
                    )
                    token.is_active = False
                    token.save(update_fields=["is_active", "updated_at"])
                    failed += 1
                    continue

                token_data = client.refresh_token(refresh_token_str)

                expires_in = token_data.get("expires_in", 7200)
                token.access_token_enc = token.encrypt_access_token(
                    token_data["access_token"]
                )
                token.refresh_token_enc = token.encrypt_refresh_token(
                    token_data["refresh_token"]
                )
                token.expires_at = datetime.now(timezone.utc) + timedelta(
                    seconds=expires_in
                )
                token.save(
                    update_fields=[
                        "access_token_enc",
                        "refresh_token_enc",
                        "expires_at",
                        "updated_at",
                    ]
                )
                refreshed += 1
        except Exception as e:
            failed += 1
            logger.error(
                "[FeishuTokenRefresh] failed for %s: %s, deactivating token",
                token.open_id[:12],
                e,
            )
            token.is_active = False
            token.save(update_fields=["is_active", "updated_at"])

    if refreshed or failed:
        logger.info(
            "[FeishuTokenRefresh] done: %d refreshed, %d failed (of %d total)",
            refreshed,
            failed,
            len(tokens),
        )
