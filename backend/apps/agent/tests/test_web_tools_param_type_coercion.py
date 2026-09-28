"""回归测试：web_search / web_fetch 的数字参数以字符串传入（降级 DSML 路径形态）
时应被归一为 int，而不是触发 int/str 比较/运算的 TypeError。

根因同 list_orders：_parse_dsml_parameters 默认 string="true"，LLM 回传的
count / max_chars 会以字符串到达 execute()。
  - web_search: `n = min(max(count or ..., 1), 10)` 在 try 之外，字符串 count
    → `max("5", 1)` 抛未被捕获的 TypeError（会打断整个工具循环）。
  - web_fetch:  `if len(text) > max_chars` 字符串 max_chars → 三种提取方式全部
    静默失败 → 返回"所有提取方式均失败"（错误结果而非报错）。
接缝：走真实 execute()，只在网络层（_search_duckduckgo / _fetch_jina）打桩，
断言真正传给下游的是 int。
"""

from unittest.mock import AsyncMock, patch

import pytest
from django.conf import settings

from apps.agent.tools.web_fetch import WebFetchTool
from apps.agent.tools.web_search import WebSearchTool


@pytest.mark.asyncio
async def test_web_search_string_count_coerced_to_int():
    """count 以字符串 '5' 传入，_search_duckduckgo 必须收到 int 5，且不崩溃。"""
    tool = WebSearchTool()
    with (
        patch.object(settings, "WEB_SEARCH_PROVIDER", "duckduckgo"),
        patch.object(
            WebSearchTool, "_search_duckduckgo", AsyncMock(return_value="ok")
        ) as m,
    ):
        result = await tool.execute(query="btc", count="5")

    assert result.success is True, f"字符串 count 导致搜索失败：{result.error!r}"
    n_arg = m.call_args.args[1]
    assert n_arg == 5, f"count 未被正确归一：{n_arg!r}"
    assert isinstance(n_arg, int), f"传给搜索的 count 必须是 int：{type(n_arg)}"


@pytest.mark.asyncio
async def test_web_search_int_count_still_works():
    """回归保护：int count（原生 function calling 正常形态）不受影响。"""
    tool = WebSearchTool()
    with (
        patch.object(settings, "WEB_SEARCH_PROVIDER", "duckduckgo"),
        patch.object(
            WebSearchTool, "_search_duckduckgo", AsyncMock(return_value="ok")
        ) as m,
    ):
        result = await tool.execute(query="btc", count=5)

    assert result.success is True
    assert m.call_args.args[1] == 5


@pytest.mark.asyncio
async def test_web_fetch_string_max_chars_coerced_to_int():
    """max_chars 以字符串 '100' 传入，_fetch_jina 必须收到 int 100。"""
    tool = WebFetchTool()
    with patch.object(
        WebFetchTool, "_fetch_jina", AsyncMock(return_value="content")
    ) as m:
        result = await tool.execute(url="https://example.com", max_chars="100")

    assert result.success is True, f"字符串 max_chars 导致抓取失败：{result.error!r}"
    assert result.data == "content"
    mc_arg = m.call_args.args[1]
    assert mc_arg == 100, f"max_chars 未被正确归一：{mc_arg!r}"
    assert isinstance(mc_arg, int), f"传给抓取层的 max_chars 必须是 int：{type(mc_arg)}"


@pytest.mark.asyncio
async def test_web_fetch_int_max_chars_still_works():
    """回归保护：int max_chars 不受影响。"""
    tool = WebFetchTool()
    with patch.object(
        WebFetchTool, "_fetch_jina", AsyncMock(return_value="content")
    ) as m:
        result = await tool.execute(url="https://example.com", max_chars=100)

    assert result.success is True
    assert m.call_args.args[1] == 100
