from rest_framework import serializers
from .models import Order, Strategy, LiveSession
from .session_stats import money, row_efficiency_keys


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

    # --- 资金效率段（spec #30 D6/D9，子票 #38，spec 用户故事 18） ------------------ #
    #
    # 列表与详情**给同一套键**。六个契约键由 `capital_efficiency_keys()`（#33 的纯模块）
    # 钉死，这里把它们摊到响应顶层：逐字同名，所以前端只有一套读法。
    #
    # **摊平不等于可以直接当回测侧那个数读。** 会话侧只有点观测（`Order` 没有成对的
    # 开平仓时刻），所以 `capital_utilization` 与 `time_in_market_pct` 会是 `0`、
    # `return_per_capital_used_pct` 会是 `None`——**那是「这一侧量不了持仓时长」，不是
    # 「这条会话没占用资金」**。`capital_efficiency_metrics_available` 与
    # `capital_efficiency_notes` 必须与数字一起读（子载荷里还有 `reason` 与
    # `occupied_seconds` / `window_seconds` 两个中间量）。少读一个，就会把「量不了」
    # 读成「几乎没用钱」——恰好把重仓读成高效率，方向正是最危险的那一侧。
    #
    # `available` 为假时六个键一律 `None`（**不是 0**）：0 会被读成「完全没用钱」。
    capital_efficiency = serializers.SerializerMethodField()
    capital_efficiency_metrics_available = serializers.SerializerMethodField()
    capital_efficiency_notes = serializers.SerializerMethodField()
    avg_capital_used = serializers.SerializerMethodField()
    capital_utilization = serializers.SerializerMethodField()
    idle_pct = serializers.SerializerMethodField()
    time_in_market_pct = serializers.SerializerMethodField()
    return_per_capital_used_pct = serializers.SerializerMethodField()
    sample_sufficient_orders = serializers.SerializerMethodField()

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
            "capital_efficiency",
            "capital_efficiency_metrics_available",
            "capital_efficiency_notes",
            "avg_capital_used",
            "capital_utilization",
            "idle_pct",
            "time_in_market_pct",
            "return_per_capital_used_pct",
            "sample_sufficient_orders",
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

    def _efficiency(self, obj) -> dict:
        """这一行的效率段。**每次序列化只算一次**（`_EFFICIENCY_CACHE` 挂在实例上）。

        九个 MethodField 都读它，而它自己要跑一遍纯模块。DRF 会对每个 MethodField 各调
        一次方法，没有这层缓存就是九个字段各算一遍——同一个数在九处各算一次，正是本模块
        开头那句要防的事。
        """
        cache = self.__dict__.setdefault("_EFFICIENCY_CACHE", {})
        key = id(obj)
        if key not in cache:
            cache[key] = row_efficiency_keys(obj)
        return cache[key]

    def _efficiency_field(self, obj, name):
        return self._efficiency(obj)[name]

    def get_capital_efficiency(self, obj):
        return self._efficiency_field(obj, "capital_efficiency")

    def get_capital_efficiency_metrics_available(self, obj):
        return self._efficiency_field(obj, "capital_efficiency_metrics_available")

    def get_capital_efficiency_notes(self, obj):
        return self._efficiency_field(obj, "capital_efficiency_notes")

    def get_avg_capital_used(self, obj):
        return self._efficiency_field(obj, "avg_capital_used")

    def get_capital_utilization(self, obj):
        return self._efficiency_field(obj, "capital_utilization")

    def get_idle_pct(self, obj):
        return self._efficiency_field(obj, "idle_pct")

    def get_time_in_market_pct(self, obj):
        return self._efficiency_field(obj, "time_in_market_pct")

    def get_return_per_capital_used_pct(self, obj):
        return self._efficiency_field(obj, "return_per_capital_used_pct")

    def get_sample_sufficient_orders(self, obj):
        """`sample_sufficient` 的**不同名**出口。

        `compute_session_stats` 的 `sample_sufficient` 数的是**订单笔数**（会话侧一笔 =
        一张带 `realized_pnl` 的订单），而资金效率子载荷里同名的那个是**区间笔数**——点
        观测下两者相等，但它们不是同一个概念，将来任何一侧改了取数都会分叉。所以列表
        响应把订单口径的那个改名带出，不让两个不同的东西共用一个键名。
        """
        return self._efficiency(obj)["capital_efficiency"].get("sample_sufficient")
