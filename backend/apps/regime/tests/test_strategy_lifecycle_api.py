"""策略启停写入口：`/regime` 页面上的「删除策略」与「恢复启用」。

（`GET api/regime/strategies/<id>/`、`POST …/retire/`、`POST …/restore/`）

`test_cleanup_ghost_strategies` 钉的是那条管理命令（同一条 `is_active` 的另一位写方）；
这个文件钉的是**人从页面上按下**这一条通路。八条性质，每一条坏了都不报警、只出错：

1. **权限的四种身份各归各的**：匿名 401、登录非 staff 读得到（200）写不了（403）、staff
   读写都行。403 与 404 不是一回事（前者「你没有这个权限」，后者「这条策略不存在」），
   混起来会让人反复刷新一个永远不出现的 id。
2. **「删除」不是删数据**：退役之后回测、切片、池化格、停用决策、豁免一条不少。
   断言的是**行数不变**，不是「接口回了 200」。
3. **退役不停会话**：`LiveSession` 一个字段都不动（交易链路不看 `is_active`，所以「删除」
   的净效果是放开而不是停止——这条性质是页面上那句话的事实基础）。
4. **留痕四件事**：`actor_kind == "web"`、`actor_name == 用户名`、`from/to` 是真读出来的、
   `reason` 与本次响应同源（客户端不能手写）。
5. **本来就是这一档 → 200 + `changed: false`，且没有第二条流水**：只断言状态码挡不住它，
   核心是流水行数不变。
6. **确认页两个方向一次都给**，且正文里那两句话在（「不删除任何数据」/「恢复启用不是
   豁免」）——它们是用户唯一能看到「删除到底做了什么」的地方。
7. **写入口不碰别的策略**：同一批里其它行一个字段都不动。
8. **池化读面带上了在册状态**：退役之后 `GET pool/` 的那一行是「已退役」+ 最近一次流水，
   页面不必为此再打一次请求。

夹具直接复用 `test_views._Fixture`（三个策略 + 一个用户 + 客户端），与 `test_switch_api`
同一条取向：两处各造一套世界，迟早会出现「同一个动作在两个文件里写出两种行」。
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.regime import halt
from apps.regime import serializers as ser
from apps.regime import strategy_lifecycle as life
from apps.regime.models import (
    ActorKind,
    DeactivationDecision,
    DeactivationExemption,
    HaltDeclaration,
    HaltTrigger,
    RebuildStatus,
    StrategyLifecycleChange,
)
from apps.regime.tests.test_gate_run import days
from apps.regime.tests.test_views import _Fixture as _ViewsFixture
from apps.trading.models import LiveSession, Strategy

PAGE = "/api/regime/strategies/{}/"
RETIRE = "/api/regime/strategies/{}/retire/"
RESTORE = "/api/regime/strategies/{}/restore/"


def _promote_to_staff(user) -> None:
    """把一个 `test_gate_run` 造出来的用户提成 staff（与 `test_switch_api` 同一手法）。

    `is_staff` 是 `is_admin` 的别名，而 `UserManager.create_user` 不接这个参数。
    """
    user.is_admin = True
    user.save(update_fields=["is_admin"])


class _Fixture(_ViewsFixture):
    """在 `test_views._Fixture` 上加：staff 身份、以及三样「退役会影响到的引用」。"""

    def setUp(self):
        super().setUp()
        _promote_to_staff(self.user)
        # `force_authenticate` 缓存的是**实例**，升级之后必须再调一次，否则权限类读到的
        # 还是那个 `is_admin=False` 的旧对象。
        self.client.force_authenticate(user=self.user)

    # --- 与退役相关的那几样引用 -------------------------------------------- #

    def session(self, strategy, *, status: str = "running", mode: str = "live"):
        """一条活跃会话。``mode`` 默认 live（无持仓/持仓未知那类闸门都不看 `is_active`）。"""
        return LiveSession.objects.create(
            user=self.user,
            strategy=strategy,
            symbol="BTC/USDT",
            mode=mode,
            status=status,
            initial_capital=Decimal("10000.00"),
        )

    def declaration(self, strategy, *, closed: bool = False):
        """一条 `strategy:<id>` 停用声明（退役之后机制该放开的那一行）。"""
        return HaltDeclaration.objects.create(
            trigger=HaltTrigger.DEACTIVATION.value,
            scope=halt.strategy_scope(strategy.pk),
            label=f"策略停用：{strategy.name}",
            opened_at=days(-1),
            closed_at=days(-1) + timedelta(hours=1) if closed else None,
            closed_reason="gate_closed" if closed else "",
            reason="测试：该策略在当前阶段不适用",
            actor_kind=ActorKind.TASK.value,
            actor_name="regime-gate-sync",
        )

    def decision(self, strategy):
        return DeactivationDecision.objects.create(
            strategy=strategy,
            regime="downtrend",
            status="suggested",
            evidence={"trades": 44},
            pool_rebuild_id=1,
            first_decided_at=days(-2),
            last_confirmed_at=days(-1),
        )

    # --- 三个端点 ---------------------------------------------------------- #

    def page(self, strategy):
        return self.client.get(PAGE.format(strategy.pk))

    def retire(self, strategy):
        return self.client.post(RETIRE.format(strategy.pk), {}, format="json")

    def restore(self, strategy):
        return self.client.post(RESTORE.format(strategy.pk), {}, format="json")

    def reload(self, strategy) -> Strategy:
        return Strategy.objects.get(pk=strategy.pk)


# --------------------------------------------------------------------------- #
# 性质 1：权限
# --------------------------------------------------------------------------- #


class TestPermissions(_Fixture):
    def test_anonymous_is_401_everywhere(self):
        """匿名一律 401（`IsStaffUser` 含认证；DRF 因为声明了 `authenticate_header` 给
        401 而不是 403）。读那一条同样是 401——它只对**登录**用户开放。"""
        anonymous = APIClient()
        for url in (
            PAGE.format(self.alpha.pk),
            RETIRE.format(self.alpha.pk),
            RESTORE.format(self.alpha.pk),
        ):
            with self.subTest(method="GET", url=url):
                self.assertEqual(anonymous.get(url).status_code, 401)
        for url in (RETIRE.format(self.alpha.pk), RESTORE.format(self.alpha.pk)):
            with self.subTest(method="POST", url=url):
                resp = anonymous.post(url, {}, format="json")
                self.assertEqual(resp.status_code, 401, resp.content)

    def test_a_logged_in_non_staff_user_can_read_but_not_write(self):
        """**读对所有登录用户开放，写要 `is_staff`**（ADR 0005）。403 不是 404：这不是
        「不属于你的资源」，是明确的权限拒绝——伪装成 404 会让人去刷新一个存在的 id。"""
        plain = get_user_model().objects.create_user(
            email="plain@test.local", username="plain", password="pw12345"
        )
        self.client.force_authenticate(user=plain)

        self.assertEqual(self.page(self.alpha).status_code, 200)
        for url in (RETIRE, RESTORE):
            with self.subTest(url=url):
                resp = self.client.post(url.format(self.alpha.pk), {}, format="json")
                self.assertEqual(resp.status_code, 403, resp.content)

        # 一次没写库：非 staff 的 403 连一条流水都不该留下。
        self.assertEqual(StrategyLifecycleChange.objects.count(), 0)
        self.assertTrue(self.reload(self.alpha).is_active)

    def test_an_unknown_id_is_a_404_with_the_error_shape(self):
        import uuid as uuid_module

        missing = uuid_module.uuid4()
        resp = self.client.get(PAGE.format(missing))

        self.assertEqual(resp.status_code, 404, resp.content)
        self.assertIn(str(missing), resp.json()["error"])

    def test_a_non_uuid_path_is_a_404_from_the_router(self):
        """路径段不是 uuid 时路由就匹配不上（没有 `<uuid:…>` 这一条）。这不是视图的 400：
        请求的**路径结构**就不存在，改写成一个 400 会让客户端去改参数而不是改路径。"""
        resp = self.client.get("/api/regime/strategies/not-a-uuid/")

        self.assertEqual(resp.status_code, 404)


# --------------------------------------------------------------------------- #
# 性质 6：确认页
# --------------------------------------------------------------------------- #


class TestTheConfirmationPage(_Fixture):
    """`GET` 那一页是给人做决定用的：两个方向一次都给，**四个数都是此刻库里的**。"""

    def setUp(self):
        super().setUp()
        self.session(self.alpha)
        self.session(self.alpha, status="paused")
        self.session(self.alpha, status="stopped")  # 不算活跃
        self.declaration(self.alpha)
        self.declaration(self.alpha, closed=True)  # 已解除的也不算
        self.decision(self.alpha)
        self.generation = self.make_gen(
            cells=((self.alpha, "downtrend", {}), (self.alpha, "range", {})),
        )

    def test_both_directions_come_back_at_once(self):
        body = self.page(self.alpha).json()

        self.assertEqual(body["strategy"]["name"], "AlphaStem")
        self.assertEqual(body["strategy"]["lifecycle"]["value"], life.ACTIVE)
        self.assertEqual(
            body["strategy"]["lifecycle"]["display"],
            life.LIFECYCLE_DISPLAY[life.ACTIVE],
        )
        for key, to_active in (("retire", False), ("restore", True)):
            with self.subTest(key=key):
                self.assertEqual(
                    body[key]["action"]["display"], life.ACTION_DISPLAY[to_active]
                )
        # 此刻在册 ⇒ 退役可用、恢复不可用。两个方向都给的是**同一件事实的两个说法**，
        # 前端据此决定哪个按钮可点，不必自己再判一次 `is_active`。
        self.assertTrue(body["retire"]["available"])
        self.assertFalse(body["restore"]["available"])

    def test_the_four_counts_are_the_live_facts(self):
        """活跃会话只数 `ACTIVE_STATUSES`、声明只数还在生效的、格子只数**当前代**的。"""
        counts = self.page(self.alpha).json()["retire"]["counts"]

        self.assertEqual(counts["active_sessions"], 2)
        self.assertEqual(counts["declarations"], 1)
        self.assertEqual(counts["decisions"], 1)
        self.assertEqual(counts["pool_cells"], 2)

    def test_the_retire_body_says_what_it_does_not_do(self):
        """正文由后端渲染。它必须说三件事：不删数据、不停会话、解除声明之后可以重新开仓
        ——第三件事与「删除」的直觉相反，不说就是一次静默的放开。"""
        body = self.page(self.alpha).json()["retire"]["body"]

        self.assertIn("不删除任何数据", body)
        self.assertIn("正在跑的会话不会被停", body)
        self.assertIn("可以重新开新仓", body)

    def test_the_restore_body_says_it_is_not_an_exemption(self):
        """先把策略退役，那一侧的正文才是「将要发生什么」——在册策略的恢复正文是
        「本来就是启用状态」那句（见下一条）。"""
        self.retire(self.alpha)

        body = self.page(self.alpha).json()["restore"]["body"]

        self.assertIn("不是豁免", body)
        self.assertIn("再次被停", body)

    def test_an_unavailable_direction_shows_the_already_sentence_not_a_checklist(self):
        """本来就是这一档 ⇒ 正文就是那句话，不是一份不会发生的动作的说明。

        把一份不会发生的动作摆给人看，与 `reused` 被显示成「已完成」是同一种谎（ADR 0006）。
        """
        body = self.page(self.alpha).json()

        self.assertFalse(body["restore"]["available"])
        self.assertEqual(
            body["restore"]["body"],
            life.already_display("AlphaStem", to_active=True),
        )
        self.assertNotIn("不是豁免", body["restore"]["body"])

    def test_the_page_is_no_store(self):
        self.assertEqual(self.page(self.alpha)["Cache-Control"], "no-store")

    def test_the_summary_is_rendered_server_side_and_matches_the_record(self):
        """落库的 `reason` 就是这一页的 `summary`（同一次求值）。"""
        summary = self.page(self.alpha).json()["retire"]["summary"]

        self.assertIn("AlphaStem", summary)
        self.assertIn("活跃会话 2", summary)


# --------------------------------------------------------------------------- #
# 性质 2/3/4/5/7：退役
# --------------------------------------------------------------------------- #


class TestRetire(_Fixture):
    def setUp(self):
        super().setUp()
        self.session_row = self.session(self.alpha)
        self.declaration_row = self.declaration(self.alpha)
        self.decision_row = self.decision(self.alpha)
        self.generation = self.make_gen(
            cells=((self.alpha, "downtrend", {}), (self.beta, "range", {})),
        )

    def test_it_flips_the_boolean_and_answers_with_the_receipt(self):
        resp = self.retire(self.alpha)

        self.assertEqual(resp.status_code, 200, resp.content)
        payload = resp.json()
        self.assertTrue(payload["changed"])
        self.assertFalse(self.reload(self.alpha).is_active)
        self.assertEqual(
            payload["display"], life.receipt_display("AlphaStem", to_active=False)
        )
        self.assertIsNotNone(payload["at"])
        # 回执里那一档已经是**翻转之后**的：前端据此立刻换掉按钮态，不必再打一次 GET。
        self.assertEqual(payload["strategy"]["lifecycle"]["value"], life.RETIRED)
        self.assertEqual(payload["action"]["display"], life.ACTION_DISPLAY[False])

    def test_the_row_records_the_four_audit_facts(self):
        """什么时候、谁、从哪档到哪档、为什么。**四个都要**——少一个就答不出「这条策略
        为什么不见了」。"""
        payload = self.retire(self.alpha).json()

        row = StrategyLifecycleChange.objects.get()
        self.assertEqual(row.strategy_id, self.alpha.pk)
        self.assertEqual(row.actor_kind, ActorKind.WEB.value)
        self.assertEqual(row.actor_name, self.user.username)
        self.assertTrue(row.from_active)
        self.assertFalse(row.to_active)
        self.assertEqual(row.reason, payload["reason"])
        self.assertEqual(ser.iso(row.at), payload["at"])

    def test_a_client_cannot_dictate_the_reason(self):
        """请求体里塞一个 reason 一律忽略：落库那句话必须与本次求值同源，否则同一条流水
        里会出现两种性质不同的字符串（与 `switch` 那条纪律同一取向）。"""
        fake = "客户端编的理由：我不想要它了"
        resp = self.client.post(
            RETIRE.format(self.alpha.pk), {"reason": fake}, format="json"
        )

        self.assertEqual(resp.status_code, 200, resp.content)
        row = StrategyLifecycleChange.objects.get()
        self.assertNotIn(fake, row.reason)
        self.assertNotIn(fake, resp.json()["reason"])

    def test_it_deletes_no_data(self):
        """**「删除」不是删数据**：回测/切片/池化格/停用决策/豁免一条不少。"""
        exemption = DeactivationExemption.objects.create(
            strategy=self.alpha,
            regime="downtrend",
            granted_at=days(-1),
            expires_at=days(9),
            granted_by="ops",
        )
        cells = self.generation.cells.count()
        decisions = DeactivationDecision.objects.count()

        self.retire(self.alpha)

        self.assertEqual(self.generation.cells.count(), cells)
        self.assertEqual(DeactivationDecision.objects.count(), decisions)
        self.assertTrue(DeactivationExemption.objects.filter(pk=exemption.pk).exists())
        self.assertTrue(Strategy.objects.filter(pk=self.alpha.pk).exists())

    def test_it_does_not_stop_any_session(self):
        """交易链路不看 `is_active`——退役一个字段都不动 `LiveSession`。这条性质是页面上
        「退役不会停止会话」那句话的事实基础：说了而做不到，就是一句安慰。"""
        self.retire(self.alpha)

        row = LiveSession.objects.get(pk=self.session_row.pk)
        self.assertEqual(row.status, "running")
        self.assertEqual(row.mode, "live")

    def test_it_does_not_touch_other_strategies(self):
        self.retire(self.alpha)

        self.assertTrue(self.reload(self.beta).is_active)
        self.assertTrue(self.reload(self.gamma).is_active)
        self.assertEqual(
            StrategyLifecycleChange.objects.filter(strategy=self.beta).count(), 0
        )

    def test_retiring_twice_is_a_200_without_a_second_row(self):
        """**本来就是这一档**：200 + `changed:false`，且没有第二条流水。用 400 回报会让
        前端显示成失败，而人看到失败只会再点一次。"""
        self.retire(self.alpha)
        before = StrategyLifecycleChange.objects.count()

        resp = self.retire(self.alpha)

        self.assertEqual(resp.status_code, 200, resp.content)
        payload = resp.json()
        self.assertFalse(payload["changed"])
        self.assertIsNone(payload["at"])
        self.assertIn("本来就是", payload["display"])
        self.assertEqual(StrategyLifecycleChange.objects.count(), before)

    def test_the_pool_read_marks_the_row_retired(self):
        """退役之后池化表那一行要带得出「已退役」与最近一次流水——否则页面要点一次
        才看得出来，而这正是「删除」看起来没生效的那个形状。"""
        self.retire(self.alpha)

        body = self.client.get("/api/regime/pool/").json()
        cells = [
            cell for cell in body["cells"] if cell["strategy"]["name"] == "AlphaStem"
        ]

        self.assertTrue(cells, "这一代里应当还有 AlphaStem 的格子（记录保留）")
        for cell in cells:
            with self.subTest(key=cell["key"]):
                self.assertEqual(cell["strategy"]["lifecycle"]["value"], life.RETIRED)
                self.assertEqual(
                    cell["strategy"]["lifecycle"]["display"],
                    life.LIFECYCLE_DISPLAY[life.RETIRED],
                )
                change = cell["strategy"]["latest_change"]
                self.assertIsNotNone(change)
                self.assertEqual(change["actor_name"], self.user.username)
                self.assertEqual(change["to_active"]["value"], life.RETIRED)
                self.assertIn("AlphaStem", change["reason"])

    def test_an_untouched_strategy_has_no_change_row_in_the_pool_read(self):
        body = self.client.get("/api/regime/pool/").json()
        cells = [
            cell for cell in body["cells"] if cell["strategy"]["name"] == "BetaStem"
        ]

        self.assertTrue(cells)
        for cell in cells:
            with self.subTest(key=cell["key"]):
                self.assertEqual(cell["strategy"]["lifecycle"]["value"], life.ACTIVE)
                self.assertIsNone(cell["strategy"]["latest_change"])


# --------------------------------------------------------------------------- #
# 恢复启用
# --------------------------------------------------------------------------- #


class TestRestore(_Fixture):
    def test_it_puts_the_strategy_back_and_records_the_other_direction(self):
        self.retire(self.alpha)

        resp = self.restore(self.alpha)

        self.assertEqual(resp.status_code, 200, resp.content)
        payload = resp.json()
        self.assertTrue(payload["changed"])
        self.assertTrue(self.reload(self.alpha).is_active)
        row = StrategyLifecycleChange.objects.order_by("-id").first()
        self.assertFalse(row.from_active)
        self.assertTrue(row.to_active)
        self.assertEqual(row.actor_kind, ActorKind.WEB.value)
        self.assertEqual(
            payload["display"], life.receipt_display("AlphaStem", to_active=True)
        )

    def test_restoring_twice_is_a_200_without_a_second_row(self):
        self.retire(self.alpha)
        self.restore(self.alpha)
        before = StrategyLifecycleChange.objects.count()

        resp = self.restore(self.alpha)

        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertFalse(resp.json()["changed"])
        self.assertEqual(StrategyLifecycleChange.objects.count(), before)

    def test_restoring_never_writes_an_exemption(self):
        """两条通路不是一回事：启停只翻 `is_active`，豁免是「人工恢复」那条通路。
        写成同一条，等于让「放回被管集合」偷偷变成「10 天不再被停」。"""
        self.retire(self.alpha)

        self.restore(self.alpha)

        self.assertEqual(DeactivationExemption.objects.count(), 0)

    def test_restoring_when_never_retired_writes_nothing(self):
        """从来在册的策略按恢复 → 200 + `changed:false`，且没有流水。**这是 fail-closed
        的方向**：宁可什么都不写，也不写一条描述不存在翻转的流水。"""
        resp = self.restore(self.beta)

        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertFalse(resp.json()["changed"])
        self.assertEqual(StrategyLifecycleChange.objects.count(), 0)
        self.assertTrue(self.reload(self.beta).is_active)


# --------------------------------------------------------------------------- #
# 模块级的判据（不经 HTTP）
# --------------------------------------------------------------------------- #


class TestTheModuleJudgements(TestCase):
    def test_a_blank_actor_is_refused(self):
        """没有触发方的翻转无法排查（与 `flip_regime_gate` 同一条）。

        用 `get_or_create` 造一条策略：`Strategy.is_active` 默认是 `False`，所以顺带钉住
        「默认不在册」这件事——恢复一个默认行是合法动作（它在册这件事是历史，不是默认值）。
        """
        strategy, _ = Strategy.objects.get_or_create(
            name="LonelyStem", defaults={"code_path": "/t/lonely.py"}
        )

        with self.assertRaises(ValueError):
            life.flip(
                strategy,
                to_active=False,
                actor_kind=ActorKind.WEB,
                actor_name="   ",
            )
        self.assertEqual(StrategyLifecycleChange.objects.count(), 0)

    def test_counts_ignore_closed_declarations_and_inactive_sessions(self):
        """ "此刻还作数"的三个计数：已解除的声明、已结束的会话都不算。"""
        strategy = Strategy.objects.create(name="CountStem", code_path="/t/c.py")
        HaltDeclaration.objects.create(
            trigger=HaltTrigger.DEACTIVATION.value,
            scope=halt.strategy_scope(strategy.pk),
            label="已解除",
            opened_at=days(-3),
            closed_at=days(-2),
            reason="x",
            actor_kind=ActorKind.TASK.value,
            actor_name="t",
        )

        counts = life.counts_for(strategy.pk)

        self.assertEqual(counts.declarations, 0)
        self.assertEqual(counts.active_sessions, 0)
        self.assertEqual(counts.pool_cells, 0)

    def test_an_unreadable_state_falls_back_to_retired(self):
        """认不出来的值当「已退役」：脏库的方向应当是「不被机制管」，不是「在册」。"""
        self.assertEqual(life.lifecycle_state(None), life.RETIRED)
        self.assertEqual(life.lifecycle_state(False), life.RETIRED)
        self.assertEqual(life.lifecycle_state(True), life.ACTIVE)

    def test_the_page_of_a_retired_strategy_offers_restore_only(self):
        strategy = Strategy.objects.create(
            name="RetiredStem", code_path="/t/r.py", is_active=False
        )

        page = life.page(strategy, to_active=False)
        restore = life.page(strategy, to_active=True)

        self.assertFalse(page.available)
        self.assertEqual(
            page.body, life.already_display("RetiredStem", to_active=False)
        )
        self.assertIn("本来就是", page.body)
        self.assertTrue(restore.available)


# --------------------------------------------------------------------------- #
# 与既有读面的接线
# --------------------------------------------------------------------------- #


class TestThePoolPayloadCarriesTheState(_Fixture):
    """读面契约：格子的 `strategy` 多出 `lifecycle` 与 `latest_change` 两个键。"""

    def setUp(self):
        super().setUp()
        self.generation = self.make_gen(
            cells=(
                (self.alpha, "downtrend", {"state": "unfit"}),
                (self.beta, "range", {"state": "fit"}),
            ),
            status=RebuildStatus.READY.value,
        )

    def test_every_cell_carries_a_paired_lifecycle(self):
        body = self.client.get("/api/regime/pool/").json()

        for cell in body["cells"]:
            with self.subTest(key=cell["key"]):
                lifecycle = cell["strategy"]["lifecycle"]
                self.assertEqual(set(lifecycle), {"value", "display"})
                self.assertTrue(lifecycle["display"])
                self.assertIn("latest_change", cell["strategy"])

    def test_the_display_comes_from_the_lifecycle_table(self):
        body = self.client.get("/api/regime/pool/").json()
        first = body["cells"][0]

        self.assertEqual(
            first["strategy"]["lifecycle"]["display"],
            life.LIFECYCLE_DISPLAY[life.lifecycle_state(True)],
        )

    def test_a_retired_strategy_is_not_dropped_from_the_generation(self):
        """**记录保留**：退役不把格子从这一代里抹掉（池化表是那一代的快照）。
        把它抹掉等于改写历史，而「为什么当初停在它身上」正要靠这份历史回答。"""
        self.retire(self.alpha)

        body = self.client.get("/api/regime/pool/").json()

        self.assertEqual(
            sorted(cell["strategy"]["name"] for cell in body["cells"]),
            ["AlphaStem", "BetaStem"],
        )
        alpha = [
            cell for cell in body["cells"] if cell["strategy"]["name"] == "AlphaStem"
        ]
        self.assertEqual(alpha[0]["strategy"]["lifecycle"]["value"], life.RETIRED)


class TestALostRace(TestCase):
    """读到「该翻」之后、写下去之前被人抢先：**不写第二条流水**，且回执说的是库里的真实档位。

    这是条件更新（`filter(pk=…, is_active=not to_active).update(…)`）唯一要买的东西。
    失败方向是安全的：宁可少写一条流水，也不写一条描述不存在翻转的流水。
    """

    def test_a_lost_race_writes_nothing_and_reports_the_real_state(self):
        from unittest.mock import patch

        from apps.regime import strategy_lifecycle as life_module

        strategy = Strategy.objects.create(
            name="RaceStem", code_path="/t/race.py", is_active=True
        )
        real_page = life_module.page
        calls = {"n": 0}

        def stale_first_page(target, *, to_active):
            """第一次求值说是「该翻」（它看到的是一份已经过期的世界），之后按真的算。"""
            calls["n"] += 1
            if calls["n"] == 1:
                return life_module.Page(
                    strategy_id=target.pk,
                    name=str(target.name),
                    is_active=True,
                    to_active=True,
                    counts=life_module.Counts(0, 0, 0, 0),
                    available=True,
                )
            return real_page(target, to_active=to_active)

        with patch.object(life_module, "page", side_effect=stale_first_page):
            outcome = life_module.flip(
                strategy,
                to_active=True,
                actor_kind=ActorKind.WEB,
                actor_name="ops",
            )

        self.assertIsNone(outcome.row, "抢输了就不该写流水")
        self.assertFalse(outcome.changed)
        self.assertEqual(StrategyLifecycleChange.objects.count(), 0)
        # 回执里的那一档是**库里真实的**（True），不是本次的目标值假装出来的
        self.assertTrue(strategy.is_active)
        self.assertFalse(outcome.page.available)
