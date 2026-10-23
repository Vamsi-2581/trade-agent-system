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
    EventImpact,
    EventStatus,
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
    """触发方类别（命令行 / 定时任务 / 聊天渠道 / 网页）。表在 `ActorKind`。"""
    return _labelled(value, _enum_display(ActorKind, value))


def outcome_payload(value: str, *, generation: int | None = None) -> dict[str, str]:
    """一次重算的收场。**词表在 `pool_rebuild`**（那是判据的家），这里只配对。

    收场不是模型上的枚举——它由 `pool_rebuild.outcome_of()` 从「状态 + 两个布尔 + 失败
    原文」折出来，所以没有 `Model.choices` 可查。展示名仍由后端给（map #40 的渲染分界），
    前端不重拼。

    `generation` 只对 `reused` 有意义（「复用第 N 代」），且它**不是词表的一部分**：编号
    是数据。传进来就补上，没传就是词表原句。
    """
    from apps.regime import pool_rebuild

    return _labelled(value, pool_rebuild.outcome_display(value, generation=generation))


def stale_building_payload(
    generation: RegimePoolRebuild, *, now: Any = None
) -> dict[str, Any]:
    """「这一代卡在重算中了吗」：布尔 + 已经过去多少分钟。

    **布尔与分钟数成对**，与 `_labelled` 同一条理由：只给布尔，前端拿不到 N 就没法把
    「超过 N 分钟仍在重算中，检查 worker」这句话说出来；只给分钟数，前端就得自己跟阈值
    比一遍——那就是本仓反复在防的「同一件事的第二个说法」，阈值改一处、另一处不知道。

    三件事实都要能让调用方区分开：还在正常跑（`stale=False` + 分钟数）、卡住了
    （`stale=True`）、以及**根本不是 `building`**（`stale=False` 且 `minutes=None`）——
    最后一种不是「跑了 0 分钟」，它是「这一代已经收场了」。给 0 会把 `failed` 的行说成
    「刚跑」。
    """
    from apps.regime import pool_rebuild

    if generation.status != RebuildStatus.BUILDING.value:
        return {"stale": False, "minutes": None, "threshold_minutes": _stale_limit()}
    return {
        "stale": pool_rebuild.is_stale_building(generation, now=now),
        "minutes": pool_rebuild.building_minutes(generation, now=now),
        "threshold_minutes": _stale_limit(),
    }


