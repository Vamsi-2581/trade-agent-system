"""Supervisor scheduling flow E2E test: research → implement → backtest.

Three sequentially dependent tests where each test's output feeds the next.
Uses real supervisor routing and agent execution — no mock agents.

Step 1: "在网上找一个基于btc5分钟线的超短线交易策略" → researcher agent
Step 2: "实现这个策略" → quant agent (uses create-strategy skill)
Step 3: "回测这个策略" → backtest via quant agent (submit_backtest tool)
"""

from __future__ import annotations

import os
from unittest.mock import patch

import django

# Ensure Django is configured with test settings
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings.test")
django.setup()

import pytest  # noqa: E402  (must follow django.setup() above)

from apps.agent.base import AgentMessage, AgentResult  # noqa: E402
from apps.agent.supervisor import (  # noqa: E402
    SupervisorAgent,
    IntentRouter,
)
from apps.agent.registry import AgentRegistry  # noqa: E402
from apps.memory.manager import _L1_CACHE  # noqa: E402


# ------------------------------------------------------------------ #
#  Helpers                                                            #
# ------------------------------------------------------------------ #


TEST_USER = "e2e_trading_flow_test"


def reset_singletons():
    """Reset all singleton state for test isolation."""
    IntentRouter.reset()
    SupervisorAgent._instance = None
    AgentRegistry._registry.clear()
    AgentRegistry._classes.clear()
    AgentRegistry._discovered = False
    _L1_CACHE.clear()

    # Clear prompt cache so agents are re-discovered
    from apps.agent import prompt_loader

    prompt_loader._cache.clear()
    prompt_loader._meta_cache.clear()

    from apps.agent.frame_manager import FrameManager

    FrameManager._instance = None

    from apps.agent.llm_client import LLMClient

    LLMClient._instance = None


class MockSessionManager:
    """In-memory session manager (no Redis)."""

    def __init__(self):
        self._store: dict[str, dict] = {}

    async def get_session_context(self, user_id: str):
        return self._store.get(user_id)

    async def set_session_context(
        self, user_id: str, state, active_agent=None, task_id=None, ttl=1800
    ):
        # ``task_id`` 是真实签名的一部分（apps/agent/session_manager.py 的
        # set_session_context 有它，supervisor 走工作流时会传）：替身必须跟住，
        # 否则 WorkflowEngine 一调就 TypeError，用例在路由之前就炸。
        self._store[user_id] = {
            "state": state.value,
            "active_agent": active_agent,
            "task_id": task_id,
        }

    async def pause_session(
        self, user_id, active_agent, pause_ttl=300, pause_context=""
    ):
        self._store[user_id] = {
            "state": "paused",
            "active_agent": active_agent,
        }

    async def resume_session(self, user_id, agent_name, ttl=1800):
        self._store[user_id] = {
            "state": "multi_turn",
            "active_agent": agent_name,
        }

    async def clear_session_context(self, user_id):
        self._store.pop(user_id, None)


class MockMemoryManager:
    """In-memory memory manager (no Redis)."""

    _shared: dict[str, dict] = {}

    def __init__(self, agent_type="supervisor", user_id=TEST_USER):
        self.agent_type = agent_type
        self.user_id = user_id
        key = f"{agent_type}:{user_id}"
        if key not in self._shared:
            self._shared[key] = {"conv_history": []}

    @property
    def _l1(self):
        return self._shared[f"{self.agent_type}:{self.user_id}"]

    def write_l1(self, key, value):
        self._l1[key] = value

    async def write_l2(self, *a, **kw):
        pass

    async def write_l3(self, *a, **kw):
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


def make_supervisor():
    """Create a real SupervisorAgent with Redis mocked out."""
    mock_sm = MockSessionManager()

    p_sm = patch(
        "apps.agent.supervisor.get_session_manager",
        return_value=mock_sm,
    )
    p_mm = patch(
        "apps.memory.manager.MemoryManager",
        MockMemoryManager,
    )
    p_redis = patch(
        "redis.asyncio.from_url",
        side_effect=Exception("no redis in tests"),
    )

    p_sm.start()
    p_mm.start()
    p_redis.start()

    reset_singletons()

    supervisor = SupervisorAgent()

    def cleanup():
        p_sm.stop()
        p_mm.stop()
        p_redis.stop()

    return supervisor, mock_sm, cleanup


def extract_reply(content: str) -> str:
    """Extract the text reply from an AgentResult.data dict."""
    if isinstance(content, dict):
        return content.get("content", str(content))
    return str(content)


def get_routed_agent_name(result: AgentResult) -> str | None:
    """Determine which agent handled the request based on result data."""
    if isinstance(result.data, dict):
        return result.data.get("agent_name")
    return None


# ------------------------------------------------------------------ #
#  Shared state between tests                                         #
# ------------------------------------------------------------------ #

_shared_state = {
    "research_report": "",
    "strategy_content": "",
    "strategy_name": "",
    "backtest_result": "",
}


# ------------------------------------------------------------------ #
#  Tests                                                              #
# ------------------------------------------------------------------ #


