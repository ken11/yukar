"""Settings router — GET/PUT /api/settings."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException

from yukar.config.loader import save_settings
from yukar.config.settings import Settings
from yukar.deps import SettingsDep

router = APIRouter(prefix="/api/settings", tags=["settings"])


@router.get("", response_model=Settings)
async def get_settings(settings: SettingsDep) -> Settings:
    return settings


@router.put("", response_model=Settings)
async def put_settings(body: Settings, settings: SettingsDep) -> Settings:
    # Expand ~ in workspace_root using the same rule as load_settings (fix #9).
    body.workspace_root = str(Path(body.workspace_root).expanduser())
    # workspace_root is wired into the supervisor / indexer / watcher / usage
    # tracker once at startup; those subsystems keep using the original root and
    # cannot be live-rewired safely.  Reject a runtime change with 422 (the
    # current root is already expanded by load_settings, so the comparison is on
    # equal footing) instead of silently persisting a value that does not take
    # effect until the next restart.
    if body.workspace_root != settings.workspace_root:
        raise HTTPException(
            status_code=422,
            detail=(
                "workspace_root cannot be changed at runtime; it is wired into "
                "the supervisor, indexer and watcher at startup. Edit "
                "settings.yaml and restart yukar to change it."
            ),
        )
    # Persist to disk
    await save_settings(body)
    # Update the in-memory instance by replacing field values
    for field in Settings.model_fields:
        setattr(settings, field, getattr(body, field))
    return settings
