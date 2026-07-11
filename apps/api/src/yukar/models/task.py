"""Task models — spec §4.2 tasks.yaml — and the plan-approval snapshot.

Plan approval (lifecycle redesign P2) binds the user's approval to a
*snapshot* of the task plan: ``compute_plan_hash`` hashes only the fields
that define the plan (never execution state), and ``PlanApproval`` records
the hash the user approved.  The dispatch gate compares the stored hash
against the current plan — a changed plan simply no longer matches, so
"invalidation" needs no imperative bookkeeping.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class Task(BaseModel):
    id: str  # e.g. "T1"
    title: str
    status: Literal["todo", "in_progress", "done", "blocked"] = "todo"
    repo: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    thread: str | None = None  # linked thread id
    contract: str = ""  # what to build + how to verify it (spec B2/F3)
    agent: str | None = None  # assigned AgentProfile name; None = default role config


class TaskProgress(BaseModel):
    done: int = 0
    total: int = 0


class TasksFile(BaseModel):
    tasks: list[Task] = Field(default_factory=list)
    progress: TaskProgress = Field(default_factory=TaskProgress)


# ---------------------------------------------------------------------------
# Plan-approval snapshot (lifecycle redesign P2)
# ---------------------------------------------------------------------------

# The fields that DEFINE a plan.  Execution state (``status``, ``thread``,
# progress) is deliberately excluded so that dispatching a task — which flips
# its status — does not strip an approval the user already gave.
_PLAN_FIELDS = ("id", "title", "repo", "depends_on", "contract", "agent")


def compute_plan_hash(tasks: list[Task]) -> str:
    """Return the SHA-256 hex digest of the plan-defining task fields.

    Deterministic: tasks are sorted by ``id`` and serialised as key-sorted
    JSON, so task order in tasks.yaml and any non-plan field (status,
    thread, progress) never affect the hash.
    """
    plan = [
        {field: getattr(task, field) for field in _PLAN_FIELDS}
        for task in sorted(tasks, key=lambda t: t.id)
    ]
    payload = json.dumps(plan, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class PlanApproval(BaseModel):
    """The user's recorded approval of one task-plan snapshot.

    Persisted per epic as ``plan_approval.yaml`` (run-independent).  A stale
    record is harmless: if ``tasks_hash`` no longer matches the current plan,
    the plan is simply treated as unapproved.
    """

    tasks_hash: str
    approved_at: datetime
