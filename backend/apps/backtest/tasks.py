"""
Celery tasks for backtest execution.

异步回测任务，供 BacktestAgent 和 API 调用。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from celery_app import app

from apps.regime.tasks import dispatch_slice

# 交易所访问共享工具（ccxt 构造 / 分页拉取 / 符号归一化）已抽到
# apps.datasource.exchange，回测与 datasource 同步任务共用。
# normalize_binance_symbol 在此 re-export，保持既有 import 路径可用：
# apps/agent/tools/backtest.py、apps/backtest/views.py、test_grid_search.py。
from apps.datasource.exchange import (  # noqa: F401
    build_exchange,
    fetch_ohlcv_paged,
    normalize_binance_symbol,
)

logger = logging.getLogger(__name__)


@app.task(bind=True, max_retries=2, acks_late=True, track_started=True)
def run_backtest_task(
    self,
    strategy_name: str,
    symbol: str,
    timeframe: str,
    initial_capital: float = 10000,
    commission_rate: float = 0.001,
    parameters: dict | None = None,
    strategy_id: str | None = None,
    exchange: str = "binance",
    start_date: str = "",
    end_date: str = "",
    user_id: str | None = None,
    result_id: str | None = None,
    benchmark: str = "",
    slippage_rate: float | None = None,
    is_perp: bool = False,
    include_funding: bool = True,
) -> dict:
    """
    异步执行策略回测。

    Args:
        strategy_name: 策略名称（StrategyRegistry 注册名）
        symbol: 交易对
        timeframe: K 线周期
        initial_capital: 初始资金
        commission_rate: 手续费率
        parameters: 策略参数
        strategy_id: Strategy 模型 UUID（用于关联回测结果）
        exchange: 交易所
        start_date: 回测开始日期（ISO 格式，如 2024-01-01），空字符串表示不限
        end_date: 回测结束日期（ISO 格式，如 2024-12-31），空字符串表示不限
        user_id: 发起回测的用户 ID
        result_id: BacktestResult UUID（如果已提前创建占位记录）
        slippage_rate: 单边滑点率（None → 引擎默认 0.0005）
        is_perp: 是否永续合约（影响资金费率结算）
        include_funding: 永续回测是否计入资金费率成本

    Returns:
        回测统计结果
    """
    from django.db import close_old_connections

    from apps.strategy_engine.backtest_mode import _resolve_strategy_name
    from apps.strategy_engine.registry import StrategyRegistry
    from apps.strategy_engine.runner import StrategyRunner
    from apps.agent.task_tracker import TaskTracker, tracker_context

    tracker = TaskTracker(
        task_id=self.request.id, user_id=str(user_id or ""), task_type="backtest"
    )
    token = tracker_context.set(tracker)
    tracker.start(
        f"开始回测：{strategy_name} {symbol} {timeframe}",
    )

    # ── Persist celery_task_id → BacktestResult mapping so get_task_result
    #     can find the result even after Redis/Celery backends expire ──
    if result_id:
        try:
            close_old_connections()
            from apps.backtest.models import BacktestResult as BR

            BR.objects.filter(id=result_id).update(celery_task_id=self.request.id)
        except Exception:
            logger.warning(
                "[BacktestTask] failed to set celery_task_id on result %s",
                result_id,
                exc_info=True,
            )

    try:
        logger.info(
            f"[BacktestTask] running: strategy={strategy_name} symbol={symbol} "
            f"tf={timeframe} exchange={exchange}"
        )

        if not StrategyRegistry._strategy_path:
            StrategyRegistry.set_strategy_path("/root/.tradelogx/strategies")

        StrategyRegistry.discover()
        strategy_name = _resolve_strategy_name(strategy_name)

        if not result_id:
            try:
                from apps.strategy_engine.backtest_mode import create_empty_result

                # Use sync ORM directly — _aio.run(sync_to_async()) fails in Celery
                # because Django's async context isn't initialized
                close_old_connections()
                from apps.trading.models import Strategy

                canonical = strategy_name  # already resolved above
                _sid_obj, _ = Strategy.objects.get_or_create(
                    name=canonical,
                    defaults={
                        "code_path": f"strategies/{canonical}.py",
                        "is_active": False,
                    },
                )
                _sid = str(_sid_obj.id)
                strategy_id = _sid
                if _sid:
                    _s = start_date or (date.today() - timedelta(days=30)).isoformat()
                    _e = end_date or date.today().isoformat()
                    result_id = create_empty_result(
                        strategy_id=_sid,
                        symbol=symbol,
                        timeframe=timeframe,
                        start_date=_s,
                        end_date=_e,
                        initial_capital=initial_capital,
                        parameters=parameters or {},
                        user_id=user_id,
                    )
                    logger.info(
                        "[BacktestTask] created placeholder result_id=%s", result_id
                    )
            except Exception:
                logger.warning(
                    "[BacktestTask] failed to create placeholder result", exc_info=True
                )

        self.update_state(
            state="STARTED", meta={"step": "fetching_ohlcv", "symbol": symbol}
        )
        tracker.milestone("正在获取历史K线数据...", progress=0.1)
        ohlcv_data, source_stats = _load_ohlcv_for_backtest(
            symbol,
            timeframe,
            start_date=start_date,
            end_date=end_date,
            exchange=exchange,
        )
        if not ohlcv_data:
            tracker.fail(f"未能获取 {symbol} {timeframe} 的历史K线数据")
            raise ValueError(f"未能获取 {symbol} {timeframe} 的历史K线数据")

        logger.info(
            f"[BacktestTask] OHLCV data fetched: {len(ohlcv_data)} bars "
            f"(from_db={source_stats['from_db']}, from_api={source_stats['from_api']}, "
            f"gaps={len(source_stats['gaps'])})"
        )
        tracker.milestone(
            f"已获取 {len(ohlcv_data)} 条K线数据，正在执行回测...", progress=0.3
        )

        self.update_state(
            state="STARTED",
            meta={"step": "running_backtest", "bars": len(ohlcv_data)},
        )

        # DB connections may have gone stale during _fetch_ohlcv_sync
        close_old_connections()

        runner = StrategyRunner()

        if is_perp and include_funding:
            _warn_funding_coverage(symbol, ohlcv_data)

        funding_rates = (
            load_funding_rates(symbol, ohlcv_data)
            if (is_perp and include_funding)
            else {}
        )

        loop = asyncio.new_event_loop()
        try:
            stats = loop.run_until_complete(
                runner.run_backtest(
                    strategy_name=strategy_name,
                    symbol=symbol,
                    timeframe=timeframe,
                    ohlcv_data=ohlcv_data,
                    initial_capital=Decimal(str(initial_capital)),
                    parameters=parameters,
                    strategy_id=strategy_id,
                    commission_rate=Decimal(str(commission_rate)),
                    result_id=result_id,
                    benchmark=benchmark,
                    slippage_rate=(
                        Decimal(str(slippage_rate))
                        if slippage_rate is not None
                        else None
                    ),
                    is_perp=is_perp,
                    funding_rates=funding_rates,
                )
            )
        finally:
            _close_async_resources(loop)
            loop.close()

        # 数据来源统计落库（spec §7.3）：结果行由 runner 写进 BacktestResult，
        # 而 data_source 是本次改造新增的字段，runner 不感知，故在此补写。
        persisted_id = stats.get("result_id") or result_id
        if persisted_id:
            from apps.backtest.models import BacktestResult

            close_old_connections()
            BacktestResult.objects.filter(id=persisted_id).update(
                data_source=source_stats
            )

        total_trades = stats.get("total_trades", 0)
        total_return = stats.get("total_return_pct", 0)
        sharpe = stats.get("sharpe_ratio", 0) or 0
        win_rate = stats.get("win_rate", 0) or 0

        logger.info(
            f"[BacktestTask] backtest complete: result_id={stats.get('result_id')} "
            f"trades={total_trades}"
        )
        tracker.complete(
            f"{strategy_name} {symbol} {timeframe} 回测完成\n"
            f"交易 {total_trades} 笔 | "
            f"收益率 {total_return:.2f}% | "
            f"夏普 {sharpe:.2f} | "
            f"胜率 {win_rate:.1%}"
        )
        # 行情阶段切片：结果已落库，独立投递一个轻量任务去算（见 apps/regime/slicing.py
        # 的模块 docstring 说明为什么不放在这里同步算）。投递失败不抛也不会走到这个
        # except：一次成功的回测不该被附加产物拖成失败。
        dispatch_slice([stats.get("result_id")])
        return stats
    except Exception as e:
        tracker.fail(f"回测失败: {strategy_name} {symbol} {timeframe}: {str(e)}")
        raise
    finally:
        tracker.stop()
        tracker_context.reset(token)


# _QUOTE_SUFFIXES / normalize_binance_symbol 已移至 apps.datasource.exchange，
# 并在文件顶部 re-export（既有调用方仍可从 apps.backtest.tasks import）。


def _parse_date_utc(value: str) -> datetime | None:
    """``YYYY-MM-DD``（或 ISO 时间串）→ aware-UTC datetime；失败返回 None。

    ``datetime.fromisoformat("2026-01-01")`` 返回 **naive** datetime，其
    ``.timestamp()`` 按**本机时区**解释：本机是 CST(+8) 时整体偏移 8 小时，与
    ``KlineRecord`` 存的 UTC 口径不一致（缺口检测错位、补拉窗口偏移）。故此处
    显式补 UTC——与仓库既有的 ``_parse_ts`` 同口径。
    """
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _date_to_epoch_ms(value: str, field: str) -> int | None:
    """日期串 → epoch 毫秒（按 UTC 解释）；非法输入记 warning 并返回 None。"""
    dt = _parse_date_utc(value)
    if dt is None:
        logger.warning(f"[BacktestTask] invalid {field}={value}, ignoring")
        return None
    return int(dt.timestamp() * 1000)


def _fetch_ohlcv_sync(
    symbol: str,
    timeframe: str,
    exchange: str = "binance",
    limit: int = 1000,
    start_date: str = "",
    end_date: str = "",
    since_ms: int | None = None,
    end_ms: int | None = None,
) -> list[dict]:
    """同步获取历史 OHLCV 数据（供 Celery task 使用）

    交易所构造与分页拉取走 ``apps.datasource.exchange`` 的共享实现（代理注入、
    rate limit、分页边界统一一处）；本函数只负责区间解析，以及把共享层返回的
    aware-datetime 行转回既有调用方依赖的 ISO 字符串形态——**输出结构与值类型
    保持不变**。

    ``since_ms`` / ``end_ms`` 是显式 epoch 毫秒，**优先于** ``start_date`` /
    ``end_date``：缺口补拉需要亚日精度，date 字符串表达不了。
    """
    symbol_normalized = normalize_binance_symbol(symbol)

    since = since_ms
    if since is None and start_date:
        since = _date_to_epoch_ms(start_date, "start_date")
    end = end_ms
    if end is None and end_date:
        end = _date_to_epoch_ms(end_date, "end_date")

    async def _fetch():
        try:
            ex = build_exchange(exchange)
        except ValueError:
            logger.error(f"[BacktestTask] unsupported exchange: {exchange}")
            return []

        try:
            rows = await fetch_ohlcv_paged(
                ex, symbol_normalized, timeframe, since, end, limit
            )
        finally:
            await ex.close()

        if not rows:
            logger.warning(f"[BacktestTask] no OHLCV data for {symbol_normalized}")
            return []

        return [
            {
                "timestamp": row["timestamp"].isoformat(),
                "open": row["open"],
                "high": row["high"],
                "low": row["low"],
                "close": row["close"],
                "volume": row["volume"],
            }
            for row in rows
        ]

    try:
        return asyncio.run(_fetch())
    except Exception as e:
        logger.error(f"[BacktestTask] OHLCV fetch failed: {e}", exc_info=True)
        return []


def _db_row_to_ohlcv(row: dict) -> dict:
    """单条 ``KlineRecord`` 查询结果 → 与 ``_fetch_ohlcv_sync`` 同构的 dict。"""
    return {
        "timestamp": row["timestamp"].isoformat(),
        "open": row["open"],
        "high": row["high"],
        "low": row["low"],
        "close": row["close"],
        "volume": row["volume"],
    }


def _ohlcv_row_to_kline(row: dict, exchange: str, symbol: str, timeframe: str) -> dict:
    """同构 OHLCV dict → ``upsert_klines`` 需要的行（timestamp 转回 datetime）。"""
    return {
        "exchange": exchange,
        "symbol": symbol,
        "timeframe": timeframe,
        "timestamp": _parse_ts(row["timestamp"]),
        "open": row["open"],
        "high": row["high"],
        "low": row["low"],
        "close": row["close"],
        "volume": row["volume"],
    }


def _detect_kline_gaps(
    db_rows: list[dict], start_dt: datetime, end_dt: datetime, step: timedelta
) -> list[tuple[datetime, datetime]]:
    """库内 bar 相对 ``[start_dt, end_dt]`` 的缺口（spec §7.1 细则）。

    头部：库空（整段都是缺口），或首行 ``> start_dt + step``；
    中间：相邻两行差 ``> step``；
    尾部：末行 ``< end_dt - step``。
    """
    if not db_rows:
        return [(start_dt, end_dt)]

    gaps: list[tuple[datetime, datetime]] = []
    first = db_rows[0]["timestamp"]
    last = db_rows[-1]["timestamp"]
    if first > start_dt + step:
        gaps.append((start_dt, first - step))
    for prev, cur in zip(db_rows, db_rows[1:]):
        if cur["timestamp"] - prev["timestamp"] > step:
            gaps.append((prev["timestamp"] + step, cur["timestamp"] - step))
    if last < end_dt - step:
        gaps.append((last + step, end_dt))
    return gaps


def _load_ohlcv_for_backtest(
    symbol: str,
    timeframe: str,
    start_date: str = "",
    end_date: str = "",
    exchange: str = "binance",
) -> tuple[list[dict], dict]:
    """回测 OHLCV 获取：**库优先，缺口回退 API**（spec §7.1）。

    有效 bar 集合 = ``timestamp + step <= end_dt``（未收盘 bar 不进回测）。库查询
    与补拉结果都按这一条过滤，因此「库里已有的 bar」与「API 会返回的 bar」口径
    一致——补一次即收敛，不会每轮都报同一个尾部缺口。

    无缺口时**零 API 调用**；有缺口时逐 gap 调 ``_fetch_ohlcv_sync`` 补拉并
    ``upsert_klines`` 回写：「跑一次回测，库就补全一段」。

    ``start_date`` / ``end_date`` 为空时无法界定区间与缺口，退回全量 API（保持
    既有行为）。

    Returns:
        ``(ohlcv_data, source_stats)``。``ohlcv_data`` 与 ``_fetch_ohlcv_sync``
        同构（引擎无需改动）；``source_stats = {"from_db": n, "from_api": m,
        "gaps": [[iso_start, iso_end], ...]}``。
    """
    from apps.datasource.exchange import timeframe_to_seconds
    from apps.datasource.models import KlineRecord
    from apps.datasource.services import upsert_klines

    start_dt = _parse_date_utc(start_date) if start_date else None
    end_dt = _parse_date_utc(end_date) if end_date else None
    if start_dt is None or end_dt is None:
        rows = _fetch_ohlcv_sync(
            symbol, timeframe, exchange, start_date=start_date, end_date=end_date
        )
        return rows, {"from_db": 0, "from_api": len(rows), "gaps": []}

    step = timedelta(seconds=timeframe_to_seconds(timeframe))

    # ① 查库：区间 + 排除未收盘 bar
    queried = list(
        KlineRecord.objects.filter(
            exchange=exchange,
            symbol=symbol,
            timeframe=timeframe,
            timestamp__gte=start_dt,
            timestamp__lte=end_dt,
        )
        .order_by("timestamp")
        .values("timestamp", "open", "high", "low", "close", "volume")
    )
    db_rows = [r for r in queried if r["timestamp"] + step <= end_dt]

    # ② 缺口检测
    gaps = _detect_kline_gaps(db_rows, start_dt, end_dt, step)
    if not gaps:
        logger.info(
            "[BacktestTask] %s %s: %d bars all from DB, 0 API calls",
            symbol,
            timeframe,
            len(db_rows),
        )
        return [_db_row_to_ohlcv(r) for r in db_rows], {
            "from_db": len(db_rows),
            "from_api": 0,
            "gaps": [],
        }

    # ③ 逐 gap 补拉 + 回写
    fetched: list[dict] = []
    for gap_start, gap_end in gaps:
        rows = _fetch_ohlcv_sync(
            symbol,
            timeframe,
            exchange,
            since_ms=int(gap_start.timestamp() * 1000),
            end_ms=int(gap_end.timestamp() * 1000),
        )
        if not rows:
            continue
        fetched.extend(rows)
        upsert_klines(
            [_ohlcv_row_to_kline(r, exchange, symbol, timeframe) for r in rows]
        )

    # ④ 合并：按 timestamp 去重，**同 timestamp 以库为准**（库值已确认）
    merged: dict[int, dict] = {}
    for row in db_rows:
        merged[int(row["timestamp"].timestamp() * 1000)] = _db_row_to_ohlcv(row)
    from_db = len(merged)

    from_api = 0
    for row in fetched:
        ts = _parse_ts(row["timestamp"])
        if ts is None or not (start_dt <= ts <= end_dt) or ts + step > end_dt:
            continue
        key = int(ts.timestamp() * 1000)
        if key in merged:
            continue
        merged[key] = row
        from_api += 1

    logger.info(
        "[BacktestTask] %s %s: from_db=%d from_api=%d gaps=%d",
        symbol,
        timeframe,
        from_db,
        from_api,
        len(gaps),
    )
    return [merged[key] for key in sorted(merged)], {
        "from_db": from_db,
        "from_api": from_api,
        "gaps": [[g[0].isoformat(), g[1].isoformat()] for g in gaps],
    }


def _warn_funding_coverage(symbol: str, ohlcv_data: list[dict]) -> None:
    """回测前检查资金费率覆盖度，缺失时告警（spec §7.2）。

    **一期只告警，不改变回测失败行为**：成本模型 P0 已定「缺费率按 0 计 +
    warning」是可接受的保守策略，避免阻断策略工厂流水线。

    ``expected`` 按「每 8h 一个结算点」估算（Binance 永续结算周期），是下界近似
    ——只用于判断「有没有缺」，不作为精确覆盖度指标。
    """
    from apps.backtest.models import FundingRateRecord

    start = _parse_ts(ohlcv_data[0].get("timestamp", "")) if ohlcv_data else None
    end = _parse_ts(ohlcv_data[-1].get("timestamp", "")) if ohlcv_data else None
    if start is None or end is None:
        return

    expected = int((end - start) / timedelta(hours=8))
    actual = FundingRateRecord.objects.filter(
        symbol=symbol, funding_time__gte=start, funding_time__lte=end
    ).count()
    missing = expected - actual
    if missing > 0:
        logger.warning(
            "[BacktestTask] %s funding rate coverage gap: %d missing in range",
            symbol,
            missing,
        )


def _close_async_resources(loop: asyncio.AbstractEventLoop) -> None:
    """显式关闭 Redis 连接等异步资源，防止 __del__ 在 loop 关闭后报错。"""
    try:
        from apps.memory.redis_client import RedisPool

        client = RedisPool.get_client()

        async def _aclose():
            await client.aclose()

        loop.run_until_complete(_aclose())
        RedisPool.close_client()
    except Exception:
        pass


@app.task(
    bind=True,
    max_retries=1,
    acks_late=True,
    track_started=True,
    soft_time_limit=3600,
    time_limit=7200,
    queue="grid_search",
)
def run_grid_search_task(self, job_id: str, user_id: str | None = None) -> dict:
    """
    执行网格搜索任务，批量回测所有参数组合。

    Args:
        job_id: GridSearchJob UUID
        user_id: 发起任务的用户 ID（定时任务时可能为 None）

    Returns:
        {"job_id": str, "total": int, "completed": int, "best_result_id": str}
    """
    from celery.exceptions import SoftTimeLimitExceeded
    from django.utils import timezone
    from django.db import close_old_connections

    from apps.backtest.models import BacktestResult, GridSearchJob
    from apps.agent.task_tracker import TaskTracker, tracker_context
    from apps.strategy_engine.grid_search import generate_combinations
    from apps.strategy_engine.runner import StrategyRunner

    try:
        job = GridSearchJob.objects.select_related("strategy").get(id=job_id)
    except GridSearchJob.DoesNotExist:
        logger.error(f"[GridSearchTask] job {job_id} not found")
        return {
            "status": "FAILURE",
            "error_type": "NotFoundError",
            "error_message": "job not found",
            "error": "job not found",
        }

    # 归一化 symbol：修正历史任务里的拼接格式（如 SOLUSDT -> SOL/USDT）
    normalized_symbol = normalize_binance_symbol(job.symbol)
    if normalized_symbol != job.symbol:
        logger.warning(
            f"[GridSearchTask] normalized symbol {job.symbol!r} -> {normalized_symbol!r}"
        )
        job.symbol = normalized_symbol
        job.save(update_fields=["symbol"])

    # Fall back to job's user_id if not provided (e.g. called via Agent tool)
    if not user_id and job.user_id:
        user_id = str(job.user_id)

    if user_id and str(job.user_id) != user_id:
        logger.error(
            f"[GridSearchTask] user mismatch: job={job.user_id} requested={user_id}"
        )
        return {
            "status": "FAILURE",
            "error_type": "PermissionError",
            "error_message": "user mismatch",
            "error": "user mismatch",
        }

    tracker = TaskTracker(
        task_id=self.request.id, user_id=str(user_id or ""), task_type="grid_search"
    )
    token = tracker_context.set(tracker)
    tracker.start(f"开始网格搜索：{job.symbol} {job.timeframe}")

    try:
        job.status = "running"
        job.celery_task_id = self.request.id
        job.save(update_fields=["status", "celery_task_id"])

        config = job.search_config
        strategy_name = job.strategy.name
        combinations = generate_combinations(config)
        job.total_combinations = len(combinations)
        job.save(update_fields=["total_combinations"])

        tracker.milestone(f"已生成 {len(combinations)} 个参数组合", progress=0.05)

        tracker.milestone("正在获取历史K线数据...", progress=0.1)
        ohlcv_data = _fetch_ohlcv_sync(
            job.symbol,
            job.timeframe,
            exchange="binance",
            start_date=job.start_date.isoformat()
            if isinstance(job.start_date, date)
            else str(job.start_date),
            end_date=job.end_date.isoformat()
            if isinstance(job.end_date, date)
            else str(job.end_date),
        )
        if not ohlcv_data:
            tracker.fail(f"未能获取 {job.symbol} {job.timeframe} 的历史K线数据")
            job.status = "failed"
            job.error_log = [{"error": "failed to fetch OHLCV data"}]
            job.save(update_fields=["status", "error_log"])
            return {
                "status": "FAILURE",
                "error_type": "DataError",
                "error_message": "OHLCV fetch failed",
                "error": "OHLCV fetch failed",
            }

        tracker.milestone(f"已获取 {len(ohlcv_data)} 条K线数据", progress=0.2)

        runner = StrategyRunner()
        results = []
        loop = asyncio.new_event_loop()
        start_time = timezone.now().timestamp()

        try:
            for idx, params in enumerate(combinations):
                # DB 连接在长时间的 OHLCV 抓取 / 上一个组合回测期间可能被
                # pgbouncer 关闭（CONN_MAX_AGE=30s）。每个组合开始前刷新连接，
                # 否则 save_backtest_result 会因 "connection already closed" 失败，
                # 导致整个网格搜索 0 条结果落库（前端因此看不到任何子回测）。
                close_old_connections()
                try:
                    job.refresh_from_db()
                    if job.status == "cancelled":
                        tracker.milestone("任务已被取消", progress=0.0)
                        return {
                            "status": "CANCELLED",
                            "cancelled": True,
                            "completed": idx,
                            "message": f"任务已被取消，已完成 {idx} 个组合",
                        }
                except Exception:
                    pass

                combo_label = ", ".join(f"{k}={v}" for k, v in params.items())
                progress = 0.2 + 0.7 * ((idx + 1) / max(len(combinations), 1))
                tracker.milestone(
                    f"执行组合 [{idx + 1}/{len(combinations)}]: {combo_label}",
                    progress=progress,
                )

                try:
                    stats = loop.run_until_complete(
                        runner.run_backtest(
                            strategy_name=strategy_name,
                            symbol=job.symbol,
                            timeframe=job.timeframe,
                            ohlcv_data=ohlcv_data,
                            initial_capital=Decimal(str(job.initial_capital)),
                            parameters=params,
                            strategy_id=str(job.strategy_id),
                            commission_rate=Decimal(str(job.commission_rate)),
                            benchmark="",
                            user_id=str(job.user_id) if job.user_id else None,
                        )
                    )
                    # run_backtest 可能耗时较长，期间 DB 连接可能被 pgbouncer 关闭，
                    # 需要在 ORM 操作前刷新连接，避免 "connection already closed"
                    close_old_connections()
                    result_id = stats.get("result_id")
                    if result_id:
                        BacktestResult.objects.filter(id=result_id).update(
                            grid_search_id=job.id, is_grid_search=True
                        )
                        results.append(
                            {"result_id": result_id, "params": params, **stats}
                        )
                except SoftTimeLimitExceeded:
                    raise
                except Exception as e:
                    close_old_connections()
                    job.refresh_from_db()
                    job.error_log = job.error_log + [
                        {"combination_index": idx, "params": params, "error": str(e)}
                    ]
                    job.save(update_fields=["error_log"])
                    logger.warning(f"[GridSearchTask] combination {idx} failed: {e}")

                job.completed_combinations = idx + 1
                job.save(update_fields=["completed_combinations"])
        finally:
            _close_async_resources(loop)
            loop.close()

        # 排序并标记最优结果
        sort_key = job.sort_by or "sharpe_ratio"
        if results:
            sorted_results = sorted(
                results,
                key=lambda r: r.get(sort_key, 0) or 0,
                reverse=True,
            )
            best = sorted_results[0]
            best_result_id = best.get("result_id")
            if best_result_id:
                try:
                    best_result_obj = BacktestResult.objects.get(id=best_result_id)
                    job.best_result = best_result_obj
                except BacktestResult.DoesNotExist:
                    pass

        elapsed = int(timezone.now().timestamp() - start_time)
        best_val = results[0].get(sort_key) if results else "N/A"
        job.status = "completed"
        job.save(update_fields=["status", "completed_combinations", "best_result"])

        # 切片：**循环结束后一次性投整批**，不在循环里逐组合投——那个循环带 7200 秒硬
        # 超时，把切片塞进去等于让「切片有多慢」决定「网格搜索跑不跑得完」。超时/取消
        # 的路径不投（那些结果由 recompute_regime_slices 兜底，见命令的 docstring）。
        dispatch_slice([r.get("result_id") for r in results])

        tracker.complete(
            f"网格搜索完成：{len(results)}/{job.total_combinations}，"
            f"耗时 {elapsed}s，最优 {sort_key}={best_val}"
        )

        return {
            "job_id": str(job.id),
            "total_combinations": job.total_combinations,
            "completed_combinations": job.completed_combinations,
            "best_result_id": str(job.best_result_id) if job.best_result else None,
        }

    except SoftTimeLimitExceeded:
        job.status = "failed"
        job.error_log = job.error_log + [{"error": "soft time limit exceeded"}]
        job.save(update_fields=["status", "error_log"])
        tracker.fail("网格搜索超时")
        return {
            "status": "FAILURE",
            "error_type": "TimeoutError",
            "error_message": "time limit exceeded",
            "error": "time limit exceeded",
        }
    except Exception as e:
        job.status = "failed"
        job.error_log = job.error_log + [{"error": str(e)}]
        job.save(update_fields=["status", "error_log"])
        tracker.fail(f"网格搜索失败: {e}")
        raise
    finally:
        tracker.stop()
        tracker_context.reset(token)


# 资金费率同步标的集合已移到 settings.FUNDING_SYNC_SYMBOLS（与 K 线同步对齐，
# 单一来源，见 spec §8/§9）。


def load_funding_rates(symbol: str, ohlcv_data: list[dict]) -> dict[str, float]:
    """把回测区间的资金费率记录装成引擎要的 {ISO 时间: rate} 字典。

    K 线时间戳为 UTC 感知 ISO（见 _fetch_ohlcv_sync），资金费率同样按 UTC 存，
    因此键可直接对齐。无数据时返回空字典（引擎会按缺失处理并记 warning）。
    """
    from apps.backtest.models import FundingRateRecord

    if not ohlcv_data:
        return {}

    start = _parse_ts(ohlcv_data[0].get("timestamp", ""))
    end = _parse_ts(ohlcv_data[-1].get("timestamp", ""))
    qs = FundingRateRecord.objects.filter(symbol=symbol)
    if start:
        qs = qs.filter(funding_time__gte=start)
    if end:
        qs = qs.filter(funding_time__lte=end)

    return {
        rec.funding_time.astimezone(timezone.utc).isoformat(): rec.rate for rec in qs
    }


def _parse_ts(value) -> datetime | None:
    """解析 K 线时间戳为 UTC 感知 datetime；失败返回 None。"""
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


@app.task(bind=True, max_retries=2, acks_late=True)
def sync_funding_rates(self, symbols: list[str] | None = None) -> dict:
    """增量同步永续资金费率历史到 FundingRateRecord。

    - 每个 symbol 从已有最新 funding_time 起拉取（无记录则从头）；
    - (symbol, funding_time) 唯一约束幂等去重；
    - Beat 每 8h 调度一次（结算点后）。
    """
    import asyncio

    from django.conf import settings
    from django.db import close_old_connections

    from apps.backtest.models import FundingRateRecord

    # 标的集合单一来源在 settings（与 K 线增量同步对齐）
    symbols = symbols or list(settings.FUNDING_SYNC_SYMBOLS)
    close_old_connections()

    # 1. 同步段：算出每个 symbol 的增量起点（DB 查询必须在 sync 上下文）
    plans: list[tuple[str, int | None]] = []
    for symbol in symbols:
        latest = (
            FundingRateRecord.objects.filter(symbol=symbol)
            .order_by("-funding_time")
            .values_list("funding_time", flat=True)
            .first()
        )
        plans.append((symbol, int(latest.timestamp() * 1000) + 1 if latest else None))

    # 2. 网络段：异步拉取（不含 DB 访问）
    fetched_by_symbol = asyncio.run(_fetch_all_symbols(plans))

    # 3. 同步段：幂等落库
    total_new = 0
    for symbol, since in plans:
        rows = fetched_by_symbol.get(symbol, [])
        created = 0
        for row in rows:
            _, created_i = FundingRateRecord.objects.get_or_create(
                symbol=symbol,
                funding_time=row["dt"],
                defaults={"rate": row["rate"]},
            )
            created += created_i
        total_new += created
        logger.info(
            "[FundingSync] %s: fetched=%d new=%d (since=%s)",
            symbol,
            len(rows),
            created,
            since,
        )

    close_old_connections()
    return {"symbols": symbols, "new_records": total_new}


async def _fetch_all_symbols(plans: list[tuple[str, int | None]]) -> dict[str, list]:
    """按计划拉取所有 symbol 的资金费率历史，返回 {symbol: rows}。

    交易所构造走 ``build_exchange``（共享层统一注入 WEB_PROXY + rate limit）：
    此处历史上漏注入代理，导致需要代理出网的环境下 K 线能拉、资金费率拉不到，
    而 sync_funding_rates 不检查失败，缺陷是静默的。统一到共享层后不再有第二处
    可漏的注入点。
    """
    exchange = build_exchange("binance")
    out: dict[str, list] = {}
    try:
        for symbol, since_ms in plans:
            out[symbol] = await _fetch_funding_history(exchange, symbol, since_ms, {})
    finally:
        await exchange.close()
    return out


async def _fetch_funding_history(
    exchange, symbol: str, since_ms: int | None, params: dict
) -> list[dict]:
    """拉取资金费率历史，返回 [{dt: aware datetime, rate: float}, ...]。"""
    out: list[dict] = []
    cursor = since_ms
    while True:
        call_params = dict(params)
        if cursor:
            call_params["startTime"] = cursor
        fetched = await exchange.fetch_funding_rate_history(
            symbol, since=cursor, limit=1000, params=call_params
        )
        if not fetched:
            break
        for item in fetched:
            out.append(
                {
                    "dt": datetime.fromtimestamp(
                        item["timestamp"] / 1000, tz=timezone.utc
                    ),
                    "rate": float(item["fundingRate"]),
                }
            )
        if len(fetched) < 1000:
            break
        cursor = fetched[-1]["timestamp"] + 1
    return out
