"""配置面（spec #19 D19）。

测的不是「几个常量等于几」，而是**配置面作为一套机制是否成立**：

- 默认值就是 spec 表格里那一行（阈值的无声漂移必须变成一次失败）；
- 每个字段都能被环境变量覆盖，且**接线与字段互为穷举**（少接一个 = 那个环境变量
  静默失效，而「我明明设了」与「没生效」在运行时会表现成同一件事）；
- 空串 / 未设置都回落默认值（`.env` 里写了键没写值不是「阈值 = 0」）；
- 坏值在构造时就炸（import 级单例因此炸在进程启动，而不是炸在某条判定上）；
- frozen + `dataclasses.replace` 可派生 + `snapshot()` JSON-safe。
"""

from __future__ import annotations

import dataclasses
import json
import os

import pytest

from apps.strategy_factory import config
from apps.strategy_factory.config import (
    ENV_PREFIX,
    FactoryConfig,
    _ENV_FIELDS,
)

#: D19 表格里的期望默认值，逐条手抄。改默认值必须同时改这里 = 一次显式动作。
SPEC_DEFAULTS = {
    "sharpe_min": 1.5,
    "oos_decay_max_pct": 30.0,
    "min_trades": 30,
    "paper_observation_days": 14,
    "wf_folds": 5,
    "wf_oos_fraction": 0.2,
    "auto_advance_enabled": True,
    # D19 表格未给数、由判定与 #24 细化的两个 paper 门槛（见 config.py 类 docstring）
    "paper_min_return_pct": 0.0,
    "paper_max_risk_hits": 0,
}


def test_defaults_match_the_spec_table():
    cfg = FactoryConfig()
    for name, expected in SPEC_DEFAULTS.items():
        assert getattr(cfg, name) == expected, name
    assert set(SPEC_DEFAULTS) == set(FactoryConfig.__dataclass_fields__)


def test_env_wiring_covers_every_field():
    """`_ENV_FIELDS` 与字段集合互为穷举（config.py 的 `_self_check` 的同一条）。"""
    assert {name for name, _, _ in _ENV_FIELDS} == set(
        FactoryConfig.__dataclass_fields__
    )


def test_env_prefix_is_stable():
    """前缀是部署契约：改它等于让所有既有覆盖值一夜失效。"""
    assert ENV_PREFIX == "STRATEGY_FACTORY_"


@pytest.mark.parametrize(
    ("name", "suffix", "raw", "expected"),
    [
        ("sharpe_min", "SHARPE_MIN", "2.25", 2.25),
        ("oos_decay_max_pct", "OOS_DECAY_MAX_PCT", "12.5", 12.5),
        ("min_trades", "MIN_TRADES", "7", 7),
        ("paper_observation_days", "PAPER_OBSERVATION_DAYS", "21", 21),
        ("paper_min_return_pct", "PAPER_MIN_RETURN_PCT", "-1.5", -1.5),
        ("paper_max_risk_hits", "PAPER_MAX_RISK_HITS", "3", 3),
        ("wf_folds", "WF_FOLDS", "10", 10),
        ("wf_oos_fraction", "WF_OOS_FRACTION", "0.35", 0.35),
        ("auto_advance_enabled", "AUTO_ADVANCE_ENABLED", "false", False),
    ],
)
def test_every_threshold_can_be_overridden_by_env(name, suffix, raw, expected):
    cfg = FactoryConfig.from_env({ENV_PREFIX + suffix: raw})
    assert getattr(cfg, name) == expected
    # 其它字段保持默认：一个覆盖值只改一项。
    for other, default in SPEC_DEFAULTS.items():
        if other != name:
            assert getattr(cfg, other) == default, other


def test_bool_words_are_explicit():
    for raw in ("1", "true", "TRUE", "Yes", "on"):
        assert (
            FactoryConfig.from_env(
                {ENV_PREFIX + "AUTO_ADVANCE_ENABLED": raw}
            ).auto_advance_enabled
            is True
        )
    for raw in ("0", "false", "No", "OFF"):
        assert (
            FactoryConfig.from_env(
                {ENV_PREFIX + "AUTO_ADVANCE_ENABLED": raw}
            ).auto_advance_enabled
            is False
        )
    # `bool("false")` 是 True，所以拼错的写法必须炸，不能静默当开关没生效。
    with pytest.raises(ValueError, match="AUTO_ADVANCE_ENABLED"):
        FactoryConfig.from_env({ENV_PREFIX + "AUTO_ADVANCE_ENABLED": "flase"})


@pytest.mark.parametrize("raw", ["", "   "])
def test_blank_env_falls_back_to_default(raw):
    """空串是手滑，不是「阈值 = 0」——后者会让漏斗静默失效。"""
    cfg = FactoryConfig.from_env({ENV_PREFIX + "SHARPE_MIN": raw})
    assert cfg.sharpe_min == SPEC_DEFAULTS["sharpe_min"]


def test_unset_env_is_the_default():
    assert FactoryConfig.from_env({}) == FactoryConfig()


def test_unparsable_value_fails_loud_with_the_variable_name():
    with pytest.raises(ValueError, match="STRATEGY_FACTORY_SHARPE_MIN"):
        FactoryConfig.from_env({ENV_PREFIX + "SHARPE_MIN": "很高"})
    with pytest.raises(ValueError, match="STRATEGY_FACTORY_MIN_TRADES"):
        FactoryConfig.from_env({ENV_PREFIX + "MIN_TRADES": "30.5"})


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"sharpe_min": 0}, "夏普门槛取 0 会让样本内闸门恒真"),
        ({"sharpe_min": -1.0}, "负门槛"),
        ({"oos_decay_max_pct": 0}, "衰减上限 0 会把所有候选拦下"),
        ({"oos_decay_max_pct": 100.1}, "上限过百等于永不触发"),
        ({"min_trades": 0}, "样本量下限 0 = 没有证据门槛"),
        ({"paper_observation_days": 0}, "观察期 0 = 刚建出来就算跑满"),
        ({"paper_max_risk_hits": -1}, "命中上限不能为负"),
        ({"wf_folds": 1}, "单折没有样本外"),
        ({"wf_oos_fraction": 0}, "样本外占比 0 = 样本外为空"),
        ({"wf_oos_fraction": 1}, "样本外占比 1 = 样本内为空"),
    ],
)
def test_bad_values_blow_up_at_construction(kwargs, reason):
    with pytest.raises(ValueError):
        FactoryConfig(**kwargs)


def test_module_singleton_is_built_from_the_environment():
    """模块级 `CONFIG` 必须就是「按当前环境解析出来的那份」——否则环境变量写了没用。"""
    assert config.CONFIG == FactoryConfig.from_env(os.environ)
    assert config.get_config() is config.CONFIG


def test_config_is_frozen_and_replaceable():
    cfg = FactoryConfig()
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.sharpe_min = 9.0  # type: ignore[misc]
    derived = dataclasses.replace(cfg, sharpe_min=2.5)
    assert derived.sharpe_min == 2.5
    assert cfg.sharpe_min == 1.5, "派生不能改到原对象（否则测试之间会互相污染）"
    assert config.CONFIG.sharpe_min == SPEC_DEFAULTS["sharpe_min"]


def test_snapshot_is_json_safe_and_complete():
    snapshot = FactoryConfig().snapshot()
    assert snapshot == dataclasses.asdict(FactoryConfig())
    assert set(snapshot) == set(SPEC_DEFAULTS)
    # 要能原样进 JSONField：判定结论里嵌的就是它。
    assert json.loads(json.dumps(snapshot, sort_keys=True)) == snapshot
