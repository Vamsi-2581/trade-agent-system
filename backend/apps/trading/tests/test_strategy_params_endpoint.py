"""策略声明参数表接口（2026-10-12 SOL/USDT 审计的 P3）。

要防的失效：部署会话时**没有任何入口**能把某个参数显式打开。SOL 会话
`config` 里没有 `volume_filter`，于是量能门全程关闭，而部署弹窗根本不发
`config`——不是界面显示错了，是压根没地方设。

这个接口只回答"策略允许你设什么"（`params_schema` 原文），**不**与任何会话的
config 合并——"这条会话实际在跑什么"是 `live_session_detail` 的 `params` 字段
（`session_params.effective_params`）负责的。两个接口各说各的一件事，
混起来就又变成那种"分不清声明与生效"的表。

与 `test_session_params.py` 同一手法：用 `monkeypatch.setitem` 挂假策略类进注册表，
退出时自动还原——不碰真的 `~/.tradelogx/strategies`。
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.strategy_engine.registry import StrategyRegistry

FAKE_NAME = "test_only_params_endpoint_strategy"
URL = f"/api/trading/strategies/{FAKE_NAME}/params/"


class _FakeStrategy:
    name = FAKE_NAME
    params_schema = {
        "lookback": {"type": "int", "default": 120, "description": "箱体检测窗口K线数"},
        "position_pct": {
            "type": "float",
            "default": 0.1,
            "min": 0.0,
            "max": 1.0,
            "description": "每次开仓占总资金比例",
        },
        # 布尔型：SOL 审计里被静默关闭的那个键
        "volume_filter": {
            "type": "bool",
            "default": False,
            "description": "True=启用突破 K 线量能过滤",
        },
    }


class _NoSchemaStrategy:
    name = "test_only_no_schema_strategy"
    params_schema = None


@pytest.fixture
def registered_fake(monkeypatch):
    monkeypatch.setitem(StrategyRegistry._strategies, FAKE_NAME, _FakeStrategy)
    monkeypatch.setitem(
        StrategyRegistry._strategies, "test_only_no_schema_strategy", _NoSchemaStrategy
    )


@pytest.fixture
def client(db):
    user = get_user_model().objects.create_user(
        username="params-endpoint-user",
        email="params-endpoint-user@example.com",
        password="pw12345",
    )
    c = APIClient()
    c.force_authenticate(user=user)
    return c


def test_declared_params_are_listed_with_defaults_and_descriptions(
    registered_fake, client
):
    """声明的每个键都要带默认值与描述——部署弹窗靠它渲染可覆盖的字段。"""
    resp = client.get(URL)

    assert resp.status_code == 200
    body = resp.json()
    assert body["resolved"] is True
    assert body["strategy_name"] == FAKE_NAME

    by_key = {p["key"]: p for p in body["params"]}
    assert set(by_key) == {"lookback", "position_pct", "volume_filter"}
    assert by_key["lookback"]["default"] == 120
    assert by_key["lookback"]["type"] == "int"
    assert by_key["lookback"]["description"] == "箱体检测窗口K线数"
    assert by_key["position_pct"]["min"] == 0.0
    assert by_key["position_pct"]["max"] == 1.0


def test_boolean_param_is_offered_even_though_its_default_is_false(
    registered_fake, client
):
    """`volume_filter` 默认 False，但必须**出现在表里**。

    这正是 SOL 审计的症结：默认关闭 + 没有入口打开 = 该功能形同不存在。
    接口把声明原样列出（含 False 默认），前端才有东西可渲染。
    """
    body = client.get(URL).json()
    row = {p["key"]: p for p in body["params"]}["volume_filter"]

    assert row["default"] is False
    assert row["type"] == "bool"
    assert "量能" in row["description"]


def test_unknown_strategy_returns_404(registered_fake, client):
    resp = client.get("/api/trading/strategies/no_such_strategy_at_all/params/")

    assert resp.status_code == 404
    assert "not resolvable" in resp.json()["error"]


def test_strategy_without_schema_returns_empty_list_not_error(registered_fake, client):
    """没有 params_schema 的策略是合法状态（不是错误）：返回空表。"""
    resp = client.get("/api/trading/strategies/test_only_no_schema_strategy/params/")

    assert resp.status_code == 200
    body = resp.json()
    assert body["resolved"] is True
    assert body["params"] == []


def test_non_dict_schema_entry_is_still_listed(registered_fake, client, monkeypatch):
    """schema 里某项不是 dict 时按原值列出——与 resolve_params 的宽松口径一致。"""

    class _Odd:
        name = "test_only_odd_schema_strategy"
        params_schema = {"weird": 42}

    monkeypatch.setitem(
        StrategyRegistry._strategies, "test_only_odd_schema_strategy", _Odd
    )
    body = client.get(
        "/api/trading/strategies/test_only_odd_schema_strategy/params/"
    ).json()

    row = {p["key"]: p for p in body["params"]}["weird"]
    assert row["default"] == 42


def test_endpoint_requires_authentication(registered_fake):
    resp = APIClient().get(URL)

    assert resp.status_code in (401, 403), "参数表不是公开信息"


def test_trailing_slash_form_is_the_one_that_serves(registered_fake, client):
    """带尾斜杠的形式是真正被服务的那个——这条钉住前后端拼接的接缝。

    链路（`next.config.ts` 的 `beforeFiles` 重写）：
        前端 `request()` 剥掉尾斜杠 → `/api/.../params`
        → Next 重写**补回**尾斜杠 → 后端 `/api/.../params/` → 命中本视图

    所以后端只认带尾斜杠的形式；不带时 Django 的 APPEND_SLASH 给 301，
    浏览器/代理会跟随。两条都断言，避免哪天重写规则改了而这里没人发现。
    """
    ok = client.get(URL)
    assert ok.status_code == 200
    assert ok.json()["strategy_name"] == FAKE_NAME

    slashless = client.get(URL.rstrip("/"))
    assert slashless.status_code == 301, (
        "无尾斜杠应由 APPEND_SLASH 301 到带斜杠形式（Next 重写会先补回斜杠，"
        "所以生产链路不会走到这个 301）"
    )
    assert slashless["Location"] == URL
