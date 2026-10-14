"""Registry[T] 泛型后端的单元测试。

验证 HTML 架构审查任务 2 的核心收益：
  1. 通用逻辑只在一处（register/get/reset）
  2. 实例之间天然隔离（测试无需清理全局态）
  3. 现有类级 API 的 reset() 与实例 reset() 行为一致
"""

from __future__ import annotations


from apps.common.registry import Registry


class TestRegistryIsolation:
    """每个 Registry 实例独立持有状态。"""

    def test_instances_do_not_share_state(self):
        a = Registry("a")
        b = Registry("b")
        a.register("x", 1)
        b.register("y", 2)

        assert a.get("x") == 1
        assert a.get("y") is None
        assert b.get("x") is None
        assert b.get("y") == 2

    def test_reset_only_affects_own_instance(self):
        a = Registry("a")
        b = Registry("b")
        a.register("x", 1)
        b.register("y", 2)

        a.reset()

        assert a.get("x") is None
        assert b.get("y") == 2  # 不受影响


class TestRegistryAPI:
    """核心 API 的正确性。"""

    def test_register_and_get(self):
        r = Registry("r")
        r.register("k", "value")
        assert r.get("k") == "value"
        assert "k" in r
        assert "missing" not in r

    def test_register_no_overwrite_by_default(self):
        r = Registry("r")
        r.register("k", "first")
        r.register("k", "second")
        assert r.get("k") == "first"

    def test_register_overwrite_when_explicit(self):
        r = Registry("r")
        r.register("k", "first")
        r.register("k", "second", overwrite=True)
        assert r.get("k") == "second"

    def test_unregister(self):
        r = Registry("r")
        r.register("k", "v")
        r.unregister("k")
        assert r.get("k") is None

    def test_unregister_missing_is_noop(self):
        r = Registry("r")
        r.unregister("missing")  # should not raise

    def test_all_and_keys(self):
        r = Registry("r")
        r.register("a", 1)
        r.register("b", 2)
        assert set(r.all()) == {1, 2}
        assert set(r.keys()) == {"a", "b"}

    def test_len_and_iter(self):
        r = Registry("r")
        r.register("a", 1)
        r.register("b", 2)
        assert len(r) == 2
        assert list(r) == [1, 2]

    def test_clear_is_alias_for_reset(self):
        r = Registry("r")
        r.register("k", "v")
        r.clear()
        assert r.get("k") is None


class TestClassLevelRegistryReset:
    """现有的类级别 Registry（AgentRegistry / ToolRegistry / SkillRegistry）
    的 reset() 与 legacy 三行清理等价。
    """

    def test_agent_registry_reset_equivalent_to_legacy(self):
        from apps.agent.registry import AgentRegistry

        # 注册一些状态
        AgentRegistry.register_dynamic(
            "legacy_test_agent", type("A", (), {"name": "legacy_test_agent"})
        )
        assert "legacy_test_agent" in AgentRegistry._classes

        # reset() 应清空 _classes、_registry、_discovered
        AgentRegistry.reset()
        assert "legacy_test_agent" not in AgentRegistry._classes
        assert len(AgentRegistry._registry) == 0
        assert AgentRegistry._discovered is False

    def test_tool_registry_reset_clears_all(self):
        from apps.agent.tools.base import BaseTool, ToolRegistry, ToolResult

        class TempTool(BaseTool):
            name = "temp_tool_xyz"
            description = "temp"

            @property
            def parameters_schema(self):
                return {}

            async def execute(self, **kw):
                return ToolResult(True)

        ToolRegistry.register(TempTool())
        assert ToolRegistry.get("temp_tool_xyz") is not None

        ToolRegistry.reset()
        assert ToolRegistry.get("temp_tool_xyz") is None

    def test_skill_registry_reset_clears_all(self):
        from apps.skill.registry import SkillRegistry

        # 注册一个临时 skill 类
        class TempSkill:
            name = "temp_skill_xyz"

        SkillRegistry._backend.register("temp_skill_xyz", TempSkill, overwrite=True)
        assert SkillRegistry.get("temp_skill_xyz") is not None

        SkillRegistry.reset()
        assert SkillRegistry.get("temp_skill_xyz") is None
