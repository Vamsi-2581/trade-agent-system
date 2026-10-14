import pytest


@pytest.mark.django_db
def test_flat_mode_still_works():
    """Verify flat mode (without grouped parameter) still works."""
    from rest_framework.test import APIClient
    from django.contrib.auth import get_user_model
    from apps.backtest.models import BacktestResult
    from apps.trading.models import Strategy
    from datetime import date, timedelta

    User = get_user_model()
    user = User.objects.create_user(
        email="flat@example.com", username="flatuser", password="testpass123"
    )
    strategy = Strategy.objects.create(
        name="FlatTest", code_path="/test", git_commit_hash="abc123"
    )

    BacktestResult.objects.create(
        strategy=strategy,
        user=user,
        symbol="BTC/USDT",
        timeframe="1h",
        start_date=date.today() - timedelta(days=10),
        end_date=date.today(),
        initial_capital=10000,
        final_capital=10500,
        total_return_pct=5.0,
        sharpe_ratio=1.0,
        is_grid_search=False,
        parameters={"test": 1},
    )

    client = APIClient()
    client.force_authenticate(user=user)

    # Test flat mode (no grouped parameter)
    resp = client.get("/api/backtest/results/")
    assert resp.status_code == 200
    data = resp.json()
    assert "count" in data
    assert "num_pages" in data
    assert "current_page" in data
    assert "results" in data
    assert isinstance(data["results"], list)
