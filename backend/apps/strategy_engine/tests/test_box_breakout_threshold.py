"""箱体突破策略：突破/在箱阈值统一 + 守卫修正 + 量能门口径（2026-10-12 SOL/USDT 审计）。

事故形状（生产实测，非构造）：

  ① 两套阈值互相矛盾。`detect_box_range` 判「仍在箱内」用 `upper×(1+tol)`
     （`indicators.py:_box_time_stats`），入场判突破用裸 `upper`。于是
     `upper < close <= upper×(1+tol)` 同时满足「已突破」与「仍在箱内」——
     SOL 那单 margin=+0.173% < tol=0.686%，触发入场的 bar 自己被算成箱体
     最后一根（end_index 正落在它身上）。

  ② 守卫失效。「前一根收盘仍在箱内」写成 `prev_close <= upper×(1+tol)`，
     于是**允许前一根已收在上沿之上**（最多 tol）。近 1000 根实测：15 个入场
     里 8 个（53.3%）前一根已经收在 upper 之上。

  ③ 量能门 MA 自包含：`window[-n:]` 含当前突破 bar 自身，把测量倍数系统性压低
     （真实 5× 测成 4.17×、10× 测成 6.90×）。

被测策略是用户策略文件（`~/.tradelogx/strategies/`），路径与 test_box_frozen_exit.py
同源；文件不在时跳过（本地 macOS 没有 /root，生产容器里有）。

断言一律**以决策当刻的箱体为准**（`s._box`）：突破 bar 本身也参与 detect_box_range
的窗口，入场时算出的 upper/tol 与入场前那根 probe 上读到的不必然相同。
"""

from __future__ import annotations

import importlib.util
import math
import os
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from apps.strategy_engine.base import StrategyContext

STRATEGY_FILENAME = "box_time_range_breakout_strategy.py"

T0 = 1789000000000


def _strategy_path() -> str | None:
    """定位策略文件。

    按「部署位优先、仓库镜像兜底」的顺序找：

    1. `BOX_STRATEGY_PATH` —— 显式覆盖，用于对某份候选文件跑同一组合约
       （上线前拿新版跑一遍，确认它真满足这些断言）；
    2. 部署位 `~/.tradelogx/strategies/`（容器里 HOME=/root）—— **真正在跑的**
       那一份；
    3. 仓库镜像 `backend/strategies/` —— CI 与开发机上没有 `~/.tradelogx`，
       没有这一条这些断言会被**静默跳过**（skip 不是 pass，等于 CI 从不检查
       这组不变量）。

    第 2 条排在前面是有意的：生产跑的是部署位那份，契约必须对**它**成立。
    两份不一致本身就是缺陷（仓库镜像会漂移），届时这些断言会在部署位上报错。

    **仓库镜像刻意保持与部署位逐字节一致，因此它没有过 `ruff format`。**
    这不是漏掉了：镜像的全部价值就是"生产跑的那份长什么样"，一旦格式化，
    部署位与仓库就只差格式——`md5` 比对立刻失效，漂移再也测不出来，镜像退化成
    一份"意译"。CI 的格式门只覆盖 `apps/`，不含 `backend/strategies/`，所以
    留着不动不会让任何检查变红。要改策略请改**部署位**，再把原样复制过来。
    """
    override = os.environ.get("BOX_STRATEGY_PATH")
    if override:
        return override if os.path.isfile(override) else None

    # 本文件在 backend/apps/strategy_engine/tests/ → parents[3] = backend/
    repo_mirror = Path(__file__).resolve().parents[3] / "strategies" / STRATEGY_FILENAME
    candidates = [
        Path("/root/.tradelogx/strategies") / STRATEGY_FILENAME,
        Path.home() / ".tradelogx" / "strategies" / STRATEGY_FILENAME,
        repo_mirror,
    ]
    for p in candidates:
        if p.is_file():
            return str(p)
    return None


