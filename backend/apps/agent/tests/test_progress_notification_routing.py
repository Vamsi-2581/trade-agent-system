"""
回归测试：验证 _route_notification 把 Celery 任务完成通知推到 web ws 端

Bug 背景：
  Celery / Stream 任务完成后，tracker.complete() → _send_notification_redis 推到
  Redis pubsub 频道 task:progress:notifications。core/asgi.py 里的 ASGI listener
  订阅该频道，调用 _route_notification(user_id, text) 把消息分发给用户。

  修复前 _route_notification 只处理 telegram/lark 两种 channel_type；无任何
  channel_layer.group_send 调用，所以 web (ChatWS) 用户收不到任何任务完成通知。

  修复后：_route_notification 在分发 telegram/lark 之前先调
  channel_layer.group_send("user_{user_id}", {"type": "task_notification", ...})，
  并在用户离线时通过 ws_pending.store 兜底。
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def mock_group_send():
    """Mock channels.layers.get_channel_layer().group_send"""
    with patch("channels.layers.get_channel_layer") as mock_get_layer:
        layer = MagicMock()
        layer.group_send = AsyncMock()
        mock_get_layer.return_value = layer
        yield layer


@pytest.fixture
def mock_ws_pending():
    """Mock apps.agent.ws_pending.store / drain"""
    with patch("apps.agent.ws_pending.store", new=AsyncMock()) as mock_store:
        yield mock_store


@pytest.fixture
def mock_channel_resolver_none():
    """get_user_active_channel 返回 None — 模拟纯 web 用户（无 telegram/lark 配置）"""
    with patch(
        "apps.channel.channel_resolver.get_user_active_channel", return_value=None
    ) as mock:
        yield mock


@pytest.fixture
def mock_channel_resolver_telegram():
    """get_user_active_channel 返回 telegram 目标"""
    target = MagicMock()
    target.channel_type = "telegram"
    target.chat_id = "12345"
    with patch(
        "apps.channel.channel_resolver.get_user_active_channel", return_value=target
    ) as mock:
        yield mock, target


def _run(coro):
    """便捷 helper: 同步跑 async coro"""
    return asyncio.run(coro)


class TestProgressNotificationRouting:
    """测试 _route_notification 把通知推到 web"""

    def test_web_user_dispatches_to_channel_group(
        self, mock_group_send, mock_ws_pending, mock_channel_resolver_none
    ):
        """纯 web 用户（无 telegram/lark）→ 通知必须推到 user_{user_id} group"""
        from core.asgi import _route_notification

        user_id = "test-user-uuid-1234"
        text = "✅ 任务 #abc 完成"

        _run(_route_notification(user_id, text))

        # 1. group_send 必须被调用，group 名为 user_{user_id}
        mock_group_send.group_send.assert_called_once()
        call_args = mock_group_send.group_send.call_args
        assert call_args.args[0] == f"user_{user_id}"
        # 2. event type 为 task_notification，text 内容正确
        event = call_args.args[1]
        assert event["type"] == "web_delivery"
        assert event["message"]["type"] == "task_notification"
        assert event["message"]["data"] == text
        assert event["message"]["delivery_id"]

    def test_web_user_buffers_to_ws_pending(
        self, mock_group_send, mock_ws_pending, mock_channel_resolver_none
    ):
        """无论 ws 是否在线，ws_pending.store 都要调用 — 离线时也能 drain"""
        from core.asgi import _route_notification

        user_id = "test-user-uuid-5678"
        text = "✅ 任务 #xyz 完成"

        _run(_route_notification(user_id, text))

        # ws_pending.store 必须被调用，payload 是前端能识别的格式
        mock_ws_pending.assert_called_once()
        call_args = mock_ws_pending.call_args
        assert call_args.args[0] == user_id
        payload = call_args.args[1]
        assert payload["type"] == "task_notification"
        assert payload["data"] == text

    def test_telegram_user_also_dispatches_to_web(
        self, mock_group_send, mock_ws_pending, mock_channel_resolver_telegram
    ):
        """有 telegram 配置的用户也要推 web — 多通道不互斥"""
        from core.asgi import _route_notification

        user_id = "test-user-uuid-tg"

        _run(_route_notification(user_id, "msg"))

        # 即使 channel_resolver 返回 telegram，web 分支也要跑
        mock_group_send.group_send.assert_called_once()
        assert mock_group_send.group_send.call_args.args[0] == f"user_{user_id}"
        mock_ws_pending.assert_called_once()

    def test_group_send_failure_does_not_break_routing(
        self, mock_ws_pending, mock_channel_resolver_none
    ):
        """channel_layer 抛错时，ws_pending 仍要执行（兜底）"""
        from core.asgi import _route_notification

        with patch("channels.layers.get_channel_layer") as mock_get_layer:
            layer = MagicMock()
            layer.group_send = AsyncMock(side_effect=RuntimeError("redis down"))
            mock_get_layer.return_value = layer

            # 不应抛异常
            _run(_route_notification("user-x", "msg"))

        # ws_pending 仍要兜底
        mock_ws_pending.assert_called_once()

    def test_channel_layer_none_does_not_break(
        self, mock_ws_pending, mock_channel_resolver_none
    ):
        """CHANNEL_LAYERS 未配置时（get_channel_layer 返回 None），ws_pending 仍要兜底"""
        from core.asgi import _route_notification

        with patch("channels.layers.get_channel_layer", return_value=None):
            _run(_route_notification("user-y", "msg"))

        mock_ws_pending.assert_called_once()
