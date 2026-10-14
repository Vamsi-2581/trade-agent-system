"""回归测试：工具通道故障时禁止"静默降级"。

背景 Bug（2026-09-16，chat 窗口实测）：
- 用户下达「用参数网格搜索来确定 box_time_range_breakout_strategy 测试 SOL 近 4 年 4h 的最佳参数」；
- 当时 LLM 中继上游（netgpu / qwen3.8-27b-fp8）约 40-50% 的请求返回
  500 `upstream error: do request failed`；
- `llm_client.chat_with_tools` 重试耗尽后降级为 plain chat（**不带 tools**），
  模型回了一段 490 字的普通文本；
- `_run_tool_loop` 直接把这段文本当答复转发 → 用户看到"有回应"，但
  DB（WorkflowHistory / GridSearchJob / BacktestResult）与 Celery 队列里
  空空如也：指令从未执行，也没有任何地方告诉用户"没执行"。

修复（本文件覆盖）：
1. `LLMToolResponse.degraded / degradation_reason` 标记降级响应；
2. `_run_tool_loop` 在「降级 + 本轮零工具执行 + 内容里也没有 JSON tool call」时
   **拒绝转发**，改为明确回报「本次请求未执行」；
3. 若降级发生前已有工具真实执行，则保留内容并追加"本轮未执行新操作"的说明；
4. 降级路径若仍解析出 JSON tool call，则照常执行（保留原有补救能力）；
5. 重试次数 2 → 4，并加抖动退避。
"""

from __future__ import annotations

import httpx
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from apps.agent.base import AgentMessage, BaseAgent
from apps.agent.llm_client import TOOL_CALL_MAX_RETRIES, LLMClient


class _ToolLoopAgent(BaseAgent):
    """最小工具循环 Agent（schema 自带，不依赖 ToolRegistry / DB）。"""

    name = "test_agent"
    _agent_tools = ["submit_backtest", "get_task_result"]
    _max_tool_rounds = 6

    def _get_tools_schema(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "submit_backtest",
                    "description": "提交回测或参数网格搜索",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "strategy_name": {"type": "string"},
                            "symbol": {"type": "string"},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_task_result",
                    "description": "查询回测/网格搜索结果",
                    "parameters": {
                        "type": "object",
                        "properties": {"task_id": {"type": "string"}},
                    },
                },
            },
        ]

    async def handle(self, message, on_tool_result=None):
        content, _is_fb = await self._run_tool_loop(
            "You are a test agent.",
            [{"role": "user", "content": message.payload.get("text", "")}],
            self._get_tools_schema(),
            max_tokens=256,
            on_tool_result=on_tool_result,
        )
        return content


def _resp(content="", tool_calls=None, degraded=False, reason=""):
    return type(
        "Resp",
        (),
        {
            "has_tool_calls": bool(tool_calls),
            "content": content,
            "tool_calls": tool_calls or [],
            "reasoning_content": "",
            "degraded": degraded,
            "degradation_reason": reason,
        },
    )()


def _tool(name="submit_backtest", **kwargs):
    return type(
        "ToolCall",
        (),
        {"call_id": f"call_{name}", "name": name, "arguments": kwargs},
    )()


def _patch_llm(*responses):
    mock_llm = MagicMock()
    mock_llm.chat_with_tools = AsyncMock(side_effect=list(responses))
    return patch("apps.agent.llm_client.LLMClient.get_instance", return_value=mock_llm)


def _msg(
    text="用参数网格搜索来确定 box_time_range_breakout_strategy 测试 SOL 近4年来4h的最佳参数",
):
    return AgentMessage(sender="user", recipient="test_agent", payload={"text": text})


@pytest.mark.asyncio
async def test_degraded_answer_without_tool_calls_is_refused():
    """核心回归：工具通道降级 + 零工具执行 → 必须回报"未执行"，不得转发降级文本。"""
    agent = _ToolLoopAgent()
    fake = "已为你提交网格搜索任务，任务ID 11111111-2222-3333-4444-555555555555，预计 5 分钟出结果。"

    with _patch_llm(
        _resp(fake, degraded=True, reason="HTTPStatusError: 500 do_request_failed")
    ):
        out = await agent.handle(_msg())

    assert "本次请求未执行" in out
    assert "11111111-2222-3333-4444-555555555555" not in out  # 降级文本不得外泄
    assert "500" in out  # 附带故障原因，便于用户/运维判断
    assert "重试" in out


