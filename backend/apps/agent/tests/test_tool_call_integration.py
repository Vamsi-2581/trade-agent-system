"""Integration tests for tool_call notification in _execute_tool_call."""

import pytest
from unittest.mock import MagicMock, AsyncMock, patch


@pytest.mark.asyncio
async def test_execute_tool_call_pushes_notification():
    """Test _execute_tool_call pushes tool_call notification."""
    from apps.agent.base import BaseAgent
    from apps.agent.task_tracker import TaskTracker, tracker_context

    # Create minimal agent
    class MockAgent(BaseAgent):
        name = "test_agent"

        async def handle(self, message, on_tool_result=None):
            pass

    agent = MockAgent()
    agent._current_user_id = "user-1"

    # Setup tracker in context
    tracker = TaskTracker(task_id="test-123", user_id="user-1")
    token = tracker_context.set(tracker)
    tracker.start()

    # Mock tool call
    tc = MagicMock()
    tc.name = "get_kline_data"
    tc.arguments = {"symbol": "BTCUSDT", "interval": "1h"}

    # Mock tool execution
    with patch.object(agent, "run_tool", new_callable=AsyncMock) as mock_run_tool:
        mock_run_tool.return_value = MagicMock(
            success=True, data="K-line data retrieved"
        )

        # Mock tracker.tool_call
        with patch.object(tracker, "tool_call") as mock_tool_call:
            await agent._execute_tool_call(tc)

            # Verify tool_call was invoked with correct args
            # Note: base.py injects user_id after tool_call notification
            mock_tool_call.assert_called_once()
            call_args = mock_tool_call.call_args[0]
            assert call_args[0] == "get_kline_data"
            assert "symbol" in call_args[1]
            assert "interval" in call_args[1]

    # Cleanup
    tracker_context.reset(token)


@pytest.mark.asyncio
async def test_multiple_tool_calls_push_sequentially():
    """Test multiple tool calls in sequence push notifications in order."""
    from apps.agent.base import BaseAgent
    from apps.agent.task_tracker import TaskTracker, tracker_context

    class MockAgent(BaseAgent):
        name = "test_agent"

        async def handle(self, message, on_tool_result=None):
            pass

    agent = MockAgent()
    agent._current_user_id = "user-1"

    tracker = TaskTracker(task_id="test-123", user_id="user-1")
    token = tracker_context.set(tracker)
    tracker.start()

    # Mock multiple tool calls
    tc1 = MagicMock()
    tc1.name = "get_kline_data"
    tc1.arguments = {"symbol": "BTC"}

    tc2 = MagicMock()
    tc2.name = "get_balance"
    tc2.arguments = {"user_id": "user-1"}

    with patch.object(agent, "run_tool", new_callable=AsyncMock) as mock_run_tool:
        mock_run_tool.return_value = MagicMock(success=True, data="result")

        with patch.object(tracker, "tool_call") as mock_tool_call:
            await agent._execute_tool_call(tc1)
            await agent._execute_tool_call(tc2)

            # Verify two calls in correct order
            assert mock_tool_call.call_count == 2
            calls = mock_tool_call.call_args_list

            # First call
            assert calls[0][0][0] == "get_kline_data"
            assert "symbol" in calls[0][0][1]

            # Second call
            assert calls[1][0][0] == "get_balance"

    tracker_context.reset(token)


# ------------------------------------------------------------------ #
#  Regression: XML-formatted tool calls inside LLM content            #
# ------------------------------------------------------------------ #


class _XmlToolCallLLM:
    """Mock LLM that returns tool calls as XML text inside content.

    Round 1: returns content with <tool_call>...<function=list_strategies>...
             <arguments>...</arguments></function></tool_call>
             tool_calls=[] (LLM did NOT use native function calling).
    Round 2 (after tool result is fed back): returns a final natural-language
             content so the loop terminates.
    """

    def __init__(self):
        self.round = 0
        self.last_messages = None  # captured each round

    async def chat_with_tools(
        self, system, messages, tools, max_tokens=2048, temperature=0.3
    ):
        from apps.agent.llm_client import LLMToolResponse

        self.round += 1
        self.last_messages = list(messages)
        if self.round == 1:
            content = (
                "我来直接执行策略开发和回测。首先检查是否\n"
                "已有相似策略，然后创建策略文件。\n\n"
                "<tool_call>\n"
                "<function=list_strategies>\n"
                "<arguments>{}</arguments>\n"
                "</function>\n"
                "</tool_call>\n"
            )
            return LLMToolResponse(content=content, tool_calls=[])
        # Round 2: tool result has been appended -> produce final reply
        return LLMToolResponse(
            content="已找到现有策略 X，下一步将基于它继续开发。",
            tool_calls=[],
        )

    async def chat(self, system, user, max_tokens=1024, temperature=0.3):
        return ""


