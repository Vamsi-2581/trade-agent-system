"""以**交易所的 ``userTrades.realizedPnl``** 为准，回填/对账本地订单的已实现盈亏。

## 它解决什么

本地 ``Order.realized_pnl`` 是 ``compute_realized_pnl`` 用移动平均成本法回放出来的，
而那条回放**只做平多**：空头自己的盈亏本地推不出来（回放结束仍无多头仓位时返回
``None``）。于是 2026-09-28 那次「用户平掉 BNB 空头」的事故里，两张回补买在交易所侧
把空头抹平（rpnl +5.9296 / +6.0996），而**本地一行记录都没有**（``reduce`` 路径从不
写 ``Order`` 行，见 ``reduce_ledger`` 模块 docstring）。本地成本链因此卡在 −1.37 空头，
09-30 起每一笔会话卖单回放到 ``pos <= 0`` 返回 ``None``，9 笔卖单合计约 −17.37 USDT
没进任何会话权益。

``recompute_realized_pnl`` 修不了它——那条命令回放的是**同一条坏历史**。本命令换一个
信息源：直接问交易所「这些单实际赚赔多少」。这是本次修复里唯一能填上历史缺口的路径，
也是长期的安全网（本地算法与交易所分叉时，以交易所为准）。

## 做两件事

1. **已有行**：``exchange_order_id`` 命中 → ``realized_pnl = Σ(交易所 rpnl)``（权威覆盖，
   自纠偏）。记 delta = 新 − 旧。
2. **缺失行**：本地没有 → **建一行**（side/qty/price 取聚合值、``symbol`` 转斜杠口径、
   归属走 ``resolve_session_for``、``status=filled``、``exchange_order_id=orderId``、
   ``realized_pnl=Σrpnl``、``client_order_id=""``）。

**回补行的 ``created_at`` 必须回填到交易时刻**（``auto_now_add`` 的绕过靠 queryset
``.update()``）：这些行要排在 09-30 的卖单**之前**，成本链才修得回来——这是整个 fix
的命门。落在建行时刻会让回补行排在所有历史卖单之后，等于没修。

## 归属与权益

归属规则与 ``reduce_ledger`` 同一条：**唯一活跃（账户 + 品种）会话，否则归空**。全部处理
完后按会话汇总 delta，``F("current_equity") + Σdelta`` 更新——只调增量，不做全量重设
（同 ``recompute_realized_pnl`` 的理由：权益 2 位小数、逐笔舍入，一次算完会差一分）。

## 默认干跑

不加 ``--apply`` 一行都不写，只打印「旧→新 / 新建行清单 / 每会话权益 delta」。确认无误后
加 ``--apply`` 重跑；幂等——第二遍所有行都已等于交易所口径，报表显示「无改动」。
"""

from __future__ import annotations

from decimal import Decimal

from django.core.management.base import BaseCommand

from apps.core.db_utils import db_async
from apps.exchange.models import ExchangeAccount
from apps.trading import reduce
from apps.trading.models import LiveSession, Order
from apps.trading.reduce_ledger import (
    TRIGGER_EXCHANGE_BACKFILL,
    _group_trades_by_order,
    resolve_session_for,
)

#: 每处理多少条断一次旧连接（长循环纪律，同其他管理命令）。
BATCH_SIZE = 200


def _order_symbols(account_id) -> list[str]:
    """该账户本地 Order 出现过的品种（斜杠口径），去重、排序。"""
    return sorted(
        {
            s
            for s in Order.objects.filter(exchange_account_id=account_id)
            .values_list("symbol", flat=True)
            .distinct()
        }
    )


