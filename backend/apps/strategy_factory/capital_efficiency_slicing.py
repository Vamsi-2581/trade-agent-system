"""资金效率的**回测侧适配层**：`BacktestTrade` 行 → `metrics["capital_efficiency"]`。

## 这一步在整条链路里的位置

    BacktestTrade 的 close 行（entry_time / exit_time / quantity / entry_price）
        ↓  本模块：行 → OccupancyInterval（只认已平仓、且 fields 齐全的行）
    占用区间序列 + 窗口 + Decimal 本金
        ↓  纯模块 `capital_efficiency.compute_capital_efficiency`（不碰 DB）
    资金效率结论（六个固定键 + 三道判不了的门）
        ↓  本模块：盖 version / computed_at / 口径快照 / 归因计数
    BacktestResult.metrics["capital_efficiency"]
        ↓  #36 的前端展示 / 重算入口 `recompute_regime_slices`

`capital_efficiency.py` 是纯函数（不吃 DB、不读时钟、不碰 `BacktestResult`），本模块是它的
适配层，也是**唯一**写 `metrics["capital_efficiency"]` 的地方。本模块自己也不是铁板一块：
`adapt_trades` / `building_blocks` / `capital_efficiency_payload` 仍是纯的（行传进来、
载荷传出去），只有 `load_intervals` / `save_capital_efficiency` / `is_stale` /
`compute_capital_efficiency_for` 碰 DB。

## 为什么适配层在 `apps/strategy_factory/`，而不是 `apps/backtest/`

spec #30 D11 的原话把回测 app 的改动范围收成**一行 dispatch**：「全部改动收敛在
`apps/strategy_factory/`（判定与纯模块）与 `apps/backtest` 保存路径（dispatch 一行）
+ `apps/trading/session_stats.py`」。回测 app 那一侧因此只放调用点，不放口径。

先例是 `apps/regime/slicing.py`：切片适配层住在 `apps/regime/`（机制那一侧），而
`metrics["regime_slice"]` 同样是 `BacktestResult` 的字段。**机制拥有自己的适配层与载荷
形状，模型所属的 app 只拿到调用点。** 沿同一条线：资金效率的纯模块在
`apps/strategy_factory/capital_efficiency.py`，它的适配层就该并排住在这里。

还有一条更硬的理由：适配层与本模块要写的那份载荷，形状由**纯模块的口径**决定
（`CAPITAL_EFFICIENCY_VERSION` / 六个键 / `sample_threshold`）。把适配层放进
`apps/backtest/` 会让「改了纯模块的口径」与「改了适配层的装配」分居两个 app，
而它们必须一起变——`version` 就是为此存在的。

## 口径快照：`sample_threshold`，以及**不是** `config_snapshot`

口径 6（`capital_efficiency.py` 模块 docstring）说 `sample_sufficient` 只开关一个标注，
不动作。但它仍然是一个**会改的输入**：门槛从 30 改成 10，同一批行会从「样本不足」翻成
「样本充足」，而载荷里那个布尔值看起来一模一样。所以门槛必须随结论一起落库——这就是
D8 要的「口径快照」，与 `slicing.py` 的 `evidence_threshold` 同一条。

**本模块刻意不塞 `config_snapshot`**（切片那边有它）。切片的结论由 judgement + evidence
两组参数决定，所以它记一整份快照；资金效率的结论只由 `sample_threshold` 一个数决定，
记一个数就够。塞一整份 `FactoryConfig.snapshot()` 会引入 `slicing.py` 已经写明的那类错：
改一个与本机制无关的配置项（比如 `wf_folds`），会让全部历史载荷看起来「是按旧参数算的」
——而 `is_stale` 只比 `sample_threshold`，于是那些载荷既不会被重算、又永远标着「旧参数」，
变成一句没人能消掉的假话。

## 占用金额（notional）：取 `quantity × entry_price`，并写清它**不是**什么

纯模块要的是「资金占用量（本金口径）」。`BacktestTrade` 行上没有一个字段直接是它，
而两个候选（`quantity × entry_price` / `quantity × exit_price`）在**能反映真实占用的那
一侧**其实相等：

* `_execute_sell`（`apps/strategy_engine/backtest_mode.py`）给 `close` 行的 `entry_price`
  写的是**引擎的加权平均成本 `_avg_entry_price`**，不是某一批的开仓价；
* 同一次卖出里 `quantity` 是 `sell_qty`；
* 于是 `quantity × entry_price` **恰好就是这段仓位占用的资金**（扣佣金前的建仓成本），
  而 `quantity × exit_price` 是**平仓时的名义值**。

取「占用」那一侧而不是「离场」那一侧的理由，与纯模块口径 1 是同一条：占用是**持有期间**
的资金压力，用离场那一刻的价格去度量它，等于用平仓时点的行情去改写这段持仓此前每一天的
资金占用——同一笔仓位只因行情最后涨了 50%，就会显得当初多占了 50% 的钱。

**它不是什么（三句，都要说清楚）：**

1. **不是保证金。** 本系统这一版回测引擎是现货口径的多头现金账户（`_cash = initial_capital`，
   `_execute_buy` 在 `total_cost > self._cash` 时拒绝成交），所以「notional = 建仓成本」
   恰好等于「真实占用的本金」。**引擎里没有任何杠杆字段**：`cost_detail` 只有
   `commission` / `slippage` / `funding` 三个成本项，`parameters` 是使用者传进来的策略
   参数（`FactoryConfig` 那一侧也没有杠杆/保证金字段）。将来若接杠杆永续，这一行会成为
   错的，而**它会安静地错**——所以口径写在 `notes` 里跟着载荷走（见下）。
2. **不是 `initial_capital`。** 本金是分母，占用是分子；两者相等只发生在满仓那一刻。
3. **不含未平仓的那一截。** 回测收尾时仍开着的那部分仓位没有任何一行 `close`，因此它占用
   的钱不在任何区间里——`avg_capital_used` 因此**系统性偏低**，`idle_pct` 偏高。这不是
   可以算出来的缺口（那一截没有平仓时刻），只能计数（`attribution.open_trades_excluded`）
   并写进 `notes`。

## 行过滤：与 `slicing.adapt_trades` 逐字同一条纪律

只认 `trade_type == "close"` 且 `exit_time` / `pnl` 都非 NULL 的行。另外两类分开计数：

* `open` / `add` 行 —— 常态（从未平仓的持仓），进 `open_trades_excluded`；
* **`close` 行却缺 `exit_time` 或 `pnl` —— 数据异常**，单独计数并 `logger.warning`。

两类都不进载荷的区间，但成因完全不同：混成一个数会让「有一笔没平完」看起来像「有一行
写坏了」，而后者是回测保存路径的真实故障。阈值口径（`slicing.py` 的那段注释）在这里同样
成立，所以下面是同一套三分法，不是重新发明一遍。

**为什么 notional 为 NULL 不在这里判**：`close` 行的 `quantity` / `entry_price` 是
NOT NULL 字段，理论上推不出 NULL。但**不做静默兜底**：万一将来某一行真的是 NULL，
`notional=None` 会被纯模块判成整段 `invalid_capital`（口径 5 的那道门），而不是被当成
「这段没占钱」——判不了会吼，静默的 0 不会。反过来，真实的 `quantity == 0` 仍然是 0
（那是一个真实的测量值），所以那条判据是 `is None` 而不是假值判断（见 `_row_notional`）。

## 幂等与收敛

同样的行 + 同样的窗口 + 同样的本金 → 同样的载荷（纯模块是纯函数，本模块只加时间戳）。
所以任务重试、重投、手动重算都不会叠加副作用，收敛靠「已经有一份不陈旧的载荷就跳过」，
而不是靠「只投一次」——后者在投递失败、任务重试这些路径上根本表达不出来。
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Iterable, Sequence

from django.db import transaction

from apps.strategy_factory.capital_efficiency import (
    CAPITAL_EFFICIENCY_METRICS_KEY,
    CAPITAL_EFFICIENCY_VERSION,
    CapitalEfficiencyResult,
    EfficiencyWindow,
    OccupancyInterval,
    OccupancyIntervalKind,
    compute_capital_efficiency,
)
from apps.strategy_factory.config import CONFIG, FactoryConfig

logger = logging.getLogger(__name__)

#: `BacktestResult.metrics` 里承载资金效率结论的键。纯模块定义它，本模块转出来是为了让
#: 调用方（`tasks.py` / 管理命令 / 测试）不必同时 import 两个模块。
METRICS_KEY = CAPITAL_EFFICIENCY_METRICS_KEY

#: 载荷里那句口径说明的来源标记。前端与人都靠它认出「这个 notional 是按建仓成本推的」。
NOTIONAL_BASIS = "quantity_x_entry_cost"

#: 随载荷一起走的口径说明。写成常量而不是就地字符串：它们是**口径的一部分**，与
#: `CAPITAL_EFFICIENCY_VERSION` 同一条链——口径变了这里也要变。
_NOTIONAL_NOTES: tuple[str, ...] = (
    "占用金额 = 成交量 × 建仓成本（close 行的 entry_price 是引擎的加权平均成本），"
    "即这一笔真实占用的本金，不是平仓时的名义值。",
    "现货口径、无杠杆：本引擎在 total_cost > cash 时拒绝成交，所以 notional 就等于"
    "占用本金。将来若接杠杆永续，这个推断不再成立。",
    "不含未平仓的那一截：回测收尾仍开着的仓位没有 close 行，"
    "avg_capital_used 因此系统性偏低、idle_pct 偏高。",
)


def _as_utc(moment: datetime) -> datetime:
    """把 DB 读回来的时刻归一成 UTC。

    与 `slicing._as_utc` 逐字同一条纪律：`USE_TZ=True` 下 `DateTimeField` 读出来一定
    是 aware 的，这里防的是「将来某处写进一个 naive datetime」——naive 交给
    `astimezone()` 会按**进程 TZ** 解释，而进程 TZ 是 Django 加载 settings 时按
    `TIME_ZONE` 覆写的产物。占用区间的时长直接由这两个时刻相减得出，口径挂在别人的
    副作用上会让 `time_in_market_pct` 悄悄偏移。

    naive → UTC 而不是抛出，是因为纯模块的 `OccupancyInterval.__post_init__` 已经在
    构造处拒绝了 naive（那条 `ValueError` 说得更清楚）。两层都要：这一层负责**归一**，
    那一层负责**拒绝**。
    """
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


# --------------------------------------------------------------------------- #
# 纯：行 → 占用区间
# --------------------------------------------------------------------------- #


def _row_notional(row) -> Decimal | None:
    """一行成交的占用金额（本金口径）→ `Decimal`，推不出来时 **`None`**。

    口径本身见模块 docstring「占用金额」：取 `quantity × entry_price`，因为 `close` 行的
    `entry_price` 是引擎的**加权平均成本**，于是这个乘积就是这段仓位真实占用的本金
    （而不是平仓时的名义值）。

    这里单独成一个函数，是为了钉住两条纪律——它们各自都对应一个会安静跑错的写法：

    1. **推不出来时给 `None`，不给 0。** `quantity` / `entry_price` 是 NOT NULL 字段，
       所以这一行理论上推不出 `None`；但真出现时（手改过的行、将来某个新字段）：
       `row.quantity * row.entry_price` 会抛 `TypeError`，于是**一条坏行让整批算不出来**；
       而 `(row.quantity * row.entry_price) or Decimal("0")` 那一类兜底会把「这一段没占
       钱」当成结论写下去，加权平均被系统性低估——「占用更多钱 ≠ 效率更高」这件事会
       因此反过来。`None` 走的第三条路是纯模块口径 5：整段 `invalid_capital`，判不了
       会吼，静默的 0 不会。
    2. **真实的 0 仍然是 0，不是 `None`。** `quantity == 0` 是一笔真实的测量（零数量，
       占 0 资金），与「推不出来」是两件事。写 `if not notional: return None` 会把前者
       误判成后者——所以下面判的是 `is None`，不是假值。
    """
    quantity = row.quantity
    price = row.entry_price
    if quantity is None or price is None:
        return None
    return quantity * price


def adapt_trades(rows: Iterable) -> tuple[list[OccupancyInterval], dict[str, int]]:
    """`BacktestTrade` 行 → `OccupancyInterval`，并数出被挡在外面的行。

    返回 `(区间, 归因计数)`。计数有三个键，对应模块 docstring 的三分法：

        open_trades_excluded     open / add 行（从未平仓的持仓，常态）
        incomplete_close_rows    close 行却缺 exit_time / pnl（**数据异常**，告警）
        closed_trades            真正进了载荷的区间数

    `pnl` / `quantity` / `entry_price` 都是 `DecimalField`，全程留在 `Decimal` 上：
    纯模块的加权平均是 `Σ(占用 × 时长) / 总时长`，全程 Decimal；这里转成 float 会把
    那条链路唯一的 float 转换点从纯模块内部挪到适配层，而纯模块的 docstring 已经写明
    转换点只有一处（`_as_pct`）。`Decimal(20,8)` 的价格乘 `Decimal(20,8)` 的数量会有
    16 位小数，远高于任何阈值的分辨率。

    **占用金额推不出来时给 `None`，绝不给 0**（`_row_notional` 的 docstring 记着这条
    纪律与它防的两个 bug）。注意这里**不是** `row.quantity * row.entry_price` 一行了事：
    那一行在 `entry_price` 为 `None` 时抛 `TypeError`，把整批挂在一条坏行上——而
    「一条坏数据让整批算不出来」与「这一条算不出来、其余照算」的差别，就是本模块
    唯一能替调用方兜住的那种故障。
    """
    intervals: list[OccupancyInterval] = []
    excluded = 0
    incomplete = 0

    for row in rows:
        if (
            row.trade_type == "close"
            and row.exit_time is not None
            and row.pnl is not None
        ):
            intervals.append(
                OccupancyInterval(
                    start=_as_utc(row.entry_time),
                    end=_as_utc(row.exit_time),
                    notional=_row_notional(row),
                    pnl=row.pnl,
                    kind=OccupancyIntervalKind.HELD.value,
                )
            )
            continue
        excluded += 1
        if row.trade_type == "close":
            # 平仓行却缺 pnl / exit_time：数据异常，与「还没平仓」不是一回事。
            incomplete += 1

    if incomplete:
        logger.warning(
            "[strategy_factory] 有 %d 行 close 成交缺 exit_time 或 pnl，未计入资金效率",
            incomplete,
        )

    return intervals, {
        "open_trades_excluded": excluded,
        "incomplete_close_rows": incomplete,
        "closed_trades": len(intervals),
    }


# --------------------------------------------------------------------------- #
# 纯：窗口 / 本金 / 载荷
# --------------------------------------------------------------------------- #


def efficiency_window(start: date, end: date) -> EfficiencyWindow:
    """回测的 `[start_date, end_date]` → 资金效率窗口（**左闭右开**）。

    纯模块口径 4 钉的是右开：`end` 那一瞬属于下一个窗口。而回测的 `end_date` 是
    「这一折的最后一个**交易日**」（含），所以窗口右端要取 `end_date + 1 天`——否则
    最后一天里开着的仓位会被整段裁掉，`time_in_market_pct` 系统性偏低。

    之所以是「+1 天」而不是「+1 秒」：`start_date` / `end_date` 是 `DateField`，它们的
    语义是**一整天**（业务日），不是某一瞬。用 `datetime.combine(day, time.min)` 把整天
    的边界显式化成 UTC 零点，与 `walk_forward` 的分折边界同一条约定（那边也是整日）。
    """
    return EfficiencyWindow(
        start=datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc),
        end=datetime.combine(
            date.fromordinal(end.toordinal() + 1),
            datetime.min.time(),
            tzinfo=timezone.utc,
        ),
    )


def payload_from_result(
    result: CapitalEfficiencyResult,
    *,
    attribution: dict[str, int] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """纯模块的结论 → 可直接写进 `metrics["capital_efficiency"]` 的完整载荷。**纯函数。**

    在 `as_payload()` 之上补四样它拿不到的东西：

    * `version` —— 载荷形状/口径的版本（`is_stale` 的第一条判据）；
    * `computed_at` —— 什么时候算的（**记录，不是输入**，见 `is_stale`）；
    * `sample_threshold` —— D8 要的口径快照。纯模块的 `as_payload()` 里**已经有**它，
      这里显式再取一次不是为了抄一遍，而是为了让「载荷顶层必须有这一个键」这件事在
      本模块里可见——`is_stale` 就是按顶层读它的，而它一旦哪天被挪进子字典，
      陈旧判定会静默退化成「永远不陈旧」；
    * `notional_basis` + `attribution` —— 口径说明与三类行计数。

    `reason` **不进载荷**（它在对象上，见纯模块 docstring）：载荷要能跟「算出来了」那一
    份同形，前端不必为判不了单独写一条分支。判不了这件事由「六个指标键全是 `None`」
    表达——而那与「键不存在」是两件事，正是本票要保证的。
    """
    payload = dict(result.as_payload())
    payload["version"] = CAPITAL_EFFICIENCY_VERSION
    payload["computed_at"] = (now or datetime.now(timezone.utc)).isoformat()
    payload["sample_threshold"] = result.sample_threshold
    payload["notional_basis"] = NOTIONAL_BASIS
    payload["computed"] = result.computed
    payload["reason"] = result.reason
    payload["attribution"] = dict(
        attribution
        if attribution is not None
        else {
            "open_trades_excluded": 0,
            "incomplete_close_rows": 0,
            "closed_trades": result.trade_count,
        }
    )
    # 口径说明跟着数字走：notional 的推断是本载荷里最容易被误读的一处（见模块
    # docstring），把三句话写进 notes 而不是留在代码注释里——看载荷的人不会来读源码。
    payload["notes"] = list(result.notes) + list(_NOTIONAL_NOTES)
    return payload


def capital_efficiency_payload(
    *,
    rows: Sequence,
    window: EfficiencyWindow,
    initial_capital: Decimal | None,
    config: FactoryConfig = CONFIG,
    now: datetime | None = None,
) -> dict[str, Any]:
    """一次回测的行 + 窗口 + 本金 → 完整载荷。**纯函数。**

    `initial_capital` 必须是 `Decimal`（`BacktestResult.initial_capital` 是
    `DecimalField(20,2)`）。**传 float 会被纯模块判成 `invalid_capital`**——这是刻意的：
    收一个 float 意味着上游已经悄悄转过一次，而这条链路上的钱从不经过二进制浮点。
    本函数不替调用方 `Decimal(str(...))`：那会把一次口径错误变成一次静默的成功。
    """
    intervals, attribution = adapt_trades(rows)
    conclusion = compute_capital_efficiency(
        intervals,
        window,
        initial_capital,
        sample_threshold=config.min_trades,
        config=config,
    )
    return payload_from_result(conclusion, attribution=attribution, now=now)


# --------------------------------------------------------------------------- #
# 落库
# --------------------------------------------------------------------------- #


def stored_payload(result) -> dict | None:
    """结果里已存的那份资金效率载荷，没有则 `None`（老回测就是这一种）。"""
    return (result.metrics or {}).get(METRICS_KEY)


def save_capital_efficiency(result_id, payload: dict) -> None:
    """把载荷写进 `metrics[METRICS_KEY]`，**只动这一个键**。

    读-改-写整个 JSON 字段，所以套行锁（`select_for_update`）：两个写方并发时后者会读到
    前者之前的快照，把前者的键抹掉——`metrics` 里已经住着 `regime_slice` 与
    `walk_forward`，那不是「资金效率被覆盖」，而是**别人的字段凭空消失**。这与
    `slicing.save_slice` / `walk_forward_runner._write_metrics_key` 是同一段代码形状，
    不是抄错：三个写方都往同一个 JSONField 里塞键，谁都只能读-改-写。

    `update_fields=["metrics"]`：不带上其余字段一起写，免得把某个并发更新（比如
    `celery_task_id` 或 `review_status`）回滚掉。

    import 在函数体内（与 `slicing.save_slice` 同一写法）：`apps.backtest` 反过来 import
    本模块（`apps/backtest/tasks.py` 取 dispatch），模块级 import 会把这条边变成环。
    """
    from apps.backtest.models import BacktestResult

    with transaction.atomic():
        result = (
            BacktestResult.objects.select_for_update()
            .only("id", "metrics")
            .get(id=result_id)
        )
        metrics = dict(result.metrics or {})
        metrics[METRICS_KEY] = payload
        result.metrics = metrics
        result.save(update_fields=["metrics"])


def is_stale(result, config: FactoryConfig = CONFIG) -> bool:
    """结果里那份资金效率是否该重算。

    比三样，每一样都对应一个能改变结论的输入：

    * `version` —— 载荷形状/口径变了（`CAPITAL_EFFICIENCY_VERSION`）；
    * `sample_threshold` —— 门槛变了。它只开关 `sample_sufficient` 一个标注，但那个标注
      是载荷的一部分，而载荷必须自足（D8）：不重算的话，「样本不足」这句结论会一直挂在
      一个已经改成 10 笔的系统上；
    * **窗口是否还盖得住已存的行**（`window_days`）—— 行被重建后窗口变了，同一批行会
      被裁到另一个区间里，结论不同。回测重跑会删旧 `BacktestTrade` 再重建
      （`backtest_mode._save` 的 `existing_result_id` 分支），所以这一条有真实的触发场景。

    **不比 `computed_at`、不比 `notes` / `attribution` 的其余部分、不比纯模块的结论值**：

    * `computed_at` 是记录不是输入。拿「算得比现在早」当陈旧会让重算入口每次都是全表
      重写，而它的全部价值就在于「只补该补的那些」——`slicing.is_stale` 的 docstring
      把这条写死了，这里照办。
    * `notes` / `attribution.open_trades_excluded` 由行本身决定，而行变了必然伴随
      `closed_trades` 或 `pnl` 变，落在那两条判据上；单独比它们只会让「同一批行重算
      一遍」永远判成陈旧。
    * 结论值（比如 `capital_utilization`）更不能比：那是**输出**，比输出等于用「算出来
      的和存着的不一样」定义陈旧，而它们不一样恰恰说明该重算——判据是循环的，且第一次
      不一致之后的每一轮都会重算。

    **本判据不做行级扫描。** 它被重算入口逐行调用，而那个入口的价值就在「全表过一遍
    但只重写该写的」；每次 `is_stale` 都去 `result.trades` 拉一遍行，`--all` 就从
    「0.3 秒的扫描」变成上千次查询。真被这条缝隙漏掉的场景（行改了但版本、门槛、窗口
    都没变）由 `--force` 兜底。
    """
    stored = stored_payload(result)
    if not stored:
        return True
    if stored.get("version") != CAPITAL_EFFICIENCY_VERSION:
        return True
    if stored.get("sample_threshold") != config.min_trades:
        return True

    expected = efficiency_window(result.start_date, result.end_date)
    stored_window = stored.get("window_seconds")
    # 老载荷或被人手改过的载荷可能没有这个键：读作「不知道当初算的是哪一段」→ 重算。
    if stored_window is None or not _same_seconds(stored_window, expected.duration):
        return True
    return False


def _same_seconds(stored: Any, expected: float) -> bool:
    """两个「秒」是否同一个数，容忍 JSON 往返的精度损失。

    `window_seconds` 在纯模块里 `round(..., 6)` 之后进 JSON，回来时是 float。直接
    `==` 在绝大多数情况下成立，但一个从 JSON 读回来的 float 与一个现算的 float 逐位
    相等是**运气**而不是保证（浮点十进制↔二进制往返）。差半个微秒以内一律读作同一个
    窗口——这个容差比任何真实窗口变化都小 12 个数量级（窗口是天级的）。

    `bool` 单独排掉：`True == 1` 会让一个被人误写成 `true` 的载荷看起来「窗口是 1 秒」，
    而那恰好会与一个巧合的 1 秒窗口撞上。宁可判陈旧。
    """
    if isinstance(stored, bool) or not isinstance(stored, (int, float)):
        return False
    return abs(float(stored) - expected) < 1e-6


# --------------------------------------------------------------------------- #
# 取数（碰 DB）
# --------------------------------------------------------------------------- #


def load_intervals(result) -> tuple[list[OccupancyInterval], dict[str, int]]:
    """从一行回测结果取 `close` 行 → 占用区间。

    走 `result.trades.all()`（`related_name="trades"`）。这里**不 defer**：需要的
    `entry_time` / `exit_time` / `quantity` / `entry_price` / `pnl` / `trade_type` 六列
    就是这张表的全部小列，一次拉回来比六次 `.only()` 拼装便宜，而 `BacktestTrade` 上
    没有 `BacktestResult` 那四列（`HEAVY_COLUMNS`）那样的巨物。
    """
    return adapt_trades(list(result.trades.all()))


def compute_capital_efficiency_for(
    result, *, config: FactoryConfig = CONFIG, now: datetime | None = None
) -> dict[str, Any]:
    """取行 → 算载荷 → 落库。返回落下去的那份载荷。

    这是**唯一的取数入口**：任务、管理命令、任何单次重算都走它，免得「同一份载荷在两处
    按另一套过滤条件装配一遍」——那正是 `session_stats_for` 那条纪律（同一个数不许有
    两个算法）在回测侧的对应物。
    """
    intervals, attribution = load_intervals(result)
    conclusion = compute_capital_efficiency(
        intervals,
        efficiency_window(result.start_date, result.end_date),
        result.initial_capital,
        sample_threshold=config.min_trades,
        config=config,
    )
    payload = payload_from_result(conclusion, attribution=attribution, now=now)
    save_capital_efficiency(result.id, payload)
    return payload
