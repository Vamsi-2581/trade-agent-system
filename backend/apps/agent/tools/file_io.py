"""File I/O tools — read and write files in the workspace."""

from __future__ import annotations

import logging
from pathlib import Path

from .base import BaseTool, ToolResult

logger = logging.getLogger(__name__)

# Default workspace root: ~/.tradelogx/
WORKSPACE_ROOT = Path.home() / ".tradelogx"


class ReadFileTool(BaseTool):
    """
    Read the content of a file from the agent workspace.
    Paths are relative to the workspace root: ~/.tradelogx/workspace/{agent_name}/
    """

    name = "read_file"
    description = (
        "读取工作区中的文件内容。"
        "路径相对于 ~/.tradelogx/ 目录。"
        "如果文件不存在或路径无效，返回错误。"
    )

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "要读取的文件路径（相对于 workspace 根目录）",
                },
                "agent_name": {
                    "type": "string",
                    "description": "所属 agent 名称，用于定位工作区",
                },
                "max_chars": {
                    "type": "integer",
                    "description": "最大读取字符数，默认 10000",
                    "default": 10000,
                    "minimum": 1,
                    "maximum": 50000,
                },
            },
            "required": ["file_path", "agent_name"],
        }

    async def execute(
        self,
        file_path: str = "",
        agent_name: str = "",
        max_chars: int = 10000,
        **kwargs,
    ) -> ToolResult:
        if not file_path:
            return ToolResult(success=False, error="file_path 参数缺失")
        if not agent_name:
            return ToolResult(success=False, error="agent_name 参数缺失")

        try:
            # Resolve path relative to agent workspace
            base = WORKSPACE_ROOT  # / agent_name
            # Allow absolute paths for safety, but prefer relative
            if Path(file_path).is_absolute():
                full_path = Path(file_path)
            else:
                full_path = (base / file_path).resolve()

            # Security: ensure path is within workspace
            try:
                full_path.relative_to(base.resolve())
            except ValueError:
                return ToolResult(
                    success=False,
                    error=f"路径越界，只允许访问 {base} 目录下的文件",
                )

            if not full_path.exists():
                return ToolResult(
                    success=False,
                    error=f"文件不存在: {full_path}",
                )

            if not full_path.is_file():
                return ToolResult(
                    success=False,
                    error=f"路径不是文件: {full_path}",
                )

            content = full_path.read_text(encoding="utf-8")
            truncated = len(content) > max_chars
            if truncated:
                content = content[:max_chars]

            return ToolResult(
                success=True,
                data={
                    "content": content,
                    "truncated": truncated,
                    "total_chars": len(full_path.read_text(encoding="utf-8")),
                    "file_path": str(full_path),
                    "line_count": len(content.splitlines()),
                },
            )

        except UnicodeDecodeError:
            return ToolResult(success=False, error="文件不是 UTF-8 编码，无法读取")
        except Exception as e:
            logger.error("[ReadFileTool] error: %s", e)
            return ToolResult(success=False, error=f"读取文件失败: {e}")


class ListDirectoryTool(BaseTool):
    """列出目录下的文件与子目录（路径相对 ~/.tradelogx/）。"""

    name = "list_directory"
    description = (
        "列出目录内容（文件带大小、子目录带 / 后缀）。"
        "路径相对于 ~/.tradelogx/，也接受绝对路径。"
        "示例: 'strategies/' 查看已有策略文件；"
        "'workspace/quant/skills/create-strategy/references/' 查看技能参考文件。"
    )

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "目录路径，相对 ~/.tradelogx/（如 'strategies/'），也接受绝对路径",
                }
            },
            "required": ["path"],
        }

    async def execute(self, path: str = "", **kwargs) -> ToolResult:
        if not path or not str(path).strip():
            return ToolResult(success=False, error="path 不能为空")
        raw = str(path).strip()
        target = Path(raw).expanduser()
        if not target.is_absolute():
            target = WORKSPACE_ROOT / raw
        if not target.exists():
            return ToolResult(success=False, error=f"路径不存在: {target}")
        if not target.is_dir():
            return ToolResult(success=False, error=f"不是目录: {target}")
        try:
            entries: list[str] = []
            for entry in sorted(target.iterdir(), key=lambda e: (e.is_file(), e.name)):
                if entry.name.startswith("."):
                    continue
                if entry.is_dir():
                    entries.append(f"{entry.name}/")
                else:
                    try:
                        entries.append(f"{entry.name}  ({entry.stat().st_size} B)")
                    except OSError:
                        entries.append(entry.name)
        except PermissionError:
            return ToolResult(success=False, error=f"无权限读取目录 {target}")
        if not entries:
            return ToolResult(success=True, data=f"目录 {target} 为空")
        limit = 200
        shown = entries[:limit]
        more = f"\n… 还有 {len(entries) - limit} 项" if len(entries) > limit else ""
        data = f"目录 {target} 共 {len(entries)} 项:\n" + "\n".join(shown) + more
        return ToolResult(success=True, data=data)


