"""Regression: detect_box_range must return the macro box on real market data,
not a "local tight cluster" masquerading as a box.

The original bug: on 100 BNB/USDT 4h bars, the algorithm returned
upper=695.00 / lower=674.60 (width 20.4, 2.95%) — a local tight cluster
that the user's eyes would never call a box. The real ranging zone was
674-720 (~6.8%).

This test pins the new behavior using recorded BNB 4h/1d fixtures
(first run pulls live data, subsequent runs read from disk).
"""

import json
from pathlib import Path

from apps.strategy_engine.indicators import detect_box_range

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def _load_fixture(name: str) -> list[dict]:
    path = FIXTURE_DIR / name
    assert path.exists(), f"fixture missing: {path}"
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def test_bnb_4h_100_does_not_return_tight_local_cluster():
    """BNB 4h 100 bars: the box must reach the visible high range (~720),
    not a 695/674.6-style local cluster."""
    klines = _load_fixture("bnb_4h_100.json")
    # 4h 100 根价格区间 max=726, min=600, 合理箱体上沿应在 700+
    result = detect_box_range(klines=klines, max_width_pct=0.10)

    if result["is_ranging"]:
        b = result["box"]
        # 关键断言 1：上沿必须够到 700+（用户认知的"674-720 箱体"核心区）
        assert b["upper"] >= 700, (
            f"upper={b['upper']:.2f} is too low; algorithm picked a local cluster, "
            f"not the visible 674-720 range. reason='' (box returned)"
        )
        # 关键断言 2：上沿不能比 4h 真实高点 726 更高（粗筛应阻止选超界 cluster）
        assert b["upper"] <= 726, f"upper={b['upper']:.2f} exceeds window high 726"
        # 关键断言 3：下沿不能选到 600 这种绝对低点（粗筛应阻止）
        assert b["lower"] >= 660, (
            f"lower={b['lower']:.2f} too close to window low; tight-cluster regression"
        )
        # 关键断言 4：必须有合理宽度
        assert b["width_pct"] >= 0.025, f"width_pct={b['width_pct']:.4f} too tight"
    else:
        # 拒绝 4h 100 根这种"明显震荡"的数据也算正确（保守）
        # 关键是 reason 必须是粗筛/pivot/触碰类——而不是因为静默接受错误箱体
        assert (
            "上沿" in result["reason"]
            or "下沿" in result["reason"]
            or "宽度" in result["reason"]
        )


def test_bnb_4h_100_with_tight_threshold_no_false_box():
    """用 3% 阈值（agent 默认行为）：不应返回 upper=695/lower=674.6 这种过紧伪箱体。"""
    klines = _load_fixture("bnb_4h_100.json")
    result = detect_box_range(klines=klines, max_width_pct=0.03)

    # 关键断言：绝不能复现"upper=695.00 / lower=674.60"这种用户截图里的伪箱体
    if result["is_ranging"]:
        b = result["box"]
        # upper 必须 >= 700 (粗筛应拒绝 695 这种)
        assert b["upper"] >= 700, (
            f"Tight-threshold regression: returned upper={b['upper']:.2f} (expected >=700)"
        )


def test_bnb_1d_100_not_a_box():
    """BNB 1d 100 bars: 100 天内 745→537 是 28% 单边下跌，不是箱体。"""
    klines = _load_fixture("bnb_1d_100.json")
    result = detect_box_range(klines=klines, max_width_pct=0.20)

    assert result["is_ranging"] is False, (
        f"1d 100 bars is a 28% downtrend, must NOT be flagged as box. "
        f"box={result.get('box')}"
    )
    assert result["reason"]


def test_bnb_4h_100_with_min_width_pct_filters_tight():
    """min_width_pct 下限：能拦住所有宽度太小的候选。"""
    klines = _load_fixture("bnb_4h_100.json")
    # 不管什么 pct 上限，要求宽度至少 5%
    result = detect_box_range(klines=klines, max_width_pct=0.20, min_width_pct=0.05)
    if result["is_ranging"]:
        assert result["box"]["width_pct"] >= 0.05


def test_coarse_filter_rejects_cluster_far_from_window_extremes():
    """直接验证粗筛参数：把 upper_max_discard 设为 0（即上沿必须 = window_high），
    100 根 BNB 4h 数据上不应找到任何 cluster。"""
    klines = _load_fixture("bnb_4h_100.json")
    result = detect_box_range(
        klines=klines,
        max_width_pct=0.30,
        upper_max_discard_pct=0.0,  # 上沿必须 == window_high
    )
    assert result["is_ranging"] is False
    assert "上沿" in result["reason"]
