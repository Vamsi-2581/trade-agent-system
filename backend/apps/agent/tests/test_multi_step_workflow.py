"""Tests for SupervisorAgent multi-step workflow execution."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import unittest

from apps.agent.supervisor import (
    IntentRouter,
    SupervisorAgent,
    AgentMessage,
    AgentResult,
)


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
    def __init__(self):
        self._store: dict = {}

    async def get_session_context(self, user_id):
        return self._store.get(f"session:{user_id}:context")

    async def set_session_context(
        self, user_id, state, active_agent=None, task_id=None, ttl=1800
    ):
        self._store[f"session:{user_id}:context"] = {
            "state": state.value if hasattr(state, "value") else state,
            "active_agent": active_agent,
            "task_id": task_id,
        }

    async def clear_session_context(self, user_id):
        self._store.pop(f"session:{user_id}:context", None)

    async def pause_session(
        self, user_id, active_agent, pause_ttl=300, pause_context=""
    ):
        self._store[f"session:{user_id}:context"] = {
            "state": "paused",
            "active_agent": active_agent,
            "pause_context": pause_context,
        }

    def get_redis(self):
        """Return a mock redis handle for workflow state storage."""

        class MockRedis:
            async def set(self, key, value, ex=None):
                pass

        return MockRedis()


class MockMemoryManager:
    def __init__(self, agent_type="supervisor", user_id="test"):
        self._l1 = {}

    async def write_l2(self, *args, **kwargs):
        pass

    async def get_conv_history(self, max_turns=5):
        return self._l1.get("conv_history", [])

    async def save_conv_history(self, history):
        self._l1["conv_history"] = list(history)

    async def append_conv_history(self, entries, keep_turns=10):
        current = self._l1.get("conv_history", [])
        updated = (current + entries)[-keep_turns:]
        self._l1["conv_history"] = updated
        return updated


def make_accepting_agent(content="ok"):
    agent = AsyncMock()
    agent.handle = AsyncMock(
        return_value=AgentResult(
            success=True,
            data={"content": content, "continue_conversation": False},
        )
    )
    return agent


def make_rejecting_agent(reason="not my domain", suggestion=""):
    agent = AsyncMock()
    agent.handle = AsyncMock(
        return_value=AgentResult(
            success=False,
            error=reason,
            need_reroute=True,
            reroute_reason=reason,
            reroute_suggestion=suggestion,
        )
    )
    return agent


def make_agent_getter(**agents):
    def getter(name):
        if name in agents:
            return agents[name]
        raise KeyError(f"Agent not registered: {name!r}")

    return getter


def create_supervisor_with_mocks():
    mock_session_mgr = MockSessionManager()

    patch_sm = patch(
        "apps.agent.supervisor.get_session_manager",
        return_value=mock_session_mgr,
    )
    patch_mm = patch("apps.memory.manager.MemoryManager", MockMemoryManager)
    patch_redis = patch(
        "redis.asyncio.from_url",
        side_effect=Exception("no redis in tests"),
    )

    mock_llm = MagicMock()
    mock_llm.chat = AsyncMock()
    mock_llm.chat_with_tools = AsyncMock()
    patch_llm = patch(
        "apps.agent.llm_client.LLMClient.get_instance",
        return_value=mock_llm,
    )

    patch_sm.start()
    patch_mm.start()
    patch_redis.start()
    patch_llm.start()

    reset_all_singletons()

    router = IntentRouter.get_instance()
    router.register_frame_intent("start_trading", "trading", "start")
    router.register_frame_intent("stop_trading", "trading", "stop")

    supervisor = SupervisorAgent()
    supervisor._llm = mock_llm

    def stopper():
        patch_sm.stop()
        patch_mm.stop()
        patch_redis.stop()
        patch_llm.stop()

    return supervisor, mock_session_mgr, stopper


class TestExecuteWorkflow(unittest.TestCase):
    """Test _execute_workflow method."""

    def _setup(self):
        sup, sm, stop = create_supervisor_with_mocks()
        self.addCleanup(stop)
        return sup, sm

    def test_two_step_workflow_researcher_then_quant(self):
        """Researcher returns findings, quant receives them as context."""
        sup, sm = self._setup()

        mock_researcher = make_accepting_agent(
            "## 策略研究\nRSI+EMA超短线策略：RSI(14)<30买入，>70卖出。"
        )
        mock_quant = make_accepting_agent("策略代码已实现。")

        workflow_plan = {
            "summary": "研究并实现策略",
            "steps": [
                {
                    "agent": "researcher",
                    "message": "研究一个BTC 5分钟线的RSI超短线策略。",
                },
                {
                    "agent": "quant",
                    "message": "基于研究结果实现策略代码。",
                },
            ],
        }

        with patch(
            "apps.agent.registry.AgentRegistry.get",
            side_effect=make_agent_getter(
                researcher=mock_researcher,
                quant=mock_quant,
            ),
        ):
            result = asyncio.run(
                sup._execute_workflow(
                    workflow_plan,
                    AgentMessage(user_id="u1"),
                )
            )

        self.assertTrue(result.success)
        mock_researcher.handle.assert_called_once()
        mock_quant.handle.assert_called_once()

        # Verify context injection
        quant_call = mock_quant.handle.call_args[0][0]
        self.assertEqual(
            quant_call.payload.get("previous_agent_name"),
            "researcher",
        )
        self.assertIn("RSI", quant_call.payload.get("previous_agent_response", ""))

    def test_workflow_step_fails_fast(self):
        """If step 1 fails, step 2 should not execute."""
        sup, sm = self._setup()

        mock_failing = make_rejecting_agent(reason="无法完成研究")
        mock_quant = make_accepting_agent("不应被执行")

        workflow_plan = {
            "summary": "研究并实现",
            "steps": [
                {"agent": "researcher", "message": "做研究"},
                {"agent": "quant", "message": "实现代码"},
            ],
        }

        with patch(
            "apps.agent.registry.AgentRegistry.get",
            side_effect=make_agent_getter(
                researcher=mock_failing,
                quant=mock_quant,
            ),
        ):
            result = asyncio.run(
                sup._execute_workflow(
                    workflow_plan,
                    AgentMessage(user_id="u1"),
                )
            )

        self.assertFalse(result.success)
        self.assertIn("步骤 1", result.error)
        self.assertIn("失败", result.error)
        mock_failing.handle.assert_called_once()
        mock_quant.handle.assert_not_called()

    def test_empty_workflow_returns_error(self):
        """Empty steps list returns failure."""
        sup, sm = self._setup()

        result = asyncio.run(
            sup._execute_workflow(
                {"summary": "空工作流", "steps": []},
                AgentMessage(user_id="u1"),
            )
        )

        self.assertFalse(result.success)
        self.assertIn("空", result.error)

    def test_three_step_workflow_with_summary(self):
        """Three steps all complete, result includes step summary."""
        sup, sm = self._setup()

        mock_a = make_accepting_agent("结果A")
        mock_b = make_accepting_agent("结果B")
        mock_c = make_accepting_agent("结果C")

        workflow_plan = {
            "summary": "三步工作流",
            "steps": [
                {"agent": "researcher", "message": "步骤A"},
                {"agent": "quant", "message": "步骤B"},
                {"agent": "researcher", "message": "步骤C"},
            ],
        }

        dispatches = []

        def mock_get(name):
            dispatches.append(name)
            if name == "quant":
                return mock_b
            count = sum(1 for d in dispatches if d == "researcher")
            return mock_a if count == 1 else mock_c

        with patch(
            "apps.agent.registry.AgentRegistry.get",
            side_effect=mock_get,
        ):
            result = asyncio.run(
                sup._execute_workflow(
                    workflow_plan,
                    AgentMessage(user_id="u1"),
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.data["step_results"][0]["agent"], "researcher")
        self.assertEqual(result.data["step_results"][1]["agent"], "quant")
        self.assertEqual(result.data["step_results"][2]["agent"], "researcher")

    def test_missing_agent_field_returns_error(self):
        """Step without agent field returns error."""
        sup, sm = self._setup()

        workflow_plan = {
            "summary": "无效工作流",
            "steps": [
                {"message": "没有指定agent"},
            ],
        }

        result = asyncio.run(
            sup._execute_workflow(
                workflow_plan,
                AgentMessage(user_id="u1"),
            )
        )

        self.assertFalse(result.success)
        self.assertIn("缺少 agent", result.error)

    def test_single_step_workflow_succeeds(self):
        """Single step workflow completes successfully."""
        sup, sm = self._setup()

        mock_quant = make_accepting_agent("策略已创建")

        workflow_plan = {
            "summary": "创建策略",
            "steps": [
                {"agent": "quant", "message": "创建一个RSI策略"},
            ],
        }

        with patch(
            "apps.agent.registry.AgentRegistry.get",
            return_value=mock_quant,
        ):
            result = asyncio.run(
                sup._execute_workflow(
                    workflow_plan,
                    AgentMessage(user_id="u1"),
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(len(result.data["step_results"]), 1)
        self.assertEqual(result.data["step_results"][0]["agent"], "quant")
        self.assertIn("策略已创建", result.data["workflow_summary"])


class TestNormalRouteWorkflowDetection(unittest.TestCase):
    """Test that _normal_route detects and executes workflow plans."""

    def _setup(self):
        sup, sm, stop = create_supervisor_with_mocks()
        self.addCleanup(stop)
        return sup, sm

    def test_parse_intent_returns_workflow_plan(self):
        """_parse_intent detects multi-step task and returns workflow plan."""
        sup, sm = self._setup()

        workflow_json = json.dumps(
            {
                "_workflow_plan": {
                    "summary": "研究并实现策略",
                    "steps": [
                        {"agent": "researcher", "message": "做研究"},
                        {"agent": "quant", "message": "实现代码"},
                    ],
                }
            }
        )

        sup._llm.chat = AsyncMock(return_value=workflow_json)

        result = asyncio.run(sup._parse_intent("先研究BTC策略然后实现它"))

        self.assertIsInstance(result, dict)
        self.assertIn("_workflow_plan", result)
        self.assertEqual(len(result["_workflow_plan"]["steps"]), 2)

    def test_normal_route_executes_workflow(self):
        """_normal_route detects workflow plan and calls _execute_workflow."""
        sup, sm = self._setup()

        mock_researcher = make_accepting_agent("研究完成")
        mock_quant = make_accepting_agent("实现完成")

        workflow_json = json.dumps(
            {
                "_workflow_plan": {
                    "summary": "研究并实现",
                    "steps": [
                        {"agent": "researcher", "message": "研究BTC策略"},
                        {"agent": "quant", "message": "实现策略代码"},
                    ],
                }
            }
        )

        sup._llm.chat = AsyncMock(return_value=workflow_json)

        with patch(
            "apps.agent.registry.AgentRegistry.get",
            side_effect=make_agent_getter(
                researcher=mock_researcher,
                quant=mock_quant,
            ),
        ):
            result = asyncio.run(
                sup.handle(
                    AgentMessage(
                        payload={"text": "先研究BTC策略然后实现它"},
                        user_id="u1",
                    )
                )
            )

        self.assertTrue(result.success)
        mock_researcher.handle.assert_called_once()
        mock_quant.handle.assert_called_once()
        result_data = result.data
        self.assertIn("step_results", result_data)
        self.assertIn("workflow_summary", result_data)

    def test_normal_route_wires_workflow_history_recorder(self):
        """回归（c7728cd 重构漏传）：_normal_route 必须把 _record_workflow 作为
        on_workflow_complete 传给 WorkflowEngine。

        漏传的后果（2026-07-06 重构 ~ 2026-09-18 实际发生）：WorkflowHistory 永不
        落库、也不产生"工作流完成/失败"通知——聊天发起的工作流在前端历史里完全
        消失（DB 取证：WorkflowHistory 最后一行 2026-07-05 00:04，通知最后一条
        同为 7/5，之后 2.5 个月为 0）。
        """
        from apps.agent.base import AgentResult

        sup, sm = self._setup()

        workflow_json = json.dumps(
            {
                "_workflow_plan": {
                    "summary": "研究并实现",
                    "steps": [
                        {"agent": "researcher", "message": "研究BTC策略"},
                    ],
                }
            }
        )
        sup._llm.chat = AsyncMock(return_value=workflow_json)

        captured = {}

        async def spy(
            workflow_plan, message, on_tool_result=None, on_workflow_complete=None
        ):
            captured["on_workflow_complete"] = on_workflow_complete
            return AgentResult(task_id=message.task_id, success=True, data="ok")

        sup._workflow_engine.execute_workflow = spy

        asyncio.run(
            sup.handle(
                AgentMessage(
                    payload={"text": "先研究BTC策略然后实现它"},
                    user_id="u1",
                )
            )
        )

        self.assertIsNotNone(
            captured.get("on_workflow_complete"),
            "引擎未收到 on_workflow_complete → 工作流历史与完成通知都不会落库",
        )
        self.assertEqual(captured["on_workflow_complete"], sup._record_workflow)

    def test_single_step_task_unaffected(self):
        """Single agent task still routes normally, not through workflow."""
        sup, sm = self._setup()

        mock_quant = make_accepting_agent("策略完成")

        # Non-workflow response
        sup._llm.chat = AsyncMock(return_value=json.dumps({"agent": "quant"}))

        with patch("apps.agent.registry.AgentRegistry.get", return_value=mock_quant):
            result = asyncio.run(
                sup.handle(
                    AgentMessage(
                        payload={"text": "帮我写一个RSI策略"},
                        user_id="u1",
                    )
                )
            )

        self.assertTrue(result.success)
        mock_quant.handle.assert_called_once()
        result_data = result.data
        if isinstance(result_data, dict):
            self.assertNotIn("step_results", result_data)


class TestNumberedStepWorkflowDetection(unittest.TestCase):
    """Test that numbered/ordered multi-step requests are properly decomposed."""

    def _setup(self):
        sup, sm, stop = create_supervisor_with_mocks()
        self.addCleanup(stop)
        return sup, sm

    def test_numbered_steps_split_researcher_and_quant(self):
        """User message with 1) 2) 3) 4) steps should produce a workflow plan.

        This is the exact scenario from the bug report:
        '请在网络上研究一个高胜率交易策略，完成以下步骤：
         1）搜索并分析当前市场环境下的高胜率策略思路；
         2）实现策略逻辑并编写回测代码；
         3）执行回测并分析结果；
         4）根据回测结果不断优化策略参数和逻辑'

        Step 1 -> researcher, Steps 2-4 -> quant
        """
        sup, sm = self._setup()

        user_text = (
            "请在网络上研究一个高胜率交易策略，完成以下步骤："
            "1）搜索并分析当前市场环境下的高胜率策略思路；"
            "2）实现策略逻辑并编写回测代码；"
            "3）执行回测并分析结果；"
            "4）根据回测结果不断优化策略参数和逻辑，目标是使夏普比率达到2以上。"
            "全程自动执行，无需人工确认。请输出策略详情、回测结果和优化过程。"
        )

        workflow_json = json.dumps(
            {
                "_workflow_plan": {
                    "summary": "研究高胜率策略并回测优化",
                    "steps": [
                        {
                            "agent": "researcher",
                            "message": "搜索并分析当前市场环境下的高胜率交易策略思路",
                        },
                        {
                            "agent": "quant",
                            "message": "基于研究结果，实现策略逻辑并编写回测代码",
                        },
                        {
                            "agent": "quant",
                            "message": "执行回测并分析回测结果",
                        },
                        {
                            "agent": "quant",
                            "message": "根据回测结果优化策略参数和逻辑，目标夏普比率达到2以上",
                        },
                    ],
                }
            }
        )

        sup._llm.chat = AsyncMock(return_value=workflow_json)

        mock_researcher = make_accepting_agent("研究完成，RSI+EMA策略最佳")
        mock_quant = make_accepting_agent("回测完成，夏普比率1.8")

        with patch(
            "apps.agent.registry.AgentRegistry.get",
            side_effect=make_agent_getter(
                researcher=mock_researcher,
                quant=mock_quant,
            ),
        ):
            result = asyncio.run(
                sup.handle(AgentMessage(payload={"text": user_text}, user_id="u1"))
            )

        self.assertTrue(result.success)
        # Workflow 指引现在在 system prompt（prompts/v1/supervisor.txt）里
        self.assertIn("_workflow_plan", sup._llm.chat.call_args[1]["system"])
        # researcher called once for step 1
        self.assertEqual(mock_researcher.handle.call_count, 1)
        # quant called 3 times for steps 2, 3, 4
        self.assertEqual(mock_quant.handle.call_count, 3)

    def test_chinese_numbered_steps_detection(self):
        """LLM prompt should guide detection of multi-step workflows."""
        sup, sm = self._setup()

        # Verify the prompt includes guidance about multi-step workflows
        sup._llm.chat = AsyncMock(return_value='{"agent": "unknown"}')
        asyncio.run(sup._parse_intent("先研究再实现"))

        # The system prompt (loaded from prompts/v1/supervisor.txt) carries
        # the workflow-plan guidance; the user prompt carries the message.
        chat_call_kwargs = sup._llm.chat.call_args[1]
        self.assertIn("workflow_plan", chat_call_kwargs["system"])
        # user prompt still mentions the agent list used for routing
        self.assertIn("available agents", chat_call_kwargs["user"].lower())


class TestWorkflowRetryAndConditions(unittest.TestCase):
    """Test enhanced workflow: retry, conditions, skip on failure."""

    def _setup(self):
        sup, sm, stop = create_supervisor_with_mocks()
        self.addCleanup(stop)
        return sup, sm

    def test_step_retry_succeeds_on_second_attempt(self):
        """Step fails first attempt, succeeds on retry."""
        sup, sm = self._setup()

        call_count = 0

        async def mock_handler(msg, on_tool_result=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return AgentResult(success=False, error="临时错误")
            return AgentResult(
                success=True,
                data={"content": "重试成功", "continue_conversation": False},
            )

        mock_agent = AsyncMock()
        mock_agent.handle = mock_handler

        workflow_plan = {
            "summary": "重试测试",
            "steps": [
                {
                    "agent": "researcher",
                    "message": "执行研究",
                    "max_retries": 2,
                    "retry_delay": 0.01,
                },
            ],
        }

        with patch(
            "apps.agent.registry.AgentRegistry.get",
            return_value=mock_agent,
        ):
            result = asyncio.run(
                sup._execute_workflow(
                    workflow_plan,
                    AgentMessage(user_id="u1"),
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(call_count, 2)  # Failed once, retried once

    def test_condition_skip_step(self):
        """Step with condition=False is skipped."""
        sup, sm = self._setup()

        mock_a = make_accepting_agent("结果A")
        mock_b = make_accepting_agent("不应执行")

        # Condition evaluates to False → step is skipped
        workflow_plan = {
            "summary": "条件测试",
            "steps": [
                {"agent": "researcher", "message": "步骤A"},
                {
                    "agent": "quant",
                    "message": "步骤B（应跳过）",
                    "condition": "variables['enable_step_b'] == True",
                },
            ],
            "variables": {"enable_step_b": False},
        }

        with patch(
            "apps.agent.registry.AgentRegistry.get",
            side_effect=make_agent_getter(
                researcher=mock_a,
                quant=mock_b,
            ),
        ):
            result = asyncio.run(
                sup._execute_workflow(
                    workflow_plan,
                    AgentMessage(user_id="u1"),
                )
            )

        self.assertTrue(result.success)
        mock_a.handle.assert_called_once()
        mock_b.handle.assert_not_called()

    def test_condition_allow_step(self):
        """Step with condition=True is executed."""
        sup, sm = self._setup()

        mock_a = make_accepting_agent("结果A")
        mock_b = make_accepting_agent("结果B")

        workflow_plan = {
            "summary": "条件测试",
            "steps": [
                {"agent": "researcher", "message": "步骤A"},
                {
                    "agent": "quant",
                    "message": "步骤B",
                    "condition": "variables['run_step_b'] == True",
                },
            ],
            "variables": {"run_step_b": True},
        }

        with patch(
            "apps.agent.registry.AgentRegistry.get",
            side_effect=make_agent_getter(
                researcher=mock_a,
                quant=mock_b,
            ),
        ):
            result = asyncio.run(
                sup._execute_workflow(
                    workflow_plan,
                    AgentMessage(user_id="u1"),
                )
            )

        self.assertTrue(result.success)
        mock_a.handle.assert_called_once()
        mock_b.handle.assert_called_once()

    def test_on_failure_skip(self):
        """Step with on_failure=skip continues after failure."""
        sup, sm = self._setup()

        mock_failing = make_rejecting_agent(reason="无法完成")
        mock_b = make_accepting_agent("步骤B完成")

        workflow_plan = {
            "summary": "跳过失败测试",
            "steps": [
                {
                    "agent": "researcher",
                    "message": "步骤A",
                    "on_failure": "skip",
                },
                {"agent": "quant", "message": "步骤B"},
            ],
        }

        with patch(
            "apps.agent.registry.AgentRegistry.get",
            side_effect=make_agent_getter(
                researcher=mock_failing,
                quant=mock_b,
            ),
        ):
            result = asyncio.run(
                sup._execute_workflow(
                    workflow_plan,
                    AgentMessage(user_id="u1"),
                )
            )

        self.assertTrue(result.success)
        mock_failing.handle.assert_called_once()
        mock_b.handle.assert_called_once()
        # Verify failed step is recorded
        step_results = result.data["step_results"]
        self.assertTrue(step_results[0].get("failed"))

    def test_workflow_id_in_result(self):
        """Completed workflow includes workflow_id and elapsed_seconds."""
        sup, sm = self._setup()

        mock_a = make_accepting_agent("完成")

        workflow_plan = {
            "summary": "ID测试",
            "steps": [
                {"agent": "researcher", "message": "执行"},
            ],
        }

        with patch(
            "apps.agent.registry.AgentRegistry.get",
            return_value=mock_a,
        ):
            result = asyncio.run(
                sup._execute_workflow(
                    workflow_plan,
                    AgentMessage(user_id="u1"),
                )
            )

        self.assertTrue(result.success)
        self.assertIn("workflow_id", result.data)
        self.assertIn("elapsed_seconds", result.data)

    def test_workflow_context_passed_to_step(self):
        """Step message includes workflow_id and variables in payload."""
        sup, sm = self._setup()

        captured_payload = {}

        async def capture_handle(msg, on_tool_result=None):
            captured_payload.update(msg.payload)
            return AgentResult(
                success=True,
                data={"content": "OK", "continue_conversation": False},
            )

        mock_agent = AsyncMock()
        mock_agent.handle = capture_handle

        workflow_plan = {
            "summary": "上下文测试",
            "steps": [
                {"agent": "researcher", "message": "执行"},
                {"agent": "quant", "message": "第二步"},
            ],
            "variables": {"topic": "BTC"},
        }

        with patch(
            "apps.agent.registry.AgentRegistry.get",
            side_effect=make_agent_getter(
                researcher=mock_agent,
                quant=mock_agent,
            ),
        ):
            asyncio.run(
                sup._execute_workflow(
                    workflow_plan,
                    AgentMessage(user_id="u1"),
                )
            )

        # Second step should have workflow context
        self.assertIn("workflow_id", captured_payload)
        self.assertIn("workflow_variables", captured_payload)
        self.assertEqual(captured_payload["workflow_variables"]["topic"], "BTC")


class TestHandleWorkflowRunning(unittest.TestCase):
    """Test that WORKFLOW_RUNNING state blocks new routing."""

    def _setup(self):
        sup, sm, stop = create_supervisor_with_mocks()
        self.addCleanup(stop)
        return sup, sm

    def test_cancel_workflow_running(self):
        """User can cancel a running workflow."""
        sup, sm = self._setup()

        result = asyncio.run(
            sup._handle_workflow_running(
                AgentMessage(
                    payload={"text": "取消"},
                    user_id="u1",
                ),
                {"state": "workflow_running"},
                sm,
            )
        )

        self.assertTrue(result.success)
        self.assertIn("取消", result.data)

    def test_new_message_blocked_during_workflow(self):
        """New messages during workflow get status reply."""
        sup, sm = self._setup()

        result = asyncio.run(
            sup._handle_workflow_running(
                AgentMessage(
                    payload={"text": "帮我写个策略"},
                    user_id="u1",
                ),
                {"state": "workflow_running"},
                sm,
            )
        )

        self.assertTrue(result.success)
        self.assertIn("工作流正在执行", result.data)


class TestRecurringTaskWithWorkflow(unittest.TestCase):
    """Test recurring task with workflow steps: tool and execution path."""

    def _setup(self):
        sup, sm, stop = create_supervisor_with_mocks()
        self.addCleanup(stop)
        return sup, sm

    def test_submit_recurring_task_with_steps_forces_supervisor(self):
        """When steps are provided, agent_name is forced to supervisor."""
        from apps.agent.tools.schedule_task import SubmitRecurringTaskTool

        tool = SubmitRecurringTaskTool()

        steps = [
            {"agent": "researcher", "message": "研究BTC策略"},
            {"agent": "quant", "message": "实现并回测"},
        ]

        # Mock sync_to_async to return an async callable
        mock_schedule = MagicMock()
        captured_kwargs = {}

        async def mock_db_call(*args, **kwargs):
            captured_kwargs.update(kwargs)
            return (mock_schedule, True)

        def mock_sync_to_async(fn):
            return mock_db_call

        async def mock_update_or_create(**kw):
            captured_kwargs.update(kw)
            pass

        with patch("asgiref.sync.sync_to_async", mock_sync_to_async):
            with patch(
                "django_celery_beat.models.CrontabSchedule.objects.get_or_create",
                side_effect=lambda **kw: (mock_schedule, True),
            ):
                with patch(
                    "django_celery_beat.models.PeriodicTask.objects.update_or_create",
                    new_callable=AsyncMock,
                    return_value=None,
                ):
                    result = asyncio.run(
                        tool.execute(
                            agent_name="quant",  # LLM might pass any agent
                            message="每小时研究并回测",
                            cron_expression="0 * * * *",
                            task_name="hourly_research_backtest",
                            user_id="u1",
                            steps=steps,
                            summary="每小时研究高胜率策略并回测优化",
                        )
                    )

        self.assertTrue(result.success)
        self.assertEqual(result.data["agent_name"], "supervisor")
        self.assertTrue(result.data["workflow"])
        self.assertIn("包含 2 个步骤", result.data["message"])

    def test_execute_recurring_workflow_task_calls_execute_workflow(self):
        """Task with workflow_steps directly calls _execute_workflow."""
        from apps.agent.tasks import execute_recurring_agent_task

        sup, sm, stop = create_supervisor_with_mocks()
        self.addCleanup(stop)

        captured_workflow = {}

        async def capture_execute_workflow(workflow_plan, message, on_tool_result=None):
            captured_workflow.update(workflow_plan)
            return AgentResult(
                success=True,
                data={
                    "workflow_id": "test-id",
                    "workflow_summary": workflow_plan.get("summary", ""),
                    "step_results": [
                        {"agent": s["agent"], "success": True}
                        for s in workflow_plan.get("steps", [])
                    ],
                },
            )

        sup._execute_workflow = capture_execute_workflow

        with patch.object(SupervisorAgent, "get_instance", return_value=sup):
            result = execute_recurring_agent_task(
                agent_name="supervisor",
                message="测试",
                user_id="u1",
                task_name="test_task",
                workflow_steps=[
                    {"agent": "researcher", "message": "研究"},
                    {"agent": "quant", "message": "回测"},
                ],
                workflow_summary="测试workflow",
            )

        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(captured_workflow["summary"], "测试workflow")
        self.assertEqual(len(captured_workflow["steps"]), 2)

    def test_execute_recurring_task_without_steps_calls_handle(self):
        """Task without workflow_steps still calls supervisor.handle()."""
        from apps.agent.tasks import execute_recurring_agent_task

        sup, sm, stop = create_supervisor_with_mocks()
        self.addCleanup(stop)

        handle_called = False

        async def capture_handle(msg, on_tool_result=None):
            nonlocal handle_called
            handle_called = True
            return AgentResult(success=True, data="done")

        sup.handle = capture_handle

        with patch.object(SupervisorAgent, "get_instance", return_value=sup):
            result = execute_recurring_agent_task(
                agent_name="supervisor",
                message="简单任务",
                user_id="u1",
                task_name="simple_task",
            )

        self.assertEqual(result["status"], "SUCCESS")
        self.assertTrue(handle_called)


class TestGoalLoop(unittest.TestCase):
    """Test goal-driven loop execution within workflows."""

    def _setup(self):
        sup, sm, stop = create_supervisor_with_mocks()
        self.addCleanup(stop)
        return sup, sm

    def test_loop_stops_when_goal_condition_met(self):
        """Deterministic goal_condition triggers stop on matching iteration."""
        sup, sm = self._setup()

        call_count = 0

        async def quant_handle(msg, on_tool_result=None):
            nonlocal call_count
            call_count += 1
            # Return metrics that satisfy goal_condition on 2nd call
            sharpe = 1.0 if call_count == 1 else 1.6
            return AgentResult(
                success=True,
                data={
                    "content": f"回测完成 夏普={sharpe}",
                    "metrics": {"sharpe": sharpe},
                },
            )

        mock_quant = AsyncMock()
        mock_quant.handle = quant_handle

        workflow_plan = {
            "summary": "优化策略",
            "steps": [
                {
                    "type": "loop",
                    "goal": "夏普 >= 1.5",
                    "goal_condition": "float(variables.get('sharpe', 0)) >= 1.5",
                    "max_iterations": 5,
                    "steps": [
                        {"agent": "quant", "message": "优化参数 第{loop_iteration}轮"},
                    ],
                },
            ],
        }

        with patch(
            "apps.agent.registry.AgentRegistry.get",
            side_effect=make_agent_getter(quant=mock_quant),
        ):
            result = asyncio.run(
                sup._execute_workflow(workflow_plan, AgentMessage(user_id="u1"))
            )

        self.assertTrue(result.success)
        self.assertEqual(call_count, 2)
        # Verify loop result in step_results
        loop_entry = result.data["step_results"][0]
        self.assertEqual(loop_entry["type"], "loop")
        self.assertTrue(loop_entry["achieved"])
        self.assertEqual(len(loop_entry["iterations"]), 2)

    def test_loop_llm_judge_feedback_injection(self):
        """LLM judge returns feedback which is injected into next iteration's message."""
        sup, sm = self._setup()

        messages_received = []

        async def quant_handle(msg, on_tool_result=None):
            messages_received.append(msg.payload.get("text", ""))
            return AgentResult(success=True, data="回测结果")

        mock_quant = AsyncMock()
        mock_quant.handle = quant_handle

        judge_responses = [
            '{"achieved": false, "score": 0.4, "feedback": "增大止损"}',
            '{"achieved": false, "score": 0.7, "feedback": "调整仓位"}',
            '{"achieved": true, "score": 0.95, "feedback": ""}',
        ]
        judge_idx = 0

        async def mock_chat(*args, **kwargs):
            nonlocal judge_idx
            # Only intercept judge calls (goal evaluation)
            system = kwargs.get("system", "") if kwargs else ""
            if "goal evaluation" in str(system):
                resp = judge_responses[min(judge_idx, len(judge_responses) - 1)]
                judge_idx += 1
                return resp
            return '{"agent": "quant"}'

        sup._llm.chat = mock_chat

        workflow_plan = {
            "summary": "优化策略",
            "steps": [
                {
                    "type": "loop",
                    "goal": "胜率 > 60%",
                    "max_iterations": 5,
                    "steps": [
                        {
                            "agent": "quant",
                            "message": "目标:{goal} 第{loop_iteration}轮 反馈:{last_feedback}",
                        },
                    ],
                },
            ],
        }

        with patch(
            "apps.agent.registry.AgentRegistry.get",
            side_effect=make_agent_getter(quant=mock_quant),
        ):
            result = asyncio.run(
                sup._execute_workflow(workflow_plan, AgentMessage(user_id="u1"))
            )

        self.assertTrue(result.success)
        self.assertEqual(len(messages_received), 3)
        # First iteration: no feedback yet
        self.assertIn("第1轮", messages_received[0])
        # Second iteration: first round's feedback injected
        self.assertIn("增大止损", messages_received[1])
        # Third iteration: second round's feedback
        self.assertIn("调整仓位", messages_received[2])

    def test_loop_max_iterations_exhausted(self):
        """Loop exhausts max_iterations without achieving goal."""
        sup, sm = self._setup()

        async def quant_handle(msg, on_tool_result=None):
            return AgentResult(success=True, data="结果不佳")

        mock_quant = AsyncMock()
        mock_quant.handle = quant_handle

        async def mock_chat(*args, **kwargs):
            return '{"achieved": false, "score": 0.3, "feedback": "继续优化"}'

        sup._llm.chat = mock_chat

        workflow_plan = {
            "summary": "优化策略",
            "steps": [
                {
                    "type": "loop",
                    "goal": "夏普 >= 2.0",
                    "max_iterations": 3,
                    "steps": [
                        {"agent": "quant", "message": "优化"},
                    ],
                },
            ],
        }

        with patch(
            "apps.agent.registry.AgentRegistry.get",
            side_effect=make_agent_getter(quant=mock_quant),
        ):
            result = asyncio.run(
                sup._execute_workflow(workflow_plan, AgentMessage(user_id="u1"))
            )

        self.assertTrue(result.success)  # workflow completed (didn't crash)
        loop_entry = result.data["step_results"][0]
        self.assertFalse(loop_entry["achieved"])
        self.assertEqual(len(loop_entry["iterations"]), 3)

    def test_loop_judge_consecutive_failure_terminates(self):
        """Two consecutive judge failures terminate the loop early (workflow fails)."""
        sup, sm = self._setup()

        async def quant_handle(msg, on_tool_result=None):
            return AgentResult(success=True, data="结果")

        mock_quant = AsyncMock()
        mock_quant.handle = quant_handle

        judge_call_count = 0

        async def mock_chat(*args, **kwargs):
            nonlocal judge_call_count
            system = kwargs.get("system", "") if kwargs else ""
            if "goal evaluation" in str(system):
                judge_call_count += 1
                return "__FALLBACK__"  # LLM degraded
            return '{"agent": "quant"}'

        sup._llm.chat = mock_chat

        workflow_plan = {
            "summary": "优化策略",
            "steps": [
                {
                    "type": "loop",
                    "goal": "优化目标",
                    "max_iterations": 10,
                    "steps": [
                        {"agent": "quant", "message": "优化"},
                    ],
                },
            ],
        }

        with patch(
            "apps.agent.registry.AgentRegistry.get",
            side_effect=make_agent_getter(quant=mock_quant),
        ):
            result = asyncio.run(
                sup._execute_workflow(workflow_plan, AgentMessage(user_id="u1"))
            )

        # Judge failures are treated as workflow failure
        self.assertFalse(result.success)
        self.assertIn("失败", result.error)
        self.assertEqual(judge_call_count, 2)  # stopped after 2 failures

    def test_loop_max_iterations_capped_at_hard_limit(self):
        """max_iterations exceeding hard cap is clamped to 10."""
        sup, sm = self._setup()

        async def quant_handle(msg, on_tool_result=None):
            return AgentResult(success=True, data="结果")

        mock_quant = AsyncMock()
        mock_quant.handle = quant_handle

        async def mock_chat(*args, **kwargs):
            return '{"achieved": false, "score": 0.1, "feedback": "继续"}'

        sup._llm.chat = mock_chat

        workflow_plan = {
            "summary": "优化",
            "steps": [
                {
                    "type": "loop",
                    "goal": "目标",
                    "max_iterations": 99,  # should be clamped to 10
                    "steps": [
                        {"agent": "quant", "message": "优化"},
                    ],
                },
            ],
        }

        with patch(
            "apps.agent.registry.AgentRegistry.get",
            side_effect=make_agent_getter(quant=mock_quant),
        ):
            result = asyncio.run(
                sup._execute_workflow(workflow_plan, AgentMessage(user_id="u1"))
            )

        self.assertTrue(result.success)
        loop_entry = result.data["step_results"][0]
        # Should run exactly 10 iterations (hard cap), not 99
        self.assertEqual(len(loop_entry["iterations"]), 10)

    def test_nested_loop_rejected(self):
        """Nested loop blocks are rejected with an error."""
        sup, sm = self._setup()

        workflow_plan = {
            "summary": "嵌套循环",
            "steps": [
                {
                    "type": "loop",
                    "goal": "外层目标",
                    "max_iterations": 3,
                    "steps": [
                        {
                            "type": "loop",
                            "goal": "内层目标",
                            "max_iterations": 2,
                            "steps": [
                                {"agent": "quant", "message": "内层步骤"},
                            ],
                        },
                    ],
                },
            ],
        }

        result = asyncio.run(
            sup._execute_workflow(workflow_plan, AgentMessage(user_id="u1"))
        )

        self.assertFalse(result.success)
        self.assertIn("嵌套", result.error)

    def test_loop_inner_step_abort_terminates_loop(self):
        """Inner step failure with on_failure=abort terminates the loop."""
        sup, sm = self._setup()

        mock_failing = AsyncMock()
        mock_failing.handle = AsyncMock(
            return_value=AgentResult(success=False, error="执行失败")
        )

        workflow_plan = {
            "summary": "优化",
            "steps": [
                {
                    "type": "loop",
                    "goal": "目标",
                    "max_iterations": 5,
                    "steps": [
                        {"agent": "quant", "message": "会失败的步骤"},
                    ],
                },
            ],
        }

        with patch(
            "apps.agent.registry.AgentRegistry.get",
            side_effect=make_agent_getter(quant=mock_failing),
        ):
            result = asyncio.run(
                sup._execute_workflow(workflow_plan, AgentMessage(user_id="u1"))
            )

        self.assertFalse(result.success)
        self.assertIn("失败", result.error)

    def test_loop_with_surrounding_steps(self):
        """Loop block works correctly between normal steps."""
        sup, sm = self._setup()

        mock_researcher = make_accepting_agent("调研完成")
        mock_coach = make_accepting_agent("总结完成")

        call_count = 0

        async def quant_handle(msg, on_tool_result=None):
            nonlocal call_count
            call_count += 1
            sharpe = 2.0 if call_count >= 2 else 0.5
            return AgentResult(
                success=True,
                data={"content": f"夏普={sharpe}", "metrics": {"sharpe": sharpe}},
            )

        mock_quant = AsyncMock()
        mock_quant.handle = quant_handle

        workflow_plan = {
            "summary": "完整优化流程",
            "steps": [
                {"agent": "researcher", "message": "调研方向"},
                {
                    "type": "loop",
                    "goal": "夏普 >= 1.5",
                    "goal_condition": "float(variables.get('sharpe', 0)) >= 1.5",
                    "max_iterations": 3,
                    "steps": [
                        {"agent": "quant", "message": "优化第{loop_iteration}轮"},
                    ],
                },
                {"agent": "coach", "message": "总结"},
            ],
        }

        with patch(
            "apps.agent.registry.AgentRegistry.get",
            side_effect=make_agent_getter(
                researcher=mock_researcher,
                quant=mock_quant,
                coach=mock_coach,
            ),
        ):
            result = asyncio.run(
                sup._execute_workflow(workflow_plan, AgentMessage(user_id="u1"))
            )

        self.assertTrue(result.success)
        self.assertEqual(call_count, 2)  # loop ran 2 iterations
        mock_researcher.handle.assert_called_once()
        mock_coach.handle.assert_called_once()
        # step_results: researcher, loop, coach = 3 entries
        self.assertEqual(len(result.data["step_results"]), 3)
        self.assertEqual(result.data["step_results"][1]["type"], "loop")
