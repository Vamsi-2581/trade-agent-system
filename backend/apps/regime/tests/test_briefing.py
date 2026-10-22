"""三张确认页的按需端点（P5 子票 #48，map #40）。

`test_views.py` 钉的是三个**廉价只读**端点；这个文件钉的是它们的反面：那个**重请求**。
三张确认页各自有自己的测试（`test_gate_switch` / `test_breaker_switch` /
`test_mechanism_switch` 钉的是「页面上算出来的数对不对」），这里一行都不断言那些数——
那会是第二处真相。这里只钉**传输**这一层：

六条性质，每一条坏了都不报警、只出错：

1. **正文与审计串同源**：`body` 与 `summary` 出自**同一次** `page()` 求值。这一条是
   `mechanism_switch.page()` docstring 钉成不变量的事（「确认页上写的数与人点头之后落进
   `reason` 的数必须是同一次快照」），而它是唯一一条**没有报错路径**的错法：各调一次会让
   两个数都出自同一个模块、却互相矛盾。所以这里既数调用次数，也断言两段文字确实来自
   那一次返回的对象。
2. **三个 kind 都通，未知 kind 是 400 不是 404**：它是枚举取值错（请求拼错了），不是
   「这一页不存在」。两者要求人做的动作完全不同。
3. **冷启动不 500，而且三张页面自己说的话不许被这里替掉**：无代、无判定、空事件库都要
   照常返回 body。替它们编一句就是给冷启动造第二个说法。
4. **认证必需**：读对所有登录用户开放（`CONTEXT.md` 的「读对所有登录用户，写要
   `is_staff`」），所以这里不造 staff 用户。
5. **方向由当前档位推出来**：当前 Shadow ⇒ 这一页对应「出 Shadow」，摘要取
   `page().summary`；当前执行态 ⇒ 对应「退回 Shadow」，摘要取 `closing_summary(data)`，
   而 gate 的正文换成关闭那一套（它是三个模块里唯一有第二个方向正文的）。
6. **`kind` → 模块的映射在一张表里，且键集就是 `MechanismKind`**：加了第四个开关而忘了
   登记时，这条会红；而不是在页面上变成一个 400。

夹具：策略那一套直接复用 `test_gate_run._Fixture`（三个策略 + 一个用户 + 注册表替身）。
三个开关的世界各造各的，因为「同一个页面在两个文件里算出两个数」正是要防的事。
"""

from __future__ import annotations

import dataclasses
from unittest.mock import patch

from django.test import TestCase
from rest_framework.test import APIClient

from apps.regime import (
    briefing as briefing_module,
    breaker_switch,
    gate_switch,
    mechanism_switch,
)
from apps.regime.models import MechanismKind, MechanismMode
from apps.regime.tests.test_views import _Fixture as _Client
from apps.regime.tests.test_gate_run import NOW, days
from apps.regime.tests.test_gate_switch import (
    CLOSING_TITLE,
    PAGE_TITLE,
    _Fixture as _GateFixture,
)


def url(kind: str) -> str:
    return f"/api/regime/briefing/{kind}/"


# --------------------------------------------------------------------------- #
# 性质 4：认证必需
# --------------------------------------------------------------------------- #


class TestAuthRequired(TestCase):
    def test_all_three_kinds_reject_anonymous(self):
        anonymous = APIClient()
        for kind in MechanismKind:
            with self.subTest(kind=kind.value):
                resp = anonymous.get(url(kind.value))
                self.assertEqual(resp.status_code, 401, resp.content)


# --------------------------------------------------------------------------- #
# 性质 6：kind → 模块的映射
# --------------------------------------------------------------------------- #


