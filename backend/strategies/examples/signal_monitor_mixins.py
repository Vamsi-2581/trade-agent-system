"""
策略预筛选信号配置示例。

策略可以通过实现 get_watch_signals() 方法来启用信号预筛选。
SignalMonitor会先检测这些最小周期信号，触发后再运行完整策略验证。
"""



class SignalMonitorMixin:
    """
    Mixin类：为策略添加SignalMonitor支持。

    使用方式:
        class MyStrategy(SignalMonitorMixin, BaseStrategy):
            def get_watch_signals(self):
                return super().get_watch_signals() + [
                    # 你的信号配置
                ]
    """

    def get_watch_signals(self):
        """
        返回策略的预筛选信号配置。

        返回格式：
        [
            {
                "interval": "15m",           # K线周期
                "indicator_type": "rsi",    # 指标类型
                "indicator_params": {"period": 14, "overbought": 70, "oversold": 30},
                "condition": {                # 触发条件
                    "operator": "cross_below",
                    "left": {"field": "rsi", "source": "indicator"},
                    "right": {"field": "oversold", "source": "param"},
                },
                "trigger_type": "continuous",  # "once" 或 "continuous"
            }
        ]
        """
        return []


class MeanReversionMixin:
    """均值回归策略的信号配置Mixin - RSI超买超卖"""

    def get_watch_signals(self):
        params = self.params if hasattr(self, 'params') else {}
        period = params.get("rsi_period", 14)
        overbought = params.get("rsi_overbought", 70)
        oversold = params.get("rsi_oversold", 30)
        interval = params.get("signal_interval", "15m")

        return [
            {
                "interval": interval,
                "indicator_type": "rsi",
                "indicator_params": {"period": period, "overbought": overbought, "oversold": oversold},
                "condition": {
                    "operator": "cross_below",
                    "left": {"field": "rsi", "source": "indicator"},
                    "right": {"field": "oversold", "source": "param"},
                },
                "trigger_type": "continuous",
            },
            {
                "interval": interval,
                "indicator_type": "rsi",
                "indicator_params": {"period": period, "overbought": overbought, "oversold": oversold},
                "condition": {
                    "operator": "cross_above",
                    "left": {"field": "rsi", "source": "indicator"},
                    "right": {"field": "overbought", "source": "param"},
                },
                "trigger_type": "continuous",
            },
        ]


class TrendFollowingMixin:
    """趋势跟踪策略的信号配置Mixin - EMA交叉"""

    def get_watch_signals(self):
        params = self.params if hasattr(self, 'params') else {}
        fast_period = params.get("ema_fast", 9)
        slow_period = params.get("ema_slow", 21)
        interval = params.get("signal_interval", "1h")

        return [
            {
                "interval": interval,
                "indicator_type": "ema_cross",
                "indicator_params": {"fast": fast_period, "slow": slow_period},
                "condition": {
                    "operator": "cross_above",
                    "left": {"field": "ema_fast", "source": "indicator"},
                    "right": {"field": "ema_slow", "source": "indicator"},
                },
                "trigger_type": "continuous",
            },
            {
                "interval": interval,
                "indicator_type": "ema_cross",
                "indicator_params": {"fast": fast_period, "slow": slow_period},
                "condition": {
                    "operator": "cross_below",
                    "left": {"field": "ema_fast", "source": "indicator"},
                    "right": {"field": "ema_slow", "source": "indicator"},
                },
                "trigger_type": "continuous",
            },
        ]


class DonchianBreakoutMixin:
    """Donchian通道突破策略的信号配置Mixin"""

    def get_watch_signals(self):
        params = self.params if hasattr(self, 'params') else {}
        period = params.get("donchian_period", 20)
        interval = params.get("signal_interval", "15m")

        return [
            {
                "interval": interval,
                "indicator_type": "donchian",
                "indicator_params": {"period": period},
                "condition": {
                    "operator": "cross_above",
                    "left": {"field": "close", "source": "price"},
                    "right": {"field": "upper", "source": "indicator"},
                },
                "trigger_type": "continuous",
            },
            {
                "interval": interval,
                "indicator_type": "donchian",
                "indicator_params": {"period": period},
                "condition": {
                    "operator": "cross_below",
                    "left": {"field": "close", "source": "price"},
                    "right": {"field": "lower", "source": "indicator"},
                },
                "trigger_type": "continuous",
            },
        ]


class BollingerBandsMixin:
    """布林带策略的信号配置Mixin"""

    def get_watch_signals(self):
        params = self.params if hasattr(self, 'params') else {}
        period = params.get("bb_period", 20)
        std_dev = params.get("bb_std", 2)
        interval = params.get("signal_interval", "15m")

        return [
            {
                "interval": interval,
                "indicator_type": "bollinger",
                "indicator_params": {"period": period, "std_dev": std_dev},
                "condition": {
                    "operator": "cross_below",
                    "left": {"field": "close", "source": "price"},
                    "right": {"field": "lower", "source": "indicator"},
                },
                "trigger_type": "continuous",
            },
            {
                "interval": interval,
                "indicator_type": "bollinger",
                "indicator_params": {"period": period, "std_dev": std_dev},
                "condition": {
                    "operator": "cross_above",
                    "left": {"field": "close", "source": "price"},
                    "right": {"field": "upper", "source": "indicator"},
                },
                "trigger_type": "continuous",
            },
        ]