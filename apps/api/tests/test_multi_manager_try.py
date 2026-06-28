"""Tests for multi-manager-try feature.

Verifies:
1. Create gate 409: POST /threads (role=manager) with an already-active trial → 409.
2. Archive flow: POST /threads/{thread_id}/archive → archived + active_thread_id cleared.
3. Conflicting run 409: start_or_inject with a different manager_thread_id → RuntimeError.
4. Worktree path: paths.worktree_dir includes manager_thread_id.
5. _is_active_manager_thread routing: returns True only for the active trial.
6. Archived POST rejection: posting to an archived thread returns 403.
12. POST /run resolves epic.active_thread_id before calling supervisor.start.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from httpx import AsyncClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _setup_project_and_epic(
    client: AsyncClient,
    project_id: str,
    project_name: str,
) -> str:
    """Create project (no repos) and one epic. Returns epic_id."""
    r = await client.post(
        "/api/projects",
        json={"id": project_id, "name": project_name, "repos": []},
    )
    assert r.status_code == 201, r.text

    r2 = await client.post(
        f"/api/projects/{project_id}/epics",
        json={"title": "Test Epic", "description": ""},
    )
    assert r2.status_code == 201, r2.text
    return r2.json()["id"]


# ---------------------------------------------------------------------------
# Test 1: Create gate 409
# ---------------------------------------------------------------------------


class TestCreateManagerGate:
    """POST /threads with role=manager returns 409 when another trial is active."""

    @pytest.mark.asyncio
    async def test_409_when_active_trial_exists(self, app_client: AsyncClient) -> None:
        """Creating a second manager trial while one is active returns 409."""
        epic_id = await _setup_project_and_epic(app_client, "gate-proj", "Gate Project")

        # Create the first manager thread (should succeed).
        r3 = await app_client.post(
            f"/api/projects/gate-proj/epics/{epic_id}/threads",
            json={"title": "Manager Trial 1", "role": "manager"},
        )
        assert r3.status_code == 201, r3.text
        first_thread_id = r3.json()["id"]
        assert r3.json()["role"] == "manager"
        assert r3.json()["status"] == "active"

        # Attempt to create a second manager thread while the first is active → 409.
        r4 = await app_client.post(
            f"/api/projects/gate-proj/epics/{epic_id}/threads",
            json={"title": "Manager Trial 2", "role": "manager"},
        )
        assert r4.status_code == 409, r4.text
        detail = r4.json()["detail"].lower()
        assert "active" in detail or first_thread_id in detail

    @pytest.mark.asyncio
    async def test_non_manager_creation_unaffected(self, app_client: AsyncClient) -> None:
        """Creating a user thread is never blocked by manager gate."""
        epic_id = await _setup_project_and_epic(app_client, "nogate-proj", "NoGate Project")

        # Create user thread — should succeed even without touching manager.
        r3 = await app_client.post(
            f"/api/projects/nogate-proj/epics/{epic_id}/threads",
            json={"title": "User Chat", "role": "user"},
        )
        assert r3.status_code == 201, r3.text

        # Create another user thread — also succeeds.
        r4 = await app_client.post(
            f"/api/projects/nogate-proj/epics/{epic_id}/threads",
            json={"title": "Another User Chat", "role": "user"},
        )
        assert r4.status_code == 201, r4.text

    @pytest.mark.asyncio
    async def test_manager_thread_has_branch_set(self, app_client: AsyncClient) -> None:
        """A created manager thread has branch set to epic.branch."""
        epic_id = await _setup_project_and_epic(app_client, "branch-proj", "Branch Project")

        r = await app_client.post(
            f"/api/projects/branch-proj/epics/{epic_id}/threads",
            json={"title": "Manager with Branch", "role": "manager"},
        )
        assert r.status_code == 201, r.text
        data = r.json()
        # branch should be set (epic.branch is derived from epic id + slug).
        assert data.get("branch") is not None
        assert data["role"] == "manager"

    @pytest.mark.asyncio
    async def test_epic_active_thread_id_set_on_manager_creation(
        self, app_client: AsyncClient
    ) -> None:
        """epic.active_thread_id is set when a manager thread is created."""
        epic_id = await _setup_project_and_epic(app_client, "atid-proj", "ATID Project")

        r = await app_client.post(
            f"/api/projects/atid-proj/epics/{epic_id}/threads",
            json={"title": "Trial", "role": "manager"},
        )
        assert r.status_code == 201, r.text
        thread_id = r.json()["id"]

        root = app_client._transport.app.state.settings.workspace_root  # type: ignore[attr-defined]  # ty: ignore[unresolved-attribute]
        from yukar.storage.epic_repo import get_epic

        epic = await get_epic(root, "atid-proj", epic_id)
        assert epic is not None
        assert epic.active_thread_id == thread_id


# ---------------------------------------------------------------------------
# Test 2: Archive flow
# ---------------------------------------------------------------------------


class TestArchiveFlow:
    """POST /threads/{thread_id}/archive sets status=archived and clears active_thread_id."""

    @pytest.mark.asyncio
    async def test_archive_clears_active_thread_id(self, app_client: AsyncClient) -> None:
        """Archive sets status=archived and epic.active_thread_id becomes None."""
        epic_id = await _setup_project_and_epic(app_client, "arch-proj", "Archive Project")

        # Create a manager trial.
        r3 = await app_client.post(
            f"/api/projects/arch-proj/epics/{epic_id}/threads",
            json={"title": "Trial 1", "role": "manager"},
        )
        assert r3.status_code == 201, r3.text
        thread_id = r3.json()["id"]

        root = app_client._transport.app.state.settings.workspace_root  # type: ignore[attr-defined]  # ty: ignore[unresolved-attribute]
        from yukar.storage.epic_repo import get_epic

        # Verify epic.active_thread_id is set.
        epic_before = await get_epic(root, "arch-proj", epic_id)
        assert epic_before is not None
        assert epic_before.active_thread_id == thread_id

        # Archive the trial.
        r4 = await app_client.post(
            f"/api/projects/arch-proj/epics/{epic_id}/threads/{thread_id}/archive",
        )
        assert r4.status_code == 200, r4.text
        assert r4.json()["status"] == "archived"
        assert r4.json()["thread_id"] == thread_id

        # Verify thread status is archived.
        from yukar.storage import threads_repo

        tf = await threads_repo.get_threads(root, "arch-proj", epic_id)
        archived = next((t for t in tf.threads if t.id == thread_id), None)
        assert archived is not None
        assert archived.status == "archived"

        # Verify epic.active_thread_id is cleared.
        epic_after = await get_epic(root, "arch-proj", epic_id)
        assert epic_after is not None
        assert epic_after.active_thread_id is None

    @pytest.mark.asyncio
    async def test_archive_enables_new_trial(self, app_client: AsyncClient) -> None:
        """After archiving, a new manager trial can be created."""
        epic_id = await _setup_project_and_epic(app_client, "arch-seq-proj", "ArchSeq Project")

        r3 = await app_client.post(
            f"/api/projects/arch-seq-proj/epics/{epic_id}/threads",
            json={"title": "Trial 1", "role": "manager"},
        )
        thread_id = r3.json()["id"]

        # Archive it.
        r4 = await app_client.post(
            f"/api/projects/arch-seq-proj/epics/{epic_id}/threads/{thread_id}/archive",
        )
        assert r4.status_code == 200

        # Now we can create a second manager trial.
        r5 = await app_client.post(
            f"/api/projects/arch-seq-proj/epics/{epic_id}/threads",
            json={"title": "Trial 2", "role": "manager"},
        )
        assert r5.status_code == 201, r5.text
        assert r5.json()["status"] == "active"

    @pytest.mark.asyncio
    async def test_archive_non_manager_returns_400(self, app_client: AsyncClient) -> None:
        """Archiving a non-manager thread returns 400."""
        epic_id = await _setup_project_and_epic(app_client, "arch-400-proj", "Arch400 Project")

        r3 = await app_client.post(
            f"/api/projects/arch-400-proj/epics/{epic_id}/threads",
            json={"title": "User Thread", "role": "user"},
        )
        assert r3.status_code == 201, r3.text
        thread_id = r3.json()["id"]

        r4 = await app_client.post(
            f"/api/projects/arch-400-proj/epics/{epic_id}/threads/{thread_id}/archive",
        )
        assert r4.status_code == 400, r4.text

    @pytest.mark.asyncio
    async def test_archive_not_found_returns_404(self, app_client: AsyncClient) -> None:
        """Archiving a non-existent thread returns 404."""
        epic_id = await _setup_project_and_epic(app_client, "arch-404-proj", "Arch404 Project")

        r = await app_client.post(
            f"/api/projects/arch-404-proj/epics/{epic_id}/threads/nonexistent/archive",
        )
        assert r.status_code == 404, r.text


# ---------------------------------------------------------------------------
# Test 3: Conflicting run 409
# ---------------------------------------------------------------------------


class TestConflictingRun:
    """start_or_inject with a different manager_thread_id raises RuntimeError."""

    @pytest.mark.asyncio
    async def test_different_trial_raises_runtime_error(self) -> None:
        """When a run for trial-A is active, messaging trial-B raises RuntimeError."""
        from yukar.runs.supervisor import RunSupervisor

        sup = RunSupervisor()

        # Simulate an active run for "manager-2".
        mock_runner = MagicMock()
        mock_runner.inject_message = MagicMock()
        mock_task = MagicMock()
        mock_task.done.return_value = False

        from yukar.runs.supervisor import _RunHandle

        handle = _RunHandle(
            run_id="run-abc",
            runner=mock_runner,
            task=mock_task,
            root="/tmp",
            project_id="p",
            epic_id="EP-1",
            manager_thread_id="manager-2",
        )
        sup._runs[("p", "EP-1")] = handle

        # Injecting to "manager" (different trial) should raise RuntimeError.
        with pytest.raises(RuntimeError, match="manager-2"):
            await sup.start_or_inject("/tmp", "p", "EP-1", "manager", "hello")

    @pytest.mark.asyncio
    async def test_same_trial_injects_ok(self) -> None:
        """When the active run is for the same trial, injection succeeds."""
        from yukar.runs.supervisor import RunSupervisor

        sup = RunSupervisor()

        inject_calls: list[tuple[str, str, str, str]] = []

        mock_runner = MagicMock()
        mock_runner.inject_message = lambda tid, text: inject_calls.append(("p", "EP-1", tid, text))
        mock_task = MagicMock()
        mock_task.done.return_value = False

        from yukar.runs.supervisor import _RunHandle

        handle = _RunHandle(
            run_id="run-abc",
            runner=mock_runner,
            task=mock_task,
            root="/tmp",
            project_id="p",
            epic_id="EP-1",
            manager_thread_id="manager",
        )
        sup._runs[("p", "EP-1")] = handle

        # Injection for the same trial should succeed.
        result = await sup.start_or_inject("/tmp", "p", "EP-1", "manager", "hello")
        assert result is True
        assert len(inject_calls) == 1

    @pytest.mark.asyncio
    async def test_start_with_different_active_trial_raises(self) -> None:
        """supervisor.start() for a different manager trial raises RuntimeError."""
        from yukar.runs.supervisor import RunSupervisor

        sup = RunSupervisor()

        mock_runner = MagicMock()
        mock_task = MagicMock()
        mock_task.done.return_value = False

        from yukar.runs.supervisor import _RunHandle

        handle = _RunHandle(
            run_id="run-xyz",
            runner=mock_runner,
            task=mock_task,
            root="/tmp",
            project_id="p",
            epic_id="EP-2",
            manager_thread_id="manager-3",
        )
        sup._runs[("p", "EP-2")] = handle

        with pytest.raises(RuntimeError, match="manager-3"):
            await sup.start("/tmp", "p", "EP-2", manager_thread_id="manager-4")


# ---------------------------------------------------------------------------
# Test 4: Worktree path includes manager_thread_id
# ---------------------------------------------------------------------------


class TestWorktreePath:
    """paths.worktree_dir must include manager_thread_id as a path segment."""

    def test_default_manager_path(self, tmp_path: Path) -> None:
        from yukar.config import paths

        root = str(tmp_path)
        p = paths.worktree_dir(root, "proj", "EP-1", "manager", "my-repo")
        # Should be: {root}/proj/epics/EP-1/worktrees/manager/my-repo
        assert p == tmp_path / "proj" / "epics" / "EP-1" / "worktrees" / "manager" / "my-repo"

    def test_custom_trial_path(self, tmp_path: Path) -> None:
        from yukar.config import paths

        root = str(tmp_path)
        p = paths.worktree_dir(root, "proj", "EP-1", "manager-2", "my-repo")
        assert p == tmp_path / "proj" / "epics" / "EP-1" / "worktrees" / "manager-2" / "my-repo"

    def test_different_trials_have_different_paths(self, tmp_path: Path) -> None:
        from yukar.config import paths

        root = str(tmp_path)
        p1 = paths.worktree_dir(root, "proj", "EP-1", "manager", "repo")
        p2 = paths.worktree_dir(root, "proj", "EP-1", "manager-2", "repo")
        assert p1 != p2

    def test_manager_worktrees_dir(self, tmp_path: Path) -> None:
        from yukar.config import paths

        root = str(tmp_path)
        d = paths.manager_worktrees_dir(root, "proj", "EP-1", "manager-2")
        assert d == tmp_path / "proj" / "epics" / "EP-1" / "worktrees" / "manager-2"

    def test_path_segment_validation_rejects_slash(self, tmp_path: Path) -> None:
        from yukar.config.paths import PathSegmentError, worktree_dir

        root = str(tmp_path)
        with pytest.raises(PathSegmentError):
            worktree_dir(root, "proj", "EP-1", "manager/../evil", "repo")


# ---------------------------------------------------------------------------
# Test 5: _is_active_manager_thread routing
# ---------------------------------------------------------------------------


class TestIsActiveManagerThread:
    """_is_active_manager_thread returns True only for the correct active trial."""

    def _make_epic(self, active_thread_id: str | None = None) -> Any:
        from yukar.models.epic import Epic

        return Epic(
            id="EP-1",
            slug="slug",
            title="T",
            branch="yukar/ep-1-slug",
            active_thread_id=active_thread_id,
        )

    def _make_tf(
        self,
        entries: list[tuple[str, str, str]],
    ) -> Any:
        from yukar.models.thread import ThreadEntry, ThreadsFile

        threads = [
            ThreadEntry(
                id=tid,
                title="T",
                role=role,  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
                status=status,  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
            )
            for tid, role, status in entries
        ]
        return ThreadsFile(threads=threads)

    def test_default_fallback_no_active_thread_id(self) -> None:
        from yukar.api.routers.threads import _is_active_manager_thread

        epic = self._make_epic(active_thread_id=None)
        tf = self._make_tf([("manager", "manager", "active")])
        # active_thread_id=None → "manager" is the fallback active id.
        assert _is_active_manager_thread(epic, tf, "manager") is True
        assert _is_active_manager_thread(epic, tf, "manager-2") is False

    def test_explicit_active_thread_id(self) -> None:
        from yukar.api.routers.threads import _is_active_manager_thread

        epic = self._make_epic(active_thread_id="th-abc123")
        tf = self._make_tf(
            [
                ("manager", "manager", "archived"),
                ("th-abc123", "manager", "active"),
            ]
        )
        assert _is_active_manager_thread(epic, tf, "th-abc123") is True
        assert _is_active_manager_thread(epic, tf, "manager") is False

    def test_archived_trial_returns_false(self) -> None:
        from yukar.api.routers.threads import _is_active_manager_thread

        epic = self._make_epic(active_thread_id=None)
        tf = self._make_tf([("manager", "manager", "archived")])
        # archived status → not active.
        assert _is_active_manager_thread(epic, tf, "manager") is False

    def test_no_thread_entry_but_fallback_manager(self) -> None:
        from yukar.api.routers.threads import _is_active_manager_thread

        # No ThreadEntry yet — orchestrator hasn't registered it yet.
        epic = self._make_epic(active_thread_id=None)
        tf = self._make_tf([])
        # Backward compat: "manager" without an entry is treated as active.
        assert _is_active_manager_thread(epic, tf, "manager") is True
        # Unknown ids without entries → False.
        assert _is_active_manager_thread(epic, tf, "unknown") is False

    def test_worker_thread_returns_false(self) -> None:
        from yukar.api.routers.threads import _is_active_manager_thread

        epic = self._make_epic(active_thread_id=None)
        tf = self._make_tf(
            [
                ("manager", "manager", "active"),
                ("worker-1", "worker", "active"),
            ]
        )
        # Worker thread is not a manager trial.
        assert _is_active_manager_thread(epic, tf, "worker-1") is False


# ---------------------------------------------------------------------------
# Test 6: Archived thread POST rejection (403)
# ---------------------------------------------------------------------------


class TestArchivedThreadRejection:
    """POST /threads/{thread_id}/messages to an archived thread returns 403."""

    @pytest.mark.asyncio
    async def test_post_to_archived_thread_returns_403(self, app_client: AsyncClient) -> None:
        """Posting a message to an archived manager thread returns 403."""
        epic_id = await _setup_project_and_epic(app_client, "arc-rej-proj", "ArcRej Project")

        # Create a manager trial.
        r3 = await app_client.post(
            f"/api/projects/arc-rej-proj/epics/{epic_id}/threads",
            json={"title": "Trial", "role": "manager"},
        )
        assert r3.status_code == 201, r3.text
        thread_id = r3.json()["id"]

        # Archive it.
        r4 = await app_client.post(
            f"/api/projects/arc-rej-proj/epics/{epic_id}/threads/{thread_id}/archive",
        )
        assert r4.status_code == 200, r4.text

        # Now try to post a message → must get 403.
        r5 = await app_client.post(
            f"/api/projects/arc-rej-proj/epics/{epic_id}/threads/{thread_id}/messages",
            json={"content": "hello after archive", "role": "user"},
        )
        assert r5.status_code == 403, r5.text
        assert "archived" in r5.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Test 7: Unique branch per trial
# ---------------------------------------------------------------------------


class TestUniqueBranchPerTrial:
    """Each manager trial gets a distinct branch name; worktree creation uses that branch."""

    @pytest.mark.asyncio
    async def test_two_trials_have_distinct_branches(self, app_client: AsyncClient) -> None:
        """Two sequential manager trials must have different ThreadEntry.branch values."""
        epic_id = await _setup_project_and_epic(app_client, "ubranch-proj", "UBranch Project")

        # Create trial 1.
        r1 = await app_client.post(
            f"/api/projects/ubranch-proj/epics/{epic_id}/threads",
            json={"title": "Trial 1", "role": "manager"},
        )
        assert r1.status_code == 201, r1.text
        branch1 = r1.json()["branch"]
        thread_id1 = r1.json()["id"]
        assert branch1 is not None, "trial 1 branch must not be None"

        # Archive trial 1 to allow trial 2.
        ra = await app_client.post(
            f"/api/projects/ubranch-proj/epics/{epic_id}/threads/{thread_id1}/archive",
        )
        assert ra.status_code == 200, ra.text

        # Create trial 2.
        r2 = await app_client.post(
            f"/api/projects/ubranch-proj/epics/{epic_id}/threads",
            json={"title": "Trial 2", "role": "manager"},
        )
        assert r2.status_code == 201, r2.text
        branch2 = r2.json()["branch"]
        assert branch2 is not None, "trial 2 branch must not be None"

        # The two branches must be distinct.
        assert branch1 != branch2, f"Trials must have different branches; both got {branch1!r}"

    @pytest.mark.asyncio
    async def test_second_trial_branch_has_ordinal_suffix(self, app_client: AsyncClient) -> None:
        """Trial 2 branch is '{epic.branch}-2', trial 3 is '{epic.branch}-3', etc."""
        epic_id = await _setup_project_and_epic(app_client, "ordsuf-proj", "OrdSuf Project")

        # Trial 1.
        r1 = await app_client.post(
            f"/api/projects/ordsuf-proj/epics/{epic_id}/threads",
            json={"title": "Trial 1", "role": "manager"},
        )
        assert r1.status_code == 201, r1.text
        branch1 = r1.json()["branch"]
        tid1 = r1.json()["id"]

        # Archive trial 1.
        await app_client.post(
            f"/api/projects/ordsuf-proj/epics/{epic_id}/threads/{tid1}/archive",
        )

        # Trial 2.
        r2 = await app_client.post(
            f"/api/projects/ordsuf-proj/epics/{epic_id}/threads",
            json={"title": "Trial 2", "role": "manager"},
        )
        assert r2.status_code == 201, r2.text
        branch2 = r2.json()["branch"]
        tid2 = r2.json()["id"]

        # branch2 should be branch1 + "-2".
        assert branch2 == f"{branch1}-2", f"Expected {branch1!r}-2, got {branch2!r}"

        # Archive trial 2, create trial 3.
        await app_client.post(
            f"/api/projects/ordsuf-proj/epics/{epic_id}/threads/{tid2}/archive",
        )
        r3 = await app_client.post(
            f"/api/projects/ordsuf-proj/epics/{epic_id}/threads",
            json={"title": "Trial 3", "role": "manager"},
        )
        assert r3.status_code == 201, r3.text
        branch3 = r3.json()["branch"]
        assert branch3 == f"{branch1}-3", f"Expected {branch1!r}-3, got {branch3!r}"

    @pytest.mark.asyncio
    async def test_epic_branch_updated_to_active_trial_branch(
        self, app_client: AsyncClient
    ) -> None:
        """After creating trial 2, epic.branch points to the new trial's branch."""
        epic_id = await _setup_project_and_epic(app_client, "epbr-proj", "EpBr Project")
        root = app_client._transport.app.state.settings.workspace_root  # type: ignore[attr-defined]  # ty: ignore[unresolved-attribute]

        from yukar.storage.epic_repo import get_epic

        # Trial 1.
        r1 = await app_client.post(
            f"/api/projects/epbr-proj/epics/{epic_id}/threads",
            json={"title": "Trial 1", "role": "manager"},
        )
        assert r1.status_code == 201, r1.text
        branch1 = r1.json()["branch"]
        tid1 = r1.json()["id"]

        epic_after_t1 = await get_epic(root, "epbr-proj", epic_id)
        assert epic_after_t1 is not None
        assert epic_after_t1.branch == branch1
        assert epic_after_t1.active_thread_id == tid1

        # Archive trial 1, create trial 2.
        await app_client.post(
            f"/api/projects/epbr-proj/epics/{epic_id}/threads/{tid1}/archive",
        )
        r2 = await app_client.post(
            f"/api/projects/epbr-proj/epics/{epic_id}/threads",
            json={"title": "Trial 2", "role": "manager"},
        )
        assert r2.status_code == 201, r2.text
        branch2 = r2.json()["branch"]
        tid2 = r2.json()["id"]

        epic_after_t2 = await get_epic(root, "epbr-proj", epic_id)
        assert epic_after_t2 is not None
        # epic.branch must now point to trial 2's unique branch.
        assert epic_after_t2.branch == branch2, (
            f"epic.branch should be {branch2!r} (trial 2), got {epic_after_t2.branch!r}"
        )
        assert epic_after_t2.active_thread_id == tid2

    @pytest.mark.asyncio
    async def test_thread_entry_branch_stored_correctly(self, app_client: AsyncClient) -> None:
        """ThreadEntry.branch for each trial is stored in threads.yaml with the unique value."""
        epic_id = await _setup_project_and_epic(app_client, "wt-br-proj", "WtBr Project")
        root = app_client._transport.app.state.settings.workspace_root  # type: ignore[attr-defined]  # ty: ignore[unresolved-attribute]

        # Trial 1.
        r1 = await app_client.post(
            f"/api/projects/wt-br-proj/epics/{epic_id}/threads",
            json={"title": "Trial 1", "role": "manager"},
        )
        assert r1.status_code == 201, r1.text
        branch1 = r1.json()["branch"]
        tid1 = r1.json()["id"]

        # Archive trial 1 and create trial 2.
        await app_client.post(
            f"/api/projects/wt-br-proj/epics/{epic_id}/threads/{tid1}/archive",
        )
        r2 = await app_client.post(
            f"/api/projects/wt-br-proj/epics/{epic_id}/threads",
            json={"title": "Trial 2", "role": "manager"},
        )
        assert r2.status_code == 201, r2.text
        branch2 = r2.json()["branch"]
        tid2 = r2.json()["id"]
        assert branch2 != branch1, "Precondition: branches must differ"

        # Verify ThreadEntry.branch is stored correctly for each trial in threads.yaml.
        from yukar.storage import threads_repo

        tf = await threads_repo.get_threads(root, "wt-br-proj", epic_id)
        entry1 = next((t for t in tf.threads if t.id == tid1), None)
        entry2 = next((t for t in tf.threads if t.id == tid2), None)
        assert entry1 is not None
        assert entry2 is not None
        assert entry1.branch == branch1, (
            f"Trial 1 ThreadEntry.branch should be {branch1!r}, got {entry1.branch!r}"
        )
        assert entry2.branch == branch2, (
            f"Trial 2 ThreadEntry.branch should be {branch2!r}, got {entry2.branch!r}"
        )
        # Confirm the two stored branches are distinct.
        assert entry1.branch != entry2.branch, (
            "ThreadEntry.branch values must be distinct across trials"
        )


