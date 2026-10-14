"""
数据源应用 Models

实时行情走内存存储（``store.py``，4h 过期）；历史 K 线走数据库持久化
（``KlineRecord``），供增量同步与回测消费。
"""

from django.db import models


class DataSourceConfig(models.Model):
    """
    数据源配置（可选）

    用于存储用户自定义的数据源配置，如 API Keys 等。
    敏感信息应加密存储。
    """

    name = models.CharField(max_length=64, unique=True)
    config = models.JSONField(default=dict)
    market_types = models.JSONField(
        default=list,
        blank=True,
        help_text='["spot"], ["futures"], 或同时配置。空列表=全部。',
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "datasource_configs"
        verbose_name = "数据源配置"
        verbose_name_plural = "数据源配置"

    def __str__(self):
        return self.name


class KlineRecord(models.Model):
    """K 线（OHLCV）持久化记录。

    增量同步以 ``(exchange, symbol, timeframe, timestamp)`` 幂等：同一根 bar
    重复拉取时覆盖 OHLCV 值——这是必需的，因为交易所返回的最后一根可能是
    **未收盘**的（bar 进行中），下一轮同一 bar 收盘后要能被覆盖成最终值。

    ``exchange`` 一期恒为 ``"binance"``，但建表时就纳入唯一键：未来扩 OKX/Bybit
    只需扩同步任务的交易所维度，不必改约束（生产改唯一键是高风险 DDL）。
    """

    exchange = models.CharField(max_length=16, default="binance")
    symbol = models.CharField(max_length=32, db_index=True)
    timeframe = models.CharField(max_length=8, db_index=True)  # "15m" / "1h" / "4h"
    timestamp = models.DateTimeField(db_index=True)  # bar 开盘时间，UTC
    open = models.FloatField()
    high = models.FloatField()
    low = models.FloatField()
    close = models.FloatField()
    volume = models.FloatField()

    class Meta:
        db_table = "datasource_klines"
        unique_together = [("exchange", "symbol", "timeframe", "timestamp")]
        # 区间查询 (symbol, timeframe, timestamp BETWEEN ...) 命中同一索引
        indexes = [models.Index(fields=["symbol", "timeframe", "timestamp"])]
        ordering = ["timestamp"]
        verbose_name = "K 线记录"
        verbose_name_plural = "K 线记录"

    def __str__(self):
        return f"{self.exchange}:{self.symbol}:{self.timeframe}@{self.timestamp}"
