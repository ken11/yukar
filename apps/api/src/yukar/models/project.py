"""Project and Repo models — spec §4.2."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class RepoCommands(BaseModel):
    allow: list[str] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=list)


class RepoIndex(BaseModel):
    enabled: bool = True


_SERVICE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


class ServiceReadiness(BaseModel):
    """When to consider a dev service booted.

    ``path`` is probed with HTTP GET on 127.0.0.1:{port}; None means waiting
    for the port to accept connections is enough.
    """

    path: str | None = None
    timeout_seconds: float = Field(default=60.0, gt=0, le=600)

    @model_validator(mode="after")
    def _normalize_path(self) -> ServiceReadiness:
        # A path without a leading slash would build "…:{port}health", whose
        # port token is unparseable — force a leading slash so the readiness
        # URL is always well-formed.  Blank collapses to None (port-only).
        if self.path is not None:
            stripped = self.path.strip()
            if not stripped:
                self.path = None
            elif not stripped.startswith("/"):
                self.path = "/" + stripped
            else:
                self.path = stripped
        return self


class DevService(BaseModel):
    """One long-running dev process, launched by the host inside a trial worktree.

    ``command`` is exec tokens (never a shell line). Tokens and ``env`` values
    may contain ``{port}`` (this service's assigned port) and ``{port:name}``
    (a sibling service's port). ``base_port`` is a preference — the host
    assigns a free port per trial so parallel worktrees never collide.
    """

    name: str
    command: list[str] = Field(min_length=1)
    cwd: str = "."  # relative to the repo root inside the worktree
    base_port: int = Field(ge=1, le=65535)
    readiness: ServiceReadiness = Field(default_factory=ServiceReadiness)
    env: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_name(self) -> DevService:
        if not _SERVICE_NAME_RE.match(self.name):
            raise ValueError(f"Invalid service name: {self.name!r}")
        return self


class DevServerBrowser(BaseModel):
    """Browser egress config for agent verification of this repo's services.

    Navigation and every subresource request are fail-closed to the trial's
    own service origins; ``allowed_origins`` adds explicit exceptions and
    ``allow_common_cdns`` enables the built-in well-known CDN preset
    (GET only, no credentials).
    """

    allowed_origins: list[str] = Field(default_factory=list)
    allow_common_cdns: bool = True


class DevServerConfig(BaseModel):
    """User-declared dev server launch config (services start in list order)."""

    services: list[DevService] = Field(min_length=1)
    browser: DevServerBrowser = Field(default_factory=DevServerBrowser)

    @model_validator(mode="after")
    def _validate_unique_names(self) -> DevServerConfig:
        names = [s.name for s in self.services]
        if len(names) != len(set(names)):
            raise ValueError("Service names must be unique")
        return self


class Repo(BaseModel):
    name: str
    path: str  # Absolute path to local git repo
    default_branch: str = "main"
    commands: RepoCommands = Field(default_factory=RepoCommands)
    index: RepoIndex = Field(default_factory=RepoIndex)
    dev_server: DevServerConfig | None = None


class Project(BaseModel):
    id: str
    name: str
    status: Literal["active", "idle"] = "active"
    repos: list[str] = Field(default_factory=list)  # repo names
    epic_counter: int = 0
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
