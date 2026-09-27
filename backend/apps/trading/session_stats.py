"""会话详情里的收益统计（CONTEXT.md Glossary「会话归因已实现盈亏」）。

**口径**：只统计带 `realized_pnl` 的订单——即平仓成交产生的那部分。所以

* 开仓单不在内（它们没有已实现盈亏）；
* **未实现的部分不在内**：会话停止时不关仓（全仓没有任何代码会关），那部分敞口留在
  账户上，只能去账户持仓里看；
* **熔断减仓实现的盈亏不在内**：减仓作用于账户持仓，其子单不挂会话外键
  （`reduce.py` 全文没有 `live_session`，见 `docs/adr/0003`）。被减过仓的会话，这里的
  数字因此**系统性偏乐观**——这是已知的、写进 caveats 的边界，不是待修的缺陷。

两个数必须同时给：**胜率与它的分母**。「胜率 55.6%」与「5/9」说的是同一件事，但只有
后者能让人看出样本有多小。本仓对「假自信」的既有处理是证据门槛 ≥30 笔（CONTEXT.md
第 35 条），这里取同一个数做「样本不足」标注——**用途不同**：那条门槛决定要不要停用一个
策略，这里的标注不做任何动作，只是别让 4 笔的胜率看起来像结论。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Iterable

from django.db.models import DecimalField, Sum
from django.db.models.functions import Coalesce

from .models import Order

#: 「样本不足」的分界。与 CONTEXT.md 第 35 条的证据门槛取同一个数（同一个仓对「多少笔
#: 才敢看胜率」的既有口径），但**不是**那条门槛：这里只产出一句标注，不产生任何动作。
SAMPLE_THRESHOLD = 30

#: 金额的统一量化口径，与 `Order.realized_pnl` 的 `decimal_places=8` 对齐。
_MONEY = Decimal("0.00000001")

#: 这两句随数字一起返回、由前端原样显示：它们讲的是**数字覆盖了什么**，而这件事只在
#: 产生数字的这一处知道。放在前端写死的话，将来谁改了 reduce 的归属，没有一个地方
#: 会变红。
_CAVEATS = (
    "不含未实现部分：会话停止时不关仓，敞口留在账户上（见账户持仓）。",
    "不含熔断减仓实现的盈亏：减仓作用于账户持仓，其子单不属于任何会话。",
)


def money(value: Decimal | None) -> str | None:
    """金额 → 字符串（8 位小数，定点写法）。None 原样返回。

    出参是字符串而不是数字，与 `OrderSerializer` 的 DecimalField 一致：这条链路上
    的金额从不经过二进制浮点。

    **用 `:f` 而不是 `str()`**：`Decimal("0").quantize(...)` 的 `str()` 是 `"0E-8"`
    ——科学计数法。它在 JS 侧恰好还能被 `parseFloat` 读成 0，所以这个错不会以「显示
    不对」的形式暴露，只会以「接口里躺着一个怪字符串」的形式留着。
    """
    if value is None:
        return None
    return f"{Decimal(value).quantize(_MONEY):f}"


def compute_session_stats(pnls: Iterable[Decimal | None]) -> dict:
    """一串已实现盈亏 → 统计。**纯函数**，不碰库。

    一笔 = 一张带 `realized_pnl` 的订单。部分成交后补齐的那张单仍是一行（`_apply_close_pnl`
    用增量修正，盈亏写回同一行），所以按行计数不会重复。

    「胜」的判据是 `> 0`：**0 与亏损同列**。盈亏恰好为 0 的那笔既不赚也不亏，把它算进
    胜率只会让胜率看起来比实际好，而这个数没有任何一个用途需要它偏高。

    n = 0 时比率类字段一律为 `None`（不是 0）：一条还没成交的会话没有胜率，写 0 会被读成
    「一笔没赢过」。
    """
    values = [Decimal(v) for v in pnls if v is not None]
    count = len(values)

    if count == 0:
        return {
            "realized_pnl_total": money(Decimal("0")),
            "closed_orders": 0,
            "winning_orders": 0,
            "win_rate": None,
            "sample_threshold": SAMPLE_THRESHOLD,
            "sample_sufficient": False,
            "avg_pnl": None,
            "best_pnl": None,
            "worst_pnl": None,
            "caveats": list(_CAVEATS),
        }

    total = sum(values, Decimal("0"))
    winning = sum(1 for v in values if v > 0)

    return {
        "realized_pnl_total": money(total),
        "closed_orders": count,
        "winning_orders": winning,
        "win_rate": round(winning / count, 4),
        "sample_threshold": SAMPLE_THRESHOLD,
        "sample_sufficient": count >= SAMPLE_THRESHOLD,
        "avg_pnl": money(total / count),
        "best_pnl": money(max(values)),
        "worst_pnl": money(min(values)),
        "caveats": list(_CAVEATS),
    }


def session_stats_for(session) -> dict:
    """一条会话的统计。**取数的唯一入口**：详情视图与任何别的调用方都走这里，
    免得「同一个数在第二处按另一套过滤条件算一遍」。
    """
    pnls = Order.objects.filter(
        live_session=session, realized_pnl__isnull=False
    ).values_list("realized_pnl", flat=True)
    return compute_session_stats(pnls)


def annotate_realized_pnl(queryset):
    """给一批会话挂上 `realized_pnl` 注解：列表接口用（一次查询算完 50 条）。

    与 `session_stats_for` 是**同一个口径的两种取数**——这里是 SQL 聚合，那里是逐单汇总。
    两条测试钉着它们对同一条会话给出同一个数：它们一旦分叉，用户会在同一屏上看到卡片与
    弹窗两个不一样的「盈亏」，而两处看起来都正常。
    """
    return queryset.annotate(
        realized_pnl=Coalesce(
            Sum("orders__realized_pnl"),
            Decimal("0"),
            output_field=DecimalField(max_digits=20, decimal_places=8),
        )
    )
