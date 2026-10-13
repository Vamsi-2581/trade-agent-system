"""
数据订阅管理模块

支持：
- 用户订阅特定数据类型和交易所
- 订阅状态跟踪
- 订阅回调分发
- 订阅持久化（Redis）
"""

import threading
import json
from typing import Dict, List, Optional, Callable, Set
from datetime import datetime
from collections import defaultdict
import redis
from django.conf import settings


class Subscription:
    """订阅信息"""

    def __init__(
        self,
        sub_id: str,
        user_id: str,
        source: str,
        symbol: str,
        data_type: str,
        interval: Optional[str] = None,
        market_type: str = "spot",
        callback: Optional[Callable] = None,
        created_at: Optional[datetime] = None,
    ):
        self.sub_id = sub_id
        self.user_id = user_id
        self.source = source
        self.symbol = symbol
        self.data_type = data_type
        self.interval = interval
        self.market_type = market_type
        self.callback = callback
        self.created_at = created_at or datetime.now()
        self.active = True
        self.last_update_time: Optional[datetime] = None
        self.update_count = 0


class DataSubscriptionManager:
    """
    数据订阅管理器 - 单例模式

    功能：
    - 管理用户订阅请求
    - 跟踪订阅状态
    - 分发数据到订阅者
    - 订阅持久化到 Redis
    - 按需加载对应的数据源
    """

    _instance: Optional["DataSubscriptionManager"] = None
    _lock = threading.Lock()

    # Redis key 前缀
    REDIS_KEY_PREFIX = "datasource:subs:"
    REDIS_USER_KEY_PREFIX = "datasource:user_subs:"

    def __new__(cls) -> "DataSubscriptionManager":
        """单例模式"""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        """初始化订阅管理器"""
        # 订阅存储：{sub_id: Subscription}
        self._subscriptions: Dict[str, Subscription] = {}

        # 用户订阅索引：{user_id: Set[sub_id]}
        self._user_subs: Dict[str, Set[str]] = defaultdict(set)

        # 数据源订阅索引：{source: {symbol: {data_type: Set[sub_id]}}}
        self._source_subs: Dict[str, Dict[str, Dict[str, Set[str]]]] = defaultdict(
            lambda: defaultdict(lambda: defaultdict(set))
        )

        # 锁
        # RLock（而非 Lock）：unsubscribe_user() 持锁后调用 unsubscribe()，
        # 后者会再次加同一把锁；非可重入锁会导致自死锁（真实复现，栈见 262 测试）。
        self._lock = threading.RLock()

        # Redis 连接
        self._redis: Optional[redis.Redis] = None
        self._init_redis()

    def _init_redis(self) -> None:
        """初始化 Redis 连接"""
        try:
            redis_url = settings.REDIS_URL
            # 加 connect/socket 超时：Redis 不可达时快速失败（由调用方 try/except
            # 捕获打印），避免永久阻塞挂起测试/任务（尤其测试环境连不上 dev Redis）。
            self._redis = redis.from_url(
                redis_url,
                decode_responses=True,
                socket_connect_timeout=2,
                socket_timeout=2,
            )
        except Exception as e:
            print(f"Redis connection failed: {e}")
            self._redis = None

    # ==================== 订阅管理 ====================

    def subscribe(
        self,
        user_id: str,
        source: str,
        symbol: str,
        data_type: str,
        interval: Optional[str] = None,
        market_type: str = "spot",
        callback: Optional[Callable] = None,
    ) -> str:
        """
        创建订阅

        Args:
            user_id: 用户ID
            source: 数据源名称
            symbol: 交易对/股票代码
            data_type: 数据类型
            interval: K线周期
            market_type: 市场类型
            callback: 数据回调函数

        Returns:
            订阅ID
        """
        # 生成订阅ID
        sub_id = f"{user_id}:{source}:{symbol}:{data_type}:{interval or 'none'}:{market_type}"

        with self._lock:
            # 检查是否已存在
            if sub_id in self._subscriptions:
                # 更新回调
                if callback:
                    self._subscriptions[sub_id].callback = callback
                return sub_id

            # 创建订阅
            subscription = Subscription(
                sub_id=sub_id,
                user_id=user_id,
                source=source,
                symbol=symbol,
                data_type=data_type,
                interval=interval,
                market_type=market_type,
                callback=callback,
            )

            # 存储
            self._subscriptions[sub_id] = subscription
            self._user_subs[user_id].add(sub_id)
            self._source_subs[source][symbol][data_type].add(sub_id)

            # 持久化到 Redis
            self._persist_subscription(subscription)

            # 触发数据源加载（按需加载）
            self._trigger_source_load(source, symbol, data_type, interval)

        return sub_id

    def unsubscribe(self, sub_id: str) -> bool:
        """
        取消订阅

        Args:
            sub_id: 订阅ID

        Returns:
            是否成功取消
        """
        with self._lock:
            if sub_id not in self._subscriptions:
                return False

            sub = self._subscriptions[sub_id]

            # 移除索引
            self._user_subs[sub.user_id].discard(sub_id)
            self._source_subs[sub.source][sub.symbol][sub.data_type].discard(sub_id)

            # 移除订阅
            sub.active = False
            del self._subscriptions[sub_id]

            # 从 Redis 删除
            self._remove_subscription(sub_id)

            # 检查是否需要停止数据源
            self._check_source_stop(sub.source, sub.symbol, sub.data_type)

        return True

    def unsubscribe_user(self, user_id: str) -> int:
        """
        取消用户的所有订阅

        Args:
            user_id: 用户ID

        Returns:
            取消的订阅数量
        """
        with self._lock:
            sub_ids = list(self._user_subs.get(user_id, set()))
            count = 0

            for sub_id in sub_ids:
                if self.unsubscribe(sub_id):
                    count += 1

            # 清理用户索引
            self._user_subs.pop(user_id, None)

        return count

    def get_subscription(self, sub_id: str) -> Optional[Subscription]:
        """获取订阅信息"""
        return self._subscriptions.get(sub_id)

    def get_user_subscriptions(self, user_id: str) -> List[Subscription]:
        """获取用户的所有订阅"""
        sub_ids = self._user_subs.get(user_id, set())
        return [
            self._subscriptions[sid] for sid in sub_ids if sid in self._subscriptions
        ]

    def get_source_subscriptions(
        self, source: str, symbol: Optional[str] = None, data_type: Optional[str] = None
    ) -> List[Subscription]:
        """
        获取数据源的订阅

        Args:
            source: 数据源名称
            symbol: 交易对（可选）
            data_type: 数据类型（可选）

        Returns:
            订阅列表
        """
        subs = []

        if source not in self._source_subs:
            return subs

        source_data = self._source_subs[source]

        if symbol is None:
            # 获取所有 symbol
            for sym_data in source_data.values():
                if data_type is None:
                    for dt_subs in sym_data.values():
                        for sub_id in dt_subs:
                            if sub_id in self._subscriptions:
                                subs.append(self._subscriptions[sub_id])
                else:
                    dt_subs = sym_data.get(data_type, set())
                    for sub_id in dt_subs:
                        if sub_id in self._subscriptions:
                            subs.append(self._subscriptions[sub_id])
        else:
            if symbol not in source_data:
                return subs

            sym_data = source_data[symbol]
            if data_type is None:
                for dt_subs in sym_data.values():
                    for sub_id in dt_subs:
                        if sub_id in self._subscriptions:
                            subs.append(self._subscriptions[sub_id])
            else:
                dt_subs = sym_data.get(data_type, set())
                for sub_id in dt_subs:
                    if sub_id in self._subscriptions:
                        subs.append(self._subscriptions[sub_id])

        return subs

    def get_all_subscriptions(self) -> List[Subscription]:
        """获取所有活跃订阅"""
        return list(self._subscriptions.values())

    # ==================== 数据分发 ====================

    def dispatch_data(
        self, source: str, symbol: str, data_type: str, data: Dict
    ) -> int:
        """
        分发数据到订阅者

        Args:
            source: 数据源
            symbol: 交易对
            data_type: 数据类型
            data: 数据内容

        Returns:
            分发的订阅数量
        """
        count = 0

        with self._lock:
            # 获取相关订阅
            subs = self.get_source_subscriptions(source, symbol, data_type)

            for sub in subs:
                if not sub.active or sub.callback is None:
                    continue

                # 调用回调
                try:
                    sub.callback(data)
                    sub.last_update_time = datetime.now()
                    sub.update_count += 1
                    count += 1
                except Exception as e:
                    print(f"Callback error for sub {sub.sub_id}: {e}")

        return count

    # ==================== 持久化 ====================

    def _persist_subscription(self, sub: Subscription) -> None:
        """持久化订阅到 Redis"""
        if self._redis is None:
            return

        try:
            # 存储订阅信息
            key = f"{self.REDIS_KEY_PREFIX}{sub.sub_id}"
            data = {
                "user_id": sub.user_id,
                "source": sub.source,
                "symbol": sub.symbol,
                "data_type": sub.data_type,
                "interval": sub.interval,
                "market_type": sub.market_type,
                "created_at": sub.created_at.isoformat(),
                "active": sub.active,
            }
            self._redis.set(key, json.dumps(data))

            # 用户订阅索引
            user_key = f"{self.REDIS_USER_KEY_PREFIX}{sub.user_id}"
            self._redis.sadd(user_key, sub.sub_id)

        except Exception as e:
            print(f"Redis persist error: {e}")

    def _remove_subscription(self, sub_id: str) -> None:
        """从 Redis 删除订阅"""
        if self._redis is None:
            return

        try:
            # 删除订阅信息
            key = f"{self.REDIS_KEY_PREFIX}{sub_id}"
            self._redis.delete(key)

            # 从用户索引移除（需要解析 user_id）
            parts = sub_id.split(":")
            if parts:
                user_id = parts[0]
                user_key = f"{self.REDIS_USER_KEY_PREFIX}{user_id}"
                self._redis.srem(user_key, sub_id)

        except Exception as e:
            print(f"Redis remove error: {e}")

    def load_from_redis(self) -> int:
        """
        从 Redis 加载订阅

        Returns:
            加载的订阅数量
        """
        if self._redis is None:
            return 0

        try:
            # 获取所有订阅键
            pattern = f"{self.REDIS_KEY_PREFIX}*"
            keys = self._redis.keys(pattern)

            count = 0
            for key in keys:
                try:
                    data_str = self._redis.get(key)
                    if data_str:
                        data = json.loads(data_str)

                        # 创建订阅（不加载 callback）
                        sub_id = key.replace(self.REDIS_KEY_PREFIX, "")
                        subscription = Subscription(
                            sub_id=sub_id,
                            user_id=data["user_id"],
                            source=data["source"],
                            symbol=data["symbol"],
                            data_type=data["data_type"],
                            interval=data.get("interval"),
                            market_type=data.get("market_type", "spot"),
                            callback=None,
                            created_at=datetime.fromisoformat(data["created_at"]),
                        )
                        subscription.active = data.get("active", True)

                        # 存储
                        with self._lock:
                            self._subscriptions[sub_id] = subscription
                            self._user_subs[subscription.user_id].add(sub_id)
                            self._source_subs[subscription.source][subscription.symbol][
                                subscription.data_type
                            ].add(sub_id)

                        count += 1

                except Exception as e:
                    print(f"Load subscription error for {key}: {e}")

            return count

        except Exception as e:
            print(f"Load from Redis error: {e}")
            return 0

    # ==================== 数据源控制 ====================

    def _resolve_market_types(self, source: str):
        """
        从 DataSourceConfig 中解析用户配置的市场类型

        Args:
            source: 数据源名称

        Returns:
            MarketType 列表，或 None（表示使用默认全部）
        """
        try:
            from .models import DataSourceConfig
            from .base import MarketType

            config = DataSourceConfig.objects.filter(
                name=source, is_active=True
            ).first()
            if config and config.market_types:
                return [MarketType(mt) for mt in config.market_types]
        except Exception:
            pass
        return None

    def _trigger_source_load(
        self, source: str, symbol: str, data_type: str, interval: Optional[str]
    ) -> None:
        """
        触发数据源加载（按需加载）

        Args:
            source: 数据源名称
            symbol: 交易对
            data_type: 数据类型
            interval: K线周期
        """
        # 导入数据源注册中心
        from .registry import DataSourceRegistry

        # 检查数据源是否已加载
        if not DataSourceRegistry.is_loaded(source):
            try:
                # 读取用户配置的市场类型
                market_types = self._resolve_market_types(source)

                # 获取数据源实例（首次调用时加载）
                ds = DataSourceRegistry.get(source, market_types=market_types)

                # 启动 WebSocket 连接（异步）
                # 这里需要通过事件循环来启动
                import asyncio

                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(ds.connect_websocket())
                loop.close()

            except Exception as e:
                print(f"Error loading datasource '{source}': {e}")

    def _check_source_stop(self, source: str, symbol: str, data_type: str) -> None:
        """
        检查是否需要停止数据源

        当某数据源的某交易对的某数据类型没有订阅者时，取消订阅
        """
        subs = self.get_source_subscriptions(source, symbol, data_type)
        if not subs:
            # 没有订阅者，取消数据源订阅
            from .registry import DataSourceRegistry

            if DataSourceRegistry.is_loaded(source):
                try:
                    ds = DataSourceRegistry.get(source)

                    # 导入数据类型枚举
                    from .base import DataType, KlineInterval

                    dt = DataType(data_type)
                    interval = None

                    # 如果有 K 线订阅，获取周期
                    for sub in self.get_source_subscriptions(source, symbol):
                        if sub.interval:
                            interval = KlineInterval(sub.interval)

                    # 取消订阅
                    import asyncio

                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    loop.run_until_complete(ds.unsubscribe(symbol, dt, interval))
                    loop.close()

                except Exception as e:
                    print(f"Error unsubscribing from datasource '{source}': {e}")

    # ==================== 统计 ====================

    def get_stats(self) -> Dict:
        """获取订阅统计"""
        return {
            "total_subscriptions": len(self._subscriptions),
            "users_count": len(self._user_subs),
            "sources_count": len(self._source_subs),
            "subscriptions_per_source": {
                source: sum(
                    sum(len(dt_subs) for dt_subs in sym_data.values())
                    for sym_data in source_data.values()
                )
                for source, source_data in self._source_subs.items()
            },
        }

    def get_subscription_count(
        self, user_id: Optional[str] = None, source: Optional[str] = None
    ) -> int:
        """获取订阅数量"""
        if user_id:
            return len(self._user_subs.get(user_id, set()))
        if source:
            return sum(
                sum(len(dt_subs) for dt_subs in sym_data.values())
                for sym_data in self._source_subs.get(source, {}).values()
            )
        return len(self._subscriptions)

    def __repr__(self) -> str:
        return f"<DataSubscriptionManager: {len(self._subscriptions)} subscriptions>"


# 全局便捷函数
def get_subscription_manager() -> DataSubscriptionManager:
    """获取订阅管理器实例"""
    return DataSubscriptionManager()
