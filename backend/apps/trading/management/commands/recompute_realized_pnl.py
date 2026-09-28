"""按当前成本法重算历史成交的「会话归因已实现盈亏」，并把会话权益同步过去。

**它存在的理由是一次已确认的错误归因。** `compute_realized_pnl` 最初用
`pos = max(0, pos - q)` 回放历史（只做多）。2026-09-20 的事故把 donchian 会话所在账户
卖成 −17718 空头；截断把这个空头压成 0，于是 09-21 那两笔**回补**被当成「空仓下的首笔
买入」直接取价，成本基准被拉到 0.094064，低于 09-28 的平仓价 0.09619 —— **−24.51 被算
成 +21.53831957**。修复把回放改成带符号仓位（见 `executor.compute_realized_pnl`），但这个
命令要解决的是**修复之后**的问题：库里那些已经写坏的行不会自己变好。

## 口径：与线上**完全同一条**代码路径

重算直接调 `compute_realized_pnl`，并且历史查询的过滤条件与
`OrderExecutor._compute_close_pnl` 逐项一致——**只有一样不同：这里多一层
`created_at__lt=本单时刻`**。

那一层不是在「修正口径」，而是在**补一个线上本来就有的隐含前提**。线上是即时算的：
本单成交那一刻它还没落库（历史查询带 `.exclude(id=order_id)`），所以线上历史天然只含
本单之前的成交。事后重算时本单已经在库里了，不加时间窗就会把**本单自己**和**本单之后**
的成交都拉进回放——实测这一条会让 14 笔卖单里的 12 笔得出与线上不同的数。所以带时间窗
的重算才是「当时那一刻本会算出的值」。

## 只改哪些行

判据是「重算结果与库中**不同**」，不是「看起来不对」。幂等之处就在这里：被修复覆盖不到
的历史（比如 2026-09-20 之前那些）重算后逐字节等于库中值，第二遍跑会全部跳过。修完的
代码再跑一次这个命令是空操作。

**`realized_pnl` 由 `None` 变成数字的行也会改写**——修复前返回 `None` 的路径比现在多
（历史里有多头仓位、但回放中被某个分支漏掉的情形），那类行此前**根本没进会话权益**。

## 权益按「本次重算的增量」调整，不做全量重设

直觉上应该把会话权益直接设成 `initial_capital + Σ(重算后的 realized_pnl)`，但**两条**
理由都指向不要这么做：

一、**精度**。权益是 2 位小数、而 `realized_pnl` 是 8 位；线上是逐笔 `equity += pnl`
（每笔落库时各舍入一次到 2 位），一次性把 Σ 加完再舍入会差一分。实测六条会话里有两条
对不上（`d278c140` 逐笔 9992.76 / 一次算完 9992.75；`caa3c4aa` 5050.00 / 5050.01）。
全量重设会把这两分钱的舍入差当成错误「修正」，而那是线上算法本身的性质，不是数据损坏。

二、**口径**。`caa3c4aa` 的 `initial_capital` 是 5012.97（建会话时按「账户权益的一半」
记的）而 Σ 已实现盈亏只有 37.04232；逐笔累加恰好等于库中的 5050.00（写权益的那条路
与这个 5012.97 无关），所以这里没有损坏，只是 `initial_capital + Σ` 这个式子本身在
这条会话上不成立。拿它当重设公式会引入 0.01232 的假差值。那是另一个口径问题
（「建会话时权益该不该归零」），**不在本次修复范围内**。

所以这里只按本次改动的 Δ 调权益，其余原样保留。Δ 是逐会话累加的，与 `_apply` 改的是
同一批行，两者必然一致。

## 一个必须在库里确认、这里不猜的边界

它假设「一个账户（`exchange_account`+`symbol`）只有一条生效的实盘会话」，这与
`_compute_close_pnl` 按账户+品种取历史的做法是同一个假设；已经有**多个会话共用同一账户
同一品种**的情况（donchian 与 box_time_range_breakout 的 DOGE 成交在同一条成本链上），
此时同一批卖单会被两条会话各按自己的 `live_session_id` 认领。这不是本命令引入的——
重算与线上用的是同一条规则，所以结果与「当时那一刻」一致。但它意味着 `Σ(会话权益增量)`
**不等于**账户真实盈亏。要判断这一点，看清单里按会话分组的条数，不要在这里改口径。

## 连接纪律

长循环自带 `close_old_connections()`，且**必须在函数内导入**（模块级导入会把名字绑死在
本模块上，测试里打桩 `django.db.close_old_connections` 就到不了这里，真跑一次会掐掉
`TestCase` 事务的连接）。理由详见 `apps/regime/management/commands/recompute_regime_slices.py`
的模块 docstring。
"""

