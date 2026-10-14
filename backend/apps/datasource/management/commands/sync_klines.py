"""K 线回填 / 增量同步管理命令。

    # 全量回填（默认 365 天，30 天/片）
    python manage.py sync_klines --backfill
    python manage.py sync_klines --backfill --days 30 --symbol BTC/USDT:USDT --timeframe 1h

    # 增量同步一次（等价于 beat 的 sync-klines 立即跑一轮）
    python manage.py sync_klines

回填按窗口分片，**每片拉完立即落库**：中断后重跑是幂等的（唯一键去重），
已完成的片不会白拉。分页拉取走共享的 ``fetch_ohlcv_paged``。
"""

import asyncio
from datetime import datetime, timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.core.db_utils import db_async
from apps.datasource.exchange import build_exchange, fetch_ohlcv_paged
from apps.datasource.models import KlineRecord
from apps.datasource.services import upsert_klines

DEFAULT_WINDOW_DAYS = 30
DEFAULT_PAGE_LIMIT = 1000


class Command(BaseCommand):
    help = "回填 / 增量同步历史 K 线到 KlineRecord"

    def add_arguments(self, parser):
        parser.add_argument(
            "--backfill",
            action="store_true",
            help="按 --days 分页全量回填（不传则只做一次增量同步）",
        )
        parser.add_argument(
            "--days",
            type=int,
            default=None,
            help="回填天数（默认 settings.KLINE_BACKFILL_DAYS）",
        )
        parser.add_argument(
            "--symbol", action="append", default=None, help="限定标的，可重复"
        )
        parser.add_argument(
            "--timeframe", action="append", default=None, help="限定周期，可重复"
        )
        parser.add_argument(
            "--window-days",
            type=int,
            default=DEFAULT_WINDOW_DAYS,
            help=f"分片窗口天数（默认 {DEFAULT_WINDOW_DAYS}）",
        )
        parser.add_argument(
            "--limit", type=int, default=DEFAULT_PAGE_LIMIT, help="ccxt 单页大小"
        )

    def handle(self, *args, **options):
        if not options["backfill"]:
            self._sync_once(options)
            return

        symbols = list(options["symbol"] or settings.KLINE_SYNC_SYMBOLS)
        timeframes = list(options["timeframe"] or settings.KLINE_SYNC_TIMEFRAMES)
        days = options["days"] or settings.KLINE_BACKFILL_DAYS
        window_days = options["window_days"]
        limit = options["limit"]

        now = timezone.now()
        start = now - timedelta(days=days)

        self.stdout.write(
            f"[KlineBackfill] {len(symbols)} 标的 × {len(timeframes)} 周期，"
            f"{days} 天（{start.date()} → {now.date()}），窗口 {window_days} 天/片"
        )

        summary = []
        for symbol in symbols:
            for timeframe in timeframes:
                total, new = self._backfill_pair(
                    symbol, timeframe, start, now, window_days, limit
                )
                summary.append(
                    {
                        "symbol": symbol,
                        "timeframe": timeframe,
                        "total": total,
                        "new": new,
                    }
                )

        self.stdout.write("[KlineBackfill] 汇总：")
        for item in summary:
            self.stdout.write(
                f"  {item['symbol']:<16} {item['timeframe']:<4} "
                f"total={item['total']:<8} new={item['new']}"
            )

    def _sync_once(self, options) -> None:
        """不传 --backfill：把 beat 的增量任务同步跑一轮。"""
        from apps.datasource.tasks import sync_klines as sync_klines_task

        result = sync_klines_task(
            symbols=options["symbol"] or None,
            timeframes=options["timeframe"] or None,
        )
        self.stdout.write(f"[KlineSync] {result}")

    def _backfill_pair(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        now: datetime,
        window_days: int,
        limit: int,
    ) -> tuple[int, int]:
        """回填单个 pair；返回 (拉取行数, 新增行数)。

        ``new`` 用同步前后 ``count()`` 差得到——批量 upsert 不区分 insert/update。
        """
        pair_filter = {"symbol": symbol, "timeframe": timeframe}
        before = KlineRecord.objects.filter(**pair_filter).count()
        total = asyncio.run(
            self._backfill_pair_async(symbol, timeframe, start, now, window_days, limit)
        )
        new = KlineRecord.objects.filter(**pair_filter).count() - before
        return total, new

    async def _backfill_pair_async(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        now: datetime,
        window_days: int,
        limit: int,
    ) -> int:
        """一个 pair 复用一个交易所实例，逐窗口拉取并落库。"""
        exchange = build_exchange("binance")
        delta = timedelta(days=window_days)
        window_start = start
        total = 0
        try:
            while window_start < now:
                window_end = min(window_start + delta, now)
                rows = await fetch_ohlcv_paged(
                    exchange,
                    symbol,
                    timeframe,
                    int(window_start.timestamp() * 1000),
                    int(window_end.timestamp() * 1000),
                    limit,
                )
                if rows:
                    # 落库是同步 ORM：必须经 db_async 出 async 上下文（CLAUDE.md 硬规则）
                    await db_async(upsert_klines)(
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
                    total += len(rows)
                self.stdout.write(
                    f"  {symbol} {timeframe} "
                    f"{window_start.date()} → {window_end.date()}: {len(rows)} 行"
                )
                window_start = window_end
        finally:
            await exchange.close()
        return total