def _stale_limit() -> int:
    """阈值本身也透出：页面要说的那句「超过 N 分钟」里的 N 不该在前端另写一份。"""
    from apps.regime import pool_rebuild

    return int(pool_rebuild.STALE_BUILDING_AFTER.total_seconds() // 60)


def kind_payload(kind: MechanismKind) -> dict[str, str]:
    """三个开关里的哪一个。表在 `MechanismKind`。"""
    return _labelled(kind.value, kind.display)


def event_impact_payload(event: Any) -> dict[str, str]:
    """一条重大事件的冲击档位。

    **收事件对象、不收 slug**：调用方（`briefing._event_summary`）拿到的是模型行，让它自己
    再查一张枚举表就是给「这条事件怎么说」造第二个说法。

    **但展示名不走模型的 `impact_display`**：那个 property 是
    `EventImpact(self.impact).display`，**没有兜底**——手工写脏的一行（迁移回填、补录）会
    让整张确认页 500，而本模块的既定口径是「认不出来的取值原样回显，不猜、不回落」
    （`_enum_display` 的 docstring，`regime_payload` / `state_payload` 都照此办）。所以这里
    走 `_enum_display`：认得出就用枚举的展示名（与 `impact_display` 逐字同一个字），认不出
    就原样回显那个脏值——**一条脏行不该让一整页炸掉，也不该被显示成某个真实档位**。
    """
    return _labelled(event.impact, _enum_display(EventImpact, event.impact))


def event_status_payload(event: Any) -> dict[str, str]:
    """一条重大事件的生命周期（排期 / 已取消）。理由同 `event_impact_payload`。"""
    return _labelled(event.status, _enum_display(EventStatus, event.status))


def event_scope_payload(event: Any) -> dict[str, str]:
    """一条重大事件的作用域（全市场 / 指定品种）。

    **这一对收模型的 property**：`scope_display` 不只是查枚举（品种列表那一支是「、」连起来
    的，只有模型知道怎么拼它），而 `scope_kind` 认不出来时它回的是空列表拼出来的空串——
    那不会抛异常，所以不需要兜底。两者的差别就在「有没有可能抛」这一件事上。
    """
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

    `note` 是**这一档的常驻说明**，只有机制整体那一档有（`mechanism_switch` 的
    `NOT_EXECUTION_SWITCH`）：那句话说的是「出 Shadow 只是留一条记录」，它必须与确认页
    正文、slash 回执同一处字面量，否则改词时会漏掉一份。前端原样显示，一个字不改。

    **它是卡片说明，不是这次动作的回执**：回执（翻没翻、从哪到哪）在 `switch/` 的响应里。
    把它放在这里而不是每次切换回一次，是因为「这一档不是执行开关」在切换**之前**就要被
    读到——人得先知道按下去会发生什么，再决定按不按。
    """
    # 延迟 import：`mechanism_switch` 会拖进 config / 事件日历 / 日报那一串模块，而本文件
    # 被几乎所有 regime 用例 import（包括只测池化表的那些）。与 `_stale_limit()` 里延迟
    # import `pool_rebuild` 同一条理由。
    from apps.regime import mechanism_switch

    mode = _mode_of(latest)
    return {
        "kind": kind_payload(kind),
        "mode": mode_payload(mode),
        "latest": None if latest is None else _switch_row(latest),
        "never_switched": latest is None,
        "note": (
            mechanism_switch.NOT_EXECUTION_SWITCH
            if kind is MechanismKind.MECHANISM
            else None
        ),
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
        # --- 这一代自己的收场（P5 子票 #47）--------------------------------- #
        # 四种收场里，`new_generation` / `duplicate` / `failed` 可以从一行记录判出来，
        # `reused` **判不出来**：复用那一轮连一条 `building` 行都没有。它只在任务状态读
        # 里出现（`rebuild_status_payload`），所以这两个键一个都不在这里撒谎。
        #
        # `outcome` 与 `status` 不是同一件事：`duplicate` 那一行在库里是 `failed`
        # （约束撞上时 `_mark_failed` 只坐这一个入口），把 `status=failed` 直接当收场显示，
        # 一次并发触发就会变成一条红色故障（ADR 0006 明令这是「约束在起作用」）。
        "outcome": outcome_payload(ser_outcome_of(generation)),
        "stale_building": stale_building_payload(generation),
    }


def ser_outcome_of(generation: RegimePoolRebuild) -> str:
    """一行记录 → 收场。**只是 `pool_rebuild.outcome_of` 的转发**，不在这里另判一次。

    存在的理由是可读性：`generation_summary` 里那行要短，而优先级（`reused` 先于
    `status`）必须留在判据那一个函数里。转发也不许加判断——一加就是第二处判据。

    `str(...)` 是「把模型列的运行时形状收在一处」的同一条手法（见本函数上面 `iso` 的
    docstring）：本仓没装 `django-stubs`，pyright 看到的是 `CharField` / `TextField`
    描述符而不是运行时值。
    """
    from apps.regime import pool_rebuild

    return pool_rebuild.outcome_of(
        status=str(generation.status), failure=str(generation.failure)
    )


def lifecycle_payload(is_active: Any) -> dict[str, str]:
    """策略的「机制在册」状态。**表在 `strategy_lifecycle`**（那是判据的家），这里只查表。

    与 `state_payload` 同一条纪律：前端不把 `False` 翻成「已退役」——那句话的家在后端，
    再写一份就是同一件事的第二个说法。
    """
    from apps.regime import strategy_lifecycle

    value = strategy_lifecycle.lifecycle_state(is_active)
    return _labelled(value, strategy_lifecycle.LIFECYCLE_DISPLAY[value])


def lifecycle_change_payload(row: Any) -> dict[str, Any] | None:
    """最近一次启停流水。**没有流水就是 `None`**（不是空对象、不是空串）。

    回显四件事：什么时候、谁、从哪档到哪档、为什么。与 `_switch_row` 同一形状，理由也
    同源——这四件就是 `StrategyLifecycleChange` 存在的理由，省掉任一件，页面上就答不出
    「这条策略为什么不见了、是谁弄的」。
    """
    if row is None:
        return None
    return {
        "at": iso(row.at),
        "actor_kind": actor_kind_payload(row.actor_kind),
        "actor_name": row.actor_name,
        "from_active": lifecycle_payload(row.from_active),
        "to_active": lifecycle_payload(row.to_active),
        "reason": row.reason,
    }


def strategy_payload(row: Mapping[str, Any], info: Mapping[str, Any]) -> dict[str, Any]:
    """格子里那个 `strategy` 对象：id + 名字 + 机制在册状态 + 最近一次启停流水。

    **状态与流水挂在格子上，而不是另开一个 `strategies` 数组**：池化表的行是前端按
    `strategy.id` 归并格子得来的，状态跟着格子走，行状态就不必再做一次 join。

    `latest_change` 在**没退役过**时是 `None`（不是编一条空流水）：页面据此只显示在册，
    而不是显示「上一次谁动过它」。历史上退役过、后来恢复了的策略，那一条流水仍然回显
    （它是事实），由它的 `to_active` 自己说明方向。
    """
    return {
        "id": str(row["strategy_id"]),
        "name": info["name"],
        "lifecycle": lifecycle_payload(info["is_active"]),
        "latest_change": lifecycle_change_payload(info.get("change")),
    }


def cell_payload(row: Mapping[str, Any], info: Mapping[str, Any]) -> dict[str, Any]:
    """一格：策略 + 阶段 + 来源 + 状态 + 原因 + 待复核 + 依据原文。

    `evidence` **原样透出，一个键都不改**：它是这张表的存在理由（哪几个回测、各多少笔、
    覆盖什么区间、合并成交数、品种、原型归一化留痕）。任何「顺手整理一下」的加工都会让
    「依据哪几个回测」在传输层变成一个派生值，而审计要的是原文。

    `info` 是那一行策略的三件事（名字 / 在册状态 / 最近一次启停流水），由调用方**一次
    查询**取回整批（`views._strategy_info`）——逐格再查一次的话，一张 40 行的表要发 40
    次查询，而本读面有查询数纪律。
    """
    return {
        "strategy": strategy_payload(row, info),
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
