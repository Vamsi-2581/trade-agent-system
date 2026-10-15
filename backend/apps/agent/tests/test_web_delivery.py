"""ASGI regression: a reconnect must receive the old task's terminal result."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from asgiref.sync import sync_to_async
from channels.testing import WebsocketCommunicator
from django.test import override_settings

from apps.agent import ws_pending
from apps.agent.base import AgentResult
from apps.agent.consumers import ChatConsumer
from apps.agent.supervisor import SupervisorAgent


def _drop_cached_connections():
    """丢弃本线程 thread-local 的 DB 连接（调用方决定在哪个线程执行）。

    本文件走 WebSocket/consumer 路径，是不碰 DB 的；但 ``reply_fanout`` 内部用
    ``channels.db.database_sync_to_async``，它会先在 asgiref 线程池线程里跑
    ``close_old_connections()``。若此前有测试用过 ORM，连接会残留在该线程的
    thread-local ``connections`` 上，就会撞上 pytest-django 的 ``django_db``
    blocker（"Database access not allowed"）。

    不能用 ``connection.close()``：settings/test.py 的 sqlite 测试库是
    ``mode=memory``（Django 测试运行器把 NAME 换成 memorydb），sqlite backend
    对内存库的 ``close()`` 是 no-op（防止销毁库），关不掉。
    """
    from django.db import connections

    for alias in connections:
        conn = connections[alias]
        if conn.connection is not None:
            conn.connection = None
        conn.health_check_done = False


@pytest.fixture(autouse=True)
def _isolate_cached_connections():
    """用例前清掉主线程与线程池线程里的残留连接，保证本文件的用例不被污染。"""
    _drop_cached_connections()  # 主线程
    asyncio.run(sync_to_async(_drop_cached_connections, thread_sensitive=True)())
    yield


@pytest.fixture
def pending(monkeypatch):
    queues = {}

    async def store(uid, msg):
        queues.setdefault(uid, []).append(dict(msg))

    async def peek(uid):
        return list(queues.get(uid, []))

    async def ack(uid, msg):
        if msg in queues.get(uid, []):
            queues[uid].remove(msg)

    async def ack_by_id(uid, did):
        for i, m in enumerate(queues.get(uid, [])):
            if m.get("delivery_id") == did:
                del queues[uid][i]
                return

    async def drain(uid):
        return queues.pop(uid, [])

    monkeypatch.setattr(ws_pending, "store", store)
    monkeypatch.setattr(ws_pending, "drain", drain)
    monkeypatch.setattr(ws_pending, "peek", peek, raising=False)
    monkeypatch.setattr(ws_pending, "ack", ack, raising=False)
    monkeypatch.setattr(ws_pending, "ack_by_id", ack_by_id, raising=False)
    monkeypatch.setattr(
        "apps.notify.middleware.authenticate_token",
        lambda token: (SimpleNamespace(id=token), None),
    )
    monkeypatch.setattr("apps.agent.reply_fanout.push_main_channel", AsyncMock())
    return queues


async def connect(uid):
    comm = WebsocketCommunicator(ChatConsumer.as_asgi(), "/?token=" + uid)
    assert (await comm.connect())[0]
    assert (await comm.receive_json_from())["status"] == "connected"
    return comm


@pytest.mark.asyncio
async def test_old_task_failure_reaches_reconnected_socket(pending, monkeypatch):
    started, finish = asyncio.Event(), asyncio.Event()

    async def handle(*args, **kwargs):
        started.set()
        await finish.wait()
        return AgentResult(success=False, error="controlled workflow failure")

    monkeypatch.setattr(
        SupervisorAgent, "get_instance", lambda: SimpleNamespace(handle=handle)
    )
    with override_settings(
        CHANNEL_LAYERS={"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}
    ):
        old = await connect("user-a")
        await old.send_json_to({"type": "chat", "text": "controlled task"})
        assert (await old.receive_json_from())["status"] == "processing"
        await started.wait()
        # The disconnect event must be processed without cancelling the workflow.
        await old.disconnect(timeout=0.3)
        new = await connect("user-a")
        other = await connect("user-b")
        try:
            finish.set()
            result = await new.receive_json_from(timeout=1)
            assert result["type"] == "chat_response"
            assert result["status"] == "error"
            assert result["error"] == "controlled workflow failure"
            assert await other.receive_nothing(timeout=0.05)
        finally:
            await new.disconnect()
            await other.disconnect()


@pytest.mark.asyncio
async def test_slow_chat_accepts_ping_and_notification(pending, monkeypatch):
    finish = asyncio.Event()

    async def handle(*args, **kwargs):
        await finish.wait()
        return AgentResult(success=True, data={"content": "done"})

    monkeypatch.setattr(
        SupervisorAgent, "get_instance", lambda: SimpleNamespace(handle=handle)
    )
    with override_settings(
        CHANNEL_LAYERS={"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}
    ):
        comm = await connect("user-a")
        try:
            await comm.send_json_to({"type": "chat", "text": "slow"})
            await comm.receive_json_from()
            await comm.send_json_to({"type": "ping"})
            assert (await comm.receive_json_from(timeout=0.3))["type"] == "pong"
            from apps.agent.reply_fanout import push_web

            await push_web("user-a", "background task done")
            assert (await comm.receive_json_from(timeout=0.5))[
                "data"
            ] == "background task done"
        finally:
            finish.set()
            await comm.disconnect()


@pytest.mark.asyncio
async def test_offline_replay_then_reconnect_does_not_repeat(pending):
    from apps.agent.reply_fanout import push_web_payload

    with override_settings(
        CHANNEL_LAYERS={"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}
    ):
        await push_web_payload(
            "user-a",
            {"type": "chat_response", "status": "done", "data": "offline result"},
        )
        assert len(pending["user-a"]) == 1
        first = await connect("user-a")
        try:
            msg = await first.receive_json_from()
            assert msg["data"] == "offline result"
            # 浏览器回送达确认 → 服务端才删缓冲（新协议）
            await first.send_json_to(
                {"type": "delivery_ack", "delivery_id": msg["delivery_id"]}
            )
            await asyncio.sleep(0.1)
        finally:
            await first.disconnect()
        assert not pending["user-a"]
        second = await connect("user-a")
        try:
            assert await second.receive_nothing(timeout=0.05)
        finally:
            await second.disconnect()


@pytest.mark.asyncio
async def test_unacked_delivery_replays_on_reconnect(pending):
    """半死连接/页面冻结场景：消息已发出但浏览器未确认 → 重连必须补发。"""
    from apps.agent.reply_fanout import push_web_payload

    with override_settings(
        CHANNEL_LAYERS={"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}
    ):
        await push_web_payload(
            "user-a", {"type": "chat_response", "status": "done", "data": "lost report"}
        )
        first = await connect("user-a")
        try:
            assert (await first.receive_json_from())["data"] == "lost report"
        finally:
            await first.disconnect()
        # 未确认即断开：缓冲条目必须保留
        assert len(pending["user-a"]) == 1
        second = await connect("user-a")
        try:
            msg = await second.receive_json_from(timeout=1)
            assert msg["data"] == "lost report"
            await second.send_json_to(
                {"type": "delivery_ack", "delivery_id": msg["delivery_id"]}
            )
            await asyncio.sleep(0.1)
        finally:
            await second.disconnect()
        assert not pending["user-a"]


@pytest.mark.asyncio
async def test_two_online_connections_each_receive_once(pending):
    from apps.agent.reply_fanout import push_web_payload

    with override_settings(
        CHANNEL_LAYERS={"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}
    ):
        first, second = await connect("user-a"), await connect("user-a")
        try:
            await push_web_payload(
                "user-a", {"type": "task_notification", "data": "both tabs"}
            )
            one, two = await first.receive_json_from(), await second.receive_json_from()
            assert one["delivery_id"] == two["delivery_id"]
            assert one["data"] == two["data"] == "both tabs"
            assert await first.receive_nothing(timeout=0.05)
            assert await second.receive_nothing(timeout=0.05)
        finally:
            await first.disconnect()
            await second.disconnect()


@pytest.mark.asyncio
async def test_main_channel_failure_does_not_prevent_web(pending, monkeypatch):
    from apps.agent.reply_fanout import fan_out_reply

    monkeypatch.setattr(
        "apps.agent.reply_fanout.push_main_channel",
        AsyncMock(side_effect=RuntimeError("main unavailable")),
    )
    with override_settings(
        CHANNEL_LAYERS={"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}
    ):
        comm = await connect("user-a")
        try:
            with pytest.raises(RuntimeError, match="main unavailable"):
                await fan_out_reply("user-a", "must reach web", origin="api")
            assert (await comm.receive_json_from())["data"] == "must reach web"
        finally:
            await comm.disconnect()
