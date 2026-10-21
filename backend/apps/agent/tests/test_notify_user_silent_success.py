"""P0 回归：`notify_user` 把「用户解析不到」和「Redis 推送失败」当成发送成功。

背景（2026-10-19 排查，纠正上一轮的误判）：

上一轮我曾断言「`notify_user` 的 DB 连接失败没被当作失败」。**该判断是错的**：
拉出生产完整异常栈后可见，`stale DB connection in sync thread; closing and
retrying once` 之后紧跟 `notification sent to user ...`，并且
`notifications` 表在 07:56:48.260657 确实落了行（`a00612ed`）。也就是说
连接重试**按设计工作**，那次 DB 故障是自愈的，不是缺陷。

真正「失败被当成成功」的出口有两个，且都在生产日志里可复现：

1. **`if user:` 静默跳过**（`_persist_notification` 内）。用户解析不到时，
   既不写库也不报错，`_persist_notification` 正常返回 → 外层认为持久化成功。
   `notifications` 表无行，但工具返回 `success=True`。
2. **`_send_via_redis` 吞掉所有异常**。`except Exception: logger.warning(...)`
   之后不 raise、不返回状态，`execute` 无从得知推送失败 → 仍报成功。
   而 Redis 推送是「实时送达用户」的唯一通道，写库只保证通知中心可查。

两者叠加的后果：用户什么都没收到，Agent 却拿到 `success=True`，于是
`had_tool_results` 被置位、报告被视为已投递——这正是 2026-09-18 / 2026-10-19
两起虚构事故共用的温床。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apps.agent.tools.notify_user import NotifyUserTool


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_unresolvable_user_is_reported_as_failure():
    """核心回归 1：用户解析不到 → 什么都没写 → 必须报失败，不得谎报成功。

    生产对应形态：`_persist_notification` 里 `if user:` 为假时静默跳过。

    这里**不 mock `_persist_notification`**（缺陷就在它内部），而是让
    真实的用户查询查不到人：走完 id/username/feishu_open_id/telegram_id
    四条兜底后 user 仍为空。
    """
    tool = NotifyUserTool()
    tool._send_via_redis = MagicMock()

    # 用真实 User 模型 + 真实 DB：查询一个不存在的 UUID → DoesNotExist，
    # 四条兜底（username/feishu_open_id/telegram_id）也都查不到。
    # 这里必须用真实模型：生产代码 `except (ValueError, User.DoesNotExist)`
    # 需要 User.DoesNotExist 是真实异常类，替身会抛 TypeError。
    import uuid as _uuid

    ghost_id = str(_uuid.uuid4())

    result = await tool.execute(message="回测报告", user_id=ghost_id)

    assert result.success is False, "用户解析不到、通知既未入库也未推送，工具却报成功"
    assert "用户" in (result.error or "")


@pytest.mark.asyncio
async def test_redis_push_failure_is_not_reported_as_success():
    """核心回归 2：写库成功但实时推送失败 → 不得报「已发送」。

    Redis 是实时送达通道；写库只让通知中心可查。吞掉推送异常等于
    「用户没收到，Agent 以为收到了」。

    这里**不 mock `_send_via_redis`**，而是让真实的 redis 客户端连不上——
    缺陷正是该方法内部 `except Exception: logger.warning(...)` 把异常吞掉，
    mock 掉方法本身就把要测的那段代码跳过去了。
    """
    tool = NotifyUserTool()
    tool._persist_notification = AsyncMock(return_value=None)

    with patch("redis.from_url", side_effect=RuntimeError("redis down")):
        result = await tool.execute(message="回测报告", user_id="user1")

    assert result.success is False, "Redis 推送失败，工具仍报成功"
    assert "推送" in (result.error or "") or "Redis" in (result.error or "")


@pytest.mark.asyncio
async def test_db_failure_then_retry_success_still_succeeds():
    """对照：DB 瞬时故障 + 重试成功 + 推送成功 → 必须报成功（不得误伤）。

    这条锁住生产里真实发生并被正确处理的路径（07:56:48 那次自愈）。
    """
    tool = NotifyUserTool()
    tool._persist_notification = AsyncMock(
        side_effect=[RuntimeError("server closed the connection unexpectedly"), None]
    )
    tool._send_via_redis = MagicMock()

    with patch("django.db.connections.close_all"):
        result = await tool.execute(message="回测报告", user_id="user1")

    assert result.success is True
    assert tool._persist_notification.await_count == 2
    assert tool._send_via_redis.call_count == 1
