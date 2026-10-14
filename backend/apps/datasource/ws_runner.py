"""
WS 持久事件循环与实时行情供给

背景
----
数据源的 WebSocket 接收任务（如 ``BinanceDataSource._ws_spot_receiver``）通过
``asyncio.create_task()`` 创建，必须活在**长驻**事件循环里。旧调用模式::

    loop = asyncio.new_event_loop()
    loop.run_until_complete(ds.connect_websocket())
    loop.close()

在 ``loop.close()`` 时立即取消接收任务，导致 WS 消息从未被读取、内存存储
（store.py）从未被填充。

本模块提供进程级持久事件循环（daemon 线程）：

- ``start_market_feed()``: 幂等入口，在 ``AppConfig.ready()`` 中调用；
  supervisor 协程保持数据源 WS 连接并订阅指定流，断线/数据过期自动重连
- ``run_ws_coroutine()``: 在持久循环上运行数据源协程（线程安全，可从 API
  请求线程调用）

行情数据路径（纯 WS，无 REST 回退）::

    Binance WS @ticker 流 -> _handle_websocket_message -> store
    -> MarketDataViewSet.ticker 读 store（未就绪返回 503）
"""

import asyncio
import logging
import os
import threading
import time
from concurrent.futures import Future
from datetime import datetime
from typing import Any, Coroutine, List, Optional, TypeVar

from apps.datasource.base import DataType, MarketType

logger = logging.getLogger(__name__)

T = TypeVar("T")

# 默认实时行情交易对（现货，head bar 展示）
DEFAULT_FEED_SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]

# 看门狗参数
_WATCHDOG_INTERVAL = 10.0  # 每 10 秒检查一次
_STALE_DATA_SECONDS = 90.0  # 90 秒无数据视为过期
_NO_DATA_GRACE_SECONDS = 120.0  # 连接后 120 秒仍无数据视为故障

_engine: Optional["_WSEngine"] = None
_engine_lock = threading.Lock()
_feed_started = False
_feed_lock = threading.Lock()


class _WSEngine:
    """daemon 线程中的长驻事件循环（进程内单例）。"""

    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()

    @property
    def loop(self) -> Optional[asyncio.AbstractEventLoop]:
        """当前引擎事件循环（未启动时为 None）。

        数据源侧跨循环委托（``BinanceDataSource._foreign_engine_loop``）
        依赖此属性判断调用方是否在引擎循环上。
        """
        return self._loop

    def start(self) -> asyncio.AbstractEventLoop:
        """启动事件循环（幂等），返回已就绪的 loop。"""
        if self._thread is not None and self._thread.is_alive():
            if not self._ready.wait(5):
                raise RuntimeError("WS engine loop did not become ready")
            return self._loop

        self._ready.clear()
        self._thread = threading.Thread(
            target=self._run, name="datasource-ws-loop", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(5):
            raise RuntimeError("WS engine loop did not become ready")
        return self._loop

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            loop.close()

    def run(self, coro: Coroutine[Any, Any, T], timeout: Optional[float] = None) -> T:
        """在持久循环上运行协程（线程安全，阻塞至完成）。"""
        loop = self._loop
        if loop is None or loop.is_closed():
            coro.close()
            raise RuntimeError("WS engine loop is not running")
        return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout)


def get_engine() -> _WSEngine:
    """获取进程级持久事件循环引擎。"""
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                _engine = _WSEngine()
    return _engine


def run_ws_coroutine(
    coro: Coroutine[Any, Any, T], timeout: Optional[float] = None
) -> T:
    """在持久 WS 事件循环上运行协程（线程安全，自动确保循环已启动）。

    用于数据源 connect/disconnect 等操作，避免旧
    ``new_event_loop() + close()`` 模式在连接完成后立即取消
    WebSocket 接收任务。
    """
    get_engine().start()
    return get_engine().run(coro, timeout)