class TestTheKindTable(TestCase):
    def test_every_mechanism_kind_is_registered(self):
        """**键集相等**，不是「三个都在」。给 `MechanismKind` 加第四个取值而忘了登记时，
        这条要红——否则那个开关的页面是一个 400。"""
        self.assertEqual(set(briefing_module.BRIEFING_SOURCES), set(MechanismKind))

    def test_only_the_gate_accepts_a_direction(self):
        """三个模块的形状差异只有这一处。把它写成显式字段而不是 `except TypeError`：
        后者会把「参数拼错了」吞成「这个模块不支持方向」，两声是同一种沉默。"""
        accepts = {
            kind
            for kind, source in briefing_module.BRIEFING_SOURCES.items()
            if source.page_accepts_direction
        }
        self.assertEqual(accepts, {MechanismKind.REGIME_GATE})

    def test_opposite_is_an_involution(self):
        for mode in MechanismMode:
            with self.subTest(mode=mode.value):
                self.assertIsNot(briefing_module.opposite(mode), mode)
                self.assertIs(
                    briefing_module.opposite(briefing_module.opposite(mode)), mode
                )


# --------------------------------------------------------------------------- #
# 性质 2：三个 kind 都通；未知 kind 是 400
# --------------------------------------------------------------------------- #


class TestAllThreeKindsAnswer(_Client):
    def test_each_kind_returns_a_non_empty_body_and_summary(self):
        for kind in MechanismKind:
            with self.subTest(kind=kind.value):
                resp = self.client.get(url(kind.value))
                self.assertEqual(resp.status_code, 200, resp.content)
                body = resp.json()
                self.assertTrue(body["body"].strip(), "body 不能是空串")
                self.assertTrue(body["summary"].strip(), "summary 不能是空串")
                self.assertEqual(body["kind"]["value"], kind.value)
                self.assertEqual(body["kind"]["display"], kind.display)
                self.assertTrue(body["heavy"])

    def test_the_payload_shape_is_the_same_on_every_kind(self):
        """键集恒定（与路径无关）：「今天和昨天有什么不同」不该多出一堆假差异。"""
        expected = {"kind", "mode", "to_mode", "body", "summary", "details", "heavy"}
        for kind in MechanismKind:
            with self.subTest(kind=kind.value):
                self.assertEqual(set(self.client.get(url(kind.value)).json()), expected)

    def test_the_response_is_not_cacheable(self):
        """重请求 + 进程级副作用（策略模块重导入）：连「存下来」都不该发生。"""
        resp = self.client.get(url(MechanismKind.REGIME_GATE.value))
        self.assertEqual(resp["Cache-Control"], "no-store")

    def test_an_unknown_kind_is_a_400_not_a_404(self):
        resp = self.client.get(url("not_a_kind"))
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("error", resp.json())

    def test_the_kind_is_case_sensitive(self):
        """`MECHANISM` 不是取值。放行它就是在枚举之外再认一套拼法。"""
        self.assertEqual(self.client.get(url("MECHANISM")).status_code, 400)


# --------------------------------------------------------------------------- #
# 性质 1：正文与审计串同源（**本票的核心**）
# --------------------------------------------------------------------------- #


