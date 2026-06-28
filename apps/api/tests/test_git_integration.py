"""Tests for M4 Git integration.

Covers:
1. merge() conflict detection returns exact file paths (not message fragments)
2. prune: normal case + force=False on unmerged branch
3. diff summary: multi-repo aggregation
4. resolve runner E2E with FakeModel (provider=fake, YUKAR_FAKE_SCRIPT pattern)
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests._helpers import make_git_repo

# ---------------------------------------------------------------------------
# Git test helpers (shared with orchestration tests)
# ---------------------------------------------------------------------------


def _git_env() -> dict[str, str]:
    return {
        **os.environ,
        "GIT_AUTHOR_NAME": "T",
        "GIT_AUTHOR_EMAIL": "t@t.com",
        "GIT_COMMITTER_NAME": "T",
        "GIT_COMMITTER_EMAIL": "t@t.com",
    }


def _make_conflict_repo(tmp_path: Path) -> Path:
    """Create a repo where merging 'feature' into 'main' causes a conflict."""
    repo = tmp_path / "conflict-repo"
    repo.mkdir()
    env = _git_env()

    def g(*args: str) -> str:
        r = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, env=env)
        assert r.returncode == 0, f"git {args}: {r.stderr}"
        return r.stdout.strip()

    g("init", "-b", "main")
    g("config", "user.email", "t@t.com")
    g("config", "user.name", "T")

    # Initial shared file
    (repo / "shared.txt").write_text("line1\n")
    (repo / "clean.txt").write_text("no conflict\n")
    g("add", ".")
    g("commit", "-m", "init")

    # Feature branch changes shared.txt
    g("checkout", "-b", "feature")
    (repo / "shared.txt").write_text("feature change\n")
    g("add", ".")
    g("commit", "-m", "feature")

    # Back to main, also change shared.txt → conflict
    g("checkout", "main")
    (repo / "shared.txt").write_text("main change\n")
    g("add", ".")
    g("commit", "-m", "main change")

    return repo


# ---------------------------------------------------------------------------
# 1. merge() conflict detection: actual file paths
# ---------------------------------------------------------------------------


class TestMergeConflictDetection:
    """Verify that MergeConflictError.conflicts contains real file paths."""

    async def test_conflict_returns_file_paths(self, tmp_path: Path) -> None:
        """conflicts must contain the actual file path, not a message fragment."""
        from yukar.git.diff import MergeConflictError, merge

        repo = _make_conflict_repo(tmp_path)

        with pytest.raises(MergeConflictError) as exc_info:
            await merge(repo, "feature")

        conflicts = exc_info.value.conflicts
        assert conflicts, "Expected at least one conflict file"
        # Each entry must be a bare filename / relative path — not
        # "Merge conflict in shared.txt" (the old buggy format)
        for c in conflicts:
            assert "Merge conflict in" not in c, (
                f"conflicts contained a message fragment instead of a path: {c!r}"
            )
            assert c.strip() == c, "Path must not have leading/trailing whitespace"
        assert "shared.txt" in conflicts, f"shared.txt should be in conflicts, got: {conflicts}"

    async def test_conflict_repo_clean_after_abort(self, tmp_path: Path) -> None:
        """After a failed merge, the repo should be back on main (clean)."""
        from yukar.git.diff import MergeConflictError, merge
        from yukar.git.runner import run_git

        repo = _make_conflict_repo(tmp_path)

        with pytest.raises(MergeConflictError):
            await merge(repo, "feature")

        # No MERGE_HEAD should remain (merge was aborted).
        result = await run_git("rev-parse", "--verify", "MERGE_HEAD", cwd=repo, check=False)
        assert result.returncode != 0, "MERGE_HEAD should not exist after abort"

        # Status should be clean (no untracked/modified from failed merge).
        status = await run_git("status", "--porcelain", cwd=repo, check=False)
        assert status.stdout.strip() == "", (
            f"Expected clean status after abort, got: {status.stdout!r}"
        )

    async def test_successful_merge_returns_sha(self, tmp_path: Path) -> None:
        """Clean merge returns a 40-char SHA."""
        from yukar.git.diff import merge

        repo = make_git_repo(tmp_path, "clean-repo")
        env = _git_env()

        def g(*args: str) -> str:
            r = subprocess.run(
                ["git", *args], cwd=str(repo), capture_output=True, text=True, env=env
            )
            assert r.returncode == 0, f"git {args}: {r.stderr}"
            return r.stdout.strip()

        g("checkout", "-b", "feat")
        (repo / "new.txt").write_text("new\n")
        g("add", ".")
        g("commit", "-m", "add new")
        g("checkout", "main")

        sha = await merge(repo, "feat")
        assert len(sha) == 40, f"Expected 40-char SHA, got: {sha!r}"


# ---------------------------------------------------------------------------
# 2. prune
# ---------------------------------------------------------------------------


class TestPruneEndpoint:
    """Tests for POST /git/prune."""

    async def test_prune_removes_worktree_and_deletes_branch(
        self, app_client: Any, tmp_path: Path, tmp_workspace: Path
    ) -> None:
        """Normal prune: removes worktree and deletes merged branch."""
        import httpx

        assert isinstance(app_client, httpx.AsyncClient)

        # Build a repo with an epic branch that is already merged.
        repo = make_git_repo(tmp_path, "prune-repo")
        env = _git_env()

        def g(*args: str) -> str:
            r = subprocess.run(
                ["git", *args], cwd=str(repo), capture_output=True, text=True, env=env
            )
            assert r.returncode == 0, f"git {args}: {r.stderr}"
            return r.stdout.strip()

        # Create epic branch, make a commit, merge it back to main.
        epic_branch = "yukar/ep-1-prune-test"
        g("checkout", "-b", epic_branch)
        (repo / "newfile.txt").write_text("hello\n")
        g("add", ".")
        g("commit", "-m", "epic work")
        g("checkout", "main")
        g("merge", "--no-ff", "-m", "Merge epic", epic_branch)

        # Bootstrap project via API.
        r = await app_client.post(
            "/api/projects",
            json={
                "id": "prune-proj",
                "name": "Prune Project",
                "repos": [
                    {
                        "name": "prune-repo",
                        "path": str(repo),
                        "default_branch": "main",
                    }
                ],
            },
        )
        assert r.status_code == 201, r.text

        r = await app_client.post(
            "/api/projects/prune-proj/epics",
            json={"title": "Prune Epic"},
        )
        assert r.status_code == 201, r.text

        # Create a worktree for the epic so prune has something to remove.
        from yukar.config import paths as p
        from yukar.git.worktree import ensure_worktree

        worktree_path = p.worktree_dir(
            str(tmp_workspace), "prune-proj", "EP-1", "manager", "prune-repo"
        )
        await ensure_worktree(
            repo_path=repo,
            worktree_path=worktree_path,
            branch=epic_branch,
            default_branch="main",
        )
        assert worktree_path.exists(), "Worktree should exist before prune"

        # Patch the epic's branch so prune knows what to delete.
        r = await app_client.patch(
            "/api/projects/prune-proj/epics/EP-1",
            json={"status": "completed"},
        )
        assert r.status_code == 200, r.text

        # Directly update epic.branch via storage (PATCH doesn't expose branch).
        from yukar.storage.epic_repo import get_epic, save_epic

        epic = await get_epic(str(tmp_workspace), "prune-proj", "EP-1")
        assert epic is not None
        epic.branch = epic_branch
        epic.touched_repos = ["prune-repo"]
        await save_epic(str(tmp_workspace), "prune-proj", epic)

        # Run prune.
        r = await app_client.post(
            "/api/projects/prune-proj/epics/EP-1/git/prune",
            json={},  # repos=None → use touched_repos
        )
        assert r.status_code == 200, r.text
        results = r.json()
        assert len(results) == 1
        result = results[0]
        assert result["repo"] == "prune-repo"
        assert result["worktree_removed"] is True
        assert result["branch_deleted"] is True
        assert result["error"] is None

        # Worktree directory should be gone.
        assert not worktree_path.exists(), "Worktree should be removed after prune"

    async def test_prune_force_false_fails_on_unmerged_branch(
        self, app_client: Any, tmp_path: Path, tmp_workspace: Path
    ) -> None:
        """force=False with an unmerged branch should fail branch deletion."""
        import httpx

        assert isinstance(app_client, httpx.AsyncClient)

        # Create a repo with an unmerged branch.
        repo = make_git_repo(tmp_path, "unmerged-repo")
        env = _git_env()

        def g(*args: str) -> str:
            r = subprocess.run(
                ["git", *args], cwd=str(repo), capture_output=True, text=True, env=env
            )
            assert r.returncode == 0, f"git {args}: {r.stderr}"
            return r.stdout.strip()

        epic_branch = "yukar/ep-1-unmerged"
        g("checkout", "-b", epic_branch)
        (repo / "unmerged.txt").write_text("unmerged\n")
        g("add", ".")
        g("commit", "-m", "unmerged work")
        g("checkout", "main")
        # Do NOT merge the branch — it stays unmerged.

        # Bootstrap project + epic.
        r = await app_client.post(
            "/api/projects",
            json={
                "id": "unmerged-proj",
                "name": "Unmerged Project",
                "repos": [
                    {
                        "name": "unmerged-repo",
                        "path": str(repo),
                        "default_branch": "main",
                    }
                ],
            },
        )
        assert r.status_code == 201, r.text
        r = await app_client.post(
            "/api/projects/unmerged-proj/epics",
            json={"title": "Unmerged Epic"},
        )
        assert r.status_code == 201, r.text

        from yukar.config import paths as p
        from yukar.git.worktree import ensure_worktree

        worktree_path = p.worktree_dir(
            str(tmp_workspace), "unmerged-proj", "EP-1", "manager", "unmerged-repo"
        )
        await ensure_worktree(
            repo_path=repo,
            worktree_path=worktree_path,
            branch=epic_branch,
            default_branch="main",
        )

        from yukar.storage.epic_repo import get_epic, save_epic

        epic = await get_epic(str(tmp_workspace), "unmerged-proj", "EP-1")
        assert epic is not None
        epic.branch = epic_branch
        epic.touched_repos = ["unmerged-repo"]
        await save_epic(str(tmp_workspace), "unmerged-proj", epic)

        # Prune with force=False → branch delete should fail (unmerged).
        r = await app_client.post(
            "/api/projects/unmerged-proj/epics/EP-1/git/prune",
            json={"force": False},
        )
        assert r.status_code == 200, r.text
        results = r.json()
        assert len(results) == 1
        result = results[0]
        assert result["repo"] == "unmerged-repo"
        # Worktree was removed (force=False still removes worktree; only branch is protected).
        assert result["worktree_removed"] is True
        # Branch delete should have failed.
        assert result["branch_deleted"] is False
        assert result["error"] is not None
        assert "branch delete failed" in result["error"]

    async def test_prune_force_false_dirty_worktree_returns_worktree_removed_false(
        self, app_client: Any, tmp_path: Path, tmp_workspace: Path
    ) -> None:
        """force=False on a dirty worktree: worktree_removed=False, error set, branch not deleted.

        A worktree with uncommitted changes cannot be removed without --force.
        git worktree remove (without --force) exits non-zero in that case.
        The prune endpoint must reflect this accurately: worktree_removed=False
        and the branch deletion must NOT proceed.
        """
        import httpx

        assert isinstance(app_client, httpx.AsyncClient)

        repo = make_git_repo(tmp_path, "dirty-wt-repo")
        env = _git_env()

        def g(*args: str) -> str:
            r = subprocess.run(
                ["git", *args], cwd=str(repo), capture_output=True, text=True, env=env
            )
            assert r.returncode == 0, f"git {args}: {r.stderr}"
            return r.stdout.strip()

        epic_branch = "yukar/ep-1-dirty"
        g("checkout", "-b", epic_branch)
        (repo / "tracked.txt").write_text("original\n")
        g("add", ".")
        g("commit", "-m", "initial on epic branch")
        g("checkout", "main")

        # Bootstrap project + epic.
        r = await app_client.post(
            "/api/projects",
            json={
                "id": "dirty-proj",
                "name": "Dirty Project",
                "repos": [
                    {
                        "name": "dirty-wt-repo",
                        "path": str(repo),
                        "default_branch": "main",
                    }
                ],
            },
        )
        assert r.status_code == 201, r.text
        r = await app_client.post(
            "/api/projects/dirty-proj/epics",
            json={"title": "Dirty Epic"},
        )
        assert r.status_code == 201, r.text

        from yukar.config import paths as p
        from yukar.git.worktree import ensure_worktree

        worktree_path = p.worktree_dir(
            str(tmp_workspace), "dirty-proj", "EP-1", "manager", "dirty-wt-repo"
        )
        await ensure_worktree(
            repo_path=repo,
            worktree_path=worktree_path,
            branch=epic_branch,
            default_branch="main",
        )

        # Make the worktree dirty: uncommitted modification.
        (worktree_path / "tracked.txt").write_text("dirty change\n")

        from yukar.storage.epic_repo import get_epic, save_epic

        epic = await get_epic(str(tmp_workspace), "dirty-proj", "EP-1")
        assert epic is not None
        epic.branch = epic_branch
        epic.touched_repos = ["dirty-wt-repo"]
        await save_epic(str(tmp_workspace), "dirty-proj", epic)

        # Prune without force — should fail on the dirty worktree.
        r = await app_client.post(
            "/api/projects/dirty-proj/epics/EP-1/git/prune",
            json={"force": False},
        )
        assert r.status_code == 200, r.text
        results = r.json()
        assert len(results) == 1
        result = results[0]
        assert result["repo"] == "dirty-wt-repo"
        # Worktree removal must be reported as failed.
        assert result["worktree_removed"] is False, (
            "worktree_removed must be False when git refuses force=False on dirty worktree"
        )
        # Branch deletion must NOT have proceeded.
        assert result["branch_deleted"] is False, (
            "branch_deleted must be False when worktree removal failed"
        )
        assert result["error"] is not None, "error must be set explaining the failure"

        # The worktree directory must still exist (it was NOT removed).
        assert worktree_path.exists(), "Worktree must still exist after failed prune"

    async def test_prune_409_when_run_active(self, app_client: Any, tmp_path: Path) -> None:
        """Prune returns 409 when a run is active for the epic."""
        from unittest.mock import patch

        import httpx

        assert isinstance(app_client, httpx.AsyncClient)

        r = await app_client.post(
            "/api/projects",
            json={"id": "p409", "name": "P409", "repos": []},
        )
        assert r.status_code == 201

        r = await app_client.post(
            "/api/projects/p409/epics",
            json={"title": "EP 409"},
        )
        assert r.status_code == 201

        # Fake the supervisor to appear as if a run is active.
        from yukar.runs.supervisor import get_supervisor

        sup = get_supervisor()
        with patch.object(sup, "is_running", return_value=True):
            r = await app_client.post(
                "/api/projects/p409/epics/EP-1/git/prune",
                json={},
            )
        assert r.status_code == 409


# ---------------------------------------------------------------------------
# 3. Diff summary (multi-repo aggregation)
# ---------------------------------------------------------------------------


class TestDiffSummary:
    """Tests for GET /git/diff/summary."""

    async def test_summary_empty_touched_repos(self, app_client: Any, tmp_workspace: Path) -> None:
        """Returns empty summary when epic.touched_repos is empty."""
        import httpx

        assert isinstance(app_client, httpx.AsyncClient)

        r = await app_client.post(
            "/api/projects",
            json={"id": "summary-proj", "name": "Summary Project", "repos": []},
        )
        assert r.status_code == 201
        r = await app_client.post(
            "/api/projects/summary-proj/epics",
            json={"title": "Summary Epic"},
        )
        assert r.status_code == 201

        r = await app_client.get(
            "/api/projects/summary-proj/epics/EP-1/git/diff/summary?mode=working"
        )
        assert r.status_code == 200
        data = r.json()
        assert data["repos"] == []
        assert data["total_files"] == 0
        assert data["total_added"] == 0
        assert data["total_deleted"] == 0

    async def test_summary_aggregates_multiple_repos(
        self, tmp_path: Path, tmp_workspace: Path
    ) -> None:
        """get_diff_summary aggregates adds/deletes across multiple repos."""
        from yukar.git.diff import get_diff_summary

        # Two repos, each with staged + committed changes on an epic branch.
        def make_repo_with_changes(name: str) -> tuple[Path, str | None, str]:
            """Return (repo_path, branch, default_branch) after creating test content."""
            repo = make_git_repo(tmp_path, name)
            env = _git_env()

            def g(*args: str) -> str:
                r = subprocess.run(
                    ["git", *args], cwd=str(repo), capture_output=True, text=True, env=env
                )
                assert r.returncode == 0, f"git {args}: {r.stderr}"
                return r.stdout.strip()

            branch = f"yukar/ep-1-{name}"
            g("checkout", "-b", branch)
            # Add 3 lines to one file.
            (repo / "added.txt").write_text("line1\nline2\nline3\n")
            g("add", ".")
            g("commit", "-m", f"add content for {name}")
            return repo, branch, "main"

        repo1, branch1, default1 = make_repo_with_changes("repo1")
        repo2, branch2, default2 = make_repo_with_changes("repo2")

        repos = [
            (repo1, "repo1", branch1, default1),
            (repo2, "repo2", branch2, default2),
        ]

        summary = await get_diff_summary(repos, mode="epic")

        assert len(summary.repos) == 2, "Should have one entry per repo"
        repo_names = {r.repo for r in summary.repos}
        assert repo_names == {"repo1", "repo2"}

        # Each repo added 3 lines → total_added = 6 (README.md has no additions vs main).
        for repo_summary in summary.repos:
            assert repo_summary.files >= 1, f"Expected at least 1 file for {repo_summary.repo}"
            assert repo_summary.added >= 3, (
                f"Expected >=3 added lines for {repo_summary.repo}, got {repo_summary.added}"
            )

        assert summary.total_added >= 6
        assert summary.total_files >= 2

    async def test_summary_skips_repos_with_no_branch(self, tmp_path: Path) -> None:
        """Repos with no epic branch return zero counts, not an error."""
        from yukar.git.diff import get_diff_summary

        repo = make_git_repo(tmp_path, "no-branch-repo")

        # Pass branch=None — the repo has no epic branch yet.
        repos = [(repo, "no-branch-repo", None, "main")]
        summary = await get_diff_summary(repos, mode="epic")

        assert len(summary.repos) == 1
        assert summary.repos[0].files == 0
        assert summary.repos[0].added == 0
        assert summary.repos[0].deleted == 0
        assert summary.total_files == 0


# ---------------------------------------------------------------------------
# 4. ResolveRunner E2E with FakeModel
# ---------------------------------------------------------------------------


def _make_workspace(tmp_path: Path) -> tuple[str, str, str]:
    root = str(tmp_path / "ws")
    project_id = "proj"
    epic_id = "EP-1"
    return root, project_id, epic_id


async def _bootstrap(
    root: str,
    project_id: str,
    epic_id: str,
    repo_path: Path,
    epic_branch: str = "yukar/ep-1-test-epic",
) -> None:
    """Bootstrap workspace, project, repo, and epic for resolve runner tests."""
    from yukar.models.epic import Epic
    from yukar.models.project import Project, Repo, RepoCommands
    from yukar.storage.epic_repo import save_epic
    from yukar.storage.project_repo import save_project, save_repo

    project = Project(id=project_id, name=project_id, status="active", repos=[repo_path.name])
    await save_project(root, project)

    repo = Repo(
        name=repo_path.name,
        path=str(repo_path),
        default_branch="main",
        commands=RepoCommands(allow=["git", "pytest"], deny=[]),
    )
    await save_repo(root, project_id, repo)

    epic = Epic(
        id=epic_id,
        slug="test-epic",
        title="Test Epic",
        description="A test epic for conflict resolution.",
        branch=epic_branch,
    )
    await save_epic(root, project_id, epic)


def _make_conflict_repo_diverged(tmp_path: Path) -> tuple[Path, str, str]:
    """Create a repo with diverged branches that will cause a merge conflict.

    Returns (repo_path, epic_branch, default_branch).
    The epic branch and main both modify 'shared.txt' differently.
    No worktree is created here — the runner creates it via ensure_worktree.
    """
    repo = tmp_path / "conflict-wt-repo"
    repo.mkdir()
    env = _git_env()

    def g(*args: str) -> str:
        r = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, env=env)
        assert r.returncode == 0, f"git {args}: {r.stderr}"
        return r.stdout.strip()

    g("init", "-b", "main")
    g("config", "user.email", "t@t.com")
    g("config", "user.name", "T")

    # Initial shared file
    (repo / "shared.txt").write_text("original content\n")
    g("add", ".")
    g("commit", "-m", "initial")

    # Epic branch changes shared.txt
    epic_branch = "yukar/ep-1-resolve-test"
    g("checkout", "-b", epic_branch)
    (repo / "shared.txt").write_text("epic branch content\n")
    g("add", ".")
    g("commit", "-m", "epic change")

    # Main also changes shared.txt → diverged (conflict on merge)
    g("checkout", "main")
    (repo / "shared.txt").write_text("main branch content\n")
    g("add", ".")
    g("commit", "-m", "main change")

    return repo, epic_branch, "main"


class TestResolveRunner:
    """E2E tests for the conflict-resolution runner using FakeModel."""

    async def test_resolve_runner_e2e_fake(self, tmp_path: Path) -> None:
        """Full resolve run: start_conflict_merge → fake agent resolves → validation passes.

        The FakeModel script:
          1. fs_read shared.txt (sees conflict markers)
          2. fs_write shared.txt (resolved content, no markers)
          3. git_add
          4. git_commit "Resolve merge conflicts"

        After the run, MERGE_HEAD must be gone and no unmerged files remain.
        """
        from unittest.mock import patch

        from yukar.config import paths as p
        from yukar.config.settings import LLMSettings
        from yukar.events import bus as event_bus
        from yukar.llm.fake import FakeModel, TextTurn, ToolUseTurn
        from yukar.runs.resolve_runner import ResolveRunner

        repo, epic_branch, default_branch = _make_conflict_repo_diverged(tmp_path)
        root, project_id, epic_id = _make_workspace(tmp_path)
        await _bootstrap(root, project_id, epic_id, repo, epic_branch)

        # The worktree will be created by the runner via ensure_worktree.
        worktree = p.worktree_dir(root, project_id, epic_id, "manager", repo.name)

        # The fake worker script:
        # 1. Read the conflicted file to understand the markers.
        # 2. Write the resolved version (no markers).
        # 3. Stage and commit to complete the merge.
        worker_script = [
            ToolUseTurn(
                tool_name="fs_read",
                tool_input={"path": "shared.txt"},
            ),
            ToolUseTurn(
                tool_name="fs_write",
                tool_input={"path": "shared.txt", "content": "resolved content\n"},
            ),
            ToolUseTurn(
                tool_name="git_add",
                tool_input={"paths": "shared.txt"},
            ),
            ToolUseTurn(
                tool_name="git_commit",
                tool_input={"message": "Resolve merge conflicts"},
            ),
            TextTurn("All conflicts resolved and committed."),
        ]

        def fake_create_model(settings: Any, role: Any = None) -> FakeModel:
            return FakeModel(script=list(worker_script))

        events_received: list[Any] = []

        async def _collect() -> None:
            async for ev in event_bus.event_stream(project_id, epic_id):
                events_received.append(ev)

        collector = asyncio.create_task(_collect())
        await asyncio.sleep(0)

        with patch("yukar.runs.resolve_runner.create_model", side_effect=fake_create_model):
            runner = ResolveRunner(
                llm_settings=LLMSettings(provider="fake"),
                repo_name=repo.name,
                git_author_name="yukar",
                git_author_email="yukar@localhost",
            )
            run_id = "resolve-test-run"
            await runner.start(root, project_id, epic_id, run_id)

        await asyncio.wait_for(collector, timeout=5.0)

        # --- Validate events ---
        event_types = [getattr(ev, "type", None) for ev in events_received]
        assert "run_started" in event_types, f"Missing run_started in {event_types}"
        assert "worker_started" in event_types, f"Missing worker_started in {event_types}"
        assert "worker_completed" in event_types, f"Missing worker_completed in {event_types}"
        assert "run_completed" in event_types, f"Missing run_completed in {event_types}"
        assert "run_failed" not in event_types, (
            f"Unexpected run_failed in {event_types}; runner must have validated cleanly"
        )

        # --- Validate git state ---
        from yukar.git.resolve import list_unmerged_files, merge_in_progress

        unmerged = await list_unmerged_files(worktree)
        assert unmerged == [], f"Expected no unmerged files after resolve, got: {unmerged}"

        in_progress = await merge_in_progress(worktree)
        assert not in_progress, "MERGE_HEAD should be gone after resolve commit"

        # --- Validate resolved file content ---
        resolved_content = (worktree / "shared.txt").read_text()
        assert "<<<<<<" not in resolved_content, "Conflict markers must be removed"
        assert "=======" not in resolved_content, "Conflict markers must be removed"
        assert ">>>>>>" not in resolved_content, "Conflict markers must be removed"
        assert resolved_content.strip() == "resolved content", (
            f"Expected 'resolved content', got: {resolved_content!r}"
        )

    async def test_resolve_runner_clean_merge_no_agent(self, tmp_path: Path) -> None:
        """If the merge completes cleanly (no conflicts), no agent is run."""
        from unittest.mock import patch

        from yukar.config.settings import LLMSettings
        from yukar.events import bus as event_bus
        from yukar.llm.fake import FakeModel, TextTurn
        from yukar.runs.resolve_runner import ResolveRunner

        # Create a repo where the merge will NOT conflict (different files).
        repo = make_git_repo(tmp_path, "clean-merge-repo")
        env = _git_env()

        def g(*args: str) -> str:
            r = subprocess.run(
                ["git", *args], cwd=str(repo), capture_output=True, text=True, env=env
            )
            assert r.returncode == 0, f"git {args}: {r.stderr}"
            return r.stdout.strip()

        epic_branch = "yukar/ep-1-clean"
        g("checkout", "-b", epic_branch)
        (repo / "epic_file.txt").write_text("epic only\n")
        g("add", ".")
        g("commit", "-m", "epic adds new file")
        g("checkout", "main")
        # main adds a different file, no overlap → clean merge
        (repo / "main_file.txt").write_text("main only\n")
        g("add", ".")
        g("commit", "-m", "main adds different file")

        # Set up workspace.
        root, project_id, epic_id = _make_workspace(tmp_path)
        await _bootstrap(root, project_id, epic_id, repo, epic_branch)

        # Create worktree for epic branch.
        from yukar.config import paths as p
        from yukar.git.worktree import ensure_worktree

        worktree_path = p.worktree_dir(root, project_id, epic_id, "manager", repo.name)
        await ensure_worktree(
            repo_path=repo,
            worktree_path=worktree_path,
            branch=epic_branch,
            default_branch="main",
        )

        model_call_count = [0]

        def fake_create_model(settings: Any, role: Any = None) -> FakeModel:
            model_call_count[0] += 1
            return FakeModel(script=[TextTurn("Should not be called.")])

        events_received: list[Any] = []

        async def _collect() -> None:
            async for ev in event_bus.event_stream(project_id, epic_id):
                events_received.append(ev)

        collector = asyncio.create_task(_collect())
        await asyncio.sleep(0)

        with patch("yukar.runs.resolve_runner.create_model", side_effect=fake_create_model):
            runner = ResolveRunner(
                llm_settings=LLMSettings(provider="fake"),
                repo_name=repo.name,
                git_author_name="yukar",
                git_author_email="yukar@localhost",
            )
            await runner.start(root, project_id, epic_id, "clean-resolve-run")

        await asyncio.wait_for(collector, timeout=5.0)

        # No agent should have been called (clean merge).
        assert model_call_count[0] == 0, (
            f"create_model should not be called for a clean merge, called {model_call_count[0]}x"
        )

        # Run should have completed successfully.
        event_types = [getattr(ev, "type", None) for ev in events_received]
        assert "run_completed" in event_types
        assert "run_failed" not in event_types

    async def test_supervisor_start_resolve_409_when_active(self, tmp_path: Path) -> None:
        """start_resolve raises RuntimeError if a run is already active."""
        from yukar.runs.supervisor import RunSupervisor

        sup = RunSupervisor()

        # Inject a fake active task.
        from unittest.mock import MagicMock

        from yukar.runs.runner import DummyRunner
        from yukar.runs.supervisor import _RunHandle

        mock_task = MagicMock()
        mock_task.done.return_value = False

        sup._runs[("proj", "EP-1")] = _RunHandle(
            run_id="active-run",
            runner=DummyRunner(),
            task=mock_task,
            root="/tmp",
            project_id="proj",
            epic_id="EP-1",
        )

        with pytest.raises(RuntimeError, match="already active"):
            await sup.start_resolve(
                root=str(tmp_path / "ws"),
                project_id="proj",
                epic_id="EP-1",
                repo_name="repo",
            )


# ---------------------------------------------------------------------------
# 5. git/resolve module unit tests
# ---------------------------------------------------------------------------


class TestGitResolveHelpers:
    """Unit tests for git/resolve.py helper functions."""

    async def test_start_conflict_merge_returns_conflict_files(self, tmp_path: Path) -> None:
        """start_conflict_merge returns conflicting file paths when merge fails."""
        from yukar.git.resolve import start_conflict_merge
        from yukar.git.worktree import ensure_worktree

        repo = make_git_repo(tmp_path, "scm-repo")
        env = _git_env()

        def g(*args: str) -> str:
            r = subprocess.run(
                ["git", *args], cwd=str(repo), capture_output=True, text=True, env=env
            )
            assert r.returncode == 0, f"git {args}: {r.stderr}"
            return r.stdout.strip()

        # Diverge main and epic branch on same file.
        epic_branch = "yukar/ep-1-scm"
        g("checkout", "-b", epic_branch)
        (repo / "conflict.txt").write_text("epic side\n")
        g("add", ".")
        g("commit", "-m", "epic side")
        g("checkout", "main")
        (repo / "conflict.txt").write_text("main side\n")
        g("add", ".")
        g("commit", "-m", "main side")

        worktree = tmp_path / "wt-scm"
        await ensure_worktree(
            repo_path=repo,
            worktree_path=worktree,
            branch=epic_branch,
            default_branch="main",
        )

        git_env_dict = {k: v for k, v in env.items() if k.startswith("GIT_")}
        conflicts = await start_conflict_merge(worktree, "main", env=git_env_dict)

        assert "conflict.txt" in conflicts
        # Markers should be present in the worktree.
        content = (worktree / "conflict.txt").read_text()
        assert "<<<<<<<" in content, "Conflict markers must be in the file"

    async def test_merge_in_progress_detects_merge_head(self, tmp_path: Path) -> None:
        """merge_in_progress returns True when MERGE_HEAD exists."""
        from yukar.git.resolve import merge_in_progress, start_conflict_merge
        from yukar.git.worktree import ensure_worktree

        repo = make_git_repo(tmp_path, "mip-repo")
        env = _git_env()

        def g(*args: str) -> str:
            r = subprocess.run(
                ["git", *args], cwd=str(repo), capture_output=True, text=True, env=env
            )
            assert r.returncode == 0, f"git {args}: {r.stderr}"
            return r.stdout.strip()

        epic_branch = "yukar/ep-1-mip"
        g("checkout", "-b", epic_branch)
        (repo / "mip.txt").write_text("branch content\n")
        g("add", ".")
        g("commit", "-m", "branch")
        g("checkout", "main")
        (repo / "mip.txt").write_text("main content\n")
        g("add", ".")
        g("commit", "-m", "main")

        worktree = tmp_path / "wt-mip"
        await ensure_worktree(
            repo_path=repo, worktree_path=worktree, branch=epic_branch, default_branch="main"
        )

        env_dict = {k: v for k, v in env.items() if k.startswith("GIT_")}
        await start_conflict_merge(worktree, "main", env=env_dict)

        # After a conflicted merge, MERGE_HEAD should exist.
        assert await merge_in_progress(worktree), "MERGE_HEAD should exist after conflict"

    async def test_abort_merge_cleans_up(self, tmp_path: Path) -> None:
        """abort_merge removes MERGE_HEAD and clears conflict markers."""
        from yukar.git.resolve import abort_merge, merge_in_progress, start_conflict_merge
        from yukar.git.worktree import ensure_worktree

        repo = make_git_repo(tmp_path, "abort-repo")
        env = _git_env()

        def g(*args: str) -> str:
            r = subprocess.run(
                ["git", *args], cwd=str(repo), capture_output=True, text=True, env=env
            )
            assert r.returncode == 0, f"git {args}: {r.stderr}"
            return r.stdout.strip()

        epic_branch = "yukar/ep-1-abort"
        g("checkout", "-b", epic_branch)
        (repo / "abort.txt").write_text("branch\n")
        g("add", ".")
        g("commit", "-m", "branch")
        g("checkout", "main")
        (repo / "abort.txt").write_text("main\n")
        g("add", ".")
        g("commit", "-m", "main")

        worktree = tmp_path / "wt-abort"
        await ensure_worktree(
            repo_path=repo, worktree_path=worktree, branch=epic_branch, default_branch="main"
        )

        env_dict = {k: v for k, v in env.items() if k.startswith("GIT_")}
        await start_conflict_merge(worktree, "main", env=env_dict)
        assert await merge_in_progress(worktree), "Precondition: merge must be in progress"

        await abort_merge(worktree)

        assert not await merge_in_progress(worktree), "MERGE_HEAD should be gone after abort"
        # File should be back to the pre-merge content (no markers).
        content = (worktree / "abort.txt").read_text()
        assert "<<<<<<<" not in content, "Conflict markers must be removed after abort"
