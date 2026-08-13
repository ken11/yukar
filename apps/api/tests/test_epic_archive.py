"""Tests for POST /api/projects/{p}/epics/archive (move-out-of-sight archive).

Archiving is a LOCATION, not a status: the epic directory moves from
``epics/`` to ``archives/`` so it drops out of the list API (a pure
directory scan) without touching epic.yaml or the open ⇄ completed bit.

Covers:
1. Archive moves the directory and hides the epic from list/GET.
2. Trial worktrees are deregistered from the source repo before the move;
   the epic branch is deliberately left alone.
3. An epic with an active run is refused (per-epic error, batch continues).
4. Batch semantics: per-epic results in request order, unknown ids reported.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from tests._helpers import git_env, make_git_repo

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _bootstrap_project(
    app_client: Any, project_id: str, repos: list[dict[str, str]] | None = None
) -> None:
    r = await app_client.post(
        "/api/projects",
        json={"id": project_id, "name": project_id, "repos": repos or []},
    )
    assert r.status_code == 201, r.text


async def _create_epic(app_client: Any, project_id: str, title: str) -> str:
    r = await app_client.post(f"/api/projects/{project_id}/epics", json={"title": title})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _inject_fake_active_run(root: str, project_id: str, epic_id: str) -> Any:
    """Register a never-finishing run handle; caller must clean it up."""
    import asyncio
    from unittest.mock import MagicMock

    from yukar.runs.supervisor import _RunHandle, get_supervisor

    sv = get_supervisor()

    async def _never() -> None:
        await asyncio.sleep(9999)

    fake_task: asyncio.Task[None] = asyncio.create_task(_never())
    sv._runs[(project_id, epic_id)] = _RunHandle(
        run_id="run-fake",
        runner=MagicMock(is_parked=False),
        task=fake_task,
        root=root,
        project_id=project_id,
        epic_id=epic_id,
    )
    return fake_task


async def _cleanup_fake_run(fake_task: Any, project_id: str, epic_id: str) -> None:
    import asyncio
    import contextlib

    from yukar.runs.supervisor import get_supervisor

    fake_task.cancel()
    with contextlib.suppress(Exception, asyncio.CancelledError):
        await fake_task
    get_supervisor()._runs.pop((project_id, epic_id), None)


# ---------------------------------------------------------------------------
# 1. Move + hide
# ---------------------------------------------------------------------------


class TestArchiveMove:
    async def test_archive_moves_directory_and_hides_from_list(
        self, app_client: Any, tmp_workspace: Path
    ) -> None:
        from yukar.config import paths as p

        await _bootstrap_project(app_client, "arch-proj")
        epic_id = await _create_epic(app_client, "arch-proj", "Archive me")

        r = await app_client.post(
            "/api/projects/arch-proj/epics/archive", json={"epic_ids": [epic_id]}
        )
        assert r.status_code == 200, r.text
        results = r.json()
        assert results == [
            {"epic_id": epic_id, "archived": True, "error": None, "error_code": None}
        ]

        # Directory moved epics/ → archives/ with its contents intact.
        assert not p.epic_dir(str(tmp_workspace), "arch-proj", epic_id).exists()
        archived = p.archived_epic_dir(str(tmp_workspace), "arch-proj", epic_id)
        assert (archived / ".yukar" / "epic.yaml").is_file()

        # Gone from the list (even with include_completed) and from GET.
        r = await app_client.get("/api/projects/arch-proj/epics?include_completed=true")
        assert r.status_code == 200
        assert r.json() == []
        r = await app_client.get(f"/api/projects/arch-proj/epics/{epic_id}")
        assert r.status_code == 404

    async def test_archive_preserves_epic_status_bit(
        self, app_client: Any, tmp_workspace: Path
    ) -> None:
        """epic.yaml is moved untouched — archive is not a status transition."""
        import yaml

        from yukar.config import paths as p

        await _bootstrap_project(app_client, "arch-bit")
        epic_id = await _create_epic(app_client, "arch-bit", "Keep my bit")
        r = await app_client.patch(
            f"/api/projects/arch-bit/epics/{epic_id}", json={"status": "completed"}
        )
        assert r.status_code == 200, r.text

        r = await app_client.post(
            "/api/projects/arch-bit/epics/archive", json={"epic_ids": [epic_id]}
        )
        assert r.status_code == 200, r.text
        assert r.json()[0]["archived"] is True

        archived_yaml = (
            p.archived_epic_dir(str(tmp_workspace), "arch-bit", epic_id) / ".yukar" / "epic.yaml"
        )
        data = yaml.safe_load(archived_yaml.read_text())
        assert data["status"] == "completed"


# ---------------------------------------------------------------------------
# 2. Worktree deregistration
# ---------------------------------------------------------------------------


class TestArchiveWorktrees:
    async def test_archive_removes_worktree_keeps_branch(
        self, app_client: Any, tmp_path: Path, tmp_workspace: Path, monkeypatch: Any
    ) -> None:
        from yukar.api.routers import epics as epics_router
        from yukar.config import paths as p
        from yukar.git.worktree import ensure_worktree

        # The teardown must take the O(1) trash-rename fast path — the
        # synchronous `git worktree remove` fallback is what used to time the
        # request out at the proxy.  Make the slow path fail loudly.
        async def _slow_path_forbidden(**_kwargs: Any) -> tuple[bool, str | None]:
            raise AssertionError("slow worktree removal taken — expected the trash fast path")

        monkeypatch.setattr(epics_router, "remove_worktree", _slow_path_forbidden)

        repo = make_git_repo(tmp_path, "arch-repo")
        await _bootstrap_project(
            app_client,
            "arch-wt",
            repos=[{"name": "arch-repo", "path": str(repo), "default_branch": "main"}],
        )
        epic_id = await _create_epic(app_client, "arch-wt", "With worktree")

        branch = f"yukar/{epic_id.lower()}-with-worktree"
        worktree_path = p.worktree_dir(
            str(tmp_workspace), "arch-wt", epic_id, "manager", "arch-repo"
        )
        await ensure_worktree(
            repo_path=repo,
            worktree_path=worktree_path,
            branch=branch,
            default_branch="main",
        )
        # Dirty the worktree — archive must still remove it (force).
        (worktree_path / "wip.txt").write_text("uncommitted\n")
        assert worktree_path.exists()

        r = await app_client.post(
            "/api/projects/arch-wt/epics/archive", json={"epic_ids": [epic_id]}
        )
        assert r.status_code == 200, r.text
        assert r.json()[0]["archived"] is True, r.text

        env = git_env()

        def g(*args: str) -> str:
            res = subprocess.run(
                ["git", *args], cwd=str(repo), capture_output=True, text=True, env=env
            )
            assert res.returncode == 0, f"git {args}: {res.stderr}"
            return res.stdout.strip()

        # Registration gone from the source repo; the branch survives.
        assert str(worktree_path) not in g("worktree", "list")
        assert g("branch", "--list", branch) != ""

        # No worktree directory travelled along into archives/.
        archived = p.archived_epic_dir(str(tmp_workspace), "arch-wt", epic_id)
        assert archived.is_dir()
        leftover = archived / "worktrees" / "manager" / "arch-repo"
        assert not leftover.exists()

        # The checkout was renamed into the workspace trash (so the request
        # never pays for the deletion) and the background sweep removes it.
        from yukar.storage.trash import drain_sweeps

        await drain_sweeps()
        trash = p.trash_dir(str(tmp_workspace))
        assert not trash.is_dir() or list(trash.iterdir()) == []


# ---------------------------------------------------------------------------
# 3. Active-run guard + 4. batch semantics
# ---------------------------------------------------------------------------


class TestArchiveGuards:
    async def test_running_epic_is_refused_but_batch_continues(
        self, app_client: Any, tmp_workspace: Path
    ) -> None:
        from yukar.config import paths as p

        await _bootstrap_project(app_client, "arch-run")
        busy_id = await _create_epic(app_client, "arch-run", "Busy epic")
        idle_id = await _create_epic(app_client, "arch-run", "Idle epic")

        fake_task = _inject_fake_active_run(str(tmp_workspace), "arch-run", busy_id)
        try:
            r = await app_client.post(
                "/api/projects/arch-run/epics/archive",
                json={"epic_ids": [busy_id, idle_id]},
            )
            assert r.status_code == 200, r.text
            busy_res, idle_res = r.json()
            assert busy_res["epic_id"] == busy_id
            assert busy_res["archived"] is False
            assert busy_res["error_code"] == "run_active"
            assert "run is active" in (busy_res["error"] or "")
            assert idle_res == {
                "epic_id": idle_id,
                "archived": True,
                "error": None,
                "error_code": None,
            }
        finally:
            await _cleanup_fake_run(fake_task, "arch-run", busy_id)

        # The busy epic stayed in place.
        assert p.epic_dir(str(tmp_workspace), "arch-run", busy_id).is_dir()

    async def test_arbiter_merge_blocks_archive(
        self, app_client: Any, tmp_workspace: Path
    ) -> None:
        """A batch merge works inside the epics' trial worktrees; archiving any
        epic of the project while the arbiter runs must be refused."""
        from yukar.config import paths as p
        from yukar.runs.supervisor import MERGE_SENTINEL

        await _bootstrap_project(app_client, "arch-arb")
        epic_id = await _create_epic(app_client, "arch-arb", "Merge target")

        fake_task = _inject_fake_active_run(str(tmp_workspace), "arch-arb", MERGE_SENTINEL)
        try:
            r = await app_client.post(
                "/api/projects/arch-arb/epics/archive", json={"epic_ids": [epic_id]}
            )
            assert r.status_code == 200, r.text
            res = r.json()[0]
            assert res["archived"] is False
            assert res["error_code"] == "merge_active"
        finally:
            await _cleanup_fake_run(fake_task, "arch-arb", MERGE_SENTINEL)

        assert p.epic_dir(str(tmp_workspace), "arch-arb", epic_id).is_dir()

    async def test_invalid_epic_id_is_per_epic_error(
        self, app_client: Any, tmp_workspace: Path
    ) -> None:
        """A malformed id must not 422 the batch after earlier epics moved."""
        from yukar.config import paths as p

        await _bootstrap_project(app_client, "arch-bad")
        epic_id = await _create_epic(app_client, "arch-bad", "Fine epic")

        r = await app_client.post(
            "/api/projects/arch-bad/epics/archive",
            json={"epic_ids": [epic_id, "EP/evil", "-EP"]},
        )
        assert r.status_code == 200, r.text
        ok, bad_slash, bad_dash = r.json()
        assert ok["archived"] is True
        assert bad_slash["archived"] is False
        assert bad_slash["error_code"] == "invalid_id"
        assert bad_dash["error_code"] == "invalid_id"
        assert p.archived_epic_dir(str(tmp_workspace), "arch-bad", epic_id).is_dir()

    async def test_dest_collision_leaves_worktree_intact(
        self, app_client: Any, tmp_path: Path, tmp_workspace: Path
    ) -> None:
        """dest_exists is checked BEFORE the destructive teardown — a failing
        archive must not delete the epic's worktrees."""
        from yukar.config import paths as p
        from yukar.git.worktree import ensure_worktree

        repo = make_git_repo(tmp_path, "col-repo")
        await _bootstrap_project(
            app_client,
            "arch-col",
            repos=[{"name": "col-repo", "path": str(repo), "default_branch": "main"}],
        )
        epic_id = await _create_epic(app_client, "arch-col", "Collision")

        worktree_path = p.worktree_dir(
            str(tmp_workspace), "arch-col", epic_id, "manager", "col-repo"
        )
        await ensure_worktree(
            repo_path=repo,
            worktree_path=worktree_path,
            branch=f"yukar/{epic_id.lower()}-collision",
            default_branch="main",
        )

        # Pre-create the destination to force the collision.
        p.archived_epic_dir(str(tmp_workspace), "arch-col", epic_id).mkdir(parents=True)

        r = await app_client.post(
            "/api/projects/arch-col/epics/archive", json={"epic_ids": [epic_id]}
        )
        assert r.status_code == 200, r.text
        res = r.json()[0]
        assert res["archived"] is False
        assert res["error_code"] == "dest_exists"

        # Epic stayed in place and its worktree survived untouched.
        assert p.epic_dir(str(tmp_workspace), "arch-col", epic_id).is_dir()
        assert worktree_path.is_dir()

    async def test_unknown_epic_reports_error(self, app_client: Any) -> None:
        await _bootstrap_project(app_client, "arch-miss")
        r = await app_client.post(
            "/api/projects/arch-miss/epics/archive", json={"epic_ids": ["EP-999"]}
        )
        assert r.status_code == 200, r.text
        res = r.json()[0]
        assert res["archived"] is False
        assert res["error_code"] == "not_found"
        assert "not found" in (res["error"] or "")

    async def test_unknown_project_is_404(self, app_client: Any) -> None:
        r = await app_client.post(
            "/api/projects/no-such-proj/epics/archive", json={"epic_ids": ["EP-1"]}
        )
        assert r.status_code == 404
