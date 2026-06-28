"""Project-scoped FAISS index for Project Memory.

Shares _atomic_write_faiss / _atomic_write_jsonl / _read_index from faiss_store.py
by calling them directly rather than duplicating them.

IndexIDMap2 over IndexFlatL2. Maintains chunk_id ↔ record mapping.
Dimension mismatch detection → raises IncrementalDimensionMismatchError (B2 fix).
Adding real-dimension vectors to an empty index (ntotal==0) is safe
(the index is rebuilt with the real dimension when ntotal==0).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import numpy as np

from yukar.indexer.faiss_store import (
    IncrementalDimensionMismatchError,
    _atomic_write_faiss,
    _atomic_write_jsonl,
    _read_index,
)

logger = logging.getLogger(__name__)

# Per-project lock registry (project_id → Lock)
_project_locks: dict[str, asyncio.Lock] = {}


def _memory_lock(project_id: str) -> asyncio.Lock:
    if project_id not in _project_locks:
        _project_locks[project_id] = asyncio.Lock()
    return _project_locks[project_id]


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


async def search_index_memory(
    index_dir: Path,
    query_vector: list[float],
    top_k: int = 5,
    *,
    project_id: str,
) -> list[tuple[dict[str, Any], float]]:
    """Search the FAISS index and return a list of (metadata_dict, distance) tuples.

    Returns an empty list when the index does not exist or is empty.
    """
    if not (index_dir / "faiss.index").exists():
        return []

    lock = _memory_lock(project_id)
    async with lock:
        return await asyncio.to_thread(
            _sync_search,
            index_dir,
            query_vector,
            top_k,
        )


def index_consistency(index_dir: Path) -> tuple[int, int] | None:
    """Return the ntotal of faiss.index and the record count of chunks.jsonl as (ntotal, n_chunks).

    Used for torn-write detection: _atomic_write_faiss and _atomic_write_jsonl are each
    individually atomic, but a crash between the two os.replace calls can leave
    faiss.ntotal != len(chunks). mtime comparison cannot detect this inconsistency,
    so a count comparison is used to trigger self-repair.

    Returns None when the index does not exist or cannot be read (caller treats this as missing).
    Contains a synchronous C extension call (faiss read) — call via to_thread.
    """
    try:
        chunks, faiss_idx = _read_index(index_dir)
    except Exception:  # if unreadable (missing/corrupt), treat as inconsistent → None
        logger.debug("memory/index: index_consistency read failed", exc_info=True)
        return None
    return int(faiss_idx.ntotal), len(chunks)


async def _rebuild_index(
    index_dir: Path,
    records: list[dict[str, Any]],
    vectors: list[list[float]],
    *,
    project_id: str,
) -> None:
    """Fully rebuild the index from the source of truth.

    Protected by a per-project lock.
    """
    lock = _memory_lock(project_id)
    async with lock:
        await asyncio.to_thread(
            _sync_rebuild,
            index_dir,
            records,
            vectors,
        )


# ---------------------------------------------------------------------------
# Synchronous helpers (run inside to_thread)
# ---------------------------------------------------------------------------


def _sync_add_to_index(
    index_dir: Path,
    record_id: str,
    vector: list[float],
    metadata: dict[str, Any],
) -> None:
    dim = len(vector)
    index_dir.mkdir(parents=True, exist_ok=True)
    faiss_path = index_dir / "faiss.index"
    jsonl_path = index_dir / "chunks.jsonl"

    # Load existing or create fresh
    if faiss_path.exists() and jsonl_path.exists():
        try:
            existing_chunks, faiss_idx = _read_index(index_dir)
        except Exception:
            logger.warning("memory/index: failed to load index; creating fresh", exc_info=True)
            existing_chunks, faiss_idx = [], _make_fresh_index(dim)
    else:
        existing_chunks = []
        faiss_idx = _make_fresh_index(dim)

    # Detect placeholder (dim=1, ntotal==0) → rebuild with real dim
    inner = getattr(faiss_idx, "index", None)
    stored_dim: int | None = None
    if inner is not None and hasattr(inner, "d"):
        stored_dim = int(inner.d)
    elif hasattr(faiss_idx, "d"):
        stored_dim = int(faiss_idx.d)

    if faiss_idx.ntotal == 0 and stored_dim is not None and stored_dim != dim:
        logger.debug(
            "memory/index: placeholder dim=%d → rebuilding with real dim=%d",
            stored_dim,
            dim,
        )
        faiss_idx = _make_fresh_index(dim)

    # B2: dimension mismatch with existing data → raise rather than silently discarding.
    # The caller (store.add) re-embeds all entries via rebuild_memory_index,
    # rebuilds the index, then adds the new record
    # (same design as the update_index→save_index fallback in faiss_store).
    if stored_dim is not None and stored_dim != dim and faiss_idx.ntotal > 0:
        raise IncrementalDimensionMismatchError(
            f"memory/index: dimension mismatch (stored={stored_dim}, new={dim}). "
            "Caller must perform a full rebuild via rebuild_memory_index."
        )

    # Next ID
    if existing_chunks:
        max_id = max(int(ch.get("chunk_id", 0)) for ch in existing_chunks)
        next_id = max_id + 1
    else:
        next_id = 0

    chunk = {
        "chunk_id": next_id,
        "record_id": record_id,
        **metadata,
    }

    vec_arr = np.array([vector], dtype=np.float32)
    ids_arr = np.array([next_id], dtype=np.int64)
    faiss_idx.add_with_ids(vec_arr, ids_arr)

    updated_chunks = existing_chunks + [chunk]
    _atomic_write_faiss(faiss_path, faiss_idx)
    _atomic_write_jsonl(jsonl_path, updated_chunks)


def _sync_search(
    index_dir: Path,
    query_vector: list[float],
    top_k: int,
) -> list[tuple[dict[str, Any], float]]:
    try:
        chunks, faiss_idx = _read_index(index_dir)
    except FileNotFoundError:
        return []

    if faiss_idx.ntotal == 0 or not chunks:
        return []

    id_to_chunk: dict[int, dict[str, Any]] = {}
    for pos, ch in enumerate(chunks):
        cid = int(ch.get("chunk_id", pos))
        id_to_chunk[cid] = ch

    actual_k = min(top_k, faiss_idx.ntotal)
    q = np.array([query_vector], dtype=np.float32)
    distances, indices = faiss_idx.search(q, actual_k)

    results: list[tuple[dict[str, Any], float]] = []
    for dist, idx in zip(distances[0], indices[0], strict=False):
        if idx < 0:
            continue
        chunk = id_to_chunk.get(int(idx))
        if chunk is None:
            continue
        results.append((chunk, float(dist)))
    return results


def _sync_rebuild(
    index_dir: Path,
    records: list[dict[str, Any]],
    vectors: list[list[float]],
) -> None:
    import faiss  # type: ignore[import-untyped]

    index_dir.mkdir(parents=True, exist_ok=True)
    faiss_path = index_dir / "faiss.index"
    jsonl_path = index_dir / "chunks.jsonl"

    if not vectors:
        inner = faiss.IndexFlatL2(1)
        faiss_idx: Any = faiss.IndexIDMap2(inner)
        _atomic_write_faiss(faiss_path, faiss_idx)
        _atomic_write_jsonl(jsonl_path, [])
        return

    dim = len(vectors[0])
    inner = faiss.IndexFlatL2(dim)
    faiss_idx = faiss.IndexIDMap2(inner)

    stamped: list[dict[str, Any]] = []
    for i, (rec, _vec) in enumerate(zip(records, vectors, strict=True)):
        chunk = dict(rec, chunk_id=i)
        stamped.append(chunk)

    mat = np.array(vectors, dtype=np.float32).reshape(-1, dim)
    ids = np.arange(len(stamped), dtype=np.int64)
    faiss_idx.add_with_ids(mat, ids)

    _atomic_write_faiss(faiss_path, faiss_idx)
    _atomic_write_jsonl(jsonl_path, stamped)


def _make_fresh_index(dim: int) -> Any:
    import faiss  # type: ignore[import-untyped]

    inner = faiss.IndexFlatL2(dim)
    return faiss.IndexIDMap2(inner)
