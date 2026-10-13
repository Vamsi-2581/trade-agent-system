from __future__ import annotations

import uuid

from django.db import models


class RiskEvent(models.Model):
    """风控事件档案（2026-10-12 自 apps.risk 迁入）。

    原本 RiskEvent 与 RiskConfig 同住 apps/risk：RiskConfig 是一个 guard 永不读取
    的死配置面（deletion test 整 app 删掉、下单路径零损伤），RiskEvent 才是唯一有
    实际写入方与前端读者的表。本次摘除按「写而不读的删，读得着的迁」处理：
    RiskConfig 随 app 删除，RiskEvent 迁到这里，表名不变（``risk_events``），
    历史数据零迁移。

    event_type 枚举同时补上 ``floating_loss``：迁移前 guard 是唯一写入方，写的
    却是枚举外的值（ CharField choices 不约束 Level 之外的写入），迁移后写入值
    与枚举一致，混合来源的历史行也可按 choices 反解。
    """

    LEVEL_CHOICES = [("P0", "P0"), ("P1", "P1"), ("P2", "P2")]
    TYPE_CHOICES = [
        ("hard_limit", "Hard Limit"),
        ("circuit_breaker", "Circuit Breaker"),
        ("reconciliation", "Reconciliation"),
        ("heartbeat", "Heartbeat"),
        ("floating_loss", "Floating Loss"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    level = models.CharField(max_length=2, choices=LEVEL_CHOICES)
    event_type = models.CharField(max_length=32, choices=TYPE_CHOICES)
    message = models.TextField()
    resolved = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "risk_events"
        # 索引名沿用 apps.risk 时代 Django 生成的名字：新旧库的 schema 完全一致，
        # 旧库里的同定义索引不必重建，fresh 库建出来的名字也和旧库对得上。
        indexes = [
            models.Index(fields=["-created_at"], name="risk_events_created_766ed6_idx")
        ]
