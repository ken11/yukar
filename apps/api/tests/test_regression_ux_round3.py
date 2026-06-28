"""Regression tests for UX-fix round-3 backend fixes.

Covers:
- Manager backfill buffer cleared per-turn: after ManagerMessageEvent the
  manager token ring-buffer must be empty so mid-run reloads do not receive
  concatenated narration from previous turns.
- Mn3 endpoint-level: the real thread_stream endpoint (not an inline loop
  reimplementation) must deliver a backfill event exactly once even when the
  event was published after subscribe registration but before the snapshot.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _base(project_id: str = "p", epic_id: str = "e", run_id: str = "r") -> dict[str, Any]:
    return {
        "project_id": project_id,
        "epic_id": epic_id,
        "run_id": run_id,
        "ts": datetime.now(UTC).isoformat(),
    }


def _token_event(thread_id: str, delta: str = "x", **kw: Any) -> Any:
    from yukar.models.events import TokenEvent

    return TokenEvent(**_base(**kw), thread_id=thread_id, delta=delta)


def _manager_message_event(turn: int = 0, text: str = "hello", **kw: Any) -> Any:
    from yukar.models.events import ManagerMessageEvent

    return ManagerMessageEvent(**_base(**kw), thread_id="manager", turn=turn, text=text)


# ---------------------------------------------------------------------------
# Manager backfill buffer cleared per-turn
# ---------------------------------------------------------------------------


class TestManagerBufferClearedOnManagerMessage:
    """After each ManagerMessageEvent, the manager token ring-buffer must be
    empty so that a mid-run reload does not receive concatenated narration
    from previous turns.

    The canonical text is already persisted in the Strands session store;
    late joiners should call list_messages rather than relying on backfill.
    """

    def setup_method(self) -> None:
        from yukar.events import bus as event_bus

        event_bus._thread_token_buffer.clear()

    def test_manager_buffer_cleared_after_manager_message(self) -> None:
        """Publish multiple manager TokenEvents then ManagerMessageEvent.
        The buffer must be empty afterwards."""
        from yukar.events import bus as event_bus

        # Turn 0: manager emits several token deltas.
        for delta in ("Hello", " ", "World"):
            ev = _token_event("manager", delta=delta)
            event_bus.publish("p", "e", ev)

        assert len(event_bus.get_thread_token_backfill("p", "e", "manager")) == 3

        # Turn completes with ManagerMessageEvent.
        msg = _manager_message_event(turn=0, text="Hello World")
        event_bus.publish("p", "e", msg)

        result = event_bus.get_thread_token_backfill("p", "e", "manager")
        assert result == [], (
            f"Manager buffer must be empty after ManagerMessageEvent, got: {result}"
        )

    def test_manager_buffer_accumulates_for_next_turn(self) -> None:
        """After ManagerMessageEvent clears the buffer, new tokens for the
        next turn accumulate correctly."""
        from yukar.events import bus as event_bus

        # Turn 0.
        ev0 = _token_event("manager", delta="turn0")
        event_bus.publish("p", "e", ev0)
        event_bus.publish("p", "e", _manager_message_event(turn=0, text="turn0"))

        # Buffer must be clear between turns.
        assert event_bus.get_thread_token_backfill("p", "e", "manager") == []

        # Turn 1: new tokens.
        ev1 = _token_event("manager", delta="turn1")
        event_bus.publish("p", "e", ev1)

        result = event_bus.get_thread_token_backfill("p", "e", "manager")
        assert len(result) == 1
        assert result[0].delta == "turn1"

    def test_manager_buffer_cleared_multiple_turns(self) -> None:
        """Three manager turns: each ManagerMessageEvent must clear the buffer.
        After all three turns the backfill contains only tokens from turn 2."""
        from yukar.events import bus as event_bus

        for turn in range(3):
            for i in range(5):
                ev = _token_event("manager", delta=f"t{turn}d{i}")
                event_bus.publish("p", "e", ev)
            event_bus.publish("p", "e", _manager_message_event(turn=turn, text=f"turn{turn}"))

            # Immediately after each ManagerMessageEvent the buffer is empty.
            assert event_bus.get_thread_token_backfill("p", "e", "manager") == [], (
                f"Buffer must be empty after turn {turn} ManagerMessageEvent"
            )

    def test_worker_buffer_not_affected_by_manager_message(self) -> None:
        """ManagerMessageEvent must not clear other threads' buffers."""
        from yukar.events import bus as event_bus

        worker_ev = _token_event("worker-1", delta="worker token")
        event_bus.publish("p", "e", worker_ev)

        manager_ev = _token_event("manager", delta="mgr token")
        event_bus.publish("p", "e", manager_ev)

        # Sanity: both buffers non-empty.
        assert len(event_bus.get_thread_token_backfill("p", "e", "worker-1")) == 1
        assert len(event_bus.get_thread_token_backfill("p", "e", "manager")) == 1

        # ManagerMessageEvent fires.
        event_bus.publish("p", "e", _manager_message_event(turn=0))

        # Manager buffer cleared; worker buffer intact.
        assert event_bus.get_thread_token_backfill("p", "e", "manager") == []
        assert len(event_bus.get_thread_token_backfill("p", "e", "worker-1")) == 1, (
            "Worker buffer must not be cleared by ManagerMessageEvent"
        )

    def test_empty_buffer_clear_is_idempotent(self) -> None:
        """ManagerMessageEvent on an empty manager buffer must not raise."""
        from yukar.events import bus as event_bus

        # No tokens published before the message.
        event_bus.publish("p", "e", _manager_message_event(turn=0, text="empty turn"))
        assert event_bus.get_thread_token_backfill("p", "e", "manager") == []


