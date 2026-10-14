"""下单失败必须让用户看得见（2026-09-21）。

事故背景：2026-09-21 的 DOGE 买单失败后**完全静默**——`record_order_failure` 只喂熔断器，
用户只能翻订单列表才发现；失败意图（买 13119 DOGE）被丢弃且无人知晓。

2026-09-22 起通知改走**出站的唯一通知口** ``apps.trading.alerts.notify_user``（落库 +
即时推送），不再自己内联写 ``Notification``。契约随之变成：

  1. 订单失败 → 除熔断计数外，把一条带订单要素与原因的告警交给 ``notify_user``。
     这是行为变更：订单失败从此还有一条即时推送，不再只在 Web 通知中心可见。
  2. 通知投递失败（抛异常 or 返回 False）**绝不能**影响订单主流程，但必须留下 ERROR
     ——「没人可送」和「送出去了」不是同一件事，不能都算完成。
  3. 无 user_id 时保持旧行为（直接返回，不计熔断、不通知）。
"""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from apps.riskguard.guard import RiskGuard

FAILURE = dict(
    symbol="DOGE/USDT",
    side="buy",
    quantity="13119.21815906",
    error="ConnectTimeout（连接超时，请求未到达交易所）",
)


class TestOrderFailureNotification(unittest.IsolatedAsyncioTestCase):
    def _patch_cb(self):
        patcher = patch("apps.riskguard.guard.CircuitBreaker")
        mock_cb = patcher.start()
        mock_cb.return_value.record_failure = AsyncMock()
        self.addCleanup(patcher.stop)
        return mock_cb

    def _patch_notify(self, *, delivered=True, error=None):
        patcher = patch(
            "apps.trading.alerts.notify_user",
            new=AsyncMock(return_value=delivered, side_effect=error),
        )
        mock = patcher.start()
        self.addCleanup(patcher.stop)
        return mock

    async def test_order_failure_notifies_the_user(self):
        guard = RiskGuard("trading")
        mock_cb = self._patch_cb()
        notify = self._patch_notify()

        await guard.record_order_failure("user-1", **FAILURE)

        mock_cb.return_value.record_failure.assert_awaited_once()
        notify.assert_awaited_once()
        user_id, message = notify.await_args.args
        self.assertEqual(user_id, "user-1")
        self.assertIn("下单失败", message)
        for fragment in ("DOGE/USDT", "buy", "13119.21815906", "ConnectTimeout"):
            self.assertIn(fragment, message)

    async def test_unknown_placement_changes_the_wording(self):
        """``unknown=True``（下单结果未知）→ 通知**不能说"失败"**。

        那张单可能已在交易所活着。用户看到"下单失败"会再下一张，敞口变两倍；
        所以措辞必须改成"结果未知 + 先核对再决定是否重下"，并把原因带上。
        """
        guard = RiskGuard("trading")
        mock_cb = self._patch_cb()
        notify = self._patch_notify()

        await guard.record_order_failure("user-1", unknown=True, **FAILURE)

        # 未知单同样是风险信号：熔断计数照旧，不能因为改了措辞就不计数
        mock_cb.return_value.record_failure.assert_awaited_once()
        notify.assert_awaited_once()
        _, message = notify.await_args.args
        self.assertIn("下单结果未知", message)
        self.assertNotIn("下单失败", message, "不得用'失败'诱导用户重下")
        self.assertIn("请先到交易所核对", message)
        for fragment in ("DOGE/USDT", "buy", "13119.21815906", "ConnectTimeout"):
            self.assertIn(
                fragment, message, "未知单同样要带上订单要素，否则无法人工核对"
            )

    async def test_determined_failure_keeps_the_failure_wording(self):
        """默认（确定失败）措辞不变，不得误报成"结果未知"。"""
        guard = RiskGuard("trading")
        self._patch_cb()
        notify = self._patch_notify()

        await guard.record_order_failure("user-1", **FAILURE)

        _, message = notify.await_args.args
        self.assertIn("下单失败", message)
        self.assertNotIn("下单结果未知", message)

    async def test_notification_goes_through_the_single_outbound_seam(self):
        """走真实的通知口（只拦它的两个投递通道）：一条 ``Notification`` 必须落下来。

        直接 patch ``notify_user`` 证明不了「用户看得见」——那条断言在别处
        （``test_drawdown_and_reject.TestRejectIsVisible``）。这里反向确认调用方
        确实用上了那个口，而不是又自己写了一份内联投递。
        """
        guard = RiskGuard("trading")
        self._patch_cb()
        with (
            patch("apps.trading.alerts._persist", new=AsyncMock()) as persist,
            patch("apps.trading.alerts._publish", new=AsyncMock()) as publish,
        ):
            await guard.record_order_failure("user-1", **FAILURE)

        persist.assert_awaited_once()
        self.assertEqual(persist.await_args.args[0], "user-1")
        self.assertIn("DOGE/USDT", persist.await_args.args[1])
        publish.assert_awaited_once()

    async def test_notification_exception_does_not_break_order_flow(self):
        guard = RiskGuard("trading")
        mock_cb = self._patch_cb()
        self._patch_notify(error=RuntimeError("redis down"))

        with self.assertLogs("apps.riskguard.guard", level="ERROR"):
            await guard.record_order_failure("user-1", symbol="DOGE/USDT", error="x")

        mock_cb.return_value.record_failure.assert_awaited_once()

    async def test_undelivered_notification_is_logged_not_counted_as_done(self):
        """``notify_user`` 返回 False = 没有接收人 → 记 ERROR，而不是当作已送达。

        熔断计数照旧完成：告警是旁路。但「没送出去」必须留在日志里，否则「用户没
        收到」这件事本身就又变成静默的了。
        """
        guard = RiskGuard("trading")
        mock_cb = self._patch_cb()
        self._patch_notify(delivered=False)

        with self.assertLogs("apps.riskguard.guard", level="ERROR") as logs:
            await guard.record_order_failure("user-1", symbol="DOGE/USDT", error="x")

        mock_cb.return_value.record_failure.assert_awaited_once()
        self.assertTrue(any("未送达" in line for line in logs.output))

    async def test_without_user_id_is_noop(self):
        guard = RiskGuard("trading")
        mock_cb = self._patch_cb()
        notify = self._patch_notify()

        await guard.record_order_failure(None, symbol="DOGE/USDT", error="x")

        mock_cb.return_value.record_failure.assert_not_awaited()
        notify.assert_not_awaited()
