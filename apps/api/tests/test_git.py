"""Tests for git status, diff, commit, merge, and conflict 409."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


class TestGitStatus:
    async def test_status_shows_untracked(self, fixture_git_repo: Path) -> None:
        from yukar.git.status import get_status

        files = await get_status(fixture_git_repo)
        paths = [f.path for f in files]
        assert any("work_in_progress" in p for p in paths)

    async def test_status_empty_clean_repo(self, tmp_path: Path) -> None:
        from yukar.git.status import get_status

        repo = tmp_path / "clean-repo"
        repo.mkdir()
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "T",
            "GIT_AUTHOR_EMAIL": "t@t.com",
            "GIT_COMMITTER_NAME": "T",
            "GIT_COMMITTER_EMAIL": "t@t.com",
        }

        def git(*args: str) -> None:
            subprocess.run(["git", *args], cwd=str(repo), env=env, check=True, capture_output=True)

        git("init", "-b", "main")
        git("config", "user.email", "t@t.com")
        git("config", "user.name", "T")
        (repo / "f.txt").write_text("hello")
        git("add", ".")
        git("commit", "-m", "init")

        files = await get_status(repo)
        assert files == []


class TestGitDiff:
    async def test_working_diff(self, fixture_git_repo: Path) -> None:
        from yukar.git.diff import get_diff

        result = await get_diff(fixture_git_repo, mode="working")
        assert result.mode == "working"
        # There's an untracked file, so files may or may not appear in diff
        # (untracked files don't show in `git diff HEAD`)
        assert isinstance(result.files, list)

    async def test_epic_diff(self, fixture_git_repo: Path) -> None:
        from yukar.git.diff import get_diff

        result = await get_diff(
            fixture_git_repo,
            mode="epic",
            branch="yukar/ep-1-test-epic",
            default_branch="main",
        )
        assert result.mode == "epic"
        assert len(result.files) > 0  # feature.py was added on the branch
        assert result.total_added > 0


class TestGitCommit:
    async def test_commit_changes(self, fixture_git_repo: Path) -> None:
        from yukar.git.diff import commit

        # Add a new file
        (fixture_git_repo / "new_file.py").write_text("# new\n")
        sha = await commit(fixture_git_repo, "Add new_file.py")
        assert len(sha) == 40  # Full SHA


class TestGitMerge:
    async def test_successful_merge(self, fixture_git_repo: Path) -> None:
        from yukar.git.diff import merge

        # We're on main; merge the feature branch
        sha = await merge(fixture_git_repo, "yukar/ep-1-test-epic")
        assert len(sha) == 40

    async def test_conflict_raises_409(self, tmp_path: Path) -> None:
        from yukar.git.diff import MergeConflictError, merge

        repo = tmp_path / "conflict-repo"
        repo.mkdir()
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "T",
            "GIT_AUTHOR_EMAIL": "t@t.com",
            "GIT_COMMITTER_NAME": "T",
            "GIT_COMMITTER_EMAIL": "t@t.com",
        }

        def git(*args: str) -> None:
            subprocess.run(["git", *args], cwd=str(repo), env=env, check=True, capture_output=True)

        git("init", "-b", "main")
        git("config", "user.email", "t@t.com")
        git("config", "user.name", "T")

        # Create initial file
        (repo / "shared.txt").write_text("line1\n")
        git("add", ".")
        git("commit", "-m", "init")

        # Feature branch modifies shared.txt
        git("checkout", "-b", "feature")
        (repo / "shared.txt").write_text("feature change\n")
        git("add", ".")
        git("commit", "-m", "feature")

        # Back to main, also modify shared.txt
        git("checkout", "main")
        (repo / "shared.txt").write_text("main change\n")
        git("add", ".")
        git("commit", "-m", "main change")

        with pytest.raises(MergeConflictError):
            await merge(repo, "feature")


class TestGitAPI:
    async def _setup(self, client: object, repo_path: Path) -> None:
        import httpx

        assert isinstance(client, httpx.AsyncClient)
        await client.post(
            "/api/projects",
            json={
                "id": "gproj",
                "name": "Git Project",
                "repos": [
                    {
                        "name": "test-repo",
                        "path": str(repo_path),
                        "default_branch": "main",
                    }
                ],
            },
        )
        await client.post("/api/projects/gproj/epics", json={"title": "Git Epic"})

    async def test_git_status_endpoint(self, app_client: object, fixture_git_repo: Path) -> None:
        import httpx

        assert isinstance(app_client, httpx.AsyncClient)
        await self._setup(app_client, fixture_git_repo)
        r = await app_client.get("/api/projects/gproj/epics/EP-1/git/status?repo=test-repo")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    async def test_git_diff_endpoint(self, app_client: object, fixture_git_repo: Path) -> None:
        import httpx

        assert isinstance(app_client, httpx.AsyncClient)
        await self._setup(app_client, fixture_git_repo)
        r = await app_client.get(
            "/api/projects/gproj/epics/EP-1/git/diff?mode=working&repo=test-repo"
        )
        assert r.status_code == 200
        data = r.json()
        assert data["mode"] == "working"

    async def test_git_merge_conflict_409(self, app_client: object, tmp_path: Path) -> None:
        import httpx

        assert isinstance(app_client, httpx.AsyncClient)
        # Create conflict repo
        repo = tmp_path / "conflict-repo"
        repo.mkdir()
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "T",
            "GIT_AUTHOR_EMAIL": "t@t.com",
            "GIT_COMMITTER_NAME": "T",
            "GIT_COMMITTER_EMAIL": "t@t.com",
        }

        def git(*args: str) -> None:
            subprocess.run(["git", *args], cwd=str(repo), env=env, check=True, capture_output=True)

        git("init", "-b", "main")
        git("config", "user.email", "t@t.com")
        git("config", "user.name", "T")
        (repo / "f.txt").write_text("original\n")
        git("add", ".")
        git("commit", "-m", "init")
        git("checkout", "-b", "yukar/ep-1-conflict-test")
        (repo / "f.txt").write_text("branch change\n")
        git("add", ".")
        git("commit", "-m", "branch")
        git("checkout", "main")
        (repo / "f.txt").write_text("main change\n")
        git("add", ".")
        git("commit", "-m", "main")

        await app_client.post(
            "/api/projects",
            json={
                "id": "conflict-proj",
                "name": "Conflict",
                "repos": [{"name": "conflict-repo", "path": str(repo)}],
            },
        )
        await app_client.post(
            "/api/projects/conflict-proj/epics",
            json={"title": "conflict test"},
        )
        # Patch epic to set branch
        await app_client.patch(
            "/api/projects/conflict-proj/epics/EP-1",
            json={"status": "in_progress"},
        )

        r = await app_client.post(
            "/api/projects/conflict-proj/epics/EP-1/git/merge",
            json={"repo": "conflict-repo"},
        )
        assert r.status_code == 409
