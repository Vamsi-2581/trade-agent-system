"""每日净值快照的**写入方**（第①段强制前置）。

``daily_snapshots`` 表自建表起没有任何写入任务，而 ``RiskGuard._check_drawdown``
拿它当期初净值——于是「当日已实现亏损」时必然走「无法获取期初资金数据」分支拒绝
下单，且不产生任何告警：**一条活的静默故障**（CONTEXT.md 第 142 条）。修法是补上
这个写入方，不是放宽那条检查。

口径（2026-09-22 定，逐条都有理由）：

* **分母 = 该用户未停止会话所引用、按账户去重后的实时 USDT 余额之和**。
  理由：单子实际就是按这些账户的余额下的额度，`RiskGuard._check_position_limit`
  用的也是 ``adapter.get_balance()``；`LiveSession.current_equity` 只是它的一个
  副本，副本会漂。**按账户去重**是因为同一账户挂两个会话时，两个会话的
  ``current_equity`` 建会话时都等于该账户的**完整**余额，直接求和会把余额翻倍。
* **任一账户余额取不到 → 本轮不写快照**，并把失败告警出去。宁可没有快照，也不写
  一个编出来的期初数字：假的期初会让回撤线静默失真，那正是本单元要消灭的东西。
* **快照标「当日」**（UTC，与 `_check_drawdown` 的 ``today`` 同口径），且当日只写
  第一条——于是它天然等于「**当日首次观测到的权益**」，也就是这条检查真正需要的
  「期初」。检查侧相应只取 ``date=today`` 那一条：当日还没写出来（beat 未起 / 刚过
  日界）时**降级放行 + WARNING**，而不是拒单。它不向前回退到昨天——昨天的权益配
  今天的盈亏是两个口径，宁缺毋滥；代价是「beat 长期不跑 ⇒ 回撤保护长期降级」这个
  缺口只能靠任务健康检查兜底（见 `guard.py` 的「已知残留缺口」）。
* **幂等**：当日已有快照则跳过。所以 5 分钟一轮可以随便重试，进程重启、账户短暂
  不可达都能自愈，不需要「必须在日界准确跑」这种脆弱前提。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from django.utils import timezone

from apps.core.db_utils import db_async

logger = logging.getLogger(__name__)

# 计入「用户当前净值」的会话状态：**除已停止外全部计入**。
# 从 STATUS_CHOICES 推导（用排除法而不是硬编码列举）：新增状态时漏计会让分母
# 偏小、回撤被高估，而 `paused` / `error` 的会话显然还持有仓位。
_STOPPED = "stopped"


def _active_session_statuses() -> tuple[str, ...]:
    from apps.trading.models import LiveSession

    return tuple(s for s, _ in LiveSession.STATUS_CHOICES if s != _STOPPED)


@dataclass
class SnapshotResult:
    """一轮快照的统计（进任务健康检查，不静默）。"""

    users: int = 0
    written: int = 0
    already_written: int = 0
    skipped_no_equity: int = 0
    alert_undelivered: int = 0
    details: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        data = {
            "users": self.users,
            "written": self.written,
            "already_written": self.already_written,
            "skipped_no_equity": self.skipped_no_equity,
            "alert_undelivered": self.alert_undelivered,
        }
        if self.details:
            data["details"] = self.details
        return data


async def _account_usdt(account) -> Decimal | None:
    """单个账户的实时 USDT 余额；取不到返回 None（**不编造 0**）。"""
    from apps.trading.pending_reconcile import build_account_adapter

    adapter = await build_account_adapter(account)
    if adapter is None:
        return None
    try:
        balance = await adapter.get_balance()
    except Exception:  # noqa: BLE001
        logger.warning(
            "[DailySnapshot] 账户 %s 余额获取失败", account.id, exc_info=True
        )
        return None
    finally:
        try:
            await adapter.disconnect()
        except Exception:  # noqa: BLE001
            logger.warning("[DailySnapshot] 适配器断开失败", exc_info=True)

    try:
        return Decimal(str(balance.get("USDT", Decimal("0")) or Decimal("0")))
    except (InvalidOperation, TypeError, ValueError):
        logger.warning(
            "[DailySnapshot] 账户 %s 的 USDT 余额无法解析: %r", account.id, balance
        )
        return None


async def collect_user_equity(user_id) -> Decimal | None:
    """该用户的当前净值 = 未停止会话引用的、去重后的账户实时 USDT 余额之和。

    任一账户取不到余额 → None（调用方据此跳过本轮，而不是写一个残缺的期初）。
    """
    from apps.trading.models import LiveSession

    sessions = await db_async(
        lambda: list(
            LiveSession.objects.filter(
                user_id=user_id, status__in=_active_session_statuses()
            ).select_related("exchange_account")
        )
    )()

    accounts: dict[str, object] = {}
    for session in sessions:
        account = session.exchange_account
        if account is not None:
            accounts[str(account.id)] = account

    if not accounts:
        # 没有任何账户可引用 → 期初无从谈起。这不写 0：0 会让 `_check_drawdown`
        # 走「initial == 0」分支拒绝下单，等于把静默故障换个入口搬回来。
        logger.info("[DailySnapshot] user=%s 无未停止会话，跳过", user_id)
        return None

    total = Decimal("0")
    for account in accounts.values():
        usdt = await _account_usdt(account)
        if usdt is None:
            return None
        total += usdt
    return total


async def write_daily_snapshots(*, now: datetime | None = None) -> SnapshotResult:
    """给每个有未停止会话的用户写当日净值快照（当日只写第一条）。

    Args:
        now: 便于测试注入「当前时间」。
    """
    from apps.trading.models import DailySnapshot, LiveSession

    today: date = (now or timezone.now()).date()

    user_ids = await db_async(
        lambda: list(
            LiveSession.objects.filter(status__in=_active_session_statuses())
            .values_list("user_id", flat=True)
            .distinct()
        )
    )()

    result = SnapshotResult(users=len(user_ids))
    for user_id in user_ids:
        already = await db_async(
            lambda uid=user_id: DailySnapshot.objects.filter(
                user_id=uid, date=today
            ).exists()
        )()
        if already:
            result.already_written += 1
            continue

        equity = await collect_user_equity(user_id)
        if equity is None:
            result.skipped_no_equity += 1
            result.details.append(f"{user_id}: no_equity")
            if not await _alert_write_failure(user_id, today):
                result.alert_undelivered += 1
            continue

        try:
            await db_async(
                lambda uid=user_id, eq=equity: DailySnapshot.objects.create(
                    user_id=uid, date=today, total_equity=eq
                )
            )()
        except Exception:  # noqa: BLE001 - 竞态：两个 worker 同时写同一天
            logger.warning(
                "[DailySnapshot] user=%s date=%s 写入失败（可能已被并发写入）",
                user_id,
                today,
                exc_info=True,
            )
            result.already_written += 1
            continue

        result.written += 1
        result.details.append(f"{user_id}: {equity}")
        logger.info("[DailySnapshot] user=%s date=%s equity=%s", user_id, today, equity)

    return result


# 同一用户同一天只告警一次：本任务 5 分钟一轮，账户持续不可达时逐轮告警会变成骚扰，
# 而骚扰的结果是用户把通知静音——那又回到「沉默」了。
_alerted_on: dict[str, date] = {}


async def _alert_write_failure(user_id, today: date) -> bool:
    from apps.trading.alerts import notify_user

    key = str(user_id)
    if _alerted_on.get(key) == today:
        return True
    delivered = await notify_user(
        user_id,
        "⚠️ 无法记录今日净值快照\n"
        "原因：绑定的交易所账户余额取不到（密钥/网络/权限）。\n"
        "影响：**日内回撤保护当前处于降级状态**——本日期的回撤分母缺失，"
        "风控不会据此拦截下单。请尽快检查账户连接。",
    )
    if delivered:
        _alerted_on[key] = today
    return delivered
