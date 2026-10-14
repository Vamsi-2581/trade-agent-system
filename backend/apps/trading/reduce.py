"""减仓的**机械层**：账户维度的枚举挂单 / 撤单 / 市价只减不增下单（第②段单元 ②d）。

## 这一层回答什么、不回答什么

只回答「怎么在**某一个交易所账户**上把事做出来」：枚举挂单、撤掉开仓意图的挂单、
按给定的数量下单、取中间价。**不回答**「该减哪个账户、该减多少、是第几次、要不要
告警、通知谁」——那些是策略层（``apps/regime/reduce_run.py``）的事。

分层的理由与 ``deactivation.py`` / ``deactivation_run.py`` 同款：机械层不碰机制状态，
所以它可以被单独测（一个假适配器就能把撤单与分片的每条分支走完），而策略层的每一行
都要在「有事件、有账户、有持仓」的世界里才有意义。

## 为什么不用 ``OrderExecutor``

``OrderExecutor.cancel_order`` / ``get_positions`` / ``get_balance`` 现在也一律按**账户 id**
解析适配器（2026-10-12 收口，见 ``OrderExecutor.resolve_adapter``），所以「拿错账户」不再是
理由。这一层仍然自己构造适配器的理由是**它不需要一个跑着的框架**：机械层要能被单独测
（一个假适配器走完撤单与分片的每条分支），而 ``OrderExecutor`` 是随 FrameManager 懒加载的
单例，取不到就什么都做不了。**减仓的目标是账户**（ADR 0003 / CONTEXT.md:117），所以这一层
一律按 ``ExchangeAccount`` 构造适配器，走 ``pending_reconcile`` 已经铺好的那条路
（``build_account_adapter``）。

## ``reduce_only`` 与 ``client_order_id`` 是同级硬前置（CONTEXT.md:128）

每张子单都带 ``reduce_only=True``，且带一个**确定的** ``client_order_id``：

* ``reduce_only`` 由交易所强制只减不增。本地没有持仓表，按本地账算出的「一半」可能
  大于实际持仓，一次「减仓」就会反向开仓、把敞口加大——熔断动作自己制造风险。
* ``client_order_id`` 是重发的锚。一张子单失败后拿**同一个** id 再发，交易所会据它
  去重（币安返回重复单号，适配器据此对账），所以重发不会变成第二张单。这正是
  CONTEXT.md:125 说的「重发不消耗新的幂等配额」在传输层的落点：配额是本地记的，
  重发连本地那一层都不进——它压根不新开 ``Order`` 行。

## ``unfinished`` 是唯一会让人停下来的一种结果

一张子单**没有结论**（发满了允许的次数仍无结论，或已受理但追问不到终态——后者可能有一张
活单挂在交易所），这时剩下的分片一律**不再发**：那张活单可能已经成交了一部分，继续发就是
减过头。宁可少减（事后有人看到告警去补），不可多减（没有任何自动路径能补回来，
CONTEXT.md:124 没有「补回」）。

## 「已受理」不是「有结论」

下单应答是**受理时刻的快照**，不是成交结果：币安对市价单返回的是 ``status=NEW``、
``executedQty=0``、``avgPrice=0``（那个 ``avgPrice="0"`` 占位符在 ``_price`` 的
docstring 里早就写着了，只是当时没把同一件事推广到 ``status``）。所以一张子单在拿到应答
之后还要**追问终态**（``SETTLE_MAX_QUERIES``）：只有**终态**才配得上 ``filled`` /
``partial`` / ``rejected`` 这三种「有结论」的形态，追问不到就是 ``unfinished``。

把受理当结论的代价不是记录难看，是**账本上那句假话**。2026-09-26 实测即为此形：testnet 上
AVAXUSDT 减 21 张**全额成交** @10.9150，而这一层给出的结论是 ``rejected``（「确定没成交」）。
原先那条路径只有一个判据 ``filled <= 0``，而受理应答恒为 0。

假 ``rejected`` 顺着唯一调用方（``reduce_run._execute_symbol``）流下去是一串**都写着「没成交」
的动作**：``:1276`` 的 ``break`` 让剩余分片不再发（它信了「确定没成交」）、``filled`` 求和为 0、
记录落 ``ReduceStatus.FAILED`` =「减仓失败（确定未成交）」；而 ``qty_after`` 照读（``unfinished``
才跳过），于是账本里「确定没成交」与「余量确实变小了」并排躺着，一次**真的减了**的减仓被记成
一次失败。更要紧的是 ``ShardResult.settled`` 的语义就是「可以继续发下一片」——``rejected`` 属于
这一档，任何**将来**按它判断要不要接着发的调用方都会减过头，而减过头没有任何自动路径能补回来。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field, replace
from decimal import Decimal

from .adapters import (
    BaseExchangeAdapter,
    OrderFill,
    OrderRequest,
    OrderResponse,
)

logger = logging.getLogger(__name__)

#: 一张子单允许的**发送次数**：首次 + ≤2 次重发（CONTEXT.md:125「≤2 次重发」）。
#: 重发用的 ``client_order_id`` 与首次**逐字相同**，所以这个数不产生新的幂等配额。
SHARD_MAX_SENDS = 3

#: 子单结果的四种形态。前三种是**有结论**的，第四种不是。
SHARD_STATUS_FILLED = "filled"  # 全部成交
SHARD_STATUS_PARTIAL = (
    "partial"  # 部分成交且**已了结**（余量已被交易所撤掉，不会再成交）
)
SHARD_STATUS_REJECTED = "rejected"  # **确定**没成交（终态 + 成交量为 0）
SHARD_STATUS_UNFINISHED = "unfinished"  # 没有结论：**可能有一张活单**

#: 「有结论」的三种（可以继续发下一片）。
_SETTLED_STATUSES = (SHARD_STATUS_FILLED, SHARD_STATUS_PARTIAL, SHARD_STATUS_REJECTED)

#: 一张子单**已受理**之后，向交易所追问终态的**次数**与**间隔**（秒）。
#:
#: 为什么非追问不可，见模块 docstring「已受理不是有结论」。市价单在交易所侧是毫秒级撮合完
#: 的，而应答是受理时刻的快照，所以追问一两次就能拿到终态；追问不到就是 ``unfinished``。
SETTLE_MAX_QUERIES = 3
SETTLE_QUERY_INTERVAL_SECONDS = 0.4

#: 交易所报出的**终态**。不在这里的一律读成「还没结束」——**包括空字符串与没见过的状态**。
#: fail-closed 的方向是「追问」而不是「挑一个结论」：猜成 ``rejected`` 会让调用方以为这一片
#: 没发出去、继续往下发，而那张单可能正在成交。
#:
#: 两套拼写都要认，因为这一层会拿到两种应答：下单与对账给的是**交易所原始状态**
#: （``_order_response_from_raw``），而 ``fetch_order`` 给的是适配器 ``_STATUS_MAP``
#: **映射后的本地状态**（``OrderFill``）。
_TERMINAL_STATUSES = frozenset(
    {
        # 交易所原始状态
        "filled",
        "canceled",
        "cancelled",
        "expired",
        "expired_in_match",
        "rejected",
        # fetch_order 映射后的本地状态（REJECTED → failed，见 binance._STATUS_MAP）
        "failed",
    }
)


def normalize_symbol(symbol: str) -> str:
    """交易所口径的品种名（``BTC/USDT`` → ``BTCUSDT``）。

    与 ``binance.py`` 内部逐字同款。**两边必须同时改**：这个函数存在的唯一理由是
    「事件库用 ``BASE/QUOTE``、交易所返回 ``BTCUSDT``」，两个口径之间需要一个换算口，
    而不是让每个调用点各写一遍 ``replace("/", "")``。
    """
    return (symbol or "").upper().replace("/", "")


def is_opening_order(side: str, position_side: str | None) -> bool:
    """一张挂单是不是**开仓意图**（CONTEXT.md:122 的判据）。

    「与当前持仓方向相反」= 开仓；**没有持仓则所有挂单都是开仓意图**——一个空仓上的
    买单在开多、卖单在开空，两张都是「窗口开启后敞口会变大」的来源，一张都不能留。
    """
    side = (side or "").lower()
    if side not in ("buy", "sell"):
        # 认不出的方向**按开仓处理**（fail-closed）：漏撤一张开仓单的代价是窗口里敞口
        # 可能变大，误撤一张平仓单的代价是那张单本来就在减仓——前者更贵。
        return True
    if position_side not in ("long", "short"):
        return True
    return not (
        (position_side == "long" and side == "sell")
        or (position_side == "short" and side == "buy")
    )


# --------------------------------------------------------------------------- #
# 下单
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ShardOrder:
    """一条减仓子单的**请求**（与执行结果无关，所以可先构造、可先落库）。"""

    client_order_id: str
    symbol: str
    side: str  # 'sell' 平多 / 'buy' 平空
    quantity: Decimal

    def as_dict(self) -> dict:
        return {
            "client_order_id": self.client_order_id,
            "symbol": self.symbol,
            "side": self.side,
            "quantity": str(self.quantity),
        }


@dataclass(frozen=True)
class ShardResult:
    """一条子单的最终结果。

    ``status == "unfinished"`` 时 ``exchange_order_id`` 可能为空（从没拿到过单号），
    ``filled_qty`` 反映**已知**的成交量——它不是「没成交」，是「不知道成交了多少」。
    这两种在文字上必须分开，所以这一层不把 ``unfinished`` 转成任何一种确定状态。已受理但
    追问不到终态也走这一档，此时 ``filled_qty`` 与 ``avg_price`` 可能都是**真的**（受理应答
    说 0 只是「此刻为止」），而 ``status`` 仍然是「不知道」——两者不矛盾。
    """

    client_order_id: str
    symbol: str
    side: str
    requested_qty: Decimal
    filled_qty: Decimal
    avg_price: Decimal | None
    status: str
    sends: int
    exchange_order_id: str = ""
    error: str = ""

    @property
    def settled(self) -> bool:
        return self.status in _SETTLED_STATUSES

    def as_dict(self) -> dict:
        return {
            "client_order_id": self.client_order_id,
            "symbol": self.symbol,
            "side": self.side,
            "requested_qty": str(self.requested_qty),
            "filled_qty": str(self.filled_qty),
            "avg_price": str(self.avg_price) if self.avg_price is not None else None,
            "status": self.status,
            "sends": self.sends,
            "exchange_order_id": self.exchange_order_id,
            "error": self.error,
        }


def _retryable(exc: Exception) -> bool:
    """这次失败值不值得拿**同一个** ``client_order_id`` 再发一次。

    参数类错误（``ValueError``：数量取整为 0 / 低于 ``minQty`` / 规则表里没这个品种）
    **不重发**：同一组参数配同一个单号，再发两次必然得到同一个拒绝，只是把一次失败
    记成三次。其余一律重发——「不知道有没有发出去」的出路恰恰就是拿同一个单号再发一次
    （适配器在重复单号上会去对账，见 ``binance._reconcile_order``）。
    """
    return not isinstance(exc, ValueError)


async def place_reduce_shard(
    adapter: BaseExchangeAdapter, order: ShardOrder
) -> ShardResult:
    """下一张**只减不增**的市价子单，失败时按 ``SHARD_MAX_SENDS`` 用同一个单号重发。

    这里**只承诺一件事**：``ShardResult`` 说的是这张子单的下场。至于「这一片该不该发」
    「发完之后还发不发下一片」，是调用方的事（见模块 docstring 关于 ``unfinished``）。

    市价单：滑点上限（CONTEXT.md:129）管的是「这一轮减仓整体的加权成交价」，不是单张
    子单的限价，所以子单不需要价格——挂限价反而会给「窗口里减不掉」造一个新的失败模式。

    拿到应答**不等于**拿到结论：应答是受理时刻的快照，所以成功分支还要追问终态
    （``_settle_order``）。追问与重发是两条路：一张子单可能走三次**发送**，但那只发生在
    下单应答**不明**（抛异常）时；追问走的是查询，不占发送次数。
    """
    last_error = ""
    sends = 0
    for attempt in range(1, SHARD_MAX_SENDS + 1):
        sends = attempt
        request = OrderRequest(
            exchange="",  # 适配器已经定好了是哪家，这里只用于日志（不用名解析账户）
            symbol=order.symbol,
            order_type="market",
            side=order.side,
            quantity=order.quantity,
            # 两个同级硬前置（CONTEXT.md:128）：只减不增 + 确定的重发锚点。
            client_order_id=order.client_order_id,
            reduce_only=True,
        )
        try:
            response = await adapter.place_order(request)
        except Exception as exc:  # noqa: BLE001 — 每一类都要变成一条留痕，不能漏出
            last_error = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "[Reduce] 子单失败 client_order_id=%s 第 %s/%s 次：%s",
                order.client_order_id,
                attempt,
                SHARD_MAX_SENDS,
                last_error,
            )
            if not _retryable(exc):
                # 参数被拒 —— 确定没成交，所以是**有结论**的失败。
                return ShardResult(
                    client_order_id=order.client_order_id,
                    symbol=order.symbol,
                    side=order.side,
                    requested_qty=order.quantity,
                    filled_qty=Decimal("0"),
                    avg_price=None,
                    status=SHARD_STATUS_REJECTED,
                    sends=sends,
                    error=last_error,
                )
            continue

        # 「已受理」不是「有结论」：先把它追到终态，追不到就如实说不知道。
        status, known, error = await _settle_order(adapter, response, order)
        return ShardResult(
            client_order_id=order.client_order_id,
            symbol=order.symbol,
            side=order.side,
            requested_qty=order.quantity,
            filled_qty=known.filled_qty,
            avg_price=known.avg_price,
            status=status,
            sends=sends,
            exchange_order_id=known.exchange_order_id,
            error=error,
        )

    # 发满了还没结论。**绝不降级成 rejected**：rejected 的语义是「确定没成交」，
    # 而这里的语义恰恰是「不知道」——一张活单可能正挂在交易所上。
    logger.error(
        "[Reduce] 子单发满 %s 次仍无结论 client_order_id=%s：%s",
        SHARD_MAX_SENDS,
        order.client_order_id,
        last_error,
    )
    return ShardResult(
        client_order_id=order.client_order_id,
        symbol=order.symbol,
        side=order.side,
        requested_qty=order.quantity,
        filled_qty=Decimal("0"),
        avg_price=None,
        status=SHARD_STATUS_UNFINISHED,
        sends=sends,
        error=last_error,
    )


def _price(value) -> Decimal | None:
    """成交价：``None`` 或非正数一律读成**没有这个数**。

    与 ``fetch_mid_price`` 同一条纪律（绝不许把取不到的价格写成 0）：0 在这里的意思是
    「这一笔成交的价格是 0」，那是一句假话，而它会被写进留痕（``ShardResult.as_dict``
    落进幂等记录的 ``sub_orders``）。币安在「已受理但还没成交」时正是拿 ``avgPrice="0"``
    当占位符，所以这条路径不是假想的。``volume_weighted_price`` 本来就跳过非正的成交价，
    这里只是让**记录**与那个事实一致。
    """
    if value is None:
        return None
    try:
        price = Decimal(str(value))
    except Exception:  # noqa: BLE001 — 解析不了就是没有这个数
        return None
    return price if price > 0 else None


def _is_terminal(raw_status: str) -> bool:
    """这个状态是不是**终态**（订单已经不会再动了）。

    不认得的（含空字符串）一律**不是**终态。这条 fail-closed 的代价只是一次追问，而猜错的
    代价是给出一句假的「有结论」——见 ``_TERMINAL_STATUSES``。
    """
    return (raw_status or "").strip().lower() in _TERMINAL_STATUSES


def _conclude(
    raw_status: str, filled: Decimal | None, requested: Decimal
) -> str | None:
    """**终态** + 一份**读得通**的成交量 → 三种有结论的形态之一；否则 ``None``。

    ``None`` 的意思是「这份应答不配当结论」，两条来路，都是「宁可说不知道，也不说一句自己
    都不信的话」：

    * **成交量为 ``None``**（这个应答形状里根本没有成交量这个字段）。它与「成交量为 0」必须
      分开——``0`` 的含义是「确定没成交」，而 ``None`` 只是**没读到数**。``_decimal`` 会把
      ``None`` 兜成 0，所以这个区分只能在**进** ``_decimal`` 之前做：放过去，就等于让一次
      字段改名/换形状重演 2026-09-26 那个 bug（终态 + 读成 0 → ``rejected``）。
    * **状态与数量互相矛盾**：终态写着 ``filled``、成交量却是 0。两个字段里可疑的通常是数量
      ——``_order_response_from_raw`` 里那句 ``Decimal(data.get("executedQty", "0"))`` 正是
      「字段不在就落 0」。拿它去坐实「确定没成交」，会把一次适配器映射 bug 洗成一条静默的假账。
    """
    if filled is None:
        return None
    status = (raw_status or "").strip().lower()
    if status == "filled" and filled <= 0:
        return None
    if filled <= 0:
        return SHARD_STATUS_REJECTED
    if status == "filled" or filled >= requested:
        return SHARD_STATUS_FILLED
    return SHARD_STATUS_PARTIAL


@dataclass(frozen=True)
class _Known:
    """「最后一份已知的交易所应答」的公共投影，只留 ``ShardResult`` 要用的四个字段。

    两种应答形状都要能读：下单与对账给 ``OrderResponse``（``status`` 是**交易所原始状态**，
    成交字段叫 ``filled_qty`` / ``avg_price``），``fetch_order`` 给 ``OrderFill``
    （``status`` 已**映射成本地状态**，字段叫 ``filled_quantity`` / ``avg_fill_price``）。
    两套名字在这里收口一次，别的地方就不必知道这件事。

    ``filled_qty`` 是可空的，**这不是为了省事**：``None`` 表示「这份应答里没有成交量这个
    字段」，与 ``Decimal("0")``（「成交了 0 张」）是两件事。``_decimal`` 的默认值会把前者
    变成后者，所以那一步必须由 ``of`` 拦在这里——见 ``_conclude``。
    """

    status: str
    filled_qty: Decimal | None
    avg_price: Decimal | None
    exchange_order_id: str

    @classmethod
    def of(
        cls, response: OrderResponse | OrderFill, *, fallback_order_id: str = ""
    ) -> "_Known":
        filled = getattr(response, "filled_qty", None)
        if filled is None:
            filled = getattr(response, "filled_quantity", None)
        avg = getattr(response, "avg_price", None)
        if avg is None:
            avg = getattr(response, "avg_fill_price", None)
        return cls(
            status=str(getattr(response, "status", "") or ""),
            filled_qty=None if filled is None else _decimal(filled),
            avg_price=_price(avg),
            # ``OrderFill`` 上没有单号，追问时得把原来的单号带下来。
            exchange_order_id=(
                str(getattr(response, "exchange_order_id", "") or "")
                or fallback_order_id
            ),
        )


def _merged(known: _Known, newer: _Known) -> _Known:
    """把追问回来的应答并进已知：**只增不减**。

    一张单的累计成交量是单调不减的，所以「后看到的比先看到的少」只可能是两个字段里有一个
    读错了，不是一次回撤。丢掉已经观测到的那个数，就可能让一份读坏的 0 反过来坐实「确定没
    成交」；反之取大的那个，最坏也只是把一次成交算进去（方向保守，正是本模块要的那一侧）。
    成交均价与单号同理：新应答没给不代表先前那个事实不算数了。
    """
    if known.filled_qty is None:
        filled = newer.filled_qty
    elif newer.filled_qty is None:
        filled = known.filled_qty
    else:
        filled = max(known.filled_qty, newer.filled_qty)
    return replace(
        newer,
        filled_qty=filled,
        avg_price=(newer.avg_price if newer.avg_price is not None else known.avg_price),
        exchange_order_id=newer.exchange_order_id or known.exchange_order_id,
    )


def _conclude_or_untrusted(known: _Known, order: ShardOrder) -> tuple[str, _Known, str]:
    """**终态**的应答 → ``(结论, known, "")``；**终态但读不通** → ``(unfinished, known, 说明)``。

    读不通只有一种（见 ``_conclude``）。它**不该再追问**：交易所给的已经是终态，再问一次拿到
    的是同一份；但它也不配当结论。所以走 ``unfinished``，并把「为什么」写进 ``error``——
    ``ShardResult.as_dict`` 落进幂等记录后，那一列是事后唯一能看出「这次不是没问到，是问到了
    一份读不通的」的地方。
    """
    status = _conclude(known.status, known.filled_qty, order.quantity)
    if status is not None:
        return status, known, ""
    detail = f"终态 {known.status!r} 与成交量 {known.filled_qty!r} 读不通，不予采信"
    logger.error("[Reduce] 子单%s client_order_id=%s", detail, order.client_order_id)
    return SHARD_STATUS_UNFINISHED, known, detail


async def _settle_order(
    adapter: BaseExchangeAdapter,
    response: OrderResponse,
    order: ShardOrder,
) -> tuple[str, _Known, str]:
    """把「已受理」追到「有结论」；追不到就如实说 ``unfinished``。

    这是本模块**唯一**一处不把下单应答当结论的地方（理由见 ``SETTLE_MAX_QUERIES``）。
    追问走 ``fetch_order``（按交易所单号查），**不是重发**：重发会多开一张单，而这里要问的
    恰恰是「刚才那张单怎么了」。

    全程**不会抛**：``_Known.of`` / ``_merged`` / ``_conclude`` 都是全函数，``fetch_order``
    的异常就地收成「没问到」。这是必要的——``place_reduce_shard`` 承诺「每张子单都有下场」，
    而从这里漏出去的异常会被调用方 ``reduce_run`` 落成 ``ReduceStatus.FAILED`` =
    「减仓失败（确定未成交）」，那正是本次要消灭的那句假话，只不过从异常的边门进来。

    Returns:
        ``(status, known, error)``。``known`` 是最后一份已知应答，``ShardResult`` 的成交量与
        成交价都从它取——追问不到时它是受理应答，那时的 0 是**已知的事实**（「此刻为止没有
        成交」），而**不是**「确定没成交」：后者只有终态才配得上。
    """
    known = _Known.of(response)

    if _is_terminal(known.status):
        return _conclude_or_untrusted(known, order)

    if not known.exchange_order_id:
        # 受理了却没有单号 = 无从追问。不猜，如实说不知道。
        detail = "已受理但应答里没有单号，无法追问终态"
        logger.error(
            "[Reduce] 子单%s client_order_id=%s 状态=%r",
            detail,
            order.client_order_id,
            known.status,
        )
        return SHARD_STATUS_UNFINISHED, known, detail

    last_error = ""
    for _ in range(SETTLE_MAX_QUERIES):
        await asyncio.sleep(SETTLE_QUERY_INTERVAL_SECONDS)
        try:
            fill = await adapter.fetch_order(known.exchange_order_id, order.symbol)
        except Exception as exc:  # noqa: BLE001 — 追问不到就是「没结论」，不是「没成交」
            last_error = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "[Reduce] 追问子单终态失败 client_order_id=%s order_id=%s：%s",
                order.client_order_id,
                known.exchange_order_id,
                last_error,
            )
            continue
        known = _merged(
            known, _Known.of(fill, fallback_order_id=known.exchange_order_id)
        )
        if _is_terminal(known.status):
            return _conclude_or_untrusted(known, order)

    logger.error(
        "[Reduce] 子单追问 %s 次仍无终态 client_order_id=%s order_id=%s 状态=%r：%s",
        SETTLE_MAX_QUERIES,
        order.client_order_id,
        known.exchange_order_id,
        known.status,
        last_error,
    )
    return SHARD_STATUS_UNFINISHED, known, last_error


def _decimal(value) -> Decimal:
    """``None`` / 字符串 / float → ``Decimal``；一律以 0 兜底。

    取不到的数一律落成 ``Decimal("0")``，由**用它的地方**决定 0 是什么意思：成交量 0
    就是没成交，成交价 0 在 ``volume_weighted_price`` 里等于「这一张不参与加权」。放在
    这里判，就得给每个字段各配一套「0 算不算有值」的规则——同一个 0 两种含义正是
    ``fetch_mid_price`` 那条纪律（绝不许返回 0）在防的事。
    """
    if value is None:
        return Decimal("0")
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001 — 解析不了就是没有这个数
        return Decimal("0")


# --------------------------------------------------------------------------- #
# 枚举挂单 / 撤单
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class OpenOrderInfo:
    """交易所侧一张还挂着的单。"""

    exchange_order_id: str
    symbol: str
    side: str
    quantity: Decimal
    filled: Decimal

    def as_dict(self) -> dict:
        return {
            "exchange_order_id": self.exchange_order_id,
            "symbol": self.symbol,
            "side": self.side,
            "quantity": str(self.quantity),
            "filled": str(self.filled),
        }


@dataclass
class EnumerationReport:
    """一次挂单枚举的结果。

    ``failed`` 记的是**没枚举成的品种**（``symbol -> 错误``）。它必须与「枚举成了、
    结果为空」严格分开：空列表的含义是「确实一张都没有」，而没枚举成的含义是
    「可能有一堆」。混在一起，一次网络抖动就会被读成「敞口没有增加的风险」。
    """

    orders: list[OpenOrderInfo] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        return not self.failed

    def as_dict(self) -> dict:
        return {
            "orders": [o.as_dict() for o in self.orders],
            "failed": dict(self.failed),
        }


@dataclass(frozen=True)
class CancelOutcome:
    """一张挂单的撤销下场。"""

    order: OpenOrderInfo
    cancelled: bool
    error: str = ""

    def as_dict(self) -> dict:
        return {
            "exchange_order_id": self.order.exchange_order_id,
            "symbol": self.order.symbol,
            "side": self.order.side,
            "quantity": str(self.order.quantity),
            "cancelled": self.cancelled,
            "error": self.error,
        }

    def line(self) -> str:
        """一句话，**未撤掉的必须点名**（CONTEXT.md:122）。"""
        head = f"{self.order.symbol} {self.order.side} {self.order.quantity}"
        if self.cancelled:
            return f"已撤：{head}"
        return f"**未撤下**：{head}（{self.error or '交易所未确认'}）"


@dataclass
class CancelReport:
    """一次撤单动作的全貌（含枚举那一半）。"""

    enumeration: EnumerationReport = field(default_factory=EnumerationReport)
    outcomes: list[CancelOutcome] = field(default_factory=list)

    @property
    def cancelled(self) -> list[CancelOutcome]:
        return [o for o in self.outcomes if o.cancelled]

    @property
    def failed(self) -> list[CancelOutcome]:
        return [o for o in self.outcomes if not o.cancelled]

    def as_dict(self) -> dict:
        return {
            "enumeration": self.enumeration.as_dict(),
            "outcomes": [o.as_dict() for o in self.outcomes],
        }

    def lines(self) -> list[str]:
        """给人看的那几句。

        「本次未能枚举挂单，敞口在窗口内仍可能增加」是**必写**的一句（CONTEXT.md:122），
        且它不是错误信息而是**事实陈述**：枚举失败不阻拦减仓，但它改变的是「现在的敞口
        还会不会变大」这个判断，所以必须说出来，而不是躲在日志里。
        """
        lines: list[str] = []
        if not self.enumeration.complete:
            detail = "；".join(
                f"{symbol}: {error}"
                for symbol, error in sorted(self.enumeration.failed.items())
            )
            lines.append(f"**本次未能枚举挂单，敞口在窗口内仍可能增加**（{detail}）")
        if not self.enumeration.orders:
            if self.enumeration.complete:
                lines.append("挂单：交易所侧没有开仓意图的挂单。")
            return lines
        lines.append(f"挂单：发现 {len(self.enumeration.orders)} 张开仓意图的挂单。")
        lines.extend("  " + o.line() for o in self.outcomes)
        return lines


async def enumerate_open_orders(
    adapter: BaseExchangeAdapter,
    symbols: list[str],
    *,
    position_side: dict[str, str | None] | None = None,
) -> EnumerationReport:
    """逐品种枚举挂单，只留下**开仓意图**的那些。

    枚举源是**交易所侧**（``fetch_open_orders``），不是本地 ``Order`` 表：本地表只知道
    自己下过什么，撤不到别处挂的单。适配器的 ``fetch_open_orders`` 在拿不到时**抛异常**
    而不是返回空列表（见 ``binance.py``），所以「确实没有」与「没问出来」在这里天然
    是两件事，只需要如实传下去。

    一个品种失败不影响其余品种：窗口里能撤几张是几张。
    """
    position_side = position_side or {}
    report = EnumerationReport()
    for symbol in symbols:
        try:
            rows = await adapter.fetch_open_orders(symbol)
        except Exception as exc:  # noqa: BLE001 — 枚举失败必须变成一条记录，不能吞
            report.failed[symbol] = f"{type(exc).__name__}: {exc}"
            logger.warning("[Reduce] 挂单枚举失败 symbol=%s：%s", symbol, exc)
            continue
        side = position_side.get(normalize_symbol(symbol))
        for row in rows or []:
            if not is_opening_order(getattr(row, "side", ""), side):
                continue
            symbol_out = normalize_symbol(getattr(row, "symbol", "") or symbol)
            report.orders.append(
                OpenOrderInfo(
                    exchange_order_id=str(getattr(row, "exchange_order_id", "") or ""),
                    symbol=symbol_out,
                    side=str(getattr(row, "side", "") or "").lower(),
                    quantity=_decimal(getattr(row, "quantity", None)),
                    filled=_decimal(getattr(row, "filled_qty", None)),
                )
            )
    return report


async def cancel_opening_orders(
    adapter: BaseExchangeAdapter,
    symbols: list[str],
    *,
    position_side: dict[str, str | None] | None = None,
) -> CancelReport:
    """撤掉**开仓意图**的挂单，逐张、串行。

    **先撤后减，串行，同一个任务里**（CONTEXT.md:122）。并行会让「撤单还没回来就先减仓」
    变成一个可能的顺序，而那个顺序下减仓可能撞上一张正在成交的开仓单。

    **撤单失败不阻拦减仓**：失败进报告、在通知里点名，减仓照常走。总枚举失败同理——
    它的后果是「敞口可能还会变大」这句话必须出现，而不是「所以这次不减了」：熔断里
    少减一半的代价，比多留一会儿挂单的代价大得多。
    """
    report = CancelReport(
        enumeration=await enumerate_open_orders(
            adapter, symbols, position_side=position_side
        )
    )
    for info in report.enumeration.orders:
        try:
            ok = await adapter.cancel_order(info.exchange_order_id, info.symbol)
        except Exception as exc:  # noqa: BLE001
            report.outcomes.append(
                CancelOutcome(
                    order=info, cancelled=False, error=f"{type(exc).__name__}: {exc}"
                )
            )
            logger.warning(
                "[Reduce] 撤单失败 order=%s symbol=%s：%s",
                info.exchange_order_id,
                info.symbol,
                exc,
            )
            continue
        if not ok:
            report.outcomes.append(
                CancelOutcome(order=info, cancelled=False, error="交易所未确认撤销")
            )
            continue
        report.outcomes.append(CancelOutcome(order=info, cancelled=True))
    return report


# --------------------------------------------------------------------------- #
# 只读的两件事
# --------------------------------------------------------------------------- #


async def account_positions(
    adapter: BaseExchangeAdapter,
) -> dict[str, tuple[str, Decimal]]:
    """账户此刻的持仓：``{交易所口径品种: (方向, 数量)}``。

    **交易所是持仓的唯一真相**（本地没有持仓表）。所以减仓的「一半」只能从这里算：
    任何本地推算都是第二份持仓算术，而它与交易所分叉的那一刻，减仓就会减错。
    """
    out: dict[str, tuple[str, Decimal]] = {}
    for position in await adapter.get_positions():
        symbol = normalize_symbol(getattr(position, "symbol", ""))
        side = str(getattr(position, "side", "") or "").lower()
        quantity = _decimal(getattr(position, "quantity", None))
        if not symbol or quantity <= 0:
            continue
        out[symbol] = (side, quantity)
    return out


async def mid_price(adapter: BaseExchangeAdapter, symbol: str) -> Decimal | None:
    """滑点基准价：**动作开始时刻**的中间价（CONTEXT.md:129）。

    ``None`` = 本次不判定滑点（适配器取不到，或取到一个不成立的数）。**绝不返回 0**：
    0 的含义是「滑点完美」，而那是一条假记录，且它是减仓成本失控的唯一告警依据。
    """
    try:
        price = await adapter.fetch_mid_price(symbol)
    except Exception as exc:  # noqa: BLE001 — 取价失败就是「不判定」，不是「0」
        logger.warning("[Reduce] 取中间价失败 symbol=%s：%s", symbol, exc)
        return None
    if price is None or price <= 0:
        return None
    return price


def volume_weighted_price(results: list[ShardResult]) -> Decimal | None:
    """一组子单的**成交量加权成交均价**；一张都没成交则 ``None``。

    **只有成交了的子单参与**（CONTEXT.md:129）：把被拒的、没发出去的那些按 0 成交量
    计进来，等于拿一个不存在的成交价去平均。
    """
    total_qty = Decimal("0")
    total_value = Decimal("0")
    for result in results:
        if result.filled_qty <= 0 or result.avg_price is None or result.avg_price <= 0:
            continue
        total_qty += result.filled_qty
        total_value += result.filled_qty * result.avg_price
    if total_qty <= 0:
        return None
    return total_value / total_qty


def slippage_pct(
    reference: Decimal | None, vwap: Decimal | None, side: str
) -> Decimal | None:
    """一次减仓的滑点（**正 = 不利**），取不到基准或没有成交则 ``None``。

    方向按「不利为正」记，而不是记绝对值：一次成交得比中间价好的减仓，与一次成交得
    差的减仓，在「要不要暂停自动减仓」这一问上不是同一件事，而绝对值把它们抹平。卖出
    （平多）成交低于基准是不利，买入（平空）成交高于基准是不利。
    """
    if reference is None or vwap is None or reference <= 0:
        return None
    if (side or "").lower() == "buy":
        return (vwap - reference) / reference
    return (reference - vwap) / reference
