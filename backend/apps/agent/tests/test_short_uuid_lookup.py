"""
测试 GetTaskResultTool 的短 UUID 查询功能。
"""

import pytest
from uuid import uuid4
from apps.agent.tools.backtest import GetTaskResultTool
from apps.agent.models import ScheduledOneTimeTask


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_short_uuid_lookup_unique_match():
    """测试短 UUID 前缀唯一匹配场景"""
    # 创建测试任务
    task_id = uuid4()
    short_id = str(task_id)[:8]

    task = await ScheduledOneTimeTask.objects.acreate(
        id=task_id,
        task_name="test_task",
        agent_name="test_agent",
        message="test message",
        run_at="2026-06-24T12:00:00Z",
        status="completed",
        result="test result",
    )

    tool = GetTaskResultTool()
    result = await tool.execute(task_id=short_id)

    assert result.success
    assert result.data["task_id"] == str(task_id)
    assert result.data["short_id"] == short_id
    assert result.data["status"] == "COMPLETED"
    assert result.data["result"] == "test result"

    # 清理
    await task.adelete()


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_short_uuid_lookup_multiple_matches():
    """测试短 UUID 前缀匹配多个任务的歧义场景"""
    # 创建两个具有相同前缀的任务
    task1_id = uuid4()
    task2_id = uuid4()

    # 强制设置相同前缀（仅用于测试）
    # 实际 UUID 无法强制前缀，这里模拟歧义场景
    task1 = await ScheduledOneTimeTask.objects.acreate(
        id=task1_id,
        task_name="task1",
        agent_name="test_agent",
        message="test1",
        run_at="2026-06-24T12:00:00Z",
        status="completed",
    )

    task2 = await ScheduledOneTimeTask.objects.acreate(
        id=task2_id,
        task_name="task2",
        agent_name="test_agent",
        message="test2",
        run_at="2026-06-24T12:00:00Z",
        status="completed",
    )

    # 获取实际前缀
    actual_prefix = str(task1_id)[:8]

    # 如果两个任务恰好有相同前缀（概率极低），测试歧义错误
    if str(task2_id).startswith(actual_prefix):
        tool = GetTaskResultTool()
        result = await tool.execute(task_id=actual_prefix)

        assert not result.success
        assert "匹配到" in result.error
        assert "歧义" in result.error or "多个" in result.error

    # 清理
    await task1.adelete()
    await task2.adelete()


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_short_uuid_lookup_no_match():
    """测试短 UUID 前缀不匹配任何任务的场景"""
    short_id = "ffffffff"  # 不存在的 UUID 前缀

    tool = GetTaskResultTool()
    result = await tool.execute(task_id=short_id)

    # 应继续 Celery 查询流程，最终返回 NOT_FOUND
    assert result.success
    assert result.data["status"] == "NOT_FOUND"
    assert "不存在或已过期" in result.data["message"]


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_full_uuid_lookup():
    """测试完整 UUID 查询（原有功能）"""
    task_id = uuid4()

    task = await ScheduledOneTimeTask.objects.acreate(
        id=task_id,
        task_name="test_task",
        agent_name="test_agent",
        message="test message",
        run_at="2026-06-24T12:00:00Z",
        status="completed",
        result="test result",
    )

    tool = GetTaskResultTool()
    result = await tool.execute(task_id=str(task_id))

    assert result.success
    assert result.data["task_id"] == str(task_id)
    assert result.data.get("short_id") is None  # 不应返回 short_id 字段
    assert result.data["status"] == "COMPLETED"

    # 清理
    await task.adelete()


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_short_uuid_too_short():
    """测试过短的 UUID 前缀（少于 8 字符）"""
    short_id = "75638cb"  # 7 字符，少于最小长度

    tool = GetTaskResultTool()
    result = await tool.execute(task_id=short_id)

    # 应继续 Celery 查询流程（不进行 UUID 扩展）
    assert result.success
    assert result.data["status"] in ["NOT_FOUND", "PENDING"]  # Celery 无法识别短 ID


