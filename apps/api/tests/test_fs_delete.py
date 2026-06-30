"""Tests for the ``fs_delete`` worker tool (yukar.agents.tools.fs).

``fs_delete`` removes a file or directory inside the worktree.  It must:

- delete regular files;
- refuse to delete a directory unless ``recursive=True``;
- delete a directory tree when ``recursive=True``;
- never escape the worktree (``../x`` and absolute paths resolve to "not found");
- refuse to delete the worktree root itself;
- treat gitignored paths as non-existent (spec §6.6);
- leave a deletion that the host's ``git add -A`` stages as a ``git rm``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from tests._helpers import git_env, make_git_repo


async def _make_ctx(worktree: Path) -> Any:
    from yukar.agents.context import AgentContext

    return await AgentContext.create(
        project_id="proj",
        epic_id="EP-1",
        repo_name="repo",
        worktree_path=worktree,
        workspace_root=str(worktree.parent),
    )


async def _fs_delete(worktree: Path):
    from yukar.agents.tools.fs import make_fs_tools

    ctx = await _make_ctx(worktree)
    _, _, _, fs_delete = make_fs_tools(ctx)
    return fs_delete


class TestFsDelete:
    async def test_delete_file(self, tmp_path: Path) -> None:
        wt = tmp_path / "wt"
        wt.mkdir()
        (wt / "keep.txt").write_text("keep")
        (wt / "gone.txt").write_text("gone")
        fs_delete = await _fs_delete(wt)

        result = fs_delete(path="gone.txt")

        assert result["status"] == "success"
        assert not (wt / "gone.txt").exists()
        assert (wt / "keep.txt").exists()  # siblings untouched

    async def test_delete_missing_file_reports_not_found(self, tmp_path: Path) -> None:
        wt = tmp_path / "wt"
        wt.mkdir()
        fs_delete = await _fs_delete(wt)

        result = fs_delete(path="nope.txt")

        assert result["status"] == "error"
        assert "not found" in result["content"][0]["text"].lower()

    async def test_delete_directory_requires_recursive(self, tmp_path: Path) -> None:
        wt = tmp_path / "wt"
        wt.mkdir()
        sub = wt / "sub"
        sub.mkdir()
        (sub / "a.txt").write_text("a")
        fs_delete = await _fs_delete(wt)

        result = fs_delete(path="sub")

        assert result["status"] == "error"
        assert "recursive" in result["content"][0]["text"].lower()
        assert sub.exists()  # not deleted without the flag

    async def test_delete_directory_recursive(self, tmp_path: Path) -> None:
        wt = tmp_path / "wt"
        wt.mkdir()
        sub = wt / "sub"
        (sub / "nested").mkdir(parents=True)
        (sub / "a.txt").write_text("a")
        (sub / "nested" / "b.txt").write_text("b")
        fs_delete = await _fs_delete(wt)

        result = fs_delete(path="sub", recursive=True)

        assert result["status"] == "success"
        assert not sub.exists()

    async def test_delete_outside_worktree_rejected(self, tmp_path: Path) -> None:
        wt = tmp_path / "wt"
        wt.mkdir()
        outside = tmp_path / "outside.txt"
        outside.write_text("secret")
        fs_delete = await _fs_delete(wt)

        result = fs_delete(path="../outside.txt")

        assert result["status"] == "error"
        assert outside.exists()  # the escape attempt deleted nothing

    async def test_delete_worktree_root_refused(self, tmp_path: Path) -> None:
        wt = tmp_path / "wt"
        wt.mkdir()
        (wt / "a.txt").write_text("a")
        fs_delete = await _fs_delete(wt)

        result = fs_delete(path=".", recursive=True)

        assert result["status"] == "error"
        assert "worktree root" in result["content"][0]["text"].lower()
        assert wt.exists()
        assert (wt / "a.txt").exists()

    async def test_delete_gitignored_path_appears_not_found(self, tmp_path: Path) -> None:
        """Gitignored files are invisible to the agent (spec §6.6), so a delete
        of one reports 'not found' and leaves the file on disk."""
        wt = tmp_path / "wt"
        wt.mkdir()
        (wt / ".gitignore").write_text(".env\n")
        (wt / ".env").write_text("SECRET=1\n")
        fs_delete = await _fs_delete(wt)

        result = fs_delete(path=".env")

        assert result["status"] == "error"
        assert "not found" in result["content"][0]["text"].lower()
        assert (wt / ".env").exists()

    async def test_deletion_is_staged_as_git_rm(self, tmp_path: Path) -> None:
        """A working-tree deletion is picked up by ``git add -A`` as a removal —
        i.e. it becomes the equivalent of ``git rm`` in the host commit path."""
        repo = make_git_repo(tmp_path, "repo")
        tracked = repo / "tracked.txt"
        tracked.write_text("data\n")
        subprocess.run(
            ["git", "add", "tracked.txt"], cwd=repo, env=git_env(), check=True
        )
        subprocess.run(
            ["git", "commit", "-m", "add tracked"], cwd=repo, env=git_env(), check=True
        )

        fs_delete = await _fs_delete(repo)
        result = fs_delete(path="tracked.txt")
        assert result["status"] == "success"

        subprocess.run(["git", "add", "-A"], cwd=repo, env=git_env(), check=True)
        status = subprocess.run(
            ["git", "status", "--porcelain", "tracked.txt"],
            cwd=repo,
            env=git_env(),
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        # Staged deletion shows as "D " in the porcelain status.
        assert status.startswith("D ")
