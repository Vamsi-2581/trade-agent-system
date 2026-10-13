# 2026-10-12：RiskEvent 自 apps.risk 迁入 riskguard（摘除半死 app 的「档案迁移」半步）。
#
# 状态迁移（SeparateDatabaseAndState）**不执行任何 DDL**：凡是已经跑过 apps.risk
# 0001/0002 的库，`risk_events` 表连同全部历史行都在原处，Django 只是把模型的
# 归属改记到 riskguard 名下——零拷贝、零锁表。真正没有这张表的库（全新部署、
# 以及只建过一半的测试库）由下面的 `ensure_table` 用 schema_editor 按模型补建，
# 不写裸 SQL 是为了 SQLite / PostgreSQL 两种方言都成立。
#
# 与 RiskEvent 同住 apps.risk 的 RiskConfig 随 app 摘除一并删除：它是 guard 从不
# 读取的死配置面（本候选的起因），下面的 RunSQL 只清 `risk_configs` 这张死表，
# 历史行不存在恢复诉求。

import uuid

from django.db import migrations, models


def ensure_table(apps, schema_editor):
    """没有 `risk_events` 表的库按模型建表；已有的库（apps.risk 时代建的）原样保留。"""
    RiskEvent = apps.get_model("riskguard", "RiskEvent")
    connection = schema_editor.connection
    tables = connection.introspection.table_names(connection.cursor())
    if RiskEvent._meta.db_table not in tables:
        schema_editor.create_model(RiskEvent)


def drop_table(apps, schema_editor):
    schema_editor.delete_model(apps.get_model("riskguard", "RiskEvent"))


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.CreateModel(
                    name="RiskEvent",
                    fields=[
                        (
                            "id",
                            models.UUIDField(
                                default=uuid.uuid4,
                                editable=False,
                                primary_key=True,
                                serialize=False,
                            ),
                        ),
                        (
                            "level",
                            models.CharField(
                                choices=[("P0", "P0"), ("P1", "P1"), ("P2", "P2")],
                                max_length=2,
                            ),
                        ),
                        (
                            "event_type",
                            models.CharField(
                                choices=[
                                    ("hard_limit", "Hard Limit"),
                                    ("circuit_breaker", "Circuit Breaker"),
                                    ("reconciliation", "Reconciliation"),
                                    ("heartbeat", "Heartbeat"),
                                    ("floating_loss", "Floating Loss"),
                                ],
                                max_length=32,
                            ),
                        ),
                        ("message", models.TextField()),
                        ("resolved", models.BooleanField(default=False)),
                        ("created_at", models.DateTimeField(auto_now_add=True)),
                        ("resolved_at", models.DateTimeField(blank=True, null=True)),
                    ],
                    options={
                        "db_table": "risk_events",
                        "indexes": [
                            models.Index(
                                fields=["-created_at"],
                                name="risk_events_created_766ed6_idx",
                            )
                        ],
                    },
                ),
            ],
        ),
        migrations.RunPython(ensure_table, drop_table),
        # 死配置表：guard 从不读取（2026-10-12 摘除），只存在于跑过旧 app 的库。
        migrations.RunSQL(
            sql="DROP TABLE IF EXISTS risk_configs;",
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
