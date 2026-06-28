"""Per-epic asyncio.Lock registry for manager-trial mutations.

Workers=1 / single event loop: this is a plain dict with asyncio.Lock values.
No cleanup of stale entries (the dict is bounded by the number of epics ever
touched in the process lifetime — acceptable for a long-running server).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

_locks: dict[tuple[str, str], asyncio.Lock] = {}


def _get_lock(project_id: str, epic_id: str) -> asyncio.Lock:
    key = (project_id, epic_id)
    if key not in _locks:
        _locks[key] = asyncio.Lock()
    return _locks[key]


@asynccontextmanager
async def epic_thread_lock(project_id: str, epic_id: str) -> AsyncGenerator[None]:
    """Async context manager: acquire the per-epic thread-mutation lock."""
    async with _get_lock(project_id, epic_id):
        yield
