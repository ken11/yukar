"""Tests for Bug-2b: epic-level token backfill in run/events SSE.

Covers:
- get_epic_token_backfill returns tokens from all threads of the target epic.
- get_epic_token_backfill does NOT include tokens from other epics or projects.
- run_events_sse delivers pre-subscribe TokenEvents via the backfill path.
- run_events_sse deduplicates events that land in both backfill and live queue
  (the subscribe-first / snapshot-second "Mn3 fix" pattern).
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _base(
    project_id: str = "p",
    epic_id: str = "e",
    run_id: str = "r",
) -> dict[str, Any]:
    return {
        "project_id": project_id,
        "epic_id": epic_id,
        "run_id": run_id,
        "ts": datetime.now(UTC).isoformat(),
    }


def _token_event(
    thread_id: str,
    delta: str = "x",
    project_id: str = "p",
    epic_id: str = "e",
) -> Any:
    from yukar.models.events import TokenEvent

    return TokenEvent(
        **_base(project_id=project_id, epic_id=epic_id),
        thread_id=thread_id,
        delta=delta,
    )


def _tool_call_event(thread_id: str, project_id: str = "p", epic_id: str = "e") -> Any:
    from yukar.models.events import ToolCallEvent

    return ToolCallEvent(
        **_base(project_id=project_id, epic_id=epic_id),
        thread_id=thread_id,
        tool_name="fs_read",
    )


# ---------------------------------------------------------------------------
# Tests for get_epic_token_backfill
# ---------------------------------------------------------------------------


class TestGetEpicTokenBackfill:
    """Unit tests for bus.get_epic_token_backfill."""

    def setup_method(self) -> None:
        from yukar.events import bus as event_bus

        event_bus._thread_token_buffer.clear()

    def test_returns_tokens_from_all_threads_in_epic(self) -> None:
        """All threads of the target (project_id, epic_id) are included."""
        from yukar.events import bus as event_bus

        ev_manager = _token_event("manager", delta="mgr")
        ev_worker1 = _token_event("worker-1", delta="w1")
        ev_worker2 = _token_event("worker-2", delta="w2")

        event_bus.publish("p", "e", ev_manager)
        event_bus.publish("p", "e", ev_worker1)
        event_bus.publish("p", "e", ev_worker2)

        backfill = event_bus.get_epic_token_backfill("p", "e")

        deltas = [ev.delta for ev in backfill]
        assert "mgr" in deltas
        assert "w1" in deltas
        assert "w2" in deltas
        assert len(backfill) == 3

    def test_excludes_tokens_from_different_epic(self) -> None:
        """Tokens from a sibling epic must not appear in the backfill."""
        from yukar.events import bus as event_bus

        ev_target = _token_event("worker-1", delta="target", epic_id="EP-1")
        ev_other = _token_event("worker-2", delta="other", epic_id="EP-2")

        event_bus.publish("p", "EP-1", ev_target)
        event_bus.publish("p", "EP-2", ev_other)

        backfill = event_bus.get_epic_token_backfill("p", "EP-1")

        deltas = [ev.delta for ev in backfill]
        assert "target" in deltas
        assert "other" not in deltas

    def test_excludes_tokens_from_different_project(self) -> None:
        """Tokens from a different project must not appear."""
        from yukar.events import bus as event_bus

        ev_proj_a = _token_event("worker-1", delta="proj-a", project_id="proj-a")
        ev_proj_b = _token_event("worker-1", delta="proj-b", project_id="proj-b")

        event_bus.publish("proj-a", "e", ev_proj_a)
        event_bus.publish("proj-b", "e", ev_proj_b)

        backfill = event_bus.get_epic_token_backfill("proj-a", "e")

        deltas = [ev.delta for ev in backfill]
        assert "proj-a" in deltas
        assert "proj-b" not in deltas

    def test_returns_empty_when_no_tokens(self) -> None:
        """Returns empty list when no tokens have been published."""
        from yukar.events import bus as event_bus

        backfill = event_bus.get_epic_token_backfill("no-project", "no-epic")
        assert backfill == []

    def test_returns_snapshot_copy(self) -> None:
        """The returned list is a copy; mutating it does not affect the buffer."""
        from yukar.events import bus as event_bus

        ev = _token_event("worker-1", delta="copy-test")
        event_bus.publish("p", "e", ev)

        backfill = event_bus.get_epic_token_backfill("p", "e")
        assert len(backfill) == 1

        # Mutating the returned list must not affect the internal buffer.
        backfill.clear()
        backfill2 = event_bus.get_epic_token_backfill("p", "e")
        assert len(backfill2) == 1

    def test_includes_tool_call_and_tool_result_events(self) -> None:
        """ToolCallEvent and ToolResultEvent are also buffered and returned."""
        from yukar.events import bus as event_bus
        from yukar.models.events import ToolResultEvent

        tc = _tool_call_event("worker-1")
        tr = ToolResultEvent(
            **_base(),
            thread_id="worker-1",
            tool_name="fs_read",
            result="file content",
        )
        event_bus.publish("p", "e", tc)
        event_bus.publish("p", "e", tr)

        backfill = event_bus.get_epic_token_backfill("p", "e")
        assert len(backfill) == 2

    def test_multiple_tokens_per_thread_all_returned(self) -> None:
        """Multiple tokens for a single thread are all included."""
        from yukar.events import bus as event_bus

        for i in range(5):
            ev = _token_event("worker-1", delta=f"chunk-{i}")
            event_bus.publish("p", "e", ev)

        backfill = event_bus.get_epic_token_backfill("p", "e")
        assert len(backfill) == 5
        deltas = [ev.delta for ev in backfill]
        for i in range(5):
            assert f"chunk-{i}" in deltas


# ---------------------------------------------------------------------------
# Tests for run_events_sse backfill behaviour
# ---------------------------------------------------------------------------


class TestRunEventsSseBackfill:
    """Endpoint-level tests for run/events SSE backfill (Bug-2b).

    Strategy: monkey-patch event_bus.subscribe to control the
    subscribe→snapshot boundary, inject a sentinel None to terminate the
    SSE stream, then parse the SSE body and verify delivery counts.
    """

    def setup_method(self) -> None:
        from yukar.events import bus as event_bus

        event_bus._thread_token_buffer.clear()

    async def test_backfill_tokens_delivered_before_subscribe(
        self, app_client: Any, tmp_workspace: Any
    ) -> None:
        """TokenEvents published before the subscriber connects are delivered
        via the backfill path in run_events_sse."""
        import asyncio

        from yukar.events import bus as event_bus
        from yukar.models.epic import Epic
        from yukar.models.project import Project
        from yukar.storage.epic_repo import save_epic
        from yukar.storage.project_repo import save_project

        root = str(tmp_workspace)
        project_id = "p-bf"
        epic_id = "EP-BF"

        await save_project(root, Project(id=project_id, name=project_id))
        await save_epic(root, project_id, Epic(id=epic_id, slug="ep-bf", title="BF"))

        # Publish a token before any subscriber — goes into the ring-buffer.
        pre_ev = _token_event(
            "worker-1", delta="pre-connect", project_id=project_id, epic_id=epic_id
        )
        event_bus.publish(project_id, epic_id, pre_ev)

        original_subscribe = event_bus.subscribe

        @asynccontextmanager
        async def _patched_subscribe(
            project_id: str, epic_id: str, maxsize: int = 256
        ) -> AsyncGenerator[asyncio.Queue[Any]]:
            async with original_subscribe(project_id, epic_id, maxsize) as q:
                q.put_nowait(None)  # terminate the stream immediately after backfill
                yield q

        event_bus.subscribe = _patched_subscribe  # type: ignore[assignment]
        try:
            resp = await asyncio.wait_for(
                app_client.get(f"/api/projects/{project_id}/epics/{epic_id}/run/events"),
                timeout=5.0,
            )
        finally:
            event_bus.subscribe = original_subscribe  # type: ignore[assignment]

        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")

        received_deltas = _parse_deltas(resp.text)

        assert received_deltas.count("pre-connect") == 1, (
            f"pre-connect token must be delivered via backfill exactly once; "
            f"got {received_deltas.count('pre-connect')}. All deltas: {received_deltas}"
        )

    async def test_backfill_no_duplicate_on_boundary_event(
        self, app_client: Any, tmp_workspace: Any
    ) -> None:
        """An event published in the subscribe→snapshot window (the Mn3 race
        for the epic-level stream) must be delivered exactly once, not twice."""
        import asyncio

        from yukar.events import bus as event_bus
        from yukar.models.epic import Epic
        from yukar.models.project import Project
        from yukar.storage.epic_repo import save_epic
        from yukar.storage.project_repo import save_project

        root = str(tmp_workspace)
        project_id = "p-mn3r"
        epic_id = "EP-MN3R"
        thread_id = "worker-mn3r"

        await save_project(root, Project(id=project_id, name=project_id))
        await save_epic(root, project_id, Epic(id=epic_id, slug="ep-mn3r", title="MN3R"))

        original_subscribe = event_bus.subscribe

        @asynccontextmanager
        async def _patched_subscribe(
            project_id: str, epic_id: str, maxsize: int = 256
        ) -> AsyncGenerator[asyncio.Queue[Any]]:
            async with original_subscribe(project_id, epic_id, maxsize) as q:
                # Publish a "boundary" event after subscribe registration but
                # before run_events_sse takes the backfill snapshot.
                boundary_ev = _token_event(
                    thread_id,
                    delta="boundary",
                    project_id=project_id,
                    epic_id=epic_id,
                )
                event_bus.publish(project_id, epic_id, boundary_ev)
                # Inject a sentinel to terminate the SSE stream.
                q.put_nowait(None)
                yield q

        # Pre-existing token (published before any subscriber).
        pre_ev = _token_event(thread_id, delta="pre", project_id=project_id, epic_id=epic_id)
        event_bus.publish(project_id, epic_id, pre_ev)

        event_bus.subscribe = _patched_subscribe  # type: ignore[assignment]
        try:
            resp = await asyncio.wait_for(
                app_client.get(f"/api/projects/{project_id}/epics/{epic_id}/run/events"),
                timeout=5.0,
            )
        finally:
            event_bus.subscribe = original_subscribe  # type: ignore[assignment]

        assert resp.status_code == 200

        received_deltas = _parse_deltas(resp.text)

        pre_count = received_deltas.count("pre")
        assert pre_count == 1, (
            f"'pre' event must appear exactly once; got {pre_count}. All: {received_deltas}"
        )

        boundary_count = received_deltas.count("boundary")
        assert boundary_count == 1, (
            f"'boundary' event must appear exactly once (no dup, no loss); "
            f"got {boundary_count}. All: {received_deltas}"
        )

    async def test_backfill_covers_multiple_threads(
        self, app_client: Any, tmp_workspace: Any
    ) -> None:
        """Backfill tokens from multiple threads (manager + worker) all appear
        in the run/events SSE stream."""
        import asyncio

        from yukar.events import bus as event_bus
        from yukar.models.epic import Epic
        from yukar.models.project import Project
        from yukar.storage.epic_repo import save_epic
        from yukar.storage.project_repo import save_project

        root = str(tmp_workspace)
        project_id = "p-multi"
        epic_id = "EP-MULTI"

        await save_project(root, Project(id=project_id, name=project_id))
        await save_epic(root, project_id, Epic(id=epic_id, slug="ep-multi", title="Multi"))

        # Publish tokens for two threads before any subscriber.
        ev_mgr = _token_event(
            "manager", delta="manager-token", project_id=project_id, epic_id=epic_id
        )
        ev_wkr = _token_event(
            "worker-1", delta="worker-token", project_id=project_id, epic_id=epic_id
        )
        event_bus.publish(project_id, epic_id, ev_mgr)
        event_bus.publish(project_id, epic_id, ev_wkr)

        original_subscribe = event_bus.subscribe

        @asynccontextmanager
        async def _patched_subscribe(
            project_id: str, epic_id: str, maxsize: int = 256
        ) -> AsyncGenerator[asyncio.Queue[Any]]:
            async with original_subscribe(project_id, epic_id, maxsize) as q:
                q.put_nowait(None)
                yield q

        event_bus.subscribe = _patched_subscribe  # type: ignore[assignment]
        try:
            resp = await asyncio.wait_for(
                app_client.get(f"/api/projects/{project_id}/epics/{epic_id}/run/events"),
                timeout=5.0,
            )
        finally:
            event_bus.subscribe = original_subscribe  # type: ignore[assignment]

        assert resp.status_code == 200

        received_deltas = _parse_deltas(resp.text)

        assert "manager-token" in received_deltas, (
            f"manager token must be in backfill. All: {received_deltas}"
        )
        assert "worker-token" in received_deltas, (
            f"worker token must be in backfill. All: {received_deltas}"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_deltas(sse_body: str) -> list[str]:
    """Extract all ``delta`` field values from an SSE body."""
    result: list[str] = []
    for line in sse_body.splitlines():
        if not line.startswith("data:"):
            continue
        raw = line[len("data:") :].strip()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        delta = payload.get("delta")
        if delta is not None:
            result.append(delta)
    return result
