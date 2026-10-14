"""Compressor 协议的两个实现。

- LLMCompressor：生产用，调用 LLMClient 生成摘要
- NoopCompressor：测试用，返回固定占位符
- IdentityCompressor：测试用，原样拼接（便于断言）
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class LLMCompressor:
    """生产用：调 LLM 把旧对话压缩成摘要。

    把 llm_client 的依赖限制在这个类里，ConversationLog 本身不知道 LLM 存在。
    """

    def __init__(self, *, max_tokens: int = 1500, temperature: float = 0.3):
        self._max_tokens = max_tokens
        self._temperature = temperature

    async def compress(self, old_turns: list[dict]) -> str:
        if not old_turns:
            return ""

        conversation_text = "\n".join(
            f"{item.get('role', 'user')}: {item.get('text', '')}" for item in old_turns
        )

        from apps.agent.llm_client import LLMClient

        llm = LLMClient.get_instance()
        prompt = (
            "请将以下对话历史压缩成简洁的要点摘要（保留关键信息，去除冗余细节）：\n\n"
            f"{conversation_text}\n\n"
            "摘要格式：\n"
            "- 按时间顺序列出关键话题和结论\n"
            "- 每个要点用 1-2 行描述\n"
            "- 保留重要的数字、日期、决策\n"
            "- 总长度控制在 2000 字以内"
        )

        summary = await llm.chat(
            system="你是一个对话摘要助手，擅长提炼关键信息。",
            user=prompt,
            max_tokens=self._max_tokens,
            temperature=self._temperature,
        )
        return summary or "（对话历史已压缩，但摘要生成失败）"


class NoopCompressor:
    """测试用：返回固定占位符，不调用任何外部服务。"""

    def __init__(self, placeholder: str = "[compressed]"):
        self._placeholder = placeholder

    async def compress(self, old_turns: list[dict]) -> str:
        return self._placeholder


class IdentityCompressor:
    """测试用：原样拼接旧轮次，便于在测试中断言输入输出一致。"""

    async def compress(self, old_turns: list[dict]) -> str:
        return "\n".join(
            f"{item.get('role', 'user')}: {item.get('text', '')}" for item in old_turns
        )
