"""机制 Web 读面的序列化层（P5 子票 #41）。

三个只读端点（`overview` / `pool` / `pool/generations`）共用这一份「取值 → 展示」的换算。
**这个文件里没有一个中文字面量**（除了本 docstring）：每一个 display 都从枚举或词表自己
那里取，一处都不抄。理由与 map #40 的「渲染分界」同源：

- `MechanismMode.display` 已经是「Shadow（只记录，不执行）」，前端再写一遍就是同一件事
  的第二个说法；
- `slice.STATE_DISPLAY` / `slice.REASON_DISPLAY` 是**落库契约**的展示侧（`slice.py` 的
  注释写着「加了一个状态却忘了给它展示名会在 `KeyError` 上立刻响」）。这里再复制一份，
  那个 `KeyError` 就永远不会响了——两份表各自自洽，只有人比对时才看得出分叉。

所以本模块只做三件事：**查表**（`.display` / `STATE_DISPLAY[...]`）、**配对**
（`{"value", "display"}`）、**兜底**（认不出来原样返回，不猜）。

## 认不出来的取值原样回显，不回落成某一档

模型上的 `choices` 只在表单校验里管用，手工写进去的行（迁移回填、补录）绕得过它。一条
脏行不该让整张池化表炸掉；但它更不该被显示成「适用」——那会让人以为机制放行了某条
策略。`deactivation_run.regime_display` 与 `report._regime_display` 早已立下这条规矩，
本模块照办，并且**复用** `regime_display` 而不另写一份（它连「认不出的阶段」这句后缀都
是同一处）。

## `reason` 为空是「没有原因」，不是「原因缺失」

`RegimePoolCell.reason` 的 `blank=True, default=""`：`fit` 那类格子本来就没有原因码。
给空串配一句「（无）」会让前端把「没有原因」显示成一个像是缺数据的占位符。空串的
display 就是空串，前端按「空则整段不渲染」处理（`regime_queries` 的
`state_text += f"：{...}"` 只在该值为真时追加，同一个读法）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Mapping

from apps.regime import deactivation_run
from apps.regime import slice as sl
from apps.regime.quant import PRIORITY
from apps.regime.models import (
    ActorKind,
    MechanismKind,
    MechanismMode,
    PoolSource,
    RebuildStatus,
    RegimeMechanismSwitch,
    RegimePoolRebuild,
)


# --------------------------------------------------------------------------- #
# 小件
# --------------------------------------------------------------------------- #


def iso(moment: Any) -> str | None:
    """时刻 → ISO 字符串；没有就是 `None`（不是空串、不是 `"null"`）。

    **参数标成 `Any` 是刻意的**：传进来的是 Django 的 `DateTimeField` 列，运行时一定是
    `datetime` 或 `None`，但本仓没有装 `django-stubs`（`pyrightconfig.json` 是 basic 模式，
    全仓模型属性都被报成字段描述符），标成 `datetime | None` 只会让每个调用点各红一次。
    这里把「模型列在运行时是什么」这件事收在一个函数里，而不是在每处调用点各写一遍
    `cast`。真正要守的契约由测试钉住（`iso(None) is None`、返回值是 ISO 串）。
    """
    if moment is None:
        return None
    return moment.isoformat() if isinstance(moment, datetime) else str(moment)


def _labelled(value: Any, display: str) -> dict[str, str]:
    """枚举的传输形状：**取值与展示名成对**，前端不重拼。

    不拆成 `state` + `state_display` 两个平级键，是因为那样前端很容易只取前一个（它更
    短），而少一个 display 不会报错、只会静默显示 slug——这正是本 map 要防的形状。
    成对之后，「拿到值就有它的说法」是结构上的保证，不是纪律上的。
    """
    return {"value": value, "display": display}


def _enum_display(enum_cls, value: Any) -> str:
    """枚举取值 → 展示名。认不出来原样返回，**不猜、不回落**。"""
    try:
        return enum_cls(value).display
    except ValueError:
        return value


# --------------------------------------------------------------------------- #
# 单个枚举的传输形状
# --------------------------------------------------------------------------- #


def regime_payload(value: Any) -> dict[str, str]:
    """行情阶段。展示名走 `deactivation_run.regime_display`（脏值有「认不出的阶段」后缀）。"""
    return _labelled(value, deactivation_run.regime_display(value))


def _from_table(table: Mapping[str, str], value: Any) -> str:
    """查展示名表。**查不到就原样回显**——不猜、不回落成某一档。

    抽成一个函数是为了让「查表」这件事只有一处：三个调用点各写一遍 `.get(v, v)` 时，
    某一个写成 `.get(v)` 就会静默给出 `None`，而 `None` 在 JSON 里是 `null`，前端拿它
    拼进模板只会显示成空白——一种没有报错路径的错。
    """
    display = table.get(value)
    return display if display is not None else str(value)


def state_payload(value: Any) -> dict[str, str]:
    """池化格子的状态。表在 `slice.STATE_DISPLAY`——这里只查表，不复制。"""
    return _labelled(value, _from_table(sl.STATE_DISPLAY, value))


def reason_payload(value: Any) -> dict[str, str]:
    """池化格子的原因码。表在 `slice.REASON_DISPLAY`。空串的 display 也是空串。"""
    if not value:
        return _labelled("", "")
    return _labelled(value, _from_table(sl.REASON_DISPLAY, value))


def source_payload(value: Any) -> dict[str, str]:
    """结论来源（本策略切片 / 原型兜底），表在 `PoolSource`。"""
    return _labelled(value, _enum_display(PoolSource, value))


def mode_payload(mode: MechanismMode) -> dict[str, str]:
    """机制档位。表在 `MechanismMode`（「Shadow（只记录，不执行）」那句就是它）。"""
    return _labelled(mode.value, mode.display)


def status_payload(value: Any) -> dict[str, str]:
    """一代池化表的状态（重算中 / 已就绪 / 失败）。表在 `RebuildStatus`。"""
    return _labelled(value, _enum_display(RebuildStatus, value))


def actor_kind_payload(value: Any) -> dict[str, str]:
    """触发方类别（命令行 / 定时任务 / 聊天渠道）。表在 `ActorKind`。"""
    return _labelled(value, _enum_display(ActorKind, value))


def kind_payload(kind: MechanismKind) -> dict[str, str]:
    """三个开关里的哪一个。表在 `MechanismKind`。"""
    return _labelled(kind.value, kind.display)


def event_impact_payload(event: Any) -> dict[str, str]:
    """一条重大事件的冲击档位。

    **收事件对象、不收 slug**：`MajorEvent.impact_display` 已经是「这一条事件怎么说法」的
    唯一出处（`Model.impact_display` 的 docstring），而收 slug 会逼调用方再去查一张枚举表
    ——那就是「事件的展示名」的第二个说法。sigil 与其它 payload 同形（成对），所以前端
    仍然只认 `{value, display}` 这一种形状。
    """
    return _labelled(event.impact, event.impact_display)


def event_status_payload(event: Any) -> dict[str, str]:
    """一条重大事件的生命周期（排期 / 已取消）。展示名取模型自己的 property。"""
    return _labelled(event.status, event.status_display)


def event_scope_payload(event: Any) -> dict[str, str]:
    """一条重大事件的作用域（全市场 / 指定品种）。展示名取模型自己的 property
    ——品种列表那一支是「、」连起来的，只有模型知道怎么拼它。"""
    return _labelled(event.scope_kind, event.scope_display)


# --------------------------------------------------------------------------- #
# 开关卡片
# --------------------------------------------------------------------------- #


def switch_payload(
    kind: MechanismKind, latest: RegimeMechanismSwitch | None
) -> dict[str, Any]:
    """一个开关的当前档 + 最近一次切换。**只吃一行流水**，不重算任何东西。

    `mode` 的取值规则与 `RegimeMechanismSwitch.current()` 逐字相同：最后一条流水的
    `to_mode`，一条流水都没有就是 `SHADOW`。这里**没有再调一次 `current()`**：那是一次
    与本函数入参完全相同的查询（`current()` 内部就是 `latest()`），两次查同一张表只为
    了让一行 Python 从别处来，不划算。代价是这条规则在仓里出现了两处，所以
    `test_views` 里有一条用例拿 `current()` 逐档对账（`_mode_of` 那一条）——分叉会在
    那里变红，而不是在页面上变成一个看不出错的档位。

    `never_switched` 是给前端的一个**布尔**，不是一句文案：「从未切换过」怎么排版属于
    前端，而「有没有切换过」是数据。
    """
    mode = _mode_of(latest)
    return {
        "kind": kind_payload(kind),
        "mode": mode_payload(mode),
        "latest": None if latest is None else _switch_row(latest),
        "never_switched": latest is None,
    }


def _mode_of(latest: RegimeMechanismSwitch | None) -> MechanismMode:
    """最后一条流水的 `to_mode`；没有流水即 `SHADOW`（与 `current()` 同一条规则）。"""
    return MechanismMode(latest.to_mode) if latest is not None else MechanismMode.SHADOW


def _switch_row(row: RegimeMechanismSwitch) -> dict[str, Any]:
    """一行切换流水：什么时候、谁、从哪档到哪档、为什么。

    四件事就是 `RegimeMechanismSwitch` 的 docstring 说它存在的理由，一件都不省：
    「为什么」缺失时这张卡片回答不了「今天为什么动过」，而那是这一页存在的一半。
    """
    return {
        "at": iso(row.at),
        "actor_kind": actor_kind_payload(row.actor_kind),
        "actor_name": row.actor_name,
        "from_mode": mode_payload(MechanismMode(row.from_mode)),
        "to_mode": mode_payload(MechanismMode(row.to_mode)),
        "reason": row.reason,
    }


# --------------------------------------------------------------------------- #
# 一代池化表
# --------------------------------------------------------------------------- #


def _mapping(raw: Any) -> Mapping[str, Any]:
    """把一个 JSON 列读成映射。**不是映射就当空**：`exclusions` 的键集由
    `pool_rebuild.EXCLUSION_KEYS` 保证，一条手工写坏的行不该让整页 500。"""
    return raw if isinstance(raw, Mapping) else {}


def generation_summary(generation: RegimePoolRebuild) -> dict[str, Any]:
    """一代重算记录的全貌：状态 / 触发方 / 时刻 / 三个计数 / 排除计数 / 失败原因。

    `exclusions` **原样透出**（`pool_rebuild.EXCLUSION_KEYS` 保证键集恒定，三个键计数为 0
    也在）。它是「看了 N 个回测、用上 M 个，差额去哪了」的唯一答案：按原因分开，因为
    「本金为 0」要人去查回测参数，「没切过片」只要跑一次重算入口——混成一个数就没法决定
    该做哪个。前端只负责把这三行排出来，不负责翻译。
    """
    return {
        "id": generation.pk,
        "status": status_payload(generation.status),
        "actor_kind": actor_kind_payload(generation.actor_kind),
        "actor_name": generation.actor_name,
        "pool_version": generation.pool_version,
        "started_at": iso(generation.started_at),
        "finished_at": iso(generation.finished_at),
        "candidates": generation.candidates,
        "results_used": generation.results_used,
        "cells_total": generation.cells_total,
        "cells_changed": generation.cells_changed,
        # 列是 `JSONField`，运行时是 dict 或 None（`default=dict`）。这里的 isinstance
        # 不是防御性编程，是把「模型列在运行时是什么」收在一处，理由同 `iso`。
        "exclusions": dict(_mapping(generation.exclusions)),
        "failure": generation.failure,
    }


def cell_payload(row: Mapping[str, Any], strategy_name: str) -> dict[str, Any]:
    """一格：策略 + 阶段 + 来源 + 状态 + 原因 + 待复核 + 依据原文。

    `evidence` **原样透出，一个键都不改**：它是这张表的存在理由（哪几个回测、各多少笔、
    覆盖什么区间、合并成交数、品种、原型归一化留痕）。任何「顺手整理一下」的加工都会让
    「依据哪几个回测」在传输层变成一个派生值，而审计要的是原文。
    """
    return {
        "strategy": {"id": str(row["strategy_id"]), "name": strategy_name},
        "regime": regime_payload(row["regime"]),
        "source": source_payload(row["source"]),
        "state": state_payload(row["state"]),
        "reason": reason_payload(row["reason"]),
        "needs_review": bool(row["needs_review"]),
        "evidence": row["evidence"] if row["evidence"] is not None else {},
    }


def triple_payload(triple: tuple[str, str, str] | None) -> dict[str, Any] | None:
    """差异里的一侧：`(状态, 原因, 来源)` 三元组，与 `pool_rebuild._cell_changes` 同源。

    差异**只比这三样**，不比 `evidence`：证据每天都在动（多一笔成交、窗口多一天），拿它
    当差异会让「这次重算改变了什么」恒等于格子总数。这条定义住在
    `pool_rebuild._cell_changes` 的 docstring 里，这里照抄它的口径并**不另立一套**——
    所以 `cells_changed` 与端点算出的差异条数在正常历史上必须相等（`test_views` 钉住）。
    """
    if triple is None:
        return None
    state, reason, source = triple
    return {
        "state": state_payload(state),
        "reason": reason_payload(reason),
        "source": source_payload(source),
    }


def cell_key(strategy_id: Any, regime: str) -> str:
    """一格的稳定标识 `"<策略 id>|<阶段>"`。前端拿它做 key / 行内展开的锚。

    不用 `(策略, 阶段)` 的数组：JSON 的数组当 key 要前端自己 `join`，而那一步的拼接分隔
    符一旦两处不同（`|` 与 `,`），展开状态就会串行。
    """
    return f"{strategy_id}|{regime}"


#: 矩阵的固定列序（`quant.PRIORITY` 的顺序：高波动 → 下行 → 上行 → 箱体）。
#:
#: 列顺序由后端给，不由前端猜：前端按数组顺序排，才不会出现两处各有一份顺序、而两份都
#: 「看起来对」。`PRIORITY` 是**判定**优先级，拿它当列序是借用它的稳定性，不是声称列序
#: 有判定含义——四档都列出来，没有优先级之分。
ORDERED_REGIMES: tuple[str, ...] = tuple(regime.value for regime in PRIORITY)

#: 阶段取值 → 列序下标。`ordered_cell_keys` 用它排同一策略内的四格。
_REGIME_ORDER: dict[str, int] = {
    value: index for index, value in enumerate(ORDERED_REGIMES)
}


def ordered_cell_keys(
    cells: Mapping[tuple[Any, str], Any], names: Mapping[Any, str]
) -> list[tuple[Any, str]]:
    """格子的稳定遍历顺序：策略名 → 策略 id → 阶段列序。

    先按名字：这一页是给人看的矩阵，行按名字排才好扫。id 兜底是为了同名策略（`Strategy`
    没有唯一约束）不会因为排序不稳定而在两次请求间换行——那会让行内展开看起来「自己
    合上了」。
    """
    return sorted(
        cells,
        key=lambda key: (
            names.get(key[0], ""),
            str(key[0]),
            _REGIME_ORDER.get(key[1], len(_REGIME_ORDER)),
        ),
    )


def strategy_ids(cells: Iterable[tuple[Any, str]]) -> list[Any]:
    """格子里出现过的策略 id（去重、保序）。"""
    seen: dict[Any, None] = {}
    for strategy_id, _ in cells:
        seen.setdefault(strategy_id, None)
    return list(seen)