def _load_strategy_cls():
    path = _strategy_path()
    if path is None:
        pytest.skip(f"{STRATEGY_FILENAME} not found in any known strategy dir")
    spec = importlib.util.spec_from_file_location(
        "box_time_range_breakout_p2_test", path
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod.BoxTimeRangeBreakoutStrategy


def _kline(i, o, h, low, c, v=1000.0):
    return {
        "timestamp": T0 + i * 3600000,
        "open": o,
        "high": h,
        "low": low,
        "close": c,
        "volume": v,
    }


def _oscillation(n=140):
    """周期 6 正弦震荡：close 在 98.7 ~ 102.0，mid≈100.35（与 frozen_exit 测试同形）。"""
    bars = []
    for i in range(n):
        c = 100.35 + 1.65 * math.sin(2 * math.pi * i / 6)
        o = 100.35 + 1.65 * math.sin(2 * math.pi * (i - 1) / 6)
        bars.append(
            _kline(
                i,
                round(o, 4),
                round(max(o, c) + 0.05, 4),
                round(min(o, c) - 0.05, 4),
                round(c, 4),
            )
        )
    return bars


def _make(cls, params=None, position="0"):
    ctx = StrategyContext(
        symbol="TEST/USDT",
        timeframe="1h",
        mode="backtest",
        params={"position_pct": 0.1, **(params or {})},
        balance=Decimal("10000"),
        position=Decimal(position),
    )
    return cls(ctx)


def _decide(s, bars, close, volume=1000.0):
    """喂一根收盘价 = close 的 bar，返回 (信号, 决策当刻的箱体)。"""
    bar = _kline(len(bars), close, close + 0.05, close - 0.05, close, v=volume)
    sig = s.on_bar(bar, bars + [bar])
    return sig, s._box


def test_breakout_threshold_equals_the_in_box_ceiling():
    """核心不变量：策略的突破门槛 == detect_box_range 的「在箱」上限。

    两者一旦分家，就存在一段 close 同时满足「已突破」与「仍在箱内」——
    2026-10-12 SOL 那单正是落在这一段里（margin +0.173% < tol 0.686%）。
    """
    from apps.strategy_engine.indicators import detect_box_range

    cls = _load_strategy_cls()
    s = _make(cls)
    bars = _oscillation()

    # 走一根仍在箱内的 bar，让策略缓存箱体
    probe = bars[-1]
    s.on_bar(probe, bars)
    box = s._box
    assert box is not None, "震荡段必须能检出箱体"

    # 独立复算「在箱」上限（与 indicators._box_time_stats 同一公式）
    fresh = detect_box_range(
        klines=bars[-s.lookback :],
        max_width_pct=s.max_width_pct,
        min_width_pct=s.min_width_pct,
        pivot_window=s.pivot_window,
        min_gap_bars=s.min_gap_bars,
        min_pivots=s.min_pivots,
        min_touches=s.min_touches,
        atr_period=s.atr_period,
        min_duration_bars=s.min_duration_bars,
    )["box"]
    in_box_ceiling = fresh["upper"] * (1 + fresh["tolerance_pct"])
    threshold = fresh["upper"] * (1 + fresh["tolerance_pct"] * s.min_breakout_tol)

    assert s.min_breakout_tol == pytest.approx(1.0), "默认必须清出整个容差带"
    assert threshold == pytest.approx(in_box_ceiling), (
        "突破门槛必须等于「在箱」上限——否则两套阈值重叠"
    )


def test_marginal_breakout_inside_tolerance_band_is_rejected():
    """核心回归：突破幅度 < 容差（噪声级突破）不得入场——SOL 那单的形状。"""
    cls = _load_strategy_cls()
    s = _make(cls)
    bars = _oscillation()

    probe = bars[-1]
    s.on_bar(probe, bars)
    box0 = s._box
    assert box0 is not None

    # 落在 (upper, upper*(1+tol)) 内：旧代码入场，新代码必须拒绝
    marginal = box0["upper"] * (1 + box0["tolerance_pct"] * 0.5)
    sig, box = _decide(s, bars, marginal)

    assert box is not None, "前提：该 bar 仍能检出箱体"
    margin = marginal / box["upper"] - 1
    assert margin < box["tolerance_pct"], (
        f"构造前提：margin={margin:.5%} 必须 < tol={box['tolerance_pct']:.5%}"
    )
    assert sig is None, "噪声级突破（margin < tol）不得入场"


def test_breakout_clearing_the_tolerance_band_is_accepted():
    """对照组：清出整个容差带后必须照常入场（不能把修复做成过度收紧）。"""
    cls = _load_strategy_cls()
    s = _make(cls)
    bars = _oscillation()

    probe = bars[-1]
    s.on_bar(probe, bars)
    box0 = s._box
    strong = box0["upper"] * (1 + box0["tolerance_pct"] * 3.0)

    sig, box = _decide(s, bars, strong)

    assert box is not None
    margin = strong / box["upper"] - 1
    assert margin > box["tolerance_pct"], "构造前提：必须清出容差带"
    assert sig is not None and sig.side == "buy", "清出容差带的突破必须入场"
    assert sig.signal_name == "box_breakout_long"


def test_min_breakout_tol_zero_restores_legacy_behaviour():
    """可回退性：min_breakout_tol=0 时必须复现「close>upper 即突破」的旧行为。

    审计修复若无法回退，就没法在不改代码的前提下做 A/B——这条锁住回退开关。
    """
    cls = _load_strategy_cls()
    s = _make(cls, {"min_breakout_tol": 0.0})
    bars = _oscillation()

    probe = bars[-1]
    s.on_bar(probe, bars)
    box0 = s._box
    assert s.min_breakout_tol == pytest.approx(0.0)

    marginal = box0["upper"] * (1 + box0["tolerance_pct"] * 0.5)
    sig, box = _decide(s, bars, marginal)

    assert box is not None
    margin = marginal / box["upper"] - 1
    assert 0 < margin < box["tolerance_pct"], "构造前提：过 upper 但未清出容差带"
    assert sig is not None and sig.side == "buy", (
        "min_breakout_tol=0 必须回退到旧行为（放进噪声级突破）"
    )
    # 回退必须包含守卫：旧代码守卫上限恒为 upper*(1+tol)，与 k 无关
    import inspect

    src = inspect.getsource(type(s).on_bar)
    assert "prev_close <= in_box_ceiling" in src, (
        "守卫上限不得随 min_breakout_tol 缩放，否则 k=0 不是逐字回退"
    )


def test_guard_rejects_when_prev_close_already_broke_out():
    """守卫：前一根收盘已在**突破门槛**之上（追高续入）不得入场。

    这里锁的是「门槛之上」这一侧。旧代码的守卫上限是 `upper*(1+tol)`，而它判突破
    用的是裸 `upper`——于是 `upper < prev_close <= upper*(1+tol)` 这一段既算
    「已突破」又算「仍在箱内」，守卫放行追高续入（近 1000 根实测 8/15 属此列）。

    修复后门槛统一为 `upper*(1+tol*k)`：守卫上限与突破门槛**同一个数**，
    「未突破」与「在箱内」互补，不再有重叠段。
    """
    cls = _load_strategy_cls()
    s = _make(cls)
    bars = _oscillation()

    probe = bars[-1]
    s.on_bar(probe, bars)
    box0 = s._box
    upper = box0["upper"]

    # 前一根收在门槛之上（1.006×upper 已越过 tol≈0.0053 的容差带）
    prev = upper * 1.006
    bars2 = list(bars)
    bars2[-1] = _kline(len(bars2) - 1, prev, upper * 0.999, upper * 0.98, prev)

    strong = upper * 1.02
    sig, box = _decide(s, bars2, strong)

    assert box is not None
    # 门槛 = 在箱上限（min_breakout_tol=1.0），守卫用的也是它
    ceiling = box["upper"] * (1 + box["tolerance_pct"])
    assert prev > ceiling, "构造前提：前一根收盘已在门槛之上"
    assert sig is None, "前一根已突破 = 追高续入，守卫必须拦下"


def test_guard_ceiling_is_the_same_number_as_the_breakout_threshold():
    """守卫上限与突破门槛必须**是同一个表达式**——这是修复的核心不变量。

    旧代码里守卫上限是 `upper*(1+tol)`、突破门槛是 `upper`：两个不同的数，
    中间夹出一条「既算突破又算在箱」的重叠带（SOL 那单就落在里面）。
    """
    cls = _load_strategy_cls()
    s = _make(cls)
    bars = _oscillation()

    probe = bars[-1]
    s.on_bar(probe, bars)
    box = s._box
    assert box is not None

    # 策略源码里两个阈值必须是具名变量、且守卫不得随门槛缩放
    import inspect

    src = inspect.getsource(type(s).on_bar)
    assert "in_box_ceiling = upper * (1 + tol)" in src, (
        "在箱上限必须按 detect_box_range 的公式具名"
    )
    assert "breakout_threshold = upper * (1 + tol * self.min_breakout_tol)" in src, (
        "突破门槛必须提为具名变量"
    )
    assert "prev_close <= in_box_ceiling" in src, (
        "守卫必须用**在箱上限**（不随门槛缩放）——"
        "若改成随门槛缩放，min_breakout_tol=0 就不再是旧行为的逐字回退"
    )


def test_guard_allows_when_prev_close_is_still_inside_the_box():
    """对照组：前一根确实还在箱内时，突破必须放行（守卫不能拦掉正常入场）。"""
    cls = _load_strategy_cls()
    s = _make(cls)
    bars = _oscillation()

    probe = bars[-1]
    s.on_bar(probe, bars)
    box0 = s._box
    upper = box0["upper"]

    # 前一根收在 upper 之下（真·箱内）
    prev = upper * 0.995
    bars2 = list(bars)
    bars2[-1] = _kline(len(bars2) - 1, prev, upper * 0.999, upper * 0.98, prev)

    strong = upper * 1.02
    sig, box = _decide(s, bars2, strong)

    assert box is not None
    ceiling = box["upper"] * (1 + box["tolerance_pct"])
    assert prev <= ceiling, "构造前提：前一根尚未突破（在箱内）"
    assert sig is not None and sig.side == "buy", "前一根在箱内的突破必须放行"


def test_volume_ma_excludes_the_breakout_bar_itself():
    """量能门 MA 不含当前 bar：自包含会把测量倍数系统性压低。

    构造（n=20，前 20 根量 100，突破 bar 量 130，门 mult=1.29）：
      不含自身 MA = 100          → 倍数 1.300 ≥ 1.29  → 通过（修复后）
      含自身   MA = 101.5        → 倍数 1.281 < 1.29  → 被拦（修复前）
    断言把两种口径都算出来，锁住差异确实存在。
    """
    n = 20
    mult = 1.29
    prior_vol, cur_vol = 100.0, 130.0

    cls = _load_strategy_cls()
    s = _make(cls, {"volume_filter": True, "volume_mult": mult, "volume_ma_period": n})
    bars = _oscillation()
    for b in bars:
        b["volume"] = prior_vol

    probe = bars[-1]
    s.on_bar(probe, bars)
    box0 = s._box
    strong = box0["upper"] * (1 + box0["tolerance_pct"] * 3.0)

    # 两种口径的解析值（先断言它们确实不同，否则这个测试没有区分力）
    ma_excl = prior_vol
    ma_incl = (prior_vol * (n - 1) + cur_vol) / n
    assert cur_vol / ma_excl >= mult, "前提：严格口径下必须通过"
    assert cur_vol / ma_incl < mult, "前提：自包含口径下必须被拦（这正是被修掉的偏置）"

    sig, box = _decide(s, bars, strong, volume=cur_vol)

    assert box is not None
    assert sig is not None and sig.side == "buy", (
        f"量 {cur_vol} 为前 {n} 根均量 {ma_excl} 的 {cur_vol / ma_excl:.3f} 倍，"
        f"门 {mult}：不含自身的正确口径下必须通过"
        f"（含自身的旧口径算成 {cur_vol / ma_incl:.3f} 会误拦）"
    )


def test_volume_filter_still_blocks_weak_volume():
    """对照组：量能门确实还在拦——量明显低于均量时不得入场。"""
    cls = _load_strategy_cls()
    s = _make(cls, {"volume_filter": True, "volume_mult": 1.0, "volume_ma_period": 20})
    bars = _oscillation()
    for b in bars:
        b["volume"] = 1000.0

    probe = bars[-1]
    s.on_bar(probe, bars)
    box0 = s._box
    strong = box0["upper"] * (1 + box0["tolerance_pct"] * 3.0)

    sig, box = _decide(s, bars, strong, volume=100.0)

    assert box is not None
    assert sig is None, "量 100 vs 均量 1000（0.1 倍）必须被量能门拦下"


def test_volume_filter_default_is_off_and_declared():
    """量能门默认关闭，但必须**在 params_schema 里声明**。

    声明是 P1 修复的前提：实盘现在注入 schema 默认值，声明了才会被显式下发，
    而不是靠策略 __init__ 的兜底——那正是 SOL 会话「界面上看不到」的成因。
    """
    cls = _load_strategy_cls()
    schema = cls.params_schema

    assert schema["volume_filter"]["default"] is False
    assert "volume_mult" in schema
    assert "volume_ma_period" in schema
    assert "min_breakout_tol" in schema, "新增门槛必须声明，否则实盘拿不到"
    assert schema["min_breakout_tol"]["default"] == pytest.approx(1.0)


def test_entry_metadata_records_breakout_margin():
    """下单元数据带上突破幅度与容差——审计时要能直接从订单回看这次突破的质量。"""
    cls = _load_strategy_cls()
    s = _make(cls)
    bars = _oscillation()

    probe = bars[-1]
    s.on_bar(probe, bars)
    box0 = s._box
    strong = box0["upper"] * (1 + box0["tolerance_pct"] * 3.0)

    sig, box = _decide(s, bars, strong)

    assert sig is not None
    md = sig.metadata
    assert md["box_tolerance_pct"] == pytest.approx(box["tolerance_pct"])
    assert md["breakout_margin_pct"] == pytest.approx(strong / box["upper"] - 1)
    assert md["breakout_threshold"] == pytest.approx(
        box["upper"] * (1 + box["tolerance_pct"])
    )
    assert md["breakout_margin_pct"] > md["box_tolerance_pct"], (
        "记录的是清出容差后的突破"
    )
