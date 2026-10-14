"""P1 回归：get_task_result 复算值注入（指标虚构拦截）。

事故（2026-09-18，定时查询任务 6e2d7793）：
- agent 调用 get_task_result 拿到真值（9 笔 / 夏普 -0.2328 / 胜率 55.56% /
  总收益 -0.5594% / 最大回撤 1.07%）；
- 随后 notify_user 失败（DB 连接被 PG 杀死），LLM 回退"直接输出报告"，
  输出中 **全部** 关键指标被虚构（26 笔 / -0.87 / 38.5% / -12.34% / 18.67%），
  且直接返回路径（had_tool_results=True）不经过任何守卫 → 虚构报告落库。

修复（本文件覆盖）：
1. get_task_result 成功且含指标字段时，在工具结果文本上追加「复算值锚定」块，
   使 LLM 后续每一轮（含直接返回的最终轮）都能在原始数据旁边看到锚定值；
2. summary 轮（轮次耗尽 / 空内容路径）的 prompt 同样注入锚定块 + 硬规则。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apps.agent.base import AgentMessage, BaseAgent

# 2026-09-18 真实事故数据（celery result 与 DB 回读精确一致）
TASK_RESULT_DATA = {
    "task_id": "3c13ea83-6a56-43b4-a012-53dbcf86a4c4",
    "status": "SUCCESS",
    "result": {
        "final_equity": 9944.062803896,
        "total_return_pct": -0.55937196104,
        "max_drawdown_pct": -1.0732772048166423,
        "win_rate": 0.5555555555555556,
        "sharpe_ratio": -0.2327821671825402,
        "total_trades": 9,
        "result_id": "bb2c03f2-765f-4502-b809-c0fc218c1115",
    },
}


class _MetricsLoopAgent(BaseAgent):
    """最小工具循环 Agent：只带 get_task_result，轮次上限 3。"""

    name = "test_metrics_agent"
    _max_tool_rounds = 3

    def _get_tools_schema(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "get_task_result",
                    "description": "查询回测任务结果",
                    "parameters": {
                        "type": "object",
                        "properties": {"task_id": {"type": "string"}},
                        "required": ["task_id"],
                    },
                },
            }
        ]

    async def handle(self, message, on_tool_result=None):
        content, _is_fb = await self._run_tool_loop(
            "You are a quant agent.",
            [{"role": "user", "content": message.payload.get("text", "")}],
            self._get_tools_schema(),
            max_tokens=256,
            on_tool_result=on_tool_result,
        )
        return content


def _resp(content="", tool_calls=None):
    return type(
        "Resp",
        (),
        {
            "has_tool_calls": bool(tool_calls),
            "content": content,
            "tool_calls": tool_calls or [],
            "reasoning_content": "",
            "degraded": False,
            "degradation_reason": "",
        },
    )()


def _query_call():
    return type(
        "ToolCall",
        (),
        {
            "call_id": "call_query",
            "name": "get_task_result",
            "arguments": {"task_id": TASK_RESULT_DATA["task_id"]},
        },
    )()


def _msg(text="查询回测结果"):
    return AgentMessage(
        sender="scheduler", recipient="test_metrics_agent", payload={"text": text}
    )


def _tool_messages_of(mock_llm) -> list[str]:
    """取出所有 chat_with_tools 调用中 role=tool 消息的文本（去重保序：
    messages 列表跨轮次原地共享，同一消息会被多次采集）。"""
    out = []
    for call in mock_llm.chat_with_tools.await_args_list:
        messages = call.kwargs.get("messages", [])
        for m in messages:
            if m.get("role") == "tool":
                out.append(m.get("content", ""))
    return list(dict.fromkeys(out))


@pytest.mark.asyncio
async def test_tool_result_anchored_with_recomputed_metrics():
    """核心回归：get_task_result 成功 → 工具结果文本必须带复算值锚定块，
    且锚定值与返回值逐字一致（直接返回路径的最终轮 LLM 能看到锚定值）。"""
    agent = _MetricsLoopAgent()
    agent._execute_tool_call = AsyncMock(return_value=str(TASK_RESULT_DATA))

    mock_llm = MagicMock()
    mock_llm.chat_with_tools = AsyncMock(
        side_effect=[_resp("", tool_calls=[_query_call()]), _resp("final report")]
    )
    mock_llm.chat = AsyncMock(return_value="unused")

    with patch("apps.agent.llm_client.LLMClient.get_instance", return_value=mock_llm):
        out = await agent.handle(_msg())

    assert out == "final report"  # 直接返回路径（最终轮无工具调用）
    tool_msgs = _tool_messages_of(mock_llm)
    # 第 2 轮 LLM 收到的 messages 必须包含带锚定的工具结果
    anchored = [t for t in tool_msgs if "复算值锚定" in t]
    assert anchored, "工具结果未注入复算值锚定块"
    anchored_text = anchored[-1]
    assert "total_trades=9" in anchored_text
    assert "sharpe_ratio=-0.2327821671825402" in anchored_text
    assert "total_return_pct=-0.55937196104" in anchored_text
    assert "max_drawdown_pct=-1.0732772048166423" in anchored_text
    assert "win_rate=0.5555555555555556" in anchored_text
    assert "final_equity=9944.062803896" in anchored_text
    assert "result_id=bb2c03f2-765f-4502-b809-c0fc218c1115" in anchored_text
    assert "严禁" in anchored_text  # 硬规则


@pytest.mark.asyncio
async def test_summary_prompt_includes_recomputed_metrics():
    """summary 轮（轮次耗尽路径）prompt 必须注入锚定块 + 硬规则。"""
    agent = _MetricsLoopAgent()
    agent._execute_tool_call = AsyncMock(return_value=str(TASK_RESULT_DATA))

    captured = {}

    async def _capture_chat(system, user, max_tokens=8192):
        captured["user"] = user
        return "final"

    mock_llm = MagicMock()
    mock_llm.chat_with_tools = AsyncMock(
        side_effect=[
            _resp("", tool_calls=[_query_call()]),
            _resp("", tool_calls=[_query_call()]),
            _resp("", tool_calls=[_query_call()]),
        ]
    )
    mock_llm.chat = _capture_chat

    with patch("apps.agent.llm_client.LLMClient.get_instance", return_value=mock_llm):
        await agent.handle(_msg())

    assert "复算值锚定" in captured["user"]
    assert "total_trades=9" in captured["user"]
    assert "sharpe_ratio=-0.2327821671825402" in captured["user"]
    assert "未提供" in captured["user"]  # 缺失指标的处理规则


@pytest.mark.asyncio
async def test_no_anchor_when_result_pending():
    """PENDING（result 无指标）不得注入锚定块（防误伤）。"""
    pending = {"task_id": "x", "status": "PENDING", "result": {}}
    agent = _MetricsLoopAgent()
    agent._execute_tool_call = AsyncMock(return_value=str(pending))

    mock_llm = MagicMock()
    mock_llm.chat_with_tools = AsyncMock(
        side_effect=[_resp("", tool_calls=[_query_call()]), _resp("final")]
    )
    mock_llm.chat = AsyncMock(return_value="unused")

    with patch("apps.agent.llm_client.LLMClient.get_instance", return_value=mock_llm):
        await agent.handle(_msg())

    assert all("复算值锚定" not in t for t in _tool_messages_of(mock_llm))


@pytest.mark.asyncio
async def test_no_anchor_on_tool_error():
    """工具失败路径不得注入锚定块。"""
    agent = _MetricsLoopAgent()
    agent._execute_tool_call = AsyncMock(return_value="Error: Task not found")

    mock_llm = MagicMock()
    mock_llm.chat_with_tools = AsyncMock(
        side_effect=[_resp("", tool_calls=[_query_call()]), _resp("final")]
    )
    mock_llm.chat = AsyncMock(return_value="unused")

    with patch("apps.agent.llm_client.LLMClient.get_instance", return_value=mock_llm):
        await agent.handle(_msg())

    tool_msgs = _tool_messages_of(mock_llm)
    assert tool_msgs == ["Error: Task not found"]


def test_extract_metrics_unit():
    """_extract_task_metrics 单元：精确提取白名单字段。"""
    m = BaseAgent._extract_task_metrics(str(TASK_RESULT_DATA))
    assert m == TASK_RESULT_DATA["result"]
    assert BaseAgent._extract_task_metrics("Error: boom") is None
    assert BaseAgent._extract_task_metrics("not a dict") is None
    assert (
        BaseAgent._extract_task_metrics(str({"status": "PENDING", "result": {}}))
        is None
    )
    # one_time 任务分支：result 是 str → 无指标
    assert (
        BaseAgent._extract_task_metrics(str({"status": "SUCCESS", "result": "hello"}))
        is None
    )
