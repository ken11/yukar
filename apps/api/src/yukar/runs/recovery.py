"""Startup state recovery — reconcile orphaned running/paused runs.

On lifespan startup, scan all epic state.yaml files.  Any run whose status is
``running`` or ``paused`` was interrupted mid-flight (e.g. a process crash).
We mark those as ``interrupted`` so the UI can show an interrupted state
instead of a stale "running" indicator.

``awaiting_input`` runs are intentionally **preserved as-is** (not transitioned
to ``interrupted``).  An ``awaiting_input`` run has no in-flight async work —
the Manager called ``ask_user`` and parked itself waiting for a human reply.
The Strands session, ``state.yaml`` (including ``pending_question``), and
tasks are all consistent on disk.  After restart the front-end restores the
question bubble from ``pending_question`` (GET /run/state), and the user's
reply triggers ``start_or_inject`` → ``start_continuation`` (no active run)
which re-opens the session via ``FileSessionManager`` and passes the reply as
``seed_prompt``.  Forcing ``awaiting_input`` → ``interrupted`` would break that
path and lose the question bubble.

Additionally, for ``running``/``paused`` runs any tasks in tasks.yaml with
status ``in_progress`` are rolled back to ``todo``.  This matches the stop-path
in the orchestrator and prevents orphaned in_progress tasks from blocking
subsequent runs.  ``awaiting_input`` tasks are NOT rolled back (they were not
in-flight at crash time).

The scan is best-effort: if a state file is malformed or a directory is missing
it is skipped silently (we do not crash the server for a stale run).

Usage
-----
Call ``recover_interrupted_runs(root)`` from ``app.py`` lifespan:

    async with lifespan(app):
        await recover_interrupted_runs(settings.workspace_root)
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

from yukar.config import paths
from yukar.storage import state_repo, tasks_repo

logger = logging.getLogger(__name__)


async def _rollback_in_progress_tasks(root: str, project_id: str, epic_id: str) -> int:
    """Roll back any in_progress tasks to todo.

    Args:
        root: Workspace root.
        project_id: Project identifier.
        epic_id: Epic identifier.

    Returns:
        Number of tasks rolled back.
    """
    try:
        tf = await tasks_repo.get_tasks(root, project_id, epic_id)
    except Exception:
        logger.debug(
            "Could not read tasks.yaml for %s/%s; skipping task rollback",
            project_id,
            epic_id,
            exc_info=True,
        )
        return 0

    rolled_back = 0
    for task in tf.tasks:
        if task.status == "in_progress":
            task.status = "todo"
            rolled_back += 1

    if rolled_back:
        try:
            await tasks_repo.save_tasks(root, project_id, epic_id, tf)
            logger.info(
                "Rolled back %d in_progress task(s) to todo for %s/%s",
                rolled_back,
                project_id,
                epic_id,
            )
        except Exception:
            logger.warning(
                "Failed to write rolled-back tasks for %s/%s",
                project_id,
                epic_id,
                exc_info=True,
            )

    return rolled_back


async def recover_interrupted_runs(root: str) -> int:
    """Scan all state.yaml files and reconcile orphaned running/paused runs.

    For each run found in ``running`` or ``paused`` state:
    - Sets state.status = ``interrupted`` (crash-recovery marker).
    - Rolls back any ``in_progress`` tasks to ``todo`` so subsequent runs
      can re-execute them cleanly.

    Runs in ``awaiting_input`` state are intentionally left untouched.
    They have no in-flight async work (the Manager parked itself waiting for
    a human reply).  ``pending_question`` and session history are consistent
    on disk.  The front-end restores the question bubble from
    ``pending_question`` (GET /run/state), and the user's reply triggers
    ``start_or_inject`` → ``start_continuation`` which passes the reply as
    ``seed_prompt`` to a new continuation run — no task rollback needed.

    Args:
        root: Workspace root (``settings.workspace_root``).

    Returns:
        The number of runs that were reconciled (awaiting_input runs are not
        counted since they are not modified).
    """
    workspace = Path(root)
    if not workspace.exists():
        return 0

    reconciled = 0

    # Walk {root}/{project_id}/epics/{epic_id}/.yukar/state.yaml.
    #
    # Recovery runs on the server-startup path, so it must be fail-soft: one
    # malformed project/epic directory (a bad iterdir, a permission error, an
    # unexpected fs object) must NOT abort recovery for every other run nor crash
    # startup.  Each project-level and per-run iteration is therefore wrapped so a
    # single bad entry is logged and skipped while the rest proceed.
    for project_dir in sorted(workspace.iterdir()):
        try:
            if not project_dir.is_dir():
                continue
            project_id = project_dir.name

            epics_dir = paths.epics_dir(root, project_id)
            if not epics_dir.exists():
                continue

            epic_dirs = sorted(epics_dir.iterdir())
        except Exception:
            logger.warning(
                "Recovery: failed to enumerate project dir %s; skipping",
                project_dir,
                exc_info=True,
            )
            continue

        for epic_dir in epic_dirs:
            try:
                if not epic_dir.is_dir():
                    continue
                epic_id = epic_dir.name

                state_path = paths.state_yaml(root, project_id, epic_id)
                if not state_path.exists():
                    continue

                try:
                    state = await state_repo.get_state(root, project_id, epic_id)
                except Exception:
                    logger.debug(
                        "Could not read state.yaml for %s/%s; skipping",
                        project_id,
                        epic_id,
                        exc_info=True,
                    )
                    continue

                if state is None:
                    continue

                # awaiting_input is preserved as-is: the Manager is parked
                # waiting for a human reply, not executing in-flight work.
                # The reply path (start_or_inject → start_continuation) handles
                # resumption cleanly after restart — no task rollback needed.
                if state.status not in ("running", "paused"):
                    continue

                logger.info(
                    "Reconciling orphaned run %s for epic %s/%s (was %s → interrupted)",
                    state.run_id,
                    project_id,
                    epic_id,
                    state.status,
                )

                state.status = "interrupted"
                state.active_workers = []
                state.last_event_at = datetime.now(UTC)

                try:
                    await state_repo.save_state(root, project_id, epic_id, state)
                    reconciled += 1
                except Exception:
                    logger.warning(
                        "Failed to write reconciled state for %s/%s",
                        project_id,
                        epic_id,
                        exc_info=True,
                    )
                    continue

                # Roll back any in_progress tasks to todo (crash-recovery).
                await _rollback_in_progress_tasks(root, project_id, epic_id)
            except Exception:
                # Defence-in-depth: any unexpected error in a single run's
                # recovery is isolated so the remaining runs still recover and
                # server startup completes.
                logger.warning(
                    "Recovery: unexpected error reconciling epic dir %s; skipping",
                    epic_dir,
                    exc_info=True,
                )
                continue

    if reconciled:
        logger.info("Recovery complete: %d run(s) reconciled to interrupted state", reconciled)

    return reconciled
