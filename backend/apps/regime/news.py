"""资讯通道的采集与预筛——第①段单元 5(ii)。

## 与 `candles.py` 同构，但有一处刻意相反

同构的部分：注入式取数（`Fetcher`，测试因此不必联网）、frozen dataclass 结果、
显式传入的 `now`、幂等落库。

**相反的那一处是「一源失败要不要掀掉整轮」。** `candles.py` 一律抛，因为日线只有
一个源，源失败就等于没有输入。资讯有七个源，一个源失败 ≠ 没有输入——交易所公告
抓不到，加密媒体那几条照样该被看到。所以这里是**逐源 try/except + 逐源记结果**：
某一源的失败进它自己的 `SourceResult.error`，不影响别的源。

于是「今天 0 条」与「源挂了」是**两件可区分的事**：`ok=True, fetched=0` 是前者，
`ok=False, error=...` 是后者。把两者混成一个「0」，正是 CONTEXT.md 要求告警的那类
静默故障。`CollectReport.ok` 只在**所有源都成功**时为真——部分成功也报不 ok，因为
日报需要说出「今天有 2 个源没取到数」。

## 「接口返回 0 条」也被区分开了

解析器对**信封**做校验：顶层不是 RSS/Atom、或 `data.catalogs` 不是列表时抛
`NewsFetchError`，而不是返回空列表。这不是洁癖，探测时就撞上了活生生的例子：
`binance.com/en/support/announcement/detail/{code}` 走代理返回 **202 + 一个 2110 字节
的通用外壳页**，`raise_for_status()` 拦不住它（202 是 2xx），一个「拿到就算成功」的
解析器会安安静静地解析出 0 条。信封校验是把那种情况变成一次可见失败的东西。

## 窗口是增量区间

`(上次判定成功时刻, 本次判定时刻]`，两端夹在 `[window_floor_hours, window_cap_hours]`，
理由见 `config.NewsConfig` 的 docstring。没有上次（冷启动）时用上限兜住。

## 正文是**按需**补的

列表阶段只取标题/链接/时间。交易所公告的正文要**再发一次请求**（一篇文章一个请求），
为 40 条全补是 40 个请求，而它们绝大多数会被关键词挡掉。所以正文只对**入选的那些**
补，且补不到不算失败——`NewsItem.body` 允许为空（标题与链接本身就有信息量）。

## `url` 原样入库，不做任何归一化

`url` 是跨天去重的唯一键，而 `cointelegraph` 这类 feed 给出来的链接带着
`?utm_source=rss_feed&utm_medium=rss&...` 这样的跟踪参数。看起来「剥掉查询串再比」更
干净，这里**刻意不剥**：去掉查询串有可能把两篇真正不同的资讯（靠 `?id=` 区分的那种
站点）合成一条，而丢掉的那条**不会**再有任何痕迹——它是先被去重挡下、再由去重本身
保证不再出现的。反过来，跟踪参数变化只会让同一篇文章多进一次库，是一次响亮、可清理
的重复。两种错的代价不对称，所以选可清理的那一种。

去重依赖的是「同一个 feed 模板给出的链接跨天稳定」，而跟踪参数写在模板里，满足这一
条。真正需要归一化的那天，动作是**迁移历史行**，不是让新的比较悄悄换一套口径。
"""

from __future__ import annotations

import email.utils
import json
import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterator, Sequence

import httpx
from django.conf import settings

# 正文抽取与 HTTP 约定直接复用 `web_fetch`：`_strip_tags` / `_normalize` 是「HTML 去
# 标签 + 空白规整」的唯一实现，抄一份意味着两条路径会各自漂移，而漂移的表现是同一
# 篇文章在两个地方呈现出不同的正文。下划线前缀表示模块私有，这里刻意跨模块取用：
# 它们是既有实现，不是待重构的细节。
from apps.agent.tools.web_fetch import USER_AGENT, _normalize, _strip_tags
from apps.regime import config
from apps.regime.models import NewsItem

logger = logging.getLogger(__name__)

#: 标题在模型上的长度上限，截断用。**与 `NewsItem.title.max_length` 必须一致**
#: （由测试对住）：Postgres 的 varchar 会硬拒超长写入，而一条标题超长的条目会让
#: 一整轮采集停在一个 DataError 上——那一轮的资讯判定就没了。
TITLE_MAX_CHARS: int = NewsItem._meta.get_field("title").max_length

