#!/usr/bin/env python
"""
实盘信号触发诊断脚本

检查SignalMonitor到LiveStrategyRunner的完整信号传递链路。

Usage:
    python backend/scripts/debug_signal_trigger.py <live_session_id>
"""

import os
import sys
import django

# Django初始化（支持Docker容器和本地环境）
if os.path.exists("/app"):  # Docker容器
    sys.path.insert(0, "/app")
else:  # 本地环境
    backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, backend_dir)

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings.dev")
django.setup()

import json
import redis
from datetime import datetime
from apps.signal_monitor.models import SignalMonitor
from apps.trading.models import LiveSession


def check_signal_monitors(live_session_id: str) -> list:
    """检查该live_session的SignalMonitor配置"""
    monitors = SignalMonitor.objects.filter(
        live_session_id=live_session_id, status="active"
    ).select_related("user")

    print("\n=== SignalMonitor 检查 ===")
    print(f"LiveSession ID: {live_session_id}")
    print(f"活跃监控数: {monitors.count()}")

    results = []
    for m in monitors:
        monitor_info = {
            "id": str(m.id),
            "name": m.name,
            "strategy_name": m.strategy_name,
            "symbol": m.symbol,
            "interval": m.interval,
            "indicator_type": m.indicator_type,
            "condition": m.condition,
            "action_type": m.action_type,
            "user_id": str(m.user.id) if m.user else None,
            "expires_at": str(m.expires_at) if m.expires_at else "None",
        }
        results.append(monitor_info)
        print(f"\nMonitor: {m.name}")
        print(f"  ID: {m.id}")
        print(f"  Strategy: {m.strategy_name}")
        print(f"  Symbol: {m.symbol} @ {m.interval}")
        print(f"  Indicator: {m.indicator_type}")
        print(f"  Action: {m.action_type}")
        print(f"  User: {m.user}")
        print(f"  Expires: {m.expires_at}")

    return results


def check_redis_list(live_session_id: str) -> dict:
    """检查Redis List中是否有待消费的事件"""
    from django.conf import settings

    r = redis.from_url(settings.REDIS_URL, decode_responses=True)
    key = f"strategy:validate:{live_session_id}"

    print("\n=== Redis List 检查 ===")
    print(f"Key: {key}")

    # 检查List长度
    length = r.llen(key)
    print(f"待消费事件数: {length}")

    # 检查TTL
    ttl = r.ttl(key)
    print(f"TTL: {ttl}秒 (若为-2表示key不存在，-1表示无过期)")

    # 查看最新事件（不消费）
    events = r.lrange(key, 0, min(5, length - 1)) if length > 0 else []
    print("事件列表（前5个）:")
    for i, event_str in enumerate(events):
        try:
            event = json.loads(event_str)
            print(f"  [{i}] {event.get('strategy_name')} {event.get('symbol')}")
            print(f"      trigger_value: {event.get('trigger_value')}")
        except (json.JSONDecodeError, TypeError, AttributeError):
            print(f"  [{i}] {event_str[:100]}")

    r.close()

    return {
        "key": key,
        "length": length,
        "ttl": ttl,
        "events": [json.loads(e) for e in events],
    }


def check_live_session(live_session_id: str) -> dict:
    """检查LiveSession状态"""
    try:
        session = LiveSession.objects.select_related(
            "strategy", "exchange_account", "user"
        ).get(pk=live_session_id)
    except LiveSession.DoesNotExist:
        print("\n=== LiveSession 不存在 ===")
        print(f"Session ID: {live_session_id} 未找到")
        return None

    print("\n=== LiveSession 状态 ===")
    print(f"ID: {session.id}")
    print(f"Strategy: {session.strategy.name}")
    print(f"Symbol: {session.symbol}")
    print(f"Mode: {session.mode}")
    print(f"Status: {session.status}")
    print(f"User: {session.user}")
    print(f"Exchange Account: {session.exchange_account}")
    print(f"Started: {session.started_at}")
    print(f"Stopped: {session.stopped_at}")

    return {
        "id": str(session.id),
        "strategy_name": session.strategy.name,
        "symbol": session.symbol,
        "mode": session.mode,
        "status": session.status,
        "user_id": str(session.user.id),
        "exchange_account_id": str(session.exchange_account.id),
    }


