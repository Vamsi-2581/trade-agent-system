from __future__ import annotations

import asyncio
import html
import logging
import re
from typing import Any

import httpx
from django.conf import settings

from .base import BaseTool, ToolResult

logger = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_7_2) AppleWebKit/537.36"


def _strip_tags(text: str) -> str:
    text = re.sub(r"<script[\s\S]*?</script>", "", text, flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", "", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def _normalize(text: str) -> str:
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _format_results(query: str, items: list[dict[str, Any]], n: int) -> str:
    if not items:
        return f"No results for: {query}"
    lines = [f"Results for: {query}\n"]
    for i, item in enumerate(items[:n], 1):
        title = _normalize(_strip_tags(item.get("title", "")))
        snippet = _normalize(_strip_tags(item.get("content", "")))
        lines.append(f"{i}. {title}\n   {item.get('url', '')}")
        if snippet:
            lines.append(f"   {snippet}")
    return "\n".join(lines)


class WebSearchTool(BaseTool):
    """
    搜索工具，支持多个搜索引擎提供商。
    通过 WEB_SEARCH_PROVIDER 设置选择：brave / tavily / searxng / jina / duckduckgo
    """

    name = "web_search"
    description = "搜索互联网获取最新信息。当需要查找实时数据、新闻、价格等信息时使用。"

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词",
                },
                "count": {
                    "type": "integer",
                    "description": "返回结果数量（1-10），默认5",
                    "minimum": 1,
                    "maximum": 10,
                },
            },
            "required": ["query"],
        }

    def _proxy(self) -> str | None:
        return getattr(settings, "WEB_PROXY", "") or None

    def _max_results(self) -> int:
        return int(getattr(settings, "WEB_SEARCH_MAX_RESULTS", 5))

    async def execute(
        self, query: str = None, count: int | None = None, **kwargs
    ) -> ToolResult:
        # 从 kwargs 中提取 query（处理 LLM 未显式传递的情况）
        if query is None:
            query = kwargs.get("query")
        if not query:
            return ToolResult(
                success=False,
                error="query 参数缺失，LLM 调用 web_search 时未提供搜索关键词",
            )
        provider = (
            (getattr(settings, "WEB_SEARCH_PROVIDER", "duckduckgo") or "duckduckgo")
            .strip()
            .lower()
        )
        # 降级 DSML 路径可能把 count 传成字符串，转 int 再参与 min/max（与 box_range 一致）
        n = min(max(int(count or self._max_results()), 1), 10)
        try:
            if provider == "brave":
                text = await self._search_brave(query, n)
            elif provider == "tavily":
                text = await self._search_tavily(query, n)
            elif provider == "searxng":
                text = await self._search_searxng(query, n)
            elif provider == "jina":
                text = await self._search_jina(query, n)
            else:
                text = await self._search_duckduckgo(query, n)
            return ToolResult(success=True, data=text)
        except Exception as e:
            logger.warning("[WebSearchTool] error: %s", e)
            return ToolResult(success=False, error=str(e))

    async def _search_brave(self, query: str, n: int) -> str:
        api_key = getattr(settings, "BRAVE_SEARCH_API_KEY", "") or ""
        if not api_key:
            logger.warning("BRAVE_SEARCH_API_KEY not set, falling back to DuckDuckGo")
            return await self._search_duckduckgo(query, n)
        async with httpx.AsyncClient(proxy=self._proxy(), timeout=10.0) as client:
            r = await client.get(
                "https://api.search.brave.com/res/v1/web/search",
                params={"q": query, "count": n},
                headers={"Accept": "application/json", "X-Subscription-Token": api_key},
            )
            r.raise_for_status()
        items = [
            {
                "title": x.get("title", ""),
                "url": x.get("url", ""),
                "content": x.get("description", ""),
            }
            for x in r.json().get("web", {}).get("results", [])
        ]
        return _format_results(query, items, n)

    async def _search_tavily(self, query: str, n: int) -> str:
        api_key = getattr(settings, "TAVILY_API_KEY", "") or ""
        if not api_key:
            logger.warning("TAVILY_API_KEY not set, falling back to DuckDuckGo")
            return await self._search_duckduckgo(query, n)
        async with httpx.AsyncClient(proxy=self._proxy(), timeout=15.0) as client:
            r = await client.post(
                "https://api.tavily.com/search",
                headers={"Authorization": f"Bearer {api_key}"},
                json={"query": query, "max_results": n},
            )
            r.raise_for_status()
        return _format_results(query, r.json().get("results", []), n)

    async def _search_searxng(self, query: str, n: int) -> str:
        base_url = (getattr(settings, "SEARXNG_BASE_URL", "") or "").strip()
        if not base_url:
            logger.warning("SEARXNG_BASE_URL not set, falling back to DuckDuckGo")
            return await self._search_duckduckgo(query, n)
        async with httpx.AsyncClient(proxy=self._proxy(), timeout=10.0) as client:
            r = await client.get(
                f"{base_url.rstrip('/')}/search",
                params={"q": query, "format": "json"},
                headers={"User-Agent": USER_AGENT},
            )
            r.raise_for_status()
        return _format_results(query, r.json().get("results", []), n)

    async def _search_jina(self, query: str, n: int) -> str:
        api_key = getattr(settings, "JINA_API_KEY", "") or ""
        headers = {"Accept": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        async with httpx.AsyncClient(proxy=self._proxy(), timeout=15.0) as client:
            r = await client.get(
                "https://s.jina.ai/",
                params={"q": query},
                headers=headers,
            )
            r.raise_for_status()
        data = r.json().get("data", [])[:n]
        items = [
            {
                "title": d.get("title", ""),
                "url": d.get("url", ""),
                "content": d.get("content", "")[:500],
            }
            for d in data
        ]
        return _format_results(query, items, n)

    async def _search_duckduckgo(self, query: str, n: int) -> str:
        try:
            from duckduckgo_search import DDGS

            ddgs = DDGS(proxy=self._proxy(), timeout=10)
            raw = await asyncio.to_thread(ddgs.text, query, max_results=n)
            if not raw:
                return f"No results for: {query}"
            items = [
                {
                    "title": r.get("title", ""),
                    "url": r.get("href", ""),
                    "content": r.get("body", ""),
                }
                for r in raw
            ]
            return _format_results(query, items, n)
        except ImportError:
            # 无 duckduckgo_search 包时降级到 DDG Instant Answer API
            return await self._search_ddg_instant(query, n)

    async def _search_ddg_instant(self, query: str, n: int) -> str:
        """DuckDuckGo Instant Answer API（无需安装额外包，但结果有限）"""
        async with httpx.AsyncClient(proxy=self._proxy(), timeout=15.0) as client:
            resp = await client.get(
                "https://api.duckduckgo.com/",
                params={
                    "q": query,
                    "format": "json",
                    "no_html": "1",
                    "skip_disambig": "1",
                },
            )
            resp.raise_for_status()
            data = resp.json()

        results = []
        if data.get("Abstract"):
            results.append(
                {
                    "title": data.get("Heading", ""),
                    "url": data.get("AbstractURL", ""),
                    "content": data["Abstract"],
                }
            )
        for topic in data.get("RelatedTopics", []):
            if "Text" in topic:
                results.append(
                    {
                        "title": topic.get("Text", "")[:80],
                        "url": topic.get("FirstURL", ""),
                        "content": topic.get("Text", ""),
                    }
                )
            if len(results) >= n:
                break

        if not results:
            return f"No results for: {query}. Tip: install duckduckgo-search for better results."
        return _format_results(query, results, n)
