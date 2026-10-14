"""
Binance Futures 交易所适配器。

使用原生 httpx 调用 Binance USDM Futures API (fapi.binance.com)。
签名算法: HMAC SHA256。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import time
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from typing import Optional
from urllib.parse import urlencode

import httpx
from anyio import BrokenResourceError, ClosedResourceError
from django.conf import settings

from .base import (
    BaseExchangeAdapter,
    OrderLookupUnavailableError,
    OrderNotFoundError,
    OrderPlacementUnknown,
    OrderRequest,
    OrderResponse,
    OrderFill,
    Position,
    SymbolRules,
)

logger = logging.getLogger(__name__)

# Futures API base URL
_BASE_URL = "https://fapi.binance.com"

# 传输层异常分类（httpx）
# 只有"请求肯定没发出去"的错误，对下单才是可安全重发的；其余（读超时/协议错误）
# 可能已被交易所受理，重发即为重复下单 → 只能按 clientOrderId 对账。
_PRE_SEND_ERRORS: tuple = (
    httpx.ConnectError,  # TCP 连接建立失败
    httpx.ConnectTimeout,  # 连接超时（尚未发出）
    httpx.PoolTimeout,  # 未从连接池拿到连接
)
# anyio 资源类错误：连接被关闭/损坏。**不是** httpx.TransportError，日志里同样只有空消息。
# 语义与传输层错误同族：请求可能没出去，也可能出去后响应通道被关掉（含糊，不可盲目重发）。
_CONNECTION_LOST_ERRORS: tuple = (ClosedResourceError, BrokenResourceError)
# 统一出口 _request 需要捕获的全部"连接层"错误
_EGRESS_ERRORS: tuple = (httpx.TransportError, *_CONNECTION_LOST_ERRORS)

_AMBIGUOUS_ORDER_ERRORS: tuple = (
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.WriteError,
    httpx.ReadError,
    httpx.RemoteProtocolError,
    *_CONNECTION_LOST_ERRORS,  # 可能已被受理：必须按 clientOrderId 对账，不能直接判失败
)
# 交易所报"单号重复"= 前一次请求其实已被受理（幂等命中）
_DUPLICATE_ORDER_MARKERS = ("-4116", "Duplicate order sent")


class BinanceAdapter(BaseExchangeAdapter):
    """
    Binance USD-M Futures 适配器。

    API 文档: https://developers.binance.com/docs/derivatives/usds-margined-futures
    """

    BASE_URL = _BASE_URL
    TIMEOUT = 10.0
    DEFAULT_LEVERAGE = 5  # 默认杠杆倍数

    def __init__(self, api_key: str, secret: str, testnet: bool = False):
        super().__init__(api_key, secret)
        if testnet:
            self.BASE_URL = "https://demo-fapi.binance.com"
        # **每个 event loop 一个 HTTP 客户端**。httpx 的连接池、socket，以及 anyio
        # StreamProtocol 的读写 Event，全都属于建立它的那个 loop；拿到别的 loop 里用就抛
        # `RuntimeError: <asyncio.locks.Event ...> is bound to a different event loop`。
        # 所以客户端只能按 loop 分开持有——共用一个槽位必然互相作废（2026-09-26 故障：
        # 两个实盘会话共享同一适配器、分属两个 loop，当日 29 次持仓同步 fail-loud）。
        # 键用 loop 对象本身而非 id(loop)：id 会被复用，新 loop 会认领别人的客户端。
        self._clients: dict[asyncio.AbstractEventLoop, httpx.AsyncClient] = {}
        # `_client`/`_client_loop` = 最近一次 connect() 建立的客户端，供外部读取与测试注入。
        # 请求解析**只在 `_client_loop is None`（无归属，即注入的替身）时才看它**：
        # 真实客户端一律走 `_clients[loop]`——共享槽位正是上面那条故障的成因。
        self._client: Optional[httpx.AsyncClient] = None
        self._client_loop: Optional[asyncio.AbstractEventLoop] = None
        self._time_offset: int = 0  # ms: local_time = server_time + offset
        self._leverage_set: set[str] = set()  # 已设置杠杆的交易对
        self._symbol_rules: Optional[dict[str, dict]] = (
            None  # exchangeInfo 精度规则缓存
        )

    async def connect(self) -> None:
        """建立**当前 loop** 专用的 HTTP 客户端（不负责处置本 loop 已有的那个）。"""
        client = await self._create_client()
        # 兼容既有读取点（`connect()` 后 `_client` 非空）：记下最近一次建立的客户端。
        self._client = client
        self._client_loop = self._running_loop()

    async def _create_client(self) -> httpx.AsyncClient:
        """为当前 loop 建一个 HTTP 客户端并同步时钟，返回它（重建路径复用）。

        **客户端只能在当前 loop 里建**，所以 loop 由 `_running_loop()` 自己取，不收参数。
        建好即入池（`_clients[loop]`）；调用方必须使用返回值——中间只要再等一次，
        同 loop 的并发重建就可能把它挤掉。
        """
        headers = {"X-MBX-APIKEY": self._api_key}
        proxy = getattr(settings, "WEB_PROXY", "") or None
        client = httpx.AsyncClient(
            base_url=self.BASE_URL,
            headers=headers,
            timeout=self.TIMEOUT,
            proxy=proxy if proxy else None,
        )
        self._clients[self._running_loop()] = client
        # Sync clock with Binance server to avoid timestamp drift errors
        await self._sync_time(client)
        return client

    async def _prune_closed_loops(self) -> None:
        """丢弃「owner loop 已关闭」的客户端（在每次解析客户端前清一遍）。

        必须清：``async_to_sync``（同步视图里调异步接口）与 ``asyncio.run`` 都是**每次
        调用新建一个 loop、用完即关**（实测：3 次 ``async_to_sync`` → 3 个 loop，全部
        `is_closed()`）。不清理的话池子会随调用次数无限增长，每个死条目还攥着一条
        keep-alive 连接——views.py 的持仓接口正是这条路径。
        只清 `is_closed()` 的：还在跑、或只是暂时停着的 loop 的客户端对它自己仍然有效，
        清掉等于替它换客户端（那正是本类要避免的事）。
        代价是一次长度为「活跃 loop 数」的遍历（线上 1-2 个），实现在读路径上是为了让
        入池实现（含被替换的实现）都无法绕开它。关闭死 loop 的客户端不会挂住：
        实测它立刻抛 `RuntimeError: Event loop is closed`，`_safe_aclose` 吃掉即可。
        """
        dead = [
            owner for owner in self._clients if owner is not None and owner.is_closed()
        ]
        for owner in dead:
            await self._safe_aclose(self._clients.pop(owner))

    async def _sync_time(self, client: httpx.AsyncClient) -> None:
        """Fetch server time and compute local clock offset.

        Stores offset so that _sign() can produce server-aligned timestamps.
        Falls back gracefully on failure (offset remains 0).

        客户端由调用方传入（本 loop 的那一个）：自己不去解析共享槽位，否则又会用上
        别的 loop 的客户端。
        """
        try:
            resp = await client.get("/fapi/v1/time")
            if resp.status_code == 200:
                server_ts = resp.json().get("serverTime", 0)
                local_ts = int(time.time() * 1000)
                self._time_offset = local_ts - server_ts
                drift_s = abs(self._time_offset) / 1000
                if drift_s > 3:
                    logger.warning(
                        f"Binance clock drift: {drift_s:.1f}s "
                        f"(offset={self._time_offset}ms), will auto-correct"
                    )
                else:
                    logger.info(f"Binance clock synced (drift={drift_s:.2f}s)")
            else:
                logger.warning(f"Binance time sync failed: HTTP {resp.status_code}")
        except Exception as e:
            logger.warning(f"Binance time sync failed: {e}")

    async def disconnect(self) -> None:
        """关闭并丢弃所有客户端，**每个都只从它自己的 loop** 关闭。

        跨 loop 调 aclose() 会重蹈本类被修的覆辙（httpcore 的池锁、anyio 的读写 Event
        也是 loop-bound）。当前 loop 的内联关闭；归属别的 loop 且那个 loop 还在跑的，
        投递到它自己的 loop 上去关；归属 loop 已停的只能丢弃引用（连接随 GC 释放）。
        """
        loop = self._running_loop()
        clients, self._clients = self._clients, {}
        legacy, self._client, self._client_loop = self._client, None, None
        # 注入的替身（无归属）不在池里，单独关——本 loop 关它没有跨 loop 问题。
        if legacy is not None and all(legacy is not c for c in clients.values()):
            await self._safe_aclose(legacy)
        for owner, client in clients.items():
            if owner is loop or owner is None:
                await self._safe_aclose(client)
            elif owner.is_running():
                fut = asyncio.run_coroutine_threadsafe(self._safe_aclose(client), owner)
                fut.add_done_callback(lambda f: f.cancelled() or f.exception())
            else:
                logger.debug(
                    "[Binance] owner loop 已停，丢弃其 HTTP 客户端（不跨 loop 关闭）"
                )

    def _sign(self, params: dict) -> str:
        """HMAC SHA256 签名，返回完整查询字符串。

        返回已签名的 query string，可直接拼接在 URL 后。
        避免 httpx 重排 params 导致签名失效。
        使用 _time_offset 校正本地时钟偏差，确保 timestamp 落在 recvWindow 内。
        """
        params = dict(params)
        params["timestamp"] = int(time.time() * 1000) - self._time_offset
        params["recvWindow"] = 5000
        query = urlencode(sorted(params.items()))
        signature = hmac.new(
            self._secret.encode(),
            query.encode(),
            hashlib.sha256,
        ).hexdigest()
        return f"{query}&signature={signature}"

    @staticmethod
    def _running_loop() -> Optional[asyncio.AbstractEventLoop]:
        """当前 running loop（无则 None）。"""
        try:
            return asyncio.get_running_loop()
        except RuntimeError:
            return None

    async def _safe_aclose(self, client: httpx.AsyncClient) -> None:
        """关闭客户端；失败只记日志（关闭不成不该掀掉调用方）。"""
        try:
            await client.aclose()
        except Exception as e:  # noqa: BLE001 - 关闭失败不影响其它客户端
            logger.debug(f"[Binance] aclose failed: {type(e).__name__}: {e!r}")

    async def _client_for_current_loop(self) -> httpx.AsyncClient:
        """返回**属于当前 loop** 的 HTTP 客户端（没有就建一个）。

        不变量：返回值一定是在当前 loop 里建出来的。调用方必须把返回值直接用在整次请求
        上，**不得**再去读共享槽位——「取客户端」与「用客户端」之间只要夹一个 await，
        别的 loop 就可能把槽位换掉，于是请求用上别人 loop 的客户端，其 keep-alive 连接
        一被复用就抛
        `RuntimeError: <asyncio.locks.Event ...> is bound to a different event loop`
        （2026-09-26 故障的确切成因：`get_positions` 的持仓同步因此 fail-loud）。
        """
        await self._prune_closed_loops()
        loop = self._running_loop()
        client = self._clients.get(loop)
        if client is not None:
            return client
        # 无 loop 归属的客户端（测试注入的替身）：不参与按 loop 重建，任何 loop 都能用。
        if self._client is not None and self._client_loop is None:
            return self._client
        if loop is None:
            raise RuntimeError("BinanceAdapter not connected. Call connect() first.")
        return await self._create_client()

    async def _reconnect(self) -> None:
        """丢弃并重建**当前 loop** 的 HTTP 客户端（并重新同步时钟）。

        长活客户端在代理/网络抖动后可能永久卡死（2026-09-20 事故：每个请求都 httpx 超时、
        连续约 10 小时 100% 失败，只能靠重启进程恢复）。重建是唯一的自愈手段。
        只动当前 loop 的那一个：别的 loop 的客户端对它们自己仍然有效，从错误的 loop 去
        关它反而会抛 loop-bound 错误（本类被修的正是这一类）。
        """
        old = self._clients.pop(self._running_loop(), None)
        if old is None and self._client_loop is None:
            # 注入的替身（无归属）：自愈同样换掉它，否则会一直用它。
            old = self._client
        if self._client is old:  # 别让 `_client` 继续指着已被关掉的客户端
            self._client = None
            self._client_loop = None
        if old is not None:
            await self._safe_aclose(old)
        await self.connect()

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        *,
        signed: bool = True,
        retry_transport: bool = True,
        **kwargs,
    ) -> httpx.Response:
        """所有请求的统一出口：传输层自愈 + 时间戳失效自愈。

        1. httpx.TransportError（连接/读写/池超时等）→ 重建客户端后重试一次。
           注意：httpx 这些异常的 message 为空，日志必须带类型名，否则只剩空消息。
           `retry_transport=False`（下单用）：只有"发送前"错误才重发，
           含糊错误（读超时/协议错误）直接抛给调用方去对账——避免重复下单。
        2. 响应为 -1021（timestamp 超出 recvWindow）→ 重新同步时钟后重试一次
           （重试会重新签名，拿到的是校正后的 timestamp。被拒=未受理，对下单也安全）。
        有界重试：每种情况最多一次，仍失败则抛出，由调用方处理。
        """

        async def attempt() -> httpx.Response:
            # 本 loop 专属的客户端，拿下之后**整次请求都用它**（不再读任何共享槽位）。
            client = await self._client_for_current_loop()
            if signed:
                target = f"{path}?{self._sign(params or {})}"
            elif params:
                # 未签名请求也要把参数带上。此前 `signed=False` 会把 params 整个丢掉，
                # 而当时唯一的调用方（exchangeInfo）恰好不带参数，所以从未显形——
                # 按 symbol 取中间价（/ticker/bookTicker）是第一个带参数的未签名调用，
                # 丢参数的后果是查到全市场盘口、再被解析成「取价失败」。
                target = f"{path}?{urlencode(sorted(params.items()))}"
            else:
                target = path
            # 用 getattr 分发到 get/post/delete（而非 client.request）：与既有调用风格、
            # 既有测试的 mock 断言保持一致，自愈改造不动其他任何地方。
            send = getattr(client, method.lower())
            return await send(target, **kwargs)

        try:
            resp = await attempt()
        except _EGRESS_ERRORS as e:
            if not retry_transport and not isinstance(e, _PRE_SEND_ERRORS):
                # 请求可能已被交易所受理（响应丢失）→ 重发会造成重复下单，交给调用方对账
                logger.warning(
                    f"Binance transport error on {method} {path} "
                    f"({type(e).__name__}: {e!r}); not resending "
                    "(request may have been accepted)"
                )
                raise
            logger.warning(
                f"Binance transport error on {method} {path} "
                f"({type(e).__name__}: {e!r}); reconnecting and retrying once"
            )
            await self._reconnect()
            resp = await attempt()

        if resp.status_code == 400 and "-1021" in (resp.text or ""):
            logger.warning(
                f"Binance -1021 on {method} {path} (timestamp outside recvWindow); "
                "re-syncing clock and retrying once"
            )
            await self._sync_time(await self._client_for_current_loop())
            resp = await attempt()

        return resp

    async def find_order_by_client_id(
        self, client_order_id: str, symbol: str
    ) -> Optional[dict]:
        """按 clientOrderId 查交易所侧真实订单（"请求发出但响应丢失"时的对账手段）。

        返回订单原始 dict；**只有交易所明确回答「订单不存在」（-2013）才返回 None**。
        查询本身失败（网络不可达、意外响应）抛 ``OrderLookupUnavailableError``——
        这两者是不同的事实，混成一个 ``None`` 会让调用方把「不知道」当成「不存在」，
        从而把一张可能真实存在的订单记成 failed（本地账面与交易所分叉）。
        """
        try:
            resp = await self._request(
                "GET",
                "/fapi/v1/order",
                {
                    "symbol": symbol.upper().replace("/", ""),
                    "origClientOrderId": client_order_id,
                },
            )
        except httpx.TransportError as e:
            raise OrderLookupUnavailableError(
                f"Binance order lookup by clientOrderId unreachable: "
                f"{type(e).__name__}: {e}"
            ) from e

        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 400:
            try:
                data = resp.json()
            except Exception:  # noqa: BLE001
                data = {}
            if data.get("code") == -2013:  # Order does not exist
                return None
        raise OrderLookupUnavailableError(
            f"Binance order lookup by clientOrderId unexpected: "
            f"{resp.status_code} - {(resp.text or '')[:200]}"
        )

    def _order_response_from_raw(self, data: dict) -> OrderResponse:
        """交易所订单 dict → OrderResponse（下单与对账共用，保持与原来一致的**原始**
        交易所状态；本地状态由 executor._map_exchange_status 统一映射）。"""
        return OrderResponse(
            exchange_order_id=str(data["orderId"]),
            status=data.get("status", ""),
            filled_qty=Decimal(data.get("executedQty", "0")),
            avg_price=Decimal(data["avgPrice"]) if data.get("avgPrice") else None,
            fee=None,
            raw=data,
            symbol=str(data.get("symbol", "") or ""),
            side=str(data.get("side", "") or "").lower(),
            # **委托量**（origQty），不是成交量（executedQty）：撤单要报的是
            # 「撤掉了多大一张单」，而 `filled_qty` 已经在上面单独给了。
            quantity=(
                Decimal(data["origQty"]) if data.get("origQty") is not None else None
            ),
        )

    async def _reconcile_order(
        self, request: OrderRequest, error: Optional[BaseException]
    ) -> Optional[OrderResponse]:
        """下单结果不明时按 clientOrderId 向交易所对账。

        返回 ``OrderResponse`` → 请求其实已被受理，采用交易所真实状态；
        返回 ``None`` → **交易所确定没有这张单**（请求未被受理），调用方按原错误处理；
        抛 ``OrderPlacementUnknown`` → **对不上账**（无幂等键 / 反查不可达 / 两处证据
        矛盾）：既证明不了已受理，也证明不了没受理。

        第三种情况原先被并进 ``None``，于是「不知道」被记成 ``failed``——若那张单
        其实活着，账面就与交易所分叉了。现在它有自己的出口（2026-09-22）。
        """
        if not request.client_order_id:
            raise OrderPlacementUnknown(
                "下单结果不明且没有 clientOrderId，无法对账（订单状态未知）"
                f"：{type(error).__name__ if error else 'duplicate-reject'}"
            )

        try:
            raw = await self.find_order_by_client_id(
                request.client_order_id, request.symbol
            )
        except OrderLookupUnavailableError as e:
            raise OrderPlacementUnknown(
                f"对账不可达 clientOrderId={request.client_order_id}"
                f"（订单状态未知，不得据此判定不存在）：{e}"
            ) from e

        if raw is None:
            logger.warning(
                f"Binance 对账未命中 clientOrderId={request.client_order_id}"
                "（交易所确定无此单，请求未被受理）"
            )
            return None

        logger.warning(
            f"Binance 对账命中 clientOrderId={request.client_order_id} → 采用交易所真实状态 "
            f"status={raw.get('status')} executedQty={raw.get('executedQty')} "
            f"avgPrice={raw.get('avgPrice')}"
        )
        return self._order_response_from_raw(raw)

    async def _ensure_leverage(self, symbol: str) -> None:
        """下单前确保已设置杠杆。每个交易对只需设置一次。"""
        if symbol in self._leverage_set:
            return
        resp = await self._request(
            "POST",
            "/fapi/v1/leverage",
            {"symbol": symbol, "leverage": self.DEFAULT_LEVERAGE},
        )
        if resp.status_code == 200:
            self._leverage_set.add(symbol)
            logger.info(f"Leverage set for {symbol}: {self.DEFAULT_LEVERAGE}x")
        else:
            logger.warning(
                f"Failed to set leverage for {symbol}: {resp.status_code} - {resp.text}"
            )

    async def _load_symbol_rules(self) -> None:
        """拉取并缓存 exchangeInfo 精度规则（stepSize / tickSize / minQty / minNotional）。"""
        try:
            resp = await self._request("GET", "/fapi/v1/exchangeInfo", signed=False)
            if resp.status_code != 200:
                logger.error(
                    f"exchangeInfo fetch failed: {resp.status_code} - {resp.text[:200]}"
                )
                raise RuntimeError(f"exchangeInfo fetch failed: {resp.status_code}")
            data = resp.json()
            rules: dict[str, dict] = {}
            for s in data.get("symbols", []):
                symbol = s.get("symbol", "")
                step_size = "1"
                tick_size = "0.01"
                min_qty = "0"
                min_notional = "0"
                for f in s.get("filters", []):
                    ftype = f.get("filterType")
                    if ftype == "LOT_SIZE":
                        step_size = f.get("stepSize", "1")
                        min_qty = f.get("minQty", "0")
                    elif ftype == "PRICE_FILTER":
                        tick_size = f.get("tickSize", "0.01")
                    elif ftype == "MIN_NOTIONAL":
                        # 减仓分片要用：每片名义价值低于它会被交易所拒（-4164）。
                        # 这里只**读**，不参与下单校验——市价单没有 price，拿不到名义
                        # 价值，在这儿加一道硬校验等于给普通下单路径加一个只在限价单
                        # 上生效的判据（CONTEXT.md:127 要的只是「补读取」）。
                        min_notional = f.get("notional", "0")
                rules[symbol] = {
                    "stepSize": Decimal(step_size),
                    "tickSize": Decimal(tick_size),
                    "minQty": Decimal(min_qty),
                    "minNotional": Decimal(min_notional),
                }
            self._symbol_rules = rules
            logger.info(f"exchangeInfo cached for {len(rules)} symbols")
        except Exception as e:
            # 不可静默吞掉：规则缺失会让数量以未归一化形态直发交易所（-1111）。
            logger.error(
                f"Failed to load exchangeInfo precision rules "
                f"({type(e).__name__}: {e!r})"
            )
            raise

    def _normalize_quantity(self, symbol: str, quantity: Decimal) -> Decimal:
        """按 LOT_SIZE stepSize 向下取整数量，避免 -1111 精度错误。"""
        if self._symbol_rules and symbol in self._symbol_rules:
            step = self._symbol_rules[symbol]["stepSize"]
            if step and step > 0:
                q = (quantity / step).to_integral_value(rounding=ROUND_DOWN) * step
                # 归一化指数位数，避免 0.01000000 之类超出精度的表示
                q = q.quantize(step)
                return q
        return quantity

    def _validate_quantity(self, symbol: str, quantity: Decimal) -> None:
        """校验归一化后的数量不为 0 且不低于 minQty，提前暴露下单参数错误。"""
        if quantity <= 0:
            raise ValueError(
                f"quantity {quantity} for {symbol} rounds to zero "
                f"below stepSize (use a larger order)"
            )
        if self._symbol_rules and symbol in self._symbol_rules:
            min_qty = self._symbol_rules[symbol]["minQty"]
            if min_qty and min_qty > 0 and quantity < min_qty:
                raise ValueError(
                    f"quantity {quantity} for {symbol} below minQty {min_qty}"
                )

    def _normalize_price(self, symbol: str, price: Decimal) -> Decimal:
        """按 PRICE_FILTER tickSize 归一化价格。"""
        if self._symbol_rules and symbol in self._symbol_rules:
            tick = self._symbol_rules[symbol]["tickSize"]
            if tick and tick > 0:
                return price.quantize(tick, rounding=ROUND_HALF_UP)
        return price

    async def place_order(self, request: OrderRequest) -> OrderResponse:
        symbol = request.symbol.upper().replace("/", "")

        # 首次下单前加载交易所精度规则（懒加载 + 缓存）
        if self._symbol_rules is None:
            await self._load_symbol_rules()

        # 精度规则不可用 → 无法保证数量/价格合法：拒绝下单。
        # 2026-09-21 实测：加载失败被静默吞掉 → _normalize_quantity 原样返回 →
        # 5332.37953400 直发 DOGEUSDT（stepSize=1）→ 交易所 -1111。
        if not self._symbol_rules or symbol not in self._symbol_rules:
            raise RuntimeError(
                f"exchangeInfo 精度规则不可用（symbol={symbol}）"
                "：拒绝下单以避免 -1111 精度错误"
            )

        # 精度归一化：数量按 stepSize 向下取整，价格按 tickSize 对齐
        quantity = self._normalize_quantity(symbol, request.quantity)
        price = (
            self._normalize_price(symbol, request.price)
            if request.price is not None
            else None
        )
        self._validate_quantity(symbol, quantity)

        # 下单前设置杠杆（Futures 必须）
        await self._ensure_leverage(symbol)

        params = {
            "symbol": symbol,
            "side": request.side.upper(),
            "type": request.order_type.upper(),
            "quantity": format(quantity, "f"),
        }
        if price is not None:
            params["price"] = format(price, "f")
            params["timeInForce"] = "GTC"
        if request.client_order_id:
            params["newClientOrderId"] = request.client_order_id
        if request.reduce_only:
            # 只减不增：减仓路径的唯一安全保证。币安在合约侧按「不得超过当前持仓」
            # 执行，超出部分直接拒单——本地记账与交易所分叉时，这是唯一能拦住
            # 「减仓反而反向开仓」的一层（CONTEXT.md:47/:48）。
            params["reduceOnly"] = "true"
        if request.stop_loss:
            params["stopPrice"] = str(request.stop_loss)
            params["workType"] = "STOP"

        try:
            resp = await self._request(
                "POST", "/fapi/v1/order", params, retry_transport=False
            )
        except _AMBIGUOUS_ORDER_ERRORS as e:
            # 请求可能已被受理但响应丢失：绝不重发，先按 clientOrderId 对账
            reconciled = await self._reconcile_order(request, e)
            if reconciled is not None:
                return reconciled
            # 能走到这里只有一种情况：对账**确定**交易所没有这张单（请求确实没出去），
            # 于是原样抛传输错误 = 确定失败。对不上账的情况 `_reconcile_order` 已经
            # 抛 OrderPlacementUnknown（未知），不会落到这一行。
            raise

        if resp.status_code >= 400:
            error_detail = resp.text or ""
            if any(marker in error_detail for marker in _DUPLICATE_ORDER_MARKERS):
                # 单号重复 = 前一次其实已受理（幂等命中），取交易所真实状态而非当作失败
                reconciled = await self._reconcile_order(request, None)
                if reconciled is not None:
                    return reconciled
                # 交易所说这个单号已存在，反查却说没有——两处证据矛盾。既证不了已受理
                # 也证不了没受理，只能如实记为「未知」，不能挑一边信。
                raise OrderPlacementUnknown(
                    f"交易所报单号重复（Binance API {resp.status_code}: "
                    f"{error_detail[:200]}），但按 clientOrderId="
                    f"{request.client_order_id or 'N/A'} 反查未命中：两处证据矛盾"
                )
            logger.error(f"Binance API error: {resp.status_code} - {error_detail}")
            # 抛出包含 Binance 错误详情的异常
            raise RuntimeError(f"Binance API {resp.status_code}: {error_detail}")

        return self._order_response_from_raw(resp.json())

    # Binance 订单状态 → 本地 Order.status 映射
    _STATUS_MAP = {
        "NEW": "submitted",
        "PARTIALLY_FILLED": "partial",
        "FILLED": "filled",
        "CANCELED": "cancelled",
        "EXPIRED": "cancelled",
        "EXPIRED_IN_MATCH": "cancelled",
        "REJECTED": "failed",
    }

    async def cancel_order(self, exchange_order_id: str, symbol: str) -> bool:
        # `symbol.upper().replace("/", "")`：与 place_order / fetch_order /
        # find_order_by_client_id 三处一致。原先只有这一处漏了 replace，传
        # 'BTC/USDT' 会被交易所当成非法符号（-1121）而这里只表现为 False，看不出
        # 原因。此前无生产调用方所以没暴露，而撤单正是 halt 的第一步。
        resp = await self._request(
            "DELETE",
            "/fapi/v1/order",
            {
                "symbol": symbol.upper().replace("/", ""),
                "orderId": exchange_order_id,
            },
        )
        return resp.status_code == 200

    async def fetch_open_orders(self, symbol: str) -> list[OrderResponse]:
        """枚举该品种当前未成交的挂单（/fapi/v1/openOrders）。

        **空列表只表示「确实一张都没有」**：任何取不到清单的情况都抛错，绝不返回空
        列表——halt 的撤单步骤若把「没查到」当成「没有挂单」，就会在真的挂着开仓单时
        报告「市场上没有需要撤的单」，而那份报告正是用户据以相信敞口已经收住的东西。
        """
        resp = await self._request(
            "GET",
            "/fapi/v1/openOrders",
            {"symbol": symbol.upper().replace("/", "")},
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"Binance openOrders {resp.status_code}: {(resp.text or '')[:300]}"
            )
        payload = resp.json()
        if not isinstance(payload, list):
            # 币安正常返回数组；回来了 dict 说明是错误体（限额/权限），不能当成「没有挂单」
            raise RuntimeError(
                f"Binance openOrders 响应不是订单数组：{str(payload)[:300]}"
            )
        return [self._order_response_from_raw(item) for item in payload]

    async def fetch_mid_price(self, symbol: str) -> Optional[Decimal]:
        """该品种的中间价 (best bid + best ask) / 2（/fapi/v1/ticker/bookTicker）。

        **取不到返回 None，绝不返回 0**（见基类 docstring）：滑点记录是「减仓成本
        失控」的唯一告警依据，把取价失败按 0 记成「滑点完美」比不记还坏。
        买卖价任一缺失、非正、或买价高于卖价（盘口交叉）都归入「取不到」。
        """
        resp = await self._request(
            "GET",
            "/fapi/v1/ticker/bookTicker",
            {"symbol": symbol.upper().replace("/", "")},
            signed=False,
        )
        if resp.status_code != 200:
            logger.warning(
                f"Binance bookTicker {symbol} 取价失败：{resp.status_code} "
                f"{(resp.text or '')[:200]}"
            )
            return None
        try:
            data = resp.json()
            bid = Decimal(str(data["bidPrice"]))
            ask = Decimal(str(data["askPrice"]))
        except (KeyError, TypeError, ValueError, ArithmeticError) as e:
            logger.warning(f"Binance bookTicker {symbol} 响应无法解析：{e!r}")
            return None
        if bid <= 0 or ask <= 0 or ask < bid:
            logger.warning(
                f"Binance bookTicker {symbol} 盘口不可用：bid={bid} ask={ask}"
            )
            return None
        return (bid + ask) / 2

    async def min_notional(self, symbol: str) -> Decimal:
        """该品种的最小名义价值（exchangeInfo 的 ``MIN_NOTIONAL.notional``）。

        规则尚未加载时先加载；**加载失败会抛**（``_load_symbol_rules`` 不为 -1111
        那类错误吞异常），调用方据此知道「这个数不知道」，而不是拿到一个 0 就当
        「交易所什么都收」。
        """
        if self._symbol_rules is None:
            await self._load_symbol_rules()
        rules = self._symbol_rules or {}
        rule = rules.get(symbol.upper().replace("/", ""))
        return rule["minNotional"] if rule else Decimal("0")

    async def symbol_rules(self, symbol: str) -> Optional[SymbolRules]:
        """该品种的申报精度（``LOT_SIZE`` 的 ``stepSize`` / ``minQty``）。

        ``min_notional`` 的同款形态：规则尚未加载时先加载，**加载失败会抛**；
        规则表里没有这个品种则返回 ``None``——「我们不知道」，不是「随便切」。

        **与 ``place_order`` 的拒绝保持一致**：``place_order`` 在同样的两种情形下
        会直接 ``RuntimeError`` 拒单（见那里的注释）。所以减仓分片拿到 ``None`` 时
        的正确反应不是「自己去猜一个精度」，而是「本次不下这一片」。

        取整口径与本适配器内部的 ``_normalize_quantity`` 逐字一致（``ROUND_DOWN``
        + ``quantize(step)``）——但它仍留在本方法之外：**这个方法只回答「规则是
        什么」，不替调用方取整**。取整是调用方要自己做、并且要留痕的事。
        """
        if self._symbol_rules is None:
            await self._load_symbol_rules()
        rules = self._symbol_rules or {}
        rule = rules.get(symbol.upper().replace("/", ""))
        if not rule:
            return None
        return SymbolRules(step_size=rule["stepSize"], min_qty=rule["minQty"])

    async def fetch_order(self, exchange_order_id: str, symbol: str) -> OrderFill:
        """查询订单当前状态与成交情况（/fapi/v1/order）。"""
        resp = await self._request(
            "GET",
            "/fapi/v1/order",
            {
                "symbol": symbol.upper().replace("/", ""),
                "orderId": str(exchange_order_id),
            },
        )

        if resp.status_code == 400:
            data = resp.json()
            if data.get("code") == -2013:  # Order does not exist
                raise OrderNotFoundError(
                    f"Order {exchange_order_id} does not exist on exchange"
                )
            raise RuntimeError(f"Binance API {resp.status_code}: {resp.text[:300]}")
        resp.raise_for_status()

        data = resp.json()
        status = self._STATUS_MAP.get(data.get("status"), "submitted")
        filled_qty = Decimal(data.get("executedQty", "0"))
        avg_price = Decimal(data["avgPrice"]) if data.get("avgPrice") else None
        if status == "submitted" and filled_qty > 0:
            status = "partial"

        return OrderFill(
            status=status,
            filled_quantity=filled_qty,
            avg_fill_price=avg_price,
            error_message=None,
        )

    async def fetch_user_trades(
        self, symbol: str, *, from_id: int = 0, limit: int = 1000
    ) -> list[dict]:
        """拉取该品种的账户成交明细（``GET /fapi/v1/userTrades``），分页到尾部。

        返回交易所原始 dict 列表。分页口径：一页返回数 **< limit** 即为尾部；否则用
        最后一笔的 ``id`` 作 ``fromId`` 续拉（币安的 ``userTrades`` 以成交 id 递增）。
        与 ``find_order_by_client_id`` 同一条纪律：**拿不到就抛**，绝不返回空列表——
        空列表的含义必须是「该品种确实没有成交」，否则盈亏会被落成 0。
        """
        native = symbol.upper().replace("/", "")
        out: list[dict] = []
        cursor = from_id
        while True:
            resp = await self._request(
                "GET",
                "/fapi/v1/userTrades",
                {"symbol": native, "fromId": cursor, "limit": limit},
            )
            resp.raise_for_status()
            page = resp.json() or []
            out.extend(page)
            if len(page) < limit:
                break
            last_id = page[-1].get("id")
            if last_id is None or int(last_id) <= cursor:
                # 游标没前进（交易所没给 id / 给了个不递增的）→ 停，避免死循环。
                break
            cursor = int(last_id)
        return out

    async def get_positions(self) -> list[Position]:
        resp = await self._request("GET", "/fapi/v2/positionRisk")
        resp.raise_for_status()

        positions = []
        for p in resp.json():
            if float(p["positionAmt"]) == 0:
                continue
            positions.append(
                Position(
                    symbol=p["symbol"],
                    side="long" if float(p["positionAmt"]) > 0 else "short",
                    quantity=abs(Decimal(p["positionAmt"])),
                    entry_price=Decimal(p["entryPrice"]),
                    unrealized_pnl=Decimal(p["unRealizedProfit"]),
                    leverage=int(p["leverage"]),
                    mark_price=(
                        Decimal(p["markPrice"]) if p.get("markPrice") else None
                    ),
                )
            )
        return positions

    async def get_balance(self) -> dict[str, Decimal]:
        """获取账户余额（USDS-M Futures）。

        /fapi/v2/balance 返回格式: [{asset, balance, walletBalance, ...}]
        Demo 环境使用 v2 端点。
        """
        resp = await self._request("GET", "/fapi/v2/balance")

        if resp.status_code != 200:
            logger.error(f"Binance balance API error: {resp.status_code} - {resp.text}")
            resp.raise_for_status()

        data = resp.json()

        # v2 返回数组格式: [{asset, balance, walletBalance, ...}]
        if isinstance(data, list):
            result = {}
            for b in data:
                asset = b.get("asset", "USDT")
                balance_val = b.get("balance") or b.get("walletBalance") or "0"
                result[asset] = Decimal(balance_val)
            return result

        # 兼容对象格式
        balance_info = data.get("balance", {})
        return {"USDT": Decimal(balance_info.get("walletBalance", "0"))}
