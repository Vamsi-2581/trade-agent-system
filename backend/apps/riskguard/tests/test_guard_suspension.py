"""「按阶段暂停开新仓」声明在**下单通路**上真的生效（issue #51）。

`test_guard_halt_strategy_scope.py` 钉的是**停用决策**那一档；本文件钉它旁边新加的一档：
`HaltTrigger.REGIME_SUSPENSION`。两档的唯一键（触发源 × 作用域）不同、收场也不同（一个要
人工恢复、一个随阶段自动翻回去），但在下单通路上**必须**穿过同一道判定口——这正是「唯一
的停止抽象」的意思：新机制接线的方式是**投声明**，不是在下单咽喉上加第二个 `if`。本文件
就是这条的实证：它一行都不碰 `RiskGuard`，却拦住了单。

五条：

1. **命中就拦开新仓**：`Executing` 档下，一条暂停声明挡住那个策略的开新仓单，且理由里
   带得出作用域。
2. **减仓单不被它拦**：停止判定只查开新仓（CONTEXT.md:47），豁免语义在 guard 内唯一定义。
3. **`Shadow` 档不施加动作**：`HALT_TRIGGER_SWITCH[REGIME_SUSPENSION]` 指向 `REGIME_GATE`
   开关，而「声明与开关都必须为真才作数」——声明在表里、档位在 Shadow，单子照常出去。
   这一条是**机制档位真的接上了**的实证（#50 是把这条漏在候选漏斗侧）。
4. **不泄漏到别的策略**：策略档只拦自己那一个。
5. **与停用声明同时命中时两条理由都在**：`HaltVerdict.reason` 逐层都写——只报一层会让人
   按报出来的那层去排查，而解除它之后单还是下不出去。

`transaction=True` 与「时钟用墙上时间」的两条理由与 `test_guard_halt_strategy_scope.py`
逐条相同（`db_async` 走到另一个线程，未提交的事务它看不见；判定入口不注入时钟，窗口必须
相对真实的「现在」落在过去）。**那两条注释不在这里重写**，读那边即可。

helper 与那个文件同名同形（`_strategy` / `_session` / `_declare` / `_check`）而各自持有一份：
它们描述的是「一块测试地基」，不是「被测对象的一部分」——跨文件 import 测试 helper 会让
改一处地基连带改两个文件的收场，而这两档的假绿灯形态并不相同。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.regime import halt
from apps.regime.models import (
    ActorKind,
    HaltDeclaration,
    HaltTrigger,
    MechanismKind,
    MechanismMode,
    RegimeMechanismSwitch,
)
from apps.riskguard.guard import RiskGuard
from apps.trading.adapters.base import OrderRequest

if TYPE_CHECKING:
    from apps.trading.models import LiveSession, Strategy

pytestmark = pytest.mark.django_db(transaction=True)

SYMBOL = "BTC/USDT"
SUSPENSION_LABEL = "按阶段暂停开仓：趋势跟踪（证据不足）"
DEACTIVATION_LABEL = "策略停用决策：趋势跟踪（下行趋势表现不佳）"


def _strategy(name: str) -> "Strategy":
    from apps.trading.models import Strategy

    return Strategy.objects.create(name=name, code_path=f"strategies/{name}.py")


def _session(strategy: "Strategy") -> "LiveSession":
    from apps.trading.models import LiveSession

    name = f"u-{uuid.uuid4().hex[:8]}"
    return LiveSession.objects.create(
        user=get_user_model().objects.create_user(
            username=name, email=f"{name}@example.com", password="pw12345"
        ),
        strategy=strategy,
        symbol=SYMBOL,
        mode="paper",
        status="running",
        initial_capital=Decimal("1000"),
    )


def _declare(
    strategy: "Strategy", *, trigger: HaltTrigger, label: str
) -> HaltDeclaration:
    """该策略的一条声明。``expires_at=None``：策略档的两路都没有预先知道的截止时刻。"""
    return HaltDeclaration.objects.create(
        trigger=trigger.value,
        scope=halt.strategy_scope(strategy.id),
        label=label,
        opened_at=timezone.now() - timedelta(hours=2),
        expires_at=None,
        reason="行情阶段 gate：当前阶段不适配（测试）",
        actor_kind=ActorKind.TASK.value,
        actor_name="regime.sync_gate",
    )


def _suspension(strategy: "Strategy") -> HaltDeclaration:
    return _declare(
        strategy,
        trigger=HaltTrigger.REGIME_SUSPENSION,
        label=SUSPENSION_LABEL,
    )


def _deactivation(strategy: "Strategy") -> HaltDeclaration:
    return _declare(
        strategy,
        trigger=HaltTrigger.DEACTIVATION,
        label=DEACTIVATION_LABEL,
    )


def _gate(mode: MechanismMode = MechanismMode.EXECUTING) -> RegimeMechanismSwitch:
    """把 `REGIME_GATE` 这个开关拨到 `mode`。**两档共用它**（`HALT_TRIGGER_SWITCH`）。"""
    return RegimeMechanismSwitch.objects.create(
        kind=MechanismKind.REGIME_GATE.value,
        from_mode=MechanismMode.SHADOW.value,
        to_mode=mode.value,
        at=timezone.now() - timedelta(days=1),
        actor_kind=ActorKind.CLI.value,
        actor_name="ops",
        reason="测试",
    )


def _request(*, live_session_id: str | None, reduce_only: bool = False) -> OrderRequest:
    return OrderRequest(
        exchange="binance",
        symbol=SYMBOL,
        order_type="market",
        side="buy",
        quantity=Decimal("0.001"),
        price=None,
        live_session_id=live_session_id,
        reduce_only=reduce_only,
    )


def _check(request: OrderRequest) -> tuple[bool, str]:
    """走**真实**的 `pre_trade_check`。``user_id=None`` 的理由同另一个文件：第 0 步之后
    那五步问的是「这个人还能不能下单」，判空会在第 0 步之后立刻返回。"""
    return asyncio.run(RiskGuard(mode="trading").pre_trade_check(request, None))


class TestASuspensionDeclarationBlocksANewPosition:
    def test_the_session_strategy_is_blocked_while_the_gate_executes(self):
        _gate()
        strategy = _strategy("donchian_atr_trend")
        session = _session(strategy)
        _suspension(strategy)

        approved, reason = _check(_request(live_session_id=str(session.id)))

        assert approved is False
        assert SUSPENSION_LABEL in reason
        # 作用域写进理由：「拦全场」与「只拦某个策略」在排查时是完全不同的两件事。
        assert f"策略 {strategy.id}" in reason

    def test_a_reduce_only_order_is_exempt(self):
        """停止判定只查开新仓：减仓是机制拦着的时候唯一想让它动起来的事。"""
        _gate()
        strategy = _strategy("donchian_atr_trend")
        session = _session(strategy)
        _suspension(strategy)

        approved, reason = _check(
            _request(live_session_id=str(session.id), reduce_only=True)
        )

        assert (approved, reason) == (True, "OK")


class TestTheMechanismTierGatesIt:
    def test_a_shadow_tier_does_not_apply_the_action(self):
        """声明在表里、档位在 Shadow ⇒ 单子照常出去。这一条钉的是「机制档位真的接上了」：
        `REGIME_SUSPENSION` 与 `DEACTIVATION` 共用 `REGIME_GATE` 开关，而不是一个没有开关、
        永远作数的源（那正是保命档那一档的性质，本档不该有）。
        """
        _gate(MechanismMode.SHADOW)
        strategy = _strategy("donchian_atr_trend")
        session = _session(strategy)
        _suspension(strategy)

        approved, reason = _check(_request(live_session_id=str(session.id)))

        assert (approved, reason) == (True, "OK")

    def test_the_row_survives_a_shadow_tier(self):
        """Shadow 只是「不施加动作」，不是「把声明删掉」：行还在表里，回到 Executing 那一刻
        它立刻又作数（不留一段没人解释的空档）。"""
        _gate(MechanismMode.SHADOW)
        strategy = _strategy("donchian_atr_trend")
        _suspension(strategy)

        assert HaltDeclaration.objects.filter(closed_at__isnull=True).count() == 1


class TestItDoesNotLeakToOtherScopes:
    def test_another_strategys_session_is_not_blocked(self):
        _gate()
        suspended = _strategy("donchian_atr_trend")
        other = _strategy("mean_reversion")
        _suspension(suspended)

        approved, reason = _check(_request(live_session_id=str(_session(other).id)))

        assert (approved, reason) == (True, "OK")


class TestBothTiersShowUpInTheReason:
    def test_the_two_reasons_are_both_reported(self):
        """同一个策略同时被两档拦住时，拒绝理由里两条都要有：只报一层会让人按报出来的那层
        去排查，而解除它之后单还是下不出去（那时会以为是别的问题）。
        """
        _gate()
        strategy = _strategy("donchian_atr_trend")
        session = _session(strategy)
        _deactivation(strategy)
        _suspension(strategy)

        approved, reason = _check(_request(live_session_id=str(session.id)))

        assert approved is False
        assert DEACTIVATION_LABEL in reason
        assert SUSPENSION_LABEL in reason
