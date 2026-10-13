"""手动熔断：``HaltTrigger.MANUAL`` 声明的写、读、解除（ADR 0001 兑现记录）。

另一份文档（``docs/adr/0001-unified-halt-state-machine.md`` 的「兑现记录」）说了「为什么
落点是声明行而不是 Redis、也不接回 pre_trade_check」；这里钉的是那个结论的边界：

1. **写**：trip 落一行 `MANUAL` 声明，留痕齐全（作用域 / 起点 / 到期 / 原因 / 触发方）；
   同一作用域再次 trip 是**原地改写**，不是关一行再开——唯一键（`uniq_live_halt_declaration`）
   与假历史两条理由。缺原因 / 缺触发方 / 写法不认识的 scope 一律当场拒（写入方的门槛；
   靠求值层的 fail-closed 兜底，表现会是「想拦品种却拦了全场」）。
2. **读**：声明活着就该在停止判定里有读者——`halt.block_reason` 命中，且**不需要任何
   机制开关**（`HALT_TRIGGER_SWITCH[MANUAL] = None`，人已确认过的决定不再被开关否决；
   这也是它与开关还停在 Shadow 的事件熔断行最不一样的地方）。
3. **解除**：`lift` 写 `closed_at` + 原因码，不删行（「这条声明存在过」要留得住）；没有
   活行时照实回 `None`。
4. **对账不碰它**：`halt_sync` 的 `_OWNED_TRIGGERS` 不含 `MANUAL`——跑一轮对账后手动行
   必须原样活着，否则「把手动的行交给 300 秒任务自动解除」就回来了。

用 `TestCase`（真库）与 `test_halt.py` 同一理由：这套机制唯一的性质是「状态在表里、
扛过进程重启」。
"""

from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone as dt_timezone

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from apps.regime import events, halt, halt_sync, manual_halt
from apps.regime.halt_notify import _close_reason_display, _period_line
from apps.regime.models import ActorKind, HaltDeclaration, HaltTrigger

NOW = datetime(2026, 10, 12, 0, 0, tzinfo=dt_timezone.utc)
SYMBOL = "DOGE/USDT"
STRATEGY_ID = "11111111-1111-1111-1111-111111111111"


def _trip(
    scope: str,
    reason: str = "连环假信号，人为叫停",
    *,
    hours: int | None = manual_halt.DEFAULT_TRIP_HOURS,
    actor_name: str = ".ops",
    now: datetime | None = NOW,
) -> HaltDeclaration:
    return manual_halt.trip(
        scope,
        reason,
        actor_kind=ActorKind.CLI,
        actor_name=actor_name,
        hours=hours,
        now=now,
    ).row


class TestScopeResolves(TestCase):
    """resolve_scope：三档归位；写歪的当场拒（写入端的门槛）。"""

    def test_the_three_scopes_round_trip(self):
        assert halt.parse_scope(manual_halt.resolve_scope("global")) == ("global", None)
        assert halt.parse_scope(manual_halt.resolve_scope("symbol", SYMBOL)) == (
            "symbol",
            SYMBOL,
        )
        assert halt.parse_scope(manual_halt.resolve_scope("strategy", STRATEGY_ID)) == (
            "strategy",
            STRATEGY_ID,
        )

    def test_empty_values_are_rejected_not_silently_global(self):
        # 想拦的是一个品种，值漏了就该拒——退成 global 是拦错全场。
        with self.assertRaises(manual_halt.ManualHaltError):
            manual_halt.resolve_scope("symbol", "   ")
        with self.assertRaises(manual_halt.ManualHaltError):
            manual_halt.resolve_scope("strategy", "")
        with self.assertRaises(manual_halt.ManualHaltError):
            manual_halt.resolve_scope("strategy", "not-a-uuid")
        with self.assertRaises(manual_halt.ManualHaltError):
            manual_halt.resolve_scope("market")  # 认不出的档

    def test_an_off_spelling_scope_is_rejected_at_the_write_side(self):
        # 求值层把读不懂的写法 fail-closed 当 global（吵闹但方向错）；写入端当场炸。
        # 「symobl」不是 typo 兜底的对象：挡在 trip 的第一道检查里。
        with self.assertRaises(manual_halt.ManualHaltError):
            manual_halt.trip(
                "symobl:DOGE/USDT",
                "原因",
                actor_kind=ActorKind.CLI,
                actor_name="ops",
                now=NOW,
            )


