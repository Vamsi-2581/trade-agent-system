"""判据下沉的钉子：`apps.regime.admission.admission`（票 #51）。

这个文件要钉住的是**一件事只有一处实现**带来的形状：

1. **判据顺序逐格钉死**：五种格子取值 × 有无豁免 × 三种阶段（普通阶段 / 高波动 /
   冷启动）—— 也就是「阶段 × 格子 → 放行 / 暂停」这张表的**每一格**。表是手写的字面量，
   不是把被测函数跑一遍抄下来：照抄等于没有断言。
2. **副作用相同、语义不同的两支必须分得开**：`fit`（适用）与 `blanket`（保命档）都不拦
   人，但一个「本来就该放」、一个「不发表意见」；`unfit`（明确不适用）与 `no_evidence`
   （判不了）都拦人，但通知里写「行情不适用」而真实情况是「切片缺数据」，排查方向会被
   引到无关的地方。所以 `code` 不是可省的装饰。
3. **豁免与保命档同时命中时保命档赢**：豁免是人的动作、排在格子之前，但排不到保命档
   之前（`deactivation_run.exemption_standing` 已明写「人工恢复豁免不穿透保命档」）。
4. **候选漏斗侧没有第二份判据**：同一组取值喂给
   `apps.strategy_factory.evaluate.regime_verdict`，落点必须与 `admission` 的 `code`
   一一对应。这一条是「禁止两份判据」在测试层的哨兵——谁把顺序在候选侧抄回去一份，
   两边就会在某个格子上分家，本文件立刻红。

零 DB、零时钟：`admission` 的两个入参全是调用方已经取好的纯值。
"""

from __future__ import annotations

import pytest

from apps.regime.admission import (
    CODE_BLANKET,
    CODE_EXEMPT,
    CODE_FIT,
    CODE_NO_EVIDENCE,
    CODE_UNFIT,
    admission,
)
from apps.regime.deactivation import is_blanket
from apps.regime.quant import BaseRegime
from apps.regime.slice import (
    STATE_BLANKET,
    STATE_FIT,
    STATE_NEUTRAL,
    STATE_UNFIT,
    STATE_UNKNOWN,
)

HIGH_VOL = BaseRegime.HIGH_VOL.value
RANGE = BaseRegime.RANGE.value

#: 格子取值的**全部**可能：五种结论 + 「没有这一格」（`None`）。
#: `None` 必须在表里：「当代池化表没这一格」与「结论是中性」是两件事，而它们在判据里
#: 落在同一个 fail-closed 结论上——两格都要有，否则「多了一种取值」时没人发现。
CELL_STATES = [
    None,
    STATE_FIT,
    STATE_UNFIT,
    STATE_NEUTRAL,
    STATE_UNKNOWN,
    STATE_BLANKET,
]

#: 阶段取值：普通阶段 / 保命档阶段 / 冷启动（没有生效判定）。
REGIMES = [RANGE, HIGH_VOL, None]

#: 手写判据表（**无豁免**）：`(格子, 阶段) → (suspend, code)`。共 6 × 3 = 18 格。
#: 高波动那一列整列 `blanket`：它是唯一一个**按阶段**判的取值，与格子说了什么无关
#: （`slice._cell` 先判 `regime_days == 0`，所以高波动阶段的格子可能是 `unknown`）。
WITHOUT_EXEMPT = {
    (None, RANGE): (True, CODE_NO_EVIDENCE),
    (STATE_FIT, RANGE): (False, CODE_FIT),
    (STATE_UNFIT, RANGE): (True, CODE_UNFIT),
    (STATE_NEUTRAL, RANGE): (True, CODE_NO_EVIDENCE),
    (STATE_UNKNOWN, RANGE): (True, CODE_NO_EVIDENCE),
    (STATE_BLANKET, RANGE): (False, CODE_BLANKET),
    (None, HIGH_VOL): (False, CODE_BLANKET),
    (STATE_FIT, HIGH_VOL): (False, CODE_BLANKET),
    (STATE_UNFIT, HIGH_VOL): (False, CODE_BLANKET),
    (STATE_NEUTRAL, HIGH_VOL): (False, CODE_BLANKET),
    (STATE_UNKNOWN, HIGH_VOL): (False, CODE_BLANKET),
    (STATE_BLANKET, HIGH_VOL): (False, CODE_BLANKET),
    (None, None): (True, CODE_NO_EVIDENCE),
    (STATE_FIT, None): (False, CODE_FIT),
    (STATE_UNFIT, None): (True, CODE_UNFIT),
    (STATE_NEUTRAL, None): (True, CODE_NO_EVIDENCE),
    (STATE_UNKNOWN, None): (True, CODE_NO_EVIDENCE),
    (STATE_BLANKET, None): (False, CODE_BLANKET),
}

