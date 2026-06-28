"""Shared response helpers for Strands tool return values.

All agent tools should return dicts that include ``"status"`` and
``"content"`` so that ``StreamTranslator`` (translator.py:153-175)
correctly propagates error status through ``ToolResultEvent.status``.

Existing metadata keys (``"ok"``, ``"name"``, ``"role"``, ``"profiles"``,
``"skills"``, ``"filename"`` etc.) are preserved alongside ``status`` /
``content`` so that callers and tests that read those keys are unaffected.

Usage::

    return make_success("Wrote file.", filename="foo.md")
    # → {"status": "success", "content": [{"text": "Wrote file."}], "filename": "foo.md"}

    return make_error("Not found: bar")
    # → {"status": "error", "content": [{"text": "Not found: bar"}]}
"""

from __future__ import annotations

from typing import Any


def make_success(text: str, **metadata: Any) -> dict[str, Any]:
    """Build a ``{status, content, **metadata}`` success response.

    Args:
        text: Human-readable success message (visible to the LLM as content).
        **metadata: Additional keys to include alongside ``status`` / ``content``
            (e.g. ``name=...``, ``ok=True``, ``filename=...``).

    Returns:
        ``{"status": "success", "content": [{"text": text}], **metadata}``
    """
    return {"status": "success", "content": [{"text": text}], **metadata}


def make_error(text: str, **metadata: Any) -> dict[str, Any]:
    """Build a ``{status, content, error, **metadata}`` error response.

    The ``"error"`` key is always included (set to *text*) so that callers
    can do ``"error" in result`` to detect failures without checking
    ``result["status"]``.  Callers may override it via ``error=...`` in
    *metadata* if a different error string is needed.

    Args:
        text: Human-readable error message (visible to the LLM as content).
        **metadata: Additional keys alongside ``status`` / ``content`` /
            ``error`` (e.g. ``ok=False``, ``name=...``).

    Returns:
        ``{"status": "error", "content": [{"text": text}], "error": text, **metadata}``
    """
    return {"status": "error", "content": [{"text": text}], "error": text, **metadata}
