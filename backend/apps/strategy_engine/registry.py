"""
策略注册中心（MVP 精简版）

复用 AgentRegistry 的装饰器注册模式。
策略类使用 @register_strategy 装饰器自动注册。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .base import BaseStrategy

logger = logging.getLogger(__name__)


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
        """装饰器：注册策略类"""
        reg_name = name or strategy_cls.name
        cls.validate_description(strategy_cls)
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
