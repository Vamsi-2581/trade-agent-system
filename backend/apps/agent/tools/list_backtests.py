"""查询用户的回测记录工具。

Agent 可按回测ID前缀、策略名称、交易对、审核状态过滤。
返回回测的基本指标（收益率、夏普比率、最大回撤、胜率等）。
"""

from __future__ import annotations

import logging
import uuid

from asgiref.sync import sync_to_async
from django.db import close_old_connections

from .base import BaseTool, ToolResult

logger = logging.getLogger(__name__)


@sync_to_async
def _query_backtests(user_id: str, filters: dict) -> tuple[list[dict], int]:
    """在 sync 线程中安全查询回测记录。

    Args:
        user_id: 用户 ID（UUID 或 username）
        filters: 过滤参数（backtest_id, symbol, strategy_name, review_status, limit）

    Returns:
        (results, total_count) - 结果列表和符合条件的总记录数
    """
    close_old_connections()

    from apps.backtest.models import BacktestResult

    # 尝试转换为 UUID
    try:
        user_uuid = uuid.UUID(str(user_id))
        queryset = BacktestResult.objects.filter(user_id=user_uuid)
    except (ValueError, TypeError):
        # 如果不是 UUID，尝试按 username 查找
        from django.contrib.auth import get_user_model

        User = get_user_model()
        user_obj = User.objects.filter(username=user_id).first()
        if user_obj:
            queryset = BacktestResult.objects.filter(user=user_obj)
        else:
            return [], 0

    # 应用过滤条件
    if filters.get("backtest_id"):
        queryset = queryset.filter(id__startswith=filters["backtest_id"])

    if filters.get("symbol"):
        queryset = queryset.filter(symbol=filters["symbol"])

    if filters.get("strategy_name"):
        queryset = queryset.filter(strategy__name__icontains=filters["strategy_name"])

    if filters.get("review_status"):
        queryset = queryset.filter(review_status=filters["review_status"])

    # 排序：按创建时间倒序
    queryset = queryset.select_related("strategy").order_by("-created_at")

    # 计算总数
    total = queryset.count()

    # 应用 limit（降级 DSML 路径可能把 limit 传成字符串，转 int 再取上界，
    # 与 box_range 对数字参数的处理一致）
    limit = min(int(filters.get("limit", 20)), 100)
    queryset = queryset[:limit]

    # 转换为字典列表（字段转换）
    results = []
    for obj in queryset:
        results.append(
            {
                "id": str(obj.id),  # UUID → 字符串（完整）
                "strategy_name": obj.strategy.name
                if obj.strategy
                else "",  # 外键 → 名称
                "symbol": obj.symbol,
                "timeframe": obj.timeframe,
                "start_date": str(obj.start_date),  # Date → 字符串
                "end_date": str(obj.end_date),
                "initial_capital": float(obj.initial_capital),  # Decimal → float
                "final_capital": float(obj.final_capital),
                "total_return_pct": obj.total_return_pct,  # float 保持
                "sharpe_ratio": obj.sharpe_ratio if obj.sharpe_ratio else None,
                "max_drawdown_pct": obj.max_drawdown_pct
                if obj.max_drawdown_pct
                else None,
                "win_rate": obj.win_rate if obj.win_rate else None,
                "total_trades": obj.total_trades,
                "review_status": obj.review_status,
                "grid_search_id": str(obj.grid_search_id)
                if obj.grid_search_id
                else None,
                "created_at": obj.created_at.isoformat(),  # datetime → ISO 8601
            }
        )

    return results, total


class ListBacktestsTool(BaseTool):
    """
    查询用户的回测记录。

    可按回测ID前缀、策略名称、交易对、审核状态过滤。
    返回回测的基本指标（收益率、夏普比率、最大回撤、胜率等）。
    当用户询问历史回测、回测表现、或需要查找特定回测时使用。
    结果按创建时间倒序排列，最多返回 limit 条记录。
    """

    name = "list_backtests"
    description = (
        "查询用户的回测记录。可按回测ID前缀、策略名称、交易对、审核状态过滤。"
        "返回回测的基本指标（收益率、夏普比率、最大回撤、胜率等）。"
        "当用户询问历史回测、回测表现、或需要查找特定回测时使用。"
        "结果按创建时间倒序排列，最多返回 limit 条记录。"
    )

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "backtest_id": {
                    "type": "string",
                    "description": "回测ID的前缀（模糊匹配开头），例如 'a1b2'",
                },
                "symbol": {
                    "type": "string",
                    "description": "交易对，例如 'BTCUSDT'",
                },
                "strategy_name": {
                    "type": "string",
                    "description": "策略名称（模糊匹配），例如 'MyStrategy'",
                },
                "review_status": {
                    "type": "string",
                    "enum": ["pending", "approved", "rejected"],
                    "description": "审核状态",
                },
                "limit": {
                    "type": "integer",
                    "description": "返回记录数量上限，默认 20，最大 100",
                    "default": 20,
                    "minimum": 1,
                    "maximum": 100,
                },
            },
            "required": [],
        }

    async def execute(
        self,
        backtest_id: str = "",
        symbol: str = "",
        strategy_name: str = "",
        review_status: str = "",
        limit: int = 20,
        user_id: str = "",  # 由 base.py 自动注入
        **kwargs,
    ) -> ToolResult:
        """执行查询并返回结果。"""
        if not user_id:
            return ToolResult(success=False, error="无法确定用户身份")

        # 构建过滤参数
        filters = {}
        if backtest_id:
            filters["backtest_id"] = backtest_id
        if symbol:
            filters["symbol"] = symbol
        if strategy_name:
            filters["strategy_name"] = strategy_name
        if review_status:
            filters["review_status"] = review_status
        if limit:
            filters["limit"] = limit

        try:
            results, total = await _query_backtests(user_id, filters)
            returned = len(results)
            has_more = total > returned

            logger.info(
                "[ListBacktestsTool] user=%s total=%d returned=%d has_more=%s",
                user_id,
                total,
                returned,
                has_more,
            )

            return ToolResult(
                success=True,
                data={
                    "total": total,
                    "returned": returned,
                    "has_more": has_more,
                    "results": results,
                },
            )
        except Exception as e:
            logger.error("[ListBacktestsTool] query failed: %s", e, exc_info=True)
            return ToolResult(success=False, error=f"查询失败: {e}")
