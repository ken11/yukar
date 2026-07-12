"""Runs router — POST .../run, POST .../run/{action}, GET .../run/events (SSE)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from yukar.api.routers import get_epic_or_404, shelve_or_409
from yukar.deps import SupervisorDep, UsageTrackerDep, WorkspaceRootDep
from yukar.events import bus as event_bus
from yukar.events.sse import (
    disconnect_aware_sse,
    format_keepalive,
    run_event_to_sse,
    sse_response,
)
from yukar.models.run import RunState

# Router for epic-scoped run endpoints.
router = APIRouter(
    prefix="/api/projects/{project_id}/epics/{epic_id}",
    tags=["runs"],
)

# Router for project-scoped event stream.
project_events_router = APIRouter(
    prefix="/api/projects/{project_id}",
    tags=["runs"],
)

# Project-events SSE timing constants (module-level so tests can reference them).
_POLL_INTERVAL: float = 1.0  # seconds between disconnect checks
_KEEPALIVE_TICKS: int = 15  # emit keepalive after this many consecutive poll timeouts


@router.post("/run", status_code=202)
async def start_run(
    project_id: str,
    epic_id: str,
    root: WorkspaceRootDep,
    supervisor: SupervisorDep,
    usage_tracker: UsageTrackerDep,
) -> dict[str, str]:
    """Start (or re-start) a run for this epic.

    - If no run is currently active: start a new run.  For epics that have an
      existing Strands session (previously run), the Manager will see its prior
      conversation history via FileSessionManager (continuation semantics).
    - If a run is currently EXECUTING a turn: return 409 Conflict.  A live
      run parked in ``waiting`` is shelved first (state preserved) — Start on
      a parked conversation behaves like a restart, not a 409 dead-end.
    - Budget exhausted: return 409 Conflict.

    Open epics may be (re-)started freely — the existing session history allows
    the Manager to resume context, which is how the user requests a revision
    after reviewing the work.  A completed epic returns 409: completing is a
    user decision, and starting a run is not an implicit reopen (the user must
    reopen the epic first).

    TOCTOU guard: reload the epic and register supervisor.start under
    epic_thread_lock so a concurrent run cannot start (and delete the worktree)
    after archive has confirmed no run is active.
    Lock order: epic_thread_lock (outer) → _start_lock (inner, run side only);
    the archive side never acquires _start_lock, so no cycle.
    """
    from yukar.storage import threads_repo
    from yukar.storage.thread_locks import epic_thread_lock

    epic = await get_epic_or_404(root, project_id, epic_id)
    if epic.status == "completed":
        raise HTTPException(
            status_code=409, detail="Epic is completed — reopen it before starting a run"
        )
    # 409 only when a live run is actually EXECUTING a turn.  A live run
    # parked in ``waiting`` does not hold the slot (P3 rule): pressing Start
    # on a parked conversation shelves the live task (state preserved) and
    # restarts — same semantics as restarting after a stop, instead of the
    # dead-end 409 the parked-live case used to produce.
    if supervisor.is_executing(project_id, epic_id):
        raise HTTPException(status_code=409, detail="Run already active for this epic")
    if usage_tracker.is_over_budget():
        raise HTTPException(status_code=409, detail="Budget limit reached")
    await shelve_or_409(supervisor, project_id, epic_id)

    # TOCTOU guard: hold epic_thread_lock while resolving the active trial and
    # registering the run.  This serialises against archive_thread / create_thread
    # which hold the same lock when they check is_running before mutating trials.
    # supervisor.start() acquires _start_lock internally; lock order is
    # epic_thread_lock (outer) → _start_lock (inner) — fixed, no cycle.
    async with epic_thread_lock(project_id, epic_id):
        # Re-read epic inside the lock so we see any status change that arrived
        # between the pre-lock check above and now (e.g. a concurrent "completed"
        # PATCH).
        from yukar.storage.epic_repo import get_epic as _get_epic

        epic_fresh = await _get_epic(root, project_id, epic_id)
        if epic_fresh is None:
            raise HTTPException(status_code=404, detail=f"Epic not found: {epic_id!r}")
        if epic_fresh.status == "completed":
            raise HTTPException(
                status_code=409, detail="Epic is completed — reopen it before starting a run"
            )

        # Resolve the active manager-trial thread_id so that the run is bound to
        # the correct worktree (worktrees/{manager_thread_id}/{repo}).
        # When active_thread_id is None the epic is single-trial and we fall back
        # to the legacy "manager" default (backward-compatible).
        manager_thread_id = epic_fresh.active_thread_id or "manager"

        # Establish the active-trial invariant: once a run has started, the epic
        # records which thread is the active manager trial.  Persisting it here
        # (rather than leaving it None for single-trial epics) means the frontend
        # resolves the active trial from epic.active_thread_id — which takes
        # priority over RunState.thread_id — so a subsequent reviewer run
        # (whose RunState.thread_id points at the reviewer thread) can never
        # shift the UI's notion of the manager trial and hide its composer.
        if epic_fresh.active_thread_id is None:
            from yukar.storage.epic_repo import save_epic as _save_epic

            epic_fresh.active_thread_id = manager_thread_id
            await _save_epic(root, project_id, epic_fresh)

        # Guard: if the resolved thread_id is archived, refuse to start a run for it.
        if epic_fresh.active_thread_id is not None:
            tf = await threads_repo.get_threads(root, project_id, epic_id)
            entry = next((t for t in tf.threads if t.id == manager_thread_id), None)
            if entry is not None and entry.status == "archived":
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Manager trial {manager_thread_id!r} is archived. "
                        "Create or activate a new trial before starting a run."
                    ),
                )

        try:
            run_id = await supervisor.start(
                root, project_id, epic_id, manager_thread_id=manager_thread_id
            )
        except RuntimeError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
    return {"run_id": run_id, "status": "started"}


@router.post("/run/{action}")
async def run_action(
    project_id: str,
    epic_id: str,
    action: Literal["pause", "resume", "stop"],
    supervisor: SupervisorDep,
) -> dict[str, str]:
    if supervisor.is_running(project_id, epic_id):
        if action == "pause":
            await supervisor.pause(project_id, epic_id)
        elif action == "resume":
            await supervisor.resume(project_id, epic_id)
        elif action == "stop":
            # A live run parked in waiting is shelved (task cancel, state stays
            # waiting) + RunStoppedEvent; an executing run gets the full stop.
            await supervisor.stop(project_id, epic_id)
        return {"status": action}
    # No live run for this epic.  ``waiting`` with no live task is the normal
    # resting state (nothing to stop — the next message resumes it), so there
    # is no parked-stop special case any more.
    raise HTTPException(status_code=404, detail="No active run for this epic")


@router.get("/run/state")
async def get_run_state(
    project_id: str,
    epic_id: str,
    root: WorkspaceRootDep,
) -> RunState:
    """Return the current RunState for an epic.

    Returns a default ``waiting`` RunState when no state.yaml exists yet (an
    epic that has never run is simply "your turn").  Returns 404 only when
    the epic itself does not exist.
    """
    from yukar.storage import state_repo

    await get_epic_or_404(root, project_id, epic_id)
    state = await state_repo.get_state(root, project_id, epic_id)
    if state is None:
        return RunState(run_id="", status="waiting")
    return state


@router.get("/run/events")
async def run_events_sse(
    project_id: str,
    epic_id: str,
) -> StreamingResponse:
    """SSE stream of RunEvents for an epic run.

    Backfill ordering (same pattern as thread_stream — "Mn3 fix"):
    Subscribe to the live queue *first*, then take a snapshot of the
    per-epic token ring-buffer for replay.  This eliminates the window
    between snapshot and subscribe where published events could be missed.

    Events that appear in both the backfill snapshot and the live queue are
    deduplicated by object identity (``publish`` appends the same object to
    both the ring-buffer and each subscriber queue, so ``id()`` equality is
    an exact match).
    """

    async def _stream() -> AsyncGenerator[str]:
        async with event_bus.subscribe(project_id, epic_id) as q:
            # Snapshot the backfill *after* registering the subscriber so that
            # any event published between snapshot and subscribe is guaranteed
            # to be in q (not lost).
            backfill = event_bus.get_epic_token_backfill(project_id, epic_id)
            # Track replayed objects by identity to dedup boundary events that
            # may also arrive via the live queue.
            replayed_ids: set[int] = set()
            for buffered_event in backfill:
                if hasattr(buffered_event, "model_dump"):
                    replayed_ids.add(id(buffered_event))
                    yield run_event_to_sse(buffered_event)

            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=15.0)
                    if event is None:
                        break
                    # Skip events that were already delivered via backfill
                    # (object identity dedup — same object appended to both
                    # buffer and queue by publish()).
                    if id(event) in replayed_ids:
                        continue
                    yield run_event_to_sse(event)
                except TimeoutError:
                    yield format_keepalive()

    return sse_response(_stream())


@project_events_router.get("/events")
async def project_events_sse(
    project_id: str,
    request: Request,
) -> StreamingResponse:
    """SSE stream of lifecycle events for all epics in a project.

    Delivers only lifecycle events: run_started / run_completed / run_failed /
    run_stopped / run_paused / run_resumed, the "your turn" signals
    your_turn / your_turn_ended (a conversation run parked in
    ``waiting`` / left it — used for live board badges), epic_status_changed,
    epic_merged and merge-progress events.  High-frequency events (token,
    tool_call, etc.) are excluded — this stream is intended for notification
    purposes.

    Each event payload already includes ``project_id`` and ``epic_id`` from
    ``BaseEvent``, so clients can identify which epic each notification
    belongs to.

    The generator exits when:
    - The client disconnects (``request.is_disconnected()`` returns True).
    - A ``None`` sentinel is published to the project queue.
    - A keepalive timeout fires (every 15 s) — disconnect is checked then.

    Disconnect detection
    --------------------
    We concurrently race the event-queue wait against a periodic disconnect
    poll so the generator does not hold open the ASGI response indefinitely
    when the client is gone.  The poll interval is short (1 s) so test
    teardown is not delayed.
    """
    return sse_response(
        disconnect_aware_sse(
            event_bus.subscribe_project(project_id),
            request,
            poll_interval=_POLL_INTERVAL,
            keepalive_ticks=_KEEPALIVE_TICKS,
        )
    )
