"""回归测试：网格搜索任务失败路径（2026-10-17 SOL/USDT 15m 线上故障）

线上现象：run_grid_search_task 触发 soft_time_limit 后，except 分支里的
`job.save()` 因为 DB 连接已被服务端 idle 超时掐断而抛
OperationalError('server closed the connection unexpectedly')，于是：
  1. 任务对外报的是「数据库连接中断」，真正的原因（超时）被覆盖；
  2. job.status 一个字都没写进去，grid_search_jobs 留下永远 running 的僵尸行。

这里锁住修复后的契约：失败路径自身绝不抛异常，且 status 一定尽力落库。
"""

from unittest.mock import patch

import pytest


@pytest.fixture
def strategy(db):
    from apps.trading.models import Strategy

    return Strategy.objects.create(
        name="TestGridFailurePathStrategy",
        code_path="strategies/test_grid_failure_path.py",
    )


@pytest.fixture
def grid_job(db, strategy):
    from apps.backtest.models import GridSearchJob
    from django.contrib.auth import get_user_model

    user = get_user_model().objects.create_user(
        username="gridfailuser", email="gridfail@example.com", password="testpass123"
    )
    return GridSearchJob.objects.create(
        user=user,
        strategy=strategy,
        symbol="SOL/USDT",
        timeframe="15m",
        start_date="2024-09-18",
        end_date="2026-09-18",
        initial_capital="10000.00",
        search_config={
            "parameters": {"rsi_period": {"min": 2, "max": 4, "step": 1}},
            "max_combinations": 10,
        },
        status="running",
        total_combinations=100,
    )


class TestMarkGridJobFailed:
    """_mark_grid_job_failed 本身的行为"""

    def test_marks_failed_and_appends_error_log(self, grid_job):
        from apps.backtest.tasks import _mark_grid_job_failed

        assert _mark_grid_job_failed(str(grid_job.id), "soft time limit exceeded")

        grid_job.refresh_from_db()
        assert grid_job.status == "failed"
        assert {"error": "soft time limit exceeded"} in grid_job.error_log

    def test_appends_to_existing_error_log(self, grid_job):
        from apps.backtest.models import GridSearchJob
        from apps.backtest.tasks import _mark_grid_job_failed

        GridSearchJob.objects.filter(id=grid_job.id).update(
            error_log=[{"combination_index": 3, "error": "boom"}]
        )

        assert _mark_grid_job_failed(str(grid_job.id), "soft time limit exceeded")

        grid_job.refresh_from_db()
        assert grid_job.error_log == [
            {"combination_index": 3, "error": "boom"},
            {"error": "soft time limit exceeded"},
        ]

    def test_survives_dead_connection_without_raising(self, grid_job):
        """第一次写库撞上连接被掐断时，重连后仍要落库，且不向外抛异常。"""
        from django.db import OperationalError
        from django.db.models.query import QuerySet

        from apps.backtest.tasks import _mark_grid_job_failed

        real_update = QuerySet.update
        state = {"calls": 0}

        def flaky_update(self, **kwargs):
            state["calls"] += 1
            if state["calls"] == 1:
                raise OperationalError("server closed the connection unexpectedly")
            return real_update(self, **kwargs)

        with patch.object(QuerySet, "update", flaky_update):
            ok = _mark_grid_job_failed(str(grid_job.id), "soft time limit exceeded")

        assert ok is True
        assert state["calls"] == 2
        grid_job.refresh_from_db()
        assert grid_job.status == "failed"

    def test_returns_false_without_raising_when_db_never_recovers(self, grid_job):
        from django.db import OperationalError

        from apps.backtest.tasks import _mark_grid_job_failed

        with patch(
            "apps.backtest.models.GridSearchJob.objects.filter",
            side_effect=OperationalError("server closed the connection unexpectedly"),
        ):
            # 永远写不进去也不能抛 —— 调用方正在处理原始异常
            assert (
                _mark_grid_job_failed(str(grid_job.id), "soft time limit exceeded")
                is False
            )

    def test_missing_job_is_not_an_error(self, db):
        from apps.backtest.tasks import _mark_grid_job_failed

        assert (
            _mark_grid_job_failed(
                "00000000-0000-0000-0000-000000000000", "soft time limit exceeded"
            )
            is False
        )


