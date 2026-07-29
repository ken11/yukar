"""Tests for ``grep_worktree`` (yukar.agents.tools.grep_tools).

The core contract that keeps agents and host aligned:

- the pattern is LITERAL by default — code pasted as-is (parens, brackets,
  pipes, backslashes) must match without any escaping;
- ``regex=True`` switches to ripgrep's Rust regex dialect, where invalid
  patterns fail with a visible error (never a silent 0);
- patterns mangled by JSON escaping (control characters such as backspace)
  are rejected with an actionable error instead of returning 0 matches;
- a 0-match result whose pattern contains a suspicious backslash carries a
  self-correction hint.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from tests._helpers import make_git_repo

pytestmark = pytest.mark.skipif(
    shutil.which("rg") is None, reason="ripgrep (rg) not installed"
)


async def _make_ctx(worktree: Path) -> Any:
    from yukar.agents.context import AgentContext

    return await AgentContext.create(
        project_id="proj",
        epic_id="EP-1",
        repo_name="repo",
        worktree_path=worktree,
        workspace_root=str(worktree.parent),
    )


async def _grep(worktree: Path, pattern: str, **kwargs: Any) -> dict[str, Any]:
    from yukar.agents.tools.grep_tools import grep_worktree

    ctx = await _make_ctx(worktree)
    return await grep_worktree(ctx, pattern, **kwargs)


def _text(result: dict[str, Any]) -> str:
    return result["content"][0]["text"]


class TestLiteralDefault:
    async def test_code_with_metachars_matches_as_is(self, tmp_path: Path) -> None:
        wt = tmp_path / "wt"
        wt.mkdir()
        (wt / "app.py").write_text(
            'logger.info("hello")\nvalue = items[0]\nresult = run(a) | run(b)\n'
        )

        for pattern in ['logger.info("hello")', "items[0]", "run(a) | run(b)"]:
            result = await _grep(wt, pattern)
            assert result["status"] == "success", pattern
            assert len(result["results"]) == 1, pattern

    async def test_backslash_in_source_matches_as_is(self, tmp_path: Path) -> None:
        wt = tmp_path / "wt"
        wt.mkdir()
        (wt / "app.py").write_text('print("a\\nb")\n')  # file contains a real backslash

        result = await _grep(wt, 'print("a\\nb")')

        assert result["status"] == "success"
        assert len(result["results"]) == 1

    async def test_zero_match_with_backslash_carries_hint(self, tmp_path: Path) -> None:
        wt = tmp_path / "wt"
        wt.mkdir()
        (wt / "app.py").write_text("x = hoge(1)\n")

        # Regex-style escaping searched literally — misses, but must say why.
        result = await _grep(wt, "hoge\\(")

        assert result["status"] == "success"
        assert result["results"] == []
        assert "LITERALLY" in _text(result)
        assert "regex=true" in _text(result)


class TestRegexMode:
    async def test_pcre_style_constructs_work(self, tmp_path: Path) -> None:
        wt = tmp_path / "wt"
        wt.mkdir()
        (wt / "app.py").write_text("status = 200\nword here\n")

        alternation = await _grep(wt, "status|word", regex=True)
        assert len(alternation["results"]) == 2

        boundary = await _grep(wt, "\\bword\\b", regex=True)
        assert len(boundary["results"]) == 1

    async def test_single_backslash_escapes_metachar(self, tmp_path: Path) -> None:
        wt = tmp_path / "wt"
        wt.mkdir()
        (wt / "app.py").write_text("x = hoge(1)\n")

        result = await _grep(wt, "hoge\\(", regex=True)

        assert result["status"] == "success"
        assert len(result["results"]) == 1

    async def test_invalid_regex_reports_error(self, tmp_path: Path) -> None:
        wt = tmp_path / "wt"
        wt.mkdir()
        (wt / "app.py").write_text("x = hoge(1)\n")

        result = await _grep(wt, "hoge(", regex=True)

        assert result["status"] == "error"
        assert "regex parse error" in _text(result)

    async def test_double_escape_zero_match_carries_hint(self, tmp_path: Path) -> None:
        wt = tmp_path / "wt"
        wt.mkdir()
        (wt / "app.py").write_text("x = foo(bar)\n")

        # Balanced double-escape compiles fine but matches a real backslash.
        result = await _grep(wt, "foo\\\\(bar\\\\)", regex=True)

        assert result["status"] == "success"
        assert result["results"] == []
        assert "double-escaped" in _text(result)


class TestPatternGuards:
    async def test_backspace_pattern_rejected_with_json_hint(self, tmp_path: Path) -> None:
        wt = tmp_path / "wt"
        wt.mkdir()
        (wt / "app.py").write_text("status = 200\n")

        # What the tool receives when a model writes "\bstatus\b" in JSON.
        result = await _grep(wt, "\x08status\x08", regex=True)

        assert result["status"] == "error"
        assert "backspace" in _text(result)
        assert "\\b" in _text(result)

    async def test_newline_pattern_rejected(self, tmp_path: Path) -> None:
        wt = tmp_path / "wt"
        wt.mkdir()
        (wt / "app.py").write_text("a\nb\n")

        result = await _grep(wt, "a\nb")

        assert result["status"] == "error"
        assert "single line" in _text(result)

    async def test_other_control_chars_rejected(self, tmp_path: Path) -> None:
        wt = tmp_path / "wt"
        wt.mkdir()
        (wt / "app.py").write_text("a\n")

        result = await _grep(wt, "a\x0cb")

        assert result["status"] == "error"
        assert "U+000C" in _text(result)

    async def test_tab_in_pattern_is_allowed(self, tmp_path: Path) -> None:
        wt = tmp_path / "wt"
        wt.mkdir()
        (wt / "app.py").write_text("\tindented\n")

        result = await _grep(wt, "\tindented")

        assert result["status"] == "success"
        assert len(result["results"]) == 1


class TestScope:
    async def test_gitignored_file_not_searched(self, tmp_path: Path) -> None:
        repo = make_git_repo(tmp_path)
        (repo / ".gitignore").write_text("*.log\n")
        (repo / "debug.log").write_text("needle_in_log\n")
        (repo / "kept.py").write_text("needle_in_source\n")

        ignored = await _grep(repo, "needle_in_log")
        kept = await _grep(repo, "needle_in_source")

        assert ignored["status"] == "success"
        assert ignored["results"] == []
        assert len(kept["results"]) == 1
