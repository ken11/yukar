"""Indexer service — use-case façade.

``IndexerService`` bundles the three main use cases:

- ``reindex_repo(project_id, repo_name, repo_path)`` — full rebuild: walk →
  filter → split → embed → save FAISS + summary.
- ``search(project_id, query, repo_name?, top_k)`` — embed query → FAISS search
  → return ranked chunks.
- ``get_status(project_id)`` — repo-level indexing status (files/chunks/last_indexed_at).

Each ``IndexerService`` instance is associated with a workspace root and an
``Embedder``.  Construct one at startup and share it via the FastAPI app state.

Concurrency
-----------
``reindex_repo`` acquires the per-``(project, repo)`` FAISS lock (inside
``faiss_store``).  Concurrent calls for different repos proceed in parallel;
concurrent calls for the *same* repo are serialised automatically.
"""

from __future__ import annotations

import asyncio
import collections.abc
import datetime
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Awaitable

from yukar.config import paths as config_paths
from yukar.indexer import faiss_store
from yukar.indexer.embedder import Embedder
from yukar.indexer.splitter import Chunk, split_file
from yukar.indexer.stats import clear_error, read_error, read_stats, write_error
from yukar.indexer.summarizer import summarize_repo
from yukar.indexer.walker import _collect_files
from yukar.sandbox.ignore import IgnoreRules

# Re-export _collect_files so that ``from yukar.indexer.service import _collect_files``
# continues to work for existing tests.
__all__ = ["IndexerService", "RepoStatus", "_collect_files"]


def _set_embedder_loop(embedder: Embedder, loop: asyncio.AbstractEventLoop) -> None:
    """Inject the running event loop into *embedder* if it supports it.

    ``Embedder`` is a structural protocol and does not declare ``set_event_loop``.
    This helper uses ``getattr`` to duck-type the call so that both
    ``FakeEmbedder``/``BedrockTitanEmbedder`` (which have the method) and any
    third-party embedder that does not define it work without a type error.
    """
    setter = getattr(embedder, "set_event_loop", None)
    if callable(setter):
        setter(loop)


def _set_embedder_context(embedder: Embedder, project_id: str, run_id: str | None) -> None:
    """Attribute subsequent embeds to *project_id* / *run_id* if supported.

    The IndexerService shares ONE embedder across all projects, so its
    constructor defaults (project_id="", run_id=None) would collapse every
    project's code-index embedding cost into a single synthetic run.  This
    helper rebinds the attribution per call to the project actually being
    indexed/searched.

    ``Embedder`` is a structural protocol and does not declare ``set_context``;
    duck-typed via ``getattr`` so third-party embedders without it still work.

    Note: with ``workers=1`` and bounded ``_EMBED_SEM``, each ``embed_batch`` is
    awaited to completion before the next call's context is set, so setting
    mutable attribution on the shared instance does not interleave across
    projects.
    """
    setter = getattr(embedder, "set_context", None)
    if callable(setter):
        setter(project_id, run_id)


logger = logging.getLogger(__name__)

# Bounded semaphores so that to_thread calls (tree-sitter / FAISS) do not
# swamp the thread pool.  These are module-level singletons intentionally
# shared by memory/store.py and memory/rebuild.py — do not move into a class.
# Lazy once-per-loop init guards against cross-loop reuse in tests (each
# pytest-asyncio test gets a fresh event loop); first access per loop recreates.
_SPLIT_SEM: asyncio.Semaphore | None = None
_EMBED_SEM: asyncio.Semaphore | None = None
_SPLIT_SEM_LOOP: asyncio.AbstractEventLoop | None = None
_EMBED_SEM_LOOP: asyncio.AbstractEventLoop | None = None


def _get_split_sem() -> asyncio.Semaphore:
    global _SPLIT_SEM, _SPLIT_SEM_LOOP  # noqa: PLW0603
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if _SPLIT_SEM is None or loop is not _SPLIT_SEM_LOOP:
        _SPLIT_SEM = asyncio.Semaphore(4)
        _SPLIT_SEM_LOOP = loop
    return _SPLIT_SEM


