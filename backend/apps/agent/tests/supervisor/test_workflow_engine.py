"""Tests for the WorkflowEngine seam.

Verifies that the engine orchestrates multi-step workflows correctly when
given mock dependencies (``llm_client`` and ``route_to_agent`` callable).
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from apps.agent.base import AgentMessage, AgentResult
from apps.agent.workflow_engine import (
    WorkflowContext,
    WorkflowEngine,
)


def _ok(data=None) -> AgentResult:
    return AgentResult(task_id="t", success=True, data=data or "")


def _fail(error: str) -> AgentResult:
    return AgentResult(task_id="t", success=False, error=error)


class _MockSessionManager:
    def __init__(self):
        self.calls = []

    async def get_session_context(self, user_id):
        return None

    async def set_session_context(
        self, user_id, state, active_agent=None, task_id=None, ttl=1800
    ):
        self.calls.append(("set", user_id, state, active_agent))

    async def clear_session_context(self, user_id):
        self.calls.append(("clear", user_id))

    async def pause_session(
        self, user_id, active_agent, pause_ttl=300, pause_context=""
    ):
        self.calls.append(("pause", user_id))


def _make_engine(route_fn=None, llm_chat=None):
    llm = MagicMock()
    llm.chat = AsyncMock(side_effect=llm_chat or (lambda *a, **kw: "{}"))
    sm = _MockSessionManager()
    engine = WorkflowEngine(
        llm_client=llm,
        route_to_agent=route_fn or (lambda *a, **kw: _ok()),
        agent_name="test",
        get_session_manager=lambda: sm,
    )
    return engine, llm, sm


class TestWorkflowContext(unittest.TestCase):
    def test_create_initializes_metadata(self):
        ctx = WorkflowContext.create(summary="demo", variables={"k": "v"})
        self.assertEqual(ctx["summary"], "demo")
        self.assertEqual(ctx["variables"], {"k": "v"})
        self.assertIn("workflow_id", ctx)
        self.assertEqual(ctx["metadata"]["completed_steps"], 0)


class TestExecuteWorkflowEmpty(unittest.TestCase):
    def test_empty_steps_returns_error(self):
        engine, _, _ = _make_engine()
        result = asyncio.run(
            engine.execute_workflow(
                {"summary": "x", "steps": []}, AgentMessage(user_id="u")
            )
        )
        self.assertFalse(result.success)
        self.assertIn("空", result.error)


class TestExecuteSteps(unittest.TestCase):
    def test_single_step_success(self):
        calls = []

        async def route(agent_name, msg, on_tool_result=None):
            calls.append((agent_name, msg.payload["text"]))
            return _ok({"content": "done"})

        engine, _, _ = _make_engine(route_fn=route)
        plan = {
            "summary": "one",
            "steps": [{"agent": "a", "message": "hello"}],
        }
        result = asyncio.run(engine.execute_workflow(plan, AgentMessage(user_id="u")))

        self.assertTrue(result.success)
        self.assertEqual(calls, [("a", "hello")])
        self.assertEqual(len(result.data["step_results"]), 1)

    def test_step_failure_abort(self):
        async def route(agent_name, msg, on_tool_result=None):
            return _fail("boom")

        engine, _, _ = _make_engine(route_fn=route)
        plan = {
            "summary": "one",
            "steps": [{"agent": "a", "message": "hello", "on_failure": "abort"}],
        }
        result = asyncio.run(engine.execute_workflow(plan, AgentMessage(user_id="u")))
        self.assertFalse(result.success)

    def test_step_failure_skip(self):
        async def route(agent_name, msg, on_tool_result=None):
            return _fail("boom")

        engine, _, _ = _make_engine(route_fn=route)
        plan = {
            "summary": "one",
            "steps": [{"agent": "a", "message": "hello", "on_failure": "skip"}],
        }
        result = asyncio.run(engine.execute_workflow(plan, AgentMessage(user_id="u")))
        self.assertTrue(result.success)
        self.assertTrue(result.data["step_results"][0].get("failed"))

    def test_missing_agent_field(self):
        engine, _, _ = _make_engine()
        plan = {"summary": "x", "steps": [{"message": "no agent"}]}
        result = asyncio.run(engine.execute_workflow(plan, AgentMessage(user_id="u")))
        self.assertFalse(result.success)
        self.assertIn("agent", result.error)


class TestConditionEvaluation(unittest.TestCase):
    def test_condition_skip(self):
        calls = []

        async def route(agent_name, msg, on_tool_result=None):
            calls.append(agent_name)
            return _ok("ok")

        engine, _, _ = _make_engine(route_fn=route)
        plan = {
            "summary": "x",
            "steps": [
                {
                    "agent": "a",
                    "message": "m",
                    "condition": "variables.get('go') == True",
                }
            ],
        }
        result = asyncio.run(engine.execute_workflow(plan, AgentMessage(user_id="u")))
        self.assertTrue(result.success)
        self.assertEqual(calls, [])  # skipped

    def test_condition_allow(self):
        calls = []

        async def route(agent_name, msg, on_tool_result=None):
            calls.append(agent_name)
            return _ok("ok")

        engine, _, _ = _make_engine(route_fn=route)
        plan = {
            "summary": "x",
            "variables": {"go": True},
            "steps": [
                {
                    "agent": "a",
                    "message": "m",
                    "condition": "variables.get('go') == True",
                }
            ],
        }
        result = asyncio.run(engine.execute_workflow(plan, AgentMessage(user_id="u")))
        self.assertTrue(result.success)
        self.assertEqual(calls, ["a"])


class TestGoalLoop(unittest.TestCase):
    def test_loop_stops_on_goal_condition(self):
        iterations = []

        async def route(agent_name, msg, on_tool_result=None):
            iterations.append(1)
            # After 2 calls, set score variable so goal_condition passes
            if len(iterations) >= 2:
                return _ok({"metrics": {"score": 0.9}})
            return _ok({"metrics": {"score": 0.3}})

        engine, _, _ = _make_engine(route_fn=route)
        plan = {
            "summary": "loop",
            "steps": [
                {
                    "type": "loop",
                    "goal": "reach high score",
                    "goal_condition": "variables.get('score', 0) >= 0.8",
                    "max_iterations": 5,
                    "steps": [{"agent": "a", "message": "try"}],
                }
            ],
        }
        result = asyncio.run(engine.execute_workflow(plan, AgentMessage(user_id="u")))
        self.assertTrue(result.success)
        # Should have stopped at iteration 2 (not 5)
        self.assertEqual(len(iterations), 2)


class TestRenderLoopMessage(unittest.TestCase):
    def test_placeholders_replaced(self):
        engine, _, _ = _make_engine()
        out = engine.render_loop_message(
            "iter {loop_iteration} of {goal}",
            {"loop_iteration": 2, "goal": "x"},
        )
        self.assertEqual(out, "iter 2 of x")

    def test_missing_keys_left_as_is(self):
        engine, _, _ = _make_engine()
        out = engine.render_loop_message("v={unknown}", {})
        self.assertEqual(out, "v={unknown}")


class TestEvaluateCondition(unittest.TestCase):
    def test_true_expression(self):
        engine, _, _ = _make_engine()
        ctx = WorkflowContext.create(summary="s", variables={"n": 5})
        self.assertTrue(engine.evaluate_condition(ctx, "variables['n'] > 3"))

    def test_false_expression(self):
        engine, _, _ = _make_engine()
        ctx = WorkflowContext.create(summary="s", variables={"n": 1})
        self.assertFalse(engine.evaluate_condition(ctx, "variables['n'] > 3"))

    def test_invalid_expression_defaults_true(self):
        engine, _, _ = _make_engine()
        ctx = WorkflowContext.create(summary="s")
        # Syntax error → default True (fail-open)
        self.assertTrue(engine.evaluate_condition(ctx, "variables['nope'].bogus"))


if __name__ == "__main__":
    unittest.main()
