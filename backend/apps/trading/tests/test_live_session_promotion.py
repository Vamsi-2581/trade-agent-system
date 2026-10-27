"""升实盘闸门：目标账户参数 + `live_session_create` 的 paper 前置收紧（#28，spec D11/D12）。

本文件钉三件事，每件都有一条「闸门真的有牙齿」的正向与反向：

1. **promote 不再必然 400**。旧实现把源 paper 账户原样继承 ⇒ 产物必被
   `mode_account_mismatch` 拒绝 ⇒ 这个接口**永远失败**。新实现接受
   `exchange_account_id`（目标实盘账户）。
2. **人工卡点 = 干跑摘要 → 显式确认**（`reduce_position` 那种形状，fail-closed）。
3. **`live_session_create(mode="live")` 的前置**从「存在任意一条 paper 会话」收紧为
   「存在一条**跑满观察期且判定通过**的 paper 会话」。

## 闸门口径的唯一出处

「跑满观察期且判定通过」= `StrategyCandidate.status == "awaiting_live_approval"`，
由 `apps/strategy_factory/live_gate.py` 读。**本文件刻意不去造时间线 / 订单 / 风控事件**
——那些是判定层（`evaluate.py` + #24）的输入，已经由 `apps/strategy_factory/tests/` 逐条
钉过。在这里再造一遍，等于把同一口径测两遍，且第二遍测的是**复制品**。
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from apps.backtest.models import BacktestResult
from apps.exchange.models import ExchangeAccount
from apps.strategy_factory.live_gate import (
    APPROVED_STATUS,
    evaluate_live_gate,
    find_approved_paper_session,
)
from apps.strategy_factory.models import StrategyCandidate
from apps.strategy_factory.status import StrategyCandidateOrigin
from apps.regime.models import HaltTrigger
from apps.regime.tests.test_halt import _declare
from apps.trading import views
from apps.trading.models import LiveSession, Strategy

User = get_user_model()

SUBMIT_URL = "/api/trading/sessions/create/"


class FakeAdapter:
    """假交易所适配器：固定余额，不碰网络。"""

    def __init__(self, api_key, secret, testnet=False):
        self.testnet = testnet
        self.connect = AsyncMock()
        self.disconnect = AsyncMock()
        self.get_balance = AsyncMock(return_value={"USDT": Decimal("2500.50")})


def _user(username: str):
    return User.objects.create_user(
        username=username, email=f"{username}@example.com", password="pw12345"
    )


def _account(label: str, testnet: bool, *, is_active: bool = True):
    """建一个账户。

    凭据用**真的 Fernet 密文**而不是占位字节：建会话要取账户余额，那条路会解密
    `api_key_enc`，占位字节会在解密那一步炸成 502——那时用例失败的原因看起来像
    「余额接口坏了」，而不是「夹具造错了」。
    """
    from cryptography.fernet import Fernet
    from django.conf import settings

    f = Fernet(settings.FERNET_KEY.encode())
    return ExchangeAccount.objects.create(
        exchange="binance",
        label=label,
        api_key_enc=f.encrypt(b"test_key"),
        api_secret_enc=f.encrypt(b"test_secret"),
        testnet=testnet,
        is_active=is_active,
    )


def _backtest(user, *, symbol="BTC/USDT", parameters=None):
    strategy = Strategy.objects.create(
        name=f"s-{user.username}", code_path="strategies/s.py"
    )
    return BacktestResult.objects.create(
        strategy=strategy,
        user=user,
        symbol=symbol,
        timeframe="4h",
        start_date=datetime.date(2026, 1, 1),
        end_date=datetime.date(2026, 2, 1),
        initial_capital=Decimal("1000"),
        final_capital=Decimal("1100"),
        total_return_pct=10.0,
        review_status="approved",
        parameters=parameters or {},
    )


def _paper_session(user, backtest, account, *, status="stopped", **kwargs):
    return LiveSession.objects.create(
        user=user,
        backtest_result=backtest,
        strategy=backtest.strategy,
        symbol=backtest.symbol,
        mode="paper",
        status=status,
        exchange_account=account,
        initial_capital=kwargs.pop("initial_capital", Decimal("2500.50")),
        current_equity=kwargs.pop("current_equity", Decimal("3100.75")),
        config=kwargs.pop("config", {"exit_at_mid": False, "position_pct": 0.2}),
        **kwargs,
    )


def _candidate(session, *, status=APPROVED_STATUS, name="cand", user=None):
    return StrategyCandidate.objects.create(
        name=name,
        origin=StrategyCandidateOrigin.MANUAL.value,
        strategy=session.strategy,
        status=status,
        paper_session=session,
        proposed_by=user or session.user,
    )


def _promote(user, session, *, body=None):
    client = Client()
    token = AccessToken.for_user(user)
    return client.post(
        f"/api/trading/sessions/{session.id}/promote/",
        data=body if body is not None else {},
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {token}",
    )


def _post_create(client, backtest, account, *, mode="live", extra=None):
    body = {
        "backtest_result_id": str(backtest.id),
        "mode": mode,
        "exchange_account_id": str(account.id),
    }
    body.update(extra or {})
    return client.post(SUBMIT_URL, body, format="json")


# --------------------------------------------------------------------------- #
# 1. live_gate：闸门本身（不经过 HTTP）
# --------------------------------------------------------------------------- #


@pytest.mark.django_db
class TestLiveGateReadsTheCandidateStatus:
    """闸门读候选状态，三种「查不出通过」的情形一律拒绝（fail-closed，Q4）。"""

    def test_allowed_when_candidate_awaits_live_approval(self):
        user = _user("gate-ok")
        bt = _backtest(user)
        paper = _paper_session(user, bt, _account("paper", testnet=True))
        cand = _candidate(paper)

        gate = evaluate_live_gate(paper)

        assert gate.allowed is True
        assert gate.reason == ""
        assert gate.candidate.pk == cand.pk
        assert gate.paper_session.pk == paper.pk

    @pytest.mark.parametrize(
        "status",
        [
            "registered",
            "in_sample_running",
            "in_sample_failed",
            "oos_running",
            "oos_failed",
            "paper_pending",
            "paper_running",
            "paper_failed",
        ],
    )
    def test_rejected_for_every_status_before_approval(self, status):
        """观察期没跑满 / 没通过的任何状态，都不得放行。

        逐态参数化而不是只测一个：闸门若写成 `!= "registered"` 这类反条件，
        单点用例会漏掉，穷举不会。`paper_running` 尤其重要——那是「正在观察」，
        它最像「快好了」，也最不该被放行。
        """
        user = _user(f"gate-{status}")
        bt = _backtest(user)
        paper = _paper_session(user, bt, _account("paper", testnet=True))
        _candidate(paper, status=status)

        gate = evaluate_live_gate(paper)

        assert gate.allowed is False
        assert gate.reason
        assert "观察期" in gate.reason or "判定" in gate.reason

    def test_rejected_when_no_candidate_row_exists(self):
        """历史 paper 会话（P2 之前建的）没有候选主行 → 拒绝，不是静默放行。"""
        user = _user("gate-orphan")
        bt = _backtest(user)
        paper = _paper_session(user, bt, _account("paper", testnet=True))

        gate = evaluate_live_gate(paper)

        assert gate.allowed is False
        assert "候选主行" in gate.reason

    def test_rejection_reason_is_human_readable_and_names_the_status(self):
        """拒绝原因必须点名**当前**状态（人话），否则用户不知道卡在哪一层。"""
        user = _user("gate-reason")
        bt = _backtest(user)
        paper = _paper_session(user, bt, _account("paper", testnet=True))
        _candidate(paper, status="paper_running", name="我的动量策略")

        gate = evaluate_live_gate(paper)

        assert "我的动量策略" in gate.reason
        assert "paper 观察中" in gate.reason
        assert "等人工卡点" in gate.reason

    def test_gate_is_not_fooled_by_a_candidate_pointing_elsewhere(self):
        """候选绑的是**另一条**会话时，本会话拿不到放行。

        这是「按会话反查候选」而不是「按用户/策略猜一个候选」的直接后果：
        同一个策略可以有多条 paper 会话（跑了两次），放行必须跟着**这条**会话走。
        """
        user = _user("gate-other-session")
        bt = _backtest(user)
        approved = _paper_session(user, bt, _account("p1", testnet=True))
        _candidate(approved)
        other = _paper_session(user, bt, _account("p2", testnet=True))

        assert evaluate_live_gate(other).allowed is False
        assert evaluate_live_gate(approved).allowed is True


@pytest.mark.django_db
class TestFindApprovedPaperSession:
    """`live_session_create(mode="live")` 用的前置查询（D12 后半）。"""

    def test_finds_the_approved_candidate_for_this_backtest(self):
        user = _user("find-ok")
        bt = _backtest(user)
        paper = _paper_session(user, bt, _account("paper", testnet=True))
        _candidate(paper)

        gate = find_approved_paper_session(user=user, backtest_result=bt)

        assert gate.allowed is True
        assert gate.paper_session.pk == paper.pk

    def test_rejects_when_paper_exists_but_not_approved(self):
        """**这是本票的核心收紧**：有 paper 会话 ≠ 可以升实盘。

        旧口径 `.filter(mode="paper").exists()` 在这里会放行。改动后必须拒绝，
        且原因要说明「跑过但没通过」，不能含糊成「还没跑过」。
        """
        user = _user("find-not-approved")
        bt = _backtest(user)
        paper = _paper_session(user, bt, _account("paper", testnet=True))
        _candidate(paper, status="paper_running")

        gate = find_approved_paper_session(user=user, backtest_result=bt)

        assert gate.allowed is False
        assert "没有" in gate.reason and "跑满观察期" in gate.reason

    def test_rejects_when_no_paper_session_at_all(self):
        user = _user("find-none")
        bt = _backtest(user)

        gate = find_approved_paper_session(user=user, backtest_result=bt)

        assert gate.allowed is False
        assert "没有任何 paper 会话" in gate.reason

    def test_rejects_candidate_whose_paper_session_belongs_to_another_backtest(self):
        """候选的 paper 会话必须**就是这条回测**的会话。

        否则「A 回测的候选通过了」会放行「B 回测」的建会话请求——一条跨回测的旁路。
        """
        user = _user("find-cross-backtest")
        bt_a = _backtest(user, symbol="BTC/USDT")
        bt_b = _backtest(user, symbol="ETH/USDT")
        paper_b = _paper_session(user, bt_b, _account("pb", testnet=True))
        _candidate(paper_b)

        gate = find_approved_paper_session(user=user, backtest_result=bt_a)

        assert gate.allowed is False

    def test_rejects_candidate_whose_paper_session_belongs_to_another_user(self):
        """别人的候选不能拿来放行自己的实盘会话。"""
        owner = _user("find-owner")
        intruder = _user("find-intruder")
        bt = _backtest(owner)
        paper = _paper_session(owner, bt, _account("paper", testnet=True))
        _candidate(paper)

        gate = find_approved_paper_session(user=intruder, backtest_result=bt)

        assert gate.allowed is False

    def test_rejects_candidate_bound_to_a_non_paper_session(self):
        """候选绑的会话必须是 `mode="paper"`——不能是另一条 live 会话。

        这条防线在正常流程里够不着（`paper_session` 由漏斗写，写的必定是 paper），
        但它防的是**一堆人手工挪状态**之后的库：一条 `awaiting_live_approval` 的候选
        若把 `paper_session` 指向一条已经建好的 live 会话，闸门就会拿 live 会话的
        权益/config 去建**第二条** live 会话，并且把它当成「继承了 paper 期」。
        直接改库造出这个状态，钉住这条约束是有牙齿的。
        """
        user = _user("find-non-paper")
        bt = _backtest(user)
        live = LiveSession.objects.create(
            user=user,
            backtest_result=bt,
            strategy=bt.strategy,
            symbol=bt.symbol,
            mode="live",
            status="stopped",
            exchange_account=_account("real", testnet=False),
            initial_capital=Decimal("100"),
            current_equity=Decimal("100"),
            config={},
        )
        _candidate(live)

        gate = find_approved_paper_session(user=user, backtest_result=bt)

        assert gate.allowed is False

    def test_rejects_candidate_with_null_paper_session(self):
        """候选状态到了 `awaiting_live_approval` 却**没有** paper 会话 → 拒绝。

        同样是手工挪状态能造出来的库：`paper_session` 可空，而闸门要拿它继承权益与
        config。放行的话会在下面 `paper_session.current_equity` 那一行炸成 500——
        把「闸门该拒」伪装成「服务坏了」。
        """
        user = _user("find-null-paper")
        bt = _backtest(user)
        StrategyCandidate.objects.create(
            name="no-session",
            origin=StrategyCandidateOrigin.MANUAL.value,
            strategy=bt.strategy,
            status=APPROVED_STATUS,
            paper_session=None,
            proposed_by=user,
        )

        gate = find_approved_paper_session(user=user, backtest_result=bt)

        assert gate.allowed is False

    def test_selects_this_users_candidate_when_two_exist_on_one_backtest(self):
        """同一回测下有两条通过态候选（不同用户）时，选中的必须是**调用者自己**那条。

        单条候选的用例钉不住 `paper_session__user=user` 这个过滤——只有放进第二条
        才逼得出来。这里第二候选的 paper 会话更早创建，若按 `created_at` 先到先得，
        被选中的就会是**别人**的那条。
        """
        owner = _user("find-owner-2")
        intruder = _user("find-intruder-2")
        bt = _backtest(owner)

        theirs = _paper_session(intruder, bt, _account("theirs", testnet=True))
        _candidate(theirs, name="theirs", user=intruder)

        mine = _paper_session(owner, bt, _account("mine", testnet=True))
        _candidate(mine, name="mine", user=owner)

        gate = find_approved_paper_session(user=owner, backtest_result=bt)

        assert gate.allowed is True
        assert gate.candidate.name == "mine", "不能选中别人的候选"
        assert gate.paper_session.pk == mine.pk


# --------------------------------------------------------------------------- #
# 2. promote：目标账户 + 两步确认
# --------------------------------------------------------------------------- #


@pytest.mark.django_db
class TestPromoteAcceptsATargetLiveAccount:
    """交付 1：接受目标实盘账户建 live 会话，替代今天「必然 400」。"""

    def test_promote_creates_live_session_with_the_target_account(self):
        user = _user("promote-ok")
        bt = _backtest(user)
        paper = _paper_session(user, bt, _account("paper-sim", testnet=True))
        _candidate(paper)
        target = _account("real-money", testnet=False)

        resp = _promote(
            user,
            paper,
            body={"confirmed": True, "exchange_account_id": str(target.id)},
        )

        assert resp.status_code == 201, resp.content
        live = LiveSession.objects.get(mode="live")
        assert live.exchange_account_id == target.id
        assert live.mode_account_mismatch() is None, "不变式仍是最后一道校验"

    def test_promote_rejects_missing_target_account_with_readable_reason(self):
        """不传目标账户 → 拒绝，且原因说清「必须显式指定」（交付 5）。"""
        user = _user("promote-no-target")
        bt = _backtest(user)
        paper = _paper_session(user, bt, _account("paper", testnet=True))
        _candidate(paper)

        resp = _promote(user, paper, body={"confirmed": True})

        assert resp.status_code == 400, resp.content
        assert "exchange_account_id" in resp.json()["error"]
        assert not LiveSession.objects.filter(mode="live").exists()

    def test_promote_rejects_inactive_target_account(self):
        user = _user("promote-inactive")
        bt = _backtest(user)
        paper = _paper_session(user, bt, _account("paper", testnet=True))
        _candidate(paper)
        target = _account("retired", testnet=False, is_active=False)

        resp = _promote(
            user,
            paper,
            body={"confirmed": True, "exchange_account_id": str(target.id)},
        )

        assert resp.status_code == 404, resp.content
        assert not LiveSession.objects.filter(mode="live").exists()

    def test_promote_rejects_unknown_target_account(self):
        import uuid

        user = _user("promote-unknown")
        bt = _backtest(user)
        paper = _paper_session(user, bt, _account("paper", testnet=True))
        _candidate(paper)

        resp = _promote(
            user,
            paper,
            body={"confirmed": True, "exchange_account_id": str(uuid.uuid4())},
        )

        assert resp.status_code == 404, resp.content

    def test_promote_rejects_garbage_target_account_id(self):
        """非 UUID 的目标账户 id 不得变成 500——那是把「参数错」伪装成「服务坏」。"""
        user = _user("promote-garbage")
        bt = _backtest(user)
        paper = _paper_session(user, bt, _account("paper", testnet=True))
        _candidate(paper)

        resp = _promote(
            user, paper, body={"confirmed": True, "exchange_account_id": "not-a-uuid"}
        )

        assert resp.status_code == 404, resp.content

    def test_promote_rejects_symbol_conflict_on_the_target_account(self):
        """同账户同品种已有活跃会话 → 拒绝（既有不变式，不放宽）。"""
        user = _user("promote-conflict")
        bt = _backtest(user)
        paper = _paper_session(user, bt, _account("paper", testnet=True))
        _candidate(paper)
        target = _account("busy", testnet=False)
        LiveSession.objects.create(
            user=user,
            backtest_result=bt,
            strategy=bt.strategy,
            symbol=bt.symbol,
            mode="live",
            status="running",
            exchange_account=target,
            initial_capital=Decimal("100"),
            current_equity=Decimal("100"),
            config={},
        )

        resp = _promote(
            user,
            paper,
            body={"confirmed": True, "exchange_account_id": str(target.id)},
        )

        assert resp.status_code == 400, resp.content
        assert "活跃会话" in resp.json()["error"]


@pytest.mark.django_db
class TestPromoteTwoStepConfirmation:
    """交付 2：干跑摘要 → 显式确认。`confirmed` **fail-closed**。"""

    def _setup(self, username: str):
        user = _user(username)
        bt = _backtest(user)
        paper = _paper_session(user, bt, _account("paper", testnet=True))
        _candidate(paper)
        target = _account("real", testnet=False)
        return user, paper, target

    def test_dry_run_changes_nothing_and_returns_a_summary(self):
        user, paper, target = self._setup("promote-dry")

        resp = _promote(user, paper, body={"exchange_account_id": str(target.id)})

        assert resp.status_code == 200, resp.content
        body = resp.json()
        assert body["dry_run"] is True
        assert body["target_account"]["label"] == "real"
        assert body["will_inherit"]["initial_capital"] == "3100.75"
        # 干跑**什么都没改**：源会话照跑、没有 live 会话、候选状态没动。
        paper.refresh_from_db()
        assert paper.status == "stopped"
        assert not LiveSession.objects.filter(mode="live").exists()

    @staticmethod
    def _open_blanket_halt(label: str = "高波动"):
        """开一层保命档：`HALT_TRIGGER_SWITCH[BLANKET] = None`，不需要任何档位就作数。

        时刻**相对真实现在**造：视图不注入时钟（它走 `halt.blocking_declarations()` 的
        默认路径），钉死一个日期的话，将来真实时间越过它，「有层在拦」那几条会静默变成
        「没有层」而照样绿。
        """
        return _declare(
            trigger=HaltTrigger.BLANKET,
            label=label,
            opened_at=datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(hours=3),
        )

    def test_dry_run_carries_the_stop_layers_that_will_hold_the_new_session(self):
        """干跑摘要要预告**升级之后**那条会话的下单通路（D11 的「先看摘要」）。

        判据必须与新会话逐字一致：新会话用的就是源 paper 会话的 `(strategy, symbol)`，
        而 `_strategy_of_session` 从会话取的是同一个 `strategy_id`。
        """
        user, paper, target = self._setup("promote-halt")
        self._open_blanket_halt()

        resp = _promote(user, paper, body={"exchange_account_id": str(target.id)})

        assert resp.status_code == 200, resp.content
        body = resp.json()
        assert body["dry_run"] is True
        assert len(body["halt_layers"]) == 1
        assert "高波动" in body["halt_layers"][0]["text"]
        assert body["halt_notice"] == views.PROMOTE_HALT_NOTICE_BLOCKED

    def test_dry_run_says_so_when_nothing_is_holding(self):
        user, paper, target = self._setup("promote-no-halt")

        resp = _promote(user, paper, body={"exchange_account_id": str(target.id)})

        body = resp.json()
        assert body["halt_layers"] == [], "没有声明时那一栏是空数组，不是缺字段"
        assert body["halt_notice"] == views.PROMOTE_HALT_NOTICE_CLEAR

    def test_a_stop_layer_does_not_block_the_promotion_itself(self):
        """**预告，不是又一道闸门**：有层在拦时升级照常进行（真下单时才拦）。

        这条是刻意钉的：升级建的是会话，不是订单。把它也变成 fail-closed 会把停止判定
        管的面扩大一倍，而那是另一个决定。
        """
        user, paper, target = self._setup("promote-halt-proceeds")
        self._open_blanket_halt()

        resp = _promote(
            user,
            paper,
            body={"exchange_account_id": str(target.id), "confirmed": True},
        )

        assert resp.status_code == 201, resp.content
        assert "dry_run" not in resp.json()
        assert LiveSession.objects.filter(mode="live").exists()

    @pytest.mark.parametrize(
        "raw",
        [None, "", "false", "no", "0", False, 0, "maybe", [], {}],
    )
    def test_unrecognised_confirmation_values_all_mean_dry_run(self, raw):
        """认不出的一律当**没确认**（fail-closed）。

        与 `reduce_position` 同一张 `_AFFIRMATIVE` 表、同一条理由：降级 DSML 路径把
        布尔传成**字符串**，而 `bool("false") is True`——LLM 说了「不要」，字符串
        `"false"` 会被读成「已确认」。这里的代价是一次不该发生的实盘升格。
        """
        user, paper, target = self._setup(f"promote-fc-{str(raw)[:6]}")
        body = {"exchange_account_id": str(target.id)}
        if raw is not None:
            body["confirmed"] = raw

        resp = _promote(user, paper, body=body)

        assert resp.status_code == 200, resp.content
        assert resp.json()["dry_run"] is True
        assert not LiveSession.objects.filter(mode="live").exists()

    @pytest.mark.parametrize(
        "raw", [True, "true", "TRUE", "1", 1, "yes", "y", "on", "t"]
    )
    def test_recognised_affirmatives_actually_promote(self, raw):
        """只认明确的肯定——这张表是闸门的另一半，缺了它「同意」就永远无效。"""
        user, paper, target = self._setup(f"promote-af-{str(raw)[:6]}")

        resp = _promote(
            user,
            paper,
            body={"confirmed": raw, "exchange_account_id": str(target.id)},
        )

        assert resp.status_code == 201, resp.content
        assert LiveSession.objects.filter(mode="live").count() == 1

    def test_testnet_target_is_rejected_before_the_dry_run_summary(self):
        """目标账户是模拟盘时，**连摘要都不给**——不是在确认之后才被兜底拦下。

        这一条钉的是显式前置检查相对于 `confirmed` 的**顺序**，而不只是「最终被拒绝」：
        没有它的话，把 `target_account.testnet` 那段整个删掉，产物仍会被最后一道
        `mode_account_mismatch` 挡下，用例照样绿——那道兜底写在这里的意义就缩成了一句
        注释。顺序也有独立价值：用户不该拿到一份「将执行」的摘要去征求同意，而那次
        同意无论如何都会失败。
        """
        user = _user("promote-testnet-order")
        bt = _backtest(user)
        paper = _paper_session(user, bt, _account("paper", testnet=True))
        _candidate(paper)
        target = _account("still-sim", testnet=True)

        # 不带 confirmed：走的是「未确认 → 回摘要」那条路。若 testnet 前置失效，
        # 这里会返回 200 + 摘要；若前置有效，这里必须是 400 且**没有**摘要。
        resp = _promote(user, paper, body={"exchange_account_id": str(target.id)})

        assert resp.status_code == 400, resp.content
        assert "testnet" in resp.json()["error"]
        assert "dry_run" not in resp.json(), "模拟盘账户不得给出「将执行」摘要"
        assert not LiveSession.objects.filter(mode="live").exists()

    def test_gate_runs_before_the_dry_run_summary(self):
        """闸门先于干跑：一条没通过观察的会话，连摘要都不该给出。

        顺序反了的话，用户会拿到一份「将执行」摘要并据此去征求同意，而那次同意
        无论如何都会被闸门拒掉——白折腾一轮，且让人以为流程是通的。
        """
        user = _user("promote-gate-first")
        bt = _backtest(user)
        paper = _paper_session(user, bt, _account("paper", testnet=True))
        _candidate(paper, status="paper_running")
        target = _account("real", testnet=False)

        resp = _promote(user, paper, body={"exchange_account_id": str(target.id)})

        assert resp.status_code == 400, resp.content
        assert "error" in resp.json()
        assert "dry_run" not in resp.json()


@pytest.mark.django_db
class TestPromoteInheritsPaperEquityAndConfig:
    """交付 4：新 live 会话继承 paper 期的权益与配置。"""

    def test_inherits_current_equity_and_full_config(self):
        user = _user("promote-inherit")
        bt = _backtest(user, parameters={"from_backtest": True})
        paper = _paper_session(
            user,
            bt,
            _account("paper", testnet=True),
            current_equity=Decimal("4321.00"),
            config={"exit_at_mid": False, "position_pct": 0.2},
        )
        _candidate(paper)
        target = _account("real", testnet=False)

        resp = _promote(
            user, paper, body={"confirmed": True, "exchange_account_id": str(target.id)}
        )

        assert resp.status_code == 201, resp.content
        live = LiveSession.objects.get(mode="live")
        assert live.initial_capital == Decimal("4321.00")
        assert live.current_equity == Decimal("4321.00")
        assert live.config == {"exit_at_mid": False, "position_pct": 0.2}

    def test_inherited_config_is_a_copy_not_a_shared_object(self):
        """继承的 config 必须是**拷贝**：共享同一个可变对象会让改一条的另一条跟着变。

        这里**不能**靠「建完之后改 `live.config`，再 refresh 看 paper」来钉——从 ORM
        取回的每个实例都是**独立反序列化**出来的 dict，改它永远影响不到另一行，于是
        那条断言在「共享」与「拷贝」两种实现下**都是绿的**（第一版就是这么写的，探针
        跑出来果然是绿的）。也不能拿测试自己新取的实例去比 `is not`：视图内部
        `LiveSession.objects.get()` 拿到的是**另一个**对象，那样比也是恒真的。

        真正能观察到共享的位置只有一个：**视图传进 `create()` 的那个 dict，是不是它
        自己手里那条 `session` 的 `config`**。所以这里替换掉 `create`，直接比对
        「传参对象」与「视图刚刚查出来的那条会话的 config 是不是同一个对象」——
        视图查会话走 `LiveSession.objects.get`，把它一并截住即可。
        """
        from unittest.mock import patch as _patch

        from apps.trading.views import LiveSession as _Live

        user = _user("promote-copy")
        bt = _backtest(user)
        original = {"exit_at_mid": False}
        paper = _paper_session(
            user, bt, _account("paper", testnet=True), config=original
        )
        _candidate(paper)
        target = _account("real", testnet=False)

        seen: dict = {}
        real_get = _Live.objects.get
        real_create = _Live.objects.create

        def _spy_get(*args, **kwargs):
            obj = real_get(*args, **kwargs)
            seen["loaded_session"] = obj
            return obj

        def _spy_create(**kwargs):
            seen["passed_config"] = kwargs.get("config")
            return real_create(**kwargs)

        with (
            _patch.object(_Live.objects, "get", side_effect=_spy_get),
            _patch.object(_Live.objects, "create", side_effect=_spy_create),
        ):
            resp = _promote(
                user,
                paper,
                body={"confirmed": True, "exchange_account_id": str(target.id)},
            )

        assert resp.status_code == 201, resp.content
        passed = seen["passed_config"]
        assert passed == original, "继承的键要在"
        assert passed is not seen["loaded_session"].config, (
            "传给新会话的 config 必须是拷贝：视图把自己手里那条会话的同一个 dict 对象"
            "传给了新会话，改一条的另一条会跟着变"
        )

    def test_falls_back_to_initial_capital_when_equity_is_null(self):
        user = _user("promote-null-equity")
        bt = _backtest(user)
        paper = _paper_session(user, bt, _account("paper", testnet=True))
        LiveSession.objects.filter(pk=paper.pk).update(current_equity=None)
        _candidate(paper)
        target = _account("real", testnet=False)

        resp = _promote(
            user, paper, body={"confirmed": True, "exchange_account_id": str(target.id)}
        )

        assert resp.status_code == 201, resp.content
        live = LiveSession.objects.get(mode="live")
        assert live.initial_capital == paper.initial_capital
        assert live.initial_capital is not None


@pytest.mark.django_db
class TestPromoteClosesTheCandidate:
    """升实盘是人工作出的终局决定，候选主行要如实记下它。"""

    def test_candidate_moves_to_live_and_records_account_and_session(self):
        user = _user("promote-candidate")
        bt = _backtest(user)
        paper = _paper_session(user, bt, _account("paper", testnet=True))
        cand = _candidate(paper)
        target = _account("real", testnet=False)

        resp = _promote(
            user, paper, body={"confirmed": True, "exchange_account_id": str(target.id)}
        )

        assert resp.status_code == 201, resp.content
        cand.refresh_from_db()
        assert cand.status == "live"
        assert cand.live_account_id == target.id
        assert cand.live_session_id == LiveSession.objects.get(mode="live").id
        assert cand.decided_at is not None, "人工决策时刻要落库（D1）"

    def test_source_paper_session_is_stopped(self):
        user = _user("promote-stop-source")
        bt = _backtest(user)
        paper = _paper_session(
            user, bt, _account("paper", testnet=True), status="running"
        )
        _candidate(paper)
        target = _account("real", testnet=False)

        resp = _promote(
            user, paper, body={"confirmed": True, "exchange_account_id": str(target.id)}
        )

        assert resp.status_code == 201, resp.content
        paper.refresh_from_db()
        assert paper.status == "stopped"
        assert paper.stopped_at is not None

    def test_promote_rejects_non_paper_source(self):
        user = _user("promote-non-paper")
        bt = _backtest(user)
        live = LiveSession.objects.create(
            user=user,
            backtest_result=bt,
            strategy=bt.strategy,
            symbol=bt.symbol,
            mode="live",
            status="stopped",
            exchange_account=_account("real", testnet=False),
            initial_capital=Decimal("100"),
            current_equity=Decimal("100"),
            config={},
        )

        resp = _promote(user, live, body={"confirmed": True})

        assert resp.status_code == 400, resp.content
        assert "paper" in resp.json()["error"].lower()

    def test_promote_rejects_pending_source(self):
        """`pending` 会话还没跑过，不能升实盘（既有前置，未放松）。"""
        user = _user("promote-pending")
        bt = _backtest(user)
        paper = _paper_session(
            user, bt, _account("paper", testnet=True), status="pending"
        )
        _candidate(paper)

        resp = _promote(user, paper, body={"confirmed": True})

        assert resp.status_code == 400, resp.content
        assert "cannot promote" in resp.json()["error"]

    def test_promote_requires_ownership(self):
        owner = _user("promote-owner")
        intruder = _user("promote-intruder")
        bt = _backtest(owner)
        paper = _paper_session(owner, bt, _account("paper", testnet=True))
        _candidate(paper)

        resp = _promote(intruder, paper, body={"confirmed": True})

        assert resp.status_code == 404, resp.content


# --------------------------------------------------------------------------- #
# 3. live_session_create：paper 前置收紧（D12 后半）
# --------------------------------------------------------------------------- #


@pytest.mark.django_db
class TestLiveCreateRequiresAPassedPaperSession:
    """mode="live" 的前置 = **跑满观察期且判定通过**，不再是「存在任意 paper 会话」。"""

    def _setup(self, username: str, *, parameters=None):
        user = _user(username)
        bt = _backtest(user, parameters=parameters)
        client = APIClient()
        client.force_authenticate(user)
        return user, client, bt

    def test_live_create_allowed_when_candidate_passed_observation(self):
        user, client, bt = self._setup("create-allowed")
        paper = _paper_session(user, bt, _account("paper", testnet=True))
        _candidate(paper)
        target = _account("real", testnet=False)

        with patch("apps.trading.adapters.ADAPTER_MAP", {"binance": FakeAdapter}):
            resp = _post_create(client, bt, target, mode="live")

        assert resp.status_code == 201, resp.content
        live = LiveSession.objects.get(mode="live")
        assert live.exchange_account_id == target.id
        assert live.mode_account_mismatch() is None

    @pytest.mark.parametrize(
        "status",
        [
            "registered",
            "in_sample_running",
            "in_sample_failed",
            "oos_running",
            "oos_failed",
            "paper_pending",
            "paper_running",
            "paper_failed",
        ],
    )
    def test_live_create_rejected_when_candidate_not_yet_passed(self, status):
        """**这是本票要修的那个口**：旧口径在这里会放行。

        对每一种「有 paper 会话但没跑满 / 没通过」的状态，live 建会话都必须拒绝。
        「跑过但没通过」与「还没跑过」必须说清是哪种——含糊会让用户去重跑一条
        已经跑过的会话。
        """
        user, client, bt = self._setup(f"create-{status}")
        paper = _paper_session(user, bt, _account("paper", testnet=True))
        _candidate(paper, status=status)
        target = _account("real", testnet=False)

        resp = _post_create(client, bt, target, mode="live")

        assert resp.status_code == 400, resp.content
        assert "观察期" in resp.json()["error"]
        assert not LiveSession.objects.filter(mode="live").exists()

    def test_live_create_rejected_when_no_paper_session_at_all(self):
        user, client, bt = self._setup("create-none")
        target = _account("real", testnet=False)

        resp = _post_create(client, bt, target, mode="live")

        assert resp.status_code == 400, resp.content
        assert "没有任何 paper 会话" in resp.json()["error"]
        assert not LiveSession.objects.filter(mode="live").exists()

    def test_live_create_rejected_when_paper_session_has_no_candidate(self):
        """裸 paper 会话（没有候选主行，P2 之前建的那种）不得放行。"""
        user, client, bt = self._setup("create-orphan")
        _paper_session(user, bt, _account("paper", testnet=True))
        target = _account("real", testnet=False)

        resp = _post_create(client, bt, target, mode="live")

        assert resp.status_code == 400, resp.content
        assert not LiveSession.objects.filter(mode="live").exists()

    def test_live_create_rejected_when_candidate_is_for_another_backtest(self):
        """候选属于**另一条**回测 → 不予放行（不能跨回测借通过）。"""
        user, client, bt = self._setup("create-cross")
        other_bt = _backtest(user, symbol="ETH/USDT")
        paper_other = _paper_session(user, other_bt, _account("p", testnet=True))
        _candidate(paper_other)
        target = _account("real", testnet=False)

        resp = _post_create(client, bt, target, mode="live")

        assert resp.status_code == 400, resp.content

    def test_live_create_inherits_paper_equity_and_config(self):
        """交付 4：live 会话继承 **paper 期**的权益与配置。

        回测参数与 paper config **故意有一个同名键** `position_pct` 且取值不同：
        没有这个重叠，两种继承顺序会给出同一个结果，断言就钉不住「谁盖谁」
        （变异探针 8 第一版跑出来就是绿的）。
        """
        user, client, bt = self._setup(
            "create-inherit",
            parameters={
                "exit_at_mid": True,
                "position_pct": 0.9,
                "only_in_backtest": 1,
            },
        )
        paper = _paper_session(
            user,
            bt,
            _account("paper", testnet=True),
            current_equity=Decimal("7777.00"),
            config={"exit_at_mid": False, "position_pct": 0.2},
        )
        _candidate(paper)
        target = _account("real", testnet=False)

        with patch("apps.trading.adapters.ADAPTER_MAP", {"binance": FakeAdapter}):
            resp = _post_create(client, bt, target, mode="live")

        assert resp.status_code == 201, resp.content
        live = LiveSession.objects.get(mode="live")
        assert live.initial_capital == Decimal("7777.00"), (
            "权益要继承 paper 期，不是账户余额"
        )
        assert live.current_equity == Decimal("7777.00")
        # paper 期的值胜出（`exit_at_mid=False` 是**实际观察过**的那一份），
        # 回测参数只在 paper 没提这个键时兜底。
        assert live.config["exit_at_mid"] is False, "paper config 必须盖过回测参数"
        assert live.config["position_pct"] == 0.2, "同名键要以 paper 期为准"

    def test_explicit_config_still_wins_on_live_create(self):
        """显式传入的 config 键仍然最优先：人工可在升实盘那一刻再覆盖。"""
        user, client, bt = self._setup("create-override")
        paper = _paper_session(
            user, bt, _account("paper", testnet=True), config={"position_pct": 0.2}
        )
        _candidate(paper)
        target = _account("real", testnet=False)

        with patch("apps.trading.adapters.ADAPTER_MAP", {"binance": FakeAdapter}):
            resp = _post_create(
                client, bt, target, mode="live", extra={"config": {"position_pct": 0.5}}
            )

        assert resp.status_code == 201, resp.content
        assert LiveSession.objects.get(mode="live").config == {"position_pct": 0.5}

    def test_paper_mode_is_unaffected_by_the_tightening(self):
        """paper 建会话**不经**这道闸门：收紧的是 live，不是整条入口。"""
        user, client, bt = self._setup("create-paper")
        account = _account("paper", testnet=True)

        with patch("apps.trading.adapters.ADAPTER_MAP", {"binance": FakeAdapter}):
            resp = _post_create(client, bt, account, mode="paper")

        assert resp.status_code == 201, resp.content
        assert LiveSession.objects.get(mode="paper").exchange_account_id == account.id

    def test_live_create_still_requires_approved_backtest(self):
        """审核前置仍在：收紧 paper 前置时没把它删掉（既有闸门逐条保留）。"""
        user, client, bt = self._setup("create-unreviewed")
        bt.review_status = "pending"
        bt.save(update_fields=["review_status"])
        paper = _paper_session(user, bt, _account("paper", testnet=True))
        _candidate(paper)
        target = _account("real", testnet=False)

        resp = _post_create(client, bt, target, mode="live")

        assert resp.status_code == 400, resp.content
        assert "approved" in resp.json()["error"]

    def test_live_create_rejects_testnet_target_account(self):
        """不变式不放松：目标账户是模拟盘时，mode=live 的会话建不出来。"""
        user, client, bt = self._setup("create-testnet-target")
        paper = _paper_session(user, bt, _account("paper", testnet=True))
        _candidate(paper)
        target = _account("still-sim", testnet=True)

        with patch("apps.trading.adapters.ADAPTER_MAP", {"binance": FakeAdapter}):
            resp = _post_create(client, bt, target, mode="live")

        assert resp.status_code == 400, resp.content
        assert "testnet" in resp.json()["error"]
        assert not LiveSession.objects.filter(mode="live").exists()
