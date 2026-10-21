"""双向持仓（做空 / 按量平仓）引擎契约测试。

覆盖 2026-10 引入的 ``supports_short`` 能力开关：

1. **只做多策略行为不变**（回归保护）——这是本次改造最重要的约束：
   现有 box / rsi_cross / ema / donchian 等策略不声明 supports_short，
   它们的 ``ctx.sell()`` 必须仍然被翻译成「清仓」，卖出量必须仍被
   ``min(qty, position)`` 钳制（超出部分丢弃，不会开空）。

2. **双向策略**：
   - 空仓卖出 → 开出空头（``_position`` 变负）
   - 持多卖出超过持仓 → 先平多、剩余开空（穿越零点）
   - 持空买入 → 先回补、剩余开多（穿越零点）
   - ``close_position()`` 多头卖出 / 空头买入，都清空
   - 空头 PnL = (开空价 - 回补价) × 数量
   - ``_position_value`` 对空头返回负市值（权益曲线正确）

3. **目标持仓翻译**：``_construct_portfolio_signed`` 的增量语义，
   以及 ``close_position()`` 的 is_close_position 标志仍翻译成 target=0。
"""

from decimal import Decimal

import pytest

from apps.strategy_engine.base import (
    BaseStrategy,
    OrderSignal,
    PortfolioTarget,
    StrategyContext,
    _signal_to_insight,
)


class _LongOnlyStrategy(BaseStrategy):
    """默认只做多（supports_short 未声明 = False）。"""

    name = "long_only"
    description = "策略类型：测试\n核心指标：无\n适用场景：单测\n入场逻辑：固定脚本"

    def on_bar(self, kline, history):
        return None


class _ShortCapableStrategy(_LongOnlyStrategy):
    """显式声明支持双向。"""

    name = "short_capable"
    supports_short = True


def _ctx(position="0", balance="10000") -> StrategyContext:
    return StrategyContext(
        symbol="BTC/USDT",
        timeframe="15m",
        mode="backtest",
        params={},
        balance=Decimal(balance),
        position=Decimal(position),
    )


def _kline(ts="2026-10-01T00:00:00+00:00", close=50000.0) -> dict:
    return {
        "timestamp": ts,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": 1.0,
    }


def _engine(strategy, initial="10000", short_margin_rate="0"):
    """构造回测引擎。

    ``short_margin_rate`` 默认传 0（不约束保证金）：本文件测的是**做空语义**
    （净持仓符号、穿越零点、PnL 公式、目标翻译），保证金是正交的另一层约束，
    由 ``TestShortMargin`` 专门覆盖。混在一起会让「卖 2 手开空」这类用例
    因现金不足变成部分成交，从而测不到它本来想测的东西。
    """
    from apps.strategy_engine.backtest_mode import BacktestEngine

    return BacktestEngine(
        strategy=strategy,
        ohlcv_data=[_kline()],
        initial_capital=Decimal(initial),
        symbol="BTC/USDT",
        timeframe="15m",
        commission_rate=Decimal("0"),
        slippage_rate=Decimal("0"),
        short_margin_rate=Decimal(short_margin_rate),
    )


# ══════════════════════════════════════════════════════════════
# 1. 只做多：行为必须与改造前逐位一致
# ══════════════════════════════════════════════════════════════


