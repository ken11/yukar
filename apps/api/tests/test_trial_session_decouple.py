"""Trial / conversation-session decoupling (案A).

Design: a *trial* is a (branch + worktree) line of work; a *conversation
session* (fresh context + role) attaches to a trial.  The worktree is keyed by
the trial, not by the conversation thread id, so multiple manager conversations
on the same branch share one worktree.

Phase 0 (this batch): introduce ``ThreadEntry.trial_id`` and route worktree
resolution through it, staying fully backward compatible (``trial_id`` defaults
to the thread's own id, so existing single-conversation trials are unchanged).

Phase 1: the "same-branch, fresh context" manager session (added later).
Phase 2: the Reviewer role (added later).
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient


async def _setup_project_and_epic(client: AsyncClient, project_id: str) -> str:
    r = await client.post(
        "/api/projects",
        json={"id": project_id, "name": project_id, "repos": []},
    )
    assert r.status_code == 201, r.text
    r2 = await client.post(
        f"/api/projects/{project_id}/epics",
        json={"title": "Test Epic", "description": ""},
    )
    assert r2.status_code == 201, r2.text
    return r2.json()["id"]


# ---------------------------------------------------------------------------
# Phase 0.1 — model field
# ---------------------------------------------------------------------------


class TestThreadEntryTrialId:
    def test_trial_id_defaults_to_none(self) -> None:
        from yukar.models.thread import ThreadEntry

        entry = ThreadEntry(id="th-abc", title="t", role="manager")  # type: ignore[arg-type]
        assert entry.trial_id is None

    def test_trial_id_of_helper_falls_back_to_id(self) -> None:
        """trial_id_of(entry) returns entry.trial_id when set, else entry.id."""
        from yukar.agents.trials import trial_id_of
        from yukar.models.thread import ThreadEntry

        legacy = ThreadEntry(id="th-legacy", title="t", role="manager")  # type: ignore[arg-type]
        assert trial_id_of(legacy) == "th-legacy"

        shared = ThreadEntry(
            id="th-second",
            title="t",
            role="manager",  # type: ignore[arg-type]
            trial_id="th-first",
        )
        assert trial_id_of(shared) == "th-first"


# ---------------------------------------------------------------------------
# Phase 0.2 — create_thread stamps trial_id = own id for a fresh trial
# ---------------------------------------------------------------------------


class TestCreateStampsTrialId:
    @pytest.mark.asyncio
    async def test_new_manager_trial_trial_id_equals_own_id(
        self, app_client: AsyncClient
    ) -> None:
        epic_id = await _setup_project_and_epic(app_client, "trial-stamp-proj")
        r = await app_client.post(
            f"/api/projects/trial-stamp-proj/epics/{epic_id}/threads",
            json={"title": "Trial 1", "role": "manager"},
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["trial_id"] == body["id"], (
            "A fresh manager trial must anchor its own trial_id to its thread id "
            "so the worktree path is backward-compatible."
        )


# ---------------------------------------------------------------------------
# Phase 0.3 — resolve_active_trial_id returns the trial_id (worktree key)
# ---------------------------------------------------------------------------


class TestResolveReturnsTrialId:
    @pytest.mark.asyncio
    async def test_returns_trial_id_when_entry_has_one(self, tmp_path: Any) -> None:
        """A continuation conversation (thread id != trial id) must resolve to the
        *trial* id so its worktree is shared with the trial it continues."""
        from yukar.agents.trials import resolve_active_trial_id
        from yukar.models.epic import Epic
        from yukar.models.project import Project
        from yukar.models.thread import ThreadEntry, ThreadsFile
        from yukar.storage import threads_repo
        from yukar.storage.epic_repo import save_epic
        from yukar.storage.project_repo import save_project

        root = str(tmp_path / "ws")
        pid, eid = "resolve-trial-proj", "EP-rt"
        await save_project(root, Project(id=pid, name=pid))
        # active conversation is th-second, but it continues trial th-first.
        active = ThreadEntry(
            id="th-second",
            title="Continue",
            role="manager",  # type: ignore[arg-type]
            trial_id="th-first",
            branch="yukar/ep-rt-slug",
        )
        await threads_repo.save_threads(root, pid, eid, ThreadsFile(threads=[active]))
        epic = Epic(
            id=eid,
            slug="slug",
            title="T",
            branch="yukar/ep-rt-slug",
            active_thread_id="th-second",
        )
        await save_epic(root, pid, epic)

        result = await resolve_active_trial_id(root, pid, eid, epic)
        assert result == "th-first", (
            "Expected the trial id (th-first), not the conversation thread id (th-second)."
        )

    @pytest.mark.asyncio
    async def test_backward_compat_no_entry_returns_active_thread_id(
        self, tmp_path: Any
    ) -> None:
        """When active_thread_id is set but no ThreadEntry is registered yet
        (legacy lazy registration), fall back to the active_thread_id itself."""
        from yukar.agents.trials import resolve_active_trial_id
        from yukar.models.epic import Epic

        root = str(tmp_path / "ws")
        epic = Epic(
            id="EP-1",
            slug="slug",
            title="T",
            branch="yukar/ep-1-slug",
            active_thread_id="th-explicit",
        )
        result = await resolve_active_trial_id(root, "proj", "EP-1", epic)
        assert result == "th-explicit"


# ---------------------------------------------------------------------------
# Phase 1 — same-branch, fresh-context manager session
# ---------------------------------------------------------------------------


def _root_of(client: AsyncClient) -> str:
    return client._transport.app.state.settings.workspace_root  # type: ignore[attr-defined,union-attr]  # ty: ignore[unresolved-attribute]


async def _create_first_trial(client: AsyncClient, project_id: str) -> tuple[str, dict[str, Any]]:
    """Create a project+epic and the first manager trial. Returns (epic_id, trial_json)."""
    epic_id = await _setup_project_and_epic(client, project_id)
    r = await client.post(
        f"/api/projects/{project_id}/epics/{epic_id}/threads",
        json={"title": "Trial 1", "role": "manager"},
    )
    assert r.status_code == 201, r.text
    return epic_id, r.json()


class TestSameBranchSession:
    @pytest.mark.asyncio
    async def test_continuation_shares_trial_and_branch(self, app_client: AsyncClient) -> None:
        pid = "sb-proj"
        epic_id, first = await _create_first_trial(app_client, pid)

        r = await app_client.post(
            f"/api/projects/{pid}/epics/{epic_id}/threads",
            json={"title": "Continue", "role": "manager", "same_branch": True},
        )
        assert r.status_code == 201, r.text
        cont = r.json()

        assert cont["id"] != first["id"], "continuation is a new conversation thread"
        assert cont["trial_id"] == first["trial_id"], (
            "continuation must inherit the trial so it shares the worktree"
        )
        assert cont["branch"] == first["branch"], "same branch — no ordinal suffix"

        from yukar.storage import threads_repo
        from yukar.storage.epic_repo import get_epic

        root = _root_of(app_client)
        tf = await threads_repo.get_threads(root, pid, epic_id)
        old = next(t for t in tf.threads if t.id == first["id"])
        assert old.status == "archived", "previous conversation is archived (kept as history)"

        epic = await get_epic(root, pid, epic_id)
        assert epic is not None
        assert epic.active_thread_id == cont["id"]
        assert epic.branch == first["branch"], "epic.branch is unchanged (no new trial)"

    @pytest.mark.asyncio
    async def test_continuation_has_empty_message_history(self, app_client: AsyncClient) -> None:
        pid = "sb-fresh-proj"
        epic_id, _first = await _create_first_trial(app_client, pid)
        r = await app_client.post(
            f"/api/projects/{pid}/epics/{epic_id}/threads",
            json={"title": "Continue", "role": "manager", "same_branch": True},
        )
        assert r.status_code == 201, r.text
        cont_id = r.json()["id"]
        msgs = await app_client.get(f"/api/projects/{pid}/epics/{epic_id}/threads/{cont_id}")
        assert msgs.status_code == 200, msgs.text
        assert msgs.json() == [], "fresh context — no inherited conversation"

    @pytest.mark.asyncio
    async def test_continuation_preserves_worktree(self, app_client: AsyncClient) -> None:
        """The trial's worktree must NOT be removed when a same-branch continuation
        archives the previous conversation."""
        from pathlib import Path

        from yukar.config import paths as p
        from yukar.storage.epic_repo import get_epic, save_epic

        pid = "sb-wt-proj"
        epic_id, first = await _create_first_trial(app_client, pid)
        root = _root_of(app_client)

        # Simulate a created worktree for the trial.
        epic = await get_epic(root, pid, epic_id)
        assert epic is not None
        epic.touched_repos = ["repoA"]
        await save_epic(root, pid, epic)
        wt = p.worktree_dir(root, pid, epic_id, first["trial_id"], "repoA")
        wt.mkdir(parents=True, exist_ok=True)
        (wt / "sentinel.txt").write_text("keep me", encoding="utf-8")

        r = await app_client.post(
            f"/api/projects/{pid}/epics/{epic_id}/threads",
            json={"title": "Continue", "role": "manager", "same_branch": True},
        )
        assert r.status_code == 201, r.text

        assert Path(wt).exists(), "worktree must be preserved for the continuing trial"
        assert (wt / "sentinel.txt").exists(), "worktree contents must be preserved"

    @pytest.mark.asyncio
    async def test_same_branch_requires_active_trial(self, app_client: AsyncClient) -> None:
        """same_branch with no manager trial to continue → 409."""
        pid = "sb-none-proj"
        epic_id = await _setup_project_and_epic(app_client, pid)
        r = await app_client.post(
            f"/api/projects/{pid}/epics/{epic_id}/threads",
            json={"title": "Continue", "role": "manager", "same_branch": True},
        )
        assert r.status_code == 409, r.text

    @pytest.mark.asyncio
    async def test_same_branch_with_active_run_returns_409(self, app_client: AsyncClient) -> None:
        """same_branch while the trial's run is active → 409 (must stop first)."""
        from unittest.mock import MagicMock

        from yukar.runs.supervisor import _RunHandle, get_supervisor

        pid = "sb-run-proj"
        epic_id, first = await _create_first_trial(app_client, pid)

        sup = get_supervisor()
        handle = MagicMock(spec=_RunHandle)
        handle.task = MagicMock()
        handle.task.done.return_value = False
        handle.manager_thread_id = first["id"]
        sup._runs[sup._key(pid, epic_id)] = handle
        try:
            r = await app_client.post(
                f"/api/projects/{pid}/epics/{epic_id}/threads",
                json={"title": "Continue", "role": "manager", "same_branch": True},
            )
            assert r.status_code == 409, r.text
        finally:
            sup._runs.pop(sup._key(pid, epic_id), None)

    @pytest.mark.asyncio
    async def test_same_branch_rejected_for_non_manager(self, app_client: AsyncClient) -> None:
        pid = "sb-role-proj"
        epic_id, _first = await _create_first_trial(app_client, pid)
        r = await app_client.post(
            f"/api/projects/{pid}/epics/{epic_id}/threads",
            json={"title": "x", "role": "user", "same_branch": True},
        )
        assert r.status_code == 422, r.text


