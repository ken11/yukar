"""Docs router — GET/PUT for project-scoped and epic-scoped Markdown docs."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from yukar.api.routers import get_epic_or_404, get_project_or_404
from yukar.deps import WorkspaceRootDep
from yukar.storage import docs_repo

router = APIRouter(tags=["docs"])


class DocResponse(BaseModel):
    filename: str
    content: str
    scope: Literal["project", "epic"]


class PutDocRequest(BaseModel):
    content: str


# ---------------------------------------------------------------------------
# Project docs
# ---------------------------------------------------------------------------


@router.get("/api/projects/{project_id}/docs", response_model=list[str])
async def list_project_docs(project_id: str, root: WorkspaceRootDep) -> list[str]:
    return docs_repo.list_project_docs(root, project_id)


@router.get("/api/projects/{project_id}/docs/{filename}", response_model=DocResponse)
async def get_project_doc(project_id: str, filename: str, root: WorkspaceRootDep) -> DocResponse:
    try:
        content = docs_repo.get_project_doc(root, project_id, filename)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    return DocResponse(filename=filename, content=content, scope="project")


@router.put(
    "/api/projects/{project_id}/docs/{filename}",
    response_model=DocResponse,
    status_code=200,
)
async def put_project_doc(
    project_id: str, filename: str, body: PutDocRequest, root: WorkspaceRootDep
) -> DocResponse:
    # A5-01: Verify the project exists before writing to avoid ghost directories.
    await get_project_or_404(root, project_id)
    try:
        await docs_repo.put_project_doc(root, project_id, filename, body.content)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    return DocResponse(filename=filename, content=body.content, scope="project")


# ---------------------------------------------------------------------------
# Epic docs
# ---------------------------------------------------------------------------


@router.get("/api/projects/{project_id}/epics/{epic_id}/docs", response_model=list[str])
async def list_epic_docs(project_id: str, epic_id: str, root: WorkspaceRootDep) -> list[str]:
    return docs_repo.list_epic_docs(root, project_id, epic_id)


@router.get(
    "/api/projects/{project_id}/epics/{epic_id}/docs/{filename}",
    response_model=DocResponse,
)
async def get_epic_doc(
    project_id: str, epic_id: str, filename: str, root: WorkspaceRootDep
) -> DocResponse:
    try:
        content = docs_repo.get_epic_doc(root, project_id, epic_id, filename)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    return DocResponse(filename=filename, content=content, scope="epic")


@router.put(
    "/api/projects/{project_id}/epics/{epic_id}/docs/{filename}",
    response_model=DocResponse,
)
async def put_epic_doc(
    project_id: str,
    epic_id: str,
    filename: str,
    body: PutDocRequest,
    root: WorkspaceRootDep,
) -> DocResponse:
    # A5-01: Verify both project and epic exist before writing to avoid ghost directories.
    await get_project_or_404(root, project_id)
    await get_epic_or_404(root, project_id, epic_id)
    try:
        await docs_repo.put_epic_doc(root, project_id, epic_id, filename, body.content)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    return DocResponse(filename=filename, content=body.content, scope="epic")
