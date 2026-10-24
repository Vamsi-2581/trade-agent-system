"""机制 Web 读面的三个只读端点（P5 子票 #41，map #40）。

`test_pool_rebuild.py` 钉的是「一代怎么落成」；这个文件钉的是「一代怎么被读出去」。
判定与落库的逻辑在这里一行都不断言——那会是第二处真相。

七条性质，每一条坏了都不报警、只出错：

1. **认证必需**：三条路由匿名一律 401。读对所有登录用户开放（`CONTEXT.md` 的
   「读对所有登录用户，写要 `is_staff`」），所以这条只钉「有没有登录」，不钉角色。
2. **冷启动不 500，而且要说出来**：没有当前代时 `pool` 返回空格子 + `has_generation=false`
   + 一句后端渲染的话。**不能只是 `null`**：`null` 只说了「没有」，没说是「从没重算过」
   还是「查询失败」，而这两件事要求人做的动作完全不同。
3. **枚举的 display 全部由后端给**：每个枚举在响应里都是 `{"value", "display"}` 成对。
   成对是**结构**保证（少一个不会报错、只会静默显示 slug），所以这条逐键断言。
4. **`state`/`reason` 的 display 与 `slice` 表同源，断言的是键集不是字符串**：断言
   字符串等于把词表抄进用例，抄错了用例照样绿。键集相等才挡得住「serializers 里复制了
   一份、而那份漏了一个成员」。
5. **`overview` 的查询数有上界**：`OVERVIEW_QUERY_BUDGET` 钉住「它不会退化成调
   `mechanism_switch.page()` / `gate_switch.page()`」。另有一条 patch 断言：三条 `page()`
   **一次都没被调用**——查询数只是那把尺子，这条是尺子量不到的那一半。
6. **代际分页与差异**：分页形状 `{count, page, page_size, has_next, results}`；
   `?generation=<id>` 给出与上一代的逐格差异，且 `changed + added + removed` 必须等于
   记录里的 `cells_changed`（两处口径分叉时，页面上说「变了 3 格」而列表里有 5 条，
   谁都不会报错）。
7. **`generation` 显式配对**：`pool` 端点把同一个代对象既用于取 id 又用于取格子。
   不显式传的话 `current_cells()` 会自己再问一次当前代，两次问之间翻了一代就是「依据写
   的是第 N 代，引用的却是第 N+1 代」。这条用「取格子的那一刻把当前代换掉」来钉。

夹具直接复用 `test_gate_run._Fixture`（三个策略 + 一个用户）：两处各造一套世界，迟早会
出现「同一个页面在两个文件里算出两个数」。
"""

from __future__ import annotations

import gzip
import itertools
from datetime import timedelta
from typing import Any
from unittest.mock import patch

from django.test import TestCase
from rest_framework.test import APIClient

from apps.regime import breaker_switch, mechanism_switch, pool, pool_rebuild
from apps.regime import gate_switch
from apps.regime import serializers as ser
from apps.regime import slice as sl
from apps.regime import views
from apps.regime.models import (
    ActorKind,
    MechanismKind,
    MechanismMode,
    RebuildStatus,
    RegimeJudgement,
    RegimeMechanismSwitch,
    RegimePoolCell,
    RegimePoolRebuild,
)
from apps.regime.pool import POOL_SOURCE_ARCHETYPE, POOL_SOURCE_STRATEGY
from apps.regime.quant import PRIORITY, BaseRegime
from apps.regime.tests.test_gate_run import (
    NOW,
    _Fixture as _GateRunFixture,
    add_cell,
    days,
)

DOWNTREND = BaseRegime.DOWNTREND
RANGE = BaseRegime.RANGE

OVERVIEW = "/api/regime/overview/"
POOL = "/api/regime/pool/"
POOL_CELL = "/api/regime/pool/cell/"
GENERATIONS = "/api/regime/pool/generations/"

#: 与 `test_deactivation_run` 同一条理由：就绪代的指纹有部分唯一约束，同一类里每次造代
#: 都得给一个新指纹，让计数器兜底。
_fingerprints = itertools.count(1)


class _Fixture(_GateRunFixture):
    """在 ③c 的夹具上加两件事：一个能登录的客户端，一个「有当代」的世界。"""

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    # --- 造数据 ------------------------------------------------------------ #

    def make_gen(
        self,
        *,
        cells: tuple = (),
        status: str = RebuildStatus.READY.value,
        finished_at: Any = "default",
        started_at: Any = None,
        failure: str = "",
    ) -> RegimePoolRebuild:
        """一代池化表。`cells` 的每一项是 `(策略, 阶段, kwargs)`，形状照 `test_gate_run`。

        **不转调 `test_gate_run.make_generation`**：那个夹具把 `finished_at` 兜成 `NOW`
        （它要钉的是「按完成时刻取最近」，留空会让 PostgreSQL 的 NULL 排到最前）。本文件
        要钉的恰好是反过来的两件事——`building` 那一代**没有** `finished_at`，而代际分页
        要按 `started_at` 排出真实先后。所以这里自己建行，两个时刻各归各的。
        """
        moment = NOW if finished_at == "default" else finished_at
        generation = RegimePoolRebuild.objects.create(
            status=status,
            actor_kind=ActorKind.TASK.value,
            actor_name="任务",
            pool_version=pool.POOL_VERSION,
            input_fingerprint=f"f{next(_fingerprints)}",
            started_at=started_at or moment or NOW,
            finished_at=moment,
            failure=failure,
            cells_total=len(cells),
        )
        for strategy, regime, kwargs in cells:
            add_cell(generation, strategy, regime, **kwargs)
        return generation

    def judged(self, *, regime: str = DOWNTREND, effective_at=None):
        """一条生效中的判定（`current_regime_state` 只读它的 `effective_regime` 与
        `effective_at`）。与 `test_gate_run._Fixture.judged` 同形，但**覆盖**它：那边
        的默认 `effective_at` 是 `NOW`，而 `current_regime_state()` 不注入时钟，于是
        「现在生效」得落在真实时间之前。"""
        moment = effective_at or (NOW - timedelta(days=30))
        return RegimeJudgement.objects.create(
            symbol="BTC/USDT",
            attribute_date=(moment - timedelta(days=2)).date(),
            effective_at=moment,
            base_regime=regime,
            effective_regime=regime,
        )

    def shared_evidence(self) -> dict:
        """一份「整份 dict 会被引用两次」的证据：`test_views` 的按需拉那组用它。"""
        return {
            "pool_version": 3,
            "results": [{"result_id": "r-1", "symbol": "BTC/USDT"}],
            "symbols": ["BTC/USDT"],
            "trades": 40,
            "months": 6,
        }

    def switch_row(self, kind: MechanismKind, mode: MechanismMode, *, at=None):
        return RegimeMechanismSwitch.objects.create(
            kind=kind.value,
            from_mode=MechanismMode.SHADOW.value,
            to_mode=mode.value,
            at=at or days(-3),
            actor_kind=ActorKind.CLI.value,
            actor_name="ops",
            reason="测试",
        )

    # --- 三个端点 ---------------------------------------------------------- #

    def overview(self):
        return self.client.get(OVERVIEW)

    def pool(self, **params):
        return self.client.get(POOL, params)

    def generations(self, **params):
        return self.client.get(GENERATIONS, params)