def _get_embed_sem() -> asyncio.Semaphore:
    global _EMBED_SEM, _EMBED_SEM_LOOP  # noqa: PLW0603
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if _EMBED_SEM is None or loop is not _EMBED_SEM_LOOP:
        _EMBED_SEM = asyncio.Semaphore(2)
        _EMBED_SEM_LOOP = loop
    return _EMBED_SEM


# Batch size for embedding: number of text chunks submitted per embed_batch call.
_EMBED_BATCH_SIZE: int = 64


def _read_and_split(fpath: Path, repo_path: Path, repo_name: str) -> list[Chunk]:
    """Stat, read, and split *fpath* in a single synchronous call.

    Intended to be called via ``asyncio.to_thread`` so that three separate
    thread hops (stat / read / split) collapse into one round-trip per file.
    """
    mtime = fpath.stat().st_mtime
    text = fpath.read_text(encoding="utf-8", errors="replace")
    rel = fpath.relative_to(repo_path).as_posix()
    return split_file(text, repo=repo_name, path=rel, mtime=mtime)


IndexState = Literal["indexed", "indexing", "stale", "unindexed", "error"]


def _scan_disk_statuses(
    cache_index_dir: Path,
    seen_repos: set[str],
) -> list[RepoStatus]:
    """Synchronous helper that reads all per-repo stats/error files on disk.

    Intended to be called via ``asyncio.to_thread`` from ``get_status`` so that
    the synchronous ``read_stats`` / ``read_error`` calls do not block the
    single uvicorn event loop.

    Args:
        cache_index_dir: ``<workspace>/<project>/.yukar/cache/index/`` directory.
        seen_repos: Repos already reported as "indexing" — skip them here.

    Returns:
        List of ``RepoStatus`` objects for repos found on disk.
    """
    statuses: list[RepoStatus] = []
    for repo_dir in sorted(cache_index_dir.iterdir()):
        if not repo_dir.is_dir():
            continue
        rname = repo_dir.name
        if rname in seen_repos:
            continue

        has_usable_index = faiss_store.index_exists(repo_dir)
        err = read_error(repo_dir)
        last_error: str | None = err.get("message") if err is not None else None
        last_error_at: str | None = err.get("failed_at") if err is not None else None

        stats_path = repo_dir / "stats.json"
        if not stats_path.exists():
            if not has_usable_index:
                if err is not None:
                    statuses.append(
                        RepoStatus(
                            repo_name=rname,
                            state="error",
                            last_error=last_error,
                            last_error_at=last_error_at,
                        )
                    )
                else:
                    statuses.append(RepoStatus(rname, "unindexed"))
                continue
            statuses.append(
                RepoStatus(
                    repo_name=rname,
                    state="stale",
                    last_error=last_error,
                    last_error_at=last_error_at,
                )
            )
            continue

        raw = read_stats(repo_dir)
        if not raw:
            statuses.append(
                RepoStatus(
                    repo_name=rname,
                    state="stale",
                    last_error=last_error,
                    last_error_at=last_error_at,
                )
            )
        else:
            statuses.append(
                RepoStatus(
                    repo_name=rname,
                    state="indexed",
                    files=int(raw.get("files_indexed", 0)),
                    chunks=int(raw.get("chunks_indexed", 0)),
                    last_indexed_at=raw.get("last_indexed_at"),
                    ts_files=int(raw.get("ts_files", 0)),
                    fallback_files=int(raw.get("fallback_files", 0)),
                    last_error=last_error,
                    last_error_at=last_error_at,
                )
            )
    return statuses


class DimensionMismatchError(ValueError):
    """Raised when the query embedding dimension does not match the stored index dimension.

    This usually means the embedding model was changed after the index was built.
    The index must be rebuilt with the new model before searching.
    """


