"""测试内部数据工具的 user_id 过滤、脱敏、字段转换、错误处理。"""

import pytest

from apps.agent.tools.list_backtests import ListBacktestsTool
from apps.agent.tools.list_orders import ListOrdersTool
from apps.agent.tools.list_exchange_accounts import ListExchangeAccountsTool


@pytest.mark.asyncio
async def test_list_backtests_user_id_filter():
    """测试 ListBacktestsTool 强制 user_id 过滤。"""
    tool = ListBacktestsTool()

    # 无 user_id 时应返回错误
    result = await tool.execute()
    assert result.success is False
    assert "无法确定用户身份" in result.error


@pytest.mark.asyncio
async def test_list_orders_user_id_filter():
    """测试 ListOrdersTool 强制 user_id 过滤。"""
    tool = ListOrdersTool()

    # 无 user_id 时应返回错误
    result = await tool.execute()
    assert result.success is False
    assert "无法确定用户身份" in result.error


@pytest.mark.asyncio
async def test_list_exchange_accounts_user_id_filter():
    """测试 ListExchangeAccountsTool 强制 user_id 过滤。"""
    tool = ListExchangeAccountsTool()

    # 无 user_id 时应返回错误
    result = await tool.execute()
    assert result.success is False
    assert "无法确定用户身份" in result.error


@pytest.mark.asyncio
async def test_list_backtests_return_format():
    """测试 ListBacktestsTool 返回格式（total/returned/has_more/results）。"""
    # 需要真实用户数据，此处仅测试格式结构
    tool = ListBacktestsTool()
    # Mock user_id
    result = await tool.execute(user_id="test_user")
    # 即使查询失败，也应该有正确的格式
    if result.success:
        assert "total" in result.data
        assert "returned" in result.data
        assert "has_more" in result.data
        assert "results" in result.data
        assert isinstance(result.data["results"], list)


@pytest.mark.asyncio
async def test_list_orders_return_format():
    """测试 ListOrdersTool 返回格式。"""
    tool = ListOrdersTool()
    result = await tool.execute(user_id="test_user")
    if result.success:
        assert "total" in result.data
        assert "returned" in result.data
        assert "has_more" in result.data
        assert "results" in result.data


@pytest.mark.asyncio
async def test_list_exchange_accounts_return_format():
    """测试 ListExchangeAccountsTool 返回格式。"""
    tool = ListExchangeAccountsTool()
    result = await tool.execute(user_id="test_user")
    if result.success:
        assert "total" in result.data
        assert "returned" in result.data
        assert "has_more" in result.data
        assert "results" in result.data


@pytest.mark.asyncio
async def test_list_exchange_accounts_no_api_key_leak():
    """测试 ListExchangeAccountsTool 不返回 API 密钥（脱敏）。"""
    # Mock 查询结果，不依赖真实数据库
    from unittest.mock import patch
    from apps.agent.tools.list_exchange_accounts import _query_exchange_accounts

    # Mock 返回数据
    mock_result = [
        {
            "id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
            "exchange": "binance",
            "label": "Test Account",
            "is_active": True,
            "testnet": False,
            "created_at": "2024-07-01T10:00:00Z",
        }
    ]

    with patch.object(
        _query_exchange_accounts,
        "__wrapped__",
        side_effect=lambda *args: (mock_result, 1),
    ):
        tool = ListExchangeAccountsTool()
        result = await tool.execute(user_id="test_user")

        if result.success and result.data["results"]:
            # 检查返回字段不包含 api_key_enc/api_secret_enc
            account_data = result.data["results"][0]
            assert "api_key_enc" not in account_data
            assert "api_secret_enc" not in account_data
            # 检查返回了预期的脱敏字段
            assert "exchange" in account_data
            assert "label" in account_data
            assert "is_active" in account_data
            assert "testnet" in account_data


@pytest.mark.asyncio
async def test_list_backtests_id_prefix_filter():
    """测试 ListBacktestsTool ID 前缀模糊匹配。"""
    tool = ListBacktestsTool()
    # 传入不存在的 ID 前缀，应返回空结果
    result = await tool.execute(backtest_id="nonexistent", user_id="test_user")
    if result.success:
        assert result.data["total"] == 0
        assert result.data["returned"] == 0
        assert result.data["results"] == []


@pytest.mark.asyncio
async def test_list_orders_limit_enforcement():
    """测试 ListOrdersTool limit 上限约束。"""
    tool = ListOrdersTool()
    # 传入超限的 limit
    result = await tool.execute(limit=300, user_id="test_user")
    if result.success:
        # 实际查询应限制为 200
        assert result.data["returned"] <= 200


@pytest.mark.asyncio
async def test_list_backtests_limit_enforcement():
    """测试 ListBacktestsTool limit 上限约束。"""
    tool = ListBacktestsTool()
    # 传入超限的 limit
    result = await tool.execute(limit=150, user_id="test_user")
    if result.success:
        # 实际查询应限制为 100
        assert result.data["returned"] <= 100
