"""启动会话那一刻的熔断回显（CONTEXT.md 第 172 条）。

第 172 条要求：被 halt 拦下的任何动作必须回显是哪个触发源在拦，**而且这条回显必须落在
「用户发起动作」的那一刻**。理由是这条链路里没有第二个人在场——全仓没有任何让用户直接
下单的接口（Order 只有 GET、无 admin、无管理命令、聊天渠道与 Agent 工具都不建单），
用户能发起、会**间接产生**下单动作的只有启动一个会话（`live_session_start`）与恢复一个
被暂停的会话（`live_session_resume`）。paper 验收与实盘走同一对视图是刻意的（验收要求
行为一致），所以两个方向回显同一段话。

它们的订单在 K 线到达之后才产生，**那时没有任何人正在看屏幕**——回显若只在订单被拦的
那一刻产生，用户永远看不到它。所以这里做一件事：在启动的响应里就说清「会话已启动，
但它开不了新仓」。它**不阻止启动**：存量仓位的止损保护必须照常上线（第 172 条），
只把「你启动了但它开不了仓」提前说清楚。

四条形状：

1. **只报命中的层，不命中就一个字都不说**（返回空串）。裁剪按「作用域是否命中这个会话」
   决定（交给 `halt.halt_layers` 的 `symbol` / `strategy_id`），**不按入口一刀切**：
   否则每次启动都附一段与己无关的熔断通告（SOL 解锁窗口里启动一个只跑 BTC 的会话），
   很快就没有人读了。这与 `query_halt` 相反——那条工具**返回全部生效层**，因为用户主动
   追问的是「现在系统在拦什么」，裁剪反而会让他怀疑工具在瞒他（第 172 条）。
2. **判定用的是下单通路那一个口径。** 这里不自己读声明表，调 `halt.halt_layers` ——
   `RiskGuard.pre_trade_check` 走的正是同一个入口。自己再读一遍就是给「什么在拦」造第二个
   答案，而两个答案的分歧只会出现在任务没跑成的时候，那时没人在看。
3. **层标题取自 `HaltLayer.text`**（行 → 层的唯一换算口），所以这里的每一行与用户在
   `query_halt` 里读到的层标题、以及订单被拒时看到的理由是**逐字同源**的。在这个模块里
   重拼一遍「触发源 + 作用域」，就是「同一件事在两处说法不同」。
4. **必须说出「减仓照常」。** 不说这一句，「停止」会被读成「什么都动不了」，而存量仓位的
   止损恰恰是熔断时唯一想让它继续动起来的事（`HaltVerdict.reason` 与 `query_halt` 里
   写着同一句，`RiskGuard.pre_trade_check` 的这一步只管开新仓）。

**时钟**：`now` 只用于求生效期，与 `halt.halt_layers` 同义；不注入时取墙上时钟——这条
回显讲的就是「你按下去的那一刻」。
"""

from __future__ import annotations

from datetime import datetime

from apps.regime import events, halt

#: 首行。**启动本身不被阻止**必须在这里说出来，否则这一页读起来像一次拒绝。
_HEADER = "⚠️ 会话已启动，但**开仓**被拦住：它现在开不了新仓。"

#: 尾行。存量仓位的止损保护是「不阻止启动」的**理由**（CONTEXT.md 第 172 条），也是
#: 「停止」最容易被误读掉的一半。
_FOOTER = "存量仓位的止损与减仓照常：停止判定只管开新仓，只减不增的单直接放行。"


def start_notice(
    symbol: str, strategy_id: str | None = None, *, now: datetime | None = None
) -> str:
    """这一张会话此刻会不会被拦住；**没命中返回空串**（调用方据此一个字都不说）。

    调用方（`live_session_start` / `live_session_resume`）拿到的这一份，与 K 线到达后
    `pre_trade_check` 求的那一份出自同一个函数、同一张声明表。`strategy_id` 与那一边
    同取（`LiveSession.strategy_id`）——**两边必须同时改**，理由见 `halt._matches`：
    只在一处带上 id，回显说「被策略停用决策拦住」而下单通路照放行，回显就成了假话。
    """
    verdict = halt.halt_layers(symbol, strategy_id, now=now)
    if not verdict.layers:
        return ""

    lines = [
        _HEADER,
        f"（{len(verdict.layers)} 层在拦；逐层的触发源、生效期与依据如下）",
    ]
    for index, layer in enumerate(verdict.layers, start=1):
        lines.append("")
        lines.append(f"【第 {index} 层｜{layer.text}】")
        lines.append(
            f"  生效期：{events.format_moment(layer.opened_at)}"
            f" → {events.format_moment(layer.expires_at)}"
        )
        lines.extend(f"  {line}" for line in (layer.reason or "").splitlines())
    lines.append("")
    lines.append(_FOOTER)
    return "\n".join(lines)
