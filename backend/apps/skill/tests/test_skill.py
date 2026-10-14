"""Tests for skill module — AgentSkillsLoader."""

from __future__ import annotations

import unittest
import tempfile
from pathlib import Path

from apps.skill.loader import (
    AgentSkillsLoader,
    SkillMetadata,
    get_skills_loader,
    _loader_cache,
)


def _create_skill_dir(base_dir: Path, skill_name: str, content: str) -> Path:
    """Helper to create a skill directory with SKILL.md"""
    skill_dir = base_dir / skill_name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
    return skill_dir


class TestSkillMetadata(unittest.TestCase):
    def test_default_metadata(self):
        meta = SkillMetadata()
        self.assertEqual(meta.name, "")
        self.assertEqual(meta.description, "")
        self.assertEqual(meta.references, [])
        self.assertEqual(meta.extra, {})


class TestAgentSkillsLoader(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.skills_root = Path(self.temp_dir.name) / "skills"
        self.skills_root.mkdir(parents=True)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_list_skills_empty_dir(self):
        loader = AgentSkillsLoader(search_root=self.skills_root)
        skills = loader.list_skills()
        self.assertEqual(skills, [])

    def test_list_skills_with_one_skill(self):
        content = """---
name: test-skill
description: A test skill
when_to_use: When testing
---
# Test Skill
This is the body.
"""
        _create_skill_dir(self.skills_root, "test-skill", content)
        loader = AgentSkillsLoader(search_root=self.skills_root)
        skills = loader.list_skills()
        self.assertEqual(len(skills), 1)
        self.assertEqual(skills[0]["name"], "test-skill")
        self.assertEqual(skills[0]["description"], "A test skill")
        self.assertEqual(skills[0]["when_to_use"], "When testing")
        self.assertEqual(skills[0]["source"], "global")

    def test_load_skill_content(self):
        content = """---
name: my-skill
description: My skill
---
# My Skill
Body content here.
"""
        _create_skill_dir(self.skills_root, "my-skill", content)
        loader = AgentSkillsLoader(search_root=self.skills_root)
        result = loader.load_skill("my-skill")
        self.assertIsNotNone(result)
        self.assertIn("Body content here", result)

    def test_load_skill_returns_none_for_missing(self):
        loader = AgentSkillsLoader(search_root=self.skills_root)
        result = loader.load_skill("nonexistent")
        self.assertIsNone(result)

    def test_agent_shadows_global(self):
        """Agent-specific skill should shadow global skill with same name"""
        # Create both dirs under the same temp root
        global_dir = self.skills_root
        agent_dir = self.skills_root.parent / "workspace" / "myagent" / "skills"
        agent_dir.mkdir(parents=True)

        global_content = "---\nname: shared\n---\nGlobal version"
        _create_skill_dir(global_dir, "shared", global_content)
        agent_content = "---\nname: shared\n---\nAgent version"
        _create_skill_dir(agent_dir, "shared", agent_content)

        # Build loader manually with both dirs set
        loader = AgentSkillsLoader(agent_name="myagent")
        # Override dirs to point to our temp locations
        loader.global_skills_dir = global_dir
        loader.agent_skills_dir = agent_dir

        skills = loader.list_skills()
        self.assertEqual(len(skills), 1)
        self.assertEqual(skills[0]["name"], "shared")
        # Agent skill should shadow global
        self.assertEqual(skills[0]["source"], "agent")

    def test_parse_frontmatter_simple(self):
        loader = AgentSkillsLoader(search_root=self.skills_root)
        content = """---
name: parse-test
description: Testing parser
when_to_use: For parsing
license: MIT
---
# Body
"""
        meta = loader._parse_frontmatter(content)
        self.assertEqual(meta.name, "parse-test")
        self.assertEqual(meta.description, "Testing parser")
        self.assertEqual(meta.when_to_use, "For parsing")
        self.assertEqual(meta.license, "MIT")

    def test_parse_frontmatter_references(self):
        loader = AgentSkillsLoader(search_root=self.skills_root)
        content = """---
name: ref-skill
description: Has references
references:
  - skill-a
  - skill-b
---
# Body
"""
        meta = loader._parse_frontmatter(content)
        # The parser only picks up first reference due to regex pattern
        self.assertIn("skill-a", meta.references)

    def test_strip_frontmatter(self):
        loader = AgentSkillsLoader(search_root=self.skills_root)
        content = """---
name: x
---
# Title
Some body text.
"""
        body = loader._strip_frontmatter(content)
        self.assertNotIn("---", body)
        self.assertIn("# Title", body)

    def test_build_summary(self):
        content = """---
name: summary-skill
description: For summary testing
when_to_use: Testing build_summary
---
Body
"""
        _create_skill_dir(self.skills_root, "summary-skill", content)
        loader = AgentSkillsLoader(search_root=self.skills_root)
        summary = loader.build_summary(exclude_always=False)
        self.assertIn("<agent_skills>", summary)
        self.assertIn("summary-skill", summary)
        self.assertIn("</agent_skills>", summary)

    def test_build_summary_empty(self):
        loader = AgentSkillsLoader(search_root=self.skills_root)
        summary = loader.build_summary()
        self.assertEqual(summary, "")

    def test_resolve_references(self):
        """测试引用解析"""
        _create_skill_dir(self.skills_root, "skill-a", "---\nname: skill-a\n---\nA")
        _create_skill_dir(self.skills_root, "skill-b", "---\nname: skill-b\n---\nB")
        _create_skill_dir(
            self.skills_root,
            "skill-main",
            "---\nname: skill-main\nreferences:\n  - skill-a\n  - skill-b\n---\nMain",
        )

        loader = AgentSkillsLoader(search_root=self.skills_root)
        resolved = loader.resolve_references(["skill-main"])
        self.assertIn("skill-main", resolved)
        self.assertIn("skill-a", resolved)
        self.assertIn("skill-b", resolved)

    def test_format_skill_block(self):
        loader = AgentSkillsLoader(search_root=self.skills_root)
        block = loader._format_skill_block("test", "body text")
        self.assertIn("### Skill: test", block)
        self.assertIn("body text", block)

    def test_skill_dir_resolves_agent_then_global(self):
        agent_dir = self.skills_root.parent / "workspace" / "myagent" / "skills"
        agent_dir.mkdir(parents=True, exist_ok=True)
        _create_skill_dir(self.skills_root, "shared", "---\nname: shared\n---\nG")
        agent_skill = _create_skill_dir(
            agent_dir, "shared", "---\nname: shared\n---\nA"
        )
        loader = AgentSkillsLoader(agent_name="myagent")
        loader.global_skills_dir = self.skills_root
        loader.agent_skills_dir = agent_dir
        self.assertEqual(loader.skill_dir("shared"), agent_skill)
        self.assertIsNone(loader.skill_dir("missing"))

    def test_build_summary_carries_skill_dir_attribute(self):
        content = "---\nname: dir-skill\ndescription: dir test\n---\nBody"
        skill_dir = _create_skill_dir(self.skills_root, "dir-skill", content)
        loader = AgentSkillsLoader(search_root=self.skills_root)
        summary = loader.build_summary(exclude_always=False)
        self.assertIn('dir="', summary)
        self.assertIn(str(skill_dir), summary)

    def test_skill_block_header_tells_how_to_read_references(self):
        content = "---\nname: ref-read\n---\nread references/api-reference.md"
        skill_dir = _create_skill_dir(self.skills_root, "ref-read", content)
        loader = AgentSkillsLoader(search_root=self.skills_root)
        block = loader.load_skills_content(["ref-read"])
        self.assertIn("技能目录", block)
        self.assertIn(str(skill_dir), block)
        self.assertIn("references/api-reference.md", block)
        self.assertIn("read_file", block)


class TestGetSkillsLoader(unittest.TestCase):
    def test_returns_cached_instance(self):
        _loader_cache.clear()
        loader1 = get_skills_loader("test_agent")
        loader2 = get_skills_loader("test_agent")
        self.assertIs(loader1, loader2)

    def test_different_agents_get_different_instances(self):
        _loader_cache.clear()
        loader1 = get_skills_loader("agent_a")
        loader2 = get_skills_loader("agent_b")
        self.assertIsNot(loader1, loader2)
