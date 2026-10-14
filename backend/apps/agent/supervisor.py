from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional

from .base import BaseAgent, AgentMessage, AgentResult
from .llm_client import LLMClient
from .prompt_loader import PromptLoader
from .frame_manager import FrameManager
from .session_manager import get_session_manager, SessionState

# Backward-compatible re-exports (used by tests).
from .workflow_engine import (  # noqa: F401
    PAUSE_TTL,
    WorkflowContext,
    WorkflowEngine,
    WorkflowStep,
    _looks_like_uuid,
)
from .memory_injector import MemoryInjector  # noqa: F401
from .intent_router import (  # noqa: F401
    IntentRouter,
    register_intent,
    register_frame_intent,
    register_fallback_rule,
)
from .intent_parser import IntentParser  # noqa: F401
from .user_resolver import resolve_user_id, get_scheduler_user_id  # noqa: F401

logger = logging.getLogger(__name__)

MAX_REROUTE = 2
# Kept as a module-level alias for backward compatibility (used by tests
# and the intent registration block below).
PAUSE_TTL = PAUSE_TTL

# ------------------------------------------------------------------ #
#  斜杠命令系统                                                         #
# ------------------------------------------------------------------ #

# 命令名 → 处理方法名
SLASH_COMMANDS = {
    "/new": "_handle_new_session",
    "/cancel": "_handle_cancel",
    "/event": "_handle_event",
    "/regime": "_handle_regime",
}

# 收参数的命令。`/new` 与 `/cancel` 不收，所以默认是不收：只有列在这里的命令才会
# 拿到命令词之后的那一段。写成一张显式清单而不是靠 `inspect.signature` 去猜——
# 「这条命令收不收参数」是一条契约，不是实现细节。
#
# `/regime` 收：`/regime` 裸着是确认页，`/regime on|off` 才是切换。它自己只认整词、
# 多一个词就报错（同 `/event` 的纪律），但「收不收参数」这件事仍然得在这里登记。
COMMANDS_WITH_ARGS = {"/event", "/regime"}

# 中文别名 → 标准命令。
#
# **别名只做整串精确匹配，不做前缀匹配。** 「取消」是一个会出现在正常句子里的词
# （「把刚才那个取消了吧」），按前缀认会把人话劫持成命令，而且是在意图解析之前被
# 劫持——用户看不出来发生了什么。
COMMAND_ALIASES = {
    "新建会话": "/new",
    "取消": "/cancel",
}

#: 命令 → 一句话说明（用于未知命令时的提示）
COMMAND_HINTS = {
    "/new": "新建会话，清除对话历史",
    "/cancel": "取消当前工作流",
    "/event": "维护重大事件（录入 / 改档 / 改期 / 取消 / 候选转正），不带参数看用法",
    "/regime": "机制开关与上线体检：不带参数看事件熔断的确认页，直接发 /regime 看全部用法"
    "（人工确认，不自动切）",
}

# 可用命令列表（用于错误提示）
AVAILABLE_COMMANDS = "\n".join(
    f"  {cmd}  {COMMAND_HINTS.get(cmd, '')}".rstrip()
    for cmd in sorted(SLASH_COMMANDS.keys())
)


def _split_command(text: str) -> tuple[str, str]:
    """`"/event add 高 …"` → `("/event", "add 高 …")`。

    **只切一刀，剩下的原样交给 handler。** 原来这里只取 `split()[0]`、参数被无声
    丢掉（CONTEXT.md 第 119 条点名的毛病）：`/event add …` 会被当成一个不带参数的
    `/event` 执行，看起来就像命令不生效。怎么读那一段是各命令自己的事——事件命令
    认选项与位置参数，别的命令可能什么都不收。
    """
    parts = text.split(maxsplit=1)
    head = parts[0].lower()
    tail = parts[1].strip() if len(parts) > 1 else ""
    return head, tail


# ------------------------------------------------------------------ #
#  内置意图注册（保持向后兼容）                                          #
# ------------------------------------------------------------------ #

_router = IntentRouter.get_instance()

# 默认框架意图
_router.register_frame_intent("start_trading", "trading", "start")
_router.register_frame_intent("stop_trading", "trading", "stop")
_router.register_frame_intent("start_monitor", "assist", "start")
_router.register_frame_intent("stop_monitor", "assist", "stop")

