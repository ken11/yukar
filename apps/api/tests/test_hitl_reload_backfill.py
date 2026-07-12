"""Empirical tests for HITL reload (reconnect) behaviour.

These tests exercise the actual subscribe() backfill path end-to-end so that
the claim "reloading while waiting replays the your-turn signal" is
proved (or falsified) by running code rather than by static reasoning.

Scenarios
---------
(A) RunStarted → YourTurnEvent published → NEW subscribe() (reload
    equivalent) → backfill must contain the YourTurnEvent.
    If this test FAILS the backend replay is broken.
    If it PASSES the backend is correct and the bug, if any, is in the frontend.

(B) YourTurnEvent + YourTurnEndedEvent both published → new
    subscribe() → both events appear in order, resolved after requested.
    Ensures the frontend can determine "no longer awaiting" from replay alone.

(C) Eviction risk: publish RunStarted + UIR + 49 more lifecycle events (total
    51 events: RunStarted clears then we add UIR + 49 filler, deque maxlen=50)
    → verify UIR is NOT present (it was evicted) so we can report the risk.
    Then test a safe run: RunStarted + UIR + 48 fillers (50 total) → UIR survives.

Coverage-gap note
-----------------
test_ask_user_gate.py::TestYourTurnBusReplay only checks that the
event lands in the _replay deque.  It does NOT verify that a new subscribe()
call actually delivers the event to the subscriber queue — i.e. the reconnect
path through bus.subscribe() is untested.  These tests fill that gap.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

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


def _uir(project_id: str, epic_id: str) -> Any:
    """Build a YourTurnEvent (a pure "your turn" signal — no payload text)."""
    from yukar.models.events import YourTurnEvent

    return YourTurnEvent(
        **_base(project_id=project_id, epic_id=epic_id),
        thread_id="manager",
    )


def _uiresolved(project_id: str, epic_id: str) -> Any:
    """Build a YourTurnEndedEvent."""
    from yukar.models.events import YourTurnEndedEvent

    return YourTurnEndedEvent(
        **_base(project_id=project_id, epic_id=epic_id),
        thread_id="manager",
    )


def _run_started(project_id: str, epic_id: str) -> Any:
    from yukar.models.events import RunStartedEvent

    return RunStartedEvent(**_base(project_id=project_id, epic_id=epic_id))


def _run_paused(project_id: str, epic_id: str) -> Any:
    from yukar.models.events import RunPausedEvent

    return RunPausedEvent(**_base(project_id=project_id, epic_id=epic_id))


def _run_resumed(project_id: str, epic_id: str) -> Any:
    from yukar.models.events import RunResumedEvent

    return RunResumedEvent(**_base(project_id=project_id, epic_id=epic_id))


# ---------------------------------------------------------------------------
# Scenario (A): New subscribe() receives YourTurnEvent via backfill
# ---------------------------------------------------------------------------


class TestScenarioA_ReloadBackfill:
    """A new subscriber (= browser reload) receives the UIR event from replay.

    This is the authoritative test for the backend half of the report:
    "the your-turn marker disappears when the page is reloaded".

    If this test passes: backend replay is correct.
    If it fails: the backend _replay → subscribe flush path is broken.
    """

    async def test_new_subscriber_receives_uir_via_backfill(self) -> None:
        """Core reconnect scenario.

        Timeline:
          1. RunStartedEvent published (clears replay buffer).
          2. Some intermediate lifecycle events published.
          3. YourTurnEvent published → enters _replay deque.
          4. No live subscribers at this point (simulating "between connections").
          5. NEW subscribe() opened (= browser reload / re-connect).
          6. Backfill flush must deliver the YourTurnEvent.
        """
        from yukar.events import bus as event_bus
        from yukar.models.events import YourTurnEvent

        project_id = "hitl-a"
        epic_id = "EP-A"

        # Step 1: RunStarted clears the replay buffer for this key.
        event_bus.publish(project_id, epic_id, _run_started(project_id, epic_id))

        # Step 2: intermediate event (pause/resume represents any lifecycle churn).
        event_bus.publish(project_id, epic_id, _run_paused(project_id, epic_id))
        event_bus.publish(project_id, epic_id, _run_resumed(project_id, epic_id))

        # Step 3: YourTurnEvent — the turn ended; it is the user's turn.
        event_bus.publish(project_id, epic_id, _uir(project_id, epic_id))

        # Verify it is in _replay before subscribing.
        buf_before = list(event_bus._replay[(project_id, epic_id)])
        uir_in_buf = [e for e in buf_before if isinstance(e, YourTurnEvent)]
        assert uir_in_buf, (
            "PRECONDITION FAILED: YourTurnEvent not in _replay buffer. "
            "The test is broken, not the implementation."
        )

        # Step 4–5: Open a NEW subscribe() — this is the reload / reconnect.
        received: list[Any] = []
        async with event_bus.subscribe(project_id, epic_id) as q:
            # Drain all backfilled events (non-blocking — they were already queued).
            while not q.empty():
                received.append(q.get_nowait())

        # Step 6: Assert that UIR arrived via backfill.
        uir_received = [e for e in received if isinstance(e, YourTurnEvent)]
        assert len(uir_received) >= 1, (
            "BACKEND REPLAY BROKEN: YourTurnEvent was NOT delivered to a "
            "new subscriber via the replay buffer. A browser reload while waiting "
            "will lose the your-turn marker. "
            f"Received events: {[getattr(e, 'type', type(e).__name__) for e in received]}"
        )
        assert uir_received[0].thread_id == "manager"

    async def test_backfill_delivers_uir_after_any_intermediate_events(self) -> None:
        """UIR survives replay even when other lifecycle events precede it.

        Intermediate events (RunPaused, RunResumed, EpicStatusChangedEvent) must
        not displace the UIR from the replay buffer before maxlen is reached.
        """
        from yukar.events import bus as event_bus
        from yukar.models.events import YourTurnEvent

        project_id = "hitl-a2"
        epic_id = "EP-A2"

        event_bus.publish(project_id, epic_id, _run_started(project_id, epic_id))
        # 10 pause/resume cycles before the your-turn signal.
        for _ in range(10):
            event_bus.publish(project_id, epic_id, _run_paused(project_id, epic_id))
            event_bus.publish(project_id, epic_id, _run_resumed(project_id, epic_id))
        # UIR is the 22nd event in the deque (1 RunStarted + 20 pause/resume + 1 UIR).
        event_bus.publish(project_id, epic_id, _uir(project_id, epic_id))

        received: list[Any] = []
        async with event_bus.subscribe(project_id, epic_id) as q:
            while not q.empty():
                received.append(q.get_nowait())

        uir_received = [e for e in received if isinstance(e, YourTurnEvent)]
        assert len(uir_received) == 1, (
            f"Expected 1 UIR in backfill; got {len(uir_received)}. "
            f"Received: {[getattr(e, 'type', type(e).__name__) for e in received]}"
        )
        assert uir_received[0].thread_id == "manager"

    async def test_uir_backfill_includes_correct_metadata(self) -> None:
        """Backfilled UIR carries thread_id and run_id so the frontend can correlate."""
        from yukar.events import bus as event_bus
        from yukar.models.events import YourTurnEvent

        project_id = "hitl-a3"
        epic_id = "EP-A3"

        event_bus.publish(project_id, epic_id, _run_started(project_id, epic_id))
        ev = YourTurnEvent(
            project_id=project_id,
            epic_id=epic_id,
            run_id="run-meta-check",
            thread_id="manager",
        )
        event_bus.publish(project_id, epic_id, ev)

        received: list[Any] = []
        async with event_bus.subscribe(project_id, epic_id) as q:
            while not q.empty():
                received.append(q.get_nowait())

        uir_received = [e for e in received if isinstance(e, YourTurnEvent)]
        assert uir_received, "UIR not in backfill"
        got = uir_received[0]
        assert got.thread_id == "manager"
        assert got.run_id == "run-meta-check"
        assert got.project_id == project_id
        assert got.epic_id == epic_id


# ---------------------------------------------------------------------------
# Scenario (B): Request + Resolved both in replay → resolved after requested
# ---------------------------------------------------------------------------


class TestScenarioB_ResolvedAfterRequested:
    """After user answers, a reload must see both events in order.

    The frontend reducer should process:
      your_turn → set awaiting state
      your_turn_ended  → clear awaiting state
    resulting in the correct "no longer awaiting" UI state.
    """

    async def test_request_then_resolved_both_replayed_in_order(self) -> None:
        from yukar.events import bus as event_bus

        project_id = "hitl-b"
        epic_id = "EP-B"

        event_bus.publish(project_id, epic_id, _run_started(project_id, epic_id))
        event_bus.publish(project_id, epic_id, _uir(project_id, epic_id))
        event_bus.publish(project_id, epic_id, _uiresolved(project_id, epic_id))

        received: list[Any] = []
        async with event_bus.subscribe(project_id, epic_id) as q:
            while not q.empty():
                received.append(q.get_nowait())

        types = [getattr(e, "type", None) for e in received]
        assert "your_turn" in types, (
            f"your_turn not in replay. types: {types}"
        )
        assert "your_turn_ended" in types, (
            f"your_turn_ended not in replay. types: {types}"
        )
        # resolved must appear AFTER requested so reducer can transition correctly.
        uir_idx = types.index("your_turn")
        resolved_idx = types.index("your_turn_ended")
        assert resolved_idx > uir_idx, (
            f"your_turn_ended ({resolved_idx}) must come after "
            f"your_turn ({uir_idx}) in replay. types: {types}"
        )

    async def test_resolved_clears_awaiting_state_for_late_subscriber(self) -> None:
        """A late subscriber that replays request+resolved ends in 'running' state.

        This is the key correctness property: the frontend must NOT show the
        your-turn marker when the user has already replied.
        """
        from yukar.events import bus as event_bus
        from yukar.models.events import YourTurnEndedEvent, YourTurnEvent

        project_id = "hitl-b2"
        epic_id = "EP-B2"

        event_bus.publish(project_id, epic_id, _run_started(project_id, epic_id))
        event_bus.publish(project_id, epic_id, _uir(project_id, epic_id))
        event_bus.publish(project_id, epic_id, _uiresolved(project_id, epic_id))
        # After resolution the run continues — publish some more lifecycle events.
        event_bus.publish(project_id, epic_id, _run_paused(project_id, epic_id))
        event_bus.publish(project_id, epic_id, _run_resumed(project_id, epic_id))

        received: list[Any] = []
        async with event_bus.subscribe(project_id, epic_id) as q:
            while not q.empty():
                received.append(q.get_nowait())

        # Simulate frontend reducer: last event that changes awaiting state.
        awaiting = False
        for ev in received:
            if isinstance(ev, YourTurnEvent):
                awaiting = True
            elif isinstance(ev, YourTurnEndedEvent):
                awaiting = False

        assert not awaiting, (
            "Late subscriber reducer would show the your-turn marker even though "
            "the user already replied. Frontend would display a stale waiting UI. "
            f"Received event types: {[getattr(e, 'type', None) for e in received]}"
        )

    async def test_only_request_no_resolve_leaves_awaiting(self) -> None:
        """If no resolved event yet, late subscriber must see awaiting state.

        This is the scenario the user reported: reload while still awaiting.
        The backend must replay UIR so the frontend can reconstruct the state.
        """
        from yukar.events import bus as event_bus
        from yukar.models.events import YourTurnEndedEvent, YourTurnEvent

        project_id = "hitl-b3"
        epic_id = "EP-B3"

        event_bus.publish(project_id, epic_id, _run_started(project_id, epic_id))
        event_bus.publish(project_id, epic_id, _uir(project_id, epic_id))
        # No YourTurnEndedEvent — user hasn't answered yet.

        received: list[Any] = []
        async with event_bus.subscribe(project_id, epic_id) as q:
            while not q.empty():
                received.append(q.get_nowait())

        awaiting = False
        for ev in received:
            if isinstance(ev, YourTurnEvent):
                awaiting = True
            elif isinstance(ev, YourTurnEndedEvent):
                awaiting = False

        assert awaiting, (
            "Late subscriber must see the waiting state when only your_turn (no ended) "
            "was published. Backend replay is broken — the your-turn marker cannot be shown. "
            f"Received: {[getattr(e, 'type', None) for e in received]}"
        )


# ---------------------------------------------------------------------------
# Scenario (C): Eviction risk — _REPLAY_MAXLEN exceeded
# ---------------------------------------------------------------------------


class TestScenarioC_EvictionRisk:
    """Verify whether UIR is evicted when _REPLAY_MAXLEN (50) is exceeded.

    The deque has maxlen=50.  RunStartedEvent clears it on new run start so the
    effective capacity resets.  If a run publishes more than 50 lifecycle events
    after RunStartedEvent the oldest events (including UIR) may be silently dropped.

    This is the "long-running run" eviction risk.

    Design intent (from bus.py module docstring):
      "Only lifecycle events are buffered. High-frequency events (TokenEvent etc.)
       are intentionally excluded: they would push lifecycle events out of the
       ring-buffer."
    Lifecycle events ARE the ones that can push each other out if there are > 50.
    _REPLAY_MAXLEN=50 is generous for most runs but a long pause/resume cycle
    could theoretically exhaust it.

    These tests characterise the eviction boundary empirically.
    """

    @pytest.fixture(autouse=True)
    def setup(self) -> None:
        from yukar.events import bus as event_bus

        event_bus._replay.clear()

    def test_uir_evicted_when_total_exceeds_maxlen(self) -> None:
        """UIR IS evicted when (RunStarted + UIR + N fillers) > _REPLAY_MAXLEN.

        Scenario: RunStarted(1) + UIR(1) + 49 RunPaused filler = 51 total.
        After RunStarted the deque is empty; then we fill it with 50 events
        (UIR at position 1, then 49 filler).  At 51 total the first item after
        RunStarted (= UIR at position 0 of the 50-item window) is evicted.

        Wait — RunStarted itself goes into the deque after clearing it, so:
          deque after RunStarted: [RunStarted]  (len=1)
          deque after UIR:        [RunStarted, UIR]  (len=2)
          deque after 48 fillers: [RunStarted, UIR, f1..f48]  (len=50)
          deque after 49th filler: [UIR, f1..f49]  (len=50, RunStarted evicted)
          deque after 50th filler: [f1..f50]  (len=50, UIR evicted)

        So UIR is evicted when we add 49 fillers after UIR (total=51 including
        RunStarted, UIR).  The 50th filler pushes UIR out.
        """
        from yukar.events import bus as event_bus
        from yukar.models.events import YourTurnEvent

        project_id = "evict-yes"
        epic_id = "EP-EY"

        from yukar.events.bus import _REPLAY_MAXLEN

        # RunStarted clears deque, then adds itself.
        event_bus.publish(project_id, epic_id, _run_started(project_id, epic_id))
        # UIR is now index 1 (RunStarted at 0).
        event_bus.publish(project_id, epic_id, _uir(project_id, epic_id))
        # Add fillers until UIR is pushed out.
        # At (maxlen - 2) fillers the deque is full at maxlen with [RunStarted, UIR, fillers...].
        # The (maxlen - 1)th filler evicts RunStarted.
        # The maxlen-th filler evicts UIR.
        fillers_to_evict_uir = _REPLAY_MAXLEN  # one more than fits after RunStarted+UIR
        for _ in range(fillers_to_evict_uir):
            event_bus.publish(project_id, epic_id, _run_paused(project_id, epic_id))

        buf = list(event_bus._replay[(project_id, epic_id)])
        uir_in_buf = [e for e in buf if isinstance(e, YourTurnEvent)]

        # Document whether UIR was evicted.
        if uir_in_buf:
            # UIR survived — eviction hasn't occurred yet at this count.
            pytest.fail(
                f"UIR was NOT evicted after {fillers_to_evict_uir} filler events "
                f"(maxlen={_REPLAY_MAXLEN}). "
                "Either maxlen was increased or the eviction calculation is wrong. "
                f"Buf len={len(buf)}."
            )
        else:
            # Expected: UIR was evicted.
            # This is not a test failure but a documented risk: a long run with
            # many pause/resume cycles can lose the UIR event from the replay buffer.
            assert len(buf) == _REPLAY_MAXLEN, (
                f"Deque should be full at maxlen={_REPLAY_MAXLEN}; got {len(buf)}"
            )

    def test_uir_survives_when_total_at_maxlen(self) -> None:
        """UIR is NOT evicted when total events == _REPLAY_MAXLEN exactly.

        Scenario: RunStarted(1) + UIR(1) + 48 fillers = 50 total = maxlen.
        The deque is exactly full; UIR is at the second position and survives.
        """
        from yukar.events import bus as event_bus
        from yukar.events.bus import _REPLAY_MAXLEN
        from yukar.models.events import YourTurnEvent

        project_id = "evict-no"
        epic_id = "EP-EN"

        event_bus.publish(project_id, epic_id, _run_started(project_id, epic_id))
        event_bus.publish(project_id, epic_id, _uir(project_id, epic_id))
        # _REPLAY_MAXLEN - 2 fillers: total = 1 (RunStarted) + 1 (UIR) + 48 = 50 = maxlen.
        fillers = _REPLAY_MAXLEN - 2
        for _ in range(fillers):
            event_bus.publish(project_id, epic_id, _run_paused(project_id, epic_id))

        buf = list(event_bus._replay[(project_id, epic_id)])
        uir_in_buf = [e for e in buf if isinstance(e, YourTurnEvent)]
        assert uir_in_buf, (
            f"UIR must survive when total events ({_REPLAY_MAXLEN}) == maxlen. "
            f"Buf len={len(buf)}, types: {[getattr(e, 'type', None) for e in buf]}"
        )
        assert len(buf) == _REPLAY_MAXLEN

    def test_uir_survives_when_just_below_eviction_boundary(self) -> None:
        """One filler event short of the eviction boundary: UIR must survive.

        Boundary: RunStarted + UIR + 49 fillers = 51 → UIR evicted.
        Just below: RunStarted + UIR + 48 fillers = 50 → UIR survives.
        (Duplicates test_uir_survives_when_total_at_maxlen from a different angle.)
        """
        from yukar.events import bus as event_bus
        from yukar.events.bus import _REPLAY_MAXLEN
        from yukar.models.events import YourTurnEvent

        project_id = "evict-boundary"
        epic_id = "EP-EB"

        event_bus.publish(project_id, epic_id, _run_started(project_id, epic_id))
        event_bus.publish(project_id, epic_id, _uir(project_id, epic_id))
        # 49 filler events would evict UIR; use 48 instead.
        fillers_safe = _REPLAY_MAXLEN - 2  # = 48
        for _ in range(fillers_safe):
            event_bus.publish(project_id, epic_id, _run_paused(project_id, epic_id))

        buf = list(event_bus._replay[(project_id, epic_id)])
        uir_in_buf = [e for e in buf if isinstance(e, YourTurnEvent)]
        assert uir_in_buf, (
            f"UIR must survive at exactly {fillers_safe} fillers (one below eviction). "
            f"Buf len={len(buf)}"
        )

    async def test_new_subscriber_does_not_receive_evicted_uir(self) -> None:
        """When UIR is evicted, a new subscribe() does NOT deliver it.

        This test proves that once eviction happens the backend replay is
        genuinely broken for late subscribers — the your-turn marker cannot be
        restored even if the frontend handles replay correctly.
        """
        from yukar.events import bus as event_bus
        from yukar.events.bus import _REPLAY_MAXLEN
        from yukar.models.events import YourTurnEvent

        project_id = "evict-sub"
        epic_id = "EP-ES"

        event_bus.publish(project_id, epic_id, _run_started(project_id, epic_id))
        event_bus.publish(project_id, epic_id, _uir(project_id, epic_id))
        # Push past maxlen so UIR is evicted.
        for _ in range(_REPLAY_MAXLEN):
            event_bus.publish(project_id, epic_id, _run_paused(project_id, epic_id))

        received: list[Any] = []
        async with event_bus.subscribe(project_id, epic_id) as q:
            while not q.empty():
                received.append(q.get_nowait())

        uir_received = [e for e in received if isinstance(e, YourTurnEvent)]
        # UIR was evicted from _replay so subscribe() cannot deliver it.
        assert len(uir_received) == 0, (
            f"Evicted UIR should NOT appear in backfill; got {len(uir_received)}. "
            "This means the eviction calculation is wrong."
        )

    async def test_uir_survives_subscribe_backfill_within_maxlen(self) -> None:
        """End-to-end: UIR within maxlen survives and arrives via subscribe() backfill.

        Combines (A) and (C): publish RunStarted + UIR + safe filler count,
        then open a new subscribe() and verify UIR is in the queue.
        """
        from yukar.events import bus as event_bus
        from yukar.events.bus import _REPLAY_MAXLEN
        from yukar.models.events import YourTurnEvent

        project_id = "evict-e2e"
        epic_id = "EP-E2E"

        event_bus.publish(project_id, epic_id, _run_started(project_id, epic_id))
        event_bus.publish(project_id, epic_id, _uir(project_id, epic_id))
        # Add exactly _REPLAY_MAXLEN - 2 filler events (total = maxlen, UIR safe).
        for _ in range(_REPLAY_MAXLEN - 2):
            event_bus.publish(project_id, epic_id, _run_paused(project_id, epic_id))

        received: list[Any] = []
        async with event_bus.subscribe(project_id, epic_id) as q:
            while not q.empty():
                received.append(q.get_nowait())

        uir_received = [e for e in received if isinstance(e, YourTurnEvent)]
        assert len(uir_received) == 1, (
            f"UIR must be delivered via backfill when within maxlen. "
            f"Got {len(uir_received)} UIR events. "
            f"Received types: {[getattr(e, 'type', None) for e in received]}"
        )
        assert uir_received[0].thread_id == "manager"


# ---------------------------------------------------------------------------
# Coverage-gap explicit documentation test
# ---------------------------------------------------------------------------


class TestCoverageGapDocumented:
    """Explicit assertion that the prior test suite lacked reconnect coverage.

    test_ask_user_gate.py::TestYourTurnBusReplay.test_uir_event_in_replay_buffer
    only verifies that the event enters `_replay`.  It does NOT call subscribe()
    and does NOT verify the event arrives in the subscriber queue.

    This class documents the gap and proves the new tests (TestScenarioA_*)
    fill it.  No assertions beyond a pass — the gap is documented, not fixed
    by this class itself.
    """

    def test_gap_documented_prior_test_only_checks_replay_buffer(self) -> None:
        """The prior test checked _replay dict entry; subscribe() flush was untested.

        Specifically: test_uir_event_in_replay_buffer asserts
            event_bus._replay[key] contains YourTurnEvent
        but never opens a subscribe() context manager and never reads from
        the queue that subscribe() returns.

        TestScenarioA_ReloadBackfill.test_new_subscriber_receives_uir_via_backfill
        fills this gap by calling subscribe() and draining the queue.
        """
        # The gap is now filled by TestScenarioA_*. This test always passes and
        # serves as documentation of what changed.
        assert True
