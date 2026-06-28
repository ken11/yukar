"""Typed summary dataclasses and internal ledger data classes for usage tracking.

Extracted from :mod:`~yukar.usage.tracker` so that the large tracker module
stays focused on accumulation / persistence / budget logic while the pure data
shapes live here.

All names are re-exported from ``yukar.usage.tracker`` so existing imports are
unaffected.

Public names
------------
- :class:`DailyUsageBucket`
- :class:`PeriodTotals`
- :class:`RunUsageSummary`
- :class:`EpicUsageSummary`
- :class:`ProjectUsageSummary`
- :class:`ModelUsageSummary`
- :class:`GlobalUsageSummary`
- :class:`UsageDelta`
- :func:`as_int`, :func:`as_float` — coercion helpers
- :class:`RunTotals` — run accumulation class
- :data:`_TOKEN_FIELDS`, :func:`sum_fields` — summary helpers
- :func:`daily_bucket_to_dict`, :func:`daily_bucket_from_dict`

Internal names (private, not imported by tracker.py)
-----------------------------------------------------
- :class:`_ModelBreakdown` — per-(model_id, role) breakdown inside a run
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Robust coercion helpers for ledger deserialization
# ---------------------------------------------------------------------------


def as_int(v: Any) -> int:
    """Coerce *v* to int — returns 0 for None, empty string, or non-numeric values."""
    if v is None:
        return 0
    try:
        return int(v)
    except (ValueError, TypeError):
        return 0


def as_float(v: Any) -> float:
    """Coerce *v* to float — returns 0.0 for None, empty string, or non-numeric values."""
    if v is None:
        return 0.0
    try:
        return float(v)
    except (ValueError, TypeError):
        return 0.0


# ---------------------------------------------------------------------------
# Public dataclass: token delta for one increment call
# ---------------------------------------------------------------------------


@dataclass(slots=True, eq=False)
class UsageDelta:
    """Token counts for one increment call."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    embedding_tokens: int = 0


# ---------------------------------------------------------------------------
# Internal ledger data classes (plain dicts in ledger YAML)
# ---------------------------------------------------------------------------


@dataclass(slots=True, eq=False)
class _ModelBreakdown:
    """Per-(model_id, role) token counts accumulated inside one Run."""

    model_id: str
    role: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    embedding_tokens: int = 0
    cost_usd: float = 0.0

    def add(self, delta: UsageDelta, cost_usd: float) -> None:
        self.input_tokens += delta.input_tokens
        self.output_tokens += delta.output_tokens
        self.cache_read_tokens += delta.cache_read_tokens
        self.cache_write_tokens += delta.cache_write_tokens
        self.embedding_tokens += delta.embedding_tokens
        self.cost_usd += cost_usd

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "role": self.role,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "embedding_tokens": self.embedding_tokens,
            "cost_usd": self.cost_usd,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> _ModelBreakdown:
        obj = cls(
            model_id=str(d.get("model_id") or ""),
            role=str(d.get("role") or ""),
        )
        obj.input_tokens = as_int(d.get("input_tokens"))
        obj.output_tokens = as_int(d.get("output_tokens"))
        obj.cache_read_tokens = as_int(d.get("cache_read_tokens"))
        obj.cache_write_tokens = as_int(d.get("cache_write_tokens"))
        obj.embedding_tokens = as_int(d.get("embedding_tokens"))
        obj.cost_usd = as_float(d.get("cost_usd"))
        return obj


