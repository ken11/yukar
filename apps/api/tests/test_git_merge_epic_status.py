"""Tests for epic status finalization after POST /git/merge.

Covers:
  - Single-repo epic: merge → epic.status becomes 'merged' + EpicStatusChanged published.
  - Multi-repo epic: partial merge (1 of 2 repos) → status unchanged;
    final merge → status becomes 'merged'.
  - is_branch_merged unit tests: merged / not-merged / branch-absent / default-branch-absent.
  - Closed epic is never overwritten by merge finalization.
  - existing sha-return, 409-conflict, 422-vetting tests still pass (unchanged).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _make_repo(base: Path, name: str) -> Path:
    """Create a bare-minimum git repo under *base/name* and return its path."""
    repo = base / name
    repo.mkdir()
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "T",
        "GIT_AUTHOR_EMAIL": "t@t.com",
        "GIT_COMMITTER_NAME": "T",
        "GIT_COMMITTER_EMAIL": "t@t.com",
    }

    def git(*args: str) -> str:
        r = subprocess.run(
            ["git", *args],
            cwd=str(repo),
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        return r.stdout.strip()

    git("init", "-b", "main")
    git("config", "user.email", "t@t.com")
    git("config", "user.name", "T")
    (repo / "README.md").write_text("init\n")
    git("add", ".")
    git("commit", "-m", "init")
    return repo


def _add_branch_commit(repo: Path, branch: str) -> None:
    """Add the epic branch with one extra commit to *repo*."""
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "T",
        "GIT_AUTHOR_EMAIL": "t@t.com",
        "GIT_COMMITTER_NAME": "T",
        "GIT_COMMITTER_EMAIL": "t@t.com",
    }

    def git(*args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=str(repo),
            env=env,
            check=True,
            capture_output=True,
        )

    git("checkout", "-b", branch)
    (repo / "feature.py").write_text("# feature\n")
    git("add", ".")
    git("commit", "-m", "feat")
    git("checkout", "main")


# ---------------------------------------------------------------------------
# Unit tests for is_branch_merged
# ---------------------------------------------------------------------------


class TestIsBranchMerged:
    async def test_not_merged(self, tmp_path: Path) -> None:
        from yukar.git.diff import is_branch_merged

        repo = _make_repo(tmp_path, "r")
        branch = "yukar/ep-1-feat"
        _add_branch_commit(repo, branch)
        # Branch has commits not in main → not merged.
        assert await is_branch_merged(repo, branch) is False

    async def test_merged(self, tmp_path: Path) -> None:
        from yukar.git.diff import is_branch_merged, merge

        repo = _make_repo(tmp_path, "r")
        branch = "yukar/ep-1-feat"
        _add_branch_commit(repo, branch)
        await merge(repo, branch)
        assert await is_branch_merged(repo, branch) is True

    async def test_branch_absent_treated_as_merged(self, tmp_path: Path) -> None:
        """A branch that does not exist in the repo is treated as merged."""
        from yukar.git.diff import is_branch_merged

        repo = _make_repo(tmp_path, "r")
        assert await is_branch_merged(repo, "yukar/ep-99-nonexistent") is True

    async def test_default_branch_absent_returns_false(self, tmp_path: Path) -> None:
        """If the default branch itself doesn't exist, return False (fail-safe)."""
        from yukar.git.diff import is_branch_merged

        repo = _make_repo(tmp_path, "r")
        branch = "yukar/ep-1-feat"
        _add_branch_commit(repo, branch)
        # Use a non-existent default branch → is-ancestor check will fail → False.
        result = await is_branch_merged(repo, branch, default_branch="nonexistent-branch")
        assert result is False


# ---------------------------------------------------------------------------
# API-level tests via app_client
# ---------------------------------------------------------------------------