# ---------------------------------------------------------------------------
# Test 8: archive_active=True — atomic archive + new trial (TestAtomicCreateThread)
# ---------------------------------------------------------------------------


class TestAtomicCreateThread:
    """archive_active=True: active trial is archived atomically, then new trial is created."""

    @pytest.mark.asyncio
    async def test_archive_active_creates_new_trial(self, app_client: AsyncClient) -> None:
        """archive_active=True archives the current trial and creates a new one atomically."""
        epic_id = await _setup_project_and_epic(app_client, "atomic-proj", "Atomic Project")

        # Create first trial.
        r1 = await app_client.post(
            f"/api/projects/atomic-proj/epics/{epic_id}/threads",
            json={"title": "Trial 1", "role": "manager"},
        )
        assert r1.status_code == 201, r1.text
        tid1 = r1.json()["id"]
        branch1 = r1.json()["branch"]

        # Create second trial with archive_active=True (no explicit archive needed).
        r2 = await app_client.post(
            f"/api/projects/atomic-proj/epics/{epic_id}/threads",
            json={"title": "Trial 2", "role": "manager", "archive_active": True},
        )
        assert r2.status_code == 201, r2.text
        data2 = r2.json()
        tid2 = data2["id"]

        assert tid2 != tid1
        assert data2["status"] == "active"
        assert data2["branch"] != branch1, "new trial must get a distinct branch"

        root = app_client._transport.app.state.settings.workspace_root  # type: ignore[attr-defined]  # ty: ignore[unresolved-attribute]
        from yukar.storage import threads_repo
        from yukar.storage.epic_repo import get_epic

        # Trial 1 must now be archived.
        tf = await threads_repo.get_threads(root, "atomic-proj", epic_id)
        entry1 = next((t for t in tf.threads if t.id == tid1), None)
        assert entry1 is not None
        assert entry1.status == "archived", f"trial 1 should be archived, got {entry1.status!r}"

        # epic.active_thread_id must point to the new trial.
        epic = await get_epic(root, "atomic-proj", epic_id)
        assert epic is not None
        assert epic.active_thread_id == tid2

    @pytest.mark.asyncio
    async def test_archive_active_with_running_run_returns_409(
        self, app_client: AsyncClient
    ) -> None:
        """archive_active=True with a running trial returns 409 and preserves the active trial."""
        from unittest.mock import MagicMock

        from yukar.runs.supervisor import _RunHandle, get_supervisor

        epic_id = await _setup_project_and_epic(app_client, "atomic-409-proj", "Atomic409 Project")

        # Create first trial.
        r1 = await app_client.post(
            f"/api/projects/atomic-409-proj/epics/{epic_id}/threads",
            json={"title": "Trial 1", "role": "manager"},
        )
        assert r1.status_code == 201, r1.text
        tid1 = r1.json()["id"]

        # Inject a fake active run for tid1 into the singleton supervisor.
        sup = get_supervisor()
        mock_runner = MagicMock()
        mock_task = MagicMock()
        mock_task.done.return_value = False
        fake_handle = _RunHandle(
            run_id="run-fake",
            runner=mock_runner,
            task=mock_task,
            root="/tmp",
            project_id="atomic-409-proj",
            epic_id=epic_id,
            manager_thread_id=tid1,
        )
        sup._runs[("atomic-409-proj", epic_id)] = fake_handle

        try:
            # Attempt to archive+create with archive_active=True → 409 (run is active).
            r2 = await app_client.post(
                f"/api/projects/atomic-409-proj/epics/{epic_id}/threads",
                json={"title": "Trial 2", "role": "manager", "archive_active": True},
            )
            assert r2.status_code == 409, r2.text
            assert "active run" in r2.json()["detail"].lower()

            # Trial 1 must NOT have been archived.
            root = app_client._transport.app.state.settings.workspace_root  # type: ignore[attr-defined]  # ty: ignore[unresolved-attribute]
            from yukar.storage import threads_repo

            tf = await threads_repo.get_threads(root, "atomic-409-proj", epic_id)
            entry1 = next((t for t in tf.threads if t.id == tid1), None)
            assert entry1 is not None
            assert entry1.status == "active", f"trial 1 should remain active, got {entry1.status!r}"
        finally:
            # Clean up the fake handle so it doesn't leak into other tests.
            sup._runs.pop(("atomic-409-proj", epic_id), None)


