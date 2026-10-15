#!/usr/bin/env python
"""
调试多轮会话路由问题的脚本
用于重现supervisor在用户回答3时没有正确路由消息的问题
"""

import asyncio
from apps.agent.supervisor import SupervisorAgent
from apps.agent.base import AgentMessage
from apps.agent.session_manager import get_session_manager, SessionState


async def test_debug_multi_turn_routing():
    """调试多轮对话路由问题"""
    print("=== 调试多轮对话路由问题 ===\n")

    # 创建supervisor实例
    supervisor = SupervisorAgent.get_instance()
    session_mgr = get_session_manager()

    # 模拟用户消息
    user_id = "debug_user_123"

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
        print("2. 多轮对话已开启 - 用户继续提问 '分析一下以太坊'...")
        msg2 = AgentMessage(
            sender='user',
            recipient='supervisor',
            user_id=user_id,
            payload={'text': '分析一下以太坊'},
        )

        result2 = await supervisor.handle(msg2)
        print(f"Supervisor响应: {result2.data}")

        # 再次检查会话状态
        session_ctx = await session_mgr.get_session_context(user_id)
        print(f"会话状态: {session_ctx}\n")

        print("3. 在多轮对话中 - 用户回答 '3' (可能的问题在于这里)...")
        msg3 = AgentMessage(
            sender='user',
            recipient='supervisor',
            user_id=user_id,
            payload={'text': '3'},
        )

        print("正在处理用户回答 '3' 的消息...")
        result3 = await supervisor.handle(msg3)
        print(f"Supervisor响应: {result3.data}")

        # 再次检查会话状态
        session_ctx = await session_mgr.get_session_context(user_id)
        print(f"会话状态: {session_ctx}\n")

        print("4. 结束多轮对话 - 用户输入结束命令...")
        msg4 = AgentMessage(
            sender='user',
            recipient='supervisor',
            user_id=user_id,
            payload={'text': '结束', 'intent': 'end_multi_turn'},
        )

        result4 = await supervisor.handle(msg4)
        print(f"Supervisor响应: {result4.data}")

        # 检查会话是否已结束
        session_ctx = await session_mgr.get_session_context(user_id)
        print(f"会话状态: {session_ctx}\n")

    print("=== 调试完成 ===")


async def analyze_problem():
    """分析路由问题的原因"""
    print("\n=== 问题分析 ===")
    print("可能的原因:")
    print("1. 当用户输入数字 '3' 时，supervisor可能试图解析意图而不是直接路由到当前活跃的agent")
    print("2. 在多轮对话模式下，数字 '3' 可能被误识别为某种意图")
    print("3. session管理器可能没有正确维护活跃agent信息")
    print("4. 子agent的响应没有正确标记是否需要继续多轮对话")

    print("\n=== 解决方案 ===")
    print("1. 确保在多轮对话模式下，消息优先路由到当前活跃的agent")
    print("2. 修改意图解析逻辑，在多轮对话期间跳过意图解析")
    print("3. 完善session管理机制")


if __name__ == "__main__":
    print("启动多轮对话路由问题调试...\n")
    asyncio.run(test_debug_multi_turn_routing())
    asyncio.run(analyze_problem())