import os
import asyncio
import logging


def initialize_services():
    """
    初始化所有必要的后台服务
    """
    import django
    from django.conf import settings

    # 设置 Django 环境
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings.dev")

    if not settings.configured:
        django.setup()

    logger = logging.getLogger(__name__)

    # 启动 AgentTaskConsumer
    try:
        from apps.agent.consumer import AgentTaskConsumer

        consumer = AgentTaskConsumer(concurrency=4)

        # 在新的事件循环中运行异步函数
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def start_consumer():
            await consumer.start()
            logger.info("[ServiceInitializer] AgentTaskConsumer started")

            # 启动 TelegramChannel
            token = getattr(settings, "TELEGRAM_BOT_TOKEN", "")
            if token:
                from apps.channel.telegram import TelegramChannel
                from apps.agent.supervisor import SupervisorAgent

                supervisor = SupervisorAgent.get_instance()
                telegram_channel = TelegramChannel(
                    token=token, supervisor_agent=supervisor
                )

                await telegram_channel.start()
                logger.info("[ServiceInitializer] TelegramChannel started")

                # 保持服务运行
                try:
                    await asyncio.Future()  # 永远等待，直到被取消
                except asyncio.CancelledError:
                    logger.info("[ServiceInitializer] Services shutdown initiated")

                    await telegram_channel.stop()
                    await consumer.stop()
                    logger.info("[ServiceInitializer] Services stopped")
            else:
                logger.warning(
                    "[ServiceInitializer] TELEGRAM_BOT_TOKEN not set, skipping TelegramChannel"
                )

                # 仅保持 AgentTaskConsumer 运行
                try:
                    await asyncio.Future()  # 永远等待，直到被取消
                except asyncio.CancelledError:
                    logger.info("[ServiceInitializer] Consumer shutdown initiated")
                    await consumer.stop()
                    logger.info("[ServiceInitializer] Consumer stopped")

        # 运行服务
        try:
            loop.run_until_complete(start_consumer())
        except KeyboardInterrupt:
            logger.info("Application interrupted by user")
        finally:
            loop.close()

    except Exception as e:
        logger.error(f"Error initializing services: {e}")
        raise


if __name__ == "__main__":
    # 配置基本日志
    logging.basicConfig(level=logging.INFO)

    print("Initializing background services...")
    initialize_services()