# ---------------------------------------------------------------------------
# Test 9: Lock concurrency (TestLockConcurrency)
# ---------------------------------------------------------------------------


class TestLockConcurrency:
    """Per-epic lock serialises concurrent create_thread calls."""

    @pytest.mark.asyncio
    async def test_lock_serialises_creates(self, app_client: AsyncClient) -> None:
        """Two concurrent manager-thread creates for the same epic result in exactly one active."""
        import asyncio

        epic_id = await _setup_project_and_epic(app_client, "lock-proj", "Lock Project")

        # Fire two concurrent creates — one should succeed (201), one should fail (409).
        async def _create() -> int:
            r = await app_client.post(
                f"/api/projects/lock-proj/epics/{epic_id}/threads",
                json={"title": "Manager Trial", "role": "manager"},
            )
            return r.status_code

        results = await asyncio.gather(_create(), _create())
        statuses = sorted(results)
        # One 201 and one 409 — the lock ensures mutual exclusion.
        assert statuses == [201, 409], (
            f"Expected [201, 409] from two concurrent creates, got {statuses}"
        )


# ---------------------------------------------------------------------------
# Test 10: Title ordinal (TestM4Title)
# ---------------------------------------------------------------------------


class TestM4Title:
    """Empty title for manager trial gets 'Trial N' default."""

    @pytest.mark.asyncio
    async def test_empty_title_gets_ordinal_default(self, app_client: AsyncClient) -> None:
        """First manager trial with empty title becomes 'Trial 1'."""
        epic_id = await _setup_project_and_epic(app_client, "title-proj", "Title Project")

        r = await app_client.post(
            f"/api/projects/title-proj/epics/{epic_id}/threads",
            json={"title": "", "role": "manager"},
        )
        assert r.status_code == 201, r.text
        assert r.json()["title"] == "Trial 1"

    @pytest.mark.asyncio
    async def test_second_empty_title_gets_trial_2(self, app_client: AsyncClient) -> None:
        """Second manager trial with empty title becomes 'Trial 2'."""
        epic_id = await _setup_project_and_epic(app_client, "title2-proj", "Title2 Project")

        r1 = await app_client.post(
            f"/api/projects/title2-proj/epics/{epic_id}/threads",
            json={"title": "Trial 1", "role": "manager"},
        )
        assert r1.status_code == 201, r1.text
        tid1 = r1.json()["id"]

        # Archive first trial.
        await app_client.post(
            f"/api/projects/title2-proj/epics/{epic_id}/threads/{tid1}/archive",
        )

        # Second trial with empty title.
        r2 = await app_client.post(
            f"/api/projects/title2-proj/epics/{epic_id}/threads",
            json={"title": "", "role": "manager"},
        )
        assert r2.status_code == 201, r2.text
        assert r2.json()["title"] == "Trial 2"

    @pytest.mark.asyncio
    async def test_whitespace_title_gets_ordinal_default(self, app_client: AsyncClient) -> None:
        """Whitespace-only title is treated as empty and gets 'Trial N'."""
        epic_id = await _setup_project_and_epic(app_client, "title-ws-proj", "TitleWS Project")

        r = await app_client.post(
            f"/api/projects/title-ws-proj/epics/{epic_id}/threads",
            json={"title": "   ", "role": "manager"},
        )
        assert r.status_code == 201, r.text
        assert r.json()["title"] == "Trial 1"


