"""
策略引擎基础模块

定义策略基类、运行上下文、订单信号以及 Phase 1 新增的 Insight/PortfolioTarget 中间层。
"""

from __future__ import annotations

import json
import logging
from abc import ABC
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal

logger = logging.getLogger(__name__)


@dataclass
class Insight:
    """策略洞察 — 表达"我看到的机会"，不关心后续怎么执行。"""

    symbol: str
    direction: Literal["buy", "sell", "hold"]
    confidence: float  # 0.0 ~ 1.0
    period: str  # 预测周期 "1h" | "4h" | "1d"
    source: str  # 策略名称
    quantity: Decimal | None = None
    signal_name: str = ""
    price: Decimal | None = None
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.direction not in ("buy", "sell", "hold"):
            raise ValueError(f"Invalid direction: {self.direction!r}")
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError(f"confidence must be in [0.0, 1.0], got {self.confidence}")
        if self.quantity is not None and self.quantity <= Decimal("0"):
            raise ValueError(f"quantity must be positive, got {self.quantity}")
        if self.price is not None and self.price <= Decimal("0"):
            raise ValueError(f"price must be positive, got {self.price}")
        if self.metadata:
            if len(json.dumps(self.metadata)) > 1024:
                raise ValueError("metadata exceeds 1KB limit")


@dataclass
class PortfolioTarget:
    """目标持仓 — 组合层将洞察转化为具体的持仓目标。"""

    symbol: str
    target_quantity: Decimal  # 目标持仓量（0 = 平仓/空仓, 正数 = 做多）
    reason: str  # 决策理由
    insight_id: str = ""  # 来源 Insight（可追溯）
    target_weight: float | None = None  # Phase 2 预留：目标权重
    metadata: dict = field(default_factory=dict)  # Phase 2: 风控标注等

    def __post_init__(self):
        if self.target_quantity < Decimal("0"):
            raise ValueError(
                f"target_quantity must be >= 0, got {self.target_quantity}"
            )
        if self.target_weight is not None and not (0.0 <= self.target_weight <= 1.0):
            raise ValueError(
                f"target_weight must be in [0.0, 1.0], got {self.target_weight}"
            )


def _signal_to_insight(signal: "OrderSignal", source: str) -> Insight:
    """将 OrderSignal 转换为 Insight，保留数量信息。

    注意：Insight.symbol 需要在调用处从 StrategyContext.symbol 填充。
    """
    return Insight(
        symbol="",  # 由调用处从 self.ctx.symbol 注入
        direction=signal.side,
        confidence=1.0,
        period="",
        source=source,
        quantity=signal.quantity,
        signal_name=signal.signal_name,
        price=signal.price,
        metadata=signal.metadata,
    )


@dataclass
class OrderSignal:
    """策略发出的订单信号"""

    side: str  # 'buy' | 'sell'
    quantity: Decimal
    order_type: str = "market"  # 'market' | 'limit'
    price: Decimal | None = None
    exchange: str = "binance"  # 默认交易所
    signal_name: str = ""  # 信号名称（用于日志和回测记录）
    metadata: dict = field(default_factory=dict)  # 额外元数据
    is_close_position: bool = False  # 平仓信号：下单时以 reduce_only 传入，由 RiskGuard 免除前置校验

    def __post_init__(self):
        if self.side not in ("buy", "sell"):
            raise ValueError(f"Invalid side: {self.side!r}, must be 'buy' or 'sell'")
        if isinstance(self.quantity, float):
            self.quantity = Decimal(str(self.quantity))
        if self.quantity <= 0:
            raise ValueError(f"Quantity must be positive, got {self.quantity}")
        if self.price is not None and isinstance(self.price, float):
            self.price = Decimal(str(self.price))


@dataclass
class PortfolioContext:
    """多标的组合上下文 — 供 Portfolio/Risk/Universe 模型使用。

    由引擎或 construct_portfolio 从 StrategyContext 转换而来，
    包含所有可交易标的的持仓、价格和资金信息。
    """

    positions: dict[str, Decimal] = field(default_factory=dict)
    current_prices: dict[str, Decimal] = field(default_factory=dict)
    total_capital: Decimal = Decimal("0")

    def get_position(self, symbol: str) -> Decimal:
        return self.positions.get(symbol, Decimal("0"))

    def get_price(self, symbol: str) -> Decimal | None:
        return self.current_prices.get(symbol)