class Command(BaseCommand):
    help = "以交易所 userTrades.realizedPnl 为准，回填/对账订单已实现盈亏（默认只打印）"

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="真的写库（覆盖 realized_pnl / 新建回补行 / 调会话权益）。不加则只打印",
        )
        parser.add_argument(
            "--account",
            default=None,
            help="只处理这个交易所账户（ExchangeAccount UUID）。不给则全部活跃账户",
        )

    def handle(self, *args, **options):
        import asyncio

        asyncio.run(self._run(apply_=options["apply"], account=options["account"]))

    # ─── 主流程 ────────────────────────────────────────────────────────────

    async def _run(self, *, apply_: bool, account: str | None) -> None:
        accounts = await db_async(self._load_accounts)(account)
        if not accounts:
            self.stdout.write(self.style.WARNING("没有可处理的交易所账户"))
            return

        changes: list[dict] = []
        created_rows: list[dict] = []
        session_deltas: dict = {}

        for acc in accounts:
            await self._process_account(
                acc, changes, created_rows, session_deltas, apply_=apply_
            )

        self._report(changes, created_rows, session_deltas, apply_)
        if not apply_:
            self.stdout.write(
                self.style.WARNING("空跑：一行都没写。确认清单无误后加 --apply 重跑")
            )
            return
        await self._apply(changes, created_rows, session_deltas)

    def _load_accounts(self, account: str | None):
        qs = ExchangeAccount.objects.all()
        if account:
            qs = qs.filter(id=account)
        return list(qs.order_by("created_at"))

    async def _process_account(
        self, acc, changes, created_rows, session_deltas, *, apply_
    ) -> None:
        from apps.trading.pending_reconcile import build_account_adapter

        try:
            adapter = await build_account_adapter(acc)
        except Exception as exc:  # noqa: BLE001
            self.stdout.write(
                self.style.WARNING(
                    f"账户 {acc.id}（{acc.label}）适配器构建失败，跳过：{exc}"
                )
            )
            return
        if adapter is None:
            self.stdout.write(
                self.style.WARNING(
                    f"账户 {acc.id}（{acc.label}）无可用适配器（缺密钥/无该交易所），跳过"
                )
            )
            return

        try:
            symbols = await db_async(_order_symbols)(acc.id)
            for session_symbol in symbols:
                native = reduce.normalize_symbol(session_symbol)
                try:
                    trades = await adapter.fetch_user_trades(native)
                except Exception as exc:  # noqa: BLE001 — 一个品种拉不到不掀掉整个账户
                    self.stdout.write(
                        self.style.WARNING(
                            f"  账户 {acc.id} 品种 {session_symbol} userTrades 拉取失败，跳过：{exc}"
                        )
                    )
                    continue
                grouped = _group_trades_by_order(trades or [])
                await self._reconcile_symbol(
                    acc, session_symbol, grouped, changes, created_rows, session_deltas
                )
        finally:
            try:
                await adapter.disconnect()
            except Exception:  # noqa: BLE001
                pass

    async def _reconcile_symbol(
        self, acc, session_symbol, grouped, changes, created_rows, session_deltas
    ) -> None:
        for order_id, entry in grouped.items():
            existing = await db_async(
                lambda oid=order_id: Order.objects.filter(
                    exchange_account_id=acc.id, exchange_order_id=oid
                ).first()
            )()
            new_pnl = entry["realized_pnl"]
            if existing is not None:
                old_pnl = existing.realized_pnl
                if not _same(old_pnl, new_pnl):
                    delta = (new_pnl or Decimal("0")) - (old_pnl or Decimal("0"))
                    changes.append(
                        {
                            "account_id": acc.id,
                            "order_id": existing.id,
                            "session_symbol": session_symbol,
                            "time": entry["time"],
                            "old": old_pnl,
                            "new": new_pnl,
                            "delta": delta,
                            "session_id": existing.live_session_id,
                        }
                    )
                    if existing.live_session_id:
                        session_deltas[existing.live_session_id] = (
                            session_deltas.get(existing.live_session_id, Decimal("0"))
                            + delta
                        )
                continue

            # 本地没有 → 回补一行。归属规则同 reduce_ledger。
            session = await resolve_session_for(acc.id, session_symbol)
            created_rows.append(
                {
                    "account_id": acc.id,
                    "order_id": order_id,
                    "session_symbol": session_symbol,
                    "side": entry["side"],
                    "qty": entry["qty"],
                    "price": entry["price"],
                    "pnl": new_pnl,
                    "time": entry["time"],
                    "session_id": getattr(session, "id", None),
                }
            )
            if session is not None and new_pnl and new_pnl != 0:
                session_deltas[session.id] = (
                    session_deltas.get(session.id, Decimal("0")) + new_pnl
                )

    # ─── 写库 ──────────────────────────────────────────────────────────────

    async def _apply(self, changes, created_rows, session_deltas) -> None:
        for row in changes:
            await db_async(
                lambda oid=row["order_id"], pnl=row["new"]: Order.objects.filter(
                    id=oid
                ).update(realized_pnl=pnl)
            )()
        self.stdout.write(self.style.SUCCESS(f"已更新 {len(changes)} 笔 realized_pnl"))

        for row in created_rows:
            await self._create_backfill_row(row)
        if created_rows:
            self.stdout.write(
                self.style.SUCCESS(f"已新建 {len(created_rows)} 笔回补行")
            )

        for sid, delta in session_deltas.items():
            if delta == 0:
                continue
            await self._bump(sid, delta)
        self.stdout.write(self.style.SUCCESS("会话权益已按增量调整"))

    async def _create_backfill_row(self, row) -> None:
        @db_async
        def _create():
            kwargs = {
                "exchange_account_id": row["account_id"],
                "symbol": row["session_symbol"],
                "side": (row["side"] or "").lower() or "sell",
                "order_type": "market",
                "quantity": row["qty"],
                "status": "filled",
                "exchange_order_id": row["order_id"],
                "client_order_id": "",
                "filled_quantity": row["qty"],
                "avg_fill_price": row["price"],
                "triggered_strategy": TRIGGER_EXCHANGE_BACKFILL,
            }
            if row["pnl"] is not None:
                kwargs["realized_pnl"] = row["pnl"]
            if row["session_id"]:
                kwargs["live_session_id"] = row["session_id"]
            return Order.objects.create(**kwargs)

        order = await _create()

        # **命门**：created_at 是 auto_now_add，建完必须回填到交易时刻，否则回补行排在
        # 所有历史卖单之后，成本链修不回来。queryset.update() 绕过 auto_now_add。
        if row["time"] is not None:
            await db_async(
                lambda oid=order.id, ts=row["time"]: Order.objects.filter(
                    id=oid
                ).update(created_at=ts)
            )()

    async def _bump(self, session_id, delta: Decimal) -> None:
        from django.db.models import F

        await db_async(
            lambda sid=session_id, d=delta: LiveSession.objects.filter(id=sid).update(
                current_equity=F("current_equity") + d
            )
        )()

    # ─── 报表 ──────────────────────────────────────────────────────────────

    def _report(self, changes, created_rows, session_deltas, apply_) -> None:
        verb = "已" if apply_ else "将"
        if not changes and not created_rows:
            self.stdout.write("没有需要改写的行——本地已与交易所口径一致")
        if changes:
            self.stdout.write(f"{verb}改写 {len(changes)} 笔已实现盈亏：")
            self.stdout.write(
                f"  {'成交时刻':<20} {'品种':<12} {'旧':>16} {'新':>16} {'Δ':>14}"
            )
            for row in changes:
                old_s = "NULL" if row["old"] is None else f"{row['old']:.8f}"
                new_s = "NULL" if row["new"] is None else f"{row['new']:.8f}"
                ts = row["time"].strftime("%Y-%m-%d %H:%M:%S") if row["time"] else "-"
                self.stdout.write(
                    f"  {ts:<20} {row['session_symbol']:<12} "
                    f"{old_s:>16} {new_s:>16} {row['delta']:>14.8f}"
                )
        if created_rows:
            self.stdout.write(f"{verb}新建 {len(created_rows)} 笔回补行：")
            self.stdout.write(
                f"  {'成交时刻':<20} {'品种':<12} {'方向':<5} {'数量':>14} "
                f"{'价格':>12} {'rpnl':>14} {'会话':<10}"
            )
            for row in created_rows:
                ts = row["time"].strftime("%Y-%m-%d %H:%M:%S") if row["time"] else "-"
                price_s = "N/A" if row["price"] is None else f"{row['price']:.8f}"
                sid = str(row["session_id"])[:8] if row["session_id"] else "（归空）"
                self.stdout.write(
                    f"  {ts:<20} {row['session_symbol']:<12} {row['side']:<5} "
                    f"{row['qty']:>14} {price_s:>12} {row['pnl']:>14.8f} {sid:<10}"
                )
        if session_deltas:
            self.stdout.write(f"{verb}调整 {len(session_deltas)} 条会话权益：")
            for sid, delta in sorted(session_deltas.items(), key=lambda kv: str(kv[0])):
                self.stdout.write(f"  {sid}：Δ {delta:+.8f}")


def _same(a: Decimal | None, b: Decimal | None) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return abs(a - b) < Decimal("0.00000001")