# --------------------------------------------------------------------------- #
# 性质 1：认证必需
# --------------------------------------------------------------------------- #


class TestAuthRequired(TestCase):
    """三条路由匿名一律 401。**读对所有登录用户开放**——所以这里不造 staff 用户，
    默认用户就是「随便一个登录用户」，它必须读得通（那一条在 `TestColdStart` 里）。"""

    def setUp(self):
        self.anonymous = APIClient()

    def test_all_three_endpoints_reject_anonymous(self):
        for url in (OVERVIEW, POOL, GENERATIONS):
            with self.subTest(url=url):
                resp = self.anonymous.get(url)
                self.assertEqual(resp.status_code, 401, resp.content)
                self.assertIn("detail", resp.json())


# --------------------------------------------------------------------------- #
# 性质 2：冷启动
# --------------------------------------------------------------------------- #


class TestColdStart(_Fixture):
    """没有当代、没有判定：**空格子 + 明确标记**，不 500，也不装成「有结论但是空」。"""

    def test_overview_says_there_is_no_generation_at_all(self):
        body = self.overview().json()

        self.assertFalse(body["pool"]["has_generation"])
        self.assertIsNone(body["pool"]["generation"])
        # 不能只是 null：那句话必须由后端给，前端不重拼（map #40 的渲染分界）。
        self.assertEqual(body["pool"]["display"], views.EMPTY_POOL_DISPLAY)

    def test_overview_says_the_regime_state_is_cold(self):
        body = self.overview().json()

        state = body["regime_state"]
        self.assertTrue(state["cold"])
        self.assertIsNone(state["regime"])
        self.assertIsNone(state["effective_at"])
        self.assertEqual(state["display"], views.COLD_STATE_DISPLAY)

    def test_overview_lists_all_three_switches_at_shadow(self):
        """三个开关都要出现，且默认档是 Shadow——`current()` 的规则：一条流水都没有
        就是 Shadow，不为它落一行「初始切换」。"""
        body = self.overview().json()

        self.assertEqual(
            [row["kind"]["value"] for row in body["switches"]],
            [kind.value for kind in views.SWITCH_KINDS],
        )
        for row in body["switches"]:
            with self.subTest(kind=row["kind"]["value"]):
                self.assertEqual(row["mode"]["value"], MechanismMode.SHADOW.value)
                self.assertEqual(row["mode"]["display"], MechanismMode.SHADOW.display)
                self.assertIsNone(row["latest"])
                self.assertTrue(row["never_switched"])

    def test_only_the_mechanism_card_carries_the_not_an_execution_switch_note(self):
        """P5 #49：档位卡片上那句「出 Shadow 只是留一条记录」必须来自后端，且只有机制
        整体那一档有它。

        断言的是**同一个常量**（`mechanism_switch.NOT_EXECUTION_SWITCH`）而不是抄一遍
        句子：抄一遍之后，改词时用例照样绿、页面却还是旧话。另两个开关没有这句——它们
        才是真正拦人的那两个，在它们的卡片上写「不是执行开关」是一句反话。
        """
        notes = {
            row["kind"]["value"]: row["note"]
            for row in self.overview().json()["switches"]
        }

        self.assertEqual(
            notes[MechanismKind.MECHANISM.value], mechanism_switch.NOT_EXECUTION_SWITCH
        )
        self.assertIsNone(notes[MechanismKind.EVENT_BREAKER.value])
        self.assertIsNone(notes[MechanismKind.REGIME_GATE.value])

    def test_pool_returns_no_cells_without_an_error(self):
        resp = self.pool()

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertFalse(body["has_generation"])
        self.assertEqual(body["display"], views.EMPTY_POOL_DISPLAY)
        self.assertEqual(body["cells"], [])

    def test_pool_still_gives_the_four_columns(self):
        """冷启动下矩阵仍要有四列——否则前端要么画一个 0 列表格，要么自己补四档，
        而那就是「顺序的第二处真相」。"""
        body = self.pool().json()

        self.assertEqual(
            [row["value"] for row in body["regimes"]], [m.value for m in PRIORITY]
        )

    def test_generations_is_an_empty_page_not_an_error(self):
        body = self.generations().json()

        self.assertEqual(body["count"], 0)
        self.assertEqual(body["results"], [])
        self.assertFalse(body["has_next"])


# --------------------------------------------------------------------------- #
# 性质 3 + 4：正常一代，display 齐全且与词表同源
# --------------------------------------------------------------------------- #


