"""
箱体时间区间突破策略（基于 detect_box_range）

每根 K 线用 detect_box_range 识别最近箱体（含上下边界与时间起止），
收盘价向上突破箱体上沿时做多；平仓参考线**冻结在入场那一刻**（入场箱体的
中线/下沿），持仓期间收盘跌破该线即平仓——不再逐 bar 重算箱体。

2026-10-07 DOGE/USDT 事故后修复（原逻辑的两个洞）：
  1. 出场参考线随 20 日窗口漂移：入场后的箱体 mid 逐 bar 重算，出场点不可预期；
  2. 箱体「消失」（is_ranging=False）时 on_bar 直接 return None——持仓期间
     没有任何出场线，止损形同虚设（0.0934 区间无箱体 → 跌到 0.088 才触发）。

2026-10-12 SOL/USDT 审计后修复（入场阈值自相矛盾 + 守卫失效 + 量能门不可信）：

  ① 突破/在箱两套阈值互相矛盾。`detect_box_range` 判定「仍在箱内」用的是
     `upper×(1+tol)`（`indicators.py:_box_time_stats`），而入场判定突破用的是
     裸 `upper`。于是 `upper < close <= upper×(1+tol)` 这段**同时**满足
     「已突破」与「仍在箱内」——触发入场的那根 bar 自己被算成箱体最后一根
     （SOL 那单：margin +0.173% < tol 0.686%，end_index 正落在这根突破 bar 上）。
     现在把两个阈值分开命名：`in_box_ceiling = upper×(1+tol)`（与 detect_box_range
     同一公式）与 `breakout_threshold = upper×(1+tol×min_breakout_tol)`。
     默认 min_breakout_tol=1.0 时两者相等，「未突破」≡「在箱内」，互补不重叠。

  ② 守卫与「在箱」共用上限本身是对的（"仍在箱内"就该按在箱定义判），问题在于
     突破门槛比它低——旧代码用裸 `upper`，于是 `upper < prev_close <= upper×(1+tol)`
     这一段既算「已突破」又算「仍在箱内」，守卫放行追高续入。
     （近 1000 根实测：旧规则 15 个入场里 8 个前一根已收在 upper 之上。）
     现在门槛抬到与在箱上限一致，重叠带消失；守卫表达式保持不变，
     所以 min_breakout_tol=0 能逐字复现旧行为。

  ③ 量能门 MA 自包含：`window[-n:]` 含当前（突破）bar 自己，把测量倍数系统性
     压低（真实 5× 测成 4.17×、10× 测成 6.90×），门槛 1.2 实际要求真实 1.213×。
     改为严格用突破 bar **之前**的 n 根，即标准口径。

  ④ 量能门默认值保持 False（`volume_filter`）：4 年 SOL 回测显示开启 1.2 会拦掉
     2023-09-29 那笔从未触及出场线的大额盈利单，反而更差——但那是「未平仓浮盈」
     不是已实现收益，不能据此认为量能门无效。此参数应由会话显式决策，不再靠
     静默兜底（配套修复：实盘参数下发已改为注入 params_schema 默认值）。

接入位置：~/.tradelogx/strategies/（与仓库内 backend/strategies/ 策略同构，
通过 apps.strategy_engine.registry 注册、由策略加载器动态导入）。
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from apps.strategy_engine.base import BaseStrategy, StrategyContext
from apps.strategy_engine.indicators import detect_box_range
from apps.strategy_engine.registry import register_strategy


@register_strategy(name="box_time_range_breakout_strategy")
class BoxTimeRangeBreakoutStrategy(BaseStrategy):
    """detect_box_range 箱体突破策略"""

    name = "box_time_range_breakout_strategy"
    description = """策略类型：突破
    核心指标：箱体（pivot 聚类 + 窗口粗筛 + 时间起止）
    适用场景：震荡后启动的突破行情
    入场逻辑：收盘价突破箱体上沿并清出容差带（close > upper×(1+tol×min_breakout_tol)）且前一根尚未突破时买入；出场参考线冻结在入场时刻（中线/下沿），持仓期间收盘跌破该线即平仓"""
    min_kline_length = 300

    params_schema = {
        "lookback": {"type": "int", "default": 120, "description": "箱体检测窗口K线数"},
        "max_width_pct": {"type": "float", "default": 0.04, "description": "箱体最大相对宽度"},
        "min_width_pct": {
            "type": "float",
            "default": 0.005,
            "description": "箱体最小相对宽度（0.005=0.5%，防止刀锋薄箱体被当成箱体）；0=不限制",
        },
        "pivot_window": {"type": "int", "default": 2, "description": "局部极值左右各多少根K线"},
        "min_gap_bars": {"type": "int", "default": 3, "description": "同一边界两次触碰最小间隔"},
        "min_pivots": {"type": "int", "default": 2, "description": "边界最少聚合极值点数"},
        "min_touches": {"type": "int", "default": 2, "description": "边界最少触碰次数"},
        "atr_period": {"type": "int", "default": 14, "description": "触碰容差 ATR 周期"},
        "min_duration_bars": {
            "type": "int",
            "default": 12,
            "description": "箱体最少持续 K 线根数（duration_bars：首根在箱 close → 末根在箱 close，含中间离箱间隙）；0=不限制",
        },
        "exit_at_mid": {"type": "bool", "default": True, "description": "True=跌破中线平仓，False=跌破下沿平仓（参考线均冻结在入场时刻）"},
        "position_pct": {"type": "float", "default": 0.1, "min": 0.0, "max": 1.0, "description": "每次开仓占总资金比例（如 0.1 表示 10%）"},
        "min_breakout_tol": {
            "type": "float",
            "default": 1.0,
            "min": 0.0,
            "description": "突破需清出的容差倍数：close > upper×(1+tol×该值)。1.0=必须清出整个容差带（默认，此时门槛 == 「在箱」上限，两者互补）；0=只要 close>upper 即算突破（逐字复现 2026-10-12 之前的旧行为，用于 A/B 与回退）",
        },
        "volume_filter": {"type": "bool", "default": False, "description": "True=启用突破 K 线量能过滤（小于 N 倍均量则跳过入场）"},
        "volume_ma_period": {"type": "int", "default": 20, "min": 1, "description": "突破 K 线量能过滤的均量窗口（根 K 线，取突破 bar **之前**的 N 根，不含自身）"},
        "volume_mult": {"type": "float", "default": 1.0, "min": 0.0, "description": "突破 K 线量 >= volume_mult × N 根均量 才入场；1.0 表示至少等于均量，1.2 表示 120% 均量"},
    }

    def __init__(self, ctx: StrategyContext) -> None:
        super().__init__(ctx)
        self.lookback: int = int(self.ctx.params.get("lookback", 120))
        self.max_width_pct: float = float(self.ctx.params.get("max_width_pct", 0.04))
        _mw = self.ctx.params.get("min_width_pct")
        self.min_width_pct: float = float(_mw) if _mw is not None else 0.005
        self.pivot_window: int = int(self.ctx.params.get("pivot_window", 2))
        self.min_gap_bars: int = int(self.ctx.params.get("min_gap_bars", 3))
        self.min_pivots: int = int(self.ctx.params.get("min_pivots", 2))
        self.min_touches: int = int(self.ctx.params.get("min_touches", 2))
        self.atr_period: int = int(self.ctx.params.get("atr_period", 14))
        self.min_duration_bars: int = int(self.ctx.params.get("min_duration_bars", 12))
        self.exit_at_mid: bool = bool(self.ctx.params.get("exit_at_mid", True))
        self.position_pct: Decimal = Decimal(str(self.ctx.params.get("position_pct", 0.1)))
        self.min_breakout_tol: float = float(self.ctx.params.get("min_breakout_tol", 1.0))
        self.volume_filter: bool = bool(self.ctx.params.get("volume_filter", False))
        self.volume_ma_period: int = max(1, int(self.ctx.params.get("volume_ma_period", 20)))
        self.volume_mult: float = float(self.ctx.params.get("volume_mult", 1.0))

        self._box: dict | None = None  # 最近一次检测到的箱体（缓存，含时间字段）
        # 出场参考线：入场那一刻冻结（入场箱体的 mid 或 lower）。持仓期间只跟它比，
        # 不逐 bar 重算——重算会让参考线随检测窗口漂移，且箱体「消失」时无线可退。
        self._entry_ref: float | None = None

    def on_bar(self, kline: dict, history: list[dict]):
        # ATR 需要预热：detect_box_range 的容差来自最新 ATR
        min_bars = self.lookback + self.atr_period + 2
        if len(history) < min_bars:
            return None

        window = history[-self.lookback:]  # 尾部窗口，末根 = 当前 K 线（时间正序）
        data = detect_box_range(
            klines=window,
            max_width_pct=self.max_width_pct,
            min_width_pct=self.min_width_pct,
            pivot_window=self.pivot_window,
            min_gap_bars=self.min_gap_bars,
            min_pivots=self.min_pivots,
            min_touches=self.min_touches,
            atr_period=self.atr_period,
            min_duration_bars=self.min_duration_bars,
        )
        self._box = data["box"] if data["is_ranging"] else None
        close = float(kline["close"])

        # —— 持仓：跌破**冻结的**出场参考线即平仓（止损必须始终可评估）——
        # 参考线冻结在入场那一刻（入场箱体的 mid/lower），不逐 bar 重算：
        # ① 箱体 mid 随检测窗口漂移，出场点不可预期；
        # ② 箱体「消失」（is_ranging=False）时原来直接 return None——持仓期间
        #    没有任何出场线，止损形同虚设（2026-10-07 DOGE/USDT：0.0934 区间
        #    无箱体，跌到 0.088 才触发出场）。
        if self.ctx.position > 0:
            if self._entry_ref is None:
                # runner 重启丢了参考线（内存态）：当前 bar 能检出箱体就重新取作
                # 参考线；否则本 bar 延后处理（宁可错过出场，不在已远离的价格
                # 立即市价平）。
                if self._box is None:
                    return None
                self._entry_ref = (
                    self._box["mid"] if self.exit_at_mid else self._box["lower"]
                )
            if close < self._entry_ref:
                return self.ctx.close_position(signal_name="box_breakdown_exit")
            return None

        # —— 空仓 ——
        self._entry_ref = None  # 空仓：清掉上一段持仓的残留参考线
        if self._box is None:
            return None

        upper = self._box["upper"]
        lower = self._box["lower"]
        mid = self._box["mid"]
        tol = self._box["tolerance_pct"]

        # 两个阈值必须分开命名，语义各自独立：
        #   in_box_ceiling     = detect_box_range 判「仍在箱内」用的上限（容差带顶）
        #   breakout_threshold = 本策略要求的突破门槛
        # 旧代码把「突破」写成裸 upper、把「在箱」写成 upper×(1+tol)，于是
        # (upper, upper×(1+tol)] 这一段同时满足两者——触发入场的 bar 自己被算成
        # 箱体最后一根（见模块 docstring ①②）。
        in_box_ceiling = upper * (1 + tol)
        breakout_threshold = upper * (1 + tol * self.min_breakout_tol)

        # 突破上沿做多。守卫「前一根收盘仍在箱内」用的就是**在箱上限**——
        # 它字面意思即「前一根按 detect_box_range 的定义还在箱里」。
        # 默认 min_breakout_tol=1.0 时 breakout_threshold == in_box_ceiling，
        # 「未突破」与「在箱内」互补、不再重叠；而守卫不随门槛缩放，
        # 所以 min_breakout_tol=0 能逐字复现旧行为（真回退开关）。
        prev_close = (
            float(window[-2]["close"]) if len(window) >= 2 else float("inf")
        )
        if close > breakout_threshold and prev_close <= in_box_ceiling:
            # 量能过滤：突破 K 线量需 >= volume_mult × 之前 N 根均量
            # 均量**不含当前 bar**：含自身会把测量倍数压低（真实 5× 测成 4.17×），
            # 门槛随放量程度非线性失真，属自包含偏置。
            if self.volume_filter:
                ma_n = min(self.volume_ma_period, len(window) - 1)
                if ma_n > 0:
                    prior = window[-(ma_n + 1) : -1]
                    avg_vol = sum(float(k["volume"]) for k in prior) / ma_n
                    cur_vol = float(kline["volume"])
                    if cur_vol < avg_vol * self.volume_mult:
                        return None
            if not (Decimal("0") < self.position_pct <= Decimal("1")):
                return None
            # 开仓量 = 总资金 × position_pct ÷ 当前收盘价
            # （空仓时无持仓市值，total_capital == balance）
            total_capital = self.ctx.to_portfolio_context().total_capital
            price = Decimal(str(close))
            if price <= Decimal("0"):
                return None
            qty = (total_capital * self.position_pct / price).quantize(
                Decimal("0.000001"), rounding=ROUND_HALF_UP
            )
            if qty <= Decimal("0"):
                return None
            # 出场参考线冻结在入场时刻（触发入场的箱体 mid/lower）
            self._entry_ref = mid if self.exit_at_mid else lower
            return self.ctx.buy(
                quantity=qty,
                signal_name="box_breakout_long",
                metadata={
                    "box_upper": upper,
                    "box_lower": lower,
                    "box_mid": mid,
                    "box_start_index": self._box.get("start_index"),
                    "box_end_index": self._box.get("end_index"),
                    # 审计字段：下单时即可回看这次突破清出了多少容差
                    "box_tolerance_pct": tol,
                    "breakout_margin_pct": close / upper - 1,
                    "breakout_threshold": breakout_threshold,
                    "in_box_ceiling": in_box_ceiling,
                },
            )

        return None
