from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from apps.common.time_utils import business_now, business_tz_label

PROMPT_BASE = Path.home() / ".tradelogx" / "agents"

_cache: dict[str, str] = {}
_meta_cache: dict[str, dict[str, Any]] = {}


def _current_datetime_label() -> str:
    """注入 prompt 的「当前时间」：业务时区（北京时间）+ 明确时区标注。

    只给一个钟点并标注清楚时区，避免出现两个数字让 LLM 挑错。
    """
    return f"{business_now():%Y-%m-%d %H:%M:%S}（{business_tz_label()}）"


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """解析 YAML frontmatter，返回 (metadata, body)。"""
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if not match:
        return {}, text

    frontmatter = match.group(1)
    body = text[match.end() :]
    meta: dict[str, Any] = {}

    # 合并多行值（包含未闭合的 [ 的行，继续拼接直到遇到 ]）
    raw_lines = frontmatter.splitlines()
    merged_lines: list[str] = []
    for line in raw_lines:
        stripped = line.strip()
        # Only merge into previous line if it has an unclosed bracket
        if merged_lines and "[" in merged_lines[-1] and "]" not in merged_lines[-1]:
            merged_lines[-1] = merged_lines[-1] + " " + stripped
        else:
            merged_lines.append(stripped)

    for line in merged_lines:
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()

        # 解析列表: [a, b, c]
        if value.startswith("[") and value.endswith("]"):
            items = value[1:-1].split(",")
            meta[key] = [i.strip().strip('"').strip("'") for i in items if i.strip()]
        # 解析布尔
        elif value.lower() == "true":
            meta[key] = True
        elif value.lower() == "false":
            meta[key] = False
        # 解析数字
        elif value.isdigit():
            meta[key] = int(value)
        else:
            meta[key] = value.strip('"').strip("'")

    return meta, body


class PromptLoader:
    @classmethod
    def load(cls, name: str, version: str = "v1") -> str:
        """
        加载指定版本的Prompt文件。
        变更必须新建版本目录（ADR-004），可追溯可回滚。
        动态注入当前时间，防止LLM使用过期信息。
        自动剥离 YAML frontmatter。
        """
        cache_key = f"{version}/{name}"
        if cache_key not in _cache:
            file_path = PROMPT_BASE / version / f"{name}.txt"
            if not file_path.exists():
                raise FileNotFoundError(f"Prompt not found: {file_path}")
            raw_text = file_path.read_text(encoding="utf-8")
            _, body = _parse_frontmatter(raw_text)
            _cache[cache_key] = body

        prompt = _cache[cache_key]
        # 动态注入当前时间，替换占位符。
        # 必须带时区标注：Django 进程内 datetime.now() 是 UTC（Django 会用
        # TIME_ZONE 覆盖进程 TZ），而用户口语里的「每天10点」是北京时间；
        # 不标注时区，LLM 会把 UTC 的钟点当成用户的钟点来生成 cron。
        prompt = prompt.replace("{{CURRENT_DATETIME}}", _current_datetime_label())
        return prompt

    @classmethod
    def list_agents(cls, version: str = "v1") -> list[dict[str, Any]]:
        """扫描 Prompt 目录，返回所有带 frontmatter 的 Agent 配置。

        优先使用 frontmatter（--- 之间）中声明的属性（name、tools、description 等）。
        仅当 frontmatter 中没有 description 时，fallback 到提取 body 前 5 行作为概述，
        供 Supervisor 的 LLM 意图识别使用。
        """
        version_dir = PROMPT_BASE / version
        if not version_dir.exists():
            return []

        agents = []
        for f in sorted(version_dir.glob("*.txt")):
            if f.name == "supervisor.txt":
                continue  # supervisor 不是子 Agent

            text = f.read_text(encoding="utf-8")
            meta, body = _parse_frontmatter(text)
            if meta and "name" in meta:
                # frontmatter 中没有 description 时，fallback 到 body 前 5 行
                if "description" not in meta:
                    description_lines = [
                        line for line in body.splitlines() if line.strip()
                    ][:5]
                    meta["description"] = "\n".join(description_lines)
                agents.append(meta)
        return agents
