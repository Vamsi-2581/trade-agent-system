"""Tests for backtest module."""

from __future__ import annotations

from decimal import Decimal
from datetime import date, datetime

import pytest
from django.test import TestCase

from apps.backtest.models import BacktestResult, BacktestTrade


class TestBacktestResultModel(TestCase):
    """测试回测结果模型"""

    def setUp(self):
        from apps.trading.models import Strategy

        self.strategy = Strategy.objects.create(
            name="Test Strategy",
            code_path="strategies/test.py",
        )

    def test_create_backtest_result(self):
        result = BacktestResult.objects.create(
            strategy=self.strategy,
            symbol="BTC/USDT",
            timeframe="1h",
            start_date=date(2024, 1, 1),
            end_date=date(2024, 6, 1),
            initial_capital=Decimal("10000.00"),
            final_capital=Decimal("12000.00"),
            total_return_pct=20.0,
            sharpe_ratio=1.5,
            max_drawdown_pct=-5.0,
            win_rate=0.65,
            total_trades=100,
        )
        self.assertEqual(result.symbol, "BTC/USDT")
        self.assertEqual(result.total_return_pct, 20.0)
        self.assertEqual(result.sharpe_ratio, 1.5)
        self.assertEqual(result.win_rate, 0.65)

    def test_backtest_result_with_parameters(self):
        """测试带参数的回测结果"""
        result = BacktestResult.objects.create(
            strategy=self.strategy,
            symbol="ETH/USDT",
            timeframe="4h",
            start_date=date(2024, 1, 1),
            end_date=date(2024, 3, 1),
            initial_capital=Decimal("5000.00"),
            final_capital=Decimal("5500.00"),
            total_return_pct=10.0,
            parameters={"rsi_period": 14, "ma_fast": 10, "ma_slow": 30},
        )
        self.assertEqual(result.parameters["rsi_period"], 14)
        self.assertEqual(result.parameters["ma_fast"], 10)

    def test_backtest_result_git_commit(self):
        """测试带 git commit hash 的回测结果"""
        result = BacktestResult.objects.create(
            strategy=self.strategy,
            symbol="BTC/USDT",
            timeframe="1d",
            start_date=date(2024, 1, 1),
            end_date=date(2024, 12, 31),
            initial_capital=Decimal("10000.00"),
            final_capital=Decimal("10000.00"),
            total_return_pct=0.0,
            git_commit_hash="abc123def456",
        )
        self.assertEqual(result.git_commit_hash, "abc123def456")

    def test_backtest_result_optional_fields(self):
        """测试可选字段为 None 的情况"""
        result = BacktestResult.objects.create(
            strategy=self.strategy,
            symbol="SOL/USDT",
            timeframe="15m",
            start_date=date(2024, 6, 1),
            end_date=date(2024, 6, 2),
            initial_capital=Decimal("1000.00"),
            final_capital=Decimal("1000.00"),
            total_return_pct=0.0,
            sharpe_ratio=None,
            max_drawdown_pct=None,
            win_rate=None,
        )
        self.assertIsNone(result.sharpe_ratio)
        self.assertIsNone(result.max_drawdown_pct)
        self.assertIsNone(result.win_rate)

    def test_backtest_result_equity_curve(self):
        """测试权益曲线 JSON 字段"""
        curve = [
            {"timestamp": "2024-01-01T00:00:00Z", "equity": 10000.0, "drawdown": 0.0},
            {
                "timestamp": "2024-01-02T00:00:00Z",
                "equity": 10150.0,
                "drawdown": -0.002,
            },
        ]
        dd_curve = [
            {"timestamp": "2024-01-01T00:00:00Z", "drawdown": 0.0},
            {"timestamp": "2024-01-02T00:00:00Z", "drawdown": -0.002},
        ]
        result = BacktestResult.objects.create(
            strategy=self.strategy,
            symbol="BTC/USDT",
            timeframe="1d",
            start_date=date(2024, 1, 1),
            end_date=date(2024, 1, 31),
            initial_capital=Decimal("10000.00"),
            final_capital=Decimal("10150.00"),
            total_return_pct=1.5,
            equity_curve=curve,
            drawdown_curve=dd_curve,
        )
        self.assertEqual(len(result.equity_curve), 2)
        self.assertEqual(result.equity_curve[0]["equity"], 10000.0)
        self.assertEqual(len(result.drawdown_curve), 2)

    def test_backtest_trade(self):
        """测试交易日志模型"""
        result = BacktestResult.objects.create(
            strategy=self.strategy,
            symbol="BTC/USDT",
            timeframe="1h",
            start_date=date(2024, 1, 1),
            end_date=date(2024, 1, 31),
            initial_capital=Decimal("10000.00"),
            final_capital=Decimal("10500.00"),
            total_return_pct=5.0,
        )
        # `BacktestTrade` 无 symbol 字段（标的在 backtest 外键上），成本也由
        # `commission` + `slippage` 两栏承载而非单一 `fees`。
        trade = BacktestTrade.objects.create(
            backtest=result,
            entry_time=datetime(2024, 1, 5, 8, 0),
            exit_time=datetime(2024, 1, 6, 14, 0),
            side="long",
            entry_price=Decimal("42000.00"),
            exit_price=Decimal("42800.00"),
            quantity=Decimal("0.5"),
            pnl=Decimal("400.00"),
            pnl_pct=1.90,
            commission=Decimal("4.20"),
            slippage=Decimal("0.50"),
            exit_reason="take_profit",
        )
        self.assertEqual(trade.side, "long")
        self.assertEqual(trade.pnl, Decimal("400.00"))
        self.assertEqual(trade.exit_reason, "take_profit")
        self.assertEqual(trade.backtest.symbol, "BTC/USDT")

    def test_backtest_trade_related_name(self):
        """测试通过 backtest.trades 反向查询"""
        result = BacktestResult.objects.create(
            strategy=self.strategy,
            symbol="ETH/USDT",
            timeframe="4h",
            start_date=date(2024, 1, 1),
            end_date=date(2024, 1, 31),
            initial_capital=Decimal("5000.00"),
            final_capital=Decimal("5200.00"),
            total_return_pct=4.0,
        )
        for i in range(3):
            BacktestTrade.objects.create(
                backtest=result,
                entry_time=datetime(2024, 1, 5 + i, 8, 0),
                exit_time=datetime(2024, 1, 6 + i, 14, 0),
                side="long" if i % 2 == 0 else "short",
                entry_price=Decimal("2200.00"),
                exit_price=Decimal("2250.00"),
                quantity=Decimal("1.0"),
                pnl=Decimal("50.00"),
            )
        self.assertEqual(result.trades.count(), 3)
        self.assertEqual(result.trades.filter(side="long").count(), 2)


