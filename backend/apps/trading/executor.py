"""
OrderExecutor - 订单执行器

职责：
1. 接收 Agent 下单指令
2. 调用 RiskGuard 前置校验（必须通过才能下单）
3. 通过交易所适配器执行订单
4. 写入 orders 表 + 更新持仓缓存

生命周期由 FrameManager 管理（initialize/shutdown）。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from decimal import Decimal
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from apps.trading.models import Order

from apps.core.db_utils import db_async
from .adapters import (
    ADAPTER_MAP,
    BaseExchangeAdapter,
    OrderNotFoundError,
    OrderPlacementUnknown,
    OrderRequest,
)
from .pending_reconcile import (
    get_fernet,
    is_beyond_placement_window,
    sweep_dangling_orders,
)

if TYPE_CHECKING:
    from apps.riskguard.guard import RiskGuard
    from apps.trading.models import Order

logger = logging.getLogger(__name__)


def _to_bytes(value):
    """将 memoryview/bytearray 等类型统一转为 bytes，供 Fernet 解密使用。"""
    if isinstance(value, (bytes, str)):
        return value
    if isinstance(value, (memoryview, bytearray)):
        return bytes(value)
    return None


def compute_realized_pnl(
    history: list[dict],
    side: str,
    filled_quantity: Decimal,
    avg_fill_price: Decimal | None,
) -> Decimal | None:
    """用移动平均成本法计算平仓单的已实现盈亏（纯函数，便于单测）。

    按时间序回放同账户+同品种的成交历史（不含本单），维护**带符号**持仓与移动
    平均成本：同向加仓 → 成本加权平均；减仓 → 成本不变；**穿越零仓 → 按成交价
    反手开新仓**。平仓盈亏 = (平仓价 - 平均成本) × 平仓量。

    仓位必须带符号：`max(0, pos - q)` 把卖超截断成空仓，会让事故后（空头状态）
    的买入被当成空仓下的首笔买入直接取价，成本基准被拉低到平仓价之下，**亏损
    显示为收益**（2026-09-28 实盘：-24.51 被算成 +21.54）。空头的「回补」部分
    不产生多头成本，只有穿越零仓后的剩余量才成为新多头成本。

    仅支持平多（卖出且历史上有多头成本）与无成本基础（返回 None）两种结论：
    不做空头的盈亏归因——那需要交易所的已实现盈亏字段，本地推不出来。

    Args:
        history: 成交历史，每项 {side, filled_quantity, avg_fill_price}，按 created_at 升序
        side: 当前订单方向
        filled_quantity: 当前订单已成交量
        avg_fill_price: 当前订单成交均价

    Returns:
        已实现盈亏；非卖出单、无成交均价、或回放结束仍无多头仓位时返回 None
    """
    if side != "sell" or filled_quantity is None or filled_quantity <= 0:
        return None
    if avg_fill_price is None:
        return None

    pos = Decimal("0")
    avg_cost = Decimal("0")
    for row in history:
        q = row.get("filled_quantity") or Decimal("0")
        p = row.get("avg_fill_price")
        if q <= 0 or p is None:
            continue
        if row.get("side") == "buy":
            if pos >= 0:
                # 多头加仓：加权平均（空仓下首笔直接取价）
                avg_cost = (avg_cost * pos + p * q) / (pos + q) if pos > 0 else p
                pos += q
            else:
                # 回补空头：回补部分不产生多头成本，穿零后的剩余量按 p 开新多头
                pos += q
                if pos > 0:
                    avg_cost = p
                elif pos == 0:
                    avg_cost = Decimal("0")
        else:
            if pos > 0:
                # 平多：成本不变；卖超则穿零，剩余量按成交价开新空头
                pos -= q
                if pos < 0:
                    avg_cost = p
                elif pos == 0:
                    avg_cost = Decimal("0")
            else:
                # 空头加仓：加权平均
                avg_cost = (
                    (avg_cost * (-pos) + p * q) / (-pos + q) if pos < 0 else p
                )
                pos -= q

    if pos <= 0:
        # 无成本基础：无历史买入（如开空单）、或历史买入已被同量卖出抵消
        return None
    return (avg_fill_price - avg_cost) * min(filled_quantity, pos)


class OrderExecutor:
    """
    订单执行器（随交易框架懒加载，由 FrameManager 管理生命周期）。

    单例模式：通过 get_instance() 访问。
    """

    _instance: "OrderExecutor | None" = None

    def __init__(self):
        # 按**交易所名**索引：同一交易所只挂一个活跃账户时它才唯一确定适配器。
        # 遗留口径，只给「调用方拿不到账户 id」的位置用（cancel_order / get_positions /
        # get_balance / 视图）；下单与成交同步一律走 `_account_adapters`。
        self._adapters: dict[str, BaseExchangeAdapter] = {}
        # 按 **ExchangeAccount.id** 索引：这才是唯一不会拿错密钥的口径。同一交易所的
        # 多个账户各有各的密钥与 testnet 属性，按交易所名解析会用到**别人**的凭证。
        self._account_adapters: dict[str, BaseExchangeAdapter] = {}
        # 账户 id → 交易所名。适配器自己不记得交易所（`BaseExchangeAdapter` 上没有这个
        # 属性），只能在加载时顺手记下：**同交易所有两个账户时，被顶掉的那个在
        # `_adapters` 里查不到交易所名**，日志标签就只剩一个 UUID 可看。这份映射是
        # `_unique_adapters()` 能给出「binance#<账户id>」的唯一来源。
        self._account_exchanges: dict[str, str] = {}
        # 同交易所有多个活跃账户的交易所名：`_adapters` 里那一条只是「最后一个加载的」，
        # 是个任意选择，调用方据此判断「按名字解析是否可信」。
        self._ambiguous_exchanges: set[str] = set()
        self._riskguard: Optional[RiskGuard] = None
        self._running = False
        self._fill_sync_task: Optional[asyncio.Task] = None
        self._dangling_sweep_task: Optional[asyncio.Task] = None

    @classmethod
    def get_instance(cls) -> "OrderExecutor | None":
        """返回当前单例，未初始化时为 None"""
        return cls._instance

    # ─── 生命周期 ────────────────────────────────────────────────────────────

    async def initialize(self) -> None:
        """
        FrameManager 启动交易框架时调用。
        1. 设置单例
        2. 从 DB 加载已激活的交易所账号
        3. 解密 API Key 并连接适配器
        """
        OrderExecutor._instance = self
        self._running = True

        await self._load_adapters()
        self._load_riskguard()
        self.start_fill_sync()

        logger.info(
            "OrderExecutor initialized: %s 个账户适配器（%s 个交易所名条目）",
            len(self._account_adapters),
            len(self._adapters),
        )

    async def shutdown(self) -> None:
        """
        FrameManager 停止交易框架时调用。
        停止成交同步任务、断开所有适配器并清理单例。
        """
        self._running = False

        if self._fill_sync_task and not self._fill_sync_task.done():
            self._fill_sync_task.cancel()
            try:
                await self._fill_sync_task
            except (asyncio.CancelledError, Exception):
                pass
            self._fill_sync_task = None

        # 悬挂行扫描是分离任务：它持有已加载的订单对象且不阻塞成交同步，
        # 所以关框架时必须显式取消，否则它会带着断掉的适配器继续跑。
        if self._dangling_sweep_task and not self._dangling_sweep_task.done():
            self._dangling_sweep_task.cancel()
            try:
                await self._dangling_sweep_task
            except (asyncio.CancelledError, Exception):
                pass
        self._dangling_sweep_task = None

        # 两个地图可能指向同一个适配器实例（同交易所单账户），按对象去重后各断一次。
        for name, adapter in self._unique_adapters():
            await adapter.disconnect()
            logger.info(f"Adapter disconnected: {name}")

        self._adapters.clear()
        self._account_adapters.clear()
        self._account_exchanges.clear()
        self._ambiguous_exchanges.clear()
        OrderExecutor._instance = None
        logger.info("OrderExecutor shutdown")

    # ─── 适配器查找 ──────────────────────────────────────────────────────────

    def _unique_adapters(self) -> list[tuple[str, BaseExchangeAdapter]]:
        """(标签, 适配器) 去重列表：账户地图是超集，遗留地图只补它没有的实例。

        标签取**交易所名**，同交易所有多个账户时补账户 id（``binance#3f2a…``）：
        账户地图的键是账户 id，直接用它会得到一行只有 UUID 的日志；而只写交易所名
        则两个账户都叫 binance，等于没区分。账户 id 才是「不会拿错密钥」的那个口径，
        所以它必须出现在标签里。

        交易所名优先查 ``_account_exchanges``（加载时记下的账户→交易所）：同交易所有
        两个账户时，被顶掉的那个**不在** ``_adapters`` 里，反查不到名字，标签会退化成
        一个裸 UUID——而那恰恰是最需要分辨账户的那个场景。

        调用方（浮亏巡检、断连日志）拿到的标签因此是**可读且唯一**的。
        """
        exchange_of = {id(a): name for name, a in self._adapters.items()}
        seen: set[int] = set()
        out: list[tuple[str, BaseExchangeAdapter]] = []
        for label, adapter in list(self._account_adapters.items()) + list(
            self._adapters.items()
        ):
            if adapter is None or id(adapter) in seen:
                continue
            seen.add(id(adapter))
            exchange = self._account_exchanges.get(label) or exchange_of.get(id(adapter))
            if exchange is None or exchange == label:
                # 遗留条目的键本来就是交易所名（也是账户地图为空时唯一的条目）
                out.append((label, adapter))
            else:
                out.append((f"{exchange}#{label}", adapter))
        return out

    def is_ambiguous_exchange(self, exchange: str) -> bool:
        """该交易所名下是否有多个活跃账户（此时按交易所名解析不可信）。"""
        return (exchange or "").lower() in self._ambiguous_exchanges

    def _resolve_adapter(
        self, exchange: str, exchange_account_id: str | None
    ) -> BaseExchangeAdapter | None:
        """按**账户 id** 取适配器；拿不到就让调用方失败，绝不退到别的账户。

        2026-09-22：`_adapters` 以交易所名为键，同一交易所挂两个账户时后者顶掉
        前者，于是下单、成交同步、反查都可能拿着**另一个账户**的密钥去做——订单
        落到错账户上是真实的资金错误，而日志里看不出来。

        遗留回退（按交易所名）只在「账户地图为空」时生效：两个地图由
        `_load_adapters` 同一段循环填充，真实运行里账户地图为空等价于「一个适配器
        都没加载」，所以这条回退在真实运行中恒为 None，只为兼容手工构造 `_adapters`
        的调用方（测试夹具）而留。**账户清单非空却没有这个账户时一律返回 None**：
        那种情况下按名字找到的是同交易所另一个账户的适配器。
        """
        account_key = str(exchange_account_id) if exchange_account_id else ""
        if account_key:
            adapter = self._account_adapters.get(account_key)
            if adapter is not None:
                return adapter
            if self._account_adapters:
                logger.warning(
                    "[OrderExecutor] 账户 %s（交易所 %s）没有已加载的适配器"
                    "（密钥缺失/已停用/加载失败）——拒绝回退到按交易所名解析："
                    "那会拿到同交易所另一个账户的密钥",
                    account_key,
                    exchange,
                )
                return None
        return self._adapters.get((exchange or "").lower())

    # ─── 下单主流程 ───────────────────────────────────────────────────────────

    async def submit_order(
        self,
        exchange: str,
        symbol: str,
        side: str,
        order_type: str,
        quantity: Decimal,
        price: Optional[Decimal],
        exchange_account_id: str,
        user_id: Optional[str] = None,
        is_close_position: bool = False,
        live_session_id: Optional[str] = None,
    ) -> dict:
        """
        下单主流程。

        Args:
            exchange: 交易所名称 ('binance')
            symbol: 交易对符号（如 'BTCUSDT'）
            side: 买卖方向 ('buy' | 'sell')
            order_type: 订单类型 ('market' | 'limit')
            quantity: 数量
            price: 价格（市价单可为空）
            exchange_account_id: ExchangeAccount UUID
            user_id: User UUID（供 RiskGuard 风控校验）
            is_close_position: 平仓信号，跳过 RiskGuard 风控检查
            live_session_id: 触发的实盘会话 UUID（用于追溯订单来源策略）

        Returns:
            包含 exchange_order_id 和 order_id 的字典

        Raises:
            RuntimeError: OrderExecutor 未运行
            ValueError: 交易所适配器不存在
            PermissionError: RiskGuard 拒绝下单
        """
        logger.info(f"[SF-07][OrderExecutor] submit: {exchange} {symbol} {side} {order_type} qty={quantity} close={is_close_position}")
        uid = user_id  # 保留引用供 finally 使用
        if not self._running:
            raise RuntimeError("OrderExecutor is not running")

        adapter = self._resolve_adapter(exchange, exchange_account_id)
        if not adapter:
            raise ValueError(
                f"Exchange adapter not found: {exchange}（account={exchange_account_id}）"
            )

        # 1. RiskGuard 前置校验（平仓跳过）
        request = OrderRequest(
            exchange=exchange,
            symbol=symbol,
            side=side,
            order_type=order_type,
            quantity=quantity,
            price=price,
            # 风控要按**同一个**账户解析：这里传下去的账户 id 就是本单即将使用的那个
            # 适配器，风控拿它算仓位/余额才和下面的下单是同一笔钱。
            exchange_account_id=str(exchange_account_id) if exchange_account_id else None,
            # 同上，会话 id 是**判定输入**而不是追溯字段：策略档的停止声明要靠它换出
            # 策略 id，而落库（`_persist_order`）在风控之后，来不及。
            live_session_id=live_session_id,
        )
        if is_close_position:
            logger.info("[SF-07a][OrderExecutor] close position — skipping RiskGuard")
        elif self._riskguard:
            # 诊断：检查方法类型
            import inspect
            method = self._riskguard.pre_trade_check
            logger.info(f"[SF-07a][OrderExecutor] pre_trade_check method: {method}, iscoroutinefunction={inspect.iscoroutinefunction(method)}")

            logger.info(f"[SF-07a][OrderExecutor] riskguard instance: {type(self._riskguard).__name__} id={id(self._riskguard)}")
            logger.info("[SF-07a][OrderExecutor] calling pre_trade_check...")
            try:
                approved, reason = await self._riskguard.pre_trade_check(request, user_id)
                logger.info(f"[SF-07a][OrderExecutor] pre_trade_check returned: approved={approved}")
            except Exception as e:
                logger.error(f"[SF-07a][OrderExecutor] pre_trade_check exception: {type(e).__name__}: {e}")
                raise
            if not approved:
                logger.warning(f"[SF-04][OrderExecutor] RiskGuard拒绝下单: {reason}")
                raise PermissionError(f"RiskGuard拒绝下单: {reason}")

        # 2. 持久化订单 (status=pending)
        logger.info("[SF-07b][OrderExecutor] persisting order...")
        try:
            order = await asyncio.wait_for(
                self._persist_order(
                    exchange_account_id=exchange_account_id,
                    user_id=user_id,
                    symbol=symbol,
                    side=side,
                    order_type=order_type,
                    quantity=quantity,
                    price=price,
                    status="pending",
                    live_session_id=live_session_id,
                ),
                timeout=30.0
            )
        except asyncio.TimeoutError:
            logger.error("[SF-07b][OrderExecutor] _persist_order timeout (30s)")
            raise TimeoutError("Order persistence timeout")
        logger.info(f"[SF-07c][OrderExecutor] order persisted: id={order.id}")

        # 用订单行的 request_id 作为交易所幂等键（newClientOrderId）：
        # 重试与"响应丢失后对账"都以它为准。没有它时，transport 重试可能重复下单
        # （2026-09-21 事故后补：当次失败单没有幂等键，交易所侧无法对账）。
        request.client_order_id = str(order.request_id)

        # 3. 发送到交易所
        try:
            logger.info("[SF-07d][OrderExecutor] calling adapter.place_order...")
            try:
                response = await asyncio.wait_for(
                    adapter.place_order(request),
                    # 45s：适配器内部预算为 10s(首次超时) + 10s(重连重发或按 clientOrderId
                    # 对账) + 余量。低于此值会在对账完成前取消，导致"订单状态未知"。
                    timeout=45.0,
                )
            except asyncio.TimeoutError:
                logger.error("[SF-07d][OrderExecutor] adapter.place_order timeout (45s)")
                raise TimeoutError("Exchange API timeout")

            # 应用下单响应中的即时成交信息（市价单通常同步成交）
            new_status = self._map_exchange_status(response.status)
            update_kwargs = {
                "status": new_status,
                "exchange_order_id": response.exchange_order_id,
            }
            try:
                filled_qty = Decimal(response.filled_qty)
            except Exception:
                filled_qty = Decimal("0")
            if filled_qty > 0:
                update_kwargs["filled_quantity"] = filled_qty
                if response.avg_price is not None:
                    try:
                        update_kwargs["avg_fill_price"] = Decimal(
                            response.avg_price
                        )
                    except Exception:
                        pass
            # 平仓卖单即时成交（市价单通常同步成交）时计算已实现盈亏，
            # 并同步会话权益。盈亏计算失败不阻塞下单主流程（订单已在交易所）。
            if (
                side == "sell"
                and filled_qty > 0
                and update_kwargs.get("avg_fill_price") is not None
            ):
                try:
                    pnl = await self._apply_close_pnl(
                        exchange_account_id=exchange_account_id,
                        symbol=symbol,
                        order_id=str(order.id),
                        live_session_id=live_session_id,
                        side=side,
                        filled_quantity=filled_qty,
                        avg_fill_price=update_kwargs["avg_fill_price"],
                    )
                except Exception as e:
                    logger.warning(
                        f"[OrderExecutor] realized_pnl compute failed: {e}"
                    )
                    pnl = None
                if pnl is not None:
                    update_kwargs["realized_pnl"] = pnl
                    logger.info(
                        f"[OrderExecutor] realized_pnl={pnl} for close order {order.id}"
                    )
            await self._update_order(
                order_id=str(order.id),
                **update_kwargs,
            )
            if self._riskguard:
                await self._riskguard.record_order_success(uid)
            logger.info(
                f"[SF-08][OrderExecutor] submitted: order_id={order.id} "
                f"exchange_order_id={response.exchange_order_id}"
            )
            return {
                "order_id": str(order.id),
                "exchange_order_id": response.exchange_order_id,
                "status": response.status,
            }
        except Exception as e:
            # 失败原因必须带类型名：httpx 超时异常的 message 为空，只写 str(e) 会落一条空消息
            # （2026-09-21 事故：库里的 failed 单 error_message='' ，无法诊断）
            error_text = f"{type(e).__name__}: {e}".strip().rstrip(":") or type(e).__name__
            # 「下单结果未知」不是失败：交易所侧可能正有一张活着的单，记 failed 会让
            # 账面与实际分叉，而且用户看到「失败」会再下一张 → 双倍敞口。落成非终态的
            # unknown，交给悬挂扫描/成交同步继续找它；熔断计数照旧（仍然算一次失败），
            # 因为未知单同样是风险信号。
            placement_unknown = isinstance(e, OrderPlacementUnknown)
            await self._update_order(
                order_id=str(order.id),
                status="unknown" if placement_unknown else "failed",
                error_message=error_text,
            )
            if self._riskguard:
                await self._riskguard.record_order_failure(
                    uid,
                    symbol=symbol,
                    side=side,
                    quantity=str(quantity),
                    error=error_text,
                    unknown=placement_unknown,
                )
            logger.error(
                f"[SF-09][OrderExecutor] {'结果未知' if placement_unknown else 'failed'}: "
                f"order_id={order.id} - {error_text}"
            )
            raise

    async def cancel_order(
        self,
        exchange: str,
        exchange_order_id: str,
        symbol: str,
    ) -> bool:
        """
        撤销指定订单。

        Returns:
            True 撤销成功，False 失败
        """
        if not self._running:
            raise RuntimeError("OrderExecutor is not running")

        adapter = self._adapters.get(exchange)
        if not adapter:
            raise ValueError(f"Exchange adapter not found: {exchange}")

        return await adapter.cancel_order(exchange_order_id, symbol)

    # ─── 持仓 & 余额查询 ─────────────────────────────────────────────────────

    async def get_positions(self, exchange: str) -> list:
        """获取指定交易所的当前持仓"""
        if not self._running:
            raise RuntimeError("OrderExecutor is not running")

        adapter = self._adapters.get(exchange)
        if not adapter:
            raise ValueError(f"Exchange adapter not found: {exchange}")

        return await adapter.get_positions()

    async def get_balance(self, exchange: str) -> dict:
        """获取指定交易所的账户余额"""
        if not self._running:
            raise RuntimeError("OrderExecutor is not running")

        adapter = self._adapters.get(exchange)
        if not adapter:
            raise ValueError(f"Exchange adapter not found: {exchange}")

        return await adapter.get_balance()

    # ─── 内部方法 ─────────────────────────────────────────────────────────────

    async def _load_adapters(self) -> None:
        """从 DB 加载已激活的交易所账号，解密并连接适配器。

        **同时填两个地图**，它们指向同一个适配器实例：
        ``_account_adapters[account.id]``（唯一不会拿错密钥的口径）与
        ``_adapters[exchange]``（遗留口径，只给拿不到账户 id 的调用方）。
        同交易所有多个活跃账户时，``_adapters`` 里那条只是「最后加载的」，是个
        任意选择——所以这里把它记进 ``_ambiguous_exchanges`` 并打出 ERROR，
        让「按名字解析到底选了谁」在日志里可查，而不是静默顶掉。
        """
        from apps.exchange.models import ExchangeAccount

        accounts = await db_async(
            lambda: list(ExchangeAccount.objects.filter(is_active=True))
        )()

        fernet = self._get_fernet()

        # exchange → 已成功加载的账户 id（按加载顺序，最后一个就是 `_adapters` 里的赢家）
        loaded: dict[str, list[str]] = {}

        for account in accounts:
            exchange = account.exchange.lower()
            adapter_cls = ADAPTER_MAP.get(exchange)
            if not adapter_cls:
                logger.warning(f"No adapter for exchange: {exchange}, skipping")
                continue

            try:
                api_key_enc = _to_bytes(account.api_key_enc)
                api_secret_enc = _to_bytes(account.api_secret_enc)
                if not api_key_enc or not api_secret_enc:
                    logger.warning(
                        "Skipping adapter for %s (%s): empty API key or secret",
                        exchange,
                        account.label or "default",
                    )
                    continue

                api_key = fernet.decrypt(api_key_enc).decode()
                api_secret = fernet.decrypt(api_secret_enc).decode()

                adapter = adapter_cls(api_key, api_secret, account.testnet)

                await adapter.connect()
                self._account_adapters[str(account.id)] = adapter
                self._account_exchanges[str(account.id)] = exchange
                self._adapters[exchange] = adapter
                loaded.setdefault(exchange, []).append(str(account.id))
                logger.info(
                    f"Adapter loaded: {exchange} ({account.label or 'default'})"
                )
            except Exception as e:
                logger.error(f"Failed to load adapter for {exchange}: {e}")

        for exchange, account_ids in loaded.items():
            if len(account_ids) < 2:
                continue
            self._ambiguous_exchanges.add(exchange)
            logger.error(
                "[OrderExecutor] 交易所 %s 有 %s 个活跃账户（%s）：按交易所名解析"
                "只能得到最后加载的那个（account=%s），其余账户必须按账户 id 访问；"
                "下单与成交同步已一律按账户 id 解析",
                exchange,
                len(account_ids),
                ", ".join(account_ids),
                account_ids[-1],
            )

    def _load_riskguard(self) -> None:
        """获取 RiskGuard 单例引用"""
        from apps.riskguard.guard import RiskGuard

        self._riskguard = RiskGuard.get_instance()

    def _get_fernet(self):
        """创建 Fernet 解密器（与 pending_reconcile 共用同一条解密路径）"""
        return get_fernet()

    async def _persist_order(
        self,
        exchange_account_id: str,
        user_id: Optional[str],
        symbol: str,
        side: str,
        order_type: str,
        quantity: Decimal,
        price: Optional[Decimal],
        status: str,
        live_session_id: Optional[str] = None,
    ) -> "Order":
        """异步写入 orders 表"""
        from apps.trading.models import Order, LiveSession

        @db_async
        def _create():
            kwargs_create = {
                "exchange_account_id": exchange_account_id,
                "symbol": symbol.upper(),
                "side": side.lower(),
                "order_type": order_type.lower(),
                "quantity": quantity,
                "price": price,
                "status": status,
                "request_id": uuid.uuid4(),
            }
            if user_id:
                kwargs_create["user_id"] = user_id
            if live_session_id:
                kwargs_create["live_session_id"] = live_session_id
                # 持久化触发策略快照：即使会话/策略后续被删除，详情仍可追溯
                triggered_strategy = None
                try:
                    ls = LiveSession.objects.select_related("strategy").get(
                        id=live_session_id
                    )
                    triggered_strategy = ls.strategy.name
                except (LiveSession.DoesNotExist, AttributeError):
                    triggered_strategy = None
                if triggered_strategy:
                    kwargs_create["triggered_strategy"] = triggered_strategy
            return Order.objects.create(**kwargs_create)

        return await _create()

    async def _update_order(self, order_id: str, **kwargs) -> None:
        """异步更新订单"""
        from apps.trading.models import Order

        @db_async
        def _update():
            Order.objects.filter(id=order_id).update(**kwargs)

        await _update()

    # ─── 已实现盈亏（平仓单）────────────────────────────────────────────────

    async def _compute_close_pnl(
        self,
        exchange_account_id: str,
        symbol: str,
        order_id: str,
        side: str,
        filled_quantity: Decimal,
        avg_fill_price: Decimal | None,
    ) -> Decimal | None:
        """查同账户+同品种成交历史，计算平仓单已实现盈亏（移动平均成本法）。"""
        from apps.trading.models import Order

        @db_async
        def _history():
            return list(
                Order.objects.filter(
                    exchange_account_id=exchange_account_id,
                    # 与 _persist_order 落库口径一致（统一大写）
                    symbol=symbol.upper(),
                    status="filled",
                    avg_fill_price__isnull=False,
                )
                .exclude(id=order_id)
                .order_by("created_at")
                .values("side", "filled_quantity", "avg_fill_price")
            )

        try:
            history = await _history()
        except Exception as e:
            logger.warning(
                f"[OrderExecutor] failed to load order history for pnl: {e}"
            )
            return None
        return compute_realized_pnl(
            history, side, filled_quantity, avg_fill_price
        )

    async def _apply_close_pnl(
        self,
        exchange_account_id: str,
        symbol: str,
        order_id: str,
        live_session_id: str | None,
        side: str,
        filled_quantity: Decimal,
        avg_fill_price: Decimal | None,
        previous_pnl: Decimal | None = None,
    ) -> Decimal | None:
        """平仓卖单成交后：计算已实现盈亏，并按增量更新会话权益。

        返回新的 realized_pnl（None 表示无法计算）；
        权益增量 = 新盈亏 - 旧盈亏（部分成交→完全成交时按全量重算，
        用增量修正权益，避免重复累加）。
        """
        pnl = await self._compute_close_pnl(
            exchange_account_id=exchange_account_id,
            symbol=symbol,
            order_id=order_id,
            side=side,
            filled_quantity=filled_quantity,
            avg_fill_price=avg_fill_price,
        )
        if pnl is None:
            return None
        delta = pnl - (previous_pnl or Decimal("0"))
        if live_session_id and delta != 0:
            await self._bump_session_equity(live_session_id, delta)
        return pnl

    async def _bump_session_equity(self, live_session_id: str, delta: Decimal) -> None:
        """会话权益 += 已实现盈亏增量。

        ponytail: current_equity 建会话时必然写入，这里不做 NULL 兜底。
        """
        from django.db.models import F

        from apps.trading.models import LiveSession

        try:
            @db_async
            def _bump():
                LiveSession.objects.filter(id=live_session_id).update(
                    current_equity=F("current_equity") + delta
                )

            await _bump()
        except Exception as e:
            logger.warning(
                f"[OrderExecutor] session equity update failed ({live_session_id}): {e}"
            )

    # ─── 成交同步（fill sync）───────────────────────────────────────────────

    # 交易所状态字符串 → 本地 Order.status 映射
    _EXCHANGE_STATUS_MAP = {
        "NEW": "submitted",
        "PARTIALLY_FILLED": "partial",
        "FILLED": "filled",
        "CANCELED": "cancelled",
        "EXPIRED": "cancelled",
        "EXPIRED_IN_MATCH": "cancelled",
        "REJECTED": "failed",
    }

    def _map_exchange_status(self, status: str) -> str:
        return self._EXCHANGE_STATUS_MAP.get(status, "submitted")

    def start_fill_sync(self, interval: float = 10.0) -> None:
        """启动成交状态后台同步任务（幂等，间隔默认 10s）。"""
        if self._fill_sync_task and not self._fill_sync_task.done():
            return
        try:
            self._fill_sync_task = asyncio.create_task(
                self._fill_sync_loop(interval)
            )
            logger.info(f"Fill sync started (interval={interval}s)")
        except RuntimeError as e:
            logger.warning(f"Fill sync not started (no running loop): {e}")

    async def _fill_sync_loop(self, interval: float) -> None:
        """后台循环：周期性同步所有活跃订单的成交状态。"""
        while True:
            try:
                await self._sync_active_orders()
            except Exception as e:
                logger.error(f"Fill sync cycle error: {e}")
            await asyncio.sleep(interval)

    async def _sync_active_orders(self) -> None:
        """同步所有活跃订单（pending/submitted/partial/unknown）的成交状态。

        ``unknown`` 必须在活跃集里：它是非终态，语义是「这张单可能仍在交易所活着」，
        漏掉它等于把唯一可能变成真实敞口的那一档当成不存在。

        无 ``exchange_order_id`` 的行不再被跳过（CONTEXT.md 第 137 条修掉的那句）
        ——它们交给 ``sweep_dangling_orders`` 反查；但**不在本循环里 await**：
        反查要走交易所网络、可能一直超时，内联等待会把后面所有交易所的成交同步
        一起饿死。所以它被 spawn 成一个分离任务。
        """
        from apps.trading.models import Order

        orders = await db_async(
            lambda: list(
                Order.objects.filter(
                    status__in=list(Order.ACTIVE_STATUSES)
                ).select_related("exchange_account")
            )
        )()

        if not orders:
            return

        by_exchange: dict[str, list] = {}
        has_dangling = False
        for order in orders:
            if not order.exchange_order_id:
                # 刚下单、place_order 还在途中的行不该被反查（此刻交易所还没有它），
                # 只有超出下单窗口的才是崩溃/丢响应的遗留。
                if is_beyond_placement_window(order):
                    has_dangling = True
                continue
            # 按**账户**分组而不是按交易所名：成交通知必须用订单自己那个账户的密钥
            # 去查，否则同交易所的两个账户会互相查对方的单（查不到 → 误判 cancelled）。
            by_exchange.setdefault(str(order.exchange_account_id), []).append(order)

        for account_id, order_list in by_exchange.items():
            exchange = (order_list[0].exchange_account.exchange or "").lower()
            adapter = self._resolve_adapter(exchange, account_id)
            if not adapter:
                continue
            for order in order_list:
                try:
                    fill = await adapter.fetch_order(
                        order.exchange_order_id, order.symbol
                    )
                except OrderNotFoundError as e:
                    await self._update_order(
                        order_id=str(order.id),
                        status="cancelled",
                        error_message=str(e),
                    )
                    logger.warning(
                        f"[FillSync] order {order.id} not found on exchange: {e}"
                    )
                    continue
                except Exception as e:
                    logger.warning(
                        f"[FillSync] failed to fetch order {order.id} "
                        f"({order.exchange_order_id}): {e}"
                    )
                    continue
                await self._apply_fill(order, fill)

        if has_dangling:
            self._spawn_dangling_sweep()

    def _spawn_dangling_sweep(self) -> None:
        """起一个分离任务扫描悬挂行（同一时刻只有一个，不叠加）。"""
        task = self._dangling_sweep_task
        if task and not task.done():
            return
        try:
            self._dangling_sweep_task = asyncio.create_task(
                self._run_dangling_sweep()
            )
        except RuntimeError as e:
            logger.warning(f"[FillSync] 悬挂行扫描未启动（无运行中的 loop）：{e}")

    async def _run_dangling_sweep(self) -> None:
        """悬挂行扫描的异常必须就地吞掉：这是一个 fire-and-forget 任务，
        逃出去的异常会被 asyncio 记成「Task exception was never retrieved」而丢失上下文。"""
        try:
            await sweep_dangling_orders()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("[FillSync] 悬挂订单扫描失败", exc_info=True)

    async def _apply_fill(self, order, fill) -> None:
        """把交易所返回的成交状态落库（仅在变化时更新）。"""
        updates: dict = {}
        if fill.status != order.status:
            updates["status"] = fill.status
        if fill.filled_quantity != order.filled_quantity:
            updates["filled_quantity"] = fill.filled_quantity
        if fill.avg_fill_price is not None and (
            order.avg_fill_price is None
            or fill.avg_fill_price != order.avg_fill_price
        ):
            updates["avg_fill_price"] = fill.avg_fill_price
        if fill.error_message and fill.error_message != order.error_message:
            updates["error_message"] = fill.error_message
        # 平仓卖单：成交状态变化时重算已实现盈亏（部分→完全成交按全量重算，
        # 权益按增量修正，避免重复累加）
        if (
            order.side == "sell"
            and fill.filled_quantity > 0
            and fill.avg_fill_price is not None
            and (
                fill.status != order.status
                or fill.filled_quantity != order.filled_quantity
            )
        ):
            try:
                pnl = await self._apply_close_pnl(
                    exchange_account_id=str(order.exchange_account_id),
                    symbol=order.symbol,
                    order_id=str(order.id),
                    live_session_id=(
                        str(order.live_session_id)
                        if order.live_session_id
                        else None
                    ),
                    side=order.side,
                    filled_quantity=fill.filled_quantity,
                    avg_fill_price=fill.avg_fill_price,
                    previous_pnl=order.realized_pnl,
                )
                if pnl is not None:
                    updates["realized_pnl"] = pnl
                    logger.info(
                        f"[FillSync] order {order.id} {order.symbol}: "
                        f"realized_pnl={pnl}"
                    )
            except Exception as e:
                logger.warning(
                    f"[FillSync] realized_pnl compute failed for {order.id}: {e}"
                )
        if updates:
            await self._update_order(order_id=str(order.id), **updates)
            logger.info(
                f"[FillSync] order {order.id} {order.symbol}: "
                f"{order.status} -> {fill.status}, filled={fill.filled_quantity}"
            )
