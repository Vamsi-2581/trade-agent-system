"""箱体策略出场参考线冻结在入场时刻（2026-10-07 DOGE/USDT 事故修复）。

事故：box_time_range_breakout_strategy 的出场条件原本是「**当前 bar 检出箱体** 且
收盘 < 箱体 mid」，两个洞：
  1. 箱体 mid 随 120 bar 检测窗口逐 bar 重算，出场参考线漂移、出场点不可预期；
  2. 箱体「消失」（is_ranging=False）时 on_bar 直接 return None——持仓期间没有任何
     出场线。DOGE 在 0.0934~0.0966 区间全程检不出箱体，止损形同虚设，直到价格跌到
     0.088、箱体重建且 close 已远低于 mid 才触发出场。

修复：入场那一刻把触发箱体的 mid/lower 冻结为 `self._entry_ref`；持仓期间只跟冻结
线比，不依赖当前 bar 能否检出箱体；箱体消失也照常用冻结线出场。

合成数据（与事故同形的几何）：周期 6 正弦震荡 98.7~102.0（mid≈100.35，宽≈3%，
能过 detect_box_range 全部门槛）；「箱体消失」用窗口低点砸到 80.3 复现——
下沿候选 98.87 > 80.3×1.15=92.3，过不了粗筛（与事故中 0.078 低点压死 0.09287
下沿是同一个粗筛条件）。
"""

from __future__ import annotations

import importlib.util
import math
import os
import sys
from decimal import Decimal

import pytest

from apps.strategy_engine.base import StrategyContext

STRATEGY_PATH = "/root/.tradelogx/strategies/box_time_range_breakout_strategy.py"

T0 = 1789000000000


