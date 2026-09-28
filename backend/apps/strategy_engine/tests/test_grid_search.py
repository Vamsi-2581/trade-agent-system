"""Tests for GridSearchRunner: combination generation and parameter merging."""

import pytest
from apps.strategy_engine.grid_search import generate_combinations


class TestGenerateCombinations:
    def test_single_param_range(self):
        """单参数范围应生成 N 个组合"""
        config = {"parameters": {"ma": {"min": 5, "max": 9, "step": 2}}}
        combos = generate_combinations(config, base_params={})
        assert len(combos) == 3  # 5, 7, 9
        assert combos[0] == {"ma": 5}
        assert combos[1] == {"ma": 7}
        assert combos[2] == {"ma": 9}

    def test_two_param_ranges(self):
        """两参数范围应生成笛卡尔积"""
        config = {
            "parameters": {
                "ma_fast": {"min": 5, "max": 7, "step": 2},
                "ma_slow": {"min": 20, "max": 30, "step": 10},
            }
        }
        combos = generate_combinations(config, base_params={})
        # ma_fast: 5,7 (2 values) × ma_slow: 20,30 (2 values) = 4
        assert len(combos) == 4

    def test_base_params_preserved(self):
        """无 range 的参数应使用 base_params 固定值"""
        config = {"parameters": {"ma": {"min": 5, "max": 7, "step": 2}}}
        base = {"quantity": 0.01}
        combos = generate_combinations(config, base_params=base)
        for c in combos:
            assert c["quantity"] == 0.01

    def test_max_combinations_limit(self):
        """超过 max_combinations 应截断"""
        config = {
            "parameters": {
                "a": {"min": 1, "max": 20, "step": 1},
                "b": {"min": 1, "max": 20, "step": 1},
            },
            "max_combinations": 50,
        }
        combos = generate_combinations(config, base_params={})
        assert len(combos) == 50

    def test_max_combinations_string_limit(self):
        """字符串 max_combinations（降级 DSML 路径形态）也应正确截断，而非抛 TypeError"""
        config = {
            "parameters": {
                "a": {"min": 1, "max": 20, "step": 1},
                "b": {"min": 1, "max": 20, "step": 1},
            },
            "max_combinations": "50",
        }
        combos = generate_combinations(config, base_params={})
        assert len(combos) == 50

    def test_invalid_range_raises(self):
        """min >= max 应抛出异常"""
        config = {"parameters": {"ma": {"min": 10, "max": 5, "step": 1}}}
        with pytest.raises(ValueError):
            generate_combinations(config, base_params={})

    def test_step_zero_raises(self):
        """step <= 0 应抛出异常"""
        config = {"parameters": {"ma": {"min": 1, "max": 10, "step": 0}}}
        with pytest.raises(ValueError):
            generate_combinations(config, base_params={})

    def test_range_wrapper_format(self):
        """支持 {"range": {"min": ..., "max": ..., "step": ...}} 格式"""
        config = {"parameters": {"ma": {"range": {"min": 5, "max": 9, "step": 2}}}}
        combos = generate_combinations(config, base_params={})
        assert len(combos) == 3
        assert combos[0] == {"ma": 5}

    def test_missing_range_fields_raises(self):
        """缺少 min/max/step 应抛出异常"""
        config = {"parameters": {"ma": {"min": 5}}}
        with pytest.raises(ValueError):
            generate_combinations(config, base_params={})

    def test_float_step_values(self):
        """浮点数步长应正确生成（避免累积误差）"""
        config = {"parameters": {"stop_loss": {"min": 0.0, "max": 0.3, "step": 0.1}}}
        combos = generate_combinations(config, base_params={})
        assert len(combos) == 4
        assert combos[0] == {"stop_loss": 0.0}
        assert combos[3] == {"stop_loss": 0.3}

    def test_negative_step_raises(self):
        """负数 step 应抛出异常"""
        config = {"parameters": {"ma": {"min": 1, "max": 10, "step": -1}}}
        with pytest.raises(ValueError):
            generate_combinations(config, base_params={})