# ---------------------------------------------------------------------------
# Test 11: is_thread_run_active (TestM5WorktreePath)
# ---------------------------------------------------------------------------


class TestM5WorktreePath:
    """supervisor.is_thread_run_active returns correct judgements."""

    def test_is_thread_run_active_returns_true_for_active_run(self) -> None:
        """is_thread_run_active returns True when the handle matches and task is not done."""
        from unittest.mock import MagicMock

        from yukar.runs.supervisor import RunSupervisor, _RunHandle

        sup = RunSupervisor()
        mock_task = MagicMock()
        mock_task.done.return_value = False
        handle = _RunHandle(
            run_id="run-1",
            runner=MagicMock(),
            task=mock_task,
            root="/tmp",
            project_id="p",
            epic_id="EP-1",
            manager_thread_id="th-abc",
        )
        sup._runs[("p", "EP-1")] = handle

        assert sup.is_thread_run_active("p", "EP-1", "th-abc") is True
        assert sup.is_thread_run_active("p", "EP-1", "th-other") is False

    def test_is_thread_run_active_returns_false_when_no_run(self) -> None:
        """is_thread_run_active returns False when no run is registered."""
        from yukar.runs.supervisor import RunSupervisor

        sup = RunSupervisor()
        assert sup.is_thread_run_active("p", "EP-1", "th-abc") is False

    def test_is_thread_run_active_returns_false_when_task_done(self) -> None:
        """is_thread_run_active returns False when the task has completed."""
        from unittest.mock import MagicMock

        from yukar.runs.supervisor import RunSupervisor, _RunHandle

        sup = RunSupervisor()
        mock_task = MagicMock()
        mock_task.done.return_value = True  # task is finished
        handle = _RunHandle(
            run_id="run-1",
            runner=MagicMock(),
            task=mock_task,
            root="/tmp",
            project_id="p",
            epic_id="EP-1",
            manager_thread_id="th-abc",
        )
        sup._runs[("p", "EP-1")] = handle

        assert sup.is_thread_run_active("p", "EP-1", "th-abc") is False

    @pytest.mark.asyncio
    async def test_active_thread_id_used_for_arbiter_worktree(self, tmp_path: Any) -> None:
        """active_thread_id drives the worktree path selection in arbiter/resolve logic."""
        from yukar.config import paths

        root = str(tmp_path)
        # When active_thread_id is "th-trial2", the path must include "th-trial2".
        wt = paths.worktree_dir(root, "proj", "EP-1", "th-trial2", "repo")
        assert "th-trial2" in str(wt)

        # Backward compat: when using "manager" fallback.
        wt_default = paths.worktree_dir(root, "proj", "EP-1", "manager", "repo")
        assert "manager" in str(wt_default)
        assert wt != wt_default


# ---------------------------------------------------------------------------
# Test 12: POST /run resolves epic.active_thread_id (TestStartRunResolvesActiveTrialId)
# ---------------------------------------------------------------------------