class TestTrip(TestCase):
    def test_trip_writes_a_fully_attributed_row(self):
        row = _trip(halt.symbol_scope(SYMBOL))
        assert HaltTrigger(row.trigger) is HaltTrigger.MANUAL
        assert row.label == HaltTrigger.MANUAL.display
        assert row.opened_at == NOW
        assert row.expires_at == NOW + timedelta(hours=24)
        assert row.reason == "连环假信号，人为叫停"
        assert row.actor_kind == ActorKind.CLI.value
        assert row.actor_name == ".ops"
        assert row.closed_at is None

    def test_hours_zero_means_until_manual_lift(self):
        row = _trip(halt.global_scope(), hours=0)
        assert row.expires_at is None

    def test_reason_and_actor_are_required(self):
        with self.assertRaises(manual_halt.ManualHaltError):
            manual_halt.trip(
                halt.global_scope(), "  ", actor_kind=ActorKind.CLI, actor_name="ops"
            )
        with self.assertRaises(manual_halt.ManualHaltError):
            manual_halt.trip(
                halt.global_scope(), "原因", actor_kind=ActorKind.CLI, actor_name="  "
            )

    def test_a_manual_row_blocks_without_any_switch(self):
        # 关键差异：事件熔断的行要等开关出 Shadow 才作数，手动行不需要任何开关——
        # 人当场确认过的决定，开关没资格否决（HALT_TRIGGER_SWITCH[MANUAL] = None）。
        _trip(halt.global_scope())
        assert "手动熔断" in halt.block_reason(SYMBOL, now=NOW)

    def test_symbol_scope_blocks_only_that_symbol(self):
        _trip(halt.symbol_scope(SYMBOL))
        assert halt.block_reason(SYMBOL, now=NOW)
        assert halt.block_reason("BTC/USDT", now=NOW) == ""

    def test_strategy_scope_needs_the_strategy_id(self):
        _trip(halt.strategy_scope(STRATEGY_ID))
        # 调用方没给 strategy_id：不认它生效（Q3 的策略档判据）。
        assert halt.block_reason(SYMBOL, now=NOW) == ""
        assert halt.block_reason(SYMBOL, strategy_id=STRATEGY_ID, now=NOW)

    def test_expiry_is_inclusive_open(self):
        # opened_at 闭、expires_at 开——与 test_halt.py 钉的是同一条边界口径。
        _trip(halt.global_scope(), hours=1)
        assert halt.block_reason(SYMBOL, now=NOW)
        assert halt.block_reason(SYMBOL, now=NOW + timedelta(hours=1)) == ""

    def test_retrip_rewrites_in_place(self):
        first = _trip(halt.symbol_scope(SYMBOL))
        later = NOW + timedelta(hours=1)
        second = manual_halt.trip(
            halt.symbol_scope(SYMBOL),
            "换一个原因",
            actor_kind=ActorKind.CLI,
            actor_name=".ops2",
            hours=24,
            now=later,
        )
        assert second.row.pk == first.pk
        assert second.rewritten is True
        first.refresh_from_db()
        assert first.reason == "换一个原因"
        assert first.opened_at == later
        assert first.actor_name == ".ops2"
        assert HaltDeclaration.objects.count() == 1

    def test_different_scopes_are_separate_rows(self):
        _trip(halt.symbol_scope(SYMBOL))
        _trip(halt.symbol_scope("BTC/USDT"))
        assert HaltDeclaration.objects.count() == 2


class TestLift(TestCase):
    def test_lift_closes_without_deleting(self):
        row = _trip(halt.symbol_scope(SYMBOL))
        lifted_at = NOW + timedelta(hours=2)
        lifted = manual_halt.lift(
            halt.symbol_scope(SYMBOL),
            actor_kind=ActorKind.CLI,
            actor_name=".ops2",
            now=lifted_at,
        )
        assert lifted is not None
        assert lifted.pk == row.pk
        lifted.refresh_from_db()
        assert lifted.closed_at == lifted_at
        assert lifted.closed_reason == manual_halt.CLOSE_REASON_LIFTED
        assert HaltDeclaration.objects.count() == 1  # 行还在：历史留得住

    def test_a_lifted_row_no_longer_blocks(self):
        _trip(halt.global_scope())
        manual_halt.lift(
            halt.global_scope(), actor_kind=ActorKind.CLI, actor_name=".ops2", now=NOW
        )
        assert halt.block_reason(SYMBOL) == ""

    def test_lift_with_nothing_live_says_so(self):
        result = manual_halt.lift(
            halt.global_scope(), actor_kind=ActorKind.CLI, actor_name=".ops2", now=NOW
        )
        assert result is None

    def test_a_lifted_scope_can_be_retripped_as_a_new_row(self):
        first = _trip(halt.symbol_scope(SYMBOL))
        manual_halt.lift(
            halt.symbol_scope(SYMBOL), actor_kind=ActorKind.CLI, actor_name=".ops2", now=NOW
        )
        again = manual_halt.trip(
            halt.symbol_scope(SYMBOL),
            "又出事了",
            actor_kind=ActorKind.CLI,
            actor_name=".ops2",
            hours=24,
            now=NOW + timedelta(hours=1),
        )
        # 解除后是**新的一行**，不是改写已解除的那行——两个 pk、各自完整的历史。
        assert again.rewritten is False
        again.row.refresh_from_db()
        assert again.row.closed_at is None
        assert again.row.pk != first.pk