def test_serializer_includes_metrics_field():
    """BacktestResultSerializer and BacktestDetailSerializer must include metrics field."""
    from apps.backtest.serializers import (
        BacktestResultSerializer,
        BacktestDetailSerializer,
    )

    assert "metrics" in BacktestResultSerializer.Meta.fields
    assert "metrics" in BacktestDetailSerializer.Meta.fields


@pytest.mark.django_db
def test_grouped_list_returns_groups():
    """GET /api/backtest/results/?grouped=1 returns grouped response shape."""
    from rest_framework.test import APIClient
    from django.contrib.auth import get_user_model

    User = get_user_model()
    user = User.objects.create_user(
        email="test@example.com", username="testuser", password="testpass123"
    )
    client = APIClient()
    client.force_authenticate(user=user)

    resp = client.get("/api/backtest/results/?grouped=1")
    assert resp.status_code == 200
    data = resp.json()
    assert "groups" in data
    assert "group_count" in data
    assert "total_records" in data
    assert "num_pages" in data
    assert "current_page" in data
    assert isinstance(data["groups"], list)


@pytest.mark.django_db
def test_grouped_list_grid_search_group():
    """Grouped API should group BacktestResults by grid_search_id."""
    from rest_framework.test import APIClient
    from django.contrib.auth import get_user_model
    from apps.backtest.models import GridSearchJob, BacktestResult
    from apps.trading.models import Strategy
    from datetime import date, timedelta

    User = get_user_model()
    user = User.objects.create_user(
        email="test@example.com", username="testuser", password="testpass123"
    )
    strategy = Strategy.objects.create(
        name="TestStrategy", code_path="/test", git_commit_hash="abc123"
    )
    job = GridSearchJob.objects.create(
        strategy=strategy,
        symbol="BTC/USDT",
        timeframe="1h",
        start_date=date.today() - timedelta(days=30),
        end_date=date.today(),
        initial_capital=10000,
        user=user,
        search_config={"parameters": {}},
    )
    _r1 = BacktestResult.objects.create(
        strategy=strategy,
        user=user,
        symbol="BTC/USDT",
        timeframe="1h",
        start_date=date.today() - timedelta(days=30),
        end_date=date.today(),
        initial_capital=10000,
        final_capital=11000,
        total_return_pct=10.0,
        sharpe_ratio=1.5,
        grid_search_id=job.id,
        is_grid_search=True,
        parameters={"period": 20},
    )
    _r2 = BacktestResult.objects.create(
        strategy=strategy,
        user=user,
        symbol="BTC/USDT",
        timeframe="1h",
        start_date=date.today() - timedelta(days=30),
        end_date=date.today(),
        initial_capital=10000,
        final_capital=10500,
        total_return_pct=5.0,
        sharpe_ratio=1.0,
        grid_search_id=job.id,
        is_grid_search=True,
        parameters={"period": 26},
    )

    client = APIClient()
    client.force_authenticate(user=user)
    resp = client.get("/api/backtest/results/?grouped=1")
    assert resp.status_code == 200
    data = resp.json()

    # Should have at least one grid_search group
    grid_groups = [g for g in data["groups"] if g["type"] == "grid_search"]
    assert len(grid_groups) >= 1
    group = grid_groups[0]
    assert group["job_id"] == str(job.id)
    assert group["symbol"] == "BTC/USDT"
    assert len(group["results"]) == 2
    # Results should have metrics field
    assert "metrics" in group["results"][0]


@pytest.mark.django_db
def test_grouped_list_single_backtest():
    """Non-grid-search results should appear as single groups."""
    from rest_framework.test import APIClient
    from django.contrib.auth import get_user_model
    from apps.backtest.models import BacktestResult
    from apps.trading.models import Strategy
    from datetime import date, timedelta

    User = get_user_model()
    user = User.objects.create_user(
        email="test@example.com", username="testuser", password="testpass123"
    )
    strategy = Strategy.objects.create(
        name="SingleStrategy", code_path="/test", git_commit_hash="abc123"
    )
    BacktestResult.objects.create(
        strategy=strategy,
        user=user,
        symbol="ETH/USDT",
        timeframe="4h",
        start_date=date.today() - timedelta(days=30),
        end_date=date.today(),
        initial_capital=10000,
        final_capital=10800,
        total_return_pct=8.0,
        sharpe_ratio=1.2,
        is_grid_search=False,
        parameters={"rsi_period": 14},
    )

    client = APIClient()
    client.force_authenticate(user=user)
    resp = client.get("/api/backtest/results/?grouped=1")
    assert resp.status_code == 200
    data = resp.json()

    singles = [g for g in data["groups"] if g["type"] == "single"]
    assert len(singles) >= 1
    assert singles[0]["result"]["symbol"] == "ETH/USDT"
