"""测试 Agent 回复/通知的多通道扇出（apps.agent.reply_fanout）。

覆盖场景：
- resolve_django_user_id：UUID 直通 / 渠道 id 解析 / 未知回落
- push_web：web WS group + ws_pending 兜底（open_id 会话也要同步 web）
- fan_out_reply：按 origin 去重 —— web 来源只补主通道（飞书），
  飞书来源只补 web，origin 即主通道时不重复推飞书

注：本文件全部为 DB-free 用例（resolve 的渠道 id 查找路径通过 mock 验证），
不与当前仓库未提交迁移的 schema 冲突。
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apps.agent.reply_fanout import (
    fan_out_reply,
    push_main_channel,
    push_web,
    resolve_django_user_id,
)

WEB_UUID = "5f1d2c0a-b5a5-4c34-8d98-1e1f3a2b4c5d"


def _run(coro):
    """便捷 helper: 同步跑 async coro"""
    return asyncio.run(coro)


@pytest.fixture
def mock_layer():
    """Mock channels.layers.get_channel_layer().group_send"""
    with patch("channels.layers.get_channel_layer") as mock_get_layer:
        layer = MagicMock()
        layer.group_send = AsyncMock()
        mock_get_layer.return_value = layer
        yield layer


@pytest.fixture
def mock_ws_pending():
    """Mock apps.agent.ws_pending.store"""
    with patch("apps.agent.ws_pending.store", new=AsyncMock()) as mock_store:
        yield mock_store


@pytest.fixture
def mock_lark_channel():
    """Mock apps.channel.lark_ws._get_channel 的单例工厂"""
    with patch("apps.channel.lark_ws._get_channel") as mock_get:
        channel = MagicMock()
        channel.send_message_to_user = AsyncMock()
        mock_get.return_value = channel
        yield channel


class TestResolveDjangoUserId:
    def test_uuid_passthrough_without_db(self):
        """合法 UUID 直接原样返回，不触发 DB 查询"""
        assert resolve_django_user_id(WEB_UUID) == WEB_UUID

    def test_unknown_returns_raw_on_lookup_failure(self, monkeypatch):
        """渠道 id 查找异常时回退到原始 id，绝不抛错（退回旧行为）"""

        def boom():
            raise RuntimeError("db access denied")

        monkeypatch.setattr("django.contrib.auth.get_user_model", boom)
        assert resolve_django_user_id("ou_unknown_open_id") == "ou_unknown_open_id"

    def test_none_and_unknown_guard(self):
        assert resolve_django_user_id("") == ""
        assert resolve_django_user_id("unknown") == "unknown"

    def test_channel_id_resolves_to_django_uuid(self, monkeypatch):
        """渠道 id（如飞书 open_id）命中用户 → 返回 Django User UUID"""

        class FakeUser:
            id = WEB_UUID

        class FakeQS:
            def first(self):
                return FakeUser()

        class FakeManager:
            @classmethod
            def filter(cls, **kwargs):
                return FakeQS()

        def fake_get_user_model():
            return type("User", (), {"objects": FakeManager()})

        monkeypatch.setattr("django.contrib.auth.get_user_model", fake_get_user_model)
        assert resolve_django_user_id("ou_resolve_me") == WEB_UUID


class TestPushWeb:
    def test_pushes_to_resolved_user_group_and_pending(
        self, mock_layer, mock_ws_pending, monkeypatch
    ):
        """web 推送：group_send user_{uuid}（解析后）+ ws_pending 兜底"""
        monkeypatch.setattr(
            "apps.agent.reply_fanout.resolve_django_user_id",
            lambda uid: WEB_UUID,
        )
        text = "✅ 任务 #xyz 完成"

        _run(push_web("ou_lark_session", text))

        # Only the resolved user's group receives the cached envelope.
        mock_layer.group_send.assert_called_once_with(
            f"user_{WEB_UUID}",
            {"type": "web_delivery", "message": mock_ws_pending.call_args.args[1]},
        )

        mock_ws_pending.assert_called_once()
        payload = mock_ws_pending.call_args.args[1]
        assert payload["type"] == "task_notification"
        assert payload["data"] == text

    def test_uuid_user_single_group_send(self, mock_layer, mock_ws_pending):
        """user_id 本身是 UUID → resolved==raw，集合去重后只发一次"""
        _run(push_web(WEB_UUID, "msg"))
        mock_layer.group_send.assert_called_once()
        assert mock_layer.group_send.call_args.args[0] == f"user_{WEB_UUID}"
        mock_ws_pending.assert_called_once()

    def test_group_send_failure_still_buffers(self, mock_ws_pending, monkeypatch):
        """group_send 抛错不影响 ws_pending 兜底"""
        monkeypatch.setattr(
            "apps.agent.reply_fanout.resolve_django_user_id",
            lambda uid: WEB_UUID,
        )
        with patch("channels.layers.get_channel_layer") as mock_get_layer:
            layer = MagicMock()
            layer.group_send = AsyncMock(side_effect=RuntimeError("redis down"))
            mock_get_layer.return_value = layer
            _run(push_web("some-user-id", "msg"))
        mock_ws_pending.assert_called_once()

    def test_channel_layer_none_still_buffers(self, mock_ws_pending):
        with patch("channels.layers.get_channel_layer", return_value=None):
            _run(push_web("some-user-id", "msg"))
        mock_ws_pending.assert_called_once()


class TestFanOutReply:
    def test_web_origin_pushes_main_channel_only(
        self, mock_layer, mock_ws_pending, mock_lark_channel, monkeypatch
    ):
        """web 来源：web 已由 chat_response 送达 → 只补主通道副本（飞书）"""

        def fake_active(u):
            from apps.channel.channel_resolver import ChannelTarget

            return ChannelTarget(
                channel_type="lark", chat_id="", user_open_id="ou_main_1"
            )

        monkeypatch.setattr(
            "apps.channel.channel_resolver.get_user_active_channel", fake_active
        )

        _run(fan_out_reply(WEB_UUID, "已安排 5 分钟后自动查询结果", origin="web"))

        # 不推 web（web 已原路收到 chat_response）
        mock_layer.group_send.assert_not_called()
        mock_ws_pending.assert_not_called()
        # 推送飞书（下达命令的 gateway + 飞书同时收到）
        mock_lark_channel.send_message_to_user.assert_called_once_with(
            "ou_main_1", "已安排 5 分钟后自动查询结果"
        )

    def test_lark_origin_pushes_web_only(
        self, mock_layer, mock_ws_pending, mock_lark_channel, monkeypatch
    ):
        """飞书来源（MAIN_CHANNEL=lark）：主通道副本跳过（bus reply 已原路送达），只补 web"""
        monkeypatch.setattr(
            "apps.agent.reply_fanout.resolve_django_user_id",
            lambda uid: WEB_UUID,
        )
        monkeypatch.setattr(
            "apps.channel.channel_resolver.get_user_active_channel",
            lambda u: None,
        )

        _run(
            fan_out_reply(
                "ou_lark_session", "已安排 5 分钟后自动查询结果", origin="lark"
            )
        )

        # web 推送执行（open_id → UUID 解析后进 group + 原始 id 兜底）
        mock_layer.group_send.assert_any_call(
            f"user_{WEB_UUID}",
            {"type": "web_delivery", "message": mock_ws_pending.call_args.args[1]},
        )
        mock_ws_pending.assert_called_once()
        # 主通道不重复推（origin==main_channel 去重）
        mock_lark_channel.send_message_to_user.assert_not_called()

    def test_unknown_origin_fans_out_everywhere(
        self, mock_layer, mock_ws_pending, mock_lark_channel, monkeypatch
    ):
        """无 origin 信息：web + 主通道都推（保守全量扇出）"""
        monkeypatch.setattr(
            "apps.agent.reply_fanout.resolve_django_user_id",
            lambda uid: WEB_UUID,
        )

        def fake_active(u):
            from apps.channel.channel_resolver import ChannelTarget

            return ChannelTarget(
                channel_type="lark", chat_id="", user_open_id="ou_main_2"
            )

        monkeypatch.setattr(
            "apps.channel.channel_resolver.get_user_active_channel", fake_active
        )

        _run(fan_out_reply("some-user-id", "通知内容", origin=""))

        mock_layer.group_send.assert_any_call(
            f"user_{WEB_UUID}",
            {"type": "web_delivery", "message": mock_ws_pending.call_args.args[1]},
        )
        mock_ws_pending.assert_called_once()
        mock_lark_channel.send_message_to_user.assert_called_once_with(
            "ou_main_2", "通知内容"
        )

    def test_garbage_input_ignored(self, mock_layer, mock_ws_pending):
        _run(fan_out_reply("", "", origin="web"))
        _run(fan_out_reply("unknown", "  ", origin="web"))
        mock_layer.group_send.assert_not_called()
        mock_ws_pending.assert_not_called()

    def test_api_origin_also_notifies_web(
        self, mock_layer, mock_ws_pending, mock_lark_channel, monkeypatch
    ):
        """REST chat（origin=api）：HTTP 响应已送达调用方 → 只补主通道"""
        monkeypatch.setattr(
            "apps.channel.channel_resolver.get_user_active_channel",
            lambda u: None,
        )
        _run(fan_out_reply(WEB_UUID, "结果", origin="api"))
        mock_layer.group_send.assert_called_once()
        mock_ws_pending.assert_called_once()
        mock_lark_channel.send_message_to_user.assert_not_called()


class TestPushMainChannel:
    def test_lark_target_sends_to_user(self, mock_lark_channel, monkeypatch):
        """Main channel = lark → send_message_to_user(open_id)"""
        from apps.channel.channel_resolver import ChannelTarget

        def fake_active(u):
            return ChannelTarget(
                channel_type="lark", chat_id="", user_open_id="ou_push_main"
            )

        monkeypatch.setattr(
            "apps.channel.channel_resolver.get_user_active_channel", fake_active
        )

        _run(push_main_channel("ou_push_main", "回测结果"))

        mock_lark_channel.send_message_to_user.assert_called_once_with(
            "ou_push_main", "回测结果"
        )

    def test_no_target_is_noop(self, mock_lark_channel, monkeypatch):
        """用户未配置主通道 → 不发送"""
        monkeypatch.setattr(
            "apps.channel.channel_resolver.get_user_active_channel",
            lambda u: None,
        )
        _run(push_main_channel("no-channel-user", "msg"))
        mock_lark_channel.send_message_to_user.assert_not_called()

    def test_telegram_target_skips_when_channel_unavailable(
        self, monkeypatch, mock_ws_pending
    ):
        """Telegram 主通道但进程内 channel 未启动 → 安全跳过"""
        from apps.channel.channel_resolver import ChannelTarget

        def fake_active(u):
            return ChannelTarget(channel_type="telegram", chat_id="12345")

        monkeypatch.setattr(
            "apps.channel.channel_resolver.get_user_active_channel", fake_active
        )
        # 延迟 import 的 core.asgi.get_telegram_channel 返回 None（未启动）
        with patch("core.asgi.get_telegram_channel", return_value=None):
            _run(push_main_channel("tg-user", "msg"))  # 不应抛错