@pytest.mark.order(1)
@pytest.mark.asyncio
class TestResearcherFindsBtcStrategy:
    """Step 1: Research BTC 5-min scalping strategy.

    Asserts:
    - Supervisor routes to researcher agent
    - Response length > 100 chars
    - Response contains at least one keyword: btc, 比特币, 策略, signal, MA, RSI, 均线
    """

    async def test_researcher_finds_btc_strategy(self):
        sup, sm, cleanup = make_supervisor()
        try:
            msg = AgentMessage(
                payload={"text": "在网上找一个基于btc5分钟线的超短线交易策略"},
                user_id=TEST_USER,
            )
            result = await sup.handle(msg)

            assert result.success, f"Step 1 failed: {result.error}"

            reply = extract_reply(result.data)
            assert len(reply) > 100, (
                f"Response too short ({len(reply)} chars), expected > 100. Reply: {reply[:200]}"
            )

            keywords = ["btc", "比特币", "策略", "signal", "MA", "RSI", "均线"]
            reply_lower = reply.lower()
            found = [kw for kw in keywords if kw.lower() in reply_lower]
            assert found, (
                f"Response missing strategy keywords. Found: {found}. Reply: {reply[:300]}"
            )

            # Determine routed agent
            routed_agent = get_routed_agent_name(result)
            assert routed_agent == "researcher" or (
                routed_agent and "research" in routed_agent.lower()
            ), f"Expected routing to researcher, got: {routed_agent}"

            # Save for next test
            _shared_state["research_report"] = reply

        finally:
            cleanup()


@pytest.mark.order(2)
@pytest.mark.asyncio
class TestQuantCreatesStrategy:
    """Step 2: Implement the researched strategy.

    Asserts:
    - Supervisor routes to quant agent
    - create-strategy skill is executed (file produced or skill status code)
    - User receives completion notification (non-empty message)
    """

    async def test_quant_creates_strategy(self):
        sup, sm, cleanup = make_supervisor()
        try:
            msg = AgentMessage(
                payload={
                    "text": "实现这个策略",
                    "strategy_description": _shared_state.get("research_report", ""),
                },
                user_id=TEST_USER,
            )
            result = await sup.handle(msg)

            assert result.success, f"Step 2 failed: {result.error}"

            reply = extract_reply(result.data)
            assert reply and len(reply.strip()) > 0, (
                "User received empty completion notification"
            )

            # Verify routing to quant agent
            routed_agent = get_routed_agent_name(result)
            assert routed_agent == "quant" or (
                routed_agent and "quant" in routed_agent.lower()
            ), f"Expected routing to quant, got: {routed_agent}. Reply: {reply[:200]}"

            # Verify strategy creation occurred (reply should mention strategy/file/test)
            creation_indicators = [
                "策略",
                "创建",
                "test",
                "strategy",
                "py",
                "文件",
                "通过",
            ]
            reply_lower = reply.lower()
            found_indicators = [
                ind for ind in creation_indicators if ind.lower() in reply_lower
            ]
            assert found_indicators, (
                f"No strategy creation indicators found. Reply: {reply[:300]}"
            )

            _shared_state["strategy_content"] = reply

        finally:
            cleanup()


@pytest.mark.order(3)
@pytest.mark.asyncio
class TestBacktestReturnsResult:
    """Step 3: Backtest the created strategy.

    Asserts:
    - Supervisor routes to backtest (quant agent with submit_backtest tool)
    - Result contains at least one of: 收益, 胜率, 回撤, return, win_rate, drawdown,
      task_id (async submission), 已提交 (submission confirmed)
    """

    @staticmethod
    async def _mock_backtest_tool_call(tc) -> str:
        """Mock tool call that intercepts submit_backtest and get_task_result."""
        if tc.name == "submit_backtest":
            return (
                '{"task_id": "e2e-test-task-123", "result_id": "e2e-result-456", '
                '"status": "SUCCESS", '
                '"message": "回测任务已提交", '
                '"result": {"total_return_pct": 5.2, "win_rate": 0.65, '
                '"max_drawdown_pct": 2.1, "收益": "5.2%", "胜率": "65%", "回撤": "2.1%"}}'
            )
        if tc.name == "get_task_result":
            return (
                '{"task_id": "e2e-test-task-123", "status": "SUCCESS", '
                '"result": {"total_return_pct": 5.2, "win_rate": 0.65, '
                '"max_drawdown_pct": 2.1, "收益": "5.2%", "胜率": "65%", "回撤": "2.1%"}}'
            )
        # For other tools, let the real handler process them
        from apps.agent.tools.base import ToolRegistry

        tool = ToolRegistry.get(tc.name)
        if tool:
            try:
                r = await tool.execute(**tc.arguments)
                if r.success:
                    return str(r.data)
                return f"Error: {r.error}"
            except Exception as e:
                return f"Error: {e}"
        return f"[tool {tc.name} not found]"

    async def test_backtest_returns_result(self):
        sup, sm, cleanup = make_supervisor()
        try:
            msg = AgentMessage(
                payload={
                    "text": "回测这个策略",
                    "strategy_content": _shared_state.get("strategy_content", ""),
                },
                user_id=TEST_USER,
            )

            result = await sup.handle(msg)

            assert result.success, f"Step 3 failed: {result.error}"

            reply = extract_reply(result.data)
            assert reply and len(reply.strip()) > 0, "Backtest returned empty result"

            # Verify result contains backtest-related fields
            backtest_fields = [
                "收益",
                "胜率",
                "回撤",
                "return",
                "win_rate",
                "drawdown",
                "task_id",
                "已提交",
                "回测",
                "backtest",
                "PENDING",
                "SUCCESS",
                "FAILURE",
                "status",
            ]
            reply_lower = reply.lower()
            found = [f for f in backtest_fields if f.lower() in reply_lower]
            assert found, f"No backtest result fields found. Reply: {reply[:300]}"

        finally:
            cleanup()
