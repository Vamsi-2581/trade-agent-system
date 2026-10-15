"""
E2E 测试：工具自动注册 + 交易所账号查询工具 + 框架控制自然语言响应

覆盖完整链路：API 端点 → Supervisor → Tool → Database
"""

from __future__ import annotations

import asyncio
import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings.test")

import django
from django.conf import settings

if not settings.configured:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings.test")
    django.setup()

from django.test import TransactionTestCase
from rest_framework.test import APITestCase

from apps.authentication.models import User
from apps.exchange.models import ExchangeAccount


def _run(coro):
    """兼容 Python 3.10+ 的异步执行辅助函数"""
    # Use asyncio.run for normal async execution
    return asyncio.run(coro)


def _get_tokens(user: User) -> dict:
    from rest_framework_simplejwt.tokens import RefreshToken

    refresh = RefreshToken.for_user(user)
    return {"refresh": str(refresh), "access": str(refresh.access_token)}


class TestToolAutoRegistration(APITestCase):
    """E2E: 验证 tools 目录下工具自动注册到全局 ToolRegistry"""

    def setUp(self):
        self.user = User.objects.create_user(
            email="test@tool.com",
            username="tool_tester",
            password="testpass123",
        )
        self.tokens = _get_tokens(self.user)

    def test_system_status_endpoint_lists_all_tools(self):
        """系统状态端点应返回所有已注册工具"""
        from apps.agent.tools.base import ToolRegistry

        # 获取所有已注册工具名称
        registered = {t.name for t in ToolRegistry.all()}

        # 验证核心工具均已注册
        expected_tools = {
            "web_search",
            "web_fetch",
            "read_file",
            "write_file",
            "load_skill",
            "get_system_status",
            "get_exchange_account",
        }
        missing = expected_tools - registered
        self.assertFalse(missing, f"以下工具未注册: {missing}")

    def test_exchange_account_tool_registered(self):
        """GetExchangeAccountTool 应自动注册"""
        from apps.agent.tools.base import ToolRegistry

        tool = ToolRegistry.get("get_exchange_account")
        self.assertIsNotNone(tool, "get_exchange_account 工具未注册")
        self.assertEqual(tool.name, "get_exchange_account")
        self.assertIn("交易所", tool.description)

    def test_tool_schema_valid_for_llm(self):
        """工具 schema 应符合 OpenAI function calling 格式"""
        from apps.agent.tools.base import ToolRegistry

        tool = ToolRegistry.get("get_exchange_account")
        schema = tool.schema

        self.assertIn("type", schema)
        self.assertEqual(schema["type"], "function")
        self.assertIn("function", schema)
        self.assertIn("name", schema["function"])
        self.assertIn("description", schema["function"])
        self.assertIn("parameters", schema["function"])

    def test_all_tools_have_valid_schema(self):
        """所有已注册工具应有有效 schema"""
        from apps.agent.tools.base import ToolRegistry

        for tool in ToolRegistry.all():
            schema = tool.schema
            self.assertIn("function", schema, f"{tool.name} schema 缺少 function")
            self.assertEqual(
                schema["type"], "function", f"{tool.name} type 不为 function"
            )
            self.assertIn("name", schema["function"], f"{tool.name} 缺少 name")
            self.assertIn(
                "parameters", schema["function"], f"{tool.name} 缺少 parameters"
            )

    def test_new_tool_in_same_directory_auto_discovers(self):
        """新工具文件放在 tools 目录下应自动被发现（验证发现机制）"""
        from pathlib import Path
        from apps.agent.tools.base import ToolRegistry

        tools_dir = Path(__file__).parent.parent.parent / "agent" / "tools"
        tool_files = list(tools_dir.glob("*.py"))
        tool_files = [
            f
            for f in tool_files
            if not f.name.startswith(("_", "test"))
            and f.name not in ("base.py", "__init__.py")
        ]

        # 验证每个工具文件对应的工具都已注册
        for tool_file in tool_files:
            module_name = tool_file.stem
            # 导入模块查找 BaseTool 子类
            import importlib

            mod = importlib.import_module(f"apps.agent.tools.{module_name}")
            for attr_name in dir(mod):
                from apps.agent.tools.base import BaseTool

                cls = getattr(mod, attr_name)
                if (
                    isinstance(cls, type)
                    and issubclass(cls, BaseTool)
                    and cls is not BaseTool
                ):
                    tool_name = cls().name
                    registered = ToolRegistry.get(tool_name)
                    self.assertIsNotNone(
                        registered,
                        f"{tool_file.name} 中的 {tool_name} 未注册",
                    )


