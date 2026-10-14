"""
回归测试：ChatConsumer WebSocket 握手不应被占用的 asgiref 共享单线程卡死。

Bug 根因（现象：前端 ChatWindow 与 Supervisor 对话"不响应"）：
  asgiref.sync.sync_to_async（默认 thread_sensitive=True）的同步函数都在进程级
  共享的 ThreadPoolExecutor(max_workers=1)（SyncToAsync.single_thread_executor，
  类级单例）上逐个排队执行。FrameManager 的 K 线回调 _on_kline_data 里 run_check
  内含阻塞的 ccxt 网络调用（Binance fetch_ohlcv → load_markets → SSL 握手），
  会长时间占住这条单线程。此时 ChatConsumer.connect() 里的
  sync_to_async(_get_user)（JWT token 校验，thread-sensitive）排队挂起，
  WebSocket 握手永远无法完成 → 前端永远"连接中"、发消息无响应。

修复：
  - ChatConsumer.connect(): sync_to_async(_get_user, thread_sensitive=False)
  - FrameManager._on_kline_data(): @sync_to_async(thread_sensitive=False) + 信号量(2) 限流
  - signal_monitor.engine: 复用共享 ccxt 实例 + 5s 超时（降内存峰值，防 512M 容器 OOM）

本文件模拟"共享单线程被占住"的场景，验证带有效 token 的握手仍能在短超时内完成。
"""

import asyncio
import threading

import pytest
from asgiref.sync import sync_to_async
from channels.testing import WebsocketCommunicator
from django.contrib.auth import get_user_model
from django.test import override_settings
from rest_framework_simplejwt.tokens import AccessToken

from core.asgi import application

# 占用共享单线程的时长（s）——大于测试超时即可证明"没排队等它"
_BLOCK_SECONDS = 15
_CONNECT_TIMEOUT = 2.0


@pytest.fixture
def chat_user(request, db):
    # transaction=True（见测试标记）：数据真实提交，避免跨线程连接
    # 读不到主线程事务里未提交的用户（ChatConsumer 的 _get_user 在
    # 通用线程池线程上执行 DB 查询）。
    unique = request.node.name.replace("test_", "")[:24]
    User = get_user_model()
    return User.objects.create_user(
        username=f"chatws_{unique}",
        email=f"chatws_{unique}@test.com",
        password="TestPass123!",
    )


def _valid_token(user) -> str:
    return str(AccessToken.for_user(user))


def _make_communicator(token: str) -> WebsocketCommunicator:
    return WebsocketCommunicator(application, f"/ws/chat/?token={token}")


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_chat_connect_works_normally(chat_user):
    """基线：不占线程时，有效 token 的握手正常完成并收到 connected。"""
    with override_settings(
        CHANNEL_LAYERS={"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}
    ):
        comm = _make_communicator(_valid_token(chat_user))
        connected, _ = await asyncio.wait_for(comm.connect(), timeout=_CONNECT_TIMEOUT)
        assert connected is True
        resp = await asyncio.wait_for(
            comm.receive_json_from(), timeout=_CONNECT_TIMEOUT
        )
        assert resp == {"type": "status", "status": "connected"}
        try:
            await comm.disconnect()
        except Exception:
            pass


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_chat_connect_not_blocked_by_occupied_single_thread(chat_user):
    """回归：共享单线程被阻塞 ccxt 类调用占住时，握手仍须快速完成。

    修复前 ChatConsumer 的 token 校验走 thread-sensitive 路径，会排在
    被占用的单线程后面 → connect 超时（red）；修复后走通用线程池 → 立即完成。
    """
    release = threading.Event()

    @sync_to_async  # thread_sensitive=True：占用 SyncToAsync.single_thread_executor
    def _block_single_thread():
        release.wait(_BLOCK_SECONDS)
        return True

    blocker = asyncio.create_task(_block_single_thread())
    try:
        await asyncio.sleep(0.2)  # 确保阻塞任务先抢到共享单线程

        with override_settings(
            CHANNEL_LAYERS={
                "default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}
            }
        ):
            comm = _make_communicator(_valid_token(chat_user))
            connected, _ = await asyncio.wait_for(
                comm.connect(), timeout=_CONNECT_TIMEOUT
            )
            assert connected is True
            resp = await asyncio.wait_for(
                comm.receive_json_from(), timeout=_CONNECT_TIMEOUT
            )
            assert resp == {"type": "status", "status": "connected"}
            try:
                await comm.disconnect()
            except Exception:
                pass
    finally:
        release.set()
        try:
            await asyncio.wait_for(blocker, timeout=5)
        except asyncio.TimeoutError:
            pass
