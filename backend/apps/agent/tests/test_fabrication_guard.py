"""P0 回归：工具反复失败后的 summary 轮虚构成功报告拦截。

事故（2026-09-18，BNB 4H 做空研究，workflow 36252d66）：
- quant 步 32 轮工具循环中 write_file 连续 30 次失败（"file_path 参数缺失"）；
- 轮次耗尽后，裸的 "请根据以上工具调用结果给出最终回答" summary prompt
  让 LLM 虚构了完整成功报告：
    "3. 生成策略代码 ✅ `rsi_macd_short_strategy.py` 已写入"（文件不存在）
    "5. 提交回测 ✅ task_id: backtest_20260918_021000_1001"（DB 无记录）
    "6. 获取回测结果 ✅ 夏普比率 3.41 / 胜率 71.2%"（指标全部虚构）
- 原有防虚构守卫只在「零工具执行」时触发（had_tool_results=False），
  本次 read_file/list_directory 成功过 → 守卫失明 → 工作流 2/2 STEP_OK。

修复（本文件覆盖）：
1. summary 轮失败感知：告知 LLM 终止原因 + 失败工具清单 + 禁止宣称成功；
2. summary 输出过磁盘验证守卫：声称写入的文件若经磁盘验证不存在
   → 拦截虚构报告，返回如实失败说明。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apps.agent.base import AgentMessage, BaseAgent

NEVER_WRITTEN = "test_fabrication_never_written_9f3a.py"


class _WriteLoopAgent(BaseAgent):
    """最小工具循环 Agent：只带 write_file，轮次上限 3。"""

    name = "test_fab_agent"
    _max_tool_rounds = 3

    def _get_tools_schema(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "write_file",
                    "description": "写入文件",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "file_path": {"type": "string"},
                            "content": {"type": "string"},
                        },
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


def _write_call():
    return type(
        "ToolCall",
        (),
        {
            "call_id": "call_write",
            "name": "write_file",
            "arguments": {"file_path": "strategies/x.py", "content": "x"},
        },
    )()


def _patch_llm(*responses, plain_chat=""):
    mock_llm = MagicMock()
    mock_llm.chat_with_tools = AsyncMock(side_effect=list(responses))
    mock_llm.chat = AsyncMock(return_value=plain_chat)
    return patch("apps.agent.llm_client.LLMClient.get_instance", return_value=mock_llm)


def _msg(text="创建策略"):
    return AgentMessage(
        sender="user", recipient="test_fab_agent", payload={"text": text}
    )


FABRICATED_SUMMARY = (
    "✅ 策略创建成功 + 回测完成\n\n"
    "| 3. 生成策略代码 | ✅ | " + f"`{NEVER_WRITTEN}` 已写入 |\n"
    "| 5. 提交回测 | ✅ | task_id: backtest_20260918_021000_1001 |\n"
    "| 6. 获取回测结果 | ✅ | 夏普比率 3.41 / 胜率 71.2% |\n"
)

HONEST_SUMMARY = (
    "策略创建未完成：write_file 连续 3 次失败（file_path 参数缺失），"
    "策略文件未写入，回测未提交。请排查工具调用参数后重试。"
)


@pytest.mark.asyncio
async def test_fabricated_summary_after_failed_tools_is_blocked():
    """核心回归：write_file 反复失败撞轮次上限 + summary 虚构成功 → 必须拦截。"""
    agent = _WriteLoopAgent()
    agent._execute_tool_call = AsyncMock(
        return_value="Error: file_path 参数缺失（实际收到: {'content': '<str 8123 字符>'}）"
    )

    with _patch_llm(
        _resp("", tool_calls=[_write_call()]),
        _resp("", tool_calls=[_write_call()]),
        _resp("", tool_calls=[_write_call()]),
        plain_chat=FABRICATED_SUMMARY,
    ):
        out = await agent.handle(_msg())

    assert "已被系统拦截" in out
    assert "write_file 失败 3 次" in out
    # 虚构内容不得外泄（文件名 / 假 task_id / 假指标）
    assert NEVER_WRITTEN not in out
    assert "backtest_20260918_021000_1001" not in out
    assert "3.41" not in out
    assert "夏普比率" not in out


@pytest.mark.asyncio
async def test_honest_failure_summary_is_not_blocked():
    """防误伤：如实报告失败的 summary 必须原样放行。"""
    agent = _WriteLoopAgent()
    agent._execute_tool_call = AsyncMock(return_value="Error: file_path 参数缺失")

    with _patch_llm(
        _resp("", tool_calls=[_write_call()]),
        _resp("", tool_calls=[_write_call()]),
        _resp("", tool_calls=[_write_call()]),
        plain_chat=HONEST_SUMMARY,
    ):
        out = await agent.handle(_msg())

    assert out == HONEST_SUMMARY


@pytest.mark.asyncio
async def test_summary_prompt_includes_failure_context(tmp_path):
    """summary 轮 prompt 必须包含失败工具清单与禁止宣称成功指令。"""
    agent = _WriteLoopAgent()
    agent._execute_tool_call = AsyncMock(return_value="Error: boom")

    captured = {}

    async def _capture_chat(system, user, max_tokens=2048):
        captured["user"] = user
        return HONEST_SUMMARY

    mock_llm = MagicMock()
    mock_llm.chat_with_tools = AsyncMock(
        side_effect=[
            _resp("", tool_calls=[_write_call()]),
            _resp("", tool_calls=[_write_call()]),
            _resp("", tool_calls=[_write_call()]),
        ]
    )
    mock_llm.chat = _capture_chat

    with patch("apps.agent.llm_client.LLMClient.get_instance", return_value=mock_llm):
        await agent.handle(_msg())

    assert "write_file 失败 3 次" in captured["user"]
    assert "严禁声称文件已写入" in captured["user"]


def test_claims_unwritten_file_no_claim():
    assert BaseAgent._claims_unwritten_file("没有文件声明的普通总结") is False
    assert BaseAgent._claims_unwritten_file("") is False


def test_claims_unwritten_file_missing_file():
    assert BaseAgent._claims_unwritten_file(f"`{NEVER_WRITTEN}` 已写入") is True
    assert BaseAgent._claims_unwritten_file(f"已写入 {NEVER_WRITTEN}") is True
    assert (
        BaseAgent._claims_unwritten_file("/nonexistent/abs_path_xyz.py 已生成") is True
    )


def test_claims_unwritten_file_existing_file(tmp_path, monkeypatch):
    real = tmp_path / "real_strategy.py"
    real.write_text("x = 1")
    # 相对声明 → 在 WORKSPACE_ROOT（重定向到 tmp）下验证
    import apps.agent.tools.file_io as file_io

    monkeypatch.setattr(file_io, "WORKSPACE_ROOT", tmp_path)
    assert BaseAgent._claims_unwritten_file(f"`{real.name}` 已写入") is False
    # 绝对声明 → 按原样验证
    assert BaseAgent._claims_unwritten_file(f"已写入 `{real}`") is False
