#!/usr/bin/env python
"""
多轮对话功能演示脚本
用于验证Agent系统的多轮对话和信息路由功能
"""

import asyncio
from apps.agent.supervisor import SupervisorAgent
from apps.agent.base import AgentMessage
from apps.agent.session_manager import get_session_manager, SessionState


async def demo_multi_turn_conversation():
    """演示多轮对话功能"""
    print("=== 多轮对话功能演示 ===\n")

    # 创建supervisor实例
    supervisor = SupervisorAgent.get_instance()
    session_mgr = get_session_manager()

    # 模拟用户消息
    user_id = "demo_user_123"

    print("1. 用户发起分析请求...")
    msg1 = AgentMessage(
        sender='user',
        recipient='supervisor',
        user_id=user_id,
        payload={'text': '请帮我分析比特币当前的市场情况'},
        intent='analyze_market'
    )

    result1 = await supervisor.handle(msg1)
    print(f"Supervisor响应: {result1.data}")

    # 检查是否开启了多轮对话模式
    session_ctx = await session_mgr.get_session_context(user_id)
    print(f"会话状态: {session_ctx}\n")

    if session_ctx and session_ctx['state'] == SessionState.MULTI_TURN.value:
        print("2. 多轮对话已开启 - 用户继续提问...")
        msg2 = AgentMessage(
            sender='user',
            recipient='supervisor',
            user_id=user_id,
            payload={'text': '那以太坊呢？和比特币相比如何？'},
        )

        result2 = await supervisor.handle(msg2)
        print(f"Supervisor响应: {result2.data}")

        # 再次检查会话状态
        session_ctx = await session_mgr.get_session_context(user_id)
        print(f"会话状态: {session_ctx}\n")

        print("3. 结束多轮对话 - 用户输入结束命令...")
        msg3 = AgentMessage(
            sender='user',
            recipient='supervisor',
            user_id=user_id,
            payload={'text': '结束', 'intent': 'end_multi_turn'},
        )

        result3 = await supervisor.handle(msg3)
        print(f"Supervisor响应: {result3.data}")

        # 检查会话是否已结束
        session_ctx = await session_mgr.get_session_context(user_id)
        print(f"会话状态: {session_ctx}\n")

    print("=== 演示完成 ===")


async def demo_intent_routing():
    """演示意图识别和路由功能"""
    print("\n=== 意图识别和路由演示 ===\n")

    supervisor = SupervisorAgent.get_instance()

    test_cases = [
        ("我想生成一个交易策略", "generate_strategy"),
        ("帮我回测一个策略", "run_backtest"),
        ("分析市场趋势", "analyze_market"),
        ("评估风险", "assess_risk"),
        ("制定交易计划", "create_plan"),
        ("复盘上周交易", "review_trade"),
    ]

    for text, expected_intent in test_cases:
        print(f"输入: {text}")
        parsed_intent = await supervisor._parse_intent(text)
        print(f"解析意图: {parsed_intent}")
        print(f"预期意图: {expected_intent}")
        print(f"匹配: {'✓' if parsed_intent == expected_intent else '✗'}\n")


if __name__ == "__main__":
    print("启动Agent多轮对话功能演示...\n")
    asyncio.run(demo_multi_turn_conversation())
    asyncio.run(demo_intent_routing())