class TestLongOnlyUnchanged:
    def test_sell_without_position_creates_no_target(self):
        """空仓 sell：只做多策略不产生任何 target（不会开空）。"""
        strat = _LongOnlyStrategy(_ctx("0"))
        insight = _signal_to_insight(
            strat.ctx.sell(Decimal("1"), signal_name="short_entry"), strat.name
        )
        insight.symbol = "BTC/USDT"
        assert strat.construct_portfolio([insight], strat.ctx) == []

    def test_sell_with_long_position_flattens_to_zero(self):
        """持多 sell：翻译成 target=0（清仓），不是减仓。"""
        strat = _LongOnlyStrategy(_ctx("2"))
        insight = _signal_to_insight(
            strat.ctx.sell(Decimal("1"), signal_name="tp1"), strat.name
        )
        insight.symbol = "BTC/USDT"
        targets = strat.construct_portfolio([insight], strat.ctx)
        assert len(targets) == 1
        assert targets[0].target_quantity == Decimal("0")

    def test_sell_never_drives_position_negative(self):
        """只做多引擎：卖出量被 min(qty, position) 钳制，持仓不会变负。"""
        strat = _LongOnlyStrategy(_ctx("0"))
        engine = _engine(strat)
        engine._position = Decimal("1")
        engine._execute_sell(
            OrderSignal(side="sell", quantity=Decimal("5")),
            Decimal("50000"),
            _kline(),
        )
        assert engine._position == Decimal("0"), "只做多策略不得因超额卖出变空头"

    def test_buy_does_not_cover_short_for_long_only(self):
        """只做多引擎没有空头概念，买入就是开多。"""
        strat = _LongOnlyStrategy(_ctx("0"))
        engine = _engine(strat, initial="100000")
        engine._execute_buy(
            OrderSignal(side="buy", quantity=Decimal("1")),
            Decimal("50000"),
            _kline(),
        )
        assert engine._position == Decimal("1")


# ══════════════════════════════════════════════════════════════
# 2. 双向：做空 / 穿越零点
# ══════════════════════════════════════════════════════════════


class TestShortEntry:
    def test_empty_sell_opens_short(self):
        """空仓卖出 → 开出空头（净持仓变负）。"""
        strat = _ShortCapableStrategy(_ctx("0"))
        engine = _engine(strat)
        engine._execute_sell(
            OrderSignal(side="sell", quantity=Decimal("2")),
            Decimal("50000"),
            _kline(),
        )
        assert engine._position == Decimal("-2")

    def test_short_open_records_short_trade(self):
        """开空要落一条 side='short'、trade_type='open' 的记录。"""
        strat = _ShortCapableStrategy(_ctx("0"))
        engine = _engine(strat)
        engine._execute_sell(
            OrderSignal(side="sell", quantity=Decimal("2")),
            Decimal("50000"),
            _kline(),
        )
        opens = [t for t in engine._trades if t["trade_type"] == "open"]
        assert len(opens) == 1
        assert opens[0]["side"] == "short"
        assert opens[0]["quantity"] == 2.0

    def test_sell_beyond_long_flips_to_short(self):
        """持多 1 卖 3 → 先平多 1，剩余 2 开空（净持仓 -2）。"""
        strat = _ShortCapableStrategy(_ctx("1"))
        engine = _engine(strat)
        engine._position = Decimal("1")
        engine._avg_entry_price = Decimal("48000")
        engine._execute_sell(
            OrderSignal(side="sell", quantity=Decimal("3")),
            Decimal("50000"),
            _kline(),
        )
        assert engine._position == Decimal("-2")
        sides = [t["side"] for t in engine._trades]
        assert "long" in sides and "short" in sides, (
            "穿越零点必须拆成平多 + 开空两条记录"
        )


class TestShortCover:
    def test_buy_covers_short(self):
        """持空 -2 买入 2 → 回到空仓。"""
        strat = _ShortCapableStrategy(_ctx("0"))
        engine = _engine(strat)
        engine._position = Decimal("-2")
        engine._avg_entry_price = Decimal("52000")
        engine._execute_buy(
            OrderSignal(side="buy", quantity=Decimal("2")),
            Decimal("50000"),
            _kline(),
        )
        assert engine._position == Decimal("0")

    def test_short_pnl_is_profit_when_price_falls(self):
        """空头在价格下跌时盈利：(开空价 - 回补价) × 数量。"""
        strat = _ShortCapableStrategy(_ctx("0"))
        engine = _engine(strat)
        engine._position = Decimal("-1")
        engine._avg_entry_price = Decimal("52000")
        engine._execute_buy(
            OrderSignal(side="buy", quantity=Decimal("1")),
            Decimal("50000"),
            _kline(),
        )
        closes = [t for t in engine._trades if t["trade_type"] == "close"]
        assert len(closes) == 1
        assert closes[0]["pnl"] == pytest.approx(2000.0), "空头跌 2000 应盈利 2000"

    def test_buy_beyond_short_flips_to_long(self):
        """持空 -1 买 3 → 先回补 1，剩余 2 开多（净持仓 +2）。"""
        strat = _ShortCapableStrategy(_ctx("0"))
        engine = _engine(strat, initial="1000000")
        engine._position = Decimal("-1")
        engine._avg_entry_price = Decimal("52000")
        engine._execute_buy(
            OrderSignal(side="buy", quantity=Decimal("3")),
            Decimal("50000"),
            _kline(),
        )
        assert engine._position == Decimal("2")


