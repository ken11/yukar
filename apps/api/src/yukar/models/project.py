"""Project and Repo models — spec §4.2."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field


class RepoCommands(BaseModel):
    allow: list[str] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=list)


class RepoIndex(BaseModel):
    enabled: bool = True


class Repo(BaseModel):
    name: str
    path: str  # Absolute path to local git repo
    default_branch: str = "main"
    commands: RepoCommands = Field(default_factory=RepoCommands)
    index: RepoIndex = Field(default_factory=RepoIndex)


class Project(BaseModel):
    id: str
    name: str
    status: Literal["active", "idle"] = "active"
    repos: list[str] = Field(default_factory=list)  # repo names
    epic_counter: int = 0
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
