"""Regression guard: dim-guard-allrepos

Fixed: the all-repos search branch in indexer/service.py (search method, around line 597)
now calls _check_dimension inside the for-loop, so a dimension mismatch raises
DimensionMismatchError instead of silently returning garbage results.

Regression guard — these tests confirm the fix holds:
- Single-repo path raises DimensionMismatchError on mismatch (was already correct).
- All-repos path also raises DimensionMismatchError on mismatch (fixed — regression guard).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Helpers (self-contained — no import from other test modules)
# ---------------------------------------------------------------------------


def _make_git_repo(tmp_path: Path, name: str = "repo") -> Path:
    """Create a minimal git repo at tmp_path/name with one commit."""
    repo = tmp_path / name
    repo.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@test.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@test.com",
    }

    def g(*args: str) -> str:
        r = subprocess.run(
            ["git", *args], cwd=str(repo), capture_output=True, text=True, env=env
        )
        assert r.returncode == 0, f"git {args}: {r.stderr}"
        return r.stdout.strip()

    g("init", "-b", "main")
    g("config", "user.email", "test@test.com")
    g("config", "user.name", "Test")
    (repo / "hello.py").write_text("def hello():\n    return 'world'\n")
    g("add", ".")
    g("commit", "-m", "initial")
    return repo


def _write_repo_yaml(workspace: Path, project_id: str, repo_name: str, repo_path: Path) -> None:
    """Write a minimal repo YAML so that list_repos() finds the repo as enabled."""
    from yukar.config import paths as config_paths

    repos_dir = config_paths.repos_dir(str(workspace), project_id)
    repos_dir.mkdir(parents=True, exist_ok=True)
    repo_yaml = repos_dir / f"{repo_name}.yaml"
    # Minimal YAML: index.enabled=true (default)
    repo_yaml.write_text(
        f"name: {repo_name}\npath: {repo_path}\nindex:\n  enabled: true\n"
    )


def _make_service(workspace: Path, dim: int) -> Any:
    """Return an IndexerService backed by FakeEmbedder with the given dim."""
    from yukar.indexer.embedder import FakeEmbedder
    from yukar.indexer.service import IndexerService

    return IndexerService(
        workspace_root=str(workspace),
        embedder=FakeEmbedder(dim=dim),
    )


# ---------------------------------------------------------------------------
# Characterization: single-repo path raises DimensionMismatchError (correct)
# ---------------------------------------------------------------------------


async def test_single_repo_path_raises_on_dim_mismatch(tmp_path: Path) -> None:
    """The single-repo (repo_name=...) branch raises DimensionMismatchError on mismatch.

    This is a PASSING characterization test — the single-repo code path already
    calls _check_dimension at line 573 of service.py.
    """
    from yukar.indexer.service import DimensionMismatchError

    workspace = tmp_path / "ws"
    workspace.mkdir()
    repo = _make_git_repo(tmp_path, "myrepo")

    # Index with dim=128
    svc_128 = _make_service(workspace, dim=128)
    await svc_128.reindex_repo("proj", "myrepo", repo)

    # Search with dim=256 — should raise
    svc_256 = _make_service(workspace, dim=256)
    with pytest.raises(DimensionMismatchError):
        await svc_256.search("proj", "hello", repo_name="myrepo", top_k=3)


# ---------------------------------------------------------------------------
# Bug confirmation: all-repos path silently skips dimension check
# ---------------------------------------------------------------------------


async def test_all_repos_path_raises_on_dim_mismatch(tmp_path: Path) -> None:
    """The all-repos (repo_name=None) branch raises DimensionMismatchError on mismatch.

    Fixed: _check_dimension is now called inside the for-loop at ~line 597, so a
    mismatched vector raises instead of silently returning garbage results.
    Regression guard — this test confirms the fix holds.
    """
    from yukar.indexer.service import DimensionMismatchError

    workspace = tmp_path / "ws"
    workspace.mkdir()
    repo = _make_git_repo(tmp_path, "myrepo")

    # Register the repo in YAML so list_repos() picks it up as enabled
    _write_repo_yaml(workspace, "proj", "myrepo", repo)

    # Index with dim=128
    svc_128 = _make_service(workspace, dim=128)
    await svc_128.reindex_repo("proj", "myrepo", repo)

    # Search with dim=256 via all-repos path (repo_name=None)
    svc_256 = _make_service(workspace, dim=256)
    with pytest.raises(DimensionMismatchError):
        await svc_256.search("proj", "hello", repo_name=None, top_k=3)


# ---------------------------------------------------------------------------
# Positive: all-repos path works when dimensions match (no regression)
# ---------------------------------------------------------------------------


async def test_all_repos_path_returns_results_when_dims_match(tmp_path: Path) -> None:
    """The all-repos path returns results normally when dimensions match.

    This guards against a future fix that might over-eagerly reject valid searches.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    repo = _make_git_repo(tmp_path, "myrepo")

    _write_repo_yaml(workspace, "proj", "myrepo", repo)

    svc = _make_service(workspace, dim=128)
    await svc.reindex_repo("proj", "myrepo", repo)

    results = await svc.search("proj", "hello", repo_name=None, top_k=5)
    # At least one chunk must be returned (we indexed hello.py)
    assert len(results) > 0
    # Results are (Chunk, float) pairs
    chunk, dist = results[0]
    assert isinstance(dist, float)
    assert "text" in chunk