# ---------------------------------------------------------------------------
# Mn3: endpoint-level test — real thread_stream route
# ---------------------------------------------------------------------------


class TestThreadStreamEndpointMn3:
    """Drive the actual thread_stream endpoint via FastAPI TestClient / httpx
    AsyncClient and verify that a backfill event is delivered exactly once
    even when it lands in the boundary window between subscribe registration
    and the backfill snapshot.

    Strategy: monkey-patch event_bus.subscribe so we can inject a "boundary"
    publish between subscription registration and the snapshot taken inside
    thread_stream.  After consuming the finite SSE body we count the delta
    occurrences and assert exactly 1 delivery per unique event.
    """

    @pytest.fixture(autouse=True)
    def _clear_bus(self) -> None:
        from yukar.events import bus as event_bus

        event_bus._thread_token_buffer.clear()

    async def test_backfill_event_delivered_exactly_once_via_endpoint(
        self, app_client: Any, tmp_workspace: Any
    ) -> None:
        """A token published in the subscribe→snapshot window must appear in
        the SSE body exactly once when the request is made via the real
        thread_stream endpoint."""
        import asyncio as _asyncio
        from contextlib import asynccontextmanager

        from yukar.events import bus as event_bus
        from yukar.models.epic import Epic
        from yukar.models.project import Project
        from yukar.storage.epic_repo import save_epic
        from yukar.storage.project_repo import save_project

        root = str(tmp_workspace)
        project_id = "p-mn3-ep"
        epic_id = "EP-MN3"
        thread_id = "worker-mn3-ep"

        await save_project(root, Project(id=project_id, name=project_id))
        await save_epic(root, project_id, Epic(id=epic_id, slug="mn3", title="Mn3"))

        original_subscribe = event_bus.subscribe

        @asynccontextmanager
        async def _patched_subscribe(
            project_id: str, epic_id: str, maxsize: int = 256
        ) -> AsyncGenerator[_asyncio.Queue[Any]]:
            async with original_subscribe(project_id, epic_id, maxsize) as q:
                # Publish a "boundary" event NOW — after the queue is
                # registered but before thread_stream takes its backfill
                # snapshot.  This replicates the exact Mn3 race.
                boundary_ev = _token_event(
                    thread_id,
                    delta="boundary",
                    project_id=project_id,
                    epic_id=epic_id,
                )
                event_bus.publish(project_id, epic_id, boundary_ev)

                # Also inject a sentinel so the SSE generator exits.
                q.put_nowait(None)

                yield q

        event_bus.subscribe = _patched_subscribe  # type: ignore[assignment]
        try:
            # Pre-existing backfill event (published before any subscriber).
            pre_ev = _token_event(
                thread_id,
                delta="pre",
                project_id=project_id,
                epic_id=epic_id,
            )
            event_bus.publish(project_id, epic_id, pre_ev)

            resp = await _asyncio.wait_for(
                app_client.get(
                    f"/api/projects/{project_id}/epics/{epic_id}/threads/{thread_id}/stream"
                ),
                timeout=5.0,
            )
        finally:
            event_bus.subscribe = original_subscribe  # type: ignore[assignment]

        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")

        # Parse SSE lines and extract JSON data payloads.
        body = resp.text
        received_deltas: list[str] = []
        for line in body.splitlines():
            if line.startswith("data:"):
                raw = line[len("data:") :].strip()
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                delta = payload.get("delta")
                if delta is not None:
                    received_deltas.append(delta)

        # "pre" event: delivered via backfill snapshot path.
        pre_count = received_deltas.count("pre")
        assert pre_count == 1, (
            f"'pre' event must appear exactly once; got {pre_count}. All deltas: {received_deltas}"
        )

        # "boundary" event: published in the subscribe→snapshot window.
        # It ends up in both the ring-buffer AND the live queue.
        # The dedup logic must deliver it exactly once.
        boundary_count = received_deltas.count("boundary")
        assert boundary_count == 1, (
            f"'boundary' event must appear exactly once (no dup, no loss); "
            f"got {boundary_count}. All deltas: {received_deltas}"
        )
