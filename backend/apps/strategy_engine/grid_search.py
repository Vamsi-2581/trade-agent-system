"""
网格搜索执行器

生成参数组合、批量执行回测、追踪进度、排序结果。
"""

from __future__ import annotations

import itertools
import logging
from typing import Any

logger = logging.getLogger(__name__)


def generate_combinations(
    search_config: dict, base_params: dict | None = None
) -> list[dict]:
    """
    根据 search_config 生成参数组合（笛卡尔积）。

    Args:
        search_config: {
            "parameters": {"ma": {"min": 5, "max": 10, "step": 1}},
            "max_combinations": 100
        }
        也支持 {"ma": {"range": {"min": 5, "max": 10, "step": 1}}} 格式。
        base_params: 不参与搜索的参数固定值（如 quantity）

    Returns:
        [{param_name: value, ...}, ...] 参数组合列表
    """
    base_params = base_params or {}
    param_ranges = search_config.get("parameters", {})
    # 降级 DSML 路径可能把 max_combinations 传成字符串，转 int 再用于比较/切片
    max_combinations = int(search_config.get("max_combinations", 100))

    range_values: dict[str, list] = {}
    for param_name, range_def in param_ranges.items():
        if not isinstance(range_def, dict):
            raise ValueError(f"Parameter range for '{param_name}' must be a dict")

        # 支持两种格式：
        # 1. {"min": 5, "max": 10, "step": 1}
        # 2. {"range": {"min": 5, "max": 10, "step": 1}}
        range_info = range_def.get("range", range_def)
        min_val = range_info.get("min")
        max_val = range_info.get("max")
        step = range_info.get("step")

        if min_val is None or max_val is None or step is None:
            raise ValueError(
                f"Parameter '{param_name}' range must define min, max, and step"
            )

        if min_val >= max_val:
            raise ValueError(
                f"Parameter '{param_name}': min ({min_val}) must be < max ({max_val})"
            )
        if step <= 0:
            raise ValueError(f"Parameter '{param_name}': step must be > 0, got {step}")

        # Use integer count to avoid floating-point drift; round to avoid fp errors
        n_steps = int(round((max_val - min_val) / step))
        values = [round(min_val + i * step, 10) for i in range(n_steps + 1)]
        range_values[param_name] = values

    if not range_values:
        raise ValueError("No parameter ranges defined in search_config")

    # 笛卡尔积
    keys = list(range_values.keys())
    value_lists = [range_values[k] for k in keys]
    all_combos = []
    for combo in itertools.product(*value_lists):
        params = dict(base_params)
        for key, val in zip(keys, combo):
            params[key] = val
        all_combos.append(params)

    # 截断
    if len(all_combos) > max_combinations:
        logger.warning(
            f"Generated {len(all_combos)} combinations, truncating to {max_combinations}"
        )
        all_combos = all_combos[:max_combinations]

    return all_combos
