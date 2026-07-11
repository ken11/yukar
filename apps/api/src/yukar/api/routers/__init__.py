"""Router helpers shared across routers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import HTTPException

from yukar.models.epic import Epic
from yukar.models.project import Project, Repo
from yukar.storage.epic_repo import get_epic
from yukar.storage.project_repo import get_project, get_repo

if TYPE_CHECKING:
    from yukar.runs.supervisor import RunSupervisor


async def get_project_or_404(root: str, project_id: str) -> Project:
    """Return the Project or raise HTTPException(404)."""
    project = await get_project(root, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail=f"Project not found: {project_id}")
    return project


async def get_epic_or_404(root: str, project_id: str, epic_id: str) -> Epic:
    """Return the Epic or raise HTTPException(404)."""
    epic = await get_epic(root, project_id, epic_id)
    if epic is None:
        raise HTTPException(status_code=404, detail=f"Epic not found: {epic_id}")
    return epic


async def get_repo_or_404(root: str, project_id: str, repo_name: str) -> Repo:
    """Return the Repo or raise HTTPException(404)."""
    repo = await get_repo(root, project_id, repo_name)
    if repo is None:
        raise HTTPException(status_code=404, detail=f"Repo not found: {repo_name}")
    return repo


async def shelve_or_409(supervisor: RunSupervisor, project_id: str, epic_id: str) -> None:
    """Yield the epic's run slot before a destructive/mutating operation.

    A live run parked in ``waiting`` is shelved (task cancelled, state.yaml
    preserved — the conversation resumes as a continuation on the next
    message).  If the shelve is refused AND a live task still exists, the run
    woke up between the caller's ``is_executing`` check and now (a user reply
    landed) — proceeding would mutate the epic under an executing turn, so
    the operation is rejected with 409 instead of silently racing it.
    """
    shelved = await supervisor.shelve_waiting(project_id, epic_id)
    if not shelved and supervisor.is_running(project_id, epic_id):
        raise HTTPException(
            status_code=409,
            detail=(
                "The run woke up (a reply arrived) while this operation was "
                "starting — it is executing now. Retry once the turn ends."
            ),
        )
