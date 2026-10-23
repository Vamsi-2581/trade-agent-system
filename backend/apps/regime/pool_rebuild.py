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
from datetime import date, datetime, timedelta
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

#: 并发触发的第二方在 `failed` 行上留下的原因原文（P5 子票 #47）。
#:
#: **它是一份契约，不是一个提示语**：`outcome_of()` 靠逐字比对它，把「撞上
#: `uniq_pool_rebuild_ready_fingerprint`」从「真失败」里摘出来。写成两处字面量，改一处
#: 就会静默地把并发触发显示成红色失败——而那正是 ADR 0006 明令禁止的读法（「这是那条
#: 约束在起作用，不是故障」）。所以写入方（`rebuild_pool` 的 `IntegrityError` 分支）与
#: 读取方（`outcome_of`）都只认这一个名字。
DUPLICATE_FAILURE_REASON = "同一输入指纹已有一代就绪：本次为重复触发，未替换当前代"

#: `building` 停留多久算「卡住」：跨过它就说明 worker 大概率没在跑。
#:
#: 这个缺口是 `beat_health.py` 模块 docstring 记下的那条：本任务是**人工投递**而非 beat
#: 调度，所以它不进 `check_task_health` 的扫面——投递成功而 worker 没跑时，没有任何东西
#: 会响。页面靠这个阈值把那句话说出来（「超过 N 分钟仍在重算中，检查 worker」）。
#:
#: **不进 `config.py`**：那一组是机制阈值（frozen dataclass，进 `GROUPS` 就进了判定记录
#: 内嵌的参数快照）。这是一个运维超时，改了不影响任何一格池化结论；混进配置面会让
#: 「哪些数决定机制行为」这条边界变糊。
#:
#: 30 分钟的出处：`rebuild_pool` 要 `iterator(chunk_size=READ_CHUNK_SIZE)` 扫**全表**
#: `BacktestResult` 并逐条算 sha256，规模随回测数线性增长；ADR 0006 记的实测参照是同一批
#: 数据的陈旧扫描在 `.defer()` 修复前 **283 秒**，而池化本身不含回测执行。留约 6 倍余量，
#: 同时短于「人回头看一眼这个页面」的粒度——再长，一个卡死的 `building` 行会有半天看起来
#: 像在正常工作。
STALE_BUILDING_AFTER = timedelta(minutes=30)

#: 一次重算的收场。**五种，由 `outcome_of()` 一处判定**，不散在视图/序列化/页面里各判
#: 一次：它们要求人做的事完全不同（看新表 / 什么都不用做 / 不用管 / 去修 / 再等等），
#: 判错一次就会把「没写进审计的复用」显示成「已完成」。
OUTCOME_NEW_GENERATION = "new_generation"
OUTCOME_REUSED = "reused"
OUTCOME_DUPLICATE = "duplicate"
OUTCOME_FAILED = "failed"
OUTCOME_BUILDING = "building"

OUTCOMES = (
    OUTCOME_NEW_GENERATION,
    OUTCOME_REUSED,
    OUTCOME_DUPLICATE,
    OUTCOME_FAILED,
    OUTCOME_BUILDING,
)

#: 五种收场各自的展示名。**由后端给**（map #40 的渲染分界：枚举 display 不重拼）。
#: `reused` 那句必须写明「本次没有产生新记录」——复用分支不建代、不碰格子、不留任何审计
#: 行，把它显示成「已完成」就是让一个没写进审计的动作看起来像写过。复用那一代的编号由
#: `outcome_display()` 补在末尾（编号是数据，不是词表的一部分）。
#: `duplicate` 那句不说「失败」：那是唯一约束在起作用。
OUTCOME_DISPLAY = {
    OUTCOME_NEW_GENERATION: "新代已就绪",
    OUTCOME_REUSED: "输入未变，本次没有产生新记录",
    OUTCOME_DUPLICATE: "并发触发：已有另一代先一步就绪，本次未替换当前代（不是故障）",
    OUTCOME_FAILED: "重算失败",
    OUTCOME_BUILDING: "重算中",
}


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
# 一次重算的收场
# --------------------------------------------------------------------------- #


