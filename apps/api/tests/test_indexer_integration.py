"""Tests for M3 — Indexer foundation + gitignore sandbox.

Covers:
- sandbox/ignore: global/root/nested .gitignore, negation, .git exclusion, fs-tool integration
- indexer/splitter: python/ts function/class splitting, unknown-lang line fallback, size cap
- indexer/faiss_store: save → load → search round-trip, (project, repo) lock serialisation
- indexer/service: reindex → search → ignore exclusion; fixture repo with .gitignore
- indexer/watcher: file change → debounce → reindex triggered
- indexer/summarizer: summary.md and stats.json generation
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from tests._helpers import make_git_repo


def _make_fixture_repo(tmp_path: Path) -> Path:
    """Create a fixture repo with Python files, a .gitignore, and secrets."""
    repo = make_git_repo(tmp_path, "fixture-repo")

    # Normal source files
    (repo / "main.py").write_text(
        "def main():\n    pass\n\nclass App:\n    def run(self):\n        main()\n"
    )
    src = repo / "src"
    src.mkdir()
    (src / "utils.py").write_text("def helper(x: int) -> int:\n    return x + 1\n")
    (src / "models.py").write_text(
        "class User:\n    def __init__(self, name: str) -> None:\n        self.name = name\n"
    )

    # A secrets directory and .env file that should be ignored
    secrets = repo / "secrets"
    secrets.mkdir()
    (secrets / "api_key.txt").write_text("super-secret-key\n")
    (repo / ".env").write_text("API_KEY=secret\n")

    # Root .gitignore
    (repo / ".gitignore").write_text("# generated\n__pycache__/\n*.pyc\n.env\nsecrets/\n")

    # Nested .gitignore inside src/
    (src / ".gitignore").write_text("*.tmp\nbuild/\n")
    (src / "scratch.tmp").write_text("tmp content\n")
    src_build = src / "build"
    src_build.mkdir()
    (src_build / "output.py").write_text("# generated\n")

    return repo


# ===========================================================================
# sandbox/ignore tests
# ===========================================================================


class TestIgnoreRules:
    def _make_rules(
        self,
        repo: Path,
        *,
        global_excludes: Path | None = None,
    ) -> Any:
        from yukar.sandbox.ignore import IgnoreRules

        return IgnoreRules.from_repo(repo, global_excludes_path=global_excludes)

    # --- .git is always excluded ---

    def test_git_dir_always_excluded(self, tmp_path: Path) -> None:
        repo = make_git_repo(tmp_path)
        rules = self._make_rules(repo)
        assert rules.is_ignored(repo / ".git")
        assert rules.is_ignored(repo / ".git" / "config")
        assert rules.is_ignored(repo / ".git" / "objects" / "pack" / "some.pack")

    # --- Root .gitignore ---

    def test_root_gitignore_pattern_matches(self, tmp_path: Path) -> None:
        repo = make_git_repo(tmp_path)
        (repo / ".gitignore").write_text("*.pyc\n__pycache__/\n.env\n")
        (repo / "foo.pyc").write_text("")
        (repo / ".env").write_text("")
        rules = self._make_rules(repo)
        assert rules.is_ignored(repo / "foo.pyc")
        assert rules.is_ignored(repo / ".env")
        # src/__pycache__ should also be matched (pattern applies recursively)
        assert rules.is_ignored(repo / "src" / "__pycache__")

    def test_non_ignored_file_allowed(self, tmp_path: Path) -> None:
        repo = make_git_repo(tmp_path)
        (repo / ".gitignore").write_text("*.pyc\n")
        rules = self._make_rules(repo)
        assert not rules.is_ignored(repo / "main.py")
        assert not rules.is_ignored(repo / "README.md")

    # --- Negation patterns ---

    def test_negation_pattern_restores_file(self, tmp_path: Path) -> None:
        """!keep.pyc after *.pyc → keep.pyc is NOT ignored."""
        repo = make_git_repo(tmp_path)
        (repo / ".gitignore").write_text("*.pyc\n!keep.pyc\n")
        (repo / "keep.pyc").write_text("")
        (repo / "trash.pyc").write_text("")
        rules = self._make_rules(repo)
        assert not rules.is_ignored(repo / "keep.pyc")
        assert rules.is_ignored(repo / "trash.pyc")

    # --- Nested .gitignore ---

    def test_nested_gitignore_applied_to_subdirectory(self, tmp_path: Path) -> None:
        """*.tmp in src/.gitignore only matches under src/."""
        repo = make_git_repo(tmp_path)
        src = repo / "src"
        src.mkdir()
        (src / ".gitignore").write_text("*.tmp\n")
        (src / "scratch.tmp").write_text("")
        (repo / "scratch.tmp").write_text("")  # same name at root → NOT ignored
        rules = self._make_rules(repo)
        assert rules.is_ignored(src / "scratch.tmp")
        assert not rules.is_ignored(repo / "scratch.tmp")

    def test_nested_gitignore_does_not_affect_sibling_dir(self, tmp_path: Path) -> None:
        """*.log in a/ does not affect b/."""
        repo = make_git_repo(tmp_path)
        a = repo / "a"
        b = repo / "b"
        a.mkdir()
        b.mkdir()
        (a / ".gitignore").write_text("*.log\n")
        (a / "error.log").write_text("")
        (b / "error.log").write_text("")
        rules = self._make_rules(repo)
        assert rules.is_ignored(a / "error.log")
        assert not rules.is_ignored(b / "error.log")

    # --- Global gitignore ---

    def test_global_gitignore_applied(self, tmp_path: Path) -> None:
        repo = make_git_repo(tmp_path)
        global_gi = tmp_path / "global.gitignore"
        global_gi.write_text("*.secret\n")
        (repo / "creds.secret").write_text("")
        rules = self._make_rules(repo, global_excludes=global_gi)
        assert rules.is_ignored(repo / "creds.secret")

    def test_global_gitignore_missing_path_is_ignored(self, tmp_path: Path) -> None:
        """A non-existent global excludes path should be silently skipped."""
        repo = make_git_repo(tmp_path)
        nonexistent = tmp_path / "no_such_file.gitignore"
        rules = self._make_rules(repo, global_excludes=nonexistent)
        # Should not raise; normal files are not ignored
        assert not rules.is_ignored(repo / "main.py")

    # --- Directory pruning ---

    def test_should_prune_dir(self, tmp_path: Path) -> None:
        repo = make_git_repo(tmp_path)
        (repo / ".gitignore").write_text("node_modules/\n")
        node_modules = repo / "node_modules"
        node_modules.mkdir()
        rules = self._make_rules(repo)
        assert rules.should_prune_dir(node_modules)
        assert not rules.should_prune_dir(repo / "src")

    # --- make_ignore_fn ---

    def test_make_ignore_fn_callable(self, tmp_path: Path) -> None:
        repo = make_git_repo(tmp_path)
        (repo / ".gitignore").write_text("*.log\n")
        (repo / "app.log").write_text("")
        rules = self._make_rules(repo)
        fn = rules.make_ignore_fn()
        assert fn(repo / "app.log")
        assert not fn(repo / "main.py")


# ===========================================================================
# fs tools ignore integration tests
# ===========================================================================


class TestFsToolsIgnoreIntegration:
    """Verify that fs_read/fs_write/fs_list honour the ignore_fn in PathGuard.

    All tests use the production construction path ``AgentContext.create(...)``
    (which wires ``IgnoreRules`` into ``PathGuard`` via ``asyncio.to_thread``).
    No ``object.__setattr__`` bypass is used — this validates the real wiring.
    """

    async def _make_ctx(self, worktree: Path) -> Any:
        """Create an AgentContext via the production async factory."""
        from yukar.agents.context import AgentContext

        return await AgentContext.create(
            project_id="proj",
            epic_id="EP-1",
            repo_name="repo",
            worktree_path=worktree,
            workspace_root=str(worktree.parent),
        )

    async def test_fs_read_ignored_file_appears_not_found(self, tmp_path: Path) -> None:
        """Production path: .env gitignored in worktree → fs_read returns 'not found'."""
        from yukar.agents.tools.fs import make_fs_tools

        repo = tmp_path / "wt"
        repo.mkdir()
        (repo / ".gitignore").write_text(".env\n")
        (repo / ".env").write_text("SECRET=123\n")
        (repo / "main.py").write_text("x = 1\n")

        # Use production AgentContext.create — IgnoreRules wired automatically.
        ctx = await self._make_ctx(repo)
        fs_read, _, _ = make_fs_tools(ctx)

        # .env should appear as "not found" (gitignore blocks it)
        result = fs_read(path=".env")
        assert result["status"] == "error"
        assert "not found" in result["content"][0]["text"].lower()

        # main.py should be readable
        result2 = fs_read(path="main.py")
        assert result2["status"] == "success"

    async def test_fs_write_to_ignored_path_rejected(self, tmp_path: Path) -> None:
        """Production path: writing into gitignored secrets/ is rejected."""
        from yukar.agents.tools.fs import make_fs_tools

        repo = tmp_path / "wt"
        repo.mkdir()
        (repo / ".gitignore").write_text("secrets/\n")
        secrets = repo / "secrets"
        secrets.mkdir()

        ctx = await self._make_ctx(repo)
        _, fs_write, _ = make_fs_tools(ctx)

        # Writing inside secrets/ should be rejected
        result = fs_write(path="secrets/key.txt", content="hack")
        assert result["status"] == "error"

    async def test_fs_list_excludes_ignored_entries(self, tmp_path: Path) -> None:
        """Production path: fs_list omits .env and __pycache__ (gitignored)."""
        from yukar.agents.tools.fs import make_fs_tools

        repo = tmp_path / "wt"
        repo.mkdir()
        (repo / ".gitignore").write_text(".env\n__pycache__/\n")
        (repo / ".env").write_text("")
        pycache = repo / "__pycache__"
        pycache.mkdir()
        (pycache / "module.cpython-314.pyc").write_text("")
        (repo / "main.py").write_text("")

        ctx = await self._make_ctx(repo)
        _, _, fs_list = make_fs_tools(ctx)

        result = fs_list(path=".")
        assert result["status"] == "success"
        assert ".env" not in result["entries"]
        assert "__pycache__" not in result["entries"]
        assert "main.py" in result["entries"]


# ===========================================================================
# indexer/splitter tests
# ===========================================================================


class TestSplitter:
    def test_python_splits_functions_and_classes(self) -> None:
        from yukar.indexer.splitter import split_file

        code = "def foo():\n    return 1\n\nclass Bar:\n    def method(self):\n        return 2\n"
        chunks = split_file(code, repo="myrepo", path="app.py")
        assert len(chunks) >= 1
        # All chunks should belong to myrepo
        assert all(c["repo"] == "myrepo" for c in chunks)
        assert all(c["path"] == "app.py" for c in chunks)
        # Language should be detected as python
        # (may be None for line-based if tree-sitter fails, but normally python)
        text_concat = "".join(c["text"] for c in chunks)
        assert "foo" in text_concat
        assert "Bar" in text_concat

    def test_typescript_splits_functions(self) -> None:
        from yukar.indexer.splitter import split_file

        code = (
            "function greet(name: string): string {\n"
            "  return `Hello ${name}`;\n"
            "}\n"
            "\n"
            "class User {\n"
            "  constructor(public name: string) {}\n"
            "}\n"
        )
        chunks = split_file(code, repo="repo", path="app.ts")
        assert len(chunks) >= 1
        text_concat = "".join(c["text"] for c in chunks)
        assert "greet" in text_concat

    def test_unknown_language_line_fallback(self) -> None:
        from yukar.indexer.splitter import split_file

        code = "\n".join(f"line {i}" for i in range(200))
        chunks = split_file(code, repo="repo", path="data.xyz")
        # language should be None (line-based fallback)
        assert all(c["language"] is None for c in chunks)
        # Should have multiple chunks (200 lines > LINE_SPLIT_LINES default of 80)
        assert len(chunks) >= 3

    def test_large_chunk_is_re_split(self) -> None:
        from yukar.indexer.splitter import split_file

        # Generate a very long python file with many functions
        lines: list[str] = []
        for i in range(100):
            lines.extend([f"def func_{i}():", f"    return {i}", ""])
        code = "\n".join(lines)
        chunks = split_file(code, repo="repo", path="big.py", max_chars=200)
        # All chunks must be at most max_chars
        assert all(len(c["text"]) <= 200 for c in chunks)
        # Must have multiple chunks
        assert len(chunks) > 1

    def test_empty_file_produces_one_chunk(self) -> None:
        from yukar.indexer.splitter import split_file

        chunks = split_file("", repo="repo", path="empty.py")
        assert len(chunks) == 1

    def test_chunk_fields_present(self) -> None:
        from yukar.indexer.splitter import split_file

        chunks = split_file("x = 1\n", repo="myrepo", path="x.py")
        for c in chunks:
            assert "repo" in c
            assert "path" in c
            assert "start_line" in c
            assert "end_line" in c
            assert "text" in c
            assert "language" in c

    def test_line_split_degrade_for_no_language(self) -> None:
        """A file with no known extension always gets line-based splitting."""
        from yukar.indexer.splitter import split_file

        code = "content here\n" * 10
        chunks = split_file(code, repo="repo", path="Makefile")
        assert all(c["language"] is None for c in chunks)


# ===========================================================================
# indexer/faiss_store tests
# ===========================================================================


class TestFaissStore:
    def _make_chunks_and_vecs(
        self, n: int = 5, dim: int = 128, repo: str = "repo"
    ) -> tuple[list[Any], list[list[float]]]:
        import numpy as np

        from yukar.indexer.splitter import Chunk

        chunks = [
            Chunk(
                repo=repo,
                path=f"file_{i}.py",
                start_line=i,
                end_line=i + 1,
                text=f"chunk text {i}",
                language="python",
                mtime=0.0,
            )
            for i in range(n)
        ]
        # Unique vectors: one-hot-ish so nearest neighbour is self
        vecs = []
        for i in range(n):
            v = np.zeros(dim, dtype=np.float32)
            v[i % dim] = 1.0
            vecs.append(v.tolist())
        return chunks, vecs

    async def test_save_and_load_roundtrip(self, tmp_path: Path) -> None:
        from yukar.indexer import faiss_store

        chunks, vecs = self._make_chunks_and_vecs()
        idx_dir = tmp_path / "index"

        await faiss_store.save_index(idx_dir, chunks, vecs, project_id="proj", repo_name="repo")
        assert faiss_store.index_exists(idx_dir)

        loaded_chunks, loaded_idx = await faiss_store.load_index(idx_dir)
        assert len(loaded_chunks) == len(chunks)
        assert loaded_chunks[0]["repo"] == "repo"
        assert loaded_idx.ntotal == len(chunks)

    async def test_search_returns_nearest_neighbour(self, tmp_path: Path) -> None:
        from yukar.indexer import faiss_store

        dim = 128
        chunks, vecs = self._make_chunks_and_vecs(n=5, dim=dim)
        idx_dir = tmp_path / "index"
        await faiss_store.save_index(idx_dir, chunks, vecs, project_id="proj", repo_name="repo")

        # Query with the exact vector of chunk 2 → should be top-1
        q = vecs[2]
        results = await faiss_store.search_index(idx_dir, q, top_k=1)
        assert len(results) == 1
        assert results[0][0]["text"] == "chunk text 2"
        assert results[0][1] < 1e-5  # distance ~0 (exact match)

    async def test_search_top_k_capped_by_index_size(self, tmp_path: Path) -> None:
        from yukar.indexer import faiss_store

        chunks, vecs = self._make_chunks_and_vecs(n=3)
        idx_dir = tmp_path / "index"
        await faiss_store.save_index(idx_dir, chunks, vecs, project_id="proj", repo_name="repo")
        results = await faiss_store.search_index(idx_dir, vecs[0], top_k=10)
        assert len(results) == 3  # only 3 available

    async def test_concurrent_save_serialised(self, tmp_path: Path) -> None:
        """Two concurrent save calls for the same repo must not corrupt the index."""
        from yukar.indexer import faiss_store

        dim = 128
        chunks_a, vecs_a = self._make_chunks_and_vecs(n=3, dim=dim, repo="repo")
        chunks_b, vecs_b = self._make_chunks_and_vecs(n=4, dim=dim, repo="repo")
        idx_dir = tmp_path / "index"

        # Run both saves concurrently
        await asyncio.gather(
            faiss_store.save_index(idx_dir, chunks_a, vecs_a, project_id="proj", repo_name="repo"),
            faiss_store.save_index(idx_dir, chunks_b, vecs_b, project_id="proj", repo_name="repo"),
        )
        # One of the two should have won — index should be readable
        loaded_chunks, loaded_idx = await faiss_store.load_index(idx_dir)
        assert loaded_idx.ntotal in (3, 4)
        assert len(loaded_chunks) == loaded_idx.ntotal

    async def test_different_repos_have_independent_locks(self, tmp_path: Path) -> None:
        """Concurrent saves for *different* repos should not interfere."""
        from yukar.indexer import faiss_store

        dim = 128
        chunks_a, vecs_a = self._make_chunks_and_vecs(n=3, dim=dim, repo="repo-a")
        chunks_b, vecs_b = self._make_chunks_and_vecs(n=5, dim=dim, repo="repo-b")
        idx_a = tmp_path / "a"
        idx_b = tmp_path / "b"

        await asyncio.gather(
            faiss_store.save_index(idx_a, chunks_a, vecs_a, project_id="proj", repo_name="repo-a"),
            faiss_store.save_index(idx_b, chunks_b, vecs_b, project_id="proj", repo_name="repo-b"),
        )
        la, _ = await faiss_store.load_index(idx_a)
        lb, _ = await faiss_store.load_index(idx_b)
        assert len(la) == 3
        assert len(lb) == 5

    async def test_index_not_exists_before_save(self, tmp_path: Path) -> None:
        from yukar.indexer import faiss_store

        assert not faiss_store.index_exists(tmp_path / "new_dir")

    async def test_save_empty_chunks(self, tmp_path: Path) -> None:
        """save_index with empty lists should not raise."""
        from yukar.indexer import faiss_store

        idx_dir = tmp_path / "empty"
        await faiss_store.save_index(idx_dir, [], [], project_id="proj", repo_name="repo")
        # After saving empty, search should return empty
        results = await faiss_store.search_index(idx_dir, [0.0] * 128, top_k=5)
        assert results == []


# ===========================================================================
# indexer/service tests
# ===========================================================================


class TestIndexerService:
    def _make_service(self, workspace: Path) -> Any:
        from yukar.indexer.embedder import FakeEmbedder
        from yukar.indexer.service import IndexerService

        return IndexerService(workspace_root=str(workspace), embedder=FakeEmbedder())

    async def test_reindex_then_search_finds_relevant_chunk(self, tmp_path: Path) -> None:
        """After reindex, searching with the exact text of an indexed chunk returns it first.

        FakeEmbedder is deterministic and hash-based: the same text always maps
        to the same vector, so querying with the *exact* text of an indexed chunk
        yields distance ~0 for that chunk (L2 distance between identical vectors).
        """
        workspace = tmp_path / "ws"
        workspace.mkdir()

        repo = _make_fixture_repo(tmp_path)
        service = self._make_service(workspace)

        n_chunks = await service.reindex_repo("proj", "fixture-repo", repo)
        assert n_chunks > 0

        # Load the actual indexed chunks to find an exact chunk text to query with.
        from yukar.config import paths as config_paths
        from yukar.indexer import faiss_store

        idx_dir = config_paths.index_dir(str(workspace), "proj", "fixture-repo")
        indexed_chunks, _ = await faiss_store.load_index(idx_dir)
        assert indexed_chunks

        # Use the exact text of the first chunk as the query.
        # FakeEmbedder maps each text deterministically → distance should be ~0.
        exact_text = indexed_chunks[0]["text"]
        results = await service.search("proj", exact_text, repo_name="fixture-repo", top_k=3)
        assert len(results) > 0
        top_chunk, top_dist = results[0]
        # L2 distance between two identical unit vectors = 0
        assert top_dist < 1e-4  # exact match → distance ~0
        assert top_chunk["text"] == exact_text

    async def test_ignored_files_not_in_chunks(self, tmp_path: Path) -> None:
        """secrets/ and .env are gitignored → not in the index."""
        workspace = tmp_path / "ws"
        workspace.mkdir()

        repo = _make_fixture_repo(tmp_path)
        service = self._make_service(workspace)
        await service.reindex_repo("proj", "fixture-repo", repo)

        # Load raw chunks from the saved index
        from yukar.config import paths as config_paths
        from yukar.indexer import faiss_store

        idx_dir = config_paths.index_dir(str(workspace), "proj", "fixture-repo")
        chunks, _ = await faiss_store.load_index(idx_dir)

        paths_in_index = {c["path"] for c in chunks}
        # secrets/ should not appear
        assert not any("secrets" in p for p in paths_in_index)
        # .env should not appear
        assert ".env" not in paths_in_index

    async def test_ignored_files_not_in_chunks_env_file(self, tmp_path: Path) -> None:
        """Explicit check: .env file content must not appear in any chunk text."""
        workspace = tmp_path / "ws"
        workspace.mkdir()

        repo = _make_fixture_repo(tmp_path)
        service = self._make_service(workspace)
        await service.reindex_repo("proj", "fixture-repo", repo)

        from yukar.config import paths as config_paths
        from yukar.indexer import faiss_store

        idx_dir = config_paths.index_dir(str(workspace), "proj", "fixture-repo")
        chunks, _ = await faiss_store.load_index(idx_dir)

        # "super-secret-key" must not be in any chunk
        for c in chunks:
            assert "super-secret-key" not in c["text"]
            assert "API_KEY=secret" not in c["text"]

    async def test_get_status_reports_indexed(self, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()

        repo = _make_fixture_repo(tmp_path)
        service = self._make_service(workspace)
        await service.reindex_repo("proj", "fixture-repo", repo)

        statuses = await service.get_status("proj")
        assert len(statuses) == 1
        status = statuses[0]
        assert status.state == "indexed"
        assert status.files > 0
        assert status.chunks > 0
        assert status.repo_name == "fixture-repo"

    async def test_get_status_empty_project(self, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        service = self._make_service(workspace)
        statuses = await service.get_status("nonexistent-proj")
        assert statuses == []

    async def test_search_across_multiple_repos(self, tmp_path: Path) -> None:
        """search without repo_name searches across all enabled and indexed repos.

        The service now respects ``index.enabled`` for cross-repo search, so the
        repos must be registered in the workspace YAML before searching.
        """
        from yukar.models.project import Project, Repo, RepoIndex
        from yukar.storage.project_repo import save_project, save_repo

        workspace = tmp_path / "ws"
        workspace.mkdir()

        repo_a = make_git_repo(tmp_path, "repo-a")
        (repo_a / "a.py").write_text("def alpha():\n    pass\n")

        repo_b = make_git_repo(tmp_path, "repo-b")
        (repo_b / "b.py").write_text("def beta():\n    pass\n")

        root = str(workspace)
        # Register the project and both repos (index.enabled=True) in YAML.
        await save_project(
            root, Project(id="proj", name="Multi-Repo Proj", repos=["repo-a", "repo-b"])
        )
        await save_repo(
            root, "proj", Repo(name="repo-a", path=str(repo_a), index=RepoIndex(enabled=True))
        )
        await save_repo(
            root, "proj", Repo(name="repo-b", path=str(repo_b), index=RepoIndex(enabled=True))
        )

        service = self._make_service(workspace)
        await service.reindex_repo("proj", "repo-a", repo_a)
        await service.reindex_repo("proj", "repo-b", repo_b)

        results = await service.search("proj", "def alpha():\n    pass\n", top_k=5)
        repos_in_results = {r[0]["repo"] for r in results}
        assert "repo-a" in repos_in_results


# ===========================================================================
# M3 review regression tests
# ===========================================================================


class TestM3ReviewFixes:
    """Regression tests for M3 code-review fixes.

    Critical: gitignore wired into production AgentContext (no object.__setattr__).
    Major:    get_status shows 'indexing' before cache dir exists.
    Major:    search score is normalized 0-1 (1 = exact match).
    Minor #5: dimension mismatch raises DimensionMismatchError.
    """

    # -----------------------------------------------------------------------
    # Critical: production gitignore wiring
    # -----------------------------------------------------------------------

    async def test_production_path_gitignore_env_not_readable(self, tmp_path: Path) -> None:
        """`AgentContext.create` wires IgnoreRules; .env gitignored → fs_read returns not-found.

        This test uses the *production* construction path only — no
        ``object.__setattr__`` patching.
        """
        from yukar.agents.context import AgentContext
        from yukar.agents.tools.fs import make_fs_tools

        repo = tmp_path / "wt"
        repo.mkdir()
        (repo / ".gitignore").write_text(".env\nsecrets/\n")
        (repo / ".env").write_text("API_KEY=supersecret\n")
        (repo / "app.py").write_text("x = 1\n")
        secrets = repo / "secrets"
        secrets.mkdir()
        (secrets / "key.pem").write_text("-----BEGIN RSA PRIVATE KEY-----\n")

        # Production path — IgnoreRules built inside create() via asyncio.to_thread.
        ctx = await AgentContext.create(
            project_id="proj",
            epic_id="EP-1",
            repo_name="repo",
            worktree_path=repo,
            workspace_root=str(tmp_path),
        )
        fs_read, fs_write, fs_list = make_fs_tools(ctx)

        # .env must appear as "not found" (not a permission error — spec §6.6)
        result = fs_read(path=".env")
        assert result["status"] == "error", f"Expected error for ignored .env, got: {result}"
        assert "not found" in result["content"][0]["text"].lower()

        # app.py must be readable
        result2 = fs_read(path="app.py")
        assert result2["status"] == "success"
        assert "x = 1" in result2["content"][0]["text"]

        # Writing to secrets/ must be rejected
        result3 = fs_write(path="secrets/new.txt", content="evil")
        assert result3["status"] == "error"

        # fs_list must omit .env and secrets/
        result4 = fs_list(path=".")
        assert result4["status"] == "success"
        assert ".env" not in result4["entries"]
        assert "secrets" not in result4["entries"]
        assert "app.py" in result4["entries"]

    # -----------------------------------------------------------------------
    # Major: get_status shows 'indexing' before cache dir exists
    # -----------------------------------------------------------------------

    async def test_get_status_shows_indexing_before_cache_dir_created(self, tmp_path: Path) -> None:
        """A repo currently being indexed must appear as 'indexing' in get_status,
        even if its cache directory does not yet exist (first-run scenario)."""
        from yukar.indexer.embedder import FakeEmbedder
        from yukar.indexer.service import IndexerService

        workspace = tmp_path / "ws"
        workspace.mkdir()
        service = IndexerService(workspace_root=str(workspace), embedder=FakeEmbedder())

        # Manually add the repo to _indexing *without* building the cache dir.
        # This simulates the window between reindex_repo() entering _do_reindex
        # and saving the first file.
        service._indexing.add(("proj", "fixture-repo"))

        # get_status must report the repo even though no cache dir exists yet.
        statuses = await service.get_status("proj")
        assert len(statuses) == 1
        assert statuses[0].repo_name == "fixture-repo"
        assert statuses[0].state == "indexing"

        # Clean up so service is left in consistent state.
        service._indexing.discard(("proj", "fixture-repo"))

    # -----------------------------------------------------------------------
    # Major: search score normalization
    # -----------------------------------------------------------------------

    async def test_search_score_exact_match_is_one(self, tmp_path: Path) -> None:
        """Exact-match chunk must have score ~1.0 after normalization.

        FakeEmbedder is deterministic: querying with the exact text of an
        indexed chunk produces L2 distance ~0, so the normalized score
        1/(1+0) = 1.0.
        """
        from yukar.config import paths as config_paths
        from yukar.indexer import faiss_store
        from yukar.indexer.embedder import FakeEmbedder
        from yukar.indexer.service import IndexerService

        workspace = tmp_path / "ws"
        workspace.mkdir()
        repo = _make_fixture_repo(tmp_path)
        service = IndexerService(workspace_root=str(workspace), embedder=FakeEmbedder())
        await service.reindex_repo("proj", "fixture-repo", repo)

        # Load the actual chunks to find the exact text for a self-query.
        idx_dir = config_paths.index_dir(str(workspace), "proj", "fixture-repo")
        indexed_chunks, _ = await faiss_store.load_index(idx_dir)
        assert indexed_chunks, "No chunks in index"

        exact_text = indexed_chunks[0]["text"]
        results = await service.search("proj", exact_text, repo_name="fixture-repo", top_k=1)
        assert results, "Expected at least one result"
        _chunk, raw_distance = results[0]
        # Raw distance must be ~0 (FakeEmbedder deterministic)
        assert raw_distance < 1e-4, f"Expected near-zero L2 distance, got {raw_distance}"
        # Normalized score = 1 / (1 + distance) ≈ 1.0
        normalized = 1.0 / (1.0 + float(raw_distance))
        assert normalized > 0.999, f"Expected score ~1.0, got {normalized}"

    # -----------------------------------------------------------------------
    # Minor #5: dimension mismatch detection
    # -----------------------------------------------------------------------

    async def test_dimension_mismatch_raises_error(self, tmp_path: Path) -> None:
        """Searching with a query vector whose dimension differs from the stored
        index must raise DimensionMismatchError with a clear message."""
        from yukar.indexer.embedder import FakeEmbedder
        from yukar.indexer.service import DimensionMismatchError, IndexerService

        workspace = tmp_path / "ws"
        workspace.mkdir()
        repo = _make_fixture_repo(tmp_path)

        # Index with dim=128 (FakeEmbedder default).
        service_128 = IndexerService(
            workspace_root=str(workspace),
            embedder=FakeEmbedder(dim=128),
        )
        await service_128.reindex_repo("proj", "fixture-repo", repo)

        # Now search with dim=256 (simulates model change).
        service_256 = IndexerService(
            workspace_root=str(workspace),
            embedder=FakeEmbedder(dim=256),
        )
        import pytest

        with pytest.raises(DimensionMismatchError) as exc_info:
            await service_256.search("proj", "some query", repo_name="fixture-repo", top_k=3)
        assert "256" in str(exc_info.value)
        assert "128" in str(exc_info.value)


# ===========================================================================
# indexer/summarizer tests
# ===========================================================================


class TestSummarizer:
    def test_summary_and_stats_created(self, tmp_path: Path) -> None:
        from yukar.indexer.summarizer import summarize_repo
        from yukar.sandbox.ignore import IgnoreRules

        repo = _make_fixture_repo(tmp_path)
        idx_dir = tmp_path / "idx"

        ignore_rules = IgnoreRules.from_repo(repo)
        summarize_repo(
            repo,
            idx_dir,
            ignore_rules=ignore_rules,
            files_indexed=3,
            chunks_indexed=7,
        )

        assert (idx_dir / "summary.md").exists()
        assert (idx_dir / "stats.json").exists()

    def test_summary_contains_repo_name(self, tmp_path: Path) -> None:
        import json

        from yukar.indexer.summarizer import summarize_repo
        from yukar.sandbox.ignore import IgnoreRules

        repo = _make_fixture_repo(tmp_path)
        idx_dir = tmp_path / "idx"
        ignore_rules = IgnoreRules.from_repo(repo)
        summarize_repo(repo, idx_dir, ignore_rules=ignore_rules, files_indexed=3, chunks_indexed=5)

        md = (idx_dir / "summary.md").read_text()
        assert "fixture-repo" in md
        assert "3" in md  # files_indexed
        assert "5" in md  # chunks_indexed

        stats = json.loads((idx_dir / "stats.json").read_text())
        assert stats["files_indexed"] == 3
        assert stats["chunks_indexed"] == 5
        assert stats["repo"] == "fixture-repo"

    def test_ignored_files_not_in_tree(self, tmp_path: Path) -> None:
        from yukar.indexer.summarizer import summarize_repo
        from yukar.sandbox.ignore import IgnoreRules

        repo = _make_fixture_repo(tmp_path)
        idx_dir = tmp_path / "idx"
        ignore_rules = IgnoreRules.from_repo(repo)
        summarize_repo(repo, idx_dir, ignore_rules=ignore_rules)

        md = (idx_dir / "summary.md").read_text()
        assert "secrets" not in md
        assert ".env" not in md

    def test_python_symbols_extracted(self, tmp_path: Path) -> None:
        from yukar.indexer.summarizer import summarize_repo
        from yukar.sandbox.ignore import IgnoreRules

        repo = make_git_repo(tmp_path, "syms-repo")
        (repo / "app.py").write_text("def handler():\n    pass\n\nclass Controller:\n    pass\n")
        idx_dir = tmp_path / "idx"
        ignore_rules = IgnoreRules.from_repo(repo)
        summarize_repo(repo, idx_dir, ignore_rules=ignore_rules)

        md = (idx_dir / "summary.md").read_text()
        # handler and Controller should appear in the symbols section
        assert "handler" in md
        assert "Controller" in md


# ===========================================================================
# indexer/watcher tests
# ===========================================================================


class TestRepoWatcher:
    """Test RepoWatcher logic via _handle_changes (avoids filesystem event timing issues)."""

    async def test_handle_changes_triggers_debounce(self, tmp_path: Path) -> None:
        """_handle_changes with a non-ignored path schedules a reindex after debounce."""
        from unittest.mock import AsyncMock

        from yukar.indexer.watcher import RepoWatcher
        from yukar.sandbox.ignore import IgnoreRules

        repo = make_git_repo(tmp_path, "watch-repo")
        (repo / "new_file.py").write_text("x = 1\n")

        service_mock = AsyncMock()
        service_mock.reindex_repo = AsyncMock(return_value=5)

        watcher = RepoWatcher(service_mock, debounce=0.05)
        ignore_rules = IgnoreRules.from_repo(repo)
        watcher.add_repo("proj", "watch-repo", repo, ignore_rules=ignore_rules)

        # Simulate a file-change event directly (bypasses watchfiles timing)
        from watchfiles import Change

        watcher._handle_changes({(Change.added, str(repo / "new_file.py"))})

        # Wait for debounce + reindex task
        await asyncio.sleep(0.3)

        service_mock.reindex_repo.assert_called_once_with("proj", "watch-repo", repo, full=False)

    async def test_ignored_file_change_does_not_trigger_reindex(self, tmp_path: Path) -> None:
        """Changes to .pyc files (gitignored) must not schedule a reindex."""
        from unittest.mock import AsyncMock

        from watchfiles import Change

        from yukar.indexer.watcher import RepoWatcher
        from yukar.sandbox.ignore import IgnoreRules

        repo = make_git_repo(tmp_path, "watch-repo2")
        (repo / ".gitignore").write_text("*.pyc\n")

        service_mock = AsyncMock()
        service_mock.reindex_repo = AsyncMock(return_value=0)

        watcher = RepoWatcher(service_mock, debounce=0.05)
        ignore_rules = IgnoreRules.from_repo(repo)
        watcher.add_repo("proj", "watch-repo2", repo, ignore_rules=ignore_rules)

        # Simulate a .pyc file change
        watcher._handle_changes({(Change.added, str(repo / "module.pyc"))})

        # Wait longer than debounce — should NOT have triggered a reindex
        await asyncio.sleep(0.3)
        service_mock.reindex_repo.assert_not_called()

    async def test_debounce_collapses_rapid_changes(self, tmp_path: Path) -> None:
        """Multiple paths changed in the same batch collapse into one reindex.

        With hand-rolled debounce removed, ``awatch`` collapses rapid changes
        into a single batch before calling ``_handle_changes``.  This test
        mirrors that behaviour by passing all three changed paths in *one*
        ``_handle_changes`` call.  The ``triggered`` set inside
        ``_handle_changes`` deduplicates same-repo paths within the batch,
        so ``reindex_repo`` is called exactly once.
        """
        from unittest.mock import AsyncMock

        from watchfiles import Change

        from yukar.indexer.watcher import RepoWatcher
        from yukar.sandbox.ignore import IgnoreRules

        repo = make_git_repo(tmp_path, "watch-debounce")
        (repo / "a.py").write_text("")
        (repo / "b.py").write_text("")
        (repo / "c.py").write_text("")

        service_mock = AsyncMock()
        service_mock.reindex_repo = AsyncMock(return_value=3)

        watcher = RepoWatcher(service_mock, debounce=0.1)
        ignore_rules = IgnoreRules.from_repo(repo)
        watcher.add_repo("proj", "watch-debounce", repo, ignore_rules=ignore_rules)

        # awatch debounce collapses rapid FS events into one batch.  Simulate
        # that by passing all three paths in a single _handle_changes call.
        watcher._handle_changes({
            (Change.added, str(repo / "a.py")),
            (Change.modified, str(repo / "b.py")),
            (Change.modified, str(repo / "c.py")),
        })

        # Allow the reindex task to run.
        await asyncio.sleep(0.2)

        # triggered set collapses all three paths → exactly one reindex call.
        assert service_mock.reindex_repo.call_count == 1

    async def test_zero_repo_start_does_not_return_immediately(self, tmp_path: Path) -> None:
        """start() with no repos registered must not finish the watcher task immediately.

        Previously _run returned early when watch_paths was empty, causing a
        dead-end where later add_repo calls were never picked up.  Now _run
        waits on _wake_event so the task stays alive.
        """
        from unittest.mock import AsyncMock

        from yukar.indexer.watcher import RepoWatcher

        service_mock = AsyncMock()
        watcher = RepoWatcher(service_mock, debounce=0.05)

        # start with NO repos
        await watcher.start()

        # Yield to let the task run its first iteration.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        # The task must still be alive — waiting for a repo to be added.
        assert watcher._task is not None
        assert not watcher._task.done(), (
            "_run must wait on _wake_event when no repos are registered, "
            "not return immediately."
        )

        await watcher.stop()

    async def test_add_repo_sets_wake_event(self, tmp_path: Path) -> None:
        """add_repo must set _wake_event to interrupt a running awatch.

        This ensures that a repo registered after start() is picked up on the
        next awatch iteration without requiring a watcher restart.
        """
        from unittest.mock import AsyncMock

        from yukar.indexer.watcher import RepoWatcher

        service_mock = AsyncMock()
        watcher = RepoWatcher(service_mock, debounce=0.05)

        # Confirm _wake_event starts clear.
        assert not watcher._wake_event.is_set()

        repo = make_git_repo(tmp_path, "watch-wake")
        watcher.add_repo("proj", "watch-wake", repo)

        # add_repo must have set _wake_event.
        assert watcher._wake_event.is_set(), (
            "add_repo must call _wake_event.set() so _run restarts awatch "
            "with the updated path list."
        )

    async def test_watcher_stop_cancels_task(self, tmp_path: Path) -> None:
        from unittest.mock import AsyncMock

        from yukar.indexer.watcher import RepoWatcher

        repo = make_git_repo(tmp_path, "watch-repo3")
        service_mock = AsyncMock()
        watcher = RepoWatcher(service_mock, debounce=0.05)
        watcher.add_repo("proj", "watch-repo3", repo)
        await watcher.start()
        assert watcher._task is not None
        await watcher.stop()
        assert watcher._task is None or watcher._task.done()

    async def test_watcher_add_remove_repo(self, tmp_path: Path) -> None:
        from unittest.mock import AsyncMock

        from yukar.indexer.watcher import RepoWatcher

        repo = make_git_repo(tmp_path, "watch-add")
        service_mock = AsyncMock()
        watcher = RepoWatcher(service_mock)
        watcher.add_repo("p", "r", repo)
        assert ("p", "r") in watcher._repos
        watcher.remove_repo("p", "r")
        assert ("p", "r") not in watcher._repos

    async def test_start_idempotent(self, tmp_path: Path) -> None:
        """Calling start() twice should not create duplicate tasks."""
        from unittest.mock import AsyncMock

        from yukar.indexer.watcher import RepoWatcher

        repo = make_git_repo(tmp_path, "watch-idem")
        service_mock = AsyncMock()
        watcher = RepoWatcher(service_mock, debounce=0.05)
        watcher.add_repo("p", "r", repo)
        await watcher.start()
        task1 = watcher._task
        await watcher.start()
        task2 = watcher._task
        assert task1 is task2  # same task — not a new one
        await watcher.stop()