class WriteFileTool(BaseTool):
    """
    Write content to a file in the agent workspace.
    Paths are relative to the workspace root: ~/.tradelogx/
    """

    name = "write_file"
    description = (
        "将内容写入工作区中的文件。"
        "路径相对于 ~/.tradelogx/ 目录。"
        "如果父目录不存在，会自动创建。"
    )

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "要写入的文件路径（相对于 workspace 根目录）",
                },
                "agent_name": {
                    "type": "string",
                    "description": "所属 agent 名称，用于定位工作区",
                },
                "content": {
                    "type": "string",
                    "description": "要写入的文件内容",
                },
                "append": {
                    "type": "boolean",
                    "description": "是否追加模式（追加到文件末尾），默认 False（覆盖）",
                    "default": False,
                },
            },
            "required": ["file_path", "agent_name", "content"],
        }

    async def execute(
        self,
        file_path: str = "",
        agent_name: str = "",
        content: str = "",
        append: bool = False,
        **kwargs,
    ) -> ToolResult:
        if not file_path:
            # 自愈错误信息：把实际收到的参数展示给模型，便于下一轮自我修正
            # （2026-09-18: qwen3.8 在长上下文中曾连续多轮漏传 file_path，
            # 仅报"参数缺失"模型无法定位问题）
            got = {
                k: (f"<str {len(v)} chars>" if isinstance(v, str) else repr(v))
                for k, v in kwargs.items()
                if k != "user_id"
            }
            if content:
                got["content"] = f"<str {len(content)} chars>"
            elif "content" not in got:
                got["content"] = repr(content)
            return ToolResult(
                success=False,
                error=(
                    "file_path 参数缺失（必需: file_path, agent_name, content；"
                    f"实际收到: {got or '{}'}）。请重新调用并带上 file_path。"
                ),
            )
        if not agent_name:
            return ToolResult(success=False, error="agent_name 参数缺失")
        if content is None:
            return ToolResult(success=False, error="content 参数缺失")

        try:
            # Resolve path relative to agent workspace
            base = WORKSPACE_ROOT  # / agent_name
            if Path(file_path).is_absolute():
                full_path = Path(file_path)
            else:
                full_path = (base / file_path).resolve()

            # Security: ensure path is within workspace
            try:
                full_path.relative_to(base.resolve())
            except ValueError:
                return ToolResult(
                    success=False,
                    error=f"路径越界，只允许写入 {base} 目录下的文件",
                )

            # Create parent directories if needed
            full_path.parent.mkdir(parents=True, exist_ok=True)

            mode = "a" if append else "w"
            with open(full_path, mode, encoding="utf-8") as f:
                f.write(content)

            return ToolResult(
                success=True,
                data={
                    "file_path": str(full_path),
                    "bytes_written": len(content.encode("utf-8")),
                    "lines_written": len(content.splitlines()),
                    "append": append,
                },
            )

        except Exception as e:
            logger.error("[WriteFileTool] error: %s", e)
            return ToolResult(success=False, error=f"写入文件失败: {e}")
