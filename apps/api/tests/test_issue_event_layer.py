"""Tests for issue② (msg_index) and issue⑥ (WorkerFailedEvent) in the event layer.

Covers:
- TokenEvent / ToolCallEvent / ToolResultEvent default msg_index == 0
- WorkerFailedEvent construction and RunEvent discriminated union resolution
- StreamTranslator increments _msg_index after each completed assistant message
- bus.publish with WorkerFailedEvent clears the worker's _thread_token_buffer entry
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# 1) Model defaults — msg_index
# ---------------------------------------------------------------------------


class TestMsgIndexDefaults:
    """TokenEvent / ToolCallEvent / ToolResultEvent default msg_index to 0."""

    def test_token_event_msg_index_default(self) -> None:
        from yukar.models.events import TokenEvent

        ev = TokenEvent(
            project_id="p1",
            epic_id="ep1",
            run_id="r1",
            thread_id="t1",
            delta="hello",
        )
        assert ev.msg_index == 0

    def test_tool_call_event_msg_index_default(self) -> None:
        from yukar.models.events import ToolCallEvent

        ev = ToolCallEvent(
            project_id="p1",
            epic_id="ep1",
            run_id="r1",
            thread_id="t1",
            tool_name="fs_write",
        )
        assert ev.msg_index == 0

    def test_tool_result_event_msg_index_default(self) -> None:
        from yukar.models.events import ToolResultEvent

        ev = ToolResultEvent(
            project_id="p1",
            epic_id="ep1",
            run_id="r1",
            thread_id="t1",
            tool_name="fs_write",
        )
        assert ev.msg_index == 0

    def test_msg_index_explicit_value(self) -> None:
        from yukar.models.events import TokenEvent

        ev = TokenEvent(
            project_id="p1",
            epic_id="ep1",
            run_id="r1",
            thread_id="t1",
            delta="x",
            msg_index=3,
        )
        assert ev.msg_index == 3


# ---------------------------------------------------------------------------
# 2) WorkerFailedEvent — construction and RunEvent union
# ---------------------------------------------------------------------------


class TestWorkerFailedEvent:
    """WorkerFailedEvent is constructable and resolves correctly from RunEvent union."""

    def test_construction_with_reason(self) -> None:
        from yukar.models.events import WorkerFailedEvent

        ev = WorkerFailedEvent(
            project_id="p1",
            epic_id="ep1",
            run_id="r1",
            worker_id="worker-abc",
            task_id="T1",
            repo="my-repo",
            reason="max_tokens",
        )
        assert ev.type == "worker_failed"
        assert ev.worker_id == "worker-abc"
        assert ev.task_id == "T1"
        assert ev.repo == "my-repo"
        assert ev.reason == "max_tokens"

    def test_construction_defaults(self) -> None:
        from yukar.models.events import WorkerFailedEvent

        ev = WorkerFailedEvent(
            project_id="p1",
            epic_id="ep1",
            run_id="r1",
            worker_id="worker-xyz",
        )
        assert ev.task_id is None
        assert ev.repo is None
        assert ev.reason == ""

    def test_run_event_discriminated_union_resolves_worker_failed(self) -> None:
        from pydantic import TypeAdapter

        from yukar.models.events import RunEvent, WorkerFailedEvent

        adapter: TypeAdapter[RunEvent] = TypeAdapter(RunEvent)
        payload = {
            "type": "worker_failed",
            "project_id": "p1",
            "epic_id": "ep1",
            "run_id": "r1",
            "worker_id": "worker-abc",
            "reason": "context_overflow",
        }
        ev = adapter.validate_python(payload)
        assert isinstance(ev, WorkerFailedEvent)
        assert ev.reason == "context_overflow"


# ---------------------------------------------------------------------------
# 3) StreamTranslator msg_index increments per assistant message
# ---------------------------------------------------------------------------


class TestStreamTranslatorMsgIndex:
    """msg_index increments after each completed assistant message turn."""

    def _make_translator(self) -> Any:
        from yukar.agents.streaming import StreamTranslator

        return StreamTranslator(
            project_id="p-idx",
            epic_id="ep-idx",
            run_id="r-idx",
            thread_id="t-idx",
        )

    def _make_assistant_message(self, tool_use_id: str = "") -> dict[str, Any]:
        """Minimal assistant message with one toolUse block (or no block)."""
        if tool_use_id:
            return {
                "role": "assistant",
                "content": [
                    {
                        "toolUse": {
                            "toolUseId": tool_use_id,
                            "name": "noop",
                            "input": {},
                        }
                    }
                ],
            }
        return {"role": "assistant", "content": []}

    def test_initial_msg_index_is_zero(self) -> None:
        translator = self._make_translator()
        assert translator._msg_index == 0

    def test_token_event_carries_current_msg_index(self) -> None:
        """TokenEvent published by callback() carries the current _msg_index."""
        translator = self._make_translator()
        published: list[Any] = []

        with patch("yukar.agents.streaming.translator.event_bus") as mock_bus:
            mock_bus.publish = MagicMock(side_effect=lambda pid, eid, ev: published.append(ev))
            translator.callback(data="hello")

        from yukar.models.events import TokenEvent

        assert len(published) == 1
        assert isinstance(published[0], TokenEvent)
        assert published[0].msg_index == 0

    def test_msg_index_increments_after_assistant_message(self) -> None:
        """After a message(role=assistant) callback, _msg_index becomes 1."""
        translator = self._make_translator()

        with patch("yukar.agents.streaming.translator.event_bus"):
            translator.callback(message=self._make_assistant_message())

        assert translator._msg_index == 1

    def test_user_message_does_not_increment_msg_index(self) -> None:
        """message(role=user) does NOT increment _msg_index."""
        translator = self._make_translator()

        with patch("yukar.agents.streaming.translator.event_bus"):
            translator.callback(message={"role": "user", "content": []})

        assert translator._msg_index == 0

    def test_full_sequence_text_then_assistant_then_text(self) -> None:
        """Sequence: text deltas → assistant message → text deltas → assistant message.

        First batch of TokenEvents must carry msg_index=0.
        Second batch (after the first assistant message) must carry msg_index=1.
        """
        translator = self._make_translator()
        published: list[Any] = []

        with patch("yukar.agents.streaming.translator.event_bus") as mock_bus:
            mock_bus.publish = MagicMock(side_effect=lambda pid, eid, ev: published.append(ev))

            # First assistant text delta (msg_index should be 0)
            translator.callback(data="first delta")

            # Assistant message completes first turn (msg_index increments to 1)
            translator.callback(
                message=self._make_assistant_message(tool_use_id="uid-1")
            )

            # Second assistant text delta (msg_index should be 1)
            translator.callback(data="second delta")

            # Assistant message completes second turn (msg_index increments to 2)
            translator.callback(
                message=self._make_assistant_message(tool_use_id="uid-2")
            )

        from yukar.models.events import TokenEvent, ToolCallEvent

        token_events = [e for e in published if isinstance(e, TokenEvent)]
        tool_call_events = [e for e in published if isinstance(e, ToolCallEvent)]

        assert len(token_events) == 2
        assert token_events[0].delta == "first delta"
        assert token_events[0].msg_index == 0

        assert token_events[1].delta == "second delta"
        assert token_events[1].msg_index == 1

        assert len(tool_call_events) == 2
        assert tool_call_events[0].msg_index == 0
        assert tool_call_events[1].msg_index == 1

        assert translator._msg_index == 2


# ---------------------------------------------------------------------------
# 4) bus.publish with WorkerFailedEvent clears the worker token buffer
# ---------------------------------------------------------------------------


class TestBusWorkerFailedBufferClear:
    """WorkerFailedEvent clears the worker's _thread_token_buffer entry."""

    def test_worker_failed_clears_token_buffer(self) -> None:
        from yukar.events import bus as event_bus
        from yukar.models.events import TokenEvent, WorkerFailedEvent

        project_id = "p-bus"
        epic_id = "ep-bus"
        worker_id = "worker-fail-test"

        # Pre-populate the token buffer by publishing a TokenEvent for the worker.
        token_ev = TokenEvent(
            project_id=project_id,
            epic_id=epic_id,
            run_id="r1",
            thread_id=worker_id,
            delta="some work",
        )
        event_bus.publish(project_id, epic_id, token_ev)

        # Verify the buffer has the worker's entry.
        buf_key = (project_id, epic_id, worker_id)
        assert buf_key in event_bus._thread_token_buffer
        assert len(event_bus._thread_token_buffer[buf_key]) == 1

        # Publish a WorkerFailedEvent — should clear the buffer.
        failed_ev = WorkerFailedEvent(
            project_id=project_id,
            epic_id=epic_id,
            run_id="r1",
            worker_id=worker_id,
            reason="max_tokens",
        )
        event_bus.publish(project_id, epic_id, failed_ev)

        # Buffer entry must be gone.
        assert buf_key not in event_bus._thread_token_buffer

    def test_worker_failed_does_not_clear_other_workers_buffer(self) -> None:
        """Only the failing worker's buffer entry is cleared, not other workers'."""
        from yukar.events import bus as event_bus
        from yukar.models.events import TokenEvent, WorkerFailedEvent

        project_id = "p-bus2"
        epic_id = "ep-bus2"
        worker_a = "worker-a"
        worker_b = "worker-b"

        # Pre-populate buffers for both workers.
        for wid in (worker_a, worker_b):
            ev = TokenEvent(
                project_id=project_id,
                epic_id=epic_id,
                run_id="r1",
                thread_id=wid,
                delta="delta",
            )
            event_bus.publish(project_id, epic_id, ev)

        # Fail worker_a.
        event_bus.publish(
            project_id,
            epic_id,
            WorkerFailedEvent(
                project_id=project_id,
                epic_id=epic_id,
                run_id="r1",
                worker_id=worker_a,
                reason="error",
            ),
        )

        # worker_a buffer gone; worker_b buffer intact.
        assert (project_id, epic_id, worker_a) not in event_bus._thread_token_buffer
        assert (project_id, epic_id, worker_b) in event_bus._thread_token_buffer

    def test_worker_failed_on_absent_key_is_safe(self) -> None:
        """Publishing WorkerFailedEvent when no buffer exists does not raise."""
        from yukar.events import bus as event_bus
        from yukar.models.events import WorkerFailedEvent

        event_bus.publish(
            "p-safe",
            "ep-safe",
            WorkerFailedEvent(
                project_id="p-safe",
                epic_id="ep-safe",
                run_id="r1",
                worker_id="worker-nonexistent",
                reason="none",
            ),
        )
        # No assertion needed — must not raise.
