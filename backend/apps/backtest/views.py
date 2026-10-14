from datetime import date, datetime, timedelta

import logging

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from django.core.paginator import Paginator
from django.db import models
from django.utils import timezone

logger = logging.getLogger(__name__)

from .models import BacktestResult, BacktestTrade
from .serializers import (
    BacktestResultSerializer,
    BacktestDetailSerializer,
    BacktestTradeSerializer,
)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def result_list(request):
    # --- grouped mode ---
    grouped = request.query_params.get("grouped") == "1"
    if grouped:
        return _build_grouped_response(request)

    # --- original flat mode (unchanged) ---
    page = int(request.query_params.get("page", 1))
    page_size = min(int(request.query_params.get("page_size", 20)), 200)
    queryset = BacktestResult.objects.defer(
        "ohlcv_data", "equity_curve", "drawdown_curve", "indicator_data",
    ).order_by("-created_at")
    paginator = Paginator(queryset, page_size)
    page_obj = paginator.get_page(page)
    return Response(
        {
            "count": paginator.count,
            "num_pages": paginator.num_pages,
            "current_page": page_obj.number,
            "results": BacktestResultSerializer(page_obj, many=True).data,
        }
    )


def _grid_job_error_summary(job) -> str | None:
    """提取网格搜索任务失败原因（取 error_log 最后一条的 error 字段）。"""
    if getattr(job, "status", None) != "failed":
        return None
    log = getattr(job, "error_log", None) or []
    if not log:
        return None
    last = log[-1]
    msg = last.get("error") if isinstance(last, dict) else str(last)
    return str(msg)[:200] if msg else None


def _build_grouped_response(request):
    """按 grid_search_id 聚合回测结果，返回分组结构。

    包含三种类型的分组：
    - grid_search: 有至少一个 BacktestResult 的网格搜索任务
    - pending_grid_search: 还没有 BacktestResult 的网格搜索任务（pending/running）
    - orphaned_grid_search: 网格搜索任务已删除，但留有 BacktestResult
    - single: 单个回测结果
    """
    from .models import GridSearchJob
    from .serializers import BacktestResultSerializer, GridSearchGroupSerializer, SingleGroupSerializer
    from collections import defaultdict

    user = request.user
    user_filter = models.Q(user=user) | models.Q(user__isnull=True)
    results = BacktestResult.objects.filter(user_filter).select_related("strategy").defer(
        "ohlcv_data", "equity_curve", "drawdown_curve", "indicator_data",
    )

    # 按 grid_search_id 分组
    grid_map = defaultdict(list)
    singles = []
    for r in results:
        if r.is_grid_search and r.grid_search_id:
            grid_map[r.grid_search_id].append(r)
        else:
            singles.append(r)

    groups = []
    existing_job_ids = set(grid_map.keys())

    # ── 1. 构建有结果的网格搜索分组 ──
    # prefetch jobs
    jobs_by_id = {}
    if existing_job_ids:
        for job in GridSearchJob.objects.filter(id__in=existing_job_ids).select_related("strategy"):
            jobs_by_id[str(job.id)] = job

    for job_id, job_results in grid_map.items():
        job = jobs_by_id.get(str(job_id))
        if job:
            strategy_name = job.strategy.name if job.strategy else "Unknown"
            job_name = f"{strategy_name} - {job.symbol}/{job.timeframe}"
            # 计算最佳指标
            best_return = max((r.total_return_pct or 0) for r in job_results)
            best_sharpe = max((r.sharpe_ratio or 0) for r in job_results)
            groups.append({
                "type": "grid_search",
                "job_id": str(job_id),
                "job_name": job_name,
                "symbol": job.symbol,
                "timeframe": job.timeframe,
                "status": job.status,
                "total_combinations": job.total_combinations,
                "completed": job.completed_combinations,
                "best_return_pct": best_return,
                "best_sharpe": best_sharpe,
                "created_at": job.created_at,
                "error": _grid_job_error_summary(job),
                "results": BacktestResultSerializer(job_results, many=True).data,
            })
        else:
            # 已删除的 job → orphaned
            first = job_results[0]
            groups.append({
                "type": "orphaned_grid_search",
                "job_id": str(job_id),
                "job_name": "未知任务",
                "symbol": first.symbol,
                "timeframe": first.timeframe,
                "status": "completed",
                "total_combinations": len(job_results),
                "completed": len(job_results),
                "best_return_pct": max((r.total_return_pct or 0) for r in job_results),
                "best_sharpe": max((r.sharpe_ratio or 0) for r in job_results),
                "created_at": first.created_at,
                "results": BacktestResultSerializer(job_results, many=True).data,
            })

    # ── 2. 构建无结果的网格搜索分组（pending/running，暂无 BacktestResult） ──
    pending_jobs = GridSearchJob.objects.filter(
        user_filter
    ).exclude(
        id__in=list(existing_job_ids)
    ).select_related("strategy").order_by("-created_at")

    for job in pending_jobs:
        strategy_name = job.strategy.name if job.strategy else "Unknown"
        job_name = f"{strategy_name} - {job.symbol}/{job.timeframe}"
        groups.append({
            "type": "grid_search",
            "job_id": str(job.id),
            "job_name": job_name,
            "symbol": job.symbol,
            "timeframe": job.timeframe,
            "status": job.status,
            "total_combinations": job.total_combinations,
            "completed": job.completed_combinations,
            "best_return_pct": None,
            "best_sharpe": None,
            "created_at": job.created_at,
            "error": _grid_job_error_summary(job),
            "results": [],
        })

    # ── 3. 单次回测 ──
    for r in singles:
        groups.append({
            "type": "single",
            "result": BacktestResultSerializer(r).data,
        })

    # 按 created_at 倒序（统一转为字符串避免 datetime vs str 比较错误）
    groups.sort(
        key=lambda g: str(g.get("created_at") or g.get("result", {}).get("created_at") or ""),
        reverse=True,
    )

    page_size = 10  # groups per page
    paginator = Paginator(groups, page_size)
    page_num = int(request.query_params.get("page", 1))
    page_obj = paginator.get_page(page_num)

    return Response({
        "groups": page_obj.object_list,
        "group_count": paginator.count,
        "total_records": results.count(),
        "num_pages": paginator.num_pages,
        "current_page": page_obj.number,
    })


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def result_detail(request, pk):
    result = BacktestResult.objects.get(pk=pk)
    return Response(BacktestResultSerializer(result).data)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def result_detail_full(request, pk):
    """Returns summary + equity_curve + drawdown_curve (no trades)."""
    result = BacktestResult.objects.get(pk=pk)
    return Response(BacktestDetailSerializer(result).data)


