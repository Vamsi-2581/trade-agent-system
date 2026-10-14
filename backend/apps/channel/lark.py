from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import requests
from .base import BaseChannel, chunk_message

logger = logging.getLogger(__name__)

# 飞书 Open API 单条文本消息安全上限（API 整体 30KB，留余量给 JSON 开销）
_LARK_MAX_LENGTH = 3800


class LarkChannel(BaseChannel):
    """飞书（Lark/Feishu）Channel — 通过 Django webhook 接收事件 + Open API 发送消息。

    使用方式：
    1. 在飞书开放平台创建企业自建应用，启用 Bot 能力
    2. 配置事件订阅：开启 im.message.receive_v1，回调地址指向
       /api/channel/webhook/lark/
    3. 填写 .env：LARK_APP_ID, LARK_APP_SECRET, LARK_VERIFICATION_TOKEN
    """

    name = "lark"

    # 飞书 Open API 端点
    _TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    _SEND_URL = "https://open.feishu.cn/open-apis/im/v1/messages"
    _IMG_UPLOAD_URL = "https://open.feishu.cn/open-apis/im/v1/images"

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        verification_token: str = "",
        encrypt_key: str = "",
        supervisor_agent=None,
    ):
        self._app_id = app_id
        self._app_secret = app_secret
        self._verification_token = verification_token
        self._encrypt_key = encrypt_key
        self._supervisor = supervisor_agent
        self._tenant_token: str = ""
        self._tenant_token_expire_at: float = 0
        self._chat_id: str = ""
        # Per-user token fields
        self._user_open_id: str = ""
        self._user_access_token: str = ""
        self._user_token_expire_at: float = 0

    # ── BaseChannel 接口 ──

    async def send_message(self, text: str) -> None:
        if not self._chat_id:
            logger.warning("[LarkChannel] no chat_id set, skipping send_message")
            return
        for chunk in chunk_message(text, _LARK_MAX_LENGTH):
            await self._send_im(self._chat_id, "text", {"text": chunk})

    async def send_message_to_user(self, open_id: str, text: str) -> None:
        """Send a message to a specific user via their open_id.

        Used for async notifications where we don't have an active chat_id.
        """
        if not open_id:
            logger.warning("[LarkChannel] no open_id, skipping send_message_to_user")
            return
        for chunk in chunk_message(text, _LARK_MAX_LENGTH):
            await self._send_im(
                open_id, "text", {"text": chunk}, receive_id_type="open_id"
            )

    async def send_photo(self, photo_bytes: bytes, caption: str = "") -> None:
        if not self._chat_id:
            logger.warning("[LarkChannel] no chat_id set, skipping send_photo")
            return
        image_key = await self._upload_image(self._chat_id, photo_bytes)
        if image_key:
            if caption:
                await self._send_im(self._chat_id, "image", {"image_key": image_key})
                await self._send_im(self._chat_id, "text", {"text": caption})
            else:
                await self._send_im(self._chat_id, "image", {"image_key": image_key})

    async def start(self) -> None:
        await self._refresh_token()
        logger.info("[LarkChannel] initialized (Django webhook mode)")

    async def stop(self) -> None:
        self._tenant_token = ""
        logger.info("[LarkChannel] stopped")

    # ── Django webhook 入口（由 views.py 调用） ──

    def handle_webhook(self, request) -> Any:
        """处理飞书事件回调。返回 HttpResponse 对象。"""
        from django.http import JsonResponse

        body = request.body.decode("utf-8")

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            logger.warning("[LarkChannel] invalid JSON body")
            return JsonResponse({"error": "invalid JSON"}, status=400)

        # URL 验证挑战（首次配置回调地址时飞书发送）
        if payload.get("type") == "url_verification":
            token = payload.get("token", "")
            challenge = payload.get("challenge", "")
            if token != self._verification_token:
                return JsonResponse({"error": "token mismatch"}, status=403)
            logger.info("[LarkChannel] URL verification passed")
            return JsonResponse({"challenge": challenge})

        # 验证签名（如果设置了 encrypt_key）
        if self._encrypt_key:
            timestamp = request.headers.get("X-Lark-Request-Timestamp", "")
            nonce = request.headers.get("X-Lark-Request-Nonce", "")
            signature = request.headers.get("X-Lark-Request-Signature", "")
            if not self._verify_signature(timestamp, nonce, signature, body):
                logger.warning("[LarkChannel] signature verification failed")
                return JsonResponse({"error": "signature mismatch"}, status=403)

        # 处理事件
        event_type = payload.get("header", {}).get("event_type", "")
        if event_type == "im.message.receive_v1":
            self._on_message_received(payload)

        return JsonResponse({"code": 0})

    # ── 内部方法 ──

    async def _refresh_token(self) -> str:
        """获取访问 token，优先级：用户 access_token → 全局 tenant token。"""
        # 1. 用户 token 优先
        if self._user_access_token and time.time() < self._user_token_expire_at:
            return self._user_access_token

        # 2. 用户 token 过期或不存在，尝试懒刷新
        if self._user_open_id:
            try:
                await self._refresh_user_token_from_db()
                if self._user_access_token and time.time() < self._user_token_expire_at:
                    return self._user_access_token
            except Exception as e:
                logger.warning("[LarkChannel] user token refresh failed: %s", e)

        # 3. 降级到全局 tenant token
        if self._tenant_token and time.time() < self._tenant_token_expire_at:
            return self._tenant_token
        try:
            return await self._refresh_tenant_token()
        except Exception as e:
            logger.error("[LarkChannel] tenant token refresh failed: %s", e)
            raise

    async def _refresh_user_token_from_db(self) -> None:
        """从 DB 加载用户 token，如果过期则尝试刷新。"""
        from django.conf import settings
        from django.db import transaction
        from asgiref.sync import sync_to_async

        from .models import FeishuUserToken
        from .oauth import FeishuOAuthClient

        @sync_to_async
        def _load_and_refresh_token():
            # Step 1: Read token under lock, check if refresh needed
            with transaction.atomic():
                try:
                    token_obj = FeishuUserToken.objects.select_for_update().get(
                        open_id=self._user_open_id, is_active=True
                    )
                except FeishuUserToken.DoesNotExist:
                    return None

                now = datetime.now(timezone.utc)
                if token_obj.expires_at > now:
                    return (
                        "cached",
                        token_obj.decrypt_access_token(),
                        token_obj.expires_at,
                    )

                # Token expired, read refresh_token while holding lock
                refresh_token = token_obj.decrypt_refresh_token()
                if not refresh_token:
                    logger.error(
                        "[LarkChannel] empty refresh_token for %s, deactivating",
                        token_obj.open_id[:12],
                    )
                    token_obj.is_active = False
                    token_obj.save(update_fields=["is_active", "updated_at"])
                    return None

            # Step 2: Call API OUTSIDE atomic block (avoids rollback of deactivation on failure)
            redirect_uri = getattr(
                settings,
                "LARK_OAUTH_REDIRECT_URI",
                "http://localhost:8000/api/channel/auth/lark/callback/",
            )
            client = FeishuOAuthClient(self._app_id, self._app_secret, redirect_uri)
            try:
                token_data = client.refresh_token(refresh_token)
            except Exception as e:
                logger.error(
                    "[LarkChannel] refresh failed for %s: %s, deactivating",
                    token_obj.open_id[:12],
                    e,
                )
                token_obj.is_active = False
                token_obj.save(update_fields=["is_active", "updated_at"])
                raise

            # Step 3: Save new tokens

            expires_in = token_data.get("expires_in", 7200)
            token_obj.access_token_enc = token_obj.encrypt_access_token(
                token_data["access_token"]
            )
            token_obj.refresh_token_enc = token_obj.encrypt_refresh_token(
                token_data["refresh_token"]
            )
            token_obj.expires_at = now + timedelta(seconds=expires_in)
            token_obj.save(
                update_fields=[
                    "access_token_enc",
                    "refresh_token_enc",
                    "expires_at",
                    "updated_at",
                ]
            )
            return ("refreshed", token_data["access_token"], token_obj.expires_at)

        result = await _load_and_refresh_token()
        if result is None:
            self._user_access_token = ""
            self._user_token_expire_at = 0
            return

        status, access_token, expires_at = result
        self._user_access_token = access_token
        self._user_token_expire_at = (
            expires_at - datetime.now(timezone.utc)
        ).total_seconds()
        logger.info(
            "[LarkChannel] user token %s for %s (expires in %.0fs)",
            status,
            self._user_open_id[:12],
            self._user_token_expire_at,
        )

    async def _refresh_tenant_token(self) -> str:
        """获取全局 tenant_access_token。"""
        try:
            resp = await asyncio.to_thread(
                requests.post,
                self._TOKEN_URL,
                json={"app_id": self._app_id, "app_secret": self._app_secret},
                timeout=10,
            )
            data = resp.json()
            if data.get("code") != 0:
                raise RuntimeError(f"[LarkChannel] token request failed: {data}")

            self._tenant_token = data["tenant_access_token"]
            expire = data.get("expire", 7200)
            self._tenant_token_expire_at = time.time() + expire - 300
            logger.info(
                "[LarkChannel] tenant_access_token refreshed (expires in %ds)", expire
            )
            return self._tenant_token
        except Exception as e:
            raise RuntimeError(f"[LarkChannel] tenant token refresh failed: {e}") from e

    async def _send_im(
        self,
        receive_id: str,
        msg_type: str,
        content: dict,
        receive_id_type: str = "chat_id",
    ) -> None:
        try:
            token = await self._refresh_token()
        except Exception as e:
            logger.error(
                "[LarkChannel] token refresh failed, cannot send message: %s", e
            )
            return
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        }
        params = {"receive_id_type": receive_id_type}
        body = {
            "receive_id": receive_id,
            "msg_type": msg_type,
            "content": json.dumps(content, ensure_ascii=False),
        }
        try:
            resp = await asyncio.to_thread(
                requests.post,
                self._SEND_URL,
                headers=headers,
                params=params,
                json=body,
                timeout=15,
            )
            data = resp.json()
            if data.get("code") != 0:
                logger.error("[LarkChannel] send message failed: %s", data)
            else:
                logger.debug("[LarkChannel] message sent to %s", receive_id)
        except Exception as e:
            logger.error("[LarkChannel] send message error: %s", e)

    async def _upload_image(self, receive_id: str, photo_bytes: bytes) -> str | None:
        try:
            token = await self._refresh_token()
        except Exception as e:
            logger.error(
                "[LarkChannel] token refresh failed, cannot upload image: %s", e
            )
            return None
        headers = {"Authorization": f"Bearer {token}"}
        form = {
            "image_type": "message",
            "image": ("image.png", photo_bytes, "image/png"),
        }
        try:
            resp = await asyncio.to_thread(
                requests.post,
                self._IMG_UPLOAD_URL,
                headers=headers,
                files=form,
                timeout=15,
            )
            data = resp.json()
            if data.get("code") == 0:
                return data["data"]["image_key"]
            logger.error("[LarkChannel] upload image failed: %s", data)
        except Exception as e:
            logger.error("[LarkChannel] upload image error: %s", e)
        return None

    def _on_message_received(self, payload: dict) -> None:
        import asyncio

        event = payload.get("event", {})
        message = event.get("message", {})
        sender = event.get("sender", {})

        chat_id = message.get("chat_id", "")
        msg_type = message.get("message_type", "")
        content_raw = message.get("content", "{}")

        user_id = sender.get("sender_id", {}).get("open_id", "")
        self._chat_id = chat_id
        self._user_open_id = user_id

        if msg_type != "text":
            logger.info("[LarkChannel] ignoring non-text message type: %s", msg_type)
            return

        try:
            content = json.loads(content_raw)
            text = content.get("text", "").strip()
        except json.JSONDecodeError:
            text = content_raw.strip()

        if not text:
            return

        logger.info("[LarkChannel] received from %s in %s: %s", user_id, chat_id, text)

        # Save feishu_open_id to User model for channel resolution
        if user_id:
            self._save_feishu_user(user_id, chat_id)
            from django.db import connections

            connections.close_all()

        from apps.agent.bus import publish, build_agent_task, wait_reply, AGENT_TASKS

        msg = build_agent_task(
            user_id=user_id or "unknown", payload={"text": text}, origin="lark"
        )

        import os

        os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = "true"
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(publish(AGENT_TASKS, msg))
            reply = loop.run_until_complete(wait_reply(msg["task_id"], timeout=3600))
            if reply:
                processed = self._extract_content(reply)
                loop.run_until_complete(self.send_message(processed))
            else:
                loop.run_until_complete(
                    self.send_message("[超时] Agent 处理超时，请稍后重试")
                )
        finally:
            loop.close()
            from django import db as django_db

            django_db.connections.close_all()

    def _extract_content(self, reply) -> str:
        try:
            if isinstance(reply, str) and reply.strip().startswith("{"):
                parsed = json.loads(reply)
                if isinstance(parsed, dict) and "content" in parsed:
                    return parsed["content"]
            elif isinstance(reply, dict) and "content" in reply:
                return reply["content"]
        except (json.JSONDecodeError, TypeError):
            pass
        return str(reply)

    def _verify_signature(
        self, timestamp: str, nonce: str, signature: str, body: str
    ) -> bool:
        if not all([timestamp, nonce, signature]):
            return False
        content = timestamp + nonce + self._encrypt_key + body
        computed = hashlib.sha256(content.encode("utf-8")).hexdigest()
        return hmac.compare_digest(computed, signature)

    def _save_feishu_user(self, open_id: str, chat_id: str) -> None:
        """Save or update the user's feishu_open_id in the database."""
        try:
            from apps.authentication.models import User

            user = User.objects.filter(feishu_open_id=open_id).first()
            if not user:
                # Try to find by telegram_id (user may have used Telegram before)
                user = User.objects.filter(telegram_id__isnull=False).first()
            if user:
                if user.feishu_open_id != open_id:
                    user.feishu_open_id = open_id
                    user.save(update_fields=["feishu_open_id"])
                    logger.info(
                        "[LarkChannel] saved feishu_open_id for user %s: %s",
                        user.username,
                        open_id[:12],
                    )
            else:
                # Auto-create a new User for first-time Feishu users
                user = User.objects.create(
                    email=f"{open_id}@lark.local",
                    username=f"lark_{open_id[:8]}",
                    feishu_open_id=open_id,
                )
                logger.info(
                    "[LarkChannel] auto-created user %s with feishu_open_id: %s",
                    user.username,
                    open_id[:12],
                )
        except Exception as e:
            logger.warning("[LarkChannel] failed to save feishu user info: %s", e)
