"""Embedder — vector embedding protocol and implementations.

``Embedder`` is a ``typing.Protocol`` that every concrete embedder must satisfy.
Two implementations are provided:

- ``BedrockTitanEmbedder`` — calls Amazon Bedrock Titan Embed v2 (real, lazy-init).
- ``FakeEmbedder`` — deterministic hash-based embeddings for tests / local smoke-runs
  (no external calls).

The ``create_embedder`` factory reads ``EmbeddingSettings`` and returns the
appropriate implementation.  ``settings.embedding.provider`` now accepts
``"fake"`` in addition to ``"bedrock"``.

Concurrency
-----------
Both implementations are synchronous.  Callers that live in the asyncio event
loop should wrap ``embed_batch`` in ``asyncio.to_thread`` with a bounded
semaphore (e.g. ``_EMBED_SEM = asyncio.Semaphore(2)``).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import json
import logging
from typing import Any, Protocol, runtime_checkable

import numpy as np

logger = logging.getLogger(__name__)

FAKE_DIM: int = 128  # kept small so FAISS tests stay fast

# Maximum concurrent ``to_thread`` calls for ``embed_batch_async``.
# Bedrock Titan is single-text-per-request; 8 concurrent calls give a good
# throughput increase without exhausting the boto3 thread pool.
_EMBED_FANOUT: int = 8


def _record_embedding_usage_sync(
    model_id: str,
    project_id: str,
    epic_id: str,
    run_id: str | None,
    token_count: int,
    loop: asyncio.AbstractEventLoop | None = None,
) -> None:
    """Fire-and-forget usage recording for embedding calls.

    Embedding calls are synchronous and run in a thread pool via
    ``asyncio.to_thread``.  Calling ``asyncio.get_event_loop()`` from a worker
    thread raises ``RuntimeError`` in Python 3.10+ (and certainly in 3.14).
    Instead the caller captures ``asyncio.get_running_loop()`` on the main
    thread and passes it here; we schedule the coroutine via
    ``asyncio.run_coroutine_threadsafe``.

    Args:
        model_id: Model identifier for cost calculation.
        project_id: Project context for the usage ledger.
        epic_id: Epic context for the usage ledger.  Empty string for usage
            that is not tied to a specific epic (e.g. code-index builds).
            Passing the real epic_id is important: ``ensure_index_fresh`` runs
            before the Manager turn, so this embedding can become the run's
            first usage event and freeze ``RunTotals.epic_id``.
        run_id: Run context (falls back to ``"embedding"`` when ``None``).
        token_count: Number of embedding tokens to record.
        loop: The running event loop captured on the main thread.  When
            ``None`` the function is a no-op (loop not available).
    """
    if token_count <= 0:
        return
    if loop is None:
        logger.debug("Embedding usage: no event loop provided; skipping token recording")
        return
    try:
        from yukar.usage.tracker import UsageDelta, get_tracker

        tracker = get_tracker()
        delta = UsageDelta(embedding_tokens=token_count)

        async def _do_record() -> None:
            await tracker.record(
                project_id=project_id,
                epic_id=epic_id,
                run_id=run_id or "embedding",
                role="embedding",
                model_id=model_id,
                delta=delta,
            )

        future = asyncio.run_coroutine_threadsafe(_do_record(), loop)

        def _log_record_failure(fut: concurrent.futures.Future[None]) -> None:
            exc = fut.exception()
            if exc is not None:
                logger.warning("Embedding usage recording failed", exc_info=exc)

        future.add_done_callback(_log_record_failure)
    except Exception:
        logger.warning("Embedding usage tracking failed", exc_info=True)


@runtime_checkable
class Embedder(Protocol):
    """Protocol for text embedding providers.

    Any class that implements ``embed_batch`` and ``embed_batch_async``
    satisfies this protocol.
    """

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Return a list of embedding vectors, one per text.

        Args:
            texts: Input strings to embed.  May be empty.

        Returns:
            A list of float vectors of the same length as *texts*.
        """
        ...

    async def embed_batch_async(self, texts: list[str]) -> list[list[float]]:
        """Return a list of embedding vectors, one per text (async variant).

        Implementations should fan-out individual calls to avoid blocking the
        event loop.  The default/trivial implementation may simply delegate to
        ``embed_batch`` wrapped in ``asyncio.to_thread``.

        Args:
            texts: Input strings to embed.  May be empty.

        Returns:
            A list of float vectors of the same length as *texts*.
        """
        ...

    @property
    def dim(self) -> int:
        """Embedding dimension.  All vectors returned by ``embed_batch`` have this size."""
        ...


