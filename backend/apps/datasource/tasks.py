"""数据源 Celery 任务。

K 线增量同步：三段式（同步段算增量起点 → asyncio 段拉取 → 同步段幂等落库），
结构参照 ``apps.backtest.tasks.sync_funding_rates``。

**model / service 的 import 一律放在函数体内**：``celery_app`` 在模块级 import
本模块，那时 Django app registry 可能尚未就绪，模块级 import models 会抛
``AppRegistryNotReady``。仓库既有约定（见 ``apps/backtest/tasks.py`` 的同类写法）。
"""

import asyncio
import logging
from datetime import datetime, timedelta

from celery_app import app

logger = logging.getLogger(__name__)

# 增量同步单页大小：15min 调度间隔下 4h 周期最多新增 1 根、15m 周期最多 16 根，
# 500 远够；取小值只是为了让「一页拿完」的语义显式。
_SYNC_FETCH_LIMIT = 500


def _incremental_since(symbol: str, timeframe: str, now: datetime) -> int:
    """算某个 ``(symbol, timeframe)`` 的增量起点（epoch 毫秒）。

    - 已有记录 → 最新 bar 开盘时间 + 1ms（不重复拉同一根 bar）
    - 无记录 → ``now - KLINE_BACKFILL_DAYS``

    ``KlineRecord.timestamp`` 是 aware-UTC（``USE_TZ=True``），``.timestamp()``
    因此直接得到正确的 epoch，无需额外时区处理。
    """
    from django.conf import settings

    from apps.datasource.models import KlineRecord

    latest = (
        KlineRecord.objects.filter(symbol=symbol, timeframe=timeframe)
        .order_by("-timestamp")
        .values_list("timestamp", flat=True)
        .first()
    )
    if latest is not None:
        return int(latest.timestamp() * 1000) + 1

    backfill_days = int(getattr(settings, "KLINE_BACKFILL_DAYS", 365))
    return int((now - timedelta(days=backfill_days)).timestamp() * 1000)


async def _fetch_pairs(plans: list[tuple[str, str, int]]) -> dict:
    """顺序拉取各 pair 的最新 K 线，返回 ``{(symbol, timeframe): rows}``。

    一期**顺序**拉取（≤6 个 pair，单页足够），不做并发——与
    ``_fetch_all_symbols`` 同构；后续扩到几十个标的再改 ``asyncio.gather``。

    单个 pair 失败隔离：记日志后继续下一个。否则一个坏标的会让整轮同步空转，
    而 beat 下一轮还会撞上同一个错。
    """
    from apps.datasource.exchange import build_exchange, fetch_ohlcv_paged

    fetched: dict = {}
    exchange = build_exchange("binance")
    try:
        for symbol, timeframe, since_ms in plans:
            try:
                fetched[(symbol, timeframe)] = await fetch_ohlcv_paged(
                    exchange, symbol, timeframe, since_ms, None, _SYNC_FETCH_LIMIT
                )
            except Exception as e:
                logger.error(
                    "[KlineSync] %s %s fetch failed: %s",
                    symbol,
                    timeframe,
                    e,
                    exc_info=True,
                )
                fetched[(symbol, timeframe)] = []
    finally:
        await exchange.close()
    return fetched


@app.task(
    bind=True,
    max_retries=3,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 3},
    acks_late=True,
)
def sync_klines(
    self, symbols: list[str] | None = None, timeframes: list[str] | None = None
) -> dict:
    """增量同步 K 线到 ``KlineRecord``（beat 每 ``KLINE_SYNC_INTERVAL`` 秒一轮）。

    标的与周期从 settings 读，不在任务内硬编码。DB 访问只出现在同步段
    （1 算起点、3 落库），网络段不含任何 DB 查询。

    Returns:
        ``{"pairs": int, "new_records": int}``；``new_records`` 是各 pair 的
        「同步前后 count() 差」之和（批量 upsert 不区分 insert/update，故用差值）。
    """
    from django.conf import settings
    from django.db import close_old_connections
    from django.utils import timezone

    from apps.datasource.models import KlineRecord
    from apps.datasource.services import upsert_klines

    symbols = list(symbols or settings.KLINE_SYNC_SYMBOLS)
    timeframes = list(timeframes or settings.KLINE_SYNC_TIMEFRAMES)
    close_old_connections()

    now = timezone.now()

    # 1. 同步段：算每个 pair 的增量起点（DB 查询必须在同步上下文）
    plans: list[tuple[str, str, int]] = [
        (symbol, timeframe, _incremental_since(symbol, timeframe, now))
        for symbol in symbols
        for timeframe in timeframes
    ]

    # 2. 网络段：异步拉取（不含 DB 访问）
    fetched = asyncio.run(_fetch_pairs(plans))

    # 3. 同步段：幂等落库
    total_new = 0
    for symbol, timeframe, since_ms in plans:
        rows = fetched.get((symbol, timeframe), [])
        if not rows:
            logger.info(
                "[KlineSync] %s %s: fetched=0 (since=%s)", symbol, timeframe, since_ms
            )
            continue

        pair_filter = {"symbol": symbol, "timeframe": timeframe}
        before = KlineRecord.objects.filter(**pair_filter).count()
        written = upsert_klines(
            [
                {
                    "exchange": "binance",
                    "symbol": symbol,
                    "timeframe": timeframe,
                    **row,
                }
                for row in rows
            ]
        )
        new = KlineRecord.objects.filter(**pair_filter).count() - before
        total_new += new
        logger.info(
            "[KlineSync] %s %s: fetched=%d written=%d new=%d (since=%s)",
            symbol,
            timeframe,
            len(rows),
            written,
            new,
            since_ms,
        )

    close_old_connections()
    return {"pairs": len(plans), "new_records": total_new}