class TestGenerationRead(_Fixture):
    """有一代就绪：格子、display、来源、待复核、依据原文逐项到位。"""

    def setUp(self):
        super().setUp()
        self.current = self.make_gen(
            cells=(
                (
                    self.alpha,
                    DOWNTREND,
                    {"state": sl.STATE_UNFIT, "reason": sl.REASON_INSUFFICIENT},
                ),
                (
                    self.beta,
                    RANGE,
                    {
                        "state": sl.STATE_NEUTRAL,
                        "reason": sl.REASON_ARCHETYPE_NOT_FIT,
                        "source": POOL_SOURCE_ARCHETYPE,
                        "evidence": {
                            "trades": 3,
                            "results": ["r1"],
                            "symbols": ["BTC/USDT"],
                        },
                        "needs_review": True,
                    },
                ),
            )
        )

    def test_every_cell_carries_value_and_display_for_every_enum(self):
        body = self.pool().json()

        self.assertTrue(body["has_generation"])
        self.assertEqual(body["generation"]["id"], self.current.pk)
        self.assertEqual(len(body["cells"]), 2)
        for cell in body["cells"]:
            with self.subTest(key=cell["key"]):
                for field in ("regime", "source", "state", "reason"):
                    self.assertEqual(set(cell[field]), {"value", "display"}, field)
                    self.assertTrue(cell[field]["display"], field)

    def test_the_displays_come_from_the_enums_themselves(self):
        """逐格比对**枚举自己给的**展示名，而不是用例里写的字面量。"""
        cells = {
            (cell["strategy"]["name"], cell["regime"]["value"]): cell
            for cell in self.pool().json()["cells"]
        }

        alpha = cells[("AlphaStem", DOWNTREND.value)]
        self.assertEqual(alpha["state"]["display"], sl.STATE_DISPLAY[sl.STATE_UNFIT])
        self.assertEqual(
            alpha["reason"]["display"], sl.REASON_DISPLAY[sl.REASON_INSUFFICIENT]
        )
        self.assertEqual(alpha["source"]["display"], "本策略切片")
        self.assertEqual(alpha["regime"]["display"], DOWNTREND.display)
        self.assertEqual(alpha["source"]["value"], POOL_SOURCE_STRATEGY)

    def test_a_reasonless_cell_has_an_empty_display_not_a_placeholder(self):
        """`fit` 那类格子本来就没有原因码。给空串配一句「（无）」会让前端把「没有原因」
        显示成一个像缺数据的占位符。"""
        generation = self.make_gen(
            cells=((self.gamma, RANGE, {"state": sl.STATE_FIT}),)
        )

        body = self.pool(generation=generation.pk).json()

        self.assertEqual(body["cells"][0]["reason"], {"value": "", "display": ""})

    def test_evidence_and_needs_review_are_passed_through_verbatim(self):
        """证据**逐字节原样**透出——只是不再每格一份副本（见 `TestPoolDoesNotRepeatEvidence`）。

        这条断言的字面内容与 2026-10-23 之前完全一样（`{"trades": 3, "results": ["r1"],
        "symbols": ["BTC/USDT"]}` 一个键都不少），变的是**从哪里取**：格子上给的是
        `evidence_key` + `results_key`，其余键在 `evidence` 表、`results` 在 `results` 表。
        把这条改成「断言一个子集」就会让「证据被悄悄裁剪」这件事再也测不出来。
        """
        body = self.pool().json()
        cells = {cell["strategy"]["name"]: cell for cell in body["cells"]}
        table = {item["key"]: item for item in body["evidence"]}
        results = {item["key"]: item["results"] for item in body["results"]}

        beta = cells["BetaStem"]
        self.assertTrue(beta["needs_review"])
        evidence = {k: v for k, v in table[beta["evidence_key"]].items() if k != "key"}
        evidence.pop("results_key", None)
        evidence["results"] = results[beta["results_key"]]
        self.assertEqual(
            evidence,
            {"trades": 3, "results": ["r1"], "symbols": ["BTC/USDT"]},
        )
        self.assertEqual(beta["source"]["value"], POOL_SOURCE_ARCHETYPE)
        self.assertEqual(beta["source"]["display"], "原型兜底")

    def test_the_matrix_columns_follow_the_backend_order(self):
        """列序由后端给（`quant.PRIORITY`），前端按数组排。"""
        body = self.pool().json()

        self.assertEqual(
            [row["value"] for row in body["regimes"]], [m.value for m in PRIORITY]
        )

    def test_cells_are_ordered_by_strategy_name_then_regime(self):
        body = self.pool().json()

        names = [cell["strategy"]["name"] for cell in body["cells"]]
        self.assertEqual(names, ["AlphaStem", "BetaStem"])

    def test_a_named_generation_reads_that_one_not_the_current(self):
        """`?generation=` 指定一代时读的是那一代，**不是当前代**——这是代际历史的读法。"""
        older = self.make_gen(
            cells=((self.alpha, RANGE, {"state": sl.STATE_FIT}),),
            finished_at=days(-5),
        )

        body = self.pool(generation=older.pk).json()

        self.assertEqual(body["generation"]["id"], older.pk)
        self.assertEqual(len(body["cells"]), 1)
        self.assertEqual(body["cells"][0]["regime"]["value"], RANGE.value)


# --------------------------------------------------------------------------- #
# 性质 4：display 与 `slice` 表同源（断言键集，不是断言字符串）
# --------------------------------------------------------------------------- #


class TestDisplayVocabularyIsSingleSourced(TestCase):
    """`state`/`reason` 的展示表住在 `apps/regime/slice.py`，serializers **只查表不复制**。

    断言的是**键集相等**，不是断言某个字符串。断言字符串等于把词表抄进用例：抄错了用例
    照样绿，而真正要防的「serializers 里复制了一份、那份漏了一个成员」一个字都挡不住。
    """

    def test_state_payload_keys_are_exactly_the_slice_table(self):
        """对**词表里的每一个键**取一次 display，断言它回的是表里的值——键集相同就是
        同源，多一个少一个都会在这里分叉。"""
        for value, display in sl.STATE_DISPLAY.items():
            with self.subTest(state=value):
                self.assertEqual(
                    ser.state_payload(value), {"value": value, "display": display}
                )

    def test_reason_payload_keys_are_exactly_the_slice_table(self):
        for value, display in sl.REASON_DISPLAY.items():
            with self.subTest(reason=value):
                self.assertEqual(
                    ser.reason_payload(value), {"value": value, "display": display}
                )

    def test_the_serializer_knows_no_value_the_slice_table_does_not(self):
        """反方向：serializers 不得给**词表之外**的取值编展示名。

        「键集相同」是一个双条件——上面两条钉的是 `词表 → payload` 这一向（表里每个键都
        被认出来，且认出来的是表里的那个字），这一条钉反向：表外的值必须原样回显。两向
        合起来才是「同一套词表」。

        取样的三类表外值都来自真实会漏进来的地方：`state` 的空串（`blank=True` 的默认
        值）、模型 `choices` 之外的手工脏值、以及一个明显不属于这张表的字。
        """
        for value in ("", "not_a_state", "fit ", "FIT"):
            with self.subTest(state=value):
                if value in sl.STATE_DISPLAY:
                    continue
                self.assertEqual(ser.state_payload(value)["display"], value)
        for value in ("not_a_reason", "INSUFFICIENT"):
            with self.subTest(reason=value):
                self.assertEqual(ser.reason_payload(value)["display"], value)

    def test_an_unknown_value_is_echoed_not_guessed(self):
        """手工写进去的脏行绕得过 `choices`。它不该让整张表炸掉，更不该被显示成某一档
        ——那会让人以为机制放行了某条策略。"""
        self.assertEqual(
            ser.state_payload("not_a_state"),
            {"value": "not_a_state", "display": "not_a_state"},
        )
        self.assertEqual(
            ser.source_payload("not_a_source"),
            {"value": "not_a_source", "display": "not_a_source"},
        )
        # 阶段走 `deactivation_run.regime_display`：它带一句「认不出的阶段」后缀，
        # 同样**不回落**成四档之一。
        self.assertEqual(
            ser.regime_payload("not_a_regime"),
            {"value": "not_a_regime", "display": "not_a_regime（认不出的阶段）"},
        )

    def test_mode_and_status_displays_come_from_their_enums(self):
        for mode in MechanismMode:
            with self.subTest(mode=mode.value):
                self.assertEqual(
                    ser.mode_payload(mode),
                    {"value": mode.value, "display": mode.display},
                )
        for status in RebuildStatus:
            with self.subTest(status=status.value):
                self.assertEqual(
                    ser.status_payload(status.value),
                    {"value": status.value, "display": status.display},
                )