@dataclass(slots=True)
class RepoStatus:
    """Status snapshot for a single repo's index."""

    repo_name: str
    state: IndexState
    files: int = 0
    chunks: int = 0
    # ISO-8601 timestamp of the last successful index build (None if never indexed).
    last_indexed_at: str | None = None
    # tree-sitter split statistics: files that used structure splitting vs
    # files that fell back to line-based splitting (language=None chunks).
    ts_files: int = 0
    fallback_files: int = 0
    # Last indexing failure details (populated from error.json).
    last_error: str | None = None
    last_error_at: str | None = None

    def to_dict(self) -> dict[str, object]:
        import dataclasses as _dc

        return _dc.asdict(self)  # type: ignore[return-value]


class IndexerService:
    """High-level indexing and search service.

    Args:
        workspace_root: Root of the yukar workspace (``yukar-projects/``).
        embedder: Concrete ``Embedder`` instance.
    """

    def __init__(self, workspace_root: str, embedder: Embedder) -> None:
        self._root = workspace_root
        self._embedder = embedder
        # Track which (project, repo) pairs are currently reindexing.
        self._indexing: set[tuple[str, str]] = set()
        # Optional callback invoked on successful reindex.
        # Signature: (project_id: str, repo_name: str, repo_path: Path) -> Awaitable[None].
        self._on_indexed: (
            collections.abc.Callable[[str, str, Path], Awaitable[None]] | None
        ) = None

    def set_on_indexed(
        self,
        callback: collections.abc.Callable[[str, str, Path], Awaitable[None]],
    ) -> None:
        """Register an async callback invoked after each successful reindex.

        The callback receives ``(project_id, repo_name, repo_path)`` and is
        awaited on the event loop.  It must not block; long synchronous work
        must be offloaded to ``asyncio.to_thread`` or expressed as an async
        coroutine.  Pass ``None`` to unregister.

        Used by the lifespan to wire the watcher: when a repo is indexed for
        the first time (e.g. during a run's index-refresh phase), the watcher
        automatically begins monitoring it for subsequent changes.
        """
        self._on_indexed = callback

    # ------------------------------------------------------------------
    # Reindex
    # ------------------------------------------------------------------

    async def reindex_repo(
        self,
        project_id: str,
        repo_name: str,
        repo_path: Path,
        *,
        full: bool = True,
    ) -> int:
        """Rebuild (or incrementally update) the FAISS index for *repo_name*.

        On success, any pre-existing ``error.json`` in the index directory is
        deleted so that ``get_status()`` no longer reports the previous failure.

        On failure, the exception is written to ``error.json`` (atomically) so
        that ``get_status()`` can surface the reason the repo is stuck at an
        unindexed state, then the exception is re-raised.

        Args:
            project_id: Project identifier.
            repo_name: Repository name (used for cache paths and lock key).
            repo_path: Absolute path to the repository on disk.
            full: When ``True`` (default), always do a full rebuild regardless
                of the existing index state (used by the API's POST /index
                endpoint).  When ``False``, perform an incremental update that
                only re-embeds files whose mtime has changed since the last
                index build (used by the filesystem watcher).

        Returns:
            Number of chunks in the final index.

        Raises:
            Exception: Any exception raised by ``_do_reindex`` is re-raised
                after being persisted to ``error.json``.
        """
        index_dir = config_paths.index_dir(self._root, project_id, repo_name)
        key = (project_id, repo_name)
        self._indexing.add(key)
        try:
            result = await self._do_reindex(project_id, repo_name, repo_path, full=full)
        except Exception as exc:
            # Persist the failure so get_status() can surface it.
            await asyncio.to_thread(write_error, index_dir, exc)
            raise
        else:
            # Success — clear any previous error record.
            await asyncio.to_thread(clear_error, index_dir)
            # Notify the on_indexed hook (e.g. to register the repo with the watcher).
            if self._on_indexed is not None:
                try:
                    await self._on_indexed(project_id, repo_name, repo_path)
                except Exception:
                    logger.warning(
                        "on_indexed hook raised for %s/%s", project_id, repo_name, exc_info=True
                    )
            return result
        finally:
            self._indexing.discard(key)

    async def _do_reindex(
        self,
        project_id: str,
        repo_name: str,
        repo_path: Path,
        *,
        full: bool = True,
    ) -> int:
        index_dir = config_paths.index_dir(self._root, project_id, repo_name)

        # Build ignore rules (sync, but fast on typical repos)
        ignore_rules = await IgnoreRules.from_repo_async(repo_path)

        # Walk and collect files to index (includes mtime for each file)
        files = await asyncio.to_thread(_collect_files, repo_path, ignore_rules)
        logger.info("reindex_repo %s/%s: %d files to index", project_id, repo_name, len(files))

        # Determine which files need (re-)embedding.
        # changed_paths=None is the sentinel meaning "embed all" (full rebuild).
        # current_rel_paths=None is paired with changed_paths=None.
        changed_paths, current_rel_paths = await self._determine_changed_chunks(
            index_dir=index_dir,
            files=files,
            repo_path=repo_path,
            full=full,
        )

        # Split all files into chunks and filter empty ones.
        all_chunks = await self._prepare_chunks(
            files=files,
            repo_path=repo_path,
            repo_name=repo_name,
        )

        if not all_chunks and (changed_paths is None or changed_paths):
            logger.warning("reindex_repo %s/%s: no chunks produced", project_id, repo_name)
            return await _write_empty_index(
                index_dir, repo_path, ignore_rules, project_id=project_id, repo_name=repo_name
            )

        if full or not faiss_store.index_exists(index_dir):
            # Full rebuild: embed all chunks and replace the index.
            if not all_chunks:
                return await _write_empty_index(
                    index_dir, repo_path, ignore_rules, project_id=project_id, repo_name=repo_name
                )
            vectors = await self._embed_chunks(all_chunks, project_id=project_id)
            await faiss_store.save_index(
                index_dir,
                all_chunks,
                vectors,
                project_id=project_id,
                repo_name=repo_name,
            )
            embedding_dim = len(vectors[0]) if vectors else 0
        else:
            # Incremental update: only embed changed/new file chunks.
            # Dimension mismatch → fall back to full rebuild (recursive; see
            # _do_incremental_update docstring for the sentinel invariant).
            assert changed_paths is not None  # set in incremental branch
            assert current_rel_paths is not None
            result = await self._do_incremental_update(
                index_dir=index_dir,
                project_id=project_id,
                repo_name=repo_name,
                repo_path=repo_path,
                all_chunks=all_chunks,
                changed_paths=changed_paths,
                current_rel_paths=current_rel_paths,
            )
            if result is None:
                # Dimension mismatch — _do_incremental_update already fell back.
                return await self._do_reindex(project_id, repo_name, repo_path, full=True)
            embedding_dim = result

        # Reload to get the actual post-update chunk count.
        try:
            final_chunks, _ = await faiss_store.load_index(index_dir)
            final_chunk_count = len(final_chunks)
        except Exception:
            final_chunk_count = len(all_chunks)

        # Count files that used tree-sitter structure splitting vs line-based fallback.
        ts_files, fallback_files = _count_split_stats(all_chunks)
        last_indexed_at = datetime.datetime.now(datetime.UTC).isoformat()

        # Update summary and stats.json in a single to_thread call (no second write).
        await asyncio.to_thread(
            summarize_repo,
            repo_path,
            index_dir,
            ignore_rules=ignore_rules,
            files_indexed=len(files),
            chunks_indexed=final_chunk_count,
            embedding_dim=embedding_dim,
            ts_files=ts_files,
            fallback_files=fallback_files,
            last_indexed_at=last_indexed_at,
        )

        logger.info(
            "reindex_repo %s/%s: done — %d files, %d chunks (full=%s)",
            project_id,
            repo_name,
            len(files),
            final_chunk_count,
            full,
        )
        return final_chunk_count

    async def _determine_changed_chunks(
        self,
        index_dir: Path,
        files: list[Path],
        repo_path: Path,
        full: bool,
    ) -> tuple[set[str] | None, set[str] | None]:
        """Compute which files need (re-)embedding for an incremental index update.

        Returns ``(changed_paths, current_rel_paths)``.
        Both are ``None`` when a full rebuild is requested (the ``None``
        sentinel signals "embed all" to the caller so no extra branching is
        needed).

        Args:
            index_dir: Directory containing the existing FAISS index.
            files: All files collected by the walker for this repo.
            repo_path: Absolute path to the repository root.
            full: When ``True``, skip the mtime comparison and return
                ``(None, None)`` immediately.

        Returns:
            ``(changed_paths, current_rel_paths)`` — both ``None`` for full
            rebuild, both non-None sets for incremental.
        """
        if full or not faiss_store.index_exists(index_dir):
            # Full rebuild sentinel: changed_paths=None means "embed all".
            return None, None

        # Compute per-file mtime from existing chunks to detect changes.
        try:
            existing_chunks, _ = await faiss_store.load_index(index_dir)
        except Exception:
            existing_chunks = []

        old_file_mtime: dict[str, float] = {}
        for ch in existing_chunks:
            ch_path = ch["path"]
            mt = float(ch.get("mtime", 0.0))
            if ch_path not in old_file_mtime or mt > old_file_mtime[ch_path]:
                old_file_mtime[ch_path] = mt

        # Identify files that are new or have changed; build current path set
        # in the same loop to avoid a second pass over ``files``.
        changed_paths: set[str] = set()
        current_rel_paths: set[str] = set()
        for fpath in files:
            rel = fpath.relative_to(repo_path).as_posix()
            current_rel_paths.add(rel)
            try:
                current_mt = fpath.stat().st_mtime
            except OSError:
                continue
            stored_mt = old_file_mtime.get(rel)
            if stored_mt is None or current_mt != stored_mt:
                changed_paths.add(rel)

        return changed_paths, current_rel_paths

    async def _prepare_chunks(
        self,
        files: list[Path],
        repo_path: Path,
        repo_name: str,
    ) -> list[Chunk]:
        """Split all *files* into chunks with bounded concurrency.

        Empty/whitespace-only chunks are filtered out before returning.
        These originate from bare ``__init__.py`` and similar empty files;
        Bedrock Titan v2 rejects empty ``inputText`` with a ValidationException
        which would abort the entire reindex.

        Args:
            files: All files to split (from the walker).
            repo_path: Absolute path to the repository root (used for
                computing relative paths stored in each chunk).
            repo_name: Repository name (stored in each chunk).

        Returns:
            List of non-empty chunks ready for embedding.
        """
        all_chunks: list[Chunk] = []
        for fpath in files:
            async with _get_split_sem():
                try:
                    chunks = await asyncio.to_thread(_read_and_split, fpath, repo_path, repo_name)
                    all_chunks.extend(chunks)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("skip %s: %s", fpath, exc)

        # Filter empty/whitespace chunks (keeps chunks↔vectors counts aligned).
        return [c for c in all_chunks if c["text"].strip()]

    async def _do_incremental_update(
        self,
        index_dir: Path,
        project_id: str,
        repo_name: str,
        repo_path: Path,
        all_chunks: list[Chunk],
        changed_paths: set[str],
        current_rel_paths: set[str],
    ) -> int | None:
        """Perform an incremental FAISS index update for changed files only.

        Only chunks whose path is in *changed_paths* are re-embedded;
        unchanged chunks are kept from the existing index.

        Returns the new ``embedding_dim`` on success, or ``None`` when a
        dimension mismatch is detected (the caller must fall back to a full
        rebuild via ``_do_reindex(..., full=True)``).

        Args:
            index_dir: FAISS index directory.
            project_id: Project identifier.
            repo_name: Repository name.
            repo_path: Absolute path to the repository root (unused here,
                kept for symmetry with other helpers).
            all_chunks: All chunks for the current file set (used to derive
                *changed_chunks* and passed to ``update_index``).
            changed_paths: Relative paths of files that have changed since
                the last index build.
            current_rel_paths: Relative paths of all currently-existing files
                (used to remove deleted-file chunks from the index).
        """
        changed_chunks = [c for c in all_chunks if c["path"] in changed_paths]

        if changed_chunks:
            changed_vectors = await self._embed_chunks(changed_chunks, project_id=project_id)
        else:
            changed_vectors = []

        try:
            diff_stats = await faiss_store.update_index(
                index_dir,
                all_chunks,
                project_id=project_id,
                repo_name=repo_name,
                changed_chunks=changed_chunks,
                changed_vectors=changed_vectors,
                current_paths=current_rel_paths,
            )
        except faiss_store.IncrementalDimensionMismatchError:
            # Embedding model changed → signal caller to fall back to full rebuild.
            logger.warning(
                "reindex_repo %s/%s: dimension mismatch in incremental update; "
                "falling back to full rebuild",
                project_id,
                repo_name,
            )
            return None

        logger.info(
            "reindex_repo %s/%s: incremental — +%d -%d unchanged=%d",
            project_id,
            repo_name,
            diff_stats.get("added", 0),
            diff_stats.get("removed", 0),
            diff_stats.get("unchanged", 0),
        )
        return len(changed_vectors[0]) if changed_vectors else 0

    async def _embed_chunks(self, chunks: list[Chunk], *, project_id: str) -> list[list[float]]:
        """Embed *chunks* in fixed-size batches with bounded concurrency.

        Splitting into fixed-size batches keeps per-thread work manageable and
        allows progress to be observable between batches.

        The running event loop is captured here (on the async thread) and
        injected into the embedder so that synchronous ``embed_batch``
        implementations can schedule usage-tracking coroutines via
        ``asyncio.run_coroutine_threadsafe`` without calling
        ``asyncio.get_event_loop()`` from a worker thread.

        The embedder's usage attribution is also rebound to *project_id* (with a
        per-project ``run_id``) so that code-index embedding cost lands under the
        project that incurred it rather than a synthetic shared run.

        Args:
            chunks: Chunks to embed (must be non-empty; caller is responsible).
            project_id: Project being indexed — used for usage attribution.

        Returns:
            Flat list of embedding vectors, one per chunk, in the same order.
        """
        texts = [c["text"] for c in chunks]
        vectors: list[list[float]] = []

        # Capture the loop on the async side and inject into the embedder.
        loop = asyncio.get_running_loop()
        _set_embedder_loop(self._embedder, loop)
        # Attribute this project's index embedding to a per-project run so the
        # cost is not collapsed into a single synthetic shared run.
        _set_embedder_context(self._embedder, project_id, f"index-{project_id}")

        for batch_start in range(0, len(texts), _EMBED_BATCH_SIZE):
            batch = texts[batch_start : batch_start + _EMBED_BATCH_SIZE]
            async with _get_embed_sem():
                batch_vecs = await self._embedder.embed_batch_async(batch)
            vectors.extend(batch_vecs)
        return vectors

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    async def search(
        self,
        project_id: str,
        query: str,
        *,
        repo_name: str | None = None,
        top_k: int = 5,
    ) -> list[tuple[Chunk, float]]:
        """Search the indexed codebase with a natural-language query.

        Args:
            project_id: Project identifier.
            query: Natural-language or code snippet to search for.
            repo_name: If provided, search only this repo's index; otherwise
                search all indexed repos in the project.
            top_k: Maximum number of results.

        Returns:
            A list of ``(chunk, distance)`` pairs ordered by ascending distance.

        Raises:
            DimensionMismatchError: When the query vector dimension does not
                match the stored index dimension (indicates a model change).
        """
        # Inject the event loop for usage tracking from the worker thread, and
        # attribute query-embedding cost to the project being searched.
        loop = asyncio.get_running_loop()
        _set_embedder_loop(self._embedder, loop)
        _set_embedder_context(self._embedder, project_id, f"index-{project_id}")

        async with _get_embed_sem():
            q_vectors = await asyncio.to_thread(self._embedder.embed_batch, [query])
        q_vec = q_vectors[0]
        q_dim = len(q_vec)

        if repo_name is not None:
            index_dir = config_paths.index_dir(self._root, project_id, repo_name)
            if not faiss_store.index_exists(index_dir):
                return []
            # Dimension mismatch check: compare query dim against stored dim.
            await _check_dimension(index_dir, q_dim, repo_name)
            return await faiss_store.search_index(
                index_dir, q_vec, top_k=top_k, project_id=project_id, repo_name=repo_name
            )

        # Search across all indexed repos in the project.
        # Restrict to repos that have ``index.enabled=true`` (Minor review fix #1).
        # We read the project's repo list to honour the enabled flag; repos that
        # are on disk but not registered (or disabled) are skipped.
        from yukar.storage.project_repo import list_repos

        try:
            repos = await list_repos(self._root, project_id)
        except Exception:
            repos = []

        enabled_repo_names: set[str] = {r.name for r in repos if r.index.enabled}
        if not enabled_repo_names:
            return []

        results: list[tuple[Chunk, float]] = []
        for rname in enabled_repo_names:
            index_dir = config_paths.index_dir(self._root, project_id, rname)
            if not faiss_store.index_exists(index_dir):
                continue
            # Dimension mismatch check: same guard as the single-repo path.
            await _check_dimension(index_dir, q_dim, rname)
            partial = await faiss_store.search_index(
                index_dir, q_vec, top_k=top_k, project_id=project_id, repo_name=rname
            )
            results.extend(partial)

        # Sort combined results by ascending distance, return top_k
        results.sort(key=lambda x: x[1])
        return results[:top_k]

    # ------------------------------------------------------------------
    # Router helpers (index presence + unindexed list)
    # ------------------------------------------------------------------

    async def resolve_search_repos(
        self,
        project_id: str,
        repo_name: str | None,
    ) -> tuple[list[str], list[str]]:
        """Return ``(searchable_repo_names, unindexed_repo_names)`` for a search.

        Encapsulates index-existence judgment and the unindexed-hint list so
        that the router only needs to call this and then ``search()`` — no
        path resolution or ``faiss_store`` imports needed in the router
        (Minor review fix #6).

        Args:
            project_id: Project identifier.
            repo_name: Specific repo to search, or ``None`` for all enabled.

        Returns:
            A 2-tuple:
            - *searchable*: repo names that have a valid FAISS index.
            - *unindexed*: enabled repos that exist but are not yet indexed.
        """
        from yukar.storage.project_repo import list_repos

        if repo_name is not None:
            index_dir = config_paths.index_dir(self._root, project_id, repo_name)
            if faiss_store.index_exists(index_dir):
                return ([repo_name], [])
            return ([], [repo_name])

        repos = await list_repos(self._root, project_id)
        searchable: list[str] = []
        unindexed: list[str] = []
        for r in repos:
            if not r.index.enabled:
                continue
            index_dir = config_paths.index_dir(self._root, project_id, r.name)
            if faiss_store.index_exists(index_dir):
                searchable.append(r.name)
            else:
                unindexed.append(r.name)
        return (searchable, unindexed)

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    @property
    def workspace_root(self) -> str:
        """Workspace root path (``yukar-projects/``)."""
        return self._root

    async def get_status(self, project_id: str) -> list[RepoStatus]:
        """Return indexing status for each repo in *project_id*.

        Repos that are **currently being indexed** (``_indexing`` set) appear
        with ``state="indexing"`` even if their cache directory does not exist
        yet — this fixes the missing-spinner issue during the first index run.

        Reads ``stats.json`` from each repo's index directory for repos that
        are not actively indexing.

        Args:
            project_id: Project identifier.

        Returns:
            A list of ``RepoStatus`` objects (one per known or active repo).
        """
        statuses: list[RepoStatus] = []
        seen_repos: set[str] = set()

        # First: emit "indexing" entries for repos currently in-flight.
        # This ensures that a repo appears in the status response even before
        # its cache directory has been created (e.g. during the very first index).
        for pid, rname in sorted(self._indexing):
            if pid != project_id:
                continue
            statuses.append(RepoStatus(rname, "indexing"))
            seen_repos.add(rname)

        # Second: scan the on-disk cache directory for completed/stale repos.
        project_yukar = config_paths.yukar_dir(self._root, project_id)
        cache_index_dir = project_yukar / "cache" / "index"
        if not cache_index_dir.exists():
            return statuses

        disk_statuses = await asyncio.to_thread(
            _scan_disk_statuses, cache_index_dir, seen_repos
        )
        statuses.extend(disk_statuses)

        return statuses


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _write_empty_index(
    index_dir: Path,
    repo_path: Path,
    ignore_rules: IgnoreRules,
    *,
    project_id: str,
    repo_name: str,
) -> int:
    """Write an empty FAISS index + zero-chunk summary and return 0.

    Extracted from ``_do_reindex`` to eliminate two byte-identical blocks that
    each call ``save_index([], [])`` then ``summarize_repo`` with zero counts.
    """
    await faiss_store.save_index(index_dir, [], [], project_id=project_id, repo_name=repo_name)
    await asyncio.to_thread(
        summarize_repo,
        repo_path,
        index_dir,
        ignore_rules=ignore_rules,
        files_indexed=0,
        chunks_indexed=0,
    )
    return 0


