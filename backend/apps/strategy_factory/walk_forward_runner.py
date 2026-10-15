"""walk-forward 分折的**执行层**（spec #19 D4 / D15 / D18；子票 #29）。

纯计算在 `walk_forward.py`；这一层只负责「把它跑出来」：按折取数 → 调既有回测入口
（`StrategyRunner.run_backtest`）→ 把折身份与聚合结论写进 `BacktestResult`。

## 三条纪律

1. **`BacktestEngine` 不动**（D15）。每折都走**既有**回测入口，复用既有成本模型
   （P0）、库优先取数（P1）与参数解析（`resolve_params` 两处共用）。「fold」是编排
   概念，不是引擎概念——把窗口逻辑塞进引擎会让实盘链路也跟着背上它。

2. **折之间串行，且本模块与折执行器都是同步的**。风险与缓解表点名的「分折把回测次数
   放大 N 倍」：库优先取数让每折零 API 调用（已有测试钉住），串行则让内存与交易所配额
   都不随折数线性上涨。一个候选一个任务，不在这里开并发。

   **为什么同步**：既有取数入口 `_load_ohlcv_for_backtest` 是**同步**函数、内部自己
   `asyncio.run`（它被 `run_backtest_task` 从同步上下文调用），而回测入口
   `StrategyRunner.run_backtest` 是 **async**。这两件事不能在同一个事件循环里共存：
   在循环内调取数会直接
   `RuntimeError: asyncio.run() cannot be called from a running event loop`。
   所以照 `run_backtest_task` / `run_grid_search_task` 的既有形状：**同步外壳**，
   同步取数 → 自建事件循环驱动 async 回测 → 关掉循环与异步资源。折执行器的契约因此
   也是同步的（它封装「取这一折的数据 + 跑这一折」这一整件事）。

3. **写 `metrics` 一律读-改-写单个键**。`metrics` 里已经有别人的键（`regime_slice`，
   由 `apps/regime/slicing.py` 写）。整字段覆盖会把它们抹掉——那不是「分折被覆盖」，
   而是「别人的字段凭空消失」。所以照 `save_slice` 的先例：`select_for_update` +
   `update_fields=["metrics"]`。

## 与 #22（编排）的接口

本模块**只跑样本外折**，不跑样本内那一次：样本内窗口是 L1 的政策（跑哪段、什么参数），
由 #22 决定并把它那次的 `BacktestResult.id` 传进来（`in_sample_result_id`）。分折从该行
读 `is_sharpe` 与区间，把聚合结论写回该行。这样两处政策不会各存一份、日后漂移。

**交接给 #22**：L1 的样本内回测应跑 `[start_date, oos_start)`，即
`plan_folds(...).in_sample_end` 那一天——参照窗口不含样本外那一段，否则衰减会被系统性
低估（用样本外自己的数据去解释样本外）。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Callable, Mapping

from apps.strategy_factory.config import CONFIG, FactoryConfig
from apps.strategy_factory.walk_forward import (
    WALK_FORWARD_METRICS_KEY,
    FoldPlan,
    WalkForwardWindowRole,
    aggregate_walk_forward,
    plan_folds,
)

logger = logging.getLogger(__name__)

#: 一折的执行结果：`{"result_id", "sharpe_ratio", "total_trades", ...}`。
#: **同步**（见模块 docstring 第 2 条）：它封装「取这一折的数据 + 跑这一折」一整件事，
#: 而取数是同步的、回测是 async——这个 async/sync 的缝合点就落在实现里。
FoldExecutor = Callable[..., Mapping[str, Any]]


def _parse_date(value: Any) -> date | None:
    """`date` / ISO 串 → `date`；认不出来返回 None。"""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _write_metrics_key(result_id: Any, payload: Mapping[str, Any]) -> None:
    """把 `metrics["walk_forward"]` 写进一行结果，**只动这一个键**（见模块 docstring 第 3 条）。"""
    from django.db import transaction

    from apps.backtest.models import BacktestResult

    with transaction.atomic():
        result = (
            BacktestResult.objects.select_for_update()
            .only("id", "metrics")
            .get(id=result_id)
        )
        metrics = dict(result.metrics or {})
        metrics[WALK_FORWARD_METRICS_KEY] = dict(payload)
        result.metrics = metrics
        result.save(update_fields=["metrics"])


def tag_fold_result(
    result_id: Any, *, candidate_id: Any, fold_payload: Mapping[str, Any]
) -> None:
    """给一折的 `BacktestResult` 打上分组键与折身份（D4 的前两条）。

    分组键是 `candidate_id`（不是外键：回测域不该知道「候选」这个概念，D13）。
    """
    from apps.backtest.models import BacktestResult

    BacktestResult.objects.filter(id=result_id).update(candidate_id=candidate_id)
    _write_metrics_key(result_id, fold_payload)


def clear_fold_results(candidate_id: Any) -> int:
    """删掉该候选**已存在的样本外折行**，返回删掉的**折行**条数（D18 的幂等键落点）。

    判据只有一条：`candidate_id` 非空即为一折。**不解析 `metrics`** —— 那是脆的：
    一行若因任何原因没写上 `window_role` 标记，按 metrics 判就会变成永远清不掉的
    孤儿，而「同一层重复触发」正是靠清理来收敛的。

    这条判据成立的前提是**候选主行不带 `candidate_id`**（它是 L1 的产物，由
    `in_sample_result_id` 显式指认，不是「一折」）。主行因此天然免疫，而不是靠一个
    可能漏写的标记来保护。

    **返回值刻意不是 `QuerySet.delete()` 的返回**：那个数是「本次删掉的全部对象」，
    含级联删除的 `BacktestTrade` 行（一行回测有几十笔成交，于是「删了 5 折」会报成
    几百）。返回值是给人看的日志与幂等断言用的，必须就是折数；所以要显式数一遍再删。
    """
    from apps.backtest.models import BacktestResult

    if not candidate_id:
        return 0
    queryset = BacktestResult.objects.filter(candidate_id=candidate_id)
    fold_count = queryset.count()
    queryset.delete()
    return fold_count


def _default_fold_executor(
    *,
    strategy_name: str,
    symbol: str,
    timeframe: str,
    start_date: str,
    end_date: str,
    initial_capital: Decimal,
    parameters: dict | None,
    strategy_id: str | None,
    exchange: str,
    is_perp: bool,
    include_funding: bool,
) -> Mapping[str, Any]:
    """一折的默认执行：既有取数（库优先）→ 既有回测入口（D15）。

    同步外壳，内部自建事件循环驱动 async 回测（见模块 docstring 第 2 条）。形状与
    `run_backtest_task` 完全一致——包括在长时间回测后刷新连接、关闭 Redis 等异步资源
    （否则 `__del__` 会在循环关闭后报错）、以及**注入资金费率**。

    **为什么必须注入资金费率**：`is_perp=True` 时成本模型（P0）含历史资金费率回放。
    样本内那一行是 `run_backtest_task` 跑的、带资金费率；若各折不带，衰减比较的就是
    「含资金费率的样本内」对「不含资金费率的样本外」——分母被系统性抬高，衰减看起来
    更小、更容易放行。这正是用户故事 13「每一层判定都复用 P0 的成本模型…口径一致」
    要防的事，也是一次静默的口径分叉。
    """
    from django.db import close_old_connections

    from apps.backtest.tasks import (
        _close_async_resources,
        _load_ohlcv_for_backtest,
        load_funding_rates,
    )
    from apps.strategy_engine.runner import StrategyRunner

    # 取数是同步的、且内部自己 asyncio.run：必须在**没有**运行中循环的上下文里调它。
    ohlcv_data, source_stats = _load_ohlcv_for_backtest(
        symbol, timeframe, start_date=start_date, end_date=end_date, exchange=exchange
    )
    if not ohlcv_data:
        # 「这一折没有数据」是一种结论，不是一次崩溃：交给聚合层判「证据不足」。
        return {
            "error": f"未能获取 {symbol} {timeframe} 在 {start_date}~{end_date} 的K线数据"
        }

    # 与 `run_backtest_task` 同一条件、同一函数：永续 + 计入资金费率时才组装费率表。
    funding_rates = (
        load_funding_rates(symbol, ohlcv_data) if (is_perp and include_funding) else {}
    )

    runner = StrategyRunner()
    loop = asyncio.new_event_loop()
    try:
        # 取数可能耗时很久，连接可能已被 pgbouncer 关闭。
        close_old_connections()
        stats = loop.run_until_complete(
            runner.run_backtest(
                strategy_name=strategy_name,
                symbol=symbol,
                timeframe=timeframe,
                ohlcv_data=ohlcv_data,
                initial_capital=initial_capital,
                parameters=parameters,
                strategy_id=strategy_id,
                commission_rate=Decimal("0.001"),
                is_perp=is_perp,
                funding_rates=funding_rates,
            )
        )
    finally:
        _close_async_resources(loop)
        loop.close()

    # `data_source` 由取数层产出，而 `save_backtest_result` 不感知它（`run_backtest_task`
    # 也是在结果落库后补写的）。票面要求每折「复用既有 data_source」，所以这里照同一
    # 形状补写——否则「这一折零 API」这句话在库里没有落点，事后无法复核。
    persisted_id = stats.get("result_id")
    if persisted_id:
        close_old_connections()
        from apps.backtest.models import BacktestResult

        BacktestResult.objects.filter(id=persisted_id).update(data_source=source_stats)

    return {**stats, "data_source": source_stats}


def run_walk_forward(
    *,
    candidate_id: Any,
    strategy_name: str,
    symbol: str,
    timeframe: str,
    in_sample_result_id: Any,
    initial_capital: Decimal | None = None,
    parameters: dict | None = None,
    strategy_id: str | None = None,
    exchange: str = "binance",
    is_perp: bool = False,
    include_funding: bool = True,
    config: FactoryConfig = CONFIG,
    fold_executor: FoldExecutor | None = None,
    fold_plan: FoldPlan | None = None,
    clear_existing: bool = True,
) -> dict[str, Any]:
    """跑该候选的样本外分折，并把聚合结论写回候选主行（同步外壳，见模块 docstring 第 2 条）。

    返回 `metrics["walk_forward"]` 的那份载荷（同时已落库）。**不抛**「数据不足」类
    异常——那些是结论；只有配置非法或主行区间解析不出才抛。

    `fold_executor` 可注入（测试用假执行器断言接线；真实路径用默认执行器）。
    """
    from django.db import close_old_connections

    from apps.backtest.models import BacktestResult

    main_row = BacktestResult.objects.get(id=in_sample_result_id)
    start = _parse_date(main_row.start_date)
    end = _parse_date(main_row.end_date)
    if start is None or end is None:
        raise ValueError(
            f"候选主行 {in_sample_result_id} 的区间无法解析："
            f"start_date={main_row.start_date!r} end_date={main_row.end_date!r}"
        )

    plan = fold_plan or plan_folds(start, end, config=config)
    executor = fold_executor or _default_fold_executor
    capital = (
        initial_capital
        if initial_capital is not None
        else Decimal(str(main_row.initial_capital))
    )
    # 参数严格继承候选主行那一份（用户故事 19：paper 与回测参数不得静默分叉，分折与
    # 样本内同理——否则衰减比较的是两个不同策略）。
    effective_params = (
        parameters if parameters is not None else (main_row.parameters or {})
    )
    resolved_strategy_id = strategy_id or str(main_row.strategy_id)
    main_sharpe = main_row.sharpe_ratio

    if clear_existing:
        removed = clear_fold_results(candidate_id)
        if removed:
            logger.info(
                "[WalkForward] candidate=%s cleared %d stale fold rows",
                candidate_id,
                removed,
            )

    if not plan.data_sufficient:
        # 切不出折：不跑任何回测，直接落「证据不足」（漏斗下限 = 什么都不发生）。
        logger.warning(
            "[WalkForward] candidate=%s insufficient geometry: %s",
            candidate_id,
            plan.reason,
        )
        payload = aggregate_walk_forward(
            candidate_id=candidate_id,
            is_sharpe=main_sharpe,
            plan=plan,
            fold_rows=(),
            config=config,
        )
        _write_metrics_key(in_sample_result_id, payload)
        return payload

    fold_rows: list[dict[str, Any]] = []
    for window in plan.windows:
        oos_start, oos_end = window.oos_range()
        # 长时间回测后连接可能已被 pgbouncer 关闭（CONN_MAX_AGE=30s）；每折开始前
        # 刷新，否则落库会以 "connection already closed" 失败。
        close_old_connections()
        try:
            stats = executor(
                strategy_name=strategy_name,
                symbol=symbol,
                timeframe=timeframe,
                start_date=oos_start,
                end_date=oos_end,
                initial_capital=capital,
                parameters=effective_params,
                strategy_id=resolved_strategy_id,
                exchange=exchange,
                is_perp=is_perp,
                include_funding=include_funding,
            )
        except Exception as exc:  # noqa: BLE001 — 一折失败不该炸掉整轮分折
            logger.warning(
                "[WalkForward] candidate=%s fold=%d raised: %s",
                candidate_id,
                window.fold_index,
                exc,
            )
            stats = {"error": str(exc)}

        fold_row = {
            "fold_index": window.fold_index,
            "window_role": WalkForwardWindowRole.OOS.value,
            "result_id": stats.get("result_id"),
            "start_date": oos_start,
            "end_date": oos_end,
            "sharpe_ratio": stats.get("sharpe_ratio"),
            "total_trades": stats.get("total_trades"),
            "in_sample_start": window.in_sample_start.isoformat(),
            "in_sample_end": window.in_sample_end.isoformat(),
        }
        if stats.get("error"):
            # 取不到数据 / 回测抛错 → 这一折没有可用夏普。**不写 0**：0 会让衰减
            # 静默变小，与 #26 修掉的 NaN fail-open 同类。聚合层据此判「证据不足」。
            fold_row["error"] = str(stats["error"])
            fold_row["sharpe_ratio"] = None

        if fold_row["result_id"]:
            close_old_connections()
            tag_fold_result(
                fold_row["result_id"],
                candidate_id=candidate_id,
                fold_payload={**fold_row, "split_mode": plan.split_mode},
            )
        fold_rows.append(fold_row)

    close_old_connections()
    payload = aggregate_walk_forward(
        candidate_id=candidate_id,
        is_sharpe=main_row.sharpe_ratio,
        plan=plan,
        fold_rows=fold_rows,
        config=config,
    )
    _write_metrics_key(in_sample_result_id, payload)
    logger.info(
        "[WalkForward] candidate=%s folds=%d sufficient=%s decay_pct=%s",
        candidate_id,
        len(fold_rows),
        payload["data_sufficient"],
        payload["decay_pct"],
    )
    return payload