# --------------------------------------------------------------------------- #
# 性质 4.4：同一份证据在响应里只出现一次
# --------------------------------------------------------------------------- #


class TestPoolDoesNotRepeatEvidence(_Fixture):
    """`GET api/regime/pool/` 的 `evidence` **按内容去重**，格子上引用它的键。

    **这是「读不出来」的根因，不是体积洁癖**。`pool._cell_for` 的原型兜底分支给每一格
    写的是「同原型全部策略的全部切片摘要」，而 `pool._summarise` 与阶段无关——于是同一
    份 `results` 在 4 个阶段里各抄一遍，又因为兜底按原型分桶，同一份还会被同桶的每一条
    策略各抄一遍。实测（2026-10-23，244 格 / 全库 4067 条回测的合成库）：

        results 条目 732,700 条，去重后 4,067 条 —— **重复率 99.4%**
        244 格里只有 **2 种**不同的 evidence，最高频那份出现 **180 次**

    87 MB 的响应里 99.9% 是这同一批字节的第 N 份副本。压缩（`GZipMiddleware`）能把它
    压到 4.1 MB，但那只是让传输好看：浏览器仍要 `JSON.parse` 出 90 MB 的对象，服务端仍
    要为每一份副本做一次序列化。真正的修法是**只发一份**。

    ## 形状

    两级去重，两个顶层表，格子上各带一个键：

        {"evidence": [{"key": "<ek>", "results_key": "<rk>", ...其余键}, ...],
         "results":  [{"key": "<rk>", "results": [...]}, ...],
         "cells":    [{"key": "7|range", "evidence_key": "<ek>", "results_key": "<rk>"}, ...]}

    `evidence` 的每一项就是**原来那份 `evidence` 原文**（`pool._evidence` 的产物，一个
    键都不改、一个键都不少）——所以「依据哪几个回测、多少笔、什么区间」这条审计要求
    没有被削弱，只是从「每格一份副本」变成「整份响应一份」。

    两级是必需的，不是过度设计：整份 evidence 因 `metrics` / `own_sample` 按阶段算而
    **各不相同**（实测 248 格去出 243 种），所以只做第一级等于没做；重复全在第二级。
    """

    def _resolved(self, body: dict, cell: dict) -> dict:
        """把响应拼回「一格完整的 evidence」，与前端 `evidenceOf` 同一套拼法。

        断言走这个辅助方法而不是直接读表：**拼回来必须与去重前的原文逐字节相同**，
        而这条只有真的拼一次才测得到。
        """
        table = {item["key"]: item for item in body["evidence"]}
        results = {item["key"]: item["results"] for item in body["results"]}
        entry = dict(table[cell["evidence_key"]])
        # 两个键都是**传输层的**，不属于证据原文；拼回来要剔除。
        entry.pop("key", None)
        entry.pop("results_key", None)
        if cell["results_key"]:
            entry["results"] = results.get(cell["results_key"], [])
        return entry

    def _evidence(self, tag: str, **extra) -> dict:
        """一份证据。`extra` 用来让**整体**各不相同（生产里 metrics / own_sample 每格都不同），
        而 `results` 仍然可以是同一批——重复的正是 `results`。"""
        return {
            "trades": 3,
            "results": [f"r-{tag}"],
            "symbols": ["BTC/USDT"],
            **extra,
        }

    def test_the_same_evidence_is_sent_once_and_referenced_by_key(self):
        shared = self._evidence("shared")
        generation = self.make_gen(
            cells=(
                (self.alpha, DOWNTREND, {"evidence": shared}),
                (self.alpha, RANGE, {"evidence": shared}),
                (self.beta, DOWNTREND, {"evidence": self._evidence("beta")}),
            )
        )

        body = self.pool(generation=generation.pk).json()

        self.assertEqual(len(body["cells"]), 3)

        # 拼回来就是原来那份原文（逐键相等，不是「够用的子集」）。
        alpha_down, alpha_range, beta_down = body["cells"]
        self.assertEqual(self._resolved(body, alpha_down), shared)
        self.assertEqual(self._resolved(body, beta_down), self._evidence("beta"))

        # 用了同一份证据的两格，两个键都一样；第三格两个键都不同。
        self.assertEqual(alpha_down["evidence_key"], alpha_range["evidence_key"])
        self.assertEqual(alpha_down["results_key"], alpha_range["results_key"])
        self.assertNotEqual(alpha_down["results_key"], beta_down["results_key"])

        # 重复的部分真的只发了一份：两种 `results` ⇒ results 表两项，
        # 而「去掉 results 之后的部分」三格完全相同 ⇒ evidence 表只有一项。
        self.assertEqual(len(body["results"]), 2)
        self.assertEqual(len(body["evidence"]), 1)

    def test_a_shared_results_list_is_sent_once_even_when_each_evidence_differs(self):
        """**生产里的真实形状**（2026-10-23 在 txy 上实测）。

        每格的 `metrics` / `full_sample` / `own_sample` 各不相同（它们按阶段算），所以
        **整份 evidence 去重几乎无效**——实测 248 格去了 243 种，等于没去。真正重复的是
        里面那份 `results`：它按原型分桶生成、与阶段无关，248 格里只有 **19 种**。

            整份去重后 243 种 ⇒ 59.0 MB（等于没省）
            仅 results 去重后 19 种 ⇒ 1.6 MB（省 35 倍）

        所以去重必须**下沉到 `results` 这一层**。只对整份 dict 做哈希，是把「99% 的字节是
        同一批 `results` 的副本」这件事整个漏掉——本用例专门钉住它，因为夹具里只要偷懒写成
        「整份相同」，那条原来就绿、而线上依然 55 MB。
        """
        generation = self.make_gen(
            cells=tuple(
                (
                    self.alpha,
                    regime,
                    {
                        "evidence": self._evidence(
                            "shared", metrics={"sharpe": i}, own_sample={"i": i}
                        )
                    },
                )
                for i, regime in enumerate((DOWNTREND, RANGE))
            )
        )

        body = self.pool(generation=generation.pk).json()

        # 整份 evidence 不同（metrics 不同）⇒ 证据表两项……
        self.assertEqual(len(body["evidence"]), 2)
        # ……但它们引用的 `results` 是同一份，只发一次。
        self.assertEqual(len(body["results"]), 1, body.get("results"))
        first, second = body["cells"]
        self.assertNotEqual(first["evidence_key"], second["evidence_key"])
        self.assertEqual(first["results_key"], second["results_key"])

        # 前端拼回来仍是逐字节相同的原文。
        resolved = self._resolved(body, first)
        self.assertEqual(resolved["results"], ["r-shared"])
        self.assertEqual(resolved["metrics"], {"sharpe": 0})

    def test_the_cell_no_longer_carries_its_own_copy(self):
        """格子上**不再有** `evidence` 内联：留着它，去重就只是「多给一份」。"""
        generation = self.make_gen(
            cells=((self.alpha, DOWNTREND, {"evidence": self._evidence("a")}),)
        )

        cell = self.pool(generation=generation.pk).json()["cells"][0]

        self.assertNotIn("evidence", cell)
        self.assertIn("evidence_key", cell)
        self.assertIn("results_key", cell)

    def test_cells_without_evidence_share_one_empty_entry(self):
        """没有证据的格子（`evidence={}`）**共用一个空项**，不是各自一个 `{}`。
        冷启动与「一格都没有」时，重复的空对象同样是 N 份副本。"""
        generation = self.make_gen(
            cells=(
                (self.alpha, DOWNTREND, {"evidence": {}}),
                (self.beta, RANGE, {"evidence": {}}),
            )
        )

        body = self.pool(generation=generation.pk).json()

        self.assertEqual(len(body["evidence"]), 1)
        self.assertEqual(
            body["cells"][0]["evidence_key"], body["cells"][1]["evidence_key"]
        )
        # 空证据没有 `results` 可言：那一项只有 key + 一个空的 results_key，
        # 且**不为它建 results 表项**（建了就是拿空数组占位）。
        self.assertIsNone(body["evidence"][0].get("results"))
        self.assertEqual(body["evidence"][0]["results_key"], "")
        self.assertEqual(body["results"], [])
        # 拼回来仍是空 dict，与库里那格一致。
        self.assertEqual(self._resolved(body, body["cells"][0]), {})


