"""
回测引擎 — 历史数据回放

加载策略代码，逐 bar 调用 strategy.on_bar()，
模拟成交并记录交易明细，计算统计指标后存入 DB。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal
from typing import TYPE_CHECKING

from asgiref.sync import sync_to_async
from django.utils import timezone

if TYPE_CHECKING:
    from .base import BaseStrategy, OrderSignal, PortfolioTarget, StrategyContext

logger = logging.getLogger(__name__)

DEFAULT_COMMISSION_RATE = Decimal("0.001")
# taker 单边滑点（0.05%），成交价劣化方向：买更贵、卖更便宜
DEFAULT_SLIPPAGE_RATE = Decimal("0.0005")


def _parse_dt(value: str | None) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    dt = datetime.fromisoformat(value)
    return dt if timezone.is_aware(dt) else timezone.make_aware(dt)


class BacktestEngine:
    """回测引擎：历史数据回放"""

    def __init__(
        self,
        strategy: "BaseStrategy",
        ohlcv_data: list[dict],
        initial_capital: Decimal,
        symbol: str,
        timeframe: str,
        commission_rate: Decimal = DEFAULT_COMMISSION_RATE,
        slippage_rate: Decimal = DEFAULT_SLIPPAGE_RATE,
        benchmark: str = "",
        is_perp: bool = False,
        funding_rates: dict[str, float] | None = None,
    ):
        self.strategy = strategy
        self.ohlcv_data = ohlcv_data
        self.initial_capital = initial_capital
        self.symbol = symbol
        self.timeframe = timeframe
        self.commission_rate = commission_rate
        self.slippage_rate = slippage_rate
        self.benchmark = benchmark
        self.is_perp = is_perp
        # 资金费率表：{ISO8601 结算时间: rate}（由调用方从 FundingRateRecord 组装）
        self.funding_rates = funding_rates or {}

        # 成本明细累计（commission/slippage/funding）
        self.cost_detail: dict = {"commission": 0.0, "slippage": 0.0, "funding": 0.0}

        # 运行状态
        self._cash = initial_capital
        self._position = Decimal("0")
        self._avg_entry_price = Decimal("0")
        self._entry_time = ""  # 开仓时间（用于单次开仓/平仓的回撤兼容）
        self._entry_times: list[
            tuple[Decimal, str]
        ] = []  # [(数量, 开仓时间), ...] 分批建仓追踪
        self._trades: list[dict] = []
        self._equity_curve: list[dict] = []
        self._peak_equity = initial_capital

    async def run(self) -> dict:
        """
        执行回测。

        Phase 2 管线（5 步）:
        ① select_universe → ② generate_insights → ③ construct_portfolio →
        ④ apply_risk_filters → ⑤ _target_to_order

        Returns:
            回测统计结果字典
        """
        self.strategy.on_start()

        for i, kline in enumerate(self.ohlcv_data):
            history = self.ohlcv_data[: i + 1]

            # 同步当前 K 线价格到 context
            self.strategy.ctx.set_price(self.symbol, Decimal(str(kline["close"])))

            # Phase 2: 5-step pipeline
            _ = self.strategy.select_universe()
            insights = self.strategy.generate_insights(kline, history)
            targets = self.strategy.construct_portfolio(insights, self.strategy.ctx)
            safe_targets = self.strategy.apply_risk_filters(targets, self.strategy.ctx)
            for target in safe_targets:
                signal = self._target_to_order(target, self.strategy.ctx)
                if signal:
                    self._process_signal(signal, kline)

            # 永续：每个 bar 检查是否命中 8h 资金费率结算点
            if self.is_perp:
                self._settle_funding(kline)

            # 记录权益点
            current_equity = self._cash + self._position_value(kline)
            self._peak_equity = max(self._peak_equity, current_equity)
            drawdown = (
                float((current_equity - self._peak_equity) / self._peak_equity)
                if self._peak_equity > 0
                else 0.0
            )
            self._equity_curve.append(
                {
                    "timestamp": kline.get("timestamp", ""),
                    "equity": float(current_equity),
                    "drawdown": drawdown,
                }
            )

        self.strategy.on_stop()

        # 如果还有持仓，按最后一根 K 线收盘价平仓
        if self._position > 0 and self.ohlcv_data:
            last_kline = self.ohlcv_data[-1]
            self._close_position(last_kline, "end_of_backtest")

        return self._compute_stats()

    def _target_to_order(
        self, target: "PortfolioTarget", ctx: "StrategyContext"
    ) -> "OrderSignal | None":
        """将目标持仓差量转化为订单信号"""
        diff = target.target_quantity - ctx.position
        if diff == 0:
            return None
        if diff > 0:
            return ctx.buy(diff, signal_name=target.reason)
        else:
            return ctx.sell(abs(diff), signal_name=target.reason)

    def _process_signal(self, signal: "OrderSignal", kline: dict) -> None:
        """处理策略信号，模拟成交"""
        # 使用 K 线收盘价作为基准价（简化市价单模拟），
        # 再按滑点方向劣化：买入成交价上浮、卖出成交价下浮。
        base_price = Decimal(str(kline["close"]))

        if signal.side == "buy":
            fill_price = base_price * (1 + self.slippage_rate)
            self._execute_buy(signal, fill_price, kline)
        elif signal.side == "sell":
            fill_price = base_price * (1 - self.slippage_rate)
            self._execute_sell(signal, fill_price, kline)

    def _execute_buy(
        self, signal: "OrderSignal", fill_price: Decimal, kline: dict
    ) -> None:
        """模拟买入成交"""
        cost = signal.quantity * fill_price
        commission = cost * self.commission_rate
        total_cost = cost + commission

        if total_cost > self._cash:
            # 资金不足，部分成交或跳过
            logger.warning(
                "Backtest: insufficient cash for buy signal. "
                f"need={total_cost}, cash={self._cash}"
            )
            # 计算最大可买数量（含滑点价与佣金）
            max_qty = self._cash / (
                fill_price * (1 + self.slippage_rate + self.commission_rate)
            )
            if max_qty <= 0:
                return
            signal = signal.__class__(
                side="buy",
                quantity=max_qty,
                order_type=signal.order_type,
                price=fill_price,
                signal_name=signal.signal_name,
                exchange=signal.exchange,
                metadata=signal.metadata,
            )
            cost = max_qty * fill_price
            commission = cost * self.commission_rate
            total_cost = cost + commission

        self._cash -= total_cost
        # 滑点成本 = 实际成交额 - 无滑点基准成交额（fill 已含滑点劣化）
        self._accumulate_cost(
            "slippage", cost * self.slippage_rate / (1 + self.slippage_rate)
        )
        self._accumulate_cost("commission", commission)

        # 更新加权平均入场价（按成交金额加权）
        old_cost_basis = self._avg_entry_price * self._position
        self._position += signal.quantity
        new_cost_basis = old_cost_basis + cost
        self._avg_entry_price = (
            new_cost_basis / self._position if self._position > 0 else Decimal("0")
        )

        # 记录开仓时间（FIFO 队列）
        entry_ts = kline.get("timestamp", "")
        self._entry_times.append((signal.quantity, entry_ts))
        self._entry_time = entry_ts  # 兼容旧逻辑

        # 记录买入订单（开仓/加仓）
        trade_type = "open" if old_cost_basis == 0 else "add"
        self._trades.append(
            {
                "entry_time": entry_ts,
                "exit_time": None,
                "side": "long",
                "entry_price": float(fill_price),
                "exit_price": None,
                "quantity": float(signal.quantity),
                "pnl": None,
                "pnl_pct": None,
                "commission": float(commission),
                "slippage": float(cost * self.slippage_rate / (1 + self.slippage_rate)),
                "signal": signal.signal_name,
                "exit_reason": "",
                "trade_type": trade_type,
            }
        )

        # 同步回策略上下文，供策略后续判断使用
        self.strategy.ctx.balance = self._cash
        self.strategy.ctx.set_position(self.symbol, self._position)
        self.strategy.ctx.avg_entry_price = self._avg_entry_price

    def _execute_sell(
        self, signal: "OrderSignal", fill_price: Decimal, kline: dict
    ) -> None:
        """模拟卖出成交"""
        sell_qty = min(signal.quantity, self._position)
        if sell_qty <= 0:
            return

        proceeds = sell_qty * fill_price
        commission = proceeds * self.commission_rate
        net_proceeds = proceeds - commission
        # 滑点成本 = 无滑点基准成交额 - 实际成交额（fill 已含滑点劣化）
        slippage_cost = proceeds * self.slippage_rate / (1 - self.slippage_rate)

        # 计算平均成本（使用引擎追踪的加权平均入场价）
        avg_cost = (
            self._avg_entry_price
            if self._avg_entry_price > 0
            else Decimal(str(kline.get("open", fill_price)))
        )
        pnl = (fill_price - avg_cost) * sell_qty - commission

        # FIFO 匹配：从最早的开仓记录中获取 entry_time
        exit_time = kline.get("timestamp", "")
        entry_time_for_trade = self._get_fifo_entry_time(sell_qty)

        self._cash += net_proceeds
        self._accumulate_cost("slippage", slippage_cost)
        self._accumulate_cost("commission", commission)
        self._position -= sell_qty

        # 同步回策略上下文
        self.strategy.ctx.balance = self._cash
        self.strategy.ctx.set_position(self.symbol, self._position)
        self.strategy.ctx.avg_entry_price = (
            self._avg_entry_price if self._position > 0 else Decimal("0")
        )
        self._trades.append(
            {
                "entry_time": entry_time_for_trade,
                "exit_time": exit_time,
                "side": "long",
                "entry_price": float(avg_cost),
                "exit_price": float(fill_price),
                "quantity": float(sell_qty),
                "pnl": float(pnl),
                "pnl_pct": float(pnl / (avg_cost * sell_qty))
                if avg_cost * sell_qty > 0
                else 0,
                "commission": float(commission),
                "slippage": float(slippage_cost),
                "signal": signal.signal_name,
                "exit_reason": signal.metadata.get("reason", "signal"),
                "trade_type": "close",
            }
        )

    def _get_fifo_entry_time(self, sell_qty: Decimal) -> str:
        """FIFO 匹配：获取卖出部分对应的最早开仓时间"""
        remaining = sell_qty
        entry_time = self._entry_time  # fallback

        i = 0
        while i < len(self._entry_times) and remaining > 0:
            qty, ts = self._entry_times[i]
            if qty <= remaining:
                # 完全消耗这一笔
                entry_time = ts
                remaining -= qty
                self._entry_times.pop(i)
                # 不增加 i，因为 pop 后下一个元素移到当前位置
            else:
                # 部分消耗这一笔
                entry_time = ts
                self._entry_times[i] = (qty - remaining, ts)
                remaining = Decimal("0")

        return entry_time

    def _close_position(self, kline: dict, reason: str) -> None:
        """强制平仓"""
        if self._position <= 0:
            return

        fill_price = Decimal(str(kline["close"])) * (1 - self.slippage_rate)
        proceeds = self._position * fill_price
        commission = proceeds * self.commission_rate
        net_proceeds = proceeds - commission
        slippage_cost = proceeds * self.slippage_rate / (1 - self.slippage_rate)
        avg_cost = (
            self._avg_entry_price
            if self._avg_entry_price > 0
            else Decimal(str(kline.get("open", fill_price)))
        )
        pnl = (fill_price - avg_cost) * self._position - commission

        self._cash += net_proceeds
        self._accumulate_cost("slippage", slippage_cost)
        self._accumulate_cost("commission", commission)

        # FIFO 匹配：获取平仓对应的最早开仓时间
        entry_time_for_close = self._get_fifo_entry_time(self._position)

        self._trades.append(
            {
                "entry_time": entry_time_for_close,
                "exit_time": kline.get("timestamp", ""),
                "side": "long",
                "entry_price": float(avg_cost),
                "exit_price": float(fill_price),
                "quantity": float(self._position),
                "pnl": float(pnl),
                "pnl_pct": float(pnl / (avg_cost * self._position))
                if avg_cost * self._position > 0
                else 0,
                "commission": float(commission),
                "slippage": float(slippage_cost),
                "signal": "close_position",
                "exit_reason": reason,
                "trade_type": "close",
            }
        )

        self._position = Decimal("0")
        self._avg_entry_price = Decimal("0")
        self._entry_time = ""
        self._entry_times = []

        # 平仓后同步回策略上下文
        self.strategy.ctx.balance = self._cash
        self.strategy.ctx.set_position(self.symbol, self._position)
        self.strategy.ctx.avg_entry_price = self._avg_entry_price

    def _accumulate_cost(self, kind: str, amount: Decimal) -> None:
        """累计成本明细（金额为 Decimal，存储为 float）。"""
        if amount:
            self.cost_detail[kind] = self.cost_detail.get(kind, 0.0) + float(amount)

    # Binance 永续资金费率结算点：UTC 00:00 / 08:00 / 16:00
    _FUNDING_HOURS = (0, 8, 16)

    def _settle_funding(self, kline: dict) -> None:
        """若当前 bar 命中资金费率结算点，对当前持仓名义结算一次。

        - 多头在正费率时支付（rate × notional），负费率时收取；
        - 空头方向相反；
        - 无持仓不结算；
        - 费率数据缺失：跳过并记 warning（不静默当作 0）。
        """
        if not self.is_perp or self._position == 0:
            return

        ts = kline.get("timestamp", "")
        try:
            dt = _parse_dt(ts)
        except (ValueError, TypeError):
            dt = None
        if dt is None:
            return
        # 结算点按 UTC 整点判定（Binance 00/08/16 UTC），与 K 线时区无关
        dt_utc = dt.astimezone(dt_timezone.utc)
        if dt_utc.hour not in self._FUNDING_HOURS or dt_utc.minute != 0:
            return

        rate = self.funding_rates.get(ts)
        if rate is None:
            # 结算时间戳可能有秒级差异，退而按 (UTC date, UTC hour) 匹配
            rate = next(
                (
                    r
                    for t, r in self.funding_rates.items()
                    if (parsed := _parse_dt(t))
                    and parsed.astimezone(dt_timezone.utc).date() == dt_utc.date()
                    and parsed.astimezone(dt_timezone.utc).hour == dt_utc.hour
                ),
                None,
            )
        if rate is None:
            logger.warning(
                "Backtest: funding rate missing at settlement %s for %s — "
                "settlement skipped (NOT treated as zero).",
                ts,
                self.symbol,
            )
            return

        # 名义价值取绝对值：_position_value 对 _position<=0 短路返回 0，
        # 直接拿它算空头会恒为 0（空头资金费率静默丢失），故单独取 abs。
        notional = abs(self._position) * Decimal(str(kline["close"]))
        # 方向由持仓符号决定：多头正费率付（正支出）；空头镜像（收正费率）
        payment = Decimal(str(rate)) * notional
        if self._position < 0:
            payment = -payment

        # payment > 0：现金支出；payment < 0：现金收入
        self._cash -= payment
        self._accumulate_cost("funding", payment)
        if payment:
            logger.info(
                "Backtest: funding settled at %s: rate=%s notional=%s payment=%s",
                ts,
                rate,
                notional,
                payment,
            )

    def _position_value(self, kline: dict) -> Decimal:
        """当前持仓市值"""
        if self._position <= 0:
            return Decimal("0")
        price = Decimal(str(kline["close"]))
        return self._position * price

    def _compute_stats(self) -> dict:
        """计算回测统计指标"""
        final_equity = self._cash + (
            self._position * Decimal(str(self.ohlcv_data[-1]["close"]))
            if self.ohlcv_data and self._position > 0
            else Decimal("0")
        )

        total_return = float(
            ((final_equity - self.initial_capital) / self.initial_capital) * 100
        )

        # 最大回撤
        max_drawdown = 0.0
        peak = self.initial_capital
        for point in self._equity_curve:
            equity = Decimal(str(point["equity"]))
            if equity > peak:
                peak = equity
            dd = float((equity - peak) / peak) if peak > 0 else 0
            if dd < max_drawdown:
                max_drawdown = dd

        # 胜率（仅统计已平仓的交易）
        closed_trades = [t for t in self._trades if t.get("pnl") is not None]
        winning_trades = [t for t in closed_trades if t["pnl"] > 0]
        total_closed = len(closed_trades)
        win_rate = len(winning_trades) / total_closed if total_closed > 0 else None

        # 夏普比率（简化：用日收益率）
        sharpe = self._compute_sharpe()

        stats: dict = {
            "final_equity": float(final_equity),
            "total_return_pct": total_return,
            "max_drawdown_pct": max_drawdown * 100,
            "win_rate": win_rate,
            "sharpe_ratio": sharpe,
            "total_trades": total_closed,
            "cost_detail": dict(self.cost_detail),
            "equity_curve": self._equity_curve,
            "trades": self._trades,
        }

        # Buy & Hold 基准对比
        if self.benchmark == "buy_and_hold" and self.ohlcv_data:
            benchmark_stats = self._compute_buy_and_hold()
            stats["benchmark"] = benchmark_stats

        return stats

    def _compute_buy_and_hold(self) -> dict:
        """计算 Buy & Hold 基准：首根K线买入，最后一根K线卖出"""
        if not self.ohlcv_data or len(self.ohlcv_data) < 2:
            return {}

        first_price = Decimal(str(self.ohlcv_data[0]["close"]))
        last_price = Decimal(str(self.ohlcv_data[-1]["close"]))

        # 买入（扣除手续费）
        buy_cost = self.initial_capital * self.commission_rate
        net_invested = self.initial_capital - buy_cost
        position = net_invested / first_price

        # 卖出（扣除手续费）
        gross_proceeds = position * last_price
        sell_commission = gross_proceeds * self.commission_rate
        final_value = gross_proceeds - sell_commission

        bh_return = float(
            (final_value - self.initial_capital) / self.initial_capital * 100
        )

        # 生成基准权益曲线
        bh_curve = []
        bh_return_decimal = Decimal(str(bh_return)) / Decimal("100")
        for point in self._equity_curve:
            ts = point["timestamp"]
            # 找到对应时间的价格（用收盘价线性近似）
            bh_curve.append(
                {
                    "timestamp": ts,
                    "equity": float(self.initial_capital * (1 + bh_return_decimal)),
                }
            )

        return {
            "name": "Buy & Hold",
            "final_value": float(final_value),
            "return_pct": bh_return,
            "curve": bh_curve,
        }

    def _compute_sharpe(self) -> float | None:
        """计算夏普比率（简化版）"""
        if len(self._equity_curve) < 2:
            return None

        returns = []
        for i in range(1, len(self._equity_curve)):
            prev = self._equity_curve[i - 1]["equity"]
            curr = self._equity_curve[i]["equity"]
            if prev > 0:
                returns.append((curr - prev) / prev)

        if not returns:
            return None

        import numpy as np

        ret_arr = np.array(returns)
        mean_ret = np.mean(ret_arr)
        std_ret = np.std(ret_arr)

        if std_ret == 0:
            return None

        # 年化夏普（假设日频率）
        annual_factor = np.sqrt(252)
        return float((mean_ret / std_ret) * annual_factor)


async def _resolve_strategy_id(strategy_name: str) -> str | None:
    from asgiref.sync import sync_to_async
    from apps.trading.models import Strategy

    # 先精确匹配，失败后尝试模糊解析
    canonical = _resolve_strategy_name(strategy_name)

    @sync_to_async
    def _get_or_create():
        # sync_to_async 在独立线程中执行，连接刷新必须在函数内部
        from django.db import close_old_connections

        close_old_connections()
        obj, created = Strategy.objects.get_or_create(
            name=canonical,
            defaults={
                "code_path": f"strategies/{canonical}.py",
                "is_active": False,
            },
        )
        if created:
            logger.info(
                f"[BacktestMode] created Strategy model: {obj.id} name={canonical}"
            )
        return str(obj.id)

    try:
        return await _get_or_create()
    except Exception as e:
        logger.error(
            f"[BacktestMode] failed to resolve Strategy for '{strategy_name}': {e}"
        )
        return None


def _resolve_strategy_name(requested_name: str) -> str:
    """解析策略名：精确匹配优先，失败后模糊匹配注册表。

    解决 Agent 提交名与实际注册名不一致的问题。
    例如 Agent 提交 "dgt_grid_strategy"，但实际注册名是 "dgt_rsi_strategy"。
    """
    from .registry import StrategyRegistry

    # 1. 精确匹配
    if requested_name in StrategyRegistry.list_registered():
        return requested_name

    registered = StrategyRegistry.list_registered()
    if not registered:
        return requested_name

    # 2. 模糊匹配：检查请求名是否是某个注册名的子串
    for reg_name in registered:
        if requested_name in reg_name or reg_name in requested_name:
            logger.info(
                "[BacktestMode] strategy name resolved: '%s' -> '%s'",
                requested_name,
                reg_name,
            )
            return reg_name

    # 3. 无匹配时返回原始名（后续会报 Strategy not found）
    return requested_name


async def _get_strategy_id_from_result(result_id: str) -> str | None:
    """从 BacktestResult 获取关联的 Strategy UUID。"""
    from asgiref.sync import sync_to_async
    from apps.backtest.models import BacktestResult

    @sync_to_async
    def _lookup():
        # sync_to_async 在独立线程中执行，连接刷新必须在函数内部
        from django.db import close_old_connections

        close_old_connections()
        result = BacktestResult.objects.get(id=result_id)
        return str(result.strategy_id)

    try:
        return await _lookup()
    except Exception as e:
        logger.error(
            f"[BacktestMode] failed to get strategy_id from result '{result_id}': {e}"
        )
        return None


def create_empty_result(
    strategy_id: str,
    symbol: str,
    timeframe: str,
    start_date: str,
    end_date: str,
    initial_capital: Decimal,
    parameters: dict,
    user_id: str | None = None,
) -> str | None:
    from apps.backtest.models import BacktestResult
    from apps.trading.models import Strategy

    try:
        strategy = Strategy.objects.get(id=strategy_id)
        result = BacktestResult.objects.create(
            strategy=strategy,
            user_id=user_id,
            symbol=symbol,
            timeframe=timeframe,
            start_date=start_date[:10] if len(start_date) > 10 else start_date,
            end_date=end_date[:10] if len(end_date) > 10 else end_date,
            initial_capital=initial_capital,
            # 占位值
            final_capital=initial_capital,
            total_return_pct=0.0,
            parameters=parameters,
        )
        return str(result.id)
    except Exception as e:
        logger.error(f"[BacktestMode] failed to create empty result: {e}")
        return None


async def create_empty_result_async(
    strategy_id: str,
    symbol: str,
    timeframe: str,
    start_date: str,
    end_date: str,
    initial_capital: Decimal,
    parameters: dict,
    user_id: str | None = None,
) -> str | None:
    """Async version of create_empty_result — for callers in async context."""
    from asgiref.sync import sync_to_async
    from apps.backtest.models import BacktestResult
    from apps.trading.models import Strategy
    from django.db import close_old_connections

    # Refresh stale connections
    close_old_connections()

    @sync_to_async
    def _create():
        strategy = Strategy.objects.get(id=strategy_id)
        result = BacktestResult.objects.create(
            strategy=strategy,
            user_id=user_id,
            symbol=symbol,
            timeframe=timeframe,
            start_date=start_date[:10] if len(start_date) > 10 else start_date,
            end_date=end_date[:10] if len(end_date) > 10 else end_date,
            initial_capital=initial_capital,
            final_capital=initial_capital,
            total_return_pct=0.0,
            parameters=parameters,
        )
        return str(result.id)

    try:
        return await _create()
    except Exception as e:
        logger.error(f"[BacktestMode] failed to create empty result: {e}")
        return None


async def save_backtest_result(
    strategy_id: str,
    symbol: str,
    timeframe: str,
    start_date: str,
    end_date: str,
    initial_capital: Decimal,
    parameters: dict,
    stats: dict,
    ohlcv_data: list[dict] | None = None,
    git_commit_hash: str = "",
    existing_result_id: str | None = None,
) -> dict:
    """
    将回测结果存入数据库（新建或更新已有记录）。

    Args:
        existing_result_id: 已有 BacktestResult 的 ID（提前创建的空记录）

    Returns:
        {"result_id": str, "trade_count": int}
    """
    from apps.backtest.models import BacktestResult, BacktestTrade
    from apps.trading.models import Strategy

    @sync_to_async
    def _save():
        # 必须在 _save 内部刷新连接，因为 sync_to_async 在独立线程中执行，
        # 线程池线程的 DB 连接可能在长时间回测后被 pgbouncer 关闭。
        # 外部调用 close_old_connections() 只能刷新主线程的连接，对此线程无效。
        from django.db import close_old_connections

        close_old_connections()
        strategy = Strategy.objects.get(id=strategy_id)

        if existing_result_id:
            # 更新已有记录
            result = BacktestResult.objects.get(id=existing_result_id)
            result.final_capital = stats["final_equity"]
            result.total_return_pct = stats["total_return_pct"]
            result.sharpe_ratio = stats["sharpe_ratio"]
            result.max_drawdown_pct = stats["max_drawdown_pct"]
            result.win_rate = stats["win_rate"]
            result.total_trades = stats["total_trades"]
            result.git_commit_hash = git_commit_hash
            result.parameters = parameters
            result.cost_detail = stats.get("cost_detail", {})
            result.equity_curve = stats.get("equity_curve", [])[:2000]
            result.drawdown_curve = [
                {"timestamp": p["timestamp"], "drawdown": p["drawdown"]}
                for p in stats.get("equity_curve", [])[:2000]
            ]
            result.ohlcv_data = ohlcv_data if ohlcv_data else []
            result.indicator_data = (
                _compute_indicators(ohlcv_data) if ohlcv_data else {}
            )
            result.review_status = "approved"
            result.reviewed_at = timezone.now()
            result.save()
        else:
            # 新建记录
            indicator_data = _compute_indicators(ohlcv_data) if ohlcv_data else {}
            result = BacktestResult.objects.create(
                strategy=strategy,
                symbol=symbol,
                timeframe=timeframe,
                start_date=start_date,
                end_date=end_date,
                initial_capital=initial_capital,
                final_capital=stats["final_equity"],
                total_return_pct=stats["total_return_pct"],
                sharpe_ratio=stats["sharpe_ratio"],
                max_drawdown_pct=stats["max_drawdown_pct"],
                win_rate=stats["win_rate"],
                total_trades=stats["total_trades"],
                cost_detail=stats.get("cost_detail", {}),
                git_commit_hash=git_commit_hash,
                parameters=parameters,
                equity_curve=stats.get("equity_curve", [])[:2000],
                drawdown_curve=[
                    {"timestamp": p["timestamp"], "drawdown": p["drawdown"]}
                    for p in stats.get("equity_curve", [])[:2000]
                ],
                ohlcv_data=ohlcv_data if ohlcv_data else [],
                indicator_data=indicator_data,
                review_status="approved",
                reviewed_at=timezone.now(),
            )

        # 保存交易明细：新建时直接创建；更新时先清理旧交易再重建
        if existing_result_id:
            # 更新已有记录，先删除旧交易
            BacktestTrade.objects.filter(backtest=result).delete()

        trades_to_create = []
        for trade_data in stats.get("trades", []):
            entry_time_val = trade_data.get("entry_time")
            exit_time_val = trade_data.get("exit_time")

            trades_to_create.append(
                BacktestTrade(
                    backtest=result,
                    entry_time=_parse_dt(entry_time_val),
                    exit_time=_parse_dt(exit_time_val),
                    side=trade_data["side"],
                    entry_price=trade_data["entry_price"],
                    exit_price=trade_data.get("exit_price"),
                    quantity=trade_data["quantity"],
                    pnl=trade_data.get("pnl"),
                    pnl_pct=trade_data.get("pnl_pct"),
                    commission=trade_data.get("commission", 0),
                    slippage=trade_data.get("slippage", 0),
                    signal=trade_data.get("signal") or "",
                    exit_reason=trade_data.get("exit_reason") or "",
                    trade_type=trade_data.get("trade_type", "open"),
                )
            )
        BacktestTrade.objects.bulk_create(trades_to_create)

        return {"result_id": str(result.id), "trade_count": result.trades.count()}

    return await _save()


def _compute_indicators(ohlcv_data: list[dict]) -> dict:
    """
    计算技术指标并返回前端可消费的 JSON 格式。

    Returns:
        {
            "ma7": [...], "ma25": [...], "ma99": [...],
            "macd": {"dif": [...], "dea": [...], "hist": [...]},
            "rsi": [...]
        }
    """
    import numpy as np

    from apps.signal_monitor.indicators import (
        compute_macd,
        compute_rsi,
        compute_sma,
    )

    closes = np.array([k["close"] for k in ohlcv_data], dtype=np.float64)

    indicator_data: dict = {}

    # MA 均线
    for period, key in [(7, "ma7"), (25, "ma25"), (99, "ma99")]:
        sma = compute_sma(closes, period)
        # 将 NaN 替换为 None 以适配 JSON 序列化
        indicator_data[key] = [float(v) if not np.isnan(v) else None for v in sma]

    # MACD
    macd_result = compute_macd(closes)
    indicator_data["macd"] = {
        "dif": [float(v) if not np.isnan(v) else None for v in macd_result["macd"]],
        "dea": [float(v) if not np.isnan(v) else None for v in macd_result["signal"]],
        "hist": [
            float(v) if not np.isnan(v) else None for v in macd_result["histogram"]
        ],
    }

    # RSI
    rsi = compute_rsi(closes)
    indicator_data["rsi"] = [float(v) if not np.isnan(v) else None for v in rsi]

    return indicator_data