class StrategyContext:
    """策略运行上下文，注入到每个策略实例"""

    def __init__(
        self,
        symbol: str,
        timeframe: str,
        mode: str,
        params: dict,
        balance: Decimal | None = None,
        position: Decimal | None = None,
    ):
        self.symbol = symbol
        self.timeframe = timeframe
        self.mode = mode  # 'backtest' | 'live'
        self.params = params
        self._balance = balance or Decimal("0")
        self._position = position or Decimal("0")
        # Phase 2: 多标的持仓追踪（与单标的 position 属性保持同步）
        self._positions: dict[str, Decimal] = {symbol: self._position}
        self._current_prices: dict[str, Decimal] = {}
        # 加权平均持仓成本，由回测/实盘引擎在成交时维护（默认 0，策略可安全读取）
        self.avg_entry_price: Decimal = Decimal("0")

    @property
    def balance(self) -> Decimal:
        """当前可用余额"""
        return self._balance

    @balance.setter
    def balance(self, value: Decimal | float) -> None:
        self._balance = Decimal(str(value))

    @property
    def position(self) -> Decimal:
        """当前主标的持仓量"""
        return self._position

    @position.setter
    def position(self, value: Decimal | float) -> None:
        self._position = Decimal(str(value))
        self._positions[self.symbol] = self._position

    @property
    def positions(self) -> dict[str, Decimal]:
        """所有标的的持仓"""
        return dict(self._positions)

    @property
    def current_prices(self) -> dict[str, Decimal]:
        return dict(self._current_prices)

    def set_position(self, symbol: str, value: Decimal | float) -> None:
        """设置某标的的持仓，同时同步主标的 position（如果 symbol 匹配）。"""
        qty = Decimal(str(value))
        self._positions[symbol] = qty
        if symbol == self.symbol:
            self._position = qty

    def set_price(self, symbol: str, value: Decimal | float) -> None:
        self._current_prices[symbol] = Decimal(str(value))

    def to_portfolio_context(self) -> PortfolioContext:
        """转换为 Phase 2 的多标的组合上下文。"""
        return PortfolioContext(
            positions=self._positions.copy(),
            current_prices=self._current_prices.copy(),
            total_capital=self._balance
            + sum(
                self._positions.get(s, Decimal("0")) * p
                for s, p in self._current_prices.items()
            ),
        )

    def buy(
        self,
        quantity: Decimal,
        price: Decimal | None = None,
        order_type: str = "market",
        signal_name: str = "",
        **kwargs,
    ) -> OrderSignal:
        """发出买入信号"""
        return OrderSignal(
            side="buy",
            quantity=quantity,
            price=price,
            order_type=order_type,
            signal_name=signal_name or "buy",
            **kwargs,
        )

    def sell(
        self,
        quantity: Decimal,
        price: Decimal | None = None,
        order_type: str = "market",
        signal_name: str = "",
        **kwargs,
    ) -> OrderSignal:
        """发出卖出信号"""
        return OrderSignal(
            side="sell",
            quantity=quantity,
            price=price,
            order_type=order_type,
            signal_name=signal_name or "sell",
            **kwargs,
        )

    def close_position(
        self, price: Decimal | None = None, signal_name: str = "close", **kwargs
    ) -> OrderSignal | None:
        """平仓信号（卖出全部持仓）— 以 reduce_only 传入 RiskGuard，由它免除前置校验"""
        if self._position <= 0:
            return None
        return self.sell(
            self._position,
            price=price,
            signal_name=signal_name,
            is_close_position=True,
            **kwargs,
        )


