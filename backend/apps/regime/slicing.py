"""切片适配层与物化（第①段单元 6ii）：`BacktestTrade` 行 → `metrics["regime_slice"]`。

## 这一步在整条链路里的位置

    日线（全量历史，判定源唯一）
        ↓  judgement.load_candles + slice.build_tags
    「日期 → 阶段」（派生映射，不落库）
        ↓  本模块：把一次回测的已平仓成交喂给 slice.slice_backtest
    切片载荷（纯函数算出来的那一半）
        ↓  本模块：盖 computed_at / config_snapshot / 标签指纹
    BacktestResult.metrics["regime_slice"]
        ↓  单元 7 池化 → 停用决策（只记建议）

`slice.py` 是纯函数（不吃 DB、不读时钟、不碰 `BacktestResult`），本模块是它的适配层，
也是**唯一**写 `metrics["regime_slice"]` 的地方。本模块自己也不是铁板一块：`slice_result`
与 `adapt_trades` 仍是纯的（行传进来，载荷传出去），只有 `load_tags` / `compute_slice` /
`save_slice` / `is_stale` 碰 DB。

## 为什么切片要独立成一个任务

CONTEXT.md 的原话：切片计算**不放在回测任务的收尾里**，而是回测结果落库后投递一个
独立的轻量任务。挡在前面的是网格搜索：`run_grid_search_task` 带 `time_limit=7200` 的硬
超时，一个组合切片几十毫秒，上百个组合就是十几秒——把切片塞进那个循环，等于让「切片
有多慢」去决定「网格搜索跑不跑得完」，而这两件事毫无关系。

于是投递点在**结果落库之后、任务返回之前**：

- `run_backtest_task` 一个结果，投一条 `[result_id]`；
- `run_grid_search_task` N 个结果，**循环结束后一次性投一批**，不在循环里逐组合投。

投递失败**不抛**（`dispatch_slice` 吞掉并记日志）：一次成功的回测不该因为一个附加产物
投不出去而变成失败——用户会以为白跑了。代价是「这次没切」，而它有两处兜底：`metrics`
里干脆没有 `regime_slice` 键这件事本身可查，以及全量重算入口（`recompute_regime_slices`）。

任务本身是**幂等**的：同样的日线 + 同样的成交 → 同样的载荷。所以重试、重投、手动重算
都不会叠加副作用，收敛靠「结果里已经有一份不陈旧的切片就跳过」而不是靠「只投一次」。

## 标签从哪来

`judgement.load_candles(judgement.SYMBOL)`——**判定源唯一（BTC），不是回测的 symbol**。
一个策略在 ETH 上回测，它的适用性仍然按 BTC 的行情阶段归档：阶段是**全市场**的宏观
描述（CONTEXT.md），按品种各判一套会得出互相矛盾的结论，而停用决策是按「策略 × 阶段」
池化的，池子里不能混着几种「阶段」的定义。

取全量历史而不是「窗口内那几根」，预热（`config.JUDGEMENT.warmup_days = 250`）就天然
满足了：窗口起点之前本来就有日线，`label_series` 对窗口第一天也能给出结论。代价是每次
切片都重算一遍全量 `label_series`（十毫秒级，一年 365 行）——这正是 `slice.py` 说的
「标签不落库」换来的东西：标签不可能与当前算法不一致。

## 标签指纹：它证明什么、不证明什么

指纹是**窗口内**那些标签的哈希（`tag_fingerprint`）。跟着窗口走是刻意的：

- 窗口外的标签变了但窗口内的没变 ⇒ 指纹不变，切片结论确实没变，不该触发重算。
- 窗口内任何一天的标签变了 ⇒ 指纹变，切片重算。

它**不**证明载荷里的其他数字没变过（门槛、初始资金、成交行）——那些由
`evidence_threshold` 与 `version` 分别看着，`is_stale` 三条一起比。

## 未平仓的行：计数，但**不**按 `trade_type` 甄别

`slice.py` 要的是「一笔已平仓的成交」（`SliceTrade`），而 `BacktestTrade` 里
`trade_type="close"` 的行才是那个东西。`open`/`add` 行没有 `exit_time`、没有 `pnl`，
它们被挡在外面并**计数**（`attribution.open_trades_excluded`）——一笔从未平仓的交易
静默消失，看起来与「这个阶段没有交易」一模一样。

但**不能**把 `close` 行上的 `entry_time` 当作「这笔开仓的时刻」直接相信：
`backtest_mode._execute_sell` 给 `close` 行的 `entry_time` 是 FIFO 配对出来的（可能来自
某个 `add` 批次，也可能落到 `self._entry_time` 那个兜底值），而 `pnl` 是按**加权平均
成本**算的。不过那正是本机制要的东西：切片要回答的是「这笔仓位是**什么时候开的**」，
FIFO 配对给的正是最早那批未平仓的开仓时刻——`_get_fifo_entry_time` 的语义与
`slice._entry_date` 的归属口径一致。所以适配层只需**取用** `close` 行的
`entry_time`/`exit_time`/`pnl`，不自己重排 FIFO：那是回测引擎的账，重排等于在本模块
里长出第二套成本口径，而两套口径一旦不一致，没有任何东西会告诉你哪套对。

代价写在这里：**当一笔仓位由多个批次凑成时，这笔 PnL 全部归属到最早那批的开仓日**。
这与「笔数按入场段计 1 笔」是同一条不对称规则的延伸——笔数是整笔的，钱按持仓日
pro-rata，都只认一个开仓时刻。

`close` 行却缺 `pnl` 或 `exit_time` 是数据异常（不是「未平仓」），单独计数并**告警**：
两类东西都不进切片，但成因完全不同，混成一个数会让「有一笔没平完」看起来像「有一行
写坏了」。
"""