class TestBodyAndSummaryComeFromOneEvaluation(_Client):
    """数调用次数 + 断言两段文字确实来自那一次返回的对象。

    两件事缺一不可：只数次数的话，一个把 `body` 从 `page()` 取、把 `summary` 又从
    `confirmation_summary()` 取的实现会调两次（那是两次求值，会红）——但一个把两次调用的
    结果颠倒了用的实现只调两次中的每一个…… 这两种错法不是同一种，所以两种都钉。
    """

    def _spy(self, module, *, closing: bool | None = None):
        """把模块的 `page()` 换成「调一次真的，然后把两段文字贴上可识别的标记」。

        返回 `(calls, holders)`：`calls` 是每次调用的 kwargs 列表，`holders` 是每次返回的
        真实 `Briefing`（用来比对 `data` 是不是同一次求值的产物）。
        """
        original = module.page
        calls: list[dict] = []
        holders: list = []

        def spy(**kwargs):
            calls.append(kwargs)
            page = original(**kwargs)
            holders.append(page)
            return dataclasses.replace(
                page, body="<BODY-MARK>", summary="<SUMMARY-MARK>"
            )

        return calls, holders, patch.object(module, "page", side_effect=spy)

    def test_the_open_direction_takes_both_texts_from_one_call(self):
        calls, holders, patcher = self._spy(mechanism_switch)
        with patcher:
            body = self.client.get(url(MechanismKind.MECHANISM.value)).json()

        self.assertEqual(len(calls), 1, "`page()` 每次请求只许调一次")
        self.assertEqual(body["body"], "<BODY-MARK>")
        # 当前档位是 Shadow ⇒ 这一页对应「出 Shadow」⇒ summary 取 page.summary。
        self.assertEqual(body["summary"], "<SUMMARY-MARK>")

    def test_the_closed_direction_still_comes_from_one_call(self):
        """执行态下摘要换成 `closing_summary(page.data)`——**收的仍是那一次求值的 data**。

        这一条要钉的是「换一份摘要不等于重新求一次值」：一个另调一次
        `module.confirmation(now=...)` 的实现会多读一遍表，于是在日界附近（或判定刚跑完
        的那一分钟）给出另一个数。
        """
        # 落一条「执行态」的流水，让方向翻过来。
        from apps.regime.models import ActorKind, RegimeMechanismSwitch

        RegimeMechanismSwitch.objects.create(
            kind=MechanismKind.MECHANISM.value,
            from_mode=MechanismMode.SHADOW.value,
            to_mode=MechanismMode.EXECUTING.value,
            at=days(-1),
            actor_kind=ActorKind.CLI.value,
            actor_name="ops",
            reason="测试",
        )

        calls, holders, patcher = self._spy(mechanism_switch)
        with patcher:
            body = self.client.get(url(MechanismKind.MECHANISM.value)).json()

        self.assertEqual(len(calls), 1)
        self.assertEqual(body["body"], "<BODY-MARK>")
        self.assertEqual(body["to_mode"]["value"], MechanismMode.SHADOW.value)
        # 关闭方向的摘要是**从同一份 data 渲染**出来的那句话，不是 page.summary。
        self.assertEqual(
            body["summary"], mechanism_switch.closing_summary(holders[0].data)
        )
        self.assertNotEqual(body["summary"], "<SUMMARY-MARK>")

    def test_the_gate_direction_reaches_the_page_as_closing(self):
        """gate 是唯一认方向的模块：执行态下页面必须是**关闭那一套正文**。

        它是撤防方向上唯一拦得住「关掉就安全了」这句要命误读的东西（保命档那一层没有
        开关可翻），所以方向没传到位时，页面会把「会拦住谁」印在一次撤防动作上。
        """
        from apps.regime.models import ActorKind, RegimeMechanismSwitch

        RegimeMechanismSwitch.objects.create(
            kind=MechanismKind.REGIME_GATE.value,
            from_mode=MechanismMode.SHADOW.value,
            to_mode=MechanismMode.EXECUTING.value,
            at=days(-1),
            actor_kind=ActorKind.CLI.value,
            actor_name="ops",
            reason="测试",
        )

        calls, holders, patcher = self._spy(gate_switch)
        with patcher:
            body = self.client.get(url(MechanismKind.REGIME_GATE.value)).json()

        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0].get("closing"))
        # 正文第一行是关闭那一套（与 CLI / `/regime gate off` 逐字同一个常量）。被替身
        # 换掉的是响应里那一段，真实那一份在 `holders` 里——两个都钉，各自钉一件事。
        self.assertTrue(holders[0].body.startswith(CLOSING_TITLE), holders[0].body[:80])
        self.assertEqual(body["body"], "<BODY-MARK>")

    def test_the_three_pages_are_not_called_by_the_other_endpoints(self):
        """反方向的锚：`overview` 一次都不许调 `page()`。

        它今天已经由 `test_views` 的查询数上界钉住；这条再钉一层**直接**断言，因为查询数
        只是那把尺子（一个 `page()` 内部的读可能恰好落在 5 次以内）。
        """
        for module in (mechanism_switch, breaker_switch, gate_switch):
            with self.subTest(module=module.__name__):
                with patch.object(module, "page") as page:
                    resp = self.client.get("/api/regime/overview/")
                self.assertEqual(resp.status_code, 200)
                page.assert_not_called()


