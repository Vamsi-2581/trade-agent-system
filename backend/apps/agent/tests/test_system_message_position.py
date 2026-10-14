"""Regression test: 压缩对话摘要 (role=system) 不得以非首位 system 消息发给 LLM 网关。

Bug 链路:
1. InMemoryConversationLog/RedisConversationLog 压缩历史时生成
   {"role": "system", "text": summary, "compressed": True}
2. sub_agents._get_recent_conversation_context 原样映射进 LLM messages
3. llm_client 组包 full_messages = [system] + messages → system 出现在 index>0
4. 网关 400 "System message must be at the beginning." → 降级 plain chat

测试模拟网关的同一条校验规则，走 压缩→映射→组包→400 处理 的真实代码路径。
"""

import httpx
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from apps.agent.llm_client import LLMClient
from apps.memory.conversation import InMemoryConversationLog


class _StubCompressor:
    async def compress(self, old_turns):
        return "压缩摘要: " + " | ".join(t.get("text", "") for t in old_turns)


async def _compressed_history() -> list[dict]:
    """构造一条触发过压缩的对话历史（含 role=system 的摘要条目）"""
    log = InMemoryConversationLog(
        compressor=_StubCompressor(),
        compress_threshold_tokens=10,
        keep_recent_rounds=2,
    )
    entries = []
    for i in range(3):
        entries.append({"role": "user", "text": f"Q{i} " + "x" * 20, "ts": i})
        entries.append({"role": "agent", "text": f"A{i} " + "y" * 20, "ts": i + 1})
    await log.append(entries)
    history = await log.get()
    assert any(e.get("role") == "system" and e.get("compressed") for e in history), (
        "前置条件：历史中应存在压缩摘要条目"
    )
    return history


def _gateway_violation(messages: list[dict]) -> bool:
    """模拟网关校验：system 消息只允许出现在最前"""
    return any(m.get("role") == "system" for m in messages[1:])


def _make_post(violations: list):
    def _post(url, **kwargs):
        payload_msgs = kwargs["json"]["messages"]
        if _gateway_violation(payload_msgs):
            violations.append(payload_msgs)
            resp = MagicMock()
            resp.status_code = 400
            resp.url = httpx.URL(str(url))
            resp.text = (
                '{"error":{"message":"System message must be at the beginning.",'
                '"type":"BadRequestError","param":"","code":400}}'
            )
            resp.raise_for_status.side_effect = httpx.HTTPStatusError(
                "400 Bad Request",
                request=httpx.Request("POST", str(url)),
                response=resp,
            )
            return resp
        resp = MagicMock()
        resp.status_code = 200
        resp.url = httpx.URL(str(url))
        resp.json.return_value = {
            "choices": [
                {
                    "message": {"role": "assistant", "content": "OK"},
                    "finish_reason": "stop",
                }
            ]
        }
        return resp

    return _post


@pytest.mark.asyncio
async def test_compressed_summary_does_not_trigger_gateway_400(monkeypatch):
    from django.conf import settings

    from apps.agent.sub_agents import _LLMAgent

    monkeypatch.setattr(settings, "OPENAI_API_KEY", "test-key", raising=False)

    history = await _compressed_history()

    agent = _LLMAgent()

    class _MemStub:
        async def get_conv_history(self, max_turns=None):
            return history

    agent._get_memory_manager = lambda message: _MemStub()
    context = await agent._get_recent_conversation_context(object())
    assert context, "应取到上下文消息"
    messages = context + [{"role": "user", "content": "继续"}]

    violations: list = []
    client = LLMClient.get_instance()
    with patch("httpx.AsyncClient") as mock_cls:
        mock_http = AsyncMock()
        mock_http.post = AsyncMock(side_effect=_make_post(violations))
        mock_http.__aenter__.return_value = mock_http
        mock_http.__aexit__.return_value = None
        mock_cls.return_value = mock_http
        resp = await client.chat_with_tools(
            system="sys prompt", messages=messages, tools=[], max_tokens=256
        )

    assert not violations, (
        "触发网关 400：system 消息未出现在最前。首个违规 payload：\n"
        + str(
            [
                {k: (v[:60] if isinstance(v, str) else v) for k, v in m.items()}
                for m in violations[0]
            ]
        )
    )
    assert resp.content == "OK"
