"""Thread index entry — spec §4.2 threads.yaml.

threads.yaml is only a thin index; message content lives in sessions/.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

from yukar.models.roles import ThreadRole


class ThreadEntry(BaseModel):
    id: str  # = agent_{thread-id} suffix, e.g. "th-aaa"
    title: str
    role: ThreadRole = "user"
    repo: str | None = None  # worker's target repo
    task: str | None = None  # linked task id
    status: Literal["active", "resolved", "failed", "archived"] = "active"
    branch: str | None = None  # role=manager only: the git branch used by this trial
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    parent_thread_id: str | None = None  # manager=None, worker="manager", evaluator=worker_id


class ThreadsFile(BaseModel):
    threads: list[ThreadEntry] = Field(default_factory=list)