def _make_quant_agent_for_test(llm):
    """Create a minimal _LLMAgent-like instance for testing _run_tool_loop."""
    from apps.agent.sub_agents import _LLMAgent

    class _TestAgent(_LLMAgent):
        name = "test_quant_for_xml_tc"
        prompt_name = ""  # no real prompt needed
        _agent_tools: list[str] = ["list_strategies"]

        def __init__(self):
            super().__init__()
            self._llm = llm
            self._current_user_id = "u-test"
            self.executed_tool_names: list[str] = []

        async def run_tool(self, tool_name, **kwargs):
            from apps.agent.tools.base import ToolResult

            self.executed_tool_names.append(tool_name)
            return ToolResult(
                success=True,
                data='[{"name":"rsi_strategy","description":"test"}]',
            )

    return _TestAgent()


@pytest.mark.asyncio
async def test_xml_tool_call_in_content_runs_real_tool():
    """When LLM writes <tool_call> XML in content instead of native tool_calls,
    _run_tool_loop must parse it and actually execute the tool. Regression for
    'bot stopped after the first reply' bug.

    User's symptom: bot only printed "我先检查..." + an XML tool_call block; no
    list_strategies ran, so the workflow never continued.
    """
    from apps.agent.task_tracker import TaskTracker, tracker_context
    from apps.agent.llm_client import LLMClient

    llm = _XmlToolCallLLM()
    agent = _make_quant_agent_for_test(llm)

    tracker = TaskTracker(task_id="t-xml-1", user_id="u-test")
    token = tracker_context.set(tracker)
    tracker.start()
    try:
        tool_schema = {
            "type": "function",
            "function": {
                "name": "list_strategies",
                "description": "List strategies",
                "parameters": {"type": "object", "properties": {}},
            },
        }

        # _run_tool_loop calls LLMClient.get_instance() directly,
        # not self._llm — patch the singleton's classmethod.
        with patch.object(LLMClient, "get_instance", return_value=llm):
            content, is_fb = await agent._run_tool_loop(
                system="you are a quant agent",
                messages=[{"role": "user", "content": "开发一个策略"}],
                tools=[tool_schema],
                max_tokens=2048,
            )
    finally:
        tracker_context.reset(token)

    # Tool was actually executed (the bug was: it never ran)
    assert "list_strategies" in agent.executed_tool_names, (
        f"list_strategies was not executed. Executed: {agent.executed_tool_names}. "
        "This is the bug: LLM wrote <tool_call> XML in content, and _run_tool_loop "
        "returned the content without parsing it as a real tool call."
    )

    # LLM was called again (round 2) with the tool result appended to messages
    assert llm.round == 2, (
        f"Expected 2 LLM rounds (one for tool call, one for final), got {llm.round}"
    )
    last_tool_msg = next(
        (m for m in reversed(llm.last_messages) if m.get("role") == "tool"), None
    )
    assert last_tool_msg is not None, (
        "Round 2 should have received a 'tool' role message with the list_strategies result"
    )
    assert "rsi_strategy" in (last_tool_msg.get("content") or ""), (
        f"Tool result should be in round 2 messages: {last_tool_msg}"
    )

    # Final content is the round 2 natural-language reply
    assert "已找到现有策略" in content


@pytest.mark.asyncio
async def test_xml_tool_call_with_arguments():
    """XML form may carry <arguments>{...}</arguments> — must be parsed as tool args."""
    from apps.agent.llm_client import LLMToolResponse, LLMClient
    from apps.agent.sub_agents import _LLMAgent
    from apps.agent.task_tracker import TaskTracker, tracker_context
    from apps.agent.tools.base import ToolResult

    received_args: list[dict] = []

    class _LLM:
        def __init__(self):
            self.round = 0

        async def chat_with_tools(
            self, system, messages, tools, max_tokens=2048, temperature=0.3
        ):
            self.round += 1
            if self.round == 1:
                content = (
                    "让我查询一下。\n"
                    "<tool_call>\n"
                    "<function=get_kline_data>\n"
                    '<arguments>{"symbol": "BTCUSDT", "interval": "1h"}</arguments>\n'
                    "</function>\n"
                    "</tool_call>\n"
                )
                return LLMToolResponse(content=content, tool_calls=[])
            return LLMToolResponse(content="done", tool_calls=[])

        async def chat(self, system, user, max_tokens=1024, temperature=0.3):
            return ""

    class _Agent(_LLMAgent):
        name = "test_xml_args"
        _agent_tools: list[str] = ["get_kline_data"]

        def __init__(self, llm):
            super().__init__()
            self._llm = llm
            self._current_user_id = "u-test"

        async def run_tool(self, tool_name, **kwargs):
            received_args.append(kwargs)
            return ToolResult(success=True, data="kline ok")

    llm = _LLM()
    agent = _Agent(llm)
    tracker = TaskTracker(task_id="t-xml-args", user_id="u-test")
    token = tracker_context.set(tracker)
    tracker.start()
    try:
        with patch.object(LLMClient, "get_instance", return_value=llm):
            await agent._run_tool_loop(
                system="x",
                messages=[{"role": "user", "content": "q"}],
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": "get_kline_data",
                            "description": "Get kline",
                            "parameters": {"type": "object", "properties": {}},
                        },
                    }
                ],
                max_tokens=2048,
            )
    finally:
        tracker_context.reset(token)

    assert received_args, "get_kline_data was not executed"
    args = received_args[0]
    # user_id is auto-injected by _execute_tool_call (base.py:140-141), but
    # the agent-injected symbol/interval from XML must be present.
    assert args.get("symbol") == "BTCUSDT", f"symbol not parsed from XML: {args}"
    assert args.get("interval") == "1h", f"interval not parsed from XML: {args}"


