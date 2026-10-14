"""F1：pause/resume 必须真正停/起 runner，且启动恢复以 DB 为权威。

2026-09-21 实测：`live_session_pause` 只写 DB status，不碰 Redis 兼容层键（DB8
`frame:live_session:*`）也不停 runner → 重启后两个已 paused 的 DOGE 会话被旧值捞回来
并自动开仓（两笔因故失败，未真正持仓）。
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import redis as redis_lib
from asgiref.sync import async_to_sync
from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import Client
from rest_framework_simplejwt.tokens import AccessToken

from apps.agent.frame_manager import FrameManager, FrameState

User = get_user_model()


def _user(username: str):
    return User.objects.create_user(
        username=username, email=f"{username}@example.com", password="pw12345"
    )


def _strategy(user, name: str = "donchian_atr_trend_strategy"):
    from apps.trading.models import Strategy

    return Strategy.objects.create(
        name=name,
        code_path=f"strategies/{name}.py",
        is_active=True,
    )


def _paper_account(label: str = "pause-resume-paper"):
    """paper 会话必须绑 testnet 账户：mode 与账户 testnet 自洽是启动前提。"""
    from apps.exchange.models import ExchangeAccount

    return ExchangeAccount.objects.create(
        exchange="binance",
        label=label,
        api_key_enc=b"enc",
        api_secret_enc=b"enc",
        testnet=True,
    )


def _session(user, strategy, status: str = "running"):
    from apps.trading.models import LiveSession

    return LiveSession.objects.create(
        user=user,
        strategy=strategy,
        symbol="DOGE/USDT",
        mode="paper",
        status=status,
        exchange_account=_paper_account(),
        initial_capital=Decimal("10000"),
        current_equity=Decimal("10000"),
        config={},
    )


def _legacy_key(session_id) -> str:
    return f"frame:live_session:{session_id}"


def _legacy_redis() -> redis_lib.Redis:
    return redis_lib.from_url(
        settings.REDIS_URL,
        db=getattr(settings, "REDIS_DB_FRAME", 8),
        decode_responses=True,
    )


def _write_legacy(session_id, user, status: str = "running") -> redis_lib.Redis:
    r = _legacy_redis()
    r.hset(
        _legacy_key(session_id),
        mapping={
            "strategy_name": "donchian_atr_trend_strategy",
            "symbol": "DOGE/USDT",
            "timeframe": "1h",
            "parameters": "{}",
            "exchange_account_id": "",
            "user_id": str(user.id),
            "initial_balance": "10000",
            "status": status,
        },
    )
    return r


def _authed(user):
    token = AccessToken.for_user(user)
    return Client(), {"HTTP_AUTHORIZATION": f"Bearer {token}"}


@pytest.mark.django_db(transaction=True)
class TestRestoreHonoursDbStatus:
    def test_paused_session_not_restored_even_if_legacy_key_says_running(self):
        """Redis 兼容层说 running、DB 说 paused → 不恢复，并把兼容层纠正为 paused。"""
        user = _user("restore-owner")
        strategy = _strategy(user)
        running = _session(user, strategy, "running")
        paused = _session(user, strategy, "paused")

        r = _write_legacy(running.id, user)
        _write_legacy(paused.id, user)
        try:
            fm = FrameManager()
            fm._trading_state = FrameState.RUNNING
            fm._order_executor = MagicMock()

            started: list[str] = []

            async def fake_start(**kw):
                started.append(kw["live_session_id"])

            with patch.object(
                FrameManager, "start_strategy_runner", AsyncMock(side_effect=fake_start)
            ):
                async_to_sync(fm._restore_live_sessions)()

            assert started == [str(running.id)]
            assert r.hgetall(_legacy_key(paused.id))["status"] == "paused"
            assert r.hgetall(_legacy_key(running.id))["status"] == "running"
        finally:
            r.delete(_legacy_key(running.id), _legacy_key(paused.id))


@pytest.mark.django_db
class TestPauseResumeViews:
    def test_pause_stops_runner_and_sets_db_status(self):
        user = _user("pause-owner")
        session = _session(user, _strategy(user), "running")
        fm = MagicMock()
        fm.stop_strategy_runner = AsyncMock()

        with patch.object(FrameManager, "get_instance", classmethod(lambda cls: fm)):
            client, headers = _authed(user)
            resp = client.post(f"/api/trading/sessions/{session.id}/pause/", **headers)

        assert resp.status_code == 200
        session.refresh_from_db()
        assert session.status == "paused"
        fm.stop_strategy_runner.assert_awaited_once_with(
            live_session_id=str(session.id)
        )

    def test_resume_starts_runner_again(self):
        user = _user("resume-owner")
        session = _session(user, _strategy(user), "paused")
        fm = MagicMock()
        fm.start_live_session = AsyncMock()

        with patch.object(FrameManager, "get_instance", classmethod(lambda cls: fm)):
            client, headers = _authed(user)
            resp = client.post(f"/api/trading/sessions/{session.id}/resume/", **headers)

        assert resp.status_code == 200
        session.refresh_from_db()
        assert session.status == "running"
        fm.start_live_session.assert_awaited_once_with(str(session.id))

    def test_pause_rejects_non_running_session(self):
        user = _user("pause-reject")
        session = _session(user, _strategy(user), "stopped")
        fm = MagicMock()
        fm.stop_strategy_runner = AsyncMock()

        with patch.object(FrameManager, "get_instance", classmethod(lambda cls: fm)):
            client, headers = _authed(user)
            resp = client.post(f"/api/trading/sessions/{session.id}/pause/", **headers)

        assert resp.status_code == 400
        fm.stop_strategy_runner.assert_not_awaited()

    def test_pause_keeps_db_status_when_runner_stop_fails(self):
        """停 runner 失败不得回滚暂停（DB 为准，缓存由下次启动纠正）。"""
        user = _user("pause-fail")
        session = _session(user, _strategy(user), "running")
        fm = MagicMock()
        fm.stop_strategy_runner = AsyncMock(side_effect=RuntimeError("frame down"))

        with patch.object(FrameManager, "get_instance", classmethod(lambda cls: fm)):
            client, headers = _authed(user)
            resp = client.post(f"/api/trading/sessions/{session.id}/pause/", **headers)

        assert resp.status_code == 200
        session.refresh_from_db()
        assert session.status == "paused"
