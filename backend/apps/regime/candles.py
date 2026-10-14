"""BTC 日线的拉取、校验与落库——第①段的唯一数据入口。

两条硬约束贯穿全模块：

**一、「拉取失败」必须与「确实没有新 K 线」可区分。**
仓库里既有的两条拉取路径（`BinanceDataSource.fetch_klines`、
`apps.backtest.tasks._fetch_ohlcv_sync`）都在异常时 `return []`，调用方无法分辨
「今天真的清淡」与「网络断了/被限频了/代理挂了」。日线是历史标注与每日判定的
同一份真相，在这上面制造沉默故障的代价是判定停摆而无人知道；而「判定失败/数据
缺失必须保持上一有效状态 + 告警」这条决策的前提，就是数据层能识别出失败。
所以本模块失败一律抛 `CandleFetchError`，**绝不返回空列表冒充成功**。

**二、正在形成中的那根日线不得落库。**
北京 08:00 = UTC 00:00，此刻「今天」那根日线刚开盘：high == low == open、成交量
≈ 0。把它写进去，ATR/EMA 序列的最末尾就会多出一根近乎零幅的假 K 线，判定从
当天起就偏。判定任务恰好排在那个时刻，所以这不是理论风险。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Callable, Sequence

from django.db import transaction

from apps.regime import config
from apps.regime.models import DECIMAL_QUANTUM, DailyCandle

logger = logging.getLogger(__name__)

ONE_DAY_MS = 86_400_000

# 分页游标不推进时的安全阀。page_limit=1000 时 50 页 ≈ 137 年，正常回填远够，
# 撞上它说明交易所忽略了 since 参数（游标原地打转）——那是 bug，不是数据多。
_MAX_PAGES = 50

# 一页原始 K 线（ccxt 形状：[ts_ms, open, high, low, close, volume]）的取数函数。
# 单列为可注入参数，测试不必去打桩 ccxt 的模块属性。
PageFetcher = Callable[[str, str, "int | None", int], Sequence[Sequence[float]]]


class CandleFetchError(RuntimeError):
    """拉取日线失败：网络 / 交易所 / 数据形状异常。

    **空列表不是失败**——「交易所说这段时间没有 K 线」是一个可信的结论，必须与
    失败区分开，否则降级路径永远不会触发。
    """


@dataclass(frozen=True)
class Candle:
    """一枚已收盘、已校验、已按列精度量化的日线。"""

    open_time: datetime
    date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    def as_row(self, symbol: str) -> dict:
        return {
            "symbol": symbol,
            "date": self.date,
            "open_time": self.open_time,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
        }


@dataclass(frozen=True)
class FetchResult:
    """一次拉取的纯粹产物，不含任何库状态。"""

    candles: tuple[Candle, ...]
    fetched: int  # 交易所返回的原始根数
    incomplete: int  # 尚未收盘被丢弃的根数
    out_of_range: int  # 落在 [start, end] 之外被丢弃的根数

    @property
    def accepted(self) -> int:
        return len(self.candles)


@dataclass(frozen=True)
class SyncReport:
    """一次同步的可审计结果。"""

    symbol: str
    requested_start: date | None
    requested_end: date | None
    fetched: int
    accepted: int
    incomplete: int
    out_of_range: int
    created: int
    updated: int
    unchanged: int
    first_date: date | None
    last_date: date | None
    dry_run: bool

    @property
    def wrote(self) -> int:
        return self.created + self.updated

    def summary(self) -> str:
        scope = (
            f"{self.requested_start} ~ {self.requested_end}"
            if self.requested_start and self.requested_end
            else "（无需同步）"
        )
        head = f"{self.symbol} {scope}"
        if self.accepted == 0 and self.fetched == 0 and self.wrote == 0:
            return f"{head}: 无新数据（未发起请求）"
        tail = "（dry-run，未写库）" if self.dry_run else ""
        return (
            f"{head}: 取回 {self.fetched} 根，落库区间 {self.first_date} ~ "
            f"{self.last_date}；新增 {self.created}、更新 {self.updated}、"
            f"未变 {self.unchanged}；丢弃 未收盘 {self.incomplete} / "
            f"越界 {self.out_of_range}{tail}"
        )


# --------------------------------------------------------------------------- #
# 时间口径
# --------------------------------------------------------------------------- #


def _ms_of(day: date) -> int:
    """某自然日 00:00 UTC 的毫秒时间戳。"""
    return int(
        datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp() * 1000
    )


def latest_complete_date(now: datetime) -> date:
    """此刻**已收盘**的最晚日线自然日。

    日线 D 在 (D+1) 00:00 UTC 收盘，故 D 已收盘 ⟺ D <= (now + 宽容带) - 1 天。
    与 `_is_closed()` 是同一条规则的两种写法，测试钉住两者一致。
    """
    return (now + config.CANDLES.close_tolerance).astimezone(
        timezone.utc
    ).date() - timedelta(days=1)


def _is_closed(open_time: datetime, now: datetime) -> bool:
    close_time = open_time + timedelta(days=1)
    return close_time <= now + config.CANDLES.close_tolerance


# --------------------------------------------------------------------------- #
# 取数
# --------------------------------------------------------------------------- #


def _ccxt_fetch_page(
    symbol: str, timeframe: str, since_ms: int | None, limit: int
) -> Sequence[Sequence[float]]:
    """默认取数实现：ccxt 单页拉取。失败抛 `CandleFetchError`。

    与 `_fetch_ohlcv_sync` 的差别只在**失败处理**：那边 `return []`，这边抛。
    """
    import ccxt.async_support as ccxt
    from django.conf import settings

    exchange_name = config.CANDLES.exchange

    async def _run():
        exchange_class = getattr(ccxt, exchange_name, None)
        if exchange_class is None:
            raise CandleFetchError(f"ccxt 不支持 exchange={exchange_name}")

        options = {"enableRateLimit": True}
        proxy = getattr(settings, "WEB_PROXY", "") or None
        if proxy:
            options["aiohttp_proxy"] = proxy

        ex = exchange_class(options)
        try:
            return await ex.fetch_ohlcv(symbol, timeframe, since=since_ms, limit=limit)
        finally:
            await ex.close()

    try:
        return asyncio.run(_run())
    except CandleFetchError:
        raise
    except Exception as e:  # 网络 / 限频 / 代理 / 解析
        raise CandleFetchError(
            f"{exchange_name} 日线拉取失败：{type(e).__name__}: {e}"
        ) from e


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #


def _to_decimal(value) -> Decimal:
    """float/str → 按列精度量化的 Decimal。

    量化是幂等同步的前提：不量化的话 `Decimal(str(float))` 常带 15 位有效数字，
    与库里 8 位小数的值永不相等（见 models.DECIMAL_QUANTUM）。
    """
    return Decimal(str(value)).quantize(DECIMAL_QUANTUM, rounding=ROUND_HALF_UP)


def _to_candle(raw: Sequence[float]) -> Candle:
    """一页原始 K 线 → 已校验的 Candle。形状异常抛 `CandleFetchError`。

    刻意**不做**「跳过坏行、继续处理」：日线是唯一真相，静默丢一天会留下一个
    与「那天没有数据」长得一模一样的洞。坏行是硬失败——失败会告警，洞不会。
    """
    if len(raw) < 6:
        raise CandleFetchError(f"K 线字段不足：{raw!r}")

    ts_ms = int(raw[0])
    if ts_ms % ONE_DAY_MS != 0:
        raise CandleFetchError(
            f"日线不在 UTC 零点开盘（ts={ts_ms}），与 DailyCandle.date 的口径不符：{raw!r}"
        )

    open_time = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    o, h, l, c = (_to_decimal(raw[i]) for i in (1, 2, 3, 4))  # noqa: E741
    volume = _to_decimal(raw[5])

    if h < l:
        raise CandleFetchError(f"high < low：{raw!r}")
    if h < max(o, c) or l > min(o, c):
        raise CandleFetchError(f"OHLC 不自洽（high/low 未包住 open/close）：{raw!r}")
    if volume < 0:
        raise CandleFetchError(f"成交量为负：{raw!r}")

    return Candle(
        open_time=open_time,
        date=open_time.date(),
        open=o,
        high=h,
        low=l,
        close=c,
        volume=volume,
    )


# --------------------------------------------------------------------------- #
# 拉取
# --------------------------------------------------------------------------- #


def fetch_daily_candles(
    symbol: str | None = None,
    start: date | None = None,
    end: date | None = None,
    *,
    fetch_page: PageFetcher | None = None,
    now: datetime | None = None,
) -> FetchResult:
    """分页拉取 [start, end]（闭区间，按 UTC 自然日）的日线并校验。

    游标按「上一页最后一根的开盘时刻 + 1ms」推进（与既有 `_fetch_ohlcv_sync`
    同一形状），因此跨年回填不受单次 limit 限制。
    """
    symbol = symbol or config.CANDLES.symbol
    fetch_page = fetch_page or _ccxt_fetch_page
    now = now or datetime.now(timezone.utc)
    if start is None or end is None:
        raise ValueError("fetch_daily_candles 需要显式 start/end")

    timeframe = config.CANDLES.timeframe
    limit = config.CANDLES.page_limit
    since_ms = _ms_of(start)
    end_ms = _ms_of(end) + ONE_DAY_MS - 1

    raw_pages: list[Sequence[float]] = []
    batch_since: int | None = since_ms
    for _ in range(_MAX_PAGES):
        page = fetch_page(symbol, timeframe, batch_since, limit)
        raw_pages.extend(page)
        if len(page) < limit:
            break  # 交易所已到最新（或本区间就这么多）
        nxt = int(page[-1][0]) + 1
        if nxt > end_ms:
            break  # 已越过终点
        if batch_since is not None and nxt <= batch_since:
            # 游标原地打转：交易所忽略了 since。继续循环只会无限重复同一页。
            raise CandleFetchError(
                f"分页游标未推进（since={batch_since} → {nxt}），中止以免死循环"
            )
        batch_since = nxt
    else:
        raise CandleFetchError(f"分页超过 {_MAX_PAGES} 页仍未覆盖到 {end}")

    start_dt = datetime.fromtimestamp(since_ms / 1000, tz=timezone.utc)
    end_dt = datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc)
    seen: dict[datetime, Candle] = {}
    incomplete = 0
    out_of_range = 0
    for raw in raw_pages:
        candle = _to_candle(raw)
        if not (start_dt <= candle.open_time <= end_dt):
            out_of_range += 1
            continue
        if not _is_closed(candle.open_time, now):
            incomplete += 1
            continue
        seen[candle.open_time] = candle  # 分页重叠时后取者胜（值相同，仅为去重）

    return FetchResult(
        candles=tuple(seen[k] for k in sorted(seen)),
        fetched=len(raw_pages),
        incomplete=incomplete,
        out_of_range=out_of_range,
    )


# --------------------------------------------------------------------------- #
# 落库
# --------------------------------------------------------------------------- #


def _persist(symbol: str, candles: Sequence[Candle]) -> tuple[int, int, int]:
    """幂等 upsert，返回 (新增, 更新, 未变)。

    分三类而不是无脑 upsert，是为了让「同步真的取回了新数据」与「同步什么都没做」
    在报告里可区分——后者在数据源静默退化时是唯一的早期信号。
    """
    by_date = {c.date: c for c in candles}
    existing = {
        row["date"]: (
            row["open_time"],
            row["open"],
            row["high"],
            row["low"],
            row["close"],
            row["volume"],
        )
        for row in DailyCandle.objects.filter(
            symbol=symbol, date__in=list(by_date)
        ).values("date", "open_time", "open", "high", "low", "close", "volume")
    }

    to_create: list[Candle] = []
    to_update: list[Candle] = []
    unchanged = 0
    for day in sorted(by_date):
        candle = by_date[day]
        prev = existing.get(day)
        if prev is None:
            to_create.append(candle)
        elif prev == (
            candle.open_time,
            candle.open,
            candle.high,
            candle.low,
            candle.close,
            candle.volume,
        ):
            unchanged += 1
        else:
            to_update.append(candle)

    if to_create:
        DailyCandle.objects.bulk_create(
            [DailyCandle(**c.as_row(symbol)) for c in to_create]
        )
    if to_update:
        DailyCandle.objects.bulk_create(
            [DailyCandle(**c.as_row(symbol)) for c in to_update],
            update_conflicts=True,
            unique_fields=["symbol", "date"],
            update_fields=[
                "open_time",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "updated_at",
            ],
        )

    if to_create or to_update:
        logger.info(
            "[Regime] %s 日线落库：新增 %d、更新 %d、未变 %d",
            symbol,
            len(to_create),
            len(to_update),
            unchanged,
        )
    return len(to_create), len(to_update), unchanged


def sync_daily_candles(
    symbol: str | None = None,
    start: date | None = None,
    end: date | None = None,
    *,
    fetch_page: PageFetcher | None = None,
    now: datetime | None = None,
    dry_run: bool = False,
) -> SyncReport:
    """把 [start, end] 的日线同步进库；`start` 省略时从「库里最后一根 + 1 天」续拉。

    日频增量因此天然幂等且**无需网络调用**：库里已到最新已收盘日时，直接返回空报告，
    一次请求都不发。

    `start` 在表为空时是必填——冷启动的回填窗口（以及它前面那 250 天分位预热）是
    人为决定，代码不替它挑一个魔数。缺了就报错，不静默什么都不做。
    """
    symbol = symbol or config.CANDLES.symbol
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("now 必须是 tz-aware（库内时间口径恒为 UTC）")

    if end is None:
        end = latest_complete_date(now)

    if start is None:
        last = (
            DailyCandle.objects.filter(symbol=symbol)
            .order_by("-date")
            .values_list("date", flat=True)
            .first()
        )
        if last is None:
            raise ValueError(
                f"日线表里没有 {symbol} 的任何数据，无法推断回填起点："
                "冷启动必须显式指定 start（回填窗口来自回测窗口，代码不替它选值）"
            )
        start = last + timedelta(days=1)

    if start > end:
        # 无新数据：不发起请求。这是日频运行最常见的一支。
        return SyncReport(
            symbol=symbol,
            requested_start=None,
            requested_end=None,
            fetched=0,
            accepted=0,
            incomplete=0,
            out_of_range=0,
            created=0,
            updated=0,
            unchanged=0,
            first_date=None,
            last_date=None,
            dry_run=dry_run,
        )

    result = fetch_daily_candles(
        symbol=symbol, start=start, end=end, fetch_page=fetch_page, now=now
    )

    if dry_run:
        existing_dates = set(
            DailyCandle.objects.filter(
                symbol=symbol, date__in=[c.date for c in result.candles]
            ).values_list("date", flat=True)
        )
        created = sum(1 for c in result.candles if c.date not in existing_dates)
        updated = result.accepted - created
        unchanged = 0
    else:
        with transaction.atomic():
            created, updated, unchanged = _persist(symbol, result.candles)

    return SyncReport(
        symbol=symbol,
        requested_start=start,
        requested_end=end,
        fetched=result.fetched,
        accepted=result.accepted,
        incomplete=result.incomplete,
        out_of_range=result.out_of_range,
        created=created,
        updated=updated,
        unchanged=unchanged,
        first_date=result.candles[0].date if result.candles else None,
        last_date=result.candles[-1].date if result.candles else None,
        dry_run=dry_run,
    )
