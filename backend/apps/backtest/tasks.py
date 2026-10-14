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
            BR.objects.filter(id=result_id).update(
                celery_task_id=self.request.id
            )
        except Exception:
            logger.warning(
                "[BacktestTask] failed to set celery_task_id on result %s",
                result_id, exc_info=True,
            )

    try:
        logger.info(
            f"[BacktestTask] running: strategy={strategy_name} symbol={symbol} "
            f"tf={timeframe} exchange={exchange}"
        )

        if not StrategyRegistry._strategy_path:
            StrategyRegistry.set_strategy_path('/root/.tradelogx/strategies')

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
        ohlcv_data = _fetch_ohlcv_sync(
            symbol, timeframe, exchange, start_date=start_date, end_date=end_date
        )
        if not ohlcv_data:
            tracker.fail(f"未能获取 {symbol} {timeframe} 的历史K线数据")
            raise ValueError(f"未能获取 {symbol} {timeframe} 的历史K线数据")

        logger.info(f"[BacktestTask] OHLCV data fetched: {len(ohlcv_data)} bars")
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
                        Decimal(str(slippage_rate)) if slippage_rate is not None else None
                    ),
                    is_perp=is_perp,
                    funding_rates=funding_rates,
                )
            )
        finally:
            _close_async_resources(loop)
            loop.close()

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


# 常见报价币后缀（长后缀在前，避免 BTC 误匹配 WBTC、ETH 误匹配 WBETH）
_QUOTE_SUFFIXES = (
    "USDT", "USDC", "FDUSD", "BFUSD", "USDP", "TUSD", "BUSD",
    "DAI", "WBTC", "WBETH", "BTC", "ETH", "BNB", "EUR", "GBP",
)


def normalize_binance_symbol(symbol: str) -> str:
    """把用户/agent 输入的 symbol 归一化成 ccxt 标准格式 BASE/QUOTE。

    - "SOL/USDT"  -> "SOL/USDT"（已标准，原样返回）
    - "SOLUSDT"   -> "SOL/USDT"（交易所拼接格式，按报价币后缀拆分）
    - "SOL-USDT" / "SOL_USDT" -> "SOL/USDT"
    - "SOL"       -> "SOL/USDT"（裸基础币，默认 USDT 报价）
    """
    s = (symbol or "").strip().upper()
    if not s:
        return s
    # 统一分隔符：SOL-USDT / SOL_USDT -> SOL/USDT
    s = s.replace("-", "/").replace("_", "/")
    if "/" in s:
        # 已含分隔符（含上面替换产生的），去空段后原样返回
        return "/".join(p for p in s.split("/") if p)
    # 裸报价币种本身（BTC/ETH/WBTC/USDC...）视为基础币，默认 USDT 报价，
    # 避免 "WBTC" 被后缀规则误切成 "W/BTC"
    if s in _QUOTE_SUFFIXES:
        return f"{s}/USDT"
    for quote in _QUOTE_SUFFIXES:
        if s.endswith(quote) and len(s) > len(quote):
            return f"{s[: -len(quote)]}/{quote}"
    return f"{s}/USDT"