class TestRunGridSearchTaskTimeoutPath:
    """软超时分支：报 TimeoutError，不因 DB 连接断开而被 OperationalError 覆盖"""

    def test_timeout_returns_timeout_error_and_marks_failed(self, grid_job):
        from celery.exceptions import SoftTimeLimitExceeded

        from apps.backtest.tasks import run_grid_search_task

        with (
            patch(
                "apps.backtest.tasks._fetch_ohlcv_sync",
                return_value=[{"close": 1.0, "high": 1.0, "low": 1.0, "open": 1.0}],
            ),
            patch(
                "apps.strategy_engine.grid_search.generate_combinations",
                return_value=[{"rsi_period": 2}],
            ),
            patch(
                "apps.strategy_engine.runner.StrategyRunner.run_backtest",
                side_effect=SoftTimeLimitExceeded(),
            ),
        ):
            result = run_grid_search_task.apply(
                args=[str(grid_job.id), str(grid_job.user_id)]
            ).get(disable_sync_subtasks=False)

        assert result["status"] == "FAILURE"
        assert result["error_type"] == "TimeoutError"
        grid_job.refresh_from_db()
        assert grid_job.status == "failed"
        assert {"error": "soft time limit exceeded"} in grid_job.error_log

    def test_timeout_still_reports_timeout_when_save_fails(self, grid_job):
        """写库彻底失败时，返回的仍是 TimeoutError，而不是 DB 连接错误。"""
        from celery.exceptions import SoftTimeLimitExceeded

        from apps.backtest.tasks import run_grid_search_task

        with (
            patch(
                "apps.backtest.tasks._fetch_ohlcv_sync",
                return_value=[{"close": 1.0, "high": 1.0, "low": 1.0, "open": 1.0}],
            ),
            patch(
                "apps.strategy_engine.grid_search.generate_combinations",
                return_value=[{"rsi_period": 2}],
            ),
            patch(
                "apps.strategy_engine.runner.StrategyRunner.run_backtest",
                side_effect=SoftTimeLimitExceeded(),
            ),
            patch(
                "apps.backtest.tasks._mark_grid_job_failed", return_value=False
            ) as mock_mark,
        ):
            result = run_grid_search_task.apply(
                args=[str(grid_job.id), str(grid_job.user_id)]
            ).get(disable_sync_subtasks=False)

        assert result["status"] == "FAILURE"
        assert result["error_type"] == "TimeoutError"
        mock_mark.assert_called_once()
        assert mock_mark.call_args.args[1] == "soft time limit exceeded"


def _make_result(job, strategy, sharpe: float):
    """造一条「已完成组合」的回测结果行，模拟 run_backtest 落库后的状态。"""
    from apps.backtest.models import BacktestResult

    return BacktestResult.objects.create(
        strategy=strategy,
        user=job.user,
        symbol=job.symbol,
        timeframe=job.timeframe,
        start_date=job.start_date,
        end_date=job.end_date,
        initial_capital=job.initial_capital,
        final_capital=job.initial_capital,
        total_return_pct=sharpe * 10,
        sharpe_ratio=sharpe,
        grid_search_id=job.id,
        is_grid_search=True,
    )


class TestFinalizePartialGrid:
    """超时/失败时保留已完成组合（第 3 项修复）"""

    def test_keeps_completed_results_and_picks_best(self, grid_job, strategy):
        from apps.backtest.tasks import _finalize_partial_grid

        r_low = _make_result(grid_job, strategy, sharpe=0.5)
        r_best = _make_result(grid_job, strategy, sharpe=2.4)
        results = [
            {"result_id": str(r_low.id), "sharpe_ratio": 0.5},
            {"result_id": str(r_best.id), "sharpe_ratio": 2.4},
        ]

        with (
            patch("apps.backtest.tasks.dispatch_slice") as mock_slice,
            patch(
                "apps.strategy_factory.tasks.dispatch_capital_efficiency"
            ) as mock_eff,
        ):
            outcome = _finalize_partial_grid(
                grid_job, str(grid_job.id), results, cause="soft time limit exceeded"
            )

        assert outcome["partial_saved"] == 2
        assert outcome["best_result_id"] == str(r_best.id)

        grid_job.refresh_from_db()
        assert grid_job.best_result_id == r_best.id
        assert grid_job.completed_combinations == 2
        # 状态不由这里改，仍是 running（由 _mark_grid_job_failed 定终态）
        assert grid_job.status == "running"

        # 切片与资金效率都要投，且只投真实落库的那些
        assert mock_slice.call_count == 1
        assert {str(i) for i in mock_slice.call_args.args[0]} == {
            str(r_low.id),
            str(r_best.id),
        }
        assert mock_eff.call_count == 1

    def test_ignores_results_not_owned_by_job(self, grid_job, strategy):
        """别人家的结果不能被算进这个 job。"""
        from apps.backtest.models import GridSearchJob
        from apps.backtest.tasks import _finalize_partial_grid

        other_job = GridSearchJob.objects.create(
            user=grid_job.user,
            strategy=strategy,
            symbol="BTC/USDT",
            timeframe="1h",
            start_date=grid_job.start_date,
            end_date=grid_job.end_date,
            initial_capital=grid_job.initial_capital,
            search_config={"parameters": {}},
            total_combinations=1,
        )
        foreign = _make_result(other_job, strategy, sharpe=9.9)

        with (
            patch("apps.backtest.tasks.dispatch_slice") as mock_slice,
            patch("apps.strategy_factory.tasks.dispatch_capital_efficiency"),
        ):
            outcome = _finalize_partial_grid(
                grid_job,
                str(grid_job.id),
                [{"result_id": str(foreign.id), "sharpe_ratio": 9.9}],
                cause="boom",
            )

        assert outcome["partial_saved"] == 0
        mock_slice.assert_not_called()
        grid_job.refresh_from_db()
        assert grid_job.best_result_id is None

    def test_no_results_is_a_noop(self, grid_job):
        from apps.backtest.tasks import _finalize_partial_grid

        with (
            patch("apps.backtest.tasks.dispatch_slice") as mock_slice,
            patch(
                "apps.strategy_factory.tasks.dispatch_capital_efficiency"
            ) as mock_eff,
        ):
            outcome = _finalize_partial_grid(
                grid_job, str(grid_job.id), [], cause="soft time limit exceeded"
            )

        assert outcome["partial_saved"] == 0
        mock_slice.assert_not_called()
        mock_eff.assert_not_called()

    def test_never_raises_when_db_is_dead(self, grid_job):
        """结算跑在异常处理路径上，自己再炸会盖掉真正的原因。"""
        from django.db.models.query import QuerySet

        from apps.backtest.tasks import _finalize_partial_grid

        def boom(self, *args, **kwargs):
            raise RuntimeError("server closed the connection unexpectedly")

        with (
            patch.object(QuerySet, "values_list", boom),
            patch.object(QuerySet, "update", boom),
        ):
            outcome = _finalize_partial_grid(
                grid_job,
                str(grid_job.id),
                [{"result_id": "11111111-1111-1111-1111-111111111111"}],
                cause="soft time limit exceeded",
            )

        assert outcome["partial_saved"] in (0, 1)


