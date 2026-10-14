"""Agent 回复 / 通知的多通道扇出（fan-out）。

需求背景
--------
用户通过任意 gateway（Web / 飞书 / Telegram / TUI）下达命令（如提交回测），
Agent 的回复（例如「⏰ 已安排 5 分钟后自动查询结果」）需要**同时**到达：

1. 下达命令的 gateway —— 原路返回（Web chat_response / 飞书 bus reply，已有实现）
2. 主通知通道（``MAIN_CHANNEL``，默认飞书）—— 由 ``fan_out_reply`` 补齐
3. 反向也要成立：主通道（飞书）发起的会话，其回复/通知同样要推送到 Web
   （``open_id`` → Django User UUID 的映射，见 ``resolve_django_user_id``）

设计
----
- ``fan_out_reply(user_id, text, origin)`` 把文本投递到**除 origin 之外**的
  所有可达通道（web WS group + ``ws_pending`` 离线兜底；主通道 lark/telegram）。
- 当 ``origin`` 即主通道时跳过主通道副本（如飞书发起的会话，飞书已由
  bus reply 原路收到，避免重复推送）。
- 本模块的推送原语同时被 ``core/asgi.py`` 的 ``_route_notification`` 复用
  （任务进度/完成通知的本职扇出，与回复扇出共享同一条通道逻辑）。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def resolve_django_user_id(user_id: str) -> str:
    """将渠道侧 user_id 映射为 Django User UUID（用于 web WS group key）。

    查找顺序：Django UUID 主键 → telegram_id → feishu_open_id → username。
    未命中时原样返回（退回旧行为：按原始 id 推送，group 空则自然丢弃）。

    Note: 同步函数，async 环境下请用 ``database_sync_to_async`` 包裹调用。
    """
    if not user_id or user_id == "unknown":
        return user_id

    # 已是 Django UUID 主键：无需查库（web 场景的常见路径）
    try:
        import uuid as _uuid

        _uuid.UUID(user_id)
        return user_id
    except (ValueError, AttributeError):
        pass  # 不是 UUID，继续走渠道 ID 查找逻辑

    from django.contrib.auth import get_user_model

    try:
        User = get_user_model()
        user = (
            User.objects.filter(telegram_id=user_id).first()
            or User.objects.filter(feishu_open_id=user_id).first()
            or User.objects.filter(username=user_id).first()
        )
    except Exception:
        logger.debug(
            "[Fanout] user lookup failed for %s, using raw id", user_id, exc_info=True
        )
        return user_id

    if user:
        return str(user.id)

    logger.debug(
        "[Fanout] user %s not resolvable to Django User, using raw id", user_id
    )
    return user_id


async def push_web_payload(user_id: str, payload: dict) -> None:
    """用户级 Web 投递：先入缓存，再唤醒当前在线连接。

    修复「先 group_send 后 store」的竞态：唤醒与缓存颠倒后，重连的
    socket 可能错过刚产生的结果。现在顺序为 store → group_send，
    连接侧（web_delivery）发送成功后按 delivery_id 确认删除。
    离线保留仍受 ws_pending.TTL_SECONDS（1 小时）约束。

    Note: user_id 必须是 Django UUID（web 场景）；渠道 id 请先走
    ``push_web`` 的 resolve_django_user_id。
    """
    from uuid import uuid4

    from channels.layers import get_channel_layer

    from apps.agent import ws_pending

    if not user_id or user_id == "unknown":
        return

    envelope = dict(payload)
    envelope.setdefault("delivery_id", str(uuid4()))

    # 1) 先持久化：任何时刻重连都能补发（幂等，靠 delivery_id 去重）
    try:
        await ws_pending.store(user_id, envelope)
    except Exception:
        logger.warning(
            "[Fanout] ws_pending.store failed for %s", user_id, exc_info=True
        )

    # 2) 唤醒在线连接（含发送者自己的新连接）；无成员时空操作
    layer = get_channel_layer()
    if layer is not None:
        try:
            await layer.group_send(
                f"user_{user_id}",
                {"type": "web_delivery", "message": envelope},
            )
        except Exception:
            logger.warning("[Fanout] web wake failed for %s", user_id, exc_info=True)


async def push_web(user_id: str, text: str) -> None:
    """推送通知到 web WS group（``user_{uuid}``），离线时经 ws_pending 兜底。

    已知匹配项：
    - ChatConsumer（``apps/agent/consumers.py``）connect 时加入
      ``user_{self.user_id}``（Django UUID），并实现 ``task_notification`` 透传。
    - ``ws_pending.drain`` 在重连时按 Django UUID 取出待发消息。
    """
    from channels.db import database_sync_to_async

    uuid_id = await database_sync_to_async(resolve_django_user_id)(user_id)
    await push_web_payload(uuid_id, {"type": "task_notification", "data": text})


async def push_main_channel(user_id: str, text: str) -> None:
    """推送主通道（``MAIN_CHANNEL``：lark / telegram）。tui 为进程内通道，跳过。"""
    from channels.db import database_sync_to_async

    from apps.channel.channel_resolver import get_user_active_channel

    target = await database_sync_to_async(get_user_active_channel)(user_id)
    if target is None:
        return

    if target.channel_type == "telegram":
        # 进程内 TelegramChannel 实例由 core.asgi lifespan 启动；延迟 import 避免循环依赖
        from core.asgi import get_telegram_channel

        channel = get_telegram_channel()
        if channel is not None and getattr(channel, "_app", None):
            old_chat_id = channel._chat_id
            try:
                channel._chat_id = int(target.chat_id)
                await channel.send_message(text)
            finally:
                channel._chat_id = old_chat_id
        else:
            logger.debug(
                "[Fanout] Telegram not ready, dropping notification: %s", text[:50]
            )

    elif target.channel_type == "lark":
        try:
            from apps.channel.lark_ws import _get_channel

            channel = _get_channel()
            await channel.send_message_to_user(target.user_open_id, text)
        except Exception:
            logger.warning("[Fanout] failed to send Lark notification", exc_info=True)

    # channel_type == "tui": 进程内渲染，无需独立投递


async def fan_out_reply(user_id: str, text: str, origin: str = "") -> None:
    """把 Agent 回复扇出到「非 origin」通道。

    Args:
        user_id: 用户标识（Django UUID 或渠道 id，如飞书 open_id）
        text: 回复/通知文本
        origin: 下达命令的 gateway（web / api / lark / telegram / tui / ""）。
            "" 表示未知来源 —— 按完整扇出处理（web + 主通道都要）。

    规则：
    - origin 为 web/api 时，回复已通过 chat_response / HTTP 原路送达，只补主通道副本；
    - 其余 origin（lark/telegram/tui/空）补推 web（含 open_id → UUID 解析）；
    - origin 即主通道时跳过主通道副本（原路已送达，避免飞书收两份）。
    """
    if not user_id or user_id == "unknown" or not text or not text.strip():
        return

    from django.conf import settings

    main_channel = getattr(settings, "MAIN_CHANNEL", "lark")

    # 1) web 副本：非 web 来源的回复，web 端也要收到
    if origin != "web":
        await push_web(user_id, text)

    # 2) 主通道副本：origin 即主通道时已原路送达，跳过
    if origin != main_channel:
        await push_main_channel(user_id, text)
