"""Integration tests for grid search API."""

import pytest
from unittest.mock import patch, MagicMock

from django.contrib.auth import get_user_model

User = get_user_model()


@pytest.fixture
def api_client():
    from rest_framework.test import APIClient

    return APIClient()


@pytest.fixture
def user(db):
    return User.objects.create_user(
        username="griduser", email="griduser@example.com", password="testpass123"
    )


@pytest.fixture
def authenticated_client(api_client, user):
    api_client.force_authenticate(user=user)
    return api_client


@pytest.fixture
def strategy(db):
    from apps.trading.models import Strategy

    return Strategy.objects.create(
        name="TestGridStrategy",
        user_id=None,
    )


class TestGridSearchAPI:
    @pytest.mark.django_db
    def test_create_grid_search_success(self, authenticated_client, strategy):
        """成功提交网格搜索任务"""
        with patch("apps.backtest.views.run_grid_search_task") as mock_task:
            mock_apply = MagicMock()
            mock_apply.id = "test-task-uuid-1234"
            mock_task.apply_async.return_value = mock_apply

            response = authenticated_client.post(
                "/api/backtest/grid-search/",
                {
                    "strategy_id": str(strategy.id),
                    "symbol": "BTC/USDT",
                    "timeframe": "1h",
                    "grid_search": {
                        "parameters": {
                            "ma_fast": {"min": 5, "max": 9, "step": 2},
                            "ma_slow": {"min": 20, "max": 30, "step": 10},
                        },
                        "sort_by": "sharpe_ratio",
                    },
                },
                format="json",
            )
            assert response.status_code == 200
            data = response.json()
            assert "grid_search_id" in data
            assert "task_id" in data
            assert data["total_combinations"] == 6  # 3 × 2

    @pytest.mark.django_db
    def test_create_grid_search_missing_ranges(self, authenticated_client, strategy):
        """缺少参数范围应返回 400"""
        response = authenticated_client.post(
            "/api/backtest/grid-search/",
            {
                "strategy_id": str(strategy.id),
                "symbol": "BTC/USDT",
                "timeframe": "1h",
                "grid_search": {
                    "parameters": {"ma": {}},  # no min/max/step
                },
            },
            format="json",
        )
        assert response.status_code == 400

    @pytest.mark.django_db
    def test_create_grid_search_too_many_combos(self, authenticated_client, strategy):
        """组合数超过 100 应返回 400"""
        response = authenticated_client.post(
            "/api/backtest/grid-search/",
            {
                "strategy_id": str(strategy.id),
                "symbol": "BTC/USDT",
                "timeframe": "1h",
                "grid_search": {
                    "parameters": {
                        "a": {"min": 1, "max": 20, "step": 1},
                        "b": {"min": 1, "max": 20, "step": 1},
                    },
                },
            },
            format="json",
        )
        assert response.status_code == 400
        assert "超过上限" in response.json()["error"]

    @pytest.mark.django_db
    def test_user_isolation(self, authenticated_client, strategy):
        """用户不能访问其他人的网格搜索任务"""
        with patch("apps.backtest.views.run_grid_search_task") as mock_task:
            mock_apply = MagicMock()
            mock_apply.id = "test-task-uuid-isolation"
            mock_task.apply_async.return_value = mock_apply

            response = authenticated_client.post(
                "/api/backtest/grid-search/",
                {
                    "strategy_id": str(strategy.id),
                    "symbol": "BTC/USDT",
                    "timeframe": "1h",
                    "grid_search": {
                        "parameters": {"ma": {"min": 5, "max": 7, "step": 2}},
                    },
                },
                format="json",
            )
            assert response.status_code == 200
            job_id = response.json()["grid_search_id"]

        # 另一个用户查询
        other_user = User.objects.create_user(
            username="otheruser", password="testpass123"
        )
        from rest_framework.test import APIClient

        other_client = APIClient()
        other_client.force_authenticate(user=other_user)

        response = other_client.get(f"/api/backtest/grid-search/{job_id}/")
        assert response.status_code == 403

    @pytest.mark.django_db
    def test_cancel_grid_search(self, authenticated_client, strategy):
        """取消网格搜索任务"""
        with patch("apps.backtest.views.run_grid_search_task") as mock_task:
            mock_apply = MagicMock()
            mock_apply.id = "test-task-uuid-cancel"
            mock_task.apply_async.return_value = mock_apply

            response = authenticated_client.post(
                "/api/backtest/grid-search/",
                {
                    "strategy_id": str(strategy.id),
                    "symbol": "BTC/USDT",
                    "timeframe": "1h",
                    "grid_search": {
                        "parameters": {"ma": {"min": 5, "max": 7, "step": 2}},
                    },
                },
                format="json",
            )
            assert response.status_code == 200
            job_id = response.json()["grid_search_id"]

        # 取消任务
        with patch("apps.backtest.views.celery_app") as mock_celery:
            mock_celery.control.revoke = MagicMock()
            response = authenticated_client.post(
                f"/api/backtest/grid-search/{job_id}/cancel/"
            )
            assert response.status_code == 200
            assert "已取消" in response.json()["message"]


class TestNormalizeSymbol:
    """normalize_binance_symbol symbol 归一化"""

    @staticmethod
    def _norm(s):
        from apps.backtest.tasks import normalize_binance_symbol

        return normalize_binance_symbol(s)

    def test_already_normalized(self):
        assert self._norm("SOL/USDT") == "SOL/USDT"
        assert self._norm("WBTC/USDT") == "WBTC/USDT"

    def test_concatenated_exchange_format(self):
        assert self._norm("SOLUSDT") == "SOL/USDT"
        assert self._norm("ETHUSDC") == "ETH/USDC"
        assert self._norm("ETHBTC") == "ETH/BTC"
        assert self._norm("WBTCUSDT") == "WBTC/USDT"

    def test_separator_format(self):
        assert self._norm("SOL-USDT") == "SOL/USDT"
        assert self._norm("SOL_USDT") == "SOL/USDT"

    def test_bare_base_defaults_usdt(self):
        assert self._norm("SOL") == "SOL/USDT"
        assert self._norm("sol") == "SOL/USDT"
        assert self._norm(" 1000SHIB ") == "1000SHIB/USDT"

    def test_bare_quote_coin_is_base(self):
        # 裸报价币种本身（如 WBTC）不得被后缀规则误切
        assert self._norm("BTC") == "BTC/USDT"
        assert self._norm("ETH") == "ETH/USDT"
        assert self._norm("WBTC") == "WBTC/USDT"
        assert self._norm("USDC") == "USDC/USDT"

    def test_empty(self):
        assert self._norm("") == ""
