"""FastAPI application factory.

Usage:
    uvicorn yukar.app:create_app --factory --host 127.0.0.1 --port 8000 --workers 1
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from yukar.api.routers import (
    docs,
    epics,
    git,
    merge,
    project_settings,
    projects,
    runs,
    schema,
    search,
    system,
    tasks,
    threads,
    usage,
)
from yukar.api.routers import (
    settings as settings_router,
)
from yukar.api.routers.runs import project_events_router
from yukar.config import paths as config_paths
from yukar.config.loader import load_settings
from yukar.config.paths import PathSegmentError
from yukar.runs.recovery import recover_interrupted_runs
from yukar.runs.supervisor import init_supervisor

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Startup / shutdown lifecycle."""
    from yukar.indexer.embedder import create_embedder
    from yukar.indexer.service import IndexerService
    from yukar.indexer.watcher import RepoWatcher
    from yukar.storage.project_repo import list_projects, list_repos
    from yukar.usage.exchange import ExchangeRateProvider
    from yukar.usage.tracker import TokenUsageTracker, init_tracker

    cfg = load_settings()
    app.state.settings = cfg

    # Strong-reference set for fire-and-forget startup tasks.
    # Keeps tasks alive until they complete so GC cannot collect them mid-run
    # (avoids the RUF006 warning pattern).  Tasks discard themselves on done.
    startup_tasks: set[asyncio.Task[Any]] = set()
    app.state.startup_tasks = startup_tasks

    # Create IndexerService — single instance for the app (single event loop).
    # This embedder is shared across all projects, so its constructor-default
    # attribution (project_id="", run_id=None) is intentionally left empty:
    # IndexerService rebinds (project_id, run_id) per embed call via
    # embedder.set_context() so index/search embedding cost lands under the
    # project actually being indexed rather than a synthetic shared run.
    embedder = create_embedder(cfg.embedding)
    indexer_service = IndexerService(
        workspace_root=cfg.workspace_root,
        embedder=embedder,
    )
    app.state.indexer_service = indexer_service

    # Pre-fetch the tree-sitter grammar bundle in the background so that
    # structure splitting is available immediately rather than on the first
    # file indexed.  The download is idempotent (no-op when already cached).
    # Failures are logged as a warning and do not abort startup.
    _grammar_task: asyncio.Task[None] = asyncio.create_task(_prefetch_grammars())
    startup_tasks.add(_grammar_task)
    _grammar_task.add_done_callback(startup_tasks.discard)

    # Initialise exchange rate provider and usage tracker.
    exchange_provider = ExchangeRateProvider(
        cache_path=config_paths.exchange_rate_yaml(cfg.workspace_root),
        fetch_enabled=cfg.usage.fetch_exchange_rate,
    )
    app.state.exchange_rate_provider = exchange_provider
    # Trigger async rate fetch in background (non-blocking).
    _rate_task: asyncio.Task[float] = asyncio.create_task(exchange_provider.get_rate())
    startup_tasks.add(_rate_task)
    _rate_task.add_done_callback(startup_tasks.discard)

    tracker = TokenUsageTracker(
        ledger_path=config_paths.ledger_yaml(cfg.workspace_root),
        exchange=exchange_provider,
    )
    await tracker.load()
    app.state.usage_tracker = tracker
    init_tracker(tracker)

    # Pass a getter that reads from app.state.settings at run-start time so that
    # PUT /api/settings changes take effect for the next Run without a restart.
    init_supervisor(
        max_parallel_epics=cfg.agent.max_parallel_epics,
        settings_getter=lambda: app.state.settings,
        indexer_service=indexer_service,
        usage_tracker=tracker,
    )
    # Reconcile any runs that were interrupted by a previous process crash.
    await recover_interrupted_runs(cfg.workspace_root)

    # Start file watcher for indexed repos (if enabled).
    from yukar.api.routers.system import IndexerWatcherHealth

    watcher: RepoWatcher | None = None
    _watcher_ok = True
    _watcher_reason: str | None = None
    _watched_repo_count = 0

    if cfg.indexer.watch:
        watcher = RepoWatcher(indexer_service)
        # Register all repos that are already indexed (have a FAISS index on disk).
        try:
            projects = await list_projects(cfg.workspace_root)
            for project in projects:
                repos = await list_repos(cfg.workspace_root, project.id)
                for repo in repos:
                    if not repo.index.enabled:
                        continue
                    from yukar.indexer import faiss_store

                    idx_dir = config_paths.index_dir(cfg.workspace_root, project.id, repo.name)
                    if faiss_store.index_exists(idx_dir):
                        from yukar.sandbox.ignore import IgnoreRules

                        ignore_rules = await IgnoreRules.from_repo_async(Path(repo.path))
                        watcher.add_repo(
                            project.id,
                            repo.name,
                            Path(repo.path),
                            ignore_rules=ignore_rules,
                        )
                        _watched_repo_count += 1
        except Exception as exc:
            _watcher_ok = False
            _watcher_reason = f"Failed to enumerate repos for watching: {exc}"
            logger.warning("Watcher setup: could not enumerate repos", exc_info=True)

        # Wire the on_indexed hook so that repos indexed for the first time
        # during a run (or via POST /index) are automatically registered with
        # the watcher.
        # - Already-watched repos are skipped immediately via is_watching(),
        #   so no IgnoreRules construction (subprocess + rglob) ever runs for
        #   incremental reindexes — keeping the event loop unblocked.
        # - First-time repos get IgnoreRules built via from_repo_async()
        #   (to_thread), which also keeps the event loop unblocked.
        # This is wired regardless of whether repo enumeration succeeded, so
        # that future indexing still registers repos even in degraded mode.
        _watcher_ref = watcher

        async def _on_indexed_hook(
            project_id: str, repo_name: str, repo_path: Path
        ) -> None:
            if _watcher_ref.is_watching(project_id, repo_name):
                return
            from yukar.sandbox.ignore import IgnoreRules as _IgnoreRules

            rules = await _IgnoreRules.from_repo_async(repo_path)
            _watcher_ref.add_repo(project_id, repo_name, repo_path, ignore_rules=rules)

        indexer_service.set_on_indexed(_on_indexed_hook)

        if _watcher_ok:
            try:
                await watcher.start()
            except Exception as exc:
                _watcher_ok = False
                _watcher_reason = f"Watcher failed to start: {exc}"
                logger.warning("Watcher setup: watcher.start() failed", exc_info=True)

    app.state.indexer_health = IndexerWatcherHealth(
        watch_enabled=cfg.indexer.watch,
        watcher_ok=_watcher_ok,
        reason=_watcher_reason,
        watched_repo_count=_watched_repo_count,
    )
    app.state.watcher = watcher

    # Dev server manager + browser sessions — host-launched per-trial dev
    # servers for agent browser verification.  Lazy: nothing starts until a
    # browser tool asks.
    from yukar.preview import DevServerManager, init_dev_server_manager
    from yukar.preview.browser import BrowserSessionManager, init_browser_session_manager

    dev_server_manager = DevServerManager()
    init_dev_server_manager(dev_server_manager)
    app.state.dev_server_manager = dev_server_manager
    browser_session_manager = BrowserSessionManager()
    init_browser_session_manager(browser_session_manager)
    app.state.browser_session_manager = browser_session_manager

    yield

    # Shutdown: close browser sessions, then stop all dev server processes,
    # before anything else so child process groups never outlive the host.
    try:
        await browser_session_manager.close_all()
    except Exception:
        logger.warning("Shutdown: browser session close_all failed", exc_info=True)
    try:
        await dev_server_manager.stop_all()
    except Exception:
        logger.warning("Shutdown: dev server manager stop_all failed", exc_info=True)
    # Clear the module singletons so a later in-process lifespan (e.g. a
    # subsequent test) does not inherit these shut-down managers.
    init_dev_server_manager(None)
    init_browser_session_manager(None)

    # Shutdown: stop watcher.
    if watcher is not None:
        await watcher.stop()

    # Flush the usage tracker before exiting to prevent losing the last few
    # seconds of token usage that may not have been debounced to disk yet.
    try:
        await tracker.flush()
    except Exception:
        logger.debug("Shutdown: usage tracker flush failed", exc_info=True)

    # Signal all project-level SSE subscribers to stop iteration cleanly.
    # publish_project_sentinel sends None to every project queue that still has
    # active subscribers, preventing generators from blocking on empty queues
    # after the server begins shutting down.
    from yukar.events import bus as event_bus
    from yukar.storage.project_repo import list_projects as _list_projects

    try:
        projects_on_shutdown = await _list_projects(cfg.workspace_root)
        for project_on_shutdown in projects_on_shutdown:
            event_bus.publish_project_sentinel(project_on_shutdown.id)
    except Exception:
        logger.debug("Shutdown: could not enumerate projects for SSE teardown", exc_info=True)

    # Signal the global usage SSE stream to close cleanly.
    try:
        event_bus.publish_usage_sentinel()
    except Exception:
        logger.debug("Shutdown: could not publish usage sentinel", exc_info=True)