@dataclass(slots=True, eq=False)
class RunTotals:
    """Accumulated token counts and cost for a single Run."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    embedding_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    cost_jpy: float = 0.0
    # Breakdown by (model_id, role) — keyed as "{model_id}|{role}" internally.
    _by_model: dict[str, _ModelBreakdown] = field(default_factory=dict)
    # Metadata
    project_id: str = ""
    epic_id: str = ""
    run_id: str = ""
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def by_model(self) -> list[_ModelBreakdown]:
        """Ordered list of per-(model, role) breakdowns."""
        return list(self._by_model.values())

    def add(
        self,
        delta: UsageDelta,
        cost_usd: float,
        cost_jpy: float,
        role: str,
        model_id: str,
    ) -> None:
        """Accumulate token counts and cost from one usage increment.

        ``cost_usd`` and ``cost_jpy`` are the confirmed costs for this
        increment (not per-token rates).  ``cost_jpy`` is the sum of all
        cost_jpy accumulated for this run and is stored directly — it is NOT
        recomputed from ``cost_usd`` on read-back to avoid exchange-rate drift.
        """
        self.input_tokens += delta.input_tokens
        self.output_tokens += delta.output_tokens
        self.cache_read_tokens += delta.cache_read_tokens
        self.cache_write_tokens += delta.cache_write_tokens
        self.embedding_tokens += delta.embedding_tokens
        self.total_tokens = (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
            + self.embedding_tokens
        )
        self.cost_usd += cost_usd
        self.cost_jpy += cost_jpy
        self.updated_at = datetime.now(UTC)

        key = f"{model_id}|{role}"
        mb = self._by_model.get(key)
        if mb is None:
            mb = _ModelBreakdown(model_id=model_id, role=role)
            self._by_model[key] = mb
        mb.add(delta, cost_usd)

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "epic_id": self.epic_id,
            "run_id": self.run_id,
            "started_at": self.started_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "embedding_tokens": self.embedding_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": self.cost_usd,
            "cost_jpy": self.cost_jpy,
            "by_model": [mb.to_dict() for mb in self._by_model.values()],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> RunTotals:
        obj = cls()
        obj.project_id = d.get("project_id") or ""
        obj.epic_id = d.get("epic_id") or ""
        obj.run_id = d.get("run_id") or ""
        obj.input_tokens = as_int(d.get("input_tokens"))
        obj.output_tokens = as_int(d.get("output_tokens"))
        obj.cache_read_tokens = as_int(d.get("cache_read_tokens"))
        obj.cache_write_tokens = as_int(d.get("cache_write_tokens"))
        obj.embedding_tokens = as_int(d.get("embedding_tokens"))
        obj.total_tokens = as_int(d.get("total_tokens"))
        obj.cost_usd = as_float(d.get("cost_usd"))
        obj.cost_jpy = as_float(d.get("cost_jpy"))

        # v2 format: by_model is a list of {model_id, role, ...} dicts.
        raw_by_model = d.get("by_model")
        if isinstance(raw_by_model, list):
            for item in raw_by_model:
                mb = _ModelBreakdown.from_dict(item)
                key = f"{mb.model_id}|{mb.role}"
                obj._by_model[key] = mb
        else:
            # v1 migration: by_role is dict keyed as "role/model_id".
            raw_by_role: dict[str, Any] = d.get("by_role", {})
            for composite_key, v in raw_by_role.items():
                # Split "role/model_id" — model_id may contain "/" (Bedrock ARNs).
                parts = composite_key.split("/", 1)
                role = parts[0] if len(parts) > 0 else ""
                model_id = parts[1] if len(parts) > 1 else composite_key
                mb = _ModelBreakdown.from_dict({"model_id": model_id, "role": role, **v})
                key = f"{model_id}|{role}"
                obj._by_model[key] = mb

        started_at = d.get("started_at")
        if started_at:
            try:
                obj.started_at = datetime.fromisoformat(str(started_at))
            except (ValueError, TypeError):
                logger.warning(
                    "Usage ledger: invalid started_at %r for run %r; using now(UTC)",
                    started_at,
                    obj.run_id,
                )
                obj.started_at = datetime.now(UTC)
        updated_at = d.get("updated_at")
        if updated_at:
            try:
                obj.updated_at = datetime.fromisoformat(str(updated_at))
            except (ValueError, TypeError):
                logger.warning(
                    "Usage ledger: invalid updated_at %r for run %r; using now(UTC)",
                    updated_at,
                    obj.run_id,
                )
                obj.updated_at = datetime.now(UTC)
        return obj


# ---------------------------------------------------------------------------
# Summary helper constants and functions
# ---------------------------------------------------------------------------

_TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "embedding_tokens",
    "total_tokens",
    "cost_usd",
    "cost_jpy",
)


def sum_fields(items: list[Any]) -> dict[str, Any]:
    """Sum all fields in ``_TOKEN_FIELDS`` across *items* in one pass."""
    totals: dict[str, Any] = {f: 0 for f in _TOKEN_FIELDS}
    for item in items:
        for f in _TOKEN_FIELDS:
            totals[f] += getattr(item, f, 0)
    return totals


def daily_bucket_to_dict(bucket: DailyUsageBucket) -> dict[str, Any]:
    return {f: getattr(bucket, f) for f in _TOKEN_FIELDS}


def daily_bucket_from_dict(date_key: str, d: dict[str, Any]) -> DailyUsageBucket:
    bucket = DailyUsageBucket(date=date_key)
    for f in _TOKEN_FIELDS:
        raw = d.get(f, 0)
        if f in ("cost_usd", "cost_jpy"):
            setattr(bucket, f, float(raw))
        else:
            setattr(bucket, f, int(raw))
    return bucket


# ---------------------------------------------------------------------------
# Typed summary dataclasses — returned by tracker.get_global_summary()
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class DailyUsageBucket:
    """Token/cost totals for a single calendar day (JST date)."""

    date: str  # "YYYY-MM-DD" in JST
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    embedding_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    cost_jpy: float = 0.0


@dataclass(slots=True)
class PeriodTotals:
    """Aggregated token/cost totals for an arbitrary time period."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    embedding_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    cost_jpy: float = 0.0


