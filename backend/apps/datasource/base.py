"""
数据源基类

定义数据源的核心接口（当前仅 Binance 实现）：
- WebSocket 实时数据捕获
- REST API 历史数据获取
- 数据标准化
- 连接管理
- 错误处理
"""

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Any, Callable
from datetime import datetime, timezone
from enum import Enum
import threading

logger = logging.getLogger(__name__)


class DataType(Enum):
    """数据类型"""

    KLINE = "kline"  # K线数据
    TICKER = "ticker"  # 行情快照
    TRADE = "trade"  # 成交数据
    DEPTH = "depth"  # 深度数据（订单簿）
    FUNDING = "funding"  # 资费率
    OPEN_INTEREST = "oi"  # 持仓量


class KlineInterval(Enum):
    """K线周期"""

    M1 = "1m"
    M3 = "3m"
    M5 = "5m"
    M15 = "15m"
    M30 = "30m"
    H1 = "1h"
    H4 = "4h"
    D1 = "1d"
    W1 = "1w"
    M1_MONTH = "1M"


class MarketType(Enum):
    """市场类型"""

    SPOT = "spot"  # 现货
    FUTURES = "futures"  # 合约（永续）


class ConnectionStatus(Enum):
    """连接状态"""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    ERROR = "error"


class BaseDataSource(ABC):
    """
    数据源基类

    所有数据源必须继承此基类，并实现以下抽象方法：
    - connect_websocket(): 建立 WebSocket 连接
    - disconnect_websocket(): 断开 WebSocket 连接
    - fetch_klines(): 获取历史 K 线数据
    - fetch_trades(): 获取历史成交数据
    - subscribe(): 订阅数据
    - unsubscribe(): 取消订阅

    数据源应：
    - 使用异步编程（asyncio）处理数据流
    - 实现错误处理和重连机制
    - 数据实时性要求 <100ms
    - 提供数据清洗和标准化方法
    """

    # 数据源名称（必须由子类定义）
    name: str = ""

    # 数据源类型
    source_type: str = ""  # 'crypto'

    # 支持的数据类型
    supported_data_types: List[DataType] = []

    # 支持的市场类型
    supported_market_types: List[MarketType] = []

    # 支持的 K 线周期
    supported_intervals: List[KlineInterval] = []

    # WebSocket 端点
    ws_endpoint: str = ""

    # REST API 端点
    rest_endpoint: str = ""

    # 连接超时（秒）
    connection_timeout: float = 10.0

    # 重连配置
    max_reconnect_attempts: int = 5
    reconnect_delay: float = 5.0

    # 数据回调函数列表
    _callbacks: Dict[DataType, List[Callable]] = {}
    # 精确回调表：(data_type, symbol, interval_str) → [callable, ...]
    # 同一 (symbol, interval) 的多个上层订阅者共享同一条 WebSocket 流，
    # 数据到达时只触发该键下的回调列表，避免广播导致的重复和串流。
    _precise_callbacks: Dict[tuple, List[Callable]] = {}

    # WebSocket 状态
    _ws_status: ConnectionStatus = ConnectionStatus.DISCONNECTED
    _ws_connection: Optional[Any] = None
    _ws_thread: Optional[threading.Thread] = None

    # 活跃订阅
    _subscriptions: Dict[str, Dict] = {}  # {sub_key: sub_info}

    # 最后接收数据时间
    _last_data_time: float = 0

    # 连接时间
    _connected_at: Optional[datetime] = None

    def __init__(self):
        """初始化数据源"""
        if not self.name:
            raise ValueError("DataSource must define 'name' attribute")

        self._callbacks = {}
        self._precise_callbacks = {}  # (data_type, symbol, interval_str) → [cb, ...]
        self._subscriptions = {}
        self._ws_status = ConnectionStatus.DISCONNECTED
        self._last_data_time = 0
        self._connected_at = None

        # 异步事件循环（在独立线程中运行）
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_thread: Optional[threading.Thread] = None

        # 用户配置的市场类型（None/空 = 全部支持类型）
        self._market_types: Optional[List[MarketType]] = None

    def set_market_types(self, market_types: List[MarketType]) -> None:
        """
        设置要连接的市场类型。空/None 表示全部支持类型。

        Args:
            market_types: 市场类型列表，如 [MarketType.SPOT]
        """
        self._market_types = market_types

    def _get_active_market_types(self) -> List[MarketType]:
        """
        返回已配置的市场类型，未配置时返回所有支持的类型。

        Returns:
            活跃的市场类型列表
        """
        if self._market_types:
            return self._market_types
        return self.supported_market_types

    def ensure_market_type(self, market_type: "MarketType") -> bool:
        """
        确保给定市场类型在活跃集合中（幂等）。

        数据源实例可能被其他调用方以受限市场配置创建（如仅 futures），
        此时新增加的市场类型的订阅将无处发送 SUBSCRIBE 帧。
        若已包含（或未配置=全部）则不改动。

        Returns:
            配置是否被修改
        """
        if not self._market_types:
            return False
        if market_type not in self._market_types:
            if market_type in self.supported_market_types:
                self._market_types = list(self._market_types) + [market_type]
                logger.info(
                    "[DataSource] %s market types extended: %s",
                    self.name,
                    [mt.value for mt in self._market_types],
                )
                return True
        return False

    # ==================== WebSocket 管理 ====================

    @abstractmethod
    async def connect_websocket(self) -> bool:
        """
        建立 WebSocket 连接

        Returns:
            是否成功连接
        """
        pass

    @abstractmethod
    async def disconnect_websocket(self, preserve_subs: bool = False) -> bool:
        """
        断开 WebSocket 连接

        Args:
            preserve_subs: True 时保留已登记的订阅（重连后由
                connect_websocket 的 _resubscribe_all 重新发送）。

        Returns:
            是否成功断开
        """
        pass

    @abstractmethod
    async def _handle_websocket_message(self, message: Any) -> None:
        """
        处理 WebSocket 消息

        Args:
            message: WebSocket 消息（已解码）
        """
        pass

    def get_ws_status(self) -> ConnectionStatus:
        """获取 WebSocket 连接状态"""
        return self._ws_status

    def is_connected(self) -> bool:
        """检查是否已连接"""
        return self._ws_status == ConnectionStatus.CONNECTED

    def get_connection_time(self) -> Optional[datetime]:
        """获取连接时间"""
        return self._connected_at

    def get_last_data_time(self) -> float:
        """获取最后接收数据时间"""
        return self._last_data_time

    def get_status(self) -> Dict[str, Any]:
        """获取数据源当前运行状态"""

        now = time.time()
        last_data_age = (
            round(now - self._last_data_time, 1) if self._last_data_time > 0 else None
        )

        return {
            "name": self.name,
            "source_type": self.source_type,
            "status": self._ws_status.value,
            "connected": self.is_connected(),
            "connected_at": self._connected_at.isoformat()
            if self._connected_at
            else None,
            "subscriptions": len(self._subscriptions),
            "subscription_keys": list(self._subscriptions.keys()),
            "last_data_time": datetime.fromtimestamp(
                self._last_data_time, tz=timezone.utc
            ).isoformat()
            if self._last_data_time > 0
            else None,
            "last_data_age_seconds": last_data_age,
            "market_types": [
                mt.value for mt in (self._market_types or self.supported_market_types)
            ],
            "supported_data_types": [dt.value for dt in self.supported_data_types],
        }

    # ==================== REST API ====================

    @abstractmethod
    async def fetch_klines(
        self,
        symbol: str,
        interval: KlineInterval,
        market_type: MarketType = MarketType.SPOT,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        limit: int = 1000,
    ) -> List[Dict]:
        """
        获取历史 K 线数据

        Args:
            symbol: 交易对/股票代码
            interval: K 线周期
            market_type: 市场类型
            start_time: 开始时间
            end_time: 结束时间
            limit: 数据条数限制

        Returns:
            K 线数据列表
        """
        pass

    @abstractmethod
    async def fetch_trades(
        self,
        symbol: str,
        market_type: MarketType = MarketType.SPOT,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        limit: int = 1000,
    ) -> List[Dict]:
        """
        获取历史成交数据

        Args:
            symbol: 交易对/股票代码
            market_type: 市场类型
            start_time: 开始时间
            end_time: 结束时间
            limit: 数据条数限制

        Returns:
            成交数据列表
        """
        pass

    @abstractmethod
    async def fetch_ticker(
        self, symbol: str, market_type: MarketType = MarketType.SPOT
    ) -> Dict:
        """
        获取实时行情快照

        Args:
            symbol: 交易对/股票代码
            market_type: 市场类型

        Returns:
            行情数据
        """
        pass

    # ==================== 数据订阅 ====================

    @abstractmethod
    async def subscribe(
        self,
        symbol: str,
        data_type: DataType,
        interval: Optional[KlineInterval] = None,
        market_type: MarketType = MarketType.SPOT,
        callback: Optional[Callable] = None,
    ) -> bool:
        """
        订阅数据

        Args:
            symbol: 交易对/股票代码
            data_type: 数据类型
            interval: K 线周期（仅 K 线数据需要）
            market_type: 市场类型
            callback: 数据回调函数

        Returns:
            是否成功订阅
        """
        pass

    @abstractmethod
    async def unsubscribe(
        self,
        symbol: str,
        data_type: DataType,
        interval: Optional[KlineInterval] = None,
        market_type: MarketType = MarketType.SPOT,
        callback: Optional[Callable] = None,
    ) -> bool:
        """
        取消订阅

        Args:
            symbol: 交易对/股票代码
            data_type: 数据类型
            interval: K 线周期
            market_type: 市场类型
            callback: 可选，精确注销该回调（不传则只停 WS 流）

        Returns:
            是否成功取消
        """
        pass

    def register_callback(
        self,
        data_type: DataType,
        callback: Callable,
        symbol: Optional[str] = None,
        interval: Optional[str] = None,
    ) -> None:
        """注册数据回调函数。

        传了 symbol 走精确注册：数据按 (type, symbol, interval) 精确分发，
        不传则走旧的全局广播（向后兼容，逐步废弃）。
        """
        if symbol:
            key = (data_type, symbol, interval or "")
            cbs = self._precise_callbacks.setdefault(key, [])
            if callback not in cbs:
                cbs.append(callback)
            return

        if data_type not in self._callbacks:
            self._callbacks[data_type] = []

        if callback not in self._callbacks[data_type]:
            self._callbacks[data_type].append(callback)

    def unregister_callback(
        self,
        data_type: DataType,
        callback: Callable,
        symbol: Optional[str] = None,
        interval: Optional[str] = None,
    ) -> None:
        """移除数据回调函数。"""
        if symbol:
            key = (data_type, symbol, interval or "")
            cbs = self._precise_callbacks.get(key)
            if cbs and callback in cbs:
                cbs.remove(callback)
                if not cbs:
                    self._precise_callbacks.pop(key, None)
            return

        if data_type in self._callbacks:
            if callback in self._callbacks[data_type]:
                self._callbacks[data_type].remove(callback)

    def _trigger_callbacks(self, data_type: DataType, data: Dict) -> None:
        """全局广播触发（旧行为，向后兼容）。新代码请用 _trigger_precise_callbacks。"""
        callbacks = self._callbacks.get(data_type, [])
        logger.debug(
            f"[DataSource] triggering {len(callbacks)} callbacks for {data_type}"
        )
        for callback in callbacks:
            try:
                import asyncio
                import inspect

                if inspect.iscoroutinefunction(callback) or inspect.iscoroutinefunction(
                    getattr(callback, "__call__", None)
                ):
                    asyncio.create_task(callback(data))
                else:
                    callback(data)
            except Exception as e:
                print(f"Error in callback for {data_type}: {e}")

    def _trigger_precise_callbacks(self, data_type: DataType, data: Dict) -> None:
        """精确分发：按数据中的 symbol/interval 路由到对应回调列表。
        同一 (symbol, interval) 的多个上层订阅者共享同一条 WS 流，
        数据到达时只触发该键下的回调，避免广播导致的重复和串流。
        """
        symbol = data.get("symbol")
        interval = data.get("interval") or ""
        key = (data_type, symbol, interval)

        precise = self._precise_callbacks.get(key, [])
        # 兼容旧全局广播（未迁移的上层订阅者）
        legacy = self._callbacks.get(data_type, [])

        callbacks = list(precise) + [c for c in legacy if c not in precise]
        logger.debug(
            f"[DataSource] precise trigger key={key} "
            f"precise={len(precise)} legacy={len(legacy)} total={len(callbacks)}"
        )
        for callback in callbacks:
            try:
                import asyncio
                import inspect

                if inspect.iscoroutinefunction(callback) or inspect.iscoroutinefunction(
                    getattr(callback, "__call__", None)
                ):
                    asyncio.create_task(callback(data))
                else:
                    callback(data)
            except Exception as e:
                print(f"Error in callback for {data_type}: {e}")

    # ==================== 数据标准化 ====================

    @abstractmethod
    def normalize_kline(self, raw_data: Any) -> Dict:
        """
        标准化 K 线数据

        Args:
            raw_data: 原始 K 线数据

        Returns:
            标准化的 K 线数据:
            {
                'symbol': str,
                'interval': str,
                'open_time': datetime,
                'close_time': datetime,
                'open': float,
                'high': float,
                'low': float,
                'close': float,
                'volume': float,
                'turnover': float,
                'trades': int,
                'source': str,
                'timestamp': datetime
            }
        """
        pass

    @abstractmethod
    def normalize_trade(self, raw_data: Any) -> Dict:
        """
        标准化成交数据

        Args:
            raw_data: 原始成交数据

        Returns:
            标准化的成交数据:
            {
                'symbol': str,
                'trade_id': str,
                'price': float,
                'quantity': float,
                'side': str,  # 'buy' | 'sell'
                'timestamp': datetime,
                'source': str
            }
        """
        pass

    @abstractmethod
    def normalize_ticker(self, raw_data: Any) -> Dict:
        """
        标准化行情快照

        Args:
            raw_data: 原始行情数据

        Returns:
            标准化的行情数据:
            {
                'symbol': str,
                'last_price': float,
                'bid_price': float,
                'bid_quantity': float,
                'ask_price': float,
                'ask_quantity': float,
                'high_24h': float,
                'low_24h': float,
                'volume_24h': float,
                'turnover_24h': float,
                'change_24h': float,
                'change_pct_24h': float,
                'timestamp': datetime,
                'source': str
            }
        """
        pass

    # ==================== 辅助方法 ====================

    def get_subscriptions(self) -> Dict[str, Dict]:
        """获取所有活跃订阅"""
        return self._subscriptions.copy()

    def get_subscription_count(self) -> int:
        """获取订阅数量"""
        return len(self._subscriptions)

    def cleanup(self) -> None:
        """清理资源（卸载时调用）"""
        # 断开 WebSocket
        if self._ws_status == ConnectionStatus.CONNECTED:
            try:
                # 在异步循环中执行断开
                if self._loop and self._loop.is_running():
                    asyncio.run_coroutine_threadsafe(
                        self.disconnect_websocket(), self._loop
                    )
            except Exception as e:
                print(f"Error disconnecting WebSocket: {e}")

        # 清理回调
        self._callbacks.clear()
        self._precise_callbacks.clear()

        # 清理订阅
        self._subscriptions.clear()

    def __repr__(self) -> str:
        return f"<DataSource: {self.name}, status={self._ws_status.value}, subs={len(self._subscriptions)}>"
