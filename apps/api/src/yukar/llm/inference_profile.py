"""Bedrock application inference profile → foundation model ID resolver.

Application inference profile ARNs (e.g.
``arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/<id>``)
do not contain pricing-table keys (``opus-4-8`` etc.), so
:func:`~yukar.usage.pricing.get_pricing` returns ``None`` and cost is recorded as 0.

This module calls the Bedrock control-plane API ``bedrock.get_inference_profile``
to resolve the foundation model ID referenced by the profile, correcting cost attribution.

Design principles:
- **fast path**: if :func:`~yukar.usage.pricing.get_pricing` already recognises
  ``model_id``, return it immediately without any network call.
- **in-process cache**: resolution results are cached per ARN to reduce duplicate
  calls to one. Cache hits require only a synchronous dict lookup (no ``to_thread``
  needed), keeping the hot path lightweight.
- **best-effort**: all exceptions (insufficient permissions, network errors, etc.)
  are swallowed and the original model_id is returned. No regression.
- boto3 is lazily imported inside the function (avoids auth checks at import time —
  same policy as the embedder).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from yukar.usage.pricing import get_pricing

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-process cache (module-level)
# ---------------------------------------------------------------------------

_resolved_cache: dict[str, str] = {}
"""Cache of ARN / profile ID → resolved model ID."""

_resolve_lock = asyncio.Lock()
"""Lock that serialises resolution of uncached ARNs."""

_warned: set[str] = set()
"""Set of ARNs already warned about, so each resolution-failure warning is logged only once."""


# ---------------------------------------------------------------------------
# Internal: synchronous boto3 calls
# ---------------------------------------------------------------------------


def _extract_region_from_arn(arn: str) -> str | None:
    """Return the third field (region) of an ARN, or ``None`` if it cannot be extracted.

    Example: ``arn:aws:bedrock:us-east-1:123...`` → ``"us-east-1"``

    Args:
        arn: A Bedrock resource ARN string.

    Returns:
        The region string, or ``None`` if parsing fails.
    """
    parts = arn.split(":")
    if len(parts) >= 4 and parts[3]:
        return parts[3]
    return None


def _resolve_blocking(model_id: str, region: str | None) -> str:
    """Resolve a foundation model ID from an inference profile using boto3 (synchronous).

    Called from :func:`resolve_model_id_for_pricing` via ``asyncio.to_thread``.

    Args:
        model_id: A Bedrock inference profile ARN or profile ID.
        region: Region name to pass to the boto3 client. When ``None``, falls back to
            boto3 standard resolution (environment variable ``AWS_REGION``, etc.).

    Returns:
        The resolved foundation model ID (suitable for pricing lookup), or *model_id*
        unchanged on failure.
    """
    # If the ARN contains an embedded region, use it preferentially
    if region is None and model_id.startswith("arn:"):
        region = _extract_region_from_arn(model_id)

    try:
        import boto3  # noqa: PLC0415 — lazy import (avoids auth checks at import time)

        client: Any = boto3.client("bedrock", region_name=region)
        resp: dict[str, Any] = client.get_inference_profile(inferenceProfileIdentifier=model_id)
        models: list[dict[str, Any]] = resp.get("models") or []
        if not models:
            return model_id

        model_arn: str = models[0].get("modelArn", "")
        if "foundation-model/" in model_arn:
            fm = model_arn.split("foundation-model/")[-1]
        else:
            fm = model_arn

        if fm and get_pricing(fm) is not None:
            logger.info(
                "Resolved inference profile %r → foundation model %r",
                model_id,
                fm,
            )
            return fm

        # Resolved successfully but not in the pricing table — return the original model_id
        return model_id

    except Exception:  # noqa: BLE001
        if model_id not in _warned:
            _warned.add(model_id)
            logger.warning(
                "Failed to resolve inference profile %r via bedrock.get_inference_profile"
                " — cost will be recorded as 0.0 for this model.",
                model_id,
            )
        return model_id


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def resolve_model_id_for_pricing(
    model_id: str,
    *,
    region: str | None,
    provider_is_bedrock: bool,
) -> str:
    """Resolve an inference profile ARN to a foundation model ID suitable for pricing lookup.

    When the pricing table already recognises ``model_id`` (system / cross-region inference
    profiles or plain model names), return it immediately without any network call (fast path).

    Args:
        model_id: The value returned by the Strands agent's ``get_config()["model_id"]``.
        region: Region name to pass to the boto3 client. When ``None``, falls back to boto3
            standard resolution or automatic extraction from the ARN.
        provider_is_bedrock: When ``False``, return immediately without calling the Bedrock API.

    Returns:
        The model ID string to use for pricing lookup. Returns *model_id* unchanged on failure.
    """
    # Detect application inference profile ARNs.
    # If the opaque ID portion of the ARN happens to contain a pricing-key substring
    # like "haiku", we skip the fast path to avoid applying the wrong pricing rate.
    # System / cross-region inference profiles ("us.anthropic.claude-…" etc.) do not
    # contain "application-inference-profile", so they pass through the fast path naturally.
    is_app_profile = "application-inference-profile" in model_id.lower()

    # Non-Bedrock providers do not call the control plane (same for app inference profiles)
    if not provider_is_bedrock:
        return model_id

    # Fast path: already known in the pricing table → return immediately (no network call).
    # Application inference profile ARNs always bypass the fast path and proceed to resolution.
    if not is_app_profile and get_pricing(model_id) is not None:
        return model_id

    # Cache hit (synchronous dict lookup only)
    cached = _resolved_cache.get(model_id)
    if cached is not None:
        return cached

    # Cache miss: re-check under lock → perform actual resolution
    async with _resolve_lock:
        # Re-check after acquiring the lock (prevents double resolution)
        cached = _resolved_cache.get(model_id)
        if cached is not None:
            return cached

        resolved = await asyncio.to_thread(_resolve_blocking, model_id, region)
        _resolved_cache[model_id] = resolved
        return resolved