async def _prefetch_grammars() -> None:
    """Ensure the tree-sitter grammar bundle is cached before indexing starts.

    ``DownloadManager.new(version).ensure_group("all")`` fetches the "all"
    bundle (~21 MB) once and stores it under the platform cache directory.
    Subsequent calls are idempotent (the files are already present).

    The call is wrapped in ``asyncio.to_thread`` because ``ensure_group`` is a
    synchronous network/filesystem operation.  Failure is logged as a warning
    so the application still starts; structure splitting will degrade to
    line-based splitting until the bundle is available.
    """

    def _do_prefetch() -> None:
        import tree_sitter_language_pack as tslp  # type: ignore[import-untyped]

        dm = tslp.DownloadManager.new(tslp.__version__)
        dm.ensure_group("all")

    try:
        await asyncio.to_thread(_do_prefetch)
        logger.info("tree-sitter grammar bundle pre-fetch complete")
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "tree-sitter grammar bundle pre-fetch failed — structure splitting will degrade "
            "to line-based fallback until the bundle is available. Cause: %s",
            exc,
        )


def create_app() -> FastAPI:
    app = FastAPI(
        title="yukar API",
        description="Autonomous coding agent — local-only",
        version="0.1.0",
        lifespan=lifespan,
    )

    # CORS — allow the Next.js dev server and local production
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:3000", "http://localhost:3000"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Register routers
    app.include_router(projects.router)
    app.include_router(epics.router)
    app.include_router(runs.router)
    app.include_router(project_events_router)
    app.include_router(threads.router)
    app.include_router(tasks.router)
    app.include_router(docs.router)
    app.include_router(git.router)
    app.include_router(search.router)
    app.include_router(settings_router.router)
    app.include_router(schema.router)
    app.include_router(usage.router)
    app.include_router(project_settings.router)
    app.include_router(merge.router)
    app.include_router(system.router)

    @app.exception_handler(PathSegmentError)
    async def path_segment_error_handler(_request: Request, exc: PathSegmentError) -> JSONResponse:
        """Convert path-traversal guard failures (PathSegmentError) to 422.

        Only PathSegmentError is mapped to 422.  Generic ValueError (e.g. from
        pydantic ValidationError or business-logic raises) is left to propagate
        as 500 so that internal details are not leaked to clients.
        """
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app
