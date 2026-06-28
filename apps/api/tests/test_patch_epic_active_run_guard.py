"""finding[patch-epic-guard]: PATCH /epics/{epic_id} has no active-run guard,
allowing terminal status (closed/merged) to be written directly.

Under the same conditions where close_epic (POST /close) returns 409,
PATCH ?status=closed currently returns 200 (bug).

Test strategy:
- TestPatchEpicGuardCharacterization : characterize the current behavior (PASS)
- TestPatchEpicShouldReject409WhenRunActive : mark the desired behavior (return 409)
  with xfail(strict=True); once fixed it becomes xpass → strict forces marker removal
  as a safety net.
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
    status: str = "planned",
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
# Characterization: current PATCH returns 200 even while run is active (recording the bug)
# ---------------------------------------------------------------------------


class TestPatchEpicGuardCharacterization:
    """Regression tests verifying that PATCH /epics/{epic_id} has an active-run guard.

    The G6 fix added a supervisor guard to patch_epic so that attempting to set
    a terminal status (closed/merged) while a run is active returns 409.
    These tests remain as regression tests to pin that behavior.
    """

    async def test_patch_closed_while_run_active_currently_returns_200(
        self, app_client: Any, tmp_workspace: Path
    ) -> None:
        """After fix: PATCH status=closed returns 409 while run is active
        (same guard as close_epic).

        The same guard applied to close_epic (POST /close) is also applied to patch_epic;
        both return 409.
        """
        root = str(tmp_workspace)
        pid, eid = "proj", "EP-1"
        await _write_project_epic(root, pid, eid, status="in_progress")

        fake_task = _inject_fake_active_run(root, pid, eid)
        try:
            # close_epic should return 409
            close_resp = await app_client.post(f"/api/projects/{pid}/epics/{eid}/close")
            assert close_resp.status_code == 409, (
                f"close_epic should 409 with active run, got {close_resp.status_code}"
            )

            # PATCH should also return 409 (after G6 fix)
            patch_resp = await app_client.patch(
                f"/api/projects/{pid}/epics/{eid}",
                json={"status": "closed"},
            )
            assert patch_resp.status_code == 409, (
                "regression: PATCH status=closed with active run should return 409, "
                f"got {patch_resp.status_code}"
            )
        finally:
            await _cleanup_fake_run(fake_task, pid, eid)

    async def test_patch_merged_while_run_active_currently_returns_200(
        self, app_client: Any, tmp_workspace: Path
    ) -> None:
        """After fix: PATCH status=merged also returns 409 while run is active (after G6 fix)."""
        root = str(tmp_workspace)
        pid, eid = "proj", "EP-1"
        await _write_project_epic(root, pid, eid, status="in_progress")

        fake_task = _inject_fake_active_run(root, pid, eid)
        try:
            patch_resp = await app_client.patch(
                f"/api/projects/{pid}/epics/{eid}",
                json={"status": "merged"},
            )
            assert patch_resp.status_code == 409, (
                "regression: PATCH status=merged with active run should return 409, "
                f"got {patch_resp.status_code}"
            )
        finally:
            await _cleanup_fake_run(fake_task, pid, eid)


# ---------------------------------------------------------------------------
# xfail: desired behavior (terminal status should be 409 while run is active)
# ---------------------------------------------------------------------------


class TestPatchEpicShouldReject409WhenRunActive:
    """PATCH should return 409 while run is active when writing terminal status.

    Currently has no guard and returns 200 (bug). After the fix it becomes xpass,
    and strict=True causes the test suite to enforce marker removal.
    """

    async def test_patch_status_closed_should_be_409_when_run_active(
        self, app_client: Any, tmp_workspace: Path
    ) -> None:
        """PATCH status=closed should return 409 while run is active.

        Currently returns 200, so mark as xfail(strict=True).
        After fix: this test becomes xpass → strict enforces marker removal.
        """
        root = str(tmp_workspace)
        pid, eid = "proj", "EP-1"
        await _write_project_epic(root, pid, eid, status="in_progress")

        fake_task = _inject_fake_active_run(root, pid, eid)
        try:
            resp = await app_client.patch(
                f"/api/projects/{pid}/epics/{eid}",
                json={"status": "closed"},
            )
            # This passes after the fix. Currently returns 200, so xfail.
            assert resp.status_code == 409
            assert "run is active" in resp.json()["detail"].lower()
        finally:
            await _cleanup_fake_run(fake_task, pid, eid)

    async def test_patch_status_merged_should_be_409_when_run_active(
        self, app_client: Any, tmp_workspace: Path
    ) -> None:
        """PATCH status=merged should return 409 while run is active.

        Currently returns 200, so mark as xfail(strict=True).
        """
        root = str(tmp_workspace)
        pid, eid = "proj", "EP-1"
        await _write_project_epic(root, pid, eid, status="in_progress")

        fake_task = _inject_fake_active_run(root, pid, eid)
        try:
            resp = await app_client.patch(
                f"/api/projects/{pid}/epics/{eid}",
                json={"status": "merged"},
            )
            assert resp.status_code == 409
            assert "run is active" in resp.json()["detail"].lower()
        finally:
            await _cleanup_fake_run(fake_task, pid, eid)

    async def test_patch_closed_while_run_active_should_not_persist(
        self, app_client: Any, tmp_workspace: Path
    ) -> None:
        """PATCH status=closed while run is active must not modify epic.yaml.

        Currently has no guard so epic.yaml gets overwritten to "closed".
        After fix: rejected with 409 → epic.yaml stays "in_progress".
        """
        root = str(tmp_workspace)
        pid, eid = "proj", "EP-1"
        await _write_project_epic(root, pid, eid, status="in_progress")

        fake_task = _inject_fake_active_run(root, pid, eid)
        try:
            await app_client.patch(
                f"/api/projects/{pid}/epics/{eid}",
                json={"status": "closed"},
            )
            # After fix: PATCH is rejected with 409 so epic.yaml should be unchanged
            from yukar.storage.epic_repo import get_epic

            loaded = await get_epic(root, pid, eid)
            assert loaded is not None
            # Currently gets overwritten to "closed", so xfail
            assert loaded.status == "in_progress"
        finally:
            await _cleanup_fake_run(fake_task, pid, eid)


# ---------------------------------------------------------------------------
# Non-terminal status PATCH should pass even while run is active (no guard needed)
# ---------------------------------------------------------------------------


class TestPatchEpicNonTerminalStatusAllowedWhileRunning:
    """PATCH for non-terminal fields like title / description / acceptance_criteria / manager_effort
    should not be rejected while a run is running.
    This is a characterization of normal behavior.
    """

    async def test_patch_title_while_run_active_is_allowed(
        self, app_client: Any, tmp_workspace: Path
    ) -> None:
        root = str(tmp_workspace)
        pid, eid = "proj", "EP-1"
        await _write_project_epic(root, pid, eid, status="in_progress")

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

    async def test_patch_non_terminal_status_while_run_active_is_allowed(
        self, app_client: Any, tmp_workspace: Path
    ) -> None:
        """Status values like planned / in_progress / failed can be written
        even while run is active."""
        root = str(tmp_workspace)
        pid, eid = "proj", "EP-1"
        await _write_project_epic(root, pid, eid, status="in_progress")

        fake_task = _inject_fake_active_run(root, pid, eid)
        try:
            # non-terminal: writing back to planned needs no guard
            resp = await app_client.patch(
                f"/api/projects/{pid}/epics/{eid}",
                json={"status": "planned"},
            )
            assert resp.status_code == 200
        finally:
            await _cleanup_fake_run(fake_task, pid, eid)
