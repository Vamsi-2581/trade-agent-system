"""断路径收窄与清扫：三个入口都不再能创建 `trade` / `notify_and_trade`。

`trade` / `notify_and_trade` 是一条**断路径**：`_execute_trade` 只做一次
`Order.objects.create()`，全仓没有任何消费者读这些行，订单永久停在
`status="pending"`、`exchange_order_id=""`，没有卡单告警也没有清理任务。
处置分三步（修改6，2026-10-12 收尾）：①收窄三个入口（第①段强制前置）；
②删除引擎 `trade` / `notify_and_trade` 分支与 `_execute_trade`；
③migration 0004 把存量 trade 行迁移为 notify 并收窄模型 choices。
本文件钉住的是 ①+③ 的入口边界；② 在 test_engine.py 钉死。

数是**三**个不是两个：创建序列化器、更新序列化器（`patch` 能把既有 notify 型改成
trade）、以及 `import/backtest/<id>/`——最后这个按请求体自由 JSON 取 `action_type`，
完全不经过 ChoiceField，是三者中最宽的一条缝。
"""

from __future__ import annotations

import datetime
import uuid
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from apps.backtest.models import BacktestResult
from apps.signal_monitor.models import SignalMonitor
from apps.trading.models import Strategy

User = get_user_model()

CREATE_URL = "/api/signal-monitor/"
IMPORT_URL = "/api/signal-monitor/import/backtest/{}/"

VALID_SIGNAL = {
    "name": "BTC RSI",
    "symbol": "BTC/USDT",
    "indicator_type": "rsi",
    "indicator_params": {"period": 14},
    "condition": {"operator": "lt", "left": {"field": ""}, "right": {"value": 30}},
}


def _user(username: str):
    return User.objects.create_user(
        username=username, email=f"{username}@example.com", password="pw12345"
    )


def _authed_client(user):
    """必须用 APIClient + format="json"：Django 原生 Client 会把嵌套 dict 压成
    字符串，`signals[].action_type` 那条断言就会因为「根本不是 dict」而假通过。"""
    client = APIClient()
    token = AccessToken.for_user(user)
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    return client


def _backtest(user):
    strategy = Strategy.objects.create(name=f"bt-{uuid.uuid4().hex[:8]}", code_path="strategies/s.py")
    return BacktestResult.objects.create(
        strategy=strategy,
        user=user,
        symbol="BTC/USDT",
        timeframe="1h",
        start_date=datetime.date(2026, 1, 1),
        end_date=datetime.date(2026, 2, 1),
        initial_capital=Decimal("1000"),
        final_capital=Decimal("1100"),
        total_return_pct=10.0,
        review_status="approved",
    )


@pytest.mark.django_db
class TestCreateAndUpdateEntryPoints:
    @pytest.mark.parametrize("bad", ["trade", "notify_and_trade", "validate_strategy", "garbage"])
    def test_create_rejects_non_notify(self, bad):
        """入口①：创建序列化器。`validate_strategy` 由 live_mode 内部写入，不由用户创建"""
        client = _authed_client(_user(f"create-{bad}"))

        resp = client.post(CREATE_URL, {**VALID_SIGNAL, "action_type": bad}, format="json")

        assert resp.status_code == 400, resp.content
        assert not SignalMonitor.objects.exists()

    def test_create_defaults_to_notify(self):
        client = _authed_client(_user("create-default"))

        resp = client.post(CREATE_URL, VALID_SIGNAL, format="json")

        assert resp.status_code == 201, resp.content
        assert SignalMonitor.objects.get().action_type == "notify"

    def test_update_rejects_non_notify(self):
        """入口②：更新序列化器——不堵这里，`patch` 会把既有 notify 型改成 trade"""
        user = _user("update-owner")
        client = _authed_client(user)
        monitor = SignalMonitor.objects.create(user=user, action_type="notify", **VALID_SIGNAL)

        resp = client.patch(
            f"/api/signal-monitor/{monitor.id}/", {"action_type": "trade"}, format="json"
        )

        assert resp.status_code == 400, resp.content
        monitor.refresh_from_db()
        assert monitor.action_type == "notify"


@pytest.mark.django_db
class TestImportEntryPoint:
    """入口③：按请求体自由 JSON 取值，不经 ChoiceField —— 最宽的一条缝。"""

    @pytest.mark.parametrize("bad", ["trade", "notify_and_trade", "validate_strategy", "garbage"])
    def test_import_rejects_non_notify(self, bad):
        user = _user(f"import-{bad}")
        client = _authed_client(user)
        backtest = _backtest(user)

        resp = client.post(
            IMPORT_URL.format(backtest.id),
            {"signals": [{**VALID_SIGNAL, "action_type": bad}]},
            format="json",
        )

        assert resp.status_code == 400, resp.content
        assert not SignalMonitor.objects.exists(), "整批都不该落库"

    def test_import_rejects_when_any_signal_is_bad(self):
        """一批里有非法值 → 整批拒绝，不做部分成功"""
        user = _user("import-partial")
        client = _authed_client(user)
        backtest = _backtest(user)

        resp = client.post(
            IMPORT_URL.format(backtest.id),
            {"signals": [VALID_SIGNAL, {**VALID_SIGNAL, "action_type": "trade"}]},
            format="json",
        )

        assert resp.status_code == 400, resp.content
        assert not SignalMonitor.objects.exists()

    def test_import_defaults_to_notify(self):
        user = _user("import-default")
        client = _authed_client(user)
        backtest = _backtest(user)

        resp = client.post(
            IMPORT_URL.format(backtest.id), {"signals": [VALID_SIGNAL]}, format="json"
        )

        assert resp.status_code == 201, resp.content
        assert SignalMonitor.objects.get().action_type == "notify"


@pytest.mark.django_db
class TestActionChoicesNarrowed:
    def test_model_choices_exclude_trade_actions(self):
        """修改6：引擎 trade 分支已删除，模型 choices 同步收窄到 notify +
        validate_strategy，库内不存在不可触达的动作类型。"""
        values = [choice[0] for choice in SignalMonitor.ACTION_TYPE_CHOICES]
        assert "trade" not in values
        assert "notify_and_trade" not in values
        assert set(values) == {"notify", "validate_strategy"}