#: 单次取数：`(url, timeout) -> 响应体文本`。抓不到 / 非 2xx 一律抛 `NewsFetchError`。
#: 单列为可注入参数，测试不必联网，也不必打桩 httpx。
Fetcher = Callable[[str, float], str]

_BINANCE_ARTICLE = "https://www.binance.com/en/support/announcement/detail/{code}"
_BINANCE_DETAIL = (
    "https://www.binance.com/bapi/composite/v1/public/cms/article/detail/query"
    "?articleCode={code}"
)

#: 富文本树里算「块级」的 tag：展平时在它们后面补一个换行，否则相邻段落会黏成一句。
_BLOCK_TAGS = ("p", "div", "li", "h1", "h2", "h3", "h4", "br")

#: 没有发布时刻的条目在排序时用的哨兵：`_EPOCH` 让它们排到最后。
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class NewsFetchError(RuntimeError):
    """**单源**的抓取或解析失败。

    只标记一个源：它被逐源捕获后记进 `SourceResult.error`，不中断整轮采集。
    空列表不是失败——「这个源今天没发东西」是一个可信的结论，必须与失败区分开。
    """


# --------------------------------------------------------------------------- #
# 结果类型
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RawItem:
    """解析器从响应里认出来的一条条目，**尚未归属任何源**。

    `body` 可能为空，且有两种不同的原因：这个接口本来就不给正文（OKX、ECB），
    或者正文要再发一次请求（币安）。前者不会再有，后者由 `BODY_FETCHERS` 补。
    """

    url: str
    title: str
    published_at: datetime | None
    body: str = ""


@dataclass(frozen=True)
class ParsedFeed:
    """一个源的解析产物。`malformed` 单独计数而不是静默丢弃。

    会被记进来的情形是「有条目但缺了入库必需的东西」——目前只有缺链接：链接既是
    去重的唯一键，也是日报里给人点的那一下。静默跳过它，表现就是「这个源今天少
    几条」，而那是看不出来的。
    """

    items: tuple[RawItem, ...]
    malformed: int = 0


@dataclass(frozen=True)
class SourceResult:
    """一个源在这一轮里的下场。`ok=False` 与 `fetched=0` 是两件事。"""

    source: str
    kind: str
    ok: bool
    fetched: int
    malformed: int
    in_window: int
    matched: int
    error: str = ""


@dataclass(frozen=True)
class CollectReport:
    """一轮采集的可审计结果。

    逐源结果与整体结果**分开**：前者回答「哪个源今天哑了」，后者回答「这一轮一共
    发生了什么」。压成一个总数，前者就再也答不出来了。
    """

    window_start: datetime
    window_end: datetime
    sources: tuple[SourceResult, ...]
    candidates: int  # 过窗口 + 过关键词 + 未见过 = 条数上限的输入
    duplicates: int  # 窗口内且命中关键词、但 url 已经见过
    dropped_by_cap: int  # 被 max_items_to_llm 切掉的
    bodies_failed: int
    created: int
    items: tuple[NewsItem, ...]  # 本轮真正送进 LLM 的那批
    dry_run: bool

    @property
    def failed_sources(self) -> tuple[str, ...]:
        return tuple(r.source for r in self.sources if not r.ok)

    @property
    def ok(self) -> bool:
        """是否**每个源都成功**。部分成功也报 False，不藏。"""
        return not self.failed_sources


# --------------------------------------------------------------------------- #
# 取数
# --------------------------------------------------------------------------- #


def http_get(url: str, timeout: float) -> str:
    """默认取数实现：一次 GET，返回文本。失败抛 `NewsFetchError`。

    代理 / UA / 重定向的取法与 `web_fetch.py` 一致（同一个 `settings.WEB_PROXY`）：
    白名单里有几个源只有走代理才可达，理由见 `config` 模块 docstring。用同步
    `httpx.Client` 而不是 `AsyncClient`：整条判定链路是同步的 Celery 任务，在这里
    造一个 async 上下文只会把 `sync_to_async` 引进来，而落库本来就该是普通 ORM。
    """
    proxy = getattr(settings, "WEB_PROXY", "") or None
    try:
        with httpx.Client(
            proxy=proxy,
            timeout=timeout,
            follow_redirects=True,
            max_redirects=5,
            headers={"User-Agent": USER_AGENT},
        ) as client:
            response = client.get(url)
            response.raise_for_status()
            return response.text
    except Exception as exc:  # 网络 / 代理 / 超时 / 非 2xx
        raise NewsFetchError(f"{url} 抓取失败：{type(exc).__name__}: {exc}") from exc


