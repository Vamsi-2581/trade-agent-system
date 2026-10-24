"""Tests for `account_list`（`/api/trading/accounts/`）—— 交易页账户卡片的数据源。

症状（2026-10-24「binance 实盘没有同步现有资产」）：交易页每一张卡片都被标成「实盘」，
余额一律 `$0.00`，而且**没有任何提示**说这个数根本没取到。两处独立的缺陷叠在同一个
端点上，所以这里分开锁：

1. **`testnet` 必须在响应里**：前端靠这一位区分实盘/模拟（徽标 + 页签筛选）。它曾经是
   `write_only=True`，读面没有这个键，前端 `undefined ? ... : ...` 恒判成实盘。
2. **余额取不到时 `balance` 是 `null`，不是 `{"total": "0"}""**：`OrderExecutor` 没跑
   时这个视图压根不去问交易所。编一个 0 出来等于告诉用户「你的账户里没有钱」，
   而真相是「这次没问」。
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import override_settings
from rest_framework.test import APITestCase

from apps.exchange.models import ExchangeAccount


@override_settings(
    CACHES={
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        }
    }
)
class TestTradingAccountsView(APITestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="t_trading_accounts",
            email="t_trading_accounts@example.com",
            password="x",
        )
        self.client.force_authenticate(self.user)
        self.live = ExchangeAccount.objects.create(
            exchange="binance",
            label="实盘账户",
            testnet=False,
            is_active=True,
            api_key_enc=b"k",
            api_secret_enc=b"s",
        )
        self.paper = ExchangeAccount.objects.create(
            exchange="binance",
            label="模拟账户",
            testnet=True,
            is_active=True,
            api_key_enc=b"k",
            api_secret_enc=b"s",
        )

    def test_testnet_flag_survives_the_trading_endpoint(self):
        """交易页读的是这个端点，`testnet` 必须跟着 `is_active` 一起出来。"""
        with patch(
            "apps.trading.executor.OrderExecutor.get_instance", return_value=None
        ):
            resp = self.client.get("/api/trading/accounts/")

        self.assertEqual(resp.status_code, 200)
        by_label = {a["label"]: a for a in resp.json()}
        self.assertIs(by_label["模拟账户"]["testnet"], True)
        self.assertIs(by_label["实盘账户"]["testnet"], False)

    def test_balance_is_null_when_executor_not_running(self):
        """执行器没跑 = 没问过交易所 = 这个数不存在。**不许编成 0。**"""
        with patch(
            "apps.trading.executor.OrderExecutor.get_instance", return_value=None
        ):
            resp = self.client.get("/api/trading/accounts/")

        for account in resp.json():
            self.assertIsNone(
                account["balance"],
                msg=f"{account['label']} 的余额在没问过交易所时必须是 null",
            )

    def test_balance_comes_from_the_account_own_adapter(self):
        """两个账户各有各的适配器，余额不能串。"""
        live_adapter = MagicMock()
        live_adapter.get_balance = AsyncMock(return_value={"USDT": Decimal("1234.5")})
        paper_adapter = MagicMock()
        paper_adapter.get_balance = AsyncMock(return_value={"USDT": Decimal("7")})

        executor = MagicMock()
        executor._running = True
        executor.account_adapters.return_value = {
            str(self.live.id): live_adapter,
            str(self.paper.id): paper_adapter,
        }

        with patch(
            "apps.trading.executor.OrderExecutor.get_instance",
            return_value=executor,
        ):
            resp = self.client.get("/api/trading/accounts/")

        by_label = {a["label"]: a for a in resp.json()}
        self.assertEqual(by_label["实盘账户"]["balance"]["total"], "1234.5")
        self.assertEqual(by_label["模拟账户"]["balance"]["total"], "7")