# --------------------------------------------------------------------------- #
# 性质 4.6：依据可以按需拉（`?evidence=0` + `pool/cell/`）
# --------------------------------------------------------------------------- #


class TestPoolEvidenceIsLazy(_Fixture):
    """列表只发矩阵，一格的依据由 `pool/cell/` 按需拉。

    **这是「读不出来」的第二层根因**（2026-10-23，在 txy 上实测后补的）。两级去重把
    整份响应从 56.7 MB 降到 1.7 MB，但在一条实测约 2 KB/s、20% 丢包的链路上，1.7 MB
    （gzip 后 278 KB）仍要 **50 秒**——页面上就是一直转圈。而矩阵本身（248 格的元数据）
    只有几十 KB，人一次也只会展开一两格。

    所以 `pool/?evidence=0` 不发两张依据表，`pool/cell/` 按格给。两者给的**是同一份
    东西**——这一点由最后一条用例钉住：同一条策略同一阶段，从两条路径取到的 evidence
    必须逐字节相同，否则「审计看到的」会随「从哪个入口点开的」而变。
    """

    def _cell(self, **params):
        return self.client.get(POOL_CELL, params)

    def test_evidence_zero_omits_both_tables_and_keeps_the_matrix(self):
        generation = self.make_gen(
            cells=(
                (self.alpha, DOWNTREND, {"evidence": self.shared_evidence()}),
                (self.beta, RANGE, {"evidence": self.shared_evidence()}),
            )
        )

        body = self.pool(generation=generation.pk, evidence=0).json()

        # 矩阵照旧：两格都在，枚举与策略信息一个不少。
        self.assertEqual(len(body["cells"]), 2)
        self.assertEqual(
            sorted(cell["regime"]["value"] for cell in body["cells"]),
            sorted([DOWNTREND.value, RANGE.value]),
        )
        for cell in body["cells"]:
            self.assertTrue(cell["strategy"]["name"])
            self.assertEqual(set(cell["state"]), {"value", "display"})
        # 两张依据表都不发。
        self.assertEqual(body["evidence"], [])
        self.assertEqual(body["results"], [])

    def test_the_default_still_ships_the_evidence(self):
        """不传 `evidence` 时默认**发**：别的调用方（脚本、导出）不该被界面优化改掉。"""
        generation = self.make_gen(
            cells=((self.alpha, DOWNTREND, {"evidence": self.shared_evidence()}),)
        )

        body = self.pool(generation=generation.pk).json()

        self.assertEqual(len(body["evidence"]), 1)
        self.assertEqual(len(body["results"]), 1)

    def test_the_cell_endpoint_returns_the_resolved_evidence(self):
        evidence = self.shared_evidence()
        generation = self.make_gen(
            cells=((self.alpha, DOWNTREND, {"evidence": evidence}),)
        )

        resp = self._cell(
            generation=generation.pk, strategy=self.alpha.pk, regime=DOWNTREND.value
        )

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["key"], f"{self.alpha.pk}|{DOWNTREND.value}")
        self.assertEqual(body["evidence"], evidence)

    def test_the_two_paths_agree_byte_for_byte(self):
        """**这条是这一组的核心**：列表里带的与按格拉的必须是同一份。

        两条路径各写一遍拼装（一条走 `evidence_dedupe` 的键、一条直接从行里取）时，
        最容易出的错就是某一处「顺手整理了一下」——而审计看到的依据会因此随入口而变。
        """
        evidence = self.shared_evidence()
        generation = self.make_gen(
            cells=((self.alpha, DOWNTREND, {"evidence": evidence}),)
        )

        listed = self.pool(generation=generation.pk).json()
        cell = listed["cells"][0]
        table = {item["key"]: item for item in listed["evidence"]}
        results = {item["key"]: item["results"] for item in listed["results"]}
        from_list = {k: v for k, v in table[cell["evidence_key"]].items() if k != "key"}
        from_list.pop("results_key", None)
        from_list["results"] = results[cell["results_key"]]

        from_cell = self._cell(
            generation=generation.pk, strategy=self.alpha.pk, regime=DOWNTREND.value
        ).json()["evidence"]

        self.assertEqual(from_list, from_cell)

    def test_an_unknown_regime_is_a_400_and_a_missing_cell_is_a_404(self):
        generation = self.make_gen(cells=((self.alpha, DOWNTREND, {"evidence": {}}),))

        bad = self._cell(
            generation=generation.pk, strategy=self.alpha.pk, regime="not_a_regime"
        )
        missing = self._cell(
            generation=generation.pk, strategy=self.beta.pk, regime=RANGE.value
        )

        # 阶段拼错 → 请求拼错（400）；这一格不存在 → 404。两者不混。
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(missing.status_code, 404)

    def test_the_required_params_are_required(self):
        generation = self.make_gen(cells=((self.alpha, DOWNTREND, {"evidence": {}}),))

        self.assertEqual(self._cell(generation=generation.pk).status_code, 400)
        self.assertEqual(
            self._cell(generation=generation.pk, strategy=self.alpha.pk).status_code,
            400,
        )

    def test_authentication_is_required(self):
        # 用一个**全新的** client，而不是 `force_authenticate(user=None)`：后者会把请求
        # 交回默认鉴权链，而本仓没有装 `django.contrib.sessions`，于是它炸在 Session 上
        # 而不是回 401——测的就不是「要不要登录」了。
        anonymous = APIClient()

        resp = anonymous.get(
            POOL_CELL, {"strategy": self.alpha.pk, "regime": DOWNTREND.value}
        )

        self.assertEqual(resp.status_code, 401)


