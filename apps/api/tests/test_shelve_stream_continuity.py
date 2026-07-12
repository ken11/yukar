"""Shelve → continuation events arrive on the SAME SSE stream (P5).

A shelve (or server shutdown) cancels the run task WITHOUT the stop flag.
Since P5 the orchestrator's finally block publishes the ``None`` sentinel
only on stop / error / normal return — NOT on a not-stopped CancelledError.
A conversation has no end, so shelving must not sever subscriber streams:
one subscriber opened before the shelve keeps receiving the continuation
run's events with no ``None`` in between (no forced EventSource reconnect).

The stop path is asserted separately: a user stop still closes the stream.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from tests._helpers import make_git_repo, wait_for_run_status, wait_until

from .test_ask_user_gate import _bootstrap


def _fake_manager_factory(manager_script: list[Any]) -> Any:
    from yukar.llm.fake import FakeModel

    def fake_create_model(settings: Any, role: Any = None, **kwargs: Any) -> FakeModel:
        return FakeModel(script=list(manager_script))

    return fake_create_model


@pytest.mark.asyncio
async def test_shelve_then_continuation_on_same_stream(tmp_path: Path) -> None:
    """One subscriber sees run-1 park, the shelve (no sentinel), and run-2 events."""
    from yukar.agents.orchestrator import EpicOrchestrator
    from yukar.config.settings import LLMSettings
    from yukar.events import bus as event_bus
    from yukar.llm.fake import TextTurn
    from yukar.models.events import RunStartedEvent, YourTurnEvent
    from yukar.storage import state_repo

    git_repo = make_git_repo(tmp_path, "myrepo")
    root = str(tmp_path / "ws")
    project_id = "proj"
    epic_id = "EP-1"
    await _bootstrap(root, project_id, epic_id, git_repo)

    # ONE subscriber queue held open across shelve + continuation.  Collect
    # raw items (including any None sentinel) so stream severance is visible.
    received: list[Any] = []
    run2_parked = asyncio.Event()

    async def _collect() -> None:
        async with event_bus.subscribe(project_id, epic_id) as q:
            while True:
                item = await q.get()
                received.append(item)
                if (
                    item is not None
                    and isinstance(item, YourTurnEvent)
                    and item.run_id == "run-2"
                ):
                    run2_parked.set()
                    return
                if item is None and run2_parked.is_set():
                    return

    collector = asyncio.create_task(_collect())
    await asyncio.sleep(0)

    llm = LLMSettings(provider="fake")

    # --- run-1: park, then SHELVE (cancel without the stop flag) ---
    orch1 = EpicOrchestrator(
        llm_settings=llm, git_author_name="yukar", git_author_email="yukar@localhost"
    )
    with patch(
        "yukar.agents.orchestrator.create_model",
        side_effect=_fake_manager_factory([TextTurn("Plan ready. Your turn.")]),
    ):
        run1 = asyncio.create_task(orch1.start(root, project_id, epic_id, "run-1"))
        await wait_for_run_status(root, project_id, epic_id, "waiting", timeout=15.0)
        assert orch1.is_parked is True
        # Shelve: same contract as supervisor.shelve_waiting — task cancel,
        # stop flag NOT set.
        run1.cancel()
        await asyncio.gather(run1, return_exceptions=True)

    # The shelve must NOT have severed the stream: no None was delivered.
    assert None not in received, (
        "Shelving published the SSE sentinel — the stream was severed and a "
        f"live client would be forced to reconnect. received={received!r}"
    )
    # state.yaml still says waiting (the conversation is intact).
    state = await state_repo.get_state(root, project_id, epic_id)
    assert state is not None and state.status == "waiting"

    # --- run-2: continuation events arrive on the SAME subscriber queue ---
    orch2 = EpicOrchestrator(
        llm_settings=llm,
        git_author_name="yukar",
        git_author_email="yukar@localhost",
        seed_prompt="Please continue.",
        is_continuation=True,
    )
    with patch(
        "yukar.agents.orchestrator.create_model",
        side_effect=_fake_manager_factory([TextTurn("Continuing. Done for now.")]),
    ):
        run2 = asyncio.create_task(orch2.start(root, project_id, epic_id, "run-2"))

        async def _run2_waiting() -> bool:
            st = await state_repo.get_state(root, project_id, epic_id)
            return st is not None and st.run_id == "run-2" and st.status == "waiting"

        try:
            await wait_until(_run2_waiting, timeout=30.0, message="run-2 to park")
            await asyncio.wait_for(run2_parked.wait(), timeout=10.0)
        finally:
            if not run2.done():
                await orch2.stop()
        await asyncio.wait_for(run2, timeout=10.0)
    await asyncio.wait_for(collector, timeout=5.0)

    # Continuity: the single stream carries run-1's park, then run-2's
    # start and park, in order, with no sentinel in between.
    run1_park_idx = next(
        i
        for i, e in enumerate(received)
        if isinstance(e, YourTurnEvent) and e.run_id == "run-1"
    )
    run2_start_idx = next(
        i
        for i, e in enumerate(received)
        if isinstance(e, RunStartedEvent) and e.run_id == "run-2"
    )
    run2_park_idx = next(
        i
        for i, e in enumerate(received)
        if isinstance(e, YourTurnEvent) and e.run_id == "run-2"
    )
    assert run1_park_idx < run2_start_idx < run2_park_idx
    assert all(item is not None for item in received[: run2_park_idx + 1]), (
        "No sentinel may interrupt the shelve→continuation event flow"
    )


@pytest.mark.asyncio
async def test_user_stop_still_closes_the_stream(tmp_path: Path) -> None:
    """Contrast case: a USER stop (stop flag set) still publishes the sentinel."""
    from yukar.agents.orchestrator import EpicOrchestrator
    from yukar.config.settings import LLMSettings
    from yukar.events import bus as event_bus
    from yukar.llm.fake import TextTurn

    git_repo = make_git_repo(tmp_path, "myrepo2")
    root = str(tmp_path / "ws")
    project_id = "proj"
    epic_id = "EP-2"
    await _bootstrap(root, project_id, epic_id, git_repo)

    got_sentinel = asyncio.Event()

    async def _collect() -> None:
        async with event_bus.subscribe(project_id, epic_id) as q:
            while True:
                item = await q.get()
                if item is None:
                    got_sentinel.set()
                    return

    collector = asyncio.create_task(_collect())
    await asyncio.sleep(0)

    orch = EpicOrchestrator(
        llm_settings=LLMSettings(provider="fake"),
        git_author_name="yukar",
        git_author_email="yukar@localhost",
    )
    with patch(
        "yukar.agents.orchestrator.create_model",
        side_effect=_fake_manager_factory([TextTurn("Parking now.")]),
    ):
        run = asyncio.create_task(orch.start(root, project_id, epic_id, "run-stop"))
        await wait_for_run_status(root, project_id, epic_id, "waiting", timeout=15.0)
        await orch.stop()
        await asyncio.wait_for(run, timeout=10.0)

    await asyncio.wait_for(got_sentinel.wait(), timeout=5.0)
    await asyncio.wait_for(collector, timeout=5.0)
