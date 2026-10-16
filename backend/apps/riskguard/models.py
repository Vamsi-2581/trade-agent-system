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

    ``live_session``（spec #19 D7，子票 #24）：**命中是会话的属性**，候选只是间接
    引用，所以这一跳钉在事件上、候选侧靠 ``StrategyCandidate.paper_session`` 反查。
    可空是**刻意的**，有两个独立的理由：

    1. **写而不读的那一侧还不全**：只有额度类拒单（``RiskGuard._reject``）手上真有
       会话（``OrderRequest.live_session_id``）。浮亏预警走的是
       ``RiskGuard._check_floating_pnl``，那是一个**账户级**循环，手里只有
       ``target``（交易所#账户）与 ``pos``，够不着任何会话——它的行永远是 NULL。
    2. **既有行与既有写入路径零回归**：加列不许把历史行或任何一条不传会话的写入路径
       变成失败。
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
    #: 这一条命中出自哪条实盘会话；``NULL`` = 归集不到会话（浮亏预警、历史行）。
    #: ``SET_NULL`` 而不是 ``CASCADE``：**档案不该因为会话被删而消失**——「这条策略
    #: 在 paper 期被风控拦过几次」是要事后答得出来的问题，删会话把事件一起抹掉，
    #: 恰恰抹掉了要回答它的那份证据。
    live_session = models.ForeignKey(
        "trading.LiveSession",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="risk_events",
        help_text="命中所属的实盘/模拟会话；为空表示归集不到具体会话",
    )
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
