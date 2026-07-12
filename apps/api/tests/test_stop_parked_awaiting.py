"""Stopping a run that is parked in ``waiting`` (lifecycle redesign).

Successor of the e771261 regression guard.  Under the lifecycle redesign a parked run comes in
two shapes:

- LIVE parked run (task alive, waiting for a reply): ``POST /run/stop`` must
  succeed by cancelling the live task WITHOUT the stop flag (shelve) — the
  persisted state stays ``waiting`` (the conversation is intact and resumes
  as a continuation) and a RunStoppedEvent is published so clients converge.
- No live task, ``waiting`` on disk (e.g. after a server restart): that is
  the NORMAL resting state — there is nothing to stop, so /run/stop 404s and
  pause/resume 404 as before.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest


async def _seed_waiting(root: str, pid: str, eid: str) -> None:
    from yukar.models.epic import Epic
    from yukar.models.project import Project
    from yukar.models.run import RunState
    from yukar.storage import state_repo
    from yukar.storage.epic_repo import save_epic
    from yukar.storage.project_repo import save_project

    await save_project(root, Project(id=pid, name=pid, status="active", repos=[]))
    await save_epic(
        root, pid, Epic(id=eid, slug="s", title="t", description="d", branch="yukar/ep-s")
    )
    await state_repo.save_state(root, pid, eid, RunState(run_id="run-x", status="waiting"))


def _register_parked_run(sup: Any, root: str, pid: str, eid: str) -> tuple[Any, Any]:
    """Register a live run handle whose runner reports is_parked=True.

    Returns (task, runner_mock).
    """
    from yukar.runs.supervisor import _RunHandle

    runner = MagicMock()
    runner.is_parked = True
    runner.stop = AsyncMock()
    task: asyncio.Task[None] = asyncio.get_event_loop().create_task(asyncio.sleep(9999))
    sup._runs[sup._key(pid, eid)] = _RunHandle(
        run_id="run-x", runner=runner, task=task, root=root, project_id=pid, epic_id=eid
    )
    return task, runner


@pytest.mark.asyncio
async def test_stop_live_waiting_run_shelves_and_preserves_state(
    app_client: Any, tmp_workspace: Path
) -> None:
    """POST /run/stop on a LIVE waiting run → 200; the task is cancelled and
    the persisted state stays ``waiting`` (conversation preserved)."""
    from yukar.runs.supervisor import get_supervisor
    from yukar.storage import state_repo

    root = str(tmp_workspace)
    pid, eid = "parked-stop-proj", "EP-parked"
    await _seed_waiting(root, pid, eid)

    sup = get_supervisor()
    task, runner = _register_parked_run(sup, root, pid, eid)
    try:
        resp = await app_client.post(f"/api/projects/{pid}/epics/{eid}/run/stop")
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "stop"

        # The live task was cancelled (shelved), and the handle removed.
        assert task.cancelled() or task.done()
        assert not sup.is_running(pid, eid)
        # Shelving must NOT go through the full stop path (no stop flag).
        runner.stop.assert_not_awaited()

        # State on disk is untouched: still waiting, run resumable.
        state = await state_repo.get_state(root, pid, eid)
        assert state is not None
        assert state.status == "waiting"
    finally:
        sup._runs.pop(sup._key(pid, eid), None)
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


@pytest.mark.asyncio
async def test_stop_live_waiting_run_publishes_run_stopped(tmp_path: Path) -> None:
    """supervisor.stop on a parked run publishes RunStoppedEvent (replayable)
    so subscribed clients converge, even though state.yaml is untouched."""
    from yukar.events import bus as event_bus
    from yukar.models.run import RunState
    from yukar.runs.supervisor import RunSupervisor
    from yukar.storage import state_repo

    root = str(tmp_path)
    pid, eid = "p", "EP-shelve-event"
    await state_repo.save_state(root, pid, eid, RunState(run_id="run-x", status="waiting"))

    events: list[Any] = []

    async def _collect() -> None:
        async with event_bus.subscribe(pid, eid) as q:
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=2.0)
                except TimeoutError:
                    break
                if ev is None:
                    break
                events.append(ev)

    collector = asyncio.create_task(_collect())
    await asyncio.sleep(0)

    sup = RunSupervisor()
    task, _runner = _register_parked_run(sup, root, pid, eid)
    try:
        await sup.stop(pid, eid)
    finally:
        if not task.done():
            task.cancel()

    event_bus.publish(pid, eid, None)  # close the collector stream
    await asyncio.wait_for(collector, timeout=5.0)

    assert any(getattr(e, "type", None) == "run_stopped" for e in events), (
        f"Expected run_stopped after shelving stop, got: {[getattr(e, 'type', e) for e in events]}"
    )

    # State stays waiting — the stop only cancelled the live task.
    state = await state_repo.get_state(root, pid, eid)
    assert state is not None
    assert state.status == "waiting"


@pytest.mark.asyncio
async def test_stop_executing_run_uses_full_stop_path(tmp_path: Path) -> None:
    """A live EXECUTING run (is_parked=False) must go through runner.stop()
    (stop flag + graceful halt), not the shelve path."""
    from yukar.models.run import RunState
    from yukar.runs.supervisor import RunSupervisor, _RunHandle
    from yukar.storage import state_repo

    root = str(tmp_path)
    pid, eid = "p", "EP-exec-stop"
    await state_repo.save_state(root, pid, eid, RunState(run_id="run-x", status="running"))

    sup = RunSupervisor()
    runner = MagicMock()
    runner.is_parked = False
    runner.stop = AsyncMock()

    async def _finish_quickly() -> None:
        await asyncio.sleep(0.01)

    task = asyncio.get_event_loop().create_task(_finish_quickly())
    sup._runs[sup._key(pid, eid)] = _RunHandle(
        run_id="run-x", runner=runner, task=task, root=root, project_id=pid, epic_id=eid
    )
    try:
        await sup.stop(pid, eid)
        runner.stop.assert_awaited()
    finally:
        sup._runs.pop(sup._key(pid, eid), None)
        if not task.done():
            task.cancel()


@pytest.mark.asyncio
async def test_stop_with_no_live_run_404(app_client: Any, tmp_workspace: Path) -> None:
    """``waiting`` with no live task is the normal resting state: nothing to
    stop, so /run/stop 404s (there is no parked-stop special case any more)."""
    root = str(tmp_workspace)
    pid, eid = "no-live-proj", "EP-resting"
    await _seed_waiting(root, pid, eid)

    resp = await app_client.post(f"/api/projects/{pid}/epics/{eid}/run/stop")
    assert resp.status_code == 404, resp.text

    # The resting state is untouched.
    from yukar.storage import state_repo

    saved = await state_repo.get_state(root, pid, eid)
    assert saved is not None
    assert saved.status == "waiting"


@pytest.mark.asyncio
async def test_pause_with_no_live_run_404(app_client: Any, tmp_workspace: Path) -> None:
    """pause/resume have no meaning without a live run — they must still 404,
    and must NOT alter the resting state."""
    root = str(tmp_workspace)
    pid, eid = "no-live-pause-proj", "EP-resting2"
    await _seed_waiting(root, pid, eid)

    resp = await app_client.post(f"/api/projects/{pid}/epics/{eid}/run/pause")
    assert resp.status_code == 404, resp.text

    from yukar.storage import state_repo

    saved = await state_repo.get_state(root, pid, eid)
    assert saved is not None
    assert saved.status == "waiting"
