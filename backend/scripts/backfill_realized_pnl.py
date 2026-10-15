"""运维工具：回填历史已成交卖单的 realized_pnl 与会话权益（移动平均成本法）。

修复实盘路径此前从未计算已实现盈亏，导致已平仓交易记录与
LiveSession.current_equity 盈亏显示为 0 的问题。本脚本对存量数据
按与 OrderExecutor._compute_close_pnl 完全一致的口径回放成交历史，
补算 realized_pnl 并把增量累加到会话权益。

用法（后端容器内）：
    DJANGO_SETTINGS_MODULE=core.settings.dev python scripts/backfill_realized_pnl.py [--dry-run]

安全性：
- 仅处理 realized_pnl IS NULL 的卖单 → 权益增量 = 完整 pnl（previous_pnl=0）。
- 时点口径：历史仅取 created_at < 本单的成交，与成交时运行的
  _compute_close_pnl 一致；每笔独立按自身时间过滤，处理顺序不影响结果。
- 幂等：重跑会跳过已回填的订单。
- 不触碰买单与非成交单。
"""

import os
import sys

# scripts/ 目录脚本需手动把项目根 (/app) 加入 path，否则 `python scripts/x.py`
# 时 sys.path[0]=scripts/ 找不到 core 包。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings.dev")
import django  # noqa: E402

django.setup()
from django.db.models import F  # noqa: E402

from apps.trading.executor import compute_realized_pnl  # noqa: E402
from apps.trading.models import LiveSession, Order  # noqa: E402


def _load_history(order: Order) -> list[dict]:
    """与 OrderExecutor._compute_close_pnl 同口径的成交历史查询。

    仅回放本单成交时刻之前（created_at < order.created_at）的成交。
    _compute_close_pnl 在成交时运行，届时后续订单尚未产生，天然只
    看到之前的成本基础；追溯回填若不加此过滤，会把本单之后的成交
    混入成本基础，对非最后一笔卖出的 pnl 失真。
    """
    return list(
        Order.objects.filter(
            exchange_account_id=order.exchange_account_id,
            symbol=order.symbol,
            status="filled",
            avg_fill_price__isnull=False,
            created_at__lt=order.created_at,
        )
        .exclude(id=order.id)
        .order_by("created_at")
        .values("side", "filled_quantity", "avg_fill_price")
    )


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    sells = (
        Order.objects.filter(side="sell", status="filled", realized_pnl__isnull=True)
        .exclude(filled_quantity=0)
        .exclude(avg_fill_price__isnull=True)
        .order_by("created_at")
    )
    updated = 0
    skipped = 0
    for order in sells:
        history = _load_history(order)
        pnl = compute_realized_pnl(
            history, order.side, order.filled_quantity, order.avg_fill_price
        )
        if pnl is None:
            print(f"SKIP {order.id} {order.symbol} (no cost basis)")
            skipped += 1
            continue
        if dry_run:
            print(f"WOULD_SET {order.id} {order.symbol} pnl={pnl}")
        else:
            Order.objects.filter(id=order.id).update(realized_pnl=pnl)
            if order.live_session_id:
                LiveSession.objects.filter(id=order.live_session_id).update(
                    current_equity=F("current_equity") + pnl
                )
            print(f"SET {order.id} {order.symbol} pnl={pnl}")
        updated += 1
    print(f"DONE updated={updated} skipped={skipped} dry_run={dry_run}")


if __name__ == "__main__":
    main()