#: 手写判据表（**有豁免**）：`(格子, 阶段) → (suspend, code)`。共 18 格。
#: 豁免排在保命档**之后**、格子**之前**，于是：高波动整列仍 `blanket`、格子 `blanket`
#: 那一行仍 `blanket`，其余一律 `exempt`（连 `fit` 也是——豁免要留痕，见
#: `regime_exempt` 的存在理由：合并成「适用」之后「靠人放过去的」就统计不出来了）。
WITH_EXEMPT = {
    (None, RANGE): (False, CODE_EXEMPT),
    (STATE_FIT, RANGE): (False, CODE_EXEMPT),
    (STATE_UNFIT, RANGE): (False, CODE_EXEMPT),
    (STATE_NEUTRAL, RANGE): (False, CODE_EXEMPT),
    (STATE_UNKNOWN, RANGE): (False, CODE_EXEMPT),
    (STATE_BLANKET, RANGE): (False, CODE_BLANKET),
    (None, HIGH_VOL): (False, CODE_BLANKET),
    (STATE_FIT, HIGH_VOL): (False, CODE_BLANKET),
    (STATE_UNFIT, HIGH_VOL): (False, CODE_BLANKET),
    (STATE_NEUTRAL, HIGH_VOL): (False, CODE_BLANKET),
    (STATE_UNKNOWN, HIGH_VOL): (False, CODE_BLANKET),
    (STATE_BLANKET, HIGH_VOL): (False, CODE_BLANKET),
    (None, None): (False, CODE_EXEMPT),
    (STATE_FIT, None): (False, CODE_EXEMPT),
    (STATE_UNFIT, None): (False, CODE_EXEMPT),
    (STATE_NEUTRAL, None): (False, CODE_EXEMPT),
    (STATE_UNKNOWN, None): (False, CODE_EXEMPT),
    (STATE_BLANKET, None): (False, CODE_BLANKET),
}


@pytest.mark.parametrize("exempt", [False, True], ids=["无豁免", "有豁免"])
@pytest.mark.parametrize("regime", REGIMES, ids=lambda r: str(r))
@pytest.mark.parametrize("cell_state", CELL_STATES, ids=lambda c: str(c))
def test_admission_matrix(cell_state, regime, exempt):
    """「阶段 × 格子 × 豁免」的每一格都逐格钉死 `suspend` 与 `code`。

    表驱动而不是挑几个代表值：这个判据的失效方式是**某一格走错分支**，而走错的那一格
    恰恰是最少被人想起来的那一格（本仓历史上有过 `fit` 漏掉分支 → 行情最好时整条漏斗
    集体停摆的先例）。
    """
    expected = (WITH_EXEMPT if exempt else WITHOUT_EXEMPT)[(cell_state, regime)]
    verdict = admission(regime, cell_state, exempt=exempt)
    assert (verdict.suspend, verdict.code) == expected, (cell_state, regime, exempt)


def test_a_blanket_phase_is_not_opened_by_an_exemption():
    """豁免**不穿透保命档**：豁免排在格子之前，但排在保命档之后。

    豁免是人的动作，它能覆盖「这一格算出来不适用」，不能覆盖「全场该停」。同一句话在
    停机链那侧早已成立（`deactivation_run.exemption_standing`：人工恢复豁免不穿透保命
    档），本判据只是不给自己开一个例外。用具体取值钉住，而不是靠读代码相信它。
    """
    for cell_state in CELL_STATES:
        assert admission(HIGH_VOL, cell_state, exempt=True) == admission(
            HIGH_VOL, cell_state
        ), cell_state

    # 对照：同一条豁免放在普通阶段上，除「格子自己就是保命档」外一律放行且留痕。
    for cell_state in [None, STATE_FIT, STATE_UNFIT, STATE_NEUTRAL, STATE_UNKNOWN]:
        verdict = admission(RANGE, cell_state, exempt=True)
        assert (verdict.suspend, verdict.code) == (False, CODE_EXEMPT), cell_state


def test_a_high_vol_phase_is_blanket_even_when_the_cell_says_unknown():
    """保命档按**阶段**判，不只按格子判——`unknown` 格子遮不住它。

    这个角落真实存在：`slice._cell` 先判 `regime_days == 0`（那个阶段在本池化窗口内
    一天都没出现过），再判 `HIGH_VOL`。所以「当前是高波动、但窗口里一个高波日都没有」
    时格子是 `unknown` 而不是 `blanket`。只看格子会把一次保命档读成「判不了」并因此
    暂停开新仓，而保命档**会自己解除**、这一侧不会——候选在保命档期间原地长死，等波动
    过去也没人推它。
    """
    for cell_state in CELL_STATES:
        verdict = admission(HIGH_VOL, cell_state)
        assert (verdict.suspend, verdict.code) == (False, CODE_BLANKET), cell_state

    # 对照：非高波动阶段 + 同一个 `unknown` 格子 → 照常 fail-closed（拦下、判不了）。
    assert admission(RANGE, STATE_UNKNOWN) == admission(RANGE, None)
    assert admission(RANGE, STATE_UNKNOWN).code == CODE_NO_EVIDENCE


