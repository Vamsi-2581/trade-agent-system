"""资讯采集与预筛（第①段单元 5(ii)）。

这一组测试里真正重要的是四条不变量，其余都是在钉住它们不被重构悄悄丢掉：

1. **「源挂了」与「今天 0 条」可区分**——逐源记结果，前者带 `error` 且整轮报不 ok。
   这是 CONTEXT.md 那类静默故障的入口：把两者混成一个 0，日报就永远不知道该看一眼。
2. **接口信封变了要抛，不能返回 0 条**——探测时撞上的活例子（币安公告页返回
   202 + 一个 2110 字节的通用外壳页）说明「拿到就算成功」的解析器会安静地 0 条。
3. **窗口是增量区间且两端都有夹**——否则重复运行会重复送同一批，长时间停摆后的
   重启会把两周前的资讯当成今天的结论。
4. **`url` 是跨天去重的唯一键，且原样入库**——不改写外部给的标识；理由见模块 docstring。

解析入口用的都是**实测抓下来的信封形状**（2026-09-22 探测），不是照着文档编的
样例：形状写错了，测试会跟着一起错。
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from django.test import SimpleTestCase, TestCase

from apps.regime import config, news
from apps.regime.models import NewsItem
from apps.regime.news import (
    NewsFetchError,
    RawItem,
    compute_window,
    flatten_rich_text,
    in_window,
    matches_keywords,
    parse_published,
)

# UTC 00:05 = 北京 08:05：判定任务（搭在 5 分钟心跳上）刚写下当天记录之后的时刻。
NOW = datetime(2026, 9, 22, 0, 5, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# 信封构造：形状照抄实测结果
# --------------------------------------------------------------------------- #


def ms(when: datetime) -> int:
    return int(when.timestamp() * 1000)


def rss(*items: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel><title>Feed</title>'
        + "".join(items)
        + "</channel></rss>"
    )


def rss_item(
    title: str = "Bitcoin ETF inflows hit a record",
    link: str | None = "https://media.test/a",
    pub: str | None = "Mon, 21 Sep 2026 18:00:00 +0000",
    description: str | None = "Bitcoin ETF inflows hit a record",
) -> str:
    parts = [f"<title>{title}</title>"]
    if link is not None:
        parts.append(f"<link>{link}</link>")
    if pub is not None:
        parts.append(f"<pubDate>{pub}</pubDate>")
    if description is not None:
        parts.append(f"<description>{description}</description>")
    return "<item>" + "".join(parts) + "</item>"


def binance_article(
    code="7379b99aa0f349a49c3b3feca1b4bbd6",
    title="Binance Will Delist BTCUSDT Perpetual Contracts",
    release=ms(NOW - timedelta(hours=3)),
):
    """一条公告。默认标题**故意命中关键词**（`delist` / `btc`），默认时刻**故意落在
    窗口内**。

    两个默认值都是为了让「一轮采集」的测试测它自己声称的那件事：

    - 探测时抓到的那条真标题是 "Binance Will List KMN"，一个关键词都不命中。那放在
      **解析器**的测试里没问题（那里只看形状），但一轮采集的测试会因此在预筛那一关
      静默塌成 0 条——于是「入库了几条」「去重了几条」全都变成 0。要测「不命中就不进
      这一批」，就显式传一个不命中的标题。
    - 同理，默认时刻若落在窗口外，条目会在窗口那一关安静消失，表现和上面一模一样。
      探测抓下来的真实 `releaseDate` 是 `1790062264920`（2026-09-22T07:31Z），它在
      `NOW`（2026-09-22T00:05Z）**之后**——照抄进默认值就等于给每个调用点埋一个
      「这条为什么没进批次」。所以真实字面量放在断言它的那个解析器测试里显式传。
    """
    return {
        "id": 285191,
        "code": code,
        "title": title,
        "type": 1,
        "releaseDate": release,
    }


def binance_payload(articles, catalogs=None) -> str:
    """`data.total` 实测是 None，总数在目录上——所以这里也刻意不填它。"""
    if catalogs is None:
        catalogs = [
            {
                "catalogId": 48,
                "catalogName": "Latest News",
                "total": len(articles),
                "articles": articles,
                "catalogs": [],
            }
        ]
    return json.dumps(
        {
            "code": "000000",
            "message": None,
            "data": {"total": None, "catalogs": catalogs},
        }
    )


def okx_detail(
    title="OKX will delist KMN/USD perpetual",
    url="https://www.okx.test/help/list-kmn",
    ptime=ms(NOW - timedelta(hours=6)),
):
    """一条公告。默认标题命中关键词（`delist`）、默认时刻落在窗口内，两个理由都见
    `binance_article`。

    探测抓下来的真实 `pTime` 是 `"1790052307266"`（2026-09-22T04:45Z，**字符串**形式），
    同样在 `NOW` 之后——它显式传在断言它的那个解析器测试里。
    """
    return {
        "annType": "announcements-new-listings",
        "title": title,
        "url": url,
        "pTime": ptime,
        "businessPTime": ptime,
    }


def okx_payload(details) -> str:
    return json.dumps(
        {"code": "0", "msg": "", "data": [{"details": details, "totalPage": 3}]}
    )


class FakeFetcher:
    """按前缀分发的取数替身。**最长前缀优先**。

    最长优先不是讲究：币安的列表与详情在同一个域下（`/bapi/composite/v1/public/...`），
    只按域分发会让详情请求吃到列表的响应，而那种错在断言里表现为「正文没抓到」，
    看起来像功能没做，不像测试替身写错了。
    """

    def __init__(
        self, routes: dict[str, str | Exception], default_error="未登记的 url"
    ):
        self._routes = sorted(routes.items(), key=lambda kv: -len(kv[0]))
        self._default_error = default_error
        self.calls: list[tuple[str, float]] = []

    def __call__(self, url: str, timeout: float) -> str:
        self.calls.append((url, timeout))
        for prefix, payload in self._routes:
            if url.startswith(prefix):
                if isinstance(payload, Exception):
                    raise payload
                return payload
        raise NewsFetchError(f"{url}：{self._default_error}")

    @property
    def urls(self) -> list[str]:
        return [url for url, _ in self.calls]


# --------------------------------------------------------------------------- #
# 登记表与配置面是同一批名字
# --------------------------------------------------------------------------- #


class TestParserRegistryMatchesConfig(SimpleTestCase):
    """`config.NEWS_PARSERS` 的 docstring 把这条承诺写在了注释里，这里把它变成断言。"""

    def test_the_two_name_sets_are_identical(self):
        """配置里写错一个 parser 名字，那个源就会安静地取不到条目——配置侧
        `NewsSource.__post_init__` 在 import 时拒绝未登记的名字，而这里拒绝的是
        「登记了但没实现」。两边必须逐字一致。"""
        self.assertEqual(set(news.PARSERS), set(config.NEWS_PARSERS))

    def test_every_registered_parser_is_callable(self):
        for name, parser in news.PARSERS.items():
            with self.subTest(parser=name):
                self.assertTrue(callable(parser))

    def test_every_whitelisted_source_resolves_to_a_real_parser(self):
        for source in config.NEWS.sources:
            with self.subTest(source=source.name):
                self.assertIn(source.parser, news.PARSERS)

    def test_body_fetchers_are_a_subset_of_the_registry(self):
        """正文抓取挂在解析入口的名字上：出现一个没有解析器的名字，那个源永远
        拿不到正文，而表现是「这个源就是没正文」——与 OKX 那种真的没有正文混在一起。"""
        self.assertTrue(set(news.BODY_FETCHERS) <= set(news.PARSERS))

    def test_title_cap_matches_the_model_column(self):
        """截断上限取自模型列宽；两处不一致会让超长标题在写库那一刻才炸。"""
        self.assertEqual(
            news.TITLE_MAX_CHARS, NewsItem._meta.get_field("title").max_length
        )


# --------------------------------------------------------------------------- #
# RSS / Atom
# --------------------------------------------------------------------------- #


class TestRssParsing(SimpleTestCase):
    SOURCE = config.NewsSource(
        name="media",
        url="https://media.test/rss",
        kind=config.NewsSourceKind.CRYPTO_MEDIA,
    )

    def parse(self, payload):
        return news.PARSERS["rss"](self.SOURCE, payload)

    def test_a_normal_rss_item_is_read(self):
        feed = self.parse(rss(rss_item()))
        self.assertEqual(feed.malformed, 0)
        item = feed.items[0]
        self.assertEqual(item.url, "https://media.test/a")
        self.assertEqual(item.title, "Bitcoin ETF inflows hit a record")
        self.assertEqual(
            item.published_at, datetime(2026, 9, 21, 18, 0, tzinfo=timezone.utc)
        )
        self.assertEqual(item.body, "Bitcoin ETF inflows hit a record")

    def test_an_html_page_instead_of_a_feed_raises(self):
        """顶层不是 RSS/Atom 就抛，而不是返回 0 条：地址换成别的东西时，0 条长得
        跟「今天很平静」一模一样（探测时币安公告页就返回过这种外壳）。"""
        with self.assertRaises(NewsFetchError) as ctx:
            self.parse(
                "<!DOCTYPE html><html><head><meta charset='utf-8'></head></html>"
            )
        self.assertIn("media", str(ctx.exception))

    def test_broken_xml_raises(self):
        with self.assertRaises(NewsFetchError):
            self.parse("<rss><channel><item>")

    def test_an_item_without_a_link_is_counted_not_silently_dropped(self):
        """链接是去重键，也是日报里给人点的那一下。没有它的条目无法入库，所以计入
        malformed——安静地少一条才是要防的那件事。"""
        feed = self.parse(
            rss(rss_item(link=None), rss_item(link="https://media.test/b"))
        )
        self.assertEqual(feed.malformed, 1)
        self.assertEqual([i.url for i in feed.items], ["https://media.test/b"])

    def test_an_item_without_a_published_time_is_kept(self):
        feed = self.parse(rss(rss_item(pub=None)))
        self.assertIsNone(feed.items[0].published_at)

    def test_an_unparseable_published_time_is_kept_as_unknown(self):
        feed = self.parse(rss(rss_item(pub="昨日")))
        self.assertIsNone(feed.items[0].published_at)

    def test_a_feed_without_any_body_element_is_fine(self):
        """ECB 实测就是这样：一个条目里连 description 都没有（0 字符）。"""
        feed = self.parse(rss(rss_item(description=None)))
        self.assertEqual(feed.items[0].body, "")
        self.assertEqual(feed.malformed, 0)

    def test_content_encoded_wins_over_the_shorter_description(self):
        """全文优先于摘要：一条 entry 里 description 常常排在 content:encoded 前面，
        按文档顺序取会拿到更短的那份。"""
        body = (
            "<description>引子</description>"
            '<content:encoded xmlns:content="http://purl.org/rss/1.0/modules/content/">'
            "全文，比引子长得多</content:encoded>"
        )
        feed = self.parse(
            rss(f"<item><title>t</title><link>https://media.test/c</link>{body}</item>")
        )
        self.assertEqual(feed.items[0].body, "全文，比引子长得多")

    def test_a_namespaced_media_title_does_not_masquerade_as_the_item_title(self):
        """`media:title` 是媒体资源的标题，不是这条资讯的标题；剥前缀匹配会把它
        当成 title——于是日报里引用的标题来自一张配图。"""
        payload = (
            "<item><media:title xmlns:media='http://search.yahoo.com/mrss/'>IMG_2043</media:title>"
            "<title>Bitcoin ETF inflows hit a record</title>"
            "<link>https://media.test/d</link></item>"
        )
        feed = self.parse(rss(payload))
        self.assertEqual(feed.items[0].title, "Bitcoin ETF inflows hit a record")

    def test_titles_are_unescaped_and_whitespace_collapsed(self):
        feed = self.parse(rss(rss_item(title="Bitcoin &amp; Ether:   the  sequel")))
        self.assertEqual(feed.items[0].title, "Bitcoin & Ether: the sequel")

    def test_a_tracking_query_string_is_kept_verbatim(self):
        """`url` 原样入库，不做归一化——理由是两种错的代价不对称，见模块 docstring。

        feed 里 `&` 必须写成 `&amp;`（`cointelegraph` 实际就是这么给的），这条顺带
        钉住「解析出来的是转义还原后的原 url」，而不是 XML 里那串字面量。
        """
        feed = self.parse(
            rss(
                rss_item(
                    link="https://cointelegraph.com/news/x?utm_source=rss_feed&amp;utm_medium=rss"
                )
            )
        )
        self.assertEqual(
            feed.items[0].url,
            "https://cointelegraph.com/news/x?utm_source=rss_feed&utm_medium=rss",
        )

    def test_an_overlong_title_is_truncated_not_rejected(self):
        feed = self.parse(rss(rss_item(title="x" * (news.TITLE_MAX_CHARS + 50))))
        self.assertEqual(len(feed.items[0].title), news.TITLE_MAX_CHARS)

    def test_atom_entries_and_href_links_are_understood(self):
        payload = (
            '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">'
            "<entry><title>Fed holds rates</title>"
            '<link rel="self" href="https://macro.test/feed"/>'
            '<link rel="alternate" href="https://macro.test/fed-holds"/>'
            "<updated>2026-09-21T18:00:00Z</updated>"
            "<summary>The Federal Reserve held rates.</summary></entry></feed>"
        )
        feed = self.parse(payload)
        self.assertEqual(feed.items[0].url, "https://macro.test/fed-holds")
        self.assertEqual(
            feed.items[0].published_at,
            datetime(2026, 9, 21, 18, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(feed.items[0].body, "The Federal Reserve held rates.")

    def test_rdf_lists_are_understood(self):
        payload = (
            '<?xml version="1.0"?><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
            "<item><title>t</title><link>https://media.test/rdf</link>"
            "<description>d</description></item></rdf:RDF>"
        )
        self.assertEqual(self.parse(payload).items[0].url, "https://media.test/rdf")


class TestPublishedTimeParsing(SimpleTestCase):
    def test_rfc822_with_each_offset_lands_on_the_same_instant(self):
        """三个 RSS 源实测混用 `+0000` / `GMT` / `+0200` 三种写法。"""
        for text in (
            "Mon, 21 Sep 2026 18:00:00 +0000",
            "Mon, 21 Sep 2026 18:00:00 GMT",
            "Mon, 21 Sep 2026 20:00:00 +0200",
        ):
            with self.subTest(text=text):
                self.assertEqual(
                    parse_published(text),
                    datetime(2026, 9, 21, 18, 0, tzinfo=timezone.utc),
                )

    def test_rfc3339_with_z_is_understood(self):
        self.assertEqual(
            parse_published("2026-09-21T18:00:00Z"),
            datetime(2026, 9, 21, 18, 0, tzinfo=timezone.utc),
        )

    def test_a_naive_time_is_read_as_utc(self):
        """库内时间口径恒为 UTC：把裸时间当成本地时区会让同一条资讯的发布时刻
        在不同机器上差出几个小时。"""
        parsed = parse_published("2026-09-21T18:00:00")
        self.assertEqual(parsed, datetime(2026, 9, 21, 18, 0, tzinfo=timezone.utc))
        self.assertIsNotNone(parsed.tzinfo)

    def test_everything_unrecognizable_is_none(self):
        for text in ("", "   ", "昨日 18:00", "2026-13-45T99:99:99Z"):
            with self.subTest(text=text):
                self.assertIsNone(parse_published(text))


# --------------------------------------------------------------------------- #
# 币安公告
# --------------------------------------------------------------------------- #


class TestBinanceParsing(SimpleTestCase):
    SOURCE = config.NewsSource(
        name="binance",
        url="https://binance.test/list",
        kind=config.NewsSourceKind.EXCHANGE,
        parser="binance_announcements",
    )

    def parse(self, payload):
        return news.PARSERS["binance_announcements"](self.SOURCE, payload)

    def test_the_list_envelope_is_read(self):
        # 显式传探测到的真实 releaseDate：它落在采集窗口之外，所以不能当默认值。
        feed = self.parse(binance_payload([binance_article(release=1790062264920)]))
        item = feed.items[0]
        self.assertEqual(item.title, "Binance Will Delist BTCUSDT Perpetual Contracts")
        # 1790062264920 是探测时抓下来的真实 releaseDate——毫秒，且带着零点九秒的小数。
        self.assertEqual(
            item.published_at,
            datetime(2026, 9, 22, 7, 31, 4, 920000, tzinfo=timezone.utc),
        )
        self.assertEqual(item.body, "")

    def test_the_url_is_synthesized_from_the_code(self):
        """接口不给 url 字段（实测字段只有 id/code/title/type/releaseDate），而 `code`
        正是详情接口的键——所以链接与正文指向同一个标识。"""
        feed = self.parse(binance_payload([binance_article(code="abc123")]))
        self.assertIn("abc123", feed.items[0].url)
        self.assertTrue(feed.items[0].url.startswith("https://www.binance.com/"))

    def test_articles_nested_under_sub_catalogs_are_collected(self):
        """目录下面还有一层 catalogs：只取第一层会在交易所调整目录结构时安静地
        少掉一批，而那与「今天没发公告」长得一样。"""
        catalogs = [
            {"catalogId": 1, "articles": [binance_article(code="top")], "catalogs": []},
            {
                "catalogId": 2,
                "articles": [],
                "catalogs": [
                    {
                        "catalogId": 3,
                        "articles": [binance_article(code="deep")],
                        "catalogs": [],
                    }
                ],
            },
        ]
        feed = self.parse(binance_payload([], catalogs=catalogs))
        self.assertEqual(
            [i.url.rpartition("/")[2] for i in feed.items], ["top", "deep"]
        )

    def test_an_article_missing_code_or_title_is_counted(self):
        feed = self.parse(
            binance_payload([binance_article(code=""), binance_article(title="")])
        )
        self.assertEqual(feed.malformed, 2)
        self.assertEqual(feed.items, ())

    def test_a_missing_catalog_list_raises(self):
        """信封变了要抛。`total` 实测在目录上、`data.total` 是 None，所以这里也把
        catalogs 拿掉——返回 None 而不报错，就正好是那个会安静 0 条的形状。"""
        payload = json.dumps(
            {"code": "000000", "data": {"total": 40, "catalogs": None}}
        )
        with self.assertRaises(NewsFetchError):
            self.parse(payload)

    def test_a_non_json_body_raises(self):
        """探测实测：走代理请求公告详情页返回 **202 + 2110 字节的通用外壳页**——
        `raise_for_status()` 拦不住它（202 是 2xx），只有信封校验能把它变成失败。"""
        with self.assertRaises(NewsFetchError):
            self.parse(
                '<!DOCTYPE html><html><head><meta charset="utf-8"></head></html>'
            )


class TestOkxParsing(SimpleTestCase):
    SOURCE = config.NewsSource(
        name="okx",
        url="https://okx.test/announcements",
        kind=config.NewsSourceKind.EXCHANGE,
        parser="okx_announcements",
    )

    def parse(self, payload):
        return news.PARSERS["okx_announcements"](self.SOURCE, payload)

    def test_the_envelope_is_read(self):
        """实测形状：`data` 是**长度 1 的列表**，条目在 `data[0]['details']` 里；
        `pTime` 是毫秒的**字符串**（币安那边是 int）。"""
        # 显式传探测到的真实 pTime（字符串形式，且在采集窗口之外）——理由见 `okx_detail`。
        feed = self.parse(okx_payload([okx_detail(ptime="1790052307266")]))
        item = feed.items[0]
        self.assertEqual(item.title, "OKX will delist KMN/USD perpetual")
        self.assertEqual(item.url, "https://www.okx.test/help/list-kmn")
        self.assertEqual(
            item.published_at,
            datetime(2026, 9, 22, 4, 45, 7, 266000, tzinfo=timezone.utc),
        )
        self.assertEqual(item.body, "")

    def test_the_body_stays_empty_because_the_interface_has_none(self):
        """实测 detail 字段只有 annType/title/url/pTime/businessPTime，没有正文。
        留空不去抓公告页：公告页无契约，为它写抓取器等于把一个会静默返回 0 条的
        依赖放进链路。"""
        self.assertEqual(self.parse(okx_payload([okx_detail()])).items[0].body, "")

    def test_details_missing_url_or_title_are_counted(self):
        feed = self.parse(okx_payload([okx_detail(url=""), okx_detail(title="")]))
        self.assertEqual(feed.malformed, 2)
        self.assertEqual(feed.items, ())

    def test_a_non_list_data_field_raises(self):
        payload = json.dumps({"code": "0", "data": {"details": []}})
        with self.assertRaises(NewsFetchError):
            self.parse(payload)


class TestRichTextFlattening(SimpleTestCase):
    def test_a_real_shaped_tree_becomes_readable_prose(self):
        """正文是一棵 `{node, tag, child}` 树（实测 `data.body`，展平后 8137 字）。"""
        tree = {
            "node": "element",
            "tag": "div",
            "child": [
                {
                    "node": "element",
                    "tag": "p",
                    "child": [
                        {"node": "text", "text": "Binance will list "},
                        {
                            "node": "element",
                            "tag": "b",
                            "child": [{"node": "text", "text": "KMN"}],
                        },
                    ],
                },
                {
                    "node": "element",
                    "tag": "p",
                    "child": [{"node": "text", "text": "Details below."}],
                },
            ],
        }
        self.assertEqual(
            flatten_rich_text(tree), "Binance will list KMN\nDetails below."
        )

    def test_garbage_nodes_do_not_raise(self):
        for payload in (
            None,
            [],
            {"node": "element"},
            {"node": "text", "text": None},
            42,
        ):
            with self.subTest(payload=payload):
                self.assertIsInstance(flatten_rich_text(payload), str)


# --------------------------------------------------------------------------- #
# 窗口与预筛
# --------------------------------------------------------------------------- #


class TestWindow(SimpleTestCase):
    """`(上次判定成功, 本次判定]`，两端夹在 `[floor, cap]`。"""

    def test_cold_start_uses_the_cap(self):
        start, end = compute_window(NOW, None)
        self.assertEqual(start, NOW - timedelta(hours=96))
        self.assertEqual(end, NOW)

    def test_a_recent_success_stretches_back_to_the_floor(self):
        """同一天里的第二次心跳、或一次重试：真正的语义是「今天这一天有什么新
        资讯」，不是「最近这五分钟」。钉在一天以上之后，重复运行看到同一批条目，
        配合 url 去重 ⇒ 整条通道可幂等重试。"""
        start, _ = compute_window(NOW, NOW - timedelta(hours=2))
        self.assertEqual(start, NOW - timedelta(hours=24))

    def test_a_normal_gap_uses_the_last_success(self):
        last = NOW - timedelta(hours=48)
        self.assertEqual(compute_window(NOW, last)[0], last)

    def test_a_long_outage_is_clamped_to_the_cap(self):
        """判定停了五天又恢复：不夹上限的话，攒下的条目会一口气送进 LLM，而那一批
        描述的可能是几天前的市场，却按今天的结论生效。

        停摆必须**长过上限**才谈得上夹——三天是 72 小时，还在 96 小时以内，走的是
        「正常间隔」那一支。这里用五天，让上限真的成为那个起作用的东西。
        """
        start, _ = compute_window(NOW, NOW - timedelta(days=5))
        self.assertEqual(start, NOW - timedelta(hours=96))

    def test_the_cap_boundary_is_the_cap_itself(self):
        """正好 96 小时不夹，多一小时才夹——边界落在哪一侧是「差一分钟算几天」那类
        争执的来源，所以两侧都钉住。"""
        self.assertEqual(
            compute_window(NOW, NOW - timedelta(hours=96))[0], NOW - timedelta(hours=96)
        )
        self.assertEqual(
            compute_window(NOW, NOW - timedelta(hours=97))[0], NOW - timedelta(hours=96)
        )

    def test_the_derived_config_is_honoured(self):
        tighter = replace(config.NEWS, window_floor_hours=2, window_cap_hours=6)
        self.assertEqual(
            compute_window(NOW, None, tighter)[0], NOW - timedelta(hours=6)
        )
        self.assertEqual(
            compute_window(NOW, NOW - timedelta(hours=1), tighter)[0],
            NOW - timedelta(hours=2),
        )


class TestPrefilter(SimpleTestCase):
    def pub(self, **kwargs):
        return RawItem(
            **{
                "url": "https://media.test/a",
                "title": "t",
                "published_at": None,
                **kwargs,
            }
        )

    def test_the_window_is_left_open_and_right_closed(self):
        """左开右闭：`start` 那一篇上一轮已经见过了（去重键是 url，重复运行因此
        不会重复送），而 `end` 那一篇是本轮刚出现的。"""
        start = NOW - timedelta(hours=24)
        self.assertFalse(in_window(self.pub(published_at=start), start, NOW))
        self.assertTrue(in_window(self.pub(published_at=NOW), start, NOW))
        self.assertTrue(
            in_window(self.pub(published_at=NOW - timedelta(seconds=1)), start, NOW)
        )

    def test_an_item_without_a_published_time_is_kept(self):
        """「未知」不是「很旧」：丢掉它，窗口过滤就成了一处会安静吞掉条目的地方。"""
        self.assertTrue(in_window(self.pub(), NOW - timedelta(hours=24), NOW))

    def test_keywords_match_title_and_body_case_insensitively(self):
        keywords = ("bitcoin", "美联储")
        self.assertTrue(matches_keywords(self.pub(title="Bitcoin ETFs"), keywords))
        self.assertTrue(
            matches_keywords(self.pub(title="ETF", body="bitcoin flows"), keywords)
        )
        self.assertTrue(matches_keywords(self.pub(title="美联储按兵不动"), keywords))
        self.assertFalse(matches_keywords(self.pub(title="Ethereum upgrade"), keywords))

    def test_the_default_keyword_list_covers_both_languages(self):
        self.assertIn("bitcoin", config.NEWS.keywords)
        self.assertIn("美联储", config.NEWS.keywords)


# --------------------------------------------------------------------------- #
# 一轮采集
# --------------------------------------------------------------------------- #

BINANCE = config.NewsSource(
    name="binance",
    url="https://binance.test/list",
    kind=config.NewsSourceKind.EXCHANGE,
    parser="binance_announcements",
)
OKX = config.NewsSource(
    name="okx",
    url="https://okx.test/announcements",
    kind=config.NewsSourceKind.EXCHANGE,
    parser="okx_announcements",
)
MEDIA = config.NewsSource(
    name="media",
    url="https://media.test/rss",
    kind=config.NewsSourceKind.CRYPTO_MEDIA,
)
KINDS = (BINANCE, OKX, MEDIA)

BINANCE_DETAIL_PREFIX = (
    "https://www.binance.com/bapi/composite/v1/public/cms/article/detail/query"
)
BINANCE_BODY_TREE = {
    "node": "element",
    "tag": "div",
    "child": [
        {
            "node": "element",
            "tag": "p",
            "child": [{"node": "text", "text": "Binance will list KMN."}],
        },
    ],
}


class TestCollectRound(TestCase):
    """一轮采集的端到端行为。取数一律注入，测试不联网。"""

    def config(self, **overrides):
        return replace(config.NEWS, sources=KINDS, **overrides)

    def collect(self, fetch, **kwargs):
        kwargs.setdefault("last_success", NOW - timedelta(hours=2))
        return news.collect_news(
            now=NOW,
            news=self.config(**kwargs.pop("news_overrides", {})),
            fetch=fetch,
            **kwargs,
        )

    def test_a_clean_round_stores_the_matched_items(self):
        # 三个源都要登记：`ok` 的语义是「**每个**源都成功」，漏登一个就等于在断言
        # 「测试替身登记全了没有」，而不是这一轮采集本身。
        fetch = FakeFetcher(
            {
                "https://binance.test/list": binance_payload([]),
                "https://media.test/rss": rss(
                    rss_item(
                        title="Bitcoin ETF inflows hit a record",
                        link="https://media.test/a",
                    ),
                    # description 默认值本身是命中关键词的，所以「换个不命中的标题」不足
                    # 以让这条不命中——标题与描述都会进预筛的干草堆，两边都要换掉。
                    rss_item(
                        title="Ethereum client release",
                        link="https://media.test/b",
                        description=None,
                    ),
                ),
                "https://okx.test/announcements": okx_payload(
                    [
                        okx_detail(
                            title="OKX will delist XYZ",
                            url="https://www.okx.test/help/delist",
                        )
                    ]
                ),
            }
        )
        report = self.collect(fetch)

        self.assertTrue(report.ok, report.failed_sources)
        # 关键词把 Ethereum 那条挡在门外：没进这一批 = 没花掉 token。
        self.assertEqual(
            sorted(i.url for i in report.items),
            ["https://media.test/a", "https://www.okx.test/help/delist"],
        )
        self.assertEqual(report.created, 2)
        self.assertEqual(NewsItem.objects.count(), 2)

        stored = NewsItem.objects.get(url="https://media.test/a")
        self.assertEqual(stored.source, "media")
        self.assertEqual(stored.kind, config.NewsSourceKind.CRYPTO_MEDIA.value)
        self.assertEqual(
            stored.published_at, datetime(2026, 9, 21, 18, 0, tzinfo=timezone.utc)
        )
        self.assertEqual(stored.fetched_at, NOW)

    def test_each_source_reports_its_own_counts(self):
        fetch = FakeFetcher(
            {
                "https://media.test/rss": rss(
                    rss_item(link="https://media.test/a"),
                    rss_item(link="https://media.test/b"),
                    # 标题与描述都要换成不命中的，见 `test_a_clean_round_stores_the_matched_items`。
                    rss_item(
                        title="Ethereum client release",
                        link="https://media.test/c",
                        description=None,
                    ),
                    rss_item(link=None),
                ),
                "https://okx.test/announcements": okx_payload([]),
            }
        )
        report = self.collect(fetch)
        by_name = {r.source: r for r in report.sources}

        # `fetched` 与 `malformed` 是**互斥**的两堆：接口一共给了 4 条，其中 1 条
        # （没有链接的那条）没能变成条目，所以 fetched 是 3。写成 4 会让这两个字段
        # 重叠计数，而「重叠」正是「今天到底收到几条」开始对不上的起点。
        self.assertEqual(by_name["media"].fetched, 3)
        self.assertEqual(by_name["media"].malformed, 1)
        self.assertEqual(by_name["media"].fetched + by_name["media"].malformed, 4)
        self.assertEqual(by_name["media"].in_window, 3)
        self.assertEqual(by_name["media"].matched, 2)
        # okx 返回 0 条：`fetched=0` 而 `ok=True`——「今天没发东西」与「源挂了」不同。
        self.assertEqual(by_name["okx"].fetched, 0)
        self.assertTrue(by_name["okx"].ok)
        self.assertEqual(by_name["okx"].error, "")

    def test_one_dead_source_does_not_take_down_the_round(self):
        """七个源里挂一个，剩下的条目照样该被看到——这是与 `candles.py` 刻意相反的
        那一处（日线只有一个源，源失败就等于没有输入）。"""
        fetch = FakeFetcher(
            {
                "https://media.test/rss": rss(rss_item(link="https://media.test/a")),
                "https://binance.test/list": NewsFetchError("连接超时"),
                "https://okx.test/announcements": okx_payload([okx_detail()]),
            }
        )
        report = self.collect(fetch)

        self.assertFalse(report.ok)
        self.assertEqual(report.failed_sources, ("binance",))
        by_name = {r.source: r for r in report.sources}
        self.assertIn("连接超时", by_name["binance"].error)
        self.assertEqual(by_name["binance"].ok, False)
        # 挂了的那一个之外，条目照常入库。
        self.assertEqual(report.created, 2)

    def test_a_source_whose_envelope_changed_is_a_visible_failure(self):
        """币安公告详情页那种响应（202 + 通用外壳页）在这里必须变成一次失败，
        而不是「今天 0 条」。"""
        fetch = FakeFetcher(
            {"https://binance.test/list": "<!DOCTYPE html><html></html>"}
        )
        report = self.collect(fetch)
        by_name = {r.source: r for r in report.sources}
        self.assertFalse(by_name["binance"].ok)
        self.assertIn("binance", by_name["binance"].error)

    def test_binance_bodies_are_fetched_per_article_and_persisted(self):
        fetch = FakeFetcher(
            {
                "https://binance.test/list": binance_payload(
                    [
                        binance_article(
                            code="abc123", release=ms(NOW - timedelta(hours=3))
                        )
                    ]
                ),
                BINANCE_DETAIL_PREFIX: json.dumps(
                    {"data": {"body": BINANCE_BODY_TREE}}
                ),
            }
        )
        report = self.collect(fetch)

        self.assertEqual(report.created, 1)
        self.assertEqual(report.bodies_failed, 0)
        stored = NewsItem.objects.get()
        self.assertEqual(stored.body, "Binance will list KMN.")
        self.assertFalse(stored.body_truncated)
        # 正文是**按需**补的：一篇文章一次请求，且用的正是列表里那个 code。
        detail_calls = [u for u in fetch.urls if u.startswith(BINANCE_DETAIL_PREFIX)]
        self.assertEqual(len(detail_calls), 1)
        self.assertIn("abc123", detail_calls[0])

    def test_a_failed_body_fetch_keeps_the_item(self):
        """标题与链接本身就有信息量：「抓不到正文」与「没有这条资讯」是两件事。"""
        fetch = FakeFetcher(
            {
                "https://binance.test/list": binance_payload(
                    [
                        binance_article(
                            code="abc123", release=ms(NOW - timedelta(hours=3))
                        )
                    ]
                ),
                BINANCE_DETAIL_PREFIX: NewsFetchError("详情页超时"),
            }
        )
        report = self.collect(fetch)

        self.assertEqual(report.created, 1)
        self.assertEqual(report.bodies_failed, 1)
        self.assertEqual(NewsItem.objects.get().body, "")

    def test_bodies_are_not_fetched_for_items_that_are_already_stored(self):
        """库里已有的 url 直接由去重挡下：它上一轮已经进过 LLM 的输入，重新下一遍
        正文只是把同一批字节再取一次。"""
        url = news.binance_article_url("abc123")
        NewsItem.objects.create(
            source="binance",
            kind=config.NewsSourceKind.EXCHANGE.value,
            url=url,
            title="Binance Will Delist BTCUSDT Perpetual Contracts",
            published_at=NOW - timedelta(hours=3),
            body="already stored",
            body_truncated=False,
            fetched_at=NOW - timedelta(hours=1),
        )
        fetch = FakeFetcher(
            {
                "https://binance.test/list": binance_payload(
                    [
                        binance_article(
                            code="abc123", release=ms(NOW - timedelta(hours=3))
                        )
                    ]
                ),
                BINANCE_DETAIL_PREFIX: json.dumps(
                    {"data": {"body": BINANCE_BODY_TREE}}
                ),
            }
        )
        report = self.collect(fetch)

        self.assertEqual(report.duplicates, 1)
        self.assertEqual(report.created, 0)
        self.assertEqual(report.items, ())
        self.assertNotIn(BINANCE_DETAIL_PREFIX, " ".join(fetch.urls))
        self.assertEqual(NewsItem.objects.get().body, "already stored")

    def test_the_same_url_from_two_sources_is_stored_once(self):
        """转载是常态，而 url 是唯一键：只能有一个赢家。赢家由配置表里的**声明顺序**
        先到先得——`KINDS` 与 `config.NEWS_SOURCES` 同序，okx 排在 media 前面。"""
        shared = "https://media.test/shared"
        fetch = FakeFetcher(
            {
                "https://media.test/rss": rss(rss_item(link=shared)),
                "https://okx.test/announcements": okx_payload([okx_detail(url=shared)]),
            }
        )
        report = self.collect(fetch)
        self.assertEqual(report.duplicates, 1)
        self.assertEqual(report.created, 1)
        self.assertEqual(NewsItem.objects.get().source, "okx")

    def test_the_cap_keeps_the_newest_and_records_what_it_dropped(self):
        """上限存在的理由不是成本，是 LLM 的判断力：一次给 200 条，它会挑几条盯着看
        而漏掉真正的大事件。所以留下的是**最新**的那批。"""
        articles = [
            binance_article(code=f"c{i}", release=ms(NOW - timedelta(hours=i + 1)))
            for i in range(5)
        ]
        fetch = FakeFetcher(
            {
                "https://binance.test/list": binance_payload(articles),
                BINANCE_DETAIL_PREFIX: json.dumps({"data": {"body": "x"}}),
            }
        )
        report = self.collect(fetch, news_overrides={"max_items_to_llm": 2})

        self.assertEqual(report.candidates, 5)
        self.assertEqual(report.dropped_by_cap, 3)
        self.assertEqual(report.created, 2)
        self.assertTrue(all("c0" in i.url or "c1" in i.url for i in report.items))

    def test_a_source_returning_more_than_the_per_source_cap_is_truncated(self):
        articles = [
            binance_article(code=f"c{i}", release=ms(NOW - timedelta(minutes=i + 1)))
            for i in range(4)
        ]
        fetch = FakeFetcher(
            {
                "https://binance.test/list": binance_payload(articles),
                BINANCE_DETAIL_PREFIX: json.dumps({"data": {"body": "x"}}),
            }
        )
        report = self.collect(fetch, news_overrides={"max_items_per_source": 2})
        by_name = {r.source: r for r in report.sources}
        # 截断发生在**源**这一层，所以 `fetched` 如实报 4（接口给了 4 条），
        # 而进入窗口的只有 2 条。
        self.assertEqual(by_name["binance"].fetched, 4)
        self.assertEqual(by_name["binance"].in_window, 2)

    def test_a_long_body_is_truncated_with_the_flag_set(self):
        long_body = "字" * 500
        body_tree = {
            "node": "element",
            "tag": "p",
            "child": [{"node": "text", "text": long_body}],
        }
        fetch = FakeFetcher(
            {
                "https://binance.test/list": binance_payload(
                    [binance_article(code="abc", release=ms(NOW - timedelta(hours=1)))]
                ),
                BINANCE_DETAIL_PREFIX: json.dumps({"data": {"body": body_tree}}),
            }
        )
        report = self.collect(fetch, news_overrides={"body_max_chars": 100})
        stored = report.items[0]
        self.assertEqual(len(stored.body), 100)
        self.assertTrue(stored.body_truncated)

    def test_items_outside_the_window_are_dropped(self):
        fetch = FakeFetcher(
            {
                "https://media.test/rss": rss(
                    rss_item(
                        link="https://media.test/fresh",
                        pub="Mon, 21 Sep 2026 18:00:00 +0000",
                    ),
                    # 窗口是 (NOW-24h, NOW] = (09-21T00:05, 09-22T00:05]
                    rss_item(
                        link="https://media.test/old",
                        pub="Sat, 19 Sep 2026 18:00:00 +0000",
                    ),
                )
            }
        )
        report = self.collect(fetch)
        self.assertEqual([i.url for i in report.items], ["https://media.test/fresh"])
        self.assertEqual({r.source: r.in_window for r in report.sources}["media"], 1)

    def test_an_item_without_a_published_time_survives_the_round(self):
        fetch = FakeFetcher({"https://media.test/rss": rss(rss_item(pub=None))})
        report = self.collect(fetch)
        self.assertEqual(report.created, 1)
        self.assertIsNone(NewsItem.objects.get().published_at)

    def test_a_rerun_in_the_same_day_stores_nothing_new(self):
        """整条通道可幂等重试：同一天里再跑一次，窗口被拉长到一天以上，看到的是
        同一批条目，而 url 去重让它们一条都进不了库。"""
        fetch = FakeFetcher(
            {"https://media.test/rss": rss(rss_item(link="https://media.test/a"))}
        )
        first = self.collect(fetch)
        second = self.collect(fetch)

        self.assertEqual(first.created, 1)
        self.assertEqual(second.created, 0)
        self.assertEqual(second.duplicates, 1)
        self.assertEqual(NewsItem.objects.count(), 1)

    def test_dry_run_writes_nothing(self):
        fetch = FakeFetcher(
            {"https://media.test/rss": rss(rss_item(link="https://media.test/a"))}
        )
        report = self.collect(fetch, dry_run=True)

        self.assertTrue(report.dry_run)
        self.assertEqual(report.created, 0)
        self.assertEqual(len(report.items), 1)
        self.assertEqual(NewsItem.objects.count(), 0)

    def test_a_naive_now_is_rejected(self):
        """库内时间口径恒为 UTC：一个裸 `datetime` 会让窗口的两端在别的时区里漂。"""
        with self.assertRaises(ValueError):
            news.collect_news(now=datetime(2026, 9, 22, 0, 5), fetch=FakeFetcher({}))

    def test_the_report_window_is_the_one_used(self):
        report = self.collect(FakeFetcher({}))
        self.assertEqual(report.window_end, NOW)
        self.assertEqual(report.window_start, NOW - timedelta(hours=24))

    def test_every_source_is_attempted_even_after_failures(self):
        fetch = FakeFetcher(
            {
                "https://binance.test/list": NewsFetchError("boom"),
                "https://okx.test/announcements": Exception("非 NewsFetchError 的意外"),
            }
        )
        report = self.collect(fetch)
        # 三个源都发过请求：一个源失败不能让它后面的源不被尝试。
        self.assertEqual(
            [u for u in fetch.urls],
            [
                "https://binance.test/list",
                "https://okx.test/announcements",
                "https://media.test/rss",
            ],
        )
        self.assertEqual(len(report.sources), 3)

    def test_an_unexpected_exception_from_the_fetcher_is_not_a_round_killer(self):
        """`http_get` 把一切异常包成 `NewsFetchError`，但注入的取数实现未必——而
        「一个源抛了个别的异常」不该让整轮采集消失在一次未捕获异常里。

        错误串带上异常类名：日报里这一行与「源挂了」要能分辨，前者指向代码，后者
        等下一轮或去修网络。
        """
        fetch = FakeFetcher(
            {
                "https://okx.test/announcements": Exception("非 NewsFetchError"),
                "https://media.test/rss": rss(rss_item(link="https://media.test/a")),
            }
        )
        report = self.collect(fetch)
        by_name = {r.source: r for r in report.sources}

        self.assertFalse(report.ok)
        self.assertFalse(by_name["okx"].ok)
        self.assertTrue(by_name["okx"].error.startswith("Exception:"))
        # 坏掉的那个源之外，条目照常入库 —— 「兜住」的意义就在这里。
        self.assertEqual(report.created, 1)
