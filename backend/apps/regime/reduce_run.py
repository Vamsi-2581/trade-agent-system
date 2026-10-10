"""减仓执行器：事件熔断窗口打开时，把「账户持仓」减掉一半（第②段单元 ②d）。

这个模块是**策略层**：它回答「该减哪个账户、该减多少、是第几次、要不要告警、通知谁」。
「怎么下一张只减不增的市价子单」是 ``apps/trading/reduce.py``（机械层）的事，两者之间的
分界线写在那个模块的 docstring 里，这里不重复。

## 一次运行的账本顺序

按账户串行，账户内按品种串行（CONTEXT.md:122「同一一次性任务内串行、不并发」）：

1. **取账户**：``is_active`` 且密钥齐备（两个密钥都能解密、长度过短的一律不算配置好）。
   这一层是必要的，因为 ``build_account_adapter`` 只判「有没有密钥」，不判「像不像密钥」。
2. **建适配器**：``build_account_adapter``（它自己会 ``connect()``）。取不到就**记一笔日志
   然后跳过这个账户**，不写任何减仓记录——「适配器建不起来」不是一次减仓动作。
3. **读持仓**：``reduce.account_positions``。**它抛异常时整段作废**：不撤单、不减仓，
   只发一条告警（理由见下面「持仓取不到为什么不写记录」）。
4. **收窄作用域**：拿真实持仓去问「哪些（事件 × 作用域）真的压在我头上、我有多少」，
   得到本账户这一轮的品种清单。**这一步只能在 async 侧做**，因为本地没有持仓表——在进
   async 之前猜一份品种清单，就是第二份持仓算术。
5. **撤单**：``reduce.cancel_opening_orders``，作用域就是上一步那批品种。撤单失败
   **不阻断减仓**（CONTEXT.md:122），枚举失败的那句「本次未能枚举挂单，敞口在窗口内仍可能
   增加」由 ``reduce.CancelReport.lines()`` 负责写出来，这里只把它放进消息。
6. **逐品种**：闸门 → 认领 → 中间价 → 分片 → 收尾。

撤单排在认领**之前**、认领排在**下单之前**，两者不矛盾：撤单让敞口变小，而认领行必须在下单
之前落库（``RegimeReduceRecord`` docstring 的「为什么认领行在下单之前写」）。所以顺序是
「撤单 → 认领 → 下单」，而这几步里没有任何一步会**增大**敞口。

## 持仓取不到为什么不写记录

持仓取不到时**一行都不落**，只发一条告警（第 3 步）。写一行 ``FAILED`` 看起来更「有据可查」，
但它会**永久占住幂等键**：``uniq_regime_reduce_record`` 是（账户 × 事件 × 品种）上的**无条件**
唯一键，而 ``claim_record`` 用它做 ``get_or_create``、``created=False`` 就整段返回（不重减）。
于是下一轮持仓读到了、本该真的减仓，认领却撞在那一行上被当成「早就处理过」——**这次熔断永远
不会减仓**，而表里躺着一行看起来很正常的历史。``baseline_qty`` 也会被这次什么也没发生的
认领冻结成当天的基准。

另一半理由是：持仓取不到时我连「该减多少」都答不出来——数量算术的唯一来源就是
``account_positions``（本地没有持仓表）。这时记 ``FAILED``（「确定未成交」）是把
「不知道账户里有什么」记成「知道、且确定没减」。两种事实在文字上必须分开，正如
``ShardResult`` 对 ``unfinished`` 的要求。

所以 fail-closed 的方向是**不动手 + 告警**：告警是唯一能让人知道「这次熔断没有减仓」的信号，
而库里那一对（事件 × 品种）保持「从未被认领」，下一轮照常重试。这也解释了为什么这一步的告警
不能省：不写记录又不发消息，失败模式与「现在没有事件」一模一样。

## 一个品种一轮只认领一次

同时压在同一个品种上的两件高影响事件，在**同一轮**里只会认领**第一件**（按 ``halt_at``、
``id`` 排）。理由是数量算术：两行各自把 ``qty_before`` 读成同一份持仓、各自计划减一半，
合起来就是一次**清仓**——而 CONTEXT.md:124 明写减仓**不设「补回」**，多减不可逆。

分开认领之后，叠仓仍然成立且是**序列**的：第二轮（300 秒后）第一件已经收场，第二件读到的
``qty_before`` 是减半之后的量，于是 ``100 → 50 → 25``——正好落在 CONTEXT.md:151 允许的
「叠到基准的 25%」上，而且每一步都能在记录里看出来。

## 第 N 次熔断与「基准」

``ordinal`` 数的是**事件**不是动作（字段 docstring 明写「本日第 N 次熔断（事件计数）」），
所以同一事件下的多个品种共享同一个 N：取当天该账户已经出现过的**不同事件个数** + 1。

``baseline_qty`` 由**这一天该品种的第一行冻结**（`RegimeReduceRecord.baseline_for`），
读取与写入共用那一个口。于是「累计已减至基准的 X%」= 这一次的 ``qty_after / baseline_qty``，
不会因为中间又减了几轮而把分母一起挪走。

## 分片：三种「问不到」分三种处理

``SymbolRules`` 刻意只有 ``step_size`` / ``min_qty``，最小名义额另走 ``min_notional()``
（``Decimal("0")`` = 这个适配器不声明该约束）。所以有三个不同的「问不到」，处理**必须不同**：

- **精度规则取不到**（``symbol_rules()`` 返回 ``None`` 或抛错）：**一张都不发**，记
  ``FAILED``（确定未成交）+ 告警。不猜一个 ``step_size`` 顶替（``SymbolRules`` docstring
  的原话），也不发一张没取整的单——后者多半会被交易所拒，而 ``place_order`` 对「规则没加载」
  是抛 ``RuntimeError``，那在 ``reduce._retryable`` 眼里是**可重试**的，三次之后会记成
  ``unfinished``（「可能仍有活单」）。为一次本地的、确定没发生的拒单去惊动一个人查活单，
  比不做更糟。
- **最小名义额问不到**（``min_notional()`` 抛错）：精度是知道的，但没法判「每片够不够
  最小额」，所以**整笔一次**。
- **最小名义额声明为 0**：表示这个适配器不声明该约束，于是不降片也不做整笔判断。

降片之后仍然低于最小额时**照样发整笔一张**（CONTEXT.md:127「降片后仍不足最小额则整笔一次
市价减仓」），并在 ``note`` 里写明——这是机制明写要发的，不是漏判。

## 子单失败：不再补一层重试

``reduce.place_reduce_shard`` 内部已经用**同一个** ``client_order_id`` 发满三次
（``SHARD_MAX_SENDS``，CONTEXT.md:125）。所以这一层不再补「同一片再试一次」：那第三次发送
与内部那两次用的是同一个单号，交易所那边是同一个幂等单元，多打一次只是把一次失败记成两次。

一片的结论是 ``REJECTED``（确定没成交）就到此为止，剩余的量**不换新单接着减**（CONTEXT.md:125
明写不许降级成这个）；是 ``UNFINISHED``（可能有一张活单）就**立刻收手**，连 ``qty_after``
都不再读——那时交易所侧还有一张不知道会不会成交的单，读到的任何持仓都只是「此刻」。

## 滑点与自动暂停

参考价取**撤单之后、第一片之前**的中间价（CONTEXT.md:129），所以它不包含撤单造成的移动。
逐品种按成交量加权，只有**成交了的子单**参与；取不到参考价按「本次不判定滑点」留显式记录
（``SlippageBasis.UNAVAILABLE``），**不按 0**——0 的意思是「滑点完美」，那是一条假的好消息。

超限（严格大于 ``DERISK.slippage_limit_pct``，**分数口径**）就告警并写一条
``RegimeReducePause``（账户 × 品种）。暂停的是**下新单**，不是撤单：撤单让敞口变小，暂停它
没有任何好处（``RegimeReducePause`` docstring）。解除只走人工，这里没有自动恢复。

CONTEXT.md:129 后半（「写入机制自熔断条件」）在 v1 是休眠的，本模块不实现它。

## 一个刻意的闸门：本日已经收场的账户不再自动减仓（Q8）

一个账户在**同一业务日**里只要已经有一条**收场的**减仓记录，后续品种、后续账户在本轮都
不再动手，各自留一句「自动减仓已暂停…」的落点，并且**每个账户一条告警**。它不引入新的持久
开关（那是 ``RegimeReducePause`` 的活），方向与 CONTEXT.md:129 的「暂停自动减仓、转人工」
一致：机制做出一次动作之后停下来等人看一眼，而不是自己连着做。

**一处收窄，必须写明**：``NO_OP``（「无需减仓（无可减）」）**不算**收场。它不是机制对市场
做出的判断，而是「这个品种减半之后不足一个步进」这个算术事实；把它当成「转人工」的信号，
会让一个刚好只有某个品种空仓的账户因为一条本来什么也没发生的记录而停摆。

**已知代价（留给 ②e/③ 再看）**：生产环境里第一次减仓收场之后，本轮的其余品种与其余账户
都要等到下一轮（300 秒后）才会各自继续，所以一次覆盖多账户的熔断是**逐个账户**慢慢推完的。
这是「人看一眼再走下一步」的直接代价，不是 bug；若 ②e 的窗口通知需要「一轮里全部推完」，
改这里的闸门而不是改 ``sync_halt_windows`` 的调度。

## 通知走账户口径，不走窗口口径

CONTEXT.md:116 第二条：减仓结果通知的受众**独立于**窗口通知的裁剪规则——只要减了你的仓，
你必然收到那条通知，哪怕你当时没有任何活跃会话。所以收件人从 ``ExchangeAccount.user`` 解析，
不是从窗口订阅推导。账户**未归属**到具体用户时发给全体 ``is_active`` 用户，并在消息首行写明
「该账户未归属到具体用户」——空收件人会让 ``alerts.notify_user`` 打一条 ERROR 然后返回
``False``，表现是一条告警静悄悄地没有收件人。

## 这一层不做什么

不设「补回」（CONTEXT.md:124）、不写本地 ``Order`` 行（账本是 ``RegimeReduceRecord.sub_orders``，
``client_order_id`` 就是日后回填的查询键）、不动 ``CircuitBreaker``、不实现
CONTEXT.md:129 后半的自熔断、不替 ②e 发窗口通知（②d 只发「减仓与撤单的结果」这一条，且
**不裁剪**——它说的事与「你有没有会话」无关）。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from typing import Sequence

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.common.time_utils import to_business
from apps.core.db_utils import db_async
from apps.exchange.models import ExchangeAccount
from apps.regime import config, halt, halt_sync
from apps.regime.models import (
    ActorKind,
    HaltTrigger,
    MechanismKind,
    ReduceStatus,
    RegimeReducePause,
    RegimeReduceRecord,
    SlippageBasis,
)
from apps.trading import reduce

logger = logging.getLogger(__name__)

# 本任务的署名。写入方的身份与 `halt_sync` 同一条纪律：排查时「谁减的」答不出来，
# 记录表就只剩一串看不出真假的数字。
ACTOR_KIND = ActorKind.TASK
ACTOR_NAME = "regime.reduce_run"

#: 密钥短于这个长度的一律当成**没配好**。判据是「明显不是一个密钥」，不是强度检查——
#: 真正的强度由交易所决定，这里只挡「字段里塞了一个占位符或空串」。
MIN_KEY_LENGTH = 20

#: ``clientOrderId`` 的字符上限（币安 ``newClientOrderId`` 的约束）。超了交易所直接拒单，
#: 而拒单在那条链路上表现为「减仓失败」，与「窗口里减不掉」在告警里长得一样。
CLIENT_ID_MAX = 36

#: `qty_after` 的十进制位数与 `RegimeReduceRecord` 的 DecimalField 一致（20, 8）。
_QTY_PLACES = Decimal("0.00000001")

#: ``finish`` 会写的字段。**与 ``finish`` 同处一地**：清单分成两份的话，加了字段只改一处，
#: 表现是「记录落了库、那个字段一直是空的」——而这正是事后查账要看的那几个字段。
_FINISH_FIELDS = (
    "status",
    "planned_qty",
    "qty_after",
    "sub_orders",
    "cancel_report",
    "slippage_pct",
    "slippage_basis",
    "reason",
    "finished_at",
)


# --------------------------------------------------------------------------- #
# 触发源：此刻到底该不该动手（只读，纯函数为主）
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class FiringScope:
    """此刻正在拦、且属于事件熔断的那一对（事件 × 作用域）。

    **刻意只有事件与作用域，没有品种。** 品种要拿**真实持仓**去筛，而本地没有持仓表——
    在进 async 之前规划一份品种清单，就是第二份持仓算术。所以「拦在哪」在规划期算得出，
    「减哪个品种」必须在 async 侧拿 ``reduce.account_positions`` 回来才对得上。
    """

    event_id: int
    event_name: str
    scope: str


@dataclass(frozen=True)
class Target:
    """一个事件压在**这个账户**上的**一个**品种（已归一到交易所口径）。

    **一条 Target 只装一个品种**，不是一个品种列表：品种清单是拿真实持仓筛出来的，
    而认领、分片、收尾全都是**按品种**做的。装成列表的话，``_run_symbol`` 就得再拆一次，
    而拆的时候一旦漏了「这个 Target 其实覆盖两个品种」，第二个品种会静默地不减。
    """

    event_id: int
    event_name: str
    scope: str
    symbols: tuple[str, ...]

    @property
    def symbol(self) -> str:
        if len(self.symbols) != 1:
            raise RuntimeError(
                f"Target 只装一个品种，收到了 {len(self.symbols)} 个：{self.symbols}"
            )
        return self.symbols[0]


def scopes_in_force(*, now: datetime | None = None) -> list[str]:
    """此刻「事件熔断」这个源真的在拦的作用域。

    取的是 ``halt.live_declarations``（生效期内 ∧ 未解除）再过一道
    ``halt.switch_open(HaltTrigger.EVENT)``。**两件都问，但都不自己重写**：开关与声明是
    两件事（``halt`` 的模块 docstring），而「哪个开关管哪条线」的唯一换算口是
    ``switch_open``——这里自己去查一遍 ``HALT_TRIGGER_SWITCH`` 就是给那张表造第二个答案，
    而它有一处刻意的不对称（保命档没有开关）。

    **顺序按 ``live_declarations`` 给的来**（``opened_at`` / ``id``）：确定性让「同一轮里
    先处理哪个作用域」有唯一答案，排查时不必猜。
    """
    at = now or timezone.now()
    cache: dict[MechanismKind, bool] = {}
    return [
        row.scope
        for row in halt.live_declarations(now=at)
        if halt.trigger_of(row) is HaltTrigger.EVENT
        and halt.switch_open(HaltTrigger.EVENT, cache)
    ]


def firing_pairs(
    scopes: Sequence[str], candidates: Sequence, at: datetime
) -> list[FiringScope]:
    """此刻**正压在**某个作用域上的（事件 × 作用域）。

    两个条件缺一不可：

    * 窗口**正在开着**（``halt_at <= at < resume_at``，与 ``HaltDeclaration`` 那条半开区间
      同一个取等号方向）。**不能只看「表里有行」**：``halt_sync`` 会**提前**把下一段写进
      声明表（那个模块 docstring 的「为什么『下一段』要提前写进表里」），所以「有行」与
      「此刻在拦」是两件事。拿有行当判据的表现是「还没到窗口就把仓减了」。
    * ``halt_sync.touches(event, scope)`` 为真。这个函数是公开的就是为了这里——写侧与读侧
      必须问同一个问题，各写一份的话分歧会落在最不该出错的地方（声明写着「熔断中」，减仓
      那一侧认不出是哪个事件，于是一动不动）。

    ``candidates`` 就是 ``halt_sync.high_impact_events()`` 那一批（**不按 status 过滤**），
    但 ``triggers_halt`` 是「已排期 ∧ 档位为高」的合取，取消掉的事件到这里自然就掉了。
    本函数刻意不自己去查库：调用方传进来，这半段就能在 ``SimpleTestCase`` 上测。
    """
    out: list[FiringScope] = []
    for event in candidates:
        if not event.triggers_halt:
            continue
        if not (event.halt_at <= at < event.resume_at):
            continue
        for scope in scopes:
            if halt_sync.touches(event, scope):
                out.append(
                    FiringScope(event_id=event.pk, event_name=event.name, scope=scope)
                )
    return out


def targets_for(
    firing: Sequence[FiringScope], positions: dict[str, tuple[str, Decimal]]
) -> list[Target]:
    """（事件 × 作用域）→ **这个账户**真正持有、且被覆盖到的那批（事件 × 品种）。

    ``positions`` 的键是交易所口径（``reduce.account_positions`` 归一过），而作用域里的
    品种是事件口径（``BASE/QUOTE``）。换算走 ``reduce.normalize_symbol``——**两个口径之间
    只有一个换算口**，这里不自己写一遍 ``replace("/", "")``。

    全市场（``global``）作用域覆盖所有持仓；品种作用域只覆盖它自己那个品种。认不出写法时
    ``halt.parse_scope`` 归到 ``global``（fail-closed），所以**读坏的作用域会真的去减**，
    而不是静默一动不动。

    **``positions`` 必须是真实持仓**。传一份猜出来的品种清单进来，全市场作用域就会只减到
    那份清单上的品种——而它看起来完全正常（确实减了几个品种）。
    全市场作用域刻意**不看**声明行自己的品种（那种行没有品种），它覆盖的就是此刻手上所有
    持仓：这正是「事件熔断停全场」的语义。

    每个品种只取**第一件**事件（``firing`` 已按 ``high_impact_events`` 的 ``halt_at`` / ``id``
    排好），理由见模块 docstring 的「一个品种一轮只认领一次」。
    """
    claimed: dict[str, FiringScope] = {}
    for item in firing:
        kind, value = halt.parse_scope(item.scope)
        if kind == "strategy":
            # 策略档声明拦的是「某个策略的下单」，不是「某个账户的持仓」——减仓的对象是账户
            # （CONTEXT.md:117），两者对不上。第③段的 gate 才会用到策略档。
            continue
        wanted = normalize_scope_symbol(kind, value)
        for symbol in positions:
            if wanted is not None and symbol != wanted:
                continue
            claimed.setdefault(symbol, item)
    # 一个品种一条 Target：``firing`` 里同一事件在同一作用域上只会出现一次，而
    # ``claimed`` 已经保证一个品种只落在一个 FiringScope 里。
    return [
        Target(
            event_id=item.event_id,
            event_name=item.event_name,
            scope=item.scope,
            symbols=(symbol,),
        )
        for symbol, item in sorted(claimed.items())
    ]


def normalize_scope_symbol(kind: str, value: str | None) -> str | None:
    """品种作用域里的品种 → 交易所口径。``None`` = 全市场（不筛）。"""
    if kind != "symbol" or not value:
        return None
    return reduce.normalize_symbol(value)


# --------------------------------------------------------------------------- #
# 分片（纯函数：不吃 DB、不读时钟）
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ShardPlan:
    """一次（账户 × 事件 × 品种）该发哪几张子单。

    ``planned_qty`` 是**意图**（减一半，取整之后的那个数），``shards`` 是**实际请求**。
    两个分开：整数分片除不尽时会有不到一个步进的零头留在账上，那部分减不掉——留空的话
    「计划 50、实际发了 49.999」看不出是漏发还是减不掉。
    """

    planned_qty: Decimal
    shards: tuple[Decimal, ...] = ()
    note: str = ""

    @property
    def total(self) -> Decimal:
        return sum(self.shards, Decimal("0"))

    @property
    def ok(self) -> bool:
        return bool(self.shards)


def _floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    """向下取到 ``step`` 的整数倍（与 ``binance._normalize_quantity`` 同一口径）。

    ``ROUND_DOWN`` 而不是四舍五入：向上取整会让请求量**大于**该减的量，而减仓多减的部分
    没有「补回」（CONTEXT.md:124）。
    """
    if step is None or step <= 0:
        return value
    return ((value / step).to_integral_value(rounding=ROUND_DOWN) * step).quantize(step)


def plan_shards(
    qty_before: Decimal,
    *,
    step_size: Decimal,
    min_qty: Decimal | None,
    min_notional: Decimal | None,
    reference: Decimal | None,
    shard_count: int,
) -> ShardPlan:
    """把「持仓的一半」切成 ``shard_count`` **片以内**的子单量。

    三个数是有序地问的，每个都改变结论（CONTEXT.md:127）：

    1. 先减半再取整（``step_size``）。取整后为 0 就是**无可减**——不是失败，是真的没什么
       可减（``NO_OP``），所以返回空 ``shards`` 而不是抛错。
    2. 片数上限是 ``shard_count``，但同时受 ``min_qty`` 约束：每片至少一个 ``min_qty``，
       否则那些片会被交易所按最小起订量拒掉。
    3. ``min_notional`` 约束每片的**名义价值**（``片量 × 参考价``），不够就降片。参考价取不到
       时**不降片**：不知道名义价值却按某个假设降片，是把一个问不到的数当成已知——而它的表现
       是「少切了几片」，看不出来。
    """
    half = _floor_to_step(qty_before / 2, step_size)
    if half <= 0:
        return ShardPlan(
            planned_qty=Decimal("0"),
            note="减半后不足一个步进（step_size），本次无可减",
        )

    size = _shard_count_for(
        half,
        min_qty=min_qty,
        min_notional=min_notional,
        reference=reference,
        cap=shard_count,
    )
    if size <= 1:
        return ShardPlan(planned_qty=half, shards=(half,), note=_single_note(half, min_notional, reference))

    shards = _split(half, size, step_size)
    if not shards:
        # 切不出两片——步进比「一片的量」还大。整笔一次，不是失败。
        return ShardPlan(
            planned_qty=half,
            shards=(half,),
            note="按步进取整后切不出多片，整笔一次",
        )
    dust = half - sum(shards, Decimal("0"))
    note = f"共 {len(shards)} 片"
    if dust > 0:
        note += f"，不足一个步进的零头 {dust} 留在账上（减不掉）"
    return ShardPlan(planned_qty=half, shards=tuple(shards), note=note)


def _shard_count_for(
    half: Decimal,
    *,
    min_qty: Decimal | None,
    min_notional: Decimal | None,
    reference: Decimal | None,
    cap: int,
) -> int:
    """片数：先按 ``min_qty`` 定上限，再按 ``min_notional`` 往下调。"""
    if min_notional is None:
        # 最小名义额**问不到**。知道精度但不知道最小额，就没法判「每片够不够」，所以不切。
        return 1
    if min_qty is not None and min_qty > 0:
        room = int((half / min_qty).to_integral_value(rounding=ROUND_DOWN))
    else:
        room = cap
    size = max(1, min(cap, room))
    if min_notional > 0 and reference is not None and reference > 0:
        while size > 1 and (half / size) * reference < min_notional:
            size -= 1
    return size


def _single_note(
    half: Decimal, min_notional: Decimal | None, reference: Decimal | None
) -> str:
    """整笔一次时写清**为什么**是整笔。三种原因的下一步动作完全不同。"""
    if min_notional is None:
        return "最小名义额问不到，本次整笔一次（不与交易所最小额赌一把）"
    if min_notional <= 0:
        return "适配器不声明最小名义额约束，按单片上限切不出多片，整笔一次"
    notional = half * reference if reference is not None and reference > 0 else None
    if notional is not None and notional < min_notional:
        return (
            f"整笔名义额 {notional} 仍低于最小额 {min_notional}，"
            "按 CONTEXT.md:127 仍发这一张（降片已到底，减不掉才是更坏的结局）"
        )
    return "整笔一次"


def _split(half: Decimal, size: int, step_size: Decimal) -> list[Decimal]:
    """把 ``half`` 等分成 ``size`` 片，每片都是 ``step_size`` 的整数倍。

    余数按**一个步进**逐片补，从第一片开始轮。不留余数在中间片里：任何一片小于
    ``min_qty`` 都会被交易所拒，而「补到前面的片」在总量上等价、在失败面上严格更好。
    补不进去的零头（< 一个步进）由调用方写进 ``note``。
    """
    base = _floor_to_step(half / size, step_size)
    if base <= 0:
        return []
    shards = [base] * size
    remainder = half - base * size
    index = 0
    while remainder >= step_size:
        shards[index] += step_size
        remainder -= step_size
        index = (index + 1) % size
    return shards


def shard_client_id(account, event_id: int, symbol: str, index: int) -> str:
    """子单的 ``clientOrderId``：**确定性 + 逐片唯一 + 不超 36 字符**。

    确定性是这条链路的一半价值（CONTEXT.md:125 的重发要靠同一个单号走幂等），所以它由
    （账户 × 事件 × 品种 × 第几片）**算出来**，不用随机数、也不查库。``reduce.place_reduce_shard``
    在三次重发里复用这同一个值；日后回填账本时它是 ``find_order_by_client_id`` 的查询键。

    品种超长时**截断**而不是哈希：截断后仍然可读（排查时一眼看出是哪个品种），而碰撞面在
    （账户 × 事件 × 品种 × 片号）这个元组上早就被记录表分开了——两个品种截断后前缀相同、
    又在同一个事件同一个账户上同时减仓，是理论问题不是实际问题。
    """
    head = f"rd{account.id.hex[:8]}-{event_id}-"
    tail = f"-{index}"
    room = CLIENT_ID_MAX - len(head) - len(tail)
    if room < 1:
        # 事件主键长到把预算吃光——只可能是主键类型换了。不静默截断 head：那会让两个事件
        # 的单号撞在一起，而撞单号在交易所侧是**同一张单**。
        raise ValueError(f"clientOrderId 预算不足：head={head!r} tail={tail!r}")
    return f"{head}{symbol[:room]}{tail}"


# --------------------------------------------------------------------------- #
# 记录、暂停、闸门（同步 DB：能在 TestCase 上直接测）
#
# 这一段的函数全是**同步**的：查询与渲染放在同一侧，测试直接调同步函数（async 那一半
# 在另一个线程里跑，`TestCase` 开着的事务它看不见——`db_async` 的 docstring 写了这条）。
# 代价是 **async 侧调用它们必须过 `db_async`**：裸调用在 Django 里是
# `SynchronousOnlyOperation`，而那个异常的表现是「减仓跑到一半整段塌掉」。
# --------------------------------------------------------------------------- #


def configured_accounts() -> list[ExchangeAccount]:
    """可以动手的账户：``is_active`` 且**像配好了密钥**。

    密钥检查放在这里而不是 ``build_account_adapter`` 里：那个函数只判「字段有没有值」，而
    「字段里塞了个占位符」与「真的配好了」在它眼里一样。这一层只挡明显的占位符，不做强度
    检查（真正决定强度的是交易所）。
    """
    out: list[ExchangeAccount] = []
    for account in ExchangeAccount.objects.filter(is_active=True).order_by("id"):
        try:
            key = account.decrypt_api_key()
            secret = account.decrypt_api_secret()
        except Exception as exc:  # noqa: BLE001 — 解密失败也是一种「没配好」，不是事故
            logger.warning(
                "[Reduce] 账户 %s（%s）密钥无法解密：%s", account.pk, account.label, exc
            )
            continue
        if (
            not key
            or not secret
            or len(key) <= MIN_KEY_LENGTH
            or len(secret) <= MIN_KEY_LENGTH
        ):
            logger.warning(
                "[Reduce] 账户 %s（%s）密钥未配置或明显不是密钥，跳过",
                account.pk,
                account.label,
            )
            continue
        out.append(account)
    return out


@dataclass(frozen=True)
class GateVerdict:
    """这个账户此刻允不允许**下新单**（不是「允不允许撤单」）。"""

    allowed: bool
    reason: str = ""


def business_day(at: datetime) -> date:
    """业务日 = 业务时区的自然日（CONTEXT.md 里「本日第 N 次」的那个「本日」）。

    与 ``regime/models.business_midnight`` 同一口径，只是那个函数要的是「某一天的 08:00」，
    这里要的是「此刻是哪个业务日」。**不引入 _date helper**：``time_utils`` 只承诺时区换算，
    日期从 ``to_business`` 的结果上取。
    """
    return to_business(at).date()


def enable_later(account, day: date) -> GateVerdict:
    """本日这个账户还能不能继续自动减仓（Q8 的闸门）。

    不可以 ⟺ 当天已经有一条**收场的**记录。``NO_OP`` 不算收场：它是「减半后不足一个步进」
    这个算术事实，不是机制对市场做出的判断（模块 docstring 的「一处收窄」）。

    **没有持久开关**：这个判据只看数据库里已经发生的事，所以进程重启、任务重跑都不会改变
    结论——而一个内存里的「暂停标志」重启之后就没了，那正是最需要它的时候。
    """
    row = (
        RegimeReduceRecord.objects.filter(exchange_account=account, business_day=day)
        .exclude(status=ReduceStatus.CLAIMED.value)
        .exclude(status=ReduceStatus.NO_OP.value)
        .order_by("claimed_at", "id")
        .first()
    )
    if row is None:
        return GateVerdict(allowed=True)
    return GateVerdict(
        allowed=False,
        reason=f"本日已有收场的自动减仓（{row}）——自动减仓已暂停，请人工确认后再放开",
    )


def stale_claims(account) -> list[RegimeReduceRecord]:
    """上一次认领之后**没有收尾**的记录：可能还有活单在交易所上。

    判据只有一处：``status == claimed`` ∧ ``finished_at IS NULL``。两个条件是同一件事的
    两面（``finish`` 同时写这两个字段），但**两个都写**：只看 ``status`` 会漏掉「收尾写了一半」
    的行，而那种行的存在恰恰说明写入中断了。
    """
    return list(
        RegimeReduceRecord.objects.filter(
            exchange_account=account,
            status=ReduceStatus.CLAIMED.value,
            finished_at__isnull=True,
        ).order_by("claimed_at", "id")
    )


def live_pause_symbols(account) -> set[str]:
    """这个账户此刻被暂停下新单的品种（``closed_at IS NULL``）。"""
    return set(
        RegimeReducePause.objects.filter(
            exchange_account=account, closed_at__isnull=True
        ).values_list("symbol", flat=True)
    )


def ordinal_for(account, day: date, event_id: int) -> int:
    """``event_id`` 这一次熔断算本日的第几次。**数事件不数动作**（字段 docstring），所以同一
    事件下的多个品种共享同一个 N。

    实现上「第 N 次」= 当天**别的**事件个数 + 1：``.exclude(event_id=event_id)`` 去掉的正是
    本事件自己。**不排除的话同一事件的第二个品种会拿到 N+1**——而这两行在库里读起来就是
    两次不同的熔断，日报上「本日第 2 次熔断」出现两遍，累计比例也跟着散开。

    另一个写法（「数一数当天已有几行 + 1」）数的是**动作**：同一事件下的第二个品种会因为
    第一个品种的行而拿到 N+1，同样错。
    """
    return (
        RegimeReduceRecord.objects.filter(exchange_account=account, business_day=day)
        .exclude(event_id=event_id)
        .values("event")
        .distinct()
        .count()
        + 1
    )


def claim_record(
    account,
    *,
    event_id: int,
    symbol: str,
    day: date,
    qty_before: Decimal,
    reason: str,
) -> tuple[RegimeReduceRecord, bool]:
    """认领一行（``CLAIMED``）。返回 ``(记录, 是不是这一轮创建的)``。

    ``get_or_create`` 打在 ``uniq_regime_reduce_record``（账户 × 事件 × 品种，**无条件**唯一）
    上，所以重复运行天然幂等——这正是 CONTEXT.md:116「幂等键必须含第 N 次」的落地：唯一键
    里的「事件」就是那个 N。第二个返回值是**这一刻的事实**，不是推测：``created=False``
    说明这一行早就存在，无论它是什么状态。

    ``baseline_qty`` 在**这一天的第一行**冻结（``baseline_for`` 就是那个读取口），读不到才
    用刚读到的 ``qty_before``。写在这里而不是调用方，是因为读取规则与写入规则必须同处一地。
    """
    baseline = RegimeReduceRecord.baseline_for(account, symbol, day)
    return RegimeReduceRecord.objects.get_or_create(
        exchange_account=account,
        event_id=event_id,
        symbol=symbol,
        defaults={
            "business_day": day,
            "ordinal": ordinal_for(account, day, event_id),
            "status": ReduceStatus.CLAIMED.value,
            "baseline_qty": baseline if baseline is not None else qty_before,
            "qty_before": qty_before,
            "reason": reason,
        },
    )


def finish(
    record: RegimeReduceRecord,
    *,
    status: ReduceStatus,
    planned_qty: Decimal | None = None,
    qty_after: Decimal | None = None,
    sub_orders: Sequence[dict] | None = None,
    cancel_report: dict | None = None,
    slippage_pct: Decimal | None = None,
    slippage_basis: SlippageBasis | None = None,
    extra_reason: str = "",
) -> None:
    """收尾：**``finished_at`` 只在这里写**（写一次，之后不再改）。

    ``finished_at`` 是「这条认领还归机制处置吗」的唯一标记（``stale_claims`` 的另一半），
    所以它必须与 ``status`` 一起写、且只写一次。分成两次写会让「收了一半」的行存在，而
    那种行在 ``stale_claims`` 里看起来与「跑到一半就没了」一模一样。
    """
    record.status = status.value
    if planned_qty is not None:
        record.planned_qty = planned_qty
    if qty_after is not None:
        record.qty_after = qty_after.quantize(_QTY_PLACES, rounding=ROUND_DOWN)
    if sub_orders is not None:
        record.sub_orders = list(sub_orders)
    if cancel_report is not None:
        record.cancel_report = cancel_report
    if slippage_basis is not None:
        record.slippage_pct = slippage_pct
        record.slippage_basis = slippage_basis.value
    if extra_reason:
        record.reason = f"{record.reason}\n{extra_reason}".strip()
    record.finished_at = timezone.now()
    record.save(update_fields=list(_FINISH_FIELDS))


def record_pause(
    account, *, symbol: str, reason: str, evidence: dict, at: datetime
) -> bool:
    """写一条自动减仓暂停。``True`` = 本次真的新开了暂停（``False`` = 早就有一条在生效）。

    撞 ``uniq_live_reduce_pause`` 就是「早就暂停了」，不是事故：那说明上一轮已经告警过，
    这一轮不该再惊动一次人。用 ``IntegrityError`` 判而不是先查再写——先查再写在两个任务
    挨着跑的时候会双写。
    """
    try:
        with transaction.atomic():
            RegimeReducePause.objects.create(
                exchange_account=account,
                symbol=symbol,
                reason=reason,
                evidence=evidence,
                opened_at=at,
                actor_kind=ACTOR_KIND.value,
                actor_name=ACTOR_NAME,
            )
        return True
    except IntegrityError:
        logger.info("[Reduce] 账户 %s 品种 %s 已有生效中的自动减仓暂停", account.pk, symbol)
        return False


# --------------------------------------------------------------------------- #
# 通知：账户口径的收件人（独立于窗口通知的裁剪规则）
# --------------------------------------------------------------------------- #

UNOWNED_NOTE = "（该账户未归属到具体用户，本条发给了全体启用用户）"


def recipients_for(account) -> tuple[list[str], str]:
    """这个账户的减仓结果该发给谁。返回 ``(user_id 列表, 首行说明)``。

    ``ExchangeAccount.user`` 为空 = **未归属**，不是「没有人要通知」——后者是一条告警
    静悄悄地没有收件人。未归属时发给全体 ``is_active`` 用户，并在消息里写明，让收件人知道
    这不是「你的账户出事了」而是「有一笔减仓你不知道归属」。

    收件人为空（一个 ``is_active`` 用户都没有）时不在这里兜底：``alerts.notify_user`` 对
    空收件人会打一条 ERROR 日志并返回 ``False``，那是「出站的唯一通知口」的职责（CONTEXT.md:66
    「没有接收人就不算告警」）。
    """
    if account.user_id is not None:
        return [str(account.user_id)], ""
    user_model = get_user_model()
    ids = [
        str(pk)
        for pk in user_model.objects.filter(is_active=True).values_list("pk", flat=True)
    ]
    return ids, UNOWNED_NOTE


async def _alert(user_ids: Sequence[str], text: str) -> int:
    """投一条消息，返回送达人数。

    与 ``report._alert_each`` 同一个写法、同一个出口（``alerts.notify_user``）。**不新增
    第二个投递机制**（``alerts`` 模块 docstring）。
    """
    from apps.trading.alerts import notify_user

    sent = 0
    for user_id in user_ids:
        if await notify_user(user_id, text):
            sent += 1
    return sent


# --------------------------------------------------------------------------- #
# 消息渲染（纯函数）
# --------------------------------------------------------------------------- #


def _pct(numerator: Decimal | None, denominator: Decimal | None) -> Decimal | None:
    """``numerator / denominator`` 的百分数（一位小数）。分母不可用时返回 ``None``。"""
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return (numerator / denominator * Decimal("100")).quantize(
        Decimal("0.1"), rounding=ROUND_HALF_UP
    )


@dataclass
class RecordOutcome:
    """一行记录的下场，供渲染用。**纯数据**：渲染在另一个线程/另一段代码里跑，不碰 ORM。"""

    symbol: str
    status: ReduceStatus
    ordinal: int
    baseline_qty: Decimal | None
    qty_after: Decimal | None
    note: str = ""
    slippage_basis: SlippageBasis | None = None
    slippage_pct: Decimal | None = None
    paused: bool = False

    def line(self) -> str:
        head = f"{self.symbol}：{self.status.display}"
        if self.note:
            head += f"（{self.note}）"
        return head


def render_account_report(
    *,
    label: str,
    day: date,
    outcomes: Sequence[RecordOutcome],
    cancel_lines: Sequence[str] = (),
) -> str:
    """一个账户这一轮的减仓结果。**不裁剪**（CONTEXT.md:116 第二条）：它说的是账户持仓，
    与收件人当时有没有活跃会话无关。

    第 151 条要求的「本日第 N 次熔断，累计已减至基准的 X%」逐行写出来：N 是事件计数（同一
    事件下的多个品种共享），X 用**这一次的** ``qty_after / baseline_qty``——基准在当天第一行
    冻结，所以 X 是相对这一天的起点，不是相对上一次减仓。
    """
    lines = [f"⚠️ 事件熔断自动减仓｜{label}（业务日 {day}）"]
    if outcomes:
        lines.append("")
        for outcome in outcomes:
            lines.append(f"· {outcome.line()}")
            pct = _pct(outcome.qty_after, outcome.baseline_qty)
            tail = f"本日第 {outcome.ordinal} 次熔断，累计已减至基准的 {pct}%"
            if pct is None:
                tail = f"本日第 {outcome.ordinal} 次熔断，基准或余量取不到，累计比例未判定"
            lines.append(f"    {tail}")
            if outcome.slippage_basis is SlippageBasis.MID and outcome.slippage_pct is not None:
                lines.append(
                    f"    滑点：{outcome.slippage_pct}（以动作发起时刻中间价为基准）"
                )
            elif outcome.slippage_basis is SlippageBasis.NO_FILL:
                lines.append("    滑点：本次无成交，无从计算")
            elif outcome.slippage_basis is SlippageBasis.UNAVAILABLE:
                lines.append("    滑点：基准价取不到，本次不判定滑点")
            if outcome.paused:
                lines.append(
                    "    **已暂停该品种的自动减仓，转人工**（滑点超过上限；解除只走人工）"
                )
    if cancel_lines:
        lines.append("")
        lines.extend(cancel_lines)
    return "\n".join(lines)


def render_reduce_failure(
    *, label: str, symbol: str, reason: str, ordinal: int, day: date
) -> str:
    """减仓没做成的那条告警。**必须写出「没做成」三个字**：一条沉默的失败与「本来就没到
    窗口」在收件人眼里一模一样。"""
    return (
        f"⚠️ 事件熔断自动减仓未完成｜{label}\n"
        f"品种 {symbol}：本日第 {ordinal} 次熔断，本次**未减仓**。\n"
        f"原因：{reason}\n"
        f"（业务日 {day}。减仓不设「补回」，已减掉的部分不会回滚。）"
    )


# --------------------------------------------------------------------------- #
# 执行（async：适配器与网络都在这一半）
# --------------------------------------------------------------------------- #


@dataclass
class AccountPlan:
    """一个账户这一轮要做的全部事，**在进 async 之前就定好**。

    ``account`` 是 ORM 实例，跨线程传递本身没问题，但这里**只读它的密钥与 id**，不碰任何
    需要回库的字段（``user`` 已经解析成 ``recipients``）——跨线程的懒加载是 async 上下文里
    最隐蔽的一类裸查询。

    ``firing`` 是（事件 × 作用域）那一对，**不是品种清单**：品种要拿真实持仓筛，那一步只能
    在 async 侧做（``targets_for``）。这里按作用域先立计划，是因为作用域覆盖到的品种与账户
    里有没有那个品种无关——账户列表是同步侧读得出来的。
    """

    account: ExchangeAccount
    label: str
    firing: tuple[FiringScope, ...]
    recipients: tuple[str, ...] = ()
    recipient_note: str = ""


@dataclass
class AccountOutcome:
    """一个账户跑完之后的结论。``stop_reason`` 非空 = 本轮后续账户不再处理（Q8）。

    ``cancel_lines`` 是**撤单那一段的渲染**（账户口径，一次撤单调用一行报告）。它必须挂在
    这里而不是逐品种的记录上：撤单是**按作用域一次做完**的，逐品种复制一份只会让同一句话
    在消息里出现 N 遍。它也不能「从记录里反推」——枚举失败那句话说的事情（「敞口在窗口内
    仍可能增加」，CONTEXT.md:122）只在内存里存在过，没有落进任何一行记录。
    """

    label: str
    rows: int = 0
    skipped: int = 0
    stop_reason: str = ""
    details: list[RecordOutcome] = field(default_factory=list)
    cancel_lines: list[str] = field(default_factory=list)


async def _open_adapter(account) -> tuple[object | None, str]:
    """建适配器（含 ``connect()``）。``(None, 原因)`` = 这个账户这一轮跳过。

    ``build_account_adapter`` 已经会 ``connect()``，这里**不再 connect 第二次**：适配器
    按 event loop 分别持有 HTTP 客户端（``BinanceAdapter._clients``，每个 loop 一个），
    所以「每个账户在自己的 ``asyncio.run`` 里建一次」是安全的用法，不需要额外管理 loop。

    适配器**取不到时记日志 + 跳过，不写记录**：那不是一次减仓动作，为它写一行就等于替机制
    断言了一次它没做过的事。
    """
    from apps.trading.pending_reconcile import build_account_adapter

    try:
        adapter = await build_account_adapter(account)
    except Exception as exc:  # noqa: BLE001 — 建不起来只跳过这一个账户，不掀掉整轮
        return None, f"适配器无法建立连接（{type(exc).__name__}: {exc}）"
    if adapter is None:
        return None, "密钥齐了但没有这家交易所的适配器"
    return adapter, ""


async def _load_rules(
    adapter, symbol: str
) -> tuple[Decimal | None, Decimal | None, Decimal | None, str]:
    """``(step_size, min_qty, min_notional, note)``。

    ``step_size`` 为 ``None`` = **精度规则问不到**，调用方据此放弃下这一单（模块 docstring
    的「三种问不到」）。``min_notional`` 为 ``None`` = 最小名义额问不到，整笔一次。

    两个口分两次问是有意的：``SymbolRules`` 刻意不含 ``min_notional``（那里 docstring 写了
    理由），所以「同一个交易所约束的两半」本来就该问两次。但**三个数要在这一次里问全**：
    ``plan_shards`` 同时要用 ``step_size`` / ``min_qty`` / ``min_notional``，而 ``symbol_rules``
    是一次网络往返。分片那一步再问一遍 ``symbol_rules`` 是**无守卫**的重问——它抛错时异常
    会穿过 ``plan_shards`` 的调用点落进 ``_run_symbol`` 的兜底，被记成一次「减仓过程中断」；
    而真相是「精度问到了、最小名义额问不到」，本该整笔一次。
    """
    try:
        rules = await adapter.symbol_rules(symbol)
    except Exception as exc:  # noqa: BLE001 — 问不到就是问不到，不猜
        return None, None, Decimal("0"), f"交易所精度规则取不到（{type(exc).__name__}: {exc}）"
    if rules is None:
        return None, None, Decimal("0"), "适配器不声明该品种的精度规则"
    try:
        min_notional = await adapter.min_notional(symbol)
    except Exception as exc:  # noqa: BLE001
        return (
            rules.step_size,
            rules.min_qty,
            None,
            f"最小名义额取不到（{type(exc).__name__}: {exc}）",
        )
    return rules.step_size, rules.min_qty, min_notional, ""


async def _run_account(plan: AccountPlan, *, day: date, at: datetime) -> AccountOutcome:
    """一个账户的整条账本。**任何异常都不许把已经减掉的事实弄丢**。"""
    account = plan.account
    outcome = AccountOutcome(label=plan.label)

    adapter, why = await _open_adapter(account)
    if adapter is None:
        logger.warning("[Reduce] 账户 %s 跳过：%s", plan.label, why)
        return outcome

    try:
        # 持仓：**交易所是唯一真相**。取不到就整段作废——不知道有几张什么单的时候撤单与
        # 减仓都可能撤错、减错，而错的方向（多减、撤掉平仓单）不可逆。
        try:
            positions = await reduce.account_positions(adapter)
        except Exception as exc:  # noqa: BLE001
            reason = f"账户持仓取不到（{type(exc).__name__}: {exc}），本次未撤单也未减仓"
            logger.error("[Reduce] %s：%s", plan.label, reason)
            await _alert(plan.recipients, f"⚠️ 事件熔断自动减仓未开始｜{plan.label}\n{reason}")
            return outcome

        # **收窄到真持仓只能在 async 侧做**：事件给的是作用域（可能是全市场），而
        # 「这个账户里到底有哪些品种」只有交易所知道（本地没有持仓表，`account_positions`
        # 的 docstring 明写这一点）。所以计划里只装（事件 × 作用域），品种在这里才落地。
        targets = targets_for(plan.firing, positions)
        if not targets:
            # 事件覆盖的品种一个都没持仓 = 无可减。**不写记录**：没有（事件 × 品种）这一对
            # 可以落，硬造一行会让「本日第 N 次」把一个什么也没发生的品种也算进去。
            logger.info("[Reduce] 账户 %s：覆盖到的品种都没有持仓，跳过", plan.label)
            return outcome
        symbols = [target.symbol for target in targets]

        # 撤单先于减仓（CONTEXT.md:122）。`position_side` 直接把持仓表递下去：适配器那边的
        # `is_opening_order(side, None)` 为真，于是「没有持仓 ⇒ 所有挂单都是开仓意图」
        # 不需要在这里再写一遍。
        try:
            cancel = await reduce.cancel_opening_orders(
                adapter, symbols, position_side=positions
            )
            outcome.cancel_lines = list(cancel.lines())
            cancel_report = cancel.as_dict()
        except Exception as exc:  # noqa: BLE001 — 撤单这条路整体塌了也不阻断减仓
            cancel_report = {"error": f"{type(exc).__name__}: {exc}"}
            outcome.cancel_lines = [
                f"**撤单整体失败**（{type(exc).__name__}: {exc}）："
                "本次未能枚举挂单，敞口在窗口内仍可能增加"
            ]
            logger.warning("[Reduce] %s 撤单整体失败：%s", plan.label, exc)

        paused = await db_async(live_pause_symbols)(account)
        for target in targets:
            target_outcome = await _run_symbol(
                plan,
                adapter,
                target=target,
                position=positions[target.symbol],
                day=day,
                at=at,
                cancel_report=cancel_report,
                paused=paused,
            )
            if target_outcome is None:
                outcome.skipped += 1
                continue
            # 这一行收场了（NO_OP 除外）⇒ 本账户本轮的后续品种停手，并且整轮停手。
            if target_outcome.status is not ReduceStatus.NO_OP:
                outcome.stop_reason = (
                    f"本日已有收场的自动减仓（{target_outcome.symbol}）"
                )
            outcome.details.append(target_outcome)
            outcome.rows += 1
        return outcome
    finally:
        try:
            await adapter.disconnect()
        except Exception as exc:  # noqa: BLE001 — 收尾失败不该盖掉真正的结论
            logger.warning("[Reduce] %s 断开连接失败：%s", plan.label, exc)


async def _run_symbol(
    plan: AccountPlan,
    adapter,
    *,
    target: Target,
    position: tuple[str, Decimal],
    day: date,
    at: datetime,
    cancel_report: dict,
    paused: set[str],
) -> RecordOutcome | None:
    """一个（事件 × 品种）的减仓。``None`` = 什么都没做（已跳过）。

    ``target`` 是**一个品种**（``Target.symbol`` 在多于一个时直接抛），所以这里不需要
    再问「这个品种属不属于那个事件」——收窄那一步（``targets_for``）已经问过了。
    """
    account = plan.account
    symbol = target.symbol
    side_of_position, qty_before = position

    if side_of_position not in ("long", "short"):
        # 方向认不出来时**不下单**。`reduce.account_positions` 只承诺
        # 「交易所口径品种 → (方向, 数量)」，而下面的 `side` 是照着方向取的；读出一个别的东西
        # 说明适配器或交易所那边的语义变了，这时 `else "buy"` 会按**做多**处理——对一个其实
        # 是空头的持仓就是**加仓**。而减仓这条链路上没有任何一步会检查「这一单是不是把仓位
        # 做大了」，唯一的方向保险就是这里。fail-closed：跳过并留日志。
        logger.error(
            "[Reduce] %s %s 持仓方向无法识别（%r），本轮不减仓",
            plan.label,
            symbol,
            side_of_position,
        )
        return None

    if symbol in paused:
        # 暂停的是**下新单**（`RegimeReducePause` docstring）。上一轮的滑点超限还没被人工
        # 处理，这里既不认领也不减，但**撤单照旧**（上面已经做过）。
        logger.info("[Reduce] %s %s 已有生效中的自动减仓暂停，跳过减仓", plan.label, symbol)
        await _alert(
            plan.recipients,
            f"⚠️ 事件熔断自动减仓已暂停｜{plan.label}\n"
            f"品种 {symbol}：该品种的自动减仓暂停仍在生效中（滑点超限后转人工），"
            f"本轮只撤单、未减仓。请人工处理后解除暂停。",
        )
        return None

    gate = await db_async(enable_later)(account, day)
    if not gate.allowed:
        logger.info("[Reduce] %s 跳过减仓：%s", plan.label, gate.reason)
        await _alert(
            plan.recipients,
            f"⚠️ 自动减仓已暂停｜{plan.label}\n"
            f"品种 {symbol} 本轮未减仓。\n原因：{gate.reason}",
        )
        return None

    stale = await db_async(stale_claims)(account)
    if stale:
        # 上一轮的认领没收尾 = 交易所侧可能还有活单。**绝不自动接着减**（模块 docstring）。
        detail = "；".join(str(row) for row in stale[:5])
        logger.error("[Reduce] %s 有未收尾的认领：%s", plan.label, detail)
        await _alert(
            plan.recipients,
            f"⚠️ 事件熔断自动减仓未继续｜{plan.label}\n"
            f"品种 {symbol} 本轮未减仓。\n"
            f"原因：本账户有 {len(stale)} 条认领没有收尾（{detail}），"
            f"交易所侧可能仍有活单。**请人工核对后再处理。**",
        )
        return None

    side = "sell" if side_of_position == "long" else "buy"
    ordinal = await db_async(ordinal_for)(account, day, target.event_id)
    record, created = await db_async(claim_record)(
        account,
        event_id=target.event_id,
        symbol=symbol,
        day=day,
        qty_before=qty_before,
        reason=(
            f"高影响事件熔断：{target.event_name}"
            f"（{halt.scope_display(target.scope)}）"
        ),
    )
    if not created:
        # 这一行早就在（上一轮做过了）。**幂等靠的是唯一键，不是这里的判断**——判断只是
        # 让日志能说清「为什么这一轮什么也没做」。
        logger.info(
            "[Reduce] %s %s 已有记录（%s），本轮不重复减仓",
            plan.label,
            symbol,
            record.status_display,
        )
        return None

    try:
        return await _execute_symbol(
            plan,
            adapter,
            record=record,
            symbol=symbol,
            side=side,
            qty_before=qty_before,
            ordinal=ordinal,
            day=day,
            at=at,
            cancel_report=cancel_report,
            event_id=target.event_id,
        )
    except Exception as exc:  # noqa: BLE001 — 收尾必须尽力，然后照抛
        reason = f"减仓过程中断（{type(exc).__name__}: {exc}）"
        logger.exception("[Reduce] %s %s %s", plan.label, symbol, reason)
        try:
            await db_async(finish)(
                record,
                status=ReduceStatus.FAILED,
                cancel_report=cancel_report,
                extra_reason=reason,
            )
        except Exception:  # noqa: BLE001 — 连收尾都失败：记录留在 CLAIMED，下一轮会被
            # `stale_claims` 抓住并转人工。**这就是那条分支存在的意义。**
            logger.exception("[Reduce] %s %s 收尾写入也失败，记录停在认领态", plan.label, symbol)
        await _alert(
            plan.recipients,
            render_reduce_failure(
                label=plan.label, symbol=symbol, reason=reason, ordinal=ordinal, day=day
            ),
        )
        raise


async def _execute_symbol(
    plan: AccountPlan,
    adapter,
    *,
    record: RegimeReduceRecord,
    symbol: str,
    side: str,
    qty_before: Decimal,
    ordinal: int,
    day: date,
    at: datetime,
    cancel_report: dict,
    event_id: int,
) -> RecordOutcome:
    """认领之后：中间价 → 分片 → 逐片下单 → 收尾。"""
    step_size, min_qty, min_notional, rules_note = await _load_rules(adapter, symbol)

    if step_size is None:
        # 精度规则问不到：**一张都不发**。理由见模块 docstring 的「三种问不到」——
        # 猜一个 step_size 会下单失败，而不取整的单多半被拒、且那种拒单在适配器里是可重试的，
        # 三次之后会记成「可能仍有活单」。
        reason = f"{rules_note}，本次未下单（不猜一个精度顶替）"
        await db_async(finish)(
            record,
            status=ReduceStatus.FAILED,
            cancel_report=cancel_report,
            extra_reason=reason,
        )
        await _alert(
            plan.recipients,
            render_reduce_failure(
                label=plan.label, symbol=symbol, reason=reason, ordinal=ordinal, day=day
            ),
        )
        return RecordOutcome(
            symbol=symbol,
            status=ReduceStatus.FAILED,
            ordinal=ordinal,
            baseline_qty=record.baseline_qty,
            qty_after=None,
            note=rules_note,
        )

    # 参考价取在**撤单之后、第一片之前**（CONTEXT.md:129），所以它不含撤单造成的移动。
    reference = await reduce.mid_price(adapter, symbol)

    plan_shard = plan_shards(
        qty_before,
        step_size=step_size,
        min_qty=min_qty,
        min_notional=min_notional,
        reference=reference,
        shard_count=config.DERISK.reduce_shard_count,
    )
    if not plan_shard.ok:
        # 「无可减」：不是失败，是真的没什么可减。`NO_OP` 与 `FAILED` 必须分开
        # （`ReduceStatus` docstring 的三分类）——把空仓或取整成 0 记成事故，后果是有人
        # 半夜去查一场不存在的故障，同时把真正该看的失败淹掉。
        await db_async(finish)(
            record,
            status=ReduceStatus.NO_OP,
            planned_qty=Decimal("0"),
            cancel_report=cancel_report,
            extra_reason=plan_shard.note,
        )
        logger.info("[Reduce] %s %s 无可减：%s", plan.label, symbol, plan_shard.note)
        return RecordOutcome(
            symbol=symbol,
            status=ReduceStatus.NO_OP,
            ordinal=ordinal,
            baseline_qty=record.baseline_qty,
            qty_after=qty_before,
            note=plan_shard.note,
        )

    results = []
    for index, quantity in enumerate(plan_shard.shards):
        shard = reduce.ShardOrder(
            client_order_id=shard_client_id(plan.account, event_id, symbol, index),
            symbol=symbol,
            side=side,
            quantity=quantity,
        )
        result = await reduce.place_reduce_shard(adapter, shard)
        results.append(result)
        # 落库（fix 2）：自动减仓成交也要有 Order 行（口径同 positions.py 的手动平仓）。
        # **落库失败不得让减仓失败**——子单已经发出去了，异常只会把一次已发生的动作
        # 读成「减仓过程中断」。只 warning，`RegimeReduceRecord.sub_orders` 仍有留痕。
        try:
            from apps.trading import reduce_ledger

            await reduce_ledger.persist_reduce_order(
                plan.account,
                shard,
                result,
                adapter=adapter,
                trigger=reduce_ledger.TRIGGER_REGIME_REDUCE,
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "[Reduce] %s %s 减仓成交落库失败：%s", plan.label, symbol, result.exchange_order_id,
                exc_info=True,
            )
        if result.status == reduce.SHARD_STATUS_UNFINISHED:
            # 可能有一张活单。**立刻收手**：下一片会再开一个新敞口，而这一单的下场还未知。
            break
        if result.status == reduce.SHARD_STATUS_REJECTED:
            # 确定没成交。剩余的量**不换新单接着减**（CONTEXT.md:125 明写不许降级成这样）。
            break

    sub_orders = [result.as_dict() for result in results]
    filled = sum((result.filled_qty for result in results), Decimal("0"))
    unfinished = any(
        result.status == reduce.SHARD_STATUS_UNFINISHED for result in results
    )

    qty_after: Decimal | None = None
    if not unfinished:
        # `unfinished` 时**不读余量**：此刻交易所侧还有一张不知道会不会成交的单，读到的
        # 任何数都只是「此刻」，写进 `qty_after` 会让它看起来像一次结算后的余量。
        try:
            after = await reduce.account_positions(adapter)
            qty_after = after.get(symbol, ("", Decimal("0")))[1]
        except Exception as exc:  # noqa: BLE001 — 余量读不到不影响「已经减了多少」的结论
            logger.warning("[Reduce] %s %s 余量读取失败：%s", plan.label, symbol, exc)

    if unfinished:
        status = ReduceStatus.UNSETTLED
    elif filled <= 0:
        status = ReduceStatus.FAILED
    elif filled >= plan_shard.planned_qty:
        status = ReduceStatus.SUCCEEDED
    else:
        status = ReduceStatus.PARTIAL

    slippage_pct, slippage_basis, paused = await _judge_slippage(
        plan,
        symbol=symbol,
        reference=reference,
        side=side,
        results=results,
        record=record,
        day=day,
        at=at,
        ordinal=ordinal,
    )

    note = plan_shard.note
    if unfinished:
        note = "，".join(filter(None, [note, "有子单发满次数仍无结论，可能仍有活单"]))
    await db_async(finish)(
        record,
        status=status,
        planned_qty=plan_shard.planned_qty,
        qty_after=qty_after,
        sub_orders=sub_orders,
        cancel_report=cancel_report,
        slippage_pct=slippage_pct,
        slippage_basis=slippage_basis,
        extra_reason=note,
    )

    if status is ReduceStatus.UNSETTLED:
        # 唯一一种会让人停下来的一种结果（`reduce.py` docstring）：报警必须写明「可能仍有活单」。
        await _alert(
            plan.recipients,
            f"⚠️ 事件熔断自动减仓未得出结论｜{plan.label}\n"
            f"品种 {symbol}：本日第 {ordinal} 次熔断，有子单发满次数仍无结论，"
            f"**交易所侧可能仍有活单**，请人工核对。\n"
            f"已确认成交 {filled}（计划 {plan_shard.planned_qty}）。",
        )

    # 人话里那个「累计已减至基准的 X%」要的是**余量**。三种情况必须分开：
    if unfinished:
        # 「可能仍有活单」时**不给比例**。拿 `qty_before` 顶替会渲染成「累计已减至基准的
        # 100%」——一句「什么都没减」的假话，而真相是「不知道」。
        reported_after = None
    elif qty_after is not None:
        reported_after = qty_after
    elif filled <= 0:
        # 一张都没成交 ⇒ 余量就是减之前那个数（读不到余量只是没读到，不是变了）。
        reported_after = qty_before
    else:
        # 有成交、但余量没读到：这里**不能**用 `qty_before` 顶替，那是减之前的数。
        reported_after = None

    return RecordOutcome(
        symbol=symbol,
        status=status,
        ordinal=ordinal,
        baseline_qty=record.baseline_qty,
        qty_after=reported_after,
        note=note,
        slippage_basis=slippage_basis,
        slippage_pct=slippage_pct,
        paused=paused,
    )


async def _judge_slippage(
    plan: AccountPlan,
    *,
    symbol: str,
    reference: Decimal | None,
    side: str,
    results: Sequence,
    record: RegimeReduceRecord,
    day: date,
    at: datetime,
    ordinal: int,
) -> tuple[Decimal | None, SlippageBasis, bool]:
    """滑点判定 + 超限暂停。返回 ``(滑点, 基准说明, 本次是否新开了暂停)``。

    三种基准**都必须留下显式记录**（``SlippageBasis`` docstring）：留空与「没跑到」在库里
    长得一样，而把 ``UNAVAILABLE`` 按 0 处理更糟——0 的含义是「滑点完美」，那是一条假的好
    消息。

    滑点用 ``reduce.slippage_pct``（正 = 不利）与 ``reduce.volume_weighted_price``（只有成交
    了的子单参与），两者都是机械层的纯函数，这里不重新实现一遍算术。
    """
    if reference is None or reference <= 0:
        return None, SlippageBasis.UNAVAILABLE, False

    vwap = reduce.volume_weighted_price(results)
    if vwap is None:
        return None, SlippageBasis.NO_FILL, False

    pct = reduce.slippage_pct(reference, vwap, side)
    if pct is None:
        return None, SlippageBasis.UNAVAILABLE, False

    limit = config.DERISK.slippage_limit_pct
    if pct <= limit:
        return pct, SlippageBasis.MID, False

    reason = (
        f"单次熔断减仓滑点 {pct} 超过上限 {limit}（分数口径），"
        f"已暂停该品种的自动减仓并转人工（CONTEXT.md:129）"
    )
    opened = await db_async(record_pause)(
        plan.account,
        symbol=symbol,
        reason=reason,
        evidence={
            "slippage_pct": str(pct),
            "limit": str(limit),
            "reference_price": str(reference),
            "vwap": str(vwap),
            "record_id": record.pk,
            "business_day": str(day),
            "ordinal": ordinal,
            "sub_orders": [result.as_dict() for result in results],
        },
        at=at,
    )
    logger.warning("[Reduce] %s %s %s", plan.label, symbol, reason)
    if opened:
        await _alert(
            plan.recipients,
            f"⚠️ 自动减仓已暂停（滑点超限）｜{plan.label}\n"
            f"品种 {symbol}：本日第 {ordinal} 次熔断，本次减仓滑点 {pct} > 上限 {limit}。\n"
            f"**该品种的自动减仓已暂停，转人工**；参考价 {reference}，成交均价 {vwap}。\n"
            f"解除只走人工（机制不自己恢复：滑点正常只说明这一次好，不说明上一次为什么坏）。",
        )
    return pct, SlippageBasis.MID, opened


# --------------------------------------------------------------------------- #
# 入口：同步装配 + 一次 asyncio.run
# --------------------------------------------------------------------------- #


def build_plans(*, at: datetime) -> list[AccountPlan]:
    """这一轮该动哪些账户、各自动什么。**全是只读**，且都在同步侧完成。

    计划里装的是（事件 × 作用域）那一对，**不是品种**：品种要拿真实持仓收窄，而真实持仓
    只能走适配器读（本地没有持仓表），那一步只能在 async 侧做。这里问的是「有没有事件
    压在某个作用域上」——它与账户里有没有那个品种无关，所以答得出来。
    """
    scopes = scopes_in_force(now=at)
    if not scopes:
        return []
    firing = firing_pairs(scopes, halt_sync.high_impact_events(), at)
    if not firing:
        return []

    plans: list[AccountPlan] = []
    for account in configured_accounts():
        recipients, note = recipients_for(account)
        plans.append(
            AccountPlan(
                account=account,
                label=f"{account.exchange}（{account.label or account.pk}）",
                firing=tuple(firing),
                recipients=tuple(recipients),
                recipient_note=note,
            )
        )
    return plans


async def _execute(plans: Sequence[AccountPlan], *, day: date, at: datetime) -> dict:
    """整轮的唯一 async 段。**一次 ``asyncio.run`` 里跑完所有账户**（Q8 的闸门需要这样：
    「本轮已经有人收场」是整轮的共享事实，不是每个账户各自重算出来的）。"""
    summary = {"accounts": len(plans), "rows": 0, "skipped": 0, "stop": ""}
    for plan in plans:
        if summary["stop"]:
            summary["skipped"] += 1
            await _alert(
                plan.recipients,
                f"⚠️ 自动减仓已暂停｜{plan.label}\n"
                f"本轮未处理该账户。\n原因：{summary['stop']}\n"
                f"（机制做出一次动作之后停下来等人确认；下一轮会继续处理。）",
            )
            continue
        try:
            outcome = await _run_account(plan, day=day, at=at)
        except Exception:  # noqa: BLE001 — 一个账户塌了不该让别的账户连日志都没有
            logger.exception("[Reduce] 账户 %s 本轮中断", plan.label)
            summary["skipped"] += 1
            continue

        if outcome.rows:
            # 逐账户一条结果消息，**不裁剪**（CONTEXT.md:116 第二条）。撤单那一段挂在
            # `outcome.cancel_lines` 上（账户口径，一次撤单调用一行），它**反推不出来**：
            # 「本次未能枚举挂单」那句话只在内存里存在过，没落进任何一行记录。
            text = render_account_report(
                label=plan.label,
                day=day,
                outcomes=outcome.details,
                cancel_lines=outcome.cancel_lines,
            )
            if plan.recipient_note:
                text = f"{text}\n{plan.recipient_note}"
            sent = await _alert(plan.recipients, text)
            logger.info(
                "[Reduce] %s 汇报送达 %s/%s 人：%s",
                plan.label,
                sent,
                len(plan.recipients),
                [item.symbol for item in outcome.details],
            )
        summary["rows"] += outcome.rows
        summary["skipped"] += outcome.skipped
        if outcome.stop_reason:
            summary["stop"] = f"{plan.label} {outcome.stop_reason}"
            logger.info("[Reduce] 本轮收手：%s", summary["stop"])
    return summary


def dispatch(*, now: datetime | None = None) -> dict:
    """窗口同步任务里的那一次调用：装配（同步）→ 执行（async）→ 返回给日志的摘要。

    ``now`` 是给测试与重放用的；生产走 ``timezone.now()``。业务日由**同一个** ``at`` 算出来，
    所以跨零点的那一轮不会出现「按昨天的业务日减、按今天的业务日算第几次」。
    """
    at = now or timezone.now()
    day = business_day(at)
    plans = build_plans(at=at)
    if not plans:
        logger.info("[Reduce] 本轮没有需要减仓的账户（业务日 %s）", day)
        return {"accounts": 0, "rows": 0, "skipped": 0, "stop": ""}
    return asyncio.run(_execute(plans, day=day, at=at))
