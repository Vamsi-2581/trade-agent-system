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

## 一处刻意留下的边界（#22 落编排时要知道）

折行由**折执行器**落库（`StrategyRunner.run_backtest` → `save_backtest_result`），
`candidate_id` 由本模块在它返回之后补写（`tag_fold_result`）。所以若进程恰好在
「执行器已落库」与「补写分组键」之间挂掉，会留下一行**没有 `candidate_id`** 的孤儿
折行——它不会被 `clear_fold_results` 清掉（清理按 `candidate_id` 匹配）。

没有为此加事务：执行器内部跨 async 边界落库（`sync_to_async` 线程），把它包进外层
事务既不安全也不省事；而代价是**有界且可见**的——孤儿行没有任何分折结论（`metrics`
里没有 `walk_forward`），不会被误读成证据，也不会进任何判定。真要收拾，`candidate_id`
为空的回测行本来就是「不属于任何候选」的既有语义（D22 不回填历史，所以历史行也长
这样），沿用既有的清理口径即可。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
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


def clear_fold_results(candidate_id: Any, *, keep_result_id: Any = None) -> int:
    """删掉该候选**已存在的样本外折行**，返回删掉的**折行**条数（D18 的幂等键落点）。

    判据只有一条：`candidate_id` 非空即为一折。**不解析 `metrics`** —— 那是脆的：
    一行若因任何原因没写上 `window_role` 标记，按 metrics 判就会变成永远清不掉的
    孤儿，而「同一层重复触发」正是靠清理来收敛的。

    `keep_result_id` 是**必须显式排除**的那一行：候选主行（样本内行）由调用方传进来，
    它完全可能也带着同一个 `candidate_id`（对调用方来说那是最自然的做法）。不排除
    它，一次重跑就会先删掉自己赖以取 `is_sharpe` 的那一行，随后 `DoesNotExist`
    崩在半路、聚合结论也没写回去——样本内证据就这么没了。

    **不假设主行「结构上不带 candidate_id」**：那是对调用方的假设，不是保证。
    """
    from apps.backtest.models import BacktestResult

    if not candidate_id:
        return 0
    queryset = BacktestResult.objects.filter(candidate_id=candidate_id)
    if keep_result_id:
        queryset = queryset.exclude(id=keep_result_id)
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


def _require_candidate_id(candidate_id: Any) -> str:
    """候选分组键必须是**非空、可规范化的 UUID**（B2 的修复）。

    为什么不能只是「非空」：`clear_fold_results` 的判据是 `candidate_id` 非空即为一折，
    而 Django 的 `filter(candidate_id=None)` 是 `IS NULL`——放一个空值进来，清理会
    一次删光所有**不属于任何候选**的行（历史回测、样本内行……）。所以空值不是
    「少写了个参数」，它是一条指向全表删除的路径。

    规范化成 `str(uuid)` 还有第二个好处：写进列与写进 `metrics` 的字符串逐字一致，
    后续按 `metrics["walk_forward"]["candidate_id"]` 反查不会因为大小写/连字符写法
    不同而查不到。
    """
    if candidate_id is None or candidate_id == "":
        raise ValueError(
            "candidate_id 必填且不能为空——空值会让 clear_fold_results 的 "
            "`filter(candidate_id=None)` 命中「所有不属任何候选的行」"
        )
    try:
        return str(uuid.UUID(str(candidate_id)))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"candidate_id 必须是 UUID，当前 {candidate_id!r}") from exc


def _validate_wiring(
    *,
    main_row: Any,
    main_start: date,
    main_end: date,
    plan: FoldPlan,
    symbol: str,
    timeframe: str,
    is_perp: bool,
) -> None:
    """调用方把行/区间接错了就**当场炸**（B1 / B3 / B4 的修复）。

    这里抛的都不是「数据不足」——那是结论；这些是**接线错误**，它们共同的特征是：
    接错了照样能跑出一个 `data_sufficient=True` 且像模像样的 `decay_pct`，于是错误
    变成一条通过记录。宁可炸在推进任务里，也不要让漏斗放行一条口径错乱的候选。

    三件事：

    1. **主行必须落在样本内区**（`main_end <= plan.in_sample_end`）。主行的
       `sharpe_ratio` 是衰减的**分母**；若那一行覆盖了整个评估窗口（含各折的样本外
       段），分母就混进了样本外的表现、被拉向样本外，衰减被系统性低估——真值 30%
       （应拦下）会算成 ~25.5%（放行）。
    2. **主行不得删**：它带 `candidate_id` 是合法的（调用方最自然的做法），
       清理阶段已用 `keep_result_id` 排除它。
    3. **行与折必须同标的同周期**（B4）：主行 `BTC/USDT:USDT`/`1h` 而各折跑
       `ETH/USDT`/`15m`，衰减就是在比两个不同的东西——照旧 `data_sufficient=True`。
       永续口径同理：主行成本里有资金费率、而调用方按现货（`is_perp=False`）跑折，
       同样是口径分叉。
    """
    if main_end > plan.in_sample_end:
        raise ValueError(
            f"样本内行 {main_row.id} 的区间 [{main_start}, {main_end}] 越过了样本内区"
            f"右界 {plan.in_sample_end}——它的 sharpe 是衰减的**分母**，覆盖到样本外"
            "会让衰减被系统性低估（真值 30% 会算成 ~25.5% 从而错误放行）。"
            "L1 的样本内回测应只跑 [evaluation_start, plan_folds(...).in_sample_end)"
        )
    if main_start < plan.start_date:
        raise ValueError(
            f"样本内行 {main_row.id} 的起点 {main_start} 早于评估窗口起点 "
            f"{plan.start_date}——两者不是同一个候选的窗口"
        )
    if main_row.symbol != symbol:
        raise ValueError(
            f"行与折的标的不一致：主行 {main_row.symbol!r} vs 本次 {symbol!r}"
            "——衰减会在两个不同标的上比较"
        )
    if main_row.timeframe != timeframe:
        raise ValueError(
            f"行与折的周期不一致：主行 {main_row.timeframe!r} vs 本次 {timeframe!r}"
            "——两个口径的夏普不可比（年化系数不同）"
        )
    if not is_perp and float((main_row.cost_detail or {}).get("funding") or 0.0) != 0.0:
        # 主行计了资金费率 → 它是按永续跑的；本次却声明现货。成本模型分叉，
        # 衰减比较的是两套成本。
        raise ValueError(
            f"成本口径分叉：主行 {main_row.id} 计了资金费率（永续），而本次 is_perp=False"
            "（现货）——两套成本模型下的夏普不可比"
        )


