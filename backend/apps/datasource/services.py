"""数据源持久化服务。

历史 K 线的幂等写入：增量同步任务（``sync_klines``）与回测补拉两条写入路径共用。

放在独立模块而非 ``store.py``：``store.py`` 是实时行情的**内存**存储
（LRU + 4h 过期），与本模块的数据库持久化是两种生命周期，混在一起会让
「哪些数据会过期」变得难以判断。
"""

import logging
from typing import Any, Dict, Iterable, List

from apps.datasource.models import KlineRecord

logger = logging.getLogger(__name__)

# 幂等键：与 KlineRecord.Meta.unique_together 一致
_KLINE_UNIQUE_FIELDS: List[str] = ["exchange", "symbol", "timeframe", "timestamp"]
# 冲突时被覆盖的列：幂等键之外的全部 OHLCV
_KLINE_UPDATE_FIELDS: List[str] = ["open", "high", "low", "close", "volume"]


def upsert_klines(rows: Iterable[Dict[str, Any]], batch_size: int = 1000) -> int:
    """按 ``(exchange, symbol, timeframe, timestamp)`` 幂等写入 K 线，返回写入行数。

    用 Django 4.1+ 的**批量冲突更新**，而不是逐行 ``update_or_create``：后者是
    SELECT + UPDATE 各一次，回填 92k 行会退化成 ~18 万次查询；按 1000 行/批
    只需 ~92 次。PostgreSQL 与 SQLite(dev, 3.24+) 都支持 ``update_conflicts``。

    幂等语义对「未收盘 bar」是必需的：同一根 bar 重复拉取时覆盖 OHLCV 值，
    先落中间值、收盘后再落最终值，行内最终是收盘值。

    批量 API 不区分 insert/update，故返回值是「提交的行数」；调用方若要「新增数」，
    用同步前后 ``count()`` 差得到（实现最简，且不需要额外的存在性预查）。

    Args:
        rows: 每项为 :class:`KlineRecord` 的构造参数；``timestamp`` 须为 aware-UTC。
        batch_size: 每批行数，必须 >= 1。

    Returns:
        提交的行数（``len(rows)``）。

    Raises:
        ValueError: ``batch_size`` < 1。
    """
    if batch_size < 1:
        raise ValueError(f"batch_size must be >= 1, got {batch_size}")

    pending = list(rows)
    if not pending:
        return 0

    written = 0
    for start in range(0, len(pending), batch_size):
        batch = pending[start : start + batch_size]
        KlineRecord.objects.bulk_create(
            [KlineRecord(**row) for row in batch],
            update_conflicts=True,
            unique_fields=_KLINE_UNIQUE_FIELDS,
            update_fields=_KLINE_UPDATE_FIELDS,
        )
        written += len(batch)

    return written