class TestStartRunResolvesActiveTrialId:
    """POST /run must resolve epic.active_thread_id before calling supervisor.start.

    Regression test for the bug where start_run always passed manager_thread_id="manager"
    (the default) to supervisor.start, ignoring epic.active_thread_id.  When the active
    trial is "th-2" (the second trial), this caused the orchestrator to write into
    worktrees/manager/ instead of worktrees/th-2/.
    """

    @pytest.mark.asyncio
    async def test_start_run_passes_active_thread_id_to_supervisor(
        self, tmp_path: Any
    ) -> None:
        """POST /run with active_thread_id=th-2 calls supervisor.start(manager_thread_id='th-2')."""
        from unittest.mock import AsyncMock, patch

        from yukar.models.epic import Epic
        from yukar.models.project import Project
        from yukar.storage.epic_repo import save_epic
        from yukar.storage.project_repo import save_project

        root = str(tmp_path / "ws")
        pid, eid = "p-start-tid", "EP-tid"
        await save_project(root, Project(id=pid, name=pid))
        await save_epic(
            root,
            pid,
            Epic(
                id=eid,
                slug="slug",
                title="T",
                branch="yukar/ep-tid-slug-th-2",
                # Simulates second trial being active.
                active_thread_id="th-2",
            ),
        )

        from yukar.api.routers import runs as runs_router
        from yukar.runs.supervisor import RunSupervisor
        from yukar.usage.tracker import TokenUsageTracker

        tracker = TokenUsageTracker.__new__(TokenUsageTracker)
        tracker._over_budget = False  # type: ignore[attr-defined]
        tracker.is_over_budget = lambda: False  # type: ignore[attr-defined]

        sup = RunSupervisor()
        mock_start = AsyncMock(return_value="run-fake")

        with (
            patch.object(runs_router, "get_epic_or_404") as mock_get_epic,
            patch.object(sup, "start", mock_start),
        ):
            from yukar.models.epic import Epic as EpicModel

            mock_get_epic.return_value = EpicModel(
                id=eid,
                slug="slug",
                title="T",
                branch="yukar/ep-tid-slug-th-2",
                active_thread_id="th-2",
            )
            # Call the handler function directly.
            result = await runs_router.start_run(
                project_id=pid,
                epic_id=eid,
                root=root,
                supervisor=sup,
                usage_tracker=tracker,
            )

        assert result == {"run_id": "run-fake", "status": "started"}
        # The critical assertion: supervisor.start must have been called with th-2.
        mock_start.assert_called_once_with(
            root, pid, eid, manager_thread_id="th-2"
        )

    @pytest.mark.asyncio
    async def test_start_run_falls_back_to_manager_when_no_active_thread_id(
        self, tmp_path: Any
    ) -> None:
        """POST /run with active_thread_id=None falls back to manager_thread_id='manager'."""
        from unittest.mock import AsyncMock, patch

        from yukar.api.routers import runs as runs_router
        from yukar.models.epic import Epic
        from yukar.models.project import Project
        from yukar.runs.supervisor import RunSupervisor
        from yukar.storage.epic_repo import save_epic
        from yukar.storage.project_repo import save_project
        from yukar.usage.tracker import TokenUsageTracker

        root = str(tmp_path / "ws2")
        pid, eid = "p-start-mgr", "EP-mgr"

        # Persist the epic so the TOCTOU lock-internal reload succeeds.
        await save_project(root, Project(id=pid, name=pid))
        await save_epic(
            root,
            pid,
            Epic(
                id=eid,
                slug="slug",
                title="T",
                branch="yukar/ep-mgr-slug",
                # active_thread_id=None → backward-compat "manager" fallback.
                active_thread_id=None,
            ),
        )

        tracker = TokenUsageTracker.__new__(TokenUsageTracker)
        tracker._over_budget = False  # type: ignore[attr-defined]
        tracker.is_over_budget = lambda: False  # type: ignore[attr-defined]

        sup = RunSupervisor()
        mock_start = AsyncMock(return_value="run-fake-mgr")

        with (
            patch.object(runs_router, "get_epic_or_404") as mock_get_epic,
            patch.object(sup, "start", mock_start),
        ):
            from yukar.models.epic import Epic as EpicModel

            mock_get_epic.return_value = EpicModel(
                id=eid,
                slug="slug",
                title="T",
                branch="yukar/ep-mgr-slug",
                # active_thread_id=None → backward-compat "manager" fallback.
                active_thread_id=None,
            )
            result = await runs_router.start_run(
                project_id=pid,
                epic_id=eid,
                root=root,
                supervisor=sup,
                usage_tracker=tracker,
            )

        assert result == {"run_id": "run-fake-mgr", "status": "started"}
        # Backward-compat: must still default to "manager".
        mock_start.assert_called_once_with(
            root, pid, eid, manager_thread_id="manager"
        )


# ---------------------------------------------------------------------------
# Test 13: Regression — post_message after run completes (resolved/failed)
# ---------------------------------------------------------------------------


class TestPostMessageAfterRunCompletes:
    """Regression: run completion (resolved/failed) must not block follow-up messages.

    Before the fix, ``_is_active_manager_thread`` checked ``entry.status == "active"``
    so a resolved/failed manager trial would fall through to the no-op inject path
    instead of the continuation ``start_or_inject`` path.
    """

    def _make_epic(self, active_thread_id: str | None = None) -> Any:
        from yukar.models.epic import Epic

        return Epic(
            id="EP-1",
            slug="slug",
            title="T",
            branch="yukar/ep-1-slug",
            active_thread_id=active_thread_id,
        )

    def _make_tf(self, entries: list[tuple[str, str, str]]) -> Any:
        from yukar.models.thread import ThreadEntry, ThreadsFile

        threads = [
            ThreadEntry(
                id=tid,
                title="T",
                role=role,  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
                status=status,  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
            )
            for tid, role, status in entries
        ]
        return ThreadsFile(threads=threads)

    # ------------------------------------------------------------------
    # Unit tests: _is_active_manager_thread semantics
    # ------------------------------------------------------------------

    def test_resolved_manager_is_active(self) -> None:
        """resolved manager trial is still continuable (not archived)."""
        from yukar.api.routers.threads import _is_active_manager_thread

        epic = self._make_epic(active_thread_id=None)
        tf = self._make_tf([("manager", "manager", "resolved")])
        assert _is_active_manager_thread(epic, tf, "manager") is True

    def test_failed_manager_is_active(self) -> None:
        """failed manager trial is still continuable (not archived)."""
        from yukar.api.routers.threads import _is_active_manager_thread

        epic = self._make_epic(active_thread_id=None)
        tf = self._make_tf([("manager", "manager", "failed")])
        assert _is_active_manager_thread(epic, tf, "manager") is True

    def test_resolved_named_trial_is_active(self) -> None:
        """resolved named trial (active_thread_id=th-x) is still continuable."""
        from yukar.api.routers.threads import _is_active_manager_thread

        epic = self._make_epic(active_thread_id="th-x")
        tf = self._make_tf([("th-x", "manager", "resolved")])
        assert _is_active_manager_thread(epic, tf, "th-x") is True

    def test_archived_manager_is_not_active(self) -> None:
        """archived manager trial must return False (read-only)."""
        from yukar.api.routers.threads import _is_active_manager_thread

        epic = self._make_epic(active_thread_id=None)
        tf = self._make_tf([("manager", "manager", "archived")])
        assert _is_active_manager_thread(epic, tf, "manager") is False

    def test_archived_named_trial_is_not_active(self) -> None:
        """archived named trial must return False even if it were somehow the active_thread_id.

        In practice archive_thread clears active_thread_id, so this can't happen
        in production.  The helper must still reject it defensively.
        """
        from yukar.api.routers.threads import _is_active_manager_thread

        # Simulate a hypothetical state where active_thread_id still points to
        # the archived trial (shouldn't occur in production).
        epic = self._make_epic(active_thread_id="th-arc")
        tf = self._make_tf([("th-arc", "manager", "archived")])
        assert _is_active_manager_thread(epic, tf, "th-arc") is False

    # ------------------------------------------------------------------
    # Integration tests: POST /messages routes to start_or_inject, not no-op
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_post_message_after_resolved_calls_start_or_inject(
        self, app_client: AsyncClient
    ) -> None:
        """After run completes (resolved), a follow-up POST routes to start_or_inject.

        Uses the lazy-registration "manager" thread id path (active_thread_id=None).
        """
        from unittest.mock import AsyncMock, patch

        from yukar.runs import supervisor as sup_module

        epic_id = await _setup_project_and_epic(app_client, "post-res-proj", "PostRes Project")

        root = app_client._transport.app.state.settings.workspace_root  # type: ignore[attr-defined]  # ty: ignore[unresolved-attribute]

        # Write a resolved manager ThreadEntry directly so the router sees it.
        from yukar.models.thread import ThreadEntry, ThreadsFile
        from yukar.storage import threads_repo

        resolved_entry = ThreadEntry(
            id="manager",
            title="Trial 1",
            role="manager",  # type: ignore[arg-type]
            status="resolved",  # type: ignore[arg-type]
        )
        await threads_repo.save_threads(
            root, "post-res-proj", epic_id, ThreadsFile(threads=[resolved_entry])
        )

        # Patch start_or_inject so we can verify it is called.
        mock_start_or_inject = AsyncMock(return_value=False)  # False = no active run → new run
        with patch.object(
            sup_module.RunSupervisor,
            "start_or_inject",
            mock_start_or_inject,
        ):
            r = await app_client.post(
                f"/api/projects/post-res-proj/epics/{epic_id}/threads/manager/messages",
                json={"content": "please fix the bug", "role": "user"},
            )

        assert r.status_code == 201, r.text
        # The key assertion: start_or_inject must have been called (continuation path),
        # NOT the no-op inject_hitl_message path.
        mock_start_or_inject.assert_called_once()
        call_args = mock_start_or_inject.call_args
        assert call_args.args[3] == "manager"  # thread_id
        assert call_args.args[4] == "please fix the bug"  # message content

    @pytest.mark.asyncio
    async def test_post_message_after_resolved_named_trial_calls_start_or_inject(
        self, app_client: AsyncClient
    ) -> None:
        """After run completes (resolved), POST to a named trial routes to start_or_inject."""
        from unittest.mock import AsyncMock, patch

        from yukar.runs import supervisor as sup_module

        epic_id = await _setup_project_and_epic(
            app_client, "post-res-named-proj", "PostResNamed Project"
        )

        root = app_client._transport.app.state.settings.workspace_root  # type: ignore[attr-defined]  # ty: ignore[unresolved-attribute]

        # Create a named manager trial via the API.
        r_create = await app_client.post(
            f"/api/projects/post-res-named-proj/epics/{epic_id}/threads",
            json={"title": "Trial 1", "role": "manager"},
        )
        assert r_create.status_code == 201, r_create.text
        thread_id = r_create.json()["id"]

        # Mark the trial as resolved (simulates orchestrator completing the run).
        from yukar.storage import threads_repo

        tf = await threads_repo.get_threads(root, "post-res-named-proj", epic_id)
        entry = next(t for t in tf.threads if t.id == thread_id)
        entry.status = "resolved"  # type: ignore[assignment]
        await threads_repo.save_threads(root, "post-res-named-proj", epic_id, tf)

        # Patch start_or_inject.
        mock_start_or_inject = AsyncMock(return_value=False)
        with patch.object(
            sup_module.RunSupervisor,
            "start_or_inject",
            mock_start_or_inject,
        ):
            r = await app_client.post(
                f"/api/projects/post-res-named-proj/epics/{epic_id}/threads/{thread_id}/messages",
                json={"content": "follow-up after completion", "role": "user"},
            )

        assert r.status_code == 201, r.text
        mock_start_or_inject.assert_called_once()
        call_args = mock_start_or_inject.call_args
        assert call_args.args[3] == thread_id
        assert call_args.args[4] == "follow-up after completion"

    @pytest.mark.asyncio
    async def test_post_message_to_archived_returns_403(
        self, app_client: AsyncClient
    ) -> None:
        """POST to an archived thread must return 403 (read-only invariant maintained)."""
        epic_id = await _setup_project_and_epic(
            app_client, "post-arc-403-proj", "PostArc403 Project"
        )

        # Create a manager trial.
        r_create = await app_client.post(
            f"/api/projects/post-arc-403-proj/epics/{epic_id}/threads",
            json={"title": "Trial 1", "role": "manager"},
        )
        assert r_create.status_code == 201, r_create.text
        thread_id = r_create.json()["id"]

        # Archive it via the API.
        r_arch = await app_client.post(
            f"/api/projects/post-arc-403-proj/epics/{epic_id}/threads/{thread_id}/archive",
        )
        assert r_arch.status_code == 200, r_arch.text

        # POST to the archived thread must return 403.
        r_post = await app_client.post(
            f"/api/projects/post-arc-403-proj/epics/{epic_id}/threads/{thread_id}/messages",
            json={"content": "should be rejected", "role": "user"},
        )
        assert r_post.status_code == 403, r_post.text
        assert "archived" in r_post.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_create_thread_409_gate_unchanged_for_active_trial(
        self, app_client: AsyncClient
    ) -> None:
        """create_thread 409 gate still fires when the current trial is active.

        Regression guard: the resolved/failed fix must not affect the create gate.
        """
        epic_id = await _setup_project_and_epic(app_client, "gate-409-proj", "Gate409 Project")

        # Create first (active) trial.
        r1 = await app_client.post(
            f"/api/projects/gate-409-proj/epics/{epic_id}/threads",
            json={"title": "Trial 1", "role": "manager"},
        )
        assert r1.status_code == 201, r1.text

        # Attempt to create a second trial while the first is still active → 409.
        r2 = await app_client.post(
            f"/api/projects/gate-409-proj/epics/{epic_id}/threads",
            json={"title": "Trial 2", "role": "manager"},
        )
        assert r2.status_code == 409, r2.text

    @pytest.mark.asyncio
    async def test_create_thread_after_resolved_succeeds(
        self, app_client: AsyncClient
    ) -> None:
        """create_thread with a resolved (non-archived) active trial returns 201.

        Spec change (fix 2): resolved/failed trials are not auto-archived.
        New trial creation succeeds with 201 and active_thread_id is updated to the new trial.
        The old trial remains in the list as resolved (not archived).
        The 409 gate for active-status trials is preserved (verified in a separate test below).
        """
        epic_id = await _setup_project_and_epic(
            app_client, "gate-res-proj", "GateRes Project"
        )

        # Create first trial.
        r1 = await app_client.post(
            f"/api/projects/gate-res-proj/epics/{epic_id}/threads",
            json={"title": "Trial 1", "role": "manager"},
        )
        assert r1.status_code == 201, r1.text
        tid1 = r1.json()["id"]

        # Mark it resolved directly in storage (simulating orchestrator completion).
        # epic.active_thread_id still points to tid1.
        root = app_client._transport.app.state.settings.workspace_root  # type: ignore[attr-defined]  # ty: ignore[unresolved-attribute]
        from yukar.storage import threads_repo

        tf = await threads_repo.get_threads(root, "gate-res-proj", epic_id)
        entry = next(t for t in tf.threads if t.id == tid1)
        entry.status = "resolved"  # type: ignore[assignment]
        await threads_repo.save_threads(root, "gate-res-proj", epic_id, tf)

        # New trial creation MUST succeed (201) — resolved trial is not an obstacle.
        r2 = await app_client.post(
            f"/api/projects/gate-res-proj/epics/{epic_id}/threads",
            json={"title": "Trial 2", "role": "manager"},
        )
        assert r2.status_code == 201, r2.text
        tid2 = r2.json()["id"]
        assert r2.json()["status"] == "active"
        assert tid2 != tid1, "new trial must have a different id"

        # Old trial must remain resolved (NOT archived).
        from yukar.storage.epic_repo import get_epic

        tf_after = await threads_repo.get_threads(root, "gate-res-proj", epic_id)
        entry1_after = next((t for t in tf_after.threads if t.id == tid1), None)
        assert entry1_after is not None
        assert entry1_after.status == "resolved", (
            f"old trial should remain resolved, got {entry1_after.status!r}"
        )

        # epic.active_thread_id must now point to the new trial.
        epic_after = await get_epic(root, "gate-res-proj", epic_id)
        assert epic_after is not None
        assert epic_after.active_thread_id == tid2

        # Branch must be unique (new trial gets its own branch).
        branch2 = r2.json()["branch"]
        assert branch2 is not None
        entry1_branch = entry1_after.branch
        assert branch2 != entry1_branch, (
            f"new trial branch {branch2!r} must differ from old trial branch {entry1_branch!r}"
        )


