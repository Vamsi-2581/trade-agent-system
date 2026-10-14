"""Tests for Agent routing correctness — fallback rules map to Agent names directly."""

import pytest

from apps.agent.supervisor import IntentRouter


@pytest.fixture(autouse=True)
def reset_router():
    """Reset IntentRouter singleton between tests."""
    IntentRouter.reset()
    yield
    IntentRouter.reset()


class TestAgentRoutingFallbackRules:
    """Verify that fallback rules route to the correct Agent (not intermediate intent names)."""

    def _setup_default_rules(self, router: IntentRouter):
        """Apply the current production fallback rules."""
        router.register_fallback_rules(
            [
                (r"(分析|行情|走势|K线|趋势).*(BTC|ETH|币|市场)", "analyst"),
                (r"(回测|测试策略|历史数据|backtest)", "quant"),
                (r"(风险|止损|仓位|风控)", "risk_advisor"),
                (r"(计划|复盘|总结|周报)", "coach"),
                (r"(实现.*策略|创建.*策略|编写.*策略|生成.*策略代码)", "quant"),
                (
                    r"(研究|调研|收集.*资料|查找.*知识|搜索.*信息|内容研究|找.*策略|搜索.*策略)",
                    "researcher",
                ),
                (
                    r"(价格|突破|跌破|高于|低于|提醒|通知|监控).*(BTC|ETH|币|\d{4,})",
                    "supervisor",
                ),
            ]
        )

    def test_research_strategy_goes_to_researcher(self):
        """'在网上找一个基于btc5分钟线的超短线交易策略' → researcher"""
        router = IntentRouter.get_instance()
        self._setup_default_rules(router)
        result = router.match_fallback("在网上找一个基于btc5分钟线的超短线交易策略")
        assert result == "researcher", f"Expected 'researcher', got '{result}'"

    def test_research_strategy_goes_to_researcher_v2(self):
        """'搜索BTC交易策略' → researcher"""
        router = IntentRouter.get_instance()
        self._setup_default_rules(router)
        result = router.match_fallback("搜索BTC交易策略")
        assert result == "researcher", f"Expected 'researcher', got '{result}'"

    def test_implement_strategy_goes_to_quant(self):
        """'实现这个策略' → quant"""
        router = IntentRouter.get_instance()
        self._setup_default_rules(router)
        result = router.match_fallback("实现这个策略")
        assert result == "quant", f"Expected 'quant', got '{result}'"

    def test_create_strategy_goes_to_quant(self):
        """'创建一个RSI策略' → quant"""
        router = IntentRouter.get_instance()
        self._setup_default_rules(router)
        result = router.match_fallback("创建一个RSI策略")
        assert result == "quant", f"Expected 'quant', got '{result}'"

    def test_backtest_goes_to_quant(self):
        """'回测这个策略' → quant"""
        router = IntentRouter.get_instance()
        self._setup_default_rules(router)
        result = router.match_fallback("回测这个策略")
        assert result == "quant", f"Expected 'quant', got '{result}'"

    def test_backtest_english_goes_to_quant(self):
        """'backtest this strategy' → quant"""
        router = IntentRouter.get_instance()
        self._setup_default_rules(router)
        result = router.match_fallback("backtest this strategy")
        assert result == "quant", f"Expected 'quant', got '{result}'"

    def test_analyze_market_goes_to_analyst(self):
        """'分析BTC行情' → analyst"""
        router = IntentRouter.get_instance()
        self._setup_default_rules(router)
        result = router.match_fallback("分析BTC行情")
        assert result == "analyst", f"Expected 'analyst', got '{result}'"

    def test_risk_goes_to_risk_advisor(self):
        """'评估风险' → risk_advisor"""
        router = IntentRouter.get_instance()
        self._setup_default_rules(router)
        result = router.match_fallback("评估风险")
        assert result == "risk_advisor", f"Expected 'risk_advisor', got '{result}'"

    def test_summary_goes_to_coach(self):
        """'做个周报总结' → coach"""
        router = IntentRouter.get_instance()
        self._setup_default_rules(router)
        result = router.match_fallback("做个周报总结")
        assert result == "coach", f"Expected 'coach', got '{result}'"

    def test_price_alert_goes_to_supervisor(self):
        """'BTC突破75000通知我' → supervisor"""
        router = IntentRouter.get_instance()
        self._setup_default_rules(router)
        result = router.match_fallback("BTC突破75000通知我")
        assert result == "supervisor", f"Expected 'supervisor', got '{result}'"

    def test_no_match_returns_none(self):
        """Non-matching text returns None"""
        router = IntentRouter.get_instance()
        self._setup_default_rules(router)
        result = router.match_fallback("你好，今天天气怎么样")
        assert result is None
