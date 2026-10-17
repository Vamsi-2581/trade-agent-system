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
# 3b. 行情持有透出（spec #30 用户故事 2 / 7，子票 #36）
# --------------------------------------------------------------------------- #


@pytest.mark.django_db
class TestRegimeHoldSurfacing:
    """被行情闸门拦下的候选，**在 REST 契约面上**能读到「停在哪个阶段、判成了什么、
    为什么」——spec 用户故事 2 要的是「候选因行情被拦时可见」，而可见性的落点是
    详情响应（`evidence` 全文 + 转移记录的 `reason_code` / `detail`）与通知正文。

    这一组只钉**契约面**：闸门的判定口径归 `evaluate.regime_verdict` 的用例，通知
    正文的渲染归 `orchestrator._message` 的用例（`test_advance.py`）。这里钉的是
    「判定结果真的走到了 REST 的字段上」——序列化器一旦收窄了透出面（例如把
    `evidence` 挑键输出），那些用例全绿而用户依然什么都看不到。
    """

    def _held(self, user, *, reason_code, detail, name="held"):
        """一条停在 `regime_hold` 的候选 + 一条真实推进史（进 hold 的那一次）。"""
        cand = StrategyCandidate.objects.create(
            name=name,
            hypothesis="探针",
            code_path="strategies/x.py",
            origin=StrategyCandidateOrigin.AGENT,
            status=StrategyCandidateStatus.REGIME_HOLD,
            proposed_by=user,
            evidence={
                "layers": {"oos": {"result_id": "7"}},
                "verdict": {
                    "layer": "pipeline",
                    "from": "oos_running",
                    "to": "regime_hold",
                    "reason_code": reason_code,
                    "detail": detail,
                    "at": "2026-06-01T00:00:00+00:00",
                },
            },
        )
        StrategyCandidateTransition.objects.create(
            candidate=cand,
            layer="pipeline",
            from_status="oos_running",
            to_status="regime_hold",
            reason_code=reason_code,
            detail=detail,
            at="2026-06-01T00:00:00+00:00",
        )
        return cand

    def test_status_display_names_the_hold(self):
        """`regime_hold` 的展示名不是 ascii slug——列表与详情都要给同一句人话。

        `status_enum.display` 在本仓是**无 `.get()` 兜底**的字典（加了状态却忘了
        给它展示名必须立刻 KeyError，见 `.p4-notes/README.md`）。所以这条用例同时
        钉住「这个枚举成员有展示名」。
        """
        user = _user("regime-hold-display")
        self._held(
            user,
            reason_code="regime_unfit",
            detail={"regime": "unfit", "regime_hold_reason": "cell_state=unfit"},
        )

        listed = _client(user).get(LIST_URL).json()[0]
        assert listed["status"] == "regime_hold"
        assert listed["status_display"] == "等行情闸门结论"

        data = _client(user).get(candidate_url(listed["id"])).json()
        assert data["status_display"] == "等行情闸门结论"

    def test_detail_carries_the_regime_findings(self):
        """详情要能读出「当前阶段 / 该策略判为 / 原因」三件套（spec 用户故事 2）。

        `evidence` 是全量透出（D21），`transitions[-1].detail` 是**平铺**的行情旁证
        （`evaluate._regime_hold_verdict` 的 docstring 说明了为什么必须平铺：通知正文
        按平铺的 `k=v` 挑键渲染，套一层子字典会让整段从「依据」里消失）。
        """
        user = _user("regime-hold-detail")
        detail = {
            "regime": "unfit",
            "regime_hold_reason": "cell_state=unfit",
            "cell_state": "unfit",
            "cell_reason": "样本外衰减超标",
        }
        cand = self._held(user, reason_code="regime_unfit", detail=detail)

        data = _client(user).get(candidate_url(cand.pk)).json()

        assert data["status"] == "regime_hold"
        assert data["status_display"] == "等行情闸门结论"
        # evidence 全文：闸门的结论就在里面（原样透出，不挑键）
        verdict = data["evidence"]["verdict"]
        assert verdict["reason_code"] == "regime_unfit"
        assert verdict["to"] == "regime_hold"
        assert verdict["detail"]["regime"] == "unfit"
        # 推进史：读记录的人能从这一行归因
        last = data["transitions"][-1]
        assert last["reason_code"] == "regime_unfit"
        assert last["to_status"] == "regime_hold"
        assert last["detail"]["regime"] == "unfit"
        assert last["detail"]["cell_state"] == "unfit"
        assert last["detail"]["cell_reason"] == "样本外衰减超标"
        assert last["detail"]["regime_hold_reason"] == "cell_state=unfit"

    def test_no_evidence_hold_is_a_distinct_reason_code(self):
        """「判不了」与「判为不适用」是两个码，不能并成一个。

        两者的补救动作不同（等重算 vs 等行情翻面），混码会让归因变成猜；而且
        用户故事 4 要的「阶段翻面后自动放行」得靠这个码知道自己为什么停着。
        """
        user = _user("regime-hold-noevidence")
        cand = self._held(
            user,
            reason_code="regime_no_evidence",
            detail={"regime": None, "regime_hold_reason": "no_usable_conclusion"},
            name="held-noev",
        )

        data = _client(user).get(candidate_url(cand.pk)).json()

        assert data["transitions"][-1]["reason_code"] == "regime_no_evidence"
        assert data["transitions"][-1]["detail"]["regime"] is None
        assert (
            data["transitions"][-1]["detail"]["regime_hold_reason"]
            == "no_usable_conclusion"
        )

    def test_exemption_leaves_a_trace(self):
        """豁免放行（D5）必须在契约面上留痕：它是**靠人放过去的**，不是「行情适用」。

        少了这一条，事后回看只会看到候选「正常升档了」，而那条结论的成色完全不同
        （spec 用户故事 7）。`exempt` 逐对点出命中的 `(策略 id, 阶段)`。
        """
        user = _user("regime-hold-exempt")
        detail = {
            "regime": "unfit",
            "exempt": [
                {
                    "strategy_id": "00000000-0000-0000-0000-0000000000aa",
                    "regime": "unfit",
                }
            ],
            "cell_state": "unfit",
        }
        cand = self._held(
            user, reason_code="regime_exempt", detail=detail, name="exempt"
        )

        data = _client(user).get(candidate_url(cand.pk)).json()

        last = data["transitions"][-1]
        assert last["reason_code"] == "regime_exempt"
        assert last["detail"]["exempt"] == [
            {
                "strategy_id": "00000000-0000-0000-0000-0000000000aa",
                "regime": "unfit",
            }
        ]


