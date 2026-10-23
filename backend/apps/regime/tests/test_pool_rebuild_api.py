"""池化重算：投递端点 + 状态读 + 那个 Celery 任务（P5 子票 #47，map #40）。

`test_pool_rebuild.py` 钉的是**一代怎么落成**（判定、指纹、并发、复用）；这个文件钉的是
**一次重算怎么被投递出去、又被读回来**。池化本身算出的结论这里一行都不断言。

为什么这个文件必须存在——票面原写「状态读复用 `pool/generations/` 即可」，而那是做不到
的，这两条是本节的全部理由：

1. **`reused` 在代际表里不存在**。`rebuild_pool` 的复用分支是它**最早**的一个返回
   （指纹命中在建 `building` 行之前），那一轮不建代、不碰格子、不留任何审计行
   （ADR 0006 把这条代价写明了）。轮询代际表永远看不到它发生过。
2. **「投递成功但 worker 没跑」在代际表里也不存在**，而且它与第 1 条**逐字节同形**——
   都是「库里什么都没变」。把前者说成「重算中」，就是让一个 worker 没跑的系统看起来在
   正常工作（ADR 0006 明令要说出这个缺口）。

所以状态读必须自己答这五档，且**五档要能分开**：新代 / `reused` / `duplicate` / 真失败 /
还在跑。前四档由 `pool_rebuild.outcome_of()` 一处判定，本文件既断言那个函数本身，也断言
端点在每档上说出来的话。

八条性质：

1. **权限**：非 staff 403、匿名 401、staff 202——403 不是 404（与 `switch/` 同一条）。
2. **投递立刻返回，不等结果**：202 + `dispatched` + `task_id`，且**任务真的被投了**
   （assert `.delay` 的参数，不只看响应）。
3. **`.delay` 抛 = 503**，不是 500：通路故障与「我们自己的 bug」是两件事，页面据它决定
   「再按一次」还是「先刷新看看」。
4. **`actor_kind` 落在库里是 `"web"`，`actor_name` 是用户名**：`ActorKind` 是 `str` 的
   子类，`str(ActorKind.WEB)` 给的是 `"ActorKind.WEB"`——写错就是一张认不出来的触发方。
5. **`reused` 那一轮库里不多一代，而状态读仍说得出来**（并且必须写明「本次没有产生新
   记录」——ADR 0006 指名的那句话）。
6. **`duplicate` 显示成「并发触发」，不是「失败」**：库里那一行是 `failed`，只靠状态读
   能否把它摘出来，全看 `DUPLICATE_FAILURE_REASON` 那一处字面量有没有分叉。所以有一条
   用例直接改一个字节的原因，断言它变回「失败」——那证明这条分类真的在读那个常量。
7. **`stale_building` 成对给出布尔与分钟数，且非 `building` 时 `minutes is None`**：
   给 0 会把一次已经失败的算说成「刚跑」。
8. **还没开始 ≠ 重算中**：任务 `PENDING` 且库里没有 `building` 行时，`outcome` 是 `None`
   并带一句「等 worker」——这一档就是 ADR 0006 那个缺口的出口。

夹具复用 `test_views._Fixture`（三个策略 + 一个用户 + 能登录的客户端），池化数据由
`test_pool_rebuild._Fixture` 提供——**不另造一套世界**。
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.regime import pool_rebuild, tasks
from apps.regime.models import (
    ActorKind,
    RebuildStatus,
    RegimePoolRebuild,
)
from apps.regime.tests.test_gate_run import NOW
from apps.regime.tests.test_views import _Fixture as _ReadFixture

REBUILD = "/api/regime/pool/rebuild/"
RESULT = "/api/regime/pool/rebuild/{task_id}/"

User = get_user_model()


def _status_url(task_id: str = "task-abc") -> str:
    return RESULT.format(task_id=task_id)


def _promote_to_staff(user) -> None:
    """`is_admin` 是 `is_staff` 背后那个布尔（与 `test_switch_api` 同一条）。"""
    user.is_admin = True
    user.save(update_fields=["is_admin"])


class _StaffPoolFixture(_ReadFixture):
    """读面的夹具（三个策略 + 一个用户 + 能登录的客户端 + `make_gen`）+ staff 身份。

    用 `test_views._Fixture` 而不是 `test_pool_rebuild._Fixture`：后者只有策略，**没有
    用户、没有 `make_gen`**，而本文件同时要「能登录的客户端」与「造一代记录」。池化的
    判定逻辑由 `test_pool_rebuild.py` 自己钉，这里只用它的 `rebuild_pool` 真实路径。

    提权后必须重新认证：`force_authenticate` 缓存的是**实例**，否则权限类读到的还是那个
    `is_admin=False` 的旧对象。
    """

    def setUp(self):
        super().setUp()
        _promote_to_staff(self.user)
        self.client.force_authenticate(user=self.user)


def _fake_async_result(task_id: str = "task-abc") -> MagicMock:
    result = MagicMock()
    result.id = task_id
    return result


def _fake_celery_result(state: str, value=None) -> MagicMock:
    """一个假的 `AsyncResult`。状态与结果两端都钉住，避免用例依赖真的结果后端。"""
    result = MagicMock()
    result.state = state
    result.result = value
    return result


# --------------------------------------------------------------------------- #
# 性质 1 + 2 + 3：投递端点
# --------------------------------------------------------------------------- #


class TestDispatchPermission(TestCase):
    """投递是**写**，只对 `is_staff` 开放（ADR 0005）。"""

    def setUp(self):
        self.user = User.objects.create_user(
            email="plain@test.local", username="plain", password="pw12345"
        )
        self.staff = User.objects.create_user(
            email="staff@test.local", username="staff", password="pw12345"
        )
        _promote_to_staff(self.staff)

    def _client(self, user=None) -> APIClient:
        client = APIClient()
        if user is not None:
            client.force_authenticate(user=user)
        return client

    def test_a_plain_user_is_forbidden(self):
        """**403 不是 404**（与 `switch/` 同一条）。"""
        with patch.object(tasks.rebuild_regime_pool, "delay") as delay:
            resp = self._client(self.user).post(REBUILD, format="json")
        self.assertEqual(resp.status_code, 403, resp.content)
        delay.assert_not_called()

    def test_an_anonymous_request_is_unauthenticated(self):
        """401 而不是 403：两者对前端完全不同（重新登录 vs 你没有这个权限）。"""
        with patch.object(tasks.rebuild_regime_pool, "delay") as delay:
            resp = self._client().post(REBUILD, format="json")
        self.assertEqual(resp.status_code, 401, resp.content)
        delay.assert_not_called()

    def test_staff_may_dispatch(self):
        """反向用例：只钉「非 staff 被拒」的实现下，一个恒拒的实现也是绿的。"""
        with patch.object(
            tasks.rebuild_regime_pool, "delay", return_value=_fake_async_result()
        ) as delay:
            resp = self._client(self.staff).post(REBUILD, format="json")
        self.assertEqual(resp.status_code, 202, resp.content)
        delay.assert_called_once_with("staff")


class TestDispatchShape(_StaffPoolFixture):
    """投递成功只说明「收下了」，所以 202，且**不等结果**。"""

    def test_a_dispatch_is_202_with_a_task_id_and_no_waiting(self):
        with patch.object(
            tasks.rebuild_regime_pool,
            "delay",
            return_value=_fake_async_result("t-42"),
        ) as delay:
            resp = self.client.post(REBUILD, format="json")

        self.assertEqual(resp.status_code, 202, resp.content)
        self.assertTrue(resp.json()["dispatched"])
        # `task_id` 是这次投递的唯一凭据：`reused` 那一轮不建行，事后只能靠它找回来。
        self.assertEqual(resp.json()["task_id"], "t-42")
        self.assertIn("poll_after_seconds", resp.json())
        # 不等结果：`rebuild_pool` 一次都没被调。
        delay.assert_called_once()

    def test_the_dispatch_does_not_run_the_rebuild_inline(self):
        """投递不执行。把 `rebuild_pool` 打个桩，断言它没被碰过。"""
        with (
            patch.object(
                tasks.rebuild_regime_pool, "delay", return_value=_fake_async_result()
            ),
            patch.object(pool_rebuild, "rebuild_pool") as inline,
        ):
            self.client.post(REBUILD, format="json")

        inline.assert_not_called()

    def test_the_dispatch_is_not_cached(self):
        """这一刻的「已投递」不是一个可以重放的答案。"""
        with patch.object(
            tasks.rebuild_regime_pool, "delay", return_value=_fake_async_result()
        ):
            resp = self.client.post(REBUILD, format="json")

        self.assertEqual(resp["Cache-Control"], "no-store")

    def test_a_broken_broker_is_503_not_500(self):
        """通路故障（broker 不可用）→ 503。

        500 会让人以为「可能已经跑起来了」，于是去刷新查一条不存在的记录；503 说的是
        「没投出去，再按一次」——而那次不会撞上第一次的指纹，因为第一次没发生。
        """
        with patch.object(
            tasks.rebuild_regime_pool,
            "delay",
            side_effect=OSError("redis is down"),
        ):
            resp = self.client.post(REBUILD, format="json")

        self.assertEqual(resp.status_code, 503, resp.content)
        self.assertIn("没有投递出去", resp.json()["error"])
        # 没有留下任何一行：投递失败不该在库里假装发生过一次重算。
        self.assertEqual(RegimePoolRebuild.objects.count(), 0)


# --------------------------------------------------------------------------- #
# 性质 4：任务本身
# --------------------------------------------------------------------------- #


class TestTheTaskRecordsWhoAsked(_StaffPoolFixture):
    """任务把 `actor_kind=web` / `actor_name=<用户名>` 写进那一代。

    `ActorKind` 是 `str` 的子类，而 `str(ActorKind.WEB)` 给的是 `"ActorKind.WEB"`——写成
    那样落库就是一列认不出来的触发方，且页面上会显示成 slug。
    """

    def test_the_generation_records_the_web_actor(self):
        summary = tasks.rebuild_regime_pool.run("eazlong")

        generation = RegimePoolRebuild.objects.get(pk=summary["rebuild_id"])
        self.assertEqual(generation.actor_kind, ActorKind.WEB.value)
        self.assertEqual(generation.actor_name, "eazlong")
        self.assertEqual(generation.status, RebuildStatus.READY.value)

    def test_the_summary_carries_an_outcome(self):
        """投递方拿不到返回值，只能靠这条摘要回答「刚才那次怎么了」——所以收场在摘要里。"""
        summary = tasks.rebuild_regime_pool.run("eazlong")

        self.assertEqual(summary["outcome"], pool_rebuild.OUTCOME_NEW_GENERATION)


class TestTheTaskReusesWithoutAFakeGeneration(_StaffPoolFixture):
    """票面验收：**给定会 reuse 的输入，任务返回 `reused=True` 且库里没多一代。**

    复用是四种收场里唯一**零行**的那一种，所以它必须由任务自己说，且必须同时断言「库里
    真的没多一代」——只断言返回值的话，一个「返回了 reused 但其实又建了一代」的实现也是
    绿的，而那正是 ADR 0006 说不能发生的（同一指纹不许有两代 ready）。
    """

    def test_a_second_run_reuses_and_adds_no_generation(self):
        first = pool_rebuild.rebuild_pool()
        self.assertFalse(first["reused"])
        before = RegimePoolRebuild.objects.count()

        summary = tasks.rebuild_regime_pool.run("eazlong")

        self.assertTrue(summary["reused"])
        self.assertEqual(summary["outcome"], pool_rebuild.OUTCOME_REUSED)
        self.assertEqual(RegimePoolRebuild.objects.count(), before)
        # 复用的那一代就是原来那一代，不是新的一代。
        self.assertEqual(summary["rebuild_id"], first["rebuild_id"])


# --------------------------------------------------------------------------- #
# 性质 5 + 8：状态读 —— reused 与「还没开始」
# --------------------------------------------------------------------------- #


class TestStatusReadsReused(_StaffPoolFixture):
    """`reused` 在代际表里不存在，只有状态读说得出来。"""

    def test_a_reused_task_says_no_new_record_was_made(self):
        first = pool_rebuild.rebuild_pool()

        with patch(
            "celery_app.app.AsyncResult",
            return_value=_fake_celery_result(
                "SUCCESS",
                {
                    "rebuild_id": first["rebuild_id"],
                    "reused": True,
                    "duplicate": False,
                    "cells_changed": first["cells_changed"],
                    "cells_total": first["cells_total"],
                    "results_used": first["results_used"],
                    "candidates": first["candidates"],
                },
            ),
        ):
            body = self.client.get(_status_url()).json()

        self.assertEqual(body["outcome"]["value"], pool_rebuild.OUTCOME_REUSED)
        # ADR 0006 指名要说的那句话：不写它，就是让一个没写进审计的动作看起来像写过。
        self.assertIn("本次没有产生新记录", body["outcome"]["display"])
        # 编号是数据，词表里没有——由端点补上。
        self.assertIn(str(first["rebuild_id"]), body["outcome"]["display"])
        self.assertTrue(body["done"])

    def test_reused_is_not_confused_with_a_new_generation(self):
        """**这一条是本文件的中心**：复用的那一代在库里是 `ready`，而一次真的重算也是。

        只按 `status` 判收场，复用会被报成「新代已就绪」——而 ADR 0006 要求这两者分开显示，
        因为一个建了代、一个什么记录都没留。这里直接钉 `outcome_of` 的优先级。
        """
        self.assertEqual(
            pool_rebuild.outcome_of(reused=True, status=RebuildStatus.READY.value),
            pool_rebuild.OUTCOME_REUSED,
        )
        self.assertEqual(
            pool_rebuild.outcome_of(status=RebuildStatus.READY.value),
            pool_rebuild.OUTCOME_NEW_GENERATION,
        )


class TestStatusReadsPending(_StaffPoolFixture):
    """投递成功但 worker 还没取走 —— **不是「重算中」**。"""

    def test_a_pending_task_with_no_building_row_is_not_building(self):
        with patch(
            "celery_app.app.AsyncResult",
            return_value=_fake_celery_result("PENDING", None),
        ):
            body = self.client.get(_status_url()).json()

        # `outcome` 为空而不是 `building`：这一档是「投递了但没开始」。说成「重算中」会让
        # 一个 worker 没跑的系统看起来在正常工作（ADR 0006 明令要说出那个缺口）。
        self.assertIsNone(body["outcome"])
        self.assertFalse(body["done"])
        self.assertIsNone(body["generation"])
        # Celery 分不出「还没开始」与「结果已过期」，这条标记把那件事写出来。
        self.assertTrue(body["state_ambiguous"])
        self.assertIn("还没有 worker 取走", body["display"])
        self.assertIn("结果已经过期", body["display"])

    def test_a_pending_task_with_a_building_row_is_building(self):
        generation = self.make_gen(
            status=RebuildStatus.BUILDING.value,
            finished_at=None,
            started_at=NOW,
        )

        with patch(
            "celery_app.app.AsyncResult",
            return_value=_fake_celery_result("STARTED", None),
        ):
            body = self.client.get(_status_url()).json()

        self.assertEqual(body["outcome"]["value"], pool_rebuild.OUTCOME_BUILDING)
        self.assertEqual(body["generation"]["id"], generation.pk)
        self.assertFalse(body["done"])


# --------------------------------------------------------------------------- #
# 性质 6：duplicate —— 约束在起作用，不是故障
# --------------------------------------------------------------------------- #


class TestStatusReadsDuplicate(_StaffPoolFixture):
    """`duplicate` 在库里是 `failed`。把它摘出来全靠 `DUPLICATE_FAILURE_REASON` 一处字面量。"""

    def test_a_duplicate_row_reads_as_duplicate_not_failed(self):
        generation = self.make_gen(
            status=RebuildStatus.FAILED.value,
            failure=pool_rebuild.DUPLICATE_FAILURE_REASON,
        )

        with patch(
            "celery_app.app.AsyncResult",
            return_value=_fake_celery_result(
                "SUCCESS", {"duplicate": True, "rebuild_id": generation.pk}
            ),
        ):
            body = self.client.get(_status_url()).json()

        self.assertEqual(body["outcome"]["value"], pool_rebuild.OUTCOME_DUPLICATE)
        self.assertIn("并发", body["outcome"]["display"])
        # **措辞上不许出现「失败」**：那是 ADR 0006 明令的读法。
        self.assertNotIn("失败", body["outcome"]["display"])
        self.assertEqual(body["generation"]["id"], generation.pk)
        # 库里那一行确实是 `failed`——收场是**读**出来的，不是把状态改掉。
        self.assertEqual(
            body["generation"]["status"]["value"], RebuildStatus.FAILED.value
        )

    def test_the_duplicate_classification_reads_the_constant(self):
        """把原因改一个字节，它就必须变回「失败」。

        **这一条是给那条常量上锁**：如果实现改成再写一份字面量（或在两处各判一次），
        改这里一个字就会静默地把并发触发显示成红色故障，而这个用例会红在那之前。
        """
        generation = self.make_gen(
            status=RebuildStatus.FAILED.value,
            failure=pool_rebuild.DUPLICATE_FAILURE_REASON + "（改了一个字）",
        )

        self.assertEqual(
            pool_rebuild.outcome_of(
                status=generation.status, failure=generation.failure
            ),
            pool_rebuild.OUTCOME_FAILED,
        )

    def test_the_task_writes_the_constant_not_a_copy(self):
        """写入方也只认那个常量。逐字节相等，且**不是**第二份字面量。"""
        real_mark_ready = pool_rebuild._mark_ready

        def race(rebuild, changed, now):
            # 与 `test_pool_rebuild` 那条同一种做法：真的跑到撞约束那一步。
            from apps.regime.tests.test_pool_rebuild import make_generation

            make_generation(
                fingerprint=rebuild.input_fingerprint,
                finished_at=now,
                actor_name="对方",
            )
            return real_mark_ready(rebuild, changed, now)

        with patch.object(pool_rebuild, "_mark_ready", race):
            summary = pool_rebuild.rebuild_pool()

        self.assertTrue(summary["duplicate"])
        generation = RegimePoolRebuild.objects.get(pk=summary["rebuild_id"])
        self.assertEqual(generation.failure, pool_rebuild.DUPLICATE_FAILURE_REASON)
        self.assertEqual(
            pool_rebuild.outcome_of(
                status=generation.status, failure=generation.failure
            ),
            pool_rebuild.OUTCOME_DUPLICATE,
        )


# --------------------------------------------------------------------------- #
# 真失败
# --------------------------------------------------------------------------- #


class TestStatusReadsRealFailure(_StaffPoolFixture):
    def test_a_failed_task_surfaces_the_failure_text(self):
        generation = self.make_gen(
            status=RebuildStatus.FAILED.value,
            failure="台锯坏了：样本收集失败",
        )

        with patch(
            "celery_app.app.AsyncResult",
            return_value=_fake_celery_result("FAILURE", RuntimeError("boom")),
        ):
            body = self.client.get(_status_url()).json()

        self.assertEqual(body["outcome"]["value"], pool_rebuild.OUTCOME_FAILED)
        self.assertTrue(body["done"])
        # **失败原因优先取库里那一列**：它带业务含义，而 Celery 那条是 traceback。
        self.assertEqual(body["failure"], "台锯坏了：样本收集失败")
        # Celery 那条照原样留着，但**标成另一个键**，不冒充 failure。
        self.assertIn("boom", body["celery_error"])
        self.assertEqual(body["generation"]["id"], generation.pk)


# --------------------------------------------------------------------------- #
# 性质 7：stale_building 成对给出布尔与分钟数
# --------------------------------------------------------------------------- #


class TestStaleBuilding(_StaffPoolFixture):
    """`building` 超过阈值 → 布尔 + 分钟数，且**阈值本身也透出**（前端不另写一份 N）。

    **夹具的 `NOW` 是 2026-06-01，而端点的时钟是真实的**——所以凡是走端点的用例，`started_at`
    必须以**真实现在**为基准造，否则「跑了两小时还是三年前」这种事会被误判。纯函数那几条
    显式传 `now=NOW`，两边同源，可以继续用夹具的时钟。
    """

    def _building(self, ago: timedelta, *, base=None) -> RegimePoolRebuild:
        moment = base if base is not None else NOW
        return self.make_gen(
            status=RebuildStatus.BUILDING.value,
            finished_at=None,
            started_at=moment - ago,
        )

    def test_a_fresh_building_row_is_not_stale(self):
        generation = self._building(timedelta(minutes=2))

        payload = (
            pool_rebuild.is_stale_building(generation, now=NOW),
            pool_rebuild.building_minutes(generation, now=NOW),
        )

        self.assertFalse(payload[0])
        self.assertEqual(payload[1], 2)

    def test_a_building_row_past_the_threshold_is_stale(self):
        generation = self._building(
            pool_rebuild.STALE_BUILDING_AFTER + timedelta(minutes=1)
        )

        self.assertTrue(pool_rebuild.is_stale_building(generation, now=NOW))
        minutes = pool_rebuild.building_minutes(generation, now=NOW)
        self.assertGreater(minutes, 30)

    def test_a_finished_generation_is_never_stale(self):
        """`ready` / `failed` 停多久都不是「卡住」——它们已经收场了。"""
        for status in (RebuildStatus.READY.value, RebuildStatus.FAILED.value):
            with self.subTest(status=status):
                generation = self.make_gen(
                    status=status, started_at=NOW - timedelta(days=9)
                )
                self.assertFalse(pool_rebuild.is_stale_building(generation, now=NOW))

    def test_the_status_read_pairs_boolean_with_minutes_and_threshold(self):
        """三样一起给：布尔 + 分钟数 + 阈值。**少任何一样，前端都得自己算一遍 N。**

        走端点，所以 `started_at` 以真实现在为基准造（见类 docstring）。
        """
        from django.utils import timezone

        real_now = timezone.now()
        self._building(
            pool_rebuild.STALE_BUILDING_AFTER + timedelta(minutes=5), base=real_now
        )

        with patch(
            "celery_app.app.AsyncResult",
            return_value=_fake_celery_result("STARTED", None),
        ):
            body = self.client.get(_status_url()).json()

        stale = body["stale_building"]
        self.assertEqual(set(stale), {"stale", "minutes", "threshold_minutes"})
        self.assertTrue(stale["stale"])
        # 35 分钟是「阈值 30 + 留的 5」；不断言绝对值而断言它确实在阈值之上，
        # 是因为真实时钟会走掉几十毫秒——但分钟数是向下取整，35 是稳的。
        self.assertEqual(stale["minutes"], 35)
        self.assertEqual(stale["threshold_minutes"], 30)
        # 那句话里带着后端给的 N（30），不是前端硬编码的另一份。
        self.assertIn("30 分钟", body["display"])
        self.assertIn("检查 worker", body["display"])

    def test_a_non_building_generation_reports_no_minutes(self):
        """**`minutes` 是 `None`，不是 0**：给 0 会把一次已经失败的算说成「刚跑」。"""
        generation = self.make_gen(status=RebuildStatus.READY.value, started_at=NOW)

        from apps.regime import serializers as ser

        stale = ser.stale_building_payload(generation)
        self.assertFalse(stale["stale"])
        self.assertIsNone(stale["minutes"])


# --------------------------------------------------------------------------- #
# 性质 9：结果过期不该变成 404
# --------------------------------------------------------------------------- #


class TestExpiredResultIsNotA404(_StaffPoolFixture):
    """`CELERY_RESULT_EXPIRES` 一天之后 Celery 不再认这个 id。**那次重算真的发生过。**"""

    def test_an_unknown_task_id_says_so_without_denying_it_happened(self):
        with patch(
            "celery_app.app.AsyncResult",
            return_value=_fake_celery_result("PENDING", None),
        ):
            resp = self.client.get(_status_url("long-gone"))

        # 不是 404：手上那个 id 没错，回「没有这个任务」是在否认一件真事。
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertEqual(body["task_id"], "long-gone")
        self.assertFalse(body["done"])


# --------------------------------------------------------------------------- #
# 收场词表本身
# --------------------------------------------------------------------------- #


class TestTheOutcomeVocabulary(TestCase):
    """五种收场、各有一句展示名。**加一种忘了给说法**会让前端显示 slug。"""

    def test_every_outcome_has_a_display(self):
        self.assertEqual(len(pool_rebuild.OUTCOMES), 5)
        self.assertEqual(set(pool_rebuild.OUTCOME_DISPLAY), set(pool_rebuild.OUTCOMES))

    def test_the_two_neutral_outcomes_do_not_say_failed(self):
        """`reused` 与 `duplicate` 都不是故障——它们在页面上是中性色，措辞也不能说「失败」。"""
        for outcome in (
            pool_rebuild.OUTCOME_REUSED,
            pool_rebuild.OUTCOME_DUPLICATE,
        ):
            with self.subTest(outcome=outcome):
                self.assertNotIn("失败", pool_rebuild.OUTCOME_DISPLAY[outcome])

    def test_outcome_of_fails_closed_on_an_unknown_status(self):
        """认不出来的状态按失败收：宁可显示「去查」也不显示「好了」。"""
        self.assertEqual(
            pool_rebuild.outcome_of(status="something-new"),
            pool_rebuild.OUTCOME_FAILED,
        )
