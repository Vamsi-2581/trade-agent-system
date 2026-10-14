"""实盘参数下发：`params_schema` 默认值必须与回测同源注入（2026-10-12 SOL/USDT 审计）。

事故形状：`StrategyRunner.start_live` 当时只做 `params.update(parameters)`，**不注入**
`params_schema` 默认值；而 `run_backtest` 注入。于是会话 config 里没写的键不会走
"策略声明的默认值"，而是落到策略 `__init__` 里各自的 `.get(key, fallback)` 兜底。

SOL/USDT 那单就是这么跑的：`live_sessions.config` 里没有 `volume_filter`，实盘没注入
schema 默认值，策略兜底 `False` ⇒ **量能门全程关闭**，而部署界面显示的是声明默认值。
两处默认值分家时，会话就是按一个界面上看不到的参数在跑。

验收合约：
  1. 实盘与回测拿到**同一份** params（同一函数，同源注入）
  2. schema 默认值被注入，会话 config 覆盖优先
  3. 未声明的 config 键照旧透传（无白名单，与 session_params.effective_params 口径一致）
  4. 策略没有 params_schema 时不炸
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from apps.strategy_engine.runner import resolve_params


class _FakeStrategy:
    params_schema = {
        "lookback": {"type": "int", "default": 120},
        "volume_filter": {"type": "bool", "default": False},
        "volume_mult": {"type": "float", "default": 1.0},
        "exit_at_mid": {"type": "bool", "default": True},
    }


def test_schema_defaults_are_injected_when_config_is_empty():
    """空 config ≠ 没有参数——是"按策略声明的默认值跑"，必须与界面显示的一致。"""
    params = resolve_params(_FakeStrategy, {})

    assert params["lookback"] == 120
    assert params["volume_filter"] is False
    assert params["volume_mult"] == 1.0
    assert params["exit_at_mid"] is True


def test_session_config_overrides_schema_defaults():
    """会话显式覆盖优先于声明默认值（SOL 会话的 lookback=240 就覆盖了默认 120）。"""
    params = resolve_params(
        _FakeStrategy,
        {"lookback": 240, "exit_at_mid": 0, "position_pct": 0.3},
    )

    assert params["lookback"] == 240, "会话覆盖必须生效"
    assert params["exit_at_mid"] == 0
    assert params["volume_filter"] is False, "未覆盖的键取声明默认值"


def test_undeclared_config_keys_pass_through():
    """无白名单：config 里多出来的键照旧到达 context.params。

    与 `session_params.effective_params` 的 `declared=False` 行同一口径——
    那边把这类键列出来，是因为它们**确实**会到策略手上。
    """
    params = resolve_params(_FakeStrategy, {"position_pct": 0.3})

    assert params["position_pct"] == 0.3


def test_none_parameters_is_treated_as_empty():
    params = resolve_params(_FakeStrategy, None)

    assert params["volume_filter"] is False
    assert "position_pct" not in params


def test_strategy_without_schema_does_not_break():
    """没有 params_schema 的策略（或非 dict）不得抛异常。"""
    no_schema = SimpleNamespace()

    assert resolve_params(no_schema, {"a": 1}) == {"a": 1}
    assert resolve_params(no_schema, None) == {}

    class _BadSchema:
        params_schema = None

    assert resolve_params(_BadSchema, {"a": 1}) == {"a": 1}


def test_non_dict_schema_entry_is_passed_through():
    """schema 里某一项不是 dict 时按原值取（与旧实现一致，不引入新约束）。"""

    class _Odd:
        params_schema = {"weird": 42, "normal": {"default": 7}}

    params = resolve_params(_Odd, {})

    assert params["weird"] == 42
    assert params["normal"] == 7


@pytest.mark.parametrize("mode", ["backtest", "live"])
def test_both_modes_share_one_resolver(mode):
    """回测与实盘必须走同一个解析函数——分家正是这次事故的根因。

    这里直接锁住"两处调用的是同一份实现"：改任何一侧都必须同时改另一侧。
    """
    import inspect

    from apps.strategy_engine import runner as runner_mod

    src = inspect.getsource(runner_mod.StrategyRunner)
    assert src.count("resolve_params(strategy_cls, parameters)") == 2, (
        "run_backtest 与 start_live 都必须调用 resolve_params"
    )
