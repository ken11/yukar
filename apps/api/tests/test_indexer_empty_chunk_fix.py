"""Tests for empty-chunk filter fix in IndexerService and BedrockTitanEmbedder.

Root cause: empty files (e.g. bare ``__init__.py``) produce Chunk(text="") from
the line-based splitter.  Bedrock Titan v2 rejects empty ``inputText`` with a
ValidationException, which aborts the entire reindex run before any index is
saved.  This means repos with even one empty file never get an index built.

Fixes applied:
1. ``indexer/service.py`` ``_do_reindex``: filter ``all_chunks`` to remove
   chunks whose ``text.strip()`` is empty, before either the full-rebuild or
   incremental embed paths.  Chunks↔vectors counts stay aligned.
2. ``indexer/embedder.py`` ``BedrockTitanEmbedder.embed_batch``: return a zero
   vector and skip the Bedrock API call for any empty/whitespace-only text,
   so the embedder itself is resilient even if filtering is bypassed.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from tests._helpers import make_git_repo


class _ExplodingOnEmptyEmbedder:
    """Test embedder that raises ValueError for any empty/whitespace-only text.

    This simulates Bedrock Titan v2 ValidationException behaviour so that tests
    can verify the service filter prevents the exception from propagating.
    Texts with actual content are embedded via a simple deterministic hash
    (same approach as FakeEmbedder but without the usage-recording overhead).
    """

    def __init__(self, dim: int = 32) -> None:
        self._dim = dim
        self.empty_calls: list[str] = []  # records any empty text that leaked through

    @property
    def dim(self) -> int:
        return self._dim

    def set_event_loop(self, loop: Any) -> None:  # noqa: ANN401 — duck-type protocol
        pass

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        import hashlib

        import numpy as np

        results: list[list[float]] = []
        for text in texts:
            if not text.strip():
                self.empty_calls.append(text)
                raise ValueError(
                    "ValidationException: 1 validation error detected: "
                    "Value at 'inputText' failed to satisfy constraint: "
                    "Member must have length greater than or equal to 1"
                )
            digest = hashlib.sha256(text.encode()).digest()
            raw = np.frombuffer(digest * ((self._dim // 32) + 2), dtype=np.uint8)[: self._dim]
            vec = raw.astype(np.float32) / 255.0
            norm = float(np.linalg.norm(vec))
            if norm > 0:
                vec = vec / norm
            results.append(vec.tolist())
        return results

    async def embed_batch_async(self, texts: list[str]) -> list[list[float]]:
        import asyncio

        return await asyncio.to_thread(self.embed_batch, texts)


# ===========================================================================
# service._do_reindex — empty-chunk filter
# ===========================================================================


class TestEmptyChunkFilter:
    """``_do_reindex`` must exclude empty/whitespace chunks before embedding."""

    def _make_service(self, workspace: Path, embedder: Any) -> Any:
        from yukar.indexer.service import IndexerService

        return IndexerService(workspace_root=str(workspace), embedder=embedder)

    async def test_repo_with_empty_init_py_completes(self, tmp_path: Path) -> None:
        """Repo containing an empty ``__init__.py`` must reindex without error.

        This is the exact production failure scenario: the empty file produces a
        Chunk(text="") that the old code sent to Bedrock, which raised
        ValidationException and aborted the build.
        """
        workspace = tmp_path / "ws"
        workspace.mkdir()

        repo = make_git_repo(tmp_path, "pkg-repo")
        pkg = repo / "pkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")  # empty — the trigger
        (pkg / "mod.py").write_text("def greet(name: str) -> str:\n    return f'Hello {name}'\n")

        embedder = _ExplodingOnEmptyEmbedder()
        service = self._make_service(workspace, embedder)

        # Must not raise — empty chunk should be filtered before embed_batch.
        n_chunks = await service.reindex_repo("proj", "pkg-repo", repo)

        # Index must exist and contain only non-empty chunks.
        from yukar.config import paths as config_paths
        from yukar.indexer import faiss_store

        idx_dir = config_paths.index_dir(str(workspace), "proj", "pkg-repo")
        assert faiss_store.index_exists(idx_dir), "Index must exist after successful reindex"

        saved_chunks, _ = await faiss_store.load_index(idx_dir)
        assert len(saved_chunks) == n_chunks, "Returned chunk count must match saved chunk count"
        # Verify no empty chunk slipped into the saved index.
        for c in saved_chunks:
            assert c["text"].strip(), f"Empty chunk found in index: {c!r}"
        # The embedder must never have been called with an empty string.
        assert not embedder.empty_calls, (
            f"Empty text reached the embedder: {embedder.empty_calls!r}"
        )

    async def test_repo_with_multiple_empty_files_completes(self, tmp_path: Path) -> None:
        """Multiple empty files (as in the yukar repo itself) must all be filtered."""
        workspace = tmp_path / "ws"
        workspace.mkdir()

        repo = make_git_repo(tmp_path, "multi-empty-repo")
        # Simulate several empty __init__.py files spread across the tree.
        for subdir in ("a", "b", "c", "a/sub"):
            (repo / subdir).mkdir(parents=True, exist_ok=True)
            (repo / subdir / "__init__.py").write_text("")
        # Also a whitespace-only file (newline only).
        (repo / "a" / "blank.py").write_text("   \n\n   \n")
        # One file with real content.
        (repo / "a" / "real.py").write_text("x = 42\n")

        embedder = _ExplodingOnEmptyEmbedder()
        service = self._make_service(workspace, embedder)

        n_chunks = await service.reindex_repo("proj", "multi-empty-repo", repo)

        from yukar.config import paths as config_paths
        from yukar.indexer import faiss_store

        idx_dir = config_paths.index_dir(str(workspace), "proj", "multi-empty-repo")
        assert faiss_store.index_exists(idx_dir)
        saved_chunks, _ = await faiss_store.load_index(idx_dir)
        assert len(saved_chunks) == n_chunks

        for c in saved_chunks:
            assert c["text"].strip(), f"Empty/whitespace chunk found in index: {c!r}"
        assert not embedder.empty_calls

    async def test_all_empty_files_produces_empty_index(self, tmp_path: Path) -> None:
        """A repo containing *only* empty files produces a valid index with 0 chunks from them.

        ``make_git_repo`` also creates a README.md with content, so the overall
        chunk count may be > 0 due to that file.  This test isolates the empty-file
        filtering by using a fresh tmp_path directory (not the git helper) and
        directly calls ``_do_reindex`` via ``reindex_repo``.  The key assertions
        are: no exception is raised, the index file exists, and no empty chunk is
        in the saved index.
        """
        import os

        workspace = tmp_path / "ws"
        workspace.mkdir()

        # Build a bare directory with only empty Python files (no README).
        # We do NOT use make_git_repo here to avoid the README.md content.
        repo = tmp_path / "bare-empty-repo"
        repo.mkdir()
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
        (repo / "__init__.py").write_text("")
        (repo / "empty.py").write_text("")
        g("add", ".")
        g("commit", "-m", "initial")

        embedder = _ExplodingOnEmptyEmbedder()
        service = self._make_service(workspace, embedder)

        # Must not raise.
        n_chunks = await service.reindex_repo("proj", "bare-empty-repo", repo)
        assert n_chunks == 0

        from yukar.config import paths as config_paths
        from yukar.indexer import faiss_store

        idx_dir = config_paths.index_dir(str(workspace), "proj", "bare-empty-repo")
        assert faiss_store.index_exists(idx_dir), "Empty index must still be saved"
        assert not embedder.empty_calls

    async def test_chunk_count_and_vector_count_aligned(self, tmp_path: Path) -> None:
        """Saved chunks count must equal vectors count (FAISS ntotal) after filtering."""
        from yukar.indexer.embedder import FakeEmbedder

        workspace = tmp_path / "ws"
        workspace.mkdir()

        repo = make_git_repo(tmp_path, "align-repo")
        pkg = repo / "mypkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")  # empty
        (pkg / "utils.py").write_text("def add(a, b):\n    return a + b\n")
        (pkg / "models.py").write_text(
            "class Point:\n    def __init__(self, x, y):\n        self.x = x\n        self.y = y\n"
        )

        service = self._make_service(workspace, FakeEmbedder())
        await service.reindex_repo("proj", "align-repo", repo)

        from yukar.config import paths as config_paths
        from yukar.indexer import faiss_store

        idx_dir = config_paths.index_dir(str(workspace), "proj", "align-repo")
        saved_chunks, faiss_idx = await faiss_store.load_index(idx_dir)

        # Core invariant: chunk list length == FAISS vector count.
        assert len(saved_chunks) == faiss_idx.ntotal, (
            f"Chunk count ({len(saved_chunks)}) != FAISS ntotal ({faiss_idx.ntotal})"
        )
        # No empty chunks in the saved index.
        for c in saved_chunks:
            assert c["text"].strip()

    async def test_incremental_reindex_also_filters_empty_chunks(self, tmp_path: Path) -> None:
        """Incremental reindex must not send empty changed_chunks to the embedder."""
        workspace = tmp_path / "ws"
        workspace.mkdir()

        repo = make_git_repo(tmp_path, "inc-empty-repo")
        pkg = repo / "pkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "app.py").write_text("def run():\n    pass\n")

        embedder = _ExplodingOnEmptyEmbedder()
        service = self._make_service(workspace, embedder)

        # Full build first.
        await service.reindex_repo("proj", "inc-empty-repo", repo, full=True)
        embedder.empty_calls.clear()

        # Touch __init__.py (still empty) so mtime changes → it becomes a "changed" file.
        import time

        time.sleep(0.01)
        (pkg / "__init__.py").write_text("")

        # Incremental — empty changed chunk must be filtered.
        await service.reindex_repo("proj", "inc-empty-repo", repo, full=False)
        assert not embedder.empty_calls, (
            "Empty changed chunk reached the embedder in incremental path"
        )


# ===========================================================================
# BedrockTitanEmbedder — empty-input guard
# ===========================================================================


class TestBedrockEmbedderEmptyGuard:
    """``BedrockTitanEmbedder.embed_batch`` must not call Bedrock for empty inputs."""

    def _make_embedder(self) -> Any:
        from yukar.indexer.embedder import BedrockTitanEmbedder

        return BedrockTitanEmbedder(model_id="amazon.titan-embed-text-v2:0")

    def test_empty_string_returns_zero_vector_without_boto3(self, tmp_path: Path) -> None:
        """Passing an empty string must return a zero vector, not call boto3."""
        from unittest.mock import MagicMock, patch

        emb = self._make_embedder()
        # Patch _get_client so any accidental boto3 call is detectable.
        mock_client = MagicMock()
        with patch.object(emb, "_get_client", return_value=mock_client):
            result = emb.embed_batch([""])

        assert result == [[0.0] * emb.dim], f"Expected zero vector, got {result}"
        mock_client.invoke_model.assert_not_called()

    def test_whitespace_only_string_returns_zero_vector(self, tmp_path: Path) -> None:
        from unittest.mock import MagicMock, patch

        emb = self._make_embedder()
        mock_client = MagicMock()
        with patch.object(emb, "_get_client", return_value=mock_client):
            result = emb.embed_batch(["   \n\t  "])

        assert result == [[0.0] * emb.dim]
        mock_client.invoke_model.assert_not_called()

    def test_non_empty_text_still_calls_bedrock(self) -> None:
        """Non-empty text must still go through the normal Bedrock API path."""
        import json
        from unittest.mock import MagicMock, patch

        emb = self._make_embedder()
        fake_vec = [0.1] * emb.dim
        mock_response_body = MagicMock()
        mock_response_body.read.return_value = json.dumps(
            {"embedding": fake_vec, "inputTextTokenCount": 5}
        ).encode()
        mock_client = MagicMock()
        mock_client.invoke_model.return_value = {"body": mock_response_body}

        with patch.object(emb, "_get_client", return_value=mock_client):
            result = emb.embed_batch(["hello world"])

        assert result == [fake_vec]
        mock_client.invoke_model.assert_called_once()

    def test_mixed_batch_empty_and_nonempty(self) -> None:
        """In a batch with both empty and non-empty texts, empty → zero vector, real → Bedrock."""
        import json
        from unittest.mock import MagicMock, patch

        emb = self._make_embedder()
        fake_vec = [0.5] * emb.dim
        mock_response_body = MagicMock()
        mock_response_body.read.return_value = json.dumps(
            {"embedding": fake_vec, "inputTextTokenCount": 3}
        ).encode()
        mock_client = MagicMock()
        mock_client.invoke_model.return_value = {"body": mock_response_body}

        texts = ["", "hello", "  \n  "]
        with patch.object(emb, "_get_client", return_value=mock_client):
            result = emb.embed_batch(texts)

        assert len(result) == 3
        # Index 0 and 2 are empty/whitespace → zero vectors.
        assert result[0] == [0.0] * emb.dim, "Empty text must yield zero vector"
        assert result[2] == [0.0] * emb.dim, "Whitespace-only text must yield zero vector"
        # Index 1 is "hello" → Bedrock result.
        assert result[1] == fake_vec
        # Bedrock was called exactly once (for "hello" only).
        assert mock_client.invoke_model.call_count == 1

    def test_zero_vector_has_correct_dimension(self) -> None:
        """Zero vector returned for empty text must have exactly ``dim`` elements."""
        from unittest.mock import MagicMock, patch

        for dims in (512, 1024):
            from yukar.indexer.embedder import BedrockTitanEmbedder

            emb = BedrockTitanEmbedder(dimensions=dims)
            mock_client = MagicMock()
            with patch.object(emb, "_get_client", return_value=mock_client):
                result = emb.embed_batch([""])
            assert len(result[0]) == dims, f"Expected dim={dims}, got {len(result[0])}"
