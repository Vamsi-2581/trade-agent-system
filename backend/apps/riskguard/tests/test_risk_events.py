"""RiskEvent 风控事件档案的模型与 API 测试。

2026-10-12 自 apps.risk 迁入：RiskEvent 现住在 ``apps.riskguard``，而
RiskConfig（guard 从不读取的死配置面）已随 apps.risk 一起摘除——原先
``TestRiskConfigModel`` 的一组用例随模型一起消失。
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.riskguard.models import RiskEvent


class TestRiskEventModel(TestCase):
    """测试风险事件模型"""

    def test_create_risk_event(self):
        event = RiskEvent.objects.create(
            level="P0",
            event_type="hard_limit",
            message="Daily loss limit exceeded",
        )
        self.assertEqual(event.level, "P0")
        self.assertEqual(event.event_type, "hard_limit")
        self.assertFalse(event.resolved)
        self.assertIsNotNone(event.created_at)
        self.assertIsNone(event.resolved_at)

    def test_risk_event_levels(self):
        for level in ["P0", "P1", "P2"]:
            event = RiskEvent.objects.create(
                level=level,
                event_type="heartbeat",
                message=f"{level} event",
            )
            self.assertEqual(event.level, level)

    def test_risk_event_types(self):
        # floating_loss 是 guard 浮亏告警的真实写入值（apps.risk 时代不在
        # 枚举里——唯一写入方季年写非法值），迁入后合法。
        for etype in [
            "hard_limit",
            "circuit_breaker",
            "reconciliation",
            "heartbeat",
            "floating_loss",
        ]:
            event = RiskEvent.objects.create(
                level="P1",
                event_type=etype,
                message=f"{etype} test",
            )
            self.assertEqual(event.event_type, etype)

    def test_resolve_event(self):
        from django.utils import timezone

        event = RiskEvent.objects.create(
            level="P0",
            event_type="hard_limit",
            message="Needs resolution",
        )
        event.resolved = True
        event.resolved_at = timezone.now()
        event.save()
        refreshed = RiskEvent.objects.get(id=event.id)
        self.assertTrue(refreshed.resolved)
        self.assertIsNotNone(refreshed.resolved_at)

    def test_ordering_by_created_at(self):
        """测试风险事件按时间倒序索引"""
        RiskEvent.objects.create(level="P2", event_type="heartbeat", message="first")
        RiskEvent.objects.create(level="P1", event_type="hard_limit", message="second")
        events = list(RiskEvent.objects.order_by("-created_at"))
        self.assertEqual(events[0].message, "second")


class TestRiskEventsApi(TestCase):
    """``/api/risk/events/`` 是右侧风控面板的轮询源，URL 搬家不换门牌。"""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="risk_reader", email="risk_reader@example.com", password="x" * 16
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_lists_latest_50_desc(self):
        RiskEvent.objects.create(level="P2", event_type="heartbeat", message="first")
        RiskEvent.objects.create(
            level="P1", event_type="floating_loss", message="second"
        )

        resp = self.client.get("/api/risk/events/")

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(len(body), 2)
        self.assertEqual(body[0]["message"], "second")
        self.assertEqual(body[1]["message"], "first")
        # floating_loss 现在是合法枚举值，前端事件面板能如实回显
        self.assertEqual(body[0]["event_type"], "floating_loss")

    def test_requires_authentication(self):
        anonymous = APIClient()
        resp = anonymous.get("/api/risk/events/")
        self.assertEqual(resp.status_code, 401)
