"""Tests for indexer degrade fixes.

Covers:
- fix #1: incremental reindex — only changed files are re-embedded
- fix #2: directory hierarchy in _build_tree
- fix #3: chunk size / overlap in splitter
- fix #5+#7: EmbeddingSettings.region / .dimensions fields
- fix #6: NUL-byte binary heuristic in _collect_files
- fix #8: 1-indexed line numbers in repo_tools
- fix #9: fs_edit tools (normal / error / sandbox escape)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

from tests._helpers import make_git_repo

# ===========================================================================
# fix #1: Incremental reindex
# ===========================================================================


class TestIncrementalReindex:
    """Verify that incremental reindex only re-embeds changed files."""

    def _make_service(self, workspace: Path) -> tuple[Any, Any]:
        """Return (service, call_tracking_embedder) with embed_batch call counting."""
        from yukar.indexer.service import IndexerService

        class TrackingEmbedder:
            """Records every embed_batch call text list for inspection."""

            def __init__(self, dim: int = 32) -> None:
                self._dim = dim
                self.calls: list[list[str]] = []
                import hashlib

                import numpy as np

                self._hashlib = hashlib
                self._np = np

            @property
            def dim(self) -> int:
                return self._dim

            def embed_batch(self, texts: list[str]) -> list[list[float]]:
                self.calls.append(list(texts))
                import hashlib

                import numpy as np

                results = []
                for text in texts:
                    digest = hashlib.sha256(text.encode()).digest()
                    raw = np.frombuffer(digest * ((self._dim // 32) + 2), dtype=np.uint8)[
                        : self._dim
                    ]
                    vec = raw.astype(np.float32) / 255.0
                    norm = np.linalg.norm(vec)
                    if norm > 0:
                        vec = vec / norm
                    results.append(vec.tolist())
                return results

            async def embed_batch_async(self, texts: list[str]) -> list[list[float]]:
                import asyncio

                return await asyncio.to_thread(self.embed_batch, texts)

        embedder = TrackingEmbedder()
        service = IndexerService(workspace_root=str(workspace), embedder=embedder)
        return service, embedder

    async def test_incremental_only_reembeds_changed_file(self, tmp_path: Path) -> None:
        """After full index, changing one file triggers re-embed only for that file."""
        workspace = tmp_path / "ws"
        workspace.mkdir()

        repo = make_git_repo(tmp_path, "inc-repo")
        (repo / "a.py").write_text("def alpha():\n    return 1\n")
        (repo / "b.py").write_text("def beta():\n    return 2\n")

        service, embedder = self._make_service(workspace)

        # Full build
        await service.reindex_repo("proj", "inc-repo", repo, full=True)
        full_calls = len(embedder.calls)
        assert full_calls >= 1

        # Modify only b.py (touch mtime).
        import time

        time.sleep(0.01)
        (repo / "b.py").write_text("def beta():\n    return 99  # changed\n")

        embedder.calls.clear()

        # Incremental update
        await service.reindex_repo("proj", "inc-repo", repo, full=False)

        # All embedded texts in the incremental run should come from b.py only.
        embedded_texts = [t for batch in embedder.calls for t in batch]
        assert embedded_texts, "Expected at least one re-embed for changed file"
        # None of the embedded texts should contain "alpha" (a.py was not changed).
        assert not any("alpha" in t for t in embedded_texts), (
            "a.py was re-embedded even though its mtime did not change"
        )
        # At least one embedded text should contain "99" or "changed" (from b.py).
        assert any("99" in t or "changed" in t for t in embedded_texts), (
            "b.py change was not reflected in re-embedded chunks"
        )

    async def test_incremental_unchanged_files_not_reembedded(self, tmp_path: Path) -> None:
        """After full index with no changes, incremental run embeds nothing new."""
        workspace = tmp_path / "ws"
        workspace.mkdir()

        repo = make_git_repo(tmp_path, "no-change-repo")
        (repo / "stable.py").write_text("x = 1\n")

        service, embedder = self._make_service(workspace)
        await service.reindex_repo("proj", "no-change-repo", repo, full=True)
        embedder.calls.clear()

        # Run incremental with no file changes.
        await service.reindex_repo("proj", "no-change-repo", repo, full=False)

        # Nothing should be re-embedded.
        assert not any(embedder.calls), (
            "Expected zero re-embeds when no files changed, "
            f"got {sum(len(b) for b in embedder.calls)} texts"
        )

    async def test_watcher_uses_incremental_by_default(self, tmp_path: Path) -> None:
        """RepoWatcher._do_reindex calls reindex_repo(full=False)."""
        from yukar.indexer.watcher import RepoWatcher
        from yukar.sandbox.ignore import IgnoreRules

        repo = make_git_repo(tmp_path, "watch-inc")
        service_mock = AsyncMock()
        service_mock.reindex_repo = AsyncMock(return_value=3)

        watcher = RepoWatcher(service_mock, debounce=0.0)
        ignore_rules = IgnoreRules.from_repo(repo)
        watcher.add_repo("proj", "watch-inc", repo, ignore_rules=ignore_rules)

        watched = watcher._repos[("proj", "watch-inc")]
        await watcher._do_reindex(watched)

        service_mock.reindex_repo.assert_called_once_with("proj", "watch-inc", repo, full=False)


# ===========================================================================
# fix #2: Directory tree hierarchy
# ===========================================================================


class TestDirectoryTree:
    """_build_tree must emit directory-level entries with correct indentation."""

    def _build(self, repo: Path) -> list[str]:
        from yukar.indexer.summarizer import _build_tree, _walk_files
        from yukar.sandbox.ignore import IgnoreRules

        ignore = IgnoreRules.from_repo(repo)
        files = _walk_files(repo, ignore)
        return _build_tree(repo, files)

    def test_directory_entry_appears_in_tree(self, tmp_path: Path) -> None:
        repo = make_git_repo(tmp_path, "tree-repo")
        src = repo / "src"
        src.mkdir()
        (src / "utils.py").write_text("x = 1\n")
        (repo / "README.md").write_text("# r\n")

        lines = self._build(repo)
        # "src/" directory line should appear before its files.
        assert any(line.rstrip().endswith("src/") for line in lines), (
            "Expected 'src/' in tree, got:\n" + "\n".join(lines)
        )

    def test_nested_directory_indented(self, tmp_path: Path) -> None:
        repo = make_git_repo(tmp_path, "tree-nested")
        (repo / "a" / "b").mkdir(parents=True)
        (repo / "a" / "b" / "deep.py").write_text("y = 2\n")

        lines = self._build(repo)
        # "a/" should appear at indent 0, "b/" at indent 1 (2 spaces).
        a_line = next((ln for ln in lines if ln.rstrip().endswith("a/")), None)
        b_line = next((ln for ln in lines if ln.rstrip().endswith("b/")), None)
        assert a_line is not None, "Expected 'a/' in tree"
        assert b_line is not None, "Expected 'b/' in tree"
        assert b_line.startswith("  "), f"'b/' should be indented under 'a/', got: {b_line!r}"

    def test_truncation_shows_remaining_count(self, tmp_path: Path) -> None:
        repo = make_git_repo(tmp_path, "tree-trunc")
        for i in range(50):
            (repo / f"file_{i:02d}.py").write_text(f"x = {i}\n")

        from yukar.indexer.summarizer import _build_tree, _walk_files
        from yukar.sandbox.ignore import IgnoreRules

        ignore = IgnoreRules.from_repo(repo)
        files = _walk_files(repo, ignore)
        lines = _build_tree(repo, files, max_lines=10)

        assert len(lines) > 0
        # Last line should be a truncation message containing "more entries".
        assert "more entries" in lines[-1], f"Expected truncation message, got: {lines[-1]!r}"


# ===========================================================================
# fix #3: Chunk size and overlap
# ===========================================================================


class TestChunkSizeAndOverlap:
    def test_default_max_chunk_chars_is_3000(self) -> None:
        from yukar.indexer.splitter import MAX_CHUNK_CHARS

        assert MAX_CHUNK_CHARS == 3000

    def test_overlap_produces_shared_lines(self) -> None:
        """Consecutive _line_split chunks must share approximately CHUNK_OVERLAP_CHARS text."""
        from yukar.indexer.splitter import _line_split

        # Create text large enough to produce 2+ chunks.
        lines = [f"line_{i:04d} = 'x' * 10\n" for i in range(500)]
        text = "".join(lines)

        chunks = _line_split(text, repo="r", path="f.py", max_chars=3000)
        assert len(chunks) >= 2, "Expected at least two chunks"

        # The tail of chunk 0 and the head of chunk 1 must share some text.
        text0 = chunks[0]["text"]
        text1 = chunks[1]["text"]
        # Find the longest common prefix between tail of text0 and head of text1.
        # At minimum one full line should overlap.
        overlap_found = any(line in text1 for line in text0.splitlines()[-5:])
        assert overlap_found, (
            "Expected overlapping lines between consecutive chunks, "
            f"tail of chunk 0:\n{text0[-200:]!r}\nhead of chunk 1:\n{text1[:200]!r}"
        )

    def test_mtime_stored_in_chunk(self) -> None:
        """mtime passed to split_file must be present in every output chunk."""
        from yukar.indexer.splitter import split_file

        chunks = split_file("x = 1\n", repo="r", path="f.py", mtime=1234567.89)
        assert all(c["mtime"] == 1234567.89 for c in chunks), "mtime not propagated into chunks"

    def test_chunk_mtime_defaults_to_zero(self) -> None:
        from yukar.indexer.splitter import split_file

        chunks = split_file("x = 1\n", repo="r", path="f.py")
        assert all(c["mtime"] == 0.0 for c in chunks)


# ===========================================================================
# fix #5+#7: EmbeddingSettings region / dimensions
# ===========================================================================


class TestEmbeddingSettings:
    def test_region_defaults_to_none(self) -> None:
        from yukar.config.settings import EmbeddingSettings

        s = EmbeddingSettings()
        assert s.region is None

    def test_region_can_be_set(self) -> None:
        from yukar.config.settings import EmbeddingSettings

        s = EmbeddingSettings(region="ap-northeast-1")
        assert s.region == "ap-northeast-1"

    def test_dimensions_defaults_to_none(self) -> None:
        from yukar.config.settings import EmbeddingSettings

        s = EmbeddingSettings()
        assert s.dimensions is None

    def test_dimensions_can_be_set(self) -> None:
        from yukar.config.settings import EmbeddingSettings

        s = EmbeddingSettings(dimensions=512)
        assert s.dimensions == 512

    def test_create_embedder_passes_region_and_dimensions(self) -> None:
        """create_embedder must forward region and dimensions to BedrockTitanEmbedder."""
        from yukar.config.settings import EmbeddingSettings
        from yukar.indexer.embedder import BedrockTitanEmbedder, create_embedder

        s = EmbeddingSettings(provider="bedrock", region="us-west-2", dimensions=256)
        emb = create_embedder(s)
        assert isinstance(emb, BedrockTitanEmbedder)
        assert emb._region == "us-west-2"
        assert emb._dimensions == 256

    def test_bedrock_embedder_region_none_by_default(self) -> None:
        from yukar.indexer.embedder import BedrockTitanEmbedder

        emb = BedrockTitanEmbedder()
        assert emb._region is None


# ===========================================================================
# fix #6: NUL-byte binary heuristic
# ===========================================================================


class TestNulByteBinaryHeuristic:
    def test_nul_file_is_skipped(self, tmp_path: Path) -> None:
        """A file with NUL bytes in the first 2048 bytes should not be indexed."""
        from yukar.indexer.service import _collect_files
        from yukar.sandbox.ignore import IgnoreRules

        repo = make_git_repo(tmp_path, "nul-repo")
        binary = repo / "binary_no_ext"
        binary.write_bytes(b"some text\x00more text")

        (repo / "normal.py").write_text("x = 1\n")

        ignore = IgnoreRules.from_repo(repo)
        files = _collect_files(repo, ignore)
        file_names = {f.name for f in files}

        assert "binary_no_ext" not in file_names, "Binary file with NUL bytes should be excluded"
        assert "normal.py" in file_names

    def test_text_file_without_nul_is_included(self, tmp_path: Path) -> None:
        from yukar.indexer.service import _collect_files
        from yukar.sandbox.ignore import IgnoreRules

        repo = make_git_repo(tmp_path, "text-repo")
        (repo / "data.bin").write_bytes(b"pure text without nul bytes")

        ignore = IgnoreRules.from_repo(repo)
        files = _collect_files(repo, ignore)
        file_names = {f.name for f in files}
        assert "data.bin" in file_names


# ===========================================================================
# A3: symlink guard — symlinks pointing outside the repo are excluded
# ===========================================================================


class TestSymlinkGuard:
    """_collect_files must skip symlinks that resolve outside the repo tree (A3)."""

    def test_symlink_outside_repo_excluded(self, tmp_path: Path) -> None:
        """A symlink in the repo that points to a file outside the tree is skipped."""
        from yukar.indexer.service import _collect_files
        from yukar.sandbox.ignore import IgnoreRules

        repo = make_git_repo(tmp_path, "symlink-repo")
        # External file outside the repo.
        external = tmp_path / "secret.txt"
        external.write_text("do not index me\n")
        # Symlink inside the repo pointing to the external file.
        link = repo / "escape.txt"
        link.symlink_to(external)
        # Normal file that should be collected.
        (repo / "normal.py").write_text("x = 1\n")

        ignore = IgnoreRules.from_repo(repo)
        files = _collect_files(repo, ignore)
        file_names = {f.name for f in files}

        assert "escape.txt" not in file_names, "Symlink outside repo must be excluded"
        assert "normal.py" in file_names

    def test_symlink_inside_repo_included(self, tmp_path: Path) -> None:
        """A symlink pointing to a target *within* the repo tree is allowed."""
        from yukar.indexer.service import _collect_files
        from yukar.sandbox.ignore import IgnoreRules

        repo = make_git_repo(tmp_path, "intra-symlink-repo")
        real_file = repo / "source.py"
        real_file.write_text("y = 2\n")
        link = repo / "alias.py"
        link.symlink_to(real_file)

        ignore = IgnoreRules.from_repo(repo)
        files = _collect_files(repo, ignore)
        file_names = {f.name for f in files}

        # Both the original and the symlink (which resolves inside the repo) should
        # be present (symlinks inside the tree are safe to index).
        assert "source.py" in file_names
        assert "alias.py" in file_names


# ===========================================================================
# fix #8: 1-indexed line numbers in repo_tools
# ===========================================================================


class TestRepoToolsLineNumbers:
    async def test_search_results_have_1indexed_lines(self, tmp_path: Path) -> None:
        """repo_search must return start_line=1 for the first line (0-indexed=0 internally)."""
        from yukar.indexer.embedder import FakeEmbedder
        from yukar.indexer.service import IndexerService

        workspace = tmp_path / "ws"
        workspace.mkdir()
        repo = make_git_repo(tmp_path, "lines-repo")
        (repo / "code.py").write_text("def first():\n    pass\n")

        service = IndexerService(workspace_root=str(workspace), embedder=FakeEmbedder())
        await service.reindex_repo("proj", "lines-repo", repo)

        # Build a tool using the make_repo_tools factory.
        from yukar.agents.tools.repo_tools import make_repo_tools

        tools = make_repo_tools("proj", service, repo_name="lines-repo")
        repo_search = tools[0]

        # Call with exact text of first chunk to get a near-zero-distance result.
        from yukar.config import paths as config_paths
        from yukar.indexer import faiss_store

        idx_dir = config_paths.index_dir(str(workspace), "proj", "lines-repo")
        indexed_chunks, _ = await faiss_store.load_index(idx_dir)
        assert indexed_chunks

        first_chunk_text = indexed_chunks[0]["text"]
        result = await repo_search(query=first_chunk_text, top_k=1)
        assert result.get("results"), f"No results: {result}"

        top = result["results"][0]
        # Internal start_line is 0-indexed, tool must add 1.
        internal_start = indexed_chunks[0]["start_line"]
        assert top["start_line"] == internal_start + 1, (
            f"Expected start_line={internal_start + 1} (1-indexed), got {top['start_line']}"
        )
        assert top["end_line"] == indexed_chunks[0]["end_line"] + 1, (
            f"Expected end_line={indexed_chunks[0]['end_line'] + 1}, got {top['end_line']}"
        )


# ===========================================================================
# fix #9: fs_edit tools
# ===========================================================================


class TestFsEditTools:
    async def _make_ctx(self, worktree: Path) -> Any:
        from yukar.agents.context import AgentContext

        return await AgentContext.create(
            project_id="proj",
            epic_id="EP-1",
            repo_name="repo",
            worktree_path=worktree,
            workspace_root=str(worktree.parent),
        )

    async def _setup(self, tmp_path: Path) -> tuple[Any, Path, Any, Any, Any]:
        wt = tmp_path / "wt"
        wt.mkdir()
        ctx = await self._make_ctx(wt)
        from yukar.agents.tools.fs_edit import make_fs_edit_tools

        tools = make_fs_edit_tools(ctx)
        replace, insert_after, insert_before = tools
        return ctx, wt, replace, insert_after, insert_before

    # --- fs_replace_exact ---

    async def test_replace_exact_success(self, tmp_path: Path) -> None:
        ctx, wt, replace, _, _ = await self._setup(tmp_path)
        f = wt / "code.py"
        f.write_text("def foo():\n    return 1\n")

        result = replace(path="code.py", old_text="return 1", new_text="return 42")
        assert result["status"] == "success"
        assert "return 42" in f.read_text()
        assert "return 1" not in f.read_text()

    async def test_replace_exact_not_found(self, tmp_path: Path) -> None:
        ctx, wt, replace, _, _ = await self._setup(tmp_path)
        (wt / "code.py").write_text("x = 1\n")

        result = replace(path="code.py", old_text="DOES_NOT_EXIST", new_text="y")
        assert result["status"] == "error"
        assert "old_text not found" in result["content"][0]["text"]

    async def test_replace_exact_multiple_matches(self, tmp_path: Path) -> None:
        ctx, wt, replace, _, _ = await self._setup(tmp_path)
        (wt / "code.py").write_text("pass\npass\npass\n")

        result = replace(path="code.py", old_text="pass", new_text="return")
        assert result["status"] == "error"
        assert "multiple locations" in result["content"][0]["text"]

    async def test_replace_exact_empty_old_text(self, tmp_path: Path) -> None:
        ctx, wt, replace, _, _ = await self._setup(tmp_path)
        (wt / "code.py").write_text("x = 1\n")

        result = replace(path="code.py", old_text="", new_text="y")
        assert result["status"] == "error"
        assert "must not be empty" in result["content"][0]["text"]

    # --- fs_insert_after_exact ---

    async def test_insert_after_exact_success(self, tmp_path: Path) -> None:
        ctx, wt, _, insert_after, _ = await self._setup(tmp_path)
        f = wt / "code.py"
        f.write_text("def foo():\n    pass\n")

        result = insert_after(path="code.py", anchor_text="def foo():\n", new_text="    # added\n")
        assert result["status"] == "success"
        assert "# added" in f.read_text()
        assert f.read_text() == "def foo():\n    # added\n    pass\n"

    async def test_insert_after_exact_anchor_not_found(self, tmp_path: Path) -> None:
        ctx, wt, _, insert_after, _ = await self._setup(tmp_path)
        (wt / "code.py").write_text("x = 1\n")

        result = insert_after(path="code.py", anchor_text="NOPE", new_text="y")
        assert result["status"] == "error"
        assert "anchor_text not found" in result["content"][0]["text"]

    # --- fs_insert_before_exact ---

    async def test_insert_before_exact_success(self, tmp_path: Path) -> None:
        ctx, wt, _, _, insert_before = await self._setup(tmp_path)
        f = wt / "code.py"
        f.write_text("def bar():\n    pass\n")

        result = insert_before(path="code.py", anchor_text="def bar():", new_text="# before\n")
        assert result["status"] == "success"
        assert f.read_text() == "# before\ndef bar():\n    pass\n"

    # --- sandbox escape ---

    async def test_replace_exact_escape_rejected(self, tmp_path: Path) -> None:
        """Path traversal outside worktree must be rejected."""
        ctx, wt, replace, _, _ = await self._setup(tmp_path)
        # Create a file outside the worktree
        outside = tmp_path / "outside.py"
        outside.write_text("secret = 'oops'\n")

        result = replace(path="../outside.py", old_text="secret", new_text="hacked")
        assert result["status"] == "error"
        # File outside worktree must not be modified.
        assert outside.read_text() == "secret = 'oops'\n"

    async def test_insert_after_gitignored_path_rejected(self, tmp_path: Path) -> None:
        """Writing to a gitignored path must be rejected."""
        wt = tmp_path / "wt2"
        wt.mkdir()
        (wt / ".gitignore").write_text("secrets/\n")
        secrets = wt / "secrets"
        secrets.mkdir()
        (secrets / "key.txt").write_text("key=abc\n")

        ctx = await self._make_ctx(wt)
        from yukar.agents.tools.fs_edit import make_fs_edit_tools

        _, insert_after, _ = make_fs_edit_tools(ctx)

        result = insert_after(path="secrets/key.txt", anchor_text="key=abc", new_text="\nhacked")
        assert result["status"] == "error"
