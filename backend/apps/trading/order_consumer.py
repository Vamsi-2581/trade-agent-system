"""
订单消费者 - 处理来自Redis Stream的订单任务

消费 trading:orders 流中的订单，通过OrderExecutor执行实际交易。
由FrameManager在启动交易框架时启动。
"""

import asyncio
import logging
from decimal import Decimal


from apps.agent.bus import TRADING_ORDERS, CG_EXECUTOR, consume, ack
from apps.trading.executor import OrderExecutor

logger = logging.getLogger(__name__)


async def start_order_consumer():
    """启动订单消费者协程"""
    consumer_name = f"order_consumer_{id(asyncio.current_task())}"
    logger.info(f"[OrderConsumer] starting with consumer name: {consumer_name}")

    while True:
        try:
            # 消费订单消息
            messages = await consume(TRADING_ORDERS, CG_EXECUTOR, consumer_name)

            if not messages:
                # 没有消息时短暂休眠避免忙等待
                await asyncio.sleep(0.1)
                continue

            # 批量处理消息
            for msg_id, fields in messages:
                try:
                    # 60秒超时保护
                    await asyncio.wait_for(_process_order(fields), timeout=60.0)
                except asyncio.TimeoutError:
                    logger.error(
                        f"[OrderConsumer] timeout processing order {fields.get('task_id')}, "
                        f"symbol={fields.get('symbol')}"
                    )
                    # 超时后仍需 ACK，避免重复处理
                except Exception as e:
                    logger.error(
                        f"[OrderConsumer] failed to process order {fields.get('task_id')}: {e}"
                    )

                # ACK 消息（无论成功失败都 ACK，避免无限重试）
                try:
                    await ack(TRADING_ORDERS, CG_EXECUTOR, msg_id)
                except Exception as e:
                    logger.error(f"[OrderConsumer] failed to ack message {msg_id}: {e}")

        except asyncio.CancelledError:
            logger.info("[OrderConsumer] shutting down")
            break
        except Exception as e:
            logger.error(f"[OrderConsumer] error in main loop: {e}")
            # 出错时短暂休眠避免无限循环
            await asyncio.sleep(1)


async def _process_order(fields: dict):
    """处理单个订单"""
    try:
        logger.info(
            f"[SF-06][OrderConsumer] received order: "
            f"{fields.get('symbol')} {fields.get('side')} {fields.get('quantity')} "
            f"signal={fields.get('signal_name')}"
        )
        # 获取OrderExecutor实例
        executor = OrderExecutor.get_instance()
        if not executor:
            logger.warning(
                "[OrderConsumer] OrderExecutor not initialized, skipping order"
            )
            return

        # 提取订单参数
        exchange = fields.get("exchange")
        symbol = fields.get("symbol")
        side = fields.get("side")
        order_type = fields.get("order_type")
        quantity = fields.get("quantity")
        price = fields.get("price")
        exchange_account_id = fields.get("exchange_account_id")
        user_id = fields.get("user_id")
        live_session_id = fields.get("live_session_id")
        is_close_position = fields.get("is_close_position", False)

        if not all([exchange, symbol, side, order_type, quantity, exchange_account_id]):
            logger.error(f"[OrderConsumer] missing required fields in order: {fields}")
            return

        # 转换数值类型
        quantity = Decimal(str(quantity)) if quantity else Decimal("0")
        price = Decimal(str(price)) if price else None

        # 提交订单
        result = await executor.submit_order(
            exchange=exchange,
            symbol=symbol,
            side=side,
            order_type=order_type,
            quantity=quantity,
            price=price,
            exchange_account_id=exchange_account_id,
            user_id=user_id,
            live_session_id=live_session_id,
            is_close_position=bool(is_close_position),
            # 合约杠杆随单透传（风控 3a/3c 判定输入）；现货消息里没有该字段，缺省 1。
            leverage=int(fields.get("leverage", 1) or 1),
        )

        logger.info(f"[OrderConsumer] order processed: {result}")

    except Exception as e:
        logger.error(
            f"[OrderConsumer] failed to process order {fields.get('task_id')}: {e}"
        )
        # 异常已在调用方处理（ACK + 继续），此处不 raise 避免中断消费者
