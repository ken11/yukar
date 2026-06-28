"""Tests for GET /api/system/status endpoint and watcher health tracking.

Covers:
- Normal startup: endpoint returns watcher_ok=True.
- watch disabled: watch_enabled=False, watcher_ok=True.
- Repo enumeration failure: watcher_ok=False with non-empty reason.
- watcher.start() failure: watcher_ok=False with non-empty reason (lifespan test).
- app.state.indexer_health not set (e.g. test bypasses lifespan): safe default returned.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import AsyncGenerator
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

# ---------------------------------------------------------------------------
# Helper: minimal app client that bypasses lifespan and sets app.state directly
# ---------------------------------------------------------------------------


async def _make_client(
    tmp_workspace: Path,
    tmp_path: Path,
    *,
    indexer_health_kwargs: dict | None = None,
) -> AsyncGenerator[AsyncClient]:
    """Yield an AsyncClient backed by the FastAPI app with app.state pre-set.

    This bypasses the lifespan so we can inject specific indexer_health values.
    If indexer_health_kwargs is None, app.state.indexer_health is not set at all
    (tests the missing-attribute default path).
    """
    from yukar.api.routers.system import IndexerWatcherHealth
    from yukar.app import create_app
    from yukar.config import paths as config_paths
    from yukar.config.settings import LLMSettings, Settings
    from yukar.indexer.embedder import FakeEmbedder
    from yukar.indexer.service import IndexerService
    from yukar.runs.supervisor import init_supervisor
    from yukar.usage.tracker import TokenUsageTracker, init_tracker

    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(exist_ok=True)
    os.environ["YUKAR_CONFIG_DIR"] = str(cfg_dir)

    app = create_app()
    settings = Settings(workspace_root=str(tmp_workspace))
    settings.llm = LLMSettings(provider="fake")
    settings.indexer.watch = False
    app.state.settings = settings

    indexer_service = IndexerService(
        workspace_root=str(tmp_workspace),
        embedder=FakeEmbedder(),
    )
    app.state.indexer_service = indexer_service
    app.state.watcher = None

    init_supervisor(
        max_parallel_epics=settings.agent.max_parallel_epics,
        settings_getter=lambda: app.state.settings,
        indexer_service=indexer_service,
    )

    tracker = TokenUsageTracker(
        ledger_path=config_paths.ledger_yaml(str(tmp_workspace)),
    )
    app.state.usage_tracker = tracker
    app.state.exchange_rate_provider = None
    init_tracker(tracker)

    if indexer_health_kwargs is not None:
        app.state.indexer_health = IndexerWatcherHealth(**indexer_health_kwargs)
    # else: deliberately leave indexer_health unset to exercise the default path

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield client
    finally:
        os.environ.pop("YUKAR_CONFIG_DIR", None)


# ---------------------------------------------------------------------------
# Tests: endpoint response for various health states
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_system_status_healthy(tmp_path: Path) -> None:
    """Endpoint returns watcher_ok=True when health is healthy."""
    ws = tmp_path / "yukar-projects"
    ws.mkdir()

    gen = _make_client(
        ws,
        tmp_path,
        indexer_health_kwargs={
            "watch_enabled": True,
            "watcher_ok": True,
            "reason": None,
            "watched_repo_count": 3,
        },
    )
    client = await gen.__anext__()
    try:
        resp = await client.get("/api/system/status")
        assert resp.status_code == 200
        data = resp.json()
        iw = data["indexer_watcher"]
        assert iw["watch_enabled"] is True
        assert iw["watcher_ok"] is True
        assert iw["reason"] is None
        assert iw["watched_repo_count"] == 3
    finally:
        with contextlib.suppress(StopAsyncIteration):
            await gen.aclose()


@pytest.mark.asyncio
async def test_system_status_watch_disabled(tmp_path: Path) -> None:
    """When watch is disabled, watch_enabled=False and watcher_ok=True (not degraded)."""
    ws = tmp_path / "yukar-projects"
    ws.mkdir()

    gen = _make_client(
        ws,
        tmp_path,
        indexer_health_kwargs={
            "watch_enabled": False,
            "watcher_ok": True,
            "reason": None,
            "watched_repo_count": 0,
        },
    )
    client = await gen.__anext__()
    try:
        resp = await client.get("/api/system/status")
        assert resp.status_code == 200
        data = resp.json()
        iw = data["indexer_watcher"]
        assert iw["watch_enabled"] is False
        assert iw["watcher_ok"] is True
        assert iw["reason"] is None
        assert iw["watched_repo_count"] == 0
    finally:
        with contextlib.suppress(StopAsyncIteration):
            await gen.aclose()


@pytest.mark.asyncio
async def test_system_status_degraded(tmp_path: Path) -> None:
    """When watcher_ok=False, reason is present and endpoint reflects degraded state."""
    ws = tmp_path / "yukar-projects"
    ws.mkdir()

    gen = _make_client(
        ws,
        tmp_path,
        indexer_health_kwargs={
            "watch_enabled": True,
            "watcher_ok": False,
            "reason": "Failed to enumerate repos for watching: Permission denied",
            "watched_repo_count": 0,
        },
    )
    client = await gen.__anext__()
    try:
        resp = await client.get("/api/system/status")
        assert resp.status_code == 200
        data = resp.json()
        iw = data["indexer_watcher"]
        assert iw["watch_enabled"] is True
        assert iw["watcher_ok"] is False
        assert iw["reason"] is not None
        assert "repos" in iw["reason"] or "Permission" in iw["reason"]
        assert iw["watched_repo_count"] == 0
    finally:
        with contextlib.suppress(StopAsyncIteration):
            await gen.aclose()


@pytest.mark.asyncio
async def test_system_status_no_indexer_health_set(tmp_path: Path) -> None:
    """When app.state.indexer_health is absent, safe default (healthy, watch off) is returned."""
    ws = tmp_path / "yukar-projects"
    ws.mkdir()

    # Pass None so _make_client skips setting app.state.indexer_health
    gen = _make_client(ws, tmp_path, indexer_health_kwargs=None)
    client = await gen.__anext__()
    try:
        resp = await client.get("/api/system/status")
        assert resp.status_code == 200
        data = resp.json()
        iw = data["indexer_watcher"]
        # Default is healthy, watch disabled
        assert iw["watcher_ok"] is True
    finally:
        with contextlib.suppress(StopAsyncIteration):
            await gen.aclose()


# ---------------------------------------------------------------------------
# Tests: lifespan sets indexer_health correctly
#
# ASGITransport does NOT run the FastAPI lifespan — it sends raw ASGI http
# frames only.  To test lifespan behaviour we call the lifespan coroutine
# directly via its asynccontextmanager interface and then inspect app.state.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lifespan_sets_health_watch_disabled(tmp_path: Path) -> None:
    """When cfg.indexer.watch=False, lifespan sets watch_enabled=False, watcher_ok=True."""
    ws = tmp_path / "yukar-projects"
    ws.mkdir()
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    os.environ["YUKAR_CONFIG_DIR"] = str(cfg_dir)

    try:
        from yukar.app import create_app, lifespan
        from yukar.config.settings import LLMSettings, Settings

        app = create_app()

        settings = Settings(workspace_root=str(ws))
        settings.llm = LLMSettings(provider="fake")
        settings.indexer.watch = False

        with patch("yukar.app.load_settings", return_value=settings):
            async with lifespan(app):
                health = app.state.indexer_health
                assert health.watch_enabled is False
                assert health.watcher_ok is True
                assert health.reason is None
    finally:
        os.environ.pop("YUKAR_CONFIG_DIR", None)


@pytest.mark.asyncio
async def test_lifespan_sets_health_degraded_on_enum_error(tmp_path: Path) -> None:
    """When list_projects raises during watcher setup, watcher_ok=False with reason set."""
    ws = tmp_path / "yukar-projects"
    ws.mkdir()
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    os.environ["YUKAR_CONFIG_DIR"] = str(cfg_dir)

    try:
        from yukar.app import create_app, lifespan
        from yukar.config.settings import LLMSettings, Settings

        app = create_app()

        settings = Settings(workspace_root=str(ws))
        settings.llm = LLMSettings(provider="fake")
        settings.indexer.watch = True  # enable so the watcher block runs

        with (
            patch("yukar.app.load_settings", return_value=settings),
            patch(
                "yukar.storage.project_repo.list_projects",
                new_callable=AsyncMock,
                side_effect=RuntimeError("disk failure"),
            ),
        ):
            async with lifespan(app):
                health = app.state.indexer_health
                assert health.watch_enabled is True
                assert health.watcher_ok is False
                assert health.reason is not None
                assert "disk failure" in health.reason
                assert health.watched_repo_count == 0
    finally:
        os.environ.pop("YUKAR_CONFIG_DIR", None)


@pytest.mark.asyncio
async def test_lifespan_sets_health_degraded_on_watcher_start_failure(
    tmp_path: Path,
) -> None:
    """When watcher.start() raises, lifespan sets watcher_ok=False with reason set."""
    ws = tmp_path / "yukar-projects"
    ws.mkdir()
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    os.environ["YUKAR_CONFIG_DIR"] = str(cfg_dir)

    try:
        from yukar.app import create_app, lifespan
        from yukar.config.settings import LLMSettings, Settings

        app = create_app()

        settings = Settings(workspace_root=str(ws))
        settings.llm = LLMSettings(provider="fake")
        settings.indexer.watch = True  # enable so the watcher block runs

        with (
            patch("yukar.app.load_settings", return_value=settings),
            # list_projects returns empty list so repo enumeration succeeds.
            patch(
                "yukar.storage.project_repo.list_projects",
                new_callable=AsyncMock,
                return_value=[],
            ),
            # watcher.start() raises to simulate a failure (e.g. watchfiles unavailable).
            patch(
                "yukar.indexer.watcher.RepoWatcher.start",
                new_callable=AsyncMock,
                side_effect=RuntimeError("watcher start boom"),
            ),
        ):
            async with lifespan(app):
                health = app.state.indexer_health
                assert health.watch_enabled is True
                assert health.watcher_ok is False
                assert health.reason is not None
                assert "watcher start boom" in health.reason
    finally:
        os.environ.pop("YUKAR_CONFIG_DIR", None)
