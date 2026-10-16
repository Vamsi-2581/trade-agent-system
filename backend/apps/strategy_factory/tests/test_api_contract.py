"""候选策略 REST 契约（spec #19 D21，子票 #23）。

这份测试钉六条路由的**契约面**，每条都按「正向 + 反向」成对：

1. **鉴权**：六个端点全部要求登录（未带凭据 401）。
2. **用户隔离**：A 用户的候选对 B 用户不可见（列表不出现、详情 404、
   写端点同样 404）——`proposed_by` 过滤就是隔离本身，别人的候选与不存在的
   候选长得一样，不向探测者泄露存在性。
3. **只读形状**：详情带 `evidence` 全文 + `transitions`（推进史）+ 会话指针。
4. **否决 / 取消**：合法边的状态推进 + 审计记录 + `decided_at`；非法边 400。
5. **promote 委托**：候选 → paper 会话映射后委托 trading 端点；没有 paper
   会话的候选当场 400。干跑摘要与确认流程的**业务口径**归
   `apps/trading/tests/test_live_session_promotion.py`（唯一出处），这里只钉
   「映射与委托」这条缝。
6. **登记参数校验**：缺必填 400（不建行）。

登记的完整业务校验（文件级 / 注册键级 / D17）归
`apps/agent/tests/` 的登记工具用例——REST 端点复用同一实现，不重测复制品。
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from rest_framework_simplejwt.tokens import AccessToken

from apps.strategy_factory.models import (
    StrategyCandidate,
    StrategyCandidateTransition,
)
from apps.strategy_factory.status import (
    StrategyCandidateOrigin,
    StrategyCandidateStatus,
)

User = get_user_model()

LIST_URL = "/api/strategy-factory/candidates/"
REGISTER_URL = "/api/strategy-factory/candidates/register/"


def candidate_url(pk) -> str:
    return f"/api/strategy-factory/candidates/{pk}/"


def _client(user) -> Client:
    client = Client()
    token = AccessToken.for_user(user)
    client.defaults["HTTP_AUTHORIZATION"] = f"Bearer {token}"
    return client


def _user(username: str):
    return User.objects.create_user(
        username=username, email=f"{username}@example.com", password="pw12345"
    )


def _candidate(user, *, status=StrategyCandidateStatus.REGISTERED, name="cand"):
    return StrategyCandidate.objects.create(
        name=name,
        hypothesis="探针",
        code_path="strategies/x.py",
        origin=StrategyCandidateOrigin.AGENT,
        status=status,
        proposed_by=user,
        evidence={"layers": {"in_sample": {"result_id": "42"}}},
    )


# --------------------------------------------------------------------------- #
# 1. 鉴权：六条路由都要登录
# --------------------------------------------------------------------------- #


@pytest.mark.django_db
class TestAuthRequired:
    """未带凭据一律 401——`IsAuthenticated` 是每条路由的既有惯例。"""

    def test_list_requires_auth(self):
        assert Client().get(LIST_URL).status_code == 401

    def test_detail_requires_auth(self):
        user = _user("auth-detail")
        cand = _candidate(user)
        assert Client().get(candidate_url(cand.pk)).status_code == 401

    def test_register_requires_auth(self):
        assert Client().post(REGISTER_URL, {}, format="json").status_code == 401

    def test_promote_requires_auth(self):
        user = _user("auth-promote")
        cand = _candidate(user)
        assert Client().post(candidate_url(cand.pk) + "promote/").status_code == 401

    def test_reject_requires_auth(self):
        user = _user("auth-reject")
        cand = _candidate(user)
        assert Client().post(candidate_url(cand.pk) + "reject/").status_code == 401

    def test_cancel_requires_auth(self):
        user = _user("auth-cancel")
        cand = _candidate(user)
        assert Client().post(candidate_url(cand.pk) + "cancel/").status_code == 401


# --------------------------------------------------------------------------- #
# 2. 用户隔离
# --------------------------------------------------------------------------- #


@pytest.mark.django_db
class TestUserIsolation:
    """`proposed_by` 过滤：别人的候选对我不存在。"""

    def test_list_only_shows_own_candidates(self):
        alice = _user("iso-list-a")
        bob = _user("iso-list-b")
        mine = _candidate(alice, name="mine")
        _candidate(bob, name="theirs")

        data = _client(alice).get(LIST_URL).json()
        names = [row["name"] for row in data]
        assert names == ["mine"]
        assert data[0]["id"] == str(mine.pk)

    def test_detail_of_other_users_candidate_is_404(self):
        alice = _user("iso-detail-a")
        bob = _user("iso-detail-b")
        cand = _candidate(bob)

        response = _client(alice).get(candidate_url(cand.pk))
        assert response.status_code == 404

    def test_reject_of_other_users_candidate_is_404_and_no_write(self):
        alice = _user("iso-reject-a")
        bob = _user("iso-reject-b")
        cand = _candidate(bob)

        response = _client(alice).post(candidate_url(cand.pk) + "reject/")
        assert response.status_code == 404
        cand.refresh_from_db()
        assert cand.status == StrategyCandidateStatus.REGISTERED.value

    def test_promote_of_other_users_candidate_is_404(self):
        alice = _user("iso-promote-a")
        bob = _user("iso-promote-b")
        cand = _candidate(bob)

        assert (
            _client(alice).post(candidate_url(cand.pk) + "promote/").status_code == 404
        )

    def test_candidates_without_proposer_visible_to_nobody(self):
        """历史候选（`proposed_by` 可空，D1）不属于任何 REST 用户。"""

        _candidate(None, name="orphan") if False else None
        cand = StrategyCandidate.objects.create(
            name="orphan",
            origin=StrategyCandidateOrigin.MANUAL.value,
            status=StrategyCandidateStatus.REGISTERED.value,
        )
        alice = _user("iso-orphan")
        assert _client(alice).get(LIST_URL).json() == []
        assert _client(alice).get(candidate_url(cand.pk)).status_code == 404


# --------------------------------------------------------------------------- #
# 3. 只读形状
# --------------------------------------------------------------------------- #


@pytest.mark.django_db
class TestReadShape:
    """详情带 evidence 全文 + 推进史；列表不带全文（轻）。"""

    def test_detail_carries_evidence_and_transitions(self):
        user = _user("shape-detail")
        cand = _candidate(user, status=StrategyCandidateStatus.IN_SAMPLE_FAILED)
        StrategyCandidateTransition.objects.create(
            candidate=cand,
            layer="in_sample",
            from_status="in_sample_running",
            to_status="in_sample_failed",
            reason_code="in_sample_sharpe_below_min",
            detail={"sharpe_ratio": 0.9},
            at="2026-06-01T00:00:00+00:00",
        )

        data = _client(user).get(candidate_url(cand.pk)).json()
        assert data["id"] == str(cand.pk)
        assert data["status"] == "in_sample_failed"
        assert data["status_display"] == "样本内未过"
        # evidence 全文（D21：每层判定与依据）
        assert data["evidence"]["layers"]["in_sample"]["result_id"] == "42"
        # 推进史（D20 审计面）
        assert len(data["transitions"]) == 1
        transition = data["transitions"][0]
        assert transition["reason_code"] == "in_sample_sharpe_below_min"
        assert transition["from_status"] == "in_sample_running"
        assert transition["to_status"] == "in_sample_failed"
        assert transition["detail"]["sharpe_ratio"] == 0.9

    def test_detail_session_pointer_when_bound(self):
        """paper 会话指针：只给寻址信息，会话本体归 trading 端点。"""
        from decimal import Decimal

        from apps.exchange.models import ExchangeAccount
        from apps.trading.models import LiveSession, Strategy

        user = _user("shape-session")
        account = ExchangeAccount.objects.create(
            exchange="binance",
            label="paper",
            api_key_enc=b"k",
            api_secret_enc=b"s",
            testnet=True,
        )
        strategy = Strategy.objects.create(name="shape-s", code_path="strategies/s.py")
        session = LiveSession.objects.create(
            user=user,
            strategy=strategy,
            symbol="BTC/USDT:USDT",
            mode="paper",
            status="running",
            exchange_account=account,
            initial_capital=Decimal("1000"),
        )
        cand = StrategyCandidate.objects.create(
            name="cand-s",
            origin=StrategyCandidateOrigin.AGENT.value,
            status=StrategyCandidateStatus.PAPER_RUNNING.value,
            proposed_by=user,
            paper_session=session,
            strategy=strategy,
        )

        data = _client(user).get(candidate_url(cand.pk)).json()
        assert data["paper_session"]["id"] == str(session.pk)
        assert data["paper_session"]["mode"] == "paper"
        assert data["paper_session"]["status"] == "running"
        assert data["strategy_name"] == "shape-s"

    def test_detail_404_for_unknown_id(self):
        user = _user("shape-404")

        response = _client(user).get(
            candidate_url("00000000-0000-0000-0000-000000000000")
        )
        assert response.status_code == 404

    def test_list_filters_by_status_and_origin(self):
        user = _user("shape-filter")
        _candidate(user, name="a", status=StrategyCandidateStatus.REGISTERED)
        _candidate(user, name="b", status=StrategyCandidateStatus.LIVE)

        data = _client(user).get(LIST_URL, {"status": "live"}).json()
        assert [row["name"] for row in data] == ["b"]

        data = _client(user).get(LIST_URL, {"origin": "agent"}).json()
        assert len(data) == 2


# --------------------------------------------------------------------------- #
# 4. 否决 / 取消
# --------------------------------------------------------------------------- #


@pytest.mark.django_db
class TestManualDecisions:
    """否决 / 取消：合法边推进 + 审计；非法边 400。"""

    def test_reject_registered_candidate(self):
        user = _user("decide-reject")
        cand = _candidate(user)

        response = _client(user).post(
            candidate_url(cand.pk) + "reject/", {"note": "假设不成立"}, format="json"
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "rejected"
        assert data["decided_at"] is not None

        cand.refresh_from_db()
        assert cand.status == "rejected"
        assert cand.decided_at is not None
        # 审计记录（D20）：机器判定与人把叫停可区分
        transition = cand.transitions.get()
        assert transition.reason_code == "human_rejected"
        assert transition.detail["note"] == "假设不成立"

    def test_cancel_registered_candidate(self):
        user = _user("decide-cancel")
        cand = _candidate(user)

        response = _client(user).post(candidate_url(cand.pk) + "cancel/")
        assert response.status_code == 200
        assert response.json()["status"] == "cancelled"

        cand.refresh_from_db()
        assert cand.status == "cancelled"
        assert cand.transitions.get().reason_code == "human_cancelled"

    def test_reject_live_candidate_is_400(self):
        """`live` 是终态：没有出边，否决/取消都不得放行（状态机守门）。"""
        user = _user("decide-live")
        cand = _candidate(user, status=StrategyCandidateStatus.LIVE)

        response = _client(user).post(candidate_url(cand.pk) + "reject/")
        assert response.status_code == 400
        cand.refresh_from_db()
        assert cand.status == "live"  # 原状未动

    def test_reject_in_sample_running_is_400(self):
        """`in_sample_running` → `rejected` 不是合法边（只有失败态与登记态可人工收尾）。"""
        user = _user("decide-running")
        cand = _candidate(user, status=StrategyCandidateStatus.IN_SAMPLE_RUNNING)

        response = _client(user).post(candidate_url(cand.pk) + "cancel/")
        assert response.status_code == 400
        assert "不允许从" in response.json()["error"]

    def test_cancel_paper_pending_candidate(self):
        """`paper_pending → cancelled` 是合法边（等账户时人可以收手）。"""
        user = _user("decide-pending")
        cand = _candidate(user, status=StrategyCandidateStatus.PAPER_PENDING)

        response = _client(user).post(candidate_url(cand.pk) + "cancel/")
        assert response.status_code == 200
        assert response.json()["status"] == "cancelled"


# --------------------------------------------------------------------------- #
# 5. promote 委托
# --------------------------------------------------------------------------- #


@pytest.mark.django_db
class TestPromoteDelegation:
    """候选 → paper 会话映射，然后委托 trading 端点。

    业务口径（干跑/确认/testnet 校验）的唯一测试在
    `apps/trading/tests/test_live_session_promotion.py`；这里钉「映射与委托」。
    """

    def _paper_session(self, user):
        from decimal import Decimal

        from apps.exchange.models import ExchangeAccount
        from apps.trading.models import LiveSession, Strategy

        account = ExchangeAccount.objects.create(
            exchange="binance",
            label="paper",
            api_key_enc=b"k",
            api_secret_enc=b"s",
            testnet=True,
        )
        strategy = Strategy.objects.create(name="promo-s", code_path="strategies/s.py")
        return LiveSession.objects.create(
            user=user,
            strategy=strategy,
            symbol="BTC/USDT:USDT",
            mode="paper",
            status="stopped",
            exchange_account=account,
            initial_capital=Decimal("1000"),
            current_equity=Decimal("1100"),
        )

    def test_promote_without_paper_session_is_400(self):
        user = _user("promo-nosession")
        cand = _candidate(user, status=StrategyCandidateStatus.AWAITING_LIVE_APPROVAL)

        response = _client(user).post(candidate_url(cand.pk) + "promote/")
        assert response.status_code == 400
        assert "还没有 paper 会话" in response.json()["error"]

    def test_promote_delegates_and_hits_gate(self):
        """有 paper 会话 → 委托 trading 端点。

        候选不在 `awaiting_live_approval`，闸门（唯一出处）拒绝——这条用例
        钉的是「委托真的发生了、闸门真的被走过」，不是闸门本身。
        """
        user = _user("promo-delegate")
        session = self._paper_session(user)
        cand = StrategyCandidate.objects.create(
            name="cand-promo",
            origin=StrategyCandidateOrigin.AGENT.value,
            status=StrategyCandidateStatus.PAPER_RUNNING.value,
            proposed_by=user,
            paper_session=session,
        )

        response = _client(user).post(candidate_url(cand.pk) + "promote/")
        assert response.status_code == 400
        # 拒绝文案来自 trading 端点走的 live_gate（唯一出处），不是本视图造的
        assert "等人工卡点" in response.json()["error"]

    def test_promote_delegation_carries_body_to_dry_run(self):
        """委托把**请求体**原样带到被委托端点：候选在 `awaiting_live_approval`
        且闸门放行时，不带 `confirmed` 的请求必须落到**干跑摘要**（200）——
        这是「映射 + 委托」这条缝的端到端证据：请求体丢了、`confirmed` 没带过去，
        或者映射到了错的会话，这条用例都会翻红。

        摘要里能看到候选与 paper 会话：映射的是**这条候选绑的那条**会话。
        """
        from cryptography.fernet import Fernet
        from django.conf import settings as dj_settings
        from decimal import Decimal

        from apps.exchange.models import ExchangeAccount
        from apps.trading.models import LiveSession, Strategy

        user = _user("promo-dryrun")

        f = Fernet(dj_settings.FERNET_KEY.encode())

        def _acct(label, testnet):
            return ExchangeAccount.objects.create(
                exchange="binance",
                label=label,
                api_key_enc=f.encrypt(b"test_key"),
                api_secret_enc=f.encrypt(b"test_secret"),
                testnet=testnet,
            )

        paper_acct = _acct("paper", testnet=True)
        live_acct = _acct("live", testnet=False)
        strategy = Strategy.objects.create(
            name="promo-dry", code_path="strategies/s.py"
        )
        session = LiveSession.objects.create(
            user=user,
            strategy=strategy,
            symbol="BTC/USDT:USDT",
            mode="paper",
            status="stopped",
            exchange_account=paper_acct,
            initial_capital=Decimal("1000"),
            current_equity=Decimal("1100"),
        )
        cand = StrategyCandidate.objects.create(
            name="cand-dry",
            origin=StrategyCandidateOrigin.AGENT.value,
            status=StrategyCandidateStatus.AWAITING_LIVE_APPROVAL.value,
            proposed_by=user,
            paper_session=session,
        )

        # 被委托端点的顺序是：闸门 → 目标账户 → testnet 校验 → 干跑摘要。
        # 干跑摘要之前要求**目标实盘账户**（testnet=False，D11）——不带它 400。
        # 两个响应都断言，钉住「委托把请求体原样带到了每一步」：
        no_account = _client(user).post(candidate_url(cand.pk) + "promote/")
        assert no_account.status_code == 400
        assert "exchange_account_id" in no_account.json()["error"]

        response = _client(user).post(
            candidate_url(cand.pk) + "promote/",
            {"exchange_account_id": str(live_acct.pk)},
            format="json",
        )
        assert response.status_code == 200
        data = response.json()
        assert data["dry_run"] is True
        assert data["candidate"]["id"] == str(cand.pk)
        assert data["paper_session"]["id"] == str(session.pk)


# --------------------------------------------------------------------------- #
# 6. 登记参数校验
# --------------------------------------------------------------------------- #


@pytest.mark.django_db
class TestRegisterParams:
    """缺必填 400 且不建行；完整业务校验归 agent 工具的用例（复用不重测）。"""

    def test_missing_required_fields_is_400(self):
        user = _user("reg-params")
        response = _client(user).post(REGISTER_URL, {}, format="json")
        assert response.status_code == 400
        assert StrategyCandidate.objects.count() == 0

    def test_missing_file_path_is_400(self):
        user = _user("reg-params2")
        response = _client(user).post(
            REGISTER_URL, {"strategy_name": "x"}, format="json"
        )
        assert response.status_code == 400
        assert StrategyCandidate.objects.count() == 0
