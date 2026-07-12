"""GET /run/state under lifecycle-redesign semantics: ``waiting`` is the single resting state.

Successor of the pending_question HTTP restore test: the question text now
lives in the conversation (the agent's last message), so the reload-restore
guarantee is just "the HTTP state says waiting" — the UI reads the question
from the thread itself.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest


async def _seed_project_epic(root: str, pid: str, eid: str) -> None:
    from yukar.models.epic import Epic
    from yukar.models.project import Project
    from yukar.storage.epic_repo import save_epic
    from yukar.storage.project_repo import save_project

    await save_project(root, Project(id=pid, name=pid, status="active", repos=[]))
    await save_epic(
        root,
        pid,
        Epic(id=eid, slug="s", title="t", description="d", branch="yukar/ep-1-s"),
    )


@pytest.mark.asyncio
async def test_get_run_state_synthesises_waiting_when_never_run(
    app_client: Any, tmp_workspace: Path
) -> None:
    """An epic that has never run is simply "your turn" — the synthesised
    default is waiting, not a separate idle state."""
    root = str(tmp_workspace)
    pid, eid = "proj", "EP-1"
    await _seed_project_epic(root, pid, eid)

    resp = await app_client.get(f"/api/projects/{pid}/epics/{eid}/run/state")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("status") == "waiting", body


@pytest.mark.asyncio
async def test_get_run_state_http_restores_waiting_after_restart(
    app_client: Any, tmp_workspace: Path
) -> None:
    """A parked run persisted on disk (including a LEGACY awaiting_input file
    with a pending_question key) is served as ``waiting`` over HTTP — this is
    the reload-restore path (REST, not SSE)."""
    from yukar.config import paths
    from yukar.models.run import RunState
    from yukar.storage import state_repo

    root = str(tmp_workspace)
    pid, eid = "proj", "EP-2"
    await _seed_project_epic(root, pid, eid)

    # Modern parked state.
    await state_repo.save_state(root, pid, eid, RunState(run_id="run-x", status="waiting"))
    resp = await app_client.get(f"/api/projects/{pid}/epics/{eid}/run/state")
    assert resp.status_code == 200, resp.text
    assert resp.json().get("status") == "waiting"

    # Legacy on-disk shape (pre-redesign restart snapshot): must not 500 and must
    # coerce to waiting; pending_question is gone from the response model.
    state_path = paths.state_yaml(root, pid, eid)
    state_path.write_text(
        "run_id: run-x\nstatus: awaiting_input\npending_question: 'Proceed?'\n"
    )
    resp = await app_client.get(f"/api/projects/{pid}/epics/{eid}/run/state")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("status") == "waiting", body
    assert "pending_question" not in body, body
