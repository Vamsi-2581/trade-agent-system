"""Agent 主动通知用户的工具。"""

from __future__ import annotations

import logging

from .base import BaseTool, ToolResult

logger = logging.getLogger(__name__)

#: 通知正文上限。与渠道侧同一口径：Telegram API 限制 4096 字符，
#: `apps/channel/telegram.py::_TG_MAX_LENGTH` 取 4000 留余量给格式化。
#: 超过这个长度必须在**入库前**截断——`Notification.message` 是无上限的 TextField，
#: 不截断就等于把一整篇 LLM 报告塞进通知中心（历史上确实会到数万字符）。
_MAX_MESSAGE_LENGTH = 4000

#: 截断标记。持久化出去的就是「正文 + 这个后缀」，所以最终长度是
#: ``_MAX_MESSAGE_LENGTH + len(_TRUNCATION_MARKER)`` = 4000 + 8 = 4008。
_TRUNCATION_MARKER = "…（已截断）"


class NotificationNotPersistedError(RuntimeError):
    """通知没有真正落地（未入库）。

    独立成异常类型而不是复用 OperationalError：它**不是**瞬时故障，
    重试没有意义（同一个 user_id 仍然解析不到），外层也不该把它当成
    「丢连接后重试」来处理。
    """


class NotificationNotDeliveredError(RuntimeError):
    """通知已入库，但实时推送（Redis pubsub）失败。

    入库成功只保证通知中心可查；用户能否立刻看到取决于推送。两者必须
    分开报：只报「已发送」会让 Agent 以为用户收到了。
    """


def _truncate(message: str) -> str:
    """把超长通知正文截到 ``_MAX_MESSAGE_LENGTH`` 并加截断标记。

    只截一次、只加一个标记：对已经截断过的文本再调一次是幂等的
    （因为它必然已经 <= 上限）。
    """
    if len(message) <= _MAX_MESSAGE_LENGTH:
        return message
    return message[:_MAX_MESSAGE_LENGTH] + _TRUNCATION_MARKER