from __future__ import annotations

import hashlib
import logging
from datetime import date, datetime, timezone
from typing import Iterable, Mapping, Sequence

from django.db import transaction

from apps.common.time_utils import as_utc
from apps.regime import config
from apps.regime import slice as slice_core
from apps.regime.judgement import SYMBOL, load_candles
from apps.regime.quant import BaseRegime

logger = logging.getLogger(__name__)

#: `BacktestResult.metrics` 里承载切片结论的键。本模块是它唯一的写方。
SLICE_KEY = "regime_slice"

#: 指纹留几位十六进制。16 位 = 64 bit，撞上的概率在本项目的量级上可以当零。
FINGERPRINT_LENGTH = 16


# --------------------------------------------------------------------------- #
# 标签
# --------------------------------------------------------------------------- #


def load_tags(params: config.JudgementConfig | None = None) -> dict[date, BaseRegime]:
    """当前参数下、全量历史日线给出的「日期 → 阶段」。

    **每次调用都重算**，不缓存：缓存会把「标签与当前算法一致」这条保证换成「标签与
    缓存建立那一刻的算法一致」，而后者是静默的——改完参数重跑切片会拿到旧标签，
    看不出任何异常。重算的代价见模块 docstring。
    """
    return slice_core.build_tags(load_candles(SYMBOL), params or config.JUDGEMENT)


def tag_fingerprint(tags: Mapping[date, BaseRegime], window: tuple[date, date]) -> str:
    """窗口内标签的指纹。按日期排序拼接，所以与构造顺序无关。

    取窗口内的标签而不是全量：见模块 docstring。空窗口（一天都没判定出来）的指纹是
    空串的哈希，是个稳定的合法值——它表达的是「这几天没有任何标签」，与「有标签但
    和记录里的不一样」由 `is_stale` 分开处理。
    """
    start, end = window
    payload = "\n".join(
        f"{day.isoformat()}:{tags[day].value}"
        for day in sorted(tags)
        if start <= day <= end
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:FINGERPRINT_LENGTH]


