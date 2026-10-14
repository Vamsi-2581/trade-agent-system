"""飞书 WebSocket 长连接客户端 — 无需公网 IP 即可接收事件回调。"""

import asyncio
import json
import logging
import os
import signal
import sys
import threading

import lark_oapi as lark

logger = logging.getLogger(__name__)

# 全局 channel 实例（懒加载）
_channel = None


class DebugEventHandler:
    """包装 EventDispatcherHandler，在事件分发前打印原始 payload。"""

    def __init__(self, handler: lark.EventDispatcherHandler):
        self._handler = handler

    def _do_without_validation(self, payload: bytes):
        pl = payload.decode("utf-8")
        logger.info("[LarkWS] RAW event payload (first 500 chars): %s", pl[:500])
        return self._handler._do_without_validation(payload)


def _get_channel():
    """全局单例 LarkChannel，用于发送消息。"""
    global _channel
    if _channel is None:
        from django.conf import settings
        from apps.channel.lark import LarkChannel

        app_id = os.environ.get("LARK_APP_ID", "") or getattr(
            settings, "LARK_APP_ID", ""
        )
        app_secret = os.environ.get("LARK_APP_SECRET", "") or getattr(
            settings, "LARK_APP_SECRET", ""
        )
        verification_token = os.environ.get("LARK_VERIFICATION_TOKEN", "") or getattr(
            settings, "LARK_VERIFICATION_TOKEN", ""
        )
        encrypt_key = os.environ.get("LARK_ENCRYPT_KEY", "") or getattr(
            settings, "LARK_ENCRYPT_KEY", ""
        )

        if not app_id or not app_secret:
            raise RuntimeError("LARK_APP_ID / LARK_APP_SECRET not configured")

        _channel = LarkChannel(
            app_id=app_id,
            app_secret=app_secret,
            verification_token=verification_token,
            encrypt_key=encrypt_key,
        )
    return _channel


def _parse_content(content_raw, msg_type) -> str:
    """解析消息 content 字段，处理 text 和 post 类型。"""
    if msg_type == "text":
        return _parse_text_content(content_raw)
    if msg_type == "post":
        return _parse_post_content(content_raw)
    return ""


def _parse_text_content(content_raw) -> str:
    try:
        if isinstance(content_raw, str):
            parsed = json.loads(content_raw)
            return parsed.get("text", "").strip()
        elif isinstance(content_raw, dict):
            return content_raw.get("text", "").strip()
    except (json.JSONDecodeError, AttributeError):
        pass
    return ""


def _parse_post_content(content_raw) -> str:
    try:
        if isinstance(content_raw, str):
            parsed = json.loads(content_raw)
        elif isinstance(content_raw, dict):
            parsed = content_raw
        else:
            return ""

        parts = []
        title = parsed.get("title", "")
        if title:
            parts.append(title)

        for paragraph in parsed.get("content", []):
            line = "".join(
                elem.get("text", "")
                for elem in paragraph
                if isinstance(elem, dict) and elem.get("tag") == "text"
            )
            if line.strip():
                parts.append(line.strip())

        return "\n".join(parts)
    except (json.JSONDecodeError, AttributeError):
        return ""