class TestArchiveSharedTrialGuards:
    """Archiving a stale sibling conversation must NOT tear down a trial that a
    still-active conversation shares (regression from the trial/session decouple)."""

    @pytest.mark.asyncio
    async def test_archiving_stale_sibling_preserves_shared_worktree_and_active(
        self, app_client: AsyncClient
    ) -> None:
        from pathlib import Path

        from yukar.config import paths as p
        from yukar.storage.epic_repo import get_epic, save_epic

        pid = "arch-share-proj"
        epic_id, first = await _create_first_trial(app_client, pid)
        root = _root_of(app_client)

        # Simulate a worktree for the trial.
        epic = await get_epic(root, pid, epic_id)
        assert epic is not None
        epic.touched_repos = ["repoA"]
        await save_epic(root, pid, epic)
        wt = p.worktree_dir(root, pid, epic_id, first["trial_id"], "repoA")
        wt.mkdir(parents=True, exist_ok=True)
        (wt / "sentinel.txt").write_text("live", encoding="utf-8")

        # same_branch continuation: old conversation archived, new one shares the trial.
        r = await app_client.post(
            f"/api/projects/{pid}/epics/{epic_id}/threads",
            json={"title": "Continue", "role": "manager", "same_branch": True},
        )
        assert r.status_code == 201, r.text
        cont_id = r.json()["id"]

        # Archiving the stale previous conversation must NOT delete the shared worktree
        # nor clear active_thread_id (the continuation still owns the trial).
        ar = await app_client.post(
            f"/api/projects/{pid}/epics/{epic_id}/threads/{first['id']}/archive",
        )
        assert ar.status_code == 200, ar.text

        assert Path(wt).exists(), "shared worktree must survive archiving a stale sibling"
        assert (wt / "sentinel.txt").exists()
        epic2 = await get_epic(root, pid, epic_id)
        assert epic2 is not None
        assert epic2.active_thread_id == cont_id, "active trial must not be orphaned"

    @pytest.mark.asyncio
    async def test_archiving_non_active_manager_keeps_active_thread_id(
        self, app_client: AsyncClient
    ) -> None:
        from yukar.models.thread import ThreadEntry, ThreadsFile
        from yukar.storage import threads_repo
        from yukar.storage.epic_repo import get_epic, save_epic

        pid = "arch-nonactive-proj"
        epic_id = await _setup_project_and_epic(app_client, pid)
        root = _root_of(app_client)

        # A resolved (non-archived) manager entry coexists with a separate active trial.
        resolved = ThreadEntry(
            id="th-resolved",
            title="Trial 1",
            role="manager",  # type: ignore[arg-type]
            status="resolved",  # type: ignore[arg-type]
            trial_id="th-resolved",
            branch="yukar/ep-x-1",
        )
        active = ThreadEntry(
            id="th-active",
            title="Trial 2",
            role="manager",  # type: ignore[arg-type]
            status="active",
            trial_id="th-active",
            branch="yukar/ep-x-2",
        )
        await threads_repo.save_threads(
            root, pid, epic_id, ThreadsFile(threads=[resolved, active])
        )
        epic = await get_epic(root, pid, epic_id)
        assert epic is not None
        epic.active_thread_id = "th-active"
        await save_epic(root, pid, epic)

        ar = await app_client.post(
            f"/api/projects/{pid}/epics/{epic_id}/threads/th-resolved/archive",
        )
        assert ar.status_code == 200, ar.text

        epic2 = await get_epic(root, pid, epic_id)
        assert epic2 is not None
        assert epic2.active_thread_id == "th-active", (
            "archiving a non-active entry must not orphan the active trial"
        )
