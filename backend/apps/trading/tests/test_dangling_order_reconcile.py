"""悬挂订单反查（扫单链路）——第①段强制前置的主测试面。

CONTEXT.md 第 137 条要的是三件事，本文件逐条钉住：

1. 「无交易所 ID 则跳过」被修掉（`_sync_active_orders` 不再永久忽略这批行）；
2. 三分支口径：确认不存在 → ``failed``；不可达/意外 → ``unknown``（**不是 failed**）；
   命中 → 只补 ``exchange_order_id``，成交与盈亏仍归 ``_apply_fill`` 唯一所有；
3. 兜底扫描是 beat 任务，且框架活跃时让位给进程内那一轮。

这里刻意全部用 mock ORM（与 ``test_fill_sync.py`` 同一风格），因为要断言的是
**写进库里的那个 status 到底是哪一档**——这是「谎报」与「如实」的分界。
"""

from __future__ import annotations

import asyncio
import unittest
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from django.utils import timezone

from apps.trading.adapters.base import (
    OrderLookupUnavailableError,
    OrderLookupUnsupportedError,
)
from apps.trading.models import Order
from apps.trading.pending_reconcile import (
    PLACEMENT_GRACE_SECONDS,
    DanglingOutcome,
    is_beyond_placement_window,
    reconcile_dangling_order,
    sweep_dangling_orders,
)


