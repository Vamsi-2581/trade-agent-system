"""
纸面交易模拟器 - 模拟真实交易执行而不进行实际下单

用于 'paper' 交易模式，允许用户测试策略而无需承担真实风险。
"""

import logging
from decimal import Decimal
from typing import Dict, Tuple
from dataclasses import dataclass


from apps.trading.adapters.base import OrderRequest, OrderResponse

logger = logging.getLogger(__name__)


@dataclass
class Position:
    """持仓信息"""

    symbol: str
    quantity: Decimal
    avg_price: Decimal
    unrealized_pnl: Decimal = Decimal("0")


class PaperTrader:
    """纸面交易模拟器"""

    def __init__(self):
        self._running = False
        self._positions: Dict[str, Position] = {}
        self._balances: Dict[str, Decimal] = {"USDT": Decimal("10000")}  # 初始资金
        self._order_id_counter = 0

    # ─── 风控前置校验 ───────────────────────────────────────────────────────────

    def _get_riskguard(self):
        """获取 RiskGuard 单例，不可用时返回 None"""
        try:
            from apps.riskguard.guard import RiskGuard

            return RiskGuard.get_instance()
        except ImportError:
            logger.warning("[PaperTrader] RiskGuard not available")
            return None

    async def _pre_trade_risk_check(self, request: OrderRequest) -> Tuple[bool, str]:
        """
        下单前风控校验，返回 (approved, reason)。

        只有一套风控实现（架构评审 #5）：RiskGuard。guard 本体对「查不出状态」
        的取向是 fail-closed，这个入口对「guard 不可用 / 校验抛错」的取向必须
        一致——拒单，而不是回落到第二套风控。曾经的硬编码 fallback（≤30次/日、
        ≤20%、无 halt）在 guard 失效时悄悄降低风控标准，正是「guard 兜底家族
        fail-open」的第二个成员，已随本次收口删除。
        """
        riskguard = self._get_riskguard()
        if riskguard is None:
            return False, "RiskGuard 不可用，纸面单按保守方向拒单（fail-closed）"
        try:
            approved, reason = await riskguard.pre_trade_check(request, "paper_trader")
        except Exception as e:
            logger.error(
                f"[PaperTrader] RiskGuard check error — 按保守方向拒单: {e}",
                exc_info=True,
            )
            return False, "RiskGuard 校验异常，纸面单按保守方向拒单（fail-closed）"
        if not approved:
            return False, f"RiskGuard拒绝: {reason}"
        return True, "OK"

    async def start(self):
        """启动纸面交易模拟器"""
        self._running = True
        logger.info("[PaperTrader] started with initial balance USDT 10000")

    async def stop(self):
        """停止纸面交易模拟器"""
        self._running = False
        logger.info("[PaperTrader] stopped")

    async def place_order(self, request: OrderRequest) -> OrderResponse:
        """模拟下单"""
        if not self._running:
            raise RuntimeError("PaperTrader is not running")

        # 风控前置校验
        approved, reason = await self._pre_trade_risk_check(request)
        if not approved:
            self._order_id_counter += 1
            order_id = f"paper_{self._order_id_counter}"
            logger.warning(f"[PaperTrader] order rejected by risk check: {reason}")
            return OrderResponse(
                exchange_order_id=order_id,
                status="rejected",
                filled_qty=Decimal("0"),
                avg_price=Decimal("0"),
                fee=Decimal("0"),
                raw={},
            )

        self._order_id_counter += 1
        order_id = f"paper_{self._order_id_counter}"

        # 模拟订单执行
        executed = await self._execute_order(request)

        if executed:
            logger.info(
                f"[PaperTrader] order executed: {request.symbol} {request.side} {request.quantity}"
            )
            return OrderResponse(
                exchange_order_id=order_id,
                status="filled",
                filled_qty=request.quantity,
                avg_price=request.price or Decimal("0"),
                fee=Decimal("0"),
                raw={},
            )
        else:
            logger.info(
                f"[PaperTrader] order rejected: {request.symbol} {request.side} {request.quantity}"
            )
            return OrderResponse(
                exchange_order_id=order_id,
                status="rejected",
                filled_qty=Decimal("0"),
                avg_price=Decimal("0"),
                fee=Decimal("0"),
                raw={},
            )

    async def _execute_order(self, request: OrderRequest) -> bool:
        """执行订单逻辑"""
        symbol = request.symbol.upper()

        # 计算订单金额
        if request.order_type == "market" and request.price is None:
            # 市价单需要获取市场价格 - 这里简化处理
            price = Decimal("50000")  # 假设比特币价格
            amount = request.quantity * price
        else:
            price = request.price or Decimal("0")
            amount = request.quantity * price

        # 检查余额
        if request.side == "buy":
            if self._balances.get("USDT", Decimal("0")) < amount:
                logger.warning(
                    f"[PaperTrader] insufficient balance for buy order: {amount}"
                )
                return False

            # 更新余额
            self._balances["USDT"] -= amount

            # 更新持仓
            if symbol in self._positions:
                pos = self._positions[symbol]
                # 加权平均成本
                total_qty = pos.quantity + request.quantity
                avg_price = (
                    pos.avg_price * pos.quantity + price * request.quantity
                ) / total_qty
                pos.quantity = total_qty
                pos.avg_price = avg_price
            else:
                self._positions[symbol] = Position(
                    symbol=symbol, quantity=request.quantity, avg_price=price
                )

        elif request.side == "sell":
            # 检查持仓
            if (
                symbol not in self._positions
                or self._positions[symbol].quantity < request.quantity
            ):
                logger.warning(
                    f"[PaperTrader] insufficient position for sell order: {symbol}"
                )
                return False

            # 更新持仓
            pos = self._positions[symbol]
            pos.quantity -= request.quantity

            # 如果完全卖出，计算盈亏
            if pos.quantity == 0:
                realized_pnl = (price - pos.avg_price) * request.quantity
                self._balances["USDT"] += price * request.quantity
                del self._positions[symbol]
                logger.info(
                    f"[PaperTrader] position closed for {symbol}, P&L: {realized_pnl}"
                )
            else:
                # 部分卖出，更新剩余持仓价值
                self._balances["USDT"] += price * request.quantity

        logger.info(
            f"[PaperTrader] balance: {self._balances}, positions: {list(self._positions.keys())}"
        )
        return True

    async def get_positions(self) -> list:
        """获取持仓"""
        if not self._running:
            raise RuntimeError("PaperTrader is not running")

        result = []
        for pos in self._positions.values():
            result.append(pos)
        return result

    async def get_balance(self) -> dict:
        """获取余额"""
        if not self._running:
            raise RuntimeError("PaperTrader is not running")

        return self._balances.copy()
