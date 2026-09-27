"""会话生效参数（CONTEXT.md Glossary「会话生效参数」）。

被测的是那张表**据实说了什么**：策略声明了哪些键、本会话覆盖了哪些、实际传下去的集合
是什么。要防的两种失效都长得很正常——

1. 只显示 `LiveSession.config` ⇒ 一条按默认参数跑着的会话显示成「无参数」，而它明明在
   按 `position_pct=0.1` 开仓；
2. 把多出来的 config 键藏起来 ⇒ 表变成「策略声明的参数」，而 runner 走的是
   `params.update(parameters)`（没有白名单），那些键确实到达了 `context.params`。

**一个进程内的注册表**：用 `monkeypatch.setitem` 挂一个假策略类进去，退出时自动还原——
不碰真的 `~/.tradelogx/strategies`，也不给生产代码加一个只为测试存在的 `unregister`。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from apps.strategy_engine.registry import StrategyRegistry
from apps.trading.session_params import effective_params

FAKE_NAME = "test_only_fake_strategy"


class _FakeStrategy:
    """参数表照 `strategies/rsi_cross.py` 的形状：声明键未必都被实现读取。"""

    name = FAKE_NAME
    params_schema = {
        "rsi_period": {"type": "integer", "default": 14},
        "position_pct": {"type": "number", "default": 0.1},
    }


@pytest.fixture
def registered_fake(monkeypatch):
    monkeypatch.setitem(StrategyRegistry._strategies, FAKE_NAME, _FakeStrategy)


def _strategy(name: str = FAKE_NAME) -> SimpleNamespace:
    return SimpleNamespace(name=name)


def test_defaults_are_shown_when_the_session_overrides_nothing(
    registered_fake,
):
    """空 config 不是「没有参数」——是「按策略默认值跑」，两者必须看起来不一样。"""
    params = effective_params(_strategy(), {})

    assert params["resolved"] is True
    rows = {row["key"]: row for row in params["rows"]}
    assert rows["rsi_period"]["default"] == 14
    assert rows["rsi_period"]["override"] is None
    assert rows["rsi_period"]["effective"] == 14
    assert rows["position_pct"]["effective"] == 0.1


def test_a_session_override_replaces_the_default(registered_fake):
    params = effective_params(_strategy(), {"position_pct": 0.25})

    row = {r["key"]: r for r in params["rows"]}["position_pct"]
    assert row["default"] == 0.1
    assert row["override"] == 0.25
    assert row["effective"] == 0.25


def test_an_undeclared_config_key_is_listed_and_marked(registered_fake):
    """config 里的键不在声明表里也照列——它与声明键的**区别**是 `declared`，不是隐藏。

    `params.update(parameters)` 没有白名单，所以这个键真的会到策略手上。
    """
    params = effective_params(_strategy(), {"stop_loss_pct": 0.05})

    row = {r["key"]: r for r in params["rows"]}["stop_loss_pct"]
    assert row["declared"] is False
    assert row["default"] is None
    assert row["effective"] == 0.05
    assert row["override"] == 0.05


def test_an_unresolvable_strategy_falls_back_to_the_raw_config():
    """解析不到实现类时不猜：只回 config 原文，由前端如实说明。

    这条路径在生产里不是假设——`strategy_engine/apps.py:ready()` 只在策略目录当时就存在
    时才扫描，长驻的 web 进程里注册表可能是空的（`regime/pool_rebuild.py` 记着这个坑）。
    """
    params = effective_params(_strategy("no_such_strategy_anywhere"), {"a": 1})

    assert params["resolved"] is False
    assert params["rows"] == []
    assert params["config"] == {"a": 1}