def _order(**overrides):
    base = {
        "id": "11111111-1111-1111-1111-111111111111",
        "request_id": "22222222-2222-2222-2222-222222222222",
        "user_id": "user-1",
        "symbol": "DOGE/USDT",
        "side": "buy",
        "quantity": Decimal("100"),
        "status": "pending",
        "exchange_order_id": "",
        "filled_quantity": Decimal("0"),
        "avg_fill_price": None,
        "realized_pnl": None,
        "error_message": "",
        "created_at": timezone.now() - timedelta(seconds=PLACEMENT_GRACE_SECONDS + 60),
        "exchange_account_id": "acc-1",
        "exchange_account": SimpleNamespace(id="acc-1", exchange="binance"),
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class _FakeAdapter:
    """只实现反查的假适配器：记录每次反查用的 clientOrderId。"""

    def __init__(self, tag="acc-1", result=None, error=None):
        self.tag = tag
        self.result = result
        self.error = error
        self.queries: list[str] = []
        self.disconnected = False

    async def find_order_by_client_id(self, client_order_id, symbol):
        self.queries.append(client_order_id)
        if self.error is not None:
            raise self.error
        return self.result

    async def disconnect(self):
        self.disconnected = True


class _MarkCapture:
    """捕获 ``Order.objects.filter(...).update(...)`` 的参数。"""

    def __init__(self, orders):
        self.updates: list[dict] = []
        self._orders = orders

    def __enter__(self):
        self._patch = patch("apps.trading.models.Order.objects")
        manager = self._patch.start()
        self.manager = manager
        manager.filter.return_value.select_related.return_value = self._orders
        manager.filter.return_value.update.side_effect = self._record
        return self

    @property
    def query_filter(self) -> dict:
        """`Order.objects.filter(...)` 的关键字参数（ORM 被 mock 后，取数范围只能这样断言）。"""
        return dict(self.manager.filter.call_args.kwargs)

    def _record(self, **kwargs):
        self.updates.append(kwargs)
        return 1

    def __exit__(self, *exc):
        self._patch.stop()
        return False

    @property
    def last(self) -> dict:
        return self.updates[-1] if self.updates else {}


class TestReconcileBranch(unittest.TestCase):
    """`reconcile_dangling_order` 的三分支口径（第 137 条的核心）。"""

    def _run(self, adapter, order=None):
        order = order or _order()
        with patch(
            "apps.trading.pending_reconcile.alert_order_anomaly",
            new=AsyncMock(return_value=True),
        ) as alert:
            with _MarkCapture([]) as captures:
                outcome = asyncio.run(reconcile_dangling_order(order, adapter))
        return order, outcome, alert, captures

    def test_exchange_confirms_absent_marks_failed_and_alerts(self):
        """交易所确定没有这张单 → failed + 告警（这是唯一允许写 failed 的分支）"""
        order, outcome, alert, captures = self._run(
            _FakeAdapter(result=None)
        )

        self.assertEqual(outcome.kind, "failed")
        self.assertEqual(captures.last.get("status"), "failed")
        self.assertTrue(outcome.needs_alert)
        alert.assert_awaited_once()
        self.assertEqual(order.status, "failed")

    def test_lookup_unreachable_marks_unknown_never_failed(self):
        """反查不可达 → 「未知」+ 告警；**绝不写 failed**（否则账面与交易所分叉）"""
        order, outcome, alert, captures = self._run(
            _FakeAdapter(error=OrderLookupUnavailableError("timeout"))
        )

        self.assertEqual(outcome.kind, "unknown")
        self.assertEqual(captures.last.get("status"), "unknown")
        self.assertNotEqual(captures.last.get("status"), "failed")
        alert.assert_awaited_once()
        self.assertEqual(order.status, "unknown")

    def test_lookup_unsupported_is_also_unknown(self):
        """适配器不具备反查能力 = 「不知道」，同样不能记 failed"""
        _order_obj, outcome, _alert, captures = self._run(
            _FakeAdapter(error=OrderLookupUnsupportedError("no capability"))
        )

        self.assertEqual(outcome.kind, "unknown")
        self.assertEqual(captures.last.get("status"), "unknown")

    def test_unexpected_exception_is_also_unknown(self):
        """未预期异常必须落进「未知」，不能穿透到 failed 分支"""
        _order_obj, outcome, _alert, captures = self._run(
            _FakeAdapter(error=RuntimeError("boom"))
        )

        self.assertEqual(outcome.kind, "unknown")
        self.assertEqual(captures.last.get("status"), "unknown")

    def test_hit_stamps_exchange_id_only_and_stays_silent(self):
        """命中 → 只补 exchange_order_id：状态与盈亏交给成交同步，且不告警"""
        order, outcome, alert, captures = self._run(
            _FakeAdapter(result={"orderId": 987654, "status": "NEW"})
        )

        self.assertEqual(outcome.kind, "resolved")
        self.assertEqual(captures.last.get("exchange_order_id"), "987654")
        self.assertNotIn("status", captures.last, "扫描不得代言成交状态")
        self.assertFalse(outcome.needs_alert)
        alert.assert_not_awaited()
        self.assertEqual(order.exchange_order_id, "987654")
        self.assertEqual(order.status, "pending", "状态保持不变，等成交同步接手")

    def test_hit_without_order_id_is_unknown(self):
        """命中却拿不到 orderId → 仍是「不知道」，不许当成 failed"""
        _order_obj, outcome, _alert, captures = self._run(
            _FakeAdapter(result={"status": "NEW"})
        )

        self.assertEqual(outcome.kind, "unknown")
        self.assertEqual(captures.last.get("status"), "unknown")

    def test_existing_unknown_row_is_not_alerted_twice(self):
        """已经是「未知」的行再扫一次 → 不重复告警（否则每轮都骚扰用户）"""
        _order_obj, outcome, alert, captures = self._run(
            _FakeAdapter(error=OrderLookupUnavailableError("still down")),
            order=_order(status="unknown"),
        )

        self.assertEqual(outcome.kind, "unknown")
        self.assertFalse(outcome.needs_alert)
        alert.assert_not_awaited()
        self.assertEqual(captures.last.get("status"), "unknown")

    def test_queried_by_request_id_as_client_order_id(self):
        """反查用的 key 必须是 request_id（幂等键 == newClientOrderId）"""
        adapter = _FakeAdapter(result=None)
        order = _order()

        self._run(adapter, order=order)

        self.assertEqual(adapter.queries, [str(order.request_id)])


class TestSweepScope(unittest.TestCase):
    """扫描范围：只碰「没有交易所 ID 且已过下单窗口」的行。"""

    def test_recent_pending_row_is_out_of_scope(self):
        """刚刚落库、可能还在途中的行不得被反查（交易所此刻还没有它）"""
        fresh = _order(created_at=timezone.now())
        self.assertFalse(is_beyond_placement_window(fresh))

        with _MarkCapture([fresh]):
            with patch(
                "apps.trading.pending_reconcile.build_account_adapter",
                new=AsyncMock(),
            ) as build:
                result = asyncio.run(sweep_dangling_orders(accounts=[]))

        self.assertEqual(result.scanned, 0)
        build.assert_not_awaited()

    def test_row_with_exchange_order_id_belongs_to_fill_sync(self):
        """已有交易所 ID 的行属于成交同步，扫描的取数范围必须把它们排除在外"""
        with _MarkCapture([_order(exchange_order_id="555")]) as captures:
            with patch(
                "apps.trading.pending_reconcile.build_account_adapter",
                new=AsyncMock(),
            ) as build:
                asyncio.run(sweep_dangling_orders(accounts=[]))
            query = captures.query_filter

        self.assertEqual(query.get("exchange_order_id"), "")
        build.assert_not_awaited()

    def test_unknown_is_an_active_status(self):
        """「未知」是非终态：漏出活跃枚举等于把唯一可能活着的单当成不存在。

        断言的是 **Order 上那个常量**：扫描器、成交同步、视图读的必须是同一份，
        各自手抄一个字面量迟早有一处漏掉 ``unknown``。
        """
        self.assertIn("unknown", Order.ACTIVE_STATUSES)

    def test_adapter_is_resolved_per_account_not_per_exchange(self):
        """两个账户同交易所 → 各查各的（按交易所名索引会互相顶掉，把活单查成不存在）"""
        acc_a = SimpleNamespace(id="acc-a", exchange="binance")
        acc_b = SimpleNamespace(id="acc-b", exchange="binance")
        order_a = _order(id="order-a", exchange_account_id="acc-a", exchange_account=acc_a)
        order_b = _order(id="order-b", exchange_account_id="acc-b", exchange_account=acc_b)
        adapter_a = _FakeAdapter(tag="acc-a", result=None)
        adapter_b = _FakeAdapter(tag="acc-b", error=OrderLookupUnavailableError("x"))

        async def _build(account):
            return {"acc-a": adapter_a, "acc-b": adapter_b}[str(account.id)]

        with _MarkCapture([order_a, order_b]):
            with patch(
                "apps.trading.pending_reconcile.build_account_adapter",
                side_effect=_build,
            ):
                with patch(
                    "apps.trading.pending_reconcile.alert_order_anomaly",
                    new=AsyncMock(return_value=True),
                ) as alert:
                    result = asyncio.run(
                        sweep_dangling_orders(accounts=[acc_a, acc_b])
                    )

        self.assertEqual(result.scanned, 2)
        self.assertEqual(adapter_a.queries, [str(order_a.request_id)])
        self.assertEqual(adapter_b.queries, [str(order_b.request_id)])
        self.assertEqual(result.failed, 1)
        self.assertEqual(result.unknown, 1)
        self.assertEqual(alert.await_count, 2)
        self.assertTrue(adapter_a.disconnected)
        self.assertTrue(adapter_b.disconnected)

    def test_alert_undelivered_is_counted_not_silently_dropped(self):
        """告警发不出去必须进统计（进任务健康检查），不能装作送达"""
        with _MarkCapture([_order()]):
            with patch(
                "apps.trading.pending_reconcile.build_account_adapter",
                new=AsyncMock(return_value=_FakeAdapter(result=None)),
            ):
                with patch(
                    "apps.trading.pending_reconcile.alert_order_anomaly",
                    new=AsyncMock(return_value=False),
                ):
                    result = asyncio.run(
                        sweep_dangling_orders(
                            accounts=[SimpleNamespace(id="acc-1", exchange="binance")]
                        )
                    )

        self.assertEqual(result.failed, 1)
        self.assertEqual(result.alert_undelivered, 1)


class TestSyncNoLongerSkipsDangling(unittest.TestCase):
    """`_sync_active_orders`：那句「无交易所 ID 则跳过」已修掉，且不阻塞成交同步。"""

    def setUp(self):
        from apps.trading.executor import OrderExecutor

        self.executor = OrderExecutor()
        self.executor._running = True
        self.executor._riskguard = None
        self.executor._account_adapters = {}

    def tearDown(self):
        from apps.trading.executor import OrderExecutor

        OrderExecutor._instance = None

    def _run_sync(self, orders):
        with _MarkCapture(orders):
            return asyncio.run(self.executor._sync_active_orders())

    def test_dangling_row_triggers_sweep(self):
        """超窗口的空交易所 ID 行会触发反查扫描（原来是被 continue 掉的）"""

        async def _scenario():
            with patch(
                "apps.trading.executor.sweep_dangling_orders", new=AsyncMock()
            ) as sweep:
                with _MarkCapture([_order()]):
                    await self.executor._sync_active_orders()
                    await asyncio.sleep(0)
                    task = self.executor._dangling_sweep_task
                    self.assertIsNotNone(task)
                    await task
            sweep.assert_awaited_once()

        asyncio.run(_scenario())

    def test_fresh_pending_row_does_not_trigger_sweep(self):
        """刚下单、请求还在途中的行不触发反查"""
        with patch(
            "apps.trading.executor.sweep_dangling_orders", new=AsyncMock()
        ) as sweep:
            self._run_sync([_order(created_at=timezone.now())])

        sweep.assert_not_awaited()

    def test_sweep_is_not_awaited_inline(self):
        """扫描不得内联 await：交易所超时会饿死后面所有交易所的成交同步"""
        release = asyncio.Event()
        started = asyncio.Event()

        async def _blocking_sweep():
            started.set()
            await release.wait()

        async def _scenario():
            with patch(
                "apps.trading.executor.sweep_dangling_orders",
                side_effect=_blocking_sweep,
            ):
                with _MarkCapture([_order()]):
                    await self.executor._sync_active_orders()
                # 走到这里说明 _sync_active_orders 没有被那个卡住的扫描拖住
                await asyncio.sleep(0)
                self.assertTrue(started.is_set(), "扫描任务应已启动")
                self.assertFalse(self.executor._dangling_sweep_task.done())
                release.set()
                await self.executor._dangling_sweep_task

        asyncio.run(_scenario())

    def test_unknown_rows_with_exchange_id_are_fill_synced(self):
        """带交易所 ID 的「未知」行必须继续被成交同步推进，否则永远卡在未知"""
        adapter = AsyncMock()
        adapter.fetch_order.return_value = SimpleNamespace(
            status="filled",
            filled_quantity=Decimal("100"),
            avg_fill_price=Decimal("1"),
            error_message=None,
        )
        self.executor._account_adapters = {"acc-1": adapter}
        order = _order(status="unknown", exchange_order_id="777", side="buy")

        self._run_sync([order])

        adapter.fetch_order.assert_awaited_once_with("777", "DOGE/USDT")

    def test_no_duplicate_sweeps_while_one_is_running(self):
        """上一轮扫描未结束时不再叠加（同一批行不需要两个写入者）"""
        release = asyncio.Event()

        async def _blocking_sweep():
            await release.wait()

        async def _scenario():
            with patch(
                "apps.trading.executor.sweep_dangling_orders",
                side_effect=_blocking_sweep,
            ):
                with _MarkCapture([_order()]):
                    await self.executor._sync_active_orders()
                    first = self.executor._dangling_sweep_task
                    await self.executor._sync_active_orders()
                    self.assertIs(self.executor._dangling_sweep_task, first)
                    release.set()
                    await first

        asyncio.run(_scenario())


class TestBeatBackstop(unittest.TestCase):
    """兜底扫描：beat 任务 + 框架活跃时让位。"""

    def test_beat_schedule_registers_the_task(self):
        """beat 里原来没有任何订单相关任务，本单元补上这一条"""
        from celery_app import app

        self.assertIn("check-order-status", app.conf.beat_schedule)
        self.assertEqual(
            app.conf.beat_schedule["check-order-status"]["task"],
            "apps.trading.tasks.check_order_status",
        )

    def test_task_skips_while_frame_is_active(self):
        """交易框架在跑 → 跳过，由进程内成交同步负责（避免两个写入者）"""
        from apps.trading.tasks import check_order_status

        with patch(
            "apps.trading.executor.OrderExecutor.get_instance",
            return_value=MagicMock(),
        ):
            with patch(
                "apps.trading.pending_reconcile.sweep_dangling_orders",
                new=AsyncMock(),
            ) as sweep:
                result = check_order_status.run()

        self.assertEqual(result, {"skipped": "order_executor_active"})
        sweep.assert_not_awaited()

    def test_task_runs_sweep_when_frame_is_down(self):
        """框架不在 → 扫描必须真的跑（悬挂行正是「进程死过一次」的产物）"""
        from apps.trading.pending_reconcile import SweepResult
        from apps.trading.tasks import check_order_status

        fake = SweepResult(scanned=2, failed=1, unknown=1)

        with patch(
            "apps.trading.executor.OrderExecutor.get_instance", return_value=None
        ):
            with patch(
                "apps.trading.pending_reconcile.sweep_dangling_orders",
                new=AsyncMock(return_value=fake),
            ) as sweep:
                result = check_order_status.run()

        sweep.assert_awaited_once()
        self.assertEqual(result["scanned"], 2)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["unknown"], 1)


