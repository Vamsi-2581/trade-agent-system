"""会话入口拒绝同账户同品种的第二支策略（跨策略串仓的第一道防线）。

背景（2026-10-07）：box_time_range_breakout_strategy 与 donchian_atr_trend_strategy
绑同一个 binance testnet 账户、同跑 DOGE/USDT。runner 每根 bar 把**账户级**该品种
持仓同步进 `ctx.position`，`close_position` 平到账户级 0——没有「本会话仓位」的
概念，于是 box 平掉了 donchian 开的 10401 DOGE/USDT，实现亏损 -79.78。

本文件钉住 create / start / resume 三个入口：同账户同品种上已有 running 或 paused
会话时，必须 400。

口径与 `LiveSession.ACTIVE_STATUSES` 不同：`pending` 没有 runner、不占持仓（它自己的
start 会走同一道检查），`stopped` / `error` 已结束——三者都不挡新会话。

和 test_live_session_mode_invariant 同一套夹具纪律：只验「准入」这一步，框架全打桩。
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.fernet import Fernet
from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import Client
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from apps.agent.frame_manager import FrameManager
from apps.backtest.models import BacktestResult
from apps.exchange.models import ExchangeAccount
from apps.trading.models import LiveSession, Strategy

User = get_user_model()

CREATE_URL = "/api/trading/sessions/create/"


class FakeAdapter:
    """与 test_live_session_create 同款：固定余额，不碰真实网络。"""

    def __init__(self, api_key, secret, testnet=False):
        self.connect = AsyncMock()
        self.disconnect = AsyncMock()
        self.get_balance = AsyncMock(return_value={"USDT": Decimal("1000")})


def _user(username: str):
    return User.objects.create_user(
        username=username, email=f"{username}@example.com", password="pw12345"
    )


def _account(user, label: str = "shared-testnet", testnet: bool = True):
    return ExchangeAccount.objects.create(
        user=user,
        exchange="binance",
        label=label,
        api_key_enc=b"enc",
        api_secret_enc=b"enc",
        testnet=testnet,
    )


def _backtest(user, symbol: str, strategy_name: str):
    strategy = Strategy.objects.create(name=strategy_name, code_path="strategies/s.py")
    backtest = BacktestResult.objects.create(
        strategy=strategy,
        user=user,
        symbol=symbol,
        timeframe="4h",
        start_date=datetime.date(2026, 1, 1),
        end_date=datetime.date(2026, 2, 1),
        initial_capital=Decimal("1000"),
        final_capital=Decimal("1100"),
        total_return_pct=10.0,
        review_status="approved",
    )
    return strategy, backtest


def _session(user, *, symbol, status, account, strategy_name, mode="paper"):
    _, backtest = _backtest(user, symbol, strategy_name)
    return LiveSession.objects.create(
        user=user,
        backtest_result=backtest,
        strategy=backtest.strategy,
        symbol=symbol,
        mode=mode,
        status=status,
        exchange_account=account,
        initial_capital=Decimal("1000"),
        current_equity=Decimal("1000"),
        config={},
    )


def _idle_frame_manager():
    """框架全部打桩：本文件只验「准入」这一步，不验框架起没起来。"""
    fm = MagicMock()
    fm.start_trading_frame = AsyncMock()
    fm.start_strategy_runner = AsyncMock()
    fm.stop_strategy_runner = AsyncMock()
    fm.start_live_session = AsyncMock()
    return fm


def _post(user, session, action: str):
    client = Client()
    token = AccessToken.for_user(user)
    with patch.object(
        FrameManager, "get_instance", classmethod(lambda cls: _idle_frame_manager())
    ):
        return client.post(
            f"/api/trading/sessions/{session.id}/{action}/",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )


# --------------------------------------------------------------------------- #
# create：同账户同品种已有活跃会话 → 拒绝创建（不落库、不动余额查询）
# --------------------------------------------------------------------------- #


@pytest.mark.django_db
class TestCreateRejectsSharedAccountSymbol:
    """create 路径会真实解密账户密钥去查余额，所以这里用真 Fernet 加密的密钥。"""

    @pytest.fixture(autouse=True)
    def _hermetic_fernet_key(self, settings):
        # 不依赖容器环境里的 FERNET_KEY：每个用例独立一把，互不串密钥
        settings.FERNET_KEY = Fernet.generate_key().decode()

    def _encrypted_account(self, user, label: str = "shared-testnet"):
        f = Fernet(settings.FERNET_KEY.encode())
        return ExchangeAccount.objects.create(
            user=user,
            exchange="binance",
            label=label,
            api_key_enc=f.encrypt(b"test_key"),
            api_secret_enc=f.encrypt(b"test_secret"),
            testnet=True,
        )

    def _post_create(self, user, backtest, account):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {AccessToken.for_user(user)}")
        with patch("apps.trading.adapters.ADAPTER_MAP", {"binance": FakeAdapter}):
            return client.post(
                CREATE_URL,
                {
                    "backtest_result_id": str(backtest.id),
                    "mode": "paper",
                    "exchange_account_id": str(account.id),
                },
                format="json",
            )

    def test_rejected_when_running_session_shares_account_and_symbol(self):
        user = _user("cc-running")
        account = self._encrypted_account(user)
        other = _session(
            user,
            symbol="DOGE/USDT",
            status="running",
            account=account,
            strategy_name="donchian_strategy",
        )
        _, incoming = _backtest(user, "DOGE/USDT", "box_strategy")

        resp = self._post_create(user, incoming, account)

        assert resp.status_code == 400, resp.content
        error = resp.json()["error"]
        assert "donchian_strategy" in error, "拒绝理由必须点出挡路的那支会话"
        assert "账户级持仓" in error, "拒绝理由必须说明为什么冲突"
        assert LiveSession.objects.count() == 1, "拒绝时不得创建会话"
        other.refresh_from_db()
        assert other.status == "running", "拒绝时不得改动既有会话"

    def test_rejected_when_paused_session_shares_account_and_symbol(self):
        """paused 可能残留持仓、resume 后立刻又占——同样挡。"""
        user = _user("cc-paused")
        account = self._encrypted_account(user)
        _session(
            user,
            symbol="DOGE/USDT",
            status="paused",
            account=account,
            strategy_name="donchian_strategy",
        )
        _, incoming = _backtest(user, "DOGE/USDT", "box_strategy")

        resp = self._post_create(user, incoming, account)

        assert resp.status_code == 400, resp.content

    def test_allowed_when_other_stopped(self):
        user = _user("cc-stopped")
        account = self._encrypted_account(user)
        _session(
            user,
            symbol="DOGE/USDT",
            status="stopped",
            account=account,
            strategy_name="donchian_strategy",
        )
        _, incoming = _backtest(user, "DOGE/USDT", "box_strategy")

        resp = self._post_create(user, incoming, account)

        assert resp.status_code == 201, resp.content
        assert LiveSession.objects.count() == 2

    def test_allowed_for_different_symbol(self):
        user = _user("cc-symbol")
        account = self._encrypted_account(user)
        _session(
            user,
            symbol="BTC/USDT",
            status="running",
            account=account,
            strategy_name="donchian_strategy",
        )
        _, incoming = _backtest(user, "DOGE/USDT", "box_strategy")

        resp = self._post_create(user, incoming, account)

        assert resp.status_code == 201, resp.content

    def test_allowed_for_different_account(self):
        user = _user("cc-account")
        account_a = self._encrypted_account(user, label="acct-a")
        account_b = self._encrypted_account(user, label="acct-b")
        _session(
            user,
            symbol="DOGE/USDT",
            status="running",
            account=account_a,
            strategy_name="donchian_strategy",
        )
        _, incoming = _backtest(user, "DOGE/USDT", "box_strategy")

        resp = self._post_create(user, incoming, account_b)

        assert resp.status_code == 201, resp.content

    def test_allowed_when_other_pending(self):
        """pending 没有 runner、不占持仓——不挡创建（它自己的 start 会再查一遍）。"""
        user = _user("cc-pending")
        account = self._encrypted_account(user)
        _session(
            user,
            symbol="DOGE/USDT",
            status="pending",
            account=account,
            strategy_name="donchian_strategy",
        )
        _, incoming = _backtest(user, "DOGE/USDT", "box_strategy")

        resp = self._post_create(user, incoming, account)

        assert resp.status_code == 201, resp.content


# --------------------------------------------------------------------------- #
# start：同账户同品种已有活跃会话 → 拒绝启动
# --------------------------------------------------------------------------- #


@pytest.mark.django_db
class TestStartRejectsSharedAccountSymbol:
    def test_rejected_when_running_session_shares_account_and_symbol(self):
        user = _user("st-running")
        account = _account(user)
        _session(
            user,
            symbol="DOGE/USDT",
            status="running",
            account=account,
            strategy_name="donchian_strategy",
        )
        target = _session(
            user,
            symbol="DOGE/USDT",
            status="pending",
            account=account,
            strategy_name="box_strategy",
        )

        resp = _post(user, target, "start")

        assert resp.status_code == 400, resp.content
        assert "donchian_strategy" in resp.json()["error"]
        target.refresh_from_db()
        assert target.status == "pending", "拒绝启动时不得改动会话状态"

    def test_rejected_when_paused_session_shares_account_and_symbol(self):
        user = _user("st-paused")
        account = _account(user)
        _session(
            user,
            symbol="DOGE/USDT",
            status="paused",
            account=account,
            strategy_name="donchian_strategy",
        )
        target = _session(
            user,
            symbol="DOGE/USDT",
            status="pending",
            account=account,
            strategy_name="box_strategy",
        )

        resp = _post(user, target, "start")

        assert resp.status_code == 400, resp.content

    def test_allowed_when_other_stopped(self):
        user = _user("st-stopped")
        account = _account(user)
        _session(
            user,
            symbol="DOGE/USDT",
            status="stopped",
            account=account,
            strategy_name="donchian_strategy",
        )
        target = _session(
            user,
            symbol="DOGE/USDT",
            status="pending",
            account=account,
            strategy_name="box_strategy",
        )

        resp = _post(user, target, "start")

        assert resp.status_code == 200, resp.content
        target.refresh_from_db()
        assert target.status == "running"

    def test_allowed_for_different_symbol(self):
        user = _user("st-symbol")
        account = _account(user)
        _session(
            user,
            symbol="BTC/USDT",
            status="running",
            account=account,
            strategy_name="donchian_strategy",
        )
        target = _session(
            user,
            symbol="DOGE/USDT",
            status="pending",
            account=account,
            strategy_name="box_strategy",
        )

        resp = _post(user, target, "start")

        assert resp.status_code == 200, resp.content

    def test_allowed_for_different_account(self):
        user = _user("st-account")
        account_a = _account(user, label="acct-a")
        account_b = _account(user, label="acct-b")
        _session(
            user,
            symbol="DOGE/USDT",
            status="running",
            account=account_a,
            strategy_name="donchian_strategy",
        )
        target = _session(
            user,
            symbol="DOGE/USDT",
            status="pending",
            account=account_b,
            strategy_name="box_strategy",
        )

        resp = _post(user, target, "start")

        assert resp.status_code == 200, resp.content

    def test_allowed_when_other_pending(self):
        """pending 不占持仓：两支 pending 先后启动各自安全（后启动的那支在 start
        时刻仍会查到先启动的——若那时对方已 running 则被挡，这里钉的是 pending
        本身不构成冲突）。"""
        user = _user("st-pending")
        account = _account(user)
        _session(
            user,
            symbol="DOGE/USDT",
            status="pending",
            account=account,
            strategy_name="donchian_strategy",
        )
        target = _session(
            user,
            symbol="DOGE/USDT",
            status="pending",
            account=account,
            strategy_name="box_strategy",
        )

        resp = _post(user, target, "start")

        assert resp.status_code == 200, resp.content

    def test_allows_self_restart(self):
        """对 running 会话点 start 是「重启自己」（幽灵状态自愈），不是冲突。
        漏掉 exclude_pk 的话，这条会把所有 running 会话的重修路堵死。"""
        user = _user("st-self")
        account = _account(user)
        target = _session(
            user,
            symbol="DOGE/USDT",
            status="running",
            account=account,
            strategy_name="box_strategy",
        )

        resp = _post(user, target, "start")

        assert resp.status_code == 200, resp.content
        target.refresh_from_db()
        assert target.status == "running"


# --------------------------------------------------------------------------- #
# resume：恢复也是一次「启动」，同一道检查
# --------------------------------------------------------------------------- #


@pytest.mark.django_db
class TestResumeRejectsSharedAccountSymbol:
    def test_rejected_when_running_session_shares_account_and_symbol(self):
        user = _user("rs-running")
        account = _account(user)
        _session(
            user,
            symbol="DOGE/USDT",
            status="running",
            account=account,
            strategy_name="donchian_strategy",
        )
        target = _session(
            user,
            symbol="DOGE/USDT",
            status="paused",
            account=account,
            strategy_name="box_strategy",
        )

        resp = _post(user, target, "resume")

        assert resp.status_code == 400, resp.content
        assert "donchian_strategy" in resp.json()["error"]
        target.refresh_from_db()
        assert target.status == "paused", "拒绝恢复时不得改动会话状态"

    def test_rejected_when_paused_session_shares_account_and_symbol(self):
        user = _user("rs-paused")
        account = _account(user)
        _session(
            user,
            symbol="DOGE/USDT",
            status="paused",
            account=account,
            strategy_name="donchian_strategy",
        )
        target = _session(
            user,
            symbol="DOGE/USDT",
            status="paused",
            account=account,
            strategy_name="box_strategy",
        )

        resp = _post(user, target, "resume")

        assert resp.status_code == 400, resp.content

    def test_allowed_when_other_stopped(self):
        user = _user("rs-stopped")
        account = _account(user)
        _session(
            user,
            symbol="DOGE/USDT",
            status="stopped",
            account=account,
            strategy_name="donchian_strategy",
        )
        target = _session(
            user,
            symbol="DOGE/USDT",
            status="paused",
            account=account,
            strategy_name="box_strategy",
        )

        resp = _post(user, target, "resume")

        assert resp.status_code == 200, resp.content
        target.refresh_from_db()
        assert target.status == "running"

    def test_allowed_when_no_other_session(self):
        user = _user("rs-alone")
        account = _account(user)
        target = _session(
            user,
            symbol="DOGE/USDT",
            status="paused",
            account=account,
            strategy_name="box_strategy",
        )

        resp = _post(user, target, "resume")

        assert resp.status_code == 200, resp.content
        target.refresh_from_db()
        assert target.status == "running"