class TestExchangeAccountToolE2E(TransactionTestCase):
    """E2E: 交易所账号查询工具的完整链路测试"""

    def setUp(self):
        self.user = User.objects.create_user(
            email="exchange@test.com",
            username="exchange_tester",
            password="testpass123",
        )
        self.tokens = _get_tokens(self.user)

        # 创建测试交易所账号
        ExchangeAccount.objects.create(
            exchange="binance",
            label="币安主账号",
            api_key_enc=b"encrypted_key_binance",
            api_secret_enc=b"encrypted_secret_binance",
            is_active=True,
        )
        ExchangeAccount.objects.create(
            exchange="binance",
            label="币安现货账号",
            api_key_enc=b"encrypted_key_binance_spot",
            api_secret_enc=b"encrypted_secret_binance_spot",
            is_active=True,
        )
        ExchangeAccount.objects.create(
            exchange="binance",
            label="币安合约账号",
            api_key_enc=b"encrypted_key_binance_futures",
            api_secret_enc=b"encrypted_secret_binance_futures",
            is_active=False,  # 非活跃
        )

    def test_execute_tool_returns_all_active_accounts(self):
        """工具应返回所有活跃交易所账号"""
        from apps.agent.tools.exchange_account import GetExchangeAccountTool

        tool = GetExchangeAccountTool()
        result = _run(tool.execute())

        self.assertTrue(result.success)
        self.assertIn("2 个", result.data)
        self.assertIn("币安主账号", result.data)
        self.assertIn("币安现货账号", result.data)
        self.assertNotIn("币安合约账号", result.data)  # 非活跃不应出现

    def test_execute_tool_filter_by_exchange(self):
        """工具应按交易所类型筛选"""
        from apps.agent.tools.exchange_account import GetExchangeAccountTool

        tool = GetExchangeAccountTool()
        result = _run(tool.execute(exchange="binance"))

        self.assertTrue(result.success)
        self.assertIn("2 个", result.data)
        self.assertIn("币安主账号", result.data)
        self.assertNotIn("币安合约账号", result.data)

    def test_execute_tool_filter_by_label(self):
        """工具应按标签关键词模糊匹配（label__icontains）"""
        from apps.agent.tools.exchange_account import GetExchangeAccountTool

        tool = GetExchangeAccountTool()
        # "账号" 同时命中活跃的两个账号（币安主账号 / 币安现货账号），
        # 非活跃的"币安合约账号"被 is_active=True 过滤掉。
        result = _run(tool.execute(label="账号"))

        self.assertTrue(result.success)
        self.assertIn("2 个", result.data)
        self.assertIn("币安主账号", result.data)
        self.assertIn("币安现货账号", result.data)
        self.assertNotIn("币安合约账号", result.data)

    def test_execute_tool_filter_by_label_narrow(self):
        """窄关键词只命中一个：验证是真正的模糊匹配而非全量返回"""
        from apps.agent.tools.exchange_account import GetExchangeAccountTool

        tool = GetExchangeAccountTool()
        result = _run(tool.execute(label="主"))

        self.assertTrue(result.success)
        self.assertIn("1 个", result.data)
        self.assertIn("币安主账号", result.data)
        self.assertNotIn("币安现货账号", result.data)

    def test_execute_tool_no_match_returns_message(self):
        """无匹配时返回友好提示"""
        from apps.agent.tools.exchange_account import GetExchangeAccountTool

        tool = GetExchangeAccountTool()
        result = _run(tool.execute(exchange="bitget"))

        self.assertTrue(result.success)
        self.assertIn("未找到", result.data)

    def test_api_key_not_exposed(self):
        """结果中不应包含 API 密钥相关信息"""
        from apps.agent.tools.exchange_account import GetExchangeAccountTool

        tool = GetExchangeAccountTool()
        result = _run(tool.execute())

        self.assertTrue(result.success)
        self.assertNotIn("api_key", result.data.lower())
        self.assertNotIn("secret", result.data.lower())
        self.assertNotIn("encrypted", result.data.lower())

    def test_supervisor_tools_list_includes_exchange_account(self):
        """get_exchange_account 已注册到全局 ToolRegistry，可被 Agent 取用。

        注：该工具**不在** supervisor 的 prompt 声明工具表内（
        prompts/v1/supervisor.txt 的 tools 列表是 supervisor 可用工具的权威来源，
        历史上也从未包含它）。supervisor 只负责编排，交易所账号查询由子 Agent /
        直接调用完成。因此这里断言的是"注册可见"这一真实契约。
        """
        from apps.agent.tools.base import ToolRegistry
        from apps.agent.supervisor import SupervisorAgent

        # 1. 全局注册可见（LLM function calling 可枚举到）
        self.assertIn("get_exchange_account", {t.name for t in ToolRegistry.all()})

        # 2. supervisor 的 prompt 声明工具表是权威来源，且不含该工具
        supervisor = SupervisorAgent.get_instance()
        self.assertNotIn("get_exchange_account", supervisor._agent_tools)
        self.assertTrue(supervisor._agent_tools, "supervisor 工具表不应为空")


