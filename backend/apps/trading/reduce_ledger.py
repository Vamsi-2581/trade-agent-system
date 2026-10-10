"""减仓成交的**落库层**：把 ``reduce.place_reduce_shard`` 的结论写进 ``orders`` 表。

## 为什么这一层必须存在（2026-09-28 事故）

``reduce.py`` 是刻意设计成「一次都不碰 DB / ADAPTER_MAP」的纯机械层，它返回
``ShardResult`` 就收工。于是**手动平仓与自动减仓的成交从来没有落过 ``Order`` 行**：
用户 09-28 让 LLM 平掉 BNB 空头，交易所侧两张市价买回把敞口抹平了，而本地一行记录
都没有。成本链因此卡在事故留下的 −1.37 空头上——09-30 起每一笔会话卖单回放
``compute_realized_pnl`` 都走到 ``pos <= 0`` 返回 ``None``，``realized_pnl`` 落 NULL，
``session_stats`` 又过滤掉 NULL，9 笔卖单合计约 −17.37 USDT 没进任何会话权益。

``recompute_realized_pnl`` 修不了（回放的是同一条坏历史），fill-sync 也修不了
（库里没有可同步的行）。所以修法是两层：**新发生的减仓成交当场落库**（本模块），
**历史缺口以交易所为准回填**（``reconcile_realized_pnl_from_exchange`` 命令）。

## 三条纪律

* **只落结算态**：``FILLED`` / ``PARTIAL`` 且带 ``exchange_order_id`` 才落行。
  ``UNFINISHED`` / ``REJECTED`` 不落——「未知 ≠ 失败」与 ``reduce.py`` 同款；
  unfinished 已经在 ``RegimeReduceRecord.sub_orders`` / 通知里留了痕，再落一行
  「成交 0」反而是一句假话。
* **幂等靠交易所单号**：``(exchange_account_id, exchange_order_id)`` 命中即返回既有行。
  与 ``reduce.py`` 的 ``client_order_id`` 重发去重是同一条思路的下一环。
* **符号口径必须转成斜杠**：会话单存的是 ``BNB/USDT``，而减仓子单的 ``symbol`` 是
  交易所口径 ``BNBUSDT``。不转就落在另一个成本桶里，等于把本次 bug 原样重演一遍
  （``to_session_symbol`` 就是 ``reduce.normalize_symbol`` 的反向）。

## 归属：唯一活跃（账户 + 品种）会话，否则归空

``resolve_session_for`` 只在**恰好一条**活跃会话时返回它；0 条或 >1 条一律 ``None``
（fail-closed，绝不硬挑一条）。这与 ADR 0003 不冲突——ADR 0003 说的是减仓的**目标**
是账户持仓，这里定的是盈亏的**归属**：谁下的单造成这笔敞口，盈亏就归谁。会话自己的
单本来就带 ``live_session_id``，让手动/自动平仓也带上归属，口径才自洽。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal

from apps.core.db_utils import db_async

from . import reduce

logger = logging.getLogger(__name__)

#: 已知的 quote 后缀，按**从长到短**尝试（``USDT`` 必须先于 ``USD`` 之类的短后缀）。
#: 只列交易所常见的稳定币与主流币计价对；认不出的后缀一律原样返回——猜错一个品种名
#: 的代价是它落进错的成本桶，比「没转换」更坏。
_QUOTE_SUFFIXES = (
    "USDT",
    "USDC",
    "BUSD",
    "FDUSD",
    "TUSD",
    "USDP",
    "DAI",
    "BTC",
    "ETH",
    "BNB",
    "EUR",
    "TRY",
)

#: 减仓成交行的来源标记（写进 ``Order.triggered_strategy``）。
#: 它是「这条行不是策略下的、是减仓动作落的」的唯一线索——排查成本链时先看它。
TRIGGER_MANUAL_REDUCE = "reduce_position"
TRIGGER_REGIME_REDUCE = "regime_reduce"
#: 由 ``reconcile_realized_pnl_from_exchange`` 从交易所 ``userTrades`` 回补出来的行。
TRIGGER_EXCHANGE_BACKFILL = "exchange_backfill"

#: ``ShardResult.status`` → ``Order.status``。只映射**结算态**。
_STATUS_MAP = {
    reduce.SHARD_STATUS_FILLED: "filled",
    reduce.SHARD_STATUS_PARTIAL: "partial",
}


def to_session_symbol(native: str) -> str:
    """交易所口径的品种名 → 会话口径（``BNBUSDT`` → ``BNB/USDT``）。

    ``reduce.normalize_symbol`` 的反向。**两边必须同时改**：会话单存的是 ``BASE/QUOTE``，
    减仓落库不转成同一个口径，两批成交就落在不同的成本桶里，``compute_realized_pnl``
    回放时看不到它们（2026-09-28 那类缺口的成因之一）。

    按已知 quote 后缀剥离并插入 ``/``；认不出 quote 就**原样返回**——不猜。
    """
    raw = (native or "").strip().upper()
    if not raw:
        return ""
    if "/" in raw:
        # 已经是斜杠口径，别再拆一次。
        return raw
    for quote in _QUOTE_SUFFIXES:
        if raw.endswith(quote) and len(raw) > len(quote):
            return f"{raw[: -len(quote)]}/{quote}"
    return raw


async def resolve_session_for(account_id, session_symbol: str):
    """该（账户 + 品种）上**唯一**活跃会话；0 个或 >1 个返回 ``None``。

    fail-closed 的方向是「归空」而不是「挑一条」：挑错会话的代价是盈亏记在别人的权益上，
    而且是一条**看起来正常**的假账。归空至少是诚实的——盈亏落在账户口径，不进任何会话。

    走 ``db_async``（同 ``reduce_run`` / ``executor`` 的 DB 纪律：async 上下文里不裸查询）。
    """
    if not session_symbol:
        return None

    @db_async
    def _query():
        from .models import LiveSession

        rows = list(
            LiveSession.objects.filter(
                exchange_account_id=account_id,
                symbol=session_symbol,
                status__in=LiveSession.ACTIVE_STATUSES,
            )[:2]
        )
        return rows[0] if len(rows) == 1 else None

    try:
        return await _query()
    except Exception as exc:  # noqa: BLE001 — 归属查询失败 → 归空（同 fail-closed 方向）
        logger.warning(
            "[ReduceLedger] 会话归属查询失败 account=%s symbol=%s：%s",
            account_id,
            session_symbol,
            exc,
        )
        return None


def _group_trades_by_order(trades: list[dict]) -> dict:
    """把 ``userTrades`` 逐 fill 按 ``orderId`` 聚合成「一张单一行」。**纯函数。**

    一张 orderId 可能对应多笔 fill（如 10-01 那张 4 笔）。聚合口径：

    * ``qty``：Σ 成交量；
    * ``price``：按成交量加权均价（qty 全为 0 时为 ``None``，绝不落成 0）；
    * ``realized_pnl``：Σ ``realizedPnl``（交易所权威口径）；
    * ``time``：``min(time)``（毫秒 → UTC ``datetime``），即这张单**第一笔成交**的时刻
      ——回补行要靠它排在后续卖单之前，成本链才修得回来；
    * ``side`` / ``symbol``：取该 orderId 第一笔的（同一张单方向、品种恒定）。

    交易所字段名是币安的 ``userTrades``：``orderId`` / ``qty`` / ``price`` /
    ``realizedPnl`` / ``time`` / ``side`` / ``symbol``。
    """
    out: dict = {}
    for trade in trades or []:
        order_id = str(trade.get("orderId") or "")
        if not order_id:
            continue
        qty = _decimal(trade.get("qty"))
        price = _decimal(trade.get("price"))
        pnl = _decimal(trade.get("realizedPnl"))
        ts = _trade_time(trade.get("time"))

        entry = out.get(order_id)
        if entry is None:
            out[order_id] = {
                "order_id": order_id,
                "symbol": str(trade.get("symbol") or ""),
                "side": str(trade.get("side") or "").lower(),
                "qty": qty,
                "notional": price * qty,
                "realized_pnl": pnl,
                "time": ts,
            }
            continue
        entry["qty"] += qty
        entry["notional"] += price * qty
        entry["realized_pnl"] += pnl
        if ts is not None and (entry["time"] is None or ts < entry["time"]):
            entry["time"] = ts

    for entry in out.values():
        entry["price"] = (
            entry["notional"] / entry["qty"] if entry["qty"] > 0 else None
        )
        del entry["notional"]
    return out


def _decimal(value) -> Decimal:
    """``None`` / 字符串 / float → ``Decimal``；一律以 0 兜底（同 ``reduce._decimal``）。"""
    if value is None:
        return Decimal("0")
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001 — 解析不了就是没有这个数
        return Decimal("0")


def _trade_time(value) -> datetime | None:
    """交易所的毫秒时间戳 → UTC ``datetime``；读不出返回 ``None``。"""
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


async def _fetch_exchange_pnl(adapter, native_symbol: str, order_id: str) -> Decimal | None:
    """best-effort：按 ``orderId`` 从交易所 ``userTrades`` 求 ``ΣrealizedPnl``。

    取不到（适配器不支持 / 网络失败 / 单号为空）返回 ``None``——交给回填命令兜底，
    **绝不落成 0**：0 的含义是「这一笔盈亏恰好为零」，那是一句可能为假的话。
    """
    if adapter is None or not order_id or not native_symbol:
        return None
    fetch = getattr(adapter, "fetch_user_trades", None)
    if fetch is None:
        return None
    try:
        trades = await fetch(native_symbol, from_id=0)
    except Exception as exc:  # noqa: BLE001 — 取不到就是没有这个数
        logger.warning(
            "[ReduceLedger] 交易所 userTrades 拉取失败 symbol=%s：%s",
            native_symbol,
            exc,
        )
        return None
    grouped = _group_trades_by_order(trades or [])
    entry = grouped.get(str(order_id))
    if entry is None:
        return None
    return entry["realized_pnl"]


async def persist_reduce_order(
    account,
    shard: "reduce.ShardOrder",
    result: "reduce.ShardResult",
    *,
    adapter=None,
    user_id=None,
    trigger: str = TRIGGER_MANUAL_REDUCE,
):
    """把一张减仓子单的结算结果落成 ``Order`` 行。返回新建/既有的 ``Order`` 或 ``None``。

    只处理**结算态**且带 ``exchange_order_id`` 的结果；``UNFINISHED`` / ``REJECTED``
    不落行（见模块 docstring）。幂等靠 ``(账户, 交易所单号)``。

    ``realized_pnl`` 取法：卖单优先本地 ``compute_realized_pnl``；买单或本地得 ``None``
    时 best-effort 用 ``adapter`` 按 ``orderId`` 求交易所 ``ΣrealizedPnl``；取不到落
    ``None``（交给回填命令）。

    落行后若归属到会话且 pnl 非 0，按 ``F("current_equity") + pnl`` 增量更新权益
    （复刻 ``OrderExecutor._bump_session_equity``）。
    """
    from django.db.models import F

    from .models import LiveSession, Order

    status = _STATUS_MAP.get(result.status)
    if status is None or not result.exchange_order_id:
        return None

    session_symbol = to_session_symbol(shard.symbol)
    native_symbol = reduce.normalize_symbol(shard.symbol)
    session = await resolve_session_for(account.id, session_symbol)

    @db_async
    def _existing():
        return Order.objects.filter(
            exchange_account_id=account.id,
            exchange_order_id=result.exchange_order_id,
        ).first()

    try:
        existing = await _existing()
    except Exception as exc:  # noqa: BLE001 — 查重失败也当「没有」，下面靠唯一性兜
        logger.warning("[ReduceLedger] 幂等查重失败 account=%s：%s", account.id, exc)
        existing = None
    if existing is not None:
        return existing

    # 先在内存里算好 pnl（本地或交易所），再一次性落行。
    pnl = await _compute_local_pnl_for(
        account_id=account.id,
        symbol=session_symbol,
        side=shard.side,
        filled_qty=result.filled_qty,
        avg_price=result.avg_price,
    )
    if pnl is None:
        pnl = await _fetch_exchange_pnl(adapter, native_symbol, result.exchange_order_id)

    @db_async
    def _create():
        kwargs = {
            "exchange_account_id": account.id,
            "symbol": session_symbol,
            "side": shard.side.lower(),
            "order_type": "market",
            "quantity": result.requested_qty,
            "status": status,
            "exchange_order_id": result.exchange_order_id,
            "client_order_id": result.client_order_id or "",
            "filled_quantity": result.filled_qty,
            "avg_fill_price": result.avg_price,
            "triggered_strategy": trigger[:128],
        }
        if user_id:
            kwargs["user_id"] = user_id
        if session is not None:
            kwargs["live_session_id"] = session.id
        if pnl is not None:
            kwargs["realized_pnl"] = pnl
        return Order.objects.create(**kwargs)

    try:
        order = await _create()
    except Exception as exc:  # noqa: BLE001 — 落库失败不得让调用方（已完成的成交）失败
        logger.warning(
            "[ReduceLedger] 减仓成交落库失败 account=%s order_id=%s：%s",
            account.id,
            result.exchange_order_id,
            exc,
        )
        return None

    if session is not None and pnl is not None and pnl != 0:
        try:
            @db_async
            def _bump():
                LiveSession.objects.filter(id=session.id).update(
                    current_equity=F("current_equity") + pnl
                )

            await _bump()
        except Exception as exc:  # noqa: BLE001 — 权益更新失败只记日志（行已落）
            logger.warning(
                "[ReduceLedger] 会话权益更新失败 session=%s：%s", session.id, exc
            )

    logger.info(
        "[ReduceLedger] 减仓成交已落库 account=%s symbol=%s side=%s qty=%s pnl=%s "
        "session=%s order_id=%s trigger=%s",
        account.id,
        session_symbol,
        shard.side,
        result.filled_qty,
        pnl,
        getattr(session, "id", None),
        result.exchange_order_id,
        trigger,
    )
    return order


async def _compute_local_pnl_for(
    *,
    account_id,
    symbol: str,
    side: str,
    filled_qty: Decimal,
    avg_price: Decimal | None,
) -> Decimal | None:
    """卖单用本地成本法算平仓盈亏（复用 ``executor.compute_realized_pnl``）。"""
    from .executor import compute_realized_pnl
    from .models import Order

    if side != "sell":
        return None

    @db_async
    def _history():
        return list(
            Order.objects.filter(
                exchange_account_id=account_id,
                symbol=symbol.upper(),
                status="filled",
                avg_fill_price__isnull=False,
            )
            .order_by("created_at")
            .values("side", "filled_quantity", "avg_fill_price")
        )

    try:
        history = await _history()
    except Exception as exc:  # noqa: BLE001 — 历史取不到就是算不出（交给交易所兜底）
        logger.warning("[ReduceLedger] 成本历史读取失败 account=%s：%s", account_id, exc)
        return None
    return compute_realized_pnl(history, side, filled_qty, avg_price)
