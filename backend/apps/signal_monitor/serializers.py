"""信号监控序列化器"""

from rest_framework import serializers
from .models import SignalMonitor, SignalTriggerLog


class SignalMonitorCreateSerializer(serializers.Serializer):
    """创建信号监控的序列化器"""

    name = serializers.CharField(max_length=128)
    symbol = serializers.CharField(max_length=32)
    interval = serializers.CharField(max_length=8, default="1h")
    source = serializers.CharField(max_length=32, default="binance")
    indicator_type = serializers.CharField(max_length=32)
    indicator_params = serializers.JSONField(default=dict)
    condition = serializers.JSONField()
    trigger_type = serializers.ChoiceField(
        choices=["once", "continuous"], default="once"
    )
    # 只能创建 notify（2026-09-21 收窄，2026-10-12 死路径清扫删掉分支）：`trade` /
    # `notify_and_trade` 曾是一条**断路径**——只做一次 `Order.objects.create()`，
    # 而全仓没有任何消费者读这些行，订单永久停在 `status="pending"`、
    # `exchange_order_id=""`，没有卡单告警也没有清理任务；存量 trade 行已由
    # migration 0004 迁移为 notify。`validate_strategy` 由 live_mode 内部写入
    # （见 apps/strategy_engine/live_mode.py），同样不由用户创建。
    action_type = serializers.ChoiceField(choices=["notify"], default="notify")
    action_params = serializers.JSONField(default=dict)
    backtest_result_id = serializers.UUIDField(required=False, allow_null=True)
    expires_at = serializers.DateTimeField(required=False, allow_null=True)


class SignalMonitorUpdateSerializer(serializers.Serializer):
    """更新信号监控的序列化器"""

    name = serializers.CharField(max_length=128, required=False)
    condition = serializers.JSONField(required=False)
    trigger_type = serializers.ChoiceField(
        choices=["once", "continuous"], required=False
    )
    # 同创建序列化器：只能保持/设为 notify。不堵这里，`patch` 会把一条既有的
    # notify 型 monitor 改成 trade，等于从更新口重新开出一条断路径
    # （trade 分支本身已在 engine 中删除，此入口是纵深防御）。
    action_type = serializers.ChoiceField(choices=["notify"], required=False)
    action_params = serializers.JSONField(required=False)
    status = serializers.ChoiceField(
        choices=["active", "disabled"],
        required=False,
    )
    expires_at = serializers.DateTimeField(required=False, allow_null=True)


class SignalMonitorListSerializer(serializers.ModelSerializer):
    """信号监控列表序列化器"""

    class Meta:
        model = SignalMonitor
        fields = [
            "id",
            "name",
            "symbol",
            "interval",
            "source",
            "indicator_type",
            "indicator_params",
            "condition",
            "trigger_type",
            "action_type",
            "action_params",
            "status",
            "last_triggered_at",
            "trigger_count",
            "expires_at",
            "backtest_result",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "status",
            "last_triggered_at",
            "trigger_count",
            "created_at",
            "updated_at",
        ]


class SignalTriggerLogSerializer(serializers.ModelSerializer):
    """触发日志序列化器"""

    class Meta:
        model = SignalTriggerLog
        fields = ["id", "monitor", "trigger_value", "action_result", "executed_at"]