# --------------------------------------------------------------------------- #
# 成交行 → SliceTrade
# --------------------------------------------------------------------------- #


#: 时刻归一成 UTC 的实现住在 `apps/common/time_utils.py`。它原本在这里有一份逐字拷贝，
#: 与 `capital_efficiency_slicing._as_utc` 一模一样——同一个「naive → UTC」的口径分头
#: 维护，正是 `capital_efficiency.py` 模块 docstring 那句「口径不会在两处分叉」要防的事。
#: 保留 `_as_utc` 这个名字：本模块与它的调用点/测试都按这个名字读，而这一层只是换个
#: 出处，不是换个语义。
_as_utc = as_utc


def adapt_trades(rows: Iterable) -> tuple[list[slice_core.SliceTrade], int]:
    """`BacktestTrade` 行 → `SliceTrade`，并数出被挡在外面的有多少行。

    返回 `(成交, 未平仓行数)`。判定口径与理由见模块 docstring：只认
    `trade_type="close"` 且 `exit_time`/`pnl` 齐全的行，`entry_time` 直接取用
    （它的 FIFO 语义正是归属要的那个时刻）。

    `pnl` 是 `Decimal(20,2)`，这里转成 `float`：切片全程是统计计算（pro-rata 除法、
    开方、`statistics.stdev`），保持在 Decimal 上只会让每一步都要定精度，而结果最终
    要进 JSON。转 float 的精度损失远小于 `Decimal(20,2)` 本身那两位小数。
    """
    trades: list[slice_core.SliceTrade] = []
    excluded = 0
    incomplete = 0

    for row in rows:
        if (
            row.trade_type == "close"
            and row.exit_time is not None
            and row.pnl is not None
        ):
            trades.append(
                slice_core.SliceTrade(
                    entry_time=_as_utc(row.entry_time),
                    exit_time=_as_utc(row.exit_time),
                    pnl=float(row.pnl),
                )
            )
            continue
        excluded += 1
        if row.trade_type == "close":
            # 平仓行却缺 pnl / exit_time：数据异常，与「还没平仓」不是一回事。
            incomplete += 1

    if incomplete:
        logger.warning(
            "[regime] 有 %d 行 close 成交缺 exit_time 或 pnl，未计入切片", incomplete
        )
    return trades, excluded


# --------------------------------------------------------------------------- #
# 载荷
# --------------------------------------------------------------------------- #


def slice_result(
    *,
    rows: Sequence,
    window: tuple[date, date],
    initial_capital: float,
    tags: Mapping[date, BaseRegime],
    params: config.EvidenceConfig | None = None,
    now: datetime | None = None,
) -> dict:
    """一次回测 → 可直接写进 `metrics["regime_slice"]` 的完整载荷。**纯函数。**

    除了 `slice.slice_backtest` 自己产出的那些键，这里补三样它拿不到的东西：
    `computed_at`（什么时候算的）、`config_snapshot`（判定与门槛两组参数的当期值）、
    `tags`（标签从哪来 + 指纹），以及把它留成占位的
    `attribution.open_trades_excluded`。

    `config_snapshot` 取 `("judgement", "evidence")` 两组而不是 `full_snapshot()`：
    切片的结果只由这两组的参数决定，把资讯那组也塞进来会让「参数没变」与「参数变了」
    混在一起——将来改一个资讯关键词就会让所有历史切片看起来「是用旧参数算的」。
    """
    trades, excluded = adapt_trades(rows)
    direct_params = params or config.EVIDENCE
    payload = slice_core.slice_backtest(
        trades,
        tags,
        initial_capital=initial_capital,
        window=window,
        params=direct_params,
    )
    payload["computed_at"] = (now or datetime.now(timezone.utc)).isoformat()
    payload["config_snapshot"] = config.snapshot("judgement", "evidence")
    payload["tags"] = {
        "symbol": SYMBOL,
        "fingerprint": tag_fingerprint(tags, window),
        "count": len(tags),
        "first": min(tags).isoformat() if tags else None,
        "last": max(tags).isoformat() if tags else None,
    }
    payload["attribution"]["open_trades_excluded"] = excluded

    if not payload["attribution"]["window_days"]:
        # 窗口内一天都没判定出来：四格全是 unknown，切片一个字都答不了。照写（「算过，
        # 结论是无从判断」与「从没算过」必须能分开），但要吼一声——成因通常是日线没回填。
        logger.warning(
            "[regime] %s ~ %s 窗口内没有任何已判定日线，切片全为 unknown（日线回填了吗？）",
            window[0],
            window[1],
        )
    return payload