class TestAlertSeam(unittest.TestCase):
    """告警必须落到**人**身上；没有接收人就不算告警。"""

    def test_order_without_user_is_not_reported_as_delivered(self):
        from apps.trading.alerts import alert_order_anomaly

        delivered = asyncio.run(
            alert_order_anomaly(_order(user_id=None), "原因")
        )

        self.assertFalse(delivered)

    def test_alert_goes_through_both_existing_channels(self):
        from apps.trading import alerts

        with patch.object(alerts, "_persist", new=AsyncMock()) as persist:
            with patch.object(alerts, "_publish", new=AsyncMock()) as publish:
                delivered = asyncio.run(
                    alerts.alert_order_anomaly(_order(), "原因")
                )

        persist.assert_awaited_once()
        publish.assert_awaited_once()
        self.assertEqual(publish.await_args.args[0], "user-1")
        self.assertTrue(delivered)

    def test_publish_failure_is_reported_as_undelivered(self):
        from apps.trading import alerts

        with patch.object(alerts, "_persist", new=AsyncMock()):
            with patch.object(
                alerts, "_publish", new=AsyncMock(side_effect=RuntimeError("down"))
            ):
                delivered = asyncio.run(
                    alerts.alert_order_anomaly(_order(), "原因")
                )

        self.assertFalse(delivered)


class TestDanglingOutcomeSemantics(unittest.TestCase):
    def test_only_resolved_and_unknown_count_as_open(self):
        self.assertTrue(DanglingOutcome("resolved").is_open)
        self.assertTrue(DanglingOutcome("unknown").is_open)
        self.assertFalse(DanglingOutcome("failed").is_open)


if __name__ == "__main__":
    unittest.main()
