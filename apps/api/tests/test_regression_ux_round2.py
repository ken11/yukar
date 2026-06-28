"""Regression tests for adversarial-review round-2 backend fixes.

Covers:
- Mj1+Mj2+N5: token buffer cleared for evaluator/manager/exception-worker on
  run boundary events (RunStartedEvent / RunCompletedEvent / RunFailedEvent).
  Stale manager tokens from a previous run must not appear in backfill.
- Mn3: event published during stream setup (between subscribe registration and
  backfill snapshot) is delivered exactly once (no loss, no duplicate).
- Mn6+N6: a single pause() emits PauseEffectiveEvent exactly once even when
  multiple concurrent callers hit _checkpoint().
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

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


def _make_orchestrator() -> Any:
    from yukar.agents.orchestrator import EpicOrchestrator
    from yukar.config.settings import LLMSettings

    return EpicOrchestrator(
        llm_settings=LLMSettings(provider="fake"),
        git_author_name="Test",
        git_author_email="test@example.com",
    )


# ---------------------------------------------------------------------------
# Mj1+Mj2: token buffers cleared on run boundary events
# ---------------------------------------------------------------------------


class TestTokenBufferClearedOnRunBoundary:
    """RunStartedEvent / RunCompletedEvent / RunFailedEvent must drop ALL
    _thread_token_buffer keys whose prefix matches (project_id, epic_id).

    This catches evaluator buffers, manager buffers, and exception-terminated
    worker buffers that never emit WorkerCompletedEvent.
    """

    def setup_method(self) -> None:
        from yukar.events import bus as event_bus

        event_bus._thread_token_buffer.clear()

    def test_evaluator_buffer_cleared_on_run_completed(self) -> None:
        from yukar.events import bus as event_bus
        from yukar.models.events import RunCompletedEvent

        # Simulate evaluator accumulating tokens during its work.
        ev = _token_event("eval-abc")
        event_bus.publish("p", "e", ev)
        assert len(event_bus.get_thread_token_backfill("p", "e", "eval-abc")) == 1

        # Run completes — evaluator never sends WorkerCompletedEvent.
        event_bus.publish("p", "e", RunCompletedEvent(**_base()))
        assert event_bus.get_thread_token_backfill("p", "e", "eval-abc") == [], (
            "Evaluator buffer must be cleared on RunCompletedEvent"
        )

    def test_evaluator_buffer_cleared_on_run_failed(self) -> None:
        from yukar.events import bus as event_bus
        from yukar.models.events import RunFailedEvent

        ev = _token_event("eval-xyz")
        event_bus.publish("p", "e", ev)
        assert len(event_bus.get_thread_token_backfill("p", "e", "eval-xyz")) == 1

        event_bus.publish("p", "e", RunFailedEvent(**_base(), error="boom"))
        assert event_bus.get_thread_token_backfill("p", "e", "eval-xyz") == [], (
            "Evaluator buffer must be cleared on RunFailedEvent"
        )

    def test_exception_worker_buffer_cleared_on_run_failed(self) -> None:
        """Worker that raises an exception never emits WorkerCompletedEvent.
        Its buffer must be cleared when the run fails."""
        from yukar.events import bus as event_bus
        from yukar.models.events import RunFailedEvent

        ev = _token_event("worker-dead")
        event_bus.publish("p", "e", ev)
        assert len(event_bus.get_thread_token_backfill("p", "e", "worker-dead")) == 1

        event_bus.publish("p", "e", RunFailedEvent(**_base(), error="worker exploded"))
        assert event_bus.get_thread_token_backfill("p", "e", "worker-dead") == [], (
            "Exception-worker buffer must be cleared on RunFailedEvent"
        )

    def test_manager_stale_tokens_cleared_on_run_started(self) -> None:
        """Manager key is constant across runs.  Tokens from run N must not
        appear in run N+1's backfill.

        Sequence: manager emits TokenEvent → run ends → new RunStartedEvent →
        get_thread_token_backfill("manager") must return [].
        """
        from yukar.events import bus as event_bus
        from yukar.models.events import RunCompletedEvent, RunStartedEvent

        # Run N: manager emits a token.
        ev = _token_event("manager", delta="narration from run N")
        event_bus.publish("p", "e", ev)
        assert len(event_bus.get_thread_token_backfill("p", "e", "manager")) == 1

        # Run N completes.
        event_bus.publish("p", "e", RunCompletedEvent(**_base()))
        # Buffer must be gone now.
        assert event_bus.get_thread_token_backfill("p", "e", "manager") == [], (
            "Manager buffer must be cleared on RunCompletedEvent"
        )

        # Run N+1 starts.
        event_bus.publish("p", "e", RunStartedEvent(**_base(run_id="run-2")))
        # Still empty — no stale tokens.
        assert event_bus.get_thread_token_backfill("p", "e", "manager") == [], (
            "Manager buffer must remain empty after RunStartedEvent"
        )

    def test_only_matching_prefix_cleared(self) -> None:
        """Buffers for a *different* (project_id, epic_id) must not be touched."""
        from yukar.events import bus as event_bus
        from yukar.models.events import RunCompletedEvent

        # Other epic.
        ev_other = _token_event("worker-other", project_id="p", epic_id="other-epic")
        event_bus.publish("p", "other-epic", ev_other)
        assert len(event_bus.get_thread_token_backfill("p", "other-epic", "worker-other")) == 1

        # Current epic run completes.
        event_bus.publish("p", "e", RunCompletedEvent(**_base()))

        # Other epic's buffer must be intact.
        assert len(event_bus.get_thread_token_backfill("p", "other-epic", "worker-other")) == 1, (
            "Buffers for a different epic must not be cleared by another epic's run boundary"
        )

    def test_multiple_thread_buffers_all_cleared(self) -> None:
        """All threads (worker, evaluator, manager) for the same epic are swept."""
        from yukar.events import bus as event_bus
        from yukar.models.events import RunCompletedEvent

        for thread_id in ("worker-1", "eval-1", "manager"):
            ev = _token_event(thread_id)
            event_bus.publish("p", "e", ev)
            assert len(event_bus.get_thread_token_backfill("p", "e", thread_id)) == 1

        event_bus.publish("p", "e", RunCompletedEvent(**_base()))

        for thread_id in ("worker-1", "eval-1", "manager"):
            assert event_bus.get_thread_token_backfill("p", "e", thread_id) == [], (
                f"Buffer for {thread_id} must be cleared on RunCompletedEvent"
            )


# ---------------------------------------------------------------------------
# Mn3: exactly-once delivery across the subscribe-then-snapshot boundary
# ---------------------------------------------------------------------------


class TestSubscribeFirstExactlyOnce:
    """subscribe() is entered before get_thread_token_backfill() snapshot is
    taken.  A TokenEvent published between subscribe registration and snapshot
    ends up in both the ring-buffer (snapshotted) and the live queue.
    The dedup by object identity must deliver it exactly once.
    """

    def setup_method(self) -> None:
        from yukar.events import bus as event_bus

        event_bus._thread_token_buffer.clear()

    async def test_boundary_event_delivered_exactly_once(self) -> None:
        """Simulate: subscribe → (publish happens) → snapshot → live loop.

        The event must appear exactly once in the delivered sequence.
        """
        from yukar.events import bus as event_bus

        project_id, epic_id, thread_id = "p-mn3", "e-mn3", "worker-mn3"

        # Pre-existing token (arrived well before the subscriber connects).
        pre_ev = _token_event(thread_id, delta="pre", project_id=project_id, epic_id=epic_id)
        event_bus.publish(project_id, epic_id, pre_ev)

        delivered: list[Any] = []

        async with event_bus.subscribe(project_id, epic_id) as q:
            # Publish a "boundary" event NOW — after subscribe is registered
            # but before we take the backfill snapshot.  This is exactly the
            # window that Mn3 targets.
            boundary_ev = _token_event(
                thread_id, delta="boundary", project_id=project_id, epic_id=epic_id
            )
            event_bus.publish(project_id, epic_id, boundary_ev)

            # Snapshot (mirrors what thread_stream does after subscribe).
            backfill = event_bus.get_thread_token_backfill(project_id, epic_id, thread_id)
            replayed_ids: set[int] = set()
            for ev in backfill:
                replayed_ids.add(id(ev))
                delivered.append(ev)

            # Drain the live queue (boundary_ev is there; pre_ev is NOT in the
            # queue because it was published before subscribe).
            # Also publish a post-subscribe live event to verify normal flow.
            live_ev = _token_event(thread_id, delta="live", project_id=project_id, epic_id=epic_id)
            event_bus.publish(project_id, epic_id, live_ev)

            # Consume everything in the queue without blocking (best-effort).
            await asyncio.sleep(0)
            while not q.empty():
                ev = q.get_nowait()
                if ev is None:
                    break
                if id(ev) in replayed_ids:
                    continue  # dedup
                evt_thread = getattr(ev, "thread_id", None)
                if evt_thread is None or evt_thread == thread_id:
                    delivered.append(ev)

        deltas = [e.delta for e in delivered]
        # pre_ev and boundary_ev must appear exactly once each.
        assert deltas.count("pre") == 1, f"pre_ev must appear exactly once: {deltas}"
        assert deltas.count("boundary") == 1, (
            f"boundary_ev must appear exactly once (no dup, no loss): {deltas}"
        )
        assert deltas.count("live") == 1, f"live_ev must appear exactly once: {deltas}"

    async def test_no_loss_when_event_published_before_snapshot(self) -> None:
        """Event published BEFORE subscribe (pre-existing backfill) must still
        be delivered via the snapshot path."""
        from yukar.events import bus as event_bus

        project_id, epic_id, thread_id = "p-mn3b", "e-mn3b", "worker-mn3b"

        pre_ev = _token_event(thread_id, delta="before", project_id=project_id, epic_id=epic_id)
        event_bus.publish(project_id, epic_id, pre_ev)

        async with event_bus.subscribe(project_id, epic_id) as _q:
            backfill = event_bus.get_thread_token_backfill(project_id, epic_id, thread_id)
            assert len(backfill) == 1
            assert backfill[0].delta == "before"


# ---------------------------------------------------------------------------
# Mn6+N6: PauseEffectiveEvent emitted exactly once per pause cycle
# ---------------------------------------------------------------------------


class TestPauseEffectiveExactlyOnce:
    """A single pause() + N concurrent _checkpoint() calls must emit
    PauseEffectiveEvent exactly once.
    """

    async def test_single_pause_emits_pause_effective_once(self) -> None:
        """Multiple concurrent _checkpoint() callers (simulating manager +
        parallel workers) must collectively emit PauseEffectiveEvent once."""
        from yukar.models.events import PauseEffectiveEvent

        orch = _make_orchestrator()
        emitted: list[Any] = []
        orch._pub = emitted.append
        orch._project_id = "p"
        orch._epic_id = "e"
        orch._run_id = "r"

        # Put orchestrator in paused state (as supervisor.pause() would do).
        await orch.pause()

        # Simulate N concurrent _checkpoint() calls (manager + 3 workers).
        tasks = [asyncio.create_task(orch._checkpoint()) for _ in range(4)]
        # Give all tasks a moment to reach the paused state and attempt emit.
        await asyncio.sleep(0.05)

        pause_effective_count = sum(1 for e in emitted if isinstance(e, PauseEffectiveEvent))
        assert pause_effective_count == 1, (
            f"Expected exactly 1 PauseEffectiveEvent, got {pause_effective_count}: {emitted}"
        )

        # Resume so tasks unblock cleanly.
        await orch.resume()
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=2.0)

    async def test_resume_then_pause_emits_again(self) -> None:
        """After resume(), a subsequent pause() must allow one more
        PauseEffectiveEvent to be emitted."""
        from yukar.models.events import PauseEffectiveEvent

        orch = _make_orchestrator()
        emitted: list[Any] = []
        orch._pub = emitted.append
        orch._project_id = "p"
        orch._epic_id = "e"
        orch._run_id = "r"

        # First pause cycle.
        await orch.pause()
        task1 = asyncio.create_task(orch._checkpoint())
        await asyncio.sleep(0.05)

        count_first = sum(1 for e in emitted if isinstance(e, PauseEffectiveEvent))
        assert count_first == 1, f"First pause cycle must emit exactly 1, got {count_first}"

        await orch.resume()
        await asyncio.wait_for(task1, timeout=1.0)

        # Second pause cycle.
        await orch.pause()
        task2 = asyncio.create_task(orch._checkpoint())
        await asyncio.sleep(0.05)

        count_second = sum(1 for e in emitted if isinstance(e, PauseEffectiveEvent))
        assert count_second == 2, (
            f"Second pause cycle must add exactly 1 more PauseEffectiveEvent, total={count_second}"
        )

        await orch.resume()
        await asyncio.wait_for(task2, timeout=1.0)

    async def test_no_pause_effective_when_not_paused(self) -> None:
        """When _paused is set (running), _checkpoint must emit nothing."""
        from yukar.models.events import PauseEffectiveEvent

        orch = _make_orchestrator()
        emitted: list[Any] = []
        orch._pub = emitted.append
        orch._project_id = "p"
        orch._epic_id = "e"
        orch._run_id = "r"

        assert orch._paused.is_set()  # running by default

        await orch._checkpoint()

        assert not any(isinstance(e, PauseEffectiveEvent) for e in emitted), (
            f"Must not emit PauseEffectiveEvent when running: {emitted}"
        )
