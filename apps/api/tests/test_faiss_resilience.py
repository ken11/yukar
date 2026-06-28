"""Regression tests: _read_index must tolerate corrupt lines in chunks.jsonl.

A single bad JSONL line must be skipped with a warning; all valid chunks must
still be returned and search must hit them correctly.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_chunks_jsonl(path: Path, rows: list[Any]) -> None:
    """Write *rows* to *path* as JSONL (one JSON value per line)."""
    lines = [json.dumps(row, ensure_ascii=False) for row in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _make_fake_faiss_index(dim: int, chunk_ids: list[int], vectors: list[list[float]]) -> Any:
    """Build a minimal IndexIDMap2 wrapping IndexFlatL2 with given vectors."""
    import faiss  # type: ignore[import-untyped]
    import numpy as np

    inner = faiss.IndexFlatL2(dim)
    idx = faiss.IndexIDMap2(inner)
    if vectors:
        mat = np.array(vectors, dtype=np.float32)
        ids = np.array(chunk_ids, dtype=np.int64)
        idx.add_with_ids(mat, ids)
    return idx


def _write_faiss_index(index_dir: Path, faiss_index: Any) -> None:
    """Write a FAISS index to *index_dir*/faiss.index."""
    import faiss  # type: ignore[import-untyped]

    index_dir.mkdir(parents=True, exist_ok=True)
    faiss.write_index(faiss_index, str(index_dir / "faiss.index"))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestReadIndexResilience:
    """_read_index tolerates corrupt lines in chunks.jsonl."""

    def _setup_index(
        self, index_dir: Path, good_chunks: list[dict[str, Any]], extra_lines: list[str]
    ) -> None:
        """Write a FAISS index + chunks.jsonl with *extra_lines* appended."""
        dim = 4
        vectors = [[float(i), 0.0, 0.0, 0.0] for i in range(len(good_chunks))]
        chunk_ids = [int(ch["chunk_id"]) for ch in good_chunks]

        faiss_idx = _make_fake_faiss_index(dim, chunk_ids, vectors)
        _write_faiss_index(index_dir, faiss_idx)

        # Write chunks.jsonl: good chunks first, then extra (possibly corrupt) lines.
        normal_lines = [json.dumps(ch, ensure_ascii=False) for ch in good_chunks]
        all_lines = normal_lines + extra_lines
        (index_dir / "chunks.jsonl").write_text("\n".join(all_lines) + "\n", encoding="utf-8")

    def test_corrupt_json_line_is_skipped(self, tmp_path: Path) -> None:
        """A line that is not valid JSON must be skipped; valid chunks are returned."""
        from yukar.indexer.faiss_store import _read_index

        index_dir = tmp_path / "idx"
        good_chunks = [
            {"chunk_id": 0, "text": "alpha", "path": "a.py", "start_line": 0, "end_line": 1},
            {"chunk_id": 1, "text": "beta", "path": "b.py", "start_line": 0, "end_line": 1},
        ]
        self._setup_index(index_dir, good_chunks, extra_lines=["NOT_VALID_JSON{{{"])

        chunks, _ = _read_index(index_dir)
        assert len(chunks) == 2, f"Expected 2 chunks, got {len(chunks)}"
        texts = {ch["text"] for ch in chunks}
        assert texts == {"alpha", "beta"}

    def test_non_dict_json_line_is_skipped(self, tmp_path: Path) -> None:
        """A line that parses as a JSON array (not an object) must be skipped."""
        from yukar.indexer.faiss_store import _read_index

        index_dir = tmp_path / "idx"
        good_chunks = [
            {"chunk_id": 0, "text": "gamma", "path": "c.py", "start_line": 0, "end_line": 1},
        ]
        self._setup_index(index_dir, good_chunks, extra_lines=["[1, 2, 3]"])

        chunks, _ = _read_index(index_dir)
        assert len(chunks) == 1
        assert chunks[0]["text"] == "gamma"

    def test_corrupt_line_emits_warning(
        self, tmp_path: Path, caplog: Any
    ) -> None:
        """A corrupt JSONL line must emit a logger.warning."""
        from yukar.indexer.faiss_store import _read_index

        index_dir = tmp_path / "idx"
        good_chunks = [
            {"chunk_id": 0, "text": "delta", "path": "d.py", "start_line": 0, "end_line": 1},
        ]
        self._setup_index(index_dir, good_chunks, extra_lines=["BAD{{"])

        with caplog.at_level(logging.WARNING, logger="yukar.indexer.faiss_store"):
            _read_index(index_dir)

        msgs = [r.message for r in caplog.records]
        assert any("JSON parse failed" in m for m in msgs), (
            f"Expected a warning about JSON parse failure; got: {msgs}"
        )

    def test_all_valid_lines_survive_mixed_corruption(self, tmp_path: Path) -> None:
        """Multiple corrupt lines interspersed with valid ones must all be skipped
        while every valid chunk is returned."""
        from yukar.indexer.faiss_store import _read_index

        index_dir = tmp_path / "idx"
        good_chunks = [
            {"chunk_id": 0, "text": "one", "path": "x.py", "start_line": 0, "end_line": 1},
            {"chunk_id": 1, "text": "two", "path": "x.py", "start_line": 2, "end_line": 3},
            {"chunk_id": 2, "text": "three", "path": "x.py", "start_line": 4, "end_line": 5},
        ]
        # Write manually to interleave bad lines between good ones.
        dim = 4
        vectors = [[float(i), 0.0, 0.0, 0.0] for i in range(3)]
        faiss_idx = _make_fake_faiss_index(dim, [0, 1, 2], vectors)
        _write_faiss_index(index_dir, faiss_idx)

        lines = [
            json.dumps(good_chunks[0]),
            "BROKEN{{",          # corrupt
            json.dumps(good_chunks[1]),
            "[42]",              # valid JSON but not a dict
            json.dumps(good_chunks[2]),
        ]
        (index_dir / "chunks.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

        chunks, _ = _read_index(index_dir)
        assert len(chunks) == 3, f"Expected 3 good chunks, got {len(chunks)}"
        texts = {ch["text"] for ch in chunks}
        assert texts == {"one", "two", "three"}

    async def test_load_index_returns_good_chunks_despite_corruption(
        self, tmp_path: Path
    ) -> None:
        """load_index (the async wrapper) must propagate resilient parsing."""
        from yukar.indexer.faiss_store import load_index

        index_dir = tmp_path / "idx"
        good_chunks = [
            {"chunk_id": 0, "text": "hello", "path": "h.py", "start_line": 0, "end_line": 1},
            {"chunk_id": 1, "text": "world", "path": "h.py", "start_line": 2, "end_line": 3},
        ]
        dim = 4
        vectors = [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]]
        faiss_idx = _make_fake_faiss_index(dim, [0, 1], vectors)
        _write_faiss_index(index_dir, faiss_idx)

        # Inject one corrupt line between the two valid ones.
        lines = [
            json.dumps(good_chunks[0]),
            "{{corrupt}}",
            json.dumps(good_chunks[1]),
        ]
        (index_dir / "chunks.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

        chunks, faiss_index = await load_index(index_dir)
        assert len(chunks) == 2
        assert {ch["text"] for ch in chunks} == {"hello", "world"}
        assert faiss_index.ntotal == 2
