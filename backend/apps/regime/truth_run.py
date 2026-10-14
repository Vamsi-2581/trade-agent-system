"""人工真值的取数与落库（CONTEXT.md 第 164 条）：`truth.py` 的另一半。

`truth.py` 是纯函数（不吃 DB、不读时钟），本模块负责「区间从哪来、写回哪里、给人看什么」，
与 `deactivation_run` 之于 `deactivation` 是同一种分工。

## 两条路的口径刻意不同

| 路 | 什么时候走 | 回显什么 |
|----|-----------|---------|
| **录入**（`add`） | 人在还不知道算法结论时标注 | **一个算法数字都不回显**（第 164 条） |
| **浏览**（`roster_lines` / `agreement`） | 事后查 | 一致率、分母、缺口、错向、资讯抬升日，全都摊开 |

第一条是第 164 条的**可执行形式**。「先标完再看算法输出」这句话拦不住任何东西——人只要
愿意，去查库、去开体检页，算法今天怎么判的一目了然。机制唯一能做的，是不在**录入的那
一次交互里**把结论递到眼前：把人看到的算法结论与他自己刚写下的标注并排放，标注就会被
锚定，而「一致率」随后测的是「人同不同意自己刚才看见的东西」。所以 `add` 的回复只报
**人自己输入的东西**（段数、天数、跟已有区间打不打架——那全是人工数据），一个算法侧的
数字都不出现。`roster_lines` 不受这条约束：它是事后查的，标注已经写死了。

## 撤回而不是删除

`retract` 只写 `retracted_at` 那一组三列，行的其余部分一个字不动（原文留在行里）。整批
拒绝语义与 `deactivation_run.revoke` 逐字同源：**先查存在性，缺一个就整批不写**——报错时
人本来就已经在「我刚写的是哪条」这件事上不确定了，这时候把其中几条悄悄撤掉，收场是半张
表被改过、屏幕上只有一句错误。

## 没有管理命令入口

`/regime label` 是**唯一**入口，刻意不给它配 `manage.py` 的孪生命令（`/regime mech` 那一档
同样只有斜杠入口）。理由不是省事：豁免有 CLI 孪生命令，是因为豁免会在事故当口被运维从
终端发出；而真值标注是一件**坐下来做一次**的事（第 164 条只收「已知区间」，一段一段地回忆），
入口多一个，就多一处「谁在什么时候标了什么」要对齐的落款。真要补 CLI，`add` / `retract`
自己就是共享层，直接调即可——本模块没有一行逻辑是给斜杠命令专用的。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable, Sequence

from django.utils import timezone

from apps.common.time_utils import format_business
from apps.regime import events, judgement, slicing, truth
from apps.regime.deactivation_run import regime_display
from apps.regime.models import ActorKind, RegimeTruthInterval
from apps.regime.quant import BaseRegime

logger = logging.getLogger(__name__)

#: 日期只认这一种写法。**不做多格式容错**：`03/04/2024` 在不同写法里是三月四日或四月三日，
#: 而真值是**比对的基准**——一个被猜到意思的日期会静默地错到底，且错得看不出来。
_DAY_HINT = "日期要写成 2024-01-05 这样（年-月-日）"


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #


def parse_day(raw: str) -> date:
    """`2024-01-05` → `date`。"""
    text = (raw or "").strip()
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise truth.TruthInputError(
            f"认不出的日期：{text or '（空）'}——{_DAY_HINT}"
        ) from exc


def parse_regime(raw: str) -> BaseRegime:
    """档位那一项。**只认 slug**，中文名由入口层翻（`/regime label` 的 `_regime_of`）。

    与 `deactivation_run.resolve_regime` 同一条：共享层只认 slug，中文表留在各自入口那一层
    ——在共享层再加一张中文表，就是给「阶段叫什么」造第二套答案。
    """
    text = (raw or "").strip()
    for member in BaseRegime:
        if text == member.value:
            return member
    names = " / ".join(member.display for member in BaseRegime)
    raise truth.TruthInputError(f"认不出的档位：{text or '（空）'}（四档是：{names}）")


# --------------------------------------------------------------------------- #
# 读
# --------------------------------------------------------------------------- #


def _as_interval(row: RegimeTruthInterval) -> truth.Interval:
    """库里的行 → 纯层的区间。**纯层的 `Interval` 不认识 Django 模型**。"""
    return truth.Interval(
        start=row.start_date,
        end=row.end_date,
        regime=BaseRegime(row.regime),
        note=row.note,
        id=row.pk,
    )


def live_intervals() -> list[truth.Interval]:
    """活着的（没被撤回的）区间，按时间升序。"""
    return [
        _as_interval(row)
        for row in RegimeTruthInterval.objects.filter(retracted_at__isnull=True)
    ]


def agreement(
    intervals: Sequence[truth.Interval] | None = None,
    *,
    tags: dict[date, BaseRegime] | None = None,
    in_force: dict[date, tuple[BaseRegime, BaseRegime]] | None = None,
) -> truth.Agreement:
    """活区间 × 历史量化标签 → 一次比对结果。

    `tags` 由调用方传进来是有意的，与 `slicing.compute_slice` 同一条理由：标签是全市场
    共用的一份（同一个 symbol、同一套参数），一次请求里要算好几处的话不该重算。传
    `None` 就在这里 `load_tags()`——那一次是**全量重算**（标签不落库，见 `slicing` 的
    docstring），体检页每开一次都会发生一次，代价十毫秒级。

    `in_force`（与 `tags` 同一条理由：可传可算）是区间跨度内**在生效的判定**，用来把资讯
    抬升日摘出分母。一段区间都没有时不查库——空清单本来就没有可摘的天。
    """
    intervals = live_intervals() if intervals is None else list(intervals)
    return truth.tally(
        intervals,
        slicing.load_tags() if tags is None else tags,
        in_force=_in_force(intervals) if in_force is None else in_force,
    )


def _in_force(
    intervals: Sequence[truth.Interval],
) -> dict[date, tuple[BaseRegime, BaseRegime]]:
    """区间跨度内在生效的判定（一次查询走完整个跨度，见 `judgement.in_force_days`）。"""
    if not intervals:
        return {}
    return judgement.in_force_days(
        min(interval.start for interval in intervals),
        max(interval.end for interval in intervals),
    )


# --------------------------------------------------------------------------- #
# 写
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AddOutcome:
    """一次录入的结果。

    `live_count` / `live_days` 是**录入之后**的全貌（不是这一段自己的），因为人问的下一句
    总是「现在一共标了多少」。`new_conflicts` 是这一次录入**新造出来**的冲突日——注意它是
    相对录入之前算的（`after - before`），不是「新段与老段重叠的天数」：新段落在**已经**
    冲突的日子里，那一天本来就不在分母里，不能算在这一次头上。
    """

    row: RegimeTruthInterval
    live_count: int
    live_days: int
    new_conflicts: tuple[date, ...]
    overlaps: tuple[RegimeTruthInterval, ...]
    conflicts_with: tuple[RegimeTruthInterval, ...]


@dataclass(frozen=True)
class RetractOutcome:
    """一次撤回的结果。`ids` 是**这一次真的撤掉的**那几条，`already` 是本来就已撤回的。"""

    ids: tuple[int, ...]
    retracted: int
    already: int


def add(
    *,
    start: date,
    end: date,
    regime: BaseRegime,
    actor_kind: str,
    actor_name: str,
    note: str = "",
    now: datetime | None = None,
) -> AddOutcome:
    """录入一段。**允许与已有区间重叠**（第 164 条：重叠本身不是错，标重了才是）。

    重叠的两段**档位一致** ⇒ 互相佐证，重叠的天只算一天；档位不同 ⇒ 那些天成为冲突日、
    退出分母（`truth.tally`）。两种都在回复里当场说出来：不说的话，「天数没有增加」看起来
    与「这次没写进去」一模一样。

    `Interval` 先构造一遍**再落库**，写进去的值从它身上取：模型上的 `CheckConstraint` 也会
    拦「终点早于起点」，但那一步抛的是 `IntegrityError`——用户看到的是一个数据库错误，
    而不是「区间终点早于起点」。校验留在纯层，落库只是它的下游。
    """
    spec = truth.Interval(start=start, end=end, regime=regime)
    now = now or timezone.now()

    before = live_intervals()
    conflicts_before = truth.conflicting_days(before)

    row = RegimeTruthInterval.objects.create(
        start_date=spec.start,
        end_date=spec.end,
        regime=spec.regime.value,
        note=note,
        actor_kind=actor_kind,
        actor_name=actor_name,
        # `created_at` 是 `auto_now_add`：录入时刻由 DB 侧盖，不由这里传——传进来的 `now`
        # 是给「同一次请求里的其它判断」用的，两者混用会让时间戳的来路有两种。
    )
    logger.info(
        "[regime] 人工真值录入 #%s：%s ~ %s %s by %s(%s)",
        row.pk,
        spec.start,
        spec.end,
        spec.regime.value,
        actor_name,
        actor_kind,
    )

    after = [*before, _as_interval(row)]

    # 「与已有区间重叠」按**活区间**算：与已撤回的区间重叠不改变任何比对结果，报出来
    # 只会让人以为自己标重了。
    overlaps = tuple(
        other
        for other in before
        if max(spec.start, other.start) <= min(spec.end, other.end)
    )
    return AddOutcome(
        row=row,
        live_count=len(after),
        live_days=len(truth.day_regimes(after)),
        new_conflicts=tuple(sorted(truth.conflicting_days(after) - conflicts_before)),
        overlaps=overlaps,
        conflicts_with=tuple(o for o in overlaps if o.regime != spec.regime),
    )


def retract(
    raw_ids: Iterable[str],
    *,
    actor_kind: str,
    actor_name: str,
    now: datetime | None = None,
) -> RetractOutcome:
    """按 id 撤回。**先查存在性，缺一个就整批不写**（`deactivation_run.revoke` 同形）。

    条件更新（`retracted_at__isnull=True`）+ 先到者为准：已经撤过的保持原样，连撤回人都不
    覆盖——「谁撤的」正是事后复盘要看的那一格，后来的那次批量撤回不该把它改写。

    先读一遍「哪些是活的」再按那个集合更新：`update()` 的返回值本身就是「真正改了几行」，
    但**名字**（哪几条）只有读一次才知道。两次之间理论上能被第三个人插进来，那会让这一行
    的名字比实际多算一条——这是一条人工路径，代价是清单上一行备注，比在这里上锁便宜。
    """
    now = now or timezone.now()
    wanted = [parse_row_id(raw) for raw in raw_ids]
    found = set(
        RegimeTruthInterval.objects.filter(id__in=wanted).values_list("id", flat=True)
    )
    missing = [str(i) for i in wanted if i not in found]
    if missing:
        raise truth.TruthInputError(
            f"这些区间 id 不存在：{'、'.join(missing)}。"
            "不带动作列一次清单就能看到现有的 id。"
        )

    live_ids = list(
        RegimeTruthInterval.objects.filter(
            id__in=wanted, retracted_at__isnull=True
        ).values_list("id", flat=True)
    )
    retracted = RegimeTruthInterval.objects.filter(
        id__in=live_ids, retracted_at__isnull=True
    ).update(
        retracted_at=now,
        retracted_by_kind=actor_kind,
        retracted_by_name=actor_name,
    )
    logger.info(
        "[regime] 人工真值撤回 %s 段：%s by %s(%s)",
        retracted,
        "、".join(f"#{i}" for i in sorted(live_ids)) or "（无）",
        actor_name,
        actor_kind,
    )
    return RetractOutcome(
        ids=tuple(sorted(live_ids)),
        retracted=retracted,
        already=len(wanted) - retracted,
    )


def parse_row_id(raw: str) -> int:
    """`#3` / `3` 都认（清单里带 `#`，人照着抄的时候不该被它绊住）。"""
    text = (raw or "").strip().lstrip("#")
    if not text.isdigit():
        raise truth.TruthInputError(
            f"认不出的区间 id：{raw or '（空）'}（应该是一个整数）"
        )
    return int(text)


# --------------------------------------------------------------------------- #
# 渲染
# --------------------------------------------------------------------------- #


def add_summary(outcome: AddOutcome) -> tuple[str, ...]:
    """录入之后的那几行。**一个算法数字都没有**（见模块 docstring 的两条路）。"""
    row = outcome.row
    lines = [
        f"已录入 #{row.pk}：{row.start_date} ~ {row.end_date}"
        f"（{row.days} 天）判为「{regime_display(row.regime)}」",
        f"人工标注现有 {outcome.live_count} 段，共 {outcome.live_days} 天（各段覆盖日的并集）",
        "本次录入**没有回显任何算法结论**（第 164 条）：先标完再看算法输出，"
        "标出来的才是独立的真值，而不是算法的回声。",
    ]
    if outcome.new_conflicts:
        partners = "、".join(
            f"#{o.id or '?'}（{regime_display(o.regime.value)}）"
            for o in outcome.conflicts_with
        )
        lines.append(
            f"⚠️ 与已有区间 {partners} 的档位不同，本次新造出 "
            f"{len(outcome.new_conflicts)} 个冲突日（{outcome.new_conflicts[0]} 起）："
            "那些天人工真值自己打架，**不计入一致率的分母**。"
            "若是标错了，用 /regime label rm 撤掉那一段再重录。"
        )
    elif outcome.overlaps:
        lines.append(
            f"· 与已有 {len(outcome.overlaps)} 段区间重叠，档位一致：重叠的天只算一天"
            "（两段互为佐证），覆盖天数没有按两段相加。"
        )
    lines.append("要看清单与一致率：/regime label")
    return tuple(lines)


def retract_summary(
    outcome: RetractOutcome, *, now: datetime | None = None
) -> tuple[str, ...]:
    """撤回之后的那几行。**原文留在行里**，所以这里只说撤了哪几条、什么时候撤的。"""
    if not outcome.retracted:
        # 整批都是已撤回的：一个「已撤回 0 段」听起来像动作失败，而它其实什么都没做错。
        return (
            f"这 {outcome.already} 段本来就都已撤回，本次没有改动任何一行"
            "（先到者为准，不覆盖先手的撤回人与撤回时刻）。",
            "要看清单：/regime label",
        )
    ids = "、".join(f"#{i}" for i in outcome.ids)
    lines = [
        f"已撤回 {outcome.retracted} 段人工标注（{ids}）· "
        f"{events.format_moment(now or timezone.now())}",
        "撤的是**这一段算不算数**：行还在库里，原文与录入人一个字没动，"
        "撤回人与撤回时刻记在同一行上。",
    ]
    if outcome.already:
        lines.append(
            f"  另外 {outcome.already} 段本来就已撤回，保持原样"
            "（先到者为准，不覆盖先手的撤回人）"
        )
    lines.append("要看清单与一致率：/regime label")
    return tuple(lines)


def interval_line(row: RegimeTruthInterval) -> str:
    """清单里的一行。**行列格式只有这一处**。

    时刻用 `format_business`（单口径、带时区标注）而不是 `events.format_moment` 的双口径：
    清单是**浏览**用的，一行一段、每段两个时刻，双口径会把一行撑成三行；要逐字核对时刻的
    地方是切换流水那一类（那里用双口径）。
    """
    line = (
        f"  #{row.id}  {row.start_date} ~ {row.end_date}（{row.days} 天）"
        f"  {regime_display(row.regime)}"
        f"  录入 {ActorKind(row.actor_kind).display}·{row.actor_name}"
        f" · {format_business(row.created_at)}"
    )
    if row.note:
        line += f"  备注：{row.note}"
    if row.retracted_at is not None:
        line += (
            f"  【已撤回 {format_business(row.retracted_at)}"
            f" 由 {ActorKind(row.retracted_by_kind).display}·{row.retracted_by_name}】"
        )
    return line


def roster_lines() -> tuple[str, ...]:
    """真值清单 + 一致率。**`/regime label` 与体检页第①段共用同一段渲染**（`truth.describe`）。

    已撤回的那些**不进清单正文**：清单是「拿什么在比对」的答案，把撤回的混进去，读的人
    会以为它们还在算。但撤回数必须报——一段区间悄悄从分母里消失，看起来与「从来没过」
    一样。

    不收 `now`：这一页上没有任何一句依赖「现在几点」（时刻全部来自行自己的
    `created_at` / `retracted_at`）。多一个不用的入参，下一个人会以为它管着什么。
    """
    live = list(RegimeTruthInterval.objects.filter(retracted_at__isnull=True))
    retracted = RegimeTruthInterval.objects.filter(retracted_at__isnull=False).count()

    if not live and not retracted:
        return (
            "还没有任何人工标注区间。",
            "  录入：/regime label add <起> <终> <档位> [备注…]",
        )

    lines = []
    if live:
        lines.append(f"人工标注区间（活 {len(live)} 段）：")
        lines.extend(interval_line(row) for row in live)
    else:
        lines.append("人工标注区间：一段活的都没有（全部已撤回）")
    if retracted:
        lines.append(f"  已撤回 {retracted} 段（行仍在库里、原文未动，只是不算数）")

    lines.append("")
    lines.extend(truth.describe(agreement()))
    return tuple(lines)
