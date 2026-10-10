"""
交易所适配器基类和核心数据类型。

所有交易所适配器实现此接口，提供统一的交易操作抽象。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional


@dataclass
class OrderRequest:
    """下单请求"""

    exchange: str
    symbol: str
    order_type: str  # 'limit' | 'market' | 'stop'
    side: str  # 'buy' | 'sell'
    quantity: Decimal
    price: Optional[Decimal] = None
    stop_loss: Optional[Decimal] = None
    take_profit: Optional[Decimal] = None
    client_order_id: Optional[str] = None
    # 下单所依据的 ExchangeAccount.id。风控前置校验要拿它去解析**本单要用的那个**
    # 适配器（`OrderExecutor._resolve_adapter`）——按交易所名解析在同交易所有两个
    # 账户时会拿到另一个账户的余额当分母，于是仓位上限是用别人的钱算出来的。
    exchange_account_id: Optional[str] = None
    # 只减不增。**自动减仓的硬前置**：本地没有持仓表，持仓只存在于交易所侧，一旦
    # 本地记的「持仓多少」与交易所实际分叉，按本地账算出的「一半」可能大于实际持仓，
    # 于是一次「减仓」反而反向开仓、把敞口加大——熔断动作自己制造风险。交给交易所
    # 强制只减不增，是唯一能保证这件事的手段（CONTEXT.md:47/:48/:128）。
    #
    # 这里只是**标志**，翻译成本地约束是各交易所适配层的事（币安 → `reduceOnly`）。
    # 在适配层翻译接通之前，任何路径都不得开启自动减仓。
    reduce_only: bool = False
    # 这一单出自哪条实盘会话（`LiveSession.id`）。**它不是流水号，是判定输入**：
    # 停止判定的策略档钉在 `Strategy.id` 上，而一张单自己只答得出会话 id —— 「会话 →
    # 策略」的那一跳由 `RiskGuard._halt_block_reason` 完成（见 `_strategy_of_session`）。
    #
    # 少了它，策略档的停用永远不生效，而那种失效不会红任何别的东西：日报里写着「已停用」，
    # 单照常出去。所以它必须随单一路到这里，而不是等到落库时再补——落库在风控之后。
    live_session_id: Optional[str] = None


@dataclass
class OrderResponse:
    """下单响应"""

    exchange_order_id: str
    status: str  # 'NEW' | 'FILLED' | 'PARTIALLY_FILLED' | etc.
    filled_qty: Decimal
    avg_price: Optional[Decimal]
    fee: Optional[Decimal]
    raw: dict  # 交易所原始响应，便于调试
    # 方向与**委托量**（不是成交量）。下单路径用不上它们（请求里就有），``fetch_open_orders``
    # 却非有不可：撤单步骤要判「这张挂单是不是开仓意图」——判据是「方向与当前持仓相反」，
    # 没有方向这一列就判不了，只能去 ``raw`` 里翻交易所字段名，而那是把交易所的键名
    # 漏进判定逻辑（CONTEXT.md:122 的撤单判据属于机制语义，不是交易所细节）。
    #
    # 默认空值是为了不改动所有既有构造点；**空方向在 ``reduce.is_opening_order`` 里
    # 按开仓处理**（fail-closed），所以「没填」不会被读成「平仓，可以不撤」。
    symbol: str = ""
    side: str = ""
    quantity: Optional[Decimal] = None


@dataclass
class Position:
    """持仓信息"""

    symbol: str
    side: str  # 'long' | 'short'
    quantity: Decimal
    entry_price: Decimal
    unrealized_pnl: Decimal
    leverage: int
    mark_price: Optional[Decimal] = None  # 实时标记价（交易所提供，可能缺失）


@dataclass
class OrderFill:
    """订单成交状态（用于成交同步 / fill sync）"""

    status: str  # 'submitted' | 'partial' | 'filled' | 'cancelled' | 'failed'
    filled_quantity: Decimal
    avg_fill_price: Optional[Decimal]
    error_message: Optional[str] = None


@dataclass(frozen=True)
class SymbolRules:
    """一个品种的**申报精度与最小起订量**（交易所约束，只读）。

    减仓分片要回答两个问题：「这一片取整之后是多少」（``step_size``）、
    「取整之后还收不收」（``min_qty``）。两个都答不出来就不能分片——**猜一个精度**
    的后果不是下单失败，是数量被悄悄改小或改大。

    字段刻意只有这两个，**不含 ``min_notional``**：那个数已经有自己的公开口
    （``min_notional()``，带「``Decimal("0")`` = 本适配器不声明该约束」的既有语义）。
    把一个数搬进两个口，就是在两个地方回答「这个适配器声不声明最小额」，而两者
    一旦不一致，减仓分片就会按其中一个降片、按另一个不去降。

    **没有 ``min_notional`` 在这里不代表分片不用管它**：调用方仍要单独问一次
    ``min_notional()``，两处问的是同一个交易所约束的两半。
    """

    step_size: Decimal
    min_qty: Decimal


class OrderNotFoundError(Exception):
    """交易所返回订单不存在（可能被外部取消或已过期）"""


class OrderLookupUnavailableError(Exception):
    """按 ``clientOrderId`` 反查**无法得出结论**（网络不可达 / 交易所返回意外响应）。

    与「确定不存在」是两个事实，必须分开：前者说明「现在不知道」，后者说明
    「交易所确实没有这张单」。把前者当成后者会把一张可能真实存在的订单记成
    ``failed``，本地账面与交易所分叉，而分叉的账面比空白更危险。
    """


class OrderLookupUnsupportedError(OrderLookupUnavailableError):
    """该适配器不支持按 ``clientOrderId`` 反查（能力缺失，同样属于「不知道」）"""


class OrderPlacementUnknown(Exception):
    """下单请求**可能已被交易所受理**，但结果没有得出结论。

    与 ``OrderLookupUnavailableError`` 是同一种「不知道」，区别在于它发生的位置：
    请求已经发出去了，只是响应丢了（读超时/连接被关），或者交易所回了重复单号而
    我们又对不上账。

    **绝不能当成失败**：交易所侧可能正有一张活着的单。本地记成 ``failed`` 会让账面
    与实际分叉，而分叉的账面比空白更危险——用户看到「下单失败」会再下一张，于是
    敞口变成两倍。调用方必须把它落成 ``status="unknown"``（非终态），让悬挂扫描与
    成交同步继续找它。
    """


class BaseExchangeAdapter(ABC):
    """
    交易所适配器抽象基类。

    所有具体交易所实现必须实现以下异步方法。
    """

    def __init__(self, api_key: str, secret: str):
        self._api_key = api_key
        self._secret = secret

    @abstractmethod
    async def connect(self) -> None:
        """建立与交易所的连接，初始化 HTTP 客户端"""

    @abstractmethod
    async def disconnect(self) -> None:
        """关闭连接，释放资源"""

    @abstractmethod
    async def place_order(self, request: OrderRequest) -> OrderResponse:
        """发送订单到交易所

        Raises:
            OrderPlacementUnknown: 请求可能已被受理但结果不明。调用方必须落成
                ``status="unknown"``，不得当成失败。
        """

    @abstractmethod
    async def cancel_order(self, exchange_order_id: str, symbol: str) -> bool:
        """撤销指定订单"""

    @abstractmethod
    async def fetch_open_orders(self, symbol: str) -> list[OrderResponse]:
        """枚举该品种在交易所侧**当前未成交**的挂单。

        halt 生效时要一并撤掉市场上的开仓挂单，而挂单清单**只能来自交易所侧**：
        本地 `Order` 表里 `status` 非终态的行既可能已经被撤/已成交而本地还不知道，
        也漏得掉别的入口下的单。用本地账去撤单会撤错对象（CONTEXT.md:122）。

        **这里刻意是抽象方法而不是像 ``find_order_by_client_id`` 那样给个会抛的默认
        实现**：那条路是「没有它也能对账」（退化成「不知道」），而这条是保命档的必经
        之路——能力缺失只允许表现为「这次枚举失败、本次未能枚举挂单」这一条明确的
        记录，不允许表现为「这个方法不存在所以没人想到要撤单」。

        Args:
            symbol: 本地交易对符号（如 'DOGE/USDT'，由适配器归一化）

        Returns:
            未成交挂单列表；**空列表只能表示「确实一张都没有」**，
            枚举失败必须抛错——两者的区别就是「撤干净了」与「没查」的区别。
        """

    @abstractmethod
    async def fetch_order(
        self, exchange_order_id: str, symbol: str
    ) -> OrderFill:
        """查询订单当前成交状态（用于成交同步）。

        Args:
            exchange_order_id: 交易所订单号
            symbol: 本地交易对符号（如 'DOGE/USDT'，由适配器归一化）

        Returns:
            OrderFill: 订单状态、已成交数量、成交均价

        Raises:
            OrderNotFoundError: 交易所返回订单不存在
        """

    @abstractmethod
    async def get_positions(self) -> list[Position]:
        """获取当前持仓"""

    async def find_order_by_client_id(
        self, client_order_id: str, symbol: str
    ) -> Optional[dict]:
        """按 ``clientOrderId`` 反查交易所侧订单的**原始**响应。

        这是「本地已落单、但 ``exchange_order_id`` 为空」那批行的唯一消解手段：
        本地只有 ``Order.request_id``，交易所侧只有 ``newClientOrderId``，两者相
        等是这条链路的前提。

        Returns:
            命中时返回交易所原始 dict。

        Raises:
            OrderLookupUnavailableError: 无法得出结论（不可达 / 意外响应）。
            OrderLookupUnsupportedError: 适配器不具备该能力。

        **注意返回 ``None`` 只允许表示「交易所确定没有这张单」**（如币安 -2013）。
        无法区分这两种情况的实现必须抛错，不能笼统返回 ``None``。
        """
        raise OrderLookupUnsupportedError(
            f"{type(self).__name__} 不支持按 clientOrderId 反查"
        )

    async def fetch_user_trades(
        self, symbol: str, *, from_id: int = 0, limit: int = 1000
    ) -> list[dict]:
        """拉取该品种的**账户成交明细**（``userTrades``），按交易所原始字段返回。

        减仓成交的已实现盈亏以**交易所的 ``realizedPnl``** 为权威口径：空头回补的盈亏
        本地推不出来（``compute_realized_pnl`` 只做平多），只能靠它。回填命令
        ``reconcile_realized_pnl_from_exchange`` 与 ``reduce_ledger`` 的 best-effort 取数
        都走这个口。

        这里给一个会抛的默认实现（同 ``find_order_by_client_id`` 的思路）：没有它也能
        运行（退化成「取不到 → 交给回填」），但**绝不能静默返回空列表**——那会把
        「这个适配器不支持」读成「该品种没有成交」，进而把盈亏落成 0。

        Args:
            symbol: 本地交易对符号（如 'BNB/USDT'，由适配器归一化）
            from_id: 分页游标，从哪一笔成交 id 之后继续拉（0 = 从头）
            limit: 单页上限

        Returns:
            交易所原始成交 dict 列表（币安：``orderId`` / ``qty`` / ``price`` /
            ``realizedPnl`` / ``time`` / ``side`` / ``symbol``）。
        """
        raise NotImplementedError(
            f"{type(self).__name__} 不支持拉取 userTrades"
        )

    @abstractmethod
    async def get_balance(self) -> dict[str, Decimal]:
        """获取账户余额，key 为资产名称，value 为数量"""

    @abstractmethod
    async def fetch_mid_price(self, symbol: str) -> Optional[Decimal]:
        """取该品种的**中间价** ``(best bid + best ask) / 2``（只读）。

        减仓滑点的参考价就是它：滑点定义为「成交均价相对**动作发起时刻中间价**的偏离」
        （CONTEXT.md:53）。**刻意不用 ``markPrice``**——标记价是交易所的合约估值口径
        （含资金费率等的平滑），不是「此刻能在市场上成交的价格」，拿它当基线算出的
        滑点不反映真实成交代价（CONTEXT.md:129）。

        Returns:
            中间价；**取不到时必须返回 ``None``**，绝不许返回 ``Decimal("0")``。
            ``None`` 的含义是「本次不判定滑点」并留一条显式记录，而不是「滑点为 0」——
            把取不到按 0 处理，会让一次失败的取价变成一次「滑点完美」的假记录，
            而这条记录正是「减仓成本失控」的唯一告警依据。
        """

    async def min_notional(self, symbol: str) -> Decimal:
        """该品种的**最小名义价值**（交易所约束，只读）。

        减仓分片的前提：每片的名义价值不得低于交易所最小额，否则整片会被交易所拒绝
        （币安 -4164），而「分片」这件事的意义正是把一次大额减仓拆成若干张能成交的单
        （CONTEXT.md:127）。

        Returns:
            最小名义价值（计价币数量）。默认 ``Decimal("0")`` 表示**本适配器不声明该
            约束**——调用方据此不降片。注意它是「我们不知道」，不是「交易所一定接受」：
            这两者混淆的后果与 ``fetch_mid_price`` 返回 0 是同一类。适配器若声明了该
            约束，就必须返回真实值。
        """
        return Decimal("0")

    async def symbol_rules(self, symbol: str) -> Optional[SymbolRules]:
        """该品种的申报精度规则（交易所约束，只读）。

        ``min_notional`` 的同款形态，同一条纪律：**取不到时必须返回 ``None``**，
        绝不许拿一个编出来的 ``step_size`` 顶替。调用方据此知道「这个品种我不敢
        自己在本地把数量取整」，而不是拿到一个 ``Decimal("1")`` 就以为可以按整数切。

        Returns:
            ``None`` 表示**本适配器不声明该品种的精度规则**（含「规则表里没有这个
            品种」与「本适配器压根不解析规则」两种情形）。这与 ``min_notional()``
            返回 ``Decimal("0")`` 是同一句话的两种写法：**我们不知道**。

        **为什么必须是公开方法**：精度规则是交易所侧的参数，散落在各适配器内部
            （币安的 ``_symbol_rules``、``_normalize_quantity`` 都是私有的，只在
            ``place_order`` 内部用）。减仓分片要在**下单之前**知道「这一片取整完还剩
            多少」，好决定分几片、要不要降片（CONTEXT.md:127）；若在 regime 侧自己
            解析一份规则，本地就会有两个关于「最小下单量是多少」的答案，而它们分叉
            的表现是一次分片成功、一次整片被拒。
        """
        return None
