"""walk-forward 分折的**纯计算层**（spec #19 D4 / D15；子票 #29）。

这一层只做三件事，全部是纯函数、零 I/O、不 import Django：

1. `plan_folds(...)` —— 把候选回测区间切成 N 个**整日对齐**的样本外窗口，并给出每折的
   样本内参照窗口（滚动 / 锚定扩张两种切分方式）。
2. `compute_decay_pct(...)` —— 样本内夏普 → 样本外夏普的衰减（**相对**口径）。
3. `aggregate_walk_forward(...)` —— 把各折结果聚合成落进 `metrics["walk_forward"]` 的载荷。

I/O 那一半（跑每折回测、写 `BacktestResult` 行）在 `walk_forward_runner.py`。分开的理由
与 `evaluate.py` 同一条：钱闸门的算术要能在没有 DB 的情况下被穷举测试。

## 四条口径（读代码前先读这四条）

1. **衰减是相对量，不是绝对差**。`decay_pct = (is - oos) / is × 100`。配置项叫
   `oos_decay_max_pct=30`，而「衰减 <30%」只有在相对口径下才是一句能成立的话：本系统
   的夏普量级在 1.5 上下，若按绝对差理解，30 点意味着允许夏普掉 30，门槛永不触发——
   与 spec D12 要修的「闸门空转」是同一类缺陷。分母恒正由样本内闸门保证
   （`is_sharpe >= sharpe_min = 1.5 > 0`），这里仍显式守门。

2. **缺一折 = 判不了，不是判通过**。任一折算不出有限夏普（权益曲线太短、标准差为 0），
   或数据不足以切出 N 个窗口，都落 `data_sufficient=False`，判定层据此落
   `oos_failed` + `insufficient_evidence`，**不放行**。这与 #26 修掉的 NaN fail-open
   是同一纪律：漏斗的下限必须是「什么都不发生」。

3. **窗口边界整日对齐**。`BacktestResult.start_date` / `end_date` 是 `DateField`，按
   UTC 午夜解析（spec D15 明写的前提）。所以分折在**天**这个粒度上切，不引入小时级
   边界；切分用 `Fraction` 而不是浮点乘，避免 `0.29 × 100 = 28.999…` 那类静默少一天。

4. **样本外区间取末尾一段，且样本内窗口绝不越过样本外起点**。默认滚动：每折的样本内
   窗口是与该折等长区间同长的固定长度窗口、紧贴其左侧（随折右移）；锚定扩张：样本内
   窗口从区间起点开始扩张，末尾仍对齐该折起点。两种模式下 `in_sample_end` 都恒等于
   `oos_start`——参照窗口不含样本外那一段，否则衰减会被系统性低估（用样本外自己的数据
   去解释样本外）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta
from enum import Enum
from fractions import Fraction
from typing import Any, Mapping, Sequence

from apps.strategy_factory.config import (
    CONFIG,
    FactoryConfig,
    WalkForwardSplitMode,
)
from apps.strategy_factory.evaluate import is_usable_metric_number

#: 衰减百分比的落库/比较精度（小数位）。
#:
#: 存在的理由是一个**边界确定性**问题：`(2.0 - 1.4) / 2.0 * 100` 在 IEEE 下是
#: `30.000000000000004`，而 `(1.5 - 1.05) / 1.5 * 100` 是 `30.0`——同一个「恰好衰减
#: 30%」在不同数字上落在门槛的两侧。spec 的边界是「衰减恰 30% **拦下**」，这条判定
#: 不能靠浮点运气。定精度后 30% 是可表示的、可复现的。
#:
#: 方向安全性：`round` 到 6 位对**放行**方向是保守的——真实值 29.9999996 会变成
#: 30.0（拦下），而真实值 30.0000004 不会变成 29.9999（放行）。即舍入只会让边界
#: 更严，不会让一个该拦的候选溜过去。这与 #26 对 NaN 的处理同一纪律：钱闸门宁可
#: 多拦一个，不可静默放行一个。
DECAY_PRECISION = 6

#: `BacktestResult.metrics` 里承载分折结论的键。本模块（与 runner）是它的唯一写方。
WALK_FORWARD_METRICS_KEY = "walk_forward"


class WalkForwardWindowRole(str, Enum):
    """一行 `BacktestResult` 在分折里扮演的角色（D4 的 `window_role`）。

    `in_sample` 落在**候选主行**（L1 那次样本内回测的产物，聚合结论写回它），
    `oos` 落在每一折各自那一行。两个值都存在，`window_role` 才有判别力。
    """

    IN_SAMPLE = "in_sample"
    OOS = "oos"

    @classmethod
    def values(cls) -> list[str]:
        return [m.value for m in cls]


class FoldPlanReason(str, Enum):
    """`plan_folds` 判「数据不足」时的原因码（进 `evidence`，供事后归因）。"""

    RANGE_TOO_SHORT = "range_too_short"
    OOS_REGION_TOO_SHORT = "oos_region_too_short"


@dataclass(frozen=True)
class FoldWindow:
    """一折的样本外窗口 + 它的样本内参照窗口（全部整日对齐，左闭右开）。"""

    fold_index: int
    oos_start: date
    oos_end: date
    in_sample_start: date
    in_sample_end: date
    days: int
    window_role: str = WalkForwardWindowRole.OOS.value

    def oos_range(self) -> tuple[str, str]:
        """喂给既有取数入口的 `(start_date, end_date)` ISO 串（右开）。"""
        return self.oos_start.isoformat(), self.oos_end.isoformat()

    def as_payload(self) -> dict[str, Any]:
        return {
            "fold_index": self.fold_index,
            "window_role": self.window_role,
            "start_date": self.oos_start.isoformat(),
            "end_date": self.oos_end.isoformat(),
            "days": self.days,
            "in_sample_start": self.in_sample_start.isoformat(),
            "in_sample_end": self.in_sample_end.isoformat(),
        }


@dataclass(frozen=True)
class FoldPlan:
    """分折几何（纯数据）。`data_sufficient=False` 时 `windows` 为空。"""

    start_date: date
    end_date: date
    total_days: int
    folds: int
    oos_fraction: float
    split_mode: str
    oos_total_days: int
    in_sample_span_days: int
    in_sample_start: date
    in_sample_end: date
    windows: tuple[FoldWindow, ...]
    data_sufficient: bool
    reason: str | None = None

    def as_payload(self) -> dict[str, Any]:
        return {
            "split_mode": self.split_mode,
            "folds": self.folds,
            "oos_fraction": self.oos_fraction,
            "total_days": self.total_days,
            "oos_total_days": self.oos_total_days,
            "in_sample_span_days": self.in_sample_span_days,
            "in_sample_start": self.in_sample_start.isoformat(),
            "in_sample_end": self.in_sample_end.isoformat(),
            "data_sufficient": self.data_sufficient,
            "insufficient_reason": self.reason,
        }


def _split_days(total_days: int, parts: int) -> list[int]:
    """把 `total_days` 天切成 `parts` 段，每段 >= 1 天，余数前倾。

    前倾（而不是把余数丢给最后一段）是为了让每一折的窗口长度尽量均匀：把余数堆在
    末尾会让最后一折明显更长，而衰减是各折等权聚合的——一折权重更大就等于聚合口径
    被窗口长度悄悄加权了。
    """
    base, remainder = divmod(total_days, parts)
    return [base + (1 if i < remainder else 0) for i in range(parts)]


def plan_folds(
    start_date: date,
    end_date: date,
    *,
    folds: int | None = None,
    oos_fraction: float | None = None,
    mode: str | None = None,
    config: FactoryConfig = CONFIG,
) -> FoldPlan:
    """把 `[start_date, end_date)` 切成 N 折样本外窗口（D4 / D15）。

    区间口径与既有取数入口一致（`end_date` 右开：`_load_ohlcv_for_backtest` 只收
    `timestamp + step <= end_dt` 的 bar），所以 `start=01-01 / end=01-02` 是 1 天。

    参数为 `None` 时取配置面（`config.wf_folds` / `wf_oos_fraction` / `wf_split_mode`）。
    区间不足以切出 N 个**每折至少一天**的窗口时**不抛异常**，而是返回
    `data_sufficient=False` 的计划——「数据不够」是一个结论，不是一次崩溃。
    """
    n_folds = config.wf_folds if folds is None else folds
    fraction = config.wf_oos_fraction if oos_fraction is None else oos_fraction
    split_mode = config.wf_split_mode if mode is None else mode

    # 与 config 的 `__post_init__` 同一组纪律：这里的入参可由调用方显式传（测试、
    # 单次重跑），所以边界要再守一遍，不能只依赖配置面。
    if split_mode not in WalkForwardSplitMode.values():
        raise ValueError(
            f"wf_split_mode 只接受 {WalkForwardSplitMode.values()}，当前 {split_mode!r}"
        )
    if n_folds < 2:
        raise ValueError(f"folds 必须 >= 2（单折没有样本外），当前 {n_folds}")
    if not 0 < fraction < 1:
        raise ValueError(f"oos_fraction 落在开区间 (0, 1)，当前 {fraction}")

    total_days = (end_date - start_date).days

    def _insufficient(reason: str) -> FoldPlan:
        return FoldPlan(
            start_date=start_date,
            end_date=end_date,
            total_days=max(total_days, 0),
            folds=n_folds,
            oos_fraction=fraction,
            split_mode=split_mode,
            oos_total_days=0,
            in_sample_span_days=0,
            in_sample_start=start_date,
            in_sample_end=start_date,
            windows=(),
            data_sufficient=False,
            reason=reason,
        )

    if total_days <= 0:
        return _insufficient(FoldPlanReason.RANGE_TOO_SHORT.value)

    # 用 Fraction 而不是 `total_days * fraction`：0.29 × 100 在 IEEE 下是
    # 28.999999999999996，`int()` 会静默少一天，而少一天就少一根 bar 的证据。
    oos_total_days = math.floor(Fraction(str(fraction)) * total_days)
    if oos_total_days < n_folds:
        # 每折至少要有一天，否则「折」这个单位就没有意义了。
        return _insufficient(FoldPlanReason.OOS_REGION_TOO_SHORT.value)

    in_sample_span_days = total_days - oos_total_days
    oos_region_start = start_date + timedelta(days=in_sample_span_days)

    windows: list[FoldWindow] = []
    cursor = oos_region_start
    for index, chunk_days in enumerate(_split_days(oos_total_days, n_folds)):
        oos_start = cursor
        oos_end = cursor + timedelta(days=chunk_days)
        cursor = oos_end
        in_sample_start = (
            start_date
            if split_mode == WalkForwardSplitMode.ANCHORED.value
            else oos_start - timedelta(days=in_sample_span_days)
        )
        windows.append(
            FoldWindow(
                fold_index=index,
                oos_start=oos_start,
                oos_end=oos_end,
                in_sample_start=in_sample_start,
                # 恒等于该折起点：参照窗口绝不越过样本外（口径 4）。
                in_sample_end=oos_start,
                days=chunk_days,
            )
        )

    return FoldPlan(
        start_date=start_date,
        end_date=end_date,
        total_days=total_days,
        folds=n_folds,
        oos_fraction=fraction,
        split_mode=split_mode,
        oos_total_days=oos_total_days,
        in_sample_span_days=in_sample_span_days,
        in_sample_start=start_date,
        in_sample_end=oos_region_start,
        windows=tuple(windows),
        data_sufficient=True,
        reason=None,
    )


def compute_decay_pct(
    is_sharpe: float | None, oos_sharpe: float | None
) -> float | None:
    """相对衰减（百分点）：`(is - oos) / is × 100`；算不出时返回 `None`。

    `None` 表示**判不了**（分母不可用、或样本外夏普不是有限数），不是「衰减 0」。
    调用方必须把它翻成 `data_sufficient=False`，绝不能拿 0 去顶——0 会一路放行。

    样本外夏普为**负**或**零**是合法输入，会得到 >100% 的衰减（必然超过任何合法上限），
    这正是要的行为：策略在样本外亏钱，衰减不该是「算不出来」。

    结果按 `DECAY_PRECISION` 定精度（理由见该常量的注释）：让「恰好 30%」在门槛上是
    一个确定的答案，而不是一次浮点抽签。
    """
    if not is_usable_metric_number(is_sharpe) or is_sharpe <= 0:
        # 分母必须为正。<=0 时这个比值没有意义（负夏普的「衰减」符号会翻转，
        # 变成「越差衰减越小」——比算不出来更危险）。
        return None
    if not is_usable_metric_number(oos_sharpe):
        return None
    return round((is_sharpe - oos_sharpe) / is_sharpe * 100.0, DECAY_PRECISION)


def _fold_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    """一折的落库事实 → 聚合载荷里的一个条目（D4 的 `folds[]`）。

    D4 点名的键（`fold_index` / `start_date` / `end_date` / `sharpe_ratio` /
    `total_trades`）全部保留，另加 `result_id` 与 `window_role`：用户故事 5/11 要求
    「依据哪几次回测」可复核，没有 `result_id` 这句话就答不出来。
    """
    return {
        "fold_index": row["fold_index"],
        "window_role": row.get("window_role", WalkForwardWindowRole.OOS.value),
        "result_id": row.get("result_id"),
        "start_date": row.get("start_date"),
        "end_date": row.get("end_date"),
        "sharpe_ratio": row.get("sharpe_ratio"),
        "total_trades": row.get("total_trades"),
        "in_sample_start": row.get("in_sample_start"),
        "in_sample_end": row.get("in_sample_end"),
    }


def aggregate_walk_forward(
    *,
    candidate_id: Any,
    is_sharpe: float | None,
    plan: FoldPlan,
    fold_rows: Sequence[Mapping[str, Any]],
    config: FactoryConfig = CONFIG,
) -> dict[str, Any]:
    """各折结果 → 落在**候选主行** `metrics["walk_forward"]` 的载荷（D4）。

    `fold_rows` 每项至少含 `fold_index` 与 `sharpe_ratio`（由 runner 从各折的
    `BacktestResult` 行装配）。判定顺序即「越根本的判不了越先报」：

        几何不够 → 样本内夏普不可用 → 折数不齐 → 某折夏普不可用

    任何一步不过都落 `data_sufficient=False` 并给出 `insufficient_reason`，
    绝不产出 `decay_pct`。
    """
    folds_payload = [_fold_payload(row) for row in fold_rows]

    def _insufficient(reason: str) -> dict[str, Any]:
        return {
            "candidate_id": str(candidate_id) if candidate_id else None,
            "window_role": WalkForwardWindowRole.IN_SAMPLE.value,
            "is_sharpe": is_sharpe,
            "oos_sharpe": None,
            "decay_pct": None,
            "fold_count": len(folds_payload),
            "expected_fold_count": plan.folds,
            "data_sufficient": False,
            "insufficient_reason": reason,
            "folds": folds_payload,
            "split": plan.as_payload(),
            "config_snapshot": config.snapshot(),
        }

    if not plan.data_sufficient:
        # 区间切不出 N 折：数据缺口不能伪装成通过（D6 的同一把尺）。
        return _insufficient(plan.reason or FoldPlanReason.RANGE_TOO_SHORT.value)

    if not is_usable_metric_number(is_sharpe) or is_sharpe <= 0:
        # 没有可用的样本内夏普就没有衰减的分母。`<=0` 同样算不可用（见 compute_decay_pct）。
        return _insufficient("in_sample_sharpe_unusable")

    if len(folds_payload) != plan.folds:
        # 折数不齐 = 少跑了（或某折没落库）。补齐前不给结论，否则衰减是在一个
        # 谁都不知道的分母/样本集上算出来的。
        return _insufficient("fold_count_mismatch")

    # 折身份必须**齐全且互不相同**：只数个数是不够的。同一折被重复计入（或某一折
    # 丢了、另一折多算一次）时，个数照样等于 `plan.folds`，但等权均值已经不再代表
    # 「每折各一段互不相同的未来」——它会被那一折重复加权。这种证据集缺陷必须在
    # 这里拦住，否则会产出一个看起来很确定的 `decay_pct`。
    indices = [entry.get("fold_index") for entry in folds_payload]
    if len(set(indices)) != len(indices) or set(indices) != set(range(plan.folds)):
        return _insufficient("fold_identity_mismatch")

    fold_sharpes: list[float] = []
    for row in folds_payload:
        sharpe = row.get("sharpe_ratio")
        if not is_usable_metric_number(sharpe):
            # 某折算不出有限夏普（权益曲线 <2 点、或标准差为 0）。把它当 0 参与均值
            # 会让衰减静默变小 → 放行，正是 #26 修掉的那类 fail-open。
            return _insufficient("fold_sharpe_unusable")
        fold_sharpes.append(float(sharpe))

    oos_sharpe = sum(fold_sharpes) / len(fold_sharpes)
    decay_pct = compute_decay_pct(is_sharpe, oos_sharpe)

    if decay_pct is None:
        # 理论上到不了（分母与各折都验过），留着是为了让「返回 None 就必须判不了」
        # 这条契约在代码里成立，而不是靠调用方相信前面的检查。
        return _insufficient("decay_not_computable")

    return {
        "candidate_id": str(candidate_id) if candidate_id else None,
        "window_role": WalkForwardWindowRole.IN_SAMPLE.value,
        "is_sharpe": is_sharpe,
        # 各折**等权**算术均值：spec 未给口径，取 walk-forward 通行做法。不用中位数
        # 是因为它会把「五折里两折崩了」平滑掉，而那正是风险承担者最该看见的信号；
        # 每折原始值都留在 folds[] 里，事后可复核、可重算。
        "oos_sharpe": oos_sharpe,
        "decay_pct": decay_pct,
        "fold_count": len(folds_payload),
        "expected_fold_count": plan.folds,
        "data_sufficient": True,
        "insufficient_reason": None,
        "folds": folds_payload,
        "split": plan.as_payload(),
        "config_snapshot": config.snapshot(),
    }
