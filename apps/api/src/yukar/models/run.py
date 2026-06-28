"""Run / state.yaml models — spec §4.2."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class ActiveWorker(BaseModel):
    worker_id: str
    task_id: str | None = None
    repo: str | None = None


class RunState(BaseModel):
    run_id: str
    status: Literal[
        "idle", "running", "paused", "awaiting_input", "error", "completed", "interrupted"
    ] = "idle"
    manager_thread: str | None = None
    active_workers: list[ActiveWorker] = Field(default_factory=list)
    started_at: datetime | None = None
    last_event_at: datetime | None = None
    # The question currently presented to the user by ask_user (or the seed
    # plan-approval prompt).  Non-None only while status == "awaiting_input".
    # Cleared to None whenever the run leaves awaiting_input (reply received,
    # stop, error, completed).  Persisted in state.yaml so GET /run/state
    # can restore the question after a page reload without relying on the
    # SSE replay buffer.
    pending_question: str | None = None
