"""Filesystem watcher for automatic repo re-indexing.

``RepoWatcher`` uses ``watchfiles`` to monitor one or more repository
directories and schedules a full re-index whenever any non-ignored file changes.

Design
------
- The watcher runs as a background ``asyncio.Task`` (start/stop API).
- Multiple repos can be watched simultaneously; changes to one repo trigger
  only that repo's reindex.
- Ignored paths (per ``IgnoreRules``) are filtered at the Rust layer via
  ``watch_filter`` — ignored events never cross the Python boundary.
- debounce is delegated to ``awatch(debounce=...)`` — no hand-rolled
  ``call_later`` timers.
- When the repo set changes after start (``add_repo`` / ``remove_repo``),
  the running ``awatch`` is interrupted via ``_wake_event`` and a new one
  is started with the updated path list.  This also eliminates the 0-repo
  dead-end: ``_run`` waits on ``_wake_event`` when there are no repos and
  resumes once one is added.
- Re-indexing goes through ``IndexerService.reindex_repo``, which acquires the
  per-``(project, repo)`` FAISS lock — so a watcher-triggered rebuild never
  races with a manual reindex call.

Usage (lifespan integration — done in a later milestone)::

    watcher = RepoWatcher(service, debounce=2.0)
    watcher.add_repo("proj", "myrepo", Path("/repos/myrepo"))
    await watcher.start()
    ...
    await watcher.stop()
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from collections.abc import Set as AbstractSet
from pathlib import Path
from typing import Any

from yukar.sandbox.ignore import IgnoreRules

logger = logging.getLogger(__name__)


class _WatchedRepo:
    """Internal record for a single watched repository."""

    def __init__(
        self,
        project_id: str,
        repo_name: str,
        repo_path: Path,
        ignore_rules: IgnoreRules,
    ) -> None:
        self.project_id = project_id
        self.repo_name = repo_name
        self.repo_path = repo_path
        self.ignore_rules = ignore_rules


class RepoWatcher:
    """Watch repositories for changes and schedule re-indexing.

    Args:
        service: An ``IndexerService`` instance (import deferred to avoid
            circular imports at module level).
        debounce: Seconds to wait after the last change event before triggering
            a reindex.  Default is ``2.0`` seconds.  Passed to
            ``awatch(debounce=...)`` (in milliseconds) so the Rust layer
            handles collapse of rapid bursts.
    """

    def __init__(
        self,
        service: Any,  # IndexerService — typed as Any to avoid circular import
        debounce: float = 2.0,
    ) -> None:
        self._service = service
        self._debounce = debounce
        self._repos: dict[tuple[str, str], _WatchedRepo] = {}
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        # _wake_event: set when the repo set changes (add_repo / remove_repo)
        # or when stop() is called.  Used to interrupt a running awatch so
        # _run can restart it with updated paths.
        self._wake_event = asyncio.Event()
        # Strong-reference set for fire-and-forget reindex tasks.  Without this
        # the Task objects are eligible for GC as soon as create_task() returns
        # (the event loop only holds a weak reference).  Tasks remove themselves
        # via add_done_callback when they finish.
        self._reindex_tasks: set[asyncio.Task[None]] = set()

    def is_watching(self, project_id: str, repo_name: str) -> bool:
        """Return ``True`` if ``(project_id, repo_name)`` is already registered.

        Args:
            project_id: Project identifier.
            repo_name: Repository name.

        Returns:
            ``True`` when the repo is in the current watched set.
        """
        return (project_id, repo_name) in self._repos

    def add_repo(
        self,
        project_id: str,
        repo_name: str,
        repo_path: Path,
        *,
        ignore_rules: IgnoreRules | None = None,
    ) -> None:
        """Register a repo for watching.

        Args:
            project_id: Project identifier.
            repo_name: Repository name.
            repo_path: Absolute path to the repository root.
            ignore_rules: Pre-built ignore rules; built eagerly here when
                ``None`` via ``IgnoreRules.from_repo`` (synchronous).  Callers
                on the event loop should pre-build the rules with
                ``IgnoreRules.from_repo_async`` and pass them in to avoid
                blocking the loop.
        """
        key = (project_id, repo_name)
        # If the repo is already registered (e.g. from a previous successful
        # reindex during the same session), skip the registration and do NOT
        # set _wake_event — restarting awatch on every incremental refresh
        # would be wasteful and could cause a tight restart loop.  This guard is
        # placed BEFORE building ignore_rules so the no-op path avoids the
        # synchronous ``IgnoreRules.from_repo`` (git-config subprocess + file
        # reads) that the on_indexed hook would otherwise pay on every reindex.
        if key in self._repos:
            return
        if ignore_rules is None:
            ignore_rules = IgnoreRules.from_repo(repo_path)
        self._repos[key] = _WatchedRepo(project_id, repo_name, repo_path, ignore_rules)
        # Signal _run to restart awatch with the updated path list.
        self._wake_event.set()

    def remove_repo(self, project_id: str, repo_name: str) -> None:
        """Deregister a repo from watching.

        Args:
            project_id: Project identifier.
            repo_name: Repository name.
        """
        key = (project_id, repo_name)
        self._repos.pop(key, None)
        # Signal _run to restart awatch with the updated path list.
        self._wake_event.set()

    async def start(self) -> None:
        """Start the watcher background task.

        Idempotent — calling ``start`` on an already-running watcher is a no-op.
        """
        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="repo-watcher")
        logger.info("RepoWatcher started (%d repos)", len(self._repos))

    async def stop(self) -> None:
        """Stop the watcher and cancel the background task.

        Cancels the main watcher task and any in-flight reindex tasks before
        returning, so no background work leaks past shutdown.
        """
        import contextlib

        self._stop_event.set()
        # Wake up _run so it can exit the awatch loop or the wake_event.wait().
        self._wake_event.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

        # Cancel any reindex tasks that are still running and wait for them to
        # finish so the shutdown is clean.
        if self._reindex_tasks:
            for t in list(self._reindex_tasks):
                t.cancel()
            await asyncio.gather(*list(self._reindex_tasks), return_exceptions=True)

        logger.info("RepoWatcher stopped")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _make_watch_filter(self) -> Callable[[Any, str], bool]:
        """Build a watchfiles ``watch_filter`` callback for the current repo set.

        The filter is called by the Rust layer *before* an event crosses into
        Python, so ignored events never reach ``_handle_changes``.

        - Events that ``DefaultFilter`` would drop (e.g. ``*.pyc``,
          ``__pycache__``) are dropped first.
        - Events for paths not under any watched repo are dropped.
        - Events for paths matched by ``IgnoreRules`` (gitignore) are dropped.
        - Everything else is allowed through.
        """
        from watchfiles import DefaultFilter  # type: ignore[import-untyped]

        # Snapshot the current repo set so the closure is self-contained.
        repos_snapshot = list(self._repos.values())
        base = DefaultFilter()

        def _filter(change: Any, path: str) -> bool:
            if not base(change, path):
                return False
            p = Path(path)
            for watched in repos_snapshot:
                try:
                    p.relative_to(watched.repo_path)
                except ValueError:
                    continue
                # Path is under this repo — apply gitignore rules.
                return not watched.ignore_rules.is_ignored(p)
            # Not under any watched repo.
            return False

        return _filter

    async def _run(self) -> None:
        """Main watch loop.

        Restarts ``awatch`` whenever the repo set changes so that newly added
        repos are watched and removed repos are no longer watched.  When there
        are no repos, waits on ``_wake_event`` instead of spinning.
        """
        try:
            from watchfiles import awatch  # type: ignore[import-untyped]
        except ImportError:
            logger.error("watchfiles not installed — RepoWatcher disabled")
            return

        while not self._stop_event.is_set():
            self._wake_event.clear()

            if self._stop_event.is_set():
                break

            watch_paths = [str(r.repo_path) for r in self._repos.values()]

            if not watch_paths:
                logger.debug("RepoWatcher: no repos to watch — waiting for add_repo")
                # Wait until add_repo (or stop) wakes us.
                await self._wake_event.wait()
                continue

            watch_filter = self._make_watch_filter()
            debounce_ms = max(0, int(self._debounce * 1000))

            try:
                async for changes in awatch(
                    *watch_paths,
                    watch_filter=watch_filter,
                    debounce=debounce_ms,
                    stop_event=self._wake_event,
                ):
                    self._handle_changes(changes)
                # awatch exited because _wake_event was set (repo change or stop).
                # Go back to the top of the while loop to re-evaluate.
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                logger.error("RepoWatcher error: %s", exc, exc_info=True)
                # Match old behaviour: a single awatch error terminates _run to
                # avoid a hot error loop.
                break

    def _handle_changes(self, changes: AbstractSet[tuple[Any, str]]) -> None:
        """Process a batch of file-change events from watchfiles.

        ``watch_filter`` (Rust layer) already drops ignored paths, so most
        events here are actionable.  A defensive ``is_ignored`` check is
        kept so that callers that bypass ``awatch`` (e.g. unit tests that
        call ``_handle_changes`` directly) still behave correctly.

        Multiple changed paths within the same batch that belong to the same
        repo are collapsed into a single reindex call via the ``triggered``
        set — mirroring how ``awatch``'s debounce collapses rapid bursts into
        one batch.

        Args:
            changes: Set of ``(ChangeType, path_str)`` pairs from watchfiles.
        """
        # Group changed paths by repo; deduplicate within the batch.
        triggered: set[tuple[str, str]] = set()

        for _change_type, path_str in changes:
            changed = Path(path_str)
            for key, watched in self._repos.items():
                try:
                    changed.relative_to(watched.repo_path)
                except ValueError:
                    continue  # not under this repo
                # Defensive ignore check — cheap because watch_filter already
                # filtered the Rust side; this guard exists for unit-test
                # callers that invoke _handle_changes directly.
                if watched.ignore_rules.is_ignored(changed):
                    continue
                triggered.add(key)

        for key in triggered:
            self._fire_reindex(key)

    def _fire_reindex(self, key: tuple[str, str]) -> None:
        """Launch a reindex task for *key* immediately (debounce is handled by awatch)."""
        watched = self._repos.get(key)
        if watched is None:
            return
        logger.info("RepoWatcher: reindexing %s/%s", key[0], key[1])
        task = asyncio.create_task(
            self._do_reindex(watched),
            name=f"reindex-{key[0]}-{key[1]}",
        )
        self._reindex_tasks.add(task)
        task.add_done_callback(self._reindex_tasks.discard)

    async def _do_reindex(self, watched: _WatchedRepo) -> None:
        """Run the reindex and log the result."""
        try:
            # Watcher-triggered reindex uses incremental mode (full=False) to
            # avoid re-embedding the entire repo on every file save.
            n_chunks = await self._service.reindex_repo(
                watched.project_id,
                watched.repo_name,
                watched.repo_path,
                full=False,
            )
            logger.info(
                "RepoWatcher: reindex %s/%s done — %d chunks",
                watched.project_id,
                watched.repo_name,
                n_chunks,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "RepoWatcher: reindex %s/%s failed: %s",
                watched.project_id,
                watched.repo_name,
                exc,
                exc_info=True,
            )
