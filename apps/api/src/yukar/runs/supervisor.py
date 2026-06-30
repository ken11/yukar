"""Run supervisor — manages asyncio.Task lifecycle per epic.

Invariants:
  - At most one active run per (project_id, epic_id).
  - max_parallel_epics is enforced via a semaphore.
  - pause/resume/stop are forwarded to the underlying runner.

Epic / state status transitions (architecture.md §3.2):
  - supervisor owns epic.yaml.status; orchestrator owns state.yaml.status.
  - epic.yaml:
      planned → in_progress  : on run start (before runner.start is awaited)
      in_progress → in_review: on successful run completion (awaits USER review;
            the supervisor never auto-completes — only the user reaches
            completed/merged via the API)
      in_progress → failed   : on run failure (unhandled exception)
      stop: epic.yaml stays in_progress — the run was interrupted and can be
            restarted by the user.
  - state.yaml (RunState.status managed by orchestrator):
      idle    → running   : orchestrator.start() begins
      running → completed : successful full-loop completion
      running → idle      : CancelledError (explicit supervisor.stop() call)
                            — "idle" means "not running, no error, restartable"
      running → error     : unhandled internal exception inside orchestrator

Settings resolution (architecture.md §5 decision#7):
  - Settings changes must apply to new Runs but NOT to already-running Runs.
  - The supervisor therefore does NOT cache LLMSettings/git author at
    construction time.  Instead it holds a ``settings_getter`` callable that
    is invoked inside ``_make_runner()`` at the moment a new Run starts.
  - This means a PUT /api/settings followed immediately by POST …/run will
    produce a runner that uses the updated settings, while any already-running
    runner is unaffected (it was created earlier).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from yukar.config.settings import Settings
from yukar.runs.runner import DummyRunner, RunnerProtocol
from yukar.storage.epic_repo import get_epic, save_epic

# Sentinel epic_id used as the key for the batch-merge (arbiter) run.
# It is not a real epic on disk.  The supervisor registers the arbiter runner
# under (project_id, MERGE_SENTINEL) so exactly one batch-merge can run per
# project at a time.
MERGE_SENTINEL = "__merge__"

logger = logging.getLogger(__name__)


class IndexNotReadyError(RuntimeError):
    """Raised by ``_ensure_repos_indexed`` when one or more repos with no
    existing index fail to build before the Manager is started.

    The error message includes the failing repo names and their error details
    so that the ``RunFailedEvent`` surfaces a human-readable reason.
    """


@dataclass
class _RunHandle:
    run_id: str
    runner: RunnerProtocol
    task: asyncio.Task[None]
    # Stored so that pause/resume can update state.yaml via state_repo.
    root: str
    project_id: str
    epic_id: str
    # The manager-trial thread_id that this run drives.
    # Defaults to "manager" for single-trial (backward-compatible) runs.
    manager_thread_id: str = "manager"
    # Mutable holder shared between the _RunHandle and the _run_with_semaphore
    # closure so that stop() can signal to the preparing phase that this is a
    # user-initiated stop (not a server shutdown).  Using a dict rather than a
    # bool field keeps the reference stable even if the handle is replaced.
    # The field_factory gives each instance its own dict (avoids shared mutable
    # defaults); callers may also pass a pre-built dict to share the reference
    # with the closure (see start() / start_continuation() for the pattern).
    _stop_flag: dict[str, bool] = field(default_factory=lambda: {"requested": False})

    @property
    def stop_requested(self) -> bool:
        return self._stop_flag["requested"]

    def mark_stop_requested(self) -> None:
        """Signal that this run was stopped by the user (not server shutdown)."""
        self._stop_flag["requested"] = True


async def _ensure_repos_indexed(
    root: str,
    project_id: str,
    indexer_service: Any | None,
) -> None:
    """Refresh the FAISS index for every enabled repo before the Manager starts.

    Called as an awaited step inside ``_run_with_semaphore`` (after the
    semaphore is acquired, before ``runner.start``).  The run task is blocked
    until all repos are refreshed or the operation is cancelled via stop().

    Behaviour per repo (``index.enabled=True``):
    - Repo path does not exist on disk → skip with a warning (config mismatch,
      not an index failure — does not block the run).
    - ``had_index=False`` (no usable index) → full rebuild.  If this fails the
      repo is classified as *fatal* and its error is collected.
    - ``had_index=True`` (existing index present) → incremental update
      (``full=False``).  If this fails, a warning is logged but the run is NOT
      blocked (the old index is still readable — ``faiss_store`` write is
      atomic, so the existing files are untouched on failure).
    - If already indexing (``_indexing`` set), the running task was kicked off
      by a previous call; we wait for it by issuing our own reindex call which
      will serialize behind the lock inside ``faiss_store``.

    After the loop, if any fatal (had_index=False) failures were collected,
    ``IndexNotReadyError`` is raised with a combined message so the caller can
    surface it as a ``RunFailedEvent`` without starting the Manager.

    CancelledError from a stop() call is never swallowed — it propagates so
    the supervisor can correctly handle the stop lifecycle.
    """
    if indexer_service is None:
        return

    from pathlib import Path

    from yukar.config import paths as config_paths
    from yukar.indexer import faiss_store
    from yukar.indexer.stats import read_error
    from yukar.storage.project_repo import list_repos

    repos = await list_repos(root, project_id)
    fatal_errors: list[str] = []  # repo names + messages for IndexNotReadyError

    for repo in repos:
        if not repo.index.enabled:
            continue

        repo_path = Path(repo.path)
        if not repo_path.exists():
            logger.warning(
                "index-guard: repo path does not exist, skipping %s/%s (%s)",
                project_id,
                repo.name,
                repo.path,
            )
            continue

        idx_dir = config_paths.index_dir(root, project_id, repo.name)
        had_index = faiss_store.index_exists(idx_dir)

        logger.info(
            "index-guard: refreshing index for %s/%s (full=%s)",
            project_id,
            repo.name,
            not had_index,
        )
        try:
            await indexer_service.reindex_repo(
                project_id,
                repo.name,
                repo_path,
                full=not had_index,
            )
        except asyncio.CancelledError:
            # Stop was requested during indexing — propagate immediately.
            raise
        except Exception as exc:
            if not had_index:
                # Fatal: no usable index exists and the build failed.
                # Try to include the persisted error detail (set by reindex_repo
                # on failure) for a richer message in RunFailedEvent.
                err_detail: str = str(exc)
                try:
                    err_json = await asyncio.to_thread(read_error, idx_dir)
                    if err_json is not None:
                        err_detail = err_json.get("message", err_detail)
                except Exception:
                    pass
                fatal_errors.append(f"{repo.name}: {err_detail}")
                logger.error(
                    "index-guard: fatal — no index for %s/%s and build failed: %s",
                    project_id,
                    repo.name,
                    exc,
                    exc_info=True,
                )
            else:
                # Non-fatal: existing index survives; warn and continue.
                logger.warning(
                    "index-guard: incremental update failed for %s/%s "
                    "(existing index retained): %s",
                    project_id,
                    repo.name,
                    exc,
                    exc_info=True,
                )

    if fatal_errors:
        raise IndexNotReadyError(
            "Cannot start run — index build failed for: " + "; ".join(fatal_errors)
        )


# ---------------------------------------------------------------------------
# Fire-and-forget helper — tracks task reference to suppress
# "Task exception was never retrieved" noise (item D).
# ---------------------------------------------------------------------------

_background_tasks: set[asyncio.Task[Any]] = set()


def _fire_and_forget(coro: Any, *, name: str | None = None) -> None:
    """Schedule *coro* as a background task, capturing the reference.

    The task reference is stored in ``_background_tasks`` until completion so
    that Python's GC does not prematurely free the task (which would emit
    "Task destroyed but it is pending!" warnings).  Any exception raised by the
    coroutine is logged at DEBUG level rather than surfacing as an unhandled
    exception (``"Task exception was never retrieved"``).
    """
    task: asyncio.Task[Any] = asyncio.create_task(coro, name=name)
    _background_tasks.add(task)

    def _on_done(t: asyncio.Task[Any]) -> None:
        _background_tasks.discard(t)
        exc = t.exception() if not t.cancelled() else None
        if exc is not None:
            logger.warning(
                "background task %r raised: %s",
                name or "?",
                exc,
                exc_info=exc,
            )

    task.add_done_callback(_on_done)


# ---------------------------------------------------------------------------
# Preparing-phase stop helper
# ---------------------------------------------------------------------------


async def _update_state_idle(root: str, project_id: str, epic_id: str) -> None:
    """Update state.yaml to idle after a preparing-phase stop.

    Called as a fire-and-forget background task from ``_emit_preparing_stopped``
    so that the await does not run inside a CancelledError handler.
    """
    from datetime import UTC, datetime

    from yukar.storage import state_repo

    # Update state.yaml: idle + clear transient fields.
    # The Manager never started, so state.yaml may not exist yet; we create or
    # update it to reflect the "stopped, restartable" state.
    existing = await state_repo.get_state(root, project_id, epic_id)
    if existing is not None:
        existing.status = "idle"
        existing.active_workers = []
        existing.pending_question = None
        existing.last_event_at = datetime.now(UTC)
        await state_repo.save_state(root, project_id, epic_id, existing)
    # If state.yaml does not exist (Manager never wrote it), leave it absent —
    # the UI will treat a missing state as idle, which is the correct outcome.


def _emit_preparing_stopped(
    root: str,
    project_id: str,
    epic_id: str,
    run_id: str,
) -> None:
    """Publish terminal lifecycle events for a stop that happened during the
    preparing (index-refresh) phase, before the Manager runner was started.

    This mirrors the orchestrator's own stop handler (orchestrator.py:406-417)
    but is invoked by the supervisor when the Manager never got to run and
    therefore the orchestrator's handler will never fire.

    Specifically:
    1. Publishes ``RunStoppedEvent`` (replayable) so the UI transitions away
       from "preparing" to "stopped".
    2. Publishes the SSE sentinel ``None`` to close the stream.
    3. Schedules a fire-and-forget task to update state.yaml to ``idle``
       (restartable) with empty active_workers and no pending_question —
       identical semantics to orchestrator stop.

    This function is intentionally synchronous because it may be called from
    within an ``except asyncio.CancelledError`` handler where the enclosing
    task has already been cancelled.  Any ``await`` inside that handler would
    immediately re-raise ``CancelledError``, swallowing the events.  The
    state.yaml update is therefore deferred to ``_update_state_idle`` which is
    scheduled as a fire-and-forget background task so that the SSE events are
    always published before the ``raise`` that follows.

    Must only be called when the stop was user-initiated (``stop_requested``).
    Server-shutdown cancellations must NOT call this so that state.yaml is
    preserved for restart recovery (same invariant as orchestrator.py:398-405).
    """
    from yukar.events import bus as event_bus
    from yukar.models.events import RunStoppedEvent

    stopped_event = RunStoppedEvent(
        project_id=project_id, epic_id=epic_id, run_id=run_id
    )
    # Publish RunStoppedEvent synchronously — cannot be cancelled.
    event_bus.publish(project_id, epic_id, stopped_event)
    # Close the SSE stream for this (project_id, epic_id) pair.
    event_bus.publish(project_id, epic_id, None)

    # Schedule state.yaml update as a fire-and-forget background task so that
    # the await does not run inside the CancelledError handler (which would
    # immediately re-raise CancelledError and swallow the update).
    _fire_and_forget(
        _update_state_idle(root, project_id, epic_id),
        name=f"preparing-stop-state-{epic_id}",
    )


async def _transition_epic_status(
    root: str,
    project_id: str,
    epic_id: str,
    status: Literal["planned", "in_progress", "in_review", "failed"],
) -> None:
    """Update epic.yaml.status atomically.  No-op if the epic cannot be loaded.

    Only the supervisor-driven transitions appear here. ``completed`` and
    ``merged`` are deliberately absent: they are user-driven approval/merge
    actions reached via the API (PATCH status / git merge), never set
    automatically when a run finishes.
    """
    epic = await get_epic(root, project_id, epic_id)
    if epic is not None:
        epic.status = status
        await save_epic(root, project_id, epic)


class RunSupervisor:
    def __init__(
        self,
        max_parallel_epics: int = 2,
        settings_getter: Callable[[], Settings] | None = None,
        indexer_service: Any | None = None,
        usage_tracker: Any | None = None,
    ) -> None:
        self._semaphore = asyncio.Semaphore(max_parallel_epics)
        self._runs: dict[tuple[str, str], _RunHandle] = {}
        # ``settings_getter`` is called at _make_runner() time so each new Run
        # always sees the current settings (architecture.md §5 decision #7).
        self._settings_getter = settings_getter
        # IndexerService shared with orchestrators for repo_search/summarize tools.
        self._indexer_service = indexer_service
        self._usage_tracker = usage_tracker
        self._start_lock = asyncio.Lock()

    def _key(self, project_id: str, epic_id: str) -> tuple[str, str]:
        return (project_id, epic_id)

    def _register(self, key: tuple[str, str], handle: _RunHandle) -> None:
        """Register *handle* under *key* and arm self-cleanup on completion.

        When the run task reaches a terminal state (completed / failed /
        cancelled) the done-callback removes the handle from ``_runs`` so the
        registry does not leak stale handles across the process lifetime and so
        ``is_running`` / inject / SSE lookups never resolve to a finished run.

        The callback removes the handle **only if it is still the one stored
        under this key** — a subsequent ``start`` may have replaced it with a
        fresh run, and we must not evict the live successor.  ``stop()`` deletes
        the handle eagerly and is therefore idempotent with this callback.
        """
        self._runs[key] = handle

        def _on_run_done(_task: asyncio.Task[None]) -> None:
            current = self._runs.get(key)
            if current is handle:
                del self._runs[key]

        handle.task.add_done_callback(_on_run_done)

    def is_running(self, project_id: str, epic_id: str) -> bool:
        key = self._key(project_id, epic_id)
        if key not in self._runs:
            return False
        return not self._runs[key].task.done()

    def is_thread_run_active(self, project_id: str, epic_id: str, thread_id: str) -> bool:
        """Return True if an active run is driving the given manager thread.

        Used by the threads router to check whether a trial's run is still active
        before archiving — avoids direct _runs/_handle private-attribute access.
        """
        key = self._key(project_id, epic_id)
        handle = self._runs.get(key)
        return (
            handle is not None and not handle.task.done() and handle.manager_thread_id == thread_id
        )

    async def start(
        self,
        root: str,
        project_id: str,
        epic_id: str,
        manager_thread_id: str = "manager",
    ) -> str:
        """Start a new run. Returns the run_id.

        Raises RuntimeError if a run is already active for this epic.
        Epic status: planned → in_progress (here, before runner starts).

        Args:
            manager_thread_id: The manager-trial thread_id this run will drive.
                Defaults to "manager" (single-trial, backward-compatible case).
        """
        async with self._start_lock:
            key = self._key(project_id, epic_id)
            if self.is_running(project_id, epic_id):
                # If the active run is driving a *different* manager trial, the
                # caller must stop that run first (multi-trial conflict gate).
                active_handle = self._runs.get(key)
                if (
                    active_handle is not None
                    and not active_handle.task.done()
                    and active_handle.manager_thread_id != manager_thread_id
                ):
                    raise RuntimeError(
                        f"A run for a different manager trial "
                        f"({active_handle.manager_thread_id!r}) is already active for "
                        f"epic {epic_id!r}. Stop it before starting a new trial."
                    )
                raise RuntimeError(f"Run already active for epic {epic_id}")
            if self.is_arbiter_running(project_id):
                raise RuntimeError("A merge (arbiter) is in progress for this project")
            if self._usage_tracker is not None and self._usage_tracker.is_over_budget():
                raise RuntimeError("Budget limit reached")
            # TOCTOU guard: re-check epic status inside the lock so a concurrent
            # close() call that landed between the router check and lock acquisition
            # cannot be silently overwritten by the supervisor's in_progress transition.
            _epic_check = await get_epic(root, project_id, epic_id)
            if _epic_check is not None and _epic_check.status == "closed":
                raise RuntimeError("Epic is closed")

            run_id = f"run-{uuid.uuid4().hex}"
            runner = self._make_runner(manager_thread_id=manager_thread_id)

            # Transition epic status to in_progress before the runner task starts.
            await _transition_epic_status(root, project_id, epic_id, "in_progress")

            _indexer_service = self._indexer_service
            # Mutable flag shared between this closure and the _RunHandle so that
            # stop() can signal "user stop" vs "server shutdown" to the preparing phase.
            _stop_flag: dict[str, bool] = {"requested": False}

            async def _run_with_semaphore() -> None:
                async with self._semaphore:
                    # Refresh indexes before starting the Manager.
                    # IndexNotReadyError → surface as RunFailedEvent and stop.
                    # CancelledError → propagate (stop lifecycle).
                    from yukar.events import bus as event_bus
                    from yukar.models.events import RunFailedEvent, RunPreparingEvent

                    event_bus.publish(
                        project_id,
                        epic_id,
                        RunPreparingEvent(
                            project_id=project_id, epic_id=epic_id, run_id=run_id
                        ),
                    )
                    try:
                        await _ensure_repos_indexed(root, project_id, _indexer_service)
                    except IndexNotReadyError as idx_err:
                        await _transition_epic_status(root, project_id, epic_id, "failed")
                        event_bus.publish(
                            project_id,
                            epic_id,
                            RunFailedEvent(
                                project_id=project_id,
                                epic_id=epic_id,
                                run_id=run_id,
                                error=str(idx_err),
                            ),
                        )
                        return
                    except asyncio.CancelledError:
                        # CancelledError during the preparing phase.  If the stop was
                        # user-initiated (_stop_flag["requested"]), emit terminal lifecycle
                        # events so the UI transitions away from "preparing".  Otherwise
                        # (server shutdown) preserve state.yaml for restart recovery.
                        # _emit_preparing_stopped is synchronous so the await-re-raise
                        # problem inside a CancelledError handler is avoided.
                        if _stop_flag["requested"]:
                            _emit_preparing_stopped(root, project_id, epic_id, run_id)
                        raise

                    # Indexing completed normally.  Check whether stop() was called
                    # while we were blocked — if so, skip starting the Manager and
                    # emit the same terminal events as the CancelledError branch.
                    if _stop_flag["requested"]:
                        _emit_preparing_stopped(root, project_id, epic_id, run_id)
                        return

                    try:
                        await runner.start(root, project_id, epic_id, run_id)
                        # The Manager finishing does NOT mean the epic is done. It
                        # only means the work is ready for the USER to review. The
                        # user then merges (→ merged) or approves (→ completed).
                        # See models.epic.EpicStatus for the lifecycle rationale.
                        await _transition_epic_status(root, project_id, epic_id, "in_review")
                    except Exception:
                        await _transition_epic_status(root, project_id, epic_id, "failed")
                        raise

            task: asyncio.Task[None] = asyncio.create_task(
                _run_with_semaphore(),
                name=f"run-{project_id}-{epic_id}",
            )
            self._register(
                key,
                _RunHandle(
                    run_id=run_id,
                    runner=runner,
                    task=task,
                    root=root,
                    project_id=project_id,
                    epic_id=epic_id,
                    manager_thread_id=manager_thread_id,
                    _stop_flag=_stop_flag,
                ),
            )
            return run_id

    async def pause(self, project_id: str, epic_id: str) -> None:
        from datetime import UTC, datetime

        from yukar.events import bus as event_bus
        from yukar.models.events import RunPausedEvent
        from yukar.storage import state_repo

        key = self._key(project_id, epic_id)
        if key not in self._runs:
            return
        handle = self._runs[key]
        await handle.runner.pause()
        # Update state.yaml → paused and publish event.
        state = await state_repo.get_state(handle.root, project_id, epic_id)
        if state is not None and state.status == "running":
            state.status = "paused"
            state.last_event_at = datetime.now(UTC)
            await state_repo.save_state(handle.root, project_id, epic_id, state)
            event_bus.publish(
                project_id,
                epic_id,
                RunPausedEvent(
                    project_id=project_id,
                    epic_id=epic_id,
                    run_id=handle.run_id,
                ),
            )

    async def resume(self, project_id: str, epic_id: str) -> None:
        from datetime import UTC, datetime

        from yukar.events import bus as event_bus
        from yukar.models.events import RunResumedEvent
        from yukar.storage import state_repo

        key = self._key(project_id, epic_id)
        if key not in self._runs:
            return
        handle = self._runs[key]
        # Unblock workers first (symmetric with pause which calls runner.pause()
        # before disk write).  Calling resume() before the disk write means the
        # worker can never write a stale "paused" state after we've already
        # transitioned to "running".
        await handle.runner.resume()
        # Always publish RunResumedEvent unconditionally after unblocking workers.
        # The disk write is guarded to avoid a no-op overwrite, but the event must
        # reach observers regardless of whether a racing worker already wrote
        # "running" to state.yaml first (spec §3.2 resume symmetry).
        event_bus.publish(
            project_id,
            epic_id,
            RunResumedEvent(
                project_id=project_id,
                epic_id=epic_id,
                run_id=handle.run_id,
            ),
        )
        # Update state.yaml → running only if it is still "paused" on disk.
        # A racing worker that started before this await may have already written
        # "running"; in that case the disk is already correct and we skip the write.
        state = await state_repo.get_state(handle.root, project_id, epic_id)
        if state is not None and state.status == "paused":
            state.status = "running"
            state.last_event_at = datetime.now(UTC)
            await state_repo.save_state(handle.root, project_id, epic_id, state)

    async def stop(self, project_id: str, epic_id: str) -> None:
        key = self._key(project_id, epic_id)
        if key not in self._runs:
            return
        handle = self._runs[key]
        # Mark this as a user-initiated stop BEFORE awaiting runner.stop() so that
        # any CancelledError raised in _run_with_semaphore's preparing phase (or
        # after the 5-second timeout + task.cancel()) can read the flag correctly.
        handle.mark_stop_requested()
        await handle.runner.stop()
        # Give it a moment to clean up; cancel if still running
        cancelled_error: asyncio.CancelledError | None = None
        try:
            await asyncio.wait_for(asyncio.shield(handle.task), timeout=5.0)
        except TimeoutError:
            # Runner did not shut down within 5 s — force cancel.
            handle.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await handle.task
        except asyncio.CancelledError as exc:
            # The shield absorbed a cancellation of the outer caller.
            # Cancel the run task and record the error to re-raise after cleanup
            # so we don't swallow the outer cancellation.
            handle.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await handle.task
            cancelled_error = exc
        # The done-callback armed by _register may already have removed this
        # handle (the task is terminal by now).  Remove only if it is still ours
        # so we neither raise KeyError nor evict a fresh successor run.
        if self._runs.get(key) is handle:
            del self._runs[key]
        if cancelled_error is not None:
            raise cancelled_error

    def get_run_id(self, project_id: str, epic_id: str) -> str | None:
        key = self._key(project_id, epic_id)
        if key not in self._runs:
            return None
        return self._runs[key].run_id

    def _resolve_settings(self) -> tuple[Any, str, str]:
        """Resolve current LLM settings and git author info at run-start time.

        By resolving settings here (at run-start time) rather than at supervisor
        construction time, any PUT /api/settings change is picked up by the very
        next Run while already-running Runs keep their original runner intact.

        Returns:
            A ``(llm_settings, git_author_name, git_author_email)`` triple.
            ``llm_settings`` is ``None`` when no settings getter is configured.
        """
        cfg = self._settings_getter() if self._settings_getter is not None else None
        llm = cfg.llm if cfg is not None else None
        git_name = cfg.git.author_name if cfg is not None else "yukar"
        git_email = cfg.git.author_email if cfg is not None else "yukar@localhost"
        return llm, git_name, git_email

    def _make_runner(self, manager_thread_id: str = "manager") -> RunnerProtocol:
        """Instantiate the appropriate runner using settings resolved right now."""
        llm, git_name, git_email = self._resolve_settings()

        if llm is not None:
            from yukar.agents.orchestrator import EpicOrchestrator

            cfg = self._settings_getter() if self._settings_getter is not None else None
            max_parallel_workers = cfg.agent.max_parallel_workers if cfg is not None else 4

            return EpicOrchestrator(
                llm_settings=llm,
                git_author_name=git_name,
                git_author_email=git_email,
                indexer_service=self._indexer_service,
                max_parallel_workers=max_parallel_workers,
                agent_settings=cfg.agent if cfg is not None else None,
                mcp_settings=cfg.mcp if cfg is not None else None,
                embedding_settings=cfg.embedding if cfg is not None else None,
                manager_thread_id=manager_thread_id,
            )
        return DummyRunner()

    async def start_resolve(
        self,
        root: str,
        project_id: str,
        epic_id: str,
        repo_name: str,
    ) -> str:
        """Start a conflict-resolution run for a single repo.

        Uses the same (project_id, epic_id) key as regular epic runs so that
        at most one run is active per epic at any time.

        **Important**: epic.yaml.status is NOT modified by this method (the
        resolve run is orthogonal to the epic lifecycle — spec §5.2).

        Returns the run_id.
        Raises RuntimeError if a run is already active for this epic.
        """
        async with self._start_lock:
            key = self._key(project_id, epic_id)
            if self.is_running(project_id, epic_id):
                raise RuntimeError(f"Run already active for epic {epic_id}")
            if self.is_arbiter_running(project_id):
                raise RuntimeError("A merge (arbiter) is in progress for this project")
            if self._usage_tracker is not None and self._usage_tracker.is_over_budget():
                raise RuntimeError("Budget limit reached")

            run_id = f"run-{uuid.uuid4().hex}"
            runner = self._make_resolve_runner(repo_name)

            async def _run_with_semaphore() -> None:
                async with self._semaphore:
                    await runner.start(root, project_id, epic_id, run_id)

            task: asyncio.Task[None] = asyncio.create_task(
                _run_with_semaphore(),
                name=f"resolve-{project_id}-{epic_id}",
            )
            self._register(
                key,
                _RunHandle(
                    run_id=run_id,
                    runner=runner,
                    task=task,
                    root=root,
                    project_id=project_id,
                    epic_id=epic_id,
                ),
            )
            return run_id

    def _make_resolve_runner(self, repo_name: str) -> RunnerProtocol:
        """Instantiate a ResolveRunner using settings resolved right now."""
        llm, git_name, git_email = self._resolve_settings()

        if llm is not None:
            from yukar.runs.resolve_runner import ResolveRunner

            return ResolveRunner(
                llm_settings=llm,
                repo_name=repo_name,
                git_author_name=git_name,
                git_author_email=git_email,
            )
        # No LLM configured — fall back to DummyRunner (tests without settings).
        return DummyRunner()

    # ------------------------------------------------------------------
    # Batch-merge (arbiter) lifecycle
    # ------------------------------------------------------------------

    def is_arbiter_running(self, project_id: str) -> bool:
        """Return True if a batch-merge (arbiter) run is active for *project_id*."""
        return self.is_running(project_id, MERGE_SENTINEL)

    async def start_merge(
        self,
        root: str,
        project_id: str,
        epic_ids: list[str],
    ) -> str:
        """Start a batch-merge run for the given epics.

        Returns the run_id.  Raises ``RuntimeError`` if:
        - an arbiter is already running for this project, OR
        - any epic_id in *epic_ids* has an active run (they must be idle), OR
        - the budget is exhausted.
        """
        async with self._start_lock:
            if self.is_arbiter_running(project_id):
                raise RuntimeError("A merge (arbiter) is already running for this project")

            busy = [eid for eid in epic_ids if self.is_running(project_id, eid)]
            if busy:
                raise RuntimeError(
                    f"The following epics have active runs and cannot be merged: {busy}"
                )

            if self._usage_tracker is not None and self._usage_tracker.is_over_budget():
                raise RuntimeError("Budget limit reached")

            run_id = f"run-{uuid.uuid4().hex}"
            runner = self._make_arbiter_runner(epic_ids)

            async def _run_with_semaphore() -> None:
                async with self._semaphore:
                    await runner.start(root, project_id, MERGE_SENTINEL, run_id)

            task: asyncio.Task[None] = asyncio.create_task(
                _run_with_semaphore(),
                name=f"merge-{project_id}",
            )
            key = self._key(project_id, MERGE_SENTINEL)
            self._register(
                key,
                _RunHandle(
                    run_id=run_id,
                    runner=runner,
                    task=task,
                    root=root,
                    project_id=project_id,
                    epic_id=MERGE_SENTINEL,
                ),
            )
            return run_id

    async def stop_merge(self, project_id: str) -> None:
        """Stop the active batch-merge run for *project_id* (best-effort)."""
        await self.stop(project_id, MERGE_SENTINEL)

    def _make_arbiter_runner(self, epic_ids: list[str]) -> RunnerProtocol:
        """Instantiate an ArbiterRunner using settings resolved right now."""
        llm, git_name, git_email = self._resolve_settings()

        if llm is not None:
            from yukar.runs.arbiter_runner import ArbiterRunner

            return ArbiterRunner(
                llm_settings=llm,
                epic_ids=epic_ids,
                git_author_name=git_name,
                git_author_email=git_email,
            )
        # No LLM configured — fall back to DummyRunner (e.g. tests without settings).
        return DummyRunner()

    def list_active_runs(self) -> list[tuple[str, str, str]]:
        """Return list of (root, project_id, epic_id) for all active runs."""
        return [
            (handle.root, handle.project_id, handle.epic_id)
            for handle in self._runs.values()
            if not handle.task.done()
        ]

    async def list_active_runs_for_budget(self) -> list[tuple[str, str, str]]:
        """Snapshot active runs without racing a concurrent Run registration."""
        async with self._start_lock:
            return self.list_active_runs()

    def can_inject(self, project_id: str, epic_id: str) -> bool:
        """Return True if the active run for this epic can accept HITL injection.

        Only the ``EpicOrchestrator`` exposes ``inject_message``; resolve and
        arbiter runners do not.  ``start_or_inject`` uses this to tell apart a
        run that can receive a manager message from one that would drop it,
        instead of conflating both into ``inject_hitl_message`` returning False.
        """
        key = self._key(project_id, epic_id)
        handle = self._runs.get(key)
        if handle is None or handle.task.done():
            return False
        return callable(getattr(handle.runner, "inject_message", None))

    def inject_hitl_message(self, project_id: str, epic_id: str, thread_id: str, text: str) -> bool:
        """Forward a HITL message to the active orchestrator.

        Returns True if an active orchestrator received the message, False if no
        run is active **or** the active runner cannot accept injection (e.g. a
        resolve/arbiter run, which has no ``inject_message``).  Callers that must
        not lose the message silently should first consult ``can_inject`` /
        route through ``start_or_inject`` (which surfaces the undeliverable case
        as a ``RuntimeError``) rather than relying on this False alone.
        """
        key = self._key(project_id, epic_id)
        if key not in self._runs:
            return False
        handle = self._runs[key]
        inject = getattr(handle.runner, "inject_message", None)
        if callable(inject):
            inject(thread_id, text)
            return True
        return False

    async def start_continuation(
        self,
        root: str,
        project_id: str,
        epic_id: str,
        seed_prompt: str | None = None,
        manager_thread_id: str = "manager",
    ) -> str:
        """Start a continuation run for an epic that has no active run.

        A continuation run differs from a fresh run in one way: the
        EpicOrchestrator's ``FileSessionManager`` restores the existing Strands
        session history, so the Manager agent sees the full prior conversation
        context and can pick up where it left off.

        The turn-0 prompt used by the orchestrator is replaced by *seed_prompt*
        (the user's message) so the Manager treats it as an incoming request
        rather than starting a brand-new epic plan from scratch.

        epic.yaml.status is set to ``in_progress`` here (same as ``start``).
        If it was ``in_review`` (or ``completed``) this effectively reopens the
        epic for revision.

        Args:
            root: Workspace root.
            project_id: Project identifier.
            epic_id: Epic identifier.
            seed_prompt: The user's message that triggered this continuation.
                When ``None``, the orchestrator uses a generic "resume" prompt.

        Returns:
            The new run_id.

        Raises:
            RuntimeError: If a run is already active for this epic, or budget
                is exhausted.
        """
        async with self._start_lock:
            key = self._key(project_id, epic_id)
            if self.is_running(project_id, epic_id):
                raise RuntimeError(f"Run already active for epic {epic_id}")
            if self.is_arbiter_running(project_id):
                raise RuntimeError("A merge (arbiter) is in progress for this project")
            if self._usage_tracker is not None and self._usage_tracker.is_over_budget():
                raise RuntimeError("Budget limit reached")
            # TOCTOU guard: re-check inside the lock so a concurrent close()
            # cannot be silently overwritten by the in_progress transition below.
            _epic_check_cont = await get_epic(root, project_id, epic_id)
            if _epic_check_cont is not None and _epic_check_cont.status == "closed":
                raise RuntimeError("Epic is closed")

            run_id = f"run-{uuid.uuid4().hex}"
            runner = self._make_continuation_runner(
                seed_prompt, manager_thread_id=manager_thread_id
            )

            # Reopen the epic for revision (in_review/completed → in_progress).
            await _transition_epic_status(root, project_id, epic_id, "in_progress")

            _indexer_service_cont = self._indexer_service
            # Same stop_flag mechanism as start(): shared between the closure and
            # the _RunHandle so stop() can signal user-initiated stop to preparing.
            _stop_flag_cont: dict[str, bool] = {"requested": False}

            async def _run_with_semaphore() -> None:
                async with self._semaphore:
                    # Same index-guard as start(): continuation runs also need
                    # fresh indexes before re-starting the Manager.
                    from yukar.events import bus as event_bus
                    from yukar.models.events import RunFailedEvent, RunPreparingEvent

                    event_bus.publish(
                        project_id,
                        epic_id,
                        RunPreparingEvent(
                            project_id=project_id, epic_id=epic_id, run_id=run_id
                        ),
                    )
                    try:
                        await _ensure_repos_indexed(
                            root, project_id, _indexer_service_cont
                        )
                    except IndexNotReadyError as idx_err:
                        await _transition_epic_status(root, project_id, epic_id, "failed")
                        event_bus.publish(
                            project_id,
                            epic_id,
                            RunFailedEvent(
                                project_id=project_id,
                                epic_id=epic_id,
                                run_id=run_id,
                                error=str(idx_err),
                            ),
                        )
                        return
                    except asyncio.CancelledError:
                        if _stop_flag_cont["requested"]:
                            _emit_preparing_stopped(root, project_id, epic_id, run_id)
                        raise

                    # Indexing completed; check for a stop that arrived late.
                    if _stop_flag_cont["requested"]:
                        _emit_preparing_stopped(root, project_id, epic_id, run_id)
                        return

                    try:
                        await runner.start(root, project_id, epic_id, run_id)
                        # The Manager finishing does NOT mean the epic is done. It
                        # only means the work is ready for the USER to review. The
                        # user then merges (→ merged) or approves (→ completed).
                        # See models.epic.EpicStatus for the lifecycle rationale.
                        await _transition_epic_status(root, project_id, epic_id, "in_review")
                    except Exception:
                        await _transition_epic_status(root, project_id, epic_id, "failed")
                        raise

            task: asyncio.Task[None] = asyncio.create_task(
                _run_with_semaphore(),
                name=f"continuation-{project_id}-{epic_id}",
            )
            self._register(
                key,
                _RunHandle(
                    run_id=run_id,
                    runner=runner,
                    task=task,
                    root=root,
                    project_id=project_id,
                    epic_id=epic_id,
                    manager_thread_id=manager_thread_id,
                    _stop_flag=_stop_flag_cont,
                ),
            )
            return run_id

    def _make_continuation_runner(
        self, seed_prompt: str | None, manager_thread_id: str = "manager"
    ) -> RunnerProtocol:
        """Instantiate a continuation-mode runner using settings resolved right now."""
        llm, git_name, git_email = self._resolve_settings()

        if llm is not None:
            from yukar.agents.orchestrator import EpicOrchestrator

            cfg = self._settings_getter() if self._settings_getter is not None else None
            max_parallel_workers = cfg.agent.max_parallel_workers if cfg is not None else 4

            return EpicOrchestrator(
                llm_settings=llm,
                git_author_name=git_name,
                git_author_email=git_email,
                indexer_service=self._indexer_service,
                max_parallel_workers=max_parallel_workers,
                seed_prompt=seed_prompt,
                is_continuation=True,
                agent_settings=cfg.agent if cfg is not None else None,
                mcp_settings=cfg.mcp if cfg is not None else None,
                embedding_settings=cfg.embedding if cfg is not None else None,
                manager_thread_id=manager_thread_id,
            )
        return DummyRunner()

    async def start_or_inject(
        self,
        root: str,
        project_id: str,
        epic_id: str,
        thread_id: str,
        content: str,
    ) -> bool:
        """Route a manager-thread user message: inject if running, else start continuation.

        This is the single entry-point called by the threads POST handler when a
        ``role=user`` message arrives on the manager thread.

        Behaviour:
        - If a run is currently active AND the active run is for the same
          manager-trial thread_id AND can accept injection (the EpicOrchestrator):
          call ``inject_hitl_message`` (unblocks awaiting_input or next turn).
        - If a run is currently active for a *different* manager trial: raise
          ``RuntimeError`` (HTTP 409 — stop the other trial first).
        - If a run is currently active but CANNOT accept injection (a resolve or
          arbiter run, which have no ``inject_message``): raise ``RuntimeError``
          instead of silently dropping the manager message.  The router maps this
          to HTTP 409 so the user learns the message was not delivered (and can
          retry once the resolve/merge run finishes) — the message is never lost
          without a signal.
        - If no run is active: start a continuation run.  The message is NOT
          persisted here — the FSM is the sole writer.  ``content`` is passed
          as ``seed_prompt`` to ``start_continuation``; on turn-0 the
          orchestrator feeds it directly to ``stream_async`` so FSM records it
          as one clean user message.  If ``start_continuation`` raises before
          the run starts, nothing is written and the caller can safely retry.

        Args:
            root: Workspace root.
            project_id: Project identifier.
            epic_id: Epic identifier.
            thread_id: The thread the message was posted to.  For the manager
                path this is the active manager-trial thread_id.
            content: The user's message text.

        Returns:
            True if the message was forwarded to an active run,
            False if a new continuation run was started.

        Raises:
            RuntimeError: If a run is active but cannot accept the injection
                (resolve/arbiter or different-trial conflict).
        """
        if self.is_running(project_id, epic_id):
            # Check for a different-trial conflict before can_inject.
            key = self._key(project_id, epic_id)
            active_handle = self._runs.get(key)
            if (
                active_handle is not None
                and not active_handle.task.done()
                and active_handle.manager_thread_id != thread_id
            ):
                raise RuntimeError(
                    f"A run for manager trial {active_handle.manager_thread_id!r} is active. "
                    "Stop it before sending messages to a different trial."
                )
            if not self.can_inject(project_id, epic_id):
                raise RuntimeError(
                    "A conflict-resolution or merge run is in progress for this "
                    "epic and cannot receive messages; please retry once it finishes."
                )
            return self.inject_hitl_message(project_id, epic_id, thread_id, content)
        # No active run — start continuation bound to this thread_id as the manager trial.
        await self.start_continuation(
            root, project_id, epic_id, seed_prompt=content, manager_thread_id=thread_id
        )
        return False


# Singleton supervisor — shared across all requests (single event loop)
_supervisor: RunSupervisor | None = None


def get_supervisor() -> RunSupervisor:
    global _supervisor
    if _supervisor is None:
        _supervisor = RunSupervisor()
    return _supervisor


def init_supervisor(
    max_parallel_epics: int = 2,
    settings_getter: Callable[[], Settings] | None = None,
    indexer_service: Any | None = None,
    usage_tracker: Any | None = None,
) -> RunSupervisor:
    """Create (or replace) the singleton supervisor.

    ``settings_getter`` is called at run-start time so that changes made via
    PUT /api/settings are picked up by subsequent Runs without a server restart.

    ``indexer_service`` is forwarded to each ``EpicOrchestrator`` so that
    Manager and Worker agents can use ``repo_search`` / ``repo_summarize`` tools.
    """
    global _supervisor
    _supervisor = RunSupervisor(
        max_parallel_epics,
        settings_getter=settings_getter,
        indexer_service=indexer_service,
        usage_tracker=usage_tracker,
    )
    return _supervisor