@pytest.mark.asyncio
async def test_degraded_json_tool_call_is_still_executed():
    """降级补救：降级文本里若含 JSON tool call，仍要真正执行工具并采纳后续总结。"""
    agent = _ToolLoopAgent()
    agent._execute_tool_call = AsyncMock(return_value='{"task_id": "real-grid-1"}')
    degraded_json = (
        '{"tool": "submit_backtest", "args": {"strategy_name": '
        '"box_time_range_breakout_strategy", "symbol": "SOL/USDT"}}'
    )

    with _patch_llm(
        _resp(degraded_json, degraded=True, reason="HTTPStatusError: 500"),
        _resp("网格搜索已提交，任务ID real-grid-1"),
    ):
        out = await agent.handle(_msg())

    agent._execute_tool_call.assert_awaited()  # 工具真的被执行了
    assert "real-grid-1" in out


@pytest.mark.asyncio
async def test_degraded_after_executed_tool_keeps_content_with_notice():
    """降级前已有真实工具结果：保留答复，但必须注明本轮未执行新操作。"""
    agent = _ToolLoopAgent()
    agent._execute_tool_call = AsyncMock(return_value='{"task_id": "real-grid-2"}')

    with _patch_llm(
        _resp("", tool_calls=[_tool("submit_backtest", symbol="SOL/USDT")]),
        _resp(
            "SOL/USDT 近 4 年 4h 网格搜索共 120 组参数组合。",
            degraded=True,
            reason="HTTP 500",
        ),
    ):
        out = await agent.handle(_msg())

    assert out.startswith("SOL/USDT 近 4 年 4h 网格搜索共 120 组参数组合。")
    assert "本轮未执行新的操作" in out
    assert "500" in out


@pytest.mark.asyncio
async def test_normal_answer_still_relayed_unchanged():
    """无回归：正常（非降级）文本答复照旧原样转发。"""
    agent = _ToolLoopAgent()
    plain = "SOL/USDT 4h 近 4 年共识别到 41 个箱体，宽度中位数 2.70%。"

    with _patch_llm(_resp(plain)):
        out = await agent.handle(_msg())

    assert out == plain


@pytest.mark.asyncio
async def test_chat_with_tools_marks_degraded_and_retries_four_times():
    """llm_client 层：全部重试失败 → degraded=True 且原因含状态码；重试次数 = 4。"""
    client = LLMClient()
    calls = {"n": 0}

    async def _boom(*_a, **_k):
        calls["n"] += 1
        req = httpx.Request("POST", "https://relay.example/v1/chat/completions")
        resp = httpx.Response(
            500, request=req, text='{"error":{"code":"do_request_failed"}}'
        )
        raise httpx.HTTPStatusError(
            "500 Internal Server Error", request=req, response=resp
        )

    with (
        patch.object(LLMClient, "_call_openai_with_tools", _boom),
        patch("apps.agent.llm_client.asyncio.sleep", AsyncMock()),
        patch.object(LLMClient, "chat", AsyncMock(return_value="降级答复")),
    ):
        r = await client.chat_with_tools(
            "sys",
            [{"role": "user", "content": "hi"}],
            tools=[{"type": "function", "function": {"name": "t", "parameters": {}}}],
        )

    assert calls["n"] == TOOL_CALL_MAX_RETRIES == 4
    assert r.degraded is True
    assert "500" in r.degradation_reason
    assert r.content == "降级答复"
    assert r.has_tool_calls is False


def test_llm_tool_response_defaults_are_not_degraded():
    """默认响应绝不是 degraded（避免误伤正常路径）。"""
    from apps.agent.llm_client import LLMToolResponse

    r = LLMToolResponse(content="ok")
    assert r.degraded is False
    assert r.degradation_reason == ""
