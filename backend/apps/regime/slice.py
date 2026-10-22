"""历史切片（第①段单元 6i）：逐笔成交 → 「策略 × 阶段」适用性证据。

## 这一步在整条链路里的位置

    日线（全量历史）
        ↓  quant.label_series —— 同一份实现，既是每日实时判定的量化部分，也是历史标注
    「日期 → 阶段」映射（build_tags，派生、不落库）
        ↓  本模块：按开仓时刻归属 + PnL 按持仓时长 pro-rata
    「策略 × 阶段」证据（笔数 / 覆盖月数 / 分段最大回撤 / 分段 Calmar / 置信区间）
        ↓  单元 6ii 物化进 BacktestResult.metrics
    池化（单元 7）→ 停用决策（只记建议）

本模块是**纯函数**：不吃 DB、不读时钟、不碰 `BacktestResult`。适配层（把
`BacktestTrade` 行转成 `SliceTrade`、加载日线、盖 `computed_at`）在单元 6ii。
这样「归属与聚合」的全部判断都能用构造数据直接测，不必先造一次回测。

## 标签为什么不落库

CONTEXT.md 说「『日期 → 阶段』标签表是唯一真相」。本模块把它实现成一份**派生映射**
（`build_tags`），**不新建表**——这一条与字面措辞有张力，理由在这里写清：

1. **`apps/regime/models.py` 的纪律就是「能确定性重算的不要存」。** 日线标签是
   `quant.label_series` 对「全量历史 + 一套参数」的确定性输出，且 `label_series`
   无前视：第 D 天的标签只依赖 ≤ D 的日线。已收盘的日线不会被改写（`candles.py`
   的同步是幂等覆盖，同样的输入得到同样的行），所以「用同一套参数重算 D 天的标签」
   永远得到同一条结论。存一份出来，多出来的不是真相而是**第二个可能与重算结果
   不一致的答案**——正是这条纪律要防的东西。
2. **「唯一真相」的实质落在「只有一处实现、且从不被抄进逐笔交易」上。** 这正是
   CONTEXT.md 那句「不给逐笔交易打阶段字段」的对照面：真正的错误是给每笔交易冻结
   一个阶段字段（判定算法一漂移，那些字段就集体变错而看起来完全正常），而不是
   「标签有没有落在某张表里」。全景里标签只有 `quant.label_series` 一处实现，
   `build_tags` 是它的投影，逐笔交易没有阶段字段，`metrics` 里只有聚合结论。
3. **真正必须落库的是池化后的适用性结论（单元 7）**，CONTEXT.md 对它另有一句
   「必须落库」：停用决策要能回答「依据哪几个回测、多少笔交易、什么时间区间」，
   那是**审计**要求，现算答不出来。标签不需要审计——它可重算；结论需要审计——
   它是在某一刻做出的、之后会被拿来说事的判断。这两件事不是同一条规矩的两次
   落地，所以一处存、一处不存是自洽的，不是双标。

代价是同一份标签在每次切片重算时被重算一遍（一次全量 `label_series` 约十毫秒级，
一年 365 行）。这个代价换来的是「标签不可能与当前算法不一致」。

## 归属规则：两处刻意的不对称

CONTEXT.md：只对**开仓时刻**归属一次阶段，不按持仓期切分；跨阶段交易的赋权刻意
不对称——**PnL 按持仓时长 pro-rata 分摊（求准），笔数按入场段计 1 笔（求保守）**。

于是一笔交易在同一份结果里可能同时：「计入」某个阶段的笔数、「贡献」另一个阶段的
PnL。这不是 bug，是设计要求：笔数进的是「这笔交易是在哪个阶段被决定的」那一格
（决策的场景），PnL 进的是「这笔钱是在哪个阶段赚到的」那些格（盈亏的场景）。
两者混用会把「策略在箱体里开仓、行情随即转下行、在趋势里赚到钱」算成「箱体里
表现好」——而它恰恰是被箱体的那一笔开进去的。

### 结果里有**两个** PnL 口径，不是笔误

同一格里能同时读到「按份额分摊的 PnL」与「按整笔计的 PnL」，它们是给两个不同问题用的：

| 量 | 口径 | 回答的问题 |
|----|------|-----------|
| 分段回撤 / 分段 Calmar（`total_pnl` 等） | **分摊份额** | 这段行情里，资金曲线发生了什么 |
| `win_rate` / `pnl_mean_ci95` / `trades` | **整笔全额** | 在这个阶段开仓的那些交易，赢面与单笔盈亏如何 |

判据是「这笔交易**归属**哪一格」还是「这笔钱**赚在**哪一格」：胜率与单笔均值刻画的是
**交易集合**（按开仓时刻集结），所以一笔就是一个观测，用全额；回撤与 Calmar 刻画的是
**资金曲线**（按日历展开），所以钱必须落在它实际停留的那些天上，用份额。混用会让
「胜率 70% 但曲线一直回撤」这种真实存在的组合变得无法表达。

### 「日期」取哪个口径

归属用**开仓时刻的 UTC 自然日**，与日线标签的键同口径（`DailyCandle.date` 就是
UTC 开盘日）。不用业务日（`to_business(...).date()`）：两者只在 UTC 00:00 = 北京
08:00 这一刻重合，对任意时刻会差一天，而差的方向恰好是错的——北京时间 01-05 02:00
的一笔开仓落在 01-04 那根日线里（01-04 00:00 UTC 起算的 24 小时），取业务日会把它
归到 01-05 那根**尚未开盘**的日线上。用 UTC 自然日与「标签是那根日线的属性」自洽。

### 这算不算前视

算，且是有意的。日线 D 的标签用到 D 的收盘价，而交易发生在 D 之内（可能很早）。
切片回答的是「这笔交易**落在哪个行情阶段**」，是对已发生的事做归类，不是模拟
「当时该不该做」——策略从未读到过标签。作为对照，实时链路用的是「最近一根已收盘
日线签的结论」（D−1），本模块不采用它：那会让「今天的交易」按昨天的阶段归类，
而「日期落在哪个阶段」问的是今天。这是一个可翻转的选择，判定点就这一处
（`_entry_date`），要改就改它一个。

## 载荷里为什么带着逐日曲线与逐笔样本（`merge_inputs`，版本 2）

单元 7 要把同一个（策略 × 阶段）在**多次回测**上的证据合并成一条结论。合并的口径是
「把样本拼起来重算」，不是「把结论投票」：一笔一笔地投会让一个 5 笔的短窗口与一个
100 笔的长窗口等价，而回撤尤其不能平均——**平均出来的回撤不是任何一个组合的回撤**
（它既可能大于也可能小于真值），与 ADR 0002 否掉「把几路信号揉成一个影响分 73」是
同一条理由。要重算就得有样本，而上面那些指标全是**聚合量**：

| 要被重算的量 | 它需要什么 | 为什么不能从已存的指标反推 |
|--------------|-----------|--------------------------|
| 分段回撤 / Calmar / 年化 | 逐日序列 | 回撤是曲线的形状，首末与总和都定不下它 |
| 胜率 / 单笔均值区间 / 笔数 | 逐笔样本 | `win_rate` 是四舍五入过的比例，反推笔数是个近似 |
| 覆盖月数（`min_months`） | 每笔的**开仓日** | **完全无法**从任何计数恢复——`months` 只说了有几个 |

于是每格多一个 `merge_inputs`，两个键：

- `daily_return`：`{"YYYY-MM-DD": 当日分摊份额 ÷ 初始资金}`；
- `trades`：`[[开仓日, 该笔全额 ÷ 初始资金], ...]`，保持成交传入的顺序（`trades`
  的第二项于是同时给出笔数、胜率、单笔均值区间三个量，开仓日给出覆盖月数）。

**为什么是收益率而不是金额**：一次（策略 × 阶段）的证据来自多份回测，而各份的
`initial_capital` 不同（`BacktestResult` 里是个没有下界校验的自由字段）。金额直接
相加等于按各家本金给权重——一份 10 万本金的回测会把一份 1 万本金的回测淹掉，
而这两份的证据强度本该由**笔数与月数**决定，不由本金决定。归一成收益率之后，
合并出来的曲线是「这些回测等额并行跑」那条真实轨迹（每条一份名义本金），
回撤与 Calmar 于是是**某个组合的真实数字**，而不是谁算出来的加权平均。

分母就是 `initial_capital`，而它**可以 ≤ 0**（同一句「没有下界校验」的另一面），
那时这一节落 `None`：不可用。不挑 `1.0` 顶替（顶替出来的曲线是金额，会把每一份正常
归一化的样本淹掉），也不抛（一个坏结果会把整批重算拖成毒丸重试）。池化那边必须把
`None` **计数**出来，理由见 `_merge_inputs`。

代价写在明处：`merge_inputs` 里的量与同格的其他数字**不同量纲**（那些是金额，这些是
比例）。这是刻意的——它们回答的是不同的问题，池化要的是「再算一遍」的原料，而原料
必须与本金无关。两个键各自对应上表的一行，谁也不能从谁推出来。

池化**不回头读 `BacktestTrade`**、也不重算标签，就在这些载荷上做纯聚合。理由是审计：
池化结论要能回答「依据哪几个回测、多少笔交易、什么区间」，而答案必须是**当时物化的
那份证据**——重新读一遍成交表，读到的可能是回测重跑后的新成交，于是报告引用的数字
与它引用的证据对不上，且没有任何东西会提示这一点。

**全样本不在这里存**：它是各格 `daily_return` 的**逐日逐元素之和**（`slice_backtest`
里 `daily[day]` 与 `per_regime_daily[regime][day]` 加的是同一个 `share`，所以这是一条
恒等式，不是近似）。多存一份就等于给同一个量留了第二个答案，而它会漂。

曲线上的数按 `_ROUND` 落库（与载荷里其余数字同一个精度）。代价是池化算出来的是
「曲线的数」的和，而不是「各格 `total_pnl`」的和——两者差在 1e-6 的量级上。留下这句
是为了将来有人对不上账时能一眼看到差异的来源，而不是去怀疑公式。

## 高波动档不参与适用性判断

CONTEXT.md：高波动是**保命档**，不做适用性判断，一律停开新仓；**证据门槛在高波动档
失效**，切片对高波动阶段无实际作用。所以这一格照样算数字（不是特例跳过、不该假装
没算），但状态是 `blanket` 而不是 `fit`/`unfit`——**它不是「适用性四态」里的第五态，
而是「此格没有适用性结论」**：在这里推不出停用决策，因为停用决策本来就与证据无关。
消费者（单元 7、日报）必须把 `blanket` 与 `中性` 分开读：中性说「证据不够，先别动」，
`blanket` 说「与证据无关，此刻谁都不该开新仓」。

## 「未知」与「中性」的分别

- `unknown`：该阶段在回测窗口内**一天都没有出现过**（已判定天数为 0）。这是
  「无历史数据」，与样本厚薄无关，再多样本也回答不了。
- `neutral`：阶段出现过，但这一格的样本没过门槛，或两个排序指标相对全样本
  给了**互相矛盾的方向**（视为样本噪声，见 `_fitness`）。
- 两者都不构成停用理由（CONTEXT.md：中性不构成停用理由），但原因不同，日报与
  单元 7 需要分开显示。
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Mapping, Sequence

from apps.regime import config
from apps.regime.quant import PRIORITY, BaseRegime, label_series

#: 载荷版本。`metrics` 里的切片结论带它，形状变了才分得清新旧。
#:
#: 2：每格多一个 `merge_inputs`（逐日收益率曲线 / 逐笔样本），供单元 7 池化重算。
#: 进位不是为了标记「算过一次」，而是让 `is_stale` 把存量切片判成陈旧、由重算入口
#: 刷新一遍——**旧载荷里没有这两样，池化在它上面无论怎么算都是错的**（缺样本会被
#: 读成「样本薄」还是「没证据」取决于实现，两种都静默）。
#:
#: 3：`_fitness` 加了一道绝对水平门（分段年化必须为正才给 `fit`）并拆出
#: `relative_fitness`。**这不只是池化的事**——`slice._cell` 也用同一个 `_fitness`
#: 往每份载荷里写 `state`，所以存量载荷里那些 `fit` 是按旧判据算的。不升这一位，
#: `is_stale` 会说「不陈旧」、切片不重算，池化读到的仍是旧的 `state`，而池化那边的
#: 绝对门只能改它自己算出来的结论、改不了已经落进载荷的这一份。
SLICE_VERSION = 3

#: 年化换算用的天数。**自然日而非交易日**：24/7 市场没有收盘、没有交易日边界，
#: 与 CONTEXT.md 给成功率定的「分母是自然日」同一条口径。365 是个约定值（不追
#: 闰年），它只影响年化后的量级，不影响任何相对比较。
DAYS_PER_YEAR = 365.0

#: 置信区间的 z 值（95%）。两处区间共用。
Z95 = 1.96

#: 四态 + `blanket`。取值即落库契约，改名等于让新旧 metrics 出现两套键。
STATE_FIT = "fit"  # 适用
STATE_UNFIT = "unfit"  # 不适用
STATE_NEUTRAL = "neutral"  # 中性（证据不足 / 样本噪声）——不构成停用理由
STATE_UNKNOWN = "unknown"  # 未知（该阶段在本窗口内没出现过）
STATE_BLANKET = "blanket"  # 保命档：不做适用性判断，与证据无关

#: `reason` 的取值。与状态分开：状态只有五个，原因要能说清「为什么落到这个状态」。
REASON_INSUFFICIENT = "insufficient_evidence"
REASON_DIRECTION_CONFLICT = "direction_conflict"
REASON_INCOMPARABLE = "incomparable"
REASON_HIGH_VOL_BLANKET = "high_vol_blanket_halt"
REASON_NO_REGIME_DAYS = "no_regime_days"
#: 相对比较过关（不比全样本差），但这一格**自己没赚钱**。2026-10-22 起适用多一道
#: 绝对水平门：`ema_trend_weekday` 年化 −171% 曾是 range 档唯一的 fit，它「不更差」
#: 只是因为别处更差。落中性而不是不适用——理由见 `_fitness` 的 docstring。
REASON_NOT_PROFITABLE = "not_profitable"
#: 兜底（原型）路径算出来是「适用」，但**这一条策略自己样本不够格**。借来的样本
#: 只能说明「这一类策略」在这个阶段如何，不能证明「这一条」适合——而 `fit` 正是被拿
#: 去做放行依据的那个值。降成中性。见 `pool._pool_cell` 的兜底分支。
REASON_ARCHETYPE_NOT_FIT = "archetype_evidence"

#: 上面两组的展示名。与 `quant._DISPLAY` 同一约定：**取值是落库契约**（改名等于让新旧
#: `metrics` 与新旧池化表出现两套键），中文只出现在展示层。放在词表的归属处而不是模型
#: 里，是为了让「加了一个状态却忘了给它展示名」在 `KeyError` 上立刻响——字典查不到就
#: 是查不到，不像 `choices` 那样会静默地不校验。
STATE_DISPLAY = {
    STATE_FIT: "适用",
    STATE_UNFIT: "不适用",
    STATE_NEUTRAL: "中性",
    STATE_UNKNOWN: "未知",
    STATE_BLANKET: "保命档",
}

REASON_DISPLAY = {
    REASON_INSUFFICIENT: "证据不足",
    REASON_DIRECTION_CONFLICT: "方向冲突",
    REASON_INCOMPARABLE: "不可比",
    REASON_HIGH_VOL_BLANKET: "高波动保命档",
    REASON_NO_REGIME_DAYS: "窗口内该阶段未出现",
    REASON_NOT_PROFITABLE: "该阶段自身未盈利",
    REASON_ARCHETYPE_NOT_FIT: "仅有同原型证据",
}

#: 比例与金额在 JSON 里的保留位数。切片是统计量，参与记账的是回测自己的
#: `equity_curve`——这里多留几位只是为了让两条记录能比对，不是精度承诺。
_ROUND = 6


@dataclass(frozen=True)
class SliceTrade:
    """切片要用的最小交易形状：**一笔已平仓的成交**。

    只有已平仓的交易有 `exit_time` 与已实现 `pnl`，而两者正是 pro-rata 的两个输入。
    未平仓的行（`trade_type` 为 `open`/`add`）不该被构造成 `SliceTrade`——它们在
    `BacktestTrade` 里没有 pnl、没有退出时刻，强行喂进来只会让「持仓时长」变成
    一段凭空的区间。适配层必须把它们挡在外面并**计数**（见 `attribution` 的
    `open_trades_excluded`）：一笔从未平仓的交易静默消失，看起来与「这个阶段没有
    交易」一模一样。
    """

    entry_time: datetime
    exit_time: datetime
    pnl: float

    def __post_init__(self) -> None:
        if self.entry_time is None or self.exit_time is None:
            raise ValueError("SliceTrade 必须有 entry_time 与 exit_time")
        if self.exit_time < self.entry_time:
            raise ValueError(
                f"平仓时刻早于开仓时刻：{self.entry_time} → {self.exit_time}"
            )


# --------------------------------------------------------------------------- #
# 标签
# --------------------------------------------------------------------------- #


def build_tags(
    candles: Sequence[Mapping], params: config.JudgementConfig = config.JUDGEMENT
) -> dict[date, BaseRegime]:
    """「日期 → 阶段」映射：`label_series` 的投影，只留判得出的那些日子。

    判不出的日子**不进映射**（不是塞一个默认档位）：调用方用 `tags.get(day)` 取，
    取不到就是「那天没有阶段结论」，与「那天是箱体震荡」在字典里必须长得不一样。

    参数默认取统一配置面的当前值，与 `quant.label_series` 同源——历史标注与实时
    判定必须用同一套阈值，否则同一天会有两个阶段。
    """
    return {
        label.date: label.regime
        for label in label_series(candles, params)
        if label.judged
    }


def _entry_date(moment: datetime) -> date:
    """开仓时刻归属到哪一根日线。**本模块唯一一处时区口径**，理由见模块 docstring。"""
    return moment.astimezone(timezone.utc).date()


def _held_days(entry: datetime, exit_: datetime) -> list[date]:
    """持仓期覆盖的 UTC 自然日，闭区间，升序。至少一天。

    按**自然日**分摊而不是按小时：整条链路的粒度是日——标签由日线定义、判定日频、
    生效日频，「某个小时属于哪个阶段」在本机制里没有定义。按小时分摊会把一笔跨 4 天、
    首末各占几小时的交易算成首末几乎不承担盈亏，而它精确化的那个尺度在标签那一侧
    并不存在。日度分摊的代价是首末两天按整天计权，好处是明确、可复述，整笔分摊能用
    一条日度曲线逐字重现。
    """
    first = _entry_date(entry)
    last = _entry_date(exit_)
    return [first + timedelta(days=n) for n in range((last - first).days + 1)]


# --------------------------------------------------------------------------- #
# 分段指标
# --------------------------------------------------------------------------- #


def _annualized_pct(
    total_pnl: float, initial_capital: float, span_days: int
) -> float | None:
    """分段年化收益率（%）。

    分母用**回测的初始资金**而不是分段自己的起点权益：分段曲线是一份反事实
    （「只在这个阶段交易会怎样」），拿它的起点当分母会让一条前期亏过钱的策略在
    后期每个阶段都被高估。用同一个分母，分段之间、分段与全样本之间才可比。

    `initial_capital <= 0` 时没有分母，年化没有定义，返回 `None`。

    **单日分段（`span_days == 1`）照样年化**，不特判：按 Calmar 的定义这就是正确的外推
    （年化收益 ÷ 最大回撤），要压住它的噪声该压的是门槛而不是年化。而它无需压——只出现
    过一天的阶段必然只有一个自然月的覆盖，卡在 `min_months` 上落中性，**到不了**
    `_fitness` 那一步。给它加特例只会让「为什么这一格是中性」多出一个与门槛并行的理由。
    """
    if initial_capital <= 0 or span_days <= 0:
        return None
    return total_pnl / initial_capital * (DAYS_PER_YEAR / span_days) * 100.0


def _segment_metrics(daily: Mapping[date, float], initial_capital: float) -> dict:
    """按日 PnL 序列算出分段的回撤与 Calmar。

    取值方式与 `BacktestResult.max_drawdown_pct` 同形（回撤 = 距**运行峰值**的跌幅，
    分母是峰值），但曲线是**用逐笔 PnL 按日重建**的。全样本那一格也走这一个函数，
    所以分段与全样本的差异只来自「纳入了哪些日子」，不来自两套算法——否则「分段比
    全样本差」这个结论本身就无法归因。

    零回撤（这条策略在该阶段从没从峰值上下来过）时 Calmar 没有定义，返回 `None`：
    报一个 `inf` 会让它在下游的排序里永远排第一，而它其实只是「没测到回撤」。

    `span_days` 是**有现金流的日子**的首末之差，不是回测窗口的长度：分段曲线只是那些
    有 PnL 的日子的曲线，年化要外推的是「这段时间的收益率」。代价是它与窗口长度不同，
    所以 `full_sample` 走同一个函数、用同一套口径——分段与全样本的差异于是只来自
    「纳入了哪些日子」，而年化分母的差异本身也是「纳入了哪些日子」的一部分。
    """
    empty = {
        "total_pnl": 0.0,
        "span_days": 0,
        "annualized_return_pct": None,
        "max_drawdown_pct": None,
        "calmar": None,
    }
    if not daily:
        return empty

    days = sorted(daily)
    total = math.fsum(daily[d] for d in days)
    span_days = (days[-1] - days[0]).days + 1

    running = 0.0
    peak = float(initial_capital)
    max_dd = 0.0
    for day in days:
        running += daily[day]
        equity = initial_capital + running
        peak = max(peak, equity)
        if peak > 0:
            max_dd = max(max_dd, (peak - equity) / peak)

    annualized = _annualized_pct(total, initial_capital, span_days)
    calmar = None
    if annualized is not None and max_dd > 0:
        calmar = annualized / (max_dd * 100.0)

    return {
        "total_pnl": round(total, _ROUND),
        "span_days": span_days,
        "annualized_return_pct": None
        if annualized is None
        else round(annualized, _ROUND),
        "max_drawdown_pct": round(max_dd * 100.0, _ROUND),
        "calmar": None if calmar is None else round(calmar, _ROUND),
    }


def wilson_interval(successes: int, n: int, z: float = Z95) -> list[float] | None:
    """胜率的 Wilson 区间。

    选 Wilson 而不是正态近似：它不依赖 `p` 远离 0/1，也不会算出负数或大于 1 的
    上下界，而小样本（稀有阶段的缩放门槛会把它放到个位数）恰恰是正态近似最不可靠
    的地方——样本少正是这里唯一的场景。闭式，不需要 resampling。
    """
    if n <= 0:
        return None
    p = successes / n
    denom = 1.0 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return [
        round(max(0.0, center - half), _ROUND),
        round(min(1.0, center + half), _ROUND),
    ]


def mean_pnl_interval(values: Sequence[float], z: float = Z95) -> list[float] | None:
    """单笔 PnL 均值的正态近似区间。`n < 2` 时没有离散度可估，返回 `None`。

    与 Wilson 区间并列输出：胜率区间回答「赢面有多大」，这个回答「赢多少、亏多少」。
    Calmar 自己的置信区间需要重采样（逐日 PnL 是自相关的，`bootstrap` 也未必诚实），
    而 CONTEXT.md 要这个区间是为了**让门槛被缩放的那些格子的证据薄弱可见**，
    单笔层面的两个区间足以做到，且不必引入一个新的重采样口径。
    """
    if len(values) < 2:
        return None
    mean = statistics.fmean(values)
    stdev = statistics.stdev(values)
    se = stdev / math.sqrt(len(values))
    return [round(mean - z * se, _ROUND), round(mean + z * se, _ROUND)]


# --------------------------------------------------------------------------- #
# 证据门槛
# --------------------------------------------------------------------------- #


def scaled_min_trades(
    day_share: float, params: config.EvidenceConfig = config.EVIDENCE
) -> int:
    """稀有阶段的笔数门槛：按该阶段占天数的比例缩放。

    两条护栏，缺一不可：

    - **只降不升**（`min`）：占天数超过满窗口的阶段（口径上不会出现，出现了也只
      说明统计窗口算错了）不该把门槛抬高到 `min_trades` 以上。
    - **下界为 1**：`day_share` 极小时 `round` 会给出 0，而「0 笔交易即过门槛」
      等于这一格根本不需要证据——门槛就从刹车变成了不存在。

    `min_months` **不参与缩放**（调用方 `_cell` 直接比对），这是缩放不会塌掉的关键：
    一个只出现过三天的稀有阶段，即使笔数门槛被缩到 1，也永远凑不满 3 个自然月的
    覆盖，仍归中性。CONTEXT.md 要求的「必须同时满足 min_months」正是这道保险。
    """
    return min(
        params.min_trades, max(1, round(params.min_trades * max(0.0, day_share)))
    )


def covered_months(dates: Sequence[date]) -> int:
    """覆盖月数：**不同的自然月个数**，不是首末日期之差。

    取严的那个：30 笔交易挤在同一个月里，跨度可能不足 30 天却确实「跨了两个月」
    （月初到月末），而首末之差会把「集中在两个月里反复交易」读成覆盖很广。
    逐月去重则要求样本**铺开在至少 3 个月份上**，与「样本先做厚」的意图一致。
    """
    return len({(d.year, d.month) for d in dates})


# --------------------------------------------------------------------------- #
# 适用性
# --------------------------------------------------------------------------- #


def _fitness(cell: dict, full: dict) -> tuple[str, str]:
    """把一格的指标与全样本比对，得出适用 / 不适用 / 中性。

    CONTEXT.md 只写了两件事：排序指标是**分段最大回撤 + 分段 Calmar**，以及
    「分段结论与全样本方向矛盾时视为样本噪声」。这里的读法是：两个指标各自与
    全样本比一次，**两个都说「不更差」才是适用，两个都说「更差」才是不适用，
    一个更好一个更差就是方向矛盾**——落中性。

    ## 只有相对比较是不够的（2026-10-22 补的角度门槛）

    上面那套是**纯相对**判据：一格只要「比自己全样本不太差」就是适用。它单独成立时
    会放过一种荒谬结论——现网实测到的那个：

        `ema_trend_weekday` 在 `range` 档：年化 **−171.4%**、Calmar −1.50、
        分段回撤 114.5%（全样本 273.4%）

    两个指标都比全样本「不更差」（100 多比 200 多好），于是判 **适用**。可它不是
    「在这个阶段能赚钱」，而是「在这个阶段亏得比别处少」。把一条年化 −171% 的策略
    标成「震荡期适用」，然后拿它去放行开仓，是把相对优势当成了绝对可行性。

    所以适用多了一道**绝对水平门**：分段年化收益必须为正。`full` 那一侧不看——
    全样本亏损是常态（回测没调好参数），只要这一段自己赚钱，就说得出「这段能跑」。

    这道门只拦 `fit`，**不制造 `unfit`**：不达标落中性。理由是 `unfit` 的语义是
    「经比较显著更差，建议停用」，而「这一段本身没赚钱」既可能是策略不行、也可能是
    这一段样本太少（分位没攒满、窗口太短让曲线形状主导）。把它当停用理由会让机制
    比证据说的更有把握，而 `neutral` 的语义正是「先用不上它」——不构成停用理由，
    也不会被读成放行。

    **`calmar` 与 `max_drawdown_pct` 那一套判据原样保留**：相对比较回答的是
    「这个阶段是不是它的场子」，绝对门回答的是「它在这里到底赚不赚钱」，两件事
    都要。去掉任何一个都会退回到上面那个荒谬结论或它的镜像。

    **形状一样但两个都无法算**（全样本零回撤、该格零回撤）时 `calmar` 双方都是
    `None`：那不是「不更差」，是「无从比较」，落中性并写明 `incomparable`——
    把它当适用会让一个从没测到回撤的分段得到一个不该有的结论。
    """
    # 相对比较那一段**只有一份实现**（`relative_fitness`），冲突判定也调它。
    # 在这里再抄一遍比较逻辑会让「同一份数据，state 说 fit、conflict 说方向不同」
    # 这种自相矛盾的证据同时出现在一条记录里。
    state, reason = relative_fitness(cell, full)
    if state == STATE_FIT and not _profitable(cell):
        # 相对比较过关，但这一段自己没赚钱。见 docstring 的「绝对水平门」。
        return STATE_NEUTRAL, REASON_NOT_PROFITABLE
    return state, reason


def relative_fitness(cell: dict, full: dict) -> tuple[str, str]:
    """**只有相对比较**的那一半判定：不比全样本差 ⇒ `fit`，两个都更差 ⇒ `unfit`。

    拆出来是为了让「跨品种冲突」的判定能单独问相对方向（`pool._conflict`）。冲突的
    语义是「合并后的结论与某个品种自己的结论方向相反」，它问的是**相对**关系；绝对
    水平门（`_fitness` 里那道 `_profitable`）与冲突无关——一条策略在所有品种上都亏钱
    时，它「不比全样本差」仍然成立，而那不是冲突。

    把两者混成一个函数会让绝对门静默地关掉冲突判定：`_fitness` 把 fit 降成中性之后
    `_direction` 返回 0，`_conflict` 直接返回 `None`，于是「两个品种方向相反、都过
    门槛」这件该让人看一眼的事不再进待复核。那正是这个拆分要挡的回归。

    **不要在两个地方各自实现一遍比较。** 这里的比较逻辑与 `_fitness` 的前半段必须
    是同一条判据，否则「同一份数据，`state` 说 fit、`conflict` 说方向不同」这种自相
    矛盾的证据会同时出现在一条记录里。
    """
    cell_dd = cell["max_drawdown_pct"]
    full_dd = full["max_drawdown_pct"]
    cell_calmar = cell["calmar"]
    full_calmar = full["calmar"]

    if cell_dd is None or full_dd is None:
        return STATE_NEUTRAL, REASON_INCOMPARABLE

    dd_not_worse = cell_dd <= full_dd

    if cell_calmar is None and cell_dd == 0.0:
        calmar_not_worse: bool | None = True
    elif cell_calmar is None or full_calmar is None:
        calmar_not_worse = None
    else:
        calmar_not_worse = cell_calmar >= full_calmar

    if calmar_not_worse is None:
        return STATE_NEUTRAL, REASON_INCOMPARABLE
    if calmar_not_worse and dd_not_worse:
        return STATE_FIT, ""
    if not calmar_not_worse and not dd_not_worse:
        return STATE_UNFIT, ""
    return STATE_NEUTRAL, REASON_DIRECTION_CONFLICT


def _profitable(cell: dict) -> bool:
    """这一格的**绝对**年化收益是否为正。算不出来时算「不达标」。

    `annualized_return_pct` 是 `None` 表示这段没有可外推的收益区间（没有现金流日子、
    或 `span_days == 0`）——那是「不知道」，不是「赚了」。`_fitness` 只在两种口径都
    算得出来时才走到这里，但把 `None` 读成「不达标」是 fail-closed 的那一侧，而这条
    判据的全部意义就是不让「不知道」冒充「可行」。

    **不设阈值**（不要求 > 5% 之类）：任何阈值都要挑一个数，而挑数的依据在这个仓库里
    不存在——`calmar` / 回撤那两维已经承担了「有多好」的排序，这里只需要拦住「在亏」。
    """
    annualized = cell.get("annualized_return_pct")
    if annualized is None:
        return False
    return annualized > 0


# --------------------------------------------------------------------------- #
# 池化输入
# --------------------------------------------------------------------------- #


def _merge_inputs(
    daily: Mapping[date, float],
    entry_dates: Sequence[date],
    pnls: Sequence[float],
    initial_capital: float,
) -> dict | None:
    """单元 7 池化要重算的那些量的**样本**（不是它们的结论）。见模块 docstring。

    逐日曲线按日期排序落库（`dict` 的插入序于是与构造顺序无关），逐笔样本保持成交
    传入的顺序——同一批成交必须给出同一份载荷，否则 `sha256` 之类的比对会随机地不
    相等。

    `entry_dates` 与 `pnls` 由 `slice_backtest` 在同一处成对追加，长度必然相同；
    `zip(..., strict=True)` 把这个隐式不变量变成一句会响的断言——错位一格的样本会
    给出「笔数与胜率都对、只有归属月错」的载荷，那种错误在报告里看不出来。

    空样本给的是**空容器**而不是 `None`：「这一格没有交易」与「这一格没算」必须是
    两件事，而 `None` 会把它们合成一件。

    **`initial_capital <= 0` 时返回 `None`**：归一化没有分母，这份回测给不出与别人
    同量纲的样本。这里不替它挑一个分母：

    - 挑 `1.0` 会让它的曲线变成**金额**（`Decimal(20,2)` 那两位小数下动辄上万），
      在池化里把每一份正常归一化的样本淹掉——一个看起来完全正常的池化结论，由一份
      没人知道有问题的回测决定。
    - 直接 `ZeroDivisionError` 会让这一个结果把**整批**重算拖成毒丸：切片任务收齐
      失败后 `retry`（3 次、递增退避），每次重试都要把已经算好的那些重算一遍，而
      失败的永远只有它一个。

    所以它落成 `None` ——「这份回测的池化样本不可用」，与空容器的「这一格没有交易」
    分开。池化那边必须把它**计数**出来（「有几个回测的样本不可用」是结论的一部分，
    与 `attribution.unjudged`、`open_trades_excluded` 是同一条纪律）。

    顺带说明为什么本函数是这条纪律的落点而不是 `_segment_metrics`：那一格自己的
    指标在 `initial_capital <= 0` 下照样算得出（`_annualized_pct` 返回 `None`、
    回撤退化成 0），是**结论**层面的退化，与「样本不可用」无关。切片的结论照旧产出，
    只是这一份不参与池化。
    """
    if not initial_capital or initial_capital <= 0:
        return None
    capital = float(initial_capital)
    return {
        "daily_return": {
            day.isoformat(): round(daily[day] / capital, _ROUND)
            for day in sorted(daily)
        },
        "trades": [
            [entry.isoformat(), round(pnl / capital, _ROUND)]
            for entry, pnl in zip(entry_dates, pnls, strict=True)
        ],
    }


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #


def _cell(
    regime: BaseRegime,
    daily: Mapping[date, float],
    entry_dates: Sequence[date],
    pnls: Sequence[float],
    regime_days: int,
    total_days: int,
    full: dict,
    initial_capital: float,
    params: config.EvidenceConfig,
) -> dict:
    """一格（策略 × 阶段）的全部证据。**每一格都产出**，包括空的那几格。

    空格子必须存在，理由与「判不出来就说判不出来」是同一条：缺失的格子在日报里
    会表现为「这一行没有」，而它有两种完全不同的意思（这个阶段没出现过 / 这个阶段
    出现了但没交易）。四格齐全 + `state` + `reason` 让两种意思各自可读。

    每格除了结论与门槛，还带一份 `merge_inputs`（池化要用的样本，见模块 docstring）
    ——**空格子也带**（空的那份），这样「这一格没交易」在池化那里与「这份载荷是旧版本、
    根本没有这个键」仍然长得不一样。

    三种「没有样本」在这里是三个不同的值，别把它们读成同一个：空容器是「这一格没有
    交易」，`None` 是「这份回测本金 ≤ 0，样本不可用」（见 `_merge_inputs`），而**键
    不存在**是「这份载荷是版本 1 的，那时还没有这一节」。
    """
    metrics = _segment_metrics(daily, initial_capital)
    months = covered_months(entry_dates)
    wins = sum(1 for pnl in pnls if pnl > 0)
    day_share = (regime_days / total_days) if total_days else 0.0
    threshold = scaled_min_trades(day_share, params)
    enough = len(entry_dates) >= threshold and months >= params.min_months

    cell = {
        "state": STATE_UNKNOWN,
        "reason": REASON_NO_REGIME_DAYS,
        # 数字全部照算照落，即使状态是未知或中性——「为什么这一格没结论」要能
        # 用数字回答，而不是靠读者相信。`None` 只出现在 `span_days` 为 0 时。
        "trades": len(entry_dates),
        "months": months,
        "win_rate": None,
        "win_rate_ci95": None,
        "pnl_mean_ci95": None,
        "threshold": {
            "min_trades": threshold,
            "min_months": params.min_months,
            "day_share": round(day_share, _ROUND),
            "regime_days": regime_days,
            "scaled": threshold < params.min_trades,
            "met": enough,
        },
        **metrics,
        # 结论是从哪来的。排在结论后面是刻意的：读载荷的人先看到「这格是什么」与
        # 「门槛是多少」，再看到支撑它的样本。
        "merge_inputs": _merge_inputs(daily, entry_dates, pnls, initial_capital),
    }
    if pnls:
        cell["win_rate"] = round(wins / len(pnls), _ROUND)
        cell["win_rate_ci95"] = wilson_interval(wins, len(pnls))
        cell["pnl_mean_ci95"] = mean_pnl_interval(pnls)

    if regime_days == 0:
        # 该阶段在本窗口内一天都没出现过。再多样本也回答不了，与门槛无关。
        return cell

    if regime is BaseRegime.HIGH_VOL:
        # 保命档：不做适用性判断。数字照留（它们仍然说明这段行情里发生了什么），
        # 但状态不是 fit/unfit——停用与证据无关，在这里推不出停用决策。
        cell["state"] = STATE_BLANKET
        cell["reason"] = REASON_HIGH_VOL_BLANKET
        return cell

    if not enough:
        cell["state"] = STATE_NEUTRAL
        cell["reason"] = REASON_INSUFFICIENT
        return cell

    state, reason = _fitness(cell, full)
    cell["state"] = state
    cell["reason"] = reason
    return cell


def slice_backtest(
    trades: Sequence[SliceTrade],
    tags: Mapping[date, BaseRegime],
    *,
    initial_capital: float,
    window: tuple[date, date],
    params: config.EvidenceConfig = config.EVIDENCE,
) -> dict:
    """把一次回测的已平仓成交切成「策略 × 阶段」的证据。

    Args:
        trades: **只含已平仓的**成交（见 `SliceTrade`）。未平仓的行由适配层挡掉并
            计数，本函数看不到它们，所以 `attribution.open_trades_excluded` 由调用
            方补进去。
        tags: 「日期 → 阶段」映射（`build_tags`），须覆盖到窗口起点**之前**的预热。
        initial_capital: 回测初始资金，全部分段共用同一个分母（见 `_annualized_pct`）。
        window: 回测的起止自然日，用来数「该阶段占了几天」。
        params: 证据门槛，默认取统一配置面的当前值。

    Returns:
        可直接存进 `BacktestResult.metrics["regime_slice"]` 的载荷（不含
        `computed_at` / 参数快照 / 标签指纹，那三样由单元 6ii 盖上去）。

    载荷是**自足的**：每格带 `merge_inputs`（池化重算要的样本），而全样本那条曲线
    等于各格 `merge_inputs["daily_return"]` 的逐日之和——所以单元 7 只靠这些载荷就能
    拼出池化样本，不必回头读 `BacktestTrade`，也不必重算标签（见模块 docstring）。
    那条恒等式成立是因为上面 `daily[day]` 与 `per_regime_daily[regime][day]` 加的是
    同一个 `share`，不是因为两份都被算对了。

    门槛那一组数落在 `evidence_threshold` 键上，**不叫 `params`**：单元 6ii 会往同一份
    载荷里盖一个 `config_snapshot`，而它的 `judgement` 组里也有一个 `params`（量化判定的
    那套阈值）。两个不同的东西同名，读者就必须靠位置猜自己看的是哪一个。

    `trades` 里若混入持仓期内的日子**没有标签**的成交（窗口之前的预热不足、日线
    缺日），那笔交易的那几天 PnL 会落进 `pnl_unattributed` 而不是凭空归给某一格。
    **不重分配**：把落空的份额摊给别的阶段，等于让每一格都说「我这里有这些钱」，
    而钱到底在哪一段时间赚的不再可查。
    """
    start, end = window
    if end < start:
        raise ValueError(f"回测窗口倒置：{start} → {end}")

    # 窗口内已判定的天数：分母（`day_share`）与「该阶段出现过没有」都从这里来。
    window_tags = {d: regime for d, regime in tags.items() if start <= d <= end}
    total_days = len(window_tags)
    regime_days: dict[BaseRegime, int] = defaultdict(int)
    for regime in window_tags.values():
        regime_days[regime] += 1

    daily: dict[date, float] = defaultdict(float)
    per_regime_daily: dict[BaseRegime, dict[date, float]] = defaultdict(
        lambda: defaultdict(float)
    )
    per_regime_entries: dict[BaseRegime, list[date]] = defaultdict(list)
    per_regime_pnls: dict[BaseRegime, list[float]] = defaultdict(list)
    pnl_unattributed = 0.0
    attributed = 0
    unjudged = 0

    for trade in trades:
        entry_day = _entry_date(trade.entry_time)
        segments = _held_days(trade.entry_time, trade.exit_time)
        share = trade.pnl / len(segments)

        for day in segments:
            regime = tags.get(day)
            if regime is None:
                # 那一天的阶段判不出来（或压根没有日线）。它的份额无处可归——
                # 记账而不是静默丢弃，见本函数的 docstring。
                pnl_unattributed += share
                continue
            daily[day] += share
            per_regime_daily[regime][day] += share

        entry_regime = tags.get(entry_day)
        if entry_regime is None:
            # **笔数不入任何一格**，但它的 PnL 已经按上面那一段分给了持仓期内的
            # 各阶段。这正是「求准 / 求保守」不对称的落点：算钱要准，算笔数要保守
            # ——一笔连开仓时是什么行情都说不出的交易，不足以支撑任何一格的结论。
            unjudged += 1
        else:
            attributed += 1
            per_regime_entries[entry_regime].append(entry_day)
            per_regime_pnls[entry_regime].append(trade.pnl)

    full = _segment_metrics(daily, initial_capital)
    cells = {
        regime.value: _cell(
            regime=regime,
            daily=per_regime_daily.get(regime, {}),
            entry_dates=per_regime_entries.get(regime, ()),
            pnls=per_regime_pnls.get(regime, ()),
            regime_days=regime_days.get(regime, 0),
            total_days=total_days,
            full=full,
            initial_capital=initial_capital,
            params=params,
        )
        for regime in PRIORITY
    }

    return {
        "version": SLICE_VERSION,
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "evidence_threshold": {
            "min_trades": params.min_trades,
            "min_months": params.min_months,
        },
        "full_sample": full,
        "cells": cells,
        "attribution": {
            "closed_trades": len(trades),
            "attributed": attributed,
            "unjudged": unjudged,
            "pnl_unattributed": round(pnl_unattributed, _ROUND),
            "regime_days": {
                regime.value: regime_days.get(regime, 0) for regime in PRIORITY
            },
            "window_days": total_days,
            # 由适配层补：本函数拿不到未平仓的行，见 `SliceTrade` 的 docstring。
            "open_trades_excluded": None,
        },
    }
