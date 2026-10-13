"""
信号监控模型

支持两种使用场景：
1. 用户直接添加技术信号到监控列表
2. 回测完成后，将回测使用的技术信号导入监控列表

信号触发类型：
- once: 单次触发，触发后自动禁用
- continuous: 持续触发，每次满足条件都会执行操作
"""

import uuid
from django.conf import settings
from django.db import models
from django.utils import timezone


class SignalMonitor(models.Model):
    """技术信号监控"""

    TRIGGER_TYPE_CHOICES = [
        ("once", "单次触发"),
        ("continuous", "持续触发"),
    ]

    STATUS_CHOICES = [
        ("active", "活跃"),
        ("triggered", "已触发"),
        ("disabled", "已禁用"),
        ("expired", "已过期"),
    ]

    # 收窄到 notify + validate_strategy（2026-10-12 死路径清扫）：`trade` /
    # `notify_and_trade` 分支已删除，存量行由 migration 0004 迁移为 notify。
    # `validate_strategy` 仅由 strategy_engine live_mode 内部写入，不走用户入口。
    ACTION_TYPE_CHOICES = [
        ("notify", "发送通知"),
        ("validate_strategy", "策略验证"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="signal_monitors",
    )

    # 信号标识
    name = models.CharField(max_length=128, verbose_name="信号名称")
    symbol = models.CharField(max_length=32, verbose_name="交易对")
    interval = models.CharField(max_length=8, default="1h", verbose_name="K��周期")
    source = models.CharField(max_length=32, default="binance", verbose_name="数据源")

    # 技术指标参数
    indicator_type = models.CharField(max_length=32, verbose_name="指标类型")
    indicator_params = models.JSONField(default=dict, verbose_name="指标参数")
    condition = models.JSONField(verbose_name="触发条件")

    # 触发配置
    trigger_type = models.CharField(
        max_length=16,
        choices=TRIGGER_TYPE_CHOICES,
        default="once",
        verbose_name="触发类型",
    )

    # 执行动作
    action_type = models.CharField(
        max_length=20,
        choices=ACTION_TYPE_CHOICES,
        default="notify",
        verbose_name="动作类型",
    )
    action_params = models.JSONField(default=dict, verbose_name="动作参数")

    # 状态
    status = models.CharField(
        max_length=16,
        choices=STATUS_CHOICES,
        default="active",
        verbose_name="状态",
    )

    # 来源
    backtest_result = models.ForeignKey(
        "backtest.BacktestResult",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="signal_monitors",
        verbose_name="关联回测",
    )

    # 策略关联（当 action_type 为 validate_strategy 时使用）
    strategy_name = models.CharField(
        max_length=128,
        blank=True,
        default="",
        verbose_name="策略名称",
    )
    live_session_id = models.CharField(
        max_length=64,
        blank=True,
        default="",
        verbose_name="实盘会话 ID",
    )

    # 时间
    last_triggered_at = models.DateTimeField(
        null=True, blank=True, verbose_name="最后触发时间"
    )
    trigger_count = models.IntegerField(default=0, verbose_name="触发次数")
    expires_at = models.DateTimeField(null=True, blank=True, verbose_name="过期时间")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "signal_monitors"
        indexes = [
            models.Index(fields=["user", "status"]),
            models.Index(fields=["symbol", "interval", "status"]),
            models.Index(fields=["-created_at"]),
        ]
        verbose_name = "信号监控"
        verbose_name_plural = "信号监控"

    def __str__(self):
        return f"{self.name} ({self.symbol} {self.indicator_type})"

    @property
    def is_active(self):
        if self.status != "active":
            return False
        if self.expires_at and self.expires_at < timezone.now():
            return False
        return True


class SignalTriggerLog(models.Model):
    """信号触发日志"""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    monitor = models.ForeignKey(
        SignalMonitor,
        on_delete=models.CASCADE,
        related_name="trigger_logs",
    )
    trigger_value = models.JSONField(verbose_name="触发时的指标值")
    action_result = models.JSONField(default=dict, verbose_name="执行结果")
    executed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "signal_trigger_logs"
        indexes = [
            models.Index(fields=["monitor", "-executed_at"]),
        ]
        verbose_name = "信号触发日志"
        verbose_name_plural = "信号触发日志"
