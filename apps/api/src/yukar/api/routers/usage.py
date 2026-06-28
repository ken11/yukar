"""Usage router — token consumption tracking, USD cost, budget management.

Endpoints:
  GET  /api/usage        — global summary (cost, tokens, breakdown, budget)
  GET  /api/usage/stream — SSE stream of TokenUsageEvent / BudgetExceededEvent
  PUT  /api/usage/budget — set global USD monthly budget limit

Response shape (all collections are typed lists, no ``dict[str, Any]``):
  UsageSummaryResponse
    ├── by_project: list[ProjectUsageBreakdown]
    │     ├── epics: list[EpicUsageBreakdown]
    │     │     └── runs: list[RunUsageBreakdown]
    │     └── arbiter: ArbiterUsageBreakdown | None
    │           └── runs: list[RunUsageBreakdown]
    └── by_model: list[ModelUsageBreakdown]

Notes on arbiter bucket:
  - ``arbiter`` is present only when the project has at least one batch-merge run.
  - Arbiter costs are NOT included in any epic's totals.
  - Project-level totals = Σ epics + arbiter (so ``project.cost_usd`` is
    consistent with the sum of its children).
  - Global totals (``total_cost_*``, ``today``, ``this_month``, ``daily``,
    ``by_model``) include arbiter costs as before (real expenditure).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from yukar.deps import UsageTrackerDep

router = APIRouter(prefix="/api/usage", tags=["usage"])


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class BudgetSetRequest(BaseModel):
    """Request body for PUT /api/usage/budget."""

    limit_usd: float | None = Field(
        None,
        description="Monthly USD budget. null clears the limit.",
        ge=0,
    )


class ExchangeRateInfo(BaseModel):
    """Exchange rate metadata."""

    rate_jpy: float = Field(description="Current USD→JPY rate used for cost conversion.")
    fetched_at: str | None = Field(None, description="ISO-8601 timestamp of last rate fetch.")
    source: str = Field(description="'api', 'cache', or 'fallback'.")


class BudgetState(BaseModel):
    """Current monthly budget status (USD basis)."""

    limit_usd: float | None = Field(None, description="Monthly USD budget. null if unset.")
    spent_usd: float = Field(description="Month-to-date USD spend (JST calendar month).")
    remaining_usd: float | None = Field(None, description="Remaining USD budget. null if no limit.")
    over_budget: bool = Field(
        description="True when month-to-date spend >= monthly limit (all runs stopped)."
    )
    daily_budget_usd: float | None = Field(
        None, description="Daily budget = monthly limit / days in month. null if no limit."
    )
    daily_spent_usd: float = Field(0.0, description="USD spend for today (JST).")
    days_in_month: int = Field(description="Number of days in the current JST calendar month.")
    month_ratio: float | None = Field(
        None, description="Month spend ratio (spent/limit). null if no limit."
    )
    day_ratio: float | None = Field(
        None,
        description="Daily spend ratio (daily_spent/daily_budget). May exceed 1.0. null if no limit.",  # noqa: E501
    )


class RunUsageBreakdown(BaseModel):
    """Token usage totals for a single Run."""

    run_id: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    embedding_tokens: int
    total_tokens: int
    cost_usd: float
    cost_jpy: float
    started_at: str = Field(description="ISO-8601 timestamp when the run started.")
    updated_at: str = Field(description="ISO-8601 timestamp of the last usage update.")


class EpicUsageBreakdown(BaseModel):
    """Token usage totals for an Epic, with per-run breakdown."""

    epic_id: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    embedding_tokens: int
    total_tokens: int
    cost_usd: float
    cost_jpy: float
    runs: list[RunUsageBreakdown] = Field(default_factory=list)


class ArbiterUsageBreakdown(BaseModel):
    """Token usage totals for arbiter (batch-merge) runs — not tied to any epic.

    Present in ``ProjectUsageBreakdown.arbiter`` only when the project has at
    least one batch-merge run.  Absent (``null``) otherwise.
    """

    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    embedding_tokens: int
    total_tokens: int
    cost_usd: float
    cost_jpy: float
    runs: list[RunUsageBreakdown] = Field(default_factory=list)


class ProjectUsageBreakdown(BaseModel):
    """Token usage totals for a Project, with per-epic breakdown."""

    project_id: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    embedding_tokens: int
    total_tokens: int
    cost_usd: float
    cost_jpy: float
    epics: list[EpicUsageBreakdown] = Field(default_factory=list)
    arbiter: ArbiterUsageBreakdown | None = None


class ModelUsageBreakdown(BaseModel):
    """Token usage totals for a specific model, aggregated across all runs."""

    model_id: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    embedding_tokens: int
    cost_usd: float
    cost_jpy: float


class UsagePeriodTotals(BaseModel):
    """Aggregated token/cost totals for a time period (today or this month)."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    embedding_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    cost_jpy: float = 0.0


class UsageDailyPoint(BaseModel):
    """Token/cost totals for a single calendar day (JST)."""

    date: str = Field(description="Date in YYYY-MM-DD format (JST).")
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    embedding_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    cost_jpy: float = 0.0


