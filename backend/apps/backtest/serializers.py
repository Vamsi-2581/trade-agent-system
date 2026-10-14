from rest_framework import serializers
from .models import BacktestResult, BacktestTrade, GridSearchJob


class BacktestResultSerializer(serializers.ModelSerializer):
    strategy_name = serializers.CharField(source="strategy.name", read_only=True)

    class Meta:
        model = BacktestResult
        fields = [
            "id",
            "strategy",
            "strategy_name",
            "symbol",
            "timeframe",
            "start_date",
            "end_date",
            "initial_capital",
            "final_capital",
            "total_return_pct",
            "sharpe_ratio",
            "max_drawdown_pct",
            "win_rate",
            "total_trades",
            "git_commit_hash",
            "parameters",
            "data_source",
            "metrics",
            "review_status",
            "review_notes",
            "reviewed_at",
            "is_grid_search",
            "grid_search_id",
            "created_at",
        ]
        read_only_fields = [
            "id",
            "created_at",
            "review_status",
            "review_notes",
            "reviewed_at",
        ]


class BacktestTradeSerializer(serializers.ModelSerializer):
    class Meta:
        model = BacktestTrade
        fields = [
            "id",
            "backtest_id",
            "entry_time",
            "exit_time",
            "side",
            "entry_price",
            "exit_price",
            "quantity",
            "pnl",
            "pnl_pct",
            "commission",
            "signal",
            "exit_reason",
            "trade_type",
        ]
        read_only_fields = ["id"]


class BacktestDetailSerializer(serializers.ModelSerializer):
    """Full detail serializer: summary + equity_curve + drawdown_curve + ohlcv + indicators."""

    equity_curve = serializers.JSONField(read_only=True)
    drawdown_curve = serializers.JSONField(read_only=True)
    ohlcv_data = serializers.JSONField(read_only=True)
    indicator_data = serializers.JSONField(read_only=True)
    strategy_name = serializers.CharField(source="strategy.name", read_only=True)

    class Meta:
        model = BacktestResult
        fields = [
            "id",
            "strategy",
            "strategy_name",
            "symbol",
            "timeframe",
            "start_date",
            "end_date",
            "initial_capital",
            "final_capital",
            "total_return_pct",
            "sharpe_ratio",
            "max_drawdown_pct",
            "win_rate",
            "total_trades",
            "git_commit_hash",
            "parameters",
            "data_source",
            "metrics",
            "equity_curve",
            "drawdown_curve",
            "ohlcv_data",
            "indicator_data",
            "review_status",
            "review_notes",
            "reviewed_at",
            "created_at",
        ]
        read_only_fields = ["id", "created_at"]


class GridSearchJobSerializer(serializers.ModelSerializer):
    strategy_name = serializers.CharField(source="strategy.name", read_only=True)

    class Meta:
        model = GridSearchJob
        fields = [
            "id",
            "strategy_name",
            "symbol",
            "timeframe",
            "start_date",
            "end_date",
            "initial_capital",
            "status",
            "source",
            "total_combinations",
            "completed_combinations",
            "best_result",
            "sort_by",
            "error_log",
            "celery_task_id",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class GridSearchGroupSerializer(serializers.Serializer):
    """序列化一个分组（网格搜索任务）"""

    type = serializers.CharField(default="grid_search")
    job_id = serializers.UUIDField()
    job_name = serializers.CharField()
    symbol = serializers.CharField()
    timeframe = serializers.CharField()
    status = serializers.CharField()
    total_combinations = serializers.IntegerField()
    completed = serializers.IntegerField()
    best_return_pct = serializers.FloatField(allow_null=True)
    best_sharpe = serializers.FloatField(allow_null=True)
    created_at = serializers.DateTimeField()
    results = serializers.ListField(child=serializers.DictField())


class SingleGroupSerializer(serializers.Serializer):
    """序列化一个单次回测分组"""

    type = serializers.CharField(default="single")
    result = serializers.DictField()
