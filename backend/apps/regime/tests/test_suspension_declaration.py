"""暂停声明这一档的落库与解除（issue #51）。

`test_gate_suspension.py` 钉的是**判定层**（一轮产出什么期望集）；本文件钉**对账**那一半：
期望集 → `HaltDeclaration` 行，以及三条只有真库才测得到的性质（`uniq_live_halt_declaration`
是数据库约束，而对账的全部难处长在它上面）：

1. **新触发源必须被对账认得**。没写进 `_OWNED_TRIGGERS` 的源会被静默跳过——于是「格子回了
   `fit`、声明还在长死」。那条长死的声明拦得住人，所以它一旦发生就是**用户先发现**：
   表里没有别的东西能显示「这一行本该被解除」。
2. **两个 300 秒任务各管各的一组**：`regime-halt-window-sync` 那一轮（不给 `gate_plan`）
   不得碰暂停行。把它们当「计划里没有」，就会在每个 5 分钟里把一条暂停解除再写回来——
   取决于两个任务相差几毫秒，比读代码难得多的多。
3. **漏了一路不会静默解除另一路**。`gate_run` 合成的 plan 总是同时含停用与暂停两路；万一
   某天只递了一路，另一路的活行必须按 fail-closed 原样留着（吵闹但看得见），而不是被当成
   「计划里没有」解除掉。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_timezone

from django.test import TestCase

from apps.regime import gate, halt, halt_sync
from apps.regime.halt_sync import ACTOR_NAME, GATE_ACTOR_NAME
from apps.regime.models import ActorKind, HaltDeclaration, HaltTrigger

#: 「此刻」。与 `test_halt_sync.py` 用同一个时刻，好让两个文件里读到的窗口一眼可比。
NOW = datetime(2026, 9, 23, 4, 0, tzinfo=dt_timezone.utc)


def suspension_declaration(strategy_id) -> gate.Declaration:
    return gate.Declaration(
        strategy_id=strategy_id,
        label=f"{gate.SUSPENSION_LABEL_PREFIX}{strategy_id}",
        reason=f"{strategy_id} 在当前「箱体震荡」阶段没有适用性结论（证据不足）。",
        opened_at=NOW - timedelta(hours=2),
        trigger=gate.TRIGGER_REGIME_SUSPENSION,
    )


def deactivation_declaration(strategy_id) -> gate.Declaration:
    """停用路的一条声明——`trigger` 走默认值，因为那条路逐字未变。"""
    return gate.Declaration(
        strategy_id=strategy_id,
        label=f"{gate.LABEL_PREFIX}{strategy_id}",
        reason=f"{strategy_id} 在当前阶段判为不适用。",
        opened_at=NOW - timedelta(hours=3),
    )


class TestTheSuspensionTierInTheDeclarationTable(TestCase):
    def test_a_suspension_round_writes_the_row_with_its_own_trigger(self):
        """落库的触发源取自声明自带的那一个，而不是写死 `DEACTIVATION`：写死的话，暂停行
        会挂到停用决策的档上——于是「机制决定停用这条策略」的开始时刻、解除路径与人工恢复
        的地基，被一批下个阶段就会自己翻回去的暂停污染。
        """
        plan = gate.GatePlan(declarations=(suspension_declaration("s-1"),))

        summary = halt_sync.sync(now=NOW, gate_plan=plan, actor_name=GATE_ACTOR_NAME)

        self.assertEqual(
            summary, {"created": 1, "updated": 0, "unchanged": 0, "closed": 0}
        )
        row = HaltDeclaration.objects.get()
        self.assertEqual(row.trigger, HaltTrigger.REGIME_SUSPENSION.value)
        self.assertEqual(row.scope, halt.strategy_scope("s-1"))
        self.assertEqual(row.opened_at, NOW - timedelta(hours=2))
        self.assertIsNone(row.expires_at)
        self.assertEqual(row.actor_kind, ActorKind.TASK.value)
        self.assertEqual(row.actor_name, GATE_ACTOR_NAME)
        self.assertNotEqual(GATE_ACTOR_NAME, ACTOR_NAME)

        again = halt_sync.sync(now=NOW, gate_plan=plan, actor_name=GATE_ACTOR_NAME)
        self.assertEqual(
            again, {"created": 0, "updated": 0, "unchanged": 1, "closed": 0}
        )

    def test_the_row_is_released_when_the_cell_goes_fit(self):
        """解除原因取自判定层的 `close_reasons`（`became_fit`）——「重新适配」这句话要能
        进通知正文：只说「解除了」的话，读的人得自己去猜是重算翻了结论、还是有人放开了它。
        """
        halt_sync.sync(
            now=NOW,
            gate_plan=gate.GatePlan(declarations=(suspension_declaration("s-1"),)),
            actor_name=GATE_ACTOR_NAME,
        )

        summary = halt_sync.sync(
            now=NOW,
            gate_plan=gate.GatePlan(close_reasons={"s-1": gate.CLOSE_BECAME_FIT}),
            actor_name=GATE_ACTOR_NAME,
        )

        self.assertEqual(
            summary, {"created": 0, "updated": 0, "unchanged": 0, "closed": 1}
        )
        row = HaltDeclaration.objects.get()
        self.assertIsNotNone(row.closed_at)
        self.assertEqual(row.closed_reason, gate.CLOSE_BECAME_FIT)

    def test_the_window_round_leaves_the_suspension_row_alone(self):
        """`regime-halt-window-sync` 那一轮（不给 `gate_plan`）**不得**碰策略档的行：它算的
        是事件与保命档两组期望，策略档的活行既不是「计划里没有」也不是「该解除」，而是
        「这一轮不归我管」。
        """
        halt_sync.sync(
            now=NOW,
            gate_plan=gate.GatePlan(declarations=(suspension_declaration("s-1"),)),
            actor_name=GATE_ACTOR_NAME,
        )

        halt_sync.sync(now=NOW)

        row = HaltDeclaration.objects.get()
        self.assertIsNone(row.closed_at)
        self.assertEqual(row.trigger, HaltTrigger.REGIME_SUSPENSION.value)

    def test_a_plan_carrying_only_one_tier_does_not_release_the_other(self):
        """只递一路（写入方 bug）时，另一路的活行必须原地留着。**这是「漏了一路」唯一挡着
        静默解除的东西**：一条被误解除的暂停不会自己回来（要等下一轮重建），而它看起来与
        「本来就没声明」一模一样。
        """
        halt_sync.sync(
            now=NOW,
            gate_plan=gate.GatePlan(declarations=(deactivation_declaration("s-1"),)),
            actor_name=GATE_ACTOR_NAME,
        )
        halt_sync.sync(
            now=NOW,
            gate_plan=gate.GatePlan(declarations=(suspension_declaration("s-2"),)),
            actor_name=GATE_ACTOR_NAME,
        )
        self.assertEqual(
            HaltDeclaration.objects.filter(closed_at__isnull=True).count(), 2
        )

        summary = halt_sync.sync(
            now=NOW,
            gate_plan=gate.GatePlan(declarations=(suspension_declaration("s-2"),)),
            actor_name=GATE_ACTOR_NAME,
        )

        self.assertEqual(summary["closed"], 0)
        self.assertEqual(
            HaltDeclaration.objects.filter(closed_at__isnull=True).count(), 2
        )

    def test_both_tiers_can_hold_the_same_strategy_at_once(self):
        """同一个策略可以同时被两条路拦住（唯一键是「触发源 × 作用域」，各占一行）：停用
        决策那条说「机制决定停用它」，暂停那条说「这片行情里它没有资格开新仓」。合成期望集
        时必须两条都写出来，否则其中一条会被当成「计划里没有」。
        """
        plan = gate.GatePlan(
            declarations=(
                deactivation_declaration("s-1"),
                suspension_declaration("s-1"),
            )
        )

        summary = halt_sync.sync(now=NOW, gate_plan=plan, actor_name=GATE_ACTOR_NAME)

        self.assertEqual(summary["created"], 2)
        self.assertEqual(
            sorted(HaltDeclaration.objects.values_list("trigger", flat=True)),
            sorted(
                [
                    HaltTrigger.DEACTIVATION.value,
                    HaltTrigger.REGIME_SUSPENSION.value,
                ]
            ),
        )
        row = HaltDeclaration.objects.get(trigger=HaltTrigger.REGIME_SUSPENSION.value)
        self.assertEqual(row.scope, halt.strategy_scope("s-1"))
