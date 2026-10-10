"""LiveStrategyRunner 只在 K 线收盘时决策（2026-10 BNB/USDT 频繁进出场修复）。

背景：Binance 的 @kline 流每次成交都推送（同一根 bar 内约 1~2 秒一次）。
若 bar 内 tick 也跑完整决策，策略里读到的「收盘价」其实是正在形成中的 bar 的
实时价——box_time_range_breakout 的上沿/中线在 4h bar 内被反复穿越，一周
10 次往返（含 14:02 卖出 → 14:04 再买入）。回测契约是「每根已收盘 K 线
决策一次」，实盘对齐它。

验收合约：
  1. bar 内 tick（is_closed=False）→ 只更新历史，不跑策略、不打交易所
  2. 收盘推送（is_closed=True）→ 决策一次，且用**最终** OHLC（不是 bar 内旧价）
  3. 无 is_closed 字段的 K 线（REST 预热格式）→ 不触发决策
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

from django.test import TestCase

from apps.strategy_engine.tests.test_position_sync_freshness import (
    KLINE,
    _FakeAdapter,
    _make_runner,
    _patch_executor,
)


class TestBarCloseDecisionGate(TestCase):
    def setUp(self):
        patcher = patch("apps.notify.models.Notification", MagicMock())
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_intra_bar_tick_updates_history_but_does_not_decide(self):
        """bar 内 tick：只更新历史，不跑策略、不打交易所 API。"""
        adapter = _FakeAdapter(
            positions=[]
        )  # 交易所无持仓（若误跑决策会触发平仓路径，便于暴露）
        with _patch_executor(adapter):
            runner, strategy, _ = _make_runner("100", adapter)
            runner._running = True
            asyncio.run(runner.on_kline(dict(KLINE, is_closed=False)))

        self.assertEqual(strategy.bar_calls, 0, "bar 内 tick 不得跑策略")
        self.assertEqual(runner._test_dispatched, [], "bar 内 tick 不得下单")
        self.assertEqual(adapter.calls, 0, "bar 内 tick 不得打交易所持仓 API")
        self.assertEqual(len(runner._kline_history), 1, "历史仍要更新")
        self.assertEqual(
            runner._kline_history[-1]["timestamp"], KLINE["timestamp"], "历史按本 bar 记录"
        )

    def test_closed_bar_decides_once_with_final_ohlcv(self):
        """收盘推送：决策恰好一次，且用的是最终 OHLC（bar 内旧价不得残留）。"""
        adapter = _FakeAdapter(
            positions=[]
        )  # 交易所已平 → 若用陈旧 ctx=100 会误发平仓单
        with _patch_executor(adapter):
            runner, strategy, ctx = _make_runner("100", adapter)
            runner._running = True
            # bar 内 tick：实时价冲高（若按 bar 内决策，这里就会触发逻辑）
            asyncio.run(runner.on_kline(dict(KLINE, is_closed=False, close="0.0930")))
            self.assertEqual(strategy.bar_calls, 0)
            # 收盘推送：最终回落 → 用最终值决策
            asyncio.run(runner.on_kline(dict(KLINE, is_closed=True, close="0.0915")))

        self.assertEqual(strategy.bar_calls, 1, "同一根 bar 只决策一次")
        self.assertEqual(len(runner._test_dispatched), 0, "按交易所真值(0)不应有平仓单")
        self.assertEqual(ctx.position, 0, "决策前已同步为交易所真值")
        self.assertEqual(
            runner._kline_history[-1]["close"], "0.0915", "历史保留的是最终 OHLC"
        )

    def test_rest_warmup_kline_without_flag_does_not_decide(self):
        """REST 预热 K 线没有 is_closed 字段 → 不触发决策（当作未收盘）。"""
        adapter = _FakeAdapter(positions=[])
        with _patch_executor(adapter):
            runner, strategy, _ = _make_runner("100", adapter)
            runner._running = True
            asyncio.run(runner.on_kline(dict(KLINE)))  # 无 is_closed 键

        self.assertEqual(strategy.bar_calls, 0)
        self.assertEqual(runner._test_dispatched, [])

    def test_same_bar_tick_then_close_decides_only_on_close(self):
        """同 bar 多次 tick + 一次收盘：决策次数 = 收盘推送次数（1）。"""
        adapter = _FakeAdapter(
            positions=[]
        )
        with _patch_executor(adapter):
            runner, strategy, _ = _make_runner("100", adapter)
            runner._running = True
            for close in ("0.0905", "0.0910", "0.0925", "0.0900"):
                asyncio.run(runner.on_kline(dict(KLINE, is_closed=False, close=close)))
            self.assertEqual(strategy.bar_calls, 0, "4 次 bar 内 tick 均不决策")
            asyncio.run(runner.on_kline(dict(KLINE, is_closed=True, close="0.0901")))

        self.assertEqual(strategy.bar_calls, 1)
