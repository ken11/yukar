"""Tests for the Manager's read-only ``read_branch_diff`` verification tool (P3).

The Manager can independently inspect the full branch diff (epic branch vs the
default branch) before calling ``complete_epic``, rather than relying solely on
Evaluator verdicts.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from tests._helpers import git_env, make_git_repo


def _add_branch_with_change(repo: Path, branch: str) -> None:
    """Create *branch* off main with one new file, then return to main."""
    env = git_env()

    def g(*args: str) -> None:
        r = subprocess.run(
            ["git", *args], cwd=str(repo), capture_output=True, text=True, env=env
        )
        assert r.returncode == 0, f"git {args}: {r.stderr}"

    g("checkout", "-b", branch)
    (repo / "feature.py").write_text("print('hello from feature')\n")
    g("add", ".")
    g("commit", "-m", "add feature")
    g("checkout", "main")


async def _setup(root: str, repo_path: Path, repo_name: str, branch: str):
    from yukar.models.epic import Epic
    from yukar.models.project import Project, Repo
    from yukar.storage.epic_repo import save_epic
    from yukar.storage.project_repo import save_project, save_repo

    await save_project(root, Project(id="proj", name="proj", repos=[repo_name]))
    await save_repo(
        root, "proj", Repo(name=repo_name, path=str(repo_path), default_branch="main")
    )
    epic = Epic(id="EP-1", slug="test", title="T", branch=branch, touched_repos=[repo_name])
    await save_epic(root, "proj", epic)
    return epic


def _make_orch(epic, root: str):
    from yukar.agents.orchestrator import EpicOrchestrator
    from yukar.config.settings import LLMSettings

    orch = EpicOrchestrator(
        llm_settings=LLMSettings(provider="fake"),
        git_author_name="Test",
        git_author_email="test@test.com",
    )
    orch._root = root
    orch._project_id = "proj"
    orch._epic_id = "EP-1"
    orch._epic = epic
    orch._manager_thread_id = "manager"
    return orch


class TestReadBranchDiff:
    async def test_returns_epic_branch_diff(self, tmp_workspace: Path, tmp_path: Path) -> None:
        root = str(tmp_workspace)
        repo = make_git_repo(tmp_path, "repo")
        branch = "yukar/ep-1-test"
        _add_branch_with_change(repo, branch)
        epic = await _setup(root, repo, "repo", branch)
        orch = _make_orch(epic, root)

        result = await orch._do_read_branch_diff()

        assert result["ok"] is True
        assert len(result["repos"]) == 1
        r0 = result["repos"][0]
        assert r0["repo"] == "repo"
        assert r0["branch"] == branch
        assert r0["total_added"] >= 1
        assert "feature.py" in r0["diff"]
        assert any(f["path"] == "feature.py" for f in r0["files"])
        assert r0["truncated"] is False

    async def test_specific_repo_argument(self, tmp_workspace: Path, tmp_path: Path) -> None:
        root = str(tmp_workspace)
        repo = make_git_repo(tmp_path, "repo")
        branch = "yukar/ep-1-test"
        _add_branch_with_change(repo, branch)
        epic = await _setup(root, repo, "repo", branch)
        orch = _make_orch(epic, root)

        result = await orch._do_read_branch_diff(repo="repo")
        assert result["ok"] is True
        assert [r["repo"] for r in result["repos"]] == ["repo"]

    async def test_unknown_repo_returns_error(self, tmp_workspace: Path, tmp_path: Path) -> None:
        root = str(tmp_workspace)
        repo = make_git_repo(tmp_path, "repo")
        branch = "yukar/ep-1-test"
        _add_branch_with_change(repo, branch)
        epic = await _setup(root, repo, "repo", branch)
        orch = _make_orch(epic, root)

        result = await orch._do_read_branch_diff(repo="does-not-exist")
        assert result["ok"] is False
        assert "unknown repo" in result["reason"]
