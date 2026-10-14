"""池化表重建（第①段单元 7i）：把纯函数池化的产物落成**一代**可读的表。

`pool.py` 是那套判定的正文（合并、门槛、冲突、原型兜底），本模块只做四件事：
**取样本 → 认指纹 → 落一代 → 报差异**。判定逻辑一行都不在这里，改判定请改 `pool.py`。

## 为什么是「一代」而不是「一张表」

`RegimePoolCell` 上挂着 `rebuild` 外键，读者**只读最近一代 `ready`**。于是重建过程
天然是「先失效后重建」：新起的那一代在翻成 `ready` 之前对任何读者都不存在，跑到一半
崩溃留下的是一代永远 `building` 的记录，而不是一张新旧混合的表。CONTEXT.md 要的
「落地前把当期池化表整体标记为重算中」在这里不需要任何显式标记——**换代本身就是那个
标记**，而多一个「重算中」标志位就多一个「标志位忘了清掉」的故障形状。

崩溃留下的 `building` 行是**记录不是垃圾**（见 `RebuildStatus` 的 docstring），不清理。

## 指纹：为什么它可以省掉一次重建，而且这是正确的

`input_fingerprint` 盖住的是**池化的全部输入**：池化算法版本、证据门槛、每一份样本的
逐日曲线与逐笔样本、以及被排除的那些切片。`build_pool` 是这些输入的纯函数，所以
「指纹相同」蕴含「结论会逐格相同」——此时再建一代不是幂等性的证明，而是让「当前是
哪一代」变成一个靠 id 大小猜的问题（模型上那条 `uniq_pool_rebuild_ready_fingerprint`
说的就是这件事）。因此指纹命中时**直接复用**、不落新记录、不碰格子。

代价要说清楚：**这一轮重算因此没有留下记录**。「每次重算必须落一条记录」指的是发生了
重算的那几次，而不是「每次有人敲了命令」。命令自己会把「复用第 N 代」打到 stdout，
那才是这条路径的可见性。

## 排除计数按**切片**数，不按策略数

`PoolResult.excluded` 是 `{策略 id: 原因}`，一个策略有多份切片时后面的会盖掉前面的
——那是给「这条策略为什么没进池化」用的。重算记录要回答的是「看了 N 个回测，用上 M 个，
差额去哪了」，所以计数在**取样本那一层**自己数（`exclusions`），不复用那个字段。

`results_used < candidates` 是正常的：候选里有还没切过片的、有本金 ≤ 0 算不出归一化
样本的。**按原因分开计数**，因为「本金为 0」要人去查回测参数，「没切过片」只要跑一次
重算入口——混成一个数就没法决定该做哪个。

## 这里不调 `close_old_connections()`

读样本是**一次查询**（`iterator` 服务端游标），写格子是几批 `bulk_create`，中间没有
长循环里的重复事务边界。而 `close_old_connections()` 在 `TestCase` 里会直接掐掉测试
那条连接（`CONN_MAX_AGE=0` ⇒ `close_at` 就是连接建立的时刻）——长循环的连接纪律属于
**调用方**（`recompute_regime_slices` 已经自带），不属于这个被调用的函数。
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections import Counter
from dataclasses import asdict
from datetime import date
from typing import Any, Iterable, Mapping, Sequence

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.backtest.models import BacktestResult
from apps.regime import config, pool, slicing
from apps.regime.models import (
    ActorKind,
    ArchetypeOverride,
    RebuildStatus,
    RegimePoolCell,
    RegimePoolRebuild,
)

logger = logging.getLogger(__name__)

#: 写格子时的每批条数。格子数 = 样本里出现的策略数 × 4，正常规模下一次性 `bulk_create`
#: 也就几百行；分批是为了策略表将来涨到几千条时内存不至于被一次请求撑爆，**不是**事务
#: 边界——这些行在整代翻成 `ready` 之前对读者都不存在（见模块 docstring）。
CELL_BATCH_SIZE = 200

#: 读回测结果时每多少条断一次服务端游标。
READ_CHUNK_SIZE = 200

#: `exclusions` 里保证出现的三个键（`pool.EXCLUDED_*` 的取值本身，不另起中文名）。
#: 计数为 0 也留着：一个恒定的键集让「某个原因从没出现过」与「这个键被改名了」能分开。
EXCLUSION_KEYS = (
    pool.EXCLUDED_NO_PAYLOAD,
    pool.EXCLUDED_NO_MERGE_INPUTS,
    pool.EXCLUDED_UNUSABLE_CAPITAL,
)


# --------------------------------------------------------------------------- #
# 当前代
# --------------------------------------------------------------------------- #


def current_generation() -> RegimePoolRebuild | None:
    """**当前**那一代池化表：最近一次翻成 `ready` 的那一代。

    按 `finished_at` 倒序而不是 `started_at`：两代并发时先起跑的那一代可能后完成，
    而「当前」问的是**哪一代最后咬人**，不是哪一代最后起步。模型 `Meta` 上的
    `-started_at` 是给管理界面列记录用的，语义不同，所以这里显式排序。
    """
    return (
        RegimePoolRebuild.objects.filter(status=RebuildStatus.READY.value)
        .order_by("-finished_at", "-id")
        .first()
    )


def current_cells(
    generation: RegimePoolRebuild | None = None,
) -> dict[tuple[int, str], dict]:
    """一代池化表的全部格子，键为 `(策略 id, 阶段)`。默认读**当前代**。

    没有当前代（从没重算过）时给空字典而不是抛：冷启动下「没有任何适用性结论」是
    正常状态，调用方要能区分它与故障。

    `generation` 只在调用方**已经**取过当前代、还要拿格子与它配对时传（`deactivation_run`
    就是这种）：不传参时本函数内部会自己再问一次当前代，而两次问之间可能正好翻了一代，
    于是调用方手里的代 id 与拿到的格子来自不同两代——落进停用决策就是「依据写的是第 N 代，
    引用的却是第 N+1 代那一格」。所以配对读取这件事必须由调用方一次性钉住，而不是靠
    「两次调用之间不会发生什么」。
    """
    if generation is None:
        generation = current_generation()
    if generation is None:
        return {}
    rows = RegimePoolCell.objects.filter(rebuild=generation).values(
        "strategy_id", "regime", "source", "state", "reason", "evidence", "needs_review"
    )
    return {(row["strategy_id"], row["regime"]): row for row in rows}


# --------------------------------------------------------------------------- #
# 原型
# --------------------------------------------------------------------------- #


def ensure_strategies_discovered() -> list[str]:
    """让注册表**当下**装的是磁盘上的策略，而不是进程启动那一刻的。

    `apps.py:ready()` 只在 `~/.tradelogx/strategies` 当时存在时才 `discover()`。长驻的
    Celery worker 可能在那目录建起来之前就启动了，于是整批策略解析不到实现类、全部落到
    「未归类」——池化照跑、日报照出，只有一个数字在说话（`strategy_raw_texts` 的
    docstring 管这叫「安静的错误形状」）。所以**入口**自己补一次。

    为什么不放进 `rebuild_pool`：`discover()` 是 `os.listdir` + 逐个 `importlib` 重导入
    的**进程级副作用**，属于入口点（命令、定时任务），不属于一个取数算数的函数；而且它
    在测试进程里会把宿主机那份策略目录拖进来，让用例的结论依赖运行环境。两个入口各调
    一次，代价是重导入一次模块列表，换来的是没有一个入口会忘。
    """
    from apps.strategy_engine.registry import StrategyRegistry

    return StrategyRegistry.discover()


def strategy_raw_texts(strategy_ids: Iterable[int]) -> tuple[dict[int, str], list[int]]:
    """策略 → 注册表里那份 `description` 原文；顺带返回**解析不到实现类**的策略 id。

    注册表是「这条策略还有代码在跑吗」的唯一出处（CONTEXT.md 的被管策略集合第一条
    判据）。解析不到的给空串，于是它会归到「未归类」并被计数——那是**如实**的结论，
    不是错误：回测自动建出来的幽灵策略行本来就没有实现类。

    返回的第二个值是给调用方吼一声用的：解析不到的策略一多，多半是
    `~/.tradelogx/strategies` 这个目录当时不存在（`apps.ready` 会静默跳过 discover），
    而不是那些策略真的都没了。那是一种**安静的错误形状**——全部落到「未归类」兜底，
    池化照跑、日报照出，只有第④段那一个数字在说话。
    """
    from apps.trading.models import Strategy

    from apps.strategy_engine.registry import StrategyRegistry

    ids = list(strategy_ids)
    if not ids:
        return {}, []

    names = dict(Strategy.objects.filter(id__in=ids).values_list("id", "name"))
    registered = set(StrategyRegistry.list_registered())

    texts: dict[int, str] = {}
    unresolved: list[int] = []
    for strategy_id in ids:
        name = names.get(strategy_id)
        if name not in registered:
            texts[strategy_id] = ""
            unresolved.append(strategy_id)
            continue
        texts[strategy_id] = (
            getattr(StrategyRegistry.get_class(name), "description", "") or ""
        )
    return texts, unresolved


def archetype_matches(
    strategy_ids: Iterable[int],
    *,
    raw_texts: Mapping[int, str] | None = None,
    overrides: Mapping[int, str] | None = None,
) -> dict[int, pool.ArchetypeMatch]:
    """策略 id → 原型归属。`overrides` 不传就现查 `ArchetypeOverride`。

    `build_pool` 要求 `matches` **覆盖到每一个有样本的策略**（见它的 docstring），
    所以这里对每个 id 都给一条，包括样本充足的——它们当下用不到，但「这条策略归到
    哪一类」不该随样本多寡变成一个没人记录过的事实。
    """
    ids = list(strategy_ids)
    if not ids:
        return {}

    if raw_texts is None:
        raw_texts, _ = strategy_raw_texts(ids)
    if overrides is None:
        overrides = dict(
            ArchetypeOverride.objects.filter(strategy_id__in=ids).values_list(
                "strategy_id", "archetype"
            )
        )
    return {
        strategy_id: pool.resolve_archetype(
            raw_texts.get(strategy_id, ""), overrides.get(strategy_id)
        )
        for strategy_id in ids
    }


# --------------------------------------------------------------------------- #
# 取样本
# --------------------------------------------------------------------------- #


def _result_rows():
    """要送进池化的全部回测结果。只取用得上的列（`metrics` 是必须的：切片在里面）。"""
    return (
        BacktestResult.objects.only("id", "strategy_id", "symbol", "metrics")
        .order_by("created_at")
        .iterator(chunk_size=READ_CHUNK_SIZE)
    )


def collect_samples() -> tuple[list[pool.SliceSample], Counter, int]:
    """读全部回测结果，取出可用的池化样本。

    返回 `(样本, 按原因的排除计数, 候选总数)`。排除计数在**这一层**按切片数，不复用
    `PoolResult.excluded`（那是按策略的，见模块 docstring）。
    """
    samples: list[pool.SliceSample] = []
    exclusions: Counter = Counter()
    candidates = 0

    for result in _result_rows():
        candidates += 1
        loaded = pool.load_sample(
            slicing.stored_slice(result),
            result_id=str(result.id),
            strategy_id=result.strategy_id,
            symbol=result.symbol,
        )
        if loaded.sample is None:
            exclusions[loaded.excluded] += 1
            continue
        samples.append(loaded.sample)

    # 恒定键集（见 EXCLUSION_KEYS 的注释）。
    for key in EXCLUSION_KEYS:
        exclusions.setdefault(key, 0)
    return samples, exclusions, candidates


# --------------------------------------------------------------------------- #
# 指纹
# --------------------------------------------------------------------------- #


def _jsonable_sample(sample: pool.SliceSample) -> dict[str, Any]:
    """一份样本的确定性字面量。日期一律 ISO 字符串：JSON 里没有日期这种类型。"""

    def curve(daily: Mapping[date, float]) -> list[list]:
        return [[day.isoformat(), value] for day, value in sorted(daily.items())]

    return {
        "result_id": str(sample.result_id),
        "strategy_id": sample.strategy_id,
        "symbol": sample.symbol,
        "window": [sample.window[0].isoformat(), sample.window[1].isoformat()],
        "cell_daily": {
            r: curve(sample.cell_daily.get(r, {})) for r in sorted(sample.cell_daily)
        },
        "cell_trades": {
            r: [
                [day.isoformat(), value] for day, value in sample.cell_trades.get(r, ())
            ]
            for r in sorted(sample.cell_trades)
        },
        "cell_regime_days": {
            r: int(n) for r, n in sorted(sample.cell_regime_days.items())
        },
        "full_daily": curve(sample.full_daily),
    }


def _evidence_block(params: config.EvidenceConfig) -> dict[str, Any]:
    """指纹里那一块证据门槛：**本次实际生效**的那一组，不是登记在 `GROUPS` 里的那一份。

    底子取整个分组（`config.snapshot("evidence")`），再拿 `params` 盖上去，于是
    「将来给 `EVIDENCE` 加字段」仍然自动进指纹，不必记得回来补一行。

    为什么必须盖：`build_pool` 用的是传进来的 `params`，指纹却去读全局的
    `config.EVIDENCE` 的话，两次**门槛不同**的重算会算出同一个指纹，第二次直接复用
    第一次那一代——一个把「换了门槛」读成「什么都没变」的静默错误。带上类名，是因为
    「换了一个类、字段恰好同名同值」也是换了一套口径。
    """
    block: dict[str, Any] = {"class": type(params).__name__}
    block.update(config.snapshot("evidence"))
    block.update(asdict(params))
    return block


def input_fingerprint(
    samples: Sequence[pool.SliceSample],
    exclusions: Mapping[str, int],
    params: config.EvidenceConfig | None = None,
) -> str:
    """本次重算「看进去了什么」的指纹。

    盖住四样，每一样都能改变结论或改变记录里该看到的数：

    - 池化算法版本（`pool.POOL_VERSION`）——合并口径变了，同样的样本会有不同结论；
    - **整个**证据分组（`config.snapshot`，不是只取两个门槛）——将来给 `EVIDENCE`
      加字段时它自动进指纹，不必记得回来补一行；
    - 每一份样本的逐日曲线、逐笔样本、该阶段天数；
    - 排除计数（它进重算记录，属于产出的一部分）。

    `sorted` + `sort_keys` + 紧凑分隔符：指纹与样本的到来顺序无关，也与 JSON 的
    空白风格无关。
    """
    payload = {
        "pool_version": pool.POOL_VERSION,
        "evidence": _evidence_block(params or config.EVIDENCE),
        "samples": [
            _jsonable_sample(sample)
            for sample in sorted(samples, key=lambda s: str(s.result_id))
        ],
        "exclusions": {key: int(value) for key, value in sorted(exclusions.items())},
    }
    blob = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# 重建
# --------------------------------------------------------------------------- #


def _cell_changes(
    previous: Mapping[tuple[int, str], tuple],
    cells: Mapping[tuple[int, str], pool.PooledCell],
) -> int:
    """相对上一代变了多少格。

    **比的是结论三元组 `(状态, 原因, 来源)`**，不比证据里的数字：证据每天都在动
    （样本多一笔、窗口多一天），拿它当差异会让这个数恒等于格子总数，而它的全部用处
    就是「这次重算到底改变了什么」。`needs_review` 也不比——它由冲突推出，冲突的
    变化必然已经体现在状态或原因上。

    没有上一代（首次重建）时全部计入变化：从「什么都没有」变成一整代结论，那确实
    是每一格都变了。
    """
    current = {
        key: (cell.state, cell.reason, cell.source) for key, cell in cells.items()
    }
    return sum(
        1
        for key in set(previous) | set(current)
        if previous.get(key) != current.get(key)
    )


def _previous_triples() -> dict[tuple[int, str], tuple]:
    generation = current_generation()
    if generation is None:
        return {}
    rows = RegimePoolCell.objects.filter(rebuild=generation).values_list(
        "strategy_id", "regime", "state", "reason", "source"
    )
    return {
        (sid, regime): (state, reason, source)
        for sid, regime, state, reason, source in rows
    }


def _needs_review_count(cells: Mapping[Any, pool.PooledCell]) -> int:
    return sum(1 for cell in cells.values() if cell.needs_review)


def _generation_summary(generation: RegimePoolRebuild) -> tuple[list[int], int]:
    """读回一代的「未归类策略 id」与「待复核格子数」。

    复用那一轮要用它：那时**没有** `PoolResult` 可问（本轮压根没算），而这两个数进的是
    重算记录和日报，返回一个恒定的 `[]`/`0` 就是拿「没算」冒充「算出来是空」。两者在
    日报上长得一模一样，而它们要求的事情相反——一个是「去看策略描述」，一个是「没事」。

    判据与 `_pool_cell` 落在格子上的东西同源：`source=archetype` 说明走的是兜底，
    兜底的原型名是「未归类」说明连兜底都没接到。
    """
    unclassified: set[int] = set()
    needs_review = 0
    rows = RegimePoolCell.objects.filter(rebuild=generation).values_list(
        "strategy_id", "source", "needs_review", "evidence"
    )
    for strategy_id, source, review, evidence in rows:
        if review:
            needs_review += 1
        if source == pool.POOL_SOURCE_ARCHETYPE and (
            (evidence or {}).get("archetype", {}).get("name")
            == pool.ARCHETYPE_UNCLASSIFIED
        ):
            unclassified.add(strategy_id)
    return sorted(unclassified), needs_review


def rebuild_pool(
    *,
    actor_kind: str = ActorKind.TASK.value,
    actor_name: str = "",
    params: config.EvidenceConfig | None = None,
    now=None,
) -> dict:
    """全量重建池化表，产出一代新的 `ready`。**同步**执行，不投递任务。

    返回值进命令的 stdout 与日志，不静默。三种收场各自说清楚：

    - `reused=True`：输入指纹与当前 `ready` 那一代相同，直接复用，没有落新代；
    - `duplicate=True`：并发触发的第二方——格子和那一代都写了，但翻 `ready` 时撞上
      `uniq_pool_rebuild_ready_fingerprint`。这不是故障，是那条约束**在起作用**，
      所以记成一条带原因的 `failed` 行并如实返回，而不是把整条命令炸掉；
    - 其余：正常翻代。

    `now` 只为测试注入；生产路径取 `timezone.now()`。
    """
    params = params or config.EVIDENCE
    now = now or timezone.now()

    samples, exclusions, candidates = collect_samples()
    fingerprint = input_fingerprint(samples, exclusions, params)

    existing = RegimePoolRebuild.objects.filter(
        status=RebuildStatus.READY.value, input_fingerprint=fingerprint
    ).first()
    if existing is not None:
        unclassified, needs_review = _generation_summary(existing)
        logger.info("[regime] 池化输入指纹未变，复用第 %s 代", existing.pk)
        return {
            "rebuild_id": existing.pk,
            "reused": True,
            "duplicate": False,
            "fingerprint": fingerprint,
            "candidates": candidates,
            "results_used": len(samples),
            "cells_total": existing.cells_total,
            "cells_changed": existing.cells_changed,
            "exclusions": dict(exclusions),
            "unclassified": unclassified,
            "needs_review": needs_review,
        }

    matches = archetype_matches([s.strategy_id for s in samples])
    result = pool.build_pool(samples, matches, params=params)

    rebuild = RegimePoolRebuild.objects.create(
        status=RebuildStatus.BUILDING.value,
        actor_kind=actor_kind,
        actor_name=actor_name,
        input_fingerprint=fingerprint,
        pool_version=pool.POOL_VERSION,
        candidates=candidates,
        results_used=len(samples),
        cells_total=len(result.cells),
        exclusions=dict(exclusions),
        started_at=now,
    )

    try:
        previous = _previous_triples()
        changed = _cell_changes(previous, result.cells)
        _write_cells(rebuild, result.cells)
        _mark_ready(rebuild, changed, now)
    except IntegrityError:
        # 并发触发的第二方：同一指纹已有一代就绪。约束按设计拦下了它。
        _mark_failed(
            rebuild,
            "同一输入指纹已有一代就绪：本次为重复触发，未替换当前代",
            now,
        )
        logger.warning(
            "[regime] 池化重算撞上同一指纹的就绪代，本次未替换 #%s", rebuild.pk
        )
        return {
            "rebuild_id": rebuild.pk,
            "reused": False,
            "duplicate": True,
            "fingerprint": fingerprint,
            "candidates": candidates,
            "results_used": len(samples),
            "cells_total": len(result.cells),
            "cells_changed": 0,
            "exclusions": dict(exclusions),
            "unclassified": list(result.unclassified_strategy_ids),
            "needs_review": _needs_review_count(result.cells),
        }
    except Exception as exc:
        # 真故障：留下一代 failed 行（连同失败原因），再往上抛。让调用方看见，
        # 但不让这次的残骸看起来像一代正常的表。
        _mark_failed(rebuild, str(exc), now)
        logger.error("[regime] 池化重算失败 #%s", rebuild.pk, exc_info=True)
        raise

    summary = {
        "rebuild_id": rebuild.pk,
        "reused": False,
        "duplicate": False,
        "fingerprint": fingerprint,
        "candidates": candidates,
        "results_used": len(samples),
        "cells_total": len(result.cells),
        "cells_changed": changed,
        "exclusions": dict(exclusions),
        "unclassified": list(result.unclassified_strategy_ids),
        "needs_review": _needs_review_count(result.cells),
    }
    logger.info(
        "[regime] 池化重算完成 #%s 候选=%s 采用=%s 格子=%s 变化=%s",
        rebuild.pk,
        summary["candidates"],
        summary["results_used"],
        summary["cells_total"],
        changed,
    )
    return summary


def _write_cells(
    rebuild: RegimePoolRebuild, cells: Mapping[Any, pool.PooledCell]
) -> None:
    """把格子分批写下去。顺序固定（策略 id、阶段优先级），只为了重算两次的行序一致。"""
    buffer: list[RegimePoolCell] = []
    for key in sorted(cells, key=lambda k: (k[0], k[1])):
        cell = cells[key]
        buffer.append(
            RegimePoolCell(
                rebuild=rebuild,
                strategy_id=cell.strategy_id,
                regime=cell.regime,
                source=cell.source,
                state=cell.state,
                reason=cell.reason,
                evidence=cell.evidence,
                needs_review=cell.needs_review,
            )
        )
        if len(buffer) >= CELL_BATCH_SIZE:
            RegimePoolCell.objects.bulk_create(buffer, batch_size=CELL_BATCH_SIZE)
            buffer.clear()
    if buffer:
        RegimePoolCell.objects.bulk_create(buffer, batch_size=CELL_BATCH_SIZE)


def _mark_ready(rebuild: RegimePoolRebuild, changed: int, now) -> None:
    """翻代。**这一步是原子替换点**：此前这一代对读者不存在，此后它就是「当前」。

    `cells_changed` 与 `finished_at` 一起在这里落——它们描述的是「这一代最终成了
    什么样」，写在创建时只能是猜测。`update_fields` 只带这三个字段，是因为这行
    此刻可能已经被别的进程读过（管理界面、重放），不该顺手把它们看到的字段回写。
    """
    with transaction.atomic():
        RegimePoolRebuild.objects.filter(pk=rebuild.pk).update(
            status=RebuildStatus.READY.value,
            cells_changed=changed,
            finished_at=now,
        )
    rebuild.status = RebuildStatus.READY.value
    rebuild.cells_changed = changed
    rebuild.finished_at = now


def _mark_failed(rebuild: RegimePoolRebuild, reason: str, now) -> None:
    """把这一代标成失败，连同原因。**不删格子**：残骸与原因一起才回答得了「为什么会这样」。"""
    RegimePoolRebuild.objects.filter(pk=rebuild.pk).update(
        status=RebuildStatus.FAILED.value,
        failure=reason,
        finished_at=now,
    )
    rebuild.status = RebuildStatus.FAILED.value
    rebuild.failure = reason
    rebuild.finished_at = now
