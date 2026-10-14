from __future__ import annotations

import html
import json
import logging
import re
from urllib.parse import urlparse

import httpx
from django.conf import settings

from .base import BaseTool, ToolResult

logger = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_7_2) AppleWebKit/537.36"
MAX_REDIRECTS = 5
_DEFAULT_MAX_CHARS = 8000


def _strip_tags(text: str) -> str:
    text = re.sub(r"<script[\s\S]*?</script>", "", text, flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", "", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def _normalize(text: str) -> str:
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _to_markdown(html_content: str) -> str:
    text = re.sub(
        r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>([\s\S]*?)</a>',
        lambda m: f"[{_strip_tags(m[2])}]({m[1]})",
        html_content,
        flags=re.I,
    )
    text = re.sub(
        r"<h([1-6])[^>]*>([\s\S]*?)</h\1>",
        lambda m: f"\n{'#' * int(m[1])} {_strip_tags(m[2])}\n",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"<li[^>]*>([\s\S]*?)</li>",
        lambda m: f"\n- {_strip_tags(m[1])}",
        text,
        flags=re.I,
    )
    text = re.sub(r"</(p|div|section|article)>", "\n\n", text, flags=re.I)
    text = re.sub(r"<(br|hr)\s*/?>", "\n", text, flags=re.I)
    return _normalize(_strip_tags(text))


def _validate_url(url: str) -> tuple[bool, str]:
    try:
        p = urlparse(url)
        if p.scheme not in ("http", "https"):
            return False, f"只支持 http/https，当前: '{p.scheme or 'none'}'"
        if not p.netloc:
            return False, "缺少域名"
        return True, ""
    except Exception as e:
        return False, str(e)


class WebFetchTool(BaseTool):
    """
    获取网页内容工具。
    优先使用 Jina Reader API（需设置 JINA_API_KEY），降级到本地 readability-lxml，再降级到原始文本。
    """

    name = "web_fetch"
    description = "获取指定网页的内容。当需要阅读具体文章、文档或页面时使用。"

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "要获取的网页URL",
                },
                "extract_mode": {
                    "type": "string",
                    "enum": ["markdown", "text"],
                    "description": "提取模式：markdown（默认）或 text",
                    "default": "markdown",
                },
                "max_chars": {
                    "type": "integer",
                    "description": f"返回最大字符数，默认 {_DEFAULT_MAX_CHARS}",
                    "default": _DEFAULT_MAX_CHARS,
                },
            },
            "required": ["url"],
        }

    def _proxy(self) -> str | None:
        return getattr(settings, "WEB_PROXY", "") or None

    def _max_chars(self) -> int:
        return int(getattr(settings, "WEB_FETCH_MAX_CHARS", _DEFAULT_MAX_CHARS))

    async def execute(
        self,
        url: str = None,
        extract_mode: str = "markdown",
        max_chars: int | None = None,
        **kwargs,
    ) -> ToolResult:
        # 从 kwargs 中提取 url（处理 LLM 未显式传递的情况）
        if url is None:
            url = kwargs.get("url")
        if not url:
            return ToolResult(
                success=False, error="url 参数缺失，LLM 调用 web_fetch 时未提供 URL"
            )

        # 降级 DSML 路径可能把 max_chars 传成字符串，转 int 再用于长度比较/切片
        # （与 box_range 对数字参数的处理一致）
        max_chars = int(max_chars or self._max_chars())

        is_valid, err = _validate_url(url)
        if not is_valid:
            return ToolResult(success=False, error=f"URL无效: {err}")

        # 尝试 Jina Reader
        result = await self._fetch_jina(url, max_chars)
        if result is not None:
            return ToolResult(success=True, data=result)

        # 降级到 readability-lxml
        result = await self._fetch_readability(url, extract_mode, max_chars)
        if result is not None:
            return ToolResult(success=True, data=result)

        # 最终降级：原始文本
        result = await self._fetch_raw(url, max_chars)
        if result is None:
            logger.warning(
                "[WebFetchTool] 所有提取方式均失败 url=%s jina/readability/raw 全部出错，请检查网络/代理/JINA_API_KEY",
                url,
            )
        return ToolResult(
            success=result is not None,
            data=result,
            error="" if result else f"所有提取方式均失败: {url}",
        )

    async def _fetch_jina(self, url: str, max_chars: int) -> str | None:
        api_key = getattr(settings, "JINA_API_KEY", "") or ""
        headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        try:
            async with httpx.AsyncClient(proxy=self._proxy(), timeout=20.0) as client:
                r = await client.get(f"https://r.jina.ai/{url}", headers=headers)
                if r.status_code == 429:
                    logger.debug("Jina Reader rate limited, falling back")
                    return None
                r.raise_for_status()
            data = r.json().get("data", {})
            text = data.get("content", "")
            if not text:
                return None
            title = data.get("title", "")
            if title:
                text = f"# {title}\n\n{text}"
            if len(text) > max_chars:
                text = text[:max_chars]
            return text
        except Exception as e:
            logger.debug("Jina Reader failed for %s: %s", url, e)
            return None

    async def _fetch_readability(
        self, url: str, extract_mode: str, max_chars: int
    ) -> str | None:
        try:
            from readability import Document
        except ImportError:
            logger.debug("readability-lxml not installed, skipping")
            return None
        try:
            async with httpx.AsyncClient(
                proxy=self._proxy(),
                follow_redirects=True,
                max_redirects=MAX_REDIRECTS,
                timeout=30.0,
            ) as client:
                r = await client.get(url, headers={"User-Agent": USER_AGENT})
                r.raise_for_status()
            ctype = r.headers.get("content-type", "")
            if "application/json" in ctype:
                text = json.dumps(r.json(), indent=2, ensure_ascii=False)
            elif "text/html" in ctype or r.text[:256].lower().startswith(
                ("<!doctype", "<html")
            ):
                doc = Document(r.text)
                content = (
                    _to_markdown(doc.summary())
                    if extract_mode == "markdown"
                    else _strip_tags(doc.summary())
                )
                text = f"# {doc.title()}\n\n{content}" if doc.title() else content
            else:
                text = r.text
            if len(text) > max_chars:
                text = text[:max_chars]
            return text
        except Exception as e:
            logger.debug("readability fetch failed for %s: %s", url, e)
            return None

    async def _fetch_raw(self, url: str, max_chars: int) -> str | None:
        try:
            async with httpx.AsyncClient(
                proxy=self._proxy(),
                follow_redirects=True,
                max_redirects=MAX_REDIRECTS,
                timeout=20.0,
            ) as client:
                r = await client.get(url, headers={"User-Agent": USER_AGENT})
                r.raise_for_status()
            text = _normalize(_strip_tags(r.text))
            if len(text) > max_chars:
                text = text[:max_chars]
            return text
        except Exception as e:
            logger.warning("[WebFetchTool] raw fetch failed for %s: %s", url, e)
            return None