# --------------------------------------------------------------------------- #
# 通用解析小件
# --------------------------------------------------------------------------- #


def _local(tag: str) -> str:
    """剥掉 XML 命名空间：`{http://www.w3.org/2005/Atom}entry` → `entry`。

    RSS 2.0 不带命名空间、RSS 1.0 与 Atom 带，但三者的**元素名是同一批**。剥掉
    前缀就能让一个解析入口通吃，而不必给每种 feed 变体写一套。
    """
    return tag.rpartition("}")[2]


def _first_text(element, *names: str) -> str:
    """按 **`names` 的给定顺序**取第一个非空文本，找不到返回空串。

    两条取舍，各自对应一处会看错的写法：

    - **按 `names` 的顺序，而不是文档顺序。** 正文的偏好是「更全的那个优先」，而一条
      entry 里 `description`（摘要）常常排在 `content:encoded`（全文）前面，按文档顺序
      取会拿到更短的那份。
    - **同一名字下优先无命名空间的元素。** 剥前缀匹配是为了让 RSS 1.0 的
      `content:encoded` 能被 `encoded` 认出来，但代价是 `media:title`
      （`{...}title`）也会冒充 `title`——而 `media:title` 是**媒体资源**的标题，
      不是这条资讯的标题。同名时先要裸的那个，裸的没有才退而取带命名空间的。
    """
    for name in names:
        fallback = ""
        for child in element:
            text = child.text or ""
            if _local(child.tag) != name or not text.strip():
                continue
            if child.tag == name:
                return text
            fallback = fallback or text
        if fallback:
            return fallback
    return ""


def parse_published(text: str) -> datetime | None:
    """RSS 的 RFC 822 / Atom 的 RFC 3339 → tz-aware UTC；认不出返回 `None`。

    `None` 不是失败：有的 feed 就不给时间。发布时刻只用于窗口过滤与排序，缺了它
    的条目仍然入库（`NewsItem` 的模型测试把这条钉住了）。
    """
    text = (text or "").strip()
    if not text:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError):
        parsed = None
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        # 不带时区的时间只能当成 UTC：三个 RSS 源实测都带偏移量，走到这里的是白名单
        # 之外的源。判成 UTC 而不是进程本地时区，是因为库内时间口径恒为 UTC。
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def ms_to_datetime(value: Any) -> datetime | None:
    """毫秒时间戳 → UTC datetime。`None` / 空串 / 认不出都返回 `None`。

    两个交易所的公告接口都用毫秒，但**一个是 int 一个是数字串**（币安
    `releaseDate` 是 int，OKX `pTime` 是 `"1790052307266"`），所以先过 `float`：
    `float64` 精确表示到 2^53，装得下这个量级的时间戳。
    """
    if value is None or value == "":
        return None
    try:
        return datetime.fromtimestamp(float(value) / 1000, tz=timezone.utc)
    except (TypeError, ValueError):
        return None


def _json_data(
    source: config.NewsSource, payload: str, expect: type, envelope: str
) -> Any:
    """`payload` → `data` 字段，并校验它的类型。形状不对抛 `NewsFetchError`。"""
    try:
        parsed = json.loads(payload)
    except (TypeError, ValueError) as exc:
        raise NewsFetchError(f"{source.name}: 响应不是 JSON：{exc}") from exc
    if not isinstance(parsed, dict):
        raise NewsFetchError(
            f"{source.name}: JSON 顶层是 {type(parsed).__name__}，期望对象"
        )
    data = parsed.get("data")
    if not isinstance(data, expect):
        raise NewsFetchError(
            f"{source.name}: 响应里没有 {envelope}——接口信封变了，"
            "或者返回的是挡在门外的一个页面（那种情况不报错，只会安静地 0 条）"
        )
    return data


def _clean_title(raw: str) -> str:
    """标题清洗：去标签、转义还原、空白规整，并截断到模型字段上限。

    截断是防御性的：Postgres 的 varchar 会硬拒超长写入，而「某条标题太长」不该让
    一整轮采集停在 DataError 上。
    """
    return _normalize(_strip_tags(raw or ""))[:TITLE_MAX_CHARS]


