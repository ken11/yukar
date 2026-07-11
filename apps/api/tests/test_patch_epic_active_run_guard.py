"""PATCH /epics/{epic_id} active-run guard for the user's "completed" toggle.

Completing an epic (the single user-owned finish action) must not race an
in-flight run: PATCH {status: "completed"} returns 409 while a run is active
and leaves epic.yaml untouched.  Reopening ({status: "open"}) and non-status
edits (title etc.) need no guard.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Helpers (follows the style of test_epic_close.py)
# ---------------------------------------------------------------------------


async def _write_project_epic(
    root: str,
    project_id: str = "proj",
    epic_id: str = "EP-1",
    status: str = "open",
) -> None:
    from yukar.models.epic import Epic
    from yukar.models.project import Project
    from yukar.storage.epic_repo import save_epic
    from yukar.storage.project_repo import save_project

    await save_project(root, Project(id=project_id, name=project_id))
    epic = Epic.model_validate(
        {
            "id": epic_id,
            "slug": "test",
            "title": "Test",
            "branch": f"yukar/{epic_id.lower()}-test",
            "status": status,
        }
    )
    await save_epic(root, project_id, epic)


def _inject_fake_active_run(
    root: str,
    project_id: str,
    epic_id: str,
) -> asyncio.Task[None]:
    """Inject a task that never finishes into supervisor._runs.
    The caller must cancel the returned task when the test ends.
    """
    from unittest.mock import MagicMock

    from yukar.runs.supervisor import RunSupervisor, _RunHandle, get_supervisor

    sv: RunSupervisor = get_supervisor()

    async def _never() -> None:
        await asyncio.sleep(9999)

    fake_task: asyncio.Task[None] = asyncio.create_task(_never())
    sv._runs[(project_id, epic_id)] = _RunHandle(
        run_id="run-fake",
        runner=MagicMock(),
        task=fake_task,
        root=root,
        project_id=project_id,
        epic_id=epic_id,
    )
    return fake_task


async def _cleanup_fake_run(
    fake_task: asyncio.Task[None],
    project_id: str,
    epic_id: str,
) -> None:
    from yukar.runs.supervisor import get_supervisor

    fake_task.cancel()
    with contextlib.suppress(Exception, asyncio.CancelledError):
        await fake_task
    get_supervisor()._runs.pop((project_id, epic_id), None)


# ---------------------------------------------------------------------------
# The guard: completed-toggle is rejected while a run is active
# ---------------------------------------------------------------------------


class TestPatchCompletedRejectedWhileRunActive:
    async def test_patch_completed_returns_409_when_run_active(
        self, app_client: Any, tmp_workspace: Path
    ) -> None:
        root = str(tmp_workspace)
        pid, eid = "proj", "EP-1"
        await _write_project_epic(root, pid, eid)

        fake_task = _inject_fake_active_run(root, pid, eid)
        try:
            resp = await app_client.patch(
                f"/api/projects/{pid}/epics/{eid}",
                json={"status": "completed"},
            )
            assert resp.status_code == 409
            assert "run is active" in resp.json()["detail"].lower()
        finally:
            await _cleanup_fake_run(fake_task, pid, eid)

    async def test_patch_completed_while_run_active_does_not_persist(
        self, app_client: Any, tmp_workspace: Path
    ) -> None:
        """The rejected PATCH must not modify epic.yaml."""
        root = str(tmp_workspace)
        pid, eid = "proj", "EP-1"
        await _write_project_epic(root, pid, eid)

        fake_task = _inject_fake_active_run(root, pid, eid)
        try:
            await app_client.patch(
                f"/api/projects/{pid}/epics/{eid}",
                json={"status": "completed"},
            )
            from yukar.storage.epic_repo import get_epic

            loaded = await get_epic(root, pid, eid)
            assert loaded is not None
            assert loaded.status == "open"
        finally:
            await _cleanup_fake_run(fake_task, pid, eid)


# ---------------------------------------------------------------------------
# No guard for reopen / non-status edits
# ---------------------------------------------------------------------------


class TestPatchAllowedWhileRunActive:
    async def test_patch_title_while_run_active_is_allowed(
        self, app_client: Any, tmp_workspace: Path
    ) -> None:
        root = str(tmp_workspace)
        pid, eid = "proj", "EP-1"
        await _write_project_epic(root, pid, eid)

        fake_task = _inject_fake_active_run(root, pid, eid)
        try:
            resp = await app_client.patch(
                f"/api/projects/{pid}/epics/{eid}",
                json={"title": "Updated Title"},
            )
            assert resp.status_code == 200
            assert resp.json()["title"] == "Updated Title"
        finally:
            await _cleanup_fake_run(fake_task, pid, eid)

    async def test_patch_open_while_run_active_is_allowed(
        self, app_client: Any, tmp_workspace: Path
    ) -> None:
        """Reopening needs no guard — it only widens what is allowed."""
        root = str(tmp_workspace)
        pid, eid = "proj", "EP-1"
        await _write_project_epic(root, pid, eid)

        fake_task = _inject_fake_active_run(root, pid, eid)
        try:
            resp = await app_client.patch(
                f"/api/projects/{pid}/epics/{eid}",
                json={"status": "open"},
            )
            assert resp.status_code == 200
        finally:
            await _cleanup_fake_run(fake_task, pid, eid)
