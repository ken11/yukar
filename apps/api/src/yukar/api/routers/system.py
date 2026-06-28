"""System router — server-side health/diagnostics accessible before SSE connects.

Endpoints:
  GET /api/system/status  — system health state (indexer watcher, etc.)

The watcher health is stored in ``app.state.indexer_health`` by the lifespan.
This router reads that state and returns it as a typed pydantic response so
that the frontend can poll once on initial load and surface any degraded state
via a toast — without relying on SSE (which connects after page render).
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/system", tags=["system"])


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class IndexerWatcherHealth(BaseModel):
    """Health state of the file-watcher component of the indexer.

    Fields:
      watch_enabled      cfg.indexer.watch — False means watcher was intentionally
                         disabled (not a degraded condition).
      watcher_ok         False only when an exception occurred during watcher
                         startup (repo enumeration failed or watcher.start()
                         raised).  When watch_enabled=False this is always True.
      reason             Human-readable reason for degraded state.  None when
                         watcher_ok=True.
      watched_repo_count Number of repos successfully registered with the watcher.
                         0 is normal when no repos have been indexed yet.
    """

    watch_enabled: bool = Field(description="True when cfg.indexer.watch is enabled.")
    watcher_ok: bool = Field(
        description="False only when an exception occurred during watcher startup."
    )
    reason: str | None = Field(
        None,
        description="Reason for degraded state.  None when watcher_ok=True.",
    )
    watched_repo_count: int = Field(
        0,
        description=(
            "Number of repos successfully registered with the watcher. "
            "0 is normal before any repo is indexed."
        ),
    )


class SystemStatusResponse(BaseModel):
    """Response for GET /api/system/status."""

    indexer_watcher: IndexerWatcherHealth = Field(
        description="Health state of the file-watcher component."
    )


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

# Default health used when app.state.indexer_health has not been set
# (e.g. in tests that bypass lifespan).  Conservative: treat as healthy so
# tests that do not exercise the lifespan do not see spurious failures.
_DEFAULT_HEALTH = IndexerWatcherHealth(
    watch_enabled=False,
    watcher_ok=True,
    reason=None,
    watched_repo_count=0,
)


@router.get("/status", response_model=SystemStatusResponse)
async def get_system_status(request: Request) -> SystemStatusResponse:
    """Return current system health state.

    Reads ``app.state.indexer_health`` set by the lifespan.  When the
    attribute is absent (tests that skip lifespan) a safe default (healthy,
    watch disabled) is returned so that callers always get a well-typed
    response.
    """
    health: IndexerWatcherHealth = getattr(
        request.app.state, "indexer_health", _DEFAULT_HEALTH
    )
    return SystemStatusResponse(indexer_watcher=health)