def _feed_data_age(
    store: Any, data_type: DataType, symbols: List[str]
) -> Optional[float]:
    """本供给负责符号的最新数据龄（秒）；尚无数据返回 None。

    存储键可能是 Binance 原始符号（``BTCUSDT``）或斜杠形式（``BTC/USDT``），
    两种都查。取各符号中最新一条的 timestamp。
    """
    now = datetime.now()
    latest: Optional[datetime] = None
    for sym in symbols:
        for key in (sym, sym.replace("/", "")):
            entries = store.get_latest(data_type.value, key, 1)
            if entries:
                ts = entries[0].get("timestamp")
                if isinstance(ts, datetime) and (latest is None or ts > latest):
                    latest = ts
                break
    if latest is None:
        return None
    return max((now - latest).total_seconds(), 0.0)


async def _supervise_feed(
    source: str,
    symbols: List[str],
    data_type: DataType,
    market_type: MarketType,
) -> None:
    """Supervisor：保持数据源 WS 连接并订阅（常驻运行，异常仅记录日志）。"""
    from apps.datasource.registry import DataSourceRegistry

    try:
        ds = DataSourceRegistry.get(source)
    except Exception as e:
        logger.error("[WSRunner] cannot load data source '%s': %s", source, e)
        return

    backoff = 2.0
    while True:
        # 0) 确保本供给所需市场类型在实例活跃集合中（幂等）。
        #    实例可能已被其他调用方以受限市场配置创建（如仅 futures），
        #    此时本供给市场的 WS 永远不会建立、SUBSCRIBE 无处可发。
        try:
            if ds.ensure_market_type(market_type):
                logger.warning(
                    "[WSRunner] %s instance market_types lacked %s; extended (see [DataSource] log)",
                    source,
                    market_type.value,
                )
        except Exception as e:
            logger.warning(
                "[WSRunner] ensure_market_type(%s) failed: %s", market_type.value, e
            )

        # 1) (重)连接
        try:
            ok = await ds.connect_websocket()
        except Exception as e:
            logger.error(
                "[WSRunner] %s connect error: %s: %s",
                source,
                type(e).__name__,
                e,
            )
            ok = False

        if not ok:
            # 旧 HTTP 会话可能已损坏，强制下次连接时惰性重建
            try:
                await ds.close_http_client()
            except Exception:
                pass
            logger.warning(
                "[WSRunner] %s connect failed, retry in %.0fs", source, backoff
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
            continue

        # 2) (重)订阅：disconnect 会清空旧订阅，每次重连后必须重新订阅。
        #    subscribe() 返回 False = 帧发送失败（如 transport 正在关闭），
        #    此时不该宣称 feed ready——交给看门狗在下一轮重连补发。
        failed_subs: List[str] = []
        for sym in symbols:
            try:
                ok = await ds.subscribe(sym, data_type, market_type=market_type)
                if not ok:
                    failed_subs.append(sym)
            except Exception as e:
                failed_subs.append(sym)
                logger.error("[WSRunner] %s subscribe %s error: %s", source, sym, e)
        backoff = 2.0
        if failed_subs:
            logger.warning(
                "[WSRunner] %s feed started but %d/%d subscribes not confirmed: %s "
                "(watchdog will retry on next reconnect)",
                source,
                len(failed_subs),
                len(symbols),
                failed_subs,
            )
        else:
            logger.info(
                "[WSRunner] %s feed ready: type=%s symbols=%s",
                source,
                data_type.value,
                symbols,
            )

        # 3) 看门狗：检查连接状态、各市场 WS 存活、数据新鲜度，不健康则自动重连。
        #    - ws_healthy 覆盖"单市场 WS 已死、另一市场数据仍新鲜"的盲区
        #    - feed_age 覆盖"其他订阅的数据喂活了全局 last_data_time，
        #      本供给的流实际已死"的盲区（必须按本供给符号单独判龄）
        from apps.datasource.store import get_data_store

        store = get_data_store()
        ws_healthy_fn = getattr(ds, "ws_healthy", None)
        while True:
            await asyncio.sleep(_WATCHDOG_INTERVAL)
            now = time.time()
            last = ds.get_last_data_time()
            connected_at = ds.get_connection_time()
            ws_ok = ws_healthy_fn() if callable(ws_healthy_fn) else True
            stale = last > 0 and (now - last) > _STALE_DATA_SECONDS
            never = (
                last == 0
                and connected_at is not None
                and (now - connected_at.timestamp()) > _NO_DATA_GRACE_SECONDS
            )
            feed_age = _feed_data_age(store, data_type, symbols)
            feed_stale = feed_age is not None and feed_age > _STALE_DATA_SECONDS
            feed_never = (
                feed_age is None
                and connected_at is not None
                and (now - connected_at.timestamp()) > _NO_DATA_GRACE_SECONDS
            )
            if (
                not ds.is_connected()
                or not ws_ok
                or stale
                or never
                or feed_stale
                or feed_never
            ):
                logger.warning(
                    "[WSRunner] %s feed unhealthy: connected=%s ws_ok=%s global_age=%s feed_age=%s, reconnecting",
                    source,
                    ds.is_connected(),
                    ws_ok,
                    f"{now - last:.0f}s" if last else "no data",
                    f"{feed_age:.0f}s" if feed_age is not None else "no data",
                )
                try:
                    # preserve_subs：保留已登记的其他调用方（策略/订阅管理器）
                    # 订阅，重连后 connect_websocket 的 _resubscribe_all 会
                    # 全部重发；避免看门狗重连连坐清空它们的流
                    await ds.disconnect_websocket(preserve_subs=True)
                except TypeError:
                    # 兼容未实现 preserve_subs 参数的数据源
                    await ds.disconnect_websocket()
                except Exception as e:
                    logger.warning("[WSRunner] %s disconnect error: %s", source, e)
                # 换全新 HTTP 会话兜底：长运行进程中被跨循环调用/反复 RST
                # 污染的 aiohttp 连接池，重连后可能持续"假活"（握手成功但
                # transport 已死）。丢弃会话让下次 connect 惰性重建。
                try:
                    await ds.close_http_client()
                except Exception as e:
                    logger.warning(
                        "[WSRunner] %s close_http_client error: %s", source, e
                    )
                break  # 回到外层循环重连


def _log_supervisor_failure(fut: "Future[None]") -> None:
    exc = fut.exception()
    if exc is not None:
        logger.error("[WSRunner] supervisor crashed: %s", exc)


def _in_pytest() -> bool:
    """当前是否运行在 pytest 下。

    不能只查 ``PYTEST_CURRENT_TEST``：Django 的 ``AppConfig.ready()``
    在 pytest 设置该变量之前就已执行（django.setup 时机），守卫会失效，
    测试进程会真实启动 supervisor 连外网。``pytest`` 模块在整个测试
    进程生命周期内都在 ``sys.modules`` 中，能覆盖 ready() 时机。
    """
    import sys

    return "pytest" in sys.modules or bool(os.environ.get("PYTEST_CURRENT_TEST"))


def start_market_feed(
    source: str = "binance",
    symbols: Optional[List[str]] = None,
    data_type: DataType = DataType.TICKER,
    market_type: MarketType = MarketType.SPOT,
) -> bool:
    """启动 WS 实时行情供给（幂等、非阻塞，立即返回）。

    守卫：
    - pytest 环境不启动（避免真实外网连接）
    - ``settings.DATASOURCE_AUTO_WS = False`` 时禁用

    成功后，supervisor 协程在持久事件循环上保持供给存活：
    连接 -> 订阅 -> 看门狗（断线/数据过期自动重连）。
    """
    global _feed_started
    with _feed_lock:
        if _feed_started:
            return True
        if _in_pytest():
            logger.info("[WSRunner] skipped in pytest environment")
            return False

        from django.conf import settings

        if not getattr(settings, "DATASOURCE_AUTO_WS", True):
            logger.info("[WSRunner] disabled by DATASOURCE_AUTO_WS")
            return False

        syms = list(symbols) if symbols else list(DEFAULT_FEED_SYMBOLS)

        try:
            loop = get_engine().start()
        except Exception as e:
            logger.error("[WSRunner] failed to start WS engine loop: %s", e)
            return False

        fut = asyncio.run_coroutine_threadsafe(
            _supervise_feed(source, syms, data_type, market_type), loop
        )
        fut.add_done_callback(_log_supervisor_failure)
        _feed_started = True
        logger.info(
            "[WSRunner] market feed supervisor started: source=%s type=%s symbols=%s",
            source,
            data_type.value,
            syms,
        )
        return True
