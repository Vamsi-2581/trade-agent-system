"""资金效率的**纯计算层**（spec #30 D6 / D7 / D11 / D12；子票 #33）。

输入（成交/订单行序列 + 窗口边界 + 总资金）→ 输出一组固定指标（spec D6 的六个键）：

    avg_capital_used            时间加权平均占用资金
    capital_utilization         avg_capital_used / total_capital
    idle_pct                    1 - capital_utilization
    time_in_market_pct          在仓时长 / 窗口总时长
    return_per_capital_used_pct 总盈亏 / avg_capital_used
    sample_sufficient           笔数与 SAMPLE_THRESHOLD 的比较（只标注，不动作）

消费方有两个（spec 用户故事 22：口径不会在两处分叉）——#35 的回测适配器与 #38 的会话
统计。**两边必须调用同一个 `compute_capital_efficiency`**，各自只负责把自己的行适配成
`OccupancyInterval`。

I/O 那一半（从 DB 取行、往 `metrics["capital_efficiency"]` 与 `session_stats` 里写）在后
续两个票。分开的理由与 `evaluate.py` / `walk_forward.py` 同一条：钱相关的算术要能在没有
DB 的情况下被穷举测试。

## 六条口径（读代码前先读这六条）

1. **占用是时间加权的，不是期末名义值**。`avg_capital_used = Σ(占用金额 × 该金额持续的
   时长) / 窗口总时长`。spec 用户故事 13 的原话：一只**满仓 3 天后清仓**的策略不该被
   算成「占用了一半资金」。期末名义值在那 3 天里是 0（已清仓），于是会把一个把全部本金
   压上去的策略读成「几乎没用钱」——恰好把高风险读成高效率。等权（把每段持仓当成同样
   长）也不对：同样两笔、错开时点时，「一笔 3 天 + 一笔 3 天」与「两笔重叠 3 天」的
   真实资金压力差一倍，等权会把这件事抹平。

2. **在仓时长与金额口径严格分开**。`time_in_market_pct` 数的是**时间**（是否有仓位开着，
   不管开多大），`avg_capital_used` 数的是**金额**（不管开多久）。两者都是「利用率」的
   常见读法，但答案不同：半仓开满全程 → 时间 100%、金额 50%；满仓只开半天 → 时间
   50%、金额（按全天窗）97% 上下。混成一个数会让「轻仓长持」与「重仓短打」这两种风险
   完全不同的策略看起来一样。

3. **空仓间隙是显式的，不是空隙**。没有任何仓位开着的那段时间，占用金额取 **0**，仍然
   按它的实际时长进加权。`idle_pct` 能表达「有多少比例的本金在睡大觉」全靠这一条：把
   空仓间隙当作「0 权重跳过」，`capital_utilization` 会变成「有仓位时的平均仓位大小」，
   于是「只开 1% 时间的满仓策略」与「全程半仓」都会得到同一个数。

4. **窗口是左闭右开 `[start, end)`**，`time_in_market_pct` 与加权平均共用同一个分母：
   窗口总时长。一笔持仓被裁剪到窗口内（窗口开始前已开仓、或窗口结束后才平仓），**只计
   窗口内那一截**——否则分子分母取自不同区间，`time_in_market_pct` 可能 >100%。边界瞬
   时 `t == window_end` 属于下一个窗口（右开），`t == window_start` 属于本窗口（左闭），
   与 `walk_forward.py` 的分折边界同一条约定：同一条口径在两处必须一致，否则一个跨日
   的持仓会被两个窗口各算一整段。

5. **判不了不是判通过，也绝不是 0**。本金 ≤0、窗口为空（`end <= start`）、零笔数都返回
   **整段 `None`** 并带 `reason`（`invalid_capital` / `empty_window` / `no_trades`），
   **不抛异常、不挑默认值顶替**。这与 #26 修掉的 NaN fail-open 是同一纪律：`0` 会被读成
   「完全没用钱」（于是「效率真高」），`invalid_capital` 落个 `0` 会让「分母都不成立的
   实验」看起来像一次干净的测量。调用方必须把整段 `None` 翻成「这一段没有资金效率证据」，
   绝不能只取其中一个键继续算。

6. **样本太薄时数字照算，但必须标出来**（spec 用户故事 14）。`sample_sufficient` 只**开关
   一个标注**，不影响任何数值：2 笔交易的效率指标照样能算，只是带了「样本不足」的记
   号。理由见 `session_stats.py` 的 `SAMPLE_THRESHOLD`——那个门槛本身不做任何动作，这里
   也一样。反过来（因为样本薄就把数字抹成 `None`）会把「有 2 笔、每笔都满仓」这个真实
   观察删掉，而它恰恰是判断「要不要再跑久一点」的起点。

## 回测侧与会话侧的不对称：写在这里，不糊过去

本模块要服务两个数据状况完全不同的调用方，**这个不对称必须在代码里写明**，而不是在两边
各长出一套看起来同名的口径：

| | 回测侧（#35） | 会话侧（#38） |
|---|---|---|
| 数据 | `BacktestTrade` 的 `close` 行 | `Order` 的平仓行 |
| 时间对 | 有 `entry_time` / `exit_time` | **没有**成对的开平仓时刻（见 `.p4-notes/backtest-save-path.md` §4） |
| 占用金额 | 可从成交量 × 价格推出 | 订单行有 `quantity` / `avg_fill_price`，但那是**单笔**名义值 |
| 结论 | `OccupancyInterval` 是真的持仓区间 | 只能给**点观测** |

决议（三条，缺一不可）：

* **区间 = 「仓位持有着的那段时间」**。回测适配器喂真正的 `[entry_time, exit_time)`，
  每段可带自己的 `notional`。这是 `OccupancyInterval` 的**主语义**。
* **会话适配器必须把每个平仓行的时刻当成一个点观测喂进来**（`start == end`，零时长），
  并**自带一段显式的 `notes`** 说明「这是点观测，时间权重为 0」。零时长的区间合法地
  贡献 0 到加权平均（加权平均的分子），但它**仍然计入笔数**（`sample_sufficient` 与
  `no_trades` 判的都是笔数，不是时长）。
  于是会话侧的 `avg_capital_used` 在**纯点观测**输入下会偏向 0，而
  `time_in_market_pct` 会是 0——这两个数在会话侧**不可与回测侧同读**。模块不假装它
  们可比：调用方必须自行决定是要把 `time_in_market_pct` 在会话侧标成「不适用」，还是
  补一段窗口外推的时间边界。
* **不让两边为「看起来一样」而统一**。真正该做的是让两边给同一组键名、同一套判定纪律
  （None / reason / 不抛），并把「这一侧的占用区间是怎么来的」写进各自的 `notes`，
  由消费方原样呈现。spec D6 要的是**同一套公式**，不是**同一份数字**。

`OccupancyInterval.start == end` 因此是**有含义的合法值**（点观测），不是异常；反向
（`end < start`）是构造错误，在 `__post_init__` 里直接拒绝。

## 数值纪律

* **金额全程 `Decimal`**，与 `Order.realized_pnl` / `BacktestResult.initial_capital` 的
  `DecimalField` 同一条链路，不进二进制浮点。百分比出参是 `float`（要进 JSON、要跟前端
  的既有数字并列），转换点只有一处（`_as_pct`），理由与 `slicing.adapt_trades` 一致：
  百分比本身的精度远高于任何阈值判定需要的精度。加权平均的中间结果**不转 float**——
  `Σ(占用 × 时长) / 总时长` 全在 Decimal 上做，除法在 28 位上下文里完成，远高于
  `Decimal(20,2)` 字段自身的两位小数。
* **时长为 0 的窗口判 `empty_window`**，不是除零：`end <= start` 时窗口里没有任何一瞬，
  时间加权平均的分母不存在。
* **非有限数不是结论**。`notional` / `pnl` / `total_capital` 若是 NaN 或 ±inf，整段判不了，
  不转默认值顶替。这些字段是 `Decimal`，判据是 `Decimal.is_finite()` 与 `__post_init__` 的
  显式拒绝——**不**复用 `evaluate.is_usable_metric_number`：那份判据只认 `int` / `float`
  （它自己 docstring 写明 `isinstance(value, (int, float))`），而 `Decimal` 会被它整个判成
  「不是数」，于是每一个合法本金都会被当成证据不足。这里不是「写第二份判据」——是那条
  判据的适用域本就不含金额，「强行复用」会让它变成一份错的共用。
* **笔数与时长是两个分母**。`sample_sufficient` 与 `no_trades` 判的是**笔数**
  （`len(clipped)`），`time_in_market_pct` 与加权平均用的是**秒**。一个零时长的点观测
  计 1 笔、贡献 0 秒。混用会把「2 笔、每笔都开满全程」与「2 笔、各开一分钟」读成同一个
  样本充足度。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Sequence

from apps.strategy_factory.config import CONFIG, FactoryConfig

#: `BacktestResult.metrics` 里承载资金效率结论的键。本模块只产出它的载荷，写方是 #35
#: 的适配层（与 `walk_forward.WALK_FORWARD_METRICS_KEY` 同一分工）。
CAPITAL_EFFICIENCY_METRICS_KEY = "capital_efficiency"

#: 百分比与比值的落库/比较精度（小数位）。
#:
#: 存在的理由与 `walk_forward.DECAY_PRECISION` 同一条**边界确定性**问题：
#: `float(Decimal(1)/Decimal(3))` 是 `0.3333333333333333`，而同一个「恰好三分之一」
#: 在不同数字上会落成 `0.33333333333333337`。资金效率的百分比要进 JSON、要在界面上与
#: 阈值并列显示，定精度后它是可表示的、可复现的。
#:
#: 方向安全性：round 到 6 位对**放行**方向是保守的。真实值 33.3333336 会变成 33.333334
#: （更严或不变），而真实值 33.3333334 只会变成 33.333333（更松一丝，但 1e-6 个百分点
#: 级别的差不可能把任何一个合法阈值翻过去——本系统没有以「利用率恰等于 X%」为界的闸门，
#: 这些指标只被呈现与被比较）。取 6 位而不是 2 位：`time_in_market_pct` 与
#: `return_per_capital_used_pct` 常在很短的窗口上算出很小的数（例如一笔 3 分钟的持仓
#: 落在 1 小时窗口上 = 5.0%），2 位会把 5.004% 与 5.0% 并成同一个显示值。
CAPITAL_EFFICIENCY_PRECISION = 6

#: 载荷形状的版本号。跟着口径走：任何一条口径变了，这里必须 +1，让 #35 的重算入口
#: 能据 `is_stale` 认出「老行是按旧口径算的」。
CAPITAL_EFFICIENCY_VERSION = 1


#: 算不了时整段 None 的原因码（进 `reason`，供事后归因）。
class CapitalEfficiencyReason(str, Enum):
    """判不了的原因码。

    三个码分别对应三个**不同**的补救动作，所以必须分得开：`invalid_capital` 是实验本身
    没设对（该去修本金），`empty_window` 是区间没给对（该去修窗口边界），`no_trades`
    是这一段确实没有成交（该去补时间，而不是修参数）。混成一个码会让归因变成猜。
    """

    INVALID_CAPITAL = "invalid_capital"
    EMPTY_WINDOW = "empty_window"
    NO_TRADES = "no_trades"


class OccupancyIntervalKind(str, Enum):
    """占用区间的来源类型。

    这个枚举存在的唯一理由是**回测侧与会话侧的数据不对称必须显式**（见模块 docstring
    「回测侧与会话侧的不对称」一节）：

    * `HELD` —— 一个真实的持仓区间 `[start, end)`，来自 `BacktestTrade` 的
      `entry_time`/`exit_time`。它有时间权重，`avg_capital_used` 与
      `time_in_market_pct` 都能从它算出来。
    * `POINT` —— 一个零时长的点观测（`start == end`），会话侧 `Order` 行只能提供这个。
      它对加权平均的分子贡献 0，但**计入笔数**。

    `POINT` 不是「坏数据」，它是会话侧**能给的全部信息**。把它标出来，是为了让消费方有
    机会说「这个数在会话侧不可与回测侧同读」——而不是让两边各自产出一个同名但不同义的
    `time_in_market_pct`，然后在同一个界面上并列显示。
    """

    HELD = "held"
    POINT = "point"

    @classmethod
    def values(cls) -> list[str]:
        return [m.value for m in cls]


@dataclass(frozen=True)
class OccupancyInterval:
    """一段时间内被占用的资金量（一个持仓区间，或会话侧的一个点观测）。

    字段语义：

    * `start` / `end` —— 占用区间，**左闭右开**。`end == start` 是合法的点观测
      （`kind=POINT`，权重 0、仍计一笔）；`end < start` 是构造错误，直接 ValueError。
      时间戳一律 aware（`USE_TZ` 下 DB 读出来就是 aware；naive 交给 `.astimezone()`
      会按**进程 TZ** 解释，而进程 TZ 是 Django 按 `TIME_ZONE` 覆写的产物，口径会挂在
      别人的副作用上——与 `slicing._as_utc` 同一条纪律，这里显式钉死）。
    * `notional` —— 这段时间占用的资金量（本金口径）。为 `None` 表示「开着仓位但金额
      不可知」：这是**判不了**而不是 0（0 会被读成「这段没占资金」，于是加权平均被系统
      性低估）。
    * `pnl` —— 这段持仓实现的盈亏。`notional` 不需要与 `pnl` 同时存在（一笔还没结算的
      持仓仍然占着钱）。
    """

    start: datetime
    end: datetime
    notional: Decimal | None = None
    pnl: Decimal | None = None
    kind: str = OccupancyIntervalKind.HELD.value
    notes: str = ""

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError(f"占用区间右端早于左端：{self.start} → {self.end}")
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError(
                "占用区间的两端必须带 tzinfo：naive 时间戳会按进程 TZ 解释，"
                "口径会挂在别人的副作用上"
            )
        if self.notional is not None and isinstance(self.notional, bool):
            raise ValueError("notional 不是金额：bool 不是 Decimal")
        if self.notional is not None and not self.notional.is_finite():
            raise ValueError(
                f"notional 不是有限数：{self.notional}（NaN/±inf 会让任何比较静默为假）"
            )
        if self.pnl is not None and isinstance(self.pnl, bool):
            raise ValueError("pnl 不是金额：bool 不是 Decimal")
        if self.pnl is not None and not self.pnl.is_finite():
            raise ValueError(
                f"pnl 不是有限数：{self.pnl}（NaN/±inf 会让任何比较静默为假）"
            )


@dataclass(frozen=True)
class EfficiencyWindow:
    """被统计的那一段时间（左闭右开）。

    独立成一个类型而不是两个裸 `datetime` 参数，是为了让「窗口边界归属哪一边」这件事
    有一个**唯一**的说明位置（见模块 docstring 口径 4）。
    """

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("窗口两端必须带 tzinfo：naive 时间戳会按进程 TZ 解释")
        if self.end < self.start:
            raise ValueError(f"窗口右端早于左端：{self.start} → {self.end}")

    @property
    def duration(self) -> float:
        """窗口总时长（秒）。`<= 0` 即空窗，由调用方判 `empty_window`。"""
        return (self.end - self.start).total_seconds()


@dataclass(frozen=True)
class CapitalEfficiencyResult:
    """一次资金效率计算的结论。

    `computed=False` 时 `reason` 必有一个值，且**全部指标键都是 `None`**——整段 None，
    不是某个键 None。（调用方若只挑一个键读，会把「判不了」读成一个正常值。）
    """

    computed: bool
    reason: str | None
    avg_capital_used: Decimal | None
    capital_utilization: float | None
    idle_pct: float | None
    time_in_market_pct: float | None
    return_per_capital_used_pct: float | None
    sample_sufficient: bool
    sample_threshold: int
    trade_count: int
    occupied_seconds: float
    window_seconds: float
    pnl_total: Decimal | None
    notes: tuple[str, ...] = field(default=())

    def as_payload(self) -> dict[str, Any]:
        """落进 `metrics["capital_efficiency"]`（或 `session_stats`）的 JSON-safe 载荷。

        金额出成**定点字符串**（8 位小数，`Decimal(20,2)` / `Decimal(20,8)` 的既有口径），
        与 `session_stats.money()` 一致：这条链路上的金额从不经过二进制浮点。百分比是
        `float`——它们要与前端既有的阈值数字并列比较，而 8 位定点串对那个用途是多余的。
        """
        return {
            "avg_capital_used": _money(self.avg_capital_used),
            "capital_utilization": self.capital_utilization,
            "idle_pct": self.idle_pct,
            "time_in_market_pct": self.time_in_market_pct,
            "return_per_capital_used_pct": self.return_per_capital_used_pct,
            "sample_sufficient": self.sample_sufficient,
            "sample_threshold": self.sample_threshold,
            "trade_count": self.trade_count,
            "occupied_seconds": round(
                self.occupied_seconds, CAPITAL_EFFICIENCY_PRECISION
            ),
            "window_seconds": round(self.window_seconds, CAPITAL_EFFICIENCY_PRECISION),
            "pnl_total": _money(self.pnl_total),
            "notes": list(self.notes),
        }


def _money(value: Decimal | None) -> str | None:
    """金额 → 8 位定点字符串（`None` 原样）。出参是字符串，理由见 `as_payload`。"""
    if value is None:
        return None
    return f"{value.quantize(Decimal('0.00000001')):f}"


def _as_pct(ratio: Decimal) -> float:
    """比值 → 百分点，按 `CAPITAL_EFFICIENCY_PRECISION` 定精度。

    全程只有一个地方把数转成 `float`：这里。比率先以 `Decimal` 算完（窗口总时长可能是
    一个很规整的数，但 `Σ(占用 × 时长) / 窗口` 不是），再乘 100 转 float。
    """
    return float(
        (ratio * Decimal(100)).quantize(
            Decimal(1).scaleb(-CAPITAL_EFFICIENCY_PRECISION)
        )
    )


def _clip_to_window(
    interval: OccupancyInterval, window: EfficiencyWindow
) -> tuple[datetime, datetime] | None:
    """把一个占用区间裁到窗口内（左闭右开）。完全在窗口外的返回 `None`。

    「窗口开始前已开仓」与「窗口结束后才平仓」都只计窗口内那一截：否则分子取自一个
    比窗口更大的区间，`time_in_market_pct` 可能 >100%，而加权平均的分母（窗口总时长）
    与分子（区间占用和）不再同源——口径 4 钉的就是这条。

    右端严格裁剪到 `window.end`：`end == window.end` 的区间**不被裁掉**（它整段在窗口内），
    而 `start == window.end` 的点观测整段在窗口外（右开，那一瞬属于下一个窗口）。
    """
    start = max(interval.start, window.start)
    end = min(interval.end, window.end)
    if end < start:
        return None
    if start == window.end and end == window.end:
        # 右开：`t == window_end` 那一刻属于下一个窗口，本窗口一秒都不给它。
        return None
    return start, end


def compute_capital_efficiency(
    intervals: Sequence[OccupancyInterval],
    window: EfficiencyWindow,
    total_capital: Decimal | None,
    *,
    pnl_total: Decimal | None = None,
    sample_threshold: int | None = None,
    config: FactoryConfig = CONFIG,
) -> CapitalEfficiencyResult:
    """占用区间序列 + 窗口 + 总资金 → spec D6 的那组固定指标。

    判定顺序即「越根本的判不了越先报」：

        本金不可用 → 窗口为空 → 零笔数 → 占用不可用 → 算

    四道判不了的门都不抛异常、不挑默认值顶替，一律返回 `computed=False` + 原因码 +
    **整段 None** 的指标。理由见模块 docstring 口径 5：`0` 会被读成一个正常测量值。

    `pnl_total` 不参与判不了的门：一笔没结算的持仓仍然占着钱，效率**算得出**，只是
    `return_per_capital_used_pct` 为 `None`。反过来 `pnl_total` 给了但不是有限数时判不了
    ——NaN 会让任何比较静默为假，而它会让 `return_per_capital_used_pct` 变成一个永远
    不达标、也永远不显眼的数。
    """
    # 门槛缺省走配置面（`FactoryConfig.min_trades`，即 `SAMPLE_THRESHOLD` 的同一数字），
    # 与 `walk_forward.plan_folds` 的 `config=CONFIG` 同一条纪律：默认值只有一处。
    # 显式传参时再守一遍边界——入参可由调用方给（测试、单次重算）。
    threshold = config.min_trades if sample_threshold is None else sample_threshold
    if isinstance(threshold, bool) or not isinstance(threshold, int) or threshold < 0:
        raise ValueError(f"sample_threshold 必须是非负整数，当前 {threshold!r}")

    # 预置一个「判不了」模板：任何一道门不过都走这里，整段 None + 原因码。
    def _unresolvable(reason: str, *, trade_count: int = 0) -> CapitalEfficiencyResult:
        return CapitalEfficiencyResult(
            computed=False,
            reason=reason,
            avg_capital_used=None,
            capital_utilization=None,
            idle_pct=None,
            time_in_market_pct=None,
            return_per_capital_used_pct=None,
            # 样本标注与「判不了」是两件事，别把前者混成后者。
            sample_sufficient=False,
            sample_threshold=threshold,
            trade_count=trade_count,
            occupied_seconds=0.0,
            window_seconds=max(window.duration, 0.0),
            pnl_total=None,
            notes=(),
        )

    if not isinstance(total_capital, Decimal) or isinstance(total_capital, bool):
        # `None` / 浮点 / bool：分母不成立。注意**浮点本金也被拒**——这条链路上钱是
        # Decimal，收一个 float 意味着上游已经悄悄转过一次，那正是要修的那个 bug。
        return _unresolvable(CapitalEfficiencyReason.INVALID_CAPITAL.value)
    if not total_capital.is_finite() or total_capital <= 0:
        # `<=0`：利用率的分母不成立。NaN 也走这里——`Decimal("NaN") <= 0` 是 **False**，
        # 所以必须先判 `is_finite`：少了这一步，NaN 本金会让 `capital_utilization > 1`
        # 这类比较静默为假，于是「利用率超标」看不出来，而分母其实根本不成立。
        return _unresolvable(CapitalEfficiencyReason.INVALID_CAPITAL.value)

    window_seconds = window.duration
    if window_seconds <= 0:
        # 空窗：时间加权平均的分母不存在。与 `walk_forward` 的 `total_days <= 0` 同一条
        # 纪律——「数据不够」是一个结论，不是一次崩溃，也不是一次除零。
        return _unresolvable(CapitalEfficiencyReason.EMPTY_WINDOW.value)

    if not intervals:
        # 零笔数：没有任何成交的窗口**没有资金效率**。这里绝不是 0——0 会被读成
        # 「完全没用钱」，于是「效率真高」，而真相是「这一段什么都没发生」。
        return _unresolvable(CapitalEfficiencyReason.NO_TRADES.value)

    # 裁剪到窗口内。完全落在窗口外的区间不算「一笔窗口内的成交」——它不改变窗口里
    # 任何一秒的资金状态，却会让笔数与样本标注失真。
    clipped: list[tuple[OccupancyInterval, datetime, datetime]] = []
    for interval in intervals:
        span = _clip_to_window(interval, window)
        if span is None:
            continue
        clipped.append((interval, span[0], span[1]))

    if not clipped:
        # 给了行、但一行都不落在窗口内。与零笔数同因同果：这一段窗口没有证据。
        return _unresolvable(CapitalEfficiencyReason.NO_TRADES.value)

    weighted_sum = Decimal("0")
    occupied = Decimal("0")
    pnl_sum = Decimal("0")
    pnl_known = True
    notes: list[str] = []

    for interval, start, end in clipped:
        seconds = Decimal(str((end - start).total_seconds()))
        if seconds < 0:  # 只可能是构造错误漏到这里的，宁可变红
            raise ValueError(f"裁剪后出现负时长区间：{start} → {end}")
        if seconds > 0:
            occupied += seconds
        if interval.notional is None:
            # 开着仓位但金额不可知 = 判不了，不是 0。把 None 当 0 会让加权平均被系统性
            # 低估，而「占用更多钱」≠「效率更高」这件事会因此反过来。
            return _unresolvable(
                CapitalEfficiencyReason.INVALID_CAPITAL.value,
                trade_count=len(clipped),
            )
        # 时间加权的分子：占用金额 × 该金额持续的时长。全程 Decimal（口径 1）。
        weighted_sum += interval.notional * seconds
        if interval.pnl is not None:
            if not interval.pnl.is_finite():
                # 单笔盈亏不是有限数：整段判不了。NaN 会让
                # `return_per_capital_used_pct` 的比较静默为假，于是「单位占用收益是
                # 正的」这句话无从成立。
                return _unresolvable(
                    CapitalEfficiencyReason.INVALID_CAPITAL.value,
                    trade_count=len(clipped),
                )
            pnl_sum += interval.pnl
        else:
            pnl_known = False
        if interval.kind == OccupancyIntervalKind.POINT.value:
            notes.append(
                f"{interval.start.isoformat()} 是点观测（零时长，权重 0、仍计一笔）"
            )
        if interval.notes:
            notes.append(interval.notes)

    # 分母是**窗口总时长**，不是 Σ时长：空仓间隙按占用 0 进加权（口径 3）。少了这一条，
    # 利用率会变成「有仓位时的平均仓位大小」。
    window_seconds_dec = Decimal(str(window_seconds))
    avg_capital_used = weighted_sum / window_seconds_dec

    capital_utilization = avg_capital_used / total_capital
    idle_pct = Decimal(1) - capital_utilization
    time_in_market_pct = occupied / window_seconds_dec

    # 显式传入的总盈亏优先于逐笔相加：调用方可能已经有一个汇总口径（#38 会），两边
    # 各自加一遍就有两个可能不一致的答案。传了但不是有限数时判不了（见上面的单笔分支）。
    if pnl_total is not None:
        if isinstance(pnl_total, bool) or not isinstance(pnl_total, Decimal):
            raise ValueError(f"pnl_total 必须是 Decimal，当前 {pnl_total!r}")
        if not pnl_total.is_finite():
            return _unresolvable(
                CapitalEfficiencyReason.INVALID_CAPITAL.value,
                trade_count=len(clipped),
            )
        pnl_sum = pnl_total
        pnl_known = True

    # 分母为 **0** 的唯一情形：窗口内只有点观测（会话侧的点观测权重为 0，见口径 2）。
    # 那不是「赚了 0」，而是「没有任何一段时间真正占着钱」——除零会砸出
    # DivisionByZero，而一个 `None` 能把这件事说清楚。其余指标照算：点观测仍计笔数
    # （口径 6），`avg_capital_used == 0` 本身就是那个诚实的答案。
    return_per_capital_used = (
        pnl_sum / avg_capital_used if pnl_known and avg_capital_used > 0 else None
    )

    return CapitalEfficiencyResult(
        computed=True,
        reason=None,
        avg_capital_used=avg_capital_used,
        capital_utilization=_as_pct(capital_utilization),
        idle_pct=_as_pct(idle_pct),
        time_in_market_pct=_as_pct(time_in_market_pct),
        return_per_capital_used_pct=(
            _as_pct(return_per_capital_used)
            if return_per_capital_used is not None
            else None
        ),
        # 样本太薄时数字照算，只打标注（口径 6 / 用户故事 14）。
        sample_sufficient=len(clipped) >= threshold,
        sample_threshold=threshold,
        trade_count=len(clipped),
        occupied_seconds=float(occupied),
        window_seconds=window_seconds,
        pnl_total=pnl_sum if pnl_known else None,
        notes=tuple(notes),
    )


def capital_efficiency_keys() -> tuple[str, ...]:
    """`compute_capital_efficiency` 结果里指标键的**固定契约**（spec D6 的六个键）。

    存在的理由是跨侧改名必须响：#35 的回测侧与 #38 的会话侧写的是同一组键，两边各自
    `payload[...]` 取用时没有任何东西会因改名而报错——只有一个测试会把这里与
    `as_payload()` 的键集对齐。改名要么两处一起改并让这个函数跟着变，要么在这里炸。
    """
    return (
        "avg_capital_used",
        "capital_utilization",
        "idle_pct",
        "time_in_market_pct",
        "return_per_capital_used_pct",
        "sample_sufficient",
    )
