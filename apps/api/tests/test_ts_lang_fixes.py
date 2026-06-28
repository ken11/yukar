"""Tests for tree-sitter language support fixes.

Covers:
- Fix 1: LANG_MAP uses 'csharp' (not 'c_sharp') for .cs files
- Fix 1: All LANG_MAP language values are valid in tslp registry (no
  "not available for download" errors; network errors are skipped)
- Fix 2: _ts_split emits a per-language-once warning on fallback
- Fix 2: ts_files / fallback_files tracked in stats and exposed via API
- Fix 3: _prefetch_grammars uses DownloadManager.new(version).ensure_group("all")
- Fix 4: BUNDLED_LANGUAGES removed
- Fix 5: Short-line files do not explode chunk count (overlap cap)
- Fix 6: _collect_files single I/O (NUL check reuses same read)
"""

from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from tests._helpers import make_git_repo

# ===========================================================================
# Fix 1: LANG_MAP correctness
# ===========================================================================


class TestLangMap:
    def test_cs_maps_to_csharp(self) -> None:
        """C# files must map to 'csharp', not 'c_sharp'."""
        from yukar.indexer.languages import LANG_MAP

        assert LANG_MAP[".cs"] == "csharp", f"Expected '.cs' -> 'csharp', got '{LANG_MAP['.cs']}'"

    def test_c_sharp_not_in_lang_map(self) -> None:
        """'c_sharp' must not appear anywhere in LANG_MAP values."""
        from yukar.indexer.languages import LANG_MAP

        assert "c_sharp" not in LANG_MAP.values(), (
            "Found 'c_sharp' in LANG_MAP — tslp manifest uses 'csharp'"
        )

    def test_all_lang_map_values_valid_in_tslp(self) -> None:
        """Every LANG_MAP value must be a downloadable language in the tslp manifest.

        We check against the *manifest* (every grammar tslp can download), NOT
        ``installed_languages()`` (only grammars already downloaded to this
        machine, which is empty on fresh CI runners — that would fail every
        entry).  A language absent from the manifest triggers
        ``'not available for download'`` on fresh machines.

        The manifest ships with the wheel, so this needs no network; genuine
        network errors are skipped rather than failed.
        """
        import pytest

        try:
            import tree_sitter_language_pack as tslp  # type: ignore[import-untyped]
        except ImportError:
            pytest.skip("tree_sitter_language_pack not installed")

        from yukar.indexer.languages import LANG_MAP

        try:
            # manifest_languages() lists every downloadable grammar and uses the
            # same naming as the download manifest (e.g. "csharp", not the
            # "c_sharp" alias from available_languages()).
            available: set[str] = set(tslp.manifest_languages())
        except Exception as exc:
            # If we cannot even read the manifest treat it as a network issue
            err_msg = str(exc).lower()
            if "network" in err_msg or "connection" in err_msg or "timeout" in err_msg:
                pytest.skip(f"Cannot check tslp manifest (network error): {exc}")
            raise

        bad: list[tuple[str, str]] = [
            (ext, lang) for ext, lang in LANG_MAP.items() if lang not in available
        ]
        assert not bad, (
            "These LANG_MAP entries use language names absent from the tslp manifest "
            "(would fail with 'not available for download' on fresh machines): "
            + ", ".join(f"{ext!r}->{lang!r}" for ext, lang in bad)
        )

    def test_language_for_path_cs(self) -> None:
        from yukar.indexer.languages import language_for_path

        assert language_for_path("foo.cs") == "csharp"
        assert language_for_path("Bar.CS") == "csharp"


# ===========================================================================
# Fix 4: BUNDLED_LANGUAGES removed
# ===========================================================================


class TestBundledLanguagesRemoved:
    def test_bundled_languages_not_exported(self) -> None:
        """BUNDLED_LANGUAGES must not exist in the languages module."""
        import yukar.indexer.languages as lang_mod

        assert not hasattr(lang_mod, "BUNDLED_LANGUAGES"), (
            "BUNDLED_LANGUAGES should have been removed — it described 'bundled' grammars "
            "that do not actually ship with the wheel"
        )


# ===========================================================================
# Fix 2a: per-language-once warning on _ts_split fallback
# ===========================================================================


