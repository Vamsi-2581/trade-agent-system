"""写 API `POST /api/regime/switch/`（P5 子票 #42，map #40）。

`test_briefing.py` 钉的是那张**读**的页；这个文件钉的是它旁边那道**写**的门。三张确认页
自己算出来的数这里一行都不断言（`test_gate_switch` / `test_breaker_switch` /
`test_mechanism_switch` 各钉各的）——这里只钉**这一层**：谁能写、写了什么、什么时候不写。

七条性质，每一条坏了都不报警、只出错：

1. **权限是 `IsStaffUser`，且 403 不是 404**：非 staff 403、匿名 401、staff 放行。403 与
   404 对前端是两件事（前者「你没有这个权限」，后者「这条记录不存在，去刷新」），混成
   404 会让人反复刷新一个永远不出现的东西。
2. **`reason` 与页面同源，请求体里的 `reason` 被忽略**：库里那一串必须等于**本次请求**
   里那次求值的摘要。这条是唯一一条**没有报错路径**的错法：让客户端传 reason 会立刻造出
   两种流水，两边都「看起来正常」。所以既断言库里等于求值结果，也传一个假 reason 断言
   它没进库。
3. **`flip_*` 返回 `None` 是 200 + `changed:false`，且没有多写一条流水**：这不是错误，
   gate 方向上它甚至真的对了一次账。用 400 回报会让前端显示成失败，人看到失败只会再点。
4. **参数错 400，`message` 原样透出**：未知 kind / 未知 mode 都是请求拼错（改请求），
   不是资源不存在（刷新页面）。
5. **gate 方向对账失败 → 500 且说明「流水已写、表未对上」**：那一刻流水行**确实已经落库**，
   不写出来人会以为整件事没发生，于是去按第二次。
6. **落库行的审计四件事**：`actor_kind == "web"`、`actor_name == 用户名`、
   `reason == 该次求值的摘要`、`from_mode` 是真读出来的（不是写死 shadow）。
7. **`kind` → 写方的映射是一张表，且键集就是 `MechanismKind`**：加第四个开关而忘了登记时
   红在那条断言上，而不是在页面上变成一个 500。短名表同理。

夹具：直接复用 `test_views._Fixture`（它又是 `test_gate_run._Fixture` 的扩展：三个策略 +
一个用户 + 注册表替身 + 能登录的客户端）。gate 那一档用 `test_gate_switch._Fixture` 的
`live_world()`——**不另造一套世界**：两处各造一套，迟早会出现「同一个动作在两个文件里
写出两种行」。
"""

from __future__ import annotations

import dataclasses

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.regime import breaker_switch, gate_switch, mechanism_switch
from apps.regime import views
from apps.regime.models import (
    ActorKind,
    MechanismKind,
    MechanismMode,
    RegimeMechanismSwitch,
)
from apps.regime.tests.test_gate_run import days
from apps.regime.tests.test_gate_switch import _Fixture as _GateFixture
from apps.regime.tests.test_views import _Fixture as _Client

SWITCH = "/api/regime/switch/"
User = get_user_model()


class _StaffFixture(_Client):
    """把 `test_views._Fixture` 那个用户升级成 staff，再重新认证一次。

    `is_admin` 是 `is_staff` 背后那个布尔（`UserManager.create_user` 不接它，必须单独写
    一行）；`force_authenticate` 缓存的是**实例**，所以升级之后要再调一次，否则权限类读到
    的还是那个 `is_admin=False` 的旧实例。
    """

    def setUp(self):
        super().setUp()
        _promote_to_staff(self.user)
        self.client.force_authenticate(user=self.user)


class _GateStaffFixture(_GateFixture):
    """gate 那一档要的是 `test_gate_switch._Fixture` 的世界（`live_world()` 在那里）。

    **不把 `live_world` 抄一份到这里**：它是「一个正常一轮的世界」的唯一出处，抄一份就是
    给「该停哪些」造第二个说法，而那正是 map #40 从头到尾在防的形态。这个类只补上
    `_GateFixture` 没有的那两件事：一个 API 客户端，和 staff 身份。
    """

    def setUp(self):
        super().setUp()
        _promote_to_staff(self.user)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)


