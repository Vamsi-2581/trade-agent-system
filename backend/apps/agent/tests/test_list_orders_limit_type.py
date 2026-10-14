"""回归测试：list_orders 收到字符串 limit 时不应崩溃。

根因：降级 DSML 伪 tool call 解析器（base.py::_parse_dsml_parameters）默认
string="true"，LLM 传回的 limit 会以**字符串**形态到达 execute()。execute()
未做类型转换，直接把字符串塞进 filters，_query_orders 里
`min(filters.get("limit", 50), 200)` 于是执行 min(str, int) →
`TypeError: '<' not supported between instances of 'int' and 'str'`，
最终回给用户的正是：`[list_orders] Error: 查询失败: '<' not supported ...`。

这里的接缝是**真实调用点**：走 ListOrdersTool.execute() → _query_orders()
的完整链路，只在 Order queryset 这一处打桩（DB I/O 与 bug 无关），
确保 min() 那条真实代码被字符串 limit 打到。
"""

import uuid
from unittest.mock import MagicMock, patch

import pytest
from apps.agent.tools.list_orders import ListOrdersTool

# _query_orders 开头的 close_old_connections() 会在连接已被前面的用例初始化过时
# 真的去 ensure_connection()，被 pytest-django 的 DB 访问守卫拦下
# （单跑本文件时连接尚未初始化，会短路，所以只有全目录跑才暴露）。
# 这里需要 django_db 放开守卫；ORM 已打桩，用例本身仍然不落库。
pytestmark = pytest.mark.django_db


def _fake_queryset(count: int = 0):
    """构造一个可链式调用的假 QuerySet，避免真正连库。"""
    qs = MagicMock()
    qs.filter.return_value = qs
    qs.select_related.return_value = qs
    qs.order_by.return_value = qs
    qs.count.return_value = count
    # queryset[:limit] → 切片结果（这里返回空，循环体不执行）
    qs.__getitem__.return_value = []
    return qs


@pytest.mark.asyncio
async def test_string_limit_does_not_crash():
    """limit 以字符串 '50' 传入（DSML 降级路径的真实形态），不应查询失败。"""
    user_uuid = str(uuid.uuid4())
    with patch("apps.trading.models.Order") as mock_order:
        mock_order.objects.filter.return_value = _fake_queryset(count=0)
        result = await ListOrdersTool().execute(user_id=user_uuid, limit="50")

    assert result.success is True, f"字符串 limit 导致查询失败：error={result.error!r}"
    # 正常应返回空结果而不是抛 TypeError
    assert result.data["total"] == 0


@pytest.mark.asyncio
async def test_int_limit_still_works():
    """回归保护：int limit（原生 function calling 的正常形态）不受影响。"""
    user_uuid = str(uuid.uuid4())
    with patch("apps.trading.models.Order") as mock_order:
        mock_order.objects.filter.return_value = _fake_queryset(count=0)
        result = await ListOrdersTool().execute(user_id=user_uuid, limit=50)

    assert result.success is True, f"int limit 回归失败：error={result.error!r}"
    assert result.data["total"] == 0