class TestTimeoutKeepsPartialResults:
    """端到端：跑完若干组合后超时，已完成的组合必须留下来"""

    # 这条路径里任务会写 DB（backtest_results / grid_search_jobs），而 SQLite 只有一个
    # 写锁：测试外层事务正持着它，任务那条连接再写就是 "database table is locked"。
    # 生产是 PostgreSQL（行级锁），不存在这个问题；所以这里让外层事务先提交。
    @pytest.mark.django_db(transaction=True)
    def test_partial_results_survive_timeout(self, grid_job, strategy):
        from celery.exceptions import SoftTimeLimitExceeded

        from apps.backtest.models import BacktestResult
        from apps.backtest.tasks import run_grid_search_task

        combo_1 = {"rsi_period": 2}
        combo_2 = {"rsi_period": 3}
        call = {"n": 0}

        def fake_run_backtest(**kwargs):
            call["n"] += 1
            if call["n"] > 2:
                raise SoftTimeLimitExceeded()
            # 模拟 runner：返回前已经把结果写进 BacktestResult
            row = _make_result(grid_job, strategy, sharpe=float(call["n"]))
            return {"result_id": str(row.id), "sharpe_ratio": float(call["n"])}

        with (
            patch(
                "apps.backtest.tasks._fetch_ohlcv_sync",
                return_value=[{"close": 1.0, "high": 1.0, "low": 1.0, "open": 1.0}],
            ),
            patch(
                "apps.strategy_engine.grid_search.generate_combinations",
                return_value=[combo_1, combo_2, {"rsi_period": 4}],
            ),
            patch(
                "apps.strategy_engine.runner.StrategyRunner.run_backtest",
                side_effect=fake_run_backtest,
            ),
            patch("apps.backtest.tasks.dispatch_slice") as mock_slice,
            patch("apps.strategy_factory.tasks.dispatch_capital_efficiency"),
        ):
            result = run_grid_search_task.apply(
                args=[str(grid_job.id), str(grid_job.user_id)]
            ).get(disable_sync_subtasks=False)

        # 对外仍然是超时失败
        assert result["status"] == "FAILURE"
        assert result["error_type"] == "TimeoutError"
        assert result["partial_results_saved"] == 2

        grid_job.refresh_from_db()
        assert grid_job.status == "failed"
        assert grid_job.completed_combinations == 2
        # 最好那个组合被记下来了，用户不必重跑才能知道哪个参数最好
        assert grid_job.best_result is not None
        assert float(grid_job.best_result.sharpe_ratio) == 2.0
        assert BacktestResult.objects.filter(grid_search_id=grid_job.id).count() == 2
        # 已完成的组合仍要补齐切片
        mock_slice.assert_called_once()
        assert len(mock_slice.call_args.args[0]) == 2
