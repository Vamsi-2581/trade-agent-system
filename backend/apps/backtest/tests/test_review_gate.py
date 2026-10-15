"""审核闸门回归护栏（spec D12 前半）。

缺陷背景：`save_backtest_result` 在**新建与更新两条路径**上都直接写
`review_status="approved"` / `reviewed_at=now()`，而 `result_review` 接口要求
`review_status == "pending"` 才受理。于是每一条回测落库即「已通过」，人工审核这
道关在代码上不存在——`result_review` 永远 400，审核闸门是空转的。

本文件锁三件事：
1. 回测保存路径落 `pending`、不写 `reviewed_at`；
2. 更新路径（`existing_result_id`）不回写审核字段——不得抹掉一次已做完的人工审核；
3. 闸门真的拦人：刚回测完的结果不能直接拿去建会话，人工审核之后才行。
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

from apps.backtest.models import BacktestResult


def _stats(**overrides) -> dict:
    """save_backtest_result 需要的最小 stats 形状。"""
    base = {
        "final_equity": 10000.0,
        "total_return_pct": 0.0,
        "max_drawdown_pct": 0.0,
        "win_rate": 0.0,
        "sharpe_ratio": 0.0,
        "total_trades": 0,
        "equity_curve": [],
        "trades": [],
    }
    base.update(overrides)
    return base


async def _save(strategy_id: str, **overrides) -> dict:
    from apps.strategy_engine.backtest_mode import save_backtest_result

    kwargs = {
        "strategy_id": strategy_id,
        "symbol": "BTC/USDT",
        "timeframe": "1h",
        "start_date": "2024-01-01",
        "end_date": "2024-01-02",
        "initial_capital": Decimal("10000"),
        "parameters": {},
        "stats": _stats(),
    }
    kwargs.update(overrides)
    return await save_backtest_result(**kwargs)


class TestSaveBacktestResultReviewFields:
    """回测保存路径与审核字段的契约。

    `transaction=True` 沿用 `TestSlippageColumnRoundTrip` 的理由：`save_backtest_result`
    用 `sync_to_async` 在事件循环线程里写库，普通 `django_db` 的事务包在测试线程上，
    异步体的写会落在事务之外而**永久提交**，泄漏给整轮其余用例。
    """

    pytestmark = pytest.mark.django_db(transaction=True)

    @pytest.mark.asyncio
    async def test_new_result_lands_pending(self):
        """新建落库后是 pending、reviewed_at 为空——审核闸门有待审的输入。"""
        from apps.trading.models import Strategy

        strategy = await Strategy.objects.acreate(
            name="review_gate_new", code_path="strategies/review_gate_new.py"
        )
        saved = await _save(str(strategy.id))

        result = await BacktestResult.objects.aget(id=saved["result_id"])
        assert result.review_status == "pending"
        assert result.reviewed_at is None
        assert result.review_notes == ""

    @pytest.mark.asyncio
    async def test_update_path_keeps_human_review(self):
        """更新已有回测不得抹掉人工审核结论。

        更新路径若回写 `pending`，一次已做完的审批会被静默作废，而且用户看不到
        任何提示——这比原来的「无条件 approved」更难发现。
        """
        from django.utils import timezone

        from apps.trading.models import Strategy

        strategy = await Strategy.objects.acreate(
            name="review_gate_update", code_path="strategies/review_gate_update.py"
        )
        saved = await _save(str(strategy.id))

        # 模拟人工审核已经做完
        reviewed_at = timezone.now()
        await BacktestResult.objects.filter(id=saved["result_id"]).aupdate(
            review_status="approved",
            review_notes="样本外复核通过",
            reviewed_at=reviewed_at,
        )

        # 同一个 result 再跑一次回测（runner 的 existing_result_id 路径）
        await _save(
            str(strategy.id),
            existing_result_id=saved["result_id"],
            stats=_stats(total_return_pct=3.5, sharpe_ratio=1.8),
        )

        result = await BacktestResult.objects.aget(id=saved["result_id"])
        assert result.review_status == "approved"
        assert result.review_notes == "样本外复核通过"
        assert result.reviewed_at == reviewed_at
        # 回测数据本身仍然被更新（护栏不能顺手把功能关掉）
        assert result.total_return_pct == pytest.approx(3.5)


@pytest.mark.django_db
class TestResultReviewEndpoint:
    """`POST /api/backtest/results/<pk>/review/` 在真实链路上可用。

    改动前这条路径不可达：每条回测都是 approved，接口一律 400。
    """

    def _client(self):
        from django.contrib.auth import get_user_model
        from rest_framework.test import APIClient

        user = get_user_model().objects.create_user(
            email="reviewgate@test.com", username="review_gate", password="x"
        )
        client = APIClient()
        client.force_authenticate(user=user)
        return client, user

    def _result(self, user, review_status="pending", **kwargs) -> BacktestResult:
        from apps.trading.models import Strategy

        strategy = Strategy.objects.create(
            name=f"review_gate_{review_status}", code_path=f"/{review_status}.py"
        )
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
            review_status=review_status,
            **kwargs,
        )

    def test_pending_is_accepted_and_decided(self):
        client, user = self._client()
        result = self._result(user)

        resp = client.post(
            f"/api/backtest/results/{result.id}/review/",
            {"approved": True, "notes": "看过了"},
            format="json",
        )

        assert resp.status_code == 200, resp.content
        result.refresh_from_db()
        assert result.review_status == "approved"
        assert result.review_notes == "看过了"
        assert result.reviewed_at is not None

    def test_rejection_is_recorded(self):
        client, user = self._client()
        result = self._result(user)

        resp = client.post(
            f"/api/backtest/results/{result.id}/review/",
            {"approved": False, "notes": "样本外衰减太大"},
            format="json",
        )

        assert resp.status_code == 200, resp.content
        result.refresh_from_db()
        assert result.review_status == "rejected"
        assert result.review_notes == "样本外衰减太大"

    def test_already_decided_still_rejected(self):
        """既有 400 语义不变：已决结果不能被二次审核覆盖。"""
        client, user = self._client()
        result = self._result(user, review_status="approved")

        resp = client.post(
            f"/api/backtest/results/{result.id}/review/",
            {"approved": False},
            format="json",
        )

        assert resp.status_code == 400
        result.refresh_from_db()
        assert result.review_status == "approved"


@pytest.mark.django_db
class TestReviewGateActuallyBlocks:
    """闸门今天真的拦人：`live_session_create` 的审核前置是有牙齿的。

    改动前 `live_session_create` 里的 `review_status != "approved"` 是永假条件
    （每条回测都 approved），这段校验是死的。本类把消费者一侧的两个方向都钉住
    ——未审核拒、已审核过——这样后续收紧 paper 前置（P2/T6）时不会把审核这道
    关静默删掉。
    """

    URL = "/api/trading/sessions/create/"

    @staticmethod
    def _fake_adapter():
        """假适配器：建会话要取账户余额，返回固定值。"""
        from decimal import Decimal as D
        from unittest.mock import AsyncMock

        class FakeAdapter:
            def __init__(self, api_key, secret, testnet=False):
                self.testnet = testnet
                self.connect = AsyncMock()
                self.disconnect = AsyncMock()
                self.get_balance = AsyncMock(return_value={"USDT": D("2500.50")})

        return FakeAdapter

    def _setup(self, review_status: str):
        from cryptography.fernet import Fernet
        from django.conf import settings
        from django.contrib.auth import get_user_model
        from django.test import override_settings
        from rest_framework.test import APIClient

        from apps.exchange.models import ExchangeAccount
        from apps.trading.models import Strategy

        self._override = override_settings(FERNET_KEY=Fernet.generate_key().decode())
        self._override.enable()

        user = get_user_model().objects.create_user(
            email=f"gate-{review_status}@test.com",
            username=f"gate_{review_status}",
            password="x",
        )
        f = Fernet(settings.FERNET_KEY.encode())
        account = ExchangeAccount.objects.create(
            exchange="binance",
            label="gate",
            api_key_enc=f.encrypt(b"k"),
            api_secret_enc=f.encrypt(b"s"),
            testnet=True,
        )
        strategy = Strategy.objects.create(
            name=f"gate_strategy_{review_status}", code_path="strategies/gate.py"
        )
        backtest = BacktestResult.objects.create(
            strategy=strategy,
            user=user,
            symbol="BTC/USDT",
            timeframe="1h",
            start_date=datetime.date(2024, 1, 1),
            end_date=datetime.date(2024, 1, 2),
            initial_capital=Decimal("10000"),
            final_capital=Decimal("10000"),
            total_return_pct=0.0,
            review_status=review_status,
        )
        client = APIClient()
        client.force_authenticate(user=user)
        return client, account, backtest

    def teardown_method(self, method):
        override = getattr(self, "_override", None)
        if override is not None:
            override.disable()

    def _post_create(self, client, account, backtest):
        return client.post(
            self.URL,
            {
                "backtest_result_id": str(backtest.id),
                "mode": "paper",
                "exchange_account_id": str(account.id),
            },
            format="json",
        )

    def test_unreviewed_backtest_cannot_be_deployed(self):
        """pending（= 回测保存路径今天落下的状态）不得建会话。"""
        client, account, backtest = self._setup("pending")
        assert backtest.review_status == "pending"

        resp = self._post_create(client, account, backtest)

        assert resp.status_code == 400, resp.content
        assert "approved" in resp.json()["error"]

    def test_approved_backtest_passes_the_gate(self):
        """人工审核通过之后，同一个结果就能建会话——闸门是卡点，不是死路。"""
        from unittest.mock import patch

        client, account, backtest = self._setup("approved")

        with patch(
            "apps.trading.adapters.ADAPTER_MAP", {"binance": self._fake_adapter()}
        ):
            resp = self._post_create(client, account, backtest)

        assert resp.status_code == 201, resp.content
