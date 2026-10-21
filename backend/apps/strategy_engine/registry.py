"""
策略注册中心（MVP 精简版）

复用 AgentRegistry 的装饰器注册模式。
策略类使用 @register_strategy 装饰器自动注册。
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .base import BaseStrategy

logger = logging.getLogger(__name__)


def _short_enabled() -> bool:
    """双向持仓策略的准入开关（默认关闭）。

    读 Django setting ``STRATEGY_ENGINE_ALLOW_SHORT``，未配置时回退环境变量
    ``STRATEGY_ENGINE_ALLOW_SHORT``。只在注册期读一次，不做热更新——启用做空
    应该是一次重启 + 显式配置的动作，而不是运行中翻转。
    """
    try:
        from django.conf import settings

        if settings.configured and hasattr(settings, "STRATEGY_ENGINE_ALLOW_SHORT"):
            return bool(settings.STRATEGY_ENGINE_ALLOW_SHORT)
    except Exception:  # noqa: BLE001 - 无 Django 上下文（单测/脚本）时走环境变量
        pass
    return os.environ.get("STRATEGY_ENGINE_ALLOW_SHORT", "").lower() in (
        "1",
        "true",
        "yes",
    )


class StrategyRegistry:
    """策略注册中心，懒加载单例模式"""

    _instance: StrategyRegistry | None = None
    _strategies: dict[str, type[BaseStrategy]] = {}
    _strategy_path: str = ""

    def __init__(self):
        pass

    @classmethod
    def get_instance(cls) -> "StrategyRegistry":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def validate_description(cls, strategy_cls: type["BaseStrategy"]) -> None:
        """校验策略描述是否符合 4 字段模板，失败抛异常。"""
        desc = getattr(strategy_cls, "description", "")
        if not desc or not desc.strip():
            name = getattr(strategy_cls, "name", "unnamed")
            raise ValueError(f"Strategy {name!r} missing description")

        required_fields = ["策略类型：", "核心指标：", "适用场景：", "入场逻辑："]
        missing = [f for f in required_fields if f not in desc]
        if missing:
            name = getattr(strategy_cls, "name", "unnamed")
            raise ValueError(f"Strategy {name!r} description missing fields: {missing}")

    @classmethod
    def register(
        cls, strategy_cls: type["BaseStrategy"], *, name: str | None = None
    ) -> type["BaseStrategy"]:
        """装饰器：注册策略类

        **双向持仓准入闸门**（2026-10）：声明 ``supports_short = True`` 的策略
        会走一条与只做多策略完全不同的下单路径（负持仓、穿越零点、按量平仓）。
        该路径尚未在真实资金链路上验证过，因此默认**拒绝注册**并告警，而不是
        静默放行——一个未经验证的做空策略上线跑真钱，是这个闸门存在的全部理由。

        刻意**不 raise**：策略引擎起不来会连带停掉所有只做多策略的决策，制造
        比「少注册一个策略」大得多的故障面。拒绝该策略 + 告警是正确量级。

        放行方式：显式设置 ``STRATEGY_ENGINE_ALLOW_SHORT = True``（环境变量或
        Django setting）。这是有意的摩擦——启用做空必须是一次显式决定。
        """
        reg_name = name or strategy_cls.name
        cls.validate_description(strategy_cls)

        if getattr(strategy_cls, "supports_short", False) and not _short_enabled():
            logger.error(
                "拒绝注册双向持仓策略 %s：supports_short=True 的下单路径尚未在"
                "真实资金链路验证。确认已在测试网跑通「开空 → 部分平 → 回补 → "
                "反手」后，设置 STRATEGY_ENGINE_ALLOW_SHORT=True 再启用。",
                reg_name,
            )
            return strategy_cls

        cls._strategies[reg_name] = strategy_cls
        logger.debug("Strategy registered: %s", reg_name)
        return strategy_cls

    @classmethod
    def get(cls, name: str) -> type["BaseStrategy"]:
        """获取策略类（精确匹配）"""
        if name not in cls._strategies:
            raise ValueError(f"Strategy not found: {name!r}")
        return cls._strategies[name]

    @classmethod
    def get_class(cls, name: str) -> type["BaseStrategy"]:
        """别名：获取策略类（兼容旧调用）"""
        return cls.get(name)

    @classmethod
    def list_registered(cls) -> list[str]:
        return list(cls._strategies.keys())

    @classmethod
    def list_registered_with_descriptions(cls) -> list[dict[str, str]]:
        """汇总所有已注册策略的名称和描述，供技能层 LLM 匹配使用。"""
        return [
            {
                "name": name,
                "description": strategy_cls.description,
            }
            for name, strategy_cls in cls._strategies.items()
        ]

    @classmethod
    def set_strategy_path(cls, path: str) -> None:
        """设置策略文件目录"""
        cls._strategy_path = path

    @classmethod
    def discover(cls) -> list[str]:
        """自动发现并注册策略模块"""
        import os
        import sys
        import importlib
        from .base import BaseStrategy

        path = cls._strategy_path
        if not path or not os.path.isdir(path):
            return cls.list_registered()
        discovered = []
        for filename in os.listdir(path):
            if filename.endswith(".py") and not filename.startswith("_"):
                module_name = filename[:-3]
                try:
                    if path not in sys.path:
                        sys.path.insert(0, path)
                    if module_name in sys.modules:
                        del sys.modules[module_name]
                    mod = importlib.import_module(module_name)
                    for attr_name in dir(mod):
                        attr = getattr(mod, attr_name)
                        if (
                            isinstance(attr, type)
                            and hasattr(attr, "name")
                            and getattr(attr, "__module__", "") == module_name
                            and issubclass(attr, BaseStrategy)
                            and attr is not BaseStrategy
                        ):
                            cls.register(attr, name=module_name)
                            discovered.append(attr.name)
                except Exception as e:
                    logger.warning("Failed to discover strategy %s: %s", module_name, e)
        return discovered


def register_strategy(
    name: str | None = None,
) -> callable:
    """装饰器：注册策略类，可选覆盖策略类自身的 name 属性"""

    def decorator(target):
        StrategyRegistry.register(target, name=name)
        return target

    return decorator


def get_strategy(name: str):
    """便捷函数：获取策略类"""
    return StrategyRegistry.get(name)