def _fetch_ohlcv_sync(
    symbol: str,
    timeframe: str,
    exchange: str = "binance",
    limit: int = 1000,
    start_date: str = "",
    end_date: str = "",
) -> list[dict]:
    """同步获取历史 OHLCV 数据（供 Celery task 使用）"""
    import ccxt.async_support as ccxt
    from django.conf import settings

    symbol_normalized = normalize_binance_symbol(symbol)

    since_ms: int | None = None
    end_ms: int | None = None

    if start_date:
        try:
            since_ms = int(datetime.fromisoformat(start_date).timestamp() * 1000)
        except (ValueError, TypeError):
            logger.warning(f"[BacktestTask] invalid start_date={start_date}, ignoring")
    if end_date:
        try:
            end_ms = int(datetime.fromisoformat(end_date).timestamp() * 1000)
        except (ValueError, TypeError):
            logger.warning(f"[BacktestTask] invalid end_date={end_date}, ignoring")

    async def _fetch():
        exchange_class = getattr(ccxt, exchange.lower(), None)
        if exchange_class is None:
            logger.error(f"[BacktestTask] unsupported exchange: {exchange}")
            return []

        options = {"enableRateLimit": True}
        proxy = getattr(settings, "WEB_PROXY", "") or None
        if proxy:
            options["aiohttp_proxy"] = proxy

        ex = exchange_class(options)
        try:
            # 分页获取全部历史数据（CCXT 每次最多返回 limit 根）
            all_candles: list = []
            batch_since = since_ms
            while True:
                ohlcv = await ex.fetch_ohlcv(
                    symbol_normalized, timeframe, since=batch_since, limit=limit
                )
                if not ohlcv:
                    break
                raw_count = len(ohlcv)
                # Filter batch by end_ms to avoid fetching beyond the target range
                if end_ms is not None:
                    ohlcv = [c for c in ohlcv if c[0] <= end_ms]
                all_candles.extend(ohlcv)
                if raw_count < limit:
                    # Reached present (exchange had fewer bars than requested)
                    break
                if not ohlcv:
                    # All bars in this batch were beyond end_ms
                    break
                # 下一批从最后一根 K 线的时间戳+1ms 开始
                batch_since = ohlcv[-1][0] + 1
                if end_ms is not None and batch_since > end_ms:
                    break
        finally:
            await ex.close()

        if not all_candles:
            logger.warning(f"[BacktestTask] no OHLCV data for {symbol_normalized}")
            return []

        result = []
        for candle in all_candles:
            ts_ms = int(candle[0])
            if end_ms is not None and ts_ms > end_ms:
                continue
            result.append(
                {
                    # UTC 感知时间戳：Binance epoch 为 UTC；naive 本地时间会让
                    # Django（TIME_ZONE=UTC）与资金费率结算点判定偏移机器时区差。
                    "timestamp": datetime.fromtimestamp(
                        ts_ms / 1000, tz=timezone.utc
                    ).isoformat(),
                    "open": float(candle[1]),
                    "high": float(candle[2]),
                    "low": float(candle[3]),
                    "close": float(candle[4]),
                    "volume": float(candle[5]),
                }
            )

        return result

    try:
        return asyncio.run(_fetch())
    except Exception as e:
        logger.error(f"[BacktestTask] OHLCV fetch failed: {e}", exc_info=True)
        return []


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
    queue='grid_search',
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

        tracker.milestone(
            f"已生成 {len(combinations)} 个参数组合", progress=0.05
        )

        tracker.milestone("正在获取历史K线数据...", progress=0.1)
        ohlcv_data = _fetch_ohlcv_sync(
            job.symbol,
            job.timeframe,
            exchange="binance",
            start_date=job.start_date.isoformat() if isinstance(job.start_date, date) else str(job.start_date),
            end_date=job.end_date.isoformat() if isinstance(job.end_date, date) else str(job.end_date),
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
                    logger.warning(
                        f"[GridSearchTask] combination {idx} failed: {e}"
                    )

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



# 同步资金费率的主流币永续（起步集合；后续可挪到配置）
FUNDING_SYNC_SYMBOLS = ["BTC/USDT:USDT", "ETH/USDT:USDT"]


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
        rec.funding_time.astimezone(timezone.utc).isoformat(): rec.rate
        for rec in qs
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

    from django.db import close_old_connections

    from apps.backtest.models import FundingRateRecord

    symbols = symbols or FUNDING_SYNC_SYMBOLS
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
    """按计划拉取所有 symbol 的资金费率历史，返回 {symbol: rows}。"""
    import ccxt.async_support as ccxt

    exchange = ccxt.binance({"enableRateLimit": True})
    out: dict[str, list] = {}
    try:
        for symbol, since_ms in plans:
            out[symbol] = await _fetch_funding_history(
                exchange, symbol, since_ms, {}
            )
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