class TestSyncNeverTouchesManualRows(TestCase):
    """手动行**不归** 300 秒对账管——自动解除的存在就是给「没人记得清了」开门。"""

    def test_a_manual_row_survives_a_sync_round(self):
        _trip(halt.global_scope(), hours=0)  # 连到期都没有，更没得解除理由可算
        summary = halt_sync.sync(now=NOW)
        row = HaltDeclaration.objects.get(closed_at=None)
        assert HaltTrigger(row.trigger) is HaltTrigger.MANUAL
        # 对账自己什么都不该写：没有更新，更没有解除。
        assert summary["closed"] == 0
        assert summary["updated"] == 0

    def test_manual_is_deliberately_not_owned(self):
        assert HaltTrigger.MANUAL not in halt_sync._OWNED_TRIGGERS


class TestNotificationWording(TestCase):
    """手动行的通知句式与解除原因——与其它档共用同一个渲染通路，句式各归各。"""

    def test_period_line_says_until_manual_lift_without_expiry(self):
        row = _trip(halt.symbol_scope(SYMBOL), hours=0)
        assert "到人工解除为止" in _period_line(row)

    def test_period_line_says_both_ends_with_expiry(self):
        row = _trip(halt.symbol_scope(SYMBOL), hours=24)
        line = _period_line(row)
        assert "→" in line
        assert events.format_moment(row.expires_at) in line

    def test_close_reason_routes_to_the_manual_table(self):
        _trip(halt.symbol_scope(SYMBOL))
        manual_halt.lift(
            halt.symbol_scope(SYMBOL), actor_kind=ActorKind.CLI, actor_name=".ops2", now=NOW
        )
        row = HaltDeclaration.objects.get(closed_reason__isnull=False)
        assert _close_reason_display(row) == "人工解除"


class TestOverview(TestCase):
    def test_overview_lists_rows_with_in_force_flag(self):
        live = _trip(halt.symbol_scope(SYMBOL))
        expired = _trip(halt.symbol_scope("BTC/USDT"), hours=1)
        closed = _trip(halt.symbol_scope("ETH/USDT"))
        manual_halt.lift(
            halt.symbol_scope("ETH/USDT"),
            actor_kind=ActorKind.CLI,
            actor_name=".ops2",
            now=NOW,
        )

        views = manual_halt.overview(now=NOW + timedelta(hours=2))
        by_id = {view.row.pk: view for view in views}
        assert by_id[live.pk].in_force is True
        # 已到点失效，但仍列着——「按过、没解除」要看得见。
        assert by_id[expired.pk].in_force is False
        assert closed.pk not in by_id  # 解除过的行不进概览


class TestCommand(TestCase):
    """管理命令：动作走 `manual_halt` 同一写点；跑完动作总是把当前状态打一遍。"""

    def test_trip_command(self):
        out = io.StringIO()
        call_command(
            "manual_halt",
            "--trip",
            "--symbol",
            SYMBOL,
            "--reason",
            "人为叫停",
            "--actor",
            "张三",
            stdout=out,
        )
        text = out.getvalue()
        assert "已落下手动熔断" in text and "品种 DOGE/USDT" in text
        row = HaltDeclaration.objects.get(closed_at=None)
        assert HaltTrigger(row.trigger) is HaltTrigger.MANUAL
        assert row.actor_name == "张三"

    def test_retrip_command_says_rewritten(self):
        _trip(halt.symbol_scope(SYMBOL))
        out = io.StringIO()
        call_command(
            "manual_halt",
            "--trip",
            "--symbol",
            SYMBOL,
            "--reason",
            "再叫停一次",
            stdout=out,
        )
        assert "已改写" in out.getvalue()
        assert HaltDeclaration.objects.count() == 1

    def test_trip_without_reason_is_a_reply_not_a_crash(self):
        # 「你给的东西不对」以 CommandError 收场（退出码非零，但说的是用法，不是栈）。
        with self.assertRaises(CommandError):
            call_command("manual_halt", "--trip", stdout=io.StringIO())

    def test_lift_command(self):
        _trip(halt.symbol_scope(SYMBOL))
        out = io.StringIO()
        call_command(
            "manual_halt",
            "--lift",
            "--symbol",
            SYMBOL,
            "--actor",
            "张三",
            stdout=out,
        )
        assert "已解除" in out.getvalue()
        row = HaltDeclaration.objects.get(closed_reason__isnull=False)
        assert row.closed_reason == manual_halt.CLOSE_REASON_LIFTED

    def test_lift_with_nothing_live_says_so(self):
        out = io.StringIO()
        call_command("manual_halt", "--lift", stdout=out)
        assert "没有活着的手动熔断可解除" in out.getvalue()

    def test_bare_command_is_read_only(self):
        _trip(halt.global_scope())
        out = io.StringIO()
        call_command("manual_halt", stdout=out)
        assert "只读" in out.getvalue()
        row = HaltDeclaration.objects.get(closed_at=None)
        assert row.closed_at is None
