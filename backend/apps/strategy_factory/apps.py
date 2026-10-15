"""Strategy Factory Application Configuration."""

from django.apps import AppConfig


class StrategyFactoryConfig(AppConfig):
    """候选策略流水线（策略工厂）。

    与 `apps/regime` 同一纪律：不在 `ready()` 里注册任何东西、不起后台线程。
    流水线的每一步都由显式任务或人工动作驱动，import 期零副作用。
    """

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.strategy_factory"
    verbose_name = "候选策略流水线"
