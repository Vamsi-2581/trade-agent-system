"""重现 bug：ListOrdersTool 无法通过 backtest_id 查询到对应 orders。

预期：parameters_schema 应声明 backtest_id 参数。
实际（bug）：schema 中没有 backtest_id，LLM 无法调用此功能。
"""

from apps.agent.tools.list_orders import ListOrdersTool, _query_orders


def test_list_orders_parameters_schema_has_backtest_id():
    """schema 应声明 backtest_id 参数，否则 LLM 无法调用。"""
    tool = ListOrdersTool()
    props = tool.parameters_schema["properties"]
    assert "backtest_id" in props, (
        "backtest_id 未在 parameters_schema 中声明，LLM 无法按回测 ID 查询 orders"
    )


def test_list_orders_execute_accepts_backtest_id():
    """execute() 应接受 backtest_id 参数，不应被 **kwargs 吞掉。"""
    import inspect

    tool = ListOrdersTool()
    sig = inspect.signature(tool.execute)
    params = sig.parameters

    # 检查是否有显式的 backtest_id 参数（不是 **kwargs）
    assert "backtest_id" in params, (
        "execute() 没有显式的 backtest_id 参数，传入会被 **kwargs 吞掉"
    )
    # 确保不是只有 **kwargs
    assert params["backtest_id"].kind != inspect.Parameter.VAR_KEYWORD, (
        "backtest_id 不应该是 **kwargs"
    )


def test_query_orders_source_has_backtest_filter():
    """验证 _query_orders 源码包含 backtest_id 过滤逻辑。"""
    import inspect

    source = inspect.getsource(_query_orders)
    assert "live_session__backtest_result_id" in source, (
        "_query_orders 缺少 live_session__backtest_result_id 过滤"
    )
    assert 'filters.get("backtest_id")' in source, (
        "_query_orders 缺少 backtest_id 参数检查"
    )
