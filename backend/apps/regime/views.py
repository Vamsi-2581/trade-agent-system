"""机制 Web 读面的端点（P5 子票 #41 与 #48）。

| 路由 | 装什么 | 为什么是这个形状 |
| --- | --- | --- |
| `GET api/regime/overview/` | 三个开关的当前档 + 最近一次切换、当前代摘要、当前生效阶段 | 首屏要的**全部**廉价数，一次请求装完 |
| `GET api/regime/pool/` | 当前代（或指定代）的全部格子 | 池化表本体，键是 `(策略, 阶段)` |
| `GET api/regime/pool/generations/` | 代际历史分页 + 指定代与上一代的逐格差异 | 「这一代谁何时算的、变了什么」 |
| `GET api/regime/briefing/<kind>/` | 三张确认页之一（正文 + 审计串 + 排版用的数） | **重请求，按需拉**——不在轮询里 |

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
from typing import Any

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response

from apps.regime import briefing as briefing_module
from apps.regime import deactivation_run, pool_rebuild
from apps.regime import serializers as ser
from apps.regime.models import (
    MechanismKind,
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


# --------------------------------------------------------------------------- #
# 小件
# --------------------------------------------------------------------------- #


def _bad_request(message: str) -> Response:
    return Response({"error": message}, status=400)


def _not_found(message: str) -> Response:
    return Response({"error": message}, status=404)


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
    """
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

    # 配对读取：同一个 `generation` 对象既给 id 又给格子（见 docstring）。
    cells = pool_rebuild.current_cells(generation)
    names = _strategy_names(ser.strategy_ids(cells))
    ordered = ser.ordered_cell_keys(cells, names)

    return Response(
        {
            **_pool_state(generation),
            # 矩阵的列序由后端给（`serializers.ORDERED_REGIMES`），前端按数组排。
            "regimes": [ser.regime_payload(regime) for regime in ser.ORDERED_REGIMES],
            "cells": [
                {
                    "key": ser.cell_key(*key),
                    **ser.cell_payload(cells[key], names.get(key[0], "")),
                }
                for key in ordered
            ],
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
    """
    try:
        payload = briefing_module.payload(kind)
    except ValueError:
        allowed = " / ".join(m.value for m in MechanismKind)
        return _bad_request(f"未知的 kind：{kind!r}（可选：{allowed}）")

    response = Response(payload)
    # 重请求 + 进程级副作用：不许缓存。`no-store` 而不是 `no-cache`——后者还允许存下来
    # （只是用之前要回源），而这里连「存下来」都不该发生。
    response["Cache-Control"] = "no-store"
    return response
