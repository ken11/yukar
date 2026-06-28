"""FAISS index + chunks.jsonl persistence for repo-level vector search.

Layout (spec §4.1):
    ``{project}/.yukar/cache/index/{repo}/faiss.index``
    ``{project}/.yukar/cache/index/{repo}/chunks.jsonl``

Index type
----------
The on-disk index is a ``faiss.IndexIDMap2`` wrapping a ``faiss.IndexFlatL2``
inner index.  This allows individual vectors to be removed by their integer ID
(needed for incremental reindex).

Stable chunk IDs
----------------
Every chunk carries a persistent integer ``chunk_id`` field that is stored in
``chunks.jsonl``.  This ID is used as the FAISS vector ID so that search
results can be resolved via an ID→chunk dictionary rather than a positional
index.  Using an ID-keyed dict avoids the position/ID skew that arises when
stale vectors are removed and new vectors are appended during incremental
updates.

* **Full rebuild** (``save_index``): chunks are assigned IDs ``0, 1, 2, …``.
* **Incremental update** (``_incremental_update``): surviving chunks keep their
  existing IDs; new chunks receive IDs starting at
  ``max(existing_ids) + 1`` (or ``0`` if the index was empty).

Concurrency
-----------
FAISS is not thread-safe for concurrent writes.  A ``(project, repo)``-scoped
``asyncio.Lock`` serialises all rebuild operations.  FAISS I/O (the synchronous
C extension calls) is dispatched via ``asyncio.to_thread`` so the event loop
stays responsive.

Write safety
------------
Both ``faiss.index`` and ``chunks.jsonl`` are written atomically using
temp-file + ``os.replace`` (same approach as ``storage/atomic.py``).

In-memory cache
---------------
``search_index`` maintains a ``(project, repo)`` keyed cache of
``(chunks, faiss_index, mtime)`` tuples.  The cache is reused when the
``faiss.index`` file's mtime has not changed since the last load.  The cache is
explicitly invalidated after every ``save_index`` call.  Because uvicorn runs
``workers=1`` the cache lives in a single process and requires only an
``asyncio.Lock`` for serialisation.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, NamedTuple

import numpy as np

logger = logging.getLogger(__name__)


class IncrementalDimensionMismatchError(ValueError):
    """Raised by ``update_index`` when the stored FAISS dimension differs from the new vectors.

    Callers should respond by performing a full rebuild (``save_index``).
    """


# ---------------------------------------------------------------------------
# Per-(project, repo) asyncio lock registry
# ---------------------------------------------------------------------------

_locks: dict[tuple[str, str], asyncio.Lock] = {}


def _index_lock(project_id: str, repo_name: str) -> asyncio.Lock:
    key = (project_id, repo_name)
    if key not in _locks:
        _locks[key] = asyncio.Lock()
    return _locks[key]


# ---------------------------------------------------------------------------
# In-memory index cache
# ---------------------------------------------------------------------------


class _CacheEntry(NamedTuple):
    chunks: list[Any]  # list[Chunk]
    faiss_index: Any  # faiss.IndexIDMap2
    file_mtime: float  # os.path.getmtime of faiss.index at load time


_index_cache: dict[tuple[str, str], _CacheEntry] = {}
# Single lock guards the entire cache dict (uvicorn workers=1, single loop).
_cache_lock: asyncio.Lock | None = None


def _get_cache_lock() -> asyncio.Lock:
    global _cache_lock
    if _cache_lock is None:
        _cache_lock = asyncio.Lock()
    return _cache_lock


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def save_index(
    index_dir: Path,
    chunks: list[Any],  # list[Chunk]
    vectors: list[list[float]],
    *,
    project_id: str,
    repo_name: str,
) -> None:
    """Persist the FAISS index and chunk metadata atomically.

    This is the *only* function that writes to ``index_dir``.  It acquires the
    ``(project_id, repo_name)`` lock before any I/O so concurrent reindex
    calls are serialised.  The in-memory cache for this ``(project_id,
    repo_name)`` pair is invalidated so the next search re-loads from disk.

    Chunks are assigned stable integer IDs ``0, 1, 2, …`` before writing.
    The ``chunk_id`` field is added to each chunk dict if not already present.

    Args:
        index_dir: Directory to write ``faiss.index`` and ``chunks.jsonl``.
        chunks: Chunk metadata list (must be same length as *vectors*).
        vectors: Float32 vectors to store (one per chunk).
        project_id: Project identifier (used for lock key).
        repo_name: Repository name (used for lock key).

    Raises:
        ValueError: If *chunks* and *vectors* have different lengths.
    """
    if len(chunks) != len(vectors):
        raise ValueError(
            f"chunks ({len(chunks)}) and vectors ({len(vectors)}) must have the same length"
        )

    # Assign stable IDs starting at 0 for a full rebuild.
    stamped = [dict(ch, chunk_id=i) for i, ch in enumerate(chunks)]

    lock = _index_lock(project_id, repo_name)
    async with lock:
        await asyncio.to_thread(_write_index, index_dir, stamped, vectors)
        # Invalidate cache so next search reads fresh data.
        cache_key = (project_id, repo_name)
        async with _get_cache_lock():
            _index_cache.pop(cache_key, None)


async def load_index(
    index_dir: Path,
) -> tuple[list[Any], Any]:
    """Load the FAISS index and chunk metadata from *index_dir*.

    Args:
        index_dir: Directory containing ``faiss.index`` and ``chunks.jsonl``.

    Returns:
        A ``(chunks, faiss_index)`` tuple.

    Raises:
        FileNotFoundError: If either file is missing.
    """
    return await asyncio.to_thread(_read_index, index_dir)


async def search_index(
    index_dir: Path,
    query_vector: list[float],
    top_k: int = 5,
    *,
    project_id: str = "",
    repo_name: str = "",
) -> list[tuple[Any, float]]:
    """Search the FAISS index with *query_vector*.

    Uses an in-memory cache keyed by ``(project_id, repo_name)`` if both are
    provided.  The cache entry is reused when the ``faiss.index`` file's mtime
    has not changed since the last load.

    Args:
        index_dir: Directory containing the persisted index.
        query_vector: Query embedding vector (must match stored dimension).
        top_k: Number of nearest neighbours to return.
        project_id: Project identifier (cache key).  Empty string disables caching.
        repo_name: Repository name (cache key).  Empty string disables caching.

    Returns:
        A list of ``(chunk, distance)`` pairs sorted by ascending distance.
        Returns an empty list if no results are found or the index is empty.

    Raises:
        FileNotFoundError: If the index files do not exist.
    """
    cache_key = (project_id, repo_name)
    use_cache = bool(project_id and repo_name)

    chunks: list[Any]
    faiss_index: Any

    if use_cache:
        faiss_path = index_dir / "faiss.index"
        current_mtime: float = 0.0
        with contextlib.suppress(OSError):
            current_mtime = faiss_path.stat().st_mtime

        async with _get_cache_lock():
            entry = _index_cache.get(cache_key)
            if entry is not None and entry.file_mtime == current_mtime:
                chunks = entry.chunks
                faiss_index = entry.faiss_index
            else:
                # Load from disk and update cache.
                chunks, faiss_index = await asyncio.to_thread(_read_index, index_dir)
                _index_cache[cache_key] = _CacheEntry(
                    chunks=chunks,
                    faiss_index=faiss_index,
                    file_mtime=current_mtime,
                )
    else:
        chunks, faiss_index = await load_index(index_dir)

    if not chunks:
        return []

    # Build an ID→chunk lookup so search results resolve via stable chunk_id,
    # not positional index.  Chunks without a chunk_id fall back to enumeration
    # (legacy indexes built before this fix).
    id_to_chunk: dict[int, Any] = {}
    for pos, ch in enumerate(chunks):
        cid = ch.get("chunk_id", pos)
        id_to_chunk[int(cid)] = ch

    def _do_search() -> list[tuple[Any, float]]:
        n = faiss_index.ntotal
        if n == 0:
            return []
        actual_k = min(top_k, n)
        q = np.array([query_vector], dtype=np.float32)
        distances, indices = faiss_index.search(q, actual_k)
        results: list[tuple[Any, float]] = []
        for dist, idx in zip(distances[0], indices[0], strict=False):
            if idx < 0:
                continue
            chunk = id_to_chunk.get(int(idx))
            if chunk is None:
                continue
            results.append((chunk, float(dist)))
        return results

    return await asyncio.to_thread(_do_search)


def index_exists(index_dir: Path) -> bool:
    """Return ``True`` if a saved index exists in *index_dir*."""
    return (index_dir / "faiss.index").exists() and (index_dir / "chunks.jsonl").exists()


# ---------------------------------------------------------------------------
# Incremental update helpers
# ---------------------------------------------------------------------------


async def update_index(
    index_dir: Path,
    all_current_chunks: list[Any],
    *,
    project_id: str,
    repo_name: str,
    changed_chunks: list[Any] | None = None,
    changed_vectors: list[list[float]] | None = None,
    current_paths: set[str] | None = None,
) -> dict[str, int]:
    """Incrementally update the FAISS index.

    The caller is responsible for determining which files changed and embedding
    only those files.  This function removes deleted/updated file chunks from
    the existing index and adds new/updated chunks.

    Args:
        index_dir: Directory containing the existing index (may not exist yet).
        all_current_chunks: Complete list of ALL chunks for the current repo
            state (used to determine surviving-file chunk sets).
        project_id: Project identifier (lock key + cache key).
        repo_name: Repository name (lock key + cache key).
        changed_chunks: Chunks for files that were added or modified (pre-split).
        changed_vectors: Embedding vectors for *changed_chunks* (same order).
        current_paths: Set of repo-relative paths that currently exist on disk.
            Files in the existing index but NOT in this set are removed.

    Returns:
        A dict with ``added``, ``removed``, ``unchanged`` file counts.
    """
    if (
        changed_chunks is not None
        and changed_vectors is not None
        and len(changed_chunks) != len(changed_vectors)
    ):
        raise ValueError(
            f"changed_chunks ({len(changed_chunks)}) and changed_vectors "
            f"({len(changed_vectors)}) must have the same length"
        )

    lock = _index_lock(project_id, repo_name)
    async with lock:
        result = await asyncio.to_thread(
            _incremental_update,
            index_dir,
            all_current_chunks,
            changed_chunks or [],
            changed_vectors or [],
            current_paths,
        )
        async with _get_cache_lock():
            _index_cache.pop((project_id, repo_name), None)
    return result


# ---------------------------------------------------------------------------
# Synchronous helpers (run inside to_thread)
# ---------------------------------------------------------------------------


def _write_index(
    index_dir: Path,
    chunks: list[Any],
    vectors: list[list[float]],
) -> None:
    """Write FAISS index and chunks.jsonl.

    Each chunk must have a ``chunk_id`` field (assigned by the caller).
    FAISS vectors are stored using ``chunk_id`` as their integer ID so that
    search results can be resolved via an ID→chunk dict, not positional index.
    """
    import faiss  # type: ignore[import-untyped]

    index_dir.mkdir(parents=True, exist_ok=True)

    if not vectors:
        # Write empty IndexIDMap2 (dim=1 placeholder).
        dim = 1
        inner = faiss.IndexFlatL2(dim)
        faiss_idx: Any = faiss.IndexIDMap2(inner)
    else:
        dim = len(vectors[0])
        mat = np.asarray(vectors, dtype=np.float32)
        assert mat.ndim == 2  # noqa: S101 — vectors must be a rectangular 2-D array
        # Use chunk_id as the FAISS vector ID (stable across incremental updates).
        ids = np.array([int(ch.get("chunk_id", i)) for i, ch in enumerate(chunks)], dtype=np.int64)
        inner = faiss.IndexFlatL2(dim)
        faiss_idx = faiss.IndexIDMap2(inner)
        faiss_idx.add_with_ids(mat, ids)

    # Write faiss.index atomically
    faiss_path = index_dir / "faiss.index"
    _atomic_write_faiss(faiss_path, faiss_idx)

    # Write chunks.jsonl atomically
    jsonl_path = index_dir / "chunks.jsonl"
    _atomic_write_jsonl(jsonl_path, chunks)


def _read_index(index_dir: Path) -> tuple[list[Any], Any]:
    import faiss  # type: ignore[import-untyped]

    faiss_path = index_dir / "faiss.index"
    jsonl_path = index_dir / "chunks.jsonl"

    if not faiss_path.exists():
        raise FileNotFoundError(f"faiss.index not found in {index_dir}")
    if not jsonl_path.exists():
        raise FileNotFoundError(f"chunks.jsonl not found in {index_dir}")

    raw_idx = faiss.read_index(str(faiss_path))
    # Ensure the loaded index supports remove_ids (wrap if needed for old plain indexes
    # that were built with the pre-IndexIDMap2 code).
    if not isinstance(raw_idx, faiss.IndexIDMap2):
        logger.debug("Wrapping legacy IndexFlatL2 in IndexIDMap2 for %s", faiss_path)
        flat_inner: Any = raw_idx  # IndexFlatL2 from older yukar versions
        wrapped: Any = faiss.IndexIDMap2(flat_inner)
        # Re-add all vectors so IDs are assigned contiguously.
        if flat_inner.ntotal > 0:
            # reconstruct_n returns (ntotal, d) float32 without touching SWIG internals.
            mat = flat_inner.reconstruct_n(0, flat_inner.ntotal)
            ids = np.arange(flat_inner.ntotal, dtype=np.int64)
            wrapped.add_with_ids(mat, ids)
        raw_idx = wrapped

    chunks: list[Any] = []
    with jsonl_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                logger.warning(
                    "faiss_store: JSON parse failed in chunks.jsonl, skipping line: %r",
                    line[:80],
                )
                continue
            if not isinstance(obj, dict):
                logger.warning(
                    "faiss_store: line in chunks.jsonl is not a JSON object, skipping: %r",
                    line[:80],
                )
                continue
            # NOTE: skipping a corrupt line shifts positional indices for legacy indexes
            # that lack chunk_id fields.  This is accepted as it is far better than
            # aborting the entire read; chunk_id-keyed indexes (all current indexes) are
            # unaffected because resolution uses id_to_chunk[chunk_id], not position.
            chunks.append(obj)

    return chunks, raw_idx


def _incremental_update(
    index_dir: Path,
    all_current_chunks: list[Any],
    changed_chunks: list[Any],
    changed_vectors: list[list[float]],
    current_paths: set[str] | None,
) -> dict[str, int]:
    """Synchronous incremental update logic (runs in to_thread).

    Args:
        index_dir: Directory containing the existing FAISS index.
        all_current_chunks: All chunks for files currently on disk (used to
            rebuild the surviving-chunk list).
        changed_chunks: Chunks for files that were added or modified.
        changed_vectors: Embedding vectors for *changed_chunks* (same order).
        current_paths: Set of repo-relative paths currently on disk.  Files in
            the existing index that are NOT in this set are deleted.
    """
    import faiss  # type: ignore[import-untyped]

    try:
        existing_chunks, existing_idx = _read_index(index_dir)
    except Exception as exc:
        logger.warning("Incremental update: failed to load index (%s), skip", exc)
        return {"added": 0, "removed": 0, "unchanged": 0}

    # Dimension mismatch check.
    if changed_vectors:
        new_dim = len(changed_vectors[0])
        inner = getattr(existing_idx, "index", None)
        stored_dim: int | None = None
        if inner is not None and hasattr(inner, "d"):
            stored_dim = int(inner.d)
        elif hasattr(existing_idx, "d"):
            stored_dim = int(existing_idx.d)
        if stored_dim is not None and stored_dim != new_dim:
            raise IncrementalDimensionMismatchError(
                f"Incremental update: dimension mismatch (stored={stored_dim}, new={new_dim}). "
                "Caller must perform a full rebuild."
            )

    # Paths being replaced (updated) or removed.
    changed_path_set: set[str] = {c["path"] for c in changed_chunks}
    if current_paths is not None:
        old_paths = {c["path"] for c in existing_chunks}
        removed_path_set = old_paths - current_paths
    else:
        removed_path_set = set()

    files_to_remove = changed_path_set | removed_path_set

    if not files_to_remove and not changed_chunks:
        unchanged_count = len({c["path"] for c in existing_chunks})
        return {"added": 0, "removed": 0, "unchanged": unchanged_count}

    # Remove stale/deleted vectors by their stable chunk_id (not positional index).
    ids_to_remove: list[int] = [
        int(ch.get("chunk_id", i))
        for i, ch in enumerate(existing_chunks)
        if ch["path"] in files_to_remove
    ]
    if ids_to_remove:
        selector = faiss.IDSelectorBatch(np.asarray(ids_to_remove, dtype=np.int64))
        existing_idx.remove_ids(selector)

    # Surviving chunks (still valid, not removed or replaced).
    surviving_chunks = [ch for ch in existing_chunks if ch["path"] not in files_to_remove]

    # Add new/updated chunks with IDs that continue past the maximum existing ID.
    if changed_chunks and changed_vectors:
        # Determine the next available ID (max existing + 1, or 0 for an empty index).
        if existing_chunks:
            max_existing_id = max(
                int(ch.get("chunk_id", i)) for i, ch in enumerate(existing_chunks)
            )
        else:
            max_existing_id = -1
        start_id = max_existing_id + 1

        # Stamp new chunks with their stable IDs before adding to FAISS.
        stamped_new: list[Any] = [
            dict(ch, chunk_id=start_id + j) for j, ch in enumerate(changed_chunks)
        ]
        mat = np.asarray(changed_vectors, dtype=np.float32)
        assert mat.ndim == 2  # noqa: S101 — changed_vectors must be a rectangular 2-D array
        new_ids = np.arange(start_id, start_id + len(stamped_new), dtype=np.int64)
        existing_idx.add_with_ids(mat, new_ids)
        final_chunks = surviving_chunks + stamped_new
    else:
        final_chunks = surviving_chunks

    # Persist atomically.
    index_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write_faiss(index_dir / "faiss.index", existing_idx)
    _atomic_write_jsonl(index_dir / "chunks.jsonl", final_chunks)

    return {
        "added": len(changed_path_set),
        "removed": len(removed_path_set),
        "unchanged": len({c["path"] for c in surviving_chunks}),
    }


def _atomic_replace(dest: Path, write_fn: Any, *, prefix: str, suffix: str = "") -> None:
    """Write *dest* atomically using a temp-file + ``os.replace`` pattern.

    ``write_fn(tmp_path: str) -> None`` is called with the path of a temporary
    file in the same directory as *dest*.  On success the temp file is renamed
    to *dest*; on failure it is deleted and the original exception is re-raised.

    Args:
        dest: Final destination path.
        write_fn: Callable that writes content to the given temp-file path.
        prefix: ``tempfile.mkstemp`` prefix (used to identify leftover temps).
        suffix: Optional ``tempfile.mkstemp`` suffix.
    """
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=prefix, suffix=suffix)
    os.close(fd)
    try:
        write_fn(tmp)
        os.replace(tmp, dest)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _atomic_write_faiss(dest: Path, faiss_idx: Any) -> None:
    """Write a FAISS index atomically (temp + os.replace)."""
    import faiss  # type: ignore[import-untyped]

    _atomic_replace(dest, lambda tmp: faiss.write_index(faiss_idx, tmp), prefix=".tmp_faiss_")


def _atomic_write_jsonl(dest: Path, chunks: list[Any]) -> None:
    """Write chunks.jsonl atomically (temp + os.replace)."""

    def _write(tmp: str) -> None:
        with Path(tmp).open("w", encoding="utf-8") as f:
            for chunk in chunks:
                f.write(json.dumps(chunk) + "\n")

    _atomic_replace(dest, _write, prefix=".tmp_chunks_", suffix=".jsonl")
