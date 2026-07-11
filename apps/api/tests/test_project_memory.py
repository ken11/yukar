"""Tests for Project Memory (cross-Epic) — memory/ package.

Coverage:
- add→search round-trip (concrete assert on content / metadata)
- provenance (epic_id / category / repo)
- content hash deduplication (case / whitespace boundaries)
- jsonl append/parse round-trip · parse of human-edited file
- rebuild, dimension-mismatch rebuild, rebuild→add success from empty jsonl
- FAISS index consistency under concurrent add() (ntotal / searchability)
- native wiring: MemoryManager attached to Manager with injection trigger=userTurn ·
  search_memory tool exists · not attached to Worker/Evaluator
- injection is ephemeral (seam: not persisted to durable session)
- remember tool writes to store (the only memory-write path)
- usage: path where embed receives loop injection and usage delta is recorded
- JSONL adversarial input: ## mem-NNNN / code fences / fake meta lines / JSON symbols
  in body do not collide with boundaries → 1 record, content fully preserved (regression)
- hand-edit resilience: skip corrupt single-line JSON
- B1: no orphan on embed failure / embed_failed distinction
- B2: past entries recoverable via search after dimension change
- B3: hand-edit of project.jsonl reflected after rebuild
- D2: store.description reflected in search_memory tool description
- D3: concurrent add real assertions (success count · ntotal==distinct count · all searchable)
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from yukar.indexer.embedder import FAKE_DIM, FakeEmbedder
from yukar.memory.index import _rebuild_index as rebuild_index
from yukar.memory.index import search_index_memory
from yukar.memory.rebuild import rebuild_memory_index
from yukar.memory.records import append_record, parse_records
from yukar.memory.store import EmbedFailedError, ProjectMemoryStore

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def embedder() -> FakeEmbedder:
    return FakeEmbedder(dim=FAKE_DIM)


@pytest.fixture
def mem_store(tmp_path: Path, embedder: FakeEmbedder) -> ProjectMemoryStore:
    return ProjectMemoryStore(
        jsonl_path=tmp_path / "project.jsonl",
        index_dir=tmp_path / "index",
        embedder=embedder,
        project_id="test-proj",
        epic_id="ep-1",
    )


# ---------------------------------------------------------------------------
# records.py tests (JSONL)
# ---------------------------------------------------------------------------


class TestJsonlAppendParse:
    """append / parse round-trip for the canonical project.jsonl."""

    async def test_append_creates_file(self, tmp_path: Path) -> None:
        jsonl = tmp_path / "project.jsonl"
        record = await append_record(
            jsonl,
            "Test convention: annotate all types",
            category="convention",
            epic_id="ep-1",
        )
        assert record is not None
        assert record.id == "mem-0001"
        assert record.category == "convention"
        assert jsonl.exists()

    async def test_parse_roundtrip(self, tmp_path: Path) -> None:
        jsonl = tmp_path / "project.jsonl"
        await append_record(
            jsonl, "Convention A", category="convention", epic_id="ep-1", repo="repo1"
        )
        await append_record(jsonl, "Lesson B", category="lesson", epic_id="ep-2", repo="repo2")

        records = parse_records(jsonl.read_text())
        assert len(records) == 2
        assert records[0].content == "Convention A"
        assert records[0].category == "convention"
        assert records[0].epic_id == "ep-1"
        assert records[0].repo == "repo1"
        assert records[1].content == "Lesson B"
        assert records[1].category == "lesson"
        assert records[1].epic_id == "ep-2"

    async def test_id_increment(self, tmp_path: Path) -> None:
        jsonl = tmp_path / "project.jsonl"
        r1 = await append_record(jsonl, "first")
        r2 = await append_record(jsonl, "second")
        assert r1 is not None and r2 is not None
        assert r1.id == "mem-0001"
        assert r2.id == "mem-0002"

    async def test_duplicate_content_skipped(self, tmp_path: Path) -> None:
        jsonl = tmp_path / "project.jsonl"
        r1 = await append_record(jsonl, "same content")
        r2 = await append_record(jsonl, "same content")
        assert r1 is not None
        assert r2 is None  # duplicate skipped

        records = parse_records(jsonl.read_text())
        assert len(records) == 1

    async def test_duplicate_case_insensitive(self, tmp_path: Path) -> None:
        jsonl = tmp_path / "project.jsonl"
        await append_record(jsonl, "Hello World")
        r2 = await append_record(jsonl, "hello world")
        assert r2 is None  # case-normalized duplicate detected

    async def test_duplicate_whitespace_normalized(self, tmp_path: Path) -> None:
        jsonl = tmp_path / "project.jsonl"
        await append_record(jsonl, "a  b   c")
        r2 = await append_record(jsonl, "a b c")
        assert r2 is None  # whitespace-normalized duplicate

    def test_parse_human_edited_file(self, tmp_path: Path) -> None:
        """Manually hand-edited project.jsonl can also be parsed."""
        import json

        jsonl = tmp_path / "project.jsonl"
        line1 = json.dumps(
            {
                "id": "mem-0001",
                "content": "Write code with type annotations.",
                "category": "convention",
                "epic_id": "ep-1",
                "task_id": "T-1",
                "repo": "main-repo",
                "created": "2024-01-01",
                "source": "remember",
            },
            ensure_ascii=False,
        )
        line2 = json.dumps(
            {
                "id": "mem-0002",
                "content": "Read the design document first.",
                "category": "lesson",
                "epic_id": "ep-2",
                "task_id": "-",
                "repo": "-",
                "created": "2024-02-01",
                "source": "remember",
            },
            ensure_ascii=False,
        )
        jsonl.write_text(line1 + "\n" + line2 + "\n", encoding="utf-8")

        records = parse_records(jsonl.read_text())
        assert len(records) == 2
        assert records[0].id == "mem-0001"
        assert records[0].content == "Write code with type annotations."
        assert records[0].category == "convention"
        assert records[1].id == "mem-0002"
        assert records[1].content == "Read the design document first."
        assert records[1].category == "lesson"

    # ------------------------------------------------------------------
    # Hand-edit resilience: corrupt-line skip test
    # ------------------------------------------------------------------

    def test_corrupt_line_skipped(self, tmp_path: Path) -> None:
        """Other records can still be parsed even when one JSON line is corrupt
        (hand-edit resilience)."""
        import json

        jsonl = tmp_path / "project.jsonl"
        good = json.dumps(
            {"id": "mem-0001", "content": "good line", "category": "fact"}, ensure_ascii=False
        )
        bad = "{ broken json @@@@"
        good2 = json.dumps(
            {"id": "mem-0002", "content": "second line", "category": "fact"}, ensure_ascii=False
        )
        jsonl.write_text(good + "\n" + bad + "\n" + good2 + "\n", encoding="utf-8")

        records = parse_records(jsonl.read_text())
        # corrupt line is skipped; 2 good lines are readable
        assert len(records) == 2, f"Failed to skip corrupt line: {[r.id for r in records]}"
        assert records[0].content == "good line"
        assert records[1].content == "second line"

    # ------------------------------------------------------------------
    # Adversarial input tests (JSONL is structurally safe)
    # ------------------------------------------------------------------

    async def test_adversarial_a_mem_heading_in_body(self, tmp_path: Path) -> None:
        """(a) Body contains leading '## mem-0009' + next line 'source: epic=x task=- repo=-':
        1 record, content fully preserved, numbering not contaminated."""
        jsonl = tmp_path / "project.jsonl"
        content = "Related notes:\n## mem-0009\nsource: epic=x task=- repo=-\nSupplemental text"
        r = await append_record(jsonl, content, category="fact")
        assert r is not None
        assert r.id == "mem-0001", f"Numbering contaminated: {r.id}"

        records = parse_records(jsonl.read_text())
        assert len(records) == 1, f"Phantom record created: {[rec.id for rec in records]}"
        assert records[0].content == content.strip(), (
            f"content changed: {records[0].content!r}"
        )
        # phantom mem-0009 must not exist
        assert all(r.id != "mem-0009" for r in records)
        # Next numbering must not be contaminated (should be 0002, not 0009)
        r2 = await append_record(jsonl, "Follow-up record", category="fact")
        assert r2 is not None and r2.id == "mem-0002", f"Numbering contaminated: {r2 and r2.id}"

    async def test_adversarial_b_code_fence_headings(self, tmp_path: Path) -> None:
        """(b) Body contains '## mem-0007' / '## Heading' inside a code fence:
        1 record, content fully preserved."""
        jsonl = tmp_path / "project.jsonl"
        content = "Example:\n```\n## mem-0007\n## Heading\n```\nExplanation"
        r = await append_record(jsonl, content, category="fact")
        assert r is not None

        records = parse_records(jsonl.read_text())
        assert len(records) == 1, f"Split into multiple records: {[rec.id for rec in records]}"
        assert "## mem-0007" in records[0].content
        assert "## Heading" in records[0].content

    async def test_adversarial_c_multiline_with_created(self, tmp_path: Path) -> None:
        """(c) Body contains multiple lines + a 'created: yesterday' line:
        1 record, content fully preserved, no phantom."""
        jsonl = tmp_path / "project.jsonl"
        content = "Line 1\nLine 2\ncreated: yesterday\nLine 3"
        r = await append_record(jsonl, content, category="fact")
        assert r is not None

        records = parse_records(jsonl.read_text())
        assert len(records) == 1, f"Phantom record created: {[rec.id for rec in records]}"
        assert "created: yesterday" in records[0].content, (
            f"'created: yesterday' was lost: {records[0].content!r}"
        )

    async def test_adversarial_d_json_special_chars(self, tmp_path: Path) -> None:
        """(d) content contains newlines, quotes, JSON special chars ({ } " \\n):
        1 record, content matches exactly."""
        jsonl = tmp_path / "project.jsonl"
        content = 'line1\n{"key": "value"}\n"quoted"\nline4'
        r = await append_record(jsonl, content, category="fact")
        assert r is not None

        records = parse_records(jsonl.read_text())
        assert len(records) == 1, f"Split by JSON special chars: {[rec.id for rec in records]}"
        assert records[0].content == content.strip(), (
            f"content changed: {records[0].content!r}"
        )


# ---------------------------------------------------------------------------
# store.py: add→search round-trip
# ---------------------------------------------------------------------------


class TestProjectMemoryStoreRoundtrip:
    async def test_add_and_search_basic(self, mem_store: ProjectMemoryStore) -> None:
        """add → search round-trip: concrete assert on content / metadata."""
        record_id = await mem_store.add(
            "Use test-driven development",
            metadata={"category": "convention", "repo": "main", "epic_id": "ep-1"},
        )
        assert record_id is not None

        entries = await mem_store.search("test-driven development")
        assert len(entries) >= 1
        found = next((e for e in entries if "test-driven development" in e.content), None)
        assert found is not None
        assert found.store_name == "project_memory"
        assert found.metadata is not None
        assert found.metadata.get("category") == "convention"

    async def test_add_multiple_and_search_returns_top_k(
        self, mem_store: ProjectMemoryStore
    ) -> None:
        for i in range(10):
            await mem_store.add(f"unique knowledge item-{i}", metadata={"category": "fact"})

        entries = await mem_store.search("unique knowledge", options={"max_search_results": 3})
        assert len(entries) <= 3

    async def test_search_empty_store_returns_empty(self, mem_store: ProjectMemoryStore) -> None:
        entries = await mem_store.search("anything")
        assert entries == []

    async def test_provenance_metadata(self, mem_store: ProjectMemoryStore) -> None:
        """provenance (epic_id / category / repo) is included in search result metadata."""
        await mem_store.add(
            "Repo-specific lesson",
            metadata={
                "category": "lesson",
                "repo": "backend-repo",
                "epic_id": "ep-42",
                "source": "remember",
            },
        )
        entries = await mem_store.search("Repo-specific lesson")
        assert len(entries) >= 1
        meta = entries[0].metadata
        assert meta is not None
        assert meta.get("category") == "lesson"
        assert meta.get("repo") == "backend-repo"
        assert meta.get("epic_id") == "ep-42"
        assert meta.get("source") == "remember"

    async def test_duplicate_add_skipped(self, mem_store: ProjectMemoryStore) -> None:
        r1 = await mem_store.add("text for duplicate check")
        r2 = await mem_store.add("text for duplicate check")
        assert r1 is not None
        assert r2 is None  # duplicate returns None

        entries = await mem_store.search("text for duplicate check")
        assert len(entries) == 1

    async def test_store_attributes(self, mem_store: ProjectMemoryStore) -> None:
        assert mem_store.name == "project_memory"
        assert mem_store.description  # non-empty meaningful string
        assert mem_store.writable is True
        assert mem_store.extraction is False
        assert mem_store.max_search_results == 5


# ---------------------------------------------------------------------------
# index.py: rebuild / dimension mismatch / rebuild→add from empty jsonl
# ---------------------------------------------------------------------------


class TestIndexBehavior:
    async def test_rebuild_from_records(self, tmp_path: Path, embedder: FakeEmbedder) -> None:
        """Index can be rebuilt from the canonical source."""
        index_dir = tmp_path / "idx"
        records = [
            {"record_id": "mem-0001", "content": "Convention A", "category": "convention"},
            {"record_id": "mem-0002", "content": "Lesson B", "category": "lesson"},
        ]
        vectors = embedder.embed_batch(["Convention A", "Lesson B"])
        await rebuild_index(index_dir, records, vectors, project_id="test")

        hits = await search_index_memory(
            index_dir, embedder.embed_batch(["Convention A"])[0], top_k=1, project_id="test"
        )
        assert len(hits) == 1
        chunk, dist = hits[0]
        assert chunk.get("content") == "Convention A"
        assert chunk.get("category") == "convention"

    async def test_empty_jsonl_rebuild_then_add_succeeds(
        self, tmp_path: Path, embedder: FakeEmbedder
    ) -> None:
        """No FAISS assertion failure when adding after rebuild from empty jsonl."""
        jsonl = tmp_path / "project.jsonl"
        index_dir = tmp_path / "idx"

        # Rebuild with empty jsonl (ntotal==0, dim=1 placeholder)
        await rebuild_memory_index(jsonl, index_dir, embedder, project_id="test")

        # add after that does not break
        store = ProjectMemoryStore(
            jsonl_path=jsonl,
            index_dir=index_dir,
            embedder=embedder,
            project_id="test",
            epic_id="ep-1",
        )
        result = await store.add("added content", metadata={"category": "fact"})
        assert result is not None

        # can be searched
        entries = await store.search("added content")
        assert len(entries) >= 1
        assert "added content" in entries[0].content

    async def test_dimension_mismatch_rebuild(self, tmp_path: Path) -> None:
        """B2: Past entries are recoverable via search after a dimension mismatch."""
        index_dir = tmp_path / "idx"
        jsonl = tmp_path / "project.jsonl"

        # First build index with dim=64
        emb64 = FakeEmbedder(dim=64)
        # Write to canonical source too (so re-embed happens at rebuild)
        await append_record(jsonl, "initial content", category="fact")
        records = [{"record_id": "mem-0001", "content": "initial content", "category": "fact"}]
        vectors64 = emb64.embed_batch(["initial content"])
        await rebuild_index(index_dir, records, vectors64, project_id="test-dim")

        # Then add with dim=128 store → detect mismatch and rebuild
        emb128 = FakeEmbedder(dim=128)
        store = ProjectMemoryStore(
            jsonl_path=jsonl,
            index_dir=index_dir,
            embedder=emb128,
            project_id="test-dim",
            epic_id="ep-1",
        )
        # add must not fail (mismatch → rebuild_memory_index → rebuild all entries)
        result = await store.add("new content", metadata={"category": "fact"})
        assert result is not None, "add failed after dim mismatch"

        # Past entries must be recoverable via search after rebuild (B2 normal case)
        entries = await store.search("initial content")
        assert len(entries) >= 1, "Past entries not searchable after rebuild"
        contents = {e.content for e in entries}
        assert "initial content" in contents

        # New entry must also be searchable
        entries2 = await store.search("new content")
        assert len(entries2) >= 1

        # B2: new record must not be double-registered via rebuild path (ntotal == distinct count).
        # Canonical source has 2 entries (initial content / new content).
        import json as _json

        import faiss as _faiss

        faiss_idx = _faiss.read_index(str(index_dir / "faiss.index"))
        chunk_lines = [
            ln for ln in (index_dir / "chunks.jsonl").read_text().splitlines() if ln.strip()
        ]
        chunks = [_json.loads(ln) for ln in chunk_lines]
        assert faiss_idx.ntotal == 2, f"Double registration: ntotal={faiss_idx.ntotal} (expected 2)"
        assert len(chunks) == 2, f"chunks double registration: {len(chunks)} (expected 2)"
        new_chunks = [c for c in chunks if c.get("content") == "new content"]
        assert len(new_chunks) == 1, (
            f"New record was registered {len(new_chunks)} times (expected 1)"
        )

    async def test_rebuild_memory_index_with_content(
        self, tmp_path: Path, embedder: FakeEmbedder
    ) -> None:
        """rebuild_memory_index correctly rebuilds from the canonical source."""
        jsonl = tmp_path / "project.jsonl"
        index_dir = tmp_path / "idx"

        await append_record(jsonl, "Convention 1: type annotations")
        await append_record(jsonl, "Lesson 2: test early")

        count = await rebuild_memory_index(jsonl, index_dir, embedder, project_id="test-rb")
        assert count == 2

        hits = await search_index_memory(
            index_dir, embedder.embed_batch(["type annotations"])[0], top_k=5, project_id="test-rb"
        )
        assert len(hits) >= 1


# ---------------------------------------------------------------------------
# B1: embed failure tests
# ---------------------------------------------------------------------------


class TestEmbedFailure:
    async def test_embed_failure_does_not_write_orphan(self, tmp_path: Path) -> None:
        """B1: No orphan must remain in the canonical source when embed fails."""

        class FailingEmbedder:
            dim: int = FAKE_DIM

            def embed_batch(self, texts: list[str]) -> list[list[float]]:
                raise RuntimeError("embed intentionally failed")

            async def embed_batch_async(self, texts: list[str]) -> list[list[float]]:
                return await asyncio.to_thread(self.embed_batch, texts)

        store = ProjectMemoryStore(
            jsonl_path=tmp_path / "project.jsonl",
            index_dir=tmp_path / "idx",
            embedder=FailingEmbedder(),
            project_id="test-embed-fail",
            epic_id="ep-1",
        )

        with pytest.raises(EmbedFailedError):
            await store.add("orphan should not appear")

        # Nothing must be written to the canonical source
        jsonl = tmp_path / "project.jsonl"
        assert not jsonl.exists() or parse_records(jsonl.read_text()) == [], (
            "A record was written to the canonical source despite embed failure"
        )

    async def test_remember_tool_embed_failed_reason(self, tmp_path: Path) -> None:
        """B1: remember tool must return embed_failed reason when embed fails."""
        from yukar.agents.orchestrator import _make_remember_tool

        class FailingEmbedder:
            dim: int = FAKE_DIM

            def embed_batch(self, texts: list[str]) -> list[list[float]]:
                raise RuntimeError("embed intentionally failed")

            async def embed_batch_async(self, texts: list[str]) -> list[list[float]]:
                return await asyncio.to_thread(self.embed_batch, texts)

        store = ProjectMemoryStore(
            jsonl_path=tmp_path / "project.jsonl",
            index_dir=tmp_path / "idx",
            embedder=FailingEmbedder(),
            project_id="test-embed-fail-tool",
            epic_id="ep-1",
        )
        remember = _make_remember_tool(store, "ep-1")

        result = await remember._tool_func(fact="failure test", category="fact", repo=None)
        assert result.get("stored") is False
        assert result.get("reason") == "embed_failed"


# ---------------------------------------------------------------------------
# B3: hand-edit of project.jsonl reflected by ensure_index_fresh
# ---------------------------------------------------------------------------


class TestEnsureIndexFresh:
    async def test_ensure_index_fresh_triggers_rebuild_when_jsonl_newer(
        self, tmp_path: Path, embedder: FakeEmbedder
    ) -> None:
        """B3: ensure_index_fresh rebuilds when project.jsonl is newer than the index."""
        jsonl = tmp_path / "project.jsonl"
        index_dir = tmp_path / "idx"

        # First create an empty index
        await rebuild_memory_index(jsonl, index_dir, embedder, project_id="test-fresh")

        # "Hand-edit" project.jsonl by appending content
        await append_record(jsonl, "manually added knowledge", category="fact")

        # Make index appear old (set mtime to the past)
        import os
        import time

        faiss_path = index_dir / "faiss.index"
        old_time = time.time() - 10  # 10 seconds ago
        os.utime(faiss_path, (old_time, old_time))

        store = ProjectMemoryStore(
            jsonl_path=jsonl,
            index_dir=index_dir,
            embedder=embedder,
            project_id="test-fresh",
            epic_id="ep-1",
        )
        await store.ensure_index_fresh()

        # After rebuild, the added entry must be searchable
        entries = await store.search("manually added")
        assert len(entries) >= 1, "Manually added entry not searchable after ensure_index_fresh"
        assert any("manually added" in e.content for e in entries)

    async def test_ensure_index_fresh_no_rebuild_when_index_current(
        self, tmp_path: Path, embedder: FakeEmbedder
    ) -> None:
        """B3: rebuild must not happen when the index is already current
        (assert rebuild not called)."""
        jsonl = tmp_path / "project.jsonl"
        index_dir = tmp_path / "idx"

        await append_record(jsonl, "existing entry", category="fact")
        await rebuild_memory_index(jsonl, index_dir, embedder, project_id="test-fresh2")

        store = ProjectMemoryStore(
            jsonl_path=jsonl,
            index_dir=index_dir,
            embedder=embedder,
            project_id="test-fresh2",
            epic_id="ep-1",
        )

        with patch.object(store, "_rebuild") as mock_rebuild:
            await store.ensure_index_fresh()
            mock_rebuild.assert_not_called()


# ---------------------------------------------------------------------------
# Concurrent add() consistency tests (D3: real assertions)
# ---------------------------------------------------------------------------


class TestConcurrentAdd:
    async def test_concurrent_add_ntotal_consistent(
        self, tmp_path: Path, embedder: FakeEmbedder
    ) -> None:
        """D3: 2 coroutines concurrent add → FAISS ntotal == distinct count and all searchable."""
        store = ProjectMemoryStore(
            jsonl_path=tmp_path / "project.jsonl",
            index_dir=tmp_path / "idx",
            embedder=embedder,
            project_id="test-concurrent",
            epic_id="ep-1",
        )

        results = await asyncio.gather(
            store.add("Content A", metadata={"category": "fact"}),
            store.add("Content B", metadata={"category": "fact"}),
        )
        # D3: concrete assert on success count (no duplicates, so both not None)
        successful = [r for r in results if r is not None]
        assert len(successful) == 2, (
            f"Concurrent add success count differs from expected: {results}"
        )

        # Directly verify FAISS index ntotal (D3: ntotal == distinct count)
        from yukar.indexer.faiss_store import _read_index

        chunks, faiss_idx = await asyncio.to_thread(_read_index, tmp_path / "idx")
        assert faiss_idx.ntotal == 2, f"ntotal differs from expected: {faiss_idx.ntotal}"
        assert faiss_idx.ntotal == len(chunks), f"ntotal={faiss_idx.ntotal} != chunks={len(chunks)}"

        # D3: all searchable (both Content A / Content B returned by search)
        entries_a = await store.search("Content A", options={"max_search_results": 5})
        entries_b = await store.search("Content B", options={"max_search_results": 5})
        contents_a = {e.content for e in entries_a}
        contents_b = {e.content for e in entries_b}
        assert "Content A" in contents_a, "Content A not searchable"
        assert "Content B" in contents_b, "Content B not searchable"

    async def test_concurrent_add_many_ntotal_consistent(
        self, tmp_path: Path, embedder: FakeEmbedder
    ) -> None:
        """D3: 5 coroutines concurrent add → invariant ntotal == distinct count is maintained."""
        store = ProjectMemoryStore(
            jsonl_path=tmp_path / "project.jsonl",
            index_dir=tmp_path / "idx",
            embedder=embedder,
            project_id="test-concurrent-5",
            epic_id="ep-1",
        )

        contents = [f"concurrent test content-{i}" for i in range(5)]
        results = await asyncio.gather(
            *[store.add(c, metadata={"category": "fact"}) for c in contents]
        )
        successful = [r for r in results if r is not None]
        expected_count = len(set(contents))  # no duplicates
        assert len(successful) == expected_count, (
            f"Success count {len(successful)} != expected {expected_count}"
        )

        from yukar.indexer.faiss_store import _read_index

        chunks, faiss_idx = await asyncio.to_thread(_read_index, tmp_path / "idx")
        assert faiss_idx.ntotal == expected_count
        assert faiss_idx.ntotal == len(chunks)


# ---------------------------------------------------------------------------
# native wiring tests: MemoryManager / injection ephemeral / not attached to Worker
# ---------------------------------------------------------------------------


class TestNativeWiring:
    """Validate the wiring of strands native MemoryManager."""

    def test_memory_manager_has_search_tool(self, mem_store: ProjectMemoryStore) -> None:
        """search_memory tool must be registered in MemoryManager."""
        from strands.memory import MemoryManager
        from strands.memory.types import MemoryInjectionConfig

        mm = MemoryManager(
            stores=[mem_store],  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
            injection=MemoryInjectionConfig(trigger="userTurn", max_entries=5),
            search_tool_config=True,
            add_tool_config=False,
        )
        tool_names = [
            t.tool_name if hasattr(t, "tool_name") else getattr(t, "name", str(t)) for t in mm.tools
        ]
        assert any("search_memory" in str(n) for n in tool_names), (
            f"search_memory tool does not exist: {tool_names}"
        )

    def test_memory_manager_injection_trigger_user_turn(
        self, mem_store: ProjectMemoryStore
    ) -> None:
        """injection trigger must be set to userTurn."""
        from strands.memory import MemoryManager
        from strands.memory.types import MemoryInjectionConfig

        cfg = MemoryInjectionConfig(trigger="userTurn", max_entries=5)
        mm = MemoryManager(
            stores=[mem_store],  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
            injection=cfg,
            search_tool_config=True,
            add_tool_config=False,
        )
        # injection config must be stored
        assert mm._injection_config is not False
        resolved = mm._injection_config
        assert resolved.get("trigger") == "userTurn"

    def test_worker_evaluator_no_memory_manager(self) -> None:
        """Verify via import that memory_manager is not passed to Worker / Evaluator.

        Static check that the run_worker / run_evaluator signatures in
        worker.py / evaluator.py do not have a memory_manager argument.
        """
        import inspect

        from yukar.agents import evaluator, worker

        worker_sig = inspect.signature(worker.run_worker)
        evaluator_sig = inspect.signature(evaluator.run_evaluator)

        assert "memory_manager" not in worker_sig.parameters, (
            "memory_manager argument was added to run_worker — invariant violation"
        )
        assert "memory_manager" not in evaluator_sig.parameters, (
            "memory_manager argument was added to run_evaluator — invariant violation"
        )

    def test_worker_evaluator_agents_have_no_memory_tools(self, tmp_path: Path) -> None:
        """Manager-only invariant: directly assert that Worker / Evaluator Agent tool lists
        contain none of the memory tools (remember / search_memory / add_memory).

        Signature inspection (above) alone misses "injection via extra_tools", so build
        Agent instances with the same configuration as run_worker / run_evaluator and
        inspect their tool_names directly.
        Also sanity-check that memory tool names match the forbidden list
        so the check is meaningful.
        """
        from strands import Agent
        from strands.memory import MemoryManager
        from strands.memory.types import MemoryInjectionConfig

        from yukar.agents.context import AgentContext
        from yukar.agents.orchestrator import _make_remember_tool
        from yukar.agents.tools.command import make_command_tools
        from yukar.agents.tools.evaluator_tools import make_evaluator_tools
        from yukar.agents.tools.fs import make_fs_tools
        from yukar.agents.tools.fs_edit import make_fs_edit_tools
        from yukar.agents.tools.git_tools import make_git_tools
        from yukar.indexer.embedder import FakeEmbedder
        from yukar.llm.fake import FakeModel

        forbidden = {"remember", "search_memory", "add_memory"}

        # sanity: memory tools must actually use forbidden names (ensures the check is meaningful).
        store = ProjectMemoryStore(
            jsonl_path=tmp_path / "p.jsonl",
            index_dir=tmp_path / "idx",
            embedder=FakeEmbedder(dim=FAKE_DIM),
            project_id="mgr-only",
            epic_id="ep-1",
        )
        remember_tool = _make_remember_tool(store, "ep-1")
        mm = MemoryManager(
            stores=[store],  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
            injection=MemoryInjectionConfig(trigger="userTurn", max_entries=5),
            search_tool_config=True,
            add_tool_config=False,
        )
        memory_tool_names = {
            str(getattr(remember_tool, "tool_name", getattr(remember_tool, "__name__", "")))
        } | {str(getattr(t, "tool_name", getattr(t, "__name__", ""))) for t in mm.tools}
        assert memory_tool_names & forbidden, (
            f"Memory tools do not use forbidden names (check is meaningless): {memory_tool_names}"
        )

        # Build AgentContext directly (path_guard is auto-generated in __post_init__).
        ctx = AgentContext(
            project_id="mgr-only",
            epic_id="ep-1",
            repo_name="repo1",
            worktree_path=tmp_path,
            workspace_root=str(tmp_path),
        )

        # Same configuration as run_worker (fs / fs_edit / cmd / git + extra_tools=MCP only).
        # Invariant: extra_tools must not contain memory tools. Build without MCP (empty) here.
        worker_tools = [
            *make_fs_tools(ctx),
            *make_fs_edit_tools(ctx),
            *make_command_tools(ctx),
            *make_git_tools(ctx, "name", "a@b"),
        ]
        worker_agent = Agent(model=FakeModel(script=[]), tools=worker_tools)
        assert not (set(worker_agent.tool_names) & forbidden), (
            f"Memory tool mixed into Worker Agent: {worker_agent.tool_names}"
        )

        # Same configuration as run_evaluator (eval_tools + verdict + extra_tools=MCP only).
        eval_tools = make_evaluator_tools(ctx)
        eval_agent = Agent(model=FakeModel(script=[]), tools=eval_tools)
        assert not (set(eval_agent.tool_names) & forbidden), (
            f"Memory tool mixed into Evaluator Agent: {eval_agent.tool_names}"
        )

    def test_injection_ephemeral_not_in_durable_history(
        self, mem_store: ProjectMemoryStore
    ) -> None:
        """D1: injection is ephemeral: verify it is not persisted to durable messages.

        native's _fold_into_last_user_message folds into context.messages (per-call copy)
        and does not modify the agent's durable messages list.
        This test confirms as a seam test that the list returned by injection folding
        is a different object from the original.
        """
        from strands.injection._message_injection import _fold_into_last_user_message

        original: list[Any] = [{"role": "user", "content": [{"text": "hello"}]}]
        folded = _fold_into_last_user_message(original, "<memory>test</memory>")

        # folded is a new list
        assert folded is not original
        # durable messages (original) are unchanged
        assert len(original[0]["content"]) == 1
        assert original[0]["content"][0] == {"text": "hello"}
        # folded contains the injection text
        all_texts = [b.get("text", "") for b in folded[0]["content"]]
        assert any("<memory>" in t for t in all_texts)

    def test_store_description_reflected_in_search_tool(
        self, mem_store: ProjectMemoryStore
    ) -> None:
        """D2: store.description must be reflected in the native search_memory tool description."""
        from strands.memory import MemoryManager
        from strands.memory.types import MemoryInjectionConfig

        mm = MemoryManager(
            stores=[mem_store],  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
            injection=MemoryInjectionConfig(trigger="userTurn", max_entries=5),
            search_tool_config=True,
            add_tool_config=False,
        )

        # store.description must be included in the search_memory tool description
        search_tools = [
            t
            for t in mm.tools
            if "search_memory"
            in str(t.tool_name if hasattr(t, "tool_name") else getattr(t, "name", str(t)))
        ]
        assert search_tools, "search_memory tool not found"
        tool = search_tools[0]

        # Get the tool description
        tool_desc = ""
        if hasattr(tool, "tool_spec"):
            spec = tool.tool_spec
            tool_desc = spec.get("description", "") if isinstance(spec, dict) else str(spec)
        elif hasattr(tool, "description"):
            tool_desc = str(tool.description)
        elif hasattr(tool, "__doc__") and tool.__doc__:
            tool_desc = tool.__doc__

        # Key keywords from store.description must be in tool description
        # (native generates tool description by referencing description)
        assert "project_memory" in tool_desc or mem_store.description[:10] in tool_desc, (
            f"store.description is not reflected in tool description.\n"
            f"store.description={mem_store.description!r}\n"
            f"tool_desc={tool_desc!r}"
        )


# ---------------------------------------------------------------------------
# remember tool
# ---------------------------------------------------------------------------


class TestRememberTool:
    async def test_remember_tool_stores_fact(self, tmp_path: Path, embedder: FakeEmbedder) -> None:
        """remember() tool must write to the store."""
        from yukar.agents.orchestrator import _make_remember_tool

        store = ProjectMemoryStore(
            jsonl_path=tmp_path / "project.jsonl",
            index_dir=tmp_path / "idx",
            embedder=embedder,
            project_id="test-remember",
            epic_id="ep-1",
        )
        remember = _make_remember_tool(store, "ep-1")

        # Invoke the tool directly (Strands tool calls _tool_func directly)
        result = await remember._tool_func(
            fact="Annotate all types",
            category="convention",
            repo="main",
        )
        assert result.get("stored") is True
        assert result.get("id") is not None

        # must be searchable from the store
        entries = await store.search("Annotate all types")
        assert len(entries) >= 1
        assert "Annotate all types" in entries[0].content

    async def test_remember_tool_duplicate_returns_false(
        self, tmp_path: Path, embedder: FakeEmbedder
    ) -> None:
        """Remembering the same fact twice must return stored=False as a duplicate."""
        from yukar.agents.orchestrator import _make_remember_tool

        store = ProjectMemoryStore(
            jsonl_path=tmp_path / "project.jsonl",
            index_dir=tmp_path / "idx",
            embedder=embedder,
            project_id="test-remember-dup",
            epic_id="ep-1",
        )
        remember = _make_remember_tool(store, "ep-1")

        r1 = await remember._tool_func(fact="duplicate test", category="fact", repo=None)
        r2 = await remember._tool_func(fact="duplicate test", category="fact", repo=None)
        assert r1.get("stored") is True
        assert r2.get("stored") is False
        assert r2.get("reason") == "duplicate"


# ---------------------------------------------------------------------------
# lesson learnings (remember is the only write path since P3)
# ---------------------------------------------------------------------------


class TestLessonLearnings:
    async def test_lessons_persist_and_are_searchable(
        self, tmp_path: Path, embedder: FakeEmbedder
    ) -> None:
        """Lesson entries must be written to the store and be searchable."""
        store = ProjectMemoryStore(
            jsonl_path=tmp_path / "project.jsonl",
            index_dir=tmp_path / "idx",
            embedder=embedder,
            project_id="test-complete",
            epic_id="ep-final",
        )

        learnings = [
            "Writing tests first speeds up debugging",
            "Add type hints from the start",
        ]
        for learning in learnings:
            await store.add(
                learning,
                metadata={
                    "category": "lesson",
                    "epic_id": "ep-final",
                    "source": "remember",
                },
            )

        # each learning must be searchable
        for learning in learnings:
            entries = await store.search(learning[:15])  # search with first 15 chars too
            assert len(entries) >= 1, f"'{learning}' not searchable"
            assert any(learning in e.content for e in entries), (
                f"'{learning}' not in search results"
            )

        # must be stored with category=lesson
        records = parse_records((tmp_path / "project.jsonl").read_text())
        lesson_records = [r for r in records if r.category == "lesson"]
        assert len(lesson_records) == 2


# ---------------------------------------------------------------------------
# usage: embed loop injection path
# ---------------------------------------------------------------------------


class TestUsageLoopInjection:
    async def test_embed_loop_injected_on_add(self, tmp_path: Path) -> None:
        """Verify that event loop is injected into embedder during add()."""
        injected_loops: list[Any] = []

        class TrackingEmbedder:
            dim: int = FAKE_DIM
            _fake = FakeEmbedder(dim=FAKE_DIM)

            def set_event_loop(self, loop: Any) -> None:
                injected_loops.append(loop)
                self._fake.set_event_loop(loop)

            def embed_batch(self, texts: list[str]) -> list[list[float]]:
                return self._fake.embed_batch(texts)

            async def embed_batch_async(self, texts: list[str]) -> list[list[float]]:
                return await asyncio.to_thread(self.embed_batch, texts)

        te = TrackingEmbedder()
        store = ProjectMemoryStore(
            jsonl_path=tmp_path / "project.jsonl",
            index_dir=tmp_path / "idx",
            embedder=te,
            project_id="test-usage",
            epic_id="ep-1",
        )

        await store.add("content for usage test")

        # _inject_loop() is called inside add(), which invokes set_event_loop
        assert len(injected_loops) >= 1, "set_event_loop was not called"
        loop = injected_loops[0]
        assert loop is not None
        # loop must be an asyncio.AbstractEventLoop instance
        assert hasattr(loop, "run_until_complete"), f"Injected loop is invalid: {loop!r}"

    async def test_embed_loop_injected_on_search(self, tmp_path: Path) -> None:
        """Event loop must also be injected into embedder during search()."""
        injected_loops: list[Any] = []

        class TrackingEmbedder:
            dim: int = FAKE_DIM
            _fake = FakeEmbedder(dim=FAKE_DIM)

            def set_event_loop(self, loop: Any) -> None:
                injected_loops.append(loop)
                self._fake.set_event_loop(loop)

            def embed_batch(self, texts: list[str]) -> list[list[float]]:
                return self._fake.embed_batch(texts)

            async def embed_batch_async(self, texts: list[str]) -> list[list[float]]:
                return await asyncio.to_thread(self.embed_batch, texts)

        te = TrackingEmbedder()
        store = ProjectMemoryStore(
            jsonl_path=tmp_path / "project.jsonl",
            index_dir=tmp_path / "idx",
            embedder=te,
            project_id="test-usage-search",
            epic_id="ep-1",
        )

        await store.search("query")
        assert len(injected_loops) >= 1, "set_event_loop was not called during search()"


# ---------------------------------------------------------------------------
# Resilience / accuracy regression tests (findings 1-8)
# ---------------------------------------------------------------------------


class _CountingEmbedder:
    """FakeEmbedder wrapper that records the number of embed_batch calls."""

    dim: int = FAKE_DIM

    def __init__(self) -> None:
        self._fake = FakeEmbedder(dim=FAKE_DIM)
        self.calls: list[list[str]] = []

    def set_event_loop(self, loop: Any) -> None:
        self._fake.set_event_loop(loop)

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return self._fake.embed_batch(texts)

    async def embed_batch_async(self, texts: list[str]) -> list[list[float]]:
        return await asyncio.to_thread(self.embed_batch, texts)


class _PoisonEmbedder:
    """FakeEmbedder wrapper that fails embed only for specific content
    (reproduces poison record)."""

    dim: int = FAKE_DIM

    def __init__(self, poison: str) -> None:
        self._fake = FakeEmbedder(dim=FAKE_DIM)
        self._poison = poison

    def set_event_loop(self, loop: Any) -> None:
        self._fake.set_event_loop(loop)

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if any(self._poison in t for t in texts):
            raise RuntimeError(f"embed rejected poison record: {self._poison!r}")
        return self._fake.embed_batch(texts)

    async def embed_batch_async(self, texts: list[str]) -> list[list[float]]:
        return await asyncio.to_thread(self.embed_batch, texts)


class TestTornWriteSelfHeal:
    """finding 1: ensure_index_fresh detects faiss.ntotal != len(chunks.jsonl) and rebuilds."""

    async def test_torn_write_triggers_rebuild(
        self, tmp_path: Path, embedder: FakeEmbedder
    ) -> None:
        jsonl = tmp_path / "project.jsonl"
        index_dir = tmp_path / "idx"

        await append_record(jsonl, "consistent entry 1", category="fact")
        await append_record(jsonl, "consistent entry 2", category="fact")
        await rebuild_memory_index(jsonl, index_dir, embedder, project_id="torn")

        # Reproduce torn write: drop 1 line from chunks.jsonl to make ntotal(2) != len(chunks)(1).
        chunks_path = index_dir / "chunks.jsonl"
        lines = [ln for ln in chunks_path.read_text().splitlines() if ln.strip()]
        assert len(lines) == 2
        chunks_path.write_text(lines[0] + "\n", encoding="utf-8")

        # Do not touch mtime (to ensure mtime-only check cannot detect this,
        # make index appear newer than jsonl).
        import os
        import time

        future = time.time() + 100
        os.utime(index_dir / "faiss.index", (future, future))

        store = ProjectMemoryStore(
            jsonl_path=jsonl,
            index_dir=index_dir,
            embedder=embedder,
            project_id="torn",
            epic_id="ep-1",
        )
        # Index appears newer by mtime, but count inconsistency must be detected and rebuilt.
        await store.ensure_index_fresh()

        from yukar.indexer.faiss_store import _read_index

        chunks, faiss_idx = await asyncio.to_thread(_read_index, index_dir)
        assert faiss_idx.ntotal == 2, f"Not rebuilt; ntotal={faiss_idx.ntotal}"
        assert faiss_idx.ntotal == len(chunks)

    async def test_missing_faiss_with_present_jsonl_rebuilds(
        self, tmp_path: Path, embedder: FakeEmbedder
    ) -> None:
        """finding 1: rebuild also happens when faiss.index is unreadable
        (index_consistency=None)."""
        jsonl = tmp_path / "project.jsonl"
        index_dir = tmp_path / "idx"

        await append_record(jsonl, "entry X", category="fact")
        await rebuild_memory_index(jsonl, index_dir, embedder, project_id="torn2")

        # Corrupt faiss.index (unreadable → index_consistency returns None).
        faiss_path = index_dir / "faiss.index"
        faiss_path.write_bytes(b"not a faiss index")
        # Set mtime to future so mtime-only check would not rebuild.
        import os
        import time

        future = time.time() + 100
        os.utime(faiss_path, (future, future))

        store = ProjectMemoryStore(
            jsonl_path=jsonl,
            index_dir=index_dir,
            embedder=embedder,
            project_id="torn2",
            epic_id="ep-1",
        )
        await store.ensure_index_fresh()

        entries = await store.search("entry X")
        assert any("entry X" in e.content for e in entries), "Corrupt index was not rebuilt"

    async def test_index_write_failure_backdates_mtime(self, tmp_path: Path) -> None:
        """finding 1: when index write during add fails (not dimension mismatch),
        backdate faiss.index mtime to be older than jsonl
        so the next ensure_index_fresh triggers a rebuild."""
        jsonl = tmp_path / "project.jsonl"
        index_dir = tmp_path / "idx"
        embedder = FakeEmbedder(dim=FAKE_DIM)

        # Create an existing healthy index with 1 entry.
        await append_record(jsonl, "existing entry", category="fact")
        await rebuild_memory_index(jsonl, index_dir, embedder, project_id="wf")
        # Set faiss.index mtime to future (to detect backdate effect).
        import os
        import time

        faiss_path = index_dir / "faiss.index"
        future = time.time() + 100
        os.utime(faiss_path, (future, future))

        store = ProjectMemoryStore(
            jsonl_path=jsonl,
            index_dir=index_dir,
            embedder=embedder,
            project_id="wf",
            epic_id="ep-1",
        )

        # Fail only the index append (general exception, not dimension mismatch).
        with (
            patch(
                "yukar.memory.store._sync_add_to_index_unlocked",
                side_effect=RuntimeError("disk full"),
            ),
            pytest.raises(RuntimeError, match="disk full"),
        ):
            await store.add("new entry", metadata={"category": "fact"})

        # Record must already be appended to canonical source (embed→append→index order).
        records = parse_records(jsonl.read_text())
        assert any(r.content == "new entry" for r in records)

        # faiss.index mtime must be older than jsonl → next rebuild is triggered.
        assert faiss_path.stat().st_mtime < jsonl.stat().st_mtime, (
            "mtime not backdated after index write failure"
        )


class TestStripConsistency:
    """finding 2: add and rebuild embed byte-identical text (no whitespace-padding drift)."""

    async def test_add_and_rebuild_produce_same_vector(self, tmp_path: Path) -> None:
        jsonl = tmp_path / "project.jsonl"
        index_dir = tmp_path / "idx"
        embedder = FakeEmbedder(dim=FAKE_DIM)

        # add content with whitespace padding.
        padded = "   knowledge text with whitespace padding   \n"
        store = ProjectMemoryStore(
            jsonl_path=jsonl,
            index_dir=index_dir,
            embedder=embedder,
            project_id="strip",
            epic_id="ep-1",
        )
        rid = await store.add(padded, metadata={"category": "fact"})
        assert rid is not None

        # Record the index vector right after add.
        from yukar.indexer.faiss_store import _read_index

        _chunks, idx_after_add = await asyncio.to_thread(_read_index, index_dir)
        import numpy as np

        vec_add = idx_after_add.reconstruct(0).copy()

        # Full rebuild (re-embed from canonical source).
        await rebuild_memory_index(jsonl, index_dir, embedder, project_id="strip")
        _chunks2, idx_after_rebuild = await asyncio.to_thread(_read_index, index_dir)
        vec_rebuild = idx_after_rebuild.reconstruct(0).copy()

        # Vectors from add and rebuild must be byte-identical (strip consistency).
        assert np.array_equal(vec_add, vec_rebuild), (
            "Embed vector drifted between add and rebuild (strip inconsistency)"
        )

        # Content stored in canonical source must be stripped.
        records = parse_records(jsonl.read_text())
        assert records[0].content == padded.strip()


class TestPreLockDedup:
    """finding 5: duplicate add early-returns before in-lock dedup (without paying for embed)."""

    async def test_duplicate_add_skips_embed(self, tmp_path: Path) -> None:
        counting = _CountingEmbedder()
        store = ProjectMemoryStore(
            jsonl_path=tmp_path / "project.jsonl",
            index_dir=tmp_path / "idx",
            embedder=counting,  # type: ignore[arg-type]
            project_id="prelock",
            epic_id="ep-1",
        )

        r1 = await store.add("duplicate cost check", metadata={"category": "fact"})
        assert r1 is not None
        calls_after_first = len(counting.calls)

        # 2nd call (duplicate): embed_batch count must not increase
        # (early return via pre-lock dedup).
        r2 = await store.add("duplicate cost check", metadata={"category": "fact"})
        assert r2 is None
        assert len(counting.calls) == calls_after_first, (
            "embed was executed on duplicate add (pre-lock dedup not working)"
        )

    async def test_inlock_dedup_still_authoritative(self, tmp_path: Path) -> None:
        """Even when concurrent add passes through pre-lock dedup, in-lock dedup (append_record)
        must ultimately converge duplicates to 1 entry (authoritative dedup not broken)."""
        store = ProjectMemoryStore(
            jsonl_path=tmp_path / "project.jsonl",
            index_dir=tmp_path / "idx",
            embedder=FakeEmbedder(dim=FAKE_DIM),
            project_id="prelock2",
            epic_id="ep-1",
        )

        # Concurrently add same content (pre-lock dedup may see "absent" for both).
        results = await asyncio.gather(
            store.add("concurrent duplicate", metadata={"category": "fact"}),
            store.add("concurrent duplicate", metadata={"category": "fact"}),
        )
        successful = [r for r in results if r is not None]
        assert len(successful) == 1, f"In-lock dedup broken: {len(successful)} entries saved"

        records = parse_records((tmp_path / "project.jsonl").read_text())
        assert len(records) == 1


class TestPoisonRecordIsolation:
    """finding 4: a single embed-rejected record must not permanently fail the entire rebuild."""

    async def test_rebuild_skips_poison_record(self, tmp_path: Path) -> None:
        jsonl = tmp_path / "project.jsonl"
        index_dir = tmp_path / "idx"

        # Write 2 normal records + 1 poison to canonical source.
        await append_record(jsonl, "normal record A", category="fact")
        await append_record(jsonl, "POISON-large record", category="fact")
        await append_record(jsonl, "normal record B", category="fact")

        poison_emb = _PoisonEmbedder(poison="POISON")
        count = await rebuild_memory_index(
            jsonl, index_dir, poison_emb, project_id="poison"  # type: ignore[arg-type]
        )

        # 1 poison record is skipped; only 2 normal records are indexed. rebuild does not fail.
        assert count == 2, f"Poison record isolation not working: count={count}"

        from yukar.indexer.faiss_store import _read_index

        chunks, faiss_idx = await asyncio.to_thread(_read_index, index_dir)
        assert faiss_idx.ntotal == 2
        assert faiss_idx.ntotal == len(chunks)
        indexed = {c.get("content") for c in chunks}
        assert "normal record A" in indexed
        assert "normal record B" in indexed
        assert "POISON-large record" not in indexed

        # Poison record remains in canonical source (jsonl); only dropped from index.
        records = parse_records(jsonl.read_text())
        assert any("POISON" in r.content for r in records), (
            "Poison record was removed from canonical source"
        )

    async def test_dimension_mismatch_rebuild_skips_poison(self, tmp_path: Path) -> None:
        """finding 4: _rebuild_under_lock on dimension mismatch must also skip poison records
        and index other records along with the new record."""
        jsonl = tmp_path / "project.jsonl"
        index_dir = tmp_path / "idx"

        # Index normal record + poison record at dim=64 (embed via healthy FakeEmbedder).
        emb64 = FakeEmbedder(dim=64)
        await append_record(jsonl, "existing normal", category="fact")
        await append_record(jsonl, "POISON-existing", category="fact")
        recs = [
            {"record_id": "mem-0001", "content": "existing normal", "category": "fact"},
            {"record_id": "mem-0002", "content": "POISON-existing", "category": "fact"},
        ]
        vecs = emb64.embed_batch(["existing normal", "POISON-existing"])
        await rebuild_index(index_dir, recs, vecs, project_id="poison-dim")

        # add with dim=128 + poison-rejecting embedder → dimension mismatch → rebuild_under_lock.
        poison128 = _PoisonEmbedder(poison="POISON")
        poison128._fake = FakeEmbedder(dim=128)
        store = ProjectMemoryStore(
            jsonl_path=jsonl,
            index_dir=index_dir,
            embedder=poison128,  # type: ignore[arg-type]
            project_id="poison-dim",
            epic_id="ep-1",
        )
        result = await store.add("new normal", metadata={"category": "fact"})
        assert result is not None, "add failed with dimension mismatch + poison record"

        from yukar.indexer.faiss_store import _read_index

        chunks, faiss_idx = await asyncio.to_thread(_read_index, index_dir)
        indexed = {c.get("content") for c in chunks}
        # existing normal + new normal are indexed; poison is dropped.
        assert "existing normal" in indexed
        assert "new normal" in indexed
        assert "POISON-existing" not in indexed
        assert faiss_idx.ntotal == len(chunks)


class TestEmptyContentNotIndexed:
    """finding 8: hand-edited records with empty/whitespace-only content are excluded
    by parse_records and not indexed."""

    def test_parse_drops_empty_content(self, tmp_path: Path) -> None:
        import json

        jsonl = tmp_path / "project.jsonl"
        good = json.dumps({"id": "mem-0001", "content": "has content"}, ensure_ascii=False)
        empty = json.dumps({"id": "mem-0002", "content": "   "}, ensure_ascii=False)
        empty2 = json.dumps({"id": "mem-0003", "content": ""}, ensure_ascii=False)
        jsonl.write_text(good + "\n" + empty + "\n" + empty2 + "\n", encoding="utf-8")

        records = parse_records(jsonl.read_text())
        assert len(records) == 1, f"Empty content not skipped: {[r.id for r in records]}"
        assert records[0].content == "has content"

    async def test_rebuild_does_not_index_empty_content(
        self, tmp_path: Path, embedder: FakeEmbedder
    ) -> None:
        """Even when empty content lines are mixed in via hand-editing,
        rebuild must not index zero vectors."""
        import json

        jsonl = tmp_path / "project.jsonl"
        index_dir = tmp_path / "idx"
        good = json.dumps(
            {"id": "mem-0001", "content": "substantive knowledge"}, ensure_ascii=False
        )
        empty = json.dumps({"id": "mem-0002", "content": "   "}, ensure_ascii=False)
        jsonl.write_text(good + "\n" + empty + "\n", encoding="utf-8")

        count = await rebuild_memory_index(jsonl, index_dir, embedder, project_id="empty")
        assert count == 1, "Empty content was indexed"

        from yukar.indexer.faiss_store import _read_index

        chunks, faiss_idx = await asyncio.to_thread(_read_index, index_dir)
        assert faiss_idx.ntotal == 1
        assert len(chunks) == 1
        assert chunks[0].get("content") == "substantive knowledge"


class TestSearchHonorsMaxResults:
    """finding 7: search() must respect self.max_search_results (not hardcoded to 5)."""

    async def test_search_uses_instance_max_search_results(self, tmp_path: Path) -> None:
        embedder = FakeEmbedder(dim=FAKE_DIM)
        store = ProjectMemoryStore(
            jsonl_path=tmp_path / "project.jsonl",
            index_dir=tmp_path / "idx",
            embedder=embedder,
            project_id="maxres",
            epic_id="ep-1",
        )
        # Lower max_search_results to 2.
        store.max_search_results = 2  # type: ignore[misc]

        for i in range(6):
            await store.add(f"search-limit test item-{i}", metadata={"category": "fact"})

        # No options specified → instance max_search_results(2) must be used.
        entries = await store.search("search-limit test")
        assert len(entries) <= 2, (
            f"search ignored max_search_results: {len(entries)} results (limit 2)"
        )

    async def test_search_options_override_instance_default(self, tmp_path: Path) -> None:
        """max_search_results in options must take priority over the instance default."""
        embedder = FakeEmbedder(dim=FAKE_DIM)
        store = ProjectMemoryStore(
            jsonl_path=tmp_path / "project.jsonl",
            index_dir=tmp_path / "idx",
            embedder=embedder,
            project_id="maxres2",
            epic_id="ep-1",
        )
        store.max_search_results = 2  # type: ignore[misc]

        for i in range(6):
            await store.add(f"override-test item-{i}", metadata={"category": "fact"})

        entries = await store.search("override-test", options={"max_search_results": 4})
        assert len(entries) <= 4
        # Verify more results are returned than instance default(2) (options wins).
        assert len(entries) > 2, "options max_search_results was overridden by instance default"


class TestRebuildVsAddUnderLock:
    """finding 3: rebuild holds the lock across read+embed+write
    and does not drop concurrent adds."""

    async def test_rebuild_holds_lock_across_read_embed_write(self, tmp_path: Path) -> None:
        """rebuild_memory_index must acquire the project lock before read and hold it through write.

        Concurrent add must be blocked waiting for the lock during embed,
        and must be serialized after rebuild completes.
        Ultimately both records must be reflected in the index.
        """
        jsonl = tmp_path / "project.jsonl"
        index_dir = tmp_path / "idx"
        embedder = FakeEmbedder(dim=FAKE_DIM)

        # Prepare 1 record in canonical source and build index.
        await append_record(jsonl, "rebuild target 1", category="fact")
        await rebuild_memory_index(jsonl, index_dir, embedder, project_id="rb-lock")

        store = ProjectMemoryStore(
            jsonl_path=jsonl,
            index_dir=index_dir,
            embedder=embedder,
            project_id="rb-lock",
            epic_id="ep-1",
        )

        order: list[str] = []

        async def run_rebuild() -> int:
            order.append("rebuild_start")
            res = await rebuild_memory_index(jsonl, index_dir, embedder, project_id="rb-lock")
            order.append("rebuild_end")
            return res

        async def concurrent_add() -> Any:
            # Attempt add after rebuild has definitely acquired the lock.
            await asyncio.sleep(0.01)
            order.append("add_attempt")
            r = await store.add("concurrent add record", metadata={"category": "fact"})
            order.append("add_done")
            return r

        rebuild_res, add_res = await asyncio.gather(run_rebuild(), concurrent_add())
        assert rebuild_res >= 1
        assert add_res is not None

        # add must complete after rebuild releases the lock (rebuild_end before add_done).
        # Since rebuild holds the lock across read+embed+write, add cannot interleave (finding 3).
        assert order.index("rebuild_end") < order.index("add_done"), (
            f"add interleaved while rebuild held the lock: {order}"
        )

        # Both rebuild target + concurrent add record must be in the index (no drops).
        entries_old = await store.search("rebuild target 1")
        entries_new = await store.search("concurrent add record")
        assert any("rebuild target 1" in e.content for e in entries_old)
        assert any("concurrent add record" in e.content for e in entries_new)


# ---------------------------------------------------------------------------
# C1: project_id / run_id must be passed to create_embedder
# ---------------------------------------------------------------------------


class TestCreateEmbedderProjectAttribution:
    def test_create_embedder_passes_project_id_to_fake(self) -> None:
        """C1: create_embedder(fake, project_id=..., run_id=...)
        must pass values to FakeEmbedder."""
        from yukar.config.settings import EmbeddingSettings
        from yukar.indexer.embedder import FakeEmbedder, create_embedder

        settings = EmbeddingSettings(provider="fake")
        embedder = create_embedder(settings, project_id="my-project", run_id="run-123")
        assert isinstance(embedder, FakeEmbedder)
        assert embedder._project_id == "my-project"
        assert embedder._run_id == "run-123"

    def test_create_embedder_default_empty_project_id(self) -> None:
        """C1: when project_id is omitted, empty string is the default (backward compatible)."""
        from yukar.config.settings import EmbeddingSettings
        from yukar.indexer.embedder import FakeEmbedder, create_embedder

        settings = EmbeddingSettings(provider="fake")
        embedder = create_embedder(settings)
        assert isinstance(embedder, FakeEmbedder)
        assert embedder._project_id == ""
        assert embedder._run_id is None