def flatten_rich_text(node: Any) -> str:
    """币安公告正文的富文本树 → 纯文本。

    正文不是 HTML 字符串，而是一棵 `{"node": "element", "tag": "p", "child": [...]}`
    的树（实测 2026-09-22：`data.body`，展平后 8137 字）。展平规则两条：文本节点取
    `text`，块级 tag 补一个换行——后者是为了不让相邻段落黏成一句。
    """
    parts: list[str] = []

    def walk(current: Any) -> None:
        if isinstance(current, list):
            for child in current:
                walk(child)
            return
        if not isinstance(current, dict):
            return
        if current.get("node") == "text":
            parts.append(str(current.get("text") or ""))
        if current.get("tag") in _BLOCK_TAGS:
            parts.append("\n")
        for key in ("child", "children"):
            if key in current:
                walk(current[key])

    walk(node)
    return _normalize("".join(parts))


# --------------------------------------------------------------------------- #
# 解析入口（名字必须与 config.NEWS_PARSERS 逐字一致，由测试对住）
# --------------------------------------------------------------------------- #


def _entry_link(entry) -> str:
    """一条 RSS/Atom 条目的链接。

    RSS 是 `<link>文本</link>`，Atom 是 `<link href="..."/>`（且可能有多个：
    `rel="alternate"` / `rel="self"` / enclosure）。优先取没有 rel 或
    `rel="alternate"` 的那个——`rel="self"` 指向 feed 自己，不是那篇文章。
    """
    fallback = ""
    for child in entry:
        if _local(child.tag) != "link":
            continue
        text = (child.text or "").strip()
        if text:
            fallback = fallback or text
            continue
        href = (child.get("href") or "").strip()
        if not href:
            continue
        if (child.get("rel") or "alternate").strip().lower() == "alternate":
            return href
        fallback = fallback or href
    return fallback


def parse_rss(source: config.NewsSource, payload: str) -> ParsedFeed:
    """RSS 2.0 / RSS 1.0(RDF) / Atom 的通用解析入口。

    三者共用一个入口是刻意的：RSS/Atom 是**有标准形态**的东西，一套解析通吃任意
    多家；只有接口信封各不相同的结构化 API 才需要各自的 parser（见 `config` 的
    「第一类因此换了形态」）。
    """
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise NewsFetchError(f"{source.name}: 不是合法 XML：{exc}") from exc

    top = _local(root.tag)
    if top not in ("rss", "feed", "RDF"):
        raise NewsFetchError(
            f"{source.name}: 顶层元素是 {top!r}，既不是 RSS（rss/RDF）也不是 Atom"
            "（feed）——订阅地址可能已经换成了别的东西（比如一个 HTML 页面）。"
            "此时若返回 0 条，它长得跟「今天很平静」一模一样"
        )

    items: list[RawItem] = []
    malformed = 0
    for entry in root.iter():
        if _local(entry.tag) not in ("item", "entry"):
            continue
        url = _entry_link(entry)
        if not url:
            # 链接是唯一键，没有它这条无法入库、也无法被日报引用。
            malformed += 1
            continue
        items.append(
            RawItem(
                url=url,
                title=_clean_title(_first_text(entry, "title")),
                published_at=parse_published(
                    _first_text(entry, "pubDate", "published", "updated", "date")
                ),
                # 正文偏好「更全的那个」：`encoded` 是 content:encoded（全文），
                # `description` 常常只是摘要。ECB 这类不给正文的源到这里是空串。
                body=_first_text(entry, "encoded", "content", "description", "summary"),
            )
        )
    return ParsedFeed(tuple(items), malformed)


def binance_article_url(code: str) -> str:
    """公告 `code` → 公告页链接。**列表接口不给 url 字段，链接由 code 拼出。**

    实测（2026-09-22）：列表里的 article 只有 `{id, code, title, type, releaseDate}`，
    而 `code` 正是详情接口的键（`?articleCode={code}`），所以链接与正文指向同一个
    标识。拼出来的是**给人看的那个地址**；它的可验证部分是 `code` 本身——链接形状
    的正确性在本环境里无法验证（走代理返回 202 + 2110 字节的通用外壳页，不走代理
    连不上），所以这里记的是「按已知形状拼、并把可验证的 code 原样留在链接里」：
    即便形状将来变了，这条记录仍然带着能重新定位到那篇公告的东西。

    **这个函数不能随手改。** 去重的唯一键就是它拼出来的 url，改了之后同一篇公告会
    以新的 url 重新入库一次。改它要同时想清楚历史行怎么办。
    """
    return _BINANCE_ARTICLE.format(code=code)