# 默认降级规则（直接映射到 Agent 名称，非中间意图名）
_router.register_fallback_rules(
    [
        (r"(分析|行情|走势|K线|趋势).*(BTC|ETH|币|市场)", "analyst"),
        (r"(回测|测试策略|历史数据|backtest)", "quant"),
        (r"(风险|止损|仓位|风控)", "risk_advisor"),
        (r"(计划|复盘|总结|周报)", "coach"),
        (r"(实现.*策略|创建.*策略|编写.*策略|生成.*策略代码)", "quant"),
        (
            r"(研究|调研|收集.*资料|查找.*知识|搜索.*信息|内容研究|找.*策略|搜索.*策略)",
            "researcher",
        ),
        (
            r"(价格|突破|跌破|高于|低于|提醒|通知|监控).*(BTC|ETH|币|\d{4,})",
            "supervisor",
        ),
    ]
)


class SupervisorAgent(BaseAgent):
    """主管Agent：LLM解析用户意图、路由子Agent、管理框架生命周期"""

    name = "supervisor"
    _agent_tools: list[str] = []

    _instance: Optional["SupervisorAgent"] = None

    def __init__(self):
        super().__init__()
        self._llm = LLMClient.get_instance()
        self._frame = FrameManager.get_instance()
        self._router = IntentRouter.get_instance()
        self._current_user_id: str = ""

        # 动态发现所有 Agent（从 Prompt 文件）
        from .registry import AgentRegistry

        AgentRegistry.discover_from_prompts()

        # 从 prompt 元数据加载工具声明
        from .prompt_loader import _parse_frontmatter

        prompt_file = (
            Path(__file__).parent.parent.parent / "prompts" / "v1" / "supervisor.txt"
        )
        raw = prompt_file.read_text(encoding="utf-8")
        meta, _ = _parse_frontmatter(raw)

        # 用 prompt 中声明的工具覆盖硬编码列表
        if "tools" in meta:
            self._agent_tools = meta["tools"]

        self._system_prompt = PromptLoader.load("supervisor")

        # Intent parser delegates LLM-based intent resolution
        self._intent_parser = IntentParser(
            llm_client=self._llm,
            router=self._router,
            system_prompt=self._system_prompt,
            agent_tools=getattr(self, "_agent_tools", []),
            get_skills_summary=self._get_skills_summary,
        )

        # Workflow engine is bound to this supervisor's route_to_agent
        # callable so the engine remains testable in isolation.
        self._workflow_engine = WorkflowEngine(
            llm_client=self._llm,
            route_to_agent=self._route_to_agent,
            agent_name=self.name,
        )

    def _build_system_prompt_with_skills(self) -> str:
        """动态构建 system prompt，注入 always 技能内容。"""
        return self._build_skills_section(self._system_prompt)

    @classmethod
    def get_instance(cls) -> "SupervisorAgent":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ------------------------------------------------------------------ #
    #  Entry point                                                         #
    # ------------------------------------------------------------------ #

    async def handle(self, message: AgentMessage, on_tool_result=None) -> AgentResult:
        text = message.payload.get("text", "").strip()

        # ========== 斜杠命令分发 ==========
        # 中文别名 → 标准命令（整串精确匹配，见 COMMAND_ALIASES）
        normalized = COMMAND_ALIASES.get(text, text)
        if normalized.startswith("/"):
            cmd, args = _split_command(normalized)
            handler_name = SLASH_COMMANDS.get(cmd)
            if handler_name:
                handler = getattr(self, handler_name)
                if cmd in COMMANDS_WITH_ARGS:
                    return await handler(message, args)
                return await handler(message)
            # 未识别的斜杠命令
            return AgentResult(
                task_id=message.task_id,
                success=True,
                data=f"未知命令: {cmd}\n可用命令:\n{AVAILABLE_COMMANDS}",
            )

        session_mgr = get_session_manager()
        session_ctx = await session_mgr.get_session_context(message.user_id)

        # ========== 会话状态分发 ==========
        if session_ctx:
            state = session_ctx["state"]

            if state == SessionState.PAUSED.value:
                return await self._handle_paused_session(
                    message, session_ctx, session_mgr, on_tool_result
                )

            if state == SessionState.MULTI_TURN.value:
                return await self._handle_multi_turn(
                    message, session_ctx, session_mgr, on_tool_result
                )

            if state == SessionState.WORKFLOW_RUNNING.value:
                return await self._handle_workflow_running(
                    message, session_ctx, session_mgr
                )

        # ========== 正常路由流程 ==========
        return await self._normal_route(message, on_tool_result)

    # ------------------------------------------------------------------ #
    #  斜杠命令 handlers                                                   #
    # ------------------------------------------------------------------ #

    async def _handle_new_session(self, message: AgentMessage) -> AgentResult:
        """新建会话：清除 SessionState + conv_history"""
        session_mgr = get_session_manager()

        # 检查工作流状态：进行中则拒绝
        ctx = await session_mgr.get_session_context(message.user_id)
        if ctx and ctx.get("state") == SessionState.WORKFLOW_RUNNING.value:
            return AgentResult(
                task_id=message.task_id,
                success=False,
                error="请先发送 /cancel 停止当前工作流，再新建会话。",
            )

        # 清除会话状态
        await session_mgr.clear_session_context(message.user_id)

        # 清除对话历史
        mi = MemoryInjector(message.user_id)
        await mi.clear_conv_history()

        logger.info("[supervisor] new session created for user=%s", message.user_id)
        return AgentResult(
            task_id=message.task_id,
            success=True,
            data="✅ 已新建会话，对话历史已清除。",
        )

    async def _handle_cancel(self, message: AgentMessage) -> AgentResult:
        """取消当前工作流"""
        session_mgr = get_session_manager()
        ctx = await session_mgr.get_session_context(message.user_id)

        # 没有工作流运行
        if not ctx or ctx.get("state") != SessionState.WORKFLOW_RUNNING.value:
            return AgentResult(
                task_id=message.task_id,
                success=True,
                data="没有正在执行的工作流。",
            )

        # 标记任务为 cancelled
        wf_task_id = ctx.get("task_id")
        if wf_task_id:
            from apps.agent.task_tracker import TaskTracker

            TaskTracker.cancel(wf_task_id)

        # 清除会话状态
        await session_mgr.clear_session_context(message.user_id)

        logger.info(
            "[supervisor] workflow cancelled for user=%s task=%s",
            message.user_id,
            wf_task_id,
        )
        return AgentResult(
            task_id=message.task_id,
            success=True,
            data="✅ 工作流已取消。",
        )

    async def _handle_event(self, message: AgentMessage, args: str) -> AgentResult:
        """`/event …`：重大事件的人工维护入口（CONTEXT.md 第 152 条走 slash，不引入 admin）。

        **分发发生在意图解析之前**，所以到达 `events.py` 的落款始终是「人敲的」——
        `events.py` 那条「提升为『高』必须人工」的判据认的正是这个。

        按需引入：supervisor 是每条消息都会走的模块，没有理由让它的导入期顺带把
        regime 的模型层也拉起来。
        """
        from .event_commands import handle_event_command

        return await handle_event_command(message, args)

    async def _handle_regime(self, message: AgentMessage, args: str) -> AgentResult:
        """`/regime …`：事件熔断开关的人工入口（第②段 ②f）。

        CONTEXT.md 第160 条：**出 Shadow 切到执行态必须人工确认，不自动切换**。这份
        「人工」的身份由分发位置保证——命令在意图解析之前被截下来，所以落进
        `RegimeMechanismSwitch.actor_kind` 的 `chat` 是真的有人在敲，而不是一次 LLM
        判断的副产品。

        按需引入（与 `_handle_event` 同一条理由）：supervisor 每条消息都会走到，
        没有理由让它的导入期顺带把 regime 的模型层也拉起来。
        """
        from .regime_commands import handle_regime_command

        return await handle_regime_command(message, args)

    # ------------------------------------------------------------------ #
    #  会话状态 handlers                                                   #
    # ------------------------------------------------------------------ #

    async def _handle_paused_session(
        self,
        message: AgentMessage,
        ctx: dict,
        session_mgr,
        on_tool_result=None,
    ) -> AgentResult:
        """处理暂停中的会话"""
        paused_at = ctx.get("paused_at", 0)
        ttl = ctx.get("pause_ttl", PAUSE_TTL)

        # 超时 → 归档
        if time.time() - paused_at > ttl:
            await self._archive_paused_session(message.user_id, ctx, session_mgr)
            return await self._normal_route(message, on_tool_result=on_tool_result)

        # Supervisor 判断是否应恢复原会话
        should_resume = await self._judge_resume(message, ctx)

        if should_resume:
            agent_name = ctx["active_agent"]
            await session_mgr.resume_session(message.user_id, agent_name)
            return await self._route_to_agent(
                agent_name, message, on_tool_result=on_tool_result
            )

        # 继续处理当前意图，保持暂停状态
        return await self._normal_route(message, on_tool_result=on_tool_result)

    async def _handle_multi_turn(
        self,
        message: AgentMessage,
        ctx: dict,
        session_mgr,
        on_tool_result=None,
    ) -> AgentResult:
        """处理多轮对话中的消息"""
        agent_name = ctx["active_agent"]
        if not agent_name:
            await session_mgr.clear_session_context(message.user_id)
            return await self._normal_route(message, on_tool_result=on_tool_result)

        # 初始化 MemoryInjector
        mi = MemoryInjector(message.user_id)

        result = await self._route_to_agent(
            agent_name, message, on_tool_result=on_tool_result
        )

        # 写入统一对话历史（原子追加，防止并发覆盖）
        if mi.available and result.success and not result.need_reroute:
            user_text = message.payload.get("text", "")
            agent_text = str(result.data) if result.data else ""
            await mi.append_conv_history(
                [
                    {
                        "role": "user",
                        "agent": agent_name,
                        "text": user_text,
                        "ts": int(time.time()),
                    },
                    {
                        "role": "agent",
                        "agent": agent_name,
                        "text": agent_text,
                        "ts": int(time.time()),
                    },
                ]
            )

        # SubAgent 拒收
        if result.need_reroute:
            logger.info(
                "Multi-turn agent %s rejected: %s, pausing session",
                agent_name,
                result.reroute_reason,
            )
            await self._pause_session(message, session_mgr, agent_name, result)

            # 尝试用新意图路由
            new_parsed = await self._parse_intent(message.payload.get("text", ""))
            if isinstance(new_parsed, dict) and new_parsed.get("_free_chat"):
                if new_parsed.get("response"):
                    return AgentResult(
                        task_id=message.task_id,
                        success=True,
                        data=new_parsed["response"],
                    )
                return await self._free_chat(message, on_tool_result=on_tool_result)
            # 新流程：parsed 直接是 agent name 或 frame intent
            new_agent = str(new_parsed)
            if new_agent and new_agent != "free_chat" and new_agent != agent_name:
                reroute_result = await self._route_to_agent(
                    new_agent, message, on_tool_result=on_tool_result
                )
                # 新Agent接受 → 更新 session 到新的 active_agent
                if not reroute_result.need_reroute:
                    await session_mgr.set_session_context(
                        message.user_id,
                        SessionState.MULTI_TURN,
                        new_agent,
                    )
                return reroute_result

            # 无法路由，走 free_chat
            return await self._free_chat(message, on_tool_result=on_tool_result)

        # 检查是否继续多轮
        if isinstance(result.data, dict) and result.data.get(
            "continue_conversation", False
        ):
            await session_mgr.set_session_context(
                message.user_id,
                SessionState.MULTI_TURN,
                agent_name,
            )
        else:
            logger.info(
                "[%s] Ending multi-turn session for user %s", self.name, message.user_id
            )
            await session_mgr.clear_session_context(message.user_id)

        return result

    async def _pause_session(
        self, message: AgentMessage, session_mgr, agent_name: str, result: AgentResult
    ) -> None:
        """暂停多轮对话"""
        # 获取当前对话上下文摘要
        pause_context = f"原意图: {agent_name}, 拒收原因: {result.reroute_reason}"
        if isinstance(result.data, dict):
            content = result.data.get("content", "")
            if content:
                pause_context = f"{content[:100]}... 续"

        await session_mgr.pause_session(
            message.user_id,
            active_agent=agent_name,
            pause_ttl=PAUSE_TTL,
            pause_context=pause_context,
        )

    async def _handle_workflow_running(
        self,
        message: AgentMessage,
        ctx: dict,
        session_mgr,
    ) -> AgentResult:
        """处理工作流执行中收到的新消息"""
        text = message.payload.get("text", "")

        # 特殊命令：取消当前工作流
        if text.strip().lower() in ("取消", "cancel", "停止工作流"):
            await session_mgr.clear_session_context(message.user_id)
            return AgentResult(
                task_id=message.task_id,
                success=True,
                data="当前工作流已取消",
            )

        # 其他消息：回复工作流状态
        return AgentResult(
            task_id=message.task_id,
            success=True,
            data="工作流正在执行中，请稍后发送新请求。发送「取消」可停止当前工作流。",
        )

    # ------------------------------------------------------------------ #
    #  正常路由流程                                                         #
    # ------------------------------------------------------------------ #

    async def _normal_route(
        self, message: AgentMessage, on_tool_result=None
    ) -> AgentResult:
        """正常路由流程：意图解析 → 路由 → 结果"""
        logger.info(
            "[%s] Handling message with intent: %s, payload keys: %s",
            self.name,
            message.intent,
            list(message.payload.keys()),
        )

        session_mgr = get_session_manager()
        mi = MemoryInjector(message.user_id)
        conversation_history: list = []
        if mi.available:
            conversation_history = await mi.get_conv_history()

        parsed = message.intent or await self._parse_intent(
            message.payload.get("text", ""),
            context=conversation_history[-5:] if conversation_history else None,
        )

        # 多步骤工作流：支持 {"_workflow_plan": {...}} 和裸 {"summary", "steps"} 两种格式
        if isinstance(parsed, dict):
            workflow_plan = parsed.get("_workflow_plan")
            if (
                not workflow_plan
                and isinstance(parsed.get("steps"), list)
                and parsed["steps"]
            ):
                workflow_plan = parsed
            if workflow_plan:
                logger.info(
                    "[%s] Multi-step workflow detected: %s",
                    self.name,
                    workflow_plan.get("summary", ""),
                )
                return await self._workflow_engine.execute_workflow(
                    workflow_plan,
                    message,
                    on_tool_result=on_tool_result,
                    on_workflow_complete=self._record_workflow,
                )

        # LLM 直接返回自由对话（节省一次 LLM 调用）
        if isinstance(parsed, dict) and parsed.get("_free_chat"):
            response_text = parsed.get("response")
            if response_text:
                return AgentResult(
                    task_id=message.task_id, success=True, data=response_text
                )
            # response 为空（LLM 降级），用 _free_chat 处理
            return await self._free_chat(message, on_tool_result=on_tool_result)

        # 新流程：parsed 直接是 agent name 或 frame intent
        resolved: str = str(parsed)

        # 框架生命周期
        if self._router.is_frame_intent(resolved):
            return await self._handle_frame(resolved, message)

        # Supervisor 自己处理（使用自身工具）
        if resolved == "supervisor":
            return await self._self_execute(message, on_tool_result=on_tool_result)

        # 路由子Agent（parsed 直接是 agent name）
        agent_name = resolved
        if agent_name and agent_name != "unknown":
            result = await self._route_to_agent(
                agent_name, message, on_tool_result=on_tool_result
            )

            # 子Agent 拒收 → 尝试重路由到建议的 agent
            if result.need_reroute:
                logger.info(
                    "Agent %s rejected in normal_route: %s, suggesting %s",
                    agent_name,
                    result.reroute_reason,
                    result.reroute_suggestion,
                )
                if (
                    result.reroute_suggestion
                    and result.reroute_suggestion != agent_name
                ):
                    return await self._route_to_agent(
                        result.reroute_suggestion,
                        message,
                        on_tool_result=on_tool_result,
                    )
                # 无法重路由，转自由对话
                return await self._free_chat(message, on_tool_result=on_tool_result)

            # 路由成功时写入统一对话历史（原子追加，防止并发覆盖）
            if mi.available and result.success and not result.need_reroute:
                user_text = message.payload.get("text", "")[:500]
                agent_text = str(result.data)[:500] if result.data else ""
                await mi.append_conv_history(
                    [
                        {
                            "role": "user",
                            "agent": agent_name,
                            "text": user_text,
                            "ts": int(time.time()),
                        },
                        {
                            "role": "agent",
                            "agent": agent_name,
                            "text": agent_text,
                            "ts": int(time.time()),
                        },
                    ],
                    keep_turns=20,
                )

                # 检查响应是否要求开始多轮对话
                if hasattr(result.data, "get") and result.data.get(
                    "start_multi_turn", False
                ):
                    await session_mgr.set_session_context(
                        message.user_id,
                        SessionState.MULTI_TURN,
                        agent_name,
                    )

            return result

        # 未知意图
        return await self._free_chat(message, on_tool_result=on_tool_result)

    async def _route_with_fallback(
        self,
        message: AgentMessage,
        intent: str,
        attempted: set | None = None,
        on_tool_result=None,
    ) -> AgentResult:
        """带拒收重路由的 Agent 调用。

        兼容旧调用方式：intent 参数可能是 agent name 或旧式 intent。
        """
        attempted = attempted or set()
        # 兼容：intent 可能是 agent name 或旧式 intent
        agent_name = self._router.get_agent_for_intent(intent) or intent

        if not agent_name or agent_name in attempted or len(attempted) >= MAX_REROUTE:
            return await self._free_chat(message, on_tool_result=on_tool_result)

        attempted.add(agent_name)
        result = await self._route_to_agent(
            agent_name, message, on_tool_result=on_tool_result
        )

        # SubAgent 拒收
        if result.need_reroute:
            logger.info("Agent %s rejected: %s", agent_name, result.reroute_reason)

            # 优先用 SubAgent 建议的目标
            if result.reroute_suggestion and result.reroute_suggestion not in attempted:
                return await self._route_with_fallback(
                    message,
                    result.reroute_suggestion,
                    attempted,
                    on_tool_result=on_tool_result,
                )

            # 重新解析意图（排除已尝试的 Agent）
            exclude_agents = list(attempted)
            new_parsed = await self._parse_intent(
                message.payload.get("text", ""),
                exclude_agents=exclude_agents,
            )
            if isinstance(new_parsed, dict) and new_parsed.get("_free_chat"):
                if new_parsed.get("response"):
                    return AgentResult(
                        task_id=message.task_id,
                        success=True,
                        data=new_parsed["response"],
                    )
                return await self._free_chat(message, on_tool_result=on_tool_result)
            new_agent = str(new_parsed)
            if new_agent and new_agent != "free_chat" and new_agent not in attempted:
                return await self._route_with_fallback(
                    message, new_agent, attempted, on_tool_result=on_tool_result
                )

            # 都失败
            return await self._free_chat(message, on_tool_result=on_tool_result)

        return result

    async def _route_to_agent(
        self,
        agent_name: str,
        message: AgentMessage,
        on_tool_result=None,
    ) -> AgentResult:
        """懒加载并调用子Agent，自动注入跨Agent上下文。"""
        # Supervisor 不走自动发现流程，直接由 supervisor 自身处理
        if agent_name == "supervisor":
            return await self._self_execute(message, on_tool_result=on_tool_result)

        from .registry import AgentRegistry

        # 注入跨agent上下文：如果上一轮是其他agent回复了用户，
        # 将其回复内容注入到当前消息的payload中，供目标agent参考。
        if message.user_id:
            try:
                mi = MemoryInjector(message.user_id)
                conv = await mi.get_conv_history()
                if len(conv) >= 1:
                    last_agent_entry = conv[-1]
                    if last_agent_entry.get("role") == "agent":
                        prev_agent = last_agent_entry.get("agent", "")
                        prev_response = last_agent_entry.get("text", "")
                        if prev_agent and prev_agent != agent_name and prev_response:
                            if "previous_agent_response" not in message.payload:
                                message.payload["previous_agent_response"] = (
                                    prev_response
                                )
                                message.payload["previous_agent_name"] = prev_agent
                                logger.info(
                                    "[%s] Injecting context: %s -> %s",
                                    self.name,
                                    prev_agent,
                                    agent_name,
                                )
            except Exception as e:
                logger.debug("Context injection failed: %s", e)

        try:
            agent = AgentRegistry.get(agent_name)
            message.sender = "supervisor"
            message.recipient = agent_name
            return await agent.handle(message, on_tool_result=on_tool_result)
        except Exception as e:
            logger.error("Routing to %s failed: %s", agent_name, e)
            return AgentResult(task_id=message.task_id, success=False, error=str(e))

    # ------------------------------------------------------------------ #
    #  Backward-compatible workflow shim                                   #
    # ------------------------------------------------------------------ #

    async def _execute_workflow(
        self,
        workflow_plan: dict,
        message: AgentMessage,
        on_tool_result=None,
    ) -> AgentResult:
        """Backward-compatible shim delegating to WorkflowEngine.

        Retained so existing tests that patch ``supervisor._execute_workflow``
        continue to work.
        """
        return await self._workflow_engine.execute_workflow(
            workflow_plan,
            message,
            on_tool_result=on_tool_result,
            on_workflow_complete=self._record_workflow,
        )

    async def _record_workflow(
        self,
        ctx: WorkflowContext,
        step_results: list,
        message: AgentMessage,
        status: str = "completed",
        error: str = "",
    ) -> None:
        """Persist workflow execution to database and notify the user."""
        try:
            from django.db import close_old_connections

            close_old_connections()

            from asgiref.sync import sync_to_async

            from apps.notify.models import Notification

            elapsed = (
                ctx["metadata"].get("completed_at", time.time())
                - ctx["metadata"]["created_at"]
            )
            completed_steps = ctx["metadata"].get("completed_steps", 0)
            total_steps = ctx["metadata"].get("total_steps", len(step_results))

            # Resolve channel user_id to Django User UUID
            user_id = await resolve_user_id(message.user_id)

            # Fallback: system_scheduler user for scheduled tasks without user context
            if not user_id:
                user_id = await get_scheduler_user_id()

            @sync_to_async
            def _create_history():
                from django.db import close_old_connections

                close_old_connections()
                from .models import WorkflowHistory

                return WorkflowHistory.objects.create(
                    workflow_id=ctx["workflow_id"],
                    user_id=user_id,
                    summary=ctx.get("summary", ""),
                    status=status,
                    total_steps=total_steps,
                    completed_steps=completed_steps,
                    step_results=step_results,
                    error=error,
                    elapsed_seconds=round(elapsed, 2),
                )

            @sync_to_async
            def _create_notification(msg: str):
                if user_id:
                    from django.db import close_old_connections

                    close_old_connections()
                    return Notification.objects.create(
                        user_id=user_id,
                        channel="web",
                        message=msg,
                    )

            await _create_history()

            status_label = (
                "工作流完成"
                if status == "completed"
                else "工作流失败"
                if status == "failed"
                else "工作流中止"
            )
            step_summary = f"{completed_steps}/{total_steps} 步"
            elapsed_str = f"{elapsed:.0f}s"
            notif_msg = (
                f"{status_label}: {ctx.get('summary', '')}\n"
                f"步骤: {step_summary} · 耗时: {elapsed_str}"
            )
            if error and status != "completed":
                notif_msg += f"\n错误: {error[:200]}"

            await _create_notification(notif_msg)

        except Exception as e:
            logger.warning(
                "[%s] Failed to record workflow history: %s",
                self.name,
                e,
            )

    # ------------------------------------------------------------------ #
    #  Session helpers                                                     #
    # ------------------------------------------------------------------ #

    async def _judge_resume(self, message: AgentMessage, paused_ctx: dict) -> bool:
        """让 Supervisor 判断当前消息是否属于暂停中的对话的延续"""
        text = message.payload.get("text", "")
        pause_context = paused_ctx.get("pause_context", "")

        prompt = (
            f"用户有一个暂停中的对话（正在和 {paused_ctx['active_agent']} 交互）：\n"
            f"暂停上下文：{pause_context}\n\n"
            f"用户当前消息：{text}\n\n"
            f"判断这条消息是否属于暂停中的对话的延续。只回答 true 或 false。"
        )
        resp = await self._llm.chat(
            system="你是一个意图判断助手，只回答 true 或 false。",
            user=prompt,
            max_tokens=10,
            temperature=0.0,
        )
        return resp.strip().lower().startswith("true")

    async def _archive_paused_session(
        self, user_id: str, ctx: dict, session_mgr
    ) -> None:
        """超时后总结暂停会话的记忆，存入 L3，关闭会话"""
        agent_name = ctx.get("active_agent", "")
        pause_context = ctx.get("pause_context", "")

        if not pause_context:
            await session_mgr.clear_session_context(user_id)
            return

        # LLM 做一句话总结
        summary = await self._llm.chat(
            system="总结以下对话上下文为一句话。",
            user=pause_context,
            max_tokens=100,
            temperature=0.1,
        )

        # 存入 L3 长期记忆
        mi = MemoryInjector(user_id)
        await mi.write_l3(
            content=f"[历史对话摘要-{agent_name}] {summary}",
            memory_type="conversation_summary",
        )

        await session_mgr.clear_session_context(user_id)
        logger.info("[%s] Paused session archived for user %s", self.name, user_id)

    # ------------------------------------------------------------------ #
    #  Intent parsing                                                      #
    # ------------------------------------------------------------------ #

    async def _parse_intent(
        self, text: str, context: list | None = None, exclude_agents: list | None = None
    ) -> str | dict:
        """Delegate to IntentParser."""
        return await self._intent_parser.parse(text, context, exclude_agents)

    def _rule_based_intent(self, text: str) -> str | None:
        """Delegate to IntentParser."""
        return self._intent_parser._rule_based_intent(text)

    # ------------------------------------------------------------------ #
    #  Frame / self / free-chat execution                                  #
    # ------------------------------------------------------------------ #

    async def _handle_frame(self, intent: str, message: AgentMessage) -> AgentResult:
        frame_action = self._router.get_frame_intent(intent)
        if not frame_action:
            return AgentResult(
                task_id=message.task_id,
                success=False,
                error=f"Unknown frame intent: {intent}",
            )

        frame_type, action = frame_action
        try:
            if action == "start":
                await self._frame.start(frame_type)
            else:
                await self._frame.stop(frame_type)
            if message.user_id:
                mi = MemoryInjector(message.user_id)
                state = "running" if action == "start" else "stopped"
                await mi.write_l2(
                    content=f"{frame_type}:{state}",
                    memory_type="frame_state",
                    importance=2,
                )
            return AgentResult(
                task_id=message.task_id,
                success=True,
                data=f"{frame_type} 已{'启动' if action == 'start' else '停止'}",
            )
        except Exception as e:
            return AgentResult(task_id=message.task_id, success=False, error=str(e))

    async def _self_execute(
        self, message: AgentMessage, on_tool_result=None
    ) -> AgentResult:
        """Supervisor 使用自身工具执行任务。"""
        self._current_user_id = message.user_id or ""
        text = message.payload.get("text", "")
        tools = self._get_tools_schema()
        system = self._build_system_prompt_with_skills()
        messages = [{"role": "user", "content": text}]

        content, is_fb = await self._run_tool_loop(
            system, messages, tools, max_tokens=8192, on_tool_result=on_tool_result
        )

        if is_fb:
            return AgentResult(
                task_id=message.task_id,
                success=False,
                error="LLM暂时不可用，请稍后再试",
            )

        return AgentResult(task_id=message.task_id, success=True, data=content)

    async def _free_chat(
        self, message: AgentMessage, on_tool_result=None
    ) -> AgentResult:
        """未识别意图，LLM自由对话，支持工具调用"""
        self._current_user_id = message.user_id or ""
        text = message.payload.get("text", "")

        mi = MemoryInjector(message.user_id)
        conversation_history: list = []
        if mi.available:
            conversation_history = (await mi.get_conv_history())[-5:]

        if conversation_history:
            history_block = "\n".join(
                f"{t['role']}({t.get('agent', 'supervisor')}): {t['text']}"
                for t in conversation_history
            )
            user_prompt = f"[对话历史]\n{history_block}\n\n[当前消息]\n{text}"
        else:
            user_prompt = text

        system = self._build_system_prompt_with_skills()
        tools = self._get_tools_schema()
        messages = [{"role": "user", "content": user_prompt}]

        content, is_fb = await self._run_tool_loop(
            system, messages, tools, max_tokens=8192, on_tool_result=on_tool_result
        )

        if is_fb:
            return AgentResult(
                task_id=message.task_id,
                success=False,
                error="LLM暂时不可用，请稍后再试",
            )

        if mi.available:
            await mi.append_conv_history(
                [
                    {
                        "role": "user",
                        "agent": "supervisor",
                        "text": text,
                        "ts": int(time.time()),
                    },
                    {
                        "role": "agent",
                        "agent": "supervisor",
                        "text": content,
                        "ts": int(time.time()),
                    },
                ]
            )
            await mi.write_l2(
                content=f"user: {text}\nassistant: {content}",
                memory_type="conversation",
                importance=1,
            )

        return AgentResult(task_id=message.task_id, success=True, data=content)

    # ------------------------------------------------------------------ #
    #  Skills helpers                                                      #
    # ------------------------------------------------------------------ #

    def _load_skills_for_context(self, skill_names: list[str]) -> str:
        """
        Load full content for a list of skill names.

        Resolves references recursively and strips frontmatter.
        """
        if not skill_names:
            return ""
        loader = self._get_skills_loader()
        resolved = loader.resolve_references(skill_names)
        return loader.load_skills_content(resolved)

    def _get_skills_summary(self) -> str:
        """Get the skills inventory XML block for progressive loading."""
        # Rebuild summary on demand in case skills were added
        return self._get_skills_loader().build_summary()

    async def handle_text(self, text: str) -> str:
        """Channel收到自然语言文本的便捷入口"""
        msg = AgentMessage(
            sender="user", recipient="supervisor", payload={"text": text}
        )
        result = await self.handle(msg)
        if result.success:
            return str(result.data)
        return f"[错误] {result.error}"