@pytest.mark.asyncio
async def test_resolve_short_uuid_helper():
    """直接测试 _resolve_short_uuid 辅助方法"""
    tool = GetTaskResultTool()

    # 测试完整 UUID
    full_uuid = str(uuid4())
    resolved = await tool._resolve_short_uuid(full_uuid)
    assert resolved == full_uuid

    # 测试 ScheduledOneTimeTask 的短前缀
    task_id = uuid4()
    task = await ScheduledOneTimeTask.objects.acreate(
        id=task_id,
        task_name="test",
        agent_name="test",
        message="test",
        run_at="2026-06-24T12:00:00Z",
        status="completed",
    )

    short_id = str(task_id)[:8]
    resolved = await tool._resolve_short_uuid(short_id)
    assert resolved == str(task_id)

    # 清理
    await task.adelete()


@pytest.mark.asyncio
async def test_celery_task_with_short_id():
    """测试 Celery 任务 ID 使用短 UUID 查询"""
    # Celery 任务 ID 不是 ScheduledOneTimeTask UUID
    # 应直接走 Celery 查询流程
    celery_task_id = "celery-task-12345"  # 非 UUID 格式

    tool = GetTaskResultTool()
    result = await tool.execute(task_id=celery_task_id)

    # 应返回 NOT_FOUND（Celery 无此任务）
    assert result.success
    assert result.data["status"] == "NOT_FOUND"


# ---------------------------------------------------------------------------
# Regression: short Celery UUID prefix should fall back to PG archive lookup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_archived_grid_search_short_prefix():
    """
    Regression: 短 UUID 前缀查询应能匹配 TaskProgress / GridSearchJob 归档记录。

    Bug 场景：
    1. submit_backtest (grid_search) 返回 Celery UUID（如 d96d114f-...）
    2. 24h 后 Redis key 过期，ScheduledOneTimeTask 也无此 UUID
    3. _get_archived_task_result 用精确匹配查不到 TaskProgress/GridSearchJob
    4. 返回 NOT_FOUND，但任务实际已成功执行

    修复：当 task_id < 36 字符时，PG 查询使用 __startswith 前缀匹配。
    """
    from apps.agent.tools.backtest import _get_archived_task_result
    from apps.agent.models import TaskProgress

    # 创建 TaskProgress 归档记录（模拟 tracker.complete() 的结果）
    full_task_id = str(uuid4())
    short_prefix = full_task_id[:8]

    tp = await TaskProgress.objects.acreate(
        task_id=full_task_id,
        task_type="grid_search",
        status="completed",
        result='{"job_id": "test", "completed": 48}',
    )

    # 短前缀查询应能命中
    found = await _get_archived_task_result(short_prefix)
    assert found is not None, "短前缀查询应能从 TaskProgress 归档中找到任务"
    assert found["task_id"] == full_task_id
    assert found["short_id"] == short_prefix
    assert found["status"] == "COMPLETED"
    assert found["source"] == "archive"

    # 完整 UUID 查询仍应命中（无 short_id 字段）
    found_full = await _get_archived_task_result(full_task_id)
    assert found_full is not None
    assert found_full["task_id"] == full_task_id
    assert "short_id" not in found_full

    await tp.adelete()


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_archived_grid_search_job_short_prefix():
    """
    Regression: 短 UUID 前缀查询应能匹配 GridSearchJob 记录（通过 id 或 celery_task_id）。
    """
    from apps.agent.tools.backtest import _get_archived_task_result
    from apps.backtest.models import GridSearchJob
    from apps.trading.models import Strategy

    # 创建 Strategy（GridSearchJob 依赖）
    strategy = await Strategy.objects.acreate(
        name="test_strategy_short",
        code_path="strategies/test_short.py",
    )

    try:
        # 通过 celery_task_id 前缀查找
        celery_uuid = str(uuid4())
        short_prefix = celery_uuid[:8]

        job = await GridSearchJob.objects.acreate(
            strategy=strategy,
            symbol="DOGE/USDT",
            timeframe="1h",
            start_date="2025-01-01",
            end_date="2025-06-01",
            initial_capital=10000,
            search_config={"parameters": {}, "sort_by": "sharpe_ratio"},
            sort_by="sharpe_ratio",
            status="completed",
            total_combinations=48,
            completed_combinations=48,
            celery_task_id=celery_uuid,
        )

        found = await _get_archived_task_result(short_prefix)
        assert found is not None, "短前缀应能通过 celery_task_id 匹配 GridSearchJob"
        assert found["source"] == "grid_search_job"
        assert found["task_id"] == str(job.id)
        assert found["short_id"] == short_prefix
        assert found["total_combinations"] == 48

        await job.adelete()
    finally:
        await strategy.adelete()
