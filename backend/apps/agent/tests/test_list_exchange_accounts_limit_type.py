"""回归测试：list_exchange_accounts 收到字符串 limit 时不应崩溃。

与 list_orders / list_backtests 同根因同签名（三个 list_* 查询工具共有的
`min(filters.get("limit", N), MAX)` 模式）：降级 DSML 伪 tool call 把 limit
当字符串传入，min(str, int) → `TypeError: '<' not supported between instances
of 'int' and 'str'`。接缝同前两者：走 ListExchangeAccountsTool.execute() →
_query_exchange_accounts() 真实链路，只在 ExchangeAccount queryset 处打桩
（注意该工具用 .all() 而非 .filter()）。
"""

import uuid
from unittest.mock import MagicMock, patch

import pytest
from apps.agent.tools.list_exchange_accounts import ListExchangeAccountsTool

# _query_exchange_accounts 开头的 close_old_connections() 会在连接已被前面的用例
# 初始化过时真的去 ensure_connection()，被 pytest-django 的 DB 访问守卫拦下
# （单跑本文件时连接尚未初始化，会短路，所以只有全目录跑才暴露）。
# 这里需要 django_db 放开守卫；ORM 已打桩，用例本身仍然不落库。
pytestmark = pytest.mark.django_db


def _fake_queryset(count: int = 0):
    qs = MagicMock()
    qs.all.return_value = qs
    qs.filter.return_value = qs
    qs.order_by.return_value = qs
    qs.count.return_value = count
    qs.__getitem__.return_value = []
    return qs


@pytest.mark.asyncio
async def test_string_limit_does_not_crash():
    """limit 以字符串 '10' 传入（DSML 降级路径的真实形态），不应查询失败。"""
    user_uuid = str(uuid.uuid4())
    with patch("apps.exchange.models.ExchangeAccount") as mock_ea:
        mock_ea.objects.all.return_value = _fake_queryset(count=0)
        result = await ListExchangeAccountsTool().execute(user_id=user_uuid, limit="10")

    assert result.success is True, f"字符串 limit 导致查询失败：error={result.error!r}"
    assert result.data["total"] == 0


@pytest.mark.asyncio
async def test_int_limit_still_works():
    """回归保护：int limit（原生 function calling 的正常形态）不受影响。"""
    user_uuid = str(uuid.uuid4())
    with patch("apps.exchange.models.ExchangeAccount") as mock_ea:
        mock_ea.objects.all.return_value = _fake_queryset(count=0)
        result = await ListExchangeAccountsTool().execute(user_id=user_uuid, limit=10)

    assert result.success is True, f"int limit 回归失败：error={result.error!r}"
    assert result.data["total"] == 0
