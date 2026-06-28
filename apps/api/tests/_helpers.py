"""Shared git bootstrap helpers for tests.

Promoted from the per-file copies that several test modules duplicated
(many self-labelled "duplicated from test_orchestration.py").
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def git_env() -> dict[str, str]:
    """Return os.environ augmented with a deterministic git identity."""
    return {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@test.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@test.com",
    }


def make_git_repo(parent: Path, name: str = "repo") -> Path:
    """Create a minimal git repo under *parent*/*name* with one commit on 'main'.

    Returns the repo path.
    """
    repo = parent / name
    repo.mkdir()
    env = git_env()

    def g(*args: str) -> str:
        r = subprocess.run(
            ["git", *args], cwd=str(repo), capture_output=True, text=True, env=env
        )
        assert r.returncode == 0, f"git {args}: {r.stderr}"
        return r.stdout.strip()

    g("init", "-b", "main")
    g("config", "user.email", "test@test.com")
    g("config", "user.name", "Test")
    (repo / "README.md").write_text("# test\n")
    g("add", ".")
    g("commit", "-m", "initial")
    return repo
