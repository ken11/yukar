"""Regenerate the FAISS index from the source-of-truth project.jsonl.

The source of truth is authoritative (single source of truth). The derived index can be
restored by a full rebuild.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from yukar.indexer.embedder import Embedder
from yukar.indexer.service import _get_embed_sem, _set_embedder_loop
from yukar.memory.index import _memory_lock, _sync_rebuild
from yukar.memory.records import parse_records

logger = logging.getLogger(__name__)


async def rebuild_memory_index(
    jsonl_path: Path,
    index_dir: Path,
    embedder: Embedder,
    *,
    project_id: str,
) -> int:
    """Fully rebuild the index from project.jsonl.

    Concurrency (finding 3): perform the read + embed + write of the source of truth
    entirely under a per-project lock. With max_parallel_epics>1, an add() during rebuild
    could be lost after the rebuild, so the source of truth is read immediately after
    acquiring the lock and the snapshot is used for the rebuild.
    Rebuild runs rarely (only at startup or when stale), so the cost of embedding
    inside the lock is acceptable.

    Poison-record isolation (finding 4): embed records one at a time so that a single
    embed failure (e.g. a record enlarged by manual editing) does not permanently break
    the entire rebuild — skip+log the failed record (excluded from the index but kept in jsonl).

    Returns:
        Number of entries registered in the index (excludes records skipped due to embed failure).
    """
    # Perform read + embed + write in a single batch while holding the lock (finding 3).
    # _sync_rebuild is an internal path that does not acquire the lock,
    # so we call to_thread directly here.
    lock = _memory_lock(project_id)
    async with lock:
        if not jsonl_path.exists():
            logger.debug("rebuild_memory_index: %s does not exist; nothing to rebuild", jsonl_path)
            await asyncio.to_thread(_sync_rebuild, index_dir, [], [])
            return 0

        # Read after acquiring the lock and rebuild from that snapshot
        # (no concurrent add is missed).
        text = jsonl_path.read_text(encoding="utf-8")
        records = parse_records(text)

        if not records:
            await asyncio.to_thread(_sync_rebuild, index_dir, [], [])
            return 0

        # Inject event loop for usage tracking
        try:
            loop = asyncio.get_running_loop()
            _set_embedder_loop(embedder, loop)
        except RuntimeError:
            pass

        record_dicts: list[dict[str, object]] = []
        vectors: list[list[float]] = []
        for r in records:
            # Poison-record isolation: embed one record at a time.
            # A single failure still allows others to be indexed (finding 4).
            try:
                async with _get_embed_sem():
                    vecs = await asyncio.to_thread(embedder.embed_batch, [r.content])
            except Exception:
                logger.warning(
                    "rebuild_memory_index: embed failed for record=%s (project=%s); "
                    "skipping from index (kept in jsonl)",
                    r.id,
                    project_id,
                    exc_info=True,
                )
                continue
            record_dicts.append(
                {
                    "record_id": r.id,
                    "content": r.content,
                    "category": r.category,
                    "epic_id": r.epic_id,
                    "task_id": r.task_id,
                    "repo": r.repo,
                    "created": r.created,
                }
            )
            vectors.append(vecs[0])

        await asyncio.to_thread(_sync_rebuild, index_dir, record_dicts, vectors)
        logger.info(
            "rebuild_memory_index: rebuilt %d/%d entries for project=%s",
            len(record_dicts),
            len(records),
            project_id,
        )
        return len(record_dicts)