def _binance_articles(catalogs: Sequence[Any]) -> Iterator[dict]:
    """递归收集 `catalogs` 下的全部 `articles`。

    递归而不是只取第一层：`catalogId=48` 这一支实测只有一个目录，但目录本身还有
    一层 `catalogs`（子目录）。只取第一层会在交易所调整目录结构时安静地少掉一批，
    而那与「今天没发公告」长得一样。
    """
    for catalog in catalogs:
        if not isinstance(catalog, dict):
            continue
        for article in catalog.get("articles") or []:
            if isinstance(article, dict):
                yield article
        yield from _binance_articles(catalog.get("catalogs") or ())


def parse_binance_announcements(source: config.NewsSource, payload: str) -> ParsedFeed:
    """币安公告：`data.catalogs[].articles[]`（信封见 `config` 模块 docstring）。"""
    data = _json_data(source, payload, dict, "data 对象")
    catalogs = data.get("catalogs")
    if not isinstance(catalogs, list):
        raise NewsFetchError(
            f"{source.name}: 响应里没有 data.catalogs 列表——接口信封变了，"
            "或者返回的是挡在门外的一个页面"
        )

    items: list[RawItem] = []
    malformed = 0
    for article in _binance_articles(catalogs):
        code = str(article.get("code") or "").strip()
        title = _clean_title(str(article.get("title") or ""))
        if not code or not title:
            malformed += 1
            continue
        items.append(
            RawItem(
                url=binance_article_url(code),
                title=title,
                published_at=ms_to_datetime(article.get("releaseDate")),
                body="",  # 正文要再发一次请求，见 BODY_FETCHERS
            )
        )
    return ParsedFeed(tuple(items), malformed)


def parse_okx_announcements(source: config.NewsSource, payload: str) -> ParsedFeed:
    """OKX 公告：`data[].details[]`。

    实测（2026-09-22）每条 detail 的字段是
    `{annType, title, url, pTime, businessPTime}`——**接口不给正文**，所以 `body`
    留空而不是去抓公告页：公告页无契约，为它写抓取器等于把一个会静默返回 0 条的
    依赖放进链路（`config` 模块 docstring 里的理由）。

    单页上限也是接口给的（实测一次 20 条，未翻页）：窗口不超过 96 小时，20 条最新
    公告足够覆盖，所以 v1 不翻页；这一点在日报里不需要特意说明，因为条数上限
    `max_items_per_source` 对它本来就不生效。
    """
    groups = _json_data(source, payload, list, "data 列表")

    items: list[RawItem] = []
    malformed = 0
    for group in groups:
        if not isinstance(group, dict):
            continue
        for detail in group.get("details") or []:
            if not isinstance(detail, dict):
                continue
            url = str(detail.get("url") or "").strip()
            title = _clean_title(str(detail.get("title") or ""))
            if not url or not title:
                malformed += 1
                continue
            items.append(
                RawItem(
                    url=url,
                    title=title,
                    published_at=ms_to_datetime(detail.get("pTime")),
                    body="",
                )
            )
    return ParsedFeed(tuple(items), malformed)


Parser = Callable[[config.NewsSource, str], ParsedFeed]

#: 解析入口登记表。**名字必须与 `config.NEWS_PARSERS` 是同一批**——配置里写错一个
#: parser 名字会让那个源安静地取不到条目，所以两边由测试对住，且配置侧的
#: `NewsSource.__post_init__` 在 import 时就拒绝未登记的名字。
PARSERS: dict[str, Parser] = {
    "rss": parse_rss,
    "binance_announcements": parse_binance_announcements,
    "okx_announcements": parse_okx_announcements,
}


def _binance_body(
    source: config.NewsSource, url: str, fetch: Fetcher, timeout: float
) -> str:
    """公告页链接 → 正文纯文本。取不到抛 `NewsFetchError`（由调用方降级为空正文）。

    `code` 是从 url 里取回来的：链接形如 `.../detail/{code}`，正好是
    `binance_article_url` 的反向。这样正文抓取与去重键指向同一个标识，不会出现
    「入库的是这篇、抓来的是那篇」。
    """
    code = url.rpartition("/")[2]
    if not code:
        raise NewsFetchError(f"{source.name}: 无法从链接里取出 code：{url}")
    payload = fetch(_BINANCE_DETAIL.format(code=code), timeout)
    data = _json_data(source, payload, dict, "data 对象")
    return flatten_rich_text(data.get("body"))


