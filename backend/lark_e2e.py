"""飞书端到端 v2：起 AgentTaskConsumer + publish 真实消息 + 监听 reply"""
import os
import django
import asyncio
import time
import logging
os.environ['DJANGO_SETTINGS_MODULE'] = 'core.settings.dev'
django.setup()

logging.getLogger('apps.strategy_engine.registry').setLevel(logging.WARNING)
logging.getLogger('apps').setLevel(logging.WARNING)

from apps.agent.bus import publish, build_agent_task, wait_reply, AGENT_TASKS
from apps.agent.consumer import AgentTaskConsumer
from django.conf import settings

app_id = os.environ.get("LARK_APP_ID", "") or getattr(settings, "LARK_APP_ID", "")
app_secret = os.environ.get("LARK_APP_SECRET", "") or getattr(settings, "LARK_APP_SECRET", "")

print("=== 飞书真实端到端 v2 ===")
print(f"LARK_APP_ID={app_id[:12]}...")
print("启动 AgentTaskConsumer...")

consumer = AgentTaskConsumer(concurrency=1)

USER_TEXT = "帮我回测 4h 箱体突破策略：4h 收盘价突破 7 日箱体上边价、成交量 ≥ 3 × 20 日均量、18 根 4h 内回踩箱体上边价进场、止损箱体下沿、止盈 2 倍止损"

async def main():
    await consumer.start()
    print("Consumer started. 等待业务跑完（预计 1-3 分钟）...\n")
    t0 = time.monotonic()

    msg = build_agent_task(
        user_id="ou_e2e_real_v2_001",
        payload={"text": USER_TEXT},
    )
    await publish(AGENT_TASKS, msg)
    print(f">>> 已发布到 agent:tasks，task_id={msg['task_id']}")

    reply = await wait_reply(msg["task_id"], timeout=600)
    elapsed = time.monotonic() - t0
    await consumer.stop()

    print(f"\n=== 完成（耗时 {elapsed:.1f}s）===")
    if reply:
        print("<<< 收到 reply（前 2000 字符）:")
        print(str(reply)[:2000])
    else:
        print("<<< 超时未收到 reply")

asyncio.run(main())
