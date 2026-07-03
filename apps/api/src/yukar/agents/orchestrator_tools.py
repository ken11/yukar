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

from collections.abc import Callable
from typing import Any, Literal

from yukar.events import bus as event_bus
from yukar.models.events import TaskUpdateEvent
from yukar.models.task import Task, TaskProgress, TasksFile
from yukar.storage import tasks_repo

# Type alias used by the task_update Strands tool parameter.
# Must be module-level so get_type_hints() can resolve it.
_TaskStatus = Literal["todo", "in_progress", "done", "blocked"]


class _ManagerTurnLimitError(Exception):
    """Raised by _run_loop when _MAX_MANAGER_TURNS is exhausted without complete_epic.

    Signals start() to set run state to ``error`` per spec §6.2.
    """


def _make_task_update_tool(
    root: str,
    project_id: str,
    epic_id: str,
    run_id: str,
    _tasks_holder: list[TasksFile],
    on_change: Callable[[], None] | None = None,
) -> Any:
    """Return a Strands tool that lets the Manager update tasks.yaml.

    The tool writes through to disk via tasks_repo and also updates the
    in-memory ``_tasks_holder[0]`` so the orchestrator loop can see changes.
    ``_tasks_holder`` is a one-element list so the closure can mutate it.

    ``on_change`` (optional) is invoked after every successful task mutation so
    the orchestrator can react to a plan change — e.g. invalidate the
    plan-approval gate so the Manager must re-confirm with the user before
    dispatching.
    """
    from strands import tool

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
        tf = _tasks_holder[0]
        # Find existing or create new.
        existing = next((t for t in tf.tasks if t.id == task_id), None)
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
            tf.tasks.append(
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
        # Recompute progress.
        done_count = sum(1 for t in tf.tasks if t.status == "done")
        tf.progress = TaskProgress(done=done_count, total=len(tf.tasks))
        _tasks_holder[0] = tf
        # Persist.
        await tasks_repo.save_tasks(root, project_id, epic_id, tf)
        # Notify the orchestrator that the plan changed (invalidates approval).
        if on_change is not None:
            on_change()
        # Publish task_update event.
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
            ),
        )
        return {"task_id": task_id, "status": status, "title": title}

    return task_update
