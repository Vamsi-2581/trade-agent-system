"""Shadow 期每日记录的写入方（第①段单元 8i 的收尾）。

`deactivation_run.run_deactivation()` 的下游：把「今天机制看到了什么、本来会做什么」落成
一行 `ShadowDailyRecord`。四件事的来源全在**两个上层的返回值**里，本模块不重新取数。

## 为什么挂在推导的下游，而不是判定的下游

要落的东西有一半是推导的产物（`suggestions`：今天本来该停谁）。挂在判定之后没有建议可落，
挂在并行位置则要让两边各自去读一遍「当前生效阶段」——两处读到的可能不是同一条。顺序是硬的，
不是偏好。

## 一行一天，但「还没有结论」不算结论

心跳 5 分钟一轮，一天里绝大多数 tick 都会走到这里，所以这条路必须便宜且无副作用。但
**不能**简单的一锤定音（第一版写的 `get_or_create`，被一个数据到达时序毁掉）：

业务日 D 的第一轮心跳落在北京时间 08:00 前后，而 D-1 那根日线**正是在那一刻**收盘
（`candles.py` 的模块 docstring 写着这条时间关系）。数据管道晚几分钟，这一轮
`run_daily_judgement` 就回 `stale_candles`——判定宁可今天没有结论，也不用前天的行情签署
今天的结论——于是 D 这一天在表里就永久是一行空三值；5 分钟后数据到了、判定正常出结论，
那一行却再也改不动。而这张表存在的三个理由（成功标准①的一致率、自熔断频率条款、日报第②段
的今昨做差）要的都是「**这一天机制最终的结论**」，不是「这一天第一轮心跳恰好看没看到数据」。

所以写入分两种，判据只有一条——**那一行里有没有结论**（`base_regime` 是否为空）：

- **空结论的行是占位**：它只说明「截至那一刻还没有结论」，可以被当天后续的心跳**补写**成
  结论行（`OUTCOME_FILLED`）。
- **有结论的行是定论**：绝不被任何后续心跳改写。判定层已经钉住了三个值不会漂——同一天第二次
  成功的判定走 `_describe`，读的正是库里那一行——能漂的只有 `suggestions`（池化表翻代、
  豁免到期、被管集合变化），而建议**冻结**正是这张表的要求（Q5）。

反向不补：已经有结论之后又来一轮「没有结论」的心跳（任务停了一天又恢复），那一行照旧。
「记录是事件，不是缓存」这条纪律在它真正要保护的地方原样成立。

## 三值、`run_day`、外键都从 `payload` 取

- **三值从 `payload` 抄，不从库里再查一遍**。内联副本的全部意义是这张表能**独立**回答
  「那天机制判的是什么」；日报第②段的今昨做差与成功标准①的比对读的正是它。表要是跟推导
  那一刻的世界对不上，比对的就是两份不同的东西。
- **`run_day` 也从 `payload` 取**。`to_business(now).date()` 与判定层的 `run_day` 是同一个
  东西，但两处各取一次 `timezone.now()`，就在业务日界（北京 08:00）附近有了翻页窗口：判定
  算在 D、这里算成 D+1，于是三值属于 D、行却记在 D+1——**两个值都合法**，没有任何断言会响。
  判定层的 `run_day` 是权威（它才决定 `effective_at`，也就是记录的唯一键），所以用它。
- **外键指向三值的那一行**（`(symbol, effective_at)` 定位），所以「外键非空 ⟺ 三值非空」。
  不指向 `current_judgement`（今天**生效**的那条）：今天的判定按 `business_midnight(run_day
  + 1)` 生效，于是「今天生效的」永远是**昨天判的**，指向它等于让外键与三值天天差一天——
  那种错位看着处处自洽，最难查。日报第①段要讲的正是「今天判出 X（明早 08:00 起生效），
  今日生效的仍是 Y」，两句话各有出处，不是同一句话。

## 收场

四种（返回值里的 `outcome`），每一种都带一句给人看的话（`note`）：**日志不算被看见**，
`note` 的正经出口是日报。判定层失败或没有结论**不抛异常**——它是「今天没有结论」，不是
本层的失败，照落一行（这正是「判定跑了但机制没表态」能被数出来的原因）。数据库故障才抛，
往上让任务标 FAILURE。
"""

from __future__ import annotations

import logging
from datetime import date, datetime

from django.utils import timezone

from apps.common.time_utils import to_business
from apps.regime import config
from apps.regime.models import RegimeJudgement, ShadowDailyRecord

logger = logging.getLogger(__name__)

#: 今天还没有任何行 → 落了一条（可能是结论行，也可能是占位行）。
OUTCOME_CREATED = "created"
#: 已有一条占位行，这一轮拿到了结论 → 补写上去。
OUTCOME_FILLED = "filled"
#: 已有结论行 → 不动。
OUTCOME_KEPT_CONCLUSION = "kept_conclusion"
#: 已有占位行，这一轮仍没有结论 → 不动。
OUTCOME_KEPT_PLACEHOLDER = "kept_placeholder"


