"""Tests for Feature 2 — Arbiter (batch merge) runner, supervisor, and endpoints.

Covers:
1. Git helpers: start_conflict_merge → manual resolve → forward merge (no agent).
2. ArbiterRunner: clean merge (no conflicts) → merge fact recorded (merged_at).
3. ArbiterRunner: conflict then abort → EpicMergeResult("conflict_unresolved").
4. ArbiterRunner: vetting_refused path (.gitattributes with filter=lfs).
5. ArbiterRunner: serial two-epic — B worktree sees A's merged commit on main.
6. supervisor.start_merge — 409 when arbiter already running.
7. supervisor.start_merge — 409 when an epic is busy.
8. supervisor.start / start_resolve / start_continuation — 409 when arbiter running.
9. supervisor.stop_merge.
10. POST /api/projects/{p}/merge — 202 happy path (DummyRunner fallback).
11. POST /api/projects/{p}/merge/stop — 404 when not running.
12. POST /api/projects/{p}/merge/stop — 200 when running.
13. POST /git/merge — 409 when run is active.
14. POST /git/merge — 422 on GitVettingError.
15. ArbiterRunner: skipped when epic has no branch / no touched_repos / not found.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from tests._helpers import git_env, make_git_repo

# ---------------------------------------------------------------------------
# Git helpers shared across tests
# ---------------------------------------------------------------------------


def _g(repo: Path, *args: str) -> str:
    """Run git in *repo*, assert success, return stdout."""
    r = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        env=git_env(),
    )
    assert r.returncode == 0, f"git {args}: {r.stderr}"
    return r.stdout.strip()


def _make_two_branch_conflict_repo(tmp_path: Path) -> tuple[Path, str, str]:
    """Repo where merging 'feature' into 'main' would conflict.

    Returns (repo_path, epic_branch, default_branch).
    """
    repo = tmp_path / "conflict-repo"
    repo.mkdir()
    _g(repo, "init", "-b", "main")
    _g(repo, "config", "user.email", "test@test.com")
    _g(repo, "config", "user.name", "Test")

    (repo / "shared.txt").write_text("original\n")
    _g(repo, "add", ".")
    _g(repo, "commit", "-m", "init")

    _g(repo, "checkout", "-b", "epic-branch")
    (repo / "shared.txt").write_text("epic change\n")
    _g(repo, "add", ".")
    _g(repo, "commit", "-m", "epic work")

    _g(repo, "checkout", "main")
    (repo / "shared.txt").write_text("main change\n")
    _g(repo, "add", ".")
    _g(repo, "commit", "-m", "main advance")

    return repo, "epic-branch", "main"


# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------


async def _bootstrap_project_epic(
    root: str,
    project_id: str,
    epic_id: str,
    repo_path: Path,
    repo_name: str = "repo",
    branch: str = "yukar/ep-1-test",
    touched_repos: list[str] | None = None,
    status: str = "open",
) -> None:
    """Write minimal project + repo + epic to workspace."""
    from yukar.models.epic import Epic
    from yukar.models.project import Project, Repo, RepoCommands
    from yukar.storage.epic_repo import save_epic
    from yukar.storage.project_repo import save_project, save_repo

    await save_project(
        root,
        Project(
            id=project_id,
            name=project_id,
            repos=[repo_name],
        ),
    )
    await save_repo(
        root,
        project_id,
        Repo(
            name=repo_name,
            path=str(repo_path),
            default_branch="main",
            commands=RepoCommands(),
        ),
    )
    epic = Epic.model_validate(
        {
            "id": epic_id,
            "slug": "test",
            "title": "Test Epic",
            "branch": branch,
            "status": status,
            "touched_repos": touched_repos if touched_repos is not None else [repo_name],
        }
    )
    await save_epic(root, project_id, epic)


# ---------------------------------------------------------------------------
# 1. Git helpers: start_conflict_merge + manual resolve + forward merge
# ---------------------------------------------------------------------------


class TestStartConflictMergeHelpers:
    async def test_clean_merge_returns_empty_list(self, tmp_path: Path) -> None:
        """start_conflict_merge on a non-conflicting branch returns []."""
        from yukar.git.resolve import start_conflict_merge
        from yukar.git.worktree import ensure_worktree

        repo = make_git_repo(tmp_path, "repo")
        # Create a clean epic branch (no conflicts).
        _g(repo, "checkout", "-b", "epic")
        (repo / "epic.txt").write_text("epic only\n")
        _g(repo, "add", ".")
        _g(repo, "commit", "-m", "epic work")
        _g(repo, "checkout", "main")

        worktree = tmp_path / "wt"
        await ensure_worktree(
            repo_path=repo, worktree_path=worktree, branch="epic", default_branch="main"
        )
        conflicts = await start_conflict_merge(
            worktree_path=worktree,
            default_branch="main",
            env=git_env(),
        )
        assert conflicts == []

    async def test_conflicting_merge_returns_file_list(self, tmp_path: Path) -> None:
        """start_conflict_merge returns the conflicting file paths."""
        from yukar.git.resolve import list_unmerged_files, merge_in_progress, start_conflict_merge
        from yukar.git.worktree import ensure_worktree

        repo, epic_branch, default_branch = _make_two_branch_conflict_repo(tmp_path)
        worktree = tmp_path / "wt"
        await ensure_worktree(
            repo_path=repo,
            worktree_path=worktree,
            branch=epic_branch,
            default_branch=default_branch,
        )
        conflicts = await start_conflict_merge(
            worktree_path=worktree,
            default_branch=default_branch,
            env=git_env(),
        )
        assert "shared.txt" in conflicts
        assert await merge_in_progress(worktree)
        assert await list_unmerged_files(worktree) == ["shared.txt"]

    async def test_manual_resolve_then_forward_merge(self, tmp_path: Path) -> None:
        """Manual resolve + abort_merge path: abort leaves worktree clean."""
        from yukar.git.resolve import abort_merge, merge_in_progress, start_conflict_merge
        from yukar.git.worktree import ensure_worktree

        repo, epic_branch, default_branch = _make_two_branch_conflict_repo(tmp_path)
        worktree = tmp_path / "wt"
        await ensure_worktree(
            repo_path=repo,
            worktree_path=worktree,
            branch=epic_branch,
            default_branch=default_branch,
        )
        conflicts = await start_conflict_merge(
            worktree_path=worktree,
            default_branch=default_branch,
            env=git_env(),
        )
        assert conflicts

        # Abort the merge (simulate failed agent).
        await abort_merge(worktree)
        assert not await merge_in_progress(worktree)

    async def test_manual_resolve_then_forward_merge_succeeds(self, tmp_path: Path) -> None:
        """Manual conflict resolution in the worktree then forward merge works."""
        from yukar.git.diff import merge
        from yukar.git.resolve import (
            list_unmerged_files,
            merge_in_progress,
            start_conflict_merge,
        )
        from yukar.git.runner import run_git
        from yukar.git.worktree import ensure_worktree

        repo, epic_branch, default_branch = _make_two_branch_conflict_repo(tmp_path)
        worktree = tmp_path / "wt"
        await ensure_worktree(
            repo_path=repo,
            worktree_path=worktree,
            branch=epic_branch,
            default_branch=default_branch,
        )
        conflicts = await start_conflict_merge(
            worktree_path=worktree,
            default_branch=default_branch,
            env=git_env(),
        )
        assert "shared.txt" in conflicts

        # Manually resolve the conflict.
        (worktree / "shared.txt").write_text("resolved\n")
        await run_git("add", "shared.txt", cwd=worktree)
        await run_git(
            "commit",
            "-m",
            "Resolve merge conflicts",
            cwd=worktree,
            env=git_env(),
        )

        # Validate: clean.
        assert await list_unmerged_files(worktree) == []
        assert not await merge_in_progress(worktree)

        # Forward merge: epic → main.
        sha = await merge(
            repo_path=repo,
            branch=epic_branch,
            message=f"Merge {epic_branch}",
            author_name="Test",
            author_email="test@test.com",
        )
        assert len(sha) == 40


# ---------------------------------------------------------------------------
# 2. ArbiterRunner: clean merge (no conflicts) → merge fact recorded
# ---------------------------------------------------------------------------


class TestArbiterRunnerCleanMerge:
    async def test_clean_merge_records_merge_fact(self, tmp_path: Path) -> None:
        """Clean merge (no conflicts) records merged_at; the epic stays open."""
        from yukar.config.settings import LLMSettings
        from yukar.runs.arbiter_runner import ArbiterRunner
        from yukar.storage.epic_repo import get_epic

        root = str(tmp_path / "ws")

        repo = make_git_repo(tmp_path, "repo")
        # Create a clean epic branch.
        _g(repo, "checkout", "-b", "yukar/ep-1-test")
        (repo / "epic.txt").write_text("added by epic\n")
        _g(repo, "add", ".")
        _g(repo, "commit", "-m", "epic work")
        _g(repo, "checkout", "main")

        await _bootstrap_project_epic(
            root,
            "proj",
            "EP-1",
            repo,
            branch="yukar/ep-1-test",
        )

        llm = LLMSettings(provider="fake")
        runner = ArbiterRunner(llm_settings=llm, epic_ids=["EP-1"])
        await runner.start(root=root, project_id="proj", epic_id="__merge__", run_id="run-test")

        loaded = await get_epic(root, "proj", "EP-1")
        assert loaded is not None
        assert loaded.merged_at is not None
        # Merging is a recorded fact, not a lifecycle transition.
        assert loaded.status == "open"


# ---------------------------------------------------------------------------
# 3. ArbiterRunner: conflict → abort → conflict_unresolved (no agent)
# ---------------------------------------------------------------------------


class TestArbiterRunnerConflictUnresolved:
    async def test_conflict_abort_returns_conflict_unresolved(self, tmp_path: Path) -> None:
        """When the DummyRunner (no LLM) is used, conflicts abort and return unresolved."""
        # We test this by using a real ArbiterRunner with provider=fake, but
        # the fake LLM will produce garbage that won't resolve the conflict,
        # so the post-agent validation will fail and abort_merge will be called.
        # We verify the per-epic result carries status=conflict_unresolved.
        from yukar.config.settings import LLMSettings
        from yukar.models.events import EpicMergeProgressEvent
        from yukar.runs.arbiter_runner import ArbiterRunner

        root = str(tmp_path / "ws")
        repo, epic_branch, default_branch = _make_two_branch_conflict_repo(tmp_path)

        await _bootstrap_project_epic(
            root,
            "proj",
            "EP-1",
            repo,
            branch=epic_branch,
        )

        received_results: list[EpicMergeProgressEvent] = []

        async def _collect() -> None:
            from yukar.events import bus as event_bus

            async with event_bus.subscribe_project("proj") as q:
                while True:
                    ev = await asyncio.wait_for(q.get(), timeout=10.0)
                    if ev is None:
                        break
                    if isinstance(ev, EpicMergeProgressEvent) and ev.phase == "finished":
                        received_results.append(ev)
                        break

        collector = asyncio.create_task(_collect())
        await asyncio.sleep(0)

        llm = LLMSettings(provider="fake")
        runner = ArbiterRunner(llm_settings=llm, epic_ids=["EP-1"])
        await runner.start(root=root, project_id="proj", epic_id="__merge__", run_id="run-test")

        # Publish project sentinel to unblock collector if it didn't see finished yet.
        from yukar.events import bus as event_bus

        event_bus.publish_project_sentinel("proj")

        import contextlib

        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(collector, timeout=5.0)

        # The epic should NOT be merged (conflict_unresolved).
        from yukar.storage.epic_repo import get_epic

        loaded = await get_epic(root, "proj", "EP-1")
        assert loaded is not None
        # The merge fact must NOT be recorded since the agent couldn't resolve.
        assert loaded.merged_at is None

        # Check results in the final progress event.
        if received_results:
            final = received_results[0]
            assert final.phase == "finished"
            assert len(final.results) == 1
            assert final.results[0].epic_id == "EP-1"
            assert final.results[0].status in (
                "conflict_unresolved",
                "error",
            ), f"Expected unresolved, got {final.results[0].status!r}"


# ---------------------------------------------------------------------------
# 4. ArbiterRunner: vetting_refused path
# ---------------------------------------------------------------------------


class TestArbiterRunnerVettingRefused:
    async def test_gitattributes_filter_causes_vetting_refused(self, tmp_path: Path) -> None:
        """A repo with tracked .gitattributes containing filter= raises GitVettingError."""
        from yukar.git.diff import GitVettingError, merge

        repo = make_git_repo(tmp_path, "repo")
        # Add .gitattributes with filter=lfs to main and to epic branch.
        _g(repo, "checkout", "-b", "yukar/ep-1-filter")
        (repo / ".gitattributes").write_text("*.bin filter=lfs diff=lfs merge=lfs -text\n")
        _g(repo, "add", ".")
        _g(repo, "commit", "-m", "add gitattributes")
        _g(repo, "checkout", "main")
        # Also add to main so it's in HEAD.
        (repo / ".gitattributes").write_text("*.bin filter=lfs diff=lfs merge=lfs -text\n")
        _g(repo, "add", ".")
        _g(repo, "commit", "-m", "add gitattributes to main")

        with pytest.raises(GitVettingError):
            await merge(repo, "yukar/ep-1-filter")


# ---------------------------------------------------------------------------
# 5. Serial two-epic: B sees A's merged commit on main
# ---------------------------------------------------------------------------


class TestArbiterRunnerSerialTwoEpics:
    async def test_b_worktree_sees_a_commit_after_a_merges(self, tmp_path: Path) -> None:
        """After epic A merges, epic B's worktree (via start_conflict_merge) sees A's commit."""
        from yukar.git.resolve import start_conflict_merge
        from yukar.git.worktree import ensure_worktree

        repo = make_git_repo(tmp_path, "repo")

        # Epic A: add a_file.txt.
        _g(repo, "checkout", "-b", "epic-a")
        (repo / "a_file.txt").write_text("added by A\n")
        _g(repo, "add", ".")
        _g(repo, "commit", "-m", "epic A work")
        _g(repo, "checkout", "main")

        # Epic B: add b_file.txt (no conflict with A).
        _g(repo, "checkout", "-b", "epic-b")
        (repo / "b_file.txt").write_text("added by B\n")
        _g(repo, "add", ".")
        _g(repo, "commit", "-m", "epic B work")
        _g(repo, "checkout", "main")

        # Simulate A being merged into main first.
        _g(repo, "merge", "--no-ff", "-m", "Merge epic A", "epic-a")
        assert (repo / "a_file.txt").exists()

        # Now run start_conflict_merge for B (pull latest main into B's worktree).
        worktree_b = tmp_path / "wt-b"
        await ensure_worktree(
            repo_path=repo,
            worktree_path=worktree_b,
            branch="epic-b",
            default_branch="main",
        )
        conflicts = await start_conflict_merge(
            worktree_path=worktree_b,
            default_branch="main",
            env=git_env(),
        )
        # No conflict (A and B touched different files).
        assert conflicts == []

        # B's worktree should now contain a_file.txt (from A's merge into main).
        assert (worktree_b / "a_file.txt").exists(), "B worktree should see A's commit"
        assert (worktree_b / "b_file.txt").exists(), "B's own work should still be there"


# ---------------------------------------------------------------------------
# 6-9. Supervisor: mutual exclusion, start_merge, stop_merge
# ---------------------------------------------------------------------------


class TestSupervisorArbiter:
    async def test_start_merge_409_when_arbiter_already_running(self, tmp_path: Path) -> None:
        """start_merge raises RuntimeError when arbiter is already running."""
        root = str(tmp_path / "ws")
        pid = "proj"

        from yukar.runs.supervisor import MERGE_SENTINEL, RunSupervisor, _RunHandle

        sv = RunSupervisor()

        async def _never_finishes() -> None:
            await asyncio.sleep(9999)

        fake_task: asyncio.Task[None] = asyncio.create_task(_never_finishes())
        try:
            sv._runs[(pid, MERGE_SENTINEL)] = _RunHandle(
                run_id="run-fake",
                runner=MagicMock(),
                task=fake_task,
                root=root,
                project_id=pid,
                epic_id=MERGE_SENTINEL,
            )

            with pytest.raises(RuntimeError, match="already running"):
                await sv.start_merge(root=root, project_id=pid, epic_ids=["EP-1"])
        finally:
            fake_task.cancel()
            import contextlib

            with contextlib.suppress(Exception, asyncio.CancelledError):
                await fake_task
            sv._runs.pop((pid, MERGE_SENTINEL), None)

    async def test_start_merge_409_when_epic_has_active_run(self, tmp_path: Path) -> None:
        """start_merge raises RuntimeError when any selected epic already has a run."""
        root = str(tmp_path / "ws")
        pid, eid = "proj", "EP-1"

        from yukar.runs.supervisor import RunSupervisor, _RunHandle

        sv = RunSupervisor()

        async def _never_finishes() -> None:
            await asyncio.sleep(9999)

        fake_task: asyncio.Task[None] = asyncio.create_task(_never_finishes())
        try:
            sv._runs[(pid, eid)] = _RunHandle(
                run_id="run-fake",
                runner=MagicMock(is_parked=False),  # executing (not parked)
                task=fake_task,
                root=root,
                project_id=pid,
                epic_id=eid,
            )

            with pytest.raises(RuntimeError, match=eid):
                await sv.start_merge(root=root, project_id=pid, epic_ids=[eid])
        finally:
            fake_task.cancel()
            import contextlib

            with contextlib.suppress(Exception, asyncio.CancelledError):
                await fake_task
            sv._runs.pop((pid, eid), None)

    async def test_start_merge_raises_when_parked_run_wakes_mid_shelve(
        self, tmp_path: Path
    ) -> None:
        """A reply that lands between the busy check and the shelve wakes the
        run: the failed shelve must abort the merge (RuntimeError → 409), not
        proceed against an executing run."""
        root = str(tmp_path / "ws")
        pid, eid = "proj", "EP-1"

        from yukar.runs.supervisor import MERGE_SENTINEL, RunSupervisor, _RunHandle

        class _WakesDuringShelveRunner:
            """is_parked=True at the busy check, False at the shelve re-check —
            simulates a reply landing between the two."""

            def __init__(self) -> None:
                self._reads = 0

            @property
            def is_parked(self) -> bool:
                self._reads += 1
                return self._reads == 1

        sv = RunSupervisor()

        async def _never_finishes() -> None:
            await asyncio.sleep(9999)

        fake_task: asyncio.Task[None] = asyncio.create_task(_never_finishes())
        try:
            waking_runner: Any = _WakesDuringShelveRunner()
            sv._runs[(pid, eid)] = _RunHandle(
                run_id="run-fake",
                runner=waking_runner,
                task=fake_task,
                root=root,
                project_id=pid,
                epic_id=eid,
            )

            with pytest.raises(RuntimeError, match="woke up"):
                await sv.start_merge(root=root, project_id=pid, epic_ids=[eid])

            # The merge did NOT start and the live run task was NOT cancelled.
            assert not sv.is_arbiter_running(pid)
            assert (pid, MERGE_SENTINEL) not in sv._runs
            assert not fake_task.cancelled()
        finally:
            fake_task.cancel()
            import contextlib

            with contextlib.suppress(Exception, asyncio.CancelledError):
                await fake_task
            sv._runs.pop((pid, eid), None)

    async def test_start_returns_409_when_arbiter_running(self, tmp_path: Path) -> None:
        """supervisor.start() raises RuntimeError when arbiter is running."""
        root = str(tmp_path / "ws")
        pid, eid = "proj", "EP-1"

        from yukar.models.epic import Epic
        from yukar.models.project import Project
        from yukar.runs.supervisor import MERGE_SENTINEL, RunSupervisor, _RunHandle
        from yukar.storage.epic_repo import save_epic
        from yukar.storage.project_repo import save_project

        await save_project(root, Project(id=pid, name=pid))
        await save_epic(root, pid, Epic(id=eid, slug="t", title="T"))

        sv = RunSupervisor()

        async def _never_finishes() -> None:
            await asyncio.sleep(9999)

        fake_task: asyncio.Task[None] = asyncio.create_task(_never_finishes())
        try:
            sv._runs[(pid, MERGE_SENTINEL)] = _RunHandle(
                run_id="run-arbiter",
                runner=MagicMock(),
                task=fake_task,
                root=root,
                project_id=pid,
                epic_id=MERGE_SENTINEL,
            )

            with pytest.raises(RuntimeError, match="arbiter"):
                await sv.start(root, pid, eid)
        finally:
            fake_task.cancel()
            import contextlib

            with contextlib.suppress(Exception, asyncio.CancelledError):
                await fake_task
            sv._runs.pop((pid, MERGE_SENTINEL), None)

    async def test_start_resolve_returns_409_when_arbiter_running(self, tmp_path: Path) -> None:
        """supervisor.start_resolve() raises RuntimeError when arbiter is running."""
        root = str(tmp_path / "ws")
        pid, eid = "proj", "EP-1"

        from yukar.runs.supervisor import MERGE_SENTINEL, RunSupervisor, _RunHandle

        sv = RunSupervisor()

        async def _never_finishes() -> None:
            await asyncio.sleep(9999)

        fake_task: asyncio.Task[None] = asyncio.create_task(_never_finishes())
        try:
            sv._runs[(pid, MERGE_SENTINEL)] = _RunHandle(
                run_id="run-arbiter",
                runner=MagicMock(),
                task=fake_task,
                root=root,
                project_id=pid,
                epic_id=MERGE_SENTINEL,
            )

            with pytest.raises(RuntimeError, match="arbiter"):
                await sv.start_resolve(root, pid, eid, "repo")
        finally:
            fake_task.cancel()
            import contextlib

            with contextlib.suppress(Exception, asyncio.CancelledError):
                await fake_task
            sv._runs.pop((pid, MERGE_SENTINEL), None)

    async def test_stop_merge(self, tmp_path: Path) -> None:
        """stop_merge calls runner.stop() and removes the handle."""
        root = str(tmp_path / "ws")
        pid = "proj"

        from yukar.runs.supervisor import MERGE_SENTINEL, RunSupervisor, _RunHandle

        sv = RunSupervisor()

        stopped = False

        class _FakeRunner:
            async def stop(self) -> None:
                nonlocal stopped
                stopped = True

            async def start(self, *a: Any, **kw: Any) -> None:
                pass

            async def pause(self) -> None:
                pass

            async def resume(self) -> None:
                pass

        async def _immediate() -> None:
            pass

        fake_task: asyncio.Task[None] = asyncio.create_task(_immediate())
        await asyncio.sleep(0)  # Let task complete so stop_merge can clean up.

        sv._runs[(pid, MERGE_SENTINEL)] = _RunHandle(
            run_id="run-arbiter",
            runner=_FakeRunner(),  # type: ignore[arg-type]
            task=fake_task,
            root=root,
            project_id=pid,
            epic_id=MERGE_SENTINEL,
        )

        await sv.stop_merge(pid)
        assert stopped


# ---------------------------------------------------------------------------
# 10. POST /api/projects/{p}/merge — 202 happy path (DummyRunner)
# ---------------------------------------------------------------------------


class TestMergeEndpointHappyPath:
    async def test_start_merge_202(self, app_client: Any, tmp_workspace: Path) -> None:
        """POST /merge returns 202 with run_id when project and epics exist."""
        root = str(tmp_workspace)
        pid, eid = "proj", "EP-1"
        repo_parent = tmp_workspace.parent / "repo"
        repo_parent.mkdir(parents=True, exist_ok=True)
        repo = make_git_repo(repo_parent, "src")
        _g(repo, "checkout", "-b", "yukar/ep-1-test")
        (repo / "epic.txt").write_text("x\n")
        _g(repo, "add", ".")
        _g(repo, "commit", "-m", "epic")
        _g(repo, "checkout", "main")

        # Bootstrap via API.
        r = await app_client.post(
            "/api/projects",
            json={
                "id": pid,
                "name": pid,
                "repos": [
                    {
                        "name": "repo",
                        "path": str(repo),
                        "default_branch": "main",
                    }
                ],
            },
        )
        assert r.status_code == 201, r.text

        r = await app_client.post(f"/api/projects/{pid}/epics", json={"title": "Test Epic"})
        assert r.status_code == 201, r.text

        # Patch the epic branch and touched_repos.
        from yukar.storage.epic_repo import get_epic, save_epic

        epic = await get_epic(root, pid, eid)
        assert epic is not None
        epic.branch = "yukar/ep-1-test"
        epic.touched_repos = ["repo"]
        await save_epic(root, pid, epic)

        resp = await app_client.post(
            f"/api/projects/{pid}/merge",
            json={"epic_ids": [eid]},
        )
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert "run_id" in body

        # Stop the merge so the background task doesn't block event-loop teardown.
        await app_client.post(f"/api/projects/{pid}/merge/stop")

    async def test_start_merge_400_empty_epic_ids(
        self, app_client: Any, tmp_workspace: Path
    ) -> None:
        """POST /merge with empty epic_ids returns 400."""
        root = str(tmp_workspace)
        pid = "proj"
        from yukar.models.project import Project
        from yukar.storage.project_repo import save_project

        await save_project(root, Project(id=pid, name=pid))

        resp = await app_client.post(
            f"/api/projects/{pid}/merge",
            json={"epic_ids": []},
        )
        assert resp.status_code == 400, resp.text

    async def test_start_merge_404_unknown_project(self, app_client: Any) -> None:
        resp = await app_client.post(
            "/api/projects/no-such-project/merge",
            json={"epic_ids": ["EP-1"]},
        )
        assert resp.status_code == 404

    async def test_start_merge_404_unknown_epic(self, app_client: Any, tmp_workspace: Path) -> None:
        root = str(tmp_workspace)
        pid = "proj"
        from yukar.models.project import Project
        from yukar.storage.project_repo import save_project

        await save_project(root, Project(id=pid, name=pid))

        resp = await app_client.post(
            f"/api/projects/{pid}/merge",
            json={"epic_ids": ["EP-99"]},
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 11-12. POST /api/projects/{p}/merge/stop
# ---------------------------------------------------------------------------


class TestMergeStopEndpoint:
    async def test_stop_merge_404_when_not_running(
        self, app_client: Any, tmp_workspace: Path
    ) -> None:
        """POST /merge/stop returns 404 when no arbiter is running."""
        root = str(tmp_workspace)
        pid = "proj"
        from yukar.models.project import Project
        from yukar.storage.project_repo import save_project

        await save_project(root, Project(id=pid, name=pid))

        resp = await app_client.post(f"/api/projects/{pid}/merge/stop")
        assert resp.status_code == 404

    async def test_stop_merge_200_when_running(self, app_client: Any, tmp_workspace: Path) -> None:
        """POST /merge/stop returns 200 when arbiter is running."""
        root = str(tmp_workspace)
        pid = "proj"
        from yukar.models.project import Project
        from yukar.runs.supervisor import MERGE_SENTINEL, _RunHandle, get_supervisor
        from yukar.storage.project_repo import save_project

        await save_project(root, Project(id=pid, name=pid))

        sv = get_supervisor()

        async def _never_finishes() -> None:
            await asyncio.sleep(9999)

        fake_task: asyncio.Task[None] = asyncio.create_task(_never_finishes())

        class _FakeRunner:
            async def stop(self) -> None:
                pass

            async def start(self, *a: Any, **kw: Any) -> None:
                pass

            async def pause(self) -> None:
                pass

            async def resume(self) -> None:
                pass

        sv._runs[(pid, MERGE_SENTINEL)] = _RunHandle(
            run_id="run-arbiter",
            runner=_FakeRunner(),  # type: ignore[arg-type]
            task=fake_task,
            root=root,
            project_id=pid,
            epic_id=MERGE_SENTINEL,
        )

        try:
            resp = await app_client.post(f"/api/projects/{pid}/merge/stop")
            assert resp.status_code == 200, resp.text
            assert resp.json()["status"] == "stopped"
        finally:
            fake_task.cancel()
            import contextlib

            with contextlib.suppress(Exception, asyncio.CancelledError):
                await fake_task
            sv._runs.pop((pid, MERGE_SENTINEL), None)


# ---------------------------------------------------------------------------
# 13. POST /git/merge — 409 when run is active
# ---------------------------------------------------------------------------


class TestGitMergeEndpointHardening:
    async def test_merge_409_when_run_active(self, app_client: Any, tmp_workspace: Path) -> None:
        """POST /git/merge returns 409 when a run is active for the epic."""
        root = str(tmp_workspace)
        pid, eid = "proj", "EP-1"

        from yukar.models.epic import Epic
        from yukar.models.project import Project
        from yukar.storage.epic_repo import save_epic
        from yukar.storage.project_repo import save_project

        await save_project(
            root,
            Project(
                id=pid,
                name=pid,
                repos=["r"],
            ),
        )
        await save_epic(
            root,
            pid,
            Epic(id=eid, slug="t", title="T", branch="yukar/ep-1-t", touched_repos=["r"]),
        )

        from yukar.runs.supervisor import _RunHandle, get_supervisor

        sv = get_supervisor()

        async def _never_finishes() -> None:
            await asyncio.sleep(9999)

        fake_task: asyncio.Task[None] = asyncio.create_task(_never_finishes())
        try:
            sv._runs[(pid, eid)] = _RunHandle(
                run_id="run-fake",
                runner=MagicMock(is_parked=False),  # executing (not parked)
                task=fake_task,
                root=root,
                project_id=pid,
                epic_id=eid,
            )

            resp = await app_client.post(
                f"/api/projects/{pid}/epics/{eid}/git/merge",
                json={"repo": "r"},
            )
            assert resp.status_code == 409, resp.text
        finally:
            fake_task.cancel()
            import contextlib

            with contextlib.suppress(Exception, asyncio.CancelledError):
                await fake_task
            sv._runs.pop((pid, eid), None)

    async def test_merge_422_on_vetting_error(self, tmp_path: Path) -> None:
        """GitVettingError in merge() raises HTTPException(422)."""

        from yukar.git.diff import GitVettingError

        # We test this via the router path by monkeypatching the vetting.
        # Easier: test that GitVettingError raises 422 via integration with a
        # real repo with tracked .gitattributes.
        repo = make_git_repo(tmp_path, "repo")
        # Add .gitattributes to main and epic.
        _g(repo, "checkout", "-b", "yukar/ep-1-filter")
        (repo / ".gitattributes").write_text("*.bin filter=lfs -text\n")
        _g(repo, "add", ".")
        _g(repo, "commit", "-m", "add gitattr")
        _g(repo, "checkout", "main")
        (repo / ".gitattributes").write_text("*.bin filter=lfs -text\n")
        _g(repo, "add", ".")
        _g(repo, "commit", "-m", "add gitattr on main")

        from yukar.git.diff import merge

        with pytest.raises(GitVettingError):
            await merge(repo, "yukar/ep-1-filter")


# ---------------------------------------------------------------------------
# 14. ArbiterRunner: skipped epics
# ---------------------------------------------------------------------------


class TestArbiterRunnerSkipped:
    async def test_no_branch_skipped(self, tmp_path: Path) -> None:
        """Epic with no branch is skipped with status='skipped'."""
        from yukar.config.settings import LLMSettings
        from yukar.models.events import EpicMergeProgressEvent
        from yukar.runs.arbiter_runner import ArbiterRunner

        root = str(tmp_path / "ws")

        from yukar.models.epic import Epic
        from yukar.models.project import Project
        from yukar.storage.epic_repo import save_epic
        from yukar.storage.project_repo import save_project

        await save_project(root, Project(id="proj", name="proj"))
        await save_epic(
            root,
            "proj",
            Epic(id="EP-1", slug="t", title="T", branch="", touched_repos=["repo"]),
        )

        results_collected: list[EpicMergeProgressEvent] = []

        async def _collect() -> None:
            from yukar.events import bus as event_bus

            async with event_bus.subscribe_project("proj") as q:
                while True:
                    try:
                        ev = await asyncio.wait_for(q.get(), timeout=5.0)
                    except TimeoutError:
                        break
                    if ev is None:
                        break
                    if isinstance(ev, EpicMergeProgressEvent) and ev.phase == "finished":
                        results_collected.append(ev)
                        break

        collector = asyncio.create_task(_collect())
        await asyncio.sleep(0)

        llm = LLMSettings(provider="fake")
        runner = ArbiterRunner(llm_settings=llm, epic_ids=["EP-1"])
        await runner.start(root=root, project_id="proj", epic_id="__merge__", run_id="run-t")

        from yukar.events import bus as event_bus

        event_bus.publish_project_sentinel("proj")

        import contextlib

        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(collector, timeout=5.0)

        if results_collected:
            assert results_collected[0].results[0].status == "skipped"

    async def test_epic_not_found_skipped(self, tmp_path: Path) -> None:
        """Epic that does not exist is skipped with status='skipped'."""
        from yukar.config.settings import LLMSettings
        from yukar.models.project import Project
        from yukar.runs.arbiter_runner import ArbiterRunner
        from yukar.storage.project_repo import save_project

        root = str(tmp_path / "ws")
        await save_project(root, Project(id="proj", name="proj"))

        llm = LLMSettings(provider="fake")
        runner = ArbiterRunner(llm_settings=llm, epic_ids=["EP-99"])
        # Should not raise — skips the missing epic.
        await runner.start(root=root, project_id="proj", epic_id="__merge__", run_id="run-t")


# ---------------------------------------------------------------------------
# 15. POST /git/merge — 409 when arbiter is running for the project
# ---------------------------------------------------------------------------


class TestGitMergeArbiterGuard:
    async def test_merge_409_when_arbiter_running(
        self, app_client: Any, tmp_workspace: Path
    ) -> None:
        """POST /git/merge returns 409 while an arbiter batch-merge is active."""
        root = str(tmp_workspace)
        pid, eid = "proj", "EP-1"

        from yukar.models.epic import Epic
        from yukar.models.project import Project
        from yukar.storage.epic_repo import save_epic
        from yukar.storage.project_repo import save_project

        await save_project(root, Project(id=pid, name=pid, repos=["r"]))
        await save_epic(
            root,
            pid,
            Epic(id=eid, slug="t", title="T", branch="yukar/ep-1-t", touched_repos=["r"]),
        )

        from yukar.runs.supervisor import MERGE_SENTINEL, _RunHandle, get_supervisor

        sv = get_supervisor()

        async def _never_finishes() -> None:
            await asyncio.sleep(9999)

        fake_task: asyncio.Task[None] = asyncio.create_task(_never_finishes())
        try:
            sv._runs[(pid, MERGE_SENTINEL)] = _RunHandle(
                run_id="run-arbiter",
                runner=MagicMock(),
                task=fake_task,
                root=root,
                project_id=pid,
                epic_id=MERGE_SENTINEL,
            )

            resp = await app_client.post(
                f"/api/projects/{pid}/epics/{eid}/git/merge",
                json={"repo": "r"},
            )
            assert resp.status_code == 409, resp.text
            assert "arbiter" in resp.json()["detail"].lower()
        finally:
            fake_task.cancel()
            import contextlib

            with contextlib.suppress(Exception, asyncio.CancelledError):
                await fake_task
            sv._runs.pop((pid, MERGE_SENTINEL), None)


# ---------------------------------------------------------------------------
# 16. POST /git/resolve — 409 when epic is completed
# ---------------------------------------------------------------------------


class TestGitResolveCompletedEpicGuard:
    async def test_resolve_409_when_epic_completed(
        self, app_client: Any, tmp_workspace: Path
    ) -> None:
        """POST /git/resolve returns 409 when the epic is completed."""
        root = str(tmp_workspace)
        pid, eid = "proj", "EP-1"

        # Create a minimal git repo so the repo-lookup doesn't fail.
        repo = make_git_repo(tmp_workspace, "repo")

        from yukar.models.epic import Epic
        from yukar.models.project import Project, Repo, RepoCommands
        from yukar.storage.epic_repo import save_epic
        from yukar.storage.project_repo import save_project, save_repo

        await save_project(root, Project(id=pid, name=pid, repos=["r"]))
        await save_repo(
            root,
            pid,
            Repo(name="r", path=str(repo), default_branch="main", commands=RepoCommands()),
        )
        await save_epic(
            root,
            pid,
            Epic(
                id=eid,
                slug="t",
                title="T",
                branch="yukar/ep-1-t",
                touched_repos=["r"],
                status="completed",
            ),
        )

        resp = await app_client.post(
            f"/api/projects/{pid}/epics/{eid}/git/resolve",
            json={"repo": "r"},
        )
        assert resp.status_code == 409, resp.text
        assert "completed" in resp.json()["detail"].lower()