# --------------------------------------------------------------------------- #
# 性质 4.5：池化表的响应体在传输层是压缩的
# --------------------------------------------------------------------------- #


class TestPoolResponseIsCompressed(_Fixture):
    """`GET api/regime/pool/` 必须走 gzip。

    **这条守的不是性能，是「读得出来」**。`pool` 的每一格都带 `evidence`，而
    `evidence.results` 是「同原型全部策略的全部切片摘要」——它与阶段无关，所以同一份
    在 4 个阶段里各抄一遍（`pool._cell_for` 的兜底分支 + `pool._evidence`）。实测
    （2026-10-23，244 格、全库 4067 条回测的合成库）：**未压缩 86.9 MB**，浏览器要把
    87 MB 收完、再 `JSON.parse` 出 90 MB 的对象，然后才画第一格——在普通带宽上就是
    「读不出来」。gzip 后 4.1 MB（4.7%）。

    所以这条断言**不是**「体积小于 N 字节」（那会把「证据有多少」变成一条随时该红的
    用例），而是「请求声明了 `Accept-Encoding: gzip` 时，响应头里确实有
    `Content-Encoding: gzip`」——`core/settings/base.py` 的 `MIDDLEWARE` 里少掉那一行，
    这条当场变红，而其它 1700 多条用例一条都不会响。

    `gzip` 解回来必须与不压缩时逐字节相同：中间件装错位置（比如装在 CORS 里面导致
    预检失败）或压了不该压的（流式响应）时，页面上看到的是「表格一片空白」而不是
    一个错误。
    """

    def setUp(self):
        super().setUp()
        self.make_gen(cells=((self.alpha, DOWNTREND, {"state": sl.STATE_FIT}),))

    def test_the_response_is_gzipped_when_the_client_asks_for_it(self):
        plain = self.client.get(POOL)
        packed = self.client.get(POOL, HTTP_ACCEPT_ENCODING="gzip")

        self.assertEqual(plain.status_code, 200)
        self.assertEqual(packed.status_code, 200)
        self.assertEqual(packed.headers.get("Content-Encoding"), "gzip")
        # 解回来必须与不压缩时是同一份 JSON（压缩是无损的，不是另一套序列化）。
        self.assertEqual(gzip.decompress(packed.content), plain.content)

    def test_a_client_that_does_not_ask_still_gets_plain_json(self):
        """不声明 `Accept-Encoding` 的客户端（curl 默认、服务端到服务端调用）不许收到
        gzip 体——它们不会解，收到的是一堆乱码。"""
        plain = self.client.get(POOL)

        self.assertEqual(plain.status_code, 200)
        self.assertIsNone(plain.headers.get("Content-Encoding"))
        self.assertIsInstance(plain.json(), dict)


# --------------------------------------------------------------------------- #
# 性质 5：overview 是廉价只读
# --------------------------------------------------------------------------- #


class TestOverviewStaysCheap(_Fixture):
    """`overview` **不许**调三张确认页。

    它们不是「慢一点」而是「每 60 秒重导入一次全部策略模块 / 全量重算所有历史标签」：
    `gate_switch.page()` 经 `managed_set()` 调 `StrategyRegistry.discover()`，
    `mechanism_switch.page()` 经 `truth_run.agreement()` 调 `slicing.load_tags()`。
    首屏要的是「现在是什么状态」，那三张页面只在人要做决定时才需要。
    """

    def setUp(self):
        super().setUp()
        self.judged()
        self.switch_row(MechanismKind.REGIME_GATE, MechanismMode.EXECUTING)
        self.make_gen(cells=((self.alpha, DOWNTREND, {"state": sl.STATE_UNFIT}),))

    def test_the_query_count_has_a_hard_ceiling(self):
        """实测值就是 `OVERVIEW_QUERY_BUDGET`：三个开关各一次 + 当前代一次 + 生效判定一次。

        退化成调 `page()` 的实现必然要多读 `RegimeJudgement` / `ShadowDailyRecord` /
        `MajorEvent` 好几张表，这个数当场就守不住。
        """
        with self.assertNumQueries(views.OVERVIEW_QUERY_BUDGET):
            resp = self.overview()
        self.assertEqual(resp.status_code, 200)

    def test_none_of_the_three_confirmation_pages_is_called(self):
        """查询数是那把尺子；这条是尺子量不到的那一半——一个 `page()` 调用可能只多一次
        查询，也可能一次都不多（缓存），但它带来的进程级副作用一次就够。"""
        with (
            patch.object(mechanism_switch, "page") as mech,
            patch.object(breaker_switch, "page") as breaker,
            patch.object(gate_switch, "page") as gate,
        ):
            resp = self.overview()

        self.assertEqual(resp.status_code, 200)
        mech.assert_not_called()
        breaker.assert_not_called()
        gate.assert_not_called()

    def test_the_switch_card_carries_the_latest_flip(self):
        body = self.overview().json()

        gate = next(
            row
            for row in body["switches"]
            if row["kind"]["value"] == MechanismKind.REGIME_GATE.value
        )
        self.assertEqual(gate["mode"]["value"], MechanismMode.EXECUTING.value)
        self.assertEqual(gate["mode"]["display"], MechanismMode.EXECUTING.display)
        self.assertFalse(gate["never_switched"])
        self.assertEqual(gate["latest"]["actor_name"], "ops")
        self.assertEqual(
            gate["latest"]["to_mode"]["display"], MechanismMode.EXECUTING.display
        )
        self.assertEqual(gate["latest"]["reason"], "测试")
        self.assertTrue(gate["latest"]["at"])

    def test_the_switch_mode_agrees_with_the_model_helper(self):
        """`switch_payload` 的取值规则与 `RegimeMechanismSwitch.current()` 是同一条
        （最后一条流水的 `to_mode`，没有流水即 Shadow）。规则在仓里出现了两处，所以这里
        逐档对账——分叉会在这一条变红，而不是在页面上变成一个看不出错的档位。"""
        for kind in views.SWITCH_KINDS:
            with self.subTest(kind=kind.value):
                payload = ser.switch_payload(kind, RegimeMechanismSwitch.latest(kind))
                self.assertEqual(
                    payload["mode"]["value"],
                    RegimeMechanismSwitch.current(kind).value,
                )

    def test_the_pool_summary_matches_the_generation_record(self):
        body = self.overview().json()

        self.assertTrue(body["pool"]["has_generation"])
        summary = body["pool"]["generation"]
        self.assertEqual(summary["status"]["value"], RebuildStatus.READY.value)
        self.assertEqual(summary["status"]["display"], RebuildStatus.READY.display)
        self.assertEqual(summary["cells_total"], 1)
        self.assertEqual(summary["pool_version"], pool.POOL_VERSION)

    def test_the_current_regime_state_is_not_cold_once_a_judgement_landed(self):
        body = self.overview().json()

        state = body["regime_state"]
        self.assertFalse(state["cold"])
        self.assertEqual(state["regime"]["value"], DOWNTREND.value)
        self.assertEqual(state["regime"]["display"], DOWNTREND.display)
        self.assertTrue(state["effective_at"])