# ------------------------------------------------------------------ #
#  Regression: DeepSeek DSML text-format tool calls in content        #
# ------------------------------------------------------------------ #

# Exact DSML the bot printed back to the user (17:31 message).
# DeepSeek V4 emits DSML text tool calls; markers may degrade from the
# documented full-width form (｜DSML｜) to ASCII / double-pipe / spaced forms.
SCREENSHOT_DSML = (
    "<| |DSML| |tool_call>\n"
    "<| |DSML| |parameter\n"
    'name="skill_name" string="true">quant-backtest</| |DSML| |parameter>\n'
    "</| |DSML| |tool_call>\n"
)

_LOAD_SKILL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "load_skill",
        "description": "Load a skill",
        "parameters": {
            "type": "object",
            "properties": {
                "skill_name": {"type": "string", "description": "skill name"},
            },
            "required": ["skill_name"],
        },
    },
}


def _make_plain_agent():
    from apps.agent.sub_agents import _LLMAgent
    from apps.agent.llm_client import LLMClient

    class _TestAgent(_LLMAgent):
        name = "test_dsml_tc"
        prompt_name = ""
        _agent_tools: list[str] = ["load_skill"]

        def __init__(self):
            super().__init__()
            self._llm = LLMClient.get_instance()
            self._current_user_id = "u-test"

    return _TestAgent()


def test_dsml_parser_handles_degraded_single_call():
    """The exact degraded DSML from the user's chat must parse to load_skill.
    Tool name is missing (invoke name dropped) -> inferred from the tools schema
    by parameter-name matching (only load_skill has skill_name)."""

    agent = _make_plain_agent()
    parsed = agent._try_parse_json_tool_call(SCREENSHOT_DSML, [_LOAD_SKILL_SCHEMA])

    assert parsed is not None, (
        "DSML tool call was not parsed — this is the bug: the bot returned the "
        "raw <| |DSML| |tool_call> XML as the reply instead of executing load_skill"
    )
    assert parsed["tool"] == "load_skill", f"expected load_skill, got {parsed}"
    assert parsed["args"] == {"skill_name": "quant-backtest"}, f"bad args: {parsed}"


def test_dsml_parser_handles_canonical_fullwidth_form():
    """Documented DeepSeek DSML: <｜DSML｜function_calls><｜DSML｜invoke name=...>..."""

    agent = _make_plain_agent()
    content = (
        "我先搜索几个方向。\n"
        "<｜DSML｜function_calls>\n"
        '<｜DSML｜invoke name="get_weather">\n'
        '<｜DSML｜parameter name="location" string="true">Hangzhou, China</｜DSML｜parameter>\n'
        "</｜DSML｜invoke>\n"
        "</｜DSML｜function_calls>\n"
    )
    parsed = agent._try_parse_json_tool_call(content, [])
    assert parsed == {"tool": "get_weather", "args": {"location": "Hangzhou, China"}}, (
        f"canonical full-width DSML not parsed: {parsed}"
    )


def test_dsml_parser_handles_degraded_double_pipe_compact():
    """Degraded ASCII double-pipe, compact (no newlines): <||DSML||tool_calls>..."""

    agent = _make_plain_agent()
    content = (
        "I will search several directions first.<||DSML||tool_calls>"
        '<||DSML||invoke name="doc_knowlegebase">'
        '<||DSML||parameter name="query" string="true">bond detail fields display'
        "</||DSML||parameter></||DSML||invoke></||DSML||tool_calls>"
    )
    parsed = agent._try_parse_json_tool_call(content, [])
    assert parsed == {
        "tool": "doc_knowlegebase",
        "args": {"query": "bond detail fields display"},
    }, f"degraded double-pipe DSML not parsed: {parsed}"


