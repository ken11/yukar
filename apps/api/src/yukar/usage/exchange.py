"""USD → JPY exchange rate provider with 12-hour YAML cache.

Source: fawazahmed0/exchange-api (currency-api), CC0/public domain, no API key required.
  Primary:  https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@latest/v1/currencies/usd.json
  Fallback: https://latest.currency-api.pages.dev/v1/currencies/usd.json

Cache behaviour:
  - On first access, fetches live rate and persists to workspace YAML.
  - Subsequent accesses within 12 hours return the cached value.
  - On fetch failure: returns expired cached value if available, else fallback.
  - Cache file: ``<workspace_root>/usage/exchange_rate.yaml`` (via paths.py helper).

All writes go through :mod:`yukar.storage.atomic`.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

# Fallback rate used when no cache exists and the API is unreachable.
_FALLBACK_JPY = 155.0

# How long a cached rate is considered fresh.
_CACHE_TTL = timedelta(hours=12)

# currency-api endpoints (CC0/public domain, no auth required).
# Official guidance is to use jsDelivr as primary and Cloudflare Pages as fallback.
_API_URL_PRIMARY = (
    "https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@latest/v1/currencies/usd.json"
)
_API_URL_FALLBACK = "https://latest.currency-api.pages.dev/v1/currencies/usd.json"


class ExchangeRateProvider:
    """Fetches and caches USD→JPY exchange rates.

    Args:
        cache_path: Path to the YAML cache file (from ``paths.exchange_rate_yaml``).
    """

    def __init__(self, cache_path: Path, fetch_enabled: bool = True) -> None:
        self._cache_path = cache_path
        self._fetch_enabled = fetch_enabled
        # In-memory state (populated on first ``get_rate()`` call).
        self._rate: float = _FALLBACK_JPY
        self._fetched_at: datetime | None = None
        self._source: Literal["api", "cache", "fallback"] = "fallback"
        self._loaded: bool = False
        # Single-flight: concurrent first callers await one in-flight refresh
        # instead of each issuing their own HTTP fetch.
        self._refresh_task: asyncio.Task[None] | None = None

    async def get_rate(self) -> float:
        """Return the current USD→JPY rate, refreshing if stale.

        This method is safe to call concurrently (single event loop assumed).
        Concurrent callers that arrive while the rate is stale share a single
        in-flight refresh (single-flight) rather than each fetching.
        """
        await self._ensure_loaded()
        if self._is_stale():
            await self._refresh_single_flight()
        return self._rate

    async def _refresh_single_flight(self) -> None:
        """Run ``_refresh`` at most once for concurrent stale callers.

        The first caller creates the refresh task; later callers await the same
        task.  The task handle is cleared on completion so a future stale window
        triggers a fresh refresh.
        """
        task = self._refresh_task
        if task is None or task.done():
            task = asyncio.create_task(self._refresh())
            self._refresh_task = task
        try:
            await task
        finally:
            if self._refresh_task is task:
                self._refresh_task = None

    def get_rate_info(self) -> dict[str, object]:
        """Return metadata about the current rate.

        Returns:
            Dict with ``rate``, ``fetched_at`` (ISO string or None), and
            ``source`` (``"api"``, ``"cache"``, or ``"fallback"``).
        """
        return {
            "rate_jpy": self._rate,
            "fetched_at": self._fetched_at.isoformat() if self._fetched_at else None,
            "source": self._source,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _ensure_loaded(self) -> None:
        """Load persisted cache on first call."""
        if self._loaded:
            return
        self._loaded = True
        try:
            await asyncio.to_thread(self._load_cache)
        except Exception:
            logger.debug("exchange rate: could not load cache", exc_info=True)

    def _is_stale(self) -> bool:
        if self._fetched_at is None:
            return True
        # Guard against a naive ``_fetched_at`` (e.g. a legacy cache written
        # without a tz offset): subtracting a naive from an aware datetime
        # raises TypeError, which previously bubbled up and pinned the rate to
        # fallback forever (it could never re-fetch).  Coerce to UTC so the
        # staleness comparison — and therefore the periodic re-fetch — works.
        fetched_at = self._fetched_at
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=UTC)
        return datetime.now(UTC) - fetched_at > _CACHE_TTL

    def _load_cache(self) -> None:
        """Load rate from disk cache (synchronous — called once at startup)."""
        from yukar.storage.yaml_io import read_yaml

        data = read_yaml(self._cache_path)
        if not data:
            return
        rate = data.get("rate_jpy")
        fetched_at_str = data.get("fetched_at")
        if isinstance(rate, (int, float)) and fetched_at_str:
            try:
                fetched_at = datetime.fromisoformat(str(fetched_at_str))
            except (ValueError, TypeError):
                # Unparsable timestamp: keep the rate but force a re-fetch by
                # leaving _fetched_at None (rather than pinning to fallback).
                logger.debug("exchange rate: unparsable cached fetched_at %r", fetched_at_str)
                self._rate = float(rate)
                return
            # Normalise a naive timestamp to UTC so it is comparable in _is_stale.
            if fetched_at.tzinfo is None:
                fetched_at = fetched_at.replace(tzinfo=UTC)
            self._rate = float(rate)
            self._fetched_at = fetched_at
            self._source = "cache"

    async def _refresh(self) -> None:
        """Fetch a fresh rate from the API and persist to cache.

        When ``fetch_enabled`` is False, skip the external HTTP fetch entirely
        and rely on the in-memory value (cache or fallback).
        """
        if not self._fetch_enabled:
            logger.debug("exchange rate: fetch disabled; using cached/fallback rate")
            return
        try:
            rate = await asyncio.to_thread(self._fetch_blocking)
            self._rate = rate
            self._fetched_at = datetime.now(UTC)
            self._source = "api"
            await self._save_cache()
            logger.debug("exchange rate refreshed: %s JPY/USD", rate)
        except Exception:
            logger.warning(
                "exchange rate fetch failed; using %s (source=%s)",
                self._rate,
                self._source,
                exc_info=True,
            )
            # Keep existing rate / source; if fetched_at is None, mark as fallback.
            if self._fetched_at is None:
                self._source = "fallback"

    @staticmethod
    def _fetch_blocking() -> float:
        """Synchronous HTTP fetch — runs in thread pool.

        Tries the jsDelivr primary URL first; falls back to the Cloudflare Pages
        mirror if the primary raises any exception (connection failure, non-2xx,
        JSON parse error, or missing key).
        """
        import httpx

        def _get_jpy(url: str, client: httpx.Client) -> float:
            resp = client.get(url)
            resp.raise_for_status()
            data = resp.json()
            return float(data["usd"]["jpy"])

        with httpx.Client(timeout=10.0) as client:
            try:
                return _get_jpy(_API_URL_PRIMARY, client)
            except Exception:
                logger.debug("exchange rate: primary URL failed, trying fallback", exc_info=True)
                return _get_jpy(_API_URL_FALLBACK, client)

    async def _save_cache(self) -> None:
        from yukar.storage.yaml_io import write_yaml

        data = {
            "rate_jpy": self._rate,
            "fetched_at": self._fetched_at.isoformat() if self._fetched_at else None,
            "source": self._source,
        }
        await write_yaml(self._cache_path, data)