def run_walk_forward(
    *,
    candidate_id: Any,
    strategy_name: str,
    symbol: str,
    timeframe: str,
    in_sample_result_id: Any,
    evaluation_start: date,
    evaluation_end: date,
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
    异常——那些是结论；配置非法、或调用方把行/区间接错了才抛。

    `evaluation_start` / `evaluation_end` 是**候选的完整评估窗口**（`#22` 从候选那里
    取），分折几何由它推出。**刻意不从 `in_sample_result_id` 那一行反推**：那一行是
    **样本内**行，它的区间只是完整窗口的前一段。拿它当完整窗口会把折切在样本内区里、
    真正要测的那段一次都不跑——而它看起来一切正常（`data_sufficient=True` + 一个
    像模像样的 `decay_pct`）。

    `in_sample_result_id` 指向 L1 的样本内行：提供 `is_sharpe`（衰减分母）、参数、
    策略与初始资金。它**必须落在样本内区**（`end_date <= plan.in_sample_end`），
    否则分母里混进了样本外自己的表现、衰减被系统性低估——见 `_validate_wiring`。
    """
    from django.db import close_old_connections

    from apps.backtest.models import BacktestResult

    candidate_uuid = _require_candidate_id(candidate_id)
    main_row = BacktestResult.objects.get(id=in_sample_result_id)
    main_start = _parse_date(main_row.start_date)
    main_end = _parse_date(main_row.end_date)
    if main_start is None or main_end is None:
        raise ValueError(
            f"候选主行 {in_sample_result_id} 的区间无法解析："
            f"start_date={main_row.start_date!r} end_date={main_row.end_date!r}"
        )

    plan = fold_plan or plan_folds(evaluation_start, evaluation_end, config=config)
    _validate_wiring(
        main_row=main_row,
        main_start=main_start,
        main_end=main_end,
        plan=plan,
        symbol=symbol,
        timeframe=timeframe,
        is_perp=is_perp,
    )

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
        # 把主行排除在清理之外：它可能也带着同一个 `candidate_id`（对调用方来说是
        # 最自然的做法），而删掉它就等于删掉样本内证据——本函数随后还要写它。
        removed = clear_fold_results(candidate_uuid, keep_result_id=in_sample_result_id)
        if removed:
            logger.info(
                "[WalkForward] candidate=%s cleared %d stale fold rows",
                candidate_uuid,
                removed,
            )

    if not plan.data_sufficient:
        # 切不出折：不跑任何回测，直接落「证据不足」（漏斗下限 = 什么都不发生）。
        logger.warning(
            "[WalkForward] candidate=%s insufficient geometry: %s",
            candidate_uuid,
            plan.reason,
        )
        payload = aggregate_walk_forward(
            candidate_id=candidate_uuid,
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
                candidate_uuid,
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
                candidate_id=candidate_uuid,
                # D4 逐字要求折行载荷含 `candidate_id` / `fold_index` / `window_role`
                # 三个键。`candidate_id` 在列上已有一份，这里再写进 `metrics` 是刻意的
                # 冗余：载荷要能**独立**回答「这一行属于谁」，不必回查列（导出/审计时
                # 列未必被带上）。值用规范化后的字符串，与列逐字一致，便于反查。
                fold_payload={
                    **fold_row,
                    "candidate_id": candidate_uuid,
                    "split_mode": plan.split_mode,
                },
            )
        fold_rows.append(fold_row)

    close_old_connections()
    payload = aggregate_walk_forward(
        candidate_id=candidate_uuid,
        is_sharpe=main_row.sharpe_ratio,
        plan=plan,
        fold_rows=fold_rows,
        config=config,
    )
    _write_metrics_key(in_sample_result_id, payload)
    logger.info(
        "[WalkForward] candidate=%s folds=%d sufficient=%s decay_pct=%s",
        candidate_uuid,
        len(fold_rows),
        payload["data_sufficient"],
        payload["decay_pct"],
    )
    return payload