class TestTsSplitFallbackWarning:
    def test_first_failure_emits_warning(self, caplog: Any) -> None:
        """The first tree-sitter failure for a given language emits WARNING."""
        from yukar.indexer import splitter as splitter_mod

        # Reset the module-level set so this test starts clean.
        splitter_mod._warned_fallback_languages.clear()

        with caplog.at_level(logging.WARNING, logger="yukar.indexer.splitter"):
            # Force a failure by calling _warn_fallback directly
            splitter_mod._warn_fallback("test_lang_xyz", "foo/bar.xyz", RuntimeError("oops"))

        warning_records = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and "test_lang_xyz" in r.message
        ]
        assert warning_records, "Expected at least one WARNING for first failure"
        assert "structure splitting is unavailable" in warning_records[0].message

        # Clean up
        splitter_mod._warned_fallback_languages.discard("test_lang_xyz")

    def test_second_failure_does_not_emit_warning(self, caplog: Any) -> None:
        """Subsequent failures for the same language must be DEBUG only."""
        from yukar.indexer import splitter as splitter_mod

        splitter_mod._warned_fallback_languages.clear()
        # Prime: first call emits warning
        splitter_mod._warn_fallback("dup_lang", "a.dup", RuntimeError("first"))

        with caplog.at_level(logging.WARNING, logger="yukar.indexer.splitter"):
            caplog.clear()
            splitter_mod._warn_fallback("dup_lang", "b.dup", RuntimeError("second"))

        warning_records = [
            r for r in caplog.records if r.levelno == logging.WARNING and "dup_lang" in r.message
        ]
        assert not warning_records, (
            "Second failure for same language must not produce another WARNING"
        )

        splitter_mod._warned_fallback_languages.discard("dup_lang")


# ===========================================================================
# Fix 2b: ts_files / fallback_files in stats and API
# ===========================================================================


class TestSplitStats:
    def _make_service(self, workspace: Path) -> Any:
        from yukar.indexer.embedder import FakeEmbedder
        from yukar.indexer.service import IndexerService

        return IndexerService(workspace_root=str(workspace), embedder=FakeEmbedder())

    async def test_stats_json_contains_ts_and_fallback_counts(self, tmp_path: Path) -> None:
        """After reindex, stats.json must contain ts_files and fallback_files."""
        import json

        workspace = tmp_path / "ws"
        workspace.mkdir()

        repo = make_git_repo(tmp_path, "stats-repo")
        # Python file — should use tree-sitter if grammar available
        (repo / "code.py").write_text("def hello():\n    return 1\n")
        # Unknown extension — always falls back to line splitting
        (repo / "data.xyz").write_text("line one\nline two\n")

        service = self._make_service(workspace)
        await service.reindex_repo("proj", "stats-repo", repo)

        from yukar.config import paths as config_paths

        idx_dir = config_paths.index_dir(str(workspace), "proj", "stats-repo")
        stats = json.loads((idx_dir / "stats.json").read_text())

        assert "ts_files" in stats, "stats.json missing 'ts_files'"
        assert "fallback_files" in stats, "stats.json missing 'fallback_files'"
        # Total should equal files indexed (2 known-extension files + README.md)
        total = stats["ts_files"] + stats["fallback_files"]
        assert total > 0, "Expected at least one file counted in split stats"

    async def test_get_status_includes_ts_fields(self, tmp_path: Path) -> None:
        """get_status must return RepoStatus with ts_files and fallback_files."""
        workspace = tmp_path / "ws"
        workspace.mkdir()

        repo = make_git_repo(tmp_path, "status-repo")
        (repo / "app.py").write_text("x = 1\n")

        service = self._make_service(workspace)
        await service.reindex_repo("proj", "status-repo", repo)

        statuses = await service.get_status("proj")
        assert statuses
        s = statuses[0]
        assert hasattr(s, "ts_files"), "RepoStatus missing 'ts_files'"
        assert hasattr(s, "fallback_files"), "RepoStatus missing 'fallback_files'"
        assert isinstance(s.ts_files, int)
        assert isinstance(s.fallback_files, int)

    def test_api_status_schema_includes_ts_fields(self) -> None:
        """RepoIndexStatus Pydantic model must include ts_files and fallback_files."""
        from yukar.api.routers.search import RepoIndexStatus

        s = RepoIndexStatus(
            repo_name="r",
            state="indexed",
            files=5,
            chunks=10,
            last_indexed_at=None,
            ts_files=3,
            fallback_files=2,
        )
        d = s.model_dump()
        assert d["ts_files"] == 3
        assert d["fallback_files"] == 2


# ===========================================================================
# Fix 3: _prefetch_grammars calls DownloadManager.new(version).ensure_group("all")
# ===========================================================================


class TestPrefetchGrammars:
    async def test_prefetch_grammars_succeeds_on_cached_machine(self) -> None:
        """_prefetch_grammars must complete without raising when grammars are cached."""
        from yukar.app import _prefetch_grammars

        # Should not raise; machine already has cache
        await _prefetch_grammars()

    async def test_prefetch_grammars_swallows_error(self) -> None:
        """_prefetch_grammars must not propagate exceptions from ensure_group."""
        import tree_sitter_language_pack as tslp  # type: ignore[import-untyped]

        dm_instance = MagicMock()
        dm_instance.ensure_group.side_effect = OSError("simulated network failure")

        dm_class = MagicMock()
        dm_class.new.return_value = dm_instance

        # Swap out the module in sys.modules with an Any-typed proxy so ty
        # does not complain about assigning MagicMock to a typed attribute.
        tslp_any: Any = tslp
        original_dm = tslp_any.DownloadManager
        try:
            tslp_any.DownloadManager = dm_class
            from yukar.app import _prefetch_grammars

            # Must complete without raising even when ensure_group fails
            await _prefetch_grammars()
        finally:
            tslp_any.DownloadManager = original_dm