def handle_message(data: lark.im.v1.P2ImMessageReceiveV1) -> None:
    """处理 im.message.receive_v1 事件。"""
    logger.info(
        "[LarkWS] message received: uuid=%s, ts=%s",
        getattr(data, "uuid", ""),
        getattr(data, "ts", ""),
    )

    event_data = getattr(data, "event", None)
    if event_data is None:
        logger.warning("[LarkWS] no event data in P2ImMessageReceiveV1")
        return

    # Extract sender open_id
    sender = getattr(event_data, "sender", None)
    user_open_id = ""
    if sender:
        sender_id = getattr(sender, "sender_id", None)
        if sender_id:
            user_open_id = getattr(sender_id, "open_id", "") or ""

    # Extract message fields
    message = getattr(event_data, "message", None)
    if message is None:
        logger.warning("[LarkWS] no message in event data")
        return

    chat_id = getattr(message, "chat_id", "") or ""
    # SDK protobuf 对象上的字段名与 JSON 不同：JSON 用 message_type，SDK 用 msg_type
    # 长连接模式经过 _do_without_validation，字段可能保持 JSON 原始名称
    msg_type = (
        getattr(message, "msg_type", None)
        or getattr(message, "message_type", None)
        or ""
    )
    message_id = getattr(message, "message_id", "") or ""

    # content 可能在 message.content（JSON 直传）或 message.body.content（SDK 模型）
    content_raw = getattr(message, "content", None)
    if content_raw is None:
        body = getattr(message, "body", None)
        if body:
            content_raw = getattr(body, "content", None)
    if content_raw is None:
        content_raw = "{}"

    text = _parse_content(content_raw, msg_type)
    if not text:
        if msg_type != "text":
            logger.info("[LarkWS] ignoring non-text message: type=%s", msg_type)
        return

    logger.info(
        "[LarkWS] from=%s chat=%s msg=%s text=%s",
        user_open_id[:16] if user_open_id else "unknown",
        chat_id,
        message_id,
        text[:80],
    )

    # Route through agent bus（在独立线程中运行 async，避免与 SDK event loop 冲突）
    channel = _get_channel()
    channel._chat_id = chat_id
    channel._user_open_id = user_open_id

    def _run_async():
        # Save feishu_open_id to User model（在独立线程中执行，避免 async context 冲突）
        if user_open_id:
            channel._save_feishu_user(user_open_id, chat_id)
            from django.db import connections

            connections.close_all()
        # 本线程独立于 Django 请求线程，允许在 event loop 上下文中使用同步 ORM
        os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = "true"
        loop = asyncio.new_event_loop()
        try:
            from apps.agent.bus import (
                publish,
                build_agent_task,
                wait_reply,
                AGENT_TASKS,
            )

            msg = build_agent_task(
                user_id=user_open_id or "unknown",
                payload={"text": text},
                origin="lark",
            )

            loop.run_until_complete(publish(AGENT_TASKS, msg))
            reply = loop.run_until_complete(wait_reply(msg["task_id"], timeout=3600))

            if reply:
                processed = channel._extract_content(reply)
                loop.run_until_complete(channel.send_message(processed))
            else:
                loop.run_until_complete(
                    channel.send_message("[超时] Agent 处理超时，请稍后重试")
                )

            logger.info("[LarkWS] task %s processed", msg["task_id"])
        except Exception as e:
            logger.error("[LarkWS] handle_message error: %s", e, exc_info=True)
            try:
                loop.run_until_complete(
                    channel.send_message("[错误] 处理消息失败，请稍后重试")
                )
            except Exception:
                pass
        finally:
            loop.close()
            from django import db as django_db

            django_db.connections.close_all()

    t = threading.Thread(target=_run_async, daemon=True, name="lark-msg-handler")
    t.start()


def build_event_handler() -> lark.EventDispatcherHandler:
    """构建事件分发器，注册消息接收回调。"""
    return (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(handle_message)
        .build()
    )


def start_ws_client(
    app_id: str,
    app_secret: str,
    log_level: int = lark.LogLevel.INFO,
) -> None:
    """启动 WebSocket 长连接客户端（阻塞调用）。"""
    handler = build_event_handler()
    # 包装一层用于调试
    debug_handler = DebugEventHandler(handler)

    cli = lark.ws.Client(
        app_id=app_id,
        app_secret=app_secret,
        event_handler=debug_handler,
        log_level=log_level,
        auto_reconnect=True,
    )

    logger.info("[LarkWS] starting WebSocket client for app_id=%s", app_id[:12])

    stop_event = threading.Event()

    def _signal_handler(sig, frame):
        logger.info("[LarkWS] received signal %s, stopping...", sig)
        stop_event.set()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    ws_thread = threading.Thread(target=cli.start, daemon=True, name="lark-ws")
    ws_thread.start()

    logger.info("[LarkWS] WebSocket client started (thread: %s)", ws_thread.name)

    try:
        while not stop_event.wait(timeout=1):
            pass
    except KeyboardInterrupt:
        pass

    logger.info("[LarkWS] shutting down...")


def main():
    """入口：从环境变量或 Django settings 读取配置，启动 WS 客户端。"""
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings.dev")

    import django

    django.setup()

    from django.conf import settings

    app_id = os.environ.get("LARK_APP_ID", getattr(settings, "LARK_APP_ID", ""))
    app_secret = os.environ.get(
        "LARK_APP_SECRET", getattr(settings, "LARK_APP_SECRET", "")
    )

    if not app_id or not app_secret:
        logger.error("[LarkWS] LARK_APP_ID / LARK_APP_SECRET not configured")
        sys.exit(1)

    log_level = lark.LogLevel.DEBUG if os.environ.get("DEBUG") else lark.LogLevel.INFO

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    start_ws_client(app_id, app_secret, log_level=log_level)


if __name__ == "__main__":
    main()
