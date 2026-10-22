"""池化：把多份切片合并成（策略 × 阶段）的适用性结论。

## 它为什么不是「把各格结论投个票」

CONTEXT.md 把池化定为**加权合并**——把多份切片的成交合起来，按合并后的样本重算。
投票与平均都被否掉了，各有具体理由：

- **投票**会让一份 5 笔的切片与一份 100 笔的切片等权。样本量恰恰是这份设计唯一
  承认的可信度来源（「门槛是唯一天然刹车」），投出去的票把它抹掉了。
- **平均回撤**（各份切片回撤取平均）算出来的数**不是任何一个组合的回撤**。这与
  ADR 0002 否掉「影响分 73」是同一条：一个没有对应实体的合成数，没人能验算它。

所以这里的做法是：把各份切片的**逐日归一化收益序列**按日相加，得到一条真实的合并
轨迹，再照常算回撤与 Calmar。归一化口径是「这些回测等额并行跑」——这正是
`slice._merge_inputs` 除以 `initial_capital` 的原因，也是为了这条相加有意义。

合并样本的**全样本对照基准**同样是把各家的全样本曲线相加，不是取某一家：分段与
全样本必须覆盖同一批策略，否则「分段比全样本差」里混进了「这家的全样本与那家的
分段比」这种不可比的比较。

## 门槛：为什么这里没有 `day_share`

单元 6 的 `scaled_min_trades` 按「该阶段占本回测窗口的天数比例」缩放笔数门槛，因为
一格样本稀薄可能只是那个阶段稀有。**池化这一层没有这个量**：合并样本横跨多个回测、
多个品种、多个窗口，没有单一的 `total_days` 可做分母。硬凑一个（比如各窗口并集）
等于替「稀有」这件事拍一个数字，而它会被下游当成事实。

所以池化直接用**未缩放的** `min_trades` 与 `min_months`，两条**同时**满足才算够。
两件事要说清：

1. 这不是「单元 6 的缩放被悄悄丢了」——缩放是每格自己的事，池化只是不接受它的
   输入（没有 `day_share`），不是忽略了它。
2. 门槛**不能只看乘积**。`min_trades × min_months`（30 × 3 = 90）是一个可读的
   规模感：90 笔铺在 3 个月上，是「先做厚样本」这句话的量化形态。但若把它写成
   `trades * months >= 90`，那么「300 笔挤在一个月里」也会过——`min_months` 那道
   保险（`scaled_min_trades` 的 docstring 点名它是缩放不会塌掉的关键）会被自己的
   乘积绕过去。所以判据是两条并列，不是一条乘积。

## 与单元 6 的判定顺序逐条对齐

`_pooled_state` 的四步与 `slice._cell` 完全同序（未出现 → 保命档 → 门槛 → 比对），
理由不是对称美学：日报要能用同一句话解释「为什么这一格是中性」。两层成因不同的话，
读的人得先判断自己在看哪一层才知道那句话成不成立。

## 冲突：阻塞建议，不阻塞结论

Q3 的读法：**方向相反且两侧都过门槛**才算「明显冲突」。冲突格子的池化结论照常产出、
照常被引用，只是那一格**不产生停用建议**并标成 `needs_review`。所以 `needs_review`
与 `state` 是两个独立的东西——并进 `state` 等于说「这格没有结论」，而它明明有。

## 原型兜底

样本不足时改用「策略原型 × 阶段」的样本。原型的归一化走**代码内关键词映射表 +
数据库人工覆盖表**，不用 LLM（机制本体是确定性服务）。映射表是**有序**的：按声明
顺序取第一个命中的原型，所以顺序是判据的一部分，不是排版。命中留痕（原文字 + 命中词）
一起进证据，未归类的策略单独计数——它是日报第④段的输入，不是一条被咽下去的噪声。

本模块**不做数据库访问**：覆盖表读成 `{策略 id: 原型}` 由调用方传进来（`resolve_archetype`
的 `override` 参数），这样「映射表怎么判」与「哪条策略该覆盖」能各自被单独测。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable, Mapping, Sequence

from apps.regime import config
from apps.regime.quant import PRIORITY, BaseRegime
from apps.regime.slice import (
    REASON_ARCHETYPE_NOT_FIT,
    REASON_HIGH_VOL_BLANKET,
    REASON_INSUFFICIENT,
    REASON_NO_REGIME_DAYS,
    STATE_BLANKET,
    STATE_FIT,
    STATE_NEUTRAL,
    STATE_UNFIT,
    STATE_UNKNOWN,
    _fitness,
    relative_fitness,
    _segment_metrics,
    covered_months,
)

#: 池化载荷的形状版本。切片有 `SLICE_VERSION`，池化有自己的——两者进位的原因不同：
#: 前者是「输入样本长什么样」，后者是「合并口径怎么算」。合成一个版本号会让「重算了
#: 切片」与「改了合并口径」在库里长得一样，而前者需要重算切片、后者只需要重算池化。
#:
#: 2：原型归属进入输入指纹（`pool_rebuild.input_fingerprint`）。第 1 代池化是在
#: 注册表为空的状态下建的，47 条策略因此全落「未归类」并共用一条策略的样本；修好
#: 注册表后必须重算出新代。**这里进位是必须的，不是顺手**——指纹虽然已把 `archetypes`
#: 并入，但「池化算法版本」是另一根轴：它让「用旧口径算的格子」与「用新口径算的格子」
#: 在库里分得开，而不是靠指纹碰巧不同。
#:
#: 3：`normalize_archetype` 改为只扫 `策略类型：` 那一行（无该行才退回整篇）。归错类的
#: 策略会换组，兜底借到的样本随之改变——第 2 代那 244 格里有一大批是按「整篇扫」算的，
#: 语义不同，不能当同构结果比差异。
POOL_VERSION = 3

#: 一份切片被排除在池化之外的原因。**按原因分开计数**，不记一个「跳过了几个」：
#: 「三个是本金为 0」与「三个是版本太旧」需要两个完全不同的动作（前者要人去查回测
#: 参数，后者跑一次重算入口就好），混成一个数就没法决定该做哪个。
EXCLUDED_NO_MERGE_INPUTS = "no_merge_inputs"  # 版本 1 的载荷 / 形状不符，没有样本
EXCLUDED_UNUSABLE_CAPITAL = "unusable_capital"  # 本金 ≤ 0，归一化没有分母
EXCLUDED_NO_PAYLOAD = "no_payload"  # `metrics` 里压根没有切片

#: 一节 `merge_inputs` 必须具备的两个键（`slice._merge_inputs` 恒同时给这两个）。
#: 拿它当**存在性**判据而不是「读到空就当没有」：后者会把一节的缺失读成「这一格
#: 没有交易」，笔数看起来是完整的。
_MERGE_INPUT_KEYS = frozenset({"daily_return", "trades"})

#: 原型归一化里「一个都没命中」的取值。它必须是一个**真的取值**而不是 `None`：
#: 未归类是要计数、要进日报的结论，而 `None` 在链路上会被 `or` 之类的写法悄悄吞掉。
ARCHETYPE_UNCLASSIFIED = "未归类"

#: 原型关键词映射表。**有序**，按声明顺序取第一个命中（Q5）。
#:
#: 顺序为什么是判据的一部分：现网真实策略的描述是自由文本，`rsi_cross.py` 的
#: 「RSI 超卖区金叉买入，超买区死叉卖出」同时命中「金叉/死叉」（趋势）与「超卖/超买」
#: （均值回归）。两类都对，但对这条策略只有一类是对的——它是振荡器在极值区的反转，
#: 不是均线交叉。把「均值回归」排在前面就是对这个歧义的判定，而判定会被留痕
#: （命中词进证据），也有覆盖表可以纠正。
#:
#: 关键词一律小写做子串匹配，所以这里写的都必须是**不会被无关词包含**的片段：
#: 因此没有裸的 `ma`（会被 `market`、`mark` 命中），只有 `macd`、`均线`、`移动平均`。
#: 五个原型的名单取自 `apps/strategy_engine/base.py` 的 `description_template`
#: （`{均值回归/趋势跟踪/突破/动量/套利}`）——那是代码里已有的词表，不是这里另起的。
ARCHETYPE_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        # 排最前：套利描述里也常出现「价差」「偏离」，那些词在别的类里是通用的，
        # 但「套利」「基差」「资金费率」不会出现在其它类的描述里——先摘走不会误伤。
        "套利",
        ("套利", "基差", "资金费率", "三角", "跨期", "跨市场"),
    ),
    (
        "均值回归",
        (
            "均值回归",
            "回归",
            "超卖",
            "超买",
            "反转",
            "反弹",
            "震荡",
            "布林",
            "网格",
            "rsi",
        ),
    ),
    (
        "突破",
        ("突破", "新高", "新低", "海龟", "放量", "上轨", "下轨"),
    ),
    (
        "动量",
        ("动量", "加速", "强势", "斜率", "涨幅", "速率"),
    ),
    (
        "趋势跟踪",
        ("趋势", "均线", "移动平均", "ema", "macd", "金叉", "死叉", "顺势", "跟踪"),
    ),
)

#: `ArchetypeMatch.source` 的取值词表（同一约定：取值是落库契约，中文只在展示层）。
ARCHETYPE_SOURCE_OVERRIDE = "override"
ARCHETYPE_SOURCE_KEYWORD = "keyword"
ARCHETYPE_SOURCE_UNCLASSIFIED = "unclassified"

#: 池化结论的来源词表，取值必须与 `models.PoolSource` 同集合。
#:
#: 这里另写一份常量而不是 import 那个枚举，是因为 `models` **不能** import 本模块
#: （本模块 import `slice`，`slice` 经 `strategy_engine.indicators` 把
#: `signal_monitor` 拖进来——那正是 `RegimePoolCell.state` 用裸 `CharField` 的同一个
#: 理由）。所以同一份取值在词表、模型、落库三处各写一遍，靠 `test_pool.py` 把
#: 「两处同集合」钉住，与 `state`/`reason` 的处理一致。
POOL_SOURCE_STRATEGY = "strategy"
POOL_SOURCE_ARCHETYPE = "archetype"


#: `description` 里声明策略类型的那一行的前缀。与
#: `strategy_engine.registry.validate_description` 的四字段模板同源——那个模板
#: 要求每条策略的 description 必须含这四个字段，所以「这一行一定在」不是假定，
#: 是注册期已经钉住的契约（`test_pool.py` 里另有一组用例把两边对齐）。
ARCHETYPE_DECLARATION_PREFIX = "策略类型："


def _declared_type_line(raw_text: str) -> str | None:
    """`description` 里「策略类型：」那一行；没有则 `None`。

    **只取第一个这种行。** 描述模板把它们排在最前，而自由文本里再出现第二次
    「策略类型：」只可能是别的东西（比如某段注释引用了模板），拿最后一处会读到
    一个不是声明的位置。
    """
    for line in (raw_text or "").splitlines():
        if line.strip().startswith(ARCHETYPE_DECLARATION_PREFIX):
            return line
    return None


def normalize_archetype(raw_text: str) -> tuple[str, str]:
    """把一段策略描述归到原型，返回 `(原型, 命中词)`；未命中给 `(ARCHETYPE_UNCLASSIFIED, "")`。

    纯函数、只看文本。与覆盖表分开是因为两者要各自被钉住：映射表判错（`rsi_cross`
    被归到趋势跟踪）与覆盖表没生效（人工改过还是走了映射表）是两个不同的故障。

    ## 先扫「策略类型：」那一行，扫不到才退回整篇

    整篇扫子串会把**不声明类型的地方**也当成声明，现网实测 18 条有描述的策略里
    5 条归错，而且方向一致地错：错向「均值回归」。

    | 策略 | 声明 | 整篇扫出来 | 命中的词来自 |
    |---|---|---|---|
    | `EMACrossoverSimple` | 趋势跟踪 | 均值回归 | 「不适合**震荡**盘整」 |
    | `box_time_range_breakout_strategy` | 突破 | 均值回归 | 「**震荡**后启动的突破行情」 |
    | `mm_with_ofi_strategy` | 做市+动量 | 均值回归 | 「高流动性**震荡**市场」 |
    | `TickImbalanceV3` | Tick失衡动量 | 均值回归 | 「Tick Imbalance**回归**中性」 |
    | `volume_support_reclaim_strategy` | 突破反转 | 均值回归 | 「假突破反**转**」 |

    前三条是**否定句与背景描述**被当成了类型，后两条是出场逻辑里的词。危害不止
    标签难看：兜底是**按原型分组借样本**的，一条策略归错类会让整组兜底结论都吃到
    不属于它的样本，而「震荡」这个词在各种「不适合震荡」的句子里到处都是——于是
    均值回归组变成所有组的垃圾桶（实测 10 条 / 755 份样本，最胖的一组）。

    退回到整篇是给不合模板的文本留的活路：`strategies/rsi_cross.py` 那句
    「RSI 超卖区金叉买入，超买区死叉卖出」没有「策略类型：」行，而它是本模块
    **有序映射**那条设计（均值回归必须排在趋势跟踪前面）唯一的真实依据，不能
    因为改用行扫描就把它判成未归类。注册期模板要求「策略类型：」存在，所以现网
    走向这一支的只会有历史文本与测试夹具。
    """
    line = _declared_type_line(raw_text)
    text = (line if line is not None else (raw_text or "")).lower()
    for archetype, keywords in ARCHETYPE_KEYWORDS:
        for word in keywords:
            if word in text:
                return archetype, word
    # 声明行命中不了（比如「策略类型：自定义」）时**不退回整篇**：声明行是作者写下的
    # 判断，其余行是背景描述，让背景描述覆盖作者自己的声明正是这次要修的那件事。
    # 整篇扫这一支只服务「压根没有声明行」的历史文本，它的回落已经体现在上行。
    return ARCHETYPE_UNCLASSIFIED, ""


@dataclass(frozen=True)
class ArchetypeMatch:
    """一条策略最终归属的原型，连同它是怎么被定下来的。

    四个字段缺一不可：`archetype` 是结论，其余三个是**依据**。池化兜底的结论会被人
    质问「凭什么说这条策略属于这一类」，答不出依据的归类就是又一次「影响分 73」
    ——一个没人能验算的数。
    """

    archetype: str
    raw_text: str = ""
    matched_word: str = ""
    #: `override` / `keyword` / `unclassified`。覆盖表 > 映射表（Q5），所以来源必须
    #: 能分辨：否则「人工纠正过」与「机器碰巧猜对」在事后长得一模一样，而前者是
    #: 下次描述改动后仍然该生效的那一个。
    source: str = ARCHETYPE_SOURCE_KEYWORD


def resolve_archetype(raw_text: str, override: str | None = None) -> ArchetypeMatch:
    """覆盖表优先，其次映射表（Q5）。`override` 由调用方从 `ArchetypeOverride` 读出。

    级联一步都不省：覆盖表命中就不再跑映射表（否则「人工纠正了但还是按机器判的」
    会读成覆盖表没生效，而它其实只是被后面的映射表盖掉了）。覆盖表的取值**不再校验**
    是否在五个原型里——它是数据不是阈值，取值可以比映射表宽（比如人工细分出「网格」
    这一档），本模块对它只做搬运与留痕。
    """
    if override:
        return ArchetypeMatch(
            archetype=override,
            raw_text=raw_text or "",
            matched_word="",
            source=ARCHETYPE_SOURCE_OVERRIDE,
        )
    archetype, matched = normalize_archetype(raw_text)
    return ArchetypeMatch(
        archetype=archetype,
        raw_text=raw_text or "",
        matched_word=matched,
        source=(
            ARCHETYPE_SOURCE_UNCLASSIFIED
            if archetype == ARCHETYPE_UNCLASSIFIED
            else ARCHETYPE_SOURCE_KEYWORD
        ),
    )


@dataclass(frozen=True)
class SliceSample:
    """一份切片对池化的贡献（已归一化）。

    只带池化真正要用的四样：每格的逐日收益、每格的逐笔样本、每格该阶段的天数、
    全样本逐日收益。品种、窗口、结果 id 都留着——它们进证据，是「依据哪几个回测、
    什么区间」的答案。
    """

    result_id: str
    strategy_id: int
    symbol: str
    window: tuple[date, date]
    cell_daily: Mapping[str, Mapping[date, float]]
    cell_trades: Mapping[str, Sequence[tuple[date, float]]]
    cell_regime_days: Mapping[str, int]
    full_daily: Mapping[date, float]


@dataclass
class SampleLoad:
    """`load_sample` 的结果：要么是一份可用样本，要么一条排除原因。"""

    sample: SliceSample | None = None
    excluded: str = ""


def load_sample(
    payload: Mapping[str, Any] | None,
    *,
    result_id: str,
    strategy_id: int,
    symbol: str,
) -> SampleLoad:
    """从一份**已落库的**切片载荷里取出池化样本。

    三种不可用各有各的原因，分开返回（见 `EXCLUDED_*`）：

    - 没有载荷 / 没有 `cells` → 这份回测没切过片。
    - `cells` 在但 `merge_inputs` **键不存在** → 版本 1 的载荷（那时还没有这一节）。
      它与「本金 ≤ 0」是两件事：前者跑一次重算入口就好，后者要人去查回测参数。
      形状不对的 `merge_inputs`（不是映射、或少了 `daily_return`/`trades` 任一个）也
      归这一条：产出方要么给 `None`、要么两个键齐全，所以「差一个键」只可能是载荷
      坏了。**不能按缺省空值读下去**——那会得到一份「这一格没有交易」的样本，笔数
      看起来是完整的，而实际是这一格的成交整个丢了。
    - `merge_inputs` 是 `None` → 本金 ≤ 0，归一化没有分母（`slice._merge_inputs`）。

    这一层是**整份可用或整份不用**：一格缺 `merge_inputs` 就排除整份，不做「有什么
    用什么」。半份样本合并出来的轨迹不属于任何东西，而它会带着一个看起来完整的
    `trades` 计数进证据——那种错比少用一份回测难发现得多。

    逐日曲线按 `date` 解析回来：JSON 里是 ISO 字符串，而合并要按日期排序相加——
    字符串序恰好与 ISO 日期的先后一致，但**排序与相加是两件事**，靠字符串序碰巧
    正确会让「以后换一种日期格式」变成一次静默的错序求和。
    """
    if not payload:
        return SampleLoad(excluded=EXCLUDED_NO_PAYLOAD)
    cells = payload.get("cells")
    if not isinstance(cells, Mapping) or not cells:
        return SampleLoad(excluded=EXCLUDED_NO_PAYLOAD)

    cell_daily: dict[str, dict[date, float]] = {}
    cell_trades: dict[str, list[tuple[date, float]]] = {}
    cell_regime_days: dict[str, int] = {}
    full_daily: dict[date, float] = defaultdict(float)

    for regime in PRIORITY:
        cell = cells.get(regime.value)
        if not isinstance(cell, Mapping) or "merge_inputs" not in cell:
            return SampleLoad(excluded=EXCLUDED_NO_MERGE_INPUTS)
        inputs = cell["merge_inputs"]
        if inputs is None:
            return SampleLoad(excluded=EXCLUDED_UNUSABLE_CAPITAL)
        if not isinstance(inputs, Mapping) or not _MERGE_INPUT_KEYS <= set(inputs):
            return SampleLoad(excluded=EXCLUDED_NO_MERGE_INPUTS)

        daily = {
            date.fromisoformat(day): float(value)
            for day, value in (inputs.get("daily_return") or {}).items()
        }
        cell_daily[regime.value] = daily
        cell_trades[regime.value] = [
            (date.fromisoformat(day), float(value))
            for day, value in (inputs.get("trades") or [])
        ]
        # 该阶段在这份回测的窗口里出现过几天（`_cell` 的 `threshold.regime_days`）。
        # 池化要它才能区分「这四格全空是因为窗口里就是没有这个阶段」与「阶段出现过
        # 但一份回测都没在那里开仓」——聚合层的 `merge_inputs` 只有成交日，看不到
        # 前者；而这两种空的结论完全不同（未知 vs 证据不足）。
        cell_regime_days[regime.value] = int(
            (cell.get("threshold") or {}).get("regime_days", 0)
        )
        # 全样本曲线 = 各格逐日之和（`slice_backtest` 的 docstring 里那条恒等式）。
        # 在这里现加而不是另存一份：另存一份就有了两个可能不一致的真相，而求和是
        # 那条恒等式本身，不是它的一个近似。
        for day, value in daily.items():
            full_daily[day] += value

    return SampleLoad(
        sample=SliceSample(
            result_id=result_id,
            strategy_id=strategy_id,
            symbol=symbol,
            window=_parse_window(payload.get("window")),
            cell_daily=cell_daily,
            cell_trades=cell_trades,
            cell_regime_days=cell_regime_days,
            full_daily=dict(full_daily),
        )
    )


def _parse_window(raw: Any) -> tuple[date, date]:
    """载荷里的窗口。缺失/畸形给 `(date.min, date.min)` 而不是抛：窗口只进证据
    （「什么区间」），拿不到它不该让整次池化失败——那会把一条记录缺失升级成一次
    全量重算失败，代价与收益完全不成比例。"""
    if isinstance(raw, Mapping):
        try:
            return (
                date.fromisoformat(str(raw.get("start"))),
                date.fromisoformat(str(raw.get("end"))),
            )
        except ValueError:
            pass
    return date.min, date.min


# --------------------------------------------------------------------------- #
# 合并与判定
# --------------------------------------------------------------------------- #

#: 合并轨迹的本金口径。归一化之后「等额并行跑」的组合本金就是 1，年化与回撤都以它
#: 为分母。写成常量而不是就地传个 `1.0`：这个数字有含义（它是 `slice._merge_inputs`
#: 除以每份回测本金的那件事在这里的另一半），就地写会看起来像随手填的占位。
_UNIT_CAPITAL = 1.0


def _sum_daily(samples: Iterable[Mapping[date, float]]) -> dict[date, float]:
    """逐日相加。等额并行跑的多份回测，合并组合的当日收益就是各家当日收益之和。"""
    merged: dict[date, float] = defaultdict(float)
    for daily in samples:
        for day, value in daily.items():
            merged[day] += value
    return dict(merged)


@dataclass(frozen=True)
class _Merged:
    """一批样本合并后的原始量。指标是**在这里**算的，不是从各份结论里拼的。"""

    metrics: dict
    full_metrics: dict
    trades: int
    months: int
    regime_days: int
    results: tuple[dict, ...]
    symbols: tuple[str, ...]
    params: config.EvidenceConfig

    @property
    def enough(self) -> bool:
        """两条门槛**并列**，不是乘积。见模块 docstring 里那段。"""
        return (
            self.trades >= self.params.min_trades
            and self.months >= self.params.min_months
        )


def _merge(
    samples: Sequence[SliceSample],
    regime: str,
    params: config.EvidenceConfig,
) -> _Merged:
    """把一批样本在 `regime` 这一格上合并，并算出合并轨迹的指标与它的全样本对照。

    对照基准永远是**这同一批样本**的全样本曲线之和。所以原型兜底那一档的基准自动
    也跟着变成「那几个同原型策略的全样本」，无需另一条分支：基准与分段永远取自同一
    批样本，这是使比较可比的那条不变量。
    """
    daily = _sum_daily(s.cell_daily.get(regime, {}) for s in samples)
    trades = [trade for s in samples for trade in s.cell_trades.get(regime, ())]
    entry_days = [day for day, _ in trades]
    return _Merged(
        metrics=_segment_metrics(daily, _UNIT_CAPITAL),
        full_metrics=_segment_metrics(
            _sum_daily(s.full_daily for s in samples), _UNIT_CAPITAL
        ),
        trades=len(entry_days),
        months=covered_months(entry_days),
        regime_days=sum(s.cell_regime_days.get(regime, 0) for s in samples),
        results=tuple(_summarise(s) for s in samples),
        symbols=tuple(sorted({s.symbol for s in samples})),
        params=params,
    )


def _summarise(sample: SliceSample) -> dict:
    """一份切片在证据里的摘要：哪几个回测、什么区间。CONTEXT.md 的三问里有两问在
    这里，「多少笔」在 `_evidence` 的 `trades` 上（合并后的总数，不是各家的）。"""
    return {
        "result_id": sample.result_id,
        "symbol": sample.symbol,
        "window": {
            "start": sample.window[0].isoformat(),
            "end": sample.window[1].isoformat(),
        },
    }


def _pooled_state(merged: _Merged, regime: BaseRegime) -> tuple[str, str]:
    """合并样本的状态。与 `slice._cell` 的判定顺序**逐条对齐**，只有门槛那一步不同
    （缩放 vs 不缩放，见模块 docstring）。

    顺序不是巧合：该阶段没出现先于保命档（`regime_days == 0` 时再多样本也回答不了），
    保命档先于门槛（保命与证据无关），门槛先于 `_fitness`（样本不够时两指标的比较
    没有意义）。两层同序，日报才能用同一句话解释「为什么这一格是中性」。
    """
    if merged.regime_days == 0:
        return STATE_UNKNOWN, REASON_NO_REGIME_DAYS
    if regime is BaseRegime.HIGH_VOL:
        return STATE_BLANKET, REASON_HIGH_VOL_BLANKET
    if not merged.enough:
        return STATE_NEUTRAL, REASON_INSUFFICIENT
    return _fitness(merged.metrics, merged.full_metrics)


def _direction(state: str) -> int:
    """结论的方向，只用于判「明显冲突」。中性/未知/保命档都是 0：它们不指向任何
    一边，与它们冲突无从谈起（「中性」本来就不构成停用理由，没有可冲突的动作）。"""
    if state == STATE_FIT:
        return 1
    if state == STATE_UNFIT:
        return -1
    return 0


def _conflict(
    pooled_state: str,
    samples: Sequence[SliceSample],
    regime: str,
    params: config.EvidenceConfig,
) -> dict | None:
    """找出「明显冲突」的品种（Q3）：方向相反**且**两侧都过门槛。

    一侧不过门槛就不是冲突，是样本噪声——CONTEXT.md 明写「分段结论与全样本方向
    矛盾时视为样本噪声」，而门槛就是「噪声」与「信号」的判据。把不过门槛的那一侧
    也算成冲突，会让每一条策略都因为某个只跑了 3 笔的品种而挂上待复核，待复核于是
    变成一个没人看的标记。

    只报**第一个**冲突品种（按品种名排序，取确定性顺序），不是全部：这个字典进证据
    是为了回答「为什么这格要人看一眼」，一个例子就够；而列出全部会让证据体积随品种
    数增长，也会让「有几个格子待复核」这种计数变得可疑。
    """
    pooled_dir = _direction(pooled_state)
    if pooled_dir == 0:
        return None

    by_symbol: dict[str, list[SliceSample]] = defaultdict(list)
    for sample in samples:
        by_symbol[sample.symbol].append(sample)
    if len(by_symbol) < 2:
        # 只有一个品种，「跨品种冲突」这个概念不成立。而且池化结论就是它的结论，
        # 自己与自己比必然一致。
        return None

    for symbol in sorted(by_symbol):
        own = _merge(by_symbol[symbol], regime, params)
        if not own.enough:
            continue
        own_state, _ = relative_fitness(own.metrics, own.full_metrics)
        if _direction(own_state) == -pooled_dir:
            return {
                "symbol": symbol,
                "symbol_state": own_state,
                "pooled_state": pooled_state,
                "symbol_trades": own.trades,
                "symbol_months": own.months,
                "symbol_results": list(own.results),
            }
    return None


def _evidence(
    merged: _Merged,
    *,
    extra: Mapping[str, Any] | None = None,
) -> dict:
    """证据：每个停用建议都要能回答「依据哪几个回测、多少笔、什么区间」。

    `threshold.met` 从 `merged.enough` 现算，不由调用方传：传进来就有第二个真相，
    而「门槛过了」这件事正是结论成立与否的判据，它不该有两处说法。
    """
    evidence = {
        "pool_version": POOL_VERSION,
        "results": list(merged.results),
        # 品种轴（CONTEXT.md：记录到 策略 × 品种 × 阶段，决策按 策略 × 阶段 池化）。
        # 存在证据里而不是另开一张表：池化结论不按品种拆，品种只是它覆盖面的说明。
        "symbols": list(merged.symbols),
        "trades": merged.trades,
        "months": merged.months,
        "regime_days": merged.regime_days,
        "threshold": {
            "min_trades": merged.params.min_trades,
            "min_months": merged.params.min_months,
            "met": merged.enough,
        },
        "metrics": merged.metrics,
        "full_sample": merged.full_metrics,
    }
    if extra:
        evidence.update(extra)
    return evidence


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #


@dataclass
class PooledCell:
    """一格的池化结论，字段与 `RegimePoolCell` 一一对应。"""

    strategy_id: int
    regime: str
    source: str
    state: str
    reason: str
    evidence: dict
    needs_review: bool = False


@dataclass
class PoolResult:
    """一次全量池化的产物。`cells` 与 `excluded` 一起构成重算记录的计数。"""

    cells: dict[tuple[int, str], PooledCell] = field(default_factory=dict)
    #: `{策略 id: 原因}` —— 被排除在池化之外的**切片**所属的策略及其原因。
    excluded: dict[int, str] = field(default_factory=dict)
    #: 样本不足、走到原型兜底、而原型也没归一化出来的策略 id（日报第④段的输入）。
    unclassified: set[int] = field(default_factory=set)

    @property
    def unclassified_strategy_ids(self) -> tuple[int, ...]:
        return tuple(sorted(self.unclassified))


def _by_strategy(samples: Sequence[SliceSample]) -> dict[int, list[SliceSample]]:
    grouped: dict[int, list[SliceSample]] = defaultdict(list)
    for sample in samples:
        grouped[sample.strategy_id].append(sample)
    return grouped


def build_pool(
    samples: Sequence[SliceSample],
    matches: Mapping[int, ArchetypeMatch],
    *,
    params: config.EvidenceConfig = config.EVIDENCE,
) -> PoolResult:
    """把一批切片样本池化成（策略 × 阶段）的结论。

    `samples` 里出现的策略**每一格都产出**（同 `slice._cell` 的理由：缺失的格子在
    日报里表现为「这一行没有」，而它有两种意思）。样本不足时改用原型兜底；原型也
    样本不足仍然产出「中性 + 证据不足」，标 `source=archetype`——**不引入第三层**（Q4）。

    `matches` 必须覆盖到一个策略 id 就有一条（`resolve_archetype` 的产物，`{}` 会让
    全部策略走未归类）。样本足的策略用不到它，但**仍然要有**：否则「这条策略归到
    哪一类」在样本一变化时就成了一个没人记录过的事实。
    """
    result = PoolResult()
    grouped = _by_strategy(samples)

    for strategy_id in sorted(grouped):
        for regime in PRIORITY:
            result.cells[(strategy_id, regime.value)] = _pool_cell(
                strategy_id=strategy_id,
                regime=regime,
                own=grouped[strategy_id],
                pool=grouped,
                matches=matches,
                result=result,
                params=params,
            )
    return result


def _pool_cell(
    *,
    strategy_id: int,
    regime: BaseRegime,
    own: Sequence[SliceSample],
    pool: Mapping[int, Sequence[SliceSample]],
    matches: Mapping[int, ArchetypeMatch],
    result: PoolResult,
    params: config.EvidenceConfig,
) -> PooledCell:
    """一格：先用自己的样本，不够再换原型那一批。

    **两条路径都只经过 `_pooled_state`，没有第二处改写。** 「原型也样本不足 → 中性」
    （Q4：不引入第三层）已经落在 `_pooled_state` 的门槛那一步；在这里再写一遍不会
    改变任何结果，却会把 `unknown`（该阶段窗口里就没出现）改写成 `neutral`——
    那是句被证据推翻的话：样本再多也回答不了「没出现过的阶段适不适用」。同理，
    保命档的状态与证据无关，也不该被门槛改写。判定的**唯一**入口是 `_pooled_state`，
    顺序的理据都在它那里。
    """
    merged = _merge(own, regime.value, params)

    if merged.enough:
        state, reason = _pooled_state(merged, regime)
        # 冲突判定问的是**相对**方向：合并结论与某个品种自己的结论是否相反。绝对水平
        # 门（`_fitness` 里那道）与它无关——一条策略在所有品种上都亏钱时，它「不比
        # 全样本差」仍然成立，而那不构成冲突。传 `state` 会让绝对门静默关掉冲突判定。
        relative_state, _ = relative_fitness(merged.metrics, merged.full_metrics)
        conflict = _conflict(relative_state, own, regime.value, params)
        return PooledCell(
            strategy_id=strategy_id,
            regime=regime.value,
            source=POOL_SOURCE_STRATEGY,
            state=state,
            reason=reason,
            evidence=_evidence(merged, extra={"conflict": conflict}),
            needs_review=conflict is not None,
        )

    # 兜底：与同原型策略的样本一起池。含自己——自己那几笔是「这类策略长什么样」的
    # 真实观测，摘掉它等于在样本最薄的地方再砍一刀。
    match = matches.get(strategy_id) or ArchetypeMatch(ARCHETYPE_UNCLASSIFIED)
    archetype = match.archetype
    siblings = [
        sample
        for other_id, other_samples in pool.items()
        if _archetype_of(other_id, matches) == archetype
        for sample in other_samples
    ]
    if archetype == ARCHETYPE_UNCLASSIFIED:
        # 未归类要「计数并出现在日报第④段」（Q5）。计数放在结果上而不是证据里：
        # 一条未归类的策略可能在四个格子上各留一条证据，而日报要的是**策略数**。
        result.unclassified.add(strategy_id)

    fallback = _merge(siblings, regime.value, params)
    state, reason = _pooled_state(fallback, regime)

    # 兜底路径**不出「适用」结论**（2026-10-22）。借来的样本能说明「这一类策略在这个
    # 阶段行不行」，说明不了「这一条行不行」——而 `fit` 是会被拿去做放行依据的那个值。
    #
    # 现网实测到的荒谬结论正出在这里：`volume_support_reclaim_strategy` 自己的样本
    # 是 0 笔，却因为借了均值回归原型组（344 笔）而判 `range / fit`；同一批证据下，
    # 它在另一代里借的是「未归类」组（11693 笔）就被判 `unfit`。**同一条策略、同样
    # 稀薄的证据，换个组就从不适用翻成适用。** 那不是证据，那是分组方式的副产品。
    #
    # 降成中性而不是不适用，与 `_fitness` 的绝对门同一条理由：中性不构成停用理由，
    # 也不构成放行理由，它准确地表达了「这一段对这条策略，我们还没有自己的证据」。
    # `unfit` 保留原样——它拦的是停用，方向上是安全的（借用别人的差样本说「你可能
    # 不行」再要求人工确认，与直接放行不是同一个量级的风险）。
    if state == STATE_FIT:
        state = STATE_NEUTRAL
        reason = REASON_ARCHETYPE_NOT_FIT

    return PooledCell(
        strategy_id=strategy_id,
        regime=regime.value,
        source=POOL_SOURCE_ARCHETYPE,
        state=state,
        reason=reason,
        evidence=_evidence(
            fallback,
            extra={
                "archetype": {
                    "name": archetype,
                    "source": match.source,
                    "raw_text": match.raw_text,
                    "matched_word": match.matched_word,
                },
                # 自己被退回的那份样本也留着：兜底结论引用的是别家的样本，而「本策略
                # 自己为什么不够格」必须能在同一条记录里读到——否则回头查「这条策略
                # 到底有几笔」要另开一个流程。
                "own_sample": {
                    "trades": merged.trades,
                    "months": merged.months,
                    "regime_days": merged.regime_days,
                    "results": list(merged.results),
                },
            },
        ),
        # 兜底结论**不产生待复核**：待复核的语义是「本策略自己的证据与池化结论打架，
        # 需要人看一眼」。兜底这一格根本没有「自己的证据」——它有的只是别家策略的。
        # 给它挂上待复核，等于把「样本不足」伪装成「有争议」。
        needs_review=False,
    )


def _archetype_of(strategy_id: int, matches: Mapping[int, ArchetypeMatch]) -> str:
    """某条策略的原型；没登记过就算未归类。

    没登记**等于**未归类，而不是「跳过」：池化兜底的分组必须是一个全覆盖的函数，
    否则同一条策略会同时属于「某个原型」与「没有原型」两个集合，两处算出来的
    样本还不一样。
    """
    match = matches.get(strategy_id)
    return match.archetype if match else ARCHETYPE_UNCLASSIFIED
