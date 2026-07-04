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
    # role=manager only: the *trial* this conversation belongs to.  A trial is the
    # (branch + worktree) line of work; the worktree is keyed by trial_id, not by
    # this thread id, so several manager conversations on the same branch share one
    # worktree.  None → legacy single-conversation trial; resolve via trial_id_of()
    # which falls back to ``id`` (backward compatible).
    trial_id: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    parent_thread_id: str | None = None  # manager=None, worker="manager", evaluator=worker_id


class ThreadsFile(BaseModel):
    threads: list[ThreadEntry] = Field(default_factory=list)
