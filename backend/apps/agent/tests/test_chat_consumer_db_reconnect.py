"""回归测试：ChatConsumer 握手必须通过 db_async 走 close_old_connections 路径。

Bug 根因（现象：握手被反复拒 → 前端 handshake rejected repeatedly）：
  ChatConsumer.connect() 里 sync_to_async(authenticate_token) 在通用线程池
  执行 User.objects.get(...)。线程池里 DB 连接是 thread-local，长时间空闲
  后 pgbouncer / Postgres 会单方面关闭 → 握手时 "server closed the
  connection unexpectedly" → 拒绝 1006。

修复：
  - apps.core.db_utils.db_async 在目标线程先 close_old_connections()。
  - ChatConsumer.connect() 改用 db_async(authenticate_token)。

不依赖真实 DB：直接验证消费者用的同步包装路径正确。
WS 端到端测试在 test_chat_consumer_connect.py（项目原有），
但当前 dev pgbouncer 不支持 test_trade_agent 库，需在有 CREATE DB 权限的
环境运行；本文件聚焦于「握手前必须重连 DB」这条核心契约的快速回归。
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.mark.asyncio
async def test_db_async_calls_close_old_connections_before_running():
    """db_async 包装的函数在执行前必须调用 close_old_connections()。"""
    from apps.core.db_utils import db_async

    sentinel = object()

    def _fn():
        return sentinel

    wrapped = db_async(_fn)
    with patch("apps.core.db_utils.close_old_connections") as mock_close:
        result = await wrapped()

    assert result is sentinel
    assert mock_close.called, (
        "db_async 必须在执行 fn 之前调用 close_old_connections()，"
        "否则长跑线程池的 DB 连接被 pgbouncer 关闭后下一次操作必失败"
    )


@pytest.mark.asyncio
async def test_db_async_uses_non_thread_sensitive_executor():
    """db_async 必须走通用线程池，不占 asgiref 共享单线程。

    FrameManager K 线回调的阻塞 ccxt 调用会占死共享单线程，
    共享单线程上的所有 sync_to_async 都会排队挂死（包含 WS 握手）。
    db_async 固定 thread_sensitive=False 是修复 WS 握手死锁的契约。

    用 AST 静态校验 db_utils.py 源码里 @sync_to_async(…) 调用是否带
    thread_sensitive=False —— 比行为 mock 更稳，不依赖 asgiref 内部
    装饰器协议。
    """
    import ast
    import inspect
    from pathlib import Path

    src_file = Path(inspect.getfile(__import__("apps.core.db_utils", fromlist=["x"])))
    tree = ast.parse(src_file.read_text())

    found_decorator = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for dec in node.decorator_list:
            # @sync_to_async(thread_sensitive=False) → ast.Call
            if (
                isinstance(dec, ast.Call)
                and isinstance(dec.func, ast.Name)
                and dec.func.id == "sync_to_async"
            ):
                found_decorator = True
                kwargs = {kw.arg: ast.unparse(kw.value) for kw in dec.keywords}
                assert kwargs.get("thread_sensitive") == "False", (
                    "db_async 内部 @sync_to_async 必须传 thread_sensitive=False，"
                    "否则会卡在 asgiref 共享单线程上（FrameManager K 线回调"
                    "会占死那条单线程 → WS 握手挂死 → 前端连接中无响应）"
                )

    assert found_decorator, (
        "未在 apps/core/db_utils.py 里找到 @sync_to_async(…) 调用，"
        "db_async 实现可能已被改坏"
    )


@pytest.mark.asyncio
async def test_chat_connect_uses_db_async_for_token_lookup():
    """ChatConsumer.connect() 必须用 db_async(authenticate_token) 做 token 校验。

    防止有人后续 '优化' 时回退到裸 sync_to_async，把 pgbouncer 关闭连接
    的修复退回去。
    """
    from apps.notify.middleware import authenticate_token
    from apps.agent import consumers

    # 抓 call 现场
    captured = {}

    async def fake_await(value):
        return value

    def fake_db_async(fn):
        def _inner(*args, **kwargs):
            captured["fn"] = fn
            captured["args"] = args
            captured["kwargs"] = kwargs
            return fake_await((None, None))  # user=None 表示拒绝

        return _inner

    with patch("apps.core.db_utils.db_async", side_effect=fake_db_async):
        # 模拟 scope: 带一个 token 的 query string
        class _Stub:
            query_string = b"token=fake.jwt.value"
            user = None

        consumer = consumers.ChatConsumer()
        consumer.scope = {"query_string": b"token=fake.jwt.value"}

        async def _fake_close(code):
            return None

        consumer.close = _fake_close  # type: ignore[assignment]
        await consumer.connect()

    assert captured.get("fn") is authenticate_token, (
        "ChatConsumer 必须用 db_async 包装 authenticate_token，否则 "
        "通用线程池的 DB 连接被 pgbouncer 关闭后 user lookup 必失败"
    )
    assert captured.get("args") == ("fake.jwt.value",), (
        "db_async(authenticate_token) 必须以 token 字符串作为唯一位置参数"
    )
