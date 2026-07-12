"""Tests for the merge-fact recording after POST /git/merge.

Covers:
  - Single-repo epic: merge → ``merged_at`` recorded + EpicMergedEvent published,
    while ``epic.status`` stays "open" (merging never completes an epic).
  - Multi-repo epic: partial merge (1 of 2 repos) → no fact yet;
    final merge → ``merged_at`` recorded.
  - is_branch_merged unit tests: merged / not-merged / branch-absent / default-branch-absent.
  - Idempotence: an epic whose fact is already recorded is never re-recorded.
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


class TestSingleRepoMergeRecordsFact:
    """Single-repo epic: after merge → merged_at is recorded, status stays open."""

    async def test_merge_records_merged_at_and_keeps_epic_open(
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

        # The merge fact is recorded, and the epic stays open (1-bit lifecycle:
        # only the user completes an epic — merging is just a recorded fact).
        r2 = await app_client.get(f"/api/projects/proj-single/epics/{epic_id}")
        body = r2.json()
        assert body["merged_at"] is not None, body
        assert body["status"] == "open", body

    async def test_merge_publishes_epic_merged_event(
        self, app_client: object, tmp_path: Path
    ) -> None:
        import httpx

        from yukar.events import bus as event_bus
        from yukar.models.events import EpicMergedEvent

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

        merged_events = [e for e in received if isinstance(e, EpicMergedEvent)]
        assert merged_events, f"No EpicMergedEvent found in {received}"
        assert merged_events[0].epic_id == epic_id
        assert merged_events[0].merged_at is not None


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

    async def test_partial_merge_does_not_record_fact(
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

        # The merge fact must NOT be recorded yet (repo-b is still unmerged).
        r2 = await app_client.get(f"/api/projects/{project_id}/epics/{epic_id}")
        assert r2.json()["merged_at"] is None, r2.json()

    async def test_final_merge_records_fact(
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

        # The fact is still unrecorded.
        r2 = await app_client.get(f"/api/projects/{project_id}/epics/{epic_id}")
        assert r2.json()["merged_at"] is None, r2.json()

        # Merge repo-b — this is the final merge.
        r = await app_client.post(
            f"/api/projects/{project_id}/epics/{epic_id}/git/merge",
            json={"repo": "repo-b"},
        )
        assert r.status_code == 200, r.text

        # Now the fact must be recorded — and the epic still open.
        r3 = await app_client.get(f"/api/projects/{project_id}/epics/{epic_id}")
        assert r3.json()["merged_at"] is not None, r3.json()
        assert r3.json()["status"] == "open", r3.json()


class TestMergeFactIdempotent:
    """An epic whose merge fact is already recorded is never re-recorded."""

    async def test_already_recorded_fact_is_not_rewritten(self, tmp_path: Path) -> None:
        from datetime import UTC, datetime

        from yukar.api.routers.git import _finalize_epic_if_all_merged
        from yukar.models.epic import Epic

        recorded_at = datetime(2025, 1, 1, tzinfo=UTC)
        epic = Epic(
            id="EP-1",
            slug="test",
            title="Test",
            branch="yukar/ep-1-test",
            touched_repos=["repo-a"],
            merged_at=recorded_at,
        )
        # Early-returns on the recorded fact — never reaches storage or git
        # (the fake root would blow up otherwise).
        await _finalize_epic_if_all_merged("unused-root", "proj-x", epic)
        assert epic.merged_at == recorded_at



class TestRecordEpicMergedFreshRead:
    """record_epic_merged re-reads the epic under a lock: no stale rollback,
    no double publish."""

    async def test_stale_caller_cannot_roll_back_concurrent_patch(
        self, tmp_path: Path
    ) -> None:
        """A caller holding a stale Epic must not roll back a concurrent
        status change: only merged_at / updated_at are written, on the
        freshest on-disk state."""
        from yukar.models.epic import Epic
        from yukar.runs.merge_facts import record_epic_merged
        from yukar.storage.epic_repo import get_epic, save_epic

        root = str(tmp_path / "ws")
        pid, eid = "proj", "EP-1"
        await save_epic(
            root, pid, Epic(id=eid, slug="s", title="T", branch="yukar/ep-1-s")
        )

        # Simulate a PATCH that completes the epic while the merge caller
        # still holds the pre-PATCH snapshot in memory.
        fresh = await get_epic(root, pid, eid)
        assert fresh is not None
        fresh.status = "completed"
        await save_epic(root, pid, fresh)

        recorded = await record_epic_merged(root, pid, eid)
        assert recorded is True

        after = await get_epic(root, pid, eid)
        assert after is not None
        assert after.merged_at is not None
        # The concurrent status change survived — no stale-object rollback.
        assert after.status == "completed"

    async def test_concurrent_recorders_publish_exactly_once(
        self, tmp_path: Path
    ) -> None:
        """Two racing call sites (single-repo endpoint + arbiter) record the
        fact once and publish exactly one EpicMergedEvent."""
        import asyncio

        from yukar.events import bus as event_bus
        from yukar.models.epic import Epic
        from yukar.models.events import EpicMergedEvent
        from yukar.runs.merge_facts import record_epic_merged
        from yukar.storage.epic_repo import save_epic

        root = str(tmp_path / "ws")
        pid, eid = "proj", "EP-2"
        await save_epic(
            root, pid, Epic(id=eid, slug="s2", title="T2", branch="yukar/ep-2-s2")
        )

        received: list[object] = []
        async with event_bus.subscribe(pid, eid) as q:
            results = await asyncio.gather(
                record_epic_merged(root, pid, eid),
                record_epic_merged(root, pid, eid, run_id="run-arb"),
            )
            while not q.empty():
                received.append(q.get_nowait())

        assert sorted(results) == [False, True], results
        merged_events = [e for e in received if isinstance(e, EpicMergedEvent)]
        assert len(merged_events) == 1, f"expected exactly one event, got {merged_events}"

    async def test_missing_epic_is_a_noop(self, tmp_path: Path) -> None:
        from yukar.runs.merge_facts import record_epic_merged

        root = str(tmp_path / "ws")
        assert await record_epic_merged(root, "proj", "EP-GONE") is False