class NotifyUserTool(BaseTool):
    """
    Agent 主动通知用户的工具。

    当任务完成、发现重要信号、或需要用户关注时，Agent 调用此工具
    主动推送消息给用户。user_id 从执行上下文自动注入。
    """

    name = "notify_user"
    description = (
        "主动通知用户。当任务完成、发现重要信息或需要用户关注时使用。"
        "通知内容应简洁明了，包含关键信息。"
        "不要用它做普通对话回复——那些放在正常回复中即可。"
    )

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "通知内容，简洁明了",
                },
            },
            "required": ["message"],
        }

    async def execute(
        self,
        message: str = "",
        _user_id: str = "",
        **kwargs,
    ) -> ToolResult:
        # user_id 由 base.py 自动注入，兼容旧签名
        user_id = kwargs.get("user_id", _user_id)

        if not message or not message.strip():
            return ToolResult(success=False, error="message 不能为空")

        if not user_id:
            return ToolResult(success=False, error="无法确定通知目标用户")

        # 入库与推送用同一份文本：两处都按渠道上限截断，避免「通知中心里是全的、
        # 推过去的是断的」这种两边对不上的状态。
        message = _truncate(message)

        # 瞬时故障重试（2026-09-18 事故）：celery worker 的 DB 连接在
        # LLM 长生成期间被 PG idle_in_transaction_session_timeout=30s
        # 杀死 → "server closed the connection unexpectedly" → 报告未投递，
        # LLM 回退自行输出并虚构指标。瞬时故障丢弃疑似坏连接后重试一次。
        #
        # 两个阶段分开重试（2026-10-19 排查）：
        # * 入库：只有瞬时 DB 故障才重试；「用户解析不到」是确定性错误，
        #   重试同一个 user_id 必然再失败，且重试会把失败伪装成「重试成功」。
        # * 推送：入库成功后推送失败时**只重试推送**——若把整段重来一遍，
        #   会再写一条 Notification，通知中心出现重复记录。
        last_error: Exception | None = None
        persisted = False
        pushed = False
        for attempt in (1, 2):
            try:
                if not persisted:
                    # 持久化到 Notification 模型
                    await self._persist_notification(user_id, message)
                    persisted = True
                if not pushed:
                    # 通过 Redis pubsub 推送（ASGI consumer 路由到活跃渠道）
                    self._send_via_redis(user_id, message)
                    pushed = True
                last_error = None
                break
            except NotificationNotPersistedError as e:
                # 确定性失败：不重试、不 close 连接（连接本身没问题）
                last_error = e
                logger.error("[NotifyUserTool] notification not persisted: %s", e)
                break
            except Exception as e:  # noqa: BLE001
                last_error = e
                logger.warning(
                    "[NotifyUserTool] attempt %d/2 failed: %s",
                    attempt,
                    e,
                    exc_info=True,
                )
                if attempt == 2:
                    break
                # 丢弃疑似坏掉的 DB 连接，重试使用新连接
                try:
                    from django.db import connections

                    connections.close_all()
                except Exception:  # noqa: BLE001
                    logger.debug("[NotifyUserTool] close_all failed", exc_info=True)

        if last_error is not None:
            return ToolResult(success=False, error=f"发送通知失败: {last_error}")

        logger.info(
            "[NotifyUserTool] notification sent to user %s: %s",
            user_id,
            message[:100],
        )
        if attempt == 2:
            return ToolResult(
                success=True,
                data={"message": "通知已发送（重试成功）"},
            )
        return ToolResult(
            success=True,
            data={"message": "通知已发送"},
        )

    @staticmethod
    async def _persist_notification(user_id: str, message: str) -> None:
        """写入 Notification 模型，留存历史记录。

        瞬时连接故障自愈（2026-09-18 事故，第二次修正）：
        Django 的 ``connections`` 是**线程局部**的，而 ORM 在 ``sync_to_async``
        的线程池里执行。连接可能在 LLM 长生成期间被 PG 的
        ``idle_in_transaction_session_timeout``（叠加 pgbouncer 中转）断开。
        重试必须在**同步线程内** ``connection.close()`` 后重放，才能拿到真正
        的新连接——在事件循环线程里 ``connections.close_all()`` 打不到这个
        线程的连接，重试只会再打旧连接（实测 ``connection already closed``）。
        """
        from asgiref.sync import sync_to_async
        from django.contrib.auth import get_user_model

        from apps.notify.models import Notification

        def _create():
            User = get_user_model()
            user = None
            try:
                import uuid

                user = User.objects.get(id=uuid.UUID(str(user_id)))
            except (ValueError, User.DoesNotExist):
                # 渠道侧 id（open_id / telegram_id / username）也要能落到正确的用户，
                # 否则飞书发起的会话通知不会出现在 web 通知中心。
                user = (
                    User.objects.filter(username=user_id).first()
                    or User.objects.filter(feishu_open_id=user_id).first()
                    or User.objects.filter(telegram_id=user_id).first()
                )
            if not user:
                # 静默跳过 = 谎报成功（2026-10-19 排查）：既不写库也不推送，
                # 但 `_persist_notification` 正常返回 → 外层认为已送达，
                # Agent 拿到 success=True 后继续宣称「报告已发送」。
                # 通知没落地就必须是失败，让调用方知道用户什么都没收到。
                raise NotificationNotPersistedError(
                    f"无法解析通知目标用户: {user_id!r}（已尝试 id/username/"
                    "feishu_open_id/telegram_id），通知未入库、未推送"
                )
            Notification.objects.create(
                user=user,
                channel="web",
                message=message,
            )

        @sync_to_async
        def _create_with_stale_connection_retry():
            from django.db import InterfaceError, OperationalError, connection

            try:
                _create()
            except (OperationalError, InterfaceError):
                logger.warning(
                    "[NotifyUserTool] stale DB connection in sync thread; "
                    "closing and retrying once",
                    exc_info=True,
                )
                # 必须在同一线程内 close：下一次 ORM 调用才会重新建连
                connection.close()
                _create()

        await _create_with_stale_connection_retry()

    @staticmethod
    def _send_via_redis(user_id: str, text: str) -> None:
        """通过 Redis pubsub 发布通知，由 ASGI consumer 消费并路由到用户活跃渠道。

        失败必须**抛出**（2026-10-19 排查）：这里原来是
        ``except Exception: logger.warning(...)``，异常被吞掉、函数正常返回，
        于是 `execute` 认为推送成功并报「已发送」——而用户实际什么都没收到。
        写库只让通知中心可查，推送才是实时送达；两者不能混为一谈。
        """
        import json

        import redis
        from django.conf import settings
        from django.utils import timezone

        from apps.agent.task_tracker import _PROGRESS_CHANNEL

        url = settings.REDIS_URL
        if url.rsplit("/", 1)[-1].isdigit():
            url = url.rsplit("/", 1)[0] + "/3"

        try:
            r = redis.from_url(url, decode_responses=True)
            r.publish(
                _PROGRESS_CHANNEL,
                json.dumps(
                    {
                        "user_id": user_id,
                        "text": text,
                        "at": timezone.now().isoformat(),
                    }
                ),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "[NotifyUserTool] Redis pubsub delivery failed", exc_info=True
            )
            raise NotificationNotDeliveredError(
                f"通知已入库，但 Redis 实时推送失败: {e}"
            ) from e
