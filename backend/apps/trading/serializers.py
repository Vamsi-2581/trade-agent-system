from rest_framework import serializers
from .models import Order, Strategy, LiveSession
from .session_stats import money


class OrderSerializer(serializers.ModelSerializer):
    live_session_id = serializers.UUIDField(
        source="live_session.id", read_only=True, allow_null=True
    )
    strategy_id = serializers.UUIDField(
        source="live_session.strategy.id", read_only=True, allow_null=True
    )
    # 触发策略：优先读订单上的持久化快照（triggered_strategy），
    # 兼容历史/未回填数据时回退到 live_session→strategy 联表。
    strategy_name = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = [
            "id",
            "request_id",
            "exchange_account",
            "symbol",
            "side",
            "order_type",
            "quantity",
            "price",
            "status",
            "exchange_order_id",
            "filled_quantity",
            "avg_fill_price",
            "realized_pnl",
            "error_message",
            "live_session_id",
            "strategy_id",
            "strategy_name",
            "triggered_strategy",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def get_strategy_name(self, obj) -> str | None:
        if getattr(obj, "triggered_strategy", ""):
            return obj.triggered_strategy
        ls = getattr(obj, "live_session", None)
        if ls is not None and getattr(ls, "strategy_id", None):
            return ls.strategy.name
        return None


class StrategySerializer(serializers.ModelSerializer):
    class Meta:
        model = Strategy
        fields = [
            "id",
            "name",
            "code_path",
            "git_commit_hash",
            "is_active",
            "created_at",
        ]
        read_only_fields = ["id", "created_at"]


class LiveSessionSerializer(serializers.ModelSerializer):
    strategy_name = serializers.CharField(source="strategy.name", read_only=True)
    backtest_result_id = serializers.UUIDField(
        source="backtest_result.id", read_only=True, allow_null=True
    )
    exchange_account_name = serializers.CharField(
        source="exchange_account.label", read_only=True, allow_null=True
    )
    # 会话归因已实现盈亏（词条见 CONTEXT.md）：列表接口靠 `live_session_list` 的 queryset
    # 注解带出（一次聚合查询算完 50 条），详情接口把同一个数取自 `session_stats_for`。
    # **两处不许各算一遍**：有一条测试钉着两个接口对同一条会话给出同一个值。
    realized_pnl = serializers.SerializerMethodField()

    class Meta:
        model = LiveSession
        fields = [
            "id",
            "user",
            "strategy",
            "strategy_name",
            "backtest_result",
            "backtest_result_id",
            "symbol",
            "mode",
            "status",
            "exchange_account",
            "exchange_account_name",
            "initial_capital",
            "current_equity",
            "realized_pnl",
            "config",
            "started_at",
            "stopped_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "created_at",
            "updated_at",
            "started_at",
            "stopped_at",
        ]

    def get_realized_pnl(self, obj) -> str | None:
        """未注解时返回 None（不是 0）——「没算」与「算出来是零」是两件事。"""
        return money(getattr(obj, "realized_pnl", None))
