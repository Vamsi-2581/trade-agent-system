"""Test: 创建交易会话时，初始资金 = 绑定账户实时 USDT 余额（而非回测固定金额）。"""

from __future__ import annotations

import datetime
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from cryptography.fernet import Fernet
from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import override_settings
from rest_framework.test import APITestCase

from apps.backtest.models import BacktestResult
from apps.exchange.models import ExchangeAccount
from apps.trading.models import LiveSession, Strategy

User = get_user_model()

URL = "/api/trading/sessions/create/"


class FakeAdapter:
    """假交易所适配器：返回固定余额，记录 connect/disconnect 调用。"""

    def __init__(self, api_key, secret, testnet=False):
        self.api_key = api_key
        self.secret = secret
        self.testnet = testnet
        self.connect = AsyncMock()
        self.disconnect = AsyncMock()
        self.get_balance = AsyncMock(return_value={"USDT": Decimal("2500.50")})


class BrokenAdapter(FakeAdapter):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.connect = AsyncMock(side_effect=RuntimeError("network down"))


@override_settings(FERNET_KEY=Fernet.generate_key().decode())
class TestLiveSessionCreateInitialCapital(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="session@test.com",
            username="session_tester",
            password="testpass123",
        )
        f = Fernet(settings.FERNET_KEY.encode())
        self.account = ExchangeAccount.objects.create(
            exchange="binance",
            label="test",
            api_key_enc=f.encrypt(b"test_key"),
            api_secret_enc=f.encrypt(b"test_secret"),
            testnet=True,
        )
        self.strategy = Strategy.objects.create(name="s1", code_path="strategies/s1.py")
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
        self.client.force_authenticate(self.user)

    def _post(self):
        return self.client.post(
            URL,
            {
                "backtest_result_id": str(self.backtest.id),
                "mode": "paper",
                "exchange_account_id": str(self.account.id),
            },
            format="json",
        )

    def test_initial_capital_uses_account_balance(self):
        """初始资金取账户余额 2500.50，而非回测的 10000"""
        with patch("apps.trading.adapters.ADAPTER_MAP", {"binance": FakeAdapter}):
            resp = self._post()
        self.assertEqual(resp.status_code, 201, resp.content)
        session = LiveSession.objects.get()
        self.assertEqual(session.initial_capital, Decimal("2500.50"))
        self.assertEqual(session.current_equity, Decimal("2500.50"))
        self.assertNotEqual(session.initial_capital, self.backtest.initial_capital)

    def test_balance_fetch_failure_returns_502_no_session(self):
        """余额拉取失败 → 502，不创建会话"""
        with patch("apps.trading.adapters.ADAPTER_MAP", {"binance": BrokenAdapter}):
            resp = self._post()
        self.assertEqual(resp.status_code, 502)
        self.assertFalse(LiveSession.objects.exists())


@override_settings(FERNET_KEY=Fernet.generate_key().decode())
class TestLiveSessionCreateConfigInheritance(APITestCase):
    """config 继承回测参数：回测用什么参数验证的，会话就按什么参数跑。

    背景（2026-10 BNB/USDT 事故）：回测用 {"exit_at_mid": false} 验证通过，
    但创建会话时 config 没带上，session.config={} → 实盘按策略默认
    exit_at_mid=true 跑，出场敏感度翻倍，一周 10 次往返全亏。
    """

    def setUp(self):
        self.user = User.objects.create_user(
            email="cfg-inherit@test.com",
            username="cfg_inherit_tester",
            password="testpass123",
        )
        f = Fernet(settings.FERNET_KEY.encode())
        self.account = ExchangeAccount.objects.create(
            exchange="binance",
            label="test",
            api_key_enc=f.encrypt(b"test_key"),
            api_secret_enc=f.encrypt(b"test_secret"),
            testnet=True,
        )
        self.strategy = Strategy.objects.create(name="s2", code_path="strategies/s2.py")
        self.backtest = BacktestResult.objects.create(
            strategy=self.strategy,
            user=self.user,
            symbol="BNB/USDT",
            timeframe="4h",
            start_date=datetime.date(2026, 1, 1),
            end_date=datetime.date(2026, 2, 1),
            initial_capital=Decimal("10000"),
            final_capital=Decimal("10000"),
            total_return_pct=0.0,
            review_status="approved",
            parameters={"exit_at_mid": False, "position_pct": 0.2},
        )
        self.client.force_authenticate(self.user)

    def _post(self, extra=None):
        body = {
            "backtest_result_id": str(self.backtest.id),
            "mode": "paper",
            "exchange_account_id": str(self.account.id),
        }
        if extra:
            body.update(extra)
        return self.client.post(URL, body, format="json")

    def test_no_config_inherits_backtest_parameters(self):
        """不传 config → 完整继承回测参数（不再静默落回策略默认值）"""
        with patch("apps.trading.adapters.ADAPTER_MAP", {"binance": FakeAdapter}):
            resp = self._post()
        self.assertEqual(resp.status_code, 201, resp.content)
        session = LiveSession.objects.get()
        self.assertEqual(
            session.config,
            {"exit_at_mid": False, "position_pct": 0.2},
            "会话必须按回测验证过的参数跑",
        )

    def test_explicit_config_overrides_backtest_parameters(self):
        """显式 config 的键优先于回测参数；未覆盖的键仍继承。"""
        with patch("apps.trading.adapters.ADAPTER_MAP", {"binance": FakeAdapter}):
            resp = self._post(extra={"config": {"position_pct": 0.5}})
        self.assertEqual(resp.status_code, 201, resp.content)
        session = LiveSession.objects.get()
        self.assertEqual(
            session.config,
            {"exit_at_mid": False, "position_pct": 0.5},
        )

    def test_backtest_without_parameters_yields_empty_config(self):
        """回测未带自定义参数（全默认）→ config 为空，行为不变。"""
        self.backtest.parameters = {}
        self.backtest.save(update_fields=["parameters"])
        with patch("apps.trading.adapters.ADAPTER_MAP", {"binance": FakeAdapter}):
            resp = self._post()
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(LiveSession.objects.get().config, {})
