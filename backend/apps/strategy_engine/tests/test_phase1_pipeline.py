"""
Phase 1 新管线单元测试 — Insight / PortfolioTarget 数据校验。
"""

import pytest
from decimal import Decimal

from apps.strategy_engine.base import Insight, PortfolioTarget


class TestInsightValidation:
    """Insight 数据校验测试"""

    def test_valid_insight(self):
        insight = Insight(
            symbol="BTC/USDT",
            direction="buy",
            confidence=0.8,
            period="1h",
            source="TestStrategy",
            quantity=Decimal("0.01"),
        )
        assert insight.direction == "buy"
        assert insight.confidence == 0.8

    def test_invalid_direction(self):
        with pytest.raises(ValueError, match="Invalid direction"):
            Insight(
                symbol="BTC/USDT",
                direction="short",
                confidence=0.5,
                period="1h",
                source="Test",
            )

    def test_confidence_out_of_range_high(self):
        with pytest.raises(ValueError, match="confidence must be in"):
            Insight(
                symbol="BTC/USDT",
                direction="buy",
                confidence=1.5,
                period="1h",
                source="Test",
            )

    def test_confidence_out_of_range_low(self):
        with pytest.raises(ValueError, match="confidence must be in"):
            Insight(
                symbol="BTC/USDT",
                direction="buy",
                confidence=-0.1,
                period="1h",
                source="Test",
            )

    def test_confidence_boundary_values(self):
        Insight(
            symbol="BTC/USDT",
            direction="buy",
            confidence=0.0,
            period="1h",
            source="Test",
        )
        Insight(
            symbol="BTC/USDT",
            direction="buy",
            confidence=1.0,
            period="1h",
            source="Test",
        )

    def test_negative_quantity(self):
        with pytest.raises(ValueError, match="quantity must be positive"):
            Insight(
                symbol="BTC/USDT",
                direction="buy",
                confidence=0.5,
                period="1h",
                source="Test",
                quantity=Decimal("-1"),
            )

    def test_zero_quantity(self):
        with pytest.raises(ValueError, match="quantity must be positive"):
            Insight(
                symbol="BTC/USDT",
                direction="buy",
                confidence=0.5,
                period="1h",
                source="Test",
                quantity=Decimal("0"),
            )

    def test_quantity_none_allowed(self):
        insight = Insight(
            symbol="BTC/USDT",
            direction="hold",
            confidence=0.5,
            period="1h",
            source="Test",
            quantity=None,
        )
        assert insight.quantity is None

    def test_negative_price(self):
        with pytest.raises(ValueError, match="price must be positive"):
            Insight(
                symbol="BTC/USDT",
                direction="buy",
                confidence=0.5,
                period="1h",
                source="Test",
                price=Decimal("-100"),
            )

    def test_metadata_exceeds_1kb(self):
        large_metadata = {"key": "x" * 2000}
        with pytest.raises(ValueError, match="metadata exceeds 1KB limit"):
            Insight(
                symbol="BTC/USDT",
                direction="buy",
                confidence=0.5,
                period="1h",
                source="Test",
                metadata=large_metadata,
            )

    def test_metadata_within_limit(self):
        Insight(
            symbol="BTC/USDT",
            direction="buy",
            confidence=0.5,
            period="1h",
            source="Test",
            metadata={"small": "data"},
        )


class TestPortfolioTargetValidation:
    """PortfolioTarget 数据校验测试"""

    def test_valid_target(self):
        target = PortfolioTarget(
            symbol="BTC/USDT",
            target_quantity=Decimal("0.5"),
            reason="ma_cross",
        )
        assert target.target_quantity == Decimal("0.5")

    def test_negative_quantity_means_short(self):
        """负数 target_quantity 表示空头持仓（双向改造后合法）。

        改造前这里断言 ValueError——那时引擎只有 spot 单边语义，
        「目标持仓为负」是不可表达的非法状态。引入双向持仓后，
        负数成为空头的正式表达方式；「能不能开空」由策略的
        ``supports_short`` 能力声明把关，而不是由数据结构拒绝。
        """
        target = PortfolioTarget(
            symbol="BTC/USDT",
            target_quantity=Decimal("-1"),
            reason="short_entry",
        )
        assert target.target_quantity == Decimal("-1")

    def test_zero_quantity_allowed(self):
        """平仓目标：target_quantity=0 表示清仓"""
        target = PortfolioTarget(
            symbol="BTC/USDT",
            target_quantity=Decimal("0"),
            reason="close_position",
        )
        assert target.target_quantity == Decimal("0")

    def test_target_weight_out_of_range(self):
        with pytest.raises(ValueError, match="target_weight must be in"):
            PortfolioTarget(
                symbol="BTC/USDT",
                target_quantity=Decimal("0.5"),
                reason="test",
                target_weight=1.5,
            )

    def test_target_weight_valid(self):
        target = PortfolioTarget(
            symbol="BTC/USDT",
            target_quantity=Decimal("0.5"),
            reason="test",
            target_weight=0.5,
        )
        assert target.target_weight == 0.5