class TestShortValuation:
    def test_position_value_negative_for_short(self):
        """空头市值必须为负，否则权益曲线漏掉浮亏、回撤失真。"""
        strat = _ShortCapableStrategy(_ctx("0"))
        engine = _engine(strat)
        engine._position = Decimal("-2")
        value = engine._position_value(_kline(close=50000.0))
        assert value == Decimal("-100000")

    def test_equity_drops_when_short_loses(self):
        """空头逆势：现金增加但持仓市值更负，权益应下降。"""
        strat = _ShortCapableStrategy(_ctx("0"))
        engine = _engine(strat, initial="10000")
        engine._execute_sell(
            OrderSignal(side="sell", quantity=Decimal("0.1")),
            Decimal("50000"),
            _kline(),
        )
        cash_after = engine._cash
        equity_at_entry = cash_after + engine._position_value(_kline(close=50000.0))
        equity_after_rise = cash_after + engine._position_value(_kline(close=55000.0))
        assert equity_after_rise < equity_at_entry, "空头遇价格上涨，权益必须下降"
        assert equity_at_entry == pytest.approx(10000.0, abs=1.0)


class TestClosePositionBothSides:
    def test_close_long_sells(self):
        ctx = _ctx("2")
        sig = ctx.close_position(signal_name="exit")
        assert sig is not None and sig.side == "sell"
        assert sig.quantity == Decimal("2")
        assert sig.is_close_position is True

    def test_close_short_buys(self):
        """空头平仓是买入回补，不是卖出。"""
        ctx = _ctx("-2")
        sig = ctx.close_position(signal_name="exit")
        assert sig is not None and sig.side == "buy"
        assert sig.quantity == Decimal("2"), "回补量应为持仓绝对值"
        assert sig.is_close_position is True

    def test_close_flat_returns_none(self):
        assert _ctx("0").close_position() is None


# ══════════════════════════════════════════════════════════════
# 3. 目标持仓翻译（双向分支）
# ══════════════════════════════════════════════════════════════


class TestSignedPortfolioTranslation:
    def test_sell_reduces_position_not_flatten(self):
        """双向策略：持多 2 卖 1 → target=1（减半），而不是 0。"""
        strat = _ShortCapableStrategy(_ctx("2"))
        insight = _signal_to_insight(
            strat.ctx.sell(Decimal("1"), signal_name="tp1"), strat.name
        )
        insight.symbol = "BTC/USDT"
        targets = strat.construct_portfolio([insight], strat.ctx)
        assert targets[0].target_quantity == Decimal("1"), "TP1 必须只平一半"

    def test_sell_from_flat_creates_negative_target(self):
        """双向策略：空仓卖 1 → target=-1（开空）。"""
        strat = _ShortCapableStrategy(_ctx("0"))
        insight = _signal_to_insight(
            strat.ctx.sell(Decimal("1"), signal_name="entry_short"), strat.name
        )
        insight.symbol = "BTC/USDT"
        targets = strat.construct_portfolio([insight], strat.ctx)
        assert targets[0].target_quantity == Decimal("-1")

    def test_close_position_still_targets_zero(self):
        """close_position() 的 is_close_position 标志仍翻译成 target=0。"""
        strat = _ShortCapableStrategy(_ctx("2"))
        sig = strat.ctx.close_position(signal_name="exit")
        insight = _signal_to_insight(sig, strat.name)
        insight.symbol = "BTC/USDT"
        targets = strat.construct_portfolio([insight], strat.ctx)
        assert targets[0].target_quantity == Decimal("0")

    def test_buy_covers_short_to_zero(self):
        """双向策略：持空 -1 买 1 → target=0。"""
        strat = _ShortCapableStrategy(_ctx("-1"))
        insight = _signal_to_insight(
            strat.ctx.buy(Decimal("1"), signal_name="cover"), strat.name
        )
        insight.symbol = "BTC/USDT"
        targets = strat.construct_portfolio([insight], strat.ctx)
        assert targets[0].target_quantity == Decimal("0")

    def test_portfolio_target_accepts_negative(self):
        """负数 target_quantity 合法（空头）。"""
        t = PortfolioTarget(
            symbol="BTC/USDT", target_quantity=Decimal("-1"), reason="short"
        )
        assert t.target_quantity == Decimal("-1")


