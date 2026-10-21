"""
策略测试运行器 — MVP 精简版

轻量级策略验证器，用于验证新创建的策略代码是否能正确加载、实例化和运行。
"""

from __future__ import annotations

import importlib.util
import logging
import random
import re
import sys
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _generate_mock_klines(n: int = 100) -> list[dict]:
    """生成模拟 K 线数据（所有价格/成交量均为 Decimal 类型）"""
    random.seed(42)
    base_price = Decimal("50000.00")
    klines = []
    for i in range(n):
        change = Decimal(str(random.uniform(-0.02, 0.02)))
        open_price = base_price * (Decimal("1") + change)
        close_price = base_price * (Decimal("1") + change * Decimal("0.5"))
        high_price = max(open_price, close_price) * (
            Decimal("1") + abs(Decimal(str(random.uniform(0, 0.01))))
        )
        low_price = min(open_price, close_price) * (
            Decimal("1") - abs(Decimal(str(random.uniform(0, 0.01))))
        )
        klines.append(
            {
                "timestamp": f"2024-01-{(i // 24) + 1:02d}T{i % 24:02d}:00:00",
                "open": open_price.quantize(Decimal("0.01")),
                "high": high_price.quantize(Decimal("0.01")),
                "low": low_price.quantize(Decimal("0.01")),
                "close": close_price.quantize(Decimal("0.01")),
                "volume": Decimal(str(round(random.uniform(100, 1000), 2))),
            }
        )
        base_price = close_price
    return klines


def _run_coro_blocking(coro):
    """在同步上下文中运行协程；若已处于事件循环内（如 async 工具调用），
    则在独立线程中运行，避免 'asyncio.run() cannot be called from a
    running event loop'。BacktestEngine.run() 为纯内存计算，子线程运行安全。"""
    import asyncio
    import concurrent.futures

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(asyncio.run, coro).result()


