"""list_directory 工具测试。"""

import pytest

from apps.agent.tools import file_io
from apps.agent.tools.file_io import ListDirectoryTool


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(file_io, "WORKSPACE_ROOT", tmp_path)
    return tmp_path


@pytest.mark.asyncio
async def test_list_directory_lists_files_and_dirs(workspace):
    (workspace / "strategies").mkdir()
    (workspace / "strategies" / "macd_atr_strategy.py").write_text(
        "x" * 128, encoding="utf-8"
    )
    (workspace / "strategies" / "sub").mkdir()
    (workspace / "strategies" / ".hidden").write_text("h", encoding="utf-8")

    tool = ListDirectoryTool()
    # OpenAI function calling schema 可生成（含抽象方法 parameters_schema 实现）
    schema = tool.schema
    assert schema["function"]["name"] == "list_directory"
    assert "path" in schema["function"]["parameters"]["properties"]

    result = await tool.execute(path="strategies/")
    assert result.success
    assert "macd_atr_strategy.py  (128 B)" in result.data
    assert "sub/" in result.data
    assert ".hidden" not in result.data


@pytest.mark.asyncio
async def test_list_directory_errors(workspace):
    (workspace / "afile").write_text("x", encoding="utf-8")
    (workspace / "empty-dir").mkdir()
    tool = ListDirectoryTool()
    r_empty = await tool.execute(path="")
    assert not r_empty.success and "不能为空" in r_empty.error
    r_missing = await tool.execute(path="nope/")
    assert not r_missing.success and "不存在" in r_missing.error
    r_notdir = await tool.execute(path="afile")
    assert not r_notdir.success and "不是目录" in r_notdir.error
    r_empty_dir = await tool.execute(path="empty-dir")
    assert r_empty_dir.success and "为空" in r_empty_dir.data
