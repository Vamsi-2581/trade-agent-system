import json
from unittest.mock import AsyncMock, patch

import pytest


class TestDeadLetterConsumer:
    """Tests for DeadLetterConsumer — handles DLQ messages without auto-retry."""

    @pytest.mark.asyncio
    async def test_handle_dlq_message_notifies_and_acks(self):
        """_handle_dlq_message → notification sent, ACKed."""
        from apps.agent.dlq_consumer import DeadLetterConsumer

        consumer = DeadLetterConsumer()

        mock_r = AsyncMock()
        mock_r.xack = AsyncMock(return_value=1)

        with (
            patch.object(consumer, "_get_redis", return_value=mock_r),
            patch("apps.agent.dlq_consumer._async_send_notification") as mock_notify,
        ):
            await consumer._handle_dlq_message(
                "msg-001",
                {
                    "task_id": json.dumps("task-dlq-1"),
                    "user_id": json.dumps("user-dlq"),
                    "dlq_reason": json.dumps("exceeded 3 retries"),
                    "retry_count": json.dumps("3"),
                },
            )

        mock_r.xack.assert_called_once_with("agent:tasks:dlq", "agents-dlq", "msg-001")
        mock_notify.assert_called_once()

    @pytest.mark.asyncio
    async def test_empty_dlq_no_action(self):
        """Empty XREADGROUP → continues silently."""
        from apps.agent.dlq_consumer import DeadLetterConsumer

        consumer = DeadLetterConsumer()

        mock_r = AsyncMock()
        # one iteration then stop
        call_count = [0]

        async def xreadgroup_once(**kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return []
            consumer._running = False
            return []

        mock_r.xreadgroup = xreadgroup_once

        with (
            patch.object(consumer, "_get_redis", return_value=mock_r),
            patch("apps.agent.dlq_consumer._async_send_notification") as mock_notify,
        ):
            await consumer._poll_loop()

        mock_notify.assert_not_called()

    @pytest.mark.asyncio
    async def test_start_and_stop(self):
        from apps.agent.dlq_consumer import DeadLetterConsumer

        consumer = DeadLetterConsumer()

        mock_r = AsyncMock()
        mock_r.xgroup_create = AsyncMock(return_value=True)
        mock_r.xreadgroup = AsyncMock(return_value=[])

        with patch.object(consumer, "_get_redis", return_value=mock_r):
            await consumer.start()
            assert consumer._running is True
            assert consumer._poll_task is not None

            await consumer.stop()
            assert consumer._running is False
