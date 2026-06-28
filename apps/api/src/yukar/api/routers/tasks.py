"""Tasks router — GET/PUT /api/projects/{p}/epics/{e}/tasks."""

from __future__ import annotations

from fastapi import APIRouter

from yukar.api.routers import get_epic_or_404
from yukar.deps import WorkspaceRootDep
from yukar.models.task import TasksFile
from yukar.storage import tasks_repo

router = APIRouter(
    prefix="/api/projects/{project_id}/epics/{epic_id}",
    tags=["tasks"],
)


@router.get("/tasks", response_model=TasksFile)
async def get_tasks(project_id: str, epic_id: str, root: WorkspaceRootDep) -> TasksFile:
    await get_epic_or_404(root, project_id, epic_id)
    return await tasks_repo.get_tasks(root, project_id, epic_id)


@router.put("/tasks", response_model=TasksFile)
async def put_tasks(
    project_id: str, epic_id: str, body: TasksFile, root: WorkspaceRootDep
) -> TasksFile:
    await get_epic_or_404(root, project_id, epic_id)
    await tasks_repo.save_tasks(root, project_id, epic_id, body)
    return body
