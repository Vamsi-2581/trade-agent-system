"""
Binance 数据源

支持：
- WebSocket 实时数据（K线、成交、深度）
- REST API 历史数据
- 现货和合约市场
"""

import asyncio
import json
import logging
import time
import traceback
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any, Callable
import aiohttp
import ccxt.async_support as ccxt
from django.conf import settings

from apps.datasource.base import (
    BaseDataSource,
    DataType,
    KlineInterval,
    MarketType,
    ConnectionStatus,
)
from apps.datasource.registry import DataSourceRegistry
from apps.datasource.store import get_data_store
from apps.datasource.monitor import get_quality_monitor

logger = logging.getLogger(__name__)


@DataSourceRegistry.register("binance")
class BinanceDataSource(BaseDataSource):
    """
    Binance 数据源

    WebSocket 端点:
    - 现货: wss://stream.binance.com:9443/ws
    - 合约: wss://fstream.binance.com/ws

    REST API 端点:
    - 现货: https://api.binance.com
    - 合约: https://fapi.binance.com
    """

    name = "binance"
    source_type = "crypto"

    supported_data_types = [
        DataType.KLINE,
        DataType.TICKER,
        DataType.TRADE,
        DataType.DEPTH,
    ]

    supported_market_types = [
        MarketType.SPOT,
        MarketType.FUTURES,
    ]

    supported_intervals = [
        KlineInterval.M1,
        KlineInterval.M3,
        KlineInterval.M5,
        KlineInterval.M15,
        KlineInterval.M30,
        KlineInterval.H1,
        KlineInterval.H4,
        KlineInterval.D1,
        KlineInterval.W1,
        KlineInterval.M1_MONTH,
    ]

    ws_futures_endpoint = "wss://fstream.binance.com:443/ws"

    @property
    def ws_spot_endpoint(self) -> str:
        """现货行情 WS 端点（可经 ``BINANCE_SPOT_WS_ENDPOINT`` 覆盖）。

        默认 ``stream.binance.com``；若该路径被网络/代理重置（握手成功但
        连接随即被 RST、收不到 ticker），可切到 Binance 官方行情端点
        ``wss://data-stream.binance.vision/ws``——同样的数据、不同的域名，
        在部分代理/区域网络下更稳。空值/缺省时回退默认端点。
        """
        return (
            getattr(settings, "BINANCE_SPOT_WS_ENDPOINT", None)
            or "wss://stream.binance.com:443/ws"
        )

    rest_spot_endpoint = "https://api.binance.com"
    rest_futures_endpoint = "https://fapi.binance.com"

    # WebSocket ping 间隔（分钟）
    WS_PING_INTERVAL = 3

    # Binance 每连接 SUBSCRIBE/UNSUBSCRIBE 限速约 5 次/秒，超限以
    # 1008 "Too many requests" 断开连接。同市场 SUBSCRIBE 帧的最小
    # 间隔（秒）：启动期 supervisor/FrameManager/实盘恢复会在同一秒内
    # 连续 subscribe，按此间隔节流后帧率恒定低于限速。
    WS_SUBSCRIBE_MIN_INTERVAL = 0.3

    def __init__(self):
        """初始化"""
        super().__init__()

        # WebSocket 连接
        self._ws_spot: Optional[aiohttp.ClientWebSocketResponse] = None
        self._ws_futures: Optional[aiohttp.ClientWebSocketResponse] = None

        # HTTP 客户端
        self._http_client: Optional[aiohttp.ClientSession] = None

        # CCXT 客户端（备用）
        self._ccxt_spot: Optional[ccxt.binance] = None
        self._ccxt_futures: Optional[ccxt.binanceusdm] = None

        # WebSocket 消息处理任务
        self._ws_tasks: List[asyncio.Task] = []

        # 连接互斥锁：supervisor 与 FrameManager 等调用方可能在启动时并发
        # connect 同一单例。无锁时两次 connect 各建一套 WS 与接收任务，
        # _ws_tasks 只跟踪后一套，前一套成孤儿任务 → 并发 receive 同一 WS
        # 崩溃（"Concurrent call to receive() is not allowed"），且订阅帧
        # 可能发往被覆盖的连接（ticker 断流 → 503）。所有 connect/
        # disconnect 都经 _foreign_engine_loop 委托到引擎循环，锁始终在
        # 同一循环上获取。
        self._ws_connect_lock = asyncio.Lock()

        # SUBSCRIBE 帧限速（见 WS_SUBSCRIBE_MIN_INTERVAL）：按市场各一把锁 +
        # 上次发送时刻，把同连接 SUBSCRIBE 帧串行并拉开最小间隔，避免启动期
        # 多个调用方连续 subscribe 触发 Binance 1008 限速断连。
        self._ws_sub_locks: Dict[MarketType, asyncio.Lock] = {
            m: asyncio.Lock() for m in (MarketType.SPOT, MarketType.FUTURES)
        }
        self._ws_last_sub_send: Dict[MarketType, float] = {}

        # Ping 任务
        self._ping_task: Optional[asyncio.Task] = None

        # 数据存储
        self._store = get_data_store()

        # 质量监控
        self._monitor = get_quality_monitor()

        # 已通过 WS 收到首个 ticker 的符号集合（用于一次性日志）
        self._ticker_logged: set = set()

    @property
    def _proxy(self) -> Optional[str]:
        """获取代理 URL，从 Django settings 读取 WEB_PROXY"""
        return getattr(settings, "WEB_PROXY", "") or None

    def _ensure_http_client(self) -> aiohttp.ClientSession:
        """创建/复用 HTTP 客户端。

        统一应用 WEB_PROXY（WebSocket 与 REST 共用同一会话），
        避免 REST 请求（ticker/klines/trades）绕过代理直连被墙端点。

        aiohttp.ClientSession 与创建它的 event loop 绑定；本数据源可能被
        不同的 loop 调用（如 API 视图每个请求新建一个 loop）。若缓存的
        session 属于已关闭的旧 loop，必须丢弃并在当前 loop 重建，否则会报
        "Timeout context manager should be used inside a task"。
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if self._http_client is not None:
            if getattr(self, "_http_client_loop", None) is loop:
                return self._http_client
            # 缓存的 session 属于另一个 loop：丢弃，在当前 loop 重建。
            # 注意：若被丢弃的 session 上还有存活的 WS 连接（引擎循环的
            # 行情会话被 REST 调用顶掉），WS 会随之失去会话、断流一次，
            # 由看门狗重连兜底——此处必须留日志定位该类断连。
            logger.warning(
                "[Binance] evicting HTTP session id=%s on loop mismatch "
                "(cached_loop=%s now=%s)",
                id(self._http_client),
                getattr(self, "_http_client_loop", None),
                loop,
            )
            self._http_client = None

        proxy = self._proxy
        if proxy:
            from urllib.parse import urlparse
            from aiohttp_socks import ProxyConnector, ProxyType

            parsed = urlparse(proxy)
            _proxy_type = {
                "socks5": ProxyType.SOCKS5,
                "socks5h": ProxyType.SOCKS5,
                "socks4": ProxyType.SOCKS4,
            }.get(parsed.scheme, ProxyType.HTTP)
            connector = ProxyConnector(
                proxy_type=_proxy_type,
                host=parsed.hostname,
                port=parsed.port,
                username=parsed.username,
                password=parsed.password,
                rdns=True,
            )
            self._http_client = aiohttp.ClientSession(connector=connector)
        else:
            self._http_client = aiohttp.ClientSession()
        self._http_client_loop = loop
        logger.info(
            "[Binance] HTTP client created, id=%s, proxy=%s, loop=%s",
            id(self._http_client),
            proxy,
            loop,
        )
        return self._http_client

    async def close_http_client(self) -> None:
        """关闭 HTTP 客户端（下次使用时由 ``_ensure_http_client`` 惰性重建）。

        WS 连接失败时调用：旧的 aiohttp 会话（含代理连接器）可能已损坏，
        必须丢弃，否则重连会持续失败。
        """
        client = self._http_client
        if client is None:
            return
        self._http_client = None
        self._http_client_loop = None
        try:
            await client.close()
            logger.info("[Binance] HTTP client closed, id=%s", id(client))
        except Exception as e:
            logger.warning("[Binance] close_http_client error: %s", e)

    # ==================== 跨循环安全 ====================

    def _foreign_engine_loop(self) -> Optional[asyncio.AbstractEventLoop]:
        """返回需要委托的 ws_runner 引擎循环；无需委托时返回 None。

        数据源是共享单例，会被多个调用方以各自的事件循环触碰：
        - ws_runner supervisor（引擎循环，正确）
        - 订阅管理器（旧 ``new_event_loop`` 模式，临时循环）
        - 交易框架（asgiref ``async_to_sync`` 临时循环 / agent 任务循环）
        - 策略实盘 LiveStrategyRunner（独立循环）

        WS 连接与接收任务只绑定创建它们的循环；其它循环上的
        connect/send/close 会产生 aiohttp "attached to a different loop"、
        "Cannot write to closing transport"、"Timeout context manager
        should be used inside a task" 等错误——这是 ticker 持久 503 的
        根因之一。引擎循环已启动且当前循环不是它时，必须委托回去。
        """
        from apps.datasource.ws_runner import get_engine

        engine_loop = getattr(get_engine(), "loop", None)
        if engine_loop is None:
            # 引擎尚未启动（如测试早期）：保持原行为直接执行
            return None
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is engine_loop:
            return None
        return engine_loop

    async def _run_on_engine(self, coro_factory: Callable[[], Any]) -> Any:
        """把协程调度到引擎循环执行，当前线程阻塞至完成（跨循环安全）。"""
        from apps.datasource.ws_runner import get_engine

        return asyncio.run_coroutine_threadsafe(
            coro_factory(), get_engine().loop
        ).result()

    # ==================== WebSocket 连接 ====================

    async def connect_websocket(self) -> bool:
        """建立 WebSocket 连接（逐市场独立，任一市场成功即视为已连接）。

        修复：旧实现是"全有或全无"——任一市场 ws_connect 抛错会整体失败，
        supervisor 随即调用 close_http_client 关闭 HTTP 会话，把已连上的
        其他市场（如现货）连接一并掐断，SUBSCRIBE 帧从未发出，导致
        store 一直为空、ticker 接口持续 503。现在每个市场独立 try/except，
        只要求"至少一个市场连上"（与 ws_healthy 的订阅感知策略一致）。

        并发安全：整个流程持 ``_ws_connect_lock``。启动时 supervisor 与
        FrameManager 可能并发调用本方法——无锁时后到的 connect 会为已打开
        的市场再建一套接收任务（_ws_tasks 被整体重置，先建的任务成孤儿），
        并发 receive 同一 WS 崩溃，且订阅帧可能发往被覆盖的连接。
        """
        # 跨循环安全：非引擎循环调用时调度到引擎循环（见 _foreign_engine_loop），
        # 保证 WS 与接收任务绑定在 ws_runner 持久循环上，而非调用方的临时循环。
        if self._foreign_engine_loop() is not None:
            return await self._run_on_engine(lambda: self.connect_websocket())
        async with self._ws_connect_lock:
            return await self._connect_websocket_unlocked()

    async def _connect_websocket_unlocked(self) -> bool:
        """connect_websocket 主体，调用方须已持有 _ws_connect_lock。"""
        try:
            self._ws_status = ConnectionStatus.CONNECTING
            active_types = self._get_active_market_types()
            logger.info(
                "[Binance] connecting WebSocket, markets=%s, spot_endpoint=%s, futures_endpoint=%s",
                [mt.value for mt in active_types],
                self.ws_spot_endpoint,
                self.ws_futures_endpoint,
            )

            # 创建 HTTP 客户端（带代理）
            self._ensure_http_client()

            connected: List[MarketType] = []
            newly_connected: List[MarketType] = []
            for mt in active_types:
                ws_attr = "_ws_spot" if mt == MarketType.SPOT else "_ws_futures"
                endpoint = (
                    self.ws_spot_endpoint
                    if mt == MarketType.SPOT
                    else self.ws_futures_endpoint
                )
                ws = getattr(self, ws_attr)
                if ws is not None and not ws.closed:
                    connected.append(mt)
                    continue
                try:
                    ws = await self._http_client.ws_connect(
                        endpoint,
                        heartbeat=self.WS_PING_INTERVAL * 60,
                        receive_timeout=30,
                    )
                    setattr(self, ws_attr, ws)
                    connected.append(mt)
                    newly_connected.append(mt)
                    logger.info("[Binance] %s WebSocket connected", mt.value)
                except Exception as e:
                    setattr(self, ws_attr, None)
                    logger.error(
                        "[Binance] %s WebSocket connect failed: %s: %s",
                        mt.value,
                        type(e).__name__,
                        e,
                    )

            if not connected:
                self._ws_status = ConnectionStatus.ERROR
                logger.error(
                    "[Binance] WebSocket connect failed for all markets: %s",
                    [mt.value for mt in active_types],
                )
                return False

            # 只为本次新建 WS 的市场启动接收任务：已打开的市场沿用既有任务
            # （_ws_tasks 不再整体重置）。否则并发/重复 connect 会为同一 WS
            # 创建重复任务——孤儿任务与既有任务并发 receive 同一 WS 崩溃，
            # 且旧任务句柄丢失、永远无法被 disconnect 取消。
            for mt in newly_connected:
                receiver = (
                    self._ws_spot_receiver
                    if mt == MarketType.SPOT
                    else self._ws_futures_receiver
                )
                self._ws_tasks.append(asyncio.create_task(receiver()))

            self._ws_status = ConnectionStatus.CONNECTED
            self._connected_at = datetime.now()

            # 诊断：连接后各市场 WS 的实际状态（spot/futures 可能一活一死）
            logger.info(
                "[Binance] ws state after connect: spot=%s futures=%s",
                "open"
                if self._ws_spot is not None and not self._ws_spot.closed
                else "none/closed",
                "open"
                if self._ws_futures is not None and not self._ws_futures.closed
                else "none/closed",
            )

            # 发送活跃订阅（只发给已连上的市场；_send_subscribe 会跳过未连市场）
            await self._resubscribe_all()

            # 诊断：未连上的市场——有活跃订阅的市场会影响对应功能
            failed = [mt for mt in active_types if mt not in connected]
            if failed:
                failed_with_subs = [
                    mt.value
                    for mt in failed
                    if any(
                        s.get("market_type") == mt and s.get("active")
                        for s in self._subscriptions.values()
                    )
                ]
                logger.warning(
                    "[Binance] WebSocket markets not connected: %s (with active subs: %s)",
                    [mt.value for mt in failed],
                    failed_with_subs,
                )

            status = self.get_status()
            logger.info(
                "[Binance] WebSocket connected: status=%s, subs=%d, last_data=%s",
                status["status"],
                status["subscriptions"],
                status["last_data_time"],
            )
            return True

        except Exception as e:
            self._ws_status = ConnectionStatus.ERROR
            logger.error(
                "[Binance] WebSocket connection error: %s: %s\n%s",
                type(e).__name__,
                e,
                traceback.format_exc(),
            )
            return False

    async def disconnect_websocket(self, preserve_subs: bool = False) -> bool:
        """断开 WebSocket 连接

        Args:
            preserve_subs: True 时保留已登记的订阅（supervisor 重连用：
                重连后 connect_websocket 的 _resubscribe_all 会重新发送，
                避免连坐清空其他调用方（策略/订阅管理器）的订阅）。
        """
        # 跨循环安全（同 connect_websocket）：任务取消/连接关闭必须在引擎循环上
        if self._foreign_engine_loop() is not None:
            return await self._run_on_engine(
                lambda: self.disconnect_websocket(preserve_subs=preserve_subs)
            )
        # 与 connect 互斥：否则断开期间并发 connect 会取消掉刚创建的任务、
        # 关闭刚建立的连接，造成"断开后仍是 connected"的状态错乱
        async with self._ws_connect_lock:
            return await self._disconnect_websocket_unlocked(
                preserve_subs=preserve_subs
            )

    async def _disconnect_websocket_unlocked(self, preserve_subs: bool = False) -> bool:
        """disconnect_websocket 主体，调用方须已持有 _ws_connect_lock。"""
        try:
            logger.info(
                "[Binance] disconnecting WebSocket, current_subs=%d",
                len(self._subscriptions),
            )

            # 取消消息处理任务
            for task in self._ws_tasks:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            self._ws_tasks = []

            # 关闭 WebSocket 连接
            if self._ws_spot and not self._ws_spot.closed:
                await self._ws_spot.close()
            self._ws_spot = None

            if self._ws_futures and not self._ws_futures.closed:
                await self._ws_futures.close()
            self._ws_futures = None

            self._ws_status = ConnectionStatus.DISCONNECTED

            # 清空旧订阅，避免 _resubscribe_all 在新连接中发送过期流
            # （preserve_subs=True 时保留：supervisor 重连会重新发送全部订阅）
            if not preserve_subs:
                self._subscriptions.clear()

            logger.info("[Binance] WebSocket disconnected")
            return True

        except Exception as e:
            logger.error("[Binance] WebSocket disconnect error: %s", e)
            return False

    def ws_healthy(self) -> bool:
        """活跃市场 WS 存活检查（订阅感知）。

        - 某市场**有活跃订阅**时，其 WS 必须存活（状态标志由
          connect/disconnect 手动维护，单市场断连时标志可能仍是 CONNECTED）；
        - 某市场**无活跃订阅**时不要求连接存活：空连接会在空闲超时后被
          服务端断开（Binance 断开无订阅的空闲连接），强行保活会造成
          无限重连抖动。之后该市场新增订阅时，本方法自动转为要求存活，
          看门狗重连后 ``_resubscribe_all`` 会补发该订阅。
        """
        try:
            active = self._get_active_market_types()
        except Exception:
            return True
        for mt in active:
            ws = self._ws_spot if mt == MarketType.SPOT else self._ws_futures
            has_sub = any(
                s.get("market_type") == mt and s.get("active")
                for s in self._subscriptions.values()
            )
            if has_sub and (ws is None or ws.closed):
                return False
        return True

    async def _ws_spot_receiver(self) -> None:
        """现货 WebSocket 消息接收"""
        while self._ws_spot and not self._ws_spot.closed:
            try:
                msg = await self._ws_spot.receive()
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._handle_websocket_message(
                        json.loads(msg.data), MarketType.SPOT
                    )
                elif msg.type in (
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSING,
                ):
                    # 对端主动关闭（如 Binance 1008 订阅限速）不走
                    # ERROR/CLOSED 分支，不打日志会让接收任务经 while
                    # 条件静默退出、断连原因无从诊断。
                    logger.warning(
                        "[Binance] spot WS closed by peer: %s code=%s reason=%r",
                        msg.type.name,
                        msg.data,
                        msg.extra,
                    )
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    logger.error(
                        "[Binance] spot WS error: %s", self._ws_spot.exception()
                    )
                    break
                elif msg.type == aiohttp.WSMsgType.CLOSED:
                    logger.warning("[Binance] spot WS closed")
                    break

            except asyncio.CancelledError:
                break
            except asyncio.TimeoutError:
                # 空闲（无订阅/无新数据）触发 receive_timeout：属正常现象，
                # aiohttp 会随后关闭连接，不视为错误。
                logger.debug("[Binance] spot WS receive timeout (idle)")
            except Exception as e:
                logger.error(
                    "[Binance] spot WS receive error: %s: %s", type(e).__name__, e
                )
                await asyncio.sleep(1)

    async def _ws_futures_receiver(self) -> None:
        """合约 WebSocket 消息接收"""
        while self._ws_futures and not self._ws_futures.closed:
            try:
                msg = await self._ws_futures.receive()
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._handle_websocket_message(
                        json.loads(msg.data), MarketType.FUTURES
                    )
                elif msg.type in (
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSING,
                ):
                    logger.warning(
                        "[Binance] futures WS closed by peer: %s code=%s reason=%r",
                        msg.type.name,
                        msg.data,
                        msg.extra,
                    )
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    logger.error(
                        "[Binance] futures WS error: %s", self._ws_futures.exception()
                    )
                    break
                elif msg.type == aiohttp.WSMsgType.CLOSED:
                    logger.warning("[Binance] futures WS closed")
                    break

            except asyncio.CancelledError:
                break
            except asyncio.TimeoutError:
                # 空闲（无订阅/无新数据）触发 receive_timeout：属正常现象，
                # aiohttp 会随后关闭连接，不视为错误。
                logger.debug("[Binance] futures WS receive timeout (idle)")
            except Exception as e:
                logger.error(
                    "[Binance] futures WS receive error: %s: %s", type(e).__name__, e
                )
                await asyncio.sleep(1)

    async def _handle_websocket_message(
        self, msg: Dict, market_type: MarketType
    ) -> None:
        """处理 WebSocket 消息"""
        # 记录接收时间
        receive_time = datetime.now()
        self._last_data_time = time.time()

        # 解析消息类型
        event_type = msg.get("e")

        if event_type == "kline":
            # K线数据
            kline = self.normalize_kline(msg)
            kline["market_type"] = market_type.value

            # 存储
            symbol = kline["symbol"]
            self._store.store("kline", symbol, kline, self.name)

            # 监控
            self._monitor.record_data(
                self.name,
                symbol,
                "kline",
                kline.get("timestamp", receive_time),
                receive_time,
            )

            # 触发回调
            logger.debug(
                f"[DataSource] kline: {kline.get('symbol')} close={kline.get('close')} vol={kline.get('volume')}"
            )
            self._trigger_precise_callbacks(DataType.KLINE, kline)

        elif event_type == "trade" or event_type == "aggTrade":
            # 成交数据
            trade = self.normalize_trade(msg)
            trade["market_type"] = market_type.value

            symbol = trade["symbol"]
            self._store.store("trade", symbol, trade, self.name)

            self._monitor.record_data(
                self.name,
                symbol,
                "trade",
                trade.get("timestamp", receive_time),
                receive_time,
            )

            self._trigger_precise_callbacks(DataType.TRADE, trade)

        elif event_type == "24hrTicker" or event_type == "24hrMiniTicker":
            # 行情快照
            ticker = self.normalize_ticker(msg)
            ticker["market_type"] = market_type.value

            symbol = ticker["symbol"]
            if symbol not in self._ticker_logged:
                self._ticker_logged.add(symbol)
                logger.info(
                    "[Binance] first ticker via WS: %s last_price=%s change_pct_24h=%s",
                    symbol,
                    ticker.get("last_price"),
                    ticker.get("change_pct_24h"),
                )
            self._store.store("ticker", symbol, ticker, self.name)

            self._monitor.record_data(
                self.name,
                symbol,
                "ticker",
                ticker.get("timestamp", receive_time),
                receive_time,
            )

            self._trigger_precise_callbacks(DataType.TICKER, ticker)

    async def _send_subscribe_frame(self, market_type: MarketType, msg: Dict) -> None:
        """限速发送 SUBSCRIBE 帧（Binance 每连接约 5 次/秒，超限 1008 断连）。

        同市场的 SUBSCRIBE 帧经同一把锁串行并拉开 WS_SUBSCRIBE_MIN_INTERVAL
        最小间隔：启动期 supervisor/FrameManager/实盘恢复会同一秒内连续
        subscribe，若各发各帧会超限被掐断。所有 SUBSCRIBE 发送都走此方法。
        """
        ws = self._ws_spot if market_type == MarketType.SPOT else self._ws_futures
        if ws is None or ws.closed:
            return
        async with self._ws_sub_locks[market_type]:
            loop = asyncio.get_running_loop()
            wait = self.WS_SUBSCRIBE_MIN_INTERVAL - (
                loop.time() - self._ws_last_sub_send.get(market_type, 0.0)
            )
            if wait > 0:
                await asyncio.sleep(wait)
            self._ws_last_sub_send[market_type] = loop.time()
            await ws.send_str(json.dumps(msg))

    async def _resubscribe_all(self) -> None:
        """重新发送所有订阅（跳过已取消的订阅）。

        同一市场的全部流合并进**一条** SUBSCRIBE 帧（params 支持多流），
        并经 _send_subscribe_frame 限速：逐订阅各发一帧时，订阅数超过约
        5/秒会触发 Binance 每连接订阅速率限制，服务端以 1008 "Too many
        requests" 断开连接（重连后再次超限 → 无限重连抖动、ticker 断流 503）。
        """
        for market_type in (MarketType.SPOT, MarketType.FUTURES):
            ws = self._ws_spot if market_type == MarketType.SPOT else self._ws_futures
            if ws is None or ws.closed:
                continue
            streams: List[str] = []
            for sub_info in self._subscriptions.values():
                if not sub_info.get("active", True):
                    continue
                if sub_info.get("market_type", MarketType.SPOT) != market_type:
                    continue
                streams.extend(
                    self._build_stream_names(
                        sub_info["symbol"],
                        sub_info["data_type"],
                        sub_info.get("interval"),
                    )
                )
            if not streams:
                continue
            msg = {
                "method": "SUBSCRIBE",
                "params": streams,
                "id": int(time.time() * 1000),
            }
            try:
                logger.info(
                    "[Binance] resubscribe batch market=%s streams=%s",
                    market_type.value,
                    streams,
                )
                await self._send_subscribe_frame(market_type, msg)
            except Exception as e:
                logger.error(
                    "[Binance] resubscribe error for %s: %s", market_type.value, e
                )

    async def _send_subscribe(self, sub_info: Dict) -> None:
        """发送订阅请求"""
        symbol = sub_info["symbol"]
        data_type = sub_info["data_type"]
        interval = sub_info.get("interval")
        market_type = sub_info.get("market_type", MarketType.SPOT)

        # 构建 WebSocket 消息
        ws = self._ws_spot if market_type == MarketType.SPOT else self._ws_futures

        if ws is None or ws.closed:
            logger.warning(
                "[Binance] _send_subscribe skipped: ws=%s (market=%s, symbol=%s)",
                "None" if ws is None else "closed",
                market_type.value,
                symbol,
            )
            return

        # 构建订阅参数
        streams = self._build_stream_names(symbol, data_type, interval)

        msg = {"method": "SUBSCRIBE", "params": streams, "id": int(time.time() * 1000)}

        await self._send_subscribe_frame(market_type, msg)

    def _build_stream_names(
        self, symbol: str, data_type: DataType, interval: Optional[KlineInterval] = None
    ) -> List[str]:
        """构建 WebSocket stream 名称"""
        symbol_lower = symbol.lower().replace("/", "")

        if data_type == DataType.KLINE:
            interval_str = interval.value if interval else "1m"
            return [f"{symbol_lower}@kline_{interval_str}"]

        elif data_type == DataType.TRADE:
            return [f"{symbol_lower}@aggTrade"]

        elif data_type == DataType.TICKER:
            return [f"{symbol_lower}@ticker"]

        elif data_type == DataType.DEPTH:
            return [f"{symbol_lower}@depth@100ms"]

        return []

    # ==================== REST API ====================

    async def fetch_klines(
        self,
        symbol: str,
        interval: KlineInterval,
        market_type: MarketType = MarketType.SPOT,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        limit: int = 1000,
    ) -> List[Dict]:
        """获取历史 K 线数据"""
        try:
            # 初始化 CCXT 客户端
            await self._init_ccxt(market_type)

            # 参数
            params = {
                "symbol": symbol.replace("/", ""),  # Binance 格式
                "interval": interval.value,
                "limit": min(limit, 1000),
            }

            if start_time:
                params["startTime"] = int(start_time.timestamp() * 1000)
            if end_time:
                params["endTime"] = int(end_time.timestamp() * 1000)

            # API 端点
            endpoint = (
                "/api/v3/klines"
                if market_type == MarketType.SPOT
                else "/fapi/v1/klines"
            )

            # 发送请求
            url = (
                f"{self.rest_spot_endpoint}{endpoint}"
                if market_type == MarketType.SPOT
                else f"{self.rest_futures_endpoint}{endpoint}"
            )

            self._ensure_http_client()

            async with self._http_client.get(url, params=params) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    klines = [
                        self.normalize_kline_rest(k, symbol, interval) for k in data
                    ]

                    # 批量存储
                    self._store.store_batch("kline", symbol, klines, self.name)

                    return klines
                else:
                    error_text = await resp.text()
                    raise Exception(f"API error {resp.status}: {error_text}")

        except Exception as e:
            logger.error("[Binance] fetch_klines error: %s", e)
            return []

    async def fetch_trades(
        self,
        symbol: str,
        market_type: MarketType = MarketType.SPOT,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        limit: int = 1000,
    ) -> List[Dict]:
        """获取历史成交数据"""
        try:
            endpoint = (
                "/api/v3/trades"
                if market_type == MarketType.SPOT
                else "/fapi/v1/trades"
            )

            url = (
                f"{self.rest_spot_endpoint}{endpoint}"
                if market_type == MarketType.SPOT
                else f"{self.rest_futures_endpoint}{endpoint}"
            )

            params = {
                "symbol": symbol.replace("/", ""),
                "limit": min(limit, 1000),
            }

            self._ensure_http_client()

            async with self._http_client.get(url, params=params) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    trades = [self.normalize_trade_rest(t, symbol) for t in data]

                    self._store.store_batch("trade", symbol, trades, self.name)

                    return trades
                else:
                    error_text = await resp.text()
                    raise Exception(f"API error {resp.status}: {error_text}")

        except Exception as e:
            logger.error("[Binance] fetch_trades error: %s", e)
            return []

    async def fetch_ticker(
        self, symbol: str, market_type: MarketType = MarketType.SPOT
    ) -> Dict:
        """获取实时行情快照"""
        try:
            endpoint = (
                "/api/v3/ticker/24hr"
                if market_type == MarketType.SPOT
                else "/fapi/v1/ticker/24hr"
            )

            url = (
                f"{self.rest_spot_endpoint}{endpoint}"
                if market_type == MarketType.SPOT
                else f"{self.rest_futures_endpoint}{endpoint}"
            )

            params = {"symbol": symbol.replace("/", "")}

            self._ensure_http_client()

            async with self._http_client.get(url, params=params) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    ticker = self.normalize_ticker_rest(data, symbol)

                    self._store.store("ticker", symbol, ticker, self.name)

                    return ticker
                else:
                    error_text = await resp.text()
                    raise Exception(f"API error {resp.status}: {error_text}")

        except Exception as e:
            logger.error(f"[Binance] fetch_ticker error: {e}")
            return {}

    async def _init_ccxt(self, market_type: MarketType) -> None:
        """初始化 CCXT 客户端"""
        options = {"enableRateLimit": True}
        proxy = self._proxy
        if proxy:
            options["aiohttp_proxy"] = proxy
        if market_type == MarketType.SPOT:
            if self._ccxt_spot is None:
                self._ccxt_spot = ccxt.binance(options)
        else:
            if self._ccxt_futures is None:
                self._ccxt_futures = ccxt.binanceusdm(options)

    # ==================== 数据订阅 ====================

    async def subscribe(
        self,
        symbol: str,
        data_type: DataType,
        interval: Optional[KlineInterval] = None,
        market_type: MarketType = MarketType.SPOT,
        callback: Optional[Callable] = None,
    ) -> bool:
        """订阅数据"""
        # 跨循环安全：注册/发帧必须在引擎循环上完成，避免对引擎循环持有的
        # WS 从其它循环 send_str（策略实盘/框架等调用方所在循环各不相同）。
        if self._foreign_engine_loop() is not None:
            return await self._run_on_engine(
                lambda: self.subscribe(
                    symbol,
                    data_type,
                    interval=interval,
                    market_type=market_type,
                    callback=callback,
                )
            )
        # 注册回调（精确注册：按 data_type + symbol + interval 路由）
        # 注意：normalize_kline 后 kline["symbol"] 是 binance 原始格式 (DOGEUSDT)，
        # 所以注册时也要用同一种格式，否则 _trigger_precise_callbacks 键不匹配。
        if callback:
            interval_str = interval.value if interval else None
            cb_symbol = symbol.replace("/", "") if symbol else symbol
            self.register_callback(
                data_type, callback, symbol=cb_symbol, interval=interval_str
            )

        # 生成订阅键
        sub_key = f"{symbol}:{data_type.value}:{interval.value if interval else 'none'}:{market_type.value}"

        logger.info("[Binance] subscribe: %s", sub_key)

        # 同一 key 的重复订阅：流已在当前连接上发出（首次 subscribe 时发送，
        # 或重连后由 _resubscribe_all 批量补发），只需更新订阅条目（如回调
        # 变更），不能重发 SUBSCRIBE 帧——重发会空耗 Binance 每连接订阅速率
        # 配额（约 5/秒），触发 1008 "Too many requests" 断连。
        existed_active = sub_key in self._subscriptions and self._subscriptions[
            sub_key
        ].get("active", True)

        # 存储订阅信息
        self._subscriptions[sub_key] = {
            "symbol": symbol,
            "data_type": data_type,
            "interval": interval,
            "market_type": market_type,
            "callback": callback,
            "active": True,
        }

        if existed_active:
            return True

        # 发送订阅：只要对应市场的 WS 处于打开状态就立即发送。
        # 不能只依赖状态标志：多事件循环并发 connect 时存在 CONNECTING 窗口，
        # 旧条件（仅 CONNECTED 时发送）会让订阅被登记却从未发出 SUBSCRIBE 帧，
        # 而之后若无重连，该流就永久缺失（head bar ticker 缺失的根因）。
        # 若 WS 尚未建立，订阅已登记在 _subscriptions，下次 connect 的
        # _resubscribe_all 会补发。
        ws = self._ws_spot if market_type == MarketType.SPOT else self._ws_futures
        if ws is not None and not ws.closed:
            try:
                await self._send_subscribe(self._subscriptions[sub_key])
                return True
            except Exception as e:
                logger.error(f"[Binance] subscribe error: {e}")
                return False

        return True

    async def unsubscribe(
        self,
        symbol: str,
        data_type: DataType,
        interval: Optional[KlineInterval] = None,
        market_type: MarketType = MarketType.SPOT,
        callback: Optional[Callable] = None,
    ) -> bool:
        """取消订阅。可传 callback 精确注销该回调（不传则只停 WS 流）。"""
        # 跨循环安全（同 subscribe）：取消帧发送必须在引擎循环上完成
        if self._foreign_engine_loop() is not None:
            return await self._run_on_engine(
                lambda: self.unsubscribe(
                    symbol,
                    data_type,
                    interval=interval,
                    market_type=market_type,
                    callback=callback,
                )
            )
        sub_key = f"{symbol}:{data_type.value}:{interval.value if interval else 'none'}:{market_type.value}"
        # 与 subscribe 保持一致：register_callback 用 binance 原始格式
        cb_symbol = symbol.replace("/", "") if symbol else symbol

        if sub_key not in self._subscriptions:
            # 即使 WS 没订阅，也清理精确回调（避免幽灵回调）
            if callback:
                interval_str = interval.value if interval else None
                self.unregister_callback(
                    data_type, callback, symbol=cb_symbol, interval=interval_str
                )
            return False

        # 标记为非活跃
        self._subscriptions[sub_key]["active"] = False

        # 如果已连接，发送取消订阅
        if self._ws_status == ConnectionStatus.CONNECTED:
            try:
                ws = (
                    self._ws_spot
                    if market_type == MarketType.SPOT
                    else self._ws_futures
                )
                if ws and not ws.closed:
                    streams = self._build_stream_names(symbol, data_type, interval)

                    msg = {
                        "method": "UNSUBSCRIBE",
                        "params": streams,
                        "id": int(time.time() * 1000),
                    }

                    # UNSUBSCRIBE 与 SUBSCRIBE 共享同一每连接限速配额，
                    # 走同一限速通道，避免与订阅突发叠加触发 1008。
                    await self._send_subscribe_frame(market_type, msg)

            except Exception as e:
                logger.error("[Binance] unsubscribe error: %s", e)

        # 移除订阅
        del self._subscriptions[sub_key]

        # 清理精确回调
        if callback:
            interval_str = interval.value if interval else None
            self.unregister_callback(
                data_type, callback, symbol=cb_symbol, interval=interval_str
            )

        return True

    # ==================== 数据标准化 ====================

    def normalize_kline(self, raw_data: Any) -> Dict:
        """标准化 WebSocket K 线数据"""
        k = raw_data.get("k", {})

        return {
            "symbol": k.get("s"),
            "interval": k.get("i"),
            "open_time": datetime.fromtimestamp(k.get("t", 0) / 1000, tz=timezone.utc),
            "close_time": datetime.fromtimestamp(k.get("T", 0) / 1000, tz=timezone.utc),
            "open": float(k.get("o", 0)),
            "high": float(k.get("h", 0)),
            "low": float(k.get("l", 0)),
            "close": float(k.get("c", 0)),
            "volume": float(k.get("v", 0)),
            "turnover": float(k.get("q", 0)),
            "trades": int(k.get("n", 0)),
            "is_closed": k.get("x", False),
            "source": self.name,
            "timestamp": datetime.fromtimestamp(k.get("t", 0) / 1000, tz=timezone.utc),
        }

    def normalize_kline_rest(
        self, raw_data: Any, symbol: str, interval: KlineInterval
    ) -> Dict:
        """标准化 REST API K 线数据"""
        return {
            "symbol": symbol,
            "interval": interval.value,
            "open_time": datetime.fromtimestamp(raw_data[0] / 1000, tz=timezone.utc),
            "close_time": datetime.fromtimestamp(raw_data[6] / 1000, tz=timezone.utc),
            "open": float(raw_data[1]),
            "high": float(raw_data[2]),
            "low": float(raw_data[3]),
            "close": float(raw_data[4]),
            "volume": float(raw_data[5]),
            "turnover": float(raw_data[7]),
            "trades": int(raw_data[8]),
            "source": self.name,
            "timestamp": datetime.fromtimestamp(raw_data[0] / 1000, tz=timezone.utc),
        }

    def normalize_trade(self, raw_data: Any) -> Dict:
        """标准化 WebSocket 成交数据"""
        return {
            "symbol": raw_data.get("s"),
            "trade_id": str(raw_data.get("a", raw_data.get("t", 0))),
            "price": float(raw_data.get("p", 0)),
            "quantity": float(raw_data.get("q", 0)),
            "side": "buy" if raw_data.get("m", False) else "sell",
            "timestamp": datetime.fromtimestamp(
                raw_data.get("T", 0) / 1000, tz=timezone.utc
            ),
            "source": self.name,
        }

    def normalize_trade_rest(self, raw_data: Any, symbol: str) -> Dict:
        """标准化 REST API 成交数据"""
        return {
            "symbol": symbol,
            "trade_id": str(raw_data.get("id", 0)),
            "price": float(raw_data.get("price", 0)),
            "quantity": float(raw_data.get("qty", 0)),
            "side": "buy" if raw_data.get("isBuyerMaker", False) else "sell",
            "timestamp": datetime.fromtimestamp(
                raw_data.get("time", 0) / 1000, tz=timezone.utc
            ),
            "source": self.name,
        }

    def normalize_ticker(self, raw_data: Any) -> Dict:
        """标准化 WebSocket 行情数据"""
        return {
            "symbol": raw_data.get("s"),
            "last_price": float(raw_data.get("c", 0)),
            "bid_price": float(raw_data.get("b", 0)),
            "bid_quantity": float(raw_data.get("B", 0)),
            "ask_price": float(raw_data.get("a", 0)),
            "ask_quantity": float(raw_data.get("A", 0)),
            "high_24h": float(raw_data.get("h", 0)),
            "low_24h": float(raw_data.get("l", 0)),
            "volume_24h": float(raw_data.get("v", 0)),
            "turnover_24h": float(raw_data.get("q", 0)),
            "change_24h": float(raw_data.get("p", 0)),
            "change_pct_24h": float(raw_data.get("P", 0)),
            "timestamp": datetime.now(),
            "source": self.name,
        }

    def normalize_ticker_rest(self, raw_data: Any, symbol: str) -> Dict:
        """标准化 REST API 行情数据"""
        return {
            "symbol": symbol,
            "last_price": float(raw_data.get("lastPrice", 0)),
            "bid_price": float(raw_data.get("bidPrice", 0)),
            "bid_quantity": float(raw_data.get("bidQty", 0)),
            "ask_price": float(raw_data.get("askPrice", 0)),
            "ask_quantity": float(raw_data.get("askQty", 0)),
            "high_24h": float(raw_data.get("highPrice", 0)),
            "low_24h": float(raw_data.get("lowPrice", 0)),
            "volume_24h": float(raw_data.get("volume", 0)),
            "turnover_24h": float(raw_data.get("quoteVolume", 0)),
            "change_24h": float(raw_data.get("priceChange", 0)),
            "change_pct_24h": float(raw_data.get("priceChangePercent", 0)),
            "timestamp": datetime.fromtimestamp(
                raw_data.get("closeTime", 0) / 1000, tz=timezone.utc
            ),
            "source": self.name,
        }

    # ==================== 清理 ====================

    def cleanup(self) -> None:
        """清理资源"""
        super().cleanup()

        # 关闭 CCXT 客户端
        if self._ccxt_spot:
            asyncio.run_coroutine_threadsafe(
                self._ccxt_spot.close(), asyncio.get_event_loop()
            )
            self._ccxt_spot = None

        if self._ccxt_futures:
            asyncio.run_coroutine_threadsafe(
                self._ccxt_futures.close(), asyncio.get_event_loop()
            )
            self._ccxt_futures = None

        # 关闭 HTTP 客户端
        if self._http_client:
            asyncio.run_coroutine_threadsafe(
                self._http_client.close(), asyncio.get_event_loop()
            )
            self._http_client = None
