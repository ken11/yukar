"""FastAPI dependency injection providers."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, Request

from yukar.config.settings import Settings
from yukar.indexer.service import IndexerService
from yukar.models.language import UiLanguage, parse_ui_language
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


def get_ui_language(
    x_yukar_locale: Annotated[str | None, Header(alias="X-Yukar-Locale")] = None,
) -> UiLanguage | None:
    """Return the caller's UI language from the ``X-Yukar-Locale`` header.

    The Web UI sends its locale toggle on every request; run-starting endpoints
    hand it to the orchestrator so Manager / Reviewer write in that language.
    Absent or unknown values yield ``None`` (no language directive).
    """
    return parse_ui_language(x_yukar_locale)


SettingsDep = Annotated[Settings, Depends(get_settings)]
UiLanguageDep = Annotated[UiLanguage | None, Depends(get_ui_language)]
WorkspaceRootDep = Annotated[str, Depends(get_workspace_root)]
SupervisorDep = Annotated[RunSupervisor, Depends(get_run_supervisor)]
IndexerServiceDep = Annotated[IndexerService, Depends(get_indexer_service)]
UsageTrackerDep = Annotated[TokenUsageTracker, Depends(get_usage_tracker)]