def _promote_to_staff(user) -> None:
    """把一个 `test_gate_run` 造出来的用户提成 staff。"""
    user.is_admin = True
    user.save(update_fields=["is_admin"])


def _body(
    kind: MechanismKind | str = MechanismKind.MECHANISM, mode=MechanismMode.EXECUTING
):
    """一个合法的请求体。`mode` 可以传字符串（那是 HTTP 里真实的形状）。"""
    return {
        "kind": kind.value if isinstance(kind, MechanismKind) else kind,
        "mode": mode.value if isinstance(mode, MechanismMode) else mode,
    }


# --------------------------------------------------------------------------- #
# 性质 1：权限
# --------------------------------------------------------------------------- #


class TestOnlyStaffMaySwitch(TestCase):
    """写只对 `is_staff` 开放（ADR 0005）。**它是界面纪律，不是安全边界**——slash 通路
    没有跟着收紧，但那不改变这一层该拦谁。"""

    def setUp(self):
        self.user = User.objects.create_user(
            email="plain@test.local", username="plain", password="pw12345"
        )
        self.staff = User.objects.create_user(
            email="staff@test.local", username="staff", password="pw12345"
        )
        self.staff.is_admin = True
        self.staff.save(update_fields=["is_admin"])

    def _client(self, user=None) -> APIClient:
        client = APIClient()
        if user is not None:
            client.force_authenticate(user=user)
        return client

    def test_a_plain_user_is_forbidden(self):
        """**403 不是 404**：这不是「不属于你的资源」，是明确的权限拒绝。"""
        resp = self._client(self.user).post(SWITCH, _body(), format="json")
        self.assertEqual(resp.status_code, 403, resp.content)
        self.assertEqual(RegimeMechanismSwitch.objects.count(), 0)

    def test_an_anonymous_request_is_unauthenticated(self):
        """401 而不是 403：`JWTAuthentication` 声明了 `authenticate_header`，DRF 走
        `NotAuthenticated`。两者对前端完全不同（重新登录 vs 你没有这个权限）。"""
        resp = self._client().post(SWITCH, _body(), format="json")
        self.assertEqual(resp.status_code, 401, resp.content)
        self.assertEqual(RegimeMechanismSwitch.objects.count(), 0)

    def test_staff_may_switch(self):
        """反向用例：只钉「staff 能过」是没用的（一个恒 False 的实现下它也绿）。"""
        resp = self._client(self.staff).post(
            SWITCH, _body(MechanismKind.EVENT_BREAKER), format="json"
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(resp.json()["changed"])
        self.assertEqual(RegimeMechanismSwitch.objects.count(), 1)


# --------------------------------------------------------------------------- #
# 性质 7：kind → 写方 / 短名的映射
# --------------------------------------------------------------------------- #


class TestTheDispatchTables(TestCase):
    def test_every_kind_has_a_writer(self):
        """**键集相等**，不是「三个都在」。加第四个开关而忘了登记时，这条要红——否则那个
        开关的写入口是一个 404（路由在、分派表里没有）。"""
        self.assertEqual(set(views.FLIP_SOURCES), set(MechanismKind))

    def test_every_kind_has_a_short_name(self):
        """短名表也要跟着长。缺一行时回执里会出现 `KeyError`——比缺一句文案更糟。"""
        self.assertEqual(set(views.KIND_SHORT_NAME), set(MechanismKind))

    def test_the_writers_are_the_three_existing_flip_functions(self):
        """**不新写任何写逻辑**：分派表指向的就是 CLI / slash 走的那三个函数本身。

        断言的是 `(模块, 函数名)` 解析出来的那个对象，而不是字典里存的东西——后者现在是
        一对 `(module, name)`，在调用那一刻才取属性（那样 `patch.object` 才拦得住）。
        """
        expected = {
            MechanismKind.MECHANISM: (mechanism_switch, "flip_mechanism"),
            MechanismKind.EVENT_BREAKER: (breaker_switch, "flip_event_breaker"),
            MechanismKind.REGIME_GATE: (gate_switch, "flip_regime_gate"),
        }
        for kind, (module, name) in expected.items():
            with self.subTest(kind=kind.value):
                got_module, got_name = views.FLIP_SOURCES[kind]
                self.assertIs(got_module, module)
                self.assertEqual(got_name, name)
                # 名字在模块上真的解析得到一个可调用对象（拼错了这条就红，而不是在
                # 第一次真实切换时变成一个 `AttributeError`）。
                self.assertTrue(callable(getattr(got_module, got_name)))


# --------------------------------------------------------------------------- #
# 性质 2、6：reason 同源 + 审计四件事
# --------------------------------------------------------------------------- #


class TestTheWrittenRow(_StaffFixture):
    def test_the_reason_is_the_summary_of_one_evaluation(self):
        """**同源**：库里那一串就是本次请求里那**一次** `page()` 求值给的摘要。

        用标记值验证（`dataclasses.replace` 把摘要换成 `<S>`），而不是「再求一次值然后比
        两段文字」——后者是两个时刻的同一个数，随时钟走过日界就会变红，且它**挡不住**
        「求值两次、用第二次的摘要写」这种真实的错法。标记值 + 调用次数一起才钉得住：
        标记证明「写的是这一次的值」，次数证明「只有一次」。
        """
        original = breaker_switch.page
        calls: list[int] = []

        def marked(**kwargs):
            calls.append(1)
            return dataclasses.replace(original(**kwargs), summary="<S>")

        with patch.object(breaker_switch, "page", side_effect=marked):
            resp = self.client.post(
                SWITCH,
                _body(MechanismKind.EVENT_BREAKER, MechanismMode.EXECUTING),
                format="json",
            )

        self.assertEqual(resp.status_code, 200, resp.content)
        row = RegimeMechanismSwitch.objects.get()
        self.assertEqual(row.reason, "<S>")
        self.assertEqual(resp.json()["reason"], "<S>")
        self.assertEqual(
            len(calls), 1, "求值了不止一次：正文与 reason 就不是同一份快照了"
        )

    def test_the_closing_direction_takes_the_closing_summary(self):
        """退回 Shadow 的 `reason` 问的是另一件事（「关掉的那一刻放掉了什么」），所以它取
        `closing_summary(data)` 而不是 `page().summary`——与 CLI / slash 逐字一致。

        只钉「写了 reason」是挡不住这条的：一个恒取 `page().summary` 的实现会写下「打开”
        那句话，而它读起来完全正常。
        """
        self.switch_row(
            MechanismKind.EVENT_BREAKER, MechanismMode.EXECUTING, at=days(-1)
        )

        resp = self.client.post(
            SWITCH,
            _body(MechanismKind.EVENT_BREAKER, MechanismMode.SHADOW),
            format="json",
        )

        self.assertEqual(resp.status_code, 200, resp.content)
        row = RegimeMechanismSwitch.objects.order_by("-id").first()
        self.assertIn("人工关闭事件熔断", row.reason)
        self.assertNotIn("人工打开事件熔断", row.reason)

    def test_a_client_supplied_reason_is_ignored(self):
        """**请求体里的 `reason` 一律忽略**。传一个一眼能认出来的假值，断言它没进库、
        也没回显——回显它会让客户端以为自己说了算。"""
        fake = "客户端编的理由：我不想拦了"
        body = {**_body(MechanismKind.EVENT_BREAKER), "reason": fake}

        resp = self.client.post(SWITCH, body, format="json")

        self.assertEqual(resp.status_code, 200, resp.content)
        row = RegimeMechanismSwitch.objects.get()
        self.assertNotIn(fake, row.reason)
        self.assertNotIn(fake, resp.json()["reason"])
        self.assertEqual(row.reason, resp.json()["reason"])

    def test_the_row_records_a_web_actor_with_the_username(self):
        """审计四件事里的「谁」：`actor_kind == "web"`、`actor_name` 取认证过的用户名。

        不与 `cli` 合并是 P5 的全部意义之一——事后要能分清「命令行敲的还是网页点的」。
        """
        self.client.post(SWITCH, _body(MechanismKind.EVENT_BREAKER), format="json")

        row = RegimeMechanismSwitch.objects.get()
        self.assertEqual(row.actor_kind, ActorKind.WEB.value)
        self.assertEqual(row.actor_name, self.user.username)

    def test_from_mode_is_read_not_hardcoded(self):
        """`from_mode` 真读出来。写死 shadow 的话，「从执行态关掉、再打开」这一段会记成
        「从 Shadow 打开」，而这条流水正是事后回答「当时它到底开没开」的唯一材料。"""
        self.switch_row(
            MechanismKind.EVENT_BREAKER, MechanismMode.EXECUTING, at=days(-1)
        )
        # 从执行态关掉。
        resp = self.client.post(
            SWITCH,
            _body(MechanismKind.EVENT_BREAKER, MechanismMode.SHADOW),
            format="json",
        )

        self.assertEqual(resp.status_code, 200, resp.content)
        row = RegimeMechanismSwitch.objects.order_by("-id").first()
        self.assertEqual(row.from_mode, MechanismMode.EXECUTING.value)
        self.assertEqual(row.to_mode, MechanismMode.SHADOW.value)
        self.assertEqual(
            resp.json()["from_mode"]["value"], MechanismMode.EXECUTING.value
        )


# --------------------------------------------------------------------------- #
# 性质 3：本来就是这一档
# --------------------------------------------------------------------------- #


class TestAlreadyInThatMode(_StaffFixture):
    def test_pressing_the_same_button_is_a_200_with_changed_false(self):
        """**200 + `changed:false`，不是 400**。它不是客户端的错，是一次成功但无变化的
        动作——用一个错误码回报，前端就会把它显示成失败。"""
        self.switch_row(
            MechanismKind.EVENT_BREAKER, MechanismMode.EXECUTING, at=days(-1)
        )
        before = RegimeMechanismSwitch.objects.count()

        resp = self.client.post(
            SWITCH,
            _body(MechanismKind.EVENT_BREAKER, MechanismMode.EXECUTING),
            format="json",
        )

        self.assertEqual(resp.status_code, 200, resp.content)
        payload = resp.json()
        self.assertFalse(payload["changed"])
        self.assertIsNone(payload["at"])
        # 那句 display 说清了这件事（前端直接显示，不重拼）。
        self.assertIn("本来就是", payload["display"])
        # **没有多写一条流水**——这是这条性质的核心，只断言 200 是挡不住它的。
        self.assertEqual(RegimeMechanismSwitch.objects.count(), before)

    def test_the_gate_still_reconciles_on_a_no_op(self):
        """gate 方向「本来就是这一档」**仍对一次账**——那是它与其他两个开关唯一的语义
        差异，也是「再点一次」在 gate 上真的有用的原因。回执里要带得出来。"""
        with patch.object(
            gate_switch.gate_run, "sync", return_value={"skipped": None}
        ) as sync:
            resp = self.client.post(
                SWITCH,
                _body(MechanismKind.REGIME_GATE, MechanismMode.SHADOW),
                format="json",
            )
            self.assertEqual(resp.status_code, 200, resp.content)
            self.assertFalse(resp.json()["changed"])
            self.assertEqual(sync.call_count, 1)
            # 摘要也回显了（对账摘要不是给人读的正文，是给排障的人看的事实）。
            self.assertIn("sync", resp.json())
        # 没有写第二条流水。
        self.assertEqual(
            RegimeMechanismSwitch.objects.filter(
                kind=MechanismKind.REGIME_GATE.value
            ).count(),
            0,
        )

    def test_the_display_matches_the_other_entry_points(self):
        """三个入口说的是同一件事。这句 display 用的词必须在 CLI / slash 的回执里出现过
        ——`KIND_SHORT_NAME` 就是那批词抄成的一张表。"""
        for kind in MechanismKind:
            with self.subTest(kind=kind.value):
                display = views._already_in_mode_display(kind, MechanismMode.SHADOW)
                self.assertIn(views.KIND_SHORT_NAME[kind], display)
                self.assertIn("本来就是", display)


# --------------------------------------------------------------------------- #
# 性质 4：参数错
# --------------------------------------------------------------------------- #


class TestBadParameters(_StaffFixture):
    def test_an_unknown_kind_is_a_400(self):
        resp = self.client.post(
            SWITCH, {"kind": "nope", "mode": "executing"}, format="json"
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("未知的 kind", resp.json()["error"])

    def test_an_unknown_mode_is_a_400(self):
        resp = self.client.post(
            SWITCH, {"kind": "mechanism", "mode": "nope"}, format="json"
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("未知的 mode", resp.json()["error"])

    def test_a_missing_field_is_a_400_not_a_crash(self):
        """缺字段走的是同一条 400 路，不是 `KeyError` → 500。"""
        for body in ({}, {"kind": "mechanism"}, {"mode": "executing"}):
            with self.subTest(body=body):
                resp = self.client.post(SWITCH, body, format="json")
                self.assertEqual(resp.status_code, 400, resp.content)

    def test_nothing_is_written_on_a_bad_request(self):
        self.client.post(SWITCH, {"kind": "nope", "mode": "nope"}, format="json")
        self.assertEqual(RegimeMechanismSwitch.objects.count(), 0)

    def test_a_value_error_from_the_writer_keeps_its_message(self):
        """「切换原因不能为空」这类话是**可自愈的提示**，原样透出（T6 之后前端会显示它）。"""
        with patch.object(
            breaker_switch,
            "flip_event_breaker",
            side_effect=ValueError("切换原因不能为空：测试"),
        ):
            resp = self.client.post(
                SWITCH, _body(MechanismKind.EVENT_BREAKER), format="json"
            )

        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(resp.json()["error"], "切换原因不能为空：测试")


# --------------------------------------------------------------------------- #
# 性质 5：gate 对账失败
# --------------------------------------------------------------------------- #


class TestTheGateReconcileFailure(_GateStaffFixture):
    """`flip_regime_gate` 的 docstring 明说 `gate_run.sync` 的失败要原样往上抛——
    「半个动作比不动作更坏」。所以本端点**不吞它**，但不能让它变成 500 白页。"""

    def test_a_sync_failure_is_a_500_that_says_the_row_was_already_written(self):
        boom = RuntimeError("对账炸了：测试")
        with patch("apps.regime.gate_run.sync", side_effect=boom):
            resp = self.client.post(
                SWITCH,
                _body(MechanismKind.REGIME_GATE, MechanismMode.EXECUTING),
                format="json",
            )

        self.assertEqual(resp.status_code, 500, resp.content)
        error = resp.json()["error"]
        # 句子里必须出现这三件事，缺任一件人就会去按第二次：档位真的翻了、流水已写、表没对上。
        self.assertIn("流水已写", error)
        self.assertIn("表未对上", error)
        self.assertIn("对账炸了", error)

    def test_the_switch_row_really_is_in_the_database_after_that_failure(self):
        """**这句话不是修辞**：那个状态下流水行确实已经落库了（`flip_regime_gate` 先写行
        再对账）。用例把它钉住，免得将来有人「顺手」把写行挪到对账之后，而 500 的措辞
        留在原地说着反话。"""
        with patch("apps.regime.gate_run.sync", side_effect=RuntimeError("boom")):
            resp = self.client.post(
                SWITCH,
                _body(MechanismKind.REGIME_GATE, MechanismMode.EXECUTING),
                format="json",
            )

        self.assertEqual(resp.status_code, 500)
        row = RegimeMechanismSwitch.objects.filter(
            kind=MechanismKind.REGIME_GATE.value
        ).first()
        self.assertIsNotNone(row)
        self.assertEqual(row.to_mode, MechanismMode.EXECUTING.value)

    def test_a_successful_gate_switch_carries_the_sync_summary(self):
        """成功路径也要把对账摘要带回去：省略它，前端就只能说「成功了」。"""
        self.live_world()
        with patch(
            "apps.regime.gate_run.sync",
            return_value={"skipped": None, "declarations": [], "regime": "downtrend"},
        ):
            resp = self.client.post(
                SWITCH,
                _body(MechanismKind.REGIME_GATE, MechanismMode.EXECUTING),
                format="json",
            )

        self.assertEqual(resp.status_code, 200, resp.content)
        payload = resp.json()
        self.assertTrue(payload["changed"])
        self.assertEqual(payload["sync"]["regime"], "downtrend")


class TestTheGateSwitch(_GateStaffFixture):
    """gate 那一档走一遍真路（不 patch `sync`）——另外两个开关的写方在各自的测试里钉，
    这里只确认「网页这条路真的把 gate 翻过去了」。"""

    def test_a_real_gate_flip(self):
        self.live_world()

        resp = self.client.post(
            SWITCH,
            _body(MechanismKind.REGIME_GATE, MechanismMode.EXECUTING),
            format="json",
        )

        self.assertEqual(resp.status_code, 200, resp.content)
        payload = resp.json()
        self.assertTrue(payload["changed"])
        self.assertEqual(payload["from_mode"]["value"], MechanismMode.SHADOW.value)
        self.assertEqual(payload["to_mode"]["value"], MechanismMode.EXECUTING.value)
        row = RegimeMechanismSwitch.objects.get()
        self.assertEqual(row.actor_kind, ActorKind.WEB.value)
        self.assertEqual(row.actor_name, self.user.username)
        # 对账真的跑过了：这一轮落下了声明行（`live_world` 造的就是「该停一个」）。
        self.assertIn("sync", payload)


# --------------------------------------------------------------------------- #
# 性质 8：响应形状恒定
# --------------------------------------------------------------------------- #


class TestThePayloadShape(_StaffFixture):
    def test_both_outcomes_share_the_same_keys(self):
        """改档与「本来就是」两条路径**键集恒定**：少一个键不会报错，只会让前端静默拿到
        `undefined`。`sync` 是 gate 独有、按路径出现的，所以它不在这条断言里。"""
        common = {
            "kind",
            "changed",
            "from_mode",
            "to_mode",
            "display",
            "reason",
            "at",
        }
        changed = self.client.post(
            SWITCH,
            _body(MechanismKind.EVENT_BREAKER, MechanismMode.EXECUTING),
            format="json",
        ).json()
        again = self.client.post(
            SWITCH,
            _body(MechanismKind.EVENT_BREAKER, MechanismMode.EXECUTING),
            format="json",
        ).json()

        self.assertEqual(set(changed), common)
        self.assertEqual(set(again), common)

    def test_the_modes_are_paired_with_their_display(self):
        """枚举一律 `{value, display}` 成对——前端不重拼（T2 定下的契约）。"""
        payload = self.client.post(
            SWITCH,
            _body(MechanismKind.MECHANISM, MechanismMode.EXECUTING),
            format="json",
        ).json()

        for key in ("kind", "from_mode", "to_mode"):
            with self.subTest(key=key):
                self.assertEqual(set(payload[key]), {"value", "display"})
                self.assertTrue(payload[key]["display"])

    def test_the_response_is_not_cacheable(self):
        """写入口一律 no-store：任何缓存/重放都会让「已经翻过了」变成一个可复现的假事实。"""
        resp = self.client.post(
            SWITCH,
            _body(MechanismKind.MECHANISM, MechanismMode.EXECUTING),
            format="json",
        )
        self.assertEqual(resp["Cache-Control"], "no-store")


# --------------------------------------------------------------------------- #
# 性质 9：三个开关都真的能通过这条路翻
# --------------------------------------------------------------------------- #


class TestAllThreeKindsWork(_StaffFixture):
    def test_each_kind_flips_and_writes_exactly_one_row_of_its_own(self):
        """三个开关各翻各的、各写各的行，**互不串档**（`KIND` 是三个模块各自的常量）。"""
        for kind in MechanismKind:
            with self.subTest(kind=kind.value):
                RegimeMechanismSwitch.objects.all().delete()
                resp = self.client.post(
                    SWITCH, _body(kind, MechanismMode.EXECUTING), format="json"
                )
                self.assertEqual(resp.status_code, 200, resp.content)
                self.assertTrue(resp.json()["changed"])
                rows = list(RegimeMechanismSwitch.objects.all())
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0].kind, kind.value)
                self.assertEqual(rows[0].to_mode, MechanismMode.EXECUTING.value)
