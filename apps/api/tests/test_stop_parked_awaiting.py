"""Stopping a run that is *parked* in awaiting_input with no live task.

recovery.py preserves an awaiting_input run on disk across a server restart
(the user is expected to reply, which starts a continuation).  Such a run has no
live task, so ``is_running`` is False — yet the persisted RunState still drives
the UI's awaiting banner AND Stop button.  Previously ``POST /run/stop`` 404ed in
that state, so the user could not dismiss the parked question (a reviewer parked
at ``ask_user`` after an auto-reload is the common case).  It must now succeed by
resetting the run to idle.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest


async def _seed_awaiting(root: str, pid: str, eid: str, question: str = "Proceed?") -> None:
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
    await state_repo.save_state(
        root,
        pid,
        eid,
        RunState(run_id="run-x", status="awaiting_input", pending_question=question),
    )


@pytest.mark.asyncio
async def test_stop_clears_parked_awaiting_run(app_client: Any, tmp_workspace: Path) -> None:
    """POST /run/stop on a parked awaiting_input run (no live task) → 200 and the
    run is reset to idle with pending_question cleared."""
    root = str(tmp_workspace)
    pid, eid = "parked-stop-proj", "EP-parked"
    await _seed_awaiting(root, pid, eid)

    resp = await app_client.post(f"/api/projects/{pid}/epics/{eid}/run/stop")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "stop"

    state = await app_client.get(f"/api/projects/{pid}/epics/{eid}/run/state")
    body = state.json()
    assert body["status"] == "idle", body
    assert body["pending_question"] is None, body


@pytest.mark.asyncio
async def test_pause_on_parked_awaiting_still_404(app_client: Any, tmp_workspace: Path) -> None:
    """pause/resume have no meaning without a live run — they must still 404,
    and must NOT alter the parked state."""
    root = str(tmp_workspace)
    pid, eid = "parked-pause-proj", "EP-parked2"
    await _seed_awaiting(root, pid, eid)

    resp = await app_client.post(f"/api/projects/{pid}/epics/{eid}/run/pause")
    assert resp.status_code == 404, resp.text

    from yukar.storage import state_repo

    saved = await state_repo.get_state(root, pid, eid)
    assert saved is not None
    assert saved.status == "awaiting_input"  # untouched


@pytest.mark.asyncio
async def test_stop_with_no_state_404(app_client: Any, tmp_workspace: Path) -> None:
    """A genuinely idle epic (no run, no parked awaiting state) still 404s on stop."""
    from yukar.models.epic import Epic
    from yukar.models.project import Project
    from yukar.storage.epic_repo import save_epic
    from yukar.storage.project_repo import save_project

    root = str(tmp_workspace)
    pid, eid = "no-run-proj", "EP-idle"
    await save_project(root, Project(id=pid, name=pid, status="active", repos=[]))
    await save_epic(root, pid, Epic(id=eid, slug="s", title="t", branch="yukar/ep-s"))

    resp = await app_client.post(f"/api/projects/{pid}/epics/{eid}/run/stop")
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_stop_parked_awaiting_false_when_live_run(tmp_path: Path) -> None:
    """A live run must go through stop(), not the parked-clear path."""
    import asyncio
    from unittest.mock import MagicMock

    from yukar.models.run import RunState
    from yukar.runs.supervisor import RunSupervisor, _RunHandle
    from yukar.storage import state_repo

    root = str(tmp_path)
    pid, eid = "p", "EP-live"

    await state_repo.save_state(root, pid, eid, RunState(run_id="r", status="awaiting_input"))

    sup = RunSupervisor()
    task = asyncio.create_task(asyncio.sleep(100))
    sup._runs[sup._key(pid, eid)] = _RunHandle(
        run_id="r", runner=MagicMock(), task=task, root=root, project_id=pid, epic_id=eid
    )
    try:
        assert await sup.stop_parked_awaiting(root, pid, eid) is False
        # The live run's persisted state must be untouched by the parked path.
        saved = await state_repo.get_state(root, pid, eid)
        assert saved is not None
        assert saved.status == "awaiting_input"
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_stop_parked_awaiting_false_when_running_status(tmp_path: Path) -> None:
    """Only awaiting_input is cleared; a persisted 'running' with no live task is
    the domain of restart recovery, not the Stop button."""
    from yukar.models.run import RunState
    from yukar.runs.supervisor import RunSupervisor
    from yukar.storage import state_repo

    root = str(tmp_path)
    pid, eid = "p", "EP-run-status"
    await state_repo.save_state(root, pid, eid, RunState(run_id="r", status="running"))

    sup = RunSupervisor()
    assert await sup.stop_parked_awaiting(root, pid, eid) is False
