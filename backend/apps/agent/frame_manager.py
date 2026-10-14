from __future__ import annotations
import asyncio
import json
import logging
from decimal import Decimal
from enum import Enum
from typing import Optional

from asgiref.sync import sync_to_async
from django.conf import settings
from django.db import close_old_connections
import redis

logger = logging.getLogger(__name__)


class FrameState(str, Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"


def live_session_kwargs(session) -> dict:
    """由 LiveSession 记录构造 start_strategy_runner 的入参。

    DB 是唯一权威来源（Redis 兼容层可能过期）：恢复 / 恢复运行（resume）共用同一构造，
    避免两处口径漂移。
    """
    return {
        "strategy_name": session.strategy.name,
        "symbol": session.symbol,
        "timeframe": (
            session.backtest_result.timeframe
            if session.backtest_result and session.backtest_result.timeframe
            else "1h"
        ),
        "parameters": session.config or {},
        "exchange_account_id": (
            str(session.exchange_account.id) if session.exchange_account else ""
        ),
        "user_id": str(session.user.id) if session.user else None,
        "live_session_id": str(session.id),
        "initial_balance": session.initial_capital,
    }


class FrameManager:
    """管理交易/辅助/回测框架的生命周期（懒加载，按需启动）"""

    _instance: Optional[FrameManager] = None

    def __init__(self):
        self._trading_state = FrameState.STOPPED
        self._assist_state = FrameState.STOPPED
        self._risk_guard_refs = 0  # 引用计数：trading+assist共享单实例
        self._data_feed_refs = 0  # 引用计数：trading+assist共享数据源
        self._riskguard = None
        self._order_executor = None
        self._order_consumer_task: asyncio.Task | None = None
        # 多策略并发：live_session_id → StrategyRunner
        self._strategy_runners: dict[str, object] = {}
        self._need_restart = (
            False  # 进程重启后底层组件需重建（OrderExecutor 无法跨进程复用）
        )
        # 限制 K 线回调触发的信号检查并发数：
        # 该检查内部会走 ccxt 同步网络调用（fetch_ohlcv），
        # 在 thread_sensitive=False 线程池上并发执行时，若不限流，
        # 会瞬时堆积大量 ccxt 实例把 512M 容器内存打爆（OOM）。
        self._kline_check_sem = asyncio.Semaphore(2)

        # 从 Redis 恢复持久化的框架状态
        self._restore_frame_states()

    @classmethod
    def get_instance(cls) -> FrameManager:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ------------------------------------------------------------------ #
    #  Redis 持久化
    # ------------------------------------------------------------------ #

    def _get_redis(self) -> redis.Redis:
        """获取 Redis 连接（DB8，无 TTL）。"""
        url = settings.REDIS_URL
        db = getattr(settings, "REDIS_DB_FRAME", 8)
        return redis.Redis.from_url(url, db=db, decode_responses=True)

    def _persist_frame_state(self) -> None:
        """将当前框架状态持久化到 Redis。"""
        try:
            r = self._get_redis()
            pipe = r.pipeline()
            pipe.set("frame:trading:state", self._trading_state.value)
            pipe.set("frame:assist:state", self._assist_state.value)
            pipe.set("frame:risk_guard_refs", self._risk_guard_refs)
            pipe.set("frame:data_feed_refs", self._data_feed_refs)
            pipe.set("frame:order_executor", "1" if self._order_executor else "0")
            pipe.execute()
        except Exception as e:
            logger.warning("[FrameManager] persist state failed: %s", e)

    def _persist_live_session(
        self,
        live_session_id: str,
        strategy_name: str,
        symbol: str,
        timeframe: str,
        parameters: dict,
        exchange_account_id: str,
        user_id: str,
        initial_balance: str,
    ) -> None:
        """将LiveSession运行信息持久化到Redis"""
        try:
            r = self._get_redis()
            key = f"frame:live_session:{live_session_id}"
            r.hset(
                key,
                mapping={
                    "strategy_name": strategy_name,
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "parameters": json.dumps(parameters or {}),
                    "exchange_account_id": exchange_account_id,
                    "user_id": user_id,
                    "initial_balance": initial_balance,
                    "status": "running",
                },
            )
            r.expire(key, 86400)  # 24小时TTL
            logger.debug("[FrameManager] persisted live session %s", live_session_id)
        except Exception as e:
            logger.warning("[FrameManager] failed to persist live session: %s", e)

    def _remove_live_session(self, live_session_id: str) -> None:
        """从Redis移除LiveSession状态"""
        try:
            r = self._get_redis()
            key = f"frame:live_session:{live_session_id}"
            r.delete(key)
            logger.debug("[FrameManager] removed live session %s", live_session_id)
        except Exception as e:
            logger.warning("[FrameManager] failed to remove live session: %s", e)

    def _restore_frame_states(self) -> None:
        """从 Redis 恢复框架状态。"""
        try:
            r = self._get_redis()
            trading = r.get("frame:trading:state")
            assist = r.get("frame:assist:state")
            risk_refs = r.get("frame:risk_guard_refs")
            data_feed_refs = r.get("frame:data_feed_refs")
            order_exec = r.get("frame:order_executor")

            if trading == FrameState.RUNNING.value:
                self._trading_state = FrameState.RUNNING
            elif trading == FrameState.STOPPED.value:
                self._trading_state = FrameState.STOPPED

            if assist == FrameState.RUNNING.value:
                self._assist_state = FrameState.RUNNING
            elif assist == FrameState.STOPPED.value:
                self._assist_state = FrameState.STOPPED

            if risk_refs is not None:
                self._risk_guard_refs = int(risk_refs)

            if data_feed_refs is not None:
                self._data_feed_refs = int(data_feed_refs)

            if order_exec == "1":
                # OrderExecutor 无法跨进程复用，标记需要重建
                # 保留 _trading_state/_assist_state 原始值（反映退出时状态），
                # 通过 _need_restart 告诉 restore_and_restart_frames() 真正重建底层组件。
                self._need_restart = True
                self._risk_guard_refs = 0
                self._data_feed_refs = 0
            elif order_exec == "0" and (
                trading == FrameState.RUNNING.value
                or assist == FrameState.RUNNING.value
            ):
                # 状态不一致：frame 标记为 running 但 executor 为 0
                # 说明上次 shutdown 只保存了部分状态，需要完全重启
                self._trading_state = FrameState.STOPPED
                self._assist_state = FrameState.STOPPED
                self._risk_guard_refs = 0
                self._data_feed_refs = 0

            if (
                trading == FrameState.RUNNING.value
                or assist == FrameState.RUNNING.value
            ):
                logger.info(
                    "[FrameManager] restored frame states: trading=%s, assist=%s",
                    self._trading_state.value,
                    self._assist_state.value,
                )
        except Exception as e:
            logger.warning("[FrameManager] restore state failed: %s", e)

    async def restore_and_restart_frames(self) -> None:
        """系统启动时调用：检查持久化状态并自动重启运行中的框架。"""
        # _need_restart=True 表示进程重启（OrderExecutor 等底层组件无法跨进程复用），
        # 需要强制重建；否则只在"状态 running 但组件未初始化"时重启。
        if self._need_restart:
            # _need_restart=True 意味着 orderExecutor 在上一进程已初始化
            # （order_exec=1，仅由 start_trading_frame 设置）。此时无论
            # frame:trading:state 是 running 还是被失败启动撕裂成 stopped，
            # 都必须重建交易框架 —— 否则框架永远无法恢复，会话空转。
            need_restart_trading = True
            need_restart_assist = self._assist_state == FrameState.RUNNING
        else:
            need_restart_trading = (
                self._trading_state == FrameState.RUNNING and self._risk_guard_refs == 0
            )
            need_restart_assist = (
                self._assist_state == FrameState.RUNNING and self._risk_guard_refs == 0
            )

        if need_restart_trading:
            logger.info(
                "[FrameManager] trading frame was running before restart, auto-restarting... (force=%s)",
                self._need_restart,
            )
            try:
                await self.start_trading_frame(mode="live", force=self._need_restart)
                logger.info("[FrameManager] trading frame auto-restarted")
            except Exception as e:
                logger.error("[FrameManager] trading frame auto-restart failed: %s", e)
                self._trading_state = FrameState.STOPPED

        if need_restart_assist:
            logger.info(
                "[FrameManager] assist frame was running before restart, auto-restarting... (force=%s)",
                self._need_restart,
            )
            try:
                await self.start_assist_frame()
                logger.info("[FrameManager] assist frame auto-restarted")
            except Exception as e:
                logger.error("[FrameManager] assist frame auto-restart failed: %s", e)
                self._assist_state = FrameState.STOPPED

        # 恢复LiveSession（必须在 DataFeed 真正连接之后，已在 start_trading_frame 内完成）
        await self._restore_live_sessions()

    async def _restore_live_sessions(self) -> None:
        """恢复运行的 LiveSession。

        **数据源约定**：DB `LiveSession(status='running')` 是唯一权威来源；
        Redis 键（24h TTL，易过期）只作兼容层。之前只读 Redis 键导致
        进程重启大于 TTL 后会话全部丢失（DB 标记 running 但无进程运行）。
        """
        # 收集待恢复运行信息：live_session_id -> kwargs
        pending: dict[str, dict] = {}

        # 1) 兼容层：Redis 持久化键（旧实现写入的运行信息）
        try:
            r = self._get_redis()
            keys = r.keys("frame:live_session:*")
            logger.info(
                "[FrameManager] found %d live session keys in Redis (legacy)",
                len(keys),
            )
            for key in keys:
                key_str = key if isinstance(key, str) else key.decode()
                session_data = r.hgetall(key)
                if not session_data:
                    continue
                if isinstance(session_data, dict):
                    decoded_data = {}
                    for k, v in session_data.items():
                        k_str = k if isinstance(k, str) else k.decode()
                        v_str = v if isinstance(v, str) else v.decode()
                        decoded_data[k_str] = v_str
                    session_data = decoded_data
                if session_data.get("status") == "running":
                    live_session_id = key_str.split(":")[-1]
                    pending[live_session_id] = {
                        "strategy_name": session_data.get("strategy_name"),
                        "symbol": session_data.get("symbol"),
                        "timeframe": session_data.get("timeframe"),
                        "parameters": json.loads(session_data.get("parameters", "{}")),
                        "exchange_account_id": session_data.get("exchange_account_id"),
                        "user_id": session_data.get("user_id") or None,
                        "live_session_id": live_session_id,
                        "initial_balance": Decimal(
                            session_data.get("initial_balance", "0")
                        ),
                    }
        except Exception as e:
            logger.warning(
                "[FrameManager] failed to read live session keys from Redis: %s", e
            )

        # 2) 权威来源：DB status=running 的 LiveSession
        db_ids: set[str] = set()
        db_ok = False
        try:
            from asgiref.sync import sync_to_async
            from apps.trading.models import LiveSession

            @sync_to_async
            def _running_sessions() -> list:
                close_old_connections()
                return list(
                    LiveSession.objects.filter(status="running").select_related(
                        "strategy", "exchange_account", "user", "backtest_result"
                    )
                )

            db_sessions = await _running_sessions()
            db_ids = {str(s.id) for s in db_sessions}
            db_ok = True
            logger.info(
                "[FrameManager] found %d running sessions in DB", len(db_sessions)
            )
            for s in db_sessions:
                sid = str(s.id)
                # DB 是权威来源：即使 Redis 键过期或值过时，
                # 也以 DB 字段覆盖重写（同一会话仅保留一份待恢复信息）。
                pending[sid] = live_session_kwargs(s)
        except Exception as e:
            logger.warning(
                "[FrameManager] failed to read running sessions from DB: %s", e
            )

        # 3) DB 是唯一权威：Redis 兼容层里 status=running、但 DB 已非 running 的会话
        # 一律不恢复（否则"暂停/停止"在重启后失效——2026-09-21 实际踩到：两个已 paused
        # 的 DOGE 会话被 Redis 旧值捞回来并自动开仓），并把兼容层状态纠正为 DB 真值。
        if db_ok and pending:
            stale = [sid for sid in pending if sid not in db_ids]
            for sid in stale:
                pending.pop(sid, None)
                logger.warning(
                    "[FrameManager] skip live session %s: DB status is not running "
                    "(Redis legacy key says running)",
                    sid,
                )
            if stale:
                try:
                    from asgiref.sync import sync_to_async
                    from apps.trading.models import LiveSession

                    @sync_to_async
                    def _real_statuses() -> dict:
                        close_old_connections()
                        return dict(
                            LiveSession.objects.filter(id__in=stale).values_list(
                                "id", "status"
                            )
                        )

                    real = {str(k): v for k, v in (await _real_statuses()).items()}
                    r = self._get_redis()
                    for sid in stale:
                        if sid in real:
                            r.hset(f"frame:live_session:{sid}", "status", real[sid])
                        else:
                            r.delete(f"frame:live_session:{sid}")
                except Exception as e:  # noqa: BLE001 - 兼容层纠正失败不影响恢复
                    logger.warning(
                        "[FrameManager] failed to correct legacy Redis keys: %s", e
                    )

        # 关键保障：只要有待恢复的 running 会话，交易框架必须处于运行态，
        # 否则 OrderConsumer 不消费 stream → 信号分发后订单永远不会执行。
        # （进程重启后框架可能因撕裂状态未恢复，此时先补启动。）
        if pending:
            if (
                self._trading_state != FrameState.RUNNING
                or self._order_executor is None
            ):
                logger.warning(
                    "[FrameManager] %d running session(s) to restore but trading "
                    "frame not running — starting trading frame first",
                    len(pending),
                )
                await self.start_trading_frame(mode="live", force=True)

        for live_session_id, kw in pending.items():
            strategy_name = kw.get("strategy_name")
            if not strategy_name:
                continue
            logger.info(
                "[FrameManager] restoring live session: %s (%s %s)",
                live_session_id,
                strategy_name,
                kw.get("symbol"),
            )
            try:
                await self.start_strategy_runner(**kw)
                logger.info("[FrameManager] live session %s restored", live_session_id)
            except Exception as e:
                logger.error(
                    "[FrameManager] failed to restore live session %s: %s",
                    live_session_id,
                    e,
                )
                # 策略无法解析（不存在/语法错误）是永久性故障：
                # 标记 DB status=error，避免每次重启都重试喷错。
                if isinstance(e, (ValueError, ImportError, SyntaxError)):
                    try:
                        from asgiref.sync import sync_to_async
                        from apps.trading.models import LiveSession

                        @sync_to_async
                        def _mark_error():
                            close_old_connections()
                            LiveSession.objects.filter(id=live_session_id).update(
                                status="error"
                            )

                        await _mark_error()
                        logger.warning(
                            "[FrameManager] live session %s marked status=error "
                            "(unresolvable strategy %s)",
                            live_session_id,
                            kw.get("strategy_name"),
                        )
                    except Exception as ex:
                        logger.warning(
                            "[FrameManager] failed to mark session %s error: %s",
                            live_session_id,
                            ex,
                        )
                try:
                    r = self._get_redis()
                    r.hset(
                        f"frame:live_session:{live_session_id}",
                        "status",
                        "restore_failed",
                    )
                except Exception:
                    pass

    # ------------------------------------------------------------------ #
    #  Public API
    # ------------------------------------------------------------------ #

    def status(self) -> dict:
        # 优先使用 Redis 持久化状态（进程重启后可恢复），
        # 不依赖进程内对象引用（进程重启后对象丢失）。
        try:
            r = self._get_redis()
            order_exec_persisted = r.get("frame:order_executor") == "1"
            risk_refs_persisted = int(r.get("frame:risk_guard_refs") or 0)
            data_feed_refs_persisted = int(r.get("frame:data_feed_refs") or 0)
        except Exception:
            order_exec_persisted = self._order_executor is not None
            risk_refs_persisted = self._risk_guard_refs
            data_feed_refs_persisted = self._data_feed_refs

        return {
            "trading": self._trading_state.value
            if hasattr(self._trading_state, "value")
            else self._trading_state,
            "assist": self._assist_state.value
            if hasattr(self._assist_state, "value")
            else self._assist_state,
            "risk_guard": "running"
            if self._risk_guard_refs > 0 or risk_refs_persisted > 0
            else "stopped",
            "order_executor": "running"
            if self._order_executor is not None or order_exec_persisted
            else "stopped",
            "data_feed": "running"
            if self._data_feed_refs > 0 or data_feed_refs_persisted > 0
            else "stopped",
        }

    # --- Trading Frame ---

    async def start_trading_frame(
        self, mode: str = "live", force: bool = False
    ) -> None:
        """mode: 'live' | 'paper'
        force=True: 跳过状态检查，强制重建底层组件（进程重启恢复场景使用）。
        """
        if not force and self._trading_state == FrameState.RUNNING:
            if self._order_executor is not None:
                logger.warning("[FrameManager] trading frame already running")
                return
            # 撕裂态自愈：状态标记 running 但 OrderExecutor 未初始化
            # （如上一次失败启动只持久化了状态），继续 early-return 会
            # 导致框架永远无法启动。此时强制重建底层组件。
            logger.warning(
                "[FrameManager] trading_state=RUNNING but OrderExecutor missing, "
                "forcing rebuild"
            )
            force = True
        self._trading_state = FrameState.STARTING
        # force 模式下重置引用计数，确保底层组件干净重建
        if force:
            self._risk_guard_refs = 0
            self._data_feed_refs = 0
        try:
            await self._start_data_feed()
            await self._start_risk_guard()
            await self._start_order_executor()
            await self._start_order_consumer(mode)
            self._trading_state = FrameState.RUNNING
            self._persist_frame_state()
            logger.info(f"[FrameManager] trading frame started (mode={mode})")
        except Exception:
            self._trading_state = FrameState.STOPPED
            self._persist_frame_state()
            raise

    async def stop_trading_frame(self) -> None:
        if self._trading_state != FrameState.RUNNING:
            return
        self._trading_state = FrameState.STOPPING
        await self.stop_strategy_runner()
        await self._stop_order_consumer()
        await self._stop_order_executor()
        await self._stop_risk_guard()
        await self._stop_data_feed()
        self._trading_state = FrameState.STOPPED
        self._persist_frame_state()
        logger.info("[FrameManager] trading frame stopped")

    # --- Assist Frame ---

    async def start_assist_frame(self) -> None:
        if self._assist_state == FrameState.RUNNING:
            return
        self._assist_state = FrameState.STARTING
        try:
            await self._start_data_feed()
            await self._start_risk_guard()
            await self._start_signal_monitor()
            self._assist_state = FrameState.RUNNING
            self._persist_frame_state()
            logger.info("[FrameManager] assist frame started")
        except Exception:
            self._assist_state = FrameState.STOPPED
            self._persist_frame_state()
            raise

    async def stop_assist_frame(self) -> None:
        if self._assist_state != FrameState.RUNNING:
            return
        self._assist_state = FrameState.STOPPING
        await self._stop_signal_monitor()
        await self._stop_risk_guard()
        await self._stop_data_feed()
        self._assist_state = FrameState.STOPPED
        self._persist_frame_state()
        logger.info("[FrameManager] assist frame stopped")

    async def stop_all(self) -> None:
        await self.stop_trading_frame()
        await self.stop_assist_frame()

    async def start(self, frame_type: str, mode: str = "live") -> None:
        """统一启动入口（supervisor调用）"""
        if frame_type == "trading":
            await self.start_trading_frame(mode=mode)
        elif frame_type == "assist":
            await self.start_assist_frame()
        else:
            raise ValueError(f"Unknown frame type: {frame_type!r}")

    async def stop(self, frame_type: str) -> None:
        """统一停止入口（supervisor调用）"""
        if frame_type == "trading":
            await self.stop_trading_frame()
        elif frame_type == "assist":
            await self.stop_assist_frame()
        else:
            raise ValueError(f"Unknown frame type: {frame_type!r}")

    # --- Strategy Runner ---

    async def start_strategy_runner(
        self,
        strategy_name: str,
        symbol: str,
        timeframe: str,
        parameters: dict | None = None,
        exchange_account_id: str = "",
        user_id: str | None = None,
        live_session_id: str | None = None,
        initial_balance: Decimal = Decimal("0"),
    ) -> None:
        """启动策略运行器，在交易框架启动后调用。
        支持多策略并发：每个 live_session_id 独立维护一个 StrategyRunner。
        """
        from apps.strategy_engine.runner import StrategyRunner

        session_key = live_session_id or ""

        # 同一 live会话重复启动：先停旧的再重启
        existing = self._strategy_runners.get(session_key)
        if existing is not None:
            logger.warning(
                "[FrameManager] strategy runner already exists for session %s, stopping first",
                session_key,
            )
            await self.stop_strategy_runner(live_session_id=live_session_id)

        runner = StrategyRunner()
        await runner.start_live(
            strategy_name=strategy_name,
            symbol=symbol,
            timeframe=timeframe,
            parameters=parameters,
            exchange_account_id=exchange_account_id,
            user_id=user_id,
            live_session_id=live_session_id,
            initial_balance=initial_balance,
        )
        self._strategy_runners[session_key] = runner
        logger.info(
            "[FrameManager] strategy runner started: %s %s %s (session=%s, total=%d)",
            strategy_name,
            symbol,
            timeframe,
            session_key,
            len(self._strategy_runners),
        )
        # 持久化LiveSession运行信息
        self._persist_live_session(
            live_session_id=session_key,
            strategy_name=strategy_name,
            symbol=symbol,
            timeframe=timeframe,
            parameters=parameters,
            exchange_account_id=exchange_account_id,
            user_id=user_id or "",
            initial_balance=str(initial_balance),
        )

    async def start_live_session(self, live_session_id: str) -> None:
        """按 DB 记录启动（或重启）某个 live session 的 runner（resume 用）。

        入参一律取自 DB（权威来源）；start_strategy_runner 会顺带重建 Redis 兼容层键。
        """
        from apps.trading.models import LiveSession

        @sync_to_async
        def _load():
            close_old_connections()
            return (
                LiveSession.objects.filter(id=live_session_id)
                .select_related(
                    "strategy", "exchange_account", "user", "backtest_result"
                )
                .first()
            )

        session = await _load()
        if session is None:
            raise ValueError(f"LiveSession {live_session_id} not found")
        if self._trading_state != FrameState.RUNNING or self._order_executor is None:
            logger.warning(
                "[FrameManager] starting trading frame before resuming session %s",
                live_session_id,
            )
            await self.start_trading_frame(mode="live", force=True)
        await self.start_strategy_runner(**live_session_kwargs(session))

    async def stop_strategy_runner(self, live_session_id: str | None = None) -> None:
        """停止策略运行器。
        live_session_id 为 None 时停止所有；指定时只停对应会话。
        """
        if live_session_id is None:
            # 停止所有
            session_keys = list(self._strategy_runners.keys())
        else:
            session_keys = [live_session_id or ""]

        for key in session_keys:
            runner = self._strategy_runners.get(key)
            if runner is None:
                continue
            try:
                # 清理持久化状态
                rm_key = key
                if hasattr(runner, "_live_runner") and runner._live_runner:
                    rm_key = runner._live_runner.live_session_id or key

                await runner.stop_live()

                # 清理Redis持久化状态
                if rm_key:
                    self._remove_live_session(rm_key)
            except Exception as e:
                logger.error("[FrameManager] failed to stop runner %s: %s", key, e)
            finally:
                self._strategy_runners.pop(key, None)
                logger.info(
                    "[FrameManager] strategy runner stopped: session=%s (remaining=%d)",
                    key,
                    len(self._strategy_runners),
                )

    # --- Internal lifecycle methods (to be implemented) ---

    async def _start_data_feed(self) -> None:
        """连接所有已注册的数据源 WebSocket，并订阅信号监控所需的 K 线流。"""
        self._data_feed_refs += 1
        if self._data_feed_refs > 1:
            # 已有其他 frame 启动了数据源，只需增加引用计数
            self._persist_frame_state()
            logger.info(
                "[FrameManager] data feed already running (refs=%d)",
                self._data_feed_refs,
            )
            return

        from apps.datasource.registry import DataSourceRegistry

        # 1. 加载并连接所有已注册的 crypto 数据源
        sources = DataSourceRegistry.list_registered()
        logger.info(
            "[FrameManager] starting data feed, registered sources: %s", sources
        )
        for source_name in sources:
            try:
                ds = DataSourceRegistry.get(source_name)
                if not ds.is_connected():
                    connected = await ds.connect_websocket()
                    if connected:
                        logger.info(
                            "[FrameManager] data source %s connected", source_name
                        )
                    else:
                        logger.warning(
                            "[FrameManager] data source %s connection failed",
                            source_name,
                        )
                else:
                    logger.info(
                        "[FrameManager] data source %s already connected", source_name
                    )

                # 输出当前运行状态
                status = ds.get_status()
                logger.info(
                    "[FrameManager] data source %s status: connected=%s, subs=%d, "
                    "last_data_age=%ss, market_types=%s, types=%s",
                    source_name,
                    status["connected"],
                    status["subscriptions"],
                    status.get("last_data_age_seconds"),
                    status["market_types"],
                    status["supported_data_types"],
                )
            except Exception as e:
                logger.warning(
                    "[FrameManager] failed to load data source %s: %s", source_name, e
                )

        # 2. 根据活跃信号监控自动订阅对应的 K 线数据
        try:
            monitors = await self._get_active_signal_monitors()
            logger.info(
                "[FrameManager] signal monitors to subscribe: %d", len(monitors)
            )
            for monitor in monitors:
                await self.subscribe_signal_klines(
                    symbol=monitor.symbol,
                    interval_str=monitor.interval or "1h",
                    indicator_params=monitor.indicator_params,
                )
        except Exception as e:
            logger.warning("[FrameManager] signal monitor subscription failed: %s", e)

        self._persist_frame_state()
        # 输出所有数据源最终状态
        for source_name in DataSourceRegistry.list_registered():
            if DataSourceRegistry.is_loaded(source_name):
                ds = DataSourceRegistry.get(source_name)
                s = ds.get_status()
                logger.info(
                    "[FrameManager] data source %s final: connected=%s, subs=%d, "
                    "sub_keys=%s",
                    source_name,
                    s["connected"],
                    s["subscriptions"],
                    s["subscription_keys"],
                )
        logger.info("[FrameManager] data feed started")

    async def _stop_data_feed(self) -> None:
        """断开所有数据源 WebSocket 连接（引用计数为 0 时）。"""
        self._data_feed_refs = max(0, self._data_feed_refs - 1)
        if self._data_feed_refs > 0:
            # 还有其他 frame 在使用数据源，不断开
            self._persist_frame_state()
            logger.info(
                "[FrameManager] data feed refs=%d, keeping alive", self._data_feed_refs
            )
            return

        from apps.datasource.registry import DataSourceRegistry

        for source_name in DataSourceRegistry.list_registered():
            if DataSourceRegistry.is_loaded(source_name):
                try:
                    ds = DataSourceRegistry.get(source_name)
                    status = ds.get_status()
                    logger.info(
                        "[FrameManager] stopping data source %s: connected=%s, subs=%d, keys=%s",
                        source_name,
                        status["connected"],
                        status["subscriptions"],
                        status["subscription_keys"],
                    )
                    if ds.is_connected():
                        await ds.disconnect_websocket()
                        logger.info(
                            "[FrameManager] data source %s disconnected", source_name
                        )
                except Exception as e:
                    logger.warning(
                        "[FrameManager] failed to disconnect %s: %s", source_name, e
                    )

        self._persist_frame_state()
        logger.info("[FrameManager] data feed stopped")

    async def _get_active_signal_monitors(
        self, user_ids: set[str] | None = None
    ) -> list:
        """获取活跃的信号监控列表。
        user_ids: 限定用户集合；None 表示不过滤（向后兼容），空集合表示返回空。
        """
        try:
            from apps.signal_monitor.models import SignalMonitor
            from django.utils import timezone

            if user_ids is not None and len(user_ids) == 0:
                return []

            @sync_to_async
            def _query():
                close_old_connections()
                qs = SignalMonitor.objects.filter(status="active")
                if user_ids is not None:
                    qs = qs.filter(user_id__in=list(user_ids))
                monitors = list(qs)
                now = timezone.now()
                return [m for m in monitors if not m.expires_at or m.expires_at > now]

            return await _query()
        except Exception as e:
            logger.warning(
                "[FrameManager] failed to get active signal monitors: %s: %s",
                type(e).__name__,
                e,
            )
            return []

    async def subscribe_signal_klines(
        self,
        symbol: str,
        interval_str: str = "1h",
        indicator_params: dict | None = None,
    ) -> None:
        """为信号监控订阅指定 symbol 的 K 线 WebSocket 数据。

        可在实盘策略启动后调用，将新注册的 SignalMonitor symbol
        加入 WebSocket 实时回调，确保信号检查立即生效（而非仅依赖
        Celery Beat 30s 轮询兜底）。

        Args:
            symbol: 交易对，如 "BTC/USDT"
            interval_str: K 线周期，如 "1h", "15m"
            indicator_params: 监控的指标参数（如 {"period": 20}），
                回调内构造的轻量 monitor 用它决定缓存命中所需的
                K 线根数；缺省时按最小根数处理。
        """
        from apps.datasource.registry import DataSourceRegistry
        from apps.datasource.base import DataType, KlineInterval, MarketType

        try:
            interval = KlineInterval(interval_str)
        except ValueError:
            interval = KlineInterval.H1
            logger.warning(
                "[FrameManager] unknown interval %s, defaulting to 1h", interval_str
            )

        registered = DataSourceRegistry.list_registered()
        if not registered:
            logger.warning(
                "[FrameManager] no data sources registered, cannot subscribe kline"
            )
            return

        for source_name in registered:
            ds = DataSourceRegistry.get(source_name)
            if not ds.is_connected():
                logger.warning(
                    "[FrameManager] data source %s not connected, skip kline subscribe",
                    source_name,
                )
                continue
            if DataType.KLINE not in ds.supported_data_types:
                logger.warning(
                    "[FrameManager] data source %s does not support KLINE, skip",
                    source_name,
                )
                continue
            await ds.subscribe(
                symbol=symbol,
                data_type=DataType.KLINE,
                interval=interval,
                market_type=MarketType.SPOT,
                callback=lambda data, sym=symbol, iv=interval_str, ip=indicator_params or {}: (
                    asyncio.get_event_loop().create_task(
                        self._on_kline_data(data, sym, iv, ip)
                    )
                ),
            )
            logger.info(
                "[FrameManager] subscribed %s kline %s @%s for signal monitor",
                source_name,
                symbol,
                interval.value,
            )

    async def _on_kline_data(
        self,
        kline: dict,
        symbol: str,
        interval: str = "1h",
        indicator_params: dict | None = None,
    ) -> None:
        """K 线数据回调：触发信号检查。"""
        try:
            from apps.signal_monitor.engine import SignalMonitorEngine

            engine = SignalMonitorEngine.get_instance()

            # thread_sensitive=False：把检查（内含阻塞 ccxt 网络调用）放到
            # 通用线程池执行，避免占住 asgiref 的共享单线程——
            # 否则该线程被 Binance API 阻塞时，所有 thread-sensitive 的
            # sync_to_async（如 ChatConsumer 的 token 校验）会排队挂起，
            # 导致聊天 WebSocket 握手永远无法完成（对话"不响应"）。
            # close_old_connections：长跑回调中，pgbouncer/PG 会因空闲关闭
            # 服务端连接，下一次 ORM 操作会抛 "connection already closed"，
            # 先关旧连接让 Django 重建即可恢复。
            @sync_to_async(thread_sensitive=False)
            def run_check():
                close_old_connections()
                klines = engine._load_klines_for_monitors(
                    [
                        type(
                            "_M",
                            (),
                            {
                                "symbol": symbol,
                                "interval": interval,
                                "indicator_params": indicator_params or {},
                            },
                        )()
                    ]
                ).get(symbol, [])
                if len(klines) >= 2:
                    engine.check_signals_for_kline(symbol, klines)

            async with self._kline_check_sem:
                await run_check()
        except Exception as e:
            logger.warning("[FrameManager] kline callback error: %s", e)

    async def _start_risk_guard(self) -> None:
        self._risk_guard_refs += 1
        if self._risk_guard_refs == 1:
            from apps.riskguard.guard import RiskGuard

            self._riskguard = RiskGuard()
            await self._riskguard.start()
            logger.info("[FrameManager] RiskGuard started")
        self._persist_frame_state()

    async def _stop_risk_guard(self) -> None:
        self._risk_guard_refs = max(0, self._risk_guard_refs - 1)
        if self._risk_guard_refs == 0 and self._riskguard:
            await self._riskguard.stop()
            self._riskguard = None
            logger.info("[FrameManager] RiskGuard stopped")
        self._persist_frame_state()

    async def _start_order_consumer(self, mode: str) -> None:
        if self._order_consumer_task and not self._order_consumer_task.done():
            logger.warning("[FrameManager] order consumer already running")
            return
        from apps.agent.bus import ensure_groups
        from apps.trading.order_consumer import start_order_consumer

        await ensure_groups()
        self._order_consumer_task = asyncio.create_task(
            start_order_consumer(),
            name="order_consumer",
        )
        logger.info(f"[FrameManager] order consumer started (mode={mode})")

    async def _stop_order_consumer(self) -> None:
        task = self._order_consumer_task
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception) as e:
                logger.debug(f"[FrameManager] order consumer cancel: {e}")
        self._order_consumer_task = None
        logger.info("[FrameManager] order consumer stopped")

    async def _start_signal_monitor(self) -> None:
        """启动信号监控。

        信号监控由 Celery Beat 每 30 秒定时驱动（check_signals 任务），
        同时 WebSocket 数据源的 K 线回调也会触发实时检查（_on_kline_data）。
        启动时重新为所有活跃监控器订阅 K 线数据。
        """
        monitors = await self._get_active_signal_monitors()
        active_count = len(monitors)
        logger.info(
            "[FrameManager] signal monitor started: %d active monitors", active_count
        )

        # 确保活跃的 signal monitor 都订阅了 WebSocket K 线数据
        for monitor in monitors:
            try:
                await self.subscribe_signal_klines(
                    symbol=monitor.symbol,
                    interval_str=monitor.interval or "1h",
                    indicator_params=monitor.indicator_params,
                )
            except Exception as e:
                logger.warning(
                    "[FrameManager] signal monitor kline subscribe failed for %s: %s",
                    monitor.symbol,
                    e,
                )

    async def _stop_signal_monitor(self) -> None:
        logger.info("[FrameManager] signal monitor stopped")

    async def _start_order_executor(self) -> None:
        from apps.trading import OrderExecutor

        self._order_executor = OrderExecutor()
        await self._order_executor.initialize()
        logger.info("[FrameManager] OrderExecutor started")
        self._persist_frame_state()

    async def _stop_order_executor(self) -> None:
        if self._order_executor:
            await self._order_executor.shutdown()
            self._order_executor = None
            logger.info("[FrameManager] OrderExecutor stopped")
        self._persist_frame_state()