def write_shadow_record(
    judgement: dict,
    deactivation: dict,
    *,
    now: datetime | None = None,
) -> dict:
    """把这一轮判定 + 推导落成当天的 Shadow 记录，返回一行摘要。

    `judgement` / `deactivation` 是 `run_daily_judgement()` 与 `run_deactivation()` 的
    返回值（任务层把它们放在 `payload["regime"]` / `payload["deactivation"]`）。返回值进
    日志与调用方的返回值，不静默。
    """
    now = now or timezone.now()
    symbol = (
        judgement.get("symbol") or deactivation.get("symbol") or config.CANDLES.symbol
    )
    run_day = _run_day(judgement, now)

    base = judgement.get("base_regime") or ""
    escalation = judgement.get("escalation") or ""
    effective = judgement.get("effective_regime") or ""
    concluded = bool(base)

    # 推导层的收场：正常推导时是 `None`，落成空串（模型约定：空 = 正常推导）。
    derivation_skipped = deactivation.get("skipped") or ""
    suggestions = list(deactivation.get("suggestions") or [])
    record = _judgement_row(symbol, judgement, concluded=concluded)

    fields = {
        "judgement": record,
        "base_regime": base,
        "escalation": escalation,
        "effective_regime": effective,
        "derivation_skipped": derivation_skipped,
        "suggestions": suggestions,
        "suggested_count": len(suggestions),
        "note": _note(judgement, deactivation, concluded=concluded),
    }

    row, created = ShadowDailyRecord.objects.get_or_create(
        symbol=symbol, run_day=run_day, defaults=fields
    )
    if created:
        outcome = OUTCOME_CREATED
    elif not row.base_regime and concluded:
        # 补写占位行。条件里再带一次 `base_regime=""`：并发下另一个 tick 可能刚补写过，
        # 那一行的字段已经在同一个方向上，重复覆盖无害；带上条件只是为了**不经手**一个
        # 已经是结论行的行。改了几行由数据库回答，不靠本地那条 `if` 猜。
        filled = ShadowDailyRecord.objects.filter(pk=row.pk, base_regime="").update(
            **fields
        )
        if filled:
            outcome = OUTCOME_FILLED
        else:
            outcome = OUTCOME_KEPT_CONCLUSION
    elif row.base_regime:
        outcome = OUTCOME_KEPT_CONCLUSION
    else:
        outcome = OUTCOME_KEPT_PLACEHOLDER

    if outcome == OUTCOME_KEPT_CONCLUSION and row.suggested_count != len(suggestions):
        # 行是定论，清单已冻结。当天后续推导又产出了不同的建议时留一行日志：这是
        # 「机制本来会做的事在这一天里变了」的信号，冻结是对的，但变化本身要看得到。
        logger.info(
            "[regime] Shadow 建议清单已冻结：当天后续推导产出 %s 条，冻结的 %s 条不被改写",
            len(suggestions),
            row.suggested_count,
        )

    summary = {
        "symbol": symbol,
        "run_day": run_day.isoformat(),
        "outcome": outcome,
        "written": outcome in (OUTCOME_CREATED, OUTCOME_FILLED),
        "judged": concluded,
        "judgement_skipped": judgement.get("skipped"),
        "derivation_skipped": derivation_skipped,
        # 库里那一行现在的条数（不是这一轮产出的条数）：摘要说的是**现实**。
        "suggested_count": len(suggestions)
        if outcome in (OUTCOME_CREATED, OUTCOME_FILLED)
        else row.suggested_count,
        "note": fields["note"],
    }
    if summary["written"]:
        logger.info("[regime] Shadow 记录 %s", summary)
    return summary


def _run_day(judgement: dict, now: datetime) -> date:
    """这一行记在哪个业务日。

    判定层的 `run_day` 是权威（见模块 docstring）。它缺席时（测试、数据订正）才回落到
    本地时刻——回落值可能与判定层不一致，所以缺席这件事要留痕。
    """
    raw = judgement.get("run_day")
    if not raw:
        logger.warning("[regime] 判定返回值没有 run_day，按本地时刻推断 Shadow 运行日")
        return to_business(now).date()
    return date.fromisoformat(raw)


def _judgement_row(
    symbol: str, judgement: dict, *, concluded: bool
) -> RegimeJudgement | None:
    """三值所抄的那一行判定。查询键与 `judgement._record_once` 的唯一约束是同一对。

    顺便把「外键非空 ⟺ 三值非空」这条不变量对着库核一遍：两边不一致说明本层的取数口径
    已经跟判定层漂开（比如有人改了 `effective_at` 的算法），那正是要立刻看见的事。
    """
    raw = judgement.get("effective_at")
    row = None
    if raw:
        try:
            at = datetime.fromisoformat(raw)
        except (TypeError, ValueError):
            logger.warning("[regime] 判定返回值的 effective_at 解析不了：%r", raw)
        else:
            row = RegimeJudgement.objects.filter(symbol=symbol, effective_at=at).first()

    if concluded and row is None:
        logger.warning(
            "[regime] 判定出了结论却找不到对应记录（%s %s），Shadow 行只留内联三值",
            symbol,
            raw,
        )
    elif not concluded and row is not None:
        logger.warning(
            "[regime] 判定没有结论却找到了一条记录（%s %s），外键照指",
            symbol,
            raw,
        )
    elif row is not None and row.base_regime != (judgement.get("base_regime") or ""):
        logger.warning(
            "[regime] Shadow 内联三值与判定记录不一致：记录 %s，返回值 %s",
            row.base_regime,
            judgement.get("base_regime"),
        )
    return row


def _note(judgement: dict, deactivation: dict, *, concluded: bool) -> str:
    """这一行为什么是这样，一句话。

    推导层已经给了一句（冷启动 / 状态过期 / 没有池化表三种收场都有），那就原样带上——
    那句话是推导层对自己的解释，第二处重写迟早会跟它漂开。只在它空缺时自己补一句。
    """
    if concluded:
        head = f"判定出结论（{judgement.get('effective_regime')}）"
    else:
        head = (
            f"今日判定未出结论（{judgement.get('skipped') or '未说明'}），"
            "保持上一有效状态"
        )
    tail = (deactivation.get("note") or "").strip()
    if not tail:
        count = len(deactivation.get("suggestions") or [])
        tail = (
            "推导正常，无停用建议" if count == 0 else f"推导正常，建议停用 {count} 条"
        )
    return f"{head}；{tail}"
