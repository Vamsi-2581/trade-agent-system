"""机制读面的第四只读端点：`GET api/regime/halt/`——此刻真的在拦的停止层。

与 `test_views.py` 的分工：那个文件钉的是「池化表怎么被读出去」，本文件钉的是「停止判定
怎么被读出去」。求值本身在 `test_halt.py` 里已经钉过，这里一条都不重述——本文件只问
一件事：**转述忠不忠实**。

四条性质，每一条坏了都不报警、只出错：

1. **匿名 401，登录用户都能读**——与 `overview` 同一条权限口径（读对所有登录用户开放）。
2. **开关关闭的触发源不出现在响应里**。这是本端点与 `live_declarations` 的唯一区别，也是
   它存在的理由：横幅的语义是「此刻挡不挡得住」，不是「库里有什么行」。读成后者的话，
   一条触发源已经关掉的声明会被报成「在拦」——而那正是本仓反复在防的「同一件事两个说法」。
3. **保命档与手动熔断永远出现**（`HALT_TRIGGER_SWITCH` 里它们是 `None`）。这条与性质 2
   是一对：两个方向都钉住，才挡得住将来「顺手给保命档也加个开关」。
4. **层是一个集合**：多层同时生效时**全部**列出。只报第一层会让人按报出来那层去排查，
   而解除它之后单还是下不出去（`HaltVerdict.reason` 的 docstring 写过这件事）。

外加一条形状纪律：`halt_layer_payload` **不发** `trigger` / `scope` / `label` 的零件——
`text` 已经是「行 → 层」的唯一换算口，发出零件等于给前端留一条自己再拼一遍的路。所以
断言的是**键集**（多一个键就是多一条分叉的路）以及 `text` 与 `halt.HaltLayer.text` 同源。

**时钟**：视图不安时钟（它调的是不注入 `now` 的默认路径），所以本文件的时刻一律**相对
真实现在**造——把夹具钉在某个固定日期上的话，「还没生效」那一类用例会随着真实时间越过
那个日期而静默变成「已生效」，用例还是绿的。
"""

from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils.timezone import now as tz_now
from rest_framework.test import APIClient

from apps.regime import halt
from apps.regime.models import HaltTrigger, MechanismKind, MechanismMode
from apps.regime.tests.test_halt import _declare, _switch

HALT = "/api/regime/halt/"

#: 一层在响应里的**全部**键。多一个就是多一条「前端自己拼」的路（模块 docstring）。
LAYER_KEYS = {"text", "reason", "opened_at", "expires_at"}

#: 相对真实现在的基准时刻。见模块 docstring 的「时钟」一节。
AT = tz_now()


class _Fixture(TestCase):
    def get(self):
        self.client.force_authenticate(user=self.user)
        return self.client.get(HALT)

    def setUp(self):
        # `AUTH_USER_MODEL = "authentication.User"`：email 是必填的第一个位置参数
        # （与 `test_gate_run._Fixture` 同一写法）。
        self.user = get_user_model().objects.create_user(
            email="halt_state@test.local",
            username="halt_state",
            password="pw12345",
        )
        self.client = APIClient()

    def layers(self) -> list[dict]:
        response = self.get()
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["halt_layers"]


# --------------------------------------------------------------------------- #
# 性质 1：认证
# --------------------------------------------------------------------------- #


class TestAuth(_Fixture):
    def test_anonymous_is_rejected(self):
        response = APIClient().get(HALT)

        self.assertEqual(response.status_code, 401, response.content)

    def test_the_read_surface_is_not_cacheable(self):
        """「此刻是什么状态」的读面都 `no-store`（`views._no_store`）：缓存一次就是缓存
        一个可能已经解除的停止判定。"""
        self.assertEqual(self.get()["Cache-Control"], "no-store")


# --------------------------------------------------------------------------- #
# 性质 2 / 3：开关与声明是两件事
# --------------------------------------------------------------------------- #


class TestSwitchDecides(_Fixture):
    def test_a_disabled_trigger_stays_out_of_the_read_surface(self):
        """事件熔断的声明在，但开关没打开（出厂就是 Shadow）→ 它不作数，不该出现。"""
        _declare(trigger=HaltTrigger.EVENT, opened_at=AT - timedelta(hours=2))

        self.assertEqual(self.layers(), [])

    def test_the_same_declaration_appears_once_the_switch_is_open(self):
        _declare(trigger=HaltTrigger.EVENT, opened_at=AT - timedelta(hours=2))
        _switch(MechanismKind.EVENT_BREAKER, MechanismMode.EXECUTING)

        self.assertEqual(len(self.layers()), 1)

    def test_the_blanket_layer_has_no_switch_and_always_counts(self):
        """保命档：`HALT_TRIGGER_SWITCH[BLANKET] = None`——「高波动算生效，它不由任何人
        确认」。所有开关都留在 Shadow，它照样出现。"""
        _declare(
            trigger=HaltTrigger.BLANKET,
            label="高波动",
            opened_at=AT - timedelta(hours=2),
        )

        layers = self.layers()

        self.assertEqual(len(layers), 1)
        self.assertIn("高波动", layers[0]["text"])

    def test_the_manual_layer_has_no_switch_either(self):
        _declare(
            trigger=HaltTrigger.MANUAL,
            label="手动熔断",
            opened_at=AT - timedelta(hours=2),
        )

        self.assertEqual(len(self.layers()), 1)


