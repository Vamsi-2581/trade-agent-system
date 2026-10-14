from unittest.mock import MagicMock

from apps.agent.task_tracker import tracker_context


class TestTaskTrackerSignals:
    def test_task_failure_clears_tracker_context(self):
        from apps.agent.signals import _on_task_failure

        tracker = MagicMock()
        token = tracker_context.set(tracker)
        try:
            _on_task_failure(exception=RuntimeError("boom"))
            tracker.fail.assert_called_once_with("boom")
            assert tracker_context.get() is None
        finally:
            tracker_context.reset(token)