class UsageSummaryResponse(BaseModel):
    """Response for GET /api/usage."""

    total_cost_usd: float
    total_cost_jpy: float
    total_input_tokens: int
    total_output_tokens: int
    total_cache_read_tokens: int
    total_cache_write_tokens: int
    total_embedding_tokens: int
    total_tokens: int
    exchange_rate: ExchangeRateInfo
    budget: BudgetState
    by_project: list[ProjectUsageBreakdown] = Field(
        default_factory=list,
        description="Per-project breakdown; each entry contains epic- and run-level details.",
    )
    by_model: list[ModelUsageBreakdown] = Field(
        default_factory=list,
        description="Per-model aggregated totals across all projects and runs.",
    )
    today: UsagePeriodTotals = Field(
        default_factory=UsagePeriodTotals,
        description="Aggregated token/cost totals for today (JST).",
    )
    this_month: UsagePeriodTotals = Field(
        default_factory=UsagePeriodTotals,
        description="Aggregated token/cost totals for the current month (JST).",
    )
    daily: list[UsageDailyPoint] = Field(
        default_factory=list,
        description="Daily breakdown for the current month (JST), date ascending.",
    )
    as_of_date: str = Field(
        default="",
        description="Server-side JST date (YYYY-MM-DD) used to compute today/this_month.",
    )


class BudgetSetResponse(BaseModel):
    limit_usd: float | None
    message: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=UsageSummaryResponse)
async def get_usage_summary(request: Request, tracker: UsageTrackerDep) -> UsageSummaryResponse:
    """Return global token usage summary including cost breakdown and budget state."""
    import dataclasses

    # Fetch exchange rate metadata.
    exchange_info = _get_exchange_info(request)

    summary = tracker.get_global_summary()
    budget_state = tracker.get_budget_state()

    # Field names on the dataclasses are 1:1 with the pydantic response models,
    # so model_validate(dataclasses.asdict(...)) eliminates the nested closures.
    return UsageSummaryResponse(
        total_cost_usd=summary.total_cost_usd,
        total_cost_jpy=summary.total_cost_jpy,
        total_input_tokens=summary.total_input_tokens,
        total_output_tokens=summary.total_output_tokens,
        total_cache_read_tokens=summary.total_cache_read_tokens,
        total_cache_write_tokens=summary.total_cache_write_tokens,
        total_embedding_tokens=summary.total_embedding_tokens,
        total_tokens=summary.total_tokens,
        exchange_rate=ExchangeRateInfo(
            rate_jpy=exchange_info["rate_jpy"],
            fetched_at=exchange_info.get("fetched_at"),
            source=exchange_info["source"],
        ),
        budget=BudgetState(
            limit_usd=budget_state["limit_usd"],
            spent_usd=budget_state["spent_usd"],
            remaining_usd=budget_state["remaining_usd"],
            over_budget=budget_state["over_budget"],
            daily_budget_usd=budget_state["daily_budget_usd"],
            daily_spent_usd=budget_state["daily_spent_usd"],
            days_in_month=budget_state["days_in_month"],
            month_ratio=budget_state["month_ratio"],
            day_ratio=budget_state["day_ratio"],
        ),
        by_project=[
            ProjectUsageBreakdown.model_validate(dataclasses.asdict(p)) for p in summary.by_project
        ],
        by_model=[
            ModelUsageBreakdown.model_validate(dataclasses.asdict(m)) for m in summary.by_model
        ],
        today=UsagePeriodTotals.model_validate(dataclasses.asdict(summary.today)),
        this_month=UsagePeriodTotals.model_validate(dataclasses.asdict(summary.this_month)),
        daily=[UsageDailyPoint.model_validate(dataclasses.asdict(b)) for b in summary.daily],
        as_of_date=summary.as_of_date,
    )


@router.get("/stream")
async def usage_stream(request: Request) -> StreamingResponse:
    """SSE stream of global usage events (TokenUsageEvent, BudgetExceededEvent).

    Delivers usage events spanning all projects and epics.  Intended for the
    Topbar and global dashboard to display real-time token cost without polling.

    No replay buffer: call ``GET /api/usage`` first to obtain current totals,
    then subscribe here for incremental updates.

    The stream exits when:
    - The client disconnects (``request.is_disconnected()`` returns True).
    - A ``None`` sentinel is published to the global usage queue.
    - A keepalive timeout fires every 15 s — disconnect is checked then.
    """
    from yukar.events import bus as event_bus
    from yukar.events.sse import disconnect_aware_sse, sse_response

    return sse_response(
        disconnect_aware_sse(
            event_bus.subscribe_usage(),
            request,
            poll_interval=1.0,
            keepalive_ticks=15,
        )
    )


@router.put("/budget", response_model=BudgetSetResponse)
async def set_budget(body: BudgetSetRequest, tracker: UsageTrackerDep) -> BudgetSetResponse:
    """Set or clear the global USD monthly spending limit."""
    await tracker.set_budget(body.limit_usd)
    msg = (
        f"Budget limit set to ${body.limit_usd} USD"
        if body.limit_usd is not None
        else "Budget limit cleared"
    )
    return BudgetSetResponse(limit_usd=body.limit_usd, message=msg)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _get_exchange_info(request: Request) -> dict[str, Any]:
    """Return exchange rate info from the app-state provider, or fallback."""
    exchange = getattr(request.app.state, "exchange_rate_provider", None)
    if exchange is not None:
        return exchange.get_rate_info()  # type: ignore[no-any-return]
    return {"rate_jpy": 155.0, "fetched_at": None, "source": "fallback"}