# --------------------------------------------------------------------------- #
# 落库
# --------------------------------------------------------------------------- #


def stored_slice(result) -> dict | None:
    """结果里已存的那份切片载荷，没有则 `None`。"""
    return (result.metrics or {}).get(SLICE_KEY)


def save_slice(result_id, payload: dict) -> None:
    """把载荷写进 `metrics[SLICE_KEY]`，只动这一个键。

    读-改-写整个 JSON 字段，所以套行锁（`select_for_update`）：两个写方并发时后者会
    读到前者之前的快照，把前者的键抹掉——`metrics` 里将来还要放别的东西，那不是
    「切片被覆盖」而是「别人的字段凭空消失」。

    `update_fields=["metrics"]`：这是 JSONField 的读改写，不带上其余字段一起写，
    免得把某个并发更新（比如 `celery_task_id`）回滚掉。
    """
    from apps.backtest.models import BacktestResult

    with transaction.atomic():
        result = (
            BacktestResult.objects.select_for_update()
            .only("id", "metrics")
            .get(id=result_id)
        )
        metrics = dict(result.metrics or {})
        metrics[SLICE_KEY] = payload
        result.metrics = metrics
        result.save(update_fields=["metrics"])


def is_stale(
    result,
    tags: Mapping[date, BaseRegime],
    params: config.EvidenceConfig | None = None,
) -> bool:
    """结果里那份切片是否该重算。

    比三样，每一样都对应一个能改变结论的输入：

    - `version`：载荷形状/算法版本变了；
    - 标签指纹：窗口内的阶段结论变了（判定算法或日线数据变了）；
    - `evidence_threshold`：门槛变了。

    **不比 `computed_at`、不比 `config_snapshot` 的其余部分**：它们是记录而不是输入。
    拿「算得比现在早」当陈旧会让每次重算都全量重算，而全量重算入口的全部价值就在于
    「只补该补的那些」。
    """
    stored = stored_slice(result)
    if not stored:
        return True
    if stored.get("version") != slice_core.SLICE_VERSION:
        return True

    window = (result.start_date, result.end_date)
    if (stored.get("tags") or {}).get("fingerprint") != tag_fingerprint(tags, window):
        return True

    direct_params = params or config.EVIDENCE
    threshold = stored.get("evidence_threshold") or {}
    if (
        threshold.get("min_trades") != direct_params.min_trades
        or threshold.get("min_months") != direct_params.min_months
    ):
        return True
    return False


def compute_slice(
    result,
    tags: Mapping[date, BaseRegime] | None = None,
    *,
    params: config.EvidenceConfig | None = None,
    now: datetime | None = None,
) -> dict:
    """取成交 → 算载荷 → 落库。返回落下去的那份载荷。

    `tags` 由调用方传进来是有意的：一次批量重算要覆盖成百上千个结果，而标签是**全市场
    共用**的一份（同一个 symbol、同一套参数）。让每个结果各自 `load_tags()` 会把一次
    全量 `label_series` 变成一千次。
    """
    tags = tags if tags is not None else load_tags()
    payload = slice_result(
        rows=list(result.trades.all()),
        window=(result.start_date, result.end_date),
        initial_capital=float(result.initial_capital),
        tags=tags,
        params=params,
        now=now,
    )
    save_slice(result.id, payload)
    return payload
