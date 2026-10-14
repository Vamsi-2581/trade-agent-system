"""Tests for channel module."""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock


class TestChunkMessage(unittest.TestCase):
    """测试 chunk_message 分片函数"""

    def test_short_text_returns_single_chunk(self):
        from apps.channel.base import chunk_message

        result = chunk_message("hello world", max_length=100)
        self.assertEqual(result, ["hello world"])

    def test_exact_length_returns_single_chunk(self):
        from apps.channel.base import chunk_message

        text = "a" * 100
        result = chunk_message(text, max_length=100)
        self.assertEqual(result, [text])

    def test_splits_by_newline(self):
        from apps.channel.base import chunk_message

        part1 = "a" * 50
        part2 = "b" * 50
        text = f"{part1}\n{part2}"
        result = chunk_message(text, max_length=60)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0], part1)
        self.assertEqual(result[1], part2)

    def test_splits_by_chinese_period(self):
        from apps.channel.base import chunk_message

        text = "这是第一句话。" + "这" * 50 + "。" + "这" * 50
        result = chunk_message(text, max_length=60)
        self.assertTrue(len(result) >= 2)
        # 所有分片拼接回去应等于原文
        self.assertEqual("".join(result), text)

    def test_splits_by_english_period(self):
        from apps.channel.base import chunk_message

        text = "First sentence. " + "a" * 50 + ". " + "b" * 50
        result = chunk_message(text, max_length=60)
        self.assertTrue(len(result) >= 2)
        self.assertEqual("".join(result), text)

    def test_hard_cut_when_no_boundary(self):
        from apps.channel.base import chunk_message

        text = "a" * 150
        result = chunk_message(text, max_length=50)
        self.assertEqual(len(result), 3)
        self.assertEqual("".join(result), text)
        for chunk in result:
            self.assertLessEqual(len(chunk), 50)

    def test_empty_text(self):
        from apps.channel.base import chunk_message

        result = chunk_message("", max_length=100)
        self.assertEqual(result, [""])

    def test_paragraph_split_preferred(self):
        from apps.channel.base import chunk_message

        para1 = "a" * 50
        para2 = "b" * 50
        text = f"{para1}\n\n{para2}"
        result = chunk_message(text, max_length=60)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0], para1)
        self.assertEqual(result[1], para2)

    def test_long_result_preserved_in_chunks(self):
        """模拟 Agent 长回复：中文 markdown 报告，确保所有文字保留"""
        from apps.channel.base import chunk_message

        text = "# 分析报告\n\n" + "这是一段分析内容。" * 300
        result = chunk_message(text, max_length=1000)
        self.assertTrue(len(result) >= 2)
        # 文字内容完整（分片边界处换行可能丢失，但字符不应丢失）
        joined = "".join(result)
        # 去除空白后比较，因为空行分隔符在分片边界会被消费
        self.assertEqual(joined.replace("\n", ""), text.replace("\n", ""))
        for chunk in result:
            self.assertLessEqual(len(chunk), 1000)


class TestTelegramChannel(unittest.TestCase):
    """测试 Telegram Channel"""

    def setUp(self):
        from apps.channel.telegram import TelegramChannel

        mock_supervisor = MagicMock()
        self.channel = TelegramChannel(
            token="test_token", supervisor_agent=mock_supervisor
        )

    def test_channel_name(self):
        self.assertEqual(self.channel.name, "telegram")

    def test_send_message_without_chat_id(self):
        """没有 chat_id 时不应发送消息"""
        self.channel._chat_id = None
        self.channel._app = MagicMock()
        result = asyncio.run(self.channel.send_message("test"))
        self.assertIsNone(result)

    def test_send_message_with_chat_id(self):
        """有 chat_id 时应发送消息"""
        self.channel._chat_id = 12345
        mock_bot = AsyncMock()
        mock_bot.send_message = AsyncMock()
        mock_app = MagicMock()
        mock_app.bot = mock_bot
        self.channel._app = mock_app

        asyncio.run(self.channel.send_message("Hello"))
        mock_bot.send_message.assert_called_once_with(chat_id=12345, text="Hello")

    def test_send_message_handles_error(self):
        """发送消息失败时应静默处理"""
        self.channel._chat_id = 12345
        mock_bot = AsyncMock()
        mock_bot.send_message = AsyncMock(side_effect=Exception("Network error"))
        mock_app = MagicMock()
        mock_app.bot = mock_bot
        self.channel._app = mock_app

        # 不应抛出异常
        asyncio.run(self.channel.send_message("test"))

    def test_send_photo_without_chat_id(self):
        """没有 chat_id 时不应发送照片"""
        self.channel._chat_id = None
        result = asyncio.run(self.channel.send_photo(b"image_data", "caption"))
        self.assertIsNone(result)

    def test_cmd_start_handler(self):
        """测试 /start 命令处理"""
        mock_update = MagicMock()
        mock_update.effective_chat.id = 99999
        mock_reply = AsyncMock()
        mock_update.message.reply_text = mock_reply

        asyncio.run(self.channel._cmd_start(mock_update, MagicMock()))
        self.assertEqual(self.channel._chat_id, 99999)
        mock_reply.assert_called_once()

    def test_cmd_help_handler(self):
        """测试 /help 命令处理"""
        mock_update = MagicMock()
        mock_reply = AsyncMock()
        mock_update.message.reply_text = mock_reply

        asyncio.run(self.channel._cmd_help(mock_update, MagicMock()))
        mock_reply.assert_called_once()


