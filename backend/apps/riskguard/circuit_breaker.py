"""
CircuitBreaker - 熔断器

触发条件（任一满足）：
1. 日内亏损 > 最大回撤阈值
2. 连续3次下单失败
3. 交易所API连接中断 > 30秒
4. 管理员手动触发

基于 Redis DB7 存储状态。

## 手动触发的落点不在本类（ADR 0001，2026-10-12 兑现）

四条触发条件里的「管理员手动触发」已兑现，但**落点是一行 ``HaltTrigger.MANUAL`` 声明**
（`apps/regime/manual_halt.py` + `manage.py manual_halt`），由 halt 状态机统一判定——
不是这里的 Redis 键：声明表上它有读者（`halt.py` 求值、通知、日报同源），而 Redis 的
trip 状态谁都不读，把它当手动入口等于兑现了一个没人看见的开关。

本类因此只剩**自动路径**（`record_failure` / `record_success`，由
`RiskGuard.record_order_failure` 喂进）；`trip` / `is_open` / `reset` /
`get_failure_count` 目前没有调用点、也没有读点——这是 ADR 0001 的既定状态（「连续下单
失败 / 日内亏损 / API 断连」将来作为触发源接进状态机时再处置），不是丢了的接线。不要把
它们接到 `pre_trade_check` 上（ADR 0001 明确否决：那会造出第二个停止抽象）。
"""

from __future__ import annotations

import logging

import redis.asyncio as aioredis
from django.conf import settings

logger = logging.getLogger(__name__)

# 熔断持续24小时（自然日重置）
CIRCUIT_BREAKER_TTL = 86400


class CircuitBreaker:
    """
    用户级熔断器。

    触发条件：
    1. 日内亏损 > 最大回撤阈值
    2. 连续3次下单失败（1小时窗口）
    3. 交易所API连接中断 > 30秒
    4. 管理员手动触发
    """

    CONSECUTIVE_FAIL_THRESHOLD = 3
    FAIL_COUNT_TTL = 3600  # 连续失败计数1小时窗口

    def __init__(self, user_id: str):
        self.user_id = user_id
        self._breaker_key = f"circuit_breaker:{user_id}"
        self._fail_count_key = f"circuit_fail_count:{user_id}"

    async def is_open(self) -> bool:
        """检查熔断器是否打开"""
        r = await self._get_redis()
        val = await r.get(self._breaker_key)
        await r.aclose()
        return val is not None

    async def trip(self, reason: str, ttl: int = CIRCUIT_BREAKER_TTL) -> None:
        """触发熔断"""
        r = await self._get_redis()
        await r.setex(self._breaker_key, ttl, reason)
        await r.aclose()
        logger.warning(f"Circuit breaker tripped for user {self.user_id}: {reason}")

        # 发送告警
        await self._send_alert(
            f"熔断器触发\n原因: {reason}\n持续时间: {ttl // 3600}小时"
        )

    async def reset(self) -> None:
        """管理员手动重置熔断器"""
        r = await self._get_redis()
        await r.delete(self._breaker_key)
        await r.delete(self._fail_count_key)
        await r.aclose()
        logger.info(f"Circuit breaker reset for user {self.user_id}")

    async def record_failure(self) -> None:
        """
        记录下单失败。
        连续失败达到阈值时触发熔断。
        """
        r = await self._get_redis()
        count = await r.incr(self._fail_count_key)
        await r.expire(self._fail_count_key, self.FAIL_COUNT_TTL)
        await r.aclose()

        if count >= self.CONSECUTIVE_FAIL_THRESHOLD:
            await self.trip(f"连续{count}次下单失败")

    async def record_success(self) -> None:
        """下单成功，重置失败计数"""
        r = await self._get_redis()
        await r.delete(self._fail_count_key)
        await r.aclose()

    async def get_failure_count(self) -> int:
        """获取当前失败计数"""
        r = await self._get_redis()
        val = await r.get(self._fail_count_key)
        await r.aclose()
        return int(val) if val else 0

    async def _get_redis(self) -> aioredis.Redis:
        # redis.asyncio.from_url 是同步函数（Redis 5.x）
        return aioredis.from_url(
            f"{settings.REDIS_URL}/{settings.REDIS_DB_RISK}",
            decode_responses=True,
        )

    async def _send_alert(self, message: str) -> None:
        """发送 Telegram 告警"""
        try:
            # TelegramChannel 需要 supervisor_agent，fallback 到日志
            logger.warning(f"[CircuitBreaker Alert] {message}")
        except Exception:
            logger.warning(f"[CircuitBreaker Alert] {message}")
