"""FastAPI dependency injection providers."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from yukar.config.settings import Settings
from yukar.indexer.service import IndexerService
from yukar.runs.supervisor import RunSupervisor, get_supervisor
from yukar.usage.tracker import TokenUsageTracker


def get_settings(request: Request) -> Settings:
    """Return the app-level settings instance from app.state."""
    return request.app.state.settings  # type: ignore[no-any-return]


def get_workspace_root(settings: Annotated[Settings, Depends(get_settings)]) -> str:
    return settings.workspace_root


def get_run_supervisor() -> RunSupervisor:
    return get_supervisor()


def get_indexer_service(request: Request) -> IndexerService:
    """Return the app-level IndexerService instance from app.state."""
    return request.app.state.indexer_service  # type: ignore[no-any-return]


def get_usage_tracker(request: Request) -> TokenUsageTracker:
    """Return the app-level TokenUsageTracker from app.state."""
    return request.app.state.usage_tracker  # type: ignore[no-any-return]


SettingsDep = Annotated[Settings, Depends(get_settings)]
WorkspaceRootDep = Annotated[str, Depends(get_workspace_root)]
SupervisorDep = Annotated[RunSupervisor, Depends(get_run_supervisor)]
IndexerServiceDep = Annotated[IndexerService, Depends(get_indexer_service)]
UsageTrackerDep = Annotated[TokenUsageTracker, Depends(get_usage_tracker)]
