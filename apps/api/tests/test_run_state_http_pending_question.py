"""Decisive backend-vs-frontend split: does GET /run/state actually return
pending_question over HTTP after a restart (awaiting_input persisted on disk)?
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest


@pytest.mark.asyncio
async def test_get_run_state_http_includes_pending_question(
    app_client: Any, tmp_workspace: Path
) -> None:
    from yukar.models.epic import Epic
    from yukar.models.project import Project
    from yukar.models.run import RunState
    from yukar.storage import state_repo
    from yukar.storage.epic_repo import save_epic
    from yukar.storage.project_repo import save_project

    root = str(tmp_workspace)
    pid, eid = "proj", "EP-1"

    await save_project(root, Project(id=pid, name=pid, status="active", repos=[]))
    await save_epic(
        root,
        pid,
        Epic(id=eid, slug="s", title="t", description="d", branch="yukar/ep-1-s"),
    )

    # Simulate the post-restart on-disk state written by ask_user.
    await state_repo.save_state(
        root,
        pid,
        eid,
        RunState(
            run_id="run-x",
            status="awaiting_input",
            pending_question="Is it OK to proceed with this plan?",
        ),
    )

    resp = await app_client.get(f"/api/projects/{pid}/epics/{eid}/run/state")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # The decisive assertion: the HTTP JSON must carry pending_question.
    assert body.get("status") == "awaiting_input", body
    assert body.get("pending_question") == "Is it OK to proceed with this plan?", body
