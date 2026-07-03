"""Token usage tracker — singleton service for accumulating and persisting usage.

Responsibilities
----------------
1. Accept usage increments (LLM invocations, embedding calls) keyed by
   (project_id, epic_id, run_id, role, model_id).
2. Compute USD cost on the fly via :mod:`~yukar.usage.pricing`.
3. Convert to JPY via :class:`~yukar.usage.exchange.ExchangeRateProvider`.
4. Persist ledger to YAML (debounced 3-second write + immediate flush on run
   end / budget breach) via ``storage.atomic``.
5. Check budget on every increment and stop all running epics when breached.
6. Publish :class:`~yukar.models.events.TokenUsageEvent` to the SSE bus.

Invariants (CLAUDE.md)
----------------------
- Single event-loop instance — no threading.Lock needed (asyncio.Lock where
  required for async mutual exclusion).
- Paths go through ``config/paths.py``.
- YAML writes through ``storage/atomic.py``.
- DB-less: state lives in ledger YAML + in-memory.

Budget semantics (2026-06 onwards)
-----------------------------------
``limit_usd`` is interpreted as a **monthly budget** (JST calendar month).
Enforcement fires when the sum of ``cost_usd`` across all daily buckets in the
current JST calendar month reaches or exceeds ``limit_usd``.  There is no
manual reset — the window resets automatically at the start of each new month.

Backward-compat load
---------------------
Legacy ledgers written with ``limit_jpy`` (instead of ``limit_usd``) are
migrated on load: the value is divided by the fallback exchange rate (155 JPY/USD)
to produce an approximate USD figure, and a WARNING is logged.  If the key is
absent, the budget is left as None.

Ledger YAML format (v2, 2026-06)
---------------------------------
The ``by_role`` dict in run records (v1) is replaced with ``by_model`` — a list
of ``{model_id, role, ...token counts, cost_usd}`` entries.  Existing v1 ledger
files whose run records contain ``by_role`` keys are migrated on load: the
``role/model_id`` composite key is split back into separate fields.  No
compatibility shim is kept after loading — a flush will rewrite in v2 format.
"""

from __future__ import annotations

import asyncio
import calendar
import logging
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from yukar.usage.exchange import ExchangeRateProvider

from yukar.usage.ledger import (
    ArbiterUsageSummary,
    DailyUsageBucket,
    EpicUsageSummary,
    GlobalUsageSummary,
    ModelUsageSummary,
    PeriodTotals,
    ProjectUsageSummary,
    RunTotals,
    RunUsageSummary,
    UsageDelta,
    as_float,
    as_int,
    daily_bucket_from_dict,
    daily_bucket_to_dict,
    sum_fields,
)
from yukar.usage.pricing import compute_cost_usd

logger = logging.getLogger(__name__)

# Debounce delay (seconds) for ledger writes.
_DEBOUNCE_SECS: float = 3.0

# Sentinel value used as epic_id in the usage ledger for arbiter (batch-merge)
# runs.  This value never appears in epic storage — it is internal to the usage
# ledger only and segregates arbiter costs into a separate top-level bucket so
# they do not inflate any real epic's cost.
ARBITER_EPIC_SENTINEL = "__arbiter__"

__all__ = [
    "ARBITER_EPIC_SENTINEL",
    "ArbiterUsageSummary",
    "DailyUsageBucket",
    "EpicUsageSummary",
    "GlobalUsageSummary",
    "ModelUsageSummary",
    "PeriodTotals",
    "ProjectUsageSummary",
    "RunUsageSummary",
    "TokenUsageTracker",
    "UsageDelta",
    "get_tracker",
    "init_tracker",
]

_JST = ZoneInfo("Asia/Tokyo")


# ---------------------------------------------------------------------------
# Tracker
# ---------------------------------------------------------------------------


