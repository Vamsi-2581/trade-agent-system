"""盈亏比 / profit_factor 的计算口径（2026-10-20）。

背景：`get_task_result` 的返回值里一直没有盈亏比，于是指标溯源守卫对报告里的
「盈亏比 0.96」无从核验——2026-10-19 那份虚构报告里的 0.96 就是这么漏过去的
（`_METRIC_CLAIM_PATTERNS` 当时把它映射成空元组 = 不参与判定）。

本文件钉住三件事：

1. **两个口径都要出，且不是同一个数**。仓库里「盈亏比」有两种读法：
   ``profit_factor`` = 总盈利/总亏损（行业通行，CFA 口径），
   ``payoff_ratio`` = 平均盈利/平均亏损（`docs/arch/architecture_final.md` 的写法）。
   只出一个会让另一种口径的正确报告被守卫误拦（与 `final_capital` vs
   `final_equity` 是同一类坑）。
2. **算不出来是 `None`，不是 0 也不是 inf**。没有亏损笔时 profit_factor 数学上是
   +∞：落 `inf` 会污染 JSON 序列化与下游排序，落 `0` 会把「一笔都没亏」读成
   「全亏」。而没有盈利笔时 ``profit_factor == 0.0`` 是**合法值**，必须与
   ``None`` 区分开——这正是「判不了不是判通过」那条纪律。
3. **口径与 win_rate / total_trades 同源**：都是「已平仓交易」，`pnl` 已扣手续费与
   滑点，未平仓的开仓记录（``pnl is None``）不参与。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from apps.strategy_engine.backtest_mode import BacktestEngine, trade_quality_metrics
from apps.strategy_engine.base import BaseStrategy, StrategyContext


class _NoopStrategy(BaseStrategy):
    """不产生任何信号：引擎测试只需要一个合法的 strategy 占位。"""

    name = "noop_for_trade_quality"
    description = "策略类型：测试\n核心指标：无\n适用场景：单测\n入场逻辑：无"

    def on_bar(self, kline, history):
        return None


def _engine() -> BacktestEngine:
    ctx = StrategyContext(
        symbol="BTC/USDT",
        timeframe="1h",
        mode="backtest",
        params={},
        balance=Decimal("10000"),
        position=Decimal("0"),
    )
    return BacktestEngine(
        strategy=_NoopStrategy(ctx),
        ohlcv_data=[],
        initial_capital=Decimal("10000"),
        symbol="BTC/USDT",
        timeframe="1h",
        commission_rate=Decimal("0"),
        slippage_rate=Decimal("0"),
    )


def _closed(pnl: float) -> dict:
    """一条已平仓交易的形状（`_compute_stats` 只读 pnl 是否存在与正负）。"""
    return {
        "entry_time": "2026-01-01T00:00:00+00:00",
        "exit_time": "2026-01-01T01:00:00+00:00",
        "side": "long",
        "entry_price": 100.0,
        "exit_price": 100.0,
        "quantity": 1.0,
        "pnl": pnl,
        "pnl_pct": 0.0,
        "commission": 0.0,
        "slippage": 0.0,
        "signal": "test",
        "exit_reason": "signal",
        "trade_type": "close",
    }


def _open() -> dict:
    """一条未平仓记录：pnl 为 None，不得参与任何交易质量指标。"""
    return {
        "entry_time": "2026-01-01T00:00:00+00:00",
        "exit_time": None,
        "side": "long",
        "entry_price": 100.0,
        "exit_price": None,
        "quantity": 1.0,
        "pnl": None,
        "pnl_pct": None,
        "commission": 0.0,
        "slippage": 0.0,
        "signal": "entry",
        "exit_reason": "",
        "trade_type": "open",
    }


# ══════════════════════════════════════════════════════════════
# 1. 两个口径不是同一个数
# ══════════════════════════════════════════════════════════════


def test_profit_factor_and_payoff_ratio_are_different_measures():
    """总额口径与单笔口径必须分别计算，且在这个样本上数值不同。

    3 笔盈利 +300/+100/+100、1 笔亏损 -100：
      * profit_factor = 500 / 100 = 5.0      （总额口径）
      * payoff_ratio  = (500/3) / (100/1) = 1.666…（单笔口径）

    如果实现把两者写成同一个公式，这个用例会立刻变红——那正是要防的：
    报告里写「盈亏比 1.67」与「profit_factor 5.0」是两句话，锚定值只有一个的话
    守卫必然误判其中一个。
    """
    trades = [_closed(300.0), _closed(100.0), _closed(100.0), _closed(-100.0)]
    profit_factor, payoff_ratio = BacktestEngine._compute_trade_quality(trades)

    assert profit_factor == pytest.approx(5.0)
    assert payoff_ratio == pytest.approx(500.0 / 3.0 / 100.0)
    assert profit_factor != pytest.approx(payoff_ratio)


def test_payoff_ratio_ignores_trade_count():
    """单笔口径对笔数不敏感，总额口径对笔数敏感——两者的区别就在这。"""
    # 同样的平均盈/平均亏，但盈利笔数翻倍 → profit_factor 翻倍，payoff 不变。
    few = [_closed(100.0), _closed(-100.0)]
    many = [_closed(100.0), _closed(100.0), _closed(-100.0)]

    pf_few, payoff_few = BacktestEngine._compute_trade_quality(few)
    pf_many, payoff_many = BacktestEngine._compute_trade_quality(many)

    assert payoff_few == pytest.approx(payoff_many) == pytest.approx(1.0)
    assert pf_few == pytest.approx(1.0)
    assert pf_many == pytest.approx(2.0)


# ══════════════════════════════════════════════════════════════
# 2. 判不了 → None；坏结果是 0.0（两者必须区分）
# ══════════════════════════════════════════════════════════════


def test_no_closed_trades_yields_none():
    """没有已平仓交易 → 两个都是 None（与 win_rate 的空样本口径一致）。"""
    assert BacktestEngine._compute_trade_quality([]) == (None, None)
    # 只有开仓记录同样算空样本
    assert BacktestEngine._compute_trade_quality([_open(), _open()]) == (None, None)


def test_no_losing_trade_yields_none_profit_factor():
    """没有亏损笔 → profit_factor 是 None（不是 inf，也不是 0）。

    数学上分母为 0 即 +∞。落 `inf` 会让 `json.dumps` 产出非法 JSON（Django 的
    JSONField 序列化直接炸），落 `0` 会把「一笔都没亏」读成「全亏」。
    单笔口径同理：没有亏损笔就没有「平均亏损」，payoff 也是 None。
    """
    pf, payoff = BacktestEngine._compute_trade_quality([_closed(100.0), _closed(50.0)])
    assert pf is None
    assert payoff is None


def test_all_losing_trades_yields_zero_profit_factor():
    """全部亏损 → profit_factor 是**合法的 0.0**，不是 None。

    这是与上一条成对的断言：`0.0` 表示「测到了，结果是一笔没赚」，
    `None` 表示「测不了」。把两者混成一个值，下游就分不清「策略很烂」
    与「这次没数据」。payoff 则仍是 None（没有盈利笔 → 没有平均盈利）。
    """
    pf, payoff = BacktestEngine._compute_trade_quality(
        [_closed(-100.0), _closed(-50.0)]
    )
    assert pf == pytest.approx(0.0)
    assert pf is not None
    assert payoff is None


def test_breakeven_trades_are_neither_win_nor_loss():
    """pnl == 0 的笔既不算盈利也不算亏损，不进任何一侧的分子分母。"""
    trades = [_closed(200.0), _closed(0.0), _closed(-100.0)]
    pf, payoff = BacktestEngine._compute_trade_quality(trades)

    assert pf == pytest.approx(2.0)  # 200/100，0 不进分子
    assert payoff == pytest.approx(2.0)  # (200/1)/(100/1)，0 不进分母的笔数


# ══════════════════════════════════════════════════════════════
# 3. 引擎接线：_compute_stats 真的把两个键放进 stats
# ══════════════════════════════════════════════════════════════


def test_compute_stats_exposes_both_keys():
    """`_compute_stats` 的返回里必须有这两个键（守卫的锚定块从顶层取）。"""
    engine = _engine()
    engine._trades = [_closed(300.0), _closed(-100.0)]

    stats = engine._compute_stats()

    assert stats["profit_factor"] == pytest.approx(3.0)
    assert stats["payoff_ratio"] == pytest.approx(3.0)
    # 与既有指标同源：同一批已平仓交易
    assert stats["total_trades"] == 2
    assert stats["win_rate"] == pytest.approx(0.5)


def test_open_positions_do_not_skew_trade_quality():
    """未平仓记录（pnl is None）不参与，否则分子分母都被稀释。"""
    engine = _engine()
    engine._trades = [_closed(300.0), _closed(-100.0), _open(), _open()]

    stats = engine._compute_stats()

    assert stats["profit_factor"] == pytest.approx(3.0)
    assert stats["total_trades"] == 2, "total_trades 也不该数开仓记录"


# ══════════════════════════════════════════════════════════════
# 4. 落库载荷：None 不落库，0.0 照写
# ══════════════════════════════════════════════════════════════


def test_trade_quality_metrics_omits_none():
    """算不出来的指标**整键不出现**，而不是写 null。

    「键不存在」= 这次没测到；`0` = 测到了、结果是 0。把 None 写成 null 会让
    下游 `metrics.get("profit_factor")` 拿到 None 而不是缺省，前端与守卫都要
    额外判一次 None，且「有没有证据」这件事变得不可读。
    """
    assert trade_quality_metrics({"profit_factor": None, "payoff_ratio": None}) == {}
    assert trade_quality_metrics({}) == {}


def test_trade_quality_metrics_keeps_zero_and_values():
    """`0.0` 是真实结果，必须落库；正常数值原样落库。"""
    assert trade_quality_metrics({"profit_factor": 0.0}) == {"profit_factor": 0.0}
    assert trade_quality_metrics({"profit_factor": 2.5, "payoff_ratio": 1.4}) == {
        "profit_factor": 2.5,
        "payoff_ratio": 1.4,
    }


def test_trade_quality_metrics_ignores_unrelated_stats():
    """只挑自己那两个键，不把 stats 里的其它字段（equity_curve 等）搬进 metrics。"""
    out = trade_quality_metrics(
        {
            "profit_factor": 1.5,
            "payoff_ratio": 1.2,
            "sharpe_ratio": 1.9,
            "equity_curve": [{"equity": 1.0}],
            "trades": [{"pnl": 1.0}],
        }
    )
    assert out == {"profit_factor": 1.5, "payoff_ratio": 1.2}
