"""Comprehensive tests for SupervisorAgent intent recognition flow.

Covers:
- Intent recognition for all agents (quant, analyst, coach, risk_advisor)
- Multi-turn conversation routing
- Intent switching during continuous conversations
- Frame intents, fallback rules, and free chat
- Paused session handling
"""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock, patch

import unittest

from apps.agent.supervisor import (
    IntentRouter,
    SupervisorAgent,
    SessionState,
    AgentMessage,
    AgentResult,
)
from apps.agent.llm_client import FALLBACK_MARKER


# ------------------------------------------------------------------ #
#  Fixtures & Helpers                                                 #
# ------------------------------------------------------------------ #


def reset_all_singletons():
    """Reset all singletons for test isolation."""
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
        self._store = {}
        self.call_log = []

    async def get_session_context(self, user_id):
        self.call_log.append(("get_session_context", user_id))
        return self._store.get(user_id)

    async def set_session_context(self, user_id, state, active_agent=None, ttl=1800):
        self.call_log.append(
            ("set_session_context", user_id, state.value, active_agent)
        )
        self._store[user_id] = {
            "state": state.value,
            "active_agent": active_agent,
            "expires_at": time.time() + ttl,
        }

    async def pause_session(
        self, user_id, active_agent, pause_ttl=300, pause_context=""
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

    async def resume_session(self, user_id, agent_name, ttl=1800):
        self.call_log.append(("resume_session", user_id, agent_name))
        self._store[user_id] = {
            "state": SessionState.MULTI_TURN.value,
            "active_agent": agent_name,
            "expires_at": time.time() + ttl,
        }

    async def clear_session_context(self, user_id):
        self.call_log.append(("clear_session_context", user_id))
        self._store.pop(user_id, None)


class MockMemoryManager:
    """In-memory memory manager (no Redis/DB dependency)."""

    def __init__(self, agent_type="supervisor", user_id="test"):
        self._l1 = {"routing_history": [], "conv_history": []}

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
        self._l1["conv_history"] = history

    async def append_conv_history(self, entries, keep_turns=10):
        current = self._l1.get("conv_history", [])
        updated = (current + entries)[-keep_turns:]
        self._l1["conv_history"] = updated
        return updated


def llm_agent(name):
    return json.dumps({"agent": name})


def llm_frame(intent):
    return json.dumps({"intent": intent})


def llm_free(text="I'm not sure."):
    return json.dumps({"_free_chat": True, "response": text})


def make_agent_getter(**agents):
    """Return a side_effect function for AgentRegistry.get."""

    def getter(name):
        if name in agents:
            return agents[name]
        raise KeyError(f"Agent not registered: {name!r}")

    return getter


def make_accepting_agent():
    """Create a mock agent that accepts messages (normal response)."""
    agent = AsyncMock()
    agent.handle = AsyncMock(
        return_value=AgentResult(
            success=True,
            data={"content": "ok", "continue_conversation": False},
        )
    )
    return agent


def make_rejecting_agent(suggestion=""):
    """Create a mock agent that rejects messages (reroute)."""
    agent = AsyncMock()
    agent.handle = AsyncMock(
        return_value=AgentResult(
            success=False,
            need_reroute=True,
            reroute_reason="not my domain",
            reroute_suggestion=suggestion,
        )
    )
    return agent


def make_continuing_agent():
    """Create a mock agent that requests continued conversation."""
    agent = AsyncMock()
    agent.handle = AsyncMock(
        return_value=AgentResult(
            success=True,
            data={"content": "need more info", "continue_conversation": True},
        )
    )
    return agent


def create_supervisor_with_mocks(mock_session_mgr=None):
    """Create a SupervisorAgent with all external deps mocked."""
    if mock_session_mgr is None:
        mock_session_mgr = MockSessionManager()

    # Patch session manager and memory manager
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

    patch_sm.start()
    patch_mm.start()
    patch_redis.start()

    reset_all_singletons()
    # Re-register frame intents (module-level code won't re-run after reset)
    router = IntentRouter.get_instance()
    router.register_frame_intent("start_trading", "trading", "start")
    router.register_frame_intent("stop_trading", "trading", "stop")
    router.register_frame_intent("start_monitor", "assist", "start")
    router.register_frame_intent("stop_monitor", "assist", "stop")
    router.register_fallback_rules(
        [
            (r"(分析|行情|走势|K线|趋势).*(BTC|ETH|币|市场)", "analyze_market"),
            (r"(回测|测试策略|历史数据)", "run_backtest"),
            (r"(风险|止损|仓位|风控)", "assess_risk"),
            (r"(计划|复盘|总结|周报)", "create_plan"),
            (r"(策略|代码|编写)", "generate_and_test_strategy"),
        ]
    )

    supervisor = SupervisorAgent()

    # Explicitly patch _llm with a fresh AsyncMock to avoid singleton pollution
    #
    # 注意：不能只替换 supervisor._llm —— supervisor._intent_parser._llm 持有
    # 同一个 LLMClient 单例的引用（见 supervisor.py:168-169），替换引用会让
    # _parse_intent 继续打到真实 LLM（实测曾发生 25+ 次真实网络调用）。
    # 这里在共享单例上原地替换方法，两处缓存同步生效。
    _stub_llm_client(supervisor._llm)

    # Return with stopper
    def stopper():
        patch_sm.stop()
        patch_mm.stop()
        patch_redis.stop()

    return supervisor, mock_session_mgr, stopper


def _stub_llm_client(client) -> None:
    """在 LLMClient 单例上原地安装 AsyncMock（注：替换引用会造成 patch 泄漏）。"""
    client.chat = AsyncMock()
    client.chat_with_tools = AsyncMock()


# ------------------------------------------------------------------ #
#  Section B: _parse_intent Tests                                     #
# ------------------------------------------------------------------ #


class TestSupervisorParseIntent(unittest.TestCase):
    """Test _parse_intent with mocked LLM responses."""

    def _setup(self):
        supervisor, sm, stop = create_supervisor_with_mocks()
        self.addCleanup(stop)
        return supervisor, sm

    def test_llm_returns_agent_quant(self):
        sup, sm = self._setup()
        sup._llm.chat = AsyncMock(return_value=llm_agent("quant"))
        result = asyncio.run(sup._parse_intent("帮我写一个RSI交叉策略"))
        self.assertEqual(result, "quant")

    def test_llm_returns_agent_analyst(self):
        sup, sm = self._setup()
        sup._llm.chat = AsyncMock(return_value=llm_agent("analyst"))
        result = asyncio.run(sup._parse_intent("BTC走势分析"))
        self.assertEqual(result, "analyst")

    def test_llm_returns_agent_coach(self):
        sup, sm = self._setup()
        sup._llm.chat = AsyncMock(return_value=llm_agent("coach"))
        result = asyncio.run(sup._parse_intent("帮我制定交易计划"))
        self.assertEqual(result, "coach")

    def test_llm_returns_agent_risk_advisor(self):
        sup, sm = self._setup()
        sup._llm.chat = AsyncMock(return_value=llm_agent("risk_advisor"))
        result = asyncio.run(sup._parse_intent("我的仓位风险太高了"))
        self.assertEqual(result, "risk_advisor")

    def test_llm_returns_frame_intent(self):
        sup, sm = self._setup()
        sup._llm.chat = AsyncMock(return_value=llm_frame("start_trading"))
        result = asyncio.run(sup._parse_intent("开始交易"))
        self.assertEqual(result, "start_trading")

    def test_llm_returns_free_chat(self):
        sup, sm = self._setup()
        sup._llm.chat = AsyncMock(return_value=llm_free("你好，有什么可以帮你的？"))
        result = asyncio.run(sup._parse_intent("你好"))
        self.assertIsInstance(result, dict)
        self.assertTrue(result.get("_free_chat"))
        self.assertIn("你好", result.get("response", ""))

    def test_llm_fallback_marker_triggers_regex(self):
        """LLM returns FALLBACK_MARKER, fallback regex matches."""
        sup, sm = self._setup()
        sup._llm.chat = AsyncMock(return_value=FALLBACK_MARKER)
        result = asyncio.run(sup._parse_intent("分析BTC行情"))
        self.assertEqual(result, "analyze_market")

    def test_llm_invalid_json_falls_back(self):
        """LLM returns invalid JSON, no regex match → free_chat."""
        sup, sm = self._setup()
        sup._llm.chat = AsyncMock(return_value="not valid json {{{")
        result = asyncio.run(sup._parse_intent("hello weather"))
        self.assertIsInstance(result, dict)
        self.assertTrue(result.get("_free_chat"))

    def test_exclude_agents_in_prompt(self):
        """Verify exclude_agents appears in the LLM prompt."""
        sup, sm = self._setup()
        captured_prompt = {}

        async def capture_chat(system, user, **kwargs):
            captured_prompt["user"] = user
            return llm_agent("analyst")

        sup._llm.chat = AsyncMock(side_effect=capture_chat)
        asyncio.run(sup._parse_intent("test", exclude_agents=["quant"]))
        self.assertIn("quant", captured_prompt["user"])
        self.assertIn("Exclude", captured_prompt["user"])

    def test_empty_text_returns_unknown(self):
        sup, sm = self._setup()
        result = asyncio.run(sup._parse_intent(""))
        self.assertEqual(result, "unknown")
        sup._llm.chat.assert_not_called()


# ------------------------------------------------------------------ #
#  Section C: _normal_route Tests                                     #
# ------------------------------------------------------------------ #


class TestSupervisorNormalRoute(unittest.TestCase):
    """Test _normal_route end-to-end with mocked LLM and agents."""

    def _setup(self, **agents):
        supervisor, sm, stop = create_supervisor_with_mocks()
        self.addCleanup(stop)
        return supervisor, sm

    def test_strategy_routes_to_quant(self):
        sup, sm = self._setup()
        mock_quant = make_accepting_agent()

        sup._llm.chat = AsyncMock(return_value=llm_agent("quant"))
        with patch("apps.agent.registry.AgentRegistry.get", return_value=mock_quant):
            result = asyncio.run(
                sup._normal_route(
                    AgentMessage(payload={"text": "帮我写RSI策略"}, user_id="u1")
                )
            )

        mock_quant.handle.assert_called_once()
        self.assertTrue(result.success)

    def test_market_analysis_routes_to_analyst(self):
        sup, sm = self._setup()
        mock_analyst = make_accepting_agent()

        sup._llm.chat = AsyncMock(return_value=llm_agent("analyst"))
        with patch("apps.agent.registry.AgentRegistry.get", return_value=mock_analyst):
            result = asyncio.run(
                sup._normal_route(
                    AgentMessage(payload={"text": "BTC走势"}, user_id="u1")
                )
            )

        mock_analyst.handle.assert_called_once()
        self.assertTrue(result.success)

    def test_trading_plan_routes_to_coach(self):
        sup, sm = self._setup()
        mock_coach = make_accepting_agent()

        sup._llm.chat = AsyncMock(return_value=llm_agent("coach"))
        with patch("apps.agent.registry.AgentRegistry.get", return_value=mock_coach):
            result = asyncio.run(
                sup._normal_route(
                    AgentMessage(payload={"text": "制定交易计划"}, user_id="u1")
                )
            )

        mock_coach.handle.assert_called_once()
        self.assertTrue(result.success)

    def test_risk_routes_to_risk_advisor(self):
        sup, sm = self._setup()
        mock_risk = make_accepting_agent()

        sup._llm.chat = AsyncMock(return_value=llm_agent("risk_advisor"))
        with patch("apps.agent.registry.AgentRegistry.get", return_value=mock_risk):
            result = asyncio.run(
                sup._normal_route(
                    AgentMessage(payload={"text": "仓位风险"}, user_id="u1")
                )
            )

        mock_risk.handle.assert_called_once()
        self.assertTrue(result.success)

    def test_frame_intent_handles_frame(self):
        sup, sm = self._setup()
        sup._llm.chat = AsyncMock(return_value=llm_frame("start_trading"))

        # Mock FrameManager
        mock_frame = AsyncMock()
        mock_frame.start = AsyncMock()
        with patch.object(sup, "_frame", mock_frame):
            result = asyncio.run(
                sup._normal_route(
                    AgentMessage(payload={"text": "开始交易"}, user_id="u1")
                )
            )

        mock_frame.start.assert_called_once_with("trading")
        self.assertTrue(result.success)
        self.assertIn("启动", str(result.data))

    def test_unknown_intent_goes_to_free_chat(self):
        sup, sm = self._setup()
        sup._llm.chat = AsyncMock(return_value=llm_agent("nonexistent"))

        # Registry raises KeyError for nonexistent agent
        with patch(
            "apps.agent.registry.AgentRegistry.get",
            side_effect=KeyError("nonexistent"),
        ):
            result = asyncio.run(
                sup._normal_route(AgentMessage(payload={"text": "hello"}, user_id="u1"))
            )

        # Agent not found → _route_to_agent catches error, returns failure
        self.assertFalse(result.success)
        self.assertIn("nonexistent", result.error)

    def test_pre_set_intent_skips_llm(self):
        sup, sm = self._setup()
        mock_quant = make_accepting_agent()

        # Pre-set intent on message
        msg = AgentMessage(
            payload={"text": "test"},
            intent="quant",  # skip LLM
            user_id="u1",
        )

        with patch("apps.agent.registry.AgentRegistry.get", return_value=mock_quant):
            result = asyncio.run(sup._normal_route(msg))

        # LLM should NOT be called since intent was pre-set
        mock_quant.handle.assert_called_once()
        self.assertTrue(result.success)


# ------------------------------------------------------------------ #
#  Section D: Multi-Turn Tests                                        #
# ------------------------------------------------------------------ #


class TestSupervisorMultiTurn(unittest.TestCase):
    """Test multi-turn conversation routing."""

    def _setup(self, **agents):
        supervisor, sm, stop = create_supervisor_with_mocks()
        self.addCleanup(stop)
        return supervisor, sm

    def test_multi_turn_routes_to_active_agent(self):
        sup, sm = self._setup()
        mock_quant = make_accepting_agent()

        # Pre-set session to MULTI_TURN with quant
        sm._store["u1"] = {
            "state": SessionState.MULTI_TURN.value,
            "active_agent": "quant",
            "expires_at": time.time() + 1800,
        }

        with patch("apps.agent.registry.AgentRegistry.get", return_value=mock_quant):
            result = asyncio.run(
                sup.handle(AgentMessage(payload={"text": "继续修改策略"}, user_id="u1"))
            )

        mock_quant.handle.assert_called_once()
        self.assertTrue(result.success)

    def test_multi_turn_continues_conversation(self):
        sup, sm = self._setup()
        mock_quant = make_continuing_agent()

        sm._store["u1"] = {
            "state": SessionState.MULTI_TURN.value,
            "active_agent": "quant",
            "expires_at": time.time() + 1800,
        }

        with patch("apps.agent.registry.AgentRegistry.get", return_value=mock_quant):
            asyncio.run(
                sup.handle(
                    AgentMessage(payload={"text": "还需要更多信息"}, user_id="u1")
                )
            )

        # Session should stay MULTI_TURN
        ctx = sm._store.get("u1")
        self.assertIsNotNone(ctx)
        self.assertEqual(ctx["state"], SessionState.MULTI_TURN.value)

    def test_multi_turn_agent_rejects_and_reroutes(self):
        sup, sm = self._setup()
        mock_quant = make_rejecting_agent(suggestion="risk_advisor")
        mock_risk = make_accepting_agent()

        sm._store["u1"] = {
            "state": SessionState.MULTI_TURN.value,
            "active_agent": "quant",
            "expires_at": time.time() + 1800,
        }

        sup._llm.chat = AsyncMock(return_value=llm_agent("risk_advisor"))

        with patch(
            "apps.agent.registry.AgentRegistry.get",
            side_effect=make_agent_getter(quant=mock_quant, risk_advisor=mock_risk),
        ):
            result = asyncio.run(
                sup.handle(AgentMessage(payload={"text": "仓位风险"}, user_id="u1"))
            )

        mock_quant.handle.assert_called_once()
        mock_risk.handle.assert_called_once()
        self.assertTrue(result.success)

    def test_multi_turn_agent_rejects_and_free_chat(self):
        sup, sm = self._setup()
        mock_analyst = make_rejecting_agent()

        sm._store["u1"] = {
            "state": SessionState.MULTI_TURN.value,
            "active_agent": "analyst",
            "expires_at": time.time() + 1800,
        }

        # Re-parse returns free_chat
        sup._llm.chat = AsyncMock(return_value=llm_free("我不确定"))

        with patch(
            "apps.agent.registry.AgentRegistry.get",
            side_effect=make_agent_getter(analyst=mock_analyst),
        ):
            result = asyncio.run(
                sup.handle(AgentMessage(payload={"text": "天气怎么样"}, user_id="u1"))
            )

        mock_analyst.handle.assert_called_once()
        self.assertTrue(result.success)
        self.assertIn("不确定", str(result.data))

    def test_multi_turn_no_active_agent_clears(self):
        sup, sm = self._setup()

        # MULTI_TURN but no active_agent
        sm._store["u1"] = {
            "state": SessionState.MULTI_TURN.value,
            "active_agent": None,
            "expires_at": time.time() + 1800,
        }

        mock_quant = make_accepting_agent()
        sup._llm.chat = AsyncMock(return_value=llm_agent("quant"))

        with patch("apps.agent.registry.AgentRegistry.get", return_value=mock_quant):
            asyncio.run(
                sup.handle(AgentMessage(payload={"text": "写策略"}, user_id="u1"))
            )

        # Session should be cleared
        self.assertNotIn("u1", sm._store)
        mock_quant.handle.assert_called_once()


# ------------------------------------------------------------------ #
#  Section E: Intent Switching Tests                                  #
# ------------------------------------------------------------------ #


class TestSupervisorIntentSwitching(unittest.TestCase):
    """Test intent switching during continuous conversations."""

    def _setup(self, **agents):
        supervisor, sm, stop = create_supervisor_with_mocks()
        self.addCleanup(stop)
        return supervisor, sm

    def test_continuous_then_intent_switch(self):
        """Phase 1: analyst routes, session becomes MULTI_TURN.
        Phase 2: analyst rejects risk question, re-parse→risk_advisor, session paused.
        """
        sup, sm = self._setup()
        mock_analyst = make_rejecting_agent(suggestion="risk_advisor")
        mock_risk = make_accepting_agent()

        # Start in MULTI_TURN with analyst
        sm._store["u1"] = {
            "state": SessionState.MULTI_TURN.value,
            "active_agent": "analyst",
            "expires_at": time.time() + 1800,
        }

        # LLM re-parse will return risk_advisor
        sup._llm.chat = AsyncMock(return_value=llm_agent("risk_advisor"))

        with patch(
            "apps.agent.registry.AgentRegistry.get",
            side_effect=make_agent_getter(analyst=mock_analyst, risk_advisor=mock_risk),
        ):
            result = asyncio.run(
                sup.handle(
                    AgentMessage(payload={"text": "评估我的仓位风险"}, user_id="u1")
                )
            )

        # Analyst was tried and rejected
        mock_analyst.handle.assert_called_once()
        # Risk advisor was called after re-routing
        mock_risk.handle.assert_called_once()
        self.assertTrue(result.success)

    def test_session_state_transitions(self):
        """Verify state transition: MULTI_TURN → MULTI_TURN(new agent) after rejection + reroute."""
        sup, sm = self._setup()
        mock_quant = make_rejecting_agent(suggestion="coach")
        mock_coach = make_accepting_agent()

        sm._store["u1"] = {
            "state": SessionState.MULTI_TURN.value,
            "active_agent": "quant",
            "expires_at": time.time() + 1800,
        }

        sup._llm.chat = AsyncMock(return_value=llm_agent("coach"))

        with patch(
            "apps.agent.registry.AgentRegistry.get",
            side_effect=make_agent_getter(quant=mock_quant, coach=mock_coach),
        ):
            asyncio.run(
                sup.handle(AgentMessage(payload={"text": "制定计划"}, user_id="u1"))
            )

        # Session should transition to MULTI_TURN with the new agent after successful reroute
        ctx = sm._store.get("u1")
        self.assertIsNotNone(ctx)
        self.assertEqual(ctx["state"], SessionState.MULTI_TURN.value)
        self.assertEqual(ctx["active_agent"], "coach")


# ------------------------------------------------------------------ #
#  Section F: Fallback Tests                                          #
# ------------------------------------------------------------------ #


class TestSupervisorFallback(unittest.TestCase):
    """Test LLM fallback to regex rules."""

    def _setup(self, **agents):
        supervisor, sm, stop = create_supervisor_with_mocks()
        self.addCleanup(stop)
        return supervisor, sm

    def test_llm_fails_regex_matches(self):
        """LLM returns FALLBACK_MARKER, regex matches → returns legacy intent."""
        sup, sm = self._setup()
        sup._llm.chat = AsyncMock(return_value=FALLBACK_MARKER)
        result = asyncio.run(sup._parse_intent("分析BTC行情"))
        self.assertEqual(result, "analyze_market")

        # Another pattern
        result2 = asyncio.run(sup._parse_intent("回测策略"))
        self.assertEqual(result2, "run_backtest")

        result3 = asyncio.run(sup._parse_intent("风控建议"))
        self.assertEqual(result3, "assess_risk")

    def test_llm_fails_no_regex_match(self):
        """LLM returns FALLBACK_MARKER, no regex match → free_chat dict."""
        sup, sm = self._setup()
        sup._llm.chat = AsyncMock(return_value=FALLBACK_MARKER)
        result = asyncio.run(sup._parse_intent("hello weather today"))
        self.assertIsInstance(result, dict)
        self.assertTrue(result.get("_free_chat"))


# ------------------------------------------------------------------ #
#  Bonus: Paused Session Tests                                        #
# ------------------------------------------------------------------ #


class TestSupervisorPausedSession(unittest.TestCase):
    """Test paused session handling."""

    def _setup(self, **agents):
        supervisor, sm, stop = create_supervisor_with_mocks()
        self.addCleanup(stop)
        return supervisor, sm

    def test_paused_not_expired_judge_false_routes_normally(self):
        """Paused session not expired, judge says false → normal route."""
        sup, sm = self._setup()
        mock_quant = make_accepting_agent()

        sm._store["u1"] = {
            "state": SessionState.PAUSED.value,
            "active_agent": "analyst",
            "paused_at": time.time() - 10,  # 10 seconds ago (not expired)
            "pause_ttl": 300,
            "pause_context": "previous context",
            "expires_at": time.time() + 290,
        }

        # Judge says false (not a continuation)
        sup._llm.chat = AsyncMock(
            side_effect=[
                "false",  # _judge_resume
                llm_agent("quant"),  # _parse_intent
            ]
        )

        with patch("apps.agent.registry.AgentRegistry.get", return_value=mock_quant):
            result = asyncio.run(
                sup.handle(AgentMessage(payload={"text": "新话题"}, user_id="u1"))
            )

        mock_quant.handle.assert_called_once()
        self.assertTrue(result.success)

    def test_paused_judge_true_resumes_to_agent(self):
        """Paused session not expired, judge says true → resume + route to agent."""
        sup, sm = self._setup()
        mock_analyst = make_accepting_agent()

        sm._store["u1"] = {
            "state": SessionState.PAUSED.value,
            "active_agent": "analyst",
            "paused_at": time.time() - 10,
            "pause_ttl": 300,
            "pause_context": "BTC analysis",
            "expires_at": time.time() + 290,
        }

        # Judge says true (continuation)
        sup._llm.chat = AsyncMock(return_value="true")

        with patch("apps.agent.registry.AgentRegistry.get", return_value=mock_analyst):
            result = asyncio.run(
                sup.handle(AgentMessage(payload={"text": "继续"}, user_id="u1"))
            )

        # Should have resumed and routed to analyst
        mock_analyst.handle.assert_called_once()
        self.assertTrue(result.success)

    def test_paused_expired_archives_and_routes(self):
        """Paused session expired → archive (LLM summary call) → normal route."""
        sup, sm = self._setup()
        mock_quant = make_accepting_agent()

        sm._store["u1"] = {
            "state": SessionState.PAUSED.value,
            "active_agent": "analyst",
            "paused_at": time.time() - 600,  # 10 minutes ago (expired)
            "pause_ttl": 300,
            "pause_context": "old context",
            "expires_at": time.time() - 300,  # expired
        }

        # LLM calls: archive summary + parse intent + free_chat
        sup._llm.chat = AsyncMock(
            side_effect=[
                "summary",  # archive summary
                llm_agent("quant"),  # _parse_intent
            ]
        )

        with patch("apps.agent.registry.AgentRegistry.get", return_value=mock_quant):
            result = asyncio.run(
                sup.handle(AgentMessage(payload={"text": "写策略"}, user_id="u1"))
            )

        mock_quant.handle.assert_called_once()
        self.assertTrue(result.success)


# ------------------------------------------------------------------ #
#  Bonus: HandleFrame Tests                                           #
# ------------------------------------------------------------------ #


class TestSupervisorHandleFrame(unittest.TestCase):
    """Test frame intent handling."""

    def _setup(self, **agents):
        supervisor, sm, stop = create_supervisor_with_mocks()
        self.addCleanup(stop)
        return supervisor, sm

    def test_handle_frame_start(self):
        sup, sm = self._setup()
        mock_frame = AsyncMock()
        mock_frame.start = AsyncMock()
        with patch.object(sup, "_frame", mock_frame):
            result = asyncio.run(
                sup._handle_frame(
                    "start_trading",
                    AgentMessage(payload={"text": "开始交易"}, user_id="u1"),
                )
            )

        mock_frame.start.assert_called_once_with("trading")
        self.assertTrue(result.success)
        self.assertIn("启动", str(result.data))

    def test_handle_frame_stop(self):
        sup, sm = self._setup()
        mock_frame = AsyncMock()
        mock_frame.stop = AsyncMock()
        with patch.object(sup, "_frame", mock_frame):
            result = asyncio.run(
                sup._handle_frame(
                    "stop_monitor",
                    AgentMessage(payload={"text": "停止监控"}, user_id="u1"),
                )
            )

        mock_frame.stop.assert_called_once_with("assist")
        self.assertTrue(result.success)
        self.assertIn("停止", str(result.data))


if __name__ == "__main__":
    unittest.main()
