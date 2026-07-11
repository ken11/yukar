"""Run / state.yaml models — spec §4.2.

Run-state vocabulary (lifecycle redesign P3):
- ``running`` / ``paused`` — a turn is actually EXECUTING (these are the only
  states that hold the epic's run slot).
- ``waiting``   — the single resting state: "your turn".  A conversation run
  parks here after every turn (and stays here across restarts).  An epic that
  has never run is also synthesised as ``waiting``.
- ``error``     — the run crashed with an unhandled exception.
- ``completed`` — JOB runs only (resolve / arbiter): finite jobs have an end.
  Conversation runs never write it — a conversation has no end (principle 2);
  the fake-provider DummyRunner also settles in ``waiting``.

``role`` (lifecycle redesign P4) records WHICH conversation agent the run
belongs to — ``manager`` or ``reviewer`` — so REST restore can attribute
"your turn" to the right thread and label it correctly.  ``manager_thread``
remains "the conversation thread this run rides on" (for a reviewer run it
points at the reviewer thread; rename is deferred to P5).

Legacy statuses (``idle`` / ``awaiting_input`` / ``interrupted``) are read
back as ``waiting`` via a BeforeValidator so old state.yaml files stay
loadable.  The removed ``pending_question`` key in old files is ignored by
pydantic's default extra handling; a missing ``role`` key defaults to
``manager``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, Field

RunStatus = Literal["running", "paused", "waiting", "error", "completed"]

# Legacy → new status mapping.  All three legacy values meant "not running,
# it is the user's turn" and collapse into ``waiting``.
_LEGACY_STATUS_MAP = {
    "idle": "waiting",
    "awaiting_input": "waiting",
    "interrupted": "waiting",
}


def _coerce_legacy_status(value: Any) -> Any:
    """Read legacy state.yaml status values as ``waiting``."""
    if isinstance(value, str):
        return _LEGACY_STATUS_MAP.get(value, value)
    return value


class ActiveWorker(BaseModel):
    worker_id: str
    task_id: str | None = None
    repo: str | None = None


class RunState(BaseModel):
    run_id: str
    status: Annotated[RunStatus, BeforeValidator(_coerce_legacy_status)] = "waiting"
    # Which conversation agent this run belongs to (P4).  Job runs (resolve /
    # arbiter / dummy) keep the default — the field is meaningful for the
    # conversation runs the user can be "waiting on".
    role: Literal["manager", "reviewer"] = "manager"
    # The conversation thread this run rides on (reviewer runs point at the
    # reviewer thread; field rename is deferred to P5).
    manager_thread: str | None = None
    active_workers: list[ActiveWorker] = Field(default_factory=list)
    started_at: datetime | None = None
    last_event_at: datetime | None = None