# --------------------------------------------------------------------------- #
# 性质 6：代际分页与差异
# --------------------------------------------------------------------------- #


class TestGenerations(_Fixture):
    """「这一代谁何时算的」+「与上一代差在哪」。"""

    def setUp(self):
        super().setUp()
        # 三代：最早一代两格、中间一代改了其中一格、最新一代一格消失。
        self.first = self.make_gen(
            cells=(
                (
                    self.alpha,
                    DOWNTREND,
                    {"state": sl.STATE_UNFIT, "reason": sl.REASON_INSUFFICIENT},
                ),
                (self.beta, RANGE, {"state": sl.STATE_FIT}),
            ),
            finished_at=days(-9),
            started_at=days(-9),
        )
        self.second = self.make_gen(
            cells=(
                (
                    self.alpha,
                    DOWNTREND,
                    {"state": sl.STATE_NEUTRAL, "reason": sl.REASON_INCOMPARABLE},
                ),
                (self.beta, RANGE, {"state": sl.STATE_FIT}),
            ),
            finished_at=days(-5),
            started_at=days(-5),
        )
        self.third = self.make_gen(
            cells=(
                (
                    self.alpha,
                    DOWNTREND,
                    {"state": sl.STATE_NEUTRAL, "reason": sl.REASON_INCOMPARABLE},
                ),
            ),
            finished_at=days(-1),
            started_at=days(-1),
        )

    def test_every_row_carries_status_actor_times_and_the_three_counts(self):
        body = self.generations().json()

        self.assertEqual(body["count"], 3)
        self.assertEqual(
            [row["id"] for row in body["results"]],
            [self.third.pk, self.second.pk, self.first.pk],
        )
        for row in body["results"]:
            with self.subTest(generation=row["id"]):
                self.assertEqual(set(row["status"]), {"value", "display"})
                self.assertEqual(set(row["actor_kind"]), {"value", "display"})
                self.assertTrue(row["started_at"])
                for key in (
                    "candidates",
                    "results_used",
                    "cells_total",
                    "cells_changed",
                ):
                    self.assertIsInstance(row[key], int, key)
                self.assertIn("exclusions", row)
                self.assertIn("failure", row)

    def test_pagination_advances_and_loses_no_rows(self):
        seen: list[int] = []
        page = 1
        while True:
            body = self.generations(page=page, page_size=2).json()
            seen.extend(row["id"] for row in body["results"])
            if not body["has_next"]:
                break
            page += 1

        self.assertEqual(seen, [self.third.pk, self.second.pk, self.first.pk])
        self.assertEqual(body["page_size"], 2)

    def test_the_diff_splits_changed_added_and_removed(self):
        body = self.generations(generation=self.third.pk).json()

        diff = body["diff"]
        self.assertEqual(diff["generation"], self.third.pk)
        self.assertEqual(diff["previous_generation"], self.second.pk)
        self.assertEqual(diff["counts"], {"changed": 0, "added": 0, "removed": 1})
        removed = diff["cells"][0]
        self.assertEqual(removed["kind"], "removed")
        self.assertEqual(removed["strategy"]["name"], "BetaStem")
        self.assertIsNone(removed["after"])
        self.assertEqual(
            removed["before"]["state"]["display"], sl.STATE_DISPLAY[sl.STATE_FIT]
        )

    def test_the_diff_of_a_changed_cell_shows_both_sides_with_displays(self):
        body = self.generations(generation=self.second.pk).json()

        diff = body["diff"]
        self.assertEqual(diff["previous_generation"], self.first.pk)
        self.assertEqual(diff["counts"], {"changed": 1, "added": 0, "removed": 0})
        row = diff["cells"][0]
        self.assertEqual(row["kind"], "changed")
        self.assertEqual(
            row["before"]["state"]["display"], sl.STATE_DISPLAY[sl.STATE_UNFIT]
        )
        self.assertEqual(
            row["after"]["state"]["display"], sl.STATE_DISPLAY[sl.STATE_NEUTRAL]
        )
        self.assertEqual(
            row["after"]["reason"]["display"], sl.REASON_DISPLAY[sl.REASON_INCOMPARABLE]
        )

    def test_the_diff_total_equals_the_recorded_cells_changed(self):
        """`changed + added + removed` 必须等于记录里那一刻算出来的 `cells_changed`。

        两处口径分叉时（比如这里拿证据里的数字当差异），页面上说「变了 3 格」而列表里
        有 5 条，谁都不会报错。`pool_rebuild._cell_changes` 比的是同一个三元组。
        """
        for generation in (self.first, self.second, self.third):
            with self.subTest(generation=generation.pk):
                # 记录里的 `cells_changed` 由 `rebuild_pool` 写；夹具绕过它造代，所以这里
                # 用**差异端点算出的合计**去和「同一口径直接算一遍」对账。
                expected = _recompute_changes(generation)
                body = self.generations(generation=generation.pk).json()
                diff = body["diff"]
                self.assertEqual(sum(diff["counts"].values()), expected)

    def test_the_first_generation_has_no_previous(self):
        """首次重建没有上一代：`previous_generation` 是 `null`，不是 0、不是它自己。

        此时它的每一格都是 `added`——从「什么都没有」变成一整代结论，那确实每一格都是
        新增。**与 `pool_rebuild._cell_changes` 同口径**：那边把首次重建整代计入变化，
        这边把它计入 `added`（差异里 `changed + added + removed` 就是那个数）。
        """
        body = self.generations(generation=self.first.pk).json()

        self.assertIsNone(body["diff"]["previous_generation"])
        self.assertEqual(
            body["diff"]["counts"], {"changed": 0, "added": 2, "removed": 0}
        )
        self.assertTrue(all(row["before"] is None for row in body["diff"]["cells"]))

    def test_a_building_generation_is_listed_and_has_cells(self):
        """`building` / `failed` 的残骸也有格子可看——那是「为什么会这样」的一半答案
        （`RebuildStatus` 的 docstring：残骸是记录不是垃圾）。"""
        stuck = self.make_gen(
            status=RebuildStatus.BUILDING.value,
            finished_at=None,
            started_at=days(-1),
            cells=((self.gamma, RANGE, {"state": sl.STATE_UNKNOWN}),),
        )

        body = self.generations().json()
        row = next(row for row in body["results"] if row["id"] == stuck.pk)
        self.assertEqual(row["status"]["display"], RebuildStatus.BUILDING.display)
        self.assertIsNone(row["finished_at"])

        cells = self.pool(generation=stuck.pk).json()["cells"]
        self.assertEqual(len(cells), 1)
        self.assertEqual(
            cells[0]["state"]["display"], sl.STATE_DISPLAY[sl.STATE_UNKNOWN]
        )

    def test_a_failed_generation_carries_its_failure_text(self):
        failed = self.make_gen(
            status=RebuildStatus.FAILED.value,
            finished_at=days(-2),
            started_at=days(-2),
        )
        RegimePoolRebuild.objects.filter(pk=failed.pk).update(
            failure="池化失败：样本读不出来"
        )
        failed.refresh_from_db()

        body = self.generations().json()
        row = next(row for row in body["results"] if row["id"] == failed.pk)
        self.assertEqual(row["failure"], "池化失败：样本读不出来")


