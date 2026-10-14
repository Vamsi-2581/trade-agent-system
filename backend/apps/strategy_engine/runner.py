"""
策略运行器 — 统一入口

管理策略在回测/实盘模式下的生命周期。
调用方无需关心底层是回测还是实盘，只需调用对应方法。
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .base import BaseStrategy, StrategyContext

logger = logging.getLogger(__name__)


def resolve_params(strategy_cls, parameters: dict | None = None) -> dict:
    """`策略声明默认值 ⊕ 会话覆盖` → 真正传给 `StrategyContext.params` 的那一份。

    回测与实盘**共用**这一个函数，是为了堵住 2026-10-12 SOL/USDT 那类静默漂移：
    实盘分支当时只做 `params.update(parameters)`，**不注入 `params_schema` 默认值**，
    于是会话 config 里没写的键不会走"策略声明的默认值"，而是落到策略 `__init__` 里
    各自的 `.get(key, fallback)` 兜底。两处默认值一旦分家，会话就是按一个**界面上
    看不到**的参数在跑——`volume_filter` 正是这么全程关着的（会话 config 里没有它，
    实盘没注入 schema 默认值，策略兜底 `False`），而部署界面显示的却是声明默认值。

    没有白名单：`parameters` 里多出来的键照旧原样传入（`session_params.effective_params`
    的 `declared=False` 行描述的就是这些键，两边口径必须一致）。
    """
    schema: dict = getattr(strategy_cls, "params_schema", None) or {}
    params = {
        key: val.get("default") if isinstance(val, dict) else val
        for key, val in schema.items()
    }
    if parameters:
        params.update(parameters)
    return params


class StrategyRunner:
    """策略运行器：回测/实盘统一入口"""

    def __init__(self):
        self._strategy: BaseStrategy | None = None
        self._context: StrategyContext | None = None
        self._live_runner = None

    # ─── 回测模式 ────────────────────────────────────────────────────────────

    async def run_backtest(
        self,
        strategy_name: str,
        symbol: str,
        timeframe: str,
        ohlcv_data: list[dict],
        initial_capital: Decimal,
        parameters: dict | None = None,
        strategy_id: str | None = None,
        git_commit_hash: str = "",
        commission_rate: Decimal = Decimal("0.001"),
        result_id: str | None = None,
        benchmark: str = "",
        user_id: str | None = None,
    ) -> dict:
        """
        用历史数据回放策略。

        Args:
            strategy_name: 策略名称（StrategyRegistry 中注册的 name）
            symbol: 交易对
            timeframe: K 线周期
            ohlcv_data: 历史 K 线数据 [{open, high, low, close, volume, timestamp}, ...]
            initial_capital: 初始资金
            parameters: 策略参数（覆盖 defaults）
            strategy_id: Strategy 模型 UUID（用于存回测结果）
            git_commit_hash: 策略代码的 git 提交哈希
            commission_rate: 手续费率

        Returns:
            回测统计结果
        """
        from .registry import StrategyRegistry
        from .base import StrategyContext
        from .backtest_mode import (
            BacktestEngine,
            save_backtest_result,
            _resolve_strategy_id,
            _get_strategy_id_from_result,
        )

        # 加载策略类
        strategy_cls = StrategyRegistry.get_class(strategy_name)

        # 合并参数：先从 params_schema 提取默认值，再覆盖用户传入的参数
        params = resolve_params(strategy_cls, parameters)

        # 创建上下文
        context = StrategyContext(
            symbol=symbol,
            timeframe=timeframe,
            mode="backtest",
            params=params,
            balance=initial_capital,
            position=Decimal("0"),
        )

        # 实例化策略
        strategy = strategy_cls(context)
        logger.info(
            f"[StrategyRunner] backtest starting: {strategy_name} "
            f"{symbol} {timeframe} bars={len(ohlcv_data)}"
        )

        # 运行回测
        engine = BacktestEngine(
            strategy=strategy,
            ohlcv_data=ohlcv_data,
            initial_capital=initial_capital,
            symbol=symbol,
            timeframe=timeframe,
            commission_rate=commission_rate,
            benchmark=benchmark,
        )
        stats = await engine.run()

        # 存入数据库：优先使用已有的 result_id（BacktestResult），否则根据 strategy_id 查找/创建
        from django.db import close_old_connections

        # DB connections may have gone stale during the backtest engine run
        close_old_connections()

        if result_id:
            # result_id 是 BacktestResult UUID，需要从中获取 strategy_id
            resolved_strategy_id = await _get_strategy_id_from_result(result_id)
            existing_result_id = result_id
        elif strategy_id:
            resolved_strategy_id = strategy_id
            existing_result_id = None
        else:
            resolved_strategy_id = await _resolve_strategy_id(strategy_name)
            existing_result_id = None

        if resolved_strategy_id:
            start_date = ohlcv_data[0].get("timestamp", "")[:10]
            end_date = ohlcv_data[-1].get("timestamp", "")[:10]
            result = await save_backtest_result(
                strategy_id=resolved_strategy_id,
                symbol=symbol,
                timeframe=timeframe,
                start_date=start_date,
                end_date=end_date,
                initial_capital=initial_capital,
                parameters=parameters or {},
                stats=stats,
                ohlcv_data=ohlcv_data[:500],
                git_commit_hash=git_commit_hash,
                existing_result_id=existing_result_id,
            )
            stats["result_id"] = result["result_id"]
            logger.info(
                f"[StrategyRunner] backtest result saved: {result['result_id']} "
                f"trades={result['trade_count']}"
            )
        else:
            logger.warning(
                f"[StrategyRunner] backtest completed but no Strategy model found "
                f"for '{strategy_name}', result not persisted"
            )

        return stats

    # ─── 实盘模式 ────────────────────────────────────────────────────────────

    async def start_live(
        self,
        strategy_name: str,
        symbol: str,
        timeframe: str,
        parameters: dict | None = None,
        exchange_account_id: str = "",
        user_id: str | None = None,
        live_session_id: str | None = None,
        initial_balance: Decimal = Decimal("0"),
    ) -> None:
        """
        启动实盘策略运行。

        Args:
            strategy_name: 策略名称
            symbol: 交易对
            timeframe: K 线周期
            parameters: 策略参数
            exchange_account_id: 交易所账户 UUID
            user_id: 用户 UUID
            live_session_id: 实盘会话 UUID
            initial_balance: 初始余额
        """
        from .registry import StrategyRegistry
        from .base import StrategyContext
        from .live_mode import LiveStrategyRunner

        # 加载策略类
        strategy_cls = StrategyRegistry.get_class(strategy_name)

        # 合并参数：与回测分支**同一函数**（见 resolve_params 的 docstring——
        # 实盘曾漏注入 params_schema 默认值，导致 volume_filter 这类键静默取兜底值）
        params = resolve_params(strategy_cls, parameters)

        # 创建上下文
        context = StrategyContext(
            symbol=symbol,
            timeframe=timeframe,
            mode="live",
            params=params,
            balance=initial_balance,
            position=Decimal("0"),
        )

        # 实例化策略
        strategy = strategy_cls(context)
        logger.info(
            f"[StrategyRunner] live starting: {strategy_name} {symbol} {timeframe}"
        )

        # 创建实盘运行器
        self._live_runner = LiveStrategyRunner(
            strategy=strategy,
            symbol=symbol,
            timeframe=timeframe,
            exchange_account_id=exchange_account_id,
            user_id=user_id,
            live_session_id=live_session_id,
        )

        # 启动（内部会按 strategy.min_kline_length 预加载历史 K 线）
        await self._live_runner.start()

    async def stop_live(self) -> None:
        """停止实盘策略运行"""
        if self._live_runner:
            await self._live_runner.stop()
            self._live_runner = None
            logger.info("[StrategyRunner] live strategy stopped")

    async def on_kline(self, kline: dict) -> None:
        """
        外部 K 线数据注入入口。
        供 FrameManager 或 DataFeed 回调使用。
        """
        if self._live_runner:
            await self._live_runner.on_kline(kline)

    @property
    def is_running_live(self) -> bool:
        """是否正在运行实盘策略"""
        return self._live_runner is not None and self._live_runner._running
