"""Shared helper functions for agent streaming.

Extracted from :mod:`~yukar.agents.streaming` for readability.  All public
names continue to be importable from ``yukar.agents.streaming`` via the
package ``__init__.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from strands import Agent

from yukar.usage.tracker import UsageDelta

_UsageSnapshot = tuple[int, int, int, int]


def extract_final_text(agent: Agent) -> str:
    """Extract the last assistant message text from ``agent.messages``.

    Shared by worker, evaluator, and resolve_runner — all three previously
    contained byte-identical copies of this logic.
    """
    for msg in reversed(agent.messages):
        if msg.get("role") == "assistant":
            content = msg.get("content", [])
            parts = [b.get("text", "") for b in content if isinstance(b, dict) and "text" in b]
            return " ".join(parts).strip()
    return ""


def resolve_model_id(model: Any) -> str:
    """Return the configured model ID, with legacy attribute fallback."""
    try:
        config = model.get_config()
        model_id = config.get("model_id")
        if model_id:
            return str(model_id)
    except Exception:  # noqa: BLE001
        pass
    return str(getattr(model, "_model_id", "unknown"))


def usage_snapshot(agent: Agent) -> _UsageSnapshot:
    """Return a (input, output, cache_read, cache_write) token snapshot."""
    usage = agent.event_loop_metrics.accumulated_usage
    return (
        int(usage.get("inputTokens", 0)),
        int(usage.get("outputTokens", 0)),
        int(usage.get("cacheReadInputTokens", 0)),
        int(usage.get("cacheWriteInputTokens", 0)),
    )


def usage_delta(previous: _UsageSnapshot, current: _UsageSnapshot) -> UsageDelta:
    """Compute the token delta between two snapshots."""
    return UsageDelta(
        input_tokens=max(0, current[0] - previous[0]),
        output_tokens=max(0, current[1] - previous[1]),
        cache_read_tokens=max(0, current[2] - previous[2]),
        cache_write_tokens=max(0, current[3] - previous[3]),
    )


def is_zero_delta(delta: UsageDelta) -> bool:
    """Return True if all token counts in *delta* are zero."""
    return (
        delta.input_tokens == 0
        and delta.output_tokens == 0
        and delta.cache_read_tokens == 0
        and delta.cache_write_tokens == 0
    )


def is_budget_enforcement_active() -> bool:
    """Return True if the global tracker is actively enforcing a budget breach."""
    try:
        from yukar.usage.tracker import get_tracker

        tracker = get_tracker()
        active = getattr(tracker, "is_budget_enforcement_active", None)
        if callable(active):
            return bool(active())
        # Compatibility for simple test doubles and older tracker instances.
        return bool(tracker.is_over_budget())
    except Exception:
        return False
