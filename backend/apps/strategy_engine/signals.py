"""
信号分发器

将策略信号写入 Redis Stream，由 OrderConsumer 消费。
同时记录 SignalTriggerLog 供审计。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .base import OrderSignal

logger = logging.getLogger(__name__)


class SignalDispatcher:
    """信号分发器：策略信号 → Redis Stream → OrderExecutor"""

    def __init__(self, live_session=None):
        """
        Args:
            live_session: LiveSession 实例（可为 None，用于关联订单）
        """
        self.live_session = live_session

    async def dispatch(
        self,
        signal: "OrderSignal",
        symbol: str,
        exchange_account_id: str,
        user_id: str | None = None,
        live_session_id: str | None = None,
    ) -> str:
        """
        分发策略信号到 Redis Stream。

        Args:
            signal: 订单信号
            symbol: 交易对符号
            exchange_account_id: 交易所账户 UUID
            user_id: 用户 UUID
            live_session_id: 实盘会话 UUID

        Returns:
            Redis Stream message_id
        """
        from apps.agent.bus import TRADING_ORDERS, publish

        payload = {
            "exchange": signal.exchange,
            "symbol": symbol,
            "side": signal.side,
            "order_type": signal.order_type,
            "quantity": str(signal.quantity),
            "price": str(signal.price) if signal.price else None,
            "exchange_account_id": exchange_account_id,
            "user_id": user_id,
            "signal_name": signal.signal_name,
            "metadata": signal.metadata,
            "is_close_position": signal.is_close_position,
        }

        if live_session_id:
            payload["live_session_id"] = live_session_id

        try:
            msg_id = await publish(TRADING_ORDERS, payload)
            logger.info(
                f"[SF-05][SignalDispatcher] dispatched: {signal.signal_name} "
                f"{signal.side} {signal.quantity} {symbol} -> msg_id={msg_id}"
            )
            return msg_id
        except Exception as e:
            logger.error(f"[SignalDispatcher] failed to dispatch signal: {e}")
            raise

    async def dispatch_with_risk_check(
        self,
        signal: "OrderSignal",
        symbol: str,
        exchange_account_id: str,
        user_id: str | None = None,
        live_session_id: str | None = None,
    ) -> str | None:
        """
        带风控预检查的信号分发。

        fail-closed：guard 拒单**或 guard 不可用**都返回 None，不留旁路。

        Returns:
            message_id 或 None（被风控拒绝 / 风控不可用）
        """
        from apps.riskguard.guard import RiskGuard

        logger.info(
            f"[SignalDispatcher] dispatching {signal.signal_name} {symbol} side={signal.side} qty={signal.quantity}"
        )

        riskguard = RiskGuard.get_instance()
        if riskguard:
            from apps.trading.adapters.base import OrderRequest

            request = OrderRequest(
                exchange=signal.exchange,
                symbol=symbol,
                side=signal.side,
                order_type=signal.order_type,
                quantity=signal.quantity,
                price=signal.price,
                # 带上账户 id：风控按它解析适配器，才不会拿同交易所另一个账户的
                # 余额来算本单的仓位上限。
                exchange_account_id=exchange_account_id,
                # 会话 id 也要带上：停止判定的策略档要靠它换出策略 id。少这一行的表现是
                # 「停用决策写进了日报，单还是出去了」——不会红任何别的东西。
                live_session_id=live_session_id,
                # 平仓信号 = reduce_only：豁免语义在 guard 定义（整单免前置校验），
                # 入口不再预跳过——否则「这张单拦不拦」又会有两种答案。
                reduce_only=signal.is_close_position,
            )
            approved, reason = await riskguard.pre_trade_check(request, user_id)
            if not approved:
                logger.warning(
                    f"[SF-04][SignalDispatcher] signal rejected by RiskGuard: {reason} "
                    f"| {signal.signal_name} {signal.side} {signal.quantity} {symbol}"
                )
                return None
        else:
            # fail-closed（架构评审 #5）：guard 不可用不再是「warning 后照常下单」。
            # guard 本体对「查不出状态」的取向是拒单，这个入口对「连 guard 都没有」
            # 的取向必须一致——在这里放行等于家族边界上的 fail-open，风控标准随
            # 部署事故悄悄归零。guard 没实例化通常意味着交易框架没起来，那单本就
            # 发不出去；这条 error 就是显式降级记录。
            logger.error(
                "[SF-04][SignalDispatcher] RiskGuard not available — signal rejected "
                f"(fail-closed) | {signal.signal_name} {signal.side} "
                f"{signal.quantity} {symbol}"
            )
            return None

        return await self.dispatch(
            signal, symbol, exchange_account_id, user_id, live_session_id
        )
