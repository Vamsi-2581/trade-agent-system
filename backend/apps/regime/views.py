"""机制 Web 读面与写入口的端点（P5 子票 #41、#48、#42）。

| 路由 | 装什么 | 为什么是这个形状 |
| --- | --- | --- |
| `GET api/regime/overview/` | 三个开关的当前档 + 最近一次切换、当前代摘要、当前生效阶段 | 首屏要的**全部**廉价数，一次请求装完 |
| `GET api/regime/pool/` | 当前代（或指定代）的全部格子 | 池化表本体，键是 `(策略, 阶段)` |
| `GET api/regime/pool/generations/` | 代际历史分页 + 指定代与上一代的逐格差异 | 「这一代谁何时算的、变了什么」 |
| `GET api/regime/briefing/<kind>/` | 三张确认页之一（正文 + 审计串 + 排版用的数） | **重请求，按需拉**——不在轮询里 |
| `POST api/regime/switch/` | 翻一个开关的档位（body `{kind, mode}`） | **写入口**：`is_staff` 才放行，`reason` 与页面同源 |
| `GET api/regime/strategies/<id>/` | 一条策略的启停确认页（退役 / 恢复启用**两个方向一次都给**） | 读对所有登录用户开放；`available=false` 那侧就是「本来就是这一档」 |
| `POST api/regime/strategies/<id>/retire/` | **删除策略**：把 `Strategy.is_active` 翻成 `False`（退役） | **写入口**：`is_staff` 才放行；不删任何数据，落一条启停流水 |
| `POST api/regime/strategies/<id>/restore/` | 恢复启用：翻回 `True` | 同一条权限；它是「永不被**自动**恢复」那句里唯一的人工出口 |

## 「删除策略」为什么落在这一层

它改的是 `Strategy.is_active`——一个**交易域**的字段，而写入口在机制这一层。这不是放错
了地方：这个字段的读者是机制（`gate_run._warrant` 的归属判据、`replay_run` 的选取），
而 `/regime` 页面正是人看见「哪些策略在被管」的地方（池化表的行就是策略）。把按钮放在
人做判断的那一页、把叙述留给后端渲染，与三个开关的写入口同形。

`POST api/regime/strategies/<id>/retire/` **不是** `DELETE`：它一行都不删。用 POST 是因为
它是一次动作（翻一个开关 + 落一条流水），而不是对某个资源的删除——叫 DELETE 会让下一个
人以为库里那行没了。

## 为什么 `overview` 不装确认页的正文、而 `briefing` 单独一条

这不是「慢一点」而是「每 60 秒重导入一次全部策略模块」。两条调用链：

- `gate_switch.page()` → `gate_run.preview()` → `deactivation_run.managed_set()` →
  `pool_rebuild.ensure_strategies_discovered()` → `StrategyRegistry.discover()`：
  `os.listdir` + 逐个 `importlib` 重导入，**进程级副作用**；
- `mechanism_switch.page()` → `truth_run.agreement(tags=None)` → `slicing.load_tags()`：
  全量重算所有历史标签（`slicing.py` 自己写着「每次调用都重算，不缓存」）。

首屏要的是「现在是什么状态」，而三张确认页只在人**要做一个决定**时才需要。它们住在
`briefing/<kind>/`（#48），按需拉。本模块因此有一条**纪律性断言**：
`OVERVIEW_QUERY_BUDGET` 把 `overview` 的查询数钉成常数，退化成调 `page()` 会当场变红。

## 枚举的 display 一律由后端给

`MechanismMode.display` 已经是「Shadow（只记录，不执行）」，前端再写一遍就是同一件事的
第二个说法。所以每个枚举在响应里都是 `{"value", "display"}` 成对出现（见
`serializers._labelled` 的 docstring：成对是结构保证，不是纪律）。

## 冷启动不 500，而且要说出来

`pool_rebuild.current_cells()` 的 docstring：没有当前代是**正常状态**，调用方要能区分它
与故障。所以：

- `overview` 的 `pool.has_generation=false` + 一句后端渲染的 `pool.display`（不是
  `null`——`null` 只说了「没有」，没说是「从没重算过」还是「查询失败」）；
- `pool` 端点返回空 `cells` + 同一个 `has_generation=false` + 那句 `display`。

两个端点用**同一个** `EMPTY_POOL_DISPLAY` 常量，于是「还没有任何一代池化表」这句话在
页面上只有一种说法。

## pyright 与「`objects` 访问不到」

本模块剩下几条 pyright 报错全是同一句 `Cannot access attribute "objects"`——那是**本仓
没装 `django-stubs`** 的全仓现象（`pyright apps/` 今天 3546 条，`apps/logging_app/views.py`
这种既有模块同样有），不是本模块引入的新形状。CI 里那一步是 `continue-on-error: true`。
能消掉的那部分已经消掉了（`serializers.iso` 收模型列的运行时形状、`_mapping` 收 JSON
列），剩下的是「任何一次 `.objects` 都会红」，只能靠装 stubs 解决，不属于本票。

## 错误形状

`{"error": "..."}`，与 `apps/strategy_factory/views.py` 一致。参数错（`generation` 不是
整数）→ 400；资源不存在（那一代 id 查不到）→ 404。**两者不混**：前者是请求拼错，后者
是「你问的那一代不在了」，而它们的处置完全不同（改请求 vs 刷新页面）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping
from uuid import UUID

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response

from apps.authentication.permissions import IsStaffUser
from apps.regime import briefing as briefing_module
from apps.regime import breaker_switch, deactivation_run, gate_switch, mechanism_switch
from apps.regime import pool_rebuild
from apps.regime import serializers as ser
from apps.regime import strategy_lifecycle
from apps.regime.models import (
    ActorKind,
    MechanismKind,
    MechanismMode,
    RebuildStatus,
    RegimeMechanismSwitch,
    RegimePoolCell,
    RegimePoolRebuild,
)
from apps.trading.models import Strategy

logger = logging.getLogger(__name__)

#: `overview` 允许的查询数上界。**实测值就是这个数**，不是「大概」：
#: 三个开关各一次 `latest()`（3）+ 当前代一次（1）+ 生效判定一次（1）= 5。
#:
#: 钉住它的理由见模块 docstring：`mechanism_switch.page()` 会全量重算历史标签、
#: `gate_switch.page()` 会重导入全部策略模块。它们都是**同步**做的，所以查询数不是
#: 衡量它们的尺子——但一个退化成调 `page()` 的实现必然要多读 `RegimeJudgement` /
#: `ShadowDailyRecord` / `MajorEvent` 好几张表，5 这个数当场就守不住。上限在这里是
#: 「别把重请求塞进轮询」的一个可执行的写法，不是对 SQL 的洁癖。
OVERVIEW_QUERY_BUDGET = 5

#: 三个开关的固定顺序。与 `CONTEXT.md` 里日报第④段的列举顺序一致（机制整体 → 事件熔断
#: → 行情阶段 gate）：**顺序由后端给**，前端按数组排，免得两处各有一份顺序。
SWITCH_KINDS: tuple[MechanismKind, ...] = (
    MechanismKind.MECHANISM,
    MechanismKind.EVENT_BREAKER,
    MechanismKind.REGIME_GATE,
)

#: 冷启动那句话。**两个端点共用**：同一件事在页面上只有一种说法。
#: 措辞与 `report.py` 的「库中还没有任何生效过的判定（冷启动）」同一纪律——先说是
#: 什么状态，再给下一步。
EMPTY_POOL_DISPLAY = "还没有任何一代池化表：先跑一次池化重算（POST /api/regime/pool/rebuild/ 或 recompute_regime_slices）"

#: 冷启动时「当前生效阶段」那句话。`deactivation.BLOCKED_COLD_START` 的展示名讲的是
#: 「本轮不产出停用建议」，而这里问的是「此刻哪一档在咬人」，所以另给一句。
COLD_STATE_DISPLAY = "库中还没有任何生效过的判定（冷启动）"

#: 代际历史分页的默认与上限。
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100

#: 投递之后**建议**多久来问一次（秒）。不是契约：真正的判据是状态读返回的 `done`。
#: 60 与页面自己那条 60 秒轮询同源——不为这一页另造一个节奏。
POLL_AFTER_SECONDS = 60

#: Celery 的终态。三种：成功、失败、被撤销（重投前显式 revoke 过）。
#: **`REVOKED` 算终态**：它不会自己再往下走了，等下去只等出一个永远不动的转圈。
TERMINAL_STATES = frozenset({"SUCCESS", "FAILURE", "REVOKED"})

#: 「投递成功但 worker 还没取走」那句话。**与「重算中」必须分开**：`
#: `beat_health.py` 模块 docstring 记的缺口正是「这个任务不进任务健康检查的扫面」，所以
#: 一个没人消费的任务看起来与一条在跑的任务一样——除非把这句话说出来。
PENDING_DISPLAY = (
    "任务已投递但还没有 worker 取走，或这次的结果已经过期（不是「重算中」）："
    "检查 celery worker 是否在运行；若确实跑过，去看代际历史里那一代有没有落成"
)


def _no_store(response: Response) -> Response:
    """状态读也 `no-store`：`building` 行每一秒都在变，缓存一次就是缓存一个错误的「现在」。

    与 `briefing` / `switch` 同一条纪律的延伸——三者都在回答「此刻是什么状态」。
    """
    response["Cache-Control"] = "no-store"
    return response


def _no_building() -> dict[str, Any]:
    """「没有一代正在建」的形状。**`minutes` 是 `None`，不是 0**——见 `stale_building_payload`。"""
    from apps.regime import pool_rebuild

    return {
        "stale": False,
        "minutes": None,
        "threshold_minutes": int(
            pool_rebuild.STALE_BUILDING_AFTER.total_seconds() // 60
        ),
    }


def _generation_from_summary(
    summary: Mapping[str, Any],
) -> RegimePoolRebuild | None:
    """摘要里的 `rebuild_id` → 那一行。查不到就是 `None`（不猜、不造）。

    查不到是真实情形：`reused` 那一轮的 id 是**旧代**的 id（它自己有），而它本来就在库里。
    真查不到只可能是两次请求之间有人删了行——那时返回 `None` 比返回一个拼接出来的对象好。
    """
    rebuild_id = summary.get("rebuild_id")
    if rebuild_id is None:
        return None
    return RegimePoolRebuild.objects.filter(pk=rebuild_id).first()


def _current_building() -> RegimePoolRebuild | None:
    """此刻正在建的那一代（`building` 且**还没被标成失败**）。

    按 `-started_at` 取最近一条：并发触发时可能不止一条在 `building`，而页面要显示的是
    「我按的那一次大概对应哪一代」。取最近一条而不是最早——最早那条更可能是已经死掉的。
    """
    return (
        RegimePoolRebuild.objects.filter(status=RebuildStatus.BUILDING.value)
        .order_by("-started_at", "-id")
        .first()
    )


def _most_recent_generation(task_id: str) -> RegimePoolRebuild | None:
    """任务失败时，库里最近那一代。

    **只用它来取 `failure` 原文与状态，不用它断言「这次失败的就是它」**：任务与代之间没
    有外键（`reused` 那条路径也没有行可挂），强认会在并发时张冠李戴。取不到就是 `None`，
    那时失败原因退回 Celery 那条串。
    """
    return RegimePoolRebuild.objects.order_by("-started_at", "-id").first()


def _failure_text(result) -> str:
    """Celery 的结果里那条异常串。**没有就空串**，不强拼一个「未知错误」。

    `result.result` 在 FAILURE 时是一个异常对象（或它的 `repr`），在其它状态下是别的
    东西——所以这里只做一件事：能转成字符串就转，转不出来给空串。
    """
    try:
        value = result.result
    except Exception:  # noqa: BLE001 —— 结果后端挂了不该让状态读 500
        return ""
    if value is None:
        return ""
    return str(value)


# --------------------------------------------------------------------------- #
# 写入口：kind → 既有写方
# --------------------------------------------------------------------------- #

#: kind → 三个既有写方之一（P5 子票 #42）。**这里没有任何写逻辑**：三个 `flip_*` 各自
#: 早于本票存在，CLI（`manage.py event_breaker` / `regime_gate`）与 slash（`/regime on` /
#: `/regime gate on` / `/regime mech exit`）走的就是它们。本表只回答「网页上点这一下」
#: 对应哪一个，于是三条通路写下去的行**在库里长得一样**。
#:
#: 写成一张表而不是 `if/elif`：与 `briefing.BRIEFING_SOURCES` 同一取向——加第四个开关时
#: 要改的是两张表，两张表的键集都被 `test_switch_api` 对 `MechanismKind` 钉住，忘了任一张
#: 会红在测试里，而不是在页面上变成一个 400。
#:
#: **表里登记的是模块与函数名，不是函数对象**（与 `briefing.Source.module` 同一取法）。
#: 在导入那一刻把 `flip_*` 抓进字典的话，`patch.object(gate_switch, "flip_regime_gate")`
#: 这类替身就失效了——属性在**调用那一刻**才取，替身才拦得住。这不只是测试便利：它同时
#: 让「这个端点调的到底是不是那三个既有写方」这件事可以在运行期被验证，而不是在导入期被
#: 冻死。
#:
#: 三个写方的返回形状**不同**：`gate_switch.flip_regime_gate` 返回 `Flip`（带对账摘要），
#: 另外两个返回 `RegimeMechanismSwitch | None`。本表因此只收写方本身、不收「统一返回值」
#: ——抹平它们要么丢掉对账摘要（gate 方向的全部排障信息），要么给另外两个编一个空的假摘要。
#: 那处差异在下面 `_apply_flip` 里显式处理，它是这三个写方唯一的形状差异。
FLIP_SOURCES: Mapping[MechanismKind, tuple[Any, str]] = {
    MechanismKind.MECHANISM: (mechanism_switch, "flip_mechanism"),
    MechanismKind.EVENT_BREAKER: (breaker_switch, "flip_event_breaker"),
    MechanismKind.REGIME_GATE: (gate_switch, "flip_regime_gate"),
}

#: 开关的**短名**，用在「本来就是这一档」那句回执里。
#:
#: 为什么不直接用 `MechanismKind.display`：那个说的是「这个开关管什么」，长到
#: 「出 Shadow（判定 + 切片 + 自动停用）」；而人看的是「机制整体本来就是 Shadow」。
#: 这批短名不是本票新造的——三个写方的 `logger.info`（「机制整体已经是 %s」）与 slash 的
#: 回显（「事件熔断**本来就是执行态**」）用的就是这一批，这里只是把它们收成一张表，
#: 好让网页那条通路的说法与另外两条逐字一致。加第四个开关时要跟着加一行。
KIND_SHORT_NAME: Mapping[MechanismKind, str] = {
    MechanismKind.MECHANISM: "机制整体",
    MechanismKind.EVENT_BREAKER: "事件熔断",
    MechanismKind.REGIME_GATE: "行情阶段 gate",
}


# --------------------------------------------------------------------------- #
# 小件
# --------------------------------------------------------------------------- #


def _bad_request(message: str) -> Response:
    return Response({"error": message}, status=400)


def _not_found(message: str) -> Response:
    return Response({"error": message}, status=404)


def _server_error(message: str) -> Response:
    """500 也要带 `{"error"}`，不能是 Django 的白页。

    T6 让前端把非 2xx 的响应体解析成 `ApiError.message`；一个 HTML 错误页在那边只能退回
    「API error: 500」，而下面这句话说的是**库里此刻是什么状态**——它决定了人接下来该做
    什么，退回成一个状态码就把那件事丢了。
    """
    return Response({"error": message}, status=500)


def _not_dispatched(message: str) -> Response:
    """503：**投递本身失败**（broker 不可用、序列化不了参数）。

    它既不是 400（请求拼错了）也不是 500（我们自己的 bug）：请求是合法的，服务端逻辑也
    跑通了，出问题的是那条到下层的通路。这个区分是给页面看的——503 该显示的文案是「没
    投出去，稍后重试」，而 500 该显示「投出去了但结果不确定，先刷新看看」。混成 500 会让
    人对着一个可能已经跑起来的重算再点一次。
    """
    return Response({"error": message}, status=503)


def _int_param(request: Request, name: str) -> int | None:
    """读一个整数查询参数；缺省（或空值）返回 `None`。**拼错就是 `ValueError`**，由调用方
    转 400。

    `?a=1&a=2` 时 `query_params.get()` 给的是 list。取最后一个：多值参数在 HTTP 里没有
    「正确」的那个，而 `int(list)` 会抛 `TypeError`（不是 `ValueError`），于是调用方的
    `except ValueError` 接不住它、直接 500——一个用户可以随手触发的白页。
    """
    raw: Any = request.query_params.get(name)
    if isinstance(raw, list):
        raw = raw[-1] if raw else None
    if raw is None or raw == "":
        return None
    return int(raw)


def _pool_state(generation: RegimePoolRebuild | None) -> dict[str, Any]:
    """「当前代有没有」这件事的统一说法。**没有代时 `generation` 是 `null`，但同时带
    `has_generation=false` 与一句 `display`**——只给 `null` 的话，前端分不清「从没重算过」
    与「后端没答上来」，而这两件事要求人做的动作完全不同。"""
    if generation is None:
        return {
            "has_generation": False,
            "display": EMPTY_POOL_DISPLAY,
            "generation": None,
        }
    return {
        "has_generation": True,
        "display": "",
        "generation": ser.generation_summary(generation),
    }


def _regime_state() -> dict[str, Any]:
    """当前**生效**的那条阶段。冷启动时 `regime` 为 `null` 并带 `cold=true`。

    `effective_at` 原样带出来（不是「算出来的时刻」）：过期判据问的是「这个结论从哪天
    开始在生效」，两者不是一回事（`deactivation.RegimeState` 的 docstring）。
    """
    state = deactivation_run.current_regime_state()
    if state.regime is None:
        return {
            "regime": None,
            "effective_at": ser.iso(state.effective_at),
            "cold": True,
            "display": COLD_STATE_DISPLAY,
        }
    return {
        "regime": ser.regime_payload(state.regime),
        "effective_at": ser.iso(state.effective_at),
        "cold": False,
        "display": "",
    }


def _strategy_names(ids: list[Any]) -> dict[Any, str]:
    """策略 id → 名字。**一次查询**，不 `select_related` 到格子上：格子那张表只有 id，
    名字在 `strategies` 表里，而这两张表在池化读面上没有别的耦合。"""
    if not ids:
        return {}
    return dict(Strategy.objects.filter(id__in=ids).values_list("id", "name"))


def _strategy_info(ids: list[Any]) -> dict[Any, dict[str, Any]]:
    """策略 id → `{name, is_active, change}`。**两次查询**（策略表一次 + 启停流水一次），
    不按格子重复查：池化表一行有四格，逐格查一次就是四倍。

    `change` 是最近一次启停流水（没有就是 `None`）。它与 `is_active` 是两件事，一起回显
    是因为页面既要显示「它此刻在不在册」，也要显示「上一次是谁、为什么动的」——而后者
    只有流水表知道。

    查不到的策略（格子指着一条已经不存在的策略行）回一个**名字为空、状态为「已退役」**的
    占位：那是脏库的形状，而脏值的方向应当是「不被机制管」，不是「在册」。
    """
    if not ids:
        return {}
    info = {
        strategy_id: {"name": name, "is_active": bool(is_active), "change": None}
        for strategy_id, name, is_active in Strategy.objects.filter(
            id__in=ids
        ).values_list("id", "name", "is_active")
    }
    for strategy_id, change in strategy_lifecycle.changes_for(ids).items():
        row = info.get(strategy_id)
        if row is not None:
            row["change"] = change
    return info


def _info_for(
    info: Mapping[Any, Mapping[str, Any]], strategy_id: Any
) -> dict[str, Any]:
    """取一条策略的读面三件事；查不到时用那个 fail-closed 占位（见 `_strategy_info`）。"""
    row = info.get(strategy_id)
    if row is None:
        return {"name": "", "is_active": None, "change": None}
    return dict(row)


# --------------------------------------------------------------------------- #
# 一代与它的上一代
# --------------------------------------------------------------------------- #


def _previous_generation(generation: RegimePoolRebuild) -> RegimePoolRebuild | None:
    """这一代**落成时**的「上一代」：在它之前翻成 `ready` 的最近那一代。

    判据照抄 `pool_rebuild._previous_triples()` 的处境，而不是另立一套：重算那一刻
    「上一代」是 `current_generation()`（最近一次翻成 `ready` 的那一代，按 `-finished_at`
    排）。所以事后回看某一代 G 的上一代，就是「`finished_at` 早于 G 的锚点、且状态是
    `ready` 的最近那一代」。

    锚点取 `finished_at or started_at`：`building` / `failed` 的一代没有 `finished_at`，
    而它同样有格子可看（残骸是记录不是垃圾，见 `RebuildStatus` 的 docstring）。
    """
    anchor = generation.finished_at or generation.started_at
    return (
        RegimePoolRebuild.objects.filter(
            status=RebuildStatus.READY.value, finished_at__lt=anchor
        )
        .exclude(pk=generation.pk)
        .order_by("-finished_at", "-id")
        .first()
    )


def _triples(generation: RegimePoolRebuild | None) -> dict[tuple[Any, str], tuple]:
    """一代的 `(状态, 原因, 来源)` 三元组。**只比这三样**，与 `_cell_changes` 同源。"""
    if generation is None:
        return {}
    rows = RegimePoolCell.objects.filter(rebuild=generation).values_list(
        "strategy_id", "regime", "state", "reason", "source"
    )
    return {
        (strategy_id, regime): (state, reason, source)
        for strategy_id, regime, state, reason, source in rows
    }


def _diff(generation: RegimePoolRebuild) -> dict[str, Any]:
    """一代相对上一代的逐格差异。

    `changed` 是**两侧都有、三元组不同**的格子；`added` / `removed` 分开列，因为它们要求
    人做的事不同（新增一格是「这条策略这个阶段第一次有结论」，消失一格是「样本没了或
    策略没了」）。`pool_rebuild._cell_changes` 把三者合起来数，这里拆开——**但
    `changed + added + removed` 必须等于记录里的 `cells_changed`**，那条等式由
    `test_views` 钉住：两处对不上时，说明差异口径分叉了，而分叉之后页面上说「变了 3 格」
    而列表里有 5 条，谁都不会报错。
    """
    previous = _previous_generation(generation)
    before = _triples(previous)
    after = _triples(generation)
    names = _strategy_names(ser.strategy_ids(list(after) + list(before)))

    rows: list[dict[str, Any]] = []
    for key in ser.ordered_cell_keys({**before, **after}, names):
        was = before.get(key)
        now = after.get(key)
        if was == now:
            continue
        kind = (
            "changed"
            if was is not None and now is not None
            else ("added" if was is None else "removed")
        )
        rows.append(
            {
                "key": ser.cell_key(*key),
                "strategy": {"id": str(key[0]), "name": names.get(key[0], "")},
                "regime": ser.regime_payload(key[1]),
                "kind": kind,
                "before": ser.triple_payload(was),
                "after": ser.triple_payload(now),
            }
        )

    counts = {"changed": 0, "added": 0, "removed": 0}
    for row in rows:
        counts[row["kind"]] += 1
    return {
        "generation": generation.pk,
        "previous_generation": previous.pk if previous is not None else None,
        "previous_finished_at": ser.iso(
            previous.finished_at if previous is not None else None
        ),
        # 记录里那一刻算出来的数。与 `counts` 合计对不上时，是口径分叉而不是「数据脏了」。
        "recorded_cells_changed": generation.cells_changed,
        "counts": counts,
        "cells": rows,
    }


# --------------------------------------------------------------------------- #
# 1. overview
# --------------------------------------------------------------------------- #


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def overview(request: Request) -> Response:
    """首屏：三个开关的档、当前代摘要、当前生效阶段。**只装廉价只读**（见模块 docstring）。

    `switches` 是**数组**而不是三个具名键：三个开关在结构上完全同形（谁、何时、为什么、
    从哪档到哪档），具名键会让前端为每一个写一遍同样的渲染，而「加第四个开关」那天还得
    改前端。数组 + `kind.value` 做 key，顺序由 `SWITCH_KINDS` 给。
    """
    switches = []
    for kind in SWITCH_KINDS:
        # 一次 `latest()`，**不调 `current()`**：两者是同一条查询，`current()` 内部就是
        # `latest()` 加一步取值。见 `serializers.switch_payload` 的 docstring。
        latest = RegimeMechanismSwitch.latest(kind)
        switches.append(ser.switch_payload(kind, latest))

    return Response(
        {
            "switches": switches,
            "pool": _pool_state(pool_rebuild.current_generation()),
            "regime_state": _regime_state(),
            # 「数据截止」时刻由前端从上面两个时刻自己取（当前代的 `finished_at`、
            # 判定的 `effective_at`），后端不另造一个「现在」——后端说「现在」的那一刻
            # 它就已经是过去的了，而页面上那个常驻时刻要回答的是「这张表是什么时候的」。
        }
    )


# --------------------------------------------------------------------------- #
# 2. pool
# --------------------------------------------------------------------------- #


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def pool(request: Request) -> Response:
    """当前代（或 `?generation=<id>` 指定的一代）的全部格子，键是 `(策略, 阶段)`。

    **`generation` 显式传进 `current_cells()`**：不传的话它会自己再问一次当前代，而两次
    问之间可能正好翻了一代，于是「这一代的 id」与「拿到的格子」来自不同两代
    （`current_cells` 的 docstring 说的就是这件事）。本端点先取代、再用同一个对象取格子，
    配对在一次请求里钉住。

    `?generation=<id>` 指定一代时**状态不限**（`building` / `failed` 的残骸也有格子可看
    ——那是「为什么会这样」的一半答案）。当前代那条路径只认 `ready`，与
    `current_generation()` 一致。

    ## `?evidence=0`：只发矩阵，不发依据（2026-10-23）

    默认 `evidence=1`（带上两张去重表：`evidence` + `results`）。传 `0` 时**两张表都不发**，
    格子上只留 `evidence_key` / `results_key`，依据由 `pool/cell/` 按格拉。

    **为什么需要它**：两级去重后整份响应仍有 1.7 MB（gzip 后 278 KB），因为依据里那个
    `results` 数组本身就大。而在一条实测只有 ~2 KB/s、20% 丢包的链路上（从开发机到 txy），
    278 KB 要 50 秒——页面上就是「一直转圈」。矩阵本身（248 格的元数据）只有几十 KB，
    而人一次只会展开一两格：**把依据从「一次性全给」改成「展开那一格才给」**，首屏才真的
    到得了。这与 `briefing/<kind>/`「按需拉」是同一条纪律。

    默认仍是 `1`：别的调用方（脚本、将来的导出）要的是完整一份，不该被这条界面优化改掉。
    """
    try:
        generation_id = _int_param(request, "generation")
        with_evidence = _int_param(request, "evidence")
    except ValueError:
        return _bad_request("generation / evidence 必须是整数")
    # `?evidence=0` 才关；`None`（没传）与任何非 0 都算开——默认必须是「发」，
    # 否则今天已有的调用方会在一次无关的部署后突然拿不到依据。
    with_evidence = with_evidence != 0

    if generation_id is None:
        generation = pool_rebuild.current_generation()
    else:
        generation = RegimePoolRebuild.objects.filter(pk=generation_id).first()
        if generation is None:
            return _not_found(f"没有 id={generation_id} 的这一代池化表")

    # 配对读取：同一个 `generation` 对象既给 id 又给格子（见 docstring）。
    cells = pool_rebuild.current_cells(generation)
    info = _strategy_info(ser.strategy_ids(cells))
    # 行序仍按名字（`ordered_cell_keys` 的判据没变）；`info` 多带的是每一行的在册状态与
    # 最近一次启停流水，那一份由 `cell_payload` 挂到格子的 `strategy` 上。
    names = {strategy_id: row["name"] for strategy_id, row in info.items()}
    ordered = ser.ordered_cell_keys(cells, names)

    # **两级去重，两张表**（2026-10-23）。理由见 `ser.evidence_dedupe` 与
    # `TestPoolDoesNotRepeatEvidence`：重复的**不是**整份 evidence（`metrics` /
    # `own_sample` 按阶段算，248 格去出 243 种），而是它内部那个 `results` 大数组
    # （占 98.7% 体积，248 格只有 19 种）。所以 `results` 单独抽出来去重。
    #
    # 实测（txy 生产库）：整份去重 59.0MB → 等于没省；两级去重 1.6MB，省 35 倍。
    #
    # 指纹与表**一次算完**：分开算就是 248 次多余的 sha256（实测约 2 秒），而两处各算
    # 一遍还会造出「表里的键」与「格子上的键」两套算法。
    # `?evidence=0` 时**连指纹都不算**（两张表直接给空，键位留空串）：算指纹要对每份
    # evidence 做 `json.dumps` + sha256，248 格约 2 秒，而那正是这条路径要省的钱。
    if with_evidence:
        evidence, results, evidence_keys = ser.evidence_dedupe(cells.values())
    else:
        evidence, results = [], []
        evidence_keys = {
            (row["strategy_id"], row["regime"]): ("", "") for row in cells.values()
        }

    return Response(
        {
            **_pool_state(generation),
            # 矩阵的列序由后端给（`serializers.ORDERED_REGIMES`），前端按数组排。
            "regimes": [ser.regime_payload(regime) for regime in ser.ORDERED_REGIMES],
            "evidence": evidence,
            "results": results,
            "cells": [
                {
                    "key": ser.cell_key(*key),
                    **ser.cell_payload(
                        cells[key],
                        _info_for(info, key[0]),
                        key=evidence_keys[key],
                    ),
                }
                for key in ordered
            ],
        }
    )


# --------------------------------------------------------------------------- #
# 2b. pool/cell —— 一格完整的依据（按需拉）
# --------------------------------------------------------------------------- #


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def pool_cell(request: Request) -> Response:
    """**一格**的完整依据。`GET api/regime/pool/?evidence=0` 的配套读面。

    为什么要它：`pool/` 带上依据时整份响应是 1.7 MB（gzip 后 278 KB），因为依据里那个
    `results` 数组本身就大。而矩阵（248 格的元数据）只有几十 KB，人一次也只会展开一两格。
    实测从开发机到 txy 的链路约 2 KB/s、20% 丢包——278 KB 要 50 秒，页面上就是「一直
    转圈」。所以列表只发矩阵，依据按格拉（与 `briefing/<kind>/`「按需拉」同一条纪律）。

    ## 参数

    - `strategy`（必填）：策略 id（与池化表格子 `strategy.id` 同一个值）；
    - `regime`（必填）：阶段 slug；
    - `generation`（选填）：哪一代，缺省是当前代——与 `pool/` 的判据一致。

    阶段不合法（不在 `ORDERED_REGIMES` 里）→ **400**：那是请求拼错，不是「这一格不在」。
    格子不存在 → **404**：策略没错、阶段没错，但这一代里没有这一格（样本不足、或策略
    不在这一代里）。两者不混。

    ## 返回

    `{"key", "evidence"}`。`evidence` 就是 `pool._evidence` 的原文**逐字节**那一份
    （`results` 已经拼回它原来的位置），所以审计要的「依据哪几个回测、多少笔、什么区间」
    与列表里带的完全一致——两条路径给的是同一份东西，只是发的时机不同。
    """
    strategy_id = request.query_params.get("strategy")
    regime = request.query_params.get("regime")
    if not strategy_id or not regime:
        return _bad_request("strategy 与 regime 都必填")
    if regime not in ser.ORDERED_REGIMES:
        allowed = " / ".join(ser.ORDERED_REGIMES)
        return _bad_request(f"未知的 regime：{regime!r}（可选：{allowed}）")

    try:
        generation_id = _int_param(request, "generation")
    except ValueError:
        return _bad_request("generation 必须是整数（一代重算记录的 id）")

    if generation_id is None:
        generation = pool_rebuild.current_generation()
    else:
        generation = RegimePoolRebuild.objects.filter(pk=generation_id).first()
        if generation is None:
            return _not_found(f"没有 id={generation_id} 的这一代池化表")

    # 只取这一行：`.filter(...).values(...).first()` 而不是 `current_cells()` 再挑——
    # 后者会把这一代**全部**格子的 evidence 读进内存（正是这次要避免的开销）。
    row = (
        RegimePoolCell.objects.filter(
            rebuild=generation, strategy_id=strategy_id, regime=regime
        )
        .values("strategy_id", "regime", "evidence")
        .first()
    )
    if row is None:
        return _not_found(f"这一代里没有「策略 {strategy_id} × {regime}」这一格")

    evidence = row["evidence"] if row["evidence"] is not None else {}
    return Response(
        {
            "key": ser.cell_key(row["strategy_id"], row["regime"]),
            "evidence": evidence,
        }
    )


# --------------------------------------------------------------------------- #
# 3. pool/generations
# --------------------------------------------------------------------------- #


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def pool_generations(request: Request) -> Response:
    """代际历史（`-started_at` 分页）；带 `?generation=<id>` 时附上那一代与上一代的差异。

    分页形状照 `apps/logging_app/views.py` 的 `{count, page, page_size, has_next, results}`
    ——仓里既有的唯一分页惯例。**不用 DRF 的 `PageNumberPagination`**：那要配全局
    `PAGE_SIZE` 或一个一次性类，而这里只有一张表要分页。

    `-started_at` 是模型 `Meta` 的排序（管理界面列记录用的那个），这里照用：「这一代谁
    何时算的」按开始时刻排最自然，而「当前是哪一代」那个问题由 `finished_at` 回答
    （`current_generation()` 的 docstring 特意把两者分开了，本端点不混用）。

    **这里不标「哪一条是当前代」**：那由 `overview` 的 `pool.generation.id` 回答，而它是
    同一条规则的另一个出口。本端点再算一遍就有了第二处「当前是哪一代」，两处各查一次
    `current_generation()` 之间翻了一代，页面就会出现两条都自称当前的行。前端拿
    `overview` 那个 id 去高亮即可（它本来就在 60 秒轮询里）。
    """
    try:
        # 不写 `or DEFAULT`：`?page=0` 会被 `or` 读成「没传」而静默变成第 1 页，于是
        # 「页码从 1 开始」这条边界永远撞不上，而用户看到的是「第 0 页返回了第 1 页」。
        page = _int_param(request, "page")
        page_size = _int_param(request, "page_size")
        generation_id = _int_param(request, "generation")
    except ValueError:
        return _bad_request("page / page_size / generation 都必须是整数")
    page = 1 if page is None else page
    page_size = DEFAULT_PAGE_SIZE if page_size is None else page_size
    if page < 1:
        return _bad_request("page 从 1 开始")
    if page_size < 1 or page_size > MAX_PAGE_SIZE:
        return _bad_request(f"page_size 必须在 1..{MAX_PAGE_SIZE} 之间")

    queryset = RegimePoolRebuild.objects.order_by("-started_at", "-id")
    total = queryset.count()
    offset = (page - 1) * page_size
    page_rows = list(queryset[offset : offset + page_size])

    payload: dict[str, Any] = {
        "count": total,
        "page": page,
        "page_size": page_size,
        "has_next": offset + page_size < total,
        "results": [ser.generation_summary(row) for row in page_rows],
    }

    if generation_id is not None:
        generation = RegimePoolRebuild.objects.filter(pk=generation_id).first()
        if generation is None:
            return _not_found(f"没有 id={generation_id} 的这一代池化表")
        payload["diff"] = _diff(generation)

    return Response(payload)


# --------------------------------------------------------------------------- #
# 4. briefing/<kind>
# --------------------------------------------------------------------------- #


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def briefing(request: Request, kind: str) -> Response:
    """三张确认页之一，**按需拉**（P5 子票 #48）。

    与上面三个端点的关系是明确的：`overview` 装廉价只读、每 60 秒轮询；本端点每一次调用
    都会重导入全部策略模块（gate）或全量重算历史标签（mechanism），所以它**不在轮询里**，
    只在人要做一个决定时拉一次。响应带 `heavy: true`，HTTP 层再发 `Cache-Control: no-store`
    ——两层都说同一件事：这一次的求值不许被任何中间层留下来，因为它的副作用（策略模块
    重导入）与结果绑定在同一个时刻上。

    **`body` 与 `summary` 同源**：都由 `briefing.evaluate()` 里那**一次** `page()` 调用渲染。
    不在本视图里分别调两次取它们的便捷函数（那正是这条不变量唯一会破的地方）。

    错误：未知 `kind` → **400**（枚举取值错），不是 404。`MechanismKind(slug)` 自己就是
    校验器，这里不另写一份 slug 清单——多一份清单就多一处「加了第四个开关而这一处没加」。

    **校验的范围只到 `kind` 那一行**，不包住 `payload()`。两件事必须分开：`kind` 认不出来
    是**请求拼错**（400，让客户端改 URL）；`page()` 内部抛出来的 `ValueError` 是**服务端
    数据坏了**（比如流水表里有一条手工写脏的 `to_mode`），把它也说成「未知的 kind」既对
    操作者撒谎（路径段明明是合法的），又违反本模块自己的错误约定（参数错与内部故障不混）。
    所以这里先解析、再求值，内部故障照常冒成 500——那才是它该有的样子。
    """
    try:
        switch = MechanismKind(kind)
    except ValueError:
        allowed = " / ".join(m.value for m in MechanismKind)
        return _bad_request(f"未知的 kind：{kind!r}（可选：{allowed}）")

    payload = briefing_module.payload(switch)

    response = Response(payload)
    # 重请求 + 进程级副作用：不许缓存。`no-store` 而不是 `no-cache`——后者还允许存下来
    # （只是用之前要回源），而这里连「存下来」都不该发生。
    response["Cache-Control"] = "no-store"
    return response


# --------------------------------------------------------------------------- #
# 5. switch/ —— 写入口
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Outcome:
    """一次 `flip_*` 调用的收场，**三个写方的返回值差异就地抹平在这里**。

    `row is None` = 本来就是这一档（gate 方向此时仍对过一次账，摘要留在 `sync` 里）。
    """

    row: RegimeMechanismSwitch | None
    sync: Mapping[str, Any] | None


def _apply_flip(
    kind: MechanismKind,
    to_mode: MechanismMode,
    *,
    actor_name: str,
    reason: str,
) -> _Outcome:
    """调 `FLIP_SOURCES` 里那一个写方。**本函数不写任何东西**——写的是三个 `flip_*`。

    `actor_kind` 一律 `ActorKind.WEB`（T1 加的第四种），`actor_name` 取认证过的
    `User.username`：网页上的人与命令行上的 `getpass.getuser()` 是**同一个取值口径、不同
    来源**，这一列的意义正是事后能分清「命令行敲的还是网页点的」（`ActorKind.WEB` 的
    docstring）。

    `reason` 由调用方传入，且必须来自**本次请求里那一次 `briefing.evaluate()`**——本函数
    不校验它、也不补它：它拿到什么就写什么，同源由调用方保证。
    """
    module, func_name = FLIP_SOURCES[kind]
    flip = getattr(module, func_name)(
        to_mode,
        actor_kind=ActorKind.WEB,
        actor_name=actor_name,
        reason=reason,
    )
    # 两个写方返回行本身，gate 返回 `Flip`。判据用 `isinstance(..., Flip)` 而不是
    # `hasattr(flip, "row")`：后者会把这个形状差异变成一次鸭子类型猜测，而它是**已知的**
    # 三个模块之间唯一的一处差异（`briefing.Source.page_accepts_direction` 是同一取向）。
    if isinstance(flip, gate_switch.Flip):
        return _Outcome(row=flip.row, sync=flip.sync)
    return _Outcome(row=flip, sync=None)


def _already_in_mode_display(kind: MechanismKind, to_mode: MechanismMode) -> str:
    """「本来就是这一档」那句话。**与 CLI / slash 逐字一致**。

    三个入口说的是同一件事，用同一批词（`KIND_SHORT_NAME` + `MechanismMode.display`），
    所以网页上的回执与聊天里的回执读起来是同一句。gate 那一档多一句「仍然对了一次账」
    ——那是它与其他两个开关唯一的语义差异（`flip_regime_gate` 无条件跑 `gate_run.sync`），
    不说出来，人就不会知道「再点一次」在 gate 上真的有用。
    """
    tail = "（仍然对了一次账）" if kind is MechanismKind.REGIME_GATE else ""
    return f"{KIND_SHORT_NAME[kind]}本来就是{to_mode.display}，没有写第二条流水{tail}。"


def _switch_payload(
    kind: MechanismKind,
    from_mode: MechanismMode,
    to_mode: MechanismMode,
    outcome: _Outcome,
    *,
    reason: str,
) -> dict[str, Any]:
    """写入口的回执。形状在改档 / 本来就是这两条路径上**键集恒定**。

    前端要回答的三个问题各有一个键：翻没翻（`changed`）、从哪到哪（`from_mode` /
    `to_mode`）、系统怎么称呼这件事（`display`）。`reason` 回显的是**真正落库的那一串**
    （与页面同源），不是请求体里的：请求体里那个若有，这里回显它会让客户端以为自己说了算。
    """
    changed = outcome.row is not None
    return {
        "kind": ser.kind_payload(kind),
        "changed": changed,
        "from_mode": ser.mode_payload(from_mode),
        "to_mode": ser.mode_payload(to_mode),
        # 操作者看到的那句话：与 CLI / slash 的回执同一批词，前端直接显示，不重拼。
        "display": (
            f"{KIND_SHORT_NAME[kind]}已切换：{from_mode.display} → {to_mode.display}"
            if changed
            else _already_in_mode_display(kind, to_mode)
        ),
        # 落库的那一串（切到执行态取 `briefing.summary`、退回 Shadow 取
        # `closing_summary(data)`）。**恒回显**，因为它是人核对「网页上看到的那页」与
        # 「流水里记下的那句」是否一致的唯一凭据。
        "reason": reason,
        "at": ser.iso(outcome.row.at) if outcome.row is not None else None,
    }


@api_view(["POST"])
@permission_classes([IsStaffUser])
def switch(request: Request) -> Response:
    """翻一个开关的档位（P5 子票 #42）。**本 map 唯一的写入口。**

    请求体 `{"kind": ..., "mode": ...}`。**只有这两个字段有含义**：客户端提交的任何
    `reason` 一律忽略（`test_switch_api` 有一条用例传一个假 reason 并断言库里不出现它）。

    ## 为什么 `reason` 不让人写

    它由服务端在**同一个请求内**重新求值该 kind 的 `page()`，按方向取
    `briefing.summary`（切到执行态）或 `closing_summary(briefing.data)`（切到 shadow）。
    这与 CLI（`event_breaker` / `regime_gate` 的 `handle`）和 slash
    （`regime_commands._on` / `_mech_exit` 等）**逐字一致**——三处都是「拿这一次求值的
    摘要去写」，于是流水里那句话与人在页面上看到的数是同一次快照。

    让网页手写理由会立刻造出两种流水：聊天里写着「窗口相交 3 条」、网页上写着「不想拦了」。
    两者对事后排查的价值差一个量级，而后者根本没法核对。所以这一页**不提供输入框**，
    理由永远由系统渲染。

    ## 三个写方，一行写逻辑都不新写

    `FLIP_SOURCES` 分派到 `mechanism_switch.flip_mechanism` / `breaker_switch.
    flip_event_breaker` / `gate_switch.flip_regime_gate`。最后一个会在同一动作里跑
    `gate_run.sync` 对账，那是它的既有语义，本视图不干预。

    ## 四种收场

    | 情形 | 状态码 | 说明 |
    | --- | --- | --- |
    | 真翻了 | 200 | `changed: true`，带 `from_mode` / `to_mode` / `at` |
    | **本来就是这一档** | **200** | `changed: false` + 一句 display。**不是 400** |
    | 未知 kind / mode | 400 | 请求拼错，`message` 原样透出 |
    | gate 方向对账失败 | 500 | `{"error"}`，且**明说流水已写、表未对上** |

    「本来就是这一档」用 200 而不是 400：这不是客户端的错，它是**一次成功但无变化的
    动作**——gate 方向上它甚至真的做了一件有用的事（对了一次账）。用一个错误码回报，
    前端就会把它显示成失败，而人看到「失败」只会再点一次。

    ## 权限是 `IsStaffUser`（不是 404）

    T1 的权限类**包含认证**，所以这里不叠 `IsAuthenticated`（叠了会让读者以为两者能分开
    满足）。匿名 401、普通用户 403——403 而不是 404，因为这不是「不属于你的资源」那种
    隐藏，是明确的权限拒绝；把它伪装成 404 会让人去刷新页面找一条不存在的记录。

    这一层是**界面纪律，不是安全边界**（ADR 0005）：slash 通路没有跟着收紧，任何登录
    用户仍然能在聊天里敲一行命令做同样的事。
    """
    body = request.data if isinstance(request.data, Mapping) else {}

    # --- 参数：两个都必须能被枚举认出来，认不出就是 400（请求拼错，不是资源不存在）--- #
    try:
        kind = MechanismKind(str(body.get("kind") or ""))
    except ValueError:
        allowed = " / ".join(m.value for m in MechanismKind)
        return _bad_request(f"未知的 kind：{body.get('kind')!r}（可选：{allowed}）")
    try:
        to_mode = MechanismMode(str(body.get("mode") or ""))
    except ValueError:
        allowed = " / ".join(m.value for m in MechanismMode)
        return _bad_request(f"未知的 mode：{body.get('mode')!r}（可选：{allowed}）")

    # 翻之前那一档：回执要说「从哪到哪」，而这个值必须在 `flip_*` 之前读——之后读到的
    # 已经是新档位，那样「从执行态 → 执行态」就会被回显成一次没发生过的原地切换。
    from_mode = RegimeMechanismSwitch.current(kind)

    # --- 同源求值：这一次 `page()` 既是给人看的那一页，也是即将写进 `reason` 的那句话 --- #
    # **显式传方向**，不让 `evaluate()` 按当前档位反推：它问的是「你要做的这一步」。两者
    # 在「本来就是这一档」的请求上会分家（反推给出反向，而请求的是原地不动）。
    #
    # kind / mode 上面都校验过了，所以这里不再接 `ValueError`：`evaluate()` 里剩下的那个
    # 异常源是流水表里一条手工写脏的 `to_mode`（`MechanismMode(...)` 认不出来），那是**服务端
    # 数据坏了**，照本模块「参数错与内部故障不混」的约定冒成 500，不冒充 400。
    briefing = briefing_module.evaluate(kind, to_mode=to_mode)
    reason = briefing.summary

    try:
        outcome = _apply_flip(
            kind, to_mode, actor_name=request.user.username, reason=reason
        )
    except ValueError as exc:
        # 两句可自愈的提示（「切换原因不能为空」「触发方不能为空」）。原样透出：它们是
        # 人能照着改的那一类话，而 T6 之后前端会显示响应体里的 message。
        return _bad_request(str(exc))
    except Exception as exc:  # noqa: BLE001 —— 见下
        # **只可能是 gate 方向的 `gate_run.sync` 失败**：另外两个写方不往外抛（它们只做一次
        # INSERT），而 `flip_regime_gate` 的 docstring 明说对账异常要原样往上抛——「半个动作
        # 比不动作更坏」。
        #
        # 但这里不能让它变成 Django 的 500 白页，因为**此刻库里是什么状态是需要说清的**：
        # 流水行已经落库了（档位真的翻了），而声明表还没对上。那句话不写出来，人会以为整件
        # 事都没发生，于是去按第二次——而按第二次只会再对一次账，仍然对不上，问题照旧。
        logger.exception("[regime] 网页翻 %s 之后对账失败", kind.value)
        return _server_error(
            f"{KIND_SHORT_NAME[kind]}已切到{to_mode.display}，**流水已写、表未对上**："
            f"流水行已经落库，但同一动作里的对账（gate_run.sync）失败了——"
            f"声明表此刻可能与新档位不一致。错误：{exc}"
        )

    payload = _switch_payload(kind, from_mode, to_mode, outcome, reason=reason)

    # 顺手回显对账摘要（只有 gate 方向有）。它不是给人读的正文，是给排障的人看的**事实**：
    # 这一轮到底解除了几条声明、跳过了没有。省略它，前端就只能说「成功了」。
    if outcome.sync is not None:
        payload["sync"] = dict(outcome.sync)

    response = Response(payload)
    # 与 `briefing` 同一条纪律的延伸：这一页的求值带进程级副作用（gate 会重导入全部策略
    # 模块），而它同时**改变**了库。任何缓存/重放都会让「已经翻过了」变成一个可以复现的假
    # 事实。写入口一律 no-store。
    response["Cache-Control"] = "no-store"
    return response


# --------------------------------------------------------------------------- #
# 6. pool/rebuild/ —— 投递重算（P5 子票 #47）
# --------------------------------------------------------------------------- #


@api_view(["POST"])
@permission_classes([IsStaffUser])
def pool_rebuild_dispatch(request: Request) -> Response:
    """投递一次池化表重算，**立刻返回**，不等结果（P5 子票 #47；ADR 0006）。

    ## 为什么只重建池化、不跑全链

    ADR 0006 的正文。一句话：`recompute_regime_slices` 三段串行、分钟级，塞进 HTTP 请求
    就是「点了转圈五分钟然后 502」，而反代超时不由我们决定。

    ## 立刻返回：202 + 一个 `task_id`

    `202 Accepted` 而不是 `200 OK`：200 说的是「做完了」，而这里只说了「收下了」。这个
    区别不是洁癖——前端要根据它决定**是去轮询还是直接读结果**。

    `task_id` 是这次投递的**唯一凭据**：`reused` 那一轮不建任何行，事后除了这个 id 没有
    别的办法把那一次重算找回来。所以它必须回在这里，不能等轮询端点再问一次。

    响应里刻意**不带**任何「现在是什么状态」：这一刻任务可能还没被 worker 取走，
    答什么都是猜。状态只有 `pool/rebuild/<task_id>/` 能答。

    ## 不写并发预检

    ADR 0006 明说「并发触发有现成的答案，不要另写锁」：`uniq_pool_rebuild_ready_fingerprint`
    会让第二个完成者撞上约束，`rebuild_pool` 把自己那条记成带原因的 `failed` 行并如实返回
    （`duplicate=True`）。加一道「已有 building 就先拒绝」的预检会做两件坏事：把那条约束
    的收益藏起来（人再也看不到并发真的发生了），以及引入一个新的错误状态——「被预检拒了
    所以什么都没记」。

    所以连点两次是对的：第二次如实留下一条 `duplicate` 记录，页面按「并发触发」显示，
    **不是红色失败**。

    ## 权限是 `IsStaffUser`（不是 404）

    与 `switch/` 同一条（ADR 0005）：匿名 401、普通用户 403。这一层是**界面纪律，不是安全
    边界**——`manage.py recompute_regime_slices` 任何人能跑。

    ## 投递失败是 503

    `.delay()` 抛（broker 不可用、结果后端挂了）是**通路**故障：请求合法、逻辑跑通，坏的
    是下层。500 会让人以为「可能已经跑起来了」，于是去刷新查一条不存在的记录；503 说的是
    「没投出去，再按一次」——而那一次不会撞上第一次的指纹，因为第一次根本没发生。
    """
    from apps.regime import tasks

    actor_name = request.user.username
    try:
        async_result = tasks.rebuild_regime_pool.delay(actor_name)
    except Exception as exc:  # noqa: BLE001 —— 见 docstring「投递失败是 503」
        logger.exception("[regime] 池化重算投递失败 actor=%s", actor_name)
        return _not_dispatched(f"池化重算没有投递出去：{exc}")

    logger.info(
        "[regime] 池化重算已投递 task_id=%s actor=%s",
        async_result.id,
        actor_name,
    )
    response = Response(
        {
            "dispatched": True,
            "task_id": async_result.id,
            # 投递方**必须**同时知道什么时候回来问：只给一个 id，前端只能靠猜一个间隔。
            # 这个数是建议值不是契约——真正的判据是状态读返回的 `done`。
            "poll_after_seconds": POLL_AFTER_SECONDS,
        },
        status=202,
    )
    response["Cache-Control"] = "no-store"
    return response


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def pool_rebuild_status(request: Request, task_id: str) -> Response:
    """一次重算的收场，按 `task_id` 读（P5 子票 #47）。

    ## 为什么必须有这一条：`reused` 在代际表里不存在

    票面原本写「状态读复用 T2 的 `pool/generations/` 即可」，而那是**做不到**的：

    - `reused=True` 是 `rebuild_pool` 的**最早**一个返回分支（指纹命中在它建行之前），
      那一轮**不建代、不碰格子、不留任何审计行**（ADR 0006 把这条代价写明了）。轮询代际
      表永远看不到它发生过；
    - 更糟的是「投递成功但 worker 没跑」：没人建 `building` 行，于是它与「复用」在代际表里
      **逐字节同形**——都是「什么都没变」。

    五种收场里有两种只能靠这条端点回答，所以它不是一个「更方便的入口」，是唯一入口。

    ## 五种收场

    | `outcome` | 含义 | 页面该怎么显示 |
    | --- | --- | --- |
    | `new_generation` | 新代已就绪，带 `cells_changed` | 绿 |
    | `reused` | 输入指纹未变，**本次没有产生新记录** | 中性色（不是失败） |
    | `duplicate` | 并发撞上唯一约束，自己那条记成 `failed` | 中性色（**不是红色**） |
    | `failed` | 真失败，带 `failure` 原文 | 红 |
    | `building` | 还在跑 | 转圈 |

    前四种由 `pool_rebuild.outcome_of()` **一处**判定（`reused` 先于 `status`，理由在那个
    函数里）；`building` 是这一层加的：它来自「任务还在跑」或「有一代正在建」，两种都是
    「再等等」。

    ## 为什么还要读 Celery

    `rebuild_pool` 的摘要落在 Celery 结果后端（保留 24 小时）。它是**唯一**能看见
    `reused` 与「还没开始」的地方：

    - 任务 `PENDING` 且库里没有新的 `building` 行 → 投递成功了，但 worker 还没取走。
      此时**不能**报「重算中」——那会让人以为在跑，而真相是连开始都没开始。这叫
      `pending`，页面该说「等待 worker」；
    - 任务 `SUCCESS` → 读摘要拿 `outcome` 与 `cells_changed`；
    - 任务 `FAILURE` → 真失败。**但失败原因优先取库里的 `failure` 原文**：那是
      `rebuild_pool` 自己写下的、带业务含义的那句话（比如「同一输入指纹已有一代就绪」），
      而 Celery 的异常串是一段 traceback。

    ## 结果过期之后：**Celery 分不出「过期」与「还没开始」**

    `CELERY_RESULT_EXPIRES=86400`，一天之后 Celery 不再认这个 id。但它对**未知 id** 与
    **还没开始的任务**回的是同一件事：`PENDING`。这不是本仓能修的，是 Celery 结果后端的
    性质（`apps/agent/tools/backtest.py` 有一段注释记着同一件事：「Celery AsyncResult alone
    returns PENDING for both 'never submitted' and 'waiting to start', which is ambiguous」）。

    所以本端点**不**假装能分开它们，也**不回 404**（回「没有这个任务」是在否认一件真的
    发生过的事，而人手上那个 id 也没错）。它把这份同形写进 `display` 与 `state_ambiguous`
    两处：一个过期一天的 id 看到的是「还没有 worker 取走，或结果已过期」，两条出路都写明
    （检查 worker / 去看代际历史里那一代有没有落成），读的人不需要猜是哪一种。

    ## 本端点不写任何东西

    纯读。轮询它（60 秒一次、由前端的既有节奏决定）没有副作用。
    """
    from celery.result import AsyncResult

    from celery_app import app as celery_app

    result: AsyncResult = celery_app.AsyncResult(task_id)
    state = result.state
    payload: dict[str, Any] = {
        "task_id": task_id,
        "state": state,
        "done": state in TERMINAL_STATES,
    }

    if state == "SUCCESS":
        summary = result.result
        if isinstance(summary, Mapping):
            generation = _generation_from_summary(summary)
            outcome = pool_rebuild.outcome_of(
                reused=bool(summary.get("reused")),
                duplicate=bool(summary.get("duplicate")),
                status=RebuildStatus.READY.value,
            )
            payload.update(
                {
                    "outcome": ser.outcome_payload(
                        outcome, generation=summary.get("rebuild_id")
                    ),
                    "generation": (
                        None
                        if generation is None
                        else ser.generation_summary(generation)
                    ),
                    "stale_building": ser.stale_building_payload(generation)
                    if generation is not None
                    else _no_building(),
                    # 复用那一轮的这几个数**不在任何行上**（没有建代），只能从摘要里取。
                    # 这就是「轮询代际表看不出 reused」的具体表现：它连 N 都没有。
                    "cells_changed": summary.get("cells_changed"),
                    "cells_total": summary.get("cells_total"),
                    "results_used": summary.get("results_used"),
                    "candidates": summary.get("candidates"),
                }
            )
            return _no_store(Response(payload))
        # 结果不是映射：本该不可能（本任务恒返回 dict），但 Celery 后端可以配置成丢结果。
        # 不猜，按「查不到」处理并说出来。
        payload.update(
            {
                "outcome": ser.outcome_payload(pool_rebuild.OUTCOME_FAILED),
                "generation": None,
                "stale_building": _no_building(),
                "error": "任务结果不在结果后端里（可能已被 CELERY_RESULT_EXPIRES 清掉）",
            }
        )
        return _no_store(Response(payload))

    if state in TERMINAL_STATES:
        # FAILURE / REVOKED。**失败原因优先从库里取**：`rebuild_pool` 抛之前会把那一代标成
        # `failed` 并写下带业务含义的原因；Celery 那条是 traceback，两层含义不同。
        generation = _most_recent_generation(task_id)
        # `generation.status` / `.failure` 是 `CharField` / `TextField` 描述符（本仓没装
        # `django-stubs`，pyright 看到的是字段对象而不是运行时值）。用 `str(...)` 把「列在
        # 运行时是什么」收在这一处——与 `serializers.iso` 的取向同源，不在每个调用点各写
        # 一遍 `cast`。
        row_status = "" if generation is None else str(generation.status)
        failure = (
            str(generation.failure)
            if generation is not None
            and generation.status == RebuildStatus.FAILED.value
            else ""
        )
        outcome = pool_rebuild.outcome_of(
            status=row_status or RebuildStatus.FAILED.value,
            failure=failure,
        )
        payload.update(
            {
                "outcome": ser.outcome_payload(
                    outcome, generation=generation.pk if generation else None
                ),
                "generation": (
                    None if generation is None else ser.generation_summary(generation)
                ),
                "stale_building": _no_building(),
                # Celery 那边的异常串**照原样透出**，但标成 `celery_error`，不冒充 failure：
                # `failure` 是库里那一列（有业务含义），两者不是一回事。
                "celery_error": _failure_text(result),
                "failure": failure or _failure_text(result),
            }
        )
        return _no_store(Response(payload))

    # PENDING / STARTED / RETRY：还没到终局。**区分「在建」与「还没开始」**——把没开始的
    # 说成「重算中」，就是让一个 worker 没跑的系统看起来在正常工作（ADR 0006 明令要说出
    # 来的那个缺口）。
    building = _current_building()
    if building is None:
        payload.update(
            {
                "outcome": None,
                "generation": None,
                "stale_building": _no_building(),
                # 这一句是「投递成功但 worker 没跑」的唯一说辞：它不是重算中，是没开始。
                # `state_ambiguous` 把 Celery 那条同形（未知 id 也回 PENDING）写出来，
                # 免得一个过期一天的 id 被读成「worker 停了」。
                "display": PENDING_DISPLAY,
                "state_ambiguous": True,
            }
        )
    else:
        stale = ser.stale_building_payload(building)
        payload.update(
            {
                "outcome": ser.outcome_payload(pool_rebuild.OUTCOME_BUILDING),
                "generation": ser.generation_summary(building),
                "stale_building": stale,
                # 阈值到点就说人话。「超过 N 分钟仍在重算中，检查 worker」——N 由后端给，
                # 前端不另写一份（`stale_building.threshold_minutes`）。
                "display": (
                    f"超过 {stale['threshold_minutes']} 分钟仍在重算中，检查 worker"
                    if stale["stale"]
                    else pool_rebuild.OUTCOME_DISPLAY[pool_rebuild.OUTCOME_BUILDING]
                ),
            }
        )
    return _no_store(Response(payload))


# --------------------------------------------------------------------------- #
# 7. strategies/ —— 策略的「机制在册」总开关（删除策略 / 恢复启用）
# --------------------------------------------------------------------------- #


def _lifecycle_page_payload(page: strategy_lifecycle.Page) -> dict[str, Any]:
    """**确认页**（`GET`）的传输形状。**回执（`POST`）不复用它**——那个形状见
    `_flip_strategy`：回执说的是「按下之后」，而这里的 `counts` / `body` 说的是「按下
    之前」，两套事实混进同一个对象，读的人分不清哪几个数属于哪个时刻。要那一页，GET
    那一条随时给。

    `available` 是**布尔数据**不是文案：「这一次会不会真的写东西」是事实，而「本来就是
    这一档」那句人话由 `body` 给（前者给前端做按钮态，后者给人读）。
    """
    return {
        "strategy": {"id": str(page.strategy_id), "name": page.name},
        "action": {
            "value": "restore" if page.to_active else "retire",
            "display": page.action_display,
        },
        "available": page.available,
        "body": page.body,
        "summary": page.summary,
        "counts": {
            "active_sessions": page.counts.active_sessions,
            "declarations": page.counts.declarations,
            "decisions": page.counts.decisions,
            "pool_cells": page.counts.pool_cells,
        },
    }


def _strategy_or_404(strategy_id: UUID) -> tuple[Strategy | None, Response | None]:
    """按 id 取策略；取不到就给出那一条 404。**返回 `(对象, 响应)` 而不是抛异常**：
    本模块的错误形状是 `{"error"}`，让 `Strategy.DoesNotExist` 冒成 Django 的 404 白页
    就把那句「哪一条不存在」丢掉了。
    """
    strategy = Strategy.objects.filter(pk=strategy_id).first()
    if strategy is None:
        return None, _not_found(f"没有 id={strategy_id} 的这一条策略")
    return strategy, None


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def strategy_lifecycle_page(request: Request, strategy_id: UUID) -> Response:
    """一条策略的启停确认页（`GET api/regime/strategies/<id>/`）。

    **两个方向一次都给**（退役 / 恢复启用）：这一页是给人做决定用的，而人会先看到「它
    此刻在册」再决定往哪边翻；分成两条路由只会让前端先猜方向、再为一个本来就已知的
    事实多打一次请求。

    `available=false` 的那一侧仍然带完整的 `body`/`summary`（不是空壳）：它就是那句
    「本来就是这一档」，前端原样显示即可，不必自己写第二份。

    读对所有登录用户开放（与 `briefing` 同一条），写才是 `is_staff`（ADR 0005）。
    """
    strategy, missing = _strategy_or_404(strategy_id)
    if missing is not None:
        return missing

    retire = strategy_lifecycle.page(strategy, to_active=False)
    restore = strategy_lifecycle.page(strategy, to_active=True)
    response = Response(
        {
            "strategy": {
                "id": str(strategy.pk),
                "name": str(strategy.name),
                "lifecycle": ser.lifecycle_payload(bool(strategy.is_active)),
                "latest_change": ser.lifecycle_change_payload(
                    strategy_lifecycle.changes_for([strategy.pk]).get(strategy.pk)
                ),
            },
            "retire": _lifecycle_page_payload(retire),
            "restore": _lifecycle_page_payload(restore),
        }
    )
    # 与 `briefing` 同一取向：这一页答的是「此刻是什么状态」，缓存它就会有一条可以复现
    # 的假事实（别人刚翻过，而页面还在说旧状态）。
    response["Cache-Control"] = "no-store"
    return response


def _flip_strategy(request: Request, strategy_id: UUID, *, to_active: bool) -> Response:
    """退役 / 恢复启用**共用的那一段**：取策略 → 翻 → 回执。两个视图只差 `to_active`。

    写的是 `Strategy.is_active` 与一条 `StrategyLifecycleChange` 流水，**没有别的动作**：
    不停会话、不写声明表、不删任何数据（理由见 `strategy_lifecycle` 的模块 docstring）。
    """
    strategy, missing = _strategy_or_404(strategy_id)
    if missing is not None:
        return missing

    try:
        outcome = strategy_lifecycle.flip(
            strategy,
            to_active=to_active,
            actor_kind=ActorKind.WEB,
            actor_name=request.user.username,
        )
    except ValueError as exc:
        # 「触发方不能为空」这类可自愈的提示，原样透出（与 `switch` 同一条）。
        return _bad_request(str(exc))

    page = outcome.page
    payload = {
        "strategy": {
            "id": str(page.strategy_id),
            "name": page.name,
            # **翻转之后**的档位（`flip` 把内存里那一份也改了）。页面据此立刻换掉按钮态，
            # 不必为这一个布尔再打一次 GET。
            "lifecycle": ser.lifecycle_payload(bool(strategy.is_active)),
        },
        "action": {
            "value": "restore" if to_active else "retire",
            "display": page.action_display,
        },
        "changed": outcome.changed,
        # 回执那句话：真的翻了就是「已退役 / 已恢复启用」，没翻就是「本来就是这一档」。
        "display": outcome.display,
        # 落库的那一串恒回显——它是人核对「页面上看到的那一页」与「流水里记下的那句」
        # 是否一致的唯一凭据。没写流水时回显的是本次求值那一份（它没落库）。
        "reason": page.summary,
        "at": ser.iso(outcome.row.at) if outcome.row is not None else None,
    }
    # 回执照 `switch` 的形状（`{changed, display, reason, at}`）而不是确认页的形状：
    # 确认页那几个键（`available` / `body` / `counts`）说的是「按下去之前是什么世界」，
    # 放进回执里就成了动作之后的第二次求值——两个时刻的事实混在一个对象里，读的人分不清
    # 哪几个数是按下前的。要那一页，GET 那一条随时给。
    response = Response(payload)
    response["Cache-Control"] = "no-store"
    return response


@api_view(["POST"])
@permission_classes([IsStaffUser])
def retire_strategy(request: Request, strategy_id: UUID) -> Response:
    """**删除策略**（`POST api/regime/strategies/<id>/retire/`）：把它退役。

    ## 这个按钮叫「删除」，做的却是退役——页面必须说清楚

    它翻的是 `Strategy.is_active`（CONTEXT.md 第 106 条：`False` = 已退役，永不被自动
    停用、也永不被自动恢复）。**一行数据都不删**：回测、切片、池化格子、停用决策、豁免
    全部保留（`strategy_lifecycle` 的模块 docstring 有那张对照表）。页面上「删除」这个词
    与「删数据」不是一回事，所以确认页正文由后端渲染、逐条写明做与不做。

    ## 它会放开一条正在跑的策略

    交易链路不看 `is_active`，所以退役**不会**停止任何会话；而机制会因此不再拦它——下一轮
    gate 对账按既有判据（`gate_run._warrant`：不在被管集合里或 `is_active=False` ⇒
    `GONE`）解除它的 `strategy:<id>` 声明。**解除之后，正在跑它的会话可以重新开新仓。**
    这是「删除」与用户直觉相反的那一面，正文里明写了（含活跃会话条数）。

    ## 四种收场

    | 情形 | 状态码 | 说明 |
    | --- | --- | --- |
    | 真退役 | 200 | `changed: true` + `at`（落了一条流水） |
    | **本来就是退役状态** | **200** | `changed: false` + 一句「本来就是」 |
    | 未知策略 id | 404 | `{"error"}`，`id` 不是 uuid 时由路由给 404 |
    | 非 staff | 403 | `IsStaffUser`（匿名 401） |

    「本来就是这一档」用 200 而不是 400：与 `switch` 的「本来就是这一档」同源——它不是
    客户端的错，是一次成功但无变化的动作，用错误码回报会让人以为失败而再点一次。

    ## 权限是 `IsStaffUser`（ADR 0005：界面纪律，不是安全边界）

    这一层挡不住任何人：管理命令与 slash 通路没跟着收紧。页面文案与 docstring 都要把
    这句话说出来，否则下一个人会以为权限已经收口。
    """
    return _flip_strategy(request, strategy_id, to_active=False)


@api_view(["POST"])
@permission_classes([IsStaffUser])
def restore_strategy(request: Request, strategy_id: UUID) -> Response:
    """**恢复启用**（`POST api/regime/strategies/<id>/restore/`）：把退役翻回去。

    为什么需要一个反面按钮：`is_active=False` 的语义里写着「永不被**自动**恢复」，于是
    退役之后**没有任何自动路径**能把它放回来；页面上只给一个单向按钮，等于一次误点要
    人去数据库里改回来。这里给的是**人**按下才算数的那条路（机制依然一个方向都不做）。

    **它不产生任何豁免**：豁免是「人工恢复」那条通路（`DeactivationExemption`，键为
    策略 × 阶段、带生效期），与启停不是一回事。恢复启用只是把策略放回被管集合——下一轮
    gate 对账若仍判它在当前阶段不适用，它会**再次被停**（确认页正文写明了这一条，免得
    人以为按一下就有了通行证）。

    收场与 `retire_strategy` 同形：本来就是启用状态 → 200 + `changed: false`。
    """
    return _flip_strategy(request, strategy_id, to_active=True)