class FakeEmbedder:
    """Deterministic hash-based embedder for tests and local smoke-runs.

    Each text produces a fixed-length float32 vector derived from its SHA-256
    hash.  The mapping is deterministic: the same text always yields the same
    vector across processes.

    Similarity is *not* meaningful — this is purely for structural tests
    (e.g. "the top-1 hit for a query that equals an indexed chunk is that
    chunk itself").

    Args:
        dim: Embedding dimension (default ``FAKE_DIM``).
        project_id: Optional project context for usage tracking.
        epic_id: Optional epic context for usage tracking.
        run_id: Optional run context for usage tracking.
    """

    def __init__(
        self,
        dim: int = FAKE_DIM,
        project_id: str = "",
        epic_id: str = "",
        run_id: str | None = None,
    ) -> None:
        self._dim = dim
        self._project_id = project_id
        self._epic_id = epic_id
        self._run_id = run_id
        self._model_id = "fake-embedder"
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def dim(self) -> int:
        """Embedding dimension."""
        return self._dim

    def set_event_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Set the main-thread event loop for usage recording from worker threads."""
        self._loop = loop

    def set_context(self, project_id: str, run_id: str | None) -> None:
        """Override the (project_id, run_id) attribution for subsequent embeds.

        Used by the shared code-index embedder so that each ``embed_batch`` is
        attributed to the project actually being indexed (the embedder is a
        single shared instance, so its constructor defaults are not useful).
        ``epic_id`` is left as the constructor default ("" for index builds).
        """
        self._project_id = project_id
        self._run_id = run_id

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Return deterministic embeddings derived from SHA-256 hashes.

        Also records approximate token usage (chars / 4) for each text.

        Args:
            texts: Input strings.

        Returns:
            List of float lists, one per input text.
        """
        results: list[list[float]] = []
        total_approx_tokens = 0
        for text in texts:
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            # Repeat digest bytes to fill *dim* floats, then normalise to unit vector.
            raw = np.frombuffer(digest * ((self._dim // 32) + 2), dtype=np.uint8)[: self._dim]
            vec = raw.astype(np.float32) / 255.0  # range [0, 1]
            # Normalise to unit length so cosine-equivalent searches work.
            norm = np.linalg.norm(vec)
            if norm > 0:
                vec = vec / norm
            results.append(vec.tolist())
            # Approximate token count: chars / 4 (common rough estimate).
            total_approx_tokens += max(1, len(text) // 4)

        _record_embedding_usage_sync(
            model_id=self._model_id,
            project_id=self._project_id,
            epic_id=self._epic_id,
            run_id=self._run_id,
            token_count=total_approx_tokens,
            loop=self._loop,
        )
        return results

    async def embed_batch_async(self, texts: list[str]) -> list[list[float]]:
        """Async variant — delegates to ``embed_batch`` via ``asyncio.to_thread``.

        ``FakeEmbedder`` is CPU-only and fast, so a single thread call is
        sufficient.  This method exists to satisfy the ``Embedder`` Protocol.

        Args:
            texts: Input strings.

        Returns:
            List of float lists, one per input text.
        """
        return await asyncio.to_thread(self.embed_batch, texts)


class BedrockTitanEmbedder:
    """Amazon Bedrock Titan Embed Text v2 embedder.

    boto3 client is initialised lazily on first call to avoid import-time
    credential checks.

    Args:
        model_id: Bedrock model ID (default: ``amazon.titan-embed-text-v2:0``).
        region: AWS region.  ``None`` (default) defers to boto3 standard
            resolution order (``AWS_REGION`` env var, profile, instance metadata).
            Passing an explicit string overrides that resolution.
        dimensions: If not ``None``, include ``dimensions`` and
            ``normalize: true`` in the request body (Titan v2 feature).
            Leave as ``None`` to preserve compatibility with existing indexes.
        project_id: Optional project context for usage tracking.
        epic_id: Optional epic context for usage tracking.
        run_id: Optional run context for usage tracking.
    """

    # Dimension of Bedrock Titan Embed Text v2 output vectors.
    _TITAN_DIM: int = 1024

    def __init__(
        self,
        model_id: str = "amazon.titan-embed-text-v2:0",
        region: str | None = None,
        dimensions: int | None = None,
        project_id: str = "",
        epic_id: str = "",
        run_id: str | None = None,
    ) -> None:
        self._model_id = model_id
        self._region = region  # None → boto3 default region resolution
        self._dimensions = dimensions
        self._client: Any | None = None
        self._project_id = project_id
        self._epic_id = epic_id
        self._run_id = run_id
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def dim(self) -> int:
        """Embedding dimension (1024 for Titan Embed v2, or *dimensions* if set)."""
        return self._dimensions if self._dimensions is not None else self._TITAN_DIM

    def set_event_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Set the main-thread event loop for usage recording from worker threads."""
        self._loop = loop

    def set_context(self, project_id: str, run_id: str | None) -> None:
        """Override the (project_id, run_id) attribution for subsequent embeds.

        Used by the shared code-index embedder so that each ``embed_batch`` is
        attributed to the project actually being indexed (the embedder is a
        single shared instance, so its constructor defaults are not useful).
        ``epic_id`` is left as the constructor default ("" for index builds).
        """
        self._project_id = project_id
        self._run_id = run_id

    def _get_client(self) -> Any:
        if self._client is None:
            import boto3  # type: ignore[import-untyped]

            # region_name=None → boto3 resolves via AWS_REGION / profile / IMDS.
            self._client = boto3.client("bedrock-runtime", region_name=self._region)
        return self._client

    def _embed_one(self, client: Any, text: str) -> tuple[list[float], int]:
        """Embed a single *text* using *client* and return (vector, token_count).

        Synchronous — intended for use inside ``asyncio.to_thread``.

        Args:
            client: A boto3 ``bedrock-runtime`` client.
            text: Input string.

        Returns:
            A ``(embedding_vector, token_count)`` pair.  Returns a zero vector
            and 0 tokens for empty/whitespace-only input (Titan rejects those).
        """
        if not text.strip():
            logger.debug(
                "embed_batch: skipping empty/whitespace-only text; returning zero vector"
            )
            return [0.0] * self.dim, 0
        body_dict: dict[str, object] = {"inputText": text}
        if self._dimensions is not None:
            body_dict["dimensions"] = self._dimensions
            body_dict["normalize"] = True
        body = json.dumps(body_dict)
        response = client.invoke_model(
            modelId=self._model_id,
            body=body,
            contentType="application/json",
            accept="application/json",
        )
        payload = json.loads(response["body"].read())
        token_count = int(payload.get("inputTextTokenCount", 0))
        return payload["embedding"], token_count

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed *texts* using the Bedrock Titan model.

        Each text is sent as a separate API call (Titan v2 is single-text per
        request).  The caller is responsible for batching if throughput matters.

        Also records ``inputTextTokenCount`` from the Bedrock response for
        usage tracking.

        Args:
            texts: Input strings.

        Returns:
            List of float vectors.

        Raises:
            Exception: Propagates any boto3 / Bedrock error.
        """
        client = self._get_client()
        results: list[list[float]] = []
        total_tokens = 0
        for text in texts:
            vec, tok = self._embed_one(client, text)
            results.append(vec)
            total_tokens += tok

        if total_tokens > 0:
            _record_embedding_usage_sync(
                model_id=self._model_id,
                project_id=self._project_id,
                epic_id=self._epic_id,
                run_id=self._run_id,
                token_count=total_tokens,
                loop=self._loop,
            )
        return results

    async def embed_batch_async(self, texts: list[str]) -> list[list[float]]:
        """Embed *texts* concurrently using a semaphore-bounded fan-out.

        Launches up to ``_EMBED_FANOUT`` simultaneous ``asyncio.to_thread``
        calls (one per text) so that network round-trips to Bedrock overlap
        instead of running serially.  Usage is recorded once for the whole
        batch after all calls complete.

        Args:
            texts: Input strings.

        Returns:
            List of float vectors in the same order as *texts*.
        """
        if not texts:
            return []
        client = self._get_client()
        sem = asyncio.Semaphore(_EMBED_FANOUT)

        async def _bounded(text: str) -> tuple[list[float], int]:
            async with sem:
                return await asyncio.to_thread(self._embed_one, client, text)

        pairs = await asyncio.gather(*[_bounded(t) for t in texts])
        results = [vec for vec, _ in pairs]
        total_tokens = sum(tok for _, tok in pairs)
        if total_tokens > 0:
            _record_embedding_usage_sync(
                model_id=self._model_id,
                project_id=self._project_id,
                epic_id=self._epic_id,
                run_id=self._run_id,
                token_count=total_tokens,
                loop=self._loop,
            )
        return results


def create_embedder(
    settings: object,
    *,
    project_id: str = "",
    epic_id: str = "",
    run_id: str | None = None,
) -> Embedder:
    """Return an ``Embedder`` instance from ``EmbeddingSettings``.

    Args:
        settings: An ``EmbeddingSettings``-compatible object with ``provider``
            and ``model_id`` attributes.
        project_id: Project context for usage ledger attribution (C1).
        epic_id: Epic context for usage ledger attribution.  Pass the real
            epic_id for the per-run memory embedder so its embedding usage is
            attributed to the right epic (it runs before the Manager turn and
            would otherwise freeze the run's epic_id to "").
        run_id: Run context for usage ledger attribution (C1).
            Falls back to ``"embedding"`` inside the embedder when ``None``.

    Returns:
        A concrete ``Embedder`` instance.

    Raises:
        ValueError: If the provider is unknown.
    """
    from yukar.config.settings import EmbeddingSettings

    if not isinstance(settings, EmbeddingSettings):
        raise TypeError(f"Expected EmbeddingSettings, got {type(settings)}")

    provider = settings.provider
    if provider == "fake":
        return FakeEmbedder(project_id=project_id, epic_id=epic_id, run_id=run_id)
    if provider == "bedrock":
        return BedrockTitanEmbedder(
            model_id=settings.model_id,
            region=settings.region,
            dimensions=settings.dimensions,
            project_id=project_id,
            epic_id=epic_id,
            run_id=run_id,
        )
    raise ValueError(f"Unknown embedding provider: {provider!r}")
