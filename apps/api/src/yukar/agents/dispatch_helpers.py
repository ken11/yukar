"""Dispatch helpers — stateless utilities used by the dispatch layer.

Contains:
- ``register_agent_thread``: session + threads.yaml index registration.
- ``publish_diff_update``: git diff --stat → DiffUpdateEvent.
- ``get_first_repo``: return the first registered repo or None.
- ``publish_task_update``: emit TaskUpdateEvent (boilerplate wrapper).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from yukar.models.events import DiffUpdateEvent
from yukar.models.roles import ThreadRole
from yukar.storage import session_store, threads_repo

logger = logging.getLogger(__name__)


async def register_agent_thread(
    root: str,
    project_id: str,
    epic_id: str,
    thread_id: str,
    role: ThreadRole,
    repo: str | None = None,
    task_id: str | None = None,
    parent_thread_id: str | None = None,
    title: str | None = None,
) -> None:
    """Ensure agent session entry and threads.yaml index entry exist.

    ``parent_thread_id`` wires the agent tree:
    - manager:   ``None``
    - worker:    ``"manager"``
    - evaluator: the worker_id it evaluated

    ``title`` overrides the default ``"{Role} {thread_id}"`` display name.
    """
    from yukar.models.thread import ThreadEntry, ThreadsFile

    state: dict[str, Any] = {"role": role, "status": "active"}
    if repo:
        state["repo"] = repo
    if task_id:
        state["task"] = task_id
    await session_store.ensure_agent(root, project_id, epic_id, thread_id, state=state)

    tf = await threads_repo.get_threads(root, project_id, epic_id)
    if not any(t.id == thread_id for t in tf.threads):
        thread_title = title if title is not None else f"{role.capitalize()} {thread_id}"
        tf.threads.append(
            ThreadEntry(
                id=thread_id,
                title=thread_title,
                role=role,
                repo=repo,
                task=task_id,
                status="active",
                parent_thread_id=parent_thread_id,
            )
        )
        await threads_repo.save_threads(root, project_id, epic_id, ThreadsFile(threads=tf.threads))


async def publish_diff_update(
    project_id: str,
    epic_id: str,
    run_id: str,
    repo_name: str,
    worktree_path: Path,
    pub: Callable[[object], None],
) -> int:
    """Publish DiffUpdateEvent with file count from git diff --stat.

    Returns the number of files changed (0 on error).
    """
    files_changed = 0
    try:
        from yukar.git.runner import run_git

        result = await run_git(
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--cached",
            "--stat",
            cwd=worktree_path,
            check=False,
        )
        lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
        files_changed = max(0, len(lines) - 1) if lines else 0
        pub(
            DiffUpdateEvent(
                project_id=project_id,
                epic_id=epic_id,
                run_id=run_id,
                repo=repo_name,
                files_changed=files_changed,
            )
        )
    except Exception:
        logger.debug("Could not compute diff stat", exc_info=True)
    return files_changed


async def get_first_repo(root: str, project_id: str) -> Any:
    """Return first registered repo or None."""
    from yukar.storage.project_repo import list_repos

    repos = await list_repos(root, project_id)
    return repos[0] if repos else None


def publish_task_update(
    pub: Callable[[object], None],
    project_id: str,
    epic_id: str,
    run_id: str,
    task_id: str,
    status: str,
    title: str,
) -> None:
    """Publish a TaskUpdateEvent — consolidates the repeated boilerplate."""
    from yukar.models.events import TaskUpdateEvent

    pub(
        TaskUpdateEvent(
            project_id=project_id,
            epic_id=epic_id,
            run_id=run_id,
            task_id=task_id,
            status=status,
            title=title,
        )
    )
