"""回归测试：_run_tool_loop 同工具连续失败熔断。

Bug：web_search 持续失败（如代理 DNS 不可达）时，LLM 反复重试同一工具，
烧满 _max_tool_rounds=32 轮才结束，消耗完 loop 且无法给出结果。

修复期望：同一工具连续失败达到阈值后，后续对该工具的调用不再真实执行，
直接返回"已被禁用"的工具结果并明令 LLM 停止重试、基于已有信息作答，
使循环在远小于 _max_tool_rounds 的轮次内收敛。
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from apps.agent.base import AgentMessage, AgentResult, BaseAgent

MAX_ROUNDS = 32
FAIL_THRESHOLD = BaseAgent._TOOL_FAILURE_DISABLE_THRESHOLD


class _LoopAgent(BaseAgent):
    name = "test_loop_agent"
    _agent_tools = ["web_search"]
    _max_tool_rounds = MAX_ROUNDS

    async def handle(self, message, on_tool_result=None):
        system = "You are a test agent."
        messages = [{"role": "user", "content": message.payload.get("text", "")}]
        tools = self._get_tools_schema()
        content, is_fb = await self._run_tool_loop(system, messages, tools)
        return AgentResult(task_id=message.task_id, success=not is_fb, data=content)


def _tool_call_resp(i: int):
    tc = type(
        "ToolCall",
        (),
        {
            "call_id": f"call_{i}",
            "name": "web_search",
            "arguments": {"query": f"btc analysis try {i}"},
        },
    )
    return type(
        "Resp",
        (),
        {
            "has_tool_calls": True,
            "content": "",
            "tool_calls": [tc],
            "reasoning_content": "",
        },
    )


@pytest.mark.asyncio
async def test_failing_tool_circuit_breaks_before_max_rounds():
    """web_search 连续失败达到阈值后熔断：不再真实执行，LLM 收到禁用通知，
    循环在 max_rounds 之内收敛而不是烧满 32 轮。"""
    agent = _LoopAgent()
    calls = {"n": 0}

    async def fake_execute(tc):
        calls["n"] += 1
        return "Error: [Errno 8] nodename nor servname provided, or not known"

    agent._execute_tool_call = AsyncMock(side_effect=fake_execute)

    summary_holder = {}

    def llm_side_effect(system, messages, tools, max_tokens=8192, **kw):
        idx = llm_side_effect.round
        llm_side_effect.round += 1
        summary_holder["summary_user"] = ""
        return _tool_call_resp(idx)

    llm_side_effect.round = 0

    async def fake_chat(system=None, user="", max_tokens=8192, **kw):
        # summary 轮：捕获 prompt 以验证失败感知提示注入
        summary_holder["summary_user"] = user
        return "搜索不可用，以下为基于已有知识的分析…"

    with patch("apps.agent.llm_client.LLMClient.get_instance") as mock_get:
        mock_llm = MagicMock()
        mock_llm.chat_with_tools = AsyncMock(side_effect=llm_side_effect)
        mock_llm.chat = AsyncMock(side_effect=fake_chat)
        mock_get.return_value = mock_llm

        msg = AgentMessage(
            sender="user",
            recipient="test_loop_agent",
            payload={"text": "分析一下 BTC 走势"},
        )
        result = await agent.handle(msg)

    # 熔断：真实执行次数 = 失败阈值，远小于 32 轮
    assert calls["n"] == FAIL_THRESHOLD, (
        f"tool executed {calls['n']} times — circuit breaker not engaged"
    )
    # LLM 轮次远小于上限（每轮一次 chat_with_tools）
    assert llm_side_effect.round < MAX_ROUNDS
    assert result.success
    assert "基于已有知识" in result.data
    # summary prompt 注入了失败感知提示（工具循环被强制终止 + web_search 失败）
    assert "web_search 失败" in summary_holder["summary_user"]