# --------------------------------------------------------------------------- #
# 性质 5：方向与 details
# --------------------------------------------------------------------------- #


class TestDirectionAndDetails(_Client):
    def test_shadow_means_this_page_is_the_exit_action(self):
        body = self.client.get(url(MechanismKind.MECHANISM.value)).json()

        self.assertEqual(body["mode"]["value"], MechanismMode.SHADOW.value)
        self.assertEqual(body["mode"]["display"], MechanismMode.SHADOW.display)
        self.assertEqual(body["to_mode"]["value"], MechanismMode.EXECUTING.value)
        self.assertEqual(body["to_mode"]["display"], MechanismMode.EXECUTING.display)

    def test_details_pair_every_enum_with_its_display(self):
        """`kind` / `mode` / `to_mode` 在**每个**响应里都是成对的。`details` 里那些枚举
        （gate 的清理原因码、breaker 的事件三档）同样成对——少一个不会报错、只会静默
        显示 slug，而少了 display 的那一栏在页面上看起来像「后端没给」。"""
        for kind in MechanismKind:
            with self.subTest(kind=kind.value):
                body = self.client.get(url(kind.value)).json()
                for field in ("kind", "mode", "to_mode"):
                    self.assertEqual(set(body[field]), {"value", "display"}, field)
                    self.assertTrue(body[field]["display"], field)

        # gate 的解除原因码：每一行都要带人话（词表在 `gate_switch`）。
        gate_body = self.client.get(url(MechanismKind.REGIME_GATE.value)).json()
        for row in gate_body["details"]["closing_by_code"]:
            self.assertEqual(set(row), {"code", "display", "count"})
            self.assertEqual(
                row["display"], gate_switch.gate_close_reason_display(row["code"])
            )
            self.assertIsInstance(row["count"], int)

    def test_the_details_carry_the_numbers_the_body_talks_about(self):
        """`details` 是排版用的数，它得与正文说的那几件事**对应**（不是重拼正文，而是
        同一个 `data` 的另一个出口）。这里只抽查三个开关各一个最能区分的数。"""
        mechanism = self.client.get(url(MechanismKind.MECHANISM.value)).json()[
            "details"
        ]
        self.assertIn("ran_days", mechanism["achieved"])
        self.assertIn("expired", mechanism["expiry"])
        self.assertEqual(
            set(mechanism["trigger_days"]),
            {"total", "deactivation", "high_vol", "event"},
        )
        # **门槛只出现一次**：`rate` 与 `thresholds` 里各有一份的话，改配置时改一处就会让
        # 页面自相矛盾（`rate["max"]` 与 `thresholds["max_trigger_rate"]` 是同一个数）。
        self.assertNotIn("max", mechanism["rate"])
        self.assertIn("max_trigger_rate", mechanism["thresholds"])
        # 一致率的门槛读的是 `config.SHADOW`，不是这次比对的结果，所以它不在这块里。
        self.assertNotIn("min_rate", mechanism["agreement"])

        breaker = self.client.get(url(MechanismKind.EVENT_BREAKER.value)).json()[
            "details"
        ]
        self.assertEqual(
            set(breaker),
            {
                "now",
                "until",
                "horizon_days",
                "library_total",
                "firing_total",
                "overlapping",
                "starting",
                "open_now",
                "next_event",
                "nearest",
                "staleness_days",
            },
        )

        gate = self.client.get(url(MechanismKind.REGIME_GATE.value)).json()["details"]
        self.assertIn("managed", gate)
        self.assertIn("blanket_hold", gate)
        self.assertIn("skipped", gate)
        # 冷启动下 `regime` 是 `None` 而 `regime_display` 仍要给一句话（理由同 T2 的
        # `overview.regime_state.cold`）：`null` 只说了「没有」。
        self.assertIsNone(gate["regime"])
        self.assertTrue(gate["regime_display"])