class TokenUsageTracker:
    """Singleton usage tracker — created in app lifespan and injected via deps.

    Args:
        ledger_path: Path to the global ledger YAML file.
        exchange: Exchange rate provider for USD→JPY conversion.
    """

    def __init__(
        self,
        ledger_path: Path,
        exchange: ExchangeRateProvider | None = None,
    ) -> None:
        self._ledger_path = ledger_path
        self._exchange = exchange
        # (project_id, run_id) → RunTotals.  Keying by the composite key (rather
        # than run_id alone) is defensive: a shared/synthetic run_id (e.g. the
        # code-index "index-<project_id>" run, or a future collision) cannot
        # cross-attribute usage between projects.
        self._runs: dict[tuple[str, str], RunTotals] = {}
        # Raw dicts for run records that failed deserialization — written back verbatim
        # on flush so they are never permanently lost.
        self._unparsed_runs: list[dict[str, Any]] = []
        # Set when the top-level ledger YAML could not be parsed at all (a raise
        # from read_yaml, not an empty {}).  While set, _write_ledger refuses to
        # auto-write so a single corrupt file is not overwritten with empty data.
        self._load_failed: bool = False
        # Daily buckets: "YYYY-MM-DD" (JST) → DailyUsageBucket
        self._daily: dict[str, DailyUsageBucket] = {}
        # Budget — monthly (JST calendar month), stored in USD
        self._limit_usd: float | None = None
        # Debounce
        self._dirty: bool = False
        self._flush_task: asyncio.Task[None] | None = None
        self._lock: asyncio.Lock = asyncio.Lock()
        # Serialises budget enforcement and marks one notification/stop per
        # breach episode.  The gate is re-armed by the monthly boundary (when
        # the new month's spending is below the limit) or a higher limit.
        self._budget_enforcement_lock: asyncio.Lock = asyncio.Lock()
        self._budget_breach_claimed: bool = False
        self._budget_enforcement_active: bool = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def load(self) -> None:
        """Load ledger from disk (call once at app startup).

        Individual corrupt run records are quarantined in ``_unparsed_runs`` and
        written back verbatim on the next flush so they are never permanently lost.
        A single broken record does not prevent subsequent records from loading.

        If the **top-level** YAML cannot be parsed at all (``read_yaml`` raises),
        the file is preserved: a ``_load_failed`` flag is set so that the next
        ``record()`` does not overwrite the unparsed file with an empty ledger
        (which would destroy all history, every quarantined run, and the budget),
        and a timestamped ``ledger.yaml.corrupt-<ts>`` copy is written aside for
        operator inspection.
        """
        from yukar.storage.yaml_io import read_yaml

        # Wrap ONLY read_yaml so we can distinguish a parse *raise* (corrupt
        # file → preserve) from a ``{}`` / falsy return (genuinely empty → fine).
        try:
            data = await asyncio.to_thread(read_yaml, self._ledger_path)
        except Exception:
            logger.error(
                "Usage ledger: top-level YAML is corrupt and could not be parsed; "
                "preserving the file and disabling auto-write until operator intervention",
                exc_info=True,
            )
            self._load_failed = True
            await asyncio.to_thread(self._quarantine_corrupt_ledger)
            return

        try:
            if not data:
                return
            # Load budget limit — prefer limit_usd (current); fall back to
            # legacy limit_jpy (divide by 155 JPY/USD fallback rate) with WARNING.
            if "limit_usd" in data:
                self._limit_usd = data.get("limit_usd")
            elif "limit_jpy" in data and data["limit_jpy"] is not None:
                legacy_jpy = data["limit_jpy"]
                _LEGACY_RATE = 155.0
                self._limit_usd = legacy_jpy / _LEGACY_RATE
                logger.warning(
                    "Usage ledger: loaded legacy limit_jpy=%.2f; "
                    "converted to limit_usd=%.4f using fallback rate %.1f JPY/USD. "
                    "Save the budget via PUT /api/usage/budget to persist in USD.",
                    legacy_jpy,
                    self._limit_usd,
                    _LEGACY_RATE,
                )
            else:
                self._limit_usd = None
            # reset_at / baseline_jpy are from the old cumulative-limit scheme.
            # They may exist in older ledger files — silently ignore them.

            # Reset state before populating so repeated load() calls are idempotent.
            self._unparsed_runs = []

            runs_raw: list[dict[str, Any]] = data.get("runs", [])
            for run_dict in runs_raw:
                run_id_hint = run_dict.get("run_id") if isinstance(run_dict, dict) else None
                try:
                    totals = RunTotals.from_dict(run_dict)
                    self._runs[(totals.project_id, totals.run_id)] = totals
                except Exception:
                    logger.warning(
                        "Usage ledger: failed to parse run record (run_id=%r); "
                        "quarantining for verbatim write-back",
                        run_id_hint,
                        exc_info=True,
                    )
                    if isinstance(run_dict, dict):
                        self._unparsed_runs.append(run_dict)

            # Load daily buckets — may be absent in older ledgers.
            # Use key-existence check (not truthiness) so that an explicitly
            # empty `daily: {}` in the YAML does not trigger re-migration from
            # runs, which would double-count tokens.
            if "daily" in data:
                daily_raw: dict[str, Any] = data.get("daily") or {}
                for date_key, bucket_dict in daily_raw.items():
                    self._daily[date_key] = daily_bucket_from_dict(date_key, bucket_dict)
            else:
                # Best-effort migration: place each run's totals into its
                # started_at JST date so existing data shows up in the chart.
                self._migrate_daily_from_runs()

            logger.info(
                "Usage ledger loaded: %d runs, %d unparsed, %d daily buckets",
                len(self._runs),
                len(self._unparsed_runs),
                len(self._daily),
            )
        except Exception:
            logger.warning("Failed to load usage ledger", exc_info=True)

    def _quarantine_corrupt_ledger(self) -> None:
        """Copy a corrupt top-level ledger aside so its bytes are never lost.

        Best-effort: failure to copy is logged but does not raise (the in-memory
        ``_load_failed`` flag already guarantees the original file is not
        overwritten).
        """
        try:
            if not self._ledger_path.exists():
                return
            ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            backup = self._ledger_path.with_name(f"{self._ledger_path.name}.corrupt-{ts}")
            backup.write_bytes(self._ledger_path.read_bytes())
            logger.warning("Usage ledger: corrupt file copied aside to %s", backup)
        except Exception:
            logger.warning("Usage ledger: could not copy corrupt file aside", exc_info=True)

    def _migrate_daily_from_runs(self) -> None:
        """Populate daily buckets from existing runs (best-effort; run start date in JST).

        Quarantined (unparsed) runs are included via their raw token fields so
        their tokens are not silently omitted from daily/global summaries — an
        unparsed ``by_model`` does not mean the run's top-level totals are
        unusable, and excluding them would undercount.
        """
        for rt in self._runs.values():
            jst_date = rt.started_at.astimezone(_JST).date().isoformat()
            bucket = self._daily.get(jst_date)
            if bucket is None:
                bucket = DailyUsageBucket(date=jst_date)
                self._daily[jst_date] = bucket
            bucket.input_tokens += rt.input_tokens
            bucket.output_tokens += rt.output_tokens
            bucket.cache_read_tokens += rt.cache_read_tokens
            bucket.cache_write_tokens += rt.cache_write_tokens
            bucket.embedding_tokens += rt.embedding_tokens
            bucket.total_tokens += rt.total_tokens
            bucket.cost_usd += rt.cost_usd
            bucket.cost_jpy += rt.cost_jpy

        # Include quarantined runs' top-level totals so their tokens still appear
        # in summaries (best-effort coercion of the raw dict fields).
        for raw in self._unparsed_runs:
            jst_date = self._unparsed_run_jst_date(raw)
            bucket = self._daily.get(jst_date)
            if bucket is None:
                bucket = DailyUsageBucket(date=jst_date)
                self._daily[jst_date] = bucket
            bucket.input_tokens += as_int(raw.get("input_tokens"))
            bucket.output_tokens += as_int(raw.get("output_tokens"))
            bucket.cache_read_tokens += as_int(raw.get("cache_read_tokens"))
            bucket.cache_write_tokens += as_int(raw.get("cache_write_tokens"))
            bucket.embedding_tokens += as_int(raw.get("embedding_tokens"))
            bucket.total_tokens += as_int(raw.get("total_tokens"))
            bucket.cost_usd += as_float(raw.get("cost_usd"))
            bucket.cost_jpy += as_float(raw.get("cost_jpy"))

    @staticmethod
    def _unparsed_run_jst_date(raw: dict[str, Any]) -> str:
        """Return the JST date for a quarantined run, falling back to today (JST)."""
        started_at = raw.get("started_at")
        if started_at:
            try:
                return datetime.fromisoformat(str(started_at)).astimezone(_JST).date().isoformat()
            except (ValueError, TypeError):
                pass
        return datetime.now(_JST).date().isoformat()

    # ------------------------------------------------------------------
    # Primary API
    # ------------------------------------------------------------------

    async def record(
        self,
        *,
        project_id: str,
        epic_id: str,
        run_id: str,
        role: str,
        model_id: str,
        delta: UsageDelta,
    ) -> None:
        """Record a usage increment and schedule a debounced ledger write.

        Also publishes a :class:`~yukar.models.events.TokenUsageEvent` to the
        SSE bus and checks the JPY budget.
        """
        jpy_rate = await self._get_jpy_rate()
        cost_usd = compute_cost_usd(
            model_id,
            input_tokens=delta.input_tokens,
            output_tokens=delta.output_tokens,
            cache_read_tokens=delta.cache_read_tokens,
            cache_write_tokens=delta.cache_write_tokens,
            embedding_tokens=delta.embedding_tokens,
        )
        cost_jpy = cost_usd * jpy_rate

        async with self._lock:
            run_key = (project_id, run_id)
            run_totals = self._runs.get(run_key)
            if run_totals is None:
                run_totals = RunTotals()
                run_totals.project_id = project_id
                run_totals.epic_id = epic_id
                run_totals.run_id = run_id
                self._runs[run_key] = run_totals
            run_totals.add(delta, cost_usd, cost_jpy, role, model_id)
            # Update daily bucket for today (JST).
            jst_date = datetime.now(_JST).date().isoformat()
            bucket = self._daily.get(jst_date)
            if bucket is None:
                bucket = DailyUsageBucket(date=jst_date)
                self._daily[jst_date] = bucket
            bucket.input_tokens += delta.input_tokens
            bucket.output_tokens += delta.output_tokens
            bucket.cache_read_tokens += delta.cache_read_tokens
            bucket.cache_write_tokens += delta.cache_write_tokens
            bucket.embedding_tokens += delta.embedding_tokens
            bucket.total_tokens += (
                delta.input_tokens
                + delta.output_tokens
                + delta.cache_read_tokens
                + delta.cache_write_tokens
                + delta.embedding_tokens
            )
            bucket.cost_usd += cost_usd
            bucket.cost_jpy += cost_jpy

        # Publish SSE event.
        self._publish_sse(
            project_id=project_id,
            epic_id=epic_id,
            run_id=run_id,
            role=role,
            model_id=model_id,
            delta=delta,
            run_totals=run_totals,
        )

        # Schedule debounced flush.
        self._schedule_flush()

        # Budget check.
        await self._check_budget(project_id, epic_id, run_id)

    async def flush(self) -> None:
        """Immediately write the ledger to disk (call on run end / budget breach)."""
        async with self._lock:
            await self._write_ledger()

    # ------------------------------------------------------------------
    # Budget management
    # ------------------------------------------------------------------

    async def set_budget(self, limit_usd: float | None) -> None:
        """Set or clear the global USD monthly spending limit."""
        async with self._lock:
            previous_limit = self._limit_usd
            self._limit_usd = limit_usd
            if limit_usd is None or (previous_limit is not None and limit_usd > previous_limit):
                self._budget_breach_claimed = False
            await self._write_ledger()
        if limit_usd is not None:
            project_id, epic_id, run_id = self._budget_trigger_context()
            await self._check_budget(project_id, epic_id, run_id)

    def is_over_budget(self) -> bool:
        """Return whether the current month's USD spend has reached the configured limit."""
        return self._limit_usd is not None and self._spent_this_month_usd() >= self._limit_usd

    def is_budget_enforcement_active(self) -> bool:
        """Return whether budget enforcement is currently stopping runs."""
        return self._budget_enforcement_active

    def _budget_trigger_context(self) -> tuple[str, str, str]:
        """Return context for enforcement caused directly by a budget change."""
        if self._runs:
            latest = next(reversed(self._runs.values()))
            return latest.project_id, latest.epic_id, latest.run_id
        return "usage", "budget", "budget-setting"

    def get_budget_state(self) -> dict[str, Any]:
        """Return current budget metadata (monthly-budget basis, USD)."""
        limit = self._limit_usd
        month_spent = self._spent_this_month_usd()
        day_spent = self._spent_today_usd()
        days_in_month = self._days_in_current_month()
        remaining = None if limit is None else max(0.0, limit - month_spent)
        daily_budget = None if limit is None else limit / days_in_month
        month_ratio = None if (limit is None or limit <= 0) else month_spent / limit
        day_ratio = (
            None if (daily_budget is None or daily_budget <= 0) else day_spent / daily_budget
        )
        return {
            "limit_usd": limit,
            "spent_usd": month_spent,
            "remaining_usd": remaining,
            "over_budget": (limit is not None and month_spent >= limit),
            "daily_budget_usd": daily_budget,
            "daily_spent_usd": day_spent,
            "days_in_month": days_in_month,
            "month_ratio": month_ratio,
            "day_ratio": day_ratio,
        }

    def _month_prefix(self) -> str:
        """Return the current JST month prefix in "%Y-%m" format."""
        return datetime.now(_JST).strftime("%Y-%m")

    def _spent_this_month_usd(self) -> float:
        """Return the total USD cost in the current JST calendar month."""
        prefix = self._month_prefix()
        return sum(b.cost_usd for k, b in self._daily.items() if k.startswith(prefix))

    def _spent_today_usd(self) -> float:
        """Return the USD cost for today (JST)."""
        today = datetime.now(_JST).date().isoformat()
        b = self._daily.get(today)
        return b.cost_usd if b is not None else 0.0

    def _days_in_current_month(self) -> int:
        """Return the number of days in the current JST calendar month."""
        now = datetime.now(_JST)
        return calendar.monthrange(now.year, now.month)[1]

    # ------------------------------------------------------------------
    # Summary / read API
    # ------------------------------------------------------------------

    def get_global_summary(self) -> GlobalUsageSummary:
        """Return typed global aggregated summary.

        Returns:
            :class:`GlobalUsageSummary` with fully-typed ``by_project`` and
            ``by_model`` breakdowns ready for the API response layer.
        """
        g = sum_fields(list(self._runs.values()))
        total_usd = g["cost_usd"]
        total_jpy = g["cost_jpy"]
        total_input = g["input_tokens"]
        total_output = g["output_tokens"]
        total_cache_read = g["cache_read_tokens"]
        total_cache_write = g["cache_write_tokens"]
        total_embed = g["embedding_tokens"]
        total_tokens = g["total_tokens"]

        # Breakdowns (by_project / by_model) are scoped to the CURRENT JST month
        # so the per-run list the UI renders cannot grow without bound.  The
        # all-time totals above (total_*) and the today/this_month/daily blocks
        # below are unaffected.
        #
        # A run is included when it was ACTIVE this month, i.e. its last usage
        # increment (updated_at) falls in the current JST month.  updated_at is
        # bumped on every record() (ledger add()), so this surfaces exactly the
        # runs still accumulating cost this month and drops stale ones.  Note:
        # a RunTotals aggregates a run's whole lifetime, so a run that straddles
        # a month boundary shows its full total here; that total can therefore
        # differ slightly from the strictly month-scoped this_month figure.
        now_jst = datetime.now(_JST)
        month_prefix = now_jst.strftime("%Y-%m")
        month_runs = [
            rt
            for rt in self._runs.values()
            if rt.updated_at.astimezone(_JST).strftime("%Y-%m") == month_prefix
        ]

        # Build by_project:
        #   epic_map:    project_id → epic_id → list[RunTotals]  (real epics only)
        #   arbiter_map: project_id → list[RunTotals]            (arbiter sentinel runs)
        project_epic_map: dict[str, dict[str, list[RunTotals]]] = defaultdict(
            lambda: defaultdict(list)
        )
        arbiter_map: dict[str, list[RunTotals]] = defaultdict(list)
        for rt in month_runs:
            if rt.epic_id == ARBITER_EPIC_SENTINEL:
                arbiter_map[rt.project_id].append(rt)
            else:
                project_epic_map[rt.project_id][rt.epic_id].append(rt)

        # Iterate over the union of both maps so that a project with ONLY arbiter
        # runs (no regular epic runs yet) is still included in the output.
        # Use dict.fromkeys to preserve first-seen insertion order (deterministic)
        # rather than set union which gives PYTHONHASHSEED-dependent ordering.
        all_project_ids = list(dict.fromkeys([*project_epic_map.keys(), *arbiter_map.keys()]))

        by_project: list[ProjectUsageSummary] = []
        for pid in all_project_ids:
            epic_map = project_epic_map.get(pid, {})
            epics: list[EpicUsageSummary] = []
            for eid, runs in epic_map.items():
                run_breakdowns = [
                    RunUsageSummary(
                        run_id=rt.run_id,
                        input_tokens=rt.input_tokens,
                        output_tokens=rt.output_tokens,
                        cache_read_tokens=rt.cache_read_tokens,
                        cache_write_tokens=rt.cache_write_tokens,
                        embedding_tokens=rt.embedding_tokens,
                        total_tokens=rt.total_tokens,
                        cost_usd=rt.cost_usd,
                        cost_jpy=rt.cost_jpy,
                        started_at=rt.started_at.isoformat(),
                        updated_at=rt.updated_at.isoformat(),
                    )
                    for rt in runs
                ]
                es = sum_fields(list(runs))
                epics.append(
                    EpicUsageSummary(
                        epic_id=eid,
                        input_tokens=es["input_tokens"],
                        output_tokens=es["output_tokens"],
                        cache_read_tokens=es["cache_read_tokens"],
                        cache_write_tokens=es["cache_write_tokens"],
                        embedding_tokens=es["embedding_tokens"],
                        total_tokens=es["total_tokens"],
                        cost_usd=es["cost_usd"],
                        cost_jpy=es["cost_jpy"],
                        runs=run_breakdowns,
                    )
                )

            # Build the arbiter bucket for this project (if any).
            arbiter_runs = arbiter_map.get(pid, [])
            arbiter_summary: ArbiterUsageSummary | None = None
            if arbiter_runs:
                arb_run_breakdowns = [
                    RunUsageSummary(
                        run_id=rt.run_id,
                        input_tokens=rt.input_tokens,
                        output_tokens=rt.output_tokens,
                        cache_read_tokens=rt.cache_read_tokens,
                        cache_write_tokens=rt.cache_write_tokens,
                        embedding_tokens=rt.embedding_tokens,
                        total_tokens=rt.total_tokens,
                        cost_usd=rt.cost_usd,
                        cost_jpy=rt.cost_jpy,
                        started_at=rt.started_at.isoformat(),
                        updated_at=rt.updated_at.isoformat(),
                    )
                    for rt in arbiter_runs
                ]
                arb_s = sum_fields(arbiter_runs)
                arbiter_summary = ArbiterUsageSummary(
                    input_tokens=arb_s["input_tokens"],
                    output_tokens=arb_s["output_tokens"],
                    cache_read_tokens=arb_s["cache_read_tokens"],
                    cache_write_tokens=arb_s["cache_write_tokens"],
                    embedding_tokens=arb_s["embedding_tokens"],
                    total_tokens=arb_s["total_tokens"],
                    cost_usd=arb_s["cost_usd"],
                    cost_jpy=arb_s["cost_jpy"],
                    runs=arb_run_breakdowns,
                )

            # Project total = sum of regular epics + arbiter (if present).
            # Combine into one sum_fields call to avoid per-field hand-coding.
            project_items: list[Any] = [*epics] + (
                [arbiter_summary] if arbiter_summary is not None else []
            )
            ps = sum_fields(project_items)

            by_project.append(
                ProjectUsageSummary(
                    project_id=pid,
                    input_tokens=ps["input_tokens"],
                    output_tokens=ps["output_tokens"],
                    cache_read_tokens=ps["cache_read_tokens"],
                    cache_write_tokens=ps["cache_write_tokens"],
                    embedding_tokens=ps["embedding_tokens"],
                    total_tokens=ps["total_tokens"],
                    cost_usd=ps["cost_usd"],
                    cost_jpy=ps["cost_jpy"],
                    epics=epics,
                    arbiter=arbiter_summary,
                )
            )

        # Build by_model: aggregate across all runs, keyed by model_id only
        # (role breakdown is available at the run level via by_model on RunTotals).
        model_agg: dict[str, ModelUsageSummary] = {}
        for rt in month_runs:
            for mb in rt.by_model:
                entry = model_agg.get(mb.model_id)
                if entry is None:
                    entry = ModelUsageSummary(
                        model_id=mb.model_id,
                        input_tokens=0,
                        output_tokens=0,
                        cache_read_tokens=0,
                        cache_write_tokens=0,
                        embedding_tokens=0,
                        cost_usd=0.0,
                        cost_jpy=0.0,
                    )
                    model_agg[mb.model_id] = entry
                entry.input_tokens += mb.input_tokens
                entry.output_tokens += mb.output_tokens
                entry.cache_read_tokens += mb.cache_read_tokens
                entry.cache_write_tokens += mb.cache_write_tokens
                entry.embedding_tokens += mb.embedding_tokens
                entry.cost_usd += mb.cost_usd
                # JPY is tracked per-run, not per-model; approximate by ratio.
                if rt.cost_usd > 0:
                    entry.cost_jpy += rt.cost_jpy * (mb.cost_usd / rt.cost_usd)
                # If run has no USD cost, cost_jpy stays 0 for this model entry.

        # Compute today / this_month / daily from the daily buckets.
        # now_jst / month_prefix were computed above for the breakdown scope.
        today_str = now_jst.date().isoformat()
        as_of_date = today_str

        today_totals = PeriodTotals()
        this_month_totals = PeriodTotals()
        month_daily: list[DailyUsageBucket] = []

        for date_key, bucket in self._daily.items():
            if date_key.startswith(month_prefix):
                month_daily.append(bucket)
                this_month_totals.input_tokens += bucket.input_tokens
                this_month_totals.output_tokens += bucket.output_tokens
                this_month_totals.cache_read_tokens += bucket.cache_read_tokens
                this_month_totals.cache_write_tokens += bucket.cache_write_tokens
                this_month_totals.embedding_tokens += bucket.embedding_tokens
                this_month_totals.total_tokens += bucket.total_tokens
                this_month_totals.cost_usd += bucket.cost_usd
                this_month_totals.cost_jpy += bucket.cost_jpy
                if date_key == today_str:
                    today_totals.input_tokens = bucket.input_tokens
                    today_totals.output_tokens = bucket.output_tokens
                    today_totals.cache_read_tokens = bucket.cache_read_tokens
                    today_totals.cache_write_tokens = bucket.cache_write_tokens
                    today_totals.embedding_tokens = bucket.embedding_tokens
                    today_totals.total_tokens = bucket.total_tokens
                    today_totals.cost_usd = bucket.cost_usd
                    today_totals.cost_jpy = bucket.cost_jpy

        month_daily.sort(key=lambda b: b.date)

        return GlobalUsageSummary(
            total_cost_usd=total_usd,
            total_cost_jpy=total_jpy,
            total_input_tokens=total_input,
            total_output_tokens=total_output,
            total_cache_read_tokens=total_cache_read,
            total_cache_write_tokens=total_cache_write,
            total_embedding_tokens=total_embed,
            total_tokens=total_tokens,
            by_project=by_project,
            by_model=list(model_agg.values()),
            today=today_totals,
            this_month=this_month_totals,
            daily=month_daily,
            as_of_date=as_of_date,
        )

    def get_run_totals(self, run_id: str, project_id: str | None = None) -> RunTotals | None:
        """Return the totals for *run_id*.

        ``self._runs`` is keyed by ``(project_id, run_id)``.  When *project_id*
        is given, look up the exact composite key; otherwise return the first
        run matching *run_id* across projects (back-compat for callers that key
        runs by id alone — run_ids are unique in practice).
        """
        if project_id is not None:
            return self._runs.get((project_id, run_id))
        for (_pid, rid), rt in self._runs.items():
            if rid == run_id:
                return rt
        return None

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _get_jpy_rate(self) -> float:
        if self._exchange is None:
            return 155.0
        try:
            return await self._exchange.get_rate()
        except Exception:
            return 155.0

    def _schedule_flush(self) -> None:
        self._dirty = True
        if self._flush_task is not None and not self._flush_task.done():
            return
        self._flush_task = asyncio.create_task(self._debounced_flush())

    async def _debounced_flush(self) -> None:
        await asyncio.sleep(_DEBOUNCE_SECS)
        async with self._lock:
            if self._dirty:
                await self._write_ledger()
                self._dirty = False

    async def _write_ledger(self) -> None:
        """Write ledger YAML — must be called with self._lock held.

        Parsed runs are serialized via ``to_dict()``.  Unparsed (corrupt) records
        are appended verbatim so they survive the round-trip and are not silently
        dropped.

        When the top-level ledger failed to parse on load (``_load_failed``), the
        write is refused: overwriting now would replace the unparsed file with an
        empty ledger, destroying all history.  The data is preserved on disk
        (and in the ``.corrupt-<ts>`` copy) until an operator intervenes.
        """
        if self._load_failed:
            logger.warning(
                "Usage ledger: refusing to write — load previously failed on a corrupt "
                "top-level file; preserving existing data until operator intervention"
            )
            return

        from yukar.storage.yaml_io import write_yaml

        daily_dict: dict[str, dict[str, Any]] = {
            date_key: daily_bucket_to_dict(bucket) for date_key, bucket in self._daily.items()
        }
        runs_out: list[dict[str, Any]] = [rt.to_dict() for rt in self._runs.values()]
        # Append verbatim copies of records that could not be parsed so they are
        # never permanently deleted from the ledger.
        runs_out.extend(self._unparsed_runs)
        data: dict[str, Any] = {
            "limit_usd": self._limit_usd,
            "updated_at": datetime.now(UTC).isoformat(),
            "runs": runs_out,
            "daily": daily_dict,
        }
        try:
            await write_yaml(self._ledger_path, data)
        except Exception:
            logger.warning("Failed to write usage ledger", exc_info=True)

    async def _check_budget(self, project_id: str, epic_id: str, run_id: str) -> None:
        """Stop all runs once for the current budget-breach episode."""
        async with self._budget_enforcement_lock:
            breach = await self._claim_budget_breach()
            if breach is None:
                return
            spent, limit = breach

            logger.warning(
                "Budget exceeded: spent $%.4f USD >= limit $%.4f USD; stopping all runs",
                spent,
                limit,
            )
            self._budget_enforcement_active = True
            try:
                await self.flush()
                await self._stop_all_runs(project_id, epic_id, run_id, spent, limit)
            finally:
                self._budget_enforcement_active = False

    async def _claim_budget_breach(self) -> tuple[float, float] | None:
        """Atomically claim enforcement for the current breach episode.

        Re-arms automatically when the current month's spending drops below the
        limit (i.e. at the start of a new calendar month, monthly spending resets
        to near zero so ``spent < limit`` and ``_budget_breach_claimed`` is cleared).
        """
        async with self._lock:
            limit = self._limit_usd
            if limit is None:
                self._budget_breach_claimed = False
                return None

            spent = self._spent_this_month_usd()
            if spent < limit:
                self._budget_breach_claimed = False
                return None
            if self._budget_breach_claimed:
                return None

            self._budget_breach_claimed = True
            return spent, limit

    async def _stop_all_runs(
        self,
        triggering_project_id: str,
        triggering_epic_id: str,
        triggering_run_id: str,
        spent_usd: float,
        limit_usd: float,
    ) -> None:
        """Stop all active runs via the RunSupervisor and publish notifications."""
        try:
            from yukar.events import bus as event_bus
            from yukar.models.events import BudgetExceededEvent
            from yukar.runs.supervisor import get_supervisor

            supervisor = get_supervisor()
            active_runs = await supervisor.list_active_runs_for_budget()

            for _root, pid, eid in active_runs:
                try:
                    await supervisor.stop(pid, eid)
                except Exception:
                    logger.warning("Budget stop: could not stop %s/%s", pid, eid, exc_info=True)

            # Publish budget exceeded event.
            budget_event = BudgetExceededEvent(
                project_id=triggering_project_id,
                epic_id=triggering_epic_id,
                run_id=triggering_run_id,
                spent_usd=spent_usd,
                limit_usd=limit_usd,
            )
            event_bus.publish(
                triggering_project_id,
                triggering_epic_id,
                budget_event,
            )
            # Also fan-out to the global usage stream.
            event_bus.publish_usage(budget_event)
        except Exception:
            logger.error("Budget enforcement: error stopping runs", exc_info=True)

    def _publish_sse(
        self,
        project_id: str,
        epic_id: str,
        run_id: str,
        role: str,
        model_id: str,
        delta: UsageDelta,
        run_totals: RunTotals,
    ) -> None:
        try:
            from yukar.events import bus as event_bus
            from yukar.models.events import TokenUsageEvent

            limit = self._limit_usd
            month_spent = self._spent_this_month_usd()
            day_spent = self._spent_today_usd()
            days_in_month = self._days_in_current_month()
            remaining = None if limit is None else max(0.0, limit - month_spent)
            daily_budget = None if limit is None else limit / days_in_month
            month_ratio = None if (limit is None or limit <= 0) else month_spent / limit
            day_ratio = (
                None if (daily_budget is None or daily_budget <= 0) else day_spent / daily_budget
            )
            event = TokenUsageEvent(
                project_id=project_id,
                epic_id=epic_id,
                run_id=run_id,
                role=role,
                model_id=model_id,
                delta={
                    "input": delta.input_tokens,
                    "output": delta.output_tokens,
                    "cache_read": delta.cache_read_tokens,
                    "cache_write": delta.cache_write_tokens,
                    "embedding": delta.embedding_tokens,
                },
                run_totals={
                    "input_tokens": run_totals.input_tokens,
                    "output_tokens": run_totals.output_tokens,
                    "cache_read_tokens": run_totals.cache_read_tokens,
                    "cache_write_tokens": run_totals.cache_write_tokens,
                    "embedding_tokens": run_totals.embedding_tokens,
                    "total_tokens": run_totals.total_tokens,
                    "cost_usd": run_totals.cost_usd,
                    "cost_jpy": run_totals.cost_jpy,
                },
                global_totals={
                    "cost_usd": sum(r.cost_usd for r in self._runs.values()),
                    "cost_jpy": sum(r.cost_jpy for r in self._runs.values()),
                    "budget_limit_usd": limit,
                    "budget_remaining_usd": remaining,
                    "month_spent_usd": month_spent,
                    "day_spent_usd": day_spent,
                    "daily_budget_usd": daily_budget,
                    "days_in_month": days_in_month,
                    "month_ratio": month_ratio,
                    "day_ratio": day_ratio,
                    "over_budget": (limit is not None and month_spent >= limit),
                },
            )
            event_bus.publish(project_id, epic_id, event)
            # Also fan-out to the global usage stream (Topbar / dashboard).
            event_bus.publish_usage(event)
        except Exception:
            logger.debug("SSE publish for token_usage failed", exc_info=True)


# ---------------------------------------------------------------------------
# Module-level singleton (initialised in app lifespan)
# ---------------------------------------------------------------------------

_tracker: TokenUsageTracker | None = None


def init_tracker(tracker: TokenUsageTracker) -> None:
    """Install the singleton tracker (called from app lifespan)."""
    global _tracker  # noqa: PLW0603
    _tracker = tracker


def get_tracker() -> TokenUsageTracker:
    """Return the singleton tracker.

    Raises:
        RuntimeError: If called before :func:`init_tracker`.
    """
    if _tracker is None:
        raise RuntimeError("TokenUsageTracker not initialised; call init_tracker() first")
    return _tracker