def outcome_of(
    *,
    reused: bool = False,
    duplicate: bool = False,
    status: str | None = None,
    failure: str = "",
) -> str:
    """把 `rebuild_pool` 的返回值（或一行记录）折成**五种收场之一**。

    这是全仓唯一一处「这次重算到底怎么了」的判定。散在视图/序列化/页面里各判一次，
    就会出现「同一个 `failed` 行在曲线上是红色、在文案里是并发触发」这种自相矛盾。

    顺序**有含义**，不是随便排的：

    1. `reused` 先判。复用那一轮**连一条 `building` 行都没有**（指纹命中在 `rebuild_pool`
       建行之前返回），所以此时 `status` 可能带着**上一代**的 `ready`——先看 `status`
       会把一次复用误报成「新代已就绪」，而那正是 ADR 0006 要求分开显示的那一对。
    2. `duplicate` 次判。它在库里记成 `status=failed`，靠**原因原文**区分
       （`DUPLICATE_FAILURE_REASON`）——那是约束在起作用，不是故障。
    3. 剩下的按状态走：`ready` 是新代、`building` 是还在跑、其余是失败。
    4. `status` 为 `None`：`rebuild_pool` 没给定状态，只可能是它真的没建行也没复用——
       那不该被猜成任何一种收场，按失败收（**fail-closed**：宁可显示「去查」也不显示
       「好了」）。
    """
    if reused:
        return OUTCOME_REUSED
    if duplicate or failure == DUPLICATE_FAILURE_REASON:
        return OUTCOME_DUPLICATE
    if status == RebuildStatus.READY.value:
        return OUTCOME_NEW_GENERATION
    if status == RebuildStatus.BUILDING.value:
        return OUTCOME_BUILDING
    return OUTCOME_FAILED


def outcome_display(outcome: str, *, generation: int | None = None) -> str:
    """收场的展示名。复用那一档补上「第 N 代」——编号是数据，词表在 `OUTCOME_DISPLAY`。"""
    display = OUTCOME_DISPLAY.get(outcome, outcome)
    if outcome == OUTCOME_REUSED and generation is not None:
        return f"输入未变，复用第 {generation} 代：本次没有产生新记录"
    return display


def _started_at(generation: RegimePoolRebuild) -> datetime:
    """这一代的开始时刻，**收掉「模型列在运行时是什么」**。

    本仓没装 `django-stubs`，pyright 看到 `generation.started_at` 是 `DateTimeField`
    描述符而不是 `datetime`，所以直接相减会报「`-` 不支持」——那是全仓的既有现象
    （`serializers.iso` 的 docstring 记着同一条），不是本模块引入的新形状。

    与 `iso()` 不同的是这里**不兜底**：读不出时刻就不能算「跑了多久」，猜一个（比如 0）会
    把一个卡死的行说成「刚跑」。判据由测试钉住。
    """
    moment = generation.started_at
    if isinstance(moment, datetime):
        return moment
    raise TypeError(
        f"started_at 不是 datetime（{type(moment).__name__}）："
        "这一行的开始时刻读不出来，「重算了多久」也就无从算起"
    )