async def _setup_single_repo_project(
    client: object,
    repo_path: Path,
    project_id: str = "gproj",
    repo_name: str = "repo-a",
) -> str:
    """Create a project + epic and return the epic_id (e.g. 'EP-1')."""
    import httpx

    assert isinstance(client, httpx.AsyncClient)
    r = await client.post(
        "/api/projects",
        json={
            "id": project_id,
            "name": project_id,
            "repos": [
                {
                    "name": repo_name,
                    "path": str(repo_path),
                    "default_branch": "main",
                }
            ],
        },
    )
    assert r.status_code == 201, r.text
    r2 = await client.post(
        f"/api/projects/{project_id}/epics",
        json={"title": "Test Epic"},
    )
    assert r2.status_code == 201, r2.text
    return r2.json()["id"]


class TestSingleRepoMergeFinalizesEpic:
    """Single-repo epic: after merge → epic.status becomes 'merged'."""

    async def test_merge_sets_epic_status_merged(
        self, app_client: object, tmp_path: Path
    ) -> None:
        import httpx

        assert isinstance(app_client, httpx.AsyncClient)

        repo = _make_repo(tmp_path, "repo-a")
        epic_id = await _setup_single_repo_project(app_client, repo, "proj-single", "repo-a")

        # Get the epic to find the branch name.
        r = await app_client.get(f"/api/projects/proj-single/epics/{epic_id}")
        epic_branch = r.json()["branch"]

        # Create the branch in the real repo.
        _add_branch_commit(repo, epic_branch)

        # Perform the merge.
        r = await app_client.post(
            f"/api/projects/proj-single/epics/{epic_id}/git/merge",
            json={"repo": "repo-a"},
        )
        assert r.status_code == 200, r.text
        assert "sha" in r.json()

        # Epic status must now be 'merged'.
        r2 = await app_client.get(f"/api/projects/proj-single/epics/{epic_id}")
        assert r2.json()["status"] == "merged", r2.json()

    async def test_merge_publishes_epic_status_changed_event(
        self, app_client: object, tmp_path: Path
    ) -> None:
        import httpx

        from yukar.events import bus as event_bus
        from yukar.models.events import EpicStatusChangedEvent

        assert isinstance(app_client, httpx.AsyncClient)

        repo = _make_repo(tmp_path, "repo-a")
        epic_id = await _setup_single_repo_project(app_client, repo, "proj-evt", "repo-a")

        r = await app_client.get(f"/api/projects/proj-evt/epics/{epic_id}")
        epic_branch = r.json()["branch"]
        _add_branch_commit(repo, epic_branch)

        # Subscribe to the event bus before merging.
        received: list[object] = []
        async with event_bus.subscribe("proj-evt", epic_id) as q:
            merge_r = await app_client.post(
                f"/api/projects/proj-evt/epics/{epic_id}/git/merge",
                json={"repo": "repo-a"},
            )
            assert merge_r.status_code == 200, merge_r.text
            # Drain the queue (non-blocking).
            while not q.empty():
                received.append(q.get_nowait())

        status_events = [
            e for e in received if isinstance(e, EpicStatusChangedEvent) and e.status == "merged"
        ]
        assert status_events, f"No EpicStatusChangedEvent(status='merged') found in {received}"


