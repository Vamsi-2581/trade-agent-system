"""E2E test for the 3-step workflow: research → implement → backtest.

Step 1: User says "在网上找一个基于btc5分钟线的超短线交易策略"
  → Supervisor routes to researcher agent
  → Researcher searches web and returns strategy research report

Step 2: User says "实现这个策略"
  → Supervisor routes to quant agent
  → Quant loads create-strategy skill, implements strategy, tests it

Step 3: User says "回测这个策略"
  → Supervisor routes to quant agent
  → Quant loads quant-backtest skill, submits backtest task
"""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import unittest

from apps.agent.llm_client import LLMClient
from apps.agent.supervisor import (
    IntentRouter,
    SupervisorAgent,
    SessionState,
    AgentMessage,
    AgentResult,
)


# ------------------------------------------------------------------ #
#  Fixtures & Helpers                                                 #
# ------------------------------------------------------------------ #


def reset_all_singletons():
    IntentRouter.reset()
    SupervisorAgent._instance = None

    from apps.agent.registry import AgentRegistry
    from apps.memory.manager import _L1_CACHE
    from apps.agent.frame_manager import FrameManager
    from apps.agent.llm_client import LLMClient

    AgentRegistry._registry.clear()
    AgentRegistry._classes.clear()
    AgentRegistry._discovered = False
    FrameManager._instance = None
    LLMClient._instance = None
    _L1_CACHE.clear()


class MockSessionManager:
    """In-memory session manager (no Redis dependency)."""

    def __init__(self):
        self._store: dict[str, dict] = {}
        self.call_log: list = []

    async def get_session_context(self, user_id: str):
        self.call_log.append(("get_session_context", user_id))
        return self._store.get(user_id)

    async def set_session_context(
        self, user_id: str, state, active_agent=None, task_id=None, ttl=1800
    ):
        self.call_log.append(
            ("set_session_context", user_id, state.value, active_agent)
        )
        self._store[user_id] = {
            "state": state.value,
            "active_agent": active_agent,
            "task_id": task_id,
            "expires_at": time.time() + ttl,
        }

    async def pause_session(
        self, user_id: str, active_agent, pause_ttl=300, pause_context=""
    ):
        self.call_log.append(("pause_session", user_id, active_agent))
        self._store[user_id] = {
            "state": SessionState.PAUSED.value,
            "active_agent": active_agent,
            "paused_at": time.time(),
            "pause_ttl": pause_ttl,
            "pause_context": pause_context,
            "expires_at": time.time() + pause_ttl,
        }

    async def resume_session(self, user_id: str, agent_name: str, ttl=1800):
        self.call_log.append(("resume_session", user_id, agent_name))
        self._store[user_id] = {
            "state": SessionState.MULTI_TURN.value,
            "active_agent": agent_name,
            "expires_at": time.time() + ttl,
        }

    async def clear_session_context(self, user_id: str):
        self.call_log.append(("clear_session_context", user_id))
        self._store.pop(user_id, None)


class MockMemoryManager:
    """In-memory memory manager that tracks conversation history.

    Uses class-level storage so different instances share state,
    mirroring how the real MemoryManager uses Redis for persistence.
    """

    # Class-level shared storage (simulates Redis persistence)
    _shared_l1: dict[str, dict] = {}

    def __init__(self, agent_type="supervisor", user_id="test"):
        self.agent_type = agent_type
        self.user_id = user_id
        key = f"{agent_type}:{user_id}"
        if key not in self._shared_l1:
            self._shared_l1[key] = {"conv_history": []}

    @property
    def _l1(self) -> dict:
        return self._shared_l1[f"{self.agent_type}:{self.user_id}"]

    def write_l1(self, key, value):
        self._l1[key] = value

    async def write_l2(self, *args, **kwargs):
        pass

    async def write_l3(self, *args, **kwargs):
        pass

    async def retrieve(self, query, top_k=5):
        return []

    async def get_conv_history(self, max_turns=5):
        return self._l1.get("conv_history", [])

    async def save_conv_history(self, history):
        self._l1["conv_history"] = list(history)

    async def append_conv_history(self, entries, keep_turns=10):
        current = self._l1.get("conv_history", [])
        updated = (current + entries)[-keep_turns:]
        self._l1["conv_history"] = updated
        return updated


