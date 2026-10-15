"""回归测试：AddSignalMonitorTool 的 expires_hours 以字符串传入（降级 DSML 路径形态）
时应被归一为 float，而不是触发 timedelta / 比较的类型错误。

根因同 list_orders：_parse_dsml_parameters 默认 string="true"，LLM 回传的
expires_hours 会以字符串到达 execute()。修复前 `expires_hours and expires_hours > 0`
即抛 TypeError: '>' not supported between instances of 'str' and 'int'
（字符串与 int 比较），被 execute 的 except 捕获后返回"创建监控任务失败: ..."。

接缝：走真实 execute()，只打桩三个 I/O 边界：
  - _get_user（DB 取用户）
  - FrameManager.get_instance（Redis + frame 状态）
  - apps.signal_monitor.models.SignalMonitor（DB 建记录）
断言真正传给模型的是 float 且 expires_at ≈ now + expires_hours。
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from asgiref.sync import sync_to_async

from apps.agent.tools.signal_monitor import AddSignalMonitorTool


def _drop_cached_connections():
    """丢弃本线程 thread-local 的 DB 连接（调用方决定在哪个线程执行）。

    本文件把三个 I/O 边界都打了桩，断言的是参数归一化，本不该碰库；但
    ``AddSignalMonitorTool`` 的 ``_create_monitor`` 是 ``sync_to_async``，进入
    该线程后会先 ``close_old_connections()``。若此前有测试用过 ORM，连接会残留
    在该线程的 thread-local ``connections`` 上，``close_old_connections()`` 就会
    碰它 → 撞上 pytest-django 的 ``django_db`` blocker，抛出
    "Database access not allowed"。

    不能用 ``connection.close()``：settings/test.py 的 sqlite 测试库是
    ``mode=memory``（Django 测试运行器把 NAME 换成 memorydb），sqlite backend
    对内存库的 ``close()`` 是 no-op（防止销毁库），关不掉；因此直接把
    ``connection`` 引用置空，让下次使用重新建连。
    """
    from django.db import connections

    for alias in connections:
        conn = connections[alias]
        if conn.connection is not None:
            conn.connection = None
        conn.health_check_done = False


@pytest.fixture(autouse=True)
def _isolate_cached_connections():
    """用例前清掉主线程与线程池线程里的残留连接，保证打桩用例不被污染。"""
    _drop_cached_connections()  # 主线程
    asyncio.run(sync_to_async(_drop_cached_connections, thread_sensitive=True)())
    yield


class _FakeMonitor:
    """替代 SignalMonitor 实例：只承载 execute() 结果读取的字段。"""

    id = "11111111-1111-1111-1111-111111111111"
    name = "BTC突破"
    symbol = "BTC/USDT"
    indicator_type = "price_watch"


@pytest.mark.asyncio
async def test_expires_hours_string_coerced_to_float():
    """expires_hours 以字符串 '24' 传入：必须成功创建，且 expires_at 为 float 小时数算出的时刻。"""
    tool = AddSignalMonitorTool()
    fake_fm = MagicMock()
    fake_fm.status.return_value = {"assist": "running"}
    create_mock = MagicMock(return_value=_FakeMonitor())

    before = datetime.now(timezone.utc)
    with (
        patch(
            "apps.agent.tools.signal_monitor._get_user",
            AsyncMock(return_value=MagicMock()),
        ),
        patch(
            "apps.agent.frame_manager.FrameManager.get_instance",
            classmethod(lambda cls: fake_fm),
        ),
        patch("apps.signal_monitor.models.SignalMonitor.objects.create", create_mock),
    ):
        result = await tool.execute(
            name="BTC突破",
            symbol="BTC/USDT",
            indicator_type="price_watch",
            condition_operator="cross_above",
            target_value=75000.0,
            expires_hours="24",
            user_id="22222222-2222-2222-2222-222222222222",
        )
    after = datetime.now(timezone.utc)

    assert result.success is True, (
        f"字符串 expires_hours 导致创建失败（修复前为 str/int 比较 TypeError）：{result.error!r}"
    )
    expires_at = create_mock.call_args.kwargs["expires_at"]
    assert expires_at is not None, "expires_hours='24' 应算出过期时间而非 None"
    assert before + timedelta(hours=24) <= expires_at <= after + timedelta(hours=24)


@pytest.mark.asyncio
async def test_expires_hours_float_still_works():
    """回归保护：float expires_hours（原生 function calling 正常形态）不受影响。"""
    tool = AddSignalMonitorTool()
    fake_fm = MagicMock()
    fake_fm.status.return_value = {"assist": "running"}
    create_mock = MagicMock(return_value=_FakeMonitor())

    with (
        patch(
            "apps.agent.tools.signal_monitor._get_user",
            AsyncMock(return_value=MagicMock()),
        ),
        patch(
            "apps.agent.frame_manager.FrameManager.get_instance",
            classmethod(lambda cls: fake_fm),
        ),
        patch("apps.signal_monitor.models.SignalMonitor.objects.create", create_mock),
    ):
        result = await tool.execute(
            name="BTC突破",
            symbol="BTC/USDT",
            indicator_type="price_watch",
            condition_operator="cross_above",
            target_value=75000.0,
            expires_hours=24,
            user_id="22222222-2222-2222-2222-222222222222",
        )

    assert result.success is True, result.error
    assert create_mock.call_args.kwargs["expires_at"] is not None