def _recompute_changes(generation: RegimePoolRebuild) -> int:
    """按 `pool_rebuild._cell_changes` 的口径**独立**算一遍，作为差异端点的对账基准。

    这里**故意不调 `views._previous_generation` / `views._triples`**：拿被测算法的两个
    半成品去拼基准，等于让基准跟着实现一起漂——两边同时错的时候它照样绿。所以直接查
    ORM、直接做集合运算，只共享「比三元组」这个**语义**（那是 `pool_rebuild._cell_changes`
    的 docstring 定下的），不共享一行代码。

    **也不拿 `generation.cells_changed` 当基准**：夹具绕过了 `rebuild_pool`，那一列是
    默认值 0。
    """
    anchor = generation.finished_at or generation.started_at
    previous = (
        RegimePoolRebuild.objects.filter(status="ready", finished_at__lt=anchor)
        .exclude(pk=generation.pk)
        .order_by("-finished_at", "-id")
        .first()
    )

    def triples(row):
        if row is None:
            return {}
        return {
            (sid, regime): (state, reason, source)
            for sid, regime, state, reason, source in RegimePoolCell.objects.filter(
                rebuild=row
            ).values_list("strategy_id", "regime", "state", "reason", "source")
        }

    before, after = triples(previous), triples(generation)
    return sum(
        1 for key in set(before) | set(after) if before.get(key) != after.get(key)
    )


# --------------------------------------------------------------------------- #
# 性质 7：`generation` 显式配对
# --------------------------------------------------------------------------- #


class TestPairingIsPinnedInsideOneRequest(_Fixture):
    """`pool` 端点把同一个代对象既用于取 id 又用于取格子。

    不显式传的话 `current_cells()` 会自己再问一次当前代，两次问之间翻了一代就是「依据
    写的是第 N 代，引用的却是第 N+1 代」（`current_cells` 的 docstring 说的正是这件事）。
    这条用「取格子的那一刻把当前代换掉」来钉：返回的 `generation.id` 必须仍是请求开头
    取到的那一代。
    """

    def test_the_cells_and_the_id_come_from_the_same_generation(self):
        """请求开头取到的那一代，就是取格子用的那一代。

        造法：库里真实的「当前代」是 `newer`（完成时刻更晚），但请求开头那一刻的当前代
        是 `asked`——用 patch 把 `current_generation()` 钉成 `asked` 来模拟「请求进行中
        翻了一代」。端点若把 `None` 递给 `current_cells()`，它自己会再问一次当前代、
        读到 `newer`，于是 id 说第 1 代、格子却是第 2 代的——那就是这条要挡的形状。
        """
        asked = self.make_gen(
            cells=((self.alpha, DOWNTREND, {"state": sl.STATE_UNFIT}),)
        )
        newer = self.make_gen(
            cells=((self.beta, RANGE, {"state": sl.STATE_FIT}),),
            finished_at=days(1),
        )

        seen: list = []
        original = pool_rebuild.current_cells

        def record(generation=None):
            seen.append(generation)
            return original(generation)

        with (
            patch.object(pool_rebuild, "current_generation", return_value=asked),
            patch.object(pool_rebuild, "current_cells", side_effect=record),
        ):
            body = self.pool().json()

        # 递进去的是**代对象本身**，不是 `None`（`None` 会触发第二次「当前代」查询）。
        self.assertEqual(seen, [asked])
        self.assertEqual(body["generation"]["id"], asked.pk)
        self.assertEqual(
            [cell["strategy"]["name"] for cell in body["cells"]], ["AlphaStem"]
        )
        self.assertNotEqual(body["generation"]["id"], newer.pk)


# --------------------------------------------------------------------------- #
# 参数错误与资源不存在
# --------------------------------------------------------------------------- #


class TestBadParameters(_Fixture):
    """参数错 → 400；资源不存在 → 404。**两者不混**：前者是请求拼错，后者是「你问的那
    一代不在了」，处置完全不同（改请求 vs 刷新页面）。"""

    def test_a_non_integer_generation_is_a_400_not_a_500(self):
        for url in (POOL, GENERATIONS):
            with self.subTest(url=url):
                resp = self.client.get(url, {"generation": "abc"})
                self.assertEqual(resp.status_code, 400, resp.content)
                self.assertIn("error", resp.json())

    def test_an_unknown_generation_is_a_404(self):
        for url in (POOL, GENERATIONS):
            with self.subTest(url=url):
                resp = self.client.get(url, {"generation": 999999})
                self.assertEqual(resp.status_code, 404, resp.content)
                self.assertIn("error", resp.json())

    def test_page_bounds_are_rejected_with_an_error_shape(self):
        for params in (
            {"page": 0},
            {"page_size": 0},
            {"page_size": views.MAX_PAGE_SIZE + 1},
        ):
            with self.subTest(params=params):
                resp = self.generations(**params)
                self.assertEqual(resp.status_code, 400, resp.content)
                self.assertIn("error", resp.json())

    def test_an_empty_generation_param_means_current(self):
        """`?generation=`（空值）是「没传」，不是「找 id 为空的那一代」。表单与前端常常
        会带一个空串。"""
        current = self.make_gen(
            cells=((self.alpha, DOWNTREND, {"state": sl.STATE_FIT}),)
        )

        body = self.pool(generation="").json()

        self.assertEqual(body["generation"]["id"], current.pk)
