"""候选策略流水线的**唯一配置面**（spec #19 D19）。

形状沿 `apps/regime/config.py`：frozen dataclass + import 时自校验 + 模块级单例。
与之的差别只有一处，也是本票明确要求的：**每个字段都能被环境变量覆盖**——调门槛
（夏普、衰减上限、观察期）不该为了换个数字改代码发版，而 `apps/regime` 那套
「只读、改阈值必须发版」是它自己的场景（机制阈值要一起审计），不是这里的场景。

三条纪律：

1. **单一来源**。所有阈值只在这里写一遍。判定纯函数吃的是这个对象，编排、任务、
   API 都不许自己再抄一份常量——抄一份的那一刻起，「漏斗的门槛」就有两个答案了。

2. **自校验**。数值边界在 import 时检查（`__post_init__`），坏配置在进程启动时就炸，
   而不是先安静地跑出一批奇怪判定、等有人翻记录才发现。环境变量解析失败同样在
   import 时炸（模块级 `CONFIG = FactoryConfig.from_env()`）。

3. **可快照**。判定结论要回答「当时用的是哪套参数」，所以配置必须能变成 JSON-safe
   的字面量进 `StrategyCandidate.evidence`（`snapshot()`）。为此它是 frozen
   dataclass —— `dataclasses.replace` 可派生，测试改阈值不必动全局。

## 环境变量清册

| 环境变量 | 字段 | 默认 |
|---|---|---|
| `STRATEGY_FACTORY_SHARPE_MIN` | `sharpe_min` | 1.5 |
| `STRATEGY_FACTORY_OOS_DECAY_MAX_PCT` | `oos_decay_max_pct` | 30 |
| `STRATEGY_FACTORY_MIN_TRADES` | `min_trades` | 30 |
| `STRATEGY_FACTORY_PAPER_OBSERVATION_DAYS` | `paper_observation_days` | 14 |
| `STRATEGY_FACTORY_PAPER_MIN_RETURN_PCT` | `paper_min_return_pct` | 0 |
| `STRATEGY_FACTORY_PAPER_MAX_RISK_HITS` | `paper_max_risk_hits` | 0 |
| `STRATEGY_FACTORY_WF_FOLDS` | `wf_folds` | 5 |
| `STRATEGY_FACTORY_WF_OOS_FRACTION` | `wf_oos_fraction` | 0.2 |
| `STRATEGY_FACTORY_AUTO_ADVANCE_ENABLED` | `auto_advance_enabled` | true |

环境变量**留空或未设置都算「用默认值」**：空串是一次手滑（`.env` 里写了键没写值），
把它当成「阈值 = 0」是最坏的一种解释——夏普门槛取 0 会让漏斗静默失效。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

#: 环境变量前缀。改它等于让所有部署的既有覆盖值一夜失效，所以它是个常量。
ENV_PREFIX = "STRATEGY_FACTORY_"


def _as_float(raw: str) -> float:
    return float(raw)


def _as_int(raw: str) -> int:
    return int(raw)


#: 布尔值的可接受写法。**不接受任意字符串**：`bool("false")` 是 True，
#: 而「我明明写了 false」与「开关根本没生效」在运行时会表现成同一件事。
_TRUE_WORDS = frozenset({"1", "true", "yes", "on"})
_FALSE_WORDS = frozenset({"0", "false", "no", "off"})


def _as_bool(raw: str) -> bool:
    value = raw.strip().lower()
    if value in _TRUE_WORDS:
        return True
    if value in _FALSE_WORDS:
        return False
    raise ValueError(
        f"只接受 {sorted(_TRUE_WORDS)} / {sorted(_FALSE_WORDS)}（不区分大小写）"
    )


#: 字段 → (环境变量后缀, 解析器)。**这张表是唯一的环境变量接线处**，且由
#: `_self_check()` 保证它与 dataclass 的字段集合互为穷举：加了字段忘了接线会在
#: import 时炸，而不是表现为「这个字段的环境变量静默无效」。
_ENV_FIELDS: tuple[tuple[str, str, Any], ...] = (
    ("sharpe_min", "SHARPE_MIN", _as_float),
    ("oos_decay_max_pct", "OOS_DECAY_MAX_PCT", _as_float),
    ("min_trades", "MIN_TRADES", _as_int),
    ("paper_observation_days", "PAPER_OBSERVATION_DAYS", _as_int),
    ("paper_min_return_pct", "PAPER_MIN_RETURN_PCT", _as_float),
    ("paper_max_risk_hits", "PAPER_MAX_RISK_HITS", _as_int),
    ("wf_folds", "WF_FOLDS", _as_int),
    ("wf_oos_fraction", "WF_OOS_FRACTION", _as_float),
    ("auto_advance_enabled", "AUTO_ADVANCE_ENABLED", _as_bool),
)


@dataclass(frozen=True)
class FactoryConfig:
    """漏斗的全部阈值与开关（D19）。

    **为什么不叫 `StrategyFactoryConfig`**：那个名字已经是 `apps.py` 里的 Django
    `AppConfig` 子类（Django 的命名约定）。同一个 app 里两个不同的东西共用一个名字，
    迟早有人在同一个文件里 import 两个而只拿到一个——本仓库为「同名不同义会误导」
    已经避让过一次（`origin` 不叫 `source`），这里照同一条纪律办。

    取值理由，逐条：

    - `sharpe_min=1.5` 与 `oos_decay_max_pct=30`：CONTEXT.md「策略工厂共识」Q1 的
      可靠标准原文（夏普 ≥1.5、样本外衰减 <30%），**不是这里拍的数**。
    - `min_trades=30`：沿用仓库既有证据门槛（CONTEXT.md 第 102 条、
      `apps/trading/session_stats.py::SAMPLE_THRESHOLD`）。与那两处同源，别处改了
      这里也要跟着改——判定纯函数用它，是为了让「4 笔的胜率」在任何一层都读不成结论。
    - `paper_observation_days=14`：Q1「paper-trade 2 周」。
    - `paper_min_return_pct=0.0` / `paper_max_risk_hits=0`：D8 要求「盈亏门槛、风控
      命中上限」都是配置项，但共识里只有「通过风控」这句话、没有给出数字。取最保守
      的一侧（不亏 + 一次风控都不许命中），因为**放行**的代价是真金白银，堵住的代价
      只是候选多跑一阵。这两个数由 #24（paper 观察期判定）细化——它会带来
      `RiskEvent` 的严重度口径，届时若确认「轻微命中不算超标」，改的是这两个默认值
      加一条注释，而不是判定逻辑的结构。
    - `wf_folds=5` / `wf_oos_fraction=0.2`：spec D19 把这两个数标为「待定 / 末尾一段」，
      分折实现归 #29。这里先给出可用的默认（5 折滚动、样本外占末尾 20%），**#29 若
      另有结论就改这里**：默认值只有一处，改了不会漂。
    - `auto_advance_enabled=True`：Q5 已裁定「自动升 paper」，这是 D19 明确写下的默认。
      安全性不靠它默认关，而靠三件事：漏斗下限是「什么都不发生」、每层可 `cancelled`、
      有总开关。
    """

    sharpe_min: float = 1.5
    oos_decay_max_pct: float = 30.0
    min_trades: int = 30
    paper_observation_days: int = 14
    paper_min_return_pct: float = 0.0
    paper_max_risk_hits: int = 0
    wf_folds: int = 5
    wf_oos_fraction: float = 0.2
    auto_advance_enabled: bool = True

    def __post_init__(self) -> None:
        if self.sharpe_min <= 0:
            # 门槛 <= 0 时「夏普不足」这个判据恒假，漏斗最外面那道闸门静默消失。
            raise ValueError(
                f"sharpe_min 必须为正，当前 {self.sharpe_min}"
                "——取 0 会让「夏普不足」永远不成立，样本内闸门形同虚设"
            )
        if not 0 < self.oos_decay_max_pct <= 100:
            # 上限 >100% 等于永不触发（衰减再狠也拦不住）；<=0 等于一律拦下。
            raise ValueError(
                f"oos_decay_max_pct 落在 (0, 100]，当前 {self.oos_decay_max_pct}"
                "——上限 100% 以上时「衰减超标」恒假，样本外闸门形同虚设"
            )
        if self.min_trades < 1:
            raise ValueError(f"min_trades 必须 >= 1，当前 {self.min_trades}")
        if self.paper_observation_days < 1:
            raise ValueError(
                f"paper_observation_days 必须 >= 1，当前 {self.paper_observation_days}"
                "——取 0 会让「刚建出来的 paper 会话」立刻算观察期满"
            )
        if self.paper_max_risk_hits < 0:
            raise ValueError(
                f"paper_max_risk_hits 不能为负，当前 {self.paper_max_risk_hits}"
            )
        if self.wf_folds < 2:
            # 1 折没有「样本外」可言；D15 的滚动窗口至少要两折才谈得上分折。
            raise ValueError(
                f"wf_folds 必须 >= 2，当前 {self.wf_folds}"
                "——单折没有样本外，衰减无从算起"
            )
        if not 0 < self.wf_oos_fraction < 1:
            raise ValueError(
                f"wf_oos_fraction 落在开区间 (0, 1)，当前 {self.wf_oos_fraction}"
                "——它是比例，0 或 1 会让样本内/样本外其中一侧为空"
            )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "FactoryConfig":
        """按 `_ENV_FIELDS` 覆盖默认值。

        `env` 可注入（测试传一个 dict 就不必碰进程环境，也不会污染别的用例）。
        解析失败直接抛 `ValueError` 并带上变量名——模块级单例在 import 时炸掉，
        这是刻意的：坏配置不该活到有人去看判定结果的那一刻。
        """
        source = os.environ if env is None else env
        kwargs: dict[str, Any] = {}
        for field_name, suffix, parser in _ENV_FIELDS:
            key = ENV_PREFIX + suffix
            raw = source.get(key)
            if raw is None or raw.strip() == "":
                continue
            try:
                kwargs[field_name] = parser(raw)
            except ValueError as exc:
                raise ValueError(f"环境变量 {key}={raw!r} 无法解析：{exc}") from exc
        return cls(**kwargs)

    def snapshot(self) -> dict[str, Any]:
        """JSON-safe 的参数快照，供判定结论内嵌（“这层判定用的是哪套参数”）。

        全部字段都是 int / float / bool，`asdict` 的结果可以直接进 `JSONField`。
        """
        return asdict(self)


def _self_check() -> None:
    """配置字段与环境变量接线互为穷举。"""
    fields = set(FactoryConfig.__dataclass_fields__)
    wired = {name for name, _, _ in _ENV_FIELDS}
    if fields != wired:
        raise RuntimeError(
            f"配置面与环境变量接线不一致：字段 {sorted(fields)}，接线 {sorted(wired)}"
            "——少接的那一项会让它的环境变量静默失效（写了等于没写）"
        )
    suffixes = [suffix for _, suffix, _ in _ENV_FIELDS]
    if len(set(suffixes)) != len(suffixes):
        raise RuntimeError(f"环境变量后缀重复：{suffixes}")


_self_check()

#: 进程级唯一实例。**整个流水线读的都是它**：判定、任务、API 都从这里取，
#: 不各自 `from_env()`（那会造出第二份真相，且各自解析的时刻还不一样）。
CONFIG = FactoryConfig.from_env()


def get_config() -> FactoryConfig:
    """取当前生效的配置。

    留一个函数而不是让调用方直接 import `CONFIG`：将来若要支持「运行中重载」
    （比如管理命令热调阈值），改这一个函数即可，调用方的 import 形式不必跟着变。
    """
    return CONFIG
