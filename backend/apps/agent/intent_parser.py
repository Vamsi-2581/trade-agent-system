from __future__ import annotations

import json
import logging
import re
from typing import Any

from .llm_client import LLMClient, is_fallback
from .prompt_loader import PromptLoader

logger = logging.getLogger(__name__)

# LLM 返回的 JSON 中表达「定时/周期执行」语义的字段值
_SCHEDULE_TASK_TYPES = {"scheduled", "recurring"}


def _carries_schedule_intent(data: dict) -> bool:
    """LLM 返回的意图 JSON 是否带「定时/周期执行」语义。

    为什么需要（2026-09-20 事故取证）：用户「每天10点全面分析加密货币市场行情并发送结果」，
    LLM 其实已正确解析为
        {"agent": "supervisor", "task_type": "recurring",
         "schedule": "daily at 10:00", "summary": ..., "steps": [{...}]}
    但 parse() 里的「裸 steps → _workflow_plan」分支先命中，把它当成一次性多 Agent
    工作流当场执行：用户只看到立刻分析了一次（workflow_history 留有 1 步 analyst 记录），
    每天10点的周期任务从未注册到 django_celery_beat。

    agent=supervisor + steps 也归入此类：supervisor 自身没有「立即执行工作流」的落地
    路径（它只有 submit_scheduled_task / submit_recurring_task 这类调度工具），prompt 中
    该组合只用于周期工作流。
    """
    if str(data.get("task_type", "")).lower() in _SCHEDULE_TASK_TYPES:
        return True
    return data.get("agent") == "supervisor" and bool(data.get("steps"))


class IntentParser:
    """Parse user intent via LLM + rule-based fallback."""

    def __init__(
        self,
        llm_client: LLMClient,
        router: Any,  # IntentRouter
        system_prompt: str,
        agent_tools: list | None = None,
        get_skills_summary: callable | None = None,
    ):
        self._llm = llm_client
        self._router = router
        self._system_prompt = system_prompt
        self._agent_tools = agent_tools or []
        self._get_skills_summary_fn = get_skills_summary

    async def parse(
        self, text: str, context: list | None = None, exclude_agents: list | None = None
    ) -> str | dict:
        """
        调用LLM解析意图，基于 Agent 概述动态识别。

        Returns:
            str: Agent 名称（如 'quant'）或框架意图（如 'start_trading'）
            dict: {'_free_chat': True, 'response': '...'} 未识别意图时 LLM 直接返回回复
        """
        if not text:
            return "unknown"

        # 构建 Agent 概述列表（排除已尝试的）
        agents = PromptLoader.list_agents()
        if exclude_agents:
            agents = [a for a in agents if a.get("name") not in exclude_agents]

        agent_descriptions = "\n".join(
            f"- {a['name']}: {a.get('description', '').replace(chr(10), chr(10) + '  ')}"
            for a in agents
            if a.get("name")
        )

        # 框架意图列表（仍然需要检查）
        frame_intents = self._router.all_frame_intents()
        frame_block = ""
        if frame_intents:
            frame_block = (
                f"Framework intents (system actions): {json.dumps(frame_intents)}\n"
            )

        context_block = ""
        if context:
            context_block = (
                f"Recent routing history (for reference): {json.dumps(context)}\n\n"
            )

        exclude_block = ""
        if exclude_agents:
            exclude_block = (
                f"Exclude these agents (already tried): {exclude_agents}\n\n"
            )

        skills_summary = ""
        if self._get_skills_summary_fn:
            skills_summary = self._get_skills_summary_fn()
        skills_block = (
            f"Available agent skills:\n{skills_summary}\n\n" if skills_summary else ""
        )

        # Supervisor 自身能力（工具声明）
        supervisor_block = ""
        if self._agent_tools:
            supervisor_block = (
                f"Your own tools (handle these yourself): {json.dumps(self._agent_tools)}\n"
                f'If the user request matches these tools, reply: {{"agent": "supervisor"}}\n\n'
            )

        user_prompt = (
            f"{context_block}"
            f"{exclude_block}"
            f"{skills_block}"
            f"{frame_block}"
            f"{supervisor_block}"
            f"Available agents:\n{agent_descriptions}\n\n"
            f'根据以上信息与流程定义，解析消息: " {text} " 中包含的用户意图 \n\n'
            "if the message is a general inquiry or doesn't clearly match any agent, reply with:\n"
            '{"_free_chat": true, "response": "your response to user"}\n'
        )

        logger.debug("Supervisor system prompt: %s", self._system_prompt)
        logger.debug("Supervisor intent parsing prompt: %s", user_prompt)
        response = await self._llm.chat(
            system=self._system_prompt,
            user=user_prompt,
            max_tokens=256,
            temperature=0.1,
        )

        if is_fallback(response):
            fallback_text = self._rule_based_intent(text)
            if fallback_text:
                return fallback_text
            return {"_free_chat": True, "response": None}

        try:
            text = response.strip()
            fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
            if fence_match:
                text = fence_match.group(1).strip()
            else:
                obj_match = re.search(r"\{[\s\S]*\}", text)
                if obj_match:
                    text = obj_match.group(0).strip()
            data = json.loads(text)
            logger.info(f"Parsed intent data: {data}")

            # 自由对话：LLM 直接返回回复内容
            if data.get("_free_chat"):
                return {"_free_chat": True, "response": data.get("response", "")}

            # 定时/周期意图：必须交给 supervisor 自身处理，不能降级成「立即执行的工作流」
            if _carries_schedule_intent(data):
                return "supervisor"

            # 工作流计划：多 Agent 顺序任务（支持 _workflow_plan 包装和裸 steps 两种格式）
            if data.get("_workflow_plan"):
                return data
            if isinstance(data.get("steps"), list) and data["steps"]:
                return {"_workflow_plan": data}

            # 框架意图
            if data.get("intent") and data["intent"] in frame_intents:
                return data["intent"]

            # Agent 路由：返回 agent name
            agent_name = data.get("agent")
            if agent_name:
                return agent_name

            return "unknown"
        except Exception:
            logger.warning(f"Intent parse failed, raw: {response[:200]}")
            fallback_text = self._rule_based_intent(text)
            if fallback_text:
                return fallback_text
            return {"_free_chat": True, "response": None}

    def _rule_based_intent(self, text: str) -> str | None:
        """规则引擎降级"""
        return self._router.match_fallback(text)