from __future__ import annotations

from decimal import Decimal

from django.core.management.base import BaseCommand

from apps.trading.executor import compute_realized_pnl
from apps.trading.models import LiveSession, Order

#: 每多少条断一次旧连接。与其他长循环命令的 BATCH_SIZE 同一个意图。
BATCH_SIZE = 50


def _recompute(order, history: list[dict]) -> Decimal | None:
    """重算一笔卖单的已实现盈亏（与 `_compute_close_pnl` 同一算法）。"""
    return compute_realized_pnl(
        history, order.side, order.filled_quantity, order.avg_fill_price
    )


class Command(BaseCommand):
    help = "按当前成本法重算历史平仓单的已实现盈亏，并同步会话权益（默认只打印）"

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="真的写库（更新 realized_pnl + 重设 current_equity）。不加则只打印清单",
        )
        parser.add_argument(
            "--session",
            default=None,
            help="只处理这条会话（LiveSession UUID）。不给则全库",
        )

    def handle(self, *args, **options):
        apply_ = options["apply"]
        session_filter = options["session"]

        from django.db import close_old_connections

        orders = (
            Order.objects.filter(
                side="sell",
                status="filled",
                avg_fill_price__isnull=False,
            )
            .exclude(filled_quantity=0)
            .order_by("created_at")
        )
        if session_filter:
            orders = orders.filter(live_session_id=session_filter)

        # 一次性把「重算要读的历史」全拉进内存。每条订单各查一次历史是 N+1（历史本身
        # 又要按账户+品种过滤），而这张表在单账户量级下面是几百行——直接全取再按
        # (account, symbol) 分组，比让数据库做几百次小查询便宜得多。
        all_rows = list(
            Order.objects.filter(
                status="filled", avg_fill_price__isnull=False
            )
            .order_by("created_at")
            .values(
                "id",
                "created_at",
                "exchange_account_id",
                "symbol",
                "side",
                "filled_quantity",
                "avg_fill_price",
                "realized_pnl",
                "live_session_id",
            )
        )
        by_key: dict[tuple, list[dict]] = {}
        for row in all_rows:
            by_key.setdefault(
                (row["exchange_account_id"], row["symbol"].upper()), []
            ).append(row)
        for rows in by_key.values():
            rows.sort(key=lambda r: r["created_at"])

        changed: list[tuple] = []
        for index, order in enumerate(orders):
            if index % BATCH_SIZE == 0:
                close_old_connections()

            key = (order.exchange_account_id, order.symbol.upper())
            history = [
                {
                    "side": r["side"],
                    "filled_quantity": r["filled_quantity"],
                    "avg_fill_price": r["avg_fill_price"],
                }
                for r in by_key.get(key, [])
                # 与线上同一条前提：只看**本单之前**的成交（线上算的时候本单还没落库）。
                # 边界与线上保持**同一个次优选择**：线上是「先筛 status=filled 再排除 id」，
                # 没有 `created_at <` 这一层，所以同一毫秒内更早写入的成交在线上是看不见的；
                # 这里用严格小于会把它算进来。两者的差别只在同毫秒成交上（真实库里
                # 最小间隔是秒级），且调整方向是「更接近真实回放」，故不复制线上的盲点。
                if r["created_at"] < order.created_at
            ]
            new = _recompute(order, history)
            old = order.realized_pnl
            same = (old is None and new is None) or (
                old is not None
                and new is not None
                and abs(old - new) < Decimal("0.00000001")
            )
            if not same:
                changed.append((order, old, new))

        self._report(changed, apply_)

        if not apply_:
            self.stdout.write(
                self.style.WARNING(
                    "空跑：一行都没写。确认清单无误后加 --apply 重跑"
                )
            )
            return

        self._apply(changed)
        self._reset_equity(changed)

    # ─── 输出 ──────────────────────────────────────────────────────────────

    def _report(self, changed: list[tuple], apply_: bool) -> None:
        if not changed:
            self.stdout.write("没有需要改写的行——库中值已与当前成本法一致")
            return

        self.stdout.write(
            f"{'改写' if apply_ else '将改写'} {len(changed)} 笔平仓单："
        )
        self.stdout.write(
            f"  {'成交时刻':<20} {'品种':<10} {'数量':>14} {'平仓价':>10} "
            f"{'库中':>16} {'重算':>16} {'Δ':>14}"
        )
        delta_sum = Decimal("0")
        for order, old, new in changed:
            old_s = "NULL" if old is None else f"{old:.8f}"
            new_s = "NULL" if new is None else f"{new:.8f}"
            delta = (new or Decimal("0")) - (old or Decimal("0"))
            delta_sum += delta
            self.stdout.write(
                f"  {order.created_at:%Y-%m-%d %H:%M:%S} {order.symbol:<10} "
                f"{order.filled_quantity:>14} {order.avg_fill_price:>10} "
                f"{old_s:>16} {new_s:>16} {delta:>14.8f}"
            )
        self.stdout.write(f"  权益合计变动：{delta_sum:.8f}")

        unassigned = [c for c in changed if c[0].live_session_id is None]
        if unassigned:
            self.stdout.write(
                self.style.WARNING(
                    f"  其中 {len(unassigned)} 笔没有 live_session_id："
                    "它们计入账户口径的盈亏，但不会进任何会话的权益"
                )
            )

        sessions = {c[0].live_session_id for c in changed if c[0].live_session_id}
        if len(sessions) > 1:
            self.stdout.write(
                "  涉及多条会话，逐条列出："
            )
            for sid in sorted(sessions, key=str):
                n = sum(1 for c in changed if c[0].live_session_id == sid)
                self.stdout.write(f"    {sid}：{n} 笔")

    # ─── 写库 ──────────────────────────────────────────────────────────────

    def _apply(self, changed: list[tuple]) -> None:
        for order, _old, new in changed:
            Order.objects.filter(id=order.id).update(realized_pnl=new)
        self.stdout.write(
            self.style.SUCCESS(f"已更新 {len(changed)} 笔 realized_pnl")
        )

    def _reset_equity(self, changed: list[tuple]) -> None:
        """把受影响的会话权益按「本次重算的增量」调整。

        **不做全量重设。** 权益的全量重设要满足「`current_equity - initial_capital`
        严格等于 Σ(该会话 `realized_pnl`)」，而库里有的会话不满足：`caa3c4aa` 的
        `initial_capital` 是 5012.97（建会话时按「账户权益的一半」记的），权益
        5050.00，Σ已实现盈亏 37.04232 —— 差 **0.01232**，与盈亏无关，来自建会话时的
        初始资金本身。（不是 12.96：那个数来自把 5012.97 误当成求和基数的算术错误。）
        全量重设会把这 0.01232 当成错误抹掉，而它是「建会话时权益该不该归零」的
        口径问题，不在本次修复范围内。所以这里只按本次改动的 Δ 调权益，其余原样保留。
        另一条不能全量重设的理由是精度：权益 2 位小数、`realized_pnl` 8 位，线上是
        逐笔各舍入一次，一次性算完 Σ 再舍入会差一分（六条会话里有两条如此）。

        Δ 是逐会话累加的，与 `_apply` 改的是同一批行，两者必然一致。
        """
        deltas: dict[str, Decimal] = {}
        for order, old, new in changed:
            sid = order.live_session_id
            if sid is None:
                continue
            deltas[sid] = deltas.get(sid, Decimal("0")) + (
                (new or Decimal("0")) - (old or Decimal("0"))
            )

        if not deltas:
            return

        for sid, delta in deltas.items():
            if delta == 0:
                continue
            session = (
                LiveSession.objects.select_related("strategy")
                .filter(id=sid)
                .first()
            )
            if session is None:
                self.stdout.write(
                    self.style.WARNING(
                        f"  交易单上的会话 {sid} 在库里不存在，权益未调整"
                    )
                )
                continue
            target = session.current_equity + delta
            LiveSession.objects.filter(id=sid).update(current_equity=target)
            self.stdout.write(
                self.style.SUCCESS(
                    f"会话 {sid}（{session.strategy.name}）权益 "
                    f"{session.current_equity} → {target}（Δ {delta:+.8f}）"
                )
            )
