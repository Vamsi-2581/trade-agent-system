from __future__ import annotations

import logging
from pathlib import Path

from .base import BaseTool, ToolResult
from .file_io import WORKSPACE_ROOT

logger = logging.getLogger(__name__)


class TestStrategyTool(BaseTool):
    name = "test_strategy"
    description = (
        "测试策略代码是否能正确加载和运行。验证 Python 语法、策略类结构、"
        "并用模拟 K 线数据运行回测，返回统计信息。"
    )

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "strategy_name": {
                    "type": "string",
                    "description": "策略名称（可选，用于显示）",
                },
                "strategy_file_path": {
                    "type": "string",
                    "description": "策略文件的绝对路径或相对于 ~/.tradelogx 的路径",
                },
            },
            "required": ["strategy_file_path"],
        }

    async def execute(
        self, strategy_file_path: str = "", strategy_name: str = "", **kwargs
    ) -> ToolResult:
        if not strategy_file_path:
            return ToolResult(success=False, error="缺少 strategy_file_path 参数")

        path = Path(strategy_file_path)
        if not path.is_absolute():
            path = WORKSPACE_ROOT / strategy_file_path

        logger.info("testing strategy: %s", path)

        from apps.strategy_engine.test_runner import StrategyTester

        tester = StrategyTester()
        result = tester.test_strategy_file(str(path))

        if result["success"]:
            stats = result.get("stats") or {}
            warnings = result.get("warnings") or []
            parts = [
                f"✅ 策略测试通过: {result.get('strategy_name', 'unnamed')}",
                f"- 总K线: {stats.get('total_bars', '?')}",
                f"- 信号数: {stats.get('signals_count', '?')} (买{stats.get('buy_count', '?')}/卖{stats.get('sell_count', '?')})",
                f"- 最终权益: {stats.get('final_equity', '?')}",
                f"- 收益率: {stats.get('total_return_pct', '?')}%",
            ]
            if warnings:
                parts.append("\n⚠️ 警告:\n" + "\n".join(f"  - {w}" for w in warnings))
            return ToolResult(success=True, data="\n".join(parts))

        errors = result.get("errors") or []
        warnings = result.get("warnings") or []
        parts = [f"❌ 策略测试失败: {result.get('strategy_name', 'unnamed')}"]
        for err in errors:
            parts.append(f"  - {err}")
        if warnings:
            parts.append("\n⚠️ 警告:\n" + "\n".join(f"  - {w}" for w in warnings))
        return ToolResult(success=False, error="\n".join(parts))
