"""
回测工具 — 异步提交回测任务 & 查询任务状态。

设计原则：
  回测是长任务（几秒到几分钟），不能在 LLM 工具调用循环里同步等待。
  Agent 通过两步完成回测：
    1. submit_backtest → 返回 celery task_id（立即返回）
    2. get_task_result  → 轮询任务状态 / 获取结果
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from .base import BaseTool, ToolResult
from apps.core.db_utils import db_async

logger = logging.getLogger(__name__)


async def _get_scheduler_user_id() -> str | None:
    """Return the Django User UUID for the system_scheduler account."""
    try:
        from apps.authentication.models import User as AuthUser

        user = await db_async(
            lambda: AuthUser.objects.filter(username="system_scheduler").first()
        )()
        if user:
            logger.info("Fallback: using system_scheduler user (%s)", user.id)
            return str(user.id)
    except Exception as exc:
        logger.warning("Error resolving system_scheduler user: %s", exc)
    return None


async def _resolve_django_user_id(channel_user_id: str | None) -> str | None:
    """将渠道 user_id（Telegram/Facebook 等）映射为 Django User UUID。

    渠道层传入的 user_id 可能是 Telegram 数字 ID、Feishu open_id 等，
    而 BacktestResult.user 是 ForeignKey → 期望 Django User 的 UUID 主键。
    直接传入渠道 ID 会导致 FK 约束错误。

    查找顺序：
    1. User.id（UUID 主键）— 适配 Web 用户
    2. telegram_id — 适配 Telegram 用户
    3. feishu_open_id — 适配飞书用户
    4. username — 兼容模式
    5. system_scheduler — 定时/自动任务的兜底用户
    """
    if not channel_user_id:
        # Fallback: system_scheduler user for scheduled/automated tasks
        return await _get_scheduler_user_id()
    try:
        from apps.authentication.models import User as AuthUser

        # 优先：尝试按 Django User UUID 主键查找（Web 用户场景）
        try:
            user_uuid = uuid.UUID(channel_user_id)
            user = await db_async(
                lambda: AuthUser.objects.filter(id=user_uuid).first()
            )()
            if user:
                logger.info(
                    "Resolved user_id '%s' via User.id (UUID primary key)",
                    channel_user_id,
                )
                return str(user.id)
        except (ValueError, AttributeError):
            pass  # 不是有效 UUID，继续走渠道 ID 查找逻辑

        # 渠道 ID 查找
        user = await db_async(
            lambda: AuthUser.objects.filter(telegram_id=channel_user_id).first()
        )()
        if user:
            logger.info("Resolved user_id '%s' via telegram_id", channel_user_id)
            return str(user.id)
        user = await db_async(
            lambda: AuthUser.objects.filter(feishu_open_id=channel_user_id).first()
        )()
        if user:
            logger.info("Resolved user_id '%s' via feishu_open_id", channel_user_id)
            return str(user.id)
        user = await db_async(
            lambda: AuthUser.objects.filter(username=channel_user_id).first()
        )()
        if user:
            logger.info("Resolved user_id '%s' via username", channel_user_id)
            return str(user.id)
        logger.warning(
            "Cannot resolve channel user_id '%s' to Django User — "
            "backtest will be created without user association",
            channel_user_id,
        )
        return None
    except Exception as exc:
        logger.warning("Error resolving channel user_id '%s': %s", channel_user_id, exc)
        return None


class SubmitBacktestTool(BaseTool):
    """
    提交回测任务到 Celery 队列，立即返回任务 ID。

    不阻塞等待结果，适合在 LLM Agent 工具循环中调用。
    结果通过 get_task_result 工具查询。
    """

    name = "submit_backtest"
    description = (
        "提交回测任务并立即返回任务ID。回测在后台异步运行，"
        "使用 get_task_result 查询任务状态和结果。"
    )

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "strategy_name": {
                    "type": "string",
                    "description": "策略名称（StrategyRegistry 注册名）",
                },
                "symbol": {
                    "type": "string",
                    "description": "交易对，如 BTC/USDT 或 BTCUSDT",
                },
                "timeframe": {
                    "type": "string",
                    "description": "K线周期，如 1h、4h、1d",
                },
                "initial_capital": {
                    "type": "number",
                    "description": "初始资金（USDT），默认 10000",
                    "default": 10000,
                },
                "commission_rate": {
                    "type": "number",
                    "description": "手续费率，默认 0.001（0.1%）",
                    "default": 0.001,
                },
                "parameters": {
                    "type": "object",
                    "description": "策略自定义参数（可选）",
                },
                "strategy_id": {
                    "type": "string",
                    "description": "Strategy 模型 UUID，用于关联回测结果（可选）",
                },
                "exchange": {
                    "type": "string",
                    "description": "交易所名称，默认 binance",
                    "default": "binance",
                },
                "start_date": {
                    "type": "string",
                    "description": "回测开始日期（ISO 格式，如 2024-01-01），默认最近 30 天",
                },
                "end_date": {
                    "type": "string",
                    "description": "回测结束日期（ISO 格式，如 2024-12-31），默认今天",
                },
                "benchmark": {
                    "type": "string",
                    "description": "基准策略名称，用于对比（如 buy_and_hold），可选",
                },
                "slippage_rate": {
                    "type": "number",
                    "description": "单边滑点率，默认 0.0005（0.05%）。回测成交价按此劣化",
                    "default": 0.0005,
                },
                "is_perp": {
                    "type": "boolean",
                    "description": "是否永续合约回测（决定是否结算资金费率成本），默认 false",
                    "default": False,
                },
                "include_funding": {
                    "type": "boolean",
                    "description": "永续回测是否计入资金费率成本，默认 true（仅 is_perp 时生效）",
                    "default": True,
                },
                "grid_search": {
                    "type": "object",
                    "description": (
                        "启用网格搜索时，指定参数范围进行自动优化。"
                        "当 grid_search.enabled=true 时，系统将生成参数组合并批量执行回测。"
                    ),
                    "properties": {
                        "enabled": {"type": "boolean", "default": False},
                        "parameters": {
                            "type": "object",
                            "description": (
                                "每个参数的搜索范围，格式: "
                                '{"ma_fast": {"min": 5, "max": 20, "step": 1}}'
                            ),
                        },
                        "sort_by": {
                            "type": "string",
                            "description": "排序指标",
                            "enum": ["sharpe_ratio", "total_return_pct", "win_rate"],
                            "default": "sharpe_ratio",
                        },
                        "max_combinations": {
                            "type": "integer",
                            "description": "最大组合数上限",
                            "default": 100,
                        },
                    },
                },
            },
            "required": ["strategy_name", "symbol", "timeframe"],
        }

    async def execute(self, **kwargs) -> ToolResult:
        strategy_name: str = kwargs.get("strategy_name", "")
        symbol: str = kwargs.get("symbol", "")
        timeframe: str = kwargs.get("timeframe", "")

        logger.info(
            "[SubmitBacktestTool] execute called with strategy_name=%s symbol=%s timeframe=%s",
            strategy_name,
            symbol,
            timeframe,
        )

        if not strategy_name or not symbol or not timeframe:
            return ToolResult(
                success=False,
                error="strategy_name、symbol、timeframe 均为必填项",
            )

        # 归一化 symbol：SOLUSDT / sol-usdt / SOL -> SOL/USDT
        from apps.backtest.tasks import normalize_binance_symbol

        symbol = normalize_binance_symbol(symbol)

        try:
            from datetime import date, timedelta
            from decimal import Decimal
            from apps.strategy_engine.backtest_mode import (
                create_empty_result_async,
                _resolve_strategy_id,
            )
            from apps.backtest.tasks import run_backtest_task

            strategy_id = kwargs.get("strategy_id")
            if not strategy_id:
                strategy_id = await _resolve_strategy_id(strategy_name)
            if not strategy_id:
                return ToolResult(
                    success=False,
                    error=f"无法解析策略: {strategy_name}",
                )

            start_date = kwargs.get("start_date", "")
            if not start_date:
                start_date = (date.today() - timedelta(days=30)).isoformat()
            end_date = kwargs.get("end_date", "")
            if not end_date:
                end_date = date.today().isoformat()
            initial_capital = kwargs.get("initial_capital", 10000)
            parameters = kwargs.get("parameters") or {}

            # Check if grid search is enabled
            grid_search = kwargs.get("grid_search") or {}
            if grid_search.get("enabled"):
                from apps.trading.models import Strategy
                from apps.backtest.models import GridSearchJob
                from apps.backtest.tasks import run_grid_search_task as run_grid_search

                try:
                    gs_strategy = await Strategy.objects.aget(id=strategy_id)
                except Strategy.DoesNotExist:
                    return ToolResult(
                        success=False,
                        error=f"无法找到策略: {strategy_id}",
                    )

                raw_gs_user_id = kwargs.get("user_id", "")
                django_gs_user_id = await _resolve_django_user_id(raw_gs_user_id)

                # Ensure user_id is set on the job so the task can fall back to it
                # for notification routing
                if not raw_gs_user_id:
                    logger.warning(
                        "[SubmitBacktestTool] user_id not provided in kwargs, "
                        "notification may not be delivered"
                    )

                create_job = db_async(GridSearchJob.objects.create)
                job = await create_job(
                    strategy=gs_strategy,
                    symbol=symbol,
                    timeframe=timeframe,
                    start_date=start_date,
                    end_date=end_date,
                    initial_capital=Decimal(str(initial_capital)),
                    search_config={
                        "parameters": grid_search.get("parameters", {}),
                        "sort_by": grid_search.get("sort_by", "sharpe_ratio"),
                        "max_combinations": grid_search.get("max_combinations", 100),
                    },
                    sort_by=grid_search.get("sort_by", "sharpe_ratio"),
                    source="agent",
                    user_id=django_gs_user_id if django_gs_user_id else None,
                )

                gs_task = run_grid_search.apply_async(
                    kwargs={
                        "job_id": str(job.id),
                        "user_id": django_gs_user_id if django_gs_user_id else None,
                    },
                    queue="grid_search",
                )
                await db_async(
                    lambda _j: job.__class__.objects.filter(id=job.id).update(
                        celery_task_id=gs_task.id
                    )
                )(job)

                # Immediately record submission so get_task_result can
                # distinguish "never submitted" from "genuinely pending"
                from apps.agent.task_tracker import TaskTracker

                TaskTracker.record_submitted(
                    task_id=gs_task.id,
                    user_id=raw_gs_user_id if raw_gs_user_id else "",
                    task_type="grid_search",
                    metadata={
                        "strategy_name": strategy_name,
                        "symbol": symbol,
                        "timeframe": timeframe,
                    },
                )

                return ToolResult(
                    success=True,
                    data={
                        "task_id": gs_task.id,
                        "grid_search_id": str(job.id),
                        "status": "PENDING",
                        "message": (
                            f"网格搜索任务已提交，task_id={gs_task.id}。"
                            f"策略={strategy_name} 标的={symbol} 周期={timeframe}。"
                            "使用 get_task_result 查询进度。"
                        ),
                    },
                )

            # Resolve channel user_id (e.g. Telegram "123456") to Django User UUID.
            raw_user_id = kwargs.get("user_id")
            logger.info(
                "[SubmitBacktestTool] user_id: raw=%s, type=%s",
                raw_user_id,
                type(raw_user_id).__name__,
            )
            django_user_id = await _resolve_django_user_id(raw_user_id)
            logger.info(
                "[SubmitBacktestTool] resolved django_user_id=%s for create_empty_result",
                django_user_id,
            )

            result_id = await create_empty_result_async(
                strategy_id=strategy_id,
                symbol=symbol,
                timeframe=timeframe,
                start_date=start_date,
                end_date=end_date,
                initial_capital=Decimal(str(initial_capital)),
                parameters=parameters,
                user_id=django_user_id,
            )
            if not result_id:
                return ToolResult(
                    success=False,
                    error="创建回测占位记录失败",
                )

            backtest_kwargs = {
                "strategy_name": strategy_name,
                "symbol": symbol,
                "timeframe": timeframe,
                "initial_capital": initial_capital,
                "commission_rate": kwargs.get("commission_rate", 0.001),
                "parameters": parameters,
                "strategy_id": strategy_id,
                "exchange": kwargs.get("exchange", "binance"),
                "start_date": start_date,
                "end_date": end_date,
                "result_id": result_id,
                "benchmark": kwargs.get("benchmark", ""),
                "slippage_rate": kwargs.get("slippage_rate"),
                "is_perp": bool(kwargs.get("is_perp", False)),
                "include_funding": bool(kwargs.get("include_funding", True)),
            }
            # Only set user_id if explicitly provided; otherwise let Supervisor
            # inject it. Setting None here would block Supervisor auto-injection.
            user_id = kwargs.get("user_id")
            if user_id:
                backtest_kwargs["user_id"] = user_id

            task = run_backtest_task.apply_async(kwargs=backtest_kwargs)
            logger.info(
                "[SubmitBacktestTool] submitted task_id=%s result_id=%s strategy=%s symbol=%s tf=%s",
                task.id,
                result_id,
                strategy_name,
                symbol,
                timeframe,
            )

            # Immediately record submission so get_task_result can
            # distinguish "never submitted" from "genuinely pending"
            from apps.agent.task_tracker import TaskTracker

            TaskTracker.record_submitted(
                task_id=task.id,
                user_id=user_id if user_id else "",
                task_type="backtest",
                metadata={
                    "strategy_name": strategy_name,
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "result_id": result_id,
                },
            )

            return ToolResult(
                success=True,
                data={
                    "task_id": task.id,
                    "result_id": result_id,
                    "status": "PENDING",
                    "message": (
                        f"回测任务已提交，task_id={task.id}。"
                        f"策略={strategy_name} 标的={symbol} 周期={timeframe}。"
                        "使用 get_task_result 查询进度和结果。"
                    ),
                },
            )
        except Exception as e:
            logger.error("[SubmitBacktestTool] submit failed: %s", e, exc_info=True)
            return ToolResult(success=False, error=f"提交回测任务失败: {e}")


async def _get_redis_task_data(task_id: str) -> dict | None:
    """Try to get full task result data from Redis Hash (may still be alive)."""
    import redis as _redis
    from django.conf import settings

    url = settings.REDIS_URL
    if url.rsplit("/", 1)[-1].isdigit():
        url = url.rsplit("/", 1)[0] + "/3"

    try:
        r = _redis.from_url(url, decode_responses=True)
        key = f"task:progress:{task_id}"
        data = r.hgetall(key)
        if not data:
            return None
        status = data.get("status", "")
        result = data.get("result", "")
        if status in ("completed", "failed") and result:
            return {
                "task_id": task_id,
                "status": status.upper(),
                "result": result,
            }
        return None
    except Exception:
        return None


#: 交易质量指标：存在 `BacktestResult.metrics` 里，但必须**提到 result 顶层**
#: 才能被指标锚定块看见（`BaseAgent._TASK_METRIC_KEYS` 读的是顶层键）。
#: 口径定义见 `apps.strategy_engine.backtest_mode.BacktestEngine._compute_trade_quality`。
_TRADE_QUALITY_KEYS = ("profit_factor", "payoff_ratio")


def _trade_quality_of(metrics: dict | None) -> dict:
    """`BacktestResult.metrics` → 顶层可锚定的交易质量键（缺失就整键不出现）。

    只挑已知的两个键，不整份展开 `metrics`：里面还住着 `regime_slice` /
    `walk_forward` / `capital_efficiency` 等嵌套载荷，塞进 result 顶层会污染
    「这一行的核心指标」这份清单，也会让锚定块变得不可读。
    """
    if not isinstance(metrics, dict):
        return {}
    return {k: metrics[k] for k in _TRADE_QUALITY_KEYS if metrics.get(k) is not None}


async def _get_archived_task_result(task_id: str) -> dict | None:
    """Fall back to PostgreSQL permanent storage.

    Checks in order:
    1. TaskProgress (Celery task archive — most direct match)
    2. BacktestResult (backtest completed, result stored by result_id)
    3. GridSearchJob (grid search completed, result stored by job_id)

    Supports short UUID prefix lookup (8-35 chars): uses ``__startswith``
    so that a Celery UUID prefix can still resolve after the Redis key has
    expired and the ScheduledOneTimeTask resolution has no match.
    """
    import logging as _logging

    _log = _logging.getLogger(__name__)

    is_short_id = len(task_id) < 36

    # 1. TaskProgress archive (Celery task_id → archive)
    try:
        from apps.agent.models import TaskProgress

        tp_filter = (
            {"task_id__startswith": task_id} if is_short_id else {"task_id": task_id}
        )
        tp = await db_async(lambda: TaskProgress.objects.filter(**tp_filter).first())()
        if tp is not None:
            data: dict = {
                "task_id": tp.task_id,  # actual full ID from DB
                "status": tp.status.upper(),
                "source": "archive",
            }
            if is_short_id:
                data["short_id"] = task_id
            if tp.result:
                data["result"] = tp.result
            if tp.completed_at:
                data["completed_at"] = tp.completed_at.isoformat()
            _log.info(
                "[get_task_result] found in TaskProgress: task_id=%s status=%s",
                tp.task_id,
                tp.status,
            )
            return data
    except Exception as e:
        _log.warning(
            "[get_task_result] TaskProgress lookup failed for %s: %s", task_id, e
        )

    # 2. BacktestResult (task_id might be a result_id or celery_task_id)
    try:
        from apps.backtest.models import BacktestResult
        from django.db.models import Q

        if is_short_id:
            br_filter = Q(id__startswith=task_id) | Q(
                celery_task_id__startswith=task_id
            )
        else:
            br_filter = Q(id=task_id) | Q(celery_task_id=task_id)
        br = await db_async(
            lambda: (
                BacktestResult.objects.select_related("strategy")
                .filter(br_filter)
                .first()
            )
        )()
        if br is not None:
            _log.info(
                "[get_task_result] found in BacktestResult: id=%s status=%s",
                br.id,
                br.review_status,
            )
            return {
                "task_id": str(br.id),
                **({"short_id": task_id} if is_short_id else {}),
                "status": "SUCCESS",
                "source": "backtest_result",
                "result": {
                    "result_id": str(br.id),
                    "strategy_name": br.strategy.name if br.strategy_id else "",
                    "symbol": br.symbol,
                    "timeframe": br.timeframe,
                    "start_date": br.start_date.isoformat(),
                    "end_date": br.end_date.isoformat(),
                    "initial_capital": str(br.initial_capital),
                    "final_capital": str(br.final_capital),
                    "total_return_pct": br.total_return_pct,
                    "sharpe_ratio": br.sharpe_ratio,
                    "max_drawdown_pct": br.max_drawdown_pct,
                    "win_rate": br.win_rate,
                    "total_trades": br.total_trades,
                    # 盈亏比 / profit_factor 落在 `metrics` 里（见
                    # `backtest_mode.trade_quality_metrics`），这里**提到 result 顶层**：
                    # 锚定块（`BaseAgent._extract_task_metrics`）只读顶层键，
                    # 留在 `metrics` 子字典里 = 守卫永远看不到它 = 报告里一出现
                    # 盈亏比就判不可核验。子字典照样原样透出，两处同源不冲突。
                    **_trade_quality_of(br.metrics),
                    "parameters": br.parameters,
                    "metrics": br.metrics,
                    "created_at": br.created_at.isoformat(),
                },
            }
    except Exception as e:
        _log.warning(
            "[get_task_result] BacktestResult lookup failed for %s: %s", task_id, e
        )

    # 3. GridSearchJob (task_id might be a job_id or celery_task_id)
    try:
        from apps.backtest.models import GridSearchJob
        from django.db.models import Q

        if is_short_id:
            job_filter = Q(id__startswith=task_id) | Q(
                celery_task_id__startswith=task_id
            )
        else:
            job_filter = Q(id=task_id) | Q(celery_task_id=task_id)
        job = await db_async(
            lambda: (
                GridSearchJob.objects.select_related("best_result", "strategy")
                .filter(job_filter)
                .first()
            )
        )()
        if job is not None:
            data: dict = {
                "task_id": str(job.id),
                **({"short_id": task_id} if is_short_id else {}),
                "status": job.status.upper(),
                "source": "grid_search_job",
                "symbol": job.symbol,
                "timeframe": job.timeframe,
                "total_combinations": job.total_combinations,
                "completed_combinations": job.completed_combinations,
                "created_at": job.created_at.isoformat(),
            }
            if job.best_result_id:
                data["best_result_id"] = str(job.best_result_id)
                data["best_result"] = {
                    "total_return_pct": job.best_result.total_return_pct,
                    "sharpe_ratio": job.best_result.sharpe_ratio,
                    "max_drawdown_pct": job.best_result.max_drawdown_pct,
                    "win_rate": job.best_result.win_rate,
                    "total_trades": job.best_result.total_trades,
                    # 与 BacktestResult 路径同一来源（`metrics`），口径不分叉。
                    **_trade_quality_of(job.best_result.metrics),
                }
            _log.info(
                "[get_task_result] found in GridSearchJob: id=%s status=%s",
                job.id,
                job.status,
            )
            return data
    except Exception as e:
        _log.warning(
            "[get_task_result] GridSearchJob lookup failed for %s: %s", task_id, e
        )

    return None


class GetTaskResultTool(BaseTool):
    """
    查询 Celery 任务的状态和结果。

    支持回测等所有通过 Celery 异步提交的任务。
    状态值: PENDING / STARTED / SUCCESS / FAILURE / RETRY / REVOKED
    """

    name = "get_task_result"
    description = (
        "查询异步任务（如回测）的当前状态和结果。"
        "状态为 submitted/PENDING/STARTED 时表示任务仍在运行，"
        "SUCCESS 时返回完整结果，FAILURE 时返回错误信息，"
        "NOT_FOUND 时表示任务 ID 不存在或已过期，需重新提交。"
    )

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "由 submit_backtest 返回的任务 ID（完整 UUID 或至少 8 字符的前缀）",
                },
            },
            "required": ["task_id"],
        }

    async def _resolve_short_uuid(self, task_id: str) -> str:
        """
        将短 UUID 前缀扩展为完整 UUID。

        Args:
            task_id: 用户输入的任务 ID（可能是完整 UUID 或前缀）

        Returns:
            完整的 UUID 字符串

        Raises:
            ValueError: 如果短前缀匹配到多个 UUID（歧义），或未找到匹配
        """
        from uuid import UUID

        # 如果已经是完整 UUID（36字符），直接返回
        if len(task_id) == 36:
            try:
                UUID(task_id)
                return task_id
            except ValueError:
                # 不是有效的 UUID 格式，继续后续处理（Celery task_id）
                pass

        # 短前缀查询（至少8字符）
        if len(task_id) >= 8:
            from apps.agent.models import ScheduledOneTimeTask

            # 查询所有以该前缀开头的 UUID
            matching_tasks = await db_async(
                lambda: list(
                    ScheduledOneTimeTask.objects.filter(
                        id__startswith=task_id
                    ).values_list("id", flat=True)
                )
            )()

            if len(matching_tasks) == 1:
                # 唯一匹配，返回完整 UUID
                return str(matching_tasks[0])
            elif len(matching_tasks) > 1:
                # 多个匹配，返回歧义错误
                raise ValueError(
                    f"短前缀 '{task_id}' 匹配到 {len(matching_tasks)} 个任务，"
                    "请提供更长的前缀或完整 UUID"
                )
            # 未找到匹配，继续后续处理（Celery task_id）

        # 返回原始 task_id（用于 Celery 查询）
        return task_id

    async def execute(self, task_id: str = "", **kwargs) -> ToolResult:
        if not task_id:
            return ToolResult(success=False, error="task_id 为必填项")

        try:
            # Step 0: Resolve short UUID prefix to full UUID
            try:
                resolved_id = await self._resolve_short_uuid(task_id)
            except ValueError as e:
                # 短 ID 匹配歧义（多个 UUID 匹配该前缀）
                return ToolResult(success=False, error=str(e))

            # Check if this is a ScheduledOneTimeTask DB UUID
            from uuid import UUID

            try:
                UUID(resolved_id)
                from apps.agent.models import ScheduledOneTimeTask

                one_time_task = await db_async(
                    lambda: ScheduledOneTimeTask.objects.filter(id=resolved_id).first()
                )()
                if one_time_task:
                    data = {
                        "task_id": str(one_time_task.id),  # Always return full UUID
                        "short_id": task_id
                        if len(task_id) < 36
                        else None,  # Show user input if it was a short ID
                        "status": one_time_task.status.upper(),
                        "source": "one_time",
                        "task_name": one_time_task.task_name,
                        "agent_name": one_time_task.agent_name,
                        "run_at": one_time_task.run_at.isoformat(),
                        "created_at": one_time_task.created_at.isoformat(),
                    }
                    if one_time_task.status == "completed" and one_time_task.result:
                        data["result"] = one_time_task.result
                    if one_time_task.error:
                        data["error"] = one_time_task.error
                    if one_time_task.executed_at:
                        data["executed_at"] = one_time_task.executed_at.isoformat()
                    if one_time_task.status == "pending":
                        data["status"] = "SCHEDULED"
                    return ToolResult(success=True, data=data)
            except ValueError:
                pass  # Not a UUID, fall through to normal Celery lookup

            # Use resolved_id for Celery lookup (it will be same as task_id if not a UUID)
            actual_task_id = resolved_id
            is_short_id = len(task_id) < 36 and task_id != resolved_id

            # Step 1: Check Redis Hash first — this tells us whether the task
            # was ever submitted. Celery AsyncResult alone returns PENDING for
            # both "never submitted" and "waiting to start", which is ambiguous.
            from apps.agent.task_tracker import TaskTracker

            tracker_status = TaskTracker.get_submission_status(actual_task_id)

            # Step 2: Query Celery for complementary state
            from celery_app import app as celery_app
            from celery.result import AsyncResult

            result: AsyncResult = celery_app.AsyncResult(actual_task_id)
            celery_state = result.state

            # Helper to add short_id to response if needed
            def _add_short_id(data: dict) -> dict:
                if is_short_id:
                    data["short_id"] = task_id
                return data

            # ── Combine Redis + Celery into a definitive status ──

            if tracker_status is None:
                # Redis Hash has no record of this task → it was NEVER submitted
                # or the Redis key has expired (24h TTL).
                # Before giving up, check Celery result backend (may survive Redis
                # key expiry) and PostgreSQL TaskProgress archive (permanent).
                archived = await _get_archived_task_result(actual_task_id)
                if archived:
                    return ToolResult(success=True, data=_add_short_id(archived))

                # Celery may still hold the result even if Redis key expired
                if celery_state == "SUCCESS":
                    res_data = result.result
                    # 检测结构化 FAILURE 结果（任务内捕获异常并返回错误详情）
                    if (
                        isinstance(res_data, dict)
                        and res_data.get("status") == "FAILURE"
                    ):
                        error_msg = res_data.get(
                            "error_message", res_data.get("error", "未知错误")
                        )
                        error_type = res_data.get("error_type", "Exception")
                        return ToolResult(
                            success=True,
                            data=_add_short_id(
                                {
                                    "task_id": actual_task_id,
                                    "status": "FAILURE",
                                    "error_type": error_type,
                                    "error": error_msg,
                                }
                            ),
                        )
                    # 检测 CANCELLED 状态
                    if (
                        isinstance(res_data, dict)
                        and res_data.get("status") == "CANCELLED"
                    ):
                        return ToolResult(
                            success=True,
                            data=_add_short_id(
                                {
                                    "task_id": actual_task_id,
                                    "status": "CANCELLED",
                                    "cancelled": res_data.get("cancelled", True),
                                    "completed": res_data.get("completed", 0),
                                    "message": res_data.get("message", "任务已被取消"),
                                }
                            ),
                        )
                    return ToolResult(
                        success=True,
                        data=_add_short_id(
                            {
                                "task_id": actual_task_id,
                                "status": "SUCCESS",
                                "result": res_data,
                            }
                        ),
                    )
                if celery_state == "FAILURE":
                    error_info = str(result.result) if result.result else "未知错误"
                    return ToolResult(
                        success=True,
                        data=_add_short_id(
                            {
                                "task_id": actual_task_id,
                                "status": "FAILURE",
                                "error": error_info,
                            }
                        ),
                    )

                return ToolResult(
                    success=True,
                    data=_add_short_id(
                        {
                            "task_id": actual_task_id,
                            "status": "NOT_FOUND",
                            "message": (
                                "该任务 ID 不存在或已过期（超过 24 小时）。"
                                "请重新提交任务。"
                            ),
                        }
                    ),
                )

            if celery_state == "SUCCESS":
                res_data = result.result
                # 检测结构化 FAILURE 结果（任务内捕获异常并返回错误详情）
                if isinstance(res_data, dict) and res_data.get("status") == "FAILURE":
                    error_msg = res_data.get(
                        "error_message", res_data.get("error", "未知错误")
                    )
                    error_type = res_data.get("error_type", "Exception")
                    task_params = res_data.get("task_params", {})

                    error_detail = (
                        f"回测任务失败 [{error_type}]: {error_msg}\n"
                        f"原始任务参数:\n"
                        f"  - strategy_name: {task_params.get('strategy_name', 'N/A')}\n"
                        f"  - symbol: {task_params.get('symbol', 'N/A')}\n"
                        f"  - timeframe: {task_params.get('timeframe', 'N/A')}\n"
                        f"  - exchange: {task_params.get('exchange', 'N/A')}\n"
                        f"  - start_date: {task_params.get('start_date', 'N/A')}\n"
                        f"  - end_date: {task_params.get('end_date', 'N/A')}\n"
                        f"  - initial_capital: {task_params.get('initial_capital', 'N/A')}\n"
                        f"  - commission_rate: {task_params.get('commission_rate', 'N/A')}\n"
                        f"  - parameters: {task_params.get('parameters', 'N/A')}\n\n"
                        f"请分析上述错误原因，修复问题后重新提交回测任务。"
                    )
                    return ToolResult(
                        success=True,
                        data=_add_short_id(
                            {
                                "task_id": actual_task_id,
                                "status": "FAILURE",
                                "error_type": error_type,
                                "error": error_msg,
                                "task_params": task_params,
                                "actionable_error": error_detail,
                            }
                        ),
                    )

                # 检测 CANCELLED 状态（任务被用户取消）
                if isinstance(res_data, dict) and res_data.get("status") == "CANCELLED":
                    return ToolResult(
                        success=True,
                        data=_add_short_id(
                            {
                                "task_id": actual_task_id,
                                "status": "CANCELLED",
                                "cancelled": res_data.get("cancelled", True),
                                "completed": res_data.get("completed", 0),
                                "message": res_data.get("message", "任务已被取消"),
                            }
                        ),
                    )

                return ToolResult(
                    success=True,
                    data=_add_short_id(
                        {
                            "task_id": actual_task_id,
                            "status": "SUCCESS",
                            "result": res_data,
                        }
                    ),
                )

            if celery_state == "FAILURE":
                error_info = str(result.result) if result.result else "未知错误"
                return ToolResult(
                    success=True,
                    data=_add_short_id(
                        {
                            "task_id": actual_task_id,
                            "status": "FAILURE",
                            "error": error_info,
                            "note": "此错误为 Celery 层面异常，请检查日志以获取详细信息。",
                        }
                    ),
                )

            if tracker_status in ("completed", "failed", "zombie"):
                # Redis already has terminal state; Celery may have lost the result.
                # Try Redis hash for full result first, then PostgreSQL archive.
                redis_data = await _get_redis_task_data(actual_task_id)
                if redis_data:
                    return ToolResult(success=True, data=_add_short_id(redis_data))
                archived = await _get_archived_task_result(actual_task_id)
                if archived:
                    return ToolResult(success=True, data=_add_short_id(archived))
                return ToolResult(
                    success=True,
                    data=_add_short_id(
                        {
                            "task_id": actual_task_id,
                            "status": tracker_status.upper(),
                            "message": f"任务已结束（{tracker_status}）",
                        }
                    ),
                )

            # PENDING / STARTED / RUNNING / SUBMITTED / RETRY
            info: Any = None
            if celery_state == "STARTED" and result.info:
                info = result.info
            display_state = (
                tracker_status
                if tracker_status in ("submitted", "running")
                else celery_state
            )
            return ToolResult(
                success=True,
                data=_add_short_id(
                    {
                        "task_id": actual_task_id,
                        "status": display_state,
                        "info": info,
                        "message": f"任务仍在运行中（{display_state}），请稍后再次查询。",
                    }
                ),
            )
        except Exception as e:
            logger.error("[GetTaskResultTool] query failed task_id=%s: %s", task_id, e)
            return ToolResult(success=False, error=f"查询任务状态失败: {e}")