def _load_strategy_cls():
    if not os.path.isfile(STRATEGY_PATH):
        pytest.skip(f"strategy file not present at {STRATEGY_PATH}")
    spec = importlib.util.spec_from_file_location(
        "box_time_range_breakout_strategy_frozen_exit_test", STRATEGY_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod.BoxTimeRangeBreakoutStrategy


def _kline(i, o, h, l, c, v=1000.0):
    return {
        "timestamp": T0 + i * 3600000,
        "open": o,
        "high": h,
        "low": l,
        "close": c,
        "volume": v,
    }


def _oscillation(n=140):
    """周期 6 正弦震荡：close 在 98.7 ~ 102.0，mid≈100.35，箱体宽≈2.95%。"""
    bars = []
    for i in range(n):
        c = 100.35 + 1.65 * math.sin(2 * math.pi * i / 6)
        o = 100.35 + 1.65 * math.sin(2 * math.pi * (i - 1) / 6)
        bars.append(_kline(i, round(o, 4), round(max(o, c) + 0.05, 4),
                           round(min(o, c) - 0.05, 4), round(c, 4)))
    return bars


def _make(cls, position="0", ref=None):
    """ref 模拟「入场过 / 重启丢参考线」。调用方负责 history 末根=当前 K 线（runner 契约）。"""
    ctx = StrategyContext(
        symbol="TEST/USDT",
        timeframe="1h",
        mode="backtest",
        params={"position_pct": 0.1},
        balance=Decimal("10000"),
        position=Decimal(position),
    )
    s = cls(ctx)
    if ref is not None:
        s._entry_ref = ref
    return s


def _enter(bars):
    """走完入场：返回 (策略实例, 入场时的冻结参考线)。"""
    cls = _load_strategy_cls()
    breakout = _kline(len(bars), 101.5, 103.2, 101.4, 103.0)
    s = _make(cls)
    sig = s.on_bar(breakout, bars + [breakout])
    assert sig is not None and sig.side == "buy", "突破上沿必须入场"
    return s, s._entry_ref


def test_entry_freezes_ref_of_the_entry_box():
    """入场的瞬间，参考线冻结为触发箱体的 mid（exit_at_mid 默认 True）。"""
    bars = _oscillation()
    s, ref = _enter(bars)
    assert s._box is not None, "入场 bar 必须检出箱体"
    assert ref == pytest.approx(s._box["mid"]), "参考线必须是入场箱体的 mid"


def test_exit_fires_when_box_gone_and_close_below_frozen_ref():
    """事故原形：箱体消失（is_ranging=False）+ close < 冻结 mid → 必须出场。

    旧代码在 `self._box is None` 时直接 return None——没有箱体就没有出场线，
    这一例在旧代码下**不会**平仓。
    """
    bars = _oscillation()
    s, ref = _enter(bars)
    assert ref == pytest.approx(100.35)

    # 窗口低点砸到 80.3：下沿候选 98.87 > 80.3*1.15=92.3，过不了粗筛 → 箱体消失
    crash = [_kline(len(bars) + j, 102.0, 102.0, c - 0.3, c)
             for j, c in enumerate([97.0, 88.0, 80.6])]
    history = bars + [_kline(len(bars), 101.5, 103.2, 101.4, 103.0)] + crash
    exit_bar = _kline(len(history), 80.0, 98.5, 80.0, 99.5)  # close 99.5 < 100.35

    cls = _load_strategy_cls()
    s2 = _make(cls, position="9.708738", ref=ref)
    sig = s2.on_bar(exit_bar, history + [exit_bar])

    assert s2._box is None, "这段数据的前提：箱体确实消失了"
    assert sig is not None and sig.side == "sell", "箱体消失也不得丢出场线"
    assert sig.signal_name == "box_breakdown_exit"


def test_no_exit_when_box_gone_and_close_above_frozen_ref():
    """对照组：箱体消失但 close 仍在冻结参考线上方 → 不出场（不放大出场敏感度）。"""
    bars = _oscillation()
    s, ref = _enter(bars)

    crash = [_kline(len(bars) + j, 102.0, 102.0, c - 0.3, c)
             for j, c in enumerate([97.0, 88.0, 80.6])]
    history = bars + [_kline(len(bars), 101.5, 103.2, 101.4, 103.0)] + crash
    hold_bar = _kline(len(history), 100.5, 100.8, 100.2, 100.6)  # close 100.6 > 100.35

    cls = _load_strategy_cls()
    s2 = _make(cls, position="9.708738", ref=ref)
    sig = s2.on_bar(hold_bar, history + [hold_bar])

    assert s2._box is None
    assert sig is None, "close 在冻结参考线上方不得出场"


def test_restart_recovers_ref_from_current_box():
    """runner 重启丢了内存里的参考线，但当时能检出箱体 → 重新取作参考线并照常评估。

    这是重启窗口的兜底：宁可临时借用当前箱体，也不能让持仓裸奔没有出场线。
    """
    cls = _load_strategy_cls()
    bars = _oscillation()
    # bars[137] 的 close ≈ 98.92 < mid 100.35：参考线重新取为 100.35 后必须出场
    current = bars[-4]
    s = _make(cls, position="100")
    sig = s.on_bar(current, bars[:-3])

    assert sig is not None and sig.side == "sell"
    assert s._entry_ref == pytest.approx(100.35), "重启后应从当前箱体重新取参考线"


def test_ref_cleared_when_flat_again():
    """空仓后参考线必须清零：上一段持仓的残留参考线不得污染下一段持仓。

    否则「平掉 → 空仓期间参考线残留 → 仓位被外部重建」时，出场会对着陈旧的线
    立即市价平。
    """
    bars = _oscillation()
    cls = _load_strategy_cls()
    s, ref = _enter(bars)
    assert ref is not None

    # 空仓（模拟平掉后 ctx.position 归 0），喂一根仍在箱内的 bar
    flat_bar = bars[-1]
    sig = s.on_bar(flat_bar, bars)
    assert sig is None  # 空仓、无突破 → 无信号
    assert s._entry_ref is None, "空仓必须清掉残留参考线"
