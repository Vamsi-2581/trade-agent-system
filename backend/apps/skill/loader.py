"""Agent Skills Loader — loads SKILL.md files for agent context injection."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Workspace root: ~/.tradelogx/
TRADELOGX_ROOT = Path.home() / ".tradelogx"
# Global shared skills directory
GLOBAL_SKILLS_DIR = TRADELOGX_ROOT / "skills"
# Agent workspace root: ~/.tradelogx/workspace/{agent_name}/
WORKSPACE_ROOT = TRADELOGX_ROOT / "workspace"


@dataclass
class SkillMetadata:
    """Parsed frontmatter from a SKILL.md file."""

    name: str = ""
    description: str = ""
    when_to_use: str = ""
    references: list[str] = field(default_factory=list)
    license: str = ""
    # Raw unrecognised fields
    extra: dict[str, Any] = field(default_factory=dict)


class AgentSkillsLoader:
    def __init__(self, agent_name: str = "", search_root: Path | None = None):
        self.agent_name = agent_name

        if search_root is not None:
            # Legacy / custom override — treat as global skills dir only
            self.global_skills_dir = search_root
            self.agent_skills_dir: Path | None = None
        else:
            self.global_skills_dir = GLOBAL_SKILLS_DIR
            self.agent_skills_dir = (
                WORKSPACE_ROOT / agent_name / "skills" if agent_name else None
            )

    #  Directory helpers                                                   #

    def _iter_skill_dirs(self) -> list[tuple[Path, str]]:
        """
        Return (skill_dir, source_label) pairs from all directories.

        Priority: agent-specific shadows global.
        """
        seen: dict[str, tuple[Path, str]] = {}

        # 1. Global shared skills
        if self.global_skills_dir.exists():
            for skill_dir in sorted(self.global_skills_dir.iterdir()):
                if skill_dir.is_dir() and (skill_dir / "SKILL.md").exists():
                    seen[skill_dir.name] = (skill_dir, "global")

        # 2. Agent-specific skills (higher priority)
        if self.agent_skills_dir and self.agent_skills_dir.exists():
            for skill_dir in sorted(self.agent_skills_dir.iterdir()):
                if skill_dir.is_dir() and (skill_dir / "SKILL.md").exists():
                    seen[skill_dir.name] = (skill_dir, "agent")

        return list(seen.values())

    #  Public API                                                          #

    def list_skills(self) -> list[dict[str, Any]]:
        """
        List all available skills with their metadata.

        Returns:
            List of dicts with 'name', 'path', 'description', 'when_to_use',
            'source', 'available'.
        """
        result: list[dict[str, Any]] = []

        for skill_dir, source in self._iter_skill_dirs():
            skill_file = skill_dir / "SKILL.md"
            meta = self._parse_frontmatter(skill_file.read_text(encoding="utf-8"))
            result.append(
                {
                    "name": skill_dir.name,
                    "path": str(skill_file),
                    "description": meta.description or skill_dir.name,
                    "when_to_use": meta.when_to_use,
                    "references": meta.references,
                    "source": source,
                    "available": True,
                }
            )

        return result

    def load_skill(self, name: str) -> str | None:
        """
        Load the full content of a skill by name.

        Agent-specific skill takes precedence over global skill with same name.

        Args:
            name: Skill directory name.

        Returns:
            Raw SKILL.md content (including frontmatter), or None if not found.
        """
        # Agent-specific first
        if self.agent_skills_dir:
            agent_skill = self.agent_skills_dir / name / "SKILL.md"
            if agent_skill.exists():
                return agent_skill.read_text(encoding="utf-8")

        # Fallback to global
        global_skill = self.global_skills_dir / name / "SKILL.md"
        if global_skill.exists():
            return global_skill.read_text(encoding="utf-8")

        return None

    def skill_dir(self, name: str) -> Path | None:
        """返回技能目录（agent 专属优先，其次全局）；不存在返回 None。"""
        if self.agent_skills_dir:
            d = self.agent_skills_dir / name
            if (d / "SKILL.md").exists():
                return d
        d = self.global_skills_dir / name
        if (d / "SKILL.md").exists():
            return d
        return None

    def load_skills_content(self, names: list[str]) -> str:
        """
        Load and format multiple skills for context injection.

        Args:
            names: List of skill names to load.

        Returns:
            Concatenated skills block.
        """
        parts: list[str] = []
        for name in names:
            content = self.load_skill(name)
            if content:
                parts.append(
                    self._format_skill_block(name, content, self.skill_dir(name))
                )
        return "\n\n---\n\n".join(parts)

    def build_summary(self, exclude_always: bool = True) -> str:
        """
        Build an XML summary of skills for progressive loading.

        The agent reads this to decide which skills to load for the current task.

        Args:
            exclude_always: If True, exclude skills marked as always=true
                           (those are already injected in full).

        Returns:
            XML-formatted skills inventory.
        """
        skills = self.list_skills()

        if exclude_always:
            always_names = set(self.get_always_skills())
            skills = [s for s in skills if s["name"] not in always_names]
        if not skills:
            return ""

        def esc(s: str) -> str:
            return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        lines = ["<agent_skills>"]
        for s in skills:
            name = esc(s["name"])
            desc = esc(s["description"])
            when = esc(s.get("when_to_use", ""))
            src = s["source"]
            dir_attr = esc(self._display_dir(Path(s["path"]).parent))

            lines.append(f'  <skill name="{name}" source="{src}" dir="{dir_attr}">')
            lines.append(f"    <description>{desc}</description>")
            if when:
                lines.append(f"    <when_to_use>{when}</when_to_use>")
            lines.append("  </skill>")
        lines.append("</agent_skills>")

        return "\n".join(lines)

    def get_always_skills(self) -> list[str]:
        """
        Skills that should always be injected (based on frontmatter 'always' flag).

        Returns:
            List of skill names marked always=true.
        """
        result: list[str] = []
        for s in self.list_skills():
            content = self.load_skill(s["name"])
            if content:
                meta = self._parse_frontmatter(content)
                # Check for 'always' in extra fields (nanobot / Claude Code skill format)
                if meta.extra.get("always"):
                    result.append(s["name"])
        return result

    #  Internal helpers                                                    #

    @staticmethod
    def _display_dir(skill_dir: Path) -> str:
        """技能目录的 read_file 友好路径：相对 ~/.tradelogx/，否则绝对路径。"""
        try:
            return skill_dir.resolve().relative_to(TRADELOGX_ROOT.resolve()).as_posix()
        except ValueError:
            return str(skill_dir)

    def _format_skill_block(
        self, name: str, content: str, base_dir: Path | None = None
    ) -> str:
        """Strip frontmatter and wrap in a named section; attach skill dir for reference paths."""
        body = self._strip_frontmatter(content)
        header = f"### Skill: {name}"
        if base_dir is not None:
            rel = self._display_dir(base_dir)
            header += (
                f"\n\n> **技能目录**: `{rel}`（read_file 用路径，相对 ~/.tradelogx/）。"
                "技能正文中的相对路径（如 `references/xxx.md`）"
                f"必须以 `{rel}/<相对路径>` 形式传给 read_file，"
                f'例: `read_file(file_path="{rel}/references/api-reference.md", agent_name="{self.agent_name}")`。'
            )
        return f"{header}\n\n{body}"

    def _strip_frontmatter(self, content: str) -> str:
        """Remove YAML frontmatter block from markdown content."""
        if content.startswith("---"):
            match = re.match(r"^---\n.*?\n---\n", content, re.DOTALL)
            if match:
                return content[match.end() :].strip()
        return content.strip()

    def _parse_frontmatter(self, content: str) -> SkillMetadata:
        """Parse YAML frontmatter into a SkillMetadata dataclass."""
        meta = SkillMetadata()
        if not content.startswith("---"):
            return meta

        match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
        if not match:
            return meta

        for line in match.group(1).split("\n"):
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip().strip("\"'")

            if not value:
                continue

            if key == "name":
                meta.name = value
            elif key == "description":
                meta.description = value
            elif key == "when_to_use":
                meta.when_to_use = value
            elif key == "references":
                # Multi-line list: lines starting with '  - '
                pass  # Handled below
            elif key == "license":
                meta.license = value
            else:
                # Collect extra fields for extensibility
                if value.lower() in ("true", "false"):
                    meta.extra[key] = value.lower() == "true"
                elif value.isdigit():
                    meta.extra[key] = int(value)
                else:
                    meta.extra[key] = value

        # Parse references list (indented '- ' lines after 'references:')
        ref_section_match = re.search(
            r"^references:\s*\n((?:\s+- .+(?:\n|$))*)",
            match.group(1),
            re.MULTILINE,
        )
        if ref_section_match:
            for ref_line in ref_section_match.group(1).split("\n"):
                ref = ref_line.strip().lstrip("- ").strip()
                if ref:
                    meta.references.append(ref)

        return meta

    def resolve_references(self, skill_names: list[str]) -> list[str]:
        """
        Resolve skill references (from frontmatter) to full skill names.

        For skills that reference other skills, expand them recursively.

        Args:
            skill_names: Starting skill names.

        Returns:
            Deduplicated list of all referenced skill names.
        """
        seen: set[str] = set()
        stack = list(skill_names)

        while stack:
            name = stack.pop()
            if name in seen:
                continue
            seen.add(name)

            content = self.load_skill(name)
            if content:
                meta = self._parse_frontmatter(content)
                for ref in meta.references:
                    # References can be paths like '.agents/skills/quant-logic-verification'
                    # or just skill names. Extract the skill dir name.
                    ref_path = Path(ref)
                    if ref_path.suffix in (".md", ".txt"):
                        ref_name = ref_path.stem
                    else:
                        ref_name = ref_path.name
                    if ref_name and ref_name not in seen and self.load_skill(ref_name):
                        stack.append(ref_name)
                        stack.append(ref_name)

        return list(seen)


# Per-agent loader cache
_loader_cache: dict[str, AgentSkillsLoader] = {}


def get_skills_loader(agent_name: str = "") -> AgentSkillsLoader:
    if agent_name not in _loader_cache:
        _loader_cache[agent_name] = AgentSkillsLoader(agent_name=agent_name)
    return _loader_cache[agent_name]
