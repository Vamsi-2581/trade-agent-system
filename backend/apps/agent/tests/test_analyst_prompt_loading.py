"""[DEBUG-bugbox] Regression: analyst prompt has no .txt suffix so list_agents
skips it, AnalystAgent never gets registered, detect_box_range tool is never
injected into the LLM tools schema.

This is the user-reported symptom: when user asks 'is BNB currently ranging',
the AnalystAgent either:
  (a) does not exist → supervisor falls back to free_chat / no tool
  (b) exists but lacks detect_box_range → LLM answers from its prior knowledge
      instead of calling the tool.

Both branches are covered by this test.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


# Ensure Django settings are loaded before importing agent modules
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings.dev")
# Add backend to path so direct pytest run also works
BACKEND_ROOT = Path(__file__).resolve().parents[3]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


@pytest.fixture(autouse=True)
def _reset_caches():
    """Reset all in-process caches that survive across tests.

    After reset, manually re-instantiate every tool module that the
    auto-discovery in apps.agent.tools.__init__ would register, so the
    ToolRegistry is repopulated for the test (the side-effecting import
    only runs once per process).
    """
    from apps.agent.registry import AgentRegistry
    from apps.agent.prompt_loader import _cache, _meta_cache
    from apps.agent.tools.base import BaseTool, ToolRegistry

    AgentRegistry.reset()
    _cache.clear()
    _meta_cache.clear()
    ToolRegistry.reset()

    # Re-register all tool classes manually (mirrors __init__.py auto-discovery)
    from pathlib import Path

    tools_dir = Path(__file__).resolve().parents[1] / "tools"
    import importlib

    for f in tools_dir.glob("*.py"):
        if f.name.startswith(("_", "test")) or f.name in ("base.py", "__init__.py"):
            continue
        mod = importlib.import_module(f"apps.agent.tools.{f.stem}")
        for attr in dir(mod):
            cls = getattr(mod, attr)
            if (
                isinstance(cls, type)
                and issubclass(cls, BaseTool)
                and cls is not BaseTool
            ):
                ToolRegistry.register(cls())

    yield
    AgentRegistry.reset()
    _cache.clear()
    _meta_cache.clear()
    ToolRegistry.reset()


def test_analyst_prompt_is_discovered():
    """[DEBUG-bugbox] analyst prompt must be discovered by list_agents."""
    from apps.agent.prompt_loader import PromptLoader

    agents = PromptLoader.list_agents()
    names = [a["name"] for a in agents]
    assert "analyst" in names, (
        f"analyst agent not discovered. Found: {names}. "
        "This means the prompt file lacks .txt suffix and is skipped by glob('*.txt')."
    )


def test_analyst_agent_registers_detect_box_range_tool():
    """[DEBUG-bugbox] AnalystAgent's tools schema must include detect_box_range."""
    from apps.agent.registry import AgentRegistry

    # Force discovery
    AgentRegistry.discover_from_prompts()

    analyst = AgentRegistry.get("analyst")

    # Build the schema that would actually be sent to the LLM
    tools = analyst._get_tools_schema()
    tool_names = [t["function"]["name"] for t in tools]

    assert "detect_box_range" in tool_names, (
        f"detect_box_range not in analyst's tool schema. "
        f"Tools available: {tool_names}. "
        "Root cause: either analyst prompt is not loaded, or its frontmatter "
        "tools list is missing detect_box_range."
    )