#: 需要**再发一次请求**才有正文的解析入口 → 它的正文抓取函数。
#: 不在这张表里的入口，正文随列表一次取回（RSS 的 description / content:encoded）。
BODY_FETCHERS: dict[str, Callable[[config.NewsSource, str, Fetcher, float], str]] = {
    "binance_announcements": _binance_body,
}


# --------------------------------------------------------------------------- #
# 窗口与预筛（纯函数）
# --------------------------------------------------------------------------- #


def compute_window(
    now: datetime,
    last_success: datetime | None = None,
    news: config.NewsConfig | None = None,
) -> tuple[datetime, datetime]:
    """本轮的增量区间 `(start, end]`，两端夹在 `[floor, cap]` 之间。

    三种情形各自对应一件真事：

    - **没有上次**（冷启动）→ `now - cap`：第一次运行没有锚点，用上限兜住；不用
      更长的区间，因为再早的资讯描述的不是今天。
    - **上次太近**（同一天里的第二次心跳、或一次重试）→ 拉长到 `now - floor`：
      此时真正的语义是「今天这一天有什么新资讯」，不是「最近这五分钟」。区间被
      钉在一天以上之后，同一天里的重复运行看到的是同一批条目——配合 url 去重，
      重复运行不会重复送同一篇，只会得出同一个结论。
    - **上次太远**（判定停了几天又恢复）→ 收窄到 `now - cap`：不设上限的话，一次
      长故障后的重启会把攒了两周的条目一口气送进 LLM，而那一批得出的结论描述的
      是两周前的市场，却会被当成今天的结论生效。
    """
    news = news or config.NEWS
    floor = timedelta(hours=news.window_floor_hours)
    cap = timedelta(hours=news.window_cap_hours)
    if last_success is None:
        return now - cap, now
    age = now - last_success
    if age < floor:
        return now - floor, now
    if age > cap:
        return now - cap, now
    return last_success, now


def in_window(item: RawItem, start: datetime, end: datetime) -> bool:
    """发布时刻落在 `(start, end]` 内。**没有发布时刻的条目保留。**

    「未知」不是「很旧」：把没有时间的条目丢掉，窗口过滤就变成了一处会安静吞掉
    条目的地方（`NewsItem` 的模型测试把这条钉住了）。
    """
    if item.published_at is None:
        return True
    return start < item.published_at <= end


def matches_keywords(item: RawItem, keywords: Sequence[str]) -> bool:
    """小写子串匹配，作用在「标题 + 正文」上。词表与口径见 `config.NEWS_KEYWORDS`。

    命中与否只是「要不要花 token 送进 LLM」的成本闸门，不是相关性判定，所以匹配
    刻意做宽（连 "ban" 命中 "urban" 也接受）：两个方向的代价不对称。
    """
    haystack = f"{item.title}\n{item.body}".lower()
    return any(keyword in haystack for keyword in keywords)


def _sort_key(item: RawItem) -> datetime:
    """按发布时刻倒序时用的键：没有时刻的排到最后。

    用 `_EPOCH` 而不是把它们丢掉或当成「现在」：丢掉会让它们在条数上限那一刀下
    无声消失；当成「现在」则会把它们顶到最前面，挤掉真正最新、最来得及反应的那
    几条。
    """
    return item.published_at or _EPOCH


# --------------------------------------------------------------------------- #
# 一轮采集
# --------------------------------------------------------------------------- #


def _failed_source(source: config.NewsSource, error: str) -> SourceResult:
    """这一个源这一轮的失败记录：条数全 0，`error` 是给人读的那句话。

    刻意**不**填 `fetched`：失败时接口给了几条是未知的，填 0 会被读成「今天 0 条」，
    而 `ok=False` 才是这条记录要说的事。
    """
    return SourceResult(
        source=source.name,
        kind=source.kind.value,
        ok=False,
        fetched=0,
        malformed=0,
        in_window=0,
        matched=0,
        error=error,
    )


