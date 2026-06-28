"""SSE formatting helpers.

Format: `event: <type>\ndata: <json>\n\n`
Keep-alive: `: keep-alive\n\n`
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from typing import Any

from fastapi.responses import StreamingResponse

# SSE response headers shared by all event-stream endpoints.
_SSE_HEADERS: dict[str, str] = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
}


def format_event(event_type: str, data: Any) -> str:
    """Format a single SSE message."""
    payload = json.dumps(data) if not isinstance(data, str) else data
    return f"event: {event_type}\ndata: {payload}\n\n"


def format_keepalive() -> str:
    return ": keep-alive\n\n"


def run_event_to_sse(event: Any) -> str:
    """Convert a RunEvent pydantic model to SSE string."""
    if hasattr(event, "model_dump"):
        data = event.model_dump(mode="json")
        event_type = data.get("type", "event")
    else:
        data = event
        event_type = "event"
    return format_event(event_type, data)


def sse_response(gen: AsyncGenerator[str]) -> StreamingResponse:
    """Wrap an async string generator in a canonical SSE ``StreamingResponse``.

    Sets ``media_type``, ``Cache-Control``, and ``X-Accel-Buffering`` headers
    consistently across all four SSE endpoints.
    """
    return StreamingResponse(gen, media_type="text/event-stream", headers=_SSE_HEADERS)


async def disconnect_aware_sse(
    queue_cm: Any,
    request: Any,
    *,
    poll_interval: float = 1.0,
    keepalive_ticks: int = 15,
) -> AsyncGenerator[str]:
    """Subscribe to *queue_cm* and yield SSE strings until disconnect or sentinel.

    Shared logic for ``project_events_sse`` (runs.py) and ``usage_stream``
    (usage.py) — both had byte-identical subscribe→wait_for→disconnect→keepalive
    loops.

    Args:
        queue_cm: Async context manager that yields an ``asyncio.Queue``.
            Must support ``async with queue_cm as q`` where ``q.get()`` returns
            events; a ``None`` item signals end-of-stream.
        request: FastAPI ``Request`` object; ``request.is_disconnected()`` is
            polled on every timeout.
        poll_interval: Seconds between disconnect checks (default 1.0).
        keepalive_ticks: Emit a keepalive comment after this many consecutive
            poll timeouts without a real event (default 15 → every ~15 s).
    """
    async with queue_cm as q:
        ticks_since_keepalive = 0
        while True:
            try:
                event = await asyncio.wait_for(q.get(), timeout=poll_interval)
            except TimeoutError:
                if await request.is_disconnected():
                    break
                ticks_since_keepalive += 1
                if ticks_since_keepalive >= keepalive_ticks:
                    ticks_since_keepalive = 0
                    yield format_keepalive()
                continue

            if event is None:
                break

            ticks_since_keepalive = 0
            yield run_event_to_sse(event)

            if await request.is_disconnected():
                break