# ══════════════════════════════════════════════════════════════
# 5. 空头保证金约束
# ══════════════════════════════════════════════════════════════


class TestShortMargin:
    """开空必须占用初始保证金。

    实盘 RiskGuard 3c 按 ``Σ|position_value × leverage| / equity ≤ 100%`` 拦单；
    回测没有这层，若完全不约束就会允许「实盘一定被拒」的仓位——那等于让回测
    撒谎，且系统性奖励做空（多头受现金约束、做空不受）。
    """

    def test_short_requires_margin(self):
        """现金 10000、价格 50000、保证金率 20% → 最多开 1 手（10000/10000）。"""
        strat = _ShortCapableStrategy(_ctx("0"))
        engine = _engine(strat, initial="10000", short_margin_rate="0.20")
        engine._execute_sell(
            OrderSignal(side="sell", quantity=Decimal("2")),
            Decimal("50000"),
            _kline(),
        )
        assert engine._position == Decimal("-1"), "现金只够 1 手的保证金"

    def test_short_partial_fill_not_silent_drop(self):
        """保证金不足时部分成交，而不是整单丢弃（与买入侧现金不足同构）。"""
        strat = _ShortCapableStrategy(_ctx("0"))
        engine = _engine(strat, initial="3000", short_margin_rate="0.20")
        engine._execute_sell(
            OrderSignal(side="sell", quantity=Decimal("2")),
            Decimal("50000"),
            _kline(),
        )
        # 3000 / (50000*0.2) = 0.3 手
        assert engine._position == Decimal("-0.3")

    def test_short_skipped_when_no_margin(self):
        """现金为 0 → 完全跳过，不得开出无限空头。"""
        strat = _ShortCapableStrategy(_ctx("0"))
        engine = _engine(strat, initial="0", short_margin_rate="0.20")
        engine._execute_sell(
            OrderSignal(side="sell", quantity=Decimal("1")),
            Decimal("50000"),
            _kline(),
        )
        assert engine._position == Decimal("0")
        assert not [t for t in engine._trades if t["trade_type"] == "open"]

    def test_zero_margin_rate_disables_constraint(self):
        """``short_margin_rate=0`` 显式关闭约束（研究场景要纯策略期望值）。"""
        strat = _ShortCapableStrategy(_ctx("0"))
        engine = _engine(strat, initial="10000", short_margin_rate="0")
        engine._execute_sell(
            OrderSignal(side="sell", quantity=Decimal("5")),
            Decimal("50000"),
            _kline(),
        )
        assert engine._position == Decimal("-5")

    def test_default_margin_rate_is_five_x(self):
        """默认保证金率 20% = 5x，与实盘适配器 DEFAULT_LEVERAGE 对齐。"""
        from apps.strategy_engine.backtest_mode import DEFAULT_SHORT_MARGIN_RATE

        assert DEFAULT_SHORT_MARGIN_RATE == Decimal("0.20")

    def test_long_only_unaffected_by_margin(self):
        """只做多策略不经过 ``_open_short``，保证金参数对它零影响（回归保护）。"""
        strat = _LongOnlyStrategy(_ctx("0"))
        engine = _engine(strat, initial="10000", short_margin_rate="0.20")
        engine._execute_sell(
            OrderSignal(side="sell", quantity=Decimal("2")),
            Decimal("50000"),
            _kline(),
        )
        assert engine._position == Decimal("0"), "只做多永远不开空"


# ══════════════════════════════════════════════════════════════
# 6. 注册期准入闸门（防未验证的做空策略上线跑真钱）
# ══════════════════════════════════════════════════════════════