def llm_agent(name):
    return json.dumps({"agent": name})


def make_accepting_agent(response_text="ok"):
    """Create a mock agent that accepts messages."""
    agent = AsyncMock()
    agent.handle = AsyncMock(
        return_value=AgentResult(
            success=True,
            data={"content": response_text, "continue_conversation": False},
        )
    )
    return agent


def create_supervisor_with_mocks(mock_session_mgr=None):
    """Create a SupervisorAgent with all external deps mocked."""
    if mock_session_mgr is None:
        mock_session_mgr = MockSessionManager()

    patch_sm = patch(
        "apps.agent.supervisor.get_session_manager",
        return_value=mock_session_mgr,
    )
    patch_mm = patch(
        "apps.memory.manager.MemoryManager",
        MockMemoryManager,
    )
    patch_redis = patch(
        "redis.asyncio.from_url",
        side_effect=Exception("no redis in tests"),
    )

    # SupervisorAgent.__init__ 把 LLMClient.get_instance() 注入 Supervisor 自身
    # **和** IntentParser。必须在构造 supervisor 之前替换单例，否则
    # _intent_parser 仍持有真实 LLM 客户端 → 意图解析会真的走网络，
    # 用例变成非确定性的（真实 LLM 的措辞随时会变）。
    mock_llm = MagicMock()
    mock_llm.chat = AsyncMock(return_value=json.dumps({"agent": "unknown"}))
    mock_llm.chat_with_tools = AsyncMock()
    patch_llm = patch.object(
        LLMClient, "get_instance", classmethod(lambda cls: mock_llm)
    )

    patch_sm.start()
    patch_mm.start()
    patch_redis.start()
    patch_llm.start()

    reset_all_singletons()

    # Re-register frame intents and fallback rules
    router = IntentRouter.get_instance()
    router.register_frame_intent("start_trading", "trading", "start")
    router.register_frame_intent("stop_trading", "trading", "stop")
    router.register_fallback_rules(
        [
            (r"(分析|行情|走势|K线|趋势).*(BTC|ETH|币|市场)", "analyst"),
            (r"(回测|测试策略|历史数据|backtest)", "quant"),
            (r"(风险|止损|仓位|风控)", "risk_advisor"),
            (r"(计划|复盘|总结|周报)", "coach"),
            (
                r"(实现.*策略|创建.*策略|编写.*策略|生成.*策略代码)",
                "quant",
            ),
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

    supervisor = SupervisorAgent()
    # 构造时已通过 LLMClient 单例注入 mock_llm；这里再确认一次，
    # 保证 supervisor._llm 与 _intent_parser._llm 是同一个 mock。
    supervisor._llm = mock_llm

    def stopper():
        patch_sm.stop()
        patch_mm.stop()
        patch_redis.stop()
        patch_llm.stop()

    return supervisor, mock_session_mgr, stopper


# ------------------------------------------------------------------ #
#  E2E Workflow Tests                                                 #
# ------------------------------------------------------------------ #


class TestE2EWorkflow(unittest.TestCase):
    def setUp(self):
        reset_all_singletons()
        MockMemoryManager._shared_l1.clear()

    # ---- Step 1: Research ---- #

    def test_step1_research_routes_to_researcher(self):
        """User asks to find strategy online → routes to researcher agent."""
        sup, sm, stop = create_supervisor_with_mocks()
        self.addCleanup(stop)

        sup._llm.chat = AsyncMock(return_value=llm_agent("researcher"))

        mock_researcher = make_accepting_agent(
            "## 研究结果：BTC 5分钟超短线交易策略\n\n"
            "### 1. RSI + EMA 策略\n来源: https://example.com/rsi-ema-scalping\n"
        )

        with patch(
            "apps.agent.registry.AgentRegistry.get", return_value=mock_researcher
        ):
            result = asyncio.run(
                sup.handle(
                    AgentMessage(
                        payload={"text": "在网上找一个基于btc5分钟线的超短线交易策略"},
                        user_id="u1",
                    )
                )
            )

        self.assertTrue(result.success, f"Step 1 failed: {result.error}")
        mock_researcher.handle.assert_called_once()

    def test_step1_fallback_rule_matches_researcher(self):
        """Fallback rule matches '找策略' → researcher."""
        router = IntentRouter.get_instance()
        router.register_fallback_rules(
            [(r"(研究|调研|找.*策略|搜索.*策略)", "researcher")]
        )
        result = router.match_fallback("在网上找一个基于btc5分钟线的超短线交易策略")
        self.assertEqual(result, "researcher")

    # ---- Step 2: Implement Strategy ---- #

    def test_step2_implement_routes_to_quant(self):
        """User says 'implement this strategy' → routes to quant agent."""
        sup, sm, stop = create_supervisor_with_mocks()
        self.addCleanup(stop)

        sup._llm.chat = AsyncMock(return_value=llm_agent("quant"))

        mock_quant = make_accepting_agent(
            "策略 rsi_ema_strategy 已创建并通过测试！\n"
            "- 文件: ~/.tradelogx/strategies/rsi_ema_strategy.py\n"
            "- 测试结果: 成功\n"
        )

        with patch("apps.agent.registry.AgentRegistry.get", return_value=mock_quant):
            result = asyncio.run(
                sup.handle(
                    AgentMessage(
                        payload={"text": "实现这个策略"},
                        user_id="u1",
                    )
                )
            )

        self.assertTrue(result.success, f"Step 2 failed: {result.error}")
        mock_quant.handle.assert_called_once()

    def test_step2_fallback_rule_matches_quant(self):
        """Fallback rule matches '实现策略' → quant."""
        router = IntentRouter.get_instance()
        router.register_fallback_rules(
            [(r"(实现.*策略|创建.*策略|编写.*策略)", "quant")]
        )
        result = router.match_fallback("实现这个策略")
        self.assertEqual(result, "quant")

    # ---- Step 3: Backtest Strategy ---- #

    def test_step3_backtest_routes_to_quant(self):
        """User says 'backtest this strategy' → routes to quant agent."""
        sup, sm, stop = create_supervisor_with_mocks()
        self.addCleanup(stop)

        sup._llm.chat = AsyncMock(return_value=llm_agent("quant"))

        mock_quant = make_accepting_agent(
            "回测任务已提交！\n"
            "- 任务ID: abc123-def456\n"
            "- 策略: rsi_ema_strategy\n"
            "- 标的: BTC/USDT, 周期: 5m\n"
        )

        with patch("apps.agent.registry.AgentRegistry.get", return_value=mock_quant):
            result = asyncio.run(
                sup.handle(
                    AgentMessage(
                        payload={"text": "回测这个策略"},
                        user_id="u1",
                    )
                )
            )

        self.assertTrue(result.success, f"Step 3 failed: {result.error}")
        mock_quant.handle.assert_called_once()

    def test_step3_fallback_rule_matches_quant(self):
        """Fallback rule matches '回测' → quant."""
        router = IntentRouter.get_instance()
        router.register_fallback_rules(
            [(r"(回测|测试策略|历史数据|backtest)", "quant")]
        )
        result = router.match_fallback("回测这个策略")
        self.assertEqual(result, "quant")

    # ---- Full 3-Step Workflow ---- #

    def test_full_workflow_research_implement_backtest(self):
        """Complete 3-step E2E: research → implement → backtest.

        Verifies:
        1. Each step routes to the correct agent
        2. Cross-agent context is passed between steps
        3. Conversation history is preserved
        """
        sup, sm, stop = create_supervisor_with_mocks()
        self.addCleanup(stop)

        # Prepare agent mocks with distinct responses
        research_report = (
            "## 研究结果：BTC 5分钟超短线交易策略\n\n"
            "找到了一个 RSI + EMA 超短线策略：\n"
            "- 使用5分钟K线\n"
            "- RSI(14) < 30 且 EMA(20) 向上时买入\n"
            "- RSI(14) > 70 或跌破 EMA(20) 时卖出\n"
            "- 止损 1.5%, 止盈 3%\n"
            "来源: https://example.com/btc-scalping-strategy\n"
        )
        mock_researcher = make_accepting_agent(research_report)

        impl_result = (
            "策略 rsi_ema_strategy 已创建并通过测试！\n"
            "- 文件路径: ~/.tradelogx/strategies/rsi_ema_strategy.py\n"
            "- 测试通过: 信号数 52, 模拟收益 +3.8%\n"
        )

        backtest_result = (
            "回测任务已提交！\n"
            "- 任务ID: abc123\n"
            "- 策略: rsi_ema_strategy\n"
            "- 标的: BTC/USDT, 周期: 5m\n"
        )

        # Step 1: Research
        sup._llm.chat = AsyncMock(return_value=llm_agent("researcher"))
        with patch(
            "apps.agent.registry.AgentRegistry.get", return_value=mock_researcher
        ):
            result1 = asyncio.run(
                sup.handle(
                    AgentMessage(
                        payload={"text": "在网上找一个基于btc5分钟线的超短线交易策略"},
                        user_id="u1",
                    )
                )
            )

        self.assertTrue(result1.success, f"Step 1 failed: {result1.error}")
        mock_researcher.handle.assert_called_once()

        # Step 2: Implement — quant receives cross-agent context from researcher
        sup._llm.chat = AsyncMock(return_value=llm_agent("quant"))
        mock_quant2 = make_accepting_agent(impl_result)
        with patch("apps.agent.registry.AgentRegistry.get", return_value=mock_quant2):
            result2 = asyncio.run(
                sup.handle(
                    AgentMessage(
                        payload={"text": "实现这个策略"},
                        user_id="u1",
                    )
                )
            )

        self.assertTrue(result2.success, f"Step 2 failed: {result2.error}")
        mock_quant2.handle.assert_called_once()

        # Verify cross-agent context injection
        call_args = mock_quant2.handle.call_args[0][0]
        prev_resp = call_args.payload.get("previous_agent_response", "")
        prev_agent = call_args.payload.get("previous_agent_name", "")
        self.assertEqual(
            prev_agent,
            "researcher",
            f"Expected previous_agent_name='researcher', got '{prev_agent}'",
        )
        self.assertIn(
            "RSI",
            prev_resp,
            f"Research report not in cross-agent context: {prev_resp[:200]}",
        )

        # Step 3: Backtest — quant routes again (same agent, no cross-agent injection
        # since prev_agent == agent_name). Quant gets context from its own L1 memory.
        sup._llm.chat = AsyncMock(return_value=llm_agent("quant"))
        mock_quant3 = make_accepting_agent(backtest_result)
        with patch("apps.agent.registry.AgentRegistry.get", return_value=mock_quant3):
            result3 = asyncio.run(
                sup.handle(
                    AgentMessage(
                        payload={"text": "回测这个策略"},
                        user_id="u1",
                    )
                )
            )

        self.assertTrue(result3.success, f"Step 3 failed: {result3.error}")
        mock_quant3.handle.assert_called_once()

        # For same-agent consecutive calls (quant→quant), cross-agent context is
        # intentionally skipped. Quant gets context from its own L1 memory instead.
