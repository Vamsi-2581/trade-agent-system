"""回归测试：验证网格搜索任务错误返回格式修复"""

import pytest
from unittest.mock import patch, MagicMock


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_grid_search_timeout_returns_failure_status():
    """验证超时任务返回 FAILURE 状态，而非 SUCCESS"""
    from apps.agent.tools.backtest import GetTaskResultTool

    # Mock Celery 返回超时结果（修复后的格式）
    mock_result = MagicMock()
    mock_result.state = "SUCCESS"
    mock_result.result = {
        "status": "FAILURE",
        "error_type": "TimeoutError",
        "error_message": "time limit exceeded",
        "error": "time limit exceeded",
    }

    with patch("celery_app.app.AsyncResult", return_value=mock_result):
        with patch(
            "apps.agent.task_tracker.TaskTracker.get_submission_status",
            return_value=None,
        ):
            tool = GetTaskResultTool()
            result = await tool.execute(task_id="test-timeout-task-123")

            assert result.success
            assert result.data["status"] == "FAILURE"
            assert result.data["error"] == "time limit exceeded"
            assert result.data["error_type"] == "TimeoutError"


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_grid_search_job_not_found_returns_failure():
    """验证 job not found 返回 FAILURE 状态"""
    from apps.agent.tools.backtest import GetTaskResultTool

    mock_result = MagicMock()
    mock_result.state = "SUCCESS"
    mock_result.result = {
        "status": "FAILURE",
        "error_type": "NotFoundError",
        "error_message": "job not found",
        "error": "job not found",
    }

    with patch("celery_app.app.AsyncResult", return_value=mock_result):
        with patch(
            "apps.agent.task_tracker.TaskTracker.get_submission_status",
            return_value=None,
        ):
            tool = GetTaskResultTool()
            result = await tool.execute(task_id="test-not-found-task-456")

            assert result.success
            assert result.data["status"] == "FAILURE"
            assert result.data["error"] == "job not found"


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_grid_search_cancelled_returns_cancelled_status():
    """验证 cancelled 任务返回 CANCELLED 状态"""
    from apps.agent.tools.backtest import GetTaskResultTool

    mock_result = MagicMock()
    mock_result.state = "SUCCESS"
    mock_result.result = {
        "status": "CANCELLED",
        "cancelled": True,
        "completed": 5,
        "message": "任务已被取消，已完成 5 个组合",
    }

    with patch("celery_app.app.AsyncResult", return_value=mock_result):
        with patch(
            "apps.agent.task_tracker.TaskTracker.get_submission_status",
            return_value=None,
        ):
            tool = GetTaskResultTool()
            result = await tool.execute(task_id="test-cancelled-task-789")

            assert result.success
            assert result.data["status"] == "CANCELLED"
            assert result.data["cancelled"] is True
            assert result.data["completed"] == 5


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_ohlcv_fetch_failed_returns_failure():
    """验证 OHLCV fetch failed 返回 FAILURE 状态"""
    from apps.agent.tools.backtest import GetTaskResultTool

    mock_result = MagicMock()
    mock_result.state = "SUCCESS"
    mock_result.result = {
        "status": "FAILURE",
        "error_type": "DataError",
        "error_message": "OHLCV fetch failed",
        "error": "OHLCV fetch failed",
    }

    with patch("celery_app.app.AsyncResult", return_value=mock_result):
        with patch(
            "apps.agent.task_tracker.TaskTracker.get_submission_status",
            return_value=None,
        ):
            tool = GetTaskResultTool()
            result = await tool.execute(task_id="test-ohlcv-failed-task")

            assert result.success
            assert result.data["status"] == "FAILURE"
            assert result.data["error"] == "OHLCV fetch failed"
            assert result.data["error_type"] == "DataError"
