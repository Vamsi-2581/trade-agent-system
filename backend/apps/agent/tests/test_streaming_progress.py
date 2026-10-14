"""Tests for on_tool_result callback in agent tool loop."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from apps.agent.base import AgentMessage, AgentResult, BaseAgent


class _SimpleAgent(BaseAgent):
    """Concrete agent for testing _run_tool_loop callback behavior."""

    name = "test_agent"
    _agent_tools = ["submit_backtest"]
    _max_tool_rounds = 2

    async def handle(self, message, on_tool_result=None):
        system = "You are a test agent."
        messages = [{"role": "user", "content": message.payload.get("text", "")}]
        tools = self._get_tools_schema()
        content, is_fb = await self._run_tool_loop(
            system,
            messages,
            tools,
            max_tokens=256,
            on_tool_result=on_tool_result,
        )
        if is_fb:
            return AgentResult(task_id=message.task_id, success=False, error="fb")
        return AgentResult(task_id=message.task_id, success=True, data=content)


@pytest.fixture
def agent():
    return _SimpleAgent()


@pytest.mark.asyncio
async def test_on_tool_result_called_on_tool_execution(agent):
    """回调在工具执行后被调用，传入 tool_name 和 result_text"""
    callback = AsyncMock()

    ToolCall = type(
        "ToolCall",
        (),
        {
            "call_id": "call_1",
            "name": "submit_backtest",
            "arguments": {
                "strategy_name": "macd",
                "symbol": "BTCUSDT",
                "timeframe": "1h",
            },
        },
    )
    Resp1 = type(
        "Resp",
        (),
        {
            "has_tool_calls": True,
            "content": "",
            "tool_calls": [ToolCall],
            "reasoning_content": "",
        },
    )
    Resp2 = type(
        "Resp",
        (),
        {"has_tool_calls": False, "content": "done", "reasoning_content": ""},
    )

    agent._execute_tool_call = AsyncMock(
        return_value='{"task_id": "abc123", "status": "PENDING"}'
    )

    with patch("apps.agent.llm_client.LLMClient.get_instance") as mock_get:
        mock_llm = MagicMock()
        mock_llm.chat_with_tools = AsyncMock(side_effect=[Resp1, Resp2])
        mock_get.return_value = mock_llm

        msg = AgentMessage(
            sender="user", recipient="test_agent", payload={"text": "run backtest"}
        )
        result = await agent.handle(msg, on_tool_result=callback)

    callback.assert_called_once()
    call_args = callback.call_args[0]
    assert call_args[0] == "submit_backtest"
    assert "abc123" in call_args[1]
    assert result.success


@pytest.mark.asyncio
async def test_on_tool_result_not_called_when_no_tools(agent):
    """无工具调用时回调不被调用"""
    callback = AsyncMock()

    Resp = type(
        "Resp",
        (),
        {"has_tool_calls": False, "content": "hello", "reasoning_content": ""},
    )

    with patch("apps.agent.llm_client.LLMClient.get_instance") as mock_get:
        mock_llm = MagicMock()
        mock_llm.chat_with_tools = AsyncMock(return_value=Resp)
        mock_get.return_value = mock_llm

        msg = AgentMessage(
            sender="user", recipient="test_agent", payload={"text": "hi"}
        )
        result = await agent.handle(msg, on_tool_result=callback)

    callback.assert_not_called()
    assert result.success


@pytest.mark.asyncio
async def test_without_callback_still_works(agent):
    """不传 on_tool_result 时仍能正常工作（向后兼容）"""
    Resp = type(
        "Resp",
        (),
        {"has_tool_calls": False, "content": "hello", "reasoning_content": ""},
    )

    with patch("apps.agent.llm_client.LLMClient.get_instance") as mock_get:
        mock_llm = MagicMock()
        mock_llm.chat_with_tools = AsyncMock(return_value=Resp)
        mock_get.return_value = mock_llm

        msg = AgentMessage(
            sender="user", recipient="test_agent", payload={"text": "hi"}
        )
        result = await agent.handle(msg)

    assert result.success
