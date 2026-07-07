"""Token pricing table — USD per 1M tokens.

Sources:
  - Anthropic official pricing page (https://www.anthropic.com/pricing) as of 2026-06.
  - AWS Bedrock pricing (Claude on Bedrock uses the same first-party rates;
    cache_write = 1.25x base input, cache_read = 0.1x base input).
  - Fable 5 (Anthropic official pricing, 2026-06 time point):
      input=$10/1M, output=$50/1M, cache_write=$12.50/1M, cache_read=$1.00/1M.

Matching strategy: model IDs are matched by partial substring.  The first
entry whose key is found in the model id wins.  Unknown models are assigned
zero cost and a warning is logged.

Model id examples that resolve correctly:
  - "claude-sonnet-5"             → sonnet-5 entry
  - "claude-sonnet-4-6"            → sonnet-4-6 entry
  - "anthropic.claude-sonnet-4-6-20250514-v1:0" → sonnet-4-6 entry
  - "us.anthropic.claude-opus-4-7-20250514-v1:0" → opus-4-7 entry
  - "claude-fable-5"              → fable-5 entry
  - "amazon.titan-embed-text-v2:0" → titan-embed-text-v2 entry

Ordering note: "sonnet-5" is not a substring of any "sonnet-4-*" id and vice
versa, so the Sonnet 5 / Sonnet 4.x entries never collide regardless of order.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModelPricing:
    """Per-model pricing in USD per 1,000,000 tokens."""

    input: float
    output: float
    cache_write: float = 0.0  # cache write (5 min TTL); 0 if unsupported
    cache_read: float = 0.0  # cache read; 0 if unsupported


# ---------------------------------------------------------------------------
# Pricing table — order matters: first match wins.
# ---------------------------------------------------------------------------

# Shared pricing constant for the Opus 4 new-series variants (4-5 through 4-8).
# All have identical rates as of 2026-06.
_OPUS_4_NEW = ModelPricing(input=5.0, output=25.0, cache_write=6.25, cache_read=0.50)

# USD per 1M tokens (Anthropic / AWS Bedrock, 2026-06)
_PRICING_TABLE: list[tuple[str, ModelPricing]] = [
    # Opus 4 variants (new series) — all share the same rate
    ("opus-4-8", _OPUS_4_NEW),
    ("opus-4-7", _OPUS_4_NEW),
    ("opus-4-6", _OPUS_4_NEW),
    ("opus-4-5", _OPUS_4_NEW),
    # Opus 4 legacy
    (
        "opus-4-1",
        ModelPricing(input=15.0, output=75.0, cache_write=18.75, cache_read=1.50),
    ),
    # Sonnet 5 — standard rate $3/$15 per 1M (same as Sonnet 4.6).  An
    # introductory discount of $2/$10 applies through 2026-08-31; it is
    # deliberately NOT encoded here because this table has no time logic and
    # would silently go stale after that date.  Standard rates also keep cost
    # estimates conservative (the budget stop-gate over- rather than
    # under-counts spend during the intro window).
    (
        "sonnet-5",
        ModelPricing(input=3.0, output=15.0, cache_write=3.75, cache_read=0.30),
    ),
    # Sonnet 4 variants
    (
        "sonnet-4-6",
        ModelPricing(input=3.0, output=15.0, cache_write=3.75, cache_read=0.30),
    ),
    (
        "sonnet-4-5",
        ModelPricing(input=3.0, output=15.0, cache_write=3.75, cache_read=0.30),
    ),
    # Haiku 4
    (
        "haiku-4-5",
        ModelPricing(input=1.0, output=5.0, cache_write=1.25, cache_read=0.10),
    ),
    # Fable 5 (internal Anthropic model)
    (
        "fable-5",
        ModelPricing(input=10.0, output=50.0, cache_write=12.50, cache_read=1.00),
    ),
    # Titan embedding (input-only cost)
    (
        "titan-embed-text-v2",
        ModelPricing(input=0.02, output=0.0, cache_write=0.0, cache_read=0.0),
    ),
]


_MILLION = 1_000_000.0


def get_pricing(model_id: str) -> ModelPricing | None:
    """Return the :class:`ModelPricing` for *model_id*, or ``None`` if unknown.

    Matching is case-insensitive substring search; the first matching entry wins.

    Args:
        model_id: Any model identifier string (short name, Bedrock ARN, etc.).

    Returns:
        :class:`ModelPricing` if a match is found, otherwise ``None``.
    """
    lower = model_id.lower()
    for key, pricing in _PRICING_TABLE:
        if key in lower:
            return pricing
    return None


def compute_cost_usd(
    model_id: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    embedding_tokens: int = 0,
) -> float:
    """Return total cost in USD for the given token counts and model.

    Unknown models produce zero cost and log a warning once per model id (the
    caller is responsible for deduplication if needed).

    Args:
        model_id: Model identifier string.
        input_tokens: Regular (non-cached) input tokens.
        output_tokens: Output / completion tokens.
        cache_read_tokens: Cache-read input tokens.
        cache_write_tokens: Cache-write input tokens.
        embedding_tokens: Embedding input tokens (Titan).

    Returns:
        Total cost in USD as a float.
    """
    pricing = get_pricing(model_id)
    if pricing is None:
        logger.warning(
            "Unknown model %r — token usage recorded but cost set to 0.0",
            model_id,
        )
        return 0.0

    cost = (
        input_tokens * pricing.input / _MILLION
        + output_tokens * pricing.output / _MILLION
        + cache_read_tokens * pricing.cache_read / _MILLION
        + cache_write_tokens * pricing.cache_write / _MILLION
        + embedding_tokens * pricing.input / _MILLION
    )
    return cost
