"""Strands tool factories for EpicOrchestrator effector tools.

Contains the module-level tool factory ``_make_task_update_tool`` and
supporting constants/types extracted from :mod:`~yukar.agents.orchestrator`
for readability.  All public names are re-exported from ``orchestrator.py``
so existing imports remain unchanged.

Note on ``_TaskStatus``
-----------------------
This type alias must be importable at the module level because ``strands.tool``
uses ``get_type_hints()`` to inspect decorated function signatures.  Placing it
here (rather than inside the factory function) ensures that the hint is always
resolvable regardless of which module calls the factory.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, Literal

from yukar.events import bus as event_bus
from yukar.models.events import TaskUpdateEvent
from yukar.models.task import Task, TaskProgress, TasksFile
from yukar.storage import tasks_repo

# Type alias used by the task_update Strands tool parameter.
# Must be module-level so get_type_hints() can resolve it.
_TaskStatus = Literal["todo", "in_progress", "done", "blocked"]


def _make_task_update_tool(
    root: str,
    project_id: str,
    epic_id: str,
    run_id: str,
    _tasks_holder: list[TasksFile],
    on_change: Callable[[], None] | None = None,
    state_lock: asyncio.Lock | None = None,
) -> Any:
    """Return a Strands tool that lets the Manager update tasks.yaml.

    The tool writes through to disk via tasks_repo and also updates the
    in-memory ``_tasks_holder[0]`` so the orchestrator loop can see changes.
    ``_tasks_holder`` is a one-element list so the closure can mutate it.

    ``on_change`` (optional) is invoked after every successful task mutation so
    a caller can react to a plan change.  Approval needs no invalidation hook:
    the dispatch gate compares the CURRENT plan hash against the recorded
    approval on every call, so a changed plan simply stops matching.

    ``state_lock`` serialises the copy→save→commit cycle.  Strands runs the
    tool calls of one assistant message CONCURRENTLY, so two task_update calls
    would otherwise each snapshot the shared TasksFile before the other's
    commit — the later disk write then lacks the earlier task (lost update).
    Passing the orchestrator's state lock also serialises against dispatch's
    status flips, which mutate the same shared file under that lock.
    """
    from strands import tool

    lock = state_lock or asyncio.Lock()

    @tool
    async def task_update(
        task_id: str,
        title: str,
        status: _TaskStatus = "todo",
        repo: str | None = None,
        depends_on: list[str] | None = None,
        contract: str = "",
        agent_profile: str = "",
    ) -> dict[str, Any]:
        """Create or update a task in tasks.yaml.

        Call this once per task discovered during planning.

        Args:
            task_id: Short identifier, e.g. ``T1``.
            title: Human-readable task title.
            status: ``todo`` | ``in_progress`` | ``done`` | ``blocked``.
            repo: Target repository name (optional; required for Worker tasks).
            depends_on: List of task_ids this task depends on.
            contract: What to build and how the Evaluator will verify it.
                      Required — write a concrete, testable contract so the
                      Worker knows exactly what to implement and the Evaluator
                      can make an objective accept/reject decision.
            agent_profile: Named agent profile to assign to this task (e.g.
                   ``"frontend-worker"`` or ``"backend-worker"``).  Leave empty
                   to use the default role config.  Use ``write_agent_profile``
                   to create profiles before assigning them here.

        Returns:
            Confirmation dict with ``task_id`` and ``status``.
        """
        def _apply(target: TasksFile) -> None:
            """Find-or-create the task on *target* and recompute progress."""
            existing = next((t for t in target.tasks if t.id == task_id), None)
            if existing is not None:
                existing.title = title
                existing.status = status
                if repo is not None:
                    existing.repo = repo
                if depends_on is not None:
                    existing.depends_on = depends_on
                if contract:
                    existing.contract = contract
                if agent_profile:
                    existing.agent = agent_profile
            else:
                target.tasks.append(
                    Task(
                        id=task_id,
                        title=title,
                        status=status,
                        repo=repo,
                        depends_on=depends_on or [],
                        contract=contract,
                        agent=agent_profile or None,
                    )
                )
            done_count = sum(1 for t in target.tasks if t.status == "done")
            target.progress = TaskProgress(done=done_count, total=len(target.tasks))

        # Persist FIRST (on a copy), then commit to the shared in-memory file.
        # The reverse order let a failed disk write leave the holder ahead of
        # tasks.yaml: the plan-approval gate and the REST surface then disagree
        # until the run is restarted.  Memory must never be ahead of disk.
        # The commit re-applies the same change in place so Task references
        # held by a concurrently running dispatch stay valid.  The whole cycle
        # runs under the lock — see the factory docstring for why.
        async with lock:
            tf = _tasks_holder[0]
            persisted = tf.model_copy(deep=True)
            _apply(persisted)
            await tasks_repo.save_tasks(root, project_id, epic_id, persisted)
            _apply(tf)
            _tasks_holder[0] = tf
        # Notify the orchestrator that the plan may have changed.
        if on_change is not None:
            on_change()
        # Publish task_update event.  plan_changed=True: this tool can touch
        # plan-defining fields, so clients must refetch the plan hash /
        # approval state rather than patch the task row in place.
        event_bus.publish(
            project_id,
            epic_id,
            TaskUpdateEvent(
                project_id=project_id,
                epic_id=epic_id,
                run_id=run_id,
                task_id=task_id,
                status=status,
                title=title,
                plan_changed=True,
            ),
        )
        return {"task_id": task_id, "status": status, "title": title}

    return task_update
