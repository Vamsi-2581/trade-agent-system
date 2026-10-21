"""盈亏比 / profit_factor 的落库与 `get_task_result` 透出（2026-10-20）。

分工（谁写、谁读，为什么要钉住）：

* **写**：引擎 `_compute_stats` 产出 → `save_backtest_result` 把
  `profit_factor` / `payoff_ratio` 合并进 `BacktestResult.metrics`。
* **读**：`get_task_result` 从 `metrics` 里挑出这两个键，**提到 result 顶层**。

第二条是本文件的核心：锚定块（`BaseAgent._extract_task_metrics`）只读顶层键。
如果盈亏比留在 `metrics` 子字典里，守卫永远看不到它 —— 于是「报告里写盈亏比
0.96」这件事对守卫完全不可见，等于又把 2026-10-19 那个洞开着。

两处另有两条硬约束：

1. `metrics` 是共享 JSONField（`regime_slice` / `walk_forward` /
   `capital_efficiency` 都住里面），保存路径必须**读-改-写**，只动自己两个键。
   整份覆盖 = 别人的字段凭空消失。
2. 算不出来的指标（`None`）**整键不落库**，`0.0` 照写。「键不存在」= 这次没测到，
   「0」= 测到了、结果是 0。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from apps.backtest.models import BacktestResult


def _stats(**overrides) -> dict:
    base = {
        "final_equity": 10000.0,
        "total_return_pct": 0.0,
        "max_drawdown_pct": 0.0,
        "win_rate": 0.0,
        "sharpe_ratio": 0.0,
        "total_trades": 0,
        "equity_curve": [],
        "trades": [],
        "profit_factor": None,
        "payoff_ratio": None,
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


@pytest.mark.django_db(transaction=True)
class TestTradeQualityPersistence:
    """`save_backtest_result` 把交易质量指标写进 `metrics`。"""

    @pytest.mark.asyncio
    async def test_new_result_persists_trade_quality(self):
        """新建路径：两个键都落进 `metrics`。"""
        from apps.trading.models import Strategy

        strategy = await Strategy.objects.acreate(
            name="tq_new", code_path="strategies/tq_new.py", is_active=False
        )
        out = await _save(
            str(strategy.id), stats=_stats(profit_factor=0.31, payoff_ratio=0.96)
        )

        result = await BacktestResult.objects.aget(id=out["result_id"])
        assert result.metrics["profit_factor"] == pytest.approx(0.31)
        assert result.metrics["payoff_ratio"] == pytest.approx(0.96)

    @pytest.mark.asyncio
    async def test_none_trade_quality_is_not_persisted(self):
        """算不出来 → 键不出现（不是 null）。

        老回测行读回来时 `metrics.get("profit_factor")` 必须是 `None`（缺省），
        而**不是** 0 —— 否则「没有亏损笔、算不出」会被下游读成「一笔没赚」。
        """
        from apps.trading.models import Strategy

        strategy = await Strategy.objects.acreate(
            name="tq_none", code_path="strategies/tq_none.py", is_active=False
        )
        out = await _save(str(strategy.id), stats=_stats())  # 两个都是 None

        result = await BacktestResult.objects.aget(id=out["result_id"])
        assert "profit_factor" not in (result.metrics or {})
        assert "payoff_ratio" not in (result.metrics or {})

    @pytest.mark.asyncio
    async def test_zero_profit_factor_is_persisted(self):
        """全部亏损 → `profit_factor == 0.0` 必须落库（与 None 区分）。"""
        from apps.trading.models import Strategy

        strategy = await Strategy.objects.acreate(
            name="tq_zero", code_path="strategies/tq_zero.py", is_active=False
        )
        out = await _save(str(strategy.id), stats=_stats(profit_factor=0.0))

        result = await BacktestResult.objects.aget(id=out["result_id"])
        assert result.metrics["profit_factor"] == 0.0
        assert "payoff_ratio" not in result.metrics  # None 仍不落库

    @pytest.mark.asyncio
    async def test_update_path_preserves_other_metrics_keys(self):
        """更新路径读-改-写：别的写方（regime_slice 等）的键不得被抹掉。"""
        from apps.trading.models import Strategy

        strategy = await Strategy.objects.acreate(
            name="tq_merge", code_path="strategies/tq_merge.py", is_active=False
        )
        first = await _save(
            str(strategy.id),
            stats=_stats(profit_factor=1.1),
            existing_result_id=None,
        )
        # 模拟另一个写方（regime 切片）往同一个 metrics 里塞了自己的键
        row = await BacktestResult.objects.aget(id=first["result_id"])
        await BacktestResult.objects.filter(id=row.id).aupdate(
            metrics={**row.metrics, "regime_slice": {"version": 1}}
        )

        await _save(
            str(strategy.id),
            stats=_stats(profit_factor=2.2),
            existing_result_id=first["result_id"],
        )

        row = await BacktestResult.objects.aget(id=first["result_id"])
        assert row.metrics["profit_factor"] == pytest.approx(2.2), "新值没写进去"
        assert row.metrics["regime_slice"] == {"version": 1}, "别的写方的键被抹掉了"


@pytest.mark.django_db(transaction=True)
class TestGetTaskResultSurfacesTradeQuality:
    """`get_task_result` 的 BacktestResult 路径把两个键提到 result 顶层。"""

    @pytest.mark.asyncio
    async def test_trade_quality_reaches_result_top_level(self):
        """核心：锚定块只读顶层键，盈亏比必须出现在 `result` 里。"""
        from apps.trading.models import Strategy
        from apps.agent.tools.backtest import GetTaskResultTool

        strategy = await Strategy.objects.acreate(
            name="tq_surface", code_path="strategies/tq_surface.py", is_active=False
        )
        out = await _save(
            str(strategy.id), stats=_stats(profit_factor=0.31, payoff_ratio=0.96)
        )

        tool = GetTaskResultTool()
        result = await tool.execute(task_id=out["result_id"])

        assert result.success
        payload = result.data["result"]
        assert payload["profit_factor"] == pytest.approx(0.31)
        assert payload["payoff_ratio"] == pytest.approx(0.96)
        # 子字典照样原样透出（前端 `metrics.profit_factor` 读的是这里）
        assert payload["metrics"]["profit_factor"] == pytest.approx(0.31)

    @pytest.mark.asyncio
    async def test_legacy_result_without_trade_quality_omits_keys(self):
        """老回测（metrics 里没这两个键）→ result 顶层也不出现，而不是 None。"""
        from apps.trading.models import Strategy
        from apps.agent.tools.backtest import GetTaskResultTool

        strategy = await Strategy.objects.acreate(
            name="tq_legacy", code_path="strategies/tq_legacy.py", is_active=False
        )
        out = await _save(str(strategy.id), stats=_stats())

        tool = GetTaskResultTool()
        result = await tool.execute(task_id=out["result_id"])

        payload = result.data["result"]
        assert "profit_factor" not in payload
        assert "payoff_ratio" not in payload

    @pytest.mark.asyncio
    async def test_nested_metrics_payloads_are_not_hoisted(self):
        """只提两个键，不把 regime_slice 这类嵌套载荷塞进 result 顶层。"""
        from apps.trading.models import Strategy
        from apps.agent.tools.backtest import GetTaskResultTool

        strategy = await Strategy.objects.acreate(
            name="tq_nested", code_path="strategies/tq_nested.py", is_active=False
        )
        out = await _save(str(strategy.id), stats=_stats(profit_factor=1.5))
        row = await BacktestResult.objects.aget(id=out["result_id"])
        await BacktestResult.objects.filter(id=row.id).aupdate(
            metrics={**row.metrics, "regime_slice": {"version": 1}}
        )

        tool = GetTaskResultTool()
        result = await tool.execute(task_id=out["result_id"])

        payload = result.data["result"]
        assert payload["profit_factor"] == pytest.approx(1.5)
        assert "regime_slice" not in payload, "嵌套载荷被提到顶层，污染核心指标清单"
        assert payload["metrics"]["regime_slice"] == {"version": 1}