class StrategyTester:
    """策略验证器：加载策略文件 → 验证语法 → 运行模拟回测"""

    def __init__(
        self, kline_count: int = 200, initial_capital: Decimal = Decimal("10000")
    ):
        self.kline_count = kline_count
        self.initial_capital = initial_capital

    def test_strategy_file(self, file_path: str) -> dict[str, Any]:
        """测试单个策略文件"""
        path = Path(file_path)
        if not path.exists():
            return {
                "success": False,
                "strategy_name": None,
                "errors": [f"策略文件不存在: {file_path}"],
                "stats": None,
                "warnings": [],
            }

        # 1) 语法检查
        syntax_ok, syntax_error = self._check_syntax(path)
        if not syntax_ok:
            return {
                "success": False,
                "strategy_name": None,
                "errors": [f"语法错误: {syntax_error}"],
                "stats": None,
                "warnings": [],
            }

        # 1.5) 静态分析 — 扫描 Decimal 类型误用
        type_warnings = self._scan_type_misuse(path)

        # 1.6) 静态分析 — 扫描不存在的 StrategyContext 属性
        ctx_attr_warnings = self._scan_context_attrs(path)

        strategy_cls, load_error = self._load_strategy_module(path)
        if load_error:
            return {
                "success": False,
                "strategy_name": None,
                "errors": [f"加载失败: {load_error}"],
                "stats": None,
                "warnings": type_warnings + ctx_attr_warnings,
            }

        validate_errors = self._validate_strategy_class(strategy_cls)
        if validate_errors:
            return {
                "success": False,
                "strategy_name": getattr(strategy_cls, "name", None),
                "errors": validate_errors,
                "stats": None,
                "warnings": type_warnings + ctx_attr_warnings,
            }

        strategy_name = getattr(strategy_cls, "name", "unnamed")

        # 检查策略注册
        reg_warning = self._check_registration(strategy_cls)

        # 检查get_watch_signals是否返回空列表（实盘信号触发的重要警告）
        signal_warning = self._check_signal_configuration(strategy_cls)

        run_result = self._run_mock_backtest(strategy_cls)
        all_warnings = run_result.get("warnings", [])
        if reg_warning:
            all_warnings.insert(0, reg_warning)
        if signal_warning:
            all_warnings.insert(0, signal_warning)

        if run_result["errors"]:
            return {
                "success": False,
                "strategy_name": strategy_name,
                "errors": run_result["errors"],
                "stats": run_result.get("stats"),
                "warnings": type_warnings + ctx_attr_warnings + all_warnings,
            }

        return {
            "success": True,
            "strategy_name": strategy_name,
            "errors": [],
            "stats": run_result["stats"],
            "warnings": type_warnings + ctx_attr_warnings + all_warnings,
        }

    def _check_syntax(self, path: Path) -> tuple[bool, str | None]:
        """验证 Python 语法"""
        import py_compile

        try:
            with tempfile.NamedTemporaryFile(suffix=".pyc", delete=False) as f:
                py_compile.compile(str(path), f.name, doraise=True)
            return True, None
        except py_compile.PyCompileError as e:
            return False, str(e)
        except Exception as e:
            return False, f"语法检查失败: {e}"

    def _scan_type_misuse(self, path: Path) -> list[str]:
        """静态扫描策略源码中的 Decimal 类型误用风险"""
        warnings: list[str] = []
        source = path.read_text(encoding="utf-8")
        lines = source.splitlines()

        # 检查是否导入了 Decimal
        uses_decimal = any(
            re.search(r"from\s+decimal\s+import", line)
            or re.search(r"import\s+decimal", line)
            for line in lines
        )
        if not uses_decimal:
            return warnings  # 没有 Decimal 用法，不扫描

        for i, line in enumerate(lines, start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue

            # Pattern 1: Decimal 与 float 字面量直接运算（缺少 str 包裹）
            # 匹配: Decimal(...) * 0.XX 或 0.XX * Decimal(...)
            float_literal_ops = re.findall(
                r"Decimal\([^)]*\)\s*([+\-*/])\s*(\d+\.\d+)"
                r"|"
                r"(\d+\.\d+)\s*([+\-*/])\s*Decimal\([^)]*\)",
                stripped,
            )
            for m in float_literal_ops:
                float_val = m[1] or m[2]
                op = m[0] or m[3]
                warnings.append(
                    f"第 {i} 行: Decimal 与 float 字面量 {float_val} 直接{op}运算，"
                    f'将抛出 TypeError。应使用 Decimal("{float_val}") 包裹。'
                )

            # Pattern 2: kline 字段（已知为 Decimal）与 float 字面量运算
            # 匹配: kline["close"] * 0.XX 或 0.XX * kline["close"]
            kline_float_ops = re.findall(
                r'kline\[[\'"](open|high|low|close|volume)[\'"]\]\s*([+\-*/])\s*(\d+\.\d+)'
                r"|"
                r'(\d+\.\d+)\s*([+\-*/])\s*kline\[[\'"](open|high|low|close|volume)[\'"]\]',
                stripped,
            )
            for m in kline_float_ops:
                if m[0]:  # kline["field"] op float
                    field, op, float_val = m[0], m[1], m[2]
                else:  # float op kline["field"]
                    float_val, op, field = m[3], m[4], m[5]
                warnings.append(
                    f'第 {i} 行: kline["{field}"]（Decimal 类型）与 float 字面量 {float_val} 直接{op}运算，'
                    f'将抛出 TypeError。应使用 Decimal("{float_val}") 包裹。'
                )

        return warnings

    def _scan_context_attrs(self, path: Path) -> list[str]:
        """静态扫描策略源码中使用了不存在的 StrategyContext 属性"""
        warnings: list[str] = []
        source = path.read_text(encoding="utf-8")
        lines = source.splitlines()

        # 不存在的属性 → 推荐替代方案
        INVALID_CTX_ATTRS: dict[str, str] = {
            "portfolio_value": "self.ctx.to_portfolio_context().total_capital",
            "equity": "self.ctx.to_portfolio_context().total_capital",
            "cash": "self.ctx.balance",
            "account_value": "self.ctx.to_portfolio_context().total_capital",
            "total_capital": "self.ctx.to_portfolio_context().total_capital",
        }

        for i, line in enumerate(lines, start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            for attr, replacement in INVALID_CTX_ATTRS.items():
                if f"self.ctx.{attr}" in stripped:
                    warnings.append(
                        f"第 {i} 行: 使用了不存在的属性 self.ctx.{attr}，"
                        f"将抛出 AttributeError。应改用 {replacement}。"
                    )
                    break  # 一行只能匹配一个无效属性

        return warnings

    def _load_strategy_module(self, path: Path) -> tuple[type | None, str | None]:
        """动态加载策略模块"""
        try:
            from apps.strategy_engine.base import BaseStrategy

            module_name = path.stem
            spec = importlib.util.spec_from_file_location(module_name, str(path))
            if spec is None or spec.loader is None:
                return None, "无法创建模块 spec"
            module = importlib.util.module_from_spec(spec)
            for mod_name in [
                "apps.strategy_engine.base",
                "apps.strategy_engine.indicators",
            ]:
                if mod_name not in sys.modules:
                    try:
                        __import__(mod_name)
                    except ImportError:
                        pass
            spec.loader.exec_module(module)
            for attr_name in dir(module):
                attr = getattr(module, attr_name)
                if (
                    isinstance(attr, type)
                    and attr is not BaseStrategy
                    and issubclass(attr, BaseStrategy)
                ):
                    return attr, None
            return None, "未找到继承自 BaseStrategy 的策略类"
        except Exception as e:
            return None, f"模块加载失败: {e}"

    def _validate_strategy_class(self, strategy_cls: type) -> list[str]:
        """验证策略类的必需属性和方法"""
        errors = []
        if not hasattr(strategy_cls, "name") or not getattr(strategy_cls, "name"):
            errors.append("策略类缺少 'name' 属性或为空")
        if not hasattr(strategy_cls, "on_bar"):
            errors.append("策略类缺少 'on_bar' 方法")

        # 验证 description 是否符合 4 字段模板
        from apps.strategy_engine.registry import StrategyRegistry

        try:
            StrategyRegistry.validate_description(strategy_cls)
        except ValueError as e:
            errors.append(str(e))

        # 验证 get_watch_signals 方法（实盘信号触发必需）
        if hasattr(strategy_cls, "get_watch_signals"):
            try:
                from apps.strategy_engine.base import StrategyContext
                from decimal import Decimal

                # 创建临时context验证返回值
                context = StrategyContext(
                    symbol="BTC/USDT",
                    timeframe="1h",
                    mode="paper",
                    params={},
                    balance=Decimal("10000"),
                    position=Decimal("0"),
                )
                strategy = strategy_cls(context)
                signals = strategy.get_watch_signals()

                if not isinstance(signals, list):
                    errors.append(
                        f"get_watch_signals() 返回类型错误：应为 list[dict]，实际返回 {type(signals).__name__}"
                    )
                elif len(signals) > 0:
                    # 验证每个信号的结构
                    for i, signal in enumerate(signals):
                        if not isinstance(signal, dict):
                            errors.append(
                                f"get_watch_signals()[{i}] 类型错误：应为 dict，实际为 {type(signal).__name__}"
                            )
                            continue

                        # 检查必需字段
                        required_fields = ["interval", "indicator_type", "condition"]
                        missing_fields = [f for f in required_fields if f not in signal]
                        if missing_fields:
                            errors.append(
                                f"get_watch_signals()[{i}] 缺少必需字段：{', '.join(missing_fields)}"
                            )

                        # 验证condition结构
                        if "condition" in signal:
                            cond = signal["condition"]
                            if not isinstance(cond, dict):
                                errors.append(
                                    f"get_watch_signals()[{i}].condition 应为 dict"
                                )
                            elif "operator" not in cond:
                                errors.append(
                                    f"get_watch_signals()[{i}].condition 缺少 'operator' 字段"
                                )

            except Exception as e:
                errors.append(f"get_watch_signals() 调用失败：{e}")
        # else: 不强制要求，因为BaseStrategy有默认空实现

        return errors

    def _check_registration(self, strategy_cls: type) -> str | None:
        """检查策略是否使用了 @register_strategy 装饰器"""
        from apps.strategy_engine.registry import StrategyRegistry

        name = getattr(strategy_cls, "name", "")
        if not name:
            return None
        # 如果策略路径已设置，尝试 discover
        if StrategyRegistry._strategy_path:
            StrategyRegistry.discover()
        if name not in StrategyRegistry.list_registered():
            return (
                f"策略未注册到 StrategyRegistry（缺少 @register_strategy() 装饰器）。"
                f"name='{name}'，已注册: {StrategyRegistry.list_registered()}"
            )
        return None

    def _check_signal_configuration(self, strategy_cls: type) -> str | None:
        """检查策略是否定义了有效的信号配置（用于实盘信号触发）"""
        from apps.strategy_engine.base import StrategyContext, BaseStrategy
        from decimal import Decimal

        # 检查是否覆盖了get_watch_signals方法
        if strategy_cls.get_watch_signals is BaseStrategy.get_watch_signals:
            return (
                "策略使用默认的 get_watch_signals()（返回空列表），"
                "实盘信号无法触发策略验证。"
                "如需启用信号预筛选，请覆盖该方法返回有效的信号配置。"
            )

        # 已覆盖方法，但返回空列表
        try:
            context = StrategyContext(
                symbol="BTC/USDT",
                timeframe="1h",
                mode="paper",
                params={},
                balance=Decimal("10000"),
                position=Decimal("0"),
            )
            strategy = strategy_cls(context)
            signals = strategy.get_watch_signals()

            if isinstance(signals, list) and len(signals) == 0:
                return (
                    "get_watch_signals() 已覆盖但返回空列表，"
                    "实盘信号无法触发策略验证。"
                    "请返回有效的信号配置或移除该方法覆盖（使用默认实现）。"
                )
        except Exception:
            # 调用失败已在_validate_strategy_class中报错，这里不重复
            pass

        return None

    def _run_mock_backtest(self, strategy_cls: type) -> dict[str, Any]:
        """用模拟 K 线数据通过真实 BacktestEngine 运行策略。

        复用 BacktestEngine 确保验证逻辑与真实回测完全一致（仓位计算、
        ctx.balance/position/price 状态同步、portfolio/risk 五步管线）。
        BacktestEngine.run() 为纯内存计算，不写数据库、不连交易所，因此
        验证流程不会污染任何真实数据。
        """
        from apps.strategy_engine.backtest_mode import BacktestEngine
        from apps.strategy_engine.base import StrategyContext

        warnings: list[str] = []
        schema: dict = getattr(strategy_cls, "params_schema", {})
        params = {
            key: val.get("default") if isinstance(val, dict) else val
            for key, val in schema.items()
        }
        context = StrategyContext(
            symbol="BTC/USDT",
            timeframe="1h",
            mode="backtest",
            params=params,
            balance=self.initial_capital,
            position=Decimal("0"),
        )
        try:
            strategy = strategy_cls(context)
        except Exception as e:
            return {"stats": None, "errors": [f"策略实例化失败: {e}"], "warnings": []}

        klines = _generate_mock_klines(self.kline_count)
        engine = BacktestEngine(
            strategy=strategy,
            ohlcv_data=klines,
            initial_capital=self.initial_capital,
            symbol="BTC/USDT",
            timeframe="1h",
        )

        try:
            stats = _run_coro_blocking(engine.run())
        except Exception as e:
            return {
                "stats": None,
                "errors": [self._enrich_runtime_error(e)],
                "warnings": warnings,
            }

        trades = stats.get("trades", []) or []
        buy_count = sum(1 for t in trades if t.get("trade_type") in ("open", "add"))
        sell_count = sum(1 for t in trades if t.get("trade_type") == "close")
        signals_count = buy_count + sell_count

        # 零交易检测 — 降级为 warning（随机模拟数据上零信号不代表策略有缺陷）
        if not trades:
            warnings.append(
                "零交易：在模拟 K 线上策略未产生任何成交。"
                "若策略依赖特定行情（趋势/突破），这可能是随机模拟数据所致；"
                "请确认 on_bar/generate_insights 在满足条件时返回 "
                "ctx.buy()/ctx.sell()/ctx.close_position() 的返回值（而非仅调用后返回 None）。"
            )

        stats_out = {
            "total_bars": len(klines),
            "signals_count": signals_count,
            "buy_count": buy_count,
            "sell_count": sell_count,
            "final_balance": float(context.balance),
            "final_position": float(context.position),
            "final_equity": stats.get("final_equity"),
            "total_return_pct": stats.get("total_return_pct"),
            "total_trades": stats.get("total_trades"),
        }
        return {"stats": stats_out, "errors": [], "warnings": warnings}

    @staticmethod
    def _enrich_runtime_error(e: Exception) -> str:
        """将回测运行期异常翻译为对策略作者友好的提示。"""
        err_msg = str(e)
        # 非 OrderSignal 返回值：generate_insights 会访问 signal.side → AttributeError
        # 非 OrderSignal 返回值：generate_insights 会访问 signal.side / signal.metadata
        # → AttributeError。属性名随实现变化（先读 side，后读 metadata），两者都认。
        if isinstance(e, AttributeError) and (
            "side" in err_msg or "metadata" in err_msg
        ):
            return (
                f"on_bar 返回了非 OrderSignal 类型（{err_msg}）。"
                "on_bar 必须返回 ctx.buy()/ctx.sell()/ctx.close_position() 的结果或 None。"
            )
        # Decimal 与 float/str 混用
        if isinstance(e, TypeError) and (
            "Decimal" in err_msg or "unsupported operand" in err_msg.lower()
        ):
            return (
                f"{err_msg}\n"
                "  → 可能是 Decimal 与 float/str 混用导致的类型错误。"
                "确保所有数值运算都使用 Decimal 类型（float 需用 Decimal(str(value)) 转换）。"
            )
        return f"回测运行出错: {err_msg}"
