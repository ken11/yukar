"""Epic model — spec §4.2."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

# Shared status literal — used by Epic.status and EpicStatusChangedEvent.status.
#
# Lifecycle:
#   planned → in_progress → in_review → {completed | merged}
#                                ↘ in_progress (revision) / closed
#
# ``in_review`` means the Manager finished all tasks and the work now awaits
# the USER's review. The Manager NEVER auto-completes: only the user reaches a
# truly-done state — ``merged`` (code merged to the default branch) or
# ``completed`` (user approval, e.g. for an investigation-only epic).
EpicStatus = Literal[
    "planned", "in_progress", "in_review", "completed", "failed", "closed", "merged"
]


class Epic(BaseModel):
    id: str  # e.g. "EP-42"
    slug: str  # e.g. "refactor-auth-flow"
    title: str
    description: str = ""
    acceptance_criteria: str = ""  # verifiable done-conditions for Evaluator (spec B2/F3)
    status: EpicStatus = "planned"
    branch: str = ""  # yukar/ep-42-refactor-auth-flow
    touched_repos: list[str] = Field(default_factory=list)
    # Inference effort for Manager (Opus). thinking is always adaptive.
    manager_effort: Literal["high", "xhigh", "max"] = "high"
    # thread_id of the active manager trial. None → falls back to "manager" for backward compat.
    active_thread_id: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
