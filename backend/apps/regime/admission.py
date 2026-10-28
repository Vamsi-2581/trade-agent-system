"""「阶段 × 格子 → 放行 / 暂停开新仓」：**唯一一处**判据（spec #30 D1、票 #51）。

本模块只回答一个问题：**这条策略在当前生效阶段上，现在该不该被暂停开新仓。**

## 为什么必须只有一处

同一个判据有两个消费者：候选漏斗（`apps.strategy_factory.evaluate.regime_verdict`，
在「样本外通过」与「paper 期满」两个升档点各问一次）和 halt 侧（按当前生效阶段逐单问
适用性）。两份实现迟早分家——某天只改了一边的顺序或某个取值，另一个消费者就按旧口径
安静地放行，而两边各自看都自洽。所以判据代码只有这里一份，两侧都调它，各自只负责把
结论接回自己的状态机。

## 语义边界（先读这两条，再读代码）

1. `neutral` 被拦住**不是**「判定为不适合」，而是「证据不足 ⇒ 暂停开新仓」。它与
   deactivation 维度的「中性不构成停用理由」（`deactivation.py` 开头那三件事之一）
   **并存**，不是矛盾：那边问的是「该不该把这条策略停掉」（停用要人工恢复，中性够不上
   这个门槛），这边问的是「现在该不该开新仓」（fail-closed——说得出来才放）。同一个
   `neutral` 在两个问题上有不同处置，正是「证据不足与不合格是两件事」这条纪律的落点。
2. `blanket` 与高波动档**不发表意见**（`suspend=False` + `CODE_BLANKET`），不能被读成
   `unfit`。保命档有自己那条全场停机链（`deactivation.is_blanket` → 保命档那一层负责
   停全场），本判据不重复产出结论。把它读成 `unfit` 的后果：这一侧的回边要靠格子回到
   `fit`，而保命档**会自己解除**，两条解除路径互不相遇——候选在保命档期间一个都出不去，
   等波动早没了也没人推它。

## 纯函数

不 import ORM、不查库、不写日志、不读时钟。`regime` / `cell_state` 是调用方已经取好的
纯值；「在期豁免」也由采集侧按它自己的时钟判完，这里只看见 `exempt` 这个布尔结论。
`is_blanket` 直接取自 `deactivation`：保命档的判据也只能有一处，抄一份 `high_vol`
字面量就又多了一处会与新阶段取值分家的地方。
"""

from __future__ import annotations

from dataclasses import dataclass

from apps.regime.deactivation import is_blanket
from apps.regime.slice import STATE_BLANKET, STATE_FIT, STATE_UNFIT

#: 不发表意见，原因是保命档（高波动阶段 / 格子为 `blanket`）。
CODE_BLANKET = "blanket"
#: 不发表意见，原因是在期人工豁免。
CODE_EXEMPT = "exempt"
#: 放行，原因是格子判为适用。
CODE_FIT = "fit"
#: 暂停开新仓，原因是格子明确判为不适用。
CODE_UNFIT = "unfit"
#: 暂停开新仓，原因是**判不了**（证据不足 / 没有格子 / 没有生效阶段）。
CODE_NO_EVIDENCE = "no_evidence"


@dataclass(frozen=True)
class Verdict:
    """一次适用性判定：拦不拦 + 为什么。

    `code` 与 `suspend` 并列而不是把原因折进布尔：`suspend=False` 有三种不同的意思
    （保命档不发表意见 / 人工豁免 / 格子适用），调用方要按 code 分派（候选侧需要
    分别落成 `None` 与一条豁免留痕），压成一个布尔就再也分不出来了。
    """

    #: True = 暂停开新仓。
    suspend: bool
    #: 上面五个 `CODE_*` 之一。
    code: str


def admission(
    regime: str | None, cell_state: str | None, *, exempt: bool = False
) -> Verdict:
    """当前阶段 × 这一格的结论 → 放行还是暂停开新仓（判据顺序见下，不可调换）。

    这里的五步顺序是**既有裁决**（候选漏斗侧早已按此运行），照抄而不是「改进」：
    `apps.strategy_factory.tests.test_evaluate.py` 那一组用例是它的回归网。

    1. **保命档（不发表意见）**：阶段高波动，或格子是 `blanket`，二者其一命中即
       `CODE_BLANKET`。**按阶段判的这半条不能省**：阶段是高波动而池化窗口内一个高波日
       都没有时，`slice._cell` 先判 `regime_days == 0`，格子落 `unknown` 而不是
       `blanket`，只看格子就会把一次保命档读成「判不了」并因此拦人——而停机链那侧按
       阶段判（同一个 `is_blanket`），两边于是对同一片行情给出两个结论。
       它排在豁免之前：保命档期间不该有「放行」这回事，豁免也穿透不了它。
    2. **在期人工豁免（放行）**：`CODE_EXEMPT`。排在格子之前——豁免是**人的动作**，
       有人看过这一格并决定放它过去，次序反了会让一个明确的人工决定输给机制算出来的
       结论。
    3. **适用（不发表意见）**：`CODE_FIT`。显式一支而不是落进失败的兜底：漏掉它，「适用」
       会被判成「判不了」并拦下候选，于是整条漏斗在行情最好的时候集体停摆，而理由写的
       是「证据不足」，把排查方向引到无关的地方去。
    4. **明确不适用**：`CODE_UNFIT`。唯一一个「行情说了不行」的取值。
    5. **其余一切（不发表意见的反面：拦）**：`neutral` / `unknown` / 没有格子
       （`cell_state is None`，当代池化表没这一格）/ 没有生效阶段（`regime is None`，
       冷启动）——四种情形同一个 fail-closed 结论 `CODE_NO_EVIDENCE`。
    """
    # 1. 保命档：不发表意见（它有自己那条全场停机链，本判据不重复产出结论）。
    if is_blanket(regime) or cell_state == STATE_BLANKET:
        return Verdict(False, CODE_BLANKET)

    # 2. 在期人工豁免：放行（留痕由调用方按自己的原因码写）。
    if exempt:
        return Verdict(False, CODE_EXEMPT)

    # 3. 适用：放行，且不合成任何「通过」结论——调用方照原有路径前进。
    if cell_state == STATE_FIT:
        return Verdict(False, CODE_FIT)

    # 4. 行情明确判为不适用。
    if cell_state == STATE_UNFIT:
        return Verdict(True, CODE_UNFIT)

    # 5. 判不了（fail-closed）：「没证据」永远读成「不放行」，绝不能读成「放行」。
    return Verdict(True, CODE_NO_EVIDENCE)