# --------------------------------------------------------------------------- #
# 性质 3：冷启动
# --------------------------------------------------------------------------- #


class TestColdStart(_Client):
    """空库 + 无判定 + 无当代：三张页面都照常返回，**而且它们自己的那句话一个不少**。

    替它们编一句就是给冷启动造第二个说法，所以这里断言的是「页面自己的收场词还在」，
    不是「这里给了一句什么」。
    """

    def test_breaker_page_says_the_library_is_empty(self):
        body = self.client.get(url(MechanismKind.EVENT_BREAKER.value)).json()
        self.assertEqual(body["details"]["library_total"], 0)
        # 空库那一段必须把**后果**说出来（第 169 条：「沉默必须能被识别为异常」）。
        self.assertIn(breaker_switch.empty_library_warning()[:20], body["body"])

    def test_mechanism_page_says_shadow_has_not_started(self):
        body = self.client.get(url(MechanismKind.MECHANISM.value)).json()
        self.assertIsNone(body["details"]["achieved"]["started_on"])
        self.assertEqual(body["details"]["achieved"]["ran_days"], 0)
        self.assertIn("机制整体 · 出 Shadow 体检", body["body"])

    def test_gate_page_says_there_is_no_generation(self):
        body = self.client.get(url(MechanismKind.REGIME_GATE.value)).json()
        self.assertIn(
            body["details"]["skipped"],
            {"no_generation", "cold_start"},
            body["details"]["skipped"],
        )
        self.assertTrue(body["details"]["skipped_display"])
        self.assertTrue(body["body"].startswith(PAGE_TITLE), body["body"][:80])


# --------------------------------------------------------------------------- #
# 有数据的 gate：关闭方向那一页（它有两个方向的正文）
# --------------------------------------------------------------------------- #


class TestTheGateClosingPage(_GateFixture):
    """有当前代、有一条该停的决策行、档位是执行态：关闭方向的那一页。

    `test_gate_switch` 已经钉了那页上的数；这里只钉**端点把方向传对了**——它把
    `/regime gate off` 会看到的那一页原样端出来。

    **时钟必须钉在同一个时刻**：端点不接受 `now` 参数（它走 `timezone.now()`），而夹具的
    世界是相对 `NOW` 铺的。不钉的话，请求读到的是真实时钟，于是「状态过期」会把这一页
    从「正常一轮」翻成「算不出该停哪些」——那样比对的就不是同一个世界，用例会以一句
    看起来像 bug 的断言失败（本票第一版就撞上了这条）。
    """

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.live_world(switch=True)

    def _at_now(self):
        return patch("django.utils.timezone.now", return_value=NOW)

    def test_the_closing_page_is_the_one_the_slash_command_prints(self):
        with self._at_now():
            # 不带 `now=`：走的就是端点走的那条路（`timezone.now()`）。
            expected = gate_switch.page(closing=True)
            body = self.client.get(url(MechanismKind.REGIME_GATE.value)).json()

        self.assertEqual(body["mode"]["value"], MechanismMode.EXECUTING.value)
        self.assertEqual(body["to_mode"]["value"], MechanismMode.SHADOW.value)
        # 正文与摘要都与 `/regime gate off` 走同一个函数（页面上那句「这一步本身不改变
        # 任何东西」必须逐字一致，CLI / slash / 网页三处同一句话）。
        self.assertTrue(body["body"].startswith(CLOSING_TITLE), body["body"][:80])
        self.assertEqual(body["body"], expected.body)
        self.assertEqual(body["summary"], gate_switch.closing_summary(expected.data))

    def test_the_open_direction_on_the_same_world_is_the_opening_page(self):
        """同一个世界、档位回到 Shadow：正文换成打开那一套，摘要换成 `page().summary`。

        两条断言合起来才是「方向由档位决定」：只有一个方向的话，一个写死 `closing=False`
        的实现会把关闭页印在一次撤防上，而那正是最要命的那句误读（保命档那一层没有开关）。
        """
        from apps.regime.models import ActorKind, RegimeMechanismSwitch

        RegimeMechanismSwitch.objects.create(
            kind=MechanismKind.REGIME_GATE.value,
            from_mode=MechanismMode.EXECUTING.value,
            to_mode=MechanismMode.SHADOW.value,
            at=days(1),
            actor_kind=ActorKind.CLI.value,
            actor_name="ops",
            reason="测试",
        )
        with self._at_now():
            expected = gate_switch.page(closing=False)
            body = self.client.get(url(MechanismKind.REGIME_GATE.value)).json()

        self.assertEqual(body["mode"]["value"], MechanismMode.SHADOW.value)
        self.assertEqual(body["to_mode"]["value"], MechanismMode.EXECUTING.value)
        self.assertTrue(body["body"].startswith(PAGE_TITLE), body["body"][:80])
        self.assertEqual(body["summary"], expected.summary)


