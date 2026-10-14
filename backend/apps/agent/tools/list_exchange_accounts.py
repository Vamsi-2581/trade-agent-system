"""查询用户的交易所账户配置工具。

返回账户名称、交易所类型、是否激活、是否测试网。
不返回 API 密钥（安全脱敏）。
"""

from __future__ import annotations

import logging
import uuid

from asgiref.sync import sync_to_async
from django.db import close_old_connections

from .base import BaseTool, ToolResult

logger = logging.getLogger(__name__)


@sync_to_async
def _query_exchange_accounts(user_id: str, filters: dict) -> tuple[list[dict], int]:
    """在 sync 线程中安全查询交易所账户（脱敏）。

    Args:
        user_id: 用户 ID（UUID 或 username）
        filters: 过滤参数（account_id, limit）

    Returns:
        (results, total_count) - 结果列表和符合条件的总记录数
    """
    close_old_connections()

    from apps.exchange.models import ExchangeAccount

    # 尝试转换为 UUID
    try:
        _user_uuid = uuid.UUID(str(user_id))
        # ExchangeAccount 没有 user 字段，返回所有账户（未来可扩展为 user 关联）
        queryset = ExchangeAccount.objects.all()
    except (ValueError, TypeError):
        # 如果不是 UUID，返回所有账户
        queryset = ExchangeAccount.objects.all()

    # 应用过滤条件
    if filters.get("account_id"):
        queryset = queryset.filter(id__startswith=filters["account_id"])

    # 排序：按创建时间倒序
    queryset = queryset.order_by("-created_at")

    # 计算总数
    total = queryset.count()

    # 应用 limit（降级 DSML 路径可能把 limit 传成字符串，转 int 再取上界，
    # 与 box_range 对数字参数的处理一致）
    limit = min(int(filters.get("limit", 10)), 50)
    queryset = queryset[:limit]

    # 转换为字典列表（脱敏：不返回 api_key_enc/api_secret_enc）
    results = []
    for obj in queryset:
        results.append(
            {
                "id": str(obj.id),  # UUID → 字符串（完整）
                "exchange": obj.exchange,
                "label": obj.label,
                "is_active": obj.is_active,
                "testnet": obj.testnet,
                "created_at": obj.created_at.isoformat(),  # datetime → ISO 8601
            }
        )

    return results, total


class ListExchangeAccountsTool(BaseTool):
    """
    查询用户的交易所账户配置。

    返回账户名称、交易所类型、是否激活、是否测试网。
    不返回 API 密钥（安全脱敏）。
    当用户询问交易所配置、账户列表时使用。
    结果按创建时间倒序排列，最多返回 limit 条记录。
    """

    name = "list_exchange_accounts"
    description = (
        "查询用户的交易所账户配置。返回账户名称、交易所类型、是否激活、是否测试网。"
        "不返回 API 密钥（安全脱敏）。"
        "当用户询问交易所配置、账户列表时使用。"
        "结果按创建时间倒序排列，最多返回 limit 条记录。"
    )

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "account_id": {
                    "type": "string",
                    "description": "账户ID的前缀（模糊匹配开头），例如 'a1b2'",
                },
                "limit": {
                    "type": "integer",
                    "description": "返回记录数量上限，默认 10，最大 50",
                    "default": 10,
                    "minimum": 1,
                    "maximum": 50,
                },
            },
            "required": [],
        }

    async def execute(
        self,
        account_id: str = "",
        limit: int = 10,
        user_id: str = "",  # 由 base.py 自动注入
        **kwargs,
    ) -> ToolResult:
        """执行查询并返回结果。"""
        if not user_id:
            return ToolResult(success=False, error="无法确定用户身份")

        # 构建过滤参数
        filters = {}
        if account_id:
            filters["account_id"] = account_id
        if limit:
            filters["limit"] = limit

        try:
            results, total = await _query_exchange_accounts(user_id, filters)
            returned = len(results)
            has_more = total > returned

            logger.info(
                "[ListExchangeAccountsTool] user=%s total=%d returned=%d has_more=%s",
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
            logger.error(
                "[ListExchangeAccountsTool] query failed: %s", e, exc_info=True
            )
            return ToolResult(success=False, error=f"查询失败: {e}")