def building_minutes(generation: RegimePoolRebuild, *, now=None) -> int:
    """这一代 `building` 了多久（整分钟，向下取整，不早于 0）。

    读 `started_at` 而不是 `created_at`：前者是重算真正开始的时刻（`rebuild_pool` 显式
    传入的同一个 `now`），后者是 auto_now_add 的落库时刻，两者在正常路径上几乎相同，
    但只有前者在测试里可注入、也只有前者是「这一代从什么时候开始算」的答案。
    """
    moment = now or timezone.now()
    elapsed = moment - _started_at(generation)
    return max(0, int(elapsed.total_seconds() // 60))


def is_stale_building(
    generation: RegimePoolRebuild, *, now=None, threshold: timedelta | None = None
) -> bool:
    """这一代是不是「卡住的重算中」——跨过 `STALE_BUILDING_AFTER` 仍在 `building`。

    **只在 `building` 上为真**：`ready` / `failed` 停多久都不是「卡住」，它们已经收场了。
    这条判据只说得出「worker 可能没在跑」，说不出「任务失败了」——两者的处置不同
    （检查 worker vs 查 `failure`），所以它们是两个字段，不是一个。
    """
    if generation.status != RebuildStatus.BUILDING.value:
        return False
    limit = STALE_BUILDING_AFTER if threshold is None else threshold
    return (now or timezone.now()) - _started_at(generation) > limit


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


def _archetype_block(
    matches: Mapping[int, pool.ArchetypeMatch] | None,
) -> dict[str, Any]:
    """原型归属那一块指纹。

    **收全部策略，不做「只有会走兜底的那批」的收窄。** 收窄看起来是免费的优化
    （实测 61 条里只有 6 条不需要兜底），但它会把本条修复要堵的那个 bug 原样放回来：

    `pool._pool_cell` 的兜底是拿**同原型的全部策略**一起池（`siblings`）。所以一条
    自己四格样本都够格的策略，它的原型仍然决定**别人**兜底时读到哪些样本——改了它
    的原型，另一条样本不足的策略那一格就变了。按「它自己不需要兜底」把它排除在指纹
    之外，指纹不会动，旧代被复用，那一格的错就没有任何地方会报出来。

    省下的那点重算（一条自给自足的策略改描述才多翻一代）远不值这个风险。

    `source` 与 `matched_word` 也进指纹，不只进 `archetype`：人工覆盖（`override`）
    与关键词命中（`keyword`）在 `archetype` 上可能恰好同名，但它们是两件不同的事
    ——前者是「人做的决定、描述下次改动也不该翻」的，后者是「机器这次猜中的」。
    只比名字会把「把一条策略从机器猜中改成人工指定、值恰好一样」读成什么都没变。
    """
    if not matches:
        return {}
    return {
        str(strategy_id): {
            "archetype": match.archetype,
            "source": match.source,
            "matched_word": match.matched_word,
        }
        for strategy_id, match in sorted(matches.items(), key=lambda kv: str(kv[0]))
    }


def input_fingerprint(
    samples: Sequence[pool.SliceSample],
    exclusions: Mapping[str, int],
    params: config.EvidenceConfig | None = None,
    *,
    archetypes: Mapping[int, pool.ArchetypeMatch] | None = None,
) -> str:
    """本次重算「看进去了什么」的指纹。

    盖住五样，每一样都能改变结论或改变记录里该看到的数：

    - 池化算法版本（`pool.POOL_VERSION`）——合并口径变了，同样的样本会有不同结论；
    - **整个**证据分组（`config.snapshot`，不是只取两个门槛）——将来给 `EVIDENCE`
      加字段时它自动进指纹，不必记得回来补一行；
    - 每一份样本的逐日曲线、逐笔样本、该阶段天数；
    - 排除计数（它进重算记录，属于产出的一部分）；
    - **原型归属**（`archetypes`）——`build_pool` 用 `matches` 决定兜底分组。

    最后一样是补上的：指纹原先不看它，于是「改了关键词表 / 写了覆盖表 / 重建时注册表
    恰好是空的」这三种情况都会算出**同一个指纹**，`rebuild_pool` 直接命中复用分支返回
    旧代——纠正无法生效，表现是「改完没反应，日志说指纹未变」。这不是可选的一节：
    `pool.resolve_archetype` 的 docstring 承诺覆盖表「可以纠正」，而纠正要能改变结论，
    就必须先能改变指纹。

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
        "archetypes": _archetype_block(archetypes),
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

    # 原型归属必须在算指纹**之前**定下来：它决定兜底分组，因而能改变格子结论。
    # 顺序反了就是本条修复之前的形状——指纹先算、原型后取，于是改映射算出的指纹
    # 与旧代相同，`existing` 命中复用分支，纠正一个字都落不下去。
    matches = archetype_matches([s.strategy_id for s in samples])

    fingerprint = input_fingerprint(samples, exclusions, params, archetypes=matches)

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
        # 原因原文走 `DUPLICATE_FAILURE_REASON`——`outcome_of()` 逐字比对它来区分
        # 「约束在起作用」与「真失败」，两处各写一份字面量会让这个区分静默失效。
        _mark_failed(rebuild, DUPLICATE_FAILURE_REASON, now)
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