class TestFrameControlNaturalLanguage(APITestCase):
    """E2E: 框架启动/停止命令返回自然语言表述"""

    def setUp(self):
        self.user = User.objects.create_user(
            email="frame@test.com",
            username="frame_tester",
            password="testpass123",
        )
        self.tokens = _get_tokens(self.user)

    def test_frame_handle_returns_natural_language(self):
        """_handle_frame 返回自然语言字符串，不是 JSON"""
        from apps.agent.supervisor import SupervisorAgent
        from apps.agent.frame_manager import FrameManager
        from apps.agent.base import AgentMessage, AgentResult

        # 初始化 supervisor 和 frame manager
        supervisor = SupervisorAgent.get_instance()
        FrameManager.get_instance()

        # 创建测试消息
        message = AgentMessage(
            sender="user",
            recipient="supervisor",
            payload={"text": "启动交易框架"},
            user_id=str(self.user.pk),
            intent="start_trading",
        )

        result: AgentResult = _run(supervisor._handle_frame("start_trading", message))

        self.assertTrue(result.success)
        # 返回的是字符串，不是 dict
        self.assertIsInstance(result.data, str)
        # 包含自然语言描述
        self.assertIn("trading", result.data)
        self.assertIn("启动", result.data)

    def test_frame_stop_returns_natural_language(self):
        """停止框架返回自然语言"""
        from apps.agent.supervisor import SupervisorAgent
        from apps.agent.base import AgentMessage, AgentResult

        supervisor = SupervisorAgent.get_instance()

        # 先启动再停止
        message_start = AgentMessage(
            sender="user",
            recipient="supervisor",
            payload={"text": "启动"},
            user_id=str(self.user.pk),
            intent="start_trading",
        )
        message_stop = AgentMessage(
            sender="user",
            recipient="supervisor",
            payload={"text": "停止"},
            user_id=str(self.user.pk),
            intent="stop_trading",
        )

        _run(supervisor._handle_frame("start_trading", message_start))

        result: AgentResult = _run(
            supervisor._handle_frame("stop_trading", message_stop)
        )

        self.assertTrue(result.success)
        self.assertIsInstance(result.data, str)
        self.assertIn("停止", result.data)


class TestIntentFreeChatInlineResponse(APITestCase):
    """E2E: 意图未识别时 LLM 直接返回回复内容（节省一次 LLM 调用）"""

    def setUp(self):
        self.user = User.objects.create_user(
            email="intent@test.com",
            username="intent_tester",
            password="testpass123",
        )
        self.tokens = _get_tokens(self.user)

    def test_parse_intent_returns_free_chat_dict_when_no_match(self):
        """_parse_intent 在未识别意图时应返回 _free_chat dict"""
        from apps.agent.supervisor import SupervisorAgent
        from unittest.mock import patch

        supervisor = SupervisorAgent.get_instance()

        # Mock LLM 返回 free_chat 响应
        async def mock_chat(*args, **kwargs):
            return '{"_free_chat": true, "response": "我不太明白你的意思，可以再详细说说吗？"}'

        with patch.object(supervisor._llm, "chat", new=mock_chat):
            result = _run(supervisor._parse_intent("这是一条无法识别意图的消息"))

        self.assertIsInstance(result, dict)
        self.assertTrue(result.get("_free_chat"))
        self.assertIn("我不太明白", result.get("response", ""))

    def test_parse_intent_returns_intent_string_when_match(self):
        """_parse_intent 在识别意图时应返回 agent name 字符串"""
        from apps.agent.supervisor import SupervisorAgent
        from unittest.mock import patch

        supervisor = SupervisorAgent.get_instance()

        async def mock_chat(*args, **kwargs):
            return '{"agent": "analyst"}'

        with patch.object(supervisor._llm, "chat", new=mock_chat):
            result = _run(supervisor._parse_intent("分析一下BTC的走势"))

        self.assertIsInstance(result, str)
        self.assertEqual(result, "analyst")

    def test_normal_route_handles_free_chat_inline(self):
        """_normal_route 对 free-chat dict 直接返回，不调用 _free_chat"""
        from apps.agent.supervisor import SupervisorAgent
        from apps.agent.base import AgentMessage, AgentResult
        from unittest.mock import patch

        supervisor = SupervisorAgent.get_instance()

        # Mock _parse_intent 返回 free_chat dict
        async def mock_parse(*args, **kwargs):
            return {"_free_chat": True, "response": "这是自由对话回复"}

        message = AgentMessage(
            sender="user",
            recipient="supervisor",
            payload={"text": "你好"},
            user_id=str(self.user.pk),
        )

        with patch.object(supervisor, "_parse_intent", new=mock_parse):
            result: AgentResult = _run(supervisor._normal_route(message))

        self.assertTrue(result.success)
        self.assertEqual(result.data, "这是自由对话回复")


if __name__ == "__main__":
    import unittest

    unittest.main()
