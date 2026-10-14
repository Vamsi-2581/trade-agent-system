"""mode 与 ExchangeAccount.testnet 的自洽不变式（第①段强制前置）。

不变式：`paper ⇒ testnet=True`、`live ⇒ testnet=False`，不一致则**拒绝启动**。

它要修的是「字段名承诺隔离、实现不隔离」这一类**静默**失效：用户以为在演练、
实际在动钱。注意这不是新增能力，只是把静默错配变成一次可见的失败——所以
`promote` 出来的会话（mode=live + 从 paper 会话继承来的 testnet 账户）在这里
会被如实拒绝，那正是这条不变式存在的意义。

文件后半段（`TestStartAndResumeEchoTheHaltLayers`）钉的是同一对视图的另一件事：
**响应里那段熔断回显**（第②段 ②f 补的 CONTEXT.md 第 172 条）。两件事共用这套夹具
是因为它们都只在「启动/resume 这一步」可观测，而这一步要打桩整个框架才能走通。
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone
from rest_framework_simplejwt.tokens import AccessToken

from apps.agent.frame_manager import FrameManager
from apps.backtest.models import BacktestResult
from apps.exchange.models import ExchangeAccount
from apps.regime import halt
from apps.regime.models import (
    ActorKind,
    HaltDeclaration,
    HaltTrigger,
    MechanismKind,
    MechanismMode,
    RegimeMechanismSwitch,
)
from apps.trading.models import LiveSession, Strategy

User = get_user_model()


def _user(username: str):
    return User.objects.create_user(
        username=username, email=f"{username}@example.com", password="pw12345"
    )


def _account(label: str, testnet: bool):
    return ExchangeAccount.objects.create(
        exchange="binance",
        label=label,
        api_key_enc=b"enc",
        api_secret_enc=b"enc",
        testnet=testnet,
    )


def _session(user, mode: str, testnet: bool, status: str = "pending"):
    strategy = Strategy.objects.create(
        name=f"s-{mode}-{testnet}-{status}", code_path="strategies/s.py"
    )
    backtest = BacktestResult.objects.create(
        strategy=strategy,
        user=user,
        symbol="BTC/USDT",
        timeframe="4h",
        start_date=datetime.date(2026, 1, 1),
        end_date=datetime.date(2026, 2, 1),
        initial_capital=Decimal("1000"),
        final_capital=Decimal("1100"),
        total_return_pct=10.0,
        review_status="approved",
    )
    return LiveSession.objects.create(
        user=user,
        backtest_result=backtest,
        strategy=strategy,
        symbol="BTC/USDT",
        mode=mode,
        status=status,
        exchange_account=_account(f"{mode}-{testnet}", testnet),
        initial_capital=Decimal("1000"),
        current_equity=Decimal("1000"),
        config={},
    )


def _idle_frame_manager():
    """框架全部打桩：本文件只验「准入」这一步，不验框架起没起来。"""
    fm = MagicMock()
    fm.start_trading_frame = AsyncMock()
    fm.start_strategy_runner = AsyncMock()
    fm.stop_strategy_runner = AsyncMock()
    fm.start_live_session = AsyncMock()
    return fm


def _post(user, session, action: str):
    client = Client()
    token = AccessToken.for_user(user)
    with patch.object(
        FrameManager, "get_instance", classmethod(lambda cls: _idle_frame_manager())
    ):
        return client.post(
            f"/api/trading/sessions/{session.id}/{action}/",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )


@pytest.mark.django_db
class TestModeAccountInvariant:
    def test_start_rejects_paper_session_on_live_account(self):
        """paper 会话绑了实盘账户 → 拒绝启动（否则「演练」在动真钱）"""
        session = _session(_user("mismatch-paper"), "paper", testnet=False)

        resp = _post(session.user, session, "start")

        assert resp.status_code == 400, resp.content
        assert "testnet" in resp.json()["error"]
        session.refresh_from_db()
        assert session.status == "pending", "拒绝启动时不得改动会话状态"

    def test_start_rejects_live_session_on_testnet_account(self):
        """live 会话绑了测试网账户 → 拒绝启动（promote 的产物正是这一形状）"""
        session = _session(_user("mismatch-live"), "live", testnet=True)

        resp = _post(session.user, session, "start")

        assert resp.status_code == 400, resp.content
        assert "testnet" in resp.json()["error"]

    def test_start_rejects_unknown_mode(self):
        """mode 是自由字符串，未知值不得被当成 live（那是更激进的一侧）"""
        session = _session(_user("mismatch-unknown"), "paper", testnet=True)
        LiveSession.objects.filter(pk=session.pk).update(mode="foo")
        session.refresh_from_db()

        resp = _post(session.user, session, "start")

        assert resp.status_code == 400, resp.content
        assert "foo" in resp.json()["error"]

    def test_start_allows_consistent_paper_session(self):
        """自洽时不受影响：paper + testnet 账户照常启动"""
        session = _session(_user("consistent-paper"), "paper", testnet=True)

        resp = _post(session.user, session, "start")

        assert resp.status_code == 200, resp.content
        session.refresh_from_db()
        assert session.status == "running"

    def test_resume_rejects_mismatch_and_keeps_db_status(self):
        """恢复也是一次启动：不能从 resume 口绕开同一不变式"""
        session = _session(
            _user("mismatch-resume"), "paper", testnet=False, status="paused"
        )

        resp = _post(session.user, session, "resume")

        assert resp.status_code == 400, resp.content
        session.refresh_from_db()
        assert session.status == "paused"

    def test_resume_allows_consistent_session(self):
        session = _session(
            _user("consistent-resume"), "paper", testnet=True, status="paused"
        )

        resp = _post(session.user, session, "resume")

        assert resp.status_code == 200, resp.content

    def test_session_without_account_is_rejected(self):
        """没有账户就无从谈一致性；start 早就有这条前置，resume 也该有"""
        session = _session(_user("no-account"), "paper", testnet=True)
        LiveSession.objects.filter(pk=session.pk).update(exchange_account=None)
        session.refresh_from_db()

        assert session.mode_account_mismatch() is not None


def _promote(user, session):
    client = Client()
    token = AccessToken.for_user(user)
    return client.post(
        f"/api/trading/sessions/{session.id}/promote/",
        HTTP_AUTHORIZATION=f"Bearer {token}",
    )


@pytest.mark.django_db
class TestPromoteProducesConsistentSession:
    """promote 原样继承源账户，源会话是 paper ⇒ 账户必然 testnet=True ⇒ 产物是
    一支 mode=live + testnet=True 的会话，启动时必被上面的校验拒绝。不能一边返回
    201「Promoted to live trading session」一边交出一支永远起不来的会话。"""

    def test_promote_rejects_testnet_account_and_leaves_source_untouched(self):
        user = _user("promote-testnet")
        session = _session(user, "paper", testnet=True, status="running")

        resp = _promote(user, session)

        assert resp.status_code == 400, resp.content
        assert "testnet" in resp.json()["error"]
        session.refresh_from_db()
        assert session.status == "running", "拒绝时必须先于停止源会话"
        assert not LiveSession.objects.filter(mode="live").exists()

    def test_promote_rejects_session_without_account(self):
        user = _user("promote-no-account")
        session = _session(user, "paper", testnet=True, status="stopped")
        LiveSession.objects.filter(pk=session.pk).update(exchange_account=None)

        resp = _promote(user, session)

        assert resp.status_code == 400, resp.content
        assert not LiveSession.objects.filter(mode="live").exists()

    def test_promote_checks_the_account_not_the_mode(self):
        """只对 testnet 拒绝，不是无差别封路：绑了实盘账户时 promote 照常产出。

        这个 paper + testnet=False 的源会话是直接写库造出来的（绕过启动校验），
        用来钉住「拒绝的唯一依据是账户属性」。
        """
        from apps.exchange.models import ExchangeAccount

        user = _user("promote-live-account")
        session = _session(user, "paper", testnet=True, status="stopped")
        live_account = ExchangeAccount.objects.create(
            exchange="binance",
            label="promote-live",
            api_key_enc=b"enc",
            api_secret_enc=b"enc",
            testnet=False,
        )
        LiveSession.objects.filter(pk=session.pk).update(exchange_account=live_account)

        resp = _promote(user, session)

        assert resp.status_code == 201, resp.content
        promoted = LiveSession.objects.get(mode="live")
        assert promoted.mode_account_mismatch() is None


# --------------------------------------------------------------------------- #
# 启动/恢复那一刻的熔断回显（CONTEXT.md 第 172 条）
# --------------------------------------------------------------------------- #


def _declare(*, trigger=HaltTrigger.EVENT, scope: str | None = None, label="FOMC 议息"):
    """投一条此刻正在生效的声明。

    `opened_at` 取**墙上时钟**而不是钉死的常量：这两个视图走的是真实调用（不注入
    `now`），钉死一个绝对时刻的话，用例会随着日子过去而悄悄变成「声明还没生效」——
    而那时的表现是回显为空、断言失败，看起来像实现坏了。
    """
    return HaltDeclaration.objects.create(
        trigger=trigger.value,
        scope=scope or halt.global_scope(),
        label=label,
        opened_at=timezone.now() - datetime.timedelta(hours=2),
        expires_at=None,
        reason="高影响事件窗口",
        actor_kind=ActorKind.TASK.value,
        actor_name="regime.sync_halt_windows",
    )


def _open(kind: MechanismKind):
    return RegimeMechanismSwitch.objects.create(
        kind=kind.value,
        from_mode=MechanismMode.SHADOW.value,
        to_mode=MechanismMode.EXECUTING.value,
        at=timezone.now() - datetime.timedelta(days=1),
        actor_kind=ActorKind.CLI.value,
        actor_name="ops",
        reason="测试",
    )


@pytest.mark.django_db
class TestStartAndResumeEchoTheHaltLayers:
    """「被 halt 拦下的动作必须回显是哪个触发源在拦，且回显落在用户发起动作的那一刻」。

    这两个视图是**唯一**的这样的时刻：会话的订单要到 K 线到达之后才产生，那时没人正在
    看屏幕。所以这里的断言全是关于「响应里多/少了哪段字」——它坏掉时不会红任何别的
    东西，只会让用户在某个深夜发现「它说会拦，但单还是出去了」。
    """

    def test_start_says_nothing_when_nothing_hits(self):
        """字段**恒在**，值为空串。恒在的话客户端不用去区分「没有层在拦」与服务端
        没这个字段——后者会把一次静默的裁剪读成一次未实现的回显。"""
        session = _session(_user("notice-clean"), "paper", testnet=True)

        resp = _post(session.user, session, "start")

        assert resp.status_code == 200, resp.content
        assert "notice" in resp.json()
        assert resp.json()["notice"] == ""

    def test_start_reports_the_layer_that_blocks_the_session(self):
        """命中就报出触发源；同时**启动本身照常成功**。

        「不阻止启动」是这一段话跟着 200 回来的前提：存量仓位的止损保护要靠这一步上线，
        所以 block 掉启动会为了拦住开仓而拿掉保命的那一半。会话状态断言与回显断言写在
        一起，是因为它们互为条件——只断前者的话，一个从不回显的实现也能全绿。
        """
        _open(MechanismKind.EVENT_BREAKER)
        _declare(label="FOMC 议息")
        session = _session(_user("notice-blocked"), "paper", testnet=True)

        resp = _post(session.user, session, "start")

        assert resp.status_code == 200, resp.content
        notice = resp.json()["notice"]
        assert "FOMC 议息" in notice
        assert HaltTrigger.EVENT.display in notice
        session.refresh_from_db()
        assert session.status == "running"

    def test_start_says_nothing_for_a_layer_scoped_to_another_symbol(self):
        """裁剪按作用域命中，**不按入口一刀切**：别的品种的窗口不该出现在这张会话的
        启动回显里（第 172 条点名的 SOL/BTC 那个例子）。"""
        _open(MechanismKind.EVENT_BREAKER)
        _declare(scope=halt.symbol_scope("DOGE/USDT"), label="DOGE 解锁")
        session = _session(_user("notice-other-symbol"), "paper", testnet=True)

        resp = _post(session.user, session, "start")

        assert resp.status_code == 200, resp.content
        assert resp.json()["notice"] == ""

    def test_start_carries_the_strategy_id_of_the_session(self):
        """视图必须把**这张会话的策略 id** 传下去。

        第②段没有任何写入方会投策略档的声明，所以这里手工造一条：被测的是那个实参
        （`str(session.strategy_id)`）有没有真的传下去。传丢了的表现是策略档的停用决策
        用户在启动时永远看不到——而第③段正是靠这一档停掉表现不佳的策略。
        """
        _open(MechanismKind.REGIME_GATE)
        session = _session(_user("notice-strategy-id"), "paper", testnet=True)
        _declare(
            trigger=HaltTrigger.DEACTIVATION,
            scope=halt.strategy_scope(session.strategy_id),
            label="策略 A 停用",
        )

        resp = _post(session.user, session, "start")

        assert resp.status_code == 200, resp.content
        assert "策略 A 停用" in resp.json()["notice"]

    def test_start_says_nothing_for_another_strategys_layer(self):
        """对照组：别人家策略的停用不该在这张会话上出声。"""
        _open(MechanismKind.REGIME_GATE)
        _declare(
            trigger=HaltTrigger.DEACTIVATION,
            scope=halt.strategy_scope("00000000-0000-0000-0000-000000000000"),
            label="别的策略停用",
        )
        session = _session(_user("notice-other-strategy"), "paper", testnet=True)

        resp = _post(session.user, session, "start")

        assert resp.status_code == 200, resp.content
        assert resp.json()["notice"] == ""

    def test_resume_carries_the_same_notice(self):
        """恢复是**同一个时刻的同一件事**：暂停过的会话从这里重新开始开仓。

        漏掉这一处，用户只要「暂停再恢复」就再也看不到那条回显了——而恢复在实践中
        恰恰是「我刚把策略放回来」的那一下。
        """
        _open(MechanismKind.EVENT_BREAKER)
        _declare(label="FOMC 议息")
        session = _session(
            _user("notice-resume"), "paper", testnet=True, status="paused"
        )

        resp = _post(session.user, session, "resume")

        assert resp.status_code == 200, resp.content
        assert "FOMC 议息" in resp.json()["notice"]