TIMEFRAME_SECONDS: dict[str, int] = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900,
    "30m": 1800, "1h": 3600, "4h": 14400,
    "1d": 86400, "1w": 604800,
}


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def result_earlier_ohlcv(request, pk):
    """返回回测起点之前的更早 K 线（无指标/交易标记）。

    Query params:
      limit: int (default 200, max 1000) — 期望 K 线根数
      cursor: str (required) — 已加载的最早 K 线时间戳（ISO 格式），
              首次调用传 result.start_date，后续调用传 preOhlcv 第一条的时间戳
      timeframe: str (optional) — K 线周期，默认使用回测结果的 timeframe
      symbol: str (optional) — 交易对，默认使用回测结果的 symbol
    """
    try:
        result = BacktestResult.objects.get(pk=pk)
    except BacktestResult.DoesNotExist:
        return Response({"error": "Backtest result not found"}, status=404)

    limit = min(int(request.query_params.get("limit", 200)), 1000)
    cursor = request.query_params.get("cursor", "")
    if not cursor:
        return Response({"error": "cursor parameter is required"}, status=400)

    timeframe = request.query_params.get("timeframe", result.timeframe)
    symbol = request.query_params.get("symbol", result.symbol)

    tf_seconds = TIMEFRAME_SECONDS.get(timeframe, 3600)

    try:
        end_dt = datetime.fromisoformat(cursor) - timedelta(seconds=1)
    except (ValueError, TypeError):
        return Response({"error": f"Invalid cursor: {cursor}"}, status=400)

    # Bounded range: fetch `limit` bars worth of time + 1 day buffer backwards
    start_dt = end_dt - timedelta(seconds=tf_seconds * limit + 86400)

    from .tasks import _fetch_ohlcv_sync

    # Use a large CCXT page size for fast fetching, then trim to requested count
    ohlcv = _fetch_ohlcv_sync(
        symbol=symbol,
        timeframe=timeframe,
        limit=1000,  # CCXT page size — large to minimise round-trips
        start_date=start_dt.isoformat(),
        end_date=end_dt.isoformat(),
    )
    # Return only the last `limit` bars (closest to cursor)
    ohlcv = ohlcv[-limit:]
    # _fetch_ohlcv_sync returns ascending (oldest first)
    return Response({"ohlcv_data": ohlcv, "count": len(ohlcv)})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def result_later_ohlcv(request, pk):
    """返回回测终点之后的更晚 K 线（无指标/交易标记）。

    Query params:
      limit: int (default 200, max 1000) — 期望 K 线根数
      cursor: str (required) — 已加载的最晚 K 线时间戳（ISO 格式），
              首次调用传 result.end_date，后续调用传 postOhlcv 最后一条的时间戳
      timeframe: str (optional) — K 线周期，默认使用回测结果的 timeframe
      symbol: str (optional) — 交易对，默认使用回测结果的 symbol
    """
    try:
        result = BacktestResult.objects.get(pk=pk)
    except BacktestResult.DoesNotExist:
        return Response({"error": "Backtest result not found"}, status=404)

    limit = min(int(request.query_params.get("limit", 200)), 1000)
    cursor = request.query_params.get("cursor", "")
    if not cursor:
        return Response({"error": "cursor parameter is required"}, status=400)

    timeframe = request.query_params.get("timeframe", result.timeframe)
    symbol = request.query_params.get("symbol", result.symbol)

    tf_seconds = TIMEFRAME_SECONDS.get(timeframe, 3600)

    try:
        start_dt = datetime.fromisoformat(cursor) + timedelta(seconds=1)
    except (ValueError, TypeError):
        return Response({"error": f"Invalid cursor: {cursor}"}, status=400)

    # Bounded range: fetch `limit` bars worth of time + 1 day buffer
    end_dt = start_dt + timedelta(seconds=tf_seconds * limit + 86400)

    from .tasks import _fetch_ohlcv_sync

    ohlcv = _fetch_ohlcv_sync(
        symbol=symbol,
        timeframe=timeframe,
        limit=1000,
        start_date=start_dt.isoformat(),
        end_date=end_dt.isoformat(),
    )
    # Return only the first `limit` bars (closest to cursor)
    ohlcv = ohlcv[:limit]
    return Response({"ohlcv_data": ohlcv, "count": len(ohlcv)})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def result_trades(request, pk):
    """Paginated trade log for a backtest result.

    Query params:
      page: int (default 1)
      page_size: int (default 50, max 200)
      sort: 'entry_time' | '-entry_time' | 'pnl' | '-pnl' (default 'entry_time')
    """
    if not BacktestResult.objects.filter(pk=pk).exists():
        return Response({"error": "Backtest result not found"}, status=404)

    sort = request.query_params.get("sort", "entry_time")
    page_size = min(int(request.query_params.get("page_size", 50)), 200)

    trades = BacktestTrade.objects.filter(backtest_id=pk).order_by(sort)
    paginator = Paginator(trades, page_size)
    page = paginator.get_page(request.query_params.get("page", 1))

    return Response(
        {
            "count": paginator.count,
            "num_pages": paginator.num_pages,
            "current_page": page.number,
            "results": BacktestTradeSerializer(page, many=True).data,
        }
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def result_review(request, pk):
    """审核回测结果

    Request body:
      approved: bool  — 是否通过审核
      notes: str (optional) — 审核备注
    """
    try:
        result = BacktestResult.objects.get(pk=pk)
    except BacktestResult.DoesNotExist:
        return Response({"error": "Backtest result not found"}, status=404)

    if result.review_status != "pending":
        return Response(
            {"error": f"Result already {result.review_status}"},
            status=400,
        )

    approved = request.data.get("approved")
    notes = request.data.get("notes", "")

    if approved is None:
        return Response({"error": "approved field is required"}, status=400)

    result.review_status = "approved" if approved else "rejected"
    result.review_notes = notes
    result.reviewed_at = timezone.now()
    result.save(update_fields=["review_status", "review_notes", "reviewed_at"])

    return Response(
        {
            "id": str(result.id),
            "review_status": result.review_status,
            "review_notes": result.review_notes,
            "reviewed_at": result.reviewed_at,
        }
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def result_create(request):
    """创建并提交回测任务。

    立即创建占位 BacktestResult 记录（UI 即时可见），
    然后异步提交 Celery 任务运行回测。

    Request body (CreateBacktestPayload):
      strategy_name: str — 策略名称
      symbol: str — 交易对
      timeframe: str — K 线周期
      start_date: str (optional) — 回测开始日期
      end_date: str (optional) — 回测结束日期
      initial_capital: float (optional) — 初始资金
      commission_rate: float (optional) — 手续费率
      parameters: dict (optional) — 策略参数
      exchange: str (optional) — 交易所
    """
    strategy_name = request.data.get("strategy_name", "")
    symbol = request.data.get("symbol", "")
    timeframe = request.data.get("timeframe", "")
    if not strategy_name or not symbol or not timeframe:
        return Response(
            {"error": "strategy_name, symbol, timeframe 为必填项"}, status=400
        )

    # 归一化 symbol：SOLUSDT / sol-usdt / SOL -> SOL/USDT
    from .tasks import normalize_binance_symbol

    symbol = normalize_binance_symbol(symbol)

    from apps.strategy_engine.backtest_mode import (
        create_empty_result,
        _resolve_strategy_id,
    )
    from apps.trading.models import Strategy
    import asyncio

    async def _resolve():
        return await _resolve_strategy_id(strategy_name)

    try:
        strategy_id = asyncio.run(_resolve())
        if not strategy_id:
            return Response({"error": f"无法解析策略: {strategy_name}"}, status=400)
        # 使用数据库中的标准策略名，而非用户提交的（可能不一致的）名称
        canonical_name = Strategy.objects.get(id=strategy_id).name
    except Exception as e:
        return Response({"error": f"策略解析失败: {e}"}, status=400)

    start_date = request.data.get("start_date", "")
    if not start_date:
        start_date = (date.today() - timedelta(days=30)).isoformat()
    end_date = request.data.get("end_date", "")
    if not end_date:
        end_date = date.today().isoformat()

    initial_capital = request.data.get("initial_capital", 10000)
    commission_rate = request.data.get("commission_rate", 0.001)
    slippage_rate = request.data.get("slippage_rate")
    is_perp = bool(request.data.get("is_perp", False))
    include_funding = bool(request.data.get("include_funding", True))
    parameters = request.data.get("parameters")
    exchange = request.data.get("exchange", "binance")
    user_id = str(request.user.id) if request.user.is_authenticated else None

    result_id = create_empty_result(
        strategy_id=strategy_id,
        symbol=symbol,
        timeframe=timeframe,
        start_date=start_date,
        end_date=end_date,
        initial_capital=initial_capital,
        parameters=parameters or {},
        user_id=user_id,
    )
    if not result_id:
        return Response({"error": "创建回测记录失败"}, status=500)

    from .tasks import run_backtest_task

    task = run_backtest_task.apply_async(
        kwargs={
            "strategy_name": canonical_name,
            "symbol": symbol,
            "timeframe": timeframe,
            "initial_capital": initial_capital,
            "commission_rate": commission_rate,
            "parameters": parameters,
            "strategy_id": strategy_id,
            "exchange": exchange,
            "start_date": start_date,
            "end_date": end_date,
            "user_id": user_id,
            "result_id": result_id,
            "slippage_rate": slippage_rate,
            "is_perp": is_perp,
            "include_funding": include_funding,
        }
    )

    return Response(
        {
            "task_id": task.id,
            "result_id": result_id,
            "message": f"回测任务已提交，task_id={task.id}",
        }
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def result_rerun(request, pk):
    """重新运行回测任务。

    基于已有回测结果的配置重新提交 Celery 任务，
    创建新的占位记录并异步运行回测。
    """
    try:
        source = BacktestResult.objects.select_related("strategy").get(pk=pk)
    except BacktestResult.DoesNotExist:
        return Response({"error": "Backtest result not found"}, status=404)

    strategy_name = source.strategy.name if source.strategy else ""
    symbol = source.symbol
    timeframe = source.timeframe
    start_date = source.start_date.isoformat()
    end_date = source.end_date.isoformat()
    initial_capital = float(source.initial_capital)
    parameters = source.parameters or {}
    user_id = str(request.user.id) if request.user.is_authenticated else None

    from apps.strategy_engine.backtest_mode import create_empty_result

    result_id = create_empty_result(
        strategy_id=str(source.strategy_id),
        symbol=symbol,
        timeframe=timeframe,
        start_date=start_date,
        end_date=end_date,
        initial_capital=initial_capital,
        parameters=parameters,
        user_id=user_id,
    )
    if not result_id:
        return Response({"error": "创建回测记录失败"}, status=500)

    from .tasks import run_backtest_task

    task = run_backtest_task.apply_async(
        kwargs={
            "strategy_name": strategy_name,
            "symbol": symbol,
            "timeframe": timeframe,
            "initial_capital": initial_capital,
            "commission_rate": 0.001,
            "parameters": parameters,
            "strategy_id": str(source.strategy_id),
            "exchange": "binance",
            "start_date": start_date,
            "end_date": end_date,
            "user_id": user_id,
            "result_id": result_id,
        }
    )

    return Response(
        {
            "task_id": task.id,
            "result_id": result_id,
            "message": f"回测任务已重新提交，task_id={task.id}",
        }
    )


# ============================================================
# Grid Search API
# ============================================================


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def grid_search_create(request):
    """提交网格搜索任务。"""
    from .models import GridSearchJob
    from .tasks import run_grid_search_task
    from apps.trading.models import Strategy

    strategy_id = request.data.get("strategy_id")
    symbol = request.data.get("symbol")
    timeframe = request.data.get("timeframe")
    grid_search = request.data.get("grid_search")

    if not all([strategy_id, symbol, timeframe]):
        return Response(
            {"error": "strategy_id, symbol, timeframe 为必填项"}, status=400
        )
    # 归一化 symbol：SOLUSDT / sol-usdt / SOL -> SOL/USDT
    from .tasks import normalize_binance_symbol

    symbol = normalize_binance_symbol(symbol)
    if not grid_search or not grid_search.get("parameters"):
        return Response(
            {"error": "grid_search.parameters 为必填项"}, status=400
        )

    try:
        strategy = Strategy.objects.get(id=strategy_id)
    except Strategy.DoesNotExist:
        return Response({"error": f"Strategy not found: {strategy_id}"}, status=404)

    user_id = str(request.user.id)
    if GridSearchJob.objects.filter(user_id=user_id, status="running").exists():
        return Response(
            {"error": "您有一个正在运行的网格搜索任务，请先等待完成或取消"},
            status=429,
        )

    start_date = request.data.get("start_date", "")
    if not start_date:
        start_date = (date.today() - timedelta(days=30)).isoformat()
    end_date = request.data.get("end_date", "")
    if not end_date:
        end_date = date.today().isoformat()

    initial_capital = request.data.get("initial_capital", 10000)

    # Validate ranges
    for param_name, range_def in grid_search["parameters"].items():
        if isinstance(range_def, dict):
            min_val = range_def.get("min")
            max_val = range_def.get("max")
            step = range_def.get("step")
            if min_val is not None and max_val is not None and step is not None:
                if min_val >= max_val:
                    return Response(
                        {"error": f"参数 {param_name}: min 必须 < max"}, status=400
                    )
                if step <= 0:
                    return Response(
                        {"error": f"参数 {param_name}: step 必须 > 0"}, status=400
                    )

    from apps.strategy_engine.grid_search import generate_combinations

    try:
        preview_combos = generate_combinations(
            {"parameters": grid_search["parameters"], "max_combinations": 101}
        )
        total = len(preview_combos)
        if total > 100:
            return Response(
                {"error": f"参数组合数 {total} 超过上限 100，请缩小搜索范围"},
                status=400,
            )
    except ValueError as e:
        return Response({"error": str(e)}, status=400)

    job = GridSearchJob.objects.create(
        strategy=strategy,
        symbol=symbol,
        timeframe=timeframe,
        start_date=start_date,
        end_date=end_date,
        initial_capital=initial_capital,
        search_config={
            "parameters": grid_search["parameters"],
            "sort_by": grid_search.get("sort_by", "sharpe_ratio"),
            "max_combinations": grid_search.get("max_combinations", 100),
        },
        sort_by=grid_search.get("sort_by", "sharpe_ratio"),
        source="api",
        user=request.user,
    )

    task = run_grid_search_task.apply_async(
        kwargs={"job_id": str(job.id), "user_id": user_id},
        queue="grid_search",
    )
    job.celery_task_id = task.id
    job.save(update_fields=["celery_task_id"])

    return Response(
        {
            "grid_search_id": str(job.id),
            "task_id": task.id,
            "total_combinations": total,
            "message": f"网格搜索任务已提交，共 {total} 个参数组合",
        }
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def grid_search_detail(request, pk):
    """查询网格搜索任务状态"""
    from .models import GridSearchJob

    try:
        job = GridSearchJob.objects.select_related("strategy").get(pk=pk)
    except GridSearchJob.DoesNotExist:
        return Response({"error": "Grid search job not found"}, status=404)

    if not request.user.is_superuser and str(job.user_id) != str(request.user.id):
        return Response({"error": "无权访问此任务"}, status=403)

    from .serializers import GridSearchJobSerializer

    return Response(GridSearchJobSerializer(job).data)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def grid_search_results(request, pk):
    """查询网格搜索任务的所有子回测结果"""
    from .models import GridSearchJob

    try:
        job = GridSearchJob.objects.get(pk=pk)
    except GridSearchJob.DoesNotExist:
        return Response({"error": "Grid search job not found"}, status=404)

    if not request.user.is_superuser and str(job.user_id) != str(request.user.id):
        return Response({"error": "无权访问此任务"}, status=403)

    # Whitelist allowed sort fields
    ALLOWED_SORT_FIELDS = {"sharpe_ratio", "total_return_pct", "win_rate", "max_drawdown", "total_trades", "-sharpe_ratio", "-total_return_pct", "-win_rate", "-max_drawdown", "-total_trades"}
    sort = request.query_params.get("sort", job.sort_by or "-sharpe_ratio")
    if sort not in ALLOWED_SORT_FIELDS:
        sort = f"-{job.sort_by}" if job.sort_by and not job.sort_by.startswith("-") else f"-{job.sort_by or 'sharpe_ratio'}"
    page_size = min(int(request.query_params.get("page_size", 20)), 200)

    results = BacktestResult.objects.filter(
        grid_search_id=job.id
    ).defer(
        "ohlcv_data", "equity_curve", "drawdown_curve", "indicator_data",
    ).order_by(sort)

    paginator = Paginator(results, page_size)
    page_obj = paginator.get_page(request.query_params.get("page", 1))

    from .serializers import BacktestResultSerializer

    return Response(
        {
            "job_id": str(job.id),
            "status": job.status,
            "total_combinations": job.total_combinations,
            "completed_combinations": job.completed_combinations,
            "count": paginator.count,
            "num_pages": paginator.num_pages,
            "current_page": page_obj.number,
            "results": BacktestResultSerializer(page_obj, many=True).data,
        }
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def grid_search_cancel(request, pk):
    """取消网格搜索任务"""
    from celery_app import app as celery_app
    from .models import GridSearchJob

    try:
        job = GridSearchJob.objects.get(pk=pk)
    except GridSearchJob.DoesNotExist:
        return Response({"error": "Grid search job not found"}, status=404)

    if not request.user.is_superuser and str(job.user_id) != str(request.user.id):
        return Response({"error": "无权操作此任务"}, status=403)

    if job.status in ("completed", "cancelled", "failed"):
        return Response({"error": f"任务已结束（{job.status}）"}, status=400)

    job.status = "cancelled"
    job.save(update_fields=["status"])

    if job.celery_task_id:
        try:
            celery_app.control.revoke(job.celery_task_id, terminate=True, signal="SIGTERM")
        except Exception:
            logger.warning("Failed to revoke celery task %s", job.celery_task_id)

    return Response({"message": "网格搜索任务已取消"})