# --------------------------------------------------------------------------- #
# 性质 7：T4 的入口（显式方向）
# --------------------------------------------------------------------------- #


class TestTheExplicitDirection(_Client):
    """`evaluate(to_mode=...)`——写 API（#42）要走的那个入口。

    端点自己不用它（它问的是「此刻我能做的那一步」），但它必须能回答另一个问题：
    「**你要做的这一步**会写进去什么」。两个问题的分叉只在「本来就是这一档」那种请求上
    ——反推给出的是反向，而请求的是原地不动。`reason` 只在真正翻过去时才写，所以那一步
    的摘要必须按**请求的**方向取，不能按当前档位反推。

    T4 的三条硬约束全部落在 `Briefing` 的两个字段上：`data`（那份快照）与 `summary`
    （从它渲染的审计串）。所以这里钉的是「同一个 `data`、按方向取两份摘要中的一份」。
    """

    def test_an_explicit_target_flips_the_summary_without_a_second_evaluation(self):
        # 当前档位是 Shadow；显式请求 Shadow（「原地不动」那一支）。
        result = briefing_module.evaluate(
            MechanismKind.MECHANISM, to_mode=MechanismMode.SHADOW
        )

        self.assertEqual(result.mode, MechanismMode.SHADOW)
        self.assertEqual(result.to_mode, MechanismMode.SHADOW)
        self.assertEqual(result.summary, mechanism_switch.closing_summary(result.data))

    def test_omitting_the_target_keeps_the_live_endpoint_semantics(self):
        """省略方向 ⇒ 反向那一档，摘要取**同一次** `page().summary`（用标记验证，不是
        再求一次值拿两段文字比对——那会变成「两个时刻的同一个数」的比对）。"""
        original = mechanism_switch.page

        def marked(**kwargs):
            return dataclasses.replace(original(**kwargs), body="<B>", summary="<S>")

        with patch.object(mechanism_switch, "page", side_effect=marked):
            result = briefing_module.evaluate(MechanismKind.MECHANISM)

        self.assertEqual(result.to_mode, MechanismMode.EXECUTING)
        self.assertEqual(result.summary, "<S>")
        self.assertEqual(result.body, "<B>")

    def test_an_unknown_target_is_a_value_error(self):
        """T4 把它转成 400（未知 mode 是请求拼错，不是资源不存在）。"""
        with self.assertRaises(ValueError):
            briefing_module.evaluate(MechanismKind.MECHANISM, to_mode="not_a_mode")

    def test_the_target_mode_is_the_one_the_flip_would_use(self):
        """方向与 `flip_*` 的入参是同一个枚举值，不是字符串。"""
        result = briefing_module.evaluate(
            MechanismKind.EVENT_BREAKER, to_mode=MechanismMode.EXECUTING
        )
        self.assertIs(result.to_mode, MechanismMode.EXECUTING)
        self.assertEqual(result.summary, breaker_switch.page().summary)
