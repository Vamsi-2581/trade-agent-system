"""
内存数据存储模块

支持：
- 4小时数据过期自动删除
- LRU 缓存策略
- 按交易对/数据类型分组存储
- 高效访问接口
"""

import logging
import time
import threading
from typing import Dict, List, Optional
from collections import OrderedDict
from datetime import datetime, timezone
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class DataEntry:
    """数据条目"""

    key: str  # 数据键
    data: Dict  # 数据内容
    data_type: str  # 数据类型
    symbol: str  # 交易对/股票代码
    source: str  # 数据源
    timestamp: float = field(default_factory=time.time)  # 创建时间戳
    expire_at: float = 0  # 过期时间戳


class MemoryDataStore:
    """
    内存数据存储 - 单例模式

    特性：
    - 数据 4 小时自动过期删除
    - LRU 缓存策略（按容量清理）
    - 按交易对/数据类型分组存储
    - 支持并发访问（线程安全）
    - 后台清理线程定期检查过期数据
    """

    _instance: Optional["MemoryDataStore"] = None
    _lock = threading.Lock()

    # 默认过期时间（4小时 = 14400秒）
    DEFAULT_EXPIRE_SECONDS = 14400

    # 最大容量（每个数据类型的最大条目数）
    DEFAULT_MAX_SIZE = 10000

    # 清理间隔（5分钟）
    CLEANUP_INTERVAL = 300

    def __new__(cls) -> "MemoryDataStore":
        """单例模式"""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        """初始化存储

        单例由 ``__new__`` 保证，但 Python 对每次 ``MemoryDataStore()`` 调用
        都会重跑 ``__init__``——旧实现会把 ``_store`` 重置为空，等价于每次
        ``get_data_store()`` 都清空行情数据（WS 刚写入的 ticker 被下一个
        读取者清掉，接口持续 503）。首次初始化后必须短路。
        """
        if getattr(self, "_initialized", False):
            return
        self._initialized = True

        # 主存储：{data_type: {symbol: OrderedDict}}
        # OrderedDict 用于 LRU
        self._store: Dict[str, Dict[str, OrderedDict]] = {}

        # 按时间索引：{data_type: OrderedDict}
        self._time_index: Dict[str, OrderedDict] = {}

        # 配置
        self._expire_seconds = self.DEFAULT_EXPIRE_SECONDS
        self._max_size = self.DEFAULT_MAX_SIZE

        # 统计信息
        self._stats = {
            "total_entries": 0,
            "expired_cleaned": 0,
            "lru_cleaned": 0,
            "last_cleanup_time": None,
        }

        # 锁（按数据类型）
        self._locks: Dict[str, threading.Lock] = {}

        # 清理线程
        self._cleanup_thread: Optional[threading.Thread] = None
        self._cleanup_running = False

        # 启动清理线程
        self._start_cleanup_thread()

    def _start_cleanup_thread(self) -> None:
        """启动后台清理线程"""
        if self._cleanup_thread is None or not self._cleanup_thread.is_alive():
            self._cleanup_running = True
            self._cleanup_thread = threading.Thread(
                target=self._cleanup_loop, daemon=True
            )
            self._cleanup_thread.start()

    def _cleanup_loop(self) -> None:
        """清理循环"""
        while self._cleanup_running:
            try:
                self.cleanup_expired()
                time.sleep(self.CLEANUP_INTERVAL)
            except Exception as e:
                print(f"Cleanup error: {e}")
                time.sleep(60)  # 错误后等待 1 分钟

    def _get_lock(self, data_type: str) -> threading.Lock:
        """获取数据类型锁"""
        if data_type not in self._locks:
            self._locks[data_type] = threading.Lock()
        return self._locks[data_type]

    # ==================== 数据写入 ====================

    def store(
        self,
        data_type: str,
        symbol: str,
        data: Dict,
        source: str,
        expire_seconds: Optional[float] = None,
        key: Optional[str] = None,
    ) -> bool:
        """
        存储数据

        Args:
            data_type: 数据类型（kline/trade/ticker/depth等）
            symbol: 交易对/股票代码
            data: 数据内容（已标准化）
            source: 数据源名称
            expire_seconds: 过期时间（秒），默认4小时
            key: 数据键（可选，默认自动生成）

        Returns:
            是否成功存储
        """
        logger.debug(
            f"[DataStore] store {data_type} {symbol} ts={data.get('timestamp')}"
        )
        # 生成键
        if key is None:
            # aware-UTC 兜底：与 normalize_* 产出的 aware 时间戳同口径，
            # 避免同一 data_type 下混入不带时区的键。
            timestamp = data.get("timestamp") or datetime.now(tz=timezone.utc)
            if isinstance(timestamp, datetime):
                ts_str = timestamp.strftime("%Y%m%d%H%M%S%f")
            else:
                ts_str = str(int(timestamp * 1000))
            key = f"{source}:{symbol}:{data_type}:{ts_str}"

        # 计算过期时间
        expire = expire_seconds or self._expire_seconds
        expire_at = time.time() + expire

        # 创建条目
        entry = DataEntry(
            key=key,
            data=data,
            data_type=data_type,
            symbol=symbol,
            source=source,
            expire_at=expire_at,
        )

        # 获取锁
        lock = self._get_lock(data_type)
        with lock:
            # 确保存储结构存在
            if data_type not in self._store:
                self._store[data_type] = {}
                self._time_index[data_type] = OrderedDict()

            if symbol not in self._store[data_type]:
                self._store[data_type][symbol] = OrderedDict()

            # 存储
            self._store[data_type][symbol][key] = entry
            self._time_index[data_type][key] = entry

            # 更新统计
            self._stats["total_entries"] += 1

            # LRU 检查
            symbol_store = self._store[data_type][symbol]
            if len(symbol_store) > self._max_size:
                self._lru_cleanup(data_type, symbol)

        return True

    def store_batch(
        self,
        data_type: str,
        symbol: str,
        data_list: List[Dict],
        source: str,
        expire_seconds: Optional[float] = None,
    ) -> int:
        """
        批量存储数据

        Args:
            data_type: 数据类型
            symbol: 交易对
            data_list: 数据列表
            source: 数据源
            expire_seconds: 过期时间

        Returns:
            成功存储的数量
        """
        count = 0
        for data in data_list:
            if self.store(data_type, symbol, data, source, expire_seconds):
                count += 1
        return count

    # ==================== 数据读取 ====================

    def get(self, data_type: str, symbol: str, key: str) -> Optional[Dict]:
        """
        获取单条数据

        Args:
            data_type: 数据类型
            symbol: 交易对
            key: 数据键

        Returns:
            数据内容，不存在则返回 None
        """
        lock = self._get_lock(data_type)
        with lock:
            if data_type not in self._store:
                return None
            if symbol not in self._store[data_type]:
                return None

            entry = self._store[data_type][symbol].get(key)
            if entry is None:
                return None

            # 检查过期
            if entry.expire_at < time.time():
                self._remove_entry(data_type, symbol, key)
                return None

            # LRU 更新（移到末尾）
            self._store[data_type][symbol].move_to_end(key)

            return entry.data

    def get_latest(self, data_type: str, symbol: str, limit: int = 100) -> List[Dict]:
        """
        获取最新数据

        Args:
            data_type: 数据类型
            symbol: 交易对
            limit: 数量限制

        Returns:
            数据列表（按时间倒序）
        """
        lock = self._get_lock(data_type)
        with lock:
            if data_type not in self._store:
                return []
            if symbol not in self._store[data_type]:
                return []

            symbol_store = self._store[data_type][symbol]

            # 从末尾获取（最新）
            items = list(symbol_store.items())[-limit:]

            # 过滤过期数据
            now = time.time()
            result = []
            expired_keys = []

            for key, entry in reversed(items):
                if entry.expire_at < now:
                    expired_keys.append(key)
                else:
                    result.append(entry.data)

            # 清理过期数据
            for key in expired_keys:
                self._remove_entry(data_type, symbol, key)

            return result

    def get_range(
        self,
        data_type: str,
        symbol: str,
        start_time: datetime,
        end_time: datetime,
        limit: int = 1000,
    ) -> List[Dict]:
        """
        获取时间范围内的数据

        Args:
            data_type: 数据类型
            symbol: 交易对
            start_time: 开始时间
            end_time: 结束时间
            limit: 数量限制

        Returns:
            数据列表
        """
        lock = self._get_lock(data_type)
        with lock:
            if data_type not in self._store:
                return []
            if symbol not in self._store[data_type]:
                return []

            symbol_store = self._store[data_type][symbol]
            result = []

            for entry in reversed(symbol_store.values()):
                if len(result) >= limit:
                    break

                # 检查过期
                if entry.expire_at < time.time():
                    continue

                # 检查时间范围
                data_ts = entry.data.get("timestamp")
                if data_ts:
                    if isinstance(data_ts, datetime):
                        if start_time <= data_ts <= end_time:
                            result.append(entry.data)
                    elif isinstance(data_ts, (int, float)):
                        ts_dt = datetime.fromtimestamp(data_ts, tz=timezone.utc)
                        if start_time <= ts_dt <= end_time:
                            result.append(entry.data)

            return result

    def get_symbols(self, data_type: str) -> List[str]:
        """获取某数据类型的所有交易对"""
        if data_type not in self._store:
            return []
        return list(self._store[data_type].keys())

    def get_all_data_types(self) -> List[str]:
        """获取所有数据类型"""
        return list(self._store.keys())

    # ==================== 数据删除 ====================

    def delete(self, data_type: str, symbol: str, key: str) -> bool:
        """删除单条数据"""
        lock = self._get_lock(data_type)
        with lock:
            return self._remove_entry(data_type, symbol, key)

    def delete_symbol(self, data_type: str, symbol: str) -> int:
        """删除某交易对的所有数据"""
        lock = self._get_lock(data_type)
        with lock:
            if data_type not in self._store:
                return 0
            if symbol not in self._store[data_type]:
                return 0

            count = len(self._store[data_type][symbol])

            # 从时间索引移除
            for key in self._store[data_type][symbol]:
                self._time_index[data_type].pop(key, None)

            # 删除
            del self._store[data_type][symbol]
            self._stats["total_entries"] -= count

            return count

    def delete_data_type(self, data_type: str) -> int:
        """删除某数据类型的所有数据"""
        lock = self._get_lock(data_type)
        with lock:
            if data_type not in self._store:
                return 0

            count = self._stats["total_entries"]
            for symbol in self._store[data_type]:
                count -= len(self._store[data_type][symbol])

            del self._store[data_type]
            del self._time_index[data_type]
            self._stats["total_entries"] = count

            return count

    def _remove_entry(self, data_type: str, symbol: str, key: str) -> bool:
        """移除条目（内部方法，不加锁）"""
        if data_type not in self._store:
            return False
        if symbol not in self._store[data_type]:
            return False

        if key in self._store[data_type][symbol]:
            del self._store[data_type][symbol][key]
            self._time_index[data_type].pop(key, None)
            self._stats["total_entries"] -= 1
            return True

        return False

    # ==================== 过期清理 ====================

    def cleanup_expired(self) -> int:
        """
        清理所有过期数据

        Returns:
            清理的数量
        """
        now = time.time()
        total_cleaned = 0

        for data_type in list(self._store.keys()):
            lock = self._get_lock(data_type)
            with lock:
                if data_type not in self._time_index:
                    continue

                expired_keys = []
                for key, entry in self._time_index[data_type].items():
                    if entry.expire_at < now:
                        expired_keys.append(key)

                for key in expired_keys:
                    entry = self._time_index[data_type].get(key)
                    if entry:
                        self._remove_entry(data_type, entry.symbol, key)
                        total_cleaned += 1

        if total_cleaned > 0:
            self._stats["expired_cleaned"] += total_cleaned
            self._stats["last_cleanup_time"] = datetime.now()

        return total_cleaned

    def _lru_cleanup(self, data_type: str, symbol: str) -> int:
        """
        LRU 清理（内部方法）

        Args:
            data_type: 数据类型
            symbol: 交易对

        Returns:
            清理的数量
        """
        # 清理 10% 的最旧数据
        target_size = int(self._max_size * 0.9)
        symbol_store = self._store[data_type][symbol]

        cleaned = 0
        while len(symbol_store) > target_size:
            # 移除最旧的（首部）
            key, entry = symbol_store.popitem(last=False)
            self._time_index[data_type].pop(key, None)
            cleaned += 1

        if cleaned > 0:
            self._stats["lru_cleaned"] += cleaned
            self._stats["total_entries"] -= cleaned

        return cleaned

    # ==================== 统计信息 ====================

    def get_stats(self) -> Dict:
        """获取统计信息"""
        stats = self._stats.copy()
        stats["data_types_count"] = len(self._store)
        stats["symbols_per_type"] = {
            dt: len(symbols) for dt, symbols in self._store.items()
        }
        return stats

    def get_count(
        self, data_type: Optional[str] = None, symbol: Optional[str] = None
    ) -> int:
        """
        获取数据数量

        Args:
            data_type: 数据类型（可选）
            symbol: 交易对（可选）

        Returns:
            数据数量
        """
        if data_type is None:
            return self._stats["total_entries"]

        if data_type not in self._store:
            return 0

        if symbol is None:
            return sum(len(s) for s in self._store[data_type].values())

        if symbol not in self._store[data_type]:
            return 0

        return len(self._store[data_type][symbol])

    # ==================== 配置 ====================

    def set_expire_seconds(self, seconds: float) -> None:
        """设置过期时间"""
        self._expire_seconds = seconds

    def set_max_size(self, size: int) -> None:
        """设置最大容量"""
        self._max_size = size

    # ==================== 清理 ====================

    def clear_all(self) -> int:
        """清空所有数据"""
        total = self._stats["total_entries"]
        self._store.clear()
        self._time_index.clear()
        self._stats["total_entries"] = 0
        return total

    def stop_cleanup(self) -> None:
        """停止清理线程"""
        self._cleanup_running = False

    def __repr__(self) -> str:
        return f"<MemoryDataStore: {self._stats['total_entries']} entries, {len(self._store)} types>"


# 全局便捷函数
def get_data_store() -> MemoryDataStore:
    """获取数据存储实例"""
    return MemoryDataStore()
