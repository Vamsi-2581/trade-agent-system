"""会话详情：一次请求拿全「会话本身 + 收益统计 + 生效参数 + 在拦的层 + 最近订单」。

这个接口存在的理由是**列表接口不该背这些**：50 条会话各算一次统计就是 50 次查询，而列表
本来只需要一个 `realized_pnl`（一次聚合）。所以下面既有「详情带了什么」，也有「列表没带
什么」——后者是性能约束，写成断言才守得住。

另外两处是**同源**关系，不是一串抄下来的字：

* 熔断层标题取自 `halt.HaltLayer.text`（与 `query_halt`、与订单被拒的理由逐字同源），
  所以断言拿 `halt.layer_of(row).text` 去比；
* 卡片与弹窗的「盈亏」是同一个数（列表的 SQL 聚合 vs 详情的逐单汇总）。
"""

from __future__ import annotations

import datetime
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.backtest.models import BacktestResult
from apps.exchange.models import ExchangeAccount
from apps.regime import halt
from apps.regime.models import ActorKind, HaltDeclaration, HaltTrigger
from apps.trading.models import LiveSession, Order, Strategy
from apps.trading.views import SESSION_DETAIL_ORDERS

User = get_user_model()


def _declare(*, scope: str, label: str) -> HaltDeclaration:
    return HaltDeclaration.objects.create(
        trigger=HaltTrigger.BLANKET.value,
        scope=scope,
        label=label,
        opened_at=timezone.now() - datetime.timedelta(hours=1),
        expires_at=None,
        closed_at=None,
        reason="测试",
        actor_kind=ActorKind.TASK.value,
        actor_name="test",
    )


class _SessionDetailBase(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="detail@test.com",
            username="detail_tester",
            password="testpass123",
        )
        self.strategy = Strategy.objects.create(name="s1", code_path="strategies/s1.py")
        self.account = ExchangeAccount.objects.create(
            exchange="binance",
            label="detail",
            api_key_enc=b"x",
            api_secret_enc=b"y",
            testnet=True,
        )
        self.backtest = BacktestResult.objects.create(
            strategy=self.strategy,
            user=self.user,
            symbol="BTC/USDT",
            timeframe="4h",
            start_date=datetime.date(2026, 1, 1),
            end_date=datetime.date(2026, 2, 1),
            initial_capital=Decimal("10000"),
            final_capital=Decimal("11000"),
            total_return_pct=10.0,
            review_status="approved",
        )
        self.session = LiveSession.objects.create(
            user=self.user,
            strategy=self.strategy,
            backtest_result=self.backtest,
            symbol="BTC/USDT",
            initial_capital=Decimal("1000"),
            current_equity=Decimal("1010"),
            config={},
        )
        self.client.force_authenticate(self.user)

    # ── 小工具 ──

    def _order(self, session, *, pnl: Decimal | None, side: str = "sell"):
        return Order.objects.create(
            exchange_account=self.account,
            live_session=session,
            symbol=session.symbol,
            side=side,
            order_type="market",
            quantity=Decimal("0.01"),
            status="filled",
            realized_pnl=pnl,
        )

    def _another_session(self) -> LiveSession:
        return LiveSession.objects.create(
            user=self.user,
            strategy=self.strategy,
            symbol="ETH/USDT",
            initial_capital=Decimal("500"),
            current_equity=Decimal("500"),
        )

    def _detail(self, session=None):
        target = session or self.session
        return self.client.get(f"/api/trading/sessions/{target.id}/")

    def _list(self):
        return self.client.get("/api/trading/sessions/")


