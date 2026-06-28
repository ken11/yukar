"""Task models — spec §4.2 tasks.yaml."""

from __future__ import annotations

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