# --------------------------------------------------------------------------- #
# 生效期与解除：行留在表里，作不作数是求值的结果
# --------------------------------------------------------------------------- #


class TestLiveness(_Fixture):
    def test_a_closed_declaration_does_not_appear(self):
        _declare(
            trigger=HaltTrigger.BLANKET,
            opened_at=AT - timedelta(hours=3),
            closed_at=AT - timedelta(hours=1),
        )

        self.assertEqual(self.layers(), [])

    def test_a_declaration_that_has_not_opened_yet_does_not_appear(self):
        _declare(
            trigger=HaltTrigger.BLANKET,
            opened_at=AT + timedelta(hours=1),
        )

        self.assertEqual(self.layers(), [])

    def test_an_expired_declaration_does_not_appear(self):
        _declare(
            trigger=HaltTrigger.BLANKET,
            opened_at=AT - timedelta(hours=3),
            expires_at=AT - timedelta(minutes=1),
        )

        self.assertEqual(self.layers(), [])

    def test_a_window_with_no_end_stays(self):
        """`expires_at=None` 是「不定何时解除」，不是「缺数据」——它必须出现，且响应里
        那个键保持 `null`（不是空串）。"""
        _declare(
            trigger=HaltTrigger.BLANKET,
            opened_at=AT - timedelta(hours=2),
            expires_at=None,
        )

        layers = self.layers()

        self.assertEqual(len(layers), 1)
        self.assertIsNone(layers[0]["expires_at"])


# --------------------------------------------------------------------------- #
# 性质 4 与形状纪律
# --------------------------------------------------------------------------- #


class TestLayerShape(_Fixture):
    def test_all_layers_are_listed_not_just_the_first(self):
        """层是集合：两层同时生效时两层都要在（`HaltVerdict.reason` 的 docstring）。"""
        _declare(
            trigger=HaltTrigger.BLANKET,
            label="保命档那层",
            opened_at=AT - timedelta(hours=3),
        )
        _declare(
            trigger=HaltTrigger.MANUAL,
            label="手动那层",
            scope=halt.symbol_scope("DOGE/USDT"),
            opened_at=AT - timedelta(hours=2),
        )

        layers = self.layers()

        self.assertEqual(len(layers), 2)
        texts = "｜".join(layer["text"] for layer in layers)
        self.assertIn("保命档那层", texts)
        self.assertIn("手动那层", texts)

    def test_a_layer_carries_only_the_transcribed_keys(self):
        """不发 `trigger` / `scope` / `label` 的零件：`text` 已是唯一换算口，发出零件就是
        给前端留一条自己再拼一遍的路（`serializers.halt_layer_payload`）。"""
        _declare(trigger=HaltTrigger.BLANKET, opened_at=AT - timedelta(hours=2))

        layer = self.layers()[0]

        self.assertEqual(set(layer), LAYER_KEYS)

    def test_the_text_is_the_backend_sentence_not_a_recomposition(self):
        """`text` 与 `halt.HaltLayer.text` **逐字**相同，且句子里带作用域——只说触发源的话
        「拦全场」与「只拦某个品种」在页面上长得一样（`HaltLayer.text` 的 docstring）。"""
        row = _declare(
            trigger=HaltTrigger.MANUAL,
            label="手动熔断",
            scope=halt.symbol_scope("DOGE/USDT"),
            opened_at=AT - timedelta(hours=2),
        )

        layer = self.layers()[0]

        self.assertEqual(layer["text"], halt.layer_of(row).text)
        self.assertIn(halt.scope_display(row.scope), layer["text"])

    def test_opened_at_is_iso_serialized(self):
        _declare(trigger=HaltTrigger.BLANKET, opened_at=AT - timedelta(hours=2))

        layer = self.layers()[0]

        # `iso()` 的输出（不是模型列的 `str()`）：能被解析回一个带时区的时刻。
        self.assertTrue(layer["opened_at"].startswith("20"), layer["opened_at"])
        self.assertEqual(
            layer["opened_at"],
            halt.layer_of(halt.blocking_declarations()[0]).opened_at.isoformat(),
        )
