# 死路径残留清扫（架构评审 修改6，2026-10-12）：
# `trade` / `notify_and_trade` 是一条断路径 —— `_execute_trade` 只做一次
# `Order.objects.create()`，全仓没有任何消费者读这些行，订单永久停在
# `status="pending"`、`exchange_order_id=""`，没有卡单告警也没有清理任务。
# engine 的 trade 分支已删除；本迁移先把存量行迁为 notify（走通知路径），
# 再收窄字段 choices，保证库内不再存在不可触达的动作类型。
# deletion test：存量 trade 行的终态不变 —— 触发后只发通知，不再产生幽灵 pending 单。

from django.db import migrations, models

LEGACY_TRADE_ACTIONS = ("trade", "notify_and_trade")


def migrate_trade_rows_to_notify(apps, schema_editor):
    SignalMonitor = apps.get_model("signal_monitor", "SignalMonitor")
    SignalMonitor.objects.filter(action_type__in=LEGACY_TRADE_ACTIONS).update(
        action_type="notify"
    )


def restore_trade_rows(apps, schema_editor):
    """反向不可精确恢复（notify 行无法区分曾经的 trade），有意保持单向。"""


class Migration(migrations.Migration):
    dependencies = [
        ("signal_monitor", "0003_remove_signalmonitor_signal_moni_strateg_c4e166_idx"),
    ]

    operations = [
        migrations.RunPython(migrate_trade_rows_to_notify, restore_trade_rows),
        migrations.AlterField(
            model_name="signalmonitor",
            name="action_type",
            field=models.CharField(
                choices=[
                    ("notify", "发送通知"),
                    ("validate_strategy", "策略验证"),
                ],
                default="notify",
                max_length=20,
                verbose_name="动作类型",
            ),
        ),
    ]
