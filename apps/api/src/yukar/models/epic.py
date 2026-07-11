"""Epic model — spec §4.2."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

# Shared status literal — used by Epic.status and EpicStatusChangedEvent.status.
#
# Lifecycle (single bit, user-owned):
#   open ⇄ completed
#
# Only the USER flips this bit (PATCH /epics/{id}).  Everything else is an
# attribute (a recorded fact), not a status:
#   - "was it merged?"    → ``merged_at`` (set when every repo's epic branch is
#     merged; the epic stays open — work may continue after a merge).
#   - "has work started?" → derived from task states, not stored here.
#   - "is it awaiting review?" → a notification/unread concern, not a status.
# Runs never transition the epic: finishing a run, failing a run, or an agent
# declaring "done" leaves ``status`` untouched.
EpicStatus = Literal["open", "completed"]

# Legacy → new status mapping (lazy migration, applied on every read).
# Old epic.yaml files may still carry the pre-redesign 7-value vocabulary.
# Every legacy value maps to "completed": pre-redesign epics are locked as
# finished history, and the user explicitly reopens the ones worth resuming.
# (Mapping in-flight values like "in_progress" to open would leak half-run
# state into the new semantics and leave abandoned epics squatting on the
# open board.)  New code only ever writes "open" / "completed".
_LEGACY_STATUS_MAP = {
    "planned": "completed",
    "in_progress": "completed",
    "in_review": "completed",
    "failed": "completed",
    "closed": "completed",
    "merged": "completed",
}


class Epic(BaseModel):
    id: str  # e.g. "EP-42"
    slug: str  # e.g. "refactor-auth-flow"
    title: str
    description: str = ""
    acceptance_criteria: str = ""  # verifiable done-conditions for Evaluator (spec B2/F3)
    status: EpicStatus = "open"
    branch: str = ""  # yukar/ep-42-refactor-auth-flow
    touched_repos: list[str] = Field(default_factory=list)
    # Merge fact (attribute, not a status): set once when every repo's epic
    # branch has been merged into its default branch.  Recording it does NOT
    # complete the epic — only the user flips ``status``.
    merged_at: datetime | None = None
    # Inference effort for Manager (Opus). thinking is always adaptive.
    manager_effort: Literal["high", "xhigh", "max"] = "high"
    # thread_id of the active manager trial. None → falls back to "manager" for backward compat.
    active_thread_id: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_status(cls, data: Any) -> Any:
        """Map the legacy 7-value status vocabulary onto the 1-bit lifecycle.

        ALL legacy values → completed (pre-redesign epics are locked as
        finished history; the user reopens the ones worth resuming).

        ``merged`` additionally back-fills ``merged_at`` from ``updated_at``
        (the moment the old code flipped the status is the best available
        approximation of when the merge fact was recorded).

        Lazy migration: the mapping is applied on every read; the new values
        are persisted by the next ``save_epic`` write-back.
        """
        if isinstance(data, dict):
            status = data.get("status")
            if isinstance(status, str) and status in _LEGACY_STATUS_MAP:
                if status == "merged" and not data.get("merged_at"):
                    data["merged_at"] = data.get("updated_at")
                data["status"] = _LEGACY_STATUS_MAP[status]
        return data
