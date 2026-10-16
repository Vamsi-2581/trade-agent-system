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


class TestTheListCarriesTheEfficiencySegment(_SessionDetailBase):
    """spec 用户故事 18：「指标在会话列表的统计接口里同样可得」。

    这一条**不是免费的**：列表原本只做 `annotate_realized_pnl` 一个 SQL 聚合，从不调
    `compute_session_stats`，所以新键不会自动出现。另一条路（逐行调 `session_stats_for`）
    被 `TestTheListStaysCheap` 那个类否掉了——50 条会话 50 次查询。所以这里钉的是第三条
    路：**在同一个聚合里**带出效率段要的标量。
    """

    #: 六个契约键（`capital_efficiency_keys()`）。列表响应里逐字同名——前端只有一套读法。
    EFFICIENCY_KEYS = (
        "avg_capital_used",
        "capital_utilization",
        "idle_pct",
        "time_in_market_pct",
        "return_per_capital_used_pct",
    )

    def test_the_list_item_carries_every_contract_key(self):
        self._order(self.session, pnl=Decimal("12"))

        item = self._list().json()[0]

        for key in self.EFFICIENCY_KEYS:
            self.assertIn(key, item)
        self.assertIn("capital_efficiency", item)
        self.assertIn("capital_efficiency_metrics_available", item)
        self.assertIn("capital_efficiency_notes", item)

    def _window_around_now(self):
        """把 `setUp` 造的那条会话推进到「有窗口、且窗口**确实包住**订单」的状态。

        ## 为什么 stopped_at 必须造在**未来**

        `Order.created_at` 是 `auto_now_add`，写的是真实挂钟，所以订单必然落在 `_order()`
        被调用**的那一刻**。而窗口是左闭右开 `[started_at, stopped_at)`（纯模块口径 4），
        所以 `stopped_at = now()` 会把几微秒之后创建的订单分到窗口**外**——详情路径于是
        判 `no_trades`。那不是被测代码的错，是一份自相矛盾的测试数据（真实系统里不会出现
        「订单在会话停止之后才成交」）。

        这条边界值得写下来：**它是本票里唯一一处两条取数路径会真正分叉的地方**，而
        `test_the_list_and_the_detail_agree_on_the_efficiency_segment` 就是为它写的。
        """
        self.session.started_at = timezone.now() - datetime.timedelta(days=1)
        self.session.stopped_at = timezone.now() + datetime.timedelta(days=1)
        self.session.status = "stopped"
        self.session.save(update_fields=["started_at", "stopped_at", "status"])

    def test_the_list_and_the_detail_agree_on_the_efficiency_segment(self):
        """卡片与弹窗的「资金效率」是同一组数。分叉的表现是两个数都正常、但不一样。"""
        self._window_around_now()
        self._order(self.session, pnl=Decimal("12.5"))
        self._order(self.session, pnl=Decimal("-2.25"))

        item = self._list().json()[0]
        stats = self._detail().json()["stats"]

        for key in self.EFFICIENCY_KEYS:
            self.assertEqual(item[key], stats[key], f"{key} 两条路分叉了")
        self.assertEqual(item["capital_efficiency"]["trade_count"], 2)

    def test_the_list_says_why_the_numbers_are_thin(self):
        """列表也带上「这一侧是点观测口径」那句保留意见——**算得出时也要带**。

        少了它，`capital_utilization = 0` 就会被当成回测侧那个数读：把重仓读成高效，方向
        正是最危险的那一侧。
        """
        self._window_around_now()
        self._order(self.session, pnl=Decimal("12"))

        item = self._list().json()[0]

        self.assertTrue(item["capital_efficiency_metrics_available"])
        self.assertTrue(item["capital_efficiency_notes"])
        self.assertIn("点观测", item["capital_efficiency_notes"][0])

    def test_an_order_outside_the_window_is_not_counted_on_either_path(self):
        """窗口外的订单**两条路都不算**——这是唯一一处两条取数会真正分叉的地方。

        列表路径靠 SQL 注解的 `created_at` 比较裁窗口，详情路径靠纯模块 `_clip_to_window`
        裁（左闭右开）。**任何一侧漏了这道裁剪**，同一个键就会在卡片与弹窗上给出两个值，
        而两处看起来都正常：列表把窗口外那笔算进来、详情没算，于是「2 笔」与「1 笔」并排
        显示，谁也不知道哪个对。

        这条用例是变异探针逼出来的：把列表那侧退回「只数全量笔数」时，其余用例全绿
        ——因为它们造的订单恰好都落在窗口内。
        """
        # 会话在**任何订单创建之前**就停下了，所以它名下的订单全部落在窗口外。
        self.session.started_at = timezone.now() - datetime.timedelta(days=5)
        self.session.stopped_at = timezone.now() - datetime.timedelta(days=1)
        self.session.status = "stopped"
        self.session.save(update_fields=["started_at", "stopped_at", "status"])
        self._order(self.session, pnl=Decimal("12.5"))

        item = self._list().json()[0]
        stats = self._detail().json()["stats"]

        self.assertEqual(item["capital_efficiency"]["trade_count"], 0)
        self.assertEqual(stats["capital_efficiency"]["trade_count"], 0)
        self.assertEqual(item["capital_efficiency"]["reason"], "no_trades")
        # 盈亏照样算——窗口只裁「效率」，不裁「收益」。
        self.assertEqual(item["realized_pnl"], "12.50000000")
        self.assertEqual(stats["realized_pnl_total"], "12.50000000")
        for key in self.EFFICIENCY_KEYS:
            self.assertEqual(item[key], stats[key], f"{key} 两条路分叉了")

    def test_a_session_that_cannot_be_measured_is_none_not_zero(self):
        """量不了的行：整段 `None` + 原因，**不是 0**（0 会被读成「完全没用钱」）。

        `setUp` 造的会话没有 `started_at`（还没启动），所以它必然落在这一支上——这恰好
        是列表里最常见的一行：新建的会话。
        """
        item = self._list().json()[0]

        self.assertIsNone(item["started_at"])
        self.assertFalse(item["capital_efficiency_metrics_available"])
        self.assertEqual(
            item["capital_efficiency"]["reason"],
            "invalid_capital",
        )
        for key in self.EFFICIENCY_KEYS:
            self.assertIsNone(item[key], f"{key} 判不了时必须是 None")
        self.assertTrue(item["capital_efficiency_notes"])