class BaseStrategy(ABC):
    """
    策略基类，所有策略必须继承。

    子类必须实现：
    - name: 策略名称
    - on_bar(kline, history): 每根 K 线完成时的逻辑（Phase 1 后改为可选）

    可选覆盖：
    - on_start(): 策略启动时调用
    - on_stop(): 策略停止时调用
    - params_schema: 参数校验 Schema
    - generate_insights(): 生成交易洞察（默认实现包装 on_bar）
    - construct_portfolio(): 构建目标持仓（默认实现从 Insight 推导）
    """

    name = "unnamed"
    description = ""
    # 策略启动前需要预加载的最小 K 线根数（warmup）。
    # 指标（RSI/MACD/布林带等）需要足够历史才能产出首个有效值，
    # 若启动时不预加载，策略会一直等待历史积累而无法发出信号。
    min_kline_length: int = 500
    description_template = """策略类型：{均值回归/趋势跟踪/突破/动量/套利}
核心指标：{RSI/MACD/布林带/均线/ATR/...}
适用场景：{震荡行情/趋势行情/高波动/低波动/...}
入场逻辑：{一句话概括买入条件，不含具体参数值}"""
    params_schema: dict[str, Any] = {}

    def __init__(self, context: StrategyContext):
        self.ctx = context
        # Phase 2: 默认模型（延迟导入避免循环引用）
        from .portfolio import BasePortfolioModel, SingleAssetPortfolio
        from .risk import BaseRiskModel
        from .universe import BaseUniverseModel, FixedListUniverse

        self.universe: BaseUniverseModel = FixedListUniverse([context.symbol])
        self.portfolio: BasePortfolioModel = SingleAssetPortfolio()
        self.risk_models: list[BaseRiskModel] = []

    def on_bar(self, kline: dict, history: list[dict]) -> "OrderSignal | None":
        """
        每根 K 线完成时调用。

        Args:
            kline: 当前 K 线 {open, high, low, close, volume, timestamp}
            history: 历史 K 线列表（含当前 K 线）

        Returns:
            OrderSignal 或 None（不操作）
        """
        return None

    def generate_insights(self, kline: dict, history: list[dict]) -> list[Insight]:
        """生成交易洞察。默认实现包装 on_bar() → Insight 转换。"""
        signal = self.on_bar(kline, history)
        if signal is None:
            return []
            
        insight = _signal_to_insight(signal, self.name)
        insight.symbol = self.ctx.symbol
        return [insight]

    def construct_portfolio(
        self, insights: list[Insight], context: StrategyContext
    ) -> list[PortfolioTarget]:
        """从洞察推导目标持仓。

        Phase 2: 如果 self.portfolio 是 SingleAssetPortfolio（默认），
        使用内置的简单映射逻辑；否则委托给 portfolio.allocate()。
        """
        # 检测是否为默认的单标的模型 — 保持向后兼容
        if self.portfolio.__class__.__name__ == "SingleAssetPortfolio":
            targets = []
            for ins in insights:
                if ins.direction == "buy":
                    qty = (
                        ins.quantity
                        if ins.quantity is not None
                        else Decimal(
                            str(
                                context.params.get(
                                    "quantity", context.params.get("position_size", 0)
                                )
                            )
                        )
                    )
                    if qty <= Decimal("0"):
                        continue
                    target_qty = context.position + qty
                elif ins.direction == "sell":
                    if context.position <= Decimal("0"):
                        continue
                    targets.append(
                        PortfolioTarget(
                            symbol=ins.symbol,
                            target_quantity=Decimal("0"),
                            reason=ins.signal_name,
                        )
                    )
                    continue
                else:
                    continue
                targets.append(
                    PortfolioTarget(
                        symbol=ins.symbol,
                        target_quantity=target_qty,
                        reason=ins.signal_name,
                    )
                )
            return targets
        # 委托给自定义 portfolio 模型
        return self.portfolio.allocate(insights, context)

    def select_universe(self) -> list[str]:
        """委托给 universe 模型，返回可交易 symbol 列表。"""
        return self.universe.select(self.ctx)

    def apply_risk_filters(
        self, targets: list[PortfolioTarget], context: StrategyContext
    ) -> list[PortfolioTarget]:
        """链式执行所有风险模型，每个模型过滤不满足条件的 target。"""
        for rm in self.risk_models:
            targets = rm.filter(targets, context)
            if not targets:
                break
        return targets

    def on_start(self) -> None:
        """策略启动时调用（初始化指标等）"""

    def on_stop(self) -> None:
        """策略停止时调用（清理资源等）"""

    def get_watch_signals(self) -> list[dict]:
        """返回最小周期信号配置，由 LiveStrategyRunner 注册到 SignalMonitor。

        每个 dict 包含:
            interval: str        — K 线周期 (如 "15m")
            indicator_type: str  — 指标类型 (donchian/bollinger/rsi/...)
            indicator_params: dict — 指标参数
            condition: dict      — 触发条件
            trigger_type: str    — "once" 或 "continuous"

        默认返回空列表。子类覆盖此方法以启用信号预筛选：
        SignalMonitor 先检测最小周期信号 → 触发后再跑完整 on_bar() 验证。
        """
        return []

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(name={self.name!r})"
