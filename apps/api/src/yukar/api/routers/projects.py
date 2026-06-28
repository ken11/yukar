"""Projects router — GET/POST /api/projects, GET/PATCH/DELETE /api/projects/{p}."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, Field

from yukar.api.routers import get_project_or_404
from yukar.deps import IndexerServiceDep, WorkspaceRootDep
from yukar.models.project import Project, Repo, RepoCommands
from yukar.storage import project_repo

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/projects", tags=["projects"])


# ---------------------------------------------------------------------------
# Request / Response bodies
# ---------------------------------------------------------------------------


class RepoInput(BaseModel):
    name: str
    path: str
    default_branch: str = "main"
    commands: RepoCommands = Field(default_factory=RepoCommands)


class CreateProjectRequest(BaseModel):
    id: str
    name: str
    repos: list[RepoInput] = []


class PatchProjectRequest(BaseModel):
    name: str | None = None
    status: Literal["active", "idle"] | None = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=list[Project])
async def list_projects(root: WorkspaceRootDep) -> list[Project]:
    return await project_repo.list_projects(root)


@router.post("", response_model=Project, status_code=201)
async def create_project(
    body: CreateProjectRequest,
    root: WorkspaceRootDep,
    indexer: IndexerServiceDep,
    background_tasks: BackgroundTasks,
) -> Project:
    # Check for duplicate
    existing = await project_repo.get_project(root, body.id)
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"Project already exists: {body.id}")

    # Validate ALL repos first — before writing any files — so a bad repo
    # cannot leave an orphaned project.yaml on disk (fix #5).
    for r in body.repos:
        try:
            project_repo.resolve_git_repo(r.path)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e

    now = datetime.now(UTC)
    project = Project(
        id=body.id,
        name=body.name,
        status="active",
        repos=[r.name for r in body.repos],
        epic_counter=0,
        created_at=now,
        updated_at=now,
    )
    await project_repo.save_project(root, project)

    # Persist repo metadata
    for r in body.repos:
        repo = Repo(
            name=r.name,
            path=r.path,
            default_branch=r.default_branch,
            commands=r.commands,
        )
        await project_repo.save_repo(root, project.id, repo)

    # Kick off initial indexing in background (spec §7.9: New Project → initial index).
    # Does not block the 201 response.
    project_id_captured = project.id
    repos_captured = list(body.repos)

    async def _initial_index() -> None:
        for r in repos_captured:
            try:
                n = await indexer.reindex_repo(project_id_captured, r.name, Path(r.path))
                logger.info("Initial index %s/%s: %d chunks", project_id_captured, r.name, n)
            except Exception:
                logger.exception("Initial index %s/%s failed", project_id_captured, r.name)

    background_tasks.add_task(_initial_index)

    return project


@router.get("/{project_id}", response_model=Project)
async def get_project(project_id: str, root: WorkspaceRootDep) -> Project:
    return await get_project_or_404(root, project_id)


@router.patch("/{project_id}", response_model=Project)
async def patch_project(
    project_id: str, body: PatchProjectRequest, root: WorkspaceRootDep
) -> Project:
    project = await get_project_or_404(root, project_id)
    if body.name is not None:
        project.name = body.name
    if body.status is not None:
        project.status = body.status
    project.updated_at = datetime.now(UTC)
    await project_repo.save_project(root, project)
    return project


@router.delete("/{project_id}", status_code=204)
async def delete_project(project_id: str, root: WorkspaceRootDep) -> None:
    await get_project_or_404(root, project_id)
    await project_repo.delete_project(root, project_id)