# ---------------------------------------------------------------------------
# Test 14: TOCTOU — archive vs. run start (TestArchiveRunTOCTOU)
# ---------------------------------------------------------------------------


class TestArchiveRunTOCTOU:
    """Fix 1: TOCTOU between archive and run start — verifies lock protection.

    Because the archive path and the run-start path are both protected by the
    same epic_thread_lock, the sequence
    "archive confirms no active run → run registers → archive deletes worktree"
    cannot occur.
    """

    @pytest.mark.asyncio
    async def test_archive_with_active_run_returns_409(self, app_client: AsyncClient) -> None:
        """archive_thread returns 409 when an active run exists, without deleting the worktree."""
        from unittest.mock import MagicMock

        from yukar.runs.supervisor import _RunHandle, get_supervisor

        epic_id = await _setup_project_and_epic(app_client, "toctou-arch-proj", "TOCTOU Arch")

        r1 = await app_client.post(
            f"/api/projects/toctou-arch-proj/epics/{epic_id}/threads",
            json={"title": "Trial 1", "role": "manager"},
        )
        assert r1.status_code == 201, r1.text
        tid1 = r1.json()["id"]

        # Inject a fake active run for tid1.
        sup = get_supervisor()
        mock_task = MagicMock()
        mock_task.done.return_value = False
        fake_handle = _RunHandle(
            run_id="run-toctou",
            runner=MagicMock(),
            task=mock_task,
            root="/tmp",
            project_id="toctou-arch-proj",
            epic_id=epic_id,
            manager_thread_id=tid1,
        )
        sup._runs[("toctou-arch-proj", epic_id)] = fake_handle

        try:
            r_arch = await app_client.post(
                f"/api/projects/toctou-arch-proj/epics/{epic_id}/threads/{tid1}/archive",
            )
            # Must be 409 — run is active.
            assert r_arch.status_code == 409, r_arch.text
            assert "active run" in r_arch.json()["detail"].lower()

            # Trial must still be active (not archived).
            root = app_client._transport.app.state.settings.workspace_root  # type: ignore[attr-defined]  # ty: ignore[unresolved-attribute]
            from yukar.storage import threads_repo

            tf = await threads_repo.get_threads(root, "toctou-arch-proj", epic_id)
            entry = next((t for t in tf.threads if t.id == tid1), None)
            assert entry is not None
            assert entry.status == "active", f"trial must remain active, got {entry.status!r}"
        finally:
            sup._runs.pop(("toctou-arch-proj", epic_id), None)

    @pytest.mark.asyncio
    async def test_start_run_with_archived_active_trial_returns_409(
        self, app_client: AsyncClient
    ) -> None:
        """POST /run returns 409 when the active trial is archived (ghost worktree prevention)."""
        epic_id = await _setup_project_and_epic(app_client, "toctou-run-proj", "TOCTOU Run")

        r1 = await app_client.post(
            f"/api/projects/toctou-run-proj/epics/{epic_id}/threads",
            json={"title": "Trial 1", "role": "manager"},
        )
        assert r1.status_code == 201, r1.text
        tid1 = r1.json()["id"]

        # Archive the trial (clears active_thread_id).
        r_arch = await app_client.post(
            f"/api/projects/toctou-run-proj/epics/{epic_id}/threads/{tid1}/archive",
        )
        assert r_arch.status_code == 200, r_arch.text

        # Forcibly set active_thread_id back to the archived trial to simulate
        # the TOCTOU window (archive cleared it, but an attacker/race re-set it).
        root = app_client._transport.app.state.settings.workspace_root  # type: ignore[attr-defined]  # ty: ignore[unresolved-attribute]
        from yukar.storage.epic_repo import get_epic, save_epic

        epic = await get_epic(root, "toctou-run-proj", epic_id)
        assert epic is not None
        epic.active_thread_id = tid1  # Point back to archived trial.
        await save_epic(root, "toctou-run-proj", epic)

        # POST /run must refuse to start a run for an archived trial.
        r_run = await app_client.post(
            f"/api/projects/toctou-run-proj/epics/{epic_id}/run",
        )
        assert r_run.status_code == 409, r_run.text
        detail_lower = r_run.json()["detail"].lower()
        assert "archived" in detail_lower


