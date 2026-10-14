"""查询用户的订单记录工具。

Agent 可按订单ID前缀、交易对、方向、状态过滤。
返回订单的核心信息（成交价格、数量、盈亏等）。
"""

from __future__ import annotations

import logging
import uuid

from asgiref.sync import sync_to_async
from django.db import close_old_connections

from .base import BaseTool, ToolResult

logger = logging.getLogger(__name__)


@sync_to_async
def _query_orders(user_id: str, filters: dict) -> tuple[list[dict], int]:
    """在 sync 线程中安全查询订单记录。

    Args:
        user_id: 用户 ID（UUID 或 username）
        filters: 过滤参数（order_id, symbol, side, status, backtest_id, limit）

    Returns:
        (results, total_count) - 结果列表和符合条件的总记录数
    """
    close_old_connections()

    from apps.trading.models import Order

    # 尝试转换为 UUID
    try:
        user_uuid = uuid.UUID(str(user_id))
        queryset = Order.objects.filter(user_id=user_uuid)
    except (ValueError, TypeError):
        # 如果不是 UUID，尝试按 username 查找
        from django.contrib.auth import get_user_model

        User = get_user_model()
        user_obj = User.objects.filter(username=user_id).first()
        if user_obj:
            queryset = Order.objects.filter(user=user_obj)
        else:
            return [], 0

    # 应用过滤条件
    if filters.get("order_id"):
        queryset = queryset.filter(id__startswith=filters["order_id"])

    if filters.get("symbol"):
        queryset = queryset.filter(symbol=filters["symbol"])

    if filters.get("side"):
        queryset = queryset.filter(side=filters["side"])

    if filters.get("status"):
        queryset = queryset.filter(status=filters["status"])

    if filters.get("backtest_id"):
        # Order → LiveSession → BacktestResult 链路过滤
        queryset = queryset.filter(
            live_session__backtest_result_id=filters["backtest_id"]
        )

    # 排序：按创建时间倒序
    queryset = queryset.select_related("exchange_account", "live_session").order_by(
        "-created_at"
    )

    # 计算总数
    total = queryset.count()

    # 应用 limit（降级 DSML 路径可能把 limit 传成字符串，转 int 再取上界，
    # 与 box_range 对数字参数的处理一致）
    limit = min(int(filters.get("limit", 50)), 200)
    queryset = queryset[:limit]

    # 转换为字典列表（字段转换）
    results = []
    for obj in queryset:
        results.append(
            {
                "id": str(obj.id),  # UUID → 字符串（完整）
                "symbol": obj.symbol,
                "side": obj.side,
                "order_type": obj.order_type,
                "quantity": float(obj.quantity),  # Decimal → float
                "price": float(obj.price) if obj.price else None,
                "status": obj.status,
                "filled_quantity": float(obj.filled_quantity),
                "avg_fill_price": float(obj.avg_fill_price)
                if obj.avg_fill_price
                else None,
                "realized_pnl": float(obj.realized_pnl) if obj.realized_pnl else None,
                "exchange_order_id": obj.exchange_order_id,
                "exchange_label": obj.exchange_account.label
                if obj.exchange_account
                else "",
                "live_session_id": str(obj.live_session.id)
                if obj.live_session
                else None,
                "created_at": obj.created_at.isoformat(),  # datetime → ISO 8601
            }
        )

    return results, total


class ListOrdersTool(BaseTool):
    """
    查询用户的订单记录。

    可按订单ID前缀、交易对、方向、状态过滤。
    返回订单的核心信息（成交价格、数量、盈亏等）。
    当用户询问交易历史、订单状态、或需要查找特定订单时使用。
    结果按创建时间倒序排列，最多返回 limit 条记录。
    """

    name = "list_orders"
    description = (
        "查询用户的订单记录。可按订单ID前缀、交易对、方向、状态、回测ID过滤。"
        "返回订单的核心信息（成交价格、数量、盈亏等）。"
        "当用户询问交易历史、订单状态、或需要查找特定订单时使用。"
        "结果按创建时间倒序排列，最多返回 limit 条记录。"
    )

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "order_id": {
                    "type": "string",
                    "description": "订单ID的前缀（模糊匹配开头），例如 'a1b2'",
                },
                "symbol": {
                    "type": "string",
                    "description": "交易对，例如 'BTCUSDT'",
                },
                "side": {
                    "type": "string",
                    "enum": ["buy", "sell"],
                    "description": "订单方向",
                },
                "status": {
                    "type": "string",
                    # unknown = 下单结果未知（可能已在交易所活着）。不给它一个名字，
                    # Agent 就查不到这批最需要人看的单手。
                    "enum": [
                        "pending",
                        "submitted",
                        "partial",
                        "filled",
                        "cancelled",
                        "failed",
                        "unknown",
                    ],
                    "description": "订单状态",
                },
                "backtest_id": {
                    "type": "string",
                    "description": "回测ID（精确匹配），查询该回测关联的实盘会话产生的订单",
                },
                "limit": {
                    "type": "integer",
                    "description": "返回记录数量上限，默认 50，最大 200",
                    "default": 50,
                    "minimum": 1,
                    "maximum": 200,
                },
            },
            "required": [],
        }

    async def execute(
        self,
        order_id: str = "",
        symbol: str = "",
        side: str = "",
        status: str = "",
        backtest_id: str = "",
        limit: int = 50,
        user_id: str = "",  # 由 base.py 自动注入
        **kwargs,
    ) -> ToolResult:
        """执行查询并返回结果。"""
        if not user_id:
            return ToolResult(success=False, error="无法确定用户身份")

        # 构建过滤参数
        filters = {}
        if order_id:
            filters["order_id"] = order_id
        if symbol:
            filters["symbol"] = symbol
        if side:
            filters["side"] = side
        if status:
            filters["status"] = status
        if backtest_id:
            filters["backtest_id"] = backtest_id
        if limit:
            filters["limit"] = limit

        try:
            results, total = await _query_orders(user_id, filters)
            returned = len(results)
            has_more = total > returned

            logger.info(
                "[ListOrdersTool] user=%s total=%d returned=%d has_more=%s",
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
            logger.error("[ListOrdersTool] query failed: %s", e, exc_info=True)
            return ToolResult(success=False, error=f"查询失败: {e}")