class TestMultiRepoMergePartialThenFull:
    """Multi-repo epic: partial merge leaves status unchanged; final merge finalizes it."""

    async def _setup_two_repo_project(
        self,
        client: object,
        repo_a: Path,
        repo_b: Path,
    ) -> tuple[str, str]:
        """Create a two-repo project, return (project_id, epic_id)."""
        import httpx

        assert isinstance(client, httpx.AsyncClient)
        project_id = "proj-multi"
        r = await client.post(
            "/api/projects",
            json={
                "id": project_id,
                "name": project_id,
                "repos": [
                    {"name": "repo-a", "path": str(repo_a), "default_branch": "main"},
                    {"name": "repo-b", "path": str(repo_b), "default_branch": "main"},
                ],
            },
        )
        assert r.status_code == 201, r.text
        r2 = await client.post(
            f"/api/projects/{project_id}/epics",
            json={"title": "Multi Repo Epic"},
        )
        assert r2.status_code == 201, r2.text
        return project_id, r2.json()["id"]

    async def test_partial_merge_does_not_set_merged(
        self, app_client: object, tmp_path: Path
    ) -> None:
        import httpx

        assert isinstance(app_client, httpx.AsyncClient)

        repo_a = _make_repo(tmp_path, "repo-a")
        repo_b = _make_repo(tmp_path, "repo-b")
        project_id, epic_id = await self._setup_two_repo_project(
            app_client, repo_a, repo_b
        )

        r = await app_client.get(f"/api/projects/{project_id}/epics/{epic_id}")
        epic_branch = r.json()["branch"]

        # Create the branch in BOTH repos.
        _add_branch_commit(repo_a, epic_branch)
        _add_branch_commit(repo_b, epic_branch)

        # Merge only repo-a.
        r = await app_client.post(
            f"/api/projects/{project_id}/epics/{epic_id}/git/merge",
            json={"repo": "repo-a"},
        )
        assert r.status_code == 200, r.text

        # Epic status must NOT be 'merged' yet (repo-b is still unmerged).
        r2 = await app_client.get(f"/api/projects/{project_id}/epics/{epic_id}")
        assert r2.json()["status"] != "merged", r2.json()

    async def test_final_merge_sets_merged(
        self, app_client: object, tmp_path: Path
    ) -> None:
        import httpx

        assert isinstance(app_client, httpx.AsyncClient)

        repo_a = _make_repo(tmp_path, "repo-a")
        repo_b = _make_repo(tmp_path, "repo-b")
        project_id, epic_id = await self._setup_two_repo_project(
            app_client, repo_a, repo_b
        )

        r = await app_client.get(f"/api/projects/{project_id}/epics/{epic_id}")
        epic_branch = r.json()["branch"]

        _add_branch_commit(repo_a, epic_branch)
        _add_branch_commit(repo_b, epic_branch)

        # Merge repo-a first.
        r = await app_client.post(
            f"/api/projects/{project_id}/epics/{epic_id}/git/merge",
            json={"repo": "repo-a"},
        )
        assert r.status_code == 200, r.text

        # Status is still not 'merged'.
        r2 = await app_client.get(f"/api/projects/{project_id}/epics/{epic_id}")
        assert r2.json()["status"] != "merged", r2.json()

        # Merge repo-b — this is the final merge.
        r = await app_client.post(
            f"/api/projects/{project_id}/epics/{epic_id}/git/merge",
            json={"repo": "repo-b"},
        )
        assert r.status_code == 200, r.text

        # Now the epic must be 'merged'.
        r3 = await app_client.get(f"/api/projects/{project_id}/epics/{epic_id}")
        assert r3.json()["status"] == "merged", r3.json()


class TestClosedEpicNotOverwritten:
    """A closed epic must NOT be set to 'merged' by the finalization logic."""

    async def test_closed_epic_stays_closed_after_merge(
        self, app_client: object, tmp_path: Path
    ) -> None:
        import httpx

        assert isinstance(app_client, httpx.AsyncClient)

        repo = _make_repo(tmp_path, "repo-a")
        epic_id = await _setup_single_repo_project(app_client, repo, "proj-closed", "repo-a")

        r = await app_client.get(f"/api/projects/proj-closed/epics/{epic_id}")
        epic_branch = r.json()["branch"]
        _add_branch_commit(repo, epic_branch)

        # Close the epic first.
        r = await app_client.post(
            f"/api/projects/proj-closed/epics/{epic_id}/close",
        )
        assert r.status_code == 200, r.text

        # Attempt to merge (this bypasses the 409 guard by using the git layer
        # directly — the router 409 check is for active runs, not closed status).
        # We test the finalization function directly to ensure it doesn't
        # overwrite 'closed'.
        from yukar.api.routers.git import _finalize_epic_if_all_merged
        from yukar.models.epic import Epic

        closed_epic = Epic(
            id=epic_id,
            slug="test",
            title="Test",
            branch=epic_branch,
            status="closed",
            touched_repos=["repo-a"],
        )
        # _finalize_epic_if_all_merged must not overwrite 'closed'.
        await _finalize_epic_if_all_merged("unused-root", "proj-closed", closed_epic)
        # status must remain 'closed' (in-memory, since we passed a fake root)
        assert closed_epic.status == "closed"
