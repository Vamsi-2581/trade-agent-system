"""
实盘策略运行器

订阅实时 K 线 → 执行策略 on_bar → 分发信号到 Redis Stream。
由 FrameManager 在启动交易框架时加载。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from decimal import Decimal
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from .base import BaseStrategy, OrderSignal, PortfolioTarget

logger = logging.getLogger(__name__)


class LiveStrategyRunner:

    # 持仓同步的最小间隔（秒）。同一根 K 线的多次 tick 推送、以及高频验证事件
    # 都按此节流；**新 K 线到来时强制刷新**（每 bar 至少一次真值）。
    POSITION_SYNC_MIN_INTERVAL = 15.0

    def __init__(
        self,
        strategy: "BaseStrategy",
        symbol: str,
        timeframe: str,
        exchange_account_id: str,
        user_id: str | None = None,
        live_session_id: str | None = None,
    ):
        self.strategy = strategy
        self.symbol = symbol
        self.timeframe = timeframe
        self.exchange_account_id = exchange_account_id
        self.user_id = user_id
        self.live_session_id = live_session_id
        self._running = False
        self._kline_history: list[dict] = []
        self._dispatcher = None
        self._validation_task: "asyncio.Task | None" = None
        # 防止同方向重复开仓：记录每个 symbol 最近的入场方向
        # "buy" | "sell" | None，平仓（target_qty=0）时清除
        self._entry_direction: dict[str, str] = {}
        # per-session 并发锁：信号处理中时拒绝新的 kline 触发
        self._signal_lock = asyncio.Lock()
        # 持仓同步状态（C 修复）：None=从未同步 / True=最近成功 / False=最近失败。
        # 失败时决策路径直接跳过本轮，绝不拿"未知或陈旧持仓"下单。
        self._position_synced: bool | None = None
        self._last_position_sync_ts: float = 0.0
        self._position_sync_failure_notified = False
        # 会话级持仓台账（2026-10-08 修复 2，2026-10-07 box 平掉 donchian 共享仓
        # 事故跟进）：ctx.position 是「本会话台账」，不再是账户持仓——
        #   首次成功同步 = 采用：同账户+同品种另有活跃会话时账户仓位无法归属
        #   （可能是别人的仓）→ 采用 0；否则采用账户真值（传统行为）；
        #   后续同步 = 对账：外部来的仓位保持 0（绝不平别会话的仓），
        #   账户仓位 < 台账 → 台账下钳（不得卖出超出账户持有的量）。
        self._position_adopted: bool = False
        self._last_actual_position: Decimal | None = None  # 账户级真值：卖出侧钳制依据
        # 最近一次成功下发订单的实时时刻：区分「本会话在途单未成交」（交易所态滞后，
        # 保持 0 属正常）与「真外部持仓」（告警+通知）。
        self._last_order_dispatch_ts: float = 0.0
        self._shared_account_notified: bool = False
        self._foreign_position_notified: bool = False
        # 最近一次决策使用的 K 线 timestamp（用于识别"新 bar"→ 强制刷新持仓）
        self._last_bar_ts = None

    async def start(self) -> None:
        """启动实盘策略运行"""
        if self._running:
            logger.warning(f"[LiveStrategyRunner] already running for {self.symbol}")
            return

        self._running = True
        self._kline_history = []
        self.strategy.on_start()

        # 初始化信号分发器
        from .signals import SignalDispatcher

        self._dispatcher = SignalDispatcher()

        # 预加载历史 K 线（warmup），让指标能立即产出有效值
        await self.load_initial_history(limit=self.strategy.min_kline_length)

        # 从交易所同步当前持仓到 ctx
        await self._sync_position_from_exchange()

        # 用最新历史收盘价初始化 ctx.price，避免第一根实时 K 线到达前
        # 策略拿到的 price 为 0（select_universe / generate_insights 可能依赖）
        if self._kline_history:
            try:
                last_close = self._kline_history[-1].get("close")
                if last_close is not None:
                    self.strategy.ctx.set_price(
                        self.symbol, Decimal(str(last_close))
                    )
            except Exception as e:
                logger.warning(
                    f"[LiveStrategyRunner] failed to seed ctx.price from history: {e}"
                )

        # 订阅 K 线数据
        await self._subscribe_kline()

        # 注册策略的最小周期信号到 SignalMonitor
        await self._register_signal_monitors()

        # 启动策略验证事件监听（由 SignalMonitor 触发后消费）
        watch_signals = self.strategy.get_watch_signals()
        if watch_signals:
            self._validation_task = asyncio.create_task(self._listen_validation())

        logger.info(
            f"[LiveStrategyRunner] started: strategy={self.strategy.name} "
            f"symbol={self.symbol} tf={self.timeframe} "
            f"watch_signals={len(watch_signals)} "
            f"exchange_account={self.exchange_account_id} "
            f"user={self.user_id} live_session={self.live_session_id}"
        )

    async def stop(self) -> None:
        """停止实盘策略运行"""
        if not self._running:
            return

        self._running = False
        self.strategy.on_stop()
        await self._unsubscribe_kline()

        # 取消验证监听任务
        if self._validation_task and not self._validation_task.done():
            self._validation_task.cancel()
            try:
                await self._validation_task
            except asyncio.CancelledError:
                pass
            self._validation_task = None

        # 清理 SignalMonitor 记录
        await self._unregister_signal_monitors()

        logger.info(f"[LiveStrategyRunner] stopped: {self.strategy.name}")

    async def on_kline(self, kline: dict) -> None:
        """
        K 线数据回调入口。
        由 DataFeed 收到新 K 线时调用。

        @kline 流每次成交都推送（同一根 bar 内约 1~2 秒一次）：bar 内 tick
        只更新历史，**K 线收盘（is_closed=True）才执行策略决策**，与回测的
        逐 bar 契约一致（2026-10 修复：bar 内决策导致箱体策略频繁进出场）。

        Args:
            kline: {open, high, low, close, volume, timestamp, is_closed, ...}
        """
        if not self._running:
            return

        # 过滤：DataSource 按 DataType 广播，需按 symbol 二次过滤（防御层，
        # 精确分发已保证同 (symbol, interval) 的回调只触发一次）
        # kline["symbol"] 可能是 binance 原始格式 (DOGEUSDT) 或 ccxt 格式 (DOGE/USDT)
        kline_symbol = kline.get("symbol")
        if kline_symbol:
            norm_kline = kline_symbol.replace("/", "").upper()
            norm_self = self.symbol.replace("/", "").upper()
            if norm_kline != norm_self:
                logger.debug(
                    f"[SF-01][LiveStrategy] drop mismatched kline: "
                    f"expected={self.symbol} got={kline_symbol} close={kline.get('close')}"
                )
                return

        logger.debug(f"[SF-01][LiveStrategy] on_kline: {self.symbol} close={kline.get('close')}")

        # 更新历史：同一根 K 线（相同 open_time）更新最后一条，新周期才 append
        kline_ts = kline.get("timestamp")
        if (
            self._kline_history
            and kline_ts is not None
            and self._kline_history[-1].get("timestamp") == kline_ts
        ):
            self._kline_history[-1] = kline
        else:
            self._kline_history.append(kline)
        # 限制历史长度，避免内存无限增长
        max_history = 500
        if len(self._kline_history) > max_history:
            self._kline_history = self._kline_history[-max_history:]

        # 只在 K 线收盘时决策：@kline 流每次成交都推送（约 1~2 秒一次），若 bar 内
        # tick 也跑完整决策，策略的「收盘价」语义会退化成实时价——2026-10 BNB/USDT
        # 4h 的箱体上沿/中线在 bar 内被反复穿越，一周 10 次往返（含 2 分钟卖完再买）。
        # 回测契约是「每根已收盘 K 线决策一次」，实盘对齐它；bar 内 tick 只更新历史。
        # 需要 bar 内监控的策略走 SignalMonitor 验证通道（get_watch_signals）。
        # 数据源契约：WS K 线必须携带 is_closed（Binance 的 k.x）；REST 预热拉取的
        # K 线没有该字段，按未收盘处理、不触发决策。
        if not kline.get("is_closed"):
            return

        try:
            # per-session 并发控制：上一轮信号仍在处理时，丢弃本次 kline
            if self._signal_lock.locked():
                logger.debug(
                    f"[SF-01][LiveStrategy] skip kline {self.symbol}: "
                    f"previous signal still processing"
                )
                return
            async with self._signal_lock:
                await self._process_kline(kline)
        except Exception as e:
            logger.error(f"[LiveStrategyRunner] on_kline error: {e}", exc_info=True)

    async def _process_kline(self, kline: dict) -> None:
        """单根 kline 的策略管线（在 _signal_lock 保护下执行）。"""
        # 同步当前价格到 context
        self.strategy.ctx.set_price(
            self.symbol, Decimal(str(kline.get("close", "0")))
        )

        # 决策前同步真实持仓（C 修复）：ctx 只在启动时同步过一次，持仓被其他会话/
        # 手工改变后若沿用旧值，close_position() 会按旧数量下单（2026-09-20 事故：
        # ctx 记着 +17718、交易所实际为 0，卖出后把账户卖成了 −17718 空头）。
        # 同一根 bar 的多次 tick 推送按 TTL 节流；新 bar 强制刷新。
        bar_ts = kline.get("timestamp")
        is_new_bar = bar_ts is not None and bar_ts != self._last_bar_ts
        if bar_ts is not None:
            self._last_bar_ts = bar_ts
        if not await self._refresh_position_before_decision(force=is_new_bar):
            logger.debug(
                f"[LiveStrategyRunner] 持仓未知 → 跳过本轮 {self.symbol} 决策"
            )
            return

        # 调试：记录当前持仓状态
        logger.debug(
            f"[LiveStrategyRunner] _process_kline start: ctx.position={self.strategy.ctx.position}, "
            f"ctx._position={self.strategy.ctx._position}, "
            f"ctx._positions={self.strategy.ctx._positions}"
        )

        # Phase 2: 5-step pipeline with diagnostic logging
        universe = self.strategy.select_universe()
        insights = self.strategy.generate_insights(kline, self._kline_history)
        logger.debug(
            f"[LiveStrategyRunner] pipeline: universe={universe} "
            f"insights={len(insights)} "
            f"insight_details={[f'{i.direction} {i.symbol} conf={i.confidence}' for i in insights]}"
        )

        # 调试：构造 portfolio 前记录 context
        logger.debug(
            f"[LiveStrategyRunner] before construct_portfolio: context.position={self.strategy.ctx.position}"
        )
        targets = self.strategy.construct_portfolio(insights, self.strategy.ctx)
        logger.debug(
            f"[LiveStrategyRunner] portfolio: targets={len(targets)} "
            f"target_details={[f'{t.symbol} qty={t.target_quantity} reason={t.reason}' for t in targets]}"
        )

        safe_targets = self.strategy.apply_risk_filters(targets, self.strategy.ctx)
        filtered_count = len(targets) - len(safe_targets)
        if filtered_count > 0:
            logger.warning(
                f"[LiveStrategyRunner] risk filter: {filtered_count}/{len(targets)} targets rejected"
            )
        logger.debug(
            f"[LiveStrategyRunner] risk_passed: safe_targets={len(safe_targets)}"
        )

        signal_count = await self._dispatch_targets(safe_targets, kline)

        if signal_count == 0 and (insights or targets):
            logger.debug(
                f"[SF-02][LiveStrategyRunner] no signals dispatched this bar "
                f"(insights={len(insights)} targets={len(targets)} safe={len(safe_targets)})"
            )
        elif signal_count > 0:
            logger.info(
                f"[SF-02][LiveStrategyRunner] pipeline done: "
                f"universe={len(universe)} insights={len(insights)} "
                f"targets={len(targets)} safe={len(safe_targets)} signals={signal_count}"
            )

    async def _sync_position_from_exchange(self) -> bool:
        """从交易所同步当前持仓到 ctx，返回是否同步成功。

        启动时调用，确保策略知道实际持仓，避免重复开仓。
        2026-09-21（C 修复）：同步失败**不再静默按现有 ctx 继续**——置位
        `_position_synced=False`、记 ERROR、通知用户一次，决策路径据此跳过本轮。
        2026-09-27：失败→恢复的转换也配对一条通知（状态结束必须可见）。
        2026-10-08（修复 2，2026-10-07 box 平掉 donchian 共享仓事故跟进）：
        ctx.position 不再是「账户持仓」，而是**本会话的台账**——
          - 首次成功同步 = 采用：同账户+同品种另有活跃会话时账户仓位无法
            归属（可能是别人的仓）→ 采用 0 并通知；否则采用账户真值；
          - 后续同步 = 对账：账户有持仓而本会话空仓 = 外部仓位（且不在
            在途窗口内）→ 保持 0（绝不平别会话的仓）；账户仓位 < 台账
            → 台账下钳（不得卖出超出账户持有的量）。
        """
        try:
            from apps.trading.executor import OrderExecutor

            executor = OrderExecutor.get_instance()
            if not executor:
                await self._on_position_sync_failed(
                    "OrderExecutor not ready（交易框架未就绪）"
                )
                return False

            # 按**本会话的账户 id** 取适配器，不按交易所名：同交易所有两个账户时
            # 按交易所名解析只会拿到其中一个（后加载的），会把**别的账户**的
            # 持仓当成本会话的持仓同步进 ctx——决策据此下单就是真实的资金错误。
            adapter = executor.resolve_adapter(self.exchange_account_id)
            if not adapter:
                await self._on_position_sync_failed(
                    f"账户 {self.exchange_account_id} 的适配器未加载"
                )
                return False

            positions = await adapter.get_positions()
            symbol_norm = self.symbol.replace("/", "").upper()

            actual_qty = Decimal("0")
            for pos in positions:
                pos_symbol = pos.symbol.replace("/", "").upper()
                if pos_symbol == symbol_norm:
                    actual_qty = pos.quantity if pos.side == "long" else -pos.quantity
                    break

            # 账户级真值：_target_to_order 卖出侧钳制的唯一依据
            self._last_actual_position = actual_qty

            if not self._position_adopted:
                # —— 首次成功同步 = 采用 ——
                self._position_adopted = True
                conflict = await self._shared_account_conflict()
                if conflict is not None:
                    self.strategy.ctx.set_position(self.symbol, Decimal("0"))
                    logger.warning(
                        f"[LiveStrategyRunner] 共享账户+同品种会话 → 采用 "
                        f"{self.symbol}=0（不采用账户真值 {actual_qty}）: {conflict}"
                    )
                    await self._notify_shared_account(conflict)
                else:
                    self.strategy.ctx.set_position(self.symbol, actual_qty)
                    logger.info(
                        f"[LiveStrategyRunner] position adopted from exchange: "
                        f"{self.symbol} = {actual_qty}"
                    )
            else:
                # —— 后续同步 = 对账：只纠正「台账 vs 账户」的偏差，
                # 绝不把别会话的仓采进自己的台账 ——
                ctx_pos = self.strategy.ctx.position
                if ctx_pos == Decimal("0") and actual_qty > Decimal("0"):
                    # 账户有该品种持仓而本会话台账为 0。两种成因：
                    #   a) 本会话刚下发的平仓单仍在途/未确认（交易所态滞后）
                    #      → 外部持仓误报；
                    #   b) 另一会话/手工的仓位（2026-10-07 box 平掉 donchian
                    #      10401 仓的事故形态）。
                    # 按距上次成功下发的实时间隔区分：在途窗口（与同步 TTL 同长）
                    # 内静默保持 0；超出窗口才是真外部持仓 → 告警 + 通知一次。
                    in_flight = (
                        time.monotonic() - self._last_order_dispatch_ts
                        < self.POSITION_SYNC_MIN_INTERVAL
                    )
                    if in_flight:
                        logger.debug(
                            f"[LiveStrategyRunner] 账户 {self.symbol}={actual_qty} "
                            f"但台账=0，处于订单在途窗口 → 保持 0，不判外部持仓"
                        )
                    else:
                        logger.warning(
                            f"[LiveStrategyRunner] 检测到外部持仓: 账户 "
                            f"{self.symbol}={actual_qty} 但本会话台账=0，"
                            f"保持 0（不会平掉其他会话的仓位）"
                        )
                        await self._notify_foreign_position(actual_qty)
                elif ctx_pos > Decimal("0") and actual_qty < ctx_pos:
                    clamped = max(actual_qty, Decimal("0"))
                    self.strategy.ctx.set_position(self.symbol, clamped)
                    logger.warning(
                        f"[LiveStrategyRunner] 账户仓位低于会话台账，下钳: "
                        f"ctx {self.symbol} {ctx_pos} → {clamped} "
                        f"(账户真值 {actual_qty})"
                    )
                else:
                    logger.debug(
                        f"[LiveStrategyRunner] position reconciled (no change): "
                        f"ctx={ctx_pos} account={actual_qty}"
                    )
            await self._on_position_sync_ok()
            return True
        except Exception as e:
            await self._on_position_sync_failed(f"{type(e).__name__}: {e}")
            return False

    async def _on_position_sync_ok(self) -> None:
        """同步成功：记录状态与时间戳，并允许后续失败重新通知。

        失败→成功 转换发恢复通知：失败侧已告知「暂停下单」，状态的结束
        必须可见——静默恢复意味着用户永远不知道会话何时恢复交易。
        """
        recovered = self._position_synced is False
        if recovered:
            logger.info(
                f"[LiveStrategyRunner] 持仓同步已恢复: {self.symbol} "
                f"ctx.position={self.strategy.ctx.position}"
            )
        self._position_synced = True
        self._last_position_sync_ts = time.monotonic()
        if recovered and self._position_sync_failure_notified and self.user_id:
            await self._notify_position_sync_recovered()
        self._position_sync_failure_notified = False

    async def _on_position_sync_failed(self, reason: str) -> None:
        """同步失败：fail-loud + 通知用户一次（转为失败时），绝不静默当作空仓。"""
        already_notified = self._position_sync_failure_notified
        self._position_synced = False
        self._last_position_sync_ts = time.monotonic()
        logger.error(
            f"[LiveStrategyRunner] 持仓同步失败 → 本轮禁止下单 "
            f"({self.symbol}, session={self.live_session_id}): {reason}"
        )
        self._position_sync_failure_notified = True
        if already_notified or not self.user_id:
            return
        try:
            from apps.core.db_utils import db_async
            from apps.notify.models import Notification

            await db_async(
                lambda: Notification.objects.create(
                    user_id=self.user_id,
                    channel="web",
                    # 措辞说清三件事：冻的是**全部**订单（含平仓——本态与 halt
                    # 不同，策略的出场逻辑同样停摆）、是瞬态（自动重试）、会自愈
                    # （恢复后自动继续）。异常细节留在日志，不进通知。
                    message=(
                        f"⚠️ {self.symbol} 持仓同步失败，该会话暂不下任何订单（含平仓），"
                        f"正在自动重试，恢复后自动继续"
                    ),
                )
            )()
        except Exception as e:  # noqa: BLE001 - 通知失败不得影响主流程
            logger.error(
                f"[LiveStrategyRunner] 持仓同步失败通知写入失败 "
                f"(user={self.user_id}): {e}"
            )

    async def _notify_position_sync_recovered(self) -> None:
        """恢复通知：与失败通知成对，告知用户「暂停下单」状态已结束。"""
        try:
            from apps.core.db_utils import db_async
            from apps.notify.models import Notification

            await db_async(
                lambda: Notification.objects.create(
                    user_id=self.user_id,
                    channel="web",
                    message=f"✅ {self.symbol} 持仓同步已恢复，该会话已恢复下单",
                )
            )()
        except Exception as e:  # noqa: BLE001 - 通知失败不得影响主流程
            logger.error(
                f"[LiveStrategyRunner] 持仓同步恢复通知写入失败 "
                f"(user={self.user_id}): {e}"
            )

    async def _shared_account_conflict(self) -> str | None:
        """同交易所账户 + 同品种上是否有另一活跃会话（running/paused）；
        有则返回拒绝原因（与视图层 LiveSession.conflicting_active_session 同口径），
        无则 None。

        任何异常（无会话 id / 非 UUID / 账户行缺失 / DB 故障）一律返回 None →
        退回传统采用行为：本方法只决定**首次**同步采用 0 还是账户真值，DB 抖动时
        宁可按旧行为继续，也不阻断交易。
        """
        if not self.live_session_id:
            return None
        try:
            uid = uuid.UUID(str(self.live_session_id))
        except (ValueError, TypeError, AttributeError):
            # 测试/独立 runner 的 "sess-1" 之类 id：库里无会话行，视为独立
            return None
        try:
            from apps.core.db_utils import db_async
            from apps.trading.models import LiveSession

            def _check() -> str | None:
                # 传 pk：runner 手里只有账户 id 字符串（classmethod 兼容 pk/实例；
                # 账户行已删除时查询为空 → None → 传统采用）。
                return LiveSession.conflicting_active_session(
                    self.exchange_account_id, self.symbol, exclude_pk=uid
                )

            return await db_async(lambda: _check())()
        except Exception as e:  # noqa: BLE001 - 冲突检查失败不得阻断同步主流程
            logger.warning(
                f"[LiveStrategyRunner] 共享账户冲突检查失败，退回传统采用: {e}"
            )
            return None

    async def _notify_shared_account(self, reason: str) -> None:
        """一次性通知：启动时发现另一活跃会话占用同账户+同品种，本会话按 0 采用。

        本会话不会再依据别会话的仓位下单、更不会再平掉它（2026-10-07 box
        平掉 donchian 10401 仓的事故正源于「账户级仓位 + close_position」）。
        在用户停掉另一会话并重启本会话之前，本会话对该品种始终空仓。
        """
        if self._shared_account_notified or not self.user_id:
            return
        self._shared_account_notified = True
        try:
            from apps.core.db_utils import db_async
            from apps.notify.models import Notification

            await db_async(
                lambda: Notification.objects.create(
                    user_id=self.user_id,
                    channel="web",
                    message=(
                        f"⚠️ {self.symbol} 共享账户持仓冲突：{reason}"
                        f"本会话已按空仓处理，不会开/平其他会话的仓位；"
                        f"如需本会话交易该品种，请先停止另一会话再重启本会话"
                    ),
                )
            )()
        except Exception as e:  # noqa: BLE001 - 通知失败不得影响主流程
            logger.error(
                f"[LiveStrategyRunner] 共享账户冲突通知写入失败 "
                f"(user={self.user_id}): {e}"
            )

    async def _notify_foreign_position(self, actual_qty: Decimal) -> None:
        """一次性通知：账户存在不属于本会话的该品种持仓（手工/另一会话）。

        本会话台账保持 0、绝不碰它，但用户必须知情：无人管理的持仓正是
        2026-10-07 事故里「跌穿才触发」的前置状态。
        """
        if self._foreign_position_notified or not self.user_id:
            return
        self._foreign_position_notified = True
        try:
            from apps.core.db_utils import db_async
            from apps.notify.models import Notification

            await db_async(
                lambda: Notification.objects.create(
                    user_id=self.user_id,
                    channel="web",
                    message=(
                        f"⚠️ {self.symbol} 检测到非本会话持仓：账户现有 "
                        f"{actual_qty}，本会话未参与开仓；本会话已按空仓处理、"
                        f"不会平掉该仓位。如属误开请自行处理"
                    ),
                )
            )()
        except Exception as e:  # noqa: BLE001 - 通知失败不得影响主流程
            logger.error(
                f"[LiveStrategyRunner] 外部持仓通知写入失败 "
                f"(user={self.user_id}): {e}"
            )

    async def _refresh_position_before_decision(self, *, force: bool) -> bool:
        """决策前确保 ctx.position 是交易所真实持仓。

        返回 False 表示持仓未知/同步失败 → 调用方必须跳过本轮决策。

        节流策略（K 线流对同一根 bar 会反复推送，约 1~2 秒一次，不能每 tick 打 API）：
          - force=True（新 bar 到来）：立即同步，保证每根 bar 至少一次真值
          - TTL 内：沿用上次结果（成功→可交易；失败→按不可交易处理，不重复打 API）
          - TTL 外：重新同步（失败态因此每 15s 退避重试一次，而不是每 tick 重试）
        """
        fresh = (
            time.monotonic() - self._last_position_sync_ts
        ) < self.POSITION_SYNC_MIN_INTERVAL
        if not force and fresh:
            return bool(self._position_synced)
        return await self._sync_position_from_exchange()

    async def _subscribe_kline(self) -> None:
        """订阅 K 线数据"""
        from apps.datasource.registry import DataSourceRegistry
        from apps.datasource.base import DataType, KlineInterval, MarketType

        try:
            interval = KlineInterval(self.timeframe)
        except ValueError:
            interval = KlineInterval.H1
            logger.warning(
                f"[LiveStrategyRunner] unknown timeframe {self.timeframe}, defaulting to 1h"
            )

        registered = DataSourceRegistry.list_registered()
        logger.info(
            f"[LiveStrategyRunner] _subscribe_kline: "
            f"symbol={self.symbol} tf={self.timeframe} "
            f"registered_sources={registered}"
        )
        if not registered:
            logger.error(
                "[LiveStrategyRunner] NO data sources registered! "
                "K-line subscription will be empty. "
                "Check that a DataSource (e.g. BinanceDataSource) is properly registered."
            )

        subscribed_count = 0
        for source_name in registered:
            # Trigger lazy loading if not yet loaded
            if not DataSourceRegistry.is_loaded(source_name):
                logger.info(
                    f"[LiveStrategyRunner] source '{source_name}' not yet loaded, triggering lazy load via get()"
                )

            ds = DataSourceRegistry.get(source_name)

            if not DataSourceRegistry.is_loaded(source_name):
                logger.error(
                    f"[LiveStrategyRunner] source '{source_name}' failed to load (get() returned but not in registry instances)"
                )
                continue

            if not ds.is_connected():
                logger.warning(
                    f"[LiveStrategyRunner] source '{source_name}' NOT connected — skipping"
                )
                continue

            if DataType.KLINE not in ds.supported_data_types:
                logger.warning(
                    f"[LiveStrategyRunner] source '{source_name}' does not support KLINE "
                    f"(supported: {ds.supported_data_types}) — skipping"
                )
                continue

            try:
                await ds.subscribe(
                    symbol=self.symbol,
                    data_type=DataType.KLINE,
                    interval=interval,
                    market_type=MarketType.SPOT,
                    callback=self.on_kline,
                )
                logger.info(
                    f"[LiveStrategyRunner] subscribed {source_name} kline "
                    f"{self.symbol} @{self.timeframe}"
                )
                subscribed_count += 1
            except Exception as e:
                logger.warning(
                    f"[LiveStrategyRunner] failed to subscribe {source_name}: {e}",
                    exc_info=True,
                )

        if subscribed_count == 0:
            logger.error(
                f"[LiveStrategyRunner] SUBSCRIPTION FAILED: 0 sources subscribed for "
                f"{self.symbol}@{self.timeframe}. Live trading will NOT receive kline data."
            )
        else:
            logger.info(
                f"[LiveStrategyRunner] subscription complete: {subscribed_count} source(s) active"
            )

    async def _unsubscribe_kline(self) -> None:
        """取消 K 线订阅"""
        from apps.datasource.registry import DataSourceRegistry
        from apps.datasource.base import DataType, KlineInterval, MarketType

        try:
            interval = KlineInterval(self.timeframe)
        except ValueError:
            interval = KlineInterval.H1

        for source_name in DataSourceRegistry.list_registered():
            if not DataSourceRegistry.is_loaded(source_name):
                continue

            ds = DataSourceRegistry.get(source_name)
            try:
                await ds.unsubscribe(
                    symbol=self.symbol,
                    data_type=DataType.KLINE,
                    interval=interval,
                    market_type=MarketType.SPOT,
                    callback=self.on_kline,
                )
            except Exception:
                pass
    async def _dispatch_signal(self, signal: "OrderSignal") -> bool:
        """分发策略信号到 Redis Stream，返回是否成功分发。"""
        if not self._dispatcher:
            logger.error(
                "[LiveStrategyRunner] _dispatcher is None! Signal will be dropped. "
                "This means SignalDispatcher was not initialized in start()."
            )
            return False

        try:
            msg_id = await self._dispatcher.dispatch_with_risk_check(
                signal=signal,
                symbol=self.symbol,
                exchange_account_id=self.exchange_account_id,
                user_id=self.user_id,
                live_session_id=self.live_session_id,
            )
            if msg_id:
                logger.info(
                    f"[LiveStrategyRunner] signal dispatched: {signal.signal_name} "
                    f"msg_id={msg_id}"
                )
                return True
            else:
                logger.warning(
                    f"[LiveStrategyRunner] signal REJECTED or dropped by risk check: "
                    f"{signal.signal_name} {signal.side} {signal.quantity} {self.symbol}"
                )
                return False
        except Exception as e:
            logger.error(
                f"[LiveStrategyRunner] failed to dispatch signal: {e}", exc_info=True
            )
            return False

    def _apply_signal_to_context(
        self, signal: "OrderSignal", estimated_price: "Decimal"
    ) -> None:
        """乐观更新 ctx.position/balance（假设市价单立即按 estimated_price 成交）。

        这是 C4 修复：实盘模式下 ctx.position 持续追踪预期持仓。
        回测模式由 BacktestEngine._execute_buy/_execute_sell 同步更新。
        """
        if signal.side == "buy":
            cost = signal.quantity * estimated_price
            self.strategy.ctx.position += signal.quantity
            self.strategy.ctx.set_position(self.symbol, self.strategy.ctx.position)
            self.strategy.ctx.balance -= cost
        elif signal.side == "sell":
            proceeds = signal.quantity * estimated_price
            self.strategy.ctx.position -= signal.quantity
            self.strategy.ctx.set_position(self.symbol, self.strategy.ctx.position)
            self.strategy.ctx.balance += proceeds

        logger.debug(
            f"[SF-11][LiveStrategyRunner] ctx updated: "
            f"position={self.strategy.ctx.position} "
            f"balance={self.strategy.ctx.balance}"
        )

    def _target_to_order(self, target: "PortfolioTarget") -> "OrderSignal | None":
        """将目标持仓差量转化为订单信号。

        卖出侧钳制（2026-10-08 修复 2）：ctx.position 是会话级台账，可能**大于**
        账户真值（另一会话平掉/部分平掉过）。卖量 = min(台账差量, 最近同步的
        账户真值)——否则超出部分在 reduceOnly 下变反向开仓（2026-09-20
        -17718 空头事故），或平掉别会话的仓位。账户真值已 0 时跳过订单
        （台账会在下次同步被下钳）。
        """
        diff = target.target_quantity - self.strategy.ctx.position
        if diff == 0:
            return None
        if diff > 0:
            return self.strategy.ctx.buy(diff, signal_name=target.reason)
        sell_qty = abs(diff)
        actual = self._last_actual_position
        if actual is not None and actual < sell_qty:
            clamped = max(actual, Decimal("0"))
            if clamped == Decimal("0"):
                logger.info(
                    f"[LiveStrategyRunner] 卖单被账户真值钳制为 0，跳过订单: "
                    f"ledger={abs(diff)} account={actual} ({self.symbol})"
                )
                return None
            logger.warning(
                f"[LiveStrategyRunner] 卖单按账户真值钳制: "
                f"ledger={abs(diff)} → {clamped} ({self.symbol})"
            )
            sell_qty = clamped
        # 卖到空仓 = 平仓：策略侧 close_position() 的意图在 Phase 2 转目标持仓时
        # 丢了（Insight/PortfolioTarget 没有这个字段，见 base.construct_portfolio），
        # 这里按「目标=空仓」还原。下游两处依赖它：以 reduce_only 传入 RiskGuard 获得
        # 豁免（signals.py / executor，语义在 guard 定义），以及 executor 把它翻译成
        # reduceOnly（只减不增）——后者是「重复平仓单变反向开仓」的唯一交易所级兜底
        # （2026-09-27 BNB 双平手账）。
        is_close = target.target_quantity == Decimal("0")
        return self.strategy.ctx.sell(
            sell_qty, signal_name=target.reason, is_close_position=is_close
        )

    async def _dispatch_targets(
        self, targets: "list[PortfolioTarget]", kline: dict
    ) -> int:
        """遍历 safe_targets，过滤重复入场后分发信号。返回实际分发数量。

        防重复规则：同一 symbol 已有同方向入场记录时，若 target 仍要求同向加仓
        （target_quantity > ctx.position），跳过该 target。平仓（target_quantity=0）
        时清除方向记录，允许反向入场。
        """
        count = 0
        for target in targets:
            direction = (
                "buy" if target.target_quantity > self.strategy.ctx.position
                else "sell"
            )
            prev = self._entry_direction.get(target.symbol)
            if prev is not None and direction == prev and target.target_quantity > 0:
                logger.debug(
                    f"[SF-02][LiveStrategyRunner] skip repeated {direction} "
                    f"for {target.symbol} (target={target.target_quantity} "
                    f"position={self.strategy.ctx.position})"
                )
                continue

            signal = self._target_to_order(target)
            if signal is None:
                logger.debug(
                    f"[SF-02][LiveStrategyRunner] _target_to_order returned None for "
                    f"target {target.symbol} (diff=0, no action needed)"
                )
                continue
            logger.info(
                f"[SF-03][LiveStrategyRunner] signal generated: "
                f"{signal.signal_name} {signal.side} qty={signal.quantity} "
                f"symbol={self.symbol}"
            )
            dispatched = await self._dispatch_signal(signal)
            if dispatched:
                estimated_price = Decimal(str(kline.get("close", "0")))
                self._apply_signal_to_context(signal, estimated_price)
                # 订单在途：交易所尚未处理它，乐观状态比交易所真值**新**。刷新同步
                # 水位，把下一次 TTL 重同步推迟 15s——否则在途窗口（秒级）内一次
                # 例行 TTL 到期重同步会把「还没反映本单的旧持仓」写回 ctx，覆盖
                # 乐观值，策略据此再发一张同量平仓单，在合约侧变成反向开仓
                # （2026-09-27 BNB 双平手账）。若订单实际被交易所拒单，最迟 15s
                # 后（或下一根新 bar 的强制同步）重取真值，偏差有界。
                self._last_position_sync_ts = time.monotonic()
                # 对账侧的在途窗口依据：平仓单在途期间「账户仍有持仓而台账=0」
                # 是本单未成交，不是外部持仓（否则误报，见 _sync_position_from_exchange）
                self._last_order_dispatch_ts = time.monotonic()
                if target.target_quantity <= 0:
                    self._entry_direction.pop(target.symbol, None)
                else:
                    self._entry_direction[target.symbol] = signal.side
                count += 1
            else:
                logger.debug(
                    f"[SF-02][LiveStrategyRunner] signal rejected, ctx not updated: "
                    f"{signal.signal_name} {signal.side} {signal.quantity}"
                )
        return count

    async def load_initial_history(self, limit: int = 500) -> None:
        """
        启动时加载历史 K 线数据，确保策略有足够的历史数据运行。

        优先从 DataSource REST API 直接拉取交易所最新 K 线（最可靠，
        不依赖本地缓存），失败时降级到 DataStore 本地存储。
        """
        # 1) 优先：直接从数据源 REST API 拉取
        try:
            klines = await self._fetch_klines_from_source(limit=limit)
            if klines:
                self._kline_history = klines
                logger.info(
                    f"[LiveStrategyRunner] pulled {len(klines)} historical klines "
                    f"for {self.symbol} via DataSource REST"
                )
                return
        except Exception as e:
            logger.warning(
                f"[LiveStrategyRunner] REST kline fetch failed: {e}, "
                f"falling back to DataStore"
            )

        # 2) 降级：本地 DataStore
        try:
            from apps.datasource.store import get_data_store

            store = get_data_store()
            # DataStore 里的 K 线按数据源原生格式存储（Binance: DOGEUSDT），
            # 而 self.symbol 是 ccxt 格式（DOGE/USDT），查缓存前必须去掉 "/"，
            # 否则永远未命中；get_latest 按时间倒序（新→旧）返回，需重新排回
            # 升序，与主路径 _fetch_klines_from_source 保持一致（末尾=最新 K 线）
            klines = store.get_latest(
                "kline", self.symbol.replace("/", ""), limit=limit
            )
            if klines:
                klines.sort(key=lambda k: k.get("timestamp", 0))
                self._kline_history = klines
                logger.info(
                    f"[LiveStrategyRunner] loaded {len(klines)} historical klines "
                    f"for {self.symbol} from DataStore"
                )
            else:
                logger.warning(
                    f"[LiveStrategyRunner] no historical klines available for "
                    f"{self.symbol} (requested {limit}). Indicators may be invalid "
                    f"until history accumulates."
                )
        except Exception as e:
            logger.warning(
                f"[LiveStrategyRunner] failed to load historical klines: {e}"
            )

    async def _fetch_klines_from_source(self, limit: int) -> list[dict]:
        """遍历已注册且已连接的 DataSource，返回最新 limit 根 K 线。"""
        from apps.datasource.registry import DataSourceRegistry
        from apps.datasource.base import DataType, KlineInterval, MarketType

        try:
            interval = KlineInterval(self.timeframe)
        except ValueError:
            interval = KlineInterval.H1

        for source_name in DataSourceRegistry.list_registered():
            if not DataSourceRegistry.is_loaded(source_name):
                continue
            ds = DataSourceRegistry.get(source_name)
            if not ds.is_connected():
                continue
            if DataType.KLINE not in ds.supported_data_types:
                continue

            try:
                klines = await ds.fetch_klines(
                    symbol=self.symbol,
                    interval=interval,
                    market_type=MarketType.SPOT,
                    limit=limit,
                )
                if klines:
                    # 按时间升序，便于 append/update 逻辑一致
                    klines.sort(key=lambda k: k.get("timestamp", 0))
                    logger.info(
                        f"[LiveStrategyRunner] source '{source_name}' provided "
                        f"{len(klines)} historical klines"
                    )
                    return klines
            except Exception as e:
                logger.warning(
                    f"[LiveStrategyRunner] source '{source_name}' fetch_klines "
                    f"failed: {e}"
                )
        return []

    async def _register_signal_monitors(self) -> None:
        """将策略的 get_watch_signals() 注册到 SignalMonitor 数据库。"""
        watch_signals = self.strategy.get_watch_signals()
        if not watch_signals:
            return

        if not self.user_id:
            logger.warning(
                "[LiveStrategyRunner] cannot register signal monitors: no user_id"
            )
            return

        from asgiref.sync import sync_to_async
        from apps.signal_monitor.models import SignalMonitor

        @sync_to_async
        def _create_monitor(ws: dict) -> None:
            SignalMonitor.objects.update_or_create(
                strategy_name=self.strategy.name,
                live_session_id=self.live_session_id or "",
                symbol=self.symbol,
                interval=ws.get("interval", self.timeframe),
                defaults={
                    "name": f"{self.strategy.name}:{ws.get('indicator_type', '')}",
                    "user_id": self.user_id,
                    "indicator_type": ws["indicator_type"],
                    "indicator_params": ws.get("indicator_params", {}),
                    "condition": ws["condition"],
                    "trigger_type": ws.get("trigger_type", "continuous"),
                    "action_type": "validate_strategy",
                    "status": "active",
                },
            )

        subscribed: set[tuple[str, str]] = set()

        for ws in watch_signals:
            try:
                await _create_monitor(ws)
                logger.info(
                    "[LiveStrategyRunner] registered signal monitor: %s %s %s",
                    self.strategy.name,
                    ws.get("indicator_type"),
                    ws.get("interval"),
                )

                # 为新增的监控订阅 WebSocket K 线回调，确保实时信号检查生效
                interval_str = ws.get("interval", self.timeframe)
                key = (self.symbol, interval_str)
                if key not in subscribed:
                    subscribed.add(key)
                    from apps.agent.frame_manager import FrameManager

                    fm = FrameManager.get_instance()
                    await fm.subscribe_signal_klines(
                        symbol=self.symbol, interval_str=interval_str
                    )
            except Exception as e:
                logger.error(
                    "[LiveStrategyRunner] failed to register signal monitor: %s",
                    e,
                )

    async def _unregister_signal_monitors(self) -> None:
        """清理该实盘会话的 SignalMonitor 记录。"""
        if not self.live_session_id:
            return

        from asgiref.sync import sync_to_async
        from apps.signal_monitor.models import SignalMonitor

        @sync_to_async
        def _cleanup():
            SignalMonitor.objects.filter(
                live_session_id=self.live_session_id,
                strategy_name=self.strategy.name,
            ).update(status="expired")

        try:
            await _cleanup()
            logger.info(
                "[LiveStrategyRunner] unregistered signal monitors for %s/%s",
                self.strategy.name,
                self.live_session_id,
            )
        except Exception as e:
            logger.error(
                "[LiveStrategyRunner] failed to unregister signal monitors: %s", e
            )

    async def _listen_validation(self) -> None:
        """后台监听 Redis List，消费 SignalMonitor 发布的策略验证事件。"""
        import json

        import redis.asyncio as aioredis
        from django.conf import settings

        key = f"strategy:validate:{self.live_session_id}"
        r = None

        try:
            r = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
            while self._running:
                try:
                    result = await r.blpop(key, timeout=5)
                    if result is None:
                        continue
                    _list_key, data = result
                    event = json.loads(data)
                    await self._on_validate_trigger(event)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.error(
                        "[LiveStrategyRunner] validation listener error: %s",
                        e,
                        exc_info=True,
                    )
                    await asyncio.sleep(1)
        finally:
            if r is not None:
                await r.aclose()

    async def _on_validate_trigger(self, event: dict) -> None:
        """SignalMonitor 触发后，运行完整策略验证。

        Args:
            event: {
                monitor_id, strategy_name, symbol, interval, trigger_value
            }
        """
        logger.info(
            "[SF-10][LiveStrategyRunner] validate trigger received: %s %s",
            event.get("strategy_name"),
            event.get("symbol"),
        )

        try:
            # 确保有足够的历史数据
            if len(self._kline_history) < 10:
                await self.load_initial_history(limit=200)
                if len(self._kline_history) < 10:
                    logger.warning(
                        "[LiveStrategyRunner] insufficient kline history for validation"
                    )
                    return

            # 持仓同步（C 修复）：验证事件可能高频触发，按最小间隔节流
            if not await self._refresh_position_before_decision(force=False):
                logger.debug(
                    "[LiveStrategyRunner] 持仓未知 → 跳过本轮验证触发 "
                    f"({self.symbol})"
                )
                return

            # 用最新 K 线运行完整策略逻辑 (Phase 2 pipeline)
            latest_kline = self._kline_history[-1]
            self.strategy.ctx.set_price(
                self.symbol, Decimal(str(latest_kline.get("close", "0")))
            )
            _ = self.strategy.select_universe()
            insights = self.strategy.generate_insights(
                latest_kline, self._kline_history
            )
            targets = self.strategy.construct_portfolio(insights, self.strategy.ctx)
            safe_targets = self.strategy.apply_risk_filters(targets, self.strategy.ctx)
            dispatched = await self._dispatch_targets(safe_targets, latest_kline)
            if dispatched == 0:
                logger.info(
                    "[LiveStrategyRunner] strategy rejected signal after full validation"
                )
        except Exception as e:
            logger.error(
                "[LiveStrategyRunner] validate trigger error: %s", e, exc_info=True
            )