class TestBaseChannel(unittest.TestCase):
    """测试基础 Channel 类"""

    def test_base_channel_is_abstract(self):
        """BaseChannel 是抽象类，不能直接实例化"""
        from apps.channel.base import BaseChannel

        # 尝试实例化抽象类应抛出 TypeError
        with self.assertRaises(TypeError):
            BaseChannel()


class TestLarkChannel(unittest.TestCase):
    """测试飞书 Lark Channel"""

    def setUp(self):
        from apps.channel.lark import LarkChannel

        self.channel = LarkChannel(
            app_id="test_app_id",
            app_secret="test_app_secret",
            verification_token="test_verify_token",
            encrypt_key="",
            supervisor_agent=MagicMock(),
        )

    def test_channel_name(self):
        self.assertEqual(self.channel.name, "lark")

    def test_send_message_without_chat_id(self):
        """没有 chat_id 时不应发送消息"""
        self.channel._chat_id = ""
        result = asyncio.run(self.channel.send_message("test"))
        self.assertIsNone(result)

    def test_send_photo_without_chat_id(self):
        """没有 chat_id 时不应发送照片"""
        self.channel._chat_id = ""
        result = asyncio.run(self.channel.send_photo(b"image_data", "caption"))
        self.assertIsNone(result)

    def test_extract_content_from_dict(self):
        """从字典响应提取 content"""
        reply = '{"content": "hello world"}'
        self.assertEqual(self.channel._extract_content(reply), "hello world")

    def test_extract_content_from_dict_obj(self):
        """从 dict 对象提取 content"""
        reply = {"content": "hello", "extra": "data"}
        self.assertEqual(self.channel._extract_content(reply), "hello")

    def test_extract_content_from_plain_string(self):
        """纯字符串直接返回"""
        self.assertEqual(self.channel._extract_content("plain text"), "plain text")

    def test_url_verification_challenge(self):
        """飞书 URL 验证挑战应返回 challenge"""
        import json
        from django.test import RequestFactory

        factory = RequestFactory()
        body = '{"type": "url_verification", "token": "test_verify_token", "challenge": "abc123"}'
        request = factory.post(
            "/api/channel/webhook/lark/",
            data=body,
            content_type="application/json",
        )
        response = self.channel.handle_webhook(request)
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data["challenge"], "abc123")

    def test_url_verification_wrong_token(self):
        """错误的 verification token 应返回 403"""
        from django.test import RequestFactory

        factory = RequestFactory()
        body = '{"type": "url_verification", "token": "wrong_token", "challenge": "abc123"}'
        request = factory.post(
            "/api/channel/webhook/lark/",
            data=body,
            content_type="application/json",
        )
        response = self.channel.handle_webhook(request)
        self.assertEqual(response.status_code, 403)

    def test_invalid_json_body(self):
        """无效 JSON 应返回 400"""
        from django.test import RequestFactory

        factory = RequestFactory()
        request = factory.post(
            "/api/channel/webhook/lark/",
            data="not json",
            content_type="application/json",
        )
        response = self.channel.handle_webhook(request)
        self.assertEqual(response.status_code, 400)

    def test_non_text_message_ignored(self):
        """非文本消息类型应被忽略（不崩溃）"""
        import json
        from django.test import RequestFactory

        factory = RequestFactory()
        body = json.dumps(
            {
                "header": {"event_type": "im.message.receive_v1"},
                "event": {
                    "message": {
                        "chat_id": "test_chat",
                        "message_type": "image",
                        "content": '{"image_key": "img_123"}',
                    },
                    "sender": {"sender_id": {"open_id": "user_123"}},
                },
            }
        )
        request = factory.post(
            "/api/channel/webhook/lark/",
            data=body,
            content_type="application/json",
        )
        # 不应抛出异常
        response = self.channel.handle_webhook(request)
        self.assertEqual(response.status_code, 200)
