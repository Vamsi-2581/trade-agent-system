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
    "wf_split_mode": "rolling",
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
        ("wf_split_mode", "WF_SPLIT_MODE", "anchored", "anchored"),
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
        ({"wf_split_mode": "rollingg"}, "拼错的分折模式必须炸，不静默回退"),
        ({"wf_split_mode": ""}, "空串不是一种分折模式"),
    ],
)
def test_bad_values_blow_up_at_construction(kwargs, reason):
    with pytest.raises(ValueError):
        FactoryConfig(**kwargs)


def test_split_mode_env_typo_fails_loud_and_does_not_fall_back():
    """拼错的分折模式**不能**静默变成滚动。

    静默回退在这里格外危险：它把「我要锚定扩张」变成一次没人察觉的滚动，而两种模式
    算出的衰减不同——门槛判定的输入就这样换了。
    """
    with pytest.raises(ValueError, match="WF_SPLIT_MODE"):
        FactoryConfig.from_env({ENV_PREFIX + "WF_SPLIT_MODE": "rollling"})
    # 大小写不敏感（与 `_as_bool` 的既有约定一致），但词必须是那两个之一。
    assert (
        FactoryConfig.from_env({ENV_PREFIX + "WF_SPLIT_MODE": "ANCHORED"}).wf_split_mode
        == "anchored"
    )


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


@pytest.mark.parametrize("raw", ["nan", "inf", "-inf", "NaN", "Infinity"])
def test_non_finite_numbers_are_never_thresholds(raw):
    """`nan` / `inf` 一律拒绝——这是**两道钱闸门同时消失**的那一类缺陷。

    与 NaN 的任何比较都是假，于是 `sharpe_min=nan` 让「夏普不足」恒不成立、
    `paper_min_return_pct=nan` 让「盈亏不达标」恒不成立。它们解析得出来，进程启动
    时一声不响，而漏斗从此放行一切。

    这与 `evaluate.py` 模块 docstring 第 4 条是**同一条纪律**：那一端堵判定输入里的
    NaN，这一端堵阈值里的 NaN；只堵一端，NaN 就从另一端进来。

    **必须断言报错里带着环境变量名**：`from_env` 只用 try 包住 `parser(raw)`，
    随后的 `cls(**kwargs)` 在 try 之外，所以 `__post_init__` 抛的错**不带**变量名。
    只断言「抛了 ValueError」的话，把解析器里的 isfinite 删掉也照样过
    （`__post_init__` 会兜住），但部署的人看到的就从
    「环境变量 STRATEGY_FACTORY_SHARPE_MIN=nan 无法解析」退化成一条不指名道姓的报错。
    带上变量名才证明是**解析层**挡住的。
    """
    for suffix in (
        "SHARPE_MIN",
        "OOS_DECAY_MAX_PCT",
        "PAPER_MIN_RETURN_PCT",
        "WF_OOS_FRACTION",
    ):
        key = ENV_PREFIX + suffix
        with pytest.raises(ValueError, match="有限数") as excinfo:
            FactoryConfig.from_env({key: raw})
        assert key in str(excinfo.value), f"报错必须点名 {key}"


@pytest.mark.parametrize("raw", ["2.5", "nan", "inf"])
def test_non_integer_values_are_rejected_for_count_fields(raw):
    """计数类字段只收整数：放 `2.5` 过去会在 `divmod` 深处炸成 TypeError。"""
    with pytest.raises(ValueError, match="整数"):
        FactoryConfig.from_env({ENV_PREFIX + "WF_FOLDS": raw})


@pytest.mark.parametrize(
    "kwargs",
    [
        {"sharpe_min": float("nan")},
        {"paper_min_return_pct": float("nan")},
        {"oos_decay_max_pct": float("inf")},
        {"wf_oos_fraction": float("nan")},
        {"wf_folds": 2.0},
        {"wf_folds": 2.5},
        {"min_trades": True},
        {"wf_folds": True},
        {"sharpe_min": True},
    ],
)
def test_non_finite_and_non_integer_are_rejected_on_direct_construction(kwargs):
    """**直接构造**（绕过 `from_env`）同样要挡住。

    `_as_float` / `_as_int` 只在环境变量那条路上；测试与将来的热重载会直接构造，
    只守环境变量那一条等于留了半个口子。
    """
    with pytest.raises(ValueError):
        FactoryConfig(**kwargs)


def test_nan_threshold_no_longer_lets_a_weak_strategy_through():
    """反向对照：把 NaN 挡在配置之外后，同一份判定输入**必须**被拦下。

    这条钉的是「修的不是解析器而是那道闸门」：若只加了 `_as_float` 的 isfinite
    而没管直接构造，这里会拿到一个 `oos_running`（放行）而不是 `in_sample_failed`。
    """
    from apps.strategy_factory.evaluate import (
        CandidateEvidence,
        InSampleEvidence,
        evaluate_candidate,
    )
    from apps.strategy_factory.status import StrategyCandidateStatus as S

    weak = CandidateEvidence(
        status=S.IN_SAMPLE_RUNNING,
        in_sample=InSampleEvidence(sharpe_ratio=0.01, total_trades=100),
    )
    verdict = evaluate_candidate(weak)
    assert verdict.next_status is S.IN_SAMPLE_FAILED
