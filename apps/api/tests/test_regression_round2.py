"""Regression tests for code-review fix items (second round).

Covers:
  1. Incremental FAISS ID / chunks.jsonl position skew (fix #1)
  2. _line_split infinite loop on short lines (fix #2)
  3. model_id resolved via get_config() not _model_id (fix #3)
  4. embedding token recording via run_coroutine_threadsafe (fix #4)
  5. dimension mismatch → full rebuild fallback (fix #5)
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from tests._helpers import make_git_repo

# ---------------------------------------------------------------------------
# Fix #1 — Incremental FAISS ID / chunks.jsonl position skew
# ---------------------------------------------------------------------------


class TestIncrementalFaissIdPositionSkew:
    """Verify that updated files are searchable after an incremental reindex.

    The bug: _incremental_update assigned new FAISS IDs starting at
    len(existing_chunks) but _do_search resolved results via chunks[idx]
    (positional lookup).  After an incremental update the IDs diverged from
    positions and the changed file's chunks could not be found.
    """

    def _make_service(self, workspace: Path) -> Any:
        from yukar.indexer.embedder import FakeEmbedder
        from yukar.indexer.service import IndexerService

        return IndexerService(workspace_root=str(workspace), embedder=FakeEmbedder(dim=32))

    async def test_changed_file_searchable_after_incremental_update(self, tmp_path: Path) -> None:
        """3 files → initial full index → change 1 file → incremental update →
        searching with the exact text of the updated chunk must return it.

        FakeEmbedder is deterministic (SHA-256 hash → vector) so querying the
        exact chunk text yields distance=0 for that chunk.  After the incremental
        update the updated chunk must resolve correctly via the ID→chunk dict
        (this is the regression: the old positional lookup returned wrong chunks
        because new FAISS IDs diverged from jsonl positions after removes+appends).
        """
        workspace = tmp_path / "ws"
        workspace.mkdir()
        repo = make_git_repo(tmp_path, "skew-repo")

        # Create 3 files so the index has multiple chunks.
        updated_text = "def beta():\n    return 'UPDATED_UNIQUE_CONTENT_XYZ'\n"
        (repo / "a.py").write_text("def alpha():\n    return 'original_alpha'\n")
        (repo / "b.py").write_text("def beta():\n    return 'original_beta'\n")
        (repo / "c.py").write_text("def gamma():\n    return 'original_gamma'\n")

        service = self._make_service(workspace)

        # Full initial build.
        await service.reindex_repo("proj", "skew-repo", repo, full=True)

        # Update b.py with distinctive new content.
        import time

        time.sleep(0.01)  # ensure mtime differs
        (repo / "b.py").write_text(updated_text)

        # Incremental update.
        await service.reindex_repo("proj", "skew-repo", repo, full=False)

        # Load the updated index and find which chunk_id holds b.py's new content.
        from yukar.config import paths as config_paths
        from yukar.indexer import faiss_store

        idx_dir = config_paths.index_dir(str(workspace), "proj", "skew-repo")
        chunks, _ = await faiss_store.load_index(idx_dir)
        b_chunks = [ch for ch in chunks if ch["path"] == "b.py"]
        assert b_chunks, "b.py must be present in the updated index"
        assert any(updated_text.strip() in ch["text"] for ch in b_chunks), (
            f"Updated content not in b.py chunks: {[ch['text'] for ch in b_chunks]}"
        )

        # Search using the exact updated chunk text → must find b.py (distance ≈ 0).
        # With FakeEmbedder the exact same text produces the same vector → nearest.
        results = await service.search("proj", updated_text, repo_name="skew-repo", top_k=5)
        assert results, "Expected at least one search result after incremental update"
        top_chunk, top_dist = results[0]
        assert top_chunk["path"] == "b.py", (
            f"Top hit must be b.py (dist={top_dist:.4f}), got path={top_chunk['path']!r}\n"
            f"Results: {[(c['path'], d) for c, d in results]}"
        )

    async def test_stable_chunk_ids_in_jsonl(self, tmp_path: Path) -> None:
        """After a full index, every chunk in chunks.jsonl must have a chunk_id field."""
        workspace = tmp_path / "ws"
        workspace.mkdir()
        repo = make_git_repo(tmp_path, "ids-repo")
        (repo / "x.py").write_text("x = 1\n")

        service = self._make_service(workspace)
        await service.reindex_repo("proj", "ids-repo", repo, full=True)

        from yukar.config import paths as config_paths
        from yukar.indexer import faiss_store

        idx_dir = config_paths.index_dir(str(workspace), "proj", "ids-repo")
        chunks, _ = await faiss_store.load_index(idx_dir)
        assert chunks, "Expected at least one chunk"
        for i, ch in enumerate(chunks):
            assert "chunk_id" in ch, f"chunk[{i}] missing chunk_id field: {ch}"
            assert isinstance(ch["chunk_id"], int), (
                f"chunk[{i}].chunk_id must be int, got {type(ch['chunk_id'])}"
            )

    async def test_incremental_ids_are_unique_and_stable(self, tmp_path: Path) -> None:
        """After an incremental update, all chunk_ids in the persisted jsonl are unique."""
        workspace = tmp_path / "ws"
        workspace.mkdir()
        repo = make_git_repo(tmp_path, "uniq-ids-repo")
        (repo / "a.py").write_text("def a(): pass\n")
        (repo / "b.py").write_text("def b(): pass\n")

        service = self._make_service(workspace)
        await service.reindex_repo("proj", "uniq-ids-repo", repo, full=True)

        import time

        time.sleep(0.01)
        (repo / "b.py").write_text("def b(): return 99\n")
        await service.reindex_repo("proj", "uniq-ids-repo", repo, full=False)

        from yukar.config import paths as config_paths
        from yukar.indexer import faiss_store

        idx_dir = config_paths.index_dir(str(workspace), "proj", "uniq-ids-repo")
        chunks, _ = await faiss_store.load_index(idx_dir)
        ids = [ch.get("chunk_id") for ch in chunks]
        assert len(ids) == len(set(ids)), f"Duplicate chunk_ids after incremental update: {ids}"


# ---------------------------------------------------------------------------
# Fix #2 — _line_split infinite loop on short lines
# ---------------------------------------------------------------------------


class TestLineSplitInfiniteLoop:
    """_line_split must complete in finite time even when all lines are shorter
    than CHUNK_OVERLAP_CHARS."""

    def test_short_lines_terminate(self) -> None:
        """200 short lines (each 2 chars) must produce a non-empty list quickly."""
        from yukar.indexer.splitter import _line_split

        # Each line is "a\n" (2 chars). CHUNK_OVERLAP_CHARS=200 means the overlap
        # would consume the entire window without the forward-progress guard.
        text = "a\n" * 200
        # This must complete in finite time (no infinite loop).
        chunks = _line_split(text, repo="r", path="f.txt", max_chars=3000)
        assert chunks, "Expected at least one chunk"

    def test_all_lines_covered_after_split(self) -> None:
        """All input lines must appear in at least one chunk."""
        from yukar.indexer.splitter import _line_split

        n = 200
        lines = [f"line{i}\n" for i in range(n)]
        text = "".join(lines)
        chunks = _line_split(text, repo="r", path="f.txt", max_chars=3000)

        # Collect all text across chunks.
        all_chunk_text = "".join(c["text"] for c in chunks)
        for i in range(n):
            assert f"line{i}" in all_chunk_text, f"line{i} missing from chunk output"

    def test_very_short_lines_no_missing_first_chunk_content(self) -> None:
        """The first chunk must include the first line of input."""
        from yukar.indexer.splitter import _line_split

        text = "a\n" * 200
        chunks = _line_split(text, repo="r", path="f.txt", max_chars=3000)
        assert chunks[0]["text"].startswith("a\n"), (
            f"First chunk must start with 'a\\n', got: {chunks[0]['text'][:20]!r}"
        )


# ---------------------------------------------------------------------------
# Fix #3 — model_id resolved via get_config(), not _model_id
# ---------------------------------------------------------------------------


class TestModelIdResolution:
    """resolve_model_id must prefer get_config()['model_id'] over _model_id."""

    def test_get_config_takes_priority(self) -> None:
        from yukar.agents.streaming import resolve_model_id as _resolve_model_id

        class MockModel:
            _model_id = "wrong-from-attr"

            def get_config(self) -> dict[str, str]:
                return {"model_id": "correct-from-get-config"}

        assert _resolve_model_id(MockModel()) == "correct-from-get-config"

    def test_falls_back_to_underscore_attr_when_no_get_config(self) -> None:
        from yukar.agents.streaming import resolve_model_id as _resolve_model_id

        class MockModel:
            _model_id = "fallback-model"

        assert _resolve_model_id(MockModel()) == "fallback-model"

    def test_returns_unknown_when_neither_available(self) -> None:
        from yukar.agents.streaming import resolve_model_id as _resolve_model_id

        class EmptyModel:
            pass

        assert _resolve_model_id(EmptyModel()) == "unknown"

    def test_get_config_returns_none_falls_back_to_attr(self) -> None:
        from yukar.agents.streaming import resolve_model_id as _resolve_model_id

        class MockModel:
            _model_id = "attr-fallback"

            def get_config(self) -> dict[str, object]:
                return {"model_id": None}  # None → fall through

        # None is falsy so _resolve_model_id must fall back.
        assert _resolve_model_id(MockModel()) == "attr-fallback"

    def test_get_config_raises_falls_back_to_attr(self) -> None:
        from yukar.agents.streaming import resolve_model_id as _resolve_model_id

        class BrokenModel:
            _model_id = "attr-from-broken"

            def get_config(self) -> dict[str, str]:
                raise RuntimeError("no config")

        assert _resolve_model_id(BrokenModel()) == "attr-from-broken"


# ---------------------------------------------------------------------------
# Fix #4 — embedding token recording via run_coroutine_threadsafe
# ---------------------------------------------------------------------------


class TestEmbeddingUsageTracking:
    """embed_batch via to_thread must record tokens to the usage tracker."""

    async def test_embed_batch_records_tokens_via_thread(self, tmp_path: Path) -> None:
        """FakeEmbedder.embed_batch called from a worker thread (via to_thread)
        must record embedding tokens to the tracker when a loop is injected."""
        from unittest.mock import patch

        from yukar.indexer.embedder import FakeEmbedder
        from yukar.usage.tracker import TokenUsageTracker, init_tracker

        ledger = tmp_path / "ledger.yaml"
        tracker = TokenUsageTracker(ledger_path=ledger)
        init_tracker(tracker)

        loop = asyncio.get_running_loop()
        embedder = FakeEmbedder(dim=32, project_id="p1", run_id="r1")
        embedder.set_event_loop(loop)

        texts = ["hello world", "foo bar baz"]
        # Run embed_batch in a worker thread — same as service._embed_chunks does.
        # Suppress SSE publish for isolation.
        with patch.object(tracker, "_publish_sse"):
            await asyncio.to_thread(embedder.embed_batch, texts)
            # Give the run_coroutine_threadsafe future time to execute.
            await asyncio.sleep(0.1)

        rt = tracker.get_run_totals("r1")
        assert rt is not None, "No usage record created for run_id='r1'"
        assert rt.embedding_tokens > 0, f"Expected embedding_tokens > 0, got {rt.embedding_tokens}"

    async def test_embed_batch_without_loop_is_noop(self, tmp_path: Path) -> None:
        """FakeEmbedder.embed_batch without a loop must not crash and returns vectors."""
        from yukar.indexer.embedder import FakeEmbedder

        embedder = FakeEmbedder(dim=32)
        # No set_event_loop call → _loop is None → _record_embedding_usage_sync is a no-op.
        vecs = await asyncio.to_thread(embedder.embed_batch, ["test text"])
        assert len(vecs) == 1
        assert len(vecs[0]) == 32


# ---------------------------------------------------------------------------
# Fix #5 — dimension mismatch → full rebuild fallback
# ---------------------------------------------------------------------------


class TestDimensionMismatchFallback:
    """Incremental reindex on an existing index with a different embedding
    dimension must transparently fall back to a full rebuild."""

    async def test_dimension_mismatch_triggers_full_rebuild(self, tmp_path: Path) -> None:
        """Build index with dim=32 embedder, then reindex with dim=64 embedder.
        The incremental path must fall back to full rebuild and the new index
        must contain chunks embedded by the dim=64 embedder."""
        from yukar.indexer.embedder import FakeEmbedder
        from yukar.indexer.service import IndexerService

        workspace = tmp_path / "ws"
        workspace.mkdir()
        repo = make_git_repo(tmp_path, "dim-repo")
        (repo / "code.py").write_text("def foo(): pass\n")

        # Build with dim=32.
        service32 = IndexerService(workspace_root=str(workspace), embedder=FakeEmbedder(dim=32))
        await service32.reindex_repo("proj", "dim-repo", repo, full=True)

        from yukar.config import paths as config_paths
        from yukar.indexer import faiss_store

        idx_dir = config_paths.index_dir(str(workspace), "proj", "dim-repo")
        chunks_32, _ = await faiss_store.load_index(idx_dir)
        assert chunks_32, "Expected initial chunks with dim=32"

        # Now try incremental update with a dim=64 embedder.
        service64 = IndexerService(workspace_root=str(workspace), embedder=FakeEmbedder(dim=64))
        # full=False triggers the incremental path, which must detect mismatch
        # and fall back to a full rebuild.
        chunk_count = await service64.reindex_repo("proj", "dim-repo", repo, full=False)
        assert chunk_count > 0, "Expected non-zero chunk count after dimension-mismatch rebuild"

        # Verify the rebuilt index can be loaded without error.
        chunks_64, faiss_idx = await faiss_store.load_index(idx_dir)
        assert chunks_64, "Expected chunks after full rebuild due to dimension mismatch"

    async def test_incremental_dimension_mismatch_error_raised_in_update_index(
        self, tmp_path: Path
    ) -> None:
        """update_index must raise IncrementalDimensionMismatchError when the
        stored dimension differs from the new vector dimension."""
        from yukar.indexer.embedder import FakeEmbedder
        from yukar.indexer.faiss_store import (
            IncrementalDimensionMismatchError,
            save_index,
            update_index,
        )
        from yukar.indexer.splitter import split_file

        workspace = tmp_path / "ws"
        workspace.mkdir()
        repo = make_git_repo(tmp_path, "dim-err-repo")
        (repo / "f.py").write_text("x = 1\n")

        emb32 = FakeEmbedder(dim=32)
        emb64 = FakeEmbedder(dim=64)

        idx_dir = tmp_path / "idx"
        idx_dir.mkdir()

        # Build index with dim=32.
        chunks = split_file("x = 1\n", repo="r", path="f.py")
        vecs32 = emb32.embed_batch([c["text"] for c in chunks])
        await save_index(idx_dir, chunks, vecs32, project_id="p", repo_name="r")

        # Attempt incremental update with dim=64 vectors → must raise.
        vecs64 = emb64.embed_batch([c["text"] for c in chunks])
        with pytest.raises(IncrementalDimensionMismatchError):
            await update_index(
                idx_dir,
                chunks,
                project_id="p",
                repo_name="r",
                changed_chunks=chunks,
                changed_vectors=vecs64,
            )
