"""回归测试：list_backtests 收到字符串 limit 时不应崩溃。

与 list_orders 同根因同签名：降级 DSML 伪 tool call 把 limit 当字符串传入，
_query_backtests 里 `min(filters.get("limit", 20), 100)` 执行 min(str, int) →
`TypeError: '<' not supported between instances of 'int' and 'str'`。
接缝同 list_orders：走 ListBacktestsTool.execute() → _query_backtests() 真实链路，
只在 BacktestResult queryset 处打桩（DB I/O 与 bug 无关）。
"""

import uuid
from unittest.mock import MagicMock, patch

import pytest
from apps.agent.tools.list_backtests import ListBacktestsTool

# _query_backtests 开头的 close_old_connections() 会在连接已被前面的用例初始化过时
# 真的去 ensure_connection()，被 pytest-django 的 DB 访问守卫拦下
# （单跑本文件时连接尚未初始化，会短路，所以只有全目录跑才暴露）。
# 这里需要 django_db 放开守卫；ORM 已打桩，用例本身仍然不落库。
pytestmark = pytest.mark.django_db


def _fake_queryset(count: int = 0):
    qs = MagicMock()
    qs.filter.return_value = qs
    qs.select_related.return_value = qs
    qs.order_by.return_value = qs
    qs.count.return_value = count
    qs.__getitem__.return_value = []
    return qs


@pytest.mark.asyncio
async def test_string_limit_does_not_crash():
    """limit 以字符串 '20' 传入（DSML 降级路径的真实形态），不应查询失败。"""
    user_uuid = str(uuid.uuid4())
    with patch("apps.backtest.models.BacktestResult") as mock_bt:
        mock_bt.objects.filter.return_value = _fake_queryset(count=0)
        result = await ListBacktestsTool().execute(user_id=user_uuid, limit="20")

    assert result.success is True, (
        f"字符串 limit 导致查询失败：error={result.error!r}"
    )
    assert result.data["total"] == 0


@pytest.mark.asyncio
async def test_int_limit_still_works():
    """回归保护：int limit（原生 function calling 的正常形态）不受影响。"""
    user_uuid = str(uuid.uuid4())
    with patch("apps.backtest.models.BacktestResult") as mock_bt:
        mock_bt.objects.filter.return_value = _fake_queryset(count=0)
        result = await ListBacktestsTool().execute(user_id=user_uuid, limit=20)

    assert result.success is True, f"int limit 回归失败：error={result.error!r}"
    assert result.data["total"] == 0