class TestShortRegistrationGate:
    """``supports_short=True`` 的策略默认被**拒绝注册**。

    这条闸门的存在理由：双向持仓下单路径（负持仓、穿越零点、按量平仓）改的是
    实盘 ``_target_to_order``，而该路径在合入时尚未经真实资金链路验证。
    一个未验证的做空策略上线跑真钱，代价不可逆。

    刻意不 ``raise``：策略引擎起不来会连带停掉所有只做多策略的决策，制造比
    「少注册一个策略」大得多的故障面。
    """

    def _clear(self, name):
        from apps.strategy_engine.registry import StrategyRegistry

        StrategyRegistry._strategies.pop(name, None)

    def test_short_strategy_rejected_by_default(self, monkeypatch):
        from apps.strategy_engine.registry import StrategyRegistry

        monkeypatch.delenv("STRATEGY_ENGINE_ALLOW_SHORT", raising=False)
        self._clear("gate_blocked")

        class GateBlocked(_ShortCapableStrategy):
            name = "gate_blocked"

        StrategyRegistry.register(GateBlocked, name="gate_blocked")
        assert "gate_blocked" not in StrategyRegistry.list_registered(), (
            "supports_short=True 在默认配置下必须被拒绝注册"
        )

    def test_short_strategy_registered_when_explicitly_enabled(self, monkeypatch):
        from apps.strategy_engine.registry import StrategyRegistry

        monkeypatch.setenv("STRATEGY_ENGINE_ALLOW_SHORT", "true")
        self._clear("gate_allowed")

        class GateAllowed(_ShortCapableStrategy):
            name = "gate_allowed"

        StrategyRegistry.register(GateAllowed, name="gate_allowed")
        assert "gate_allowed" in StrategyRegistry.list_registered()
        self._clear("gate_allowed")

    def test_long_only_strategy_unaffected(self, monkeypatch):
        """只做多策略不受闸门影响（回归保护：闸门不能误伤 20+ 个现有策略）。"""
        from apps.strategy_engine.registry import StrategyRegistry

        monkeypatch.delenv("STRATEGY_ENGINE_ALLOW_SHORT", raising=False)
        self._clear("gate_long_only")

        class GateLongOnly(_LongOnlyStrategy):
            name = "gate_long_only"

        StrategyRegistry.register(GateLongOnly, name="gate_long_only")
        assert "gate_long_only" in StrategyRegistry.list_registered()
        self._clear("gate_long_only")

    def test_rejection_does_not_raise(self, monkeypatch):
        """拒绝注册不得抛异常——否则整个策略引擎起不来。"""
        from apps.strategy_engine.registry import StrategyRegistry

        monkeypatch.delenv("STRATEGY_ENGINE_ALLOW_SHORT", raising=False)
        self._clear("gate_no_raise")

        class GateNoRaise(_ShortCapableStrategy):
            name = "gate_no_raise"

        # 不抛即通过
        StrategyRegistry.register(GateNoRaise, name="gate_no_raise")
        assert "gate_no_raise" not in StrategyRegistry.list_registered()

    def test_production_strategies_declare_no_short(self):
        """生产策略目录里零策略声明 supports_short=True（当前状态的快照断言）。

        这条会刻意在你将来真想启用做空时失败——逼你显式修改它，而不是让一个
        做空策略悄悄混进生产。这是它的价值，不是缺陷。
        """
        import ast
        from pathlib import Path

        strategies_dir = Path.home() / ".tradelogx" / "strategies"
        if not strategies_dir.is_dir():
            pytest.skip("生产策略目录不存在（非生产环境）")

        offenders = []
        for path in strategies_dir.glob("*.py"):
            if path.name.startswith("_"):
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                for stmt in node.body:
                    if not isinstance(stmt, ast.Assign):
                        continue
                    targets = [t.id for t in stmt.targets if isinstance(t, ast.Name)]
                    if "supports_short" not in targets:
                        continue
                    if isinstance(stmt.value, ast.Constant) and stmt.value.value:
                        offenders.append(f"{path.name}:{node.name}")

        assert not offenders, (
            "生产策略声明了 supports_short=True，但双向持仓下单路径尚未经真实"
            f"资金链路验证：{offenders}"
        )
