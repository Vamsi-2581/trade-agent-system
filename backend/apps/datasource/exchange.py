"""交易所访问共享工具。

集中 ccxt 异步客户端的构造（rate limit + ``WEB_PROXY`` 代理注入）与历史 K 线
分页拉取，供回测任务与数据源增量同步任务共用，避免两处各写一份分页 / 代理逻辑
（历史上 ``_fetch_all_symbols`` 就因为漏了代理注入而静默失效）。

依赖方向：``apps.backtest`` → ``apps.datasource`` 单向（消费方依赖数据提供方）；
本模块不反向 import backtest。
"""

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from django.conf import settings

logger = logging.getLogger(__name__)

# 常见报价币后缀（长后缀在前，避免 BTC 误匹配 WBTC、ETH 误匹配 WBETH）
_QUOTE_SUFFIXES = (
    "USDT",
    "USDC",
    "FDUSD",
    "BFUSD",
    "USDP",
    "TUSD",
    "BUSD",
    "DAI",
    "WBTC",
    "WBETH",
    "BTC",
    "ETH",
    "BNB",
    "EUR",
    "GBP",
)

# ccxt 周期字符串 → 秒。``M`` 为月（ccxt 约定 30 天），大小写敏感。
_TIMEFRAME_UNITS: Dict[str, int] = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
    "w": 604800,
    "M": 2592000,
}

_TIMEFRAME_RE = re.compile(r"^(\d+)([smhdwM])$")


def timeframe_to_seconds(timeframe: str) -> int:
    """把 ccxt 周期字符串转成秒数（bar 步长）。

    缺口检测、增量起点推进、未收盘 bar 过滤都要用 bar 步长，故集中一处。

    >>> timeframe_to_seconds("15m")
    900
    >>> timeframe_to_seconds("4h")
    14400
    """
    match = _TIMEFRAME_RE.match((timeframe or "").strip())
    if match is None:
        raise ValueError(f"unsupported timeframe: {timeframe!r}")
    return int(match.group(1)) * _TIMEFRAME_UNITS[match.group(2)]


def normalize_binance_symbol(symbol: str) -> str:
    """把用户/agent 输入的 symbol 归一化成 ccxt 标准格式 BASE/QUOTE。

    - "SOL/USDT"  -> "SOL/USDT"（已标准，原样返回）
    - "SOLUSDT"   -> "SOL/USDT"（交易所拼接格式，按报价币后缀拆分）
    - "SOL-USDT" / "SOL_USDT" -> "SOL/USDT"
    - "SOL"       -> "SOL/USDT"（裸基础币，默认 USDT 报价）

    注意：这是 **Binance 口径** 的归一化（裸基础币补 ``/USDT``）。对已经是
    ccxt 标准格式的输入是幂等的，因此 ``BTC/USDT:USDT`` 原样返回。
    """
    s = (symbol or "").strip().upper()
    if not s:
        return s
    # 统一分隔符：SOL-USDT / SOL_USDT -> SOL/USDT
    s = s.replace("-", "/").replace("_", "/")
    if "/" in s:
        # 已含分隔符（含上面替换产生的），去空段后原样返回
        return "/".join(p for p in s.split("/") if p)
    # 裸报价币种本身（BTC/ETH/WBTC/USDC...）视为基础币，默认 USDT 报价，
    # 避免 "WBTC" 被后缀规则误切成 "W/BTC"
    if s in _QUOTE_SUFFIXES:
        return f"{s}/USDT"
    for quote in _QUOTE_SUFFIXES:
        if s.endswith(quote) and len(s) > len(quote):
            return f"{s[: -len(quote)]}/{quote}"
    return f"{s}/USDT"


def build_exchange(exchange_id: str = "binance", **options: Any) -> Any:
    """构造 ccxt 异步交易所实例，统一注入 rate limit 与 ``WEB_PROXY`` 代理。

    代理注入必须集中在这里：漏注入时，需要代理出网的环境下 K 线能拉、其他接口
    静默失败，且调用方通常不检查失败（历史缺陷，见 spec §3.1）。

    Args:
        exchange_id: ccxt 交易所 id，如 ``"binance"``。
        **options: 追加到 ccxt 构造参数（会覆盖同名默认值）。

    Raises:
        ValueError: ``exchange_id`` 不是已安装 ccxt 支持的交易所。
    """
    import ccxt.async_support as ccxt

    exchange_class = getattr(ccxt, exchange_id.lower(), None)
    if exchange_class is None:
        raise ValueError(f"unsupported exchange: {exchange_id}")

    merged: Dict[str, Any] = {"enableRateLimit": True}
    merged.update(options)

    proxy = getattr(settings, "WEB_PROXY", "") or None
    if proxy:
        merged["aiohttp_proxy"] = proxy

    return exchange_class(merged)


async def fetch_ohlcv_paged(
    exchange: Any,
    symbol: str,
    timeframe: str,
    since_ms: Optional[int] = None,
    end_ms: Optional[int] = None,
    limit: int = 1000,
) -> List[Dict[str, Any]]:
    """分页拉取历史 OHLCV，直到到达 ``end_ms`` 或交易所不再返回数据。

    调用方负责把 ``symbol`` 归一化（见 :func:`normalize_binance_symbol`）——
    本函数对交易所保持中立，不内嵌 Binance 口径的符号规则。

    Returns:
        ``[{"timestamp": aware-UTC datetime, "open"/"high"/"low"/"close"/"volume": float}]``。
        ``timestamp`` 是 bar **开盘**时间（ccxt 语义）；用 aware datetime 而非字符串，
        是因为落库（DateTimeField）与 ISO 序列化两种消费方都要，且 epoch→datetime
        的时区口径必须集中在带 ``tz=`` 的这一处。
    """
    candles: List[Any] = []
    batch_since = since_ms

    while True:
        ohlcv = await exchange.fetch_ohlcv(
            symbol, timeframe, since=batch_since, limit=limit
        )
        if not ohlcv:
            break
        raw_count = len(ohlcv)
        # 丢弃超出目标区间的 bar（交易所可能越过 end_ms 多返回）
        if end_ms is not None:
            ohlcv = [c for c in ohlcv if c[0] <= end_ms]
        candles.extend(ohlcv)
        if raw_count < limit:
            # 交易所返回不足一页 = 已到最新，收工
            break
        if not ohlcv:
            # 本页全部超出 end_ms
            break
        # 下一批从最后一根 K 线的时间戳 +1ms 开始，避免重复拉同一根
        batch_since = ohlcv[-1][0] + 1
        if end_ms is not None and batch_since > end_ms:
            break

    result: List[Dict[str, Any]] = []
    for candle in candles:
        ts_ms = int(candle[0])
        if end_ms is not None and ts_ms > end_ms:
            continue
        result.append(
            {
                # Binance epoch 为 UTC；naive 本地时间会让 Django（TIME_ZONE=UTC）
                # 与结算点判定偏移机器时区差。必须带 tz=timezone.utc。
                "timestamp": datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc),
                "open": float(candle[1]),
                "high": float(candle[2]),
                "low": float(candle[3]),
                "close": float(candle[4]),
                "volume": float(candle[5]),
            }
        )

    return result