def _collect_source(
    source: config.NewsSource,
    news: config.NewsConfig,
    window_start: datetime,
    window_end: datetime,
    fetch: Fetcher,
) -> tuple[SourceResult, list[RawItem]]:
    """抓一个源、解析、窗口过滤、关键词预筛；**任何失败都只记在这一个源上**。

    护栏只圈住「取数 + 解析」，因为那是唯一接触**外部输入**的地方：这一圈之内的失败
    是「别人给的东西长得不对」，代价由这一个源承担——七个源里挂一个就把整轮采集带走，
    等于让一个坏 feed 决定今天有没有资讯结论。

    失败分两类记，因为它们指向的动作不同：

    - `NewsFetchError`：预期内的失败（网络、状态码、信封变了、XML 坏了）。WARNING，
      消息是给人读的一句话。
    - 其它异常：解析器没见过的形状引发的 bug（`KeyError`/`AttributeError` 之类）。
      **不往上抛**，因为抛出去的代价是这一轮所有源都白采；但也不当普通失败——ERROR
      带堆栈，错误串前缀异常类名，于是在日报里与「源挂了」可分辨：前者要改代码，
      后者等下一轮或去修网络。

    这一圈**之外**（窗口过滤、关键词匹配）的异常照旧往上抛：那是我们自己的逻辑，出错
    影响的是所有源，兜住它只会把 bug 变成「今天很平静」。
    """
    try:
        payload = fetch(source.url, news.fetch_timeout_seconds)
        parsed = PARSERS[source.parser](source, payload)
    except NewsFetchError as exc:
        logger.warning("[regime/news] 源 %s 本轮失败：%s", source.name, exc)
        return _failed_source(source, str(exc)), []
    except Exception as exc:  # noqa: BLE001 —— 见 docstring：外部形状未知，逐源兜住
        logger.error("[regime/news] 源 %s 抛出未预期异常", source.name, exc_info=True)
        return _failed_source(source, f"{type(exc).__name__}: {exc}"), []

    capped = parsed.items[: news.max_items_per_source]
    if len(capped) < len(parsed.items):
        # 截断要可见：接口明明更多、我们却只收了这么多。config 把币安的 pageSize
        # 配成与 max_items_per_source 同值，就是为了让这一支不常触发。
        logger.warning(
            "[regime/news] 源 %s 返回 %d 条，按 max_items_per_source=%d 截断",
            source.name,
            len(parsed.items),
            news.max_items_per_source,
        )

    windowed = [item for item in capped if in_window(item, window_start, window_end)]
    matched = [item for item in windowed if matches_keywords(item, news.keywords)]
    return (
        SourceResult(
            source=source.name,
            kind=source.kind.value,
            ok=True,
            fetched=len(parsed.items),
            malformed=parsed.malformed,
            in_window=len(windowed),
            matched=len(matched),
            error="",
        ),
        matched,
    )


def _drop_seen(
    pool: Sequence[tuple[config.NewsSource, RawItem]],
) -> tuple[list[tuple[config.NewsSource, RawItem]], int]:
    """去掉 url 已在库里的、以及本轮内重复出现的。返回 `(新条目, 重复数)`。

    「库里有没有」用一次 `url__in` 回答，不逐条 `exists()`：几百条各来一次往返，
    而这是每一轮都要走的路径。

    **同一篇文章被多个源同时给出时，按配置表的声明顺序先到先得。** 转载是常态，
    而 url 是唯一键，所以必须只有一个赢家；顺序取自配置表里那行显式的字面量，
    不是遍历顺序的运气。
    """
    if not pool:
        return [], 0
    urls = [item.url for _, item in pool]
    existing = set(NewsItem.objects.filter(url__in=urls).values_list("url", flat=True))

    fresh: list[tuple[config.NewsSource, RawItem]] = []
    seen: set[str] = set()
    duplicates = 0
    for source, item in pool:
        if item.url in existing or item.url in seen:
            duplicates += 1
            continue
        seen.add(item.url)
        fresh.append((source, item))
    return fresh, duplicates


