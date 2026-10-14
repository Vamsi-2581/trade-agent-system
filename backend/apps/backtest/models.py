import uuid
from django.conf import settings
from django.db import models


class BacktestResult(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    strategy = models.ForeignKey("trading.Strategy", on_delete=models.CASCADE)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="backtest_results",
        help_text="发起回测的用户",
    )
    symbol = models.CharField(max_length=32)
    timeframe = models.CharField(max_length=8)
    start_date = models.DateField()
    end_date = models.DateField()
    initial_capital = models.DecimalField(max_digits=20, decimal_places=2)
    final_capital = models.DecimalField(max_digits=20, decimal_places=2)
    total_return_pct = models.FloatField()
    sharpe_ratio = models.FloatField(null=True)
    max_drawdown_pct = models.FloatField(null=True)
    win_rate = models.FloatField(null=True)
    total_trades = models.IntegerField(default=0)
    git_commit_hash = models.CharField(max_length=40, blank=True)
    parameters = models.JSONField(default=dict)
    cost_detail = models.JSONField(
        default=dict,
        blank=True,
        help_text='回测成本明细 {"commission": x, "slippage": y, "funding": z}',
    )
    data_source = models.JSONField(
        default=dict,
        blank=True,
        help_text='K线数据来源统计 {"from_db": n, "from_api": m, "gaps": [...]}',
    )

    # 审核字段
    REVIEW_STATUS_CHOICES = [
        ("pending", "待审核"),
        ("approved", "审核通过"),
        ("rejected", "审核不通过"),
    ]
    review_status = models.CharField(
        max_length=12,
        choices=REVIEW_STATUS_CHOICES,
        default="pending",
        help_text="回测审核状态",
    )
    review_notes = models.TextField(
        blank=True,
        help_text="审核备注",
    )
    reviewed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="审核时间",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    # Time series data (downsampled to ≤2000 points at write time)
    equity_curve = models.JSONField(
        default=list,
        blank=True,
        help_text='[{"timestamp": "...", "equity": 10000.0, "drawdown": 0.0}, ...]',
    )
    drawdown_curve = models.JSONField(
        default=list,
        blank=True,
        help_text='[{"timestamp": "...", "drawdown": -0.012}, ...]',
    )

    # OHLCV market data used for backtest
    ohlcv_data = models.JSONField(
        default=list,
        blank=True,
        help_text='[{"timestamp": "...", "open": x, "high": x, "low": x, "close": x, "volume": x}, ...]',
    )

    # Computed technical indicators
    indicator_data = models.JSONField(
        default=dict,
        blank=True,
        help_text='{"ma7": [...], "ma25": [...], "macd": {"dif": [...], "dea": [...], "hist": [...]}, "rsi": [...]}',
    )

    # Advanced performance metrics (computed from equity curve and trades)
    metrics = models.JSONField(
        default=dict,
        blank=True,
        help_text="Advanced stats: sortino/calmar/annualized/profit_factor/etc.",
    )

    # Grid search fields
    grid_search_id = models.UUIDField(
        null=True,
        blank=True,
        db_index=True,
        help_text="所属网格搜索任务 ID",
    )
    is_grid_search = models.BooleanField(
        default=False,
        help_text="是否为网格搜索的子回测",
    )
    celery_task_id = models.CharField(
        max_length=128,
        blank=True,
        default="",
        db_index=True,
        help_text="Celery 任务 ID，用于 get_task_result 反查回测结果",
    )

    class Meta:
        db_table = "backtest_results"


class BacktestTrade(models.Model):
    """Individual trade record within a backtest."""

    backtest = models.ForeignKey(
        BacktestResult, on_delete=models.CASCADE, related_name="trades"
    )
    entry_time = models.DateTimeField()
    exit_time = models.DateTimeField(null=True, blank=True)
    side = models.CharField(
        max_length=5,
        choices=[("long", "Long"), ("short", "Short")],
    )
    entry_price = models.DecimalField(max_digits=20, decimal_places=8)
    exit_price = models.DecimalField(
        max_digits=20, decimal_places=8, null=True, blank=True
    )
    quantity = models.DecimalField(max_digits=20, decimal_places=8)
    pnl = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    pnl_pct = models.FloatField(null=True, blank=True)
    commission = models.DecimalField(
        max_digits=20, decimal_places=8, default=0, blank=True
    )
    slippage = models.DecimalField(
        max_digits=20, decimal_places=8, default=0, blank=True
    )
    signal = models.CharField(
        max_length=64,
        blank=True,
        help_text="What signal triggered this trade",
    )
    exit_reason = models.CharField(
        max_length=64,
        blank=True,
        help_text="Why this trade was closed (stop_loss, take_profit, signal, etc.)",
    )
    trade_type = models.CharField(
        max_length=8,
        choices=[("open", "Open"), ("add", "Add"), ("close", "Close")],
        default="open",
        help_text="Type of trade: open (initial position), add (add to position), close (exit position)",
    )

    class Meta:
        db_table = "backtest_trades"
        ordering = ["entry_time"]


class FundingRateRecord(models.Model):
    """永续合约资金费率历史（每 8h 一条，Binance fetchFundingRateHistory）。

    用于回测中的资金费率成本结算。增量同步以 (symbol, funding_time) 幂等。
    """

    symbol = models.CharField(max_length=32, db_index=True)
    funding_time = models.DateTimeField(db_index=True)
    rate = models.FloatField(help_text="资金费率，如 0.0001 = 0.01%/8h")

    class Meta:
        db_table = "backtest_funding_rates"
        unique_together = [("symbol", "funding_time")]
        ordering = ["funding_time"]

    def __str__(self) -> str:
        return f"{self.symbol}@{self.funding_time.isoformat()} rate={self.rate}"


class GridSearchJob(models.Model):
    """网格搜索任务，聚合多个 BacktestResult"""

    STATUS_CHOICES = [
        ("pending", "待执行"),
        ("running", "运行中"),
        ("completed", "已完成"),
        ("failed", "失败"),
        ("cancelled", "已取消"),
    ]
    SOURCE_CHOICES = [
        ("agent", "Agent 对话"),
        ("cron", "定时任务"),
        ("api", "REST API"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="grid_search_jobs",
    )
    strategy = models.ForeignKey(
        "trading.Strategy",
        on_delete=models.CASCADE,
        related_name="strategy_grid_jobs",
    )
    symbol = models.CharField(max_length=32)
    timeframe = models.CharField(max_length=8)
    start_date = models.DateField()
    end_date = models.DateField()
    initial_capital = models.DecimalField(max_digits=20, decimal_places=2)
    commission_rate = models.DecimalField(max_digits=6, decimal_places=4, default=0.001)
    search_config = models.JSONField(default=dict)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default="pending")
    source = models.CharField(max_length=8, choices=SOURCE_CHOICES, default="api")
    total_combinations = models.IntegerField(default=0)
    completed_combinations = models.IntegerField(default=0)
    best_result = models.ForeignKey(
        "backtest.BacktestResult",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="best_grid_result",
    )
    sort_by = models.CharField(max_length=32, default="sharpe_ratio")
    error_log = models.JSONField(default=list, blank=True)
    celery_task_id = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "grid_search_jobs"
        ordering = ["-created_at"]

    def __str__(self):
        return f"GridSearchJob({self.symbol}/{self.timeframe} status={self.status})"