# ---------------------------------------------------------------------------
# Test 15: resolved trial — new trial creation (TestResolvedTrialNewTrial)
# ---------------------------------------------------------------------------


class TestResolvedTrialNewTrial:
    """Fix 2: new trial creation must succeed even when a resolved trial already exists."""

    @pytest.mark.asyncio
    async def test_failed_trial_allows_new_trial(self, app_client: AsyncClient) -> None:
        """New trial creation returns 201 even when a failed (non-archived) trial exists."""
        epic_id = await _setup_project_and_epic(app_client, "failed-new-proj", "Failed New")

        r1 = await app_client.post(
            f"/api/projects/failed-new-proj/epics/{epic_id}/threads",
            json={"title": "Trial 1", "role": "manager"},
        )
        assert r1.status_code == 201, r1.text
        tid1 = r1.json()["id"]

        # Simulate failure (status="failed").
        root = app_client._transport.app.state.settings.workspace_root  # type: ignore[attr-defined]  # ty: ignore[unresolved-attribute]
        from yukar.storage import threads_repo

        tf = await threads_repo.get_threads(root, "failed-new-proj", epic_id)
        entry = next(t for t in tf.threads if t.id == tid1)
        entry.status = "failed"  # type: ignore[assignment]
        await threads_repo.save_threads(root, "failed-new-proj", epic_id, tf)

        # New trial must succeed (201) without archiving the failed trial.
        r2 = await app_client.post(
            f"/api/projects/failed-new-proj/epics/{epic_id}/threads",
            json={"title": "Trial 2", "role": "manager"},
        )
        assert r2.status_code == 201, r2.text
        assert r2.json()["status"] == "active"

        # Old trial must remain failed (not archived).
        tf_after = await threads_repo.get_threads(root, "failed-new-proj", epic_id)
        entry1 = next((t for t in tf_after.threads if t.id == tid1), None)
        assert entry1 is not None
        assert entry1.status == "failed", f"old trial must remain failed, got {entry1.status!r}"

    @pytest.mark.asyncio
    async def test_active_trial_still_blocks_new_trial(self, app_client: AsyncClient) -> None:
        """New trial creation returns 409 when an active (status=active) trial exists
        (existing behaviour preserved)."""
        epic_id = await _setup_project_and_epic(app_client, "active-block-proj", "Active Block")

        r1 = await app_client.post(
            f"/api/projects/active-block-proj/epics/{epic_id}/threads",
            json={"title": "Trial 1", "role": "manager"},
        )
        assert r1.status_code == 201, r1.text

        # Second trial must be blocked while first is active.
        r2 = await app_client.post(
            f"/api/projects/active-block-proj/epics/{epic_id}/threads",
            json={"title": "Trial 2", "role": "manager"},
        )
        assert r2.status_code == 409, r2.text


# ---------------------------------------------------------------------------
# Test 16: Ghost worktree prevention (TestGhostWorktree)
# ---------------------------------------------------------------------------


class TestGhostWorktree:
    """Fix 3: arbiter/resolve must not create ghost worktrees after all trials are archived."""

    @pytest.mark.asyncio
    async def test_resolve_runner_raises_when_all_trials_archived(
        self, tmp_path: Any
    ) -> None:
        """ResolveRunner.start() raises RuntimeError after all trials are archived."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from yukar.config.settings import LLMSettings
        from yukar.models.epic import Epic
        from yukar.models.project import Project
        from yukar.models.thread import ThreadEntry, ThreadsFile
        from yukar.runs.resolve_runner import ResolveRunner
        from yukar.storage import threads_repo
        from yukar.storage.epic_repo import save_epic
        from yukar.storage.project_repo import save_project

        root = str(tmp_path / "ws")
        pid, eid = "ghost-proj", "EP-ghost"
        repo_name = "repo1"

        await save_project(root, Project(id=pid, name=pid))
        await save_epic(
            root,
            pid,
            Epic(
                id=eid,
                slug="slug",
                title="T",
                branch="yukar/ep-ghost-slug",
                active_thread_id=None,  # cleared by archive
            ),
        )

        # Write archived manager trial.
        archived_entry = ThreadEntry(
            id="th-old",
            title="Trial 1",
            role="manager",  # type: ignore[arg-type]
            status="archived",  # type: ignore[arg-type]
        )
        await threads_repo.save_threads(root, pid, eid, ThreadsFile(threads=[archived_entry]))

        # Stub get_repo so ResolveRunner can load it.
        mock_repo = MagicMock()
        mock_repo.path = str(tmp_path / "repo")
        mock_repo.default_branch = "main"
        mock_repo.commands.allow = []
        mock_repo.commands.deny = []

        llm = LLMSettings()
        runner = ResolveRunner(llm_settings=llm, repo_name=repo_name)

        with (
            patch("yukar.runs.resolve_runner.get_repo", AsyncMock(return_value=mock_repo)),
            pytest.raises(RuntimeError, match="no active manager trial"),
        ):
            await runner.start(root, pid, eid, "run-test")

    @pytest.mark.asyncio
    async def test_arbiter_runner_skips_when_all_trials_archived(
        self, tmp_path: Any
    ) -> None:
        """ArbiterRunner._process_epic() returns a skipped result after all trials are archived."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from yukar.config.settings import LLMSettings
        from yukar.models.epic import Epic
        from yukar.models.project import Project
        from yukar.models.thread import ThreadEntry, ThreadsFile
        from yukar.runs.arbiter_runner import ArbiterRunner
        from yukar.storage import threads_repo
        from yukar.storage.epic_repo import save_epic
        from yukar.storage.project_repo import save_project

        root = str(tmp_path / "ws")
        pid, eid = "ghost-arb-proj", "EP-ghost-arb"
        repo_name = "repo1"

        await save_project(root, Project(id=pid, name=pid))
        await save_epic(
            root,
            pid,
            Epic(
                id=eid,
                slug="slug",
                title="T",
                branch="yukar/ep-ghost-arb-slug",
                active_thread_id=None,  # cleared by archive
                touched_repos=[repo_name],
            ),
        )

        # Write archived manager trial.
        archived_entry = ThreadEntry(
            id="th-old",
            title="Trial 1",
            role="manager",  # type: ignore[arg-type]
            status="archived",  # type: ignore[arg-type]
        )
        await threads_repo.save_threads(root, pid, eid, ThreadsFile(threads=[archived_entry]))

        # Stub get_repo.
        mock_repo = MagicMock()
        mock_repo.path = str(tmp_path / "repo")
        mock_repo.default_branch = "main"
        mock_repo.commands.allow = []
        mock_repo.commands.deny = []

        llm = LLMSettings()
        runner = ArbiterRunner(llm_settings=llm, epic_ids=[eid])

        with (
            patch("yukar.runs.arbiter_runner.get_repo", AsyncMock(return_value=mock_repo)),
            patch("yukar.runs.arbiter_runner.state_repo") as mock_state,
        ):
            mock_state.save_state = AsyncMock()
            result = await runner._process_epic(
                root=root,
                project_id=pid,
                real_epic_id=eid,
                run_id="run-test",
            )

        # Must be skipped (not a ghost merge attempt).
        assert result.status == "skipped", f"expected skipped, got {result.status!r}"
        assert "no active manager trial" in result.detail.lower()


# ---------------------------------------------------------------------------
# Test 17: Path segment leading dash (TestPathSegmentLeadingDash)
# ---------------------------------------------------------------------------


class TestPathSegmentLeadingDash:
    """Minor supplement: _validate_segment rejects a leading '-'."""

    def test_leading_dash_rejected(self, tmp_path: Any) -> None:
        from yukar.config.paths import PathSegmentError, worktree_dir

        root = str(tmp_path)
        with pytest.raises(PathSegmentError, match=r"must not start with '-'"):
            worktree_dir(root, "proj", "EP-1", "-evil", "repo")

    def test_leading_dash_in_project_id_rejected(self, tmp_path: Any) -> None:
        from yukar.config.paths import PathSegmentError, project_dir

        root = str(tmp_path)
        with pytest.raises(PathSegmentError, match=r"must not start with '-'"):
            project_dir(root, "-evil-proj")

    def test_normal_segment_with_inner_dash_accepted(self, tmp_path: Any) -> None:
        """A hyphen that is not leading is valid (commonly used in branch names and ids)."""
        from yukar.config import paths

        root = str(tmp_path)
        # Should not raise.
        p = paths.worktree_dir(root, "proj", "EP-1", "th-abc123", "my-repo")
        assert "th-abc123" in str(p)


# ---------------------------------------------------------------------------
# Test 18: post_message inject-only path returns 409 (TestInjectOnly409)
# ---------------------------------------------------------------------------


