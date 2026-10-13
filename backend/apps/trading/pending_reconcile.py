"""悬挂订单的对账扫描（第①段强制前置）。

本地落单成功、``place_order`` 返回之前进程死掉，那批行会永远停在 ``pending`` 且
``exchange_order_id=""``。成交同步原来靠一句「无交易所 ID 则跳过」把它们**永久忽略**
——既不会推进，也不会失败，账面于是既不是空白也不是真实。本模块是这批行唯一的
消解路径，口径严格按 CONTEXT.md 第 137 条：

* 反查确认交易所无此单        → ``failed`` + 告警；
* 反查不可达 / 超时 / 不支持   → ``unknown`` + 告警，**绝不自动标 failed**；
* 反查命中                    → 只补 ``exchange_order_id``，其余交给成交同步。

最后一条是有意的：成交状态与已实现盈亏的**唯一** owner 是
``OrderExecutor._apply_fill``（它还要同步会话权益）。在这里再算一遍盈亏就是第二份
权益算术，两份算术迟早分叉。所以扫描只做「把交易所侧的身份补回本地」这一件事，
补上之后成交同步立刻接手。

**扫描范围严格限定 ``exchange_order_id == ""`` 的活跃订单**：已经有交易所 ID 的行
属于成交同步，不属于这里。

**适配器按订单自己的账户解析，不按交易所名**：按交易所名索引会把同交易所的两个账户
混成一个（拿别人的单去查），于是拿到假的 ``OrderNotFoundError``，进而把一张活着的单
标成 cancelled。本模块一律按 ``exchange_account_id`` 构造/缓存适配器；这与
``OrderExecutor.resolve_adapter``（账户是唯一口径）是同一个判据。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from django.conf import settings
from django.utils import timezone

from apps.core.db_utils import db_async
from .adapters import (
    ADAPTER_MAP,
    BaseExchangeAdapter,
    OrderLookupUnavailableError,
)
from .alerts import alert_order_anomaly

logger = logging.getLogger(__name__)

# 处于这些状态的订单仍可能变成真实敞口，扫描与成交同步都要看它们。
# 常量本体在 ``Order.ACTIVE_STATUSES``（见 models.py 上的说明），不再在这里另立一份：
# 同一份枚举抄两处，迟早有一处漏掉 ``unknown``。

# 落单到 ``place_order`` 返回的窗口。适配器内部预算 45s（下单）+ 30s（落库），
# 留出余量到 5 分钟：比这更新的 ``pending`` 行可能只是正在途中的请求，
# 此刻去反查会得到「交易所还没有这张单」，从而把一张马上要成交的单误标 failed。
PLACEMENT_GRACE_SECONDS = 300


def is_beyond_placement_window(order, now: datetime | None = None) -> bool:
    """该行是否已超出下单窗口（即：只可能是崩溃/丢响应留下的悬挂行）。"""
    created = getattr(order, "created_at", None)
    if created is None:
        return False
    cutoff = (now or timezone.now()) - timedelta(seconds=PLACEMENT_GRACE_SECONDS)
    return created <= cutoff


def _to_bytes(value):
    """将 memoryview/bytearray 等类型统一转为 bytes，供 Fernet 解密使用。"""
    if isinstance(value, (bytes, str)):
        return value
    if isinstance(value, (memoryview, bytearray)):
        return bytes(value)
    return None


def get_fernet():
    """创建 Fernet 解密器（下单适配器与扫描共用同一条解密路径）。"""
    from cryptography.fernet import Fernet

    key = getattr(settings, "FERNET_KEY", "")
    if not key:
        raise ValueError("FERNET_KEY not configured in settings")
    return Fernet(key.encode())


async def build_account_adapter(account) -> BaseExchangeAdapter | None:
    """为**指定账户**构造并连接适配器；缺能力时返回 None（调用方自行计数）。

    与 ``OrderExecutor._load_adapters`` 是同一段逻辑，区别只在按账户而非按交易所名
    索引，且不吞掉异常——扫描需要知道「这个账户连不上」而不是静默跳过。
    """
    adapter_cls = ADAPTER_MAP.get((account.exchange or "").lower())
    if not adapter_cls:
        logger.warning(
            "[PendingReconcile] 账户 %s 的交易所 %s 没有适配器",
            account.id,
            account.exchange,
        )
        return None

    api_key_enc = _to_bytes(account.api_key_enc)
    api_secret_enc = _to_bytes(account.api_secret_enc)
    if not api_key_enc or not api_secret_enc:
        logger.warning(
            "[PendingReconcile] 账户 %s（%s）缺少 API 密钥，无法反查",
            account.id,
            account.label or "default",
        )
        return None

    fernet = get_fernet()
    adapter = adapter_cls(
        fernet.decrypt(api_key_enc).decode(),
        fernet.decrypt(api_secret_enc).decode(),
        account.testnet,
    )
    await adapter.connect()
    return adapter


@dataclass
class DanglingOutcome:
    """一条悬挂行的消解结果。"""

    kind: str  # "resolved" | "failed" | "unknown"
    detail: str = ""
    exchange_order_id: str = ""
    needs_alert: bool = False
    alerted: bool = False

    @property
    def is_open(self) -> bool:
        """是否仍是一张**可能活着**的单（用于进活跃订单口径）。"""
        return self.kind in ("resolved", "unknown")


async def _mark(order, **kwargs) -> None:
    """落库并把内存对象同步成落库后的样子（供同一轮循环继续判断）。"""
    from apps.trading.models import Order

    await db_async(lambda: Order.objects.filter(id=order.id).update(**kwargs))()
    for key, value in kwargs.items():
        setattr(order, key, value)


async def reconcile_dangling_order(order, adapter) -> DanglingOutcome:
    """按幂等键（``request_id`` ↔ ``newClientOrderId``）反查并消解一条悬挂行。

    三分支严格对应 CONTEXT.md 第 137 条。**任何拿不到结论的路径都归到「未知」**——
    包括适配器抛出的意外异常，因为把「不知道」写成 ``failed`` 是谎报：若单子其实在
    交易所，本地记 failed 会让账面与实际分叉，而分叉的账面比空白更危险。
    """
    client_order_id = str(order.request_id)
    already_unknown = order.status == "unknown"

    try:
        raw = await adapter.find_order_by_client_id(client_order_id, order.symbol)
    except OrderLookupUnavailableError as e:
        detail = f"按幂等键反查交易所订单未能得出结论：{e}"
        log = logger.info if already_unknown else logger.warning
        log("[PendingReconcile] order %s: %s", order.id, detail)
    except Exception as e:  # noqa: BLE001
        # 未预期异常同样属于「不知道」，绝不能掉进 failed 分支
        detail = f"按幂等键反查交易所订单失败：{type(e).__name__}: {e}"
        logger.warning("[PendingReconcile] order %s: %s", order.id, detail, exc_info=True)
    else:
        if raw is None:
            detail = "已按幂等键反查，交易所确认不存在此单"
            await _mark(order, status="failed", error_message=detail)
            alerted = await alert_order_anomaly(
                order, f"{detail}。本地已标记为 failed。"
            )
            return DanglingOutcome("failed", detail, needs_alert=True, alerted=alerted)

        exchange_order_id = str(raw.get("orderId") or "")
        if not exchange_order_id:
            # 命中了却拿不到 orderId：仍然是「不知道」，不能当成 failed
            detail = f"按幂等键反查到订单但响应缺少 orderId：{str(raw)[:200]}"
            logger.warning("[PendingReconcile] order %s: %s", order.id, detail)
        else:
            # 只补身份，不写成交状态与盈亏——那是 _apply_fill 的唯一职责
            await _mark(order, exchange_order_id=exchange_order_id)
            detail = f"交易所存在此单（exchange_order_id={exchange_order_id}），已补回本地并交成交同步"
            logger.info("[PendingReconcile] order %s: %s", order.id, detail)
            return DanglingOutcome("resolved", detail, exchange_order_id=exchange_order_id)

    # 到这里一定是「不知道」：标 unknown，且只在**状态发生迁移**时告警一次，
    # 否则每轮扫描都会给用户重发同一条消息。
    await _mark(order, status="unknown", error_message=detail)
    if already_unknown:
        return DanglingOutcome("unknown", detail)
    alerted = await alert_order_anomaly(
        order,
        f"{detail}。本地已标记为「未知」——这张单可能仍在交易所活着，"
        f"请人工确认后再处理（未自动标为 failed）。",
    )
    return DanglingOutcome("unknown", detail, needs_alert=True, alerted=alerted)


@dataclass
class SweepResult:
    """一轮扫描的统计（进任务健康检查，不静默）。"""

    scanned: int = 0
    resolved: int = 0
    failed: int = 0
    unknown: int = 0
    adapter_unavailable: int = 0
    alert_undelivered: int = 0
    details: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        data = {
            "scanned": self.scanned,
            "resolved": self.resolved,
            "failed": self.failed,
            "unknown": self.unknown,
            "adapter_unavailable": self.adapter_unavailable,
            "alert_undelivered": self.alert_undelivered,
        }
        if self.details:
            data["details"] = self.details
        return data


async def sweep_dangling_orders(
    *,
    now: datetime | None = None,
    accounts: list | None = None,
) -> SweepResult:
    """扫描所有悬挂行并逐条消解。进程内成交同步与 beat 兜底扫描共用这一条路径。

    Args:
        now: 便于测试注入「当前时间」。
        accounts: 便于测试注入账户集合；默认从 DB 取 ``is_active=True``。
    """
    from apps.trading.models import Order

    candidates = await db_async(
        lambda: list(
            Order.objects.filter(
                status__in=Order.ACTIVE_STATUSES, exchange_order_id=""
            ).select_related("exchange_account")
        )
    )()
    candidates = [o for o in candidates if is_beyond_placement_window(o, now)]

    result = SweepResult(scanned=len(candidates))
    if not candidates:
        return result

    if accounts is None:
        from apps.exchange.models import ExchangeAccount

        accounts = await db_async(
            lambda: list(ExchangeAccount.objects.filter(is_active=True))
        )()
    by_id = {str(a.id): a for a in accounts}

    # 按账户缓存适配器（不是按交易所名）：同一交易所的多个账户各有各的密钥与
    # testnet 属性，混用会把一张活着的单查成「不存在」。
    built: dict[str, BaseExchangeAdapter | None] = {}
    try:
        for order in candidates:
            account_id = str(order.exchange_account_id)
            if account_id not in built:
                account = by_id.get(account_id)
                if account is None:
                    logger.warning(
                        "[PendingReconcile] order %s 的账户 %s 不在活跃账户中，跳过本轮",
                        order.id,
                        account_id,
                    )
                    built[account_id] = None
                else:
                    try:
                        built[account_id] = await build_account_adapter(account)
                    except Exception:  # noqa: BLE001
                        logger.warning(
                            "[PendingReconcile] 账户 %s 适配器构建失败",
                            account_id,
                            exc_info=True,
                        )
                        built[account_id] = None
            adapter = built[account_id]
            if adapter is None:
                result.adapter_unavailable += 1
                continue

            outcome = await reconcile_dangling_order(order, adapter)
            setattr(result, outcome.kind, getattr(result, outcome.kind) + 1)
            if outcome.needs_alert and not outcome.alerted:
                result.alert_undelivered += 1
            result.details.append(f"{order.id}: {outcome.kind}")
    finally:
        for adapter in built.values():
            if adapter is None:
                continue
            try:
                await adapter.disconnect()
            except Exception:  # noqa: BLE001
                logger.warning("[PendingReconcile] 适配器断开失败", exc_info=True)

    logger.info("[PendingReconcile] sweep done: %s", result.as_dict())
    return result