def _build_rows(
    selected: Sequence[tuple[config.NewsSource, RawItem]],
    news: config.NewsConfig,
    fetch: Fetcher,
    fetched_at: datetime,
) -> tuple[list[NewsItem], int]:
    """入选条目 → 待入库的 `NewsItem`；正文按需补齐、清洗、截断。

    存下来的 `body` **就是送给 LLM 的那份文本**（截断后的），因为留存的是「当时
    LLM 到底看到了什么」。先清洗再截断，让上限全部用在有效字符上。
    """
    rows: list[NewsItem] = []
    bodies_failed = 0
    for source, item in selected:
        body = item.body
        body_fetcher = BODY_FETCHERS.get(source.parser)
        if not body and body_fetcher is not None:
            try:
                body = body_fetcher(source, item.url, fetch, news.fetch_timeout_seconds)
            except NewsFetchError as exc:
                # 正文抓不到不是丢弃这条的理由：标题与链接本身就有信息量，而
                # 「抓不到正文」与「没有这条资讯」是两件事。
                logger.warning(
                    "[regime/news] %s 的正文抓取失败（%s）：%s",
                    source.name,
                    item.url,
                    exc,
                )
                bodies_failed += 1
                body = ""
            except Exception as exc:  # noqa: BLE001 —— 同 `_collect_source`：外部输入
                # 兜住它比在解析那儿更要紧：正文抓取发生在**落库之前**，抛出去等于把
                # 今天一整批条目全部丢掉，只因为其中一篇的正文响应长得不对。错误串
                # 前缀异常类名，让这一行读起来与「源挂了」可分辨。
                logger.error(
                    "[regime/news] %s 的正文抓取抛出未预期异常（%s）：%s: %s",
                    source.name,
                    item.url,
                    type(exc).__name__,
                    exc,
                    exc_info=True,
                )
                bodies_failed += 1
                body = ""
        body = _normalize(_strip_tags(body))
        rows.append(
            NewsItem(
                source=source.name,
                kind=source.kind.value,
                url=item.url,
                title=item.title,
                published_at=item.published_at,
                body=body[: news.body_max_chars],
                body_truncated=len(body) > news.body_max_chars,
                fetched_at=fetched_at,
            )
        )
    return rows, bodies_failed


def _persist(rows: Sequence[NewsItem]) -> int:
    """只增不删，返回写入行数。

    去重在 `_drop_seen` 已经做过；这里的 `ignore_conflicts` 只兜并发心跳——两个
    tick 同时走到这里时，后到的会被唯一约束挡下，挡下的是**同一篇**，不是一条该被
    看见的失败（条目表没有 `auto_now`，所以冲突不会造成任何改写）。
    """
    if not rows:
        return 0
    NewsItem.objects.bulk_create(rows, ignore_conflicts=True)
    return len(rows)


def collect_news(
    now: datetime | None = None,
    last_success: datetime | None = None,
    *,
    fetch: Fetcher | None = None,
    news: config.NewsConfig | None = None,
    dry_run: bool = False,
) -> CollectReport:
    """采集一轮资讯：逐源抓取 → 窗口 → 关键词 → 去重 → 条数上限 → 正文 → 落库。

    返回的 `items` **就是本轮该送进 LLM 的那批**（也因此是本轮新建的行）：凡是没
    有进这一批的，都没有花掉 token，也不该出现在判定记录的引用里。

    `last_success` 是上一轮**成功**采集的时刻（不是上一次运行——失败的那次没有可
    依赖的锚点，见 `compute_window`）。`None` 表示冷启动。
    """
    news = news or config.NEWS
    fetch = fetch or http_get
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("now 必须是 tz-aware（库内时间口径恒为 UTC）")

    window_start, window_end = compute_window(now, last_success, news)

    results: list[SourceResult] = []
    pool: list[tuple[config.NewsSource, RawItem]] = []
    for source in news.sources:
        result, matched = _collect_source(source, news, window_start, window_end, fetch)
        results.append(result)
        pool.extend((source, item) for item in matched)

    fresh, duplicates = _drop_seen(pool)
    fresh.sort(key=lambda pair: _sort_key(pair[1]), reverse=True)
    selected = fresh[: news.max_items_to_llm]
    dropped_by_cap = len(fresh) - len(selected)

    rows, bodies_failed = _build_rows(selected, news, fetch, now)
    created = 0 if dry_run else _persist(rows)

    report = CollectReport(
        window_start=window_start,
        window_end=window_end,
        sources=tuple(results),
        candidates=len(fresh),
        duplicates=duplicates,
        dropped_by_cap=dropped_by_cap,
        bodies_failed=bodies_failed,
        created=created,
        items=tuple(rows),
        dry_run=dry_run,
    )
    logger.info(
        "[regime/news] 采集完成 窗口 %s ~ %s：%d 个源（失败 %d 个），"
        "候选 %d、重复 %d、超上限丢弃 %d、正文缺失 %d、落库 %d",
        window_start.isoformat(),
        window_end.isoformat(),
        len(results),
        len(report.failed_sources),
        report.candidates,
        duplicates,
        dropped_by_cap,
        bodies_failed,
        created,
    )
    return report
