"""Regression tests for git output parsing + git-arg hardening.

Covers the correctness fixes in git/runner.py, git/status.py, git/diff.py,
git/worktree.py, git/resolve.py, and agents/tools/git_tools.py:

1/2. ``parse_numstat`` keys renames and non-ASCII filenames correctly when fed
     ``--numstat -z`` output, and ``get_status`` reports real churn (not +0/-0)
     for both.
3.   Refs that begin with ``-`` are rejected, and config/LLM-derived refs are
     fenced behind ``--end-of-options``.
4.   ``run_git`` caps captured stdout/stderr with a truncation marker.
5.   ``_unquote_git_path`` round-trips a non-ASCII (UTF-8) octal-escaped name.
6.   ``git_commit`` returns a clean short hash, including the root commit.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest


def _make_git(repo: Path) -> Any:
    """Return a ``git(*args)`` helper bound to *repo* with a fixed identity."""
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "T",
        "GIT_AUTHOR_EMAIL": "t@t.com",
        "GIT_COMMITTER_NAME": "T",
        "GIT_COMMITTER_EMAIL": "t@t.com",
    }

    def git(*args: str) -> str:
        result = subprocess.run(
            ["git", *args], cwd=str(repo), capture_output=True, text=True, env=env
        )
        assert result.returncode == 0, f"git {args} failed: {result.stderr}"
        return result.stdout

    return git


# ---------------------------------------------------------------------------
# 1 + 2: parse_numstat — rename + non-ASCII encodings (NUL-delimited -z stream)
# ---------------------------------------------------------------------------


class TestParseNumstat:
    def test_plain_change(self) -> None:
        from yukar.git.runner import parse_numstat

        # Normal change record: added\tdeleted\t<path>\0
        out = "3\t1\tsrc/app.py\x00"
        assert parse_numstat(out) == [(3, 1, "src/app.py")]

    def test_rename_keys_new_path(self) -> None:
        from yukar.git.runner import parse_numstat

        # Rename record: added\tdeleted\t\0<old>\0<new>\0 — must key on NEW path
        # with the real counts (not +0/-0 against a garbled "old => new" path).
        out = "1\t0\t\x00oldname.txt\x00newname.txt\x00"
        assert parse_numstat(out) == [(1, 0, "newname.txt")]

    def test_non_ascii_filename(self) -> None:
        from yukar.git.runner import parse_numstat

        # In -z mode the non-ASCII path is emitted raw (UTF-8), not octal-escaped.
        out = "2\t0\tファイル.txt\x00"
        assert parse_numstat(out) == [(2, 0, "ファイル.txt")]

    def test_mixed_rename_plain_and_non_ascii(self) -> None:
        from yukar.git.runner import parse_numstat

        out = "1\t0\t\x00old.txt\x00new.txt\x002\t3\tplain.py\x001\t0\tファイル.txt\x00"
        assert parse_numstat(out) == [
            (1, 0, "new.txt"),
            (2, 3, "plain.py"),
            (1, 0, "ファイル.txt"),
        ]

    def test_binary_counts(self) -> None:
        from yukar.git.runner import parse_numstat

        # Binary files show '-' for both counts → 0/0.
        out = "-\t-\timage.png\x00"
        assert parse_numstat(out) == [(0, 0, "image.png")]

    def test_empty_output(self) -> None:
        from yukar.git.runner import parse_numstat

        assert parse_numstat("") == []

    def test_real_git_numstat_z_round_trip(self, tmp_path: Path) -> None:
        """Parse the actual ``git diff --numstat -z`` bytes git produces."""
        from yukar.git.runner import parse_numstat

        repo = tmp_path / "numstat-repo"
        repo.mkdir()
        git = _make_git(repo)
        git("init", "-b", "main")
        (repo / "oldname.txt").write_text("l1\nl2\nl3\n")
        (repo / "plain.txt").write_text("x\n")
        git("add", ".")
        git("commit", "-m", "init")
        # rename + modify, plus add a non-ASCII file
        git("mv", "oldname.txt", "newname.txt")
        (repo / "newname.txt").write_text("l1\nl2\nl3\nl4\n")
        (repo / "ファイル.txt").write_text("data\n")
        git("add", "-A")

        raw = subprocess.run(
            ["git", "diff", "--no-ext-diff", "--no-textconv", "--numstat", "-z", "HEAD"],
            cwd=str(repo),
            capture_output=True,
            text=True,
        ).stdout
        parsed = dict((fp, (a, d)) for a, d, fp in parse_numstat(raw))
        assert parsed["newname.txt"] == (1, 0)
        assert parsed["ファイル.txt"] == (1, 0)
        # The garbled "old => new" path must NOT appear.
        assert all("=>" not in fp for fp in parsed)


# ---------------------------------------------------------------------------
# 1 + 2: get_status churn for renamed + non-ASCII files
# ---------------------------------------------------------------------------


class TestGetStatusChurn:
    async def test_renamed_file_reports_real_churn(self, tmp_path: Path) -> None:
        from yukar.git.status import get_status

        repo = tmp_path / "rename-status"
        repo.mkdir()
        git = _make_git(repo)
        git("init", "-b", "main")
        (repo / "old.txt").write_text("a\nb\nc\n")
        git("add", ".")
        git("commit", "-m", "init")
        git("mv", "old.txt", "new.txt")
        (repo / "new.txt").write_text("a\nb\nc\nd\n")  # +1 line on the rename
        git("add", "-A")

        files = await get_status(repo)
        by_path = {f.path: f for f in files}
        assert "new.txt" in by_path, by_path
        # The rename target must carry the actual +1 churn, not +0/-0.
        assert by_path["new.txt"].added == 1
        assert by_path["new.txt"].deleted == 0
        assert by_path["new.txt"].status.startswith("R")
        # No garbled "old -> new" / "old => new" key leaks through.
        assert all("->" not in p and "=>" not in p for p in by_path)

    async def test_non_ascii_file_reports_real_churn(self, tmp_path: Path) -> None:
        from yukar.git.status import get_status

        repo = tmp_path / "nonascii-status"
        repo.mkdir()
        git = _make_git(repo)
        git("init", "-b", "main")
        (repo / "seed.txt").write_text("seed\n")
        git("add", ".")
        git("commit", "-m", "init")
        # Modify a tracked non-ASCII file so it appears in numstat.
        (repo / "ファイル.txt").write_text("one\n")
        git("add", ".")
        git("commit", "-m", "add non-ascii")
        (repo / "ファイル.txt").write_text("one\ntwo\nthree\n")  # +2 lines

        files = await get_status(repo)
        by_path = {f.path: f for f in files}
        assert "ファイル.txt" in by_path, by_path
        # numstat key (raw UTF-8) must match the porcelain key → real churn.
        assert by_path["ファイル.txt"].added == 2
        assert by_path["ファイル.txt"].deleted == 0


# ---------------------------------------------------------------------------
# 5: _unquote_git_path octal round-trip (UTF-8 name, not per-byte mojibake)
# ---------------------------------------------------------------------------


class TestUnquoteGitPath:
    def test_octal_utf8_round_trip(self) -> None:
        from yukar.git.status import _unquote_git_path

        # git status --porcelain (no -z) octal-escapes "ファイル.txt" byte by byte.
        quoted = (
            '"\\343\\203\\225\\343\\202\\241\\343\\202\\244\\343\\203\\253.txt"'
        )
        assert _unquote_git_path(quoted) == "ファイル.txt"

    def test_octal_run_decodes_as_unicode_not_per_byte(self) -> None:
        from yukar.git.status import _unquote_git_path

        # "é" is U+00E9 → UTF-8 0xC3 0xA9 → octal \303\251.  The old per-byte
        # decode produced two codepoints (mojibake "Ã©"); the fixed decode
        # yields the single correct character.
        assert _unquote_git_path('"\\303\\251.txt"') == "é.txt"

    def test_plain_quoted_escapes(self) -> None:
        from yukar.git.status import _unquote_git_path

        assert _unquote_git_path('"a\\"b.txt"') == 'a"b.txt'
        assert _unquote_git_path('"a\\\\b.txt"') == "a\\b.txt"

    def test_unquoted_path_passthrough(self) -> None:
        from yukar.git.status import _unquote_git_path

        # Raw (-z) paths are not quoted → returned unchanged (stripped).
        assert _unquote_git_path("src/app.py") == "src/app.py"
        assert _unquote_git_path("ファイル.txt") == "ファイル.txt"


# ---------------------------------------------------------------------------
# 3: leading-dash ref is neutralized (validate_git_ref + --end-of-options)
# ---------------------------------------------------------------------------


class TestValidateGitRef:
    def test_accepts_normal_ref(self) -> None:
        from yukar.git.runner import validate_git_ref

        assert validate_git_ref("main") == "main"
        assert validate_git_ref("yukar/EP-1-foo") == "yukar/EP-1-foo"
        assert validate_git_ref("main...feature") == "main...feature"

    def test_rejects_leading_dash(self) -> None:
        from yukar.git.runner import GitRefError, validate_git_ref

        with pytest.raises(GitRefError):
            validate_git_ref("-evil")
        with pytest.raises(GitRefError):
            validate_git_ref("--output=/tmp/pwned")

    def test_rejects_empty(self) -> None:
        from yukar.git.runner import GitRefError, validate_git_ref

        with pytest.raises(GitRefError):
            validate_git_ref("")

    async def test_get_diff_rejects_leading_dash_branch(self, tmp_path: Path) -> None:
        from yukar.git.diff import get_diff
        from yukar.git.runner import GitRefError

        repo = tmp_path / "diff-dash"
        repo.mkdir()
        git = _make_git(repo)
        git("init", "-b", "main")
        (repo / "f.txt").write_text("x\n")
        git("add", ".")
        git("commit", "-m", "init")

        with pytest.raises(GitRefError):
            await get_diff(repo, mode="epic", branch="--output=/tmp/pwned", default_branch="main")

    async def test_ensure_worktree_rejects_leading_dash_branch(self, tmp_path: Path) -> None:
        from yukar.git.runner import GitRefError
        from yukar.git.worktree import ensure_worktree

        repo = tmp_path / "wt-dash"
        repo.mkdir()
        git = _make_git(repo)
        git("init", "-b", "main")
        (repo / "f.txt").write_text("x\n")
        git("add", ".")
        git("commit", "-m", "init")

        # A leading-dash branch must be rejected BEFORE git is invoked — never
        # leaked into worktree-add's internal ``git branch`` call.
        with pytest.raises(GitRefError):
            await ensure_worktree(
                repo_path=repo,
                worktree_path=tmp_path / "wt",
                branch="-evil",
                default_branch="main",
            )

    async def test_diff_with_dashish_range_is_fenced_not_executed(self, tmp_path: Path) -> None:
        """--end-of-options stops a crafted range-spec writing a file via --output."""
        from yukar.git.runner import run_git

        repo = tmp_path / "fence"
        repo.mkdir()
        git = _make_git(repo)
        git("init", "-b", "main")
        (repo / "f.txt").write_text("x\n")
        git("add", ".")
        git("commit", "-m", "init")

        sentinel = tmp_path / "pwned.txt"
        # Without --end-of-options, "--output=<file>" would be parsed as a diff
        # option and create the file.  With the separator git treats it as a
        # (non-existent) revision and errors instead of writing anything.
        result = await run_git(
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--end-of-options",
            f"--output={sentinel}",
            cwd=repo,
            check=False,
        )
        assert not sentinel.exists()
        assert result.returncode != 0


# ---------------------------------------------------------------------------
# 4: run_git output truncation
# ---------------------------------------------------------------------------


class TestOutputTruncation:
    def test_truncate_under_limit_passthrough(self) -> None:
        from yukar.git.runner import _truncate

        raw = b"hello world"
        assert _truncate(raw) == "hello world"

    def test_truncate_over_limit_adds_marker(self) -> None:
        from yukar.git.runner import _MAX_CAPTURE_BYTES, _truncate

        raw = b"x" * (_MAX_CAPTURE_BYTES + 4096)
        out = _truncate(raw)
        assert len(out.encode("utf-8")) < len(raw)
        assert "truncated" in out
        assert out.startswith("x")

    def test_truncate_split_multibyte_does_not_raise(self) -> None:
        from yukar.git.runner import _truncate

        # Limit cuts through the middle of a 3-byte UTF-8 sequence; replace mode
        # must keep it from raising.
        raw = "あ".encode() * 10  # 30 bytes
        out = _truncate(raw, limit=7)
        assert "truncated" in out

    async def test_run_git_caps_huge_output(self, tmp_path: Path, monkeypatch: Any) -> None:
        from yukar.git import runner
        from yukar.git.runner import run_git

        repo = tmp_path / "huge"
        repo.mkdir()
        git = _make_git(repo)
        git("init", "-b", "main")
        # A large tracked file produces a large diff.
        (repo / "big.txt").write_text("seed\n")
        git("add", ".")
        git("commit", "-m", "init")
        (repo / "big.txt").write_text("line\n" * 200_000)

        # Shrink the cap so the test stays fast but still exercises truncation.
        monkeypatch.setattr(runner, "_MAX_CAPTURE_BYTES", 64 * 1024)
        result = await run_git(
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "HEAD",
            cwd=repo,
            check=False,
        )
        assert "truncated" in result.stdout
        assert len(result.stdout.encode("utf-8")) < 200_000


# ---------------------------------------------------------------------------
# 6: git_commit short-hash extraction (normal + root commit)
# ---------------------------------------------------------------------------


class TestGitCommitShortHash:
    def _setup_worktree_repo(self, tmp_path: Path) -> Path:
        """Create a primary repo (so HEAD's first commit IS the root commit)."""
        repo = tmp_path / "commit-repo"
        repo.mkdir()
        git = _make_git(repo)
        git("init", "-b", "main")
        git("config", "user.email", "t@t.com")
        git("config", "user.name", "T")
        return repo

    async def _make_ctx(self, worktree: Path) -> Any:
        from yukar.agents.context import AgentContext

        return await AgentContext.create(
            project_id="proj",
            epic_id="EP-git",
            repo_name="repo",
            worktree_path=worktree,
            workspace_root=str(worktree.parent),
        )

    async def test_root_commit_returns_clean_short_hash(self, tmp_path: Path) -> None:
        from yukar.agents.tools.git_tools import make_git_tools

        repo = self._setup_worktree_repo(tmp_path)
        (repo / "first.py").write_text("x = 1\n")
        ctx = await self._make_ctx(repo)
        _, _, git_add, git_commit = make_git_tools(ctx)
        await git_add(paths="first.py")
        result = await git_commit(message="root: first")

        assert result["status"] == "success"
        h = result["commit_hash"]
        assert h is not None
        # A real short hash: lowercase hex, no trailing ']' and not '(root-commit)'.
        assert "]" not in h
        assert "(" not in h
        assert h != "(root-commit)"
        assert all(c in "0123456789abcdef" for c in h), h
        assert 4 <= len(h) <= 40
        # And it must actually resolve to HEAD.
        full = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True
        ).stdout.strip()
        assert full.startswith(h)

    async def test_normal_commit_returns_clean_short_hash(self, tmp_path: Path) -> None:
        from yukar.agents.tools.git_tools import make_git_tools

        repo = self._setup_worktree_repo(tmp_path)
        (repo / "first.py").write_text("x = 1\n")
        git = _make_git(repo)
        git("add", ".")
        git("commit", "-m", "init")

        (repo / "second.py").write_text("y = 2\n")
        ctx = await self._make_ctx(repo)
        _, _, git_add, git_commit = make_git_tools(ctx)
        await git_add(paths="second.py")
        result = await git_commit(message="feat: second")

        h = result["commit_hash"]
        assert h is not None
        assert "]" not in h
        assert all(c in "0123456789abcdef" for c in h), h
        full = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True
        ).stdout.strip()
        assert full.startswith(h)