def simulate_trigger_event(
    live_session_id: str, strategy_name: str, symbol: str
) -> str:
    """模拟SignalMonitor触发事件"""
    from django.conf import settings

    r = redis.from_url(settings.REDIS_URL, decode_responses=True)
    key = f"strategy:validate:{live_session_id}"

    payload = json.dumps(
        {
            "monitor_id": "test-monitor-id",
            "strategy_name": strategy_name,
            "symbol": symbol,
            "interval": "1h",
            "trigger_value": {"test": True, "timestamp": datetime.now().isoformat()},
        }
    )

    r.rpush(key, payload)
    r.expire(key, 300)  # 5分钟TTL

    print("\n=== 模拟触发事件已发布 ===")
    print(f"Key: {key}")
    print(f"Payload: {payload[:100]}")

    r.close()
    return key


def check_live_strategy_runner(live_session_id: str) -> dict:
    """检查LiveStrategyRunner是否在运行（通过进程内状态推断）"""
    from apps.agent.frame_manager import FrameManager

    fm = FrameManager.get_instance()
    status = fm.status()

    print("\n=== FrameManager 状态 ===")
    print(f"Trading: {status['trading']}")
    print(f"Assist: {status['assist']}")
    print(f"RiskGuard: {status['risk_guard']}")
    print(f"OrderExecutor: {status['order_executor']}")
    print(f"DataFeed: {status['data_feed']}")

    # 检查是否有策略运行器（进程内）
    runner_exists = fm._strategy_runner is not None
    print(f"\n策略运行器: {'存在' if runner_exists else '不存在'}")

    if runner_exists and hasattr(fm._strategy_runner, "_live_runner"):
        runner = fm._strategy_runner._live_runner
        print(f"LiveSession ID: {runner.live_session_id}")
        print(f"Running: {runner._running}")
        print(f"Validation Task: {runner._validation_task is not None}")
        print(f"User ID: {runner.user_id}")
        print(f"Symbol: {runner.symbol}")

    return status


def main():
    if len(sys.argv) < 2:
        print("Usage: python debug_signal_trigger.py <live_session_id>")
        sys.exit(1)

    live_session_id = sys.argv[1]

    print("=" * 60)
    print("实盘信号触发诊断")
    print("=" * 60)

    # 1. 检查LiveSession
    session_info = check_live_session(live_session_id)
    if not session_info:
        print("\n❌ LiveSession不存在，请检查ID是否正确")
        sys.exit(1)

    # 2. 检查SignalMonitor配置
    monitors = check_signal_monitors(live_session_id)
    if len(monitors) == 0:
        print("\n⚠️  没有活跃的SignalMonitor配置")
        print("可能原因：")
        print("  - 策略未定义 get_watch_signals()")
        print("  - LiveStrategyRunner启动时user_id缺失")
        print("  - LiveStrategyRunner._register_signal_monitors失败")
    else:
        print("\n✓ SignalMonitor配置正常")

    # 3. 检查Redis List
    redis_info = check_redis_list(live_session_id)

    # 4. 检查FrameManager状态
    fm_status = check_live_strategy_runner(live_session_id)

    # 5. 诊断总结
    print("\n" + "=" * 60)
    print("诊断总结")
    print("=" * 60)

    issues = []

    if session_info["status"] != "running":
        issues.append(f"LiveSession状态为'{session_info['status']}'（应为'running'）")

    if len(monitors) == 0:
        issues.append("没有活跃的SignalMonitor配置")

    if fm_status["trading"] != "running":
        issues.append(f"Trading Frame状态为'{fm_status['trading']}'（应为'running'）")

    if fm_status["data_feed"] != "running":
        issues.append(f"DataFeed状态为'{fm_status['data_feed']}'（数据源未连接）")

    if len(issues) == 0:
        print("\n✓ 所有关键组件状态正常")
        print("\n下一步建议：")
        print("  1. 检查策略的 get_watch_signals() 返回值")
        print("  2. 检查SignalMonitor的condition是否可能触发")
        print("  3. 手动模拟触发事件（见下方）")
        print("\n模拟触发命令：")
        print(
            f"  python backend/scripts/debug_signal_trigger.py {live_session_id} --trigger"
        )
    else:
        print("\n❌ 发现以下问题：")
        for i, issue in enumerate(issues, 1):
            print(f"  {i}. {issue}")

    # 如果指定了 --trigger 参数，模拟触发事件
    if len(sys.argv) > 2 and sys.argv[2] == "--trigger":
        simulate_trigger_event(
            live_session_id, session_info["strategy_name"], session_info["symbol"]
        )
        print("\n模拟事件已发布，等待30秒检查是否被消费...")
        import time

        time.sleep(30)
        redis_info_after = check_redis_list(live_session_id)
        if redis_info_after["length"] < redis_info["length"]:
            print("✓ 事件已被消费（LiveStrategyRunner正在监听）")
        else:
            print("❌ 事件未被消费（LiveStrategyRunner可能未启动监听）")


if __name__ == "__main__":
    main()