def test_dsml_parser_handles_markerless_function_calls():
    """Server may strip the DSML markers entirely (sglang/vLLM parser strips them)."""

    agent = _make_plain_agent()
    content = (
        "我先并行搜索几个关键方向。\n"
        "<function_calls>\n"
        '<invoke name="get_weather">\n'
        '<parameter name="location" string="true">Hangzhou, China</parameter>\n'
        "</invoke>\n"
        "</function_calls>\n"
    )
    parsed = agent._try_parse_json_tool_call(content, [])
    assert parsed == {"tool": "get_weather", "args": {"location": "Hangzhou, China"}}, (
        f"markerless function_calls DSML not parsed: {parsed}"
    )


def test_dsml_parser_ignores_plain_text():
    """Ordinary chat content must NOT be treated as a tool call."""

    agent = _make_plain_agent()
    content = "我可以帮你搜索相关信息，请告诉我你想查什么。"
    assert agent._try_parse_json_tool_call(content, []) is None


@pytest.mark.asyncio
async def test_dsml_tool_call_in_content_runs_real_tool():
    """End-to-end: LLM returns the DSML block in content (no native tool_calls),
    _run_tool_loop must parse it and actually execute load_skill — the task
    (backtest workflow) then continues instead of the bot replying with raw XML.

    Regression for: '回测任务返回了xml,任务没有完成' — the bot printed
    <| |DSML| |tool_call>...<| |DSML| |parameter name="skill_name"...>quant-backtest
    and never ran the skill.
    """
    from apps.agent.llm_client import LLMToolResponse, LLMClient
    from apps.agent.task_tracker import TaskTracker, tracker_context
    from apps.agent.tools.base import ToolResult
    from unittest.mock import AsyncMock, patch

    class _DsmlToolCallLLM:
        def __init__(self):
            self.round = 0
            self.last_messages = None

        async def chat_with_tools(
            self, system, messages, tools, max_tokens=2048, temperature=0.3
        ):
            self.round += 1
            self.last_messages = list(messages)
            if self.round == 1:
                return LLMToolResponse(content=SCREENSHOT_DSML, tool_calls=[])
            return LLMToolResponse(
                content="已加载 quant-backtest 技能，开始执行回测。", tool_calls=[]
            )

        async def chat(self, system, user, max_tokens=1024, temperature=0.3):
            return ""

    from apps.agent.sub_agents import _LLMAgent

    class _TestAgent(_LLMAgent):
        name = "test_dsml_loop"
        prompt_name = ""
        _agent_tools: list[str] = ["load_skill"]

        def __init__(self):
            super().__init__()
            self._current_user_id = "u-test"

    llm = _DsmlToolCallLLM()
    agent = _TestAgent()
    agent._llm = llm

    tracker = TaskTracker(task_id="t-dsml-1", user_id="u-test")
    token = tracker_context.set(tracker)
    tracker.start()
    try:
        with patch.object(LLMClient, "get_instance", return_value=llm):
            with patch("apps.agent.tools.load_skill.LoadSkillTool") as mock_cls:
                instance = mock_cls.return_value
                instance.execute = AsyncMock(
                    return_value=ToolResult(
                        success=True, data="# quant-backtest: full skill content"
                    )
                )
                content, is_fb = await agent._run_tool_loop(
                    system="you are a quant agent",
                    messages=[{"role": "user", "content": "回测如下策略: ..."}],
                    tools=[_LOAD_SKILL_SCHEMA],
                    max_tokens=2048,
                )
    finally:
        tracker_context.reset(token)

    # The tool was actually executed — the bug was: it never ran, the raw DSML
    # XML went straight to the user as the reply.
    assert instance.execute.await_count == 1, (
        "load_skill was not executed. This is the bug: LLM wrote DSML tool call "
        "in content, and _run_tool_loop returned the content without parsing it."
    )
    assert instance.execute.call_args.kwargs.get("skill_name") == "quant-backtest", (
        f"skill_name not parsed from DSML: {instance.execute.call_args}"
    )

    # LLM was called again (round 2) with the tool result appended
    assert llm.round == 2, f"expected 2 LLM rounds, got {llm.round}"
    last_tool_msg = next(
        (m for m in reversed(llm.last_messages) if m.get("role") == "tool"), None
    )
    assert last_tool_msg is not None, "round 2 should receive the tool result"
    assert "quant-backtest: full skill content" in (last_tool_msg.get("content") or "")

    # Final reply is the round-2 natural-language text, not the DSML XML
    assert content and "已加载 quant-backtest 技能" in content, (
        f"final content should be the natural-language summary, got: {content!r}"
    )
