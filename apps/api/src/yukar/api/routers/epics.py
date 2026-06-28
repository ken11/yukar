"""Epics router — CRUD under /api/projects/{p}/epics/."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from yukar.api.routers import get_epic_or_404, get_project_or_404
from yukar.deps import SupervisorDep, WorkspaceRootDep
from yukar.events import bus as event_bus
from yukar.models.epic import Epic
from yukar.models.events import EpicStatusChangedEvent
from yukar.storage import epic_repo, project_repo

router = APIRouter(prefix="/api/projects/{project_id}/epics", tags=["epics"])


def _slugify(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")[:50]


# ---------------------------------------------------------------------------
# Request/Response
# ---------------------------------------------------------------------------


class CreateEpicRequest(BaseModel):
    title: str
    description: str = ""
    acceptance_criteria: str = ""
    manager_effort: Literal["high", "xhigh", "max"] = "high"


class PatchEpicRequest(BaseModel):
    title: str | None = None
    description: str | None = None
    acceptance_criteria: str | None = None
    status: Literal["planned", "in_progress", "completed", "failed", "closed", "merged"] | None = (
        None
    )
    manager_effort: Literal["high", "xhigh", "max"] | None = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=list[Epic])
async def list_epics(
    project_id: str,
    root: WorkspaceRootDep,
    include_closed: bool = False,
) -> list[Epic]:
    await get_project_or_404(root, project_id)
    epics = await epic_repo.list_epics(root, project_id)
    if not include_closed:
        epics = [e for e in epics if e.status != "closed"]
    return epics


@router.post("", response_model=Epic, status_code=201)
async def create_epic(project_id: str, body: CreateEpicRequest, root: WorkspaceRootDep) -> Epic:
    await get_project_or_404(root, project_id)

    counter = await project_repo.increment_epic_counter(root, project_id)
    epic_id = epic_repo.make_epic_id(counter)
    slug = _slugify(body.title)
    branch = epic_repo.make_branch_name(epic_id, slug)

    now = datetime.now(UTC)
    epic = Epic(
        id=epic_id,
        slug=slug,
        title=body.title,
        description=body.description,
        acceptance_criteria=body.acceptance_criteria,
        status="planned",
        branch=branch,
        touched_repos=[],
        manager_effort=body.manager_effort,
        created_at=now,
        updated_at=now,
    )
    await epic_repo.save_epic(root, project_id, epic)
    return epic


@router.get("/{epic_id}", response_model=Epic)
async def get_epic(project_id: str, epic_id: str, root: WorkspaceRootDep) -> Epic:
    return await get_epic_or_404(root, project_id, epic_id)


@router.patch("/{epic_id}", response_model=Epic)
async def patch_epic(
    project_id: str,
    epic_id: str,
    body: PatchEpicRequest,
    root: WorkspaceRootDep,
    supervisor: SupervisorDep,
) -> Epic:
    # Guard: prevent setting a terminal status while a run is active —
    # consistent with the dedicated /close endpoint.
    if body.status in {"closed", "merged"} and supervisor.is_running(project_id, epic_id):
        raise HTTPException(status_code=409, detail="A run is active — close is not allowed")
    epic = await get_epic_or_404(root, project_id, epic_id)
    previous_status = epic.status
    if body.title is not None:
        epic.title = body.title
    if body.description is not None:
        epic.description = body.description
    if body.acceptance_criteria is not None:
        epic.acceptance_criteria = body.acceptance_criteria
    if body.status is not None:
        epic.status = body.status
    if body.manager_effort is not None:
        epic.manager_effort = body.manager_effort
    epic.updated_at = datetime.now(UTC)
    await epic_repo.save_epic(root, project_id, epic)
    if body.status is not None and epic.status != previous_status:
        event_bus.publish(
            project_id,
            epic_id,
            EpicStatusChangedEvent(
                project_id=project_id,
                epic_id=epic_id,
                run_id="",
                status=epic.status,
            ),
        )
    return epic


@router.post("/{epic_id}/close", response_model=Epic)
async def close_epic(
    project_id: str,
    epic_id: str,
    root: WorkspaceRootDep,
    supervisor: SupervisorDep,
) -> Epic:
    """Mark an epic as closed (user-driven terminal status).

    Returns 409 if a run is currently active — the caller must stop the run
    first.  Closing is idempotent: closing an already-closed epic is allowed
    (the updated_at timestamp is refreshed).

    Publishes an EpicStatusChangedEvent so other browser tabs can react
    without polling.
    """
    if supervisor.is_running(project_id, epic_id):
        raise HTTPException(status_code=409, detail="A run is active — close is not allowed")
    epic = await get_epic_or_404(root, project_id, epic_id)
    epic.status = "closed"
    epic.updated_at = datetime.now(UTC)
    await epic_repo.save_epic(root, project_id, epic)
    event_bus.publish(
        project_id,
        epic_id,
        EpicStatusChangedEvent(
            project_id=project_id,
            epic_id=epic_id,
            run_id="",
            status="closed",
        ),
    )
    return epic