# --------------------------------------------------------------------------- #
# 3c. 回测资金效率的透出与降级契约（spec #30 用户故事 12，子票 #36）
# --------------------------------------------------------------------------- #


@pytest.mark.django_db
class TestBacktestCapitalEfficiencyContract:
    """回测响应里的 `metrics.capital_efficiency`：**整份 `metrics` 原样透出**，没有
    白名单（`apps/backtest/serializers.py` 的 `metrics` 就是一个裸 `ModelSerializer`
    字段）。所以这一票在序列化器上零改动——但「零改动」这件事本身需要一条用例钉住，
    否则将来任何人给 `metrics` 加一个 allowlist，前端会**静默**少一块，而没有一处变红。

    降级契约是这一组里更重要的一半：存量回测**压根没有这个键**（不是 null、不是
    `{}`）。前端的展示必须容得下这一种，所以后端要保证它仍然是 200、且键就是不出现
    ——而不是被补成一个 `null`（那会让前端多一种要处理的不存在状态）。
    """

    def _client(self):
        from django.contrib.auth import get_user_model
        from rest_framework.test import APIClient

        user = get_user_model().objects.create_user(
            email="t36-bt@test.com", username="t36_backtest", password="x"
        )
        client = APIClient()
        client.force_authenticate(user=user)
        return client, user

    def _result(self, user, *, metrics):
        import datetime
        from decimal import Decimal

        from apps.backtest.models import BacktestResult
        from apps.trading.models import Strategy

        strategy = Strategy.objects.create(name="t36-bt-s", code_path="/t36.py")
        return BacktestResult.objects.create(
            strategy=strategy,
            user=user,
            symbol="BTC/USDT",
            timeframe="1h",
            start_date=datetime.date(2024, 1, 1),
            end_date=datetime.date(2024, 1, 2),
            initial_capital=Decimal("10000"),
            final_capital=Decimal("10000"),
            total_return_pct=0.0,
            metrics=metrics,
        )

    def test_a_result_with_the_segment_surfaces_it_verbatim(self):
        """有钱效率段：六个契约键逐字可达，金额是 8 位定点**字符串**。

        形状取自 #35 的适配层（`capital_efficiency_slicing.payload_from_result`）：
        `version` + `computed_at` + 口径快照 + `notes`。这里按真实载荷造数据，而不是
        手写一个"差不多"的字典——形状一旦分叉，这条用例就是那个会响的地方。
        """
        client, user = self._client()
        payload = {
            "version": 1,
            "computed_at": "2026-06-01T00:00:00+00:00",
            "sample_threshold": 30,
            "computed": True,
            "reason": None,
            "attribution": {
                "open_trades_excluded": 1,
                "incomplete_close_rows": 0,
                "closed_trades": 4,
            },
            "notional_basis": "quantity * entry_price",
            "avg_capital_used": "2500.00000000",
            "capital_utilization": 25.0,
            "idle_pct": 75.0,
            "time_in_market_pct": 12.5,
            "return_per_capital_used_pct": 8.0,
            "sample_sufficient": False,
            "trade_count": 4,
            "occupied_seconds": 4500.0,
            "window_seconds": 36000.0,
            "pnl_total": "200.00000000",
            "notes": ["金额口径：notional = quantity × entry_price。"],
        }
        result = self._result(user, metrics={"capital_efficiency": payload})

        resp = client.get(f"/api/backtest/results/{result.id}/")
        assert resp.status_code == 200, resp.content
        data = resp.json()

        segment = data["metrics"]["capital_efficiency"]
        for key in (
            "avg_capital_used",
            "capital_utilization",
            "idle_pct",
            "time_in_market_pct",
            "return_per_capital_used_pct",
            "sample_sufficient",
        ):
            assert key in segment, f"契约键 {key} 没透出来"
        # 金额是定点字符串、百分比是 float（百分点）——两种类型的区别是前端格式化
        # 分支的依据，透出时不能变成同一种。
        assert segment["avg_capital_used"] == "2500.00000000"
        assert isinstance(segment["capital_utilization"], float)
        assert segment["capital_utilization"] == 25.0
        assert segment["notes"] == ["金额口径：notional = quantity × entry_price。"]
        # 口径快照随载荷走（D8）：没有它，看数字的人不知道 threshold 是哪一个
        assert segment["sample_threshold"] == 30
        assert segment["version"] == 1

    def test_an_old_result_without_the_key_is_still_200_and_the_key_is_absent(self):
        """**降级契约**：存量回测的 `metrics` 里没有这个键，响应照样 200。

        键「不存在」与键「是 null」是两件事：前者是「这份回测没算过资金效率」，后者
        会被读成「算过、结论是没有」。后端不许替老行补一个 null——补了就是替 #35
        的重算入口做了一个假的「已算过」标记，而 `is_stale` 正是按键存在与否判的。
        """
        client, user = self._client()
        # 老回测：metrics 里有别的键，**没有** capital_efficiency
        result = self._result(user, metrics={"sortino_ratio": 1.4, "calmar_ratio": 0.9})

        resp = client.get(f"/api/backtest/results/{result.id}/")
        assert resp.status_code == 200, resp.content
        data = resp.json()

        assert data["metrics"]["sortino_ratio"] == 1.4
        assert "capital_efficiency" not in data["metrics"]

    def test_metrics_can_be_entirely_empty_too(self):
        """另一种降级：`metrics` 整个是空的（甚至没有键）。

        前端的外层守卫 `detail.metrics && Object.keys(...).length > 0` 挡的是这一种，
        而内层的段守卫挡的是上一条。两种都要在契约面上存在，否则前端会以为其中一种
        不会发生，只写一半。
        """
        client, user = self._client()
        result = self._result(user, metrics={})

        resp = client.get(f"/api/backtest/results/{result.id}/")
        assert resp.status_code == 200, resp.content
        assert "capital_efficiency" not in (resp.json()["metrics"] or {})

    def test_the_detail_endpoint_also_passes_it_through(self):
        """详情端点（`.../detail/`，带曲线的那一个）走的是另一个序列化器。

        两个序列化器各自暴露裸 `metrics`，所以两条路径都得钉——只钉一条的话，
        另一条将来加了 allowlist 不会响，而前端在详情页上用的是这一条。
        """
        client, user = self._client()
        result = self._result(
            user,
            metrics={
                "capital_efficiency": {
                    "computed": True,
                    "reason": None,
                    "avg_capital_used": "10.00000000",
                    "capital_utilization": None,
                    "idle_pct": None,
                    "time_in_market_pct": None,
                    "return_per_capital_used_pct": None,
                    "sample_sufficient": False,
                    "notes": ["量不了时长"],
                    "version": 1,
                }
            },
        )

        resp = client.get(f"/api/backtest/results/{result.id}/detail/")
        assert resp.status_code == 200, resp.content
        segment = resp.json()["metrics"]["capital_efficiency"]
        # 不可用的比率是 **null**，不是 0——前端据此渲染「—」（0% 会被读成真数）。
        assert segment["capital_utilization"] is None
        assert segment["return_per_capital_used_pct"] is None
        assert segment["notes"] == ["量不了时长"]


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
