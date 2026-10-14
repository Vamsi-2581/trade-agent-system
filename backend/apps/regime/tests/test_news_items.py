"""资讯条目原文表（第①段单元 5）。

这张表存的是「采集器看到了什么」，它的几条硬约束各自对应一类会让整条资讯通道静默
失真的写法，所以逐条钉住：

1. **跨天去重的唯一键是 `url`**——去重必须落在一个持久的集合上，进程内去重在每次
   运行都从零开始，等于没有。「今天 0 条」因此有了确切含义：窗口内没有未见过的新
   条目；它与「某个源抓取失败」是两件事，后者由采集轮次的逐源结果回答。
2. **只增不删，行不改写**——没有 `auto_now` 字段，因为没有「上次更新」这回事。
3. **类别取值就是 `NewsSourceKind`**——两边一旦漂移，日报里的分类计数会静默地掉进
   第四类。
"""

from __future__ import annotations

from datetime import datetime, timezone

from django.db import IntegrityError, transaction
from django.test import TestCase

from apps.regime import config
from apps.regime.models import NewsItem

FETCHED = datetime(2026, 9, 22, 0, 5, tzinfo=timezone.utc)


def make(url="https://example.com/a", **overrides):
    fields = {
        "source": "cointelegraph",
        "kind": config.NewsSourceKind.CRYPTO_MEDIA.value,
        "url": url,
        "title": "Bitcoin ETF inflows hit a record",
        "published_at": datetime(2026, 9, 21, 18, 0, tzinfo=timezone.utc),
        "body": "Bitcoin ETF inflows hit a record...",
        "body_truncated": False,
        "fetched_at": FETCHED,
    }
    fields.update(overrides)
    return NewsItem.objects.create(**fields)


class TestUrlIsTheCrossDayDedupKey(TestCase):
    def test_the_same_url_cannot_be_stored_twice(self):
        """同一条资讯在多个来源、多天里重复出现是常态：唯一键必须落在 url 上，
        否则每次运行都会重新插一遍同一篇，条目表会翻倍增长而内容不变。"""
        make()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                make(source="theblock", kind=config.NewsSourceKind.CRYPTO_MEDIA.value)

    def test_different_urls_coexist(self):
        make(url="https://example.com/a")
        make(url="https://example.com/b")
        self.assertEqual(NewsItem.objects.count(), 2)


class TestNothingIsRewritten(TestCase):
    def test_no_field_rewrites_itself_on_save(self):
        """整张表没有 `auto_now`：条目是化石，不是缓存。有「上次更新」就等于承认
        同一行可以被改写，而「今天为什么没判出抬升」要读的正是当初那一行。

        `created_at` 用的是 `auto_now_add`，那是**写入时刻**，不是更新时间——它是
        这条记录进入库的时间，不会被后来的任何一次 save 改动。
        """
        for field in NewsItem._meta.get_fields():
            with self.subTest(field=getattr(field, "name", field)):
                self.assertFalse(
                    getattr(field, "auto_now", False),
                    f"{field.name} 带 auto_now：条目一旦被 save 就会被改写",
                )

    def test_created_at_is_set_once(self):
        item = make()
        self.assertIsNotNone(item.created_at)


class TestKindVocabularyMatchesTheConfigEnum(TestCase):
    def test_model_choices_are_the_config_enum(self):
        """类别的取值只有一个来源（config.NewsSourceKind）：模型自己抄一份，
        日报按类别计数时就会出现「有第五个类别谁都没见过」。"""
        choices = dict(NewsItem._meta.get_field("kind").choices)
        self.assertEqual(set(choices), {m.value for m in config.NewsSourceKind})
        self.assertEqual(choices["exchange"], config.NewsSourceKind.EXCHANGE.display)


class TestTruncationIsVisible(TestCase):
    def test_a_short_original_is_distinguishable_from_a_cut_one(self):
        """`body_truncated` 让「原文就这么短」与「我们只取了前 N 字」可分辨：少了它，
        一条被截断到关键句之前的条目看起来就像一条无关的短讯。"""
        short = make(url="https://example.com/short", body="ok", body_truncated=False)
        cut = make(url="https://example.com/cut", body="x" * 2000, body_truncated=True)
        self.assertFalse(short.body_truncated)
        self.assertTrue(cut.body_truncated)

    def test_body_may_be_empty(self):
        """正文抓不到不是丢弃这条的理由：标题与链接本身就有信息量，而「抓不到正文」
        与「没有这条资讯」是两件事。"""
        item = make(url="https://example.com/empty", body="")
        self.assertEqual(item.body, "")


class TestMissingPublishedTimeIsNotADropReason(TestCase):
    def test_items_without_a_published_time_are_stored(self):
        """有的 feed 不给发布时间：那是**未知**，不是「很旧」。把它丢掉会让预筛的
        时间倒序变成「悄悄少几条」。"""
        item = make(url="https://example.com/undated", published_at=None)
        self.assertIsNone(item.published_at)
        self.assertEqual(NewsItem.objects.count(), 1)


class TestOrderingIsNewestFirst(TestCase):
    def test_default_ordering_is_newest_fetched_first(self):
        """默认排序钉住：预筛取「来得及反应」的那批时按时间倒序，靠的就是这里。"""
        make(
            url="https://example.com/old",
            fetched_at=datetime(2026, 9, 20, tzinfo=timezone.utc),
        )
        make(
            url="https://example.com/new",
            fetched_at=datetime(2026, 9, 22, tzinfo=timezone.utc),
        )
        self.assertEqual(
            [i.url for i in NewsItem.objects.all()],
            ["https://example.com/new", "https://example.com/old"],
        )


class TestFetchedAtIsAnInstantNotABusinessDay(TestCase):
    def test_fetched_at_keeps_the_exact_instant(self):
        """采集窗口是增量区间（上次判定成功 → 本次判定），边界是绝对时刻而不是自然
        日界。如果这里退化成「业务日」，窗口的两个端点就都失去了精度，而症状是
        「有时候会漏掉一整天边缘的条目」——只在跨日附近复现。"""
        item = make(fetched_at=FETCHED)
        item.refresh_from_db()
        self.assertEqual(item.fetched_at, FETCHED)