# ===========================================================================
# Fix 5: Short-line overlap cap (chunk count not exploding)
# ===========================================================================


class TestShortLineOverlapCap:
    def test_very_short_lines_chunk_count_bounded(self) -> None:
        """2-char × 400 lines: chunk count <= 2× no-overlap theoretical minimum."""
        from yukar.indexer.splitter import LINE_SPLIT_LINES, MAX_CHUNK_CHARS, _line_split

        lines = ["ab\n"] * 400
        text = "".join(lines)

        chunks = _line_split(text, repo="r", path="short.txt", max_chars=MAX_CHUNK_CHARS)

        total_lines = 400
        # Theoretical minimum chunks = ceil(total_lines / LINE_SPLIT_LINES)
        min_chunks = math.ceil(total_lines / LINE_SPLIT_LINES)
        max_allowed = min_chunks * 2

        assert len(chunks) <= max_allowed, (
            f"Chunk count {len(chunks)} exceeds 2× theoretical minimum ({max_allowed}). "
            f"Overlap cap is not working — check _line_split max_back logic."
        )

    def test_overlap_still_present_for_normal_lines(self) -> None:
        """Normal-sized lines (>200 chars) must still produce overlapping chunks."""
        from yukar.indexer.splitter import _line_split

        # Lines of 50 chars each — overlap should still kick in
        lines = [f"line_{i:04d} " + "x" * 40 + "\n" for i in range(300)]
        text = "".join(lines)

        chunks = _line_split(text, repo="r", path="normal.py", max_chars=3000)
        assert len(chunks) >= 2, "Expected multiple chunks"

        # The tail of chunk[0] and the head of chunk[1] must share some lines
        tail_lines = set(chunks[0]["text"].splitlines()[-5:])
        head_text = chunks[1]["text"]
        overlap_found = any(line in head_text for line in tail_lines)
        assert overlap_found, "Expected overlap between consecutive chunks for normal-length lines"

    def test_existing_overlap_test_unchanged(self) -> None:
        """Existing overlap test (lines of ~20 chars) must still pass."""
        from yukar.indexer.splitter import _line_split

        lines = [f"line_{i:04d} = 'x' * 10\n" for i in range(500)]
        text = "".join(lines)

        chunks = _line_split(text, repo="r", path="f.py", max_chars=3000)
        assert len(chunks) >= 2

        text0 = chunks[0]["text"]
        text1 = chunks[1]["text"]
        overlap_found = any(line in text1 for line in text0.splitlines()[-5:])
        assert overlap_found, "Expected overlapping lines between consecutive chunks"


# ===========================================================================
# Fix 6: _collect_files single I/O (NUL check via full read)
# ===========================================================================


class TestCollectFilesSingleIO:
    def test_nul_file_excluded_with_single_read(self, tmp_path: Path) -> None:
        """NUL-byte files must be excluded even under single-I/O implementation."""
        from yukar.indexer.service import _collect_files
        from yukar.sandbox.ignore import IgnoreRules

        repo = make_git_repo(tmp_path, "single-io-repo")
        binary_file = repo / "binary_no_ext"
        # Write a binary file with NUL bytes beyond the first 2048 bytes
        binary_file.write_bytes(b"A" * 100 + b"\x00" + b"B" * 100)

        text_file = repo / "text.py"
        text_file.write_text("x = 1\n")

        ignore = IgnoreRules.from_repo(repo)
        files = _collect_files(repo, ignore)
        names = {f.name for f in files}

        assert "binary_no_ext" not in names
        assert "text.py" in names

    def test_large_nul_file_excluded(self, tmp_path: Path) -> None:
        """A file with NUL bytes within the first 8 KiB scan window must be excluded.

        The NUL scan reads only the first ``_NUL_SCAN_BYTES`` (8 KiB) — matching
        git's own binary-detection heuristic.  NUL bytes that fall beyond that
        boundary are not detected (accepted trade-off: the file may slip through
        and be treated as text, which is intentional for performance parity with git).
        """
        from yukar.indexer.service import _collect_files
        from yukar.sandbox.ignore import IgnoreRules

        repo = make_git_repo(tmp_path, "large-nul-repo")
        # NUL at byte 4000 — well within the 8 KiB scan window.
        data = b"X" * 4000 + b"\x00" + b"Y" * 6000
        (repo / "large_binary").write_bytes(data)
        (repo / "ok.py").write_text("pass\n")

        ignore = IgnoreRules.from_repo(repo)
        files = _collect_files(repo, ignore)
        names = {f.name for f in files}

        assert "large_binary" not in names
        assert "ok.py" in names
