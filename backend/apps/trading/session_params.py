"""会话生效参数（CONTEXT.md Glossary「会话生效参数」）。

runner 拿到的是 `params.update(session.config)` 之后的那一份（`strategy_engine/runner.py`
的 live 分支），这份模块把它按「策略声明了什么 / 本会话覆盖了什么 / 实际传下去的是什么」
三列摊开——因为单看 `LiveSession.config` 会得到一个**反过来的**结论：它是空的（部署弹窗
根本不发这个字段，`views.py` 只是把请求体透传），而会话照样在按策略默认参数跑。

**刻意不声称「策略读取了每一个键」**：`params_schema` 是策略**声明**的参数集，哪个键真的
被实现读到只有策略代码自己知道（同一个策略里 `quantity` 与 `position_pct` 都声明了，
而按资金比例开仓的那条路径只读后者）。所以行里带 `declared` 一列，且词条里写死了这句话。

策略类解析不到时（实现类被删/改名，或注册表在这个进程里没扫到）**不猜**：返回
`resolved=False` 与 config 原文，由前端如实说明。
"""

from __future__ import annotations

from typing import Any


def _params_schema(strategy) -> tuple[dict[str, Any], bool]:
    """策略类的 `params_schema`，以及「解析到了没有」。

    `discover()` **只在注册表里没有这个名字时才调**，两个理由：

    * 它不便宜（`os.listdir` + 每个文件 importlib 重导入），而这是一个每 10 秒被轮询
      一次的读接口；
    * 更要紧的是**它显示的东西必须与 runner 用的一致**。runner 走的是
      `StrategyRegistry.get_class()`，即进程里已有的那个类；这里若每次都重新扫描，表里
      就会显示一个比 runner 手上更新的 `params_schema`——而「参数表说的」与「实际跑的」
      分家正是这个栏目最该避免的事。

    不扫的话注册表可能是空的：`strategy_engine/apps.py:ready()` 只在
    `~/.tradelogx/strategies` **当时就存在**时才扫描（`regime/pool_rebuild.py` 记着
    这个坑）。那种情况下名字必然不在表里，于是这里会扫一次——与
    `agent/tools/list_strategies.py` 同样的兜底。
    """
    from apps.strategy_engine.registry import StrategyRegistry

    if strategy.name not in StrategyRegistry.list_registered():
        StrategyRegistry.discover()

    try:
        strategy_cls = StrategyRegistry.get_class(strategy.name)
    except Exception:
        return {}, False

    schema = getattr(strategy_cls, "params_schema", None)
    if not isinstance(schema, dict):
        return {}, True
    return schema, True


def effective_params(strategy, config: dict | None = None) -> dict:
    """`(策略声明 ⊕ 会话覆盖)` → 详情页要的那张表。

    多出来的 config 键**也列出来**（`declared=False`）：`runner` 走的是
    `params.update(parameters)`，没有白名单，所以这些键确实会到达 `context.params`。
    把它们藏起来，表就成了「策略声明的参数」而不是「实际传下去的集合」。
    """
    config = config or {}
    strategy_name = getattr(strategy, "name", None)
    schema, resolved = _params_schema(strategy)

    if not resolved:
        return {
            "resolved": False,
            "strategy_name": strategy_name,
            "rows": [],
            "config": config,
        }

    rows: list[dict[str, Any]] = []
    for key, spec in schema.items():
        default = spec.get("default") if isinstance(spec, dict) else None
        overridden = key in config
        rows.append(
            {
                "key": key,
                "declared": True,
                "default": default,
                "override": config.get(key) if overridden else None,
                "effective": config[key] if overridden else default,
            }
        )
    for key, value in config.items():
        if key in schema:
            continue
        rows.append(
            {
                "key": key,
                "declared": False,
                "default": None,
                "override": value,
                "effective": value,
            }
        )

    return {
        "resolved": True,
        "strategy_name": strategy_name,
        "rows": rows,
        "config": config,
    }
