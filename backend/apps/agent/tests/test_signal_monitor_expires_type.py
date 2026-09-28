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

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apps.agent.tools.signal_monitor import AddSignalMonitorTool


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
