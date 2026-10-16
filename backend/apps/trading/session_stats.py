"""会话详情里的收益统计（CONTEXT.md Glossary「会话归因已实现盈亏」）。

**口径**：只统计带 `realized_pnl` 的订单——即平仓成交产生的那部分。所以

* 开仓单不在内（它们没有已实现盈亏）；
* **未实现的部分不在内**：会话停止时不关仓（全仓没有任何代码会关），那部分敞口留在
  账户上，只能去账户持仓里看；
* **熔断减仓实现的盈亏不在内**：减仓作用于账户持仓，其子单不挂会话外键
  （`reduce.py` 全文没有 `live_session`，见 `docs/adr/0003`）。被减过仓的会话，这里的
  数字因此**系统性偏乐观**——这是已知的、写进 caveats 的边界，不是待修的缺陷。

两个数必须同时给：**胜率与它的分母**。「胜率 55.6%」与「5/9」说的是同一件事，但只有
后者能让人看出样本有多小。本仓对「假自信」的既有处理是证据门槛 ≥30 笔（CONTEXT.md
第 35 条），这里取同一个数做「样本不足」标注——**用途不同**：那条门槛决定要不要停用一个
策略，这里的标注不做任何动作，只是别让 4 笔的胜率看起来像结论。

## 资金效率那一段（spec #30 D6 / D9，子票 #38）：会话侧与回测侧**不同读**

返回值里多了一串 `capital_efficiency_*` 前缀的键，它们来自 #33 的纯模块
(`apps/strategy_factory/capital_efficiency.py`)。**同一套公式、同一个纯函数**，但
会话侧的数据状况与回测侧不一样，这个不对称是**写在这里、不糊过去**的：

* 回测侧（#35）喂的是 `BacktestTrade` 的 `close` 行，有成对的 `entry_time`/`exit_time`
  ——每一项是一段**真的持仓区间**，于是 `avg_capital_used` / `time_in_market_pct` 有
  时间权重，是可以直接读的效率数字。
* 会话侧喂的是 `Order` 的平仓行，**没有成对的开平仓时刻**（`.p4-notes/backtest-save-path.md`
  §4）。每个平仓时刻只能喂成一个**点观测**（`start == end`，`kind=point`）——它对加权
  平均贡献 0 秒，但仍然**计一笔**。

点观测输入的后果是确定的、也是**必须标出来**的：`avg_capital_used` 是 `0.00000000`、
`time_in_market_pct` 是 `0.0`、`return_per_capital_used_pct` 是 `None`。**这三个数不是
「这条会话没用钱」**，而是「这一侧量不了时长」。把它们当效率数字读，会把「重仓短打」
读成「几乎没用资金」——恰好把高风险读成高效率。

所以本模块**不拍平成回测侧的键名形状**：

* 六个契约键（`capital_efficiency_keys()` 那一组）住在 `capital_efficiency` 子载荷里，
  **逐字同回测侧**（spec 用户故事 22：口径不在两处分叉）；
* 但会话侧**不把它们摊平到返回字典顶层**。顶层一出现 `capital_utilization`，消费方就会
  按回测侧的读法读它，而会话侧那个 `0` 是「量不了」而不是「没占用」——D6 要的是同一套
  公式，不是同一份数字。谁要读，就得连 `capital_efficiency_metrics_available` 与
  `capital_efficiency_notes` 一起读，那两个字段会把「这一侧是粗口径」明说出来。
* 子载荷还带 `trade_count` / `occupied_seconds` / `window_seconds`，都是 `Decimal` 出参
  不必要知道、但**校验「为什么是 0」**必须看到的中间量。

会话侧的 `window_seconds` 是**墙钟**（`started_at → stopped_at / now`），不是「在仓时长」
——它只当加权平均的分母用，而这个分母恰好让点观测的权重落在 0 上。这一侧没有第二个
时间来源可用，不要拿它当「在仓时长」读。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Callable, Iterable

from django.db.models import Count, DecimalField, F, Q, Sum
from django.db.models.functions import Abs, Coalesce
from django.utils import timezone

from apps.strategy_factory.capital_efficiency import (
    CapitalEfficiencyReason,
    EfficiencyWindow,
    OccupancyInterval,
    OccupancyIntervalKind,
    capital_efficiency_keys,
    compute_capital_efficiency,
)

from .models import Order

#: 「样本不足」的分界。与 CONTEXT.md 第 35 条的证据门槛取同一个数（同一个仓对「多少笔
#: 才敢看胜率」的既有口径），但**不是**那条门槛：这里只产出一句标注，不产生任何动作。
SAMPLE_THRESHOLD = 30

#: 金额的统一量化口径，与 `Order.realized_pnl` 的 `decimal_places=8` 对齐。
_MONEY = Decimal("0.00000001")

#: 这两句随数字一起返回、由前端原样显示：它们讲的是**数字覆盖了什么**，而这件事只在
#: 产生数字的这一处知道。放在前端写死的话，将来谁改了 reduce 的归属，没有一个地方
#: 会变红。
_CAVEATS = (
    "不含未实现部分：会话停止时不关仓，敞口留在账户上（见账户持仓）。",
    "不含熔断减仓实现的盈亏：减仓作用于账户持仓，其子单不属于任何会话。",
)

#: 资金效率那一段的落点（模块 docstring「资金效率那一段」）：**子载荷**，不摊平到顶层。
#:
#: 存在的理由是 D6 与「口径可比性」的冲突：D6 要两边同键名（所以子载荷里就是
#: `capital_efficiency_keys()` 那一组，逐字相同），但会话侧那组数字是**点观测**算出来的
#: ——`capital_utilization` 会是 `0`，而那个 `0` 的意思是「量不了时长」，不是「没占用
#: 资金」。摊平到顶层就等于把一个读起来像回测指标、含义却不同的数摆在同一个位置上，
#: 消费方无从分辨。所以会话侧把它**收进一个必须被显式打开的子载荷**，并在顶层并列回
#: 一个状态位与一组说明，让「这一侧是粗口径」无法被忽略。
#:
#: 子载荷里的键名**不加 `capital_efficiency_` 前缀**：它已经是子载荷的名字，再加一次会
#: 长出 `capital_efficiency["capital_efficiency_avg_capital_used"]` 这种读两遍才知道在
#: 说什么的东西。
CAPITAL_EFFICIENCY_KEY = "capital_efficiency"

#: 子载荷里每个指标的键名，逐字取自 #33 的纯模块。**不在本模块重新枚举一遍**：抄一份
#: 就等于给「改了纯模块却忘了改这里」留了门，而 `as_payload()` 与
#: `capital_efficiency_keys()` 之间已经有一条测试钉着它们对齐。
_EFFICIENCY_METRIC_KEYS = capital_efficiency_keys()

#: 「这一段资金效率能不能当结论读」。**与指标键分开的顶层布尔**，而不是把指标写成
#: `None` 了事：`None` 表达不了「算出来了、但这一侧的量法比回测侧粗」。消费方（前端、
#: 漏斗）读它才知道该不该把数字摆到与回测侧同一栏里。
CAPITAL_EFFICIENCY_AVAILABLE_KEY = "capital_efficiency_metrics_available"

#: 「为什么只是粗口径 / 为什么整段没有」。**算得出时也非空**：会话侧没有一次计算是等价于
#: 回测侧的，所以总得有一句说清楚。空列表会被消费方读成「没有任何保留意见」，而那正是
#: 这条链路上最贵的一种误读。
CAPITAL_EFFICIENCY_NOTES_KEY = "capital_efficiency_notes"

#: 会话侧点观测的固定说明（算得出时带上）。
_POINT_OBSERVATION_NOTES = (
    "本侧只给点观测（每个平仓时刻一项，零时长、时间权重 0、仍计一笔）："
    "avg_capital_used / time_in_market_pct 恒为 0、return_per_capital_used_pct 恒为 "
    "None——那是「这一侧量不了持仓时长」，不是「这条会话没占用资金」。回测侧有成对的"
    "开平仓时刻，同一组数字在那边可以直接读。",
    "会话侧的窗口是墙钟（started_at → stopped_at/now），它只当加权平均的分母用；本侧"
    "没有「在仓时长」这个量，不要拿窗口去替代它。",
)

#: `session.initial_capital` 不可用时整段资金效率判不了的那句话，与纯模块的 `reason`
#: 同词表（`invalid_capital`）。**「未设」与「≤0」共用同一句**：两者的后果完全一样——
#: 利用率的分母不成立，整段判不了，而不是「利息为 0」。
_INVALID_CAPITAL_NOTE = (
    "会话本金不可用（未设或 ≤0）：利用率的分母不成立，整段判不了——不是「占用为 0」。"
)


def money(value: Decimal | None) -> str | None:
    """金额 → 字符串（8 位小数，定点写法）。None 原样返回。

    出参是字符串而不是数字，与 `OrderSerializer` 的 DecimalField 一致：这条链路上
    的金额从不经过二进制浮点。

    **用 `:f` 而不是 `str()`**：`Decimal("0").quantize(...)` 的 `str()` 是 `"0E-8"`
    ——科学计数法。它在 JS 侧恰好还能被 `parseFloat` 读成 0，所以这个错不会以「显示
    不对」的形式暴露，只会以「接口里躺着一个怪字符串」的形式留着。
    """
    if value is None:
        return None
    return f"{Decimal(value).quantize(_MONEY):f}"


def efficiency_payload(
    intervals: Iterable[OccupancyInterval],
    window: EfficiencyWindow,
    total_capital: Decimal | None,
    *,
    pnl_total: Decimal | None = None,
    sample_threshold: int | None = None,
) -> dict[str, Any]:
    """占用区间 + 窗口 + 本金 → **返回字典里的那一段**（纯函数，不碰库）。

    这一层很薄，薄到值得单独存在：它把纯模块的 `CapitalEfficiencyResult` 翻成「会话侧
    该怎么呈现」——一个状态位与一组说明文字，而不是把六个指标裸着递给消费方。原因是那
    六个键**在这一侧**的含义与回测侧不同（模块 docstring「资金效率那一段」），而这件事
    不能指望消费方自己记得。所以：

    * 算不出（本金不可用 / 空窗 / 零笔数）→ `available=False`、六个指标整段 `None`、
      `notes` 里带原因码与一句人话。**整段 None，不是某个键 None**；调用方若只挑一个键
      读，会把「判不了」读成一个正常值。
    * 算得出（含纯点观测）→ `available=True`，但 `notes` **仍然非空**：会话侧的成功案例
      照旧比回测侧粗，这句保留意见不随成败消失。
    * 无论成败都带上 `trade_count` / `occupied_seconds` / `window_seconds`：它们是三个
      **校验「为什么是 0」必须看到**的中间量，少了它们，「`avg_capital_used` 是 0」只能
      靠猜。点观测下 `occupied_seconds` 为 0 就是那句「量不了时长」的证据。
    """
    # 门槛缺省走 `SAMPLE_THRESHOLD`：本模块的六个键与 `sample_sufficient` 都源自它，
    # 让它有第二个默认值就等于让同一屏上的两处「样本够不够」可能不一致。
    threshold = SAMPLE_THRESHOLD if sample_threshold is None else sample_threshold

    result = compute_capital_efficiency(
        tuple(intervals),
        window,
        total_capital,
        pnl_total=pnl_total,
        sample_threshold=threshold,
    )
    metrics = result.as_payload()

    # `reason` 与 `computed`在结果对象上、不在载荷里（纯模块 docstring 钉过），所以子
    # 载荷在这里把它们补回来：没有原因码的「判不了」在事后无法归因。
    notes = list(_POINT_OBSERVATION_NOTES) if result.computed else []
    if not result.computed:
        notes.append(f"整段判不了：{result.reason}")
        if result.reason == CapitalEfficiencyReason.INVALID_CAPITAL.value:
            notes.append(_INVALID_CAPITAL_NOTE)

    payload: dict[str, Any] = {
        "computed": result.computed,
        "reason": result.reason,
        "trade_count": result.trade_count,
        "sample_threshold": result.sample_threshold,
        "pnl_total": metrics["pnl_total"],
        "occupied_seconds": metrics["occupied_seconds"],
        "window_seconds": metrics["window_seconds"],
        "notes": notes,
    }
    for key in _EFFICIENCY_METRIC_KEYS:
        payload[key] = metrics[key]
    return payload


#: 本模块**已经占用**的键名，与六个契约键撞了名。
#:
#: `sample_sufficient` 在两个地方各有一个：本模块顶层那个数的是**订单笔数**
#: （`closed_orders` 的口径），纯模块里那个数的是**区间笔数**。点观测下两者恒等
#: （一笔订单 = 一个点观测），但它们是两个概念，将来任何一侧改了取数就会分叉——
#: 而撞名会让「分叉」以**静默覆盖**的形式发生：`**efficiency` 展开时后写的赢，于是顶层
#: 那个数会悄悄换成另一套口径，没有任何测试或异常指着这件事。
#:
#: 所以摊平时**跳过它**：顶层 `sample_sufficient` 保持原义（订单笔数），效率口径的那个
#: 留在 `capital_efficiency` 子载荷里（列表响应另有 `sample_sufficient_orders` 出口）。
_COLLIDING_METRIC_KEYS = frozenset({"sample_sufficient"})


def _efficiency_keys(payload: dict[str, Any] | None) -> dict[str, Any]:
    """把 `efficiency_payload` 摊成**返回字典顶层的那几个键**。

    摊平这件事本身就是「这一段能不能当结论读」的答案：`computed` 为假时契约键一律
    `None`——**不是缺键、也不是 0**。缺键会让消费方在 `dict[...]` 上炸或悄悄拿旧值；0 会
    被读成「完全没用钱」（于是「效率真高」），而真相是「这一段没有资金效率证据」（纯模块
    口径 5）。子载荷本身**原样带出**（含 `computed` / `reason` / 中间量），所以「为什么是
    None」永远有一个地方可以查。

    **与既有键撞名的那些不摊平**（`_COLLIDING_METRIC_KEYS`）：它们只在子载荷里出现。
    理由见那个常量的注释——撞名的后果是一次静默覆盖，而最贵的错误正是这种「两处看起来
    都正常」的。
    """
    payload = payload or {}
    computed = bool(payload.get("computed", False))
    keys: dict[str, Any] = {
        CAPITAL_EFFICIENCY_AVAILABLE_KEY: computed,
        CAPITAL_EFFICIENCY_NOTES_KEY: list(payload.get("notes", ())),
        CAPITAL_EFFICIENCY_KEY: payload,
    }
    for key in _EFFICIENCY_METRIC_KEYS:
        if key in _COLLIDING_METRIC_KEYS:
            continue
        keys[key] = payload.get(key) if computed else None
    return keys


def _unavailable_efficiency(reason: str, note: str) -> dict[str, Any]:
    """整段判不了（不经纯模块）。

    本函数在**窗口之前**就得能回答：本金没设的会话连 `started_at` 都可能没有，而纯模块
    的 `invalid_capital` 门要一个 `EfficiencyWindow`。这里不造一个假窗口去喂它——假窗口
    会让 `window_seconds` 出现一个看起来可信的数，而它并不是这条会话的窗口。
    """
    return _efficiency_keys(
        {
            "computed": False,
            "reason": reason,
            "trade_count": 0,
            "sample_threshold": SAMPLE_THRESHOLD,
            "pnl_total": None,
            "occupied_seconds": 0.0,
            "window_seconds": 0.0,
            "notes": [f"整段判不了：{reason}", note],
        }
    )


def compute_session_stats(
    pnls: Iterable[Decimal | None],
    *,
    intervals: Iterable[OccupancyInterval] | None = None,
    window: EfficiencyWindow | None = None,
    total_capital: Decimal | None = None,
) -> dict:
    """一串已实现盈亏（+ 可选的占用区间 / 窗口 / 本金）→ 统计。**纯函数**，不碰库。

    一笔 = 一张带 `realized_pnl` 的订单。部分成交后补齐的那张单仍是一行（`_apply_close_pnl`
    用增量修正，盈亏写回同一行），所以按行计数不会重复。

    「胜」的判据是 `> 0`：**0 与亏损同列**。盈亏恰好为 0 的那笔既不赚也不亏，把它算进
    胜率只会让胜率看起来比实际好，而这个数没有任何一个用途需要它偏高。

    n = 0 时比率类字段一律为 `None`（不是 0）：一条还没成交的会话没有胜率，写 0 会被读成
    「一笔没赢过」。

    ## 为什么是**扩参**（可选 kwarg）而不是**另起一个兄弟函数**

    spec 用户故事 18 要的是「指标在列表 / 详情 / 漏斗里同样可得」，也就是**同一个入口**。
    另起一个 `session_capital_efficiency(session)` 会让「胜率」与「资金效率」各有一个取数
    口，两个口将来各按一套过滤条件取数——那正是本模块开头那句「免得同一个数在第二处按
    另一套过滤条件算一遍」要防的事。所以扩的是这里；默认值让既有的三个调用方
    （`session_stats_for`、`effects.read_paper`、纯函数那半测试）一行都不用改。

    **资金效率那三个参数是 `keyword-only`**：`compute_session_stats(pnls, window)` 这种
    位置传参一旦被写出来，将来在中间插参就是一次静默错位。

    ## 三个参数缺省时：整段判不了，不是静默跳过

    只给 `pnls` 时（调用方还没适配、或这条会话压根没有本金可当分母）**仍然返回那组键**，
    值为 `None`、原因写进 `capital_efficiency_notes`。这与「新键对既有调用方安全」不冲突：
    键在、值说清楚了为什么没有。**绝不能缺键**——缺键会让「没适配」与「没数据」在消费侧
    长得一模一样。

    判定顺序与纯模块一致（越根本的越先报）：本金/窗口都缺 → `invalid_capital`；两者都有
    再交给纯模块判空窗与零笔数。注意 n = 0 时**先**走这里、**再**走下面那个 count==0 的
    早返回：纯模块对零笔数判 `no_trades`（而不是「利用率 0」），而胜率那一段判「比率全
    None」——两件事各有各的码，不互相顶替。
    """
    values = [Decimal(v) for v in pnls if v is not None]
    count = len(values)

    if intervals is None or window is None:
        # 调用方还没适配这一侧，或这条会话没有可用的本金/起点。
        efficiency = _unavailable_efficiency(
            CapitalEfficiencyReason.INVALID_CAPITAL.value, _INVALID_CAPITAL_NOTE
        )
    else:
        efficiency = _efficiency_keys(
            efficiency_payload(
                intervals,
                window,
                total_capital,
                # 总盈亏由**本函数**这一处汇总，不让纯模块再逐项加一遍：`realized_pnl_total`
                # 与子载荷的 `pnl_total` 是两个键名下的同一个数，两处各加一次就有两个可能
                # 不一致的答案。零笔数时传 None 而不是 Decimal("0")——「没有成交」不是
                # 「赚了 0」，纯模块据此判 `no_trades`。
                pnl_total=sum(values, Decimal("0")) if count else None,
            )
        )

    if count == 0:
        return {
            "realized_pnl_total": money(Decimal("0")),
            "closed_orders": 0,
            "winning_orders": 0,
            "win_rate": None,
            "sample_threshold": SAMPLE_THRESHOLD,
            "sample_sufficient": False,
            "avg_pnl": None,
            "best_pnl": None,
            "worst_pnl": None,
            "caveats": list(_CAVEATS),
            **efficiency,
        }

    total = sum(values, Decimal("0"))
    winning = sum(1 for v in values if v > 0)

    return {
        "realized_pnl_total": money(total),
        "closed_orders": count,
        "winning_orders": winning,
        "win_rate": round(winning / count, 4),
        "sample_threshold": SAMPLE_THRESHOLD,
        "sample_sufficient": count >= SAMPLE_THRESHOLD,
        "avg_pnl": money(total / count),
        "best_pnl": money(max(values)),
        "worst_pnl": money(min(values)),
        "caveats": list(_CAVEATS),
        **efficiency,
    }


def session_efficiency_input(
    session,
    rows: Iterable[tuple[Decimal | None, Any]],
    *,
    now: Callable[[], Any] | None,
) -> tuple[list[OccupancyInterval], EfficiencyWindow | None, Decimal | None]:
    """`(pnl, 该行的时刻)` 序列 + 会话 → 纯模块要的三个输入。**取数口径只此一处。**

    ## 时刻取的是 `created_at`（这是口径的一部分，不是实现细节）

    `Order` 没有 `filled_at`（`.p4-notes/backtest-save-path.md` §4），只有 `created_at`
    / `updated_at` 两个自动字段。`created_at` 是**下单**时刻、不是成交时刻——它离真实
    平仓时刻有多远，这一侧无从得知。**正是因为存在这个误差，本侧才只喂点观测**：拿一个
    「大概是那时候」的时刻去撑起一段假的持仓时长，比承认量不了更坏——那是把一个猜出来
    的数放进与回测侧同名的指标里。

    ## 窗口是墙钟，且 `now()` 必须可注入

    `started_at → stopped_at`（已停）/ `now()`（未停）。`now` 可注入与 `deps.now` 同一
    条纪律：不给注入点就没法钉「恰好落在窗口右端那一刻的成交算不算这一窗」这条边界，
    用例只能靠真实时钟，而那些断言会随运行时刻漂移。生产传 `None`，走 `timezone.now()`。

    `started_at` 为空、或时间倒挂（`stopped_at < started_at`，库里的一行脏数据）→ 窗口
    为 `None`（量不了）。**不造一个「从第一笔成交起」的窗口**：那会让分母随成交分布变化，
    利用率就不再是「占用 / 窗口」，而是一个自己长出来的数。倒挂也不留给纯模块去炸：
    它的 `ValueError` 是给**构造错误**的纪律，脏数据该判不了，不该炸。
    """
    total_capital = session.initial_capital
    if total_capital is None or total_capital <= 0:
        # 分母不成立。与纯模块的 `invalid_capital` 同码、同一句话。
        return [], None, None

    started_at = session.started_at
    if started_at is None:
        return [], None, total_capital

    end = session.stopped_at
    if end is None:
        end = now() if now is not None else timezone.now()
    if end < started_at:
        return [], None, total_capital

    intervals = [
        OccupancyInterval(
            start=at,
            end=at,
            # 点观测的占用金额取该行的单笔名义值。纯点观测下它乘的是 0 秒，对加权平均
            # 一分钱也不贡献——所以**它取什么并不改变答案**，写进去只是为了让
            # `occupied_seconds == 0` 那个 0 有一个可读的来源。故意不在这里解释「为什么
            # 不是会话本金」：那会让人以为它在参与计算。
            notional=abs(pnl),
            pnl=pnl,
            kind=OccupancyIntervalKind.POINT.value,
            notes="会话侧点观测：Order 没有成对的开平仓时刻，只有 created_at。",
        )
        for pnl, at in ((p, a) for p, a in rows if p is not None)
    ]
    return intervals, EfficiencyWindow(start=started_at, end=end), total_capital


def session_stats_for(session, *, now: Callable[[], Any] | None = None) -> dict:
    """一条会话的统计。**取数的唯一入口**：详情视图与任何别的调用方都走这里，
    免得「同一个数在第二处按另一套过滤条件算一遍」。

    spec 用户故事 18（「指标在会话列表的统计接口里同样可得」）与这里的关系：详情视图与
    列表视图**共用** `annotate_efficiency_input` 那一个 SQL 聚合取数（见它的 docstring），
    所以两个接口给的是同一套键、同一个数；本函数只是把它换算成纯函数要的形状。

    `now` 透传下去（可注入，纪律见 `session_efficiency_input`）。默认 `None` 即真实时钟。
    """
    rows = list(
        Order.objects.filter(live_session=session, realized_pnl__isnull=False)
        # 排序 + `values_list` 里带上 `created_at`：`session_efficiency_input` 要「每笔
        # 盈亏 + 它的时刻」这一对，而两列必须来自**同一行**——分两次 `.values_list`
        # 取会得到一个按 PK 排、一个按成交时刻排的两串，配对出来的是两个不相干的行。
        .order_by("created_at")
        .values_list("realized_pnl", "created_at")
    )
    intervals, window, total_capital = session_efficiency_input(session, rows, now=now)
    return compute_session_stats(
        (pnl for pnl, _ in rows),
        intervals=intervals,
        window=window,
        total_capital=total_capital,
    )


def annotate_realized_pnl(queryset):
    """给一批会话挂上 `realized_pnl` 注解：列表接口用（一次查询算完 50 条）。

    与 `session_stats_for` 是**同一个口径的两种取数**——这里是 SQL 聚合，那里是逐单汇总。
    两条测试钉着它们对同一条会话给出同一个数：它们一旦分叉，用户会在同一屏上看到卡片与
    弹窗两个不一样的「盈亏」，而两处看起来都正常。
    """
    return queryset.annotate(
        realized_pnl=Coalesce(
            Sum("orders__realized_pnl"),
            Decimal("0"),
            output_field=DecimalField(max_digits=20, decimal_places=8),
        )
    )


def annotate_efficiency_input(queryset):
    """列表接口的资金效率取数：**两次聚合、零额外查询**。

    为什么需要这个函数（spec 用户故事 18「指标在会话列表的统计接口里同样可得」）：
    列表接口原本只用 `annotate_realized_pnl`（一个 SQL `SUM`），从不调
    `compute_session_stats`，所以那组新键在列表上**不会自动出现**。让序列化器逐行去调
    `session_stats_for` 是另一条路，但 `live_session_detail` 的 docstring 已经把那条路
    否掉了：「挂到列表上就是 50 条会话各算一次统计（50 次查询）」。所以走第三条路——
    把效率段需要的**标量**在同一次查询里聚合出来，逐行换算成纯函数要的形状。

    ## 为什么不聚合出「每一行」再喂点观测

    详情路径喂的是一串点观测（每笔盈亏一个时刻）。列表路径**拿不到那个序列**：
    `ArrayAgg` 只有 PostgreSQL 有，而本仓测试跑在 SQLite 上——只在一侧能跑的聚合，等于
    把 bug 留给另一侧。而本侧的点观测**全部零时长、权重 0**，所以它们对加权平均的贡献
    是 `0`，对 `occupied_seconds` 的贡献也是 `0`；真正改变答案的只有三个**标量**：

    * `trade_count` —— 笔数（`sample_sufficient` 与 `no_trades` 判的都是它）；
    * `pnl_total` —— 盈亏代数和（与 `realized_pnl` 注解同源，**同一处汇总**）；
    * `occupied_notional` —— `SUM(ABS(realized_pnl))`，点观测下唯一参与计算的金额。

    于是「逐笔序列」与「这三个标量」喂给纯模块后给出**同一个答案**，而后者只用两种后端都
    有的 `Sum` / `Count` / `Coalesce`。这不是近似：点观测的时间权重恒为 0，`weighted_sum`
    两条路都是 0；笔数由 `efficiency_trade_count` 还原成**同样条数**的点观测，所以
    `trade_count` 也是同一个数（合成一条会让它变成 1，与详情侧分叉——见
    `efficiency_input_from_row`）。

    ## 与 `session_stats_for` 的关系

    两条路都经 `compute_session_stats` 出结论，所以列表卡片与详情弹窗读的是同一套键。
    有一条测试（`TestTheTwoReadingsAgree`）钉着它们对同一条会话给出同一个数——分叉的
    表现是同一屏上卡片与弹窗两个不一样的「资金效率」，而两处看起来都正常。

    `now` 不在这里注入：本函数只产注解，真实时钟在 `efficiency_input_from_row` 那一步
    用（见它的签名），那样纯换算就能被无库的测试钉住。
    """
    return annotate_realized_pnl(queryset).annotate(
        efficiency_trade_count=Count(
            "orders__realized_pnl",
            filter=Q(orders__realized_pnl__isnull=False),
            distinct=True,
        ),
        # **落在窗口内**的笔数（左闭右开，与纯模块 `_clip_to_window` 同一条边界）。
        #
        # 必须有这一条：只数 `efficiency_trade_count` 会把窗口外的订单也算进来，而详情
        # 路径（逐行喂点观测、由纯模块按同一个边界裁）会把它们丢掉——于是同一个键在两条
        # 路上两个值，卡片与弹窗不一致，而两处看起来都正常。这个 bug 是用例先发现的：
        # `stopped_at` 与订单 `created_at` 只差几微秒时，右开边界就会把它们分到两边。
        #
        # 右端用 `F("stopped_at")` 表达「每行自己的停止时刻」，下推成 SQL 比较（不是
        # Python 侧过滤，也不是逐行回表）；`stopped_at` 为空即「还在跑」，右端是 now，
        # 而 now 在 SQL 里没法优雅比较，所以那一支退化成「不设上界」——对未停止的会话，
        # 「订单晚于 started_at」就是全部条件（未来的订单本就不该存在）。
        efficiency_trade_count_in_window=Count(
            "orders__realized_pnl",
            filter=Q(
                orders__realized_pnl__isnull=False,
                orders__created_at__gte=F("started_at"),
            )
            & (Q(orders__created_at__lt=F("stopped_at")) | Q(stopped_at__isnull=True)),
            distinct=True,
        ),
        # 点观测下的「占用金额」。**不是会话本金**：纯点观测的加权分子恒为 0，
        # 这个数乘 0 秒，改不了任何结论，写它只是让 `occupied_seconds == 0` 那个 0
        # 有一个可读的来源。
        efficiency_occupied_notional=Coalesce(
            Sum(Abs("orders__realized_pnl")),
            Decimal("0"),
            output_field=DecimalField(max_digits=20, decimal_places=8),
        ),
    )


def efficiency_input_from_row(row, *, now: Callable[[], Any] | None = None):
    """列表注解行 → `compute_session_stats` 的资金效率三个参数。**纯换算，不碰库。**

    返回 `(intervals, window, total_capital)`，形状与 `session_efficiency_input` 一致，
    所以两条路共用同一个纯函数入口。

    行是注解结果（`LiveSession` 实例 + 那三个注解），字段缺失一律按「这一行没适配到」处理
    （`getattr(..., None)`）——测试可以拿一个 `SimpleNamespace` 直接喂进来钉边界，不必把
    会话造进库。这也是本函数与 `session_efficiency_input` 分开的理由：那个要真的
    `Order` 行，这个只要三个数。

    ## 三种「判不了」，都不抛

    * 本金缺失或 ≤0 → `invalid_capital`（利用率的分母不成立）；
    * `started_at` 为空、或 `stopped_at < started_at`（脏数据）→ `invalid_capital`
      （没有可用的窗口，而窗口是加权平均的分母）；
    * 零笔数 → 留着窗口、零区间，交给纯模块判 `no_trades`。

    **绝不把它们翻成 0**：`capital_utilization = 0` 会被读成「完全没用钱」，于是「效率
    真高」——而真相是这一段没有资金效率证据（纯模块口径 5）。

    `now` 可注入（与 `deps.now` 同一条纪律）：未停止的会话窗口右端就是它，没有注入点就
    没法钉这条边界，用例只能靠真实时钟、断言会随运行时刻漂移。生产传 `None`。
    """
    # **窗口内**的笔数（`efficiency_trade_count_in_window`），不是全部笔数：两者的差就是
    # 落在窗口外的那些订单，而详情路径会把它们裁掉。用全部笔数会让 `trade_count` 在两条
    # 路上分叉——这条注解存在就是为这件事。
    #
    # 退回 `efficiency_trade_count`：只有「只注解了全量笔数」的老调用方才走到这里，而那些
    # 调用方本来也不传窗口（下面 `window is None` 那一支会判 `invalid_capital`）。
    count = getattr(row, "efficiency_trade_count_in_window", None)
    if count is None:
        count = getattr(row, "efficiency_trade_count", None) or 0
    # 行可以只带 `realized_pnl` 注解（`session_stats_for` 之外的调用方），那时用不到
    # `efficiency_occupied_notional`——点观测的占用金额本来就不改变结论。
    notional = getattr(row, "efficiency_occupied_notional", None) or Decimal("0")
    pnl_total = getattr(row, "efficiency_pnl_sum", None)
    if pnl_total is None:
        pnl_total = getattr(row, "realized_pnl", None)

    total_capital = getattr(row, "initial_capital", None)
    if total_capital is None or total_capital <= 0:
        return [], None, None

    started_at = getattr(row, "started_at", None)
    stopped_at = getattr(row, "stopped_at", None)
    if started_at is None or (stopped_at is not None and stopped_at < started_at):
        return [], None, total_capital

    window = EfficiencyWindow(
        start=started_at,
        end=stopped_at
        if stopped_at is not None
        else (now() if now else timezone.now()),
    )

    if count == 0:
        # 零笔数：窗口照给，纯模块据此判 `no_trades`（而不是「利用率 0」）。
        return [], window, total_capital

    # `count` 条点观测，**每条各一个**（时刻都用窗口左端——列表侧拿不到逐笔时刻，而这些
    # 点观测的权重全是 0，用什么时刻都不改变任何数值）。
    #
    # 为什么不是合成一条、把笔数写进 notes：纯模块按 `len(clipped)` 数笔数，合成一条会让
    # `trade_count` 变成 1，而详情路径给的是真实的笔数——同一个键在两条路上两个值，正是
    # 「卡片与弹窗不一致」那个最贵的形状。而 `trade_count` 是**契约键之外**的中间量，
    # 消费方会拿它来交叉核对，所以它必须是真的。
    #
    # 占用金额只在第一条上给：纯点观测下 `Σ(占用 × 0 秒)` 恒为 0，所以给几条都一样；
    # 其余给 0 是为了让「这个数没被重复计入」这件事在数据里看得见。
    intervals = [
        OccupancyInterval(
            start=window.start,
            end=window.start,
            notional=notional if index == 0 else Decimal("0"),
            pnl=Decimal(pnl_total) if index == 0 and pnl_total is not None else None,
            kind=OccupancyIntervalKind.POINT.value,
            notes=(
                "会话侧点观测（列表路径）：由 SQL 聚合还原的笔数，零时长、权重 0，"
                "仍计笔数。"
            ),
        )
        for index in range(count)
    ]
    return intervals, window, total_capital


def row_efficiency_keys(row, *, now: Callable[[], Any] | None = None) -> dict[str, Any]:
    """列表行 → **返回字典里那段资金效率键**（与 `compute_session_stats` 逐字同套）。

    存在的理由是把「列表也要有这些键」这件事做成一个可以单独调用的入口：序列化器不用把
    `compute_session_stats` 的胜率那一半也算一遍——列表的胜率归 `annotate_realized_pnl`
    那条 SQL，而这一段只补效率键。共用同一个纯模块，所以键名与数值口径都不会分叉。
    """
    intervals, window, total_capital = efficiency_input_from_row(row, now=now)
    pnl_total = getattr(row, "efficiency_pnl_sum", None)
    if pnl_total is None:
        pnl_total = getattr(row, "realized_pnl", None)

    if window is None:
        # 本金/窗口不可用：整段判不了。**不经纯模块**——它要一个真实窗口，而这里没有。
        return _unavailable_efficiency(
            CapitalEfficiencyReason.INVALID_CAPITAL.value, _INVALID_CAPITAL_NOTE
        )
    # 零笔数也要经纯模块：它判 `no_trades`，与上面的 `invalid_capital` 是**两个不同**的
    # 原因码，对应两种不同的补救动作（补时间 vs 修本金），不能混成一个。
    return _efficiency_keys(
        efficiency_payload(
            intervals,
            window,
            total_capital,
            pnl_total=Decimal(pnl_total) if pnl_total is not None else None,
        )
    )