def test_every_regime_other_than_high_vol_is_not_a_blanket_phase():
    """阶段那半条判据与 `deactivation.is_blanket` **同源**，没有第二份词表。

    本判据直接调 `is_blanket`（而不是自己写一个 `regime == "high_vol"`）：抄一份字面量
    就多了一处会与新阶段取值分家的地方。这条用例钉的是那个关系在取值上真的成立——
    枚举里的每个阶段都过一遍，只有保命档那一个不吃格子结论。
    """
    for regime in BaseRegime:
        expected = (
            (False, CODE_BLANKET) if is_blanket(regime.value) else (True, CODE_UNFIT)
        )
        verdict = admission(regime.value, STATE_UNFIT)
        assert (verdict.suspend, verdict.code) == expected, regime


def test_codes_partition_the_outcomes_into_two_kinds():
    """五个 `code` 是「两种拦 + 三种不拦」，不是一个布尔。

    为什么 `suspend` 之外还要 `code`：`suspend=False` 的三种情形在调用方要分开处理——
    `fit` 让调用方照原路径前进、`blanket` 同样前进但语义是「保命档不发表意见」、
    `exempt` 得**留痕**（否则「有多少候选是靠人放过去的」统计不出来）。合成一个布尔，
    候选侧就再也分不出「要不要写一条豁免记录」。
    """
    outcomes = {
        admission(RANGE, STATE_FIT): (False, CODE_FIT),
        admission(None, STATE_BLANKET): (False, CODE_BLANKET),
        admission(RANGE, STATE_UNFIT, exempt=True): (False, CODE_EXEMPT),
        admission(RANGE, STATE_UNFIT): (True, CODE_UNFIT),
        admission(RANGE, STATE_NEUTRAL): (True, CODE_NO_EVIDENCE),
    }
    for verdict, expected in outcomes.items():
        assert (verdict.suspend, verdict.code) == expected
    codes = {verdict.code for verdict in outcomes}
    assert len(codes) == 5, "五个 code 必须互不重叠"
    assert {v.code for v in outcomes if v.suspend} == {CODE_UNFIT, CODE_NO_EVIDENCE}


def test_the_candidate_funnel_does_not_keep_a_second_copy_of_the_criterion():
    """候选漏斗侧的落点必须与 `admission` 的 `code` 一一对应（#51 的哨兵）。

    这是「禁止两份判据」在测试层的守卫：`regime_verdict` 现在只把 `admission` 的结论
    接回候选的状态机，所以对同一组取值，两边的结论必须能对上——

    * `fit` / `blanket`（不发表意见）→ `None`，调用方照原有结论前进；
    * `unfit` → 停 `regime_hold` + `regime_unfit`；
    * `no_evidence` → 停 `regime_hold` + `regime_no_evidence`。

    豁免那一支刻意不在这张表里（它要构造 `(策略 id, 阶段)` 的键，另有
    `apps/strategy_factory/tests/test_evaluate.py` 那 18 例覆盖）：这里钉的是判据本身
    没有分家，而不是把候选侧的用例抄一遍。
    """
    from apps.strategy_factory.evaluate import (
        ReasonCode,
        RegimePoolCellEvidence,
        regime_verdict,
    )
    from apps.strategy_factory.status import StrategyCandidateStatus

    for cell_state in CELL_STATES:
        for regime in REGIMES:
            cell = (
                None if cell_state is None else RegimePoolCellEvidence(state=cell_state)
            )
            verdict = admission(regime, cell_state)
            funnel = regime_verdict("strat-1", regime, cell, {})

            if verdict.code in (CODE_FIT, CODE_BLANKET):
                assert funnel is None, (cell_state, regime, verdict.code)
                continue

            assert funnel is not None, (cell_state, regime, verdict.code)
            assert funnel.next_status is StrategyCandidateStatus.REGIME_HOLD
            assert funnel.reason_code is (
                ReasonCode.REGIME_UNFIT
                if verdict.code == CODE_UNFIT
                else ReasonCode.REGIME_NO_EVIDENCE
            ), (cell_state, regime, verdict.code)


def test_admission_exposes_no_codes_beyond_the_five():
    """词表就是上面那五个——多一个少一个都会让调用方的分派漏一格。

    两个消费者各自按 `code` 分派（候选侧落 `None` / 留痕放行 / `regime_hold`，halt 侧
    落它自己的触发源），所以这里把集合钉成字面量：将来加第六个 `code` 时本文件会红，
    逼着改的人先想清「两个消费者各自该怎么处理它」。
    """
    from apps.regime import admission as admission_module

    codes = {
        value
        for name, value in vars(admission_module).items()
        if name.startswith("CODE_")
    }
    assert codes == {CODE_BLANKET, CODE_EXEMPT, CODE_FIT, CODE_UNFIT, CODE_NO_EVIDENCE}