@dataclass(slots=True)
class RunUsageSummary:
    """Per-run token usage totals."""

    run_id: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    embedding_tokens: int
    total_tokens: int
    cost_usd: float
    cost_jpy: float
    started_at: str  # ISO-8601
    updated_at: str  # ISO-8601


@dataclass(slots=True)
class EpicUsageSummary:
    """Per-epic token usage totals with run-level breakdown."""

    epic_id: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    embedding_tokens: int
    total_tokens: int
    cost_usd: float
    cost_jpy: float
    runs: list[RunUsageSummary] = field(default_factory=list)


@dataclass(slots=True)
class ArbiterUsageSummary:
    """Token usage totals for arbiter (batch-merge) runs — not tied to any epic."""

    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    embedding_tokens: int
    total_tokens: int
    cost_usd: float
    cost_jpy: float
    runs: list[RunUsageSummary] = field(default_factory=list)


@dataclass(slots=True)
class ProjectUsageSummary:
    """Per-project token usage totals with epic-level breakdown."""

    project_id: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    embedding_tokens: int
    total_tokens: int
    cost_usd: float
    cost_jpy: float
    epics: list[EpicUsageSummary] = field(default_factory=list)
    arbiter: ArbiterUsageSummary | None = None


@dataclass(slots=True)
class ModelUsageSummary:
    """Per-model token usage totals aggregated across all runs."""

    model_id: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    embedding_tokens: int
    cost_usd: float
    cost_jpy: float


@dataclass(slots=True)
class GlobalUsageSummary:
    """Top-level summary returned by
    :meth:`~yukar.usage.tracker.TokenUsageTracker.get_global_summary`.
    """

    total_cost_usd: float
    total_cost_jpy: float
    total_input_tokens: int
    total_output_tokens: int
    total_cache_read_tokens: int
    total_cache_write_tokens: int
    total_embedding_tokens: int
    total_tokens: int
    by_project: list[ProjectUsageSummary] = field(default_factory=list)
    by_model: list[ModelUsageSummary] = field(default_factory=list)
    today: PeriodTotals = field(default_factory=PeriodTotals)
    this_month: PeriodTotals = field(default_factory=PeriodTotals)
    daily: list[DailyUsageBucket] = field(default_factory=list)
    as_of_date: str = ""  # "YYYY-MM-DD" in JST
