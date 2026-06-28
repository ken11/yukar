"""ProjectMemoryStore — strands.memory.MemoryStore Protocol implementation.

Persists project-scoped cross-cutting knowledge (conventions, facts, past Epic lessons)
using FAISS + JSONL.

- extraction=False: explicit add only (no BeforeInvocation extraction).
- writable=True: written via the remember() tool / complete_epic learnings.
- search: query embed → FAISS top-k → MemoryEntry(content, metadata).
- add: run embed outside the lock → acquire lock → dedup check + append_record
  + index append → release (B1 fix: reduces lock hold time, prevents orphans).

B1: if embed fails, nothing is written to the source of truth.
    Return value: record_id (success) / None (duplicate) / raises (embed_failed).
    Callers can distinguish store.add returning None (duplicate) from an exception (embed_failed).
    The remember() tool translates embed_failed to {stored:false, reason:'embed_failed'}
    and reports it to the Manager.

B2: if IncrementalDimensionMismatchError is raised on a dimension mismatch,
    re-embed all entries via rebuild_memory_index, rebuild the index, then add the new record
    (same design as the update_index→save_index fallback in faiss_store).

B3: mtime check at __init__ → async rebuild launched via ensure_index_fresh.

Usage tracking:
    Inject the event loop into the embedder via _set_embedder_loop before embedding,
    so Titan usage tokens are recorded in the usage ledger.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from strands.memory.types import MemoryEntry, SearchOptions

from yukar.indexer.embedder import Embedder
from yukar.indexer.faiss_store import IncrementalDimensionMismatchError
from yukar.indexer.service import _get_embed_sem, _set_embedder_loop
from yukar.memory.index import (
    _memory_lock,
    index_consistency,
    search_index_memory,
)
from yukar.memory.index import _sync_add_to_index as _sync_add_to_index_unlocked
from yukar.memory.records import VALID_CATEGORIES, append_record, make_content_hash

logger = logging.getLogger(__name__)


class ProjectMemoryStore:
    """Project-scoped knowledge store that satisfies the strands.memory.MemoryStore Protocol.

    Attributes:
        name: Store identifier. Can be passed as the store argument to MemoryManager.search_memory.
        description: Description string embedded by the native search_memory tool.
        max_search_results: Default upper limit on search result count.
        writable: Writable (always True).
        extraction: Automatic extraction disabled (always False).
    """

    name: str = "project_memory"
    description: str = (
        "Cross-cutting knowledge for this project: repo conventions, facts, and lessons"
        " from past Epics. Searches design decisions, coding conventions, and learnings"
        " that persist across multiple Epics."
    )
    max_search_results: int = 5
    writable: bool = True
    extraction: bool = False

    def __init__(
        self,
        jsonl_path: Path,
        index_dir: Path,
        embedder: Embedder,
        *,
        project_id: str,
        epic_id: str = "-",
    ) -> None:
        self._jsonl_path = jsonl_path
        self._index_dir = index_dir
        self._embedder = embedder
        self._project_id = project_id
        self._epic_id = epic_id

    # ------------------------------------------------------------------
    # B3: mtime check at startup — lazy rebuild when index is stale or missing
    # ------------------------------------------------------------------

    async def ensure_index_fresh(self) -> None:
        """Rebuild if project.jsonl mtime > index mtime (or index is missing).

        Ensures that manual edits to project.jsonl are reflected in search/injection.
        Call at run startup.
        """
        faiss_path = self._index_dir / "faiss.index"
        if not self._jsonl_path.exists():
            return  # no jsonl → rebuild not needed

        jsonl_mtime = self._jsonl_path.stat().st_mtime
        if not faiss_path.exists():
            logger.info(
                "ensure_index_fresh: index missing for project=%s; rebuilding",
                self._project_id,
            )
            await self._rebuild()
            return

        # torn-write self-heal: faiss.index and chunks.jsonl are each atomically written,
        # but a crash between the two os.replace calls can leave ntotal != len(chunks).
        # mtime comparison cannot detect this inconsistency (both files have been written),
        # so we compare counts directly to force a rebuild independently of mtime.
        consistency = await asyncio.to_thread(index_consistency, self._index_dir)
        if consistency is None or consistency[0] != consistency[1]:
            detail = (
                "unreadable"
                if consistency is None
                else f"ntotal={consistency[0]} chunks={consistency[1]}"
            )
            logger.warning(
                "ensure_index_fresh: index inconsistent for project=%s (%s); rebuilding",
                self._project_id,
                detail,
            )
            await self._rebuild()
            return

        index_mtime = faiss_path.stat().st_mtime
        if jsonl_mtime > index_mtime:
            logger.info(
                "ensure_index_fresh: project.jsonl newer than index for project=%s; rebuilding",
                self._project_id,
            )
            await self._rebuild()

    async def _rebuild(self) -> None:
        """Internal helper that calls rebuild_memory_index.
        Uses a lazy import to avoid circular imports."""
        from yukar.memory.rebuild import rebuild_memory_index

        await rebuild_memory_index(
            self._jsonl_path,
            self._index_dir,
            self._embedder,
            project_id=self._project_id,
        )

    # ------------------------------------------------------------------
    # MemoryStore Protocol
    # ------------------------------------------------------------------

    async def search(self, query: str, options: SearchOptions | None = None) -> list[MemoryEntry]:
        """Embed query → FAISS top-k → return a list of MemoryEntry objects."""
        if not query.strip():
            return []

        max_results = self.max_search_results
        if options is not None:
            r = options.get("max_search_results")
            if r is not None:
                max_results = r

        self._inject_loop()
        try:
            async with _get_embed_sem():
                vectors = await asyncio.to_thread(self._embedder.embed_batch, [query])
        except Exception:
            logger.warning("ProjectMemoryStore.search: embed failed", exc_info=True)
            return []

        query_vec = vectors[0]

        try:
            hits = await search_index_memory(
                self._index_dir,
                query_vec,
                top_k=max_results,
                project_id=self._project_id,
            )
        except Exception:
            logger.warning("ProjectMemoryStore.search: FAISS search failed", exc_info=True)
            return []

        entries: list[MemoryEntry] = []
        for chunk, _dist in hits:
            content = chunk.get("content", "")
            meta: dict[str, Any] = {k: v for k, v in chunk.items() if k not in ("content",)}
            entries.append(MemoryEntry(content=content, store_name=self.name, metadata=meta))
        return entries

    async def add(self, content: str, metadata: dict[str, Any] | None = None) -> Any:
        """Embed content (outside the lock) → acquire lock → append_record + index append.

        B1: run embed outside the lock first; if embed fails, nothing is written to the
            source of truth.
            Returns None on duplicate. Raises EmbedFailedError on embed failure.
            Callers (the remember() tool) can distinguish None→'duplicate' from
            exception→'embed_failed'.

        B2: if IncrementalDimensionMismatchError is raised, call rebuild_memory_index from
            the source of truth to re-embed and rebuild all entries, then re-add the new record.

        normalize: content is stripped exactly once at the start and normalised to `stored`;
            both embed and persist use `stored`. append_record / rebuild also save and re-embed
            the same stripped text, so add and rebuild embed byte-identical text and vectors
            do not drift.
            dedup is unaffected by stripping because make_content_hash normalises further.

        Note: running embed outside the lock also reduces lock hold time.
              _project_locks not including root (low practical impact given workers=1)
              is preserved as-is.
        """
        stored = content.strip()
        if not stored:
            return None

        meta = metadata or {}
        category = meta.get("category", "fact")
        if category not in VALID_CATEGORIES:
            category = "fact"
        repo = meta.get("repo") or "-"
        epic_id = meta.get("epic_id") or self._epic_id
        task_id = meta.get("task_id") or "-"
        source = meta.get("source") or "remember"

        # Content hash can be computed before embed. The authoritative dedup is inside
        # append_record's lock, but we do a pre-lock dedup here to return early on
        # duplicates without paying the embed cost.
        candidate_hash = make_content_hash(stored)
        if await asyncio.to_thread(self._content_hash_exists, candidate_hash):
            logger.debug("ProjectMemoryStore.add: duplicate content skipped before embed")
            return None

        # B1: run embed outside the lock first (embed the stripped `stored` value).
        # If embed fails, nothing is written to the source of truth.
        self._inject_loop()
        try:
            async with _get_embed_sem():
                vectors = await asyncio.to_thread(self._embedder.embed_batch, [stored])
        except Exception as exc:
            logger.warning("ProjectMemoryStore.add: embed failed before lock", exc_info=True)
            raise EmbedFailedError(f"embed failed: {exc}") from exc

        vector = vectors[0]

        # Per-project lock (makes jsonl append + index append atomic)
        lock = _memory_lock(self._project_id)
        async with lock:
            # The authoritative dedup is append_record's within-lock check (return None on dup).
            # We do not repeat the same read+parse+O(N) hash scan on the add side.
            record = await append_record(
                self._jsonl_path,
                stored,
                category=category,
                epic_id=epic_id,
                task_id=task_id,
                repo=repo,
                source=source,
            )
            if record is None:
                # Duplicate detected by append_record's within-lock dedup
                # (includes post-pre-lock races)
                logger.debug("ProjectMemoryStore.add: duplicate detected by append_record")
                return None

            index_meta: dict[str, Any] = {
                "content": record.content,
                "category": record.category,
                "epic_id": record.epic_id,
                "task_id": record.task_id,
                "repo": record.repo,
                "created": record.created,
                "source": source,
            }
            # Lock is already held → call the internal function that does not acquire the lock
            try:
                await asyncio.to_thread(
                    _sync_add_to_index_unlocked,
                    self._index_dir,
                    record.id,
                    vector,
                    index_meta,
                )
            except IncrementalDimensionMismatchError:
                # B2: dimension mismatch → rebuild then add.
                # We are inside the lock, so rebuild must also use the internal path
                # that does not acquire the lock.
                # rebuild_index acquires the lock itself, so we call _sync_rebuild directly here.
                logger.warning(
                    "ProjectMemoryStore.add: dimension mismatch for project=%s; "
                    "rebuilding index from project.jsonl before adding record=%s",
                    self._project_id,
                    record.id,
                )
                await self._rebuild_under_lock()
            except Exception:
                # Index append failed (reason other than dimension mismatch). The record is
                # already appended to the source of truth (jsonl), so backdate faiss.index mtime
                # to be older than the source of truth so that the next ensure_index_fresh
                # (jsonl_mtime > index_mtime) always triggers a full rebuild
                # (_backdate_index_mtime self-repair).
                logger.warning(
                    "ProjectMemoryStore.add: index write failed for project=%s record=%s; "
                    "backdating index mtime to force rebuild next run",
                    self._project_id,
                    record.id,
                    exc_info=True,
                )
                await self._backdate_index_mtime()
                raise

            return record.id

    def _content_hash_exists(self, candidate_hash: str) -> bool:
        """Check whether the source of truth already contains content matching candidate_hash.

        Best-effort dedup. Call from outside the lock (for pre-lock early return).
        The authoritative dedup is held by append_record.
        Contains synchronous I/O (read + parse) — call via to_thread.
        """
        if not self._jsonl_path.exists():
            return False
        from yukar.memory.records import parse_records

        text = self._jsonl_path.read_text(encoding="utf-8")
        return any(r.content_hash() == candidate_hash for r in parse_records(text))

    async def _backdate_index_mtime(self) -> None:
        """Set faiss.index mtime to source-of-truth jsonl_mtime-1 to force a full rebuild next time.

        Self-repair when index append partially fails.
        Only applies when both faiss.index and the source of truth exist.
        """
        faiss_path = self._index_dir / "faiss.index"
        if faiss_path.exists() and self._jsonl_path.exists():
            jsonl_mtime = self._jsonl_path.stat().st_mtime
            await asyncio.to_thread(os.utime, faiss_path, (jsonl_mtime - 1, jsonl_mtime - 1))

    async def _rebuild_under_lock(self) -> None:
        """Perform a full rebuild from the source of truth inside the lock on dimension mismatch.

        B2: the new record is already in project.jsonl (appended by append_record), so a full
        rebuild that re-embeds all records per-record also indexes the new record.
        Do not re-add the new record separately after rebuild (avoids duplicate registration where
        ntotal != distinct count).
        The caller already holds the per-project lock, so we call _sync_rebuild directly
        (the version that does not acquire the lock).
        """
        from yukar.memory.index import _sync_rebuild

        # Re-embed all entries from the source of truth
        if self._jsonl_path.exists():
            from yukar.memory.records import parse_records as _parse

            text = self._jsonl_path.read_text(encoding="utf-8")
            records = _parse(text)
        else:
            records = []

        record_dicts: list[dict[str, Any]] = []
        vectors: list[list[float]] = []
        skipped = False

        if records:
            self._inject_loop()
            for r in records:
                # Poison-record isolation: embed one record at a time so that a single embed
                # failure (e.g. enlarged by manual editing) does not permanently break the entire
                # rebuild — skip+log. Skipped records are dropped from the index but remain in
                # the source of truth (jsonl); the subsequent mtime backdate causes the next full
                # rebuild to retry them.
                try:
                    async with _get_embed_sem():
                        vecs = await asyncio.to_thread(self._embedder.embed_batch, [r.content])
                except Exception:
                    logger.warning(
                        "ProjectMemoryStore._rebuild_under_lock: embed failed for record=%s; "
                        "skipping from index (kept in jsonl)",
                        r.id,
                        exc_info=True,
                    )
                    skipped = True
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

        # Full rebuild (lock already held, so call _sync_rebuild directly via to_thread).
        # B2 fix: project.jsonl already contains the new record (added by append_record),
        # so do not re-add the new record after rebuild (avoids duplicate registration).
        await asyncio.to_thread(_sync_rebuild, self._index_dir, record_dicts, vectors)

        if skipped:
            # Some records failed to embed and were dropped from the index. Backdate the
            # index mtime to be older than the source of truth so that the next
            # ensure_index_fresh (jsonl_mtime > index_mtime check) retries the full rebuild
            # (recovery from transient embed errors).
            await self._backdate_index_mtime()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _inject_loop(self) -> None:
        """Inject the event loop into the embedder so usage tokens are recorded in the ledger."""
        try:
            loop = asyncio.get_running_loop()
            _set_embedder_loop(self._embedder, loop)
        except RuntimeError:
            pass


class EmbedFailedError(RuntimeError):
    """Exception indicating that embed failed (B1).

    The remember() tool catches this and returns {stored: False, reason: 'embed_failed'}.
    """
