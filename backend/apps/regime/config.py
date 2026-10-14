"""行情阶段机制的**唯一配置面**（v1 只读）。

CONTEXT.md 决策：所有机制阈值收敛到同一处，不散落为各模块常量——它们会被一起
调整、一起审计，散开后「为什么这个门槛是 30」将无处可查。

三条纪律：

1. **只读**。v1 没有任何权限设施（无角色模型），「改动广播给所有人」只能告知、
   不能阻止；改阈值必须改代码发版。等有了角色模型再开放写入，届时留痕机制已就位。
   所以这里不放 DB 模型，也不提供 setter；每个分组都是 frozen dataclass。

2. **留痕靠快照，不靠读回配置**。判定记录内嵌「当时生效的完整参数快照」：半年后
   要解释「这条结论用的是哪套参数」时，答案在记录自己身上，不必翻 git 考古。
   为此每个分组都必须是 `dataclasses.replace` 可派生的 frozen dataclass，并且
   能经 `snapshot()` 变成 JSON-safe 的字面量。**测试就是把这两条钉住**。

3. **自校验**。数值边界在 import 时检查（`__post_init__`），坏编辑在进程启动时
   就炸，而不是先安静地跑出一批奇怪判定、等有人去翻日报才发现。

分组按「会被一起调整」划分，不按使用它的模块划分。

## 分组清册

CONTEXT.md 的阈值全枚举，以及每一组落在哪一段（防止「没写 = 忘了」）：

| 分组 | 内容 | 状态 |
|------|------|------|
| `CANDLES` | 日线拉取与落库参数 | ✔ 单元 1 |
| `JUDGEMENT` | 判定指标周期、分位阈值、趋势判据 | ✔ 单元 3 |
| `JUDGEMENT_LIFECYCLE` | 最小持续期、状态过期 | ✔ 本单元 |
| `EVIDENCE` | 切片证据门槛 | ✔ 本单元 |
| `DEACTIVATION` | 人工豁免生效期 | ✔ 单元 7 |
| `BOX` | 箱体判定与高低点结构的参数（约 10 个） | ✔ 单元 8 |
| `NEWS` | 采集窗口、预筛条数、正文截断、白名单源 | ✔ 本单元 |
| `SHADOW` | Shadow 到期上下限、一致率达标阈值 | ✔ 单元 8 |
| `REPORT` | 日报投递看门狗时刻 | ✔ 单元 8 |
| `EVENT` | 事件窗口与全局上下限、事件库衰减告警、候选失效期 | ✔ 第①段（录入端） |
| `DERISK` | 滑点上限、减仓分片数上限 | ✔ 第②段 ②a |
| `SELF_FUSE` | 自熔断频率 | 第②段 |
| `RECOVERY` | 恢复豁免期 | 第③段 |

后两组的数值 CONTEXT.md 已给出，但**形状还不可知**（例如自熔断频率是「一次动作内
的失败率」还是「一段时间的次数」，取决于减仓动作本身最后长成什么样）。先写下来等于
替将来的设计拍一个形状，而那个形状会被后来者 cargo-cult。所以它们等各自的段落地时
再加，不在此处预置空壳。

`BOX` 是同一个理由的另一种形态：形状是已知的（`detect_box_range` 的入参就摆在那里），
而且**基础阶段不依赖它**——「箱体震荡」是四档里的兜底，检不检出箱体都落到它。它只在
日报要写依据时才需要（「判为箱体，且确实检出 [a, b]」对「判为箱体，但未见成形箱体」），
所以它的每一个数都是**描述性**的，不是信号：没有哪个数会让哪条策略被停掉。这一点决定了
它的默认值怎么取，见 `BoxConfig` 的 docstring。

## `BOX` 不能「照着函数签名誊一遍」：那个函数的默认值是**不可用的**

原本的打算是「默认值逐字抄 `detect_box_range`，表示不调参」。写下才发现抄不了：
`max_width_abs` 与 `max_width_pct` 的默认值**都是 `None`**，而
`_validate_box_range_params` 的第一条校验是「必须传入 `max_width_abs` 或 `max_width_pct`
之一」——即 `detect_box_range(klines)` 这个调用**必然抛 ValueError**。逐字抄的结果是一个
import 时就炸的 `BoxConfig()`，或者一个一调用就炸的配置。

所以这一组有**一个**数不是函数默认值：`max_width_pct`，取 `0.30`。出处不是发明：本仓库
里已有调用点的取值集合是 `0.01`–`0.50`（`apps/strategy_engine/tests/` 与
`apps/agent/tools/test_box_range.py`），而 `0.30` 是**被当作「宽到不成为约束」用的**那个
——`test_box_range_real_market.py` 把它跟 `upper_max_discard_pct=0.0` 配在一起，测的是
粗筛参数，宽度上限在那里只是背景。

沿用同一个用法：这里的宽度上限**不该是那个在做判断的东西**，做判断的是函数自身的结构
要求（几个 pivot、每个碰几次、离窗口极值的距离）。理由是这个数不参与档位归属：
「箱体震荡」是四档的兜底，收不放宽都不会让哪条策略被停或不停，它只决定日报第①段写
「判为箱体，且确实检出 [a, b]」还是「判为箱体，但未见成形箱体」。既然唯一后果是一句话的
强度，就取宽的一侧：偏紧会让日报在最像箱体的那些天里说不成形，而那条句子长得跟「今天
确实没有箱体」一模一样，没人会去查。

`max_width_abs` 保持 `None`（不启用）：绝对宽度随标的价格量级而变，对 BTC 而言「$5000」
在 3 万和 12 万时完全是两件事，而相对宽度没有这个问题。两个都设会被函数自己的校验拦下
（二选一），`BoxConfig.__post_init__` 提前到 import 时拦同一件事。

## `NEWS` 的白名单源：可达性取决于代理，第一类因此换了形态

`NewsConfig.sources` 的默认值是**实机验证过可达**的那批源（2026-09-22 从本项目的运行
环境逐个探测，`httpx` + `follow_redirects`）。探测结果**分两种情形**，而这两种情形的差别
不是细节，它决定了第一类源能不能有成员：

- **不走代理**：`cointelegraph.com/rss`、`theblock.co/rss.xml`、`decrypt.co/feed`、
  `federalreserve.gov/feeds/press_all.xml`、`ecb.europa.eu/rss/press.html` 可达；
  `binance.com`、`okx.com`、`blog.kraken.com`、`blog.coinbase.com`、`blog.bitmex.com`、
  `bitget.com` 一律连接超时（`bls.gov` 是 403）。
- **走 `settings.WEB_PROXY`**：上述源照常可达，**并且交易所也通了**。本部署的容器里
  始终配着这个代理（`web_fetch.py` 走的就是它），所以运行时走的是这一支——但这条
  依赖必须显式写出来，否则「白名单里有没有交易所」会被一个没写下来的环境前提决定。

通了之后发现交易所这一类的**形态与前两类不同**：Kraken/Coinbase 那几家仍不可达，而
Binance 与 OKX 各自暴露了一个结构化、免鉴权的**公告 JSON 接口**（比公告页更适合当输入）：

- `binance.com/bapi/composite/v1/public/cms/article/list/query?...&catalogId=48`（公告目录）
- `okx.com/api/v5/support/announcements`

所以 v1 的三类白名单**都有成员**，但第一类不是 RSS。这件事不能靠「一个通用解析器吃掉
所有源」蒙混过去：RSS/Atom 是标准形态，一套解析入口通吃；而这两个接口的信封各不相同
（`data.catalogs[].articles[]` vs `data[].details[]`，时间戳一个毫秒整数一个字符串，
链接一个要给字段一个要用 code 拼）。**这正是 `NewsSource.parser` 存在的理由**：第二、
三类用通用的 `rss`，第一类用具名 parser，把「这个源要怎么读」变成配置表上一行显式的、
可审计的字面量，而不是藏在采集器里的一个 `if "binance" in url`。

**刻意不含 HTML 公告页。** 探测中 `announcements.bybit.com` 的 HTML 是可达的，但为公告页
写抓取器意味着把页面结构焊进代码，而页面结构没有任何契约保证——站点改版后抓取器**不会
报错，只会返回 0 条**，那是一条比抓取失败更难发现的故障（它长得跟「今天很平静」一模一样）。
真需要时应该加一个具名 parser，而不是加一个「通用 HTML 抓取器」。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, is_dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any

# --------------------------------------------------------------------------- #
# 分组定义
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CandleConfig:
    """日线数据的拉取与落库参数。

    这些是**数据面的操作参数**，不是机制阈值（不决定任何判定结果），但仍放在
    这里：第①段的每个旋钮都只有一个家，读者不必猜「还有没有别处能调」。
    """

    # 单一判定源：机制是全市场级，BTC 日线是历史标注与每日判定的同一份真相。
    symbol: str = "BTC/USDT"
    exchange: str = "binance"
    # == apps.datasource.base.KlineInterval.D1，此处写字面量以免为了一根字符串
    # 把 datasource 的模块图拉进本 app。一致性由测试对 KlineInterval.D1.value 断言。
    timeframe: str = "1d"

    # ccxt 单次请求上限。分页游标推进（而非固定窗口）由调用方保证。
    page_limit: int = 1000

    # 「已收盘」的宽容带：判定任务排在 UTC 00:00 整（北京 08:00），而那一刻
    # 前一日线刚收盘，时钟微偏就会让游标永远落后一天。留一点余量让它仍算收盘，
    # 落库是幂等 upsert，值若尚未最终化，下一次运行会自愈。
    close_tolerance: timedelta = timedelta(minutes=5)

    def __post_init__(self) -> None:
        if self.page_limit < 1:
            raise ValueError(f"page_limit 必须 >= 1，当前 {self.page_limit}")
        if self.timeframe != "1d":
            # 判定与切片的自然日口径建立在「日线」上：D 的 date 就是它的开盘日，
            # 且业务日 == UTC 开盘日 == 北京同日。换成别的周期，这个等式不成立。
            raise ValueError(
                f"timeframe 必须为 '1d'——判定与切片的日粒度口径依赖它，当前 {self.timeframe!r}"
            )


@dataclass(frozen=True)
class JudgementConfig:
    """量化判定的指标周期与分位阈值。

    阈值一律用**滚动历史分位数**表达而非绝对水平：币圈波动率水平会整体漂移，
    固定常量必然周期性失效。分位窗口因此是这套参数里最重要的一个数——它决定了
    「高波动」是相对什么而言的高波动。
    """

    # 指标周期（CONTEXT.md：ATR 默认 14 日线、EMA 默认 20/60）
    atr_period: int = 14
    ema_fast_period: int = 20
    ema_slow_period: int = 60

    # 滚动分位窗口：250 个自然日 ≈ 一年，够长到包含一轮完整的波动收敛与扩张。
    quantile_window_days: int = 250

    # 高波动档：ATR% 分位 > 80%。四档里唯一由单一阈值直接给出的档位。
    high_vol_quantile: float = 0.80

    # 趋势判据 = 「EMA 斜率符号」与「(EMA20−EMA60)/ATR 过阈值」**同时**成立。
    # 两者分工不同：符号是廉价的粗筛（噪声大但不会漏方向），间距倍数才是实质门槛。
    # 只要符号、不要间距幅度，噪声喊一声就算趋势；只要间距、不要符号，(EMA20−EMA60)
    # 是带符号的，负间距会变成「下行」——符号那一项其实在防的就是这个误读。
    #
    # 斜率回看 5 个自然日：短于「最小持续期 5 天」的斜率是在测噪声（机制自己都规定
    # 阶段至少站 5 天才算数），长于它的斜率则追不上已经在走的趋势。
    ema_slope_lookback_days: int = 5

    # 快慢均线间距至少要走满 0.5 个 ATR。用 ATR 归一化而非绝对价格：币圈价格量级
    # 跨四年变化几十倍，绝对间距门槛必然周期性失效（与「阈值按分位自校准」同一理由）。
    trend_separation_min_atr: float = 0.5

    @property
    def warmup_days(self) -> int:
        """历史标注要在回测窗口起点前多取多少天。

        等于分位窗口本身：要算出第一个分位数，就得先攒满一整个窗口。**刻意做成
        派生属性而不是第二个字段**——两个独立字段可以互相漂移（有人把窗口改成
        500 却忘了改预热），而漂移的表现是标注序列开头一段 NaN，看起来像「那几天
        数据缺失」，不像参数配错。
        """
        return self.quantile_window_days

    def __post_init__(self) -> None:
        for name in ("atr_period", "ema_fast_period", "ema_slow_period"):
            if getattr(self, name) < 2:
                raise ValueError(f"{name} 必须 >= 2，当前 {getattr(self, name)}")
        if self.ema_fast_period >= self.ema_slow_period:
            raise ValueError(
                f"ema_fast_period({self.ema_fast_period}) 必须小于 "
                f"ema_slow_period({self.ema_slow_period})"
            )
        if self.quantile_window_days < 2:
            raise ValueError(
                f"quantile_window_days 必须 >= 2，当前 {self.quantile_window_days}"
            )
        if not 0.0 < self.high_vol_quantile < 1.0:
            raise ValueError(
                f"high_vol_quantile 必须在开区间 (0, 1) 内，当前 {self.high_vol_quantile}"
            )
        if self.ema_slope_lookback_days < 1:
            raise ValueError(
                f"ema_slope_lookback_days 必须 >= 1，当前 {self.ema_slope_lookback_days}"
            )
        if self.trend_separation_min_atr <= 0:
            raise ValueError(
                f"trend_separation_min_atr 必须为正，当前 {self.trend_separation_min_atr}"
            )


@dataclass(frozen=True)
class JudgementLifecycleConfig:
    """判定结论的时间约束。

    两个数管的是两件不同的事，但都在回答「上一版结论还算不算数」，会被一起调整，
    所以同组：

    - `min_dwell_days`：阶段是慢变量，切换后至少站住 5 个自然日才允许再切。
      挡的是判定在阈值附近来回抖（每天都换个说法，日报就没法读）。
    - `stale_after_days`：状态过期超 3 个自然日就不再新增停用决策。判定任务故障
      时**保持上一有效状态**（绝不降级成「最保守」——那会让任务故障伪装成风控
      动作），但一个过期太久的结论不该继续指挥停用。
    """

    min_dwell_days: int = 5
    stale_after_days: int = 3

    def __post_init__(self) -> None:
        for name in ("min_dwell_days", "stale_after_days"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} 不能为负，当前 {getattr(self, name)}")


@dataclass(frozen=True)
class EvidenceConfig:
    """切片结论的证据门槛。

    门槛是「用十几笔交易停掉一个策略」的唯一天然刹车，因此不足者判为「中性」，
    而**中性不构成停用理由**——宁可如实显示中性，也不为了机制看起来有输出而降门槛。

    `min_trades` 是基数；稀有阶段按该阶段占天数的比例缩放到 `min_trades` 以下，
    但缩放后仍必须同时满足 `min_months`，并且必须同时输出置信区间。
    """

    min_trades: int = 30
    min_months: int = 3

    def __post_init__(self) -> None:
        if self.min_trades < 1:
            raise ValueError(f"min_trades 必须 >= 1，当前 {self.min_trades}")
        if self.min_months < 1:
            raise ValueError(f"min_months 必须 >= 1，当前 {self.min_months}")


@dataclass(frozen=True)
class DeactivationConfig:
    """停用推导（第①段单元 7）里唯一一个可调的数。

    `exemption_days` 是人工豁免的生效期（自然日）。CONTEXT.md 把它明确划进「与判定
    参数同一配置面」，所以它在这里而不在豁免模型上：模型上的默认值会让「10 天」出现
    第二份，而两份会在某次有人改了其中一份之后开始分歧——分歧的表现是「豁免到底
    多久」，而它恰好是唯一一个让人跳过自动停用的开关，错了没人会发现。

    **生效期在写入豁免时就换算成绝对时刻存下来**（`expires_at`），所以改动这个数
    **不会**追溯延长或缩短已经发出的豁免。同 `RegimeJudgement` 存 `attribute_date` 的
    理由：留痕靠记录自己，不靠用今天的规则去反推。生效期因此是「发豁免那一刻的配置」，
    而不是「查询那一刻的配置」。
    """

    exemption_days: int = 10

    def __post_init__(self) -> None:
        if self.exemption_days < 1:
            raise ValueError(f"exemption_days 必须 >= 1，当前 {self.exemption_days}")


@dataclass(frozen=True)
class BoxConfig:
    """箱体判定的参数（`detect_box_range` 的入参，第①段单元 8）。

    **它不参与档位归属。** 「箱体震荡」是四档里的兜底，无论检不检出箱体，判不出趋势
    与高波动时都落到它。所以这里每个数的作用只有一处：日报第①段写依据时，说清楚那天
    的「箱体震荡」是「确实检出 [a, b] 一个成形箱体」还是「只是没判出别的」。既然唯一
    后果是一句话的强度，`max_width_pct` 就取宽的一侧（见模块 docstring）。

    字段与默认值**逐一对应函数签名**，唯一例外是与 `None` 有关的三个：

    - `max_width_abs` / `max_width_pct`：函数要求**二选一**，两个都是 `None` 时它直接
      抛错。所以本组必须给出一个，`__post_init__` 把函数那条校验提前到 import 时——
      逐字誊抄函数默认值会得到一个 import 就炸（或一调用就炸）的配置，
      模块 docstring 记了这件事。
    - `min_width_abs`：保持 `None`（不启用绝对下限），理由与 `max_width_abs` 对称。

    `kwargs()` 把 `None` 丢掉再交给函数：丢与不丢在**函数语义**上等价（它的默认值就是
    `None`），区别只在调用点读起来是「这一项没配」而不是「这一项配成了 None」。真正
    会因缺项而失败的那一项（宽度上限）由 `__post_init__` 拦在更早的地方。
    """

    # -- 宽度上下限：二选一的那个「一」在这里 -- #
    max_width_abs: float | None = None
    max_width_pct: float | None = 0.30
    min_width_abs: float | None = None
    min_width_pct: float | None = 0.005

    # -- 高低点结构 -- #
    pivot_window: int = 2
    min_gap_bars: int = 3
    min_pivots: int = 2
    min_touches: int = 2

    # -- 波动与时长 -- #
    atr_period: int = 14
    min_duration_bars: int | None = 12

    # -- 粗筛：上/下沿离窗口极值超过这个比例就丢弃该候选 -- #
    upper_max_discard_pct: float = 0.15
    lower_max_discard_pct: float = 0.15

    def __post_init__(self) -> None:
        if (self.max_width_abs is None) == (self.max_width_pct is None):
            raise ValueError(
                "max_width_abs 与 max_width_pct 必须二选一"
                f"（当前 abs={self.max_width_abs}, pct={self.max_width_pct}）"
                "——这是 detect_box_range 自己的要求，"
                "而逐字誊抄它的默认值恰好会踩中这一条"
            )
        for name in (
            "max_width_abs",
            "max_width_pct",
            "min_width_abs",
            "min_width_pct",
        ):
            value = getattr(self, name)
            if value is not None and value <= 0:
                raise ValueError(f"{name} 必须为正数，当前 {value}")
        for name in (
            "pivot_window",
            "min_gap_bars",
            "min_pivots",
            "min_touches",
            "atr_period",
        ):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} 必须 >= 1，当前 {getattr(self, name)}")
        if self.min_duration_bars is not None and self.min_duration_bars < 1:
            raise ValueError(
                f"min_duration_bars 必须 >= 1 或为 None，当前 {self.min_duration_bars}"
            )
        for name in ("upper_max_discard_pct", "lower_max_discard_pct"):
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} 落在 [0, 1]，当前 {value}")

    def kwargs(self) -> dict[str, Any]:
        """交给 `detect_box_range` 的关键字参数（丢掉 `None`，见类 docstring）。"""
        return {k: v for k, v in asdict(self).items() if v is not None}


# --------------------------------------------------------------------------- #
# 资讯通道（第①段单元 5）
# --------------------------------------------------------------------------- #


class NewsSourceKind(str, Enum):
    """白名单源的三个类别。

    做成枚举而不是自由字符串，是因为这三个类别在日报与采集结果里要**分别计数**：
    「交易所公告这一类今天哑了」和「加密媒体这一类今天 0 条」是两条不同的信息，
    而只要类别名能拼错，计数就会静默地掉进第四类。
    """

    EXCHANGE = "exchange"
    CRYPTO_MEDIA = "crypto_media"
    MACRO = "macro"

    @property
    def display(self) -> str:
        return _NEWS_KIND_DISPLAY[self]

    @classmethod
    def choices(cls) -> list[tuple[str, str]]:
        return [(m.value, m.display) for m in cls]


#: 类别 → 展示名。落进模型 choices 与日报措辞，不就地写中文。
_NEWS_KIND_DISPLAY = {
    NewsSourceKind.EXCHANGE: "交易所公告",
    NewsSourceKind.CRYPTO_MEDIA: "主流加密媒体",
    NewsSourceKind.MACRO: "宏观类站点",
}


@dataclass(frozen=True)
class NewsSource:
    """白名单里的一个源。

    `name` 是**稳定标识**：资讯条目与判定留痕里记的都是它。改名等于新开一个源——
    历史条目上的来源名不会被追溯改写，那是「记录是化石」的另一面。

    `parser` 指名**解析入口**（取值见 `NEWS_PARSERS`），不是站点名，也不是「这个源属于
    哪一类」——类别是 `kind`，两者正交：`rss` 这一个 parser 同时服务加密媒体与宏观两类。

    **刻意不做成「按站点写抓取器」**：那会把每个站点的页面结构焊进代码里，而页面结构
    是随时会变的。RSS/Atom 是标准形态，一个解析入口通吃任意多家；只有**接口信封各不相同
    的结构化 API**（Binance / OKX 的公告接口）才需要各自的 parser，而那样的 parser 依赖的
    是接口契约（有版本、会报错），不是页面结构（无契约、改版后安静地返回 0 条）。
    两者是「会坏的依赖」与「无声坏的依赖」的区别，所以只接受前者。
    """

    name: str
    url: str
    kind: NewsSourceKind
    parser: str = "rss"

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("资讯源的 name 不能为空——它是条目与判定留痕里的来源标识")
        if not self.url.startswith(("http://", "https://")):
            raise ValueError(
                f"资讯源 {self.name} 的 url 必须是 http(s)，当前 {self.url!r}"
            )
        if self.kind not in NewsSourceKind:
            raise ValueError(
                f"资讯源 {self.name} 的 kind 未登记：{self.kind!r}；"
                f"可用：{[m.value for m in NewsSourceKind]}"
            )
        if self.parser not in NEWS_PARSERS:
            raise ValueError(
                f"资讯源 {self.name} 的 parser 未登记：{self.parser!r}；"
                f"可用：{list(NEWS_PARSERS)}"
                "——拼错的 parser 会让整个源安静地取不到条目，所以在 import 时就拦下"
            )


#: 解析入口的登记表。加一个成员 = 在采集器里加一个同名函数，**两边必须是同一批名字**
#: （由采集器的测试对这里断言）。`rss` 含 Atom——两者解析路径相同。
#:
#: `html` 不在表里，理由见模块 docstring：页面结构无契约，改版后返回 0 条而不是报错。
NEWS_PARSERS = ("rss", "binance_announcements", "okx_announcements")


#: 关键词预筛的默认词表。**刻意放宽**：命中与否只是「要不要花 token 送进 LLM」的
#: 成本闸门，不是相关性判定——漏掉一条该被看到的资讯，代价是一次错误的抬升缺失；
#: 多送一条无关的，代价是几十个 token。两个方向的代价不对称，所以宁滥勿缺。
#: 匹配是**小写子串**匹配，因此像 "ban" 也会命中 "urban" 这类词，这是接受的。
#: 全部小写（匹配前会把标题与正文也小写化）；中文词不受大小写影响，一并列出。
NEWS_KEYWORDS: tuple[str, ...] = (
    # 标的与市场
    "bitcoin",
    "btc",
    "crypto",
    "stablecoin",
    "tether",
    "usdt",
    "etf",
    "liquidation",
    "liquidations",
    "exchange",
    "custody",
    # 监管、法律与安全事件（最可能触发抬升的一类）
    "regulation",
    "regulator",
    "lawsuit",
    "settlement",
    "ban",
    "sanction",
    "tariff",
    "hack",
    "exploit",
    "halt",
    "delist",
    "bankruptcy",
    # 宏观
    "interest rate",
    "rate cut",
    "rate hike",
    "inflation",
    "cpi",
    "fomc",
    "fed",
    "federal reserve",
    "recession",
    "liquidity",
    # 中文（宏观类源里有中文标题的站点）
    "比特币",
    "加密",
    "监管",
    "禁止",
    "黑客",
    "被盗",
    "稳定币",
    "清算",
    "降息",
    "加息",
    "通胀",
    "美联储",
    "衰退",
    "关税",
    "制裁",
)


#: 实机验证可达的默认源，三类齐全。可达性与「为什么第一类走具名 parser」见模块
#: docstring。补源只需在这里加一行；**这一类今天有几个源**由 `kinds_covered` 回答。
NEWS_SOURCES: tuple[NewsSource, ...] = (
    # ---- 第一类：交易所公告 ------------------------------------------------ #
    # 这两个源只有走 `settings.WEB_PROXY` 才可达（容器里配着，见模块 docstring）。
    # `pageSize=40` 与 `NewsConfig.max_items_per_source` 同值：前者是接口一次给多少，
    # 后者是采集器收多少，配成一样是为了让「接口明明有更多、我们却只看到 40 条」这种
    # 情况不出现——真被 `max_items_per_source` 截断时，那是一次可见的截断。
    NewsSource(
        name="binance",
        url=(
            "https://www.binance.com/bapi/composite/v1/public/cms/article/list/query"
            "?type=1&pageNo=1&pageSize=40&catalogId=48"
        ),
        kind=NewsSourceKind.EXCHANGE,
        parser="binance_announcements",
    ),
    NewsSource(
        name="okx",
        url="https://www.okx.com/api/v5/support/announcements",
        kind=NewsSourceKind.EXCHANGE,
        parser="okx_announcements",
    ),
    # ---- 第二类：主流加密媒体 ---------------------------------------------- #
    NewsSource(
        name="cointelegraph",
        url="https://cointelegraph.com/rss",
        kind=NewsSourceKind.CRYPTO_MEDIA,
    ),
    NewsSource(
        name="theblock",
        url="https://www.theblock.co/rss.xml",
        kind=NewsSourceKind.CRYPTO_MEDIA,
    ),
    NewsSource(
        name="decrypt",
        url="https://decrypt.co/feed",
        kind=NewsSourceKind.CRYPTO_MEDIA,
    ),
    # ---- 第三类：宏观类站点 ------------------------------------------------ #
    NewsSource(
        name="federalreserve",
        url="https://www.federalreserve.gov/feeds/press_all.xml",
        kind=NewsSourceKind.MACRO,
    ),
    NewsSource(
        name="ecb",
        url="https://www.ecb.europa.eu/rss/press.html",
        kind=NewsSourceKind.MACRO,
    ),
)


@dataclass(frozen=True)
class NewsConfig:
    """资讯通道的采集、预筛与留痕参数。

    这一组管的是**输入**：从哪些源、取多久以内的条目、留下多少、把多长的正文送给
    LLM。资讯**结论**的形状（方向 / 是否抬升 / 强度）不在这里——那是 LLM 的输出，
    不是可调的旋钮；把「抬升阈值」做进配置面，等于给这套机制留一个能被调松的阀门。

    ## 采集窗口是**增量区间**，不是「最近 N 小时」

    窗口 = (上次判定成功时刻, 本次判定时刻]，上限 `window_cap_hours`（96 小时）兜住
    「判定停了几天又恢复」：没有上限，一次长故障后的重启会把攒了两周的条目一口气
    送进 LLM，而那一批得出的结论描述的是两周前的市场——它会被当成今天的结论生效。

    下限 `window_floor_hours`（24 小时）管的是反方向：判定成功后又跑一次（同一天里
    的重试、或当天第二次心跳）时，窗口只有几分钟。此时真正的语义是「今天这一天有
    什么新资讯」，不是「最近这五分钟」。下限把窗口钉在一天以上，于是同一天里的重复
    运行看到的是同一批条目——配合条目表的链接去重，重复运行不会重复送同一篇，
    只会得出同一个结论，这让整条通道可以幂等地重试。

    两个数配反了（上限 < 下限）会让区间永不到头或直接为空，而运行时的表现是
    「每天都 0 条」——安静得看不出是配错了，所以在 import 时校验。

    ## 预筛是**硬过滤 + 条数上限**，不是排序取前 N

    关键词不命中就直接丢，条数上限只在窗口很长（如上述故障恢复）时才咬人，咬的时候
    按发布时间倒序取最新的——「来得及反应」的那批。上限存在的理由不是成本，而是
    **LLM 的判断力**：一百条混在一起的一次性判断，比二十条时更容易滑向「没什么大事」。
    """

    # ---- 采集窗口（小时） ----
    window_floor_hours: int = 24
    window_cap_hours: int = 96

    # ---- 单源抓取 ----
    # 每个源的超时是**独立**的：一个源挂住不该让整轮采集陪葬，所以逐源 try/except
    # 并逐源记结果（「某个源抓取失败」必须能被单独看见，见模块 docstring）。
    fetch_timeout_seconds: float = 20.0
    # 单源取回条目数的上限，防止某个源一次给几千条把内存与预筛成本抬起来。
    max_items_per_source: int = 40

    # ---- 预筛 ----
    keywords: tuple[str, ...] = NEWS_KEYWORDS
    max_items_to_llm: int = 20
    # 送给 LLM 的正文截断上限。**这就是永久留存的那份原文**（截断后的），
    # 因为留存的是「当时 LLM 到底看到了什么」。将来体积真成问题时，正确的动作是
    # 下调这个数，不是删旧条目（CONTEXT.md 决策）。
    body_max_chars: int = 2000

    # ---- 白名单源 ----
    sources: tuple[NewsSource, ...] = NEWS_SOURCES

    def __post_init__(self) -> None:
        if self.window_floor_hours < 1:
            raise ValueError(
                f"window_floor_hours 必须 >= 1，当前 {self.window_floor_hours}"
                "——下限为 0 时窗口退化成「这一刻之后」，等于永远没有输入"
            )
        if self.window_cap_hours < self.window_floor_hours:
            raise ValueError(
                f"window_cap_hours({self.window_cap_hours}) 不能小于 "
                f"window_floor_hours({self.window_floor_hours})"
                "——区间会变成空的，而表现是「每天都 0 条」"
            )
        if self.fetch_timeout_seconds <= 0:
            raise ValueError(
                f"fetch_timeout_seconds 必须为正，当前 {self.fetch_timeout_seconds}"
            )
        for name in ("max_items_per_source", "max_items_to_llm", "body_max_chars"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} 必须 >= 1，当前 {getattr(self, name)}")
        if not self.keywords:
            raise ValueError(
                "keywords 不能为空——空词表等于「所有条目都命中」，"
                "预筛这道成本闸门会静默消失"
            )
        for kw in self.keywords:
            if kw != kw.strip() or kw.lower() != kw or not kw:
                raise ValueError(
                    f"关键词必须是非空、无首尾空白的小写串，当前 {kw!r}"
                    "——匹配是小写子串匹配，词表里出现大写等于这个词永远不命中"
                )
        if not self.sources:
            raise ValueError(
                "sources 不能为空——没有源就没有输入，而「没有输入」与「今天 0 条」"
                "在运行时会表现成同一件事"
            )
        names = [s.name for s in self.sources]
        duplicates = sorted({n for n in names if names.count(n) > 1})
        if duplicates:
            raise ValueError(
                f"资讯源重名：{duplicates}——来源名是条目与判定留痕里的标识，"
                "重名会让「这条来自哪个源」无法回答"
            )

    @property
    def kinds_covered(self) -> tuple[str, ...]:
        """当前白名单实际覆盖到的类别（按 `NewsSourceKind` 的声明顺序）。

        **派生而不是字段**：它是 `sources` 的函数，单独存一份就会与 `sources` 漂移，
        而漂移的表现是「日报说三类齐全，实际有一类一个源都没有」。写进快照，是为了
        让每条判定记录都能回答「当时是哪几类在供数」。

        它**不保证三类齐全**：某类被整个摘掉（源全挂了、被移出白名单）时，这里就少
        一项，而「少一项」与「这一类今天 0 条」是两件不同的事——前者是白名单的空洞，
        后者是正常输入，两者都会让那一天的相关资讯缺失，但只有前者需要人去补源。
        """
        covered = {s.kind for s in self.sources}
        return tuple(m.value for m in NewsSourceKind if m in covered)


# --------------------------------------------------------------------------- #
# Shadow 与日报（第①段单元 8）
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ShadowConfig:
    """Shadow 期的到期条件与出 Shadow 的成功标准（CONTEXT.md 第 159–164 条）。

    这些数是**承诺**，不是调优旋钮：它们定义「机制被验证到什么程度才算数」，而验证
    一旦开始就不能改——改了等于把已经跑过的那段重新解释一遍。所以它们跟别的分组一样
    只读、进快照，出 Shadow 的那一刻用的是**当时写下的那套数**。

    三个自然日的数各管一件事，缺一不可：

    - `min_natural_days`（20）与 `min_event_windows`（3）是**触发器**：至少 20 个自然日
      **且**至少经历 3 次高影响事件窗口，两个都满足才谈出 Shadow。只要其中一个，会得到
      一段「20 天里一次事件都没有」或「3 次事件挤在 6 天里」的验证——前者没验到事件
      那一路，后者没验到日常那一路。
    - `expiry_cap_days`（60）是**兜底**：事件可能很久不来，不能因为「还没遇到 3 次事件」
      就无限期 Shadow 下去。到期只说明「可以谈了」，不说明「达标了」。

    `min_agreement_rate`（0.80）与 `max_trigger_rate`（0.10）是 CONTEXT.md 第 163 条
    成功标准①②的**阈值**（第 164 条要求它们进统一配置面）。

    `0.80` 这个数需要一句解释：四档均匀分布下随机一致率是 0.25，但真实市场里「箱体
    震荡」（兜底档）占多数，于是随机基线会被抬高——一个永远答「箱体震荡」的退化机制
    也能拿到不低的一致率。所以**单看 ① 不够**（一致率只看「对了几次」），第 164 条把
    「系统性错向」单列成不达标条件正是为了堵住这条路。

    错向是**单向**的：人工标〈高波动〉或〈下行趋势〉而算法判〈箱体震荡〉，**一次**即
    不达标（`truth.EXTREME` / `truth.CALM`）；反过来的错（人工标〈箱体震荡〉而算法判
    〈高波动〉）只算普通的不一致。这个不对称正是要堵的那条路——箱体震荡是**兜底档**
    （`quant.PRIORITY` 的末位，「既非高波动、也无趋势」的剩余），退化成「永远答箱体
    震荡」的机制能拿到不低的一致率，而它恰恰在**该保命的日子里说不必保命**；反过来的
    错是保守方向，代价是多停几次策略，不是爆仓。两条一起才拦得住退化机制。

    触发频率按**自然日**计（CONTEXT.md 原话），不是按判定次数：心跳每 5 分钟一轮，
    按次数算出来的「%」是心跳频率的函数，与机制行为无关。
    """

    min_natural_days: int = 20
    min_event_windows: int = 3
    expiry_cap_days: int = 60
    min_agreement_rate: float = 0.80
    max_trigger_rate: float = 0.10

    def __post_init__(self) -> None:
        for name in ("min_natural_days", "min_event_windows", "expiry_cap_days"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} 必须 >= 1，当前 {getattr(self, name)}")
        if self.expiry_cap_days < self.min_natural_days:
            # 下限高于上限的配置是自相矛盾的：任何时候都判不出「可以谈了」，
            # 而它看起来跟「还没到期」一模一样。
            raise ValueError(
                f"expiry_cap_days（{self.expiry_cap_days}）不能小于 "
                f"min_natural_days（{self.min_natural_days}）"
                "——那等于永远不出 Shadow，且症状与「还没到期」无法区分"
            )
        for name in ("min_agreement_rate", "max_trigger_rate"):
            value = getattr(self, name)
            if not 0.0 < value <= 1.0:
                raise ValueError(f"{name} 落在 (0, 1]，当前 {value}")


@dataclass(frozen=True)
class ReportConfig:
    """日报的形状与投递看门狗（CONTEXT.md 第 80、175、183 条）。

    **日报什么时候发不由 `watchdog_hour` 决定**：它由判定任务驱动，判定任务确定结束
    （成功或最终失败）之后才发，不固定钟点。这个数只决定「到了这个点还没成功投递就
    升级告警」——所以它是**看门狗**的时刻，不是发送时刻，取名与用途必须一致，否则
    下一个人会照着它去改发送逻辑。

    时刻按**业务时区（北京时间）**解读：这个字段是给人看的钟点（「早九点还没收到日报
    就该响了」），而系统里所有的绝对时刻都存 UTC。口径写在 `apps/common/time_utils.py`。

    `event_horizon_days`（7）是第③段「未来 N 天的高影响事件」的那个 N。它进统一配置面
    是 CONTEXT.md 第 80 条点名的（「`query_events` 的查询天数」），而它同时是
    `apps/agent/event_commands.py` 里 `/event list` 的默认天数——**三处必须是同一个数**，
    否则「我在命令里查了没有」与「日报里没有」会变成两句不同的话，而两条消息都不会
    提到这个差别。守卫这一条的是 `apps/regime/tests/test_report.py` 里的一条断言。

    **它不是给 Agent 传参用的旋钮**（CONTEXT.md 第 183 条）：`query_events` 工具不吃
    天数参数，读的就是这个数。进配置面是为了「改了就一定看得见」（跟 `shadow` 那组
    同一个理由），不是为了让它可被调用方覆盖。
    """

    watchdog_hour: int = 9
    watchdog_minute: int = 0
    event_horizon_days: int = 7

    def __post_init__(self) -> None:
        if not 0 <= self.watchdog_hour <= 23:
            raise ValueError(f"watchdog_hour 落在 [0, 23]，当前 {self.watchdog_hour}")
        if not 0 <= self.watchdog_minute <= 59:
            raise ValueError(
                f"watchdog_minute 落在 [0, 59]，当前 {self.watchdog_minute}"
            )
        if self.event_horizon_days < 1:
            raise ValueError(
                f"event_horizon_days 必须 >= 1，当前 {self.event_horizon_days}"
                "——0 会让第③段每次都报「未来 0 天内没有事件」，"
                "而那与「真的没有事件」在日报上一模一样"
            )


# --------------------------------------------------------------------------- #
# 重大事件（第①段单元 8ii）
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class EventsConfig:
    """事件熔断的窗口形状与两条时效（CONTEXT.md 第 147、152、154 条）。

    **本组里没有任何一个数是「要不要熔断」的判据**：那是三档 impact 加上人录进来的
    `event_time`，两者都是事实。这里全是**形状**参数，它们的共同特征是「改了会让已入库
    的事件含义变化」——同一个 `event_time` 配不同的窗口就是两个不同的熔断区间，所以
    它跟别的分组一样只读、进快照。

    前半段（四个分钟数）**只在录入端参与计算**：`apps.regime.events.resolve_window()`
    在录入那一刻用它算出这条事件的停/恢复时刻，同时校验收窄/放宽的覆盖值必须落在全局
    上下限内。第②段只读算好的结果——判据留在录入端，是为了让「这个窗口凭什么这么长」
    在**录入那一刻**就被人看见，而不是等到窗口开启时才由一段没人读的日志说出来。

    **默认窗口不是对称的**（前 2 小时、后 1 小时），这是刻意的：事件前的价格行为是
    预期驱动的（提前离场有意义），事件后的第一小时是价格发现最乱的一段（进场没有意义），
    而再往后就是在拿「我不确定」当理由长期停摆。上下限（15 分钟 ~ 24 小时）拦的是
    另一种错误：一个手滑多打一个 0 的覆盖值会把单条事件变成三天停摆。

    `candidate_expiry_days`（14）与 `coverage_decay_days`（14）数的是两件不同的事，
    所以是两个字段而不是一个——它们现在恰好相等，将来也会各自漂开：

    - 前者是**候选事件的失效期**（CONTEXT.md 第 37 条）：到期未确认即丢弃。它是一条
      硬规则的时长，写进 `CandidateEvent.expires_at`。
    - 后者是**覆盖率衰减的提醒阈值**（CONTEXT.md 第 152 条）：超过这么多天没有新事件
      入库，日报第③段与告警都要说。它**只能用来提醒，不能用来判故障**——日历型事件
      靠人录，而平静期可以持续很久，把它当故障判据会得到一份在平静期天天响的告警。

    `confirm_horizon_days`（14，第②段 ②f 加的）是**上线确认页**回显的那个天数，所以它
    与上面两个 14 又是另一件事，共处一组只是恰好同值：

    - **与 `REPORT.event_horizon_days`（7）刻意不同，也必须不同**（CONTEXT.md 第 183
      条）。日报第③段回答的是「未来一周有什么事件」，天天要读；上线确认回答的是
      「接下来两周这个开关会不会空转」，一个人一辈子点几次。同一个数被拿去做两件事，
      两边就都没法各自调——某个平静的月份里，日报想说的是「一周内没有」、确认页想说的
      是「两周边界内有没有」，把它们绑在一起等于让其中一个开口说假话。
    - **与 `coverage_decay_days`（14）也不是一件事**：后者是「多久没新事件入库要提醒」，
      是**回看**；这个是「往后看多久」，是**前瞻**。同样只有一个字段的理由与上面那条
      一样——它们现在恰好相等，将来也会各自漂开。
    """

    default_halt_before_minutes: int = 120
    default_resume_after_minutes: int = 60
    window_floor_minutes: int = 15
    window_cap_minutes: int = 1440
    candidate_expiry_days: int = 14
    coverage_decay_days: int = 14
    confirm_horizon_days: int = 14

    def __post_init__(self) -> None:
        for name in (
            "default_halt_before_minutes",
            "default_resume_after_minutes",
            "window_floor_minutes",
            "window_cap_minutes",
            "candidate_expiry_days",
            "coverage_decay_days",
            "confirm_horizon_days",
        ):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} 必须 >= 1，当前 {getattr(self, name)}")
        if self.window_floor_minutes > self.window_cap_minutes:
            # 上下限反过来 ⇒ 所有覆盖值都被拒，而症状是「覆盖功能坏了」而不是
            # 「参数配错了」——下一个读日志的人会去查入口代码。
            raise ValueError(
                f"window_floor_minutes（{self.window_floor_minutes}）不能大于 "
                f"window_cap_minutes（{self.window_cap_minutes}）"
                "——那会让每一个覆盖值都落不进去"
            )
        for name in ("default_halt_before_minutes", "default_resume_after_minutes"):
            value = getattr(self, name)
            if not self.window_floor_minutes <= value <= self.window_cap_minutes:
                raise ValueError(
                    f"{name}（{value}）必须落在 "
                    f"[{self.window_floor_minutes}, {self.window_cap_minutes}] 之内"
                    "——默认窗口自己越界的话，所有不写覆盖值的事件都会带着一个"
                    "系统自己都不接受的长度入库"
                )


@dataclass(frozen=True)
class DeriskConfig:
    """自动减仓的成本边界（CONTEXT.md 第 125–129 条）。

    **本组只有两个数，且两个都不是「要不要减仓」的判据**：要不要减仓由事件窗口与影响
    档位决定，那是事实。这两个数管的是**减仓这个动作怎么花钱**——一个划出「贵到不能再
    自动做下去」的那条线，一个限制单笔市价冲击。它们进统一配置面，是因为改了会直接
    决定「熔断在最需要它的时候会不会自己停下」，而那正是这组参数唯一的、也是最坏的
    失败形态。

    `slippage_limit_pct`（0.005 = 0.5%）**是分数口径**（与 `BoxConfig.max_width_pct`
    同一套读法：值 × 100 = 百分数）。口径本身是钉死的（CONTEXT.md 第 129 条：参考价取
    动作发起时刻的中间价，逐品种按成交量加权，只计成交的子单），这里只放那条线。超过
    它 → 告警 + 暂停自动减仓、转人工。它是**上限不是目标**：正常减仓不该贴着它跑，
    撞上它的意思是这次动作的成本不可接受、机制不该再自己动手。

    **0 与 >= 1 都是坏的，且坏法不同**：

    - 取 0（或任何小到正常情形都会越过的值）⇒ 每一次减仓都判超限 ⇒ 自动减仓被永久
      暂停在第一次动作上，而这恰好发生在最需要它的时候——与「分片数不足交易所最小额」
      是同一种失败，只是更彻底。
    - 取 >= 1（100%）⇒ 这条线永远不会被越过 ⇒ 告警与「转人工」形同虚设，而它存在的
      **唯一**意义就是在成本失控时叫停。一个恒假判据比没有判据更坏：它会让「这半年来
      从没触发过」被读成「成本一直很干净」。

    `reduce_shard_count`（3）是**上限**，不是「必须切成 N 片」（CONTEXT.md 第 127 条）：
    每片名义价值低于交易所最小额时自动降片，降到底仍不足则整笔一次市价——那是唯一一条
    合法的「分片数不生效」路径。所以把这个数调**大**不会让减仓更激进，只会让小仓位更早
    走到降片那条路上。调成 1 与「整笔一次」在动作上等价，但走的是两条不同的代码路径
    （一条是配置，一条是降级），日志要能分开读，所以**不拿它当「关掉分片」的开关**。

    上界（10）拦的是另一种错误：手滑多打一个 0 的 30 片会让每片名义价值集体跌到最小额
    以下，于是每一张子单都被拒——失败率被污染，滑点则被**间接**污染（降片 → 每片变大
    → 下一片成交更差），两条路都通向上面那条「滑点超限暂停」。
    """

    slippage_limit_pct: Decimal = Decimal("0.005")
    reduce_shard_count: int = 3

    def __post_init__(self) -> None:
        if not Decimal("0") < self.slippage_limit_pct < Decimal("1"):
            raise ValueError(
                f"slippage_limit_pct（{self.slippage_limit_pct}）必须落在开区间 (0, 1)"
                "——它是分数口径（0.005 = 0.5%）。取 0 会让第一次减仓就判超限、"
                "自动减仓被永久暂停；取 >= 1 则这条线永远不会被越过，等于取消这条告警"
            )
        if not 1 <= self.reduce_shard_count <= 10:
            raise ValueError(
                f"reduce_shard_count（{self.reduce_shard_count}）必须落在 [1, 10]"
                "——手滑多打一个 0 会让每片名义价值集体跌到交易所最小额以下，"
                "每张子单都被拒（CONTEXT.md 第 127 条）"
            )


CANDLES = CandleConfig()
JUDGEMENT = JudgementConfig()
JUDGEMENT_LIFECYCLE = JudgementLifecycleConfig()
EVIDENCE = EvidenceConfig()
DEACTIVATION = DeactivationConfig()
BOX = BoxConfig()
NEWS = NewsConfig()
SHADOW = ShadowConfig()
REPORT = ReportConfig()
EVENTS = EventsConfig()
DERISK = DeriskConfig()


# --------------------------------------------------------------------------- #
# 参数快照
# --------------------------------------------------------------------------- #

# 快照可用的分组及其键名。判定/切片记录里嵌的键就是这里的字符串，所以它们是
# **落库契约**：改名等于让新旧记录出现两套键，不要轻易动。
GROUPS: dict[str, Any] = {
    "candles": CANDLES,
    "judgement": JUDGEMENT,
    "judgement_lifecycle": JUDGEMENT_LIFECYCLE,
    "evidence": EVIDENCE,
    "deactivation": DEACTIVATION,
    "box": BOX,
    "news": NEWS,
    "shadow": SHADOW,
    "report": REPORT,
    "events": EVENTS,
    "derisk": DERISK,
}

#: 派生属性（`asdict` 里没有，但同样是「当时生效的参数」）按分组点名补进快照。
#: 点名而不是「扫描所有 property」：自动扫描会把纯粹的便利属性（如 `__repr__` 之外的
#: 内部辅助量）也塞进落库契约，而落库契约只应该包含**解释结论时需要的那几个量**。
DERIVED_IN_SNAPSHOT: dict[str, tuple[str, ...]] = {
    "judgement": ("warmup_days",),
    "news": ("kinds_covered",),
}


def _jsonable(value: Any) -> Any:
    """把配置值转成 JSONField 能直接存的东西。

    三条转换规则各自有理由：

    - `Decimal` → `str`：金额口径恒用 Decimal，转 float 会在快照这一步丢精度，
      而快照是事后归因的唯一依据。字符串能无损还原。
    - `timedelta` → 秒（float）：可比较、可计算，比 "PT5M" 之类更好用。
    - `Enum` → `value`：存的是枚举的实际取值，不是成员名——成员名会因为重构而变。
    """
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, Enum):
        return _jsonable(value.value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def snapshot(*names: str) -> dict[str, dict]:
    """取若干分组当前值的 JSON-safe 快照，供判定/切片记录内嵌。

    只接受已登记的分组名，且**拼错就报错**：若允许「取不到就跳过」，一次手滑的
    快照会安静地少一组参数，而少了的那组恰恰是日后要用来解释结论的那组。
    """
    unknown = [n for n in names if n not in GROUPS]
    if unknown:
        raise KeyError(f"未登记的配置分组：{unknown}；可用：{sorted(GROUPS)}")

    result: dict[str, dict] = {}
    for name in names:
        group = GROUPS[name]
        if not is_dataclass(group) or not isinstance(group, type(group)):
            result[name] = {"value": _jsonable(group)}
            continue
        result[name] = {k: _jsonable(v) for k, v in asdict(group).items()}
        # 派生属性不在 asdict 里，但它同样是「当时生效的参数」（判定与历史标注都
        # 依赖预热长度；「当时覆盖了哪几类资讯源」也只能从这里读到），所以显式补进去。
        for extra in DERIVED_IN_SNAPSHOT.get(name, ()):
            if extra in result[name]:
                continue
            value = getattr(group, extra, None)
            if value is not None:
                result[name][extra] = _jsonable(value)
    return result


# 全部参数组的合并快照：判定记录用这个，省得逐次点名。
def full_snapshot() -> dict[str, dict]:
    """所有已登记分组的快照。判定记录直接内嵌它。"""
    return snapshot(*GROUPS)


def dumps(*names: str) -> str:
    """快照的 JSON 字面量（排序键，便于比对两条记录是否同参）。"""
    return json.dumps(
        snapshot(*names) if names else full_snapshot(),
        ensure_ascii=False,
        sort_keys=True,
    )
