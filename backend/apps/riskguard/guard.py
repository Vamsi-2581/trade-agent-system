"""
RiskGuard - 风控守卫

前置校验（pre_trade_check）：
- 豁免：reduce_only（平仓 / 减仓）单 → 整单免风控。豁免语义**只在这里定义**，
  入口（executor / signals）只传标志、不预跳过（CONTEXT.md「平仓放行标记」）
- 停止判定（halt 状态机，ADR 0001）——只查开新仓
- 单笔仓位上限
- 最大回撤限制
- 日内交易次数

熔断器（circuit breaker）**不在前置校验链上**：ADR 0001 把它降级成 halt 状态机的一个
触发源，于是「它自己的状态能不能拒单」这个问题的答案是不能——否则就存在一个绕过 halt
状态机的隐藏停机开关。见 `pre_trade_check` 里第 1 步的注释。

实时监控（monitor loop）：
- 浮亏超阈值预警
- 止损触发强平

由 FrameManager 管理生命周期，非独立进程（ADR-003）。
"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from typing import TYPE_CHECKING, Optional, Tuple

from apps.core.db_utils import db_async

if TYPE_CHECKING:
    from apps.regime.halt import HaltVerdict
    from apps.trading.adapters.base import OrderRequest

from apps.riskguard.circuit_breaker import CircuitBreaker

logger = logging.getLogger(__name__)

#: 停止判定查不出来时的拒单理由（Q2 定死的 fail-closed 口径）。
#:
#: 写成模块常量而不是就地拼串，是因为它会被测试与告警文案引用——两处各拼一遍迟早
#: 会漂，而「理由文案变了」这种事不会有人报上来。
HALT_LOOKUP_FAILED_REASON = "熔断状态查询失败，按保守方向处理"


def _strategy_of_session(live_session_id: str | None) -> str | None:
    """这张单属于哪个策略。**下单通路上「会话 → 策略」的唯一换算口。**

    停止声明的策略档钉在 `Strategy.id` 上（`halt.strategy_scope`），而一张单自己只答
    得出会话 id——这一跳是两者之间唯一的桥。少了它，策略档的停用永远不生效，而那种
    失效不会红任何别的东西：日报上写着「已停用」，单照常出去。

    查不出来就返回 ``None``，**既不抛也不按全市场处理**：``None`` 在 `halt._matches`
    里的语义正是「策略档行不认它生效」，于是退化成**停用没生效**（池化与停用日报里
    看得见），而不是「全场莫名停摆」。会话行不见了，该管的是那条会话，不是把这一单
    拦下来。

    查询本身抛（例如 id 根本不是 UUID）**不在这里吞**：那属于「查不出状态」，由
    `pre_trade_check` 的 fail-closed 兜着，取向与第 0 步其余部分一致。
    """
    if not live_session_id:
        return None
    from apps.trading.models import LiveSession

    strategy_id = (
        LiveSession.objects.filter(id=live_session_id)
        .values_list("strategy_id", flat=True)
        .first()
    )
    return str(strategy_id) if strategy_id else None


def _halt_verdict(request: "OrderRequest") -> HaltVerdict:
    """一张单 + 此刻的声明表 → 停止判定。**同步**，由 `_halt_block_reason` 经 `db_async` 进来。

    两次读库放在**同一次线程切换**里：先由会话换出策略 id，再拿它去求那一组声明。
    分成两次 `db_async` 的话，中间那一刻正好换了策略，就会出现「拿 A 的身份判 B 的那张
    单」——而这一层要的恰恰是「此刻的这一张单」。
    """
    from apps.regime import halt

    return halt.halt_layers(
        request.symbol, _strategy_of_session(request.live_session_id)
    )


class RiskGuard:
    """
    风控守卫（随交易框架或辅助框架启动）。
    """

    _instance: "RiskGuard | None" = None

    # 风控阈值。曾经有一条「可由管理员通过 RiskConfig 覆盖」的注释，但实现从未存在
    # ——那个配置面 guard 从不读取（2026-10-12 随 apps.risk 摘除）。现在这四个类常量
    # 就是阈值的唯一配置面，想改它们只有一个定义可改。
    MAX_POSITION_RATIO = Decimal("0.20")  # 单仓不超过总资产20%（现货）
    MAX_DAILY_DRAWDOWN = Decimal("0.05")  # 日内最大回撤5%
    MAX_DAILY_TRADES = 50  # 日内最大交易次数
    FLOATING_LOSS_ALERT = Decimal("-0.03")  # 浮亏-3%预警

    # ---- 合约（perp）模式阈值（Ticket 3 共识参数）----
    #: 合约杠杆上限。超过即拒：杠杆是把上面所有比例类阈值同时放大的旋钮，
    #: 必须最先卡住。
    MAX_LEVERAGE = 5
    #: 合约单仓上限（名义 / 总资产）。现货的 20% 口径不适用于带杠杆的合约。
    MAX_POSITION_RATIO_PERP = Decimal("0.10")
    #: 组合总名义敞口硬顶：Σ|position_value × leverage| / total_equity。
    MAX_TOTAL_NOTIONAL_EXPOSURE = Decimal("1.00")

    def __init__(self, mode: str = "trading"):
        self.mode = mode
        self._running = False
        self._monitor_task: asyncio.Task | None = None
        # 持仓抓取连续失败计数（成功后归零）：用于区分偶发超时与适配器卡死
        self._position_fetch_failures = 0

    @classmethod
    def get_instance(cls) -> "RiskGuard | None":
        return cls._instance

    async def start(self) -> None:
        RiskGuard._instance = self
        self._running = True
        if self.mode in ("trading", "monitor"):
            self._monitor_task = asyncio.create_task(self._monitor_loop())
        logger.info(f"RiskGuard started (mode={self.mode})")

    async def stop(self) -> None:
        self._running = False
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
            self._monitor_task = None
        RiskGuard._instance = None
        logger.info("RiskGuard stopped")

    # ------------------------------------------------------------------ #
    #  前置校验                                                          #
    # ------------------------------------------------------------------ #

    async def pre_trade_check(
        self, request: "OrderRequest", user_id: str | None
    ) -> Tuple[bool, str]:
        """
        返回 (approved, reason)，approved=False 时 OrderExecutor 禁止下单。

        校验顺序：
        - 豁免（不算步）：reduce_only → 整单免风控，直接放行。「这张单拦不拦」只有
          一个答案，豁免语义只在这里定义——入口（executor / signals）不再预跳过，
          只传标志。
        0. 停止判定（halt 状态机，只查开新仓）
        1. 熔断器检查（已禁用，ADR 0001）
        2. 日内交易次数
        3. 仓位上限（3a 杠杆 / 3b 单仓比例 / 3c 总名义敞口）
        4. 日内回撤

        **RiskEvent 归档不对称**：只有第 3 步的三条 perp 校验（3a/3b/3c）拒单时写
        ``RiskEvent(P0, hard_limit)``；第 0/2/4 步的拒单不写 RiskEvent（沿用历史行为，
        未纳入本次 P0）。读者别默认所有拒单都归档。

        **第 0 步排在 ``user_id`` 判空之前**，这是它与后面四步的根本区别：后面四步问的
        都是「这个人还能不能下单」（次数、仓位、回撤都是按人的额度），而停止判定问的是
        「这个系统此刻还让不让开新仓」——它不按人裁剪（CONTEXT.md:172）。放在判空之后
        的话，任何拿不到 ``user_id`` 的调用点（纸面交易、内部路径）都能绕过熔断，
        而熔断恰恰是最不该有例外的那个。
        """
        logger.info(f"[RiskGuard] pre_trade_check: {request.symbol} {request.side}")

        # 豁免（唯一定义，唯一实现）：reduce_only → 整单免风控。
        #
        # 「这张单拦不拦」只能有一个答案。2026-10-12 合一前是三个：executor / signals
        # 两个入口对平仓单整段预跳过 guard，而这里的分支只免 halt、次数/仓位/回撤仍查
        # （且生产上那个分支只有测试在钉）。现在入口不再预跳过，只传标志，豁免边界由
        # `test_guard_halt.py::TestReduceOnly` 钉死——未来任何新入口把标志传进来都得到
        # 同一个答案，不会再踩出第三种口径。
        #
        # 整单免的原因：熔断想停的是「加仓」，减仓是熔断时唯一想让它动起来的事
        # （CONTEXT.md:47）；按人的额度（次数/仓位/回撤）问的是「这个人还能不能开新仓」，
        # 减仓单不是开仓。查库失败（下面的 fail-closed）同样只约束开仓单：数据库挂了
        # 不能拦一次减仓。
        if request.reduce_only:
            logger.info(
                f"[RiskGuard] {request.symbol} {request.side} reduce_only — 整单免除前置校验"
            )
            return True, "OK"

        # 0. 停止判定（halt 状态机，第②段接线）
        logger.info("[RiskGuard] step 0: halt 状态机判定")
        try:
            halt_reason = await self._halt_block_reason(request)
        except Exception:  # noqa: BLE001 - 查不出来时的取向由下面这行决定
            # **fail-closed**：查不到状态就按「可能在拦」处理。反方向（查不到就放行）
            # 会让「数据库挂了」变成一次无声的机制失效——而那正是熔断最需要生效的时刻。
            halt_reason = HALT_LOOKUP_FAILED_REASON
            logger.error(
                "[RiskGuard] step 0 停止判定查询失败，按保守方向处理（拒开仓）",
                exc_info=True,
            )
        if halt_reason:
            logger.warning(f"[RiskGuard] REJECTED: {halt_reason}")
            # 无 user_id 时不走 `_reject`：那条路要发通知，而「发给谁」这里答不出来。
            # 判定的结果不变（仍然是拒绝），只是没人可告知。
            if user_id:
                return await self._reject(user_id, halt_reason)
            return False, halt_reason

        if not user_id:
            return True, "OK"

        # 1. 熔断器检查（已禁用——ADR 0001 把它降级成 halt 状态机的一个**触发源**）
        #
        # 这里**刻意保持禁用**，而且不是「暂时」：熔断器状态若能拒单，就等于存在一个绕过
        # halt 状态机的隐藏停机开关——用户看到的拒单理由会是「熔断」，而框架的停机口径
        # 在声明表里，两边对不上。要它重新具备停机能力，路径是让它去投递声明，不是在这
        # 里加回一个 if。`apps/riskguard/tests/test_guard.py` 有一条测试钉的就是「它根本
        # 没被问过」，而不是「它打开时不拒单」。
        #
        # 手动触发那条线已于 2026-10-12 兑现（ADR 0001 的欠账）：`manage.py manual_halt`
        # → `apps/regime/manual_halt.trip` → 一行 ``HaltTrigger.MANUAL`` 声明，拦单走的
        # 正是上面第 0 步的停止判定，与事件熔断同一个口——不是这里。
        logger.info("[RiskGuard] step 1: circuit breaker (disabled)")

        # 2. 日内交易次数
        logger.info("[RiskGuard] step 2: daily trade count check")
        daily_count = await self._get_daily_trade_count(user_id)
        logger.info(f"[RiskGuard] step 2 done: count={daily_count}")
        if daily_count >= self.MAX_DAILY_TRADES:
            reason = f"日内交易次数已达上限 {self.MAX_DAILY_TRADES}"
            logger.warning(f"[RiskGuard] REJECTED: {reason}")
            return await self._reject(user_id, reason)

        # 3. 仓位上限（含 3a 杠杆 / 3b 合约单仓 / 3c 总名义敞口，见下面各方法）
        logger.info("[RiskGuard] step 3: position limit check")
        # 3a 杠杆上限（最便宜的本地判定，排最前）
        leverage_reason = await self._check_leverage(request)
        if leverage_reason:
            logger.warning(f"[RiskGuard] REJECTED: {leverage_reason}")
            return await self._reject(
                user_id,
                leverage_reason,
                record_event=True,
                live_session_id=request.live_session_id,
            )
        # 3c 总名义敞口（本单成交后 Σ|value × leverage| / equity）
        exposure_reason = await self._check_total_notional_exposure(request, user_id)
        if exposure_reason:
            logger.warning(f"[RiskGuard] REJECTED: {exposure_reason}")
            return await self._reject(
                user_id,
                exposure_reason,
                record_event=True,
                live_session_id=request.live_session_id,
            )
        position_ok, reason = await self._check_position_limit(request, user_id)
        logger.info(f"[RiskGuard] step 3 done: ok={position_ok}")
        if not position_ok:
            logger.warning(f"[RiskGuard] REJECTED: {reason}")
            return await self._reject(
                user_id,
                reason,
                record_event=True,
                live_session_id=request.live_session_id,
            )

        # 4. 日内回撤
        logger.info("[RiskGuard] step 4: drawdown check")
        drawdown_ok, reason = await self._check_drawdown(user_id)
        logger.info(f"[RiskGuard] step 4 done: ok={drawdown_ok}")
        if not drawdown_ok:
            logger.warning(f"[RiskGuard] REJECTED: {reason}")
            return await self._reject(user_id, reason)

        return True, "OK"

    async def _halt_block_reason(self, request: "OrderRequest") -> str:
        """这一张单此刻有没有被停止声明挡住；挡住时返回拒绝理由，空串 = 放行。

        **这是第 0 步唯一的取数口，故意留成一个方法**：`pre_trade_check` 的调用方遍布
        下单通路（executor / paper_trader / strategy_engine），而在没有数据库的测试里
        它是唯一需要被打桩的地方。把 `db_async(...)` 直接写在 `pre_trade_check` 里，
        每个测前置校验的用例都得去 patch `apps.regime.halt.halt_layers`——那是拿被测
        对象的内部结构当接口。

        判定本身一行都不在这里：它全在 `apps.regime.halt`（唯一的停止抽象，ADR 0001）。
        这里只负责把**一张单**翻译成那两个参数。``strategy_id`` 由
        `_strategy_of_session` 从 ``request.live_session_id`` 换出来：第②段下单通路上
        这个字段一直是 ``None``，所以策略档不生效；第③段把它接上之后，`halt._matches`
        里那条策略档判据才第一次真正生效。
        """
        verdict = await db_async(_halt_verdict)(request)
        return verdict.reason

    async def _reject(
        self,
        user_id: str,
        reason: str,
        *,
        record_event: bool = False,
        live_session_id: Optional[str] = None,
    ) -> Tuple[bool, str]:
        """拒绝下单**并让用户看见**，然后返回 (False, reason)。

        2026-09-22：`pre_trade_check` 返回 False 时 `OrderExecutor` 抛
        PermissionError，而这发生在订单落库**之前**——既没有订单行，也不会走
        `record_order_failure` 那条通知。用户的意图于是被静默丢弃，与
        `record_order_failure` 里记的 2026-09-21 事故是同一种病。
        通知失败只记日志，绝不改变「拒绝」这个决定本身。

        ``record_event=True``（Ticket 3：额度类新拒单——杠杆 / 单仓 / 总敞口）时
        另写一条 ``RiskEvent``（type=hard_limit，message 即带具体数字的 reason）。
        写入失败只记日志：档案缺一行不能变成二次拒单或放行。

        ``live_session_id``（spec #19 D7，#24）：额度类命中归集到会话的唯一入口。
        ``OrderRequest.live_session_id`` 在下单通路上现成有（第 0 步 halt 已经用它做
        「会话 → 策略」换算），传下来即可。**浮亏预警不经这里**（它是账户级循环，
        手里没有会话，行落 NULL——#24 已确认口径：risk_hits 只算 hard_limit）。
        归集失败只记日志：档案缺一列归属不能变成二次拒单或放行，与上面同一条纪律。
        """
        from apps.trading.alerts import notify_user

        if record_event:
            await self._record_risk_event(
                level="P0",
                event_type="hard_limit",
                message=reason,
                live_session_id=live_session_id,
            )

        try:
            await notify_user(user_id, f"⚠️ 下单被风控拦截\n{reason}")
        except Exception:  # noqa: BLE001 - 通知不得影响风控判定
            logger.error("[RiskGuard] 拒单通知投递失败 user=%s", user_id, exc_info=True)
        return False, reason

    async def _is_circuit_open(self, user_id: str) -> bool:
        """检查熔断器是否打开"""
        cb = CircuitBreaker(user_id=user_id)
        return await cb.is_open()

    async def record_order_success(self, user_id: str) -> None:
        """订单成功时调用，重置失败计数"""
        if not user_id:
            return
        cb = CircuitBreaker(user_id=user_id)
        await cb.record_success()

    async def record_order_failure(
        self,
        user_id: Optional[str],
        *,
        symbol: str = "",
        side: str = "",
        quantity: str = "",
        error: str = "",
        unknown: bool = False,
    ) -> None:
        """订单失败时调用：连续失败触发熔断 + 给用户一条可见通知。

        2026-09-21 事故：失败只喂熔断器，用户完全无感（意图中的订单被静默丢弃，
        只能翻订单列表才发现）。因此这里必须留一条用户可见的通知。

        2026-09-22：通知改走出站唯一口 ``notify_user``（落库 + 即时推送），不再自己
        内联写 ``Notification``——两个投递口迟早会漂。**行为变更**：订单失败从此
        还有一条即时推送，不再只在 Web 通知中心可见。
        通知失败只记 ERROR，绝不影响熔断/下单主流程。

        ``unknown=True`` 用于「下单结果未知」（``OrderPlacementUnknown``）：那张单
        可能已在交易所活着，所以**必须改口径**——说「下单失败」会让用户重下一张，
        把敞口变成两倍。熔断计数不变（未知单同样是风险信号），只是措辞如实。
        """
        if not user_id:
            return
        cb = CircuitBreaker(user_id=user_id)
        await cb.record_failure()

        from apps.trading.alerts import notify_user

        detail = f"{symbol} {side} qty={quantity}".strip()
        if unknown:
            prefix = "⚠️ 下单结果未知（可能已成交，请先到交易所核对再决定是否重下）"
        else:
            prefix = "⚠️ 下单失败"
        try:
            delivered = await notify_user(
                user_id,
                f"{prefix}: {detail}｜原因: {error or '交易所未返回原因'}",
            )
        except Exception as e:  # noqa: BLE001 - 通知失败不得影响主流程
            logger.error(
                "下单失败通知投递异常（熔断计数已完成，user=%s symbol=%s）: %s",
                user_id,
                symbol,
                e,
                exc_info=True,
            )
            return

        if not delivered:
            logger.error(
                "下单失败通知未送达（熔断计数已完成，user=%s symbol=%s）: %s",
                user_id,
                symbol,
                error or "交易所未返回原因",
            )

    async def _get_daily_trade_count(self, user_id: str) -> int:
        """统计日内已成交订单数量"""
        from apps.trading.models import Order
        from django.utils import timezone

        today = timezone.now().date()

        @db_async
        def count():
            # 「未知」也计入：它意味着这张单可能真的发到了交易所，按保守口径
            # 必须占用当日的下单额度（漏计会让熔断线被绕过）。
            return Order.objects.filter(
                user_id=user_id,
                created_at__date=today,
                status__in=["submitted", "filled", "unknown"],
            ).count()

        return await count()

    @staticmethod
    def _is_perp_request(request: "OrderRequest") -> bool:
        """这张单是不是永续合约单。

        两个口径取或：``request.is_perp`` 显式标志（币安 fapi 通路由下单方声明），
        或 symbol 后缀 ``-SWAP``（OKX 风格的统一合约符号）。现货（默认）两者皆无。
        """
        return bool(request.is_perp) or request.symbol.upper().endswith("-SWAP")

    async def _check_leverage(self, request: "OrderRequest") -> str:
        """3a 杠杆上限。返回空串 = 放行，否则返回带具体数字的拒绝理由。"""
        if request.leverage > self.MAX_LEVERAGE:
            return (
                f"杠杆 {request.leverage}x 超过上限 {self.MAX_LEVERAGE}x"
                f"（MAX_LEVERAGE={self.MAX_LEVERAGE}）"
            )
        return ""

    async def _check_total_notional_exposure(
        self, request: "OrderRequest", user_id: str
    ) -> str:
        """3c 组合总名义敞口：Σ|position_value × leverage| / total_equity ≤ 100%。

        本单若成交也计入分子（它此刻就是新的敞口）。持仓与净值都按**本单的账户**
        解析（与 `_check_position_limit` 同一个适配器、同一个口径）；任一取数失败
        或分母非正 → 降级放行，与现货单仓检查的取向一致——这里的职责是拦「明知
        超限还开仓」，不是把取数故障变成全场停摆。
        """
        from apps.trading.executor import OrderExecutor

        executor = OrderExecutor.get_instance()
        if not executor:
            return ""
        adapter = executor.resolve_adapter(
            getattr(request, "exchange_account_id", None)
        )
        if not adapter:
            return ""

        try:
            balance = await adapter.get_balance()
            positions = await adapter.get_positions()
        except Exception:  # noqa: BLE001 - 取数失败降级放行（同现货单仓检查）
            logger.warning(
                "[RiskGuard] step 3c 总名义敞口取数失败，本轮降级放行",
                exc_info=True,
            )
            return ""

        total_equity = balance.get("USDT", Decimal("0"))
        if total_equity <= 0:
            return ""

        total_notional = Decimal("0")
        for pos in positions:
            mark = pos.mark_price if pos.mark_price is not None else pos.entry_price
            total_notional += abs(pos.quantity * mark * pos.leverage)

        if request.price is not None:
            # 本单成交后的新增名义敞口。spec 公式无条件 ×leverage：现货单
            # leverage=1（OrderRequest 缺省），perp 单 ×实际杠杆——同一式子。
            total_notional += abs(
                request.price * request.quantity * Decimal(request.leverage)
            )

        ratio = total_notional / total_equity
        if ratio > self.MAX_TOTAL_NOTIONAL_EXPOSURE:
            return (
                f"总名义敞口 {ratio:.1%} 超过上限 "
                f"{self.MAX_TOTAL_NOTIONAL_EXPOSURE:.0%}"
                f"（持仓+本单名义 {total_notional}，净值 {total_equity}）"
            )
        return ""

    async def _check_position_limit(
        self, request: "OrderRequest", user_id: str
    ) -> Tuple[bool, str]:
        """单笔仓位不超过总资产20%

        2026-09-22：适配器改按 ``request.exchange_account_id`` 解析（与
        `OrderExecutor.submit_order` 用的是同一个口径）。此前按**交易所名**取
        `executor._adapters`：同一交易所挂两个账户时那张表里只有「最后加载的」那个，
        于是给 A 账户下的单可能拿 B 账户的余额当分母——仓位上限被算成了别人的钱，
        而且两边日志都写着 binance，看不出来。

        2026-10-12：改走 `OrderExecutor.resolve_adapter()` 这个**公开** interface，
        不再跨 app 摸私有方法；按名字索引的那张表也已删除，所以「拿不到账户 id 的
        调用方（纸面交易）」现在**拿不到适配器**→ 放行（与旧行为一致），而不是退回
        按交易所名解析（那会拿到同交易所另一个账户的余额）。
        """
        from apps.trading.executor import OrderExecutor

        executor = OrderExecutor.get_instance()
        if not executor:
            return True, ""

        adapter = executor.resolve_adapter(
            getattr(request, "exchange_account_id", None)
        )
        if not adapter:
            return True, ""

        try:
            balance = await adapter.get_balance()
        except Exception:
            return True, ""

        total_usdt = balance.get("USDT", Decimal("0"))
        if total_usdt == 0:
            return True, ""

        if request.price is None:
            # 市价单无法预知价格，跳过仓位校验
            return True, ""

        order_value = request.price * request.quantity
        ratio = order_value / total_usdt
        # 3b：perp（显式标志或 -SWAP 后缀）用合约单仓上限，现货维持 0.20 不回退。
        max_ratio = (
            self.MAX_POSITION_RATIO_PERP
            if self._is_perp_request(request)
            else self.MAX_POSITION_RATIO
        )
        if ratio > max_ratio:
            return False, (f"单笔仓位 {ratio:.1%} 超过上限 {max_ratio:.0%}")
        return True, ""

    async def _check_drawdown(self, user_id: str) -> Tuple[bool, str]:
        """
        日内已实现回撤检查：分子是当日已实现盈亏，分母是**当日净值快照**
        （`daily_snapshots`，由 `apps.trading.tasks.snapshot_daily_equity` 每 5 分钟
        写入、当日只写第一条 → `date=today` 那条就是「当日首次观测到的权益」，
        也正是本检查要的「期初」）。

        分母只取**当日**（`date=today`），不向前回退到昨天：昨天的权益配今天的
        盈亏是两个口径，宁可承认「今天还没有分母」也不拿旧分母充数。代价是每个
        UTC 日界之后、当日第一条快照写出来之前（≤ 一个 beat 周期）分母缺失，这段
        窗口本检查降级放行（见下）。

        原实现在这里直接「无法获取期初资金数据」拒单——把「当天还没有分母」当成
        「不该下单」，于是新用户第一天完全不能开仓、每天日界后还要禁交易 5 分钟，
        且不产生任何告警。这是本单元要消灭的静默故障：**降级放行 + WARNING**，
        而写入方取不到余额时会主动告警用户（`daily_snapshot._alert_write_failure`）。

        已知残留缺口（未消除，只是不再沉默）：若 beat/worker 长期不跑，`date=today`
        永远取不到分母，回撤保护就一直处于降级状态，而写入方自己也没机会告警。
        这一层由任务健康检查兜底；`date__lte` 那种回退写法没有这个缺口。
        """
        from django.db.models import Sum
        from django.utils import timezone

        today = timezone.now().date()

        @db_async
        def get_today_pnl():
            from apps.trading.models import Order

            result = Order.objects.filter(
                user_id=user_id,
                status="filled",
                created_at__date=today,
            ).aggregate(total_pnl=Sum("realized_pnl"))
            return result["total_pnl"]

        @db_async
        def get_opening_equity():
            """取**当日**快照；当日还没写出来则返回 None（由调用方降级放行）。"""
            from apps.trading.models import DailySnapshot

            snap = (
                DailySnapshot.objects.filter(
                    user_id=user_id,
                    date=today,
                )
                .order_by("-date")
                .first()
            )
            if snap:
                return snap.total_equity
            return None

        try:
            pnl = await get_today_pnl()
        except Exception:
            return await self._reject(user_id, "无法获取当日已实现盈亏数据")

        # 无已成交订单，视作无亏损
        if pnl is None:
            pnl = Decimal("0")

        if pnl >= 0:
            return True, ""

        try:
            initial = await get_opening_equity()
        except Exception:
            return await self._reject(user_id, "无法读取净值快照（数据库异常）")

        if initial is None:
            logger.warning(
                "[RiskGuard] user=%s 当日（%s）没有净值快照，日内回撤检查本轮降级放行"
                "（等待 snapshot_daily_equity 写入，它与本检查同一口径）",
                user_id,
                today,
            )
            return True, ""

        if initial <= 0:
            # 账户净值真的是 0（不是「取不到」）——这是可以告知用户的事实，
            # 且此时任何下单都会在交易所侧失败，拒绝是如实而不是沉默。
            return await self._reject(
                user_id, f"账户净值为 {initial}，无法计算日内回撤"
            )

        drawdown = abs(pnl) / initial
        if drawdown > self.MAX_DAILY_DRAWDOWN:
            return await self._reject(
                user_id,
                f"日内回撤 {drawdown:.1%} 超过上限 {self.MAX_DAILY_DRAWDOWN:.0%}"
                f"（当日已实现亏损 {pnl}，期初净值 {initial}）",
            )
        return True, ""

    # ------------------------------------------------------------------ #
    #  实时监控 Loop                                                     #
    # ------------------------------------------------------------------ #

    async def _monitor_loop(self) -> None:
        """每分钟检查所有持仓的浮亏"""
        while self._running:
            try:
                await self._check_floating_pnl()
            except Exception as e:
                logger.error(f"RiskGuard monitor error: {e}")
            await asyncio.sleep(60)

    async def _check_floating_pnl(self) -> None:
        """浮亏超阈值时发送告警

        2026-09-22：遍历改用账户级适配器清单（标签形如 ``binance#<账户id>``）。
        此前遍历 `executor._adapters`——那张表按**交易所名**索引，同一交易所的两个
        活跃账户里只有一个在里面，于是另一个账户的持仓**永远不会被巡检**，浮亏到了
        阈值也没人告警，而日志上看不出少了什么。

        2026-10-12：改走 `OrderExecutor.iter_adapters()` 这个**公开** interface，
        不再跨 app 摸私有方法；那张按名字索引的表已删除，所以「每个账户都被巡检」
        现在是**结构上**成立的，不再依赖「账户地图是超集」这个隐含前提。
        """
        from apps.trading.executor import OrderExecutor

        executor = OrderExecutor.get_instance()
        if not executor:
            return

        for target, adapter in executor.iter_adapters():
            try:
                positions = await adapter.get_positions()
            except Exception as e:
                self._position_fetch_failures += 1
                # 必须带异常类型：httpx 超时异常的 message 为空，只打 {e} 会得到一行空消息
                # （2026-09-20 事故：连续 10 小时 100% 失败却无从诊断）
                logger.error(
                    f"Failed to fetch positions from {target} "
                    f"({type(e).__name__}: {e!r}); "
                    f"consecutive_failures={self._position_fetch_failures}"
                )
                continue
            self._position_fetch_failures = 0

            try:
                balance = await adapter.get_balance()
            except Exception as e:
                logger.error(
                    f"Failed to fetch balance from {target} ({type(e).__name__}: {e!r})"
                )
                continue

            total = balance.get("USDT", Decimal("1"))
            if total <= 0:
                total = Decimal("1")

            for pos in positions:
                pnl_ratio = pos.unrealized_pnl / total
                if pnl_ratio < self.FLOATING_LOSS_ALERT:
                    await self._send_floating_loss_alert(target, pos, pnl_ratio)
                    # 记录风控事件
                    await self._record_risk_event(
                        level="P1",
                        event_type="floating_loss",
                        message=f"{target}:{pos.symbol} 浮亏 {pnl_ratio:.2%}",
                    )

    async def _send_floating_loss_alert(self, target: str, pos, ratio: Decimal) -> None:
        """发送 Telegram 浮亏预警（``target`` = 交易所名或 ``交易所#账户id``）"""
        try:
            # TelegramChannel 需要 app 实例，通过日志作为 fallback
            logger.warning(
                f"浮亏预警 | 交易所/账户: {target} | 品种: {pos.symbol} | "
                f"方向: {pos.side} | 数量: {pos.quantity} | 浮亏: {ratio:.2%}"
            )
        except Exception:
            logger.warning(
                f"浮亏预警 | 交易所/账户: {target} | 品种: {pos.symbol} | "
                f"方向: {pos.side} | 数量: {pos.quantity} | 浮亏: {ratio:.2%}"
            )

    async def _record_risk_event(
        self,
        level: str,
        event_type: str,
        message: str,
        live_session_id: Optional[str] = None,
    ) -> None:
        """记录风控事件到数据库"""
        try:
            from apps.riskguard.models import RiskEvent

            @db_async
            def create():
                # live_session_id 直接给外键列：uuid 校验交给数据库——非法 id 让写入
                # 失败并走下面的 except 记日志，而不是在这里多一道「先查一次」的查询
                # （那一道查询自己也可能挂，挂了同样要走 except，白绕一圈）。
                return RiskEvent.objects.create(
                    level=level,
                    event_type=event_type,
                    message=message,
                    live_session_id=live_session_id,
                )

            await create()
        except Exception as e:
            logger.error(f"Failed to record risk event: {e}")
