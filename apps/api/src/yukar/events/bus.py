"""asyncio pub/sub event bus — (project_id, epic_id) scoped fan-out.

Single event loop assumed (uvicorn workers=1).
Subscribers receive all events for their (project, epic) pair.

Replay buffer
-------------
Lifecycle events (RunStartedEvent / RunCompletedEvent / RunFailedEvent) are
buffered per (project_id, epic_id) key so that a subscriber that connects
*after* a run has already started (or finished) still receives those events.

Design decisions:
- Only lifecycle events are buffered.  High-frequency events (TokenEvent etc.)
  are intentionally excluded: they would push lifecycle events out of the
  ring-buffer and are recoverable from state.yaml / REST endpoints.
- None sentinels are NOT replayed.  Replaying None to a fresh subscriber would
  close their SSE stream immediately, causing an EventSource reconnect storm.
- When a new RunStartedEvent is published the replay buffer for that key is
  cleared.  This prevents stale events from a previous run being delivered to
  subscribers that open the stream for a brand-new run.
- No lock is needed: single event loop (uvicorn workers=1).

Token buffer lifecycle
----------------------
Per-thread token ring-buffers (``_thread_token_buffer``) accumulate tokens for
backfill.  A ``WorkerCompletedEvent`` clears the worker's own buffer entry.
However, evaluator threads, the manager thread, and workers that raise
exceptions never emit ``WorkerCompletedEvent``, so their buffer entries would
otherwise leak indefinitely.  The manager key is constant across runs, so stale
tokens from a previous run would bleed into the next run's backfill.

To prevent this, ``RunStartedEvent``, ``RunCompletedEvent``, and
``RunFailedEvent`` all drop every ``_thread_token_buffer`` key whose prefix
matches ``(project_id, epic_id)``.  These events are emitted from the
orchestrator's try/except/finally so they fire on every run outcome.

Global usage stream
-------------------
``publish_usage`` / ``subscribe_usage`` / ``publish_usage_sentinel`` provide a
project-and-epic-agnostic fan-out for TokenUsageEvent and BudgetExceededEvent.
This is used by ``GET /api/usage/stream`` (Topbar / global dashboard).

No replay buffer for the usage stream: current totals are always recoverable
from ``GET /api/usage`` (REST).  A late subscriber should call that endpoint
first and then subscribe to the stream for incremental updates.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import defaultdict, deque
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from yukar.models.events import (
    EpicMergeProgressEvent,
    EpicStatusChangedEvent,
    ManagerMessageEvent,
    RunCompletedEvent,
    RunFailedEvent,
    RunPausedEvent,
    RunResumedEvent,
    RunStartedEvent,
    RunStoppedEvent,
    SensitiveFileWrittenEvent,
    TokenEvent,
    ToolCallEvent,
    ToolResultEvent,
    UserInputRequestedEvent,
    UserInputResolvedEvent,
    UserMessageCommittedEvent,
    WorkerCompletedEvent,
    WorkerFailedEvent,
)

logger = logging.getLogger(__name__)

# Key: (project_id, epic_id)
_queues: dict[tuple[str, str], list[asyncio.Queue[Any]]] = defaultdict(list)

# Project-level subscriber queues — fan-out of lifecycle events only.
# Key: project_id
_project_queues: dict[str, list[asyncio.Queue[Any]]] = defaultdict(list)

# Global usage subscriber queues — fan-out of TokenUsageEvent / BudgetExceededEvent
# across all projects and epics.  No replay buffer (see module docstring).
_usage_queues: list[asyncio.Queue[Any]] = []

# Replay buffer: only lifecycle events, bounded to avoid unbounded growth.
# Unlike _queues, keys are never removed: each deque is bounded (maxlen) and
# the key count grows only with the number of epics ever run in this process,
# which is small for a local single-user server.  Revisit if epics become
# mass-produced (e.g. drop the key after the terminal event is consumed).
_REPLAY_MAXLEN = 50
_replay: dict[tuple[str, str], deque[Any]] = defaultdict(lambda: deque(maxlen=_REPLAY_MAXLEN))

# Types that are eligible for the replay buffer (and project-level fan-out).
_LIFECYCLE_TYPES = (
    RunStartedEvent,
    RunCompletedEvent,
    RunFailedEvent,
    RunStoppedEvent,
    RunPausedEvent,
    RunResumedEvent,
    UserInputRequestedEvent,
    UserInputResolvedEvent,
    EpicStatusChangedEvent,
    EpicMergeProgressEvent,
    SensitiveFileWrittenEvent,
)

# Per-thread token ring-buffer for backfill (A4 — issue 3).
#
# Key: (project_id, epic_id, thread_id)
# Value: deque of TokenEvent / ToolCallEvent / ToolResultEvent
#
# When a subscriber opens the thread stream *after* tokens have already been
# emitted, this buffer lets us replay recent activity so the screen is not
# empty.  Bounded to ~200 events per thread to stay memory-safe.
# ``workers=1`` — no lock needed.
#
# Worker buffers are cleared on WorkerCompletedEvent.  Evaluator, manager, and
# exception-terminated worker buffers are cleared on RunStartedEvent /
# RunCompletedEvent / RunFailedEvent (see module docstring for rationale).
_TOKEN_BUFFER_MAXLEN = 200
_TOKEN_BUFFER_TYPES = (TokenEvent, ToolCallEvent, ToolResultEvent)
_thread_token_buffer: dict[tuple[str, str, str], deque[Any]] = defaultdict(
    lambda: deque(maxlen=_TOKEN_BUFFER_MAXLEN)
)

# Run-boundary event types that trigger a full prefix sweep of _thread_token_buffer.
_RUN_BOUNDARY_TYPES = (RunStartedEvent, RunCompletedEvent, RunFailedEvent, RunStoppedEvent)

# Per-thread user-message ring-buffer for backfill (PR-B).
#
# Key: (project_id, epic_id, thread_id)
# Value: deque of UserMessageCommittedEvent
#
# When a subscriber opens the thread SSE stream *after* a user message has
# already been committed (e.g. after reconnect), this buffer lets us replay
# the committed user messages so the UI is not missing the human turn.
# Bounded to ~50 messages per thread (user messages are rare compared to tokens).
# ``workers=1`` — no lock needed.
#
# Unlike the ephemeral token buffer, committed user messages are PERSISTENT: they
# must survive run completion so a late/reconnect subscriber *after* the run still
# replays the human turn. The buffer is therefore cleared ONLY at the START of a
# new run (RunStartedEvent), so a prior run's messages do not bleed into the next
# run's backfill while still being replayable until then.
_USER_MSG_BUFFER_MAXLEN = 50
_thread_user_msg_buffer: dict[tuple[str, str, str], deque[Any]] = defaultdict(
    lambda: deque(maxlen=_USER_MSG_BUFFER_MAXLEN)
)


def publish(project_id: str, epic_id: str, event: Any) -> None:
    """Publish an event to all current subscribers for (project_id, epic_id).

    Lifecycle events are also stored in the replay buffer so that late
    subscribers receive them on ``subscribe()``.  A new RunStartedEvent clears
    the previous buffer to avoid delivering stale events from a prior run.

    Lifecycle events are additionally fan-out to the project-level queue so
    that ``subscribe_project`` subscribers receive them.

    Token/tool events are also stored in per-thread ring-buffers so that a
    subscriber connecting to ``thread_stream`` *after* a worker has already
    started can receive recently emitted tokens (backfill, issue 3).
    """
    key = (project_id, epic_id)

    if isinstance(event, _LIFECYCLE_TYPES):
        # Clear replay buffer on new run start so stale lifecycle events are
        # not delivered to subscribers that open the stream for a new run.
        if isinstance(event, RunStartedEvent):
            _replay[key].clear()
        _replay[key].append(event)
        # Fan-out ALL lifecycle events (including RunPausedEvent /
        # RunResumedEvent) to project-level subscribers.
        for q in _project_queues[project_id]:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning(
                    "project-level SSE queue full for project %s; dropping %s",
                    project_id,
                    type(event).__name__,
                )

    # Drop all per-thread token buffers for this (project_id, epic_id) on run
    # boundaries so stale tokens from evaluators, the manager, and
    # exception-terminated workers never bleed into a subsequent run's backfill.
    # WorkerCompletedEvent only clears the completing worker's own key; this
    # sweep handles every other thread that never emits WorkerCompletedEvent.
    if isinstance(event, _RUN_BOUNDARY_TYPES):
        stale_keys = [k for k in _thread_token_buffer if k[0] == project_id and k[1] == epic_id]
        for stale_key in stale_keys:
            del _thread_token_buffer[stale_key]

    # The user-message buffer holds PERSISTENT committed messages, so unlike the
    # token buffer it is cleared ONLY at the start of a new run — not on
    # completion/failure/stop — so a late/reconnect subscriber after the run can
    # still replay the human turn (frontend dedups by message_id against REST).
    if isinstance(event, RunStartedEvent):
        stale_msg_keys = [
            k for k in _thread_user_msg_buffer if k[0] == project_id and k[1] == epic_id
        ]
        for stale_key in stale_msg_keys:
            del _thread_user_msg_buffer[stale_key]

    # Accumulate token/tool events in per-thread ring-buffer for backfill.
    if isinstance(event, _TOKEN_BUFFER_TYPES):
        thread_id: str = getattr(event, "thread_id", "")
        if thread_id:
            thread_key = (project_id, epic_id, thread_id)
            _thread_token_buffer[thread_key].append(event)

    # Clear thread buffer when a Worker finishes (tokens are no longer useful
    # for backfill; the final text is in the session store).
    if isinstance(event, WorkerCompletedEvent):
        worker_thread_key = (project_id, epic_id, event.worker_id)
        _thread_token_buffer.pop(worker_thread_key, None)

    if isinstance(event, WorkerFailedEvent):
        _thread_token_buffer.pop((project_id, epic_id, event.worker_id), None)

    # Clear the manager token buffer after each Manager turn completes.
    # ManagerMessageEvent is the canonical end-of-turn marker for the manager.
    # Without this, a mid-run reload would replay all per-turn deltas
    # concatenated, producing duplicated/corrupted narration in the UI.
    # The final turn text is already persisted in the Strands session store,
    # so late joiners should call list_messages instead of relying on backfill.
    if isinstance(event, ManagerMessageEvent):
        _thread_token_buffer.pop((project_id, epic_id, event.thread_id), None)

    # Accumulate UserMessageCommittedEvent in per-thread ring-buffer for backfill.
    # A late subscriber to the thread SSE stream can replay committed user messages
    # so the human-authored text is visible on reconnect.
    if isinstance(event, UserMessageCommittedEvent):
        user_msg_key = (project_id, epic_id, event.thread_id)
        _thread_user_msg_buffer[user_msg_key].append(event)

    for q in _queues[key]:
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning(
                "epic-level SSE queue full for (%s, %s); dropping %s",
                project_id,
                epic_id,
                type(event).__name__,
            )


def get_thread_token_backfill(project_id: str, epic_id: str, thread_id: str) -> list[Any]:
    """Return a snapshot of the per-thread token ring-buffer for backfill.

    Called by the thread stream endpoint before subscribing to the live queue
    so that a late-connecting viewer receives recently emitted tokens.
    The returned list is a copy — safe to iterate while new events arrive.
    """
    thread_key = (project_id, epic_id, thread_id)
    buf = _thread_token_buffer.get(thread_key)
    return list(buf) if buf else []


def get_epic_token_backfill(project_id: str, epic_id: str) -> list[Any]:
    """Return a snapshot of all per-thread token ring-buffers for an epic.

    Called by the run/events SSE endpoint after subscribing to the live queue
    so that a late-connecting viewer receives recently emitted tokens across
    all threads (manager, workers, evaluators) for the epic.

    The returned list is a flat copy of all matching thread buffers in dict
    iteration order (insertion order, per CPython 3.7+).  Each event carries
    a ``thread_id`` attribute so callers can demux by thread.

    Snapshot semantics are identical to :func:`get_thread_token_backfill`:
    the caller must have already entered ``subscribe(project_id, epic_id)``
    before calling this function to avoid a missed-event window.
    """
    result: list[Any] = []
    for key, buf in _thread_token_buffer.items():
        if key[0] == project_id and key[1] == epic_id:
            result.extend(buf)
    return result


def get_user_message_backfill(project_id: str, epic_id: str, thread_id: str) -> list[Any]:
    """Return a snapshot of the per-thread user-message ring-buffer for backfill.

    Called by the thread stream endpoint *after* registering the subscriber
    (subscribe-first / snapshot-second, matching the token backfill pattern)
    so that a late-connecting viewer receives committed user messages.
    The returned list is a copy — safe to iterate while new events arrive.
    """
    user_msg_key = (project_id, epic_id, thread_id)
    buf = _thread_user_msg_buffer.get(user_msg_key)
    return list(buf) if buf else []


@asynccontextmanager
async def subscribe(
    project_id: str, epic_id: str, maxsize: int = 256
) -> AsyncGenerator[asyncio.Queue[Any]]:
    """Context manager that registers a subscriber queue and cleans up on exit.

    Before yielding the queue, any buffered lifecycle events are flushed into
    it so that late subscribers see the current run state immediately.
    """
    key = (project_id, epic_id)
    q: asyncio.Queue[Any] = asyncio.Queue(maxsize=maxsize)
    _queues[key].append(q)
    try:
        # Replay buffered lifecycle events to the new subscriber.
        # None sentinels are never in the buffer (see module docstring).
        for buffered_event in list(_replay[key]):
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(buffered_event)
        yield q
    finally:
        with contextlib.suppress(ValueError):
            _queues[key].remove(q)
        if not _queues[key]:
            del _queues[key]


async def event_stream(project_id: str, epic_id: str) -> AsyncGenerator[Any]:
    """Async generator that yields events as they arrive.

    Test-only helper — production SSE routes use ``subscribe()`` directly.
    """
    async with subscribe(project_id, epic_id) as q:
        while True:
            event = await q.get()
            if event is None:  # Sentinel to stop
                break
            yield event


def publish_project_sentinel(project_id: str) -> None:
    """Publish a ``None`` sentinel to all project-level subscribers.

    This signals each ``subscribe_project`` consumer to stop iteration.
    Useful in tests and shutdown paths to avoid leaving generators blocked
    on an empty queue.
    """
    for q in _project_queues.get(project_id, []):
        with contextlib.suppress(asyncio.QueueFull):
            q.put_nowait(None)


@asynccontextmanager
async def subscribe_project(
    project_id: str, maxsize: int = 256
) -> AsyncGenerator[asyncio.Queue[Any]]:
    """Context manager for a project-level lifecycle event queue.

    Only lifecycle events (run_started, run_completed, run_failed,
    run_paused, run_resumed) are delivered here — high-frequency events
    (token, tool_call, etc.) are excluded.

    No replay buffer for project-level subscriptions: the project stream is
    used for notifications, not for state reconstruction.
    """
    q: asyncio.Queue[Any] = asyncio.Queue(maxsize=maxsize)
    _project_queues[project_id].append(q)
    try:
        yield q
    finally:
        with contextlib.suppress(ValueError):
            _project_queues[project_id].remove(q)
        if not _project_queues[project_id]:
            del _project_queues[project_id]


# ---------------------------------------------------------------------------
# Global usage stream (TokenUsageEvent / BudgetExceededEvent)
# ---------------------------------------------------------------------------


def publish_usage(event: Any) -> None:
    """Publish a usage event to all global usage subscribers.

    Intended for :class:`~yukar.models.events.TokenUsageEvent` and
    :class:`~yukar.models.events.BudgetExceededEvent`.  Events are delivered
    to every active ``subscribe_usage`` consumer regardless of project or epic.

    Queue-full events are dropped with a warning (same policy as the epic-level
    and project-level queues).  No replay buffer — late subscribers should call
    ``GET /api/usage`` first to obtain current totals, then subscribe here for
    incremental updates.
    """
    for q in _usage_queues:
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning(
                "global usage SSE queue full; dropping %s",
                type(event).__name__,
            )


def publish_usage_sentinel() -> None:
    """Publish a ``None`` sentinel to all global usage subscribers.

    Signals each ``subscribe_usage`` consumer to stop iteration.
    Useful in tests and shutdown paths to avoid leaving generators blocked
    on an empty queue.
    """
    for q in _usage_queues:
        with contextlib.suppress(asyncio.QueueFull):
            q.put_nowait(None)


@asynccontextmanager
async def subscribe_usage(maxsize: int = 256) -> AsyncGenerator[asyncio.Queue[Any]]:
    """Context manager for the global usage event queue.

    Delivers :class:`~yukar.models.events.TokenUsageEvent` and
    :class:`~yukar.models.events.BudgetExceededEvent` published by
    ``publish_usage``, spanning all projects and epics.

    No replay buffer: current totals are always recoverable from
    ``GET /api/usage``.  Subscribers should fetch that endpoint for the
    current snapshot and then subscribe here for incremental updates.
    """
    q: asyncio.Queue[Any] = asyncio.Queue(maxsize=maxsize)
    _usage_queues.append(q)
    try:
        yield q
    finally:
        with contextlib.suppress(ValueError):
            _usage_queues.remove(q)
