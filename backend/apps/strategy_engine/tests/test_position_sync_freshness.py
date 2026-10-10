"""LiveStrategyRunner 决策前持仓同步（C 修复）回归测试。

背景（2026-09-20 事故）：ctx.position 只在 runner 启动时同步一次。持仓被其他会话
平掉后，ctx 仍记着旧多头 17718 → 策略走 position>0 分支 → close_position() 按旧数量
卖出 → 把实际为 0 的账户卖成了 −17718 空头。

验收合约：
  1. 每根 K 线决策前**强制**同步真实持仓；验证事件路径按最小间隔节流
  2. 同步失败 → 本轮跳过决策（不拿未知/陈旧持仓交易），且**不得把 ctx 当成 0**
  3. 恢复后可正常交易
  4. 转为失败时给用户一条可见通知（不按 bar 刷屏）
  5. 恢复（失败→成功转换）配对一条可见通知——失败侧告知了「暂停下单」，
     状态的结束必须可见（静默恢复 = 用户永远不知道会话何时恢复交易）
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import TestCase

from apps.strategy_engine.live_mode import LiveStrategyRunner
from apps.trading.executor import OrderExecutor

KLINE = {
    "open": "0.0900",
    "high": "0.0920",
    "low": "0.0890",
    "close": "0.0915",
    "volume": "1",
    "timestamp": 1789000000000,
}


# ─────────────────────────── 最小替身 ───────────────────────────


@dataclass
class _Signal:
    side: str
    quantity: Decimal
    signal_name: str
    is_close_position: bool = False


@dataclass
class _Target:
    symbol: str = "DOGE/USDT"
    target_quantity: Decimal = Decimal("0")
    reason: str = "channel_exit"


class _Ctx:
    """StrategyContext 替身（含 position/balance 的 setter，与真实实现一致）。"""

    def __init__(self, position="0", balance="10000"):
        self._position = Decimal(str(position))
        self._positions = {"DOGE/USDT": self._position}
        self._balance = Decimal(str(balance))
        self.price = Decimal("0")

    @property
    def position(self) -> Decimal:
        return self._position

    @position.setter
    def position(self, value) -> None:
        self._position = Decimal(str(value))

    def set_position(self, symbol, quantity) -> None:
        self._position = Decimal(str(quantity))

    @property
    def balance(self) -> Decimal:
        return self._balance

    @balance.setter
    def balance(self, value) -> None:
        self._balance = Decimal(str(value))

    def set_price(self, symbol, price) -> None:
        self.price = Decimal(str(price))

    def buy(self, quantity, **kw):
        return _Signal(
            "buy",
            Decimal(str(quantity)),
            kw.get("signal_name", "buy"),
            kw.get("is_close_position", False),
        )

    def sell(self, quantity, **kw):
        return _Signal(
            "sell",
            Decimal(str(quantity)),
            kw.get("signal_name", "sell"),
            kw.get("is_close_position", False),
        )

    def close_position(self, **kw):
        if self._position <= 0:
            return None
        return _Signal(
            "sell",
            self._position,
            kw.get("signal_name", "close"),
            True,
        )


class _FakeStrategy:
    """on_bar 语义：ctx.position > 0 时产出"平仓"目标（与 donchian 同形）。"""

    name = "fake_position_sync"
    min_kline_length = 5

    def __init__(self, ctx):
        self.ctx = ctx
        self.bar_calls = 0

    def on_start(self) -> None:
        pass

    def select_universe(self):
        return ["DOGE/USDT"]

    def generate_insights(self, kline, history):
        self.bar_calls += 1
        # 与真实 Insight 同形的替身（pipeline 的 debug 日志会读 .direction/.symbol/.confidence）
        return (
            [SimpleNamespace(direction="sell", symbol="DOGE/USDT", confidence=1.0)]
            if self.ctx.position > 0
            else []
        )

    def construct_portfolio(self, insights, ctx):
        return [_Target()] if insights else []

    def apply_risk_filters(self, targets, ctx):
        return targets


class _FakeAdapter:
    def __init__(self, positions=None, error=None):
        self._positions = positions or []
        self._error = error
        self.calls = 0

    async def get_positions(self):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return list(self._positions)


class _FakeExecutor:
    """替身只提供 `_resolve_adapter` 需要的两张地图，解析逻辑复用真实实现。

    适配器按**账户 id** 取（同交易所两个账户时按交易所名会拿到别人的适配器），
    所以这里必须把适配器挂在会话用的那个账户上，而不是只挂一个 "binance"。
    """

    # 复用真实解析逻辑：替身自己实现一份迟早会和被替身的东西漂开
    _resolve_adapter = OrderExecutor._resolve_adapter

    def __init__(self, adapter):
        self._adapters = {"binance": adapter}
        self._account_adapters = {"acct-1": adapter}


def _make_runner(position: str, adapter: _FakeAdapter):
    ctx = _Ctx(position=position)
    strategy = _FakeStrategy(ctx)
    runner = LiveStrategyRunner(
        strategy, "DOGE/USDT", "1h", "acct-1", user_id="user-1", live_session_id="sess-1"
    )
    runner._test_dispatched = []  # type: ignore[attr-defined]

    async def _record(signal):
        runner._test_dispatched.append(signal)
        return True

    runner._dispatch_signal = _record  # type: ignore[assignment]
    return runner, strategy, ctx


def _patch_executor(adapter: _FakeAdapter):
    return patch(
        "apps.trading.executor.OrderExecutor.get_instance",
        return_value=_FakeExecutor(adapter),
    )


# ─────────────────────────── 用例 ───────────────────────────


class TestDecisionTimePositionSync(TestCase):
    def setUp(self):
        # 本类聚焦"持仓未知时是否下单"，通知写入用替身，避免噪声与外键依赖
        patcher = patch("apps.notify.models.Notification", MagicMock())
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_bar_decision_refreshes_stale_long_to_flat(self):
        """陈旧多头 + 交易所已平 → 决策前刷新成 0，不再误发平仓单。"""
        adapter = _FakeAdapter(positions=[])  # 交易所无持仓
        with _patch_executor(adapter):
            runner, strategy, ctx = _make_runner("17718", adapter)
            asyncio.run(runner._process_kline(dict(KLINE)))

        self.assertEqual(ctx.position, Decimal("0"), "决策前必须把 ctx 刷新为交易所真实值")
        self.assertEqual(adapter.calls, 1)
        self.assertEqual(runner._test_dispatched, [], "刷新后为空仓，不应再产生平仓单")
        self.assertEqual(strategy.bar_calls, 1)

    def test_sync_failure_blocks_orders_and_keeps_ctx(self):
        """同步失败 → 本轮不下单（修复前会按陈旧 17718 卖出），且 ctx 不被当成 0。"""
        adapter = _FakeAdapter(error=RuntimeError("positions API down"))
        with _patch_executor(adapter):
            runner, strategy, ctx = _make_runner("17718", adapter)
            with self.assertLogs("apps.strategy_engine.live_mode", level="ERROR") as logs:
                asyncio.run(runner._process_kline(dict(KLINE)))

        self.assertEqual(runner._test_dispatched, [], "持仓未知时绝不允许下单")
        self.assertEqual(ctx.position, Decimal("17718"), "失败时不得篡改 ctx（不能假装空仓）")
        self.assertEqual(strategy.bar_calls, 0, "同步失败时不应进入策略决策")
        self.assertTrue(
            any("持仓同步失败" in m or "position sync" in m for m in logs.output),
            f"必须有 fail-loud 的 ERROR 日志: {logs.output}",
        )

    def test_recovery_allows_orders_again(self):
        """同步失败后恢复成功 → 下一次决策可正常下单。"""
        adapter = _FakeAdapter(error=RuntimeError("boom"))
        with _patch_executor(adapter):
            runner, strategy, ctx = _make_runner("17718", adapter)
            asyncio.run(runner._process_kline(dict(KLINE)))  # 失败：被阻断
            self.assertEqual(runner._test_dispatched, [])

            # 交易所恢复：真实持有多头 1000；下一根 bar 到来 → 强制刷新
            adapter._error = None
            adapter._positions = [
                SimpleNamespace(symbol="DOGEUSDT", side="long", quantity=Decimal("1000"))
            ]
            asyncio.run(
                runner._process_kline(dict(KLINE, timestamp=KLINE["timestamp"] + 3_600_000))
            )

        self.assertEqual(len(runner._test_dispatched), 1, "恢复后应能正常下单")
        # 数量 = 同步到的真实持仓（证明用的是刷新后的值，不是陈旧值）
        self.assertEqual(runner._test_dispatched[0].quantity, Decimal("1000"))
        self.assertEqual(runner._test_dispatched[0].side, "sell")
        # 平仓单发出后 ctx 被乐观更新（1000 - 1000 = 0），下一根 bar 的强制同步会
        # 再用交易所真值覆盖，属预期行为
        self.assertEqual(ctx.position, Decimal("0"))

    def test_short_position_synced_as_negative(self):
        """空头同步为负值（不取绝对值），避免被误当成多头去平仓。"""
        adapter = _FakeAdapter(
            positions=[SimpleNamespace(symbol="DOGEUSDT", side="short", quantity=Decimal("4599"))]
        )
        with _patch_executor(adapter):
            runner, strategy, ctx = _make_runner("17718", adapter)
            asyncio.run(runner._process_kline(dict(KLINE)))

        self.assertEqual(ctx.position, Decimal("-4599"))

    def test_new_bar_forces_refresh(self):
        """新 K 线到来 → 强制同步（每根 bar 至少一次真值）。"""
        adapter = _FakeAdapter(
            positions=[SimpleNamespace(symbol="DOGEUSDT", side="long", quantity=Decimal("100"))]
        )
        with _patch_executor(adapter):
            runner, _, _ = _make_runner("100", adapter)
            asyncio.run(runner._process_kline(dict(KLINE)))
            asyncio.run(
                runner._process_kline(dict(KLINE, timestamp=KLINE["timestamp"] + 3_600_000))
            )

        self.assertEqual(adapter.calls, 2)

    def test_intra_bar_ticks_are_throttled(self):
        """同一根 bar 的多次 tick 推送（约 1~2 秒一次）不得每次都打交易所 API。"""
        adapter = _FakeAdapter(
            positions=[SimpleNamespace(symbol="DOGEUSDT", side="long", quantity=Decimal("100"))]
        )
        with _patch_executor(adapter):
            runner, strategy, _ = _make_runner("100", adapter)
            for _ in range(5):  # 同一 timestamp 连推 5 次
                asyncio.run(runner._process_kline(dict(KLINE)))

        self.assertEqual(adapter.calls, 1, "同 bar 内应节流为 1 次请求")
        self.assertEqual(
            strategy.bar_calls,
            5,
            "直接调 _process_kline 仍每次运行（生产入口 on_kline 已按 bar 收盘"
            "门控，见 test_bar_close_gate.py）",
        )

    def test_failed_sync_retries_after_ttl_not_every_tick(self):
        """失败态：TTL 内不重复打 API，TTL 外才退避重试一次。"""
        adapter = _FakeAdapter(error=RuntimeError("down"))
        with _patch_executor(adapter):
            runner, _, _ = _make_runner("17718", adapter)
            asyncio.run(runner._process_kline(dict(KLINE)))  # 新 bar → 尝试一次
            for _ in range(4):  # 同 bar 内 4 次 tick → 都不该再打 API
                asyncio.run(runner._process_kline(dict(KLINE)))
            self.assertEqual(adapter.calls, 1)

            runner._last_position_sync_ts = time.monotonic() - 999  # TTL 过期
            asyncio.run(runner._process_kline(dict(KLINE)))
            self.assertEqual(adapter.calls, 2, "TTL 过期后应重试一次")
        self.assertEqual(runner._test_dispatched, [], "全程不得下单")

    def test_validate_trigger_syncs_with_min_interval(self):
        """验证事件路径：TTL 内不重复请求，TTL 外重新同步。"""
        adapter = _FakeAdapter(
            positions=[SimpleNamespace(symbol="DOGEUSDT", side="long", quantity=Decimal("100"))]
        )
        with _patch_executor(adapter):
            runner, _, _ = _make_runner("100", adapter)
            runner._kline_history = [dict(KLINE, timestamp=1789000000000 + i) for i in range(15)]

            runner._position_synced = True  # 已成功同步过
            runner._last_position_sync_ts = time.monotonic()
            asyncio.run(runner._on_validate_trigger({"symbol": "DOGE/USDT"}))
            self.assertEqual(adapter.calls, 0, "TTL 内不应重复请求持仓")

            runner._last_position_sync_ts = time.monotonic() - 999  # TTL 过期
            asyncio.run(runner._on_validate_trigger({"symbol": "DOGE/USDT"}))
            self.assertEqual(adapter.calls, 1, "TTL 过期后应重新同步")

            # 两次验证事件都会正常产单（TTL 只影响是否重新请求持仓）：
            # 第 1 次沿用缓存的 ctx=100 → 平仓 100；第 2 次重新同步后同样产单
            self.assertEqual(len(runner._test_dispatched), 2)
            self.assertEqual(runner._test_dispatched[0].side, "sell")
            self.assertEqual(runner._test_dispatched[0].quantity, Decimal("100"))

    def test_validate_trigger_blocked_when_sync_fails(self):
        """验证事件路径同步失败 → 不下单。"""
        adapter = _FakeAdapter(error=RuntimeError("down"))
        with _patch_executor(adapter):
            runner, strategy, _ = _make_runner("17718", adapter)
            runner._kline_history = [dict(KLINE, timestamp=1789000000000 + i) for i in range(15)]
            asyncio.run(runner._on_validate_trigger({"symbol": "DOGE/USDT"}))

        self.assertEqual(runner._test_dispatched, [])
        self.assertEqual(strategy.bar_calls, 0)


class TestPositionSyncFailureNotification(TestCase):
    """转为失败时通知用户一次（不按 bar 刷屏）。"""

    def _run_failed_bars(self, runner, adapter, times: int) -> MagicMock:
        notify_cls = MagicMock()
        with _patch_executor(adapter), patch("apps.notify.models.Notification", notify_cls):
            for _ in range(times):
                asyncio.run(runner._process_kline(dict(KLINE)))
        return notify_cls

    def test_notifies_once_per_failure_transition(self):
        adapter = _FakeAdapter(error=RuntimeError("positions API down"))
        runner, _, _ = _make_runner("17718", adapter)
        notify_cls = self._run_failed_bars(runner, adapter, times=3)

        calls = notify_cls.objects.create.call_args_list
        self.assertEqual(len(calls), 1, f"应只通知一次，实际 {len(calls)} 次")
        kwargs = calls[0].kwargs
        self.assertEqual(kwargs.get("channel"), "web")
        self.assertIn("持仓同步失败", kwargs.get("message", ""))
        self.assertEqual(str(kwargs.get("user_id")), "user-1")

    def test_notification_failure_does_not_break_flow(self):
        """通知写入抛异常时不得影响主流程（仍然阻断下单、不崩）。"""
        adapter = _FakeAdapter(error=RuntimeError("down"))
        runner, _, _ = _make_runner("17718", adapter)
        boom = MagicMock()
        boom.objects.create.side_effect = RuntimeError("notify db down")
        with _patch_executor(adapter), patch("apps.notify.models.Notification", boom):
            asyncio.run(runner._process_kline(dict(KLINE)))
        self.assertEqual(runner._test_dispatched, [])

    def test_recovery_resets_notification_flag(self):
        """恢复后再失败应能再次通知。"""
        adapter = _FakeAdapter(error=RuntimeError("down"))
        runner, _, _ = _make_runner("17718", adapter)
        self._run_failed_bars(runner, adapter, times=1)

        # 恢复（下一根 bar 强制刷新）
        adapter._error = None
        adapter._positions = [
            SimpleNamespace(symbol="DOGEUSDT", side="long", quantity=Decimal("100"))
        ]
        with _patch_executor(adapter), patch("apps.notify.models.Notification", MagicMock()):
            asyncio.run(
                runner._process_kline(dict(KLINE, timestamp=KLINE["timestamp"] + 3_600_000))
            )

        # 再失败
        adapter._error = RuntimeError("down again")
        adapter._positions = []
        notify_cls = self._run_failed_bars(runner, adapter, times=1)
        self.assertEqual(len(notify_cls.objects.create.call_args_list), 1)
        self.assertNotEqual(str(uuid.uuid4()), "")  # 占位：保持 uuid 导入被使用

    def test_recovery_notifies_once_and_only_after_failure(self):
        """失败→恢复：恢复通知与失败通知成对、各一条；后续正常 bar 不重复。"""
        adapter = _FakeAdapter(error=RuntimeError("down"))
        runner, _, _ = _make_runner("17718", adapter)
        notify_cls = MagicMock()
        with _patch_executor(adapter), patch("apps.notify.models.Notification", notify_cls):
            asyncio.run(runner._process_kline(dict(KLINE)))  # 失败 → 一条通知

            adapter._error = None
            adapter._positions = [
                SimpleNamespace(symbol="DOGEUSDT", side="long", quantity=Decimal("100"))
            ]
            asyncio.run(
                runner._process_kline(dict(KLINE, timestamp=KLINE["timestamp"] + 3_600_000))
            )  # 恢复 → 一条通知
            asyncio.run(
                runner._process_kline(dict(KLINE, timestamp=KLINE["timestamp"] + 7_200_000))
            )  # 正常 bar → 不通知

        calls = notify_cls.objects.create.call_args_list
        self.assertEqual(len(calls), 2, f"失败 + 恢复各一条，实际 {len(calls)} 条")
        self.assertIn("持仓同步失败", calls[0].kwargs.get("message", ""))
        self.assertIn("已恢复", calls[1].kwargs.get("message", ""))

    def test_no_notification_at_all_without_user_id(self):
        """无 user_id：失败与恢复都不通知（没有可投递的人），阻断行为不变。"""
        adapter = _FakeAdapter(error=RuntimeError("down"))
        runner, _, _ = _make_runner("17718", adapter)
        runner.user_id = None
        notify_cls = MagicMock()
        with _patch_executor(adapter), patch("apps.notify.models.Notification", notify_cls):
            asyncio.run(runner._process_kline(dict(KLINE)))  # 失败：不通知

            adapter._error = None
            adapter._positions = [
                SimpleNamespace(symbol="DOGEUSDT", side="long", quantity=Decimal("100"))
            ]
            asyncio.run(
                runner._process_kline(dict(KLINE, timestamp=KLINE["timestamp"] + 3_600_000))
            )  # 恢复：仍不通知

        self.assertEqual(len(notify_cls.objects.create.call_args_list), 0)
        self.assertEqual(len(runner._test_dispatched), 1, "恢复后决策路径照常工作")


class _FakeClock:
    """可控的 monotonic 时钟：把事故时间线里两个 tick 之间真实的 2 秒间隔
    压进单元测试（不能真睡 2 秒）。只替换 live_mode 模块引用的 time.monotonic。"""

    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TestDispatchRefreshesSyncWatermark(TestCase):
    """2026-09-27 BNB 双平手账：成功 dispatch 后的乐观持仓，不得被在途窗口内
    的一次 TTL 到期重同步覆盖。

    时间线（与事故一致）：上次真实同步 T0 在 14s 前 → tick 1 在 TTL 内沿用缓存
    （position=100）→ 发平仓单 → 乐观 position=0；tick 2 隔 2 秒到来，距 T0 已
    16s → 旧逻辑在这里 TTL 到期重同步，而交易所（在途单尚未处理）仍返回 100
    → ctx 被覆盖回 100 → 第二张同量平仓单 → 合约侧变成反向开仓。
    """

    def test_close_dispatch_blocks_resync_in_inflight_window(self):
        adapter = _FakeAdapter(
            positions=[
                SimpleNamespace(symbol="DOGEUSDT", side="long", quantity=Decimal("100"))
            ]
        )
        clock = _FakeClock()
        with _patch_executor(adapter), patch(
            "apps.strategy_engine.live_mode.time.monotonic", clock
        ):
            runner, _, ctx = _make_runner("100", adapter)
            runner._last_bar_ts = KLINE["timestamp"]  # 不当新 bar：测 TTL 路径
            runner._position_synced = True
            runner._last_position_sync_ts = clock() - 14.0  # T0：上次真实同步

            asyncio.run(runner._process_kline(dict(KLINE)))  # tick 1 @ T0+14（TTL 内）
            self.assertEqual(adapter.calls, 0, "tick 1 在 TTL 内不应请求交易所")
            self.assertEqual(len(runner._test_dispatched), 1, "position>0 → 一张平仓单")
            self.assertEqual(ctx.position, Decimal("0"), "dispatch 后乐观更新")
            self.assertEqual(
                runner._last_position_sync_ts,
                clock(),
                "成功 dispatch 必须把同步水位推到当前时刻",
            )

            clock.advance(2.0)  # tick 2 @ T0+16：距 T0 已 TTL 过期
            asyncio.run(runner._process_kline(dict(KLINE)))

        self.assertEqual(
            adapter.calls,
            0,
            "在途窗口内不得拉旧交易所值覆盖乐观持仓",
        )
        self.assertEqual(len(runner._test_dispatched), 1, "不得发出第二张平仓单")
        self.assertEqual(ctx.position, Decimal("0"))

    def test_rejected_dispatch_does_not_refresh_watermark(self):
        """订单被拒（没发出去）→ 不得占用同步窗口：交易所状态仍是唯一真值。"""
        adapter = _FakeAdapter(
            positions=[
                SimpleNamespace(symbol="DOGEUSDT", side="long", quantity=Decimal("100"))
            ]
        )
        clock = _FakeClock()
        with _patch_executor(adapter), patch(
            "apps.strategy_engine.live_mode.time.monotonic", clock
        ):
            runner, _, ctx = _make_runner("100", adapter)
            runner._last_bar_ts = KLINE["timestamp"]
            runner._position_synced = True
            runner._last_position_sync_ts = clock() - 14.0

            async def _reject(_signal):
                return False

            runner._dispatch_signal = _reject
            asyncio.run(runner._process_kline(dict(KLINE)))  # 被拒：无乐观更新
            self.assertEqual(len(runner._test_dispatched), 0)
            self.assertEqual(ctx.position, Decimal("100"))

            clock.advance(2.0)  # 距 T0 已 16s：TTL 过期 → 必须照常重同步
            asyncio.run(runner._process_kline(dict(KLINE)))

        self.assertEqual(adapter.calls, 1, "未成功的 dispatch 不得占用同步窗口")
        self.assertEqual(ctx.position, Decimal("100"), "重同步后按交易所真值覆盖")


class TestTargetToOrderCloseFlag(TestCase):
    """Phase 2 目标持仓 → 订单：卖到空仓必须带回 is_close_position 标志。

    策略侧 close_position() 置上的标志在 Insight/PortfolioTarget 转换时丢失
    （见 base.construct_portfolio），导致平仓单按普通卖单下发：RiskGuard 不被
    跳过、交易所侧不带 reduceOnly（超持仓的卖单在合约上变反向开仓而非拒单）。
    `_target_to_order` 是标志回流订单信号的唯一出口，按「目标=空仓」还原。
    """

    def _bare_runner(self, position: str) -> LiveStrategyRunner:
        ctx = _Ctx(position=position)
        strategy = _FakeStrategy(ctx)
        return LiveStrategyRunner(
            strategy, "DOGE/USDT", "1h", "acct-1", user_id="user-1", live_session_id="sess-1"
        )

    def test_sell_to_flat_is_marked_close(self):
        runner = self._bare_runner("100")
        sig = runner._target_to_order(_Target(target_quantity=Decimal("0")))
        self.assertIsNotNone(sig)
        self.assertEqual(sig.side, "sell")
        self.assertEqual(sig.quantity, Decimal("100"))
        self.assertTrue(
            sig.is_close_position,
            "卖到空仓必须带平仓标志（下游依赖它跳过风控、翻译成 reduceOnly）",
        )

    def test_buy_is_not_marked_close(self):
        runner = self._bare_runner("0")
        sig = runner._target_to_order(_Target(target_quantity=Decimal("50")))
        self.assertIsNotNone(sig)
        self.assertEqual(sig.side, "buy")
        self.assertFalse(
            sig.is_close_position,
            "开仓单不得带平仓标志（带了会被翻译成 reduceOnly，开仓直接被拒）",
        )

    def test_no_order_when_already_at_target(self):
        runner = self._bare_runner("0")
        self.assertIsNone(runner._target_to_order(_Target(target_quantity=Decimal("0"))))