class TestWhatTheDetailCarries(_SessionDetailBase):
    def test_the_blocks_are_all_there(self):
        self._order(self.session, pnl=Decimal("12"))

        body = self._detail().json()

        for key in ("stats", "params", "halt_layers", "orders", "orders_total"):
            self.assertIn(key, body)
        self.assertEqual(body["realized_pnl"], body["stats"]["realized_pnl_total"])

    def test_the_stats_are_the_sessions_own_orders(self):
        self._order(self.session, pnl=Decimal("12"))
        self._order(self.session, pnl=Decimal("-4"))
        self._order(self.session, pnl=None, side="buy")  # 开仓单不算
        self._order(self._another_session(), pnl=Decimal("999"))  # 别人的会话

        stats = self._detail().json()["stats"]

        self.assertEqual(stats["realized_pnl_total"], "8.00000000")
        self.assertEqual(stats["closed_orders"], 2)
        self.assertEqual(stats["winning_orders"], 1)
        # 边界文案随数字一起回（前端原样显示，不在前端写死）
        self.assertTrue(any("熔断减仓" in c for c in stats["caveats"]))

    def test_a_session_without_orders_says_so_instead_of_zeroes(self):
        """没成交的会话：比率是 null（不是 0），订单表是空的、总数是 0。

        前端的「该会话尚无成交」就靠这三个事实——而不是靠它自己去猜。
        """
        body = self._detail().json()

        self.assertEqual(body["stats"]["closed_orders"], 0)
        self.assertIsNone(body["stats"]["win_rate"])
        self.assertEqual(body["orders"], [])
        self.assertEqual(body["orders_total"], 0)
        self.assertEqual(body["realized_pnl"], "0.00000000")

    def test_only_this_sessions_orders_are_embedded(self):
        mine = self._order(self.session, pnl=Decimal("1"))
        theirs = self._order(self._another_session(), pnl=Decimal("2"))

        ids = [o["id"] for o in self._detail().json()["orders"]]

        self.assertEqual(ids, [str(mine.id)])
        self.assertNotIn(str(theirs.id), ids)

    def test_the_order_list_is_capped_and_the_total_is_reported(self):
        """截断必须连同总数一起回：只回 50 条而不说总数，这张表看起来像全部。"""
        Order.objects.bulk_create(
            [
                Order(
                    exchange_account=self.account,
                    live_session=self.session,
                    symbol="BTC/USDT",
                    side="sell",
                    order_type="market",
                    quantity=Decimal("0.01"),
                    status="filled",
                    realized_pnl=Decimal("1"),
                )
                for _ in range(SESSION_DETAIL_ORDERS + 1)
            ]
        )

        body = self._detail().json()

        self.assertEqual(body["orders_total"], SESSION_DETAIL_ORDERS + 1)
        self.assertEqual(len(body["orders"]), SESSION_DETAIL_ORDERS)

    def test_an_unresolvable_strategy_is_admitted_not_guessed(self):
        """`s1` 没有实现类 —— 如实说解析不到，并回 config 原文。"""
        params = self._detail().json()["params"]

        self.assertFalse(params["resolved"])
        self.assertEqual(params["rows"], [])

    def test_another_users_session_is_a_404(self):
        other = User.objects.create_user(
            email="other@test.com", username="other_tester", password="testpass123"
        )
        theirs = LiveSession.objects.create(
            user=other,
            strategy=self.strategy,
            symbol="DOGE/USDT",
            initial_capital=Decimal("1"),
        )

        self.assertEqual(self._detail(theirs).status_code, 404)


class TestTheHaltLayerBlock(_SessionDetailBase):
    def test_a_layer_that_hits_this_session_is_reported_verbatim(self):
        """标题与 `halt.HaltLayer.text` 逐字同源——不在这里重拼一遍。"""
        row = _declare(scope=halt.global_scope(), label="FOMC 议息")

        layers = self._detail().json()["halt_layers"]

        self.assertEqual(
            layers,
            [
                {
                    "text": halt.layer_of(row).text,
                    "reason": row.reason,
                    "opened_at": row.opened_at.isoformat(),
                    "expires_at": None,
                }
            ],
        )

    def test_a_layer_for_another_symbol_is_not_mentioned(self):
        """只跑 BTC 的会话不该收到 DOGE 解锁窗口的通告——连名字都不该出现（第 172 条）。"""
        _declare(scope=halt.symbol_scope("DOGE/USDT"), label="DOGE 解锁")

        body = self._detail().json()

        self.assertEqual(body["halt_layers"], [])
        self.assertNotIn("DOGE", str(body["halt_layers"]))


class TestTheListStaysCheap(_SessionDetailBase):
    def test_the_list_carries_the_pnl_but_none_of_the_detail_blocks(self):
        self._order(self.session, pnl=Decimal("12"))

        item = self._list().json()[0]

        self.assertEqual(item["realized_pnl"], "12.00000000")
        for key in ("stats", "params", "halt_layers", "orders", "orders_total"):
            self.assertNotIn(key, item)

    def test_list_and_detail_agree_on_the_same_number(self):
        """卡片与弹窗显示的是同一个「盈亏」。分叉的表现是两个数都正常、但不一样。"""
        self._order(self.session, pnl=Decimal("12.5"))
        self._order(self.session, pnl=Decimal("-2.25"))

        item = self._list().json()[0]
        body = self._detail().json()

        self.assertEqual(item["realized_pnl"], body["realized_pnl"])
        self.assertEqual(item["realized_pnl"], body["stats"]["realized_pnl_total"])

    def test_the_other_users_sessions_are_not_in_my_list(self):
        other = User.objects.create_user(
            email="other2@test.com", username="other_tester2", password="testpass123"
        )
        LiveSession.objects.create(
            user=other,
            strategy=self.strategy,
            symbol="DOGE/USDT",
            initial_capital=Decimal("1"),
        )

        ids = [s["id"] for s in self._list().json()]

        self.assertEqual(ids, [str(self.session.id)])