def _count_split_stats(chunks: list[Chunk]) -> tuple[int, int]:
    """Count how many files used tree-sitter vs line-based splitting.

    A file is classified as ``ts`` if *any* of its chunks has a non-``None``
    ``language`` field (i.e. structure splitting succeeded for at least one
    chunk).  Otherwise the file is counted as ``fallback``.  Files that
    produced no chunks at all are not counted.

    Args:
        chunks: All chunks produced by the most recent reindex pass.

    Returns:
        A 2-tuple ``(ts_files, fallback_files)``.
    """
    # Build a per-path set of languages seen
    path_has_ts: dict[str, bool] = {}
    for c in chunks:
        p = c["path"]
        if c["language"] is not None:
            path_has_ts[p] = True
        elif p not in path_has_ts:
            path_has_ts[p] = False

    ts_files = sum(1 for v in path_has_ts.values() if v)
    fallback_files = sum(1 for v in path_has_ts.values() if not v)
    return ts_files, fallback_files


def _check_dimension_sync(index_dir: Path, query_dim: int, repo_name: str) -> None:
    """Synchronous core of the dimension check — run via ``to_thread``.

    Reads ``embedding_dim`` from ``stats.json``.  If the field is absent (old
    index that pre-dates this fix), the check is skipped — the search will
    proceed and may return garbage results if the dimensions actually differ.

    Args:
        index_dir: Directory containing ``stats.json``.
        query_dim: Dimension of the current query vector.
        repo_name: Repository name (used in error message).

    Raises:
        DimensionMismatchError: When a stored ``embedding_dim`` exists and
            differs from *query_dim*.
    """
    raw = read_stats(index_dir)
    if not raw:
        return  # No stats → skip check
    stored_dim = raw.get("embedding_dim")
    if stored_dim is None:
        return  # Old index without dim field → skip check
    if int(stored_dim) != query_dim:
        raise DimensionMismatchError(
            f"Repo '{repo_name}': index was built with embedding_dim={stored_dim} "
            f"but current embedder produces dim={query_dim}. "
            "Re-index the repository to resolve this mismatch."
        )


async def _check_dimension(index_dir: Path, query_dim: int, repo_name: str) -> None:
    """Async wrapper: offload the ``stats.json`` read to a thread.

    Prevents the synchronous file I/O in ``_check_dimension_sync`` from blocking
    the single uvicorn event loop (the carve-in finding from the best-practices
    report).  Raises ``DimensionMismatchError`` on mismatch.
    """
    await asyncio.to_thread(_check_dimension_sync, index_dir, query_dim, repo_name)