class TestInjectOnly409:
    """Minor supplement: POST to a non-active, non-archived trial returns 409."""

    @pytest.mark.asyncio
    async def test_post_to_non_active_manager_thread_returns_409(
        self, app_client: AsyncClient
    ) -> None:
        """POST /messages to a thread that is not the active manager trial → 409."""
        epic_id = await _setup_project_and_epic(
            app_client, "inject-409-proj", "Inject409 Project"
        )

        # Create trial 1 (active).
        r1 = await app_client.post(
            f"/api/projects/inject-409-proj/epics/{epic_id}/threads",
            json={"title": "Trial 1", "role": "manager"},
        )
        assert r1.status_code == 201, r1.text
        tid1 = r1.json()["id"]

        # Archive trial 1; now active_thread_id=None.
        r_arch = await app_client.post(
            f"/api/projects/inject-409-proj/epics/{epic_id}/threads/{tid1}/archive",
        )
        assert r_arch.status_code == 200, r_arch.text

        # Mark trial 1 as non-archived manually to reach the inject-only branch
        # (i.e. it's not archived but also not the active trial).
        root = app_client._transport.app.state.settings.workspace_root  # type: ignore[attr-defined]  # ty: ignore[unresolved-attribute]
        from yukar.storage import threads_repo

        tf = await threads_repo.get_threads(root, "inject-409-proj", epic_id)
        entry = next((t for t in tf.threads if t.id == tid1), None)
        assert entry is not None
        entry.status = "resolved"  # type: ignore[assignment]
        await threads_repo.save_threads(root, "inject-409-proj", epic_id, tf)

        # Now post to tid1 (which is resolved, but active_thread_id=None, so it
        # is NOT the active manager thread).  This hits the else branch → 409.
        r_msg = await app_client.post(
            f"/api/projects/inject-409-proj/epics/{epic_id}/threads/{tid1}/messages",
            json={"content": "hello", "role": "user"},
        )
        assert r_msg.status_code == 409, r_msg.text
        detail_lower = r_msg.json()["detail"].lower()
        assert "manager trial" in detail_lower or "not the active" in detail_lower


# ---------------------------------------------------------------------------
# Test 19: resolve_active_trial_id — direct unit test for the 3 branches
# ---------------------------------------------------------------------------


class TestResolveActiveTrialId:
    """Direct verification of the 3 branches and predicate in
    agents/trials.py::resolve_active_trial_id.

    Security requirement: ghost-worktree fallback rejection must work correctly in each branch.
    """

    # ------------------------------------------------------------------
    # is_active_manager_thread predicate
    # ------------------------------------------------------------------

    def test_predicate_manager_non_archived_is_true(self) -> None:
        """manager + non-archived (active/resolved/failed) is True."""
        from yukar.agents.trials import is_active_manager_thread
        from yukar.models.thread import ThreadEntry

        for status in ("active", "resolved", "failed"):
            entry = ThreadEntry(
                id="th-1",
                title="T",
                role="manager",  # type: ignore[arg-type]
                status=status,  # type: ignore[arg-type]
            )
            assert is_active_manager_thread(entry) is True, (
                f"manager+{status} should be True"
            )

    def test_predicate_manager_archived_is_false(self) -> None:
        """manager + archived is False."""
        from yukar.agents.trials import is_active_manager_thread
        from yukar.models.thread import ThreadEntry

        entry = ThreadEntry(
            id="th-1",
            title="T",
            role="manager",  # type: ignore[arg-type]
            status="archived",  # type: ignore[arg-type]
        )
        assert is_active_manager_thread(entry) is False

    def test_predicate_non_manager_is_false(self) -> None:
        """worker/evaluator/user is False because role is not manager."""
        from yukar.agents.trials import is_active_manager_thread
        from yukar.models.thread import ThreadEntry

        for role in ("worker", "evaluator", "user"):
            entry = ThreadEntry(
                id="th-1",
                title="T",
                role=role,  # type: ignore[arg-type]
                status="active",  # type: ignore[arg-type]
            )
            assert is_active_manager_thread(entry) is False, (
                f"{role}+active should be False"
            )

    # ------------------------------------------------------------------
    # resolve_active_trial_id — 3 branches
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_branch_a_explicit_active_thread_id(self, tmp_path: Any) -> None:
        """(a) epic.active_thread_id is set → returns that value without reading threads.yaml.

        Confirms that active_thread_id is returned as-is regardless of archived status.
        threads.yaml points to a non-existent file, so attempting to read it would error.
        """
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
        # Returns active_thread_id as-is even when threads.yaml does not exist.
        result = await resolve_active_trial_id(root, "proj", "EP-1", epic)
        assert result == "th-explicit"

    @pytest.mark.asyncio
    async def test_branch_b_no_active_thread_id_no_archived(self, tmp_path: Any) -> None:
        """(b) active_thread_id=None and no archived manager thread exists
        (backward-compat epic) → returns "manager".
        """
        from yukar.agents.trials import resolve_active_trial_id
        from yukar.models.epic import Epic
        from yukar.models.project import Project
        from yukar.storage.epic_repo import save_epic
        from yukar.storage.project_repo import save_project

        root = str(tmp_path / "ws")
        pid, eid = "compat-proj", "EP-compat"
        await save_project(root, Project(id=pid, name=pid))
        await save_epic(
            root,
            pid,
            Epic(
                id=eid,
                slug="slug",
                title="T",
                branch="yukar/ep-compat-slug",
                active_thread_id=None,
            ),
        )
        # threads.yaml does not exist (no archived manager) → returns "manager".
        epic = Epic(
            id=eid,
            slug="slug",
            title="T",
            branch="yukar/ep-compat-slug",
            active_thread_id=None,
        )
        result = await resolve_active_trial_id(root, pid, eid, epic)
        assert result == "manager"

    @pytest.mark.asyncio
    async def test_branch_c_no_active_thread_id_with_archived(self, tmp_path: Any) -> None:
        """(c) active_thread_id=None and one or more archived manager threads exist
        → returns None (ghost fallback refused).
        """
        from yukar.agents.trials import resolve_active_trial_id
        from yukar.models.epic import Epic
        from yukar.models.project import Project
        from yukar.models.thread import ThreadEntry, ThreadsFile
        from yukar.storage import threads_repo
        from yukar.storage.epic_repo import save_epic
        from yukar.storage.project_repo import save_project

        root = str(tmp_path / "ws")
        pid, eid = "ghost-guard-proj", "EP-ghost-guard"
        await save_project(root, Project(id=pid, name=pid))
        await save_epic(
            root,
            pid,
            Epic(
                id=eid,
                slug="slug",
                title="T",
                branch="yukar/ep-ghost-guard-slug",
                active_thread_id=None,  # all trials are archived
            ),
        )
        archived_entry = ThreadEntry(
            id="th-old",
            title="Trial 1",
            role="manager",  # type: ignore[arg-type]
            status="archived",  # type: ignore[arg-type]
        )
        await threads_repo.save_threads(root, pid, eid, ThreadsFile(threads=[archived_entry]))

        epic = Epic(
            id=eid,
            slug="slug",
            title="T",
            branch="yukar/ep-ghost-guard-slug",
            active_thread_id=None,
        )
        result = await resolve_active_trial_id(root, pid, eid, epic)
        assert result is None, (
            "Expected None after all trials are archived (ghost fallback refused), "
            f"but got {result!r}"
        )


# ---------------------------------------------------------------------------
# Test 20: POST /threads with role="arbiter" returns 422
# ---------------------------------------------------------------------------


class TestArbiterRoleRejected:
    """Invariant: API clients cannot create arbiter threads directly.

    Because UserCreatableThreadRole excludes arbiter, passing role="arbiter"
    to POST /threads must result in a pydantic v2 validation error (422).
    manager / worker / evaluator / user must continue to be accepted.
    """

    @pytest.mark.asyncio
    async def test_arbiter_role_returns_422(self, app_client: AsyncClient) -> None:
        """POST /threads with role="arbiter" is rejected with 422 (pydantic validation error)."""
        epic_id = await _setup_project_and_epic(
            app_client, "arbiter-422-proj", "Arbiter422 Project"
        )

        r = await app_client.post(
            f"/api/projects/arbiter-422-proj/epics/{epic_id}/threads",
            json={"title": "Arbiter Thread", "role": "arbiter"},
        )
        assert r.status_code == 422, (
            f"Expected 422 for role=arbiter but got {r.status_code}: {r.text}"
        )

    @pytest.mark.asyncio
    async def test_user_creatable_roles_accepted(self, app_client: AsyncClient) -> None:
        """manager / worker / evaluator / user are accepted by POST /threads (201).

        Minimal happy-path check as regression guard against accidental arbiter exclusion.
        Only the first manager trial is created, so 201 is expected.
        """
        epic_id = await _setup_project_and_epic(
            app_client, "allowed-roles-proj", "AllowedRoles Project"
        )
        base = f"/api/projects/allowed-roles-proj/epics/{epic_id}/threads"

        # manager — the first trial is accepted with 201.
        r_mgr = await app_client.post(base, json={"title": "Manager", "role": "manager"})
        assert r_mgr.status_code == 201, f"Expected 201 for manager: {r_mgr.text}"

        # worker / evaluator / user can be created even after the manager exists.
        for role in ("worker", "evaluator", "user"):
            r = await app_client.post(base, json={"title": f"{role} thread", "role": role})
            assert r.status_code == 201, f"Expected 201 for role={role}: {r.text}"
